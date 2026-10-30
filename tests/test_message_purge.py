"""Messenger deletion purge: a deleted message keeps no content anywhere (ANYM-511). Synthetic only."""
import json
import logging
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
import pytest

from messaging_service.blobs import BlobWriter
from messaging_service.envelope import stable_id
from test_archive import archive as archive, event
from test_messenger_commands import operator as operator  # noqa: F401  (fixture)
from test_sending import sender as sender  # noqa: F401  (fixture)

CHAT=stable_id('chat','archive-test','100')
VOICE={'provider_id':'voice-17','filename':'sentinel-a-voice.ogg','mime':'audio/ogg','bytes':5}
SCAN={'provider_id':'scan-17','filename':'sentinel-a-scan.pdf','mime':'application/pdf','bytes':5}


@pytest.fixture()
def purge(archive):
    from messaging_service.message_purge import Purger
    ar,s,key,_=archive
    with s.pool.connection() as c:
        # Marks and links are cleared by `seeded`; the worker state restarts with the feed.
        c.execute("""UPDATE purge_state SET value=CASE name WHEN 'event_cursor' THEN '0' WHEN 'receipt_cursor' THEN ''
            WHEN 'receipt_backfill' THEN 'pending' ELSE 'true' END""")
    return ar,s,key,Purger(s)


def leaks(s,needle):
    found={}
    with s.pool.connection() as c:
        tables=[r['table_name'] for r in c.execute("""SELECT table_name FROM information_schema.tables
            WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1""")]
        for table in tables:
            n=c.execute(sql.SQL('SELECT count(*) AS n FROM {} t WHERE t::text ILIKE %s').format(sql.Identifier(table)),
                        ('%'+needle+'%',)).fetchone()['n']
            if n:found[table]=n
    return found


def principal(s,key):
    with s.authorized(key) as (_,p):return dict(p)


def store_media(ar,s,count):
    for _ in range(count):
        job=ar.claim_media('fixture');writer=BlobWriter(s.files,reserve_bytes=0)
        ar.finish_media(job,writer.write([b'bytes']),writer)


def delete(ar,provider_id='17',at='2026-10-01T12:05:00Z',**changes):
    return ar.ingest(event(provider_id=provider_id,deleted=True,updated_at=at,**changes))


def seed_everything(ar,s,key):
    """Content of message 17 in every place the card names; message 18 is the unrelated control."""
    mid=ar.ingest(event(body='sentinel-a first',media=[VOICE,SCAN],metadata={'chat_type':'private'}))['message']
    ar.ingest(event(body='sentinel-a second',media=[VOICE,SCAN],metadata={'chat_type':'private'},
                    updated_at='2026-10-01T12:01:00Z'))
    store_media(ar,s,2)
    other=ar.ingest(event(provider_id='18',body='sentinel-b keeps'))['message']
    p=principal(s,key)
    s.set_access('archive-reader',permissions=['read','review-write'])
    s.record_review(key,message_id=mid,revision=2,result_ref='urn:personal-os:task:sentinel-a',request_id=str(uuid4()))
    with s.pool.connection() as c:
        files={r['filename']:r['id'] for r in c.execute('SELECT id,filename FROM attachments WHERE message=%s',(mid,))}
        voice,scan=files[VOICE['filename']],files[SCAN['filename']]
        c.execute("UPDATE attachment_text SET state='ready',text_content='sentinel-a ocr text' WHERE attachment=%s",(scan,))
        c.execute("""UPDATE attachment_enrichment SET state='ready',kind='audio',provider='t',model='m',title='sentinel-a title',
            summary='sentinel-a summary',text_content='sentinel-a transcript',metadata=%s,indexed_at=clock_timestamp()
            WHERE attachment=%s""",(Jsonb({'segments':[{'start':0,'end':5,'text':'sentinel-a segment'}]}),voice))
        c.execute("""UPDATE attachment_enrichment SET state='ready',kind='document',provider='t',model='m',title='sentinel-a doc',
            summary='sentinel-a doc summary',text_content='sentinel-a doc text',indexed_at=clock_timestamp()
            WHERE attachment=%s""",(scan,))
        c.execute("""UPDATE attachment_understanding SET state='ready',source_artifact_hash=%s,profile_hash=%s,
            published_profile_hash=%s,published_provider='ollama',published_model='m',title='sentinel-a understood',
            summary='sentinel-a understood summary',provider='ollama',model='m',
            coverage='{"basis":"extracted_text","complete":true}',indexed_at=clock_timestamp() WHERE attachment=%s""",
            ('a'*64,'b'*64,'b'*64,scan))
        c.execute('''UPDATE message_metadata SET metadata=metadata||'{"note":"sentinel-a meta"}'::jsonb WHERE message=%s''',(mid,))
        send(c,p,'send_purge','sentinel-a sent','100','17')
        # Native WhatsApp: requested PN chat and wire ID differ from the archived message (outgoing.py:199).
        send(c,p,'send_native','sentinel-a native','62811@c.us','true_62811@c.us_W1',
             {'provider_chat_id':'100','provider_message_id':'17'})
        # Requested IDs name message 17, but the verified wire reference is message 18: kept.
        send(c,p,'send_other','sentinel-b wire','100','17',{'provider_chat_id':'100','provider_message_id':'18'})
        command('edit-purge-1',c,p,mid,'edit_message','message-edit',True,{'parameters':{'text':'sentinel-a edit'}},
                {'outcome':'confirmed','data':{'note':'sentinel-a edited'}})
        command('live-purge-1',c,p,None,'search_messages_live','read-live',False,{'parameters':{'query':'lighthouse'}},
                {'outcome':'confirmed','data':{'hits':[{'chat':CHAT,'provider_id':'17','text':'sentinel-a hit'},
                                                       {'chat':CHAT,'provider_id':'18','text':'sentinel-b hit'}]}})
        # Commands whose output is the message: no message target, matched by the provider IDs they returned.
        command('poll-purge-1',c,p,None,'send_poll','send',True,{'provider_chat':'100','parameters':{
            'question':'sentinel-a question','options':['sentinel-a yes','sentinel-a no'],'multiple':False}},
            {'outcome':'confirmed','provider_message_id':'17'},chat=CHAT)
        command('file-purge-1',c,p,None,'send_file','send',True,{'provider_chat':'62811@c.us','parameters':{
            'mode':'document','caption':'sentinel-a caption','attachment_id':'f1'}},
            {'outcome':'confirmed','provider_message_id':'true_62811@c.us_W2',
             'data':{'archive_reference':{'provider_chat_id':'100','provider_message_id':'17'}}},chat=CHAT)
        command('poll-keep-1',c,p,None,'send_poll','send',True,{'provider_chat':'100','parameters':{
            'question':'sentinel-b question','options':['sentinel-b yes'],'multiple':False}},
            {'outcome':'confirmed','provider_message_id':'18'},chat=CHAT)
        c.execute('''INSERT INTO reply_dismissals(key_id,chat,message,revision,incoming_head,reason,note)
            VALUES(%s,%s,%s,2,%s,'other','sentinel-a note')''',(p['key_id'],CHAT,mid,mid))
        c.execute("""INSERT INTO reply_snoozes(key_id,chat,message,revision,incoming_head,snoozed_until,note)
            VALUES(%s,%s,%s,2,%s,clock_timestamp()+interval '1 day','sentinel-a snooze')""",(p['key_id'],CHAT,mid,mid))
    return mid,other


