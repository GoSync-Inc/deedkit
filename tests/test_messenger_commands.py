"""Durable operations never activate real messenger sessions."""
import asyncio
import hashlib

import pytest

from messaging_service.archive import Archive
from messaging_service.collector_state import CollectorState
from messaging_service.envelope import stable_id
from messaging_service.store import APIError
from test_archive import event


def test_command_result_is_bounded_and_drops_diagnostics():
    from messaging_service.commands import safe_result
    assert safe_result({'outcome':'confirmed','data':{'title':'Synthetic'},
                        'token':'not for archive'}) == {'outcome':'confirmed','data':{'title':'Synthetic'}}
    with pytest.raises(ValueError):safe_result({'outcome':'confirmed','data':{'items':['x'*5000]}})
    with pytest.raises(ValueError):safe_result({'outcome':'confirmed','data':{'session':'secret'}})


def test_registered_file_hash_is_verified_against_opened_original(tmp_path):
    from types import SimpleNamespace
    from messaging_service.commands import _registered_file
    content=b'Synthetic CAS original';sha=hashlib.sha256(content).hexdigest()
    row={'id':'synthetic-attachment','message':'synthetic-message','digest':sha,
         'account':'synthetic-account','chat':'synthetic-chat','bytes':len(content)}
    class Connection:
        def execute(self,*args):return self
        def fetchone(self):return row
    p={'accounts':['synthetic-account'],'chat_ids':['synthetic-chat']}
    parameters={'attachment_id':row['id'],'sha256':sha}
    (tmp_path/sha).write_bytes(content)
    assert _registered_file(SimpleNamespace(files=tmp_path),Connection(),p,
                            'synthetic-account',parameters,verify=True)==row
    (tmp_path/sha).write_bytes(b'X'*len(content))
    with pytest.raises(APIError,match='attachment_unavailable'):
        _registered_file(SimpleNamespace(files=tmp_path),Connection(),p,
                          'synthetic-account',parameters,verify=True)


def test_registered_file_os_failure_is_a_safe_error(tmp_path,monkeypatch):
    import os
    from types import SimpleNamespace
    from messaging_service.commands import _registered_file
    content=b'Synthetic CAS original';sha=hashlib.sha256(content).hexdigest()
    row={'id':'synthetic-attachment','message':'synthetic-message','digest':sha,
         'account':'synthetic-account','chat':'synthetic-chat','bytes':len(content)}
    class Connection:
        def execute(self,*args):return self
        def fetchone(self):return row
    (tmp_path/sha).write_bytes(content)
    def unavailable(*args):raise PermissionError('PRIVATE_PATH_SENTINEL')
    monkeypatch.setattr(os,'open',unavailable)
    with pytest.raises(APIError,match='^attachment_unavailable$'):
        _registered_file(SimpleNamespace(files=tmp_path),Connection(),
            {'accounts':['synthetic-account'],'chat_ids':None},'synthetic-account',
            {'attachment_id':row['id'],'sha256':sha},verify=True)


@pytest.fixture
def operator(seeded):
    s, _, _ = seeded
    archive=Archive(s)
    archive.register('command-fixture','telegram','Synthetic operations',discover=True)
    archive.ingest(event(account='command-fixture',chat='100',provider_id='1',
                         direction='outgoing',body='Original synthetic text'))
    archive.ingest(event(account='command-fixture',chat='200',provider_id='2',
                         direction='incoming',body='Second synthetic chat'))
    key=s.issue_key('command-agent',['command-fixture'],permissions=[
        'read','download','send','read-live','message-edit','message-delete','message-react',
        'read-state','groups-read','groups-manage','contacts-read'])
    epoch=archive.acquire('command-fixture','synthetic-command-worker')
    state=CollectorState(archive,'command-fixture',('synthetic-command-worker',epoch))
    state.phase('connection','connected','provider_ready')
    s.set_messenger_operations('command-fixture',['edit_message','delete_message','send_reaction',
        'get_message_info','forward_message','send_file','mark_read','list_participants','list_contacts'])
    yield s,archive,key,state
    archive.release('command-fixture','synthetic-command-worker',epoch)


def operation_args(**changes):
    return {'account':'command-fixture','operation':'delete_message',
            'chat_id':stable_id('chat','command-fixture','100'),
            'message_id':stable_id('msg','command-fixture','100','1'),
            'expected_revision':1,'parameters':{},**changes}


def queue(s,key,**changes):
    args=operation_args(**changes)
    draft=s.preview_messenger_operation(key,**args)
    return s.execute_messenger_operation(key,**args,request_id='synthetic-operation-0001',
                                         preview_digest=draft['preview_digest'])


def test_preview_is_passive_and_digest_is_scoped(operator):
    s,_,key,_=operator
    draft=s.preview_messenger_operation(key,**operation_args())
    assert draft['owner_approval_verified_by_server'] is False
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n']==0
    other=s.issue_key('command-agent',['command-fixture'])
    with pytest.raises(APIError,match='preview_mismatch'):
        s.execute_messenger_operation(other,**operation_args(),request_id='synthetic-operation-0001',
                                       preview_digest=draft['preview_digest'])


def test_capabilities_reports_independent_operations_contract(operator):
    s,_,key,_=operator
    caps=s.capabilities(key)
    ops=caps['messenger_operations']
    assert ops['account_opt_in_required'] is True and ops['independent_key_permissions'] is True
    assert ops['catalog_version']==1 and ops['operation_count']>30
    assert caps['sending']['text_only'] is False
    assert caps['sending']['legacy_send_message_text_only'] is True
    assert {'list_messenger_operations','preview_messenger_operation','execute_messenger_operation',
            'get_messenger_operation'}<=set(caps['available'])


def test_mutation_requires_preview_and_idempotency_is_exact(operator):
    s,_,key,_=operator
    with pytest.raises(APIError,match='preview_required'):
        s.execute_messenger_operation(key,**operation_args(),request_id='synthetic-operation-0001')
    first=queue(s,key)
    assert first['status']=='queued'
    assert queue(s,key)['operation_id']==first['operation_id']
    with pytest.raises(APIError,match='idempotency_conflict'):
        s.execute_messenger_operation(key,**operation_args(operation='send_reaction',parameters={'emoji':'👍'}),
                                       request_id='synthetic-operation-0001',preview_digest='0'*64)


def test_account_opt_in_and_full_account_scope(operator):
    s,_,key,_=operator
    s.set_messenger_operations('command-fixture',[])
    with pytest.raises(APIError,match='operation_disabled'):
        s.preview_messenger_operation(key,**operation_args())
    s.set_messenger_operations('command-fixture',['list_contacts','list_participants'])
    s.set_access('command-agent',chats=[stable_id('chat','command-fixture','100')])
    with pytest.raises(APIError,match='forbidden'):
        s.preview_messenger_operation(key,account='command-fixture',operation='list_contacts',parameters={})
    assert s.preview_messenger_operation(key,account='command-fixture',operation='list_participants',
                                         chat_id=stable_id('chat','command-fixture','100'),parameters={})


def test_target_revision_and_outgoing_ownership(operator):
    s,_,key,_=operator
    with pytest.raises(APIError,match='operation_target_changed'):
        s.preview_messenger_operation(key,**operation_args(expected_revision=2))
    with pytest.raises(APIError,match='forbidden'):
        s.preview_messenger_operation(key,**operation_args(chat_id=stable_id('chat','command-fixture','200'),
            message_id=stable_id('msg','command-fixture','200','2')))


# spec: ANYM-747
def test_a_drained_edit_moves_the_expected_revision(operator,tmp_path):
    from datetime import datetime,timezone
    from types import SimpleNamespace as NS
    from telethon import types
    from messaging_service.adapters import telegram_message
    from messaging_service.collector_state import CaptureSpool
    s,_,key,state=operator
    edited=NS(id=1,chat_id=100,sender_id=42,message='Edited synthetic text',entities=[],out=True,
        peer_id=types.PeerUser(100),date=datetime(2026,10,1,12,tzinfo=timezone.utc),
        edit_date=datetime(2026,11,4,9,30,tzinfo=timezone.utc),file=None,document=None,photo=None,media=None)
    spool=CaptureSpool(tmp_path/'capture',reserve_bytes=0)
    spool.put({'kind':'message','peer_kind':'user','envelope':telegram_message('command-fixture',edited),
               'captured_at':'2026-11-04T09:30:01+00:00'})
    assert spool.drain(state.apply)==1
    edit=dict(operation='edit_message',parameters={'text':'Another synthetic text'})
    with pytest.raises(APIError,match='operation_target_changed'):
        s.preview_messenger_operation(key,**operation_args(**edit,expected_revision=1))
    assert s.preview_messenger_operation(key,**operation_args(**edit,expected_revision=2))['preview_digest']


# spec: ANYM-747
def test_unknown_edit_names_the_verifying_read(operator):
    from messaging_service.commands import VERIFY,_verify
    s,_,_,_=operator
    row={'operation':'edit_message','status':'unknown','account':'command-fixture','payload':{'parameters':{'text':'x'}},
         'result':{'outcome':'unknown','data':{'error':'provider_result_unverified'}}}
    with s.pool.connection() as c:
        assert _verify(row,c)==VERIFY['edit_message']=={'operation':'get_message_info','requires':'read-live',
            'decides':'text equal to the requested text means the edit applied'}
        flood={**row,'result':{'outcome':'unknown','data':{'error':'provider_result_unverified',
            'evidence':{'basis':'provider_read_back','retry_after_s':45}}}}
        assert _verify(flood,c)=={**VERIFY['edit_message'],'retry_after_s':45}
        assert _verify({**row,'status':'succeeded'},c) is None


FORMATTED_EDIT={'text':'<b>Edited</b> <a href="https://example.invalid/x">link</a>','format':'html'}


# spec: ANYM-755
def test_formatted_edit_binds_the_rendered_artifact_to_its_preview_digest(operator):
    from messaging_service.formatting import render_message
    s,_,key,_=operator
    plain=s.preview_messenger_operation(key,**operation_args(operation='edit_message',parameters={'text':'Edited'}))
    assert 'edit_artifact' not in plain and plain['parameters']=={'text':'Edited'}  # defaults: today's payload
    explicit=s.preview_messenger_operation(key,**operation_args(operation='edit_message',
        parameters={'text':'Edited','format':'plain','link_preview':False,'allow_lossy':False}))
    assert explicit['preview_digest']==plain['preview_digest'] and explicit['parameters']=={'text':'Edited'}
    with pytest.raises(APIError,match='invalid_operation_parameters'):  # a plain edit keeps its 4000 units
        s.preview_messenger_operation(key,**operation_args(operation='edit_message',parameters={'text':'x'*4001}))
    assert s.preview_messenger_operation(key,**operation_args(operation='edit_message',
        parameters={'text':'<b>'+'x'*4000+'</b>','format':'html'}))['edit_artifact']['utf16_units']==4000
    args=operation_args(operation='edit_message',parameters=FORMATTED_EDIT)
    draft=s.preview_messenger_operation(key,**args)
    assert draft['edit_artifact']==render_message('telegram',FORMATTED_EDIT['text'],format='html')
    assert draft['edit_artifact']['rendered_text']=='Edited link'
    assert draft['edit_artifact']['provider_payload']['entities']==[{'type':'bold','offset':0,'length':6},
        {'type':'text_link','offset':7,'length':4,'url':'https://example.invalid/x'}]
    # the same continuation as any mutation without (or with another) preview; nothing is queued
    for digest,code in (('','preview_required'),(plain['preview_digest'],'preview_mismatch')):
        with pytest.raises(APIError,match=code) as refused:
            s.execute_messenger_operation(key,**args,request_id='synthetic-format-0001',preview_digest=digest)
        assert refused.value.next_action=='preview_then_send_with_digest'
        assert refused.value.next_call['tool']=='preview_messenger_operation'
    other=s.preview_messenger_operation(key,**operation_args(operation='edit_message',
        parameters={**FORMATTED_EDIT,'link_preview':True}))
    assert other['preview_digest']!=draft['preview_digest']
    queued=s.execute_messenger_operation(key,**args,request_id='synthetic-format-0001',
                                         preview_digest=draft['preview_digest'])
    with s.pool.connection() as c:
        payload=c.execute('SELECT payload FROM messenger_commands WHERE id=%s',(queued['operation_id'],)).fetchone()['payload']
    assert payload['edit_artifact']==draft['edit_artifact']
    # a source the send path refuses is refused here, before anything is queued
    with pytest.raises(APIError,match='unsafe_message_markup|unsupported_message_formatting'):
        s.preview_messenger_operation(key,**operation_args(operation='edit_message',
            parameters={'text':'<script>x</script>','format':'html'}))


