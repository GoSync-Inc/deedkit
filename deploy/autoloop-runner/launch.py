#!/usr/bin/env python3
"""The autoloop runner's launcher (ANYM-574, docs/operations/feedback-autoloop.md, Container runner).

    launch.py build INTENT CARD   # the build worker's Superset terminal runs this (scripts/autoloop.py build_start)
    launch.py check               # the isolation check and one model call: the subscription login works in a container

It runs on the owner's Mac. It builds the pinned image (tagged by the hash of this directory), keeps an internal
Docker network whose only way out is the allowlist proxy, and starts one container per build with a fresh clone and
no mount of ~/.ssh, an SSH agent socket, ~/.deedkit or the orchestrator directory. The existing logins go in as
environment values only: the gh token, the Claude subscription's current access token (never its refresh token) and
the Tiden token; no new key is made. Their values never appear on a command line: `docker run -e NAME` reads them
from the launcher's own environment. One mount: a read-only control directory (the two brief files and the gate
file, rewritten from the host state every 30 s; a stop there, the build time limit or a closed terminal stops the
container). The outbox stays inside the container: after it ends the launcher copies out at most one small regular
file, keeps its `finish`, `decline` and `large` requests for this intent in a pending file, removes the container and
applies them on the host, retrying while the tick holds the lock. What it cannot apply stays pending:

    launch.py apply INTENT        # apply what a build left pending (or collect it from a stopped container first)
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import io
import tarfile
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import autoloop  # noqa: E402

FILES = ('Dockerfile', 'entrypoint.sh', 'proxy.py', 'selfcheck.py')
IMAGE = 'deedkit-autoloop-runner'
NETWORK, PROXY = 'deedkit_autoloop', 'deedkit_autoloop_proxy'
PROXY_URL = f'http://{PROXY}:3128'
RUNS = Path.home() / '.cache' / 'deedkit-autoloop-runner'
REPO = 'GoSync-Inc/deedkit'
KEYCHAIN = 'Claude Code-credentials'
TIDEN = Path.home() / '.tiden' / 'config.json'
SECRETS = ('GH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', 'TIDEN_API_TOKEN')
# Every variable the launcher passes; selfcheck.ENV holds them all (a test keeps them equal).
ENV = (*SECRETS, 'TIDEN_BASE_URL', 'TIDEN_WORKSPACE_ID', 'GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME',
       'GIT_COMMITTER_EMAIL', 'HTTPS_PROXY', 'HTTP_PROXY', 'https_proxy', 'http_proxy', 'NO_PROXY', 'no_proxy',
       'AUTOLOOP_RUNNER', 'AUTOLOOP_INTENT', 'AUTOLOOP_CARD', 'AUTOLOOP_PROMPT', 'AUTOLOOP_MODEL', 'AUTOLOOP_EFFORT',
       'AUTOLOOP_PROD_HOST', 'AUTOLOOP_REPO', 'AUTOLOOP_MODE')
MOUNT = '/run/autoloop'
POLL, STOP_SECONDS, CALL = 30, 20, 120
MAX_REQUESTS, MAX_LINE = 10, 512
MAX_OUTBOX = MAX_REQUESTS * (MAX_LINE + 1)  # bytes of the outbox file the host reads
OUTBOX = f'{MOUNT}/outbox/requests.jsonl'
ATTEMPTS, RETRY = 40, 30  # applying a request while a tick holds the lock: up to 20 minutes
BUILD = re.compile(r'autoloop-build-[a-z0-9][a-z0-9-]{2,60}')
CARD = re.compile(r'ANYM-[1-9][0-9]*')


class Refused(Exception):
    pass


def tag():
    digest = hashlib.sha256()
    for name in FILES:
        digest.update(name.encode() + b'\0' + (HERE / name).read_bytes() + b'\0')
    return f'{IMAGE}:{digest.hexdigest()[:16]}'


def call(argv, env=None, timeout=CALL):
    """One command whose stdout is returned; a failure is refused with its first stderr line, never a secret."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refused(f'{argv[0]} {argv[1] if len(argv) > 1 else ""}: {type(error).__name__}') from None
    if done.returncode:
        raise Refused(f'{" ".join(argv[:2])}: exit {done.returncode}: {done.stderr.strip()[:200]}')
    return done.stdout


def image(docker=call):
    name = tag()
    try:
        docker(['docker', 'image', 'inspect', name])
    except Refused:
        docker(['docker', 'build', '--tag', name, str(HERE)], timeout=1800)
    return name


def proxy_argv(name):
    return ['docker', 'run', '--detach', '--rm', '--name', PROXY, '--network', 'bridge', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--read-only', '--memory', '256m', '--pids-limit', '256',
            '--entrypoint', 'python3', name, '/opt/runner/proxy.py']


