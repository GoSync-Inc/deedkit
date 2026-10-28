"""Per-call voice engine choice (ANYM-584). Synthetic only; no provider I/O."""
import itertools
import json
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_api import METHODS
from app import create_app
from core import GROK_MODEL, GROK_VOICES, MODEL, VOICES, ServiceError
import voice_engines
from test_call_voice_default import AGENT, ROUTES, every_channel, set_voice
from test_tasks import Archive, BRIEF, HEADERS, owner_headers

KEYS = {'gemini':'synthetic-gemini-key','xai':'synthetic-xai-key'}
GROK_OWNER = {'provider':'grok_realtime','grok_voice':'eve','gemini_voice':'Kore'}
REPLICATED = {'provider':'gemini_live','gemini_voice':'Kore','gemini_custom_voice_id':'voice_owner_fixture'}
VENDOR_KEY = {'gemini_live':'gemini','grok_live':'xai'}


def prepare(client,channel='phone',**extra):
    body={**BRIEF,'source_message_ids':[],**(ROUTES[channel] or {}),**extra}
    return client.post('/v1/prepare_call' if ROUTES[channel] is None else '/v1/prepare_external_call',
                       json={'brief':body},headers=AGENT)


def briefs(client):
    return client.app.state.service.db.execute('SELECT count(*) FROM briefs').fetchone()[0]


def app_client(tmp_path,keys=KEYS):
    data=tmp_path/'data'
    client=TestClient(create_app(data,Archive(),agent_registry=every_channel(tmp_path)))
    client.__enter__()
    client.headers.update(owner_headers(data))
    if keys:
        assert client.post('/api/credentials',json=keys,headers=HEADERS).status_code==200
    return client


# spec: ANYM-584
def test_voice_engine_choice_is_deterministic_and_never_crosses_vendor():
    # Every request, language, owner engine and key set: the whole domain, not a sample.
    seen=0
    for requested,language,provider,gemini,xai in itertools.product(
            [None,'auto','gemini_live','grok_live'],['ru','en','id','de'],['gemini_live','grok_realtime'],[False,True],[False,True]):
        credentials={'gemini':gemini,'xai':xai}
        owner='grok_live' if provider=='grok_realtime' else 'gemini_live'
        try:
            choice=voice_engines.choose(requested,language,provider,credentials)
        except ServiceError as error:
            assert requested is not None
            assert error.status==503
            if requested=='auto':
                assert error.code=='no_voice_engine_available'
                assert set(error.detail['skipped'])=={'gemini_live','grok_live'}
            else:
                assert error.code=='voice_engine_unavailable' and error.detail['voice_engine']==requested
                assert error.detail['reason']==('key_not_configured' if not credentials[VENDOR_KEY[requested]] else 'language_unsupported')
            continue
        seen+=1
        assert choice==voice_engines.choose(requested,language,provider,dict(credentials))
        if requested is None:
            # Today's behaviour: the owner's engine, with no new key or language check.
            assert choice=={'requested':None,'selected':owner,'reason':'owner_call_voice','skipped':{}}
            continue
        assert credentials[VENDOR_KEY[choice['selected']]] and language in ('ru','en','id')
        if requested!='auto':
            assert choice['selected']==requested and choice['reason']=='requested'
        elif credentials[VENDOR_KEY[owner]]:
            assert choice=={'requested':'auto','selected':owner,'reason':'auto_owner_engine','skipped':{}}
        else:
            assert choice['selected']!=owner and choice['reason']=='auto_first_fitting'
            assert choice['skipped']=={owner:'key_not_configured'}
    assert seen>16


# spec: ANYM-584
def test_voice_engine_requirements_skip_an_engine_that_lacks_them(monkeypatch):
    # Both real engines carry ru/en/id and the task tools; a third table entry may not.
    limited={'vendor':'example','dialog_engine':'example_live','key':'example','model':'m','voices':[],
             'languages':['en'],'tool_calls':True,'success_fields':True}
    real=dict(voice_engines.ENGINES)
    for lack,reason in (({'languages':['en']},'language_unsupported'),({'tool_calls':False},'tool_calls_unsupported'),
                        ({'success_fields':False},'success_fields_unsupported')):
        monkeypatch.setattr(voice_engines,'ENGINES',{'example_live':{**limited,**lack},**real})
        monkeypatch.setattr(voice_engines,'BY_DIALOG_ENGINE',{'example_live':'example_live','gemini_live':'gemini_live'})
        credentials={'example':True,'gemini':True}
        language='ru' if reason=='language_unsupported' else 'en'
        choice=voice_engines.choose('auto',language,'example_live',credentials)
        assert choice=={'requested':'auto','selected':'gemini_live','reason':'auto_first_fitting','skipped':{'example_live':reason}}
        with pytest.raises(ServiceError) as refused:
            voice_engines.choose('example_live',language,'gemini_live',credentials)
        assert refused.value.detail=={'voice_engine':'example_live','reason':reason}


