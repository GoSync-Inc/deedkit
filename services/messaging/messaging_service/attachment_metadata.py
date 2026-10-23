"""Presentation for already authorized attachment occurrences; never fetches files.

A provider descriptor declares a name, MIME and expected byte length. A download
receipt establishes stored bytes, not continued filesystem presence. Only a caller
that checked the original can set ``original_present``. This is a presence check,
not a replacement for the archive's digest/integrity verification.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .file_index import file_response_fields
from .status_details import describe


# Download phase of a queued original the current collector run will not fetch by itself.
WAITING = {'outside_prefetch_window':'available_on_request','outside_history_bounds':'available_on_request',
    'collection_excluded':'blocked'}


def processing_phases(c, row):
    """Current authorized occurrence, independent of original filesystem presence."""
    if not row.get('digest'):
        return {}
    from .file_index import UNDERSTANDING_JOIN,UNDERSTANDING_COLUMNS
    return c.execute('SELECT '+UNDERSTANDING_COLUMNS+''',coalesce(f.state,'unknown') AS extraction_status,
        CASE WHEN r.state='evicted' AND EXISTS(SELECT FROM attachment_eviction_journal j
            WHERE j.id=r.eviction_id AND j.digest=r.digest AND j.bytes=r.bytes AND j.state='completed')
            THEN 'evicted' WHEN r.state='evicted' THEN 'missing' ELSE r.state END AS original_residency,
        (SELECT platform FROM accounts WHERE id=m.account) AS provider_platform,
        m.provider_id AS provider_message_id,
        (SELECT metadata->>'media_kind' FROM message_metadata
            WHERE message=m.id AND revision=m.revision) AS media_kind,
        coalesce(length(f.text_content),0) AS text_chars,f.processor_version,
        coalesce(f.truncated,false) AS truncated,
        (f.extraction_metadata-'page_spans'-'sheet_spans'-'cell_spans') AS extraction_metadata,
        CASE WHEN coalesce(e.state,'unconfigured')='unconfigured' AND EXISTS(
            SELECT FROM processing_profiles pp WHERE pp.account=m.account AND pp.enabled
            AND pp.kind=CASE WHEN a.mime LIKE 'audio/%%' OR a.mime='application/ogg' THEN 'audio'
                WHEN a.mime LIKE 'image/%%' THEN 'image' ELSE 'document' END)
            THEN 'queued' ELSE coalesce(e.state,'unconfigured') END AS enrichment_status
        FROM attachments a JOIN messages m ON m.id=a.message
        LEFT JOIN attachment_text f ON f.attachment=a.id AND f.source_digest=a.digest
            AND f.source_revision=m.revision AND f.source_filename=a.filename
            AND f.source_mime=a.mime AND f.source_bytes=a.bytes AND f.state<>'superseded'
        LEFT JOIN attachment_enrichment e ON e.attachment=a.id AND e.source_digest=a.digest
            AND e.source_revision=m.revision AND e.state<>'superseded'
        LEFT JOIN attachment_residency r ON r.digest=a.digest AND r.bytes=a.bytes
        '''+UNDERSTANDING_JOIN+''' WHERE a.id=%s AND a.active AND NOT m.deleted''',(row['id'],)).fetchone() or {}


def attachment_fields(row, *, original_present=None):
    """Return additive metadata without copying provider descriptors or URL grants.

    ``row`` accepts an attachment (id/digest) or a media job with attachment_id,
    descriptor and job_state. Optional extraction_state/enrichment_state describe
    the current authorized source revision, independently of original presence.
    """
    if original_present is not None and type(original_present) is not bool:
        raise ValueError('invalid_original_presence')
    descriptor=row.get('descriptor') or {}
    attachment_id=row.get('attachment_id') or row.get('id')
    digest=row.get('digest') or row.get('sha256')
    registered=bool(row.get('id') and digest)
    state=row.get('job_state') or row.get('state') or row.get('status') or ('ready' if registered else 'unknown')
    expired_transfer=state=='leased' and row.get('transfer_active') is False
    if expired_transfer:
        state='failed' if row.get('attempts',0)>=5 else 'queued'
    diagnostic_state='failed' if state=='queued' and row.get('attempts',0)>=5 else state
    filename=row.get('filename') if row.get('filename') is not None else descriptor.get('filename')
    mime=row.get('mime') if row.get('mime') is not None else descriptor.get('mime')
    size=row.get('bytes') if row.get('bytes') is not None else descriptor.get('bytes')
    error=row.get('download_error_code') or row.get('error_code')
    if expired_transfer and state=='failed':
        error='retry_exhausted'
    until=row.get('requested_until')
    requested=isinstance(until,datetime) and until.tzinfo is not None and until>datetime.now(timezone.utc)
    request_state='requested' if requested else 'expired' if until is not None else 'not_requested'
    eligibility=row.get('download_eligibility')
    if registered:
        download_status='unavailable' if original_present is False else 'ready'
        phase='unavailable' if original_present is False else 'ready'
        reason='original_missing' if original_present is False else 'original_downloaded'
        action='check_original_storage' if original_present is False else 'get_attachment'
        if original_present is False and row.get('original_residency')=='evicted':
            reason='original_evicted';action='request_file_download'
            # A re-download is bound by collection policy (ANYM-284); an explicit request reaches below the history bound (ANYM-477).
            if eligibility=='collection_excluded':
                reason=eligibility;action=describe(eligibility)['next_action']
    else:
        download_status={'leased':'processing','ready':'unavailable'}.get(state,state)
        phase=download_status
        reason=error or {'leased':'transfer_in_progress','ready':'original_not_registered'}.get(state,state)
        if diagnostic_state=='queued' and eligibility in WAITING:
            # Why the job waits now, not a code recorded while it was eligible (ANYM-284).
            reason=eligibility;action=describe(eligibility)['next_action'];phase=WAITING[eligibility]
        elif error:
            phase='blocked' if error in ('paused_storage','disk_reserve','media_size_limit') else download_status
            action=describe(error,job_state=diagnostic_state)['next_action']
        else:
            action='get_attachment' if requested or state=='leased' else 'request_file_download' if state=='queued' else 'get_sync_status_for_chat'
    extraction=row.get('extraction_state',row.get('extraction_status','unknown')) if registered else 'awaiting_download'
    enrichment=row.get('enrichment_state',row.get('enrichment_status','unconfigured')) if registered else 'awaiting_download'
    coverage={key:row[key] for key in ('text_chars','processor_version','truncated','extraction_metadata')
        if registered and key in row}
    provenance={key:row[key] for key in ('provider_platform','provider_message_id','media_kind') if key in row}
    description={key:value for key,value in row.items() if key.startswith('understanding_')}
    detail=(describe(reason) if reason in WAITING else
        describe(error,job_state=diagnostic_state) if error else None)
    item=file_response_fields({'id':attachment_id,'attachment_id':attachment_id,
        'filename':filename,'mime':mime,'bytes':size,'sha256':digest if registered else None,
        'digest':digest if registered else None,'error_code':error,**coverage,**provenance,**description},
        download_status=download_status,extraction_status=extraction,
        enrichment_status={'leased':'processing','unconfigured':'not_configured'}.get(enrichment,enrichment))
    item.update(state=state,status='unavailable' if original_present is False and registered else state,
        declared_mime=descriptor.get('mime'),declared_bytes=descriptor.get('bytes'),
        verified_bytes=row.get('bytes') if registered else None,
        metadata_basis='download_receipt' if registered else 'provider_descriptor',
        original_presence_verified=original_present is not None,
        original_available=original_present if registered else False if original_present is False else None,
        original_residency=('resident' if original_present is True else
            ('evicted' if row.get('original_residency')=='evicted' else 'missing')
            if original_present is False else row.get('original_residency') or 'unknown'),
        download_phase=phase,download_reason=reason,reason=reason,next_action=action,
        download_request={'state':request_state,'until':until},error_detail=detail,
        download_retry_scheduled=bool(detail and detail.get('retry_scheduled')))
    for key in ('attempts','next_due','download_eligibility'):
        if key in row:item[key]=row[key]
    kind=(mime or '').split(';',1)[0].lower()
    if enrichment=='ready' and (kind.startswith('audio/') or kind=='application/ogg'):
        item['transcript_ready']=True  # get_file_text returns it by default (ANYM-453).
    if eligibility=='unknown':item['eligibility_reason']='history_bound_unknown'
    source={}
    for name,aliases in {'account':('account',),'chat':('chat',),'message_id':('message_id','message'),
            'provider_chat_id':('provider_chat_id','provider_chat'),
            'provider_message_id':('provider_message_id','provider_id'),
            'message_revision':('message_revision','revision')}.items():
        for alias in aliases:
            if alias in row:
                source[name]=row[alias];break
    item['source']=source
    return item
