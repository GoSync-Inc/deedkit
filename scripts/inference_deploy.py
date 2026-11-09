#!/usr/bin/env python3
"""Deploy or roll back the inference service on the Mac mini (ANYM-684 criterion 7).

    scripts/inference_deploy.py <sha>              deploy a green commit of main
    scripts/inference_deploy.py --rollback         repoint `current` to the previous release
    scripts/inference_deploy.py --stage-only <sha> first install: unpack, sync and point `current`; the service is not touched
    options: --force (SIGKILL after the drain limit), --host USER@ADDR (else DEEDKIT_REMOTE_HOST, else the first
    line of ~/.config/deedkit/mini-host, as scripts/remote-test.sh)

A deploy never cuts a job. In this order, over ssh as the mini's admin account (the service user's files through `sudo -n -u`):

 1. refuse unless <sha> is on main and its main push run passed `CI gate` and `checks / verify`;
 2. take the deploy lock (a flock held by a process on the mini for the length of the ssh session: two deploys
    cannot run, and a dropped connection frees it);
 3. unpack `services/inference` of that commit into `releases/<sha>/` and `uv sync --locked --group engines` there,
    then `uv sync --locked --compile-bytecode` in its `docling/` (ANYM-850: the Docling engine's own venv; the bytecode
    is compiled here because the service cannot write the release, and a cold child without it imports for about a
    minute), while the running release keeps serving; `.ready` is written only after both;
 4. SIGTERM the service: admission closes at once, queued and running jobs finish, it exits 0 (ANYM-682). The
    script waits up to the drain limit and refuses to kill (`--force` kills after it). The LaunchDaemon's KeepAlive
    {SuccessfulExit: false} does not restart a clean exit; after the service's own drain limit it exits 3 and launchd
    restarts the old release, which is stopped again. Then the job is unloaded (`launchctl bootout`: graceful, SIGTERM
    first), so nothing can restart the old release between the last look and the switch;
 5. switch `current` (the old target becomes `previous`), `launchctl bootstrap` and `kickstart` (no -k), read back `/health` and
    `/v1/models`: its `release` must equal the deployed SHA.

The script touches `releases/`, `current` and `previous` of the service home and reads `probe.key`; never the model
store, `policy.json`, the busy file or the setup ledger. It does not roll back by itself: a failed read-back says
which command does.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Generator
import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import production  # noqa: E402  the GitHub reads and the Refused type of promotion

ROOT = Path(__file__).resolve().parents[1]
USER = '_deedkitinf'
HOME = '/Users/' + USER
LABEL = 'com.deedkit.inference'
PLIST = '/Library/LaunchDaemons/com.deedkit.inference.plist'
UNLOAD_TIMEOUT = 1100                # `launchctl bootout --wait` is graceful: SIGTERM, then SIGKILL after ExitTimeOut (960 s)
PORT = 8090
PYTHON = '/opt/homebrew/bin/python3.13'
UV = '/opt/homebrew/bin/uv'
# `UV_NO_CONFIG=1` (the sync below) also drops the project's own `[tool.uv]` settings, its wheel-only
# `no-build-package` list included (ANYM-850 Astra r1, reproduced with uv 0.9.26). So the sync writes that list from
# the release's pyproject.toml into a uv.toml-format file and passes it with `--config-file`; a missing or malformed
# list stops the `&&` chain before any install, so the release is never marked ready.
WHEEL_ONLY = ('import json, re, sys, tomllib; '
              'names = tomllib.load(open(sys.argv[1], "rb")).get("tool", {}).get("uv", {}).get("no-build-package"); '
              'ok = isinstance(names, list) and names and all(isinstance(n, str) and re.fullmatch(r"[a-z0-9][a-z0-9._-]*", n) for n in names); '
              'sys.exit("no wheel-only list in " + sys.argv[1]) if not ok else print("no-build-package = " + json.dumps(names))')
DRAIN_SECONDS = 900                  # the service's own limit (ANYM-682 drain_seconds)
DRAIN_LIMIT = DRAIN_SECONDS + 60     # how long this script waits for the exit
MAX_RESTARTS = 3                     # launchd restarts of the old release one deploy will drain before it gives up
START_WAIT = 60
POLL = 5
SHA = re.compile(r'[0-9a-f]{40}')

# Held by a process on the mini for as long as its stdin is open: closing the ssh session frees the lock.
LOCK_HOLDER = '''import fcntl, sys
f = open(sys.argv[1], "a")
try:
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    print("busy", flush=True)
    sys.exit(1)
print("held", flush=True)
sys.stdin.read()
'''

# Run on the mini as the service user with the key file as its argument; the key never appears in an argument list.
MODELS_SCRIPT = '''import json, sys, urllib.request
key = open(sys.argv[1]).read().strip()
request = urllib.request.Request("http://127.0.0.1:%d/v1/models", headers={"Authorization": "Bearer " + key})
with urllib.request.urlopen(request, timeout=10) as response:
    sys.stdout.write(response.read(1 << 20).decode())
''' % PORT


class DeployError(RuntimeError):
    pass


class Refused(DeployError):
    pass


class Busy(DeployError):
    pass


class DrainTimeout(DeployError):
    pass


class MiniError(DeployError):
    pass


def verify_green(sha: str, get: Callable[[str], dict] = production.api) -> dict:
    """`sha` is a commit of main whose main push run passed `CI gate` and `checks / verify` (the rule of production.py)."""
    if not SHA.fullmatch(sha):
        raise Refused('an exact lowercase 40-character commit SHA is required')
    try:
        comparison = get('compare/main...' + sha)
        if (comparison.get('status') not in ('identical', 'behind') or comparison.get('ahead_by') != 0
                or comparison.get('merge_base_commit', {}).get('sha') != sha):
            raise Refused('the commit is not on main')
        latest = production.latest_run(sha, 'push', get)
        if (latest is None or latest.get('head_sha') != sha or latest.get('head_branch') != 'main'
                or latest.get('event') != 'push' or latest.get('status') != 'completed'
                or latest.get('conclusion') != 'success'
                or latest.get('head_repository', {}).get('full_name') != production.REPOSITORY):
            raise Refused('main has no completed successful CI push run for this exact commit')
        jobs = production.attempt_jobs(latest, get)
        if not production.job_concluded(jobs, 'CI gate'):
            raise Refused('CI gate did not pass for this commit')
        if production.job_concluded(jobs, 'checks / verify', 'skipped'):
            production.verify(sha, get)  # the push run reused the queue's verification: promotion's full check
        elif not production.job_concluded(jobs, 'checks / verify'):
            raise Refused('checks / verify did not pass for this commit')
        return {'sha': sha, 'run_id': latest['id'], 'url': latest.get('html_url')}
    except production.Refused as exc:
        raise Refused(str(exc)) from None
    except (OSError, ValueError, KeyError) as exc:
        raise Refused(f'CI state could not be read ({type(exc).__name__}); nothing is deployed') from None


def git_archive(sha: str) -> bytes:
    done = subprocess.run(['git', '-C', str(ROOT), 'archive', '--format=tar', sha, 'services/inference'],
                          capture_output=True, timeout=300)
    if done.returncode:
        raise DeployError('git archive failed (is the commit fetched? git fetch origin main): '
                          + done.stderr.decode(errors='replace')[-200:])
    return done.stdout


class SshTransport:
    """ssh to the mini as the admin account. `run` returns (exit code, output); `hold` keeps a command alive."""

    def __init__(self, host: str) -> None:
        if not host or host.startswith('-') or re.search(r'\s', host):
            raise DeployError('bad mini address')
        self.base = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '--', host]

    def run(self, command: str, stdin: bytes | None = None, timeout: float | None = 120) -> tuple[int, str]:
        try:
            done = subprocess.run(self.base + [command], input=stdin if stdin is not None else b'', capture_output=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            raise MiniError(f'the mini did not answer within {timeout} s') from None
        out = done.stdout.decode(errors='replace')
        return done.returncode, out if done.returncode == 0 else out + done.stderr.decode(errors='replace')

    @contextlib.contextmanager
    def hold(self, command: str) -> Generator[None]:
        process = subprocess.Popen(self.base + [command], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            line = process.stdout.readline().strip() if process.stdout else ''
            if line == 'busy':
                raise Busy('another deploy holds the lock on the mini')
            if line != 'held':
                raise MiniError('the lock holder did not start on the mini')
            yield
        finally:
            if process.stdin:
                with contextlib.suppress(OSError):
                    process.stdin.close()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def sudo(*words: str) -> str:
    return ' '.join(['sudo -n -H -u', USER, *words])


class SshMini:
    """The operations of a deploy, each one ssh command. Paths: releases/, current, previous, probe.key only."""

    def __init__(self, transport, home: str = HOME) -> None:
        self.transport, self.home = transport, home

    def _run(self, command: str, **options) -> tuple[int, str]:
        code, out = self.transport.run(command, **options)
        if code == 255:
            raise MiniError('ssh to the mini failed')
        return code, out

    def hold_lock(self):
        return self.transport.hold(sudo(PYTHON, '-I', '-c', shlex.quote(LOCK_HOLDER), f'{self.home}/releases/.deploy.lock'))

    def _link(self, name: str) -> str | None:
        code, out = self._run(sudo('readlink', f'{self.home}/{name}'))
        target = os.path.basename(out.strip()) if code == 0 else ''
        return target if SHA.fullmatch(target) else None

    def current_release(self) -> str | None:
        return self._link('current')

    def previous_release(self) -> str | None:
        return self._link('previous')

    def has_release(self, sha: str) -> bool:
        return self._run(sudo('test', '-f', f'{self.home}/releases/{sha}/.ready'))[0] == 0

    def upload(self, sha: str, archive: bytes) -> None:
        directory = f'{self.home}/releases/{sha}'
        script = (f'set -e ; d={directory} ; mkdir -p "$d" ; tar -x -f - --strip-components=2 -C "$d" ; '
                  f'printf %s {sha} > "$d/RELEASE"')
        code, out = self._run(sudo('sh', '-c', shlex.quote(script)), stdin=archive, timeout=600)
        if code:
            raise MiniError('unpacking the release failed: ' + out[-300:])

    def sync(self, sha: str) -> None:
        directory = f'{self.home}/releases/{sha}'
        wheel_only = f'{PYTHON} -I -c {shlex.quote(WHEEL_ONLY)} pyproject.toml > .wheel-only.toml'
        script = (f'cd {directory} && {wheel_only} && '
                  f'{UV} sync --locked --group engines --python {PYTHON} --config-file .wheel-only.toml && '
                  f'cd {directory}/docling && {wheel_only} && '
                  f'{UV} sync --locked --compile-bytecode --python {PYTHON} --config-file .wheel-only.toml && '
                  f'touch {directory}/.ready')
        # An empty environment and no uv configuration file: uv runs unsandboxed as the service user, and every file in
        # that user's home (uv.toml, a proxy setting) is something the sandboxed service could once have written.
        command = sudo('env', '-i', f'HOME={self.home}', 'PATH=/opt/homebrew/bin:/usr/bin:/bin',
                       f'UV_CACHE_DIR={self.home}/releases/.uv-cache', 'UV_PYTHON_DOWNLOADS=never', 'UV_NO_CONFIG=1',
                       'sh', '-c', shlex.quote(script))
        code, out = self._run(command, timeout=1800)
        if code:
            raise MiniError('uv sync failed: ' + out[-400:])

    def service_pid(self) -> int | None:
        code, out = self._run(f'sudo -n launchctl print system/{LABEL}')
        if code and 'Could not find service' in out:
            return None  # not loaded: stopped for good (after a bootout) or never installed (start() says so)
        if code:
            raise MiniError('launchctl print failed: ' + out[-200:])
        found = re.search(r'^\s*pid = (\d+)', out, re.MULTILINE)
        return int(found.group(1)) if found else None

    def _signal(self, name: str) -> None:
        code, out = self._run(f'sudo -n launchctl kill {name} system/{LABEL}')
        if code and 'No such process' not in out:
            raise MiniError(f'launchctl kill {name} failed: ' + out[-200:])

    def term(self) -> None:
        self._signal('SIGTERM')

    def kill(self) -> None:
        self._signal('SIGKILL')

    def service_loaded(self) -> bool:
        code, out = self._run(f'sudo -n launchctl print system/{LABEL}')
        if code == 0:
            return True
        if 'Could not find service' in out:
            return False
        raise MiniError('launchctl print failed: ' + out[-200:])

    def unload(self) -> None:
        """Unload the job: launchd can no longer restart it. bootout sends SIGTERM and --wait returns when the removal is
        done, so a restarted old release that took work meanwhile drains too; it kills only after ExitTimeOut, above the
        service's own limit. The caller still checks that the process is gone."""
        code, out = self._run(f'sudo -n launchctl bootout --wait system/{LABEL}', timeout=UNLOAD_TIMEOUT)
        if code and 'Could not find service' not in out and 'No such process' not in out:
            raise MiniError('launchctl bootout failed: ' + out[-200:])

    def _recover_command(self, sha: str, *flags: str) -> str:
        return sudo('env', '-i', f'HOME={self.home}', 'PATH=/usr/bin:/bin', f'{self.home}/releases/{sha}/.venv/bin/deedkit-inference',
                    'recover', '--state-dir', f'{self.home}/state', *flags)

    def recover_capable(self, sha: str) -> bool:
        """True when that release has `recover` and can read the ledger and the process table (exit 0 of --check). A
        release without the command answers with a usage error (2) or is not runnable: False. Exit 4 is an operational
        failure of a release that has it (ledger unreadable, `ps` failing): MiniError, never read as "no command"."""
        code, out = self._run(self._recover_command(sha, '--check'), timeout=60)
        if code == 4:
            raise MiniError('recover --check failed: ' + out[-300:])
        return code == 0

    def ledger_exists(self) -> bool:
        script = f'if test -e {self.home}/state/paused.json ; then echo present ; else echo absent ; fi'
        code, out = self._run(sudo('sh', '-c', shlex.quote(script)))
        if code or out.strip() not in ('present', 'absent'):
            raise MiniError('could not look for the pause ledger: ' + out[-200:])
        return out.strip() == 'present'

    def recover(self, sha: str) -> None:
        """Continue and kill the engine groups a stopped service left paused (ANYM-691); fails when one remains."""
        code, out = self._run(self._recover_command(sha), timeout=120)
        if code:
            raise MiniError('recover failed: ' + out[-300:])

    def point(self, sha: str) -> None:
        h = self.home
        script = (f'set -e ; test -f {h}/releases/{sha}/.ready ; '
                  f'if [ -L {h}/current ] && [ "$(readlink {h}/current)" != releases/{sha} ] ; then '
                  f'ln -sfn "$(readlink {h}/current)" {h}/previous ; fi ; '
                  f'ln -sfn releases/{sha} {h}/current.new ; mv -f -h {h}/current.new {h}/current')
        code, out = self._run(sudo('sh', '-c', shlex.quote(script)))
        if code:
            raise MiniError('switching current failed: ' + out[-300:])

    def start(self) -> None:
        code, out = self._run(f'sudo -n launchctl bootstrap system {PLIST}')  # loads the job (RunAtLoad starts it)
        if code:
            raise MiniError('launchctl bootstrap failed (is the setup runbook done?): ' + out[-200:])
        code, out = self._run(f'sudo -n launchctl kickstart system/{LABEL}')  # no -k: never kill a running job
        if code and self.service_pid() is None:
            raise MiniError('launchctl kickstart failed: ' + out[-200:])

    def _json(self, code: int, out: str, what: str) -> dict:
        try:
            value = json.loads(out)
        except ValueError:
            value = None
        if code or not isinstance(value, dict):
            raise MiniError(f'{what} did not answer with a JSON object')
        return value

    def health(self) -> dict:
        code, out = self._run(f'/usr/bin/curl -sS --max-time 5 http://127.0.0.1:{PORT}/health', timeout=20)
        return self._json(code, out, '/health')

    def models(self) -> dict:
        code, out = self._run(sudo(PYTHON, '-I', '-', f'{self.home}/probe.key'), stdin=MODELS_SCRIPT.encode(), timeout=30)
        return self._json(code, out, '/v1/models')


