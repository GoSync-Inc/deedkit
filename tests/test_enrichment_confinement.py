"""The mini-facing worker proves its own network confinement before and while it claims (ANYM-688); a fake network layer, no sockets, no database."""
import json
import socket
import sys
import time

import pytest

from messaging_service import enrichment, inference_confinement
from messaging_service.enrichment import EnrichmentWorker
from messaging_service.enrichment_providers import validate_profile
from messaging_service.inference_confinement import Confinement, ConfinementLost

HOST = 'deedkit-inference.tail1234.ts.net'
MINI_IP = '100.101.102.103'


class Fake:
    """What the worker's network looks like: by default confined (everything times out, only the pinned mini name resolves)."""
    def __init__(self, open_ports=(), refused=(), dns_answers=False, resolves_anything=False, nxdomain=False, hang=None, raises=None):
        self.open_ports, self.refused, self.dns_answers = set(open_ports), set(refused), dns_answers
        self.resolves_anything, self.nxdomain, self.hang, self.raises = resolves_anything, nxdomain, hang, raises
        self.attempts = []

    def connect(self, host, port, timeout):
        self.attempts.append((host, port))
        if self.raises == (host, port):
            raise RuntimeError('a broken check')
        if self.hang == (host, port):
            time.sleep(3)
        if (host, port) in self.open_ports:
            return
        if (host, port) in self.refused:
            raise ConnectionRefusedError()
        raise TimeoutError()

    def dns_reply(self, host, port, query, timeout):
        if self.dns_answers:
            return True
        raise TimeoutError()

    def resolve(self, name):
        if name == HOST:
            return [MINI_IP]
        if self.resolves_anything:
            return ['203.0.113.7']
        if self.nxdomain:
            raise socket.gaierror(socket.EAI_NONAME, 'Name or service not known')     # a resolver answered
        raise socket.gaierror(socket.EAI_AGAIN, 'Temporary failure in name resolution')   # nobody answered


def guard(network=None, **kw):
    return Confinement([HOST], network=network or Fake(), timeout=kw.pop('timeout', 0.2), **kw)


# spec: ANYM-688
def test_a_confined_network_passes_every_probe():
    fake = Fake()
    assert guard(fake).probe() == []
    # it really tried to leave: public v4 and v6 on 443, a public resolver, and two other ports of the mini
    assert {('1.1.1.1', 443), ('8.8.8.8', 443), ('2606:4700:4700::1111', 443), ('1.1.1.1', 53),
            (MINI_IP, 22), (MINI_IP, 80)} <= set(fake.attempts)
    assert (MINI_IP, 443) not in fake.attempts      # the one allowed destination is never probed as a leak


# spec: ANYM-688
@pytest.mark.parametrize('fake,failed', [
    (Fake(open_ports=[('1.1.1.1', 443)]), 'tcp:1.1.1.1:443'),
    (Fake(open_ports=[('8.8.8.8', 443)]), 'tcp:8.8.8.8:443'),
    (Fake(open_ports=[('2606:4700:4700::1111', 443)]), 'tcp:2606:4700:4700::1111:443'),
    (Fake(open_ports=[('1.1.1.1', 53)]), 'tcp:1.1.1.1:53'),
    (Fake(refused=[('8.8.8.8', 443)]), 'tcp:8.8.8.8:443'),         # a refusal is an answer: a dropped packet gets none
    (Fake(dns_answers=True), 'dns:public_udp'),
    (Fake(resolves_anything=True), 'dns:system_resolver'),
    (Fake(nxdomain=True), 'dns:system_resolver'),                   # NXDOMAIN is an answer from a resolver
    (Fake(open_ports=[(MINI_IP, 22)]), f'mini_ports:{HOST}'),
    (Fake(open_ports=[(MINI_IP, 80)]), f'mini_ports:{HOST}'),
    (Fake(raises=('1.1.1.1', 443)), 'tcp:1.1.1.1:443'),             # a check that cannot run proves nothing
])
def test_any_way_out_fails_the_probe_and_names_the_check(fake, failed):
    assert guard(fake).probe() == [failed]


# spec: ANYM-688
def test_an_unresolvable_mini_name_is_not_confined():
    class NoMini(Fake):
        def resolve(self, name):
            if name == HOST:
                raise socket.gaierror(socket.EAI_NONAME, 'missing extra_hosts')
            return super().resolve(name)
    assert guard(NoMini()).probe() == [f'mini_ports:{HOST}']


# spec: ANYM-688
def test_a_hanging_attempt_is_bounded_and_counts_as_dropped():
    started = time.monotonic()
    assert guard(Fake(hang=('1.1.1.1', 443))).probe() == []
    assert time.monotonic() - started < 2.5          # timeout 0.2 s + 1 s margin, not the 3 s the fake sleeps


# spec: ANYM-688
def test_a_proof_is_reused_for_a_minute_then_repeated_and_a_loss_is_final():
    now = [1000.0]
    fake = Fake()
    g = guard(fake, clock=lambda: now[0], fresh=60.0)
    g.ensure()
    first = len(fake.attempts)
    now[0] += 59
    g.ensure()
    assert len(fake.attempts) == first               # still fresh: no new attempts
    now[0] += 2
    g.ensure()
    assert len(fake.attempts) == 2 * first           # older than a minute: probed again
    fake.open_ports.add(('1.1.1.1', 443))
    now[0] += 61
    with pytest.raises(ConfinementLost) as lost:
        g.ensure()
    assert lost.value.args[0] == ['tcp:1.1.1.1:443']
    fake.open_ports.clear()
    count = len(fake.attempts)
    with pytest.raises(ConfinementLost):
        g.ensure()                                   # once lost it stays lost, without probing again
    assert len(fake.attempts) == count


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
    worker = Recorder(guard(fake, fresh=0.0), after_first=lambda: fake.open_ports.add(('1.1.1.1', 443)))
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


def mini_config(tmp_path):
    key = tmp_path / 'inference.key'
    key.write_text('synthetic-key')
    key.chmod(0o600)
    profile = {'provider': 'deedkit_inference', 'local_inference': True, 'model': 'audio-default',
               'base_url': f'https://{HOST}/v1', 'api_key_file': str(key)}
    path = tmp_path / 'enrichment-mini.json'
    path.write_text(json.dumps({'enabled': True, 'accounts': ['a'], 'max_jobs_per_day': 5, 'profiles': {'audio': profile}}))
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
    monkeypatch.setattr(inference_confinement, 'Network', lambda: Fake(dns_answers=True))
    monkeypatch.setattr(inference_confinement, 'TIMEOUT', 0.2)
    monkeypatch.setattr(sys, 'argv', ['inference_confinement', HOST])
    with pytest.raises(SystemExit) as exit_info:
        inference_confinement.main()
    assert exit_info.value.code == 78
    assert json.loads(capsys.readouterr().out) == {'confined': False, 'failed': ['dns:public_udp']}
    monkeypatch.setattr(inference_confinement, 'Network', lambda: Fake())
    with pytest.raises(SystemExit) as exit_info:
        inference_confinement.main()
    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out) == {'confined': True, 'failed': []}
