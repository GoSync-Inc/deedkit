"""PLAUD audio routing uses explicit independent gateway and backend ceilings."""
import hashlib
import json
import sys
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG
from test_policy import config
from test_transport import rpc

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'services/recordings/src'))


def private(path, value):
    path.write_text(value)
    path.chmod(0o600)
    return str(path)


# spec: ANYM-649
@pytest.mark.parametrize('ceiling', ['allowed', 'gateway', 'backend', 'profile', 'account'])
def test_plaud_audio_real_gateway_backend_roundtrip_requires_all_ceilings(tmp_path, ceiling):
    from deedkit_recordings.app import create_app as recordings_app
    scopes = ['recordings:read', 'recordings:audio']
    token = private(tmp_path / 'token', 'synthetic-remote-oauth')
    secret = private(tmp_path / 'secret', 's' * 64)
    key = private(tmp_path / 'key', 'synthetic-recordings')
    doc = {'version': 1, 'cursor_secret_file': secret,
           'accounts': [{'id': 'plaud-a', 'provider': 'plaud', 'label': 'Synthetic', 'token_file': token}],
           'principals': [{'id': 'a', 'key_sha256': hashlib.sha256(b'synthetic-recordings').hexdigest(),
             'expires_at': '2099-01-01T00:00:00Z', 'accounts': [] if ceiling == 'account' else ['plaud-a'],
             'permissions': ['recordings:read'] if ceiling == 'backend' else scopes}]}
    seen = []
    def provider(request):
        seen.append(request)
        assert request.url.host == 'mcp.plaud.ai'
        if request.method == 'DELETE':
            return httpx.Response(204)
        value = json.loads(request.content)
        if value['method'] == 'notifications/initialized':
            return httpx.Response(202)
        if value['method'] == 'initialize':
            result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'synthetic', 'version': '0.3.14'}}
        else:
            assert value['params'] == {'name': 'get_file', 'arguments': {'file_id': 'file-a'}}
            result = {'structuredContent': {'id': 'file-a', 'presigned_url': 'https://audio.invalid/synthetic'}}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': value['id'], 'result': result})
    backend = recordings_app(private(tmp_path / 'recordings.json', json.dumps(doc)), transport=httpx.MockTransport(provider))
    policy = config()
    policy['deployment'] = {'modules': ['recordings'], 'backends': {'recordings': {'url': 'http://recordings'}}}
    policy['profiles']['reader']['tools'] = ['platform_capabilities', 'recordings_capabilities'] + ([] if ceiling == 'profile' else ['recordings_audio'])
    actor = policy['principals'][0]
    actor['grants']['recordings'] = ['recordings:read'] if ceiling == 'gateway' else scopes
    actor['credentials']['recordings'] = {'key_file': key, 'key_sha256': hashlib.sha256(b'synthetic-recordings').hexdigest()}
    async def forward(request):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=backend), base_url='http://recordings') as c:
            return await c.post(request.url.path, content=request.content, headers=request.headers)
    gateway = create_app(private(tmp_path / 'policy.json', json.dumps(policy)), 'http://testserver',
                         transports={'recordings': httpx.MockTransport(forward)})
    assert CATALOG['recordings_audio']['scopes'] == scopes
    with TestClient(gateway) as c:
        result = rpc(c, 'tools/call', {'name': 'recordings_audio', 'arguments': {
            'account': 'plaud-a', 'recording_id': 'file-a'}}).json()['result']
        if ceiling == 'allowed':
            assert not result.get('isError'), result
            assert 'https://audio.invalid/synthetic' in json.dumps(result)
            assert len(seen) == 3
        else:
            assert result['isError'] and seen == []


# spec: ANYM-649
def test_plaud_window_limit_has_permanent_gateway_guidance():
    import asyncio
    from agent_gateway.http_clients import Clients
    from agent_gateway.policy import Denied
    async def run():
        deployment={'modules':['recordings'],'backends':{'recordings':{'url':'http://recordings'}}}
        transport=httpx.MockTransport(lambda request:httpx.Response(503,json={
            'error':'transcript_window_exceeded','private':'untrusted provider details'}))
        async with Clients(deployment,{'recordings':transport}) as clients:
            with pytest.raises(Denied) as failure:
                await clients.call('recordings','read_recording','synthetic',{})
            assert failure.value.detail=={'error':'transcript_window_exceeded','retryable':False,
                'next_action':'provider_transcript_window_limit'}
    asyncio.run(run())
