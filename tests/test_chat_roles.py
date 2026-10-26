"""The account's own role in Telegram channels and groups (ANYM-533). Synthetic Telethon entities and chats only."""
import asyncio
from datetime import datetime,timezone
from types import SimpleNamespace as NS

import pytest
from telethon import types
from telethon.extensions import BinaryReader

from messaging_service.archive import Archive
from messaging_service.chat_roles import derive,observation,valid
from messaging_service.collector_state import CollectorState
from messaging_service.envelope import stable_id
from messaging_service.telegram_collector import TelethonProvider
from test_archive import event
from test_collectors import worker  # noqa: F401  (fixture)
from test_compact_queue_rows import recent_queue  # noqa: F401  (fixture)
from test_extend_key import old_schema_store
from test_telegram_operations import GroupClient,run

UNTIL=datetime(2038,1,1,tzinfo=timezone.utc)


def parsed(entity):
    # The provider hands over entities parsed from the wire, whose unset flags read False, not None.
    return BinaryReader(bytes(entity)).tgread_object()


def channel(**flags):
    return parsed(types.Channel(id=1001,title='Synthetic',photo=types.ChatPhotoEmpty(),date=None,access_hash=1,**flags))


def ban(**rights):return types.ChatBannedRights(until_date=UNTIL,**rights)


CASES={
    'creator_channel':(lambda:channel(creator=True,broadcast=True),'channel','creator',True),
    'admin_with_post':(lambda:channel(broadcast=True,admin_rights=types.ChatAdminRights(post_messages=True)),'channel','admin',True),
    'admin_without_post':(lambda:channel(broadcast=True,admin_rights=types.ChatAdminRights(edit_messages=True)),'channel','admin',False),
    'subscription':(lambda:channel(broadcast=True),'channel','member',False),
    'left_channel':(lambda:channel(broadcast=True,left=True),'channel','left',None),
    'forbidden_channel':(lambda:parsed(types.ChannelForbidden(id=1001,access_hash=1,title='Synthetic',broadcast=True)),'channel','left',None),
    'group_default_send_ban':(lambda:channel(megagroup=True,default_banned_rights=ban(send_messages=True)),'group','member',False),
    'group_personal_send_ban':(lambda:channel(megagroup=True,banned_rights=ban(send_messages=True)),'group','member',False),
    'group_member':(lambda:channel(megagroup=True,default_banned_rights=ban(send_media=True)),'group','member',True),
    'group_admin_over_default_ban':(lambda:channel(megagroup=True,admin_rights=types.ChatAdminRights(ban_users=True),
        default_banned_rights=ban(send_messages=True)),'group','admin',True),
    'basic_group_creator':(lambda:parsed(types.Chat(id=1001,title='Synthetic',photo=types.ChatPhotoEmpty(),participants_count=2,
        date=None,version=1,creator=True)),'group','creator',True),
    'missing_entity':(lambda:None,'channel','unknown',None),
    'unreadable_entity':(lambda:NS(left='no',creator=False),'channel','unknown',None),
    'private_chat':(lambda:NS(is_self=False),'private','unknown',None),
}


# spec: ANYM-533
@pytest.mark.parametrize('case',list(CASES))
def test_role_and_can_post_derive_from_the_loaded_entity(case):
    make,chat_type,role,can_post=CASES[case]
    assert derive(make(),chat_type)==(role,can_post)


# spec: ANYM-533
def test_malformed_role_observation_reads_unknown_and_username_keeps_only_a_valid_handle():
    assert observation(channel(creator=True,broadcast=True,username='synthetic_flow'),'channel')=={
        'my_role':'creator','can_post':True,'username':'synthetic_flow'}
    assert observation(NS(left=False,creator=False,username='not a handle!'),'channel')['username'] is None
    unknown={'my_role':'unknown','can_post':None,'username':None}
    for value in (None,'creator',{'my_role':'owner','can_post':True,'username':None},
                  {'my_role':'left','can_post':True,'username':None},{'my_role':'admin','can_post':'yes','username':None}):
        assert valid(value)==unknown


