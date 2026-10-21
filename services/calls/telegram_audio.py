"""Experimental private Telegram calls; native audio lives outside AnyMsg's collector."""
import asyncio
import contextlib
import threading
import time
from pathlib import Path

from core import ACTIVE, ServiceError
from audio_probe import PCMProbe, StartTiming, diagnostics
from telegram_broker import BrokerClient


def dispose_native(py):
    # start() stores C++ callbacks capturing PyTgCalls, forming a cycle across
    # Python/native ownership. Release them while the interpreter/loop are live.
    binding=py._binding
    for name in dir(binding):
        if name.startswith('on_'):getattr(binding,name)(lambda *args:None)
    py._is_running=False
    py.executor.shutdown(wait=False,cancel_futures=True)
    py._binding=None


async def attempt(step):
    """Run one cleanup step; a failure is swallowed. True when this task was cancelled meanwhile.

    The cancel request count, not the exception, tells a cancellation of this task from a child task's
    own: a step may swallow the CancelledError (BrokerClient.close does), and an earlier handled
    cancellation leaves the count positive.
    """
    task=asyncio.current_task();before=task.cancelling()
    try:await step()
    except (Exception,asyncio.CancelledError):pass
    return task.cancelling()>before


class MediaDiagnostics:
    """Bounded Telegram media facts for owner diagnosis: no audio, ids or key material."""
    def __init__(self,limit=24):
        self.limit,self.origin=limit,time.monotonic()
        self.events=[]
        self.dropped=0
        self.lock=threading.Lock()
        self.library_versions=self.p2p_allowed=None

    def _add(self,kind,**value):
        with self.lock:
            if len(self.events)<self.limit:
                self.events.append({'kind':kind,'at_ms':round((time.monotonic()-self.origin)*1000),**value})
            else:self.dropped+=1

    # Native callbacks run off the loop thread, so the capacity check is locked; failures stay silent.
    def remote_source(self,source):
        name=lambda value:getattr(value,'name',str(value))
        with contextlib.suppress(Exception):
            self._add('remote_source',device=name(source.device),state=name(source.state))

    def connection(self,info):
        name=lambda value:getattr(value,'name',str(value))
        with contextlib.suppress(Exception):
            self._add('connection',state=name(info.state),connection_kind=name(info.kind))

    def protocol(self,protocol):
        with contextlib.suppress(Exception):
            self.library_versions=[str(value) for value in protocol.library_versions][:8]
            self.p2p_allowed=protocol.p2p_allowed

    def snapshot(self):
        with self.lock:
            return {'library_versions':self.library_versions,'p2p_allowed':self.p2p_allowed,
                    'events':[dict(event) for event in self.events],'events_dropped':self.dropped}


def observe_media(py,media):
    # PyTgCalls never registers on_remote_source_change; connection changes and the confirmed call
    # protocol are observed by wrapping its own methods, which are looked up at call time.
    # Diagnostics are best effort and must never stop a call from being placed.
    with contextlib.suppress(Exception):
        py._binding.on_remote_source_change(lambda chat_id,source:media.remote_source(source))
    with contextlib.suppress(Exception):
        connection=py._handle_connection_changed
        async def connection_changed(chat_id,net_state):
            media.connection(net_state)
            await connection(chat_id,net_state)
        py._handle_connection_changed=connection_changed
    with contextlib.suppress(Exception):
        confirm=py._app.confirm_call
        async def confirm_call(*args,**kwargs):
            protocol=await confirm(*args,**kwargs)
            media.protocol(protocol)
            return protocol
        py._app.confirm_call=confirm_call


def observe_start(state,audio,ready):
    try:state['start_timing'].frame(audio,ready)
    except Exception:
        # Recorded as unavailable, never as "no frame"; the frame itself still goes on.
        with contextlib.suppress(Exception):state['start_timing'].failed=True


def start_snapshot(state):
    try:return state['start_timing'].snapshot()
    except Exception:return {'inbound_start':None,'inbound_start_unavailable':True}


