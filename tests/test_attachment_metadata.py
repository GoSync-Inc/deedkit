"""Synthetic attachment presentation distinguishes declarations from original proof."""
from datetime import datetime, timezone

from messaging_service.conversation import present_message


def fields(row, **kwargs):
    from messaging_service.attachment_metadata import attachment_fields
    return attachment_fields(row, **kwargs)


def pending(**changes):
    return {'attachment_id':'file_synthetic','id':None,'job_state':'queued',
        'descriptor':{'filename':'Полный каталог товаров.pdf','mime':'application/pdf','bytes':4096},
        'account':'whatsapp-fixture','chat':'chat_fixture','message_id':'msg_fixture',
        'provider_chat_id':'123@s.whatsapp.net','provider_message_id':'provider-original',
        'message_revision':3,**changes}


def test_pending_original_exposes_full_declared_metadata_and_source_without_claiming_download():
    result=fields(pending())
    assert result['id']=='file_synthetic' and result['filename']=='Полный каталог товаров.pdf'
    assert result['source_filename']==result['filename'] and result['display_name']==result['filename']
    assert result['mime']=='application/pdf' and result['bytes']==4096
    assert result['declared_mime']=='application/pdf' and result['declared_bytes']==4096
    assert result['verified_bytes'] is None and result['metadata_basis']=='provider_descriptor'
    assert result['download_status']=='queued' and result['original_presence_verified'] is False
    assert result['original_available'] is None and result['next_action']=='request_file_download'
    assert result['extraction_status']=='awaiting_download' and result['enrichment_status']=='awaiting_download'
    assert result['source']=={'account':'whatsapp-fixture','chat':'chat_fixture','message_id':'msg_fixture',
        'provider_chat_id':'123@s.whatsapp.net','provider_message_id':'provider-original','message_revision':3}
    assert 'descriptor' not in result and 'download_url' not in result


def test_paused_storage_is_a_blocked_transfer_with_an_operator_action():
    result=fields(pending(download_error_code='paused_storage',attempts=0))
    assert result['download_status']=='queued'  # Legacy queue state remains additive.
    assert result['download_phase']=='blocked' and result['download_reason']=='paused_storage'
    assert result['reason']=='paused_storage'
    assert result['next_action']=='check_media_storage_capacity' and result['attempts']==0
    row=present_message({'id':'msg_fixture','body':'','message_kind':'document','attachments':[result]},1)
    assert 'приостановлена: недостаточно места' in row['display_text']
    assert 'загружается' not in row['display_text']


def test_explicitly_requested_queued_original_has_a_poll_action():
    until=datetime(2099,1,1,tzinfo=timezone.utc)
    result=fields(pending(requested_until=until,next_due=until))
    assert result['download_request']=={'state':'requested','until':until}
    assert result['next_action']=='get_attachment' and result['download_phase']=='queued'
    assert result['next_due']==until


def test_registered_original_keeps_database_receipt_without_fabricating_presence_proof():
    row=pending(id='file_synthetic',filename='checked.txt',mime='text/plain',bytes=4,digest='a'*64,
        job_state='ready',extraction_state='ready',enrichment_state='queued')
    result=fields(row)
    assert result['download_status']=='ready' and result['metadata_basis']=='download_receipt'
    assert result['original_presence_verified'] is False and result['original_available'] is None
    assert result['verified_bytes']==4 and result['declared_bytes']==4096
    assert result['extraction_status']=='ready' and result['enrichment_status']=='queued'
    assert result['next_action']=='get_attachment'


def test_absent_registered_original_never_has_a_download_link_and_keeps_text_phase():
    row=pending(id='file_synthetic',filename='checked.txt',mime='text/plain',bytes=4,digest='a'*64,
        job_state='ready',extraction_state='ready',download_url='https://invalid.example/old-grant',
        download_expires_in=120)
    result=fields(row,original_present=False)
    assert result['download_status']=='unavailable' and result['status']=='unavailable'
    assert result['download_reason']=='original_missing'
    assert result['original_presence_verified'] is True and result['original_available'] is False
    assert result['next_action']=='check_original_storage' and result['extraction_status']=='ready'
    assert 'download_url' not in result and 'download_expires_in' not in result


def test_present_registered_original_is_distinct_from_queued_extraction():
    result=fields(pending(id='file_synthetic',filename='zero.txt',mime='text/plain',bytes=0,
        digest='a'*64,job_state='ready',extraction_state='queued'),original_present=True)
    assert result['bytes']==0 and result['verified_bytes']==0
    assert result['download_status']=='ready' and result['original_available'] is True
    assert result['extraction_status']=='queued'


def test_pending_descriptor_never_becomes_ready_from_mime_or_an_untrusted_digest():
    result=fields(pending(descriptor={'filename':'file.bin','mime':'text/plain','bytes':0,'digest':'b'*64}))
    assert result['bytes']==0 and result['verified_bytes'] is None
    assert result['download_status']=='queued' and result['sha256'] is None
    assert 'digest' not in result.get('source',{})


def test_conversation_pending_file_labels_distinguish_queue_from_active_transfer():
    queued=present_message({'id':'q','body':'','message_kind':'document',
        'attachments':[{'filename':'catalogue.pdf','download_status':'queued'}]},1)
    assert 'файл ожидает загрузки' in queued['display_text']
    active=present_message({'id':'p','body':'','message_kind':'photo',
        'attachments':[{'download_status':'processing'}]},2)
    assert 'файл загружается' in active['display_text']


from test_archive import archive as archive,event


def test_history_pending_attachment_contains_declared_metadata_and_current_occurrence(archive):
    ar,s,key,hidden=archive
    mid=ar.ingest(event(body='Synthetic catalogue',media=[{'provider_id':'media-one',
        'filename':'Полный каталог товаров.pdf','mime':'application/pdf','bytes':4096}]))['message']
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET error_code='paused_storage' WHERE message=%s",(mid,))
    item=s.search(key,'Synthetic catalogue')['items'][0]['attachments'][0]
    assert item['mime']=='application/pdf' and item['bytes']==4096
    assert item['metadata_basis']=='provider_descriptor' and item['verified_bytes'] is None
    assert item['download_reason']=='paused_storage' and item['download_phase']=='blocked'
    assert item['source']=={'account':'archive-test','chat':s.search(key)['items'][0]['chat'],
        'message_id':mid,'provider_chat_id':'100','provider_message_id':'17','message_revision':1}
    assert not s.search(hidden,'Synthetic catalogue')['items']
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET error_code=NULL,requested_until='2099-01-01T00:00:00Z' WHERE message=%s",(mid,))
    refreshed=s.get_context(key,mid)['message']['attachments'][0]
    assert refreshed['download_request']['state']=='requested' and refreshed['next_action']=='get_attachment'