# spec: ANYM-584
def test_followup_keeps_parent_voice_engine_choice(tmp_path):
    client=app_client(tmp_path)
    try:
        body={'approval_fields':['delivery_window']}
        parent=prepare(client,'telegram',voice_engine='grok_live',**body).json()
        assert client.delete('/api/credentials',headers=HEADERS).status_code==200
        set_voice(client,{'provider':'gemini_live','gemini_voice':'Kore'})
        service=client.app.state.service
        call=service.start(parent['id']);cid=call['id'];service.connected(cid)
        service.transcript(cid,'user','12–13')
        decision=service.tool(cid,'record_answer',{'field':'delivery_window','value':'12–13','evidence':'12–13'},'t1')['owner_decision']
        service.stop(cid,'awaiting_owner');service.decide(cid,decision['decision_id'],'No')
        child=client.post('/v1/prepare_followup',json={'call_id':cid,'decision_id':decision['decision_id']},headers=AGENT).json()
        assert child['dialog_engine']=='grok_realtime' and child['model']==GROK_MODEL
        assert child['voice_engine']==parent['voice_engine']
    finally:
        client.__exit__(None,None,None)


# spec: ANYM-584
def test_agent_requested_engine_speaks_with_owner_voice_on_every_channel(tmp_path):
    client=app_client(tmp_path)
    try:
        assert set_voice(client,REPLICATED).status_code==200
        for channel in ROUTES:
            grok=prepare(client,channel,voice_engine='grok_live')
            assert grok.status_code==200, (channel,grok.text)
            b=grok.json()
            assert b['dialog_engine']=='grok_realtime' and b['model']==GROK_MODEL and b['grok_voice']=='rex'
            assert b['custom_voice_id'] is None
            assert b['voice_engine']=={'requested':'grok_live','selected':'grok_live','reason':'requested','skipped':{}}
        assert set_voice(client,GROK_OWNER).status_code==200
        gemini=prepare(client,'telegram',voice_engine='gemini_live').json()
        assert gemini['dialog_engine']=='gemini_live' and gemini['model']==MODEL and gemini['voice']=='Kore'
        auto=prepare(client,'whatsapp',voice_engine='auto').json()
        assert auto['dialog_engine']=='grok_realtime' and auto['grok_voice']=='eve'
        assert auto['voice_engine']['reason']=='auto_owner_engine'
        # The choice is part of the frozen snapshot.
        saved=client.get(f'/api/briefs/{auto["id"]}').json()
        assert saved['voice_engine']==auto['voice_engine'] and saved['snapshot_sha256']==auto['snapshot_sha256']
    finally:
        client.__exit__(None,None,None)


# spec: ANYM-584
def test_omitted_voice_engine_keeps_owner_call_voice_without_keys(tmp_path):
    client=app_client(tmp_path,keys=None)
    try:
        assert set_voice(client,GROK_OWNER).status_code==200
        for channel in ROUTES:
            response=prepare(client,channel)
            assert response.status_code==200, (channel,response.text)
            b=response.json()
            assert (b['dialog_engine'],b['model'],b['grok_voice'])==('grok_realtime',GROK_MODEL,'eve')
            assert b['voice_engine']=={'requested':None,'selected':'grok_live','reason':'owner_call_voice','skipped':{}}
    finally:
        client.__exit__(None,None,None)


# spec: ANYM-584
def test_no_fitting_voice_engine_is_refused_before_saving(tmp_path):
    client=app_client(tmp_path,keys={'gemini':KEYS['gemini']})
    try:
        for channel in ROUTES:
            refused=prepare(client,channel,voice_engine='grok_live')
            assert refused.status_code==503
            assert refused.json()=={'error':'voice_engine_unavailable','voice_engine':'grok_live','reason':'key_not_configured'}
        assert client.delete('/api/credentials',headers=HEADERS).status_code==200
        refused=prepare(client,'phone',voice_engine='auto')
        assert refused.status_code==503 and refused.json()=={'error':'no_voice_engine_available',
            'skipped':{'gemini_live':'key_not_configured','grok_live':'key_not_configured'}}
        assert prepare(client,'phone',voice_engine='gemini').status_code==422
        assert briefs(client)==0
    finally:
        client.__exit__(None,None,None)


