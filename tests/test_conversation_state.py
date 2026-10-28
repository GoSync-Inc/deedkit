"""Conversation head assertions; all messages and provider observations are synthetic."""
from datetime import datetime, timedelta, timezone

import pytest


NOW = datetime(2026, 10, 15, 0, 0, 0, tzinfo=timezone.utc)


def message(mid, direction='incoming', *, second=0, body='Synthetic text', **changes):
    return {'id':mid, 'provider_id':mid, 'revision':1, 'direction':direction,
        'sender':'synthetic-person', 'sent_at':NOW-timedelta(seconds=second),
        'body':body, 'kind':'text' if body else 'unknown', 'display_text':body,
        'is_service':False, 'order_status':'known', **changes}


def state(messages, *, platform='whatsapp', chat_type='private', history=None, **changes):
    from messaging_service.conversation_state import derive_state
    return derive_state(messages, platform=platform, chat_type=chat_type, as_of=NOW,
        history={'last_head_scan_at':NOW, 'gaps':[], 'state':'window_traversed',
            'last_error_code':None, **(history or {})}, **changes)


def test_mixed_direction_equal_second_is_not_a_known_whatsapp_turn():
    result=state([message('Z','incoming'), message('A','outgoing')])
    assert result['last_turn']=='unknown'
    assert result['suggested_action']=='unclear'
    assert result['basis']['code']=='ambiguous_message_order'
    assert {e['message_id'] for e in result['basis']['evidence']}=={'A','Z'}


def test_equal_second_same_actor_preserves_actor_but_latest_message_is_ambiguous():
    result=state([message('Z','outgoing'), message('A','outgoing')])
    assert result['last_turn']=='owner'
    assert result['suggested_action']=='await_peer'
    assert result['order_status']=='ambiguous'
    assert result['latest_known_meaningful_message'] is None
    assert result['latest_known_meaningful_messages'][0]['id']=='Z'


def test_telegram_provider_order_in_a_second_has_one_latest_turn():
    result=state([message('11','outgoing'), message('10','incoming')],platform='telegram')
    assert result['last_turn']=='owner'
    assert result['suggested_action']=='await_peer'
    assert result['latest_known_meaningful_message']['provider_message_id']=='11'


@pytest.mark.parametrize('history,code',[
    ({'last_head_scan_at':None},'provider_head_not_verified'),
    ({'last_head_scan_at':NOW-timedelta(minutes=6)},'provider_head_stale'),
    ({'gaps':['catchup_gap']},'history_has_gaps'),
    ({'last_error_code':'provider_unavailable'},'provider_unavailable'),
])
def test_head_uncertainty_changes_suggestion_without_relabeling_archive_actor(history,code):
    result=state([message('out','outgoing')],history=history)
    assert result['last_turn']=='owner'
    assert result['suggested_action']=='unclear'
    assert result['basis']['code']==code
    assert result['as_of']==NOW


def test_scan_before_newest_message_is_not_a_current_head():
    result=state([message('new')],history={'last_head_scan_at':NOW-timedelta(seconds=1)})
    assert result['head_freshness']=='behind_latest_message'
    assert result['suggested_action']=='unclear'


def test_latest_known_service_event_does_not_replace_meaningful_message():
    result=state([message('timer','outgoing',is_service=True,kind='system'),
        message('question','incoming',second=1)])
    assert result['last_turn']=='peer'
    assert result['latest_known_meaningful_message']['id']=='question'
    assert result['suggested_action']=='review_incoming'


def test_newer_unknown_empty_message_requires_review_instead_of_guessing_media():
    result=state([message('empty','outgoing',body=''),message('known','incoming',second=1)])
    assert result['last_turn']=='unknown'
    assert result['suggested_action']=='unclear'
    assert result['basis']['code']=='unclassified_latest_message'
    assert result['latest_known_meaningful_message']['id']=='known'


