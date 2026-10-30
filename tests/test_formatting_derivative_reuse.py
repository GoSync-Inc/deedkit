"""Formatting evidence never repeats work for unchanged, current originals."""
from pathlib import Path
from contextlib import contextmanager

import psycopg
import pytest

from messaging_service.blobs import BlobWriter
from messaging_service.enrichment import EnrichmentWorker
from messaging_service.enrichment_providers import normalize_result, validate_profile
from messaging_service.file_extract import Extraction
from messaging_service.file_index import FileIndexer
from messaging_service.store import APIError
from test_archive import archive as archive, event
from test_incoming_formatting import proof


OCR_TEXT = 'ocrlighthouse ' + 'piece ' * 7000 + 'deepformatneedle'
MODEL_TEXT = 'modellighthouse ' + 'derived ' * 6000 + 'deepmodelneedle'


@pytest.fixture()
def derivatives(archive, tmp_path):
    ar, s, key, other = archive
    raw = event(media=[{'provider_id':'synthetic-image','filename':'fixture.png',
                       'mime':'image/png','bytes':15}])
    mid = ar.ingest(raw)['message']
    writer = BlobWriter(s.files, reserve_bytes=0)
    blob = writer.write([b'synthetic-image'], expected_bytes=15)
    media = ar.claim_media('formatting-fixture', account='archive-test')
    assert media is not None
    ar.finish_media(media, blob, writer)
    indexer = FileIndexer(s.pool.conninfo, s.files, extractor=lambda *_a, **kw:
        Extraction('ready', OCR_TEXT if kw['filename']=='fixture.png' else 'unrelated synthetic text',
            'synthetic-ocr-v1', metadata={'ocr_pages':1}))
    for _ in range(10):
        if not indexer.run_once()['claimed']:break
    else:raise AssertionError('synthetic extraction queue did not drain')
    secret = tmp_path / 'synthetic-model-key'
    secret.write_text('synthetic-key');secret.chmod(0o600)
    profile = validate_profile({'provider':'openai','model':'fixture-model',
        'allow_cloud':True,'api_key_file':str(secret)}, 'image')
    enrichment = EnrichmentWorker(s.pool.conninfo, s.files,
        {'accounts':['archive-test'],'max_jobs_per_day':10000,'profiles':{'image':profile}},
        processor=lambda *_:normalize_result({'title':'Synthetic document',
            'summary':'A synthetic search fixture','text':MODEL_TEXT,'document_type':'fixture'}))
    with s.pool.connection() as c:c.execute('TRUNCATE enrichment_budget')
    assert enrichment.once()
    return ar, s, key, other, raw, mid, media['id'], indexer, enrichment


def rows(s, attachment):
    with s.pool.connection() as c:
        return (c.execute('SELECT * FROM attachment_text WHERE attachment=%s',(attachment,)).fetchone(),
                c.execute('SELECT * FROM attachment_enrichment WHERE attachment=%s',(attachment,)).fetchone())


def formatted(raw, *, italic=False, minute=0):
    return {**raw,'formatting':proof(raw['body'], [{'type':'italic' if italic else 'bold',
        'offset':0,'length':6}]), 'updated_at':f'2026-10-01T12:{minute:02d}:00Z'}


def assert_searchable_current(s, key, other, attachment, revision):
    for needle in ('deepformatneedle','deepmodelneedle'):
        found=s.search_files(key,needle)['items']
        assert [x['attachment_id'] for x in found]==[attachment]
        assert found[0]['revision']==revision
        assert s.search_files(other,needle)['items']==[]
    assert s.get_file_text(key,attachment)['text']==OCR_TEXT[:4000]
    assert s.get_file_text(key,attachment,content='enriched')['text']==MODEL_TEXT[:4000]
    with pytest.raises(APIError,match='not_found'):
        s.get_file_text(other,attachment)


