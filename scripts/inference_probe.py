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

Upload throughput (ANYM-692): `--upload-bytes N --key-file K [--timeout S]` (N 1 to 8,388,608; S default 300) sends N random
bytes as the `file` of `POST /v1/audio/transcriptions` (fields `model`, `response_format`, a valid deadline header). The
service reads the whole body, then refuses it with 422 `audio_unreadable` before any engine runs, so no job is spent:

    inference upload bytes=N seconds=S kbytes_per_s=R status=<code|reason> refusal=<code|-> valid=yes|no

`valid=yes` only for a 422 that carries `audio_unreadable`; anything else (408, 429, 403, 502, 503, a timeout, an early 422
before the body was read) is `valid=no` with its status and refusal. The seconds run from after the TCP and TLS connect to
the end of the reply: the endpoint's effective throughput (1 KB = 1000 bytes), not raw link capacity. The key is never
printed; no redirect is followed; nothing of the response body is printed but its error code.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LIMIT = 64 * 1024
UPLOAD_MAX = 8 * 1024 * 1024
UPLOAD_TIMEOUT = 300.0
CHUNK = 64 * 1024


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


def error_code(body: bytes) -> str:
    """The service's `{"error": {"code": ...}}` code, `-` for any other body."""
    try:
        code = json.loads(body.decode())['error']['code']
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return '-'
    return clean(code) if isinstance(code, str) and code else '-'


def upload_line(size: int, seconds: float, status: str, refusal: str = '-', valid: bool = False) -> str:
    rate = size / 1000 / seconds if seconds > 0 else 0.0
    return (f'inference upload bytes={size} seconds={seconds:.3f} kbytes_per_s={rate:.1f} '
            f'status={status} refusal={refusal} valid={"yes" if valid else "no"}')


def upload(url: str, key_file: str, size: int, timeout: float, context: ssl.SSLContext | None) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or not 1 <= size <= UPLOAD_MAX or timeout <= 0:
        return upload_line(size, 0.0, 'usage')
    try:
        key = Path(key_file).read_text().strip()
    except OSError:
        key = ''
    if not key:
        return upload_line(size, 0.0, 'key_unreadable')
    boundary = os.urandom(16).hex()
    head = ''.join(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
                   for name, value in (('model', 'audio-default'), ('response_format', 'json')))
    head += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="probe.bin"\r\nContent-Type: application/octet-stream\r\n\r\n'
    tail = f'\r\n--{boundary}--\r\n'.encode()
    head_bytes = head.encode()
    connection = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=min(timeout, 30.0), context=context)
    try:
        connection.connect()
    except (OSError, http.client.HTTPException) as exc:
        return upload_line(size, 0.0, classify(exc))
    started = time.monotonic()
    limit = started + timeout

    def remaining() -> float:
        left = limit - time.monotonic()
        if left <= 0:
            raise TimeoutError
        connection.sock.settimeout(left)
        return left

    def body():
        remaining()
        yield head_bytes
        sent = 0
        while sent < size:
            piece = os.urandom(min(CHUNK, size - sent))
            remaining()
            yield piece
            sent += len(piece)
        remaining()
        yield tail

    headers = {'Authorization': 'Bearer ' + key, 'Content-Type': f'multipart/form-data; boundary={boundary}',
               'Content-Length': str(len(head_bytes) + size + len(tail)), 'Accept': 'application/json',
               'Accept-Encoding': 'identity', 'Connection': 'close',
               'X-Request-Deadline-Seconds': str(max(1, int(timeout)))}
    try:
        try:
            connection.request('POST', '/v1/audio/transcriptions', body=body(), headers=headers)
        except (TimeoutError, socket.timeout):
            raise
        except OSError:
            pass  # the service answered before the body was read (an early refusal) and closed: read its answer
        remaining()
        response = connection.getresponse()
        answer = response.read(LIMIT + 1)[:LIMIT]
        elapsed = time.monotonic() - started
        refusal = error_code(answer)
        return upload_line(size, elapsed, str(response.status), refusal, response.status == 422 and refusal == 'audio_unreadable')
    except (TimeoutError, socket.timeout):
        return upload_line(size, time.monotonic() - started, 'timeout')
    except (OSError, http.client.HTTPException) as exc:
        reason = 'bad_response' if isinstance(exc, http.client.HTTPException) else classify(exc)
        return upload_line(size, time.monotonic() - started, reason)
    finally:
        connection.close()


class Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise Usage(message)


def main(argv: list[str] | None = None, *, context: ssl.SSLContext | None = None, now: float | None = None) -> int:
    try:
        parser = Parser(add_help=False)
        parser.add_argument('--url', default=os.environ.get('DEEDKIT_INFERENCE_URL'))
        parser.add_argument('--key-file')
        parser.add_argument('--timeout', type=float)
        parser.add_argument('--upload-bytes', type=int)
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
        if not args.url:
            raise Usage('--url')
        if args.upload_bytes is not None:
            if not args.key_file:
                raise Usage('--key-file')
            print(upload(args.url, args.key_file, args.upload_bytes, UPLOAD_TIMEOUT if args.timeout is None else args.timeout, context))
            return 0
        print(probe(args.url, args.key_file, 5.0 if args.timeout is None else args.timeout, context, time.time() if now is None else now))
    except Usage:
        print('inference reachable=no reason=usage')
    except BaseException as exc:  # noqa: BLE001 - the probe never fails
        print(f'inference reachable=no reason=internal_{clean(type(exc).__name__)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
