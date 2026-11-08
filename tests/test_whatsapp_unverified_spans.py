"""ANYM-813: a native WhatsApp catch-up that stalls on the local cache records an unverified span."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from messaging_service.archive import Archive
from messaging_service.blobs import BlobWriter
from messaging_service.collector_state import CaptureSpool
from messaging_service.envelope import stable_id
from messaging_service.presentation import render
from messaging_service.sync_status import get_sync_status
from messaging_service.whatsapp_collector import WhatsAppCollector, WhatsAppState
from test_whatsapp import event, capture

A, B = '111@c.us', '222@c.us'
DAY = 86400


def at(mid):
    return datetime.fromtimestamp(event(mid)['timestamp'], timezone.utc).isoformat()


class NativeCache:
    """The native adapter's local cache: never exhausted, never asks the provider (whatsmeow history.go)."""
    native_history = True
    exhausted = False

    def __init__(self):
        self.cache = {A: [], B: []}
        self.pages = 0
        self.policy = None

    def subscribe(self, *args): pass
    async def connect(self): pass
    async def disconnect(self): pass
    async def set_policy(self, policy): self.policy = policy
    async def dialogs(self, limit): return [(A, 'user', 'Fixture A'), (B, 'user', 'Fixture B')]

    def keep(self, chat, *mids):
        self.cache[chat] = [event(m, chat=chat) for m in mids]

    async def page(self, chat, *, limit, before=None):
        self.pages += 1
        selected = sorted((e for e in self.cache[chat] if not before or [e['timestamp'], e['messageId']] < before),
                          key=lambda e: [e['timestamp'], e['messageId']])
        return {'messages': deepcopy(selected[-limit:]), 'exhausted': self.exhausted, 'pending': False}


class BrowserCache(NativeCache):
    """The browser provider: an empty page reports pending, never a stall rule."""
    native_history = False

    async def page(self, chat, *, limit, before=None):
        page = await super().page(chat, limit=limit, before=before)
        return {**page, 'pending': not page['messages']}


def collector(seeded, tmp_path, provider):
    s, _, _ = seeded
    a = Archive(s); a.register('wa-one', 'whatsapp', 'Synthetic WA', discover=True)
    w = WhatsAppCollector(a, 'wa-one', provider, CaptureSpool(tmp_path/'pages', reserve_bytes=0),
        events_spool=CaptureSpool(tmp_path/'events', reserve_bytes=0), batch_size=2,
        writer=BlobWriter(s.files, reserve_bytes=0), owner='wa-fixture')
    epoch = a.acquire(w.account, w.owner); w.state = WhatsAppState(a, w.account, (w.owner, epoch))
    w.policy = w.state.policy()
    return w, s


@pytest.fixture
def native(seeded, tmp_path):
    return collector(seeded, tmp_path, NativeCache())


def scan(w, times=1):
    for _ in range(times):
        asyncio.run(w.sync_once())


def progress(w, chat=A):
    return w.state.progress(chat, 'user')


def history(w, s, chats=None):
    with s.pool.connection() as c:
        return get_sync_status(c, {'accounts': [w.account], 'chat_ids': chats})['sources'][0]['history']


def start_catchup(w, chat, head, newer):
    """The head is archived, then the cache holds only newer messages: the pointer sits on the newest page (2)."""
    w.provider.keep(chat, head-1, head); scan(w)
    w.provider.keep(chat, *newer); scan(w)
    assert progress(w, chat)['catchup_before'][0] == event(newer[-2])['timestamp']


# spec: ANYM-813
def test_two_empty_native_pages_record_the_span_and_one_does_not(native):
    w, s = native
    start_catchup(w, A, 5, [20, 21])
    assert history(w, s)['catchup_pending_chats'] == 1
    pages = w.provider.pages
    scan(w)
    first = progress(w)
    assert 'unverified_span' not in first and first['catchup_stalled_scans'] == 1
    # The pointer did not move in the last scan: it no longer counts as a catch-up in progress.
    assert history(w, s)['catchup_pending_chats'] == 0 and history(w, s)['unverified_span_chats'] == 0
    scan(w)
    second = progress(w)
    assert second['unverified_span']['from'] == at(5) and second['unverified_span']['to'] == at(20)
    assert second['unverified_span']['stalled_since'] is not None
    # The local cache read stays as it was: same pointer, one cache page per chat and scan, no other request.
    assert second['catchup_before'] == first['catchup_before'] and second['head'] == first['head']
    assert w.provider.pages-pages == 2*2
    status = history(w, s)
    assert status['unverified_span_chats'] == 1 and status['catchup_pending_chats'] == 0
    assert status['gap_chats'] == 0 and 'gap' not in second


# spec: ANYM-813
def test_a_moving_page_keeps_the_chat_pending(native):
    w, s = native
    start_catchup(w, A, 5, [10, 11, 20, 21])
    scan(w)
    moved = progress(w)
    assert moved['catchup_before'][0] == event(10)['timestamp']
    assert 'unverified_span' not in moved and 'catchup_stalled_scans' not in moved
    assert history(w, s)['catchup_pending_chats'] == 1 and history(w, s)['unverified_span_chats'] == 0


