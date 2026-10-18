from datetime import datetime,timezone
from types import SimpleNamespace

import pytest

from messaging_service.adapters import telegram_message,whatsapp_message


def test_telegram_without_network_and_with_original_metadata():
    message=SimpleNamespace(chat_id=-100123,id=19,sender_id=42,message='photo caption',
        date=datetime(2026,10,1,tzinfo=timezone.utc),edit_date=None,out=True,
        file=SimpleNamespace(name=None,ext='.jpg',mime_type='image/jpeg',size=32*1024**2),
        document=None,photo=SimpleNamespace(id=123456))
    e=telegram_message('tg-owner',message)
    assert e['chat']=='-100123' and e['direction']=='outgoing'
    assert e['media'][0]['provider_id']=='123456' and e['media'][0]['bytes']==32*1024**2


def test_telegram_short_send_receipt_uses_verified_self_without_mutating_message():
    from telethon import types
    owner=types.User(id=42,is_self=True,first_name='Synthetic',last_name='Owner',username='fixture_owner')
    message=types.Message(id=19,peer_id=types.PeerUser(100),out=True,message='approved fixture',
        date=datetime(2026,10,1,tzinfo=timezone.utc))
    assert message.sender_id is None
    e=telegram_message('tg-owner',message,verified_self=owner)
    assert e['sender']=='42'
    assert e['sender_profile']=={'display_name':'Synthetic Owner','username':'fixture_owner'}
    assert e['metadata']['chat_type']=='private'
    assert e['metadata']['sender_identity_basis']=='authenticated_self'
    assert message.sender_id is None and message.from_id is None


@pytest.mark.parametrize('peer,out,sender',[
    ('user',False,None),('chat',True,None),('channel',True,None),('user',True,99)])
def test_verified_self_does_not_guess_other_or_anonymous_senders(peer,out,sender):
    from telethon import types
    owner=types.User(id=42,is_self=True,first_name='Synthetic')
    peers={'user':types.PeerUser(100),'chat':types.PeerChat(100),'channel':types.PeerChannel(100)}
    message=types.Message(id=19,peer_id=peers[peer],out=out,message='fixture',
        from_id=types.PeerUser(sender) if sender else None,date=datetime(2026,10,1,tzinfo=timezone.utc))
    expected=str(message.sender_id or '')
    e=telegram_message('tg-owner',message,verified_self=owner)
    assert e['sender']==expected
    assert e['metadata'].get('sender_identity_basis')!='authenticated_self'


def test_verified_self_can_supply_profile_for_explicit_owner_id():
    from telethon import types
    owner=types.User(id=42,is_self=True,first_name='Synthetic',username='fixture_owner')
    message=types.Message(id=19,peer_id=types.PeerChat(100),out=True,from_id=types.PeerUser(42),
        message='fixture',date=datetime(2026,10,1,tzinfo=timezone.utc))
    e=telegram_message('tg-owner',message,verified_self=owner)
    assert e['sender']=='42' and e['sender_profile']['username']=='fixture_owner'
    assert e['metadata'].get('sender_identity_basis')!='authenticated_self'


def test_telegram_webpage_document_metadata_does_not_use_preview_photo():
    from telethon import types,utils
    now=datetime(2026,10,13,tzinfo=timezone.utc)
    document=types.Document(id=200,access_hash=0,file_reference=b'ref',date=now,
        mime_type='application/pdf',size=1234,dc_id=1,
        attributes=[types.DocumentAttributeFilename(file_name='document.pdf')])
    photo=types.Photo(id=300,access_hash=0,file_reference=b'ref',date=now,
        sizes=[types.PhotoSize(type='x',w=2,h=2,size=4)],dc_id=1)
    webpage=types.WebPage(id=20,url='https://example.invalid',display_url='example.invalid',
        hash=0,document=document,photo=photo)
    message=types.Message(id=10,peer_id=types.PeerUser(42),from_id=types.PeerUser(42),
        date=now,message='preview',media=types.MessageMediaWebPage(webpage=webpage))
    assert message.file.size==4  # Pinned Telethon selects the preview by default.
    descriptor=telegram_message('tg-owner',message)['media'][0]
    assert descriptor=={'provider_id':'200','filename':'document.pdf','mime':'application/pdf','bytes':1234}
    assert utils._get_file_info(message.document or message.photo).size==descriptor['bytes']


