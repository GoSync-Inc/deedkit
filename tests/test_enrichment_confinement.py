"""The mini-facing worker proves its own network confinement before and while it claims (ANYM-688); a fake network layer, no sockets, no database."""
import errno
import json
import socket
import sys
import threading
import time

import pytest

from messaging_service import enrichment, inference_confinement
from messaging_service.enrichment import EnrichmentWorker
from messaging_service.enrichment_providers import validate_profile
from messaging_service.inference_confinement import Confinement, ConfinementLost

HOST = 'deedkit-inference.tail1234.ts.net'
MINI_IP = '100.101.102.103'
GATEWAY = '192.168.0.1'


class Fake:
    """What the worker's network looks like: by default confined (everything times out, the resolver is the mini)."""
    def __init__(self, open_ports=(), refused=(), dns_answers=(), dns_refused=(), errors=None, delays=None,
                 nameservers=(MINI_IP,), gateway=GATEWAY, dns_rcodes=None, barrier=None, explode=False):
        self.open_ports, self.refused = set(open_ports), set(refused)
        self.dns_answers, self.dns_refused = set(dns_answers), set(dns_refused)
        self.dns_rcodes = dict(dns_rcodes or {})      # host -> RCODE of the reply that host gives
        self.errors, self.delays = dict(errors or {}), dict(delays or {})
        self.servers, self.gate = nameservers, gateway
        self.barrier, self.explode = barrier, explode
        self.attempts = []

    def connect(self, host, port, timeout):
        self.attempts.append((host, port))
        if self.barrier is not None and host == MINI_IP and port in (22, 80):
            self.barrier.wait(timeout=1)        # two checks must be running at the same time, or this breaks
        if (host, port) in self.delays:
            time.sleep(self.delays[(host, port)])
        if (host, port) in self.errors:
            raise self.errors[(host, port)]
        if (host, port) in self.open_ports:
            return
        if (host, port) in self.refused:
            raise ConnectionRefusedError()
        raise TimeoutError()

    def dns_reply(self, host, port, query, timeout):
        self.attempts.append((host, 'udp53'))
        if host in self.dns_answers or host in self.dns_rcodes:
            return b'\x12\x34\x81' + bytes([self.dns_rcodes.get(host, 0)]) + bytes(8)      # a DNS header with this RCODE
        if host in self.dns_refused:
            raise ConnectionRefusedError()
        raise TimeoutError()

    def nameservers(self):
        if self.explode:
            raise RuntimeError("can't start new thread")
        if isinstance(self.servers, Exception):
            raise self.servers
        return list(self.servers)

    def gateway(self):
        if isinstance(self.gate, Exception):
            raise self.gate
        return self.gate

    def resolve(self, name):
        if name == HOST:
            return [MINI_IP]
        raise socket.gaierror(socket.EAI_AGAIN, 'Temporary failure in name resolution')


def guard(network=None, **kw):
    return Confinement([HOST], network=network or Fake(), timeout=kw.pop('timeout', 0.2), **kw)


STUB = '127.0.0.11'      # Docker's embedded resolver, what resolv.conf names on a custom network


# spec: ANYM-688
def test_a_confined_network_passes_every_probe():
    fake = Fake()
    assert guard(fake).probe() == []
    # it really tried to leave: public v4 and v6, the metadata address, a public resolver, the configured resolver,
    # the host side of the bridge and two other ports of the mini
    assert {('1.1.1.1', 443), ('8.8.8.8', 443), ('2606:4700:4700::1111', 443), ('1.1.1.1', 53), ('169.254.169.254', 80),
            ('1.1.1.1', 'udp53'), (MINI_IP, 'udp53'), (MINI_IP, 53), (GATEWAY, 22), (GATEWAY, 443), (MINI_IP, 22),
            (MINI_IP, 80)} <= set(fake.attempts)
    assert (MINI_IP, 443) not in fake.attempts      # the one allowed destination is never probed as a leak


