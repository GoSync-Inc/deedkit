"""Inline keyboards of a Telegram message reach agents as read-only evidence (ANYM-658).

Synthetic messages only. Nothing here presses, opens or fetches a button; the capture keeps text, kind and
link, and never the callback data or an inline query.
"""
from datetime import datetime,timezone
import json
import logging

import pytest
from telethon import types

from messaging_service import message_buttons
from messaging_service.adapters import telegram_message
from messaging_service.envelope import MAX_ENVELOPE,canonical,stable_id,validate
from messaging_service.subscriptions import match_subscriptions
from test_archive import archive as archive, event

SENTINEL=b'SYNTHETIC-CALLBACK-SECRET-7731'
QUERY='SYNTHETIC-INLINE-QUERY-5520'
NOW=datetime(2026,10,13,tzinfo=timezone.utc)
BUTTON=types.KeyboardInlineButton


def rows(*lines):
    return types.ReplyInlineMarkup(rows=[types.KeyboardInlineButtonRow(buttons=list(line)) for line in lines])


def url(text,link='https://example.invalid/a'):return BUTTON(text=text,type=types.InlineButtonTypeUrl(url=link))
def callback(text,data=SENTINEL,**kw):return BUTTON(text=text,type=types.InlineButtonTypeCallback(data=data,**kw))


def bot_message(markup,*,body='Choose',**changes):
    return types.Message(id=changes.pop('id',10),peer_id=types.PeerUser(42),from_id=types.PeerUser(42),date=NOW,
        message=body,reply_markup=markup,**changes)


@pytest.fixture()
def captured(monkeypatch):
    monkeypatch.setenv('ANYMSG_CAPTURE_BUTTONS','1')
    return lambda message,**kw:telegram_message('tg-owner',message,**kw)


ALL_KINDS=[
    (url('Open'),{'text':'Open','kind':'url','url':'https://example.invalid/a'}),
    (callback('Pay'),{'text':'Pay','kind':'callback'}),
    (BUTTON(text='App',type=types.InlineButtonTypeWebView(url='https://example.invalid/app')),
        {'text':'App','kind':'web_app','url':'https://example.invalid/app'}),
    (BUTTON(text='Login',type=types.InlineButtonTypeUrlAuth(url='https://example.invalid/auth',button_id=7)),
        {'text':'Login','kind':'login_url','url':'https://example.invalid/auth'}),
    (BUTTON(text='Share',type=types.InlineButtonTypeSwitchInline(query=QUERY)),{'text':'Share','kind':'switch_inline'}),
    (BUTTON(text='Buy',type=types.InlineButtonTypeBuy()),{'text':'Buy','kind':'buy'}),
    (BUTTON(text='Copy',type=types.InlineButtonTypeCopy(copy_text='CODE-1')),{'text':'Copy','kind':'copy','copy_text':'CODE-1'}),
    (BUTTON(text='Play',type=types.InlineButtonTypeGame()),{'text':'Play','kind':'game'}),
    (BUTTON(text='Profile',type=types.InlineButtonTypeUserProfile(user_id=5)),{'text':'Profile','kind':'unknown'}),
]


# spec: ANYM-658
def test_buttons_are_captured_only_with_the_flag(monkeypatch):
    message=bot_message(rows([url('Open')]))
    monkeypatch.delenv('ANYMSG_CAPTURE_BUTTONS',raising=False)
    assert 'buttons' not in telegram_message('tg-owner',message)['metadata']
    monkeypatch.setenv('ANYMSG_CAPTURE_BUTTONS','0')
    assert 'buttons' not in telegram_message('tg-owner',message)['metadata']
    monkeypatch.setenv('ANYMSG_CAPTURE_BUTTONS','1')
    assert telegram_message('tg-owner',message)['metadata']['buttons']=={
        'rows':[[{'text':'Open','kind':'url','url':'https://example.invalid/a'}]]}


# spec: ANYM-658
def test_every_button_kind_has_its_shape(captured):
    layout=rows(*[[button] for button,_ in ALL_KINDS])
    assert captured(bot_message(layout))['metadata']['buttons']['rows']==[[shape] for _,shape in ALL_KINDS]


