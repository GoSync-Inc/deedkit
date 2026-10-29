"""ANYM-621: each account's collector re-checks its open version conflicts under each new rule by itself."""
from contextlib import contextmanager
import hashlib
import itertools
from pathlib import Path
import threading
import time

from hypothesis import HealthCheck, given, settings, strategies as st
import psycopg
from psycopg.types.json import Jsonb
import pytest

from messaging_service import conflict_reevaluation as cr
from messaging_service.conflict_reevaluation import Job, Runner, _Dedicated
from messaging_service.envelope import canonical, validate
from messaging_service.migration_rollout import new_tables, role_class, unattended_class
from test_archive import archive, event  # noqa: F401 (archive is a fixture)
from test_db_role_isolation import FORBIDDEN, as_role, install, remove

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'services/messaging/messaging_service'
MIGRATION = SOURCE/'migrations/057_conflict_reevaluation.sql'
BODY = 'telegram_body_equal_version'
NEWER = 'telegram_body_newer_version'  # the class of a row raised by an older rule (raised())
EXAMPLES = itertools.count()


def contradict(ar, provider_id, body='Другой текст', account='archive-test', chat='100'):
    mid = ar.ingest(event(provider_id=provider_id, account=account, chat=chat))['message']
    assert ar.ingest(event(provider_id=provider_id, account=account, chat=chat, body=body))['outcome'] == 'conflict'
    return mid


def raised(ar, provider_id, body='Другой текст', account='archive-test', chat='100', reason='telegram_body_newer_version'):
    """A conflict an older rule raised: today's ingest accepts this stored candidate (the same text with its formatting
    completed) as a new revision at any provider version order."""
    from messaging_service.incoming_formatting import telegram
    mid = ar.ingest(event(provider_id=provider_id, account=account, chat=chat, body=body))['message']
    candidate = validate(event(provider_id=provider_id, account=account, chat=chat, body=body,
                               formatting=telegram(body, [])))
    digest = hashlib.sha256(canonical(candidate)).hexdigest()
    with ar.store.pool.connection() as c:
        c.execute("INSERT INTO ingest_receipts(digest,account,envelope,outcome) VALUES(%s,%s,%s,'conflict')",
                  (digest, account, Jsonb(candidate)))
        c.execute('''INSERT INTO message_conflicts(receipt_digest,message,observed_revision,candidate_updated_at,
            observed_at,reason,reason_version) VALUES(%s,%s,1,%s,clock_timestamp(),%s,1)''',
            (digest, mid, candidate['updated_at'], reason))
    return mid


@contextmanager
def runner_connection(s):
    with psycopg.connect(s.pool.conninfo, **{**s.pool.kwargs, 'autocommit': True}) as c: yield c


def lease(ar, account='archive-test'):
    """The test collector's lease: a later start of the same collector keeps its epoch."""
    with ar.store.pool.connection() as c:
        row = c.execute('''SELECT epoch FROM collector_leases WHERE account=%s AND owner='reevaluation-test'
            AND expires_at>clock_timestamp()''', (account,)).fetchone()
    return ('reevaluation-test', row['epoch'] if row else ar.acquire(account, 'reevaluation-test'))


def run(ar, s, jobs, account='archive-test', held=None, **options):
    with runner_connection(s) as c:
        return Runner(s, account, held or lease(ar, account), jobs=jobs, **options).run(c)


def event_message(provider_id, account='archive-test', chat='100'):
    from messaging_service.envelope import stable_id
    return stable_id('msg', account, chat, provider_id)


def conflicts(s, account='archive-test'):
    with s.pool.connection() as c:
        return {r['message']: r for r in c.execute('''SELECT mc.* FROM message_conflicts mc JOIN messages m
            ON m.id=mc.message WHERE m.account=%s''', (account,))}


def progress(s, account='archive-test'):
    with s.pool.connection() as c:
        return {r['job']: r for r in c.execute('SELECT * FROM conflict_reevaluations WHERE account=%s', (account,))}


def body_rule(keep):
    """A synthetic rule fix: a row of the body class whose candidate text is in `keep` is still raised."""
    return lambda row: 'keep' if row['envelope']['body'] in keep else 'resolve'


def resolve_job(keep=(), name='synthetic_body', version=1, function=None):
    return Job(name, version, 'resolve', function or body_rule(keep), classes=('telegram_body_*',))


def unclassify(s, mid):
    with s.pool.connection() as c:
        c.execute('UPDATE message_conflicts SET reason=NULL,reason_version=NULL WHERE message=%s', (mid,))


# spec: ANYM-621
def test_migration_is_expand_and_role_files_only_append_grants():
    text = MIGRATION.read_text()
    assert unattended_class(text, {'message_conflicts', 'accounts'}) == 'expand'
    created = new_tables(text)
    assert set(created) == {'conflict_reevaluations'}
    assert created['conflict_reevaluations'] == {'job', 'job_version', 'account', 'cursor', 'watermark', 'done_at', 'last_error'}
    for name, line in [('collector_role.sql', 'GRANT SELECT,INSERT,UPDATE(cursor,watermark,done_at,last_error) ON conflict_reevaluations TO messaging_collector;'),
                       ('gateway_role.sql', 'GRANT SELECT ON conflict_reevaluations TO messaging_gateway;')]:
        current = (SOURCE/name).read_text()
        assert line in current
        current = current[:current.index(line)+len(line)]  # later schemas append after this grant
        base = current[:current.rindex(line)]
        base = base[:base.index('-- Schema 57')]  # without the comment above the grant
        assert role_class(base, current, created) == 'additive'


