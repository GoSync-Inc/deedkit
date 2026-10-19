"""Synthetic privacy, delivery isolation and gateway instrumentation checks."""
import builtins
import json
import threading
import time

import pytest
from starlette.testclient import TestClient
from tiden_telemetry import Client

from messaging_service import telemetry
from messaging_service.app import create_app
from messaging_service.store import APIError

SECRET = 'synthetic-private-token phone=+123456 chat=secret passport=example'


class Transport:
    def __init__(self):
        self.envelopes = []
        self.ready = threading.Event()

    def send(self, envelope):
        self.envelopes.append(envelope)
        self.ready.set()


def error():
    try:
        # A public code location, but exception text and locals contain private data.
        exec(compile('raise RuntimeError(secret)', str(telemetry.ROOT / 'app.py'), 'exec'),
             {'secret': SECRET})
    except RuntimeError as exc:
        return exc


def reporter(transport, component='gateway'):
    client = Client('https://synthetic-key@ingest.example.test/synthetic-project',
                    before_send=telemetry.before_send, transport=transport)
    return telemetry.Reporter(client, component, 'local-synthetic')


def test_sdk_wire_has_only_allowlisted_diagnostics():
    transport = Transport()
    worker = reporter(transport)
    try:
        assert worker.capture(error(), 'request_failed')
        assert transport.ready.wait(1)
    finally:
        worker.close()
    raw = transport.envelopes[0].decode()
    event = json.loads(raw.splitlines()[2])
    assert SECRET not in raw and 'synthetic-key' not in raw
    assert 'abs_path' not in raw and 'locals' not in raw and 'request' not in event
    assert event['exception']['values'][0]['stacktrace']['frames'] == [
        {'filename': 'services/messaging/messaging_service/app.py', 'lineno': 1, 'in_app': True}]
    assert event['tags'] == {'component': 'gateway', 'error_code': 'request_failed'}
    assert event['environment'] == 'local-synthetic'


def test_exception_stringification_is_never_called():
    class DangerousError(Exception):
        def __str__(self):
            raise AssertionError('must not stringify')
    event = telemetry.safe_event(DangerousError(SECRET), 'gateway', 'request_failed')
    assert event['exception']['values'][0]['type'] == 'UnexpectedError'
    assert SECRET not in json.dumps(event)


def test_before_send_removes_nested_extras():
    event = telemetry.safe_event(error(), 'gateway', 'request_failed')
    event.update(event_id='a'*32, timestamp=1, environment='local-synthetic', request={'body':SECRET},
                 breadcrumbs=[SECRET], user={'email':SECRET}, sdk={'secret':SECRET})
    event['tags']['account'] = SECRET
    value = event['exception']['values'][0]
    value.update(value=SECRET, secret=SECRET)
    value['stacktrace']['frames'][0].update(abs_path=SECRET, vars={'secret':SECRET})
    event['exception']['values'].append({'value': SECRET})
    result = telemetry.before_send(event)
    assert result is not None and SECRET not in json.dumps(result)


@pytest.mark.parametrize('field,value', [('tags', {'component':SECRET,'error_code':'request_failed'}),
    ('environment',SECRET), ('event_id',SECRET), ('timestamp',float('nan')), ('exception', {})])
def test_invalid_events_fail_closed(field, value):
    event = telemetry.safe_event(error(), 'gateway', 'request_failed')
    event.update(event_id='b'*32, timestamp=1, environment='local-synthetic')
    event[field] = value
    assert telemetry.before_send(event) is None


SOURCE_SHA = '0123456789abcdef0123456789abcdef01234567'


def test_release_names_the_exact_source_revision(monkeypatch):
    monkeypatch.setenv('DEEDKIT_SOURCE_SHA', SOURCE_SHA)
    transport = Transport()
    worker = reporter(transport)
    try:
        assert worker.capture(error(), 'request_failed')
        assert transport.ready.wait(1)
    finally:
        worker.close()
    event = json.loads(transport.envelopes[0].decode().splitlines()[2])
    assert event['release'] == 'deedkit@' + SOURCE_SHA


