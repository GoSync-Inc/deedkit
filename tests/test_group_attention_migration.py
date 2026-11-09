"""ANYM-915 migration: two partial indexes on message_metadata for the group-attention candidates, in the 074 shape
(IF NOT EXISTS with a pg_index precondition). Disposable databases, synthetic rows only."""
from pathlib import Path

import psycopg
import pytest

from messaging_service import migration_rollout as rollout
from test_extend_key import old_schema_store

MIGRATION = next((Path(__file__).resolve().parents[1] / 'services/messaging/messaging_service/migrations').glob(
    '[0-9][0-9][0-9]_group_attention_candidates.sql'))
VERSION = int(MIGRATION.name[:3])
DEFINITIONS = {
    'message_metadata_owner_attention': "CREATE INDEX message_metadata_owner_attention ON public.message_metadata USING btree"
        " (message) WHERE (((metadata -> 'mentions_owner'::text) = 'true'::jsonb) OR ((metadata -> 'reply_to_owner'::text)"
        " = 'true'::jsonb))",
    'message_metadata_reply_reference': "CREATE INDEX message_metadata_reply_reference ON public.message_metadata USING btree"
        " (((metadata ->> 'reply_to_provider_id'::text))) WHERE ((metadata ->> 'reply_to_provider_id'::text) IS NOT NULL)"}


def definitions(c):
    return {r['name']:r['d'] for r in c.execute('''SELECT x.relname AS name,pg_get_indexdef(i.indexrelid) AS d
        FROM pg_index i JOIN pg_class x ON x.oid=i.indexrelid WHERE x.relname=ANY(%s) AND i.indisvalid AND i.indisready''',
        (list(DEFINITIONS),)).fetchall()}


# spec: ANYM-915
def test_group_attention_migration_is_a_manual_non_critical_file_that_migrate_expand_still_runs_unattended():
    text = MIGRATION.read_text()
    assert rollout.rollout_class(text) == 'manual'
    assert rollout.unattended_class(text) == 'expand'
    assert rollout.risk_class(text) == ('non-critical', None, None)


# spec: ANYM-915
def test_group_attention_migration_builds_both_indexes_and_a_repeated_migrate_keeps_them(database, tmp_path):
    with old_schema_store(database, tmp_path, VERSION-1) as store:
        with store.pool.connection() as c:
            assert definitions(c) == {}
        store.migrate()
        store.migrate()
        with store.pool.connection() as c:
            assert c.execute('SELECT max(version) AS v FROM archive_migrations').fetchone()['v'] >= VERSION
            assert definitions(c) == DEFINITIONS


# spec: ANYM-915
@pytest.mark.parametrize('shape', ['invalid', 'not_partial', 'unique', 'plain_column'])
def test_group_attention_migration_refuses_an_unusable_index_of_its_name_and_builds_it_once_dropped(database, tmp_path, shape):
    name = 'message_metadata_reply_reference' if shape == 'plain_column' else 'message_metadata_owner_attention'
    with old_schema_store(database, tmp_path, VERSION-1) as store:
        with store.pool.connection() as c:
            c.execute({
                'invalid': "CREATE INDEX message_metadata_owner_attention ON message_metadata(message)"
                    " WHERE metadata->'mentions_owner'='true'::jsonb OR metadata->'reply_to_owner'='true'::jsonb",
                'not_partial': "CREATE INDEX message_metadata_owner_attention ON message_metadata(message)",
                'unique': "CREATE UNIQUE INDEX message_metadata_owner_attention ON message_metadata(message)"
                    " WHERE metadata->'mentions_owner'='true'::jsonb OR metadata->'reply_to_owner'='true'::jsonb",
                'plain_column': "CREATE INDEX message_metadata_reply_reference ON message_metadata(message)"
                    " WHERE metadata->>'reply_to_provider_id' IS NOT NULL"}[shape])
            if shape == 'invalid':
                c.execute("UPDATE pg_index SET indisvalid=false WHERE indexrelid='message_metadata_owner_attention'::regclass")
        with pytest.raises(psycopg.errors.RaiseException, match='not the valid btree index this migration defines'):
            store.migrate()
        with store.pool.connection() as c:
            assert c.execute('SELECT max(version) AS v FROM archive_migrations').fetchone()['v'] == VERSION-1
            c.execute('DROP INDEX '+name)
        store.migrate()
        with store.pool.connection() as c:
            assert definitions(c) == DEFINITIONS
