"""ANYM-476: live search hits are archived through the backfill page-commit path; synthetic fakes only."""
import asyncio
from datetime import datetime,timezone
from types import SimpleNamespace as NS

from telethon import types
from telethon._updates import EntityCache

from messaging_service.envelope import stable_id
from messaging_service.messenger_operations import validate_operation
from messaging_service.subscriptions import match_subscriptions
from messaging_service.telegram_operations import execute
from test_collectors import message,worker as worker  # noqa: F401  (worker is a fixture)

PERSON=types.User(100,access_hash=5,first_name='Fixture')
MEMBER=types.User(11,access_hash=6,first_name='Synthetic',last_name='Member')
OWNER=types.User(500,access_hash=7,first_name='Owner',is_self=True)
GROUP=types.Chat(22,'Synthetic group',None,3,datetime.fromtimestamp(0,timezone.utc),1)
DOCUMENT=types.Document(id=200,access_hash=0,file_reference=b'reference',date=datetime(2026,10,2,tzinfo=timezone.utc),
    mime_type='application/octet-stream',size=4,dc_id=1,attributes=[types.DocumentAttributeFilename('test.bin')])


def hit(number,text='zebra hit',*,peer=None,sender=None,day=2,edited=None,media=None):
    return types.Message(number,peer or types.PeerUser(100),datetime(2026,10,day,12,tzinfo=timezone.utc),text,
        from_id=types.PeerUser(sender) if sender else None,edit_date=edited,media=media)


class Client:
    """Fake Telethon client: one canned answer; the owner is user 500."""
    def __init__(self,response):self.response,self._self_id,self._mb_entity_cache=response,500,EntityCache()
    async def __call__(self,request):return self.response


def search(w,*messages,after=None):
    """One global live search answered by a fake client on the collector's own operation path."""
    async def provider(job):
        answer=await execute(Client(types.messages.Messages(list(messages),[],[GROUP],[PERSON,MEMBER,OWNER])),job)
        if after:after()
        return answer
    w.provider.execute_operation=provider
    job={'operation':'search_messages_live','mutation':False,'expires_at':4e9,'account':w.account,'chat_id':'',
         'provider_chat':None,'parameters':validate_operation('telegram','search_messages_live',{'query':'zebra'})}
    return asyncio.run(w.execute_operation(job))['data']['hits']


def rows(store,table,where='true'):
    with store.pool.connection() as c:
        return c.execute(f'SELECT * FROM {table} WHERE {where}').fetchall()


def counts(store):
    return {t:len(rows(store,t)) for t in ('messages','message_revisions','events','media_jobs','chats','collector_chats')}


# spec: ANYM-476
def test_new_identical_edited_deleted_and_conflicting_hits_follow_the_archive_rules(worker):
    w,store,_=worker
    asyncio.run(w.sync_once())
    mid=stable_id('msg',w.account,'100','50')
    first=search(w,hit(50))
    assert first==[{**first[0],'stored':True,'message_id':mid}]
    assert [(r['body'],r['revision']) for r in rows(store,'messages',f"id='{mid}'")]==[('zebra hit',1)]
    before=counts(store)
    search(w,hit(50))
    assert counts(store)==before
    edited=datetime(2026,10,3,tzinfo=timezone.utc)
    search(w,hit(50,'zebra hit, edited',edited=edited))
    assert [(r['body'],r['revision']) for r in rows(store,'messages',f"id='{mid}'")]==[('zebra hit, edited',2)]
    # A deletion observed before the hit (archive.py tombstone rule) stores it as deleted.
    with store.pool.connection() as c:
        c.execute("INSERT INTO provider_tombstones VALUES(%s,'100','51',%s)",(w.account,edited))
    assert search(w,hit(51))[0]['stored']
    assert [r['deleted'] for r in rows(store,'messages',f"id='{stable_id('msg',w.account,'100','51')}'")]==[True]
    stored=stable_id('msg',w.account,'100','5')
    original=rows(store,'messages',f"id='{stored}'")
    conflict=search(w,hit(5,day=1))
    assert (conflict[0]['stored'],conflict[0]['message_id'])==(True,stored)
    assert rows(store,'messages',f"id='{stored}'")==original


