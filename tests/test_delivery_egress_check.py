"""The dispatcher's egress check and host rules (ANYM-534); local sockets only, no firewall is changed."""
import json
from pathlib import Path
import socket
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/delivery_egress_check.py'


def run(*args):
    result = subprocess.run([sys.executable, '-I', str(SCRIPT), *args], text=True, capture_output=True, timeout=30)
    return result.returncode, result.stdout


# spec: ANYM-534
def test_check_passes_only_when_open_targets_connect_and_blocked_ones_never_answer():
    with socket.socket() as closed:
        # A port nobody holds: every OS refuses it (a bound, non-listening one times out on macOS).
        closed.bind(('127.0.0.1', 0))
        refused_target = f'127.0.0.1:{closed.getsockname()[1]}'
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        open_target = f'127.0.0.1:{listener.getsockname()[1]}'
        # TEST-NET-1 has no route or no answer: what a DROP looks like from inside the dispatcher.
        code, out = run('check', '--timeout', '1', '--open', open_target, '--blocked', '192.0.2.1:8080')
        report = json.loads(out)
        assert code == 0 and report['passed'] is True
        assert [(r['target'], r['expect'], r['passed']) for r in report['results']] == [
            (open_target, 'open', True), ('192.0.2.1:8080', 'blocked', True)]
        assert report['results'][0]['outcome'] == 'connected' and report['results'][1]['outcome'] in ('timeout', 'unreachable')
        # A name sends no packet when it does not resolve, so a blocked target must be an IP literal: names and
        # malformed targets fail instead of passing as "nothing answered".
        code, out = run('check', '--timeout', '1', '--blocked', 'api.invalid:8080', '--blocked', 'api:8080',
                        '--blocked', '127.0.0.1:http')
        report = json.loads(out)
        assert code == 1 and [(r['outcome'], r['passed']) for r in report['results']] == [('malformed', False)] * 3
        # A refused connection proves the address answers: a DROP rule never sends a reset, so this fails.
        code, out = run('check', '--timeout', '1', '--blocked', refused_target, '--blocked', open_target)
        report = json.loads(out)
        assert code == 1 and report['passed'] is False
        assert [(r['outcome'], r['passed']) for r in report['results']] == [('refused', False), ('connected', False)]
    # An open target that does not connect fails too.
    code, out = run('check', '--timeout', '1', '--open', 'db.invalid:5432')
    assert code == 1 and json.loads(out)['results'][0]['outcome'] == 'unresolved'


# spec: ANYM-534
def test_dns_probes_need_an_answer_or_silence_and_the_listener_is_a_positive_control():
    import threading
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as resolver, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as silent:
        resolver.bind(('127.0.0.1', 0))
        silent.bind(('127.0.0.1', 0))
        def answer():
            query, peer = resolver.recvfrom(512)
            resolver.sendto(query[:2] + b'\x81\x80' + query[4:], peer)
        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        allowed, other = (f'127.0.0.1:{s.getsockname()[1]}' for s in (resolver, silent))
        code, out = run('check', '--timeout', '1', '--open-dns', allowed, '--blocked-dns', other)
        thread.join(5)
    assert code == 0, out
    assert [(r['expect'], r['outcome']) for r in json.loads(out)['results']] == [('open-dns', 'answered'), ('blocked-dns', 'timeout')]
    # A resolver that does not answer (the port is closed now) fails as open; a name is malformed.
    code, out = run('check', '--timeout', '1', '--open-dns', other, '--blocked-dns', 'resolver.invalid')
    results = json.loads(out)['results']
    assert code == 1 and results[0]['outcome'] in ('refused', 'timeout') and not results[0]['passed']
    assert (results[1]['outcome'], results[1]['passed']) == ('malformed', False)
    # The synthetic listener (run in the matcher container) accepts until its time is up.
    listener = subprocess.Popen([sys.executable, '-I', str(SCRIPT), 'listen', '--port', '0', '--seconds', '10'],
                                stdout=subprocess.PIPE, text=True)
    try:
        port = json.loads(listener.stdout.readline())['listening']
        code, out = run('check', '--timeout', '1', '--open', f'127.0.0.1:{port}')
        assert code == 0 and json.loads(out)['results'][0]['outcome'] == 'connected'
    finally:
        listener.kill()
        listener.wait(5)