# spec: ANYM-813
def test_a_cached_page_that_reaches_the_head_completes_the_catchup_and_keeps_the_span(native):
    w, s = native
    start_catchup(w, A, 5, [20, 21]); scan(w, 2)
    span = progress(w)['unverified_span']
    # The cache now bridges to the old head; messages 6..19 are still missing inside.
    w.provider.keep(A, 5, 20, 21); scan(w)
    done = progress(w)
    assert 'catchup_before' not in done and done['head'][0] == event(21)['timestamp']
    assert done['unverified_span'] == span and 'catchup_stalled_scans' not in done
    # A later stall never replaces the recorded span.
    w.provider.keep(A, 40, 41); scan(w, 3)
    assert progress(w)['unverified_span'] == span
    status = history(w, s)
    assert status['unverified_span_chats'] == 1 and status['catchup_pending_chats'] == 0


# spec: ANYM-813
def test_a_flagged_gap_chat_is_left_as_it_is(native):
    w, s = native
    w.provider.keep(A, 4, 5); scan(w)
    # An exhausted page that does not reach the head flags the gap (ANYM-700).
    w.provider.keep(A, 20, 21); w.provider.exhausted = True; scan(w)
    assert progress(w)['gap'] is True
    w.provider.exhausted = False
    w.provider.keep(A, 30, 31); scan(w, 3)
    flagged = progress(w)
    assert flagged['gap'] is True and 'unverified_span' not in flagged
    status = history(w, s)
    assert status['gap_chats'] == 1 and status['unverified_span_chats'] == 0
    assert status['gap_chat_ids'] == [{'chat_id': stable_id('chat', w.account, A), 'span': 'unknown'}]


# spec: ANYM-813
def test_the_browser_provider_is_unchanged(seeded, tmp_path):
    w, s = collector(seeded, tmp_path, BrowserCache())
    start_catchup(w, A, 5, [20, 21]); scan(w, 3)
    p = progress(w)
    assert 'unverified_span' not in p and 'catchup_stalled_scans' not in p
    assert history(w, s)['catchup_pending_chats'] == 1 and history(w, s)['unverified_span_chats'] == 0


# spec: ANYM-813
def test_status_lists_spans_by_length_in_full_counts_them_in_compact_and_scopes_them(native):
    from messaging_service.chat_evidence import history as chat_history, rows
    w, s = native
    start_catchup(w, A, 5, [3*DAY, 3*DAY+1])
    start_catchup(w, B, 5, [DAY, DAY+1])
    scan(w, 2)
    full = history(w, s)
    a, b = stable_id('chat', w.account, A), stable_id('chat', w.account, B)
    assert full['unverified_span_chats'] == 2
    assert [(x['chat_id'], x['from'], x['to'], x['days']) for x in full['unverified_spans']] == [
        (a, at(5), at(3*DAY), 3.0), (b, at(5), at(DAY), 1.0)]
    assert full['gap_chat_ids'] == []
    key = s.issue_key('wa-span-reader', [w.account])
    compact = render('get_sync_status', s.get_sync_status(key, refresh=True), 'compact')['sources'][0]['history']
    assert compact['unverified_span_chats'] == 2 and 'unverified_spans' not in compact and 'sources' not in compact
    # A chat-scoped key sees only its chats' spans.
    narrow = s.issue_key('wa-span-narrow', [w.account], chats=[b])
    scoped = s.get_sync_status(narrow, refresh=True)['sources'][0]['history']
    assert scoped['unverified_span_chats'] == 1 and [x['chat_id'] for x in scoped['unverified_spans']] == [b]
    # The chat's own status shows its span; chat evidence keeps today's gaps and state (not a gap, no caveat).
    chat = s.get_sync_status(key, chat_id=a)['history']
    assert chat['unverified_span']['from'] == at(5) and chat['unverified_span']['days'] == 3.0
    assert chat['gaps'] == ['catchup_pending'] and chat['state'] == 'gaps'
    with s.pool.connection() as c:
        assert chat_history(rows(c, {'accounts': [w.account], 'chat_ids': [b]})[0])['gaps'] == ['catchup_pending']


# spec: ANYM-813
def test_history_sources_name_the_pairing_batch_live_since_and_the_older_messages_call(native):
    w, s = native
    w.provider.keep(A, 4, 5); scan(w)
    with s.pool.connection() as c:
        row = get_sync_status(c, {'accounts': [w.account], 'chat_ids': None})['sources'][0]
    assert row['history']['sources'] == {'pairing_batch_days': 7, 'pairing_batch_per_chat': 50,
        'live_since': None, 'older_messages_call': 'request_chat_history'}
    # A live message under the browser provider is not a native capture.
    w.state.provider_observation('webjs'); w.state.capture_progress(capture(event(6, chat=A)), 1, 0)
    assert history(w, s)['sources']['live_since'] is None
    w.state.provider_observation('whatsmeow'); w.state.capture_progress(capture(event(7, chat=A)), 1, 0)
    first = history(w, s)['sources']['live_since']
    assert first is not None
    w.state.capture_progress(capture(event(8, chat=A)), 1, 0)
    assert history(w, s)['sources']['live_since'] == first
    assert row['detail_code'] == 'whatsapp_history_partial'
    explanation = row['detail']['explanation']
    assert 'request_chat_history' in explanation and explanation.count('. ') == 0 and explanation.endswith('.')


# spec: ANYM-813
def test_tool_descriptions_name_unverified_spans_and_the_older_messages_call():
    from test_phone_next_call import schemas
    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root/'services/gateway/src/agent_gateway/catalog.json').read_text())
    published = next(t for t in catalog if t['module'] == 'messaging' and t['operation'] == 'get_sync_status')
    for description in (schemas()['get_sync_status']['description'], published['description']):
        sentence = next(line for line in description.split('\n') if 'unverified_span_chats' in line)
        assert 'unverified_spans' in sentence and 'request_chat_history' in sentence and 'not a gap' in sentence
