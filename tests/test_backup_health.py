"""Backup alerts (ANYM-807): failed, undelivered and unverified backups become Tiden issues. Synthetic files only."""
import configparser
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import subprocess

from hypothesis import given, settings, strategies as st
import pytest
from tiden_telemetry import Client, parse_dsn
from tiden_telemetry._envelope import CONTENT_TYPE as SDK_CONTENT_TYPE

from messaging_service import backup_health as h, telemetry

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / 'deploy'
NOW = datetime(2026, 11, 6, 21, 37, 2, tzinfo=timezone.utc)
SHA = 'a' * 40
DSN = 'https://synthetic-key@ingest.example.test/synthetic-project'
PRODUCER_UNITS = {'messaging': ['personal-os-backup.service'],
                  'calendar': ['deedkit-calendar-backup.service', 'deedkit-calendar-backup-catchup.service'],
                  'recordings': ['deedkit-recordings-backup.service', 'deedkit-recordings-backup-catchup.service'],
                  'calls': ['deedkit-calls-backup.service', 'deedkit-calls-backup-catchup.service']}
VERIFIER_UNITS = {p: f'deedkit-{p}-backup-r2-verifier.service' for p in h.PROFILES}


def iso(moment):
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def stamp(moment):
    return moment.strftime('%a %Y-%m-%d %H:%M:%S.%f UTC')


def config(tmp_path, side='producer', profiles=h.PROFILES):
    (tmp_path / 'state').mkdir(exist_ok=True)
    (tmp_path / 'dsn').write_text(DSN + '\n')
    entries = {}
    for p in profiles:
        if side == 'producer':
            entries[p] = {'units': PRODUCER_UNITS[p], 'status': str(tmp_path / f'{p}-status.json')}
        else:
            (tmp_path / p).mkdir(exist_ok=True)
            entries[p] = {'unit': VERIFIER_UNITS[p], 'directory': str(tmp_path / p)}
    data = {'side': side, 'environment': 'local-synthetic', 'release_sha': SHA, 'dsn_file': str(tmp_path / 'dsn'),
            'state_directory': str(tmp_path / 'state'), 'profiles': entries}
    path = tmp_path / f'{side}.json'
    path.write_text(json.dumps(data))
    return h.load_config(path)


def systemctl(states=None, fail=False, timeout=False):
    """A fake `systemctl show` run: {unit: (ActiveState, moment or None)}; inactive with no timestamp by default."""
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if timeout:
            raise subprocess.TimeoutExpired(argv, kwargs.get('timeout'))
        units = argv[argv.index('--') + 1:]
        blocks = []
        for unit in units:
            state, moment = (states or {}).get(unit, ('inactive', None))
            blocks.append(f'Id={unit}\nActiveState={state}\nStateChangeTimestamp={stamp(moment) if moment else ""}')
        return subprocess.CompletedProcess(argv, 1 if fail else 0, '\n\n'.join(blocks) + '\n', '')
    run.calls = calls
    return run


def status(tmp_path, profile, **fields):
    (tmp_path / f'{profile}-status.json').write_text(json.dumps(fields))


def good_status(tmp_path, profile, captured=NOW - timedelta(hours=3), success=None):
    status(tmp_path, profile, state='ok', last_snapshot_at=iso(captured),
           last_success_at=(success or captured + timedelta(minutes=50)).isoformat())


def readings(cfg, run=None, now=NOW):
    return h.evaluate(cfg, now, run or systemctl())


def verified(tmp_path, profile, captured, verified_at=None, failure_at=None):
    directory = tmp_path / profile
    run = 'b' * 32
    backup_id = captured.strftime('%Y%m%dT%H%M%SZ') + '-' + 'c' * 16
    (directory / 'last-success.json').write_text(json.dumps({
        'v': 1, 'status': 'verified', 'backup_id': backup_id, 'verification_id': run,
        'verified_at': iso(verified_at or captured + timedelta(hours=2))}))
    if profile in h.PROOF_CAPTURE:
        (directory / f'proof-{run}.json').write_text(json.dumps({
            'v': 1, 'status': 'verified', 'verification': {'id': run, 'profile': profile, 'captured_at': iso(captured)}}))
    if failure_at:
        (directory / 'last-failure.json').write_text(json.dumps({'v': 1, 'status': 'failed', 'error': 'x',
                                                                 'failed_at': iso(failure_at)}))


