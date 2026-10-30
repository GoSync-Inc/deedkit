"""Operational evidence and transport failures, using synthetic sessions only."""
import asyncio
from datetime import datetime, timezone
import json
import shutil

import pytest

from messaging_service.collector_state import CollectorState, session_lock
from messaging_service.sync_status import get_sync_status
from messaging_service.whatsapp_collector import NodeProvider
from test_collectors import worker as worker
from test_whatsapp import wa as wa, event, capture


def source(w, s, chats=None):
    with s.pool.connection() as c:
        return get_sync_status(c, {'accounts': [w.account], 'chat_ids': chats})['sources'][0]


def test_history_gap_does_not_mark_connection_or_live_capture_failed(wa):
    w,s,_=wa
    w.state.phase('connection','connected','provider_ready')
    w.state.phase('live_capture','receiving','capture_ready')
    asyncio.run(w.sync_once())
    w.provider.messages=[event(20),event(21)]
    for _ in range(2):asyncio.run(w.sync_once())
    row=source(w,s)
    assert row['operations']['connection']['state']=='connected'
    assert row['operations']['live_capture']['state']=='receiving'
    assert row['operations']['history']['state']=='partial_with_gaps'
    assert row['history']['full_history_verified'] is False


def test_history_pages_do_not_make_a_quiet_live_session_look_backlogged(wa):
    w,s,_=wa
    w.state.phase('connection','connected','provider_ready')
    w.state.phase('live_capture','receiving','capture_ready')
    w.state.capture_progress(None,0,0,history_pending=40)
    row=source(w,s)
    assert row['operations']['live_capture']['state']=='receiving'
    assert row['operations']['history']['pending_events']==40
    assert row['liveness']['state']=='idle_connected' and row['liveness']['quiet_but_healthy'] is True
    item=capture(event(30));w.state.apply(item);w.state.capture_progress(item,1,0)
    assert source(w,s)['liveness']['state']=='receiving'
    w.state.capture_progress(None,0,3)
    assert source(w,s)['liveness']['state']=='capture_backlogged'
    # Chat-restricted readers never receive account-wide session health.
    from messaging_service.envelope import stable_id
    assert 'liveness' not in source(w,s,[stable_id('chat',w.account,'123@c.us')])


def test_connection_failure_survives_restart_and_normal_status_updates(wa):
    w,s,_=wa
    w.state.connection_failed('whatsapp_logged_out')
    reopened=CollectorState(w.archive,w.account,w.state.lease)
    reopened.status('stopped','requires_reconciliation')
    row=source(w,s)['operations']['connection']
    assert row['state']=='blocked' and row['reason_code']=='whatsapp_logged_out'
    assert row['next_action']=='relink_in_owner_terminal' and row['retry_at'] is None
    assert reopened.connection_gate()['blocked'] is True


def test_retry_is_capped_persisted_and_does_not_reset_at_start(wa):
    w,s,_=wa
    for _ in range(12):w.state.connection_failed('provider_disconnected')
    reopened=CollectorState(w.archive,w.account,w.state.lease)
    reopened.status('starting','whatsapp_history_partial')
    gate=reopened.connection_gate()
    assert 290<gate['remaining_seconds']<=300 and not gate['blocked']
    assert source(w,s)['operations']['connection']['retry_attempts']==12
    reopened.phase('connection','connected','provider_ready')
    assert reopened.connection_gate()['remaining_seconds']==0
    assert source(w,s)['operations']['connection']['retry_attempts']==0


def test_terminal_gate_does_not_open_a_provider_even_after_restart(wa):
    w,s,_=wa;w.state.connection_failed('provider_auth_failed')
    calls=[]
    async def connect():calls.append(True)
    w.provider.connect=connect
    async def check():
        task=asyncio.create_task(w.connect_provider())
        await asyncio.sleep(.1);task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    asyncio.run(check())
    assert calls==[] and source(w,s)['operations']['connection']['state']=='blocked'


