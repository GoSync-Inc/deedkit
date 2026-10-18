"""One owner-set call voice for every agent call (ANYM-359). Synthetic only; no provider I/O."""
import json
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_api import METHODS
from app import create_app
from core import GROK_MODEL, MODEL
from test_agent_api import registry
from test_tasks import Archive, BRIEF, HEADERS, owner_headers

AGENT = {'Authorization':'Bearer alice'}
VOICE_FIELDS = ('dialog_engine','voice','grok_voice','custom_voice_id','voice_style')
ROUTES = {
    'browser':None,
    'phone':{'channel':'phone','account':'twilio','destination':'+12025550123'},
    'sip':{'channel':'sip','account':'twilio','destination':'sip:alice@pbx.example.test'},
    'whatsapp':{'channel':'whatsapp','account':'twilio','destination':'+12025550124'},
    'telegram':{'channel':'telegram','account':'telegram','destination':'123456'},
}
GROK = {'provider':'grok_realtime','grok_voice':'eve'}


def property_names(schema):
    if isinstance(schema,dict):
        own=set(schema.get('properties',{})) if isinstance(schema.get('properties'),dict) else set()
        return own.union(*(property_names(value) for value in schema.values()))
    if isinstance(schema,list):
        return set().union(*(property_names(value) for value in schema))
    return set()


def every_channel(tmp_path):
    path,doc,save=registry(tmp_path)
    for principal in doc['principals']:
        principal['channels']=list(ROUTES)
        principal['destinations']={'sip':['sip:alice@pbx.example.test']}
    save()
    return path


def agent_prepare(client,channel):
    body={**BRIEF,'source_message_ids':[]}
    if ROUTES[channel] is None:
        return client.post('/v1/prepare_call',json={'brief':body},headers=AGENT)
    return client.post('/v1/prepare_external_call',json={'brief':{**body,**ROUTES[channel]}},headers=AGENT)


def set_voice(client,value):
    return client.post('/api/call-voice',json=value,headers=HEADERS)


# spec: ANYM-359
def test_owner_sets_call_voice_privately_and_it_survives_restart(tmp_path):
    data=tmp_path/'data'
    with TestClient(create_app(data,Archive())) as client:
        assert client.get('/api/call-voice').status_code==401
        assert client.post('/api/call-voice',json=GROK,headers=HEADERS).status_code==401
        assert client.post('/v1/call-voice',json=GROK,headers=AGENT).status_code in {401,403,404}
        client.headers.update(owner_headers(data))
        unset=client.get('/api/call-voice').json()
        assert unset=={'provider':'gemini_live','gemini_voice':'Puck','grok_voice':'rex',
                       'gemini_custom_voice_id':None,'voice_style':'neutral'}
        saved=set_voice(client,GROK)
        assert saved.status_code==200 and saved.json()['provider']=='grok_realtime'
        assert (data/'call-voice.json').stat().st_mode&0o077==0
        replicated={'provider':'gemini_live','gemini_custom_voice_id':'voice_owner_fixture','voice_style':'friendly'}
        assert set_voice(client,replicated).json()['gemini_custom_voice_id']=='voice_owner_fixture'
        setup={'enabled':True,'allowed':{},'twilio':{'account_sid':'AC'+'1'*32,'auth_token':'synthetic-token',
               'phone_from':'+12025550125','public_url':'https://voice.example.test'}}
        assert client.post('/api/calling',json=setup,headers=HEADERS).status_code==200
        assert client.get('/api/call-voice').json()['gemini_custom_voice_id']=='voice_owner_fixture'
    with TestClient(create_app(data,Archive())) as client:
        client.headers.update(owner_headers(data))
        assert client.get('/api/call-voice').json()=={'provider':'gemini_live','gemini_voice':'Puck','grok_voice':'rex',
            'gemini_custom_voice_id':'voice_owner_fixture','voice_style':'friendly'}