# spec: ANYM-807
@pytest.mark.parametrize('side', ['producer', 'verifier'])
def test_a_valid_configuration_of_each_side_loads(tmp_path, side):
    cfg = config(tmp_path, side)
    assert cfg['side'] == side and set(cfg['profiles']) == set(h.PROFILES)
    for name in ('backup-health-production.example.json', 'backup-health-backup1.example.json'):
        assert h.load_config(DEPLOY / name)['profiles']


# spec: ANYM-807
@pytest.mark.parametrize('change', ['unknown_key', 'unknown_side', 'unknown_profile', 'empty_profiles', 'bad_sha',
                                    'bad_environment', 'relative_path', 'bad_unit', 'entry_key', 'not_json',
                                    'side_list', 'environment_object'])
def test_each_refused_configuration_exits_2_before_any_read(tmp_path, change, monkeypatch):
    cfg = config(tmp_path)
    data = json.loads((tmp_path / 'producer.json').read_text())
    if change == 'unknown_key':
        data['extra'] = 1
    elif change == 'unknown_side':
        data['side'] = 'both'
    elif change == 'unknown_profile':
        data['profiles']['photos'] = data['profiles']['messaging']
    elif change == 'empty_profiles':
        data['profiles'] = {}
    elif change == 'bad_sha':
        data['release_sha'] = 'main'
    elif change == 'bad_environment':
        data['environment'] = 'synthetic'
    elif change == 'relative_path':
        data['profiles']['messaging']['status'] = 'status.json'
    elif change == 'bad_unit':
        data['profiles']['messaging']['units'] = ['personal-os-backup.timer']
    elif change == 'entry_key':
        data['profiles']['messaging']['directory'] = '/x'
    elif change == 'side_list':
        data['side'] = []
    elif change == 'environment_object':
        data['environment'] = {}
    path = tmp_path / 'bad.json'
    path.write_text('{' if change == 'not_json' else json.dumps(data))
    monkeypatch.setattr(h, 'evaluate', lambda *a, **k: pytest.fail('read before refusal'))
    assert h.main(['--config', str(path)]) == 2
    assert cfg


# spec: ANYM-807
def test_an_error_state_raises_backup_failed(tmp_path):
    cfg = config(tmp_path, profiles=['messaging'])
    good_status(tmp_path, 'messaging')
    status(tmp_path, 'messaging', **{**json.loads((tmp_path / 'messaging-status.json').read_text()), 'state': 'error',
                                     'error_code': 'docker_operation'})
    reading = readings(cfg)['messaging']
    assert 'backup_failed' in reading['raised'] and reading['word'] == 'FAILED'


# spec: ANYM-807
@pytest.mark.parametrize('offset,raised', [(timedelta(hours=1), True), (timedelta(0), True),
                                           (timedelta(hours=-1), False)])
def test_a_failed_unit_raises_only_when_it_failed_at_or_after_the_last_success(tmp_path, offset, raised):
    cfg = config(tmp_path, profiles=['calendar'])
    success = datetime(2026, 11, 6, 20, 45, 1, 123456, tzinfo=timezone.utc)
    good_status(tmp_path, 'calendar', captured=success - timedelta(minutes=30), success=success)
    run = systemctl({'deedkit-calendar-backup.service': ('failed', success + offset)})
    assert ('backup_failed' in readings(cfg, run)['calendar']['raised']) is raised


