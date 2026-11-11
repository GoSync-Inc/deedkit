"""Validated provider-independent input. No network or session ownership here."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import re
from uuid import UUID, uuid5

log = logging.getLogger(__name__)
NAMESPACE = UUID('5d833db7-c8ae-4c82-bfe3-e0e4d498d4da')
MAX_ENVELOPE = 512 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def stable_id(kind, *parts):
    return kind + '_' + uuid5(NAMESPACE, canonical(parts).decode()).hex


def utc(value):
    if not isinstance(value, str): raise ValueError('invalid_timestamp')
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None: raise ValueError()
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError: raise ValueError('invalid_timestamp') from None


FORWARD_KINDS = ('user','chat','channel','hidden_user','status','newsletter','unknown')
FORWARD_BASES = ('telegram_fwd_from','whatsmeow_context_info','whatsapp_web_js_message')
WHATSAPP_FORWARD_ORIGINS = ('UNKNOWN','CHAT','STATUS','CHANNELS','META_AI','UGC')
FORWARD_TEXT = {'sender_id':160,'sender_name':256,'chat_id':160,'provider_message_id':256,'post_author':256}


def forward_origin(value):
    """Provider-stated origin of a forwarded message: bounded strings, closed enums, no inference."""
    if not isinstance(value,dict) or set(value)-{*FORWARD_TEXT,'kind','sent_at','forwarding_score','provider_origin_type','basis'} \
            or value.get('kind') not in FORWARD_KINDS or value.get('basis') not in FORWARD_BASES:
        raise ValueError('invalid_metadata')
    result={}
    for key,item in value.items():
        if item is None:continue
        if key in FORWARD_TEXT:
            if not isinstance(item,str) or not item or '\x00' in item or len(item)>FORWARD_TEXT[key]:raise ValueError('invalid_metadata')
        elif key=='sent_at':
            # Provider time: NUL never reaches the parser and an out-of-range offset is invalid, not a crash.
            if not isinstance(item,str) or '\x00' in item:raise ValueError('invalid_metadata')
            try:item=utc(item)
            except (ValueError,OverflowError):raise ValueError('invalid_metadata') from None
        elif key=='forwarding_score':
            if type(item) is not int or not 0<=item<2**32:raise ValueError('invalid_metadata')
        elif key=='provider_origin_type' and item not in WHATSAPP_FORWARD_ORIGINS:raise ValueError('invalid_metadata')
        result[key]=item
    return result


CONTACT_STATUSES = ('parsed','truncated','parse_failed','unavailable')
CONTACT_TEXT = re.compile(r'[^\x00-\x1f\x7f]{1,256}')
CONTACT_ID = re.compile(r'[0-9]{1,32}')
E164 = re.compile(r'\+[1-9][0-9]{7,14}')


def contact_block(value):
    """Shared contact cards (ANYM-529): bounded display text and phones as the provider stated them."""
    def bounded(item,pattern=CONTACT_TEXT):
        if not isinstance(item,str) or not pattern.fullmatch(item):raise ValueError('invalid_metadata')
        return item
    if not isinstance(value,dict) or set(value)!={'status','contacts'} or value['status'] not in CONTACT_STATUSES \
            or not isinstance(value['contacts'],list) or len(value['contacts'])>20:
        raise ValueError('invalid_metadata')
    contacts=[]
    for contact in value['contacts']:
        if not isinstance(contact,dict) or set(contact)-{'name','vcard_name','telegram_user_id','phones'} \
                or not isinstance(contact.get('phones'),list) or len(contact['phones'])>10:
            raise ValueError('invalid_metadata')
        name=contact.get('name')
        item={'name':'' if name=='' else bounded(name),'phones':[]}
        if 'vcard_name' in contact:item['vcard_name']=bounded(contact['vcard_name'])
        if 'telegram_user_id' in contact:item['telegram_user_id']=bounded(contact['telegram_user_id'],CONTACT_ID)
        for phone in contact['phones']:
            if not isinstance(phone,dict) or set(phone)-{'number','normalized','type','whatsapp_id'} \
                    or type(phone.get('normalized')) is not bool:
                raise ValueError('invalid_metadata')
            number=bounded(phone.get('number'))
            # normalized means E.164 as the sender wrote it, never a guessed country.
            if phone['normalized'] and not E164.fullmatch(number):raise ValueError('invalid_metadata')
            entry={'number':number,'normalized':phone['normalized']}
            if 'type' in phone:entry['type']=bounded(phone['type'])
            if 'whatsapp_id' in phone:entry['whatsapp_id']=bounded(phone['whatsapp_id'],CONTACT_ID)
            item['phones'].append(entry)
        if not item['name'] and not item['phones']:raise ValueError('invalid_metadata')
        contacts.append(item)
    if value['status']=='unavailable' and contacts:raise ValueError('invalid_metadata')
    result={'status':value['status'],'contacts':contacts}
    if len(canonical(result))>16*1024:raise ValueError('invalid_metadata')
    return result


LOCATION_KEYS=frozenset(('latitude','longitude','accuracy_m','title','address','live'))


def location_block(value):
    """A shared point or venue (ANYM-922): coordinates, accuracy, title and address as the sender stated them.
    A live location is only the fact that it was shared, {live: true}: never a point of its track."""
    def coordinate(item,limit):
        # Type and range before any float conversion: a huge integer is out of range, never an OverflowError; NaN fails
        # every comparison (Astra r1 M1).
        if type(item) not in (int,float) or not -limit<=item<=limit:raise ValueError('invalid_location')
        return round(float(item),6)
    def label(item):
        if item is not None and (not isinstance(item,str) or '\x00' in item or len(item)>256):raise ValueError('invalid_location')
        return item or None
    if not isinstance(value,dict) or set(value)-LOCATION_KEYS or type(value.get('live')) is not bool:raise ValueError('invalid_location')
    if value['live']:
        if set(value)!={'live'}:raise ValueError('invalid_location')
        return {'live':True}
    accuracy=value.get('accuracy_m')
    if accuracy is not None and (type(accuracy) is not int or not 0<=accuracy<2**32):raise ValueError('invalid_location')
    return {'latitude':coordinate(value.get('latitude'),90),'longitude':coordinate(value.get('longitude'),180),
        'accuracy_m':accuracy,'title':label(value.get('title')),'address':label(value.get('address')),'live':False}


GROUP_CHANGES=('rename','topic','join','leave','add','remove','promote','demote','joined_group')
MAX_GROUP_CHANGE_PARTICIPANTS=2048


def group_change(value):
    """{change, actor, participants, value}: stored participant IDs; value is the new name or topic text."""
    if not isinstance(value,dict) or set(value)!={'change','actor','participants','value'} or value['change'] not in GROUP_CHANGES:
        raise ValueError('invalid_metadata')
    member=lambda i: isinstance(i,str) and 0<len(i)<=160 and '\x00' not in i
    if value['actor'] is not None and not member(value['actor']):raise ValueError('invalid_metadata')
    participants=value['participants']
    if (not isinstance(participants,list) or len(participants)>MAX_GROUP_CHANGE_PARTICIPANTS
            or not all(member(i) for i in participants) or len(set(participants))!=len(participants)):
        raise ValueError('invalid_metadata')
    named=value['value']
    if named is not None and (not isinstance(named,str) or '\x00' in named or len(named)>4096):raise ValueError('invalid_metadata')
    return {'change':value['change'],'actor':value['actor'],'participants':list(participants),'value':named}


CALL_OUTCOMES=('missed','rejected','answered','unknown')
CALL_FIELDS={'call_id','direction','media','group','started_at','ended_at','outcome'}


def call_block(value):
    """{call_id, direction, media, group, started_at, ended_at, outcome} of an incoming WhatsApp call (ANYM-923)."""
    if (not isinstance(value,dict) or set(value)!=CALL_FIELDS or not isinstance(value['call_id'],str)
            or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',value['call_id']) or value['direction']!='incoming'
            or value['media'] not in (None,'audio','video') or type(value['group']) is not bool
            or value['outcome'] not in CALL_OUTCOMES):
        raise ValueError('invalid_metadata')
    try:
        started=utc(value['started_at'])
        ended=None if value['ended_at'] is None else utc(value['ended_at'])
    except ValueError: raise ValueError('invalid_metadata') from None
    if ended is not None and datetime.fromisoformat(ended)<datetime.fromisoformat(started):raise ValueError('invalid_metadata')
    return {**value,'started_at':started,'ended_at':ended}


def text(value, maximum, name, empty=False):
    if not isinstance(value, str) or '\x00' in value or len(value)>maximum or (not empty and not value):
        raise ValueError('invalid_'+name)
    return value


def checked_location(data):
    """The shared place is validated before the envelope is encoded or measured (ANYM-922): a block the archive cannot
    accept (out of range, NaN, a huge number, an oversized title, not a location message) is dropped and only the reason
    code is logged, never the coordinates; the message itself is still stored (Astra r1 M1, M2)."""
    metadata=data.get('metadata')
    if not isinstance(metadata,dict) or metadata.get('location') is None:return data
    try:
        if metadata.get('message_kind')!='location':raise ValueError('invalid_location')
        location=location_block(metadata['location'])
    except (ValueError,TypeError,OverflowError):
        log.warning('location_dropped reason=invalid_location')
        return {**data,'metadata':{k:v for k,v in metadata.items() if k!='location'}}
    return {**data,'metadata':{**metadata,'location':location}}


def validate(data):
    if not isinstance(data, dict): raise ValueError('invalid_envelope')
    data=checked_location(data)
    from .event_invites import checked_event
    data=checked_event(data)
    if len(canonical(data))>MAX_ENVELOPE: raise ValueError('envelope_too_large')
    allowed={'account','platform','chat','chat_title','provider_id','sender','body','sent_at',
             'updated_at','deleted','direction','origin','media','sender_profile','metadata','formatting'}
    if set(data)-allowed: raise ValueError('unknown_envelope_fields')
    result={k:text(data.get(k),n,k,empty=k in ('body','sender','chat_title')) for k,n in
            [('account',64),('chat',160),('chat_title',512),('provider_id',256),('sender',160),('body',65536)]}
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+',result['account']): raise ValueError('invalid_account')
    for key, choices in [('platform',('telegram','whatsapp')),('direction',('incoming','outgoing')),
                         ('origin',('live','backfill','reconcile','fixture'))]:
        if data.get(key) not in choices: raise ValueError('invalid_'+key)
        result[key]=data[key]
    if type(data.get('deleted')) is not bool: raise ValueError('invalid_deleted')
    result['deleted']=data['deleted']
    result['sent_at']=utc(data.get('sent_at')); result['updated_at']=utc(data.get('updated_at'))
    if result['updated_at']<result['sent_at']: raise ValueError('invalid_update_time')
    media=data.get('media',[])
    if not isinstance(media,list) or len(media)>32: raise ValueError('invalid_media')
    result['media']=[]
    if 'sender_profile' in data:
        profile=data['sender_profile']
        if not isinstance(profile,dict) or set(profile)-{'display_name','username'}:
            raise ValueError('invalid_sender_profile')
        result['sender_profile']={key:text(profile.get(key,''),maximum,key,empty=True)
                                  for key,maximum in [('display_name',512),('username',128)]}
        result['sender_profile']['username']=result['sender_profile']['username'].lstrip('@')
    if 'metadata' in data:
        metadata=data['metadata']
        if not isinstance(metadata,dict) or set(metadata)-{'reply_to_provider_id','forwarded','media_kind','mentions_owner','reply_to_owner','sender_is_bot','chat_type','sender_identity_basis','is_service','is_self_chat','sender_is_contact','sender_is_business_or_verified',
                'message_kind','provider_message_type','timestamp_resolution','media_duration_seconds','forward_origin','contact',
                'web_preview_id','buttons','group_change','call','poll','poll_results','location','event','event_responses'}:
            raise ValueError('invalid_metadata')
        result['metadata']={}
        for key,value in metadata.items():
            if key in ('forwarded','mentions_owner','reply_to_owner','sender_is_bot','is_service','is_self_chat','sender_is_contact','sender_is_business_or_verified'):
                if value is not None and type(value) is not bool:raise ValueError('invalid_metadata')
            elif key=='forward_origin':
                # Only a forwarded message has an origin; it is never the sender's own authorship.
                if value is not None:
                    if metadata.get('forwarded') is not True:raise ValueError('invalid_metadata')
                    value=forward_origin(value)
            elif key=='contact':
                # Only a contact message carries a card; "unavailable" is stored as no block.
                if value is None:continue
                if metadata.get('message_kind')!='contact':raise ValueError('invalid_metadata')
                value=contact_block(value)
                if value['status']=='unavailable':continue
            elif key=='buttons':
                # An inline keyboard is Telegram evidence: labels, kinds and links, never a press token (ANYM-658).
                if value is None:continue
                if result['platform']!='telegram':raise ValueError('invalid_metadata')
                from .message_buttons import validate_buttons
                value=validate_buttons(value)
            elif key=='group_change':
                # A pushed WhatsApp group change (ANYM-869); names and topics are member-set data.
                if metadata.get('message_kind')!='group_change' or result['platform']!='whatsapp':raise ValueError('invalid_metadata')
                value=group_change(value)
            elif key=='call':
                # An incoming WhatsApp call (ANYM-923): caller, media, times and outcome, never content.
                if metadata.get('message_kind')!='call' or result['platform']!='whatsapp':raise ValueError('invalid_metadata')
                value=call_block(value)
            elif key=='poll':
                # A poll's question and options (ANYM-921); option texts are member-set data.
                if metadata.get('message_kind')!='poll':raise ValueError('invalid_metadata')
                from .polls import poll_block
                value=poll_block(value)
            elif key=='poll_results':
                # A captured Telegram poll's counts (ANYM-920): transient, applied by the archive, never stored here.
                if result['platform']!='telegram' or not isinstance(metadata.get('poll'),dict):raise ValueError('invalid_metadata')
                from .polls import telegram_results
                value=telegram_results(value,metadata['poll'])
            elif key=='location':
                # A place someone shared (ANYM-922). A block the archive cannot accept never costs the message:
                # it was dropped by checked_location() before the envelope was measured.
                if value is None:continue
            elif key in ('event','event_responses'):
                # A WhatsApp event invitation and its counts (ANYM-924), checked by checked_event() before measuring.
                if value is None:continue
            elif key=='web_preview_id':
                # Telegram's link preview page ID in its unsigned 64-bit decimal form (ANYM-611).
                if value is not None and (type(value) is not str or not re.fullmatch(r'[0-9]{1,20}',value)):
                    raise ValueError('invalid_metadata')
            elif key=='media_duration_seconds':
                if value is not None and (type(value) not in (int,float) or not 0<=value<=86400 or not math.isfinite(value)):
                    raise ValueError('invalid_metadata')
            elif value is not None:
                text(value,256 if key=='reply_to_provider_id' else 64 if key=='provider_message_type' else 32,key)
                if key=='media_kind' and value not in ('photo','video','audio','voice','document','sticker','animation','other'):
                    raise ValueError('invalid_metadata')
                if key=='chat_type' and value not in ('private','group','channel','unknown'):
                    raise ValueError('invalid_metadata')
                if key=='sender_identity_basis' and value!='authenticated_self':
                    raise ValueError('invalid_metadata')
                if key=='message_kind' and value not in ('text','photo','video','voice','audio','document','sticker','location','contact','poll','system','group_change','call','event','unknown'):
                    raise ValueError('invalid_metadata')
                if key=='timestamp_resolution' and value not in ('second','unknown'):
                    raise ValueError('invalid_metadata')
                if key=='provider_message_type' and not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',value):
                    raise ValueError('invalid_metadata')
            result['metadata'][key]=value
    seen=set()
    for item in media:
        if not isinstance(item,dict) or set(item)-{'provider_id','filename','mime','bytes'}:
            raise ValueError('invalid_media')
        mid=text(item.get('provider_id'),256,'media_id')
        if mid in seen: raise ValueError('duplicate_media_id')
        seen.add(mid)
        size=item.get('bytes')
        if size is not None and (type(size) is not int or not 0<=size<=8*1024**3):
            raise ValueError('invalid_media_size')
        # Display-only names never become filesystem paths or response header injection.
        name=text(item.get('filename'),255,'filename')
        name=re.sub(r'[\x00-\x1f\x7f/\\]', '_',name)
        mime=text(item.get('mime'),127,'mime')
        if not re.fullmatch(r'[\w.+-]+/[\w.+-]+',mime): raise ValueError('invalid_mime')
        result['media'].append({'provider_id':mid,'filename':name,'mime':mime,'bytes':size})
    if 'formatting' in data:
        from .incoming_formatting import validate as validate_formatting, unknown as unknown_formatting
        # Deleted captures can carry the retained revision's old wire proof.
        # It no longer describes current content and must be cleared before
        # validating offsets/hash against a deliberately empty tombstone body.
        result['formatting']=unknown_formatting() if result['deleted'] else \
            validate_formatting(data['formatting'],result['body'],result['platform'])
    if result['deleted']:
        result['body']=''; result['media']=[]
        # A deleted message keeps no shared names or phone numbers.
        result.get('metadata',{}).pop('contact',None)
        # Nor a keyboard: its labels and links are message content (ANYM-658).
        result.get('metadata',{}).pop('buttons',None)
        # Nor a shared place (ANYM-922), nor an event invitation or its counts (ANYM-924).
        result.get('metadata',{}).pop('location',None)
        result.get('metadata',{}).pop('event',None);result.get('metadata',{}).pop('event_responses',None)
    return result


def fingerprint(data):
    # A retry/backfill observation does not itself create a new message revision.
    content={k:v for k,v in data.items() if k not in ('origin','updated_at','chat_title','sender_profile','metadata','formatting')}
    return hashlib.sha256(canonical(content)).hexdigest()
