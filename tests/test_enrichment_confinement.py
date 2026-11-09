"""The mini-facing worker proves its own network confinement before and while it claims (ANYM-688); a fake network layer, no sockets, no database."""
import errno
import json
import socket
import sys
import threading
import time

from hypothesis import given, strategies as st
import psycopg
import pytest

from messaging_service import enrichment, inference_confinement, telemetry
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


# spec: ANYM-838
def test_two_lanes_claiming_at_once_share_one_proof_made_after_both_asked():
    import threading
    g = guard(fresh=0.0)
    probes, entered = [], threading.Event()
    def slow_probe():
        probes.append(threading.current_thread().name)
        entered.set()
        time.sleep(0.3)
        return []
    g.probe = slow_probe
    first = threading.Thread(target=g.ensure, name='main-lane')
    first.start()
    assert entered.wait(5)
    started = time.monotonic()
    g.ensure()          # asked while the other lane's proof runs: that proof ends after this call asked, so it counts
    waited = time.monotonic() - started
    first.join(5)
    assert probes == ['main-lane'] and waited < 0.55     # one shared proof, never a second one of its own
    g.ensure()          # nobody else is proving: a claim still gets its own proof (fresh 0)
    assert len(probes) == 2


# spec: ANYM-838
def test_a_lane_waiting_for_a_proof_that_fails_is_stopped_by_it():
    import threading
    g = guard(fresh=0.0)
    entered = threading.Event()
    def failing_probe():
        entered.set()
        time.sleep(0.2)
        return ['tcp:synthetic']
    g.probe = failing_probe
    errors = []
    def lane():
        try:
            g.ensure()
        except ConfinementLost as error:
            errors.append(error.args[0])
    first = threading.Thread(target=lane)
    first.start()
    assert entered.wait(5)
    with pytest.raises(ConfinementLost):
        g.ensure()
    first.join(5)
    assert errors == [['tcp:synthetic']]


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


class Flaky(Recorder):
    """Recorder whose passes follow a script: an exception is what that pass raises, None a clean pass.
    The worker is told to stop when the script ends."""
    def __init__(self, script):
        super().__init__(None)
        self.script = list(script)

    def once(self):
        self.calls.append('once')
        if not self.script:
            enrichment.STOP.set()
            return True
        step = self.script.pop(0)
        if step:
            raise step
        return True


@pytest.fixture
def waits(stop_state, monkeypatch):
    """The delays the loop waited for, without waiting."""
    seen = []
    stop = stop_state.STOP
    monkeypatch.setattr(stop, 'wait', lambda seconds=None: seen.append(seconds) or stop.is_set())
    return seen


# Incident 11-05: one statement timeout in claim() ended enricher-mini (exit 1, restart policy `no`) and halted autodeploy.
# spec: ANYM-688
@pytest.mark.parametrize('error', [
    psycopg.errors.QueryCanceled('synthetic'), psycopg.errors.LockNotAvailable('synthetic'),
    psycopg.errors.DeadlockDetected('synthetic'), psycopg.errors.AdminShutdown('synthetic'),
    psycopg.OperationalError('connection failed: connection timeout expired'),
    psycopg.OperationalError('consuming input failed: SSL SYSCALL error: EOF detected'),
    psycopg.OperationalError('consuming input failed: SSL error: unexpected eof while reading'),
], ids=['QueryCanceled', 'LockNotAvailable', 'DeadlockDetected', 'AdminShutdown', 'connect_timeout_text',
        'ssl_syscall_eof_text', 'ssl_unexpected_eof_text'])
def test_a_transient_database_error_in_a_pass_does_not_end_the_worker(waits, error):
    worker = Flaky([error])
    worker.run()
    assert worker.calls == ['publish', 'once', 'publish', 'once', 'unpublish']
    assert waits == [enrichment.DB_RETRY_SECONDS]


# spec: ANYM-688
def test_a_transient_database_error_while_publishing_profiles_does_not_end_the_worker(waits):
    worker = Flaky([])
    real = worker.publish_profiles
    failures = [psycopg.errors.QueryCanceled('synthetic')]

    def publish(*, enabled=True):
        real(enabled=enabled)
        if failures:
            raise failures.pop()

    worker.publish_profiles = publish
    worker.run()
    assert worker.calls == ['publish', 'publish', 'once', 'unpublish']
    assert waits == [enrichment.DB_RETRY_SECONDS]


# spec: ANYM-688
def test_the_database_retry_delay_doubles_to_its_limit_and_a_clean_pass_resets_it(waits):
    timeout = psycopg.errors.QueryCanceled('synthetic')
    Flaky([timeout] * 6 + [None, timeout]).run()
    assert waits == [5, 10, 20, 40, 60, 60, 5]


