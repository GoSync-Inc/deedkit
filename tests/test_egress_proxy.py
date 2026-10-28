"""The delivery egress proxy and the dispatcher's proxy transports (ANYM-581). Loopback only: the proxy's resolver
and upstream connector are synthetic, so no packet leaves the host and no live provider is touched."""
import json
import socket
import threading
import time

import pytest

from messaging_service import delivery_worker, egress_proxy
from messaging_service.egress import EgressNotSent, proxy_lookup, proxy_transport
from messaging_service.event_delivery import OPERATOR_DNS_SLOTS, _transport

PUBLIC = {'hooks.example': ['1.1.1.1', '8.8.8.8'], 'other.example': ['9.9.9.9'], 'inside.example': ['10.0.0.5']}


class Upstream:
    """A loopback listener standing in for every public address; records what each connection received."""
    def __init__(self, close_after_accept=False):
        self.server = socket.create_server(('127.0.0.1', 0))
        self.received, self.accepted, self.close_after_accept = [], [], close_after_accept
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            self.accepted.append(sock)
            if self.close_after_accept:
                sock.close()
                continue
            def read(sock=sock):
                data = b''
                try:
                    while part := sock.recv(4096):
                        data += part
                except OSError:
                    pass
                self.received.append(data)
            threading.Thread(target=read, daemon=True).start()

    def connect(self, address):
        self.pinned = address
        return socket.create_connection(self.server.getsockname(), timeout=2)


@pytest.fixture
def proxy_env():
    policy = {'allow': ['hooks.example', 'inside.example'], 'deny': [], 'allow_any_public': False}
    calls, records, upstream = [], [], Upstream()
    def resolver(host, port, *, slots=None):
        calls.append((host, port, slots))
        if host not in PUBLIC:
            raise OSError('receiver_dns_unavailable')
        return PUBLIC[host]
    proxy = egress_proxy.Proxy(resolver=resolver, connector=upstream.connect, policy=lambda: policy,
                               tunnel_max_s=0.6, tunnels={'operator': 1, 'endpoint': 1}, log=records.append)
    server = socket.create_server(('127.0.0.1', 0))
    threading.Thread(target=proxy.serve, args=(server,), daemon=True).start()
    address = '127.0.0.1:%d' % server.getsockname()[1]
    yield {'proxy': proxy, 'address': address, 'policy': policy, 'calls': calls, 'records': records, 'upstream': upstream}
    proxy.stop.set()
    server.close()
    upstream.server.close()


def raw(address, head, wait=2):
    host, _, port = address.rpartition(':')
    sock = socket.create_connection((host, int(port)), timeout=wait)
    sock.sendall(head.encode('ascii'))
    return sock


def read_head(sock):
    data = b''
    while b'\r\n\r\n' not in data:
        part = sock.recv(1)
        if not part:
            break
        data += part
    return data.decode('ascii')


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# spec: ANYM-581
def test_resolve_returns_the_proxys_public_answers_for_an_allowed_host_and_operator_lookups_use_their_own_slots(proxy_env):
    assert proxy_lookup(proxy_env['address'])('hooks.example', 443) == ['1.1.1.1', '8.8.8.8']
    proxy_env['proxy'].cache.clear()
    assert proxy_lookup(proxy_env['address'], lane='operator')('hooks.example', 443) == ['1.1.1.1', '8.8.8.8']
    assert [slots for *_, slots in proxy_env['calls']] == [None, OPERATOR_DNS_SLOTS]


# spec: ANYM-581
def test_hosts_outside_the_policy_ports_other_than_443_and_non_canonical_hosts_are_refused_before_any_dns(proxy_env):
    lookup = proxy_lookup(proxy_env['address'])
    for host, port in (('other.example', 443), ('hooks.example', 80), ('Hooks.Example', 443)):
        with pytest.raises(EgressNotSent, match='policy_refused'):
            lookup(host, port)
    transport = proxy_transport(proxy_env['address'])
    with pytest.raises(EgressNotSent, match='policy_refused'):
        transport('https://other.example/', b'{}', {}, resolved=('other.example', ['9.9.9.9'], '/'))
    assert proxy_env['calls'] == [] and proxy_env['upstream'].accepted == []


# spec: ANYM-581
def test_a_private_answer_is_an_unsafe_endpoint_and_is_never_connected(proxy_env):
    with pytest.raises(ValueError, match='unsafe_receiver'):
        proxy_lookup(proxy_env['address'])('inside.example', 443)
    with pytest.raises(ValueError, match='unsafe_receiver'):
        proxy_transport(proxy_env['address'])('https://inside.example/', b'{}', {},
                                              resolved=('inside.example', ['10.0.0.5'], '/'))
    assert proxy_env['upstream'].accepted == []


