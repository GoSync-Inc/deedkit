"""Shared contact cards reach agents as a bounded block: names, phones, messenger IDs (ANYM-529).

Synthetic cards and numbers only; the disposable test database for archive tests.
"""
from datetime import datetime,timezone
import json
import logging

import pytest
from telethon import types

from pathlib import Path

from messaging_service.adapters import normalize_phone,telegram_message,whatsapp_message
from messaging_service.archive import Archive
from messaging_service.store import APIError
from messaging_service.subscriptions import match_subscriptions
from messaging_service.collector_state import CaptureSpool,CollectorState
from messaging_service.conversation import present_message
from messaging_service.envelope import contact_block,stable_id,validate
from test_archive import event as archive_event
from test_whatsapp import capture,event,wa  # noqa: F401  (wa is a fixture)

PHONE='+6281200000001'
CARD={'status':'parsed','contacts':[{'name':'Synthetic Friend','vcard_name':'Friend Card',
    'phones':[{'number':PHONE,'normalized':True,'type':'cell','whatsapp_id':'6281200000001'},
              {'number':'0812-0000-0002','normalized':False}]}]}
# Synthetic vCards and the blocks the whatsmeow and whatsapp-web.js parsers emit for them.
PARSER_OUTPUTS=json.loads((Path(__file__).parent/'fixtures'/'contact-card-parser-outputs.json').read_text())
DENIED={'forbidden','not_found','chat_not_found','message_not_found','access_denied'}


def card_event(mid,card=CARD,**changes):
    value={**event(mid,body='Synthetic Friend'),'messageType':'vcard',**changes}
    if card is not None:value['contactCard']=card
    return value


# spec: ANYM-529
def test_contact_block_is_closed_and_bounded():
    assert contact_block(CARD)==CARD
    contact={'name':'A','phones':[]}
    phone={'number':PHONE,'normalized':True}
    bad=[{**CARD,'extra':1},{'status':'other','contacts':[]},
        {'status':'parsed','contacts':[contact]*21},
        {'status':'parsed','contacts':[{'name':'A','phones':[phone]*11}]},
        {'status':'parsed','contacts':[{'name':'x'*257,'phones':[]}]},
        {'status':'parsed','contacts':[{'name':'A\nfake line','phones':[]}]},
        {'status':'parsed','contacts':[{'name':'A','phones':[{'number':'0812','normalized':True}]}]},
        {'status':'parsed','contacts':[{'name':'A','telegram_user_id':'12a','phones':[]}]},
        {'status':'parsed','contacts':[{'name':'','phones':[]}]},
        {'status':'unavailable','contacts':[contact]},
        {'status':'parsed','contacts':[{'name':'n'*250,'phones':[{'number':'x'*250,'normalized':False}]*10}]*20}]
    for value in bad:
        with pytest.raises(ValueError):contact_block(value)
    assert contact_block({'status':'parsed','contacts':[contact]*20})['contacts']==[contact]*20


# spec: ANYM-529
def test_contact_block_only_on_contact_messages_and_never_on_deleted():
    base=archive_event(metadata={'message_kind':'contact','contact':CARD})
    assert validate(base)['metadata']['contact']==CARD
    with pytest.raises(ValueError):validate({**base,'metadata':{'message_kind':'text','contact':CARD}})
    assert 'contact' not in validate({**base,'deleted':True})['metadata']
    unavailable={**base,'metadata':{'message_kind':'contact','contact':{'status':'unavailable','contacts':[]}}}
    assert 'contact' not in validate(unavailable)['metadata']


# spec: ANYM-529
def test_phone_normalization_is_strict_and_matches_whatsmeow():
    assert normalize_phone('+62 812-0000-0001')==(PHONE,True)
    assert normalize_phone('tel:0062 (812) 0000.0001')==(PHONE,True)
    assert normalize_phone(' +62 812 0000 0001 ')==(PHONE,True)
    assert normalize_phone(' 0812-0000-0001 ')==('0812-0000-0001',False)
    assert normalize_phone('6281200000001')==('6281200000001',False)
    assert normalize_phone('+0 812')==('+0 812',False)


