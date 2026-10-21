"""Agent-visible feedback disposition that only the local operator sets (ANYM-343)."""
from contextlib import contextmanager
from copy import copy
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from messaging_service import __main__ as operator
from messaging_service import store as store_module
from messaging_service.backup import create_snapshot, database_manifest, verify_bundle, verify_snapshot
from messaging_service.policy import AUDIT_OPERATIONS, OPERATION_PERMISSIONS
from messaging_service.store import APIError, Store
from test_event_delivery import call, identity

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(store_module.__file__).parent
MERGED = 'deedkit@' + 'a' * 40
DEPLOYED = 'deedkit@' + 'b' * 40


def submit(store, token, n):
    return call(store, token, 'submit_feedback', 'synthetic-feedback-%d' % n, 'bug', 'Synthetic report %d' % n)['feedback_id']


def listed(store, token, **arguments):
    return {item['id']: item for item in call(store, token, 'list_feedback', **arguments)['items']}


def feedback_rows(store):
    with store.pool.connection() as c:
        return c.execute('SELECT * FROM agent_feedback ORDER BY id').fetchall()


def status_audit(store):
    with store.pool.connection() as c:
        return c.execute("SELECT * FROM audit_log WHERE operation='set_feedback_status' ORDER BY id").fetchall()


@pytest.fixture
def schema38_store(database, tmp_path):
    """An independent database built from the unchanged schema-38 SQL and checksums."""
    name = 'anymsg_feedback_' + uuid4().hex + '_test'
    dsn = database.pool.conninfo
    old = None
    with psycopg.connect(dsn, autocommit=True) as owner:
        owner.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        files = tmp_path / 'originals'
        files.mkdir()
        old = Store(make_conninfo(dsn, dbname=name), files, 'http://testserver')
        with old.pool.connection() as c:
            c.execute(SOURCE.joinpath('schema.sql').read_text())
            for migration in sorted(SOURCE.joinpath('migrations').glob('*.sql')):
                version = int(migration.name[:3])
                if version > 38:
                    break
                c.execute(migration.read_text())
                if version == 17:
                    from messaging_service.chat_evidence import backfill
                    backfill(c)
                c.execute('INSERT INTO archive_migrations(version,sha256) VALUES(%s,%s)',
                    (version, hashlib.sha256(migration.read_bytes()).hexdigest()))
                c.execute("UPDATE probe_metadata SET value=%s WHERE name='schema_version'", (str(version),))
        old.seed()
        yield old
    finally:
        if old is not None:
            old.close()
        with psycopg.connect(dsn, autocommit=True) as owner:
            owner.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


# spec: ANYM-343
def test_feedback_status_migration(schema38_store):
    store = schema38_store
    token = identity(store)
    old = submit(store, token, 1)
    with store.pool.connection() as c:
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '38'
        migrations = c.execute('SELECT * FROM archive_migrations ORDER BY version').fetchall()
    store.migrate()
    store.migrate()
    with store.pool.connection() as c:
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '39'
        assert c.execute('SELECT * FROM archive_migrations WHERE version<=38 ORDER BY version').fetchall() == migrations
        assert c.execute('SELECT sha256 FROM archive_migrations WHERE version=39').fetchone()['sha256'] == \
            hashlib.sha256(SOURCE.joinpath('migrations/039_feedback_status.sql').read_bytes()).hexdigest()
    row, = feedback_rows(store)
    assert str(row['id']) == old and row['status'] == 'received'
    assert all(row[name] is None for name in ('status_note', 'fixed_in', 'spec_ref', 'duplicate_of', 'status_changed_at'))
    assert listed(store, token)[old]['status'] == 'received'
    with store.pool.connection() as c:
        c.execute("UPDATE archive_migrations SET sha256='wrong' WHERE version=39")
    with pytest.raises(RuntimeError, match='migration_checksum_mismatch'):
        store.migrate()


