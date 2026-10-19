"""Owner authentication and bounded HTTP callbacks; media has its own protocol."""
import asyncio
import base64
import binascii
import hmac
import ipaddress
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse


class OwnerCredential:
    """Private, independent credential, re-read so atomic rotation takes effect."""

    def __init__(self, directory):
        self.path = Path(directory) / 'owner.token'
        if not self.path.exists():
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'w') as stream:
                    stream.write(secrets.token_urlsafe(32) + '\n')
                    stream.flush()
                    os.fsync(stream.fileno())
        self.read()  # Unsafe existing credentials are a startup error, never repaired silently.

    def read(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or
                    info.st_size not in {43, 44}):
                raise ValueError('Unsafe owner credential file')
            token = stream.read(128).strip()
        if not re.fullmatch(rb'[A-Za-z0-9_-]{43}', token):
            raise ValueError('Invalid owner credential')
        return token

    def accepts(self, authorization):
        try:
            scheme, value = authorization.split(' ', 1)
            if scheme.lower() == 'basic':
                user, value = base64.b64decode(value, validate=True).split(b':', 1)
                if user != b'owner':
                    return False
            elif scheme.lower() == 'bearer':
                value = value.encode('ascii')
            else:
                return False
            return hmac.compare_digest(value, self.read())
        except (OSError, ValueError, UnicodeError, binascii.Error):
            return False


MAPPED = ipaddress.ip_network('::ffff:0:0/96')


def trusted_peer(item):
    network = ipaddress.ip_network(item)
    if network.version == 6 and network.prefixlen >= 96 and network.subnet_of(MAPPED):
        # An IPv4-mapped entry names the IPv4 peer it maps.
        return ipaddress.ip_network(f'{network.network_address.ipv4_mapped}/{network.prefixlen - 96}')
    return network


def trusted_peers(value):
    """VOICE_TRUSTED_PEERS: comma-separated IP addresses or networks; an invalid entry stops startup."""
    try:
        return tuple(trusted_peer(item.strip()) for item in (value or '').split(',') if item.strip())
    except ValueError:
        raise ValueError('Invalid VOICE_TRUSTED_PEERS') from None


@dataclass(frozen=True)
class CallbackLimits:
    body_bytes: int = 64 * 1024
    read_seconds: float = 5.0
    concurrent: int = 8
    fields: int = 64
    key_chars: int = 128
    value_chars: int = 4096


