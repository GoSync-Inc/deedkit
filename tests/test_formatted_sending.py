"""Rich outbox contracts. Only synthetic providers and isolated PostgreSQL."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from messaging_service import outgoing
from messaging_service.envelope import stable_id
from messaging_service.store import APIError
from test_sending import sender as sender


def invoke(s,key,operation,**args):
    with s.authorized(key,'preview_message' if operation=='preview' else 'send_message') as (c,p):
        return getattr(outgoing,operation)(c,p,**args)


def arguments(**changes):
    return dict(account='send-fixture',chat_id=stable_id('chat','send-fixture','100'),
        text='**Тест 😀**',format='markdown',**changes)


def queue(s,key,**changes):
    args=arguments()
    args.update(changes)
    preview=invoke(s,key,'preview',**args)
    reply=args.pop('reply_to','')
    return invoke(s,key,'enqueue',**args,request_id='formatted-synthetic-request',
        preview_digest=preview['preview_digest'],reply_to=reply)


def test_preview_is_passive_without_send_permission_or_ready_collector(sender):
    s,archive,_,_,state=sender
    reader=s.issue_key('preview-reader',['send-fixture'],permissions=['read'])
    archive.set_sending('send-fixture',False)
    state.phase('connection','retry_wait','provider_disconnected')
    preview=invoke(s,reader,'preview',**arguments())
    assert preview['rendered_text']=='Тест 😀'
    assert preview['provider_payload']['entities']==[{'type':'bold','offset':0,'length':7}]
    assert len(preview['preview_digest'])==64
    assert preview['owner_approval_verified_by_server'] is False
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM outgoing_messages').fetchone()['n']==0


def test_review_digest_required_and_target_reply_policy_binding(sender):
    s,archive,key,_,_=sender
    args=arguments()
    with pytest.raises(APIError,match='preview_required'):
        invoke(s,key,'enqueue',**args,request_id='formatted-synthetic-request',reply_to='')
    preview=invoke(s,key,'preview',**args)
    for change in ({'text':'**changed**'},{'format':'html'},{'link_preview':True},
                   {'allow_lossy':True},{'reply_to':stable_id('msg','send-fixture','100','1')}):
        modified={**args,**change}
        reply=modified.pop('reply_to','')
        with pytest.raises(APIError,match='preview_mismatch'):
            invoke(s,key,'enqueue',**modified,request_id='formatted-synthetic-request',
                preview_digest=preview['preview_digest'],reply_to=reply)
    archive.ingest(__import__('test_archive').event(account='send-fixture',chat='200',provider_id='1',body='Different chat'))
    with pytest.raises(APIError,match='preview_mismatch'):
        invoke(s,key,'enqueue',**{**args,'chat_id':stable_id('chat','send-fixture','200')},
            request_id='formatted-synthetic-request',reply_to='',preview_digest=preview['preview_digest'])
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM outgoing_messages').fetchone()['n']==0


def test_outbox_replays_stored_source_before_renderer_upgrade_and_dispatches_persisted(sender,monkeypatch):
    import messaging_service.formatting as formatting
    s,_,key,_,state=sender
    args=arguments()
    preview=invoke(s,key,'preview',**args)
    queued=invoke(s,key,'enqueue',**args,request_id='formatted-synthetic-request',reply_to='',
        preview_digest=preview['preview_digest'])
    with s.pool.connection() as c:
        row=c.execute('SELECT * FROM outgoing_messages WHERE id=%s',(queued['send_id'],)).fetchone()
        assert row['body']=='Тест 😀' and row['source_text']=='**Тест 😀**'
        assert row['source_format']=='markdown' and row['render_artifact']['entities']==preview['entities']
    def upgraded(*a,**kw):raise AssertionError('Must not rerender an existing action')
    monkeypatch.setattr(formatting,'render_message',upgraded)
    assert invoke(s,key,'enqueue',**args,request_id='formatted-synthetic-request',reply_to='',
        preview_digest=preview['preview_digest'])['send_id']==queued['send_id']
    for changes in ({'text':'**changed**'},{'format':'html'},{'link_preview':True},
                    {'allow_lossy':True},{'preview_digest':'0'*64}):
        with pytest.raises(APIError,match='idempotency_conflict'):
            invoke(s,key,'enqueue',**{**args,'preview_digest':preview['preview_digest'],**changes},
                request_id='formatted-synthetic-request',reply_to='')
    calls=[]
    async def provider(job):calls.append(job);return {'provider_message_id':'42'}
    asyncio.run(outgoing.OutgoingWorker(state,provider).once())
    assert calls[0]['body']=='Тест 😀' and calls[0]['render_artifact']==row['render_artifact']
    safe=s.get_send_status(key,queued['send_id'])
    assert safe['formatting']['format']=='markdown'
    assert safe['formatting']['preview_digest']==preview['preview_digest']
    assert '**Тест 😀**' not in str(safe) and 'entities' not in str(safe)


def test_legacy_plain_hash_replay_remains_compatible(sender,monkeypatch):
    import messaging_service.formatting as formatting
    s,_,key,_,_=sender
    args={**arguments(),'text':'Original plain','format':'plain'}
    queued=invoke(s,key,'enqueue',**args,request_id='formatted-synthetic-request',reply_to='')
    with s.pool.connection() as c:
        c.execute('UPDATE outgoing_messages SET source_text=NULL,render_artifact=NULL,render_policy=NULL,preview_digest=NULL WHERE id=%s',
            (queued['send_id'],))
    monkeypatch.setattr(formatting,'render_message',lambda *a,**kw:pytest.fail('Legacy replay rerendered'))
    assert invoke(s,key,'enqueue',**args,request_id='formatted-synthetic-request',reply_to='')['send_id']==queued['send_id']
    with pytest.raises(APIError,match='idempotency_conflict'):
        invoke(s,key,'enqueue',**{**args,'format':'markdown'},request_id='formatted-synthetic-request',reply_to='')


@pytest.mark.parametrize('change,expected',[('expiry','expired'),('revoke','cancelled'),('acl','cancelled')])
def test_rich_pending_rechecks_expiry_and_revoked_access(sender,change,expected):
    s,_,key,_,state=sender
    queued=queue(s,key)
    if change=='expiry':
        with s.pool.connection() as c:c.execute("UPDATE outgoing_messages SET expires_at=now()-interval '1 second'")
    elif change=='revoke':s.revoke(key)
    else:s.set_access('send-agent',chats=[])
    calls=[]
    async def provider(job):calls.append(job)
    asyncio.run(outgoing.OutgoingWorker(state,provider).once())
    assert not calls
    with s.pool.connection() as c:
        assert c.execute('SELECT status FROM outgoing_messages WHERE id=%s',(queued['send_id'],)).fetchone()['status']==expected


def artifact_job(platform='telegram',**changes):
    text='😀 Hello'
    entities=[{'type':'bold','offset':3,'length':5}] if platform=='telegram' else []
    artifact={'format':'markdown','source_text':'😀 **Hello**','rendered_text':text,'provider_text':text,
        'entities':entities,'provider_payload':{'text':text,'entities':entities,'link_preview':True},
        'warnings':[],'links':[],'utf16_units':8,'renderer_version':'anymsg-renderer-1'}
    return {'provider_chat':'100','body':text,'source_text':artifact['source_text'],'source_format':'markdown',
        'render_artifact':artifact,'render_policy':{'link_preview':True,'allow_lossy':False},'preview_digest':'f'*64,
        'expires_at':datetime.now(timezone.utc).timestamp()+300,**changes}


def test_telegram_uses_persisted_entities_and_preview_policy_without_rerender():
    from messaging_service.telegram_collector import TelethonProvider
    from telethon import types
    calls=[]
    class Client:
        async def send_message(self,*args,**options):calls.append((args,options));return SimpleNamespace(id=42,chat_id=100)
    provider=TelethonProvider.__new__(TelethonProvider);provider.client=Client()
    asyncio.run(provider.send_text(artifact_job()))
    assert calls[0][0]==(100,'😀 Hello')
    assert calls[0][1]['parse_mode'] is None and calls[0][1]['link_preview'] is True
    entity=calls[0][1]['formatting_entities'][0]
    assert isinstance(entity,types.MessageEntityBold) and entity.offset==3 and entity.length==5


@pytest.mark.parametrize('malformed',[{'type':'mention_name','offset':3,'length':5,'user_id':42},
    {'type':'bold','offset':1,'length':1},{'type':'bold','offset':True,'length':5},
    {'type':'text_link','offset':3,'length':5,'url':'javascript:alert(1)'}])
def test_telegram_invalid_persisted_payload_never_invokes_provider(malformed):
    from messaging_service.telegram_collector import TelethonProvider
    calls=[]
    class Client:
        async def send_message(self,*a,**kw):calls.append(a)
    provider=TelethonProvider.__new__(TelethonProvider);provider.client=Client()
    job=artifact_job()
    job['render_artifact']['entities']=[malformed]
    job['render_artifact']['provider_payload']['entities']=[malformed]
    with pytest.raises(outgoing.SendRejected):asyncio.run(provider.send_text(job))
    assert not calls


def test_whatsapp_priority_lane_carries_stored_payload_policy():
    from messaging_service.whatsapp_collector import NodeProvider
    provider=NodeProvider.__new__(NodeProvider)
    provider.send_lock=SimpleNamespace(locked=lambda:True)
    calls=[]
    async def call(method,params,**options):calls.append((method,params,options));return {'provider_message_id':'synthetic'}
    provider._call_unlocked=call
    job=artifact_job('whatsapp',provider_chat='fixture@c.us')
    asyncio.run(provider.send_text(job))
    assert calls[0][0]=='send_text' and calls[0][1]['link_preview'] is True
    assert calls[0][1]['text']=='😀 Hello' and calls[0][2]=={'sending':True}


@pytest.mark.parametrize('change',[
    'extra_artifact','wrong_wire','wrong_units','wrong_source','unknown_format',
    'missing_digest','wrong_policy','extra_payload_option','bad_preview_type',
])
def test_persisted_artifact_inconsistency_rejects_before_rpc(change):
    job=artifact_job()
    artifact=job['render_artifact']
    if change=='extra_artifact':artifact['send_silently']=True
    elif change=='wrong_wire':artifact['provider_payload']['text']='Different wire'
    elif change=='wrong_units':artifact['utf16_units']=7
    elif change=='wrong_source':job['source_text']='Different source'
    elif change=='unknown_format':job['source_format']=artifact['format']='native'
    elif change=='missing_digest':job['preview_digest']=None
    elif change=='wrong_policy':job['render_policy']['allow_lossy']='false'
    elif change=='extra_payload_option':artifact['provider_payload']['silent']=True
    else:artifact['provider_payload']['link_preview']=1
    with pytest.raises(outgoing.SendRejected,match='send_target_invalid'):
        outgoing.dispatch_payload(job,'telegram')


def test_whatsapp_invalid_persisted_payload_never_reaches_priority_socket():
    from messaging_service.whatsapp_collector import NodeProvider
    provider=NodeProvider.__new__(NodeProvider)
    provider.send_lock=SimpleNamespace(locked=lambda:True)
    calls=[]
    async def call(*args,**kwargs):calls.append(args)
    provider._call_unlocked=call
    job=artifact_job('whatsapp',provider_chat='fixture@c.us')
    job['render_artifact']['provider_payload']['text']='Different wire'
    with pytest.raises(outgoing.SendRejected,match='send_target_invalid'):
        asyncio.run(provider.send_text(job))
    assert not calls


def test_actual_whatsapp_renderer_artifact_dispatches_native_wire_on_priority_lane():
    from messaging_service.formatting import render_message
    from messaging_service.whatsapp_collector import NodeProvider
    source='**Привет 😀**'
    artifact=render_message('whatsapp',source,format='markdown')
    assert artifact['entities'] and artifact['provider_payload']['entities']==[]
    job={'provider_chat':'fixture@c.us','body':artifact['provider_text'],'source_text':source,
        'source_format':'markdown','render_artifact':artifact,
        'render_policy':{'link_preview':False,'allow_lossy':False},'preview_digest':'f'*64,
        'expires_at':datetime.now(timezone.utc).timestamp()+300}
    provider=NodeProvider.__new__(NodeProvider)
    provider.send_lock=SimpleNamespace(locked=lambda:True)
    calls=[]
    async def call(method,params,**options):calls.append((method,params,options));return {'provider_message_id':'synthetic'}
    provider._call_unlocked=call
    asyncio.run(provider.send_text(job))
    assert calls==[('send_text',{'chat':'fixture@c.us','text':'*Привет 😀*','link_preview':False,
        'reply_provider_id':None,'expires_at':job['expires_at']},{'sending':True})]


def test_whatsapp_semantic_preview_spans_are_still_validated_before_dispatch():
    from messaging_service.formatting import render_message
    source='😀 **Hello**'
    artifact=render_message('whatsapp',source,format='markdown')
    artifact['entities'][0]['offset']=1  # Splits a surrogate pair in visible text.
    job={'body':artifact['provider_text'],'source_text':source,'source_format':'markdown',
        'render_artifact':artifact,'render_policy':{'link_preview':False,'allow_lossy':False},
        'preview_digest':'f'*64}
    with pytest.raises(outgoing.SendRejected,match='send_target_invalid'):
        outgoing.dispatch_payload(job,'whatsapp')


@pytest.mark.parametrize('source,mode',[
    ('```\nx = 1\n```','markdown'),
    ('`x = 1`','markdown'),
    ('<u>name </u>','html'),
    ('<a href="https://example.test">site </a>','html'),
    ('literal plain with space \n','plain'),
])
def test_actual_whatsapp_artifacts_preserve_semantic_whitespace_and_dispatch(source,mode):
    from messaging_service.formatting import render_message
    from messaging_service.whatsapp_collector import NodeProvider
    artifact=render_message('whatsapp',source,format=mode)
    job={'provider_chat':'fixture@c.us','body':artifact['provider_text'],'source_text':source,
        'source_format':mode,'render_artifact':artifact,
        'render_policy':{'link_preview':False,'allow_lossy':False},'preview_digest':'f'*64 if mode!='plain' else None,
        'expires_at':datetime.now(timezone.utc).timestamp()+300}
    provider=NodeProvider.__new__(NodeProvider)
    provider.send_lock=SimpleNamespace(locked=lambda:True)
    calls=[]
    async def call(method,params,**options):calls.append((method,params,options));return {'provider_message_id':'synthetic'}
    provider._call_unlocked=call
    asyncio.run(provider.send_text(job))
    assert calls[0][1]['text']==artifact['provider_text']
    assert calls[0][2]=={'sending':True} and calls[0][1]['link_preview'] is False
