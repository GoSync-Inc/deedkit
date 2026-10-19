"""Bounded explicit scheduling of existing authorized originals.

Only collectors contact providers. Requests never reset attempts, bypass provider
cooldowns or change source references. The shared demand lock precedes job locks;
this path never acquires the collector/capacity lock while holding either one.
"""
from .attachment_metadata import attachment_fields, processing_phases
from .file_index import _available,_absent
from .media_policy import ELIGIBILITY,ELIGIBILITY_JOIN,HISTORY_BOUND,published_window
from .policy import scope_filter, targets
from .store import APIError, serial

PER_KEY_LIMIT=32
GLOBAL_LIMIT=128


def occurrence(c,p,attachment_id,*,lock=False):
    if not isinstance(attachment_id,str) or not 1<=len(attachment_id)<=256:
        raise APIError('not_found',404)
    where,args=scope_filter(p,'m')
    row=c.execute('''SELECT j.*,j.state AS job_state,
        (j.lease_until>clock_timestamp()) AS transfer_active,m.chat,m.provider_id AS provider_message_id,
        m.revision AS message_revision,ch.provider_chat AS provider_chat_id,
        a.digest,a.filename,a.mime,a.bytes,a.id AS registered_id,
        p.enabled AS collection_enabled,p.include_chats,p.exclude_chats,p.discover_chats,
        '''+ELIGIBILITY+''' AS download_eligibility,'''+HISTORY_BOUND+''' AS history_bound
        FROM media_jobs j JOIN messages m ON m.id=j.message AND m.account=j.account
        JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
        JOIN account_policies p ON p.account=m.account
        LEFT JOIN attachments a ON a.id=j.id AND a.active AND a.message=m.id
        '''+published_window('m.account')+' '+ELIGIBILITY_JOIN+'''
        WHERE j.id=%s AND NOT m.deleted AND j.state<>'superseded' AND '''+where+
        (' FOR UPDATE OF j' if lock else ''),[attachment_id,*args]).fetchone()
    return row


def request(store,c,p,attachment_id):
    from .file_lifecycle import lifecycle_lock,is_managed_evicted,touch_original
    # Lifecycle precedes demand/job locks and protects presence/touch/requeue.
    lifecycle_lock(c)
    # Quota and idempotency must serialize across HTTP workers and credentials.
    c.execute("SELECT pg_advisory_xact_lock(hashtextextended('media-demand-admission',0))")
    row=occurrence(c,p,attachment_id,lock=True)
    if row is None:
        # Fixture/legacy attachments can exist without a media job; no fetch is
        # possible, but a present original needs no request at all.
        where,args=scope_filter(p,'m')
        row=c.execute('''SELECT a.*,m.account,m.chat,m.provider_id AS provider_message_id,
            m.revision AS message_revision,ch.provider_chat AS provider_chat_id
            FROM attachments a JOIN messages m ON m.id=a.message
            JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
            WHERE a.id=%s AND a.active AND NOT m.deleted AND '''+where,[attachment_id,*args]).fetchone()
        if row is None:raise APIError('not_found',404)
    targets(p,[attachment_id])
    present=None
    managed=False
    if row.get('digest'):
        present=_available(store.files,{'sha256':row['digest'],'bytes':row['bytes']})
        managed=not present and _absent(store.files,{'sha256':row['digest']}) and is_managed_evicted(c,row['digest'],row['bytes'])
    row.update(processing_phases(c,row))
    if present is False and not managed:
        row['original_residency']='missing'
    result=attachment_fields(row,original_present=present)
    result.update(attachment_id=attachment_id,changes_provider_state=False,
        poll_operation='get_attachment',poll_after_seconds=3)
    if row.get('digest'):
        touch_original(c,row['digest'],row['bytes'])
    if row.get('digest') and not managed:
        result.update(request_status='ready' if present else 'unavailable',
            reason='original_downloaded' if present else 'original_missing',requested_until=None)
        return serial(result)
    if managed and 'job_state' not in row:
        result.update(request_status='unavailable',reason='provider_source_unavailable',requested_until=None)
        return serial(result)
    if row['job_state']=='ready' and not managed:
        result.update(request_status='unavailable',reason='original_not_registered',requested_until=None)
        return serial(result)
    if (not row['collection_enabled'] or row['provider_chat_id'] in row['exclude_chats'] or
            not (row['discover_chats'] or row['provider_chat_id'] in row['include_chats'])):
        result.update(request_status='blocked',reason='collection_disabled',requested_until=None,
            next_action='check_collection_policy')
        return serial(result)
    if row['job_state']=='leased' and row['transfer_active']:
        result.update(request_status='processing',requested_until=row['requested_until'],
            next_action='get_attachment')
        return serial(result)
    if row['job_state']=='failed' or row['attempts']>=5:
        result.update(request_status='failed',reason=row['error_code'] or 'retry_exhausted',
            requested_until=None)
        return serial(result)
    if row.get('download_eligibility')=='outside_history_bounds':
        # A request never overrides the collector's history bound (ANYM-284).
        result.update(request_status='unavailable',reason='outside_history_bounds',requested_until=None,
            next_action='get_sync_status_for_chat')
        return serial(result)
    active=c.execute('SELECT %s::timestamptz>clock_timestamp() AS active',
        (row['requested_until'],)).fetchone()['active']
    if active:
        result.update(request_status='coalesced',requested_until=row['requested_until'])
        return serial(result)
    deferred=c.execute('''SELECT %s::timestamptz>=clock_timestamp()+interval '1 hour' AS deferred,
        greatest(0,ceil(extract(epoch FROM %s::timestamptz-clock_timestamp())))::bigint AS retry_after_seconds''',
        (row['next_due'],row['next_due'])).fetchone()
    if deferred['deferred']:
        result.update(request_status='deferred',reason='retry_after_request_window',
            requested_until=None,retry_after_seconds=deferred['retry_after_seconds'],
            next_action='request_file_download_after_retry')
        return serial(result)
    count=c.execute('''SELECT count(*) AS total,count(*) FILTER(WHERE request_key_digest=%s) AS own
        FROM media_jobs WHERE requested_until>clock_timestamp() AND state IN ('queued','leased')''',
        (p['key_digest'],)).fetchone()
    if count['total']>=GLOBAL_LIMIT or count['own']>=PER_KEY_LIMIT:
        raise APIError('media_request_limit',429)
    until=c.execute('''UPDATE media_jobs SET requested_until=clock_timestamp()+interval '1 hour',
        request_key_digest=%s WHERE id=%s RETURNING requested_until''',
        (p['key_digest'],attachment_id)).fetchone()['requested_until']
    if managed and row['job_state']=='ready':
        changed=c.execute('SELECT public.queue_managed_original(%s) AS queued',(attachment_id,)).fetchone()['queued']
        if not changed:raise APIError('original_requeue_conflict',409)
    # Without a confirmed bound the request is recorded but promises no download.
    row.update(requested_until=until,download_eligibility='eligible' if row['history_bound']=='confirmed' else 'unknown')
    result.update(attachment_fields(row,original_present=present))
    if row['download_eligibility']=='unknown':result['eligibility_reason']='history_bound_unknown'
    result.update(request_status='queued',requested_until=until,
        download_request={'state':'requested','until':until},next_action='get_attachment')
    if managed:result['reason']='managed_original_evicted'
    return serial(result)
