"""Claude Code headers helper for one DeedKit key (ANYM-498).

`deedkit key install` copies this file to ~/.config/deedkit/headers.py and registers it as
`<interpreter> -I -S <that path> <id>`; Claude Code runs it on each connection. It prints
{"Authorization":"Bearer <token>"} from `<id>.token` in its own directory, and only when that file is a regular 0600
file of this user of at most 1 KiB holding one gateway token. Any failure prints nothing to stdout and exits 1.
Python 3.9+ standard library only (macOS /usr/bin/python3 is 3.9).
"""
import json
import os
import re
import stat
import sys

NAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,49}")  # the admin CLI's key ID rule
TOKEN = re.compile(rb"dk_[A-Za-z0-9_-]{43}\n?")  # a gateway token as `deedkit admin key issue` writes it
LIMIT = 1024


def token(key):
    directory = os.open(os.path.dirname(os.path.abspath(__file__)), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("the directory is not private")
        file = os.open(key + ".token", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        info = os.fstat(file)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 \
                or info.st_size > LIMIT:
            raise ValueError("the token file is not a private file")
        data = os.read(file, LIMIT + 1)
    finally:
        os.close(file)
    if not TOKEN.fullmatch(data):
        raise ValueError("the token file holds no gateway token")
    return data.rstrip(b"\n").decode()


def main(argv):
    try:
        if len(argv) != 1 or not NAME.fullmatch(argv[0]):
            raise ValueError("usage: headers.py <key id>")
        value = token(argv[0])
    except (OSError, ValueError) as exc:
        print("deedkit headers: " + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__), file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps({"Authorization": "Bearer " + value}, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
