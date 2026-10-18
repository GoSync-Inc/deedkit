"""Pure normalizers for existing provider clients; these do not open sessions.

Call the durable spool before acknowledging an adapter capture. Live event wiring,
provider catch-up and single-session handover are separate deployment gates.
"""
from datetime import datetime,timezone
import math
import os
import re

from .envelope import validate
from .incoming_formatting import telegram as telegram_formatting, unknown as unknown_formatting


def timestamp(value):
    if isinstance(value,datetime): return value.isoformat()
    if isinstance(value,(int,float)) and not isinstance(value,bool):
        return datetime.fromtimestamp(value,timezone.utc).isoformat()
    return value


MAX_MEDIA_BYTES=8*1024**3


def display_text(value,maximum):
    """Bound provider-supplied display text instead of rejecting the whole message."""
    if not isinstance(value,str):return ''
    return value.replace('\x00','\ufffd')[:maximum]


def media_mime(value):
    # Senders choose these values; parameters and malformed types must not block capture.
    if not isinstance(value,str) or re.search(r'[\x00-\x1f\x7f]',value):return 'application/octet-stream'
    mime=value.split(';',1)[0].strip().lower()
    return mime if len(mime)<=127 and re.fullmatch(r'[\w.+-]+/[\w.+-]+',mime) else 'application/octet-stream'


def media_name(value,fallback):
    name=display_text(value,4096) or fallback
    if len(name)<=255:return name
    stem,dot,suffix=name.rpartition('.')
    # Keep a short extension; it is display metadata, never a storage path.
    return stem[:254-len(suffix)]+dot+suffix if dot and stem and len(suffix)<=16 else name[:255]


def media_bytes(value):
    return value if type(value) is int and 0<=value<=MAX_MEDIA_BYTES else None


def observed_duration(value):
    """Retain only bounded numeric duration observed in cached provider metadata."""
    return value if type(value) in (int,float) and 0<=value<=86400 and math.isfinite(value) else None


def telegram_kind(message,media_kind):
    from telethon import types
    if isinstance(message,types.MessageService) or getattr(message,'action',None) is not None:
        return 'system','service'
    media=getattr(message,'media',None)
    token=type(media).__name__.removeprefix('MessageMedia').lower() if media is not None else 'message'
    token=token if re.fullmatch(r'[a-z][a-z0-9_]{0,63}',token) else 'unsupported'
    if isinstance(media,(types.MessageMediaGeo,types.MessageMediaGeoLive,types.MessageMediaVenue)):
        return 'location',token
    if isinstance(media,types.MessageMediaContact):return 'contact',token
    if isinstance(media,types.MessageMediaPoll):return 'poll',token
    if media_kind:return 'video' if media_kind=='animation' else media_kind,token
    # Unknown constructors remain unknown even if a body happens to be present.
    if media is not None and not isinstance(media,(types.MessageMediaEmpty,types.MessageMediaWebPage)):
        return 'unknown',token
    return ('text' if getattr(message,'message',None) else 'unknown'),token


def forward_origin_capture():
    # Off until the release a rollback would return to accepts forward_origin (SEARCH.md).
    return os.environ.get('ANYMSG_CAPTURE_FORWARD_ORIGIN')=='1'


def telegram_forward_origin(message):
    """Origin stated by fwd_from and the entities Telethon cached from this update; never a lookup."""
    from telethon import types,utils
    header=getattr(message,'fwd_from',None)
    if not isinstance(header,types.MessageFwdHeader):return None
    # Message.forward is built from the update's entities; its get_sender()/get_chat() may call the network.
    cached=getattr(message,'forward',None)
    peer=header.from_id
    origin={'kind':'unknown','basis':'telegram_fwd_from','sent_at':timestamp(header.date) if header.date else None,
        'provider_message_id':str(header.channel_post) if header.channel_post else None,
        'post_author':display_text(header.post_author,256) or None}
    if isinstance(peer,types.PeerUser):
        entity=getattr(cached,'sender',None)
        name=' '.join(x for x in (entity.first_name,entity.last_name) if isinstance(x,str)) if isinstance(entity,types.User) else ''
        origin.update(kind='user',sender_id=str(peer.user_id),sender_name=display_text(name or header.from_name,256) or None)
    elif isinstance(peer,(types.PeerChat,types.PeerChannel)):
        entity=getattr(cached,'chat',None)
        group=isinstance(peer,types.PeerChat) or getattr(entity,'megagroup',False) is True
        title=getattr(entity,'title','') if isinstance(entity,(types.Chat,types.ChatForbidden,types.Channel,types.ChannelForbidden)) else ''
        origin.update(kind='chat' if group else 'channel',chat_id=str(utils.get_peer_id(peer)),sender_name=display_text(title,256) or None)
    elif header.from_name:
        # The original sender hid the link to their account: only the name is stated.
        origin.update(kind='hidden_user',sender_name=display_text(header.from_name,256) or None)
    return origin