def test_unknown_equal_time_sibling_cannot_be_hidden_by_a_hash_tie_breaker():
    result=state([message('Z','incoming'),message('A','outgoing',body='')])
    assert result['last_turn']=='unknown'
    assert result['suggested_action']=='unclear'
    assert result['basis']['code']=='unclassified_latest_message'


def test_invalid_telegram_id_sibling_cannot_be_given_a_numeric_order():
    result=state([message('11','incoming'),message('unclassified','outgoing',body='')],platform='telegram')
    assert result['last_turn']=='unknown'
    assert result['suggested_action']=='unclear'


def test_media_only_is_meaningful_and_its_original_body_is_preserved():
    result=state([message('voice','outgoing',body='',kind='voice',
        display_text='[Голосовое; файл загружается]')])
    assert result['last_turn']=='owner'
    assert result['latest_known_meaningful_message']['body']==''
    assert result['latest_known_meaningful_message']['display_text']=='[Голосовое; файл загружается]'


def test_head_summary_marks_truncation_and_keeps_full_message_reference():
    original=message('large',body='x'*5000,display_text='x'*5000)
    head=state([original])['latest_known_meaningful_message']
    assert head['id']=='large'
    assert len(head['body'])<=1000
    assert head['body_truncated'] is True
    assert head['display_text_truncated'] is True
    assert original['body']=='x'*5000


def test_channel_and_saved_messages_do_not_claim_somebody_owes_a_reply():
    assert state([message('news')],chat_type='channel')['suggested_action']=='not_applicable'
    assert state([message('saved','outgoing')],self_chat=True)['last_turn']=='not_applicable'


def test_group_unrelated_chatter_keeps_explicit_owner_attention():
    target=message('mention',second=10)
    result=state([message('noise')],chat_type='group',attention=[target])
    assert result['last_turn']=='not_applicable'
    assert result['owner_attention']['last_turn']=='peer'
    assert result['owner_attention']['target_message']['id']=='mention'
    assert result['suggested_action']=='review_incoming'
    assert result['basis']['heuristic'] is True


def test_ambiguous_attention_reply_is_not_reported_as_resolved():
    target=message('mention',second=10,attention_status='ambiguous')
    result=state([message('noise')],chat_type='group',attention=[target])
    assert result['suggested_action']=='unclear'
    assert result['basis']['code']=='ambiguous_attention_reply_order'


def test_empty_head_explicitly_says_no_archived_evidence():
    result=state([])
    assert result['last_turn']=='unknown'
    assert result['suggested_action']=='unclear'
    assert result['basis']['code']=='no_archived_messages'
    assert result['latest_known_meaningful_message'] is None


@pytest.mark.parametrize('evidence,code',[
    ({'active_conflicts':1},'message_version_conflict'),
    ({'revision_changed_during_read':True},'message_revision_changed_during_read'),
])
def test_unstable_head_revision_cannot_recommend_an_assured_next_turn(evidence,code):
    result=state([message('out','outgoing',evidence=evidence)])
    assert result['suggested_action']=='unclear'
    assert result['last_turn']=='unknown'
    assert result['basis']['code']==code