# spec: ANYM-343
def test_feedback_status_backup_round_trip(seeded, tmp_path):
    store, *_ = seeded
    with store.pool.connection() as c:
        c.execute("UPDATE attachment_residency SET state='resident',eviction_id=NULL,evicted_at=NULL")
        c.execute('DELETE FROM attachment_eviction_journal')
        c.execute('DELETE FROM attachment_backup_fences')
    token = identity(store)
    first, second = submit(store, token, 1), submit(store, token, 2)
    store.set_feedback_status([first], 'verify_requested', spec_ref='ANYM-274', fixed_in=DEPLOYED, note='Please re-check')
    store.set_feedback_status([second], 'duplicate', duplicate_of=first)
    # Unit stub only; real pg_dump/pg_restore run in the deployment restore drill.
    create_snapshot(store, tmp_path / 'snapshot', [sys.executable, '-c', 'print("synthetic dump")'])
    manifest = verify_bundle(tmp_path / 'snapshot')
    assert manifest['schema_version'] == 39 and manifest['tables']['agent_feedback']['rows'] == 2
    assert verify_snapshot(store, tmp_path / 'snapshot')['status'] == 'verified'
    with store.pool.connection() as c:
        before = database_manifest(c)['agent_feedback']
    store.set_feedback_status([second], 'declined')
    with store.pool.connection() as c:
        assert database_manifest(c)['agent_feedback'] != before
    # The manifest covers the disposition columns, so a restore that lost them is refused.
    with pytest.raises(RuntimeError, match='database_mismatch'):
        verify_snapshot(store, tmp_path / 'snapshot')


# spec: ANYM-343
def test_feedback_status_visible_to_owner_key(seeded):
    store, *_ = seeded
    token = identity(store)
    first, second, untouched = submit(store, token, 1), submit(store, token, 2), submit(store, token, 3)
    result = store.set_feedback_status([first, second.upper()], 'accepted', spec_ref='ANYM-274')
    assert result == {'updated': 2, 'status': 'accepted', 'feedback_ids': sorted([first, second])}
    items = listed(store, token)
    for fid in (first, second):
        assert items[fid]['status'] == 'accepted' and items[fid]['spec_ref'] == 'ANYM-274'
        assert items[fid]['status_changed_at'] and items[fid]['fixed_in'] is None and items[fid]['status_note'] is None
        assert 'refs' not in items[fid]
    assert items[untouched]['status'] == 'received' and items[untouched]['status_changed_at'] is None
    store.set_feedback_status([first], 'fixed', spec_ref='ANYM-274', fixed_in=MERGED)
    store.set_feedback_status([first], 'verify_requested', spec_ref='ANYM-274', fixed_in=DEPLOYED,
        note='Deployed; please re-check')
    item = listed(store, token)[first]
    assert (item['status'], item['fixed_in'], item['status_note']) == ('verify_requested', DEPLOYED, 'Deployed; please re-check')
    assert list(listed(store, token)) == [item['id'] for item in call(store, token, 'list_feedback')['items']]


# spec: ANYM-343
def test_feedback_status_hidden_from_other_keys_and_principals(seeded):
    store, *_ = seeded
    owner = identity(store)
    fid = submit(store, owner, 1)
    store.set_feedback_status([fid], 'accepted', spec_ref='ANYM-274', note='Planned')
    for other in (identity(store), identity(store, 'event-other')):
        assert fid not in listed(store, other)
        assert listed(store, other, status='accepted') == {}
        submit(store, other, 1)
        assert all(item['status'] == 'received' for item in listed(store, other).values())
    assert listed(store, owner, status='accepted')[fid]['status_note'] == 'Planned'


