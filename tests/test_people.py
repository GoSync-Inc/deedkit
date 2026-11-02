from types import SimpleNamespace as NS
import logging
import asyncio

import pytest
from starlette.testclient import TestClient

from messaging_service.adapters import telegram_message
from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.store import APIError
from messaging_service.security_logging import redact_library_logs
from test_archive import event
from test_collectors import worker as worker,message


def directory(seeded):
    s,a,b=seeded
    ar=Archive(s);ar.register('people-test','telegram','Synthetic people',discover=True)
    key=s.issue_key('people-reader',['people-test'])
    mid=ar.ingest(event(account='people-test',sender_profile={'display_name':'Анна Иванова','username':'anna_test'}))['message']
    return s,ar,key,mid


def test_names_usernames_and_sender_ids_do_not_merge_accounts_or_chats(seeded):
    s,ar,key,mid=directory(seeded)
    first=s.search_people(key,'АННА')['items'][0]
    assert first['sender_id']=='42' and first['source_message']==mid
    assert s.search_people(key,'@Anna_Test')['items']==[first]
    assert s.search_people(key,'42')['items']==[first]
    assert s.search(key,account=first['account'],sender=first['sender_id'])['items'][0]['id']==mid
    assert s.search_people(key,accounts=[])['items']==[]
    ar.ingest(event(account='people-test',chat='private',sender_profile={'display_name':'HiddenAlias'}))
    assert len(s.search_people(key)['items'])==2
    s.set_access('people-reader',chats=[first['chat']])
    assert s.search_people(key,'HiddenAlias')['items']==[]
    assert s.search_people(key)['items']==[first]
    assert s.search_people(seeded[1])['items']==[]


def test_metadata_refresh_does_not_edit_message_or_reveal_deleted_source(seeded):
    s,ar,key,mid=directory(seeded)
    assert ar.ingest(event(account='people-test',sender_profile={'display_name':'New Name','username':'new'}))['outcome']=='unchanged'
    assert s.get_context(key,mid)['message']['revision']==1
    assert s.search_people(key,'Анна')['items']==[]
    assert s.search_people(key,'new')['items'][0]['source_message']==mid
    ar.ingest(event(account='people-test',provider_id='1',sent_at='2026-09-01T00:00:00Z',
                    updated_at='2026-09-01T00:00:00Z',sender_profile={'display_name':'Old Name'}))
    assert s.search_people(key,'Old')['items']==[]
    ar.ingest(event(account='people-test',deleted=True,updated_at='2026-10-02T00:00:00Z'))
    assert s.search_people(key)['items']==[]


def test_people_pagination_and_literal_patterns(seeded):
    s,ar,key,_=directory(seeded)
    ar.ingest(event(account='people-test',sender='43',provider_id='19',sender_profile={'display_name':'name%_literal'}))
    assert len(s.search_people(key,'%_')['items'])==1
    first=s.search_people(key,limit=1)
    second=s.search_people(key,after=first['next_cursor'],limit=1)
    assert len(second['items'])==1 and first['items'][0]['id']!=second['items'][0]['id']
    assert second['next_cursor'] is None
    with pytest.raises(APIError,match='not_found'):s.search_people(seeded[1],after=first['next_cursor'])


def fold_directory(seeded):
    s,ar,key,_=directory(seeded)
    for sender,name in (('51','Фёдор Пример'),('52','Федор Другой'),('53','Ёлка Лесная'),('54','Елена Ёлкина')):
        ar.ingest(event(account='people-test',sender=sender,provider_id='fold-'+sender,sender_profile={'display_name':name}))
    return s,ar,key


# spec: ANYM-663
def test_people_yo_and_ye_are_one_letter_in_both_directions(seeded):
    s,_,key=fold_directory(seeded)
    names=lambda q,**kw:{r['display_name']:r['match_type'] for r in s.search_people(key,q,**kw)['items']}
    assert names('Федор')=={'Федор Другой':'literal','Фёдор Пример':'literal_folded'}
    assert names('фёдор')=={'Федор Другой':'literal_folded','Фёдор Пример':'literal'}
    assert set(names('ФЕДОР'))==set(names('фёдор'))=={'Федор Другой','Фёдор Пример'}
    assert set(names('Фёдор'))=={'Федор Другой','Фёдор Пример'}   # е + U+0308 is read as ё
    assert names('  @Елка   ')=={'Ёлка Лесная':'literal_folded'} and names('ёлка')=={'Ёлка Лесная':'literal'}
    assert names('Анна')=={'Анна Иванова':'literal'}    # no е or ё in the query: today's LIKE path


