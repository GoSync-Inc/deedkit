"""Network-quarantined stdin restore helper. No cloud or producer authority."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace

from . import backup
from .backup_crypto import decrypt_snapshot_stream
from .backup_r2_receiver import private_json, encoded
from .spool import fsync_dir

DATABASE = 'deedkit_verify_restore_test'
# Every role a migration or role file names. pg_restore --no-acl skips GRANTs but not row-level security
# policies (050: `CREATE POLICY ... TO messaging_gateway`), so the fresh isolated cluster gets each as a NOLOGIN
# role before the restore. A fixed list, never read from the dump; tests/test_backup_restore_roles.py checks
# every migration and role file against it.
RESTORE_ROLES=('messaging_cold_archive','messaging_collector','messaging_enricher','messaging_event_delivery',
               'messaging_file_lifecycle','messaging_gateway','messaging_indexer','messaging_purge','messaging_subscriptions')
IDENTIFIER=re.compile(r'[a-z_][a-z0-9_]{0,62}')
# pg_restore's first error line becomes one fixed phrase. Only words from these fixed alternatives are kept, never
# a name: a row value can reach a quoted name (a regrole value in restored INSERT data names a missing role), so
# no message text can enter the detail (backup_r2_verifier.DETAIL_SHAPE).
OBJECT=r'(role|relation|schema|extension|type|table|column|constraint|index|policy|trigger|sequence|view|database)'
QUERY='could not execute query: ERROR:  '
DETAIL_TEMPLATES=(
    re.compile(QUERY+OBJECT+r' ".*" (does not exist|already exists)'),
    re.compile(QUERY+r'(extension) ".*" (is not available)'),
    re.compile(r'(COPY failed) for table ".*": ERROR:  .*'),
    re.compile(r'(?:'+QUERY+r')?(permission denied|out of memory|could not extend file|could not write to file|'
               r'deadlock detected|canceling statement due to (?:statement timeout|user request))\b.*'),
)


STAGES={'restore_decryption_failed','restore_database_failed','restore_snapshot_failed','restore_quarantine_failed'}


class RestoreStageError(RuntimeError):detail=''


def stage(code,operation):
    if code not in STAGES:raise ValueError('invalid_restore_stage')
    try:return operation()
    except Exception:raise RestoreStageError(code) from None


def restore_detail(raw):
    """The first `pg_restore: error:` line as a fixed template, else `unclassified pg_restore error`; '' without one.
    With --exit-on-error that line is the failure; later lines (SQL, DETAIL, CONTEXT, a value's own newlines) are never read."""
    for line in raw.decode('utf-8','replace').split('\n'):
        if line.startswith('pg_restore: error: '):
            message=line[len('pg_restore: error: '):]
            for template in DETAIL_TEMPLATES:
                if match:=template.fullmatch(message):return ' '.join(match.groups())
            return 'unclassified pg_restore error'
    return ''


def restore_roles_sql():
    """One statement creating each fixed restore role NOLOGIN, from constants only."""
    if not all(IDENTIFIER.fullmatch(role) for role in RESTORE_ROLES):raise ValueError('invalid_restore_role')
    return 'DO $$BEGIN\n'+''.join("IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='"+role+"') THEN CREATE ROLE "+role+
                                  ' NOLOGIN; END IF;\n' for role in RESTORE_ROLES)+'END$$'


def restored_schema_version(store,manifest_version):
    """Bind the proof/quarantine policy to the actual restored metadata row."""
    with store.pool.connection() as connection:
        row=connection.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()
    actual=row.get('value') if row else None
    if (not isinstance(actual,str) or not actual.isascii() or not actual.isdigit() or len(actual)>3 or
            int(actual) not in range(2,56) or type(manifest_version) is not int or int(actual)!=manifest_version):
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
    # The fresh isolated cluster has only restore_owner: the roles a policy names come first, from RESTORE_ROLES.
    stage('restore_database_failed',lambda:subprocess.run(['psql','-X','-q','-v','ON_ERROR_STOP=1','-c',restore_roles_sql()],
                       env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True))
    with tempfile.TemporaryFile() as errors:
        try:stage('restore_database_failed',lambda:subprocess.run(['pg_restore','--exit-on-error','--no-owner','--no-acl','--jobs=1',
                        '--dbname='+DATABASE,str(restored/'database.dump')],env=env,
                       stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=errors,check=True))
        except RestoreStageError as error:
            errors.seek(0);error.detail=restore_detail(errors.read(64*1024));raise
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
        # A separate file keeps failure.json's exact shape for verifiers that predate the detail.
        if isinstance(error,RestoreStageError) and error.detail:
            try:
                fd=os.open('/work/failure-detail.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'wb') as out:
                    out.write(encoded({'v':1,'detail':error.detail}));out.flush();os.fsync(out.fileno())
            except Exception:pass
        try:
            fd=os.open('/work/failure.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as out:
                out.write(encoded({'v':1,'status':'failed','error':code}));out.flush();os.fsync(out.fileno())
            fsync_dir('/work')
        except Exception:pass
        print(code,file=sys.stderr);return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