# spec: ANYM-584
def test_frozen_grok_brief_never_falls_back_to_gemini_key(tmp_path):
    client=app_client(tmp_path)
    try:
        setup={'enabled':True,'allowed':{},'twilio':{'account_sid':'AC'+'1'*32,'auth_token':'synthetic-token',
               'phone_from':'+12025550125','public_url':'https://voice.example.test'}}
        assert client.post('/api/calling',json=setup,headers=HEADERS).status_code==200
        grok=prepare(client,'phone',voice_engine='grok_live').json()
        gemini=prepare(client,'phone',voice_engine='gemini_live').json()
        assert client.post('/v1/preflight_call',json={'brief_id':grok['id']},headers=AGENT).status_code==200
        # Only the Gemini key left: the Grok brief does not move to Google, it reports the missing key.
        assert client.delete('/api/credentials',headers=HEADERS).status_code==200
        assert client.post('/api/credentials',json={'gemini':KEYS['gemini']},headers=HEADERS).status_code==200
        refused=client.post('/v1/preflight_call',json={'brief_id':grok['id']},headers=AGENT)
        assert refused.status_code==409 and refused.json()['error']=='model_key_required'
        assert client.post('/v1/preflight_call',json={'brief_id':gemini['id']},headers=AGENT).status_code==200
    finally:
        client.__exit__(None,None,None)


# spec: ANYM-584
def test_capabilities_list_voice_engines_and_status_reports_choice(tmp_path):
    client=app_client(tmp_path,keys={'xai':KEYS['xai']})
    try:
        listing=client.post('/v1/capabilities',json={},headers=AGENT).json()['voice_engines']
        assert listing=={'engines':[
            {'name':'gemini_live','dialog_engine':'gemini_live','vendor':'google','available':False,'model':MODEL,
             'voices':VOICES,'languages':['ru','en','id'],'tool_calls':True,'success_fields':True},
            {'name':'grok_live','dialog_engine':'grok_realtime','vendor':'xai','available':True,'model':GROK_MODEL,
             'voices':GROK_VOICES,'languages':['ru','en','id'],'tool_calls':True,'success_fields':True}],
            'omitted_uses':'gemini_live','voice':'owner_call_voice','transcript_analysis_vendor':'google',
            'transcript_analysis_input':['brief','facts','transcript','answers','decisions','tool_calls']}
        assert KEYS['xai'] not in json.dumps(listing)
        b=prepare(client,'browser',voice_engine='auto').json()
        assert b['voice_engine']=={'requested':'auto','selected':'grok_live','reason':'auto_first_fitting',
                                   'skipped':{'gemini_live':'key_not_configured'}}
        call=client.post('/v1/start_call',json={'brief_id':b['id']},headers=AGENT).json()
        status=client.post('/v1/get_call_status',json={'call_id':call['id']},headers=AGENT).json()
        assert status['voice_engine']==b['voice_engine']
        result=client.post('/v1/get_call_result',json={'call_id':call['id']},headers=AGENT).json()
        assert result['brief']['voice_engine']==b['voice_engine']
    finally:
        client.__exit__(None,None,None)


# spec: ANYM-584
def test_voice_engine_is_one_shared_agent_brief_field_and_owner_briefs_refuse_it(tmp_path):
    for name in ('prepare_call','prepare_external_call'):
        brief=METHODS[name][0].model_json_schema()
        field=brief['$defs'][next(k for k in brief['$defs'] if 'Brief' in k)]['properties']['voice_engine']
        assert {'enum':['auto','gemini_live','grok_live'],'type':'string'} in field['anyOf']
    catalog=json.loads((Path(__file__).resolve().parents[3]/'services/gateway/src/agent_gateway/catalog.json').read_text())
    rows={row['name']:row for row in catalog if row['module']=='calls'}
    assert all('voice_engine' in json.dumps(rows['calls_'+n]['inputSchema']) for n in ('prepare_call','prepare_external_call'))
    client=app_client(tmp_path)
    try:
        owner=client.post('/api/briefs',json={**BRIEF,'source_message_ids':[],'voice_engine':'grok_live'},headers=HEADERS)
        assert owner.status_code==422
    finally:
        client.__exit__(None,None,None)