# spec: ANYM-621
def test_collector_role_has_exactly_select_insert_update_and_writes_only_its_account(archive):  # noqa: F811
    ar, s, _, _ = archive
    ar.register('archive-other', 'telegram', 'Synthetic other', include_chats=['200'])
    role = install(s, 'collector_role.sql', 'messaging_collector', account='archive-test')
    try:
        with s.pool.connection() as c:
            granted = {r['privilege_type'] for r in c.execute('''SELECT privilege_type FROM information_schema.role_table_grants
                WHERE grantee=%s AND table_name='conflict_reevaluations' ''', (role,))}
            columns = {r['column_name'] for r in c.execute('''SELECT column_name FROM information_schema.column_privileges
                WHERE grantee=%s AND table_name='conflict_reevaluations' AND privilege_type='UPDATE' ''', (role,))}
        # SELECT, INSERT and UPDATE of the progress columns only: the key columns are not updatable.
        assert granted == {'SELECT', 'INSERT'} and columns == {'cursor', 'watermark', 'done_at', 'last_error'}
        insert = "INSERT INTO conflict_reevaluations(job,job_version,account) VALUES('synthetic_body',1,%s)"
        with as_role(s, role) as c:
            c.execute(insert, ('archive-test',))
            c.execute("UPDATE conflict_reevaluations SET last_error='evaluation_error' WHERE account='archive-test'")
        with s.pool.connection() as c: c.execute(insert, ('archive-other',))
        for statement, params in [(insert, ('archive-other',)),
                ("UPDATE conflict_reevaluations SET done_at=now() WHERE account='archive-other'", ())]:
            with pytest.raises(psycopg.errors.CheckViolation), as_role(s, role) as c: c.execute(statement, params)
        # Moving a row between accounts in either direction (Astra r1 B1: a CHECK sees only the new row).
        with s.pool.connection() as c: c.execute("UPDATE conflict_reevaluations SET job='synthetic_free' WHERE account='archive-test'")
        for statement in ("UPDATE conflict_reevaluations SET account='archive-test' WHERE account='archive-other'",
                          "UPDATE conflict_reevaluations SET account='archive-other' WHERE account='archive-test'",
                          "UPDATE conflict_reevaluations SET job='synthetic_free' WHERE account='archive-other'"):
            with pytest.raises(FORBIDDEN), as_role(s, role) as c: c.execute(statement)
        with pytest.raises(FORBIDDEN), as_role(s, role) as c: c.execute('DELETE FROM conflict_reevaluations')
        assert progress(s, 'archive-other')['synthetic_body']['done_at'] is None
    finally:
        remove(s, role)


def n1_archive(store):
    """An older deployed Archive (schema 55, before ANYM-558/560/613): its pinned copy, loaded inside messaging_service."""
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader('messaging_service.archive_n1',
                                                  str(ROOT/'tests/fixtures/anym621/archive_schema55.py.txt'))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module.Archive(store)


# spec: ANYM-621
def test_ingest_resolves_name_how_and_n1_writers_leave_resolution_unknown(archive):  # noqa: F811
    ar, s, key, _ = archive
    edited = contradict(ar, '1')
    assert ar.ingest(event(provider_id='1', body='Правка', updated_at='2026-10-01T12:05:00Z'))['outcome'] == 'edited'
    # The deployed N-1 ingest at schema 57: it raises (with a class) and resolves, and leaves resolution unknown.
    old = n1_archive(s)
    assert old.ingest(event(provider_id='2'))['outcome'] == 'created'
    assert old.ingest(event(provider_id='2', body='Другой текст'))['outcome'] == 'conflict'
    assert old.ingest(event(provider_id='2', body='Правка', updated_at='2026-10-01T12:05:00Z'))['outcome'] == 'edited'
    legacy = event_message('2')
    rows = conflicts(s)
    assert rows[edited]['resolution'] == 'newer_observation' and rows[edited]['resolved_revision'] == 2
    assert rows[legacy]['resolved_revision'] == 2 and rows[legacy]['resolution'] is None
    assert rows[legacy]['reason'] == BODY
    assert s.get_sync_status(key, refresh=True)['sources'][0]['historical_conflicts'] == 2


# spec: ANYM-621
def test_without_migration_57_old_and_new_code_ingest_and_the_runner_refuses_jobs(archive):  # noqa: F811
    from messaging_service.archive import Archive
    ar, s, _, _ = archive
    mid = contradict(ar, '1')
    held = lease(ar)
    with runner_connection(s) as c:
        with c.transaction(force_rollback=True):
            # A schema-56 database: the migration's column and table are absent, inside a transaction rolled back.
            c.execute('ALTER TABLE message_conflicts DROP COLUMN resolution')
            c.execute('DROP TABLE conflict_reevaluations')
            c.execute("UPDATE probe_metadata SET value='56' WHERE name='schema_version'")
            new, old = Archive(_Dedicated(s, c)), n1_archive(_Dedicated(s, c))
            assert new.ingest(event(provider_id='1', body='Правка', updated_at='2026-10-01T12:05:00Z'))['outcome'] == 'edited'
            assert old.ingest(event(provider_id='2'))['outcome'] == 'created'
            assert old.ingest(event(provider_id='2', body='Другой текст'))['outcome'] == 'conflict'
            assert new.ingest(event(provider_id='2', body='Правка', updated_at='2026-10-01T12:05:00Z'))['outcome'] == 'edited'
            resolved = {r['message']: r['resolved_revision'] for r in c.execute('SELECT message,resolved_revision FROM message_conflicts')}
            assert resolved[mid] == resolved[event_message('2')] == 2
            assert Runner(s, 'archive-test', held, jobs=[resolve_job()]).run(c) == 'schema_not_ready'
            assert cr.status(c, ['archive-test']) == {}
    assert conflicts(s)[mid]['resolved_at'] is None


