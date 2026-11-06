"""ANYM-810: an opt-in reply-queue switch leaves out private chats that ended with a courtesy after the owner's answer."""
import pytest
from starlette.testclient import TestClient

from messaging_service import chat_queries
from messaging_service.app import create_app
from messaging_service.store import APIError
from test_client_contract import rpc
from test_queue_demotion import components, demote as demote, structured  # noqa: F401  (demote is a fixture)
from test_queue_promotional import promotional_fixture

OWNER_SENDER='OWNER2'
STICKER={'body':'','media_kind':'sticker'}


def conversation(add, chat, *steps, chat_type='private', newest=1):
    """Steps oldest first, one hour apart, the newest `newest` hours ago: ('in'|'out', body or a dict of fields)."""
    for n,(side,content) in enumerate(steps):
        fields=dict(content) if isinstance(content,dict) else {'body':content}
        hours=newest+len(steps)-1-n
        if side=='out':add(chat,str(n+1),hours,direction='outgoing',sender=OWNER_SENDER,chat_type=chat_type,**fields)
        else:add(chat,str(n+1),hours,sender='S'+chat,chat_type=chat_type,**fields)


ASK=('in','Can you send the report today')
ANSWER=('out','Sent it, see the attachment')
OMITTED=('thanks','okthumb','emoji','sticker','two','depth19')
LISTED=('tomorrow','when','again','nostickerreply','payment','askok','askthumb','depth25','never','grp')


def courtesy_fixture(add):
    conversation(add,'thanks',ASK,ANSWER,('in','спасибо'))
    conversation(add,'okthumb',ASK,ANSWER,('in','ok 👍'))
    conversation(add,'emoji',ASK,ANSWER,('in','🙏🙏'))
    conversation(add,'sticker',ASK,ANSWER,('in',STICKER))
    conversation(add,'two',ASK,ANSWER,('in','спасибо'),('in','ок'))
    # X is the 20th meaningful incoming message back from the head: still within the depth.
    conversation(add,'depth19',ASK,ANSWER,*[('in','спасибо')]*19)
    conversation(add,'tomorrow',ASK,ANSWER,('in','ок, завтра созвонимся'))
    conversation(add,'when',ASK,ANSWER,('in','спасибо, а когда?'))
    conversation(add,'again',ASK,ANSWER,('in','And the invoice for March'),('in','🙏'))
    conversation(add,'nostickerreply',('out','Synthetic owner message'),ASK,('in',STICKER))
    # Both owner messages come after X; a question mark in any of them keeps the chat.
    conversation(add,'payment',('in','Statement attached'),('out','Did payment arrive?'),('out','Please check today'),('in','iya'))
    conversation(add,'askok',ASK,('out','Can you confirm the date?'),('in','ок'))
    conversation(add,'askthumb',ASK,('out','Can you confirm the date?'),('in','👍'))
    conversation(add,'depth25',ASK,ANSWER,*[('in','спасибо')]*25)
    conversation(add,'never',('in','Hello there'),('in','спасибо'))
    conversation(add,'grp',('in',{'body':'@owner please review the plan','mentions_owner':True}),ANSWER,
        ('in',{'body':'thanks','mentions_owner':True}),chat_type='group')


def chats(page):
    return sorted(r['provider_chat_id'] for r in page['items'])


# spec: ANYM-810
def test_exclude_omits_only_private_chats_closed_by_a_courtesy_after_the_owners_answer(demote):
    s,ar,key,add,history,queue,rows=demote
    courtesy_fixture(add)
    include=queue(limit=100)
    assert chats(include)==sorted([*OMITTED,*LISTED])
    assert 'excluded_courtesies_total' not in include
    exclude=queue(limit=100,courtesies='exclude')
    assert chats(exclude)==sorted(LISTED)
    assert exclude['excluded_courtesies_total']==len(OMITTED) and exclude['awaiting_reply_total']==len(LISTED)
    only=queue(limit=100,courtesies='only')
    assert chats(only)==sorted(OMITTED) and only['awaiting_reply_total']==len(OMITTED)
    assert 'excluded_courtesies_total' not in only
    # include keeps today's rows, demoted as before.
    got=rows()
    assert components(got['thanks'])['closing_courtesy']==-30 and components(got['grp'])['closing_courtesy']==-30


