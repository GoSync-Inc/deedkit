"""Capabilities diagnose configuration without contacting providers or exposing other principals."""
import json
import pytest

from fastapi.testclient import TestClient
from app import create_app
from test_agent_api import registry
from test_tasks import Archive, HEADERS, owner_headers
import voice_engines


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


def engines_app(tmp_path,keys,scopes_without=(),enabled=True):
    tmp_path.mkdir(exist_ok=True)
    path,doc,save=registry(tmp_path)
    for principal in doc['principals']:
        principal['channels'].append('sip');principal['destinations']={'sip':['sip:alice@pbx.example.test']}
        for scope in scopes_without:principal['scopes'].remove(scope)
    save()
    app=create_app(tmp_path/'data',Archive(),agent_registry=path)
    client=TestClient(app);client.__enter__()
    client.headers.update(owner_headers(tmp_path/'data'))
    if keys:client.post('/api/credentials',json=keys,headers=HEADERS)
    assert client.post('/api/call-voice',json={'provider':'gemini_live'},headers=HEADERS).status_code==200
    setup={'enabled':enabled,'allowed':{'sip':['sip:alice@pbx.example.test']},'twilio':{
        'account_sid':'AC'+'1'*32,'auth_token':'synthetic-token','phone_from':'+12025550125',
        'public_url':'https://voice.example.test'}}
    assert client.post('/api/calling',json=setup,headers=HEADERS).status_code==200
    ready=client.post('/v1/capabilities',json={},headers={'Authorization':'Bearer alice'}).json()['calling_readiness']
    assert app.state.service.list_calls()==[]
    client.__exit__(None,None,None)
    return ready


# spec: ANYM-595
def test_readiness_judges_each_voice_engine_by_its_own_key(tmp_path):
    ready=engines_app(tmp_path/'xai',{'xai':'synthetic-xai-secret'})
    assert set(ready['channels'])=={'browser','phone','sip'}
    for name,channel in ready['channels'].items():
        assert list(channel['voice_engines'])==list(voice_engines.ENGINES), name
        assert channel['voice_engines']['grok_live']=={'ready':True,'blockers':[]}, name
        assert channel['voice_engines']['gemini_live']=={'ready':False,'blockers':['key_not_configured']}, name
        assert channel['ready_engines']==['grok_live'], name
        assert channel['auto']=={'ready':True,'selects':'grok_live'}, name
        # An omitted voice_engine still uses the owner's Gemini: today's aggregate is unchanged.
        assert channel['configuration_ready'] is False and channel['blockers']==['model_key_required'], name
    both=engines_app(tmp_path/'both',{'xai':'synthetic-xai-secret','gemini':'synthetic-gemini-secret'})
    for name,channel in both['channels'].items():
        assert channel['ready_engines']==['gemini_live','grok_live'], name
        assert channel['auto']=={'ready':True,'selects':'gemini_live'}, name
        assert channel['configuration_ready'] is True, name
    none=engines_app(tmp_path/'none',{})
    for name,channel in none['channels'].items():
        assert channel['ready_engines']==[] and channel['auto']=={'ready':False,'selects':None}, name
    serialized=json.dumps([ready,both,none])
    assert 'synthetic' not in serialized and 'secret' not in serialized


# spec: ANYM-595
@pytest.mark.parametrize('scopes_without,enabled,code',[(('calls:dial',),True,'calls:dial_required'),((),False,'calling_disabled')])
def test_dial_scope_or_disabled_calling_blocks_every_engine(tmp_path,scopes_without,enabled,code):
    ready=engines_app(tmp_path,{'xai':'synthetic-xai-secret','gemini':'synthetic-gemini-secret'},scopes_without,enabled)
    for name in ('phone','sip'):
        channel=ready['channels'][name]
        for engine in voice_engines.ENGINES:
            assert channel['voice_engines'][engine]['ready'] is False and code in channel['voice_engines'][engine]['blockers']
        assert channel['ready_engines']==[] and channel['auto']=={'ready':False,'selects':'gemini_live'}
    assert ready['channels']['browser']['ready_engines']==['gemini_live','grok_live']


# spec: ANYM-595
def test_unreadable_call_voice_still_judges_explicit_engines_by_their_keys():
    from types import SimpleNamespace
    from readiness import calling_readiness
    principal=SimpleNamespace(channels=['browser','telegram'],scopes={'calls:start','calls:dial'},destinations={})
    outbound=SimpleNamespace(settings=SimpleNamespace(enabled=True,allowed={}),providers={'telegram':object()},
                             daily_calls_remaining=lambda:3)
    ready=calling_readiness(principal,outbound,{'gemini':False,'xai':True},True,None)
    for name,channel in ready['channels'].items():
        assert channel['blockers']==['call_voice_invalid'], name
        # Prepare freezes the owner's voice into every brief, so it refuses each engine alike (ANYM-595 fix).
        assert channel['voice_engines']=={
            'gemini_live':{'ready':False,'blockers':['call_voice_invalid','key_not_configured']},
            'grok_live':{'ready':False,'blockers':['call_voice_invalid']}}, name
        assert channel['ready_engines']==[] and channel['auto']=={'ready':False,'selects':'grok_live'}, name


# spec: ANYM-595
@pytest.mark.parametrize('provider',['gemini_live','grok_realtime',None])
@pytest.mark.parametrize('keys',[{'gemini':True,'xai':True},{'gemini':True,'xai':False},{'gemini':False,'xai':True},{'gemini':False,'xai':False}])
def test_auto_selects_what_choose_picks(provider,keys):
    from types import SimpleNamespace
    from core import ServiceError
    from readiness import calling_readiness
    principal=SimpleNamespace(channels=['browser'],scopes={'calls:start'},destinations={})
    ready=calling_readiness(principal,None,keys,True,provider)
    try:selected=voice_engines.choose('auto','ru',provider,keys)['selected']
    except ServiceError as error:
        assert error.code=='no_voice_engine_available';selected=None
    # An unreadable call_voice (provider None) still names choose's pick, but no engine can be prepared (ANYM-595 fix).
    assert ready['channels']['browser']['auto']=={'ready':selected is not None and provider is not None,'selects':selected}


# spec: ANYM-595
def test_capabilities_description_names_ready_engines():
    from agent_api import DESCRIPTIONS
    assert 'ready_engines' in DESCRIPTIONS['capabilities']
