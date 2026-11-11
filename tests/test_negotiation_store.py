"""ANYM-907: each chat's negotiation stored with versions never reused; a purge hides it at once and the worker's
cleanup removes what it fed. Real test database, synthetic chats only."""
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

from messaging_service import negotiation_store as store_module
from messaging_service.negotiation_store import (Checkpoint, apply, clean_if_purged, cleanup, negotiation_lock_key,
                                                 take_rebuild, visible)
from test_archive import archive as archive, event  # noqa: F401  (fixture)
from test_db_role_isolation import install, remove
from test_message_purge import delete, purge as purge  # noqa: F401  (fixture)
from test_telegram_group_receipts_migration import at_schema as at_schema  # noqa: F401  (fixture)

TABLES = store_module.TABLES
HAS = '''SELECT CASE WHEN EXISTS (SELECT FROM pg_roles WHERE rolname=%(r)s) THEN has_table_privilege(%(r)s,%(t)s,%(p)s)
    ELSE false END AS ok'''


def granted_on(c, role, table, privilege):
    """A role's table privilege; a cluster without the role (a local one without the role files) has none."""
    return c.execute(HAS, {'r': role, 't': table, 'p': privilege}).fetchone()['ok']
ACCOUNT = 'telegram-demo'
NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture()
def neg(seeded):
    s = seeded[0]
    with s.pool.connection() as c:
        c.execute('TRUNCATE ' + ','.join(TABLES))
        c.execute('INSERT INTO negotiation_cursor(id,event_id,updated_at) VALUES(1,0,now())')
    with psycopg.connect(s.pool.conninfo, autocommit=True, row_factory=dict_row) as c:
        yield s, c


@pytest.fixture()
def worker(neg):
    """A LOGIN role that is only a member of messaging_negotiations, with fixture-only SELECT on the source tables (the
    worker's own grants on them come with ANYM-908)."""
    s, c = neg
    role, password = 'anym907_worker_' + uuid4().hex[:10], uuid4().hex
    c.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} IN ROLE messaging_negotiations').format(
        sql.Identifier(role), sql.Literal(password)))
    c.execute(sql.SQL('GRANT SELECT ON messages,chats,message_metadata TO {}').format(sql.Identifier(role)))
    try:
        dsn = make_conninfo(s.pool.conninfo, user=role, password=password)
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as w:
            yield s, c, w, role
    finally:
        c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
        c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


@pytest.fixture()
def scrub(neg):
    """A synthetic public.scrub_negotiation_events(text) (the real one comes with ANYM-911): it blanks the synthetic
    events recorded on the negotiation's transitions."""
    s, c = neg
    c.execute('CREATE TABLE tmp_negotiation_events(id bigint PRIMARY KEY, payload text)')
    c.execute('''CREATE FUNCTION public.scrub_negotiation_events(negotiation text) RETURNS void LANGUAGE sql AS $$
        UPDATE tmp_negotiation_events SET payload=NULL WHERE id IN (SELECT t.event_id FROM negotiation_transitions t
        WHERE t.negotiation=scrub_negotiation_events.negotiation AND t.event_id IS NOT NULL) $$''')
    try:
        yield s, c
    finally:
        c.execute('DROP FUNCTION public.scrub_negotiation_events(text)')
        c.execute('DROP TABLE tmp_negotiation_events')


def published(c, negotiation, version, event_id):
    """What the publisher (ANYM-908) records: one event, and the transition marked published with its id."""
    c.execute("INSERT INTO tmp_negotiation_events(id,payload) VALUES(%s,'state agreed, awaiting none')", (event_id,))
    c.execute('UPDATE negotiation_transitions SET published_at=now(),event_id=%s WHERE negotiation=%s AND version=%s',
              (event_id, negotiation, version))


def payload(c, event_id):
    return c.execute('SELECT payload FROM tmp_negotiation_events WHERE id=%s', (event_id,)).fetchone()['payload']