# spec: ANYM-529
def test_whatsapp_card_the_archive_cannot_accept_keeps_the_message():
    e=whatsapp_message('wa-one',card_event(1,card={'status':'parsed','contacts':'not a list'}))
    assert e['body']=='Synthetic Friend' and e['metadata']['message_kind']=='contact'
    assert e['metadata']['contact']=={'status':'parse_failed','contacts':[]}
    assert 'contact' not in whatsapp_message('wa-one',card_event(2,messageType='chat'))['metadata']
    deleted=whatsapp_message('wa-one',card_event(3),change='delete',observed_at=1790000100)
    assert 'contact' not in deleted['metadata']


def telegram_contact(**media):
    contact=types.MessageMediaContact(**{'phone_number':'+62 812 0000 0001','first_name':'Synthetic','last_name':'Friend',
        'vcard':'','user_id':777,**media})
    return types.Message(id=19,peer_id=types.PeerUser(42),date=datetime(2026,10,1,tzinfo=timezone.utc),
        message='',media=contact,out=False)


# spec: ANYM-529
def test_telegram_contact_media_fills_the_block_only_when_enabled(monkeypatch):
    assert 'contact' not in telegram_message('tg-owner',telegram_contact())['metadata']
    monkeypatch.setenv('ANYMSG_CAPTURE_CONTACT_CARDS','1')
    e=telegram_message('tg-owner',telegram_contact())
    assert e['metadata']['message_kind']=='contact'
    assert e['metadata']['contact']=={'status':'parsed','contacts':[{'name':'Synthetic Friend','telegram_user_id':'777',
        'phones':[{'number':PHONE,'normalized':True}]}]}
    local=telegram_message('tg-owner',telegram_contact(phone_number='6281200000001',user_id=0,last_name=''))
    assert local['metadata']['contact']['contacts']==[{'name':'Synthetic','phones':[{'number':'6281200000001','normalized':False}]}]
    empty=telegram_message('tg-owner',telegram_contact(phone_number='',first_name='',last_name='',user_id=0))
    assert 'contact' not in empty['metadata']


# spec: ANYM-529
def test_display_has_one_line_per_contact_and_marks_truncation():
    block={'status':'truncated','contacts':[CARD['contacts'][0],{'name':'Second','telegram_user_id':'5',
        'phones':[{'number':'+6281200000003','normalized':True}]},{'name':'','phones':[{'number':'12345','normalized':False}]}]}
    row=present_message({'body':'Synthetic Friend','message_kind':'contact','contact':block},1)
    assert row['display_text'].split('\n')==['[Контакт] Synthetic Friend: '+PHONE+' (WhatsApp) (+1)',
        '[Контакт] Second: +6281200000003 (Telegram)','[Контакт] 12345 [обрезано]']
    assert present_message({'body':'','message_kind':'contact'},1)['display_text']=='[Контакт]'


# spec: ANYM-529
def test_fallback_raw_vcard_body_never_crowds_out_the_contact_line():
    from messaging_service.collector_state import validate_capture as validate_one
    long_card=PARSER_OUTPUTS['cases'][-1]
    vcard='\n'.join(c['vcard'] for c in long_card['cards'])
    e=validate_one(capture(card_event(1,card=long_card['block'],body=vcard)))['envelope']
    row=present_message({'body':vcard,'message_kind':'contact','contact':e['metadata']['contact']},1)
    lines=row['display_text'].split('\n')
    assert lines[0]=='[Контакт] Synthetic 0: +6281200000005' and len(lines)==20 and lines[-1].endswith('[обрезано]')
    assert 'BEGIN:VCARD' not in row['display_text'] and row['body']==vcard


