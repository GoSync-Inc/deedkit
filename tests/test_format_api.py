"""Formatting API parity, authorization and passive previews; synthetic only."""
import json

import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.envelope import stable_id
from messaging_service.store import APIError
from test_sending import sender as sender


class RecordingStore:
    public_url = 'http://testserver'
    def __init__(self): self.calls=[]
    def authenticate(self, token): return 'synthetic-principal'
    def audit_boundary(self, code): pass
    def preview_message(self, token, **params):
        self.calls.append(('preview_message',params)); return {'preview_digest':'a'*64}
    def send_message(self, token, **params):
        self.calls.append(('send_message',params)); return {'status':'queued'}


def rpc(client, name, arguments):
    return client.post('/mcp',headers={'Authorization':'Bearer synthetic-token',
        'Accept':'application/json, text/event-stream'},json={
        'jsonrpc':'2.0','id':1,'method':'tools/call',
        'params':{'name':name,'arguments':arguments}})


@pytest.mark.parametrize('operation',['preview_message','send_message'])
def test_format_arguments_match_rest_and_mcp(operation):
    store=RecordingStore()
    args=dict(account='synthetic',chat_id='synthetic-chat',text='**Текст 😀**',
        reply_to='synthetic-message',format='markdown',link_preview=True,allow_lossy=False)
    if operation=='send_message':args.update(request_id='synthetic-action',preview_digest='a'*64)
    with TestClient(create_app(store,['testserver'])) as client:
        rest=client.post('/v1/'+operation,headers={'Authorization':'Bearer synthetic-token'},json=args)
        mcp=rpc(client,operation,args)
    assert rest.status_code==200
    assert not mcp.json().get('error') and not mcp.json()['result'].get('isError')
    assert store.calls==[(operation,args),(operation,args)]


@pytest.mark.parametrize('operation',['preview_message','send_message'])
def test_plain_old_calls_keep_defaults(operation):
    store=RecordingStore()
    args=dict(account='synthetic',chat_id='synthetic-chat',text='Literal 5*3')
    if operation=='send_message':args['request_id']='synthetic-action'
    defaults={**args,'reply_to':'','format':'plain','link_preview':False,'allow_lossy':False}
    if operation=='send_message':defaults['preview_digest']=''
    with TestClient(create_app(store,['testserver'])) as client:
        rest=client.post('/v1/'+operation,headers={'Authorization':'Bearer synthetic-token'},json=args)
        mcp=rpc(client,operation,args)
    assert rest.status_code==200 and not mcp.json().get('error')
    assert store.calls==[(operation,defaults),(operation,defaults)]


@pytest.mark.parametrize('operation,missing',[
    ('preview_message','account'),('preview_message','chat_id'),('preview_message','text'),
    ('send_message','account'),('send_message','chat_id'),('send_message','text'),
    ('send_message','request_id'),
])
def test_required_format_arguments_rejected_equally(operation,missing):
    store=RecordingStore()
    args=dict(account='synthetic',chat_id='synthetic-chat',text='synthetic-private-draft')
    if operation=='send_message':args['request_id']='synthetic-action'
    del args[missing]
    with TestClient(create_app(store,['testserver'])) as client:
        rest=client.post('/v1/'+operation,headers={'Authorization':'Bearer synthetic-token'},json=args)
        mcp=rpc(client,operation,args)
    if missing=='chat_id':
        # ANYM-532: the recipient is chat_id or phone; neither is refused before the store.
        assert rest.status_code==400 and rest.json()['error']=='invalid_arguments'
        assert rest.json()['fields']==['chat_id','phone']
        result=mcp.json()['result']
        assert result['isError'] and json.loads(result['content'][0]['text'].split(': ',1)[1])['fields']==['chat_id','phone']
    else:
        assert rest.status_code==400 and rest.json()['fields']==[missing]
        assert mcp.json()['error']['code']==-32602
        assert mcp.json()['error']['data']['fields']==[missing]
    assert 'synthetic-private-draft' not in rest.text+mcp.text
    assert store.calls==[]


