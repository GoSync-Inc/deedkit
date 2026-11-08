"""Omi `memory_created` webhook as a wake-up hint (ANYM-760). Three parts, one contract: an id brings the next list forward.

`Receiver` is the internet-facing process of its own container: one secret route, the id taken from the body and everything
else dropped. It has no network at all: it serves on a Unix socket that only `relay` reads, and its one connection goes to the
backend's channel socket. `relay` is a byte pipe with bounds, the only part on a network the proxy shares; it parses nothing.
`Channel` is the backend's second listener, on that socket (or an address), with its own admission. `Hints` is the
in-memory queue the collector's sleep wakes on: it lists the newest page once, outside the regular pass, and reads only
never-fetched conversations of the last 24 hours. No hint names a record to read, stores an id or spends above `omi_budget`;
polling stays the source of truth. Nothing of a request but counters by reason is ever logged or kept.
"""
import asyncio
from collections import Counter, OrderedDict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import time
from typing import Annotated
from urllib.parse import parse_qs

import httpx
from pydantic import Field

from .omi import ID, fingerprint, metadata, pagination
from .security import Failure, RateLimited, Strict, digest_matches, hint_authorized, loads, private

ROUTE_PREFIX = '/omi/memory/'
PATH_TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')  # 256 random bits, base64url
MAX_BODY = 4 * 1024 ** 2
DEADLINE = 2  # seconds for one delivery, the body and the channel call together
SLOTS = 4
RECEIVER_PORT = 13236
KEY_TTL, KEY_ENTRIES = 600, 1024  # the same Idempotency-Key inside ten minutes has one effect
LOG_EVERY = 300
RECEIVER_REASONS = frozenset(('accepted', 'duplicate', 'not_found', 'bad_uid', 'bad_shape', 'too_large', 'deadline', 'busy',
                              'channel_failed'))

CHANNEL_PATH = '/omi-hint'
CHANNEL_ALIAS = 'recordings-hint-channel'
CHANNEL_PORT = 13235
CHANNEL_BODY = 4096
CHANNEL_IDS = 16
CHANNEL_SLOTS = 2
CHANNEL_PER_MINUTE = 20
CHANNEL_DEADLINE = 2
CHANNEL_CONNECTIONS = 16
RECEIVER_CONNECTIONS = 64
CONNECTION_LIFETIME = 5  # seconds any connection may live: above one request's deadline, so idle and half-sent ones end too
LISTEN_ENV = 'RECORDINGS_HINT_LISTEN'
CHANNEL_URL = 'http://' + CHANNEL_ALIAS + CHANNEL_PATH  # over the socket: the host is a label, never resolved
SOCKET_PATH = re.compile(r'/[A-Za-z0-9._/-]{1,90}\.sock')  # inside AF_UNIX's 104-byte limit on every platform
RECEIVER_SOCKET = '/run/deedkit/omi-hook/receiver.sock'  # on the tmpfs volume only the receiver writes and the relay reads
CHANNEL_SOCKET = '/run/deedkit/omi-hint-channel/channel.sock'  # on the tmpfs volume only the backend writes and the receiver reads
RELAY_CONNECTIONS = RECEIVER_CONNECTIONS
RELAY_UP = MAX_BODY + 65536  # bytes from the proxy per connection: one capped body and its headers
RELAY_DOWN = 16384  # bytes back per connection: the receiver's answers are fixed and short
RELAY_CHUNK = 65536

SETTLE = 15  # Omi caches a list for 10 s
RETRIES = (60, 300)  # a hinted id the list does not show is looked for again this long after the hint, then polling has it
PENDING = 64
SEEN, SEEN_TTL = 1024, 600
FORCED_PER_HOUR = 6
HINT_WORK = 3  # the never-fetched class of one work selection (archive_store.py, work_items)
PAGE = 200
PASS_DEADLINE = 60
HOUR, DAY = 3600, 86400

JSON_HEADERS = [(b'content-type', b'application/json'), (b'cache-control', b'no-store')]
BODIES = {200: b'{"status":"ok"}', 400: b'{"error":"bad_request"}', 401: b'{"error":"unauthorized"}', 404: b'{"error":"not_found"}',
          408: b'{"error":"request_timeout"}', 413: b'{"error":"too_large"}', 429: b'{"error":"busy"}', 503: b'{"error":"unavailable"}'}