# spec: ANYM-688
def test_the_database_retry_reports_only_the_exception_type(waits, capsys, monkeypatch):
    captured = []
    monkeypatch.setattr(telemetry, 'capture', lambda error, code: captured.append((type(error), code)))
    Flaky([psycopg.OperationalError('synthetic-dsn-host secret-text: Connection refused')]).run()
    err = capsys.readouterr().err
    assert 'synthetic-dsn-host' not in err and 'secret-text' not in err
    assert json.loads(err) == {'status': 'error', 'error_code': 'enrichment_database_retry',
                               'exception_type': 'OperationalError', 'retry_seconds': 5}
    assert captured == [(psycopg.OperationalError, 'enrichment_failed')]


# spec: ANYM-688
def test_confinement_lost_after_a_database_error_still_ends_the_worker_before_the_next_claim(waits):
    fake = Fake()

    def lost_then_timeout():
        fake.open_ports.add(('1.1.1.1', 443))
        raise psycopg.errors.QueryCanceled('synthetic')

    worker = Recorder(guard(fake), after_first=lost_then_timeout)
    with pytest.raises(ConfinementLost):
        worker.run()
    assert worker.calls == ['publish', 'once', 'unpublish']


# spec: ANYM-688
def test_the_confinement_monitor_ends_a_database_backoff_at_once(stop_state, monkeypatch):
    fake = Fake()
    monkeypatch.setattr(enrichment, 'DB_RETRY_SECONDS', 60, raising=False)

    def lost_then_timeout():
        fake.open_ports.add(('1.1.1.1', 443))
        raise psycopg.errors.QueryCanceled('synthetic')

    worker = Recorder(guard(fake, fresh=3600.0, interval=0.05), after_first=lost_then_timeout)
    started = time.monotonic()
    with pytest.raises(ConfinementLost):
        worker.run()
    assert time.monotonic() - started < 5
    assert worker.calls == ['publish', 'once', 'unpublish']


# spec: ANYM-688
def test_a_database_error_after_the_stop_request_still_ends_the_worker(waits):
    def stop_then_timeout():
        enrichment.STOP.set()
        raise psycopg.errors.QueryCanceled('synthetic')

    worker = Recorder(None, after_first=stop_then_timeout)
    with pytest.raises(psycopg.errors.QueryCanceled):
        worker.run()
    assert worker.calls == ['publish', 'once', 'unpublish']
    assert waits == []


# spec: ANYM-688
def test_a_database_error_that_is_not_transient_still_ends_the_worker(waits):
    worker = Flaky([psycopg.errors.UndefinedTable('synthetic')])
    with pytest.raises(psycopg.errors.UndefinedTable):
        worker.run()
    assert worker.calls == ['publish', 'once', 'unpublish']
    assert waits == []


# spec: ANYM-688
@pytest.mark.parametrize('error', [
    psycopg.errors.InvalidPassword('synthetic'), psycopg.errors.ConfigFileError('synthetic'),
    psycopg.OperationalError('connection is bad: FATAL:  password authentication failed for user "synthetic"'),
    psycopg.OperationalError('connection is bad: FATAL:  no pg_hba.conf entry for host "192.0.2.1"'),
    psycopg.OperationalError('connection failed: invalid sslmode value: "synthetic"'),
    psycopg.OperationalError('connection failed: Connection refused\nMultiple connection attempts failed. '
                             'FATAL:  password authentication failed for user "synthetic"'),
    psycopg.OperationalError('connection failed: SSL error: certificate verify failed'),
], ids=['InvalidPassword', 'ConfigFileError', 'password_text', 'hba_text', 'sslmode_text', 'mixed_attempts_text',
        'certificate_text'])
def test_a_permanent_database_failure_still_ends_the_worker(waits, error):
    worker = Flaky([error])
    with pytest.raises(psycopg.OperationalError):
        worker.run()
    assert worker.calls == ['publish', 'once', 'unpublish']
    assert waits == []


def coded_operational_errors(parent=psycopg.OperationalError):
    for child in parent.__subclasses__():
        yield child
        yield from coded_operational_errors(child)


# spec: ANYM-688
def test_an_operational_error_outside_the_transient_list_is_fatal_whatever_its_sqlstate():
    coded = {kind for kind in coded_operational_errors() if kind.sqlstate}
    assert set(enrichment.DB_TRANSIENT) < coded          # the list names real classes, and not all of them
    for kind in coded:
        assert enrichment.transient_database_error(kind('synthetic')) is (kind in enrichment.DB_TRANSIENT)


# spec: ANYM-688
@given(st.text().filter(lambda text: not any(marker in text for marker in
                                              enrichment.DB_CONNECTION_LOST + enrichment.DB_LOGIN_REFUSED)),
       st.sampled_from(enrichment.DB_CONNECTION_LOST), st.sampled_from(enrichment.DB_LOGIN_REFUSED))
def test_an_error_without_a_sqlstate_is_transient_only_for_a_lost_connection_and_no_refusal(text, lost, refused):
    assert not enrichment.transient_database_error(psycopg.OperationalError(text))
    assert enrichment.transient_database_error(psycopg.OperationalError(text + lost))
    assert not enrichment.transient_database_error(psycopg.OperationalError(text + refused))
    assert not enrichment.transient_database_error(psycopg.OperationalError(lost + text + refused))
    assert not enrichment.transient_database_error(psycopg.OperationalError(refused + text + lost))


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


