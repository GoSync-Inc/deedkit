"""Private voice controls; signed public Twilio media and a narrow Telegram broker."""
import asyncio
import contextlib
import os
import sys
import json
import re
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from anymsg import AnyMsg, UnconfiguredArchive
from core import ACTIVE, HANDOVER_SECS, MODEL, GROK_MODEL, GROK_VOICES, PROMPT_VERSION, VOICES, BriefInput, CallVoice, ServiceError, VoiceService, now
from control import control_key, create_control
from agent_api import AgentAPI, AgentRegistry
from learning import LearningWorker, ANALYZER_MODEL
from supervisor import Supervisor, SUPERVISOR_MODEL
from outbound import Outbound,CallingSettings,TwilioCalls,ExternalBrief
from destination_policy import PREMIUM_UNITS,recipient_policy
from settings import Setup,read_private,write_private
from call_errors import record_model_error
from security import OwnerCredential, VoiceBoundary, trusted_peers

SOURCE = Path(__file__).resolve()
PRIVATE = Path(os.environ.get('VOICE_STATE_DIR', str(Path.home() / '.local' / 'share' / 'deedkit' / 'calls')))


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Credentials(Input):
    gemini: SecretStr | None = Field(default=None, min_length=1, max_length=512)
    xai: SecretStr | None = Field(default=None, min_length=1, max_length=512)


class Search(Input):
    query: str = Field(min_length=2, max_length=200)
    account: str = Field(default='', max_length=200)
    chat: str = Field(default='', max_length=200)


class Start(Input):
    brief_id: str = Field(min_length=1, max_length=100)


class Join(Input):
    call_id: str = Field(min_length=1, max_length=100)


class Offer(Input):
    sdp: str = Field(min_length=1, max_length=100000)
    type: Literal['offer']
    requestData: Join
    pc_id: str | None = Field(default=None, max_length=100)
    restart_pc: bool | None = None


class Candidate(Input):
    candidate: str = Field(max_length=4000)
    sdpMid: str | None = Field(default=None, max_length=100)
    sdpMLineIndex: int | None = Field(default=None, ge=0, le=100)


class Patch(Input):
    pc_id: str = Field(max_length=100)
    candidates: list[Candidate] = Field(max_length=32)


class OwnerDecision(Input):
    answer: str = Field(min_length=1, max_length=1000)


class AudioHealth(Input):
    availability: Literal['pending','supported','unavailable'] = 'pending'
    samples: int = Field(default=0,ge=0,le=100000)
    packets_received: int | None = Field(default=None,ge=0,le=10**12)
    packets_lost: int | None = Field(default=None,ge=0,le=10**12)
    concealed_samples: int | None = Field(default=None,ge=0,le=10**12)
    silent_concealed_samples: int | None = Field(default=None,ge=0,le=10**12)
    packets_discarded: int | None = Field(default=None,ge=0,le=10**12)
    concealment_events: int | None = Field(default=None,ge=0,le=10**12)
    total_samples_received: int | None = Field(default=None,ge=0,le=10**12)
    concealed_percent: float | None = Field(default=None,ge=0,le=100)
    jitter_ms: float | None = Field(default=None,ge=0,le=600000)
    jitter_buffer_ms: float | None = Field(default=None,ge=0,le=600000)
    browser_stalls: int = Field(default=0,ge=0,le=100000)
    max_poll_gap_ms: float = Field(default=0,ge=0,le=600000)
    audio_context_state: Literal['running','suspended','interrupted','closed'] | None = None
    player_waiting_events: int = Field(default=0,ge=0,le=100000)


class Metrics(Input):
    p50: float | None = Field(default=None, ge=0, le=600000)
    p95: float | None = Field(default=None, ge=0, le=600000)
    n: int = Field(default=0, ge=0, le=1000)
    interruption_stop_ms: list[float] = Field(default_factory=list, max_length=1000)
    method: Literal['browser_audio_rms'] = 'browser_audio_rms'
    audio_health: AudioHealth | None = None


