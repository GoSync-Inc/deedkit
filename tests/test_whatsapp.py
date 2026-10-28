import asyncio
from copy import deepcopy
from datetime import datetime,timezone
import json
from pathlib import Path
import subprocess
import shutil

import pytest

from messaging_service.archive import Archive
from messaging_service.blobs import BlobWriter
from messaging_service.collector_state import CaptureSpool,validate_capture,session_lock
from messaging_service.envelope import stable_id
from messaging_service.whatsapp_collector import WhatsAppCollector,WhatsAppState,NodeProvider,configuration


def event(mid,chat='123@c.us',body=None,media=False,edited=None):
    return {'chatId':chat,'messageId':f'false_{chat}_{mid:06d}','senderId':'42@c.us',
            'body':body or f'item {mid}','timestamp':1790000000+mid,'fromMe':False,'hasMedia':media,
            'filename':'sample.bin' if media else None,'mimetype':'application/octet-stream' if media else None,
            'filesize':4 if media else None,'editedTimestampMs':edited}


def capture(e,account='wa-one',change='message'):
    return {'kind':'whatsapp_event','account':account,'event':e,'change':change,
            'observed_at':'2026-10-13T00:00:00+00:00','origin':'live'}


class Provider:
    def __init__(self):self.messages=[event(i) for i in range(1,6)];self.closed=False;self.policy=None
    def subscribe(self,*args):pass
    async def connect(self):pass
    async def disconnect(self):self.closed=True
    async def wait_disconnected(self):await asyncio.Event().wait()
    async def set_policy(self,policy):self.policy=policy
    async def dialogs(self,limit):return [('123@c.us','user','Fixture')]
    async def page(self,chat,*,limit,before=None):
        selected=sorted([e for e in self.messages if not before or [e['timestamp'],e['messageId']]<before],key=lambda e:[e['timestamp'],e['messageId']])
        return {'messages':deepcopy(selected[-limit:]),'exhausted':len(selected)<limit,'pending':False}
    async def chunks(self,source,descriptor):yield b'data'


@pytest.fixture
def wa(seeded,tmp_path):
    s,_,_=seeded;a=Archive(s);a.register('wa-one','whatsapp','Synthetic WA',discover=True)
    p=Provider();w=WhatsAppCollector(a,'wa-one',p,CaptureSpool(tmp_path/'pages',reserve_bytes=0),
        events_spool=CaptureSpool(tmp_path/'events',reserve_bytes=0),batch_size=2,
        writer=BlobWriter(s.files,reserve_bytes=0),owner='wa-fixture')
    epoch=a.acquire(w.account,w.owner);w.state=WhatsAppState(a,w.account,(w.owner,epoch));w.policy=w.state.policy()
    return w,s,s.issue_key('wa-reader',['wa-one'])


def test_backfill_and_persistent_catchup_does_not_skip_a_burst(wa):
    w,s,key=wa
    asyncio.run(w.sync_once());assert len(s.search(key)['items'])==2
    w.provider.messages.extend(event(i) for i in range(6,13))
    asyncio.run(w.sync_once())
    p=w.state.progress('123@c.us','user');assert p['head'][0]==1790000005 and p['catchup_before'][0]==1790000011
    # Reopen state from DB. Message IDs remain opaque, never bigint offsets.
    w.state=WhatsAppState(w.archive,w.account,w.state.lease)
    for _ in range(6):asyncio.run(w.sync_once())
    p=w.state.progress('123@c.us','user');assert p['head'][0]==1790000012 and p['history_exhausted']
    assert not p.get('catchup_before') and not p.get('gap')
    assert len(s.search(key)['items'])==12
    w.spool.drain(w.state.apply);assert len(s.search(key)['items'])==12


def test_page_commit_failure_preserves_previous_cursor_and_spool(wa):
    w,s,key=wa;original=w.state.apply;calls=0
    def fail(item):
        nonlocal calls
        calls+=1
        if calls==2:raise RuntimeError('db_offline')
        return original(item)
    w.state.apply=fail
    with pytest.raises(RuntimeError,match='db_offline'):asyncio.run(w.sync_once())
    assert not any(k in w.state.progress('123@c.us','user') for k in ['head','history_before','catchup_before'])
    # The committed first message left the spool; the failed one waits for replay.
    assert len(list(w.spool.path.glob('*.json')))==1
    w.state.apply=original;w.spool.drain(original);asyncio.run(w.sync_once())
    assert len(s.search(key)['items'])==2


