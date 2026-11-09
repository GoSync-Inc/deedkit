"""ANYM-837: answer an archived WhatsApp bot menu with the bot's own structured choice.

The archive side only: it finds the menu and the option an agent names, refuses what
the native adapter cannot answer, and gives the preview its facts. The adapter
re-reads the captured menu and binds the option losslessly before it sends
(whatsapp/whatsmeow/menu.go, menuAnswer). DeedKit never picks an option; the menu
text and labels are bot-controlled data.
"""
import re

from .store import APIError

# Archived provider_message_type of a menu (whatsapp/whatsmeow/menu.go, menuOptions).
MENU_TYPES=('list','buttons','interactive','template')
# The menu types whose reply shape this release ships; mirrors shippedMenuReplies in
# whatsapp/whatsmeow/menu.go (tests/test_menu_choice.py keeps them equal).
SHIPPED_MENU_TYPES=('list','buttons')
REPLY_TYPES=('list_response','buttons_response','template_reply','interactive_response')
MENU_TEXT_LIMIT=1000
RISK=("live answer by the owner's account; the bot decides what it means; it may book, order, pay or cancel; "
    'DeedKit cannot undo it or verify its effect')
FRESH_MENU='ask the bot for a fresh menu (a text send; needs the owner\'s confirmation), then preview again'
NEXT_ACTION={
    'menu_choice_unsupported':'WhatsApp only, on the native adapter; Telegram bot buttons: press_button',
    'menu_options_not_captured':'this menu was archived before menu options were captured; '+FRESH_MENU,
    'menu_option_not_found':'only options shown with [id: ...] can be answered (not [id (inexact): ...] or [id (native '
        'flow): ...]); links, numbers and codes go to the owner',
    'menu_option_ambiguous':'several options show this ID; a text reply is the fallback',
    'menu_reply_shape_unverified':'this menu kind cannot be answered yet; a text reply is the fallback; the owner can '
        'enable it by tapping an informational option of such a menu on the phone once (a later release adds the shape)',
    'menu_option_mismatch':'pass text empty, the archived_label or its title',
    'menu_choice_unavailable':'this server cannot store menu choices yet; a text reply is the fallback',
}
TEXT_FALLBACK="a text reply is the fallback (a text send; needs the owner's confirmation)"
# The next_action of a failed menu choice by its refusal code (native adapter or dispatch).
FAILED_NEXT={
    'menu_unavailable':'the menu is no longer captured; '+FRESH_MENU,
    'menu_changed':'the menu changed since the preview: preview_message again with its newest menu; if it is gone, '+FRESH_MENU,
    'menu_option_ambiguous':'several options show this ID; '+TEXT_FALLBACK,
    'menu_option_id_unrepresentable':'the option ID is not shown exactly; '+TEXT_FALLBACK,
    'menu_option_unsupported':'this option cannot be answered with a structured reply; '+TEXT_FALLBACK,
    'menu_reply_shape_unverified':'this menu kind cannot be answered yet; '+TEXT_FALLBACK,
    'menu_choice_unsupported':'WhatsApp only, on the native adapter; '+TEXT_FALLBACK,
}
HEADER=re.compile(r'Menu(?: \(.*\))?:')
NUMBERED=re.compile(r'(\d+)\. (.*)')
# The last [id: ...] before the optional link and call markers (menu.go, optionLine). Only this plain marker names a
# choosable option: an ID shown inexactly or a native-flow unit in a buttons menu carries [id (<note>): ...].
WITH_ID=re.compile(r'(.*) \[id: (.*?)\]((?: \[link: [^\]]*\])?(?: \[call: [^\]]*\])?)')
MARKERS=re.compile(r'(?: \[link: [^\]]*\])?(?: \[call: [^\]]*\])?$')


def refusal(code,status=409,**fields):
    failure=APIError(code,status)
    if code in NEXT_ACTION:setattr(failure,'next_action',NEXT_ACTION[code])
    for name,value in fields.items():setattr(failure,name,value)
    return failure


def validate(value):
    """(message_id, option_id) of a menu_choice argument, or invalid_parameters."""
    if (not isinstance(value,dict) or set(value)!={'message_id','option_id'} or
            not all(isinstance(v,str) for v in value.values()) or not 1<=len(value['message_id'])<=256 or
            not 1<=len(value['option_id'])<=256 or any(ord(v)<32 for v in value['option_id'])):
        from .field_errors import refusal as field_refusal
        raise field_refusal('invalid_parameters',[('menu_choice','format','{message_id, option_id}')])
    return value['message_id'],value['option_id']


def options(body):
    """The archived option lines after the last menu header: [(number, label, id or None)], None without one."""
    lines=(body or '').split('\n')
    start=None
    for i,line in enumerate(lines):
        if HEADER.fullmatch(line):start=i
    if start is None:return None
    found=[]
    for line in lines[start+1:]:
        numbered=NUMBERED.fullmatch(line)
        if not numbered:continue
        number,rest=int(numbered.group(1)),numbered.group(2)
        with_id=WITH_ID.fullmatch(rest)
        if with_id:found.append((number,with_id.group(1),with_id.group(2)))
        else:found.append((number,MARKERS.sub('',rest,count=1),None))
    return found


def column_present(c):
    """True when outgoing_messages has the menu_choice column (schema 70+); a plain send never asks."""
    return c.execute('''SELECT 1 FROM pg_attribute WHERE attrelid='outgoing_messages'::regclass
        AND attname='menu_choice' AND NOT attisdropped''').fetchone() is not None


