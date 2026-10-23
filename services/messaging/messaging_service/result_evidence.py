"""Consistent, bounded message/file evidence from already authorized rows."""
import re
from contextvars import ContextVar

from .attachment_metadata import attachment_fields
from .media_policy import ELIGIBILITY_JOIN

# Set by Store.access; without it no excerpt is shown (the presence rule cannot run).
FILES_ROOT:ContextVar=ContextVar('files_root',default=None)
EXCERPT_CHARS=300
EXCERPT_BUDGET=3000
# get_file_text's default choice: ready extracted text, else a voice note's ready transcript (ANYM-453).
_EXTRACTED="f.state='ready' AND coalesce(f.text_content,'')<>''"
_TRANSCRIPT="(a.mime LIKE 'audio/%%' OR a.mime='application/ogg') AND e.state='ready' AND coalesce(e.text_content,'')<>''"
_TEXT='(CASE WHEN '+_EXTRACTED+' THEN f.text_content WHEN '+_TRANSCRIPT+' THEN e.text_content END)'
EXCERPT_COLUMNS=(',CASE WHEN '+_EXTRACTED+" THEN 'extracted' WHEN "+_TRANSCRIPT+" THEN 'transcript' END AS excerpt_source,"
    'left('+_TEXT+',%s) AS excerpt_head,length('+_TEXT+') AS excerpt_total,'
    'CASE WHEN '+_EXTRACTED+' THEN f.truncated WHEN '+_TRANSCRIPT+' THEN e.truncated END AS excerpt_source_truncated')


def send_provenance(c,rows):
    """Already authorized message rows only; never expose credentials/request IDs."""
    if not rows:return
    ids=list({r.get('id') or r['message'] for r in rows})
    sends=c.execute('''SELECT DISTINCT ON(m.id) m.id AS message_id,o.id AS send_id,o.principal,o.body,
        count(*) OVER(PARTITION BY m.id) AS candidate_count
        FROM messages m JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
        JOIN outgoing_messages o ON o.account=m.account AND o.status='sent' AND
            ((o.archive_reference IS NULL AND o.chat=m.chat AND o.provider_message_id=m.provider_id) OR
             (o.archive_reference->>'provider_chat_id'=ch.provider_chat AND
              o.archive_reference->>'provider_message_id'=m.provider_id))
        WHERE m.id=ANY(%s) AND m.direction='outgoing'
        ORDER BY m.id,o.finished_at DESC,o.id''',(ids,)).fetchall()
    sent={r['message_id']:r for r in sends}
    for row in rows:
        action=sent.get(row.get('id') or row['message'])
        if action is None:continue
        if action['candidate_count']!=1:
            row['agent_send']={'basis':'ambiguous_outbox_receipts','candidate_count':action['candidate_count'],
                'owner_approval_verified_by_server':False}
        else:
            row['agent_send']={'send_id':action['send_id'],'principal':action['principal'],
                'basis':'confirmed_outbox_receipt','body_matches_sent_text':row.get('body')==action['body'],
                'owner_approval_verified_by_server':False}


def formatting_evidence(c,rows):
    """Read exact authorized revisions; never attach newer proof to old events."""
    from .incoming_formatting import unknown
    missing=[r for r in rows if 'formatting' not in r]
    if not missing:return
    # A message may occur at multiple frozen revisions in an update batch.
    pairs=list(dict.fromkeys((r.get('id') or r['message'],r['revision']) for r in missing))
    proof={(r['message'],r['revision']):r['formatting'] for r in c.execute(
        """SELECT mr.message,mr.revision,mr.snapshot->'formatting' AS formatting
            FROM message_revisions mr JOIN unnest(%s::text[],%s::integer[]) AS requested(message,revision)
                ON mr.message=requested.message AND mr.revision=requested.revision""",
        ([p[0] for p in pairs],[p[1] for p in pairs])).fetchall()}
    for row in missing:
        value=proof.get((row.get('id') or row['message'],row['revision']))
        row['formatting']=value if value is not None else unknown()


def delivery_evidence(c,rows):
    """Decorate authorized rows, preserving receipt events' frozen evidence."""
    from .receipts import for_rows,unknown_evidence
    missing=[r for r in rows if r.get('direction')=='outgoing' and 'delivery_receipt' not in r]
    proofs=for_rows(c,missing)
    for row in missing:
        row['delivery_receipt']=proofs.get(row.get('id') or row.get('message'),unknown_evidence())