# spec: ANYM-688
@pytest.mark.parametrize('fake,failed', [
    (Fake(open_ports=[('1.1.1.1', 443)]), 'tcp:1.1.1.1:443'),
    (Fake(open_ports=[('8.8.8.8', 443)]), 'tcp:8.8.8.8:443'),
    (Fake(open_ports=[('2606:4700:4700::1111', 443)]), 'tcp:2606:4700:4700::1111:443'),
    (Fake(open_ports=[('1.1.1.1', 53)]), 'tcp:1.1.1.1:53'),
    (Fake(open_ports=[('169.254.169.254', 80)]), 'tcp:169.254.169.254:80'),
    (Fake(refused=[('8.8.8.8', 443)]), 'tcp:8.8.8.8:443'),            # a refusal is an answer: a dropped packet gets none
    (Fake(dns_answers=['1.1.1.1']), 'dns:public_udp:1.1.1.1'),
    (Fake(dns_answers=[MINI_IP]), f'dns:configured_udp:{MINI_IP}'),    # the configured resolver answers
    (Fake(dns_refused=[MINI_IP]), f'dns:configured_udp:{MINI_IP}'),    # ICMP port unreachable is an answer too
    (Fake(open_ports=[(MINI_IP, 53)]), f'dns:configured_tcp:{MINI_IP}'),
    (Fake(open_ports=[(GATEWAY, 22)]), f'host:{GATEWAY}:22'),
    (Fake(open_ports=[(GATEWAY, 443)]), f'host:{GATEWAY}:443'),
    (Fake(open_ports=[(MINI_IP, 22)]), f'mini:{HOST}:22'),
    (Fake(open_ports=[(MINI_IP, 80)]), f'mini:{HOST}:80'),
])
def test_any_way_out_fails_the_probe_and_names_the_check(fake, failed):
    assert guard(fake).probe() == [failed]


# spec: ANYM-688
@pytest.mark.parametrize('target,error,confined', [
    (('1.1.1.1', 443), OSError(errno.ENETUNREACH, 'unreachable'), True),
    (('1.1.1.1', 443), OSError(errno.EHOSTUNREACH, 'no route'), True),
    (('2606:4700:4700::1111', 443), OSError(errno.EADDRNOTAVAIL, 'ipv6 is disabled'), True),
    (('2606:4700:4700::1111', 443), OSError(errno.EAFNOSUPPORT, 'no ipv6'), True),
    (('1.1.1.1', 443), OSError(errno.EADDRNOTAVAIL, 'not a drop on ipv4'), False),
    (('1.1.1.1', 443), OSError(errno.EMFILE, 'too many open files'), False),      # an unobserved attempt proves nothing
    (('1.1.1.1', 443), OSError(errno.EPERM, 'operation not permitted'), False),
    (('1.1.1.1', 443), RuntimeError('a broken check'), False),
])
def test_only_a_dropped_attempt_counts_as_confined_and_an_unobserved_one_fails(target, error, confined):
    result = guard(Fake(errors={target: error})).probe()
    assert (result == []) is confined
    if not confined:
        assert result == [f'tcp:{target[0]}:{target[1]}']


# spec: ANYM-688
def test_local_facts_that_cannot_be_read_or_resolved_are_not_confined():
    assert guard(Fake(nameservers=OSError('no resolv.conf'))).probe() == ['resolv_conf_unreadable']
    assert guard(Fake(gateway=OSError('no route table'))).probe() == ['gateway_unreadable']

    class NoMini(Fake):
        def resolve(self, name):
            raise socket.gaierror(socket.EAI_NONAME, 'missing extra_hosts')
    assert guard(NoMini()).probe() == [f'mini_unresolved:{HOST}']


# spec: ANYM-688
def test_a_check_that_does_not_finish_in_time_fails_closed():
    started = time.monotonic()
    # the attempt would have been dropped after 1.6 s, but the proof cannot wait for it: not confined
    assert guard(Fake(delays={('1.1.1.1', 443): 1.6})).probe() == ['tcp:1.1.1.1:443']
    assert time.monotonic() - started < 2.0


# spec: ANYM-688
def test_the_other_mini_ports_are_probed_in_parallel_and_a_late_answer_is_not_missed():
    # the two attempts wait for each other at a barrier: a serial probe would break it and fail BOTH checks
    fake = Fake(barrier=threading.Barrier(2), delays={(MINI_IP, 22): 0.1}, open_ports=[(MINI_IP, 80)])
    assert guard(fake).probe() == [f'mini:{HOST}:80']