def monotonic():
    """One seam for the channel's window and the receiver's key memory (tests move it)."""
    return time.monotonic()


def wall():
    """One seam for the time a hint arrives, in the unit of a collector pass's `now`."""
    return time.time()


class Reject(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


async def answer(send, status):
    """The whole response. Fixed bytes per status: no request value can reach it."""
    body = BODIES[status]
    await send({'type': 'http.response.start', 'status': status,
                'headers': [*JSON_HEADERS, (b'content-length', str(len(body)).encode())]})
    await send({'type': 'http.response.body', 'body': body})


def header(scope, name):
    for key, value in scope.get('headers') or ():
        if key == name:
            return value
    return None


async def read_body(scope, receive, maximum, seconds):
    """The body inside its cap and deadline; the read stops at the first chunk past the cap."""
    declared = header(scope, b'content-length')
    if declared is not None and declared.isdigit() and int(declared[:16]) > maximum:
        raise Reject(413, 'too_large')
    body = bytearray()
    try:
        async with asyncio.timeout(seconds):
            while True:
                message = await receive()
                if message['type'] != 'http.request':
                    raise Reject(400, 'bad_shape')
                chunk = message.get('body', b'')
                if len(body) + len(chunk) > maximum:
                    raise Reject(413, 'too_large')
                body.extend(chunk)
                if not message.get('more_body', False):
                    return body
    except TimeoutError:
        raise Reject(408, 'deadline') from None


def bounded_protocol(limit, lifetime):
    """uvicorn's h11 protocol with a transport bound. uvicorn itself accepts every connection and applies `limit_concurrency` only
    after complete headers, and its keep-alive timer starts after a response, so idle or half-sent connections would hold
    descriptors outside any admission. Pinned uvicorn 0.54.0."""
    from uvicorn.protocols.http.h11_impl import H11Protocol

    class Bounded(H11Protocol):
        timer, refused = None, False

        def connection_made(self, transport):
            if len(self.connections) >= limit:
                self.refused = True
                transport.abort()
                return
            super().connection_made(transport)
            self.timer = self.loop.call_later(lifetime, transport.abort)

        def connection_lost(self, exc):
            if self.refused:
                return
            if self.timer is not None:
                self.timer.cancel()
            super().connection_lost(exc)
    return Bounded


# --- The receiver process

class ReceiverSettings(Strict):
    """The receiver's own private file. The backend's configuration, archive and provider tokens are not in its mount."""
    uid: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    path_token_sha256: list[Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]] = Field(min_length=1, max_length=2)
    channel_socket: str = Field(pattern=SOCKET_PATH.pattern.join('^$'))
    channel_key_file: str


def conversation_id(raw):
    """The conversation id of a delivery. The parsed document goes out of scope with this call."""
    try:
        value = loads(raw)
    except (ValueError, TypeError, RecursionError):
        raise Reject(400, 'bad_shape') from None
    ident = value.get('id') if isinstance(value, dict) else None
    if not isinstance(ident, str) or not ID.fullmatch(ident):
        raise Reject(400, 'bad_shape')
    return ident


