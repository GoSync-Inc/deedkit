"""ANYM-908: the negotiation worker, the one writer of negotiations, under a LOGIN that is a member of
messaging_negotiations only. Real test database, synthetic chats only; never a provider."""
import json
import logging
from pathlib import Path
import subprocess
import sys
import time
from datetime import timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest
import yaml

from messaging_service import heartbeats, negotiation_store as store_module, negotiation_worker as nw
from messaging_service.negotiation_store import Checkpoint, negotiation_lock_key, visible
from messaging_service.store import Store
from messaging_service.time_references import UTC_FALLBACK, owner_zone
from test_negotiation_store import ACCOUNT, NOW, neg as neg, new_chat, purge_row, rows, say  # noqa: F401  (fixture)
from test_telegram_group_receipts_migration import at_schema as at_schema  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'services/messaging/messaging_service'
ROLE_SQL = (SOURCE / 'negotiation_role.sql').read_text()
PROPOSAL, ACCEPT = 'давай созвонимся завтра в 10', 'ок'
# Every grant the role file gives on an existing object, one by one (criterion 1).
GRANTS = [('column', table, column) for table, columns in nw.COLUMNS.items() for column in columns] + [
    ('table', table, privilege) for table, privileges in nw.TABLE_GRANTS.items() for privilege in privileges]
SYNTHETIC_FUNCTIONS = {
    # Stand-ins for ANYM-911's functions: only their presence and EXECUTE grant matter here.
    'publish_negotiation_event': 'public.publish_negotiation_event(transition text)',
    'scrub_negotiation_events': 'public.scrub_negotiation_events(negotiation text)'}


@pytest.fixture()
def login(neg):
    """The role file installed on messaging_negotiations, and a fresh LOGIN that is only its member."""
    s, c = neg
    c.execute(ROLE_SQL)
    c.execute("DELETE FROM component_heartbeats WHERE component='negotiations'")
    c.execute('UPDATE negotiation_cursor SET event_id=(SELECT coalesce(max(id),0) FROM events)')
    role, password = 'anym908_' + uuid4().hex[:10], uuid4().hex
    c.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} IN ROLE messaging_negotiations').format(
        sql.Identifier(role), sql.Literal(password)))
    dsn = make_conninfo(s.pool.conninfo, user=role, password=password)
    made = []

    def make(**options):
        options.setdefault('sleep', lambda seconds: None)
        options.setdefault('now', lambda: NOW)
        worker = nw.Worker(dsn, options.pop('zone', UTC_FALLBACK), **options)
        made.append(worker)
        return worker
    try:
        yield s, c, make, dsn
    finally:
        for worker in made:worker.close()
        c.execute(ROLE_SQL)  # any grant a test revoked comes back
        c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
        c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


@pytest.fixture()
def functions(neg):
    """ANYM-911 installed: both functions present, EXECUTE only for messaging_negotiations."""
    s, c = neg
    for signature in SYNTHETIC_FUNCTIONS.values():
        c.execute(f'CREATE FUNCTION {signature} RETURNS void LANGUAGE plpgsql AS $$ BEGIN END $$')
        name = signature.split('(')[0]
        c.execute(f'REVOKE ALL ON FUNCTION {name}(text) FROM PUBLIC')
        c.execute(f'GRANT EXECUTE ON FUNCTION {name}(text) TO messaging_negotiations')
    try:
        yield c
    finally:
        for signature in SYNTHETIC_FUNCTIONS.values():c.execute(f"DROP FUNCTION {signature.split('(')[0]}(text)")


def journal(c, message, kind='message.created'):
    """What a collector commits with a message change: one journal event (store.append_event)."""
    return Store.append_event(c, ACCOUNT, message, kind)


def post(c, chat, body, minutes_ago, outgoing=False):
    message = say(c, chat, body, minutes_ago, outgoing=outgoing)
    journal(c, message)
    return message


def edit(c, message, body):
    c.execute('UPDATE messages SET body=%s,revision=revision+1 WHERE id=%s', (body, message))
    journal(c, message, 'message.edited')


def delete(c, message):
    c.execute('UPDATE messages SET deleted=true WHERE id=%s', (message,))
    journal(c, message, 'message.deleted')


def started(make, **options):
    worker = make(**options)
    assert worker.start() is None
    return worker