# spec: ANYM-621
def test_resolve_job_closes_only_rows_the_current_rule_would_not_raise_and_reports_them(archive):  # noqa: F811
    ar, s, key, _ = archive
    kept = raised(ar, '1', body='Всё ещё спор')
    fixed = [raised(ar, str(n), body=f'Исправлено {n}') for n in range(2, 5)]
    other = raised(ar, '9', body='')  # another class: never selected by the job
    with s.pool.connection() as c:
        c.execute("UPDATE message_conflicts SET reason='telegram_sender_equal_version' WHERE message=%s", (other,))
    job = resolve_job(keep={'Всё ещё спор'})
    assert run(ar, s, [job]) == 'done'
    rows = conflicts(s)
    assert rows[kept]['resolved_at'] is None and rows[other]['resolved_at'] is None
    for mid in fixed:
        assert rows[mid]['resolution'] == 'rule_changed:synthetic_body@1'
        assert rows[mid]['resolved_revision'] == rows[mid]['observed_revision'] == 1
        assert rows[mid]['reason'] == NEWER
    line = s.get_sync_status(key, refresh=True)['sources'][0]
    assert line['conflict_reevaluations'] == [{'job': 'synthetic_body', 'version': 1, 'done': True,
        'done_at': line['conflict_reevaluations'][0]['done_at'], 'rows_resolved': 3, 'last_error': None}]
    assert line['active_conflicts'] == 2
    # A rerun after done_at only scans for newer rows: nothing to write.
    before = progress(s)['synthetic_body']
    assert run(ar, s, [job]) == 'done'
    assert progress(s)['synthetic_body'] == before and conflicts(s) == rows


# spec: ANYM-621
def test_catch_up_after_done_covers_rows_written_later_and_a_new_version_starts_over(archive):  # noqa: F811
    ar, s, _, _ = archive
    first = raised(ar, '1')
    assert run(ar, s, [resolve_job()]) == 'done'
    later = raised(ar, '2')
    with s.pool.connection() as c:  # an N-1 writer during a rollback resolves nothing new; a row raised later
        assert c.execute('SELECT resolved_at FROM message_conflicts WHERE message=%s', (later,)).fetchone()['resolved_at'] is None
    assert run(ar, s, [resolve_job()]) == 'done'
    assert conflicts(s)[later]['resolution'] == 'rule_changed:synthetic_body@1'
    reopened = raised(ar, '3', body='Ещё один')
    assert run(ar, s, [resolve_job(keep={'Ещё один'}, version=2)]) == 'done'
    assert conflicts(s)[reopened]['resolved_at'] is None and conflicts(s)[first]['resolution'].endswith('@1')
    assert {(r['job'], r['job_version']) for r in progress(s).values()} >= {('synthetic_body', 2)}


# spec: ANYM-621
def test_rule_jobs_wait_for_classification_and_classify_stores_a_class(archive):  # noqa: F811
    ar, s, _, _ = archive
    mid = raised(ar, '1')
    unclassify(s, mid)
    assert run(ar, s, [resolve_job()]) == 'done'
    assert progress(s)['synthetic_body']['last_error'] == 'classify_pending'
    assert conflicts(s)[mid]['resolved_at'] is None
    classify = Job('synthetic_classify', 1, 'classify', lambda row: NEWER)
    assert run(ar, s, [resolve_job(), classify][::-1]) == 'done'
    row = conflicts(s)[mid]
    assert row['reason'] == NEWER and row['reason_version'] == 1
    assert row['resolution'] == 'rule_changed:synthetic_body@1'
    assert progress(s)['synthetic_body']['last_error'] is None and progress(s)['synthetic_classify']['done_at']


# spec: ANYM-621
def test_registry_refuses_unknown_kinds_classes_and_duplicate_names(monkeypatch):
    monkeypatch.setattr(cr, 'JOBS', {})
    with pytest.raises(ValueError, match='invalid_job_classes'): Job('x', 1, 'resolve', lambda row: 'keep', classes=('nothing_*',))
    with pytest.raises(ValueError, match='invalid_job_classes'): Job('x', 1, 'resolve', lambda row: 'keep', classes=('unclassified',))
    with pytest.raises(ValueError, match='invalid_job_classes'): Job('x', 1, 'classify', lambda row: BODY, classes=(BODY,))
    with pytest.raises(ValueError, match='invalid_job_kind'): Job('x', 1, 'rewrite', lambda row: 'keep')
    with pytest.raises(ValueError, match='invalid_job_name'): Job('X;', 1, 'classify', lambda row: BODY)
    cr.register(resolve_job())
    with pytest.raises(ValueError, match='duplicate_job'): cr.register(resolve_job(version=2))
    assert resolve_job().classes == tuple(f'telegram_body_{r}' for r in ('equal_version', 'newer_version', 'older_version'))
    assert cr.start(object(), 'archive-test', ('o', 1)) is None  # no store: nothing starts, nothing raises


