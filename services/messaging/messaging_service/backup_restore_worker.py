"""Network-quarantined stdin restore helper. No cloud or producer authority."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace

from . import backup
from .backup_crypto import decrypt_snapshot_stream
from .backup_r2_receiver import MAX_WARNINGS, WARNING_BYTES, private_json, encoded
from .spool import fsync_dir

DATABASE = 'deedkit_verify_restore_test'
# Every role a migration or role file names. pg_restore --no-acl skips GRANTs but not row-level security
# policies (050: `CREATE POLICY ... TO messaging_gateway`), so the fresh isolated cluster gets each as a NOLOGIN
# role before the restore. A fixed list, never read from the dump; tests/test_backup_restore_roles.py checks
# every migration and role file against it.
RESTORE_ROLES=('messaging_cold_archive','messaging_collector','messaging_enricher','messaging_event_delivery',
               'messaging_file_lifecycle','messaging_gateway','messaging_indexer','messaging_purge','messaging_subscriptions')
# The calendar's roles (services/calendar/family_calendar/service/roles.sql). No calendar policy names one today;
# the same fixed treatment keeps a future one restorable (tests/test_backup_restore_roles.py checks the files).
CALENDAR_RESTORE_ROLES=('fcs_gateway','fcs_worker')
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


STAGES={'restore_decryption_failed','restore_database_failed','restore_snapshot_failed','restore_quarantine_failed',
        # The calendar profile (ANYM-606): one code per check after the shared ones, and a bundle of the other profile.
        'restore_bundle_failed','restore_schema_failed','restore_inventory_failed','restore_vault_incomplete',
        'restore_vault_decrypt_failed','verifier_profile_mismatch'}
CALENDAR_CHECKS=('decryption','bundle_integrity','database_restore','schema_version','table_inventory',
                 'vault_complete','vault_decrypts')


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


def restore_roles_sql(roles=RESTORE_ROLES):
    """One statement creating each fixed restore role NOLOGIN, from constants only."""
    if roles not in (RESTORE_ROLES,CALENDAR_RESTORE_ROLES) or not all(IDENTIFIER.fullmatch(role) for role in roles):
        raise ValueError('invalid_restore_role')
    return 'DO $$BEGIN\n'+''.join("IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='"+role+"') THEN CREATE ROLE "+role+
                                  ' NOLOGIN; END IF;\n' for role in roles)+'END$$'


def restored_schema_version(store,manifest_version):
    """Bind the proof/quarantine policy to the actual restored metadata row."""
    with store.pool.connection() as connection:
        row=connection.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()
    actual=row.get('value') if row else None
    if (not isinstance(actual,str) or not actual.isascii() or not actual.isdigit() or len(actual)>3 or
            int(actual) not in backup.SCHEMAS or type(manifest_version) is not int or int(actual)!=manifest_version):
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


UNREADABLE=object()
# verify_calendar_bundle's errors. decrypt_snapshot_stream raises them only after age authenticated the whole
# stream (backup_crypto._decrypt_snapshot), so they are integrity failures, not decryption ones.
CALENDAR_BUNDLE_ERRORS={'invalid_calendar_bundle','dump_corrupt','state_corrupt'}


def bundle_profile(path):
    """The manifest's profile: absent is messaging, any present value (null too) is itself; UNREADABLE for a
    manifest that is no JSON object, which verification then refuses."""
    try:
        with path.open('rb') as manifest:value=json.load(manifest)
    except (OSError,ValueError):return UNREADABLE
    if not isinstance(value,dict):return UNREADABLE
    return value['profile'] if 'profile' in value else 'messaging'


def proof_warnings(warnings):
    """Labels only (kind and a name cut to 64 characters); past MAX_WARNINGS the rest becomes one count."""
    if not isinstance(warnings,list) or not all(isinstance(w,dict) and set(w)=={'kind','name'} and
                                                isinstance(w['kind'],str) and isinstance(w['name'],str) for w in warnings):
        raise ValueError('invalid_calendar_bundle')
    items=[{'kind':w['kind'],'name':w['name'][:64]} for w in warnings]
    def kept(count):
        return items[:count]+([{'kind':'warnings_truncated','name':str(len(items))}] if count<len(items) else [])
    count=len(items) if len(items)<=MAX_WARNINGS else MAX_WARNINGS-1
    # Count and characters do not bound bytes (an escaped emoji is 12): drop from the end until it fits.
    while len(encoded(kept(count)))>WARNING_BYTES:count-=1
    return kept(count)


def calendar_vault_decrypts(restored,manifest):
    """Every bundled token decrypts under the bundled key (HMAC alone proves only the signing half).

    The plaintext stays in memory, is parsed only to tell a JSON object from anything else, and is never
    written or logged; the warning names the connection, never a value."""
    from cryptography.fernet import Fernet
    tokens=sorted(name for name in manifest['state_files'] if name.startswith('vault/') and name.endswith('.enc'))
    if not tokens:return []
    cipher=Fernet((restored/'state/vault/key').read_bytes())
    warnings=[]
    for name in tokens:
        plaintext=cipher.decrypt((restored/'state'/name).read_bytes())
        try:shaped=isinstance(json.loads(plaintext),dict)
        except (ValueError,RecursionError):shaped=False
        del plaintext
        if not shaped:warnings.append({'kind':'token_not_json_object','name':name[len('vault/'):-len('.enc')]})
    return warnings


def restore_calendar(restored,manifest_sha,*,tool,env,dsn,database):
    """V2 checks after decryption on a decrypted calendar bundle; `tool(name)` gives psql/pg_restore their fixed target."""
    import psycopg
    from psycopg.rows import dict_row
    from .backup_calendar import calendar_connections,calendar_manifest,calendar_schema_version,verify_calendar_bundle
    manifest=stage('restore_bundle_failed',lambda:verify_calendar_bundle(restored))
    stage('restore_bundle_failed',lambda:proof_warnings(manifest['vault_warnings']))
    warnings=list(manifest['vault_warnings'])
    stage('restore_database_failed',lambda:subprocess.run([*tool('psql'),'-X','-q','-v','ON_ERROR_STOP=1','-c',
                                                            restore_roles_sql(CALENDAR_RESTORE_ROLES)],
                       env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True))
    with tempfile.TemporaryFile() as errors,(restored/'database.dump').open('rb') as dump:
        try:stage('restore_database_failed',lambda:subprocess.run([*tool('pg_restore'),'--exit-on-error','--no-owner','--no-acl',
                        '--jobs=1'],env=env,stdin=dump,stdout=subprocess.DEVNULL,stderr=errors,check=True))
        except RestoreStageError as error:
            errors.seek(0);error.detail=restore_detail(errors.read(64*1024));raise
    def schema(c):
        if calendar_schema_version(c)!=manifest['schema_version']:raise ValueError('restored_schema_mismatch')
        return manifest['schema_version']
    def inventory(c):
        if calendar_manifest(c)[0]!=manifest['tables']:raise ValueError('restored_inventory_mismatch')
    with psycopg.connect(dsn,row_factory=dict_row,connect_timeout=10) as c:
        if c.execute('SELECT current_database() AS name').fetchone()['name']!=database or not database.endswith('_restore_test'):
            raise ValueError('not_restore_database')
        version=stage('restore_schema_failed',lambda:schema(c))
        stage('restore_inventory_failed',lambda:inventory(c))
        connections=stage('restore_vault_incomplete',lambda:calendar_connections(c))
        c.rollback()
    def complete():
        missing=[name for name,enabled in connections.items() if enabled and 'vault/'+name+'.enc' not in manifest['state_files']]
        if missing:raise ValueError('restored_vault_incomplete')
    stage('restore_vault_incomplete',complete)
    warnings+=stage('restore_vault_decrypt_failed',lambda:calendar_vault_decrypts(restored,manifest))
    return {'v':1,'status':'verified','profile':'calendar','schema_version':version,'manifest_sha256':manifest_sha,
            'checks':{name:True for name in CALENDAR_CHECKS},
            'vault_warnings':proof_warnings(warnings)}


def run(config, source=None):
    required = {'v','identity_file','work_directory','database_host','database_password','snapshot_sha256','max_plaintext_bytes'}
    # A calendar worker config (ANYM-606) adds `profile: calendar`; a messaging one is unchanged.
    profile = 'calendar' if isinstance(config,dict) and 'profile' in config else 'messaging'
    if profile=='calendar':
        if config['profile']!='calendar':raise ValueError('invalid_restore_worker_config')
        required = required | {'profile'}
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
    try:restored=decrypt_snapshot_stream(source,config['identity_file'],work/'bundle',max_bytes=config['max_plaintext_bytes'])
    except Exception as error:
        code='restore_bundle_failed' if profile=='calendar' and str(error) in CALENDAR_BUNDLE_ERRORS else 'restore_decryption_failed'
        raise RestoreStageError(code) from None
    with (restored/'manifest.json').open('rb') as manifest_file:
        manifest_sha=hashlib.file_digest(manifest_file,'sha256').hexdigest()
    if manifest_sha != config['snapshot_sha256']:raise RuntimeError('restore_manifest_mismatch')
    found=bundle_profile(restored/'manifest.json')
    if found is not UNREADABLE and found!=profile:raise RestoreStageError('verifier_profile_mismatch')
    # Isolated DB name, host, user and arguments are fixed, never taken from the dump.
    env={**os.environ,'PGHOST':'restore-postgres','PGPORT':'5432','PGUSER':'restore_owner',
         'PGDATABASE':DATABASE,'PGPASSWORD':config['database_password'],'PGCONNECT_TIMEOUT':'10'}
    if profile=='calendar':
        from psycopg.conninfo import make_conninfo
        result=restore_calendar(restored,manifest_sha,tool=lambda name:[name,'--dbname='+DATABASE],env=env,database=DATABASE,
                                dsn=make_conninfo(host='restore-postgres',port='5432',user='restore_owner',dbname=DATABASE,
                                                  password=config['database_password'],connect_timeout='10'))
        return write_result(work,result)
    # Originals were hashed while extracting; the dump is read back here, before pg_restore (ANYM-630).
    extracted=getattr(restored,'extracted',None); digests={} if extracted is None else {'extracted':extracted}
    manifest=stage('restore_snapshot_failed',lambda:backup.verify_bundle(restored,**digests))
    # The fresh isolated cluster has only restore_owner: the roles a policy names come first, from RESTORE_ROLES.
    stage('restore_database_failed',lambda:subprocess.run(['psql','-X','-q','-v','ON_ERROR_STOP=1','-c',restore_roles_sql()],
                       env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True))
    with tempfile.TemporaryFile() as errors:
        try:stage('restore_database_failed',lambda:subprocess.run(['pg_restore','--exit-on-error','--no-owner','--no-acl','--jobs=2',
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
        stage('restore_snapshot_failed',lambda:backup.verify_snapshot(store,restored,**digests))
        stage('restore_quarantine_failed',lambda:backup.quarantine_restore(store))
        stage('restore_quarantine_failed',lambda:assert_quarantined(store,actual_version))
    result={'v':1,'status':'verified','schema_version':actual_version,'manifest_sha256':manifest_sha,
            'checks':{name:True for name in ('decryption','bundle_integrity','database_restore','table_inventory',
                                            'original_hashes','recovery_objects','collectors_quarantined')}}
    return write_result(work,result)


def write_result(work,result):
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
        print(write_failure(Path('/work'),error),file=sys.stderr);return 1
    return 0


def write_failure(work,error):
    """The failure fact the verifier reads (backup_r2_verifier.helper_failure): a fixed stage code only."""
    code=str(error) if isinstance(error,RestoreStageError) else 'isolated_restore_failed'
    # A separate file keeps failure.json's exact shape for verifiers that predate the detail.
    if isinstance(error,RestoreStageError) and error.detail:
        try:
            fd=os.open(work/'failure-detail.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as out:
                out.write(encoded({'v':1,'detail':error.detail}));out.flush();os.fsync(out.fileno())
        except Exception:pass
    try:
        fd=os.open(work/'failure.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as out:
            out.write(encoded({'v':1,'status':'failed','error':code}));out.flush();os.fsync(out.fileno())
        fsync_dir(work)
    except Exception:pass
    return code


if __name__=='__main__':raise SystemExit(main())