# spec: ANYM-581
def test_connect_pins_only_an_address_from_the_proxys_own_resolution(proxy_env):
    sock = raw(proxy_env['address'], 'CONNECT hooks.example:443 HTTP/1.1\r\nX-DeedKit-Address: 10.0.0.5\r\n\r\n')
    assert read_head(sock).startswith('HTTP/1.1 403 address_not_resolved')
    sock.close()
    sock = raw(proxy_env['address'], 'CONNECT hooks.example:443 HTTP/1.1\r\nX-DeedKit-Address: 8.8.8.8\r\n\r\n')
    assert read_head(sock).startswith('HTTP/1.1 200 ok')
    assert proxy_env['upstream'].pinned == '8.8.8.8' and len(proxy_env['upstream'].accepted) == 1
    sock.close()


# spec: ANYM-581
def test_a_policy_change_refuses_the_next_connect_and_an_established_tunnel_ends_at_its_lifetime_cap(proxy_env):
    tunnel = raw(proxy_env['address'], 'CONNECT hooks.example:443 HTTP/1.1\r\nX-DeedKit-Address: 1.1.1.1\r\n\r\n')
    assert read_head(tunnel).startswith('HTTP/1.1 200 ok')
    tunnel.sendall(b'synthetic-bytes')
    proxy_env['policy']['allow'] = ['inside.example']
    # The endpoint lane's one tunnel is held; the operator lane shows the refusal.
    with pytest.raises(EgressNotSent, match='policy_refused'):
        proxy_lookup(proxy_env['address'], lane='operator')('hooks.example', 443)
    started = time.monotonic()
    tunnel.settimeout(3)
    assert tunnel.recv(16) == b''
    assert time.monotonic() - started < 2
    tunnel.close()
    assert wait_for(lambda: any(item.get('ended') == 'lifetime_cap' for item in proxy_env['records']))
    assert wait_for(lambda: proxy_env['upstream'].received == [b'synthetic-bytes'])


# spec: ANYM-581
def test_tunnels_are_bounded_per_lane_and_logs_carry_no_host_or_address(proxy_env):
    held = raw(proxy_env['address'], 'CONNECT hooks.example:443 HTTP/1.1\r\nX-DeedKit-Address: 1.1.1.1\r\n\r\n')
    assert read_head(held).startswith('HTTP/1.1 200 ok')
    with pytest.raises(EgressNotSent, match='busy'):
        proxy_lookup(proxy_env['address'])('hooks.example', 443)
    # The operator lane has its own bound, so a held endpoint tunnel never blocks it.
    assert proxy_lookup(proxy_env['address'], lane='operator')('hooks.example', 443) == ['1.1.1.1', '8.8.8.8']
    held.close()
    assert wait_for(lambda: len(proxy_env['records']) >= 3)
    logged = json.dumps(proxy_env['records'])
    assert all(secret not in logged for secret in ('hooks.example', '1.1.1.1', '8.8.8.8'))
    assert {item['status'] for item in proxy_env['records']} == {200, 503}


# spec: ANYM-581
def test_failures_before_the_first_request_byte_are_not_sent_on_both_transports(proxy_env):
    closed = socket.create_server(('127.0.0.1', 0))
    unused = '127.0.0.1:%d' % closed.getsockname()[1]
    closed.close()
    with pytest.raises(EgressNotSent):
        proxy_transport(unused)('https://hooks.example/', b'{}', {}, resolved=('hooks.example', ['1.1.1.1'], '/'))
    with pytest.raises(OSError, match='receiver_dns_unavailable'):
        proxy_lookup(unused)('hooks.example', 443)
    # The upstream closes during the TLS handshake: no request byte was written, so the hint retries.
    proxy_env['proxy'].connector = Upstream(close_after_accept=True).connect
    with pytest.raises(EgressNotSent, match='receiver_tls_failed'):
        proxy_transport(proxy_env['address'])('https://hooks.example/', b'{"hint":1}', {},
                                              resolved=('hooks.example', ['1.1.1.1'], '/'))
    with pytest.raises(EgressNotSent):
        _transport('https://hooks.example/', b'{}', {}, resolved=('hooks.example', [unused.split(':')[0]], '/'),
                   timeout=1)


# spec: ANYM-581
def test_the_dispatcher_with_a_proxy_sends_every_lane_through_it(monkeypatch):
    seen = []
    def fake_dispatch(store, **kwargs):
        seen.append((kwargs.get('lane'), kwargs['transport'], kwargs['resolver']))
        return {}
    monkeypatch.setattr(delivery_worker, 'dispatch_hints', fake_dispatch)
    monkeypatch.setattr(delivery_worker, 'endpoint_tests', lambda store, **kwargs: seen.append(
        ('tests', kwargs['transport'], kwargs['resolver'])))
    monkeypatch.setattr(delivery_worker, 'pulse', lambda *a, **k: True)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    delivery_worker.run(object(), stop, interval=0.05, proxy='delivery-egress:3128')
    assert {lane for lane, *_ in seen} == {'operator', 'endpoint', 'tests'}
    for lane, transport, resolver in seen:
        assert transport.__qualname__ == 'proxy_transport.<locals>.transport', lane
        assert resolver.__qualname__ == 'proxy_lookup.<locals>.lookup', lane
        assert transport is not _transport
