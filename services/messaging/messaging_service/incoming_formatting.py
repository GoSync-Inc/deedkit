"""Bounded cached formatting evidence, separate from the outgoing renderer.

Offsets refer to the complete provider body in UTF-16 code units. No provider,
contact, quoted-message or media fetches occur here; incomplete observations are
explicit and never become an invented empty native entity list.
"""
from copy import deepcopy
import hashlib
import re
import unicodedata
from urllib.parse import urlsplit

MAX_ENTITIES = 256
_FIELDS = {'version','state','basis','wire_sha256','entities','unsupported_entities','warnings'}
_TYPES = {'bold','italic','underline','strikethrough','spoiler','code','pre','blockquote','expandable_blockquote',
          'text_link','url','email','phone','mention','hashtag','cashtag','bot_command'}
_UNSUPPORTED = {'mention_name','input_mention_name','custom_emoji','unknown','text_link','pre'}
_WARNINGS = {'native_entities_not_captured','native_entities_invalid','native_entities_limit',
             'native_entities_unsupported','native_link_unsupported','native_language_unsupported',
             'syntax_interpretation_unknown'}


def unknown():
    return {'version':1,'state':'unknown','basis':None,'wire_sha256':None,'entities':None,
            'unsupported_entities':[],'warnings':['native_entities_not_captured']}


def wire_digest(body):
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def boundaries(body):
    result={0};length=0
    for char in body:
        length+=2 if ord(char)>0xffff else 1
        result.add(length)
    return result


_TG_HOSTS=('resolve','join','addstickers')
_CREDENTIAL_MIRRORS=('t.me','telegram.me','telegram.dog','telesco.pe')


def safe_url(value):
    """What the validator accepts: http(s) without userinfo, or a tg:// link to one of three harmless hosts."""
    if not isinstance(value,str) or not value or len(value)>2048 or any(c.isspace() or unicodedata.category(c) in ('Cc','Cf','Cs') for c in value):
        return False
    try:
        parsed=urlsplit(value)
        if parsed.scheme=='tg':
            # tg://resolve, join and addstickers only; login, passport, setlanguage and the rest are not links.
            return parsed.netloc.lower() in _TG_HOSTS and parsed.username is None and parsed.port is None
        return parsed.scheme in ('http','https') and bool(parsed.hostname) and parsed.username is None and parsed.password is None
    except ValueError:
        return False


def credential_link(value):
    """A login-code link in either scheme: tg://login and https://t.me/login/<code> on Telegram's own hosts."""
    try:
        parsed=urlsplit(value)
        if parsed.scheme=='tg':return parsed.netloc.lower()=='login'
        return (parsed.hostname or '').lower() in _CREDENTIAL_MIRRORS and (parsed.path.lower()=='/login' or parsed.path.lower().startswith('/login/'))
    except ValueError:
        return True


def capture_url(value):
    """The capture-time link rule for text links and buttons: safe, no credential link, and a tg:// link only
    while ANYMSG_CAPTURE_BUTTONS is on (the deployed validator rejects such spans, so a rollback must not receive them)."""
    if not safe_url(value) or credential_link(value):return False
    if urlsplit(value).scheme=='tg':
        from .message_buttons import buttons_capture
        return buttons_capture()
    return True


def _span(value, points, *, unsupported=False):
    if not isinstance(value,dict):raise ValueError('invalid_formatting')
    allowed={'type','offset','length'}
    if not unsupported and value.get('type')=='text_link':allowed.add('url')
    if not unsupported and value.get('type')=='pre':allowed.add('language')
    if set(value)-allowed or not {'type','offset','length'}<=set(value):raise ValueError('invalid_formatting')
    kind=value['type'];offset=value['offset'];length=value['length']
    if not isinstance(kind,str) or kind not in (_UNSUPPORTED if unsupported else _TYPES):raise ValueError('invalid_formatting')
    if type(offset) is not int or type(length) is not int or length<=0 or offset not in points or offset+length not in points:
        raise ValueError('invalid_formatting')
    if not unsupported and kind=='text_link' and not safe_url(value.get('url')):raise ValueError('invalid_formatting')
    if 'language' in value and (not isinstance(value['language'],str) or not re.fullmatch(r'[a-zA-Z0-9_.+-]{0,32}',value['language'])):
        raise ValueError('invalid_formatting')
    return dict(value)


