"""Schema26 backup/quarantine and matcher privileges, with synthetic data only."""
from contextlib import contextmanager
from copy import copy
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from messaging_service.backup import database_manifest, quarantine_restore
from messaging_service.store import Store
from messaging_service.subscriptions import match_subscriptions


def _subscription(store, principal):
    token = store.issue_key(principal, ['telegram-demo'],
        permissions=['read', 'subscription-write'], key_permissions=['read', 'subscription-write'])
    definition = store.create_subscription(token, ['telegram-demo-chat'],
        label='Synthetic subscription', sender_ids=['demo-person-1'])
    with store.pool.connection() as connection:
        event = store.append_event(connection, 'telegram-demo', 'demo-tg-1', origin='live')
    return token, definition, event


def test_schema26_manifest_includes_definitions_sources_and_exact_match_references(seeded):
    store, _, _ = seeded
    _, definition, event = _subscription(store, 'synthetic-manifest-subscriber')
    match_subscriptions(store)
    with store.pool.connection() as connection:
        assert connection.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '62'
        manifest = database_manifest(connection)
    assert len(manifest) == 91
    assert all(manifest[name]['rows'] == 1 for name in (
        'subscriptions', 'subscription_sources', 'subscription_matches'))
    assert all(len(manifest[name]['sha256']) == 64 for name in (
        'subscriptions', 'subscription_sources', 'subscription_matches'))
    with store.pool.connection() as connection:
        exact = connection.execute('SELECT subscription,event FROM subscription_matches').fetchone()
        connection.execute('UPDATE subscriptions SET label=%s WHERE id=%s',
            ('Changed synthetic label', definition['subscription_id']))
        changed = database_manifest(connection)
    assert str(exact['subscription']) == definition['subscription_id'] and exact['event'] == event
    assert manifest['subscriptions']['sha256'] != changed['subscriptions']['sha256']
    assert manifest['subscription_sources'] == changed['subscription_sources']
    assert manifest['subscription_matches'] == changed['subscription_matches']


