import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_broker import SessionBroker, BrokerClient, BrokerError

@pytest.fixture
def socket_path():
    with tempfile.TemporaryDirectory(prefix='vb-',dir='/tmp') as directory:
        yield Path(directory)/'b.sock'


class Provider:
    expected_user_id=1
    def __init__(self):
        self.handlers=[];self.requests=[];self.client=self
        self.verified_self=SimpleNamespace(id=1)
    def add_event_handler(self,callback,event): self.handlers.append(callback)
    def remove_event_handler(self,callback,event): self.handlers.remove(callback)
    async def get_input_entity(self,who):
        from telethon.tl.types import InputPeerUser
        return InputPeerUser(int(who),123)
    async def rpc(self,operation,phase): return await operation()
    async def __call__(self,request): self.requests.append(request);return True


async def test_broker_uses_existing_client_rejects_unrelated_rpc_and_wrong_recipient(tmp_path,socket_path):
    policy=tmp_path/'policy.json';policy.write_text(json.dumps({'telegram':['2']}))
    provider=Provider();socket=socket_path
    broker=SessionBroker(provider,socket,'fixture',policy)
    await broker.start()
    client=BrokerClient(socket,'fixture',2)
    await client.connect()
    assert (await client.get_me()).id==1
    assert (await client.get_input_entity(2)).user_id==2
    with pytest.raises(BrokerError): await client.get_input_entity(3)
    from telethon.tl.functions.messages import SendMessageRequest
    from telethon.tl.types import InputPeerUser
    with pytest.raises(BrokerError): await client._call(None,SendMessageRequest(InputPeerUser(2,123),'not allowed'))
    assert provider.requests==[]
    # Incoming/unrelated updates never leave the owner's session.
    from telethon.tl.types import UpdatePhoneCallSignalingData
    await provider.handlers[0](UpdatePhoneCallSignalingData(999,b'unrelated'))
    assert not client.received_updates
    await client.close();await broker.close()
    assert not socket.exists() and provider.handlers==[]


async def test_wrong_token_and_second_connection_do_not_take_session(tmp_path,socket_path):
    policy=tmp_path/'policy.json';policy.write_text(json.dumps({'telegram':['2']}))
    provider=Provider();broker=SessionBroker(provider,socket_path,'fixture',policy)
    await broker.start()
    with pytest.raises(BrokerError): await BrokerClient(socket_path,'wrong',2).connect()
    first=BrokerClient(socket_path,'fixture',2);await first.connect()
    with pytest.raises(BrokerError): await BrokerClient(socket_path,'fixture',2).connect()
    await first.close();await broker.close()


async def test_shared_rate_gate_applies_to_dh_and_no_native_library_in_owner(tmp_path,socket_path):
    policy=tmp_path/'policy.json';policy.write_text(json.dumps({'telegram':['2']}))
    provider=Provider();seen=[]
    async def gate(operation,phase): seen.append(phase);return await operation()
    provider.rpc=gate
    broker=SessionBroker(provider,socket_path,'fixture',policy);await broker.start()
    client=BrokerClient(socket_path,'fixture',2);await client.connect()
    from telethon.tl.functions.messages import GetDhConfigRequest
    assert await client._call(None,GetDhConfigRequest(0,256)) is True
    assert len(provider.requests)==1 and seen==['provider_call']
    await client.close();await broker.close()


async def test_pinned_native_adapter_starts_over_broker_without_telegram_client(tmp_path,socket_path):
    from pytgcalls import PyTgCalls
    from pytgcalls.pytgcalls_session import PyTgCallsSession
    PyTgCallsSession.notice_displayed=True
    policy=tmp_path/'policy.json';policy.write_text(json.dumps({'telegram':['2']}))
    provider=Provider();broker=SessionBroker(provider,socket_path,'fixture',policy);await broker.start()
    client=BrokerClient(socket_path,'fixture',2);await client.connect()
    native=PyTgCalls(client,workers=2)
    try:
        await native.start()
        assert native._my_id==1 and native._cache_local_peer.user_id==1
        assert provider.requests==[]  # No DH or requestCall before actual play.
    finally:
        from telegram_audio import dispose_native
        await client.close();await broker.close();dispose_native(native)


async def test_broker_accepts_unlisted_user_without_policy_file(socket_path):
    # ANYM-288: the calls service owns the destination policy; the broker keeps no list.
    provider=Provider();broker=SessionBroker(provider,socket_path,'fixture')
    await broker.start()
    client=BrokerClient(socket_path,'fixture',987654321);await client.connect()
    assert (await client.get_input_entity(987654321)).user_id==987654321
    from telethon.tl.functions.phone import RequestCallRequest
    from telethon.tl.types import InputUser,PhoneCallProtocol
    request=RequestCallRequest(InputUser(987654321,123),b'a'*32,PhoneCallProtocol(65,92,['synthetic']),random_id=7)
    # The fake provider returns no call object, so the reply fails after the request passed the broker.
    with pytest.raises(BrokerError): await client._call(None,request)
    assert [type(r).__name__ for r in provider.requests]==['RequestCallRequest']
    await client.close();await broker.close()
