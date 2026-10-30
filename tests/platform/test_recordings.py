"""Omi contract tests: synthetic HTTP only, no wearable/provider sessions."""
import asyncio
import hashlib
import importlib
import json
from pathlib import Path
import sys

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'services/recordings/src'))
SCOPES = ['recordings:read', 'recordings:content']
HEADERS = {'Authorization': 'Bearer backend-a'}


def private(path, value):
    path.write_text(value)
    path.chmod(0o600)
    return str(path)


def config(tmp_path):
    secret = private(tmp_path / 'cursor.key', 'c' * 64)
    token = private(tmp_path / 'omi.key', 'omi_dev_synthetic')
    value = {'version': 1, 'cursor_secret_file': secret,
             'accounts': [{'id': 'omi-a', 'provider': 'omi', 'label': 'Synthetic', 'token_file': token}],
             'principals': [{'id': 'agent-a', 'key_sha256': hashlib.sha256(b'backend-a').hexdigest(),
                             'accounts': ['omi-a'], 'permissions': SCOPES.copy(),
                             'expires_at': '2099-01-01T00:00:00Z'}]}
    path = tmp_path / 'config.json'
    private(path, json.dumps(value))
    return path, value


def conversation(text='Synthetic ' * 60, **extra):
    return {'id': 'conv-a', 'created_at': '2026-10-01T00:00:00Z', 'transcript_included': True,
            'structured': {'title': 'Synthetic title', 'overview': 'Synthetic overview'},
            'transcript_segments': [{'text': text, 'speaker': 'SPEAKER_00', 'start': 0, 'end': 60}], **extra}


def application(path, handler, **kwargs):
    module = importlib.import_module('deedkit_recordings.app')
    return module.create_app(path, transport=httpx.MockTransport(handler), **kwargs)


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers=HEADERS)


# spec: ANYM-648
def test_omi_list_is_metadata_only_and_preserves_bound_provider_continuation(tmp_path):
    path, _ = config(tmp_path)
    seen = []
    def provider(request):
        seen.append(request)
        return httpx.Response(200, json=[conversation()], headers={
            'X-Omi-Has-More': 'true', 'X-Omi-Next-Cursor': 'provider-page-two',
            'X-Omi-Effective-Limit': '1', 'X-Omi-List-Truncated': 'false'})
    async def run():
        app = application(path, provider)
        async with client(app) as c:
            args = {'account': 'omi-a', 'limit': 1, 'start_date': '2026-10-01T00:00:00Z'}
            one = (await c.post('/v1/list_recordings', json=args)).json()
            assert one['items'][0]['title'] == 'Synthetic title'
            assert 'transcript' not in json.dumps(one) and 'Synthetic overview' not in json.dumps(one)
            assert one['pagination']['has_more'] is True and one['pagination']['complete'] is False
            cursor = one['next_cursor']
            assert (await c.post('/v1/list_recordings', json={**args, 'cursor': cursor})).status_code == 200
            assert (await c.post('/v1/list_recordings', json={**args, 'limit': 2, 'cursor': cursor})).status_code == 400
            assert (await c.post('/v1/list_recordings', json={**args, 'cursor': cursor[:-2]+'xx'})).status_code == 400
        assert len(seen) == 2
        assert seen[0].url.host == 'api.omi.me' and seen[0].url.scheme == 'https'
        assert seen[0].headers['Authorization'] == 'Bearer omi_dev_synthetic'
        assert seen[0].url.params['include_transcript'] == 'false'
        assert seen[1].url.params['cursor'] == 'provider-page-two'
    asyncio.run(run())


# spec: ANYM-648
@pytest.mark.parametrize('headers,complete', [({}, None), ({'X-Omi-Has-More':'oops'}, None),
    ({'X-Omi-Has-More':'false','X-Omi-List-Truncated':'true'}, False)])
def test_empty_or_malformed_provider_page_never_claims_complete(tmp_path, headers, complete):
    path, _ = config(tmp_path)
    async def run():
        async with client(application(path, lambda r: httpx.Response(200, json=[], headers=headers))) as c:
            result = (await c.post('/v1/list_recordings', json={'account':'omi-a'})).json()
            assert result['items'] == [] and result['pagination']['complete'] is complete
            assert result.get('next_cursor') is None
    asyncio.run(run())