def test_native_schema26_quarantine_closes_versions_preserves_refs_and_revokes_runtime(database, tmp_path):
    """Never quarantine the fixture source: own an isolated *_restore_test DB."""
    base_dsn = database.pool.conninfo
    restore_name = 'anymsg_sub_' + uuid4().hex + '_restore_test'
    restored = None
    created = False
    try:
        with psycopg.connect(base_dsn, autocommit=True) as owner:
            owner.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(restore_name)))
        created = True
        restored = Store(make_conninfo(base_dsn, dbname=restore_name),
            tmp_path / 'synthetic-originals', 'http://testserver')
        restored.migrate()
        restored.seed()
        token, active, _ = _subscription(restored, 'synthetic-quarantine-subscriber')
        credential=restored.capabilities(token)['credential_id']
        paused = restored.create_subscription(token, ['telegram-demo-chat'])
        paused = restored.update_subscription(token, paused['subscription_id'], paused['version'], action='pause')
        match_subscriptions(restored)
        with restored.pool.connection() as connection:
            connection.execute("""INSERT INTO account_policies(account,enabled,send_enabled,operations_enabled)
                SELECT id,true,true,ARRAY['mark_read'] FROM accounts""")
            connection.execute("""INSERT INTO collector_leases(account,owner,epoch,expires_at)
                SELECT id,'synthetic-restore-collector',1,clock_timestamp()+interval '1 hour' FROM accounts""")
            connection.execute("""INSERT INTO source_status(account,status,coverage,detail_code)
                SELECT id,'receiving','partial','synthetic' FROM accounts""")
            connection.execute("""INSERT INTO file_uploads(principal,key_id,account,chat,filename,mime,
                bytes,sha256,received,state,volume_key,request_id)
                VALUES('synthetic-quarantine-subscriber',%s,'telegram-demo','telegram-demo-chat',
                'synthetic.txt','text/plain',1,%s,1,'ready','fs:synthetic','synthetic-restore-upload')""",
                (credential,'a'*64))
            receiver = uuid4()
            connection.execute('''INSERT INTO subscription_receivers(id,principal,key_id,endpoint,secret_file)
                VALUES(%s,'synthetic-quarantine-subscriber',%s,'https://synthetic.invalid/wake',
                '/private/synthetic-receiver.key')''', (receiver, credential))
            connection.execute('INSERT INTO subscription_delivery(subscription,receiver) VALUES(%s,%s)',
                (active['subscription_id'], receiver))
            connection.execute('''INSERT INTO subscription_outbox(subscription,subscription_version,receiver,
                through_position,kind) VALUES(%s,1,%s,1,'events')''', (active['subscription_id'], receiver))
            connection.execute('INSERT INTO subscription_consumption(subscription,acked_through) VALUES(%s,0)',
                (active['subscription_id'],))
            connection.execute('''INSERT INTO subscription_receipts(receipt_hash,subscription,principal,key_id,
                scope_version,key_permission_version,subscription_version,consumption_version,after_position,
                through_position) VALUES(%s,%s,'synthetic-quarantine-subscriber',%s,1,1,1,1,0,1)''',
                ('b'*64, active['subscription_id'], credential))
            connection.execute('''INSERT INTO agent_feedback(principal,key_id,request_id,request_hash,category,
                description,refs) VALUES('synthetic-quarantine-subscriber',%s,'synthetic-feedback',%s,
                'bug','Synthetic restore fixture','{}')''', (credential, 'c'*64))
            before_versions = {str(row['id']): row['version'] for row in
                connection.execute('SELECT id,version FROM subscriptions').fetchall()}
            before = database_manifest(connection)
        assert before['subscriptions']['rows'] == 2 and before['subscription_matches']['rows'] == 1
        assert set(before_versions) == {active['subscription_id'], paused['subscription_id']}
        quarantine_restore(restored)
        with restored.pool.connection() as connection:
            after = database_manifest(connection)
            definitions = connection.execute('SELECT id,state,version FROM subscriptions').fetchall()
            keys = connection.execute('SELECT revoked FROM api_keys').fetchall()
            policies = connection.execute('SELECT enabled,send_enabled,operations_enabled FROM account_policies').fetchall()
            leases = connection.execute('SELECT expires_at<clock_timestamp() AS expired FROM collector_leases').fetchall()
            statuses = connection.execute('SELECT status,coverage,detail_code FROM source_status').fetchall()
            assert connection.execute('SELECT state FROM file_uploads').fetchone()['state']=='expired'
            assert not connection.execute('SELECT enabled FROM subscription_receivers').fetchone()['enabled']
            assert connection.execute('SELECT state FROM subscription_outbox').fetchone()['state'] == 'cancelled'
            assert connection.execute('SELECT count(*) AS n FROM subscription_receipts').fetchone()['n'] == 0
            assert after['subscription_consumption'] == before['subscription_consumption']
            assert after['agent_feedback'] == before['agent_feedback']
        assert len(after) == 91 and after['subscriptions']['rows'] == 2
        assert all(row['state'] == 'closed' and row['version'] == before_versions[str(row['id'])] + 1
            for row in definitions)
        assert after['subscription_sources'] == before['subscription_sources']
        assert after['subscription_matches'] == before['subscription_matches']
        assert after['events'] == before['events']
        assert keys and all(row['revoked'] for row in keys)
        assert policies and all(not row['enabled'] and not row['send_enabled'] and row['operations_enabled'] == []
            for row in policies)
        assert leases and all(row['expired'] for row in leases)
        assert statuses and all(row == {'status': 'restored_disabled', 'coverage': 'unknown',
            'detail_code': 'requires_reconciliation'} for row in statuses)
        assert match_subscriptions(restored)['checked'] == 0
    finally:
        if restored is not None:
            restored.close()
        if created:
            with psycopg.connect(base_dsn, autocommit=True) as owner:
                owner.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(restore_name)))


