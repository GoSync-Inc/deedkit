"""Synthetic subscription transport/SDK contracts and annotation pins; only the fetch bookkeeping test uses a database."""
import json
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from anymsg import AnyMsgError, Client
from messaging_service.app import create_app
from messaging_service.store import APIError


METHODS=('create_subscription','list_subscriptions','get_subscription',
    'update_subscription','get_subscription_events')


class RecordingStore:
    public_url='http://testserver'

    def __init__(self,error=None):self.calls=[];self.error=error
    def authenticate(self,token):return 'synthetic-principal'
    def audit_boundary(self,code):pass
    def _record(self,method,token,params):
        assert token=='synthetic-token'
        self.calls.append((method,params))
        if self.error:raise APIError(self.error,409)
        return {'synthetic':True,'method':method,'parameters':params}
    def create_subscription(self,token,**params):return self._record(METHODS[0],token,params)
    def list_subscriptions(self,token,**params):return self._record(METHODS[1],token,params)
    def get_subscription(self,token,**params):return self._record(METHODS[2],token,params)
    def update_subscription(self,token,**params):return self._record(METHODS[3],token,params)
    def get_subscription_events(self,token,**params):return self._record(METHODS[4],token,params)


def arguments(method):
    if method==METHODS[0]:return {'chat_ids':['chat_synthetic'], 'sender_ids':['sender_synthetic'],
        'kinds':['message.created','file.transcript.ready'],'direction':'incoming',
        'origins':['live','reconcile'],'label':'synthetic source set',
        'expires_at':'2030-01-01T00:00:00Z'}
    if method==METHODS[1]:return {'limit':4}
    params={'subscription_id':'sub_synthetic'}
    if method==METHODS[3]:params.update(version=2,action='resume',resume_policy='catch_up')
    if method==METHODS[4]:params.update(after=17,limit=4)
    return params


def rpc(client,method,params):
    return client.post('/mcp',headers={'Authorization':'Bearer synthetic-token',
        'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,
        'method':'tools/call','params':{'name':method,'arguments':params}})


@pytest.mark.parametrize('method',METHODS)
def test_subscription_rest_and_mcp_preserve_exact_payload(method):
    store=RecordingStore();params=arguments(method)
    with TestClient(create_app(store,['testserver'])) as client:
        rest=client.post('/v1/'+method,headers={'Authorization':'Bearer synthetic-token'},json=params)
        mcp=rpc(client,method,params)
    assert rest.status_code==200
    assert not mcp.json().get('error') and not mcp.json()['result'].get('isError')
    assert rest.json()==mcp.json()['result']['structuredContent']
    assert store.calls==[(method,params),(method,params)]


@pytest.mark.parametrize('method',METHODS)
def test_subscription_unknown_arguments_rejected_before_backend(method):
    store=RecordingStore();params={**arguments(method),'task_id':'private-unverified-task'}
    with TestClient(create_app(store,['testserver'])) as client:
        rest=client.post('/v1/'+method,headers={'Authorization':'Bearer synthetic-token'},json=params)
        mcp=rpc(client,method,params)
    assert rest.status_code==400 and rest.json()['fields']==['task_id']
    assert mcp.json()['error']['code']==-32602 and mcp.json()['error']['data']['fields']==['task_id']
    assert 'private-unverified-task' not in rest.text+mcp.text
    assert store.calls==[]


@pytest.mark.parametrize('method,field',[
    (METHODS[0],'chat_ids'),(METHODS[2],'subscription_id'),
    (METHODS[3],'subscription_id'),(METHODS[3],'version'),(METHODS[3],'action'),
    (METHODS[4],'subscription_id'),
])
def test_subscription_required_fields_rejected_before_backend(method,field):
    store=RecordingStore();params=arguments(method);del params[field]
    with TestClient(create_app(store,['testserver'])) as client:
        rest=client.post('/v1/'+method,headers={'Authorization':'Bearer synthetic-token'},json=params)
        mcp=rpc(client,method,params)
    assert rest.status_code==400 and rest.json()['fields']==[field]
    assert mcp.json()['error']['data']['fields']==[field]
    assert store.calls==[]


INVALID=[
    (METHODS[0],{'chat_ids':None}),(METHODS[0],{'chat_ids':[]}),(METHODS[0],{'chat_ids':['x']*65}),
    (METHODS[0],{'chat_ids':[1]}),(METHODS[0],{'chat_ids':['bad\nreference']}),
    (METHODS[0],{'sender_ids':[]}),(METHODS[0],{'sender_ids':True}),
    (METHODS[0],{'kinds':['private-kind']}),(METHODS[0],{'kinds':[]}),
    (METHODS[0],{'origins':['unknown']}),(METHODS[0],{'origins':[]}),
    (METHODS[0],{'direction':False}),(METHODS[0],{'label':'x'*129}),
    (METHODS[0],{'label':'private\ncontrol'}),
    (METHODS[0],{'expires_at':'2030-01-01T00:00:00'}),
    (METHODS[0],{'expires_at':'2030-02-30T00:00:00Z'}),
    (METHODS[0],{'expires_at':True}),
    (METHODS[1],{'limit':0}),(METHODS[1],{'limit':101}),(METHODS[1],{'limit':True}),
    (METHODS[2],{'subscription_id':''}),(METHODS[2],{'subscription_id':1}),
    (METHODS[3],{'version':True}),(METHODS[3],{'version':'2'}),(METHODS[3],{'version':0}),
    (METHODS[3],{'action':'change_sources'}),(METHODS[3],{'resume_policy':'unbounded'}),
    (METHODS[4],{'after':True}),(METHODS[4],{'after':-1}),
    (METHODS[4],{'after':9007199254740992}),(METHODS[4],{'limit':101}),
]