@pytest.fixture()
def dialogue_archive(seeded):
    from messaging_service.archive import Archive
    from messaging_service.envelope import stable_id
    from test_archive import event
    s,_,_=seeded; ar=Archive(s)
    for account,platform in [('dialogue-wa','whatsapp'),('dialogue-tg','telegram')]:
        ar.register(account,platform,'Synthetic dialogue',discover=True)
    key=s.issue_key('dialogue-agent',['dialogue-wa','dialogue-tg'])
    anchor=datetime.now(timezone.utc).replace(microsecond=0)-timedelta(seconds=60)
    def add(mid,direction='incoming',*,account='dialogue-wa',chat='123@g.us',body='Synthetic text',
            second=0,chat_type='private',metadata=None):
        at=anchor+timedelta(seconds=second)
        ar.ingest(event(account=account,platform='telegram' if account=='dialogue-tg' else 'whatsapp',
            chat=chat,provider_id=mid,sent_at=at.isoformat(),updated_at=at.isoformat(),
            direction=direction,body=body,metadata={'chat_type':chat_type,**(metadata or {})}))
        cid=stable_id('chat',account,chat)
        with s.pool.connection() as c:
            c.execute('''INSERT INTO collector_chats(account,provider_chat,peer_kind,initialized,last_head_scan_at)
                VALUES(%s,%s,'user',true,clock_timestamp()) ON CONFLICT(account,provider_chat)
                DO UPDATE SET last_head_scan_at=clock_timestamp()''',(account,chat))
        return cid
    def query(account,chat):
        from messaging_service.conversation_state import conversation_state
        with s.authorized(key,'list_chats') as (c,p):
            return conversation_state(c,p,account,chat)
    def catalog(**changes):
        from messaging_service.chat_queries import list_chats
        with s.authorized(key,'list_chats') as (c,p):
            return list_chats(s,c,p,view='conversation',include_coverage=False,**changes)
    return s,key,add,query,catalog


def test_scoped_archive_head_reads_actual_newest_not_middle_page(dialogue_archive):
    s,key,add,query,catalog=dialogue_archive
    chat=add('old','incoming',second=-10)
    add('latest','outgoing',second=0)
    result=query('dialogue-wa',chat)
    assert result['last_turn']=='owner'
    assert result['latest_known_meaningful_message']['provider_message_id']=='latest'
    row=catalog()['items'][0]
    assert row['conversation']['latest_known_meaningful_message']['provider_message_id']=='latest'


def test_scoped_whatsapp_tie_is_unknown_and_candidate_reply_queue_uncertain(dialogue_archive):
    s,key,add,query,catalog=dialogue_archive
    chat=add('Z','incoming')
    add('A','outgoing')
    result=query('dialogue-wa',chat)
    assert result['last_turn']=='unknown'
    row=catalog(awaiting_reply=True)['items'][0]
    assert row['conversation']['suggested_action']=='unclear'
    assert row['awaiting_reply_confidence']=='ambiguous_message_order'
    assert row['last_message_id'] is None
    assert row['last_message_direction'] is None


def test_conversation_catalog_ignores_newer_known_service_as_last_message(dialogue_archive):
    s,key,add,query,catalog=dialogue_archive
    add('real','incoming')
    add('timer','outgoing',second=1,body='',metadata={'is_service':True})
    row=catalog()['items'][0]
    assert row['conversation']['latest_known_meaningful_message']['provider_message_id']=='real'
    assert row['last_message_direction']=='incoming'


def test_conversation_group_attention_is_not_closed_by_unrelated_owner_post(dialogue_archive):
    s,key,add,query,catalog=dialogue_archive
    chat=add('mention','incoming',chat_type='group',metadata={'mentions_owner':True})
    # Owner posts in another chat of this account and in the same provider chat
    # of another account are not messages in this group.
    add('noise','outgoing',second=1,chat='456@g.us',chat_type='group')
    add('7','outgoing',second=1,account='dialogue-tg',chat_type='group')
    result=query('dialogue-wa',chat)
    assert result['owner_attention']['target_message']['provider_message_id']=='mention'
    assert catalog(awaiting_reply=True)['items'][0]['conversation']['suggested_action']=='review_incoming'


def test_conversation_explicit_group_reply_closes_only_its_thread(dialogue_archive):
    s,key,add,query,catalog=dialogue_archive
    chat=add('thread-a','incoming',chat_type='group',metadata={'mentions_owner':True})
    # thread-b arrives after answer-a, so it is open by the owner-message time
    # rule (ANYM-277); answer-b, a later owner message, then closes it.
    add('answer-a','outgoing',second=1,chat_type='group',metadata={'reply_to_provider_id':'thread-a'})
    add('thread-b','incoming',second=2,chat_type='group',metadata={'mentions_owner':True})
    result=query('dialogue-wa',chat)
    assert result['owner_attention']['target_message']['provider_message_id']=='thread-b'
    add('answer-b','outgoing',second=3,chat_type='group',metadata={'reply_to_provider_id':'thread-b'})
    assert query('dialogue-wa',chat)['owner_attention']['target_message'] is None
    assert catalog(awaiting_reply=True)['items']==[]


