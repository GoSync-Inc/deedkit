"""Dispatcher side of the delivery egress proxy (ANYM-581); stdlib only, no package imports.

The dispatcher joins only internal networks, so its one way out is the `delivery-egress` proxy
(messaging_service.egress_proxy). Every failure before the first request byte is EgressNotSent: the hint was not
sent and retries. After the request starts, OSError, TimeoutError and ValueError keep their meaning (maybe sent).
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import ipaddress
import json
import math
import re
import socket
import ssl
import threading
import time

LOOKUP_TIMEOUT_S = 2
HEAD_LIMIT = 4096
_LOOKUP_SLOTS = threading.BoundedSemaphore(4)
# Resolving the proxy's own name (Docker's embedded DNS) may stall too: bounded threads, the operator lane apart.
_PROXY_DNS_SLOTS = {'operator': threading.BoundedSemaphore(2), 'endpoint': threading.BoundedSemaphore(4)}


class EgressNotSent(OSError):
    """Nothing of the request left the process: the attempt is retryable, never a maybe-sent."""


def deadline_after(timeout):
    deadline = time.monotonic() + timeout
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError
        return value
    return remaining


def post(connection, host, path, body, headers, remaining, *, retry_after=False):
    """One HTTP/1.1 POST on an open TLS connection; the status code. Raises OSError/ValueError once sending began.

    With retry_after, (status, seconds): a 429 or 503's Retry-After delay (ANYM-604), else None."""
    request = ('POST ' + path + ' HTTP/1.1\r\nHost: ' + ('[' + host + ']' if ':' in host else host) + '\r\nConnection: close\r\n' +
        'Content-Length: ' + str(len(body)) + '\r\n' +
        ''.join(key + ': ' + value + '\r\n' for key, value in headers.items()) + '\r\n').encode('ascii')
    try:
        connection.settimeout(remaining())
    except TimeoutError as error:
        # The deadline passed before the first request byte.
        raise EgressNotSent('receiver_deadline') from error
    connection.sendall(request + body)
    data, total = b'', 0
    def receive(marker):
        nonlocal data, total
        while marker not in data:
            connection.settimeout(remaining())
            part = connection.recv(256)
            total += len(part)
            if not part or total > HEAD_LIMIT:
                raise ValueError('invalid_receiver_response')
            data += part
    while True:
        receive(b'\r\n')
        match = re.fullmatch(rb'HTTP/1\.[01] ([0-9]{3})(?: [\x20-\x7e]*)?', data.split(b'\r\n', 1)[0])
        if match is None:
            raise ValueError('invalid_receiver_response')
        status = int(match[1])
        if not 100 <= status < 200 or status == 101:
            if not retry_after:
                return status
            seconds = None
            if status in (429, 503):
                try:
                    receive(b'\r\n\r\n')
                    seconds = retry_after_seconds(data.split(b'\r\n\r\n', 1)[0])
                except (OSError, ValueError):
                    # A head cut short, too long or past the deadline: the dispatcher keeps its own backoff.
                    pass
            return status, seconds
        # An interim response (100 Continue, 103 Early Hints): skip its head; the final status follows it.
        receive(b'\r\n\r\n')
        data = data.split(b'\r\n\r\n', 1)[1]


def retry_after_seconds(head, now=None):
    """The delay of a response head's one Retry-After in whole seconds (RFC 9110 section 10.2.3: delta-seconds or an
    HTTP-date), or None when it is missing, repeated, unparsable or not in the future."""
    values = [line.split(b':', 1)[1].strip() for line in head.split(b'\r\n')[1:]
        if b':' in line and line.split(b':', 1)[0].strip().lower() == b'retry-after']
    if len(values) != 1:
        return None
    value = values[0].decode('ascii', 'replace')
    if re.fullmatch('[0-9]+', value):
        # Any delay past the dispatcher's cap is the cap; a long digit run never reaches int().
        return int(value) if len(value) <= 9 else 10**9
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = math.ceil((when - (now or datetime.now(timezone.utc))).total_seconds())
    return seconds if seconds > 0 else None


def answer(value):
    """A transport's result as (status, Retry-After seconds or None); a bare status (test transports) has none."""
    return value if type(value) is tuple and len(value) == 2 else (value, None)


def _proxy_address(proxy, lane, remaining):
    """(ip, port) of the proxy within the deadline; the lookup runs in a bounded thread like _addresses."""
    host, _, port = proxy.rpartition(':')
    if not host or not port.isdigit():
        raise OSError('invalid_egress_proxy')
    try:
        return str(ipaddress.ip_address(host)), int(port)
    except ValueError:
        pass
    slots = _PROXY_DNS_SLOTS['operator' if lane == 'operator' else 'endpoint']
    if not slots.acquire(blocking=False):
        raise OSError('egress_proxy_dns_busy')
    finished, result = threading.Event(), {}
    def lookup():
        try:
            result['address'] = socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)[0][4][:2]
        except OSError:
            pass
        finally:
            slots.release()
            finished.set()
    try:
        threading.Thread(target=lookup, daemon=True, name='deedkit-egress-dns').start()
    except BaseException:
        slots.release()
        raise
    if not finished.wait(min(remaining(), LOOKUP_TIMEOUT_S)) or 'address' not in result:
        raise OSError('egress_proxy_dns_unavailable')
    return result['address']