def validate(value, body, platform):
    """Reject forged normalized metadata; native normalization degrades safely."""
    if not isinstance(value,dict) or set(value)!=_FIELDS or type(value['version']) is not int or value['version']!=1:
        raise ValueError('invalid_formatting')
    state=value['state'];warnings=value['warnings'];unsupported=value['unsupported_entities'];entities=value['entities']
    if not isinstance(state,str) or state not in ('unknown','observed','partial','wire_observed'):
        raise ValueError('invalid_formatting')
    if not isinstance(warnings,list) or len(warnings)>len(_WARNINGS) or any(not isinstance(w,str) or w not in _WARNINGS for w in warnings):
        raise ValueError('invalid_formatting')
    if len(set(warnings))!=len(warnings) or not isinstance(unsupported,list):raise ValueError('invalid_formatting')
    if state=='unknown':
        if value['basis'] is not None or value['wire_sha256'] is not None or entities is not None or unsupported:
            raise ValueError('invalid_formatting')
        if warnings not in ([],['native_entities_not_captured']):raise ValueError('invalid_formatting')
        return deepcopy(value)
    if value['wire_sha256']!=wire_digest(body):raise ValueError('invalid_formatting')
    if state=='wire_observed':
        if platform!='whatsapp' or value['basis']!='whatsapp_native_wire' or entities is not None or unsupported or warnings!=['syntax_interpretation_unknown']:
            raise ValueError('invalid_formatting')
        return deepcopy(value)
    if platform!='telegram' or value['basis']!='telegram_native_entities' or not isinstance(entities,list):raise ValueError('invalid_formatting')
    if len(entities)+len(unsupported)>MAX_ENTITIES:raise ValueError('invalid_formatting')
    if state=='observed' and (unsupported or warnings):raise ValueError('invalid_formatting')
    if state=='partial' and not warnings:raise ValueError('invalid_formatting')
    points=boundaries(body)
    return {**value,'entities':[_span(span,points) for span in entities],
            'unsupported_entities':[_span(span,points,unsupported=True) for span in unsupported],'warnings':list(warnings)}


def telegram(body, native_entities):
    """Capture only known Telethon entity objects and their harmless metadata."""
    if native_entities is None:return unknown()
    result={'version':1,'state':'observed','basis':'telegram_native_entities','wire_sha256':wire_digest(body),
            'entities':[],'unsupported_entities':[],'warnings':[]}
    def warn(code):
        result['state']='partial'
        if code not in result['warnings']:result['warnings'].append(code)
    if not isinstance(native_entities,(list,tuple)):
        warn('native_entities_invalid');return result
    if len(native_entities)>MAX_ENTITIES:
        warn('native_entities_limit');return result
    from telethon import types
    names={'Bold':'bold','Italic':'italic','Underline':'underline','Strike':'strikethrough','Spoiler':'spoiler',
           'Code':'code','Pre':'pre','Blockquote':'blockquote','TextUrl':'text_link','Url':'url','Email':'email',
           'Phone':'phone','Mention':'mention','Hashtag':'hashtag','Cashtag':'cashtag','BotCommand':'bot_command',
           'MentionName':'mention_name','CustomEmoji':'custom_emoji','Unknown':'unknown'}
    classes={getattr(types,'MessageEntity'+name):kind for name,kind in names.items() if hasattr(types,'MessageEntity'+name)}
    if hasattr(types,'InputMessageEntityMentionName'):classes[types.InputMessageEntityMentionName]='input_mention_name'
    points=boundaries(body)
    for entity in native_entities:
        kind=classes.get(type(entity))
        if kind is None:
            warn('native_entities_invalid');continue
        if kind=='blockquote' and getattr(entity,'collapsed',None) is True:kind='expandable_blockquote'
        span={'type':kind,'offset':entity.offset,'length':entity.length}
        try:
            _span(span,points,unsupported=kind in _UNSUPPORTED and kind not in ('pre','text_link'))
        except ValueError:
            # text_link's destination is validated below, after its offsets.
            if kind!='text_link' or not (type(entity.offset) is int and type(entity.length) is int
                and entity.length>0 and entity.offset in points and entity.offset+entity.length in points):
                warn('native_entities_invalid');continue
        if kind in ('mention_name','input_mention_name','custom_emoji','unknown'):
            result['unsupported_entities'].append(span);warn('native_entities_unsupported');continue
        if kind=='text_link':
            if not capture_url(getattr(entity,'url',None)):
                result['unsupported_entities'].append(span);warn('native_link_unsupported');continue
            span['url']=entity.url
        if kind=='pre' and getattr(entity,'language',None):
            language=entity.language
            if not isinstance(language,str) or not re.fullmatch(r'[a-zA-Z0-9_.+-]{1,32}',language):
                result['unsupported_entities'].append(span);warn('native_language_unsupported');continue
            span['language']=language
        result['entities'].append(span)
    return validate(result,body,'telegram')


def known(value):
    return value is not None and value.get('state')!='unknown'


def summary(value):
    """Keep compact output bounded; spans cannot be applied to its snippet."""
    if value.get('state')=='unknown':return {'state':'unknown'}
    result=deepcopy(value)
    result['offsets_reference']='full_provider_body_utf16'
    for key,count_key in [('entities','entity_count'),('unsupported_entities','unsupported_entity_count')]:
        if result[key] is not None and len(result[key])>16:
            result[count_key]=len(result[key]);result[key]=result[key][:16]
            result[key+'_truncated']=True
    return result
