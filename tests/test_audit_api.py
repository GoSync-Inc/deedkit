"""Synthetic wire parity for event consumption and feedback, with no providers."""
import json
import pytest
from starlette.testclient import TestClient
from messaging_service.app import create_app
from anymsg import Client
import httpx
from tests.test_event_delivery import call as feedback_call, identity
from tests.test_subscription_api import RecordingStore, rpc
from messaging_service.store import APIError

CASES = [
    ('fetch_subscription_events', {'subscription_id':'synthetic-sub','limit':4}),
    ('ack_subscription_events', {'subscription_id':'synthetic-sub','receipt':'synthetic-receipt-'+'x'*24,'version':1}),
    ('configure_subscription_delivery', {'subscription_id':'synthetic-sub','version':1,'receiver_id':None}),
    ('test_subscription', {'subscription_id':'synthetic-sub','request_id':'synthetic-request'}),
    ('submit_feedback', {'request_id':'synthetic-feedback','category':'bug','description':'Synthetic issue','operation':None,'refs':None}),
    ('list_feedback', {'limit':4}),
]

class Store(RecordingStore):
    def __getattr__(self, method):
        return lambda token, **params: self._record(method,token,params)

@pytest.mark.parametrize('method,params',CASES)
def test_audit_tools_preserve_mcp_rest_and_python_payload(method,params):
    store=Store()
    with TestClient(create_app(store,['testserver'])) as c:
        rest=c.post('/v1/'+method,headers={'Authorization':'Bearer synthetic-token'},json=params)
        mcp=rpc(c,method,params)
    assert rest.status_code==200
    assert mcp.json()['result']['structuredContent']==rest.json()
    assert store.calls==[(method,params)]*2
    seen=[]
    def transport(request):
        seen.append(json.loads(request.content));return httpx.Response(200,json={'synthetic':True})
    with Client('https://archive.example','synthetic-token',transport=httpx.MockTransport(transport)) as c:
        assert getattr(c,method)(**params)=={'synthetic':True}
    assert seen==[params]

@pytest.mark.parametrize('method,params',CASES)
def test_audit_tools_reject_unknown_fields_before_backend(method,params):
    store=Store()
    with TestClient(create_app(store,['testserver'])) as c:
        rest=c.post('/v1/'+method,headers={'Authorization':'Bearer synthetic-token'},json={**params,'url':'https://private.invalid'})
        mcp=rpc(c,method,{**params,'url':'https://private.invalid'})
    assert rest.status_code==400 and mcp.json()['error']['code']==-32602
    assert store.calls==[]


def test_subscription_wait_is_available_in_both_transports_without_a_worker():
    class Waiting(Store):
        def fetch_subscription_events(self,token,subscription_id,limit=50):
            return {'items':[{'event_id':1}], 'subscription':{'state':'active'}}
    with TestClient(create_app(Waiting(),['testserver'])) as c:
        args={'subscription_id':'synthetic-sub','timeout':0,'limit':4}
        rest=c.post('/v1/wait_for_subscription_events',headers={'Authorization':'Bearer synthetic-token'},json=args)
        mcp=rpc(c,'wait_for_subscription_events',args)
    assert rest.status_code==200
    assert rest.json()['items']==mcp.json()['result']['structuredContent']['items']

@pytest.mark.parametrize('wait_s',[True,-1,26,'2'])
def test_invalid_send_wait_rejected_before_enqueue(wait_s):
    store=Store()
    args=dict(account='synthetic-account',chat_id='synthetic-chat',text='Synthetic text',request_id='synthetic-request',wait_s=wait_s)
    with TestClient(create_app(store,['testserver'])) as c:
        rest=c.post('/v1/send_message',headers={'Authorization':'Bearer synthetic-token'},json=args)
        mcp=rpc(c,'send_message',args)
    assert rest.status_code==400 and mcp.json()['error']['code']==-32602
    assert store.calls==[]

