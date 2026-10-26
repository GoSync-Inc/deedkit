"""Shared contact cards reach agents as a bounded block: names, phones, messenger IDs (ANYM-529).

Synthetic cards and numbers only; the disposable test database for archive tests.
"""
from datetime import datetime,timezone
import json
import logging

import pytest
from telethon import types

from messaging_service.adapters import normalize_phone,telegram_message,whatsapp_message
from messaging_service.collector_state import CaptureSpool
from messaging_service.conversation import present_message
from messaging_service.envelope import contact_block,stable_id,validate
from test_archive import event as archive_event
from test_whatsapp import capture,event,wa  # noqa: F401  (wa is a fixture)

PHONE='+6281200000001'
CARD={'status':'parsed','contacts':[{'name':'Synthetic Friend','vcard_name':'Friend Card',
    'phones':[{'number':PHONE,'normalized':True,'type':'cell','whatsapp_id':'6281200000001'},
              {'number':'0812-0000-0002','normalized':False}]}]}


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
def test_every_parser_outcome_drains_through_a_restarted_spool_then_a_normal_message(wa,tmp_path):
    w,s,key=wa
    queue=CaptureSpool(tmp_path/'cards',reserve_bytes=0)
    outcomes=[CARD,{**CARD,'status':'truncated'},{'status':'parse_failed','contacts':[{'name':'Broken','phones':[]}]},
        {'status':'unavailable','contacts':[]},{'status':'parsed','contacts':'invalid'},None]
    for n,card in enumerate(outcomes,1):queue.put(capture(card_event(n,card=card)))
    queue.put(capture(event(9,body='plain after cards')))
    restarted=CaptureSpool(queue.path,reserve_bytes=0)
    assert restarted.drain(w.state.apply)==7 and not list(queue.path.glob('*.json'))
    items={i['provider_id']:i for i in s.search(key,limit=50)['items']}
    assert len(items)==7 and items[event(9)['messageId']]['body']=='plain after cards'
    blocks=[items[event(n)['messageId']].get('contact') for n in range(1,7)]
    assert blocks[:3]==[CARD,{**CARD,'status':'truncated'},outcomes[2]]
    assert blocks[3] is None and blocks[4]=={'status':'parse_failed','contacts':[]} and blocks[5] is None
    assert items[event(4)['messageId']]['contact_details']=='unavailable'
    assert 'contact_details' not in items[event(1)['messageId']]


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
    for query in ('00001','+62 812 0000 0009','Nobody'):
        assert s.search(key,query)['items']==[],query
    w.state.apply(validate_capture(capture(event(3,chat='999@c.us',body='other chat'))))
    other=s.issue_key('wa-other-chat',['wa-one'],chats=[stable_id('chat','wa-one','999@c.us')])
    assert [i['body'] for i in s.search(other)['items']]==['other chat']
    assert s.search(other,'Synthetic Friend')['items']==[] and s.search(other,PHONE)['items']==[]
    with pytest.raises(Exception):s.get_context(other,mid)


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