def cursor(c):
    row = c.execute('SELECT event_id FROM negotiation_cursor WHERE id=1').fetchone()
    return row and row['event_id']


def snapshot(c, chats):
    """What a read derives per chat: live negotiations, their times and live sources; ids and versions left out."""
    found = {}
    for chat in chats:
        items = []
        for n in rows(c, 'negotiations', chat=chat):
            if n['state'] == 'removed':continue
            times = [(t['message'], t['status'], t['starts_at'], t['ends_at'], t['zone'])
                     for t in rows(c, 'negotiation_times', negotiation=n['id'])]
            sources = sorted((r['message'], r['revision'], r['intent'], r['cue_start'])
                             for r in rows(c, 'negotiation_sources', negotiation=n['id']) if r['removed_at'] is None)
            items.append((n['state'], n['awaiting'], n['medium'], n['agreed_start'], n['fingerprint'], times, sources))
        found[chat] = sorted(items, key=repr)
    return found


def rebuilt(c, make, start):
    """The same chats from nothing: empty tables, the cursor back at `start`, one pass."""
    c.execute('TRUNCATE ' + ','.join(store_module.TABLES))
    c.execute('INSERT INTO negotiation_cursor(id,event_id,updated_at) VALUES(1,%s,now())', (start,))
    worker = started(make)
    while worker.busy(worker.run_pass()):pass
    worker.close()


def column_grants(c):
    found = set()
    for r in c.execute('''SELECT cl.relname, a.attname, x.privilege_type FROM pg_attribute a
            JOIN pg_class cl ON cl.oid=a.attrelid, aclexplode(a.attacl) x
            WHERE cl.relnamespace='public'::regnamespace AND x.grantee='messaging_negotiations'::regrole''').fetchall():
        found.add((r['relname'], r['attname'], r['privilege_type']))
    return found


def table_grants(c):
    return {(r['relname'], r['privilege_type']) for r in c.execute('''SELECT cl.relname, x.privilege_type
        FROM pg_class cl, aclexplode(cl.relacl) x WHERE cl.relnamespace='public'::regnamespace
        AND x.grantee='messaging_negotiations'::regrole''').fetchall()}


# spec: ANYM-908
def test_the_role_file_grants_exactly_the_listed_objects_and_the_worker_starts_under_them(login):
    s, c, make, dsn = login
    assert column_grants(c) == {(table, column, 'SELECT') for table, columns in nw.COLUMNS.items() for column in columns}
    assert table_grants(c) == {(table, p) for table in store_module.TABLES for p in store_module.PRIVILEGES} | {
        ('component_heartbeats', p) for p in nw.TABLE_GRANTS['component_heartbeats']}
    c.execute('DELETE FROM negotiation_cursor')
    worker = started(make)
    assert nw.fence(worker.c) is None and cursor(c) is not None


# spec: ANYM-908
@pytest.mark.parametrize('grant', GRANTS, ids=['.'.join(g[1:]) for g in GRANTS])
def test_with_any_one_grant_revoked_the_worker_exits_not_ready_and_writes_nothing(login, grant, capsys):
    s, c, make, dsn = login
    kind, table, item = grant
    revoke = (f'REVOKE SELECT({item}) ON {table} FROM messaging_negotiations' if kind == 'column'
              else f'REVOKE {item} ON {table} FROM messaging_negotiations')
    c.execute(revoke)
    c.execute('DELETE FROM negotiation_cursor')
    assert make().run() == 3
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out['status'] == 'not_ready' and table in out['reason']
    assert cursor(c) is None and rows(c, 'component_heartbeats', component='negotiations') == []


# spec: ANYM-908
def test_the_worker_is_not_ready_while_the_negotiation_tables_are_absent(at_schema):
    old = at_schema(75)
    with psycopg.connect(old.pool.conninfo, autocommit=True, row_factory=dict_row) as c:
        assert nw.fence(c) == 'negotiation_tables'