@pytest.mark.parametrize('value', [None, '', SOURCE_SHA[:12], SOURCE_SHA.upper(), SOURCE_SHA + '0',
                                   SOURCE_SHA + '\n', 'g' * 40, SECRET])
def test_release_without_a_valid_source_revision_keeps_the_package_version(value, monkeypatch):
    if value is None:
        monkeypatch.delenv('DEEDKIT_SOURCE_SHA', raising=False)
    else:
        monkeypatch.setenv('DEEDKIT_SOURCE_SHA', value)
    event = telemetry.safe_event(error(), 'gateway', 'request_failed')
    event.update(event_id='c'*32, timestamp=1, environment='production')
    result = telemetry.before_send(event)
    assert result['release'] == 'anymsg@' + telemetry.__version__
    assert SECRET not in json.dumps(result) and 'deedkit@' not in json.dumps(result)


def test_frames_use_repository_root_paths_only():
    event = telemetry.safe_event(error(), 'gateway', 'request_failed')
    assert [frame['filename'] for frame in event['exception']['values'][0]['stacktrace']['frames']] == [
        'services/messaging/messaging_service/app.py']
    event.update(event_id='d'*32, timestamp=1, environment='production')
    event['exception']['values'][0]['stacktrace']['frames'] = [
        {'filename': name, 'lineno': 7, 'in_app': True} for name in (
            'messaging_service/app.py', 'services/messaging/messaging_service/app.py',
            'services/messaging/messaging_service/missing.py', str(telemetry.ROOT / 'app.py'),
            'services/gateway/agent_gateway/app.py')]
    frames = telemetry.before_send(event)['exception']['values'][0]['stacktrace']['frames']
    assert frames == [{'filename': 'services/messaging/messaging_service/app.py', 'lineno': 7, 'in_app': True}]


@pytest.mark.parametrize('environment,accepted', [('production', True), ('local-synthetic', True),
    ('staging', False), ('synthetic', False), ('github-actions-synthetic', False), ('Production', False)])
def test_only_production_and_local_synthetic_environments_are_accepted(environment, accepted):
    event = telemetry.safe_event(error(), 'gateway', 'request_failed')
    event.update(event_id='e'*32, timestamp=1, environment=environment)
    result = telemetry.before_send(event)
    assert (result is not None and result['environment'] == environment) is accepted
    client = Client('https://synthetic-key@ingest.example.test/synthetic-project',
                    before_send=telemetry.before_send, transport=Transport())
    if accepted:
        telemetry.Reporter(client, 'gateway', environment).close()
    else:
        with pytest.raises(ValueError):
            telemetry.Reporter(client, 'gateway', environment)


@pytest.mark.parametrize('environment,expected', [(None, 'production'), ('local-synthetic', 'local-synthetic'),
                                                  ('staging', None), ('synthetic', None)])