def test_first_native_format_preserves_ready_derivatives_and_chunk_revisions(derivatives):
    ar,s,key,other,raw,mid,attachment,indexer,enrichment=derivatives
    before=rows(s,attachment)
    assert_searchable_current(s,key,other,attachment,1)
    assert ar.ingest(formatted(raw))['outcome']=='edited'
    after=rows(s,attachment)
    for old,new in zip(before,after):
        assert new=={**old,'source_revision':2}
    with s.pool.connection() as c:
        for table in ('attachment_text_chunks','attachment_enrichment_chunks'):
            chunks=c.execute('SELECT source_revision,text_content FROM '+table+' WHERE attachment=%s ORDER BY chunk_index',
                (attachment,)).fetchall()
            assert len(chunks)>1 and all(x['source_revision']==2 for x in chunks)
    assert_searchable_current(s,key,other,attachment,2)
    assert indexer.claim() is None and enrichment.claim() is None
    assert ar.ingest(formatted(raw,italic=True,minute=1))['outcome']=='edited'
    for old,new in zip(before,rows(s,attachment)):
        assert new=={**old,'source_revision':3}
    assert_searchable_current(s,key,other,attachment,3)


@contextmanager
def reject_chunk_rebuild(s):
    tables=('attachment_text_chunks','attachment_enrichment_chunks')
    with s.pool.connection() as c:
        c.execute("""CREATE FUNCTION public.test_formatting_rebuild_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'unchanged_chunk_rebuilt'; END $$""")
        for table in tables:
            c.execute('CREATE TRIGGER test_formatting_rebuild_guard BEFORE INSERT OR DELETE OR '
                'UPDATE OF source_digest,text_content,header ON '+table+
                ' FOR EACH ROW EXECUTE FUNCTION public.test_formatting_rebuild_guard()')
    try:
        yield
    finally:
        with s.pool.connection() as c:
            for table in tables:c.execute('DROP TRIGGER test_formatting_rebuild_guard ON '+table)
            c.execute('DROP FUNCTION public.test_formatting_rebuild_guard()')


def test_ready_chunk_revision_rebind_does_not_rebuild_passage_vectors(derivatives):
    ar,s,_,_,raw,_,attachment,_,_=derivatives
    with reject_chunk_rebuild(s):
        assert ar.ingest(formatted(raw))['outcome']=='edited'
        with s.pool.connection() as c:
            for table in ('attachment_text_chunks','attachment_enrichment_chunks'):
                values=c.execute('SELECT source_revision FROM '+table+' WHERE attachment=%s',(attachment,)).fetchall()
                assert len(values)>1 and all(v['source_revision']==2 for v in values)


@pytest.mark.parametrize('leased',['extraction','enrichment'])
def test_noop_revision_update_preserves_ready_index_and_inflight_lease(derivatives,leased):
    _,s,_,_,_,mid,attachment,indexer,enrichment=derivatives
    with s.pool.connection() as c:
        table='attachment_text' if leased=='extraction' else 'attachment_enrichment'
        c.execute('UPDATE '+table+" SET state='queued',attempts=0,next_due=clock_timestamp() WHERE attachment=%s",(attachment,))
    job=indexer.claim() if leased=='extraction' else enrichment.claim()[0]
    before=rows(s,attachment)
    try:
        with reject_chunk_rebuild(s):
            with s.pool.connection() as c:
                c.execute('UPDATE messages SET revision=revision,deleted=deleted WHERE id=%s',(mid,))
        assert rows(s,attachment)==before
    finally:
        if leased=='extraction':indexer.finish(job,Extraction('ready',OCR_TEXT,'test-v1'))
        else:enrichment.finish(job,normalize_result({'text':MODEL_TEXT}))


@pytest.mark.parametrize('table',['attachment_text_chunks','attachment_enrichment_chunks'])
@pytest.mark.parametrize('damage',['missing','revision','digest','header','text','offsets'])
def test_format_only_revision_rebuilds_incomplete_or_stale_passages(derivatives,table,damage):
    ar,s,key,_,raw,_,attachment,_,_=derivatives
    with s.pool.connection() as c:
        saved=c.execute('SELECT * FROM '+table+' WHERE attachment=%s ORDER BY chunk_index',(attachment,)).fetchall()
        if damage=='missing':
            c.execute('DELETE FROM '+table+' WHERE attachment=%s AND chunk_index=0',(attachment,))
        else:
            corruption={'revision':'source_revision=99','digest':"source_digest=repeat('0',64)",
                'header':"header='unverified stale header'",'text':"text_content=repeat('x',length(text_content))",
                'offsets':'start_offset=start_offset+1,end_offset=end_offset+1'}[damage]
            c.execute('UPDATE '+table+' SET '+corruption+' WHERE attachment=%s AND chunk_index=0',(attachment,))
    assert ar.ingest(formatted(raw))['outcome']=='edited'
    with s.pool.connection() as c:
        restored=c.execute('SELECT * FROM '+table+' WHERE attachment=%s ORDER BY chunk_index',(attachment,)).fetchall()
    assert restored==[{**old,'source_revision':2} for old in saved]
    assert s.get_file_text(key,attachment)['text']==OCR_TEXT[:4000]


