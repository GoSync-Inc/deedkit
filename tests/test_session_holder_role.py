"""ANYM-936: the session holder database role reaches only its own account's claim, lease and status rows."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
import pytest

from messaging_service import backup, backup_restore_worker as restore_worker, session_claim as sc
from messaging_service import store as store_module
from messaging_service.archive import Archive

SOURCE = Path(store_module.__file__).parent
FORBIDDEN = psycopg.errors.InsufficientPrivilege
MINE, OTHER = 'telegram-demo', 'whatsapp-demo-1'


def group(store):
    """The real NOLOGIN group from the role file (cluster-wide; the template is idempotent)."""
    with store.pool.connection() as c:
        c.execute(SOURCE.joinpath('session_holder_role.sql').read_text())


def login(store, account=None, *, password=None):
    role = 'anym936_holder_' + uuid4().hex[:10]
    with store.pool.connection() as c:
        c.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS IN ROLE session_holder{}').format(
            sql.Identifier(role), sql.SQL(' PASSWORD {}').format(sql.Literal(password)) if password else sql.SQL('')))
        if account:
            c.execute('INSERT INTO session_holder_accounts(role_name,account) VALUES(%s,%s)', (role, account))
    return role


def drop(store, role):
    with store.pool.connection() as c:
        c.execute('DELETE FROM session_holder_accounts WHERE role_name=%s', (role,))
        c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
        c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


@contextmanager
def as_role(store, role):
    with store.pool.connection() as c:
        c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
        yield c


class RolePool:
    def __init__(self, store, role):
        self.store, self.role = store, role

    def connection(self, **_):  # session_claim.release passes a pool timeout
        return as_role(self.store, self.role)


def refused(store, role, statement, params=()):
    with pytest.raises(FORBIDDEN):
        with as_role(store, role) as c:
            c.execute(statement, params)


def seed_rows(store):
    """A claim, a lease and a status row for both accounts, written by the owner."""
    with store.pool.connection() as c:
        for account in (MINE, OTHER):
            c.execute('''INSERT INTO session_claims(account,host_id,boot_id,lock_ref,holder_id,holder_kind,generation,
                claimed_at,seen_at,released_at,released_by) VALUES(%s,%s,'boot','1:2',%s,'collector',1,
                clock_timestamp(),clock_timestamp(),clock_timestamp(),'holder') ON CONFLICT(account) DO NOTHING''',
                (account, '1' * 32, '2' * 32))
            c.execute('''INSERT INTO collector_leases(account,owner,epoch,expires_at)
                VALUES(%s,'collector-owner',3,clock_timestamp()+interval '60 seconds') ON CONFLICT(account) DO NOTHING''',
                (account,))
            c.execute('INSERT INTO source_status(account) VALUES(%s) ON CONFLICT DO NOTHING', (account,))


@pytest.fixture()
def holders(seeded):
    store, _, _ = seeded
    group(store)
    seed_rows(store)
    made = []

    def make(account=None, **kwargs):
        made.append(login(store, account, **kwargs))
        return made[-1]
    yield store, make
    for role in made:
        drop(store, role)


# spec: ANYM-936
def test_a_holder_login_sees_and_changes_only_its_own_accounts_rows(holders):
    store, make = holders
    mine = make(MINE)
    with as_role(store, mine) as c:
        assert [r['account'] for r in c.execute('SELECT account FROM session_claims')] == [MINE]
        assert [r['account'] for r in c.execute('SELECT account,owner,epoch,expires_at FROM collector_leases')] == [MINE]
        assert [r['account'] for r in c.execute('SELECT account,status FROM source_status')] == [MINE]
        # Another account's claim is invisible to UPDATE: nothing changes.
        assert c.execute("UPDATE session_claims SET seen_at=clock_timestamp() WHERE account=%s", (OTHER,)).rowcount == 0
        assert c.execute("UPDATE session_claims SET seen_at=clock_timestamp() WHERE account=%s", (MINE,)).rowcount == 1
    # Moving its own row to another account, or inserting one, fails the policy's check.
    refused(store, mine, 'UPDATE session_claims SET account=%s WHERE account=%s', (OTHER, MINE))
    with store.pool.connection() as c:
        c.execute('DELETE FROM session_claims WHERE account=%s', (OTHER,))
    refused(store, mine, '''INSERT INTO session_claims(account,host_id,boot_id,lock_ref,holder_id,holder_kind,
        generation,claimed_at,seen_at) VALUES(%s,%s,'b','1:1',%s,'holder',1,clock_timestamp(),clock_timestamp())''',
        (OTHER, '3' * 32, '4' * 32))
    # Nothing beyond the granted columns and tables.
    refused(store, mine, 'SELECT * FROM messages')
    refused(store, mine, 'SELECT detail_code FROM source_status')
    refused(store, mine, "UPDATE collector_leases SET expires_at=clock_timestamp()")
    refused(store, mine, "INSERT INTO collector_leases(account,owner,epoch,expires_at) VALUES(%s,'x',1,now())", (MINE,))
    refused(store, mine, "UPDATE source_status SET status='ok'")
    refused(store, mine, 'DELETE FROM session_claims')
    refused(store, mine, "INSERT INTO session_holder_accounts(role_name,account) VALUES('x',%s)", (OTHER,))


# spec: ANYM-936
def test_an_unbound_login_and_the_group_itself_see_no_row(holders):
    store, make = holders
    unbound = make()
    for role in (unbound, 'session_holder'):
        with as_role(store, role) as c:
            assert c.execute('SELECT account FROM session_claims').fetchall() == []
            assert c.execute('SELECT account,owner,epoch,expires_at FROM collector_leases').fetchall() == []
            assert c.execute('SELECT account,status FROM source_status').fetchall() == []
    # A bound login that switches to the group keeps nothing of its binding.
    mine = make(MINE)
    with as_role(store, mine) as c:
        c.execute('SET LOCAL ROLE session_holder')
        assert c.execute('SELECT account FROM session_claims').fetchall() == []


# spec: ANYM-936
def test_a_real_holder_login_connection_is_held_to_its_account(holders):
    store, make = holders
    password = 'synthetic-' + uuid4().hex
    mine = make(MINE, password=password)
    with store.pool.connection() as c:
        dsn = c.info.dsn
    params = conninfo_to_dict(dsn)
    params.pop('password', None)
    params.pop('user', None)
    try:
        connection = psycopg.connect(make_conninfo(**params, user=mine, password=password), row_factory=dict_row)
    except psycopg.OperationalError as exc:  # a cluster whose pg_hba refuses password logins for new roles
        pytest.skip(f'password login unavailable on this test cluster: {type(exc).__name__}')
    with connection:
        assert connection.execute('SELECT session_user AS s').fetchone()['s'] == mine
        assert [r['account'] for r in connection.execute('SELECT account FROM session_claims')] == [MINE]
        assert [r['account'] for r in connection.execute('SELECT account,status FROM source_status')] == [MINE]
        with pytest.raises(FORBIDDEN):
            connection.execute('SELECT * FROM messages')


# spec: ANYM-936
def test_the_holder_runs_anym_914s_claim_statements_for_its_own_account(holders, tmp_path):
    store, make = holders
    mine = make(MINE)
    with store.pool.connection() as c:
        c.execute('DELETE FROM session_claims')
    directory = tmp_path / 'session-locks'
    directory.mkdir(mode=0o700)
    boot = tmp_path / 'boot_id'
    boot.write_text('boot-one\n')
    pool = RolePool(store, mine)
    with sc.host_lock(MINE, directory, boot_id_path=boot) as lock:
        claim = sc.acquire(pool, lock, 'holder')
        assert claim.kind == 'holder' and claim.generation == 1
        assert sc.refresh(pool, claim) is True
        assert sc.row(pool, MINE)['holder_kind'] == 'holder'
        assert sc.release(pool, claim) is True
        again = sc.acquire(pool, lock, 'holder')
        assert again.generation == 2
        sc.release(pool, again)
    # Its account's claim is the only one it can take.
    with sc.host_lock(OTHER, directory, boot_id_path=boot) as foreign:
        with pytest.raises(FORBIDDEN):
            sc.acquire(pool, foreign, 'holder')


# spec: ANYM-936
def test_the_restore_gate_holds_a_holder_on_a_restored_pre_claim_database(holders, tmp_path):
    store, make = holders
    mine = make(MINE)
    with store.pool.connection() as c:
        c.execute('DELETE FROM session_claims')
        # What quarantine_restore leaves for a backup from before the claim: no row and restored_disabled.
        c.execute("UPDATE source_status SET status='restored_disabled' WHERE account=%s", (MINE,))
    directory = tmp_path / 'session-locks'
    directory.mkdir(mode=0o700)
    boot = tmp_path / 'boot_id'
    boot.write_text('boot-one\n')
    with sc.host_lock(MINE, directory, boot_id_path=boot) as lock:
        with pytest.raises(sc.ClaimRefused, match='session_claim_restore_hold'):
            sc.acquire(RolePool(store, mine), lock, 'holder')
    with store.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM session_claims').fetchone()['n'] == 0


# spec: ANYM-936
def test_collector_and_gateway_roles_keep_their_lease_claim_and_status_access(holders, tmp_path):
    from test_db_role_isolation import install, remove
    store, make = holders
    make(MINE)  # a holder exists: row security is live for everyone
    with store.pool.connection() as c:
        c.execute('DELETE FROM collector_leases')
        c.execute('DELETE FROM session_claims')
    bound = install(store, 'collector_role.sql', 'messaging_collector', account=MINE)
    shared = install(store, 'collector_role.sql', 'messaging_collector')
    gateway = install(store, 'gateway_role.sql', 'messaging_gateway')
    try:
        for role, account in ((bound, MINE), (shared, OTHER)):
            archive = Archive(NS(pool=RolePool(store, role), append_event=store.append_event, files=store.files))
            owner = 'owner-' + role
            epoch = archive.acquire(account, owner)
            archive.renew(account, owner, epoch)
            with as_role(store, role) as c:
                Archive.fence(c, account, (owner, epoch))
                assert c.execute('SELECT status FROM source_status WHERE account=%s', (account,)).fetchone()
                assert c.execute('UPDATE source_status SET detail_code=detail_code WHERE account=%s', (account,)).rowcount == 1
            archive.release(account, owner, epoch)
            directory = tmp_path / ('locks-' + role)
            directory.mkdir(mode=0o700)
            boot = directory.parent / ('boot-' + role)
            boot.write_text('boot-one\n')
            with sc.host_lock(account, directory, boot_id_path=boot) as lock:
                claim = sc.acquire(RolePool(store, role), lock, 'collector')
                assert sc.refresh(RolePool(store, role), claim) and sc.release(RolePool(store, role), claim)
        # A bound collector still cannot write another account's lease (the 037 guard is unchanged).
        refused(store, bound, "UPDATE collector_leases SET expires_at=clock_timestamp() WHERE account=%s", (OTHER,))
        with as_role(store, gateway) as c:
            assert {r['account'] for r in c.execute('SELECT account FROM source_status')} >= {MINE, OTHER}
            assert {r['account'] for r in c.execute('SELECT account FROM session_claims')} == {MINE, OTHER}
    finally:
        for role in (bound, shared, gateway):
            remove(store, role)


def collector_round(store, role, account):
    """A collector role's lease acquire, renew and release (each statement passes the row security)."""
    archive = Archive(NS(pool=RolePool(store, role), append_event=store.append_event, files=store.files))
    owner = 'owner-' + role
    epoch = archive.acquire(account, owner)
    archive.renew(account, owner, epoch)
    archive.release(account, owner, epoch)