# spec: ANYM-688
@pytest.mark.parametrize('rcode,failed', [
    (2, []),                                  # SERVFAIL: the stub could not reach its upstream (the confined case)
    (5, []),                                  # REFUSED
    (3, [f'dns:configured_udp:{STUB}']),      # NXDOMAIN: an upstream resolver answered about an external name
    (0, [f'dns:configured_udp:{STUB}']),      # NOERROR
])
def test_dockers_embedded_resolver_is_judged_by_what_it_answers_not_by_listening(rcode, failed):
    # resolv.conf names Docker's local stub; it always listens (TCP and UDP), so listening proves nothing
    fake = Fake(nameservers=[STUB], dns_rcodes={STUB: rcode}, open_ports=[(STUB, 53)])
    assert guard(fake).probe() == failed


# spec: ANYM-688
def test_a_silent_embedded_resolver_is_confined_and_a_remote_resolver_reply_of_any_kind_is_not():
    assert guard(Fake(nameservers=[STUB])).probe() == []
    assert guard(Fake(dns_rcodes={MINI_IP: 2})).probe() == [f'dns:configured_udp:{MINI_IP}']     # not loopback: any reply


# spec: ANYM-688
def test_a_reachable_upstream_behind_a_servfailing_stub_is_found_by_asking_the_upstream_directly():
    # Docker's stub says SERVFAIL when a REACHABLE upstream returned SERVFAIL or REFUSED: the stub alone proves nothing,
    # so the pinned upstream (the mini) is probed on UDP and TCP 53 itself
    stub = {STUB: 2}
    assert guard(Fake(nameservers=[STUB], dns_rcodes=stub)).probe() == []
    assert guard(Fake(nameservers=[STUB], dns_rcodes={**stub, MINI_IP: 2})).probe() == [f'dns:mini_udp:{MINI_IP}']
    assert guard(Fake(nameservers=[STUB], dns_refused=[MINI_IP], dns_rcodes=stub)).probe() == [f'dns:mini_udp:{MINI_IP}']
    assert guard(Fake(nameservers=[STUB], dns_rcodes=stub, open_ports=[(MINI_IP, 53)])).probe() == [f'dns:mini_tcp:{MINI_IP}']
    # when the mini is itself the configured resolver it is probed once, as a configured one
    assert guard(Fake(dns_answers=[MINI_IP])).probe() == [f'dns:configured_udp:{MINI_IP}']


# spec: ANYM-688
@pytest.mark.parametrize('reply', [b'\x12\x34\x81\x02', b'\x12\x34\x81\x02' + bytes(7), b'\x12\x34\x01\x02' + bytes(8), b''])
def test_a_short_or_malformed_stub_reply_is_not_evidence_of_confinement(reply):
    class Odd(Fake):
        def dns_reply(self, host, port, query, timeout):
            if host == STUB:
                return reply
            return super().dns_reply(host, port, query, timeout)
    expected = [] if reply == b'' else [f'dns:configured_udp:{STUB}']      # silence is silence; anything incomplete fails
    assert guard(Odd(nameservers=[STUB])).probe() == expected


# spec: ANYM-688
def test_a_probe_that_cannot_be_set_up_is_a_failure_and_stops_a_running_job(stop_state):
    fake = Fake()
    worker = Recorder(guard(fake, fresh=3600.0, interval=0.05),
                      after_first=lambda: (setattr(fake, 'explode', True), stop_state.STOP.wait(10)))
    started = time.monotonic()
    with pytest.raises(ConfinementLost) as lost:
        worker.run()
    assert lost.value.args[0] == ['probe_error']
    assert time.monotonic() - started < 5 and worker.calls[-1] == 'unpublish'
    with pytest.raises(ConfinementLost):
        guard(Fake(explode=True)).ensure()


# spec: ANYM-688
def test_known_listening_addresses_from_the_config_must_not_answer():
    other_node = ('100.91.112.119', 22)
    assert guard(Fake(open_ports=[other_node]), blocked=[other_node]).probe() == ['configured:100.91.112.119:22']
    assert guard(Fake(open_ports=[other_node])).probe() == []            # not listed: not probed
    assert guard(Fake(), blocked=[other_node]).probe() == []             # listed and dropped