# spec: ANYM-658
def test_a_password_callback_stays_a_callback_and_a_message_without_a_keyboard_has_no_key(captured):
    kinds=captured(bot_message(rows([callback('Pay',requires_password=True)])))['metadata']['buttons']['rows']
    assert kinds==[[{'text':'Pay','kind':'callback'}]]
    assert 'buttons' not in captured(bot_message(None))['metadata']
    assert 'buttons' not in captured(bot_message(rows()))['metadata']
    # Reply keyboards are not captured.
    reply=types.ReplyKeyboardMarkup(rows=[types.KeyboardButtonRow(buttons=[
        types.KeyboardButton(text='Yes',type=types.ButtonTypeDefault())])])
    assert 'buttons' not in captured(bot_message(reply))['metadata']


# spec: ANYM-658
def test_callback_data_and_inline_queries_never_enter_the_capture(captured,caplog):
    caplog.set_level(logging.DEBUG)
    layout=rows([callback('Pay'),BUTTON(text='Share',type=types.InlineButtonTypeSwitchInline(query=QUERY))])
    envelope=captured(bot_message(layout))
    text=json.dumps(envelope)+repr(envelope)+caplog.text
    for secret in (SENTINEL.decode(),repr(SENTINEL),QUERY,SENTINEL.hex()):
        assert secret not in text
    # The tiny callback keyboard still holds the labels.
    assert [b['kind'] for b in envelope['metadata']['buttons']['rows'][0]]==['callback','switch_inline']


# spec: ANYM-658
def test_limits_cut_text_copy_text_and_the_button_count(captured):
    long_label='Ω'*300
    layout=rows([BUTTON(text=long_label,type=types.InlineButtonTypeCopy(copy_text='c'*400))])
    button=captured(bot_message(layout))['metadata']['buttons']['rows'][0][0]
    assert len(button['text'])==128 and len(button['copy_text'])==256
    many=rows(*[[url('b%d'%n)] for n in range(130)])
    buttons=captured(bot_message(many))['metadata']['buttons']
    assert sum(len(r) for r in buttons['rows'])==100 and buttons['truncated'] is True
    assert [b['text'] for r in buttons['rows'] for b in r][-1]=='b99'


# spec: ANYM-658
def test_control_characters_and_multibyte_labels_are_normalised(captured):
    layout=rows([url('Pay\x00\n now‮ 💳\x07')])
    text=captured(bot_message(layout))['metadata']['buttons']['rows'][0][0]['text']
    # Control and bidi-control characters become spaces; the label is stripped and cut to 128 characters.
    assert text=='Pay   now  💳'


# spec: ANYM-658
def test_unsafe_unknown_and_malformed_pieces_are_dropped_not_fatal(captured):
    layout=rows([url('Js','javascript:alert(1)'),url('Ok'),url('Userinfo','https://u:p@example.invalid/'),
        BUTTON(text='X',type=object())])
    buttons=captured(bot_message(layout))['metadata']['buttons']['rows'][0]
    assert buttons[0]=={'text':'Js','kind':'url'} and buttons[1]['url']=='https://example.invalid/a'
    assert buttons[2]=={'text':'Userinfo','kind':'url'} and buttons[3]=={'text':'X','kind':'unknown'}
    junk=types.Message(id=11,peer_id=types.PeerUser(42),from_id=types.PeerUser(42),date=NOW,message='junk')
    for markup in (object(),types.ReplyInlineMarkup(rows=[None,object(),types.KeyboardInlineButtonRow(buttons=[None,7])]),
            types.ReplyInlineMarkup(rows='nope')):
        junk.reply_markup=markup
        metadata=captured(junk)['metadata']
        assert 'buttons' not in metadata or all(isinstance(b,dict) for r in metadata['buttons']['rows'] for b in r)


# spec: ANYM-658
def test_a_parser_failure_costs_the_keyboard_never_the_message(captured,monkeypatch):
    def boom(*args,**kwargs):raise RuntimeError('parser exploded '+SENTINEL.decode())
    monkeypatch.setattr(message_buttons,'build_rows',boom)
    envelope=captured(bot_message(rows([callback('Pay')]),body='still here'))
    assert envelope['body']=='still here' and 'buttons' not in envelope['metadata']
    validate(envelope)