# spec: ANYM-529
def test_every_parser_outcome_drains_through_a_restarted_spool_then_a_normal_message(wa,tmp_path,monkeypatch):
    w,s,key=wa
    queue=CaptureSpool(tmp_path/'cards',reserve_bytes=0)
    # The blocks both WhatsApp parsers emit (the shared fixture), the fallback's "unavailable", a block
    # the archive cannot accept, and a card captured with emission off.
    parsed=[c['block'] for c in PARSER_OUTPUTS['cases']]
    outcomes=[*parsed,{'status':'unavailable','contacts':[]},{'status':'parsed','contacts':'invalid'},None]
    for n,card in enumerate(outcomes):
        queue.put(capture(card_event(2*n+1,card=card)));queue.put(capture(event(2*n+2,body='plain after card '+str(n))))
    restarted=CaptureSpool(queue.path,reserve_bytes=0)
    assert restarted.drain(w.state.apply)==2*len(outcomes) and not list(queue.path.glob('*.json'))
    items={i['provider_id']:i for i in s.search(key,limit=50)['items']}
    assert len(items)==2*len(outcomes)
    assert all(items[event(2*n+2)['messageId']]['body']=='plain after card '+str(n) for n in range(len(outcomes)))
    blocks=[items[event(2*n+1)['messageId']].get('contact') for n in range(len(outcomes))]
    assert blocks==[*parsed,None,{'status':'parse_failed','contacts':[]},None]
    assert [items[event(2*n+1)['messageId']].get('contact_details') for n in range(len(parsed),len(outcomes))]==['unavailable',None,'unavailable']
    assert 'contact_details' not in items[event(1)['messageId']]
    # Telegram: the real adapter's envelope through the same spool, then a normal message.
    monkeypatch.setenv('ANYMSG_CAPTURE_CONTACT_CARDS','1')
    archive=Archive(s);archive.register('tg-owner','telegram','Synthetic TG',discover=True)
    tg=s.issue_key('tg-reader',['tg-owner'])
    plain=types.Message(id=20,peer_id=types.PeerUser(42),date=datetime(2026,10,1,0,1,tzinfo=timezone.utc),message='plain after telegram card',out=False)
    spool=CaptureSpool(tmp_path/'telegram',reserve_bytes=0)
    for message in (telegram_contact(),plain):
        spool.put({'kind':'message','peer_kind':'user','envelope':telegram_message('tg-owner',message),
            'captured_at':'2026-10-01T00:02:00+00:00'})
    state=CollectorState(archive,'tg-owner',('tg-fixture',archive.acquire('tg-owner','tg-fixture')))
    assert CaptureSpool(spool.path,reserve_bytes=0).drain(state.apply)==2
    found={i['body']:i for i in s.search(tg)['items']}
    assert found['']['contact']['contacts'][0]['phones']==[{'number':PHONE,'normalized':True}]
    assert 'plain after telegram card' in found


# spec: ANYM-529
def test_reads_and_search_find_the_card_by_name_or_digits_within_scope(wa):
    w,s,key=wa
    w.state.apply(validate_capture(capture(card_event(1))))
    w.state.apply(validate_capture(capture(event(2,body='unrelated text'))))
    mid=next(i['id'] for i in s.search(key)['items'] if i.get('contact'))
    page=s.get_messages(key,s.search(key)['items'][0]['chat'],view='conversation')['items']
    shown=next(i for i in page if i['id']==mid)
    assert shown['display_text']=='[Контакт] Synthetic Friend: '+PHONE+' (WhatsApp) (+1)'
    assert shown['contact']==CARD
    context=s.get_context(key,mid,view='conversation')
    assert any(i.get('contact')==CARD for i in [context.get('message',{}),*context.get('items',[])])
    for query in ('Friend Card','synthetic friend','+62 812 0000 0001','081200000001','200000001','0812-0000-0002'):
        assert [i['id'] for i in s.search(key,query)['items']]==[mid],query
    # Zero-led suffixes of six and seven digits are digits as typed, not a trunk prefix.
    for query in ('000001','0000001','0062 812 0000 0001'):
        assert [i['id'] for i in s.search(key,query)['items']]==[mid],query
    for query in ('00001','+62 812 0000 0009','Nobody'):
        assert s.search(key,query)['items']==[],query
    # Contact names follow the query mode; a contact hit keeps fuzzy search on the exact view.
    for query,mode in (('Friend Synthetic','all'),('"Synthetic Friend"','web'),('Synthetic Friend','phrase'),('ynthetic Fri','exact')):
        assert [i['id'] for i in s.search(key,query,query_mode=mode)['items']]==[mid],(query,mode)
    assert s.search(key,'Friend Synthetic',query_mode='phrase')['items']==[]
    fuzzy=s.search(key,'Synthetic',fuzzy=True)
    assert [i['id'] for i in fuzzy['items']]==[mid] and fuzzy['items'][0]['match_type']!='trigram_fallback'
    w.state.apply(validate_capture(capture(event(3,chat='999@c.us',body='other chat'))))
    chat=shown['chat']
    other=s.issue_key('wa-other-chat',['wa-one'],chats=[stable_id('chat','wa-one','999@c.us')])
    Archive(s).register('wa-two','whatsapp','Synthetic WA two',discover=True)
    elsewhere=s.issue_key('wa-other-account',['wa-two'])
    assert [i['body'] for i in s.search(other)['items']]==['other chat']
    for token in (other,elsewhere):
        assert s.search(token,'Synthetic Friend')['items']==[] and s.search(token,PHONE)['items']==[]
        # The chat page is scope-filtered to nothing; the message itself is not found.
        assert s.get_messages(token,chat)['items']==[]
        with pytest.raises(APIError) as denied:s.get_context(token,mid)
        assert denied.value.code in DENIED,denied.value.code