def test_group_dismissal_is_not_reopened_by_unrelated_incoming_chatter(dialogue_archive):
    s,key,add,query,catalog=dialogue_archive
    chat=add('mention','incoming',chat_type='group',metadata={'mentions_owner':True})
    row=catalog(awaiting_reply=True)['items'][0]
    s.dismiss_reply(key,chat,row['awaiting_reply_message_id'],row['awaiting_reply_revision'])
    assert catalog(awaiting_reply=True)['items']==[]
    add('noise','incoming',second=1,chat_type='group')
    assert catalog(awaiting_reply=True)['items']==[]
    assert s.list_chats(key,awaiting_reply=True,include_coverage=False)['items']==[]


def test_group_attention_can_be_dismissed_after_unrelated_owner_post(dialogue_archive):
    s,key,add,query,catalog=dialogue_archive
    chat=add('mention','incoming',chat_type='group',metadata={'mentions_owner':True})
    add('unrelated-owner-post','outgoing',second=1,chat='456@g.us',chat_type='group')
    row=catalog(awaiting_reply=True)['items'][0]
    result=s.dismiss_reply(key,chat,row['awaiting_reply_message_id'],row['awaiting_reply_revision'])
    assert result['dismissed'] is True
    assert catalog(awaiting_reply=True)['items']==[]


def test_conversation_group_attention_clears_after_any_owner_message(dialogue_archive):
    from messaging_service.store import APIError
    s,key,add,query,catalog=dialogue_archive
    chat=add('mention','incoming',chat_type='group',metadata={'mentions_owner':True})
    row=catalog(awaiting_reply=True)['items'][0]
    add('ok-will-take-it','outgoing',second=1,chat_type='group',body='ок, заберу')
    assert catalog(awaiting_reply=True)['items']==[]
    assert query('dialogue-wa',chat)['owner_attention']['target_message'] is None
    with pytest.raises(APIError,match='reply_target_changed'):
        s.dismiss_reply(key,chat,row['awaiting_reply_message_id'],row['awaiting_reply_revision'])


def test_conversation_group_new_mention_after_owner_message_reopens(dialogue_archive):
    s,key,add,query,catalog=dialogue_archive
    chat=add('mention-1','incoming',chat_type='group',metadata={'mentions_owner':True})
    add('owner','outgoing',second=1,chat_type='group')
    add('mention-3','incoming',second=3,chat_type='group',metadata={'reply_to_owner':True})
    row=catalog(awaiting_reply=True)['items'][0]
    assert row['awaiting_reply_provider_message_id']=='mention-3'
    # Only attention after the owner message is counted.
    assert row['awaiting_reply_incoming_count']==1
    assert query('dialogue-wa',chat)['owner_attention']['target_message']['provider_message_id']=='mention-3'