@pytest.mark.parametrize('state',['failed','unsupported'])
def test_format_only_revision_retains_terminal_derivative_results(derivatives,state):
    ar,s,_,_,raw,_,attachment,_,_=derivatives
    with s.pool.connection() as c:
        for table in ('attachment_text','attachment_enrichment'):
            c.execute('UPDATE '+table+" SET state=%s,text_content='',error_code='synthetic_terminal',attempts=3 WHERE attachment=%s",
                (state,attachment))
    before=rows(s,attachment)
    assert ar.ingest(formatted(raw))['outcome']=='edited'
    for old,new in zip(before,rows(s,attachment)):
        assert new=={**old,'source_revision':2}


@pytest.mark.parametrize('deleted',[False,True])
def test_real_content_change_or_deletion_invalidates_derivatives(derivatives,deleted):
    ar,s,key,_,raw,_,attachment,_,_=derivatives
    candidate={**raw,'body':'changed provider body','deleted':deleted,
        'updated_at':'2026-10-01T12:01:00Z'}
    assert ar.ingest(candidate)['outcome']==('deleted' if deleted else 'edited')
    extracted,enriched=rows(s,attachment)
    assert extracted['source_revision']==enriched['source_revision']==2
    if deleted:
        assert extracted['state']==enriched['state']=='superseded'
        assert extracted['text_content']==enriched['text_content']==''
        assert enriched['title']==enriched['summary']==''
        assert s.search_files(key,'deepformatneedle')['items']==[]
        assert s.search_files(key,'deepmodelneedle')['items']==[]
    else:
        # Editing the parent caption does not change this active CAS file.
        assert extracted['state']==enriched['state']=='ready'
        assert extracted['text_content']==OCR_TEXT and enriched['text_content']==MODEL_TEXT
        assert s.search_files(key,'deepformatneedle')['items']
        assert s.search_files(key,'deepmodelneedle')['items']


@pytest.mark.parametrize('table',['attachment_text','attachment_enrichment'])
@pytest.mark.parametrize('staleness',['revision','digest'])
def test_format_only_revision_does_not_promote_stale_derivative_proof(derivatives,table,staleness):
    ar,s,_,_,raw,_,attachment,_,_=derivatives
    with s.pool.connection() as c:
        c.execute('UPDATE '+table+' SET '+('source_revision=99' if staleness=='revision' else "source_digest=repeat('0',64)")+
            ' WHERE attachment=%s',(attachment,))
    assert ar.ingest(formatted(raw))['outcome']=='edited'
    extracted,enriched=rows(s,attachment)
    stale=extracted if table=='attachment_text' else enriched
    retained=enriched if table=='attachment_text' else extracted
    assert stale['state']==('queued' if table=='attachment_text' else 'unconfigured')
    assert stale['text_content']=='' and stale['source_revision']==2
    with s.pool.connection() as c:
        current=c.execute('SELECT digest,filename,mime,bytes FROM attachments WHERE id=%s',(attachment,)).fetchone()
    assert stale['source_digest']==current['digest']
    if table=='attachment_text':
        assert (stale['source_filename'],stale['source_mime'],stale['source_bytes'])==(
            current['filename'],current['mime'],current['bytes'])
    assert retained['state']=='ready' and retained['source_revision']==2