def network(name, docker=call):
    """The internal network (no route out) and the proxy, the one container on both it and the default bridge."""
    try:
        docker(['docker', 'network', 'inspect', NETWORK])
    except Refused:
        try:
            docker(['docker', 'network', 'create', '--internal', NETWORK])
        except Refused:
            pass  # a second launcher created it meanwhile: the inspect below decides
    if json.loads(docker(['docker', 'network', 'inspect', NETWORK]))[0].get('Internal') is not True:
        raise Refused(f'network {NETWORK} is not internal')
    if proxy_image(docker) == name:
        return
    if proxy_image(docker):  # an older image's proxy: replaced, after `--rm` has removed it
        docker(['docker', 'stop', PROXY])
        for _ in range(30):
            if not proxy_image(docker):
                break
            time.sleep(1)
    try:
        docker(proxy_argv(name))
        docker(['docker', 'network', 'connect', NETWORK, PROXY])
    except Refused:
        if proxy_image(docker) != name:  # not a second launcher's start either
            raise


def proxy_image(docker=call):
    try:
        return docker(['docker', 'inspect', '--format', '{{.Config.Image}}', PROXY]).strip() or None
    except Refused:
        return None


def access_token(raw, now, minimum):
    """The subscription's current access token from the Keychain item; its refresh token never leaves the host."""
    try:
        oauth = json.loads(raw)['claudeAiOauth']
        token, expires = oauth['accessToken'], int(oauth['expiresAt']) / 1000
    except (ValueError, KeyError, TypeError):
        raise Refused('the Claude login in the Keychain is not readable') from None
    if not isinstance(token, str) or not token or expires - now.timestamp() < minimum:
        raise Refused(f'the Claude access token expires in under {minimum // 60} minutes: run the agent CLI once on '
                      'the host to refresh it')
    return token


def tiden(path=TIDEN):
    try:
        found = json.loads(path.read_text())['workspaces']
    except (OSError, ValueError, KeyError, TypeError):
        raise Refused('the Tiden login is not readable') from None
    if not isinstance(found, dict) or len(found) != 1:
        raise Refused('the Tiden login holds no single workspace')
    (workspace, entry), = found.items()
    return {'TIDEN_API_TOKEN': entry['apiToken'], 'TIDEN_BASE_URL': entry['baseUrl'], 'TIDEN_WORKSPACE_ID': workspace}


def production_host(run=call):
    found = re.search(r'^hostname (\S+)$', run(['ssh', '-G', autoloop.triage.HOST]), re.M)
    if not found:
        raise Refused('no production host name')
    return found[1]


def logins(now, minimum, run=call):
    """The existing logins as environment values (no new key): gh, the Claude subscription, Tiden, the git name."""
    env = {'GH_TOKEN': run(['gh', 'auth', 'token']).strip(),
           'CLAUDE_CODE_OAUTH_TOKEN': access_token(
               run(['security', 'find-generic-password', '-s', KEYCHAIN, '-w']), now, minimum), **tiden()}
    for role in ('AUTHOR', 'COMMITTER'):
        env[f'GIT_{role}_NAME'] = run(['git', 'config', '--global', 'user.name']).strip()
        env[f'GIT_{role}_EMAIL'] = run(['git', 'config', '--global', 'user.email']).strip()
    if not all(env.values()):
        raise Refused('a login is empty')
    return env


def prompt(intent, card):
    return (f'Read and follow {MOUNT}/control/common.md and {MOUNT}/control/autoloop-build.md. '
            f'Intent: {intent}. Card: {card}. In this container there is no Codex: run each review round as an Opus '
            'subagent; the controller\'s Astra review on the host is the review of record.')


def run_argv(name, image_name, env_names, control=None, tty=False, remove=True):
    """`docker run` for one runner container: the internal network, no capabilities, one read-only mount at most. A
    build keeps its container after it ends (`remove=False`) until the launcher has copied the outbox out."""
    argv = ['docker', 'run', *(['--rm'] if remove else []), '--interactive', *(['--tty'] if tty else []), '--name', name,
            '--network', NETWORK, '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--init',
            '--memory', '8g', '--pids-limit', '4096', '--label', 'deedkit.autoloop=runner']
    if control:
        argv += ['--mount', f'type=bind,source={control},target={MOUNT}/control,readonly']
    for env_name in env_names:
        argv += ['--env', env_name]
    return [*argv, image_name]


