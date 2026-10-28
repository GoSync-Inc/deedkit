"""`migrate-expand` (ANYM-596): DROP-free expand migrations and appended role grants in one transaction under
2 s lock, 4 s statement and 4 s transaction budgets, or nothing. Real PostgreSQL, one disposable database per test,
synthetic SQL only."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

from messaging_service import store as store_module
from messaging_service.store import ExpandError, RUNTIME_SCHEMA, Store

SOURCE = Path(store_module.__file__).parent
NEXT = RUNTIME_SCHEMA + 1
NOTE = '-- rollout: expand\nALTER TABLE messages ADD COLUMN synthetic_note text;\n'


@pytest.fixture(scope='module')
def template(database):
    """A database at the runtime schema; each test gets a copy."""
    name = 'anymsg_expand_tpl_' + uuid4().hex
    dsn = database.pool.conninfo
    with psycopg.connect(dsn, autocommit=True) as owner:
        owner.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        built = Store(make_conninfo(dsn, dbname=name), Path('/nonexistent-probe-files'), 'http://127.0.0.1:1')
        try: built.migrate()
        finally: built.close()
        yield dsn, name
    finally:
        with psycopg.connect(dsn, autocommit=True) as owner:
            owner.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


@pytest.fixture
def db(template, tmp_path):
    dsn, source = template
    name = 'anymsg_expand_' + uuid4().hex + '_test'
    with psycopg.connect(dsn, autocommit=True) as owner:
        owner.execute(sql.SQL('CREATE DATABASE {} TEMPLATE {}').format(sql.Identifier(name), sql.Identifier(source)))
    target = make_conninfo(dsn, dbname=name)
    store = Store(target, tmp_path / 'files', 'http://127.0.0.1:1')
    try:
        yield store, target
    finally:
        store.close()
        with psycopg.connect(dsn, autocommit=True) as owner:
            owner.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def plan_for(root, base, target, appended=None):
    """The plan deploy would write: every role file's prefix before `appended[name]`."""
    roles = {}
    for path in sorted(Path(root).glob('*_role.sql')):
        data = path.read_bytes()
        prefix = data[:len(data) - len((appended or {}).get(path.name, '').encode())]
        roles[path.name] = {'bytes': len(prefix), 'sha256': hashlib.sha256(prefix).hexdigest()}
    return {'base': base, 'target': target, 'roles': roles}


def package(tmp_path, migration=NOTE, roles=None, *, slow=False):
    """A copy of the messaging package (the image) plus migration NEXT and appended role text; `slow` makes the
    copy's migrate_expand sleep 5 s before its commit. The CLI runs it through PYTHONPATH."""
    root = tmp_path / 'image' / 'messaging_service'
    shutil.copytree(SOURCE, root, ignore=shutil.ignore_patterns('__pycache__'))
    root.joinpath('migrations', f'{NEXT:03d}_synthetic.sql').write_text(migration)
    for name, text in (roles or {}).items():
        with root.joinpath(name).open('a') as role: role.write(text)
    if slow:
        with root.joinpath('store.py').open('a') as store:
            store.write('\n_synthetic_original = migrate_expand\n\n\n'
                        'def migrate_expand(dsn, plan, *, root=None, before_commit=None):\n'
                        "    return _synthetic_original(dsn, plan, root=root, before_commit=lambda c: c.execute('SELECT pg_sleep(5)'))\n")
    return root


def ledger(dsn):
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        version = c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']
        rows = c.execute('SELECT count(*) AS n FROM archive_migrations WHERE version>%s', (RUNTIME_SCHEMA,)).fetchone()
        column = c.execute("SELECT count(*) AS n FROM information_schema.columns WHERE table_name='messages' "
                           "AND column_name='synthetic_note'").fetchone()
        return int(version), rows['n'], column['n']