# spec: ANYM-658
def test_a_huge_keyboard_with_2kb_links_fits_the_envelope(captured):
    link='https://example.invalid/'+'a'*2000
    layout=rows(*[[url('B%d'%n,link)] for n in range(100)])
    envelope=captured(bot_message(layout))
    record={'kind':'message','peer_kind':'user','envelope':envelope,'captured_at':'2026-10-13T00:00:00+00:00'}
    assert len(canonical(record))<=MAX_ENVELOPE and validate(envelope)['metadata']['buttons']


def crowded(captured,label,count=100):
    """A message whose body and 150 long links leave the keyboard only a few KB of the envelope."""
    body='𝔸'*65536
    entities=[types.MessageEntityTextUrl(offset=2*n,length=2,url='https://example.invalid/'+'p'*1500) for n in range(150)]
    layout=rows(*[[url(label)] for _ in range(count)])
    message=bot_message(layout,body=body,entities=entities)
    return captured(message)


# spec: ANYM-658
def test_a_nearly_full_envelope_drops_links_then_trailing_buttons_and_still_validates(captured):
    envelope=crowded(captured,'𝔸'*128)
    buttons=envelope['metadata']['buttons']
    record={'kind':'message','peer_kind':'user','envelope':envelope,'captured_at':'2026-10-13T00:00:00+00:00'}
    assert len(canonical(record))<=MAX_ENVELOPE
    flat=[b for r in buttons['rows'] for b in r]
    assert buttons['truncated'] is True and 0<len(flat)<100 and all('url' not in b for b in flat)
    validate(envelope)


# spec: ANYM-658
def test_no_room_left_keeps_the_message_without_buttons(captured,monkeypatch):
    assert message_buttons.fit([[{'text':'x','kind':'callback'}]],False,10) is None
    monkeypatch.setattr(message_buttons,'WRAPPER_RESERVE',MAX_ENVELOPE)
    envelope=captured(bot_message(rows([url('x')]),body='Choose'))
    assert envelope['body']=='Choose' and 'buttons' not in envelope['metadata'] and envelope['metadata']['message_kind']=='text'
    validate(envelope)


# spec: ANYM-658
def test_validator_accepts_the_declared_shape_and_nothing_else():
    ok={'rows':[[{'text':'Open','kind':'url','url':'https://example.invalid/a'},{'text':'C','kind':'copy','copy_text':'x'}]],
        'truncated':True}
    assert validate(event(metadata={'buttons':ok}))['metadata']['buttons']==ok
    bad=[{'rows':[[{'text':'a','kind':'press'}]]},{'rows':[[{'text':'a','kind':'callback','data':'AA=='}]]},
        {'rows':[[{'text':'a','kind':'url','url':'javascript:1'}]]},{'rows':[[{'text':'a','kind':'callback','url':'https://example.invalid/'}]]},
        {'rows':[[{'text':'a'*129,'kind':'callback'}]]},{'rows':[[{'text':'a','kind':'copy','copy_text':'c'*257}]]},
        {'rows':[[{'text':'a','kind':'callback'}]]*101},{'rows':[[{'text':'a\x00','kind':'callback'}]]},
        {'rows':[[{'text':'a','kind':'url','url':'https://t.me/login/12345'}]]},
        {'rows':[[{'text':'a','kind':'url','url':'tg://login?code=1'}]]},
        {'rows':[[{'text':'a','kind':'url','url':'tg://passport'}]]},
        {'rows':[]},{'rows':'x'},{'rows':[[{'text':'a','kind':'callback'}]],'extra':1},{'rows':[[{'text':'a','kind':'callback'}]],'truncated':'yes'},
        {'rows':[['a']]},{'rows':[[{'text':'a','kind':'callback','copy_text':'x'}]]}]
    for value in bad:
        with pytest.raises(ValueError):validate(event(metadata={'buttons':value}))
    assert 'buttons' not in validate(event(metadata={'buttons':None}))['metadata']


