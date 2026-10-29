"""ANYM-557: one honest conflict line per account, a stored class per conflict, no step that cannot act."""
from datetime import datetime,timedelta,timezone
import re
from pathlib import Path

from hypothesis import given,settings,strategies as st
import pytest

from messaging_service.envelope import stable_id
from messaging_service.presentation import render
from messaging_service.store import APIError
from test_archive import archive,event

ROOT=Path(__file__).resolve().parents[1]
OLD_INSERT='''INSERT INTO message_conflicts(receipt_digest,message,observed_revision,
                    candidate_updated_at,observed_at) VALUES(%s,%s,%s,%s,clock_timestamp())'''


def contradict(ar,provider_id,chat='100',**changes):
    mid=ar.ingest(event(provider_id=provider_id,chat=chat))['message']
    assert ar.ingest(event(provider_id=provider_id,chat=chat,**changes))['outcome']=='conflict'
    return mid


def account(s,key):
    return s.get_sync_status(key)['sources'][0]


def stored(s,mid):
    with s.pool.connection() as c:
        return c.execute('SELECT reason,reason_version,resolved_at FROM message_conflicts WHERE message=%s',(mid,)).fetchone()


def steps(value,path=()):
    """Every suggested step in a result, with the path that carries it."""
    if isinstance(value,dict):
        for k,v in value.items():
            if k in ('next_action','next_step') and isinstance(v,str):yield path+(k,),v
            else:yield from steps(v,path+(k,))
    elif isinstance(value,list):
        for item in value:yield from steps(item,path)


def unregistered(step,tools):
    """A clause that starts like a read tool must be one, by name or as its prefix."""
    return [clause for clause in re.split(r'_(?:or|and)_',step)
        if clause.split('_')[0] in ('get','list','search','resolve','request','fetch')
        and not any(clause==t or clause.startswith(t+'_') for t in tools)]


# spec: ANYM-557
def test_account_line_counts_attention_and_known_classes_with_one_step(archive):
    ar,s,key,_=archive
    contradict(ar,'1',body='Другой текст')
    contradict(ar,'2',direction='outgoing')
    row=account(s,key)
    assert row['active_conflicts']==2
    assert row['conflicts']['attention']==0 and row['conflicts']['known']==2
    assert row['conflicts']['next_step']=='none_required'
    assert {c['class'] for c in row['conflicts']['top_classes']}=={'telegram_body_equal_version','telegram_direction_equal_version'}
    assert all(c['count']==1 and c['explanation'] and '\n' not in c['explanation'] for c in row['conflicts']['top_classes'])
    assert 'archive_version_conflicts' not in row['alerts']
    assert row['archive_detail_code']=='version_conflict' and row['archive_detail']['next_action']=='none_required'
    assert s.capabilities(key)['sources'][0]['detail']['next_action']=='none_required'
    compact=render('get_sync_status',s.get_sync_status(key,refresh=True),'compact')['sources'][0]
    assert compact['conflicts']==row['conflicts']
    contradict(ar,'3',sender='43')
    row=s.get_sync_status(key,refresh=True)['sources'][0]
    assert row['conflicts']['attention']==1 and row['conflicts']['known']==2
    assert row['conflicts']['next_step']=='inspect_chat'
    assert 'archive_version_conflicts' in row['alerts'] and row['active_conflicts']==3
    assert row['archive_detail']['next_action']=='inspect_chat'
    assert s.capabilities(key)['sources'][0]['detail']['next_action']=='inspect_chat'


# spec: ANYM-557
def test_compact_status_omits_an_empty_conflict_line(archive):
    ar,s,key,_=archive
    ar.ingest(event())
    full=s.get_sync_status(key)
    assert full['sources'][0]['conflicts']=={'attention':0,'known':0,'top_classes':[],'next_step':'none_required'}
    assert 'conflicts' not in render('get_sync_status',full,'compact')['sources'][0]