class Dialogs:
    """One synthetic dialog page; every page load goes through the provider's rpc()."""
    def __init__(self,dialogs):self.buffer=None;self.index=0;self.dialogs=dialogs
    def __aiter__(self):return self
    async def __anext__(self):
        if self.buffer is None:self.buffer=self.dialogs;self.index=0
        if self.index==len(self.buffer):raise StopAsyncIteration
        item=self.buffer[self.index];self.index+=1;return item


def page(entities):
    rows=[NS(id=100,is_channel=False,is_user=True,is_group=False,name='Fixture',entity=NS(is_self=False),dialog=None)]
    for i,(make,chat_type,*_) in enumerate(CASES[c] for c in ('creator_channel','subscription','group_personal_send_ban','missing_entity')):
        rows.append(NS(id=-1001-i,is_channel=True,is_user=False,is_group=chat_type=='group',name=f'Synthetic {i}',
                       entity=make() if entities else None,dialog=None))
    return rows


def batches(dialogs):
    requests=[]
    class Governor:
        async def call(self,operation,phase):requests.append(phase);return await operation()
    provider=TelethonProvider.__new__(TelethonProvider)
    # The client offers iter_dialogs only: any other Telegram request would fail the test.
    provider.client=NS(iter_dialogs=lambda **kwargs:Dialogs(dialogs));provider.governor=Governor()
    async def collect():return [row async for batch in provider.dialog_batches(None) for row in batch]
    return asyncio.run(collect()),requests


# spec: ANYM-533
def test_dialog_batches_carry_role_beside_read_observation_without_extra_requests():
    rows,requests=batches(page(True))
    _,baseline=batches(page(False))
    assert requests==baseline==['telegram_discovery','telegram_discovery']
    assert [row[5] for row in rows]==[None,
        {'my_role':'creator','can_post':True,'username':None},{'my_role':'member','can_post':False,'username':None},
        {'my_role':'member','can_post':False,'username':None},{'my_role':'unknown','can_post':None,'username':None}]
    # The read observation stays the closed reply_state element.
    assert all(set(row[4])=={'observed_at','unread_count','read_inbox_max_id','is_self_chat','basis'} for row in rows)


def role(store,account,chat):
    with store.pool.connection() as c:
        return c.execute('SELECT my_role,can_post,username,role_observed_at FROM chats WHERE id=%s',
                         (stable_id('chat',account,chat),)).fetchone()


CREATOR={'my_role':'creator','can_post':True,'username':'synthetic_flow'}
MEMBER={'my_role':'member','can_post':False,'username':None}


def discover(state,dialogs):
    state.discovery_started();state.inventory(dialogs,None,discovery=True);state.discovery_finished()


# spec: ANYM-533
def test_discovery_stores_role_replay_keeps_observation_and_absent_chat_reads_unknown(worker):
    w,s,_=worker
    discover(w.state,[('-1001','channel','Owner notes','channel',None,CREATOR),('-1002','channel','News','channel',None,MEMBER),
        ('-1003','channel','Broken','channel',None,{'my_role':'owner'}),('100','user','Fixture','private',None,None)])
    first=role(s,w.account,'-1001')
    assert {k:first[k] for k in CREATOR}==CREATOR and first['role_observed_at']
    # A malformed observation does not abort the batch; the chat reads unknown.
    assert role(s,w.account,'-1003')['my_role']=='unknown' and role(s,w.account,'100')['my_role'] is None
    # A cached replay between passes neither refreshes the time nor overwrites the role.
    w.state.inventory([('-1001','channel','Owner notes','channel',None,MEMBER)],None)
    assert role(s,w.account,'-1001')==first
    # The next pass no longer lists -1002: its role is unknown, its last observation time stays.
    before=role(s,w.account,'-1002')['role_observed_at']
    discover(w.state,[('-1001','channel','Owner notes','channel',None,CREATOR)])
    assert role(s,w.account,'-1001')['role_observed_at']>first['role_observed_at']
    assert role(s,w.account,'-1002')=={'my_role':'unknown','can_post':None,'username':None,'role_observed_at':before}


