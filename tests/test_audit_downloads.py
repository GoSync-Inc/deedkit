"""Current attachment presentation through public download/request paths."""
import pytest
from messaging_service.blobs import BlobWriter
from messaging_service.file_index import file_response_fields
from test_archive import archive as archive, event

@pytest.mark.parametrize('kind,generated',[('photo',True),('document',False)])
def test_download_receipt_has_current_source_filename_proof(archive,kind,generated):
    ar,store,_,_=archive
    ar.register('synthetic-wa','whatsapp','Synthetic source',discover=True)
    key=store.issue_key('synthetic-reader',['synthetic-wa'],permissions=['read','download'])
    provider='false_1234567890@c.us_SYNTHETIC'
    ar.ingest(event(account='synthetic-wa',platform='whatsapp',provider_id=provider,
        metadata={'media_kind':kind},media=[{'provider_id':'synthetic-media',
        'filename':provider+'.bin','mime':'image/jpeg','bytes':4}]))
    job=ar.claim_media('synthetic-fixture',account='synthetic-wa')
    writer=BlobWriter(store.files,reserve_bytes=0)
    ar.finish_media(job,writer.write([b'xxxx']),writer=writer)
    link=store.get_attachment(key,job['id'])['download_url']
    grant=link.rsplit('/',1)[-1]
    row,path,binding=store.download(grant)
    assert path.read_bytes()==b'xxxx'
    expected='attachment-'+job['id']+'.jpg' if generated else provider+'.bin'
    assert file_response_fields(row)['download_filename']==expected
    assert row['filename']==provider+'.bin'
    # Revalidating an admitted transfer retains the same current source metadata.
    with store.pool.connection() as c:
        c.execute('DELETE FROM download_grants')
    active_row,active_path,_=store.download(grant,active=binding)
    assert active_path==path
    assert file_response_fields(active_row)['download_filename']==expected


def test_fifth_active_media_attempt_is_still_processing_when_requested(archive):
    ar,store,key,_=archive
    ar.ingest(event(media=[{'provider_id':'synthetic-media','filename':'image.jpg',
        'mime':'image/jpeg','bytes':4}]))
    job=ar.claim_media('synthetic-fixture')
    with store.pool.connection() as c:
        c.execute('UPDATE media_jobs SET attempts=5 WHERE id=%s',(job['id'],))
    result=store.request_file_download(key,job['id'])
    assert result['download_status']=='processing'
    assert result['request_status'] in ('coalesced','processing')
    assert result.get('reason')!='retry_exhausted'
    assert result['next_action']=='get_attachment'


def test_expired_fifth_media_lease_reports_exhaustion(archive):
    ar,store,key,_=archive
    ar.ingest(event(media=[{'provider_id':'synthetic-media','filename':'image.jpg',
        'mime':'image/jpeg','bytes':4}]))
    job=ar.claim_media('synthetic-fixture')
    with store.pool.connection() as c:
        c.execute("UPDATE media_jobs SET attempts=5,lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(job['id'],))
    result=store.request_file_download(key,job['id'])
    assert result['download_status']=='failed'
    assert result['request_status']=='failed'
    assert result['reason']=='retry_exhausted'
    assert result['download_retry_scheduled'] is False