# spec: ANYM-807
def test_a_first_launch_failure_with_no_recorded_success_raises(tmp_path):
    cfg = config(tmp_path, profiles=['calls'])
    status(tmp_path, 'calls', state='capturing', last_snapshot_at=iso(NOW - timedelta(hours=1)))
    run = systemctl({'deedkit-calls-backup.service': ('failed', NOW - timedelta(minutes=5))})
    assert 'backup_failed' in readings(cfg, run)['calls']['raised']


# spec: ANYM-807
@pytest.mark.parametrize('state', ['ok', 'skipped'])
def test_ok_skipped_and_a_missing_success_with_an_inactive_unit_raise_nothing(tmp_path, state):
    cfg = config(tmp_path, profiles=['recordings'])
    status(tmp_path, 'recordings', state=state, last_snapshot_at=iso(NOW - timedelta(hours=2)))
    reading = readings(cfg)['recordings']
    assert reading['raised'] == set() and reading['word'] == 'delivered'


# spec: ANYM-807
def test_a_newer_verification_failure_raises_and_an_older_one_does_not(tmp_path):
    cfg = config(tmp_path, 'verifier', ['calendar'])
    captured = NOW - timedelta(hours=5)
    verified(tmp_path, 'calendar', captured, failure_at=captured + timedelta(hours=3))
    assert readings(cfg)['calendar']['raised'] == {'backup_verify_failed'}
    verified(tmp_path, 'calendar', captured, failure_at=captured + timedelta(hours=1))
    reading = readings(cfg)['calendar']
    assert reading['raised'] == set() and reading['word'] == 'verified'


# spec: ANYM-807
def test_a_failed_verifier_unit_raises_and_busy_does_not(tmp_path):
    cfg = config(tmp_path, 'verifier', ['messaging'])
    verified(tmp_path, 'messaging', NOW - timedelta(hours=4))
    (tmp_path / 'messaging' / 'status.json').write_text(json.dumps({'v': 1, 'status': 'busy', 'code': 'receiver_busy'}))
    assert readings(cfg)['messaging']['raised'] == set()
    run = systemctl({'deedkit-messaging-backup-r2-verifier.service': ('failed', NOW)})
    assert readings(cfg, run)['messaging']['raised'] == {'backup_verify_failed'}


# spec: ANYM-807
@pytest.mark.parametrize('profile', h.PROFILES)
@pytest.mark.parametrize('side', ['producer', 'verifier'])
def test_stale_is_just_over_the_profiles_limit_and_not_just_under(tmp_path, profile, side):
    cfg = config(tmp_path, side, [profile])
    code = 'backup_stale' if side == 'producer' else 'backup_unverified'
    limit = timedelta(hours=h.LIMIT_HOURS[profile])
    for age, raised in ((limit - timedelta(minutes=1), False), (limit + timedelta(minutes=1), True)):
        if side == 'producer':
            good_status(tmp_path, profile, captured=NOW - age)
        else:
            verified(tmp_path, profile, NOW - age, verified_at=NOW - timedelta(minutes=10))
        assert (code in readings(cfg)[profile]['raised']) is raised


# spec: ANYM-807
def test_the_limits_are_named_with_the_card_values():
    assert h.LIMIT_HOURS == {'messaging': 30, 'calendar': 32, 'recordings': 30, 'calls': 36}
    assert (h.REPEAT_S, h.FUTURE_GRACE_S, h.PROBE_TIMEOUT_S, h.SEND_TIMEOUT_S, h.SEND_DEADLINE_S) == (86400, 300, 10, 5, 45)


# spec: ANYM-807
def test_a_retried_old_snapshot_keeps_its_old_age_on_the_verifier_side(tmp_path):
    cfg = config(tmp_path, 'verifier', ['calls'])
    verified(tmp_path, 'calls', NOW - timedelta(hours=40), verified_at=NOW - timedelta(minutes=5))
    reading = readings(cfg)['calls']
    assert 'backup_unverified' in reading['raised'] and reading['age_hours'] == 40.0