def _readable(c,file):
    """get_file_text's rule: the original is resident, or gone by a completed managed eviction."""
    from .file_index import _absent,_available
    from .file_lifecycle import is_managed_evicted
    root=FILES_ROOT.get()
    if root is None or not file.get('digest'):return False
    row={'sha256':file['digest'],'bytes':file['bytes']}
    return _available(root,row) or (_absent(root,row) and is_managed_evicted(c,file['digest'],file['bytes']))


def text_excerpt(c,file,budget):
    """A bounded head of the text get_file_text returns by default, with where to read on."""
    source=file.get('excerpt_source')
    if not source or not _readable(c,file):return None
    total=file['excerpt_total']
    pointer={'operation':'get_file_text','attachment_id':file['attachment_id'],'offset':0,
        'content':'extracted' if source=='extracted' else 'enriched'}
    head=file['excerpt_head'][:EXCERPT_CHARS]
    if total>EXCERPT_CHARS:
        # Prefer a word boundary near the end so the excerpt does not stop mid-word.
        match=re.search(r'\s\S*$',head[EXCERPT_CHARS-40:])
        if match:head=head[:EXCERPT_CHARS-40+match.start()].rstrip() or head
    common={'source':source,'model_generated':source=='transcript','untrusted':True}
    if len(head)>budget:return {'omitted':'response_budget',**common,'total_chars':total,'read_more':pointer}
    return {'text':head,**common,'excerpt_chars':len(head),'total_chars':total,
        'complete':len(head)==total and not file['excerpt_source_truncated'],
        'read_more':None if len(head)==total else {**pointer,'offset':len(head)}}


