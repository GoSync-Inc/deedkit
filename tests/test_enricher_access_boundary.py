"""The enricher's access boundary (ANYM-78): what the real enrichment_role.sql lets the role read and write on a
disposable database, and what the committed enrichment Compose overlay mounts and connects. The container sandbox
bounds (read-only root, capabilities, limits) are proved by tests/test_compose_runtime.py and are not repeated.

Two later decisions shape the current contract and are asserted as such: readiness events go only through the fenced
publish_file_enrichment_event function (ANYM-254 revokes the direct events/feed_counter writes), and the role may read
four subscription columns and three source columns to apply the on-request media policy (ANYM-805, owner 2026-11-06).
ANYM-78 criteria 3 and 4 state this same boundary, citing ANYM-254 criterion 1 and ANYM-805 criterion 1."""
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
import yaml

from messaging_service import store as store_module

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = Path(store_module.__file__).with_name('enrichment_role.sql')
READS = ('attachments', 'messages', 'attachment_text', 'attachment_residency', 'attachment_eviction_journal',
         'message_metadata', 'message_revisions', 'events')
WRITES = ('enrichment_budget', 'processing_profiles', 'processing_resource_leases', 'processing_budget', 'processing_control')
NO_ACCESS = ('api_keys', 'principals', 'consumers', 'download_grants', 'delivery_endpoints', 'outgoing_messages',
             'messenger_commands', 'feed_counter', 'audit_log')
SUBSCRIPTION_COLUMNS = {'subscriptions': {'id', 'state', 'expires_at', 'closed_at'},
                        'subscription_sources': {'subscription', 'account', 'chat'}}


@pytest.fixture
def enricher(database):
    role = 'anym78_enricher_' + uuid4().hex[:10]
    with database.pool.connection() as c:
        c.execute(TEMPLATE.read_text().replace('messaging_enricher', role))
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


def table(c, role, name, action):
    return c.execute('SELECT has_table_privilege(%s,%s,%s) AS ok', (role, name, action)).fetchone()['ok']


def first_column(c, name):
    return c.execute('''SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position LIMIT 1''', (name,)).fetchone()['column_name']


# spec: ANYM-78
def test_the_enricher_reads_its_inputs_updates_enrichment_rows_only_and_writes_its_bookkeeping(enricher):
    store, role = enricher
    with as_role(store, role) as c:
        for name in READS:
            c.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(name)))
    with store.pool.connection() as c:
        assert table(c, role, 'attachment_enrichment', 'UPDATE')
        for name in WRITES:
            assert all(table(c, role, name, action) for action in ('SELECT', 'INSERT', 'UPDATE')), name
            assert not table(c, role, name, 'DELETE'), name
        column = first_column(c, 'messages')
    refused(store, role, 'INSERT INTO attachment_enrichment DEFAULT VALUES')
    refused(store, role, 'DELETE FROM attachment_enrichment WHERE false')
    refused(store, role, sql.SQL('UPDATE messages SET {0}={0} WHERE false').format(sql.Identifier(column)))
    refused(store, role, 'DELETE FROM messages WHERE false')
    refused(store, role, 'DELETE FROM attachments WHERE false')


# spec: ANYM-78
def test_readiness_is_published_only_through_the_enrichment_function(enricher):
    store, role = enricher
    with store.pool.connection() as c:
        def executes(signature):
            return c.execute("SELECT has_function_privilege(%s,%s,'EXECUTE') AS ok", (role, signature)).fetchone()['ok']
        assert executes('public.publish_file_enrichment_event(text,text,text,text,jsonb)')
        assert not executes('public.publish_file_text_event(text,text,text,text,jsonb)')
        assert not executes('public.publish_processing_event(text[],text,text,text,text,jsonb)')
        column = first_column(c, 'events')
    refused(store, role, 'INSERT INTO events DEFAULT VALUES')
    refused(store, role, sql.SQL('UPDATE events SET {0}={0} WHERE false').format(sql.Identifier(column)))
    refused(store, role, 'UPDATE feed_counter SET value=value')
    refused(store, role, 'SELECT value FROM feed_counter')


# spec: ANYM-78
def test_no_credential_key_message_mutation_or_subscription_write_access(enricher):
    store, role = enricher
    with store.pool.connection() as c:
        for name in NO_ACCESS:
            assert not any(table(c, role, name, action) for action in ('SELECT', 'INSERT', 'UPDATE', 'DELETE')), name
        for name, allowed in SUBSCRIPTION_COLUMNS.items():
            columns = {r['column_name'] for r in c.execute('''SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name=%s''', (name,))}
            readable = {col for col in columns if c.execute("SELECT has_column_privilege(%s,%s,%s,'SELECT') AS ok",
                                                            (role, name, col)).fetchone()['ok']}
            assert readable == allowed, name
            assert not any(table(c, role, name, action) for action in ('INSERT', 'UPDATE', 'DELETE')), name
    refused(store, role, 'SELECT digest FROM api_keys')


def enrichment():
    return yaml.safe_load((ROOT / 'deploy' / 'compose.enrichment.yaml').read_text())


def internal_networks():
    networks = yaml.safe_load((ROOT / 'deploy' / 'compose.yaml').read_text())['networks']
    return {name for name, value in networks.items() if (value or {}).get('internal') is True}


def mounts(service):
    """(target, read_only) for every volume, short or long form."""
    found = []
    for volume in service['volumes']:
        if isinstance(volume, str):
            parts = volume.split(':')
            found.append((parts[1], len(parts) > 2 and parts[2] == 'ro'))
        else:
            found.append((volume['target'], volume.get('read_only') is True))
    return found


# spec: ANYM-78
@pytest.mark.parametrize('name', ['enricher', 'enricher-local'])
def test_both_profiles_mount_blobs_and_secrets_read_only_with_only_the_lock_directory_writable(name):
    service = enrichment()['services'][name]
    found = mounts(service)
    assert ('/data', True) in found
    assert [target for target, read_only in found if not read_only] == ['/processing-locks']
    if name == 'enricher-local':
        assert ('/models', True) in found


# spec: ANYM-78
def test_only_the_cloud_profile_reaches_a_network_with_egress():
    services, internal = enrichment()['services'], internal_networks()
    assert services['enricher']['profiles'] == ['enrichment-cloud'] and set(services['enricher']['networks']) - internal
    assert services['enricher-local']['profiles'] == ['enrichment-local']
    assert set(services['enricher-local']['networks']) == {'backend'} <= internal
