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
    # ANYM-620: Telegram itself named the verified owner, so the basis is authenticated.
    assert e['metadata'].get('sender_identity_basis')=='authenticated_self'


# spec: ANYM-620
@pytest.mark.parametrize('peer,sender,verified,basis',[
    ('chat',('user',42),'self',True),('channel',('user',42),'self',True),('user',('user',42),'self',True),
    ('chat',('user',99),'self',False),('channel',('channel',100),'self',False),('chat',None,'self',False),
    ('chat',('user',42),'not_self',False),('chat',('user',42),None,False)],
    ids=['group','supergroup','private','other_user','anonymous_admin','missing_sender','unverified','no_self'])
def test_authenticated_self_basis_only_when_telegram_names_the_owner(peer,sender,verified,basis):
    from telethon import types
    selves={'self':types.User(id=42,is_self=True,first_name='Synthetic'),
        'not_self':types.User(id=42,is_self=False,first_name='Synthetic'),None:None}
    peers={'user':types.PeerUser,'chat':types.PeerChat,'channel':types.PeerChannel}
    message=types.Message(id=19,peer_id=peers[peer](100),out=True,message='fixture',
        from_id=peers[sender[0]](sender[1]) if sender else None,date=datetime(2026,10,1,tzinfo=timezone.utc))
    expected=str(message.sender_id or '')
    e=telegram_message('tg-owner',message,verified_self=selves[verified])
    # A missing group or channel sender stays empty: it may be anonymous.
    assert e['sender']==expected
    assert (e['metadata'].get('sender_identity_basis')=='authenticated_self')==basis


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


def preview_message(media):
    from telethon import types
    return types.Message(id=10,peer_id=types.PeerUser(42),from_id=types.PeerUser(42),
        date=datetime(2026,10,13,tzinfo=timezone.utc),message='preview',media=media)


# spec: ANYM-611
@pytest.mark.parametrize('page_id,expected',[(20,'20'),(-1,'18446744073709551615'),(2**63-1,'9223372036854775807')],
    ids=['positive','negative','largest'])
def test_telegram_web_preview_id_is_captured_only_with_the_flag(monkeypatch,page_id,expected):
    from telethon import types
    now=datetime(2026,10,13,tzinfo=timezone.utc)
    pages=[types.WebPage(id=page_id,url='https://example.invalid',display_url='example.invalid',hash=0),
        types.WebPagePending(id=page_id,date=now),types.WebPageEmpty(id=page_id)]
    for page in pages:
        message=preview_message(types.MessageMediaWebPage(webpage=page))
        monkeypatch.delenv('ANYMSG_CAPTURE_WEB_PREVIEW_ID',raising=False)
        assert 'web_preview_id' not in telegram_message('tg-owner',message)['metadata']
        monkeypatch.setenv('ANYMSG_CAPTURE_WEB_PREVIEW_ID','1')
        # A signed 64-bit page ID is kept in its unsigned decimal form: one ID, one string.
        metadata=telegram_message('tg-owner',message)['metadata']
        assert metadata['web_preview_id']==expected and metadata['provider_message_type']=='webpage'


# spec: ANYM-611
def test_telegram_web_preview_id_is_omitted_without_a_page(monkeypatch):
    from telethon import types
    monkeypatch.setenv('ANYMSG_CAPTURE_WEB_PREVIEW_ID','1')
    photo=types.Photo(id=300,access_hash=0,file_reference=b'ref',date=datetime(2026,10,13,tzinfo=timezone.utc),
        sizes=[types.PhotoSize(type='x',w=2,h=2,size=4)],dc_id=1)
    for media in (types.MessageMediaWebPage(webpage=types.WebPageNotModified()),types.MessageMediaPhoto(photo=photo),None):
        assert 'web_preview_id' not in telegram_message('tg-owner',preview_message(media))['metadata']


