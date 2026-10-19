"""Native capabilities follow the fenced account implementation, not caller input."""
import asyncio
import pytest
from messaging_service.archive import Archive
from messaging_service.whatsapp_collector import WhatsAppState
from messaging_service.commands import CommandWorker
from messaging_service.messenger_operations import operation_manifest, validate_operation
from messaging_service.envelope import stable_id
from messaging_service.store import APIError
from test_whatsapp import capture, event


def history_status():
    return {'state':'retrying','reason':'history_download_failed','attempt':2,'max_attempts':3,
        'next_attempt_ms':1792044000000,'retry':'bounded_passive_download','coverage':'partial',
        'observed_at':'2026-10-15T06:00:00Z'}


def test_private_history_diagnostics_exclude_payloads_and_unsafe_paths(tmp_path):
    import json
    from messaging_service.whatsapp_collector import WhatsmeowProvider,validate_native_history_status
    provider=WhatsmeowProvider({'events_directory':str(tmp_path),'native_executable':'/unused'},tmp_path/'unused',3)
    assert provider.history_status() is None
    path=tmp_path/'.history-status';path.write_text(json.dumps(history_status()));path.chmod(0o600)
    assert provider.history_status()['reason']=='history_download_failed'
    unsafe={**history_status(),'raw':'private message'}
    with pytest.raises(ValueError,match='invalid_native_history_status'):validate_native_history_status(unsafe)
    path.unlink();target=tmp_path/'target';target.write_text('{}');path.symlink_to(target)
    with pytest.raises(OSError):provider.history_status()


@pytest.mark.parametrize('code', ['history_rate_limited','history_anchor_unavailable'])
def test_history_preflight_denial_is_rejected_not_an_unknown_mutation(tmp_path, code):
    import json
    from types import SimpleNamespace
    from messaging_service.commands import CommandRejected
    from messaging_service.whatsapp_collector import NodeProvider
    async def run():
        provider=NodeProvider({},tmp_path/'unused',3)
        provider.process=SimpleNamespace(stdout=asyncio.StreamReader())
        provider.process.stdout.feed_data(json.dumps({'id':1,'error':code}).encode()+b'\n')
        with pytest.raises(CommandRejected,match=code):await provider.reply(1)
    asyncio.run(run())


@pytest.fixture
def native_account(seeded):
    s,*_=seeded;a=Archive(s);a.register('wa-native','whatsapp','Synthetic native',discover=True)
    epoch=a.acquire('wa-native','native-test')
    state=WhatsAppState(a,'wa-native',('native-test',epoch))
    state.phase('connection','connected','provider_ready')
    item=event(1);item['fromMe']=True;item['senderId']='42@c.us'
    item['messageId']='true_123@c.us_000001';state.apply(capture(item,account='wa-native'))
    key=s.issue_key('native-agent',['wa-native'],permissions=['read','read-live','message-delete','contacts-manage'])
    args={'account':'wa-native','operation':'chat_info','chat_id':stable_id('chat','wa-native','123@c.us'),'parameters':{}}
    yield s,a,state,key,args
    a.release('wa-native','native-test',epoch)


def test_native_manifest_is_explicit_about_global_revoke_and_parity_gaps():
    native={x['operation']:x for x in operation_manifest('whatsapp',provider='whatsmeow')}
    legacy={x['operation']:x for x in operation_manifest('whatsapp')}
    assert native['delete_message']['supported'] and not legacy['delete_message']['supported']
    assert validate_operation('whatsapp','delete_message',{},provider='whatsmeow')=={}
    assert validate_operation('whatsapp','request_chat_history',{},provider='whatsmeow')=={'count':50}
    with pytest.raises(ValueError,match='invalid_operation_parameters'):
        validate_operation('whatsapp','request_chat_history',{'count':51},provider='whatsmeow')
    for name in ('add_contact','delete_contact','get_labels','set_chat_labels'):
        assert not native[name]['supported']
        with pytest.raises(ValueError,match='unsupported_operation'):
            validate_operation('whatsapp',name,{},provider='whatsmeow')
    for name in ('get_message_info','get_reactions','get_pinned_messages'):
        assert 'unknown' in native[name]['limitation']


def test_fenced_observation_updates_manifest_without_expanding_grants(native_account):
    s,a,state,key,args=native_account
    assert s.list_messenger_operations(key,'wa-native')['provider']=='webjs'
    state.provider_observation('whatsmeow')
    result=s.list_messenger_operations(key,'wa-native')
    assert result['provider']=='whatsmeow'
    rows={x['operation']:x for x in result['items']}
    assert rows['delete_message']['supported'] and not rows['delete_message']['available']
    s.set_messenger_operations('wa-native',['delete_message'])
    assert next(x for x in s.list_messenger_operations(key,'wa-native')['items'] if x['operation']=='delete_message')['available']
    with pytest.raises(ValueError,match='unsupported_operation'):
        s.set_messenger_operations('wa-native',['add_contact'])
    caps=s.capabilities(key)['messenger_operations']['supported_by_account']['wa-native']
    assert caps['provider']=='whatsmeow' and caps['count']==44
    assert len(caps['operations'])==44
    assert 'delete_message' in caps['operations'] and 'add_contact' not in caps['operations']
    a.release('wa-native','native-test',state.lease[1])
    with pytest.raises(RuntimeError,match='collector_lease_lost'):state.provider_observation('webjs')


