"""Operator-only consistent PG dump + referenced immutable blobs and verification.

pg_dump receives an exported snapshot from the same transaction used for the
manifest. Never mount this operator capability into the public gateway.
"""
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import uuid

from psycopg import sql

from .blobs import Blob,BlobWriter
from .envelope import canonical
from .spool import fsync_dir
from .store import serial as serial

# Explicit tables and deterministic ordering; no SQL/identifiers supplied by agents.
TABLES={'probe_metadata':'name','archive_migrations':'version','accounts':'id','principals':'id',
    'api_keys':'digest','chats':'id','messages':'id','attachments':'id','feed_counter':'id',
    'events':'id','consumers':'principal,name,scope_version','deliveries':'digest','download_grants':'digest',
    'account_policies':'account','collector_leases':'account','source_status':'account',
    'ingest_receipts':'digest','message_revisions':'message,revision','media_jobs':'id'}
V3_TABLES={'collector_chats':'account,provider_chat','provider_tombstones':'account,provider_chat,provider_id'}
V4_TABLES={'whatsapp_checkpoints':'account,provider_chat'}
V5_TABLES={'audit_log':'occurred_at,id'}
V6_TABLES={'review_counter':'id','reviews':'position','review_sources':'review,message'}
V8_TABLES={'sender_profiles':'id'}
V9_TABLES={'attachment_text':'attachment'}
V14_TABLES={'collector_health':'account'}
V16_TABLES={'attachment_text_chunks':'attachment,chunk_index','attachment_enrichment_chunks':'attachment,chunk_index'}
V18_TABLES={'processing_profiles':'account,kind'}
V19_TABLES={'whatsapp_identity_observations':'account,source_message,identity_id,pn,lid,provenance'}
V20_TABLES={'processing_budget':'day,account,kind,profile_hash',
           'processing_control':'account,kind,profile_hash','processing_resource_leases':'resource'}
V21_TABLES={'media_reservations':'job'}
V22_TABLES={'outgoing_messages':'id'}
V23_TABLES={'message_receipts':'id','receipt_event_jobs':'receipt','receipt_event_publications':'message'}
V25_TABLES={'messenger_commands':'id'}
V27_TABLES={'chat_read_observations':'chat','reply_dismissals':'key_id,chat'}
V29_TABLES={'file_uploads':'id'}
V32_TABLES={'subscription_consumption':'subscription','subscription_receipts':'receipt_hash',
    'subscription_receivers':'id','subscription_delivery':'subscription','subscription_outbox':'id',
    'agent_feedback':'id'}
V33_TABLES={'attachment_residency':'digest','attachment_eviction_journal':'id','attachment_backup_fences':'id'}
V34_TABLES={'attachment_understanding':'attachment','file_understanding_profiles':'account,kind'}
V35_TABLES={'attachment_cold_copies':'digest'}
V37_TABLES={'collector_account_roles':'role_name'}
V38_TABLES={'subscription_hint_windows':'subscription'}
V41_TABLES={'reply_snoozes':'key_id,chat','reply_mutes':'id'}
V42_TABLES={'owner_message_reads':'chat,provider_id'}
V43_TABLES={'person_links':'account,sender'}
V44_TABLES={'api_key_operations':'operation_id'}
V45_TABLES={'message_purges':'message','chat_tombstones':'id','ingest_receipt_links':'digest','purge_state':'name'}
V48_TABLES={'subscription_dispatcher_heartbeat':'id'}
V49_TABLES={'delivery_endpoints':'id','delivery_endpoint_tests':'id'}
V50_TABLES={'component_heartbeats':'component'}
V51_TABLES={'send_previews':'token_hash','send_intents':'id'}
V53_TABLES={'person_preferences':'person,purpose'}
# ANYM-562: the module journal, its counter, each subscription's module sources, stream positions and
# matches. module_source_health is derived router state (self-clearing), like source_health_journal.
V56_TABLES={'module_event_counter':'id','module_events':'seq','subscription_module_sources':'subscription,source,scope_ref',
    'subscription_module_streams':'subscription','subscription_module_matches':'subscription,event'}