def test_conversation_group_owner_message_same_second_ordering(dialogue_archive):
    s,key,add,query,catalog=dialogue_archive
    # Telegram orders one second by numeric provider ID.
    answered=add('10','incoming',account='dialogue-tg',chat='-1001',chat_type='group',metadata={'mentions_owner':True})
    add('11','outgoing',account='dialogue-tg',chat='-1001',chat_type='group')
    open_tg=add('21','incoming',account='dialogue-tg',chat='-1002',chat_type='group',metadata={'mentions_owner':True})
    add('20','outgoing',account='dialogue-tg',chat='-1002',chat_type='group')
    # WhatsApp has no order within a second: the mention stays a candidate.
    tied=add('mention','incoming',chat_type='group',metadata={'mentions_owner':True})
    add('owner','outgoing',chat_type='group')
    rows={r['id']:r for r in catalog(awaiting_reply=True)['items']}
    assert set(rows)=={open_tg,tied}
    assert query('dialogue-tg',answered)['owner_attention']['target_message'] is None
    assert rows[open_tg]['awaiting_reply_provider_message_id']=='21'
    assert rows[open_tg]['awaiting_reply_confidence']=='heuristic_not_obligation'
    assert rows[tied]['awaiting_reply_provider_message_id']=='mention'
    # The archive row states the tie; conversation state qualifies it further.
    archive_row=next(r for r in s.list_chats(key,awaiting_reply=True,include_coverage=False)['items'] if r['id']==tied)
    assert archive_row['awaiting_reply_confidence']=='ambiguous_message_order'
    assert rows[tied]['awaiting_reply_confidence']=='ambiguous_attention_reply_order'
    state=query('dialogue-wa',tied)
    assert state['owner_attention']['target_message']['provider_message_id']=='mention'
    assert state['basis']['code']=='ambiguous_attention_reply_order'


def test_conversation_group_chatter_still_does_not_clear(dialogue_archive):
    s,key,add,query,catalog=dialogue_archive
    chat=add('mention','incoming',chat_type='group',metadata={'mentions_owner':True})
    first=catalog(awaiting_reply=True)['items'][0]
    add('chatter-1','incoming',second=1,chat_type='group')
    add('chatter-2','incoming',second=2,chat_type='group',metadata={'reply_to_provider_id':'chatter-1'})
    row=catalog(awaiting_reply=True)['items'][0]
    assert row['awaiting_reply_provider_message_id']=='mention'
    assert row['awaiting_reply_sent_at']==first['awaiting_reply_sent_at']
    assert row['awaiting_reply_incoming_count']==1
    assert query('dialogue-wa',chat)['owner_attention']['target_message']['provider_message_id']=='mention'


def test_conversation_group_service_empty_or_deleted_owner_message_does_not_clear(dialogue_archive):
    s,key,add,query,catalog=dialogue_archive
    chat=add('mention','incoming',chat_type='group',metadata={'mentions_owner':True})
    add('timer','outgoing',second=1,chat_type='group',body='Synthetic service notice',metadata={'is_service':True})
    add('empty','outgoing',second=2,chat_type='group',body='')
    add('removed','outgoing',second=3,chat_type='group')
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET deleted=true WHERE account='dialogue-wa' AND provider_id='removed'")
    assert catalog(awaiting_reply=True)['items'][0]['awaiting_reply_provider_message_id']=='mention'
    assert query('dialogue-wa',chat)['owner_attention']['target_message']['provider_message_id']=='mention'


def test_whatsapp_answer_matches_incoming_serialized_id_using_bare_quoted_stanza(dialogue_archive):
    s,key,add,query,catalog=dialogue_archive
    chat=add('false_123@g.us_MENTION_42@lid','incoming',chat_type='group',metadata={'mentions_owner':True})
    add('true_123@g.us_ANSWER_43@lid','outgoing',second=1,chat_type='group',
        metadata={'reply_to_provider_id':'MENTION'})
    assert query('dialogue-wa',chat)['owner_attention']['target_message'] is None
    assert catalog(awaiting_reply=True)['items']==[]


def test_exact_chat_state_obeys_restricted_and_revoked_acl(dialogue_archive):
    from messaging_service.conversation_state import conversation_state
    from messaging_service.store import APIError
    s,key,add,query,catalog=dialogue_archive
    allowed=add('allowed',chat='allowed')
    hidden=add('hidden',chat='hidden')
    scoped=s.issue_key('state-scoped',['dialogue-wa'],chats=[allowed])
    with s.authorized(scoped,'list_chats') as (c,p):
        for account,chat in [('dialogue-wa',hidden),('dialogue-tg',allowed),('dialogue-wa','absent')]:
            with pytest.raises(APIError,match='not_found'):
                conversation_state(c,p,account,chat)
    s.set_access('state-scoped',chats=[])
    with s.authorized(scoped,'list_chats') as (c,p):
        with pytest.raises(APIError,match='not_found'):
            conversation_state(c,p,'dialogue-wa',allowed)


