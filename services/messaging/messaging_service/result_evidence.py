"""Consistent, bounded message/file evidence from already authorized rows."""
from .attachment_metadata import attachment_fields
from .media_policy import ELIGIBILITY_JOIN


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


def decorate(c,rows):
    if not rows:return rows
    ids=list({r['id'] for r in rows})
    files=c.execute('''SELECT j.message,j.id AS attachment_id,a.id,a.filename,a.mime,a.bytes,a.digest,
        j.descriptor,j.attempts,j.next_due,j.requested_until,m.account,m.chat,m.revision AS message_revision,
        m.provider_id AS provider_message_id,ch.provider_chat AS provider_chat_id,
        j.state AS job_state,j.error_code AS download_error_code,el.occurrence AS download_eligibility,
        coalesce(a.filename,j.descriptor->>'filename') AS source_name,
        coalesce(f.state,'unknown') AS extraction_state,CASE WHEN coalesce(e.state,'unconfigured')='unconfigured' AND EXISTS(SELECT FROM processing_profiles pp WHERE pp.account=m.account AND pp.enabled AND pp.kind=CASE WHEN a.mime LIKE 'audio/%%' OR a.mime='application/ogg' THEN 'audio' WHEN a.mime LIKE 'image/%%' THEN 'image' ELSE 'document' END) THEN 'queued' ELSE coalesce(e.state,'unconfigured') END AS enrichment_state
        FROM media_jobs j JOIN messages m ON m.id=j.message
        JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
        LEFT JOIN attachments a ON a.id=j.id AND a.active
        '''+ELIGIBILITY_JOIN+'''
        LEFT JOIN attachment_text f ON f.attachment=a.id AND f.source_digest=a.digest
            AND f.source_revision=m.revision AND f.source_filename=a.filename AND f.source_mime=a.mime
            AND f.source_bytes=a.bytes AND f.state<>'superseded'
        LEFT JOIN attachment_enrichment e ON e.attachment=a.id AND e.source_digest=a.digest
            AND e.source_revision=m.revision AND e.state<>'superseded'
        WHERE j.message=ANY(%s) AND j.state<>'superseded' AND NOT m.deleted ORDER BY j.id''',(ids,)).fetchall()
    # Synthetic fixtures and restored legacy originals may predate media jobs.
    files+=c.execute('''SELECT a.message,a.id AS attachment_id,a.id,a.filename,a.mime,a.bytes,a.digest,
        NULL::jsonb AS descriptor,NULL::integer AS attempts,NULL::timestamptz AS next_due,
        NULL::timestamptz AS requested_until,m.account,m.chat,m.revision AS message_revision,
        m.provider_id AS provider_message_id,ch.provider_chat AS provider_chat_id,
        'ready' AS job_state,NULL AS download_error_code,a.filename AS source_name,
        coalesce(f.state,'unknown') AS extraction_state,CASE WHEN coalesce(e.state,'unconfigured')='unconfigured' AND EXISTS(SELECT FROM processing_profiles pp WHERE pp.account=m.account AND pp.enabled AND pp.kind=CASE WHEN a.mime LIKE 'audio/%%' OR a.mime='application/ogg' THEN 'audio' WHEN a.mime LIKE 'image/%%' THEN 'image' ELSE 'document' END) THEN 'queued' ELSE coalesce(e.state,'unconfigured') END AS enrichment_state
        FROM attachments a JOIN messages m ON m.id=a.message
        JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
        LEFT JOIN attachment_text f ON f.attachment=a.id AND f.source_digest=a.digest
            AND f.source_revision=m.revision AND f.source_filename=a.filename AND f.source_mime=a.mime
            AND f.source_bytes=a.bytes AND f.state<>'superseded'
        LEFT JOIN attachment_enrichment e ON e.attachment=a.id AND e.source_digest=a.digest
            AND e.source_revision=m.revision AND e.state<>'superseded'
        WHERE a.message=ANY(%s) AND a.active AND NOT m.deleted
        AND NOT EXISTS(SELECT 1 FROM media_jobs j WHERE j.id=a.id) ORDER BY a.id''',(ids,)).fetchall()
    grouped={}
    for file in files:
        # A database download receipt is not a fresh filesystem presence check.
        # get_attachment verifies the original before issuing any bearer URL.
        item=attachment_fields(file)
        grouped.setdefault(file['message'],[]).append(item)
    conflicts={r['message']:r for r in c.execute('''SELECT message,
        count(*) FILTER(WHERE resolved_at IS NULL) AS active_conflicts,
        count(*) FILTER(WHERE resolved_at IS NOT NULL) AS historical_conflicts,
        max(observed_at) AS last_conflict_at FROM message_conflicts
        WHERE message=ANY(%s) GROUP BY message''',(ids,))}
    current={r['id']:r['revision'] for r in c.execute('SELECT id,revision FROM messages WHERE id=ANY(%s)',(ids,))}
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
