"""ANYM-907: each chat's negotiation stored with versions never reused; a purge hides it at once and the worker's
cleanup removes what it fed. Real test database, synthetic chats only."""
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from messaging_service import negotiation_store as store_module
from messaging_service.negotiation_store import Checkpoint, apply, cleanup, negotiation_lock_key, take_rebuild, visible
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
    assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '76'


# spec: ANYM-907
def test_the_migration_applies_on_the_deployed_schema_and_gives_the_purge_role_nothing(at_schema):
    old = at_schema(75)
    with old.pool.connection() as c:
        assert c.execute("SELECT to_regclass('public.negotiations') AS t").fetchone()['t'] is None
    old.migrate()
    old.migrate()  # a repeated migrate changes nothing
    with old.pool.connection() as c:
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '76'
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
def test_a_purge_of_a_source_that_is_not_the_trigger_blanks_the_transition_it_triggered(neg):
    s, c = neg
    chat, a, b, negotiation = agreed_pair(c)
    # The owner's acceptance was published as an event before the purge of the proposal.
    c.execute('UPDATE negotiation_transitions SET published_at=now(),event_id=77 WHERE negotiation=%s AND version=2',
              (negotiation,))
    purge_row(c, a)
    cleanup(c, NOW)
    published = rows(c, 'negotiation_transitions', negotiation=negotiation, version=2)[0]
    assert published['trigger_message'] is None and published['state'] is None and published['event_id'] == 77
    nothing_derived(c, negotiation, a)


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
    assert c.execute(store_module.PURGED_SQL, (10,)).fetchall() == []
    assert c.execute("SELECT count(*) AS n FROM negotiations WHERE state<>'removed'").fetchone()['n'] == 0


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
