"""Family calendar capture (ANYM-605): one snapshot for the dump and a lossless catalog manifest,
plus the credential vault and the service profile files.

Runs in the backup image beside messaging's capture and shares only its encryption.
No provider call and no calendar code: the vault is copied and HMAC-checked, never decrypted.
"""
import base64
import binascii
from datetime import datetime,timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

import psycopg
from psycopg import sql
from psycopg.rows import dict_row,tuple_row

from .spool import fsync_dir

# Every client setting that changes a value's text form; set locally in the manifest transaction.
SESSION={'TimeZone':'UTC','DateStyle':'ISO','IntervalStyle':'postgres','extra_float_digits':'3',
         'search_path':'pg_catalog','lc_monetary':'C','bytea_output':'hex',
         'quote_all_identifiers':'off'}
PROFILE_FILES=('postgres.env','runtime.json','gateway.json','worker.json')
# runtime.json is written by the local bootstrap only; the compose profile needs the other three.
OPTIONAL_PROFILE_FILES={'runtime.json'}
TOKEN_FILE=re.compile(r'[a-z][a-z0-9_-]{0,47}\.enc')  # Registry.validate_id + '.enc'
STATE_MEMBER=re.compile(r'state/(?:vault/(?:key|[a-z][a-z0-9_-]{0,47}\.enc)|'
                        r'profile/(?:postgres\.env|runtime\.json|gateway\.json|worker\.json))')
MAX_STATE_BYTES=1024**2
FIRST_USER_OID=16384
RESTORE_MODE='verify_only_no_provider_calls'
MANIFEST_KEYS={'profile','format','created_at','captured_at','schema_version','tables','type_warnings',
               'dump_sha256','state_files','vault_warnings','vault_staging_skipped','restore_mode'}


class CalendarCaptureRefused(RuntimeError):
    """A capture that must not become a bundle; the previous protected point stays."""
    def __init__(self,code,subject):
        super().__init__(code);self.code=code;self.subject=subject


def rows_digest(texts):
    """Order-free multiset hash: sha256 of the sorted per-row sha256 digests; returns (rows, hex)."""
    digests=sorted(hashlib.sha256(t.encode()).digest() for t in texts)
    return len(digests),hashlib.sha256(b''.join(digests)).hexdigest()


def fernet_hmac_valid(key,token):
    """Fernet's HMAC-SHA256 over version|timestamp|iv|ciphertext under the key's signing half."""
    try:
        secret=base64.urlsafe_b64decode(key.strip());raw=base64.urlsafe_b64decode(token.strip())
    except (binascii.Error,ValueError):return False
    if len(secret)!=32 or len(raw)<1+8+16+32 or raw[0]!=0x80:return False
    return hmac.compare_digest(hmac.new(secret[:16],raw[:-32],'sha256').digest(),raw[-32:])


def _type_tested(oid,types,depth=0):
    t=types.get(oid)
    if t is None or depth>8:return False
    if t['typtype']=='b' and oid<FIRST_USER_OID:return True
    if t['typtype']=='b' and t['typcategory']=='A' and t['typelem']:return _type_tested(t['typelem'],types,depth+1)
    if t['typtype']=='e':return True
    if t['typtype']=='d':return _type_tested(t['typbasetype'],types,depth+1)
    return False


def calendar_manifest(c):
    """Every ordinary table from the catalog: rows and an order-free hash of each row's text form.

    Call inside the capture (or restore-check) transaction: the settings are transaction-local.
    An untested column type is hashed anyway and reported, never refused.
    """
    if c.autocommit:raise ValueError('manifest_requires_transaction')
    cur=c.cursor(row_factory=dict_row)
    for name,value in SESSION.items():cur.execute('SELECT set_config(%s,%s,true)',(name,value))
    tables=cur.execute('''SELECT c.oid,n.nspname,c.relname,format('%I.%I',n.nspname,c.relname) AS name
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind='r' AND c.relpersistence<>'t' AND n.nspname NOT IN ('pg_catalog','information_schema')
        AND n.nspname NOT LIKE 'pg\\_toast%' ''').fetchall()
    tables.sort(key=lambda t:t['name'])  # code-point order, independent of the database collation
    types={r['oid']:r for r in cur.execute('SELECT oid,typtype,typcategory,typelem,typbasetype FROM pg_type').fetchall()}
    columns=cur.execute('''SELECT attrelid,attname,atttypid,format_type(atttypid,atttypmod) AS type FROM pg_attribute
        WHERE attrelid=ANY(%s) AND attnum>0 AND NOT attisdropped ORDER BY attrelid,attnum''',
        ([t['oid'] for t in tables],)).fetchall()
    names={t['oid']:t['name'] for t in tables}
    warnings=sorted(({'table':names[r['attrelid']],'column':r['attname'],'type':r['type']}
                     for r in columns if not _type_tested(r['atttypid'],types)),
                    key=lambda w:(w['table'],w['column']))
    result={}
    for index,table in enumerate(tables):
        with c.cursor(name='calendar_manifest_%d'%index,row_factory=tuple_row) as rows:
            rows.itersize=2000
            rows.execute(sql.SQL('SELECT t::text FROM {} t').format(sql.Identifier(table['nspname'],table['relname'])))
            count,digest=rows_digest(text for (text,) in rows)
        result[table['name']]={'rows':count,'sha256':digest}
    return result,warnings


