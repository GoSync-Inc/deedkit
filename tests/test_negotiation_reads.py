"""ANYM-909: list_negotiations, every negotiation being arranged now in one read. Real test database, synthetic
negotiations (ANYM-907 rows written as the worker would) in synthetic chats only."""
import json
import socket
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import jsonschema
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

from messaging_service import negotiation_reads
from messaging_service.negotiation_reads import _window, read_ready
from messaging_service.policy import OPERATION_PERMISSIONS
from messaging_service.store import APIError
from test_negotiation_store import ACCOUNT, NOW, neg as neg, purge_row  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
CATALOG = {t['name']: t for t in json.loads((ROOT / 'services/gateway/src/agent_gateway/catalog.json').read_text())}
PUBLIC = {'get_context': 'messages_get_context', 'calendar_find_time': 'calendar_find_time'}
OWNER = '6f1c2a3e-4b5d-4e6f-8a7b-9c0d1e2f3a4b'
ANNA = '0d9e8f7a-6b5c-4d3e-9f2a-1b0c9d8e7f6a'
COMPACT = {'id', 'version', 'account', 'chat', 'state', 'phase', 'medium', 'confidence', 'awaiting', 'participants',
           'current_times', 'agreed_time', 'previous_agreed_time', 'rescheduled', 'ambiguous', 'phrase', 'opened_at',
           'updated_at', 'next'}


def chat(c, chat_type='private', account=ACCOUNT, title='Synthetic negotiation'):
    ident = 'chat_' + uuid4().hex
    c.execute('INSERT INTO chats(id,account,provider_chat,title,chat_type) VALUES(%s,%s,%s,%s,%s)',
              (ident, account, str(uuid4().int % 10**9), title, chat_type))
    return ident


