#!/usr/bin/env python3
"""The busy probe (ANYM-684 criterion 3): tells the inference service when Between or a DeedKit test run is active.

Stdlib only. It runs as the admin account (the only user who sees Between's containers and Docker contexts), as a
LaunchDaemon of its own with KeepAlive, from /usr/local/libexec/deedkit-busy-probe (root-owned: the service user
cannot change code that runs as the admin account). Every 5 s it writes

    {"ts": <epoch>, "between": bool, "deedkit_ci": bool}

to /Users/Shared/deedkit-inference/busy.json, atomically, mode 0640 (owner writes, the service user's group reads).
The service (ANYM-682) treats a missing, unparseable or older than 30 s file as busy, so a dead probe closes
admission. Any failure to observe sets both flags true: Between and the test runs come first.

  between     a container named or labelled Between runs in any Docker context of the admin account whose socket
              exists (scripts/remote-test.sh checks only the default context); a context that cannot be queried is busy.
              Between's CI runs inside the colima VMs of the user _betweenci, whose containers the admin account cannot
              list (11-02: a full pytest job ran there while this flag read false). What the admin account does see is
              the VM's Virtualization process, whose CPU is the guest's CPU: `between` is also true while the summed
              %cpu of those processes shows work (VmLoad below). The runner processes inside the guest are invisible.
  deedkit_ci  a process of _deedkitci whose arguments name .locks/slot: the run slot is a flock held by a python
              process of that user in its private home, which the admin account cannot open.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time

DEFAULT_FILE = '/Users/Shared/deedkit-inference/busy.json'
INTERVAL = 5
COMMAND_TIMEOUT = 5
CI_USER = '_deedkitci'
SLOT_MARK = '.locks/slot'
BETWEEN_USER = '_betweenci'
VM_MARK = 'com.apple.Virtualization.VirtualMachine'
# Recorded on the mini 11-02: idle VMs sit at 0.2-7 % (summed), a CI job spikes to 90-100 % and idles within seconds.
VM_WINDOW = 60.0  # seconds of samples averaged
VM_MEAN = 10.0  # busy while the window's mean is at least this many % of one core
VM_SPIKE = 50.0  # one sample this high is busy at once
VM_HOLD = 120.0  # and stays busy this long after the last high reading: a bursty job must not reopen admission
BETWEEN = re.compile(r'^between-|io\.between\.', re.IGNORECASE)
BUSY = {'between': True, 'deedkit_ci': True}


def default_run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=True,
                          stdin=subprocess.DEVNULL).stdout


def default_uid(user: str) -> int:
    return pwd.getpwnam(user).pw_uid


def between_busy(run: Callable[[list[str]], str], socket_exists: Callable[[str], bool], docker: str) -> bool:
    busy = False
    for line in run([docker, 'context', 'ls', '--format', '{{json .}}']).splitlines():
        if not line.strip():
            continue
        context = json.loads(line)
        name, endpoint = context['Name'], context.get('DockerEndpoint', '')
        if endpoint.startswith('unix://') and not socket_exists(endpoint[len('unix://'):]):
            continue  # a context without a running VM behind it holds no container
        listing = run([docker, '--context', name, 'ps', '--format', '{{.Names}} {{.Labels}}'])
        busy = busy or any(BETWEEN.search(entry) for entry in listing.splitlines())
    return busy


def ci_busy(run: Callable[[list[str]], str], uid_of: Callable[[str], int]) -> bool:
    try:
        uid = uid_of(CI_USER)
    except KeyError:
        return False  # no such user on this host: no process can run as it
    for line in run(['/bin/ps', '-axo', 'uid=,args=']).splitlines():
        fields = line.split(None, 1)
        if len(fields) == 2 and fields[0] == str(uid) and SLOT_MARK in fields[1]:
            return True
    return False


class VmLoad:
    """The CPU of Between's VMs over time. Keeps the samples of the last VM_WINDOW seconds and the time of the last
    high reading; the state lives as long as the probe process (a restart starts empty and reads one sample)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.samples: list[tuple[float, float]] = []
        self.last_high: float | None = None

    def update(self, cpu: float) -> bool:
        now = self.clock()
        self.samples = [(at, value) for at, value in self.samples if now - at <= VM_WINDOW] + [(now, cpu)]
        mean = sum(value for _, value in self.samples) / len(self.samples)
        if cpu >= VM_SPIKE or mean >= VM_MEAN:
            self.last_high = now
        return self.last_high is not None and now - self.last_high < VM_HOLD