# spec: ANYM-343
def test_set_feedback_status_validation(seeded):
    store, *_ = seeded
    token = identity(store)
    first, second = submit(store, token, 1), submit(store, token, 2)
    foreign = submit(store, identity(store), 9)
    unknown = str(uuid4())
    before = feedback_rows(store)
    refused = [
        (([first], 'verified'), {}, 'status_reserved_for_reporting_agent'),
        (([first], 'reopened'), {}, 'status_reserved_for_reporting_agent'),
        (([first], 'planned'), {}, 'invalid_status'),
        (([first], 'accepted'), {}, 'spec_ref_required'),
        (([first], 'fixed'), {'spec_ref': 'ANYM-274'}, 'fixed_in_required'),
        (([first], 'verify_requested'), {'spec_ref': 'ANYM-274'}, 'fixed_in_required'),
        (([first], 'fixed'), {'fixed_in': MERGED}, 'spec_ref_required'),
        (([first], 'duplicate'), {}, 'duplicate_of_or_spec_ref_required'),
        (([first], 'accepted'), {'spec_ref': 'ANYM-0'}, 'invalid_spec_ref'),
        (([first], 'accepted'), {'spec_ref': 'https://tiden.example/ANYM-274'}, 'invalid_spec_ref'),
        (([first], 'fixed'), {'spec_ref': 'ANYM-274', 'fixed_in': 'deedkit@abc123'}, 'invalid_fixed_in'),
        (([first], 'fixed'), {'spec_ref': 'ANYM-274', 'fixed_in': 'main'}, 'invalid_fixed_in'),
        (([first], 'accepted'), {'spec_ref': 'ANYM-274', 'fixed_in': MERGED}, 'fixed_in_only_for_fixed_or_verify_requested'),
        (([first], 'duplicate'), {'duplicate_of': 'not-a-feedback-id'}, 'invalid_duplicate_of'),
        (([first], 'declined'), {'duplicate_of': second}, 'duplicate_of_only_for_duplicate'),
        (([first], 'declined'), {'note': 'x' * 281}, 'invalid_status_note'),
        (([first], 'declined'), {'note': 'line\nbreak'}, 'invalid_status_note'),
        (([first], 'declined'), {'note': 'bell\x07'}, 'invalid_status_note'),
        (([first], 'declined'), {'note': ''}, 'invalid_status_note'),
        (([], 'declined'), {}, 'feedback_ids_1_to_100'),
        (([str(uuid4()) for _ in range(101)], 'declined'), {}, 'feedback_ids_1_to_100'),
        ((['not-a-uuid'], 'declined'), {}, 'invalid_feedback_id'),
        (([first], 'duplicate'), {'duplicate_of': first}, 'duplicate_of_names_an_updated_item'),
        (([first], 'duplicate'), {'duplicate_of': foreign}, 'duplicate_of_not_same_key'),
        (([first, foreign], 'duplicate'), {'duplicate_of': second}, 'duplicate_of_not_same_key'),
    ]
    for arguments, options, code in refused:
        with pytest.raises(ValueError) as failure:
            store.set_feedback_status(*arguments, **options)
        assert code in str(failure.value).split(','), (arguments[1], options, str(failure.value))
    with pytest.raises(ValueError) as failure:
        store.set_feedback_status([first, unknown, second], 'declined')
    assert str(failure.value) == 'unknown_feedback: 1 of 3 IDs' and unknown not in str(failure.value)
    assert feedback_rows(store) == before and status_audit(store) == []
    assert store.set_feedback_status([second], 'duplicate', duplicate_of=first)['updated'] == 1
    assert store.set_feedback_status([first], 'duplicate', spec_ref='ANYM-274')['updated'] == 1


