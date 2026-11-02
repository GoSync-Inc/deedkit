import asyncio
import json

import httpx
import pytest

from core import VoiceService, ServiceError
from outbound import ExternalBrief, CallingSettings, Outbound, TwilioCalls
from test_tasks import BRIEF


def brief(channel='phone', destination='+628123456789'):
    return ExternalBrief(**BRIEF, channel=channel, account='twilio', destination=destination)


async def test_preparation_is_not_dialing_and_snapshot_has_real_role(tmp_path):
    service=VoiceService(tmp_path)
    saved=service.prepare(brief(),[])
    assert saved['channel']=='phone' and saved['destination']=='+628123456789'
    assert 'BROWSER REHEARSAL' not in saved['prompt']
    assert 'real outbound' in saved['prompt']
    assert service.list_calls()==[]
    service.close()


async def test_dial_requires_explicit_route_recipient_key_and_reference(tmp_path):
    service=VoiceService(tmp_path); saved=service.prepare(brief(),[])
    manager=Outbound(service,lambda:None,CallingSettings(),{})
    with pytest.raises(ServiceError,match='calling_disabled'):
        await manager.dial(saved['id'],'Owner asked in this conversation')
    assert service.list_calls()==[]
    manager.settings=CallingSettings(enabled=True)
    # ANYM-288: only SIP keeps a recipient list; an unlisted phone number reaches the key check.
    sip=service.prepare(brief('sip','sip:alice@pbx.example.test'),[])
    with pytest.raises(ServiceError,match='recipient_not_allowed'):
        await manager.dial(sip['id'],'Owner asked in this conversation')
    with pytest.raises(ServiceError,match='model_key_required'):
        await manager.dial(saved['id'],'Owner asked in this conversation')
    assert service.list_calls()==[]
    service.close()


async def test_dial_one_attempt_even_concurrent_repeated_and_unknown(tmp_path):
    service=VoiceService(tmp_path); saved=service.prepare(brief(),[]); seen=[]
    class Provider:
        async def preflight(self,b): return {'configured':True}
        async def dial(self,call):
            seen.append(call['id']); await asyncio.sleep(.01)
            raise httpx.ReadTimeout('May have reached provider; secret-data')
    settings=CallingSettings(enabled=True,allowed={'phone':['+628123456789']})
    manager=Outbound(service,lambda:'fake',settings,{'twilio':Provider()})
    a,b=await asyncio.gather(manager.dial(saved['id'],'Exact owner instruction'),manager.dial(saved['id'],'Exact owner instruction'))
    assert a['id']==b['id'] and len(seen)==1
    assert service.get_call(a['id'])['dial_state']=='unknown'
    assert 'secret-data' not in json.dumps(service.get_call(a['id']))
    service.close()
    # A restart must not turn an ambiguous request into another paid call.
    service=VoiceService(tmp_path)
    manager=Outbound(service,lambda:'fake',settings,{'twilio':Provider()})
    await manager.dial(saved['id'],'Exact owner instruction')
    assert len(seen)==1
    service.close()


async def test_twilio_channels_signed_stream_and_no_cross_channel_fallback():
    requests=[]
    async def receive(req):
        requests.append(req)
        return httpx.Response(201,json={'sid':'CA'+'1'*32,'status':'queued'})
    client=httpx.AsyncClient(transport=httpx.MockTransport(receive))
    config={'account_sid':'AC'+'2'*32,'auth_token':'fixture-token',
            'phone_from':'+12025550123','whatsapp_from':'+628123456789','public_url':'https://voice.example.test'}
    provider=TwilioCalls(config,http=client)
    for channel,dest in [('whatsapp','+628111111111'),('phone','+12025550125'),('sip','sip:alice@pbx.example.test')]:
        call={'id':'call-1','brief':{'channel':channel,'destination':dest,'max_duration_secs':60},'stream_token':'fixture-stream-token'}
        await provider.dial(call)
    from urllib.parse import parse_qs
    payloads=[parse_qs(r.content.decode()) for r in requests]
    assert payloads[0]['To']==['whatsapp:+628111111111']
    assert payloads[0]['From']==['whatsapp:+628123456789']
    assert payloads[1]['To']==['+12025550125']
    assert payloads[2]['To']==['sip:alice@pbx.example.test']
    assert 'wss://voice.example.test/provider/twilio/media/call-1' in payloads[0]['Twiml'][0]
    assert 'fixture-stream-token' in payloads[0]['Twiml'][0]
    assert 'fixture-token' not in payloads[0]['Twiml'][0]
    assert payloads[0]['TimeLimit']==['60']
    await client.aclose()