# spec: ANYM-936
def test_the_role_file_gives_back_the_binding_read_a_no_acl_restore_drops(holders):
    from test_db_role_isolation import install, remove
    store, make = holders
    make(MINE)
    with store.pool.connection() as c:
        c.execute('DELETE FROM collector_leases')
        # What pg_restore --no-acl leaves: the policies, but no grant on the binding table they read.
        c.execute('REVOKE ALL ON session_holder_accounts FROM PUBLIC')
    collector = install(store, 'collector_role.sql', 'messaging_collector', account=MINE)
    try:
        with pytest.raises(FORBIDDEN, match='session_holder_accounts'):
            collector_round(store, collector, MINE)
        group(store)  # the role files applied again after the restore
        collector_round(store, collector, MINE)
    finally:
        with store.pool.connection() as c:
            c.execute('GRANT SELECT ON session_holder_accounts TO PUBLIC')
        remove(store, collector)


# spec: ANYM-936
def test_a_grant_appended_to_the_role_file_stays_additive_and_a_public_one_does_not():
    from messaging_service import migration_rollout as rollout
    base = SOURCE.joinpath('session_holder_role.sql').read_text()
    tables = {'session_claims', 'collector_leases', 'source_status', 'session_holder_accounts'}
    # The file's own GRANT ... TO PUBLIC does not stop a later unattended expand (store.migrate_expand, role_not_additive).
    assert rollout.role_class(base, base + 'GRANT SELECT(holder_id) ON session_claims TO session_holder;\n', {},
                              tables=tables) == 'additive'
    # PUBLIC is never one of the file's roles: widening it again goes by hand.
    assert rollout.role_class(base, base + 'GRANT SELECT ON session_claims TO PUBLIC;\n', {}, tables=tables) == 'manual'


