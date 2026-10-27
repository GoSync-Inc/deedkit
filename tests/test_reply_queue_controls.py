"""Reply-queue reasons, snoozes, undo, mutes and their list (ANYM-281); synthetic provider data only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.envelope import stable_id
from messaging_service.presentation import render
from messaging_service.store import APIError
from test_archive import event

ROOT = Path(__file__).resolve().parents[1]
CONTROLS = ('snooze_reply', 'restore_reply', 'mute_reply_source', 'unmute_reply_source', 'list_reply_dismissals')
# Values that move with the wall clock between two reads of the same row.
VOLATILE = {'awaiting_reply_waiting_hours', 'awaiting_reply_priority', 'awaiting_reply_priority_reasons',
            'awaiting_reply_priority_components', 'freshness', 'last_activity_age_hours'}


@pytest.fixture()
def inbox(seeded):
    s, _, _ = seeded
    ar = Archive(s)
    ar.register('controls-test', 'telegram', 'Synthetic controls', discover=True)
    key = s.issue_key('controls-agent', ['controls-test'])
    def add(chat, mid, seconds=0, account='controls-test', hours=2, **changes):
        at = (datetime.now(timezone.utc)-timedelta(hours=hours)+timedelta(seconds=seconds)).isoformat()
        return ar.ingest(event(account=account, chat=chat, provider_id=str(mid), sent_at=at, updated_at=at,
            **{'metadata':{'chat_type':'private'}, **changes}))['message']
    def queue(token=None, **kwargs):
        return s.list_chats(token or key, awaiting_reply=True, include_coverage=False, **kwargs)
    return s, ar, key, add, queue


def chat_of(provider_chat, account='controls-test'):
    return stable_id('chat', account, provider_chat)


def hint(row):
    return row['awaiting_reply_message_id'], row['awaiting_reply_revision']


def stable(rows):
    return [{k: v for k, v in row.items() if k not in VOLATILE} for row in rows]


def provider_effects(s):
    with s.pool.connection() as c:
        return (c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n'],
                c.execute('SELECT count(*) AS n FROM chat_read_observations').fetchone()['n'])


# spec: ANYM-281
def test_dismiss_reply_records_reason(inbox):
    s, ar, key, add, queue = inbox
    first, second = add('100', 1), add('200', 1, sender='77')
    plain = s.dismiss_reply(key, chat_of('200'), second, 1)
    assert plain['dismissed'] is True and not {'suggest', 'reason', 'note'} & set(plain)
    for bad in ({'reason': 'later'}, {'note': ''}, {'note': 'x'*201}, {'note': 'line\nbreak'}, {'note': 'bell\x07'}):
        with pytest.raises(APIError, match='invalid_filter'):
            s.dismiss_reply(key, chat_of('100'), first, 1, **bad)
    assert [r['awaiting_reply_message_id'] for r in queue()['items']] == [first]
    result = s.dismiss_reply(key, chat_of('100'), first, 1, reason='spam_or_mailing', note='Synthetic promo <b>x</b>')
    assert result['dismissed'] is True and result['suggest'] == 'mute_reply_source'
    assert 'suggest' not in s.dismiss_reply(key, chat_of('100'), first, 1, reason='no_reply_needed')
    assert queue()['items'] == []
    items = {i['chat_id']: i for i in s.list_reply_dismissals(key, kind='dismissed')['items']}
    assert items[chat_of('100')]['reason'] == 'no_reply_needed' and 'note' not in items[chat_of('100')]
    assert 'reason' not in items[chat_of('200')] and 'note' not in items[chat_of('200')]
    s.dismiss_reply(key, chat_of('100'), first, 1, reason='other', note='Synthetic promo <b>x</b>')
    item = next(i for i in s.list_reply_dismissals(key)['items'] if i['chat_id'] == chat_of('100'))
    assert (item['reason'], item['note']) == ('other', 'Synthetic promo <b>x</b>')


# spec: ANYM-281
def test_snooze_reply_returns_after_until(inbox):
    s, ar, key, add, queue = inbox
    mid = add('100', 1)
    chat = chat_of('100')
    with s.pool.connection() as c:
        now = c.execute('SELECT clock_timestamp() AS t').fetchone()['t']
    for bad in ('', 'tomorrow', (now+timedelta(days=1)).replace(tzinfo=None).isoformat(),
                (now-timedelta(minutes=1)).isoformat(), (now+timedelta(days=90, minutes=5)).isoformat()):
        with pytest.raises(APIError, match='invalid_date_range'):
            s.snooze_reply(key, chat, mid, 1, bad)
    assert s.snooze_reply(key, chat, mid, 1, (now+timedelta(days=89)).isoformat())['snoozed'] is True
    until = (now+timedelta(days=30)).isoformat()
    result = s.snooze_reply(key, chat, mid, 1, until, note='Remind me')
    assert result['snoozed'] is True and result['provider_read_changed'] is False
    assert queue()['items'] == []
    other = s.issue_key('controls-agent', ['controls-test'])
    assert queue(other)['items']
    # A fixed database clock: the snooze falls due and the hint is older than the 14-day window.
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET sent_at=sent_at-interval '20 days' WHERE chat=%s", (chat,))
        c.execute("UPDATE reply_snoozes SET snoozed_until=CURRENT_TIMESTAMP-interval '1 minute' WHERE chat=%s", (chat,))
    assert queue(other)['items'] == []
    row = queue()['items'][0]
    assert row['awaiting_reply_message_id'] == mid and 'snooze_due' in row['awaiting_reply_priority_reasons']
    shown = render('list_chats', queue(view='conversation'), 'compact')['items'][0]['awaiting_reply']
    assert shown['snoozed_until'] == row['awaiting_reply_snoozed_until'] < datetime.now(timezone.utc).isoformat()
    # A new incoming returns the row early, without the reminder.
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET sent_at=sent_at+interval '20 days' WHERE chat=%s", (chat,))
    s.snooze_reply(key, chat, mid, 1, until)
    assert queue()['items'] == []
    newer = add('100', 2, 5)
    row = queue()['items'][0]
    assert row['awaiting_reply_message_id'] == newer and 'snooze_due' not in row['awaiting_reply_priority_reasons']
    # A dismissal replaces the snooze and the other way round.
    s.snooze_reply(key, chat, newer, 1, until)
    s.dismiss_reply(key, chat, newer, 1)
    assert s.list_reply_dismissals(key, kind='snoozed')['items'] == []
    assert len(s.list_reply_dismissals(key, kind='dismissed')['items']) == 1


# spec: ANYM-281
def test_dismiss_target_skips_muted_sender(inbox):
    s, ar, key, add, queue = inbox
    group = {'chat_type': 'group', 'mentions_owner': True}
    b = add('300', 1, 1, sender='8', body='Older mention B', metadata=group)
    a = add('300', 2, 2, sender='7', body='Newer mention A', metadata=group)
    chat = chat_of('300')
    assert queue(view='conversation')['items'][0]['awaiting_reply_message_id'] == a
    s.mute_reply_source(key, account='controls-test', sender_id='7')
    assert queue(view='conversation')['items'][0]['awaiting_reply_message_id'] == b
    with pytest.raises(APIError, match='reply_target_changed'):
        s.dismiss_reply(key, chat, a, 1)
    assert s.dismiss_reply(key, chat, b, 1)['dismissed'] is True
    assert queue(view='conversation')['items'] == []
    # A chat mute does not change the target: the dismissal still applies after the unmute.
    s.mute_reply_source(key, chat_id=chat)
    s.unmute_reply_source(key, chat_id=chat)
    assert queue(view='conversation')['items'] == []


# spec: ANYM-281
def test_mute_sender_hides_future_rows_until_unmuted(inbox):
    s, ar, key, add, queue = inbox
    before = provider_effects(s)
    add('100', 1, 1)
    ar.register('controls-two', 'telegram', 'Synthetic second', discover=True)
    s.set_access('controls-agent', accounts=['controls-test', 'controls-two'])
    add('100', 1, 1, account='controls-two')
    sibling = s.issue_key('controls-agent', ['controls-test', 'controls-two'])
    stranger = s.issue_key('controls-stranger', ['controls-test'])
    for bad in ({'reason': 'later'}, {'note': ''}):
        with pytest.raises(APIError, match='invalid_filter'):
            s.mute_reply_source(key, account='controls-test', sender_id='42', **bad)
    with pytest.raises(APIError, match='invalid_filter'):
        s.mute_reply_source(key, chat_id=chat_of('100'), sender_id='42')
    with pytest.raises(APIError, match='not_found'):
        s.mute_reply_source(key, account='controls-test', sender_id='never-wrote')
    muted = s.mute_reply_source(key, account='controls-test', sender_id='42', reason='spam_or_mailing')
    assert muted['muted'] is True and muted['created'] is True and muted['provider_changed'] is False
    again = s.mute_reply_source(key, account='controls-test', sender_id='42', note='Synthetic mailing')
    assert again['created'] is False and again['note'] == 'Synthetic mailing'
    # One sender ID in two accounts needs one mute each.
    assert [r['account'] for r in queue()['items']] == ['controls-two']
    assert queue()['muted_total'] == 1
    add('100', 2, 5)
    assert [r['account'] for r in queue(sibling)['items']] == ['controls-two']
    assert [r['provider_chat_id'] for r in queue(stranger)['items']] == ['100']
    shown = queue(include_muted=True)
    row = next(r for r in shown['items'] if r['account'] == 'controls-test')
    assert row['awaiting_reply_muted'] == 'sender'
    with pytest.raises(APIError, match='reply_target_changed'):
        s.dismiss_reply(key, row['id'], *hint(row))
    assert s.unmute_reply_source(sibling, account='controls-test', sender_id='42')['unmuted'] is True
    assert s.unmute_reply_source(sibling, account='controls-test', sender_id='42')['unmuted'] is False
    assert len(queue()['items']) == 2 and 'muted_total' not in queue()
    # A chat mute covers rows created by later messages.
    s.mute_reply_source(key, chat_id=chat_of('100'))
    add('100', 3, 9)
    assert [r['account'] for r in queue()['items']] == ['controls-two']
    shown = render('list_chats', queue(include_muted=True, view='conversation'), 'compact')['items']
    assert next(r for r in shown if r['account'] == 'controls-test')['awaiting_reply']['muted'] == 'chat'
    # A WhatsApp sender is muted by its exact ID.
    ar.register('controls-wa', 'whatsapp', 'Synthetic WhatsApp', discover=True)
    s.set_access('controls-agent', accounts=['controls-test', 'controls-two', 'controls-wa'])
    add('321@c.us', 1, 1, account='controls-wa', platform='whatsapp', sender='321@c.us')
    s.mute_reply_source(key, account='controls-wa', sender_id='321@c.us')
    assert all(r['account'] != 'controls-wa' for r in queue()['items'])
    assert provider_effects(s) == before


# spec: ANYM-281
def test_mute_limit_admits_one_of_two_concurrent_mutes(inbox):
    s, ar, key, add, queue = inbox
    add('100', 1, 1);add('200', 1, 2, sender='77')
    with s.pool.connection() as c:
        c.execute('''INSERT INTO reply_mutes(principal,account,sender)
            SELECT 'controls-agent','controls-test','synthetic-'||n FROM generate_series(1,999) n''')
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(s.mute_reply_source, key, chat_id=chat_of(c)) for c in ('100', '200')]
    outcomes = []
    for future in futures:
        try:outcomes.append(future.result()['muted'])
        except APIError as error:outcomes.append(str(error))
    assert sorted(map(str, outcomes)) == ['True', 'mute_limit_reached']
    # A repeated mute at the limit still updates.
    done = next(c for c, o in zip(('100', '200'), outcomes) if o is True)
    assert s.mute_reply_source(key, chat_id=chat_of(done), reason='other')['created'] is False
    with s.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM reply_mutes WHERE principal='controls-agent'").fetchone()['n'] == 1000


# spec: ANYM-281
def test_restore_reply_recomputes_the_row(inbox):
    s, ar, key, add, queue = inbox
    group = {'chat_type': 'group', 'mentions_owner': True}
    private = add('100', 1, 1)
    add('300', 1, 2, sender='8', body='Group mention', metadata=group)
    snoozed = add('400', 1, 3, sender='9')
    def rows():
        return {r['id']: r for r in stable(render('list_chats', queue(view='conversation'), 'full')['items'])}
    before = rows()
    for provider in ('100', '300'):
        row = next(r for r in queue(view='conversation')['items'] if r['provider_chat_id'] == provider)
        s.dismiss_reply(key, row['id'], *hint(row), reason='handled_elsewhere')
    with s.pool.connection() as c:
        until = (c.execute('SELECT clock_timestamp() AS t').fetchone()['t']+timedelta(days=3)).isoformat()
    s.snooze_reply(key, chat_of('400'), snoozed, 1, until)
    assert queue(view='conversation')['items'] == []
    for item in s.list_reply_dismissals(key, kind='dismissed')['items']+s.list_reply_dismissals(key, kind='snoozed')['items']:
        result = s.restore_reply(key, item['chat_id'], item['message_id'], item['revision'])
        assert result['restored'] is True and result['removed']['active'] is True
        assert result['provider_read_changed'] is False
    after = rows()
    assert after.keys() == before.keys()
    def changed(a, b, path=''):
        if isinstance(a, dict) and isinstance(b, dict):
            return [x for key in a.keys() | b.keys() for x in changed(a.get(key), b.get(key), path+'.'+key)]
        return [] if a == b or path.endswith('.as_of') else [(path, a, b)]
    assert [x for k in before for x in changed(before[k], after[k])] == []
    # A repeated dismissal is one entry; a stale message ID removes nothing.
    s.dismiss_reply(key, chat_of('100'), private, 1);s.dismiss_reply(key, chat_of('100'), private, 1)
    assert s.restore_reply(key, chat_of('100'), snoozed, 1)['restored'] is False
    assert s.restore_reply(key, chat_of('100'), private, 2)['restored'] is False
    assert s.restore_reply(key, chat_of('100'), private, 1)['removed']['kind'] == 'dismissed'
    assert s.restore_reply(key, chat_of('100'), private, 1)['restored'] is False
    # After a newer incoming the old entry no longer hides the row.
    s.dismiss_reply(key, chat_of('100'), private, 1)
    newer = add('100', 2, 9)
    removed = s.restore_reply(key, chat_of('100'), private, 1)['removed']
    assert removed['active'] is False and queue()['items'][0]['awaiting_reply_message_id'] != private
    # A lost response: the repeated restore also removes a dismissal made in between, as documented.
    s.dismiss_reply(key, chat_of('100'), newer, 1)
    s.restore_reply(key, chat_of('100'), newer, 1)
    s.dismiss_reply(key, chat_of('100'), newer, 1)
    assert s.restore_reply(key, chat_of('100'), newer, 1)['restored'] is True
    assert s.list_reply_dismissals(key)['items'] == []
    # Restore never touches a mute.
    s.mute_reply_source(key, chat_id=chat_of('100'))
    assert s.restore_reply(key, chat_of('100'), newer, 1)['restored'] is False
    assert len(s.list_reply_dismissals(key, kind='muted')['items']) == 1


# spec: ANYM-281
def test_list_reply_dismissals_scope(inbox):
    s, ar, key, add, queue = inbox
    first, second = add('100', 1, 1, body='Synthetic first'), add('200', 1, 2, sender='77')
    s.dismiss_reply(key, chat_of('100'), first, 1, note='Wrong one')
    s.dismiss_reply(key, chat_of('200'), second, 1)
    s.mute_reply_source(key, account='controls-test', sender_id='77')
    page = s.list_reply_dismissals(key, limit=1)
    assert page['total'] == 2 and page['next_cursor'] and page['scope'] == 'this_credential'
    item = page['items'][0]
    assert {'target', 'chat_id', 'title', 'sender_id', 'created_at', 'message_id', 'revision', 'active', 'snippet'} <= set(item)
    assert item['target'] == 'chat' and item['active'] is True
    sibling = s.issue_key('controls-agent', ['controls-test'])
    with pytest.raises(APIError, match='invalid_cursor'):
        s.list_reply_dismissals(sibling, after=page['next_cursor'], limit=1)
    assert s.list_reply_dismissals(sibling)['items'] == []
    muted = s.list_reply_dismissals(sibling, kind='muted')
    assert muted['scope'] == 'this_principal' and muted['items'][0]['target'] == 'sender'
    assert muted['items'][0]['sender_id'] == '77' and 'chat_id' not in muted['items'][0]
    for bad in ({'kind': 'archived'}, {'limit': 0}, {'limit': 101}):
        with pytest.raises(APIError):
            s.list_reply_dismissals(key, **bad)
    restored = s.restore_reply(key, item['chat_id'], item['message_id'], item['revision'])
    assert restored['restored'] is True
    # A narrowed scope hides entries without deleting them, and reveals nothing.
    s.dismiss_reply(key, chat_of('100'), first, 1)
    s.set_access('controls-agent', chats=[chat_of('100')])
    assert [i['chat_id'] for i in s.list_reply_dismissals(key)['items']] == [chat_of('100')]
    assert s.list_reply_dismissals(key)['total'] == 1
    assert s.list_reply_dismissals(key, kind='muted')['items'] == []
    with pytest.raises(APIError, match='not_found'):
        s.restore_reply(key, chat_of('200'), second, 1)
    with pytest.raises(APIError, match='not_found'):
        s.unmute_reply_source(key, account='controls-test', sender_id='77')
    s.set_access('controls-agent', chats=None)
    assert s.list_reply_dismissals(key)['total'] == 2
    assert len(s.list_reply_dismissals(key, kind='muted')['items']) == 1


# spec: ANYM-281
def test_reply_controls_access_and_rotation(inbox):
    s, ar, key, add, queue = inbox
    mid = add('100', 1, 1)
    chat = chat_of('100')
    readonly = s.issue_key('controls-readonly', ['controls-test'], permissions=['read'])
    for call in (lambda: s.snooze_reply(readonly, chat, mid, 1, '2999-01-01T00:00:00Z'),
                 lambda: s.restore_reply(readonly, chat, mid, 1),
                 lambda: s.mute_reply_source(readonly, chat_id=chat),
                 lambda: s.unmute_reply_source(readonly, chat_id=chat)):
        with pytest.raises(APIError, match='forbidden'):
            call()
    assert s.list_reply_dismissals(readonly)['items'] == []
    hidden = s.issue_key('controls-hidden', ['controls-test'], chats=[])
    for call in (lambda: s.restore_reply(hidden, chat, mid, 1), lambda: s.mute_reply_source(hidden, chat_id=chat)):
        with pytest.raises(APIError, match='not_found'):
            call()
    s.dismiss_reply(key, chat, mid, 1)
    s.mute_reply_source(key, account='controls-test', sender_id='42')
    # A rotation drops the key's dismissals and keeps the principal's mutes.
    rotated = s.issue_key('controls-agent', ['controls-test'])
    s.revoke(credential_id=s.capabilities(key)['credential_id'])
    with pytest.raises(APIError):
        s.mute_reply_source(key, chat_id=chat)
    assert s.list_reply_dismissals(rotated)['items'] == []
    assert s.list_reply_dismissals(rotated, kind='muted')['items'][0]['sender_id'] == '42'
    assert queue(rotated)['items'] == [] and queue(rotated)['muted_total'] == 1
    with s.pool.connection() as c:
        ops = {r['operation'] for r in c.execute(
            "SELECT DISTINCT operation FROM audit_log WHERE operation=ANY(%s) AND outcome='ok'", (['dismiss_reply', *CONTROLS],)).fetchall()}
    assert {'dismiss_reply', 'mute_reply_source', 'list_reply_dismissals'} <= ops


# spec: ANYM-281
def test_gateway_role_runs_reply_controls(inbox):
    s, ar, key, add, queue = inbox
    mid = add('100', 1, 1)
    chat = chat_of('100')
    role = 'test_reply_controls_'+uuid4().hex
    ddl = (ROOT/'services/messaging/messaging_service/gateway_role.sql').read_text().replace('messaging_gateway', role)
    class RestrictedPool:
        @contextmanager
        def connection(self):
            with s.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                yield c
    restricted = copy(s);restricted.pool = RestrictedPool()
    with s.pool.connection() as c:c.execute(ddl)
    try:
        with s.pool.connection() as c:
            until = (c.execute('SELECT clock_timestamp() AS t').fetchone()['t']+timedelta(days=1)).isoformat()
        assert restricted.dismiss_reply(key, chat, mid, 1, reason='other', note='Synthetic')['dismissed']
        assert restricted.snooze_reply(key, chat, mid, 1, until)['snoozed']
        assert restricted.restore_reply(key, chat, mid, 1)['restored']
        assert restricted.dismiss_reply(key, chat, mid, 1)['dismissed']
        assert restricted.restore_reply(key, chat, mid, 1)['restored']
        assert restricted.mute_reply_source(key, chat_id=chat)['created']
        assert not restricted.mute_reply_source(key, chat_id=chat, note='Again')['created']
        assert restricted.list_reply_dismissals(key, kind='muted')['items']
        assert restricted.unmute_reply_source(key, chat_id=chat)['unmuted']
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with restricted.pool.connection() as c:c.execute('UPDATE chat_read_observations SET unread_count=0')
    finally:
        with s.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


# spec: ANYM-281
def test_reply_controls_surface_and_backup(inbox):
    from messaging_service.backup import database_manifest
    s, ar, key, add, queue = inbox
    mid = add('100', 1, 1)
    caps = s.capabilities(key)
    assert set(CONTROLS) <= set(caps['available']) and set(CONTROLS) <= set(caps['tool_manifest']['names'])
    headers = {'Authorization': 'Bearer '+key}
    with TestClient(create_app(s, ['testserver'])) as client:
        tools = {t['name']: t for t in client.post('/mcp', headers={**headers, 'Accept': 'application/json, text/event-stream'},
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).json()['result']['tools']}
        muted = client.post('/v1/mute_reply_source', headers=headers, json={'chat_id': chat_of('100')})
        listed = client.post('/v1/list_reply_dismissals', headers=headers, json={'kind': 'muted'})
        shown = client.post('/v1/list_chats', headers=headers, json={'awaiting_reply': True, 'include_muted': True})
    assert set(CONTROLS) <= set(tools) and 'include_muted' in tools['list_chats']['inputSchema']['properties']
    assert {'reason', 'note'} <= set(tools['dismiss_reply']['inputSchema']['properties'])
    assert muted.status_code == 200 and listed.json()['items'][0]['chat_id'] == chat_of('100')
    assert shown.json()['muted_total'] == 1
    catalog = {t['name']: t for t in json.loads((ROOT/'services/gateway/src/agent_gateway/catalog.json').read_text())}
    for name in CONTROLS:
        gateway = catalog['messages_'+name]
        assert gateway['scopes'] == (['read'] if name == 'list_reply_dismissals' else ['cursor-write'])
        assert gateway['description'] == tools[name]['description']
    with s.pool.connection() as c:
        manifest = database_manifest(c)
        assert manifest['reply_mutes']['rows'] == 1 and manifest['reply_snoozes']['rows'] == 0
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '49'
    assert s.list_reply_dismissals(key)['items'] == [] and mid


# spec: ANYM-281
def test_due_snooze_does_not_revive_a_superseded_hint(inbox):
    s, ar, key, add, queue = inbox
    first = add('100', 1)
    chat = chat_of('100')
    with s.pool.connection() as c:
        until = (c.execute('SELECT clock_timestamp() AS t').fetchone()['t']+timedelta(days=30)).isoformat()
    s.snooze_reply(key, chat, first, 1, until)
    newer = add('100', 2, 5)
    assert hint(queue()['items'][0]) == (newer, 1)
    # Both hints age out of the 14-day window, then the stale snooze falls due.
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET sent_at=sent_at-interval '20 days' WHERE chat=%s", (chat,))
        c.execute("UPDATE reply_snoozes SET snoozed_until=CURRENT_TIMESTAMP-interval '1 minute' WHERE chat=%s", (chat,))
    assert queue()['items'] == []
    with pytest.raises(APIError, match='reply_target_changed'):
        s.dismiss_reply(key, chat, first, 1)


# spec: ANYM-281
@pytest.mark.parametrize('view', ['archive', 'conversation'])
def test_due_group_snooze_reads_only_messages_since_its_hint(inbox, view):
    from messaging_service.chat_queries import list_chats
    from test_reply_queue_scale import QueueMeasured
    from test_search_performance import scans, visited
    s, ar, key, add, queue = inbox
    group = {'metadata': {'chat_type': 'group', 'mentions_owner': True}}
    first = add('500', 1, 1, **group)
    chat = chat_of('500')
    # A dense older history of owner mentions before the snoozed hint.
    with s.pool.connection() as c:
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'dense-'||n,m.account,m.chat,(100000+n)::text,m.sender,m.body,
                m.sent_at-interval '1 day'-n*interval '1 hour','incoming'
            FROM messages m CROSS JOIN generate_series(1,300) n WHERE m.id=%s''', (first,))
        c.execute('''INSERT INTO message_metadata(message,revision,metadata)
            SELECT 'dense-'||n,1,md.metadata FROM message_metadata md CROSS JOIN generate_series(1,300) n
            WHERE md.message=%s''', (first,))
        until = (c.execute('SELECT clock_timestamp() AS t').fetchone()['t']+timedelta(days=30)).isoformat()
    s.snooze_reply(key, chat, first, 1, until)
    add('500', 2, 5, **group)
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET sent_at=sent_at-interval '20 days' WHERE chat=%s", (chat,))
        c.execute("UPDATE reply_snoozes SET snoozed_until=CURRENT_TIMESTAMP-interval '1 minute' WHERE chat=%s", (chat,))
        c.execute('ANALYZE messages')
    with s.authorized(key, 'list_chats') as (c, p):
        measured = QueueMeasured(c)
        page = list_chats(s, measured, p, awaiting_reply=True, include_coverage=False, view=view)
    assert page['items'] == []
    # Only the scans that carry the age window: the unbounded latest-incoming lookup
    # may legitimately walk the chat under a shared database's statistics.
    def windowed(node):
        conds = ' '.join(str(node.get(k) or '') for k in ('Index Cond', 'Recheck Cond', 'Filter'))
        own = visited({**node, 'Plans': []}, {'messages'}) if "'14 days'" in conds else 0
        return own+sum(windowed(child) for child in node.get('Plans', []))
    # The due snooze moves the bound back to its own hint, never over the older history.
    assert windowed(measured.plans[0]['Plan']) < 100, scans(measured.plans[0]['Plan'])


# spec: ANYM-281
def test_pending_group_row_counts_while_the_key_holds_a_snooze(inbox):
    s, ar, key, add, queue = inbox
    snoozed = add('100', 1, 1)
    with s.pool.connection() as c:
        until = (c.execute('SELECT clock_timestamp() AS t').fetchone()['t']+timedelta(days=1)).isoformat()
    s.snooze_reply(key, chat_of('100'), snoozed, 1, until)
    add('500', 1, 2, metadata={'chat_type': 'group', 'mentions_owner': True})
    rows = queue(view='conversation')['items']
    assert [(r['id'], r['awaiting_reply_incoming_count']) for r in rows] == [(chat_of('500'), 1)]


# spec: ANYM-281
def test_muted_rows_stay_counted_under_row_filters(inbox):
    s, ar, key, add, queue = inbox
    plain = add('300', 1, 1, sender='43', body='Synthetic plain note')
    question = add('300', 2, 5, body='Synthetic question?')
    chat = chat_of('300')
    s.mute_reply_source(key, account='controls-test', sender_id='42')
    assert hint(queue()['items'][0]) == (plain, 1) and queue()['muted_total'] == 0
    # Without the mute the question row meets only_questions; with it the row is removed and counted.
    filtered = queue(only_questions=True)
    assert filtered['items'] == [] and filtered['muted_total'] == 1
    shown = queue(only_questions=True, include_muted=True)['items']
    assert [(r['id'], r['awaiting_reply_muted'], hint(r)) for r in shown] == [(chat, 'sender', (question, 1))]
    # A dismissed skipping hint leaves the muted sender's row counted and visible.
    s.dismiss_reply(key, chat, plain, 1)
    assert queue()['items'] == [] and queue()['muted_total'] == 1
    assert [hint(r) for r in queue(include_muted=True)['items']] == [(question, 1)]


# spec: ANYM-281
def test_list_reply_dismissals_names_senders(inbox):
    s, ar, key, add, queue = inbox
    profile = {'sender_profile': {'display_name': 'Synthetic Sender'}}
    mid = add('100', 1, 1, **profile)
    add('200', 1, 2, sender='77', **profile)
    s.dismiss_reply(key, chat_of('100'), mid, 1, reason='other')
    s.mute_reply_source(key, account='controls-test', sender_id='77')
    assert s.list_reply_dismissals(key)['items'][0]['sender_name'] == 'Synthetic Sender'
    assert s.list_reply_dismissals(key, kind='muted')['items'][0]['sender_name'] == 'Synthetic Sender'


# spec: ANYM-281
def test_reply_controls_serialize_with_key_changes(inbox):
    from threading import Event
    from messaging_service.reply_state import mute
    from test_access import wait_for_acl_waiter
    s, ar, key, add, queue = inbox
    mid = add('100', 1, 1)
    chat = chat_of('100')
    credential = s.capabilities(key)['credential_id']
    s.dismiss_reply(key, chat, mid, 1, reason='other', note='Synthetic')
    with s.pool.connection() as c:
        until = (c.execute('SELECT clock_timestamp() AS t').fetchone()['t']+timedelta(days=1)).isoformat()
    # extend-key keeps the key's entries.
    s.extend_key(credential, 30)
    assert [i['reason'] for i in s.list_reply_dismissals(key)['items']] == ['other']
    # An admitted mutation finishes before a permission change; the next call rechecks.
    started = Event()
    def narrow():started.set();return s.set_access('controls-agent', permissions=['read'])
    with ThreadPoolExecutor(1) as threads:
        with s.authorized(key, 'mute_reply_source') as (c, p):
            changing = threads.submit(narrow);assert started.wait(1)
            wait_for_acl_waiter(s);assert not changing.done()
            assert mute(s, c, p, chat, None, None, None, None)['created'] is True
        changing.result(timeout=3)
    with pytest.raises(APIError, match='forbidden'):
        s.snooze_reply(key, chat, mid, 1, until)
    s.set_access('controls-agent', permissions=['read', 'cursor-write'])
    # An admitted mutation finishes before a revocation; the next call fails.
    with ThreadPoolExecutor(1) as threads:
        with s.authorized(key, 'unmute_reply_source') as (c, p):
            revoking = threads.submit(s.revoke, credential_id=credential)
            wait_for_acl_waiter(s);assert not revoking.done()
        assert revoking.result(timeout=3) == 1
    with pytest.raises(APIError, match='unauthorized'):
        s.restore_reply(key, chat, mid, 1)
    assert s.list_reply_dismissals(s.issue_key('controls-agent', ['controls-test']), kind='muted')['items']


# spec: ANYM-281
def test_reply_controls_backup_round_trip(inbox, tmp_path):
    """Real PG18 pg_dump and pg_restore of populated reply controls into a separate disposable database."""
    import os
    import subprocess
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from messaging_service.backup import create_snapshot, quarantine_restore, verify_bundle, verify_snapshot
    from messaging_service.store import Store
    container = os.environ.get('ANYMSG_TEST_POSTGRES_CONTAINER')
    if not container:
        if os.environ.get('CI'):
            pytest.fail('CI must provide the isolated PostgreSQL container for the restore drill')
        pytest.skip('isolated PostgreSQL container unavailable locally')
    s, ar, key, add, queue = inbox
    mids = [add('100', 1, 1), add('200', 1, 2, sender='77')]
    with s.pool.connection() as c:
        until = (c.execute('SELECT clock_timestamp() AS t').fetchone()['t']+timedelta(days=1)).isoformat()
    s.dismiss_reply(key, chat_of('100'), mids[0], 1, reason='spam_or_mailing', note='Synthetic note')
    s.snooze_reply(key, chat_of('200'), mids[1], 1, until, note='Synthetic later')
    s.mute_reply_source(key, account='controls-test', sender_id='77', reason='other')
    def controls(store):
        with store.pool.connection() as c:
            return [c.execute(f'SELECT * FROM {table} ORDER BY 1,2').fetchall()
                for table in ('reply_dismissals', 'reply_snoozes', 'reply_mutes')]
    values = conninfo_to_dict(s.pool.conninfo)
    assert values['dbname'] == 'messaging_probe_test'
    container_dsn = make_conninfo(host='127.0.0.1', port=5432, user=values['user'], dbname='messaging_probe_test')
    docker = ['docker', 'exec', '-i', '-e', 'PGPASSWORD=' + values['password'], container]
    create_snapshot(s, tmp_path / 'capture', [*docker, 'pg_dump', '--format=custom', '--dbname=' + container_dsn])
    manifest = verify_bundle(tmp_path / 'capture')
    assert manifest['schema_version'] == 49
    assert [manifest['tables'][t]['rows'] for t in ('reply_dismissals', 'reply_snoozes', 'reply_mutes')] == [1, 1, 1]
    name = 'anymsg_reply_' + uuid4().hex + '_restore_test'
    restored = None
    created = False
    try:
        with psycopg.connect(s.pool.conninfo, autocommit=True) as owner:
            owner.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        created = True
        subprocess.run([*docker, 'pg_restore', '--dbname=' + make_conninfo(container_dsn, dbname=name),
            '--exit-on-error', '--no-owner', '--no-acl'],
            input=(tmp_path / 'capture/database.dump').read_bytes(), check=True, capture_output=True)
        restored = Store(make_conninfo(s.pool.conninfo, dbname=name), tmp_path / 'capture/files', 'http://testserver')
        assert verify_snapshot(restored, tmp_path / 'capture')['status'] == 'verified'
        source = controls(s)
        assert controls(restored) == source and [len(rows) for rows in source] == [1, 1, 1]
        assert (source[0][0]['reason'], source[0][0]['note']) == ('spam_or_mailing', 'Synthetic note')
        # Quarantine revokes keys and disables accounts but keeps the controls.
        quarantine_restore(restored)
        assert controls(restored) == source
    finally:
        if restored is not None:
            restored.close()
        if created:
            with psycopg.connect(s.pool.conninfo, autocommit=True) as owner:
                owner.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