def say(c, chat_id, body='давай созвонимся завтра в 15:00', minutes_ago=30, sender='fixture-anna', outgoing=False,
        account=ACCOUNT):
    ident = 'msg_' + uuid4().hex
    c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''', (ident, account, chat_id, ident, 'owner' if outgoing else sender, body,
        NOW - timedelta(minutes=minutes_ago), 'outgoing' if outgoing else 'incoming'))
    return ident


def at(days=0, hours=15):
    return (NOW + timedelta(days=days)).replace(hour=hours, minute=0, second=0)


def make(c, chat_id, *, state='proposed', awaiting='owner', confidence='high', medium='call', times=None, sources=None,
         updated=60, account=ACCOUNT, agreed=None, body='давай созвонимся завтра в 15:00'):
    """One negotiation as ANYM-907's apply writes it: the row, its live sources and its times."""
    sources = sources or [say(c, chat_id, body, updated + 1, account=account)]
    if times is None:times = [('current', at(1), at(1, 16), 'time')]
    ident = 'neg_' + uuid4().hex
    start, end = agreed or (None, None)
    c.execute('''INSERT INTO negotiations(id,account,chat,state,medium,confidence,version,awaiting,rescheduled,ambiguous,
        agreed_start,agreed_end,agreed_zone,fingerprint,opened_at,updated_at,last_source_at)
        VALUES(%s,%s,%s,%s,%s,%s,1,%s,false,false,%s,%s,%s,%s,%s,%s,%s)''',
        (ident, account, chat_id, state, medium, confidence, awaiting, start, end, 'UTC' if start else None, '0' * 64,
         NOW - timedelta(minutes=updated + 5), NOW - timedelta(minutes=updated), NOW - timedelta(minutes=updated)))
    for message in sources:
        c.execute('''INSERT INTO negotiation_sources(message,negotiation,revision,intent,confidence,medium,cue_start,cue_end)
            VALUES(%s,%s,1,'propose','high',%s,0,8)''', (message, ident, medium))
    for ord_, (status, s, e, granularity) in enumerate(times):
        c.execute('''INSERT INTO negotiation_times(negotiation,ord,message,status,starts_at,ends_at,granularity,zone,
            zone_source) VALUES(%s,%s,%s,%s,%s,%s,%s,'Europe/Berlin','owner')''',
            (ident, ord_, sources[0], status, s, e, granularity))
    return ident


def link(c, person, sender, account=ACCOUNT):
    c.execute('INSERT INTO person_links(person,account,sender) VALUES(%s,%s,%s)', (person, account, sender))


def ids(result):
    return [i['id'] for i in result['items']]


def refused(call):
    with pytest.raises(APIError) as caught:
        call()
    return caught.value


@pytest.fixture()
def reads(neg, seeded):
    s, c = neg
    with s.pool.connection() as w:
        w.execute('TRUNCATE person_links')
    return s, c, seeded[1], seeded[2]


# spec: ANYM-909
def test_each_filter_alone_and_combined_with_the_default_sets(reads):
    s, c, one, _ = reads
    chats = [chat(c) for _ in range(7)]
    group = chat(c, 'group')
    a = make(c, chats[0], awaiting='owner')
    b = make(c, chats[1], awaiting='peer', confidence='medium', times=[('current', at(3), at(3, 16), 'time')])
    low = make(c, chats[2], confidence='low')
    upcoming = make(c, chats[3], state='agreed', awaiting='none', times=[('agreed', at(2), at(2, 16), 'time')],
                    agreed=(at(2), at(2, 16)))
    past = make(c, chats[4], state='agreed', awaiting='none', times=[('agreed', at(-2), at(-2, 16), 'time')],
                agreed=(at(-2), at(-2, 16)))
    cancelled = make(c, chats[5], state='cancelled', awaiting='none')
    stale = make(c, chats[6], state='stale', awaiting='none', updated=5)
    grouped = make(c, group, awaiting='others')
    # The defaults: open and upcoming, private chats, medium confidence or more; an agreed one whose end passed is out.
    assert set(ids(s.list_negotiations(one))) == {a, b, upcoming}
    for states, expected in [(['past'], {past}), (['cancelled'], {cancelled}), (['stale'], {stale}),
                             (['open'], {a, b}), (['upcoming'], {upcoming})]:
        assert set(ids(s.list_negotiations(one, states=states))) == expected, states
    assert set(ids(s.list_negotiations(one, chat_types=['group']))) == {grouped}
    assert set(ids(s.list_negotiations(one, chat_ids=[chats[0], chats[2]]))) == {a}
    assert ids(s.list_negotiations(one, accounts=['whatsapp-demo-1'])) == []
    assert set(ids(s.list_negotiations(one, accounts=[ACCOUNT]))) == {a, b, upcoming}
    assert set(ids(s.list_negotiations(one, min_confidence='low'))) == {a, b, low, upcoming}
    assert set(ids(s.list_negotiations(one, min_confidence='high'))) == {a, upcoming}
    # A time window overlaps current or agreed times only.
    window = {'from_time': at(3).isoformat(), 'to_time': at(3, 17).isoformat()}
    assert ids(s.list_negotiations(one, **window)) == [b]
    assert ids(s.list_negotiations(one, from_time=at(2, 15).isoformat(), to_time=at(2, 15).replace(minute=30).isoformat())) == [
        upcoming]
    assert set(ids(s.list_negotiations(one, updated_since=(NOW - timedelta(minutes=10)).isoformat(),
                                       states=list(negotiation_reads.STATES)))) == {stale}
    assert set(ids(s.list_negotiations(one, negotiation_ids=[a, past]))) == {a}
    assert ids(s.list_negotiations(one, negotiation_ids=[a, past], states=['past', 'open'], chat_ids=[chats[4]])) == [past]
    # Combined: open in two chats with high confidence.
    assert ids(s.list_negotiations(one, states=['open'], chat_ids=chats[:2], min_confidence='high')) == [a]
    assert set(ids(s.list_negotiations(one, states=['open', 'past'], chat_types=['private', 'group'],
                                       min_confidence='low'))) == {a, b, low, past, grouped}
    for bad in [{'states': ['agreed']}, {'chat_types': ['dm']}, {'order': 'newest'}, {'detail': 'all'},
                {'min_confidence': 'certain'}, {'limit': 51}, {'negotiation_ids': [f'neg_{i:032x}' for i in range(51)]},
                {'from_time': 'tomorrow'}]:
        assert refused(lambda: s.list_negotiations(one, **bad)).code in ('invalid_filter', 'invalid_limit'), bad


# spec: ANYM-909
def test_each_order(reads):
    s, c, one, _ = reads
    peer = make(c, chat(c), awaiting='peer', updated=1, times=[('current', at(3), at(3, 16), 'time')])
    older = make(c, chat(c), awaiting='owner', updated=50, times=[('current', at(1), at(1, 16), 'time')])
    newer = make(c, chat(c), awaiting='owner', updated=10, times=[])
    assert ids(s.list_negotiations(one)) == [newer, older, peer]
    assert ids(s.list_negotiations(one, order='awaiting_owner_first')) == [newer, older, peer]
    assert ids(s.list_negotiations(one, order='updated')) == [peer, newer, older]
    # Soonest current or agreed time first; one with no time last.
    assert ids(s.list_negotiations(one, order='soonest')) == [older, peer, newer]


def pages(s, key, **arguments):
    seen, result = [], s.list_negotiations(key, **arguments)
    seen.append(result)
    while result['cursor']:
        result = s.list_negotiations(key, **arguments, cursor=result['cursor'])
        seen.append(result)
    return seen


# spec: ANYM-909
@pytest.mark.parametrize('order', ['awaiting_owner_first', 'updated', 'soonest'])
def test_the_cursor_over_three_pages_loses_and_repeats_nothing_while_a_negotiation_changes(reads, order):
    s, c, one, _ = reads
    made = [make(c, chat(c), updated=10 + i, times=[('current', at(1 + i), at(1 + i, 16), 'time')]) for i in range(8)]
    first = s.list_negotiations(one, order=order, limit=3)
    assert len(first['items']) == 3 and first['cursor']
    shown = ids(first)
    # One not shown yet and one already shown change between pages, as the worker's next version would.
    later = next(i for i in made if i not in shown)
    c.execute('UPDATE negotiations SET version=version+1,updated_at=clock_timestamp() WHERE id=ANY(%s)',
              ([later, shown[0]],))
    rest = [first]
    result = first
    while result['cursor']:
        result = s.list_negotiations(one, order=order, limit=3, cursor=result['cursor'])
        rest.append(result)
    assert len(rest) == 3
    paged = [i for r in rest for i in ids(r)]
    assert len(paged) == len(set(paged))
    # The last page names the changed ones and the read that returns them; together nothing is lost.
    changed = rest[-1]['changed']
    assert changed['count'] == 2 and changed['next']['tool'] == 'list_negotiations'
    again = s.list_negotiations(one, **changed['next']['arguments'])
    assert set(ids(again)) == {later, shown[0]}
    assert set(paged) | set(ids(again)) == set(made)
    # Unchanged, the pages alone hold every negotiation once and name nothing changed.
    unchanged = pages(s, one, order=order, limit=3)
    assert sorted(i for r in unchanged for i in ids(r)) == sorted(made) and 'changed' not in unchanged[-1]
    # A cursor is bound to its read: other filters refuse it.
    assert refused(lambda: s.list_negotiations(one, order=order, limit=3, states=['past'],
                                               cursor=first['cursor'])).code == 'invalid_cursor'


# spec: ANYM-909
def test_person_exact_ambiguous_and_unknown(reads):
    s, c, one, _ = reads
    anna_chat, other_chat = chat(c, title='Anna Synthetic'), chat(c, title='Anna Fixture')
    group = chat(c, 'group', title='Synthetic team')
    mine = make(c, anna_chat, sources=[say(c, anna_chat, sender='fixture-anna-2')])
    theirs = make(c, other_chat, sources=[say(c, other_chat, sender='fixture-boris')])
    grouped = make(c, group, sources=[say(c, group, sender='fixture-anna-2')])
    link(c, ANNA, 'fixture-anna-2')
    assert set(ids(s.list_negotiations(one, person=ANNA, chat_types=['private', 'group']))) == {mine, grouped}
    assert ids(s.list_negotiations(one, person=ANNA.upper())) == [mine]
    # A chat ID is a resolve_target input too: that chat's negotiations.
    assert ids(s.list_negotiations(one, person=other_chat)) == [theirs]
    ambiguous = refused(lambda: s.list_negotiations(one, person='Anna'))
    assert ambiguous.code == 'selection_required' and {x['id'] for x in ambiguous.candidates} >= {anna_chat, other_chat}
    for unknown in ['Nobody Synthetic', '11111111-2222-4333-8444-555555555555']:
        assert refused(lambda: s.list_negotiations(one, person=unknown)).code == 'not_found'


# spec: ANYM-909
def test_compact_against_full(reads):
    s, c, one, _ = reads
    room = chat(c)
    ask = say(c, room, 'давай созвонимся завтра в 15:00', 40)
    counter = say(c, room, 'лучше послезавтра в 11', 30, outgoing=True)
    n = make(c, room, sources=[ask, counter], times=[('current', at(2, 11), at(2, 12), 'time'),
                                                       ('countered', at(1), at(1, 16), 'time'),
                                                       ('declined_only', at(1, 9), at(1, 10), 'time')])
    compact = s.list_negotiations(one)['items'][0]
    assert compact['id'] == n and set(compact) == COMPACT
    assert compact['chat'] == {'id': room, 'title': 'Synthetic negotiation', 'type': 'private'}
    assert [(p['sender_id'], p['is_owner']) for p in compact['participants']] == [('fixture-anna', False), ('owner', True)]
    assert all(set(p) == {'account', 'sender_id', 'person_id', 'name', 'is_owner'} for p in compact['participants'])
    assert [t['start'] for t in compact['current_times']] == [at(2, 11).isoformat()]
    assert 'послезавтра в 11' in compact['phrase']
    full = s.list_negotiations(one, detail='full')['items'][0]
    assert set(full) == COMPACT | {'countered_times', 'declined_times', 'sources'}
    assert [(t['start'], t['proposed_by']) for t in full['countered_times']] == [
        (at(1).isoformat(), {'sender_id': 'fixture-anna', 'is_owner': False})]
    assert [t['start'] for t in full['declined_times']] == [at(1, 9).isoformat()]
    assert [(x['message_id'], x['direction'], x['intent'], x['deleted']) for x in full['sources']] == [
        (ask, 'incoming', 'propose', False), (counter, 'outgoing', 'propose', False)]
    assert set(full['sources'][0]) == {'message_id', 'revision', 'sent_at', 'direction', 'sender_id', 'sender_name',
                                       'intent', 'phrase', 'deleted'}


# spec: ANYM-909
def test_isolation_two_principals_a_forbidden_chat_and_another_accounts_ids(reads):
    s, c, one, two = reads
    shared, hidden = chat(c), chat(c)
    other = chat(c, account='whatsapp-demo-1')
    a, b = make(c, shared), make(c, hidden)
    w = make(c, other, account='whatsapp-demo-1')
    assert set(ids(s.list_negotiations(one))) == {a, b, w}
    # agent-b reads telegram-demo only.
    assert set(ids(s.list_negotiations(two))) == {a, b}
    assert refused(lambda: s.list_negotiations(two, negotiation_ids=[w])).code == 'not_found_or_forbidden'
    assert refused(lambda: s.list_negotiations(two, negotiation_ids=[a, w])).code == 'not_found_or_forbidden'
    # A key scoped to one chat of an allowed account never sees the other chat's negotiation, by any filter.
    scoped = s.issue_key('agent-c', [ACCOUNT], chats=[shared])
    assert ids(s.list_negotiations(scoped)) == [a]
    assert ids(s.list_negotiations(scoped, chat_ids=[hidden])) == []
    assert refused(lambda: s.list_negotiations(scoped, negotiation_ids=[b])).code == 'not_found_or_forbidden'
    assert refused(lambda: s.list_negotiations(scoped, negotiation_ids=['neg_' + '0' * 32])).code == 'not_found_or_forbidden'
    # Every read is audited under its operation, with the IDs it returned.
    rows = c.execute('''SELECT outcome,resource_ids FROM audit_log WHERE operation='list_negotiations'
        AND principal='agent-c' ORDER BY occurred_at''').fetchall()
    assert [(r['outcome'], r['resource_ids']) for r in rows] == [('ok', [a]), ('ok', []), ('denied', []), ('denied', [])]


# spec: ANYM-909
def test_a_deleted_source_gives_no_phrase_and_a_purged_one_hides_the_negotiation_at_once(reads):
    s, c, one, _ = reads
    room, gone = chat(c), chat(c)
    first = say(c, room, 'давай созвонимся завтра в 15:00', 40)
    latest = say(c, room, 'x' * 200 + ' или завтра в 17:30? ' + 'y' * 200, 30)
    n = make(c, room, sources=[first, latest])
    phrase = s.list_negotiations(one)['items'][0]['phrase']
    assert len(phrase) <= 120 and 'завтра в 17:30' in phrase
    c.execute('UPDATE messages SET deleted=true WHERE id=%s', (latest,))
    item = s.list_negotiations(one, detail='full')['items'][0]
    assert item['id'] == n and item['phrase'] is None
    assert [(x['message_id'], x['deleted'], x['phrase'] is None) for x in item['sources']] == [
        (first, False, False), (latest, True, True)]
    # A purged source hides its negotiation from every read, before the worker's cleanup reaches it.
    p = make(c, gone)
    source = c.execute('SELECT message FROM negotiation_sources WHERE negotiation=%s', (p,)).fetchone()['message']
    assert p in ids(s.list_negotiations(one))
    purge_row(c, source)
    assert p not in ids(s.list_negotiations(one, states=list(negotiation_reads.STATES), chat_types=['private']))
    assert refused(lambda: s.list_negotiations(one, negotiation_ids=[p])).code == 'not_found_or_forbidden'
    assert c.execute('SELECT state FROM negotiations WHERE id=%s', (p,)).fetchone()['state'] == 'proposed'
    # A removed tombstone is never shown either.
    c.execute("UPDATE negotiations SET state='removed',medium=NULL,confidence=NULL,awaiting=NULL,rescheduled=NULL,"
              "ambiguous=NULL,fingerprint=NULL,last_source_at=NULL WHERE id=%s", (n,))
    assert ids(s.list_negotiations(one, states=list(negotiation_reads.STATES))) == []


def validate(entry):
    assert set(entry) == {'tool', 'arguments'}, entry
    jsonschema.validate(entry['arguments'], CATALOG[PUBLIC[entry['tool']]]['inputSchema'])


# spec: ANYM-909
def test_calendar_next_validates_as_given_and_passes_only_the_owner(reads):
    s, c, one, _ = reads
    room, group, quiet = chat(c), chat(c, 'group'), chat(c)
    ask = say(c, room, 'давай созвонимся завтра в 15:00', 40)
    yes = say(c, room, 'ок', 30, outgoing=True)
    n = make(c, room, sources=[ask, yes], medium='call')
    link(c, OWNER, 'owner')
    link(c, ANNA, 'fixture-anna')
    item = s.list_negotiations(one, chat_ids=[room])['items'][0]
    assert item['id'] == n and [x['tool'] for x in item['next']] == ['get_context', 'calendar_find_time']
    for entry in item['next']:validate(entry)
    assert item['next'][0]['arguments'] == {'message_id': yes}
    assert item['next'][1]['arguments'] == {'people': [OWNER], 'duration': 30, 'window': {
        'from': (at(1) - timedelta(minutes=30)).isoformat(), 'to': (at(1, 16) + timedelta(minutes=30)).isoformat()}}
    assert {p['sender_id']: p['person_id'] for p in item['participants']} == {'fixture-anna': ANNA, 'owner': OWNER}
    # A group with 40 identified participants still passes only the owner (the catalog allows 32 people).
    senders = [f'fixture-member-{i}' for i in range(40)]
    sources = [say(c, group, f'давай встретимся завтра в {10 + i % 8}:00', 60 - i, sender=x) for i, x in enumerate(senders)]
    for x in senders:link(c, str(uuid4()), x)
    g = make(c, group, sources=sources, medium='meeting')
    item = s.list_negotiations(one, chat_types=['group'])['items'][0]
    assert item['id'] == g and len(item['participants']) == 41
    finds = [x for x in item['next'] if x['tool'] == 'calendar_find_time']
    assert finds and all(x['arguments']['people'] == [OWNER] and x['arguments']['duration'] == 60 for x in finds)
    for entry in item['next']:validate(entry)
    # No current or agreed time: no calendar entry.
    bare = make(c, quiet, times=[])
    assert [x['tool'] for x in s.list_negotiations(one, negotiation_ids=[bare])['items'][0]['next']] == ['get_context']
    # The owner without a person UUID: no calendar entry either.
    c.execute("DELETE FROM person_links WHERE sender='owner'")
    assert [x['tool'] for x in s.list_negotiations(one, chat_ids=[room])['items'][0]['next']] == ['get_context']
    # Never a planning or writing call and never a field to fill.
    for item in s.list_negotiations(one, chat_types=['private', 'group'], detail='full')['items']:
        for entry in item['next']:
            assert entry['tool'] in PUBLIC and 'fill' not in entry and 'fill' not in entry['arguments']


# spec: ANYM-909
@pytest.mark.parametrize('granularity,start,end,window', [
    ('time', '2026-11-10T15:00:00+01:00', '2026-11-10T16:00:00+01:00',
     ('2026-11-10T14:30:00+01:00', '2026-11-10T16:30:00+01:00')),
    ('range', '2026-11-10T15:00:00+01:00', '2026-11-10T17:00:00+01:00',
     ('2026-11-10T14:30:00+01:00', '2026-11-10T17:30:00+01:00')),
    ('part_of_day', '2026-11-10T12:00:00+01:00', '2026-11-10T17:00:00+01:00',
     ('2026-11-10T11:30:00+01:00', '2026-11-10T17:30:00+01:00')),
    ('day', '2026-11-10T00:00:00+01:00', '2026-11-11T00:00:00+01:00',
     ('2026-11-10T08:00:00+01:00', '2026-11-10T20:00:00+01:00')),
    ('week', '2026-11-12T00:00:00+01:00', '2026-11-16T00:00:00+01:00',
     ('2026-11-09T00:00:00+01:00', '2026-11-16T00:00:00+01:00')),
], ids=['time', 'range', 'part_of_day', 'day', 'week'])
def test_the_find_time_window_rule_per_granularity(granularity, start, end, window):
    t = {'start': start, 'end': end, 'granularity': granularity, 'zone': 'Europe/Berlin'}
    found = _window(t)
    assert (found['from'], found['to']) == window
    jsonschema.validate({'people': [OWNER], 'window': found, 'duration': 60},
                        CATALOG['calendar_find_time']['inputSchema'])


# spec: ANYM-909
def test_the_read_makes_no_http_request(reads, monkeypatch):
    s, c, one, _ = reads
    room = chat(c)
    make(c, room, sources=[say(c, room), say(c, room, 'ок', 20, outgoing=True)])
    link(c, OWNER, 'owner')
    made = []

    def refuse(*args, **kwargs):
        made.append(args)
        raise AssertionError('no network call from list_negotiations')
    # The database driver is libpq (C), so a Python socket connect or any HTTP client is the read's own.
    monkeypatch.setattr(socket.socket, 'connect', refuse)
    monkeypatch.setattr(socket, 'create_connection', refuse)
    import httpx
    import urllib.request
    monkeypatch.setattr(httpx.Client, 'send', refuse)
    monkeypatch.setattr(httpx.AsyncClient, 'send', refuse)
    monkeypatch.setattr(urllib.request, 'urlopen', refuse)
    item = s.list_negotiations(one, detail='full')['items'][0]
    assert [x['tool'] for x in item['next']] == ['get_context', 'calendar_find_time'] and made == []


# spec: ANYM-909
def test_registration_audit_and_capabilities(reads):
    s, c, one, _ = reads
    assert OPERATION_PERMISSIONS['list_negotiations'] == 'read'
    assert 'list_negotiations' in s.capabilities(one)['available']
    entry = CATALOG['messages_list_negotiations']
    assert (entry['module'], entry['operation'], entry['scopes'], entry['annotations']['readOnlyHint']) == (
        'messaging', 'list_negotiations', ['read'], True)
    s.list_negotiations(one)
    assert c.execute("SELECT count(*) AS n FROM audit_log WHERE operation='list_negotiations'").fetchone()['n'] == 1
    # A key without read is refused, and the refusal audited.
    writer = s.issue_key('agent-d', [ACCOUNT], permissions=['feedback-write'])
    assert refused(lambda: s.list_negotiations(writer)).code == 'forbidden'


# spec: ANYM-909
def test_not_ready_while_a_table_is_absent_or_not_readable(reads, monkeypatch):
    s, c, one, _ = reads
    monkeypatch.setattr(negotiation_reads, 'READ_TABLES', (*negotiation_reads.READ_TABLES, 'negotiation_absent'))
    error = refused(lambda: s.list_negotiations(one))
    assert (error.code, error.status) == ('not_ready', 503)
    monkeypatch.undo()
    role, password = 'anym909_reader_' + uuid4().hex[:10], uuid4().hex
    c.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role), sql.Literal(password)))
    try:
        with psycopg.connect(make_conninfo(s.pool.conninfo, user=role, password=password), autocommit=True,
                             row_factory=dict_row) as r:
            assert read_ready(r) is False
            c.execute(sql.SQL('GRANT SELECT ON negotiations,negotiation_sources TO {}').format(sql.Identifier(role)))
            assert read_ready(r) is False
            c.execute(sql.SQL('GRANT SELECT ON negotiation_times TO {}').format(sql.Identifier(role)))
            assert read_ready(r) is True
    finally:
        c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
        c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
    with s.pool.connection() as w:
        assert read_ready(w) is True