# spec: ANYM-621
@pytest.mark.parametrize('function,code,malformed', [
    (lambda row: 1/0, 'evaluation_error', False),
    (lambda row: 'resolve_everything', 'invalid_result', False),
    (lambda row: row['envelope']['body'].strip() and 'resolve', 'evaluation_error', True),
], ids=['exception', 'invalid-result', 'malformed-envelope'])
def test_a_failing_function_records_a_fixed_code_and_writes_nothing(archive, function, code, malformed):  # noqa: F811
    ar, s, _, _ = archive
    mid = contradict(ar, '1')
    if malformed:
        with s.pool.connection() as c:  # a malformed legacy receipt: no body
            c.execute("UPDATE ingest_receipts SET envelope=envelope-'body' WHERE digest=%s",
                      (conflicts(s)[mid]['receipt_digest'],))
    assert run(ar, s, [resolve_job(function=function), resolve_job(name='synthetic_next', function=lambda row: 'resolve')]) == 'done'
    assert progress(s)['synthetic_body']['last_error'] == code and progress(s)['synthetic_body']['cursor'] is None
    assert conflicts(s)[mid]['resolved_at'] is None or conflicts(s)[mid]['resolution'] == 'rule_changed:synthetic_next@1'
    assert progress(s)['synthetic_next']['done_at']  # one job's error does not stop the next job


# spec: ANYM-621
def test_a_function_that_never_returns_holds_no_lock_and_the_next_start_skips_its_row(archive):  # noqa: F811
    ar, s, _, _ = archive
    stuck, fine = raised(ar, '1', body='Зависание'), raised(ar, '2')
    release = threading.Event()

    def stall(row):
        if row['envelope']['body'] == 'Зависание': release.wait(30)
        return 'resolve'
    try:
        held = lease(ar)
        result = {}
        worker = threading.Thread(target=lambda: result.update(code=run(ar, s, [resolve_job(function=stall)], held=held,
                                                                         evaluation_timeout=0.5)))
        worker.start()
        time.sleep(0.2)
        # While the function stalls, capture of the same account and other accounts proceeds at once.
        started = time.monotonic()
        assert ar.ingest(event(provider_id='3', body='Живой поток'))['outcome'] == 'created'
        assert time.monotonic() - started < 1
        with s.pool.connection() as c:
            assert not c.execute('''SELECT 1 FROM pg_locks WHERE locktype='advisory' AND granted
                AND pid<>pg_backend_pid()''').fetchall()
        worker.join(5)
        assert result['code'] == 'evaluation_timeout'
        assert progress(s)['synthetic_body']['last_error'] == 'evaluation_timeout'
        assert conflicts(s)[stuck]['resolved_at'] is None
        assert run(ar, s, [resolve_job(function=stall)], held=held, evaluation_timeout=0.5) == 'done'
        rows = conflicts(s)
        assert rows[stuck]['resolved_at'] is None and rows[fine]['resolution'] == 'rule_changed:synthetic_body@1'
    finally:
        release.set()


# spec: ANYM-621
def test_a_busy_account_lock_skips_the_tick_without_delaying_ingest(archive):  # noqa: F811
    ar, s, _, _ = archive
    mid = raised(ar, '1')
    held, result = lease(ar), {}
    runner = Runner(s, 'archive-test', held, jobs=[resolve_job()], tick=0.1)
    with runner_connection(s) as other, runner_connection(s) as c:
        with other.transaction():
            # A concurrent ingest holds the account lock: the runner's try-lock skips, it never queues behind it.
            other.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('archive:archive-test',))
            worker = threading.Thread(target=lambda: result.update(code=runner.run(c)))
            worker.start()
            time.sleep(0.5)
            assert conflicts(s)[mid]['resolved_at'] is None and 'synthetic_body' not in progress(s)
            with s.pool.connection() as probe:
                assert not probe.execute("SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted").fetchall()
        worker.join(5)
    assert result['code'] == 'done' and conflicts(s)[mid]['resolution'] == 'rule_changed:synthetic_body@1'


# spec: ANYM-621
def test_the_lifecycle_lock_contended_does_not_hold_up_resolve(archive):  # noqa: F811
    from messaging_service.file_lifecycle import LOCK_NAME
    ar, s, _, _ = archive
    mid = raised(ar, '1')
    with runner_connection(s) as other, other.transaction():
        other.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (LOCK_NAME,))
        started = time.monotonic()
        assert run(ar, s, [resolve_job()]) == 'done'
        assert time.monotonic() - started < 2
    assert conflicts(s)[mid]['resolution'] == 'rule_changed:synthetic_body@1'


# spec: ANYM-621
def test_a_lost_lease_writes_nothing(archive):  # noqa: F811
    ar, s, _, _ = archive
    mid = raised(ar, '1')
    held = lease(ar)
    ar.release('archive-test', *held)
    with pytest.raises(RuntimeError, match='collector_lease_lost'): run(ar, s, [resolve_job()], held=held)
    assert conflicts(s)[mid]['resolved_at'] is None and progress(s) == {}


# spec: ANYM-621
def test_a_row_ingest_resolved_between_evaluation_and_apply_is_not_touched(archive):  # noqa: F811
    ar, s, _, _ = archive
    mid = contradict(ar, '1')

    def meanwhile(row):
        # Live ingest edits the message after the snapshot, outside the runner's transaction.
        ar.ingest(event(provider_id='1', body='Правка', updated_at='2026-10-01T12:05:00Z'))
        return 'resolve'
    assert run(ar, s, [resolve_job(function=meanwhile)]) == 'done'
    row = conflicts(s)[mid]
    assert row['resolution'] == 'newer_observation' and row['resolved_revision'] == 2