def contradicted_head(s, add, *changes):
    """A Telegram private head with one open conflict per change, each at the head's own provider version."""
    from messaging_service.archive import Archive
    from test_archive import event
    chat=add('head','incoming',account='dialogue-tg',chat='616')
    with s.pool.connection() as c:
        row=c.execute('SELECT id,sent_at FROM messages WHERE chat=%s',(chat,)).fetchone()
    at=row['sent_at'].isoformat(); ar=Archive(s)
    for change in changes:
        assert ar.ingest(event(**{'account':'dialogue-tg','chat':'616','provider_id':'head','sent_at':at,'updated_at':at,
            'body':'Synthetic text','metadata':{'chat_type':'private'},**change}))['outcome']=='conflict'
    return chat,row['id']


# spec: ANYM-616
def test_a_known_conflict_class_keeps_the_head_stable_and_names_its_quote_caveat(dialogue_archive):
    from messaging_service.conflict_classes import SHOWN
    from messaging_service.presentation import render
    s,key,add,query,catalog=dialogue_archive
    chat,mid=contradicted_head(s,add,{'body':'Another synthetic text'})
    result=query('dialogue-tg',chat)
    assert result['suggested_action']=='review_incoming' and result['last_turn']=='peer'
    assert result['basis']['code']=='last_archived_message_incoming' and 'next_step' not in result
    known={'class':'telegram_body_equal_version','meaning':SHOWN['telegram_body_equal_version']}
    assert result['basis']['evidence']==[{'message_id':mid,'revision':1,'conflict':known}]
    assert 'archived text is shown' in known['meaning'] and 'provider may show the other' in known['meaning']
    from test_compact_queue_rows import assert_each_fact_once
    rows=render('list_chats',catalog(accounts=['dialogue-tg']),'compact')['items']
    row=next(r for r in rows if r['id']==chat)
    shown='awaiting_reply' if 'awaiting_reply' in row else 'last_message'
    assert row[shown]['message_id' if shown=='awaiting_reply' else 'id']==mid
    assert row['conversation']['known_conflicts']==[{'on':shown,**known}]
    assert_each_fact_once(row)


# spec: ANYM-616
def test_a_known_conflict_beside_an_attention_conflict_is_answered_with_a_caveat(dialogue_archive):
    s,key,add,query,catalog=dialogue_archive
    chat,_=contradicted_head(s,add,{'body':'Another synthetic text'},{'sender':'43'})
    result=query('dialogue-tg',chat)
    assert result['basis']['code']=='message_version_conflict' and result['suggested_action']=='unclear'
    assert result['next_step']=='answer_with_caveat'
    assert not [e for e in result['basis']['evidence'] if 'conflict' in e]


# spec: ANYM-616
def test_a_conflict_without_a_stored_class_is_answered_with_a_caveat(dialogue_archive):
    s,key,add,query,catalog=dialogue_archive
    chat,mid=contradicted_head(s,add,{'body':'Another synthetic text'})
    with s.pool.connection() as c:
        # N-1 code inserts without a class.
        c.execute('UPDATE message_conflicts SET reason=NULL,reason_version=NULL WHERE message=%s',(mid,))
    result=query('dialogue-tg',chat)
    assert result['basis']['code']=='message_version_conflict' and result['next_step']=='answer_with_caveat'


# spec: ANYM-616
def test_a_conflict_on_a_schema_before_stored_classes_is_answered_with_a_caveat(dialogue_archive,monkeypatch):
    from messaging_service import store
    from messaging_service.conflict_classes import CLASS_SCHEMA
    s,key,add,query,catalog=dialogue_archive
    chat,_=contradicted_head(s,add,{'body':'Another synthetic text'})
    monkeypatch.setattr(store,'schema_version',lambda c:CLASS_SCHEMA-1)
    result=query('dialogue-tg',chat)
    assert result['basis']['code']=='message_version_conflict' and result['next_step']=='answer_with_caveat'