CANARY = 'synthetic-canary-of-the-masked-error'


class GivesBack(Recorder):
    """The real once() around a job the stop ends, with no database: the claim is canned and the requeue fails for good."""
    def once(self):
        self.calls.append('once')
        return EnrichmentWorker.once(self)

    def claim(self):
        return {'attachment': 'synthetic', 'lease_token': 'synthetic'}, {}

    def _requeue(self, row):
        raise psycopg.Error(CANARY)


class CannotUnpublish(Recorder):
    def unpublish_profiles(self):
        self.calls.append('unpublish')
        raise psycopg.OperationalError(CANARY)


def run_main(worker_for, fake, tmp_path, monkeypatch):
    """main() with its real exit handling around a worker with no database; the monitor checks every 50 ms."""
    path, dsn = mini_config(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['enrichment', '--config', str(path)])
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn))
    monkeypatch.setenv('PROBE_FILES', str(tmp_path))
    monkeypatch.setattr('messaging_service.telemetry.configure', lambda *_: None)
    monkeypatch.setattr(enrichment.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(inference_confinement, 'Confinement', lambda *_a, **_k: guard(fake, fresh=3600.0, interval=0.05))
    monkeypatch.setattr(enrichment, 'EnrichmentWorker', lambda *_a, confinement: worker_for(confinement))
    enrichment.main()


def assert_exit_78_and_only_check_names(worker_for, fake, tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exit_info:
        run_main(worker_for, fake, tmp_path, monkeypatch)
    assert exit_info.value.code == inference_confinement.EXIT_CODE == 78
    output = capsys.readouterr()
    assert json.loads(output.err) == {'error': 'confinement_not_effective', 'checks': ['tcp:1.1.1.1:443']}
    assert CANARY not in output.err + output.out and 'Traceback' not in output.err


def lost_in_flight(fake, stop):
    """The rules vanish while a job runs; the job ends only when the monitor's request_stop() has ended it."""
    fake.open_ports.add(('1.1.1.1', 443))
    assert stop.wait(10)


# spec: ANYM-786
def test_a_database_error_on_the_stop_path_of_a_lost_confinement_exits_78(stop_state, tmp_path, monkeypatch, capsys):
    fake, workers = Fake(), []

    def lost_then_error():
        lost_in_flight(fake, stop_state.STOP)
        raise psycopg.errors.QueryCanceled(CANARY)

    def worker_for(confinement):
        workers.append(Recorder(confinement, after_first=lost_then_error))
        return workers[-1]
    assert_exit_78_and_only_check_names(worker_for, fake, tmp_path, monkeypatch, capsys)
    assert workers[0].calls == ['publish', 'once', 'unpublish']


# spec: ANYM-786
def test_a_requeue_that_runs_out_of_time_after_a_lost_confinement_exits_78(stop_state, tmp_path, monkeypatch, capsys):
    fake, workers = Fake(), []
    monkeypatch.setattr(enrichment, 'REQUEUE_SECONDS', 0)

    def ended_by_the_stop(_files, _row, _profile):
        lost_in_flight(fake, stop_state.STOP)
        raise enrichment.WorkerStopping

    def worker_for(confinement):
        workers.append(GivesBack(confinement))
        workers[-1].processor = ended_by_the_stop
        return workers[-1]
    assert_exit_78_and_only_check_names(worker_for, fake, tmp_path, monkeypatch, capsys)
    assert workers[0].requeueing and workers[0].calls == ['publish', 'once', 'unpublish']


# spec: ANYM-786
def test_an_unpublish_that_fails_after_a_lost_confinement_exits_78(stop_state, tmp_path, monkeypatch, capsys):
    fake, workers = Fake(), []

    def worker_for(confinement):
        workers.append(CannotUnpublish(confinement, after_first=lambda: lost_in_flight(fake, stop_state.STOP)))
        return workers[-1]
    assert_exit_78_and_only_check_names(worker_for, fake, tmp_path, monkeypatch, capsys)
    assert workers[0].calls == ['publish', 'once', 'unpublish']


# spec: ANYM-786
def test_a_clean_stop_of_a_confined_worker_exits_0(stop_state, tmp_path, monkeypatch, capsys):
    run_main(lambda confinement: Recorder(confinement, after_first=stop_state.STOP.set), Fake(), tmp_path, monkeypatch)
    assert capsys.readouterr().err == ''


# spec: ANYM-786
def test_without_a_lost_confinement_a_database_error_is_not_masked(stop_state, tmp_path, monkeypatch):
    def fails():
        raise psycopg.errors.UndefinedTable(CANARY)
    with pytest.raises(psycopg.errors.UndefinedTable, match=CANARY):
        run_main(lambda confinement: Recorder(confinement, after_first=fails), Fake(), tmp_path, monkeypatch)
    stop_state.STOP.set()
    with pytest.raises(psycopg.OperationalError, match=CANARY):
        run_main(lambda confinement: CannotUnpublish(confinement), Fake(), tmp_path, monkeypatch)


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