# spec: ANYM-807
def test_a_resumed_messaging_job_reads_its_job_age_not_a_later_capture(tmp_path):
    cfg = config(tmp_path, 'verifier', ['messaging'])
    job = NOW - timedelta(hours=31)
    verified(tmp_path, 'messaging', job, verified_at=NOW - timedelta(hours=1))
    reading = readings(cfg)['messaging']
    assert reading['age_hours'] == 31.0 and 'backup_unverified' in reading['raised']


# spec: ANYM-807
@pytest.mark.parametrize('case', ['missing', 'corrupt', 'future', 'not_verified'])
@pytest.mark.parametrize('side', ['producer', 'verifier'])
def test_missing_corrupt_and_future_files_count_as_stale(tmp_path, side, case):
    cfg = config(tmp_path, side, ['recordings'])
    target = tmp_path / 'recordings-status.json' if side == 'producer' else tmp_path / 'recordings' / 'last-success.json'
    if case == 'corrupt':
        target.write_text('{')
    elif case == 'future':
        if side == 'producer':
            good_status(tmp_path, 'recordings', captured=NOW + timedelta(minutes=6))
        else:
            verified(tmp_path, 'recordings', NOW + timedelta(minutes=6))
    elif case == 'not_verified':
        target.write_text(json.dumps({'status': 'idle'}))
    code = 'backup_stale' if side == 'producer' else 'backup_unverified'
    assert code in readings(cfg)['recordings']['raised']


def timer_hours(name):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = lambda option: option
    parser.read_string((DEPLOY / name).read_text())
    match = re.search(r'(\d\d):(\d\d):00', parser['Timer']['OnCalendar'])
    return int(match[1]) + int(match[2]) / 60


# spec: ANYM-807
@pytest.mark.parametrize('profile', h.PROFILES)
def test_the_latest_normal_capture_plus_two_hours_of_verification_stays_under_the_limit(profile):
    daily = 18.0 if profile == 'messaging' else timer_hours(f'deedkit-{profile}-backup.timer')
    latest = daily
    if (DEPLOY / f'deedkit-{profile}-backup-catchup.timer').exists():
        # A catch-up after a failed day, with the runner's 5400 s host-lock wait (BACKUP.md, Daily run and freshness).
        latest = timer_hours(f'deedkit-{profile}-backup-catchup.timer') + 1.5
    assert 24 + (latest - daily) + 2 < h.LIMIT_HOURS[profile]


# spec: ANYM-807
def test_episodes_raise_once_repeat_after_a_day_and_start_again_after_clearing(tmp_path):
    cfg = config(tmp_path, profiles=['messaging'])
    status(tmp_path, 'messaging', state='error', last_snapshot_at=iso(NOW - timedelta(hours=1)))
    sent = []

    def send(url, data, timeout):
        sent.append(json.loads(data.split(b'\n')[2])['tags']['error_code'])
        return True
    run = systemctl()
    h.check(cfg, now=NOW, run=run, send=send)
    h.check(cfg, now=NOW + timedelta(hours=23), run=run, send=send)
    assert sent == ['backup_failed.messaging']
    h.check(cfg, now=NOW + timedelta(hours=24), run=run, send=send)
    assert sent == ['backup_failed.messaging'] * 2
    good_status(tmp_path, 'messaging', captured=NOW + timedelta(hours=24))
    h.check(cfg, now=NOW + timedelta(hours=25), run=run, send=send)
    status(tmp_path, 'messaging', state='error', last_snapshot_at=iso(NOW + timedelta(hours=24)))
    h.check(cfg, now=NOW + timedelta(hours=26), run=run, send=send)
    assert sent == ['backup_failed.messaging'] * 3


# spec: ANYM-807
@pytest.mark.parametrize('value', ['1e309', '-1e309', '1' + '0' * 400, 'true', '"x"'])
def test_a_corrupt_stored_time_never_silences_an_alert(tmp_path, value):
    cfg = config(tmp_path, profiles=['messaging', 'calls'])
    for p in ('messaging', 'calls'):
        status(tmp_path, p, state='error', last_snapshot_at=iso(NOW))
    (tmp_path / 'state' / 'state.json').write_text(
        '{"v":1,"codes":{"backup_failed.messaging":{"sent":%s,"attempted":null}}}' % value)
    sent = []
    h.check(cfg, now=NOW, run=systemctl(),
            send=lambda url, data, timeout: sent.append(json.loads(data.split(b'\n')[2])['tags']['error_code']) or True)
    assert set(sent) == {'backup_failed.messaging', 'backup_failed.calls'}


