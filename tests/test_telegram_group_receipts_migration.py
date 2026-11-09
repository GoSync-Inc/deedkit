"""ANYM-868 migration: the Telegram branch of the message_receipts CHECK also allows an aggregate group boundary.
A NOT VALID swap in one ALTER TABLE, run through the real `migrate-expand` batch on disposable databases with
synthetic receipts only."""
from contextlib import ExitStack
import hashlib
import re
import threading
import time

import psycopg
from psycopg.rows import dict_row
import pytest

from messaging_service import migration_rollout as rollout
from messaging_service import store as store_module
from messaging_service.receipts import REFERENCE_TG_SQL, ROW_PROOFS_SQL
from messaging_service.store import ExpandError, RUNTIME_SCHEMA
import test_migrate_expand
from test_extend_key import old_schema_store
from test_migrate_expand import SOURCE, plan_for

# A disposable database per test from a module template at the runtime schema (test_migrate_expand's fixtures).
template, db = test_migrate_expand.template, test_migrate_expand.db

MIGRATION = next(SOURCE.glob('migrations/[0-9][0-9][0-9]_telegram_group_receipts.sql'))
VERSION = int(MIGRATION.name[:3])
# The production schema the card names (N-1 is 70 or 71): a release from it applies 071 (outgoing_messages.menu_choice
# with its CHECK and a gateway grant), 072 (an attachments index) and this file in one migrate-expand batch.
DEPLOYED = 70
RECEIPTS = 100_000
LOCK_S, TRANSACTION_S = 2, 4  # store.py expand_connection: lock_timeout 2 s, statement and transaction 4 s
# The schema-(VERSION-1) runtime's receipt write (receipts.record) and boundary read, unchanged by this card.
OLD_INSERT = ('INSERT INTO message_receipts(id,account,platform,provider_chat,provider_id,max_id,state,basis,audience,'
              'observed_at,provider_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL) ON CONFLICT(id) DO NOTHING RETURNING id')


@pytest.fixture
def at_schema(database, tmp_path):
    """`at_schema(version)`: the DSN of a disposable database built with the migrations up to `version` only."""
    with ExitStack() as stack:
        yield lambda version: stack.enter_context(old_schema_store(database, tmp_path, version)).pool.conninfo


def appended_roles(base):
    """Each role file's blocks for schemas after `base` (`-- Schema N` headers): what a release from `base` appends."""
    found = {}
    for path in sorted(SOURCE.glob('*_role.sql')):
        text = path.read_text()
        later = [m.start() for m in re.finditer(r'^-- Schema (\d+)', text, re.M) if int(m.group(1)) > base]
        if later: found[path.name] = text[later[0]:]
    return found


def release_plan(base):
    return plan_for(SOURCE, base, VERSION, appended_roles(base))


def gateway_role(dsn):
    # As gateway_role.sql's own first statement: the role an appended grant names exists before the release.
    with psycopg.connect(dsn) as c:
        c.execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_gateway') THEN "
                  "CREATE ROLE messaging_gateway NOLOGIN; END IF; END $$")


def seed(dsn, rows=RECEIPTS):
    with psycopg.connect(dsn) as c:
        c.execute("INSERT INTO accounts(id,platform,label) VALUES ('synthetic','telegram','synthetic')")
        c.execute("INSERT INTO message_receipts(id,account,platform,provider_chat,provider_id,max_id,state,basis,"
                  "audience,observed_at) SELECT encode(sha256(('fixture-'||g)::bytea),'hex'),'synthetic','telegram',"
                  "(g%%500+1)::text,NULL,g,'read','telegram_outbox_boundary','private_peer',now() "
                  "FROM generate_series(1,%s) g", (rows,))