# production.md "Session holder role", step 4: a new login takes over its account's binding in one statement.
BIND_SQL = '''INSERT INTO session_holder_accounts(role_name,account) VALUES(%s,%s) ON CONFLICT (account) DO UPDATE SET
 role_name=excluded.role_name, created_at=clock_timestamp()'''


# spec: ANYM-936
@pytest.mark.parametrize('version', [81, 82])
def test_a_real_no_acl_restore_with_its_role_files_keeps_collectors_working_and_holders_held(database, tmp_path, version):
    from test_db_role_isolation import install, remove
    from test_subscription_retention import restore_database, snapshot_restored
    group(database)  # the cluster's fixed NOLOGIN role (RESTORE_ROLES on a fresh cluster)
    with restore_database(database, tmp_path / 'source', through=version) as source:
        source.seed()
        seed_rows(source)
        old = login(source, MINE) if version >= 82 else None  # a schema-82 backup holds a binding
        try:
            with snapshot_restored(source, tmp_path) as restored:
                backup.quarantine_restore(restored)
                if version < 82:
                    restored.migrate()
                group(restored)  # the role files, applied again after the --no-acl restore
                collector = install(restored, 'collector_role.sql', 'messaging_collector', account=MINE)
                new = 'anym936_holder_' + uuid4().hex[:10]
                try:
                    with restored.pool.connection() as c:
                        c.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS IN ROLE session_holder').format(
                            sql.Identifier(new)))
                        c.execute(BIND_SQL, (new, MINE))
                        assert [r['role_name'] for r in c.execute('SELECT role_name FROM session_holder_accounts')] == [new]
                    collector_round(restored, collector, MINE)
                    with as_role(restored, new) as c:
                        assert [(r['account'], r['restore_hold']) for r in c.execute(
                            'SELECT account,restore_hold FROM session_claims')] == [(MINE, True)]
                        assert [r['account'] for r in c.execute('SELECT account,status FROM source_status')] == [MINE]
                    if old:  # the restored binding moved: the backup's login reaches no row any more
                        with as_role(restored, old) as c:
                            assert c.execute('SELECT account FROM session_claims').fetchall() == []
                    directory = tmp_path / 'session-locks'
                    directory.mkdir(mode=0o700)
                    boot = tmp_path / 'boot_id'
                    boot.write_text('boot-one\n')
                    with sc.host_lock(MINE, directory, boot_id_path=boot) as lock:
                        with pytest.raises(sc.ClaimRefused, match='session_claim_restore_hold'):
                            sc.acquire(RolePool(restored, new), lock, 'holder')
                finally:
                    remove(restored, collector)
                    drop(restored, new)
        finally:
            if old:
                drop(source, old)


# spec: ANYM-936
def test_the_binding_table_is_backed_up_and_the_group_is_a_fixed_restore_role(holders):
    store, make = holders
    mine = make(MINE)
    assert 'session_holder' in restore_worker.RESTORE_ROLES
    assert 'CREATE ROLE session_holder NOLOGIN;' in restore_worker.restore_roles_sql()
    with store.pool.connection() as c:
        manifest = backup.database_manifest(c)
        policies = {(r['tablename'], r['policyname']) for r in c.execute(
            "SELECT tablename,policyname FROM pg_policies WHERE policyname='session_holder_scope'")}
    assert manifest['session_holder_accounts']['rows'] == 1 and mine
    assert policies == {(t, 'session_holder_scope') for t in ('session_claims', 'collector_leases', 'source_status')}