# spec: ANYM-658
def test_a_deleted_message_loses_its_buttons_in_the_validator():
    keyboard={'rows':[[{'text':'Open','kind':'callback'}]]}
    assert 'buttons' not in validate(event(metadata={'buttons':keyboard},deleted=True,body=''))['metadata']


def ingest_keyboard(ar,labels,*,when='2026-10-01T12:00:00+00:00',**changes):
    keyboard={'rows':[[{'text':label,'kind':'callback'} for label in labels]]} if labels else None
    metadata={'chat_type':'private','message_kind':'text',**({'buttons':keyboard} if keyboard else {})}
    return ar.ingest(event(body='Pay now',metadata=metadata,sent_at='2026-10-01T12:00:00+00:00',updated_at=when,**changes))


def stored(s,mid,revision=None):
    with s.pool.connection() as c:
        if revision is None:
            return c.execute('SELECT metadata FROM message_metadata WHERE message=%s',(mid,)).fetchone()['metadata']
        return c.execute('SELECT snapshot FROM message_revisions WHERE message=%s AND revision=%s',(mid,revision)).fetchone()['snapshot']['metadata']


# spec: ANYM-658
def test_a_markup_only_edit_is_a_new_revision_and_a_removed_keyboard_has_no_key(archive):
    ar,s,key,_=archive
    first=ingest_keyboard(ar,['Pay'])
    mid=first['message']
    assert first['outcome']=='created'
    assert ingest_keyboard(ar,['Pay'])['outcome']=='duplicate'
    # The same text with a replaced keyboard, a second later: a bot replaced "Pay" with "Done".
    assert ingest_keyboard(ar,['Done'],when='2026-10-01T12:00:01+00:00')['outcome']=='edited'
    assert stored(s,mid,1)['buttons']['rows'][0][0]['text']=='Pay'
    assert stored(s,mid,2)['buttons']['rows'][0][0]['text']=='Done'
    assert stored(s,mid)['buttons']['rows'][0][0]['text']=='Done'
    # The edit that removes the keyboard yields a revision without the key.
    assert ingest_keyboard(ar,None,when='2026-10-01T12:00:02+00:00')['outcome']=='edited'
    assert 'buttons' not in stored(s,mid,3) and 'buttons' not in stored(s,mid)
    items=s.get_messages(key,stable_id('chat','archive-test','100'))['items']
    assert len(items)==1 and 'buttons' not in items[0] and 'buttons_count' not in items[0]


# spec: ANYM-658
def test_an_older_or_same_version_reread_never_rewrites_the_keyboard(archive):
    ar,s,key,_=archive
    mid=ingest_keyboard(ar,['Done'],when='2026-10-01T12:00:05+00:00')['message']
    # A history read of an older version and a flag-off re-read of the same version change nothing.
    assert ingest_keyboard(ar,['Pay'],when='2026-10-01T12:00:01+00:00',origin='backfill')['outcome']=='unchanged'
    assert ingest_keyboard(ar,None,when='2026-10-01T12:00:05+00:00',origin='backfill')['outcome']=='unchanged'
    assert stored(s,mid)['buttons']['rows'][0][0]['text']=='Done'


# spec: ANYM-658
def test_a_same_version_observation_of_another_keyboard_changes_nothing(archive):
    ar,s,key,_=archive
    mid=ingest_keyboard(ar,['Pay'])['message']
    # Same provider version, another keyboard: not an edit. No revision, no event, and the reads keep what the
    # revision's snapshot says.
    assert ingest_keyboard(ar,['Done'])['outcome']=='unchanged'
    assert stored(s,mid)['buttons']['rows'][0][0]['text']=='Pay' and stored(s,mid,1)['buttons']['rows'][0][0]['text']=='Pay'
    with s.pool.connection() as c:
        assert c.execute('SELECT revision FROM messages WHERE id=%s',(mid,)).fetchone()['revision']==1
    assert s.get_messages(key,stable_id('chat','archive-test','100'))['items'][0]['buttons']['rows'][0][0]['text']=='Pay'