def vm_cpu(run: Callable[[list[str]], str], uid_of: Callable[[str], int]) -> float | None:
    """Summed %cpu of the Virtualization processes of _betweenci, or None when that user does not exist."""
    try:
        uid = uid_of(BETWEEN_USER)
    except KeyError:
        return None
    total = 0.0
    for line in run(['/bin/ps', '-axo', 'uid=,pcpu=,comm=']).splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0] == str(uid) and fields[2].endswith(VM_MARK):
            try:
                total += float(fields[1])
            except ValueError:
                continue
    return total


def observe(run: Callable[[list[str]], str] = default_run, *, socket_exists: Callable[[str], bool] = os.path.exists,
            uid_of: Callable[[str], int] = default_uid, docker: str | None = None,
            between_ci: VmLoad | None = None) -> dict[str, bool]:
    """Both flags, or both true when anything could not be observed. Never raises. `between_ci` carries the VM
    readings from one call to the next; without one a call sees a single reading."""
    try:
        docker = docker or shutil.which('docker', path='/opt/homebrew/bin:/usr/local/bin:/usr/bin') or 'docker'
        between = between_busy(run, socket_exists, docker)
        cpu = vm_cpu(run, uid_of)
        if cpu is not None:
            between = (between_ci or VmLoad()).update(cpu) or between
        return {'between': between, 'deedkit_ci': ci_busy(run, uid_of)}
    except Exception:  # noqa: BLE001 - every failure to observe is busy
        return dict(BUSY)


def observe_safely(observer: Callable[[], dict[str, bool]]) -> dict[str, bool]:
    try:
        state = observer()
        if set(state) != {'between', 'deedkit_ci'} or not all(isinstance(value, bool) for value in state.values()):
            return dict(BUSY)
        return state
    except Exception:  # noqa: BLE001
        return dict(BUSY)


def write_busy(path: str | Path, state: dict[str, bool], now: float) -> None:
    """Atomic: a 0640 temporary file in the same directory, then rename. A failure leaves the old file and no temp."""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.busy.')
    try:
        os.fchmod(descriptor, 0o640)  # whatever the umask; the directory's group is the service user's
        with os.fdopen(descriptor, 'w') as handle:
            json.dump({'ts': now, 'between': state['between'], 'deedkit_ci': state['deedkit_ci']}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        unlink_quietly(temporary)
        raise


def unlink_quietly(name: str) -> None:
    try:
        os.unlink(name)
    except OSError:
        pass


def loop(path: str | Path, *, observe: Callable[[], dict[str, bool]] = observe, clock: Callable[[], float] = time.time,
         sleep: Callable[[float], None] = time.sleep, rounds: int | None = None, interval: float = INTERVAL) -> None:
    done = 0
    while rounds is None or done < rounds:
        state = observe_safely(observe)
        try:
            write_busy(path, state, clock())
        except Exception as exc:  # noqa: BLE001 - the file goes stale and the service reads that as busy
            print(f'busy file not written: {type(exc).__name__}', file=sys.stderr, flush=True)
        done += 1
        if rounds is None or done < rounds:
            sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or '').split('\n')[0])
    parser.add_argument('--file', default=DEFAULT_FILE)
    parser.add_argument('--interval', type=float, default=INTERVAL)
    parser.add_argument('--docker', default=None)
    parser.add_argument('--once', action='store_true', help='print one observation as JSON; exit 0 only if idle')
    args = parser.parse_args(argv)
    if args.once:
        state = observe_safely(lambda: observe(docker=args.docker))
        print(json.dumps(state))
        return 0 if not any(state.values()) else 1
    vm_load = VmLoad()
    loop(args.file, observe=lambda: observe(docker=args.docker, between_ci=vm_load), interval=args.interval)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