# spec: ANYM-908
def test_the_first_start_reads_only_the_journal_of_the_last_14_days(login):
    s, c, make, dsn = login
    old_chat, new = new_chat(c), new_chat(c)
    post(c, old_chat, PROPOSAL, 60)
    c.execute("UPDATE events SET created_at=now()-interval '20 days'")  # everything so far is older than 14 days
    post(c, new, PROPOSAL, 30)
    c.execute('DELETE FROM negotiation_cursor')
    worker = started(make)
    assert worker.run_pass()['events'] == 1
    assert rows(c, 'negotiations', chat=old_chat) == [] and len(visible(c, ACCOUNT, new)) == 1
    assert cursor(c) == c.execute('SELECT max(id) AS m FROM events').fetchone()['m']


# spec: ANYM-908
def test_a_native_reply_to_the_owner_puts_a_group_in_scope_and_a_silent_group_stays_out(login):
    s, c, make, dsn = login
    replied, silent = new_chat(c, 'group'), new_chat(c, 'group')
    owner = say(c, replied, 'привет всем', 60 * 24 * 20, outgoing=True)  # 20 days ago: no recent outgoing message
    asked = post(c, replied, PROPOSAL, 30)
    c.execute('INSERT INTO message_metadata(message,revision,metadata) VALUES(%s,1,%s)',
              (asked, json.dumps({'reply_to_provider_id': owner})))
    post(c, silent, PROPOSAL, 30)
    worker = started(make)
    assert nw.in_scope(worker.c, ACCOUNT, replied, [asked], NOW) is True
    worker.run_pass()
    assert len(visible(c, ACCOUNT, replied)) == 1 and rows(c, 'negotiations', chat=silent) == []


# spec: ANYM-908
def test_two_heartbeat_writes_in_a_row_succeed_and_no_other_component_row_can_be_written(login):
    s, c, make, dsn = login
    worker = started(make)
    assert heartbeats.beat(worker.c, 'negotiations') is True and heartbeats.beat(worker.c, 'negotiations') is True
    (row,) = rows(c, 'component_heartbeats', component='negotiations')
    assert row['version'] == heartbeats.identity()
    assert heartbeats.beat(worker.c, 'matcher') is False


# spec: ANYM-908
def test_the_worker_login_cannot_update_messages_or_write_outside_its_own_tables_and_heartbeat_row(login):
    s, c, make, dsn = login
    chat = new_chat(c)
    message = post(c, chat, PROPOSAL, 30)
    c.execute("""INSERT INTO component_heartbeats(component,version,schema_version,beat_at) VALUES('matcher','0.1',1,now())
        ON CONFLICT (component) DO NOTHING""")
    with nw.connect(dsn) as w:
        assert w.execute('SELECT body FROM messages WHERE id=%s', (message,)).fetchone()['body'] == PROPOSAL
        for statement, params in [("UPDATE messages SET body='' WHERE id=%s", (message,)),
                                  ('UPDATE chats SET title=%s WHERE id=%s', ('x', chat)),
                                  ('SELECT title FROM chats WHERE id=%s', (chat,)),
                                  ('INSERT INTO events(id,account,message,kind) VALUES(-1,%s,%s,%s)',
                                   (ACCOUNT, message, 'message.created')),
                                  ("DELETE FROM component_heartbeats WHERE component='matcher'", ()),
                                  ('UPDATE message_metadata SET metadata=%s', ('{}',))]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):w.execute(statement, params)
        # Row-level security: another component's row is invisible and untouchable.
        assert w.execute("UPDATE component_heartbeats SET beat_at=now() WHERE component='matcher'").rowcount == 0


# spec: ANYM-908
def test_incremental_passes_equal_a_full_rebuild(login):
    s, c, make, dsn = login
    start = cursor(c)
    private, group = new_chat(c), new_chat(c, 'group')
    worker = started(make)
    a = post(c, private, PROPOSAL, 60)
    worker.run_pass()
    post(c, private, ACCEPT, 50, outgoing=True)
    post(c, group, 'созвонимся в пятницу в 12?', 40, outgoing=True)
    worker.run_pass()
    edit(c, a, 'давай созвонимся завтра в 11')
    post(c, group, 'давай лучше в субботу в 13', 30)
    worker.run_pass()
    incremental = snapshot(c, [private, group])
    assert incremental[private] and incremental[group]
    worker.close()
    rebuilt(c, make, start)
    assert snapshot(c, [private, group]) == incremental


