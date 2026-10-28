"""Delivery egress proxy (ANYM-581): the dispatcher's only way out, in its own container with no database.

The dispatcher joins only internal networks; this process alone joins the egress bridge. On :3128 it accepts
`RESOLVE host:443` (200 and a JSON list of addresses) and `CONNECT host:443` with `X-DeedKit-Address: <ip>` (200 and
a raw tunnel). Both need port 443, a canonical host allowed by DELIVERY_POLICY_FILE (read per request, so a policy
change applies to the next request) and answers from this process's own DNS that are all public; a CONNECT also
needs the pinned address to be one of those answers. Tunnels are bounded per lane and end at TUNNEL_MAX_S. Logs
carry the method, status, reason and lane, never a host, address or URL.
"""
from __future__ import annotations

import ipaddress
import json
import os
import selectors
import signal
import socket
import threading
import time

from .delivery_endpoints import canonical_host, host_allowed, load_policy
from .event_delivery import OPERATOR_DNS_SLOTS, _addresses, _refused

TUNNEL_MAX_S = 30
HEAD_TIMEOUT_S = 5
CONNECT_TIMEOUT_S = 5
HEAD_LIMIT = 4096
# The dispatcher's RESOLVE and CONNECT of one attempt are seconds apart; a CONNECT reuses this process's answer.
CACHE_S = 60
TUNNELS = {'operator': 4, 'endpoint': 12}
HANDLERS = 64