# spec: ANYM-648
def test_content_continuation_rejects_changed_source_and_cross_credential_reuse(tmp_path):
    path, value = config(tmp_path)
    current = [conversation()]
    def provider(request):
        assert request.url.path == '/v1/dev/user/conversations/conv-a'
        return httpx.Response(200, json=current[0])
    async def run():
        app = application(path, provider)
        async with client(app) as c:
            args = {'account':'omi-a','recording_id':'conv-a','page_chars':100}
            first = (await c.post('/v1/read_recording', json=args)).json()
            assert first['content_state'] == 'available' and len(first['text']) == 100
            cursor = first['next_cursor']
            second = (await c.post('/v1/read_recording', json={**args,'cursor':cursor})).json()
            assert second['offset'] == 100 and second['source_digest'] == first['source_digest']
            value['principals'].append({**value['principals'][0], 'id':'agent-b',
                'key_sha256':hashlib.sha256(b'backend-b').hexdigest()})
            private(path,json.dumps(value))
            cross = await c.post('/v1/read_recording',json={**args,'cursor':cursor},
                                 headers={'Authorization':'Bearer backend-b'})
            assert cross.status_code == 400 and cross.json()['error'] == 'invalid_cursor'
            current[0] = conversation('changed transcript')
            changed = await c.post('/v1/read_recording',json={**args,'cursor':cursor})
            assert changed.status_code == 409 and changed.json()['error'] == 'source_changed'
    asyncio.run(run())


# spec: ANYM-648
def test_backend_scope_account_and_revocation_are_checked_before_egress(tmp_path):
    path, value = config(tmp_path)
    seen = []
    def provider(request):
        seen.append(request)
        return httpx.Response(200, json=conversation())
    async def run():
        async with client(application(path,provider)) as c:
            caps = (await c.post('/v1/capabilities',json={})).json()
            assert caps['accounts'] == [{'id':'omi-a','provider':'omi','label':'Synthetic'}]
            assert 'token_file' not in json.dumps(caps) and caps['audio'] == 'unsupported'
            args = {'account':'other-account','recording_id':'conv-a'}
            assert (await c.post('/v1/read_recording',json=args)).status_code == 403
            value['principals'][0]['permissions'] = ['recordings:read']
            private(path,json.dumps(value))
            assert (await c.post('/v1/read_recording',json={**args,'account':'omi-a'})).status_code == 403
            value['principals'] = []
            private(path,json.dumps(value))
            assert (await c.post('/v1/capabilities',json={})).status_code == 401
        assert seen == []
    asyncio.run(run())


# spec: ANYM-648
@pytest.mark.parametrize('payload,kind,state', [
    ({'transcript_included':False},'transcript','unavailable'),
    ({'transcript_segments':[]},'transcript','empty'),
    ({'processing_deferred':True},'summary','pending'),
    ({'structured':{'title':'Synthetic','overview':''}},'summary','empty'),
])
def test_content_states_and_strict_arguments(tmp_path,payload,kind,state):
    path,_ = config(tmp_path)
    async def run():
        app=application(path,lambda r:httpx.Response(200,json=conversation(**payload)))
        async with client(app) as c:
            args={'account':'omi-a','recording_id':'conv-a','kind':kind}
            result=(await c.post('/v1/read_recording',json=args)).json()
            assert result['content_state'] == state and result.get('next_cursor') is None
            for invalid in ({**args,'recording_id':'../other'}, {**args,'url':'https://other.invalid'},
                            {**args,'page_chars':True}, {**args,'kind':'audio'}):
                rejected=await c.post('/v1/read_recording',json=invalid)
                assert rejected.status_code == 422 and 'https://other.invalid' not in rejected.text
    asyncio.run(run())


# spec: ANYM-648
@pytest.mark.parametrize('status,body,code',[(302,b'private provider text','provider_unavailable'),
    (401,b'private provider text','provider_auth_required'),
    (200,b'not json private','invalid_provider_response'),
    (200,b'x'*1025,'response_too_large')])
