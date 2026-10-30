"""Actual Omi failures remain actionable through the gateway, without provider text."""
import hashlib
import json
from pathlib import Path
import sys

import httpx
import pytest
from starlette.testclient import TestClient

from test_recordings import setup
from test_transport import rpc

sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'services/recordings/src'))


# spec: ANYM-648
@pytest.mark.parametrize('failure,code,action',[('large','response_too_large','source_exceeds_read_limit'),
    ('auth','provider_auth_required','owner_reauthorize_provider')])
def test_omi_permanent_backend_failures_reach_gateway_without_transient_retry(tmp_path,failure,code,action):
    from deedkit_recordings.app import create_app
    def private(name,text):
        path=tmp_path/name
        path.write_text(text)
        path.chmod(0o600)
        return str(path)
    value={'version':1,'cursor_secret_file':private('secret','x'*64),
      'accounts':[{'id':'omi-a','provider':'omi','label':'Synthetic','token_file':private('token','synthetic-omi-token')}],
      'principals':[{'id':'a','key_sha256':hashlib.sha256(b'synthetic-recordings').hexdigest(),
        'expires_at':'2099-01-01T00:00:00Z','accounts':['omi-a'],
        'permissions':['recordings:read','recordings:content']}]}
    def provider(request):
        return httpx.Response(401,text='private failure') if failure=='auth' else httpx.Response(200,content=b'x'*5000)
    backend=create_app(private('recordings.json',json.dumps(value)),transport=httpx.MockTransport(provider),provider_bytes=1024)
    app,_=setup(tmp_path,backend_app=backend)
    with TestClient(app) as client:
        result=rpc(client,'tools/call',{'name':'recordings_read','arguments':{
            'account':'omi-a','recording_id':'conv-a'}}).json()['result']
        assert result['isError']
        payload=result['structuredContent']
        assert payload['error']==code and payload['retryable'] is False
        assert payload['next_action']==action and 'retry_after_seconds' not in payload
        assert 'private failure' not in json.dumps(result)
