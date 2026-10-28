#!/usr/bin/env python3
"""The autoloop runner's isolation check (ANYM-574). The entrypoint runs it before the agent starts; any finding
stops the container, so no build runs with an SSH key, an agent or Docker socket, a variable outside ENV, a mount of
the owner's SSH, DeedKit or orchestrator directories, or a route to the production host (directly or through the
proxy).

    selfcheck.py   (exit 0: `selfcheck: ok`; exit 1: one line per finding)
"""
import os
from pathlib import Path
import socket
import stat
import sys

# The container's whole environment: Docker's and the image's own names, then the launcher's (launch.py ENV).
ENV = frozenset({  # LC_CTYPE: Python's own C-locale coercion (PEP 538) sets it
    'HOME', 'HOSTNAME', 'PATH', 'TERM', 'LANG', 'LC_ALL', 'LC_CTYPE', 'PWD', 'SHLVL', '_', 'OLDPWD', 'COLUMNS', 'LINES',
    'NODE_VERSION', 'YARN_VERSION', 'UV_PYTHON_INSTALL_DIR', 'UV_PYTHON_PREFERENCE', 'UV_LINK_MODE',
    'DISABLE_AUTOUPDATER', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC', 'DISABLE_TELEMETRY', 'GH_NO_UPDATE_NOTIFIER',
    'GH_PROMPT_DISABLED', 'NPM_CONFIG_UPDATE_NOTIFIER', 'COLORTERM',
    'GH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', 'TIDEN_API_TOKEN', 'TIDEN_BASE_URL', 'TIDEN_WORKSPACE_ID',
    'GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL',
    'HTTPS_PROXY', 'HTTP_PROXY', 'https_proxy', 'http_proxy', 'NO_PROXY', 'no_proxy',
    'AUTOLOOP_RUNNER', 'AUTOLOOP_INTENT', 'AUTOLOOP_CARD', 'AUTOLOOP_PROMPT', 'AUTOLOOP_MODEL', 'AUTOLOOP_EFFORT',
    'AUTOLOOP_PROD_HOST', 'AUTOLOOP_REPO', 'AUTOLOOP_MODE',
})
# Mount sources that must never appear (the card: no ~/.ssh, SSH agent socket, ~/.deedkit or orchestrator directory).
FORBIDDEN_MOUNTS = ('.ssh', '.deedkit', 'deedkit-orchestrator', 'docker.sock', 'ssh-agent', 'ssh-auth',
                    'host-services', 'com.apple.launchd', '.config/gh', '.codex', '.claude', '.tiden',
                    'Library/Keychains')
KEY_NAMES = ('id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519', 'id_ecdsa_sk', 'id_ed25519_sk', 'identity')
SOCKET_ROOTS = ('/tmp', '/run', '/var/run', '/private', '/home', '/root')
PROD_PORTS = (22, 443)
TIMEOUT = 5


def keys(home):
    """SSH private keys and agent material under the home directory and the root's."""
    found = []
    for root in {Path(home), Path('/root')}:  # an unreadable root (another user's) is unreadable to the agent too
        try:
            ssh = root / '.ssh'
            if ssh.is_dir() and any(ssh.iterdir()):
                found.append(f'SSH directory not empty: {ssh}')
            for path in root.rglob('*'):
                if path.name in KEY_NAMES or path.name.endswith('.pem') and 'PRIVATE KEY' in _head(path):
                    found.append(f'private key file: {path}')
        except OSError:
            continue
    return found


def _head(path):
    try:
        with open(path, 'rb') as handle:
            return handle.read(4096).decode('utf-8', 'replace')
    except OSError:
        return ''


def sockets(roots=SOCKET_ROOTS):
    """Unix sockets anywhere under the roots: an SSH agent or a Docker socket would show here."""
    found = []
    for root in map(Path, roots):
        if not root.exists():
            continue
        for current, dirs, files in os.walk(root, onerror=lambda _: None):
            for name in files + dirs:
                path = Path(current) / name
                try:
                    if stat.S_ISSOCK(path.lstat().st_mode):
                        found.append(f'socket: {path}')
                except OSError:
                    continue
    return found


def variables(env):
    found = [f'variable outside the runner list: {name}' for name in sorted(set(env) - ENV)]
    if env.get('SSH_AUTH_SOCK') or env.get('SSH_AGENT_PID'):
        found.append('SSH agent variable set')
    return found


def mounts(mountinfo):
    """Bind mounts whose source names a forbidden directory (fields 4 and 10 of /proc/self/mountinfo)."""
    found = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        sources = ' '.join([fields[3], fields[4], *(fields[fields.index('-') + 1:] if '-' in fields else [])])
        found += [f'forbidden mount: {fields[4]} ({word})' for word in FORBIDDEN_MOUNTS if word in sources]
    return found


def direct(host, port, timeout=TIMEOUT):
    """True when a TCP connection to host:port opens."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def proxied(proxy, host, port, timeout=TIMEOUT):
    """True when the proxy answers 200 to CONNECT host:port."""
    name, _, number = proxy.rpartition('/')[2].partition(':')
    try:
        with socket.create_connection((name, int(number or 3128)), timeout=timeout) as conn:
            conn.sendall(f'CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n'.encode())
            return conn.recv(64).split(b' ')[1:2] == [b'200']
    except (OSError, ValueError, IndexError):
        return False


def production(env, connect=direct, tunnel=proxied):
    host, proxy = env.get('AUTOLOOP_PROD_HOST'), env.get('HTTPS_PROXY')
    if not host:
        return ['AUTOLOOP_PROD_HOST is not set: the production check cannot run']
    found = [f'production host reachable: {host}:{port}' for port in PROD_PORTS if connect(host, port)]
    if proxy:
        found += [f'production host reachable through the proxy: {host}:{port}' for port in PROD_PORTS
                  if tunnel(proxy, host, port)]
        if not tunnel(proxy, 'github.com', 443):
            found.append('the proxy does not reach github.com')
    else:
        found.append('HTTPS_PROXY is not set')
    return found


def check(env, home, mountinfo, roots=SOCKET_ROOTS, connect=direct, tunnel=proxied):
    return variables(env) + keys(home) + sockets(roots) + mounts(mountinfo) + production(env, connect, tunnel)


def main():
    try:
        mountinfo = Path('/proc/self/mountinfo').read_text()
    except OSError:
        mountinfo = ''
    found = check(dict(os.environ), os.environ.get('HOME', '/'), mountinfo)
    for line in found:
        print(f'selfcheck: {line}', file=sys.stderr)
    print('selfcheck: ok' if not found else f'selfcheck: {len(found)} finding(s)', file=sys.stderr)
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
