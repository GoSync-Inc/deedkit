"""A pooled connection the server ended while idle (restart, terminate, idle timeout, network reset) is replaced
at checkout, before the request's transaction starts. The checkout's empty probe precedes BEGIN, so no business
statement, BEGIN or COMMIT is ever repeated; an error after BEGIN still fails the request once.
Disposable database only. Production issue 09746 had this frame signature; its historical cause stays unknown."""
import psycopg
import pytest

from messaging_service.store import APIError, Store


@pytest.fixture()
def fresh(seeded, tmp_path):
    database, one, _ = seeded
    store = Store(database.pool.conninfo, tmp_path, 'http://testserver')
    yield database, store, one
    store.close()


def end_idle(store):
    """The server ends the pool's only idle connection and the test waits until that backend is gone."""
    stats = store.pool.get_stats()
    assert stats['pool_size'] == 1 and stats['pool_available'] == 1
    with store.pool.connection() as c:
        pid = c.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
    with psycopg.connect(store.pool.conninfo, autocommit=True) as admin:
        assert admin.execute('SELECT pg_terminate_backend(%s, 5000)', (pid,)).fetchone()[0]
    return pid


def audited(database, outcome):
    with database.pool.connection() as c:
        return c.execute('''SELECT count(*) AS n FROM audit_log WHERE operation='capabilities' AND outcome=%s''',
            (outcome,)).fetchone()['n']


# spec: ANYM-14
def test_a_request_after_the_server_ended_an_idle_pooled_connection_is_served_once(fresh):
    database, store, token = fresh
    ended = end_idle(store)
    runs = []
    with store.authorized(token, 'capabilities') as (c, p):
        runs.append(c.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'])
    assert len(runs) == 1 and runs[0] != ended
    assert (audited(database, 'ok'), audited(database, 'denied'), audited(database, 'error')) == (1, 0, 0)


# spec: ANYM-14
def test_an_unknown_key_after_the_server_ended_an_idle_pooled_connection_is_still_unauthorized(fresh):
    _, store, _ = fresh
    end_idle(store)
    with pytest.raises(APIError) as refused:
        with store.authorized('u' * 40, 'capabilities'):
            pytest.fail('an unknown key must not reach the operation')
    assert (refused.value.code, refused.value.status) == ('unauthorized', 401)


# spec: ANYM-14
def test_a_connection_lost_after_the_transaction_began_fails_the_request_once_without_a_retry(fresh):
    database, store, token = fresh
    runs = []
    with pytest.raises(APIError) as failed:
        with store.authorized(token, 'capabilities') as (c, p):
            runs.append(1)
            c.execute('SELECT pg_terminate_backend(pg_backend_pid())')
    assert (failed.value.code, failed.value.status) == ('service_unavailable', 503)
    assert runs == [1]
    # The failure is audited once on a fresh connection; nothing of the request committed.
    assert (audited(database, 'ok'), audited(database, 'denied')) == (0, 1)
    with store.authorized(token, 'capabilities'):
        pass
    assert audited(database, 'ok') == 1


# spec: ANYM-14
def test_a_healthy_pooled_connection_is_handed_out_without_an_extra_statement(fresh, monkeypatch):
    _, store, _ = fresh
    seen = []
    original = psycopg.Connection.execute

    def counting(self, query, *args, **kwargs):
        seen.append(query)
        return original(self, query, *args, **kwargs)
    monkeypatch.setattr(psycopg.Connection, 'execute', counting)
    for _ in range(3):
        with store.pool.connection():
            pass
    assert seen == []