# spec: ANYM-908
def test_each_pass_runs_the_cleanup_before_every_apply_and_publish(login, functions, monkeypatch):
    s, c, make, dsn = login
    order = []
    cleanup, apply = store_module.cleanup, store_module.apply
    monkeypatch.setattr(store_module, 'cleanup', lambda *a, **k: order.append('cleanup') or cleanup(*a, **k))
    monkeypatch.setattr(store_module, 'apply', lambda *a, **k: order.append('apply') or apply(*a, **k))
    monkeypatch.setattr(nw, 'PUBLISH', lambda c, t: order.append('publish'))
    first, second = new_chat(c), new_chat(c)
    post(c, first, PROPOSAL, 60)
    post(c, second, PROPOSAL, 50)
    worker = started(make)
    worker.checkpoint = Checkpoint(interval=0)
    worker.last_sweep = time.monotonic()  # the 10-minute stale sweep is not due in this pass
    result = worker.run_pass()
    assert result['published'] == 2 and order.count('apply') == 2 and order[0] == 'cleanup'
    for at, step in enumerate(order):
        if step in ('apply', 'publish'):assert order[at - 1] == 'cleanup', order
    assert order.index('publish') > max(at for at, step in enumerate(order) if step == 'apply')


# spec: ANYM-908
def test_a_publish_cleans_a_purged_negotiation_first_and_publishes_nothing_of_it(login, functions, monkeypatch):
    s, c, make, dsn = login
    published = []
    monkeypatch.setattr(nw, 'PUBLISH', lambda c, t: published.append((t['negotiation'], t['version'])))
    chat = new_chat(c)
    a = post(c, chat, PROPOSAL, 60)
    worker = started(make)
    monkeypatch.setattr(worker, 'check', lambda: 0)  # no checkpoint reaches it first
    worker.journal()
    (n,) = rows(c, 'negotiations', chat=chat)
    purge_row(c, a)
    assert worker.publish() == 0 and published == []
    assert rows(c, 'negotiations', chat=chat)[0]['state'] == 'removed'


# spec: ANYM-908
def test_a_replay_after_an_injected_failure_gives_the_same_rows(login, monkeypatch):
    s, c, make, dsn = login
    start = cursor(c)
    chats = [new_chat(c), new_chat(c)]
    for chat in chats:
        post(c, chat, PROPOSAL, 60)
        post(c, chat, ACCEPT, 50, outgoing=True)
    failures = []

    def crash(stage):
        if stage == 'written' and not failures:
            failures.append(stage)
            raise RuntimeError('injected crash before commit')
    monkeypatch.setattr(store_module, 'hook', crash)
    waits = []
    worker = started(make, sleep=waits.append)
    worker.run_pass()
    assert failures and waits == [1] and worker.skipped == 0
    replayed = snapshot(c, chats)
    worker.close()
    monkeypatch.setattr(store_module, 'hook', None)
    rebuilt(c, make, start)
    assert snapshot(c, chats) == replayed and all(replayed[chat] for chat in chats)


# spec: ANYM-908
def test_a_timed_out_evaluation_leaves_the_cursor_unmoved_is_retried_and_then_skipped(login, monkeypatch, caplog):
    s, c, make, dsn = login
    start = cursor(c)
    stalled, fine = new_chat(c), new_chat(c)
    message = post(c, stalled, PROPOSAL, 60)
    post(c, fine, PROPOSAL, 50)
    monkeypatch.setattr(store_module, 'TIMEOUT', '300ms')
    calls = []

    def stall(stage):
        if stage == 'evaluated':
            calls.append(stage)
            if len(calls) <= 4:time.sleep(0.6)  # the first try and its three retries, all on the stalled chat
    monkeypatch.setattr(store_module, 'hook', stall)
    waits = []
    worker = started(make, sleep=lambda seconds: waits.append((seconds, cursor(c))))
    with caplog.at_level(logging.INFO, logger=nw.__name__):
        worker.run_pass()
    assert waits == [(1, start), (4, start), (16, start)]  # nothing committed while it was retried
    assert worker.skipped == 1 and rows(c, 'negotiations', chat=stalled) == [] and len(visible(c, ACCOUNT, fine)) == 1
    assert cursor(c) == c.execute('SELECT max(id) AS m FROM events').fetchone()['m']
    skipped = [r.getMessage() for r in caplog.records if 'negotiation_event_skipped' in r.getMessage()]
    assert skipped == [f'negotiation_event_skipped message={message} error=TransactionTimeout']
    assert not any(PROPOSAL in r.getMessage() for r in caplog.records)