# spec: ANYM-359
def test_agent_brief_freezes_owner_call_voice_on_every_channel(tmp_path):
    data=tmp_path/'data'
    with TestClient(create_app(data,Archive(),agent_registry=every_channel(tmp_path))) as client:
        client.headers.update(owner_headers(data))
        default=agent_prepare(client,'whatsapp').json()
        assert default['dialog_engine']=='gemini_live' and default['model']==MODEL and default['voice']=='Puck'
        assert set_voice(client,GROK).status_code==200
        briefs={}
        for channel in ROUTES:
            response=agent_prepare(client,channel)
            assert response.status_code==200, (channel,response.text)
            brief=response.json()
            assert brief['channel']==channel and brief['dialog_engine']=='grok_realtime'
            assert brief['model']==GROK_MODEL and brief['grok_voice']=='eve' and brief['custom_voice_id'] is None
            briefs[channel]=brief
        assert set_voice(client,{'provider':'gemini_live','gemini_voice':'Kore'}).status_code==200
        for channel,brief in briefs.items():
            saved=client.get(f'/api/briefs/{brief["id"]}').json()
            assert saved['snapshot_sha256']==brief['snapshot_sha256'] and saved['grok_voice']=='eve'
    with TestClient(create_app(data,Archive(),agent_registry=every_channel(tmp_path))) as client:
        client.headers.update(owner_headers(data))
        for channel,brief in briefs.items():
            saved=client.get(f'/api/briefs/{brief["id"]}').json()
            assert saved['snapshot_sha256']==brief['snapshot_sha256'] and saved['dialog_engine']=='grok_realtime'
        assert agent_prepare(client,'telegram').json()['voice']=='Kore'


# spec: ANYM-359
def test_followup_keeps_parent_call_voice_after_owner_switches(tmp_path):
    data=tmp_path/'data'
    with TestClient(create_app(data,Archive(),agent_registry=every_channel(tmp_path))) as client:
        client.headers.update(owner_headers(data))
        set_voice(client,GROK)
        body={**BRIEF,'source_message_ids':[],'approval_fields':['delivery_window'],**ROUTES['telegram']}
        parent=client.post('/v1/prepare_external_call',json={'brief':body},headers=AGENT).json()
        set_voice(client,{'provider':'gemini_live','gemini_voice':'Kore'})
        service=client.app.state.service
        call=service.start(parent['id']);cid=call['id'];service.connected(cid)
        service.transcript(cid,'user','12–13')
        decision=service.tool(cid,'record_answer',{'field':'delivery_window','value':'12–13','evidence':'12–13'},'t1')['owner_decision']
        service.stop(cid,'awaiting_owner');service.decide(cid,decision['decision_id'],'No')
        child=client.post('/v1/prepare_followup',json={'call_id':cid,'decision_id':decision['decision_id']},headers=AGENT).json()
        assert child['dialog_engine']=='grok_realtime' and child['grok_voice']=='eve' and child['model']==GROK_MODEL
        assert child['channel']=='telegram' and child['followup']['owner_answer']=='No'


# spec: ANYM-359
@pytest.mark.parametrize('field,value',[
    ('dialog_engine','gemini_live'),('dialog_engine','grok_realtime'),('dialog_engine',None),
    ('voice','Puck'),('voice',None),('grok_voice','rex'),('grok_voice',None),
    ('custom_voice_id','voice_owner_fixture'),('custom_voice_id',None),('voice_style','neutral'),('voice_style',None)])
def test_agent_cannot_choose_or_change_call_voice(tmp_path,field,value):
    data=tmp_path/'data'
    with TestClient(create_app(data,Archive(),agent_registry=every_channel(tmp_path))) as client:
        for channel in ('browser','phone','telegram'):
            body={**BRIEF,'source_message_ids':[],**(ROUTES[channel] or {}),field:value}
            operation='prepare_call' if channel=='browser' else 'prepare_external_call'
            response=client.post(f'/v1/{operation}',json={'brief':body},headers=AGENT)
            assert response.status_code==422 and response.json()=={'error':'invalid_arguments'}
        assert client.app.state.service.db.execute('SELECT count(*) FROM briefs').fetchone()[0]==0
    assert {'goal','destination'}<=property_names(METHODS['prepare_external_call'][0].model_json_schema())
    for name,(model,_) in METHODS.items():
        assert 'voice' not in name
        assert not set(VOICE_FIELDS)&property_names(model.model_json_schema()), name
    catalog=json.loads((Path(__file__).resolve().parents[3]/'services/gateway/src/agent_gateway/catalog.json').read_text())
    for row in catalog:
        if row['module']=='calls':
            assert not set(VOICE_FIELDS)&property_names(row['inputSchema']), row['name']


# spec: ANYM-359
async def test_control_mcp_freezes_call_voice_and_refuses_voice_fields(tmp_path,monkeypatch):
    monkeypatch.setenv('VOICE_EXTERNAL_MCP','1')
    app=create_app(tmp_path,Archive(),control_token='control-fixture-token')
    app.state.service.set_call_voice(app.state.service.call_voice().model_copy(update={'provider':'grok_realtime'}))
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app),headers={'Authorization':'Bearer control-fixture-token'}) as http:
            async with streamable_http_client('http://testserver/control/mcp',http_client=http) as (read,write):
                async with ClientSession(read,write) as client:
                    await client.initialize()
                    for tool,extra in (('prepare_call',{}),('prepare_external_call',ROUTES['whatsapp'])):
                        for field in VOICE_FIELDS:
                            refused=await client.call_tool(tool,{'brief':{**BRIEF,**extra,field:None}})
                            assert refused.is_error, (tool,field)
                        assert app.state.service.db.execute('SELECT count(*) FROM briefs').fetchone()[0]==0
                    for tool,extra in (('prepare_call',{}),('prepare_external_call',ROUTES['whatsapp'])):
                        prepared=await client.call_tool(tool,{'brief':{**BRIEF,**extra}})
                        assert not prepared.is_error, prepared
                        assert prepared.structured_content['dialog_engine']=='grok_realtime'
                        assert prepared.structured_content['model']==GROK_MODEL