def formatted_edit_args(source):
    return operation_args(operation='edit_message',parameters={'text':source,'format':'html'})


def refused_alike(s,key,args,expected):
    """Preview and execute give one refusal, and nothing is queued."""
    with s.pool.connection() as c:
        before=c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n']
    for call,extra in ((s.preview_messenger_operation,{}),
                       (s.execute_messenger_operation,{'request_id':'synthetic-size-0001','preview_digest':''})):
        with pytest.raises(APIError,match='invalid_operation_parameters') as refused:
            call(key,**args,**extra)
        assert refused.value.field_errors==[{'field':'parameters','reason':'size','expected':expected}]
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n']==before


# spec: ANYM-755
def test_a_formatted_edit_that_previews_also_executes_at_the_request_limit(operator):
    # Sol r2: a source inside the 16000-unit allowance previewed, then failed the canonical request cap on execute.
    from messaging_service.envelope import canonical
    s,_,key,_=operator
    source=lambda count:'<b>'+'&#120;'*count+'</b>'
    size=lambda count:len(canonical({**formatted_edit_args(source(count)),'preview_digest':'0'*64}))
    largest=max(count for count in range(2500,2700) if size(count)<=16000)
    assert largest<2650 and len(source(2650))<=16000  # the reported source is inside the unit allowance
    args=formatted_edit_args(source(largest))
    draft=s.preview_messenger_operation(key,**args)
    queued=s.execute_messenger_operation(key,**args,request_id='synthetic-size-0002',preview_digest=draft['preview_digest'])
    assert queued['status']=='queued'
    for count in (largest+1,2650):
        refused_alike(s,key,formatted_edit_args(source(count)),'canonical request of at most 16000 bytes')


# spec: ANYM-755
def test_a_formatted_edit_over_the_stored_payload_bound_is_refused_at_preview(operator):
    # The stored row repeats the source and the rendering: 4000 CJK characters fit the request, not the payload CHECK.
    import json
    s,_,key,_=operator
    args=lambda count:formatted_edit_args('<b>'+'漢'*count+'</b>')
    def previews(count):
        try:return bool(s.preview_messenger_operation(key,**args(count))['preview_digest'])
        except APIError as refused:
            assert refused.field_errors==[{'field':'parameters','reason':'size',
                'expected':'stored formatted edit of at most 65536 bytes'}]
            return False
    fits,over=3000,4000
    assert previews(fits) and not previews(over)
    while over-fits>1:
        middle=(fits+over)//2
        if previews(middle):fits=middle
        else:over=middle
    refused_alike(s,key,args(over),'stored formatted edit of at most 65536 bytes')
    # The largest that previews is stored: the bound is the CHECK's own measure, not an estimate of it.
    draft=s.preview_messenger_operation(key,**args(fits))
    queued=s.execute_messenger_operation(key,**args(fits),request_id='synthetic-size-0003',preview_digest=draft['preview_digest'])
    with s.pool.connection() as c:
        row=c.execute('SELECT payload,octet_length(payload::text) AS bytes FROM messenger_commands WHERE id=%s',
                      (queued['operation_id'],)).fetchone()
    assert row['bytes']==len(json.dumps(row['payload'],ensure_ascii=False).encode())<=65536


# spec: ANYM-755
@pytest.mark.parametrize('options',[{'format':'html'},{'format':'markdown'},{'link_preview':True},{'allow_lossy':True}])
def test_whatsapp_formatted_edit_is_refused_unsupported_with_a_plain_next_step(seeded,options):
    from messaging_service.commands import EDIT_FORMAT_NEXT_STEP
    s,_,_=seeded
    archive=Archive(s)
    archive.register('edit-wa','whatsapp','Synthetic WhatsApp',discover=True)
    archive.ingest(event(account='edit-wa',platform='whatsapp',chat='900@c.us',provider_id='1',direction='outgoing',
                         body='Original'))
    key=s.issue_key('wa-editor',['edit-wa'],permissions=['read','message-edit'])
    s.set_messenger_operations('edit-wa',['edit_message'])
    args=dict(account='edit-wa',operation='edit_message',chat_id=stable_id('chat','edit-wa','900@c.us'),
              message_id=stable_id('msg','edit-wa','900@c.us','1'),expected_revision=1)
    with pytest.raises(APIError,match='unsupported_operation') as refused:
        s.preview_messenger_operation(key,**args,parameters={'text':'*Edited*',**options})
    assert refused.value.next_step==EDIT_FORMAT_NEXT_STEP
    assert 'edit_artifact' not in s.preview_messenger_operation(key,**args,parameters={'text':'*Edited*'})


@pytest.mark.parametrize('change',['key','principal','policy','revision','expiry','connection'])
def test_worker_rechecks_mutable_authority(operator,change):
    from messaging_service.commands import CommandWorker
    s,archive,key,state=operator
    queued=queue(s,key)
    if change=='key':s.set_key_permissions(s.capabilities(key)['credential_id'],['read'])
    elif change=='principal':s.set_access('command-agent',permissions=['read'])
    elif change=='policy':s.set_messenger_operations('command-fixture',[])
    elif change=='revision':archive.ingest(event(account='command-fixture',chat='100',provider_id='1',
        direction='outgoing',body='Changed synthetic text',updated_at='2026-10-01T12:01:00Z'))
    elif change=='expiry':
        with s.pool.connection() as c:c.execute("UPDATE messenger_commands SET expires_at=now()-interval '1 second'")
    else:state.phase('connection','retry_wait','provider_disconnected')
    calls=[]
    async def provider(job):calls.append(job);return {'outcome':'confirmed'}
    assert asyncio.run(CommandWorker(state,provider).once())
    assert not calls
    with s.pool.connection() as c:
        assert c.execute('SELECT status FROM messenger_commands WHERE id=%s',
                         (queued['operation_id'],)).fetchone()['status'] in ('cancelled','expired')


@pytest.mark.parametrize('outcome,status',[('confirmed','succeeded'),('submitted','unknown'),('unknown','unknown')])
def test_mutations_never_retry_an_ambiguous_provider_result(operator,outcome,status):
    from messaging_service.commands import CommandWorker
    s,_,key,state=operator
    queued=queue(s,key);calls=[]
    async def provider(job):calls.append(job);return {'outcome':outcome}
    worker=CommandWorker(state,provider)
    assert asyncio.run(worker.once()) and not asyncio.run(worker.once())
    assert s.get_messenger_operation(key,queued['operation_id'])['status']==status
    assert queue(s,key)['status']==status and len(calls)==1


SPAM='Synthetic spam offer '*20
MODERATOR=['read','read-live','groups-read','groups-moderate']


def moderation(operator,permissions=MODERATOR):
    """A supergroup (-1001), a channel (-1002) and its discussion group (-1003) with other people's messages."""
    s,archive,_,state=operator
    def message(chat,provider_id,title,kind,**changes):
        archive.ingest(event(account='command-fixture',chat=chat,chat_title=title,provider_id=provider_id,
                             metadata={'chat_type':kind},**changes))
        return stable_id('msg','command-fixture',chat,provider_id)
    ids={'spam':message('-1001','11','Synthetic supergroup','group',sender='42',body=SPAM,
                        sender_profile={'display_name':'Synthetic Seller','username':'synthetic_seller'}),
         'spam2':message('-1001','12','Synthetic supergroup','group',sender='43',body='Second synthetic spam'),
         'own':message('-1001','13','Synthetic supergroup','group',sender='7',direction='outgoing',body='Own text'),
         'post':message('-1002','21','Synthetic news','channel',sender='-1002',body='Synthetic post'),
         'forwarded':message('-1003','31','Synthetic news chat','group',sender='-1002',body='Synthetic post copy'),
         'comment':message('-1003','32','Synthetic news chat','group',sender='44',body='Synthetic comment spam')}
    for chat in ('-1001','-1002','-1003'):state.chat(chat,'channel')
    key=s.issue_key('command-agent',['command-fixture'],permissions=permissions)
    s.set_messenger_operations('command-fixture',['delete_member_messages','delete_message'])
    return s,archive,key,state,ids


def moderation_args(chat,*messages,revisions=None):
    return {'account':'command-fixture','operation':'delete_member_messages',
            'chat_id':stable_id('chat','command-fixture',chat),
            'parameters':{'message_ids':list(messages),'expected_revisions':revisions or [1]*len(messages)}}


def refused_fields(s,key,args):
    with pytest.raises(APIError,match='^invalid_operation_parameters$') as failure:
        s.preview_messenger_operation(key,**args)
    return failure.value


# spec: ANYM-934
def test_delete_member_messages_preview_names_each_sender_and_the_chat_type(operator):
    s,_,key,_,ids=moderation(operator)
    draft=s.preview_messenger_operation(key,**moderation_args('-1001',ids['spam'],ids['spam2']))
    shown=draft['moderation']
    assert shown['chat']['chat_type']=='supergroup' and shown['count']==2
    assert shown['archive']=='the archive copy is purged as well'
    first,second=shown['messages']
    assert (first['message_id'],first['sender_id'],first['sender_name'],first['sender_username'])==(
        ids['spam'],'42','Synthetic Seller','synthetic_seller')
    assert first['text']==SPAM[:200] and first['kind'] and first['sent_at'].startswith('2026-10-01')
    assert second['sender_id']=='43' and second['sender_username'] is None
    assert draft['required_permissions']==['groups-moderate','read'] and draft['mutation'] is True
    assert [t['message_id'] for t in draft['targets']]==[ids['spam'],ids['spam2']]
    comments=s.preview_messenger_operation(key,**moderation_args('-1003',ids['comment']))['moderation']['chat']
    assert comments['chat_type']=='discussion group of Synthetic news'
    assert s.preview_messenger_operation(key,**moderation_args('-1002',ids['post']))['moderation']['chat'][
        'chat_type']=='channel'


# spec: ANYM-934
def test_delete_member_messages_digest_binds_the_revisions_and_an_edit_refuses(operator):
    s,archive,key,_,ids=moderation(operator)
    args=moderation_args('-1001',ids['spam'])
    draft=s.preview_messenger_operation(key,**args)
    archive.ingest(event(account='command-fixture',chat='-1001',provider_id='11',sender='42',body='Edited synthetic spam',
                         metadata={'chat_type':'group'},updated_at='2026-10-01T12:01:00Z'))
    with pytest.raises(APIError,match='^operation_target_changed$'):
        s.execute_messenger_operation(key,**args,request_id='synthetic-moderation-1',preview_digest=draft['preview_digest'])
    with pytest.raises(APIError,match='^preview_mismatch$'):
        s.execute_messenger_operation(key,**moderation_args('-1001',ids['spam'],revisions=[2]),
                                      request_id='synthetic-moderation-2',preview_digest=draft['preview_digest'])


# spec: ANYM-934
def test_delete_member_messages_names_each_target_it_refuses(operator):
    s,_,key,_,ids=moderation(operator)
    failure=refused_fields(s,key,moderation_args('-1002',ids['spam'],ids['comment'],ids['post']))
    assert [(e['field'],e['reason']) for e in failure.field_errors]==[
        ('message_ids/0','message_from_other_chat'),('message_ids/1','message_from_other_chat')]
    assert failure.other_chats==[
        {'field':'message_ids/0','chat_id':stable_id('chat','command-fixture','-1001'),'chat_type':'supergroup'},
        {'field':'message_ids/1','chat_id':stable_id('chat','command-fixture','-1003'),
         'chat_type':'discussion group of Synthetic news'}]
    unknown='msg_'+'f'*32
    failure=refused_fields(s,key,moderation_args('-1001',unknown,ids['own'],ids['spam']))
    assert [(e['field'],e['reason']) for e in failure.field_errors]==[
        ('message_ids/0','message_not_found'),('message_ids/1','own_message')]
    with s.pool.connection() as c:
        c.execute("UPDATE account_policies SET exclude_chats=ARRAY['-1001'] WHERE account='command-fixture'")
    assert [(e['field'],e['reason']) for e in refused_fields(s,key,moderation_args('-1001',ids['spam'])).field_errors]==[
        ('chat_id','chat_not_collected')]