# spec: ANYM-688
def test_the_config_lists_blocked_targets_as_ip_and_port_only(tmp_path):
    assert inference_confinement.parse_targets(['100.91.112.119:22', '[2606:4700:4700::1111]:443']) == [
        ('100.91.112.119', 22), ('2606:4700:4700::1111', 443)]
    for bad in ('x', ['host.example:22'], ['1.2.3.4'], ['1.2.3.4:0'], ['1.2.3.4:70000'], [5], ['1.2.3.4:22'] * 17):
        with pytest.raises(ValueError, match='invalid_confinement_targets'):
            inference_confinement.parse_targets(bad)
    path, _ = mini_config(tmp_path, blocked=['100.91.112.119:22'])
    assert enrichment.configuration(path)['confinement_blocked'] == ['100.91.112.119:22']
    path, _ = mini_config(tmp_path, blocked=['not an address'])
    with pytest.raises(ValueError, match='invalid_confinement_targets'):
        enrichment.configuration(path)


# spec: ANYM-688
def test_the_proof_is_repeated_before_every_claim_and_a_loss_is_final():
    fake = Fake()
    g = guard(fake)
    g.ensure()
    first = len(fake.attempts)
    g.ensure()
    assert len(fake.attempts) == 2 * first           # the default is a fresh proof for every claim
    fake.open_ports.add(('1.1.1.1', 443))
    with pytest.raises(ConfinementLost) as lost:
        g.ensure()
    assert lost.value.args[0] == ['tcp:1.1.1.1:443']
    fake.open_ports.clear()
    count = len(fake.attempts)
    with pytest.raises(ConfinementLost):
        g.ensure()                                   # once lost it stays lost, without probing again
    assert len(fake.attempts) == count


# spec: ANYM-688
def test_a_proof_can_be_reused_for_the_configured_freshness():
    now = [1000.0]
    fake = Fake()
    g = guard(fake, clock=lambda: now[0], fresh=60.0)
    g.ensure()
    first = len(fake.attempts)
    now[0] += 59
    g.ensure()
    assert len(fake.attempts) == first
    now[0] += 2
    g.ensure()
    assert len(fake.attempts) == 2 * first


# spec: ANYM-688
def test_only_a_deedkit_inference_config_needs_the_proof_and_names_its_hosts(tmp_path):
    key = tmp_path / 'k'
    key.write_text('synthetic')
    key.chmod(0o600)
    mini = validate_profile({'provider': 'deedkit_inference', 'local_inference': True, 'model': 'audio-default',
                             'base_url': f'https://{HOST}/v1', 'api_key_file': str(key)}, 'audio')
    local = validate_profile({'provider': 'faster_whisper', 'model': str(tmp_path), 'timeout_seconds': 60}, 'audio')
    assert inference_confinement.required({'profiles': {'audio': mini}}) is True
    assert inference_confinement.mini_hosts({'profiles': {'audio': mini}}) == [HOST]
    assert inference_confinement.required({'profiles': {'audio': local}}) is False


class Recorder(EnrichmentWorker):
    """The worker's loop with no database: records what it publishes and claims."""
    def __init__(self, confinement, after_first=None):
        super().__init__('dsn', '/tmp', {'accounts': [], 'profiles': {}}, confinement=confinement)
        self.calls, self.after_first = [], after_first

    def publish_profiles(self, *, enabled=True):
        self.calls.append('publish')

    def unpublish_profiles(self):
        self.calls.append('unpublish')

    def once(self):
        self.calls.append('once')
        if self.after_first and self.calls.count('once') == 1:
            self.after_first()
        return True


@pytest.fixture
def stop_state():
    enrichment.STOP.clear()
    yield enrichment
    enrichment.STOP.clear()


# spec: ANYM-688
def test_the_worker_claims_and_publishes_nothing_when_it_starts_unconfined(stop_state):
    worker = Recorder(guard(Fake(open_ports=[('1.1.1.1', 443)])))
    with pytest.raises(ConfinementLost):
        worker.run()
    assert worker.calls == []           # no profile published, nothing claimed, nothing to unpublish