# spec: ANYM-807
@pytest.mark.parametrize('error', ['IncompleteRead', 'BadStatusLine', 'RemoteDisconnected'])
def test_an_http_protocol_error_is_a_failed_send_and_later_codes_still_go(tmp_path, monkeypatch, error):
    import http.client
    cfg = config(tmp_path, profiles=['messaging', 'calls'])
    for p in ('messaging', 'calls'):
        status(tmp_path, p, state='error', last_snapshot_at=iso(NOW))
    raised = {'IncompleteRead': http.client.IncompleteRead(b''), 'BadStatusLine': http.client.BadStatusLine('x'),
              'RemoteDisconnected': http.client.RemoteDisconnected('x')}[error]
    calls = []

    def urlopen(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise raised

        class Response:
            status = 202
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=-1): return b''
        return Response()
    monkeypatch.setattr(h.urllib.request, 'urlopen', urlopen)
    h.check(cfg, now=NOW, run=systemctl())
    codes = json.loads((tmp_path / 'state' / 'state.json').read_text())['codes']
    assert len(calls) == 2 and sorted(c['sent'] is None for c in codes.values()) == [False, True]


# spec: ANYM-807
def test_a_send_that_trickles_past_its_timeout_counts_as_failed_and_the_run_stays_bounded(tmp_path, monkeypatch):
    import time as clock
    cfg = config(tmp_path, profiles=['messaging'])
    status(tmp_path, 'messaging', state='error', last_snapshot_at=iso(NOW))
    monkeypatch.setattr(h, 'SEND_TIMEOUT_S', 0.2)
    started = clock.monotonic()
    h.check(cfg, now=NOW, run=systemctl(), send=lambda url, data, timeout: clock.sleep(3) or True)
    assert clock.monotonic() - started < 2
    assert json.loads((tmp_path / 'state' / 'state.json').read_text())['codes']['backup_failed.messaging']['sent'] is None


# spec: ANYM-807
def test_a_refused_send_stays_due_and_a_corrupt_state_file_is_replaced(tmp_path):
    cfg = config(tmp_path, profiles=['messaging'])
    status(tmp_path, 'messaging', state='error', last_snapshot_at=iso(NOW))
    (tmp_path / 'state' / 'state.json').write_text('{corrupt')
    answers = [False, True]
    h.check(cfg, now=NOW, run=systemctl(), send=lambda *a: answers.pop(0))
    entry = json.loads((tmp_path / 'state' / 'state.json').read_text())['codes']['backup_failed.messaging']
    assert entry['sent'] is None
    h.check(cfg, now=NOW + timedelta(hours=1), run=systemctl(), send=lambda *a: answers.pop(0))
    assert json.loads((tmp_path / 'state' / 'state.json').read_text())['codes']['backup_failed.messaging']['sent']
    assert (tmp_path / 'state' / 'state.json').stat().st_mode & 0o777 == 0o600


# spec: ANYM-807
@settings(max_examples=60, deadline=None)
@given(st.lists(st.sets(st.sampled_from(sorted(h.CODES))), min_size=1, max_size=8),
       st.lists(st.booleans(), min_size=8, max_size=8))
def test_any_sequence_of_checks_sends_each_episode_at_most_once_a_day(rounds, delivered):
    """Invariant: between two successful sends of one code in one episode lie at least REPEAT_S."""
    state, last = {}, {}
    for index, raised in enumerate(rounds):
        now = index * 3600.0
        state = {c: state.get(c, {'sent': None, 'attempted': None}) for c in raised}
        last = {c: t for c, t in last.items() if c in raised}
        for n, code in enumerate(h.due_codes(raised, state, now)):
            state[code]['attempted'] = now
            if delivered[n % 8]:
                assert code not in last or now - last[code] >= h.REPEAT_S
                state[code]['sent'] = last[code] = now