CANARY ='PROVIDER_TEXT_CANARY'
LEAKY_NUMBER = '+442071838750'


def twilio_rejecting(status, body, content=None):
    seen = []

    async def receive(request):
        seen.append(request)
        return httpx.Response(status, json=body) if content is None else httpx.Response(status, content=content)
    http = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    config = {'account_sid': 'AC' + '2' * 32, 'auth_token': 'fixture-token', 'phone_from': '+12025550123',
              'public_url': 'https://voice.example.test'}
    return TwilioCalls(config, http=http), seen


async def dial_with(tmp_path, provider):
    service = VoiceService(tmp_path)
    saved = service.prepare(brief(), [])
    settings = CallingSettings(enabled=True, allowed={'phone': ['+628123456789']})
    manager = Outbound(service, lambda: 'fake', settings, {'twilio': provider})
    call = await manager.dial(saved['id'], 'Exact owner instruction')
    return service, service.get_call(call['id'])


# spec: ANYM-657
@pytest.mark.parametrize('code,cause', [
    (21215, 'geo_permission_missing'), (13227, 'geo_permission_missing'), (21211, 'invalid_destination'),
    (13224, 'invalid_destination'), (21214, 'destination_not_reachable'), (21217, 'destination_not_dialable'),
    (21219, 'destination_unverified'), (21210, 'source_number_invalid'), (21212, 'source_number_invalid'),
    (21213, 'source_number_invalid'), (21216, 'destination_blocked'), (20429, 'rate_limited'),
    (20003, 'provider_auth_failed'), (20404, 'provider_auth_failed'), (99999, 'provider_rejected_other'),
    (None, 'provider_rejected_other'), ('21215', 'provider_rejected_other'), (1234567, 'provider_rejected_other'),
])
async def test_a_twilio_4xx_stores_the_code_and_a_fixed_cause_and_no_provider_text(tmp_path, code, cause):
    body = {'message': CANARY + ' to ' + LEAKY_NUMBER, 'more_info': 'https://www.twilio.com/docs/errors/' + CANARY,
            'status': 400, **({'code': code} if code is not None else {})}
    provider, seen = twilio_rejecting(400, body)
    service, call = await dial_with(tmp_path, provider)
    assert len(seen) == 1
    stored = {'http_status': 400, 'code': code if type(code) is int and code < 10**6 else None, 'cause': cause}
    assert call['provider_error'] == stored
    assert call['provider_status'] == 'provider_rejected' and call['dial_state'] == 'rejected'
    assert call['end_reason'] == 'provider_rejected'
    everything = json.dumps([call, service.get_call(call['id']), service.list_calls()])
    assert CANARY not in everything and LEAKY_NUMBER not in everything and 'twilio.com/docs' not in everything
    service.close()


# spec: ANYM-657
@pytest.mark.parametrize('content', [b'<html>' + b'PROVIDER_TEXT_CANARY' * 10 + b'</html>',
    b'{"code": 21215, "message": "' + b'x' * 70000 + b'"}', b'[21215]', b'{"code": true}'])