# spec: ANYM-663
def test_people_literal_matches_sort_before_folded_ones_and_the_cursor_pages(seeded):
    s,_,key=fold_directory(seeded)
    full=s.search_people(key,'федор')['items']
    assert [r['match_type'] for r in full]==['literal','literal_folded']
    seen=[];page=s.search_people(key,'федор',limit=1)
    while True:
        seen+=[r['id'] for r in page['items']]
        if not page['next_cursor']:break
        page=s.search_people(key,'федор',limit=1,after=page['next_cursor'])
    assert seen==[r['id'] for r in full]


# spec: ANYM-663
def test_people_fold_keeps_group_by_sender_and_hidden_chats(seeded):
    s,ar,key=fold_directory(seeded)
    grouped=s.search_people(key,'федор',group_by_sender=True)['items']
    assert {r['match_type'] for r in grouped}=={'literal','literal_folded'}
    ar.ingest(event(account='people-test',chat='private',sender='55',provider_id='fold-55',sender_profile={'display_name':'Тайный Фёдор'}))
    assert len(s.search_people(key,'федор')['items'])==3
    first=s.search_people(key,'анна')['items'][0]
    s.set_access('people-reader',chats=[first['chat']])
    assert {r['display_name'] for r in s.search_people(key,'федор')['items']}=={'Федор Другой','Фёдор Пример'}
    assert s.search_people(key,'тайный фёдор')['items']==[] and s.search_people(key,'тайный федор')['items']==[]


# spec: ANYM-663
def test_people_folded_rows_sort_after_literal_ones_also_with_transliteration(seeded):
    s,ar,key=fold_directory(seeded)
    ar.ingest(event(account='people-test',sender='56',provider_id='fold-56',
        sender_profile={'display_name':'Фёдор Пример Два','username':'fedor'}))
    for grouped in (False,True):
        rows=s.search_people(key,'федор',transliterate=True,group_by_sender=grouped)['items']
        assert [r['match_type'] for r in rows]==['literal']+['literal_folded']*2


def people_binding(p,query,**changes):
    from messaging_service.search_query import cursor_binding
    return cursor_binding(p,'people',{'query':query,'accounts':None,'chats':None,'transliterate':False,
        'group_by_sender':False,'spelling_candidates':2,**changes})


# spec: ANYM-663
def test_people_cursors_issued_before_the_fold_still_continue(seeded):
    from messaging_service.search_query import decode_cursor,encode_cursor
    s,ar,key,_=directory(seeded)
    for sender,name in (('71','xxfedor'),('72','yyfedor'),('73','Анна Иванова Вторая')):
        ar.ingest(event(account='people-test',sender=sender,provider_id='old-'+sender,sender_profile={'display_name':name}))
    with s.pool.connection() as c:c.execute("UPDATE sender_profiles SET id='cursor-'||sender WHERE sender IN ('71','72')")
    with s.authorized(key,'search_people') as (c,p):
        # A transliteration-only candidate was rank 7 and still is: its old cursor continues after it.
        old=encode_cursor(people_binding(p,'федор',transliterate=True),{'rank':7,'id':'cursor-71'})
        legacy_double_space=people_binding(p,'анна  иванова')
    assert [r['id'] for r in s.search_people(key,'федор',transliterate=True,after=old)['items']]==['cursor-72']
    # The binding of a query with inner spaces is the one it always had (the match itself collapses them).
    first=s.search_people(key,'Анна  Иванова',limit=1)
    assert len(first['items'])==1 and first['next_cursor']
    assert set(decode_cursor(first['next_cursor'],legacy_double_space))=={'rank','id'}
    assert len(s.search_people(key,'Анна  Иванова',after=first['next_cursor'],limit=1)['items'])==1


# spec: ANYM-663
def test_people_rank_eight_is_still_an_invalid_cursor_for_a_fold_query(seeded):
    from messaging_service.search_query import encode_cursor
    s,_,key=fold_directory(seeded)
    with s.authorized(key,'search_people') as (c,p):
        flat=encode_cursor(people_binding(p,'федор'),{'rank':8,'id':'x'})
        grouped=encode_cursor(people_binding(p,'федор',group_by_sender=True),{'rank':8,'account':'people-test','sender':'51'})
    with pytest.raises(APIError,match='invalid_cursor'):s.search_people(key,'федор',after=flat)
    with pytest.raises(APIError,match='invalid_cursor'):s.search_people(key,'федор',group_by_sender=True,after=grouped)