class Deploy:
    def __init__(self, mini, *, verifier: Callable[[str], dict] = verify_green,
                 archive: Callable[[str], bytes] = git_archive, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, out: Callable[[str], None] = print,
                 drain_limit: float = DRAIN_LIMIT, force: bool = False, start_wait: float = START_WAIT) -> None:
        self.mini, self.verifier, self.archive, self.sleep, self.clock = mini, verifier, archive, sleep, clock
        self.out, self.drain_limit, self.force, self.start_wait = out, drain_limit, force, start_wait
        self.was_running = True

    def deploy(self, sha: str) -> None:
        self.verifier(sha)
        with self.mini.hold_lock():
            if self.mini.has_release(sha):
                self.out(f'release {sha[:12]} is already on the mini')
            else:
                self.out(f'unpacking and syncing {sha[:12]} while the running release keeps serving')
                self.mini.upload(sha, self.archive(sha))
                self.mini.sync(sha)
            self.recovery_release(sha)
            self.drain()
            self.recover(sha)
            self.mini.point(sha)
            self.mini.start()
            self.read_back(sha)

    def stage(self, sha: str) -> None:
        """First install: unpack, sync and point `current` at the release. The service is not touched (it is not loaded yet)."""
        self.verifier(sha)
        with self.mini.hold_lock():
            if self.mini.current_release() is not None:
                raise DeployError('`current` already points at a release; use a normal deploy')
            self.out(f'unpacking and syncing {sha[:12]}; the service is not started')
            self.mini.upload(sha, self.archive(sha))
            self.mini.sync(sha)
            self.mini.point(sha)

    def rollback(self) -> None:
        with self.mini.hold_lock():
            previous = self.mini.previous_release()
            if previous is None or not self.mini.has_release(previous):
                raise DeployError('there is no previous release to roll back to')
            self.recovery_release(previous)
            self.drain()
            self.recover(previous)
            self.mini.point(previous)
            self.mini.start()
            self.read_back(previous)

    def recovery_release(self, target: str) -> str | None:
        """The release whose `recover` runs for this switch, or None when no release on the mini has the command. The
        release being left created any ledger, so it goes first; then the target. A release without the command (the one
        deployed before the feature, or a rollback to it) is skipped; when none has it no release could pause anything, so
        a ledger cannot exist, and one that does exist fails the deploy closed. Read-only: it runs before the drain too,
        so a ledger nobody can read stops the deploy while the service is still serving."""
        leaving = self.mini.current_release()
        for release in dict.fromkeys(r for r in (leaving, target) if r):
            try:
                capable = self.mini.recover_capable(release)
            except MiniError as exc:
                raise DeployError(f'{exc}; nothing was switched or stopped. Read the runbook section 14 (Recover), then run '
                                  'the same command again') from None
            if capable:
                return release
        if self.mini.ledger_exists():
            raise DeployError('a pause ledger (state/paused.json) exists and no release on the mini can read it; nothing was '
                              'switched or stopped. Read the runbook section 14 (Recover), then run the same command again')
        return None

    def recover(self, target: str) -> None:
        """After the job is unloaded and before `current` moves: continue and kill engine groups the service left stopped
        (ANYM-691 criterion 5). A failure never promotes the target: the unchanged release is started again (its own start
        runs the same recovery) when the service was running before, so a failed recovery does not strand the service."""
        try:
            release = self.recovery_release(target)
            if release is None:
                self.out('no release on the mini can pause engines and no pause ledger exists: recovery skipped')
                return
            self.out(f'recovering stopped engine groups with release {release[:12]}')
            self.mini.recover(release)
        except MiniError as exc:  # MiniError is a DeployError: first
            raise DeployError(f'{exc}; nothing was switched. {self.restore()}') from None
        except DeployError as exc:  # the preflight's own refusals, repeated after the unload
            raise DeployError(f'{exc} {self.restore()}') from None

    def restore(self) -> str:
        """Start the unchanged `current` release again after a failure that came after the unload."""
        leaving = self.mini.current_release()
        if not self.was_running or leaving is None:
            return 'The service was not running before, so it was not started.'
        try:
            self.mini.start()
            self.read_back(leaving)
        except (MiniError, DeployError) as exc:
            return f'Starting the unchanged release {leaving[:12]} again failed too ({exc}): start it by hand (runbook, Recovery).'
        return f'The unchanged release {leaving[:12]} is serving again; fix the cause and run the same command again.'

    def drain(self) -> None:
        pid = self.mini.service_pid()
        self.was_running = pid is not None
        if pid is None:
            self.out('the service is not running')
            self.unload()
            return
        watched, deadline, killed, restarts = None, 0.0, False, -1
        while pid is not None:
            if pid != watched:
                # a restarted old release (launchd KeepAlive after exit 3 or a kill) may have taken work: it drains under
                # the same limit as the first process
                restarts += 1
                if restarts > MAX_RESTARTS:
                    raise DrainTimeout('launchd keeps restarting the old release; nothing was switched')
                self.out(f'SIGTERM to the service (pid {pid}): admission closes, queued and running jobs finish')
                self.mini.term()
                watched, killed = pid, False
                deadline = self.clock() + self.drain_limit
            elif self.clock() >= deadline:
                if not self.force or killed:
                    raise DrainTimeout(f'the service is still draining after {self.drain_limit:.0f} s; nothing was killed, '
                                       'nothing was switched. Run again later, or with --force to kill it.')
                self.out('drain limit passed: --force, SIGKILL')
                self.mini.kill()
                killed = True
            self.sleep(POLL)
            pid = self.mini.service_pid()
        self.out('the service has stopped')
        self.unload()

    def unload(self) -> None:
        # A stable stopped state: after exit 3 launchd restarts the old release (KeepAlive {SuccessfulExit: false}), and
        # that restart can happen between our last look and the switch. Unloading the job ends restarts for good and
        # drains anything a restarted release accepted meanwhile.
        self.out('unloading the job (no restart possible; a restarted release drains)')
        self.mini.unload()
        deadline = self.clock() + UNLOAD_TIMEOUT
        while self.mini.service_loaded() or self.mini.service_pid() is not None:
            if self.clock() >= deadline:
                raise DeployError('the job or its process is still there after bootout; nothing was switched')
            self.sleep(POLL)

    def read_back(self, expected: str) -> None:
        deadline = self.clock() + self.start_wait
        while True:
            try:
                health = self.mini.health()
                break
            except MiniError as exc:
                if self.clock() >= deadline:
                    raise DeployError(f'/health did not answer after the start ({exc}). The previous release is kept: '
                                      'run scripts/inference_deploy.py --rollback') from None
                self.sleep(2)
        try:
            models = self.mini.models()
        except MiniError as exc:
            raise DeployError(f'/v1/models read-back failed ({exc}); run scripts/inference_deploy.py --rollback') from None
        queue = health.get('queue') or {}
        release = models.get('release')
        self.out(f'read-back: release={release} status={health.get("status")} admission={health.get("admission")} '
                 f'queue={queue.get("depth")}/{queue.get("active")}/{queue.get("capacity")}')
        if release != expected:
            raise DeployError(f'the running release is {release}, not the deployed {expected}; '
                              'run scripts/inference_deploy.py --rollback')


def mini_host(option: str | None) -> str:
    host = option or os.environ.get('DEEDKIT_REMOTE_HOST', '')
    if not host:
        try:
            host = (Path.home() / '.config' / 'deedkit' / 'mini-host').read_text().splitlines()[0].strip()
        except (OSError, IndexError):
            host = ''
    return host


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or '').split('\n')[0])
    parser.add_argument('sha', nargs='?')
    parser.add_argument('--rollback', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--stage-only', action='store_true')
    parser.add_argument('--host')
    args = parser.parse_args(argv)
    if bool(args.sha) == args.rollback or (args.stage_only and not args.sha):
        parser.error('give a commit SHA (with --stage-only for the first install), or --rollback')
    try:
        mini = SshMini(SshTransport(mini_host(args.host)))
        runner = Deploy(mini, force=args.force)
        if args.rollback:
            runner.rollback()
        elif args.stage_only:
            runner.stage(args.sha)
        else:
            runner.deploy(args.sha)
    except Busy as exc:
        print(f'busy: {exc}', file=sys.stderr)
        return 2
    except DeployError as exc:
        print(f'{type(exc).__name__.lower()}: {exc}', file=sys.stderr)
        return 1
    print('done')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