def command(request_id,c,p,mid,operation,permission,mutation,payload,result,status='succeeded',chat=None):
    return c.execute('''INSERT INTO messenger_commands(principal,key_id,scope_version,key_permission_version,request_id,
        request_hash,account,chat,message,expected_revision,operation,permission,mutation,payload,status,result)
        VALUES(%s,%s,%s,%s,%s,%s,'archive-test',%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (p['id'],p['key_id'],p['scope_version'],p['key_permission_version'],request_id,'a'*64,
         CHAT if mid else chat,mid,2 if mid else 0,operation,permission,mutation,Jsonb(payload),status,
         Jsonb(result) if result else None)).fetchone()['id']


def send(c,p,ident,body,provider_chat,provider_message_id,reference=None,status='sent'):
    c.execute('''INSERT INTO outgoing_messages(id,principal,key_id,scope_version,key_permission_version,
        request_id,request_hash,account,chat,provider_chat,body,status,provider_message_id,archive_reference,
        source_text,render_artifact,render_policy) VALUES(%s,%s,%s,%s,%s,%s,%s,'archive-test',%s,%s,%s,%s,%s,%s,
        %s,%s,'{}')''',(ident,p['id'],p['key_id'],p['scope_version'],p['key_permission_version'],'req-'+ident,'a'*64,
        CHAT,provider_chat,body,status,provider_message_id,Jsonb(reference) if reference else None,
        body+' source',Jsonb({'html':body})))


# spec: ANYM-511
def test_deleted_message_content_is_purged_from_every_table(purge):
    ar,s,key,p=purge
    mid,other=seed_everything(ar,s,key)
    before=leaks(s,'sentinel-a')
    for table in ('messages','message_revisions','events','ingest_receipts','attachments','media_jobs',
                  'attachment_text','attachment_enrichment','attachment_understanding','message_metadata',
                  'reviews','outgoing_messages','messenger_commands','reply_dismissals','reply_snoozes'):
        assert table in before,table
    control=leaks(s,'sentinel-b')
    delete(ar,media=[VOICE,SCAN])
    result=p.run_once()
    assert result['purged']==1 and result['failed']==0 and result['links_complete']
    assert leaks(s,'sentinel-a')=={}
    assert leaks(s,'sentinel-b')==control
    with s.pool.connection() as c:
        row=c.execute('SELECT * FROM message_purges WHERE message=%s',(mid,)).fetchone()
        files=c.execute('SELECT digest,bytes,mime FROM attachments WHERE message=%s ORDER BY mime',(mid,)).fetchall()
        commands={r['request_id']:r for r in c.execute('SELECT request_id,status,result,payload FROM messenger_commands')}
        review=c.execute('SELECT result_ref FROM reviews WHERE message=%s',(mid,)).fetchone()
    assert row['reason']=='telegram_deleted' and row['purged_at'] and row['provider_id']=='17' and row['provider_chat']=='100'
    assert row['counts']['messages']==1 and row['counts']['file_names']==2
    assert [(f['mime'],f['bytes']) for f in files]==[('application/pdf',5),('audio/ogg',5)] and all(f['digest'] for f in files)
    edit=commands['edit-purge-1']
    assert edit['status']=='succeeded' and edit['result']=={'outcome':'confirmed','purged':True}
    assert edit['payload']=={'parameters':{'text':'[deleted]'}}
    hits=commands['live-purge-1']['result']['data']['hits']
    assert hits[0]=={'chat':CHAT,'provider_id':'17','text':'','purged':True} and hits[1]['text']=='sentinel-b hit'
    poll,upload=commands['poll-purge-1'],commands['file-purge-1']
    assert poll['payload']['parameters']=={'question':'[deleted]','options':['[deleted]','[deleted]'],'multiple':False}
    assert poll['payload']['provider_chat']=='100' and poll['result']=={'outcome':'confirmed','provider_message_id':'17','purged':True}
    assert upload['payload']['parameters']=={'mode':'document','caption':'[deleted]','attachment_id':'f1'}
    assert upload['result']['data']=={'archive_reference':{'provider_chat_id':'100','provider_message_id':'17'}}
    assert review['result_ref']=='[deleted]'
    with s.pool.connection() as c:
        sends={r['id']:r for r in c.execute('SELECT id,body,source_text,render_artifact,provider_message_id FROM outgoing_messages')}
    assert sends['send_purge']['body']==sends['send_native']['body']=='[deleted]'
    assert sends['send_native']['source_text']=='[deleted]' and sends['send_native']['render_artifact']=={}
    assert sends['send_native']['provider_message_id']=='true_62811@c.us_W1' and sends['send_other']['body']=='sentinel-b wire'
    assert row['counts']['sends']==2 and row['counts']['commands']==3
    assert s.get_context(key,other)['message']['body']=='sentinel-b keeps'


# spec: ANYM-511
def test_history_import_after_purge_stays_content_free(purge):
    ar,s,key,p=purge
    mid=ar.ingest(event(body='sentinel-a first'))['message']
    delete(ar);p.run_once()
    with s.pool.connection() as c:mark=c.execute('SELECT * FROM message_purges WHERE message=%s',(mid,)).fetchone()
    outcome=ar.ingest(event(body='sentinel-a first',origin='backfill',updated_at='2026-10-02T12:00:00Z'))['outcome']
    assert outcome=='tombstoned'
    ar.ingest(event(body='sentinel-a first',origin='backfill'))
    assert leaks(s,'sentinel-a')=={}
    with s.pool.connection() as c:
        assert c.execute('SELECT * FROM message_purges WHERE message=%s',(mid,)).fetchone()==mark
        # A receipt written by code that does not scrub (schema-44 collectors) is scrubbed by the guard.
        c.execute('''INSERT INTO ingest_receipts(digest,account,envelope,outcome) VALUES('legacy-digest','archive-test',%s,'duplicate')''',
                  (Jsonb(event(body='sentinel-a legacy')),))
    assert leaks(s,'sentinel-a')=={}


# spec: ANYM-511
def test_finishers_after_purge_publish_nothing(purge):
    from messaging_service.file_extract import Extraction
    from messaging_service.file_index import FileIndexer
    ar,s,key,p=purge
    mid=ar.ingest(event(body='sentinel-a body',media=[SCAN]))['message']
    store_media(ar,s,1)
    ar.ingest(event(provider_id='19',body='plain',media=[{**VOICE,'provider_id':'voice-19','filename':'plain.ogg'}]))
    media_job=ar.claim_media('late')
    indexer=FileIndexer(s.pool.conninfo,s.files)
    extraction=next(job for job in iter(indexer.claim,None) if job['message']==mid)
    with s.pool.connection() as c:
        edit=command('edit-late-1',c,principal(s,key),mid,'edit_message','message-edit',True,
                     {'parameters':{'text':'sentinel-a edit'}},None,status='dispatching')
    delete(ar,media=[SCAN]);assert p.run_once()['purged']==1
    assert indexer.finish(extraction,Extraction('ready','sentinel-a late text','test-v1'))=='superseded'
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM attachment_text a JOIN attachments f ON f.id=a.attachment WHERE f.message=%s',
                         (mid,)).fetchone()['n']==0
        c.execute('INSERT INTO attachment_text(attachment,state,text_content) SELECT id,%s,%s FROM attachments WHERE message=%s',
                  ('ready','sentinel-a raw insert',mid))
        c.execute("UPDATE messenger_commands SET status='succeeded',result=%s WHERE id=%s",
                  (Jsonb({'outcome':'confirmed','data':{'note':'sentinel-a late'}}),edit))
        assert c.execute('SELECT result FROM messenger_commands WHERE id=%s',(edit,)).fetchone()['result']=={'outcome':'confirmed','purged':True}
    # An unrelated media lease is untouched and still finishes.
    writer=BlobWriter(s.files,reserve_bytes=0);ar.finish_media(media_job,writer.write([b'bytes']),writer)
    assert leaks(s,'sentinel-a')=={}


# spec: ANYM-511
def test_transcript_and_understanding_finishers_after_purge_publish_nothing(purge,tmp_path):
    from messaging_service.enrichment_providers import normalize_result
    from test_enrichment import add_audio,worker
    from test_file_understanding import add_source,make_worker
    _,s,_,p=purge
    add_audio(s);add_source(s)
    with s.pool.connection() as c:c.execute('TRUNCATE enrichment_budget')
    enricher=worker(s,tmp_path);transcription,_=enricher.claim()
    understander=make_worker(s);understanding,_=understander.claim()
    assert transcription['attachment']=='enrich-audio' and understanding['attachment']=='understand-fixture'
    with s.pool.connection() as c:
        # The seeded message is deleted without a new revision: only the purge fences the leases.
        c.execute("UPDATE messages SET deleted=true WHERE id='demo-tg-2'")
    assert p.apply()['purged']==1
    assert enricher.finish(transcription,normalize_result({'text':'sentinel-a transcript','segments':[
        {'start':0,'end':1,'text':'sentinel-a segment'}]},audio=True)) is False
    assert understander.finish(understanding,{'title':'sentinel-a title','summary':'sentinel-a summary',
        'coverage':{'mode':'transcript_summary','complete':True}}) is False
    with s.pool.connection() as c:
        assert c.execute('''SELECT count(*) AS n FROM attachment_enrichment WHERE attachment IN ('enrich-audio','understand-fixture')''').fetchone()['n']==0
        assert c.execute("SELECT count(*) AS n FROM attachment_understanding WHERE attachment='understand-fixture'").fetchone()['n']==0
    assert leaks(s,'sentinel-a')=={} and leaks(s,'Полная расшифровка')=={}


# spec: ANYM-511
def test_media_lease_taken_before_purge_cannot_publish(purge):
    ar,s,key,p=purge
    ar.ingest(event(body='sentinel-a body',media=[SCAN]))
    job=ar.claim_media('late');writer=BlobWriter(s.files,reserve_bytes=0);blob=writer.write([b'bytes'])
    delete(ar,media=[SCAN]);assert p.run_once()['purged']==1
    with pytest.raises(RuntimeError,match='lease_lost'):ar.finish_media(job,blob,writer)
    assert leaks(s,'sentinel-a')=={}


# spec: ANYM-511
def test_feed_reads_and_listing_after_purge(purge):
    ar,s,key,p=purge
    mid=ar.ingest(event(body='sentinel-a first'))['message']
    ar.ingest(event(body='sentinel-a second',updated_at='2026-10-01T12:01:00Z'))
    delete(ar)
    early=s.resolve_reference(key,'archive-test','100','17')['message']
    assert early['deleted'] and early['purged'] is False
    with s.pool.connection() as c:ids=[r['id'] for r in c.execute('SELECT id FROM events WHERE message=%s ORDER BY id',(mid,))]
    assert [c['id'] for c in s.list_chats(key)['items']]==[CHAT]
    p.run_once()
    late=s.resolve_reference(key,'archive-test','100','17')['message']
    assert late['deleted'] and late['purged'] and late['body']==''
    feed=s.get_updates(key,'main',limit=50)
    assert [e['event_sequence'] for e in feed['items']]==ids
    assert [e['kind'] for e in feed['items']]==['message.created','message.edited','message.deleted']
    assert all(e['deleted'] and e['purged'] and e['body']=='' for e in feed['items'])
    assert 'sentinel-a' not in json.dumps(feed,default=str)
    s.ack_updates(key,'main',feed['receipt'],feed['ack_version'])
    assert s.list_chats(key)['items']==[]
    ar.ingest(event(provider_id='20',body='new message',sent_at='2026-10-03T12:00:00Z',updated_at='2026-10-03T12:00:00Z'))
    assert [c['id'] for c in s.list_chats(key)['items']]==[CHAT]
    assert s.search(key,'sentinel')['items']==[]


# spec: ANYM-511
def test_existing_deleted_preview_writes_nothing_and_apply_purges_once(purge):
    ar,s,key,p=purge
    ar.ingest(event(body='sentinel-a first',media=[SCAN]));store_media(ar,s,1);delete(ar,media=[SCAN])
    with s.pool.connection() as c:
        state=c.execute('SELECT * FROM purge_state ORDER BY name').fetchall()
    preview=p.preview()
    assert preview==[{'account':'archive-test','messages':1,'originals':1,'bytes':5}]
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM message_purges').fetchone()['n']==0
        assert c.execute('SELECT * FROM purge_state ORDER BY name').fetchall()==state
    first=p.apply()
    assert first['queued']==1 and first['purged']==1 and first['failed']==0 and first['complete']
    assert p.apply()=={'queued':0,'purged':0,'gave_way':0,'failed':0,'passes':0,'pending':0,'complete':True}
    assert p.preview()==[]
    with s.pool.connection() as c:
        assert c.execute('SELECT reason FROM message_purges').fetchone()['reason']=='existing_deleted'
    assert leaks(s,'sentinel-a')=={}
    assert p.run_once()['purged']==0


def account_status(s,key):
    return {r['account']:r for r in s.get_sync_status(key)['sources']}['archive-test']['deletions']


# spec: ANYM-511
def test_audit_logs_and_status_through_failure_and_retry(purge,monkeypatch,caplog):
    from messaging_service import message_purge
    ar,s,key,p=purge
    owner=s.issue_key('purge-owner',['archive-test'])
    mid=ar.ingest(event(body='sentinel-a first'))['message'];delete(ar)
    p.discover()
    pending=account_status(s,owner)
    assert pending['pending']==1 and pending['purged_total']==0 and pending['guarantee']=='best_effort' and pending['gaps']==[]
    def broken(*args):raise psycopg.errors.DataError('injected')
    monkeypatch.setattr(message_purge.Purger,'scrub',staticmethod(broken))
    caplog.set_level(logging.INFO,logger='messaging_service.message_purge')
    assert p.run_once()['failed']==1
    ar.ingest(event(provider_id='21',body='ingest continues'))
    failing=account_status(s,owner)
    assert failing['failed']=={'count':1,'codes':{'purge_error':1}} and failing['pending']==1
    assert [f['message'] for f in p.status()['failed']]==[mid]
    monkeypatch.undo()
    with s.pool.connection() as c:c.execute('UPDATE message_purges SET next_attempt_at=clock_timestamp()')
    assert p.run_once()['purged']==1
    done=account_status(s,owner)
    assert done['failed']=={'count':0,'codes':{}} and done['purged_total']==1 and done['pending']==0 and done['last_purge_at']
    assert p.status()['failed']==[]
    with s.pool.connection() as c:
        audit=c.execute("SELECT actor,outcome,resource_ids FROM audit_log WHERE operation='message_purge'").fetchall()
        c.execute("UPDATE message_purges SET purged_at=NULL,failure_code=NULL,observed_at=clock_timestamp()-interval '16 minutes'")
    assert audit==[{'actor':'operator','outcome':'ok','resource_ids':[mid]}]
    assert account_status(s,owner)['failed']=={'count':1,'codes':{'purge_overdue':1}}
    assert 'sentinel' not in caplog.text and 'purged=1' in caplog.text
    assert leaks(s,'sentinel-a')=={}


# spec: ANYM-511
def test_purge_runs_under_its_role_and_is_refused_elsewhere(purge):
    from pathlib import Path
    from messaging_service.message_purge import Purger
    ar,s,key,_=purge
    role='purge_'+uuid4().hex
    ddl=Path('services/messaging/messaging_service/purge_role.sql').read_text().replace('messaging_purge',role)
    with s.pool.connection() as c:c.execute(ddl)
    try:
        seed_everything(ar,s,key);delete(ar,media=[VOICE,SCAN])
        restricted=Purger(s,role)
        assert restricted.run_once()['purged']==1
        assert restricted.preview()==[] and restricted.status()['failed']==[]
        assert leaks(s,'sentinel-a')=={}
        # WHERE false: the privilege check fails before any row is read.
        for statement in ("UPDATE messages SET sender='forged' WHERE false","UPDATE events SET kind='forged' WHERE false",
                          'DELETE FROM messages WHERE false','DELETE FROM events WHERE false',
                          'UPDATE reviews SET request_id=request_id WHERE false','DELETE FROM reviews WHERE false',
                          'CREATE TABLE purge_forged(id int)','UPDATE attachments SET active=false WHERE false'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with restricted.tx() as c:c.execute(statement)
    finally:
        with s.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


# spec: ANYM-511
def test_purge_gives_way_to_archive_lock_and_ignores_residency_lock(purge):
    from messaging_service.backup import database_manifest
    ar,s,key,p=purge
    ar.ingest(event(body='sentinel-a first'));delete(ar)
    with s.pool.connection() as holder:
        holder.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:archive-test',))
        held=p.run_once()
    assert held['gave_way']==1 and held['purged']==0
    with s.pool.connection() as holder:
        holder.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('attachment-residency-v1',))
        assert p.run_once()['purged']==1
        with s.pool.connection() as c:
            tables=database_manifest(c)
    assert {'message_purges','chat_tombstones','ingest_receipt_links','purge_state'}<=set(tables)
    assert ar.ingest(event(provider_id='22',body='after'))['outcome']=='created'


def threads(*functions):
    """Start each function in a thread; exceptions are kept for the assertion."""
    import threading
    errors=[]
    def guarded(function):
        try:function()
        except Exception as exc:errors.append(exc)
    started=[threading.Thread(target=guarded,args=(f,)) for f in functions]
    for t in started:t.start()
    return started,errors


def hold_scrub(monkeypatch,after=None):
    """Purger.scrub pauses inside the purge transaction until the returned gate opens."""
    import threading
    from messaging_service import message_purge
    gate=threading.Event();inside=threading.Event();original=message_purge.Purger.scrub
    def held(c,account,rows):
        counts=original(c,account,rows) if after is None else after(c)
        inside.set();gate.wait(2);return counts
    monkeypatch.setattr(message_purge.Purger,'scrub',staticmethod(held))
    return gate,inside


# spec: ANYM-511
def test_publications_racing_a_purge_wait_for_it_and_stay_content_free(purge,monkeypatch):
    import time
    ar,s,key,p=purge
    mid=ar.ingest(event(body='sentinel-a first'))['message']
    pr=principal(s,key);s.set_access('archive-reader',permissions=['read','review-write'])
    with s.pool.connection() as c:
        live=command('live-race-1',c,pr,None,'search_messages_live','read-live',False,{'parameters':{'query':'x'}},
                     None,status='dispatching')
        send(c,pr,'send_race','sentinel-a race send','100',None,status='dispatching')
    delete(ar)
    gate,inside=hold_scrub(monkeypatch)
    results={}
    purger,purge_errors=threads(lambda:results.update(purge=p.run_once()))
    assert inside.wait(5)
    def review():
        s.record_review(key,message_id=mid,revision=1,result_ref='urn:personal-os:task:sentinel-a-late',request_id=str(uuid4()))
    def live_result():
        with s.pool.connection() as c:
            c.execute("UPDATE messenger_commands SET status='succeeded',result=%s WHERE id=%s",(Jsonb({'outcome':'confirmed',
                'data':{'hits':[{'chat':CHAT,'provider_id':'17','text':'sentinel-a late hit'}]}}),live))
    def send_receipt():
        with s.pool.connection() as c:
            c.execute("UPDATE outgoing_messages SET status='sent',provider_message_id='17' WHERE id='send_race'")
    def request():
        with s.pool.connection() as c:
            command('edit-race-1',c,pr,mid,'edit_message','message-edit',True,
                    {'parameters':{'text':'sentinel-a late edit'}},None,status='queued')
    writers,errors=threads(review,live_result,send_receipt,request)
    time.sleep(0.3)
    # Each publication waits for the purge of its account instead of racing past it.
    assert [w.is_alive() for w in writers]==[True]*4,errors
    gate.set()
    for t in purger+writers:t.join(5)
    assert errors==[] and purge_errors==[] and results['purge']['purged']==1
    assert leaks(s,'sentinel-a')=={}
    with s.pool.connection() as c:
        assert c.execute('SELECT result_ref FROM reviews WHERE message=%s',(mid,)).fetchone()['result_ref']=='[deleted]'
        assert c.execute("SELECT body FROM outgoing_messages WHERE id='send_race'").fetchone()['body']=='[deleted]'


# spec: ANYM-511
def test_purge_gives_way_to_a_publication_in_flight(purge):
    ar,s,key,p=purge
    ar.ingest(event(body='sentinel-a first'))
    with s.pool.connection() as c:
        live=command('live-flight-1',c,principal(s,key),None,'search_messages_live','read-live',False,
                     {'parameters':{'query':'x'}},None,status='dispatching')
    delete(ar)
    with s.pool.connection() as w:
        # Published before the purge, committed after it started: the purge must not miss it.
        w.execute("UPDATE messenger_commands SET status='succeeded',result=%s WHERE id=%s",(Jsonb({'outcome':'confirmed',
            'data':{'hits':[{'chat':CHAT,'provider_id':'17','text':'sentinel-a hit'}]}}),live))
        held=p.run_once()
    assert held['gave_way']==1 and held['purged']==0
    assert p.run_once()['purged']==1
    assert leaks(s,'sentinel-a')=={}


# spec: ANYM-511
def test_slow_purge_is_cut_at_its_transaction_bound_while_ingest_continues(purge,monkeypatch):
    import time
    ar,s,key,p=purge
    ar.ingest(event(body='sentinel-a first'));ar.ingest(event(provider_id='23',body='sentinel-a other'))
    delete(ar);delete(ar,provider_id='23')
    def slow(c):
        for _ in range(3):c.execute('SELECT pg_sleep(1.5)')
    gate,inside=hold_scrub(monkeypatch,after=slow)
    gate.set()
    results={}
    purger,purge_errors=threads(lambda:results.update(purge=p.run_once()))
    time.sleep(0.3)
    started=time.monotonic()
    assert ar.ingest(event(provider_id='24',body='ingest continues'))['outcome']=='created'
    waited=time.monotonic()-started
    purger[0].join(10)
    assert purge_errors==[] and waited<3.5
    assert results['purge']['purged']==0 and results['purge']['gave_way']==1 and results['purge']['failed']==2
    with s.pool.connection() as c:
        marks=c.execute('SELECT failure_code,attempts FROM message_purges ORDER BY message').fetchall()
        c.execute('UPDATE message_purges SET next_attempt_at=clock_timestamp()')
    assert marks==[{'failure_code':'transaction_timeout','attempts':1}]*2
    monkeypatch.undo()
    # After a failure the batch shrinks to one message per transaction, and one pass drains them.
    assert p.run_once()['purged']==2
    with s.pool.connection() as c:
        audit=c.execute("SELECT resource_ids FROM audit_log WHERE operation='message_purge'").fetchall()
    assert sorted(len(r['resource_ids']) for r in audit)==[1,1]
    assert leaks(s,'sentinel-a')=={}


# spec: ANYM-511
def test_apply_drains_marks_an_interrupted_run_left_and_reports_contention(purge,monkeypatch):
    from messaging_service import message_purge
    monkeypatch.setattr(message_purge,'APPLY_RETRIES',0)
    ar,s,key,p=purge
    ar.ingest(event(body='sentinel-a first'));delete(ar)
    p.discover()  # queued: a run stopped before its purge leaves the mark pending
    with s.pool.connection() as holder:
        holder.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:archive-test',))
        held=p.apply()
    assert held['queued']==0 and held['purged']==0 and held['gave_way']>=1
    assert held['pending']==1 and held['complete'] is False
    done=p.apply()
    assert done['queued']==0 and done['purged']==1 and done['pending']==0 and done['complete']
    assert leaks(s,'sentinel-a')=={}


def reset_state(s):
    with s.pool.connection() as c:
        c.execute("""UPDATE purge_state SET value=CASE name WHEN 'event_cursor' THEN '0' WHEN 'receipt_cursor' THEN ''
            WHEN 'receipt_backfill' THEN 'pending' ELSE 'true' END""")


# spec: ANYM-511
def test_purged_command_status_and_replay_stay_readable(operator):
    from messaging_service.message_purge import Purger
    from test_messenger_commands import operation_args
    s,archive,key,_=operator
    reset_state(s)
    args=operation_args(operation='edit_message',parameters={'text':'sentinel-a edit'})
    draft=s.preview_messenger_operation(key,**args)
    first=s.execute_messenger_operation(key,**args,request_id='purge-replay-0001',preview_digest=draft['preview_digest'])
    with s.pool.connection() as c:
        c.execute("UPDATE messenger_commands SET status='succeeded',result=%s,finished_at=clock_timestamp() WHERE id=%s",
                  (Jsonb({'outcome':'confirmed'}),first['operation_id']))
    archive.ingest(event(account='command-fixture',chat='100',provider_id='1',direction='outgoing',
                         body='Original synthetic text',deleted=True,updated_at='2026-10-01T12:05:00Z'))
    assert Purger(s).run_once()['purged']==1
    status=s.get_messenger_operation(key,first['operation_id'])
    assert status['status']=='succeeded' and status['request_id']=='purge-replay-0001'
    replay=s.execute_messenger_operation(key,**args,request_id='purge-replay-0001',preview_digest=draft['preview_digest'])
    assert replay['operation_id']==first['operation_id'] and replay['status']=='succeeded'
    assert leaks(s,'sentinel-a')=={}


# spec: ANYM-511
def test_reply_controls_keep_working_in_a_fully_purged_chat(purge):
    from messaging_service.store import APIError
    ar,s,key,p=purge
    from datetime import datetime,timedelta,timezone
    now=datetime.now(timezone.utc)
    asked,deleted=(now-timedelta(hours=2)).isoformat(),(now-timedelta(hours=1)).isoformat()
    mid=ar.ingest(event(body='sentinel-a ask',sent_at=asked,updated_at=asked,metadata={'chat_type':'private'}))['message']
    s.dismiss_reply(key,CHAT,mid,1,reason='other',note='sentinel-a note')
    delete(ar,at=deleted,sent_at=asked,metadata={'chat_type':'private'});assert p.run_once()['purged']==1
    assert s.list_chats(key)['items']==[]
    # The hidden chat stays addressable (before the fix each control raised IndexError): the
    # dismissal is restored, and a purged message is no reply target any more.
    assert s.restore_reply(key,CHAT,mid,1)
    for control in (lambda:s.snooze_reply(key,CHAT,mid,2,'2026-12-01T00:00:00Z'),
                    lambda:s.dismiss_reply(key,CHAT,mid,2,reason='other')):
        with pytest.raises(APIError,match='reply_target_changed'):control()
    assert leaks(s,'sentinel-a')=={}


# spec: ANYM-511
def test_receipts_linked_after_the_purge_are_scrubbed_by_the_backfill(purge,monkeypatch):
    # Name kept for the Tiden case identity; since Astra r2 B3 no purge completes before the backfill.
    from messaging_service import message_purge
    ar,s,key,p=purge
    ar.ingest(event(body='sentinel-a first'))
    for n in range(9):ar.ingest(event(provider_id=str(30+n),body='filler'))
    with s.pool.connection() as c:c.execute('DELETE FROM ingest_receipt_links')  # receipts stored before schema 45
    delete(ar)
    # An interrupted backfill: one chunk of two receipts per pass, never the whole table.
    monkeypatch.setattr(message_purge,'LINK_CHUNK',2)
    monkeypatch.setattr(message_purge.Purger,'backfill',lambda self,seconds=0:self.backfill_links())
    for _ in range(3):
        result=p.run_once()
        assert result['purged']==0 and result['links_complete'] is False
    # Called directly, the purge refuses too: it reports nothing while a copy may remain.
    assert p.purge_account('archive-test')=={'purged':0,'gave_way':0,'failed':0}
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM message_purges WHERE purged_at IS NULL').fetchone()['n']==1
        assert c.execute("SELECT value FROM purge_state WHERE name='receipt_backfill'").fetchone()['value']=='pending'
        c.execute("UPDATE message_purges SET observed_at=clock_timestamp()-interval '1 hour'")
    assert [f['code'] for f in p.status()['failed']]==['purge_overdue']
    assert 'ingest_receipts' in leaks(s,'sentinel-a')
    monkeypatch.undo()
    result=p.run_once()
    assert result['links_complete'] and result['purged']==1
    assert leaks(s,'sentinel-a')=={} and p.status()['failed']==[]


# spec: ANYM-511
def test_migration_seeds_the_event_cursor_at_the_feed_head(purge):
    from pathlib import Path
    ar,s,key,p=purge
    ar.ingest(event(body='sentinel-a before the release'));delete(ar)
    text=Path('services/messaging/messaging_service/migrations/045_message_purge.sql').read_text()
    start=text.index('INSERT INTO purge_state')
    with s.pool.connection() as c:
        c.execute('DELETE FROM purge_state');c.execute(text[start:text.index(';',start)])
        head=c.execute('SELECT max(id) AS n FROM events').fetchone()['n']
        state={r['name']:r['value'] for r in c.execute('SELECT name,value FROM purge_state')}
    assert state=={'enabled':'true','event_cursor':str(head),'receipt_cursor':'','receipt_backfill':'pending'}
    # A deletion before the release is the operator's backlog, not the worker's.
    assert p.run_once()['discovered']==0 and p.preview()[0]['messages']==1


# spec: ANYM-511
def test_whatsapp_deletion_is_reported_and_purged(purge):
    ar,s,key,p=purge
    ar.register('wa-purge','whatsapp','Synthetic WhatsApp',include_chats=['100'])
    owner=s.issue_key('wa-owner',['wa-purge'])
    mid=ar.ingest(event(account='wa-purge',platform='whatsapp',body='sentinel-a wa'))['message']
    ar.ingest(event(account='wa-purge',platform='whatsapp',deleted=True,updated_at='2026-10-01T12:05:00Z'))
    assert p.run_once()['purged']==1
    with s.pool.connection() as c:
        assert c.execute('SELECT reason FROM message_purges WHERE message=%s',(mid,)).fetchone()['reason']=='deleted_for_everyone'
    status={r['account']:r for r in s.get_sync_status(owner)['sources']}['wa-purge']['deletions']
    assert status['guarantee']=='reported' and status['purged_total']==1
    assert leaks(s,'sentinel-a')=={}


# spec: ANYM-511
def test_store_created_file_and_location_commands_keep_no_content(operator):
    import asyncio
    import hashlib
    from messaging_service.commands import CommandWorker
    from messaging_service.message_purge import Purger
    s,archive,key,state=operator
    reset_state(s)
    s.set_messenger_operations('command-fixture',['send_file','send_location'])
    content=b'Synthetic purge original';sha=hashlib.sha256(content).hexdigest()
    s.files.mkdir(parents=True,exist_ok=True);(s.files/sha).write_bytes(content)
    aid=stable_id('att','command-fixture','100','1','purge-file')
    with s.pool.connection() as c:
        c.execute('INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES(%s,%s,%s,%s,%s,%s)',
            (aid,stable_id('msg','command-fixture','100','1'),sha,'sentinel-f-document.pdf','application/pdf',len(content)))
    chat=stable_id('chat','command-fixture','200')
    def queue(operation,parameters,request_id):
        args={'account':'command-fixture','operation':operation,'chat_id':chat,'parameters':parameters}
        draft=s.preview_messenger_operation(key,**args)
        first=s.execute_messenger_operation(key,**args,request_id=request_id,preview_digest=draft['preview_digest'])
        return args,draft,first
    def provider(provider_id):
        async def send(job):return {'outcome':'confirmed','provider_message_id':provider_id}
        return send
    def output(provider_id,body):
        archive.ingest(event(account='command-fixture',chat='200',provider_id=provider_id,direction='outgoing',body=body))
        archive.ingest(event(account='command-fixture',chat='200',provider_id=provider_id,direction='outgoing',
                             deleted=True,updated_at='2026-10-01T12:05:00Z'))
    # The location result is published before its message is deleted and purged ...
    location=queue('send_location',{'latitude':12.3456789,'longitude':98.7654321},'purge-location-0001')
    assert asyncio.run(CommandWorker(state,provider('41')).once())
    output('41','sentinel-a location')
    # ... the file result after: the publication guard redacts it.
    sent=queue('send_file',{'mode':'document','attachment_id':aid,'sha256':sha,'caption':'sentinel-a caption'},
               'purge-file-send-0001')
    with s.pool.connection() as c:
        saved=c.execute('SELECT payload FROM messenger_commands WHERE id=%s',(sent[2]['operation_id'],)).fetchone()['payload']
    assert saved['file_name']=='sentinel-f-document.pdf'  # the real payload carries the name at top level
    output('42','sentinel-a caption')
    assert Purger(s).run_once()['purged']==2
    assert asyncio.run(CommandWorker(state,provider('42')).once())
    with s.pool.connection() as c:
        rows={r['operation']:r for r in c.execute('''SELECT operation,status,payload,result FROM messenger_commands
            WHERE operation IN ('send_file','send_location')''')}
    for row in rows.values():
        stored=json.dumps(row['payload'])+json.dumps(row['result'])
        for content_value in ('sentinel','12.3456789','98.7654321'):assert content_value not in stored
        assert row['status']=='succeeded' and row['result']['purged']
    assert rows['send_file']['payload']['file_name']=='[deleted]'
    assert rows['send_file']['payload']['parameters']['attachment_id']==aid
    assert rows['send_file']['payload']['file_digest']==sha and rows['send_file']['payload']['provider_chat']=='200'
    assert rows['send_location']['payload']['parameters']=={'latitude':None,'longitude':None,'description':'[deleted]'}
    assert rows['send_location']['payload']['request_input']['parameters']=={'latitude':None,'longitude':None}
    # Status and an idempotent replay stay readable.
    for (args,draft,first),request_id in ((location,'purge-location-0001'),(sent,'purge-file-send-0001')):
        assert s.get_messenger_operation(key,first['operation_id'])['status']=='succeeded'
        replay=s.execute_messenger_operation(key,**args,request_id=request_id,preview_digest=draft['preview_digest'])
        assert replay['operation_id']==first['operation_id']
    assert leaks(s,'sentinel-a')=={}


# spec: ANYM-511
def test_a_slow_chat_visibility_check_does_not_stop_the_purge(purge,monkeypatch):
    from messaging_service import message_purge
    ar,s,key,p=purge
    ar.ingest(event(body='sentinel-a only'));delete(ar)
    # Stands in for a chat whose long purged history the visibility probe cannot read in time.
    monkeypatch.setattr(message_purge,'HIDE_STATEMENT','50ms')
    monkeypatch.setattr(message_purge,'HIDE_CHATS',message_purge.HIDE_CHATS+' AND (SELECT true FROM pg_sleep(1))')
    assert p.run_once()['purged']==1
    assert leaks(s,'sentinel-a')=={}
    with s.pool.connection() as c:
        counts=c.execute('SELECT counts FROM message_purges').fetchone()['counts']
    assert counts['hidden_chats']==0 and counts['hidden_chats_skipped']==1 and counts['messages']==1
    assert [c['id'] for c in s.list_chats(key)['items']]==[CHAT]  # listed for now, content-free
    assert p.status()['hide_pending']==1
    # Still slow: the retry leaves the mark pending; nothing else changes.
    assert p.run_once()['hides_retried']==0 and p.status()['hide_pending']==1
    monkeypatch.undo()
    # The next pass retries the hide without any new message; the probe fits and the chat is hidden.
    result=p.run_once()
    assert result['hides_retried']==1 and result['purged']==0 and p.status()['hide_pending']==0
    assert s.list_chats(key)['items']==[]
    # A new message reopens it (the AFTER INSERT trigger, under the archive lock).
    ar.ingest(event(provider_id='31',body='reopened',sent_at='2026-10-03T12:00:00Z',updated_at='2026-10-03T12:00:00Z'))
    assert [c['id'] for c in s.list_chats(key)['items']]==[CHAT]


def schema44(database,tmp_path):
    from test_extend_key import old_schema_store
    return old_schema_store(database,tmp_path,44)


# spec: ANYM-511
@pytest.mark.parametrize('index',['invalid','other_key','collation'])
def test_migration_refuses_an_unusable_events_message_index(database,tmp_path,index):
    with schema44(database,tmp_path) as store:
        with store.pool.connection() as c:
            if index=='invalid':
                # What an interrupted CREATE INDEX CONCURRENTLY leaves behind.
                c.execute('CREATE INDEX events_message ON events(message)')
                c.execute("UPDATE pg_index SET indisvalid=false WHERE indexrelid='events_message'::regclass")
            elif index=='other_key':c.execute('CREATE INDEX events_message ON events(kind)')
            # The purge's message=ANY(...) compares with the column collation: this index cannot serve it.
            else:c.execute('CREATE INDEX events_message ON events(message COLLATE "C")')
        with pytest.raises(psycopg.errors.RaiseException,match='not a valid btree index'):store.migrate()
        with store.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='44'
            c.execute('DROP INDEX events_message');c.execute('CREATE INDEX events_message ON events(message)')
        store.migrate()
        with store.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='62'
            # A small database also gets the visibility probe's index in place (production: release step 2).
            assert c.execute("SELECT to_regclass('messages_unpurged') IS NOT NULL AS ok").fetchone()['ok']


def migrate_attempted(watcher):
    """045's lock loop is between two NOWAIT attempts (pg_sleep), or migrate waits on a lock."""
    return watcher.execute("""SELECT 1 FROM pg_stat_activity a JOIN pg_locks l ON l.pid=a.pid AND l.locktype='advisory'
        AND l.objid=74509137 AND l.granted WHERE a.wait_event='PgSleep' OR a.wait_event_type='Lock'""").fetchone()


# spec: ANYM-511
def test_migration_045_never_holds_a_table_while_it_waits_for_another(database,tmp_path):
    import time
    from test_extend_key import migrate_behind_lock
    with schema44(database,tmp_path) as store:
        # A writer in flight on a late trigger target. Locked one by one, 045 would hold events while
        # it waits here, and an event writer would wait with it.
        outcome,writer_seconds,migrate_seconds=migrate_behind_lock(store,[('message_metadata','ROW EXCLUSIVE')],
            migrate_attempted,lambda c:c.execute('LOCK TABLE events IN ROW EXCLUSIVE MODE'))
        assert outcome==['lock_not_available'] and writer_seconds<1 and migrate_seconds<10
        with store.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='44'
        # Successive writers that each finish within the 2 s retry: 045 applies and no writer waits.
        holders=[psycopg.connect(store.pool.conninfo) for _ in range(2)]
        outcome,waits=[],[]
        def migrate():
            try:store.migrate();outcome.append('migrated')
            except psycopg.errors.LockNotAvailable:outcome.append('lock_not_available')
        def writers():
            holders[0].execute('LOCK TABLE reviews IN ROW EXCLUSIVE MODE')
            time.sleep(0.4)
            holders[1].execute('LOCK TABLE outgoing_messages IN ROW EXCLUSIVE MODE');holders[0].rollback()
            for table in ('events','ingest_receipts','attachments'):
                started=time.monotonic()
                with store.pool.connection() as c:c.execute(sql.SQL('LOCK TABLE {} IN ROW EXCLUSIVE MODE').format(
                    sql.Identifier(table)))
                waits.append(time.monotonic()-started)
            time.sleep(0.4);holders[1].rollback()
        try:
            started,errors=threads(writers)
            time.sleep(0.1)
            migrator,migrate_errors=threads(migrate)
            for t in started+migrator:t.join(15)
        finally:
            for h in holders:h.close()
        assert errors==[] and migrate_errors==[] and outcome==['migrated']
        assert max(waits)<0.5,waits
        with store.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='62'


# spec: ANYM-511
@pytest.mark.parametrize('mode',['held','timeout'])
def test_send_download_backup_and_ingest_complete_around_a_purge(sender,monkeypatch,mode):
    import time
    from messaging_service.backup import database_manifest
    from messaging_service.file_lifecycle import lifecycle_lock
    from messaging_service.message_purge import Purger
    from messaging_service.outgoing import OutgoingWorker
    s,archive,key,_,state=sender
    reset_state(s)
    p=Purger(s)
    sent=s.send_message(key,account='send-fixture',chat_id=stable_id('chat','send-fixture','100'),
                        text='sentinel-a sent',request_id='purge-send-0001')
    job=OutgoingWorker(state,None).claim()  # claimed and fenced before the purge
    assert job['id']==sent['send_id']
    archive.ingest(event(account='send-fixture',chat='100',provider_id='17',direction='outgoing',body='sentinel-a sent'))
    archive.ingest(event(account='send-fixture',chat='100',provider_id='25',body='kept with a file',
        media=[{**SCAN,'provider_id':'scan-25','filename':'kept-scan.pdf'}]))
    media=archive.claim_media('fixture',account='send-fixture')  # an unrelated collector download in flight
    assert media and media['message']==stable_id('msg','send-fixture','100','25')
    archive.ingest(event(account='send-fixture',chat='100',provider_id='17',direction='outgoing',deleted=True,
                         updated_at='2026-10-01T12:05:00Z'))
    def slow(c):
        for _ in range(3):c.execute('SELECT pg_sleep(1.5)')
    gate,inside=hold_scrub(monkeypatch,after=slow if mode=='timeout' else None)
    if mode=='timeout':gate.set()
    results,seconds={},{}
    purger,purge_errors=threads(lambda:results.update(purge=p.run_once()))
    if mode=='held':assert inside.wait(5)
    else:time.sleep(0.3)
    def timed(name,function):
        def run():
            started=time.monotonic();function();seconds[name]=time.monotonic()-started
        return run
    def finish_send():OutgoingWorker(state,None).finish(job,'sent','17')
    def ingest():assert archive.ingest(event(account='send-fixture',chat='100',provider_id='26',body='during'))['outcome']=='created'
    def download():
        writer=BlobWriter(s.files,reserve_bytes=0);archive.finish_media(media,writer.write([b'bytes']),writer)
    def backup():
        with s.pool.connection() as c:
            lifecycle_lock(c)  # the residency lock a backup snapshot holds
            assert 'message_purges' in database_manifest(c)
    workers,errors=threads(*(timed(n,f) for n,f in (('send',finish_send),('ingest',ingest),('download',download),
                                                    ('backup',backup))))
    if mode=='held':
        time.sleep(0.5);gate.set()
    for t in purger+workers:t.join(15)
    assert errors==[] and purge_errors==[]
    assert set(seconds)=={'send','ingest','download','backup'} and max(seconds.values())<3.5,seconds
    if mode=='timeout':
        assert results['purge']['purged']==0 and results['purge']['failed']==1
        monkeypatch.undo()
        with s.pool.connection() as c:c.execute('UPDATE message_purges SET next_attempt_at=clock_timestamp()')
        assert p.run_once()['purged']==1
    else:assert results['purge']['purged']==1
    status=s.get_send_status(key,sent['send_id'])
    assert status['status']=='sent'
    with s.pool.connection() as c:
        assert c.execute('SELECT body FROM outgoing_messages WHERE id=%s',(sent['send_id'],)).fetchone()['body']=='[deleted]'
        assert c.execute('SELECT digest FROM attachments WHERE message=%s',(media['message'],)).fetchone()['digest']
    assert leaks(s,'sentinel-a')=={}