# spec: ANYM-621
def test_a_row_committed_inside_the_scanned_range_after_the_snapshot_is_not_skipped(archive, monkeypatch):  # noqa: F811
    ar, s, _, _ = archive
    first, last = raised(ar, '1'), raised(ar, '3')
    late = raised(ar, '2')
    with s.pool.connection() as c:  # observed between the two rows, committed after the evaluation read them
        c.execute('''UPDATE message_conflicts SET observed_at=(SELECT observed_at FROM message_conflicts WHERE message=%s)
            +interval '1 microsecond',reason='telegram_sender_equal_version' WHERE message=%s''', (first, late))
    calls = []

    def reclassify(row):
        if not calls:
            with s.pool.connection() as c:
                c.execute('UPDATE message_conflicts SET reason=%s WHERE message=%s', (NEWER, late))
        calls.append(row)
        return 'resolve'
    assert run(ar, s, [resolve_job(function=reclassify)]) == 'done'
    assert {mid: r['resolution'] for mid, r in conflicts(s).items()} == dict.fromkeys(
        (first, late, last), 'rule_changed:synthetic_body@1')


# spec: ANYM-621
@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(st.booleans(), min_size=1, max_size=9), st.integers(1, 4), st.integers(0, 3))
def test_any_crash_point_resumes_without_double_resolution(archive, keep, batch, crashes):  # noqa: F811
    """Invariant: after any number of interrupted starts, exactly the rows the rule would not raise are resolved,
    each once, by the job; kept rows stay open."""
    ar, s, _, _ = archive
    run_id = next(EXAMPLES)  # one database per test: each example adds its own rows after the earlier ones
    mids = {(contradict if k else raised)(ar, f'p{run_id}_{n}', body=('Спор' if k else 'Исправлено') + str(n)): k for n, k in enumerate(keep)}
    job = Job('synthetic_body', 1, 'resolve', lambda row: 'keep' if row['envelope']['body'].startswith('Спор') else 'resolve',
              classes=('telegram_body_*',))
    held = lease(ar)
    original = cr.BATCH
    cr.BATCH = batch
    try:
        for _ in range(crashes):
            runner = Runner(s, 'archive-test', held, jobs=[job])
            seen = []
            runner_function = job.function
            stopping = Job(job.name, 1, 'resolve', lambda row: (seen.append(1), runner.stopping.set(),
                                                                 runner_function(row))[-1], classes=job.classes)
            runner.jobs = [stopping]
            with runner_connection(s) as c: assert runner.run(c) in ('stopped', 'done')
        assert run(ar, s, [job], held=held) == 'done'
    finally:
        cr.BATCH = original
    rows = conflicts(s)
    for mid, kept in mids.items():
        assert (rows[mid]['resolved_at'] is None) == kept
        assert rows[mid]['resolution'] == (None if kept else 'rule_changed:synthetic_body@1')
    assert progress(s)['synthetic_body']['done_at'] and progress(s)['synthetic_body']['last_error'] is None


# spec: ANYM-621
def test_accept_writes_the_completed_revision_through_ingest_and_names_the_rule(archive):  # noqa: F811
    ar, s, key, _ = archive
    mid = contradict(ar, '1', body='Полный текст')

    def complete(row):
        return {**row['envelope'], 'updated_at': '2026-10-01T12:00:01+00:00'}
    job = Job('synthetic_accept', 1, 'accept', complete, classes=(BODY,))
    assert run(ar, s, [job]) == 'done'
    row = conflicts(s)[mid]
    assert row['resolution'] == 'rule_changed:synthetic_accept@1' and row['resolved_revision'] == 2
    with s.pool.connection() as c:
        message = c.execute('SELECT body,revision FROM messages WHERE id=%s', (mid,)).fetchone()
        events = c.execute("SELECT count(*) AS n FROM events WHERE message=%s AND kind='message.edited'", (mid,)).fetchone()
    assert message == {'body': 'Полный текст', 'revision': 2} and events['n'] == 1
    # A candidate for another message is refused before any write.
    other = contradict(ar, '2', body='Чужой')
    stray = Job('synthetic_stray', 1, 'accept', lambda row: {**row['envelope'], 'provider_id': '1'}, classes=(BODY,))
    assert run(ar, s, [stray]) == 'done'
    assert progress(s)['synthetic_stray']['last_error'] == 'invalid_result' and conflicts(s)[other]['resolved_at'] is None


# spec: ANYM-621
def test_collector_start_runs_registered_jobs_on_their_own_thread(archive, monkeypatch):  # noqa: F811
    ar, s, _, _ = archive
    mid = raised(ar, '1')
    monkeypatch.setattr(cr, 'JOBS', {})
    cr.register(resolve_job())
    runner = cr.start(ar, 'archive-test', lease(ar))
    runner.thread.join(10)
    assert runner.code == 'done' and runner.thread.name == 'conflict-reevaluation'
    assert conflicts(s)[mid]['resolution'] == 'rule_changed:synthetic_body@1'
    monkeypatch.setattr(cr, 'JOBS', {})
    assert cr.start(ar, 'archive-test', ('o', 1)) is None