class Refused(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


def _connect(address):
    return socket.create_connection((address, 443), timeout=CONNECT_TIMEOUT_S)


def _read_head(client):
    data = b''
    while b'\r\n\r\n' not in data:
        part = client.recv(512)
        if not part:
            raise Refused(400, 'bad_request')
        data += part
        if len(data) > HEAD_LIMIT:
            raise Refused(400, 'bad_request')
    head, _, rest = data.partition(b'\r\n\r\n')
    if rest:
        # The client waits for 200 before its first tunnel byte.
        raise Refused(400, 'bad_request')
    try:
        lines = head.decode('ascii').split('\r\n')
    except UnicodeDecodeError:
        raise Refused(400, 'bad_request') from None
    parts = lines[0].split(' ')
    if len(parts) != 3 or parts[0] not in ('RESOLVE', 'CONNECT') or parts[2] != 'HTTP/1.1':
        raise Refused(400, 'bad_request')
    headers = {}
    for line in lines[1:]:
        name, colon, value = line.partition(':')
        if not colon:
            raise Refused(400, 'bad_request')
        headers[name.strip().lower()] = value.strip()
    return parts[0], parts[1], headers


def _reply(client, status, reason, body=b''):
    head = f'HTTP/1.1 {status} {reason}\r\n'
    if body:
        head += f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n'
    client.sendall(head.encode('ascii') + b'\r\n' + body)


class Proxy:
    def __init__(self, *, resolver=_addresses, connector=_connect, policy=load_policy, tunnel_max_s=TUNNEL_MAX_S,
                 tunnels=None, log=None):
        self.resolver, self.connector, self.policy, self.tunnel_max_s = resolver, connector, policy, tunnel_max_s
        self.tunnels = {lane: threading.BoundedSemaphore(n) for lane, n in (tunnels or TUNNELS).items()}
        self.handlers = threading.BoundedSemaphore(HANDLERS)
        self.cache, self.cache_lock = {}, threading.Lock()
        self.log = log or (lambda record: print(json.dumps(record), flush=True))
        self.stop = threading.Event()

    def answers(self, host, lane):
        now = time.monotonic()
        with self.cache_lock:
            cached = self.cache.get(host)
            if cached and cached[0] > now:
                return cached[1]
        try:
            addresses = self.resolver(host, 443, slots=OPERATOR_DNS_SLOTS if lane == 'operator' else None)
        except OSError:
            raise Refused(502, 'dns_unavailable') from None
        try:
            if not addresses or len(addresses) > 32:
                raise ValueError
            addresses = sorted({str(ipaddress.ip_address(item)) for item in addresses})
            if any(_refused(ipaddress.ip_address(item)) for item in addresses):
                raise ValueError
        except ValueError:
            raise Refused(403, 'unsafe_address') from None
        with self.cache_lock:
            if len(self.cache) > 1024:
                self.cache.clear()
            self.cache[host] = (now + CACHE_S, addresses)
        return addresses

    def admit(self, target):
        host, _, port = target.rpartition(':')
        if port != '443':
            raise Refused(403, 'policy_refused')
        try:
            if canonical_host(host) != host:
                raise ValueError
        except ValueError:
            raise Refused(403, 'policy_refused') from None
        if not host_allowed(self.policy(), host):
            raise Refused(403, 'policy_refused')
        return host

    def handle(self, client):
        record: dict[str, object] = {'egress': 'unknown', 'lane': 'endpoint'}
        try:
            client.settimeout(HEAD_TIMEOUT_S)
            try:
                method, target, headers = _read_head(client)
                record['egress'] = method.lower()
                record['lane'] = lane = 'operator' if headers.get('x-deedkit-lane') == 'operator' else 'endpoint'
                gate = self.tunnels[lane]
                if not gate.acquire(blocking=False):
                    raise Refused(503, 'busy')
                try:
                    host = self.admit(target)
                    addresses = self.answers(host, lane)
                    if method == 'RESOLVE':
                        _reply(client, 200, 'ok', json.dumps(addresses).encode())
                        record['status'] = 200
                        return
                    try:
                        pinned = str(ipaddress.ip_address(headers.get('x-deedkit-address', '')))
                    except ValueError:
                        raise Refused(400, 'bad_request') from None
                    if pinned not in addresses:
                        raise Refused(403, 'address_not_resolved')
                    try:
                        upstream = self.connector(pinned)
                    except OSError:
                        raise Refused(502, 'connect_failed') from None
                    with upstream:
                        _reply(client, 200, 'ok')
                        record['status'] = 200
                        record['ended'] = self.splice(client, upstream)
                finally:
                    gate.release()
            except Refused as refusal:
                record.update(status=refusal.status, reason=refusal.reason)
                _reply(client, refusal.status, refusal.reason)
        except Exception as error:
            record.update(status=0, reason='client_failed', **{'class': type(error).__name__})
        finally:
            client.close()
            self.log(record)

    def splice(self, client, upstream):
        """Copy both ways until either side closes or the tunnel's lifetime cap; how it ended."""
        deadline = time.monotonic() + self.tunnel_max_s
        peers = {client: upstream, upstream: client}
        with selectors.DefaultSelector() as selector:
            for sock in peers:
                selector.register(sock, selectors.EVENT_READ, sock)
            while (left := deadline - time.monotonic()) > 0 and not self.stop.is_set():
                for key, _ in selector.select(min(left, 1)):
                    source = key.data
                    data = source.recv(65536)
                    if not data:
                        return 'closed'
                    peers[source].settimeout(max(0.1, deadline - time.monotonic()))
                    peers[source].sendall(data)
        return 'lifetime_cap'

    def serve(self, server):
        server.settimeout(1)
        while not self.stop.is_set():
            try:
                client, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                if self.stop.is_set():
                    break
                continue
            if not self.handlers.acquire(blocking=False):
                client.close()
                continue
            def run(client=client):
                try:
                    self.handle(client)
                finally:
                    self.handlers.release()
            threading.Thread(target=run, daemon=True, name='deedkit-egress').start()


def main():
    port = int(os.environ.get('DELIVERY_EGRESS_PORT', '3128'))
    proxy = Proxy()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: proxy.stop.set())
    with socket.create_server(('', port), backlog=64) as server:
        print(json.dumps({'egress': 'listening', 'port': server.getsockname()[1]}), flush=True)
        proxy.serve(server)


if __name__ == '__main__':
    main()
