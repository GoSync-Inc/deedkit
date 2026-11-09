import json
import sys

import pytest

from messaging_service.backup import create_snapshot,verify_snapshot,quarantine_restore


def test_snapshot_manifest_missing_blob_and_dump_failure(seeded,tmp_path):
    s,a,b=seeded
    with pytest.raises(RuntimeError,match='pg_dump_failed'):
        create_snapshot(s,tmp_path/'failed',[sys.executable,'-c','raise SystemExit(2)'])
    assert not (tmp_path/'failed').exists()
    assert list(tmp_path.glob('.backup-*'))==[]
    # Unit stub only; real pg_dump/pg_restore exercised by the separate deployment restore drill.
    result=create_snapshot(s,tmp_path/'snapshot',[sys.executable,'-c','print("unit-dump-stub")'])
    assert result['blobs']==1
    assert verify_snapshot(s,tmp_path/'snapshot')['status']=='verified'
    manifest=json.loads((tmp_path/'snapshot/manifest.json').read_text())
    (tmp_path/'snapshot/files'/manifest['blobs'][0]['digest']).write_bytes(b'bad')
    with pytest.raises(RuntimeError,match='blob_corrupt'): verify_snapshot(s,tmp_path/'snapshot')


def test_snapshot_rejects_changed_database_and_source_quarantine(seeded,tmp_path):
    s,a,b=seeded
    create_snapshot(s,tmp_path/'snapshot',[sys.executable,'-c','print("unit-dump-stub")'])
    s.get_updates(a,'changed-after-snapshot')
    with pytest.raises(RuntimeError,match='database_mismatch'): verify_snapshot(s,tmp_path/'snapshot')
    with pytest.raises(ValueError,match='not_restore_database'): quarantine_restore(s)
    with pytest.raises(ValueError,match='destination_exists'):
        create_snapshot(s,tmp_path/'snapshot',[sys.executable])


# spec: ANYM-848
def test_a_restored_schema72_backup_quarantines_and_migrates_to_the_wake_tables(database,tmp_path):
    from messaging_service.backup import database_manifest
    from messaging_service.wake_targets import schema_ready
    from test_subscription_retention import acked_subscription,restore_database,snapshot_restored
    with restore_database(database,tmp_path/'source',through=72) as source:
        acked_subscription(source,name='wake-old-snapshot')
        with source.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='72'
            before=database_manifest(c)
        assert 'wake_targets' not in before
        # snapshot_restored verifies the schema-72 bundle before and the restored database after pg_restore.
        with snapshot_restored(source,tmp_path) as restored:
            with restored.pool.connection() as c:
                after=database_manifest(c)
                # The capture's own fence row is the one difference (attachment_backup_fences); the rest is the source.
                assert set(after)==set(before) and after['subscriptions']==before['subscriptions']
                assert schema_ready(c) is False
            # This runtime's quarantine skips the wake step on a schema-72 restore; migrating then adds the tables.
            quarantine_restore(restored)
            restored.migrate()
            restored.migrate()
            with restored.pool.connection() as c:
                assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='76'
                assert schema_ready(c) is True
                assert c.execute('SELECT count(*) AS n FROM wake_targets').fetchone()['n']==0


# spec: ANYM-848
def test_schema73_snapshot_round_trips_the_wake_targets(database,tmp_path):
    from messaging_service.backup import database_manifest
    from test_subscription_retention import acked_subscription,restore_database,snapshot_restored
    with restore_database(database,tmp_path/'source') as source:
        token,sid,_=acked_subscription(source,name='wake-snapshot')
        with source.pool.connection() as c:
            c.execute("UPDATE principals SET permissions=permissions||ARRAY['delivery-write'] WHERE id='wake-snapshot'")
            c.execute("UPDATE api_keys SET permissions=permissions||ARRAY['delivery-write'] WHERE principal='wake-snapshot'")
        source.set_delivery_features(True)
        source.register_wake_target(token,'wake-snapshot-1','codex_thread','Snapshot chat',[sid])
        with source.pool.connection() as c:
            manifest=database_manifest(c)
            before={name:manifest[name] for name in ('wake_targets','wake_target_subscriptions')}
        assert before['wake_targets']['rows']==1 and before['wake_target_subscriptions']['rows']==1
        # snapshot_restored verifies the bundle before and the restored database after pg_restore.
        with snapshot_restored(source,tmp_path) as restored:
            with restored.pool.connection() as c:
                manifest=database_manifest(c)
                assert {name:manifest[name] for name in before}==before


