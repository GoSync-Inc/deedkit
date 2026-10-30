"""Narrow call signaling over a private socket in AnyMsg's existing Telethon owner.

The collector imports only this file, not Pipecat/NTgCalls. The remote adapter has
no Telegram auth key, session file, network sender, or sign-in capability.
"""
import asyncio
import base64
import contextlib
import hmac
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid


class BrokerError(Exception): pass


def pack(value):
    if isinstance(value,bool): return {'bool':value}
    return {'tl':base64.b64encode(bytes(value)).decode()}


def unpack(value):
    if 'bool' in value: return bool(value['bool'])
    from telethon.extensions import BinaryReader
    raw=base64.b64decode(value['tl'],validate=True)
    if len(raw)>48000: raise BrokerError('payload_too_large')
    with BinaryReader(raw) as reader:
        result=reader.tgread_object()
        if reader.read(): raise BrokerError('trailing_payload')
        return result


class SessionBroker:
    def __init__(self,provider,socket,token,policy=None):
        # `policy` (the former Telegram recipient list file) is accepted and ignored since ANYM-288.
        self.provider,self.socket,self.token=provider,Path(socket),token
        self.server=None;self.current=None;self.handlers=set()

    async def start(self):
        from telethon.events import Raw
        self.event=Raw();self.provider.client.add_event_handler(self._update,self.event)
        self.socket.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        # This path belongs solely to this broker, under the collector session lock.
        self.socket.unlink(missing_ok=True)
        self.server=await asyncio.start_unix_server(self._accept,path=self.socket,limit=65536)
        os.chmod(self.socket,0o600)

    async def _send(self,state,value):
        async with state['write_lock']:
            state['writer'].write(json.dumps(value).encode()+b'\n')
            await state['writer'].drain()

    async def _update(self,update):
        from telethon.tl.types import UpdatePhoneCall,UpdatePhoneCallSignalingData,PhoneCallRequested
        state=self.current
        if not state: return
        if isinstance(update,UpdatePhoneCall):
            call=update.phone_call
            # Never offer or answer other incoming calls through the voice service.
            if isinstance(call,PhoneCallRequested): return
            if state.get('call') is None:
                if getattr(call,'participant_id',None)!=state['user'] or getattr(call,'admin_id',None)!=self.provider.expected_user_id:
                    return
                from telethon.tl.types import InputPhoneCall
                state['call']=InputPhoneCall(call.id,call.access_hash)
            if call.id!=state['call'].id: return
        elif isinstance(update,UpdatePhoneCallSignalingData):
            if not state.get('call') or update.phone_call_id!=state['call'].id: return
        else: return
        try: await self._send(state,{'update':pack(update)})
        except (ConnectionError,RuntimeError): pass

    async def _operation(self,state,msg):
        from telethon.tl.functions.messages import GetDhConfigRequest
        from telethon.tl.functions.phone import RequestCallRequest,ConfirmCallRequest,SendSignalingDataRequest,DiscardCallRequest
        from telethon.tl.types import InputPeerUser,InputUser,InputPhoneCall
        operation=msg.get('operation')
        if operation=='get_me': return {'user_id':self.provider.expected_user_id}
        if operation=='get_peer':
            who=msg.get('user')
            if type(who) is not int or who not in {state['user'],self.provider.expected_user_id}: raise BrokerError('peer_not_allowed')
            peer=await self.provider.rpc(lambda:self.provider.client.get_input_entity(who),'provider_call')
            if not isinstance(peer,InputPeerUser): raise BrokerError('private_user_required')
            return pack(peer)
        if operation!='rpc': raise BrokerError('operation_not_allowed')
        request=unpack(msg['payload'])
        if isinstance(request,GetDhConfigRequest):
            if request.random_length!=256 or request.version!=0: raise BrokerError('invalid_dh_request')
        elif isinstance(request,RequestCallRequest):
            if state.get('requested'): raise BrokerError('dial_not_allowed')
            if not isinstance(request.user_id,(InputUser,InputPeerUser)) or request.user_id.user_id!=state['user'] or request.video:
                raise BrokerError('dial_not_allowed')
            peer=await self.provider.rpc(lambda:self.provider.client.get_input_entity(state['user']),'provider_call')
            if request.user_id.access_hash!=peer.access_hash or len(request.g_a_hash)!=32:
                raise BrokerError('dial_not_allowed')
            state['requested']=True
        elif isinstance(request,(ConfirmCallRequest,SendSignalingDataRequest,DiscardCallRequest)):
            peer=state.get('call')
            if not peer or request.peer.id!=peer.id or request.peer.access_hash!=peer.access_hash:
                raise BrokerError('call_not_allowed')
            if isinstance(request,SendSignalingDataRequest) and len(request.data)>32000: raise BrokerError('signaling_too_large')
        else: raise BrokerError('rpc_not_allowed')
        result=await self.provider.rpc(lambda:self.provider.client(request),'provider_call')
        if isinstance(request,RequestCallRequest):
            call=result.phone_call;state['call']=InputPhoneCall(call.id,call.access_hash)
        if isinstance(request,DiscardCallRequest): state['discarded']=True
        return pack(result)

    async def _accept(self,reader,writer):
        task=asyncio.current_task();self.handlers.add(task);state=None
        try:
            hello=json.loads(await asyncio.wait_for(reader.readline(),5))
            user=hello.get('user')
            if not isinstance(hello.get('token'),str) or not hmac.compare_digest(hello['token'],self.token):
                raise BrokerError('authentication_required')
            if hello.get('probe') is True:
                governor=getattr(self.provider,'governor',None)
                remaining=await asyncio.to_thread(governor.state.cooldown_remaining) if governor else 0
                writer.write(json.dumps({'ready':True,'account_id':self.provider.expected_user_id,'protocol':1,'cooldown_secs':remaining}).encode()+b'\n')
                await writer.drain();return
            if self.current: raise BrokerError('another_call_active')
            # No recipient list (ANYM-288): the authenticated calls service owns the destination policy.
            if type(user) is not int or user<=0: raise BrokerError('recipient_not_allowed')
            state={'writer':writer,'write_lock':asyncio.Lock(),'user':user,'call':None}
            self.current=state
            await self._send(state,{'ready':True,'account_id':self.provider.expected_user_id,'protocol':1})
            while raw:=await asyncio.wait_for(reader.readline(),660):
                msg=json.loads(raw)
                identity=msg.get('id')
                if not isinstance(identity,str) or len(identity)>100: raise BrokerError('invalid_request')
                try:
                    result=await asyncio.wait_for(self._operation(state,msg),30)
                    response={'id':identity,'result':result}
                except BrokerError as exc: response={'id':identity,'error':str(exc)}
                except Exception: response={'id':identity,'error':'provider_rpc_unavailable'}
                await self._send(state,response)
        except BrokerError as exc:
            with contextlib.suppress(Exception):
                writer.write(json.dumps({'error':str(exc)}).encode()+b'\n');await writer.drain()
        except (Exception,asyncio.CancelledError): pass
        finally:
            if state and state.get('call') and not state.get('discarded'):
                from telethon.tl.functions.phone import DiscardCallRequest
                from telethon.tl.types import PhoneCallDiscardReasonHangup
                with contextlib.suppress(Exception):
                    # Dropped/crashed audio worker owns only this one call.
                    await asyncio.wait_for(self.provider.rpc(lambda:self.provider.client(DiscardCallRequest(
                        state['call'],0,PhoneCallDiscardReasonHangup(),0,video=False)),'provider_call'),10)
            if self.current is state: self.current=None
            writer.close()
            with contextlib.suppress(Exception): await writer.wait_closed()
            self.handlers.discard(task)

    async def close(self):
        if self.server:
            self.server.close();await self.server.wait_closed()
        for task in list(self.handlers): task.cancel()
        await asyncio.gather(*list(self.handlers),return_exceptions=True)
        self.provider.client.remove_event_handler(self._update,self.event)
        self.socket.unlink(missing_ok=True)


