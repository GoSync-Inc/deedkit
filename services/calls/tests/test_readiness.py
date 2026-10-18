"""Capabilities diagnose configuration without contacting providers or exposing other principals."""
import json
import pytest

from fastapi.testclient import TestClient
from app import create_app
from test_agent_api import registry
from test_tasks import Archive, HEADERS, owner_headers


def test_capabilities_report_scoped_blockers_without_provider_io(tmp_path):
    path,doc,save=registry(tmp_path)
    doc['principals'][0]['scopes'].remove('calls:dial');save()
    app=create_app(tmp_path/'data',Archive(),agent_registry=path)
    class Provider:
        async def preflight(self,*args):raise AssertionError('no provider requests')
        async def probe(self,*args):raise AssertionError('no probes')
    app.state.outbound.providers['twilio']=Provider()
    # Direct ASGI requests, no lifespan: readiness itself must not run recovery.
    import asyncio
    result=asyncio.run(app.state.agent_api.call('alice','capabilities',{}))
    ready=result['calling_readiness']
    assert set(ready['channels'])=={'browser','phone'}
    # No recipient list for phone since ANYM-288; only SIP can report recipient_not_allowed.
    assert ready['channels']['phone']['blockers']==[
        'calls:dial_required','calling_disabled','model_key_required']
    assert ready['channels']['phone']['recipient_policy']=='any_valid_destination'
    assert ready['channels']['phone']['daily_calls_remaining']==10
    assert ready['channels']['phone']['provider_connection']=='not_probed'
    assert ready['channels']['phone']['live_call_verified'] is False
    assert app.state.service.list_calls()==[]
    app.state.service.close()


def test_ready_twilio_configuration_requires_scoped_recipient_and_selected_model_key(tmp_path):
    path,doc,save=registry(tmp_path)
    # Since ANYM-288 only SIP keeps scoped recipients; phone needs no list.
    for principal,uri in zip(doc['principals'],['sip:alice@pbx.example.test','sip:bob@pbx.example.test']):
        principal['channels'].append('sip');principal['destinations']={'sip':[uri]}
    save()
    app=create_app(tmp_path/'data',Archive(),agent_registry=path)
    with TestClient(app) as client:
        client.headers.update(owner_headers(tmp_path/'data'))
        client.post('/api/credentials',json={'xai':'synthetic-xai-key'},headers=HEADERS)
        # ANYM-359: the selected model key is the owner's call voice provider.
        client.post('/api/call-voice',json={'provider':'grok_realtime'},headers=HEADERS)
        setup={'enabled':True,'allowed':{'sip':['sip:alice@pbx.example.test']},'twilio':{
            'account_sid':'AC'+'1'*32,'auth_token':'synthetic-token','phone_from':'+12025550125',
            'public_url':'https://voice.example.test'}}
        assert client.post('/api/calling',json=setup,headers=HEADERS).status_code==200
        alice=client.post('/v1/capabilities',json={},headers={'Authorization':'Bearer alice'}).json()['calling_readiness']
        bob=client.post('/v1/capabilities',json={},headers={'Authorization':'Bearer bob'}).json()['calling_readiness']
        assert alice['channels']['phone']['configuration_ready'] and bob['channels']['phone']['configuration_ready']
        assert 'allowed_recipient_count' not in alice['channels']['phone']
        assert alice['channels']['sip']['configuration_ready']
        assert alice['channels']['sip']['allowed_recipient_count']==1
        assert bob['channels']['sip']['blockers']==['recipient_not_allowed']
        assert 'sip:alice@pbx.example.test' not in json.dumps(bob)
        assert 'synthetic' not in json.dumps(alice)
        assert alice['external_action_authorized'] is False
        assert app.state.service.list_calls()==[]


@pytest.mark.parametrize('credential,available,unavailable',[
    ('xai','grok_realtime','gemini_live'),('gemini','gemini_live','grok_realtime')])
def test_twilio_preflight_requires_the_briefs_own_model_credential(tmp_path,credential,available,unavailable):
    from test_tasks import BRIEF
    path,_,_=registry(tmp_path)
    app=create_app(tmp_path/'data',Archive(),agent_registry=path)
    with TestClient(app) as client:
        client.headers.update(owner_headers(tmp_path/'data'))
        client.post('/api/credentials',json={credential:'synthetic-model-key'},headers=HEADERS)
        setup={'enabled':True,'allowed':{'phone':['+12025550123']},'twilio':{
            'account_sid':'AC'+'1'*32,'auth_token':'synthetic-token','phone_from':'+12025550125',
            'public_url':'https://voice.example.test'}}
        client.post('/api/calling',json=setup,headers=HEADERS)
        auth={'Authorization':'Bearer alice'}
        for engine,expected in [(unavailable,'model_key_required'),(available,None)]:
            # ANYM-359: the engine is the owner's call voice, never an agent field.
            client.post('/api/call-voice',json={'provider':engine},headers=HEADERS)
            body={**BRIEF,'source_message_ids':[],'channel':'phone',
                  'account':'twilio','destination':'+12025550123'}
            brief=client.post('/v1/prepare_external_call',json={'brief':body},headers=auth)
            assert brief.status_code==200
            response=client.post('/v1/preflight_call',json={'brief_id':brief.json()['id']},headers=auth)
            if expected:
                assert response.status_code==409 and response.json()['error']==expected
            else:
                assert response.status_code==200 and response.json()['configured']
        assert app.state.service.list_calls()==[]