def test_wa_edit_uses_provider_version_and_delete_survives_before_backfill(wa):
    w,s,key=wa;e=event(5)
    w.events_spool.put(capture(e));w.events_spool.drain(w.state.apply)
    edited=event(5,body='corrected',edited=1791840000000)
    w.events_spool.put(capture(edited,change='edit'));w.events_spool.drain(w.state.apply)
    w.state.apply(capture(e));assert s.search(key,'corrected')['items']
    tombstone={'kind':'whatsapp_delete','account':'wa-one','chat':'123@c.us','ids':[event(4)['messageId']],
               'observed_at':'2026-10-13T00:00:00+00:00'}
    w.events_spool.put(tombstone);w.events_spool.drain(w.state.apply)
    asyncio.run(w.sync_once())
    assert s.get_context(key,stable_id('msg','wa-one','123@c.us',event(4)['messageId']))['message']['deleted']
    # Same opaque ID in another chat/account is not touched.
    other=event(4,chat='456@c.us');other['messageId']=event(4)['messageId']
    w.state.apply(capture(other));assert len(s.search(key,'item 4')['items'])==1


def test_distinct_accounts_policies_and_consumers_remain_independent(wa,tmp_path):
    w,s,key=wa;w.archive.register('wa-two','whatsapp','Second',discover=True)
    epoch=w.archive.acquire('wa-two','second');state=WhatsAppState(w.archive,'wa-two',('second',epoch))
    w.state.apply(capture(event(1)));state.apply(capture(event(1),'wa-two'))
    assert len(s.search(key)['items'])==1
    assert s.search(key)['items'][0]['account']=='wa-one'
    with pytest.raises(ValueError,match='foreign_capture'):w.state.apply(capture(event(1),'wa-two'))
    w.archive.register('wa-one','whatsapp','First',discover=True,exclude_chats=['123@c.us'])
    assert w.state.apply(capture(event(2)))['outcome']=='excluded'
    assert len(s.search(key)['items'])==1


def test_pending_history_is_not_reported_as_exhausted_and_gap_is_visible(wa):
    w,s,key=wa
    asyncio.run(w.sync_once())
    w.provider.messages=[event(20),event(21)]
    asyncio.run(w.sync_once())
    # Provider no longer has a bridge to the old head; never claim full history.
    asyncio.run(w.sync_once())
    p=w.state.progress('123@c.us','user');assert p['gap']
    with s.pool.connection() as c:
        row=c.execute("SELECT * FROM source_status WHERE account='wa-one'").fetchone()
    # Missing history does not make live receiving look broken.
    assert row['status']=='receiving' and row['detail_code']=='whatsapp_catchup_gap' and row['coverage']=='partial'


def test_node_raw_spool_is_compatible_with_python_validator(tmp_path):
    node=shutil.which('node');assert node,'Node runtime required for cross-language spool test'
    capture_module=(Path(__file__).parents[1]/'whatsapp'/'capture.mjs').as_uri()
    item=capture(event(1));directory=tmp_path/'incoming'
    script=f"import {{DiskCapture}} from {json.dumps(capture_module)}; new DiskCapture(process.argv[1],{{reserveBytes:0}}).put(JSON.parse(process.argv[2]));"
    subprocess.run([node,'--input-type=module','-e',script,str(directory),json.dumps(item)],check=True)
    seen=[];CaptureSpool(directory,reserve_bytes=0).drain(seen.append)
    assert seen==[validate_capture(item)]


# spec: ANYM-92
def test_whatsapp_event_capture_bounds_sender_name_and_rejects_unlisted_fields():
    for name,expected in [('N'*512,'N'*512),('N'*600,'N'*512),('Ann\x00a','Ann�a')]:
        converted=validate_capture(capture({**event(1),'senderName':name,'senderUsername':'synthetic_user'}))
        assert converted['kind']=='message' and converted['envelope']['sender_profile']['display_name']==expected
    for bad in [{**event(1),'senderPhone':'15550000000'},{**event(1),'rawMessage':{}},['not','a','dict']]:
        with pytest.raises(ValueError,match='^invalid_whatsapp_event$'):validate_capture(capture(bad))