# spec: ANYM-807
@pytest.mark.parametrize('code', sorted(h.CODES))
def test_the_event_is_exactly_what_before_send_keeps_for_every_code(code, monkeypatch):
    monkeypatch.setenv('DEEDKIT_SOURCE_SHA', SHA)
    event = h.build_event(code, {'release_sha': SHA, 'environment': 'local-synthetic'}, 1793860000.0)
    assert telemetry.before_send(json.loads(json.dumps(event))) == event
    assert set(event) == {'platform', 'level', 'release', 'environment', 'tags', 'exception', 'fingerprint',
                          'event_id', 'timestamp', 'sdk'}
    assert event['fingerprint'] == ['backup-health', 'backup', code]
    assert event['exception']['values'][0]['stacktrace']['frames'][0]['filename'] == telemetry.FRAME_PREFIX + 'backup_health.py'


# spec: ANYM-807
def test_the_codes_are_the_sixteen_of_telemetry():
    assert len(h.CODES) == 16 and h.CODES == telemetry.BACKUP_HEALTH_CODES <= telemetry.CODES
    assert 'backup' in telemetry.COMPONENTS


# spec: ANYM-807
def test_the_request_equals_the_sdk_transports_request(monkeypatch):
    seen = {}

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return b''

    def urlopen(request, timeout):
        seen.update(method=request.get_method(), url=request.full_url, data=request.data,
                    headers={k.lower(): v for k, v in request.header_items()}, timeout=timeout)
        return Response()
    monkeypatch.setattr(h.urllib.request, 'urlopen', urlopen)
    monkeypatch.setattr(h.time, 'strftime', lambda fmt, t=None: '2026-11-06T21:37:02Z')
    event = h.build_event('backup_stale.calls', {'release_sha': SHA, 'environment': 'local-synthetic'}, 1.0)
    assert h.post(h.ingest_url(DSN), h.envelope(event), 5)
    sdk = {}
    from tiden_telemetry import _transport, _envelope
    monkeypatch.setattr(_transport.urllib.request, 'urlopen', lambda request, timeout: sdk.update(
        method=request.get_method(), url=request.full_url, data=request.data,
        headers={k.lower(): v for k, v in request.header_items()}) or Response())
    monkeypatch.setattr(_envelope.time, 'strftime', lambda fmt, t=None: '2026-11-06T21:37:02Z')
    Client(DSN, before_send=lambda e: event).capture_event(dict(event))
    assert seen['url'] == sdk['url'] == parse_dsn(DSN).ingest_url
    assert seen['method'] == sdk['method'] == 'POST' and seen['data'] == sdk['data']
    assert seen['headers'] == sdk['headers'] and seen['headers']['content-type'] == SDK_CONTENT_TYPE
    assert seen['timeout'] == 5


# spec: ANYM-807
@pytest.mark.parametrize('dsn', ['http://key@ingest.example.test/p', 'https://ingest.example.test/p',
                                 'https://key:secret@ingest.example.test/p', 'https://key@/p', 'https://key@host/'])
def test_a_dsn_that_is_not_https_with_a_user_and_no_password_is_refused(dsn):
    with pytest.raises(ValueError):
        h.ingest_url(dsn)