# spec: ANYM-611
@pytest.mark.parametrize('value',['0','20','18446744073709551615','0'*20,None])
def test_envelope_accepts_a_decimal_web_preview_id(value):
    from messaging_service.envelope import validate
    e=validate({'account':'tg-owner','platform':'telegram','chat':'42','chat_title':'Fixture','provider_id':'10',
        'sender':'42','body':'preview','sent_at':'2026-10-13T00:00:00Z','updated_at':'2026-10-13T00:00:00Z',
        'deleted':False,'direction':'incoming','origin':'fixture','metadata':{'web_preview_id':value}})
    assert e['metadata']['web_preview_id']==value


# spec: ANYM-611
@pytest.mark.parametrize('value',['','-1','1'*21,'１２','12a',' 12','12\n',12,1.5,True,['12'],{'id':'12'}])
def test_envelope_rejects_any_other_web_preview_id(value):
    from messaging_service.envelope import validate
    with pytest.raises(ValueError,match='^invalid_metadata$'):
        validate({'account':'tg-owner','platform':'telegram','chat':'42','chat_title':'Fixture','provider_id':'10',
            'sender':'42','body':'preview','sent_at':'2026-10-13T00:00:00Z','updated_at':'2026-10-13T00:00:00Z',
            'deleted':False,'direction':'incoming','origin':'fixture','metadata':{'web_preview_id':value}})


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


class NoRequestClient:
    """Synthetic Telethon client: any request or lookup fails the test."""
    _self_id=1

    def __init__(self):
        from telethon._updates import EntityCache
        self._mb_entity_cache=EntityCache()

    def __call__(self,*args,**kwargs):raise AssertionError('telegram_request')

    def __getattr__(self,name):raise AssertionError('telegram_lookup:'+name)


def forwarded_message(header,entities=()):
    from telethon import types,utils
    message=types.Message(id=19,peer_id=types.PeerUser(100),message='forwarded words',fwd_from=header,
        from_id=types.PeerUser(100),date=datetime(2026,10,2,tzinfo=timezone.utc))
    message._finish_init(NoRequestClient(),{utils.get_peer_id(e):e for e in entities},None)
    return message


@pytest.fixture()
def forward_capture(monkeypatch):
    monkeypatch.setenv('ANYMSG_CAPTURE_FORWARD_ORIGIN','1')


def test_telegram_forward_from_user_keeps_original_sender_and_date(forward_capture):
    from telethon import types
    original=datetime(2026,9,1,8,30,tzinfo=timezone.utc)
    header=types.MessageFwdHeader(date=original,from_id=types.PeerUser(555),from_name=None)
    e=telegram_message('tg-owner',forwarded_message(header,[types.User(id=555,first_name='Synthetic',last_name='Author')]))
    assert e['sender']=='100' and e['metadata']['forwarded'] is True
    assert e['metadata']['forward_origin']=={'kind':'user','sender_id':'555','sender_name':'Synthetic Author',
        'sent_at':original.isoformat(),'basis':'telegram_fwd_from'}


def test_telegram_hidden_and_channel_forward_origin_invent_nothing(forward_capture):
    from telethon import types
    hidden=types.MessageFwdHeader(date=datetime(2026,9,1,tzinfo=timezone.utc),from_id=None,from_name='X')
    origin=telegram_message('tg-owner',forwarded_message(hidden))['metadata']['forward_origin']
    assert origin=={'kind':'hidden_user','sender_name':'X','sent_at':'2026-09-01T00:00:00+00:00','basis':'telegram_fwd_from'}
    channel=types.MessageFwdHeader(date=datetime(2026,9,1,tzinfo=timezone.utc),from_id=types.PeerChannel(777),
        channel_post=41,post_author='Synthetic Editor')
    entity=types.Channel(id=777,title='Synthetic News',photo=types.ChatPhotoEmpty(),date=None,broadcast=True)
    origin=telegram_message('tg-owner',forwarded_message(channel,[entity]))['metadata']['forward_origin']
    assert origin=={'kind':'channel','chat_id':'-1000000000777','sender_name':'Synthetic News','provider_message_id':'41',
        'post_author':'Synthetic Editor','sent_at':'2026-09-01T00:00:00+00:00','basis':'telegram_fwd_from'}
    # Without a cached entity the channel is still named by its ID only.
    origin=telegram_message('tg-owner',forwarded_message(channel))['metadata']['forward_origin']
    assert origin['kind']=='channel' and origin['chat_id']=='-1000000000777' and 'sender_name' not in origin


