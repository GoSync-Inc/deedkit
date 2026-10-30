"""Receipt evidence over existing APIs, using synthetic provider inputs only."""
import asyncio
import pytest

from messaging_service.envelope import stable_id
from messaging_service.outgoing import OutgoingWorker
from messaging_service.presentation import render
from test_archive import event
from test_sending import sender as sender, submit


def read_capture(account='send-fixture', maximum=42):
    return {'kind':'message_receipt','account':account,'platform':'telegram',
        'chat':'100','provider_id':None,'max_id':maximum,'state':'read',
        'basis':'telegram_outbox_boundary','audience':'private_peer',
        'observed_at':'2026-10-14T10:00:00+00:00','provider_at':None}


def test_unsent_action_explicitly_has_unknown_receipt(sender):
    store, _, key, _, _ = sender
    action=submit(store,key)
    assert action['delivery_receipt']['state']=='unknown'
    assert action['read_at'] is None
    assert action['delivered_at'] is None
    assert action['recipient_delivery_confirmed'] is False


def test_plain_outgoing_history_does_not_invent_receipts(sender):
    store,archive,key,_,_=sender
    mid=archive.ingest(event(account='send-fixture',provider_id='42',direction='outgoing'))['message']
    row=store.get_context(key,mid,before_count=0,after_count=0)['message']
    assert row['delivery_receipt']['state']=='unknown'
    compact=render('get_context',{'message':row,'context':[],'attachments':[],
        'pending_attachments':[]},'compact')
    assert compact['message']['delivery_receipt']['state']=='unknown'


def test_receipt_backup_inventory_preserves_pending_work(sender):
    from messaging_service.backup import database_manifest
    store,_,_,_,state=sender
    state.apply(read_capture())
    with store.pool.connection() as c:
        manifest=database_manifest(c)
    assert manifest['message_receipts']['rows']==1
    assert manifest['receipt_event_jobs']['rows']==1
    assert manifest['receipt_event_publications']['rows']==0


def test_read_observation_exposed_without_exact_read_time(sender):
    store,archive,key,_,state=sender
    action=submit(store,key)
    async def provider(job):return {'provider_message_id':'42'}
    assert asyncio.run(OutgoingWorker(state,provider).once())
    mid=archive.ingest(event(account='send-fixture',provider_id='42',direction='outgoing',
        body='Exact synthetic text'))['message']
    state.apply(read_capture())
    state.publish_receipts()
    status=store.get_send_status(key,action['send_id'])
    assert status['status']=='sent'
    assert status['recipient_delivery_confirmed'] is True
    assert status['read_at'] is None and status['delivered_at'] is None
    assert status['delivery_receipt']['state']=='read'
    assert status['delivery_receipt']['read_observed_at']=='2026-10-14T10:00:00+00:00'
    rows=[store.get_context(key,mid,before_count=0,after_count=0)['message'],
        store.get_messages(key,stable_id('chat','send-fixture','100'))['items'][0],
        store.search(key,query='Exact synthetic text')['items'][0]]
    assert all(row['delivery_receipt']['state']=='read' for row in rows)
    # Existing views do not silently change their filter binding.
    first=store.get_updates(key,'receipt-api',kinds=['message.created'])
    import pytest
    from messaging_service.store import APIError
    with pytest.raises(APIError,match='consumer_filter_mismatch'):
        store.get_updates(key,'receipt-api',kinds=['message.status'])
    page=store.get_updates(key,'receipt-status-api',kinds=['message.status'])
    assert len(page['items'])==1
    assert page['items'][0]['message']==mid
    assert page['items'][0]['delivery_receipt']['state']=='read'
    assert render('get_updates',page,'compact')['items'][0]['delivery_receipt']['state']=='read'
    assert first['filters']=={'kinds':['message.created']}


@pytest.mark.parametrize('group',[False,True])
def test_whatsapp_status_before_ingestion_and_frozen_updates(seeded,group):
    from messaging_service.archive import Archive
    from messaging_service.collector_state import CollectorState
    from messaging_service.store import APIError
    store,_,_=seeded;archive=Archive(store)
    account='receipt-wa-api';chat='100@g.us' if group else '100@c.us'
    provider_id='true_'+chat+'_synthetic'
    archive.register(account,'whatsapp','Synthetic receipt account',discover=True)
    archive.ingest(event(account=account,platform='whatsapp',chat=chat,provider_id='existing'))
    key=store.issue_key('receipt-wa-reader',[account],permissions=['read','send'])
    epoch=archive.acquire(account,'receipt-wa-worker')
    state=CollectorState(archive,account,('receipt-wa-worker',epoch))
    state.phase('connection','connected','provider_ready');archive.set_sending(account,True)
    action=store.send_message(key,account,stable_id('chat',account,chat),
        'Synthetic receipt payload','receipt-wa-api-0001')
    async def provider(job):return {'provider_message_id':provider_id}
    assert asyncio.run(OutgoingWorker(state,provider).once())
    capture={'kind':'message_receipt','account':account,'platform':'whatsapp','chat':chat,
        'provider_id':provider_id,'max_id':None,'state':'server','basis':'whatsapp_message_ack',
        'audience':'provider_aggregate' if group else 'private_peer',
        'observed_at':'2026-10-14T10:00:00Z','provider_at':None}
    state.apply(capture)
    assert store.get_send_status(key,action['send_id'])['delivery_receipt']['state']=='server'
    mid=archive.ingest(event(account=account,platform='whatsapp',chat=chat,
        provider_id=provider_id,direction='outgoing',body='Synthetic receipt payload'))['message']
    state.publish_receipts()
    state.apply({**capture,'state':'read','observed_at':'2026-10-14T10:01:00Z'})
    state.publish_receipts()
    page=store.get_updates(key,'wa-receipt-snapshots',kinds=['message.status'])
    assert [r['delivery_receipt']['state'] for r in page['items']]==['server','read']
    assert all(r['message']==mid for r in page['items'])
    status=store.get_send_status(key,action['send_id'])
    assert status['delivery_receipt']['state']=='read'
    assert status['recipient_delivery_confirmed'] is (not group)
    assert status['read_at'] is None
    assert store.send_message(key,account,stable_id('chat',account,chat),
        'Synthetic receipt payload','receipt-wa-api-0001')['delivery_receipt']['state']=='read'
    store.set_access('receipt-wa-reader',chats=[])
    with pytest.raises(APIError,match='not_found'):store.get_send_status(key,action['send_id'])
    with pytest.raises(APIError,match='not_found'):store.get_context(key,mid)