# spec: ANYM-663
def test_people_fold_pattern_is_never_user_controlled(seeded):
    s,ar,key,_=directory(seeded)
    for sender,name in (('61','ёж.* (x)'),('62','ежабв'),('63','еж50xx'),('64','ёж[y]'),('65','ёж\\z'),
                        ('66','ёж50%_'),('67','ёж('),('68','ёж|аб')):
        ar.ingest(event(account='people-test',sender=sender,provider_id='meta-'+sender,sender_profile={'display_name':name}))
    found=lambda q:sorted(r['sender_id'] for r in s.search_people(key,q)['items'])
    assert found('еж.*')==['61'] and found('еж.* (x)')==['61'] and found('еж.')==['61']
    assert found('еж[y]')==['64'] and found('еж\\z')==['65'] and found('еж50%_')==['66']
    assert found('еж(')==['67'] and found('еж|аб')==['68'] and found('еж|ёж')==[] and found('еж%')==[]
    assert found('еж50')==['63','66']


# spec: ANYM-663
def test_people_query_normalizer_and_fold_pattern():
    from messaging_service.name_variants import fold_pattern,normalize
    assert normalize('  @Фёдор \t Иванов ')=='фёдор иванов'
    assert normalize('@@Anna')=='anna' and normalize('')==''
    assert fold_pattern('федор')=='ф[её]дор' and fold_pattern('фёдор')=='ф[её]дор'
    assert fold_pattern('ёж') is None and fold_pattern('anna') is None and fold_pattern('ann') is None
    assert fold_pattern('еж.*')=='[её]ж\\.\\*' and fold_pattern('еле на')=='[её]л[её]\\ на'
    assert fold_pattern('ёa(%_\\')=='[её]a\\(\\%\\_\\\\'


def test_people_rest_mcp_permission_and_audit(seeded):
    s,_,key,_=directory(seeded)
    with TestClient(create_app(s,['testserver'])) as client:
        headers={'Authorization':'Bearer '+key,'Accept':'application/json, text/event-stream'}
        rest=client.post('/v1/search_people',json={'query':'@anna'},headers=headers)
        rpc=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':'search_people','arguments':{'query':'@anna'}}}).json()['result']
        assert rest.status_code==200 and rpc['structuredContent']==rest.json()
        s.set_access('people-reader',permissions=[])
        assert client.post('/v1/search_people',json={},headers=headers).status_code==403
    assert s.audit_tail('people-reader',1)[0]['outcome']=='denied'


@pytest.mark.parametrize('kwargs',[{'query':None},{'query':'x\x00'},{'accounts':'oops'},
    {'chats':[True]},{'after':True},{'limit':True}])
def test_people_invalid_parameters(seeded,kwargs):
    with pytest.raises(APIError):seeded[0].search_people(seeded[1],**kwargs)


class LookupReached(Exception):pass
class NoLookup:
    def __getitem__(self,key):raise LookupReached(key)
    def execute(self,*args):raise LookupReached('execute')

# spec: ANYM-44
@pytest.mark.parametrize('kwargs,code',[({'query':'q'*201},'invalid_filter'),({'after':'a'*4097},'invalid_filter'),
    ({'query':None},'invalid_filter'),({'after':'x\x00'},'invalid_filter'),({'accounts':'oops'},'invalid_filter'),
    ({'chats':[True]},'invalid_filter'),({'limit':True},'invalid_limit')])
def test_people_parameter_codes_and_length_bounds_before_any_lookup(kwargs,code):
    from messaging_service.people_candidates import search_people
    with pytest.raises(APIError) as error:search_people(None,NoLookup(),NoLookup(),**kwargs)
    assert error.value.code==code
    # The bound itself is accepted: validation passes and the first lookup is reached.
    with pytest.raises(LookupReached):search_people(None,NoLookup(),NoLookup(),query='q'*200,after='a'*4096)

def test_telegram_cached_sender_has_no_network_lookup():
    m=message(1);m.sender=NS(first_name='Anna',last_name='Example',username='anna_example')
    value=telegram_message('fixture',m)
    assert value['sender_profile']=={'display_name':'Anna Example','username':'anna_example'}


