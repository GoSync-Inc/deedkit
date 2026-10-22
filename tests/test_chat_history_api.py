"""ANYM-477: request_chat_history anchors, session check, WhatsApp limits and the archive commit (synthetic DB)."""
import asyncio

import pytest
from telethon import types

from messaging_service.archive import Archive
from messaging_service.collector_state import CollectorState
from messaging_service.envelope import stable_id
from messaging_service.messenger_operations import validate_operation
from messaging_service.store import APIError
from messaging_service.telegram_operations import execute
from test_archive import event
from test_collectors import worker  # noqa: F401  (worker is a fixture)
from test_live_search_store import GROUP, MEMBER, OWNER, PERSON, Client, counts, hit, rows
from test_whatsmeow_capabilities import native_account  # noqa: F401  (a fixture)

ACCOUNT = 'history-fixture'


@pytest.fixture
def history(seeded):
    s, _, _ = seeded
    archive = Archive(s)
    archive.register(ACCOUNT, 'telegram', 'Synthetic history', discover=True)
    for chat, number in (('100', '7'), ('100', '9'), ('200', '3')):
        archive.ingest(event(account=ACCOUNT, chat=chat, provider_id=number, direction='incoming', body='Synthetic'))
    key = s.issue_key('history-agent', [ACCOUNT], permissions=['read', 'read-live'])
    epoch = archive.acquire(ACCOUNT, 'synthetic-history-worker')
    state = CollectorState(archive, ACCOUNT, ('synthetic-history-worker', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    s.set_messenger_operations(ACCOUNT, ['request_chat_history'])
    yield s, key, state
    archive.release(ACCOUNT, 'synthetic-history-worker', epoch)


def chat(provider):
    return stable_id('chat', ACCOUNT, provider)


def msg(provider_chat, number):
    return stable_id('msg', ACCOUNT, provider_chat, number)


def request(s, key, request_id='synthetic-history-0001', chat_id=None, **parameters):
    return s.execute_messenger_operation(key, account=ACCOUNT, operation='request_chat_history',
                                         chat_id=chat_id or chat('100'), parameters=parameters, request_id=request_id)


def payload(s, operation_id):
    with s.pool.connection() as c:
        return c.execute('SELECT payload,mutation FROM messenger_commands WHERE id=%s', (operation_id,)).fetchone()


def queued(s):
    with s.pool.connection() as c:
        return c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n']


# spec: ANYM-477
def test_anchor_of_the_same_chat_is_resolved_and_needs_no_preview(history):
    s, key, _ = history
    first = request(s, key, before_message=msg('100', '9'))
    saved = payload(s, first['operation_id'])
    assert first['status'] == 'queued' and saved['mutation'] is False
    assert saved['payload']['provider_message_id'] == '9' and saved['payload']['parameters']['count'] == 50
    around = request(s, key, 'synthetic-history-0002', around_message=msg('100', '7'), count=20)
    assert payload(s, around['operation_id'])['payload']['provider_message_id'] == '7'
    plain = request(s, key, 'synthetic-history-0003', from_time='2026-10-01T00:00:00Z')
    assert payload(s, plain['operation_id'])['payload']['provider_message_id'] is None


# spec: ANYM-477
def test_another_chats_or_unknown_message_is_an_invalid_anchor(history):
    s, key, _ = history
    for anchor in (msg('200', '3'), msg('100', '404')):
        for field in ('before_message', 'around_message'):
            with pytest.raises(APIError, match='invalid_anchor'):
                request(s, key, **{field: anchor})
    assert queued(s) == 0


# spec: ANYM-477
def test_chat_outside_the_key_scope_is_not_found(history):
    s, key, _ = history
    s.set_access('history-agent', chats=[chat('200')])
    for outside in (chat('100'), chat('999')):
        with pytest.raises(APIError, match='not_found'):
            request(s, key, chat_id=outside)


# spec: ANYM-477
def test_unavailable_session_is_refused_at_once_and_nothing_is_queued(history):
    s, key, state = history
    state.phase('connection', 'retry_wait', 'provider_disconnected')
    with pytest.raises(APIError, match='session_unavailable'):
        request(s, key)
    assert queued(s) == 0


# spec: ANYM-477
def test_whatsapp_keeps_preview_count_limit_and_receipt_says_via_phone(native_account):
    from messaging_service.commands import CommandWorker
    s, _, state, key, args = native_account
    state.provider_observation('whatsmeow')
    s.set_messenger_operations('wa-native', ['request_chat_history'])
    base = {**args, 'operation': 'request_chat_history'}
    with pytest.raises(APIError, match='invalid_operation_parameters'):
        s.preview_messenger_operation(key, **{**base, 'parameters': {'count': 51}})
    with pytest.raises(APIError, match='preview_required'):
        s.execute_messenger_operation(key, **{**base, 'parameters': {'count': 10}}, request_id='synthetic-wa-0001')
    preview = s.preview_messenger_operation(key, **{**base, 'parameters': {'count': 10}})
    assert preview['mutation'] is True
    queued_row = s.execute_messenger_operation(key, **{**base, 'parameters': {'count': 10}},
                                               request_id='synthetic-wa-0002', preview_digest=preview['preview_digest'])
    async def phone(job):
        return {'outcome': 'submitted', 'data': {'requested': True, 'count': 10, 'pending': True}}
    assert asyncio.run(CommandWorker(state, phone).once())
    data = s.get_messenger_operation(key, queued_row['operation_id'])['result']['data']
    assert data['delivery'] == 'via_phone' and data['search_again_after_s'] == 60 and data['requested'] is True


class Resolving(Client):
    async def get_input_entity(self, value):return types.InputPeerUser(100, 5)


def fetch(w, *messages, anchor=None, parameters=None):
    """One history page answered by a fake client on the collector's own operation path."""
    seen = {}
    async def provider(job):
        seen['anchor'] = job.get('provider_message_id')
        return await execute(Resolving(types.messages.Messages(list(messages), [], [GROUP], [PERSON, MEMBER, OWNER])), job)
    w.provider.execute_operation = provider
    job = {'operation': 'request_chat_history', 'platform': 'telegram', 'mutation': False, 'expires_at': 4e9,
           'account': w.account, 'chat_id': stable_id('chat', w.account, '100'), 'provider_chat': '100',
           'provider_message_id': anchor,
           'parameters': validate_operation('telegram', 'request_chat_history', parameters or {'count': 10})}
    return asyncio.run(w.execute_operation(job))['data'], seen['anchor']


# spec: ANYM-477
def test_page_is_committed_as_backfill_and_a_rerun_adds_no_rows(worker):
    w, store, _ = worker
    asyncio.run(w.sync_once())
    coverage = rows(store, 'collector_chats')
    page = [hit(n, 'older synthetic', day=1) for n in (40, 39)]
    archived = [r['provider_id'] for r in rows(store, 'messages', f"chat='{stable_id('chat', w.account, '100')}'")]
    first, anchor = fetch(w, *page)
    assert anchor == min(archived, key=int)  # no anchor: before the oldest archived message of the chat
    assert first['fetched'] == 2 and first['stored_new'] == 2 and first['delivery'] == 'immediate'
    stored = rows(store, 'messages', f"id='{stable_id('msg', w.account, '100', '40')}'")
    assert len(stored) == 1
    events = [e for e in rows(store, 'events') if e['message'] == stored[0]['id']]
    assert events and {e['origin'] for e in events} == {'backfill'}
    before = counts(store)
    again, _ = fetch(w, *page)
    assert again['fetched'] == 2 and again['stored_new'] == 0 and counts(store) == before
    assert rows(store, 'collector_chats') == coverage


# spec: ANYM-477
def test_a_stalled_archive_write_frees_the_lease_fence_at_the_deadline(worker, monkeypatch):
    import threading
    import time
    w, store, _ = worker
    asyncio.run(w.sync_once())
    fence = Archive.fence
    def stalled(c, account, lease):
        fence(c, account, lease)
        # The write thread stalls on the client while its transaction holds the lease row.
        if threading.current_thread() is not threading.main_thread(): time.sleep(3)
    monkeypatch.setattr(Archive, 'fence', staticmethod(stalled))
    found = []
    job = {'operation': 'request_chat_history', 'platform': 'telegram', 'mutation': False, 'expires_at': 4e9, 'account': w.account,
           'chat_id': stable_id('chat', w.account, '100'), 'provider_chat': '100', 'provider_message_id': '50',
           'parameters': validate_operation('telegram', 'request_chat_history', {'count': 10}), '_history': found,
           '_policy': {'enabled': True, 'discover_chats': True, 'include_chats': [], 'exclude_chats': []}}
    asyncio.run(execute(Resolving(types.messages.Messages([hit(40, 'older synthetic', day=1)], [], [GROUP], [PERSON, MEMBER, OWNER])), job))
    assert found
    def lease_row_free():
        with store.pool.connection() as c:
            c.execute("SET LOCAL lock_timeout='500ms'")
            return bool(c.execute('SELECT 1 FROM collector_leases WHERE account=%s FOR UPDATE', (w.account,)).fetchone())
    async def timed_out():
        began = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(w.store_live_hits(found, label='chat_history', deadline=began + 1), 1)
        # Back on the foreground path (where the receipt and the next send claim run) while the thread still stalls.
        return asyncio.get_running_loop().time() - began, await asyncio.to_thread(lease_row_free)
    elapsed, free = asyncio.run(timed_out())  # asyncio.run then waits for the stalled thread itself
    assert elapsed < 2.5 and free  # the deadline plus STORE_GRACE, not the 3 s stall; the server ended the fence