# spec: ANYM-934
def test_delete_member_messages_needs_groups_moderate_and_a_telegram_account(operator):
    s,archive,_,_,ids=moderation(operator)
    plain=s.issue_key('command-agent',['command-fixture'],permissions=['read','read-live','groups-manage','message-delete'])
    with pytest.raises(APIError,match='^forbidden$'):
        s.preview_messenger_operation(plain,**moderation_args('-1001',ids['spam']))
    listed={i['operation']:i for i in s.list_messenger_operations(plain,'command-fixture')['items']}
    assert listed['delete_member_messages']['key_permitted'] is False
    archive.register('moderation-wa','whatsapp','Synthetic WhatsApp',discover=True)
    archive.ingest(event(account='moderation-wa',platform='whatsapp',chat='900@g.us',provider_id='1'))
    wa=s.issue_key('command-agent',['moderation-wa'],permissions=MODERATOR)
    with pytest.raises(APIError,match='^moderation_unsupported$'):
        s.preview_messenger_operation(wa,account='moderation-wa',operation='delete_member_messages',
            chat_id=stable_id('chat','moderation-wa','900@g.us'),
            parameters={'message_ids':[stable_id('msg','moderation-wa','900@g.us','1')],'expected_revisions':[1]})


# spec: ANYM-934
def test_a_rights_refusal_fails_the_operation_with_its_evidence_and_deletes_nothing(operator):
    from messaging_service.commands import CommandRejected,CommandWorker
    s,_,key,state,ids=moderation(operator)
    args=moderation_args('-1001',ids['spam'])
    draft=s.preview_messenger_operation(key,**args)
    queued=s.execute_messenger_operation(key,**args,request_id='synthetic-moderation-3',preview_digest=draft['preview_digest'])
    jobs=[]
    async def provider(job):
        jobs.append(job);raise CommandRejected('missing_admin_right',data={'evidence':{'missing_right':'delete_messages'}})
    assert asyncio.run(CommandWorker(state,provider).once())
    receipt=s.get_messenger_operation(key,queued['operation_id'])
    assert (receipt['status'],receipt['error_code'])==('failed','missing_admin_right')
    assert receipt['result']['data']=={'code':'missing_admin_right','evidence':{'missing_right':'delete_messages'}}
    assert jobs[0]['targets']==[{'message_id':ids['spam'],'revision':1,'provider_message_id':'11'}]
    with s.pool.connection() as c:
        assert not c.execute('SELECT deleted FROM messages WHERE id=%s',(ids['spam'],)).fetchone()['deleted']


def test_status_is_bound_to_originating_key(operator):
    s,_,key,_=operator
    queued=queue(s,key)
    other=s.issue_key('command-agent',['command-fixture'])
    with pytest.raises(APIError,match='not_found'):
        s.get_messenger_operation(other,queued['operation_id'])


def test_preflight_rechecks_authority_after_claim(operator):
    from messaging_service.commands import CommandRejected,CommandWorker
    s,_,key,state=operator
    queue(s,key)
    worker=CommandWorker(state,None)
    job=worker.claim()
    s.set_key_permissions(s.capabilities(key)['credential_id'],['read'])
    with pytest.raises(CommandRejected):worker.preflight(job)


def test_interrupted_dispatch_is_unknown_and_never_replayed(operator):
    from messaging_service.commands import CommandWorker
    s,_,key,state=operator
    queued=queue(s,key)
    with s.pool.connection() as c:
        c.execute("UPDATE messenger_commands SET status='dispatching',lease_epoch=0 WHERE id=%s",(queued['operation_id'],))
    calls=[]
    async def provider(job):calls.append(job);return {'outcome':'confirmed'}
    assert not asyncio.run(CommandWorker(state,provider).once())
    assert not calls and s.get_messenger_operation(key,queued['operation_id'])['status']=='unknown'


def test_expired_same_epoch_dispatch_is_unknown_after_target_change(operator):
    from messaging_service.commands import CommandWorker
    s,archive,key,state=operator
    queued=queue(s,key);calls=[]
    async def provider(job):calls.append(job);return {'outcome':'confirmed'}
    worker=CommandWorker(state,provider)
    assert worker.claim()['lease_epoch']==state.lease[1]
    # Simulate a completed provider mutation whose receipt could not be saved,
    # while the original collector continues renewing the same epoch.
    archive.ingest(event(account='command-fixture',chat='100',provider_id='1',direction='outgoing',
        body='',deleted=True,updated_at='2026-10-01T12:01:00Z'))
    with s.pool.connection() as c:
        c.execute("UPDATE messenger_commands SET expires_at=now()-interval '1 second' WHERE id=%s",(queued['operation_id'],))
    assert s.get_messenger_operation(key,queued['operation_id'])['status']=='unknown'
    assert not asyncio.run(worker.once()) and not asyncio.run(worker.once()) and not calls
    with s.pool.connection() as c:
        assert c.execute('SELECT status FROM messenger_commands WHERE id=%s',
                         (queued['operation_id'],)).fetchone()['status']=='unknown'


def test_live_read_can_enqueue_without_preview_and_returns_bounded_result(operator):
    from messaging_service.commands import CommandWorker
    s,_,key,state=operator
    queued=s.execute_messenger_operation(key,**operation_args(operation='get_message_info'),
                                         request_id='synthetic-read-0001')
    async def provider(job):return {'outcome':'confirmed','data':{'provider_message_id':'1'}}
    assert asyncio.run(CommandWorker(state,provider).once())
    result=s.get_messenger_operation(key,queued['operation_id'])
    assert result['status']=='succeeded' and result['result']['data']['provider_message_id']=='1'


def test_audit_contains_no_command_payload(operator):
    s,_,key,_=operator
    queue(s,key,operation='edit_message',parameters={'text':'SYNTHETIC_PRIVATE_DRAFT_SENTINEL'})
    with s.pool.connection() as c:
        rows=c.execute('SELECT * FROM audit_log').fetchall()
    assert 'SYNTHETIC_PRIVATE_DRAFT_SENTINEL' not in str(rows)


def test_exact_retry_does_not_reinterpret_parameters(operator,monkeypatch):
    import messaging_service.messenger_operations as catalog
    s,_,key,_=operator
    args=operation_args()
    draft=s.preview_messenger_operation(key,**args)
    first=s.execute_messenger_operation(key,**args,request_id='synthetic-operation-0001',
                                       preview_digest=draft['preview_digest'])
    def changed_validator(*args):raise ValueError('invalid_operation_parameters')
    monkeypatch.setattr(catalog,'validate_operation',changed_validator)
    repeated=s.execute_messenger_operation(key,**args,request_id='synthetic-operation-0001',
                                           preview_digest=draft['preview_digest'])
    assert repeated['operation_id']==first['operation_id']


@pytest.mark.parametrize('deleted',[False,True])
def test_terminal_result_and_exact_retry_survive_successful_target_change(operator,deleted):
    from messaging_service.commands import CommandWorker
    s,archive,key,state=operator
    args=operation_args(operation='delete_message' if deleted else 'edit_message',
                        parameters={} if deleted else {'text':'Updated synthetic body'})
    draft=s.preview_messenger_operation(key,**args)
    first=s.execute_messenger_operation(key,**args,request_id='synthetic-operation-0001',
                                       preview_digest=draft['preview_digest'])
    async def provider(job):return {'outcome':'confirmed'}
    assert asyncio.run(CommandWorker(state,provider).once())
    archive.ingest(event(account='command-fixture',chat='100',provider_id='1',direction='outgoing',
        body='' if deleted else 'Updated synthetic body',deleted=deleted,updated_at='2026-10-01T12:01:00Z'))
    s.set_messenger_operations('command-fixture',[])
    assert s.get_messenger_operation(key,first['operation_id'])['status']=='succeeded'
    repeated=s.execute_messenger_operation(key,**args,request_id='synthetic-operation-0001',
                                           preview_digest=draft['preview_digest'])
    assert repeated['operation_id']==first['operation_id'] and repeated['status']=='succeeded'
    s.set_access('command-agent',chats=[])
    with pytest.raises(APIError,match='not_found'):
        s.get_messenger_operation(key,first['operation_id'])


def test_terminal_forward_result_keeps_source_scope_after_source_deleted(operator):
    _assert_forward_receipt_keeps_source_scope(operator,completed=True)


def test_pending_forward_result_keeps_source_scope_after_source_deleted(operator):
    _assert_forward_receipt_keeps_source_scope(operator,completed=False)


def _assert_forward_receipt_keeps_source_scope(operator,*,completed):
    from messaging_service.commands import CommandWorker
    s,archive,key,state=operator
    args={'account':'command-fixture','operation':'forward_message',
          'chat_id':stable_id('chat','command-fixture','100'),
          'parameters':{'source_message_id':stable_id('msg','command-fixture','200','2'),'source_revision':1}}
    draft=s.preview_messenger_operation(key,**args)
    first=s.execute_messenger_operation(key,**args,request_id='synthetic-forward-0001',
                                       preview_digest=draft['preview_digest'])
    async def provider(job):return {'outcome':'confirmed'}
    if completed:assert asyncio.run(CommandWorker(state,provider).once())
    archive.ingest(event(account='command-fixture',chat='200',provider_id='2',body='',deleted=True,
                         updated_at='2026-10-01T12:01:00Z'))
    assert s.get_messenger_operation(key,first['operation_id'])['status']==('succeeded' if completed else 'queued')
    s.set_access('command-agent',chats=[args['chat_id']])
    with pytest.raises(APIError,match='not_found'):
        s.get_messenger_operation(key,first['operation_id'])


def test_forward_requires_archive_read_permission(operator):
    s,_,key,_=operator
    s.set_access('command-agent',permissions=['send'])
    with pytest.raises(APIError,match='forbidden'):
        s.preview_messenger_operation(key,account='command-fixture',operation='forward_message',
            chat_id=stable_id('chat','command-fixture','100'),parameters={
                'source_message_id':stable_id('msg','command-fixture','200','2'),'source_revision':1})


def test_send_file_requires_download_permission_before_exposing_metadata(operator):
    s,_,key,_=operator
    s.set_access('command-agent',permissions=['send'])
    with pytest.raises(APIError,match='forbidden'):
        s.preview_messenger_operation(key,account='command-fixture',operation='send_file',
            chat_id=stable_id('chat','command-fixture','100'),parameters={
                'attachment_id':'att_'+'0'*32,'sha256':'0'*64})


def test_operation_manifest_requires_all_file_and_forward_permissions(operator):
    s,_,key,_=operator
    s.set_access('command-agent',permissions=['send'])
    items={entry['operation']:entry for entry in s.list_messenger_operations(key,'command-fixture')['items']}
    for operation,required in [('send_file',['download','send']),('forward_message',['read','send'])]:
        assert items[operation]['required_permissions']==required
        assert items[operation]['account_enabled'] and not items[operation]['key_permitted']
        assert not items[operation]['available']


def test_terminal_file_result_survives_original_eviction(operator):
    from messaging_service.commands import CommandWorker
    s,_,key,state=operator
    content=b'Synthetic original ready for authorized send';sha=hashlib.sha256(content).hexdigest()
    s.files.mkdir(parents=True,exist_ok=True);(s.files/sha).write_bytes(content)
    aid=stable_id('att','command-fixture','100','1','evicted-file')
    with s.pool.connection() as c:
        c.execute('INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES(%s,%s,%s,%s,%s,%s)',
            (aid,stable_id('msg','command-fixture','100','1'),sha,'synthetic.txt','text/plain',len(content)))
    args={'account':'command-fixture','operation':'send_file','chat_id':stable_id('chat','command-fixture','200'),
          'parameters':{'attachment_id':aid,'sha256':sha}}
    draft=s.preview_messenger_operation(key,**args)
    first=s.execute_messenger_operation(key,**args,request_id='synthetic-evicted-send-0001',
                                       preview_digest=draft['preview_digest'])
    async def provider(job):return {'outcome':'confirmed','provider_message_id':'42'}
    assert asyncio.run(CommandWorker(state,provider).once())
    (s.files/sha).unlink()
    assert s.get_messenger_operation(key,first['operation_id'])['status']=='succeeded'
    repeated=s.execute_messenger_operation(key,**args,request_id='synthetic-evicted-send-0001',
                                           preview_digest=draft['preview_digest'])
    assert repeated['operation_id']==first['operation_id'] and repeated['status']=='succeeded'
    s.set_access('command-agent',chats=[args['chat_id']])
    with pytest.raises(APIError,match='not_found'):
        s.get_messenger_operation(key,first['operation_id'])