def test_telegram_forward_origin_makes_no_provider_request(forward_capture):
    from telethon import types
    header=types.MessageFwdHeader(date=datetime(2026,9,1,tzinfo=timezone.utc),from_id=types.PeerUser(555))
    message=forwarded_message(header)
    # The entity is not cached: the origin keeps the ID and never asks Telegram for the name.
    origin=telegram_message('tg-owner',message)['metadata']['forward_origin']
    assert origin['kind']=='user' and origin['sender_id']=='555' and 'sender_name' not in origin
    with pytest.raises(AssertionError,match='telegram_request'):message._client(object())


def test_forward_origin_capture_is_off_until_enabled(monkeypatch):
    from telethon import types
    monkeypatch.delenv('ANYMSG_CAPTURE_FORWARD_ORIGIN',raising=False)
    header=types.MessageFwdHeader(date=datetime(2026,9,1,tzinfo=timezone.utc),from_id=types.PeerUser(555))
    e=telegram_message('tg-owner',forwarded_message(header))
    assert e['metadata']['forwarded'] is True and 'forward_origin' not in e['metadata']
    event={'chatId':'1@c.us','messageId':'F','senderId':'1@c.us','body':'x','timestamp':1800000000,'forwarded':True,
        'forwardOrigin':{'kind':'unknown','forwarding_score':4,'basis':'whatsapp_web_js_message'}}
    # The server side always accepts an origin a collector captured.
    assert whatsapp_message('wa-owner',event)['metadata']['forward_origin']=={'kind':'unknown','forwarding_score':4,
        'basis':'whatsapp_web_js_message'}
    assert 'forward_origin' not in whatsapp_message('wa-owner',{**event,'forwarded':False})['metadata']


@pytest.mark.parametrize('origin',[
    {'kind':'user','basis':'telegram_fwd_from','sender_avatar':'x'},
    {'kind':'not_captured','basis':'telegram_fwd_from'},
    {'kind':'user','basis':'guess'},
    {'kind':'unknown','basis':'whatsmeow_context_info','provider_origin_type':'FRIEND'},
    {'kind':'user','basis':'telegram_fwd_from','sender_name':'x'*257},
    {'kind':'channel','basis':'telegram_fwd_from','chat_id':'1'*161},
    {'kind':'unknown','basis':'whatsmeow_context_info','forwarding_score':-1},
    {'kind':'unknown','basis':'whatsmeow_context_info','forwarding_score':True},
    {'kind':'user','basis':'telegram_fwd_from','sent_at':'yesterday'},
    'user'])
def test_envelope_rejects_invalid_forward_origin(origin):
    from messaging_service.envelope import validate
    from test_archive import event
    with pytest.raises(ValueError,match='^invalid_metadata$'):
        validate(event(metadata={'forwarded':True,'forward_origin':origin}))
    valid={'kind':'user','sender_id':'555','basis':'telegram_fwd_from'}
    assert validate(event(metadata={'forwarded':True,'forward_origin':valid}))['metadata']['forward_origin']==valid
    # An origin without the forwarded flag would present the sender as someone else.
    with pytest.raises(ValueError,match='^invalid_metadata$'):
        validate(event(metadata={'forwarded':False,'forward_origin':valid}))


@pytest.mark.parametrize('sent_at',['2026-09-01\x0012:00:00+00:00','0001-01-01T00:00:00+01:00',20260901])
def test_envelope_rejects_unusable_forward_origin_time(sent_at):
    from messaging_service.envelope import validate
    from test_archive import event
    with pytest.raises(ValueError,match='^invalid_metadata$'):
        validate(event(metadata={'forwarded':True,'forward_origin':{'kind':'user','basis':'telegram_fwd_from','sent_at':sent_at}}))