@pytest.mark.parametrize('method,extra',INVALID)
def test_subscription_strict_bounds_before_backend(method,extra):
    store=RecordingStore();params={**arguments(method),**extra}
    with TestClient(create_app(store,['testserver'])) as client:
        rest=client.post('/v1/'+method,headers={'Authorization':'Bearer synthetic-token'},json=params)
        mcp=rpc(client,method,params)
    assert rest.status_code==400 and rest.json()['error']=='invalid_parameters'
    assert mcp.json()['error']['code']==-32602
    assert store.calls==[]


# spec: ANYM-733
def test_live_recovered_is_an_accepted_subscription_origin_on_rest_and_mcp():
    store=RecordingStore();origins=['live','live_recovered','backfill','reconcile','fixture']
    params={**arguments(METHODS[0]),'origins':origins}
    with TestClient(create_app(store,['testserver'])) as client:
        rest=client.post('/v1/'+METHODS[0],headers={'Authorization':'Bearer synthetic-token'},json=params)
        mcp=rpc(client,METHODS[0],params)
    assert rest.status_code==200 and not mcp.json()['result']['isError']
    assert [call[1]['origins'] for call in store.calls]==[origins,origins]


def test_subscription_defaults_do_not_enable_historical_replay_or_delivery():
    store=RecordingStore()
    with TestClient(create_app(store,['testserver'])) as client:
        assert not rpc(client,METHODS[0],{'chat_ids':['chat_synthetic']}).json()['result']['isError']
    assert store.calls==[(METHODS[0],dict(chat_ids=['chat_synthetic'],sender_ids=None,kinds=None,
        direction='incoming',origins=None,label='',expires_at=None))]


def test_subscription_manifest_has_strict_schemas_and_passive_event_read():
    store=RecordingStore()
    with TestClient(create_app(store,['testserver'])) as client:
        listed=client.post('/mcp',headers={'Authorization':'Bearer synthetic-token',
            'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,
            'method':'tools/list','params':{}}).json()['result']['tools']
    tools={tool['name']:tool for tool in listed}
    assert len(tools)==54
    for method in METHODS:
        tool=tools[method]
        assert tool['inputSchema']['additionalProperties'] is False
        mutation=method in (METHODS[0],METHODS[3])
        assert tool['annotations']['readOnlyHint'] is not mutation
        assert tool['annotations']['openWorldHint'] is False
        assert tool['annotations']['destructiveHint'] is False
    legacy=tools['get_updates']['inputSchema']['properties']['kinds']
    assert 'file.transcript.ready' not in json.dumps(legacy)
    assert 'file.transcript.ready' in json.dumps(tools[METHODS[0]]['inputSchema'])
    assert 'ack_subscription_events' in tools


def test_subscription_backend_conflict_returns_safe_actionable_errors():
    store=RecordingStore('subscription_conflict')
    with TestClient(create_app(store,['testserver'])) as client:
        rest=client.post('/v1/'+METHODS[3],headers={'Authorization':'Bearer synthetic-token'},
            json=arguments(METHODS[3]))
        mcp=rpc(client,METHODS[3],arguments(METHODS[3]))
    assert rest.status_code==409 and rest.json()['error']=='subscription_conflict'
    assert mcp.json()['result']['isError'] is True
    assert 'subscription_conflict' in mcp.text


# spec: ANYM-825
def test_chat_types_values_reach_the_store_so_it_answers_invalid_filter():
    store=RecordingStore()
    # The schema takes any list of strings: the store checks values, count, duplicates and the channel rule
    # and answers invalid_filter (tests/test_subscriptions.py), over REST and MCP alike.
    lists=[['private','unknown'],['bot'],[],['private']*5,['private','private']]
    crossed={'all_visible_chats':True,'include_channels':True,'chat_types':['private']}
    sent=[{'all_visible_chats':True,'chat_types':chat_types} for chat_types in lists]+[crossed]
    with TestClient(create_app(store,['testserver'])) as client:
        for sources in sent:
            assert client.post('/v1/create_subscription',headers={'Authorization':'Bearer synthetic-token'},
                json={'sources':sources}).status_code==200
            assert mcp_result(rpc(client,'create_subscription',{'sources':sources})).get('isError') is not True
        assert [params['sources'] for _,params in store.calls]==[sources for sources in sent for _ in range(2)]
        store.calls.clear()
        # Only a value that is not a list of strings stops at the schema.
        for chat_types in ('private',[1]):
            params={'sources':{'all_visible_chats':True,'chat_types':chat_types}}
            rest=client.post('/v1/create_subscription',headers={'Authorization':'Bearer synthetic-token'},json=params)
            assert rest.status_code==400 and rest.json()['error']=='invalid_parameters'
            assert rpc(client,'create_subscription',params).json()['error']['code']==-32602
    assert store.calls==[]


def mcp_result(response):
    return response.json()['result']


@pytest.mark.parametrize('method',METHODS)
def test_python_subscription_helpers_preserve_exact_parameters(method):
    seen=[]
    def transport(request):
        seen.append((request.url.path,json.loads(request.content)))
        return httpx.Response(200,json={'synthetic':True})
    with Client('https://archive.example/base','synthetic-token',transport=httpx.MockTransport(transport)) as client:
        assert getattr(client,method)(**arguments(method))=={'synthetic':True}
    assert seen==[('/base/v1/'+method,arguments(method))]


@pytest.mark.parametrize('method,extra',INVALID)
def test_python_subscription_bounds_prevent_transport(method,extra):
    seen=[]
    with Client('https://archive.example','synthetic-token',
            transport=httpx.MockTransport(lambda request:seen.append(request))) as client:
        with pytest.raises(ValueError,match='invalid_parameters'):
            client.call(method,{**arguments(method),**extra})
    assert seen==[]