def calendar_schema_version(c):
    row=c.cursor(row_factory=dict_row).execute(
        "SELECT CASE WHEN to_regclass('fcs.migrations') IS NULL THEN NULL ELSE 1 END AS present").fetchone()
    if row['present'] is None:raise ValueError('calendar_schema_missing')
    version=c.cursor(row_factory=dict_row).execute('SELECT max(version) AS v FROM fcs.migrations').fetchone()['v']
    if version is None:raise ValueError('calendar_schema_missing')
    return int(version)


def calendar_connections(c):
    return {r['id']:r['enabled'] for r in c.cursor(row_factory=dict_row).execute(
        'SELECT id,enabled FROM fcs.connections').fetchall()}


def _open_dir(path,subject):
    try:return os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    except FileNotFoundError:return None
    except OSError:raise CalendarCaptureRefused('calendar_state_unsafe',subject) from None


def _read_entry(dir_fd,name):
    """Open without following links, check the held descriptor, read through it."""
    try:fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=dir_fd)
    except FileNotFoundError:return None
    except OSError:raise CalendarCaptureRefused('calendar_state_unsafe',name) from None
    with os.fdopen(fd,'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):raise CalendarCaptureRefused('calendar_state_unsafe',name)
        data=source.read(MAX_STATE_BYTES+1)
    if len(data)>MAX_STATE_BYTES:raise CalendarCaptureRefused('calendar_state_too_large',name)
    return data


def capture_state(vault_directory,profile_directory,destination,connections):
    """Copy the vault and profile files into destination; connections maps id -> enabled at the snapshot."""
    destination=Path(destination);destination.mkdir(mode=0o700)
    try:
        vault={};unknown=[];skipped=0;profile={};warnings=[]
        dir_fd=_open_dir(vault_directory,'vault')
        if dir_fd is not None:
            try:
                for name in sorted(os.listdir(dir_fd)):
                    if name.startswith('.tmp-'):skipped+=1;continue  # the writer's staging, never committed
                    if name!='key' and not TOKEN_FILE.fullmatch(name):unknown.append(name);continue
                    data=_read_entry(dir_fd,name)
                    if data is not None:vault[name]=data
            finally:os.close(dir_fd)
        dir_fd=_open_dir(profile_directory,'profile')
        try:
            for name in PROFILE_FILES:
                data=_read_entry(dir_fd,name) if dir_fd is not None else None
                if data is not None:profile[name]=data
                elif name in OPTIONAL_PROFILE_FILES:warnings.append({'kind':'profile_file_absent','name':name})
                else:raise CalendarCaptureRefused('calendar_profile_incomplete',name)
        finally:
            if dir_fd is not None:os.close(dir_fd)
        tokens={name[:-4] for name in vault if name!='key'}
        for connection,enabled in sorted(connections.items()):
            if connection in tokens:continue
            if enabled:raise CalendarCaptureRefused('calendar_vault_incomplete',connection)
            warnings.append({'kind':'disabled_connection_without_token','name':connection})
        if tokens and 'key' not in vault:raise CalendarCaptureRefused('calendar_vault_key_missing','key')
        for connection in sorted(tokens):
            if not fernet_hmac_valid(vault['key'],vault[connection+'.enc']):
                raise CalendarCaptureRefused('calendar_vault_key_mismatch',connection+'.enc')
            if connection not in connections:warnings.append({'kind':'token_without_connection','name':connection})
        warnings+=[{'kind':'unknown_entry','name':name} for name in unknown]
        files={}
        for folder,entries in (('vault',vault),('profile',profile)):
            if not entries:continue
            (destination/folder).mkdir(mode=0o700)
            for name,data in sorted(entries.items()):
                fd=os.open(destination/folder/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
                files[folder+'/'+name]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
            fsync_dir(destination/folder)
        fsync_dir(destination)
        return {'state_files':files,'vault_warnings':warnings,'vault_staging_skipped':skipped}
    except BaseException:
        shutil.rmtree(destination,ignore_errors=True);raise


def write_calendar_manifest(directory,*,captured_at,schema_version,tables,type_warnings,state):
    directory=Path(directory)
    with (directory/'database.dump').open('rb') as dump:dump_sha=hashlib.file_digest(dump,'sha256').hexdigest()
    manifest={'profile':'calendar','format':1,'created_at':datetime.now(timezone.utc).isoformat(),
              'captured_at':captured_at,'schema_version':schema_version,'tables':tables,
              'type_warnings':type_warnings,'dump_sha256':dump_sha,**state,'restore_mode':RESTORE_MODE}
    with (directory/'manifest.json').open('x') as out:
        out.write(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n');out.flush();os.fsync(out.fileno())
    return manifest


def create_calendar_snapshot(dsn,destination,dump_command,*,vault_directory,profile_directory,exported_callback=None):
    """One REPEATABLE READ snapshot for the schema version, connections, manifest and pg_dump; state after it."""
    destination=Path(destination)
    if destination.exists():raise ValueError('backup_destination_exists')
    if not dump_command or not all(isinstance(x,str) for x in dump_command):raise ValueError('invalid_dump_command')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=Path(tempfile.mkdtemp(prefix='.calendar-backup-',dir=destination.parent))
    try:
        with psycopg.connect(dsn,row_factory=dict_row) as c:
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            c.execute('SET LOCAL statement_timeout=0')
            exported=c.execute('SELECT pg_export_snapshot() AS id,clock_timestamp() AS captured_at').fetchone()
            if exported_callback is not None:exported_callback()
            schema_version=calendar_schema_version(c)
            connections=calendar_connections(c)
            tables,type_warnings=calendar_manifest(c)
            with (temporary/'database.dump').open('xb') as output:
                # Operator-configured command, no shell; the dump reads the exported snapshot.
                process=subprocess.run([*dump_command,'--format=custom','--no-owner','--no-acl',
                    '--snapshot='+exported['id']],stdout=output,stderr=subprocess.PIPE,timeout=600)
                if process.returncode:raise RuntimeError('pg_dump_failed')
                output.flush();os.fsync(output.fileno())
            c.rollback()
        # A connection row in the snapshot had its token saved before it (credential first, row second).
        state=capture_state(vault_directory,profile_directory,temporary/'state',connections)
        write_calendar_manifest(temporary,captured_at=exported['captured_at'].isoformat(),schema_version=schema_version,
                                tables=tables,type_warnings=type_warnings,state=state)
        fsync_dir(temporary);os.rename(temporary,destination);fsync_dir(destination.parent)
        return {'status':'snapshot_created','profile':'calendar','tables':len(tables),
                'state_files':len(state['state_files'])}
    finally:
        if temporary.exists():shutil.rmtree(temporary)  # Only this function's temporary directory.


def valid_calendar_member(name):
    return name in ('manifest.json','database.dump') or bool(STATE_MEMBER.fullmatch(name))


def _sha_regular(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):raise ValueError('invalid_calendar_bundle')
        return hashlib.file_digest(source,'sha256').hexdigest()


def verify_calendar_bundle(directory):
    """Structure and hashes of a calendar bundle; restore checks belong to the verifier (ANYM-606)."""
    from .recovery_state import read_manifest
    directory=Path(directory);manifest=read_manifest(directory)
    if (not isinstance(manifest,dict) or set(manifest)!=MANIFEST_KEYS or manifest['profile']!='calendar' or
        manifest['format']!=1 or type(manifest['schema_version']) is not int or manifest['restore_mode']!=RESTORE_MODE or
        not isinstance(manifest['tables'],dict) or not isinstance(manifest['state_files'],dict) or
        not all(isinstance(k,str) and STATE_MEMBER.fullmatch('state/'+k) and isinstance(v,dict) and
                set(v)=={'bytes','sha256'} for k,v in manifest['state_files'].items())):
        raise ValueError('invalid_calendar_bundle')
    entries={p.name for p in directory.iterdir()}
    if not {'manifest.json','database.dump'}<=entries<={'manifest.json','database.dump','state'}:
        raise ValueError('invalid_calendar_bundle')
    if _sha_regular(directory/'database.dump')!=manifest['dump_sha256']:raise RuntimeError('dump_corrupt')
    found={}
    state=directory/'state'
    if 'state' in entries:
        if state.is_symlink() or not state.is_dir():raise ValueError('invalid_calendar_bundle')
        for folder in state.iterdir():
            if folder.is_symlink() or not folder.is_dir() or folder.name not in ('vault','profile'):
                raise ValueError('invalid_calendar_bundle')
            for path in folder.iterdir():
                name=folder.name+'/'+path.name
                if not STATE_MEMBER.fullmatch('state/'+name):raise ValueError('invalid_calendar_bundle')
                found[name]=_sha_regular(path)
    if found!={k:v['sha256'] for k,v in manifest['state_files'].items()}:raise RuntimeError('state_corrupt')
    return manifest