# spec: ANYM-658
def test_removal_after_a_completed_keyboard_still_makes_a_revision(archive):
    ar,s,key,_=archive
    # Archived while the flag was off, completed by a same-version re-read with the flag on, then the bot removes it.
    mid=ingest_keyboard(ar,None)['message']
    assert ingest_keyboard(ar,['Pay'])['outcome']=='unchanged' and stored(s,mid)['buttons']['rows'][0][0]['text']=='Pay'
    assert ingest_keyboard(ar,None,when='2026-10-01T12:00:09+00:00')['outcome']=='edited'
    assert 'buttons' not in stored(s,mid) and 'buttons' not in stored(s,mid,2)
    assert 'buttons' not in s.get_messages(key,stable_id('chat','archive-test','100'))['items'][0]


def conversation(s,key,method,detail,**kw):
    from messaging_service.presentation import render
    chat=stable_id('chat','archive-test','100')
    if method=='get_messages':result=s.get_messages(key,chat,view='conversation',compact=detail=='compact',**kw)
    elif method=='search':result=s.search(key,view='conversation',compact=detail=='compact',**kw)
    else:
        mid=s.get_messages(key,chat,view='conversation')['items'][0]['id']
        result=s.get_context(key,mid,view='conversation',compact=detail=='compact')
    return render(method,result,detail)


def rows_of(output,method):
    return [output['message'],*output['context']] if method=='get_context' else output['items']


# spec: ANYM-658
@pytest.mark.parametrize('method',['get_messages','search','get_context'])
def test_compact_conversation_reads_and_transcripts_carry_the_buttons_without_links(archive,method):
    ar,s,key,_=archive
    keyboard={'rows':[[{'text':'B%d'%n,'kind':'url','url':'https://example.invalid/secret-%d'%n} for n in range(12)]]}
    ar.ingest(event(body='Menu',metadata={'chat_type':'private','message_kind':'text','buttons':keyboard}))
    compact=conversation(s,key,method,'compact')
    shown=next(r for r in rows_of(compact,method) if r.get('buttons') or r.get('buttons_count'))
    assert shown['buttons_count']==12 and len(shown['buttons'])==8 and all(set(b)=={'text','kind'} for b in shown['buttons'])
    assert 'secret-' not in json.dumps(compact,default=str)
    full=conversation(s,key,method,'full')
    assert next(r for r in rows_of(full,method) if r.get('buttons'))['buttons']==keyboard
    transcript=conversation(s,key,method,'transcript')
    assert '[Кнопки] B0 | B1 | B2 | B3 | B4 | B5 | B6 | B7' in transcript['transcript'] and 'secret-' not in json.dumps(transcript)


def three_chat(s,key):
    return stable_id('chat','archive-test','100')


# spec: ANYM-658
def test_full_and_compact_reads_carry_the_buttons(archive):
    ar,s,key,_=archive
    keyboard={'rows':[[{'text':'B%d'%n,'kind':'url','url':'https://example.invalid/%d'%n} for n in range(12)]],'truncated':True}
    ar.ingest(event(body='Menu',metadata={'chat_type':'private','message_kind':'text','buttons':keyboard}))
    ar.ingest(event(provider_id='18',body='Plain',metadata={'chat_type':'private','message_kind':'text'}))
    chat=three_chat(s,key)
    full={i['body']:i for i in s.get_messages(key,chat)['items']}
    assert full['Menu']['buttons']==keyboard and 'buttons' not in full['Plain'] and 'buttons_count' not in full['Plain']
    compact={i['snippet']:i for i in s.get_messages(key,chat,compact=True)['items']}
    summary=compact['Menu']
    assert summary['buttons_count']==12 and len(summary['buttons'])==8
    assert all(set(b)=={'text','kind'} for b in summary['buttons']) and summary['buttons'][0]['text']=='B0'
    assert 'https://example.invalid' not in json.dumps(summary)
    assert 'buttons' not in compact['Plain']
    found={i['body']:i for i in s.search(key)['items']}
    assert found['Menu']['buttons']==keyboard
    anchor=s.get_context(key,full['Menu']['id'])
    assert any(i.get('buttons')==keyboard for i in [anchor.get('message',{}),*anchor.get('items',[]),*anchor.get('context',[])])