def test_wa_media_uses_same_integrity_and_fencing_path(wa):
    w,s,key=wa;w.state.apply(capture(event(1,media=True)))
    assert asyncio.run(w.media_once())
    with s.pool.connection() as c:row=c.execute("SELECT a.bytes,a.digest FROM attachments a JOIN messages m ON m.id=a.message WHERE m.account='wa-one'").fetchone()
    assert row['bytes']==4 and len(row['digest'])==64


def test_wa_shutdown_releases_both_ownership_leases(wa):
    w,s,_=wa;w.archive.release(w.account,w.owner,w.state.lease[1])
    async def run():
        task=asyncio.create_task(w.run());await asyncio.sleep(.15);w.stopping.set();await task
    asyncio.run(run());assert w.provider.closed
    with s.pool.connection() as c:row=c.execute('SELECT expires_at FROM collector_leases WHERE account=%s',(w.account,)).fetchone()
    assert row['expires_at']<=datetime.now(timezone.utc)


def test_wa_config_requires_explicit_handover_and_separate_directories(tmp_path):
    cfg={'enabled':True,'account':'wa-one','expected_wid':'123456789@c.us','permit_file':str(tmp_path/'permit'),
         'session_directory':str(tmp_path/'session'),'events_directory':str(tmp_path/'events'),
         'pages_directory':str(tmp_path/'pages'),'chromium_executable':'/usr/bin/chromium','node_executable':'/usr/local/bin/node'}
    config=tmp_path/'config';permit=tmp_path/'permit'
    config.write_text(json.dumps(cfg));config.chmod(0o600)
    permit.write_text(json.dumps({'account':'wa-one','expected_wid':cfg['expected_wid'],'prior_clients_stopped':False}));permit.chmod(0o600)
    with pytest.raises(RuntimeError,match='handover'):configuration(config)
    permit.write_text(json.dumps({'account':'wa-one','expected_wid':cfg['expected_wid'],'prior_clients_stopped':True}))
    assert configuration(config)['max_media_bytes']==512*1024**2
    cfg['events_directory']=cfg['session_directory']+'/events';config.write_text(json.dumps(cfg))
    with pytest.raises(ValueError,match='overlap'):configuration(config)


def test_inherited_session_lock_prevents_new_owner_after_parent_scope(tmp_path):
    target=tmp_path/'owned'
    with session_lock(target) as fd:
        child=subprocess.Popen(['python','-c','import sys; sys.stdin.read()'],stdin=subprocess.PIPE,pass_fds=(fd,))
    try:
        with pytest.raises(RuntimeError,match='session_already_owned'):
            with session_lock(target):pass
    finally:child.stdin.close();child.wait(timeout=3)
    with session_lock(target):pass


def test_whatsapp_pairing_requires_owner_terminal_before_config():
    from messaging_service.whatsapp_pair import pair_config
    with pytest.raises(RuntimeError,match='owner_terminal_required'):
        asyncio.run(pair_config('/nonexistent/no-secrets-read'))


def test_private_node_protocol_streams_and_recovers_from_integrity_error(tmp_path):
    from messaging_service.blobs import BlobWriter
    script=tmp_path/'fake.mjs'
    script.write_text('''import readline from 'node:readline';
const lines=readline.createInterface({input:process.stdin});
for await(const line of lines){const r=JSON.parse(line);const emit=x=>process.stdout.write(JSON.stringify({id:r.id,...x})+'\\n');
 if(r.method==='media'){emit({chunk:Buffer.from('data').toString('base64')});
  if(r.params.message==='bad')emit({error:'media_hash_mismatch'});else emit({result:{verified:true}});
 }else emit({result:{ready:true}});
}''')
    async def run():
        with session_lock(tmp_path/'owner') as fd:
            p=NodeProvider({'node_executable':shutil.which('node')},tmp_path/'unused',fd,script=script)
            p.policy={};await p.connect();writer=BlobWriter(tmp_path/'blobs',reserve_bytes=0)
            try:
                with pytest.raises(RuntimeError,match='media_hash_mismatch'):
                    await writer.write_async(p.chunks({'provider_chat':'123@c.us','provider_id':'bad'},{}),expected_bytes=4)
                assert not list((tmp_path/'blobs').iterdir())
                assert await p.call('policy',{})=={'ready':True}
                result=await writer.write_async(p.chunks({'provider_chat':'123@c.us','provider_id':'good'},{}),expected_bytes=4)
                assert result.bytes==4
            finally:await p.disconnect()
            assert p.process.returncode is not None
    asyncio.run(run())