CATALOG=[('-1001','channel','Owner notes','channel',None,CREATOR),
         ('-1002','channel','Team posts','channel',None,{'my_role':'admin','can_post':True,'username':None}),
         ('-1003','channel','Moderated','channel',None,{'my_role':'admin','can_post':False,'username':None}),
         ('-1004','channel','News','channel',None,MEMBER),
         ('-1005','channel','Old','channel',None,{'my_role':'left','can_post':None,'username':None}),
         ('-1006','channel','Group','group',None,{'my_role':'member','can_post':True,'username':None}),
         ('-1007','channel','Not yet seen','channel',None,None),('100','user','Fixture','private',None,None)]


def ids(result):return [row['provider_chat_id'] for row in result['items']]


# spec: ANYM-533
def test_list_chats_filters_by_own_role_and_counts_unobserved_channels(worker):
    w,s,key=worker
    discover(w.state,CATALOG)
    owned=s.list_chats(key,chat_types=['channel'],my_roles=['creator','admin'])
    assert sorted(ids(owned))==['-1001','-1002','-1003']
    assert owned['unknown_role_rows']==1 and owned['hint']=='role_captured_on_next_catalog_pass'
    row=next(r for r in owned['items'] if r['provider_chat_id']=='-1001')
    assert row['my_role']=='creator' and row['can_post'] is True and row['role_observed_at']
    assert sorted(ids(s.list_chats(key,my_roles=['creator','admin'],can_post=True)))==['-1001','-1002']
    assert ids(s.list_chats(key,chat_types=['group'],my_roles=['member']))==['-1006']
    # Pagination keeps the filter; pages add up to the filtered list.
    seen,after=[],''
    while True:
        result=s.list_chats(key,chat_types=['channel'],my_roles=['creator','admin'],limit=1,after=after)
        seen+=ids(result);after=result['next_cursor']
        if not after:break
    assert sorted(seen)==['-1001','-1002','-1003']
    # Without a role filter there is no unknown count; private chats read unknown.
    plain=s.list_chats(key)
    assert 'unknown_role_rows' not in plain and {r['provider_chat_id']:r['my_role'] for r in plain['items']}['100']=='unknown'
    for bad in ({'my_roles':['owner']},{'can_post':'yes'}):
        with pytest.raises(Exception,match='invalid_filter'):s.list_chats(key,**bad)


# spec: ANYM-533
@pytest.mark.parametrize('query',['synthetic_flow','@synthetic_flow','@Synthetic_Flo'])
def test_list_chats_query_matches_channel_username_with_or_without_at(worker,query):
    w,s,key=worker
    discover(w.state,CATALOG)
    assert ids(s.list_chats(key,query=query))==['-1001']


# spec: ANYM-533
def test_code_on_schema_45_reads_unknown_role_and_ignores_role_capture(database,tmp_path):
    with old_schema_store(database,tmp_path,45) as store:
        archive=Archive(store);archive.register('roles-tg','telegram','Synthetic roles',discover=True)
        archive.ingest(event(account='roles-tg',platform='telegram',chat='-1001',chat_title='Owner notes',provider_id='1',
                             metadata={'chat_type':'channel'}))
        state=CollectorState(archive,'roles-tg',('fixture-owner',archive.acquire('roles-tg','fixture-owner')))
        discover(state,[('-1001','channel','Owner notes','channel',None,CREATOR)])
        key=store.issue_key('roles-reader',['roles-tg'])
        assert [(r['my_role'],r['can_post']) for r in store.list_chats(key)['items']]==[('unknown',None)]
        filtered=store.list_chats(key,my_roles=['creator'],query='@synthetic_flow')
        assert filtered['items']==[]
        assert store.list_chats(key,my_roles=['creator'])['unknown_role_rows']==1


class ChannelClient(GroupClient):
    """GroupClient whose channel entity carries the account's own flags as parsed from the wire."""
    def __init__(self,**flags):super().__init__(admin=False);self.flags=flags

    def entity(self,basic=False):
        return parsed(types.Channel(id=100,title='Synthetic',photo=types.ChatPhotoEmpty(),date=None,access_hash=1,**self.flags))