# spec: ANYM-534
def test_rules_drop_private_tailnet_metadata_and_host_and_allow_only_dns_and_443():
    code, out = run('rules', '--resolver', '1.1.1.1')
    assert code == 0
    lines = out.splitlines()
    chain = [line for line in lines if line.startswith('iptables -A DEEDKIT-DELIVERY')]
    for network in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', '169.254.0.0/16', '127.0.0.0/8'):
        assert f'iptables -A DEEDKIT-DELIVERY -d {network} -j DROP' in chain, network
    dns = [line for line in chain if '--dport 53' in line]
    assert dns and all('-d 1.1.1.1' in line and line.endswith('-j RETURN') for line in dns)
    # DNS first, then the private drops, then public 443, then drop everything else.
    assert chain.index(dns[-1]) < chain.index('iptables -A DEEDKIT-DELIVERY -d 10.0.0.0/8 -j DROP')
    assert chain[-2:] == ['iptables -A DEEDKIT-DELIVERY -p tcp --dport 443 -j RETURN', 'iptables -A DEEDKIT-DELIVERY -j DROP']
    assert 'iptables -I DOCKER-USER -i dk-delivery0 -j DEEDKIT-DELIVERY' in lines
    # The host itself (INPUT, not forwarded) and every IPv6 packet (ULA, NAT64) are dropped.
    # The internal database bridge has a host gateway address too: host-bound packets drop on both bridges.
    for bridge in ('dk-delivery0', 'dk-deliverydb0'):
        assert f'iptables -I INPUT -i {bridge} -j DROP' in lines and f'ip6tables -I INPUT -i {bridge} -j DROP' in lines
    assert 'ip6tables -I DOCKER-USER -i dk-delivery0 -j DROP' in lines
    # The undo removes every inserted rule and the chain.
    code, out = run('rules', '--undo')
    assert code == 0
    undo = out.splitlines()
    inserted = [line.replace(' -I ', ' -D ', 1) for line in lines if ' -I ' in line]
    assert sorted(line for line in undo if ' -D ' in line) == sorted(inserted)
    assert undo[-2:] == ['iptables -F DEEDKIT-DELIVERY', 'iptables -X DEEDKIT-DELIVERY']
    # A resolver inside a dropped range (a tailnet DNS) is allowed only on port 53, before the drops.
    code, out = run('rules', '--resolver', '100.100.100.100')
    assert code == 0 and 'iptables -A DEEDKIT-DELIVERY -d 100.100.100.100 -p udp --dport 53 -j RETURN' in out
    code, _ = run('rules', '--resolver', 'not-an-address')
    assert code != 0


# spec: ANYM-581
def test_rules_also_drop_host_bound_packets_from_the_proxy_bridge():
    code, out = run('rules', '--resolver', '1.1.1.1')
    lines = out.splitlines()
    assert code == 0
    assert 'iptables -I INPUT -i dk-deliverypx0 -j DROP' in lines and 'ip6tables -I INPUT -i dk-deliverypx0 -j DROP' in lines
    code, out = run('rules', '--undo')
    undo = out.splitlines()
    assert code == 0
    assert 'iptables -D INPUT -i dk-deliverypx0 -j DROP' in undo and 'ip6tables -D INPUT -i dk-deliverypx0 -j DROP' in undo