# spec: ANYM-343
def test_feedback_status_privileges(seeded, schema38_store):
    store, *_ = seeded
    role = 'test_feedback_gateway_' + uuid4().hex[:8]
    ddl = SOURCE.joinpath('gateway_role.sql').read_text().replace('messaging_gateway', role)
    with store.pool.connection() as c:
        c.execute(ddl)
    class RestrictedPool:
        @contextmanager
        def connection(self):
            with store.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                yield c
    gateway = copy(store)
    gateway.pool = RestrictedPool()
    try:
        token = identity(store)
        fid = submit(gateway, token, 1)
        assert listed(gateway, token)[fid]['status'] == 'received'
        key = store.capabilities(token)['credential_id']
        insert = '''INSERT INTO agent_feedback(principal,key_id,request_id,request_hash,category,description,refs,{})
            VALUES('event-reader',%s,'synthetic-maintainer',repeat('a',64),'bug','Synthetic','{{}}',%s)'''
        for column, value in (('status', 'fixed'), ('fixed_in', MERGED), ('spec_ref', 'ANYM-274'),
                              ('status_note', 'Synthetic'), ('duplicate_of', fid)):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with gateway.pool.connection() as c:
                    c.execute(insert.format(column), (key, value))
        for statement in ("UPDATE agent_feedback SET status='fixed'", "UPDATE agent_feedback SET description='x'",
                          'DELETE FROM agent_feedback'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with gateway.pool.connection() as c:
                    c.execute(statement)
        assert feedback_rows(store)[0]['status'] == 'received'
    finally:
        with store.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
    # Upgraded database: migration 039 narrows an existing gateway login's schema-38 table-wide INSERT.
    old = schema38_store
    with old.pool.connection() as c:
        created = c.execute('''SELECT NOT EXISTS(SELECT FROM pg_roles WHERE rolname='messaging_gateway') AS created''').fetchone()['created']
        if created:
            c.execute('CREATE ROLE messaging_gateway NOLOGIN')
        c.execute('GRANT SELECT,INSERT ON agent_feedback TO messaging_gateway')
    try:
        old.migrate()
        with old.pool.connection() as c:
            def privilege(query, *args):
                return c.execute(query, args).fetchone()['allowed']
            table = "SELECT has_table_privilege('messaging_gateway','agent_feedback',%s) AS allowed"
            column = "SELECT has_column_privilege('messaging_gateway','agent_feedback',%s,'INSERT') AS allowed"
            assert privilege(table, 'SELECT') and not privilege(table, 'INSERT') and not privilege(table, 'UPDATE')
            assert all(privilege(column, name) for name in ('principal', 'key_id', 'request_id', 'request_hash',
                'category', 'description', 'operation', 'refs'))
            assert not any(privilege(column, name) for name in ('status', 'status_note', 'fixed_in', 'spec_ref',
                'duplicate_of', 'status_changed_at', 'id', 'created_at'))
    finally:
        with old.pool.connection() as c:
            c.execute('DROP OWNED BY messaging_gateway')
        if created:
            with psycopg.connect(store.pool.conninfo, autocommit=True) as owner:
                owner.execute('DROP ROLE messaging_gateway')
    # No agent key reaches the command: it is no operation, catalog tool or contract tool.
    assert 'set_feedback_status' not in OPERATION_PERMISSIONS and 'set_feedback_status' in AUDIT_OPERATIONS
    catalog = json.loads((ROOT / 'services/gateway/src/agent_gateway/catalog.json').read_text())
    contracts = json.loads((ROOT / 'tests/platform/fixtures/messaging-contracts.json').read_text())
    assert not any('feedback_status' in tool['name'] or tool.get('operation') == 'set_feedback_status' for tool in catalog)
    assert not any('feedback_status' in tool['name'] for tool in contracts['tools'])


# spec: ANYM-343
def test_set_feedback_status_writes_one_operator_audit_row_per_item(seeded):
    store, *_ = seeded
    token = identity(store)
    key = store.capabilities(token)['credential_id']
    first, second = submit(store, token, 1), submit(store, token, 2)
    note = 'Synthetic private maintainer note'
    store.set_feedback_status([first, second], 'declined', note=note)
    rows = status_audit(store)
    assert sorted(row['resource_ids'][0] for row in rows) == sorted([first, second])
    for row in rows:
        assert (row['actor'], row['outcome'], row['principal'], str(row['key_id']), row['error_code']) == \
            ('operator', 'ok', 'event-reader', key, None)
        assert len(row['resource_ids']) == 1 and note not in json.dumps(row, default=str)


# spec: ANYM-343
def test_list_feedback_status_filter(seeded):
    store, *_ = seeded
    token = identity(store)
    first, second, third = submit(store, token, 1), submit(store, token, 2), submit(store, token, 3)
    store.set_feedback_status([first, third], 'verify_requested', spec_ref='ANYM-274', fixed_in=DEPLOYED)
    page = call(store, token, 'list_feedback', status='verify_requested')
    assert [item['id'] for item in page['items']] == [third, first] and not page['has_more']
    assert list(listed(store, token, status='received')) == [second]
    assert [item['id'] for item in call(store, token, 'list_feedback', limit=1, status='verify_requested')['items']] == [third]
    with pytest.raises(APIError, match='invalid_feedback') as failure:
        call(store, token, 'list_feedback', status='planned')
    assert failure.value.field_errors[0]['field'] == 'status' and failure.value.field_errors[0]['reason'] == 'enum'
    assert 'verify_requested' in failure.value.field_errors[0]['allowed']


class RecordingStore:
    calls = []
    def __init__(self, *args): pass
    def close(self): self.calls.append(('close',))
    def set_feedback_status(self, ids, status, **options):
        self.calls.append(('set_feedback_status', ids, status, options))
        return {'updated': len(ids), 'status': status, 'feedback_ids': ids}


@pytest.fixture
def cli(monkeypatch, tmp_path):
    RecordingStore.calls = []
    monkeypatch.setattr(operator, 'Store', RecordingStore)
    dsn = tmp_path / 'synthetic.dsn'
    dsn.write_text('postgresql://synthetic.invalid/messaging_probe_test')
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn))
    def invoke(*arguments):
        monkeypatch.setattr(sys, 'argv', ['anymsg', *arguments])
        operator.main()
    return invoke, RecordingStore.calls


# spec: ANYM-343
def test_cli_set_feedback_status_forwards_ids_and_fields(cli, capsys):
    invoke, calls = cli
    ids = ['00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000002']
    invoke('set-feedback-status', '--id', *ids, '--status', 'fixed', '--spec', 'ANYM-274', '--fixed-in', MERGED,
           '--note', 'Merged')
    assert calls == [('set_feedback_status', ids, 'fixed', {'fixed_in': MERGED, 'spec_ref': 'ANYM-274',
        'duplicate_of': None, 'note': 'Merged'}), ('close',)]
    assert json.loads(capsys.readouterr().out) == {'updated': 2, 'status': 'fixed', 'feedback_ids': ids}
    for arguments in (['set-feedback-status', '--status', 'declined'], ['set-feedback-status', '--id', ids[0]]):
        with pytest.raises(SystemExit) as failure:
            invoke(*arguments)
        assert failure.value.code == 2
    assert [call[0] for call in calls] == ['set_feedback_status', 'close', 'close', 'close']
