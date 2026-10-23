"""Real schema29 upgrade: preserve old evidence and replay native metadata failures."""
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from messaging_service import store as store_module
from messaging_service.archive import Archive
from messaging_service.backup import create_snapshot, verify_bundle
from messaging_service.store import Store
from messaging_service.whatsapp_collector import WhatsAppState
from messaging_service.whatsapp_identity import PROVENANCES
from test_whatsapp_identity import observed


@pytest.fixture
def schema29_archive(database, tmp_path):
    """Build an independent old database using unchanged historical SQL/checksums."""
    name = 'anymsg_native_' + uuid4().hex + '_restore_test'
    dsn = database.pool.conninfo
    migrated = None
    with psycopg.connect(dsn, autocommit=True) as owner:
        owner.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        files = tmp_path / 'originals'
        files.mkdir()
        migrated = Store(make_conninfo(dsn, dbname=name), files, 'http://testserver')
        source = Path(store_module.__file__).parent
        with migrated.pool.connection() as c:
            c.execute(source.joinpath('schema.sql').read_text())
            for migration in sorted(source.joinpath('migrations').glob('*.sql')):
                version = int(migration.name[:3])
                if version > 29:
                    break
                c.execute(migration.read_text())
                if version == 17:
                    from messaging_service.chat_evidence import backfill
                    backfill(c)
                c.execute('INSERT INTO archive_migrations(version,sha256) VALUES(%s,%s)',
                    (version, hashlib.sha256(migration.read_bytes()).hexdigest()))
                c.execute("UPDATE probe_metadata SET value=%s WHERE name='schema_version'", (str(version),))
        archive = Archive(migrated)
        archive.register('wa-identities', 'whatsapp', 'Synthetic upgrade evidence', discover=True)
        owner = 'synthetic-upgrade'
        epoch = archive.acquire('wa-identities', owner)
        state = WhatsAppState(archive, 'wa-identities', (owner, epoch))
        yield migrated, state
    finally:
        if migrated is not None:
            migrated.close()
        with psycopg.connect(dsn, autocommit=True) as owner:
            owner.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


def test_schema29_upgrade_preserves_webjs_and_replays_both_native_provenances(schema29_archive, tmp_path):
    store, state = schema29_archive
    webjs = observed()
    original = state.apply(webjs)
    with store.pool.connection() as c:
        before = c.execute('SELECT * FROM whatsapp_identity_observations').fetchall()
        migrations = c.execute('SELECT * FROM archive_migrations ORDER BY version').fetchall()
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '29'
    pending = []
    for number, provenance in enumerate(['whatsmeow_message_alt_v1', 'whatsmeow_lid_store_v1'], 2):
        value = observed(mid=number)
        value['event']['identityObservations'][0]['provenance'] = provenance
        # Exactly the production failure: message commit survives metadata rejection.
        with pytest.raises(psycopg.errors.CheckViolation,
                match='whatsapp_identity_observations_provenance_check'):
            state.apply(value)
        pending.append(value)
    store.migrate()
    store.migrate()
    for value in pending:
        assert state.apply(value)['outcome'] == 'duplicate'
    with store.pool.connection() as c:
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '43'
        assert c.execute('SELECT * FROM archive_migrations WHERE version<=29 ORDER BY version').fetchall() == migrations
        assert c.execute("SELECT * FROM whatsapp_identity_observations WHERE provenance='webjs_contact_cache_v1'").fetchall() == before
        assert {r['provenance'] for r in c.execute('SELECT provenance FROM whatsapp_identity_observations')} == PROVENANCES
        assert c.execute('SELECT revision FROM messages WHERE id=%s', (original['message'],)).fetchone()['revision'] == 1
        # The database still rejects unverified provenance even if Python is bypassed.
        with pytest.raises(psycopg.errors.CheckViolation), c.transaction():
            c.execute("""INSERT INTO whatsapp_identity_observations
                SELECT account,source_message,identity_id,pn,lid,'name_match',first_observed_at,observed_at
                FROM whatsapp_identity_observations LIMIT 1""")
    # The current backup accepts the upgraded schema and its complete table set.
    bundle = tmp_path / 'schema30-snapshot'
    create_snapshot(store, bundle, [sys.executable, '-c', 'print("SYNTHETIC_DUMP")'])
    manifest = verify_bundle(bundle)
    assert manifest['schema_version'] == 43
    assert manifest['tables']['whatsapp_identity_observations']['rows'] == 3
    assert len(manifest['tables']) == 69
    future = {**manifest, 'schema_version': 44}
    bundle.joinpath('manifest.json').write_text(json.dumps(future))
    with pytest.raises(ValueError, match='unsupported_backup'):
        verify_bundle(bundle)