def test_catalog_version_change_does_not_rewrite_queued_reviewed_intent(operator,monkeypatch):
    import messaging_service.messenger_operations as catalog
    from messaging_service.commands import CommandWorker
    s,_,key,state=operator
    args=operation_args();draft=s.preview_messenger_operation(key,**args)
    first=s.execute_messenger_operation(key,**args,request_id='synthetic-operation-0001',
                                       preview_digest=draft['preview_digest'])
    monkeypatch.setattr(catalog,'CATALOG_VERSION',2)
    repeated=s.execute_messenger_operation(key,**args,request_id='synthetic-operation-0001',
                                           preview_digest=draft['preview_digest'])
    assert repeated['operation_id']==first['operation_id']
    observed=[]
    async def provider(job):observed.append(job['catalog_version']);return {'outcome':'confirmed'}
    assert asyncio.run(CommandWorker(state,provider).once())
    assert observed==[1] and s.get_messenger_operation(key,first['operation_id'])['status']=='succeeded'


def test_worker_injects_trusted_last_moment_preflight():
    from types import SimpleNamespace
    from messaging_service.commands import CommandWorker
    calls=[]
    class Worker(CommandWorker):
        def claim(self):return {'id':'synthetic-job','mutation':True,'operation':'send_file'}
        def preflight(self,job,*,verify_file=True):calls.append(('preflight',verify_file));return job
        def finish(self,*args):calls.append(('finish',args[1]))
    async def execute(job):
        await job['_preflight']()
        calls.append('provider')
        return {'outcome':'confirmed'}
    async def scenario():return await Worker(SimpleNamespace(),execute)._once()
    assert asyncio.run(scenario()) is True
    assert calls==[('preflight',True),('preflight',False),'provider',('finish','succeeded')]


def test_registered_file_worker_has_bounded_upload_timeout(monkeypatch):
    from types import SimpleNamespace
    from messaging_service.commands import CommandWorker
    timeouts=[]
    class Worker(CommandWorker):
        def claim(self):return {'id':'synthetic-job','mutation':True,'operation':'send_file'}
        def preflight(self,job,*,verify_file=True):return job
        def finish(self,*args):pass
    original=asyncio.wait_for
    async def observed(task,timeout):timeouts.append(timeout);return await original(task,timeout)
    monkeypatch.setattr(asyncio,'wait_for',observed)
    async def execute(job):return {'outcome':'confirmed'}
    assert asyncio.run(Worker(SimpleNamespace(),execute)._once())
    assert timeouts==[180]


def test_forward_source_acl_is_checked_at_preview_and_dispatch(operator):
    from messaging_service.commands import CommandWorker
    s,_,key,state=operator
    args={'account':'command-fixture','operation':'forward_message',
          'chat_id':stable_id('chat','command-fixture','100'),
          'parameters':{'source_message_id':stable_id('msg','command-fixture','200','2'),'source_revision':1}}
    draft=s.preview_messenger_operation(key,**args)
    queued=s.execute_messenger_operation(key,**args,request_id='synthetic-forward-0001',
                                         preview_digest=draft['preview_digest'])
    s.set_access('command-agent',chats=[args['chat_id']]);calls=[]
    async def provider(job):calls.append(job);return {'outcome':'confirmed'}
    asyncio.run(CommandWorker(state,provider).once())
    assert not calls
    with s.pool.connection() as c:
        assert c.execute('SELECT status FROM messenger_commands WHERE id=%s',
                         (queued['operation_id'],)).fetchone()['status']=='cancelled'


def test_send_file_revalidates_registered_content_without_accepting_paths(operator):
    from messaging_service.commands import CommandWorker
    s,_,key,state=operator
    content=b'Synthetic registered original';sha=hashlib.sha256(content).hexdigest()
    s.files.mkdir(parents=True,exist_ok=True);(s.files/sha).write_bytes(content)
    aid=stable_id('att','command-fixture','100','1','synthetic-file')
    with s.pool.connection() as c:
        c.execute('INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES(%s,%s,%s,%s,%s,%s)',
            (aid,stable_id('msg','command-fixture','100','1'),sha,'synthetic.txt','text/plain',len(content)))
    args={'account':'command-fixture','operation':'send_file','chat_id':stable_id('chat','command-fixture','200'),
          'parameters':{'attachment_id':aid,'sha256':sha}}
    draft=s.preview_messenger_operation(key,**args)
    queued=s.execute_messenger_operation(key,**args,request_id='synthetic-file-send-0001',
                                         preview_digest=draft['preview_digest'])
    (s.files/sha).write_bytes(b'Corrupt synthetic registered content');calls=[]
    async def provider(job):calls.append(job);return {'outcome':'confirmed'}
    asyncio.run(CommandWorker(state,provider).once())
    assert not calls
    with s.pool.connection() as c:
        assert c.execute('SELECT status FROM messenger_commands WHERE id=%s',
                         (queued['operation_id'],)).fetchone()['status']=='cancelled'


def test_send_file_mode_mismatch_names_field(operator):
    s,_,key,_=operator
    content=b'Synthetic text original';sha=hashlib.sha256(content).hexdigest()
    s.files.mkdir(parents=True,exist_ok=True);(s.files/sha).write_bytes(content)
    aid=stable_id('att','command-fixture','100','1','synthetic-mode')
    with s.pool.connection() as c:
        c.execute('INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES(%s,%s,%s,%s,%s,%s)',
            (aid,stable_id('msg','command-fixture','100','1'),sha,'synthetic.txt','text/plain',len(content)))
    with pytest.raises(APIError,match='^invalid_operation_parameters$') as failure:
        s.preview_messenger_operation(key,account='command-fixture',operation='send_file',
            chat_id=stable_id('chat','command-fixture','200'),parameters={'attachment_id':aid,'sha256':sha,'mode':'photo'})
    assert failure.value.field_errors==[{'field':'mode','reason':'format','expected':
        'a mode matching the original MIME family: photo and sticker image/*, video and video_note video/*, '
        'audio and voice audio/*; document for any file'}]


def registered_original(s,name):
    content=b'Synthetic silent original '+name.encode();sha=hashlib.sha256(content).hexdigest()
    s.files.mkdir(parents=True,exist_ok=True);(s.files/sha).write_bytes(content)
    aid=stable_id('att','command-fixture','100','1',name)
    with s.pool.connection() as c:
        c.execute('INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES(%s,%s,%s,%s,%s,%s)',
            (aid,stable_id('msg','command-fixture','100','1'),sha,'synthetic.txt','text/plain',len(content)))
    return aid,sha


# spec: ANYM-927
def test_a_silent_telegram_send_file_is_bound_and_an_explicit_false_is_the_usual_send(operator):
    s,_,key,_=operator
    aid,sha=registered_original(s,'synthetic-silent')
    args={'account':'command-fixture','operation':'send_file','chat_id':stable_id('chat','command-fixture','200')}
    plain={'attachment_id':aid,'sha256':sha}
    loud=s.preview_messenger_operation(key,**args,parameters=plain)
    silent=s.preview_messenger_operation(key,**args,parameters={**plain,'silent':True})
    assert silent['preview_digest']!=loud['preview_digest']
    assert s.preview_messenger_operation(key,**args,parameters={**plain,'silent':False})['preview_digest']==loud['preview_digest']
    with pytest.raises(APIError,match='preview_mismatch'):
        s.execute_messenger_operation(key,**args,parameters=plain,request_id='silent-file-send-0001',
            preview_digest=silent['preview_digest'])
    # An explicit false and an omitted silent are one request: a retry with either returns the first operation.
    first=s.execute_messenger_operation(key,**args,parameters={**plain,'silent':False},request_id='silent-file-send-0002',
        preview_digest=loud['preview_digest'])
    again=s.execute_messenger_operation(key,**args,parameters=plain,request_id='silent-file-send-0002',
        preview_digest=loud['preview_digest'])
    quiet=s.execute_messenger_operation(key,**args,parameters={**plain,'silent':True},request_id='silent-file-send-0003',
        preview_digest=silent['preview_digest'])
    assert again['operation_id']==first['operation_id']
    with s.pool.connection() as c:
        rows={r['id']:r['payload'] for r in c.execute('SELECT id,payload FROM messenger_commands').fetchall()}
    assert 'silent' not in rows[first['operation_id']]['parameters']
    assert 'silent' not in rows[first['operation_id']]['request_input']['parameters']
    assert rows[quiet['operation_id']]['parameters']['silent'] is True


# spec: ANYM-927
def test_a_whatsapp_send_file_with_silent_is_refused_silent_unsupported_by_name(seeded):
    s,_,_=seeded
    archive=Archive(s)
    archive.register('wa-files','whatsapp','Synthetic WhatsApp files',discover=True)
    archive.ingest(event(account='wa-files',platform='whatsapp',chat='fixture@c.us',provider_id='1',body='Synthetic'))
    s.set_messenger_operations('wa-files',['send_file'])
    key=s.issue_key('wa-files-agent',['wa-files'],permissions=['read','download','send'])
    args={'account':'wa-files','operation':'send_file','chat_id':stable_id('chat','wa-files','fixture@c.us'),
          'parameters':{'attachment_id':'att_'+'a'*32,'sha256':'b'*64,'silent':True}}
    for call in (lambda:s.preview_messenger_operation(key,**args),
                 lambda:s.execute_messenger_operation(key,**args,request_id='wa-silent-file-0001',preview_digest='c'*64)):
        with pytest.raises(APIError,match='^silent_unsupported$') as failure:
            call()
        assert [e['field'] for e in failure.value.field_errors]==['silent']
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n']==0


GROUP_ADMIN_TELEGRAM = ['set_group_messages_admins_only', 'set_group_info_admins_only', 'set_slow_mode',
                        'list_join_requests', 'approve_join_requests', 'reject_join_requests']


# spec: ANYM-242
def test_group_admin_operations_need_permission_and_opt_in(operator):
    s,_,key,_=operator
    def rows(token):
        return {r['operation']:r for r in s.list_messenger_operations(token,account='command-fixture')['items']}
    listed=rows(key)
    assert 'set_join_approval' not in listed
    assert all(listed[n]['key_permitted'] and not listed[n]['account_enabled'] and not listed[n]['available']
               for n in GROUP_ADMIN_TELEGRAM)
    chat=stable_id('chat','command-fixture','100')
    with pytest.raises(APIError,match='operation_disabled'):
        s.preview_messenger_operation(key,account='command-fixture',operation='set_slow_mode',chat_id=chat,
                                      parameters={'seconds':30})
    s.set_messenger_operations('command-fixture',['set_slow_mode','list_join_requests'])
    listed=rows(key)
    assert {n for n in GROUP_ADMIN_TELEGRAM if listed[n]['available']}=={'set_slow_mode','list_join_requests'}
    assert s.preview_messenger_operation(key,account='command-fixture',operation='set_slow_mode',chat_id=chat,
                                         parameters={'seconds':30})['parameters']=={'seconds':30}
    reader=s.issue_key('command-agent',['command-fixture'],permissions=['read','groups-read'])
    listed=rows(reader)
    assert listed['list_join_requests']['available'] and not listed['set_slow_mode']['available']
    assert listed['set_slow_mode']['account_enabled'] and not listed['set_slow_mode']['key_permitted']