# spec: ANYM-908
def test_a_stop_during_a_backoff_ends_the_pass_with_the_cursor_unmoved_and_nothing_skipped(login, monkeypatch):
    s, c, make, dsn = login
    start = cursor(c)
    post(c, new_chat(c), PROPOSAL, 60)
    monkeypatch.setattr(store_module, 'hook', lambda stage: (_ for _ in ()).throw(RuntimeError('injected')))
    worker = started(make)
    worker.sleep = lambda seconds: worker.stop.set()
    with pytest.raises(nw.Stopped):worker.run_pass()
    assert cursor(c) == start and worker.skipped == 0


# spec: ANYM-908
def test_an_edit_and_a_delete_refold_the_chat(login):
    s, c, make, dsn = login
    chat = new_chat(c)
    a = post(c, chat, PROPOSAL, 60)
    worker = started(make)
    worker.run_pass()
    (before,) = rows(c, 'negotiation_times', negotiation=visible(c, ACCOUNT, chat)[0]['id'])
    edit(c, a, 'давай созвонимся завтра в 11')
    worker.run_pass()
    (after,) = rows(c, 'negotiation_times', negotiation=visible(c, ACCOUNT, chat)[0]['id'])
    assert after['starts_at'] - before['starts_at'] == timedelta(hours=1)
    delete(c, a)
    worker.run_pass()
    assert visible(c, ACCOUNT, chat) == [] and rows(c, 'negotiations', chat=chat)[0]['state'] == 'removed'


# spec: ANYM-908
def test_a_delete_in_a_chat_now_out_of_scope_still_refolds_and_an_edit_there_does_not(login):
    s, c, make, dsn = login
    chat = new_chat(c)
    a = post(c, chat, PROPOSAL, 60)
    worker = started(make)
    worker.run_pass()
    (n,) = rows(c, 'negotiations', chat=chat)
    c.execute("UPDATE chats SET chat_type='channel' WHERE id=%s", (chat,))
    edit(c, a, 'давай созвонимся завтра в 11')
    worker.run_pass()
    assert rows(c, 'negotiations', chat=chat)[0]['version'] == n['version']  # out of scope: no refold
    delete(c, a)
    worker.run_pass()
    assert rows(c, 'negotiations', chat=chat)[0]['state'] == 'removed'


# spec: ANYM-908
def test_the_rebuild_queue_after_a_purge_refolds_the_chat_from_what_is_left(login):
    s, c, make, dsn = login
    chat = new_chat(c)
    a = post(c, chat, PROPOSAL, 60)
    worker = started(make)
    worker.run_pass()
    post(c, chat, 'давай лучше в пятницу в 12', 40, outgoing=True)
    worker.run_pass()
    (old,) = visible(c, ACCOUNT, chat)
    purge_row(c, a)
    worker.checkpoint = Checkpoint(interval=0)  # the next checkpoint is due (it runs at most once a second)
    worker.run_pass()
    assert rows(c, 'negotiation_rebuild') == [] and rows(c, 'negotiations', id=old['id'])[0]['state'] == 'removed'
    (new,) = visible(c, ACCOUNT, chat)
    assert new['id'] != old['id'] and a not in {r['message'] for r in rows(c, 'negotiation_sources', negotiation=new['id'])}


# spec: ANYM-908
def test_a_second_worker_waits_for_the_first(login):
    s, c, make, dsn = login
    first = started(make)
    waited = []
    second = make()
    second.sleep = lambda seconds: (waited.append(seconds), second.stop.set())
    assert second.start() == 'stopped' and waited == [nw.IDLE_S]
    first.close()
    second.stop.clear()
    assert second.start() is None