# spec: ANYM-529
def test_an_empty_body_card_matches_by_name_in_every_query_mode_and_under_fuzzy(wa):
    w,s,key=wa
    # An array card has no body, so only the contact branch can match.
    w.state.apply(validate_capture(capture(card_event(1,body='',messageType='multi_vcard'))))
    w.state.apply(validate_capture(capture(event(2,body='unrelated text'))))
    mid=next(i['id'] for i in s.search(key)['items'] if i.get('contact'))
    for query,mode in (('Friend Synthetic','all'),('"Synthetic Friend"','web'),('Synthetic Friend','phrase'),
            ('ynthetic Fri','exact'),('Friend Card','all')):
        assert [i['id'] for i in s.search(key,query,query_mode=mode)['items']]==[mid],(query,mode)
    assert s.search(key,'Friend Synthetic',query_mode='phrase')['items']==[]
    fuzzy=s.search(key,'Synthetic',fuzzy=True)
    assert [i['id'] for i in fuzzy['items']]==[mid] and fuzzy['items'][0]['match_type']=='contact'


# spec: ANYM-529
def test_deletion_before_after_ingest_and_stale_replay_leave_no_block(wa):
    w,s,key=wa
    def delete(n):
        w.state.apply(validate_capture({'kind':'whatsapp_delete','account':'wa-one','chat':'123@c.us',
            'ids':[event(n)['messageId']],'observed_at':'2026-10-13T00:00:00+00:00'}))
    w.state.apply(validate_capture(capture(card_event(1))));delete(1)
    w.state.apply(validate_capture(capture(card_event(1))))
    delete(2);w.state.apply(validate_capture(capture(card_event(2))))
    for item in s.search(key,limit=50)['items']+s.search(key,'Synthetic Friend')['items']+s.search(key,PHONE)['items']:
        assert 'contact' not in item and PHONE not in json.dumps(item)
    # The deleted messages themselves, as context anchors and in their chat page.
    chat=stable_id('chat','wa-one','123@c.us')
    for n in (1,2):
        anchor=s.get_context(key,stable_id('msg','wa-one','123@c.us',event(n)['messageId']),view='conversation')
        assert PHONE not in json.dumps(anchor,default=str) and 'Friend Card' not in json.dumps(anchor,default=str)
    page=s.get_messages(key,chat,view='conversation',limit=50)
    assert PHONE not in json.dumps(page,default=str)


# spec: ANYM-529
def test_subscription_references_carry_no_contact_fields(wa):
    w,s,key=wa
    permissions=['read','subscription-write']
    token=s.issue_key('wa-subscriber',['wa-one'],permissions=permissions,key_permissions=permissions)
    chat=stable_id('chat','wa-one','123@c.us')
    w.state.apply(validate_capture(capture(event(2,body='before the subscription'))))
    sid=s.create_subscription(token,[chat],kinds=['message.created','message.deleted'],
        origins=['live','fixture','backfill','reconcile'])['subscription_id']
    w.state.apply(validate_capture(capture(card_event(1))))
    w.state.apply(validate_capture({'kind':'whatsapp_delete','account':'wa-one','chat':'123@c.us',
        'ids':[event(1)['messageId']],'observed_at':'2026-10-13T00:00:00+00:00'}))
    match_subscriptions(s)
    items=s.get_subscription_events(token,sid,limit=50)['items']
    assert items and all('contact' not in (i.get('metadata') or {}) for i in items)
    assert PHONE not in json.dumps(items,default=str) and 'Friend Card' not in json.dumps(items,default=str)
    assert items[0]['metadata']['message_kind']=='contact'