# spec: ANYM-807
def test_no_backup_unit_names_the_checker_and_the_checker_depends_on_nothing():
    for path in DEPLOY.glob('*.service'):
        if path.name != 'deedkit-backup-health.service':
            assert 'backup-health' not in path.read_text() and 'backup_health' not in path.read_text()
    for name in ('deedkit-backup-health.service', 'deedkit-backup-health.timer'):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = lambda option: option
        parser.read_string((DEPLOY / name).read_text())
        for section in parser.sections():
            assert not {'Requires', 'Wants', 'After', 'Before', 'PartOf', 'BindsTo', 'Requisite'} & set(parser[section])
    service = (DEPLOY / 'deedkit-backup-health.service').read_text()
    assert 'TimeoutStartSec=90s' in service and 'ProtectSystem=strict' in service and 'Nice=10' in service
    assert 'OnCalendar=*-*-* *:37:00 UTC' in (DEPLOY / 'deedkit-backup-health.timer').read_text()


# spec: ANYM-807
def test_a_timing_out_probe_still_raises_another_profiles_error(tmp_path):
    cfg = config(tmp_path)
    status(tmp_path, 'recordings', state='error', last_snapshot_at=iso(NOW - timedelta(hours=1)))
    for p in ('messaging', 'calendar', 'calls'):
        good_status(tmp_path, p)
    run = systemctl(timeout=True)
    found = readings(cfg, run)
    assert found['recordings']['raised'] == {'backup_failed'}
    assert all(found[p]['word'] == 'UNKNOWN' and found[p]['raised'] == set() for p in ('messaging', 'calendar', 'calls'))
    assert run.calls[0][1]['timeout'] == h.PROBE_TIMEOUT_S and len(run.calls) == 1


# spec: ANYM-807
def test_an_unknown_kind_keeps_its_episode_instead_of_ending_it(tmp_path):
    cfg = config(tmp_path, profiles=['calendar'])
    good_status(tmp_path, 'calendar')
    run = systemctl({'deedkit-calendar-backup.service': ('failed', NOW)})
    h.check(cfg, now=NOW, run=run, send=lambda *a: True)
    h.check(cfg, now=NOW + timedelta(hours=1), run=systemctl(timeout=True), send=lambda *a: pytest.fail('repeat'))
    assert 'backup_failed.calendar' in json.loads((tmp_path / 'state' / 'state.json').read_text())['codes']


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# spec: ANYM-807
def test_eight_timing_out_sends_are_each_attempted_within_the_deadline_and_stay_due(tmp_path):
    cfg = config(tmp_path)
    for p in h.PROFILES:
        status(tmp_path, p, state='error')  # failed and stale: 8 codes
    clock, attempted = Clock(), []

    def send(url, data, timeout):
        attempted.append(timeout)
        clock.t += timeout
        return False
    h.check(cfg, now=NOW, run=systemctl(), send=send, clock=clock)
    assert len(attempted) == 8 and clock.t <= h.SEND_DEADLINE_S
    assert json.loads((tmp_path / 'state' / 'result.json').read_text())['profiles']['calls']['word'] == 'FAILED'
    codes = json.loads((tmp_path / 'state' / 'state.json').read_text())['codes']
    assert len(codes) == 8 and all(entry['sent'] is None for entry in codes.values())


# spec: ANYM-807
def test_a_later_code_is_sent_although_the_first_sends_keep_timing_out(tmp_path):
    cfg = config(tmp_path)
    for p in h.PROFILES:
        status(tmp_path, p, state='error')
    clock, sent = Clock(), set()
    stuck = {'backup_failed.calendar', 'backup_failed.calls', 'backup_failed.messaging'}

    def send(url, data, timeout):
        code = json.loads(data.split(b'\n')[2])['tags']['error_code']
        clock.t += timeout
        if code in stuck:
            return False
        sent.add(code)
        return True
    for hour in range(3):
        h.check(cfg, now=NOW + timedelta(hours=hour), run=systemctl(), send=send, clock=clock)
    assert sent == set(f'{k}.{p}' for k in ('backup_failed', 'backup_stale') for p in h.PROFILES) - stuck