# spec: ANYM-242
@pytest.mark.parametrize('failure',['timeout','crash'])
def test_join_request_batch_worker_interrupted_receipt_is_unknown_without_rows(operator,monkeypatch,failure):
    from messaging_service.commands import CommandWorker
    from messaging_service.telegram_operations import execute
    from test_telegram_operations import SUPERGROUP,GroupClient
    s,_,key,state=operator
    s.set_messenger_operations('command-fixture',['approve_join_requests'])
    reached=asyncio.Event()
    class Client(GroupClient):
        async def __call__(self,request):
            if getattr(getattr(request,'user_id',None),'user_id',None)==202:
                reached.set()
            return await super().__call__(request)
    client=Client()
    client.hide[202]='hang' if failure=='timeout' else RuntimeError('synthetic crash')
    original=asyncio.wait_for
    async def after_201(awaitable,timeout):
        # The worker's short deadline starts only once 201 is approved and 202's request has begun, so 201 never
        # races it (ANYM-591); 202's hang lasts 30 s, far past it.
        task=asyncio.ensure_future(awaitable)
        started=asyncio.ensure_future(reached.wait())
        await asyncio.wait({task,started},return_when=asyncio.FIRST_COMPLETED)
        started.cancel()
        return await original(task,0.5)
    monkeypatch.setattr(asyncio,'wait_for',after_201)
    async def provider(job):
        result=await execute(client,{**job,'provider_chat':SUPERGROUP})
        if failure=='crash':raise RuntimeError('synthetic worker crash after the provider answered')
        return result
    args=dict(operation='approve_join_requests',message_id='',expected_revision=0,parameters={'user_ids':['201','202']})
    queued=queue(s,key,**args)
    worker=CommandWorker(state,provider)
    assert asyncio.run(worker.once())
    receipt=s.get_messenger_operation(key,queued['operation_id'])
    assert receipt['status']=='unknown' and receipt['result'] is None
    assert receipt['error_code']=='operation_result_unknown'
    assert receipt['next_action']=='verify_provider_state_before_any_new_action'
    assert [c.user_id.user_id for c in client.mutations()]==[201,202]
    # The same request_id returns that receipt; nothing is dispatched again.
    again=queue(s,key,**args)
    assert again['operation_id']==queued['operation_id'] and again['status']=='unknown'
    assert not asyncio.run(worker.once()) and len(client.mutations())==2


# spec: ANYM-242
@pytest.mark.parametrize('failure,status',[('refused','succeeded'),('503','unknown')])
def test_join_request_batch_worker_receipt_keeps_rows(operator,failure,status):
    from telethon.errors import HideRequesterMissingError,RPCError
    from messaging_service.commands import CommandWorker
    from messaging_service.telegram_operations import execute
    from test_telegram_operations import SUPERGROUP,GroupClient
    s,_,key,state=operator
    s.set_messenger_operations('command-fixture',['approve_join_requests'])
    client=GroupClient()
    client.hide[202]=HideRequesterMissingError(None) if failure=='refused' else RPCError(None,'TIMEOUT',503)
    async def provider(job):return await execute(client,{**job,'provider_chat':SUPERGROUP})
    queued=queue(s,key,operation='approve_join_requests',message_id='',expected_revision=0,
                 parameters={'user_ids':['201','202','203']})
    assert asyncio.run(CommandWorker(state,provider).once())
    receipt=s.get_messenger_operation(key,queued['operation_id'])
    assert receipt['status']==status and receipt['result']['data']['all_applied'] is False
    assert [r['state'] for r in receipt['result']['data']['results']]==(
        ['applied','refused','applied'] if failure=='refused' else ['applied','unknown','not_attempted'])


# spec: ANYM-242
@pytest.mark.parametrize('operation',['approve_join_requests','reject_join_requests'])
def test_join_request_batch_worker_first_id_revoked_keeps_rows(operator,operation):
    from messaging_service.commands import CommandWorker
    from messaging_service.telegram_operations import execute
    from test_telegram_operations import SUPERGROUP,GroupClient
    s,_,key,state=operator
    s.set_messenger_operations('command-fixture',[operation])
    class Revoking(GroupClient):
        async def get_input_entity(self,peer):
            # The owner withdraws the opt-in after admission, before the first mutation.
            if peer==201:s.set_messenger_operations('command-fixture',[])
            return await super().get_input_entity(peer)
    client=Revoking()
    async def provider(job):return await execute(client,{**job,'provider_chat':SUPERGROUP})
    queued=queue(s,key,operation=operation,message_id='',expected_revision=0,parameters={'user_ids':['201','202']})
    assert asyncio.run(CommandWorker(state,provider).once())
    receipt=s.get_messenger_operation(key,queued['operation_id'])
    assert receipt['status']=='unknown' and client.mutations()==[]
    assert receipt['result']['data']=={'all_applied':False,'results':[
        {'id':'201','state':'not_attempted'},{'id':'202','state':'not_attempted'}]}


LINK='https://t.me/+AbCd1234'
JOINED_CHAT='-1000000000500'


def link_data(membership='not_member',title='Synthetic link chat'):
    return {'title':title,'kind':'group','member_count':12,'approval_required':True,'paid':False,
            'membership':membership,'membership_basis':'provider_invite'}


def link_check(s,key,state,membership='not_member',request_id='synthetic-check-0001',title='Synthetic link chat'):
    # The worker answers the check directly, so a queued join is never claimed by it.
    from psycopg.types.json import Jsonb
    queued=s.execute_messenger_operation(key,account='command-fixture',operation='check_chat_link',
                                         parameters={'link':LINK},request_id=request_id)
    with s.pool.connection() as c:
        c.execute('''UPDATE messenger_commands SET status='succeeded',result=%s,started_at=clock_timestamp(),
            finished_at=clock_timestamp() WHERE id=%s''',
            (Jsonb({'outcome':'confirmed','data':link_data(membership,title)}),queued['operation_id']))
    return s.get_messenger_operation(key,queued['operation_id'])


def join_queue(s,key,request_id='synthetic-join-0001'):
    args={'account':'command-fixture','operation':'join_chat','parameters':{'link':LINK}}
    draft=s.preview_messenger_operation(key,**args)
    return s.execute_messenger_operation(key,**args,request_id=request_id,preview_digest=draft['preview_digest'])


def link_keys(s):
    perms=['read','groups-read','groups-manage']
    return s.issue_key('command-agent',['command-fixture'],permissions=perms),s.issue_key('command-agent',['command-fixture'],permissions=perms)


# spec: ANYM-243
def test_join_requires_fresh_check(operator):
    s,_,key,state=operator
    s.set_messenger_operations('command-fixture',['check_chat_link','join_chat'])
    args={'account':'command-fixture','operation':'join_chat','parameters':{'link':LINK}}
    with pytest.raises(APIError,match='preview_required'):s.preview_messenger_operation(key,**args)
    other,_=link_keys(s)
    link_check(s,other,state,request_id='synthetic-check-other')
    with pytest.raises(APIError,match='preview_required'):s.preview_messenger_operation(key,**args)
    link_check(s,key,state,'member',request_id='synthetic-check-0001')
    with pytest.raises(APIError,match='already_member'):s.preview_messenger_operation(key,**args)
    receipt=link_check(s,key,state,request_id='synthetic-check-0002')
    assert receipt['status']=='succeeded'
    with s.pool.connection() as c:
        c.execute("UPDATE messenger_commands SET finished_at=clock_timestamp()-interval '61 minutes' WHERE operation='check_chat_link'")
    with pytest.raises(APIError,match='preview_required'):s.preview_messenger_operation(key,**args)
    link_check(s,key,state,request_id='synthetic-check-0003')
    draft=s.preview_messenger_operation(key,**args)
    assert draft['parameters']=={'link':'+AbCd1234'}
    assert draft['expected_chat']=={'title':'Synthetic link chat','kind':'group','member_count':12}
    with pytest.raises(APIError,match='preview_required'):
        s.execute_messenger_operation(key,**args,request_id='synthetic-join-0001')
    # A later check naming another chat changes the digest the owner confirmed.
    link_check(s,key,state,request_id='synthetic-check-0004',title='Synthetic renamed chat')
    with pytest.raises(APIError,match='preview_mismatch'):
        s.execute_messenger_operation(key,**args,request_id='synthetic-join-0001',preview_digest=draft['preview_digest'])
    assert join_queue(s,key)['status']=='queued'


# spec: ANYM-243
def test_join_target_changed(operator):
    from messaging_service.commands import CommandWorker
    from messaging_service.telegram_operations import execute
    from test_telegram_operations import LinkClient
    from telethon.tl import types
    s,_,key,state=operator
    s.set_messenger_operations('command-fixture',['check_chat_link','join_chat'])
    link_check(s,key,state)
    queued=join_queue(s,key)
    client=LinkClient(invite=types.ChatInvite(title='Synthetic renamed chat',photo=types.PhotoEmpty(0),
                                              participants_count=12,color=0,megagroup=True))
    async def provider(job):return await execute(client,job)
    assert asyncio.run(CommandWorker(state,provider).once())
    receipt=s.get_messenger_operation(key,queued['operation_id'])
    assert receipt['status']=='failed' and receipt['error_code']=='operation_target_changed' and client.joins()==[]


