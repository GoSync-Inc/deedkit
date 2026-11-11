"""ANYM-464: owner-confirmed Telegram scheduled sends; isolated PostgreSQL and synthetic providers only."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from hypothesis import given, strategies as st

from messaging_service import scheduled_send
from messaging_service.envelope import stable_id
from messaging_service.outgoing import OutgoingWorker
from messaging_service.store import APIError
from test_archive import event
from test_sending import preview_of, queued_count, sender as sender, submit

CHAT = stable_id('chat', 'send-fixture', '100')


@pytest.fixture(autouse=True)
def no_pending_updates(monkeypatch):
    """Each test starts with an empty buffer of not-yet-matched scheduled-list updates."""
    monkeypatch.setattr(scheduled_send, '_pending', {}, raising=False)


def ahead(**delta):
    return (datetime.now(timezone(timedelta(hours=3))) + timedelta(**delta)).replace(microsecond=0).isoformat()


def scheduled_row(s, key, state, at, provider_id='7', text='Exact synthetic text', request_id='synthetic-request-0001'):
    """Preview, send and dispatch one scheduled send; the synthetic provider answers as Telegram does."""
    digest = s.preview_message(key, account='send-fixture', chat_id=CHAT, text=text, schedule_at=at)['preview_digest']
    queued = s.send_message(key, account='send-fixture', chat_id=CHAT, text=text, request_id=request_id,
                            schedule_at=at, preview_digest=digest)
    held = scheduled_send.parse(at)[0]

    async def provider(job):
        return {'provider_message_id': provider_id, 'scheduled': True, 'schedule_at': held}
    assert asyncio.run(OutgoingWorker(state, provider).once()) is True
    return queued


def events_of(s, send_id):
    with s.pool.connection() as c:
        return [row['snapshot'] for row in c.execute("""SELECT snapshot FROM events WHERE kind='message.status'
            AND snapshot->>'send_id'=%s ORDER BY id""", (send_id,)).fetchall()]


# spec: ANYM-464
def test_the_preview_shows_the_time_in_utc_and_the_owner_zone_and_its_digest_binds_it(sender, monkeypatch):
    s, _, key, _, _ = sender
    at = ahead(days=1)
    monkeypatch.setenv('ANYMSG_OWNER_TIMEZONE', 'Europe/Moscow')
    shown = preview_of(s, key, schedule_at=at)
    assert shown['schedule_at_utc'] == scheduled_send.utc_text(scheduled_send.parse(at)[0])
    assert shown['owner_zone_source'] == 'owner' and shown['schedule_at_local'].endswith('+03:00')
    assert shown['render_policy']['schedule_at'] == shown['schedule_at_utc']
    monkeypatch.delenv('ANYMSG_OWNER_TIMEZONE')
    fallback = preview_of(s, key, schedule_at=at)
    assert fallback['owner_zone_source'] == 'utc_fallback' and 'schedule_at_local' not in fallback
    assert fallback['schedule_at_offset'] == '+03:00'
    assert fallback['preview_digest'] == shown['preview_digest']
    assert preview_of(s, key, schedule_at=ahead(days=2))['preview_digest'] != shown['preview_digest']
    assert preview_of(s, key)['preview_digest'] != shown['preview_digest']
    with pytest.raises(APIError, match='preview_required'):
        submit(s, key, schedule_at=at)
    with pytest.raises(APIError, match='preview_mismatch'):
        submit(s, key, schedule_at=ahead(days=2), preview_digest=shown['preview_digest'])
    assert queued_count(s) == 0
    with pytest.raises(APIError, match='invalid_parameters'):
        preview_of(s, key, schedule_at='2026-11-12T09:00:00')  # no offset


# spec: ANYM-464
@pytest.mark.parametrize('delta', [{'seconds': 30}, {'days': 366}, {'days': -1}])
def test_a_time_under_60_s_or_over_365_days_ahead_is_out_of_range_and_queues_nothing(sender, delta):
    s, _, key, _, _ = sender
    at = ahead(**delta)
    with pytest.raises(APIError, match='schedule_at_out_of_range'):
        preview_of(s, key, schedule_at=at)
    with pytest.raises(APIError, match='schedule_at_out_of_range') as refused:
        submit(s, key, schedule_at=at, preview_digest='a' * 64)
    assert refused.value.next_action == 'send_now_or_preview_again'
    assert queued_count(s) == 0


# spec: ANYM-464
def test_on_whatsapp_schedule_at_is_schedule_unsupported_and_nothing_is_queued(sender):
    s, archive, _, _, _ = sender
    archive.register('wa-schedule', 'whatsapp', 'Synthetic WhatsApp', discover=True)
    archive.ingest(event(account='wa-schedule', platform='whatsapp', chat='fixture@c.us', provider_id='1',
        body='Existing synthetic message'))
    archive.set_sending('wa-schedule', True)
    key = s.issue_key('wa-schedule-agent', ['wa-schedule'], permissions=['read', 'send'])
    chat = stable_id('chat', 'wa-schedule', 'fixture@c.us')
    with pytest.raises(APIError, match='schedule_unsupported'):
        s.preview_message(key, account='wa-schedule', chat_id=chat, text='Synthetic', schedule_at=ahead(days=1))
    with pytest.raises(APIError, match='schedule_unsupported'):
        s.send_message(key, account='wa-schedule', chat_id=chat, text='Synthetic', request_id='wa-schedule-01',
            schedule_at=ahead(days=1), preview_digest='a' * 64)
    assert queued_count(s) == 0


# spec: ANYM-464
def test_a_scheduled_send_rests_in_scheduled_ends_the_wait_and_emits_message_status(sender):
    from messaging_service.send_wait import _PENDING_STATES
    s, _, key, _, state = sender
    at = ahead(days=1)
    queued = scheduled_row(s, key, state, at)
    receipt = s.get_send_status(key, queued['send_id'])
    assert receipt['status'] == 'scheduled' and receipt['scheduled_message_id'] == '7'
    assert receipt['provider_message_id'] is None and receipt['message_id'] is None
    assert receipt['next_action'] == 'wait_until_schedule_at' and receipt['edited_in_app'] is False
    assert 'scheduled' not in _PENDING_STATES  # a wait_s wait returns at once
    assert [e['status'] for e in events_of(s, queued['send_id'])] == ['scheduled']
    assert 'Exact synthetic text' not in str(events_of(s, queued['send_id']))


# spec: ANYM-464
def test_telegram_receives_schedule_for_a_chat_and_a_channel_and_the_held_message_is_not_archived():
    from messaging_service.telegram_collector import TelethonProvider
    calls = []

    class Client:
        async def send_message(self, chat, text, **options):
            calls.append((chat, options))
            return SimpleNamespace(id=9, chat_id=chat, date=options.get('schedule'))
    provider = TelethonProvider.__new__(TelethonProvider)
    provider.client = Client()
    text = 'Synthetic later'
    at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=2)

    def job(chat, policy):
        artifact = {'format': 'plain', 'source_text': text, 'rendered_text': text, 'provider_text': text, 'entities': [],
            'provider_payload': {'text': text, 'entities': [], 'link_preview': False}, 'warnings': [], 'links': [],
            'utf16_units': len(text), 'renderer_version': 'anymsg-renderer-1'}
        return {'provider_chat': chat, 'body': text, 'source_text': text, 'source_format': 'plain',
            'render_artifact': artifact, 'render_policy': policy, 'preview_digest': 'f' * 64,
            'expires_at': datetime.now(timezone.utc).timestamp() + 300}
    policy = {'link_preview': False, 'allow_lossy': False, 'schedule_at': scheduled_send.utc_text(at)}
    for chat in ('100', '-1001234567890'):
        receipt = asyncio.run(provider.send_text(job(chat, policy)))
        assert receipt['scheduled'] is True and receipt['provider_message_id'] == '9'
    assert [(chat, options['schedule']) for chat, options in calls] == [(100, at), (-1001234567890, at)]
    # A silent (ANYM-927) scheduled send carries both options.
    asyncio.run(provider.send_text(job('100', {**policy, 'silent': True})))
    assert calls[-1][1]['silent'] is True and calls[-1][1]['schedule'] == at
    # A stored time no longer ahead is refused before the provider; nothing is sent.
    from messaging_service.outgoing import SendRejected
    late = {**policy, 'schedule_at': scheduled_send.utc_text(datetime.now(timezone.utc))}
    with pytest.raises(SendRejected, match='schedule_at_out_of_range'):
        asyncio.run(provider.send_text(job('100', late)))
    assert len(calls) == 3

    class Collector:
        def __init__(self):
            self.provider, self.captured = provider, []

        async def capture(self, item):
            self.captured.append(item)
    from messaging_service.telegram_collector import TelegramCollector
    collector = Collector()
    asyncio.run(TelegramCollector.send_outgoing(collector, job('100', policy)))
    assert collector.captured == []


class Clock:
    """A patched scheduled_send._now that admission waits move forward."""
    def __init__(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self):
        return self.now


# spec: ANYM-464
def test_a_time_that_admission_waits_bring_too_near_is_refused_inside_the_provider_call(monkeypatch):
    from messaging_service.outgoing import SendRejected
    from messaging_service.telegram_collector import TelethonProvider
    clock = Clock()
    monkeypatch.setattr(scheduled_send, '_now', clock, raising=False)
    calls = []

    class Client:
        async def send_message(self, chat, text, **options):
            calls.append(options)
            return SimpleNamespace(id=9, chat_id=chat, date=options.get('schedule'))
    provider = TelethonProvider.__new__(TelethonProvider)
    provider.client = Client()
    text = 'Synthetic later'
    at = clock.now + timedelta(seconds=45)  # ahead at dispatch; a 20 s governor wait leaves 25 s

    async def waited():
        clock.now += timedelta(seconds=20)
    artifact = {'format': 'plain', 'source_text': text, 'rendered_text': text, 'provider_text': text, 'entities': [],
        'provider_payload': {'text': text, 'entities': [], 'link_preview': False}, 'warnings': [], 'links': [],
        'utf16_units': len(text), 'renderer_version': 'anymsg-renderer-1'}
    job = {'provider_chat': '100', 'body': text, 'source_text': text, 'source_format': 'plain',
           'render_artifact': artifact, 'preview_digest': 'f' * 64, '_preflight': waited,
           'render_policy': {'link_preview': False, 'allow_lossy': False, 'schedule_at': scheduled_send.utc_text(at)},
           'expires_at': datetime.now(timezone.utc).timestamp() + 300}
    with pytest.raises(SendRejected, match='schedule_at_out_of_range'):
        asyncio.run(provider.send_text(job))
    assert calls == []


# spec: ANYM-464
def test_silent_combines_with_schedule_at(sender):
    s, _, key, _, _ = sender
    at = ahead(days=1)
    shown = preview_of(s, key, schedule_at=at, silent=True)
    assert shown['silent'] is True and shown['render_policy']['schedule_at'] == shown['schedule_at_utc']
    queued = submit(s, key, schedule_at=at, silent=True, preview_digest=shown['preview_digest'])
    with s.pool.connection() as c:
        row = c.execute('SELECT render_policy FROM outgoing_messages WHERE id=%s', (queued['send_id'],)).fetchone()
    assert row['render_policy'] == {'link_preview': False, 'allow_lossy': False, 'silent': True,
                                    'schedule_at': shown['schedule_at_utc']}


# spec: ANYM-464
def test_published_moved_edited_and_deleted_updates_drive_the_status(sender):
    s, _, key, _, state = sender
    first = scheduled_row(s, key, state, ahead(days=1))
    moved = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=3)
    assert scheduled_send.apply_update(state, {'kind': 'new', 'chat': '100', 'id': 7, 'date': moved,
                                               'text': 'Exact synthetic text'}) == 1
    receipt = s.get_send_status(key, first['send_id'])
    assert receipt['schedule_at'] == moved.isoformat() and receipt['edited_in_app'] is False
    scheduled_send.apply_update(state, {'kind': 'new', 'chat': '100', 'id': 7, 'date': moved, 'text': 'Owner edit'})
    assert s.get_send_status(key, first['send_id'])['edited_in_app'] is True
    with s.pool.connection() as c:  # the confirmed text stays stored
        assert c.execute('SELECT body FROM outgoing_messages WHERE id=%s', (first['send_id'],)).fetchone()['body'] == \
            'Exact synthetic text'
    # Another chat's or the owner's own scheduled message with the same ID changes nothing.
    assert scheduled_send.apply_update(state, {'kind': 'deleted', 'chat': '200', 'ids': [7], 'sent': None}) == 0
    assert scheduled_send.apply_update(state, {'kind': 'deleted', 'chat': '100', 'ids': [7], 'sent': [55]}) == 1
    sent = s.get_send_status(key, first['send_id'])
    assert sent['status'] == 'sent' and sent['provider_message_id'] == '55'
    assert sent['message_id'] == stable_id('msg', 'send-fixture', '100', '55')
    second = scheduled_row(s, key, state, ahead(days=2), provider_id='8', request_id='synthetic-request-0002')
    scheduled_send.apply_update(state, {'kind': 'deleted', 'chat': '100', 'ids': [8], 'sent': None})
    cancelled = s.get_send_status(key, second['send_id'])
    assert cancelled['status'] == 'scheduled_cancelled' and cancelled['cancel_reason'] == 'owner_app'
    assert cancelled['next_action'] == 'ask_owner_before_scheduling_again'
    assert [e['status'] for e in events_of(s, first['send_id'])] == ['scheduled', 'sent']
    assert [e['status'] for e in events_of(s, second['send_id'])] == ['scheduled', 'scheduled_cancelled']


# spec: ANYM-464
def test_a_delete_that_arrives_before_the_receipt_commits_still_cancels_the_send(sender):
    s, _, key, _, state = sender
    at = ahead(days=1)
    digest = preview_of(s, key, schedule_at=at)['preview_digest']
    queued = submit(s, key, schedule_at=at, preview_digest=digest)
    # The owner's own scheduled message (no row) is left alone.
    assert scheduled_send.apply_update(state, {'kind': 'deleted', 'chat': '100', 'ids': [99], 'sent': None}) == 0

    async def provider(job):  # Telegram reports the owner's delete before the dispatch records the receipt
        assert scheduled_send.apply_update(state, {'kind': 'deleted', 'chat': '100', 'ids': [7], 'sent': None}) == 0
        return {'provider_message_id': '7', 'scheduled': True, 'schedule_at': scheduled_send.parse(at)[0]}
    assert asyncio.run(OutgoingWorker(state, provider).once()) is True
    receipt = s.get_send_status(key, queued['send_id'])
    assert receipt['status'] == 'scheduled_cancelled' and receipt['cancel_reason'] == 'owner_app'
    assert [e['status'] for e in events_of(s, queued['send_id'])] == ['scheduled', 'scheduled_cancelled']


# spec: ANYM-464
def test_an_overdue_send_settles_through_the_archive_or_one_live_read_and_never_resends(sender):
    s, archive, key, _, state = sender
    # Only the first one's text reaches the archive; the other two differ by text.
    sends = [scheduled_row(s, key, state, ahead(days=1 + n), provider_id=str(7 + n),
                           text='Exact synthetic text' if not n else f'Synthetic later {n}',
                           request_id=f'synthetic-request-000{n + 1}') for n in range(3)]
    with s.pool.connection() as c:  # the time has passed while the collector was offline
        c.execute("UPDATE outgoing_messages SET schedule_at=clock_timestamp()-interval '5 minutes'")
    published = datetime.now(timezone.utc) - timedelta(minutes=4)
    archive.ingest(event(account='send-fixture', chat='100', provider_id='77', direction='outgoing',
        body='Exact synthetic text', sent_at=published.isoformat(), updated_at=published.isoformat()))
    reads = []

    class Client:  # the one live read: Telegram still holds the second send, an hour later
        async def get_input_entity(self, chat):
            return chat

        async def __call__(self, request):
            return SimpleNamespace(messages=[SimpleNamespace(id=8, date=datetime.now(timezone.utc) + timedelta(hours=1))])

    class Provider:
        client = Client()

        async def rpc(self, operation, phase):
            reads.append(phase)
            return await operation()
    provider = Provider()
    outcomes = asyncio.run(scheduled_send.sweep(state, provider))
    assert sorted(outcomes) == ['scheduled', 'sent', 'unknown'] and reads == ['scheduled_check']
    statuses = {r['send_id']: s.get_send_status(key, r['send_id']) for r in sends}
    assert statuses[sends[0]['send_id']]['provider_message_id'] == '77'
    assert statuses[sends[1]['send_id']]['status'] == 'scheduled'
    assert statuses[sends[2]['send_id']]['status'] == 'unknown'
    assert statuses[sends[2]['send_id']]['error_code'] == 'scheduled_outcome_unknown'
    # The check runs once: a second pass makes no live read.
    asyncio.run(scheduled_send.sweep(state, provider))
    assert reads == ['scheduled_check']


def overdue(s):
    with s.pool.connection() as c:  # the time has passed while the collector was offline
        c.execute("UPDATE outgoing_messages SET schedule_at=clock_timestamp()-interval '5 minutes'")


class ScheduledReader:
    """A synthetic provider for the one live read: `answer` is the scheduled list, or an awaitable that never ends."""
    def __init__(self, answer):
        self.answer, self.reads = answer, []

    @property
    def client(self):
        reader = self

        class Client:
            async def get_input_entity(self, chat):
                return chat

            async def __call__(self, request):
                if reader.answer is None:
                    await asyncio.Event().wait()
                return SimpleNamespace(messages=reader.answer)
        return Client()

    async def rpc(self, operation, phase):
        self.reads.append(phase)
        return await operation()


# spec: ANYM-464
def test_a_stalled_live_read_ends_at_its_deadline_settles_unknown_and_is_never_repeated(sender, monkeypatch):
    s, _, key, _, state = sender
    sends = [scheduled_row(s, key, state, ahead(days=1 + n), provider_id=str(7 + n), text=f'Synthetic later {n}',
                           request_id=f'synthetic-request-000{n + 1}') for n in range(2)]
    overdue(s)
    monkeypatch.setattr(scheduled_send, 'READ_TIMEOUT_S', 0.2, raising=False)
    provider = ScheduledReader(None)
    outcomes = asyncio.run(asyncio.wait_for(scheduled_send.sweep(state, provider), 10))
    assert outcomes == ['unknown', 'unknown'] and provider.reads == ['scheduled_check']
    for queued in sends:
        receipt = s.get_send_status(key, queued['send_id'])
        assert receipt['status'] == 'unknown' and receipt['error_code'] == 'scheduled_outcome_unknown'
    assert asyncio.run(asyncio.wait_for(scheduled_send.sweep(state, provider), 10)) == []
    assert provider.reads == ['scheduled_check']


# spec: ANYM-464
def test_a_send_still_overdue_after_its_one_live_read_is_not_read_again(sender):
    s, _, key, _, state = sender
    queued = scheduled_row(s, key, state, ahead(days=1))
    overdue(s)
    # Telegram still holds it, at a time that has also passed (publishing is late).
    provider = ScheduledReader([SimpleNamespace(id=7, date=datetime.now(timezone.utc) - timedelta(minutes=3))])
    assert asyncio.run(scheduled_send.sweep(state, provider)) == ['scheduled']
    assert asyncio.run(scheduled_send.sweep(state, provider)) == ['pending']
    assert provider.reads == ['scheduled_check'] and s.get_send_status(key, queued['send_id'])['status'] == 'scheduled'


# spec: ANYM-464
def test_an_archived_message_that_a_file_operation_already_claims_is_not_the_published_send(sender):
    s, archive, key, _, state = sender
    queued = scheduled_row(s, key, state, ahead(days=1))
    overdue(s)
    published = datetime.now(timezone.utc) - timedelta(minutes=4)
    archive.ingest(event(account='send-fixture', chat='100', provider_id='77', direction='outgoing',
        body='Exact synthetic text', sent_at=published.isoformat(), updated_at=published.isoformat()))
    with s.pool.connection() as c:  # a send_file whose caption is the same text owns message 77
        c.execute("""INSERT INTO messenger_commands(principal,key_id,scope_version,key_permission_version,request_id,
            request_hash,account,chat,expected_revision,operation,permission,mutation,payload,status,result)
            SELECT principal,key_id,scope_version,key_permission_version,'synthetic-operation-0001',repeat('a',64),
            account,chat,0,'send_file','send',true,'{}'::jsonb,'succeeded',
            jsonb_build_object('outcome','confirmed','provider_message_id','77') FROM outgoing_messages WHERE id=%s""",
                  (queued['send_id'],))
    provider = ScheduledReader([])
    assert asyncio.run(scheduled_send.sweep(state, provider)) == ['unknown']
    assert s.get_send_status(key, queued['send_id'])['provider_message_id'] is None


# spec: ANYM-464
def test_a_second_identical_scheduled_send_is_duplicate_scheduled_whatever_its_age(sender):
    s, _, key, _, state = sender
    at = ahead(days=1)
    first = scheduled_row(s, key, state, at)
    with s.pool.connection() as c:
        c.execute("UPDATE outgoing_messages SET created_at=clock_timestamp()-interval '3 days'")
    digest = preview_of(s, key, schedule_at=at)['preview_digest']
    with pytest.raises(APIError, match='duplicate_scheduled') as refused:
        s.send_message(key, account='send-fixture', chat_id=CHAT, text='Exact synthetic text',
                       request_id='synthetic-request-0002', schedule_at=at, preview_digest=digest)
    assert first['status'] == 'queued' and refused.value.status == 409
    assert refused.value.earlier_request_id == 'synthetic-request-0001'
    assert refused.value.scheduled_message_id == '7' and refused.value.next_call['tool'] == 'list_scheduled'
    again = s.send_message(key, account='send-fixture', chat_id=CHAT, text='Exact synthetic text',
                           request_id='synthetic-request-0002', schedule_at=at, preview_digest=digest,
                           allow_duplicate=True)
    assert again['status'] == 'queued'


# spec: ANYM-464
def test_a_send_moved_in_the_app_is_a_duplicate_at_its_new_time_not_its_old_one(sender):
    s, _, key, _, state = sender
    original = ahead(days=1)
    scheduled_row(s, key, state, original)
    moved = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=3)
    scheduled_send.apply_update(state, {'kind': 'new', 'chat': '100', 'id': 7, 'date': moved, 'text': 'Exact synthetic text'})
    at = moved.isoformat()
    digest = preview_of(s, key, schedule_at=at)['preview_digest']
    with pytest.raises(APIError, match='duplicate_scheduled') as refused:
        s.send_message(key, account='send-fixture', chat_id=CHAT, text='Exact synthetic text',
                       request_id='synthetic-request-0002', schedule_at=at, preview_digest=digest)
    assert refused.value.earlier_request_id == 'synthetic-request-0001'
    # The original time is free again: Telegram no longer holds a message then.
    digest = preview_of(s, key, schedule_at=original)['preview_digest']
    assert s.send_message(key, account='send-fixture', chat_id=CHAT, text='Exact synthetic text', schedule_at=original,
                          request_id='synthetic-request-0003', preview_digest=digest)['status'] == 'queued'


# spec: ANYM-464
def test_an_exact_retry_after_the_time_returns_the_saved_send(sender, monkeypatch):
    s, _, key, _, state = sender
    at = ahead(days=1)
    queued = scheduled_row(s, key, state, at)
    digest = preview_of(s, key, schedule_at=at)['preview_digest']
    later = datetime.now(timezone.utc) + timedelta(days=2)
    monkeypatch.setattr(scheduled_send, '_now', lambda: later, raising=False)
    again = s.send_message(key, account='send-fixture', chat_id=CHAT, text='Exact synthetic text',
                           request_id='synthetic-request-0001', schedule_at=at, preview_digest=digest)
    assert again['send_id'] == queued['send_id']
    with pytest.raises(APIError, match='schedule_at_out_of_range'):  # a new send at that time is out of range
        s.send_message(key, account='send-fixture', chat_id=CHAT, text='Exact synthetic text',
                       request_id='synthetic-request-0002', schedule_at=at, preview_digest=digest)


# spec: ANYM-464
def test_a_submit_timeout_is_unknown_and_never_retried(sender):
    s, _, key, _, state = sender
    at = ahead(days=1)
    digest = preview_of(s, key, schedule_at=at)['preview_digest']
    queued = submit(s, key, schedule_at=at, preview_digest=digest)
    calls = []

    async def provider(job):
        calls.append(job['id'])
        raise TimeoutError()
    worker = OutgoingWorker(state, provider)
    asyncio.run(worker.once())
    assert asyncio.run(worker.once()) is False and calls == [queued['send_id']]
    assert s.get_send_status(key, queued['send_id'])['status'] == 'unknown'


# spec: ANYM-464
def test_send_file_takes_schedule_at_on_telegram_and_whatsapp_refuses_it():
    from messaging_service.messenger_operations import operation_manifest, validate_operation
    base = {'attachment_id': 'att_' + 'a' * 32, 'sha256': 'b' * 64}
    at = ahead(days=1)
    valid = validate_operation('telegram', 'send_file', {**base, 'schedule_at': at})
    assert valid['schedule_at'] == scheduled_send.normalized(at)
    with pytest.raises(ValueError, match='invalid_operation_parameters'):
        validate_operation('telegram', 'send_file', {**base, 'schedule_at': 'tomorrow'})
    with pytest.raises(ValueError, match='schedule_unsupported'):
        validate_operation('whatsapp', 'send_file', {**base, 'schedule_at': at})
    rows = {r['operation']: r for r in operation_manifest('telegram')}
    assert rows['send_file']['parameters_schema']['schedule_at']['type'] == 'schedule_time'
    with pytest.raises(APIError, match='schedule_at_out_of_range'):
        scheduled_send.operation_preview('send_file', {**base, 'schedule_at': ahead(seconds=10)})


# spec: ANYM-464
def test_a_scheduled_file_to_a_channel_is_recognised_only_for_its_own_message():
    from telethon import types
    held = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    channel = types.PeerChannel(1234567890)
    message = SimpleNamespace(id=12, peer_id=channel, date=held)
    receipt = scheduled_send.scheduled_receipt(message, {12}, '-1001234567890')
    assert receipt == {'outcome': 'scheduled', 'basis': 'provider_scheduled_message', 'provider_message_id': '12',
                       'provider_chat_id': '-1001234567890', 'schedule_at': held.isoformat()}
    assert scheduled_send.scheduled_receipt(message, {13}, '-1001234567890') is None
    assert scheduled_send.scheduled_receipt(message, {12}, '100') is None
    assert scheduled_send.operation_time(scheduled_send.utc_text(held)) == held
    assert scheduled_send.operation_time(scheduled_send.utc_text(datetime.now(timezone.utc))) is None


def file_job(tmp_path, at, waited=None):
    import hashlib
    from test_telegram_operations import job
    original = tmp_path / 'original.bin'
    original.write_bytes(b'synthetic original')
    return job('send_file', {'mode': 'document', 'schedule_at': scheduled_send.utc_text(at)},
               registered_file_path=str(original), file_digest=hashlib.sha256(original.read_bytes()).hexdigest(),
               **({'_preflight': waited} if waited else {}))


# spec: ANYM-464
def test_a_scheduled_file_carries_schedule_date_and_a_time_waits_bring_too_near_is_refused_unsent(tmp_path, monkeypatch):
    from telethon import functions
    from messaging_service.commands import CommandRejected
    from messaging_service.telegram_operations import execute
    from test_telegram_operations import Client
    clock = Clock()
    monkeypatch.setattr(scheduled_send, '_now', clock, raising=False)
    held = clock.now + timedelta(hours=1)
    client = Client()
    asyncio.run(execute(client, file_job(tmp_path, held)))
    request = next(c for c in client.calls if isinstance(c, functions.messages.SendMediaRequest))
    assert request.schedule_date == held
    # Every governed call waits 20 s here: the time is ahead before the uploads, too near at the final call.
    waits = []

    async def waited():
        waits.append(1)
        clock.now += timedelta(seconds=20)
    client = Client()
    start = clock.now
    asyncio.run(execute(client, file_job(tmp_path, start + timedelta(days=1), waited)))
    calls = len(waits)  # the governed calls of one send_file; the last one is SendMediaRequest
    clock.now = start
    client = Client()
    with pytest.raises(CommandRejected, match='schedule_at_out_of_range'):
        asyncio.run(execute(client, file_job(tmp_path, start + timedelta(seconds=20 * (calls - 1) + 45), waited)))
    assert not any(isinstance(c, functions.messages.SendMediaRequest) for c in client.calls)


# spec: ANYM-464
def test_capabilities_report_telegram_schedules(sender):
    s, _, key, _, _ = sender
    shown = s.capabilities(key)
    assert shown['sending']['schedules'] == ['telegram'] and 'schedules' not in shown['unavailable']


# spec: ANYM-464
def test_a_scheduled_status_event_reaches_only_the_sending_key():
    from messaging_service.subscriptions import _failure_audience, own_send_status
    status = {'kind': 'message.status', 'account': 'a', 'snapshot': {'send_id': 'send_1', 'principal': 'p1',
                                                                     'key_id': 'k1', 'chat': 'c'}}
    receipt = {'kind': 'message.status', 'account': 'a', 'snapshot': {'chat': 'c'}}
    assert own_send_status(status) and not own_send_status(receipt)
    assert _failure_audience(status, True, {'principal': 'p1', 'key_id': 'k1'}, {})
    assert not _failure_audience(status, True, {'principal': 'p2', 'key_id': 'k2'}, {})


# spec: ANYM-464
def test_a_scheduled_status_event_skips_another_key_of_the_same_principal():
    from messaging_service.subscriptions import _failure_audience
    status = {'kind': 'message.status', 'account': 'a', 'snapshot': {'send_id': 'send_1', 'principal': 'p1',
                                                                     'key_id': 'k1', 'chat': 'c'}}
    assert not _failure_audience(status, True, {'principal': 'p1', 'key_id': 'k2'}, {})


OFFSETS = st.integers(min_value=-14 * 60, max_value=14 * 60).map(lambda m: timezone(timedelta(minutes=m)))


# spec: ANYM-464
@given(st.datetimes(min_value=datetime(2001, 1, 1), max_value=datetime(2099, 1, 1)), OFFSETS)
def test_any_offset_of_one_instant_normalizes_to_the_same_utc_second(moment, zone):
    local = moment.replace(tzinfo=timezone.utc).astimezone(zone)
    text = local.isoformat()
    expected = scheduled_send.utc_text(moment.replace(tzinfo=timezone.utc, microsecond=0))
    assert scheduled_send.normalized(text) == expected
    assert scheduled_send.normalized(scheduled_send.normalized(text)) == expected
    assert scheduled_send.stored(expected) == moment.replace(tzinfo=timezone.utc, microsecond=0)