class Receiver:
    """ASGI app. Everything but one route answers one fixed 404 before the body is read."""
    def __init__(self, path, *, transport=None, deadline=DEADLINE):
        self.path, self.transport, self.deadline = Path(path).absolute(), transport, deadline
        self.counters = Counter()
        self.active = 0
        self.keys = OrderedDict()  # digests of delivered Idempotency-Key values
        self.loaded, self.stamp = None, None
        self.logged, self.last_line = monotonic(), None

    def settings(self):
        """The current private file (an atomic replace reaches the running receiver); None when it cannot be used."""
        try:
            info = os.stat(self.path)
            stamp = info.st_ino, info.st_mtime_ns, info.st_size
            if stamp != self.stamp:
                self.loaded = ReceiverSettings.model_validate(loads(private(str(self.path), 65536)))
                self.stamp = stamp
            return self.loaded
        except (OSError, ValueError, TypeError, RecursionError):
            self.loaded, self.stamp = None, None
            return None

    def report(self):
        return dict(self.counters)

    def log_counters(self, force=False):
        """Counters by reason, at most one line per interval and only when something but a liveness probe changed."""
        moment = monotonic()
        if not force and moment - self.logged < LOG_EVERY:
            return
        self.logged = moment
        line = json.dumps(self.report(), sort_keys=True)
        rest = {key: value for key, value in self.counters.items() if key != 'not_found'}
        if force or rest != self.last_line:
            self.last_line = rest
            print('omi_hint_receiver ' + line, flush=True)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return
        try:
            status, reason = await self.handle(scope, receive)
        except Exception:  # a fault here is a redelivery for Omi and a counter for us: never a traceback holding a value
            status, reason = 503, 'channel_failed'
        self.counters[reason] += 1
        await answer(send, status)
        self.log_counters()

    async def handle(self, scope, receive):
        path = scope.get('path', '')
        token = path[len(ROUTE_PREFIX):] if path.startswith(ROUTE_PREFIX) else ''
        settings = self.settings() if scope.get('method') == 'POST' and PATH_TOKEN.fullmatch(token) else None
        if settings is None or not digest_matches(settings.path_token_sha256, token):
            return 404, 'not_found'
        if self.active >= SLOTS:
            return 503, 'busy'
        self.active += 1
        try:
            await self.deliver(scope, receive, settings)
            return 200, 'accepted'
        except Reject as refused:
            if refused.reason == 'duplicate':
                return 200, 'duplicate'
            return refused.status, refused.reason
        finally:
            self.active -= 1

    async def deliver(self, scope, receive, settings):
        started = monotonic()
        query = parse_qs(scope.get('query_string', b'').decode('latin-1'), keep_blank_values=True)
        if query.get('uid') != [settings.uid]:
            raise Reject(400, 'bad_uid')
        ident = conversation_id(await read_body(scope, receive, MAX_BODY, self.deadline))
        key = self.key_digest(scope)
        if key is not None and self.recent(key):
            raise Reject(200, 'duplicate')
        await self.forward(settings, ident, self.deadline - (monotonic() - started))
        if key is not None:
            self.remember(key)

    @staticmethod
    def key_digest(scope):
        raw = header(scope, b'idempotency-key')
        return hashlib.sha256(raw).hexdigest() if raw and len(raw) <= 256 else None

    def recent(self, key):
        moment = monotonic()
        while self.keys and next(iter(self.keys.values())) <= moment - KEY_TTL:
            self.keys.popitem(last=False)
        return key in self.keys

    def remember(self, key):
        """Only after the channel took the hint: a failed delivery must succeed on Omi's redelivery."""
        self.keys[key] = monotonic()
        self.keys.move_to_end(key)
        while len(self.keys) > KEY_ENTRIES:
            self.keys.popitem(last=False)

    async def forward(self, settings, ident, seconds):
        """The one connection this process starts, to the channel's socket file: the receiver has no network. Only the id crosses it."""
        if seconds <= 0:
            raise Reject(503, 'channel_failed')
        try:
            credential = private(settings.channel_key_file, 1024).decode().strip()
            transport = self.transport or httpx.AsyncHTTPTransport(uds=settings.channel_socket)
            async with asyncio.timeout(seconds):
                async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                             timeout=seconds) as client:
                    response = await client.post(CHANNEL_URL, content=json.dumps({'ids': [ident]}, separators=(',', ':')),
                                                 headers={'Authorization': 'Bearer ' + credential, 'Content-Type': 'application/json'})
        except (OSError, ValueError, TimeoutError, httpx.HTTPError):
            raise Reject(503, 'channel_failed') from None
        if response.status_code != 200:
            raise Reject(503, 'channel_failed')


# --- The backend's channel

