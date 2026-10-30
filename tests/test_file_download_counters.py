"""Chat/account download counters: one current state per file occurrence."""
from messaging_service.blobs import BlobWriter
from messaging_service.envelope import stable_id
from test_archive import archive as archive,event

CURRENT=('queued','leased','ready','failed','unavailable')


def media(*names):
    return [{'provider_id':'media-'+n,'filename':n+'.txt','mime':'text/plain','bytes':4} for n in names]


def job(c,provider_id,name):
    return c.execute('''SELECT j.id,j.message FROM media_jobs j JOIN messages m ON m.id=j.message
        WHERE m.provider_id=%s AND j.descriptor->>'provider_id'=%s''',(provider_id,'media-'+name)).fetchone()


def stored(c,row):
    # Mirrors a completed download receipt; byte presence is not part of these counters.
    c.execute('''INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES(%s,%s,%s,'x.txt','text/plain',4)
        ON CONFLICT(id) DO UPDATE SET active=true''',(row['id'],row['message'],'c'*64))
    c.execute("UPDATE media_jobs SET state='ready',error_code=NULL WHERE id=%s",(row['id'],))


def check(files,**expected):
    current={k:v for k,v in files['download'].items() if k in CURRENT and v}
    assert current==expected,files
    assert files['download'].get('ready',0)==files['stored_files']
    assert files['current_files']==sum(current.values())
    assert files['download_basis']=='current_file_occurrences'


def test_download_states_do_not_overlap_stored_originals(archive):
    ar,s,key,_=archive
    # Re-added original: downloaded, removed by an edit, then restored by a later edit.
    ar.ingest(event(provider_id='18',media=media('c')))
    claimed=ar.claim_media('worker');writer=BlobWriter(s.files,reserve_bytes=0)
    ar.finish_media(claimed,writer.write([b'data'],expected_bytes=4),writer)
    ar.ingest(event(provider_id='18',body='file removed',updated_at='2026-10-01T12:01:00Z'))
    ar.ingest(event(provider_id='18',media=media('c'),body='file back',updated_at='2026-10-01T12:02:00Z'))
    # One downloaded and one outside-window original in the same message.
    ar.ingest(event(provider_id='17',media=media('a','b')))
    # An older revision's replaced original is not a current file.
    ar.ingest(event(provider_id='20',media=media('d')))
    ar.ingest(event(provider_id='19'))
    with s.pool.connection() as c:
        assert c.execute("SELECT state FROM media_jobs WHERE id=%s",(claimed['id'],)).fetchone()['state']=='queued'
        c.execute("UPDATE media_jobs SET error_code='media_network_error' WHERE id=%s",(claimed['id'],))
        c.execute("UPDATE media_jobs SET error_code='rate_limited' WHERE id=%s",(job(c,'17','b')['id'],))
        stored(c,job(c,'17','a'));stored(c,job(c,'20','d'))
        # Fixture/legacy original without a media job.
        legacy=c.execute("SELECT id FROM messages WHERE provider_id='19'").fetchone()['id']
        c.execute('''INSERT INTO attachments(id,message,digest,filename,mime,bytes)
            VALUES('legacy-file',%s,%s,'legacy.txt','text/plain',4)''',(legacy,'d'*64))
    ar.ingest(event(provider_id='20',media=media('e'),body='replaced',updated_at='2026-10-01T12:03:00Z'))
    cid=stable_id('chat','archive-test','100')
    chat=s.resolve_reference(key,'archive-test','100')['sync']['files']
    check(chat,ready=3,queued=2)
    assert chat['stored_files']==3 and chat['download']['superseded']==1
    assert chat['download_errors']=={'rate_limited':1}
    assert chat['download_error_states']=={'rate_limited':{'queued':1}}
    assert s.get_sync_status(key,chat_id=cid)['files']==chat
    account=s.get_sync_status(key)['sources'][0]['files']
    check(account,ready=3,queued=2)
    assert account['download']=={'queued':2,'leased':0,'ready':3,'failed':0,'superseded':1}
    assert account['download_errors']=={'rate_limited':1}
    index=s.search_files(key,'absentneedle')['result_evidence']['file_index']
    assert {k:v for k,v in index['download'].items() if v}=={'ready':3,'queued':2,'superseded':1}
    # Message-level evidence uses the same rule for the restored original.
    message=s.resolve_reference(key,'archive-test','100','18')['message']
    assert [a['download_status'] for a in message['attachments'] if a['id']==claimed['id']]==['ready']


def test_ready_job_without_registered_original_is_not_counted_as_stored(archive):
    ar,s,key,_=archive
    ar.ingest(event(media=media('a')))
    with s.pool.connection() as c:c.execute("UPDATE media_jobs SET state='ready'")
    chat=s.resolve_reference(key,'archive-test','100')['sync']['files']
    check(chat,unavailable=1)
    assert chat['stored_files']==0
    account=s.get_sync_status(key)['sources'][0]['files']
    check(account,unavailable=1)
    assert account['download']['ready']==0


def test_chat_without_files_reports_zero_current_files(archive):
    ar,s,key,_=archive
    ar.ingest(event())
    chat=s.resolve_reference(key,'archive-test','100')['sync']['files']
    assert chat['stored_files']==chat['current_files']==0 and chat['download']=={}
    assert chat['download_basis']=='current_file_occurrences'
