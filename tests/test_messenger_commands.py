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
    client=GroupClient()
    client.hide[202]='hang' if failure=='timeout' else RuntimeError('synthetic crash')
    original=asyncio.wait_for
    monkeypatch.setattr(asyncio,'wait_for',lambda awaitable,timeout:original(awaitable,0.5))
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