# spec: ANYM-658
def test_button_labels_are_not_indexed_by_search(archive):
    ar,s,key,_=archive
    ar.ingest(event(body='Menu',metadata={'chat_type':'private','message_kind':'text',
        'buttons':{'rows':[[{'text':'Zebrafish','kind':'callback'}]]}}))
    assert s.search(key,'Zebrafish')['items']==[]
    assert len(s.search(key,'Menu')['items'])==1


# spec: ANYM-658
def test_a_deleted_message_loses_its_buttons_everywhere(archive):
    ar,s,key,_=archive
    keyboard={'rows':[[{'text':'Pay','kind':'callback'}]]}
    mid=ar.ingest(event(body='Menu',metadata={'chat_type':'private','message_kind':'text','buttons':keyboard}))['message']
    assert ar.ingest(event(body='',deleted=True,updated_at='2026-10-01T12:05:00+00:00'))['outcome']=='deleted'
    chat=three_chat(s,key)
    for item in s.get_messages(key,chat)['items']+s.search(key)['items']:
        assert 'buttons' not in item and 'Pay' not in json.dumps(item,default=str)
    assert 'Pay' not in json.dumps(s.get_context(key,mid),default=str)
    # A deletion observed before the message is imported leaves no keyboard either.
    with s.pool.connection() as c:
        c.execute('DELETE FROM message_metadata WHERE message=%s',(mid,))
    assert 'Pay' not in json.dumps(s.get_messages(key,chat)['items'],default=str)


# spec: ANYM-658
def test_subscription_references_carry_no_buttons_while_the_snapshot_keeps_them(archive):
    ar,s,key,_=archive
    permissions=['read','subscription-write']
    token=s.issue_key('btn-subscriber',['archive-test'],permissions=permissions,key_permissions=permissions)
    chat=stable_id('chat','archive-test','100')
    ar.ingest(event(provider_id='1',body='before'))
    sid=s.create_subscription(token,[chat],kinds=['message.created','message.edited'],
        origins=['live','fixture','backfill','reconcile'])['subscription_id']
    keyboard={'rows':[[{'text':'Zebra-Pay','kind':'url','url':'https://example.invalid/zz'}]]}
    meta={'chat_type':'private','message_kind':'text'}
    ar.ingest(event(provider_id='2',body='created',metadata={**meta,'buttons':keyboard}))
    ar.ingest(event(provider_id='2',body='created',updated_at='2026-10-01T12:01:00+00:00',
        metadata={**meta,'buttons':{'rows':[[{'text':'Zebra-Done','kind':'callback'}]]}}))
    match_subscriptions(s)
    items=s.get_subscription_events(token,sid,limit=50)['items']
    assert {i['kind'] for i in items}>={'message.created','message.edited'}
    assert all('buttons' not in (i.get('metadata') or {}) for i in items)
    assert 'Zebra' not in json.dumps(items,default=str)
    with s.pool.connection() as c:
        snapshots=c.execute("SELECT snapshot FROM events WHERE kind LIKE 'message.%' ORDER BY id").fetchall()
    assert any('buttons' in (r['snapshot'].get('metadata') or {}) for r in snapshots)


# spec: ANYM-658
def test_a_synthetic_callback_appears_in_no_stored_envelope_event_or_response(archive,caplog):
    ar,s,key,_=archive
    caplog.set_level(logging.DEBUG)
    envelope=telegram_message_with_secret()
    ar.ingest(event(body=envelope['body'],metadata=envelope['metadata']))
    with s.pool.connection() as c:
        dumped=json.dumps([dict(r) for r in c.execute('SELECT envelope FROM ingest_receipts').fetchall()],default=str)
        dumped+=json.dumps([dict(r) for r in c.execute('SELECT snapshot FROM message_revisions').fetchall()],default=str)
        dumped+=json.dumps([dict(r) for r in c.execute('SELECT snapshot FROM events').fetchall()],default=str)
        dumped+=json.dumps([dict(r) for r in c.execute('SELECT metadata FROM message_metadata').fetchall()],default=str)
    dumped+=json.dumps(s.search(key),default=str)+json.dumps(s.get_messages(key,stable_id('chat','archive-test','100')),default=str)
    for secret in (SENTINEL.decode(),SENTINEL.hex(),QUERY):
        assert secret not in dumped and secret not in caplog.text