# spec: ANYM-359
def test_owner_rehearsal_keeps_its_choice_and_leaves_call_voice(tmp_path):
    data=tmp_path/'data'
    with TestClient(create_app(data,Archive())) as client:
        client.headers.update(owner_headers(data))
        set_voice(client,GROK)
        before=client.get('/api/call-voice').json()
        for choice in ({'dialog_engine':'gemini_live_flows'},{'dialog_engine':'gemini_live','voice':'Charon'},
                       {'dialog_engine':'grok_realtime','grok_voice':'sal'}):
            brief=client.post('/api/briefs',json={**BRIEF,**choice},headers=HEADERS).json()
            assert {key:brief[key] for key in choice}==choice
        external=client.post('/api/external/briefs',json={**BRIEF,**ROUTES['phone'],'voice':'Leda'},headers=HEADERS).json()
        assert external['dialog_engine']=='gemini_live' and external['voice']=='Leda'
        assert client.get('/api/call-voice').json()==before


# spec: ANYM-359
@pytest.mark.parametrize('value',[
    {'provider':'grok_realtime','grok_voice':'unknown'},{'provider':'gemini_live','gemini_voice':'Nobody'},
    {'provider':'gemini_live_flows'},{'provider':'grok_realtime','gemini_custom_voice_id':'voice_owner_fixture'},
    {'provider':'gemini_live','extra':'field'}])
def test_call_voice_refuses_unknown_voice(tmp_path,value):
    data=tmp_path/'data'
    with TestClient(create_app(data,Archive())) as client:
        client.headers.update(owner_headers(data))
        before=client.get('/api/call-voice').json()
        assert set_voice(client,value).status_code==422
        assert client.get('/api/call-voice').json()==before
        assert client.post('/api/briefs',json={**BRIEF,'grok_voice':'unknown'},headers=HEADERS).status_code==422
        assert client.post('/api/briefs',json={**BRIEF,'voice':'Nobody'},headers=HEADERS).status_code==422
    assert not (data/'call-voice.json').exists()


# spec: ANYM-359
def test_capabilities_report_owner_call_voice(tmp_path):
    data=tmp_path/'data'
    app=create_app(data,Archive(),agent_registry=every_channel(tmp_path))
    class Provider:
        async def preflight(self,*args):raise AssertionError('no provider requests')
        async def probe(self,*args):raise AssertionError('no probes')
    with TestClient(app) as client:
        app.state.outbound.providers['twilio']=Provider()
        client.headers.update(owner_headers(data))
        caps=client.post('/v1/capabilities',json={},headers=AGENT).json()
        assert caps['call_voice']=={'provider':'gemini_live','model':MODEL,'voice':'Puck','custom_voice':False}
        set_voice(client,{'provider':'gemini_live','gemini_custom_voice_id':'voice_owner_fixture'})
        caps=client.post('/v1/capabilities',json={},headers=AGENT).json()
        assert caps['call_voice']=={'provider':'gemini_live','model':MODEL,'voice':'replicated','custom_voice':True}
        assert 'voice_owner_fixture' not in json.dumps(caps)
        set_voice(client,GROK)
        caps=client.post('/v1/capabilities',json={},headers=AGENT).json()
        assert caps['call_voice']=={'provider':'grok_realtime','model':GROK_MODEL,'voice':'eve','custom_voice':False}
        assert app.state.service.list_calls()==[]


# spec: ANYM-359
@pytest.mark.parametrize('voice,present,missing',[
    ({'provider':'grok_realtime'},'gemini','xai'),({'provider':'gemini_live'},'xai','gemini')])
