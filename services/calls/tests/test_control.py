import json

import httpx
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app import create_app
from test_tasks import Archive, BRIEF


async def test_mcp_separate_auth_browser_job_and_durable_result(tmp_path):
    app=create_app(tmp_path,Archive(),control_token='control-fixture-token')
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver') as c:
            r=await c.post('/control/mcp',json={})
            assert r.status_code==401
            r=await c.post('/control/mcp',headers={'Authorization':'Bearer archive-key'},json={})
            assert r.status_code==401
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app),headers={'Authorization':'Bearer control-fixture-token'}) as http:
            async with streamable_http_client('http://testserver/control/mcp',http_client=http) as (read,write):
                async with ClientSession(read,write) as client:
                    await client.initialize()
                    manifest=await client.list_tools()
                    assert {t.name for t in manifest.tools} == {'get_voice_capabilities','prepare_call','start_call','get_call_status','stop_call','get_call_result',
                                                              'get_call_analysis','get_voice_learning','submit_call_analysis',
                                                              'get_owner_requests','prepare_followup'}
                    assert 'submit_owner_decision' not in {t.name for t in manifest.tools}
                    result=await client.call_tool('get_voice_capabilities',{})
                    assert result.structured_content['channels']['whatsapp']=='not_connected'
                    prepared=await client.call_tool('prepare_call',{'brief':BRIEF})
                    assert not prepared.is_error, prepared
                    brief=prepared.structured_content
                    started=await client.call_tool('start_call',{'brief_id':brief['id']})
                    identity=started.structured_content['id']
                    assert started.structured_content['status']=='waiting_for_browser'
                    repeated=await client.call_tool('start_call',{'brief_id':brief['id']})
                    assert repeated.structured_content['id']==identity
                    stopped=await client.call_tool('stop_call',{'call_id':identity})
                    assert stopped.structured_content['status']=='finished'
                    saved=await client.call_tool('get_call_result',{'call_id':identity})
                    assert saved.structured_content['brief']['facts'][1]['source_ref']=='selected-message'
                    assert 'control-fixture-token' not in json.dumps(saved.structured_content)
                    report=await client.call_tool('get_call_analysis',{'call_id':identity})
                    assert report.structured_content['status']=='no_speech'
                    learning=await client.call_tool('get_voice_learning',{})
                    assert len(learning.structured_content['cases'])==12
                    inbox=await client.call_tool('get_owner_requests',{})
                    assert inbox.structured_content['items']==[]
                    assert inbox.structured_content['notification_channel']=='not_connected'
                    assert 'tell the owner yourself' in inbox.structured_content['notification_explanation']
                    service=app.state.service
                    second=await client.call_tool('prepare_call',{'brief':{**BRIEF,'approval_fields':['delivery_window']}})
                    created=await client.call_tool('start_call',{'brief_id':second.structured_content['id']})
                    cid=created.structured_content['id'];service.connected(cid)
                    service.transcript(cid,'user','12–13')
                    request=service.tool(cid,'record_answer',{'field':'delivery_window','value':'12–13','evidence':'12–13'},'time')['owner_decision']
                    service.tool(cid,'end_conversation',{'reason':'needs_owner','summary':'Ответим позже'},'bye')
                    service.stop(cid,'awaiting_owner')
                    pending=await client.call_tool('get_owner_requests',{})
                    assert pending.structured_content['items'][0]['decision']['status']=='pending'
                    # Simulated trusted owner channel, not an MCP agent's self-approval.
                    service.decide(cid,request['decision_id'],'Нет')
                    followup=await client.call_tool('prepare_followup',{'call_id':cid,'decision_id':request['decision_id']})
                    assert followup.structured_content['followup']['owner_answer']=='Нет'
                    child=await client.call_tool('start_call',{'brief_id':followup.structured_content['id']})
                    assert child.structured_content['requires_browser_join'] is True
                    assert child.structured_content['status']=='waiting_for_browser'


# spec: ANYM-595
async def test_control_call_status_names_the_engine_the_call_used(tmp_path):
    from core import BriefInput
    app=create_app(tmp_path,Archive(),control_token='control-fixture-token')
    async with app.router.lifespan_context(app):
        owner={'Authorization':'Bearer '+(tmp_path/'owner.token').read_text().strip(),'Origin':'http://testserver','X-Voice-Service':'1'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver') as c:
            assert (await c.post('/api/credentials',json={'xai':'synthetic-xai-key'},headers=owner)).status_code==200
        service=app.state.service
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app),headers={'Authorization':'Bearer control-fixture-token'}) as http:
            async with streamable_http_client('http://testserver/control/mcp',http_client=http) as (read,write):
                async with ClientSession(read,write) as client:
                    await client.initialize()
                    prepared=await client.call_tool('prepare_call',{'brief':{**BRIEF,'voice_engine':'grok_live'}})
                    assert not prepared.is_error, prepared
                    started=await client.call_tool('start_call',{'brief_id':prepared.structured_content['id']})
                    status=(await client.call_tool('get_call_status',{'call_id':started.structured_content['id']})).structured_content
                    assert status['voice_engine']=={'requested':'grok_live','selected':'grok_live','reason':'requested','skipped':{}}
                    assert status['voice_engine']==service.get_call(status['id'])['brief']['voice_engine']
                    await client.call_tool('stop_call',{'call_id':status['id']})
                    older=service.start(service.prepare(BriefInput(**BRIEF),[])['id'])['id']
                    legacy=(await client.call_tool('get_call_status',{'call_id':older})).structured_content
                    assert legacy['id']==older and 'voice_engine' not in legacy