# spec: ANYM-581
def test_proxied_probes_need_a_tunnel_or_the_proxys_refusal():
    import threading
    from messaging_service import egress_proxy
    upstream = socket.create_server(('127.0.0.1', 0))
    proxy = egress_proxy.Proxy(resolver=lambda host, port, slots=None: ['1.1.1.1'],
        connector=lambda address: socket.create_connection(upstream.getsockname(), timeout=2),
        policy=lambda: {'allow': ['hooks.example'], 'deny': [], 'allow_any_public': False}, log=lambda record: None)
    server = socket.create_server(('127.0.0.1', 0))
    threading.Thread(target=proxy.serve, args=(server,), daemon=True).start()
    address = '127.0.0.1:%d' % server.getsockname()[1]
    try:
        code, out = run('check', '--proxy', address, '--open-proxied', 'hooks.example', '--refused-proxied', 'other.example')
        assert code == 0, out
        assert [item['outcome'] for item in json.loads(out)['results']] == ['tunnel', 'refused']
        # Reversed expectations fail, and so does a proxied target with no proxy at all.
        code, out = run('check', '--proxy', address, '--open-proxied', 'other.example', '--refused-proxied', 'hooks.example')
        assert code == 1 and not any(item['passed'] for item in json.loads(out)['results'])
        code, out = run('check', '--open-proxied', 'hooks.example')
        assert code == 1 and json.loads(out)['results'][0]['outcome'] == 'no_proxy'
    finally:
        proxy.stop.set()
        server.close()
        upstream.close()


# spec: ANYM-581
def test_proxied_probe_reads_a_fragmented_body_whole_and_stops_at_its_deadline():
    import threading
    import time
    server = socket.create_server(('127.0.0.1', 0))
    stall = threading.Event()

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                request = conn.recv(4096)
                if request.startswith(b'RESOLVE'):
                    body = b'["1.1.1.1"]'
                    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n' % len(body) + body[:3])
                    if stall.is_set():
                        time.sleep(5)
                    else:
                        time.sleep(0.3)
                    conn.sendall(body[3:])
                else:
                    conn.sendall(b'HTTP/1.1 200 OK\r\n\r\n')

    threading.Thread(target=serve, daemon=True).start()
    address = '127.0.0.1:%d' % server.getsockname()[1]
    try:
        # The body arrives in two segments: the probe waits for all of it, then tunnels.
        code, out = run('check', '--proxy', address, '--open-proxied', 'hooks.example')
        assert code == 0 and json.loads(out)['results'][0]['outcome'] == 'tunnel', out
        # A body that stalls past the deadline fails the probe within the timeout, not after the stall.
        stall.set()
        started = time.monotonic()
        code, out = run('check', '--proxy', address, '--timeout', '1', '--open-proxied', 'hooks.example')
        assert code == 1 and json.loads(out)['results'][0]['outcome'] == 'unreachable', out
        assert time.monotonic() - started < 4
    finally:
        server.close()


# spec: ANYM-581
def test_unreached_targets_accept_a_local_refusal_but_never_a_connection():
    listener = socket.create_server(('127.0.0.1', 0))
    closed = socket.create_server(('127.0.0.1', 0))
    refused = '127.0.0.1:%d' % closed.getsockname()[1]
    closed.close()
    try:
        # An isolated bridge's subnet .1 refuses locally: --unreached passes where --blocked (a firewall DROP) fails.
        code, out = run('check', '--unreached', refused)
        assert code == 0 and json.loads(out)['results'][0]['outcome'] == 'refused', out
        code, out = run('check', '--blocked', refused)
        assert code == 1, out
        # A listener behind the address is reached: --unreached fails.
        code, out = run('check', '--unreached', '127.0.0.1:%d' % listener.getsockname()[1])
        assert code == 1 and json.loads(out)['results'][0]['outcome'] == 'connected', out
        code, out = run('check', '--unreached', 'localhost:22')
        assert code == 1 and json.loads(out)['results'][0]['outcome'] == 'malformed', out
    finally:
        listener.close()