class Channel:
    """ASGI app of the second listener: `POST /omi-hint` only, its own admission, its own deadline."""
    def __init__(self, hints, *, deadline=CHANNEL_DEADLINE):
        self.hints, self.deadline = hints, deadline
        self.active = 0
        self.window = deque()

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return
        await answer(send, await self.handle(scope, receive))

    async def handle(self, scope, receive):
        if scope.get('method') != 'POST' or scope.get('path') != CHANNEL_PATH:
            return 404
        moment = monotonic()
        while self.window and self.window[0] <= moment - 60:
            self.window.popleft()
        if self.active >= CHANNEL_SLOTS or len(self.window) >= CHANNEL_PER_MINUTE:
            return 429
        self.window.append(moment)
        self.active += 1
        try:
            return await self.admitted(scope, receive)
        except Reject as refused:
            return refused.status
        except Exception:
            return 503
        finally:
            self.active -= 1

    async def admitted(self, scope, receive):
        try:
            archive = self.hints.runtime.document().archive
        except Failure:
            return 503
        section = archive.omi_hint if archive is not None else None
        if section is None:
            return 404  # no route without the object: a revocation needs no restart
        authorization = header(scope, b'authorization') or b''
        presented = authorization[7:] if authorization.startswith(b'Bearer ') else b''
        if not 1 <= len(presented) <= 512 or not hint_authorized(section, presented.decode('latin-1')):
            return 401
        raw = await read_body(scope, receive, CHANNEL_BODY, self.deadline)
        try:
            value = loads(raw)
        except (ValueError, TypeError, RecursionError):
            return 400
        ids = value.get('ids') if isinstance(value, dict) and set(value) == {'ids'} else None
        if (not isinstance(ids, list) or not 1 <= len(ids) <= CHANNEL_IDS
                or not all(isinstance(ident, str) and ID.fullmatch(ident) for ident in ids)):
            return 400
        self.hints.offer(ids, wall())
        return 200


class MainGuard:
    """The main API and the event feed never answer on the hint address (the host rules drop it first; this holds without them)."""
    def __init__(self, app, hints):
        self.app, self.hints = app, hints

    async def __call__(self, scope, receive, send):
        server = scope.get('server')
        if scope['type'] == 'http' and server and self.hints.address is not None and server[0] == self.hints.address:
            await answer(send, 404)
            return
        await self.app(scope, receive, send)


class Listener:
    def __init__(self, hints, server, task, port, path=None):
        self.hints, self.server, self.task, self.port, self.path = hints, server, task, port, path

    async def stop(self):
        self.server.should_exit = True
        try:
            async with asyncio.timeout(5):
                await asyncio.shield(self.task)
        except TimeoutError:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        if self.path is not None:
            try:
                if stat.S_ISSOCK(os.lstat(self.path).st_mode):
                    os.unlink(self.path)
            except OSError:
                pass
        self.hints.address, self.hints.listener = None, None


def unix_socket(value):
    """A listening socket at `value`, an absolute `.sock` path in a directory only this user can enter. A stale socket there is
    replaced; any other file is refused, never removed. Nothing is resolved, so nothing here waits on a name or a network."""
    if not isinstance(value, str) or not SOCKET_PATH.fullmatch(value) or '/../' in value + '/' or '/./' in value + '/':
        raise ValueError('socket path')
    parent = os.lstat(os.path.dirname(value))
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
        raise ValueError('socket directory not private')
    try:
        if not stat.S_ISSOCK(os.lstat(value).st_mode):
            raise ValueError('not a socket')
        os.unlink(value)
    except FileNotFoundError:
        pass
    listening = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listening.bind(value)
        os.chmod(value, 0o600)
        listening.setblocking(False)
    except OSError:
        listening.close()
        raise
    return listening


async def bound_socket(value):
    """A socket on the one address `host:port` names; a wildcard, a missing or an ambiguous name is refused."""
    host, separator, port = value.rpartition(':')
    if not separator or not host or not port.isascii() or not port.isdigit() or int(port) > 65535:
        raise ValueError('listen address')
    host = host.strip('[]')
    found = await asyncio.get_running_loop().getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
    addresses = {info[4][0] for info in found}
    if len(addresses) != 1:
        raise ValueError('ambiguous address')
    address = addresses.pop()
    if ipaddress.ip_address(address).is_unspecified:
        raise ValueError('wildcard address')
    listening = socket.socket(found[0][0], socket.SOCK_STREAM)
    try:
        listening.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listening.setblocking(False)
        listening.bind(found[0][4])
    except OSError:
        listening.close()
        raise
    return listening, address


