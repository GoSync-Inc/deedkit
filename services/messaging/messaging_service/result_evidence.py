"""Consistent, bounded message/file evidence from already authorized rows."""
import hashlib
import re
from contextvars import ContextVar

from .attachment_metadata import attachment_fields, count_media_error, media_fields, media_unavailable
from .file_extract import ocr_quality
from .media_policy import (DESCRIPTION_WAITS, ELIGIBILITY_JOIN, ENRICHMENT_REASON, RECOGNITION_REASON, RECOGNITION_WITHHELD,
    VISIBLE_TEXT_READY)

# Set by Store.access; without it no excerpt is shown (the presence rule cannot run).
FILES_ROOT:ContextVar=ContextVar('files_root',default=None)
EXCERPT_CHARS=300
EXCERPT_BUDGET=3000
# get_file_text's default choice: an image's ready vision text (ANYM-987), else ready extracted text, else a voice
# note's ready transcript (ANYM-453).
_VISIBLE=VISIBLE_TEXT_READY
_EXTRACTED="f.state='ready' AND coalesce(f.text_content,'')<>''"
_TRANSCRIPT="(a.mime LIKE 'audio/%%' OR a.mime='application/ogg') AND e.state='ready' AND coalesce(e.text_content,'')<>''"
_TEXT='(CASE WHEN '+_VISIBLE+' THEN e.text_content WHEN '+_EXTRACTED+' THEN f.text_content WHEN '+_TRANSCRIPT+' THEN e.text_content END)'
EXCERPT_COLUMNS=(',CASE WHEN '+_VISIBLE+" THEN 'visible_text' WHEN "+_EXTRACTED+" THEN 'extracted' WHEN "+_TRANSCRIPT+" THEN 'transcript' END AS excerpt_source,"
    'left('+_TEXT+',%s) AS excerpt_head,length('+_TEXT+') AS excerpt_total,'
    'CASE WHEN '+_VISIBLE+' THEN e.truncated WHEN '+_EXTRACTED+' THEN f.truncated WHEN '+_TRANSCRIPT+' THEN e.truncated END AS excerpt_source_truncated,')
# ANYM-664: is_managed_evicted's rule inside this read, so a pending label adds no query per file.
RESIDENCY_COLUMN=("CASE WHEN EXISTS(SELECT FROM attachment_residency r JOIN attachment_eviction_journal ej ON ej.id=r.eviction_id"
    " WHERE r.digest=a.digest AND r.bytes=a.bytes AND r.state='evicted' AND ej.state='completed'"
    " AND ej.digest=r.digest AND ej.bytes=r.bytes) THEN 'evicted' END AS original_residency")
EXCERPT_COLUMNS+=RESIDENCY_COLUMN


# A media job's file row as a message read builds it (decorate) and the event stream reads it (event_attachments).
# profile_kinds: the account's enabled processing profiles, for the later stages of a file not downloaded yet (ANYM-866).
JOB_FILE_COLUMNS=('''j.message,j.id AS attachment_id,a.id,a.filename,a.mime,a.bytes,a.digest,
        j.descriptor,j.attempts,j.next_due,j.requested_until,m.account,m.chat,m.revision AS message_revision,
        m.provider_id AS provider_message_id,ch.provider_chat AS provider_chat_id,
        j.state AS job_state,j.error_code AS download_error_code,el.occurrence AS download_eligibility,
        '''+RECOGNITION_WITHHELD+''' AS recognition_withheld,'''+RECOGNITION_REASON+''' AS recognition_reason,
        '''+ENRICHMENT_REASON+''',
        coalesce(a.filename,j.descriptor->>'filename') AS source_name,
        coalesce(f.state,'unknown') AS extraction_state,f.extractor,jsonb_strip_nulls(jsonb_build_object('ocr_words',f.extraction_metadata->'ocr_words','ocr_confidence',f.extraction_metadata->'ocr_confidence')) AS ocr_metadata,CASE WHEN '''+DESCRIPTION_WAITS+''' THEN 'available_on_request' WHEN coalesce(e.state,'unconfigured')='unconfigured' AND EXISTS(SELECT FROM processing_profiles pp WHERE pp.account=m.account AND pp.enabled AND pp.kind=CASE WHEN a.mime LIKE 'audio/%%' OR a.mime='application/ogg' THEN 'audio' WHEN a.mime LIKE 'image/%%' THEN 'image' ELSE 'document' END) THEN 'queued' ELSE coalesce(e.state,'unconfigured') END AS enrichment_state,
        ARRAY(SELECT pp.kind FROM processing_profiles pp WHERE pp.account=m.account AND pp.enabled) AS profile_kinds''')
