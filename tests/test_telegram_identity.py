"""No provider session: repairs use a verified synthetic owner and confirmed outbox."""
import asyncio
from copy import deepcopy

import pytest
from telethon import types

from messaging_service.envelope import stable_id
from messaging_service.outgoing import OutgoingWorker
from test_archive import event
from test_sending import sender as sender,submit


def sent_fixture(sender,*,body='Exact synthetic text',direction='outgoing',chat_type='private'):
    s,archive,key,_,state=sender
    action=submit(s,key)
    async def send(job):return {'provider_message_id':'42'}
    asyncio.run(OutgoingWorker(state,send).once())
    mid=archive.ingest(event(account='send-fixture',provider_id='42',sender='',body=body,
        direction=direction,metadata={'chat_type':chat_type}))['message']
    return s,archive,key,state,action,mid


def test_repair_confirmed_private_send_uses_new_revision_and_no_provider_calls(sender):
    from messaging_service.telegram_identity import repair_sent_senders
    s,archive,key,state,action,mid=sent_fixture(sender)
    with s.pool.connection() as c:
        before=deepcopy(c.execute('SELECT snapshot FROM message_revisions WHERE message=%s',(mid,)).fetchone()['snapshot'])
    owner=types.User(id=42,is_self=True,first_name='Synthetic',username='fixture_owner')
    assert repair_sent_senders(archive,'send-fixture',state.lease,owner)==1
    assert repair_sent_senders(archive,'send-fixture',state.lease,owner)==0
    message=s.get_context(key,mid)['message']
    assert message['sender']=='42' and message['username']=='fixture_owner' and message['revision']==2
    with s.pool.connection() as c:
        assert c.execute('SELECT snapshot FROM message_revisions WHERE message=%s AND revision=1',(mid,)).fetchone()['snapshot']==before
        assert c.execute('SELECT count(*) AS n FROM outgoing_messages').fetchone()['n']==1


@pytest.mark.parametrize('change',[
    {'body':'different text'},{'direction':'incoming'},{'chat_type':'group'}])
def test_repair_does_not_guess_sender_without_matching_confirmed_private_send(sender,change):
    from messaging_service.telegram_identity import repair_sent_senders
    s,archive,key,state,_,mid=sent_fixture(sender,**change)
    owner=types.User(id=42,is_self=True,first_name='Synthetic')
    assert repair_sent_senders(archive,'send-fixture',state.lease,owner)==0
    assert s.get_context(key,mid)['message']['sender']==''


def test_repair_requires_verified_self_active_lease_and_matching_reply(sender):
    from messaging_service.telegram_identity import repair_sent_senders
    s,archive,key,state,action,mid=sent_fixture(sender)
    assert repair_sent_senders(archive,'send-fixture',state.lease,None)==0
    assert repair_sent_senders(archive,'send-fixture',state.lease,types.User(id=42))==0
    owner=types.User(id=42,is_self=True,first_name='Synthetic')
    with s.pool.connection() as c:c.execute('UPDATE outgoing_messages SET reply_provider_id=%s WHERE id=%s',('999',action['send_id']))
    assert repair_sent_senders(archive,'send-fixture',state.lease,owner)==0
    with s.pool.connection() as c:
        c.execute('UPDATE outgoing_messages SET reply_provider_id=NULL WHERE id=%s',(action['send_id'],))
        c.execute("UPDATE collector_leases SET expires_at=now()-interval '1 second' WHERE account='send-fixture'")
    with pytest.raises(RuntimeError,match='collector_lease_lost'):
        repair_sent_senders(archive,'send-fixture',state.lease,owner)
    assert s.get_context(key,mid)['message']['sender']==''


def test_collector_passes_cached_verified_owner_to_capture_and_history(sender,tmp_path):
    from types import SimpleNamespace
    from datetime import datetime,timezone
    from messaging_service.collector_state import CaptureSpool
    from messaging_service.telegram_collector import TelegramCollector
    s,archive,key,_,state=sender
    owner=types.User(id=42,is_self=True,first_name='Synthetic')
    provider=SimpleNamespace(verified_self=owner)
    spool=CaptureSpool(tmp_path/'capture',reserve_bytes=0)
    collector=TelegramCollector(archive,'send-fixture',provider,spool)
    collector.state=state;collector.policy=state.policy()
    message=types.Message(id=42,peer_id=types.PeerUser(100),out=True,message='fixture',
        date=datetime(2026,10,1,tzinfo=timezone.utc))
    async def run():
        await collector.capture(SimpleNamespace(message=message))
        await collector.commit_page([message],'100','Fixture','user','reconcile')
    asyncio.run(run())
    assert not collector.failed.is_set()
    assert s.get_context(key,stable_id('msg','send-fixture','100','42'))['message']['sender']=='42'