V57_TABLES={'conflict_reevaluations':'job,job_version,account'}
# Every schema this runtime can capture, verify and restore (one ceiling for capture and verify_bundle).
# ANYM-575: the router's cursors and open or resolved gaps, consistent with module_events in one snapshot.
V58_TABLES={'module_router_cursors':'source','module_router_gaps':'id'}
# ANYM-520: each endpoint hint attempt, kept 7 days (no body, header value or URL).
V61_TABLES={'delivery_attempts':'id'}
SCHEMAS=range(2,63)
V26_TABLES={'subscriptions':'id','subscription_sources':'subscription,account,chat',
            'subscription_matches':'subscription,event'}
V17_TABLES={'message_conflicts':'receipt_digest'}
V15_TABLES={'message_metadata':'message','attachment_enrichment':'attachment','enrichment_budget':'day'}


def database_manifest(c):
    tables={}
    version=int(c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'])
    if version not in SCHEMAS: raise ValueError('unsupported_backup_schema')
    for name,order in {**TABLES,**(V3_TABLES if version>=3 else {}),**(V4_TABLES if version>=4 else {}),
                       **(V5_TABLES if version>=5 else {}),**(V6_TABLES if version>=6 else {}),
                       **(V8_TABLES if version>=8 else {}),**(V9_TABLES if version>=9 else {}),
                       **(V14_TABLES if version>=14 else {}),**(V15_TABLES if version>=15 else {}),**(V16_TABLES if version>=16 else {}),**(V17_TABLES if version>=17 else {}),**(V18_TABLES if version>=18 else {}),
                       **(V19_TABLES if version>=19 else {}),**(V20_TABLES if version>=20 else {}),
                       **(V21_TABLES if version>=21 else {}),**(V22_TABLES if version>=22 else {}),
                       **(V23_TABLES if version>=23 else {}),**(V25_TABLES if version>=25 else {}),
                       **(V26_TABLES if version>=26 else {}),**(V27_TABLES if version>=27 else {}),
                       **(V29_TABLES if version>=29 else {}),**(V32_TABLES if version>=32 else {}),
                       **(V33_TABLES if version>=33 else {}),**(V34_TABLES if version>=34 else {}),
                       **(V35_TABLES if version>=35 else {}),
                       **(V37_TABLES if version>=37 else {}),
                       **(V38_TABLES if version>=38 else {}),**(V41_TABLES if version>=41 else {}),**(V42_TABLES if version>=42 else {}),**(V43_TABLES if version>=43 else {}),
                       **(V44_TABLES if version>=44 else {}),**(V45_TABLES if version>=45 else {}),**(V48_TABLES if version>=48 else {}),**(V49_TABLES if version>=49 else {}),**(V50_TABLES if version>=50 else {}),**(V51_TABLES if version>=51 else {}),**(V53_TABLES if version>=53 else {}),**(V56_TABLES if version>=56 else {}),**(V57_TABLES if version>=57 else {}),**(V58_TABLES if version>=58 else {}),**(V61_TABLES if version>=61 else {})}.items():
        query=sql.SQL('SELECT row_to_json(t) AS record FROM {} t ORDER BY {}').format(
            sql.Identifier(name),sql.SQL(',').join(sql.Identifier(part) for part in order.split(',')))
        sha=hashlib.sha256(); count=0
        with c.cursor(name='backup_'+name) as cursor:
            cursor.execute(query)
            for row in cursor:
                sha.update(canonical(row['record'])+b'\n'); count+=1
        tables[name]={'rows':count,'sha256':sha.hexdigest()}
    return tables


def blob_inventory(c,version):
    """Only a completed, matching lifecycle journal may omit an original."""
    if version<33:
        return c.execute('SELECT DISTINCT digest,bytes FROM attachments ORDER BY digest').fetchall(),[]
    # Schema 35 names each absent original's verified cold copy (ANYM-297).
    cold=('''LEFT JOIN attachment_cold_copies cc ON cc.digest=a.digest AND cc.bytes=a.bytes AND cc.state='verified' ''' if version>=35
          else '')
    rows=c.execute('''SELECT DISTINCT a.digest,a.bytes,r.state,r.bytes AS residency_bytes,r.eviction_id,
        j.state AS eviction_state,j.digest AS eviction_digest,j.bytes AS eviction_bytes'''+
        (',cc.object_key AS cold_object_key ' if version>=35 else ' ')+
        '''FROM attachments a LEFT JOIN attachment_residency r ON r.digest=a.digest
        LEFT JOIN attachment_eviction_journal j ON j.id=r.eviction_id '''+cold+'ORDER BY a.digest,a.bytes').fetchall()
    present=[];absent=[];seen=set()
    for row in rows:
        digest=row['digest'];size=row['bytes']
        if digest in seen or (row['residency_bytes'] is not None and row['residency_bytes']!=size):
            raise RuntimeError('backup_blob_inventory_mismatch')
        seen.add(digest)
        if row['state']=='evicting':raise RuntimeError('backup_eviction_in_progress')
        if row['state']=='evicted':
            if (row['eviction_id'] is None or row['eviction_state']!='completed' or
                row['eviction_digest']!=digest or row['eviction_bytes']!=size):
                raise RuntimeError('backup_absence_unproven')
            item={'digest':digest,'bytes':size,'eviction_id':str(row['eviction_id'])}
            # Only a verified cold copy is named; older evictions keep the schema-33 shape.
            if version>=35 and row['cold_object_key']:item['cold_object_key']=row['cold_object_key']
            absent.append(item)
        else:present.append({'digest':digest,'bytes':size})
    return present,absent


def validate_reference_fence(store,manifest,token):
    """A reusable capture requires its original, still-active durable fence."""
    with store.pool.connection() as c:
        version=int(c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'])
        if version<33:return
        if manifest.get('schema_version',0)<33 or manifest.get('backup_fence')!=token:
            raise ValueError('backup_capture_unfenced')
        row=c.execute('SELECT ended_at FROM attachment_backup_fences WHERE id=%s',(token,)).fetchone()
        if row is None or row['ended_at'] is not None:raise ValueError('backup_capture_unfenced')


def create_snapshot(store,destination,dump_command,recovery_config=None,*,captured_callback=None,
                    reference_blobs=False,backup_fence=None,dump_timeout=600):
    """Release cold-state locks after the DB dump; immutable blob copying may continue."""
    from contextlib import nullcontext
    from .recovery_state import recovery_guard
    guard=recovery_guard(store,recovery_config,destination) if recovery_config is not None else nullcontext(None)
    destination=Path(destination)
    if type(reference_blobs) is not bool:raise ValueError('invalid_blob_reference_mode')
    if destination.exists(): raise ValueError('backup_destination_exists')
    if not dump_command or not all(isinstance(x,str) for x in dump_command): raise ValueError('invalid_dump_command')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=Path(tempfile.mkdtemp(prefix='.backup-',dir=destination.parent))
    fence=None;fence_committed=False
    try:
        with store.pool.connection() as c:
            version=int(c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'])
            if version>=33:
                from .file_lifecycle import begin_backup_fence
                fence=backup_fence or 'backup-'+uuid.uuid4().hex
                begin_backup_fence(c,fence)
        fence_committed=fence is not None
        # The fence transaction commits before exporting the point-in-time DB.
        # It survives connection loss, process death, and encryption/upload retry.
        recovery=None
        with guard as recovery_sources:
            if recovery_sources is not None:
                from .recovery_state import snapshot_sources
                recovery=snapshot_sources(recovery_sources,temporary/'recovery')
            with store.pool.connection() as c:
                c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                c.execute('SET LOCAL statement_timeout=0')
                exported=c.execute('SELECT pg_export_snapshot() AS id,clock_timestamp() AS captured_at').fetchone()
                snapshot=exported['id'];captured_at=exported['captured_at'].isoformat()
                if recovery is not None:
                    from .recovery_state import verify_account_coverage
                    verify_account_coverage(c,{s['account'] for s in recovery['sources'] if s['kind']=='session'})
                tables=database_manifest(c)
                schema_version=int(c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'])
                if schema_version>=33 and fence is None:raise RuntimeError('backup_capture_unfenced')
                blobs,absent=blob_inventory(c,schema_version)
                with (temporary/'database.dump').open('xb') as output:
                    # Command is configured by the operator, not accepted by MCP/REST. No shell.
                    # Restoring needs pg_restore from PostgreSQL 18 built with zstd, as the image (BACKUP.md).
                    process=subprocess.run([*dump_command,'--format=custom','--compress=zstd:1','--no-owner','--no-acl',
                        '--snapshot='+snapshot],stdout=output,stderr=subprocess.PIPE,timeout=dump_timeout)
                    if process.returncode: raise RuntimeError('pg_dump_failed')
                    output.flush(); os.fsync(output.fileno())
        if captured_callback is not None:captured_callback()
        target=temporary/'files'; target.mkdir(mode=0o700)
        source=BlobWriter(store.files,reserve_bytes=0)
        total=sum(b['bytes'] for b in blobs)
        if shutil.disk_usage(temporary).free-(0 if reference_blobs else total)<1024**3: raise RuntimeError('backup_disk_reserve')
        for item in blobs:
            original=source.verify(Blob(item['digest'],item['bytes']))
            if not reference_blobs:
                with original.open('rb') as src, (target/item['digest']).open('xb') as dst:
                    shutil.copyfileobj(src,dst,1024**2); dst.flush(); os.fsync(dst.fileno())
        fsync_dir(target)
        with (temporary/'database.dump').open('rb') as dump:
            dump_sha=hashlib.file_digest(dump,'sha256').hexdigest()
        manifest={'format':2 if recovery is not None else 1,'created_at':datetime.now(timezone.utc).isoformat(),
            'captured_at':captured_at,
            'schema_version':schema_version,'tables':tables,'blobs':blobs,'dump_sha256':dump_sha,
            'includes_provider_sessions':bool(recovery and any(s['kind']=='session' for s in recovery['sources'])),
            'restore_mode':'verify_then_quarantine_collectors_no_sends'}
        if schema_version>=33:
            manifest.update(absent_blobs=absent,backup_fence=fence)
        if recovery is not None: manifest['recovery']=recovery
        with (temporary/'manifest.json').open('x') as out:
            out.write(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n'); out.flush(); os.fsync(out.fileno())
        fsync_dir(temporary); os.rename(temporary,destination); fsync_dir(destination.parent)
        return {'status':'snapshot_created','tables':len(tables),'blobs':len(blobs),'blob_bytes':total}
    finally:
        if temporary.exists(): shutil.rmtree(temporary)  # Only this function's temporary directory.
        if fence_committed and not destination.exists():
            # A safely abandoned unpublished capture no longer needs originals.
            # Unknown commit/cleanup outcomes retain the fence and fail closed.
            from .file_lifecycle import end_backup_fence
            with store.pool.connection() as c:end_backup_fence(c,fence)


def verify_bundle(directory,*,blob_root=None,extracted=None,read_dump=True,hash_dump=True,hash_originals=True):
    """`extracted` ({member: (sha256, bytes)} hashed while this process extracted the bundle) replaces the
    read of each original; `read_dump=False` also takes the dump's digest from it. Restore read-backs: ANYM-630.
    `hash_dump=False`/`hash_originals=False` check the dump's type and each original's size without
    reading them: the streaming producer hashes both while tar reads them (ANYM-631)."""
    from .recovery_state import read_manifest,verify_recovery
    directory=Path(directory); manifest=read_manifest(directory)
    if manifest.get('format') not in (1,2) or manifest.get('schema_version') not in SCHEMAS: raise ValueError('unsupported_backup')
    if (extracted is None and not read_dump) or (extracted is not None and blob_root is not None):
        raise ValueError('invalid_backup_verification')
    verify_recovery(directory,manifest)
    if (directory/'database.dump').is_symlink(): raise ValueError('invalid_backup_file')
    if read_dump:
        fd=os.open(directory/'database.dump',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as dump:
            if not stat.S_ISREG(os.fstat(dump.fileno()).st_mode):
                raise ValueError('invalid_backup_file')
            if hash_dump and hashlib.file_digest(dump,'sha256').hexdigest()!=manifest['dump_sha256']: raise RuntimeError('dump_corrupt')
    else:
        if not stat.S_ISREG(os.lstat(directory/'database.dump').st_mode): raise ValueError('invalid_backup_file')
        if extracted.get('database.dump',(None,))[0]!=manifest['dump_sha256']: raise RuntimeError('dump_corrupt')
    if (directory/'files').is_symlink(): raise ValueError('invalid_backup_file')
    writer=BlobWriter(blob_root if blob_root is not None else directory/'files',reserve_bytes=0); seen=set()
    if not isinstance(manifest['blobs'],list) or len(manifest['blobs'])>1000000:raise ValueError('invalid_backup_manifest')
    absent=manifest.get('absent_blobs',[])
    if (not isinstance(absent,list) or len(absent)+len(manifest['blobs'])>1000000 or
        ('absent_blobs' in manifest and manifest['schema_version']<33)):
        raise ValueError('invalid_backup_manifest')
    absent_seen=set()
    # Schema 35 names an absent original's verified cold copy when one exists.
    base={'digest','bytes','eviction_id'}
    for item in absent:
        cold=item.get('cold_object_key') if isinstance(item,dict) else None
        if (not isinstance(item,dict) or set(item) not in ([base,base|{'cold_object_key'}] if manifest['schema_version']>=35 else [base]) or
            ('cold_object_key' in item and (not isinstance(cold,str) or not isinstance(item['digest'],str) or
             not re.fullmatch(r'[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*/cold/'+re.escape(item['digest'])+r'\.age',cold))) or
            type(item['bytes']) is not int or item['bytes']<0 or
            not isinstance(item['digest'],str) or not re.fullmatch(r'[a-f0-9]{64}',item['digest']) or
            item['digest'] in absent_seen or not isinstance(item['eviction_id'],str)):
            raise ValueError('invalid_backup_manifest')
        try:
            if str(uuid.UUID(item['eviction_id']))!=item['eviction_id']:raise ValueError('invalid_backup_manifest')
        except (ValueError,AttributeError):raise ValueError('invalid_backup_manifest') from None
        absent_seen.add(item['digest'])
        # A referenced source may rehydrate after the captured DB point in
        # time. That does not change its captured absence or require inclusion.
        if blob_root is None and os.path.lexists(writer.root/item['digest']):
            raise RuntimeError('backup_absence_mismatch')
    for item in manifest['blobs']:
        if (not isinstance(item,dict) or set(item)!={'digest','bytes'} or type(item['bytes']) is not int or item['bytes']<0 or
            not isinstance(item['digest'],str) or not re.fullmatch(r'[a-f0-9]{64}',item['digest']) or
            item['digest'] in seen or item['digest'] in absent_seen):
            raise ValueError('invalid_backup_manifest')
        if extracted is not None:
            path=writer.root/item['digest']
            if (extracted.get('files/'+item['digest'])!=(item['digest'],item['bytes']) or path.is_symlink() or
                not path.is_file() or path.stat().st_size!=item['bytes']):
                raise RuntimeError('blob_corrupt')
        elif hash_originals:writer.verify(Blob(item['digest'],item['bytes']))
        else:
            path=writer.root/item['digest']
            if path.is_symlink() or not path.is_file() or path.stat().st_size!=item['bytes']:
                raise RuntimeError('backup_source_changed')
        seen.add(item['digest'])
    # Reference capture is an operator workspace, not a self-contained backup.
    # Restore accepts only a bundle containing its exact complete original set.
    expected=set() if blob_root is not None else seen
    if {p.name for p in (directory/'files').iterdir()}!=expected: raise ValueError('backup_blob_set_mismatch')
    return manifest


def verify_snapshot(store,directory,*,extracted=None):
    # With extraction digests the bundle check reads only the dump; the original_hashes read-back below stays.
    directory=Path(directory); manifest=verify_bundle(directory,extracted=extracted)
    with store.pool.connection() as c:
        c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        tables=database_manifest(c)
        present,absent=blob_inventory(c,manifest['schema_version'])
    if tables!=manifest['tables']: raise RuntimeError('restored_database_mismatch')
    if present!=manifest['blobs'] or absent!=manifest.get('absent_blobs',[]):
        raise RuntimeError('restored_blob_inventory_mismatch')
    restored=BlobWriter(store.files,reserve_bytes=0)
    for item in manifest['blobs']:
        restored.verify(Blob(item['digest'],item['bytes']))
    if any(os.path.lexists(store.files/item['digest']) for item in absent):
        raise RuntimeError('restored_blob_inventory_mismatch')
    return {'status':'verified','tables':len(tables),'blobs':len(manifest['blobs'])}


def quarantine_restore(store):
    """Run only on a verified isolated restore, never on the active source."""
    with store.pool.connection() as c:
        database=c.execute('SELECT current_database() AS name').fetchone()['name']
        if not database.endswith('_restore_test'): raise ValueError('not_restore_database')
        c.execute('UPDATE account_policies SET enabled=false')
        version=int(c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'])
        if version>=33:
            # Verification precedes quarantine: the captured fence rows must
            # first match the dump exactly. They protect the source capture,
            # not this isolated, disabled restore's future operator work.
            from .file_lifecycle import lifecycle_lock
            lifecycle_lock(c)
            c.execute('UPDATE attachment_backup_fences SET ended_at=coalesce(ended_at,clock_timestamp())')
        if version>=22:
            c.execute('UPDATE account_policies SET send_enabled=false')
            c.execute("""UPDATE outgoing_messages SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'unknown' END,
                error_code='restored_disabled',finished_at=clock_timestamp() WHERE status IN ('queued','dispatching')""")
        if version>=25:
            c.execute("UPDATE account_policies SET operations_enabled='{}'")
            c.execute("""UPDATE messenger_commands SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'unknown' END,
                error_code='restored_disabled',finished_at=clock_timestamp() WHERE status IN ('queued','dispatching')""")
        if version>=62:
            # ANYM-447: the 30 days after closure count from this restore, and only after the release.
            c.execute("UPDATE subscriptions SET state='closed',version=version+1,retention_hold=true,closed_at=clock_timestamp()")
        elif version>=40:
            # The hold keeps restored matches and consumption out of every pruning path
            # until an explicit operator release (ANYM-446).
            c.execute("UPDATE subscriptions SET state='closed',version=version+1,retention_hold=true")
        elif version>=26:
            c.execute("UPDATE subscriptions SET state='closed',version=version+1")
        if version>=32:
            c.execute('UPDATE subscription_receivers SET enabled=false')
            c.execute("UPDATE subscription_outbox SET state='cancelled',lease_token=NULL,lease_until=NULL,finished_at=clock_timestamp(),error_code='restored_disabled' WHERE state IN ('pending','leased')")
            c.execute('DELETE FROM subscription_receipts')
        if version>=49:
            # Self-service endpoints stay off until the operator enables each one (ANYM-519).
            c.execute("UPDATE delivery_endpoints SET state='operator_disabled',version=version+1,updated_at=clock_timestamp() WHERE state<>'deleted'")
            c.execute("UPDATE delivery_endpoint_tests SET state='cancelled',lease_token=NULL,lease_until=NULL,finished_at=clock_timestamp(),error_code='restored_disabled' WHERE state IN ('pending','leased')")
        if version>=38:
            # Held hints and silence timers never fire from a restored snapshot.
            c.execute("""UPDATE subscription_hint_windows SET first_match_at=NULL,last_match_at=NULL,match_count=0,
                through_position=NULL,reasons='{}',due_at=NULL,held_reason=NULL,silence_state='cancelled',
                silence_due_at=NULL,silence_held_reason=NULL,updated_at=clock_timestamp()""")
        if version>=29:
            # Upload staging is transient and excluded from original blob backups.
            c.execute("UPDATE file_uploads SET state='expired'")
        # A snapshot can predate a key revocation. Never revive old credentials on restore.
        c.execute('UPDATE api_keys SET revoked=true')
        c.execute("UPDATE collector_leases SET expires_at=now()-interval '1 second'")
        c.execute("UPDATE source_status SET status='restored_disabled',coverage='unknown',detail_code='requires_reconciliation'")