# spec: ANYM-584
async def test_control_mcp_accepts_voice_engine(tmp_path,monkeypatch):
    monkeypatch.setenv('VOICE_EXTERNAL_MCP','1')
    app=create_app(tmp_path,Archive(),control_token='control-fixture-token')
    app.state.service.engine_credentials=lambda:{'gemini':True,'xai':True}
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app),headers={'Authorization':'Bearer control-fixture-token'}) as http:
            async with streamable_http_client('http://testserver/control/mcp',http_client=http) as (read,write):
                async with ClientSession(read,write) as client:
                    await client.initialize()
                    prepared=await client.call_tool('prepare_external_call',{'brief':{**BRIEF,**ROUTES['whatsapp'],'voice_engine':'grok_live'}})
                    assert not prepared.is_error, prepared
                    assert prepared.structured_content['dialog_engine']=='grok_realtime'
                    assert prepared.structured_content['voice_engine']['selected']=='grok_live'
    app.state.service.engine_credentials=lambda:{}
    with pytest.raises(ServiceError,match='voice_engine_unavailable'):
        from core import AgentBrief
        app.state.service.with_call_voice(AgentBrief(**{**BRIEF,'voice_engine':'gemini_live'}))


# spec: ANYM-584
def test_omitted_voice_engine_never_reads_key_status(tmp_path):
    from core import AgentBrief, VoiceService
    service=VoiceService(tmp_path)
    def unreadable():raise OSError('synthetic unreadable key store')
    service.engine_credentials=unreadable
    assert service.with_call_voice(AgentBrief(**BRIEF))._voice_engine['reason']=='owner_call_voice'
    with pytest.raises(ServiceError) as error:
        service.with_call_voice(AgentBrief(**{**BRIEF,'voice_engine':'auto'}))
    assert (error.value.code,error.value.status,error.value.detail)==(
        'voice_engine_unavailable',503,{'voice_engine':'auto','reason':'key_status_unreadable'})


# spec: ANYM-584
def test_regression_replay_rebuilds_an_agent_brief_with_its_engine_choice(tmp_path):
    from core import AgentBrief, VoiceService
    from regressions import replay_input
    service=VoiceService(tmp_path/'agent')
    service.engine_credentials=lambda:{'gemini':True,'xai':True}
    for engine in (None,'grok_live'):
        stored=service.prepare(service.with_call_voice(AgentBrief(**{**BRIEF,'voice_engine':engine})),[])
        replay=VoiceService(tmp_path/f'replay-{engine}').prepare(replay_input(stored),[])
        assert replay['voice_engine']==stored['voice_engine']
        assert replay['dialog_engine']==stored['dialog_engine']


# spec: ANYM-584
async def test_control_mcp_refusal_names_engine_and_reason(tmp_path,monkeypatch):
    monkeypatch.setenv('VOICE_EXTERNAL_MCP','1')
    app=create_app(tmp_path,Archive(),control_token='control-fixture-token')
    app.state.service.engine_credentials=lambda:{'gemini':False,'xai':False}
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app),headers={'Authorization':'Bearer control-fixture-token'}) as http:
            async with streamable_http_client('http://testserver/control/mcp',http_client=http) as (read,write):
                async with ClientSession(read,write) as client:
                    await client.initialize()
                    browser=await client.call_tool('prepare_call',{'brief':{**BRIEF,'voice_engine':'grok_live'}})
                    external=await client.call_tool('prepare_external_call',{'brief':{**BRIEF,**ROUTES['whatsapp'],'voice_engine':'auto'}})
        assert app.state.service.db.execute('SELECT count(*) FROM briefs').fetchone()[0]==0
    assert browser.is_error and json.loads(browser.content[0].text.split(': ',1)[1])=={
        'error':'voice_engine_unavailable','voice_engine':'grok_live','reason':'key_not_configured'}
    assert external.is_error and json.loads(external.content[0].text.split(': ',1)[1])=={'error':'no_voice_engine_available',
        'skipped':{'gemini_live':'key_not_configured','grok_live':'key_not_configured'}}


# spec: ANYM-595
def test_an_unreadable_call_voice_leaves_no_engine_ready_and_prepare_refuses_each(tmp_path):
    client=app_client(tmp_path)
    try:
        (tmp_path/'data'/'call-voice.json').write_text('{damaged')
        ready=client.post('/v1/capabilities',json={},headers=AGENT).json()['calling_readiness']['channels']
        for channel in ROUTES:
            assert ready[channel]['ready_engines']==[] and ready[channel]['auto']['ready'] is False, channel
            for engine in voice_engines.ENGINES:
                assert 'call_voice_invalid' in ready[channel]['voice_engines'][engine]['blockers'], (channel,engine)
                refused=prepare(client,channel,voice_engine=engine)
                assert refused.status_code==503 and 'call_voice_invalid' in refused.text, (channel,engine,refused.text)
        assert briefs(client)==0
    finally:
        client.__exit__(None,None,None)