def test_configured_environment_defaults_to_production_and_refuses_others(
        environment, expected, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(telemetry, '_reporter', None)
    monkeypatch.setattr(telemetry.sys, 'excepthook', telemetry.sys.excepthook)
    monkeypatch.setattr(telemetry.threading, 'excepthook', telemetry.threading.excepthook)
    monkeypatch.setattr(telemetry.atexit, 'register', lambda callback: None)
    dsn = tmp_path/'test.dsn'
    dsn.write_text('https://synthetic-key@ingest.example.test/synthetic-project')
    monkeypatch.setenv('ANYMSG_TIDEN_ENABLED', '1')
    monkeypatch.setenv('ANYMSG_TIDEN_DSN_FILE', str(dsn))
    if environment is None:
        monkeypatch.delenv('ANYMSG_TIDEN_ENVIRONMENT', raising=False)
    else:
        monkeypatch.setenv('ANYMSG_TIDEN_ENVIRONMENT', environment)
    try:
        assert telemetry.configure('gateway') is (expected is not None)
        if expected is None:
            assert telemetry._reporter is None
            assert 'telemetry_configuration_unavailable' in caplog.text
            assert environment not in caplog.text
        else:
            assert telemetry._reporter.environment == expected
    finally:
        if telemetry._reporter is not None:
            telemetry._reporter.close()


def test_blocked_network_never_blocks_capture_and_close_is_bounded():
    class SlowTransport(Transport):
        release = threading.Event()
        def send(self, envelope):
            self.ready.set()
            self.release.wait(5)
    transport = SlowTransport()
    worker = reporter(transport)
    try:
        start = time.monotonic()
        assert worker.capture(error(), 'request_failed')
        assert time.monotonic() - start < 0.1
        assert transport.ready.wait(1)
        # A second independent event is accepted while HTTP is stalled.
        start = time.monotonic()
        assert worker.capture(error(), 'tool_failed')
        assert time.monotonic() - start < 0.1
        worker.close()
        assert worker.thread.is_alive()
        assert not worker.capture(error(), 'indexer_failed')
    finally:
        transport.release.set()
        worker.thread.join(timeout=2)
    assert not worker.thread.is_alive()


def test_deduplication_and_rate_budget():
    transport = Transport()
    worker = reporter(transport)
    try:
        assert worker.capture(error(), 'request_failed')
        assert not worker.capture(error(), 'request_failed')
        with worker.lock:
            worker.count = 20
        assert not worker.capture(error(), 'tool_failed')
        assert worker.queue.maxsize == 32
        assert not worker.capture(error(), SECRET)
    finally:
        worker.close()


def test_disabled_configuration_has_no_import_or_thread(monkeypatch):
    monkeypatch.setattr(telemetry, '_reporter', None)
    monkeypatch.delenv('ANYMSG_TIDEN_ENABLED', raising=False)
    previous = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert name != 'tiden_telemetry'
        return previous(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    assert not telemetry.configure('gateway')
    assert telemetry._reporter is None


@pytest.mark.parametrize('dsn', ['http://key@example.test/p', 'https://example.test/p',
                                'https://key:password@example.test/p'])
def test_invalid_dsn_does_not_crash_or_expose_configuration(dsn, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(telemetry, '_reporter', None)
    filename = tmp_path/'test.dsn'
    filename.write_text(dsn)
    monkeypatch.setenv('ANYMSG_TIDEN_ENABLED', '1')
    monkeypatch.setenv('ANYMSG_TIDEN_DSN_FILE', str(filename))
    assert not telemetry.configure('gateway')
    assert dsn not in caplog.text
    assert 'telemetry_configuration_unavailable' in caplog.text


@pytest.mark.parametrize('channel', ['rest','mcp'])
@pytest.mark.parametrize('unexpected', [True,False])
def test_gateway_reports_unexpected_errors_only(channel, unexpected, monkeypatch):
    class Store:
        public_url = 'http://testserver'
        def authenticate(self, token): return 'synthetic'
        def audit_boundary(self, code): pass
        def capabilities(self, token):
            if unexpected:
                raise RuntimeError(SECRET)
            raise APIError('not_found',404)
    transport = Transport()
    worker = reporter(transport)
    monkeypatch.setattr(telemetry, '_reporter', worker)
    try:
        with TestClient(create_app(Store(), ['testserver'])) as client:
            if channel == 'rest':
                response = client.post('/v1/capabilities',json={},headers={'Authorization':'Bearer synthetic'})
                # The server-error handler responds before Starlette's fallback.
                assert response.status_code == (503 if unexpected else 404)
                if unexpected:
                    assert response.json()['error'] == 'temporarily_unavailable'
            else:
                response = client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/call',
                    'params':{'name':'capabilities','arguments':{}}},headers={
                        'Authorization':'Bearer synthetic','Accept':'application/json, text/event-stream'})
                assert response.json()['result']['isError']
            assert SECRET not in response.text
    finally:
        worker.close()
    assert bool(transport.envelopes) == unexpected
    assert SECRET not in repr(transport.envelopes)


def stopped(component, monkeypatch, text, detail=None, stop=None):
    """Run a collector's main against a synthetic stop (or a prepared exception)."""
    import sys
    from pathlib import Path
    from messaging_service import telegram_collector, whatsapp_collector
    collector = {'telegram': telegram_collector, 'whatsapp': whatsapp_collector}[component]
    monkeypatch.setattr(telemetry, 'configure', lambda component: None)
    async def failed(config):
        assert config == Path('/synthetic/config.json')
        try:
            raise OSError(SECRET)
        except OSError as cause:
            error = stop
            if error is None:
                error = RuntimeError(text)
                error.detail = detail
            raise error from cause
    monkeypatch.setattr(collector, 'run_config', failed)
    monkeypatch.setattr(collector.os, 'umask', lambda mode: None)
    monkeypatch.setattr(sys, 'argv', ['anymsg', '--config', '/synthetic/config.json'])
    with pytest.raises(SystemExit) as result:
        collector.main()
    assert result.value.code == 1


@pytest.mark.parametrize('component,text,detail,code,line', [
    ('telegram', 'capture_overload', None, 'capture_overload', {'code': 'capture_overload'}),
    ('telegram', 'provider_disconnected', SECRET, 'provider_disconnected', {'code': 'provider_disconnected'}),
    ('telegram', 'durable_capture_failed', 'spool_io_error', 'durable_capture_failed.spool_io_error',
     {'code': 'durable_capture_failed', 'detail': 'spool_io_error'}),
    ('telegram', 'durable_capture_failed', 'disk_reserve', 'durable_capture_failed.disk_reserve',
     {'code': 'durable_capture_failed', 'detail': 'disk_reserve'}),
    ('telegram', 'durable_capture_failed', SECRET, 'durable_capture_failed', {'code': 'durable_capture_failed'}),
    ('telegram', 'durable_capture_failed', None, 'durable_capture_failed', {'code': 'durable_capture_failed'}),
    ('telegram', 'durable_capture_failed', ['spool_full'], 'durable_capture_failed', {'code': 'durable_capture_failed'}),
    # A connection code reaches Tiden; the supervisor line keeps its own smaller vocabulary.
    ('telegram', 'spool_corrupt', None, 'spool_corrupt', {'code': 'collector_failed'}),
    ('telegram', 'durable_capture_failed.spool_full', None, 'collector_failed', {'code': 'collector_failed'}),
    ('telegram', SECRET, 'spool_full', 'collector_failed', {'code': 'collector_failed'}),
    ('whatsapp', 'provider_disconnected', None, 'provider_disconnected', {'code': 'provider_disconnected'}),
    ('whatsapp', 'whatsapp_logged_out', None, 'whatsapp_logged_out', {'code': 'collector_failed'}),
    ('whatsapp', 'durable_capture_failed', None, 'durable_capture_failed', {'code': 'collector_failed'}),
    # Only Telegram records a fixed capture detail; WhatsApp never qualifies its code.
    ('whatsapp', 'durable_capture_failed', 'spool_full', 'durable_capture_failed', {'code': 'collector_failed'}),
    ('whatsapp', SECRET, None, 'collector_failed', {'code': 'collector_failed'}),
])
def test_collector_stop_reports_the_fixed_stop_code(component, text, detail, code, line, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(telemetry, 'capture', lambda error, code: seen.append(code))
    stopped(component, monkeypatch, text, detail)
    assert seen == [code]
    assert json.loads(capsys.readouterr().out) == {'status': 'stopped', **line}


# spec: ANYM-291
@pytest.mark.parametrize('detail,line', [
    ('waiters_limit', {'code': 'capture_overload', 'detail': 'waiters_limit'}),
    ('slot_wait_timeout', {'code': 'capture_overload', 'detail': 'slot_wait_timeout'}),
    ('spool_full', {'code': 'capture_overload'}), (SECRET, {'code': 'capture_overload'}),
])
def test_capture_overload_stop_line_names_the_bound_and_telemetry_stays_unqualified(detail, line, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(telemetry, 'capture', lambda error, code: seen.append(code))
    stopped('telegram', monkeypatch, 'capture_overload', detail)
    assert seen == ['capture_overload']
    assert json.loads(capsys.readouterr().out) == {'status': 'stopped', **line}


class LooksSafe(str):
    """Private text whose hash and equality pretend to be an allowlisted code."""
    def __new__(cls, text, target):
        value = super().__new__(cls, text)
        value.target = target
        return value
    def __hash__(self):
        return hash(self.target)
    def __eq__(self, other):
        return other == self.target
    def __add__(self, other):
        return LooksSafe(str.__add__(self, other), self.target + other)


class Unhashable(str):
    __hash__ = None


def odd_stop(kind):
    class TextRaises(Exception):
        def __str__(self):
            raise AssertionError(SECRET)
    class TextLooksSafe(Exception):
        def __str__(self):
            return LooksSafe(SECRET, 'capture_overload')
    class DetailRaises(RuntimeError):
        @property
        def detail(self):
            raise ValueError(SECRET)
    if kind == 'text_raises':
        return TextRaises(SECRET)
    if kind == 'text_looks_safe':
        return TextLooksSafe(SECRET)
    if kind == 'detail_raises':
        return DetailRaises('durable_capture_failed')
    stop = RuntimeError('durable_capture_failed')
    stop.detail = {'detail_looks_safe': LooksSafe(SECRET, 'spool_full'), 'detail_unhashable': Unhashable('spool_full')}[kind]
    return stop


@pytest.mark.parametrize('kind,code', [('text_raises', 'collector_failed'), ('text_looks_safe', 'collector_failed'),
    ('detail_raises', 'durable_capture_failed'), ('detail_looks_safe', 'durable_capture_failed'),
    ('detail_unhashable', 'durable_capture_failed')])
def test_collector_stop_with_odd_text_or_detail_still_reports_a_fixed_code(kind, code, monkeypatch, capsys):
    transport = Transport()
    worker = reporter(transport, 'telegram')
    monkeypatch.setattr(telemetry, '_reporter', worker)
    try:
        stopped('telegram', monkeypatch, None, stop=odd_stop(kind))
        assert transport.ready.wait(1)
    finally:
        worker.close()
    raw = transport.envelopes[0].decode()
    event = json.loads(raw.splitlines()[2])
    assert SECRET not in raw and 'synthetic-private-token' not in raw
    assert event['tags'] == {'component': 'telegram', 'error_code': code}
    assert event['exception']['values'][0]['value'] == code
    line = capsys.readouterr().out
    assert SECRET not in line and json.loads(line) == {'status': 'stopped', 'code': code}


@pytest.mark.parametrize('component,text,detail,code', [
    ('telegram', 'durable_capture_failed', 'spool_full', 'durable_capture_failed.spool_full'),
    ('telegram', 'durable_capture_failed', SECRET, 'durable_capture_failed'),
    ('telegram', 'capture_overload', SECRET, 'capture_overload'),
    ('telegram', SECRET, SECRET, 'collector_failed'),
    ('whatsapp', 'whatsapp_logged_out', SECRET, 'whatsapp_logged_out'),
    ('whatsapp', 'durable_capture_failed', 'spool_full', 'durable_capture_failed'),
    ('whatsapp', SECRET, SECRET, 'collector_failed'),
])
def test_collector_stop_event_on_the_wire_has_no_exception_text(component, text, detail, code, monkeypatch):
    transport = Transport()
    worker = reporter(transport, component)
    monkeypatch.setattr(telemetry, '_reporter', worker)
    try:
        stopped(component, monkeypatch, text, detail)
        assert transport.ready.wait(1)
    finally:
        worker.close()
    raw = transport.envelopes[0].decode()
    event = json.loads(raw.splitlines()[2])
    assert SECRET not in raw and 'synthetic-private-token' not in raw
    assert event['tags'] == {'component': component, 'error_code': code}
    value = event['exception']['values'][0]
    assert value['type'] == 'RuntimeError' and value['value'] == code
    assert {frame['filename'] for frame in value['stacktrace']['frames']} <= {
        telemetry.FRAME_PREFIX + component + '_collector.py'}


def test_stop_codes_are_allowlisted_for_collectors_only():
    from messaging_service.collector_state import CAPTURE_STOP_DETAILS
    from messaging_service.operational import CONNECTION_ERRORS, SUPERVISOR_STOP_CODES
    durable = {'durable_capture_failed.' + detail for detail in CAPTURE_STOP_DETAILS}
    assert telemetry.STOP_CODES['telegram'] == SUPERVISOR_STOP_CODES['telegram'] | CONNECTION_ERRORS | durable
    assert telemetry.STOP_CODES['whatsapp'] == SUPERVISOR_STOP_CODES['whatsapp'] | CONNECTION_ERRORS
    assert telemetry.safe_event(error(), 'telegram', 'durable_capture_failed.spool_io_error')
    assert telemetry.safe_event(error(), 'whatsapp', 'durable_capture_failed.spool_io_error') is None
    for component in ('telegram', 'whatsapp'):
        assert telemetry.safe_event(error(), component, 'durable_capture_failed.' + SECRET) is None
        assert telemetry.safe_event(error(), component, SECRET) is None
        assert telemetry.safe_event(error(), component, LooksSafe(SECRET, 'capture_overload')) is None
    for component in telemetry.COMPONENTS - {'telegram', 'whatsapp'}:
        assert telemetry.safe_event(error(), component, 'capture_overload') is None
    event = telemetry.safe_event(error(), 'telegram', 'capture_overload')
    event.update(event_id='c'*32, timestamp=1, environment='local-synthetic')
    assert telemetry.before_send(event)['tags']['error_code'] == 'capture_overload'
    for component, code in [('telegram', 'durable_capture_failed.' + SECRET), ('gateway', 'capture_overload'),
                            ('whatsapp', 'durable_capture_failed.spool_full'),
                            ('telegram', LooksSafe(SECRET, 'capture_overload'))]:
        event['tags'].update(component=component, error_code=code)
        assert telemetry.before_send(event) is None
    event['tags'].update(component='telegram', error_code='capture_overload')
    event['environment'] = LooksSafe(SECRET, 'local-synthetic')
    assert telemetry.before_send(event) is None


def test_telemetry_document_lists_the_stop_vocabulary():
    import re
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1]/'TELEMETRY.md').read_text()
    def listed(title):
        section = re.search(rf'^{title}: (.+?)\.$', text, re.M | re.S)
        return set(re.findall(r'`([a-z_.]+)`', section.group(1)))
    # Both collectors share the stop codes; a split must be documented.
    assert listed('Collector stop codes') == telemetry.STOP_CODES['whatsapp']
    assert listed('Collector stop codes') | listed('Telegram only') == telemetry.STOP_CODES['telegram']


def stop_event(component, text, detail, monkeypatch):
    """The decoded envelope event of one collector stop sent through the real SDK."""
    transport = Transport()
    worker = reporter(transport, component)
    monkeypatch.setattr(telemetry, '_reporter', worker)
    try:
        stopped(component, monkeypatch, text, detail)
        assert transport.ready.wait(1)
    finally:
        worker.close()
    return json.loads(transport.envelopes[0].decode().splitlines()[2])


# spec: ANYM-410
@pytest.mark.parametrize('component,text,detail,code', [
    ('telegram', 'capture_overload', None, 'capture_overload'),
    ('telegram', 'durable_capture_failed', None, 'durable_capture_failed'),
    ('telegram', 'durable_capture_failed', 'spool_full', 'durable_capture_failed.spool_full'),
    ('telegram', SECRET, None, 'collector_failed'),
    ('whatsapp', 'provider_disconnected', None, 'provider_disconnected'),
    ('whatsapp', SECRET, None, 'collector_failed'),
])
def test_collector_stop_event_carries_its_code_as_the_fingerprint(component, text, detail, code, monkeypatch):
    event = stop_event(component, text, detail, monkeypatch)
    assert event['fingerprint'] == ['collector-stop', component, code]
    assert 'title' not in event


# spec: ANYM-410
def test_two_stop_codes_give_two_fingerprints_and_one_code_keeps_its_own(monkeypatch):
    fingerprints = {code: stop_event('telegram', text, detail, monkeypatch)['fingerprint']
                    for code, text, detail in [('capture_overload', 'capture_overload', None),
                                               ('durable_capture_failed', 'durable_capture_failed', None),
                                               ('durable_capture_failed.disk_reserve', 'durable_capture_failed', 'disk_reserve'),
                                               ('durable_capture_failed.spool_full', 'durable_capture_failed', 'spool_full')]}
    assert len({tuple(value) for value in fingerprints.values()}) == 4
    # The same code under another exception type and other frames keeps one fingerprint.
    other = compile_error('store.py')
    first = telemetry.safe_event(error(), 'telegram', 'capture_overload')
    second = telemetry.safe_event(other, 'telegram', 'capture_overload')
    assert first['exception']['values'][0]['type'] != second['exception']['values'][0]['type']
    assert first['exception']['values'][0]['stacktrace'] != second['exception']['values'][0]['stacktrace']
    assert first['fingerprint'] == second['fingerprint'] == fingerprints['capture_overload']


def compile_error(name):
    try:
        exec(compile('raise ValueError(secret)', str(telemetry.ROOT / name), 'exec'), {'secret': SECRET})
    except ValueError as exc:
        return exc


# spec: ANYM-410
@pytest.mark.parametrize('injected', [
    ['{{ default }}', SECRET], 'collector-stop', 5, [], [SECRET], ['collector-stop', 'telegram', SECRET], {'x': SECRET}])
def test_an_injected_fingerprint_or_title_never_reaches_the_wire(injected):
    transport = Transport()
    client = Client('https://synthetic-key@ingest.example.test/synthetic-project',
                    before_send=telemetry.before_send, transport=transport)
    for component, code, expected in [('telegram', 'capture_overload', ['collector-stop', 'telegram', 'capture_overload']),
                                      ('gateway', 'request_failed', None)]:
        event = telemetry.safe_event(error(), component, code)
        event.update(event_id='d'*32, timestamp=1, environment='local-synthetic', fingerprint=injected, title=SECRET)
        client.capture_event(event)
        sent = json.loads(transport.envelopes[-1].decode().splitlines()[2])
        assert SECRET not in transport.envelopes[-1].decode() and 'title' not in sent
        assert sent.get('fingerprint') == expected


# spec: ANYM-410
def test_unrelated_diagnostics_carry_no_fingerprint():
    for component in telemetry.COMPONENTS - {'telegram', 'whatsapp'}:
        assert 'fingerprint' not in telemetry.safe_event(error(), component, 'request_failed')
    # A collector's non-stop capture still names its code, never a title.
    event = telemetry.safe_event(error(), 'telegram', 'uncaught_exception')
    assert event['fingerprint'] == ['collector-stop', 'telegram', 'uncaught_exception'] and 'title' not in event
    # An invalid component or code builds no event, so no fingerprint is ever formed from it.
    assert telemetry.safe_event(error(), 'telegram', SECRET) is None
    assert telemetry.safe_event(error(), 'gateway', 'capture_overload') is None


# spec: ANYM-410
def test_telemetry_document_states_the_fingerprint_and_grouping_rule():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1]/'TELEMETRY.md').read_text()
    assert '`fingerprint`' in text and '["collector-stop", <component>, <code>]' in text