# spec: ANYM-557
def test_mixed_classes_attention_outside_the_top_three_still_counts(archive):
    ar,s,key,_=archive
    for n in range(3):contradict(ar,f'b{n}',body=f'Другой {n}')
    for n in range(3):contradict(ar,f'd{n}',direction='outgoing')
    for n in range(2):contradict(ar,f't{n}',sent_at='2026-10-01T11:59:55+00:00')
    contradict(ar,'s0',sender='43')
    conflicts=account(s,key)['conflicts']
    assert [c['class'] for c in conflicts['top_classes']]==['telegram_body_equal_version',
        'telegram_direction_equal_version','telegram_sent_at_equal_version']
    assert conflicts['attention']==3 and conflicts['known']==6 and conflicts['next_step']=='inspect_chat'


# spec: ANYM-557
def test_class_is_stored_at_insert_against_the_observed_revision(archive):
    from messaging_service.conflict_classes import REASON_VERSION
    ar,s,key,_=archive
    mid=contradict(ar,'17',direction='outgoing')
    assert stored(s,mid)['reason']=='telegram_direction_equal_version'
    assert stored(s,mid)['reason_version']==REASON_VERSION
    # The retained revision advances after the conflict; the class still describes what was contradicted.
    assert ar.ingest(event(body='Вторая версия',updated_at='2026-10-01T12:01:00Z'))['outcome']=='edited'
    resolved=stored(s,mid)['resolved_at']
    assert resolved is not None
    assert stored(s,mid)=={'reason':'telegram_direction_equal_version','reason_version':REASON_VERSION,'resolved_at':resolved}


# spec: ANYM-557
def test_relation_is_to_the_version_the_archive_held_after_an_unchanged_reobservation(archive):
    ar,s,key,_=archive
    later='2026-10-01T12:03:00+00:00'
    # An `unchanged` re-read advances the archive's provider version past the revision's snapshot (ANYM-561 s4).
    for provider_id,changes in (('d',{'direction':'outgoing'}),('b',{'body':'Другой текст'})):
        ar.ingest(event(provider_id=provider_id))
        assert ar.ingest(event(provider_id=provider_id,updated_at=later,origin='reconcile'))['outcome']=='unchanged'
        assert ar.ingest(event(provider_id=provider_id,updated_at=later,**changes))['outcome']=='conflict'
    direction,body=(stable_id('msg','archive-test','100',p) for p in ('d','b'))
    assert stored(s,direction)['reason']=='telegram_direction_equal_version'
    assert stored(s,body)['reason']=='telegram_body_equal_version'
    assert stored(s,direction)['reason']=='telegram_direction_equal_version'
    assert stored(s,body)['reason']=='telegram_body_equal_version'
    assert account(s,key)['conflicts']=={**account(s,key)['conflicts'],'attention':0,'known':2,'next_step':'none_required'}


# spec: ANYM-557
def test_rows_without_a_stored_class_read_as_unclassified_attention(archive):
    ar,s,key,_=archive
    good=[contradict(ar,f'g{n}',body=f'Другой {n}') for n in range(3)]
    legacy=contradict(ar,'legacy',body='Другой')
    with s.pool.connection() as c:
        # N-1 code inserts without a class.
        row=c.execute('DELETE FROM message_conflicts WHERE message=%s RETURNING *',(legacy,)).fetchone()
        c.execute(OLD_INSERT,(row['receipt_digest'],row['message'],row['observed_revision'],row['candidate_updated_at']))
    assert all(stored(s,mid)['reason']=='telegram_body_equal_version' for mid in good)
    assert stored(s,legacy)['reason'] is None
    conflicts=account(s,key)['conflicts']
    assert conflicts['attention']==1 and conflicts['known']==3 and conflicts['next_step']=='inspect_chat'
    assert {'class':'unclassified','count':1} in [{k:c[k] for k in ('class','count')} for c in conflicts['top_classes']]


LATER='2026-10-01T12:03:00+00:00'


def reread(ar,provider_id,updated_at=LATER):
    assert ar.ingest(event(provider_id=provider_id,updated_at=updated_at,origin='reconcile'))['outcome']=='unchanged'


def legacy(s,*mids):
    """Rows as written before schema 54 or by N-1 code: no class."""
    with s.pool.connection() as c:
        c.execute('UPDATE message_conflicts SET reason=NULL,reason_version=NULL WHERE message=ANY(%s)',(list(mids),))


def classify_existing(ar,s):
    from messaging_service import conflict_reevaluation as cr
    from test_conflict_reevaluation import run
    assert run(ar,s,[cr.JOBS['classify']])=='done'


