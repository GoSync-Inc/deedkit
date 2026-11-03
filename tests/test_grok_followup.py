"""Agent-facing regressions from the second review; synthetic sources only."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from messaging_service.adapters import telegram_message
from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.envelope import fingerprint, stable_id, validate
from messaging_service.presentation import render
from messaging_service.store import APIError
from test_archive import event
from test_client_contract import rpc


def test_status_compact_keeps_coverage_alerts_and_separate_operation_states():
    full = {'scope': 'permitted_accounts', 'coverage': 'partial', 'observed_at': '2026-10-14T06:00:00Z',
        'cache': {'hit': False, 'age_seconds': 0, 'max_age_seconds': 10}, 'sources': [{
        'account': 'synthetic-wa', 'platform': 'whatsapp', 'status': 'degraded',
        'newest_message_at': '2026-10-13T11:00:00Z', 'last_ingest_at': '2026-10-14T05:59:00Z',
        'alerts': ['provider_history_gaps'], 'freshness': {'state': 'recent_scan', 'scan_age_seconds': 2},
        'history': {'gap_chats': 1, 'full_history_verified': False},
        'operations': {'connection': {'state': 'connected'},
            'live_capture': {'state': 'backlogged', 'pending_events': 16},
            'history': {'state': 'partial_with_gaps'}, 'media': {'state': 'needs_attention'}},
        'files': {'download_errors': {'network_error': 603}, 'extraction': {'queued': 20},
            'enrichment': {'queued': 2}},
        'detail': {'explanation': 'long diagnostic ' * 1000}}]}
    compact = render('get_sync_status', full, 'compact')
    row = compact['sources'][0]
    assert 'detail' not in row
    assert row['history']['full_history_verified'] is False
    assert row['operations']['live_capture']['pending_events'] == 16
    assert row['files']['download_errors'] == {'network_error': 603}
    assert row['alerts'] == ['provider_history_gaps']
    assert compact['observed_at'] == full['observed_at']
    assert render('get_sync_status', full, 'full') == full
    assert 'detail' in full['sources'][0]  # Rendering never mutates the cache.


def test_chat_status_remains_detailed_in_compact_mode():
    status = {'chat_id': 'synthetic-chat', 'history': {'gaps': ['synthetic-gap']}}
    assert render('get_sync_status', status, 'compact') == status


def test_activity_alias_and_default_mcp_overview_are_recent_first(seeded):
    s, key, _ = seeded
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET sent_at=now() WHERE id='demo-tg-1'")
    with TestClient(create_app(s, ['testserver'])) as client:
        for arguments in ({'limit': 1}, {'limit': 1, 'order': 'last_activity_desc'}):
            response = rpc(client, key, 'list_chats', arguments).json()
            assert 'error' not in response
            result = response['result']['structuredContent']
            assert result['items'][0]['id'] == 'telegram-demo-chat'
        tools = client.post('/mcp', headers={'Authorization': 'Bearer '+key,
            'Accept': 'application/json, text/event-stream'}, json={
            'jsonrpc': '2.0', 'id': 8, 'method': 'tools/list', 'params': {}}).json()['result']['tools']
        schema = next(t for t in tools if t['name'] == 'list_chats')['inputSchema']
        assert 'last_activity_desc' in schema['properties']['order']['enum']
    # Explicit stable catalogue traversal and old direct SDK calls stay available.
    assert s.list_chats(key, order='id')['order'] == 'id'
    page = s.list_chats(key, order='last_activity_desc', limit=1)
    following = s.list_chats(key, order='activity', limit=1, after=page['next_cursor'])
    assert following['items'][0]['id'] != page['items'][0]['id']


def test_reply_queue_ignores_old_private_messages_and_known_bots(seeded):
    s, _, _ = seeded
    archive = Archive(s)
    archive.register('synthetic-inbox', 'telegram', 'Synthetic inbox', discover=True)
    now = datetime.now(timezone.utc)
    for chat, age, bot in [('recent', 1, False), ('old', 30, False), ('bot', 1, True)]:
        at = (now-timedelta(days=age)).isoformat()
        archive.ingest(event(account='synthetic-inbox', chat=chat, sent_at=at, updated_at=at,
            metadata={'chat_type': 'private', 'sender_is_bot': bot}))
    key = s.issue_key('synthetic-inbox-reader', ['synthetic-inbox'])
    result = s.list_chats(key, awaiting_reply=True, include_coverage=False)
    assert [r['provider_chat_id'] for r in result['items']] == ['recent']
    assert result['awaiting_reply_days'] == 14
    expanded = s.list_chats(key, awaiting_reply=True, awaiting_reply_days=0, include_coverage=False)
    assert {r['provider_chat_id'] for r in expanded['items']} == {'recent', 'old'}
    with pytest.raises(APIError):
        s.list_chats(key, awaiting_reply=True, awaiting_reply_days=-1)
    s.set_access('synthetic-inbox-reader', chats=[stable_id('chat', 'synthetic-inbox', 'old')])
    assert s.list_chats(key, awaiting_reply=True)['awaiting_reply_total'] == 0


def test_group_attention_age_is_its_message_age_not_unrelated_chat_activity(seeded):
    s, _, _ = seeded
    archive = Archive(s)
    archive.register('synthetic-group-age', 'telegram', 'Synthetic group', discover=True)
    now = datetime.now(timezone.utc)
    for provider, age, metadata in [('1', 30, {'mentions_owner': True}), ('2', 1, {})]:
        at = (now-timedelta(days=age)).isoformat()
        archive.ingest(event(account='synthetic-group-age', provider_id=provider,
            sent_at=at, updated_at=at, metadata={'chat_type': 'group', **metadata}))
    key = s.issue_key('synthetic-group-reader', ['synthetic-group-age'])
    assert s.list_chats(key, awaiting_reply=True)['items'] == []
    assert s.list_chats(key, awaiting_reply=True, awaiting_reply_days=0)['awaiting_reply_total'] == 1


def test_cached_telegram_entities_supply_title_type_and_bot_without_network():
    from telethon.types import Channel, User
    now = datetime.now(timezone.utc)
    msg = SimpleNamespace(chat_id=-1000000000123, id=1, sender_id=42, message='synthetic',
        date=now, edit_date=None, out=False, file=None, document=None, photo=None,
        sender=User(id=42, first_name='Synthetic bot', bot=True),
        chat=Channel(id=123, title='Synthetic channel', photo=None, date=now, broadcast=True))
    normalized = telegram_message('synthetic-entity', msg)
    assert normalized['chat_title'] == 'Synthetic channel'
    assert normalized['metadata']['chat_type'] == 'channel'
    assert normalized['metadata']['sender_is_bot'] is True
    assert telegram_message('synthetic-entity', msg, chat_title=str(msg.chat_id))['chat_title'] == 'Synthetic channel'
    msg.chat = Channel(id=456, title='Wrong peer', photo=None, date=now, broadcast=True)
    wrong = telegram_message('synthetic-entity', msg)
    assert wrong['chat_title'] == str(msg.chat_id)
    assert wrong['metadata']['chat_type'] is None
    msg.chat = None
    assert telegram_message('synthetic-entity', msg)['metadata']['chat_type'] is None


def test_chat_observation_does_not_change_message_revision_or_erase_known_type(seeded):
    s, _, _ = seeded
    archive = Archive(s)
    archive.register('synthetic-observation', 'telegram', 'Synthetic observation', discover=True)
    initial = event(account='synthetic-observation', chat_title='100')
    mid = archive.ingest(initial)['message']
    richer = {**initial, 'chat_title': 'Synthetic name', 'metadata': {'chat_type': 'group'}}
    assert fingerprint(validate(initial)) == fingerprint(validate(richer))
    assert archive.ingest(richer)['outcome'] == 'unchanged'
    archive.ingest({**richer, 'chat_title': '100', 'metadata': {'chat_type': None}})
    with s.pool.connection() as c:
        row = c.execute('SELECT ch.title,ch.chat_type,m.revision FROM messages m JOIN chats ch ON ch.id=m.chat WHERE m.id=%s', (mid,)).fetchone()
    assert row == {'title': 'Synthetic name', 'chat_type': 'group', 'revision': 1}


@pytest.mark.parametrize('metadata', [{'sender_is_bot': 'true'}, {'chat_type': 'invented'}])
def test_provider_chat_metadata_is_strict(metadata):
    with pytest.raises(ValueError, match='invalid_metadata'):
        validate(event(metadata=metadata))


def test_sync_status_accepts_explicit_full_detail_over_mcp(seeded):
    s, key, _ = seeded
    # ANYM-671: an account with no collector reports a not_ready outbound block in compact, so the quiet overview
    # this budget measures has connected collectors, as in a deployment.
    from messaging_service.collector_state import CollectorState
    archive = Archive(s)
    for account, platform in (('telegram-demo', 'telegram'), ('whatsapp-demo-1', 'whatsapp')):
        archive.register(account, platform, 'Synthetic ' + account, discover=True)
        CollectorState(archive, account, ('budget-worker', archive.acquire(account, 'budget-worker'))).phase(
            'connection', 'connected', 'provider_ready')
        archive.set_sending(account, True)
    with TestClient(create_app(s, ['testserver'])) as client:
        for detail in ['compact', 'full']:
            response = rpc(client, key, 'get_sync_status', {'detail': detail}).json()
            assert 'error' not in response
            result = response['result']['structuredContent']
            assert result['sources']
            assert result['coverage'] == 'partial'
            if detail == 'compact':
                assert len(json.dumps(response).encode()) < 8192