# spec: ANYM-243
@pytest.mark.parametrize('change',['opt_in','permission','expiry'])
def test_join_dispatch_rechecks_access(operator,change):
    from messaging_service.commands import CommandWorker
    s,_,key,state=operator
    s.set_messenger_operations('command-fixture',['check_chat_link','join_chat'])
    link_check(s,key,state)
    queued=join_queue(s,key)
    if change=='opt_in':s.set_messenger_operations('command-fixture',['check_chat_link'])
    if change=='permission':s.set_key_permissions(s.capabilities(key)['credential_id'],['read','groups-read'])
    if change=='expiry':
        with s.pool.connection() as c:
            c.execute("UPDATE messenger_commands SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                      (queued['operation_id'],))
    calls=[]
    async def provider(job):calls.append(job);return {'outcome':'confirmed','data':{'state':'joined'}}
    asyncio.run(CommandWorker(state,provider).once())
    with s.pool.connection() as c:
        row=c.execute('SELECT status FROM messenger_commands WHERE id=%s',(queued['operation_id'],)).fetchone()
    assert calls==[] and row['status'] in ('failed','expired','cancelled')


# spec: ANYM-243
def test_unknown_join_guard(operator):
    s,_,key,state=operator
    s.set_messenger_operations('command-fixture',['check_chat_link','join_chat'])
    other,_=link_keys(s)
    link_check(s,key,state)
    first=join_queue(s,key)
    link_check(s,other,state,request_id='synthetic-check-other-1')
    with pytest.raises(APIError,match='operation_result_unknown'):join_queue(s,other,'synthetic-join-other-1')
    with s.pool.connection() as c:
        c.execute("UPDATE messenger_commands SET status='dispatching' WHERE id=%s",(first['operation_id'],))
    with pytest.raises(APIError,match='operation_result_unknown'):join_queue(s,other,'synthetic-join-other-2')
    with s.pool.connection() as c:
        c.execute("""UPDATE messenger_commands SET status='unknown',error_code='operation_result_unknown',
            finished_at=clock_timestamp() WHERE id=%s""",(first['operation_id'],))
    with pytest.raises(APIError,match='operation_result_unknown'):join_queue(s,other,'synthetic-join-other-3')
    # A check from before the join ended (other-1) and a later unknown one keep the guard, with no time limit.
    link_check(s,key,state,'unknown',request_id='synthetic-check-0002')
    with s.pool.connection() as c:
        c.execute("UPDATE messenger_commands SET finished_at=finished_at-interval '30 days' WHERE id=%s",(first['operation_id'],))
        c.execute("UPDATE messenger_commands SET started_at=clock_timestamp()-interval '31 days' WHERE operation='check_chat_link'")
    with pytest.raises(APIError,match='operation_result_unknown'):join_queue(s,other,'synthetic-join-other-4')
    # A later check by any key that settles membership ends it.
    link_check(s,key,state,request_id='synthetic-check-0003')
    assert join_queue(s,other,'synthetic-join-other-5')['status']=='queued'
    # The same request_id still returns its own receipt.
    assert join_queue(s,key)['operation_id']==first['operation_id']


# spec: ANYM-243
def test_join_guard_concurrent_keys(operator):
    import threading
    s,_,_,state=operator
    s.set_messenger_operations('command-fixture',['check_chat_link','join_chat'])
    keys=link_keys(s)
    drafts=[]
    for n,token in enumerate(keys):
        link_check(s,token,state,request_id='synthetic-check-con-%d'%n)
        drafts.append(s.preview_messenger_operation(token,account='command-fixture',operation='join_chat',
                                                    parameters={'link':LINK}))
    barrier=threading.Barrier(2);results=[]
    def submit(n):
        barrier.wait()
        try:results.append(s.execute_messenger_operation(keys[n],account='command-fixture',operation='join_chat',
                parameters={'link':LINK},request_id='synthetic-join-con-%d'%n,preview_digest=drafts[n]['preview_digest'])['status'])
        except APIError as exc:results.append(exc.code)
    threads=[threading.Thread(target=submit,args=(n,)) for n in range(2)]
    for t in threads:t.start()
    for t in threads:t.join(10)
    assert sorted(results)==['operation_result_unknown','queued']


# spec: ANYM-243
def test_join_guard_after_restart(operator):
    from messaging_service.commands import CommandWorker
    s,_,key,state=operator
    s.set_messenger_operations('command-fixture',['check_chat_link','join_chat'])
    other,_=link_keys(s)
    link_check(s,key,state)
    first=join_queue(s,key)
    calls=[]
    async def crash(job):
        calls.append(job)
        # The provider joined; the process dies before the receipt is written.
        with s.pool.connection() as c:
            c.execute("UPDATE messenger_commands SET lease_epoch=0 WHERE id=%s",(first['operation_id'],))
        raise SystemExit
    with pytest.raises(SystemExit):asyncio.run(CommandWorker(state,crash).once())
    assert len(calls)==1
    link_check(s,other,state,request_id='synthetic-check-other-1')
    receipt=s.get_messenger_operation(key,first['operation_id'])
    assert receipt['status']=='unknown'
    with pytest.raises(APIError,match='operation_result_unknown'):join_queue(s,other,'synthetic-join-other-1')
    assert len(calls)==1


def unreadable(s,token,chat):
    try:return not s.get_messages(token,chat)['items']
    except APIError as exc:return exc.code=='not_found'


def joined_by_worker(s,key,state,client):
    from messaging_service.commands import CommandWorker
    from messaging_service.telegram_operations import execute
    s.set_messenger_operations('command-fixture',['check_chat_link','join_chat'])
    link_check(s,key,state)
    queued=join_queue(s,key)
    async def provider(job):return await execute(client,job)
    assert asyncio.run(CommandWorker(state,provider).once())
    return s.get_messenger_operation(key,queued['operation_id'])


# spec: ANYM-243
def test_join_scope_chat_scoped_key(operator):
    from test_telegram_operations import LinkClient
    s,archive,key,state=operator
    scoped=s.issue_key('scoped-agent',['command-fixture'],chats=[stable_id('chat','command-fixture','100')],
                       permissions=['read','read-live','groups-read','groups-manage'])
    joined=stable_id('chat','command-fixture',JOINED_CHAT)
    def scoped_cannot_reach():
        assert unreadable(s,scoped,joined)
        with pytest.raises(APIError,match='not_found'):
            s.preview_messenger_operation(scoped,account='command-fixture',operation='list_participants',chat_id=joined)
        assert all(r['chat_id']!=joined for r in s.search(scoped,'Joined synthetic')['items'])
    scoped_cannot_reach()
    receipt=joined_by_worker(s,key,state,LinkClient())
    assert receipt['status']=='succeeded' and receipt['result']['data']['archive']=='collected'
    archive.ingest(event(account='command-fixture',chat=JOINED_CHAT,provider_id='5',body='Joined synthetic text'))
    scoped_cannot_reach()
    assert not unreadable(s,key,joined)


# spec: ANYM-243
def test_join_scope_collection_policy(operator):
    from test_telegram_operations import LinkClient
    s,_,key,state=operator
    with s.pool.connection() as c:
        c.execute('UPDATE account_policies SET exclude_chats=%s WHERE account=%s',([JOINED_CHAT],'command-fixture'))
    receipt=joined_by_worker(s,key,state,LinkClient())
    data=receipt['result']['data']
    assert receipt['status']=='succeeded' and data['state']=='joined' and data['archive']=='not_collected'
    assert 'provider_chat_id' not in data and 'collection policy' in data['next_step']
    assert unreadable(s,key,stable_id('chat','command-fixture',JOINED_CHAT))


# spec: ANYM-243
def test_failed_join_preserves_send_and_collection(operator):
    from messaging_service.commands import CommandWorker
    from messaging_service.telegram_operations import execute
    from test_telegram_operations import LinkClient
    from telethon import errors
    s,archive,key,state=operator
    s.set_messenger_operations('command-fixture',['check_chat_link','join_chat','send_reaction'])
    link_check(s,key,state)
    failed=join_queue(s,key)
    client=LinkClient(join=errors.InviteHashExpiredError(None))
    async def provider(job):
        if job['operation']=='join_chat':return await execute(client,job)
        return {'outcome':'confirmed','data':{}}
    worker=CommandWorker(state,provider)
    assert asyncio.run(worker.once())
    assert s.get_messenger_operation(key,failed['operation_id'])['error_code']=='invite_expired'
    # A malformed link is refused before queueing.
    with pytest.raises(APIError,match='invalid_operation_parameters'):
        s.preview_messenger_operation(key,account='command-fixture',operation='join_chat',parameters={'link':'bare_name'})
    sent=queue(s,key,operation='send_reaction',parameters={'emoji':'👍'},expected_revision=1)
    assert asyncio.run(worker.once())
    assert s.get_messenger_operation(key,sent['operation_id'])['status']=='succeeded'
    archive.ingest(event(account='command-fixture',chat='100',provider_id='3',body='Still collected synthetic text'))
    assert any(m['body']=='Still collected synthetic text' for m in
               s.get_messages(key,stable_id('chat','command-fixture','100'))['items'])


class NativeLane:
    """Synthetic native pipe: keeps every serialized frame and answers with one frame."""
    def __init__(self,answer):self.frames,self.answer,self.reader=[],answer,None
    def write(self,data):
        import json
        frame=json.loads(data);self.frames.append(frame)
        self.reader.feed_data(json.dumps({'id':frame['id'],**self.answer}).encode()+b'\n')
    async def drain(self):pass


# spec: ANYM-243
@pytest.mark.parametrize('code',['invite_invalid','invite_expired','chat_full','join_not_allowed',
                                 'already_member','operation_target_changed'])
def test_native_join_carries_confirmed_chat_and_definite_refusal(operator,tmp_path,code):
    from types import SimpleNamespace
    from messaging_service.commands import CommandWorker
    from messaging_service.whatsapp_collector import NodeProvider
    s,_,key,state=operator
    s.set_messenger_operations('command-fixture',['check_chat_link','join_chat'])
    link_check(s,key,state)
    queued=join_queue(s,key)
    lane=NativeLane({'error':code})
    native=NodeProvider({'events_directory':str(tmp_path/'events')},tmp_path/'unused',None)
    native.process,native.send_writer=SimpleNamespace(returncode=None),lane
    async def provider(job):
        native.send_reader=lane.reader=asyncio.StreamReader()
        async with native.send_gate:return await native.execute_operation(job)
    assert asyncio.run(CommandWorker(state,provider).once())
    (frame,)=lane.frames
    assert frame['method']=='messenger_operation'
    assert frame['params']['expected_chat']=={'title':'Synthetic link chat','kind':'group','member_count':12}
    receipt=s.get_messenger_operation(key,queued['operation_id'])
    assert receipt['status']=='failed' and receipt['error_code']==code
    # A definite refusal leaves no unknown-join guard behind.
    link_check(s,key,state,request_id='synthetic-check-0002')
    assert join_queue(s,key,'synthetic-join-0002')['status']=='queued'


CREATED_CHAT='-5000000023'


def collector_worker(state,client):
    """The Telegram collector's operation path (catalog of a new chat included) over a synthetic client."""
    from types import SimpleNamespace
    from messaging_service.commands import CommandWorker
    from messaging_service.telegram_collector import TelegramCollector
    from messaging_service.telegram_operations import execute
    collector=TelegramCollector.__new__(TelegramCollector)
    async def provider(job):return await execute(client,job)
    collector.state,collector.account,collector.history_since=state,'command-fixture',None
    collector.provider=SimpleNamespace(execute_operation=provider)
    return CommandWorker(state,collector.execute_operation)


def created_by_worker(s,key,state,run=asyncio.run):
    from datetime import datetime,timezone
    from telethon.tl import functions,types
    from test_telegram_operations import Client
    s.set_messenger_operations('command-fixture',['create_group'])
    args={'account':'command-fixture','operation':'create_group',
          'parameters':{'title':'Synthetic new group','user_ids':['200']}}
    draft=s.preview_messenger_operation(key,**args)
    queued=s.execute_messenger_operation(key,**args,request_id='synthetic-create-0001',
                                         preview_digest=draft['preview_digest'])
    client=Client()
    created=types.Chat(int(CREATED_CHAT[1:]),'Synthetic new group',types.ChatPhotoEmpty(),2,datetime.now(timezone.utc),1)
    client.responses[functions.messages.CreateChatRequest]=types.messages.InvitedUsers(
        types.Updates([],[],[created],datetime.now(timezone.utc),0),[])
    assert run(collector_worker(state,client).once())
    return s.get_messenger_operation(key,queued['operation_id'])


# spec: ANYM-171
def test_created_group_is_in_the_archive_at_once_and_its_receipt_names_the_chat(operator):
    # A new basic group's only message is a service message, which live capture skips; the group used to
    # wait for the next dialog discovery, so resolve_reference found nothing and nothing could be sent to it.
    s,_,key,state=operator
    receipt=created_by_worker(s,key,state)
    data=receipt['result']['data']
    chat=stable_id('chat','command-fixture',CREATED_CHAT)
    assert receipt['status']=='succeeded' and data['provider_chat_id']==CREATED_CHAT and data['chat_id']==chat
    resolved=s.resolve_reference(key,'command-fixture',CREATED_CHAT)['chat']
    assert resolved['id']==chat and resolved['title']=='Synthetic new group'
    assert chat in {c['id'] for c in s.list_chats(key,accounts=['command-fixture'],order='id',limit=100)['items']}


# spec: ANYM-171
def test_a_failed_catalog_write_keeps_the_created_group_confirmed(operator,monkeypatch):
    s,_,key,state=operator
    def broken(*args):raise RuntimeError('synthetic catalog failure')
    monkeypatch.setattr(CollectorState,'catalog',broken)
    receipt=created_by_worker(s,key,state)
    data=receipt['result']['data']
    assert receipt['status']=='succeeded' and data['provider_chat_id']==CREATED_CHAT and 'chat_id' not in data


# spec: ANYM-171
def test_a_created_group_the_collection_policy_leaves_out_gets_no_archive_chat(operator):
    s,_,key,state=operator
    with s.pool.connection() as c:
        c.execute('UPDATE account_policies SET exclude_chats=%s WHERE account=%s',([CREATED_CHAT],'command-fixture'))
    receipt=created_by_worker(s,key,state)
    assert receipt['status']=='succeeded' and 'chat_id' not in receipt['result']['data']
    with pytest.raises(APIError,match='not_found'):s.resolve_reference(key,'command-fixture',CREATED_CHAT)


# spec: ANYM-243
def test_a_joined_chat_is_in_the_archive_at_once_and_its_receipt_names_the_chat(operator):
    from test_telegram_operations import LinkClient
    s,_,key,state=operator
    s.set_messenger_operations('command-fixture',['check_chat_link','join_chat'])
    link_check(s,key,state)
    queued=join_queue(s,key)
    assert asyncio.run(collector_worker(state,LinkClient()).once())
    data=s.get_messenger_operation(key,queued['operation_id'])['result']['data']
    chat=stable_id('chat','command-fixture',JOINED_CHAT)
    assert data['archive']=='collected' and data['chat_id']==chat
    assert s.resolve_reference(key,'command-fixture',JOINED_CHAT)['chat']['id']==chat


# spec: ANYM-171
def test_a_slow_catalog_write_is_cut_off_and_keeps_the_created_group_confirmed(operator,monkeypatch):
    import time
    from messaging_service import telegram_collector
    s,_,key,state=operator
    monkeypatch.setattr(telegram_collector,'CATALOG_SECONDS',0.2)
    def slow(*args):time.sleep(4);return True
    monkeypatch.setattr(CollectorState,'catalog',slow)
    receipt=created_by_worker(s,key,state)
    assert receipt['status']=='succeeded' and 'chat_id' not in receipt['result']['data']
    with s.pool.connection() as c:
        took=c.execute("SELECT extract(epoch FROM finished_at-started_at) AS s FROM messenger_commands WHERE id=%s",
                       (receipt['operation_id'],)).fetchone()['s']
    # The receipt is written once the budget and the thread grace are spent, not after the slow write.
    assert took<telegram_collector.STORE_GRACE+1.5


# spec: ANYM-171
def test_a_shutdown_during_the_catalog_write_settles_the_created_group_before_it_stops(operator,monkeypatch):
    # The provider already created the group; a collector shutdown at that point used to leave the receipt
    # dispatching, and recovery then read the confirmed change as unknown.
    import threading
    import time
    s,_,key,state=operator
    started=threading.Event()
    def slow(*args):started.set();time.sleep(.5);return True
    monkeypatch.setattr(CollectorState,'catalog',slow)
    async def cancel_during_catalog(once):
        task=asyncio.ensure_future(once)
        await asyncio.to_thread(started.wait,10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        return True
    receipt=created_by_worker(s,key,state,lambda once:asyncio.run(cancel_during_catalog(once)))
    assert receipt['status']=='succeeded' and receipt['result']['data']['provider_chat_id']==CREATED_CHAT


# spec: ANYM-171
def test_a_collector_checkpoint_without_an_archive_chat_gives_no_chat_id(operator):
    # Live capture writes the checkpoint before the chats row; the receipt names only a chat the archive holds.
    s,_,key,state=operator
    with s.pool.connection() as c:
        c.execute('INSERT INTO collector_chats(account,provider_chat,peer_kind) VALUES(%s,%s,%s)',
                  ('command-fixture',CREATED_CHAT,'chat'))
    receipt=created_by_worker(s,key,state)
    assert receipt['status']=='succeeded' and 'chat_id' not in receipt['result']['data']


# spec: ANYM-171
def test_a_tracked_chat_the_policy_now_leaves_out_gives_no_chat_id(operator):
    s,_,key,state=operator
    state.inventory([(CREATED_CHAT,'chat','Synthetic old title','group')],None)
    with s.pool.connection() as c:
        c.execute('UPDATE account_policies SET exclude_chats=%s WHERE account=%s',([CREATED_CHAT],'command-fixture'))
    receipt=created_by_worker(s,key,state)
    assert receipt['status']=='succeeded' and 'chat_id' not in receipt['result']['data']


# --- ANYM-681: inspect_message_buttons --------------------------------------------------------------------


def run_live_job(worker):
    """One worker turn, then the off-path receipt write of a live read (settled in the same loop)."""
    async def turn():
        assert await worker.once()
        await asyncio.gather(*list(worker.settling))
    asyncio.run(turn())



# spec: ANYM-681
def test_inspect_message_buttons_is_a_telegram_only_read_live_message_operation():
    from messaging_service.messenger_operations import operation_manifest
    rows = {p: {r['operation']: r for r in operation_manifest(p)}['inspect_message_buttons'] for p in ('telegram', 'whatsapp')}
    assert rows['telegram']['supported'] is True and rows['whatsapp']['supported'] is False
    for row in rows.values():
        assert (row['permission'], row['target'], row['mutation']) == ('read-live', 'message', False)
    assert 'every fp is stale' in rows['telegram']['limitation'] and '300 per UTC day' in rows['telegram']['limitation']


# spec: ANYM-681
def test_inspection_needs_the_account_opt_in_and_the_read_live_permission(operator):
    s, archive, key, state = operator
    args = operation_args(operation='inspect_message_buttons')
    with pytest.raises(APIError, match='operation_disabled'):
        s.execute_messenger_operation(key, **args, request_id='synthetic-inspect-0001')
    s.set_messenger_operations('command-fixture', ['inspect_message_buttons', 'get_message_info'])
    reader = s.issue_key('command-reader', ['command-fixture'], permissions=['read'])
    with pytest.raises(APIError, match='forbidden'):
        s.execute_messenger_operation(reader, **args, request_id='synthetic-inspect-0002')
    queued = s.execute_messenger_operation(key, **args, request_id='synthetic-inspect-0003')
    assert queued['operation_id']


# spec: ANYM-681
def test_inspection_runs_as_a_live_read_and_stores_a_bounded_receipt(operator):
    from messaging_service.commands import CommandWorker
    s, _, key, state = operator
    s.set_messenger_operations('command-fixture', ['inspect_message_buttons'])
    queued = s.execute_messenger_operation(key, **operation_args(operation='inspect_message_buttons'),
                                           request_id='synthetic-inspect-0004')
    rows = [[{'row': 0, 'column': 0, 'kind': 'callback', 'text': 'Pay', 'fp': 'a' * 32}]]

    async def provider(job):
        assert job['operation'] == 'inspect_message_buttons' and job['provider_message_id'] == '1'
        return {'outcome': 'confirmed', 'data': {'edit_date': '2026-10-01T12:00:00+00:00', 'text_sha256': '0' * 64, 'rows': rows}}
    run_live_job(CommandWorker(state, provider))
    done = s.get_messenger_operation(key, queued['operation_id'])
    assert done['status'] == 'succeeded' and done['result']['data']['rows'] == rows


# spec: ANYM-681
def test_inspection_names_no_archive_revision_and_a_given_one_must_be_current(operator):
    from messaging_service.commands import CommandWorker
    s, _, key, state = operator
    s.set_messenger_operations('command-fixture', ['inspect_message_buttons'])
    args = operation_args(operation='inspect_message_buttons')
    del args['expected_revision']  # the documented request carries none
    preview = s.preview_messenger_operation(key, **args)
    assert preview['expected_revision'] == 0 and preview['mutation'] is False
    queued = s.execute_messenger_operation(key, **args, request_id='synthetic-inspect-0010')
    seen = []

    async def provider(job):
        seen.append((job['expected_revision'], job['provider_message_id']))
        return {'outcome': 'confirmed', 'data': {'edit_date': None, 'text_sha256': '0' * 64, 'rows': []}}
    run_live_job(CommandWorker(state, provider))  # worker preflight re-resolves the saved request
    assert s.get_messenger_operation(key, queued['operation_id'])['status'] == 'succeeded' and seen == [(0, '1')]
    with pytest.raises(APIError, match='operation_target_changed'):
        s.preview_messenger_operation(key, **{**args, 'expected_revision': 2})
    elsewhere = {**args, 'message_id': stable_id('msg', 'command-fixture', '200', '2')}
    with pytest.raises(APIError, match='not_found'):  # still bound to the named chat
        s.preview_messenger_operation(key, **elsewhere)


# spec: ANYM-681
def test_inspection_sentinels_reach_no_receipt_audit_payload_or_log_on_success_or_failure(operator, monkeypatch, caplog):
    import json
    import logging
    from datetime import datetime, timezone
    from types import SimpleNamespace as NS
    from telethon import types
    from messaging_service import button_fingerprint
    from messaging_service.commands import CommandRejected, CommandWorker
    from messaging_service.telegram_operations import execute
    s, _, key, state = operator
    caplog.set_level(logging.DEBUG)
    secret, mac_key = b'SYNTHETIC-CALLBACK-DATA-9147', bytes(range(32))
    monkeypatch.setattr(button_fingerprint, '_KEY', mac_key)
    markup = types.ReplyInlineMarkup(rows=[types.KeyboardInlineButtonRow(buttons=[types.KeyboardInlineButton(
        text='Pay', type=types.InlineButtonTypeCallback(data=secret))])])
    message = NS(id=1, chat_id=100, message='hello', out=False, entities=[], reply_markup=markup,
                 date=datetime(2026, 10, 14, tzinfo=timezone.utc), edit_date=None, media=None)

    class Fake:
        broken = False

        async def get_input_entity(self, peer):
            return types.InputPeerUser(peer, 1)

        async def get_messages(self, peer, *, ids):
            if self.broken:
                raise RuntimeError('provider said ' + secret.decode() + mac_key.hex())
            return message

    client = Fake()

    async def provider(job):
        return await execute(client, {**job, '_policy': {'enabled': True, 'discover_chats': True,
                                                         'include_chats': [], 'exclude_chats': []}})
    s.set_messenger_operations('command-fixture', ['inspect_message_buttons'])
    args = operation_args(operation='inspect_message_buttons')
    ok = s.execute_messenger_operation(key, **args, request_id='synthetic-inspect-0011')
    run_live_job(CommandWorker(state, provider))
    done = s.get_messenger_operation(key, ok['operation_id'])
    assert done['status'] == 'succeeded' and done['result']['data']['rows'][0][0]['fp']
    client.broken = True
    fail = s.execute_messenger_operation(key, **args, request_id='synthetic-inspect-0012')
    run_live_job(CommandWorker(state, provider))
    assert s.get_messenger_operation(key, fail['operation_id'])['status'] == 'failed'
    with pytest.raises(CommandRejected) as refused:  # the chain of the fixed code carries nothing either
        asyncio.run(provider({'operation': 'inspect_message_buttons', 'parameters': {}, 'provider_chat': '100',
                              'provider_message_id': '1', 'expires_at': 2 ** 40}))
    import traceback
    chain = ''.join(traceback.format_exception(refused.value))
    with s.pool.connection() as c:
        stored = json.dumps([c.execute('SELECT row_to_json(t)::text AS r FROM messenger_commands t').fetchall(),
                             c.execute('SELECT row_to_json(t)::text AS r FROM audit_log t').fetchall()], default=str)
    everything = stored + caplog.text + chain + json.dumps(done)
    for sentinel in (secret.decode(), secret.hex(), mac_key.hex(), repr(mac_key), repr(secret)):
        assert sentinel not in everything


# spec: ANYM-681
def test_the_worker_bounds_the_inspection_as_a_live_read_and_a_timeout_is_safe_to_repeat(monkeypatch):
    import messaging_service.commands as commands
    from types import SimpleNamespace
    from messaging_service.commands import CommandWorker, live_read
    from test_live_search import once
    assert live_read('inspect_message_buttons', 'telegram') is True and live_read('inspect_message_buttons', 'whatsapp') is False
    finished = []

    class Worker(CommandWorker):
        def claim(self):
            return {'id': 'synthetic-job', 'mutation': False, 'operation': 'inspect_message_buttons', 'platform': 'telegram'}

        def preflight(self, job, *, verify_file=True):
            return job

        def finish_live(self, job, status, result, code, budget):
            finished.append((status, result, code))
    monkeypatch.setattr(commands, 'LIVE_SEARCH_TIMEOUT', 0.05)
    monkeypatch.setattr(commands, 'LIVE_FOREGROUND_RESERVE', 0)

    async def hangs(job):
        await asyncio.sleep(60)
    assert once(Worker(SimpleNamespace(), hangs))
    assert finished[-1] == ('failed', {'data': {'code': 'provider_timeout', 'retry': True}}, 'provider_timeout')


# spec: ANYM-681
def test_a_stalled_receipt_write_does_not_hold_the_send_loop(monkeypatch):
    import threading
    import time
    from types import SimpleNamespace
    import messaging_service.commands as commands
    from messaging_service.commands import CommandWorker
    release = threading.Event()
    started = []

    class Worker(CommandWorker):
        def claim(self):
            return {'id': 'synthetic-job', 'mutation': False, 'operation': 'inspect_message_buttons', 'platform': 'telegram'}

        def preflight(self, job, *, verify_file=True):
            return job

        def finish_live(self, job, status, result, code, budget):
            started.append(status)
            release.wait(5)
    monkeypatch.setattr(commands, 'LIVE_SEARCH_TIMEOUT', 5)

    async def answer(job):
        return {'outcome': 'confirmed', 'data': {'rows': []}}

    async def turn():
        worker = Worker(SimpleNamespace(), answer)
        began = time.monotonic()
        assert await worker._once()
        elapsed = time.monotonic() - began
        release.set()
        await asyncio.gather(*list(worker.settling))
        return elapsed
    assert asyncio.run(turn()) < 2 and started == ['succeeded']


# --- ANYM-676: press_button -------------------------------------------------------------------------------------
PRESS_LAYOUT = {'rows': [[{'text': 'Pay', 'kind': 'callback'}, {'text': 'Site', 'kind': 'url', 'url': 'https://example.invalid/a'}],
                         [{'text': 'Cancel', 'kind': 'callback'}]]}
FP = 'a' * 32


@pytest.fixture
def pressing(operator):
    s, archive, key, state = operator

    def bot_message(provider_id='5', body='Pay now', updated='2026-10-01T12:00:00+00:00', buttons=PRESS_LAYOUT):
        metadata = {'sender_is_bot': True, **({'buttons': buttons} if buttons else {})}
        result = archive.ingest(event(account='command-fixture', chat='100', provider_id=provider_id, sender='555',
                                      direction='incoming', body=body, sent_at='2026-10-01T12:00:00+00:00',
                                      updated_at=updated, metadata=metadata))
        return result['message']

    s.set_messenger_operations('command-fixture', ['press_button', 'get_message_info'])
    presser = s.issue_key('command-presser', ['command-fixture'], permissions=['read', 'bot-press'])
    return s, archive, presser, state, bot_message


def press_args(message_id, revision=1, **parameters):
    return {'account': 'command-fixture', 'operation': 'press_button', 'chat_id': stable_id('chat', 'command-fixture', '100'),
            'message_id': message_id, 'expected_revision': revision,
            'parameters': {'row': 0, 'column': 0, 'label': 'Pay', 'fp': FP, **parameters}}


def press_now(s, key, args, request_id):
    draft = s.preview_messenger_operation(key, **args)
    return s.execute_messenger_operation(key, **args, request_id=request_id, preview_digest=draft['preview_digest'])


# spec: ANYM-676
def test_the_press_preview_refuses_definitely_with_its_own_code_and_makes_no_provider_call(pressing):
    from messaging_service.messenger_operations import validate_operation
    s, _, key, _, bot_message = pressing
    mid = bot_message()
    plain = bot_message('6', buttons=None)
    cases = [(press_args(plain), 'button_not_captured', {}), (press_args(mid, row=3), 'button_not_found', {}),
             (press_args(mid, column=5), 'button_not_found', {}),
             (press_args(mid, label='Buy'), 'label_mismatch', {'archived_label': 'Pay'}),
             (press_args(mid, column=1, label='Site'), 'button_kind_unsupported', {'kind': 'url'})]
    for args, code, extra in cases:
        for call, extra_args in ((s.preview_messenger_operation, {}),
                                 (s.execute_messenger_operation, {'request_id': 'synthetic-press-0000'})):
            with pytest.raises(APIError) as refused:
                call(key, **args, **extra_args)
            assert refused.value.code == code and getattr(refused.value, 'next_action', '')
            assert all(getattr(refused.value, name) == value for name, value in extra.items())
    with pytest.raises(ValueError, match='unsupported_operation'):
        validate_operation('whatsapp', 'press_button', {'row': 0, 'column': 0, 'label': 'Pay', 'fp': FP}, provider='whatsmeow')


# spec: ANYM-676
def test_the_press_preview_names_the_bot_shows_the_risk_and_binds_edit_date_layout_text_and_fp(pressing):
    s, archive, key, _, bot_message = pressing
    mid = bot_message()
    base = s.preview_messenger_operation(key, **press_args(mid))
    block = base['press']
    assert block['bot']['known'] is True and block['bot']['id'] == '555' and block['risk'].startswith('live action by')
    assert block['message_text']['untrusted'] is True and block['other_labels'] == ['Site', 'Cancel']
    assert base['mutation'] is True and base['owner_approval_verified_by_server'] is False
    assert s.preview_messenger_operation(key, **press_args(mid, fp='b' * 32))['preview_digest'] != base['preview_digest']
    for field, edit in (('edit_date', dict(updated='2026-10-01T12:00:05+00:00')), ('text_sha256', dict(body='Pay later',
                        updated='2026-10-01T12:00:01+00:00')), ('layout_digest', dict(buttons={'rows': [[
                            {'text': 'Pay', 'kind': 'callback'}, {'text': 'Cancel', 'kind': 'callback'}]]},
                            updated='2026-10-01T12:00:02+00:00'))):
        other = bot_message(str(10 + len(field)), **edit)
        changed = s.preview_messenger_operation(key, **press_args(other))
        assert changed['press'][field] != block[field] and changed['preview_digest'] != base['preview_digest']
    stale = archive.ingest(event(account='command-fixture', chat='100', provider_id='5', sender='555', direction='incoming',
                                 body='Pay now', updated_at='2026-10-01T12:05:00+00:00',
                                 metadata={'sender_is_bot': True, 'buttons': {'rows': [[{'text': 'Done', 'kind': 'callback'}]]}}))
    assert stale['outcome'] == 'edited'
    with pytest.raises(APIError, match='operation_target_changed'):
        s.preview_messenger_operation(key, **press_args(mid))  # the keyboard moved on: a new revision is needed


# spec: ANYM-676
def test_an_earlier_uncertain_press_blocks_a_new_request_until_it_is_acknowledged(pressing):
    s, _, key, state, bot_message = pressing
    mid = bot_message()
    first = press_now(s, key, press_args(mid), 'synthetic-press-0001')
    assert first['status'] == 'queued'
    assert s.execute_messenger_operation(key, **press_args(mid), request_id='synthetic-press-0001',
                                         preview_digest=s.preview_messenger_operation(key, **press_args(mid))['preview_digest']
                                         )['operation_id'] == first['operation_id']  # same ID replays the receipt
    other = s.issue_key('command-presser-2', ['command-fixture'], permissions=['read', 'bot-press'])
    for who, rid in zip((key, other), ('synthetic-press-0002', 'synthetic-press-0003')):  # queued blocks, any key
        with pytest.raises(APIError) as blocked:
            press_now(s, who, press_args(mid), rid)
        assert blocked.value.code == 'press_uncertain' and blocked.value.operation_id == first['operation_id']
    with s.pool.connection() as c:  # a worker timeout leaves status unknown and no result at all
        c.execute("UPDATE messenger_commands SET status='unknown',result=NULL WHERE id=%s", (first['operation_id'],))
    with pytest.raises(APIError, match='press_uncertain'):
        press_now(s, key, press_args(mid), 'synthetic-press-0004')
    with pytest.raises(APIError, match='press_uncertain'):  # acknowledging another operation lifts nothing
        press_now(s, key, press_args(mid, acknowledge_uncertain='op_' + '0' * 32), 'synthetic-press-0005')
    second = press_now(s, key, press_args(mid, acknowledge_uncertain=first['operation_id']), 'synthetic-press-0006')
    assert second['status'] == 'queued'


# spec: ANYM-676
@pytest.mark.parametrize('result,blocks', [
    ({'outcome': 'unknown', 'data': {'error': 'provider_result_unverified'}}, True),
    ({'outcome': 'submitted', 'data': {'answer': None, 'answer_timeout': True, 'message_after': None}}, True),
    ({'outcome': 'submitted', 'data': {'answer': None, 'answer_timeout': True,
                                       'message_after': {'edited': False, 'deleted': False, 'new_messages': False}}}, True),
    ({'outcome': 'submitted', 'data': {'answer': {'message': 'ok', 'alert': False, 'url': None}, 'message_after': None}}, False),
    ({'outcome': 'submitted', 'data': {'answer': None, 'answer_timeout': True,
                                       'message_after': {'edited': True, 'deleted': False, 'new_messages': False}}}, False),
    ({'outcome': 'submitted', 'data': {'answer': None, 'answer_timeout': True,
                                       'message_after': {'edited': False, 'deleted': False, 'new_messages': True}}}, False)])
def test_only_a_press_with_an_answer_or_a_visible_change_may_be_repeated(pressing, result, blocks):
    from messaging_service.commands import CommandWorker
    s, _, key, state, bot_message = pressing
    mid = bot_message()
    first = press_now(s, key, press_args(mid), 'synthetic-press-0010')

    async def provider(job):
        assert job['operation'] == 'press_button' and job['press']['fp'] == FP
        return result
    assert asyncio.run(CommandWorker(state, provider).once())
    done = s.get_messenger_operation(key, first['operation_id'])
    assert done['status'] == 'unknown' and done['result']['outcome'] == result['outcome']  # submitted is stored unknown
    if blocks:
        with pytest.raises(APIError, match='press_uncertain'):
            press_now(s, key, press_args(mid), 'synthetic-press-0011')
    else:
        assert press_now(s, key, press_args(mid), 'synthetic-press-0011')['status'] == 'queued'


# spec: ANYM-676
def test_a_press_refused_by_the_collector_is_failed_with_the_live_view_and_may_be_repeated(pressing):
    from messaging_service.commands import CommandRejected, CommandWorker
    s, _, key, state, bot_message = pressing
    mid = bot_message()
    first = press_now(s, key, press_args(mid), 'synthetic-press-0020')

    async def provider(job):
        raise CommandRejected('message_changed', data={'live': {'edit_date': None, 'text': 'Pay later', 'labels': ['Pay']}})
    assert asyncio.run(CommandWorker(state, provider).once())
    done = s.get_messenger_operation(key, first['operation_id'])
    assert done['status'] == 'failed' and done['error_code'] == 'message_changed'
    assert done['result']['data']['live']['text'] == 'Pay later' and 'inspect again' in done['result']['data']['next_action']
    assert press_now(s, key, press_args(mid), 'synthetic-press-0021')['status'] == 'queued'  # a failed press repeats freely


# spec: ANYM-676
def test_acknowledging_the_latest_uncertain_press_is_enough_after_two_uncertain_generations(pressing):
    s, _, key, state, bot_message = pressing
    mid = bot_message()
    first = press_now(s, key, press_args(mid), 'synthetic-press-0030')
    with s.pool.connection() as c:
        c.execute("UPDATE messenger_commands SET status='unknown',result=NULL WHERE id=%s", (first['operation_id'],))
    second = press_now(s, key, press_args(mid, acknowledge_uncertain=first['operation_id']), 'synthetic-press-0031')
    with s.pool.connection() as c:
        c.execute("UPDATE messenger_commands SET status='unknown',result=NULL WHERE id=%s", (second['operation_id'],))
    with pytest.raises(APIError) as blocked:  # only the newest uncertain press blocks; the first was acknowledged
        press_now(s, key, press_args(mid), 'synthetic-press-0032')
    assert blocked.value.operation_id == second['operation_id']
    third = press_now(s, key, press_args(mid, acknowledge_uncertain=second['operation_id']), 'synthetic-press-0033')
    assert third['status'] == 'queued'


# spec: ANYM-676
def test_the_preview_lists_the_earlier_uncertain_press_of_another_key_without_its_content(pressing):
    s, _, key, state, bot_message = pressing
    mid = bot_message()
    assert s.preview_messenger_operation(key, **press_args(mid))['earlier_presses'] == []
    first = press_now(s, key, press_args(mid), 'synthetic-press-0040')
    other = s.issue_key('command-presser-3', ['command-fixture'], permissions=['read', 'bot-press'])
    shown = s.preview_messenger_operation(other, **press_args(mid))['earlier_presses']
    assert [item['operation_id'] for item in shown] == [first['operation_id']] and shown[0]['status'] == 'queued'
    assert set(shown[0]) == {'operation_id', 'status', 'created_at', 'outcome', 'answer_timeout', 'pressed_at'}
    digest = s.preview_messenger_operation(other, **press_args(mid))['preview_digest']
    with pytest.raises(APIError, match='forbidden|not_found'):  # the other key cannot read the receipt itself
        s.get_messenger_operation(other, first['operation_id'])
    assert digest == s.preview_messenger_operation(other, **press_args(mid))['preview_digest']  # not bound
