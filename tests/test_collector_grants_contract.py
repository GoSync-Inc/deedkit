"""The collector role's remaining grants (ANYM-100), through the real template on a disposable database: file_uploads
is read-only, and register_resident_original is the one function the template opens, never to PUBLIC."""
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from messaging_service import store as store_module

TEMPLATE = Path(store_module.__file__).with_name('collector_role.sql')
REGISTER = 'public.register_resident_original(text,bigint)'


@pytest.fixture
def collector(database):
    role = 'anym100_collector_' + uuid4().hex[:10]
    with database.pool.connection() as c:
        c.execute(TEMPLATE.read_text().replace('messaging_collector', role))
    yield database, role
    with database.pool.connection() as c:
        c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
        c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


@contextmanager
def as_role(store, role):
    with store.pool.connection() as c:
        c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
        yield c


def refused(store, role, statement):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_role(store, role) as c:
            c.execute(statement)


# spec: ANYM-100
def test_the_collector_reads_file_uploads_and_cannot_insert_update_or_delete_them(collector):
    store, role = collector
    with store.pool.connection() as c:
        column = c.execute('''SELECT column_name FROM information_schema.columns WHERE table_schema='public'
            AND table_name='file_uploads' ORDER BY ordinal_position LIMIT 1''').fetchone()['column_name']
    with as_role(store, role) as c:
        assert c.execute('SELECT count(*) AS n FROM file_uploads').fetchone()['n'] >= 0
    refused(store, role, 'INSERT INTO file_uploads DEFAULT VALUES')
    refused(store, role, sql.SQL('UPDATE file_uploads SET {0}={0} WHERE false').format(sql.Identifier(column)))
    refused(store, role, 'DELETE FROM file_uploads WHERE false')


def granted_functions(c, grantee):
    """public functions `grantee` may execute through an explicit grant (aclexplode; PUBLIC is grantee 0)."""
    oid = 0 if grantee == 'public' else c.execute('SELECT oid FROM pg_roles WHERE rolname=%s', (grantee,)).fetchone()['oid']
    return {row['f'] for row in c.execute('''SELECT p.oid::regprocedure::text AS f FROM pg_proc p
        JOIN pg_namespace n ON n.oid=p.pronamespace, aclexplode(p.proacl) a
        WHERE n.nspname='public' AND p.proacl IS NOT NULL AND a.grantee=%s AND a.privilege_type='EXECUTE' ''', (oid,))}


# spec: ANYM-100
def test_register_resident_original_is_the_only_function_the_template_opens_and_public_cannot_execute_it(collector):
    store, role = collector
    with store.pool.connection() as c:
        assert granted_functions(c, role) == {'register_resident_original(text,bigint)'}
        assert 'register_resident_original(text,bigint)' not in granted_functions(c, 'public')
    # The collector reaches the function body (an unknown original is its own refusal, not a privilege error) ...
    with pytest.raises(psycopg.errors.RaiseException, match='unknown_original'):
        with as_role(store, role) as c:
            c.execute('SELECT ' + REGISTER.split('(')[0] + '(%s,%s)', ('0' * 64, 1))
    # ... while a role with no grant of its own, which holds only what PUBLIC holds, cannot call it.
    bare = 'anym100_public_' + uuid4().hex[:10]
    with store.pool.connection() as c:
        c.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(bare)))
    try:
        refused(store, bare, sql.SQL('SELECT public.register_resident_original({},{})').format(
            sql.Literal('0' * 64), sql.Literal(1)))
    finally:
        with store.pool.connection() as c:
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(bare)))