def rows(s):
    with s.pool.connection() as c:
        return {r['message']:r for r in c.execute('SELECT mc.*,mc.xmin::text AS xmin FROM message_conflicts mc')}


# spec: ANYM-622
def test_existing_content_rows_are_equal_version_even_after_an_advancing_reread(archive):
    ar,s,key,_=archive
    ar.ingest(event(provider_id='b'))
    reread(ar,'b')  # the snapshot now shows an older version than the archive held at the conflict
    mid=contradict(ar,'b',body='Другой текст',updated_at=LATER)
    legacy(s,mid)
    classify_existing(ar,s)
    assert stored(s,mid)=={'reason':'telegram_body_equal_version','reason_version':1,'resolved_at':None}


# spec: ANYM-622
def test_existing_identity_rows_are_exact_only_when_the_version_provably_held(archive):
    ar,s,key,_=archive
    newer=contradict(ar,'n',direction='outgoing',updated_at='2026-10-01T12:01:00+00:00')
    ar.ingest(event(provider_id='same'))
    reread(ar,'same',updated_at='2026-10-01T12:00:00+00:00')  # a re-read at the same version advances nothing
    same=contradict(ar,'same',sender='43')
    ar.ingest(event(provider_id='adv'))
    reread(ar,'adv')
    advanced=contradict(ar,'adv',direction='outgoing',updated_at=LATER)
    after=contradict(ar,'after',direction='outgoing',updated_at='2026-10-01T12:05:00+00:00')
    reread(ar,'after')  # advances the version after the conflict, below its candidate (stays open): still unprovable
    tombstoned=contradict(ar,'t',sent_at='2026-10-01T11:59:00+00:00')
    with s.pool.connection() as c:
        c.execute("INSERT INTO provider_tombstones(account,provider_chat,provider_id,observed_at) VALUES('archive-test','100','t',%s)",
                  ('2026-10-01T12:05:00+00:00',))
        # Receipt and conflict times interleave within one second; no comparison of them decides a class.
        conflict_at=c.execute('SELECT observed_at FROM message_conflicts WHERE message=%s',(advanced,)).fetchone()['observed_at']
        c.execute('''UPDATE ingest_receipts SET received_at=%s::timestamptz+interval '400 milliseconds'
            WHERE outcome='unchanged' AND envelope->>'provider_id'='adv' ''',(conflict_at,))
    legacy(s,newer,same,advanced,after,tombstoned)
    classify_existing(ar,s)
    assert {mid:stored(s,mid)['reason'] for mid in (newer,same,advanced,after,tombstoned)}=={
        newer:'telegram_direction_newer_version',same:'telegram_sender_equal_version',
        advanced:'telegram_direction_unordered',after:'telegram_direction_unordered',tombstoned:'telegram_sent_at_unordered'}
    conflicts=account(s,key)['conflicts']
    assert conflicts['attention']==5 and conflicts['next_step']=='inspect_chat'


# spec: ANYM-622
def test_classify_existing_touches_only_open_rows_without_a_class_and_reruns_write_nothing(archive):
    ar,s,key,_=archive
    resolved=contradict(ar,'r',direction='outgoing',updated_at='2026-10-01T12:01:00+00:00')
    assert ar.ingest(event(provider_id='r',body='Вторая версия',updated_at='2026-10-01T12:02:00+00:00'))['outcome']=='edited'
    kept=contradict(ar,'k',body='Другой текст')
    unreadable=contradict(ar,'u',body='Другой текст')
    legacy(s,resolved,unreadable)
    with s.pool.connection() as c:
        c.execute("UPDATE message_conflicts SET reason='telegram_body_newer_version' WHERE message=%s",(kept,))
        c.execute("UPDATE message_revisions SET snapshot=snapshot-'sent_at' WHERE message=%s",(unreadable,))
        content=c.execute('SELECT id,body,revision,provider_updated_at FROM messages ORDER BY id').fetchall()
    before=rows(s)
    assert before[resolved]['resolved_at'] is not None
    classify_existing(ar,s)
    after=rows(s)
    assert after[resolved]==before[resolved] and after[kept]==before[kept]
    assert (after[unreadable]['reason'],after[unreadable]['reason_version'])==('unclassified',1)
    assert all(r['resolved_at']==before[m]['resolved_at'] for m,r in after.items())
    classify_existing(ar,s)
    assert rows(s)==after  # same xmin: no row was written again
    late=contradict(ar,'late',body='Ещё текст')  # an N-1 writer after the job finished: the catch-up scan
    legacy(s,late)
    classify_existing(ar,s)
    assert stored(s,late)['reason']=='telegram_body_equal_version'
    with s.pool.connection() as c:
        assert c.execute('SELECT id,body,revision,provider_updated_at FROM messages WHERE id<>%s ORDER BY id',
                         (late,)).fetchall()==content