def decorate(c,rows,*,order_key=None):
    if not rows:return rows
    ids=list({r['id'] for r in rows})
    files=c.execute('''SELECT j.message,j.id AS attachment_id,a.id,a.filename,a.mime,a.bytes,a.digest,
        j.descriptor,j.attempts,j.next_due,j.requested_until,m.account,m.chat,m.revision AS message_revision,
        m.provider_id AS provider_message_id,ch.provider_chat AS provider_chat_id,
        j.state AS job_state,j.error_code AS download_error_code,el.occurrence AS download_eligibility,
        coalesce(a.filename,j.descriptor->>'filename') AS source_name,
        coalesce(f.state,'unknown') AS extraction_state,CASE WHEN coalesce(e.state,'unconfigured')='unconfigured' AND EXISTS(SELECT FROM processing_profiles pp WHERE pp.account=m.account AND pp.enabled AND pp.kind=CASE WHEN a.mime LIKE 'audio/%%' OR a.mime='application/ogg' THEN 'audio' WHEN a.mime LIKE 'image/%%' THEN 'image' ELSE 'document' END) THEN 'queued' ELSE coalesce(e.state,'unconfigured') END AS enrichment_state
        '''+EXCERPT_COLUMNS+'''
        FROM media_jobs j JOIN messages m ON m.id=j.message
        JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
        LEFT JOIN attachments a ON a.id=j.id AND a.active
        '''+ELIGIBILITY_JOIN+'''
        LEFT JOIN attachment_text f ON f.attachment=a.id AND f.source_digest=a.digest
            AND f.source_revision=m.revision AND f.source_filename=a.filename AND f.source_mime=a.mime
            AND f.source_bytes=a.bytes AND f.state<>'superseded'
        LEFT JOIN attachment_enrichment e ON e.attachment=a.id AND e.source_digest=a.digest
            AND e.source_revision=m.revision AND e.state<>'superseded'
        WHERE j.message=ANY(%s) AND j.state<>'superseded' AND NOT m.deleted ORDER BY j.id''',(EXCERPT_CHARS+1,ids)).fetchall()
    # Synthetic fixtures and restored legacy originals may predate media jobs.
    files+=c.execute('''SELECT a.message,a.id AS attachment_id,a.id,a.filename,a.mime,a.bytes,a.digest,
        NULL::jsonb AS descriptor,NULL::integer AS attempts,NULL::timestamptz AS next_due,
        NULL::timestamptz AS requested_until,m.account,m.chat,m.revision AS message_revision,
        m.provider_id AS provider_message_id,ch.provider_chat AS provider_chat_id,
        'ready' AS job_state,NULL AS download_error_code,a.filename AS source_name,
        coalesce(f.state,'unknown') AS extraction_state,CASE WHEN coalesce(e.state,'unconfigured')='unconfigured' AND EXISTS(SELECT FROM processing_profiles pp WHERE pp.account=m.account AND pp.enabled AND pp.kind=CASE WHEN a.mime LIKE 'audio/%%' OR a.mime='application/ogg' THEN 'audio' WHEN a.mime LIKE 'image/%%' THEN 'image' ELSE 'document' END) THEN 'queued' ELSE coalesce(e.state,'unconfigured') END AS enrichment_state
        '''+EXCERPT_COLUMNS+'''
        FROM attachments a JOIN messages m ON m.id=a.message
        JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
        LEFT JOIN attachment_text f ON f.attachment=a.id AND f.source_digest=a.digest
            AND f.source_revision=m.revision AND f.source_filename=a.filename AND f.source_mime=a.mime
            AND f.source_bytes=a.bytes AND f.state<>'superseded'
        LEFT JOIN attachment_enrichment e ON e.attachment=a.id AND e.source_digest=a.digest
            AND e.source_revision=m.revision AND e.state<>'superseded'
        WHERE a.message=ANY(%s) AND a.active AND NOT m.deleted
        AND NOT EXISTS(SELECT 1 FROM media_jobs j WHERE j.id=a.id) ORDER BY a.id''',(EXCERPT_CHARS+1,ids)).fetchall()
    grouped={}
    for file in files:
        # A database download receipt is not a fresh filesystem presence check.
        # get_attachment verifies the original before issuing any bearer URL.
        item=attachment_fields(file)
        grouped.setdefault(file['message'],[]).append(item)
        file['item']=item
    current={r['id']:r['revision'] for r in c.execute('SELECT id,revision FROM messages WHERE id=ANY(%s)',(ids,))}
    # One budget per decorated result, spent in the caller's row order; rows whose revision
    # changed during the read lose their attachments below and spend nothing.
    budget=EXCERPT_BUDGET
    order={}
    # The caller's final order when it sorts after decoration (get_context's conversation view).
    for n,r in enumerate(sorted(rows,key=order_key) if order_key else rows):
        if current.get(r['id'])==r['revision']:order.setdefault(r['id'],n)
    for file in sorted((f for f in files if f['message'] in order),key=lambda f:order[f['message']]):
        excerpt=text_excerpt(c,file,budget)
        if excerpt:
            file['item']['text_excerpt']=excerpt
            budget-=excerpt.get('excerpt_chars',0)
    conflicts={r['message']:r for r in c.execute('''SELECT message,
        count(*) FILTER(WHERE resolved_at IS NULL) AS active_conflicts,
        count(*) FILTER(WHERE resolved_at IS NOT NULL) AS historical_conflicts,
        max(observed_at) AS last_conflict_at FROM message_conflicts
        WHERE message=ANY(%s) GROUP BY message''',(ids,))}
    send_provenance(c,rows)
    delivery_evidence(c,rows)
    formatting_evidence(c,rows)
    for row in rows:
        # Some query paths truncate before decoration; never imply these spans
        # address the shortened response body. Do not mutate stored snapshots.
        row['formatting']={**row['formatting'],'offsets_reference':'full_provider_body_utf16'}
        row['attachments']=grouped.get(row['id'],[])
        conflict=conflicts.get(row['id'],{'active_conflicts':0,'historical_conflicts':0,'last_conflict_at':None})
        row['evidence']={k:v for k,v in conflict.items() if k!='message'}
        changed=current.get(row['id'])!=row['revision']
        if changed:row['attachments']=[]
        row['evidence'].update(archive_revision=row['revision'],current_revision_at_evidence_read=current.get(row['id']),
            revision_changed_during_read=changed,provider_currentness_verified=False,
            next_action='reread_message' if changed else
                'inspect_chat_coverage_and_request_reconciliation' if conflict['active_conflicts'] else 'read_context')
    return rows