def test_telegram_child_logs_cannot_include_login_data_or_exception(caplog):
    redact_library_logs()
    for prefix in ['personal_os.telegram.transport','telethon']:
        log=logging.getLogger(prefix+'.network.mtprotosender')
        with caplog.at_level(logging.ERROR):
            try:raise ValueError('SYNTHETIC_OTP_SECRET')
            except ValueError:log.error('request=%r',{'phone':'SYNTHETIC_PHONE_SECRET'},exc_info=True,stack_info=True)
    assert 'SYNTHETIC_' not in caplog.text
    assert 'exception_type=ValueError' in caplog.text


def test_private_telegram_peer_does_not_starve_other_chats(worker):
    from telethon.errors import ChannelPrivateError
    w,s,key=worker; original=w.sync_chat; visited=[]
    async def dialogs(limit):return [('-100','channel','Private'),('100','user','Visible')]
    async def sync(chat,kind,title):
        visited.append(chat)
        if chat=='-100':raise ChannelPrivateError(None)
        await original(chat,kind,title)
    w.provider.dialogs=dialogs;w.sync_chat=sync
    asyncio.run(w.sync_once())
    assert visited==['-100','100'] and s.search(key)['items']
    assert s.capabilities(key)['sources'][0]['detail_code']=='configured_chat_unavailable'


def test_backup_discovers_extra_collectors_by_explicit_label(monkeypatch):
    from messaging_service.backup_runner import Docker
    docker=Docker()
    monkeypatch.setattr(docker,'call',lambda *a:'a b c d e')
    def inspect(cid):
        service={'a':'telegram','b':'telegram2','c':'whatsapp3','d':'api','e':'telegram'}[cid]
        labels={'com.docker.compose.service':service}
        if cid in ('b','c'):labels['org.messaging-archive.collector']='true'
        if cid=='e':labels['com.docker.compose.oneoff']='True'
        return {'Id':cid,'Config':{'Labels':labels}}
    monkeypatch.setattr(docker,'inspect',inspect)
    assert [c['service'] for c in docker.running_collectors('fixture')]==['telegram','telegram2','whatsapp3']


@pytest.fixture
def wa_names(seeded):
    from messaging_service.whatsapp_collector import WhatsAppState
    from test_whatsapp import event as wa_event,capture
    s,*_=seeded;ar=Archive(s)
    ar.register('wa-names','whatsapp','Synthetic names',discover=True)
    owner='names-fixture';epoch=ar.acquire('wa-names',owner)
    state=WhatsAppState(ar,'wa-names',(owner,epoch))
    key=s.issue_key('names-reader',['wa-names'])
    def say(mid,chat,sender,name='',pn=None):
        e=wa_event(mid,chat=chat);e.update(senderId=sender,senderName=name)
        if pn:e['identityObservations']=[{'identity_id':sender,'pn':pn,'lid':sender.split(':')[0]+'@lid' if ':' in sender else sender,
            'provenance':'webjs_contact_cache_v1','observed_at':'2026-10-14T00:00:00Z'}]
        return state.apply(capture(e,account='wa-names'))['message']
    def named(mid):
        row=s.get_context(key,mid,limit=1)['message']
        return row['sender'],row.get('sender_name'),row.get('sender_name_basis')
    return s,key,say,named


def test_group_sender_without_push_name_is_named_from_readable_private_chat(wa_names):
    s,key,say,named=wa_names
    private=say(1,'123@c.us','123@c.us','Synthetic Bob')
    group=say(2,'777@g.us','123@c.us')
    assert named(private)==('123@c.us','Synthetic Bob','message')
    assert named(group)==('123@c.us','Synthetic Bob','seen_elsewhere')
    row=s.search(key,'item 2')['items'][0]
    assert (row['sender_name'],row['sender_name_basis'])==('Synthetic Bob','seen_elsewhere')
    from messaging_service.presentation import lean_message
    assert lean_message(row)['sender_name_basis']=='seen_elsewhere'
    assert 'sender_name_basis' not in lean_message(s.search(key,'item 1')['items'][0])


def test_name_from_a_chat_the_key_cannot_read_is_not_used(wa_names):
    s,key,say,named=wa_names
    say(1,'123@c.us','123@c.us','Synthetic Bob')
    group=say(2,'777@g.us','123@c.us')
    s.set_access('names-reader',chats=[s.get_context(key,group,limit=1)['message']['chat']])
    assert named(group)==('123@c.us','+123','phone_number')
    assert 'Synthetic Bob' not in str(s.search(key,'item'))


