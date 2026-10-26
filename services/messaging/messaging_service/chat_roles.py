"""The account's own role in a Telegram channel or group (ANYM-533), read from an entity already loaded.

Telegram's own "Channels I manage" filter uses the same fields: Channel.creator and admin_rights. Nothing here
makes a request; a missing or unreadable entity reads `unknown` and never raises.
"""
import re

# chats.my_role, chats.can_post, chats.role_observed_at and chats.username (migration 046).
CHAT_ROLE_SCHEMA=46
ROLES=('creator','admin','member','left','unknown')
USERNAME=re.compile(r'[A-Za-z0-9_]{1,64}')


def flag(value):
    # A TL flag the server left unset reads None on a constructed object and False on a parsed one.
    if value is None or type(value) is bool:return bool(value)
    raise ValueError('malformed_flag')


def _derive(entity,chat_type):
    if entity is None or chat_type not in ('group','channel'):return 'unknown',None
    if type(entity).__name__ in ('ChatForbidden','ChannelForbidden'):return 'left',None
    left,creator=getattr(entity,'left',None),getattr(entity,'creator',None)
    if type(left) is not bool or type(creator) is not bool:return 'unknown',None
    if left:return 'left',None
    admin=getattr(entity,'admin_rights',None)
    role='creator' if creator else 'admin' if admin is not None else 'member'
    if chat_type=='channel':
        # A broadcast channel: only the creator and an admin with post_messages write to it.
        return role,True if role=='creator' else flag(getattr(admin,'post_messages',None)) if role=='admin' else False
    if role!='member':return role,True
    for name in ('banned_rights','default_banned_rights'):
        rights=getattr(entity,name,None)
        if rights is not None and flag(getattr(rights,'send_messages',None)):return role,False
    return role,True


def derive(entity,chat_type):
    """(my_role, can_post): can_post is None for left, unknown or unreadable rights."""
    try:return _derive(entity,chat_type)
    except Exception:return 'unknown',None


def username(entity):
    try:
        names=[getattr(entity,'username',None),*[getattr(u,'username',None) for u in (getattr(entity,'usernames',None) or [])
            if getattr(u,'active',False) is True]]
        return next((n for n in names if isinstance(n,str) and USERNAME.fullmatch(n)),None)
    except Exception:return None


def observation(entity,chat_type):
    role,can_post=derive(entity,chat_type)
    return {'my_role':role,'can_post':can_post,'username':username(entity)}


def valid(value):
    # A malformed observation is stored as unknown; it never aborts the catalog batch.
    if (isinstance(value,dict) and set(value)=={'my_role','can_post','username'} and value['my_role'] in ROLES and
            (value['can_post'] is None or type(value['can_post']) is bool) and
            (value['username'] is None or isinstance(value['username'],str) and USERNAME.fullmatch(value['username'])) and
            (value['can_post'] is None or value['my_role'] not in ('left','unknown'))):
        return value
    return {'my_role':'unknown','can_post':None,'username':None}
