"""The owner's one login step per engine (ANYM-972 criterion 4): `deedkit-android engine login claude|codex`.

Claude: runs `claude setup-token` on a pseudo-terminal, shows its output with any token redacted and writes the token it
printed to the configured `token_file` (0600) without ever echoing it. Codex: runs `codex login --device-auth` (a link and a code to confirm on any device) with `CODEX_HOME` set to the
configured private Codex home, where Codex keeps its own `auth.json`. The agent never runs this: the owner types it."""

import fcntl
import os
from pathlib import Path
import pty
import re
import select
import struct
import subprocess
import sys
import termios

from . import engines

TOKEN = re.compile(rb'sk-ant-[A-Za-z0-9_-]{20,}')
PARTIAL = re.compile(rb'sk-ant-[A-Za-z0-9_-]*\Z')
LIMIT = 1 << 20


def run_with_terminal(argv, env):
    """Run argv on a pseudo-terminal; its output goes to our stdout with tokens redacted, our stdin goes to it.
    Returns (exit code, the last token it printed or None)."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 50, 500, 0, 0))  # wide: a token is never wrapped
    process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
    os.close(slave)
    seen, tail, forward = b'', b'', sys.stdin.isatty()
    token = None
    try:
        while True:
            watch = [master] + ([sys.stdin.fileno()] if forward else [])
            ready, _, _ = select.select(watch, [], [], 0.5)
            if master in ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b''
                if not chunk:
                    break
                seen = (seen + chunk)[-LIMIT:]
                found = TOKEN.findall(seen)
                token = found[-1] if found else token
                # A token that may continue in the next read is held back, so no part of one is ever shown.
                text = tail + chunk
                partial = PARTIAL.search(text)
                tail, text = (text[partial.start():], text[:partial.start()]) if partial else (b'', text)
                shown = TOKEN.sub(b'[token hidden]', text)
                sys.stdout.buffer.write(shown)
                sys.stdout.buffer.flush()
            elif forward and sys.stdin.fileno() in ready:
                data = os.read(sys.stdin.fileno(), 1024)
                if data:
                    os.write(master, data)
            elif process.poll() is not None and master not in ready:
                break
        if tail:
            sys.stdout.buffer.write(TOKEN.sub(b'[token hidden]', tail))
            sys.stdout.buffer.flush()
    finally:
        os.close(master)
    return process.wait(), token


def write_private(path, data):
    """Replace `path` with `data` as a 0600 regular file; the old file stays until the new one is complete."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def login(config, name):
    """The login of one engine; returns the exit code (0 only when a usable login now exists)."""
    engine = config.engines.get(name)
    if engine is None:
        print('engine_not_configured', file=sys.stderr)
        return 2
    if name == 'claude':
        if not engine.token_file:
            print('token_file_not_configured', file=sys.stderr)
            return 2
        code, token = run_with_terminal([engine.binary, 'setup-token'], dict(os.environ))
        if code != 0 or token is None:
            print('login_failed' if code != 0 else 'token_not_found', file=sys.stderr)
            return 1
        write_private(engine.token_file, token + b'\n')
    else:
        if not engine.codex_home:
            print('codex_home_not_configured', file=sys.stderr)
            return 2
        home = Path(engine.codex_home)
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        home.chmod(0o700)
        code = subprocess.run([engine.binary, 'login', '--device-auth'], env={**os.environ, 'CODEX_HOME': str(home)}, check=False).returncode
        auth = home / 'auth.json'
        if auth.is_file() and not auth.is_symlink():
            auth.chmod(0o600)
        if code != 0:
            print('login_failed', file=sys.stderr)
            return 1
    if engines.login_env(name, engine) is None:
        print('login_not_usable', file=sys.stderr)
        return 1
    print(f'{name} login stored; run `deedkit-android engine probe {name} --config FILE` next.')
    return 0