class Review(Input):
    task: Literal['pass', 'partial', 'fail']
    naturalness: int = Field(ge=1, le=5)
    notes: str = Field(default='', max_length=2000)


class Result(Input):
    metrics: Metrics | None = None
    review: Review | None = None


class CaseReview(Input):
    status:Literal['accepted','rejected']
    turns:list[str] | None=Field(default=None,min_length=1,max_length=8)
    expected_behavior:str | None=Field(default=None,min_length=3,max_length=1500)


def create_app(data_dir=None, archive=None, control_token=None, reviewer=None, advisor=None, agent_registry=None,
               callback_limits=None):
    directory = Path(data_dir or os.environ.get('VOICE_DATA_DIR',str(PRIVATE / 'data')))
    if data_dir is None and (not directory.is_absolute() or not directory.is_dir()):
        raise ValueError("Set VOICE_DATA_DIR to an explicitly initialized absolute state directory")
    peers = trusted_peers(os.environ.get('VOICE_TRUSTED_PEERS'))
    service = VoiceService(directory)
    try:
        owner = OwnerCredential(directory)
    except Exception:
        service.close()
        raise
    archive_config = Path(os.environ.get('VOICE_ANYMSG_CONFIG', str(PRIVATE / 'anymsg.json')))
    archive = archive if archive is not None else AnyMsg.from_config(archive_config) if archive_config.exists() else UnconfiguredArchive()
    keys, session = {}, {}
    def model_key(brief):
        return keys.get('xai' if brief.get('dialog_engine')=='grok_realtime' else 'gemini')

    def credential_status():
        return {name:bool(keys.get(name)) for name in ('gemini','xai')}
    service.engine_credentials=credential_status
    control_token=control_token or control_key(directory)
    lock = asyncio.Lock()
    learner=LearningWorker(service,lambda:keys.get('gemini'),reviewer=reviewer)
    supervisor=Supervisor(service,lambda:keys.get('gemini'),advisor=advisor)
    settings_path=directory/'calling.json'
    saved=read_private(settings_path)
    policy=CallingSettings(enabled=saved.get('enabled',False),allowed=saved.get('allowed',{}))
    providers={}
    if saved.get('twilio'):providers['twilio']=TwilioCalls(saved['twilio'])
    if os.environ.get('VOICE_TELEGRAM_SOCKET'):
        from telegram_audio import TelegramCalls
        providers['telegram']=TelegramCalls({'socket':os.environ['VOICE_TELEGRAM_SOCKET'],
            'token_file':os.environ['VOICE_TELEGRAM_TOKEN_FILE']},service,model_key,supervisor)
    outbound=Outbound(service,lambda:keys.get('gemini'),policy,providers,model_key=model_key)
    persist=os.environ.get('VOICE_PERSIST_CREDENTIALS')=='1'
    browser_enabled=os.environ.get('VOICE_BROWSER_ENABLED','1')=='1'
    if persist:
        keys.update(read_private(directory/'model-key.json'))

    def voice_provider():
        try:return service.call_voice().provider
        except ServiceError:return None

    async def cancel_learning():
        task=getattr(app.state,'learning_task',None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def stop_runtime(identity=None, reason='owner_stop'):
        if not session or (identity and session.get('call_id') != identity):
            if identity:
                if service.get_call(identity)['channel']!='browser':await outbound.stop(identity,reason)
                else:service.stop(identity, reason)
            return
        # UI disconnect cleanup can race the model error callback. Keep the
        # failure already observed instead of reclassifying it as owner_stop.
        # A phone call keeps its current media session's state apart (ANYM-451).
        live=session.get('state',session)
        if live.get('end_reason')=='model_error':reason='model_error'
        live['end_reason'] = reason
        runner = live.get('runner')
        if runner:
            await runner.cancel()
        await app.state.rtc.close()
        task = session.get('task')
        if task and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        session.clear()
        if identity:
            if service.get_call(identity)['channel']!='browser':await outbound.stop(identity,reason)
            else:service.stop(identity, reason)

    outbound.runtime_stop=stop_runtime

    @asynccontextmanager
    async def lifespan(app):
        from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler
        app.state.rtc = SmallWebRTCRequestHandler(ice_servers=[])
        await outbound.recover()
        app.state.learning_task=asyncio.create_task(learner.run(),name='voice-post-call-analysis')
        try:
            async with app.state.control_app.router.lifespan_context(app.state.control_app):
                yield
        finally:
            await cancel_learning()
            await stop_runtime(session.get('call_id'), 'service_shutdown')
            await outbound.close()
            for call in service.list_calls():
                if call['status'] in ACTIVE:
                    service.stop(call['id'], 'service_shutdown')
            keys.clear()
            if hasattr(archive, 'close'):
                await archive.close()
            service.close()

    app = FastAPI(title='Voice Tasks — Pipecat', lifespan=lifespan)
    app.state.service, app.state.archive = service, archive
    app.state.session = session
    app.state.supervisor=supervisor
    app.state.outbound=outbound

    app.add_middleware(VoiceBoundary, owner=owner, control_token=control_token,
                       outbound=outbound, limits=callback_limits, peers=peers)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse({'detail':'Некорректные параметры запроса.'},status_code=422)

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        return JSONResponse({'detail':exc.code},status_code=exc.status)

    async def archive_status():
        try:
            result = await archive.capabilities()
            return {**result, 'connected':result.get('configured', True)}
        except Exception:
            return {'connected':False,'coverage':'not_checked','accounts':[], 'error':'archive_unavailable'}

    @app.get('/api/health')
    async def health():
        # Readiness of this process only; neighbours are reported by /api/config.
        return {'ok':True}

    @app.get('/api/config')
    async def config():
        learned=service.learning.active_candidate()
        connected=await calling_status()
        channels={'browser':'available' if browser_enabled else 'local_rehearsal_only'}
        for channel in ('telegram','whatsapp','phone','sip'):
            channels[channel]=('experimental_ready' if connected['telegram']['connected'] else 'not_connected') if channel=='telegram' else ('configured_needs_pilot' if connected['twilio']['configured'] else 'not_connected')
        return {'credentials':credential_status(),'model':MODEL,'voices':VOICES,
                'voice_providers':{'gemini':{'model':MODEL,'voices':VOICES},
                                   'xai':{'model':GROK_MODEL,'voices':GROK_VOICES,'scope':['browser','telegram','whatsapp','phone','sip']}},
                'call_voice':app.state.agent_api.call_voice_summary(),
                'prompt_version':learned['prompt_version'] if learned else PROMPT_VERSION,'framework':'Pipecat 1.12.0', 'transport':'SmallWebRTC',
                'dialog_engines':['gemini_live','gemini_live_flows','grok_realtime'],
                'live_flows':{'scope':'browser_prototype','context_strategy':'APPEND','fixed_tools':True,
                              'dynamic_tools':False,'remote_context_reset':False,'custom_tts':False},
                'analysis':{'automatic':True,'model':ANALYZER_MODEL,'audio_recorded':False},
                'supervisor':{'model':SUPERVISOR_MODEL,'mode':'on_demand_advice','timeout_secs':6,
                              'owner_approval_authority':False,'scope':'selected_task_and_conversation'},
                'owner_workflow':{'attention':'asynchronous','notification_channel':'not_connected','followup':'fresh_authorized_brief'},
                'archive':await archive_status(), 'active_call_id':next((v['id'] for v in service.list_calls() if v['status'] in ACTIVE),None),
                'channels':channels,'call_cap':outbound.cap_status()}

    @app.get('/api/calling')
    async def calling_status():
        twilio=outbound.providers.get('twilio');telegram=outbound.providers.get('telegram')
        cfg=twilio.config if twilio else {}
        return {'enabled':outbound.settings.enabled,'allowed':outbound.settings.allowed,
            'model_key_present':any(credential_status().values()),'model_credentials':credential_status(),
            'credential_storage':'private_server_file' if persist else 'memory_only',
            'twilio':{'configured':bool(cfg.get('account_sid') and cfg.get('auth_token')),
                      **{k:cfg.get(k,'') for k in ('account_sid','phone_from','whatsapp_from','public_url','sip_from','sip_auth_username')}},
            'telegram':await telegram.probe() if telegram else {'connected':False,'error':'telegram_broker_not_configured'},
            'live_call_verified':{'telegram':False,'whatsapp':False,'phone':False,'sip':False}}

    @app.post('/api/calling')
    async def calling_setup(body:Setup):
        async with lock:
            if any(c['status'] in ACTIVE for c in service.list_calls()):raise ServiceError('another_call_active')
            cfg=body.twilio.private()
            if not cfg['auth_token'] and outbound.providers.get('twilio'):
                cfg['auth_token']=outbound.providers['twilio'].config.get('auth_token','')
            if not cfg['sip_auth_password'] and outbound.providers.get('twilio') and cfg['sip_auth_username']:
                cfg['sip_auth_password']=outbound.providers['twilio'].config.get('sip_auth_password','')
            if outbound.providers.get('twilio'):await outbound.providers['twilio'].close()
            if cfg['account_sid'] or cfg['auth_token'] or cfg['public_url']:outbound.providers['twilio']=TwilioCalls(cfg)
            else:outbound.providers.pop('twilio',None)
            outbound.settings=CallingSettings(enabled=body.enabled,allowed=body.allowed)
            write_private(settings_path,{**outbound.settings.model_dump(),'twilio':cfg})
        return await calling_status()

    @app.post('/api/calling/twilio/probe')
    async def twilio_probe():
        provider=outbound.providers.get('twilio')
        return await provider.probe() if provider else {'connected':False,'error':'twilio_credentials_required'}

    @app.get('/api/call-voice')
    async def call_voice():
        return service.call_voice().model_dump()

    @app.post('/api/call-voice')
    async def set_call_voice(body:CallVoice):
        # Agent briefs already prepared keep the voice frozen in them.
        return service.set_call_voice(body).model_dump()

    @app.post('/api/external/briefs')
    async def prepare_external(body:ExternalBrief):
        return await prepare(body)

    @app.post('/api/credentials')
    async def credentials(body: Credentials):
        supplied={name:value.get_secret_value().strip() for name in ('gemini','xai') if (value:=getattr(body,name)) is not None}
        if not supplied or not all(supplied.values()):
            raise HTTPException(422,'Введи API-ключ выбранной модели.')
        keys.update(supplied)
        if persist:write_private(directory/'model-key.json',keys)
        return {'credentials':credential_status()}

    @app.delete('/api/credentials')
    async def forget():
        async with lock:
            for call in service.list_calls():
                if call['status'] in ACTIVE:await stop_runtime(call['id'],'credentials_removed')
            await cancel_learning()
            keys.clear()
            if persist:(directory/'model-key.json').unlink(missing_ok=True)
            app.state.learning_task=asyncio.create_task(learner.run(),name='voice-post-call-analysis')
        return {'credentials':credential_status()}

    @app.post('/api/context/search')
    async def search(body: Search):
        try:
            found=await archive.search(body.query,body.account,body.chat)
            # Expose only selection fields; no arbitrary archive metadata or neighboring chats.
            fields={'id','snippet','account','chat','sender','sent_at','revision','body_truncated','evidence','integrity'}
            return {'items':[{k:v for k,v in row.items() if k in fields} for row in found.get('items',[])[:12]],
                    'coverage':found.get('coverage','partial')}
        except Exception:
            raise HTTPException(502,'Не удалось прочитать AnyMsg. Контекст не добавлен.') from None

    @app.post('/api/briefs')
    async def prepare(body: BriefInput):
        try:
            messages=[await archive.message(identity) for identity in dict.fromkeys(body.source_message_ids)]
        except Exception:
            raise HTTPException(502,'Не удалось получить выбранное сообщение. Проверь источник.') from None
        return service.prepare(body,messages)

    @app.get('/api/briefs/{identity}')
    async def brief(identity: str):
        return service.get_brief(identity)

    @app.post('/api/calls/start')
    async def start(body: Start):
        async with lock:
            if not browser_enabled:raise ServiceError('use_local_browser_rehearsal')
            if service.get_brief(body.brief_id)['channel']!='browser':raise ServiceError('use_dial_call')
            return service.start(body.brief_id)

    @app.get('/api/calls')
    async def calls(limit:int=Query(default=100,ge=1,le=100),offset:int=Query(default=0,ge=0)):
        items=service.list_calls(limit,offset)
        return {'items':items,'next_offset':offset+limit if len(items)==limit else None}

    @app.get('/api/decisions')
    async def inbox(limit:int=Query(default=100,ge=1,le=100),offset:int=Query(default=0,ge=0)):
        items=service.decision_inbox(limit,offset)
        return {'items':items,'next_offset':offset+limit if len(items)==limit else None,
                'notification_channel':'not_connected','callback_available':False}

    @app.post('/api/calls/{identity}/decisions/{decision_id}/followup')
    async def followup(identity:str,decision_id:str):
        async with lock:
            return service.prepare_followup(identity,decision_id)

    @app.get('/api/calls/{identity}/analysis')
    async def analysis(identity:str):
        return service.learning.report(identity)

    @app.post('/api/calls/{identity}/analysis/retry')
    async def retry_analysis(identity:str):
        return service.learning.retry(identity)

    @app.get('/api/learning')
    async def learning():
        candidate=service.learning.active_candidate()
        return {'active_prompt_version':candidate['prompt_version'] if candidate else PROMPT_VERSION,
                'cases':service.learning.cases(),'candidates':service.learning.candidates(),
                'evaluations':service.learning.evaluations(),'analyzer_model':ANALYZER_MODEL}

    @app.post('/api/learning/cases/{identity}')
    async def review_case(identity:str,body:CaseReview):
        return service.learning.confirm_case(identity,**body.model_dump())

    @app.post('/api/learning/candidates/{identity}/evaluate')
    async def evaluate_candidate(identity:str):
        return service.learning.request_evaluation(identity)

    @app.post('/api/learning/candidates/{identity}/activate')
    async def activate_candidate(identity:str):
        return service.learning.activate(identity)

    @app.post('/api/learning/rollback')
    async def rollback():
        return service.learning.rollback()

    @app.get('/api/calls/{identity}')
    async def call(identity: str):
        return service.get_call(identity)

    @app.post('/api/calls/{identity}/stop')
    async def stop(identity: str):
        async with lock:
            await stop_runtime(identity)
        return service.get_call(identity)

    @app.post('/api/calls/{identity}/result')
    async def result(identity: str, body: Result):
        if body.metrics and any(x < 0 or x > 600000 for x in body.metrics.interruption_stop_ms):
            raise HTTPException(422,'Некорректные задержки.')
        return service.result(identity,body.metrics.model_dump() if body.metrics else None,
                              body.review.model_dump() if body.review else None)

    @app.post('/api/calls/{identity}/decisions/{decision_id}')
    async def decide(identity: str, decision_id: str, body: OwnerDecision):
        async with lock:
            decision=service.decide(identity,decision_id,body.answer)
            live=session.get('state',session)
            if session.get('call_id') == identity and live.get('owner_update'):
                await live['owner_update'](decision)
            return decision

    @app.post('/api/offer')
    async def offer(body: Offer):
        from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequest
        identity=body.requestData.call_id
        async with lock:
            if session or body.pc_id:
                raise HTTPException(409,'Браузер уже подключён. Сначала останови разговор.')
            brief=service.get_call(identity)['brief']
            if brief['channel']!='browser':raise ServiceError('browser_brief_required')
            key=model_key(brief)
            if not key:
                raise HTTPException(409,'Введи API-ключ выбранной модели в этом сервисе.')
            service.connecting(identity)
            session.update(call_id=identity,service=service,brief=brief,supervisor=supervisor)

            async def connected(connection):
                session['pc_id']=connection.pc_id

                async def run():
                    from voice import run_voice
                    reason='disconnected'
                    try:
                        await asyncio.wait_for(run_voice(connection,brief,key,session),timeout=brief['max_duration_secs'])
                        reason=session.get('end_reason',reason)
                    except asyncio.CancelledError:
                        reason=session.get('end_reason','disconnected')
                        raise
                    except TimeoutError:
                        reason='duration_limit'
                    except Exception as exc:
                        reason='model_error'
                        connection.send_app_message({'label':'rtvi-ai','type':'server-message',
                            'data':record_model_error(session,exception=exc)})
                    finally:
                        service.stop(identity,reason)
                        try:
                            with contextlib.suppress(Exception):
                                await connection.disconnect()
                        finally:
                            # Closing a peer can cancel this task via the transport's
                            # disconnected handler. Always release the call slot.
                            if session.get('call_id') == identity:
                                session.clear()

                session['task']=asyncio.create_task(run(),name='voice-task-'+identity)

            try:
                return await app.state.rtc.handle_web_request(SmallWebRTCRequest(sdp=body.sdp,type=body.type),connected)
            except Exception:
                await stop_runtime(identity,'connection_error')
                raise HTTPException(400,'Не удалось установить WebRTC-соединение.') from None

    @app.patch('/api/offer')
    async def patch(body: Patch):
        from pipecat.transports.smallwebrtc.request_handler import IceCandidate, SmallWebRTCPatchRequest
        if not session or body.pc_id != session.get('pc_id'):
            raise HTTPException(404,'Соединение не найдено.')
        await app.state.rtc.handle_patch_request(SmallWebRTCPatchRequest(pc_id=body.pc_id,
            candidates=[IceCandidate(**v.model_dump()) for v in body.candidates]))
        return {'ok':True}

    async def control_start(identity):
        if not browser_enabled:raise ServiceError('use_local_browser_rehearsal')
        async with lock:
            if service.get_brief(identity)['channel']!='browser':raise ServiceError('use_dial_call')
            return service.start(identity)

    async def control_stop(identity):
        async with lock:
            await stop_runtime(identity)
        return service.get_call(identity)

    async def control_capabilities():
        value=await config()
        value['calling']=await calling_status()
        value['calling']['recipient_policy']={channel:recipient_policy(channel) for channel in ('telegram','whatsapp','phone','sip')}
        value['calling'].update(daily_calls_remaining=outbound.daily_calls_remaining(),daily_call_units=outbound.daily_call_units(),
                                premium_call_units=PREMIUM_UNITS,call_cap=outbound.cap_status())
        value['calling']['credential_storage']='private_server_file' if persist else 'memory_only'
        for channel in ('telegram','whatsapp','phone','sip'):
            value['channels'][channel]=('configured_needs_pilot' if channel!='telegram' and value['calling']['twilio']['configured'] else 'not_connected' if channel!='telegram' else
                                       'experimental_ready' if value['calling']['telegram']['connected'] else 'not_connected')
        return value
    app.state.control_app=create_control(service,prepare,control_start,control_stop,control_capabilities,
                                         outbound=outbound if os.environ.get('VOICE_EXTERNAL_MCP')=='1' else None,
                                         browser_url=os.environ.get('VOICE_BROWSER_URL','http://127.0.0.1:13842/'))

    from readiness import calling_readiness
    app.state.agent_api=AgentAPI(service,AgentRegistry(agent_registry or os.environ.get('VOICE_AGENT_REGISTRY')),
        control_start,control_stop,outbound,
        readiness=lambda principal:calling_readiness(principal,outbound,credential_status(),browser_enabled,voice_provider()))

    @app.post('/v1/{operation}')
    async def agent_operation(operation:str,request:Request):
        return await app.state.agent_api.endpoint(operation,request)

    @app.post('/provider/twilio/status/{identity}')
    async def twilio_status(identity:str,request:Request):
        provider=outbound.providers.get('twilio');params=request.state.twilio_form
        if not provider or not provider.signature_valid(provider.config['public_url'].rstrip('/')+'/provider/twilio/status/'+identity,
                                                       params,request.headers.get('x-twilio-signature')):
            raise HTTPException(403,'Provider signature required')
        call=service.get_call(identity)
        if params.get('AccountSid')!=provider.config['account_sid'] or params.get('CallSid')!=call.get('provider_call_id'):
            raise HTTPException(403,'Attempt mismatch')
        status=params.get('CallStatus','')
        if status not in {'completed','busy','failed','no-answer','canceled'}:return {'ok':True}
        service.provider_update(identity,provider_status=status)
        if call['status'] in ACTIVE:await stop_runtime(identity,'provider_'+status)
        return {'ok':True}

    @app.post('/provider/twilio/inbound')
    async def twilio_inbound(request:Request):
        # Sender enablement needs a Voice endpoint. This outbound pilot never
        # answers an incoming call or invents a task for an unknown caller.
        provider=outbound.providers.get('twilio');params=request.state.twilio_form
        if not provider or params.get('AccountSid')!=provider.config.get('account_sid') or not provider.signature_valid(
            provider.config['public_url'].rstrip('/')+'/provider/twilio/inbound',params,request.headers.get('x-twilio-signature')):
            raise HTTPException(403,'Provider signature required')
        return Response('<Response><Reject reason="rejected"/></Response>',media_type='application/xml')

    @app.websocket('/provider/twilio/media/{identity}')
    async def twilio_media(socket:WebSocket,identity:str):
        provider=outbound.providers.get('twilio')
        base=provider.config['public_url'].rstrip('/')+'/provider/twilio/media/'+identity if provider else ''
        urls=[base,base+'/',base.replace('https://','wss://',1),base.replace('https://','wss://',1)+'/']
        if not provider or socket.url.hostname!=urlsplit(base).hostname or socket.url.query or not any(
            provider.signature_valid(url,{},socket.headers.get('x-twilio-signature')) for url in urls):
            await socket.close(code=1008);return
        await socket.accept()
        reason='media_disconnected';joined=retired=False;state=timer=None
        loop=asyncio.get_running_loop()
        def remaining():return max(0,session.get('deadline',0)-loop.time())
        try:
            for _ in range(2):
                raw=await asyncio.wait_for(socket.receive_text(),5)
                if len(raw)>8192:await socket.close(code=1009);return
                value=json.loads(raw)
                if value.get('event')=='start':break
            start=value.get('start',{})
            sid=start.get('callSid','');token=start.get('customParameters',{}).get('attempt_token')
            if value.get('event')!='start' or not re.fullmatch(r'MZ[0-9a-fA-F]{32}',start.get('streamSid','')) or start.get('mediaFormat')!={'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1} or start.get('accountSid')!=provider.config['account_sid'] or not outbound.valid_stream(identity,token,sid):
                await socket.close(code=1008);return
            async with lock:
                previous=None
                if session:
                    # Only the stream of a pending out-of-band handover may join a running call (ANYM-451).
                    generation=service.activate_generation(identity) if session.get('call_id')==identity and session.get('websocket') else None
                    if not generation:await socket.close(code=1008);return
                    previous=session.get('state')
                call=service.get_call(identity)
                if previous is None:
                    generation=call.get('generation',1)
                    session.update(call_id=identity,service=service,brief=call['brief'],supervisor=supervisor,websocket=True,
                                   deadline=loop.time()+call['brief']['max_duration_secs'])
                state={'call_id':identity,'service':service,'brief':call['brief'],'supervisor':supervisor,'websocket':True,
                       'generation':generation,'resume':previous is not None,'started_at':now(),'resumed':asyncio.Event()}
                session.update(state=state,task=asyncio.current_task());joined=True
            if previous is not None:
                # The old session lost its authority on activation; its audio stops too, and its own
                # cleanup sees it was replaced.
                previous['resumed'].set()
                if previous.get('runner'):previous['retiring']=asyncio.create_task(previous['runner'].cancel())
            async def keep_stream():
                await asyncio.sleep(HANDOVER_SECS)
                service.cancel_handover(identity,generation)
            async def out_of_band(keys):
                nonlocal timer
                outcome=await outbound.send_digits(identity,keys)
                # A kept stream may press again later: each reservation gets its own window.
                if outcome!='rejected' and (timer is None or timer.done()):timer=asyncio.create_task(keep_stream())
                return outcome
            state['out_of_band']=out_of_band
            from pipecat.serializers.twilio import TwilioFrameSerializer
            from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport,FastAPIWebsocketParams
            from voice import KeypadTwilioSerializer,run_transport
            serializer=KeypadTwilioSerializer(stream_sid=start['streamSid'],params=TwilioFrameSerializer.InputParams(auto_hang_up=False))
            # Activation of a newer stream revokes this one's output at once, before its runner is retired.
            serializer.authority=lambda:session.get('state') is state
            transport=FastAPIWebsocketTransport(websocket=socket,params=FastAPIWebsocketParams(
                audio_in_enabled=True,audio_out_enabled=True,audio_out_sample_rate=8000,
                audio_out_end_silence_secs=0,serializer=serializer))
            key=model_key(call['brief'])
            if not key:raise ServiceError('model_key_required')
            # One deadline for the whole call: a reconnected stream gets only the time left.
            await asyncio.wait_for(run_transport(transport,call['brief'],key,state),remaining())
            reason=state.get('end_reason',reason)
            if timer:timer.cancel();timer=None
            if session.get('state') is not state:retired=True
            elif service.handover_pending(identity,generation):
                # The provider replaced this stream to send the key: wait for the reconnect, not longer.
                try:
                    await asyncio.wait_for(state['resumed'].wait(),min(HANDOVER_SECS,remaining()))
                    retired=True
                except TimeoutError:
                    reason='duration_limit' if remaining()<=0 else 'keypress_handover_failed'
                    service.cancel_handover(identity,generation,'timed_out')
        except asyncio.CancelledError:
            reason=(state or {}).get('end_reason','owner_stop');raise
        except TimeoutError:reason='duration_limit'
        except Exception:reason='media_error'
        finally:
            if timer:timer.cancel()
            if joined and not retired and session.get('state') is state:
                await outbound.stop(identity,reason)
                if session.get('call_id')==identity:session.clear()
            with contextlib.suppress(Exception):await socket.close()
    app.mount('/control',app.state.control_app)

    frontend=Path(__file__).parent/'frontend'/'dist'
    if frontend.exists():
        app.mount('/',StaticFiles(directory=frontend,html=True),name='frontend')
    return app


def server(app):
    """The supported entry point: proxy headers never rewrite the transport peer that VoiceBoundary admits."""
    import uvicorn
    return uvicorn.Server(uvicorn.Config(app,host=os.environ.get('VOICE_BIND','127.0.0.1'),port=int(os.environ.get('VOICE_PORT','13842')),
                                         access_log=False,proxy_headers=False))


if __name__ == '__main__':
    logger.remove()
    logger.add(sys.stderr,level='ERROR',format='{time:HH:mm:ss} | voice pipeline error')
    server(create_app()).run()