def test_private_node_disconnects_on_cancelled_partial_download(tmp_path):
    script=tmp_path/'fake.mjs'
    script.write_text('''import readline from 'node:readline';
const lines=readline.createInterface({input:process.stdin});
for await(const line of lines){const r=JSON.parse(line);
 if(r.method==='media'){process.stdout.write(JSON.stringify({id:r.id,chunk:'ZGF0YQ=='})+'\\n');await new Promise(()=>{});}
 else process.stdout.write(JSON.stringify({id:r.id,result:{ready:true}})+'\\n');
}''')
    async def run():
        with session_lock(tmp_path/'owner') as fd:
            p=NodeProvider({'node_executable':shutil.which('node')},tmp_path/'unused',fd,script=script);p.policy={}
            await p.connect()
            stream=p.chunks({'provider_chat':'123@c.us','provider_id':'abc'},{})
            assert await anext(stream)==b'data';await stream.aclose()
            assert p.process.returncode is not None
    asyncio.run(run())


def test_backup_includes_whatsapp_progress(wa,tmp_path):
    from messaging_service.backup import database_manifest
    w,s,_=wa;asyncio.run(w.sync_once())
    with s.pool.connection() as c:before=database_manifest(c)
    assert before['whatsapp_checkpoints']['rows']==1
    w.state.checkpoint_wa('123@c.us',{'head':[1790000100,'opaque']})
    with s.pool.connection() as c:after=database_manifest(c)
    assert before['whatsapp_checkpoints']['sha256']!=after['whatsapp_checkpoints']['sha256']


def test_policy_refresh_never_waits_for_media_rpc_lock(tmp_path):
    async def run():
        with session_lock(tmp_path/'owner') as fd:
            p=NodeProvider({'node_executable':shutil.which('node'),'events_directory':str(tmp_path/'events')},tmp_path/'unused',fd)
            policy={'discover_chats':True,'include_chats':[],'exclude_chats':['123@c.us']}
            async with p.lock:await asyncio.wait_for(p.set_policy(policy),.5)
            saved=json.loads((tmp_path/'events'/'.policy').read_text())
            assert saved['policy']=={**policy,'enabled':True,'send_enabled':False,'operations_enabled':[]} and saved['expires_at']>datetime.now(timezone.utc).timestamp()
            assert (tmp_path/'events'/'.policy').stat().st_mode&0o077==0
    asyncio.run(run())


def test_one_unavailable_chat_does_not_stop_other_chat_ingest(wa):
    from messaging_service.whatsapp_collector import ProviderOperationError
    w,s,key=wa
    async def dialogs(limit):return [('456@c.us','user','Unavailable'),('123@c.us','user','Available')]
    page=w.provider.page
    async def failing_page(chat,**kwargs):
        if chat=='456@c.us':raise ProviderOperationError('provider_operation_failed')
        return await page(chat,**kwargs)
    w.provider.dialogs=dialogs;w.provider.page=failing_page
    asyncio.run(w.sync_once())
    assert len(s.search(key)['items'])==2
    with s.pool.connection() as c:row=c.execute("SELECT status,detail_code FROM source_status WHERE account='wa-one'").fetchone()
    assert row=={'status':'degraded','detail_code':'whatsapp_chat_unavailable'}


def test_cold_whatsapp_profile_drops_only_old_browser_singletons(tmp_path):
    from messaging_service import whatsapp_collector as wa
    profile=tmp_path/'session';(profile/'Default').mkdir(parents=True)
    (profile/'Local State').write_bytes(b'PERSISTENT_AUTH_STATE')
    (profile/'Default/auth.db').write_bytes(b'PERSISTENT_AUTH_DB')
    for name in ['SingletonLock','SingletonSocket','SingletonCookie']:
        (profile/name).symlink_to('/missing/old-container')
    wa.clear_browser_runtime_locks(tmp_path)
    assert not any(p.is_symlink() for p in profile.iterdir())
    assert (profile/'Default/auth.db').read_bytes()==b'PERSISTENT_AUTH_DB'
    assert (profile/'Local State').read_bytes()==b'PERSISTENT_AUTH_STATE'
    (profile/'SingletonLock').write_bytes(b'UNEXPECTED_REGULAR_FILE')
    with pytest.raises(ValueError,match='invalid_browser_lock'):
        wa.clear_browser_runtime_locks(tmp_path)
    assert (profile/'SingletonLock').read_bytes()==b'UNEXPECTED_REGULAR_FILE'