# spec: ANYM-616
def test_only_a_revision_changed_during_read_is_reread_now():
    from messaging_service.conversation_state import NEXT_STEP
    assert [code for code,step in NEXT_STEP.items() if step=='reread_now']==['message_revision_changed_during_read']
    known={'active_conflicts':1,'conflict':{'class':'telegram_body_equal_version','next_step':'none_required'}}
    result=state([message('out','outgoing',evidence={**known,'revision_changed_during_read':True})],platform='telegram')
    assert result['basis']['code']=='message_revision_changed_during_read' and result['next_step']=='reread_now'


# spec: ANYM-616
@pytest.mark.parametrize('conflict',[None,'telegram_body_equal_version',{'class':None},{'class':['x']},
    {'class':'unclassified'},{'class':'whatsapp_body_equal_version'}])
def test_any_other_conflict_evidence_is_answered_with_a_caveat(conflict):
    from messaging_service.conflict_classes import KNOWN
    assert not [name for name in KNOWN if not name.startswith('telegram_')]
    result=state([message('out','outgoing',evidence={'active_conflicts':1,'conflict':conflict})])
    assert result['basis']['code']=='message_version_conflict' and result['next_step']=='answer_with_caveat'
    assert 'conflict' not in result['basis']['evidence'][0]


KNOWN_EVIDENCE={'active_conflicts':1,'conflict':{'class':'telegram_body_equal_version','next_step':'none_required'}}


# spec: ANYM-616
def test_a_shown_group_head_keeps_its_known_conflict_caveat_beside_owner_attention():
    from messaging_service.conflict_classes import SHOWN
    from messaging_service.presentation import lean_conversation
    result=state([message('msg_head',evidence=KNOWN_EVIDENCE)],platform='telegram',chat_type='group',
        attention=[message('msg_mention',second=10)])
    assert result['suggested_action']=='review_incoming' and result['basis']['code']=='explicit_owner_attention'
    known={'class':'telegram_body_equal_version','meaning':SHOWN['telegram_body_equal_version']}
    assert result['latest_known_meaningful_message']['conflict']==known
    assert lean_conversation(result)['known_conflicts']==[{'on':'latest_message_id',**known}]


# spec: ANYM-616
def test_a_compact_group_row_states_a_known_conflict_caveat_once():
    from messaging_service.presentation import lean_chat
    from test_compact_queue_rows import assert_each_fact_once
    result=state([message('msg_head',evidence=KNOWN_EVIDENCE)],platform='telegram',chat_type='group',
        attention=[message('msg_mention',second=10)])
    row=lean_chat({'id':'chat_synthetic','account':'synthetic','title':'Synthetic group','type':'group',
        'last_activity':NOW,'conversation':result,'awaiting_reply':True,'awaiting_reply_message_id':'msg_mention',
        'awaiting_reply_sender':'synthetic-person','awaiting_reply_snippet':'Synthetic text'})
    assert row['last_message']['id']=='msg_head' and row['awaiting_reply']['message_id']=='msg_mention'
    assert [k['on'] for k in row['conversation']['known_conflicts']]==['last_message']
    assert_each_fact_once(row)


# spec: ANYM-616
def test_a_shown_meaningful_fallback_keeps_its_known_conflict_caveat():
    from messaging_service.presentation import lean_conversation
    result=state([message('top',body='')],platform='telegram',
        meaningful_fallback=message('older',second=5,evidence=KNOWN_EVIDENCE))
    assert result['basis']['code']=='unclassified_latest_message'
    assert result['latest_known_meaningful_message']['conflict']['class']=='telegram_body_equal_version'
    assert [k['on'] for k in lean_conversation(result,shown=['older'])['known_conflicts']]==['latest_message_id']
