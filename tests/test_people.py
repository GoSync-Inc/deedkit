from types import SimpleNamespace as NS
import logging
import asyncio

import pytest
from starlette.testclient import TestClient

from messaging_service.adapters import telegram_message,whatsapp_message
from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.store import APIError
from messaging_service.security_logging import redact_library_logs
from test_archive import event
from test_collectors import worker,message


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
