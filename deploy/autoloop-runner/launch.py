#!/usr/bin/env python3
"""The autoloop runner's launcher (ANYM-574, docs/operations/feedback-autoloop.md, Container runner).

    launch.py build INTENT CARD   # the build worker's Superset terminal runs this (scripts/autoloop.py build_start)
    launch.py check               # the isolation check and one model call: the subscription login works in a container

It runs on the owner's Mac. It builds the pinned image (tagged by the hash of this directory), keeps an internal
Docker network whose only way out is the allowlist proxy, and starts one container per build with a fresh clone and
no mount of ~/.ssh, an SSH agent socket, ~/.deedkit or the orchestrator directory. The existing logins go in as
environment values only: the gh token, the Claude subscription's current access token (never its refresh token) and
the Tiden token; no new key is made. Their values never appear on a command line: `docker run -e NAME` reads them
from the launcher's own environment. Two mounts: a read-only control directory (the two brief files and the gate
file, rewritten from the host state every 30 s; a stop there or a closed terminal stops the container) and an outbox
whose `finish`, `decline` and `large` requests for this intent only are applied on the host after the container ends.
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
            f'Intent: {intent}. Card: {card}.')


def run_argv(name, image_name, env_names, control=None, outbox=None, tty=False):
    """`docker run` for one runner container: the internal network, no capabilities, two mounts at most."""
    argv = ['docker', 'run', '--rm', '--interactive', *(['--tty'] if tty else []), '--name', name,
            '--network', NETWORK, '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--init',
            '--memory', '8g', '--pids-limit', '4096', '--label', 'deedkit.autoloop=runner']
    if control:
        argv += ['--mount', f'type=bind,source={control},target={MOUNT}/control,readonly']
    if outbox:
        argv += ['--mount', f'type=bind,source={outbox},target={MOUNT}/outbox']
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
    """The run directory: control/ (the two briefs and the gate file, read-only in the container) and outbox/."""
    run = runs / intent
    control, outbox = run / 'control', run / 'outbox'
    for path in (runs, run, control, outbox):
        path.mkdir(mode=0o700, exist_ok=True)
    for name in ('common.md', 'autoloop-build.md'):
        shutil.copyfile(briefs / name, control / name)
    (outbox / 'requests.jsonl').unlink(missing_ok=True)
    return control, outbox


def write_gate(control, why):
    temp = control / 'gate.tmp'
    temp.write_text(f'{why or "go"}\n')
    temp.replace(control / 'gate')


def requests(outbox, intent):
    """The outbox requests the host applies: this intent only, known actions and reasons, at most ten lines."""
    try:
        lines = (outbox / 'requests.jsonl').read_text(errors='replace').splitlines()[:MAX_REQUESTS]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line[:MAX_LINE])
        except ValueError:
            continue
        if not isinstance(item, dict) or item.get('intent') != intent or item.get('action') not in autoloop.RUNNER_ACTIONS:
            continue
        if (item['action'] == 'decline') != (item.get('reason') in autoloop.DECLINED):
            continue
        step = [item['action'], intent, *(['--reason', item['reason']] if item['action'] == 'decline' else [])]
        if step not in out:
            out.append(step)
    # An end (`decline` or `large`) before `finish`, as the brief orders them.
    return sorted(out, key=lambda step: step[0] == 'finish')


def apply(outbox, intent, main=autoloop.main):
    codes = []
    for step in requests(outbox, intent):
        codes.append((step[0], main(step)))
    return codes


def watch(state, intent, control, name, done, docker=call):
    """Every POLL seconds: the host gate into the gate file; a stop (PAUSE, a held card, a dropped item, an ended
    intent) also stops the container."""
    while not done.wait(POLL):
        why = autoloop.gate(state, intent)
        write_gate(control, why)
        if why:
            print(f'runner: STOP {why}; stopping the container', file=sys.stderr, flush=True)
            stop(name, docker)
            return


def stop(name, docker=call):
    try:
        docker(['docker', 'stop', '--time', str(STOP_SECONDS), name], timeout=STOP_SECONDS + 30)
    except Refused:
        pass  # already gone


def build(intent, card, state=autoloop.STATE, now=None):
    if not BUILD.fullmatch(intent) or not CARD.fullmatch(card):
        raise Refused('usage: launch.py build autoloop-build-<slug> ANYM-<n>')
    why = autoloop.gate(state, intent)
    if why:
        raise Refused(f'STOP {why}')
    now = now or datetime.now(timezone.utc)
    control, outbox = prepare(intent)
    write_gate(control, None)
    name_ = image()
    network(name_)
    values = {**logins(now, autoloop.LIMITS['build']), **settings(intent, card, production_host())}
    argv = run_argv(intent, name_, sorted(values), control, outbox, sys.stdin.isatty())
    done = threading.Event()
    for number in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):  # a closed terminal (fence, hold) stops it too
        signal.signal(number, lambda *_: (done.set(), stop(intent)))
    watcher = threading.Thread(target=watch, args=(state, intent, control, intent, done), daemon=True)
    watcher.start()
    try:
        code = subprocess.run(argv, env=client_env(values)).returncode
    finally:
        done.set()
    for action, result in apply(outbox, intent):
        print(f'runner: {action} applied on the host: exit {result}', file=sys.stderr)
    return code


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
    parser.add_argument('action', choices=['build', 'check'])
    parser.add_argument('intent', nargs='?')
    parser.add_argument('card', nargs='?')
    args = parser.parse_args(argv)
    if (args.action == 'build') != bool(args.intent and args.card):
        parser.error('build takes INTENT CARD; check takes nothing')
    try:
        return build(args.intent, args.card) if args.action == 'build' else check()
    except (Refused, autoloop.Refused) as error:
        print(f'runner: REFUSED {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
