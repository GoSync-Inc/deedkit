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
applies them on the host, retrying while the tick holds the lock, then ends the intent there (`autoloop.py end`: a
build that did not finish is interrupted and no longer holds a slot, a lane or paths). Every wait on a child process
has a bound. What it cannot apply stays pending:

    launch.py apply INTENT        # apply what a build left pending (or collect it from a stopped container first)
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import io
import select
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
PROXY_URL = f'http://{PROXY}:3128'  # the names' prefix: each image has its own proxy (proxy_name)
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
REAP = 5  # seconds a killed child gets to be reaped; past them it is left to the OS
IDLE = 3600  # another image's proxy, unused this long by a launcher and serving no container, is stopped
CHECK = 'deedkit_autoloop_check'
MAX_REQUESTS, MAX_LINE = 10, 512
MAX_OUTBOX = MAX_REQUESTS * (MAX_LINE + 1)  # bytes of the outbox file the host reads
OUTBOX = f'{MOUNT}/outbox/requests.jsonl'
ATTEMPTS, RETRY = 40, 30  # applying a request while a tick holds the lock: up to 20 minutes
LEASES = []  # this process's proxy marks, open with a shared lock until it exits
BUILD = re.compile(r'autoloop-build-[a-z0-9][a-z0-9-]{2,60}')
CARD = re.compile(r'ANYM-[1-9][0-9]*')


class Refused(Exception):
    pass


def tag():
    digest = hashlib.sha256()
    for name in FILES:
        digest.update(name.encode() + b'\0' + (HERE / name).read_bytes() + b'\0')
    return f'{IMAGE}:{digest.hexdigest()[:16]}'


def supervise(argv, env=None, timeout=CALL, capture=True):
    """(exit code, stdout, stderr) of one child, every wait on it bounded. `subprocess.run` and Popen's `with` wait
    on a killed child without a bound, so a child past its time is killed here and reaped for at most REAP seconds;
    the time-out is raised. capture=False: the child keeps the terminal (the attached build client)."""
    pipe = subprocess.PIPE if capture else None
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL if capture else None, stdout=pipe, stderr=pipe, text=True,
                             env=env)
    try:
        out, err = child.communicate(timeout=timeout)
        return child.returncode, out or '', err or ''
    except subprocess.TimeoutExpired:
        child.kill()
        try:
            child.communicate(timeout=REAP)
        except subprocess.TimeoutExpired:
            pass  # a grandchild holds the pipes: closed below, never waited on
        raise
    finally:
        reap(child)


def reap(child):
    """A child still running is killed and waited on for REAP seconds at most; its pipes are closed (no other thread
    reads them, so a close never waits on a lock)."""
    if child.poll() is None:
        child.kill()
        try:
            child.wait(REAP)
        except subprocess.TimeoutExpired:
            pass  # a child that outlives SIGKILL is left to the OS
    for stream in (child.stdout, child.stderr):
        if stream:
            stream.close()