def test_whatsapp_accounts_mapping_and_delete_time():
    event={'messageId':'true_group_ABC','chatId':'123@g.us','senderId':'456@c.us',
           'body':'caption','fromMe':True,'timestamp':1791000000,'hasMedia':True,
           'filename':'video.mp4','mimetype':'video/mp4','filesize':256*1024**2}
    first=whatsapp_message('wa-1',event); second=whatsapp_message('wa-2',event)
    assert first['account']!=second['account']
    assert first['sender']=='456@c.us' and first['chat']=='123@g.us'
    assert first['media'][0]['bytes']==256*1024**2
    with pytest.raises(ValueError,match='missing_change_time'): whatsapp_message('wa-1',event,change='edit')
    deleted=whatsapp_message('wa-1',event,change='delete',observed_at=1791000030)
    assert deleted['deleted'] and deleted['body']=='' and deleted['media']==[]


def test_owner_attention_is_provider_metadata_not_text_guessing():
    message=SimpleNamespace(chat_id=-123,id=19,sender_id=42,message='@Someone',
        date=datetime(2026,10,1,tzinfo=timezone.utc),edit_date=None,out=False,
        file=None,document=None,photo=None,mentioned=True)
    assert telegram_message('tg-owner',message)['metadata']['mentions_owner'] is True
    del message.mentioned
    assert telegram_message('tg-owner',message)['metadata']['mentions_owner'] is None
    wa={'messageId':'false_123@g.us_A','chatId':'123@g.us','senderId':'456@c.us',
        'timestamp':1791000000,'body':'@Owner','mentionsOwner':True,'replyToOwner':True}
    md=whatsapp_message('wa-owner',wa)['metadata']
    assert md['mentions_owner'] is True and md['reply_to_owner'] is True
    wa.pop('mentionsOwner');wa.pop('replyToOwner')
    assert whatsapp_message('wa-owner',wa)['metadata']['mentions_owner'] is None


def test_telegram_cross_peer_reply_does_not_resolve_in_local_id_namespace():
    from telethon.types import PeerChat
    message=SimpleNamespace(chat_id=-123,id=19,sender_id=42,message='Reply',
        date=datetime(2026,10,1,tzinfo=timezone.utc),edit_date=None,out=False,
        file=None,document=None,photo=None,
        reply_to=SimpleNamespace(reply_to_msg_id=10,reply_to_peer_id=PeerChat(456)))
    assert telegram_message('tg-owner',message)['metadata']['reply_to_provider_id'] is None
    message.reply_to.reply_to_peer_id=PeerChat(123)
    assert telegram_message('tg-owner',message)['metadata']['reply_to_provider_id']=='10'


def test_whatsapp_round_video_is_a_video_with_observed_duration():
    event={'messageId':'false_123@c.us_ROUND','chatId':'123@c.us','senderId':'123@c.us',
           'body':'','fromMe':False,'timestamp':1791000000,'hasMedia':True,'messageType':'ptv',
           'mediaKind':'video','durationSeconds':9,'filename':'round.bin','mimetype':'video/mp4','filesize':4096}
    e=whatsapp_message('wa-1',event)
    assert e['metadata']['message_kind']=='video' and e['metadata']['provider_message_type']=='ptv'
    assert e['metadata']['media_duration_seconds']==9
    assert e['media']==[{'provider_id':'false_123@c.us_ROUND','filename':'round.bin','mime':'video/mp4','bytes':4096}]
