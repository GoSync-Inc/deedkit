"""The mini-facing enrichment worker's host rules and egress check (ANYM-688); local sockets only, no firewall is changed."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/inference_egress_check.py'
MINI = '100.101.102.103'
CHAIN = 'DEEDKIT-INFERENCE'


def run(*args):
    result = subprocess.run([sys.executable, '-I', str(SCRIPT), *args], text=True, capture_output=True, timeout=30)
    return result.returncode, result.stdout, result.stderr


def rules(*extra):
    code, out, err = run('rules', '--mini-ip', MINI, *extra)
    assert code == 0, err
    return out.splitlines()


def chain_rules(lines):
    return [line for line in lines if line.startswith(f'iptables -A {CHAIN} ')]


# spec: ANYM-688
def test_egress_rules_allow_exactly_the_mini_on_443_and_drop_everything_else():
    lines = rules()
    chain = chain_rules(lines)
    # The one allow comes first; everything after it is a drop, ending in a catch-all.
    assert chain[0] == f'iptables -A {CHAIN} -d {MINI} -p tcp --dport 443 -j RETURN'
    assert all(line.endswith('-j DROP') for line in chain[1:]) and chain[-1] == f'iptables -A {CHAIN} -j DROP'
    assert not any('ACCEPT' in line for line in lines)
    # The ranges the card names are dropped explicitly: RFC 1918, link-local with metadata, the tailnet, loopback.
    for network in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '100.64.0.0/10', '127.0.0.0/8'):
        assert f'iptables -A {CHAIN} -d {network} -j DROP' in chain, network
    # Only the egress bridge is sent through the chain; host-bound packets and all IPv6 are dropped.
    assert f'iptables -I DOCKER-USER -i dk-inference0 -j {CHAIN}' in lines
    assert 'iptables -I INPUT -i dk-inference0 -j DROP' in lines
    assert 'ip6tables -I DOCKER-USER -i dk-inference0 -j DROP' in lines
    assert 'ip6tables -I INPUT -i dk-inference0 -j DROP' in lines
    # The chain exists before it is filled and is emptied first, so a second run never doubles the rules.
    assert lines.index(f'iptables -N {CHAIN}') < lines.index(f'iptables -F {CHAIN}') < lines.index(chain[0])
    # The same chain also hangs in `raw` PREROUTING, which runs before the filter FORWARD chain: a Tailscale
    # `ts-forward` accept placed ahead of DOCKER-USER (or re-placed after a tailscaled restart) cannot pass it.
    raw = [line.replace('iptables -t raw ', 'iptables ') for line in lines if line.startswith('iptables -t raw -A ')]
    assert raw == chain
    assert f'iptables -t raw -I PREROUTING -i dk-inference0 -j {CHAIN}' in lines
    assert 'ip6tables -t raw -I PREROUTING -i dk-inference0 -j DROP' in lines
    assert lines.index(f'iptables -t raw -N {CHAIN}') < lines.index(f'iptables -t raw -I PREROUTING -i dk-inference0 -j {CHAIN}')
    assert sum(' -j RETURN' in line for line in lines) == 2


# spec: ANYM-688
def test_egress_rules_drop_dns_and_the_db_bridge_host_packets():
    lines = rules()
    chain = chain_rules(lines)
    # DNS to any address (the mini included: its port 53 is not 443) is dropped, after the one allow.
    for protocol in ('udp', 'tcp'):
        drop = f'iptables -A {CHAIN} -p {protocol} --dport 53 -j DROP'
        assert drop in chain and chain.index(drop) > 0
    # The isolated db bridge has no host address; its host-bound drop is the second layer. It is never forwarded
    # through the egress chain, and it has no IPv6.
    assert 'iptables -I INPUT -i dk-inferencedb0 -j DROP' in lines
    assert 'ip6tables -I INPUT -i dk-inferencedb0 -j DROP' in lines
    assert not any('DOCKER-USER' in line and 'dk-inferencedb0' in line for line in lines)


# spec: ANYM-688
def test_egress_rules_undo_is_the_mirror():
    applied, undone = rules(), run('rules', '--undo')[1].splitlines()
    inserts = {line.replace(' -I ', ' -D ', 1) for line in applied if ' -I ' in line}
    deletes = {line for line in undone if ' -D ' in line}
    assert inserts == deletes and inserts
    # The hooks are removed before either chain is flushed and deleted, or the delete would fail.
    assert undone[-4:] == [f'iptables -F {CHAIN}', f'iptables -X {CHAIN}',
                           f'iptables -t raw -F {CHAIN}', f'iptables -t raw -X {CHAIN}']
    assert max(undone.index(line) for line in deletes) < undone.index(f'iptables -F {CHAIN}')
    # `--undo` needs no address: it removes what any address added. A given address changes nothing.
    assert rules('--undo') == undone


# spec: ANYM-688
def test_egress_rules_refuse_a_non_tailnet_or_ipv6_address():
    for bad in ('192.168.1.5', '10.0.0.1', '8.8.8.8', '100.63.255.255', '100.128.0.0', '127.0.0.1', '169.254.169.254',
                'fd7a:115c:a1e0::1', '::1', 'deedkit-inference', '100.101.102.103/32', ''):
        code, out, err = run('rules', '--mini-ip', bad)
        assert code != 0 and out == '' and err, bad
    code, out, err = run('rules')
    assert code != 0 and out == ''
    for good in ('100.64.0.1', '100.127.255.254', MINI):
        code, out, _ = run('rules', '--mini-ip', good)
        assert code == 0 and f'-d {good} -p tcp --dport 443 -j RETURN' in out


# spec: ANYM-688
def test_check_reports_open_and_blocked_targets_with_a_positive_control():
    with socket.socket() as closed:
        # A port nobody holds: every OS refuses it.
        closed.bind(('127.0.0.1', 0))
        refused_target = f'127.0.0.1:{closed.getsockname()[1]}'
    with socket.socket() as listener, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as resolver, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as silent:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        resolver.bind(('127.0.0.1', 0))
        silent.bind(('127.0.0.1', 0))
        listening = f'127.0.0.1:{listener.getsockname()[1]}'
        answering = f'127.0.0.1:{resolver.getsockname()[1]}'

        def answer():
            query, peer = resolver.recvfrom(512)
            resolver.sendto(query[:2] + b'\x81\x80' + query[4:], peer)
        threading.Thread(target=answer, daemon=True).start()
        # The worker's view: the database and the mini open; TEST-NET-1 has no answer, what a DROP looks like from
        # inside; an alias that does not resolve and a resolver that never answers (a local silent socket: a real
        # address may sit behind a DNS-intercepting network) are blocked too.
        code, out, _ = run('check', '--timeout', '1', '--open', listening, '--blocked', '192.0.2.1:8080',
                           '--blocked-name', 'api.invalid:8080', '--blocked-dns', f'127.0.0.1:{silent.getsockname()[1]}')
        report = json.loads(out)
        assert code == 0 and report['passed'] is True
        assert [(r['expect'], r['passed']) for r in report['results']] == [
            ('open', True), ('blocked', True), ('blocked-name', True), ('blocked-dns', True)]
        assert report['results'][0]['outcome'] == 'connected' and report['results'][2]['outcome'] == 'unresolved'
        # The positive control, run from a sibling container: the same listener must connect, so a "blocked" in the
        # worker's run cannot mean that nothing listens. A control nobody answers fails, and so does the run.
        code, out, _ = run('check', '--timeout', '1', '--control', listening)
        assert code == 0 and json.loads(out)['results'][0] == {
            'target': listening, 'expect': 'control', 'outcome': 'connected', 'passed': True}
        code, out, _ = run('check', '--timeout', '1', '--control', refused_target)
        assert code == 1 and json.loads(out)['results'][0]['outcome'] == 'refused'
        # Every way an exit can be open fails: a connection, a refusal (something answered), a resolved name, an
        # answered DNS query. Names and malformed targets never pass as an IP literal.
        code, out, _ = run('check', '--timeout', '1', '--blocked', listening, '--blocked', refused_target,
                           '--blocked-name', 'localhost:' + listening.rpartition(':')[2], '--blocked-dns', answering,
                           '--blocked', 'api:8080', '--open', 'db.invalid:5432')
        report = json.loads(out)
        assert code == 1 and report['passed'] is False
        assert [r['passed'] for r in report['results']] == [False] * 6
        # A name that resolves fails whatever the connection would do: DNS escaped even if TCP is then dropped.
        assert [r['outcome'] for r in report['results']] == [
            'connected', 'refused', 'resolved', 'answered', 'malformed', 'unresolved']
        # An empty check proves nothing.
        code, out, _ = run('check')
        assert code == 1 and json.loads(out) == {'passed': False, 'results': []}
        # `listen` is the synthetic listener for the control: it prints its port, then accepts a connection.
        process = subprocess.Popen([sys.executable, '-I', str(SCRIPT), 'listen', '--port', '0', '--seconds', '10'],
                                   stdout=subprocess.PIPE, text=True)
        assert process.stdout is not None
        try:
            port = json.loads(process.stdout.readline())['listening']
            with socket.create_connection(('127.0.0.1', port), timeout=5):
                pass
        finally:
            process.terminate()
            process.wait(timeout=10)
            process.stdout.close()
        # In-process: a name that resolves fails even when the connection to its address is then dropped (the
        # lookup itself proves a resolver answered from inside), and an unresolved name passes.
        import importlib.util
        spec = importlib.util.spec_from_file_location('inference_egress_check', SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = module.socket.getaddrinfo
        module.socket.getaddrinfo = lambda *args, **kwargs: [(2, 1, 6, '', ('192.0.2.1', 8080))]
        try:
            result = module.check([('leak.example:8080', 'blocked-name')], 1)['results'][0]
        finally:
            module.socket.getaddrinfo = original
        assert (result['outcome'], result['passed']) == ('resolved', False)