@pytest.mark.parametrize('method,args',[
    ('ack_subscription_events',{'subscription_id':'synthetic-sub','receipt':'short','version':1}),
    ('configure_subscription_delivery',{'subscription_id':'synthetic-sub','version':1,'receiver_id':'https://public.invalid'}),
    ('test_subscription',{'subscription_id':'synthetic-sub','request_id':'short'}),
    ('submit_feedback',{'request_id':'synthetic-request','category':'bug','description':'issue','refs':{'other_ids':['x']}}),
    ('submit_feedback',{'request_id':'synthetic-request','category':'bug','description':'issue','refs':{'chat_ids':['x']*33}}),
    ('submit_feedback',{'request_id':'synthetic-request','category':'bug','description':'issue','refs':{'chat_ids':['hidden\nvalue']}}),
    ('create_subscription',{'chat_ids':['synthetic-chat'],'wake_rules':['incoming_message']*5}),
    ('create_subscription',{'chat_ids':['synthetic-chat'],'reply_to_send':'x'*129}),
])
def test_new_wire_bounds_reject_before_store(method,args):
    store=Store()
    with TestClient(create_app(store,['testserver'])) as c:
        rest=c.post('/v1/'+method,headers={'Authorization':'Bearer synthetic-token'},json=args)
        mcp=rpc(c,method,args)
    assert rest.status_code==400 and mcp.json()['error']['code']==-32602
    assert store.calls==[]

def test_send_wait_failure_after_enqueue_keeps_send_id(monkeypatch):
    from messaging_service import send_wait
    from messaging_service.store import APIError
    class Queued(Store):
        def send_message(self,token,**params):
            return {'send_id':'synthetic-send','status':'queued'}
    async def busy(*args,**kwargs):
        raise APIError('send_wait_busy',503)
    monkeypatch.setattr(send_wait,'wait_for_send',busy)
    args=dict(account='synthetic-account',chat_id='synthetic-chat',text='Synthetic text',request_id='synthetic-request',wait_s=1)
    with TestClient(create_app(Queued(),['testserver'])) as c:
        rest=c.post('/v1/send_message',headers={'Authorization':'Bearer synthetic-token'},json=args)
    assert rest.status_code==200
    assert rest.json()=={'send_id':'synthetic-send','status':'queued','wait_timed_out':True,'wait_error':'send_wait_busy'}


def submitted(store, token, request_id, operation):
    return feedback_call(store, token, 'submit_feedback', request_id, 'bug', 'Synthetic report', operation=operation)['feedback_id']


# spec: ANYM-342
def test_feedback_operation_accepts_gateway_prefix(seeded):
    store, _, _ = seeded
    token = identity(store)
    first = submitted(store, token, 'synthetic-prefixed', 'messages_search')
    # The other spelling of the same name is the same request, not a conflict.
    assert submitted(store, token, 'synthetic-prefixed', 'search') == first
    assert submitted(store, token, 'synthetic-bare', 'search') != first
    assert [item['operation'] for item in feedback_call(store, token, 'list_feedback')['items']] == ['search', 'search']


# spec: ANYM-342
def test_feedback_operation_other_modules(seeded):
    store, _, _ = seeded
    token = identity(store)
    for index, name in enumerate(('calendar_slots', 'calls_get_call_status', 'platform_capabilities')):
        submitted(store, token, 'synthetic-module-%d' % index, name)
    stored = {item['operation'] for item in feedback_call(store, token, 'list_feedback')['items']}
    assert stored == {'calendar_slots', 'calls_get_call_status', 'platform_capabilities'}
    for index, name in enumerate(('messages_nonexistent', 'Search', 'calendar_', 'messages_', 'calls_Dial', 'calls_' + 'a' * 121)):
        with pytest.raises(APIError, match='invalid_feedback'):
            submitted(store, token, 'synthetic-bad-%d' % index, name)


# spec: ANYM-342
def test_feedback_operation_unknown_field_error(seeded):
    store, _, _ = seeded
    token = identity(store)
    with pytest.raises(APIError, match='invalid_feedback') as failure:
        submitted(store, token, 'synthetic-unknown', 'messages_nonexistent-canary')
    assert failure.value.field_errors == [{'field': 'operation', 'reason': 'enum',
        'expected': 'a tool name from tools/list, with or without the messages_ prefix'}]
    assert 'canary' not in json.dumps(failure.value.field_errors)