# spec: ANYM-807
def test_the_runners_microsecond_time_reaches_the_status_line_in_whole_seconds(tmp_path):
    from scripts import production
    cfg = config(tmp_path, profiles=['messaging'])
    status(tmp_path, 'messaging', state='ok', last_snapshot_at='2026-11-06T18:00:19.126354+00:00',
           last_success_at='2026-11-06T19:10:02.000001+00:00')
    h.check(cfg, now=NOW, run=systemctl(), send=lambda *a: True)
    result = tmp_path / 'state' / 'result.json'
    assert json.loads(result.read_text())['profiles']['messaging']['last_snapshot_at'] == '2026-11-06T18:00:19Z'
    line = production.backup_health_lines(result, at=NOW)[0]
    assert line == 'backup messaging: delivered last capture 2026-11-06T18:00:19Z age=3.6h (checked 2026-11-06T21:37:02Z)'


# spec: ANYM-807
def test_a_capture_time_that_overflows_in_utc_still_raises_stale(tmp_path):
    cfg = config(tmp_path, profiles=['messaging'])
    status(tmp_path, 'messaging', state='error', last_snapshot_at='9999-12-31T23:59:59-01:00')
    reading = readings(cfg)['messaging']
    assert reading['raised'] == {'backup_failed', 'backup_stale'} and reading['last_snapshot_at'] is None


# spec: ANYM-807
def test_the_result_file_is_written_before_any_send_and_is_readable_by_status(tmp_path):
    cfg = config(tmp_path, profiles=['messaging'])
    status(tmp_path, 'messaging', state='error')

    def send(*a):
        result = json.loads((tmp_path / 'state' / 'result.json').read_text())
        assert result['profiles']['messaging']['word'] == 'FAILED'
        return True
    h.check(cfg, now=NOW, run=systemctl(), send=send)
    assert (tmp_path / 'state' / 'result.json').stat().st_mode & 0o777 == 0o644


# spec: ANYM-807
def test_an_unexpected_failure_exits_1_with_one_fixed_line(tmp_path, monkeypatch, capsys):
    config(tmp_path)
    monkeypatch.setattr(h, 'check', lambda cfg: (_ for _ in ()).throw(RuntimeError('/private/path')))
    assert h.main(['--config', str(tmp_path / 'producer.json')]) == 1
    assert capsys.readouterr().err == h.UNAVAILABLE + '\n'


# spec: ANYM-807
def test_print_shows_one_line_a_profile_and_sends_nothing(tmp_path, monkeypatch):
    config(tmp_path, 'verifier', ['calls'])
    verified(tmp_path, 'calls', datetime.now(timezone.utc) - timedelta(hours=3))
    monkeypatch.setattr(h, 'post', lambda *a: pytest.fail('sent'))
    monkeypatch.setattr(h.subprocess, 'run', systemctl())
    out = []
    assert h.main(['--config', str(tmp_path / 'verifier.json'), '--print'], out=out.append) == 0
    assert len(out) == 1 and out[0].startswith('backup calls: verified last capture ')
    assert not (tmp_path / 'state' / 'state.json').exists()


# spec: ANYM-807
@settings(max_examples=80, deadline=None)
@given(st.binary(max_size=200) | st.recursive(st.none() | st.booleans() | st.integers() | st.text(max_size=40),
                                              lambda inner: st.lists(inner) | st.dictionaries(st.text(max_size=20), inner),
                                              max_leaves=8).map(lambda v: json.dumps(v).encode()))
def test_any_status_file_gives_one_reading_and_no_error(tmp_path_factory, content):
    tmp_path = tmp_path_factory.mktemp('any')
    cfg = config(tmp_path, profiles=['calls'])
    (tmp_path / 'calls-status.json').write_bytes(content)
    reading = readings(cfg)['calls']
    assert reading['word'] in ('delivered', 'FAILED', 'STALE', 'UNKNOWN') and reading['raised'] <= set(h.KINDS['producer'])


# spec: ANYM-807
def test_telemetry_md_lists_the_codes():
    text = (ROOT / 'TELEMETRY.md').read_text()
    for kind in ('backup_failed', 'backup_stale', 'backup_verify_failed', 'backup_unverified'):
        assert f'`{kind}.<profile>`' in text
    assert '["backup-health", "backup", <code>]' in text