# spec: ANYM-908
def test_a_collector_edit_and_append_commit_without_waiting_while_the_worker_applies_that_chat(login, monkeypatch):
    s, c, make, dsn = login
    chat = new_chat(c)
    a = post(c, chat, PROPOSAL, 60)
    done = []

    def collector(stage):
        if stage != 'evaluated' or done:return
        with psycopg.connect(s.pool.conninfo, autocommit=True) as other:
            other.execute("SET lock_timeout='500ms'")
            other.execute('UPDATE messages SET body=%s,revision=revision+1 WHERE id=%s', ('давай созвонимся завтра в 12', a))
            say(other, chat, 'ещё сообщение', 1)
        done.append(stage)
    monkeypatch.setattr(store_module, 'hook', collector)
    worker = started(make)
    worker.run_pass()
    assert done == ['evaluated'] and worker.skipped == 0


SCOPES = [('private', 'peer', True), ('private', 'owner_only', False), ('group', 'owner_recent', True),
          ('group', 'mention', True), ('group', 'none', False), ('channel', 'owner_recent', False), (None, 'peer', False)]


# spec: ANYM-908
@pytest.mark.parametrize('chat_type,case,expected', SCOPES, ids=[f'{t}-{k}' for t, k, _ in SCOPES])
def test_scope_covers_each_branch(login, chat_type, case, expected):
    s, c, make, dsn = login
    chat = new_chat(c, chat_type)
    if case == 'owner_recent':say(c, chat, 'я тут', 60 * 24, outgoing=True)
    message = post(c, chat, PROPOSAL, 30, outgoing=case == 'owner_only')
    if case == 'mention':
        c.execute('INSERT INTO message_metadata(message,revision,metadata) VALUES(%s,1,%s)',
                  (message, json.dumps({'mentions_owner': True})))
    worker = started(make)
    assert nw.in_scope(worker.c, ACCOUNT, chat, [message], NOW) is expected
    worker.run_pass()
    assert bool(rows(c, 'negotiations', chat=chat)) is expected


# spec: ANYM-908
def test_before_anym_911_the_worker_starts_cleans_and_publishes_nothing(login, monkeypatch):
    s, c, make, dsn = login
    assert c.execute("SELECT to_regprocedure('public.scrub_negotiation_events(text)') AS f").fetchone()['f'] is None
    monkeypatch.setattr(nw, 'PUBLISH', lambda c, t: pytest.fail('no publisher is installed'))
    chat = new_chat(c)
    a = post(c, chat, PROPOSAL, 60)
    worker = started(make)
    worker.run_pass()
    purge_row(c, a)
    worker.checkpoint = Checkpoint(interval=0)
    result = worker.run_pass()
    assert result['cleaned'] == 1 and result['published'] == 0


# spec: ANYM-908
@pytest.mark.parametrize('function', sorted(SYNTHETIC_FUNCTIONS))
def test_after_anym_911_a_revoked_execute_on_either_function_exits_not_ready(login, functions, function, capsys):
    s, c, make, dsn = login
    worker = started(make)
    worker.close()
    c.execute(f'REVOKE EXECUTE ON FUNCTION public.{function}(text) FROM messaging_negotiations')
    assert make().run() == 3
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])['reason'] == f'{function}:EXECUTE'


# spec: ANYM-908
def test_the_fence_is_rechecked_every_10_minutes_and_a_revoked_grant_stops_writes_until_it_returns(login):
    s, c, make, dsn = login
    clock = [1000.0]
    worker = started(make, clock=lambda: clock[0])
    c.execute('REVOKE SELECT(body) ON messages FROM messaging_negotiations')
    assert worker.fenced() is False  # not due yet
    clock[0] += nw.PERIOD_S
    assert worker.fenced() is True and worker.fenced() is True  # rechecked at once while it is missing
    c.execute(ROLE_SQL)
    assert worker.fenced() is False


# spec: ANYM-908
def test_the_loop_writes_nothing_while_fenced(login):
    s, c, make, dsn = login
    clock = [1000.0]
    worker = started(make, clock=lambda: clock[0])
    worker.start = lambda: None  # already started
    chat = new_chat(c)
    post(c, chat, PROPOSAL, 60)
    c.execute('REVOKE SELECT(body) ON messages FROM messaging_negotiations')
    clock[0] += nw.PERIOD_S
    sleeps = []
    worker.sleep = lambda seconds: (sleeps.append(seconds), len(sleeps) >= 2 and worker.stop.set())
    assert worker.run() == 0
    assert sleeps == [nw.IDLE_S, nw.IDLE_S] and rows(c, 'negotiations', chat=chat) == []