JOB_FILE_FROM=('''
        FROM media_jobs j JOIN messages m ON m.id=j.message
        JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
        LEFT JOIN attachments a ON a.id=j.id AND a.active
        '''+ELIGIBILITY_JOIN+'''
        LEFT JOIN attachment_text f ON f.attachment=a.id AND f.source_digest=a.digest
            AND f.source_revision=m.revision AND f.source_filename=a.filename AND f.source_mime=a.mime
            AND f.source_bytes=a.bytes AND f.state<>'superseded'
        LEFT JOIN attachment_enrichment e ON e.attachment=a.id AND e.source_digest=a.digest
            AND e.source_revision=m.revision AND e.state<>'superseded'
''')


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


def formatting_evidence(c,rows,*,with_body=False):
    """Read exact authorized revisions; never attach newer proof to old events.

    with_body (ANYM-737, body_format): the same statement also returns that revision's complete body, kept in an
    internal field that formatting_readback.attach removes before the response."""
    from .incoming_formatting import unknown
    from .formatting_readback import BODY_KEY
    missing=[r for r in rows if ('formatting' not in r or with_body and BODY_KEY not in r)]
    if not missing:return
    # A message may occur at multiple frozen revisions in an update batch.
    pairs=list(dict.fromkeys((r.get('id') or r['message'],r['revision']) for r in missing))
    proof={(r['message'],r['revision']):r for r in c.execute(
        """SELECT mr.message,mr.revision,mr.snapshot->'formatting' AS formatting"""+(""",mr.snapshot->>'body' AS body""" if with_body else '')+"""
            FROM message_revisions mr JOIN unnest(%s::text[],%s::integer[]) AS requested(message,revision)
                ON mr.message=requested.message AND mr.revision=requested.revision""",
        ([p[0] for p in pairs],[p[1] for p in pairs])).fetchall()}
    for row in missing:
        found=proof.get((row.get('id') or row['message'],row['revision']))
        value=found['formatting'] if found else None
        if 'formatting' not in row:row['formatting']=value if value is not None else unknown()
        if with_body:row[BODY_KEY]=found['body'] if found else None


def delivery_evidence(c,rows):
    """Decorate authorized rows, preserving receipt events' frozen evidence."""
    from .receipts import for_rows,unknown_evidence
    missing=[r for r in rows if r.get('direction')=='outgoing' and 'delivery_receipt' not in r]
    proofs=for_rows(c,missing)
    for row in missing:
        row['delivery_receipt']=proofs.get(row.get('id') or row.get('message'),unknown_evidence())


def original_present(file):
    """The original is resident now; a filesystem check only (ANYM-664: no new SQL on a message read)."""
    from .file_index import _available
    root=FILES_ROOT.get()
    return root is not None and bool(file.get('digest')) and _available(root,{'sha256':file['digest'],'bytes':file['bytes']})


def _readable(c,file):
    """get_file_text's rule: the original is resident, or gone by a completed managed eviction."""
    from .file_index import _absent,_available
    from .file_lifecycle import is_managed_evicted
    root=FILES_ROOT.get()
    if root is None or not file.get('digest'):return False
    row={'sha256':file['digest'],'bytes':file['bytes']}
    if _available(root,row):return True
    if not _absent(root,row):return False
    # A decorate row carries is_managed_evicted's answer from its own read (EXCERPT_COLUMNS, original_residency),
    # so an absent original costs no statement per file (ANYM-889).
    if 'original_residency' in file:return file['original_residency']=='evicted'
    return is_managed_evicted(c,file['digest'],file['bytes'])


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
    common={'source':source,'model_generated':source in ('transcript','visible_text'),'untrusted':True}
    # ANYM-669: the OCR confidence class of photo text, in the short key; low text is one get_file_text call away,
    # never in a message row and never against the budget. An unmeasured row reads as ok here.
    quality=ocr_quality(file.get('extractor'),file.get('ocr_metadata')) if source=='extracted' else None
    if quality:common['quality']=quality
    if quality=='low':return {'omitted':'ocr_low_quality',**common,'total_chars':total,'read_more':pointer}
    if len(head)>budget:return {'omitted':'response_budget',**common,'total_chars':total,'read_more':pointer}
    return {'text':head,**common,'excerpt_chars':len(head),'total_chars':total,
        'complete':len(head)==total and not file['excerpt_source_truncated'],
        'read_more':None if len(head)==total else {**pointer,'offset':len(head)}}