@pytest.mark.parametrize('method',METHODS)
def test_python_subscription_unknown_fields_prevent_transport(method):
    seen=[]
    with Client('https://archive.example','synthetic-token',
            transport=httpx.MockTransport(lambda request:seen.append(request))) as client:
        with pytest.raises(ValueError,match='invalid_parameters'):
            client.call(method,{**arguments(method),'task_id':'unverified'})
    assert seen==[]


@pytest.mark.parametrize('code',['subscription_conflict','subscription_limit','subscription_closed',
    'subscription_access_changed','invalid_subscription_state','invalid_subscription'])
def test_python_subscription_errors_are_sanitized_without_retry(code):
    seen=[]
    def transport(request):
        seen.append(request)
        return httpx.Response(409,json={'error':code,'detail':'synthetic-private-source'})
    with Client('https://archive.example','synthetic-token',transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(AnyMsgError) as error:client.create_subscription(chat_ids=['chat_synthetic'])
    assert error.value.code==code and 'synthetic-private-source' not in str(error.value)
    assert len(seen)==1


def test_python_uncertain_subscription_creation_never_retries():
    seen=[]
    def transport(request):
        seen.append(request);raise httpx.ReadTimeout('synthetic-private-diagnostic')
    with Client('https://archive.example','synthetic-token',transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(AnyMsgError,match='request_failed'):
            client.create_subscription(chat_ids=['chat_synthetic'])
    assert len(seen)==1


READ_ONLY_TOOLS=('list_subscriptions','get_subscription','get_subscription_events',
    'fetch_subscription_events','wait_for_subscription_events','get_attachment')


def consumption_state(store,sid):
    with store.pool.connection() as c:
        subscription=c.execute('''SELECT version,state,updated_at,matched_through,expires_at
            FROM subscriptions WHERE id=%s''',(sid,)).fetchone()
        consumption=c.execute('''SELECT acked_through,version,updated_at
            FROM subscription_consumption WHERE subscription=%s''',(sid,)).fetchone()
        receipts=c.execute('SELECT count(*) AS n FROM subscription_receipts WHERE subscription=%s',(sid,)).fetchone()['n']
    return subscription,consumption,receipts


# spec: ANYM-257
def test_fetch_is_read_only_for_consumption(seeded):
    from messaging_service.subscriptions import match_subscriptions
    from tests.test_event_delivery import call,emit,identity,subscription
    store,_,_=seeded
    acking=identity(store,'acking-reader')
    plain_permissions=['read','subscription-write']
    plain=store.issue_key('plain-reader',['telegram-demo'],permissions=plain_permissions,
        key_permissions=plain_permissions)
    acking_sid,plain_sid=subscription(store,acking),subscription(store,plain)
    first,second=emit(store),emit(store)
    match_subscriptions(store)
    empty_sid=subscription(store,acking)
    # An empty page never gets a receipt, even for a key that may ACK.
    empty=call(store,acking,'fetch_subscription_events',empty_sid)
    assert empty['items']==[] and empty['receipt'] is None
    empty_before=consumption_state(store,empty_sid)

    # Without subscription-ack: pages are returned, no receipt row is written and nothing is consumed.
    page=call(store,plain,'fetch_subscription_events',plain_sid)
    assert [e['event_id'] for e in page['items']]==[first,second]
    assert page['receipt'] is None and page['ack_available'] is False
    baseline=consumption_state(store,plain_sid)
    assert baseline[1]['version']==1 and baseline[2]==0
    for _ in range(3):
        again=call(store,plain,'fetch_subscription_events',plain_sid)
        assert again['items']==page['items'] and again['receipt'] is None
    assert consumption_state(store,plain_sid)==baseline

    # With subscription-ack: exactly one receipt per non-empty page, a retry sees the same events from the
    # same position, and no fetch moves acked_through, the ACK version or the subscription version.
    page=call(store,acking,'fetch_subscription_events',acking_sid,limit=1)
    assert [e['event_id'] for e in page['items']]==[first] and page['receipt'] and page['ack_available']
    acking_baseline=consumption_state(store,acking_sid)
    assert acking_baseline[2]==1
    retry=call(store,acking,'fetch_subscription_events',acking_sid,limit=1)
    assert retry['items']==page['items'] and retry['acked_through']==page['acked_through']
    assert retry['ack_version']==page['ack_version'] and retry['next_after']==page['next_after']
    assert retry['receipt']!=page['receipt']
    after=consumption_state(store,acking_sid)
    assert after[0]==acking_baseline[0] and after[1]==acking_baseline[1] and after[2]==2
    for _ in range(130):
        call(store,acking,'fetch_subscription_events',acking_sid)
    final=consumption_state(store,acking_sid)
    assert final[0]==acking_baseline[0] and final[1]==acking_baseline[1] and final[2]==128

    # Neither the other key's subscription nor the event-less one changed.
    assert consumption_state(store,plain_sid)==baseline
    assert consumption_state(store,empty_sid)==empty_before
    assert empty_before[2]==0 and empty_before[1]['version']==1


# spec: ANYM-257
def test_state_writing_reads_stay_read_only_in_backend_gateway_and_client():
    root=Path(__file__).resolve().parents[1]
    catalog={row['name']:row for row in json.loads(
        (root/'services/gateway/src/agent_gateway/catalog.json').read_text())}
    client={row['name']:row['read_only'] for row in json.loads(
        (root/'packages/deedkit/lib/tools.json').read_text())}
    with TestClient(create_app(RecordingStore(),['testserver'])) as http:
        listed=http.post('/mcp',headers={'Authorization':'Bearer synthetic-token',
            'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,
            'method':'tools/list','params':{}}).json()['result']['tools']
    backend={tool['name']:tool for tool in listed}
    read={'readOnlyHint':True,'destructiveHint':False,'openWorldHint':False}
    for name in READ_ONLY_TOOLS:
        assert backend[name]['annotations']==read,name
        assert catalog['messages_'+name]['annotations']==backend[name]['annotations'],name
        assert client['messages_'+name] is True,name
    for name in ('create_subscription','update_subscription','ack_subscription_events',
            'configure_subscription_delivery'):
        assert backend[name]['annotations']['readOnlyHint'] is False,name
        assert catalog['messages_'+name]['annotations']==backend[name]['annotations'],name
        assert client['messages_'+name] is False,name


# spec: ANYM-257
def test_receipt_issuing_reads_say_so_and_gateway_text_equals_backend():
    root=Path(__file__).resolve().parents[1]
    catalog={row['name']:row for row in json.loads(
        (root/'services/gateway/src/agent_gateway/catalog.json').read_text())}
    with TestClient(create_app(RecordingStore(),['testserver'])) as http:
        listed=http.post('/mcp',headers={'Authorization':'Bearer synthetic-token',
            'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,
            'method':'tools/list','params':{}}).json()['result']['tools']
    backend={tool['name']:tool for tool in listed}
    for name in ('fetch_subscription_events','wait_for_subscription_events'):
        text=backend[name]['description']
        assert catalog['messages_'+name]['description']==text,name
        for phrase in ('short-lived receipt','safe to retry','oldest receipt','128','fails closed'):
            assert phrase in text.lower(),(name,phrase)


# spec: ANYM-518
@pytest.mark.parametrize('transport',['rest','mcp'])
def test_subscriber_waits_fetches_and_acks_over_rest_and_mcp(seeded,transport):
    from messaging_service.subscriptions import match_subscriptions
    from tests.test_capabilities_summary import structured
    from tests.test_client_contract import rpc as tool_call
    from tests.test_event_delivery import emit,identity
    store,_,_=seeded
    key=identity(store,'subscriber-'+transport)
    with TestClient(create_app(store,['testserver'])) as client:
        def tool(name,**arguments):
            if transport=='mcp':return structured(tool_call(client,key,name,arguments))
            response=client.post('/v1/'+name,headers={'Authorization':'Bearer '+key},json=arguments)
            assert response.status_code==200,response.text
            return response.json()
        sid=tool('create_subscription',chat_ids=['telegram-demo-chat'])['subscription_id']
        event=emit(store);match_subscriptions(store)
        assert tool('wait_for_subscription_events',subscription_id=sid,timeout=1)['wait_status']=='events_available'
        page=tool('fetch_subscription_events',subscription_id=sid)
        assert [e['event_id'] for e in page['items']]==[event] and page['receipt']
        ack=tool('ack_subscription_events',subscription_id=sid,receipt=page['receipt'],version=page['ack_version'])
        assert ack['acked_through']>page['acked_through']
        again=tool('fetch_subscription_events',subscription_id=sid)
        assert again['items']==[] and again['receipt'] is None
        assert tool('get_subscription',subscription_id=sid)['acked_through']==ack['acked_through']


COMPACT_PAGE_SUBSCRIPTION={'subscription_id','state','version','label','matched_through','matcher_caught_up',
    'acked_through','blocked_reason','gap','scope_masked'}


def page_store_call(store,token,method,sid,**kwargs):
    if method=='wait_for_subscription_events':
        import asyncio
        from messaging_service.subscription_consumption import wait_for_subscription_events
        return asyncio.run(wait_for_subscription_events(store,token,sid,timeout=0,**kwargs))
    return getattr(store,method)(token,sid,**kwargs)


def receipt_range(store,receipt):
    import hashlib
    with store.pool.connection() as c:
        row=c.execute('''SELECT after_position,through_position,module_after_position,module_through_position
            FROM subscription_receipts WHERE receipt_hash=%s''',(hashlib.sha256(receipt.encode()).hexdigest(),)).fetchone()
    return dict(row)


def static_case(store):
    from tests.test_event_delivery import identity
    from tests.test_subscriptions import emit
    token=identity(store,'compact-static')
    sid=store.create_subscription(token,['telegram-demo-chat'])['subscription_id']
    emit(store);emit(store)
    return token,sid


def dynamic_case(store):
    from messaging_service.subscriptions import match_subscriptions
    from tests.test_subscriptions import ALL_VISIBLE,chat_message,dynamic_key,emit
    store.set_dynamic_subscriptions(True)
    token=dynamic_key(store,'compact-dynamic')
    chat_message(store,'telegram-demo-chat-826','demo-tg-826')
    sid=store.create_subscription(token,sources=ALL_VISIBLE)['subscription_id']
    emit(store);emit(store,'demo-tg-826')
    match_subscriptions(store)
    # The second chat leaves the key's scope: its reference is hidden above the visible one.
    store.set_access('compact-dynamic',chats=['telegram-demo-chat'])
    return token,sid


def resumed_case(store):
    from tests.test_event_delivery import identity
    from tests.test_subscriptions import emit
    token=identity(store,'compact-resumed')
    sid=store.create_subscription(token,['telegram-demo-chat'])['subscription_id']
    emit(store)
    paused=store.update_subscription(token,sid,1,action='pause')
    emit(store)
    store.update_subscription(token,sid,paused['version'],action='resume')
    return token,sid


def module_case(store):
    from tests.test_module_events import beat,call_event,mixed,publish,subscriber
    from tests.test_subscriptions import emit
    token=subscriber(store)
    sid=mixed(store,token)
    beat(store)
    publish(store,call_event(1,'call-826'))
    emit(store)
    return token,sid


# spec: ANYM-826
@pytest.mark.parametrize('case',['static','dynamic_hidden','paused_resumed','module'])
def test_compact_and_full_fetches_page_receipt_and_ack_the_same_range(seeded,case):
    from messaging_service.subscriptions import match_subscriptions
    store,_,_=seeded
    try:
        token,sid={'static':static_case,'dynamic_hidden':dynamic_case,'paused_resumed':resumed_case,
            'module':module_case}[case](store)
        match_subscriptions(store)
        full=store.fetch_subscription_events(token,sid,limit=50)
        compact=store.fetch_subscription_events(token,sid,limit=50,detail='compact')
        assert full['items'] or full.get('module',{}).get('items')
        assert [item['event_id'] for item in compact['items']]==[item['event_id'] for item in full['items']]
        for field in ('next_after','has_more','ack_version','acked_through','ack_available','consumption'):
            assert compact[field]==full[field],field
        assert compact.get('module')==full.get('module')
        if case=='dynamic_hidden':
            assert len(full['items'])==1 and full['next_after']>full['items'][-1]['event_id']
        assert full['receipt'] and compact['receipt'] and compact['receipt']!=full['receipt']
        assert receipt_range(store,compact['receipt'])==receipt_range(store,full['receipt'])
        ack=store.ack_subscription_events(token,sid,compact['receipt'],compact['ack_version'])
        assert ack['acked_through']==full['next_after'] and ack['ack_version']==full['ack_version']+1
        if case=='module':
            assert ack['module_acked_through']==full['module']['next_after']
        # The routing a host acts on is the same in both views.
        assert [item['wake_reason'] for item in compact['items']]==[item['wake_reason'] for item in full['items']]
    finally:
        store.set_dynamic_subscriptions(False)


# spec: ANYM-826
@pytest.mark.parametrize('method',['get_subscription_events','fetch_subscription_events','wait_for_subscription_events'])
def test_compact_page_keeps_page_fields_and_only_the_named_blocks(seeded,method):
    from messaging_service.subscription_consumption import COMPACT_ITEM
    from messaging_service.subscriptions import match_subscriptions
    store,_,_=seeded
    token,sid=static_case(store)
    match_subscriptions(store)
    full=page_store_call(store,token,method,sid)
    compact=page_store_call(store,token,method,sid,detail='compact')
    assert set(compact)==set(full)
    assert set(compact['subscription'])==COMPACT_PAGE_SUBSCRIPTION
    for omitted in ('chat_ids','delivery','freshness_basis','matching_evidence','matched_event_count'):
        assert omitted in full['subscription'],omitted
        assert omitted not in compact['subscription'],omitted
    assert compact['items'] and all(set(item)<=set(COMPACT_ITEM)|{'metadata','reply_evidence'}
        for item in compact['items'])
    assert all('matching_basis' in item for item in full['items'])
    # include re-adds the blocks; with detail=full it changes nothing.
    added=page_store_call(store,token,method,sid,detail='compact',include=['subscription','metadata'])
    assert added['subscription']==full['subscription'] and [i.get('metadata') for i in added['items']]==[
        i.get('metadata') for i in full['items']]
    delivery=page_store_call(store,token,method,sid,detail='compact',include=['delivery'])
    assert delivery['subscription']['delivery']==full['subscription']['delivery']
    same=page_store_call(store,token,method,sid,detail='full',include=['delivery'])
    for name in ('subscription','items','next_after','has_more'):
        assert same[name]==full[name],name
    for bad in ({'detail':'brief'},{'include':['selector']},{'include':'metadata'},{'detail':True}):
        with pytest.raises(APIError) as error:
            page_store_call(store,token,method,sid,**bad)
        assert error.value.code=='invalid_parameters'


# spec: ANYM-826
def test_empty_compact_page_is_the_state_block_and_cursors(seeded):
    from tests.test_event_delivery import identity
    store,_,_=seeded
    token=identity(store,'compact-empty')
    sid=store.create_subscription(token,['telegram-demo-chat'])['subscription_id']
    page=store.fetch_subscription_events(token,sid,detail='compact')
    assert page['items']==[] and page['receipt'] is None
    assert set(page)=={'subscription','items','has_more','next_after','consumption','scope_masked','ack_version',
        'receipt','acked_through','receipt_expires_at','ack_available'}
    assert set(page['subscription'])==COMPACT_PAGE_SUBSCRIPTION
    passive=store.get_subscription_events(token,sid,detail='compact')
    assert set(passive)=={'subscription','items','has_more','next_after','consumption','scope_masked'}


# spec: ANYM-826
def test_compact_page_of_fifty_forwarded_media_items_is_smaller(seeded):
    from messaging_service.subscriptions import match_subscriptions
    from tests.test_event_delivery import identity
    from tests.test_subscriptions import chat_message,emit
    store,_,_=seeded
    token=identity(store,'compact-bytes')
    sid=store.create_subscription(token,['telegram-demo-chat'])['subscription_id']
    for n in range(50):
        message=chat_message(store,'telegram-demo-chat',f'demo-tg-fwd-{n}',metadata={
            'forward_origin':{'kind':'user','sender_id':f'synthetic-{n}','sender_name':'Synthetic Author',
                'sent_at':'2026-01-01T00:00:00Z'},
            'media':{'kind':'photo','width':1280,'height':720,'mime_type':'image/jpeg','size_bytes':123456},
            'mentions_owner':False})
        emit(store,message)
    match_subscriptions(store)
    full=store.fetch_subscription_events(token,sid,limit=50)
    compact=store.fetch_subscription_events(token,sid,limit=50,detail='compact')
    assert len(full['items'])==len(compact['items'])==50
    assert compact['items'][0]['metadata']=={'mentions_owner':False,'forward_origin':{'kind':'user'}}
    full_bytes,compact_bytes=(len(json.dumps(page,separators=(',',':'),default=str).encode())
        for page in (full,compact))
    print(f'ANYM-826 page bytes: full={full_bytes} compact={compact_bytes}')
    assert compact_bytes<full_bytes


def tracked_keys(function,item,*args):
    seen=set()

    class Tracked(dict):
        def get(self,key,default=None):
            seen.add(key)
            return super().get(key,default)

        def __getitem__(self,key):
            seen.add(key)
            return super().__getitem__(key)

    return function({**item,'metadata':Tracked(item.get('metadata') or {}),
        'reply_evidence':Tracked(item.get('reply_evidence') or {})},*args),seen


# spec: ANYM-826
@pytest.mark.parametrize('metadata,proof',[
    ({'mentions_owner':True,'reply_to_owner':True,'reply_to_provider_id':'p-1','sender_is_bot':True,
        'forward_origin':{'kind':'channel','chat_id':'c-1'},'media':{'kind':'photo'}},{}),
    ({'reply_to_owner':False,'reply_to_provider_id':'n-1','unread':True},
        {'basis':'exact_native_reply_to_archived_outgoing','target_provider_id':'p-2','excerpt':'x'}),
    ({},{}),
])
def test_wake_reason_and_dynamic_match_read_only_compact_keys(metadata,proof):
    import inspect
    import re
    from messaging_service import subscription_consumption as consumption, subscriptions
    from messaging_service.subscription_consumption import COMPACT_METADATA,COMPACT_REPLY_EVIDENCE,project_page
    item={'event_id':1,'kind':'message.created','direction':'incoming','account':'a','chat':'c','historical':False,
        'metadata':metadata,'reply_evidence':proof,'matching_basis':'exact_event_revision'}
    selector={'reply_anchor':{'account':'a','chat':'c','provider_id':'p-1','native_reply_id':'n-1'}}
    compact=project_page({'items':[item]},'compact')['items'][0]
    assert 'matching_basis' not in compact
    full_reason,full_seen=tracked_keys(consumption.wake_reason,item,selector)
    compact_reason,_=tracked_keys(consumption.wake_reason,compact,selector)
    assert compact_reason==full_reason
    assert full_seen<=set(COMPACT_METADATA)|set(COMPACT_REPLY_EVIDENCE)
    for sources in ({'include_bots':False},{'include_bots':True}):
        assert subscriptions._dynamic_match(sources,'group',compact)==subscriptions._dynamic_match(sources,'group',item)
    _,dynamic_seen=tracked_keys(lambda snapshot:subscriptions._dynamic_match({'include_bots':False},'group',snapshot),item)
    assert dynamic_seen<=set(COMPACT_METADATA)
    # Every metadata or reply-evidence key a server reader names is on the allow-list.
    for reader in (consumption.wake_reason,subscriptions._dynamic_match,subscriptions._own_send_replies):
        names=set(re.findall(r"(?:metadata|proof|get\('metadata'\) or \{\}\))\.get\('([a-z_]+)'",inspect.getsource(reader)))
        assert names and names<=set(COMPACT_METADATA)|set(COMPACT_REPLY_EVIDENCE),(reader.__name__,names)


# spec: ANYM-826
def test_compact_subscription_keeps_only_the_delivery_health_state():
    from messaging_service.subscription_consumption import project_page
    health={'state':'failing','consecutive_failures':3,'last_success':None,'last_error':'timeout','next_step':'x'}
    state={'subscription_id':'s','state':'active','version':2,'label':None,'matched_through':5,'matcher_caught_up':True,
        'acked_through':4,'blocked_reason':None,'gap':None,'scope_masked':False,'chat_ids':['c'],
        'freshness_basis':'committed_archive_journal_not_provider_freshness','matched_event_count':1,
        'delivery':{'push':True,'delivery_health':health}}
    page={'subscription':state,'items':[],'has_more':False,'next_after':4,'positions':{'synthetic':True}}
    compact=project_page(page,'compact')
    assert compact['subscription']=={**{key:state[key] for key in COMPACT_PAGE_SUBSCRIPTION},
        'delivery_health':{'state':'failing'}}
    assert compact['positions']==page['positions'] and compact['next_after']==4
    assert project_page(page,'full',frozenset({'delivery'})) is page


# spec: ANYM-826
@pytest.mark.parametrize('method',['get_subscription_events','fetch_subscription_events','wait_for_subscription_events'])
def test_detail_and_include_reach_the_backend_only_when_given(method,monkeypatch):
    seen=[]

    class PageStore(RecordingStore):
        def get_subscription_events(self,token,**params):
            seen.append(params)
            return {'items':[]}

        def fetch_subscription_events(self,token,**params):
            seen.append(params)
            return {'items':[]}

    async def fake_wait(store,token,subscription_id,**kwargs):
        seen.append({'subscription_id':subscription_id,**kwargs})
        return {'items':[]}

    import messaging_service.subscription_consumption as consumption
    monkeypatch.setattr(consumption,'wait_for_subscription_events',fake_wait)
    params={'subscription_id':'sub_synthetic'}
    with TestClient(create_app(PageStore(),['testserver'])) as client:
        headers={'Authorization':'Bearer synthetic-token'}
        plain=client.post('/v1/'+method,headers=headers,json=params)
        compact=client.post('/v1/'+method,headers=headers,json={**params,'detail':'compact','include':['delivery']})
        over_mcp=rpc(client,method,{**params,'detail':'compact'})
        wrong=client.post('/v1/'+method,headers=headers,json={**params,'detail':'brief'})
        wrong_include=rpc(client,method,{**params,'include':['selector']})
    assert plain.status_code==200 and compact.status_code==200 and not over_mcp.json().get('error')
    assert 'detail' not in seen[0] and 'include' not in seen[0]
    assert seen[1]['detail']=='compact' and seen[1]['include']==['delivery'] and seen[2]['detail']=='compact'
    assert wrong.status_code==400 and wrong.json()['error']=='invalid_parameters'
    assert wrong_include.json()['error']['code']==-32602 and len(seen)==3


# spec: ANYM-826
def test_page_detail_is_described_alike_in_backend_gateway_and_clients():
    root=Path(__file__).resolve().parents[1]
    catalog={row['name']:row for row in json.loads(
        (root/'services/gateway/src/agent_gateway/catalog.json').read_text())}
    with TestClient(create_app(RecordingStore(),['testserver'])) as http:
        listed=http.post('/mcp',headers={'Authorization':'Bearer synthetic-token',
            'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,
            'method':'tools/list','params':{}}).json()['result']['tools']
    backend={tool['name']:tool for tool in listed}
    sent=[]
    def transport(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200,json={'synthetic':True})
    python=Client('https://archive.example','synthetic-token',transport=httpx.MockTransport(transport))
    for name in ('get_subscription_events','fetch_subscription_events','wait_for_subscription_events'):
        properties=backend[name]['inputSchema']['properties']
        assert 'default' not in properties['detail'] and 'default' not in properties['include']
        gateway=catalog['messages_'+name]
        assert gateway['inputSchema']['properties']['detail']==properties['detail']
        assert gateway['inputSchema']['properties']['include']==properties['include']
        assert 'detail=compact' in backend[name]['description']
        assert gateway['description']==backend[name]['description']
        sent.clear()
        getattr(python,name)(subscription_id='sub_synthetic')
        getattr(python,name)(subscription_id='sub_synthetic',detail='compact',include=['metadata'])
        assert 'detail' not in sent[0] and sent[1]['detail']=='compact' and sent[1]['include']==['metadata']
        for bad in ({'detail':'brief'},{'include':['selector']}):
            with pytest.raises(ValueError):
                getattr(python,name)(subscription_id='sub_synthetic',**bad)
        assert len(sent)==2
    readme=(root/'SUBSCRIPTIONS.md').read_text()
    assert "detail='compact'" in readme and 'the default stays `full`' in readme


ENDPOINT_TOOLS=('register_delivery_endpoint','list_delivery_endpoints','update_delivery_endpoint',
    'delete_delivery_endpoint','test_delivery_endpoint')


class EndpointStore(RecordingStore):
    def _endpoint(self,method,token,params):
        self.calls.append((method,params))
        return {'endpoint_id':'11111111-1111-4111-8111-111111111111','test':{'test_status':'pending'}}
    def register_delivery_endpoint(self,token,**params):return self._endpoint('register_delivery_endpoint',token,params)
    def list_delivery_endpoints(self,token,**params):return self._endpoint('list_delivery_endpoints',token,params)
    def update_delivery_endpoint(self,token,**params):return self._endpoint('update_delivery_endpoint',token,params)
    def delete_delivery_endpoint(self,token,**params):return self._endpoint('delete_delivery_endpoint',token,params)
    def test_delivery_endpoint(self,token,**params):return self._endpoint('test_delivery_endpoint',token,params)
    def list_delivery_attempts(self,token,**params):return self._endpoint('list_delivery_attempts',token,params)
    def configure_subscription_delivery(self,token,**params):return self._endpoint('configure_subscription_delivery',token,params)


# spec: ANYM-519
def test_endpoint_tools_match_the_gateway_catalog_and_client_lists():
    root=Path(__file__).resolve().parents[1]
    catalog={row['name']:row for row in json.loads(
        (root/'services/gateway/src/agent_gateway/catalog.json').read_text())}
    client={row['name']:row['read_only'] for row in json.loads((root/'packages/deedkit/lib/tools.json').read_text())}
    with TestClient(create_app(RecordingStore(),['testserver'])) as http:
        listed=http.post('/mcp',headers={'Authorization':'Bearer synthetic-token',
            'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,
            'method':'tools/list','params':{}}).json()['result']['tools']
    backend={tool['name']:tool for tool in listed}
    for name in (*ENDPOINT_TOOLS,'create_subscription','configure_subscription_delivery'):
        for field in ('description','inputSchema','annotations'):
            assert catalog['messages_'+name][field]==backend[name][field],(name,field)
    for name in ENDPOINT_TOOLS:
        read=name=='list_delivery_endpoints'
        assert backend[name]['annotations']['readOnlyHint'] is read and client['messages_'+name] is read
        assert catalog['messages_'+name]['scopes']==['read','delivery-write']
    for name in ('register_delivery_endpoint','update_delivery_endpoint'):
        assert 'write-only' in backend[name]['description'].lower()
    assert 'operator sets the host policy' in backend['register_delivery_endpoint']['description']


# spec: ANYM-519
@pytest.mark.parametrize('transport',['rest','mcp'])
def test_endpoint_calls_drop_omitted_options_and_wait_for_the_inline_test(transport,monkeypatch):
    from messaging_service import delivery_endpoints
    waited=[]
    async def finish(store,token,result,*,wait_s=10.0):
        waited.append(token);return {**result,'test':{'test_status':'delivered'}}
    monkeypatch.setattr(delivery_endpoints,'finish_inline_test',finish)
    store=EndpointStore()
    endpoint='11111111-1111-4111-8111-111111111111'
    secret='whsec_'+'A'*32
    calls=[('register_delivery_endpoint',{'request_id':'synthetic-endpoint','url':'https://hooks.example','signing_secret':secret}),
        ('update_delivery_endpoint',{'endpoint_id':endpoint,'version':1,'action':'pause'}),
        ('test_delivery_endpoint',{'endpoint_id':endpoint,'request_id':'synthetic-probe'}),
        ('list_delivery_endpoints',{}),
        ('configure_subscription_delivery',{'subscription_id':'sub_synthetic','version':1,'endpoint_id':endpoint})]
    results=[]
    with TestClient(create_app(store,['testserver'])) as http:
        for name,values in calls:
            if transport=='rest':
                response=http.post('/v1/'+name,headers={'Authorization':'Bearer synthetic-token'},json=values)
                assert response.status_code==200,response.text;results.append(response.json())
            else:
                body=http.post('/mcp',headers={'Authorization':'Bearer synthetic-token',
                    'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,
                    'method':'tools/call','params':{'name':name,'arguments':values}}).json()['result']
                assert not body.get('isError'),body;results.append(body['structuredContent'])
    assert store.calls==[
        ('register_delivery_endpoint',{'request_id':'synthetic-endpoint','url':'https://hooks.example','signing_secret':secret,'label':''}),
        ('update_delivery_endpoint',{'endpoint_id':endpoint,'version':1,'action':'pause'}),
        ('test_delivery_endpoint',{'endpoint_id':endpoint,'request_id':'synthetic-probe'}),
        ('list_delivery_endpoints',{}),
        ('configure_subscription_delivery',{'subscription_id':'sub_synthetic','version':1,'receiver_id':None,'endpoint_id':endpoint})]
    # Only register and test wait for the dispatcher's result.
    assert waited==['synthetic-token']*2
    assert [r['test']['test_status'] for r in results]==['delivered','pending','delivered','pending','pending']
    assert all(secret not in json.dumps(r) for r in results)


# spec: ANYM-520
def test_attempt_listing_matches_the_gateway_catalog_and_the_client_lists():
    root=Path(__file__).resolve().parents[1]
    catalog={row['name']:row for row in json.loads(
        (root/'services/gateway/src/agent_gateway/catalog.json').read_text())}
    client={row['name']:row['read_only'] for row in json.loads((root/'packages/deedkit/lib/tools.json').read_text())}
    with TestClient(create_app(RecordingStore(),['testserver'])) as http:
        listed=http.post('/mcp',headers={'Authorization':'Bearer synthetic-token',
            'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,
            'method':'tools/list','params':{}}).json()['result']['tools']
    backend={tool['name']:tool for tool in listed}
    for name in ('list_delivery_attempts','update_delivery_endpoint'):
        for field in ('description','inputSchema','annotations'):
            assert catalog['messages_'+name][field]==backend[name][field],(name,field)
    assert backend['list_delivery_attempts']['annotations']['readOnlyHint'] is True
    assert client['messages_list_delivery_attempts'] is True
    assert catalog['messages_list_delivery_attempts']['scopes']==['read','delivery-write']
    schema=backend['list_delivery_attempts']['inputSchema']
    assert schema['required']==['endpoint_id'] and schema['properties']['limit']['maximum']==50
    assert 'signed test' in backend['update_delivery_endpoint']['description']


# spec: ANYM-520
@pytest.mark.parametrize('transport',['rest','mcp'])
def test_attempt_listing_reaches_the_store_and_a_resume_waits_for_its_signed_test(transport,monkeypatch):
    from messaging_service import delivery_endpoints
    waited=[]
    async def finish(store,token,arguments,result,*,wait_s=10.0):
        waited.append(arguments);return {**result,'state':'enabled'}
    monkeypatch.setattr(delivery_endpoints,'finish_resume',finish)
    store=EndpointStore()
    endpoint='11111111-1111-4111-8111-111111111111'
    calls=[('list_delivery_attempts',{'endpoint_id':endpoint,'limit':5,'before':endpoint}),
        ('list_delivery_attempts',{'endpoint_id':endpoint}),
        ('update_delivery_endpoint',{'endpoint_id':endpoint,'version':3,'action':'resume'}),
        ('update_delivery_endpoint',{'endpoint_id':endpoint,'version':4,'action':'pause'})]
    results=[]
    with TestClient(create_app(store,['testserver'])) as http:
        for name,values in calls:
            if transport=='rest':
                response=http.post('/v1/'+name,headers={'Authorization':'Bearer synthetic-token'},json=values)
                assert response.status_code==200,response.text;results.append(response.json())
            else:
                body=http.post('/mcp',headers={'Authorization':'Bearer synthetic-token',
                    'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,
                    'method':'tools/call','params':{'name':name,'arguments':values}}).json()['result']
                assert not body.get('isError'),body;results.append(body['structuredContent'])
    assert store.calls==[('list_delivery_attempts',{'endpoint_id':endpoint,'limit':5,'before':endpoint}),
        ('list_delivery_attempts',{'endpoint_id':endpoint,'limit':50}),
        ('update_delivery_endpoint',{'endpoint_id':endpoint,'version':3,'action':'resume'}),
        ('update_delivery_endpoint',{'endpoint_id':endpoint,'version':4,'action':'pause'})]
    # Only the resume waits, with its exact arguments for the finishing call.
    assert waited==[{'endpoint_id':endpoint,'version':3,'action':'resume'}]
    assert [r.get('state') for r in results]==[None,None,'enabled',None]
