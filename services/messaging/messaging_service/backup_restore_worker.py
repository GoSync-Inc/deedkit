"""Network-quarantined stdin restore helper. No cloud or producer authority."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

from . import backup
from .backup_crypto import decrypt_snapshot_stream
from .backup_r2_receiver import private_json, encoded
from .spool import fsync_dir

DATABASE = 'deedkit_verify_restore_test'


STAGES={'restore_decryption_failed','restore_database_failed','restore_snapshot_failed','restore_quarantine_failed'}


class RestoreStageError(RuntimeError):pass


def stage(code,operation):
    if code not in STAGES:raise ValueError('invalid_restore_stage')
    try:return operation()
    except Exception:raise RestoreStageError(code) from None


def restored_schema_version(store,manifest_version):
    """Bind the proof/quarantine policy to the actual restored metadata row."""
    with store.pool.connection() as connection:
        row=connection.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()
    actual=row.get('value') if row else None
    if (not isinstance(actual,str) or not actual.isascii() or not actual.isdigit() or len(actual)>3 or
            int(actual) not in range(2,48) or type(manifest_version) is not int or int(actual)!=manifest_version):
        raise ValueError('restored_schema_mismatch')
    return int(actual)


def assert_quarantined(store, version):
    """Check committed disabled state rather than trusting the UPDATE result."""
    predicates = {
        'account_policies': 'enabled', 'api_keys': 'NOT revoked',
        'collector_leases': 'expires_at > clock_timestamp()',
        'source_status': "status <> 'restored_disabled' OR coverage <> 'unknown' OR detail_code IS DISTINCT FROM 'requires_reconciliation'",
    }
    if version >= 22:
        predicates['account_policies'] += ' OR send_enabled'
        predicates['outgoing_messages'] = "status IN ('queued','dispatching')"
    if version >= 25:
        predicates['account_policies'] += " OR operations_enabled IS DISTINCT FROM '{}'::text[]"
        predicates['messenger_commands'] = "status IN ('queued','dispatching')"
    if version >= 26:predicates['subscriptions'] = "state <> 'closed'"
    if version >= 40:predicates['subscriptions'] += ' OR NOT retention_hold'
    if version >= 29:predicates['file_uploads'] = "state <> 'expired'"
    if version >= 32:
        predicates['subscription_receivers'] = 'enabled'
        predicates['subscription_outbox'] = "state IN ('pending','leased') OR lease_token IS NOT NULL OR lease_until IS NOT NULL"
        predicates['subscription_receipts'] = 'true'
    if version >= 33:predicates['attachment_backup_fences'] = 'ended_at IS NULL'
    if version >= 38:
        predicates['subscription_hint_windows'] = ("due_at IS NOT NULL OR silence_due_at IS NOT NULL OR "
            "silence_state IS DISTINCT FROM 'cancelled'")
    with store.pool.connection() as connection:
        if connection.execute('SELECT current_database() AS name').fetchone()['name'] != DATABASE:
            raise ValueError('not_restore_database')
        for table, predicate in predicates.items():
            if connection.execute('SELECT 1 FROM '+table+' WHERE '+predicate+' LIMIT 1').fetchone():
                raise RuntimeError('restore_quarantine_failed')


def run(config, source=None):
    required = {'v','identity_file','work_directory','database_host','database_password','snapshot_sha256','max_plaintext_bytes'}
    if (not isinstance(config,dict) or set(config)!=required or type(config['v']) is not int or config['v']!=1 or
            config['identity_file']!='/run/restore/identity' or config['work_directory']!='/work' or
            config['database_host']!='restore-postgres' or type(config['max_plaintext_bytes']) is not int or
            not 1 <= config['max_plaintext_bytes'] <= 128*1024**3 or
            not isinstance(config['database_password'],str) or len(config['database_password'])!=64 or
            not isinstance(config['snapshot_sha256'],str) or
            len(config['snapshot_sha256'])!=64 or any(c not in '0123456789abcdef' for c in config['snapshot_sha256'])):
        raise ValueError('invalid_restore_worker_config')
    source = sys.stdin.buffer if source is None else source
    work=Path(config['work_directory'])
    restored=stage('restore_decryption_failed',lambda:decrypt_snapshot_stream(source,config['identity_file'],work/'bundle',max_bytes=config['max_plaintext_bytes']))
    with (restored/'manifest.json').open('rb') as manifest_file:
        manifest_sha=hashlib.file_digest(manifest_file,'sha256').hexdigest()
    if manifest_sha != config['snapshot_sha256']:raise RuntimeError('restore_manifest_mismatch')
    manifest=stage('restore_snapshot_failed',lambda:backup.verify_bundle(restored))
    # Isolated DB name, host, user and arguments are fixed, never taken from the dump.
    env={**os.environ,'PGHOST':'restore-postgres','PGPORT':'5432','PGUSER':'restore_owner',
         'PGDATABASE':DATABASE,'PGPASSWORD':config['database_password'],'PGCONNECT_TIMEOUT':'10'}
    with tempfile.TemporaryFile() as errors:
        stage('restore_database_failed',lambda:subprocess.run(['pg_restore','--exit-on-error','--no-owner','--no-acl','--jobs=1',
                        '--dbname='+DATABASE,str(restored/'database.dump')],env=env,
                       stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=errors,check=True))
    from psycopg.conninfo import make_conninfo
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    dsn=make_conninfo(host='restore-postgres',port='5432',user='restore_owner',dbname=DATABASE,
                     password=config['database_password'],connect_timeout='10')
    with ConnectionPool(dsn,min_size=1,max_size=1,kwargs={'row_factory':dict_row},timeout=30) as pool:
        store=SimpleNamespace(pool=pool,files=restored/'files')
        actual_version=stage('restore_snapshot_failed',lambda:restored_schema_version(store,manifest['schema_version']))
        stage('restore_snapshot_failed',lambda:backup.verify_snapshot(store,restored))
        stage('restore_quarantine_failed',lambda:backup.quarantine_restore(store))
        stage('restore_quarantine_failed',lambda:assert_quarantined(store,actual_version))
    result={'v':1,'status':'verified','schema_version':actual_version,'manifest_sha256':manifest_sha,
            'checks':{name:True for name in ('decryption','bundle_integrity','database_restore','table_inventory',
                                            'original_hashes','recovery_objects','collectors_quarantined')}}
    # This is a helper result, NOT a trusted root proof. The host independently
    # binds cloud size/hash and copies only validated facts into separate control.
    target=work/'result.json'
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as out:out.write(encoded(result));out.flush();os.fsync(out.fileno())
    fsync_dir(work)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',required=True)
    args=parser.parse_args()
    try:run(private_json(args.config))
    except Exception as error:
        code=str(error) if isinstance(error,RestoreStageError) else 'isolated_restore_failed'
        try:
            fd=os.open('/work/failure.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as out:
                out.write(encoded({'v':1,'status':'failed','error':code}));out.flush();os.fsync(out.fileno())
            fsync_dir('/work')
        except Exception:pass
        print(code,file=sys.stderr);return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