# spec: ANYM-529
def test_no_contact_field_reaches_log_lines(wa,caplog):
    w,s,key=wa
    caplog.set_level(logging.DEBUG)
    w.state.apply(validate_capture(capture(card_event(1))))
    w.state.apply(validate_capture(capture(card_event(2,card={'status':'parsed','contacts':'invalid'}))))
    s.search(key,PHONE)
    text=caplog.text+json.dumps([r.__dict__ for r in caplog.records],default=str)
    assert PHONE not in text and '0812-0000-0002' not in text and 'Friend Card' not in text


def validate_capture(value):
    from messaging_service.collector_state import validate_capture as validate_one
    return validate_one(value)


# spec: ANYM-529
def test_migration_047_indexes_only_contact_metadata_and_repeats_over_schema46_rows(database,tmp_path):
    from test_extend_key import old_schema_store
    with old_schema_store(database,tmp_path,46) as store:
        Archive(store).register('cards-wa','whatsapp','Synthetic cards',discover=True)
        for provider_id in ('1','2'):
            Archive(store).ingest(archive_event(account='cards-wa',platform='whatsapp',chat='chat-1',provider_id=provider_id))
        with store.pool.connection() as c:
            card,plain=[r['id'] for r in c.execute('SELECT id FROM messages ORDER BY provider_id')]
            c.execute('INSERT INTO message_metadata(message,revision,metadata) VALUES (%s,1,%s),(%s,1,%s)',
                (card,json.dumps({'contact':CARD}),plain,json.dumps({'summary':'synthetic'})))
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='46'
            assert c.execute("SELECT to_regclass('message_metadata_contact') AS i").fetchone()['i'] is None
        store.migrate();store.migrate()
        with store.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='49'
            assert c.execute('SELECT count(*) AS n FROM archive_migrations WHERE version=47').fetchone()['n']==1
            index=c.execute('''SELECT i.indisvalid,i.indisready,pg_get_expr(i.indpred,i.indrelid) AS predicate
                FROM pg_index i WHERE i.indexrelid='message_metadata_contact'::regclass''').fetchone()
            assert index['indisvalid'] and index['indisready'] and "'contact'" in index['predicate']
            assert [r['message'] for r in c.execute("SELECT message FROM message_metadata WHERE metadata ? 'contact'")]==[card]


def build_contact_index(store,shape):
    with store.pool.connection() as c:
        index={'other_key':'message_metadata(revision)',
            'no_predicate':'message_metadata(message)'}.get(shape,"message_metadata(message) WHERE metadata ? 'contact'")
        c.execute('CREATE INDEX message_metadata_contact ON '+index)
        # What an interrupted CREATE INDEX CONCURRENTLY leaves behind.
        if shape=='invalid':c.execute("UPDATE pg_index SET indisvalid=false WHERE indexrelid='message_metadata_contact'::regclass")


# spec: ANYM-529
def test_migration_047_keeps_an_index_built_before_it(database,tmp_path):
    from test_extend_key import old_schema_store
    with old_schema_store(database,tmp_path,46) as store:
        build_contact_index(store,'valid')
        with store.pool.connection() as c:
            before=c.execute("SELECT 'message_metadata_contact'::regclass::oid AS i").fetchone()['i']
        store.migrate();store.migrate()
        with store.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='49'
            assert c.execute('SELECT count(*) AS n FROM archive_migrations WHERE version=47').fetchone()['n']==1
            assert c.execute("SELECT 'message_metadata_contact'::regclass::oid AS i").fetchone()['i']==before


# spec: ANYM-529
@pytest.mark.parametrize('shape',['invalid','other_key','no_predicate'])
def test_migration_047_refuses_an_unusable_prebuilt_index(database,tmp_path,shape):
    import psycopg
    from test_extend_key import old_schema_store
    with old_schema_store(database,tmp_path,46) as store:
        build_contact_index(store,shape)
        with pytest.raises(psycopg.errors.RaiseException,match='not a valid index'):store.migrate()
        with store.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='46'
            c.execute('DROP INDEX message_metadata_contact')
        store.migrate()
        with store.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='49'
            assert c.execute('''SELECT indisvalid FROM pg_index
                WHERE indexrelid='message_metadata_contact'::regclass''').fetchone()['indisvalid']