def telegram_message(account,message,*,chat_title='',origin='live',verified_self=None):
    """Telethon Message-shaped object. No entity/network lookups during capture."""
    media=[]
    file=getattr(message,'file',None)
    document=getattr(message,'document',None); photo=getattr(message,'photo',None)
    provider_media=document or photo
    if document is not None and photo is not None:
        # WebPage can expose both. Message.file prefers its photo, while this
        # archive selects the document; derive size/type/name from that object.
        from telethon.tl.custom.file import File
        file=File(provider_media)
    if file and provider_media:
        media.append({'provider_id':str(provider_media.id),
                      'filename':media_name(file.name,f'{message.id}{file.ext or ".bin"}'),
                      'mime':media_mime(file.mime_type),'bytes':media_bytes(file.size)})
    entity=getattr(message,'sender',None)  # Telethon's cached entity; never get_sender()/network here.
    # NewMessage can belong to a chat absent from the last dialog discovery.
    # Use only the entity already attached to this message, and verify its peer.
    from telethon import types,utils
    sender_id=message.sender_id
    identity_basis=None
    # UpdateShortSentMessage has no from_id. The provider supplies only the
    # self entity it has already authenticated against the configured owner ID.
    # A missing group/channel sender may be anonymous: never infer it there.
    if isinstance(verified_self,types.User) and verified_self.is_self:
        if not sender_id and message.out and isinstance(getattr(message,'peer_id',None),types.PeerUser):
            sender_id=verified_self.id
            identity_basis='authenticated_self'
        if sender_id==verified_self.id and entity is None:
            entity=verified_self
    chat_entity=getattr(message,'chat',None)
    observed_type='private' if isinstance(getattr(message,'peer_id',None),types.PeerUser) else None
    observed_title=''
    if isinstance(chat_entity,(types.User,types.Chat,types.ChatForbidden,types.Channel,types.ChannelForbidden)) and utils.get_peer_id(chat_entity)==message.chat_id:
        if isinstance(chat_entity,types.User):
            observed_type='private'
            observed_title=' '.join(x for x in (chat_entity.first_name,chat_entity.last_name) if isinstance(x,str))
        else:
            observed_title=chat_entity.title or ''
            if isinstance(chat_entity,(types.Chat,types.ChatForbidden)):observed_type='group'
            elif getattr(chat_entity,'megagroup',False):observed_type='group'
            elif getattr(chat_entity,'broadcast',False):observed_type='channel'
    name=' '.join(x for x in [getattr(entity,'first_name',None),getattr(entity,'last_name',None)] if isinstance(x,str))
    profile={'display_name':(name or getattr(entity,'title','') or '')[:512],
             'username':(getattr(entity,'username','') or '')[:128]}
    reply=getattr(message,'reply_to',None)
    reply_id=str(getattr(reply,'reply_to_msg_id','') or '') or None
    # Replies may explicitly point into a different Telegram peer namespace.
    # Never resolve that ID against a coincidentally numbered local message.
    reply_peer=getattr(reply,'reply_to_peer_id',None)
    if reply_peer is not None:
        from telethon.utils import get_peer_id
        if get_peer_id(reply_peer)!=message.chat_id:reply_id=None
    media_kind=next((name for attr,name in [('voice','voice'),('video_note','video'),('sticker','sticker'),
        ('gif','animation'),('video','video'),('audio','audio'),('photo','photo'),('document','document')]
        if getattr(message,attr,None)),None)
    kind,provider_type=telegram_kind(message,media_kind)
    forwarded=getattr(message,'fwd_from',None) is not None
    forward=telegram_forward_origin(message) if forwarded and forward_origin_capture() else None
    return validate({'account':account,'platform':'telegram','chat':str(message.chat_id),
        'chat_title':(chat_title if chat_title and chat_title!=str(message.chat_id) else observed_title) or str(message.chat_id),
        'provider_id':str(message.id),
        'sender':str(sender_id or ''),'body':message.message or '',
        'formatting':telegram_formatting(message.message or '',getattr(message,'entities',None)),
        'sent_at':timestamp(message.date),'updated_at':timestamp(message.edit_date or message.date),
        'deleted':False,'direction':'outgoing' if message.out else 'incoming','origin':origin,'media':media,
        'sender_profile':profile,'metadata':{
            'chat_type':observed_type,
            'is_service':kind=='system',
            'message_kind':kind,'provider_message_type':provider_type,
            'timestamp_resolution':'second',
            'media_duration_seconds':observed_duration(getattr(file,'duration',None)) if kind in ('voice','audio','video') else None,
            'is_self_chat':bool(isinstance(chat_entity,types.User) and chat_entity.is_self) or
                bool(isinstance(verified_self,types.User) and verified_self.is_self and message.chat_id==verified_self.id),
            'sender_is_contact':getattr(entity,'contact',None) if isinstance(entity,types.User) else None,
            'sender_identity_basis':identity_basis,
            'sender_is_bot':getattr(entity,'bot',None) if isinstance(entity,types.User) else None,
            'reply_to_provider_id':reply_id,
            'mentions_owner':getattr(message,'mentioned',None),
            'forwarded':forwarded,
            **({'forward_origin':forward} if forward else {}),
            'media_kind':media_kind}})