def at(minutes):
    return datetime(2026,10,1,12,0,tzinfo=timezone.utc)+timedelta(minutes=minutes)


SNAPSHOT={**event(),'message':'m','provider_chat':'100','chat':'chat-id','revision':1}
CHANGES={'sent_at':'2026-10-01T11:59:00+00:00','direction':'outgoing','sender':'43','body':'Другой текст',
         'media':[{'provider_id':'p1','filename':'a.jpg','mime':'image/jpeg','bytes':10}],'other':None}  # other: the text with its formatting known


# spec: ANYM-622
@settings(deadline=None)
@given(st.sampled_from(sorted(CHANGES)),st.integers(0,3),st.integers(-3,3),st.sampled_from([False,True,None]))
def test_reclassify_is_equal_version_for_content_and_exact_or_unordered_for_identity(field,candidate,archived,tombstoned):
    from messaging_service.conflict_classes import IDENTITY,classify,reclassify
    from messaging_service.incoming_formatting import telegram
    change={'formatting':telegram(SNAPSHOT['body'],[])} if field=='other' else {field:CHANGES[field]}
    envelope=event(**change,updated_at=at(candidate).isoformat())
    archive_version=at(archived) if archived>=0 else None
    name=reclassify({'envelope':envelope,'observed':SNAPSHOT,'archive_version':archive_version,'tombstoned':tombstoned})
    if field not in IDENTITY: assert name==f'telegram_{field}_equal_version'
    elif archived==0 and tombstoned is False: assert name==classify(SNAPSHOT,envelope)!='unclassified'
    else: assert name==f'telegram_{field}_unordered'
    assert reclassify({'envelope':None,'observed':SNAPSHOT,'archive_version':None,'tombstoned':False})=='unclassified'


# spec: ANYM-557
@pytest.mark.parametrize('candidate,retained',[
    ({'direction':'sideways','sender':'43'},{}),
    ({'sent_at':'2026-10-01T12:00:00'},{}),
    ({'media':'not-a-list'},{}),
    ({},{'direction':'sideways'}),
    ({},{'provider_chat':None}),
    ({'platform':'whatsapp'},{}),
],ids=['candidate-direction','naive-time','media-type','retained-direction','retained-chat','platform-mix'])
def test_malformed_evidence_with_present_fields_is_never_a_known_class(candidate,retained):
    from messaging_service.conflict_classes import classify
    base=event(body='Другой текст')
    snapshot={**event(),'message':'m','provider_chat':'100','chat':'chat-id','revision':1}
    assert classify(snapshot,base)=='telegram_body_equal_version'
    assert classify({**snapshot,**retained},{**base,**candidate})=='unclassified'


# spec: ANYM-557
def test_migration_numbers_have_no_gap_so_a_later_lower_number_cannot_follow_an_applied_one():
    from messaging_service.store import RUNTIME_SCHEMA
    source=(ROOT/'services/messaging/messaging_service/store.py').read_text()
    listed=[int(n) for n in re.findall(r"\((\d+),'\d{3}_[a-z0-9_]+\.sql'\)",source)]
    files=sorted(int(p.name[:3]) for p in (ROOT/'services/messaging/messaging_service/migrations').glob('[0-9][0-9][0-9]_*.sql'))
    # migrate() treats every number at or below the ledger as applied (Astra r1 F3): 054 ships after 053 exists.
    assert listed==files==list(range(2,RUNTIME_SCHEMA+1))


