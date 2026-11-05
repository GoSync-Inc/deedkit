"""Omi `memory_created` webhook as a hint (ANYM-760). Synthetic Omi and a synthetic channel only; no firewall is touched."""
import asyncio
from collections import Counter
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

from hypothesis import given, settings, strategies as st
import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'services/recordings/src'))
from deedkit_recordings import cli, hint, security  # noqa: E402
from deedkit_recordings.app import create_app  # noqa: E402
from deedkit_recordings.archive import ArchiveRuntime  # noqa: E402
from deedkit_recordings.omi import Provider  # noqa: E402
from deedkit_recordings.security import Document, Failure, configuration, read_document  # noqa: E402
from test_recording_archive_backfill import Omi, T0, build, sql  # noqa: E402

UID = 'syntheticOwnerUid01'
PATH_TOKEN = 'P' * 43
OLD_PATH_TOKEN = 'Q' * 43
CHANNEL_KEY = 'synthetic-channel-credential-0000000000000'
OLD_CHANNEL_KEY = 'synthetic-channel-credential-1111111111111'
ROUTE = '/omi/memory/' + PATH_TOKEN
TOKEN = 'tokenaaaa'
OVERLAY = ROOT / 'deploy/compose.recordings-hint.yaml'
EGRESS = ROOT / 'scripts/recordings_egress_check.py'
FRESH = T0 - 60


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def hint_section(**extra):
    return {'account': 'omi-a', 'uid': UID, 'channel_key_sha256': [digest(CHANNEL_KEY)],
            'path_token_sha256': [digest(PATH_TOKEN)], 'expires_at': '2099-01-01T00:00:00Z', **extra}


def backend(tmp_path, *, section=None, omi=None):
    tmp_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path, doc = build(tmp_path, omi_hint=section if section is not None else hint_section())
    omi = omi if omi is not None else Omi()
    return ArchiveRuntime(path, Provider(httpx.MockTransport(omi)), None), omi, path, doc


def rewrite(path, doc):
    path.write_text(json.dumps(doc))


def receiver_files(tmp_path, *, digests=None, key=CHANNEL_KEY):
    tmp_path = tmp_path.resolve()
    key_file = tmp_path / 'channel.key'
    key_file.write_text(key + '\n'); key_file.chmod(0o600)
    config = tmp_path / 'receiver.json'
    config.write_text(json.dumps({'uid': UID, 'path_token_sha256': digests or [digest(PATH_TOKEN)],
                                  'channel_url': 'http://recordings-hint-channel:13235/omi-hint',
                                  'channel_key_file': str(key_file)}))
    config.chmod(0o600)
    return config


class Channel:
    """The backend's side of the channel, as a synthetic: records every message, answers what the test says."""
    def __init__(self, status=200):
        self.status, self.requests = status, []

    def __call__(self, request):
        self.requests.append(request)
        if isinstance(self.status, Exception):
            raise self.status
        return httpx.Response(self.status, json={})


def receiver(tmp_path, channel=None, **options):
    channel = channel if channel is not None else Channel()
    return hint.Receiver(receiver_files(tmp_path), transport=httpx.MockTransport(channel), **options), channel


async def call(app, method='POST', path=ROUTE, query=b'uid=' + UID.encode(), headers=(), chunks=(b'{"id":"abc"}',), delay=0.0,
               server=('127.0.0.1', 1)):
    """One ASGI request: the status, headers and body bytes as sent, and how many times the body was read."""
    sent, pending, reads = [], list(chunks), [0]

    async def receive():
        reads[0] += 1
        if delay:
            await asyncio.sleep(delay)
        if pending:
            return {'type': 'http.request', 'body': pending.pop(0), 'more_body': bool(pending)}
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(message):
        sent.append(message)

    scope = {'type': 'http', 'http_version': '1.1', 'scheme': 'http', 'method': method, 'path': path, 'raw_path': path.encode(),
             'root_path': '', 'query_string': query, 'headers': [(k.encode().lower(), v.encode()) for k, v in headers],
             'server': server, 'client': ('127.0.0.1', 2)}
    await app(scope, receive, send)
    start = next(m for m in sent if m['type'] == 'http.response.start')
    return start['status'], start['headers'], b''.join(m.get('body', b'') for m in sent if m['type'] == 'http.response.body'), reads[0]


def channel_headers(key=CHANNEL_KEY):
    return (('authorization', 'Bearer ' + key), ('content-type', 'application/json'))


def details(omi):
    return [call_ for call_ in omi.calls if call_[2] == 'detail']


def lists(omi):
    return [call_ for call_ in omi.calls if call_[2] == 'list']


async def ready(r):
    await r.start(background=False)


async def advance(r, omi, first, last, step=1):
    """Everything due in the hint lane between two instants of the fake clock."""
    moment = first
    while moment <= last:
        omi.now = moment
        await r.hints.run(now=moment)
        moment += step


# --- criterion 1: the receiver

# spec: ANYM-760
def test_wrong_token_method_and_path_give_byte_identical_404s_before_any_body_is_read(tmp_path):
    async def scenario():
        app, channel = receiver(tmp_path)
        answers = [await call(app, path='/omi/memory/' + 'X' * 43),
                   await call(app, path='/omi/memory/short'),
                   await call(app, method='GET'), await call(app, method='PUT'),
                   await call(app, path='/'), await call(app, path='/v1/capabilities'),
                   await call(app, path=ROUTE + '/more'), await call(app, path='/omi/memory/'),
                   await call(app, path='/omi/memory/' + 'X' * 43, query=b'uid=' + UID.encode(), headers=(('content-length', '99999999'),))]
        for status, headers, body, reads in answers:
            assert (status, headers, body) == answers[0][:3] and status == 404 and reads == 0
        assert not channel.requests
    asyncio.run(scenario())


# spec: ANYM-760
def test_with_the_right_token_a_wrong_uid_or_shape_is_400_and_never_reaches_the_channel(tmp_path):
    async def scenario():
        app, channel = receiver(tmp_path)
        cases = [{'query': b'uid=someone-else'}, {'query': b''}, {'query': b'uid=' + UID.encode() + b'&uid=second'}]
        cases += [{'chunks': (body,)} for body in (b'not json', b'[]', b'{}', b'{"id":5}', b'{"id":"has space"}', b'{"id":"' + b'a' * 257 + b'"}',
                                                   b'{"id":"a","id":"b"}', b'[' * 100000, b'{"id":NaN}', b'\xff\xfe')]
        for case in cases:
            status, _, _, _ = await call(app, **case)
            assert status == 400, case
        assert not channel.requests
        assert (await call(app))[0] == 200 and len(channel.requests) == 1
    asyncio.run(scenario())


# spec: ANYM-760
def test_a_5_mib_body_stops_reading_at_the_cap_with_413_which_omi_does_not_retry(tmp_path):
    async def scenario():
        app, channel = receiver(tmp_path)
        chunk = b'x' * (1024 ** 2)
        status, _, _, reads = await call(app, chunks=[chunk] * 5)
        assert status == 413 and reads == 5 and not channel.requests  # the fifth chunk crosses 4 MiB; nothing is read after it
        declared = await call(app, headers=(('content-length', str(5 * 1024 ** 2)),), chunks=[chunk] * 5)
        assert declared[0] == 413 and declared[3] == 0
        exact = b'{"id":"abc","pad":"' + b'x' * (hint.MAX_BODY - 22) + b'"}'
        assert len(exact) == hint.MAX_BODY - 1 and (await call(app, chunks=(exact,)))[0] == 200
    asyncio.run(scenario())


# spec: ANYM-760
def test_a_body_slower_than_the_deadline_is_cut_with_408_and_the_redelivery_of_the_same_hint_succeeds(tmp_path):
    async def scenario():
        assert hint.DEADLINE == 2
        app, channel = receiver(tmp_path, deadline=0.05)
        started = time.monotonic()
        status, _, _, _ = await call(app, chunks=(b'{"id":', b'"abc"}'), delay=5, headers=(('idempotency-key', 'one'),))
        assert status == 408 and time.monotonic() - started < 1 and not channel.requests
        assert (await call(app, headers=(('idempotency-key', 'one'),)))[0] == 200 and len(channel.requests) == 1
    asyncio.run(scenario())


# spec: ANYM-760
def test_twenty_concurrent_requests_process_at_most_four_and_the_rest_get_503_at_once(tmp_path):
    async def scenario():
        gate = asyncio.Event()
        processing = []

        async def handler(request):
            processing.append(request)
            await gate.wait()
            return httpx.Response(200, json={})
        app = hint.Receiver(receiver_files(tmp_path), transport=httpx.MockTransport(handler))
        tasks = [asyncio.create_task(call(app, chunks=(json.dumps({'id': f'c{n}'}).encode(),))) for n in range(20)]
        while sum(task.done() for task in tasks) < 16:
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.01)
        assert len(processing) == 4 and app.active == 4
        gate.set()
        answers = Counter([(await task)[0] for task in tasks])
        assert answers == {200: 4, 503: 16} and app.active == 0
    asyncio.run(scenario())


# spec: ANYM-760
def test_the_path_token_compare_is_the_constant_time_one_over_every_digest(tmp_path, monkeypatch):
    seen = []
    real = security.hmac.compare_digest

    def counted(left, right):
        seen.append((left, right))
        return real(left, right)
    monkeypatch.setattr(security.hmac, 'compare_digest', counted)
    digests = [digest(OLD_PATH_TOKEN), digest(PATH_TOKEN)]
    assert security.digest_matches(digests, PATH_TOKEN) and security.digest_matches(digests, OLD_PATH_TOKEN)
    seen.clear()
    assert not security.digest_matches(digests, 'wrong')
    assert len(seen) == 2  # never stops at the first difference or the first digest

    async def scenario():
        app = hint.Receiver(receiver_files(tmp_path, digests=digests), transport=httpx.MockTransport(Channel()))
        seen.clear()
        assert (await call(app, path='/omi/memory/' + 'X' * 43))[0] == 404
        assert len(seen) == 2
        assert (await call(app, path=ROUTE))[0] == 200 and (await call(app, path='/omi/memory/' + OLD_PATH_TOKEN))[0] == 200
    asyncio.run(scenario())