def cli(dsn, tmp_path, *args, root=None):
    """The real entry point; returns (exit code, its one JSON line, seconds). PostgreSQL's text never reaches
    stdout or stderr: stderr must stay empty."""
    tmp_path.joinpath('dsn').write_text(dsn)
    env = dict(os.environ, DATABASE_URL_FILE=str(tmp_path / 'dsn'), PROBE_FILES=str(tmp_path / 'files'))
    if root: env['PYTHONPATH'] = str(root.parent)
    started = time.monotonic()
    done = subprocess.run([sys.executable, '-m', 'messaging_service', 'migrate-expand', *args], env=env,
                          capture_output=True, text=True, timeout=60)
    lines = done.stdout.splitlines()
    assert len(lines) == 1 and done.stderr == '', done.stdout + done.stderr
    return done.returncode, json.loads(lines[0]), time.monotonic() - started


def acls(dsn):
    """Every relation's ACL in public and pg_catalog, and the public relations."""
    with psycopg.connect(dsn) as c:
        return c.execute("SELECT n.nspname, c.relname, c.relkind, c.relacl::text FROM pg_class c "
                         "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('public','pg_catalog') "
                         "ORDER BY 1, 2").fetchall()


def write_plan(tmp_path, plan):
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps(plan))
    return str(path)


# spec: ANYM-596
def test_the_entry_point_reads_the_ledger_and_runs_an_empty_plan(db, tmp_path):
    _, dsn = db
    assert cli(dsn, tmp_path, '--status')[:2] == (0, {'schema_version': RUNTIME_SCHEMA})
    plan = plan_for(SOURCE, RUNTIME_SCHEMA, RUNTIME_SCHEMA)
    assert cli(dsn, tmp_path, '--plan', write_plan(tmp_path, plan))[:2] == (0, {'schema_version': RUNTIME_SCHEMA})


# spec: ANYM-596
def test_the_entry_point_opens_one_connection_under_the_budgets_and_no_pool(db, tmp_path, monkeypatch):
    # Astra r1 F5: no application pool beside the bounded connection.
    from messaging_service import __main__ as cli_module
    _, dsn = db
    tmp_path.joinpath('dsn').write_text(dsn)
    opened, connect = [], psycopg.connect

    def counted(*args, **kwargs):
        opened.append(kwargs.get('options', ''))
        return connect(*args, **kwargs)

    def no_store(*args, **kwargs): raise AssertionError('migrate-expand built the application Store')
    monkeypatch.setattr(store_module.psycopg, 'connect', counted)
    monkeypatch.setattr(cli_module, 'Store', no_store)
    monkeypatch.setattr(store_module, 'ConnectionPool', no_store)
    monkeypatch.setenv('DATABASE_URL_FILE', str(tmp_path / 'dsn'))
    plan = write_plan(tmp_path, plan_for(SOURCE, RUNTIME_SCHEMA, RUNTIME_SCHEMA))
    monkeypatch.setattr(sys, 'argv', ['messaging_service', 'migrate-expand', '--plan', plan])
    assert cli_module.main() == 0
    assert len(opened) == 1
    assert all(f'-c {name}=' in opened[0] for name in ('lock_timeout', 'statement_timeout', 'transaction_timeout'))


# spec: ANYM-596
def test_a_held_migration_lock_returns_busy_at_once_and_changes_nothing(db, tmp_path):
    _, dsn = db
    plan = write_plan(tmp_path, plan_for(SOURCE, RUNTIME_SCHEMA, RUNTIME_SCHEMA))
    baseline = cli(dsn, tmp_path, '--status')[2]  # interpreter start and connect, without any lock
    with psycopg.connect(dsn) as holder:
        holder.execute('SELECT pg_advisory_lock(74509137)')
        code, out, elapsed = cli(dsn, tmp_path, '--plan', plan)
    assert (code, out) == (1, {'error': 'busy'})
    assert elapsed < baseline + 1
    assert ledger(dsn) == (RUNTIME_SCHEMA, 0, 0)


# spec: ANYM-596
def test_a_ledger_at_another_version_or_checksum_refuses(db, tmp_path):
    _, dsn = db
    stale = write_plan(tmp_path, plan_for(SOURCE, RUNTIME_SCHEMA - 1, RUNTIME_SCHEMA))
    assert cli(dsn, tmp_path, '--plan', stale)[:2] == (1, {'error': 'ledger_mismatch'})
    with psycopg.connect(dsn) as c:
        c.execute("UPDATE archive_migrations SET sha256=repeat('0',64) WHERE version=%s", (RUNTIME_SCHEMA,))
    plan = write_plan(tmp_path, plan_for(SOURCE, RUNTIME_SCHEMA, RUNTIME_SCHEMA))
    assert cli(dsn, tmp_path, '--plan', plan)[:2] == (1, {'error': 'ledger_mismatch'})


