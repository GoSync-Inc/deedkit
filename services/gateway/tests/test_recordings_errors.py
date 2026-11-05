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


# spec: ANYM-753
def test_invalid_provider_response_detail_code_reaches_gateway_and_unknown_fields_are_dropped(tmp_path):
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
        return httpx.Response(200,json={'id':'conv-a','transcript_included':True,
            'transcript_segments':[{'text':'private segment','start':'private offset'}]})
    backend=create_app(private('recordings.json',json.dumps(value)),transport=httpx.MockTransport(provider))
    app,_=setup(tmp_path,backend_app=backend)
    with TestClient(app) as client:
        result=rpc(client,'tools/call',{'name':'recordings_read','arguments':{
            'account':'omi-a','recording_id':'conv-a'}}).json()['result']
        assert result['isError']
        assert result['structuredContent']=={'error':'invalid_provider_response','retryable':False,
            'next_action':'operator_check_provider_contract','detail_code':'segment_offset'}
        assert 'private' not in json.dumps(result)


# spec: ANYM-753
@pytest.mark.parametrize('body,expected',[
    ({'error':'invalid_provider_response','detail_code':'segment_offset','provider_text':'private'},'segment_offset'),
    ({'error':'invalid_provider_response','detail_code':'private value'},None),
    ({'error':'invalid_provider_response','detail_code':7},None),
    ({'error':'provider_unavailable','detail_code':'segment_offset'},None)])
def test_gateway_relays_only_allowlisted_detail_codes(body,expected):
    from agent_gateway.recordings_errors import DETAIL_CODES, failure
    assert DETAIL_CODES==frozenset({'not_object','id_invalid','id_mismatch','metadata_field','segment_shape',
        'segment_text','segment_speaker','segment_offset','list_shape','list_pagination','encoding','not_json',
        'projection_error'})
    detail=failure(body,503).detail
    assert set(detail)<={'error','retryable','next_action','retry_after_seconds','detail_code'}
    assert detail.get('detail_code')==expected and 'private' not in json.dumps(detail)


# spec: ANYM-770
@pytest.mark.parametrize('arguments,segments,detail',[
    ({'from_seconds':5,'to_seconds':5},[],'range_empty'),
    ({'kind':'summary','to_seconds':5},[],'range_not_for_summary'),
    ({'from_seconds':1},[{'text':'private segment'}],'range_needs_offsets')])
def test_range_detail_code_reaches_the_agent_through_the_gateway(tmp_path,arguments,segments,detail):
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
        return httpx.Response(200,json={'id':'conv-a','transcript_included':True,'transcript_segments':segments})
    backend=create_app(private('recordings.json',json.dumps(value)),transport=httpx.MockTransport(provider))
    app,_=setup(tmp_path,backend_app=backend)
    with TestClient(app) as client:
        result=rpc(client,'tools/call',{'name':'recordings_read','arguments':{
            'account':'omi-a','recording_id':'conv-a',**arguments}}).json()['result']
        assert result['isError']
        assert result['structuredContent']=={'error':'invalid_parameters','retryable':False,
            'next_action':'correct_arguments','detail_code':detail}
        assert 'private' not in json.dumps(result)


# spec: ANYM-770
@pytest.mark.parametrize('body,expected',[
    ({'error':'invalid_parameters','detail_code':'range_empty'},'range_empty'),
    ({'error':'invalid_parameters','detail_code':'segment_offset'},None),
    ({'error':'invalid_parameters','detail_code':'private value'},None),
    ({'error':'invalid_provider_response','detail_code':'range_empty'},None)])
def test_gateway_relays_range_codes_only_for_invalid_parameters(body,expected):
    from agent_gateway.recordings_errors import RANGE_DETAIL_CODES, failure
    assert RANGE_DETAIL_CODES==frozenset({'range_not_for_summary','range_needs_offsets','range_empty'})
    assert failure(body,422).detail.get('detail_code')==expected
