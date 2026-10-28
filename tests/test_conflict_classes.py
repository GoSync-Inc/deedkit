"""ANYM-557: one honest conflict line per account, a stored class per conflict, no step that cannot act."""
import re
from pathlib import Path

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
    from messaging_service.conflict_classes import REASON_VERSION,backfill
    ar,s,key,_=archive
    mid=contradict(ar,'17',direction='outgoing')
    assert stored(s,mid)['reason']=='telegram_direction_equal_version'
    assert stored(s,mid)['reason_version']==REASON_VERSION
    # The retained revision advances after the conflict; the class still describes what was contradicted.
    assert ar.ingest(event(body='Вторая версия',updated_at='2026-10-01T12:01:00Z'))['outcome']=='edited'
    resolved=stored(s,mid)['resolved_at']
    assert resolved is not None
    with s.pool.connection() as c:
        c.execute('UPDATE message_conflicts SET reason=NULL,reason_version=NULL WHERE message=%s',(mid,))
    assert backfill(s.pool)==1
    assert stored(s,mid)=={'reason':'telegram_direction_equal_version','reason_version':REASON_VERSION,'resolved_at':resolved}


# spec: ANYM-557
def test_relation_is_to_the_version_the_archive_held_after_an_unchanged_reobservation(archive):
    from messaging_service.conflict_classes import backfill
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
    with s.pool.connection() as c:
        c.execute('UPDATE message_conflicts SET reason=NULL,reason_version=NULL')
    assert backfill(s.pool)==2
    assert stored(s,direction)['reason']=='telegram_direction_equal_version'
    assert stored(s,body)['reason']=='telegram_body_equal_version'
    assert account(s,key)['conflicts']=={**account(s,key)['conflicts'],'attention':0,'known':2,'next_step':'none_required'}


# spec: ANYM-557
def test_backfill_is_bounded_idempotent_and_reads_malformed_or_n1_rows_as_unclassified(archive):
    from messaging_service.conflict_classes import backfill
    ar,s,key,_=archive
    good=[contradict(ar,f'g{n}',body=f'Другой {n}') for n in range(3)]
    bad=contradict(ar,'bad',body='Другой')
    legacy=contradict(ar,'legacy',body='Другой')
    with s.pool.connection() as c:
        c.execute('UPDATE message_conflicts SET reason=NULL,reason_version=NULL')
        # Malformed legacy evidence: a receipt without the fields the class needs.
        c.execute('''UPDATE ingest_receipts SET envelope='{"platform":"telegram"}'::jsonb
            WHERE digest=(SELECT receipt_digest FROM message_conflicts WHERE message=%s)''',(bad,))
    assert backfill(s.pool,batch=2)==5
    with s.pool.connection() as c:
        first=c.execute('SELECT receipt_digest,reason,reason_version,resolved_at FROM message_conflicts ORDER BY 1').fetchall()
    assert backfill(s.pool,batch=2)==0
    with s.pool.connection() as c:
        assert c.execute('SELECT receipt_digest,reason,reason_version,resolved_at FROM message_conflicts ORDER BY 1').fetchall()==first
        # N-1 code inserts without a class after the backfill.
        row=c.execute('DELETE FROM message_conflicts WHERE message=%s RETURNING *',(legacy,)).fetchone()
        c.execute(OLD_INSERT,(row['receipt_digest'],row['message'],row['observed_revision'],row['candidate_updated_at']))
    assert all(stored(s,mid)['reason']=='telegram_body_equal_version' for mid in good)
    assert stored(s,bad)['reason']=='unclassified' and stored(s,legacy)['reason'] is None
    assert all(r['resolved_at'] is None for r in first)
    conflicts=account(s,key)['conflicts']
    assert conflicts['attention']==2 and conflicts['known']==3 and conflicts['next_step']=='inspect_chat'
    assert {'class':'unclassified','count':2} in [{k:c[k] for k in ('class','count')} for c in conflicts['top_classes']]


# spec: ANYM-557
def test_backfill_commits_each_batch_so_ingest_and_resolution_never_wait_for_it(archive):
    from messaging_service.conflict_classes import backfill
    ar,s,key,_=archive
    mids=[contradict(ar,f'c{n}',body=f'Другой {n}') for n in range(3)]
    with s.pool.connection() as c:
        c.execute('UPDATE message_conflicts SET reason=NULL,reason_version=NULL')
    seen=[]
    def between():
        # A collector on its own connection, with a lock wait far below the batch bounds: a new conflict and a
        # resolving edit of an already classified row both go through while the backfill is between batches.
        with s.pool.connection() as c:
            c.execute("SET LOCAL lock_timeout='200ms'")
            seen.append(c.execute('SELECT count(*) AS n FROM message_conflicts WHERE reason IS NOT NULL').fetchone()['n'])
        if len(seen)==1:
            contradict(ar,'during',body='Другой')
            assert ar.ingest(event(provider_id='c0',body='Новая версия',updated_at='2026-10-01T12:05:00Z'))['outcome']=='edited'
    assert backfill(s.pool,batch=1,after_batch=between)==3
    # Each batch was committed when the next began; the conflict ingested in between was classified at insert.
    assert seen==[1,3,4]
    assert stored(s,mids[0])['resolved_at'] is not None and stored(s,mids[0])['reason']=='telegram_body_equal_version'
    assert stored(s,stable_id('msg','archive-test','100','during'))['reason']=='telegram_body_equal_version'


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