def _request(proxy, head, remaining, lane=None):
    """Open a proxy connection, send the head and read the response head (one byte at a time, so a tunnel's first
    byte is never consumed). (socket, status, reason, headers); the caller closes the socket."""
    sock = socket.create_connection(_proxy_address(proxy, lane, remaining), timeout=remaining())
    try:
        sock.settimeout(remaining())
        sock.sendall(head.encode('ascii'))
        data = b''
        while not data.endswith(b'\r\n\r\n'):
            sock.settimeout(remaining())
            part = sock.recv(1)
            if not part or len(data) >= HEAD_LIMIT:
                raise OSError('egress_proxy_closed')
            data += part
        lines = data.decode('ascii').split('\r\n')
        match = re.fullmatch(r'HTTP/1\.[01] ([0-9]{3}) ([a-z_]+)', lines[0])
        if match is None:
            raise OSError('egress_proxy_invalid')
        headers = {}
        for line in lines[1:]:
            if line:
                name, _, value = line.partition(':')
                headers[name.strip().lower()] = value.strip()
        return sock, int(match[1]), match[2], headers
    except BaseException:
        sock.close()
        raise


def _refusal(status, reason):
    # The proxy found the host or address unsafe: a dead letter, like a private DNS answer without the proxy.
    if status == 403 and reason == 'unsafe_address':
        raise ValueError('unsafe_receiver')
    raise EgressNotSent('egress_' + reason)


def _lane_header(lane):
    return 'X-DeedKit-Lane: operator\r\n' if lane == 'operator' else ''


def proxy_lookup(proxy, *, lane=None):
    """A resolver like event_delivery._addresses that asks the proxy: its own DNS answers, which it also checks."""
    def lookup(host, port, *, slots=None):
        slots = _LOOKUP_SLOTS if slots is None else slots
        if not slots.acquire(blocking=False):
            raise OSError('receiver_dns_busy')
        try:
            remaining = deadline_after(LOOKUP_TIMEOUT_S + 1)
            try:
                sock, status, reason, headers = _request(proxy, f'RESOLVE {host}:{port} HTTP/1.1\r\n'
                    f'Host: {host}:{port}\r\n{_lane_header(lane)}\r\n', remaining, lane)
            except (OSError, UnicodeError):
                raise OSError('receiver_dns_unavailable') from None
            with sock:
                if status != 200:
                    _refusal(status, reason)
                length = headers.get('content-length', '')
                if not length.isdigit() or int(length) > HEAD_LIMIT:
                    raise OSError('receiver_dns_unavailable')
                body = b''
                while len(body) < int(length):
                    sock.settimeout(remaining())
                    part = sock.recv(int(length) - len(body))
                    if not part:
                        raise OSError('receiver_dns_unavailable')
                    body += part
            try:
                addresses = json.loads(body)
                if not isinstance(addresses, list) or len(addresses) > 32 or not all(type(item) is str for item in addresses):
                    raise ValueError
                return sorted({str(ipaddress.ip_address(item)) for item in addresses})
            except ValueError:
                # A malformed proxy answer is an unavailable resolver, never an unsafe endpoint.
                raise OSError('receiver_dns_unavailable') from None
        except TimeoutError:
            raise OSError('receiver_dns_unavailable') from None
        finally:
            slots.release()
    return lookup


def proxy_transport(proxy, *, lane=None):
    """A transport like event_delivery._transport through a CONNECT tunnel pinned to the validated address."""
    def transport(endpoint, body, headers, *, resolved, timeout=5):
        host, addresses, path = resolved
        remaining = deadline_after(timeout)
        try:
            sock, status, reason, _ = _request(proxy, f'CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n'
                f'X-DeedKit-Address: {addresses[0]}\r\n{_lane_header(lane)}\r\n', remaining, lane)
        except (OSError, UnicodeError) as error:
            raise EgressNotSent('egress_proxy_unavailable') from error
        with sock:
            if status != 200:
                _refusal(status, reason)
            try:
                sock.settimeout(remaining())
                connection = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            except OSError as error:
                # TLS handshake or certificate: nothing was sent (ssl errors are OSError; cert ones also ValueError).
                raise EgressNotSent('receiver_tls_failed') from error
            with connection:
                return post(connection, host, path, body, headers, remaining, retry_after=True)
    return transport
