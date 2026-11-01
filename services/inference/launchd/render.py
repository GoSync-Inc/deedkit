#!/usr/bin/env python3
"""Renders the files the Mac mini install puts in place (ANYM-684, docs/operations/inference-mini-setup.md).

Stdlib only; nothing here touches the mini. `render.py --admin NAME --admin-home DIR --python PATH --out DIR` writes

    com.deedkit.inference.plist       LaunchDaemon of the service: user _deedkitinf, sandboxed, nice 10
    com.deedkit.inference-busy.plist  LaunchDaemon of the busy probe: the admin account, KeepAlive
    deedkit-inference.sb              sandbox-exec profile: no network but accepting on loopback 8090; the service home read-only
    deedkit-inference.newsyslog       /etc/newsyslog.d line for the service logs
    config.json                       the service config (ANYM-682 README), aliases left to ANYM-683

The admin account name, its home and the Python are the only inputs; the rest is fixed here so that the plist, the
profile, the config and the probe cannot drift apart (tests/test_inference_ops.py).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import plistlib

SERVICE_USER = '_deedkitinf'
HOME = '/Users/' + SERVICE_USER
LABEL = 'com.deedkit.inference'
BUSY_LABEL = 'com.deedkit.inference-busy'
PORT = 8090
BIND = '127.0.0.1'
DRAIN_SECONDS = 900
EXIT_TIMEOUT = DRAIN_SECONDS + 60          # launchd must not SIGKILL a service that is still draining
SHARED = '/Users/Shared/deedkit-inference'
BUSY_FILE = SHARED + '/busy.json'
PROFILE_PATH = '/usr/local/etc/deedkit-inference/deedkit-inference.sb'
PROBE_PATH = '/usr/local/libexec/deedkit-busy-probe'
LOG_OUT = HOME + '/logs/service.out.log'
LOG_ERR = HOME + '/logs/service.err.log'
BUSY_LOG = SHARED + '/busy-probe.log'


def render_service_plist(admin: str, admin_home: str, python: str) -> bytes:
    """`admin`, `admin_home` and `python` belong to the busy probe; the service plist does not use them."""
    script = (f'DEEDKIT_INFERENCE_RELEASE="$(cat {HOME}/current/RELEASE)" exec {HOME}/current/.venv/bin/deedkit-inference '
              f'serve --config {HOME}/config.json')
    return plistlib.dumps({
        'Label': LABEL,
        'UserName': SERVICE_USER,
        'GroupName': SERVICE_USER,
        'ProgramArguments': ['/usr/bin/sandbox-exec', '-f', PROFILE_PATH, '/bin/sh', '-c', script],
        'WorkingDirectory': HOME + '/work',
        'EnvironmentVariables': {'HOME': HOME, 'PATH': '/usr/bin:/bin', 'HF_HUB_OFFLINE': '1', 'PYTHONDONTWRITEBYTECODE': '1'},
        'RunAtLoad': True,
        # Restart after a crash or the service's own drain limit (exit 3), never after the exit 0 of a finished
        # drain: a deploy's SIGTERM must leave the old release stopped until `kickstart`.
        'KeepAlive': {'SuccessfulExit': False},
        'ProcessType': 'Background',
        'Nice': 10,
        'ExitTimeOut': EXIT_TIMEOUT,
        'ThrottleInterval': 10,
        'StandardOutPath': LOG_OUT,
        'StandardErrorPath': LOG_ERR,
    })


def render_busy_plist(admin: str, admin_home: str, python: str) -> bytes:
    return plistlib.dumps({
        'Label': BUSY_LABEL,
        'UserName': admin,
        'ProgramArguments': [python, PROBE_PATH, '--file', BUSY_FILE],
        'EnvironmentVariables': {'HOME': admin_home, 'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'},
        'RunAtLoad': True,
        'KeepAlive': True,
        'ProcessType': 'Background',
        'Nice': 10,
        'ThrottleInterval': 10,
        'StandardOutPath': BUSY_LOG,
        'StandardErrorPath': BUSY_LOG,
    })


def render_sandbox_profile(port: int = PORT) -> str:
    # `allow default` keeps process and IPC rules as the OS has them (the engines need them); then ALL network operations
    # are denied, and only binding and accepting on the loopback port are allowed back. The last matching rule wins.
    # Accepted TCP connections stay usable in both directions; whether replies need more is measured by sandbox_proof.py
    # on the mini (the pf fallback covers a profile that breaks accepting).
    # Sandboxed code must not change anything that is later read or run outside the sandbox (uv config, releases, the
    # links, the config, the policy, the keys): the whole service home is read-only to it except the four places it
    # works in; the keys are unreadable to it.
    home = HOME
    return f'''(version 1)
; deedkit-inference: the whole process tree may not open an outbound connection, send a packet or listen anywhere
; but on its own loopback port.
(allow default)
(deny network*)
(allow network-bind (local ip "localhost:{port}"))
(allow network-inbound (local ip "localhost:{port}"))
(deny file-write* (subpath "{home}"))
(allow file-write* (subpath "{home}/work") (subpath "{home}/logs") (subpath "{home}/state") (subpath "{home}/.cache"))
(deny file-read* (subpath "{home}/keys") (literal "{home}/probe.key"))
'''


def render_newsyslog() -> str:
    # logfile owner:group mode count size(KB) when flags. N: no signal (launchd holds the descriptor); the next
    # deploy restarts the service onto a fresh file. The service logs metadata only (ANYM-682).
    return ''.join(f'{path} {SERVICE_USER}:{SERVICE_USER} 640 7 10240 * JN\n' for path in (LOG_OUT, LOG_ERR))


def render_config() -> str:
    return json.dumps({
        'version': 1,
        'mode': 'production',
        'bind': BIND,
        'port': PORT,
        'policy_file': HOME + '/policy.json',
        'busy_file': BUSY_FILE,
        'work_dir': HOME + '/work',
        'usage_file': HOME + '/state/usage.json',
        'drain_seconds': DRAIN_SECONDS,
        'aliases': {},
    }, indent=2) + '\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or '').split('\n')[0])
    parser.add_argument('--admin', required=True)
    parser.add_argument('--admin-home', required=True)
    parser.add_argument('--python', required=True)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    values = {'admin': args.admin, 'admin_home': args.admin_home, 'python': args.python}
    args.out.mkdir(parents=True, exist_ok=True)
    files = {
        LABEL + '.plist': render_service_plist(**values),
        BUSY_LABEL + '.plist': render_busy_plist(**values),
        'deedkit-inference.sb': render_sandbox_profile().encode(),
        'deedkit-inference.newsyslog': render_newsyslog().encode(),
        'config.json': render_config().encode(),
    }
    for name, content in files.items():
        (args.out / name).write_bytes(content)
        print(args.out / name)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