def decorate(c,rows,*,order_key=None,with_body=False,media=False,groups=None):
    """groups (ANYM-889): rows split into results, each decorated as its own call would (its own excerpt budget,
    row order and attachment items), while every read runs once over all of them. None is one result."""
    if not rows:return rows
    ids=list({r['id'] for r in rows})
    files=c.execute('SELECT '+JOB_FILE_COLUMNS+EXCERPT_COLUMNS+JOB_FILE_FROM+'''
        WHERE j.message=ANY(%s) AND j.state<>'superseded' AND NOT m.deleted ORDER BY j.id''',(EXCERPT_CHARS+1,ids)).fetchall()
    # Synthetic fixtures and restored legacy originals may predate media jobs.
    files+=c.execute('''SELECT a.message,a.id AS attachment_id,a.id,a.filename,a.mime,a.bytes,a.digest,
        NULL::jsonb AS descriptor,NULL::integer AS attempts,NULL::timestamptz AS next_due,
        NULL::timestamptz AS requested_until,m.account,m.chat,m.revision AS message_revision,
        m.provider_id AS provider_message_id,ch.provider_chat AS provider_chat_id,
        'ready' AS job_state,NULL AS download_error_code,a.filename AS source_name,
        '''+RECOGNITION_WITHHELD+''' AS recognition_withheld,'''+RECOGNITION_REASON+''' AS recognition_reason,
        '''+ENRICHMENT_REASON+''',
        coalesce(f.state,'unknown') AS extraction_state,f.extractor,jsonb_strip_nulls(jsonb_build_object('ocr_words',f.extraction_metadata->'ocr_words','ocr_confidence',f.extraction_metadata->'ocr_confidence')) AS ocr_metadata,CASE WHEN '''+DESCRIPTION_WAITS+''' THEN 'available_on_request' WHEN coalesce(e.state,'unconfigured')='unconfigured' AND EXISTS(SELECT FROM processing_profiles pp WHERE pp.account=m.account AND pp.enabled AND pp.kind=CASE WHEN a.mime LIKE 'audio/%%' OR a.mime='application/ogg' THEN 'audio' WHEN a.mime LIKE 'image/%%' THEN 'image' ELSE 'document' END) THEN 'queued' ELSE coalesce(e.state,'unconfigured') END AS enrichment_state
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
    built=[]
    for part in [rows] if groups is None else groups:
        wanted={r['id'] for r in part};grouped={};items=[]
        for file in files:
            if file['message'] not in wanted:continue
            # A database download receipt is not a fresh filesystem presence check.
            # get_attachment verifies the original before issuing any bearer URL.
            item=attachment_fields(file)
            if media:item['media']=media_fields(file,item)  # ANYM-866: get_context's files, as the event stream shows them
            grouped.setdefault(file['message'],[]).append(item)
            items.append((file,item))
        built.append((part,grouped,items))
    current={r['id']:r['revision'] for r in c.execute('SELECT id,revision FROM messages WHERE id=ANY(%s)',(ids,))}
    for part,_,items in built:
        # One budget per decorated result, spent in the caller's row order; rows whose revision
        # changed during the read lose their attachments below and spend nothing.
        budget=EXCERPT_BUDGET
        order={}
        # The caller's final order when it sorts after decoration (get_context's conversation view).
        for n,r in enumerate(sorted(part,key=order_key) if order_key else part):
            if current.get(r['id'])==r['revision']:order.setdefault(r['id'],n)
        for file,item in sorted((fi for fi in items if fi[0]['message'] in order),key=lambda fi:order[fi[0]['message']]):
            excerpt=text_excerpt(c,file,budget)
            if excerpt:
                item['text_excerpt']=excerpt
                budget-=excerpt.get('excerpt_chars',0)
    conflicts={r['message']:r for r in c.execute('''SELECT message,
        count(*) FILTER(WHERE resolved_at IS NULL) AS active_conflicts,
        count(*) FILTER(WHERE resolved_at IS NOT NULL) AS historical_conflicts,
        max(observed_at) AS last_conflict_at FROM message_conflicts
        WHERE message=ANY(%s) GROUP BY message''',(ids,))}
    # One class per message with an open conflict: one that needs attention first, then the latest (ANYM-557).
    from .conflict_classes import class_sql,known_classes,message_conflict
    active=[m for m,r in conflicts.items() if r['active_conflicts']]
    name=class_sql(c) if active else ''
    classes={r['message']:r['class'] for r in c.execute('''SELECT DISTINCT ON (mc.message) mc.message,'''+name+''' AS class
        FROM message_conflicts mc WHERE mc.message=ANY(%s) AND mc.resolved_at IS NULL
        ORDER BY mc.message,'''+name+'''=ANY(%s),mc.observed_at DESC,mc.receipt_digest''',
        (active,known_classes()))} if active else {}
    send_provenance(c,rows)
    delivery_evidence(c,rows)
    formatting_evidence(c,rows,with_body=with_body)
    for row,grouped in ((row,grouped) for part,grouped,_ in built for row in part):
        # Some query paths truncate before decoration; never imply these spans
        # address the shortened response body. Do not mutate stored snapshots.
        row['formatting']={**row['formatting'],'offsets_reference':'full_provider_body_utf16'}
        row['attachments']=grouped.get(row['id'],[])
        conflict=conflicts.get(row['id'],{'active_conflicts':0,'historical_conflicts':0,'last_conflict_at':None})
        row['evidence']={k:v for k,v in conflict.items() if k!='message'}
        step=None
        if row['id'] in classes:
            row['evidence']['conflict']=message_conflict(classes[row['id']]);step=row['evidence']['conflict']['next_step']
        changed=current.get(row['id'])!=row['revision']
        if changed:row['attachments']=[]
        row['evidence'].update(archive_revision=row['revision'],current_revision_at_evidence_read=current.get(row['id']),
            revision_changed_during_read=changed,provider_currentness_verified=False,
            next_action='reread_message' if changed else
                'inspect_chat' if step=='inspect_chat' else 'read_context')
    return rows


# ANYM-866: the files one event page reads (a page has at most 100 items of up to 32 files each).
PAGE_FILES=200
# The events that name their own files (ANYM-867 adds the terminal download failure).
FILE_EVENTS=('attachment.ready','attachment.failed')


def event_attachments(c,items):
    """ANYM-866: the files of already visible message.created, message.edited, attachment.ready and attachment.failed
    items, each with the state a message read shows now (media_fields). A message event names its own revision's files,
    so a file an edit removed reads superseded; attachment.ready names its file and attachment.failed its failed files
    (ANYM-867). No file name leaves; a file's error stays its own."""
    from .envelope import canonical,stable_id
    pairs=[(i['message'],i['revision']) for i in items
        if i['kind'] in ('message.created','message.edited') and isinstance(i.get('message'),str)]
    files={}
    if pairs:
        for r in c.execute('''SELECT message,revision,snapshot->'media' AS media FROM message_revisions
                WHERE (message,revision) IN (SELECT * FROM unnest(%s::text[],%s::integer[]))''',
                ([m for m,_ in pairs],[v for _,v in pairs])):
            # The archive's own job identity of each descriptor (Archive.ingest).
            files[(r['message'],r['revision'])]=[stable_id('file',r['message'],hashlib.sha256(canonical(d)).hexdigest())
                for d in (r['media'] if isinstance(r['media'],list) else [])]
    for i in items:
        processing=i.get('processing') if isinstance(i.get('processing'),dict) else {}
        ident=processing.get('attachment_id') if i['kind']=='attachment.ready' else None
        if isinstance(ident,str):files[('ready',i['event_id'])]=[ident]
        failed=processing.get('failed') if i['kind']=='attachment.failed' else None
        if isinstance(failed,list):
            files[('ready',i['event_id'])]=[f['attachment'] for f in failed
                if isinstance(f,dict) and isinstance(f.get('attachment'),str)][:32]
    # A bounded page: at most PAGE_FILES files; the items past the bound carry no attachments, counted once.
    def key_of(i):
        return ('ready',i['event_id']) if i['kind'] in FILE_EVENTS else (i.get('message'),i.get('revision'))
    kept,total=set(),0
    for key in dict.fromkeys(key_of(i) for i in items if key_of(i) in files):  # page order
        total+=len(files[key])
        if total>PAGE_FILES:
            files.pop(key)
        else:
            kept.update(files[key])
    if total>PAGE_FILES:count_media_error('page_file_limit')
    ids=sorted(kept)
    rows={r['attachment_id']:r for r in c.execute('SELECT '+JOB_FILE_COLUMNS+','+RESIDENCY_COLUMN+JOB_FILE_FROM+
        ' WHERE j.id=ANY(%s)',(ids,))} if ids else {}

    def view(ident,message):
        row=rows.get(ident)
        if row is None or row['message']!=message:
            return {'attachment_id':ident,'mime':None,'bytes':None,**media_unavailable('missing_row')}
        descriptor=row['descriptor'] if isinstance(row['descriptor'],dict) else {}
        return {'attachment_id':ident,'mime':row['mime'] if row['mime'] is not None else descriptor.get('mime'),
            'bytes':row['bytes'] if row['bytes'] is not None else descriptor.get('bytes'),**media_fields(row)}
    for i in items:
        key=key_of(i)
        if i['kind'] in ('message.created','message.edited',*FILE_EVENTS) and key in files:
            i['attachments']=[view(f,i.get('message')) for f in files[key]]