def telegram_message_with_secret():
    import os
    os.environ['ANYMSG_CAPTURE_BUTTONS']='1'
    try:
        layout=rows([callback('Pay'),BUTTON(text='Share',type=types.InlineButtonTypeSwitchInline(query=QUERY))])
        e=telegram_message('tg-owner',bot_message(layout,body='Menu'))
    finally:
        del os.environ['ANYMSG_CAPTURE_BUTTONS']
    return {'body':e['body'],'metadata':{k:v for k,v in e['metadata'].items() if v is not None}}


# spec: ANYM-658
def test_messaging_tool_descriptions_state_the_read_only_rule():
    import pathlib
    snapshot=json.loads((pathlib.Path(__file__).parent/'platform'/'fixtures'/'messaging-contracts.json').read_text())
    tools={tool['name']:tool['description'] for tool in snapshot['tools']}
    for name in ('search','get_messages','get_context'):
        text=' '.join(tools[name].split())
        assert 'read-only evidence' in text and 'Mini App is a live action and is not offered' in text, name
        assert 'untrusted data' in text and 'login_url' in text, name


# spec: ANYM-797
def test_button_texts_say_what_is_offered_and_how_absence_reads():
    import ast,pathlib,re
    root=pathlib.Path(__file__).parents[1]
    flat=lambda text:' '.join(text.split())
    package=root/'services/messaging/messaging_service'
    trees=[ast.parse(path.read_text()) for path in (package/'app.py',*sorted((package/'app_tools').glob('*.py')))]
    tools=('search','get_messages','get_context')
    docs=[flat(ast.get_docstring(n)) for tree in trees for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in tools]
    assert len(docs)==3
    catalog=json.loads((root/'services/gateway/src/agent_gateway/catalog.json').read_text())
    entry=flat(next(t for t in catalog if t['name']=='messages_get_context')['description'])
    setup=flat((root/'SETUP.md').read_text())
    for text in (*docs,entry):
        assert 'pressing a button or opening a Mini App is a live action and is not offered' not in text
        assert 'untrusted data' in text and 'login_url' in text
    passages=[re.findall(r'Buttons are read-only evidence.*?(?= Every button link| Links:)',text)[0] for text in (*docs,entry)]
    for text in passages:
        assert len(text)<=400 and len(re.findall(r'[.?!](?: |$)',text))<=3, text
        # A listed operation is not an offered one: the manifest lists every supported operation, `available` decides.
        for word in ('inspect_message_buttons','press_button','owner-confirmed','callback','available','list_messenger_operations',
                     'none,','capture off','production','2026-11-02','unfit','unparsed','deleted'):
            assert word in text, word
        assert text.count('is not offered')==1 and 'Mini App is' in text
        assert 'capture off (default; production on since 2026-11-02, no backfill)' in text, text
    assert len(set(passages[:3]))==1 and 'no captured keyboard' in passages[0] and passages[0] in setup and 'Nothing presses' not in setup


# spec: ANYM-797
def test_button_texts_name_the_tool_and_operation_that_inspect_a_live_keyboard():
    # Feedback cfff3996: an agent read the bare name as a tool and called messages_inspect_message_buttons.
    import ast,pathlib
    root=pathlib.Path(__file__).parents[1]
    flat=lambda text:' '.join(text.split())
    package=root/'services/messaging/messaging_service'
    trees=[ast.parse(path.read_text()) for path in (package/'app.py',*sorted((package/'app_tools').glob('*.py')))]
    docs=[flat(ast.get_docstring(n)) for tree in trees for n in ast.walk(tree)
          if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in ('search','get_messages','get_context')]
    assert len(docs)==3
    for text in (*docs,flat((root/'SETUP.md').read_text())):
        assert 'execute_messenger_operation(operation=inspect_message_buttons)' in text
        assert 'evidence: inspect_message_buttons reads' not in text