async def test_an_unreadable_or_oversized_4xx_body_gives_no_code(tmp_path, content):
    provider, _ = twilio_rejecting(403, None, content=content)
    service, call = await dial_with(tmp_path, provider)
    assert call['provider_error'] == {'http_status': 403, 'code': None, 'cause': 'provider_rejected_other'}
    assert CANARY not in json.dumps(call)
    service.close()


# spec: ANYM-657
@pytest.mark.parametrize('status', [500, 503])
async def test_a_5xx_keeps_the_ambiguous_path_with_no_provider_error_and_no_redial(tmp_path, status):
    provider, seen = twilio_rejecting(status, {'code': 21215, 'message': CANARY})
    service, call = await dial_with(tmp_path, provider)
    assert call['dial_state'] == 'unknown' and 'provider_error' not in call
    assert call['provider_status'] == 'reconciliation_required'
    assert len(seen) == 1
    assert CANARY not in json.dumps(call)
    service.close()


# spec: ANYM-657
async def test_a_network_error_keeps_the_ambiguous_path_with_no_provider_error(tmp_path):
    async def broken(request):
        raise httpx.ConnectError('socket closed ' + CANARY)
    config = {'account_sid': 'AC' + '2' * 32, 'auth_token': 'fixture-token', 'phone_from': '+12025550123',
              'public_url': 'https://voice.example.test'}
    provider = TwilioCalls(config, http=httpx.AsyncClient(transport=httpx.MockTransport(broken)))
    service, call = await dial_with(tmp_path, provider)
    assert call['dial_state'] == 'unknown' and 'provider_error' not in call
    assert CANARY not in json.dumps(call)
    service.close()


def test_destination_validation_no_credentials_or_uri_injection():
    from pydantic import ValidationError
    for channel,dest in [('phone','sip:a@x'),('telegram','-100123'),('sip','sip:alice:password@pbx.test'),('sip','sip:a@pbx.test?method=INVITE')]:
        with pytest.raises(ValidationError): brief(channel,dest)


async def test_restart_reconciles_known_call_without_redial(tmp_path):
    service=VoiceService(tmp_path);saved=service.prepare(brief(),[]);call=service.start(saved['id'])
    service.provider_update(call['id'],dial_state='accepted',provider_call_id='CA'+'3'*32)
    service.close();service=VoiceService(tmp_path);seen=[]
    class Provider:
        async def status(self,sid):seen.append(('status',sid));return {'provider_status':'in-progress'}
        async def hangup(self,sid):seen.append(('hangup',sid))
    manager=Outbound(service,lambda:'fixture',CallingSettings(),{'twilio':Provider()})
    await manager.recover()
    assert [operation for operation,sid in seen]==['status','hangup']
    assert service.get_call(call['id'])['provider_status']=='completed_on_recovery'
    assert service.get_call(call['id'])['status']=='interrupted'
    service.close()


async def test_media_before_receipt_binds_attempt_and_timeout_closes_it(tmp_path):
    service=VoiceService(tmp_path);saved=service.prepare(brief(),[]);seen=[]
    sid='CA'+'4'*32
    class Provider:
        async def preflight(self,b):return {'configured':True}
        async def dial(self,call):
            assert manager.valid_stream(call['id'],call['stream_token'],sid)
            assert service.get_call(call['id'])['provider_call_id']==sid
            raise httpx.ReadTimeout('receipt lost after media connected')
        async def hangup(self,value):seen.append(value)
    manager=Outbound(service,lambda:'fixture',CallingSettings(enabled=True,allowed={'phone':['+628123456789']}),{'twilio':Provider()})
    result=await manager.dial(saved['id'],'Exact owner instruction')
    assert result['dial_state']=='unknown' and result['status'] not in {'dialing','active'}
    assert seen==[sid]
    assert not manager.valid_stream(result['id'],'wrong',sid)
    repeated=await manager.dial(saved['id'],'Exact owner instruction')
    assert repeated['id']==result['id'] and seen==[sid]
    service.close()