MENU_SQL='''SELECT m.id,m.provider_id,m.sender,m.body,m.sent_at,m.revision,m.direction,m.deleted,
    coalesce(mm.metadata,mr.snapshot->'metadata')->>'provider_message_type' AS provider_message_type
    FROM messages m LEFT JOIN message_revisions mr ON mr.message=m.id AND mr.revision=m.revision
    LEFT JOIN message_metadata mm ON mm.message=m.id AND mm.revision=m.revision
    WHERE m.id=%s AND m.chat=%s AND m.account=%s'''
NEWER_SQL='''SELECT m.id FROM messages m LEFT JOIN message_revisions mr ON mr.message=m.id AND mr.revision=m.revision
    LEFT JOIN message_metadata mm ON mm.message=m.id AND mm.revision=m.revision
    WHERE m.account=%s AND m.chat=%s AND m.sender=%s AND m.direction='incoming' AND NOT m.deleted
      AND m.sent_at>%s AND m.id<>%s
      AND coalesce(mm.metadata,mr.snapshot->'metadata')->>'provider_message_type'=ANY(%s)
    ORDER BY m.sent_at DESC,m.id DESC LIMIT 1'''
ANSWERED_SQL='''SELECT m.body,m.sent_at FROM messages m LEFT JOIN message_revisions mr ON mr.message=m.id AND mr.revision=m.revision
    LEFT JOIN message_metadata mm ON mm.message=m.id AND mm.revision=m.revision
    WHERE m.account=%s AND m.chat=%s AND m.direction='outgoing' AND NOT m.deleted
      AND coalesce(mm.metadata,mr.snapshot->'metadata')->>'reply_to_provider_id'=%s
      AND coalesce(mm.metadata,mr.snapshot->'metadata')->>'provider_message_type'=ANY(%s)
    ORDER BY m.sent_at DESC,m.id DESC LIMIT 1'''
CHOSEN=re.compile(r'Chose menu option: (.*?)(?: \[id(?: \([a-z ]+\))?: .*\])?')


def resolve(c,account,chat_id,chat,value,text):
    """The menu and option a menu_choice names, re-read from the archive (no IPC, no provider call).

    Returns the facts the preview shows and the digest binds; text is normalized to the archived label."""
    message_id,option_id=validate(value)
    if not isinstance(text,str):raise APIError('invalid_message_text')
    if chat['platform']!='whatsapp':raise refusal('menu_choice_unsupported')
    from .whatsapp_capabilities import account_provider
    if account_provider(c,account,'whatsapp')!='whatsmeow':raise refusal('menu_choice_unsupported')
    menu=c.execute(MENU_SQL,(message_id,chat_id,account)).fetchone()
    if not menu or menu['deleted'] or not menu['provider_id']:raise refusal('menu_not_found',404)
    if menu['direction']!='incoming' or menu['provider_message_type'] not in MENU_TYPES:
        raise refusal('menu_not_a_menu')
    listed=options(menu['body'])
    if listed is None:raise refusal('menu_options_not_captured')
    chosen=[o for o in listed if o[2]==option_id]
    if not chosen:raise refusal('menu_option_not_found')
    if len(chosen)>1:raise refusal('menu_option_ambiguous')
    number,label,_=chosen[0]
    if menu['provider_message_type'] not in SHIPPED_MENU_TYPES:raise refusal('menu_reply_shape_unverified')
    if text and text!=label and not label.startswith(text+' - '):
        raise refusal('menu_option_mismatch',archived_label=label)
    newer=c.execute(NEWER_SQL,(account,chat_id,menu['sender'],menu['sent_at'],menu['id'],list(MENU_TYPES))).fetchone()
    answered=c.execute(ANSWERED_SQL,(account,chat_id,menu['provider_id'],list(REPLY_TYPES))).fetchone()
    hints={}
    if newer:hints['newer_menu_message_id']=newer['id']
    if answered:
        match=CHOSEN.fullmatch((answered['body'] or '').split('\n',1)[0])
        hints['already_answered']={'label':match.group(1) if match else None,'at':answered['sent_at']}
    return {'message_id':menu['id'],'provider_message_id':menu['provider_id'],'menu_type':menu['provider_message_type'],
        'revision':menu['revision'],'option_id':option_id,'label':label,'number':number,
        'sender':menu['sender'],'menu_text':(menu['body'] or '')[:MENU_TEXT_LIMIT],
        'other_options':[o[1] for o in listed if o is not chosen[0]],'hints':hints}


def column(choice):
    """The outgoing_messages.menu_choice value: no option text (body, source_text and the artifact hold it)."""
    return {'message_id':choice['message_id'],'provider_message_id':choice['provider_message_id'],
        'menu_type':choice['menu_type']}


def artifact_key(choice):
    """The eleventh render_artifact key; an older runtime refuses the row in dispatch_payload."""
    return {'option_id':choice['option_id'],'label':choice['label'],'menu_type':choice['menu_type']}


def preview_block(choice,chat):
    """What the owner is shown once per bot conversation; the menu text and labels are untrusted bot data."""
    return {'chat_title':chat.get('title'),'menu_sender':choice['sender'],
        'menu_text':{'text':choice['menu_text'],'format':'plain','trust':'untrusted_bot_content'},
        'chosen_option':{'number':choice['number'],'label':choice['label'],'option_id':choice['option_id']},
        'other_options':choice['other_options'],**choice['hints'],'risk':RISK,
        'message_id':choice['message_id'],'menu_type':choice['menu_type']}