def test_browser_runtime_cleanup_rejects_profile_symlink(tmp_path):
    from messaging_service import whatsapp_collector as wa
    outside=tmp_path/'outside';outside.mkdir();(outside/'SingletonLock').symlink_to('/missing')
    session=tmp_path/'source';session.mkdir();(session/'session').symlink_to(outside)
    with pytest.raises((OSError,ValueError)):
        wa.clear_browser_runtime_locks(session)
    assert (outside/'SingletonLock').is_symlink()


@pytest.mark.parametrize('mime,expected',[('audio/ogg; codecs=opus','audio/ogg'),(' Text/Plain; charset="utf-8" ','text/plain')])
def test_whatsapp_media_parameters_normalize_before_durable_ingest(mime,expected):
    from messaging_service.adapters import whatsapp_message
    raw={'chatId':'123@c.us','messageId':'false_123@c.us_AUDIO','timestamp':1800000000,'hasMedia':True,'mimetype':mime,'filesize':123}
    result=whatsapp_message('test',raw)
    assert result['media'][0]['mime']==expected


def test_whatsapp_mime_controls_remain_rejected():
    # The name is a stable test identity: control characters are still rejected
    # as MIME text, but now neutralized so a hostile sender cannot stop capture.
    from messaging_service.adapters import whatsapp_message
    raw={'chatId':'123@c.us','messageId':'false_123@c.us_AUDIO','timestamp':1800000000,'hasMedia':True,'mimetype':'audio/ogg; codecs=opus\r\nInjected: header','filesize':123}
    # A hostile sender must not stop capture, and no header text survives.
    media=whatsapp_message('test',raw)['media'][0]
    assert media['mime']=='application/octet-stream' and 'Injected' not in json.dumps(media)


def test_failed_chat_moves_behind_other_chats_after_restart_without_advancing_cursor(wa):
    w,s,key=wa
    async def dialogs(limit):return [('456@c.us','user','Stalled'),('123@c.us','user','Available')]
    page=w.provider.page;calls=[]
    async def failing_page(chat,**kwargs):
        calls.append(chat)
        if chat=='456@c.us':raise RuntimeError('provider_disconnected')
        return await page(chat,**kwargs)
    w.provider.dialogs=dialogs;w.provider.page=failing_page
    with pytest.raises(RuntimeError,match='provider_disconnected'):asyncio.run(w.sync_once())
    assert calls==['456@c.us']
    w.state=WhatsAppState(w.archive,w.account,w.state.lease)
    failed=w.state.progress('456@c.us','user')
    assert not any(k in failed for k in ['head','history_before','catchup_before'])
    with s.pool.connection() as c:
        attempted_scan=c.execute("SELECT last_scan_at FROM whatsapp_checkpoints WHERE account='wa-one' AND provider_chat='456@c.us'").fetchone()['last_scan_at']
    calls.clear()
    with pytest.raises(RuntimeError,match='provider_disconnected'):asyncio.run(w.sync_once())
    assert calls[0]=='123@c.us'
    assert len(s.search(key)['items'])==2
    with s.pool.connection() as c:
        assert c.execute("SELECT last_scan_at FROM whatsapp_checkpoints WHERE account='wa-one' AND provider_chat='456@c.us'").fetchone()['last_scan_at']==attempted_scan


def test_attempt_marker_requires_current_collector_lease(wa):
    w,s,_=wa
    w.archive.release(w.account,w.owner,w.state.lease[1])
    with pytest.raises(RuntimeError,match='collector_lease_lost'):w.state.begin_attempt('123@c.us','user')
    with s.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM whatsapp_checkpoints WHERE account='wa-one'").fetchone()['n']==0
