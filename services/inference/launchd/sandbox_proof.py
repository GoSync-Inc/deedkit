#!/usr/bin/env python3
"""The sandbox proof (ANYM-684 criterion 2). Run it INSIDE the profile, as the service user:

    sudo -n -u _deedkitinf sandbox-exec -f deedkit-inference.sb python3 -I sandbox_proof.py

and, from another shell on the mini, `curl -s http://127.0.0.1:8090/` while it waits. It listens on loopback 8090
(accepting is allowed), answers one request, then tries TCP and UDP to a loopback port other than 8090 (a listener
the caller started outside the sandbox: `--other-port`) and TCP to a public address, and prints one JSON line:
{"accepted": true, "tcp_other_loopback": "denied", "udp_other_loopback": "denied", "tcp_public": "denied",
"udp_public": "denied", "bind_other_port": "denied", "bind_all_interfaces": "denied", "child_tcp_public": "denied",
"write_releases": "denied", "write_keys": "denied", "write_home_root": "denied", "write_work": "ALLOWED"}. A denied connect sends no packet. Stdlib only; the public address is only connected to, never written to.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys

PUBLIC = ('1.1.1.1', 443)


def attempt(kind: int, address: tuple[str, int]) -> str:
    sock = socket.socket(socket.AF_INET, kind)
    sock.settimeout(3)
    try:
        if kind == socket.SOCK_STREAM:
            sock.connect(address)
        else:
            sock.sendto(b'x', address)
        return 'ALLOWED'
    except PermissionError:
        return 'denied'
    except OSError as exc:
        return f'error:{exc.errno}'
    finally:
        sock.close()


def bind(address: tuple[str, int]) -> str:
    sock = socket.socket()
    try:
        sock.bind(address)
        sock.listen(1)
        return 'ALLOWED'
    except PermissionError:
        return 'denied'
    except OSError as exc:
        return f'error:{exc.errno}'
    finally:
        sock.close()


def write(path: str) -> str:
    try:
        with open(path, 'w'):
            pass
        os.unlink(path)
        return 'ALLOWED'
    except PermissionError:
        return 'denied'
    except OSError as exc:
        return f'error:{exc.errno}'


def child() -> str:
    """A child process of the sandboxed tree connects to a public address: it inherits the profile."""
    code = ("import socket,sys\ns=socket.socket(); s.settimeout(3)\n"
            "try:\n s.connect(('1.1.1.1',443)); print('ALLOWED')\n"
            "except PermissionError: print('denied')\nexcept OSError as e: print('error:%s' % e.errno)")
    return subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True, timeout=20).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or '').split('\n')[0])
    parser.add_argument('--other-port', type=int, required=True)
    parser.add_argument('--wait', type=float, default=60.0)
    parser.add_argument('--home', default='/Users/_deedkitinf')
    args = parser.parse_args(argv)
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', 8090))
    server.listen(1)
    server.settimeout(args.wait)
    accepted = False
    try:
        connection, _ = server.accept()
        connection.recv(1024)
        connection.sendall(b'HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok')
        connection.close()
        accepted = True
    except OSError:
        pass
    finally:
        server.close()
    print(json.dumps({'accepted': accepted,
                      'tcp_other_loopback': attempt(socket.SOCK_STREAM, ('127.0.0.1', args.other_port)),
                      'udp_other_loopback': attempt(socket.SOCK_DGRAM, ('127.0.0.1', args.other_port)),
                      'tcp_public': attempt(socket.SOCK_STREAM, PUBLIC),
                      'udp_public': attempt(socket.SOCK_DGRAM, PUBLIC),
                      'bind_other_port': bind(('127.0.0.1', 18092)),
                      'bind_all_interfaces': bind(('0.0.0.0', 18093)),
                      'child_tcp_public': child(),
                      'write_releases': write(args.home + '/releases/.sandbox-proof'),
                      'write_keys': write(args.home + '/keys/.sandbox-proof'),
                      'write_home_root': write(args.home + '/.sandbox-proof'),
                      'write_work': write(args.home + '/work/.sandbox-proof')}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