def test_dedicated_matcher_role_matches_without_credentials_or_provider_mutations(seeded):
    store, _, _ = seeded
    token, definition, event = _subscription(store, 'synthetic-role-subscriber')
    role = 'test_subscription_matcher_' + uuid4().hex
    ddl = Path(__file__).resolve().parents[1].joinpath('services/messaging/messaging_service/subscription_role.sql').read_text()
    ddl = ddl.replace('messaging_subscriptions', role)
    created = False

    class RestrictedPool:
        @contextmanager
        def connection(self):
            with store.pool.connection() as connection:
                connection.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                yield connection

    matcher = copy(store)
    matcher.pool = RestrictedPool()
    try:
        with store.pool.connection() as connection:
            connection.execute(ddl)
        created = True
        stats = match_subscriptions(matcher)
        assert stats['matched'] == 1 and stats['blocked'] == 0
        page = store.get_subscription_events(token, definition['subscription_id'])
        assert [item['event_id'] for item in page['items']] == [event]
        assert page['subscription']['matcher_caught_up']
        with matcher.pool.connection() as connection:
            assert connection.execute('SELECT id,principal,permissions,permission_version FROM api_keys').fetchall()
            assert connection.execute('SELECT id,accounts,chat_ids,permissions,scope_version,enabled FROM principals').fetchall()
        forbidden = (
            'SELECT digest FROM api_keys', 'SELECT * FROM api_keys',
            'UPDATE api_keys SET revoked=true', 'UPDATE principals SET enabled=false',
            'UPDATE account_policies SET enabled=true',
            "UPDATE messages SET body='forged'", 'DELETE FROM messages',
            'INSERT INTO events SELECT * FROM events WHERE false', 'DELETE FROM events',
            'UPDATE feed_counter SET value=value+1',
            'INSERT INTO outgoing_messages DEFAULT VALUES',
            'INSERT INTO messenger_commands DEFAULT VALUES',
            'INSERT INTO subscriptions SELECT * FROM subscriptions WHERE false',
            'DELETE FROM subscriptions', 'UPDATE subscriptions SET key_id=key_id',
            'UPDATE subscriptions SET selector=selector', 'DELETE FROM subscription_sources',
            "UPDATE subscription_sources SET sender_ids='{}'",
            'UPDATE subscription_matches SET matched_at=clock_timestamp()',
        )
        for statement in forbidden:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with matcher.pool.connection() as connection:
                    connection.execute(statement)
        assert store.get_subscription_events(token, definition['subscription_id'])['items'] == page['items']
    finally:
        if created:
            with store.pool.connection() as connection:
                connection.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                connection.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


# spec: ANYM-519
def test_quarantine_restore_disables_every_self_service_endpoint(database, tmp_path):
    """Never quarantine the fixture source: own an isolated *_restore_test DB."""
    base_dsn = database.pool.conninfo
    restore_name = 'anymsg_endpoint_' + uuid4().hex + '_restore_test'
    restored, created = None, False
    try:
        with psycopg.connect(base_dsn, autocommit=True) as owner:
            owner.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(restore_name)))
        created = True
        restored = Store(make_conninfo(base_dsn, dbname=restore_name), tmp_path / 'synthetic-originals', 'http://testserver')
        restored.migrate()
        restored.seed()
        token = restored.issue_key('synthetic-endpoint-owner', ['telegram-demo'])
        key_id = restored.capabilities(token)['credential_id']
        with restored.pool.connection() as connection:
            for index, state in enumerate(('enabled', 'agent_paused', 'auto_paused', 'deleted')):
                endpoint = connection.execute("""INSERT INTO delivery_endpoints(principal,key_id,request_id,request_hash,
                    url,host,state,signing_secret) VALUES('synthetic-endpoint-owner',%s,%s,%s,
                    'https://hooks.example.test/hook','hooks.example.test',%s,%s) RETURNING id""",
                    (key_id, f'synthetic-request-{index}', 'b' * 64, state, None if state == 'deleted' else
                     'whsec_c3ludGhldGljLXJlc3RvcmUtc2VjcmV0LTAxMjM0NTY3')).fetchone()['id']
                connection.execute('INSERT INTO delivery_endpoint_tests(endpoint,request_id) VALUES(%s,%s)',
                    (endpoint, f'synthetic-test-{index}'))
            before = database_manifest(connection)
        assert before['delivery_endpoints']['rows'] == 4 and before['delivery_endpoint_tests']['rows'] == 4
        quarantine_restore(restored)
        with restored.pool.connection() as connection:
            states = sorted(row['state'] for row in connection.execute('SELECT state FROM delivery_endpoints'))
            tests = connection.execute('SELECT DISTINCT state,error_code FROM delivery_endpoint_tests').fetchall()
        assert states == ['deleted', 'operator_disabled', 'operator_disabled', 'operator_disabled']
        assert tests == [{'state': 'cancelled', 'error_code': 'restored_disabled'}]
    finally:
        if restored is not None:
            restored.close()
        if created:
            with psycopg.connect(base_dsn, autocommit=True) as owner:
                owner.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(restore_name)))