@pytest.mark.parametrize('operation',['preview_message','send_message'])
@pytest.mark.parametrize('extra',[
    {'format':'bad'},{'format':None},{'preview_digest':None},
    {'link_preview':'false'},{'link_preview':1},{'link_preview':None},
    {'allow_lossy':'true'},{'allow_lossy':1},{'allow_lossy':None},
    {'approved':True},{'entities':[]},{'payload':{}},{'text':None},
])
def test_format_argument_denials_are_strict_before_operation(operation,extra):
    store=RecordingStore()
    args=dict(account='synthetic',chat_id='synthetic-chat',text='synthetic-private-draft')
    if operation=='send_message':args['request_id']='synthetic-action'
    args.update(extra)
    with TestClient(create_app(store,['testserver'])) as client:
        rest=client.post('/v1/'+operation,headers={'Authorization':'Bearer synthetic-token'},json=args)
        mcp=rpc(client,operation,args)
    assert rest.status_code==400 and rest.json()['error']=='invalid_parameters'
    assert mcp.json()['error']['code']==-32602
    assert 'synthetic-private-draft' not in rest.text+mcp.text
    assert store.calls==[]


def test_preview_registry_is_read_only_and_has_strict_required_arguments():
    store=RecordingStore()
    with TestClient(create_app(store,['testserver'])) as client:
        response=client.post('/mcp',headers={'Authorization':'Bearer synthetic-token',
            'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':2,'method':'tools/list'})
    tool=next(item for item in response.json()['result']['tools'] if item['name']=='preview_message')
    assert tool['annotations']['readOnlyHint'] is True
    # ANYM-532: a phone preview does one provider lookup; the recipient is chat_id or phone.
    assert tool['annotations']['openWorldHint'] is True
    assert set(tool['inputSchema']['required'])=={'account','text'}
    assert tool['inputSchema']['additionalProperties'] is False


def test_preview_is_read_authorized_with_no_send_policy_readiness_or_queue(sender):
    store,archive,_,_,state=sender
    key=store.issue_key('preview-reader',['send-fixture'],permissions=['read'])
    archive.set_sending('send-fixture',False)
    state.connection_failed('provider_disconnected')
    chat=stable_id('chat','send-fixture','100')
    result=store.preview_message(key,account='send-fixture',chat_id=chat,text='**Синтетический текст 😀**',format='markdown')
    assert result['preview_digest'] and result['rendered_text']=='Синтетический текст 😀'
    with pytest.raises(APIError,match='forbidden'):
        store.send_message(key,account='send-fixture',chat_id=chat,text='**Синтетический текст 😀**',
            request_id='synthetic-action',format='markdown',preview_digest=result['preview_digest'])
    with store.pool.connection() as connection:
        assert connection.execute('SELECT count(*) n FROM outgoing_messages').fetchone()['n']==0
        audit=connection.execute("SELECT operation,resource_ids FROM audit_log WHERE operation='preview_message'").fetchall()
    assert audit and 'Синтетический текст' not in str(audit)


def test_preview_hidden_chat_reply_and_revocation_are_not_found(sender):
    store,_,key,_,_=sender
    chat=stable_id('chat','send-fixture','100')
    for account,chat_id,reply in [('telegram-demo','telegram-demo-chat',''),
        ('send-fixture',chat,'demo-tg-1')]:
        with pytest.raises(APIError,match='not_found'):
            store.preview_message(key,account=account,chat_id=chat_id,text='draft',reply_to=reply)
    store.set_access('send-agent',chats=[])
    with pytest.raises(APIError,match='not_found'):
        store.preview_message(key,account='send-fixture',chat_id=chat,text='draft')


def test_mcp_rich_preview_digest_can_be_submitted_only_for_exact_policy(sender):
    store,_,key,_,_=sender
    args=dict(account='send-fixture',chat_id=stable_id('chat','send-fixture','100'),
        text='**Синтетический текст 😀**',format='markdown',link_preview=False,allow_lossy=False)
    with TestClient(create_app(store,['testserver'])) as client:
        headers={'Authorization':'Bearer '+key,'Accept':'application/json, text/event-stream'}
        def call(name,arguments):
            return client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':3,'method':'tools/call',
                'params':{'name':name,'arguments':arguments}}).json()['result']
        preview=call('preview_message',args)['structuredContent']
        digest=preview['preview_digest']
        mismatch=call('send_message',{**args,'request_id':'synthetic-mismatch',
            'link_preview':True,'preview_digest':digest})
        assert mismatch['isError'] is True and 'preview_mismatch' in str(mismatch)
        result=call('send_message',{**args,'request_id':'synthetic-valid','preview_digest':digest})['structuredContent']
    assert result['status']=='queued'
    assert result['formatting']['format']=='markdown'
    assert 'Синтетический текст' not in str(result)