class BrokerClient:
    # PyTgCalls 3.0 selects its adapter by module name. This is a transport shim,
    # not a TelegramClient: it cannot authenticate or open an MTProto connection.
    __module__='telethon.voice_broker'
    _no_updates=False

    def __init__(self,socket,token,user):
        self.socket,self.token,self.user=str(socket),token,int(user)
        self.session=SimpleNamespace(dc_id=0);self._sender=None
        self.pending={};self.callbacks=[];self.received_updates=[];self.writer=None;self.reader_task=None
        self.write_lock=asyncio.Lock();self.account_id=None;self.callback_tasks=set()

    async def connect(self):
        try:
            self.reader,self.writer=await asyncio.open_unix_connection(self.socket,limit=65536)
            self.writer.write(json.dumps({'token':self.token,'user':self.user}).encode()+b'\n');await self.writer.drain()
            hello=json.loads(await asyncio.wait_for(self.reader.readline(),5))
            if not hello.get('ready'): raise BrokerError(hello.get('error','broker_unavailable'))
            self.account_id=hello['account_id']
            self.reader_task=asyncio.create_task(self._read())
        except Exception:
            await self.close();raise

    async def _read(self):
        try:
            while raw:=await self.reader.readline():
                value=json.loads(raw)
                if 'update' in value:
                    update=unpack(value['update']);self.received_updates.append(type(update).__name__)
                    self.received_updates=self.received_updates[-100:]
                    # Do not block RPC responses behind an update handler that itself invokes RPC.
                    for callback in self.callbacks:
                        task=asyncio.create_task(callback(update));self.callback_tasks.add(task)
                        task.add_done_callback(self._callback_done)
                elif value.get('id') in self.pending:
                    future=self.pending.pop(value['id'])
                    if 'error' in value: future.set_exception(BrokerError(value['error']))
                    else: future.set_result(value['result'])
        finally:
            for future in self.pending.values():
                if not future.done(): future.set_exception(BrokerError('broker_disconnected'))
            self.pending.clear()

    def _callback_done(self,task):
        self.callback_tasks.discard(task)
        if not task.cancelled():task.exception()

    async def request(self,operation,**values):
        identity=str(uuid.uuid4());future=asyncio.get_running_loop().create_future();self.pending[identity]=future
        try:
            async with self.write_lock:
                self.writer.write(json.dumps({'id':identity,'operation':operation,**values}).encode()+b'\n');await self.writer.drain()
            return await asyncio.wait_for(future,35)
        finally: self.pending.pop(identity,None)

    async def get_me(self):
        value=await self.request('get_me');return SimpleNamespace(id=value['user_id'])
    async def get_input_entity(self,user): return unpack(await self.request('get_peer',user=int(user)))
    async def _call(self,sender,request,**kwargs): return unpack(await self.request('rpc',payload=pack(request)))
    async def start(self): raise BrokerError('sign_in_not_available')
    def is_connected(self): return bool(self.writer and not self.writer.is_closing())
    def on(self,event):
        def decorate(callback): self.callbacks.append(callback);return callback
        return decorate
    async def close(self):
        for task in list(self.callback_tasks):task.cancel()
        await asyncio.gather(*list(self.callback_tasks),return_exceptions=True)
        if self.reader_task:
            self.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await self.reader_task
        if self.writer:
            self.writer.close()
            with contextlib.suppress(Exception): await self.writer.wait_closed()
        self.callbacks.clear()


if __name__=='__main__':
    raise SystemExit('Import this module from the existing Telegram session owner; do not start a second client.')