# spec: ANYM-810
def test_question_emoji_ask_and_a_gif_is_not_a_courtesy(demote):
    s,ar,key,add,history,queue,rows=demote
    for chat,body in (('ask','❓'),('ask2','❔'),('nudge','❗'),('think','🤔'),('thumb','👍'),('pray','🙏')):
        conversation(add,chat,ASK,ANSWER,('in',body))
    conversation(add,'gif',ASK,ANSWER,('in',{'body':'','media_kind':'animation'}))
    got=rows(courtesies='include')
    for chat in ('ask','ask2'):
        assert 'question_mark' in components(got[chat]) and 'closing_courtesy' not in components(got[chat]),chat
        assert got[chat]['awaiting_reply_has_question'] is True
    for chat in ('nudge','think','gif'):assert 'closing_courtesy' not in components(got[chat]),chat
    for chat in ('thumb','pray'):assert components(got[chat])['closing_courtesy']==-30,chat
    assert chats(queue(limit=100,courtesies='exclude'))==['ask','ask2','gif','nudge','think']
    # The owner's ❓ asks in the awaiting-their-reply list too, and is no longer a courtesy run.
    conversation(add,'ownerask',('in','Synthetic peer text'),('out','❓'),newest=30)
    waits=s.list_chats(key,awaiting_their_reply=True,order='waiting_longest',only_questions=True,include_coverage=False,limit=100)
    assert 'ownerask' in chats(waits)


# spec: ANYM-810
def test_courtesies_switch_totals_cursor_and_errors(demote, monkeypatch):
    s,ar,key,add,history,queue,rows=demote
    courtesy_fixture(add)
    promotional_fixture(add,history)
    for promotional in chat_queries.PROMOTIONAL:
        every=queue(limit=100,promotional=promotional)['awaiting_reply_total']
        shown=queue(limit=1,promotional=promotional,courtesies='exclude')
        assert shown['awaiting_reply_total']+shown.get('excluded_courtesies_total',0)==every,promotional
    # The totals, the read-state count and the pages agree with the filtered set, on later pages too.
    page=queue(limit=2,courtesies='exclude',order='priority')
    seen=chats(page)
    while page['next_cursor']:
        page=queue(limit=2,courtesies='exclude',order='priority',after=page['next_cursor'])
        assert page['awaiting_reply_total']==len(LISTED)+6 and page['read_state_unknown_total']==page['awaiting_reply_total']
        assert page['excluded_courtesies_total']==len(OMITTED)
        seen+=chats(page)
    assert sorted(seen)==sorted([*LISTED,'first','regular','shopc','shopt','person','group'])
    # A cursor taken with one value is refused with another.
    first=queue(limit=1,courtesies='exclude')
    for other in ('include','only'):
        with pytest.raises(APIError,match='invalid_cursor'):queue(limit=1,courtesies=other,after=first['next_cursor'])
    # A priority cursor from the previous formula is refused.
    monkeypatch.setattr(chat_queries,'SCORE_FORMULA',3)
    old=queue(order='priority',limit=1)['next_cursor']
    monkeypatch.undo()
    with pytest.raises(APIError,match='invalid_cursor'):queue(order='priority',limit=1,after=old)
    # The count stays within the key's scope; absent means none.
    narrow=s.issue_key('courtesy-other',['demote-tg2'])
    assert 'excluded_courtesies_total' not in s.list_chats(narrow,awaiting_reply=True,include_coverage=False,courtesies='exclude')
    assert 'excluded_courtesies_total' not in queue(chat_types=['group'],courtesies='exclude')
    for bad in ({'courtesies':'none'},{'courtesies':'exclude','awaiting_reply':False},{'courtesies':'only','awaiting_reply':False}):
        with pytest.raises(APIError,match='invalid_filter'):s.list_chats(key,include_coverage=False,**{'awaiting_reply':True,**bad})
    with pytest.raises(APIError,match='invalid_filter'):
        s.list_chats(key,awaiting_their_reply=True,order='waiting_longest',courtesies='exclude',include_coverage=False)
    with TestClient(create_app(s,['testserver'])) as client:
        body=structured(rpc(client,key,'list_chats',{'awaiting_reply':True,'courtesies':'exclude','limit':100}))
        bad=rpc(client,key,'list_chats',{'awaiting_reply':True,'courtesies':'none'}).json()
    assert body['excluded_courtesies_total']==len(OMITTED)
    assert bad['error']['data']['field_errors']==[{'field':'courtesies','reason':'enum','allowed':['include','exclude','only']}]