def test_lid_sender_uses_visible_non_conflicting_linked_phone_identity_name(wa_names):
    s,key,say,named=wa_names
    say(1,'555@c.us','555@c.us','Synthetic Carol')
    group=say(2,'777@g.us','900:3@lid',pn='555@c.us')
    assert named(group)==('900:3@lid','Synthetic Carol','linked_identity')
    person=[r for r in s.search_people(key)['items'] if r['sender_id']=='900:3@lid'][0]
    assert (person['display_name'],person['sender_name'],person['sender_name_basis'])==('','Synthetic Carol','linked_identity')
    say(3,'778@g.us','900@lid',pn='556@c.us')
    assert named(group)==('900:3@lid',None,None)


def test_phone_sender_falls_back_to_number_and_lid_sender_to_null(wa_names):
    s,key,say,named=wa_names
    assert named(say(1,'777@g.us','321@c.us'))==('321@c.us','+321','phone_number')
    assert named(say(2,'777@g.us','901:1@lid'))==('901:1@lid',None,None)


def test_stored_name_equal_to_sender_id_counts_as_absent(wa_names,seeded):
    s,key,say,named=wa_names
    assert named(say(1,'777@g.us','444@c.us','444'))==('444@c.us','+444','phone_number')
    assert named(say(2,'777@g.us','902@lid','902@lid'))==('902@lid',None,None)
    tg,ar,tg_key,_=directory(seeded)
    raw=ar.ingest(event(account='people-test',provider_id='30',sender='77',sender_profile={'display_name':'+77'}))['message']
    message=tg.get_context(tg_key,raw,limit=1)['message']
    assert message['sender']=='77' and message.get('sender_name') is None
    assert [r['sender_name'] for r in tg.search_people(tg_key)['items'] if r['sender_id']=='77']==[None]


def test_raw_name_rule_covers_device_suffix_whitespace_and_non_whatsapp_accounts(wa_names,seeded):
    s,key,say,named=wa_names
    assert named(say(1,'777@g.us','900:2@lid','900@lid'))==('900:2@lid',None,None)
    assert named(say(2,'777@g.us','445@c.us',' 445 '))==('445@c.us','+445','phone_number')
    assert named(say(3,'777@g.us','903@lid','903:2@lid'))==('903@lid',None,None)
    say(4,'446@c.us','446@c.us','446:1@c.us')
    assert named(say(5,'777@g.us','446@c.us'))==('446@c.us','+446','phone_number')
    tg,ar,tg_key,_=directory(seeded)
    shaped=ar.ingest(event(account='people-test',provider_id='31',sender='123@c.us'))['message']
    message=tg.get_context(tg_key,shaped,limit=1)['message']
    assert message['sender']=='123@c.us' and message.get('sender_name') is None


def test_repeated_identity_observations_still_resolve_one_link(wa_names):
    s,key,say,named=wa_names
    say(1,'555@c.us','555@c.us','Synthetic Carol')
    group=say(2,'777@g.us','900@lid',pn='555@c.us')
    with s.pool.connection() as c:
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            SELECT 'repeat-'||g,m.account,m.chat,'repeat-'||g,m.sender,'synthetic',m.sent_at FROM messages m,
            generate_series(1,2000) g WHERE m.id=%s''',(group,))
        c.execute('''INSERT INTO whatsapp_identity_observations(account,source_message,identity_id,pn,lid,provenance,
            first_observed_at,observed_at) SELECT account,'repeat-'||g,'900@lid','555@c.us','900@lid','webjs_contact_cache_v1',
            now(),now() FROM messages,generate_series(1,2000) g WHERE id=%s''',(group,))
    assert named(group)==('900@lid','Synthetic Carol','linked_identity')


def test_conversation_views_keep_the_fallback_basis(wa_names):
    from messaging_service.presentation import render
    s,key,say,named=wa_names
    say(1,'123@c.us','123@c.us','Synthetic Bob')
    group=say(2,'777@g.us','123@c.us')
    chat=s.get_context(key,group,limit=1)['message']['chat']
    full=[r for r in s.list_chats(key,view='conversation',include_coverage=False)['items'] if r['id']==chat][0]
    latest=full['conversation']['latest_known_meaningful_message']
    assert (latest['sender_name'],latest['sender_name_basis'])==('Synthetic Bob','seen_elsewhere')
    lean=[r for r in render('list_chats',s.list_chats(key,view='conversation',include_coverage=False),'compact')['items']
        if r['id']==chat][0]
    assert lean['last_message']['sender_name_basis']=='seen_elsewhere'