# spec: ANYM-621
def test_status_read_without_the_grant_is_empty_and_the_gateway_reads_progress(archive):  # noqa: F811
    ar, s, _, _ = archive
    raised(ar, '1')
    assert run(ar, s, [resolve_job()]) == 'done'
    role = install(s, 'gateway_role.sql', 'messaging_gateway')
    try:
        with as_role(s, role) as c:
            assert [j['rows_resolved'] for j in cr.status(c, ['archive-test'])['archive-test']] == [1]
        with s.pool.connection() as c:
            c.execute(f'REVOKE SELECT ON conflict_reevaluations FROM "{role}"')
        with as_role(s, role) as c:
            assert cr.status(c, ['archive-test']) == {}
    finally:
        remove(s, role)


# spec: ANYM-621
def test_no_job_resolves_a_row_the_current_rule_still_raises(archive):  # noqa: F811
    ar, s, _, _ = archive
    genuine, fixed = contradict(ar, '1'), raised(ar, '2')
    assert run(ar, s, [resolve_job(function=lambda row: 'resolve')]) == 'done'
    rows = conflicts(s)
    # Ingest's own rule (archive.ingest_outcome) still raises the equal-version contradiction: it stays open.
    assert rows[genuine]['resolved_at'] is None and rows[fixed]['resolution'] == 'rule_changed:synthetic_body@1'
    assert progress(s)['synthetic_body']['last_error'] == 'rule_still_raises'
    with s.pool.connection() as c:
        evidence = {r['message']: r for r in c.execute(cr.CANDIDATES.format(selector='true'),
            {'account': 'archive-test', 'at': None, 'digest': None, 'limit': 50})}
    assert cr.still_raised(evidence[genuine]) is True
    assert cr.still_raised({**evidence[genuine], 'envelope': {'body': 1}}) is True  # unreadable evidence stays open


# spec: ANYM-621
def test_accept_replays_a_stored_candidate_the_current_rule_accepts(archive):  # noqa: F811
    ar, s, _, _ = archive
    mid, genuine = raised(ar, '1', body='Завершённый текст'), contradict(ar, '2')
    job = Job('synthetic_accept', 1, 'accept', lambda row: dict(row['envelope']), classes=('telegram_body_*',))
    assert run(ar, s, [job]) == 'done'
    row = conflicts(s)[mid]
    assert row['resolution'] == 'rule_changed:synthetic_accept@1' and row['resolved_revision'] == 2
    with s.pool.connection() as c:
        assert c.execute('SELECT body FROM messages WHERE id=%s', (mid,)).fetchone()['body'] == 'Завершённый текст'
        assert c.execute('SELECT count(*) AS n FROM ingest_receipts WHERE digest=%s', (row['receipt_digest'],)).fetchone()['n'] == 1
    # The genuine contradiction replays as a conflict again: refused, nothing written.
    assert progress(s)['synthetic_accept']['last_error'] == 'accept_rejected'
    assert conflicts(s)[genuine]['resolved_at'] is None


# spec: ANYM-621
def test_an_apply_past_its_wall_deadline_rolls_back_with_a_code(archive):  # noqa: F811
    ar, s, _, _ = archive
    mid = raised(ar, '1')
    assert run(ar, s, [resolve_job()], wall=0) == 'done'
    assert progress(s)['synthetic_body']['last_error'] == 'wall_deadline' and progress(s)['synthetic_body']['cursor'] is None
    assert conflicts(s)[mid]['resolved_at'] is None
    with runner_connection(s) as c, c.transaction():
        c.execute(cr.TRANSACTION_TIMEOUT)
        assert c.execute('SHOW transaction_timeout').fetchone()['transaction_timeout'] == '5s'


# spec: ANYM-621
def test_a_read_timeout_records_its_code_and_the_next_job_runs(archive, monkeypatch):  # noqa: F811
    ar, s, _, _ = archive
    mid = raised(ar, '1')
    original = Runner._read

    def slow(self, c, job):
        if job.name == 'synthetic_body': raise psycopg.errors.QueryCanceled('canceling statement due to statement timeout')
        return original(self, c, job)
    monkeypatch.setattr(Runner, '_read', slow)
    assert run(ar, s, [resolve_job(), resolve_job(name='synthetic_next')]) == 'done'
    assert progress(s)['synthetic_body']['last_error'] == 'statement_timeout'
    assert conflicts(s)[mid]['resolution'] == 'rule_changed:synthetic_next@1'


# spec: ANYM-621
def test_rule_jobs_never_pass_a_row_without_a_class_and_wait_for_a_classify_error(archive):  # noqa: F811
    ar, s, _, _ = archive
    first, middle, last = raised(ar, '1'), raised(ar, '2'), raised(ar, '3')
    classify = Job('synthetic_classify', 1, 'classify', lambda row: NEWER)

    def meanwhile(row):
        if row['envelope']['provider_id'] == '1': unclassify(s, middle)  # an older writer's row appears mid-scan
        return 'resolve'
    assert run(ar, s, [classify, resolve_job(function=meanwhile)]) == 'done'
    rows = conflicts(s)
    assert rows[first]['resolved_at'] and rows[middle]['reason'] is None and rows[last]['resolved_at'] is None
    assert progress(s)['synthetic_body']['last_error'] == 'classify_pending'
    # A classify job that fails in this start holds the rule jobs, whatever an earlier start completed.
    broken = Job('synthetic_classify', 1, 'classify', lambda row: 1/0)
    assert run(ar, s, [broken, resolve_job()]) == 'done'
    assert progress(s)['synthetic_classify']['done_at'] and progress(s)['synthetic_classify']['last_error'] == 'evaluation_error'
    assert progress(s)['synthetic_body']['last_error'] == 'classify_pending' and conflicts(s)[last]['resolved_at'] is None
    assert run(ar, s, [classify, resolve_job()]) == 'done'
    rows = conflicts(s)
    assert rows[middle]['resolution'] == rows[last]['resolution'] == 'rule_changed:synthetic_body@1'