# spec: ANYM-596
def test_a_plan_whose_role_prefix_hash_differs_refuses(db, tmp_path):
    _, dsn = db
    plan = plan_for(SOURCE, RUNTIME_SCHEMA, RUNTIME_SCHEMA)
    plan['roles']['gateway_role.sql']['sha256'] = hashlib.sha256(b'synthetic').hexdigest()
    assert cli(dsn, tmp_path, '--plan', write_plan(tmp_path, plan))[:2] == (1, {'error': 'prefix_mismatch'})
    del plan['roles']['gateway_role.sql']
    assert cli(dsn, tmp_path, '--plan', write_plan(tmp_path, plan))[:2] == (1, {'error': 'plan_invalid'})


# spec: ANYM-596
def test_an_expand_migration_and_its_role_grant_apply_together(db, tmp_path):
    _, dsn = db
    role = 'synthetic_expand_' + uuid4().hex[:12]
    migration = '-- rollout: expand\nCREATE TABLE synthetic_marks (id text PRIMARY KEY);\n' + NOTE.split('\n', 1)[1]
    grant = f'-- synthetic\nGRANT SELECT(id) ON synthetic_marks TO {role};\n'
    root = package(tmp_path, migration)
    root.joinpath('synthetic_role.sql').write_text(f'GRANT SELECT ON messages TO {role};\n' + grant)
    with psycopg.connect(dsn, autocommit=True) as c: c.execute(sql.SQL('CREATE ROLE {}').format(sql.Identifier(role)))
    try:
        plan = write_plan(tmp_path, plan_for(root, RUNTIME_SCHEMA, NEXT, {'synthetic_role.sql': grant}))
        assert cli(dsn, tmp_path, '--plan', plan, root=root)[:2] == (0, {'schema_version': NEXT})
        assert ledger(dsn) == (NEXT, 1, 1)
        with psycopg.connect(dsn) as c:
            assert c.execute('SELECT has_column_privilege(%s, %s, %s, %s)',
                             (role, 'synthetic_marks', 'id', 'SELECT')).fetchone()[0]
    finally:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


# spec: ANYM-596
@pytest.mark.parametrize(('migration', 'roles', 'code'), [
    ('-- rollout: expand\nALTER TABLE messages DROP COLUMN body;\n', {}, 'not_unattended'),
    ('-- rollout: manual\n' + NOTE.split('\n', 1)[1], {}, 'not_unattended'),
    (NOTE, {'gateway_role.sql': 'REVOKE SELECT ON messages FROM messaging_gateway;\n'}, 'role_not_additive'),
    (NOTE, {'gateway_role.sql': 'GRANT UPDATE ON synthetic_seq TO messaging_gateway;\n'}, 'role_not_additive'),
    # Sol r2 F1, F2: a "new" table named like an existing sequence, or like a catalog the GRANT would resolve to.
    ('-- rollout: expand\nCREATE TABLE IF NOT EXISTS synthetic_seq (id text);\n',
     {'gateway_role.sql': 'GRANT UPDATE ON synthetic_seq TO messaging_gateway;\n'}, 'not_unattended'),
    ('-- rollout: expand\nCREATE TABLE pg_authid (id text);\n',
     {'gateway_role.sql': 'GRANT SELECT ON pg_authid TO messaging_gateway;\n'}, 'not_unattended'),
    (NOTE, {'gateway_role.sql': 'GRANT SELECT ON pg_authid TO messaging_gateway;\n'}, 'role_not_additive'),
], ids=['drop', 'declared-manual', 'revoke', 'not-a-table', 'sequence-collision', 'catalog-collision', 'catalog'])
def test_anything_outside_the_unattended_language_applies_nothing(db, tmp_path, migration, roles, code):
    _, dsn = db
    with psycopg.connect(dsn) as c: c.execute('CREATE SEQUENCE synthetic_seq')
    root = package(tmp_path, migration, roles)
    plan = write_plan(tmp_path, plan_for(root, RUNTIME_SCHEMA, NEXT, roles))
    before = acls(dsn)
    assert cli(dsn, tmp_path, '--plan', plan, root=root)[:2] == (1, {'error': code})
    assert ledger(dsn) == (RUNTIME_SCHEMA, 0, 0)
    assert acls(dsn) == before


