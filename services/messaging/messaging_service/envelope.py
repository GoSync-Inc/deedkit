"""Validated provider-independent input. No network or session ownership here."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re
from uuid import UUID, uuid5

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


def text(value, maximum, name, empty=False):
    if not isinstance(value, str) or '\x00' in value or len(value)>maximum or (not empty and not value):
        raise ValueError('invalid_'+name)
    return value


def validate(data):
    if not isinstance(data, dict): raise ValueError('invalid_envelope')
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
        if not isinstance(metadata,dict) or set(metadata)-{'reply_to_provider_id','forwarded','media_kind','mentions_owner','reply_to_owner','sender_is_bot','chat_type','sender_identity_basis','is_service','is_self_chat','sender_is_contact',
                'message_kind','provider_message_type','timestamp_resolution','media_duration_seconds','forward_origin'}:
            raise ValueError('invalid_metadata')
        result['metadata']={}
        for key,value in metadata.items():
            if key in ('forwarded','mentions_owner','reply_to_owner','sender_is_bot','is_service','is_self_chat','sender_is_contact'):
                if value is not None and type(value) is not bool:raise ValueError('invalid_metadata')
            elif key=='forward_origin':
                # Only a forwarded message has an origin; it is never the sender's own authorship.
                if value is not None:
                    if metadata.get('forwarded') is not True:raise ValueError('invalid_metadata')
                    value=forward_origin(value)
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
                if key=='message_kind' and value not in ('text','photo','video','voice','audio','document','sticker','location','contact','poll','system','unknown'):
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
    return result


def fingerprint(data):
    # A retry/backfill observation does not itself create a new message revision.
    content={k:v for k,v in data.items() if k not in ('origin','updated_at','chat_title','sender_profile','metadata','formatting')}
    return hashlib.sha256(canonical(content)).hexdigest()
