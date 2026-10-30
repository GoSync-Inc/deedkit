"""Optional recordings: reviewed tool/scopes, independent HTTP backend, no provider sessions."""
import asyncio
import hashlib
import json

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG
from agent_gateway.http_clients import Clients
from agent_gateway.policy import Denied
from test_policy import config
from test_transport import rpc

TOOLS=['recordings_capabilities','recordings_list','recordings_read']
SCOPES=['recordings:read','recordings:content']


def setup(tmp_path, gateway_scopes=SCOPES, backend_scopes=SCOPES, content_tool=True, deployed=True, backend_app=None):
    value=config()
    value['deployment']={'modules':['calendar']+(['recordings'] if deployed else []),
        'backends':{'calendar':{'url':'http://calendar'},'recordings':{'url':'http://recordings'}}}
    tools=['platform_capabilities','calendar_slots', *TOOLS]
    if not content_tool:
        tools.remove('recordings_read')
    value['profiles']['reader']['tools']=tools
    actor=value['principals'][0]
    actor['grants']['recordings']=list(gateway_scopes)
    for module in ('calendar','recordings'):
        key=tmp_path/(module+'.key')
        key.write_text('synthetic-'+module)
        key.chmod(0o600)
        actor['credentials'][module]={'key_file':str(key),
            'key_sha256':hashlib.sha256(key.read_bytes()).hexdigest()}
    path=tmp_path/'policy.json'
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    calls=[]
    async def backend(request):
        module=request.url.host
        if module=='recordings' and backend_app is not None:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=backend_app),
                                         base_url='http://recordings') as real:
                return await real.post(request.url.path,content=request.content,headers=request.headers)
        operation=request.url.path.removeprefix('/v1/')
        if operation=='capabilities':
            return httpx.Response(200,json={'permissions':list(backend_scopes) if module=='recordings' else ['availability:read'],
                'tool_manifest':{'names':['capabilities','list_recordings','read_recording']}})
        calls.append((module,operation,json.loads(request.content)))
        return httpx.Response(200,json={'items':[],'coverage':'unknown'})
    return create_app(path,'http://testserver',transports={m:httpx.MockTransport(backend)
                      for m in ('calendar','recordings')}),calls


# spec: ANYM-648
def test_optional_recordings_catalog_and_routing(tmp_path):
    app,calls=setup(tmp_path)
    assert all(CATALOG[n]['required_manifest'] and CATALOG[n]['annotations']['readOnlyHint'] for n in TOOLS)
    with TestClient(app) as c:
        result=rpc(c,'tools/list').json()['result']
        assert set(TOOLS) <= {t['name'] for t in result['tools']}
        read=rpc(c,'tools/call',{'name':'recordings_read','arguments':{
            'account':'omi-a','recording_id':'conv-a'}}).json()['result']
        assert not read.get('isError'),read
        assert calls==[('recordings','read_recording',{'account':'omi-a','recording_id':'conv-a',
            'kind':'transcript','page_chars':4000})]


# spec: ANYM-648
@pytest.mark.parametrize('kwargs',[{'gateway_scopes':['recordings:read']},
    {'backend_scopes':['recordings:read']},{'content_tool':False},{'deployed':False}])
def test_every_gateway_ceiling_is_required_before_recordings_egress(tmp_path,kwargs):
    app,calls=setup(tmp_path,**kwargs)
    with TestClient(app) as c:
        result=rpc(c,'tools/call',{'name':'recordings_read','arguments':{
            'account':'omi-a','recording_id':'conv-a'}}).json()['result']
        assert result['isError']
        assert calls==[]
        normal=rpc(c,'tools/call',{'name':'calendar_slots','arguments':{
            'member_ids':['synthetic'],'duration_minutes':30,
            'start':'2026-10-01T00:00:00Z','end':'2026-10-02T00:00:00Z'}}).json()['result']
        assert not normal.get('isError'),normal


# spec: ANYM-648
def test_recordings_saturation_failure_and_cancellation_leave_other_modules_available():
    async def run():
        entered=asyncio.Event()
        async def slow(request):
            entered.set()
            await asyncio.Event().wait()
        deployment={'modules':['recordings','calendar'], 'backends':{
            name:{'url':'http://'+name} for name in ('recordings','calendar')}}
        async with Clients(deployment,{'recordings':httpx.MockTransport(slow),
                'calendar':httpx.MockTransport(lambda r:httpx.Response(200,json={'coverage':'unknown'}))}) as clients:
            tasks=[asyncio.create_task(clients.call('recordings','list_recordings','synthetic',{})) for _ in range(32)]
            try:
                await entered.wait()
                await asyncio.sleep(0.01)
                with pytest.raises(Denied,match='backend_busy'):
                    await clients.call('recordings','list_recordings','synthetic',{})
                async with asyncio.timeout(0.2):
                    assert (await clients.call('calendar','slots','synthetic',{}))['coverage']=='unknown'
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
            assert clients.budgets['recordings'].pending==0
    asyncio.run(run())


# spec: ANYM-648
def test_gateway_dispatch_reaches_actual_recordings_http_routes(tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'services/recordings/src'))
    from deedkit_recordings.app import create_app as recordings_app
    def private(name,text):
        path=tmp_path/name
        path.write_text(text)
        path.chmod(0o600)
        return str(path)
    token=private('omi.key','omi_dev_synthetic')
    secret=private('cursor.key','x'*64)
    doc={'version':1,'cursor_secret_file':secret,
         'accounts':[{'id':'omi-a','provider':'omi','label':'Synthetic','token_file':token}],
         'principals':[{'id':'agent-a','key_sha256':hashlib.sha256(b'synthetic-recordings').hexdigest(),
              'expires_at':'2099-01-01T00:00:00Z','accounts':['omi-a'],'permissions':SCOPES}]}
    path=private('recordings.json',json.dumps(doc))
    seen=[]
    def provider(request):
        seen.append(request)
        return httpx.Response(200,json={'id':'conv-a','transcript_included':True,
            'structured':{'title':'Synthetic'},'transcript_segments':[{'text':'Synthetic transcript','start':0}]})
    backend=recordings_app(path,transport=httpx.MockTransport(provider))
    app,_=setup(tmp_path,backend_app=backend)
    with TestClient(app) as c:
        data=rpc(c,'tools/call',{'name':'recordings_read','arguments':{
            'account':'omi-a','recording_id':'conv-a'}}).json()['result']
        assert not data.get('isError'),data
        assert 'Synthetic transcript' in json.dumps(data)
        doc['principals'][0]['permissions']=['recordings:read']
        private('recordings.json',json.dumps(doc))
        denied=rpc(c,'tools/call',{'name':'recordings_read','arguments':{
            'account':'omi-a','recording_id':'conv-a'}}).json()['result']
        assert denied['isError'] and len(seen)==1