async def start_listener(runtime, environ=None):
    """The second listener, or None: without the variable or the object nothing listens. A failure here leaves the backend as it
    is and is named in the status. Binding here, not in uvicorn, because uvicorn exits the process on a failed bind. An absolute
    path is a Unix socket (the deployed form: the receiver shares no network with the backend); `host:port` is the older form."""
    environ = os.environ if environ is None else environ
    hints = runtime.hints
    value = environ.get(LISTEN_ENV)
    if not value:
        return None
    try:
        archive = runtime.document().archive
        if archive is None or archive.omi_hint is None:
            return None
        if value.startswith('/'):
            listening, address = unix_socket(value), None
        else:
            async with asyncio.timeout(5):
                listening, address = await bound_socket(value)
    except (Failure, OSError, ValueError, TimeoutError):
        hints.listener_error = 'listener_unavailable'
        return None
    import uvicorn

    class Quiet(uvicorn.Server):
        @contextmanager
        def capture_signals(self):  # the main server owns the signals; this one stops from the lifespan
            yield

    # No request line, header or body is ever logged here. Connections are bounded at the transport; admission does the rest.
    config = uvicorn.Config(Channel(hints), http=bounded_protocol(CHANNEL_CONNECTIONS, CONNECTION_LIFETIME), log_config=None, access_log=False, lifespan='off', server_header=False, date_header=False,
                            timeout_keep_alive=1, limit_concurrency=CHANNEL_CONNECTIONS, proxy_headers=False,
                            h11_max_incomplete_event_size=16384)
    server = Quiet(config)
    unix = listening.family == socket.AF_UNIX
    port = None if unix else listening.getsockname()[1]

    async def serve():
        try:
            await server.serve(sockets=[listening])
        except (Exception, SystemExit):
            hints.listener_error = 'listener_unavailable'
    task = asyncio.create_task(serve())
    while not server.started and not task.done():
        await asyncio.sleep(0.005)
    if not server.started:
        hints.listener_error = 'listener_unavailable'
        listening.close()
        return None
    hints.address, hints.listener_error = address, None
    hints.listener = Listener(hints, server, task, port, value if unix else None)
    return hints.listener


# --- The relay: the proxy's side of the receiver

async def pipe(source, target, limit):
    """Copy bytes one way until end of file, at most `limit` of them; past that the connection is cut. Nothing is parsed or kept.
    No half-close is passed on: uvicorn ends a connection on end of file, which would drop an answer still on its way."""
    total = 0
    while chunk := await source.read(RELAY_CHUNK):
        total += len(chunk)
        if total > limit:
            raise ValueError('relay limit')
        target.write(chunk)
        await target.drain()


async def serve_relay(host, port, upstream, *, limit=None, lifetime=None):
    """A TCP listener whose every connection is piped to the receiver's socket, and nothing else: the receiver itself has no
    network, so even a compromised receiver opens no connection to the proxy or anything on its network. Bounded at the transport:
    at most `limit` connections, each living at most `lifetime` seconds, capped bytes each way."""
    limit = RELAY_CONNECTIONS if limit is None else limit
    lifetime = CONNECTION_LIFETIME if lifetime is None else lifetime
    active = set()

    async def handle(reader, writer):
        if len(active) >= limit:
            writer.transport.abort()
            return
        task = asyncio.current_task()
        active.add(task)
        upstream_writer, legs, clean = None, (), False
        writer.transport.set_write_buffer_limits(0)  # drain() returns only once every byte went to the kernel
        try:
            async with asyncio.timeout(lifetime):
                upstream_reader, upstream_writer = await asyncio.open_unix_connection(upstream)
                upstream_writer.transport.set_write_buffer_limits(0)
                legs = (asyncio.create_task(pipe(reader, upstream_writer, RELAY_UP)),
                        asyncio.create_task(pipe(upstream_reader, writer, RELAY_DOWN)))
                up, down = legs
                await asyncio.wait(legs, return_when=asyncio.FIRST_COMPLETED)
                if up.done() and up.exception() is None and not down.done():
                    await down  # the proxy is done sending; the answer may still be on its way
                clean = down.done() and down.exception() is None  # the receiver ended it after a whole answer
        except (OSError, ValueError, TimeoutError):
            pass
        finally:
            for leg in legs:
                leg.cancel()
            await asyncio.gather(*legs, return_exceptions=True)
            if upstream_writer is not None:
                upstream_writer.transport.abort()
            if clean:
                writer.close()  # every byte is already with the kernel (no buffer above): a normal close delivers it
            else:
                writer.transport.abort()
            active.discard(task)
    server = await asyncio.start_server(handle, host, port, limit=RELAY_CHUNK)
    server.relay_active = active
    return server