def test_format_only_revision_cancels_leases_and_rejects_old_worker_publication(derivatives):
    ar,s,key,_,raw,_,attachment,indexer,enrichment=derivatives
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_text SET state='queued',attempts=0,next_due=clock_timestamp() WHERE attachment=%s",(attachment,))
        c.execute("UPDATE attachment_enrichment SET state='queued',attempts=0,next_due=clock_timestamp() WHERE attachment=%s",(attachment,))
    old_extracted=indexer.claim();old_enriched,_=enrichment.claim()
    assert old_extracted['attachment']==old_enriched['attachment']==attachment
    assert ar.ingest(formatted(raw))['outcome']=='edited'
    extracted,enriched=rows(s,attachment)
    assert extracted['state']==enriched['state']=='queued'
    assert extracted['lease_token'] is enriched['lease_token'] is None
    assert extracted['attempts']==enriched['attempts']==1
    assert indexer.finish(old_extracted,Extraction('ready','stale-ocr','test-v1'))=='superseded'
    assert not enrichment.finish(old_enriched,normalize_result({'text':'stale-model'}))
    assert s.search_files(key,'stale-ocr')['items']==[]
    assert s.search_files(key,'stale-model')['items']==[]
    fresh_extracted=indexer.claim();fresh_enriched,_=enrichment.claim()
    assert fresh_extracted['source_revision']==fresh_enriched['source_revision']==2
    assert fresh_extracted['attempts']==fresh_enriched['attempts']==2
    assert indexer.finish(fresh_extracted,Extraction('ready',OCR_TEXT,'test-v1'))=='ready'
    assert enrichment.finish(fresh_enriched,normalize_result({'text':MODEL_TEXT}))


@pytest.mark.parametrize('old_hash',['',None,'not-a-content-hash'])
def test_legacy_unproven_hash_and_direct_body_change_do_not_reuse_derivatives(derivatives,old_hash):
    _,s,_,_,_,mid,attachment,_,_=derivatives
    with s.pool.connection() as c:
        c.execute('UPDATE messages SET content_hash=%s WHERE id=%s',(old_hash,mid))
        c.execute("UPDATE messages SET revision=revision+1,body='directly changed' WHERE id=%s",(mid,))
    extracted,enriched=rows(s,attachment)
    # Message caption hashes do not replace current immutable file identity.
    assert extracted['state']==enriched['state']=='ready'
    assert extracted['text_content']==OCR_TEXT and enriched['text_content']==MODEL_TEXT


def test_same_hash_with_direct_body_change_invalidates_derivatives(derivatives):
    _,s,_,_,_,mid,attachment,_,_=derivatives
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET revision=revision+1,body='directly changed' WHERE id=%s",(mid,))
    extracted,enriched=rows(s,attachment)
    # Message caption hashes do not replace current immutable file identity.
    assert extracted['state']==enriched['state']=='ready'
    assert extracted['text_content']==OCR_TEXT and enriched['text_content']==MODEL_TEXT


def test_derivative_rebind_uses_existing_trigger_privileges_without_collector_access(derivatives):
    import messaging_service.store as module
    ar,s,key,_,raw,mid,attachment,_,_=derivatives
    role='test_formatting_reuse_collector'
    ddl=Path(module.__file__).with_name('collector_role.sql').read_text().replace('messaging_collector',role)
    with s.pool.connection() as c:c.execute(ddl)
    try:
        for table in ('attachment_text','attachment_enrichment'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with s.pool.connection() as c:
                    c.execute('SET LOCAL ROLE '+role)
                    c.execute('SELECT * FROM '+table)
        with s.pool.connection() as c:
            for function in ('invalidate_message_attachment_text()','invalidate_message_enrichment()'):
                assert not c.execute('SELECT has_function_privilege(%s,%s,\'EXECUTE\') ok',(role,function)).fetchone()['ok']
            c.execute('SET LOCAL ROLE '+role)
            c.execute('UPDATE messages SET revision=revision+1 WHERE id=%s',(mid,))
        assert all(x['state']=='ready' and x['source_revision']==2 for x in rows(s,attachment))
        assert s.get_file_text(key,attachment)['text']==OCR_TEXT[:4000]
    finally:
        with s.pool.connection() as c:
            c.execute('DROP OWNED BY '+role)
            c.execute('DROP ROLE '+role)