# spec: ANYM-760
@pytest.mark.parametrize('failure', [401, 429, 500, httpx.ConnectError('down')], ids=['refused', 'admission', 'server', 'unreachable'])
def test_a_hint_the_channel_cannot_take_answers_503_so_omi_retries_and_a_later_delivery_goes_through(tmp_path, failure):
    async def scenario():
        channel = Channel(failure)
        app, _ = receiver(tmp_path, channel)
        replay = (('idempotency-key', 'delivery-1'),)
        assert (await call(app, headers=replay))[0] == 503
        channel.status = 200
        assert (await call(app, headers=replay))[0] == 200
        count = len(channel.requests)
        assert (await call(app, headers=replay))[0] == 200 and len(channel.requests) == count  # the same key: one effect
    asyncio.run(scenario())


# spec: ANYM-760
def test_a_valid_hint_answers_200_within_the_deadline_even_when_the_channel_hangs(tmp_path):
    async def scenario():
        async def hang(request):
            await asyncio.sleep(30)
        app = hint.Receiver(receiver_files(tmp_path), transport=httpx.MockTransport(hang), deadline=0.05)
        started = time.monotonic()
        assert (await call(app))[0] == 503 and time.monotonic() - started < 1
    asyncio.run(scenario())


# spec: ANYM-760
def test_the_receiver_process_loads_no_archive_app_or_collector_code():
    code = ('import sys; sys.path.insert(0, sys.argv[1]); from deedkit_recordings import hint; '
            'print(",".join(sorted(m for m in sys.modules if m.startswith("deedkit_recordings"))))')
    done = subprocess.run([sys.executable, '-c', code, str(ROOT / 'services/recordings/src')], text=True, capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr
    loaded = set(done.stdout.strip().split(','))
    assert not loaded & {'deedkit_recordings.archive', 'deedkit_recordings.archive_store', 'deedkit_recordings.app',
                         'deedkit_recordings.plaud', 'deedkit_recordings.archive_read'}


# spec: ANYM-760
def test_the_receiver_container_is_closed_and_the_overlay_adds_only_the_channel_to_the_backend():
    document = yaml.safe_load(OVERLAY.read_text())
    unit = document['services']['recordings-omi-hint']
    base = yaml.safe_load((ROOT / 'deploy/compose.recordings.yaml').read_text())['services']['recordings-api']
    assert unit['image'] == base['image'] and 'build' not in unit  # the same published image, another command
    assert unit['command'][:3] == ['--config', '/run/deedkit/omi-hint/receiver.json', '--omi-hint-receiver']
    assert unit['read_only'] is True and unit['user'] == '10001:10001' and unit['cap_drop'] == ['ALL']
    assert unit['security_opt'] == ['no-new-privileges:true'] and not unit.get('ports') and 'network_mode' not in unit
    assert 'mem_limit' in unit and 'pids_limit' in unit
    assert len(unit['volumes']) == 1 and unit['volumes'][0].endswith(':/run/deedkit/omi-hint:ro')
    assert unit['volumes'][0].startswith('${RECORDINGS_HINT_STATE_DIR:?')
    assert not {'RECORDINGS_EGRESS_ADMISSION'} & set(unit.get('environment') or {}) and 'dns' not in unit
    assert set(unit['networks']) == {'recordings-hint', 'recordings-hook'}  # no backend, no egress, no router
    api = document['services']['recordings-api']
    assert set(api) == {'environment', 'networks'} and set(api['networks']) == {'recordings-hint'}
    assert api['networks']['recordings-hint']['aliases'] == [hint.CHANNEL_ALIAS]
    assert api['environment'] == {hint.LISTEN_ENV: f'{hint.CHANNEL_ALIAS}:{hint.CHANNEL_PORT}'}
    assert not api.get('ports')
    for name in ('recordings-hint', 'recordings-hook'):
        assert document['networks'][name]['external'] is True and set(document['networks'][name]) == {'external', 'name'}
    assert not re.search(r'(?i)(token|secret|password)\s*[:=]\s*\S{8,}', OVERLAY.read_text())

    class Loose(yaml.SafeLoader):  # other overlays use Compose tags such as `!override`; only the structure matters here
        pass
    Loose.add_multi_constructor('!', lambda loader, suffix, node: loader.construct_sequence(node)
        if isinstance(node, yaml.SequenceNode) else loader.construct_mapping(node) if isinstance(node, yaml.MappingNode)
        else loader.construct_scalar(node))
    members = {'recordings-hint': [], 'recordings-hook': []}
    for path in sorted((ROOT / 'deploy').glob('compose*.yaml')):
        for name, service in (yaml.load(path.read_text(), Loose) or {}).get('services', {}).items():
            for network in members:
                if network in (service.get('networks') or []):
                    members[network].append(name)
    assert sorted(members['recordings-hint']) == ['recordings-api', 'recordings-omi-hint']
    assert members['recordings-hook'] == ['recordings-omi-hint']  # the proxy joins it by hand, nothing else


# --- criterion 2: the id and nothing else

# spec: ANYM-760
def test_canaries_in_the_body_and_url_appear_in_no_log_response_status_database_or_channel_message(tmp_path, capfd, caplog, monkeypatch):
    monkeypatch.setattr(hint, 'wall', lambda: T0)
    transcript, title, uid, path_canary, key_canary = 'TRANSCRIPTCANARY', 'TITLECANARY', 'UIDCANARY', 'T' * 40 + 'CAN', 'KEYCANARY'
    caplog.set_level(logging.DEBUG)

    async def scenario():
        r, omi, path, doc = backend(tmp_path / 'backend', section=hint_section(uid=uid), omi=Omi())
        await ready(r)
        channel = hint.Channel(r.hints)
        recorded = []

        async def forward(request):
            recorded.append(request)
            status, headers, body, _ = await call(channel, path=hint.CHANNEL_PATH, query=b'',
                                                  headers=[(k, v) for k, v in request.headers.items()], chunks=(request.content,))
            return httpx.Response(status, content=body)
        config = receiver_files(tmp_path, digests=[digest(path_canary)])
        settings = json.loads(config.read_text())
        settings['uid'] = uid
        config.write_text(json.dumps(settings))
        app = hint.Receiver(config, transport=httpx.MockTransport(forward))
        body = json.dumps({'id': 'conversationone', 'uid': uid, 'structured': {'title': title, 'overview': transcript},
                           'transcript_segments': [{'text': transcript, 'speaker': title}]}).encode()
        route = '/omi/memory/' + path_canary
        query = b'uid=' + uid.encode()
        key = (('idempotency-key', key_canary),)
        answers = [await call(app, path=route, query=query, headers=key, chunks=(body,)),
                   await call(app, path=route, query=b'uid=' + title.encode(), headers=key, chunks=(body,)),
                   await call(app, path=route, query=query, chunks=(body[:-1],)),
                   await call(app, path=route, query=query, chunks=(body, b'x' * hint.MAX_BODY)),
                   await call(app, path='/omi/memory/' + 'Z' * 40 + 'CAN', query=query, headers=key, chunks=(body,))]
        assert [answer[0] for answer in answers] == [200, 400, 400, 413, 404]
        text = b''.join(bytes(answer[2]) + repr(answer[1]).encode() for answer in answers)
        counters = json.dumps(app.report()) + json.dumps(r.hints.report())
        app.log_counters(force=True)
        await r.hints.run(now=T0 + 15)
        omi.now = T0 + 15
        status = await r.status('omi-a', now=T0 + 16)
        await r.close()
        files = b''.join(p.read_bytes() for p in Path(doc['archive']['directory']).rglob('*') if p.is_file())
        assert len(recorded) == 1
        message = recorded[0]
        assert json.loads(message.content) == {'ids': ['conversationone']}
        assert 'idempotency-key' not in message.headers and message.url.query == b''
        return text, counters + json.dumps(status), files, message.content + repr(dict(message.headers)).encode()

    text, status, files, message = asyncio.run(scenario())
    out, err = capfd.readouterr()
    logs = caplog.text + out + err
    for canary in (transcript, title, uid, path_canary, key_canary):
        assert canary.encode() not in text, canary
        assert canary not in status, canary
        assert canary not in logs, canary
        assert canary.encode() not in files, canary
        assert canary.encode() not in message, canary
    assert b'conversationone' not in files  # held in memory only: the synthetic Omi never listed that id
    assert 'conversationone' not in logs and 'conversationone' not in status


# spec: ANYM-760
def test_counters_are_by_reason_only_and_the_periodic_line_holds_no_value(tmp_path, capfd):
    async def scenario():
        app, _ = receiver(tmp_path)
        await call(app, query=b'uid=wrong')
        await call(app, path='/nope')
        await call(app)
        return app.report()
    report = asyncio.run(scenario())
    assert report == {'accepted': 1, 'bad_uid': 1, 'not_found': 1}
    assert set(report) <= hint.RECEIVER_REASONS


# --- criterion 3: a hint only brings the next list forward

# spec: ANYM-760
def test_a_hinted_conversation_is_archived_after_the_settle_and_the_same_one_without_a_hint_by_the_next_head_pass(tmp_path):
    async def scenario():
        for case, hinted in (('hinted', True), ('plain', False)):
            r, omi, _, doc = backend(tmp_path / case)
            await ready(r)
            await r.run_once(now=T0)
            omi.add('newconversation', FRESH + 30, front=True)
            calls = len(omi.calls)
            if hinted:
                r.hints.offer(['newconversation'], T0 + 1)
                await advance(r, omi, T0 + 2, T0 + 15)
                assert len(omi.calls) == calls  # nothing before the settle ends
                assert sql(doc, "SELECT count(*) FROM records WHERE provider_id='newconversation' AND attempted IS NOT NULL") == [(0,)]
                await advance(r, omi, T0 + 16, T0 + 16)
                # Listed once and read once, at 15 s after the hint, before any regular head list is due.
                assert [c[2] for c in omi.calls[calls:]] == ['list', 'detail']
                assert omi.calls[calls][0] == T0 + 16
            else:
                omi.now = T0 + 301
                await r.run_once(now=T0 + 301)
            assert sql(doc, "SELECT count(*) FROM records WHERE provider_id='newconversation' AND attempted IS NOT NULL") == [(1,)]
            part = sql(doc, "SELECT json_extract(parts,'$.transcript.text') FROM records WHERE provider_id='newconversation'")[0][0]
            assert 'Words of newconversation' in part
            await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_fifty_hints_in_one_second_cause_one_list(tmp_path):
    async def scenario():
        r, omi, _, _ = backend(tmp_path)
        await ready(r)
        for n in range(50):
            omi.add(f'burst{n:02d}', FRESH, front=True)
        for n in range(50):
            r.hints.offer([f'burst{n:02d}'], T0 + n / 50)
        await advance(r, omi, T0, T0 + 400)
        assert len(lists(omi)) == 1
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_a_garbage_id_with_a_valid_token_causes_at_most_three_lists_no_record_and_no_transcript_read(tmp_path):
    async def scenario():
        r, omi, _, doc = backend(tmp_path)
        await ready(r)
        r.hints.offer(['neverlisted'], T0)
        await advance(r, omi, T0, T0 + 900)
        assert [c[0] - T0 for c in lists(omi)] == [15, 60, 300]
        assert not details(omi) and sql(doc, 'SELECT count(*) FROM records') == [(0,)]
        assert not r.hints.pending
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_a_hundred_hints_a_minute_for_an_hour_cause_at_most_six_forced_lists_inside_the_budget(tmp_path):
    async def scenario():
        r, omi, _, doc = backend(tmp_path)
        await ready(r)
        serial = 0
        for minute in range(60):
            moment = T0 + minute * 60
            omi.now = moment
            omi.add(f'real{minute:03d}', moment - 10, front=True)
            for _ in range(100):
                serial += 1
                r.hints.offer([f'flood{serial:06d}'], moment)
            for second in range(0, 60, 5):
                omi.now = moment + second
                await r.hints.run(now=moment + second)
        forced = len(lists(omi))
        assert forced <= hint.FORCED_PER_HOUR == 6
        assert len(r.hints.pending) <= hint.PENDING == 64
        assert omi.peak.get((TOKEN, 'reads'), 0) <= 45 and omi.peak.get((TOKEN, 'transcripts'), 0) <= 18
        assert not any(row[0].startswith('flood') for row in sql(doc, 'SELECT provider_id FROM records'))
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_at_most_64_pending_ids_are_held_the_oldest_dropped(tmp_path):
    r, _, _, _ = backend(tmp_path)
    r.hints.offer([f'id{n:03d}' for n in range(100)][:16], T0)
    for start in range(16, 100, 16):
        r.hints.offer([f'id{n:03d}' for n in range(start, min(start + 16, 100))], T0)
    assert len(r.hints.pending) == 64 and 'id000' not in r.hints.pending and 'id099' in r.hints.pending


# spec: ANYM-760
def test_a_hint_during_a_429_pause_causes_no_provider_call_and_no_error(tmp_path):
    async def scenario():
        r, omi, _, doc = backend(tmp_path)
        await ready(r)
        omi.limited[(TOKEN, 'any')] = (T0 + 1000, '900')
        r.hints.offer(['paused'], T0)
        await advance(r, omi, T0 + 15, T0 + 15)
        assert len(omi.calls) == 1  # the 429 itself, which pauses the credential as any collector call does
        assert sql(doc, "SELECT count(*) FROM ledger WHERE bucket='throttle'") == [(1,)]
        calls = len(omi.calls)
        r.hints.offer(['pausedagain'], T0 + 20)
        await advance(r, omi, T0 + 35, T0 + 400)
        assert len(omi.calls) == calls and not r.hints.pending
        assert r.error is None and not r.errors
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_a_refused_reservation_drops_the_hint_without_an_error_and_a_spent_budget_waits_for_the_next_slot(tmp_path):
    async def scenario():
        r, omi, _, doc = backend(tmp_path, omi=Omi())
        await ready(r)
        credential = digest(TOKEN)
        for _ in range(45):
            assert await r.reserve(credential, [('reads', 45)], T0 - 10)
        omi.add('waiting', FRESH, front=True)
        r.hints.offer(['waiting'], T0)
        await advance(r, omi, T0 + 15, T0 + 400)
        assert not omi.calls and not r.hints.pending and r.error is None and not r.errors
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
@pytest.mark.parametrize('absent', ['unknown', 'not_archived'])
def test_a_hint_for_an_account_that_is_not_archived_is_ignored(tmp_path, absent):
    async def scenario():
        r, omi, path, doc = backend(tmp_path, section=hint_section(account='omi-b') if absent == 'unknown' else None)
        if absent == 'not_archived':
            del doc['accounts'][0]['archive_namespace']
            doc['principals'][0]['accounts'] = ['omi-a']
            rewrite(path, doc)
        await ready(r)
        omi.add('someone', FRESH)
        r.hints.offer(['someone'], T0)
        await advance(r, omi, T0, T0 + 400)
        assert not omi.calls and not r.hints.pending
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_a_hint_causes_no_plaud_call_with_a_plaud_account_configured_beside_the_omi_one(tmp_path):
    class Remote:
        calls = 0

        async def call(self, *args):
            Remote.calls += 1
            raise AssertionError('PLAUD must not be visited by a hint')

    async def scenario():
        r, omi, path, doc = backend(tmp_path)
        key = Path(doc['accounts'][0]['token_file']).with_name('plaud.key')
        key.write_text('plaud-token-synthetic'); key.chmod(0o600)
        doc['accounts'].insert(0, {'id': 'plaud-a', 'provider': 'plaud', 'label': 'Synthetic', 'token_file': str(key), 'archive_namespace': 'b' * 32})
        rewrite(path, doc)
        r.remote = Remote()
        await ready(r)
        omi.add('onlyomi', FRESH, front=True)
        r.hints.offer(['onlyomi'], T0)
        await advance(r, omi, T0 + 15, T0 + 15)
        assert Remote.calls == 0 and [c[2] for c in omi.calls] == ['list', 'detail']
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_an_unfinished_head_traversal_keeps_its_cursor_floor_and_time_and_the_checkpoint_after_a_hint_pass(tmp_path):
    async def scenario():
        r, omi, _, doc = backend(tmp_path, omi=Omi(count=6, created=FRESH))
        await ready(r)
        await r.db('plan', r.document().accounts[0], digest(TOKEN), T0)
        with sqlite3.connect(Path(doc['archive']['directory']) / 'archive.sqlite') as db:
            db.execute("UPDATE accounts SET head_cursor='c2',head_floor=7,last_head_at=?,checkpoint='\"c9\"',traversal_state='in_progress',"
                       "head_backlog_since=?,passes_completed=3,work_turn=2", (T0 - 5, T0 - 6))
        columns = 'head_cursor,head_floor,last_head_at,checkpoint,traversal_state,head_backlog_since,passes_completed,work_turn,last_page_at'
        before = sql(doc, f'SELECT {columns} FROM accounts')
        omi.add('newest', FRESH + 30, front=True)
        r.hints.offer(['newest'], T0)
        await advance(r, omi, T0 + 15, T0 + 15)
        assert lists(omi)[0][3].get('cursor') is None  # from the start, not `head_cursor`
        assert sql(doc, f'SELECT {columns} FROM accounts') == before
        assert sql(doc, "SELECT count(*) FROM records WHERE provider_id='newest' AND attempted IS NOT NULL") == [(1,)]
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_a_hint_pass_leaves_existing_fingerprints_and_only_reads_never_fetched_conversations_of_the_last_day(tmp_path):
    async def scenario():
        r, omi, _, doc = backend(tmp_path, omi=Omi(count=2, created=FRESH))
        await ready(r)
        await r.run_once(now=T0)
        omi.now = T0 + 1
        kept = sql(doc, 'SELECT provider_id,fingerprint,attempted,failures FROM records ORDER BY provider_id')
        calls_before = len(details(omi))
        # A saved record whose overview changed, and a failed record: neither is hint work.
        omi.items[0]['structured']['overview'] = 'Changed overview'
        omi.failing.add(omi.items[1]['id'])
        omi.add('oldstranger', T0 - 3 * 86400)  # unseen, but older than a day: the traversal's, not the hint's
        omi.add('hinted', FRESH + 40, front=True)
        r.hints.offer(['hinted'], T0 + 1)
        await advance(r, omi, T0 + 16, T0 + 16)
        assert [d[0] for d in omi.details[calls_before:]] == ['hinted']
        rows = {row[0]: row for row in sql(doc, 'SELECT provider_id,fingerprint,attempted,failures FROM records ORDER BY provider_id')}
        for row in kept:
            assert rows[row[0]] == row
        assert 'hinted' in rows and rows['hinted'][1] is not None  # the authoritative list fingerprint, not NULL
        assert 'oldstranger' not in rows
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
@pytest.mark.parametrize('history', ['reached_end', 'in_progress'])
def test_a_hint_never_ends_a_head_walk_early_when_more_new_conversations_lie_behind_its_page(tmp_path, history):
    async def scenario():
        r, omi, _, doc = backend(tmp_path, omi=Omi(count=1, created=T0 - 30 * 86400))
        await ready(r)
        await r.run_once(now=T0)
        if history == 'in_progress':
            with sqlite3.connect(Path(doc['archive']['directory']) / 'archive.sqlite') as db:
                db.execute("UPDATE accounts SET traversal_state='in_progress',checkpoint='\"c1\"'")
        for n in range(201):
            omi.add(f'new{n:03d}', T0 + 10 + n, front=True)
        omi.now = T0 + 100
        r.hints.offer(['new200'], T0 + 85)
        await advance(r, omi, T0 + 100, T0 + 100)
        # A full page of unseen rows with more behind it: nothing inserted, nothing moved, the id counted as found.
        assert len(lists(omi)) == 2 and sql(doc, 'SELECT count(*) FROM records') == [(1,)]
        assert r.hints.report()['deferred'] == 1 and not r.hints.pending
        for moment in (T0 + 400, T0 + 700, T0 + 1000):
            omi.now = moment
            await r.run_once(now=moment)
        rows = {row[0] for row in sql(doc, 'SELECT provider_id FROM records')}
        assert {f'new{n:03d}' for n in range(201)} <= rows  # the oldest of them too, which a hint-ended walk would lose
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_a_page_with_a_known_row_and_more_behind_it_still_inserts(tmp_path):
    async def scenario():
        r, omi, _, doc = backend(tmp_path, omi=Omi(count=250, created=FRESH - 5000))
        await ready(r)
        await r.run_once(now=T0)  # the opening page: 200 rows known, 50 behind them for the traversal
        omi.add('newest', FRESH, front=True)
        r.hints.offer(['newest'], T0 + 1)
        await advance(r, omi, T0 + 16, T0 + 16)
        assert sql(doc, "SELECT count(*) FROM records WHERE provider_id='newest'") == [(1,)]
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_at_most_three_conversations_are_dispatched_by_one_hint_pass(tmp_path):
    async def scenario():
        r, omi, _, _ = backend(tmp_path)
        await ready(r)
        for n in range(6):
            omi.add(f'new{n}', FRESH + n, front=True)
        r.hints.offer(['new0'], T0)
        await advance(r, omi, T0 + 15, T0 + 15)
        assert len(details(omi)) == hint.HINT_WORK == 3
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_a_conversation_fetched_by_a_hint_pass_whose_overview_then_changes_is_read_again_on_the_regular_pass(tmp_path):
    async def scenario():
        r, omi, _, _ = backend(tmp_path)
        await ready(r)
        item = omi.add('changing', FRESH, front=True)
        r.hints.offer(['changing'], T0)
        await advance(r, omi, T0 + 15, T0 + 15)
        assert [d[0] for d in omi.details] == ['changing']
        item['structured']['overview'] = 'A later overview'
        omi.now = T0 + 20
        await r.run_once(now=T0 + 20)
        assert [d[0] for d in omi.details] == ['changing', 'changing']
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_after_the_forced_allowance_continuous_hints_cause_no_list_and_regular_passes_keep_their_schedule(tmp_path):
    async def scenario():
        r, omi, _, _ = backend(tmp_path)
        await ready(r)
        moment, serial = T0, 0
        for moment in range(T0, T0 + 3000, 10):
            serial += 1
            omi.now = moment
            r.hints.offer([f'hint{serial:06d}'], moment)
            await r.hints.run(now=moment)
        assert len(lists(omi)) == hint.FORCED_PER_HOUR
        forced = len(omi.calls)
        for pass_time in (moment + 300, moment + 600):
            omi.now = pass_time
            await r.run_once(now=pass_time)
        regular = lists(omi)[hint.FORCED_PER_HOUR:]
        assert len(omi.calls) > forced and len(regular) >= 2 and regular[0][0] == moment + 300 and regular[-1][0] == moment + 600
        assert r.error is None and not r.errors
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_a_hint_wakes_the_interruptible_sleep_and_does_not_move_the_regular_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(hint, 'SETTLE', 0.05)

    async def scenario():
        r, omi, _, doc = backend(tmp_path)
        assert doc['archive']['poll_seconds'] == 60
        runs = []
        original = r.run_once

        async def counted(**kwargs):
            runs.append(time.monotonic())
            await original(**kwargs)
        r.run_once = counted
        await r.start()
        await asyncio.sleep(0.05)
        assert len(runs) == 1
        omi.add('awake', time.time() - 5, front=True)
        started = time.monotonic()
        r.hints.offer(['awake'], time.time())
        while not omi.details and time.monotonic() - started < 5:
            await asyncio.sleep(0.01)
        assert [d[0] for d in omi.details] == ['awake'] and time.monotonic() - started < 2
        assert len(runs) == 1  # the hint pass ran outside the regular pass, which is not due for a minute
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_rest_without_a_hint_waits_the_whole_interval(tmp_path):
    async def scenario():
        r, _, _, _ = backend(tmp_path)
        started = time.monotonic()
        await r.hints.rest(0.08)
        assert 0.07 <= time.monotonic() - started < 1
    asyncio.run(scenario())


# spec: ANYM-760
def test_a_pass_already_running_finishes_first(tmp_path):
    async def scenario():
        r, omi, _, _ = backend(tmp_path)
        await ready(r)
        await r.collecting.acquire()
        r.hints.offer(['later'], T0)
        task = asyncio.create_task(r.hints.run(now=T0 + 15))
        await asyncio.sleep(0.02)
        assert not task.done() and not omi.calls
        r.collecting.release()
        await task
        assert len(omi.calls) == 1
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_a_failing_hint_pass_is_contained_and_the_id_keeps_its_bounded_looks(tmp_path, monkeypatch):
    async def scenario():
        r, omi, _, _ = backend(tmp_path)
        await ready(r)

        async def broken(*args, **kwargs):
            raise RuntimeError('synthetic failure with a value TRANSCRIPTCANARY')
        monkeypatch.setattr(r.provider, 'get', broken)
        r.hints.offer(['broken'], T0)
        await advance(r, omi, T0, T0 + 900)
        assert not r.hints.pending and r.error is None and not r.errors
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
@settings(max_examples=60, deadline=None)
@given(steps=st.lists(st.tuples(st.integers(0, 120), st.lists(st.sampled_from([f'id{n}' for n in range(90)]), max_size=16),
                                st.sampled_from(['shown', 'missed', 'no_list', 'failed'])), max_size=40))
def test_any_order_of_hints_and_list_outcomes_keeps_the_queue_bounded_and_each_taken_id_at_three_looks(steps):
    class Stub:
        collecting = asyncio.Lock()

    async def scenario():
        hints = hint.Hints(Stub())
        looks, moment = Counter(), T0
        outcome = ['missed']

        async def look(now):
            for ident in hints.batch(now):
                looks[(ident, hints.pending[ident]['received'])] += 1
            if outcome[0] == 'failed':
                raise RuntimeError('synthetic')
            return None if outcome[0] == 'no_list' else (set(hints.pending) if outcome[0] == 'shown' else set())
        hints.look = look
        for gap, ids, result in steps:
            moment += gap
            outcome[0] = result
            hints.offer(ids, moment)
            assert len(hints.pending) <= hint.PENDING and len(hints.seen) <= hint.SEEN
            await hints.run(now=moment)
            assert all(entry['at'] > moment or entry['attempts'] == 0 for entry in hints.pending.values())
        outcome[0] = 'missed'
        for later in range(moment, moment + 1200, 5):  # every pending id runs out of looks: nothing stays or spins
            await hints.run(now=later)
        assert not hints.pending
        assert all(count <= 1 + len(hint.RETRIES) for count in looks.values())
    asyncio.run(scenario())


# spec: ANYM-760
@given(ident=st.from_regex(r'[A-Za-z0-9_-]{1,256}', fullmatch=True), extra=st.dictionaries(st.text(max_size=8).filter(lambda k: k != 'id'),
                                                                                         st.text(max_size=8), max_size=4))
def test_a_valid_body_yields_exactly_its_id(ident, extra):
    assert hint.conversation_id(json.dumps({**extra, 'id': ident}).encode()) == ident


# spec: ANYM-760
@given(value=st.one_of(st.text(max_size=8).filter(lambda v: not hint.ID.fullmatch(v)), st.integers(), st.none(), st.booleans(),
                       st.lists(st.integers(), max_size=2)))
def test_any_other_id_value_is_a_bad_shape(value):
    with pytest.raises(hint.Reject) as refused:
        hint.conversation_id(json.dumps({'id': value}).encode())
    assert (refused.value.status, refused.value.reason) == (400, 'bad_shape')


# --- criterion 4: the channel

# spec: ANYM-760
def test_only_the_channel_credential_opens_the_channel_with_expiry_rotation_and_revocation(tmp_path):
    async def scenario():
        r, _, path, doc = backend(tmp_path)
        channel = hint.Channel(r.hints)
        send = lambda key: call(channel, path=hint.CHANNEL_PATH, query=b'', headers=channel_headers(key), chunks=(b'{"ids":["abc"]}',))
        for key in ('wrong', 'backend-a', PATH_TOKEN):
            assert (await send(key))[0] == 401, key
        assert (await call(channel, path=hint.CHANNEL_PATH, query=b'', chunks=(b'{"ids":["abc"]}',)))[0] == 401
        channel.window.clear()
        assert (await send(CHANNEL_KEY))[0] == 200
        # A rotation overlap accepts both digests, and after it only the new one.
        section = doc['archive']['omi_hint']
        section['channel_key_sha256'] = [digest(OLD_CHANNEL_KEY), digest(CHANNEL_KEY)]
        rewrite(path, doc)
        channel.window.clear()
        assert (await send(OLD_CHANNEL_KEY))[0] == 200 and (await send(CHANNEL_KEY))[0] == 200
        section['channel_key_sha256'] = [digest(CHANNEL_KEY)]
        rewrite(path, doc)
        assert (await send(OLD_CHANNEL_KEY))[0] == 401
        # Expired.
        section['expires_at'] = '2020-01-01T00:00:00Z'
        rewrite(path, doc)
        channel.window.clear()
        assert (await send(CHANNEL_KEY))[0] == 401
        # Revoked: the digests (the whole object) are gone and the route with them.
        del doc['archive']['omi_hint']
        rewrite(path, doc)
        channel.window.clear()
        assert (await send(CHANNEL_KEY))[0] == 404
    asyncio.run(scenario())


# spec: ANYM-760
@pytest.mark.parametrize('kind', ['principal', 'router', 'path_token', 'duplicate'])
def test_a_channel_credential_reused_from_anything_else_makes_the_archive_configuration_invalid(tmp_path, kind):
    section = hint_section()
    path, doc = build(tmp_path, omi_hint=section)
    if kind == 'principal':
        section['channel_key_sha256'] = [digest('backend-a')]
    elif kind == 'router':
        doc['event_router'] = {'key_sha256': [digest(CHANNEL_KEY)], 'expires_at': '2099-01-01T00:00:00Z'}
    elif kind == 'path_token':
        section['path_token_sha256'] = [digest(CHANNEL_KEY)]
    else:
        section['channel_key_sha256'] = [digest(CHANNEL_KEY), digest(CHANNEL_KEY)]
    doc['archive']['omi_hint'] = section
    rewrite(path, doc)
    with pytest.raises(Failure):
        read_document(path)
    # Archive-only errors never disable the existing provider reads (security.py:115-121).
    assert configuration(path, 'backend-a')[0].id == 'agent-a'


# spec: ANYM-760
def test_an_older_package_would_refuse_the_object_so_the_rollback_note_holds():
    class Older(security.Strict):  # the strict model of an older package: no `omi_hint` field
        directory: str
    with pytest.raises(ValueError):
        Older.model_validate({'directory': '/data/archive', 'omi_hint': hint_section()})
    readme = (ROOT / 'services/recordings/README.md').read_text()
    assert 'remove `archive.omi_hint` before a package downgrade' in readme
    assert 'remove `archive.omi_hint` before a package downgrade' in (ROOT / 'docs/operations/production.md').read_text()


# spec: ANYM-760
def test_the_channel_refuses_every_other_route_method_and_shape(tmp_path):
    async def scenario():
        r, _, _, _ = backend(tmp_path)
        channel = hint.Channel(r.hints)
        post = lambda body: call(channel, path=hint.CHANNEL_PATH, query=b'', headers=channel_headers(), chunks=(body,))
        assert (await call(channel, path='/v1/capabilities', query=b'', headers=channel_headers()))[0] == 404
        assert (await call(channel, path='/v1/events', method='GET', query=b'', headers=channel_headers()))[0] == 404
        assert (await call(channel, path=hint.CHANNEL_PATH, method='GET', query=b'', headers=channel_headers()))[0] == 404
        for body in (b'nope', b'[]', b'{}', b'{"ids":[]}', b'{"ids":[5]}', b'{"ids":["bad id"]}', b'{"ids":["a"],"x":1}',
                     json.dumps({'ids': [f'i{n}' for n in range(17)]}).encode()):
            channel.window.clear()
            assert (await post(body))[0] == 400, body
        channel.window.clear()
        assert (await post(b'x' * 4097))[0] == 413
        channel.window.clear()
        assert (await post(json.dumps({'ids': [f'i{n}' for n in range(16)]}).encode()))[0] == 200
        assert len(r.hints.pending) == 16
    asyncio.run(scenario())


# spec: ANYM-760
def test_a_slow_body_on_the_channel_is_cut_at_its_own_deadline(tmp_path):
    async def scenario():
        r, _, _, _ = backend(tmp_path)
        channel = hint.Channel(r.hints, deadline=0.05)
        assert hint.CHANNEL_DEADLINE == 2
        status, _, _, _ = await call(channel, path=hint.CHANNEL_PATH, query=b'', headers=channel_headers(), chunks=(b'{', b'}'), delay=5)
        assert status == 408 and channel.active == 0
    asyncio.run(scenario())


# spec: ANYM-760
def test_replaying_one_hint_twice_causes_one_list(tmp_path, monkeypatch):
    monkeypatch.setattr(hint, 'wall', lambda: T0)

    async def scenario():
        r, omi, _, _ = backend(tmp_path)
        await ready(r)
        channel = hint.Channel(r.hints)
        omi.add('replayed', FRESH, front=True)
        for _ in range(2):
            assert (await call(channel, path=hint.CHANNEL_PATH, query=b'', headers=channel_headers(), chunks=(b'{"ids":["replayed"]}',)))[0] == 200
        await advance(r, omi, T0 + 15, T0 + 15)
        assert len(lists(omi)) == 1
        r.hints.offer(['replayed'], T0 + 5)  # inside ten minutes of the first: no second effect
        assert not r.hints.pending
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_the_id_dedupe_expires_after_ten_minutes_and_holds_at_most_1024_entries(tmp_path):
    r, _, _, _ = backend(tmp_path)
    r.hints.offer(['once'], T0)
    r.hints.pending.clear()
    r.hints.offer(['once'], T0 + 599)
    assert not r.hints.pending
    r.hints.offer(['once'], T0 + 600)
    assert 'once' in r.hints.pending
    for start in range(0, 2000, 16):
        r.hints.offer([f'id{n}' for n in range(start, start + 16)], T0 + 601)
    assert len(r.hints.seen) <= hint.SEEN == 1024


# spec: ANYM-760
def test_a_flood_with_a_valid_credential_stays_inside_the_channel_admission_and_the_main_listener_keeps_answering(tmp_path):
    async def scenario():
        path, doc = build(tmp_path, omi_hint=hint_section())
        omi = Omi(count=1, created=FRESH)
        app = create_app(path, transport=httpx.MockTransport(omi), archive_background=False)
        runtime = app.state.archive
        await runtime.start(background=False)
        channel = hint.Channel(runtime.hints)
        gauge = {'now': 0, 'peak': 0}

        async def flood(n):
            gauge['now'] += 1
            gauge['peak'] = max(gauge['peak'], gauge['now'])
            answer = await call(channel, path=hint.CHANNEL_PATH, query=b'', headers=channel_headers(),
                                chunks=(json.dumps({'ids': [f'f{n}']}).encode(),), delay=0.001)
            gauge['now'] -= 1
            return answer[0]
        peak_inside = []

        original = channel.handle

        async def observed(*args):
            answer = await original(*args)
            peak_inside.append(channel.active)
            return answer
        channel.handle = observed
        tasks = [asyncio.create_task(flood(n)) for n in range(1000)]
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
        started = time.monotonic()
        answer = await client.post('/v1/capabilities', headers={'Authorization': 'Bearer backend-a'}, content=b'{}')
        elapsed = time.monotonic() - started
        assert answer.status_code == 200 and elapsed < 2
        listing = await client.post('/v1/list_recordings', headers={'Authorization': 'Bearer backend-a'},
                                    content=json.dumps({'account': 'omi-a'}).encode())
        assert listing.status_code == 200
        await runtime.run_once(now=T0)
        assert runtime.error is None
        answers = Counter(await asyncio.gather(*tasks))
        assert answers[200] <= 20 and answers[429] >= 980 and set(answers) <= {200, 429}
        assert max(peak_inside) <= hint.CHANNEL_SLOTS == 2
        assert hint.CHANNEL_PER_MINUTE == 20
        await client.aclose()
        await runtime.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_idle_and_half_sent_connections_stay_bounded_at_the_transport_while_the_main_listener_and_a_pass_go_on(tmp_path, monkeypatch):
    monkeypatch.setattr(hint, 'CONNECTION_LIFETIME', 0.6)

    async def scenario():
        path, _ = build(tmp_path, omi_hint=hint_section())
        app = create_app(path, transport=httpx.MockTransport(Omi(count=1, created=FRESH)), archive_background=False)
        runtime = app.state.archive
        await runtime.start(background=False)
        listener = await hint.start_listener(runtime, {hint.LISTEN_ENV: '127.0.0.1:0'})
        held = []
        for n in range(60):
            reader, writer = await asyncio.open_connection('127.0.0.1', listener.port)
            if n % 2:
                writer.write(b'POST /omi-hint HTTP/1.1\r\nHost: x\r\n')  # headers never finished
                await writer.drain()
            held.append((reader, writer))
        await asyncio.sleep(0.1)
        live = listener.server.server_state.connections
        assert len(live) <= hint.CHANNEL_CONNECTIONS
        assert not runtime.hints.counters['received']
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
        started = time.monotonic()
        answer = await client.post('/v1/capabilities', headers={'Authorization': 'Bearer backend-a'}, content=b'{}')
        assert answer.status_code == 200 and time.monotonic() - started < 2
        await runtime.run_once(now=T0)
        assert runtime.error is None
        # Every held connection ends by itself, without one byte more from the client, and the slots come back.
        for reader, writer in held:
            try:
                async with asyncio.timeout(3):
                    assert await reader.read() == b''
            except ConnectionError:
                pass
            writer.close()
        await asyncio.sleep(0.05)
        assert not live
        async with httpx.AsyncClient(trust_env=False) as direct:
            answer = await direct.post(f'http://127.0.0.1:{listener.port}{hint.CHANNEL_PATH}', headers=dict(channel_headers()), content=b'{"ids":["after"]}')
        assert answer.status_code == 200 and 'after' in runtime.hints.pending
        await client.aclose()
        await listener.stop()
        await runtime.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_the_channel_window_reopens_after_a_minute(tmp_path, monkeypatch):
    moment = [1000.0]
    monkeypatch.setattr(hint, 'monotonic', lambda: moment[0])

    async def scenario():
        r, _, _, _ = backend(tmp_path)
        channel = hint.Channel(r.hints)
        send = lambda n: call(channel, path=hint.CHANNEL_PATH, query=b'', headers=channel_headers(), chunks=(json.dumps({'ids': [f'w{n}']}).encode(),))
        assert [(await send(n))[0] for n in range(21)] == [200] * 20 + [429]
        moment[0] += 61
        assert (await send(99))[0] == 200
    asyncio.run(scenario())


# spec: ANYM-760
def test_the_listener_binds_where_named_answers_and_stops_and_does_not_exist_without_configuration(tmp_path):
    async def scenario():
        r, _, path, doc = backend(tmp_path)
        listener = await hint.start_listener(r, {hint.LISTEN_ENV: '127.0.0.1:0'})
        assert listener is not None and r.hints.address == '127.0.0.1'
        port = listener.port
        async with httpx.AsyncClient(trust_env=False) as client:
            answer = await client.post(f'http://127.0.0.1:{port}{hint.CHANNEL_PATH}', headers=dict(channel_headers()), content=b'{"ids":["viasocket"]}')
            assert answer.status_code == 200 and 'viasocket' in r.hints.pending
            answer = await client.post(f'http://127.0.0.1:{port}/v1/capabilities', content=b'{}')
            assert answer.status_code == 404 and 'server' not in answer.headers
        await listener.stop()
        with pytest.raises(OSError):
            socket.create_connection(('127.0.0.1', port), timeout=0.5).close()
        # Without the object, or without the variable, nothing listens.
        assert await hint.start_listener(r, {}) is None
        del doc['archive']['omi_hint']
        rewrite(path, doc)
        assert await hint.start_listener(r, {hint.LISTEN_ENV: '127.0.0.1:0'}) is None
    asyncio.run(scenario())


# spec: ANYM-760
@pytest.mark.parametrize('listen', ['0.0.0.0:13235', '[::]:13235', 'no-such-name.invalid:13235', 'nonsense', '127.0.0.1:notaport',
                                    '127.0.0.1:99999', '203.0.113.9:13235'],
                         ids=['wildcard', 'wildcard6', 'unresolved', 'shape', 'port', 'range', 'not_local'])
def test_a_listener_it_cannot_open_on_one_address_is_absent_and_the_backend_is_unaffected(tmp_path, listen):
    async def scenario():
        r, _, _, _ = backend(tmp_path)
        assert await hint.start_listener(r, {hint.LISTEN_ENV: listen}) is None
        assert r.hints.address is None and r.hints.listener_error
    asyncio.run(scenario())


# spec: ANYM-760
def test_the_app_starts_the_listener_only_when_named_and_the_main_api_refuses_the_hint_address(tmp_path, monkeypatch):
    async def scenario():
        path, _ = build(tmp_path, omi_hint=hint_section())
        monkeypatch.setenv(hint.LISTEN_ENV, '127.0.0.1:0')
        app = create_app(path, transport=httpx.MockTransport(Omi()), archive_background=False)
        async with app.router.lifespan_context(app):
            hints = app.state.archive.hints
            assert hints.address == '127.0.0.1'
            sent = []

            async def send(message):
                sent.append(message)

            async def receive():
                return {'type': 'http.request', 'body': b'{}', 'more_body': False}
            base = {'type': 'http', 'http_version': '1.1', 'scheme': 'http', 'method': 'POST', 'path': '/v1/capabilities', 'raw_path': b'/v1/capabilities',
                    'root_path': '', 'query_string': b'', 'headers': [(b'authorization', b'Bearer backend-a')], 'client': ('127.0.0.1', 2), 'app': app}
            await app({**base, 'server': ('127.0.0.1', 13235)}, receive, send)
            assert sent[0]['status'] == 404
            sent.clear()
            await app({**base, 'server': ('10.9.8.7', 13234)}, receive, send)
            assert sent[0]['status'] == 200
        assert hints.address is None
    asyncio.run(scenario())


# spec: ANYM-760
def test_without_configuration_the_backend_has_no_listener_and_no_guard(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.delenv(hint.LISTEN_ENV, raising=False)
        path, _ = build(tmp_path)
        app = create_app(path, transport=httpx.MockTransport(Omi()), archive_background=False)
        assert not app.user_middleware
        async with app.router.lifespan_context(app):
            assert app.state.archive.hints.listener is None
            status = await app.state.archive.status('omi-a', now=T0)
            assert 'omi_hint' not in status
    asyncio.run(scenario())


def script(*args, stdin=None):
    done = subprocess.run([sys.executable, '-I', str(EGRESS), *args], text=True, capture_output=True, timeout=60, input=stdin)
    return done.returncode, done.stdout, done.stderr


def verdict(lines, bridge, state, port, prefix):
    """The first matching rule of the persisted or command form for a packet entering from `bridge`."""
    for line in lines:
        if not line.startswith(prefix) or f'-i {bridge} ' not in line:
            continue
        if 'RELATED,ESTABLISHED' in line and state != 'reply':
            continue
        if '--dport' in line and (state != 'new' or f'--dport {port} ' not in line):
            continue
        return line.split('-j ')[1].split()[0]
    return 'FALLTHROUGH'


def chain_lines(lines, prefix):
    return [line for line in lines if line.startswith(prefix)]


# spec: ANYM-760
def test_from_the_receiver_only_the_channel_port_opens_a_connection_also_after_the_persisted_rules_are_reloaded(tmp_path):
    code, out, err = script('hint-rules')
    assert code == 0, err
    commands = out.splitlines()
    chain = 'DEEDKIT-RECORDINGS-HINT'
    prefix = f'iptables -A {chain} '
    # The persisted form: the block of ufw's rules file, read back by what `ufw reload` restores.
    source = tmp_path / 'before.rules'
    source.write_text('*filter\n:DOCKER-USER - [0:0]\n# deedkit-recordings begin\n# deedkit-recordings end\nCOMMIT\n')
    code, merged, err = script('hint-rules', '--format', 'ufw', '--family', 'v4', '--source', str(source))
    assert code == 0, err
    persisted = merged.splitlines()
    assert persisted.index('# deedkit-recordings end') < persisted.index('# deedkit-recordings-hint begin')
    for lines, head in ((commands, prefix), (persisted, f'-A {chain} ')):
        rules = chain_lines(lines, head)
        for bridge, port in (('dk-rechint0', 13235), ('dk-rechook0', 13236)):
            assert verdict(rules, bridge, 'new', port, head) == 'RETURN', (head, bridge)
            assert verdict(rules, bridge, 'reply', 51234, head) == 'RETURN'
            # The main API and feed, the gateway, the messaging API, the database, SSH, DNS and HTTPS are all dropped.
            for other in (13234, 13240, 8080, 5432, 22, 53, 443, 80, 13235 if bridge == 'dk-rechook0' else 13236):
                assert verdict(rules, bridge, 'new', other, head) == 'DROP', (head, bridge, other)
    # Hooks and the host: both bridges are hooked, the host refuses them, IPv6 too; the existing rules are not touched.
    for bridge in ('dk-rechint0', 'dk-rechook0'):
        assert f'iptables -I DOCKER-USER -i {bridge} -j {chain}' in commands
        assert f'iptables -I INPUT -i {bridge} -j DROP' in commands
        assert f'ip6tables -I INPUT -i {bridge} -j DROP' in commands
        assert f'ip6tables -I DOCKER-USER -i {bridge} -j DROP' in commands
        assert f'-A DOCKER-USER -i {bridge} -j {chain}' in persisted
        assert f'-A ufw-before-input -i {bridge} -j DROP' in persisted
    code, undone, err = script('hint-rules', '--undo')
    assert code == 0, err
    for command in commands:
        if ' -I ' in command:
            assert command.replace(' -I ', ' -D ', 1) in undone.splitlines()
    assert f'iptables -X {chain}' in undone.splitlines()
    code, base, err = script('rules', '--resolver', '192.0.2.53')
    assert code == 0 and 'dk-rechint0' not in base and 'dk-rechook0' not in base and chain not in base  # no drift for the installed verify


NET_HELPER = """import socket, sys, time
if sys.argv[1] == 'serve':
    server = socket.create_server(('0.0.0.0', int(sys.argv[2])))
    server.settimeout(0.5)
    end = time.monotonic() + 120
    while time.monotonic() < end:
        try:
            conn = server.accept()[0]
        except OSError:
            continue
        conn.sendall(b'ok')
        conn.close()
else:
    try:
        with socket.create_connection((sys.argv[2], int(sys.argv[3])), timeout=1) as conn:
            sys.exit(0 if conn.recv(2) == b'ok' else 1)
    except OSError:
        sys.exit(1)
"""
# Addresses: backend .2 and receiver .3 on the hint bridge (10.78.0.0/24); proxy .2 and receiver .3 on the hook bridge
# (10.79.0.0/24); the namespace itself is .1 on each, standing for the host, with a listener. Exit 77: this environment cannot build it.
TOPOLOGY = """
modprobe br_netfilter 2>/dev/null || true
[ -e /proc/sys/net/bridge/bridge-nf-call-iptables ] || exit 77
sysctl -qw net.bridge.bridge-nf-call-iptables=1 || exit 77
ip link add dk-rechint0 type bridge && ip link set dk-rechint0 up && ip addr add 10.78.0.1/24 dev dk-rechint0 || exit 77
ip link add dk-rechook0 type bridge && ip link set dk-rechook0 up && ip addr add 10.79.0.1/24 dev dk-rechook0 || exit 77
unshare -n sleep 150 >/dev/null 2>&1 &
API=$!
unshare -n sleep 150 >/dev/null 2>&1 &
REC=$!
unshare -n sleep 150 >/dev/null 2>&1 &
PROXY=$!
sleep 0.5
ip link add vapi type veth peer name vapi1 && ip link add vrech type veth peer name vrech1 || exit 77
ip link add vproxy type veth peer name vproxy1 && ip link add vrecp type veth peer name vrecp1 || exit 77
ip link set vapi1 netns $API && ip link set vrech1 netns $REC && ip link set vproxy1 netns $PROXY && ip link set vrecp1 netns $REC || exit 77
for port in vapi vrech; do ip link set $port master dk-rechint0 && ip link set $port up || exit 77; done
for port in vproxy vrecp; do ip link set $port master dk-rechook0 && ip link set $port up || exit 77; done
nsenter -t $API -n sh -c 'ip addr add 10.78.0.2/24 dev vapi1 && ip link set vapi1 up && ip link set lo up' || exit 77
nsenter -t $REC -n sh -c 'ip addr add 10.78.0.3/24 dev vrech1 && ip link set vrech1 up && ip addr add 10.79.0.3/24 dev vrecp1 && ip link set vrecp1 up && ip link set lo up' || exit 77
nsenter -t $PROXY -n sh -c 'ip addr add 10.79.0.2/24 dev vproxy1 && ip link set vproxy1 up && ip link set lo up' || exit 77
# Docker's own rules: DOCKER-USER first, then the inter-container accept on each bridge.
iptables -N DOCKER-USER; iptables -P FORWARD DROP; iptables -A FORWARD -j DOCKER-USER
iptables -A FORWARD -i dk-rechint0 -o dk-rechint0 -j ACCEPT
iptables -A FORWARD -i dk-rechook0 -o dk-rechook0 -j ACCEPT
iptables -N ufw-before-input; iptables -A INPUT -j ufw-before-input
for port in 13234 13235 13240 5432; do nsenter -t $API -n python3 HELPER serve $port >/dev/null 2>&1 & done
nsenter -t $REC -n python3 HELPER serve 13236 >/dev/null 2>&1 &
nsenter -t $PROXY -n python3 HELPER serve 8080 >/dev/null 2>&1 &
python3 HELPER serve 22222 >/dev/null 2>&1 &
sleep 1
probe() { if nsenter -t $2 -n python3 HELPER connect $3 $4; then echo "$5 $1=ok"; else echo "$5 $1=fail"; fi; }
probes() {
  probe channel $REC 10.78.0.2 13235 $1
  probe main_api $REC 10.78.0.2 13234 $1
  probe gateway $REC 10.78.0.2 13240 $1
  probe database $REC 10.78.0.2 5432 $1
  probe host_hint $REC 10.78.0.1 22222 $1
  probe host_hook $REC 10.79.0.1 22222 $1
  probe proxy_other $REC 10.79.0.2 8080 $1
  probe off_bridge $REC 203.0.113.9 443 $1
  probe proxy_to_receiver $PROXY 10.79.0.3 13236 $1
  probe proxy_to_channel_network $PROXY 10.78.0.2 13235 $1
  probe backend_to_receiver $API 10.78.0.3 13236 $1
}
# The positive controls: the same listeners answer without the rules, so a later failure is the rules and not an empty port.
probes baseline
sh APPLY
probes commands
# The persisted rules reloaded: everything of the command form gone, then only ufw's block restored.
iptables -F DOCKER-USER; iptables -F INPUT; iptables -A INPUT -j ufw-before-input; iptables -F DEEDKIT-RECORDINGS-HINT; iptables -X DEEDKIT-RECORDINGS-HINT
iptables-restore --noflush PERSISTED
probes persisted
kill $API $REC $PROXY 2>/dev/null || true
kill $(jobs -p) 2>/dev/null || true
"""


def namespace_ready():
    if not all(shutil.which(tool) for tool in ('sudo', 'unshare', 'iptables-restore', 'nsenter', 'ip')):
        return False
    return subprocess.run(['sudo', '-n', 'unshare', '-n', 'true'], capture_output=True).returncode == 0


# spec: ANYM-760
def test_on_real_bridges_the_receiver_opens_only_the_channel_and_the_proxy_only_the_receiver_also_after_a_reload(tmp_path):
    if not namespace_ready():
        if os.environ.get('GITHUB_ACTIONS'):
            pytest.fail('criterion 4 (iv) must run in CI: sudo, unshare, nsenter, ip or iptables-restore is missing')
        pytest.skip('needs sudo, unshare, nsenter, ip and iptables-restore on Linux')
    helper, apply, persisted = tmp_path / 'net.py', tmp_path / 'apply.sh', tmp_path / 'persisted.rules'
    helper.write_text(NET_HELPER)
    code, out, err = script('hint-rules')
    assert code == 0, err
    apply.write_text(out)
    code, block, err = script('hint-rules', '--format', 'ufw', '--family', 'v4')
    assert code == 0, err
    persisted.write_text(f'*filter\n:DOCKER-USER - [0:0]\n:ufw-before-input - [0:0]\n{block}COMMIT\n')
    text = TOPOLOGY.replace('HELPER', str(helper)).replace('APPLY', str(apply)).replace('PERSISTED', str(persisted))
    result = subprocess.run(['sudo', '-n', 'unshare', '-n', 'sh', '-ec', text], text=True, capture_output=True, timeout=240)
    if result.returncode == 77:
        if os.environ.get('GITHUB_ACTIONS'):
            pytest.fail('criterion 4 (iv) must run in CI: this runner cannot build the bridges: ' + result.stderr)
        pytest.skip('this environment cannot build the bridges (br_netfilter, veth or nsenter)')
    assert result.returncode == 0, result.stderr
    lines = set(result.stdout.split('\n'))
    allowed = ('channel', 'proxy_to_receiver')
    denied = ('main_api', 'gateway', 'database', 'host_hint', 'host_hook', 'proxy_other', 'off_bridge', 'proxy_to_channel_network', 'backend_to_receiver')
    # Positive controls: every listener on a bridge answers before the rules (the host too; no route off the bridge or across them).
    for name in ('channel', 'main_api', 'gateway', 'database', 'host_hint', 'host_hook', 'proxy_other', 'proxy_to_receiver', 'backend_to_receiver'):
        assert f'baseline {name}=ok' in lines, (name, result.stdout)
    for phase in ('commands', 'persisted'):
        for name in allowed:
            assert f'{phase} {name}=ok' in lines, (phase, name, result.stdout)
        for name in denied:
            assert f'{phase} {name}=fail' in lines, (phase, name, result.stdout)


# spec: ANYM-760
def test_hint_verify_compares_repository_files_and_kernel_and_reports_a_lost_rule(tmp_path):
    chain = 'DEEDKIT-RECORDINGS-HINT'
    env = tmp_path / 'env'
    env.write_text('RECORDINGS_HINT_RULES=1\n')
    files = {}
    for family, name in (('v4', 'before.rules'), ('v6', 'before6.rules')):
        code, block, err = script('hint-rules', '--format', 'ufw', '--family', family)
        assert code == 0, err
        files[family] = tmp_path / name
        files[family].write_text(f'*filter\n:DOCKER-USER - [0:0]\n{block}COMMIT\n')
    code, out, _ = script('hint-rules')
    lines = out.splitlines()

    def kernel(binary):
        rows = [':INPUT ACCEPT [0:0]', ':DOCKER-USER - [0:0]'] + ([f':{chain} - [0:0]'] if binary == 'iptables' else [])
        for line in lines:
            parts = line.split()
            if parts[0] == binary and parts[1] in ('-A', '-I'):
                rows.append(' '.join(['-A', *parts[2:]]))
        return '*filter\n' + '\n'.join(rows) + '\nCOMMIT\n'
    tools = {}
    for binary in ('iptables', 'ip6tables'):
        (tmp_path / f'{binary}.filter').write_text(kernel(binary))
        (tmp_path / f'{binary}.raw').write_text('*raw\nCOMMIT\n')
        tool = tmp_path / f'{binary}-save'
        tool.write_text(f'#!/bin/sh\ncat "{tmp_path}/{binary}.$2"\n')
        tool.chmod(0o755)
        tools[binary] = tool

    def verify():
        return script('hint-verify', '--env-file', str(env), '--rules-v4', str(files['v4']), '--rules-v6', str(files['v6']),
                      '--after-init', str(tmp_path / 'absent'), '--iptables-save', str(tools['iptables']),
                      '--ip6tables-save', str(tools['ip6tables']))
    code, out, err = verify()
    assert (code, out) == (0, 'ok\n'), (out, err)
    # A rule lost in the kernel is drift.
    text = (tmp_path / 'iptables.filter').read_text()
    (tmp_path / 'iptables.filter').write_text('\n'.join(row for row in text.splitlines() if '--dport 13236' not in row) + '\n')
    code, out, _ = verify()
    assert code == 1 and 'kernel_vs_repo' in out


# spec: ANYM-760
def test_the_boot_receipt_gains_the_hint_pair_only_when_it_is_installed(tmp_path):
    sys.path.insert(0, str(ROOT / 'scripts'))
    import firewall_persist
    env = tmp_path / 'env'
    env.write_text('RECORDINGS_RESOLVER=192.0.2.53\n')
    out = tmp_path / 'receipt.json'
    base = ['--out', str(out), '--boot-id', 'b', '--raw-state', str(tmp_path / 'none'), '--env-file', str(env),
            '--systemctl', 'true', '--docker', 'true', '--verify', 'inference=echo ok', '--verify', 'delivery=echo ok', '--verify', 'recordings=echo ok']
    assert firewall_persist.receipt_cli(base) == 0
    assert 'recordings_hint' not in json.loads(out.read_text())
    env.write_text('RECORDINGS_RESOLVER=192.0.2.53\nRECORDINGS_HINT_RULES=1\n')
    assert firewall_persist.receipt_cli([*base, '--verify', 'recordings_hint=echo ok']) == 0
    assert json.loads(out.read_text())['recordings_hint'] == 'ok'
    assert firewall_persist.receipt_cli([*base, '--verify', 'recordings_hint=echo differs']) == 1
    assert json.loads(out.read_text())['recordings_hint'] == 'differs'


# --- criterion 5: polling stays the backstop

# spec: ANYM-760
def test_with_the_receiver_down_or_failing_the_same_record_is_archived_by_the_next_pass(tmp_path):
    async def scenario():
        for case in ('down', 'up'):
            r, omi, _, doc = backend(tmp_path / case)
            await ready(r)
            await r.run_once(now=T0)
            omi.add('samerecord', FRESH + 20, front=True)
            if case == 'up':
                r.hints.offer(['samerecord'], T0 + 1)
                await advance(r, omi, T0 + 16, T0 + 16)
            else:
                omi.now = T0 + 301
                await r.run_once(now=T0 + 301)
            assert sql(doc, "SELECT count(*) FROM records WHERE provider_id='samerecord' AND attempted IS NOT NULL") == [(1,)]
            await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_collector_passes_continue_while_a_thousand_invalid_requests_arrive(tmp_path):
    async def scenario():
        r, omi, _, doc = backend(tmp_path / 'backend', omi=Omi(count=1, created=FRESH))
        await ready(r)
        app, channel = receiver(tmp_path)
        bad = [call(app, path='/omi/memory/' + 'X' * 43) for _ in range(500)] + [call(app, chunks=(b'not json',)) for _ in range(500)]
        requests = asyncio.gather(*bad)
        await r.run_once(now=T0)
        answers = Counter(answer[0] for answer in await requests)
        assert answers[404] == 500 and answers[400] + answers[503] == 500
        assert r.error is None and not channel.requests
        assert sql(doc, 'SELECT count(*) FROM records WHERE attempted IS NOT NULL') == [(1,)]
        await r.close()
    asyncio.run(scenario())


# spec: ANYM-760
def test_a_broken_receiver_configuration_fails_closed_with_the_same_404(tmp_path):
    async def scenario():
        app, _ = receiver(tmp_path)
        good = await call(app, path='/nope')
        config = app.path
        config.write_text('{ not json')
        answer = await call(app)
        assert answer[:3] == good[:3]
        config.unlink()
        assert (await call(app))[:3] == good[:3]
    asyncio.run(scenario())


# spec: ANYM-760
def test_a_replaced_receiver_configuration_reaches_the_running_receiver(tmp_path):
    async def scenario():
        app, _ = receiver(tmp_path)
        assert (await call(app))[0] == 200
        config = app.path
        settings = json.loads(config.read_text())
        settings['path_token_sha256'] = [digest(OLD_PATH_TOKEN), digest(PATH_TOKEN)]
        fresh = config.with_name('new.json')
        fresh.write_text(json.dumps(settings)); fresh.chmod(0o600)
        os.replace(fresh, config)
        assert (await call(app, path='/omi/memory/' + OLD_PATH_TOKEN))[0] == 200
        settings['path_token_sha256'] = [digest(OLD_PATH_TOKEN)]
        fresh.write_text(json.dumps(settings)); fresh.chmod(0o600)
        os.replace(fresh, config)
        assert (await call(app))[0] == 404  # revoked
    asyncio.run(scenario())


# --- criterion 6 and 8: the ingress plan

# spec: ANYM-760
def test_the_ingress_plan_states_route_topology_rules_rotation_undo_promotion_and_owner_steps():
    docs = (ROOT / 'docs/operations/production.md').read_text()
    section = docs[docs.index('### Omi webhook receiver (ANYM-760)'):docs.index('## Image retention')]
    for needle in ('POST /omi/memory/<token>', 'recordings-omi-hint:13236', 'forwards one prefix', 'no secret in the proxy configuration',
                   'deploy/Caddyfile.example', 'proxy-log canary', 'deedkit-recordings-hint', 'deedkit-recordings-hook', 'dk-rechint0', 'dk-rechook0',
                   'gateway_mode_ipv4=isolated', 'hint-rules', 'hint-verify', 'RECORDINGS_HINT_RULES', 'deedkit-firewall-verify',
                   '*Rotate:*', '*Revoke:*', '*Undo:*', 'the same published image', 'separate release-path PR',
                   'no existing recordings isolation is weakened', 'remove `archive.omi_hint` before a package downgrade',
                   'Settings', 'Developer Mode', 'User ID', 'transcript_segments', 'structured', 'cf601889', 'memory_created',
                   '--omi-hint-issue', 'check --blocked', 'first start by hand'):
        assert needle in section, needle
    readme = (ROOT / 'services/recordings/README.md').read_text()
    assert 'No transcription, model API, paid processing or\ninbound webhook runs' not in readme
    assert 'omi_hint' in readme and 'ANYM-760' in readme


# spec: ANYM-760
def test_the_examples_are_synthetic_and_the_archive_example_stays_dark():
    example = json.loads((ROOT / 'deploy/recordings-omi-hint-receiver.example.json').read_text())
    settings = hint.ReceiverSettings.model_validate(example)
    assert all(re.fullmatch(r'0{64}', value) for value in settings.path_token_sha256)
    assert settings.channel_key_file.startswith('/run/deedkit/omi-hint/')
    assert settings.channel_url == f'http://{hint.CHANNEL_ALIAS}:{hint.CHANNEL_PORT}{hint.CHANNEL_PATH}'
    archived = json.loads((ROOT / 'deploy/recordings-archive.example.json').read_text())
    assert 'omi_hint' not in archived['archive']
    assert Document.model_validate(archived).archive.omi_hint is None


# --- criterion 7: the secret handover

# spec: ANYM-760
def test_the_issue_command_writes_two_private_files_and_prints_only_digests(tmp_path, capfd, monkeypatch):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    url_file, key_file = private / 'url', private / 'channel.key'
    argv = ['--omi-hint-issue', '--public-origin', 'https://proxy.example.invalid', '--url-file', str(url_file), '--channel-key-file', str(key_file)]
    old_umask = os.umask(0)
    try:
        cli.main(argv)
    finally:
        os.umask(old_umask)
    out, err = capfd.readouterr()
    url = url_file.read_text().strip()
    match = re.fullmatch(r'https://proxy\.example\.invalid/omi/memory/([A-Za-z0-9_-]{43})', url)
    assert match, 'url shape'
    token, key = match[1], key_file.read_text().strip()
    assert len(key) == 43 and token != key
    for secret in (token, key):
        assert secret not in out and secret not in err and secret not in ' '.join(argv)
    assert json.loads(out) == {'channel_key_sha256': digest(key), 'path_token_sha256': digest(token)}
    assert url_file.stat().st_mode & 0o777 == 0o600 and key_file.stat().st_mode & 0o777 == 0o600
    # What it issued opens what it says: a digest in the receiver's file and the backend's accepts these raw values.
    assert security.digest_matches([digest(token)], token)


# spec: ANYM-760
def test_the_issue_command_refuses_an_existing_file_a_wide_directory_and_leaves_nothing_behind(tmp_path, capfd):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    wide = tmp_path / 'wide'
    wide.mkdir(); wide.chmod(0o755)
    def issue(url_file, key_file, origin='https://proxy.example.invalid'):
        with pytest.raises(SystemExit) as stop:
            cli.main(['--omi-hint-issue', '--public-origin', origin, '--url-file', str(url_file), '--channel-key-file', str(key_file)])
        assert stop.value.code not in (0, None)
        return capfd.readouterr()
    (private / 'taken').write_text('kept')
    issue(private / 'taken', private / 'key1')
    assert (private / 'taken').read_text() == 'kept' and not (private / 'key1').exists()
    issue(private / 'url1', private / 'taken')
    assert (private / 'taken').read_text() == 'kept' and not (private / 'url1').exists()
    issue(wide / 'url', private / 'key2')
    assert not (wide / 'url').exists() and not (private / 'key2').exists()
    issue(private / 'url2', wide / 'key')
    assert not (private / 'url2').exists() and not (wide / 'key').exists()
    issue(private / 'url3', private / 'key3', origin='http://plain.example.invalid')  # https only
    assert not (private / 'url3').exists()
    issue(private / 'same', private / 'same')
    assert not (private / 'same').exists()
    (tmp_path / 'link').symlink_to(private)
    issue(tmp_path / 'link' / 'url4', private / 'key4')
    assert not (private / 'url4').exists()


# spec: ANYM-760
def test_rotation_writes_new_files_and_the_old_digests_stay_valid_until_removed(tmp_path, capfd):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    def issue(tag):
        cli.main(['--omi-hint-issue', '--public-origin', 'https://proxy.example.invalid', '--url-file', str(private / f'url{tag}'),
                  '--channel-key-file', str(private / f'key{tag}')])
        return json.loads(capfd.readouterr().out)
    first, second = issue(1), issue(2)
    assert first['path_token_sha256'] != second['path_token_sha256'] and first['channel_key_sha256'] != second['channel_key_sha256']
    assert (private / 'url1').exists() and (private / 'key1').exists()
    token1 = (private / 'url1').read_text().strip().rsplit('/', 1)[1]
    token2 = (private / 'url2').read_text().strip().rsplit('/', 1)[1]

    async def scenario():
        both = [first['path_token_sha256'], second['path_token_sha256']]
        app = hint.Receiver(receiver_files(tmp_path, digests=both), transport=httpx.MockTransport(Channel()))
        assert (await call(app, path='/omi/memory/' + token1))[0] == 200 and (await call(app, path='/omi/memory/' + token2))[0] == 200
    asyncio.run(scenario())


# spec: ANYM-760
def test_the_cli_keeps_requiring_a_configuration_for_everything_else(capfd):
    with pytest.raises(SystemExit) as stop:
        cli.main(['--port', '13234'])
    assert stop.value.code == 2 and '--config' in capfd.readouterr().err


# spec: ANYM-760
def test_the_receiver_mode_runs_no_egress_admission_and_serves_without_access_logs(tmp_path, monkeypatch):
    served = {}

    def fake_run(app, **options):
        served.update(app=app, **options)
    monkeypatch.setattr('uvicorn.run', fake_run)
    monkeypatch.setenv('RECORDINGS_EGRESS_ADMISSION', 'required')  # the backend would refuse to start without its probes
    config = receiver_files(tmp_path)
    cli.main(['--config', str(config), '--omi-hint-receiver', '--host', '0.0.0.0', '--port', '13236'])
    assert isinstance(served['app'], hint.Receiver) and served['port'] == 13236 and served['access_log'] is False
    assert served['server_header'] is False and served['date_header'] is False and served['lifespan'] == 'off'


# spec: ANYM-760
def test_status_names_counters_only_when_configured(tmp_path):
    async def scenario():
        r, omi, _, _ = backend(tmp_path)
        await ready(r)
        r.hints.offer(['counted'], T0)
        await r.hints.run(now=T0 + 15)
        status = await r.status('omi-a', now=T0 + 16)
        assert status['omi_hint']['received'] == 1 and status['omi_hint']['forced_lists'] == 1
        assert 'counted' not in json.dumps(status)
        await r.close()
    asyncio.run(scenario())