# spec: ANYM-476
def test_hit_in_an_uncatalogued_chat_gets_its_chat_row_through_the_inventory(worker):
    w,store,_=worker
    w.history_since=datetime(2026,9,1,tzinfo=timezone.utc)
    assert search(w,hit(5,peer=types.PeerChat(22)))[0]['stored']
    [chat]=rows(store,'collector_chats',"provider_chat='-22'")
    assert (chat['peer_kind'],chat['history_since'],chat['initialized'])==('chat',w.history_since,False)
    [row]=rows(store,'chats',f"id='{stable_id('chat',w.account,'-22')}'")
    assert (row['title'],row['chat_type'])==('Synthetic group','group')


# spec: ANYM-476
def test_hit_older_than_history_since_is_reported_and_not_stored(worker):
    w,store,_=worker
    w.history_since=datetime(2026,9,1,tzinfo=timezone.utc)
    old=hit(7,day=1)
    old.date=datetime(2026,8,1,tzinfo=timezone.utc)
    before=counts(store)
    found=search(w,old,hit(8))
    assert {k:found[0][k] for k in ('stored','reason')}=={'stored':False,'reason':'outside_history_window'}
    assert 'message_id' not in found[0]
    assert found[1]['stored'] and found[1]['message_id']==stable_id('msg',w.account,'100','8')
    assert counts(store)['messages']==before['messages']+1


# spec: ANYM-476
def test_hit_with_media_creates_the_same_jobs_as_a_backfill_page(worker):
    w,store,_=worker
    media=types.MessageMediaDocument(document=DOCUMENT)
    search(w,hit(60,media=media))
    asyncio.run(w.commit_page([hit(61,media=media)],'100','Fixture','user','backfill'))
    def jobs(number):
        return [(r['state'],r['descriptor'],r['account']) for r in
                rows(store,'media_jobs',f"message='{stable_id('msg',w.account,'100',number)}'")]
    assert jobs('60') and jobs('60')==jobs('61')


# spec: ANYM-476
def test_hits_do_not_advance_coverage_and_reach_only_unfiltered_updates(worker):
    w,store,_=worker
    rights=['read','subscription-write','subscription-ack']
    key=store.issue_key('live-search-reader',[w.account],permissions=rights,key_permissions=rights)
    asyncio.run(w.sync_once())
    chat=stable_id('chat',w.account,'100')
    sid=store.create_subscription(key,[chat])['subscription_id']
    coverage=rows(store,'collector_chats')
    search(w,hit(70),hit(71,peer=types.PeerChat(22)))
    assert [r for r in rows(store,'collector_chats') if r['provider_chat']=='100']==coverage
    match_subscriptions(store)
    assert store.get_subscription_events(key,sid)['items']==[]
    found=[x for x in store.get_updates(key,'all',100)['items']
           if x['message'] in (stable_id('msg',w.account,'100','70'),stable_id('msg',w.account,'-22','71'))]
    assert len(found)==2 and {(x['origin'],x['is_backfill']) for x in found}=={('backfill',True)}
    asyncio.run(w.capture(NS(message=message(72))))
    w.spool.drain(w.state.apply)
    match_subscriptions(store)
    assert len(store.get_subscription_events(key,sid)['items'])==1


# spec: ANYM-476
def test_an_archive_failure_marks_the_hit_keeps_the_answer_and_queues_nothing(worker):
    w,store,_=worker
    def broken(*args):raise OSError('synthetic archive failure')
    w.state.apply=broken
    found=search(w,hit(80))
    assert [(h['provider_id'],h['stored'],h['reason']) for h in found]==[('80',False,'not_stored')]
    assert rows(store,'messages',f"id='{stable_id('msg',w.account,'100','80')}'")==[]
    assert not list(w.spool.path.rglob('*.json'))


# spec: ANYM-476
def test_a_chat_excluded_before_the_commit_reports_not_stored(worker):
    w,store,_=worker
    def exclude():w.archive.register(w.account,'telegram','Synthetic collector',exclude_chats=['100'],discover=True)
    found=search(w,hit(81),after=exclude)
    assert [(h['stored'],h['reason']) for h in found]==[(False,'not_stored')]
    assert rows(store,'messages',f"id='{stable_id('msg',w.account,'100','81')}'")==[]


# spec: ANYM-476
def test_saved_messages_and_sender_names_match_an_initialized_history_page(worker):
    w,store,_=worker
    found=search(w,hit(82,peer=types.PeerUser(500)),hit(83,peer=types.PeerChat(22),sender=11))
    assert all(h['stored'] for h in found)
    [note]=rows(store,'messages',f"id='{stable_id('msg',w.account,'500','82')}'")
    assert note['direction']=='outgoing'
    [profile]=rows(store,'sender_profiles',"sender='11'")
    assert profile['display_name']=='Synthetic Member'