def test_phase_transition_time_is_stable_and_requires_current_lease(wa):
    w,s,_=wa
    w.state.phase('connection','connected','provider_ready')
    before=source(w,s)['operations']['connection']['changed_at']
    w.state.phase('connection','connected','provider_ready')
    assert source(w,s)['operations']['connection']['changed_at']==before
    w.archive.release(w.account,w.owner,w.state.lease[1])
    with pytest.raises(RuntimeError,match='collector_lease_lost'):
        w.state.phase('connection','connected','provider_ready')
    assert source(w,s)['operations']['connection']['state']=='not_running'


def test_transport_failure_and_media_diagnostics_cross_private_ipc(tmp_path):
    script=tmp_path/'fake.mjs'
    script.write_text("""import readline from 'node:readline';
for await(const line of readline.createInterface({input:process.stdin})) {
 const r=JSON.parse(line); const error=r.method==='start'?'provider_auth_failed':'media_timeout';
 process.stdout.write(JSON.stringify({id:r.id,error})+'\\n');
}""")
    async def check():
        with session_lock(tmp_path/'owned') as fd:
            p=NodeProvider({'node_executable':shutil.which('node'),'events_directory':str(tmp_path/'events')},tmp_path/'unused',fd,script=script)
            try:
                with pytest.raises(RuntimeError,match='^provider_auth_failed$'):await p.connect()
                await p.disconnect()
                script.write_text(script.read_text().replace("r.method==='start'?'provider_auth_failed':'media_timeout'","r.method==='start'?null:'media_timeout'").replace("JSON.stringify({id:r.id,error})","JSON.stringify(error?{id:r.id,error}:{id:r.id,result:{ready:true}})"))
                await p.connect()
                with pytest.raises(RuntimeError,match='^media_timeout$'):await p.call('media',{})
            finally:await p.disconnect()
    asyncio.run(check())


@pytest.mark.parametrize('code', ['media_timeout','media_network_error','media_metadata_unavailable','invalid_media_path'])
def test_download_job_keeps_bounded_failure_reason_and_does_not_stop_capture(wa, code):
    w,s,_=wa;w.state.apply(capture(event(1,media=True)))
    async def failed(*_):
        raise RuntimeError(code)
        yield b''
    w.provider.chunks=failed
    assert asyncio.run(w.media_once())
    with s.pool.connection() as c:
        job=c.execute('SELECT state,error_code FROM media_jobs WHERE account=%s',(w.account,)).fetchone()
    assert job['error_code']==code
    assert source(w,s)['operations']['media']['errors'][code]==1


def test_capture_commit_timings_do_not_change_message_revision(wa):
    w,s,key=wa
    item=capture(event(1));w.state.apply(item)
    first=s.search(key)['items'][0]['revision']
    w.state.capture_progress(item,1,0)
    item['observed_at']=datetime.now(timezone.utc).isoformat()
    w.state.apply(item);w.state.capture_progress(item,1,0)
    assert s.search(key)['items'][0]['revision']==first
    latest=source(w,s)['operations']['live_capture']['last_commit']
    assert latest['capture_to_commit_seconds']>=0 and latest['provider_age_at_capture_seconds']>0
    assert latest['mcp_available_after_commit'] is True


def test_account_connection_state_does_not_leak_to_chat_scoped_readers(wa):
    from messaging_service.envelope import stable_id
    w,s,_=wa;w.state.apply(capture(event(1)))
    w.state.connection_failed('whatsapp_identity_mismatch')
    row=source(w,s,[stable_id('chat',w.account,'123@c.us')])
    assert 'operations' not in row and 'processing_profiles' not in row