async def broker_status(socket,token):
    import json
    writer=None
    try:
        reader,writer=await asyncio.open_unix_connection(str(socket),limit=65536)
        writer.write(json.dumps({'token':token,'probe':True}).encode()+b'\n');await writer.drain()
        value=json.loads(await asyncio.wait_for(reader.readline(),3))
        return {'connected':value.get('ready') is True,'account_id':value.get('account_id'),
                'protocol':value.get('protocol'),'cooldown_secs':value.get('cooldown_secs',0),'mode':'experimental_private_calls'}
    except Exception: return {'connected':False,'error':'telegram_broker_unavailable'}
    finally:
        if writer:
            writer.close()
            with contextlib.suppress(Exception): await writer.wait_closed()


def pcm_transport(send,ready_callback):
    from pipecat.transports.base_transport import BaseTransport,TransportParams
    from pipecat.transports.base_input import BaseInputTransport
    from pipecat.transports.base_output import BaseOutputTransport
    params=TransportParams(audio_in_enabled=True,audio_out_enabled=True,audio_in_sample_rate=48000,
                           audio_out_sample_rate=48000,audio_out_channels=1,audio_out_10ms_chunks=1,
                           audio_out_end_silence_secs=0)
    class Input(BaseInputTransport):
        async def start(self,frame):
            await super().start(frame);await self.set_transport_ready(frame)
            ready_callback(self)
    class Output(BaseOutputTransport):
        async def start(self,frame):
            await super().start(frame);await self.set_transport_ready(frame)
        async def write_audio_frame(self,frame):
            await send(frame.audio);return True
    class Transport(BaseTransport):
        def __init__(self):
            super().__init__();self.i=Input(params);self.o=Output(params)
        def input(self): return self.i
        def output(self): return self.o
    return Transport()


