"""Migration 046 and backups on both sides of it (ANYM-533). Synthetic rows, disposable databases."""
import sys
from types import SimpleNamespace

import pytest

from messaging_service.archive import Archive
from messaging_service.backup import create_snapshot, verify_bundle, verify_snapshot
from messaging_service.envelope import stable_id
from test_archive import event
from test_extend_key import old_schema_store

COLUMNS={'my_role':('text','YES'),'can_post':('boolean','YES'),
         'role_observed_at':('timestamp with time zone','YES'),'username':('text','YES')}
DUMP=[sys.executable,'-c','print("SYNTHETIC_DUMP")']


def role_columns(store):
    with store.pool.connection() as c:
        return {r['column_name']:(r['data_type'],r['is_nullable']) for r in c.execute('''SELECT column_name,data_type,
            is_nullable FROM information_schema.columns WHERE table_schema='public' AND table_name='chats'
            AND column_name=ANY(%s)''',(list(COLUMNS),))}


def schema(store):
    with store.pool.connection() as c:
        return c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']


def channel(store):
    Archive(store).register('roles-tg','telegram','Synthetic roles',discover=True)
    Archive(store).ingest(event(account='roles-tg',platform='telegram',chat='-1001',chat_title='Synthetic channel',
        provider_id='1',metadata={'chat_type':'channel'}))
    return stable_id('chat','roles-tg','-1001')


# spec: ANYM-533
def test_migration_046_adds_nullable_role_columns_and_repeats_over_schema45_rows(database,tmp_path):
    with old_schema_store(database,tmp_path,45) as store:
        chat=channel(store)
        assert schema(store)=='45' and role_columns(store)=={}
        with store.pool.connection() as c:before=c.execute('SELECT * FROM chats WHERE id=%s',(chat,)).fetchone()
        store.migrate();store.migrate()
        assert schema(store)=='49' and role_columns(store)==COLUMNS
        with store.pool.connection() as c:
            after=c.execute('SELECT * FROM chats WHERE id=%s',(chat,)).fetchone()
            assert c.execute('SELECT count(*) AS n FROM archive_migrations WHERE version=46').fetchone()['n']==1
        assert {k:after[k] for k in before}==before and all(after[k] is None for k in COLUMNS)


# spec: ANYM-533
def test_schema46_backup_carries_role_columns_and_verifies(seeded,tmp_path):
    store=seeded[0];chat=channel(store)
    with store.pool.connection() as c:
        c.execute('''UPDATE chats SET my_role='creator',can_post=true,username='synthetic_channel',
            role_observed_at=clock_timestamp() WHERE id=%s''',(chat,))
    create_snapshot(store,tmp_path/'capture',DUMP)
    manifest=verify_bundle(tmp_path/'capture')
    assert manifest['schema_version']==49 and len(manifest['tables'])==78
    assert verify_snapshot(store,tmp_path/'capture')['status']=='verified'
    # The chats digest covers the role columns: a changed role no longer matches the snapshot.
    with store.pool.connection() as c:c.execute("UPDATE chats SET my_role='admin' WHERE id=%s",(chat,))
    with pytest.raises(Exception):verify_snapshot(store,tmp_path/'capture')


# spec: ANYM-533
def test_schema45_backup_still_verifies_with_the_schema46_code(database,tmp_path):
    with old_schema_store(database,tmp_path,45) as store:
        channel(store)
        create_snapshot(store,tmp_path/'capture',DUMP)
        manifest=verify_bundle(tmp_path/'capture')
        assert manifest['schema_version']==45 and len(manifest['tables'])==75
        assert verify_snapshot(store,tmp_path/'capture')['status']=='verified'


# spec: ANYM-533
@pytest.mark.parametrize('version',[45,46])
def test_restore_proof_accepts_schema_45_and_46(version):
    from messaging_service import backup_r2_receiver as r2
    from messaging_service import backup_restore_worker as worker
    from test_backup_r2_verifier import module
    class Lease:
        def __enter__(self):return SimpleNamespace(execute=lambda query:SimpleNamespace(fetchone=lambda:{'value':str(version)}))
        def __exit__(self,*args):pass
    assert worker.restored_schema_version(SimpleNamespace(pool=SimpleNamespace(connection=Lease)),version)==version
    receipt={'wire':{'id':'20261026T101500Z-0123456789abcdef','bytes':100,'sha256':'a'*64,'snapshot_sha256':'b'*64}}
    facts={'v':1,'status':'verified','schema_version':version,'manifest_sha256':'b'*64,'checks':{k:True for k in r2.CHECKS}}
    assert module().make_proof(receipt,facts,'c'*32)['verification']['schema_version']==version


# spec: ANYM-533
def test_prepared_chat_queries_survive_migration_046_on_a_serving_connection(database,tmp_path):
    from messaging_service import commands,outgoing
    with old_schema_store(database,tmp_path,45) as store:
        chat=channel(store);p={'accounts':['roles-tg'],'chat_ids':None}
        with store.pool.connection() as c:
            # Past psycopg's automatic preparation threshold, as a long-lived serving connection would be.
            for _ in range(8):
                commands._chat(c,p,'roles-tg',chat);outgoing.target(c,p,'roles-tg',chat,'')
            c.commit()
            store.migrate()  # another pool connection
            assert schema(store)=='49'
            assert commands._chat(c,p,'roles-tg',chat)['provider_chat']=='-1001'
            assert outgoing.target(c,p,'roles-tg',chat,'')[0]['platform']=='telegram'


# spec: ANYM-533
def test_real_schema45_dump_restores_verifies_and_quarantines_before_migration_046(database,tmp_path):
    from messaging_service import backup_restore_worker as worker
    from messaging_service.backup import quarantine_restore
    from test_subscription_retention import snapshot_restored
    with old_schema_store(database,tmp_path,45) as source:
        chat=channel(source)
        # pg_dump of the schema-45 source, pg_restore into a fresh database, verified at 45 by this release.
        with snapshot_restored(source,tmp_path) as restored:
            assert worker.restored_schema_version(restored,45)==45 and role_columns(restored)=={}
            quarantine_restore(restored)
            with restored.pool.connection() as c:
                assert not c.execute('SELECT 1 FROM account_policies WHERE enabled OR send_enabled').fetchone()
                assert not c.execute('SELECT 1 FROM api_keys WHERE NOT revoked').fetchone()
            restored.migrate()
            assert schema(restored)=='49' and role_columns(restored)==COLUMNS
            with restored.pool.connection() as c:
                row=c.execute('SELECT provider_chat,my_role,can_post FROM chats WHERE id=%s',(chat,)).fetchone()
            assert row=={'provider_chat':'-1001','my_role':None,'can_post':None}