def whatsapp_message(account,event,*,origin='live',change='message',observed_at=None):
    """Existing bridge event shape; edit/delete capture must supply its saved observation time.

    The legacy RAM poll is not a durable source. Server callbacks write a spool.
    """
    if change not in ('message','edit','delete'): raise ValueError('invalid_change')
    if change!='message' and observed_at is None: raise ValueError('missing_change_time')
    media=[]
    if event.get('hasMedia') and change!='delete':
        media.append({'provider_id':str(event['messageId']),
                      'filename':media_name(event.get('filename'),str(event['messageId'])[:120]+'.bin'),
                      'mime':media_mime(event.get('mimetype')),'bytes':media_bytes(event.get('filesize'))})
    edited=event.get('editedTimestampMs')
    updated=(edited/1000 if type(edited) is int and edited>0 else None)
    if change=='delete' or (change=='edit' and updated is None): updated=observed_at
    native_type=event.get('messageType')
    native_type=native_type if isinstance(native_type,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}',native_type) else None
    service=event.get('isService') is True or native_type in ('notification','e2e_notification','call_log','gp2','protocol','ciphertext','revoked')
    kind='system' if service else {'chat':'text','image':'photo','video':'video','gif':'video','ptv':'video',
        'ptt':'voice','audio':'audio','document':'document','sticker':'sticker','location':'location',
        'vcard':'contact','multi_vcard':'contact','poll_creation':'poll'}.get(native_type,'unknown')
    if native_type is None and event.get('mediaKind'):
        kind='video' if event['mediaKind']=='animation' else event['mediaKind'] if event['mediaKind']!='other' else 'unknown'
    body=event.get('body') or ''
    formatting=event.get('formatting',unknown_formatting())
    if isinstance(body,str) and ('\x00' in body or len(body)>65536):
        # Sender-controlled text: keep a bounded copy. Its wire proof no longer applies.
        body,formatting=display_text(body,65536),unknown_formatting()
    reply=event.get('replyToId')
    reply=reply if isinstance(reply,str) and 0<len(reply)<=256 and '\x00' not in reply else None
    forward=event.get('forwardOrigin') if event.get('forwarded') is True else None
    if isinstance(forward,dict) and 'sender_name' in forward:
        forward={**forward,'sender_name':display_text(forward['sender_name'],256) or None}
    return validate({'account':account,'platform':'whatsapp','chat':event['chatId'],
        'chat_title':display_text(event.get('chatName'),512) or event['chatId'],'provider_id':event['messageId'],
        'sender':event.get('senderId') or '','body':body,
        'formatting':formatting,
        'sent_at':timestamp(event['timestamp']),'updated_at':timestamp(updated or event['timestamp']),
        'deleted':change=='delete','direction':'outgoing' if event.get('fromMe') else 'incoming',
        'origin':origin,'media':media,'sender_profile':{'display_name':display_text(event.get('senderName'),512),
                                                     'username':display_text(event.get('senderUsername'),128)},
        'metadata':{'reply_to_provider_id':reply,
                    'is_service':service,
                    'message_kind':kind,'provider_message_type':native_type,
                    'timestamp_resolution':'second',
                    'media_duration_seconds':observed_duration(event.get('durationSeconds')) if kind in ('voice','audio','video') else None,
                    'is_self_chat':event.get('isSelfChat'),
                    'sender_is_contact':event.get('senderIsContact'),
                    'mentions_owner':event.get('mentionsOwner'),
                    'reply_to_owner':event.get('replyToOwner'),
                    'forwarded':event.get('forwarded'),
                    **({'forward_origin':forward} if forward is not None else {}),
                    'media_kind':event.get('mediaKind')}})
