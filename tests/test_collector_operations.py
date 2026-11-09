"""Operational evidence and transport failures, using synthetic sessions only."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil

from hypothesis import given, strategies as st
import pytest

from messaging_service.collector_state import CollectorState, session_lock
from messaging_service.envelope import stable_id
from messaging_service.sync_status import get_sync_status
from messaging_service.whatsapp_collector import NodeProvider
from test_collectors import worker as worker
from test_whatsapp import wa as wa, event, capture


def source(w, s, chats=None):
    with s.pool.connection() as c:
        return get_sync_status(c, {'accounts': [w.account], 'chat_ids': chats})['sources'][0]


# A Telegram create_group on the collector's own operation path (ANYM-750): the provider confirms a new basic
# group; the collector catalogs it at once and the receipt says whether it is in the archive.
GROUP = '-4001'


def set_policy(w, policy):
    with w.archive.store.pool.connection() as c:
        c.execute('UPDATE account_policies SET discover_chats=%s,include_chats=%s,exclude_chats=%s '
                  'WHERE account=%s', (policy.get('discover', True), policy.get('include', []),
                                       policy.get('exclude', []), w.account))


def create_group(w, policy=None, during=None, **job):
    if policy:
        set_policy(w, policy)
    async def provider(work):
        if during:
            during()
        return {'outcome': 'confirmed', 'data': {'provider_chat_id': GROUP, 'missing_invitee_ids': [],
                                                 'all_members_added': True, 'basis': 'provider_created_chat'}}
    w.provider.execute_operation = provider
    return asyncio.run(w.execute_operation({'operation': 'create_group',
                                            'parameters': {'title': 'Synthetic team', 'user_ids': ['42']}, **job}))


# spec: ANYM-750
def test_created_group_is_listed_and_sendable_at_once_and_its_receipt_says_collected(worker):
    w, s, key = worker
    answer = create_group(w)
    chat_id = stable_id('chat', w.account, GROUP)
    assert answer['outcome'] == 'confirmed'
    assert answer['data']['chat_id'] == chat_id and answer['data']['archive'] == 'collected'
    assert 'next_step' not in answer['data']
    assert chat_id in [chat['id'] for chat in s.list_chats(key, accounts=[w.account])['items']]
    # No discovery pass has run: the archive row written by the operation is enough to send.
    w.archive.set_sending(w.account, True)
    w.state.phase('connection', 'connected', 'provider_ready')
    sender = s.issue_key('group-sender', [w.account], permissions=['read', 'send'])
    queued = s.send_message(sender, account=w.account, chat_id=chat_id, text='Synthetic hello',
                            request_id='synthetic-request-0750')
    assert queued['status'] == 'queued'


# spec: ANYM-750
@pytest.mark.parametrize('case,reason', [('discovery_off', 'discovery_disabled'), ('excluded', 'chat_excluded'),
                                         ('write_raises', 'catalog_failed'), ('budget_spent', 'catalog_timeout'),
                                         ('write_too_slow', 'catalog_timeout'), ('pool_timeout', 'catalog_timeout'),
                                         ('transaction_timeout', 'catalog_timeout'),
                                         ('discovery_off_during_create', 'discovery_disabled'),
                                         ('excluded_and_write_raises', 'chat_excluded')])
def test_created_group_receipt_names_why_it_is_not_in_the_archive(worker, monkeypatch, caplog, case, reason):
    import time
    from messaging_service import telegram_collector
    from messaging_service.telegram_operations import CREATE_GROUP_NEXT_STEPS
    from psycopg.errors import TransactionTimeout
    from psycopg_pool import PoolTimeout
    w, s, key = worker
    policy, job, during = None, {}, None
    if case == 'discovery_off':
        policy = {'discover': False}
    if case in ('excluded', 'excluded_and_write_raises'):
        policy = {'exclude': [GROUP]}
    if case == 'discovery_off_during_create':
        # the policy read before the provider call still allows the group; the one the write is judged by does not
        during = lambda: set_policy(w, {'discover': False})  # noqa: E731
    failure = {'write_raises': RuntimeError('synthetic-secret-detail'),
               'excluded_and_write_raises': RuntimeError('synthetic-secret-detail'),
               'pool_timeout': PoolTimeout('synthetic-secret-detail'),
               'transaction_timeout': TransactionTimeout('synthetic-secret-detail')}.get(case)
    if failure is not None:
        def broken(*args, **kwargs):
            raise failure
        monkeypatch.setattr(CollectorState, 'catalog', broken)
    if case == 'budget_spent':
        job = {'_ends_at': time.monotonic()}
    if case == 'write_too_slow':
        monkeypatch.setattr(telegram_collector, 'CATALOG_SECONDS', .05)
        monkeypatch.setattr(telegram_collector, 'STORE_GRACE', 0)
        real = CollectorState.catalog
        def slow(*args, **kwargs):
            time.sleep(.3)
            return real(*args, **kwargs)
        monkeypatch.setattr(CollectorState, 'catalog', slow)
    caplog.set_level('WARNING', 'messaging_service.telegram_collector')
    answer = create_group(w, policy, during, **job)
    assert answer['outcome'] == 'confirmed'
    data = answer['data']
    assert data['archive'] == 'not_collected' and data['archive_reason'] == reason
    assert data['next_step'] == CREATE_GROUP_NEXT_STEPS[reason] and 'chat_id' not in data
    assert data['provider_chat_id'] == GROUP
    assert 'synthetic-secret-detail' not in json.dumps(answer) + caplog.text
    if case in ('discovery_off', 'excluded', 'discovery_off_during_create'):
        assert stable_id('chat', w.account, GROUP) not in [c['id'] for c in s.list_chats(key)['items']]


# spec: ANYM-750
def test_cancelled_group_catalog_write_still_settles_the_confirmed_receipt_first(worker, monkeypatch):
    from threading import Event
    from messaging_service import telegram_collector
    w, _, _ = worker
    monkeypatch.setattr(telegram_collector, 'STORE_GRACE', 0)
    entered, release = Event(), Event()
    def stalled(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return False
    monkeypatch.setattr(CollectorState, 'catalog', stalled)
    async def provider(work):
        return {'outcome': 'confirmed', 'data': {'provider_chat_id': GROUP, 'basis': 'provider_created_chat'}}
    w.provider.execute_operation = provider
    job = {'operation': 'create_group', 'parameters': {'title': 'Synthetic team', 'user_ids': ['42']}}
    async def scenario():
        task = asyncio.ensure_future(w.execute_operation(job))
        while not entered.is_set():
            await asyncio.sleep(.01)
        task.cancel()
        try:
            return await task
        finally:
            release.set()
    answer = asyncio.run(scenario())
    assert job['_cancel_after_settle'] is True  # the command worker settles the receipt, then stops
    assert answer['outcome'] == 'confirmed' and answer['data']['archive'] == 'not_collected'
    assert answer['data']['archive_reason'] == 'catalog_failed' and 'chat_id' not in answer['data']


def creation_notice(mid, chat=GROUP):
    """The live service message Telegram sends when a basic group is created in the app (no DeedKit operation)."""
    from telethon import types
    return types.MessageService(id=mid, peer_id=types.PeerChat(-int(chat)), from_id=types.PeerUser(42),
                                date=datetime(2026, 11, 9, 1, 10, tzinfo=timezone.utc), out=False,
                                action=types.MessageActionChatCreate(title='Synthetic team', users=[42]))


def registered_events(update):
    """The events the handlers TelethonProvider.subscribe registers for capture build from one raw update."""
    from messaging_service.telegram_collector import TelethonProvider
    handlers = []

    class Client:
        def add_event_handler(self, callback, event):
            handlers.append((callback, event))
    provider = TelethonProvider.__new__(TelethonProvider)
    provider.client = Client()
    capture = object()
    provider.subscribe(capture, object())
    builders = [event for callback, event in handlers if callback is capture]
    assert builders
    return [event for event in (type(b).build(update, None, None) for b in builders) if event is not None]


# spec: ANYM-845
@pytest.mark.parametrize('policy,listed', [({}, True), ({'exclude': [GROUP]}, False), ({'discover': False}, False)],
                         ids=['discovery_on', 'excluded', 'discovery_off_not_included'])
def test_a_group_whose_only_message_is_its_creation_notice_is_listed_after_the_next_discovery_pass_not_live(
        worker, policy, listed):
    from telethon import types
    w, s, key = worker
    if policy:
        set_policy(w, policy)
    w.policy = w.state.policy()
    chat_id = stable_id('chat', w.account, GROUP)

    def chats():
        return [c['id'] for c in s.list_chats(key, accounts=[w.account])['items']]
    assert chat_id not in chats()
    # The registered handlers build an event from an ordinary new message in the same chat ...
    text = types.Message(id=8, peer_id=types.PeerChat(-int(GROUP)), date=datetime(2026, 11, 9, 1, 11, tzinfo=timezone.utc),
                         message='Synthetic text', from_id=types.PeerUser(42), out=False)
    assert len(registered_events(types.UpdateNewMessage(message=text, pts=2, pts_count=1))) == 1
    # ... but none from the creation notice: Telethon 1.45.0 NewMessage skips MessageService updates.
    for built in registered_events(types.UpdateNewMessage(message=creation_notice(7), pts=1, pts_count=1)):
        asyncio.run(w.capture(built))
    w.spool.drain(w.state.apply)
    assert chat_id not in chats()
    # The next dialog discovery pass lists the group, as far as the account's policy allows it.
    async def dialogs(limit):
        return [(GROUP, 'chat', 'Synthetic team', 'group')]
    w.provider.dialogs, w.provider.messages, w._next_discovery = dialogs, [], 0
    asyncio.run(w.sync_once())
    assert (chat_id in chats()) is listed
    if listed:
        w.archive.set_sending(w.account, True)
        w.state.phase('connection', 'connected', 'provider_ready')
        sender = s.issue_key('notice-sender', [w.account], permissions=['read', 'send'])
        preview = s.preview_message(sender, w.account, chat_id, 'Synthetic hello')
        assert preview['chat_id'] == chat_id and preview.get('preview_digest')


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


GAP_CHAT='123@c.us'


def flag_gap(w):
    """Run real passes until the provider has no bridge to the old head."""
    asyncio.run(w.sync_once())
    w.provider.messages=[event(20),event(21)]
    asyncio.run(w.sync_once());asyncio.run(w.sync_once())
    progress=w.state.progress(GAP_CHAT,'user');assert progress['gap'] is True
    return progress


def clear(s,account,chat=GAP_CHAT):
    from messaging_service.collector_control import clear_gap
    with s.pool.connection() as c:return clear_gap(c,account,chat)


def clear_during_next_pass(w,s,prepare=lambda:None):
    original=w.state.begin_attempt
    def begin(chat,kind):
        progress=original(chat,kind);w.state.begin_attempt=original
        assert clear(s,w.account)=='gap_cleared';prepare()
        return progress
    w.state.begin_attempt=begin
    asyncio.run(w.sync_once())
    return w.state.progress(GAP_CHAT,'user')


# spec: ANYM-700
def test_clear_gap_removes_only_the_flag_and_records_the_operator(wa):
    w,s,_=wa;before=flag_gap(w)
    assert clear(s,w.account)=='gap_cleared'
    after=w.state.progress(GAP_CHAT,'user')
    assert 'gap' not in after and after['gap_cleared_by']=='operator' and type(after['gap_cleared_at']) is float
    assert {k:v for k,v in after.items() if not k.startswith('gap_cleared')}=={k:v for k,v in before.items() if k!='gap'}


# spec: ANYM-700
def test_clear_gap_refuses_a_chat_without_the_flag_unknown_chats_and_bad_input(wa):
    w,s,_=wa;asyncio.run(w.sync_once())
    before=w.state.progress(GAP_CHAT,'user')
    for account,chat,code in ((w.account,GAP_CHAT,'gap_not_set'),(w.account,'999@c.us','chat_not_found'),
            ('wa-other',GAP_CHAT,'chat_not_found'),(w.account,'not a chat','invalid_chat'),
            ('bad account',GAP_CHAT,'invalid_account')):
        with pytest.raises(ValueError,match=code):clear(s,account,chat)
    assert w.state.progress(GAP_CHAT,'user')==before


# spec: ANYM-700
def test_clear_gap_repeat_changes_nothing(wa):
    w,s,_=wa;flag_gap(w);clear(s,w.account)
    first=w.state.progress(GAP_CHAT,'user')
    assert clear(s,w.account)=='gap_already_cleared'
    assert w.state.progress(GAP_CHAT,'user')==first


# spec: ANYM-700
def test_clear_committed_during_a_pass_stays_cleared(wa):
    w,s,_=wa;flag_gap(w)
    after=clear_during_next_pass(w,s)
    assert 'gap' not in after and after['gap_cleared_by']=='operator'
    assert after['gap_cleared_at']>after['last_attempt_at']
    with s.pool.connection() as c:
        health=c.execute('SELECT detail_code FROM collector_health WHERE account=%s',(w.account,)).fetchone()
    assert health['detail_code']=='whatsapp_history_partial'
    asyncio.run(w.sync_once())
    assert 'gap' not in w.state.progress(GAP_CHAT,'user')


# spec: ANYM-700
def test_a_pass_that_sets_a_new_gap_after_a_clear_keeps_the_new_gap(wa):
    w,s,_=wa;flag_gap(w)
    def older_history_gone():w.provider.messages=[event(30)]
    after=clear_during_next_pass(w,s,older_history_gone)
    assert after['gap'] is True and after['gap_cleared_by']=='operator'
    history=source(w,s)['history'];assert history['gap_chats']==1 and history['gap_cleared_chats']==0


# spec: ANYM-700
def test_status_and_evidence_after_the_last_gap_is_cleared(wa):
    from messaging_service.chat_evidence import history,rows
    w,s,_=wa;flag_gap(w)
    scope={'accounts':[w.account],'chat_ids':None}
    with s.pool.connection() as c:
        row=get_sync_status(c,scope)['sources'][0]
        assert history(rows(c,scope)[0])['gaps']==['catchup_gap']
    assert row['history']['gap_chats']==1 and row['history']['gap_cleared_chats']==0
    assert row['detail_code']==row['archive_detail_code']=='whatsapp_catchup_gap'
    assert 'provider_history_gaps' in row['alerts']
    clear(s,w.account)
    with s.pool.connection() as c:
        row=get_sync_status(c,scope)['sources'][0];evidence=history(rows(c,scope)[0])
    # Before the next pass rewrites the stored code: the reader follows the flag.
    assert row['history']['gap_chats']==0 and row['history']['gap_cleared_chats']==1
    assert row['detail_code']==row['archive_detail_code']=='whatsapp_history_partial'
    assert row['detail']['next_action']!='inspect_chat_gaps' and 'provider_history_gaps' not in row['alerts']
    assert row['history']['full_history_verified'] is False and evidence['gaps']==[]


# spec: ANYM-700
def test_clear_gap_command_is_exclusive_and_a_failed_run_changes_nothing(wa,tmp_path,monkeypatch,capsys):
    from messaging_service.collector_control import main
    w,s,_=wa;before=flag_gap(w)
    dsn=tmp_path/'owner.dsn';dsn.write_text(Path(os.environ['PROBE_TEST_DSN_FILE']).read_text());dsn.chmod(0o600)
    base=['collector_control','--dsn-file',str(dsn),'--account',w.account]
    monkeypatch.setattr('sys.argv',base+['--resume','--clear-gap',GAP_CHAT])
    with pytest.raises(SystemExit) as exit_info:main()
    assert exit_info.value.code==2
    capsys.readouterr()
    monkeypatch.setattr('sys.argv',base+['--clear-gap','not a chat'])
    with pytest.raises(SystemExit) as exit_info:main()
    assert exit_info.value.code==1 and capsys.readouterr().out.strip()=='invalid_chat'
    # A held row lock runs into the lock timeout; the output is one fixed code.
    monkeypatch.setattr('sys.argv',base+['--clear-gap',GAP_CHAT])
    with s.pool.connection() as c:
        c.execute('SELECT 1 FROM whatsapp_checkpoints WHERE account=%s FOR UPDATE',(w.account,))
        with pytest.raises(SystemExit) as exit_info:main()
    assert exit_info.value.code==1 and capsys.readouterr().out.strip()=='clear_gap_failed'
    assert w.state.progress(GAP_CHAT,'user')==before
    main()
    assert capsys.readouterr().out.strip()=='gap_cleared' and 'gap' not in w.state.progress(GAP_CHAT,'user')


# spec: ANYM-700
def test_clear_gap_command_reports_one_failure_when_the_commit_fails(tmp_path,monkeypatch,capsys):
    from messaging_service import collector_control
    class CommitFails:
        def __enter__(self):return self
        def __exit__(self,*_):raise collector_control.psycopg.OperationalError('commit failed')
    monkeypatch.setattr(collector_control.psycopg,'connect',lambda *_,**__:CommitFails())
    monkeypatch.setattr(collector_control,'clear_gap',lambda *_:'gap_cleared')
    dsn=tmp_path/'owner.dsn';dsn.write_text('synthetic');dsn.chmod(0o600)
    with pytest.raises(SystemExit) as exit_info:collector_control.run_clear_gap(dsn,'wa-one',GAP_CHAT)
    assert exit_info.value.code==1 and capsys.readouterr().out.strip()=='clear_gap_failed'


POSITION=st.lists(st.one_of(st.integers(0,2**40),st.text(max_size=8)),max_size=2)
PROGRESS=st.dictionaries(st.sampled_from(('head','history_before','catchup_before','catchup_head','history_exhausted','other')),
    st.one_of(POSITION,st.booleans()))
CLOCK=st.one_of(st.none(),st.floats(0,2e9),st.integers(0,2**31),st.text(max_size=4),st.booleans())


# spec: ANYM-700
@given(PROGRESS,CLOCK,CLOCK,st.booleans(),st.booleans())
def test_merge_of_a_stored_clear_touches_only_the_gap_keys(base,start,cleared,had_gap,gap_set):
    from messaging_service.whatsapp_collector import merge_gap_clear
    progress={**base,'last_attempt_at':start};progress.update({'gap':True} if had_gap or gap_set else {})
    stored={'gap_cleared_at':cleared,'gap_cleared_by':'operator'}
    original=dict(progress);merge_gap_clear(progress,stored,gap_set)
    gap_keys=('gap','gap_cleared_at','gap_cleared_by')
    assert {k:v for k,v in progress.items() if k not in gap_keys}=={k:v for k,v in original.items() if k not in gap_keys}
    numbers=all(type(v) in (int,float) for v in (start,cleared))
    later=numbers and cleared>start
    assert ('gap' in progress)==('gap' in original and (gap_set or not later))
    assert ('gap_cleared_at' in progress)==bool(later)


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