# spec: ANYM-596
def test_a_held_table_lock_fails_lock_timeout_while_an_old_runtime_insert_finishes(db, tmp_path):
    _, dsn = db
    root = package(tmp_path)
    plan = write_plan(tmp_path, plan_for(root, RUNTIME_SCHEMA, NEXT))
    with psycopg.connect(dsn) as c:
        c.execute("INSERT INTO accounts(id,platform,label) VALUES ('synthetic','telegram','synthetic')")
        c.execute("INSERT INTO chats(id,account,title) VALUES ('synthetic-chat','synthetic','synthetic')")
    holder = psycopg.connect(dsn)
    holder.execute('SELECT count(*) FROM messages')  # an open reader holds ACCESS SHARE
    outcome, insert = {}, {}

    def migrate(): outcome['result'] = cli(dsn, tmp_path, '--plan', plan, root=root)[:2]

    def write():
        # The old runtime's insert queues behind the migration's ACCESS EXCLUSIVE request, under its 5 s budget.
        started = time.monotonic()
        with psycopg.connect(dsn, options='-c statement_timeout=5000') as c:
            c.execute("INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at) "
                      "VALUES ('synthetic-m','synthetic','synthetic-chat','p1','s','synthetic',now())")
        insert['elapsed'] = time.monotonic() - started
    try:
        runner = threading.Thread(target=migrate)
        runner.start()
        deadline = time.monotonic() + 30
        with psycopg.connect(dsn, autocommit=True) as watch:
            while not watch.execute("SELECT count(*) FROM pg_locks WHERE NOT granted AND relation='messages'::regclass"
                                    ).fetchone()[0] and time.monotonic() < deadline: time.sleep(0.02)
        writer = threading.Thread(target=write)
        writer.start()
        runner.join(60), writer.join(10)
    finally: holder.close()
    assert outcome == {'result': (1, {'error': 'lock_timeout'})}
    assert insert['elapsed'] < 5
    assert ledger(dsn) == (RUNTIME_SCHEMA, 0, 0)


# spec: ANYM-596
def test_a_slow_statement_ends_at_the_transaction_budget_with_nothing_applied(db, tmp_path):
    _, dsn = db
    root = package(tmp_path, slow=True)
    plan = write_plan(tmp_path, plan_for(root, RUNTIME_SCHEMA, NEXT))
    baseline = cli(dsn, tmp_path, '--status', root=root)[2]
    code, out, elapsed = cli(dsn, tmp_path, '--plan', plan, root=root)
    assert (code, out) == (1, {'error': 'transaction_timeout'})
    assert elapsed < baseline + 5
    assert ledger(dsn) == (RUNTIME_SCHEMA, 0, 0)


# spec: ANYM-596
def test_a_failing_role_statement_after_a_good_file_leaves_neither(db, tmp_path):
    # The role exists in the base text but not in this cluster: PostgreSQL refuses the GRANT and names the role,
    # and that name must not reach stdout or stderr.
    _, dsn = db
    absent = 'synthetic_absent_' + uuid4().hex[:12]
    grant = f'GRANT SELECT ON messages TO {absent};\n'
    root = package(tmp_path, NOTE)
    root.joinpath('synthetic_role.sql').write_text(f'GRANT SELECT ON chats TO {absent};\n' + grant)
    plan = write_plan(tmp_path, plan_for(root, RUNTIME_SCHEMA, NEXT, {'synthetic_role.sql': grant}))
    assert cli(dsn, tmp_path, '--plan', plan, root=root)[:2] == (1, {'error': 'failed'})
    assert ledger(dsn) == (RUNTIME_SCHEMA, 0, 0)


# spec: ANYM-596
def test_the_session_budgets_are_read_back_before_anything_runs(db, monkeypatch):
    store, _ = db
    monkeypatch.setitem(store_module.EXPAND_SETTINGS, 'transaction_timeout', '5s')
    with pytest.raises(ExpandError) as refused: store.expand_status()
    assert refused.value.code == 'settings_refused'