# spec: ANYM-621
def test_a_stalled_classification_stores_unclassified_for_its_row(archive):  # noqa: F811
    ar, s, _, _ = archive
    mid = raised(ar, '1')
    unclassify(s, mid)
    release = threading.Event()
    try:
        stall = Job('synthetic_classify', 1, 'classify', lambda row: release.wait(30) and BODY)
        assert run(ar, s, [stall], evaluation_timeout=0.3) == 'evaluation_timeout'
    finally:
        release.set()
    assert conflicts(s)[mid]['reason'] == 'unclassified'


# spec: ANYM-621
def test_populated_progress_backs_up_and_restores_under_an_unbound_owner(archive, tmp_path):  # noqa: F811
    from test_subscription_retention import snapshot_restored
    ar, s, _, _ = archive
    raised(ar, '1')
    assert run(ar, s, [resolve_job()]) == 'done'
    role = install(s, 'collector_role.sql', 'messaging_collector', account='archive-test')
    try:
        with as_role(s, role) as c:  # a bound collector's own row
            c.execute("INSERT INTO conflict_reevaluations(job,job_version,account) VALUES('synthetic_bound',1,'archive-test')")
        with snapshot_restored(s, tmp_path) as restored, restored.pool.connection() as c:
            rows = c.execute('SELECT job,done_at IS NOT NULL AS done FROM conflict_reevaluations ORDER BY job').fetchall()
            assert rows == [{'job': 'synthetic_body', 'done': True}, {'job': 'synthetic_bound', 'done': False}]
            assert c.execute("SELECT count(*) AS n FROM message_conflicts WHERE resolution LIKE 'rule_changed:%'").fetchone()['n'] == 1
    finally:
        remove(s, role)


# spec: ANYM-621
def test_a_session_the_server_ends_at_its_transaction_timeout_is_recorded_and_the_next_job_runs(archive, monkeypatch):  # noqa: F811
    ar, s, _, _ = archive
    mid = raised(ar, '1')
    monkeypatch.setattr(cr, 'TRANSACTION_TIMEOUT', "SET LOCAL transaction_timeout='300ms'")
    original = Runner._cut

    def slow(self, c, job, start, scanned):
        if job.name == 'synthetic_body': c.execute('SELECT pg_sleep(1)')  # cumulative time past the server deadline
        return original(self, c, job, start, scanned)
    monkeypatch.setattr(Runner, '_cut', slow)
    with runner_connection(s) as c:
        assert Runner(s, 'archive-test', lease(ar), jobs=[resolve_job(), resolve_job(name='synthetic_next')]).run(c) == 'done'
    assert progress(s)['synthetic_body']['last_error'] == 'wall_deadline' and progress(s)['synthetic_body']['cursor'] is None
    assert conflicts(s)[mid]['resolution'] == 'rule_changed:synthetic_next@1'


# spec: ANYM-621
def test_an_unchanged_reobservation_makes_a_later_snapshot_class_untrusted(archive):  # noqa: F811
    """Astra r2 M2: body A at t0, unchanged A at t1, B at t1 is an equal-version conflict, whatever a class from the
    t0 snapshot says; it stays open."""
    ar, s, _, _ = archive
    mid = ar.ingest(event(provider_id='1'))['message']
    assert ar.ingest(event(provider_id='1', updated_at='2026-10-01T12:01:00+00:00'))['outcome'] == 'unchanged'
    assert ar.ingest(event(provider_id='1', body='Другой текст', updated_at='2026-10-01T12:01:00+00:00'))['outcome'] == 'conflict'
    with s.pool.connection() as c:
        c.execute('UPDATE message_conflicts SET reason=%s WHERE message=%s', (NEWER, mid))
    assert run(ar, s, [resolve_job(function=lambda row: 'resolve')]) == 'done'
    assert conflicts(s)[mid]['resolved_at'] is None and progress(s)['synthetic_body']['last_error'] == 'rule_still_raises'


# spec: ANYM-621
def test_a_sender_completion_at_an_unknown_version_order_stays_open(archive):  # noqa: F811
    """Sol r3 M2: an empty-sender outgoing message at t0, re-observed unchanged at t2, then completed at t1, is a
    conflict (completion needs the same version); the t0 snapshot cannot rule that order out, so it stays open."""
    ar, s, _, _ = archive
    private = {'chat_type': 'private'}
    base = dict(provider_id='1', direction='outgoing', sender='', metadata=private)
    mid = ar.ingest(event(**base))['message']
    assert ar.ingest(event(**base, updated_at='2026-10-01T12:02:00+00:00'))['outcome'] == 'unchanged'
    completed = event(**{**base, 'sender': '42', 'metadata': {**private, 'sender_identity_basis': 'authenticated_self'}},
                      updated_at='2026-10-01T12:01:00+00:00')
    assert ar.ingest(completed)['outcome'] == 'conflict'
    assert run(ar, s, [Job('synthetic_sender', 1, 'resolve', lambda row: 'resolve', classes=('telegram_sender_*',))]) == 'done'
    assert conflicts(s)[mid]['resolved_at'] is None and progress(s)['synthetic_sender']['last_error'] == 'rule_still_raises'