def settings(intent=None, card=None, host='', mode='build'):
    out = {'HTTPS_PROXY': PROXY_URL, 'HTTP_PROXY': PROXY_URL, 'https_proxy': PROXY_URL, 'http_proxy': PROXY_URL,
           'NO_PROXY': '', 'no_proxy': '', 'AUTOLOOP_MODEL': autoloop.MODEL, 'AUTOLOOP_PROD_HOST': host,
           'AUTOLOOP_REPO': REPO, 'AUTOLOOP_MODE': mode}
    if mode == 'build':
        out.update(AUTOLOOP_RUNNER=MOUNT, AUTOLOOP_INTENT=intent, AUTOLOOP_CARD=card, AUTOLOOP_PROMPT=prompt(intent, card),
                   AUTOLOOP_EFFORT=autoloop.EFFORT)
    return out


def client_env(values):
    """The docker CLI's environment: its own lookup paths plus the values `--env NAME` passes on."""
    base = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'USER', 'DOCKER_HOST', 'DOCKER_CONTEXT',
                                                          'DOCKER_CONFIG', 'TMPDIR', 'LANG')}
    return {**base, **values}


def prepare(intent, runs=RUNS, briefs=autoloop.BRIEFS):
    """The run directory: control/ (the two briefs and the gate file, read-only in the container)."""
    run = runs / intent
    control = run / 'control'
    for path in (runs, run, control):
        path.mkdir(mode=0o700, exist_ok=True)
    for name in ('common.md', 'autoloop-build.md'):
        shutil.copyfile(briefs / name, control / name)
    return control


def write_gate(control, why):
    temp = control / 'gate.tmp'
    temp.write_text(f'{why or "go"}\n')
    temp.replace(control / 'gate')


def outbox_text(archive):
    """The outbox file out of `docker cp`'s tar stream: one regular file of at most MAX_OUTBOX bytes, else nothing
    (a link, a FIFO, a directory or an oversized file is the container's, never followed or read whole)."""
    try:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            members = tar.getmembers()
            if len(members) != 1 or not members[0].isreg() or members[0].size > MAX_OUTBOX:
                return ''
            data = tar.extractfile(members[0])
            return data.read(MAX_OUTBOX).decode('utf-8', 'replace') if data else ''
    except (tarfile.TarError, OSError, EOFError):
        return ''