def constraints(dsn):
    """{name: validated} of message_receipts' table CHECKs (the column CHECKs of 023 excluded)."""
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        return {r['conname']: r['convalidated'] for r in c.execute(
            "SELECT conname,convalidated FROM pg_constraint WHERE conrelid='message_receipts'::regclass "
            "AND contype='c' AND conname IN ('message_receipts_check','message_receipts_source_check')")}


def version(dsn, base=None):
    """(schema_version, ledger rows above `base`, default VERSION-1)."""
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        return (int(c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']),
                c.execute('SELECT count(*) AS n FROM archive_migrations WHERE version>%s',
                          (VERSION - 1 if base is None else base,)).fetchone()['n'])


def menu_choice(dsn):
    with psycopg.connect(dsn) as c:
        return c.execute("SELECT count(*) FROM information_schema.columns WHERE table_name='outgoing_messages' "
                         "AND column_name='menu_choice'").fetchone()[0]


def receipt(account, chat, max_id, audience):
    return (hashlib.sha256(f'{account}/{chat}/{max_id}/{audience}'.encode()).hexdigest(), account, 'telegram', chat, None, max_id,
            'read', 'telegram_outbox_boundary', audience, '2026-11-09T08:00:00+00:00')


# spec: ANYM-868
def test_the_migration_is_an_unattended_non_critical_check_swap_that_backup_and_the_runtime_know():
    text = MIGRATION.read_text()
    assert rollout.unattended_class(text) == 'expand' and rollout.risk_class(text) == ('non-critical', None, None)
    parsed = rollout.statements(text)
    assert any(('word', 'valid') in s for s in parsed) and not any(('word', 'validate') in s for s in parsed)
    from messaging_service import backup, backup_r2_verifier
    assert VERSION == RUNTIME_SCHEMA and VERSION in backup.SCHEMAS and VERSION in backup_r2_verifier.SCHEMAS


# spec: ANYM-868
def test_the_widened_check_takes_group_aggregates_only_and_rewrites_no_row(at_schema):
    dsn = at_schema(VERSION - 1)
    seed(dsn, 1000)
    assert constraints(dsn) == {'message_receipts_check': True}
    with psycopg.connect(dsn) as c:
        before = c.execute('SELECT md5(string_agg(m::text,\'\' ORDER BY id)) FROM message_receipts m').fetchone()[0]
    assert store_module.migrate_expand(dsn, release_plan(VERSION - 1)) == {'schema_version': VERSION}
    assert constraints(dsn) == {'message_receipts_source_check': False} and version(dsn) == (VERSION, 1)
    with psycopg.connect(dsn) as c:
        assert c.execute('SELECT md5(string_agg(m::text,\'\' ORDER BY id)) FROM message_receipts m').fetchone()[0] == before
        assert c.execute(OLD_INSERT, receipt('synthetic', '-1000000000100', 23, 'provider_aggregate')).fetchone()
        assert c.execute(OLD_INSERT, receipt('synthetic', '-100', 23, 'provider_aggregate')).fetchone()
        assert c.execute(OLD_INSERT, receipt('synthetic', '100', 23, 'private_peer')).fetchone()
    for chat, audience in (('100', 'provider_aggregate'), ('-0', 'provider_aggregate'), ('-1x', 'provider_aggregate'),
                           ('-100', 'all_members')):
        with psycopg.connect(dsn) as c, pytest.raises(psycopg.errors.CheckViolation):
            c.execute(OLD_INSERT, receipt('synthetic', chat, 23, audience))


# spec: ANYM-868
def test_a_rerun_over_a_rewound_ledger_is_idempotent(db):
    store, dsn = db
    with psycopg.connect(dsn) as c:
        c.execute('DELETE FROM archive_migrations WHERE version=%s', (VERSION,))
        c.execute("UPDATE probe_metadata SET value=%s WHERE name='schema_version'", (str(VERSION - 1),))
    store.migrate()
    assert constraints(dsn) == {'message_receipts_source_check': False} and version(dsn) == (VERSION, 1)
    store.migrate()
    assert constraints(dsn) == {'message_receipts_source_check': False}


def old_runtime(dsn, stop, log, kind, worker):
    """A previous runtime's reader or writer looping until `stop`: each statement's (kind, worker, start, end, error)."""
    with psycopg.connect(dsn, autocommit=True, options='-c statement_timeout=15000') as c:
        n = 0
        while not stop.is_set():
            n += 1; started = time.monotonic(); error = None
            try:
                if kind == 'reader': c.execute(REFERENCE_TG_SQL, ('synthetic', str(n % 500 + 1), n)).fetchall()
                elif kind == 'outgoing': c.execute("SELECT count(*) FROM outgoing_messages WHERE account='synthetic'")
                else: c.execute(OLD_INSERT, receipt('synthetic', str(n % 500 + 1), RECEIPTS + n, 'private_peer'))
            except psycopg.Error as exc: error = type(exc).__name__
            log.append((kind, worker, started, time.monotonic(), error))
            time.sleep(0.02)


# spec: ANYM-868
@pytest.mark.parametrize('base', [DEPLOYED, VERSION - 1], ids=['from-deployed-70', 'from-previous'])
@pytest.mark.parametrize(('hold_s', 'delay_s', 'code'), [
    (1.0, 1.0, None),                    # the reader lets go inside lock_timeout; the batch commits
    (3.0, 0.0, 'lock_timeout'),          # the reader outlasts lock_timeout; the batch aborts
    (0.5, 5.0, 'transaction_timeout'),   # a later statement of the batch outlasts the transaction budget
], ids=['commits', 'lock_wait_aborts', 'slow_batch_aborts'])
def test_the_swap_on_100k_receipts_commits_or_aborts_inside_the_runner_bounds(at_schema, base, hold_s, delay_s, code):
    """Criterion 1's lock test through the real migrate-expand batch from a database built at `base` with that
    schema's own SQL: from 70 the batch also holds 071's outgoing_messages lock and its gateway grant while the swap
    waits for message_receipts."""
    dsn = at_schema(base)
    gateway_role(dsn); seed(dsn)
    plan = release_plan(base)
    assert ('gateway_role.sql' in appended_roles(base)) == (base < 71)  # 071's grant rides a release from 70
    holder = psycopg.connect(dsn)
    holder.execute('SELECT count(*) FROM message_receipts')  # an old-runtime reader holds ACCESS SHARE
    stop, log, inside, result = threading.Event(), [], {}, {}

    def later_statement(c):
        # Runs in the batch's transaction after every file and role grant: the swap scanned no row of
        # message_receipts and holds ACCESS EXCLUSIVE on it.
        inside['seq_scan'] = c.execute("SELECT coalesce(seq_scan,0) AS n FROM pg_stat_xact_user_tables "
                                       "WHERE relname='message_receipts'").fetchone()['n']
        inside['mode'] = c.execute("SELECT mode FROM pg_locks WHERE pid=pg_backend_pid() AND granted "
                                   "AND relation='message_receipts'::regclass ORDER BY mode").fetchall()
        if delay_s: c.execute('SELECT pg_sleep(%s)', (delay_s,))

    def migrate():
        started = time.monotonic()
        try: result['out'] = store_module.migrate_expand(dsn, plan, before_commit=later_statement)
        except ExpandError as exc: result['out'] = exc.code
        result['span'] = (started, time.monotonic())

    sessions = [threading.Thread(target=old_runtime, args=(dsn, stop, log, kind, i))
                for i, kind in enumerate(['reader', 'reader', 'writer', 'writer', 'outgoing'])]
    for s in sessions: s.start()
    time.sleep(0.3)
    runner = threading.Thread(target=migrate); runner.start()
    time.sleep(hold_s); holder.close()
    runner.join(30); time.sleep(0.5); stop.set()
    for s in sessions: s.join(30)
    started, ended = result['span']
    assert result['out'] == ({'schema_version': VERSION} if code is None else code)
    assert ended - started <= LOCK_S + TRANSACTION_S + 1
    # Every old-runtime statement finished without an error, and none waited past the runner's bounds: a statement
    # blocked behind the batch waits from its own start until the batch commits or aborts.
    assert log and all(error is None for *_, error in log), [e for *_, e in log if e]
    assert max(end - begin for _, _, begin, end, _ in log) <= LOCK_S + TRANSACTION_S
    blocked = [(begin, end) for _, _, begin, end, _ in log if started <= begin <= ended and end - begin > 0.3]
    assert blocked and all(end <= ended + 0.5 for _, end in blocked)
    assert {kind for kind, _, begin, _, _ in log if begin > ended} == {'reader', 'writer', 'outgoing'}
    if code is None:
        assert inside['seq_scan'] == 0 and {'mode': 'AccessExclusiveLock'} in inside['mode']
        assert constraints(dsn) == {'message_receipts_source_check': False}
        assert version(dsn, base) == (VERSION, VERSION - base) and menu_choice(dsn) == 1
    else:
        # Nothing applied, from any file of the batch: the old CHECK in place, no later column, the version not
        # recorded. The next deploy retries the same batch.
        assert constraints(dsn) == {'message_receipts_check': True}
        assert version(dsn, base) == (base, 0) and menu_choice(dsn) == (1 if base >= 71 else 0)
        assert store_module.migrate_expand(dsn, plan) == {'schema_version': VERSION}
        assert constraints(dsn) == {'message_receipts_source_check': False}
        assert version(dsn, base) == (VERSION, VERSION - base) and menu_choice(dsn) == 1


# spec: ANYM-868
def test_a_validating_swap_would_scan_the_table(db):
    """The control for the zero seq_scan above: the same swap without NOT VALID scans message_receipts once."""
    _, dsn = db
    seed(dsn, 1000)
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        c.execute(MIGRATION.read_text().replace(' NOT VALID;', ';'))
        assert c.execute("SELECT seq_scan FROM pg_stat_xact_user_tables WHERE relname='message_receipts'"
                         ).fetchone()['seq_scan'] >= 1
        c.rollback()


# spec: ANYM-868
def test_a_previous_runtime_writes_and_reads_receipts_on_the_widened_schema(db):
    """Rollback is application-only: the schema-(VERSION-1) write and reads (receipts.record's INSERT, REFERENCE_TG_SQL,
    ROW_PROOFS_SQL, all unchanged by this card) work on the new schema, and read an aggregate row a newer runtime
    wrote without filtering on audience."""
    _, dsn = db
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        c.execute("INSERT INTO accounts(id,platform,label) VALUES ('synthetic','telegram','synthetic')")
        c.execute("INSERT INTO chats(id,account,title,provider_chat,chat_type) VALUES "
                  "('synthetic-group','synthetic','Group','-100','group'),('synthetic-peer','synthetic','Peer','100','private')")
        for chat, mid in (('synthetic-group', '17'), ('synthetic-peer', '17')):
            c.execute("INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction) "
                      "VALUES (%s,'synthetic',%s,%s,'42','synthetic',now(),'outgoing')", (chat + '-m', chat, mid))
        assert c.execute(OLD_INSERT, receipt('synthetic', '100', 20, 'private_peer')).fetchone()
        assert c.execute(OLD_INSERT, receipt('synthetic', '-100', 20, 'provider_aggregate')).fetchone()
        found = {r['id']: (r['state'], r['audience']) for r in c.execute(
            ROW_PROOFS_SQL, (['synthetic-group-m', 'synthetic-peer-m'],))}
        assert found == {'synthetic-group-m': ('read', 'provider_aggregate'), 'synthetic-peer-m': ('read', 'private_peer')}
        assert [r['audience'] for r in c.execute(REFERENCE_TG_SQL, ('synthetic', '-100', 17))] == ['provider_aggregate']
