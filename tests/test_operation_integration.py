"""Regression checks of command boundaries, with synthetic providers only."""
import asyncio
from types import SimpleNamespace
import pytest
from messaging_service.envelope import stable_id
from messaging_service.outgoing import OutgoingWorker,SendRejected
from messaging_service.whatsapp_collector import NodeProvider
from test_sending import sender,submit


@pytest.mark.parametrize('regrant',[False,True])
def test_legacy_sender_respects_key_ceiling_and_generation(sender,regrant):
    s,_,key,_,state=sender
    pending=submit(s,key)
    kid=s.capabilities(key)['credential_id']
    s.set_key_permissions(kid,['read'])
    if regrant:s.set_key_permissions(kid,['read','send'])
    calls=[]
    async def send(job):calls.append(job);return {'provider_message_id':'42'}
    assert asyncio.run(OutgoingWorker(state,send).once())
    assert calls==[]
    with s.pool.connection() as c:
        row=c.execute('SELECT status,error_code FROM outgoing_messages WHERE id=%s',(pending['send_id'],)).fetchone()
    assert row=={'status':'cancelled','error_code':'send_access_changed'}


def test_provider_wait_cannot_send_after_key_revocation(sender):
    s,_,key,_,state=sender
    submit(s,key)
    kid=s.capabilities(key)['credential_id']
    calls=[]
    async def send(job):
        # Emulate a provider rate gate that outlives the initial claim.
        await asyncio.to_thread(s.set_key_permissions,kid,['read'])
        await job['_preflight']()
        calls.append('must never send')
        return {'provider_message_id':'42'}
    asyncio.run(OutgoingWorker(state,send).once())
    assert calls==[]
    with s.pool.connection() as c:
        assert c.execute('SELECT status FROM outgoing_messages').fetchone()['status']=='cancelled'


def test_node_operation_uses_foreground_and_strips_internal_objects(tmp_path):
    provider=NodeProvider({'events_directory':str(tmp_path/'events')},tmp_path/'unused',None)
    calls=[]
    async def call(method,parameters,*,sending=False):
        calls.append((method,parameters,sending));return {'outcome':'confirmed','data':{}}
    provider._call_unlocked=call
    async def scenario():
        async def preflight():calls.append('preflight')
        async with provider.send_gate:
            await provider.execute_operation({'operation':'chat_info','parameters':{},'provider_chat':'123@c.us',
                'expires_at':123,'_preflight':preflight,'_policy':{'private':'not IPC'},'key_id':'not IPC'})
    asyncio.run(scenario())
    assert calls[0]=='preflight'
    assert calls[1][0]=='messenger_operation' and calls[1][2] is True
    assert set(calls[1][1])=={'operation','parameters','provider_chat','expires_at'}


def test_node_known_operation_error_keeps_pipe_synchronized(tmp_path):
    from messaging_service.commands import CommandRejected
    provider=NodeProvider({'events_directory':str(tmp_path/'events')},tmp_path/'unused',None)
    async def scenario():
        provider.send_reader=asyncio.StreamReader()
        provider.send_reader.feed_data(b'{"id":1,"error":"operation_disabled"}\n')
        provider.send_reader.feed_data(b'{"id":2,"result":{"outcome":"confirmed","data":{}}}\n')
        with pytest.raises(CommandRejected,match='operation_disabled'):
            await provider.reply(1,sending=True)
        assert (await provider.reply(2,sending=True))['result']['outcome']=='confirmed'
    asyncio.run(scenario())


# spec: ANYM-242
def test_native_not_admin_refusal_is_a_known_rejection(tmp_path):
    from messaging_service.commands import CommandRejected
    provider=NodeProvider({'events_directory':str(tmp_path/'events')},tmp_path/'unused',None)
    async def scenario():
        provider.send_reader=asyncio.StreamReader()
        provider.send_reader.feed_data(b'{"id":1,"error":"forbidden"}\n')
        with pytest.raises(CommandRejected,match='^forbidden$'):
            await provider.reply(1,sending=True)
    asyncio.run(scenario())