def collect(name, popen=subprocess.Popen):
    """The outbox of a stopped container, read through `docker cp` with a byte bound before any parsing."""
    try:
        with popen(['docker', 'cp', f'{name}:{OUTBOX}', '-'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                   stdin=subprocess.DEVNULL) as copy:
            archive = copy.stdout.read(MAX_OUTBOX + 4096) if copy.stdout else b''
            copy.kill()
    except OSError:
        return ''
    return outbox_text(archive)


def requests(text, intent):
    """The outbox requests the host applies: this intent only, known actions and reasons, at most ten lines."""
    out = []
    for line in text.splitlines()[:MAX_REQUESTS]:
        try:
            item = json.loads(line[:MAX_LINE])
        except ValueError:
            continue
        if not isinstance(item, dict) or item.get('intent') != intent or item.get('action') not in autoloop.RUNNER_ACTIONS:
            continue
        reason = item.get('reason')
        if not (reason is None or isinstance(reason, str)) or (item['action'] == 'decline') != (reason in autoloop.DECLINED):
            continue
        step = [item['action'], intent, *(['--reason', reason] if item['action'] == 'decline' else [])]
        if step not in out:
            out.append(step)
    # An end (`decline` or `large`) before `finish`, as the brief orders them.
    return sorted(out, key=lambda step: step[0] == 'finish')


def apply(pending, main=autoloop.main, sleep=time.sleep):
    """Apply the pending steps in order, each retried while it fails (a tick holding the lock answers BUSY and writes
    nothing); a step still failing and the ones after it stay in the pending file."""
    steps = json.loads(pending.read_text()) if pending.exists() else []
    codes = []
    while steps:
        code = 1
        for attempt in range(ATTEMPTS):
            code = main(steps[0])
            if code == 0:
                break
            if attempt + 1 < ATTEMPTS:
                sleep(RETRY)
        codes.append((steps[0][0], code))
        if code:
            break
        steps = steps[1:]
        temp = pending.with_suffix('.tmp')
        temp.write_text(json.dumps(steps))
        temp.replace(pending)
    if not steps:
        pending.unlink(missing_ok=True)
    return codes


def exists(name, docker=call):
    try:
        docker(['docker', 'inspect', '--format', '{{.State.Running}}', name])
        return True
    except Refused:
        return False


def gather(intent, runs=RUNS, docker=call, copy=collect):
    """A stopped build container's requests into the run's pending file, then the container removed."""
    pending = runs / intent / 'pending.json'
    if exists(intent, docker):
        steps = requests(copy(intent), intent)
        known = json.loads(pending.read_text()) if pending.exists() else []
        temp = pending.with_suffix('.tmp')
        temp.write_text(json.dumps(known + [s for s in steps if s not in known]))
        temp.replace(pending)
        docker(['docker', 'rm', intent])
    return pending


def report(codes, pending):
    for action, result in codes:
        print(f'runner: {action} applied on the host: exit {result}', file=sys.stderr)
    if pending.exists():
        print(f'runner: PENDING {pending.read_text()}: not applied; run `launch.py apply {pending.parent.name}`',
              file=sys.stderr, flush=True)
        return 1
    return 0


def watch(state, intent, control, name, done, deadline, docker=call, clock=time.monotonic):
    """Every POLL seconds: the host gate into the gate file; a stop (PAUSE, a held card, a dropped item, an ended
    intent), the time limit or any failure of this check stops the container."""
    try:
        while not done.wait(POLL):
            why = autoloop.gate(state, intent) or ('time limit' if clock() > deadline else None)
            write_gate(control, why)
            if why:
                print(f'runner: STOP {why}; stopping the container', file=sys.stderr, flush=True)
                stop(name, docker)
                return
    except Exception as error:  # a gate that cannot be checked is a stop
        print(f'runner: STOP gate check failed ({type(error).__name__}); stopping the container', file=sys.stderr,
              flush=True)
        stop(name, docker)


def stop(name, docker=call):
    """Stop the container, then kill it if it still runs; a container still running after both is said loudly."""
    for argv in (['docker', 'stop', '--time', str(STOP_SECONDS), name], ['docker', 'kill', name]):
        try:
            docker(argv, timeout=STOP_SECONDS + 30)
        except Refused:
            pass  # already stopped or gone: the inspect below decides
        try:
            if docker(['docker', 'inspect', '--format', '{{.State.Running}}', name]).strip() != 'true':
                return True
        except Refused:
            return True
    print(f'runner: FAILED to stop container {name}; stop it by hand: docker kill {name}', file=sys.stderr,
          flush=True)
    return False


def build(intent, card, state=autoloop.STATE, now=None):
    if not BUILD.fullmatch(intent) or not CARD.fullmatch(card):
        raise Refused('usage: launch.py build autoloop-build-<slug> ANYM-<n>')
    why = autoloop.gate(state, intent)
    if why:
        raise Refused(f'STOP {why}')
    if exists(intent) or (RUNS / intent / 'pending.json').exists():
        raise Refused(f'a container or pending requests of {intent} exist: run `launch.py apply {intent}` first')
    now = now or datetime.now(timezone.utc)
    control = prepare(intent)
    write_gate(control, None)
    name_ = image()
    network(name_)
    values = {**logins(now, autoloop.LIMITS['build']), **settings(intent, card, production_host())}
    argv = run_argv(intent, name_, sorted(values), control, sys.stdin.isatty(), remove=False)
    done = threading.Event()
    for number in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):  # a closed terminal (fence, hold) stops it too
        signal.signal(number, lambda *_: (done.set(), stop(intent)))
    deadline = time.monotonic() + autoloop.LIMITS['build']
    watcher = threading.Thread(target=watch, args=(state, intent, control, intent, done, deadline), daemon=True)
    watcher.start()
    try:
        code = subprocess.run(argv, env=client_env(values)).returncode
    finally:
        done.set()
    pending = gather(intent)
    return report(apply(pending), pending) or code


def resume(intent):
    if not BUILD.fullmatch(intent):
        raise Refused('usage: launch.py apply autoloop-build-<slug>')
    if exists(intent) and not stop(intent):
        raise Refused(f'container {intent} still runs')
    pending = gather(intent)
    return report(apply(pending), pending)


def check(now=None):
    """The card's first step: the isolation check and one model call in a container with the subscription login."""
    now = now or datetime.now(timezone.utc)
    name_ = image()
    network(name_)
    values = {**logins(now, 300), **settings(host=production_host(), mode='check')}
    done = subprocess.run(run_argv('deedkit_autoloop_check', name_, sorted(values)), env=client_env(values),
                          capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
    sys.stderr.write(done.stderr[-4000:])
    answer = done.stdout.strip().lower()
    print(f'check: exit {done.returncode}; model answer {answer[:40]!r}')
    return 0 if done.returncode == 0 and 'ok' in answer else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description='autoloop container runner launcher (ANYM-574)')
    parser.add_argument('action', choices=['build', 'check', 'apply'])
    parser.add_argument('intent', nargs='?')
    parser.add_argument('card', nargs='?')
    args = parser.parse_args(argv)
    if [bool(args.intent), bool(args.card)] != {'build': [True, True], 'apply': [True, False],
                                                'check': [False, False]}[args.action]:
        parser.error('build takes INTENT CARD; apply takes INTENT; check takes nothing')
    try:
        if args.action == 'apply':
            return resume(args.intent)
        return build(args.intent, args.card) if args.action == 'build' else check()
    except (Refused, autoloop.Refused) as error:
        print(f'runner: REFUSED {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