class TelegramCalls:
    def __init__(self,config,service,key,supervisor):
        self.config,self.service,self.key,self.supervisor=config,service,key,supervisor
        self.active={}

    def token(self):
        try: return Path(self.config['token_file']).read_text().strip()
        except OSError: raise ServiceError('telegram_broker_unavailable') from None

    async def probe(self):
        try: return await broker_status(self.config['socket'],self.token())
        except (KeyError,ServiceError): return {'connected':False,'error':'telegram_broker_not_configured'}

    async def preflight(self,b):
        result=await self.probe()
        if not result['connected']: raise ServiceError('telegram_broker_unavailable')
        if result.get('cooldown_secs',0)>0:raise ServiceError('telegram_rate_limited')
        return {**result,'configured':True,'destination':b['destination'],'channel':'telegram',
                'one_existing_session':True,'pilot_verified':False}

    async def dial(self,call):
        from pytgcalls import PyTgCalls
        from pytgcalls.pytgcalls_session import PyTgCallsSession
        from pytgcalls.types import MediaStream,ExternalMedia,RecordStream,Device,StreamFrames,Direction,CallConfig,ChatUpdate
        from pytgcalls.types.raw import AudioParameters
        cid=call['id'];user=int(call['brief']['destination'])
        client=BrokerClient(self.config['socket'],self.token(),user)
        await client.connect()
        py=PyTgCalls(client,workers=2)
        state={'service':self.service,'call_id':cid,'brief':call['brief'],'supervisor':self.supervisor,
               'client':client,'py':py,'user':user,'input':None,'dial_task':asyncio.current_task(),
               'audio_probe':diagnostics(PCMProbe),'media':diagnostics(MediaDiagnostics),
               'start_timing':diagnostics(StartTiming)}
        self.active[cid]=state
        @py.on_update()
        async def updates(py,update):
            if getattr(update,'chat_id',None)!=user: return
            if isinstance(update,StreamFrames) and update.direction==Direction.INCOMING and update.device in {Device.MICROPHONE,Device.SPEAKER}:
                from pipecat.frames.frames import InputAudioRawFrame
                target=state.get('input')
                for frame in update.frames:
                    # PyTgCalls wraps NTgCalls' raw `data` as public `frame`.
                    audio=frame.frame
                    # Start timing sees every frame before the readiness gate (ANYM-450); best effort.
                    observe_start(state,audio,target is not None)
                    if target and len(audio)<=48000:
                        # A diagnostic failure must never skip the frame (same isolation as InboundAudioProbe).
                        with contextlib.suppress(Exception): state['audio_probe'].input(audio)
                        await target.push_audio_frame(InputAudioRawFrame(audio=audio,sample_rate=48000,num_channels=1))
            elif isinstance(update,ChatUpdate) and update.status & ChatUpdate.Status.DISCARDED_CALL:
                state['end_reason']='counterparty_hangup'
                if state.get('runner'): await state['runner'].cancel()
        try:
            # Version is pinned in uv.lock; do not fetch GitHub during call setup.
            PyTgCallsSession.notice_displayed=True
            await py.start()
            observe_media(py,state['media'])
            await asyncio.wait_for(py.play(user,MediaStream(ExternalMedia.AUDIO,AudioParameters(48000,1)),
                                           config=CallConfig(timeout=30)),45)
            # play() returns once NTgCalls reports the call CONNECTED.
            with contextlib.suppress(Exception):state['start_timing'].mark('peer_connected')
            await py.record(user,RecordStream(audio=True,audio_parameters=AudioParameters(48000,1)))
            peer=await py._app.get_input_call(user)
            state['provider_id']=str(peer.id)
            async def send(audio):
                await py.send_frame(user,Device.MICROPHONE,audio)
                with contextlib.suppress(Exception): state['audio_probe'].output(audio)
            def input_ready(value):
                state['input']=value
                with contextlib.suppress(Exception):state['start_timing'].mark('input_ready')
            transport=pcm_transport(send,input_ready)
            state['task']=asyncio.create_task(self._run(cid,transport,state),name='telegram-audio-'+cid)
            return {'provider_call_id':str(peer.id),'provider_status':'connected'}
        except (Exception,asyncio.CancelledError):
            # The original setup exception or cancellation propagates, whatever release does.
            await attempt(lambda:self._release(cid))
            raise

    async def _run(self,cid,transport,state):
        from voice import run_transport
        reason='disconnected'
        try:
            await asyncio.wait_for(run_transport(transport,state['brief'],self.key(state['brief']),state),state['brief']['max_duration_secs'])
            reason=state.get('end_reason',reason)
        except asyncio.CancelledError:
            reason=state.get('end_reason','owner_stop');raise
        except TimeoutError: reason='duration_limit'
        except Exception: reason='model_error'
        finally:
            try:await self._release(cid)
            finally:
                # Diagnostics are best effort: a failure here must not skip service.stop.
                with contextlib.suppress(Exception):
                    value=self.service.get_call(cid)
                    self.service.result(cid,metrics={**value['metrics'],
                        'telegram_audio':{**state['audio_probe'].snapshot(),'media':state['media'].snapshot(),
                                          'start':start_snapshot(state)}})
                self.service.stop(cid,reason)

    async def _release(self,cid):
        state=self.active.pop(cid,None)
        if not state:return
        py,cancelled=state['py'],False
        async def leave():await asyncio.wait_for(py.leave_call(state['user']),10)
        async def stop():await py._binding.stop(state['user'])
        async def close():await state['client'].close()
        # Every step is attempted. A cancellation that arrives meanwhile is held until the last step
        # has run, then re-raised, so the caller still gets to stop the call with its own reason.
        for step in (leave,stop,close):
            cancelled=await attempt(step) or cancelled
        with contextlib.suppress(Exception):dispose_native(py)
        if cancelled:raise asyncio.CancelledError

    async def hangup(self,sid):
        cancelled=False
        for cid,state in list(self.active.items()):
            if state.get('provider_id')==sid:
                cancelled=await attempt(lambda:self.cancel_attempt(cid)) or cancelled
        if cancelled:raise asyncio.CancelledError

    async def cancel_attempt(self,cid):
        state=self.active.get(cid)
        if not state:return
        state['end_reason']='owner_stop';cancelled=False
        if state.get('runner'):cancelled=await attempt(state['runner'].cancel)
        task=state.get('task') or state.get('dial_task')
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            async def joined():await task
            # The child's own cancellation is consumed here; this task's own is held and re-raised.
            cancelled=await attempt(joined) or cancelled
        cancelled=await attempt(lambda:self._release(cid)) or cancelled
        if cancelled:raise asyncio.CancelledError

    async def close(self):
        cancelled=False
        for cid in list(self.active):
            cancelled=await attempt(lambda:self.cancel_attempt(cid)) or cancelled
        if cancelled:raise asyncio.CancelledError
