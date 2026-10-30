"""Exact private provider acknowledgements recover IPC loss, never infer sends."""
import asyncio

import pytest

from messaging_service.collector_state import CollectorState
from messaging_service.outgoing import OutgoingWorker, reconcile_send_receipt, validate_send_receipt
from messaging_service.spool import DurableSpool
from test_archive import event
from test_whatsmeow_send_reference import native_sender as native_sender, ACCOUNT, PN, LID, PUBLIC, WIRE, REFERENCE


def receipt(queued, **changes):
    return {'kind':'outgoing_send_receipt','account':ACCOUNT,'send_id':queued['send_id'],
        'requested_chat':PN,'provider_message_id':PUBLIC,'archive_reference':REFERENCE,
        'observed_at':'2026-10-15T07:00:00Z',**changes}


@pytest.mark.parametrize('journal_first',[True,False])
def test_exact_journal_recovers_either_finish_race_and_is_removed_only_after_commit(native_sender,tmp_path,journal_first):
    store, _, key, state, queued = native_sender
    worker=OutgoingWorker(state,None)
    job=worker.claim()
    spool=DurableSpool(tmp_path/'receipts',validator=validate_send_receipt,reserve_bytes=0)
    spool.put(receipt(queued))
    if journal_first:
        assert spool.drain(lambda value:reconcile_send_receipt(state,value))==1
        # A later IPC failure cannot downgrade exact confirmed provider proof.
        worker.finish(job,'unknown',None,'send_result_unknown')
    else:
        worker.finish(job,'unknown',None,'send_result_unknown')
        assert store.get_send_status(key,queued['send_id'])['status']=='unknown'
        assert spool.drain(lambda value:reconcile_send_receipt(state,value))==1
    result=store.get_send_status(key,queued['send_id'])
    assert result['status']=='sent' and result['provider_message_id']==PUBLIC
    assert result['archive_reference']['provider_message_id']==WIRE
    assert result['recipient_delivery_confirmed'] is False
    assert not list(spool.path.glob('*.json'))
    assert reconcile_send_receipt(state,receipt(queued))=={'outcome':'duplicate_send_receipt'}
    assert worker.claim() is None  # Recovery never requeues a mutation.


def test_journal_remains_queued_when_collector_lease_is_lost_and_current_owner_can_recover(native_sender,tmp_path):
    store, archive, key, state, queued=native_sender
    worker=OutgoingWorker(state,None);job=worker.claim()
    worker.finish(job,'unknown',None,'provider_disconnected')
    spool=DurableSpool(tmp_path/'receipts',validator=validate_send_receipt,reserve_bytes=0)
    spool.put(receipt(queued))
    stale=CollectorState(archive,ACCOUNT,(state.lease[0],state.lease[1]-1))
    for invalid in (stale,CollectorState(archive,ACCOUNT,None)):
        with pytest.raises(RuntimeError,match='collector_lease_lost'):
            spool.drain(lambda value:reconcile_send_receipt(invalid,value))
        assert len(list(spool.path.glob('*.json')))==1
        assert store.get_send_status(key,queued['send_id'])['status']=='unknown'
    assert spool.drain(lambda value:reconcile_send_receipt(state,value))==1


@pytest.mark.parametrize('change',[
    {'account':'different-account'}, {'send_id':'send_'+'f'*32},
    {'requested_chat':'999999@c.us','provider_message_id':'true_999999@c.us_ABC123',
     'archive_reference':{'provider_chat_id':'999999@c.us','provider_message_id':'true_999999@c.us_ABC123'}},
    {'archive_reference':{'provider_chat_id':LID,'provider_message_id':'true_'+LID+'_OTHER'}},
    {'archive_reference':{'provider_chat_id':'999@g.us','provider_message_id':'true_999@g.us_ABC123'}},
    {'kind':'message_receipt'}, {'unexpected':'field'},
])
def test_wrong_account_send_chat_or_provider_stanza_is_never_success(native_sender,change):
    store, _, key, state, queued=native_sender
    worker=OutgoingWorker(state,None);job=worker.claim();worker.finish(job,'unknown')
    with pytest.raises(ValueError):reconcile_send_receipt(state,receipt(queued,**change))
    assert store.get_send_status(key,queued['send_id'])['status']=='unknown'


@pytest.mark.parametrize('terminal',['queued','failed','cancelled','expired'])
def test_only_an_attempted_dispatch_can_be_reconciled(native_sender,terminal):
    store, _, key, state, queued=native_sender
    with store.pool.connection() as c:
        c.execute('UPDATE outgoing_messages SET status=%s WHERE id=%s',(terminal,queued['send_id']))
    with pytest.raises(ValueError,match='send_receipt_state_mismatch'):
        reconcile_send_receipt(state,receipt(queued))
    assert store.get_send_status(key,queued['send_id'])['status']==terminal


def test_matching_body_and_time_without_exact_journal_is_not_send_proof_or_retry(native_sender):
    store, archive, key, state, queued=native_sender
    calls=[]
    async def lost(job):calls.append(job['id']);raise TimeoutError('private error')
    worker=OutgoingWorker(state,lost)
    assert asyncio.run(worker.once())
    archive.ingest(event(account=ACCOUNT,platform='whatsapp',chat=PN,
        provider_id=PUBLIC,direction='outgoing',sender='111@c.us',body='Synthetic routed send'))
    assert store.get_send_status(key,queued['send_id'])['status']=='unknown'
    assert not asyncio.run(worker.once()) and calls==[queued['send_id']]


def test_conflicting_journal_cannot_replace_a_confirmed_stanza(native_sender):
    _, _, _, state, queued=native_sender
    OutgoingWorker(state,None).claim()
    reconcile_send_receipt(state,receipt(queued))
    with pytest.raises(ValueError,match='send_receipt_conflict'):
        reconcile_send_receipt(state,receipt(queued,provider_message_id='true_'+PN+'_OTHER',
            archive_reference={'provider_chat_id':LID,'provider_message_id':'true_'+LID+'_OTHER'}))


def test_receipt_for_a_cancelled_send_is_quarantined_not_a_crash_loop(native_sender,tmp_path):
    # After restoring an older backup the row can be terminal or missing while
    # the collector still holds the provider receipt. Later receipts must drain.
    store, _, key, state, queued=native_sender
    with store.pool.connection() as c:
        c.execute("UPDATE outgoing_messages SET status='cancelled' WHERE id=%s",(queued['send_id'],))
    spool=DurableSpool(tmp_path/'receipts',validator=validate_send_receipt,reserve_bytes=0)
    spool.put(receipt(queued))
    spool.put(receipt(queued,send_id='send_'+'f'*32))
    assert spool.drain(lambda value:reconcile_send_receipt(state,value))==0
    assert not list(spool.path.glob('*.json')) and spool.quarantined()==2
    reasons=sorted(path.read_text() for path in spool.quarantine_path.glob('*.reason'))
    assert reasons==['send_receipt_state_mismatch\n','send_receipt_target_mismatch\n']
    assert store.get_send_status(key,queued['send_id'])['status']=='cancelled'