# spec: ANYM-557
def test_chat_listing_is_bounded_and_hidden_chats_stay_out_of_the_aggregates(archive):
    ar,s,key,_=archive
    with s.pool.connection() as c:
        c.execute("UPDATE account_policies SET include_chats=ARRAY['100','200'] WHERE account='archive-test'")
    for n in range(21):contradict(ar,f'm{n}',sender='43')
    hidden=contradict(ar,'h1',chat='200',sender='43')
    first=stable_id('chat','archive-test','100');second=stable_id('chat','archive-test','200')
    chat=s.get_sync_status(key,chat_id=first)
    assert chat['conflicts']['active']==21 and chat['conflicts']['attention']==21
    assert chat['next_action']=='inspect_chat' and chat['conflicts']['next_step']=='inspect_chat'
    items=chat['conflicts']['items']
    assert len(items)==20
    assert all(set(i)=={'message_id','class','explanation','observed_at'} for i in items)
    assert {i['class'] for i in items}=={'telegram_sender_equal_version'}
    assert 'Первый текст' not in str(chat) and hidden not in str(chat)
    default=s.get_sync_status(key,refresh=True)
    assert 'items' not in default['sources'][0]['conflicts'] and default['sources'][0]['conflicts']['attention']==22
    # A key of one chat sees neither the account line nor the other chat.
    narrow=s.issue_key('one-chat-reader',['archive-test'],chats=[second])
    source=s.get_sync_status(narrow)['sources'][0]
    assert not {'conflicts','active_conflicts','historical_conflicts','alerts'}&set(source)
    own=s.get_sync_status(narrow,chat_id=second)
    assert own['conflicts']['active']==1 and [i['message_id'] for i in own['conflicts']['items']]==[hidden]
    with pytest.raises(APIError,match='not_found'):s.get_sync_status(narrow,chat_id=first)


# spec: ANYM-557
def test_conflict_results_suggest_only_registered_tools(archive):
    from messaging_service.status_details import DETAILS,describe
    ar,s,key,_=archive
    attention=contradict(ar,'1',sender='43')
    known=contradict(ar,'2',body='Другой текст')
    tools=s.capabilities(key)['tool_manifest']['names']
    cid=stable_id('chat','archive-test','100')
    results={'status':s.get_sync_status(key),'chat':s.get_sync_status(key,chat_id=cid),
        'search':s.search(key),'context':s.get_context(key,attention),'known':s.get_context(key,known),
        'capabilities':s.capabilities(key)}
    results['compact']=render('get_sync_status',results['status'],'compact')
    results['search_compact']=render('search',results['search'],'compact')
    for name,result in results.items():
        for path,step in steps(result):
            assert not unregistered(step,tools),(name,path,step)
    assert not [code for code,(_,step) in DETAILS.items() if unregistered(step,tools)]
    assert describe('version_conflict')['next_action']=='inspect_chat'
    message=results['context']['message']['evidence']
    assert message['conflict']=={'class':'telegram_sender_equal_version','next_step':'inspect_chat'}
    assert message['next_action']=='inspect_chat'
    calm=results['known']['message']['evidence']
    assert calm['conflict']=={'class':'telegram_body_equal_version','next_step':'none_required'}
    assert calm['next_action']=='read_context'
    rows={r['id']:r for r in results['search']['items']}
    assert rows[attention]['evidence']['conflict']['next_step']=='inspect_chat'
    assert results['search']['result_evidence']['next_action']=='inspect_chat'
    summary={r['id']:r for r in results['search_compact']['items']}[attention]
    assert summary['integrity']['conflict']=={'class':'telegram_sender_equal_version','next_step':'inspect_chat'}
    for result in results.values():
        assert 'inspect_chat_coverage_and_request_reconciliation' not in str(result)
        assert not [step for path,step in steps(result) if step=='get_context' and 'conflict' in str(path)]


# spec: ANYM-557
def test_collector_states_documents_conflict_keys_and_every_class():
    from messaging_service.conflict_classes import EXPLANATIONS,FIELDS,KNOWN,RELATIONS,UNCLASSIFIED
    text=(ROOT/'COLLECTOR_STATES.md').read_text()
    for word in ('attention','known','top_classes','next_step','none_required','inspect_chat','reason_version',
            UNCLASSIFIED,*FIELDS,*RELATIONS,*KNOWN):
        assert f'`{word}`' in text,word
    assert set(KNOWN)<set(EXPLANATIONS) and UNCLASSIFIED in EXPLANATIONS
    assert all(line and '\n' not in line for line in EXPLANATIONS.values())