def call(argv, env=None, timeout=CALL):
    """One command whose stdout is returned; a failure is refused with its first stderr line, never a secret."""
    try:
        code, out, err = supervise(argv, env, timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refused(f'{argv[0]} {argv[1] if len(argv) > 1 else ""}: {type(error).__name__}') from None
    if code:
        raise Refused(f'{" ".join(argv[:2])}: exit {code}: {err.strip()[:200]}')
    return out


def image(docker=call):
    name = tag()
    try:
        docker(['docker', 'image', 'inspect', name])
    except Refused:
        docker(['docker', 'build', '--tag', name, str(HERE)], timeout=1800)
    return name


def proxy_name(name):
    """The proxy of one image: a build of another image never loses its proxy when the runner changes."""
    return f"{PROXY}_{name.rpartition(':')[2]}"


def proxy_argv(name):
    return ['docker', 'run', '--detach', '--rm', '--name', proxy_name(name), '--network', 'bridge', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--read-only', '--memory', '256m', '--pids-limit', '256',
            '--log-opt', 'max-size=1m', '--log-opt', 'max-file=1', '--label', 'deedkit.autoloop=proxy',
            '--entrypoint', 'python3', name, '/opt/runner/proxy.py']


def network(name, docker=call, runs=None, clock=time.time):
    """The internal network (no route out) and this image's proxy, the one container on both it and the default
    bridge; its name is returned. Another image's proxy is stopped only when no launcher holds its mark, none has
    named it for IDLE seconds and no container uses it (each launcher leases its proxy's mark before it starts it)."""
    try:
        docker(['docker', 'network', 'inspect', NETWORK])
    except Refused:
        try:
            docker(['docker', 'network', 'create', '--internal', NETWORK])
        except Refused:
            pass  # a second launcher created it meanwhile: the inspect below decides
    if json.loads(docker(['docker', 'network', 'inspect', NETWORK]))[0].get('Internal') is not True:
        raise Refused(f'network {NETWORK} is not internal')
    runs = runs or RUNS
    proxy, marks = proxy_name(name), runs / 'proxies'
    for path in (runs, marks):
        path.mkdir(mode=0o700, exist_ok=True)
    lease(marks / proxy)
    if proxy_image(docker, proxy) != name:
        try:
            docker(proxy_argv(name))
            docker(['docker', 'network', 'connect', NETWORK, proxy])
        except Refused:
            if proxy_image(docker, proxy) != name:  # not a second launcher's start either
                raise
    retire(proxy, docker, marks, clock)
    return proxy


def lease(mark, wait=CALL):
    """A shared lock on a proxy's mark, held while this process lives, and the mark's time refreshed: retire() never
    stops a proxy a running launcher holds. A mark retire() removed meanwhile is opened again."""
    end = time.monotonic() + wait
    while True:
        fd = os.open(mark, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if os.path.samestat(os.fstat(fd), os.stat(mark)):
                os.utime(fd)
                LEASES.append(fd)
                return fd
        except (BlockingIOError, FileNotFoundError):
            pass  # a retire() holds it, or has just removed it
        os.close(fd)
        if time.monotonic() > end:
            raise Refused(f'the proxy mark {mark.name} stays locked')
        time.sleep(0.2)


def retire(mine, docker, marks, clock):
    """Stop the other images' idle proxies; one still in use, or one this cannot tell about, is kept. The checks and
    the stop run under an exclusive lock on the proxy's mark, so no launcher can lease it in between."""
    try:
        others = docker(['docker', 'ps', '--all', '--filter', 'label=deedkit.autoloop=proxy', '--format',
                         '{{.Names}}']).split()
    except Refused:
        return
    for other in others:
        if other == mine:
            continue
        mark = marks / other
        try:
            try:
                fd, made = os.open(mark, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600), True
            except FileExistsError:
                fd, made = os.open(mark, os.O_RDWR), False
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # refused while a launcher holds the mark
            if made:
                os.utime(fd, (0, 0))  # made here: no launcher has named it
            if clock() - os.fstat(fd).st_mtime < IDLE or docker(
                    ['docker', 'ps', '--all', '--quiet', '--filter', f'label=deedkit.autoloop.proxy={other}']).strip():
                continue
            docker(['docker', 'stop', other], timeout=STOP_SECONDS + 30)
            mark.unlink(missing_ok=True)
        except (Refused, OSError):
            continue
        finally:
            os.close(fd)


def proxy_image(docker=call, proxy=PROXY):
    try:
        return docker(['docker', 'inspect', '--format', '{{.Config.Image}}', proxy]).strip() or None
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


def run_argv(name, image_name, env_names, control=None, tty=False, remove=True, proxy=PROXY):
    """`docker run` for one runner container: the internal network, no capabilities, one read-only mount at most. A
    build keeps its container after it ends (`remove=False`) until the launcher has copied the outbox out. Its proxy
    label keeps that proxy running while the container exists."""
    argv = ['docker', 'run', *(['--rm'] if remove else []), '--interactive', *(['--tty'] if tty else []), '--name', name,
            '--network', NETWORK, '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--init',
            '--memory', '8g', '--pids-limit', '4096', '--label', 'deedkit.autoloop=runner',
            '--label', f'deedkit.autoloop.proxy={proxy}', '--log-driver', 'none']
    if control:
        argv += ['--mount', f'type=bind,source={control},target={MOUNT}/control,readonly']
    for env_name in env_names:
        argv += ['--env', env_name]
    return [*argv, image_name]


def settings(intent=None, card=None, host='', mode='build', proxy=PROXY):
    url = f'http://{proxy}:3128'
    out = {'HTTPS_PROXY': url, 'HTTP_PROXY': url, 'https_proxy': url, 'http_proxy': url, 'NO_PROXY': '', 'no_proxy': '', 'AUTOLOOP_MODEL': autoloop.MODEL, 'AUTOLOOP_PROD_HOST': host,
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


def collect(name, popen=subprocess.Popen, timeout=CALL):
    """The outbox of a stopped container, read through `docker cp` with a byte and a time bound before any parsing.
    The entrypoint makes the file, so a failed or unfinished copy is None (the container is kept), while an
    oversized or malformed one is rejected content ('')."""
    limit = MAX_OUTBOX + 16384  # the tar headers and padding around one small file
    try:
        copy = popen(['docker', 'cp', f'{name}:{OUTBOX}', '-'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL)
    except OSError:
        return None
    try:  # no `with`: its exit waits on the child without a bound; every wait here has one
        got = bounded_read(copy.stdout, limit, time.monotonic() + timeout)
        if got is None:
            return None  # no answer in time
        if len(got) >= limit:
            return ''  # more than an outbox may hold: rejected
        try:
            code = copy.wait(timeout)
        except subprocess.TimeoutExpired:
            return None
    finally:
        reap(copy)  # the pipe is read in this thread only, so its close never waits
    return outbox_text(got) if code == 0 else None


def bounded_read(stream, limit, deadline):
    """Up to `limit` bytes of a pipe until its end, read without blocking; None when the deadline comes first (a
    writer that keeps the pipe open never holds the reader)."""
    fd, chunks, size = stream.fileno(), [], 0
    os.set_blocking(fd, False)
    while size < limit:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        if not select.select([fd], [], [], left)[0]:
            continue
        try:
            chunk = os.read(fd, limit - size)
        except BlockingIOError:
            continue
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b''.join(chunks)


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


def running(name, docker=call):
    """True or False for a container that exists, None for one that does not; any other inspect failure (the Docker
    daemon down, a time-out) is not an answer and is refused."""
    try:
        return docker(['docker', 'inspect', '--format', '{{.State.Running}}', name]).strip() == 'true'
    except Refused as error:
        if 'no such' in str(error).lower():  # `docker inspect` says `no such object`, the daemon `No such container`
            return None
        raise


def exists(name, docker=call):
    return running(name, docker) is not None


def gather(intent, runs=RUNS, docker=call, copy=collect):
    """A stopped build container's requests into the run's pending file, then the container removed."""
    pending = runs / intent / 'pending.json'
    if exists(intent, docker):
        text = copy(intent)
        if text is None:
            raise Refused(f'the outbox of {intent} could not be copied; the container is kept: run '
                          f'`launch.py apply {intent}` again')
        steps = requests(text, intent)
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


def watch(state, intent, control, name, done, deadline, docker=call, clock=time.monotonic, cancelled=None):
    """Every POLL seconds: the host gate into the gate file; a stop (PAUSE, a held card, a dropped item, an ended
    intent), a cancel (a closed terminal), the time limit or any failure of this check stops the container, again
    every POLL seconds until the build client has returned (`done`)."""
    try:
        while not done.wait(POLL):
            why = ('cancelled' if cancelled is not None and cancelled.is_set() else None) \
                or autoloop.gate(state, intent) or ('time limit' if clock() > deadline else None)
            write_gate(control, why)
            if why:
                print(f'runner: STOP {why}; stopping the container', file=sys.stderr, flush=True)
                break
        else:
            return
    except Exception as error:  # a gate that cannot be checked is a stop
        print(f'runner: STOP gate check failed ({type(error).__name__}); stopping the container', file=sys.stderr,
              flush=True)
    while True:  # a container gone is no end while the client runs: it may still create it
        stop(name, docker)
        if done.wait(POLL):
            return


def stop(name, docker=call):
    """Stop the container, then kill it if it still runs; a container still running after both is said loudly."""
    for argv in (['docker', 'stop', '--time', str(STOP_SECONDS), name], ['docker', 'kill', name]):
        try:
            docker(argv, timeout=STOP_SECONDS + 30)
        except Refused:
            pass  # already stopped or gone: the inspect below decides
        try:
            if running(name, docker) is not True:
                return True  # confirmed stopped, or gone
        except Refused:
            pass  # no answer: not confirmed
    print(f'runner: FAILED to stop container {name}; stop it by hand: docker kill {name}', file=sys.stderr,
          flush=True)
    return False


def ending(pending, intent):
    """The host's last step for a build that is over: `end` (a run that did not finish is interrupted there and its
    slot, lane and paths are released; a finished one is left as it is). It is pending until applied."""
    steps = json.loads(pending.read_text()) if pending.exists() else []
    if ['end', intent] not in steps:
        for path in (pending.parent.parent, pending.parent):
            path.mkdir(mode=0o700, exist_ok=True)
        temp = pending.with_suffix('.tmp')
        temp.write_text(json.dumps(steps + [['end', intent]]))
        temp.replace(pending)
    return pending


def build(intent, card, state=autoloop.STATE, now=None):
    """One build in its container. Any exit after the checks below (a refusal, a cancel, a failed or timed-out
    client) stops the container with a confirmed stop before its outbox is read, then ends the intent on the host."""
    if not BUILD.fullmatch(intent) or not CARD.fullmatch(card):
        raise Refused('usage: launch.py build autoloop-build-<slug> ANYM-<n>')
    if exists(intent) or (RUNS / intent / 'pending.json').exists():
        raise Refused(f'a container or pending requests of {intent} exist: run `launch.py apply {intent}` first')
    done, cancelled = threading.Event(), threading.Event()
    numbers = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)  # a closed terminal (fence, hold) stops it too
    before = {number: signal.signal(number, lambda *_: (cancelled.set(), stop(intent))) for number in numbers}
    refused, code = None, 1
    try:
        code = contained(intent, card, state, now, done, cancelled)
    except Exception as error:  # any failure (a refusal, a missing brief) still ends the intent below
        refused = error
    finally:
        try:
            if not stop(intent):  # supervision ends only after a confirmed stop
                raise Refused(f'container {intent} still runs; once it stops run `launch.py apply {intent}`')
        finally:
            done.set()
            for number, handler in before.items():
                signal.signal(number, handler)
    pending = ending(gather(intent), intent)
    result = report(apply(pending), pending)
    if refused:
        raise refused
    return result or code


def contained(intent, card, state, now, done, cancelled):
    """The gate, the image, the proxy and the logins, then the attached container under the watcher; a cancel latched
    before the start never starts it, and the client is bounded by the build limit even if the watcher fails."""
    why = autoloop.gate(state, intent)
    if why:
        raise Refused(f'STOP {why}')
    now = now or datetime.now(timezone.utc)
    control = prepare(intent)
    write_gate(control, None)
    name_ = image()
    proxy = network(name_)
    values = {**logins(now, autoloop.LIMITS['build']), **settings(intent, card, production_host(), proxy=proxy)}
    argv = run_argv(intent, name_, sorted(values), control, sys.stdin.isatty(), remove=False, proxy=proxy)
    deadline = time.monotonic() + autoloop.LIMITS['build']
    watcher = threading.Thread(target=watch, args=(state, intent, control, intent, done, deadline),
                               kwargs={'cancelled': cancelled}, daemon=True)
    if cancelled.is_set():
        raise Refused('cancelled before the container started')
    watcher.start()
    try:
        return supervise(argv, client_env(values), autoloop.LIMITS['build'] + 2 * POLL + 2 * (STOP_SECONDS + 30),
                         capture=False)[0]
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refused(f'the build client: {type(error).__name__}') from None


def resume(intent):
    if not BUILD.fullmatch(intent):
        raise Refused('usage: launch.py apply autoloop-build-<slug>')
    if running(intent) and not stop(intent):
        raise Refused(f'container {intent} still runs')
    pending = ending(gather(intent), intent)
    return report(apply(pending), pending)


def check(now=None):
    """The card's first step: the isolation check and one model call in a container with the subscription login. The
    container is stopped (and so removed) on every exit, also after a time-out."""
    now = now or datetime.now(timezone.utc)
    name_ = image()
    proxy = network(name_)
    values = {**logins(now, 300), **settings(host=production_host(), mode='check', proxy=proxy)}
    try:
        code, out, err = supervise(run_argv(CHECK, name_, sorted(values), proxy=proxy), client_env(values), 600)
    except (OSError, subprocess.TimeoutExpired) as error:
        code, out, err = type(error).__name__, '', ''
    finally:
        stop(CHECK)
    sys.stderr.write(err[-4000:])
    answer = out.strip().lower()
    print(f'check: exit {code}; model answer {answer[:40]!r}')
    return 0 if code == 0 and 'ok' in answer else 1


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
