"""Residual agent query and presentation feedback (DK-39): reply snippets, chat titles, unknown chats."""
import pytest

from messaging_service.archive import Archive
from messaging_service.envelope import stable_id
from messaging_service.presentation import render
from messaging_service.store import APIError
from messaging_service.error_details import payload
from test_archive import event
from test_file_index import add_file,drain,ids,search,worker_for

LONG='Это длинный вымышленный ответ о встрече во вторник, который должен быть обрезан до восьмидесяти символов в сниппете'


@pytest.fixture()
def replies(seeded):
    s,_,_=seeded
    ar=Archive(s)
    ar.register('reply-ctx','telegram','Synthetic replies',discover=True)
    key=s.issue_key('reply-ctx-reader',['reply-ctx'])
    def add(chat,mid,second,**changes):
        at=f'2026-10-01T12:00:{second:02d}Z'
        return ar.ingest(event(**{'account':'reply-ctx','chat':chat,'provider_id':mid,'sent_at':at,'updated_at':at,**changes}))['message']
    return s,key,add


def reply_to_of(result,message):
    item=next(i for i in result['items'] if i['id']==message)
    return item.get('reply_to')


# spec: ANYM-253
def test_compact_reply_to_includes_snippet(replies):
    s,key,add=replies
    target=add('100','10',1,body=LONG,sender='7',sender_profile={'display_name':'Анна'})
    reply=add('100','11',2,body='Согласна',metadata={'reply_to_provider_id':'10'})
    for view in ('conversation','archive'):
        found=s.search(key,compact=True,view=view)
        compact=render('search',found,'compact')
        item=next(i for i in compact['items'] if i['id']==reply)
        assert item['reply_to_provider_id']=='10'
        assert item['reply_to']['message_id']==target
        assert 0<len(item['reply_to']['snippet'])<=80 and LONG.startswith(item['reply_to']['snippet'][:40])
        assert item['reply_to']['sender_name']=='Анна'
        listed=render('get_messages',s.get_messages(key,stable_id('chat','reply-ctx','100'),compact=True,view=view),'compact')
        assert next(i for i in listed['items'] if i['id']==reply)['reply_to']['message_id']==target
        context=render('get_context',s.get_context(key,target,compact=True,view=view),'compact')
        assert next(i for i in context['context'] if i['id']==reply)['reply_to']['message_id']==target
        assert 'reply_to' not in next(i for i in compact['items'] if i['id']==target)


# spec: ANYM-253
def test_compact_reply_to_never_leaks_an_unarchived_or_foreign_target(replies):
    s,key,add=replies
    add('200','10',1,body='SECRET_OTHER_CHAT_TEXT')
    gone=add('100','12',2,body='SECRET_DELETED_TEXT')
    add('100','13',3,body='ответ на отсутствующее',metadata={'reply_to_provider_id':'99'})
    add('100','14',4,body='ответ в другом чате',metadata={'reply_to_provider_id':'10'})
    add('100','15',5,body='ответ на удалённое',metadata={'reply_to_provider_id':'12'})
    with s.pool.connection() as c:
        c.execute('UPDATE messages SET deleted=true WHERE id=%s',(gone,))
    s.set_access('reply-ctx-reader',chats=[stable_id('chat','reply-ctx','100')])
    for view in ('conversation','archive'):
        text=str(render('search',s.search(key,compact=True,view=view),'compact'))
        assert 'SECRET_' not in text
        items=render('search',s.search(key,compact=True,view=view,query='ответ'),'compact')['items']
        assert len(items)==3 and all('reply_to' not in i and i['reply_to_provider_id'] for i in items)


# spec: ANYM-253
def test_search_files_and_people_include_chat_title(seeded):
    s,a,_=seeded
    add_file(s,'file-title','demo-tg-1',b'Synthetic invoice for the lighthouse',"inv.txt")
    assert all(r['status']=='ready' for r in drain(worker_for(s)))
    full=search(s,a,'lighthouse',chats=['telegram-demo-chat'])
    assert 'file-title' in ids(full)
    row=next(i for i in full['items'] if i['attachment_id']=='file-title')
    assert row['chat_title']=='Synthetic chat' and row['sent_at']
    compact=render('search_files',full,'compact')
    item=next(i for i in compact['items'] if i['attachment_id']=='file-title')
    assert item['chat_title']=='Synthetic chat' and item['sent_at']
    ar=Archive(s);ar.register('title-people','telegram','Synthetic people',discover=True)
    key=s.issue_key('title-people-reader',['title-people'])
    ar.ingest(event(account='title-people',chat_title='Q'*100,sender_profile={'display_name':'Анна Иванова'}))
    for grouped in (False,True):
        found=s.search_people(key,'Анна',group_by_sender=grouped)['items'][0]
        person=found['observations'][0] if grouped else found
        assert person['chat_title']=='Q'*64 and person['source_sent_at']


# spec: ANYM-253
def test_unknown_and_forbidden_chat_are_indistinguishable(seeded):
    s,a,b=seeded
    forbidden='whatsapp-demo-1-chat'
    seen=[]
    for chat in ('no-such-chat',forbidden):
        with pytest.raises(APIError) as error:
            s.search(b,chat=chat)
        assert error.value.status==404
        seen.append((error.value.code,payload(error.value,'search',request_id='r')))
    assert seen[0]==seen[1] and seen[0][0]=='not_found'
    assert seen[0][1]['fields']==['chat']
    with pytest.raises(APIError) as error:
        s.search(b,chats=['no-such-chat',forbidden])
    assert error.value.code=='not_found' and payload(error.value,'search')['fields']==['chats']
    # A list that still names a readable chat intersects with the scope, as documented.
    assert {r['chat'] for r in s.search(b,chats=['no-such-chat',forbidden,'telegram-demo-chat'])['items']}=={'telegram-demo-chat'}
    assert s.search(a,chat=forbidden)['items']
    assert s.search(b,chats=[])['items']==[]
    assert s.search(b,chat='telegram-demo-chat')['items']
    assert s.search(b,chat='')['items']
    with pytest.raises(APIError) as error:
        s.search(b,chat=7)
    assert error.value.code!='not_found'
    # Unrelated reads keep their empty page.
    assert s.get_messages(b,forbidden)['items']==[]


# spec: ANYM-253
def test_reply_to_resolves_a_whatsapp_native_reference(seeded):
    s,_,_=seeded
    ar=Archive(s)
    ar.register('reply-wa','whatsapp','Synthetic WhatsApp',discover=True)
    key=s.issue_key('reply-wa-reader',['reply-wa'])
    chat='120363@g.us'
    def add(mid,second,**changes):
        at=f'2026-10-01T12:00:{second:02d}Z'
        return ar.ingest(event(**{'account':'reply-wa','platform':'whatsapp','chat':chat,'provider_id':mid,
            'sent_at':at,'updated_at':at,**changes}))['message']
    target=add(f'false_{chat}_ABC123_77@lid',1,body='Исходное сообщение группы')
    reply=add(f'false_{chat}_DEF456_88@lid',2,body='ответ',metadata={'reply_to_provider_id':'ABC123'})
    miss=add(f'false_{chat}_GHI789_88@lid',3,body='ответ мимо',metadata={'reply_to_provider_id':'ABC'})
    items=render('search',s.search(key,compact=True),'compact')['items']
    by_id={i['id']:i for i in items}
    assert by_id[reply]['reply_to']['message_id']==target and by_id[reply]['reply_to']['snippet']=='Исходное сообщение группы'
    assert 'reply_to' not in by_id[miss]
