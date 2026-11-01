#!/usr/bin/env python3
"""One-line health probe of the inference service from production (ANYM-684 criterion 8). Stdlib only.

    python3 inference_probe.py --url https://<mini>.<tailnet>.ts.net [--key-file private/inference/inference.key]

Prints exactly one line and exits 0 whatever the answer, so it can never page or block a deploy:

    inference mini=<host> reachable=yes status=ok queue=<depth>/<active>/<capacity> admission=open|closed
    inference mini=<host> reachable=yes ... key_days_left=<n> key_expires=<iso time>     (with --key-file)
    inference mini=<host> reachable=no reason=refused|timeout|bad_certificate|tls_error|bad_response|unreachable|...

`reachable=yes status=http_503` means something answered (Tailscale Serve when the service process is down).
With a key file it also calls `GET /v1/models` with the key (HTTPS only, no redirects, the key is never printed) and
prints the days left on that key and its expiry, which `keygen --confirm-readback` compares with the new key's.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

LIMIT = 64 * 1024


class Usage(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Answer(Exception):
    """The host answered, but not with a 2xx status."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class Failure(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def clean(value: object) -> str:
    return re.sub(r'[^A-Za-z0-9_./:+=-]', '_', str(value))[:64]


def classify(exc: BaseException) -> str:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        return 'bad_certificate'
    if isinstance(reason, ssl.SSLError):
        return 'tls_error'
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return 'timeout'
    if isinstance(reason, ConnectionRefusedError):
        return 'refused'
    return 'unreachable'


def get_json(url: str, headers: dict[str, str], timeout: float, context: ssl.SSLContext | None) -> dict:
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), NoRedirect)
    request = urllib.request.Request(url, headers={'Accept': 'application/json', **headers})
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(LIMIT + 1)
    except urllib.error.HTTPError as exc:
        raise Answer(exc.code) from None
    except (urllib.error.URLError, OSError) as exc:
        raise Failure(classify(exc)) from None
    try:
        value = json.loads(body[:LIMIT].decode())
    except ValueError:
        raise Failure('bad_response') from None
    if len(body) > LIMIT or not isinstance(value, dict):
        raise Failure('bad_response')
    return value


def days_left(expires_at: str, now: float) -> int:
    expires = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return math.floor((expires.timestamp() - now) / 86400)


def probe(url: str, key_file: str | None, timeout: float, context: ssl.SSLContext | None, now: float) -> str:
    parts = urllib.parse.urlsplit(url)
    host = clean(parts.hostname or '-')
    head = f'inference mini={host}'
    if parts.scheme != 'https' or not parts.hostname:
        return f'{head} reachable=no reason=bad_url'
    base = f'{parts.scheme}://{parts.netloc}'
    try:
        health = get_json(base + '/health', {}, timeout, context)
    except Answer as exc:
        return f'{head} reachable=yes status=http_{exc.status}'
    except Failure as exc:
        return f'{head} reachable=no reason={exc.reason}'
    queue = health['queue'] if isinstance(health.get('queue'), dict) else {}
    line = (f'{head} reachable=yes status={clean(health.get("status", "?"))} '
            f'queue={clean(queue.get("depth", "?"))}/{clean(queue.get("active", "?"))}/{clean(queue.get("capacity", "?"))} '
            f'admission={clean(health.get("admission", "?"))}')
    if key_file is None:
        return line
    try:
        key = Path(key_file).read_text().strip()
    except OSError:
        key = ''
    if not key:
        return f'{line} key=unreadable'
    try:
        models = get_json(base + '/v1/models', {'Authorization': 'Bearer ' + key}, timeout, context)
        expires = models['key_expires_at']
        return f'{line} key_days_left={days_left(expires, now)} key_expires={clean(expires)}'
    except Answer as exc:
        return f'{line} key={"rejected" if exc.status in (401, 403) else "http_" + str(exc.status)}'
    except Failure as exc:
        return f'{line} key=error_{exc.reason}'
    except (KeyError, TypeError, ValueError, AttributeError):
        return f'{line} key=error_bad_response'


class Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise Usage(message)


def main(argv: list[str] | None = None, *, context: ssl.SSLContext | None = None, now: float | None = None) -> int:
    import time
    try:
        parser = Parser(add_help=False)
        parser.add_argument('--url', default=os.environ.get('DEEDKIT_INFERENCE_URL'))
        parser.add_argument('--key-file')
        parser.add_argument('--timeout', type=float, default=5.0)
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
        if not args.url:
            raise Usage('--url')
        print(probe(args.url, args.key_file, args.timeout, context, time.time() if now is None else now))
    except Usage:
        print('inference reachable=no reason=usage')
    except BaseException as exc:  # noqa: BLE001 - the probe never fails
        print(f'inference reachable=no reason=internal_{clean(type(exc).__name__)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