def new_chat(c, chat_type='private'):
    chat = 'neg-' + uuid4().hex[:12]
    c.execute('INSERT INTO chats(id,account,provider_chat,title,chat_type) VALUES(%s,%s,%s,%s,%s)',
              (chat, ACCOUNT, str(uuid4().int % 10**9), 'Synthetic negotiation', chat_type))
    return chat


def say(c, chat, body, minutes_ago, outgoing=False, ident=None):
    ident = ident or chat + '-' + uuid4().hex[:8]
    c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''', (ident, ACCOUNT, chat, ident, 'owner' if outgoing else 'fixture-anna', body,
        NOW - timedelta(minutes=minutes_ago), 'outgoing' if outgoing else 'incoming'))
    return ident


def purge_row(c, message):
    """What the purge scrub (message_purge.py) leaves on the message row: no body, purged."""
    c.execute("UPDATE messages SET body='',purged=true WHERE id=%s", (message,))


def rows(c, table, **where):
    clause = ' AND '.join(f'{k}=%s' for k in where) or 'true'
    order = {'negotiation_transitions': 'negotiation,version'}.get(table, '1')
    return c.execute(f'SELECT * FROM {table} WHERE {clause} ORDER BY {order}', tuple(where.values())).fetchall()


def versions(c, chat):
    return [(t['version'], t['kind']) for n in rows(c, 'negotiations', chat=chat)
            for t in rows(c, 'negotiation_transitions', negotiation=n['id'])]


# spec: ANYM-907
def test_the_migration_creates_the_role_and_grants_only_on_its_own_tables(neg):
    s, c = neg
    assert c.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname='messaging_negotiations'").fetchone() == {
        'rolcanlogin': False}
    for table in TABLES:
        assert all(granted_on(c, 'messaging_negotiations', table, p) for p in ('SELECT', 'INSERT', 'UPDATE', 'DELETE'))
        assert not any(granted_on(c, 'messaging_purge', table, p) for p in ('SELECT', 'INSERT', 'UPDATE', 'DELETE'))
    # The API role (gateway_role.sql "Schema 76") reads the three tables agents see and writes none.
    gateway = install(s, 'gateway_role.sql', 'messaging_gateway')
    try:
        for table in ('negotiations', 'negotiation_sources', 'negotiation_times'):
            assert granted_on(c, gateway, table, 'SELECT') and not granted_on(c, gateway, table, 'INSERT')
        for table in ('negotiation_transitions', 'negotiation_rebuild', 'negotiation_cursor'):
            assert not granted_on(c, gateway, table, 'SELECT')
    finally:
        remove(s, gateway)
    # The worker's grants on existing tables come with ANYM-908.
    assert not granted_on(c, 'messaging_negotiations', 'messages', 'SELECT')
    assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '93'


# spec: ANYM-907
def test_the_migration_applies_on_the_deployed_schema_and_gives_the_purge_role_nothing(at_schema):
    old = at_schema(75)
    with old.pool.connection() as c:
        assert c.execute("SELECT to_regclass('public.negotiations') AS t").fetchone()['t'] is None
    old.migrate()
    old.migrate()  # a repeated migrate changes nothing
    with old.pool.connection() as c:
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '93'
        assert store_module.schema_ready(c) is True
        for table in TABLES:
            assert not granted_on(c, 'messaging_purge', table, 'SELECT')


# spec: ANYM-907
def test_versions_strictly_increase_through_edit_delete_a_late_source_and_a_crash_replay(neg, monkeypatch):
    s, c = neg
    chat = new_chat(c)
    a = say(c, chat, 'давай созвонимся завтра в 10', 60)
    assert apply(c, ACCOUNT, chat, NOW) == 1
    b = say(c, chat, 'ок', 50, outgoing=True)
    assert apply(c, ACCOUNT, chat, NOW) == 1 and rows(c, 'negotiations', chat=chat)[0]['state'] == 'agreed'
    c.execute("UPDATE messages SET body='давай созвонимся завтра в 11',revision=2 WHERE id=%s", (a,))

    def crash(stage):
        if stage == 'written':raise RuntimeError('injected crash before commit')
    monkeypatch.setattr(store_module, 'hook', crash)
    with pytest.raises(RuntimeError):apply(c, ACCOUNT, chat, NOW)
    assert versions(c, chat) == [(1, 'opened'), (2, 'agreed')]  # the crash left nothing
    monkeypatch.setattr(store_module, 'hook', None)
    assert apply(c, ACCOUNT, chat, NOW) == 1  # the replay
    c.execute('UPDATE messages SET deleted=true WHERE id=%s', (b,))
    assert apply(c, ACCOUNT, chat, NOW) == 1
    say(c, chat, 'давай созвонимся в пятницу в 12', 70)  # a late source, older than the proposal
    assert apply(c, ACCOUNT, chat, NOW) == 1
    assert apply(c, ACCOUNT, chat, NOW) == 0  # nothing changed: no new row
    got = versions(c, chat)
    assert [v for v, _ in got] == [1, 2, 3, 4, 5] and got[0][1] == 'opened' and got[1][1] == 'agreed'
    (n,) = rows(c, 'negotiations', chat=chat)
    assert n['version'] == 5 and n['state'] == 'proposed'
    # The deleted acceptance keeps only ids.
    (row,) = rows(c, 'negotiation_sources', message=b)
    assert row['removed_at'] is not None and row['intent'] is None and row['negotiation'] == n['id']


# spec: ANYM-907
def test_removing_every_source_leaves_one_removed_tombstone_and_a_new_proposal_opens_a_new_id(neg):
    s, c = neg
    chat = new_chat(c)
    a = say(c, chat, 'давай созвонимся завтра в 10', 60)
    apply(c, ACCOUNT, chat, NOW)
    c.execute('UPDATE messages SET deleted=true WHERE id=%s', (a,))
    assert apply(c, ACCOUNT, chat, NOW) == 1
    (n,) = rows(c, 'negotiations', chat=chat)
    assert n['state'] == 'removed' and n['version'] == 2
    derived = ('medium', 'confidence', 'awaiting', 'rescheduled', 'ambiguous', 'agreed_start', 'agreed_end', 'agreed_zone',
               'previous_agreed_start', 'previous_agreed_end', 'previous_agreed_zone', 'fingerprint', 'last_source_at')
    assert all(n[k] is None for k in derived) and rows(c, 'negotiation_times', negotiation=n['id']) == []
    assert versions(c, chat) == [(1, 'opened'), (2, 'removed')]
    say(c, chat, 'давай созвонимся в пятницу в 12', 10)
    assert apply(c, ACCOUNT, chat, NOW) == 1
    fresh = [r for r in rows(c, 'negotiations', chat=chat) if r['state'] != 'removed']
    assert len(fresh) == 1 and fresh[0]['id'] != n['id'] and fresh[0]['version'] == 1
    assert visible(c, ACCOUNT, chat) == fresh


# spec: ANYM-907
def test_editing_the_only_proposal_to_plain_text_removes_its_negotiation_and_a_replay_changes_nothing(neg):
    s, c = neg
    chat = new_chat(c)
    a = say(c, chat, 'давай созвонимся завтра в 10', 60)
    apply(c, ACCOUNT, chat, NOW)
    c.execute("UPDATE messages SET body='привет как дела',revision=2 WHERE id=%s", (a,))
    assert apply(c, ACCOUNT, chat, NOW) == 1
    assert apply(c, ACCOUNT, chat, NOW) == 0  # the replay
    (n,) = rows(c, 'negotiations', chat=chat)
    assert n['state'] == 'removed' and versions(c, chat) == [(1, 'opened'), (2, 'removed')]
    (link,) = rows(c, 'negotiation_sources', message=a)
    assert link['removed_at'] is not None and link['intent'] is None and link['negotiation'] == n['id']
    assert visible(c, ACCOUNT, chat) == [] and rows(c, 'negotiation_times', negotiation=n['id']) == []


# spec: ANYM-907
def test_an_acceptance_left_after_its_proposal_is_edited_to_plain_text_keeps_no_agreement(neg):
    s, c = neg
    chat, a, b, negotiation = agreed_pair(c)
    c.execute("UPDATE messages SET body='привет как дела',revision=2 WHERE id=%s", (a,))
    assert apply(c, ACCOUNT, chat, NOW) == 1
    assert apply(c, ACCOUNT, chat, NOW) == 0  # the replay
    (n,) = rows(c, 'negotiations', chat=chat)
    assert n['state'] == 'removed' and n['agreed_start'] is None
    assert versions(c, chat) == [(1, 'opened'), (2, 'agreed'), (3, 'removed')]
    links = rows(c, 'negotiation_sources', negotiation=negotiation)
    assert {r['message'] for r in links} == {a, b}
    assert all(r['removed_at'] is not None and r['intent'] is None for r in links)
    assert visible(c, ACCOUNT, chat) == [] and rows(c, 'negotiation_times', negotiation=negotiation) == []


# spec: ANYM-907
def test_a_source_the_current_negotiation_takes_keeps_its_old_link_so_a_purge_cleans_both(neg):
    s, c = neg
    chat = new_chat(c)
    a = say(c, chat, 'давай созвонимся завтра в 10', 60)
    cancel = say(c, chat, 'давай отменим созвон', 50)
    apply(c, ACCOUNT, chat, NOW)
    (first,) = rows(c, 'negotiations', chat=chat)
    assert first['state'] == 'cancelled'
    say(c, chat, 'давай созвонимся в пятницу в 12', 40)
    apply(c, ACCOUNT, chat, NOW)
    (second,) = [n for n in rows(c, 'negotiations', chat=chat) if n['id'] != first['id']]
    c.execute("UPDATE messages SET body='привет',revision=2 WHERE id=%s", (cancel,))
    apply(c, ACCOUNT, chat, NOW)  # A and B now fold into one negotiation: the newer one takes A
    links = {r['negotiation']: r for r in rows(c, 'negotiation_sources', message=a)}
    assert set(links) == {first['id'], second['id']}
    assert links[first['id']]['removed_at'] is not None and links[second['id']]['removed_at'] is None
    assert rows(c, 'negotiations', id=first['id'])[0]['state'] == 'removed'
    assert [n['id'] for n in visible(c, ACCOUNT, chat)] == [second['id']]
    purge_row(c, a)
    assert visible(c, ACCOUNT, chat) == []
    assert cleanup(c, NOW) == 2
    for negotiation in (first['id'], second['id']):nothing_derived(c, negotiation, a)


# spec: ANYM-907
def test_a_stalled_evaluate_rolls_back_and_leaves_no_row_and_no_lock(neg, monkeypatch):
    s, c = neg
    chat = new_chat(c)
    say(c, chat, 'давай созвонимся завтра в 10', 60)
    monkeypatch.setattr(store_module, 'TIMEOUT', '400ms')
    monkeypatch.setattr(store_module, 'hook', lambda stage: time.sleep(1.5) if stage == 'evaluated' else None)
    with psycopg.connect(s.pool.conninfo, autocommit=True, row_factory=dict_row) as stalled:
        with pytest.raises(psycopg.Error):apply(stalled, ACCOUNT, chat, NOW)
    assert rows(c, 'negotiations', chat=chat) == []
    assert c.execute('SELECT pg_try_advisory_lock(%s) AS ok', (negotiation_lock_key(ACCOUNT, chat),)).fetchone()['ok']
    c.execute('SELECT pg_advisory_unlock(%s)', (negotiation_lock_key(ACCOUNT, chat),))


def agreed_pair(c):
    """Anna proposes, the owner accepts: one negotiation, two transitions (the second triggered by the owner)."""
    chat = new_chat(c)
    a = say(c, chat, 'давай созвонимся завтра в 10', 60)
    apply(c, ACCOUNT, chat, NOW)
    b = say(c, chat, 'ок', 50, outgoing=True)
    apply(c, ACCOUNT, chat, NOW)
    return chat, a, b, rows(c, 'negotiations', chat=chat)[0]['id']


def nothing_derived(c, negotiation, message):
    (n,) = rows(c, 'negotiations', id=negotiation)
    assert n['state'] == 'removed' and n['fingerprint'] is None and n['awaiting'] is None
    assert rows(c, 'negotiation_sources', negotiation=negotiation) == [] == rows(c, 'negotiation_times', negotiation=negotiation)
    assert rows(c, 'negotiation_sources', message=message) == []
    history = rows(c, 'negotiation_transitions', negotiation=negotiation)
    assert all(t['trigger_message'] is None and t['awaiting'] is None for t in history)
    assert all(t['state'] is None for t in history[:-1]) and history[-1]['kind'] == 'removed'
    # Nothing earlier can publish: every unpublished transition but the tombstone's is invalidated.
    assert all(t['invalidated_at'] is not None for t in history[:-1] if t['published_at'] is None)


# spec: ANYM-907
def test_a_partial_source_purge_is_hidden_at_once_and_the_next_pass_removes_everything_it_fed(neg):
    s, c = neg
    chat, a, b, negotiation = agreed_pair(c)
    purge_row(c, a)
    assert visible(c, ACCOUNT, chat) == []  # hidden from the moment the purge commits
    assert cleanup(c, NOW) == 1
    nothing_derived(c, negotiation, a)
    assert take_rebuild(c) == [(ACCOUNT, chat)]
    apply(c, ACCOUNT, chat, NOW)  # the rebuild: the owner's lone "ок" opens nothing
    assert visible(c, ACCOUNT, chat) == []


# spec: ANYM-907
def test_a_purge_after_a_refolded_delete_still_removes_the_removed_source(neg):
    s, c = neg
    chat, a, b, negotiation = agreed_pair(c)
    c.execute('UPDATE messages SET deleted=true WHERE id=%s', (b,))
    apply(c, ACCOUNT, chat, NOW)
    assert rows(c, 'negotiation_sources', message=b)[0]['removed_at'] is not None
    purge_row(c, b)
    assert cleanup(c, NOW) == 1
    nothing_derived(c, negotiation, b)


# spec: ANYM-907
def test_a_purge_of_a_source_that_is_not_the_trigger_blanks_the_transition_it_triggered(scrub):
    s, c = scrub
    chat, a, b, negotiation = agreed_pair(c)
    published(c, negotiation, 2, 77)  # the owner's acceptance, published before the purge of the proposal
    purge_row(c, a)
    cleanup(c, NOW)
    transition = rows(c, 'negotiation_transitions', negotiation=negotiation, version=2)[0]
    assert transition['trigger_message'] is None and transition['state'] is None and transition['event_id'] == 77
    assert payload(c, 77) is None  # its event is scrubbed
    nothing_derived(c, negotiation, a)


# spec: ANYM-907
def test_an_event_published_between_two_checkpoints_is_blanked_by_the_next_one_under_the_worker_role(worker, scrub):
    s, c, w, role = worker
    c.execute(sql.SQL('GRANT SELECT,UPDATE ON tmp_negotiation_events TO {}').format(sql.Identifier(role)))
    clock = [100.0]
    checkpoint = Checkpoint(clock=lambda: clock[0])
    chat, a, b, negotiation = agreed_pair(c)
    assert checkpoint(w, NOW) == 0
    published(c, negotiation, 2, 78)  # after that cleanup, before the purge
    purge_row(c, a)
    clock[0] += 1.1
    assert checkpoint(w, NOW) == 1
    assert payload(c, 78) is None
    transition = rows(c, 'negotiation_transitions', negotiation=negotiation, version=2)[0]
    assert transition['trigger_message'] is None and transition['state'] is None and transition['event_id'] == 78
    nothing_derived(c, negotiation, a)


# spec: ANYM-907
def test_the_worker_role_alone_applies_reads_and_cleans_and_cannot_write_a_message(worker):
    s, c, w, role = worker
    assert store_module.schema_ready(w) is True
    chat = new_chat(c)
    a = say(c, chat, 'давай созвонимся завтра в 10', 60)
    assert apply(w, ACCOUNT, chat, NOW) == 1
    say(c, chat, 'ок', 50, outgoing=True)
    assert apply(w, ACCOUNT, chat, NOW) == 1 and visible(w, ACCOUNT, chat)[0]['state'] == 'agreed'
    (n,) = rows(c, 'negotiations', chat=chat)
    purge_row(c, a)
    assert visible(w, ACCOUNT, chat) == []
    assert Checkpoint()(w, NOW) == 1
    nothing_derived(c, n['id'], a)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        w.execute("UPDATE messages SET body='' WHERE id=%s", (a,))


# spec: ANYM-907
def test_with_the_worker_stopped_reads_hide_at_once_and_its_first_pass_cleans_before_any_apply(neg):
    s, c = neg
    chat, a, b, negotiation = agreed_pair(c)
    purge_row(c, a)
    assert visible(c, ACCOUNT, chat) == []
    checkpoint = Checkpoint()
    assert checkpoint(c, NOW) == 1  # the first call of a started worker always runs
    nothing_derived(c, negotiation, a)


# spec: ANYM-907
def test_a_failed_first_checkpoint_keeps_the_full_startup_lap_for_its_retry(neg, monkeypatch):
    s, c = neg
    pairs = [agreed_pair(c) for _ in range(4)]  # eight source rows, more than one checkpoint's SWEEP below
    monkeypatch.setattr(store_module, 'SWEEP', 3)
    last = c.execute('SELECT message FROM negotiation_sources ORDER BY message DESC LIMIT 1').fetchone()['message']
    (target,) = [p for p in pairs if last in p[1:3]]
    purge_row(c, last)
    original, calls = store_module.cleanup, []

    def fails_once(*args, **kwargs):
        calls.append(kwargs.get('budget'))
        if len(calls) == 1:raise psycopg.OperationalError('injected: the database went away')
        return original(*args, **kwargs)
    monkeypatch.setattr(store_module, 'cleanup', fails_once)
    clock = [100.0]
    checkpoint = Checkpoint(clock=lambda: clock[0])
    with pytest.raises(psycopg.OperationalError):checkpoint(c, NOW)
    clock[0] += 0.1  # the retry is not throttled: the failed call did not count
    assert checkpoint(c, NOW) == 1 and calls == [None, None]
    nothing_derived(c, target[3], last)


# spec: ANYM-907
def test_the_checkpoint_runs_at_most_once_a_second_and_cleans_a_purge_committed_in_between(neg):
    s, c = neg
    clock = [100.0]
    checkpoint = Checkpoint(clock=lambda: clock[0])
    assert checkpoint(c, NOW) == 0
    chat, a, b, negotiation = agreed_pair(c)
    purge_row(c, a)  # committed between a cleanup and the next publish or apply
    clock[0] += 0.5
    assert checkpoint(c, NOW) == 0  # not due yet: at most once a second
    clock[0] += 0.6
    assert checkpoint(c, NOW) == 1
    nothing_derived(c, negotiation, a)


# spec: ANYM-907
def test_two_hundred_and_one_affected_negotiations_are_all_cleaned_in_one_cleanup(neg):
    s, c = neg
    messages = []
    for _ in range(201):
        chat = new_chat(c)
        messages.append(say(c, chat, 'давай созвонимся завтра в 10', 60))
        apply(c, ACCOUNT, chat, NOW)
    c.execute("UPDATE messages SET body='',purged=true WHERE id=ANY(%s)", (messages,))
    assert cleanup(c, NOW) == 201
    assert c.execute('''SELECT count(*) AS n FROM negotiation_sources s JOIN messages m ON m.id=s.message
        WHERE m.purged''').fetchone()['n'] == 0
    assert c.execute("SELECT count(*) AS n FROM negotiations WHERE state<>'removed'").fetchone()['n'] == 0


# spec: ANYM-907
def test_discovery_reads_a_bounded_slice_from_a_durable_cursor_and_a_lap_finds_a_sparse_purge(neg):
    s, c = neg
    pairs = [agreed_pair(c) for _ in range(5)]  # ten source rows
    keys = [(r['message'], r['negotiation']) for r in c.execute(
        'SELECT message, negotiation FROM negotiation_sources ORDER BY message, negotiation').fetchall()]
    last = keys[-1][0]
    (target,) = [p for p in pairs if last in p[1:3]]
    purge_row(c, last)

    def cursor():
        row = c.execute('SELECT purge_message, purge_negotiation FROM negotiation_cursor WHERE id=1').fetchone()
        return row['purge_message'], row['purge_negotiation']
    assert cleanup(c, NOW, batch=2, budget=4) == 0 and cursor() == keys[3]
    assert cleanup(c, NOW, batch=2, budget=4) == 0 and cursor() == keys[7]  # a new call goes on from the cursor
    assert cleanup(c, NOW, batch=2, budget=4) == 1
    nothing_derived(c, target[3], last)


# spec: ANYM-907
def test_apply_and_the_publish_guard_clean_their_own_purged_negotiation_whatever_the_cursor(neg):
    s, c = neg
    chat, a, b, negotiation = agreed_pair(c)
    other_chat, x, y, other = agreed_pair(c)
    purge_row(c, a)
    purge_row(c, x)
    with c.transaction():
        assert clean_if_purged(c, other, NOW) is True  # nothing of it may publish
        assert clean_if_purged(c, other, NOW) is False
    nothing_derived(c, other, x)
    assert apply(c, ACCOUNT, chat, NOW) == 1  # cleans the chat's purged negotiation before it reads
    nothing_derived(c, negotiation, a)


# spec: ANYM-907
def test_a_restart_between_cleanup_batches_resumes_and_finishes(neg, monkeypatch):
    s, c = neg
    pairs = [agreed_pair(c) for _ in range(3)]
    for chat, a, b, negotiation in pairs:purge_row(c, a)
    original, calls = store_module._purge_one, []

    def dies_on_the_second(*args):
        calls.append(1)
        if len(calls) == 2:raise RuntimeError('worker stopped')
        return original(*args)
    monkeypatch.setattr(store_module, '_purge_one', dies_on_the_second)
    with pytest.raises(RuntimeError):cleanup(c, NOW, batch=1)
    monkeypatch.setattr(store_module, '_purge_one', original)
    assert cleanup(c, NOW, batch=1) == 2
    for chat, a, b, negotiation in pairs:nothing_derived(c, negotiation, a)


# spec: ANYM-907
def test_a_real_purge_batch_with_a_negotiation_present_succeeds_and_the_cleanup_removes_it(purge):
    ar, s, key, purger = purge
    with s.pool.connection() as c:c.execute('TRUNCATE ' + ','.join(TABLES))
    mid = ar.ingest(event(body='давай созвонимся завтра в 10'))['message']  # sent 2026-10-01T12:00Z, as the purge tests
    at = datetime(2026, 10, 1, 12, 30, tzinfo=timezone.utc)
    with psycopg.connect(s.pool.conninfo, autocommit=True, row_factory=dict_row) as c:
        row = c.execute('SELECT account, chat FROM messages WHERE id=%s', (mid,)).fetchone()
        assert apply(c, row['account'], row['chat'], at) == 1
        delete(ar)
        result = purger.run_once()  # the purge job as today, under its own role
        assert result['purged'] == 1 and result['failed'] == 0
        assert visible(c, row['account'], row['chat']) == []
        assert cleanup(c, at) == 1
        assert rows(c, 'negotiation_sources', message=mid) == []