def test_worker_profile_is_independent_of_unvisited_file_job(wa):
    from messaging_service.enrichment import EnrichmentWorker
    from messaging_service.enrichment_providers import validate_profile
    w,s,key=wa
    item=capture(event(1,media=True));item['event']['mimetype']='audio/ogg'
    w.state.apply(item);asyncio.run(w.media_once())
    profile=validate_profile({'provider':'faster_whisper','model':'/models/synthetic','allow_cloud':False},'audio')
    worker=EnrichmentWorker(s.pool.conninfo,s.files,{'accounts':[w.account],'profiles':{'audio':profile},'max_jobs_per_day':10})
    worker.publish_profiles()
    row=source(w,s)
    assert row['processing_profiles'][0]['kind']=='audio' and row['processing_profiles'][0]['configured']
    attachment=s.search_files(key)['items'][0]['attachment_id']
    result=s.get_file_segments(key,attachment)
    assert result['empty_reason']=='processing_pending' and result['enrichment_status']=='queued'
    assert '/models/' not in json.dumps(row['processing_profiles'],default=str)
    with s.pool.connection() as c:c.execute('UPDATE processing_profiles SET expires_at=clock_timestamp()-interval \'1 second\'')
    result=s.get_file_segments(key,attachment)
    assert result['empty_reason']=='processing_worker_unavailable' and result['next_action']=='check_processing_worker'


def test_explicit_owner_resume_clears_only_the_selected_terminal_gate(wa):
    from messaging_service.collector_control import resume
    w,s,_=wa;w.state.connection_failed('whatsapp_logged_out')
    with s.pool.connection() as c:resume(c,w.account)
    assert w.state.connection_gate()=={'blocked':False,'remaining_seconds':0}
    with s.pool.connection() as c:
        with pytest.raises(ValueError,match='connection_not_blocked'):resume(c,w.account)


def test_post_ready_failure_file_is_read_even_without_an_rpc_in_flight(tmp_path):
    script=tmp_path/'fake.mjs';directory=tmp_path/'events';directory.mkdir(mode=0o700)
    script.write_text("""import readline from 'node:readline';import fs from 'node:fs';
for await(const line of readline.createInterface({input:process.stdin})){
 const r=JSON.parse(line);process.stdout.write(JSON.stringify({id:r.id,result:{ready:true}})+'\\n');
 setTimeout(()=>{fs.writeFileSync(process.argv[2],JSON.stringify({code:'whatsapp_logged_out'}),{mode:0o600});process.exit(1);},20);
}""")
    async def check():
        with session_lock(tmp_path/'owned') as fd:
            p=NodeProvider({'node_executable':shutil.which('node'),'events_directory':str(directory)},directory/'.transport-failure',fd,script=script)
            try:
                await p.connect()
                with pytest.raises(RuntimeError,match='whatsapp_logged_out'):await p.wait_disconnected()
            finally:await p.disconnect()
    asyncio.run(check())


def test_transient_start_retry_honors_persisted_deadline_without_new_client(wa):
    w,s,_=wa;w.state.connection_failed('provider_initialization_failed');calls=[]
    async def connect():calls.append(True)
    w.provider.connect=connect
    async def check():
        task=asyncio.create_task(w.connect_provider());await asyncio.sleep(.1)
        task.cancel();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(check())
    assert calls==[] and source(w,s)['operations']['connection']['retry_attempts']==1


def test_legacy_capture_has_unknown_timing_not_provider_age_as_queue_delay(wa):
    from messaging_service.collector_state import validate_capture
    w,s,_=wa;item=validate_capture(capture(event(1)));item.pop('captured_at')
    w.state.apply(item);w.state.capture_progress(item,1,0)
    latest=source(w,s)['operations']['live_capture']['last_commit']
    assert latest['capture_to_commit_seconds'] is None
    assert latest['measurement']=='capture_time_not_recorded'


def test_shutdown_while_terminal_blocked_does_not_clear_gate_on_restart(wa):
    w,s,_=wa;w.state.connection_failed('whatsapp_logged_out')
    w.archive.release(w.account,w.owner,w.state.lease[1])
    calls=[]
    async def connect():calls.append(True)
    w.provider.connect=connect
    async def check():
        task=asyncio.create_task(w.run());await asyncio.sleep(.15);w.stopping.set()
        await asyncio.wait_for(task,3)
    asyncio.run(check())
    assert calls==[]
    assert source(w,s)['operations']['connection']['state']=='blocked'
