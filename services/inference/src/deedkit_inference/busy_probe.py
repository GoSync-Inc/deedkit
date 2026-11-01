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


def observe(run: Callable[[list[str]], str] = default_run, *, socket_exists: Callable[[str], bool] = os.path.exists,
            uid_of: Callable[[str], int] = default_uid, docker: str | None = None) -> dict[str, bool]:
    """Both flags, or both true when anything could not be observed. Never raises."""
    try:
        docker = docker or shutil.which('docker', path='/opt/homebrew/bin:/usr/local/bin:/usr/bin') or 'docker'
        return {'between': between_busy(run, socket_exists, docker), 'deedkit_ci': ci_busy(run, uid_of)}
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
    loop(args.file, observe=lambda: observe(docker=args.docker), interval=args.interval)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