def test_readiness_requires_the_call_voice_provider_key_on_every_channel(tmp_path,voice,present,missing):
    data=tmp_path/'data'
    with TestClient(create_app(data,Archive(),agent_registry=every_channel(tmp_path))) as client:
        client.headers.update(owner_headers(data))
        set_voice(client,voice)
        client.post('/api/credentials',json={present:'synthetic-model-key'},headers=HEADERS)
        ready=client.post('/v1/capabilities',json={},headers=AGENT).json()['calling_readiness']['channels']
        assert all('model_key_required' in ready[channel]['blockers'] for channel in ROUTES), ready
        assert all(ready[channel]['model_providers']==['gemini','xai'] for channel in ROUTES)
        client.post('/api/credentials',json={missing:'synthetic-model-key'},headers=HEADERS)
        ready=client.post('/v1/capabilities',json={},headers=AGENT).json()['calling_readiness']['channels']
        assert not any('model_key_required' in ready[channel]['blockers'] for channel in ROUTES), ready


# spec: ANYM-359
def test_call_voice_command_shows_and_sets_the_owner_setting(tmp_path,monkeypatch):
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'tests/platform'))
    from runtime_helpers import port, server
    from call_voice import call_voice
    from urllib.error import HTTPError
    data=tmp_path/'data'
    selected=port()
    app=create_app(data,Archive())
    monkeypatch.setenv('VOICE_DATA_DIR',str(data));monkeypatch.setenv('VOICE_PORT',str(selected))
    with server(app,selected):
        assert call_voice()['provider']=='gemini_live'
        assert call_voice({'provider':'grok_realtime','grok_voice':'rex'})['grok_voice']=='rex'
        assert call_voice()=={'provider':'grok_realtime','gemini_voice':'Puck','grok_voice':'rex',
                              'gemini_custom_voice_id':None,'voice_style':'neutral'}
        with pytest.raises(HTTPError) as refused:
            call_voice({'provider':'grok_realtime','grok_voice':'unknown'})
        assert refused.value.code==422 and call_voice()['grok_voice']=='rex'


# spec: ANYM-359
async def test_agents_never_receive_the_custom_voice_id(tmp_path,monkeypatch):
    monkeypatch.setenv('VOICE_EXTERNAL_MCP','1')
    data=tmp_path/'data'
    secret='voice_owner_fixture'
    app=create_app(data,Archive(),agent_registry=every_channel(tmp_path),control_token='control-fixture-token')
    service=app.state.service
    service.set_call_voice(service.call_voice().model_copy(update={'gemini_custom_voice_id':secret}))
    seen=[]
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app),base_url='http://testserver') as http:
            async def agent(operation,body):
                response=await http.post(f'/v1/{operation}',json=body,headers=AGENT)
                assert response.status_code==200, (operation,response.text)
                seen.append(response.text)
                return response.json()
            body={**BRIEF,'source_message_ids':[],'approval_fields':['delivery_window'],**ROUTES['telegram']}
            parent=await agent('prepare_external_call',{'brief':body})
            assert parent['custom_voice_id']=='redacted' and secret in json.dumps(service.get_brief(parent['id']))
            call=service.start(parent['id']);cid=call['id'];service.connected(cid)
            service.transcript(cid,'user',f'12–13, {secret}')
            decision=service.tool(cid,'record_answer',{'field':'delivery_window','value':'12–13','evidence':'12–13'},'t1')['owner_decision']
            service.stop(cid,'awaiting_owner');service.decide(cid,decision['decision_id'],'No')
            for operation in ('get_call_result','get_call_status'):
                await agent(operation,{'call_id':cid})
            await agent('prepare_followup',{'call_id':cid,'decision_id':decision['decision_id']})
            await agent('list_briefs',{});await agent('list_calls',{});await agent('get_owner_requests',{})
            browser=await agent('prepare_call',{'brief':{**BRIEF,'source_message_ids':[]}})
            started=await agent('start_call',{'brief_id':browser['id']})
            await agent('stop_call',{'call_id':started['id']})
            owner=await http.get(f'/api/briefs/{browser["id"]}',headers=owner_headers(data))
            assert owner.json()['custom_voice_id']==secret
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app),headers={'Authorization':'Bearer control-fixture-token'}) as http:
            async with streamable_http_client('http://testserver/control/mcp',http_client=http) as (read,write):
                async with ClientSession(read,write) as client:
                    await client.initialize()
                    async def tool(name,arguments):
                        result=await client.call_tool(name,arguments)
                        assert not result.is_error, (name,result)
                        seen.append(json.dumps(result.model_dump(mode='json')))
                        return result.structured_content
                    prepared=await tool('prepare_call',{'brief':BRIEF})
                    assert prepared['custom_voice_id']=='redacted'
                    await tool('prepare_external_call',{'brief':{**BRIEF,**ROUTES['whatsapp']}})
                    started=await tool('start_call',{'brief_id':prepared['id']})
                    for name in ('get_call_status','get_call_result','stop_call'):
                        await tool(name,{'call_id':started['id']})
                    await tool('get_call_result',{'call_id':cid})
    assert len(seen)==17 and not any(secret in text for text in seen)