# spec: ANYM-810
def test_muted_and_dismissed_courtesy_rows_follow_the_promotional_precedent(demote):
    s,ar,key,add,history,queue,rows=demote
    courtesy_fixture(add)
    thanks=next(r for r in queue(courtesies='only',limit=100)['items'] if r['provider_chat_id']=='thanks')
    s.mute_reply_source(key,chat_id=thanks['id'])
    default=queue(courtesies='exclude',limit=100)
    assert default['excluded_courtesies_total']==len(OMITTED)-1 and default['muted_total']==0
    assert 'thanks' not in chats(queue(courtesies='only',limit=100))
    # include_muted returns muted rows; the courtesies choice still applies to them.
    assert 'thanks' not in chats(queue(courtesies='exclude',include_muted=True,limit=100))
    shown={r['provider_chat_id']:r for r in queue(courtesies='only',include_muted=True,limit=100)['items']}
    assert shown['thanks']['awaiting_reply_muted']=='chat'
    # A row listed through only can be dismissed and restored.
    two=next(r for r in queue(courtesies='only',limit=100)['items'] if r['provider_chat_id']=='two')
    s.dismiss_reply(key,two['id'],two['awaiting_reply_message_id'],two['awaiting_reply_revision'],reason='no_reply_needed')
    assert 'two' not in chats(queue(courtesies='only',limit=100))
    assert queue(courtesies='exclude',limit=100)['excluded_courtesies_total']==len(OMITTED)-2
    s.restore_reply(key,two['id'],two['awaiting_reply_message_id'],two['awaiting_reply_revision'])
    assert 'two' in chats(queue(courtesies='only',limit=100))


# spec: ANYM-810
def test_the_courtesy_count_ignores_the_broadcast_filter_in_the_muted_hint_choice(demote):
    s,ar,key,add,history,queue,rows=demote
    # A sender-muted fallback: the courtesy came from a muted sender, so the hint falls back to the older request.
    conversation(add,'mixed',ASK,ANSWER,('in',{'body':'спасибо','sender':'Muted'}))
    s.mute_reply_source(key,account='demote-tg',sender_id='Muted')
    plain=queue(include_muted=True,courtesies='exclude')
    filtered=queue(include_muted=True,courtesies='exclude',exclude_likely_broadcasts=True)
    assert plain.get('excluded_courtesies_total',0)==filtered.get('excluded_courtesies_total',0)
    every=queue(include_muted=True)
    assert plain['awaiting_reply_total']+plain.get('excluded_courtesies_total',0)==every['awaiting_reply_total']


# spec: ANYM-810
def test_list_chats_schemas_show_the_courtesies_argument_and_count():
    from pathlib import Path
    import json
    root=Path(__file__).resolve().parents[1]
    catalog=json.loads((root/'services/gateway/src/agent_gateway/catalog.json').read_text())
    contracts=json.loads((root/'tests/platform/fixtures/messaging-contracts.json').read_text())
    for tool in (next(t for t in catalog if t.get('operation')=='list_chats'),
                 next(t for t in contracts['tools'] if t['name']=='list_chats')):
        argument=tool['inputSchema']['properties']['courtesies']
        assert argument['enum']==['include','exclude','only'] and argument['default']=='include'
        schema=tool['outputSchema']
        assert schema['additionalProperties'] is True and 'required' not in schema
        assert schema['properties']['excluded_courtesies_total']['type']=='integer'
    described=next(t for t in contracts['tools'] if t['name']=='list_chats')['description']
    assert 'courtesies=exclude' in described and 'courtesies=only once' in described