# spec: ANYM-621
def test_a_whatsapp_sent_at_jitter_conflict_an_older_rule_raised_is_no_longer_raised(archive):  # noqa: F811
    """Rebase on ANYM-558: the deployed N-1 ingest raised a WhatsApp re-read a few seconds off; ingest_outcome now
    carries 558's tolerance, so a rule job may close that row, and a real sent-time contradiction stays open."""
    from test_archive import jitter_event, shifted
    ar, s, _, _ = archive
    ar.register('archive-wa', 'whatsapp', 'Synthetic WhatsApp', include_chats=['900@lid'])
    old = n1_archive(s)
    jitter = old.ingest(jitter_event())['message']
    assert old.ingest(jitter_event(sent_at=shifted(3), origin='backfill'))['outcome'] == 'conflict'
    far = old.ingest(jitter_event(provider_id='true_900@lid_3EB0FAR'))['message']
    assert old.ingest(jitter_event(provider_id='true_900@lid_3EB0FAR', sent_at=shifted(60)))['outcome'] == 'conflict'
    job = Job('synthetic_jitter', 1, 'resolve', lambda row: 'resolve', classes=('whatsapp_*',))
    assert run(ar, s, [job], account='archive-wa') == 'done'
    rows = conflicts(s, 'archive-wa')
    assert rows[jitter]['resolution'] == 'rule_changed:synthetic_jitter@1' and rows[far]['resolved_at'] is None
    assert progress(s, 'archive-wa')['synthetic_jitter']['last_error'] == 'rule_still_raises'


# spec: ANYM-621
def test_a_whatsapp_business_text_conflict_an_older_rule_raised_is_accepted_as_a_completion(archive):  # noqa: F811
    """Rebase on ANYM-560: the deployed N-1 ingest raised a business message's text against its stored empty revision;
    ingest_outcome now completes it, so an accept job writes the text through ingest and names the rule."""
    from test_archive import BUSINESS_TEXT, business, unreadable
    ar, s, _, _ = archive
    ar.register('archive-wa', 'whatsapp', 'Synthetic WhatsApp', include_chats=['900@lid'])
    old = n1_archive(s)
    mid = old.ingest(unreadable(origin='live', metadata={'message_kind': 'unknown', 'chat_type': 'private'}))['message']
    assert old.ingest(business())['outcome'] == 'conflict'
    job = Job('synthetic_business', 1, 'accept', lambda row: row['envelope'], classes=('whatsapp_*',))
    assert run(ar, s, [job], account='archive-wa') == 'done'
    row = conflicts(s, 'archive-wa')[mid]
    assert row['resolution'] == 'rule_changed:synthetic_business@1' and row['resolved_revision'] == 2
    with s.pool.connection() as c:
        assert c.execute('SELECT body,revision FROM messages WHERE id=%s', (mid,)).fetchone() == \
            {'body': BUSINESS_TEXT, 'revision': 2}


# spec: ANYM-621
def test_an_authenticated_group_sender_conflict_an_older_rule_raised_is_accepted_as_a_completion(archive):  # noqa: F811
    """Rebase on ANYM-620: the deployed N-1 ingest raised the owner's Telegram-named group sender against its stored
    empty sender; ingest_outcome now completes it, so an accept job writes the sender through ingest."""
    from test_archive import GROUP_SELF, group_formatting
    ar, s, _, _ = archive
    old = n1_archive(s)
    original = event(provider_id='1', sender='', direction='outgoing', metadata={'chat_type': 'group'},
                     formatting=group_formatting())
    mid = old.ingest(original)['message']
    assert old.ingest({**original, 'sender': '42', 'metadata': GROUP_SELF})['outcome'] == 'conflict'
    job = Job('synthetic_group', 1, 'accept', lambda row: row['envelope'], classes=('telegram_*',))
    assert run(ar, s, [job]) == 'done'
    row = conflicts(s)[mid]
    assert row['resolution'] == 'rule_changed:synthetic_group@1' and row['resolved_revision'] == 2
    with s.pool.connection() as c:
        assert c.execute('SELECT sender,revision FROM messages WHERE id=%s', (mid,)).fetchone() == {'sender': '42', 'revision': 2}


# spec: ANYM-621
def test_a_telegram_link_preview_refresh_conflict_an_older_rule_raised_is_no_longer_raised(archive):  # noqa: F811
    """Rebase on ANYM-611: the deployed N-1 ingest raised Telegram's re-crawl of the same preview page; ingest_outcome
    now treats it as unchanged at the retained version, so a rule job may close the row, and another page stays open."""
    from test_archive import PREVIEW_DOCUMENT, preview
    ar, s, _, _ = archive
    old = n1_archive(s)
    refreshed = old.ingest(preview(provider_id='1'))['message']
    assert old.ingest(preview(provider_id='1', media=PREVIEW_DOCUMENT, origin='reconcile'))['outcome'] == 'conflict'
    other = old.ingest(preview(provider_id='2'))['message']
    assert old.ingest(preview(page='21', provider_id='2', media=PREVIEW_DOCUMENT, origin='reconcile'))['outcome'] == 'conflict'
    job = Job('synthetic_preview', 1, 'resolve', lambda row: 'resolve', classes=('telegram_*',))
    assert run(ar, s, [job]) == 'done'
    rows = conflicts(s)
    assert rows[refreshed]['resolution'] == 'rule_changed:synthetic_preview@1' and rows[other]['resolved_at'] is None
    assert progress(s)['synthetic_preview']['last_error'] == 'rule_still_raises'