# spec: ANYM-908
def test_the_start_logs_the_owner_zone_label_and_status_shows_it_with_the_heartbeat(login, caplog):
    s, c, make, dsn = login
    zone = (ZoneInfo('Europe/Moscow'), 'Europe/Moscow')
    with caplog.at_level(logging.INFO, logger=nw.__name__):
        worker = started(make, zone=zone)
    assert any('negotiation_worker_started owner_zone=Europe/Moscow' in r.getMessage() for r in caplog.records)
    assert owner_zone({})[1] == 'UTC_fallback'
    assert nw.status(dsn, {'ANYMSG_OWNER_TIMEZONE': 'Europe/Moscow'}) == {'owner_zone': 'Europe/Moscow', 'heartbeat': None}
    worker.beat()
    found = nw.status(dsn, {})
    assert found['owner_zone'] == 'UTC_fallback' and found['heartbeat']['version'] == heartbeats.identity()


# spec: ANYM-908
def test_a_failed_heartbeat_write_never_stops_the_worker(login, monkeypatch):
    s, c, make, dsn = login
    worker = started(make)
    c.execute('REVOKE INSERT ON component_heartbeats FROM messaging_negotiations')
    worker.beat()
    assert rows(c, 'component_heartbeats', component='negotiations') == []
    monkeypatch.setattr(heartbeats, 'beat', lambda *a: (_ for _ in ()).throw(RuntimeError('boom')))
    worker.last_beat = None
    worker.beat()
    chat = new_chat(c)
    post(c, chat, PROPOSAL, 60)
    worker.run_pass()
    assert len(visible(c, ACCOUNT, chat)) == 1


# spec: ANYM-908
def test_a_stalled_apply_holds_no_chat_lock_after_its_timeout(login, monkeypatch):
    s, c, make, dsn = login
    chat = new_chat(c)
    post(c, chat, PROPOSAL, 60)
    monkeypatch.setattr(store_module, 'TIMEOUT', '300ms')
    monkeypatch.setattr(store_module, 'hook', lambda stage: time.sleep(0.6) if stage == 'evaluated' else None)
    worker = started(make)
    worker.run_pass()
    key = negotiation_lock_key(ACCOUNT, chat)
    assert c.execute('SELECT pg_try_advisory_lock(%s) AS ok', (key,)).fetchone()['ok']
    c.execute('SELECT pg_advisory_unlock(%s)', (key,))


# spec: ANYM-908
def test_collectors_sends_and_hints_import_no_negotiation_code():
    probe = ('import sys\n'
             'for name in ("telegram_collector", "whatsapp_collector", "subscription_worker", "delivery_worker"):\n'
             '    __import__("messaging_service." + name)\n'
             'print(sorted(k for k in sys.modules if k.rsplit(".", 1)[-1] in '
             '("negotiation_worker", "scheduling_rules", "negotiation_state")))')
    result = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, timeout=60, check=True)
    assert result.stdout.strip() == '[]'


# spec: ANYM-908
def test_no_module_but_the_worker_itself_names_the_worker():
    users = {p.name for p in SOURCE.glob('*.py') if 'negotiation_worker' in p.read_text()}
    assert users == {'negotiation_worker.py'}


# spec: ANYM-908
def test_the_container_has_the_subscription_hardening_and_its_own_dsn():
    services = yaml.safe_load((ROOT / 'deploy/compose.subscriptions.yaml').read_text())['services']
    worker, reference = services['negotiations'], services['subscriptions']
    assert worker['command'] == ['python', '-m', 'messaging_service.negotiation_worker']
    for key in ('read_only', 'cap_drop', 'security_opt', 'tmpfs', 'pids_limit', 'user', 'networks', 'init', 'restart'):
        assert worker[key] == reference[key], key
    assert worker['read_only'] is True and worker['cap_drop'] == ['ALL'] and worker['pids_limit'] == 32
    assert worker['security_opt'] == ['no-new-privileges:true'] and worker['tmpfs'] == ['/tmp:size=16m,noexec,nosuid']
    assert worker['mem_limit'] == '192m'
    (volume,) = worker['volumes']
    assert volume['source'] == './private/negotiations.dsn' and volume['read_only'] is True
    assert volume['bind']['create_host_path'] is False