class VoiceBoundary:
    def __init__(self, app, *, owner, control_token, outbound, limits=None, peers=()):
        self.app, self.owner = app, owner
        self.control_token, self.outbound = control_token, outbound
        self.limits = limits or CallbackLimits()
        self.peers = tuple(peers)
        self.pending_callbacks = 0

    def local_peer(self, scope):
        """The transport peer, never a proxy header: loopback or an explicitly trusted host-side peer."""
        try:
            peer = ipaddress.ip_address(scope['client'][0])
        except (KeyError, TypeError, IndexError, ValueError):
            return False
        peer = getattr(peer, 'ipv4_mapped', None) or peer  # trusted_peers maps entries the same way
        return peer.is_loopback or any(peer in network for network in self.peers)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            # Twilio WebSocket signature, attempt binding and audio budgets live in app.py.
            return await self.app(scope, receive, send)

        async def secured_send(message):
            if message['type'] == 'http.response.start':
                headers = MutableHeaders(scope=message)
                headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                                'Referrer-Policy': 'no-referrer'})
            await send(message)

        async def reject(status, detail, headers=None):
            await JSONResponse({'detail': detail}, status_code=status, headers=headers)(scope, receive, secured_send)

        request = Request(scope)
        if len(request.headers.getlist('host')) != 1:
            return await reject(400, 'Invalid Host')
        try:
            hostname = request.url.hostname
        except ValueError:
            return await reject(400, 'Invalid Host')
        path = scope['path']
        if path.startswith('/provider/'):
            provider = self.outbound.providers.get('twilio')
            expected = urlsplit(provider.config.get('public_url', '')).hostname if provider else None
            if not expected or hostname != expected:
                return await reject(403, 'Provider host required')
            if path != '/provider/twilio/inbound' and not re.fullmatch(r'/provider/twilio/status/[^/]+', path):
                return await reject(404, 'Unknown callback')
            if request.method != 'POST':
                return await reject(405, 'POST required', {'Allow': 'POST'})
            signatures = request.headers.getlist('x-twilio-signature')
            if len(signatures) != 1 or not re.fullmatch(r'[A-Za-z0-9+/]{27}=', signatures[0]):
                return await reject(403, 'Provider signature required')
            if scope.get('query_string'):
                return await reject(400, 'Callback query not supported')
            content_types = request.headers.getlist('content-type')
            if (len(content_types) != 1 or
                    content_types[0].split(';', 1)[0].strip().lower() != 'application/x-www-form-urlencoded'):
                return await reject(415, 'URL-encoded form required')
            lengths = request.headers.getlist('content-length')
            if len(lengths) > 1 or (lengths and not re.fullmatch(r'[0-9]{1,20}', lengths[0])):
                return await reject(400, 'Invalid Content-Length')
            if lengths and int(lengths[0]) > self.limits.body_bytes:
                return await reject(413, 'Callback too large')
            # No await between check and increment: one event-loop worker owns this budget.
            if self.pending_callbacks >= self.limits.concurrent:
                return await reject(429, 'Callback capacity exceeded', {'Retry-After': '1'})
            self.pending_callbacks += 1
            try:
                body = bytearray()
                try:
                    async with asyncio.timeout(self.limits.read_seconds):
                        while True:
                            message = await receive()
                            if message['type'] == 'http.disconnect':
                                return
                            chunk = message.get('body', b'')
                            if len(body) + len(chunk) > self.limits.body_bytes:
                                return await reject(413, 'Callback too large')
                            body.extend(chunk)
                            if not message.get('more_body', False):
                                break
                except TimeoutError:
                    return await reject(408, 'Callback read timeout')
                if lengths and int(lengths[0]) != len(body):
                    return await reject(400, 'Content-Length mismatch')
                try:
                    text = body.decode('utf-8', errors='strict')
                    if re.search(r'%(?![0-9a-fA-F]{2})', text):
                        raise ValueError('Invalid percent encoding')
                    pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True,
                                      errors='strict', max_num_fields=self.limits.fields)
                    params = dict(pairs)
                    if (len(params) != len(pairs) or
                            any(not key or len(key) > self.limits.key_chars or len(value) > self.limits.value_chars
                                for key, value in pairs)):
                        raise ValueError('Invalid fields')
                except (ValueError, UnicodeError):
                    return await reject(400, 'Invalid callback form')
                # Retain every field for the provider's exact-URL HMAC verification.
                scope.setdefault('state', {})['twilio_form'] = params
                return await self.app(scope, receive, secured_send)
            finally:
                self.pending_callbacks -= 1

        if hostname not in {'127.0.0.1', 'localhost', 'testserver'}:
            return await reject(403, 'Loopback host required')
        if scope.get('root_path'):
            # Routing strips a mount prefix the boundary would not see; the supported entry point has none.
            return await reject(404, 'Unsupported root path')
        # Only an exact /v1/<operation> is the agent route: a dot segment would reach the panel's static files.
        agent = re.fullmatch(r'/v1/[A-Za-z0-9_]+', path) is not None
        if not agent and not self.local_peer(scope):
            return await reject(403, 'Local peer required')
        authorization = request.headers.getlist('authorization')
        credential = authorization[0] if len(authorization) == 1 else ''
        if path == '/control' or path.startswith('/control/'):
            if not hmac.compare_digest(credential.encode('utf-8'), ('Bearer ' + self.control_token).encode('utf-8')):
                return await reject(401, 'Control credential required')
        elif agent:
            pass  # AgentAPI authenticates its own principal and bounds its request body.
        else:
            if not self.owner.accepts(credential):
                return await reject(401, 'Owner credential required', {'WWW-Authenticate': 'Basic realm="Voice owner"'})
            if request.method not in {'GET', 'HEAD'}:
                if (request.headers.getlist('origin') != [str(request.base_url).rstrip('/')] or
                        request.headers.getlist('x-voice-service') != ['1']):
                    return await reject(403, 'Local interface required')
        await self.app(scope, receive, secured_send)