# spec: ANYM-688
def test_the_worker_stops_claiming_when_the_confinement_is_lost_between_claims(stop_state):
    fake = Fake()
    worker = Recorder(guard(fake), after_first=lambda: fake.open_ports.add(('1.1.1.1', 443)))
    with pytest.raises(ConfinementLost):
        worker.run()
    assert worker.calls == ['publish', 'once', 'unpublish']       # the second claim never started


# spec: ANYM-688
def test_the_monitor_stops_a_worker_that_is_in_the_middle_of_a_job(stop_state):
    fake = Fake()
    g = guard(fake, fresh=3600.0, interval=0.05)

    def in_flight():
        fake.open_ports.add(('1.1.1.1', 443))          # the rules vanish while a long job runs
        stop_state.STOP.wait(10)                       # request_stop() from the monitor ends the job

    worker = Recorder(g, after_first=in_flight)
    started = time.monotonic()
    with pytest.raises(ConfinementLost):
        worker.run()
    assert time.monotonic() - started < 5
    assert worker.calls[-1] == 'unpublish'


# spec: ANYM-688
def test_a_worker_without_the_guard_runs_as_before(stop_state):
    worker = Recorder(None, after_first=stop_state.STOP.set)
    worker.run()
    assert worker.calls == ['publish', 'once', 'unpublish']


def mini_config(tmp_path, blocked=None):
    key = tmp_path / 'inference.key'
    key.write_text('synthetic-key')
    key.chmod(0o600)
    profile = {'provider': 'deedkit_inference', 'local_inference': True, 'model': 'audio-default',
               'base_url': f'https://{HOST}/v1', 'api_key_file': str(key)}
    path = tmp_path / 'enrichment-mini.json'
    extra = {'confinement_blocked': blocked} if blocked is not None else {}
    path.write_text(json.dumps({'enabled': True, 'accounts': ['a'], 'max_jobs_per_day': 5, 'profiles': {'audio': profile}, **extra}))
    path.chmod(0o600)
    dsn = tmp_path / 'db.dsn'
    dsn.write_text('postgresql://nobody@127.0.0.1:1/none')
    dsn.chmod(0o600)
    return path, dsn


# spec: ANYM-688
def test_the_worker_process_exits_78_with_the_failed_checks_and_never_touches_the_database(tmp_path, monkeypatch, capsys):
    class Leaking(Fake):
        def __init__(self):
            super().__init__(open_ports=[('1.1.1.1', 443)])
    path, dsn = mini_config(tmp_path)
    monkeypatch.setattr(inference_confinement, 'Network', Leaking)
    monkeypatch.setattr(inference_confinement, 'TIMEOUT', 0.2)
    monkeypatch.setattr(sys, 'argv', ['enrichment', '--config', str(path)])
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn))
    monkeypatch.setenv('PROBE_FILES', str(tmp_path))
    monkeypatch.setattr('messaging_service.telemetry.configure', lambda *_: None)

    def no_database(*_a, **_k):
        raise AssertionError('the worker reached for the database although it is not confined')
    monkeypatch.setattr('psycopg.connect', no_database)
    with pytest.raises(SystemExit) as exit_info:
        enrichment.main()
    assert exit_info.value.code == inference_confinement.EXIT_CODE == 78
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert line == {'error': 'confinement_not_effective', 'checks': ['tcp:1.1.1.1:443']}


# spec: ANYM-688
def test_the_probe_command_prints_one_json_line_and_exits_78_when_not_confined(monkeypatch, capsys):
    monkeypatch.setattr(inference_confinement, 'Network', lambda: Fake(dns_answers=['1.1.1.1']))
    monkeypatch.setattr(inference_confinement, 'TIMEOUT', 0.2)
    monkeypatch.setattr(sys, 'argv', ['inference_confinement', HOST])
    with pytest.raises(SystemExit) as exit_info:
        inference_confinement.main()
    assert exit_info.value.code == 78
    assert json.loads(capsys.readouterr().out) == {'confined': False, 'failed': ['dns:public_udp:1.1.1.1']}
    monkeypatch.setattr(inference_confinement, 'Network', lambda: Fake())
    with pytest.raises(SystemExit) as exit_info:
        inference_confinement.main()
    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out) == {'confined': True, 'failed': []}