def test_account_discovery_omits_unsupported_operations_and_cannot_queue_them(native_account):
    s,a,state,key,args=native_account
    state.provider_observation('whatsmeow')
    rows=s.list_messenger_operations(key,'wa-native')['items']
    assert rows and all(x['supported'] for x in rows)
    assert not ({'add_contact','delete_contact','get_labels','set_chat_labels'} & {x['operation'] for x in rows})
    with pytest.raises(APIError,match='unsupported_operation'):
        s.preview_messenger_operation(key,account='wa-native',operation='add_contact',
            parameters={'phone':'+123456789','first_name':'Synthetic'})
    with pytest.raises(APIError,match='unsupported_operation'):
        s.execute_messenger_operation(key,account='wa-native',operation='add_contact',
            parameters={'phone':'+123456789','first_name':'Synthetic'},
            request_id='synthetic-unsupported',preview_digest='a'*64)
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n']==0


def test_provider_change_invalidates_reviewed_preview_before_dispatch(native_account):
    s,a,state,key,args=native_account
    s.set_messenger_operations('wa-native',['chat_info'])
    old=s.preview_messenger_operation(key,**args)
    assert 'adapter' not in old # existing legacy previews retain their intent shape
    state.provider_observation('whatsmeow')
    with pytest.raises(APIError,match='preview_mismatch'):
        s.execute_messenger_operation(key,**args,request_id='native-provider-change-1',preview_digest=old['preview_digest'])
    new=s.preview_messenger_operation(key,**args)
    assert new['adapter']=='whatsmeow' and new['preview_digest']!=old['preview_digest']
    pending=s.execute_messenger_operation(key,**args,request_id='native-provider-change-2',preview_digest=new['preview_digest'])
    state.provider_observation('webjs');state.phase('connection','connected','provider_ready')
    calls=[]
    async def execute(job):calls.append(job);return {'outcome':'confirmed','data':{}}
    assert asyncio.run(CommandWorker(state,execute).once())
    assert not calls
    with s.pool.connection() as c:
        row=c.execute('SELECT status,error_code FROM messenger_commands WHERE id=%s',(pending['operation_id'],)).fetchone()
    assert row['status']=='cancelled' and row['error_code']=='operation_target_changed'


@pytest.mark.parametrize('claimed',[False,True])
def test_pending_receipt_survives_provider_change_without_authorizing_dispatch(native_account,claimed):
    from messaging_service.commands import CommandRejected
    s,a,state,key,_=native_account
    s.set_messenger_operations('wa-native',['add_contact'])
    args={'account':'wa-native','operation':'add_contact',
          'parameters':{'phone':'+123456789','first_name':'Synthetic'}}
    draft=s.preview_messenger_operation(key,**args)
    pending=s.execute_messenger_operation(key,**args,request_id='synthetic-native-pending',
        preview_digest=draft['preview_digest'])
    calls=[]
    async def execute(job):calls.append(job);return {'outcome':'confirmed'}
    worker=CommandWorker(state,execute)
    job=worker.claim() if claimed else None
    state.provider_observation('whatsmeow')
    expected='dispatching' if claimed else 'queued'
    assert s.get_messenger_operation(key,pending['operation_id'])['status']==expected
    if claimed:
        with pytest.raises(CommandRejected,match='unsupported_operation'):worker.preflight(job)
    else:
        assert asyncio.run(worker.once())
        receipt=s.get_messenger_operation(key,pending['operation_id'])
        assert receipt['status']=='cancelled' and receipt['error_code']=='unsupported_operation'
    assert not calls
    other=s.issue_key('native-agent',['wa-native'],permissions=['read','contacts-manage'])
    with pytest.raises(APIError,match='not_found'):
        s.get_messenger_operation(other,pending['operation_id'])
    s.set_access('native-agent',chats=[])
    with pytest.raises(APIError,match='forbidden'):
        s.get_messenger_operation(key,pending['operation_id'])


def test_history_diagnostics_are_fenced_partial_and_visible_in_status(native_account):
    s,a,state,key,args=native_account
    state.provider_observation('whatsmeow');state.native_history_observation(history_status())
    result=s.get_sync_status(key)
    row=next(x for x in result['sources'] if x['account']=='wa-native')
    report=row['operations']['provider_history']
    assert report['state']=='retrying' and report['attempt']==2
    assert report['scope']=='latest_native_history_batch' and report['full_history_verified'] is False
    state.provider_observation('webjs')
    with s.pool.connection() as c:
        assert 'provider_history' not in c.execute('SELECT operations FROM collector_health WHERE account=%s',('wa-native',)).fetchone()['operations']
    a.release('wa-native','native-test',state.lease[1])
    with pytest.raises(RuntimeError,match='collector_lease_lost'):state.native_history_observation(history_status())


def test_native_rich_preview_rejected_before_queue_and_plain_urls_remain_passive(native_account):
    s,a,state,key,args=native_account
    state.provider_observation('whatsmeow')
    with pytest.raises(APIError,match='link_preview_unsupported'):
        s.preview_message(key,account='wa-native',chat_id=args['chat_id'],text='https://example.invalid',link_preview=True)
    draft=s.preview_message(key,account='wa-native',chat_id=args['chat_id'],text='https://example.invalid')
    assert draft['provider_text']=='https://example.invalid'
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM outgoing_messages').fetchone()['n']==0