# spec: ANYM-533
@pytest.mark.parametrize('flags,expected',[
    ({'broadcast':True,'creator':True},('creator',True)),
    ({'broadcast':True,'admin_rights':types.ChatAdminRights(edit_messages=True)},('admin',False)),
    ({'broadcast':True},('member',False)),
    ({'megagroup':True,'banned_rights':ban(send_messages=True)},('member',False)),
],ids=['creator','admin_without_post','subscription','restricted_member'])
def test_chat_info_reports_own_role_and_can_post_from_the_live_entity(flags,expected):
    client=ChannelClient(**flags)
    data=run(client,'chat_info',provider_chat='-1000000000100')['data']
    assert (data['my_role'],data['can_post'])==expected
    # The same single full-channel request; no extra Telegram request for the role.
    assert [type(c).__name__ for c in client.calls if not isinstance(c,tuple)]==['GetFullChannelRequest']


# spec: ANYM-533
def test_worst_case_queue_rows_with_roles_stay_within_the_size_guard(recent_queue):
    import json
    from test_compact_queue_rows import assert_each_fact_once,compact_queue
    s,key=recent_queue
    with s.pool.connection() as c:
        c.execute('''UPDATE chats SET my_role='creator',can_post=true,role_observed_at=clock_timestamp(),username=%s
            WHERE account=ANY(%s)''',('a'*64,['queue-tg','queue-wa']))
    result=compact_queue(s,key,my_roles=['creator','admin'])
    assert len(result['items'])==25 and all(row['my_role']=='creator' and row['can_post'] is True for row in result['items'])
    assert 'unknown_role_rows' not in result
    for row in result['items']:assert_each_fact_once(row)
    # The ANYM-269 guard holds with the role fields on every row.
    assert len(json.dumps(result,ensure_ascii=False))<=30000


MALFORMED={
    'admin_rights_not_an_object':channel(broadcast=True,admin_rights=types.ChatAdminRights(post_messages=True)),
    'group_banned_rights_not_an_object':channel(megagroup=True),
    'channel_admin_rights_without_post_flag':channel(broadcast=True),
}


# spec: ANYM-533
@pytest.mark.parametrize('case',list(MALFORMED))
def test_malformed_rights_read_unknown_and_never_grant_posting(case):
    entity=MALFORMED[case]
    # Swap in rights that no Telegram object has; the derivation must not read missing flags as false.
    if case=='admin_rights_not_an_object':entity.admin_rights='broken'
    elif case=='group_banned_rights_not_an_object':entity.banned_rights='broken'
    else:entity.admin_rights=NS(edit_messages=True)
    chat_type='group' if case.startswith('group') else 'channel'
    assert derive(entity,chat_type)==('unknown',None)
    assert observation(entity,chat_type)['my_role']=='unknown'


# spec: ANYM-533
def test_list_chats_refuses_an_empty_role_filter(worker):
    w,s,key=worker
    discover(w.state,CATALOG)
    with pytest.raises(Exception,match='invalid_filter'):s.list_chats(key,my_roles=[])
    with pytest.raises(Exception,match='invalid_filter'):s.list_chats(key,my_roles=[],chat_types=['channel'],can_post=True)


# spec: ANYM-533
def test_chat_info_result_reads_unknown_role_before_migration_046(database,tmp_path):
    from messaging_service.chat_roles import gate_chat_info
    answer={'outcome':'confirmed','data':{'provider_chat_id':'-1001','title':'Owner notes','my_role':'creator','can_post':True}}
    with old_schema_store(database,tmp_path,45) as store,store.pool.connection() as c:
        gated=gate_chat_info(c,answer)
    assert gated['data']['my_role']=='unknown' and gated['data']['can_post'] is None and gated['data']['title']=='Owner notes'
    with database.pool.connection() as c:
        assert gate_chat_info(c,answer)==answer
    with database.pool.connection() as c:
        assert gate_chat_info(c,None) is None and gate_chat_info(c,{'outcome':'refused'})=={'outcome':'refused'}