def test_provider_failures_are_bounded_content_free_and_not_retried(tmp_path,status,body,code):
    path,_=config(tmp_path)
    seen=[]
    def provider(r):
        seen.append(r)
        return httpx.Response(status,content=body,headers={'Location':'http://127.0.0.1/private'})
    async def run():
        async with client(application(path,provider,provider_bytes=1024)) as c:
            response=await c.post('/v1/read_recording',json={'account':'omi-a','recording_id':'conv-a'})
            assert response.json()['error'] == code
            assert 'private' not in response.text and 'omi_dev_' not in response.text
        assert len(seen)==1
    asyncio.run(run())


# spec: ANYM-648
def test_timeout_saturation_and_cancellation_release_owned_admission(tmp_path):
    path,_=config(tmp_path)
    async def run():
        entered=asyncio.Event()
        async def provider(r):
            entered.set()
            await asyncio.Event().wait()
        app=application(path,provider,provider_timeout=0.1)
        async with client(app) as c:
            args={'account':'omi-a'}
            tasks=[asyncio.create_task(c.post('/v1/list_recordings',json=args)) for _ in range(4)]
            await entered.wait()
            busy=await c.post('/v1/list_recordings',json=args)
            assert busy.status_code==503 and busy.json()['error']=='provider_busy'
            tasks[0].cancel()
            await asyncio.gather(tasks[0],return_exceptions=True)
            remaining=await asyncio.gather(*tasks[1:])
            assert all(r.json()['error']=='provider_timeout' for r in remaining)
            assert app.state.provider.active==0
    asyncio.run(run())


# spec: ANYM-648
@pytest.mark.parametrize('permissions',[[],['recordings:content']])
def test_content_alone_and_empty_permissions_grant_no_access(tmp_path,permissions):
    path,value=config(tmp_path)
    value['principals'][0]['permissions']=permissions
    private(path,json.dumps(value))
    seen=[]
    async def run():
        app=application(path,lambda r:seen.append(r))
        async with client(app) as c:
            for op,args in [('capabilities',{}),('list_recordings',{'account':'omi-a'}),
                            ('read_recording',{'account':'omi-a','recording_id':'conv-a'})]:
                assert (await c.post('/v1/'+op,json=args)).status_code==403
        assert not seen
    asyncio.run(run())


# spec: ANYM-648
def test_continuation_cannot_cross_accounts_or_provider_token_rotation(tmp_path):
    path,value=config(tmp_path)
    other=private(tmp_path/'second.key','omi_dev_second_synthetic')
    value['accounts'].append({'id':'omi-b','provider':'omi','label':'Other','token_file':other})
    value['principals'][0]['accounts'].append('omi-b')
    private(path,json.dumps(value))
    seen=[]
    def provider(r):
        seen.append(r)
        return httpx.Response(200,json=[conversation()],headers={'X-Omi-Has-More':'true',
            'X-Omi-Next-Cursor':'next','X-Omi-Effective-Limit':'1','X-Omi-List-Truncated':'false'})
    async def run():
        async with client(application(path,provider)) as c:
            args={'account':'omi-a','limit':1}
            token=(await c.post('/v1/list_recordings',json=args)).json()['next_cursor']
            assert (await c.post('/v1/list_recordings',json={**args,'account':'omi-b','cursor':token})).status_code==400
            private(Path(value['accounts'][0]['token_file']),'omi_dev_rotated_synthetic')
            assert (await c.post('/v1/list_recordings',json={**args,'cursor':token})).status_code==400
        assert len(seen)==1
    asyncio.run(run())


# spec: ANYM-648
def test_provider_stream_read_deadline_private_files_and_input_bounds(tmp_path):
    path,value=config(tmp_path)
    class Stalled(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'['
            await asyncio.Event().wait()
    async def run():
        app=application(path,lambda r:httpx.Response(200,stream=Stalled()),provider_timeout=0.02)
        async with client(app) as c:
            response=await c.post('/v1/list_recordings',json={'account':'omi-a'})
            assert response.json()['error']=='provider_timeout' and app.state.provider.active==0
            assert (await c.post('/v1/capabilities',content=b'x'*16385)).status_code==413
            assert (await c.post('/v1/capabilities',content=b'{"x":NaN}')).status_code==422
            value['principals'][0]['expires_at']='2000-01-01T00:00:00Z'
            private(path,json.dumps(value))
            assert (await c.post('/v1/capabilities',json={})).status_code==401
            path.chmod(0o644)
            assert (await c.post('/v1/capabilities',json={})).json()=={'error':'configuration_unavailable'}
    asyncio.run(run())