def run_relay(host, port, upstream):
    async def forever():
        server = await serve_relay(host, port, upstream)
        async with server:
            await server.serve_forever()
    asyncio.run(forever())


# --- The collector's side

class Hints:
    """Pending ids in memory only. A restart forgets them, which is harmless: a hint is idempotent and polling covers it."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.wake = asyncio.Event()
        self.pending = OrderedDict()  # id -> {'received', 'at', 'attempts'}
        self.seen = OrderedDict()     # id -> when it was last taken, for the ten-minute dedupe
        self.forced = deque()
        self.counters = Counter()
        self.address, self.listener, self.listener_error = None, None, None

    def offer(self, ids, now):
        """Take ids from the channel. No await: a flood costs a dictionary write and never a provider call."""
        for ident in ids:
            self.counters['received'] += 1
            taken = self.seen.get(ident)
            if taken is not None and now - taken < SEEN_TTL:
                self.counters['duplicate'] += 1
                continue
            self.seen[ident] = now
            self.seen.move_to_end(ident)
            while len(self.seen) > SEEN:
                self.seen.popitem(last=False)
            self.pending[ident] = {'received': now, 'at': now + SETTLE, 'attempts': 0}
            self.pending.move_to_end(ident)
            while len(self.pending) > PENDING:
                self.pending.popitem(last=False)
                self.counters['dropped'] += 1
        self.wake.set()

    def next_at(self):
        return min((entry['at'] for entry in self.pending.values()), default=None)

    def report(self):
        return {'received': self.counters['received'], 'duplicate': self.counters['duplicate'], 'found': self.counters['found'],
                'dropped': self.counters['dropped'], 'failed': self.counters['failed'], 'forced_lists': self.counters['forced_lists'],
                'deferred': self.counters['deferred'],
                'pending': len(self.pending), 'listening': self.listener is not None, 'listener_error': self.listener_error}

    async def rest(self, seconds):
        """The collector's sleep: the same interval, woken by a hint, with hint passes run on the way. A regular pass keeps its time."""
        deadline = monotonic() + seconds
        while (left := deadline - monotonic()) > 0:
            due = self.next_at()
            wait = left if due is None else min(left, max(due - wall(), 0))
            self.wake.clear()
            try:
                async with asyncio.timeout(wait):
                    await self.wake.wait()
                continue
            except TimeoutError:
                pass
            due = self.next_at()
            if due is not None and due <= wall():
                await self.run()

    def batch(self, now):
        """Everything a list made now answers: the due ids and every hint still inside its settle window (they coalesce)."""
        if not any(entry['at'] <= now for entry in self.pending.values()):
            return []
        return [ident for ident, entry in self.pending.items() if entry['at'] <= now or entry['attempts'] == 0]

    async def run(self, now=None):
        """One hint pass, if one is due. Contained: nothing here reaches `error`, the journal or the regular pass."""
        if not self.pending:
            return
        async with self.runtime.collecting:  # a pass already running finishes first
            moment = wall() if now is None else now
            batch = self.batch(moment)
            if not batch:
                return
            try:
                async with asyncio.timeout(PASS_DEADLINE):
                    shown = await self.look(moment)
            except Exception:
                self.counters['failed'] += 1
                shown = set()
            self.settle(batch, shown, moment)

    def settle(self, batch, shown, now):
        """`shown` None: no list could be made (a refused reservation, a pause, no account, no allowance): the hints are dropped."""
        if shown is None:
            for ident in batch:
                self.pending.pop(ident, None)  # a flood during the pass may have pushed it out already
            self.counters['dropped'] += len(batch)
            return
        for ident in shown & self.pending.keys():
            del self.pending[ident]
            self.counters['found'] += 1
        missed = [ident for ident in batch if ident in self.pending]
        later = []
        for ident in missed:
            entry = self.pending[ident]
            entry['attempts'] += 1
            if entry['attempts'] > len(RETRIES):
                del self.pending[ident]
                self.counters['dropped'] += 1
            else:
                later.append(entry)
        if later:
            # One time for the whole batch, so its ids keep sharing one list.
            at = max(now + SETTLE, *(entry['received'] + RETRIES[entry['attempts'] - 1] for entry in later))
            for entry in later:
                entry['at'] = at

    def allowed(self, now):
        while self.forced and self.forced[0] <= now - HOUR:
            self.forced.popleft()
        return len(self.forced) < FORCED_PER_HOUR

    async def look(self, now):
        """The forced list and the reads it leads to. The set of listed ids, or None when no list was made."""
        runtime = self.runtime
        doc = runtime.document()
        settings = doc.archive
        if runtime.store is None or settings is None or settings.omi_hint is None:
            return None
        section = settings.omi_hint
        account = next((a for a in doc.accounts if a.id == section.account and a.provider == 'omi'
                        and a.archive_namespace is not None), None)
        if account is None or not self.allowed(now):
            return None
        runtime.store.feed_enabled = doc.event_router is not None
        await runtime.db('capacity')
        token = runtime.current(account)
        credential = hashlib.sha256(token.encode()).hexdigest()
        runtime.reclock()
        reads = settings.omi_budget.reads_per_hour
        if not await runtime.reserve(credential, [('reads', reads)], runtime.clock(now)):
            return None  # the budget or a 429 pause: no provider call, no error
        self.forced.append(now)
        self.counters['forced_lists'] += 1
        try:
            values, headers = await runtime.provider.get(token, params={'limit': PAGE, 'offset': 0, 'include_transcript': 'false'})
        except RateLimited as limited:
            await runtime.throttle(credential, limited.retry_after_s, runtime.clock(now))
            return None
        if not isinstance(values, list) or len(values) > PAGE:
            raise Failure('invalid_provider_response')
        items = [metadata(value) for value in values]
        fingerprints = {item['id']: fingerprint(value) for item, value in zip(items, values)}
        info, next_page = pagination(headers, len(items), PAGE)
        if await self.contiguous(account, items, info, next_page):
            cutoff = datetime.fromtimestamp(now - DAY, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
            fresh = [item for item in items if str(item.get('created_at', ''))[:19] >= cutoff]
            await runtime.db('discover', account, fresh, None, now, lambda: runtime.current(account, credential), 'hint', fingerprints)
        else:
            self.counters['deferred'] += 1
        try:
            await self.dispatch(account, credential, settings.omi_budget, now)
        except RateLimited as limited:
            await runtime.throttle(credential, limited.retry_after_s, runtime.clock(now))
        except Exception:
            self.counters['failed'] += 1
        return {item['id'] for item in items}

    async def contiguous(self, account, items, info, next_page):
        """May this page insert rows? A regular head walk ends at the first page holding a known row, so a row inserted here must not
        be what ends it: the page must already hold a known row, or be the last page. Otherwise more unseen conversations may lie
        behind it, and the regular head, which walks on, inserts them all. Holds by induction: a row inserted here was contiguous."""
        if info['list_truncated'] is not False:
            return False
        if next_page is None and info['has_more'] is False:
            return True
        for item in items:
            if await self.runtime.db('record', account, item['id']) is not None:
                return True
        return False

    async def dispatch(self, account, credential, budget, now):
        """Only the never-fetched class of the last 24 hours, reserved like any new conversation; no rotation of the other classes."""
        runtime = self.runtime
        items = await runtime.db('work_items', account, now, 8, lambda: runtime.current(account), False)
        for recording_id in [item[0] for item in items if item[1] and not item[2]][:HINT_WORK]:
            if not await runtime.reserve(credential, [('reads', budget.reads_per_hour), ('transcripts', budget.transcript_per_hour)],
                                         runtime.clock(now)):
                break
            await runtime.observe(account, recording_id, now)