# spec: ANYM-907
def test_schema76_snapshot_round_trips_the_negotiations_and_restores_their_grants(database,tmp_path):
    from datetime import datetime,timedelta,timezone
    from messaging_service.backup import V76_TABLES,database_manifest
    from messaging_service.backup_restore_worker import restore_roles_sql
    from messaging_service.negotiation_store import apply
    from test_subscription_retention import restore_database,snapshot_restored
    now=datetime.now(timezone.utc)
    with restore_database(database,tmp_path/'source') as source:
        with source.pool.connection() as c:
            c.execute("INSERT INTO accounts(id,platform,label) VALUES('neg-backup','telegram','Synthetic')")
            c.execute("INSERT INTO chats(id,account,provider_chat,title,chat_type) VALUES('neg-chat','neg-backup','5','Synthetic','private')")
            c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
                VALUES('neg-m1','neg-backup','neg-chat','1','fixture-anna','давай созвонимся завтра в 10',%s,'incoming')''',
                (now-timedelta(hours=1),))
        with source.pool.connection() as c:
            assert apply(c,'neg-backup','neg-chat',now)==1
        with source.pool.connection() as c:
            manifest=database_manifest(c)
            before={name:manifest[name] for name in V76_TABLES}
        assert before['negotiations']['rows']==1 and before['negotiation_transitions']['rows']==1
        # snapshot_restored verifies the bundle before and the restored database after pg_restore.
        with snapshot_restored(source,tmp_path) as restored:
            # As the restore worker: every RESTORE_ROLES role first, then migrate re-applies 076's grants (a restore
            # runs without ACLs).
            with restored.pool.connection() as c:c.execute(restore_roles_sql())
            restored.migrate()
            with restored.pool.connection() as c:
                manifest=database_manifest(c)
                assert {name:manifest[name] for name in before}==before
                has=lambda role,table,privilege: c.execute('''SELECT CASE WHEN EXISTS (SELECT FROM pg_roles
                    WHERE rolname=%s) THEN has_table_privilege(%s,%s,%s) ELSE NULL END AS ok''',
                    (role,role,table,privilege)).fetchone()['ok']
                for table in V76_TABLES:
                    assert has('messaging_negotiations',table,'DELETE') is True
                    assert has('messaging_purge',table,'SELECT') is False
                for table in ('negotiations','negotiation_sources','negotiation_times'):
                    assert has('messaging_gateway',table,'SELECT') is True and has('messaging_gateway',table,'INSERT') is False


# spec: ANYM-824
def test_schema70_snapshot_restores_the_activity_counts(database,tmp_path):
    from messaging_service.backup import database_manifest
    from test_subscription_retention import acked_subscription,restore_database,snapshot_restored
    with restore_database(database,tmp_path/'source') as source:
        acked_subscription(source,name='activity-snapshot')
        with source.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='76'
            before=database_manifest(c)['subscription_activity']
            rows=c.execute('SELECT * FROM subscription_activity ORDER BY metric').fetchall()
        assert before['rows']==2 and [row['metric'] for row in rows]==['matched','scanned']
        # snapshot_restored verifies the bundle before and the restored database after pg_restore.
        with snapshot_restored(source,tmp_path) as restored:
            with restored.pool.connection() as c:
                assert database_manifest(c)['subscription_activity']==before
                assert c.execute('SELECT * FROM subscription_activity ORDER BY metric').fetchall()==rows
