"""Receipt evidence and recovery use synthetic captures and isolated PostgreSQL."""
from copy import deepcopy
from pathlib import Path

import pytest

from messaging_service.archive import Archive
from messaging_service.collector_state import CollectorState, CaptureSpool, validate_capture
from messaging_service.envelope import stable_id
from messaging_service.receipts import RANGE_SQL,ROW_PROOFS_SQL,REFERENCE_TG_SQL
from test_archive import event


def capture(platform='telegram', **changes):
    value={'kind':'message_receipt','account':'receipt-test','platform':platform,
        'chat':'100' if platform=='telegram' else '100@c.us',
        'provider_id':None if platform=='telegram' else 'wa-17',
        'max_id':17 if platform=='telegram' else None,'state':'read',
        'basis':'telegram_outbox_boundary' if platform=='telegram' else 'whatsapp_message_ack',
        'audience':'private_peer','observed_at':'2026-10-14T08:00:00Z','provider_at':None}
    value.update(changes)
    if platform=='whatsapp' and 'provider_id' not in changes:
        value['provider_id']='true_'+value['chat']+'_wa-17'
    return value


def test_receipt_capture_is_strict_and_spooled(tmp_path):
    expected={**capture(),'observed_at':'2026-10-14T08:00:00+00:00'}
    assert validate_capture(capture())==expected
    spool=CaptureSpool(tmp_path/'captures.jsonl')
    spool.put(capture());assert len(list(spool.path.glob('*.json')))==1
    assert spool.drain(lambda item: item==expected)==1


@pytest.mark.parametrize('state',['pending','error','server','delivered','read','played'])
def test_generic_receipt_observation_time_remains_distinct_from_provider_time(state):
    from messaging_service.receipts import _evidence,unknown_evidence
    proof=_evidence([{'state':state,'basis':'whatsapp_message_ack','audience':'private_peer',
        'observed_at':'2026-10-14T08:00:00+00:00'}])
    assert proof['observed_at']=='2026-10-14T08:00:00+00:00'
    assert proof['provider_at'] is None
    assert unknown_evidence()['observed_at'] is None


@pytest.mark.parametrize('changes',[
    {'unexpected':1},{'account':'bad account'},{'chat':'-100'},{'chat':True},
    {'max_id':True},{'max_id':0},{'max_id':2**63},{'provider_id':'17'},
    {'state':'delivered'},{'basis':'whatsapp_message_ack'},{'audience':'provider_aggregate'},
    {'provider_at':'2026-10-14T08:00:00Z'},{'observed_at':'2026-10-14T08:00:00'},
    {'observed_at':False},{'observed_at':'invalid'},
    {'state':[]},{'platform':{}},
])
def test_invalid_telegram_receipts_rejected(changes):
    with pytest.raises(ValueError):validate_capture(capture(**changes))


# spec: ANYM-868
@pytest.mark.parametrize(('chat','audience','valid'),[
    ('-100','provider_aggregate',True),('-1000000000100','provider_aggregate',True),
    ('-9223372036854775807','provider_aggregate',True),('-9223372036854775808','provider_aggregate',False),
    ('-0','provider_aggregate',False),('-012','provider_aggregate',False),('--1','provider_aggregate',False),
    ('-100','private_peer',False),('100','provider_aggregate',False),('100','private_peer',True),
],ids=['basic-group','supergroup','largest','too-large','zero','leading-zero','double-sign','private-audience',
       'positive-aggregate','private'])
def test_telegram_group_receipts_take_a_marked_chat_id_with_the_aggregate_audience_only(chat,audience,valid):
    if valid:assert validate_capture(capture(chat=chat,audience=audience))['chat']==chat
    else:
        with pytest.raises(ValueError):validate_capture(capture(chat=chat,audience=audience))


@pytest.mark.parametrize('changes',[
    {'chat':'100@g.us'},{'chat':'100@evil'},{'provider_id':True},{'provider_id':''},
    {'provider_id':'a\nsecret'},{'provider_id':'x'*257},{'max_id':17},
    {'provider_id':'wa-17'},{'provider_id':'false_100@c.us_wa-17'},
    {'provider_id':'true_200@c.us_wa-17'},{'provider_id':'true_100@c.us_'},
    {'basis':'telegram_outbox_boundary'},{'state':'sent'},{'audience':'all_members'},
    {'state':[]},{'state':True},
])
def test_invalid_whatsapp_receipts_rejected(changes):
    with pytest.raises(ValueError):validate_capture(capture('whatsapp',**changes))


@pytest.fixture
def receipt_state(seeded):
    store,_,_=seeded;archive=Archive(store)
    archive.register('receipt-test','telegram','Synthetic receipts',discover=True)
    epoch=archive.acquire('receipt-test','receipt-worker')
    state=CollectorState(archive,'receipt-test',('receipt-worker',epoch))
    yield store,archive,state
    archive.release('receipt-test','receipt-worker',epoch)


def evidence(store, rows):
    from messaging_service.receipts import for_rows
    with store.pool.connection() as c:return for_rows(c,rows)


def outgoing(archive, mid=17, **changes):
    return archive.ingest(event(account='receipt-test',provider_id=str(mid),direction='outgoing',**changes))['message']


def statuses(store):
    with store.pool.connection() as c:
        return c.execute("SELECT * FROM events WHERE kind='message.status' ORDER BY id").fetchall()


def apply_and_publish(state,value):
    result=state.apply(value);state.publish_receipts();return result


def test_receipt_before_message_is_durable_without_content_revision(receipt_state):
    store,archive,state=receipt_state
    state.apply(capture())
    assert statuses(store)==[]
    mid=outgoing(archive)
    result=evidence(store,[{'id':mid}])[mid]
    assert result['state']=='read' and result['recipient_delivery_confirmed'] is True
    assert result['read_at'] is None and result['delivered_at'] is None
    assert result['read_observed_at']=='2026-10-14T08:00:00+00:00'
    with store.pool.connection() as c:
        assert c.execute('SELECT revision FROM messages WHERE id=%s',(mid,)).fetchone()['revision']==1
        assert c.execute('SELECT count(*) AS n FROM message_revisions WHERE message=%s',(mid,)).fetchone()['n']==1


def test_publication_failure_replays_durable_job_without_double_event(receipt_state,tmp_path,monkeypatch):
    store,archive,state=receipt_state;mid=outgoing(archive)
    spool=CaptureSpool(tmp_path/'receipts');spool.put(capture())
    original=state.publish_receipts
    def interrupted(*args,**kwargs):raise RuntimeError('synthetic_publication_interrupted')
    monkeypatch.setattr(state,'publish_receipts',interrupted)
    assert spool.drain(state.apply)==1
    with pytest.raises(RuntimeError,match='synthetic_publication_interrupted'):state.publish_receipts()
    assert len(list(spool.path.glob('*.json')))==0
    assert evidence(store,[{'id':mid}])[mid]['state']=='read' and statuses(store)==[]
    monkeypatch.setattr(state,'publish_receipts',original)
    assert state.publish_receipts()==1 and len(statuses(store))==1
    assert spool.drain(state.apply)==0 and len(statuses(store))==1


def test_duplicates_and_old_boundaries_do_not_publish_again(receipt_state):
    store,archive,state=receipt_state;mid=outgoing(archive)
    apply_and_publish(state,capture())
    assert len(statuses(store))==1
    frozen=deepcopy(statuses(store)[0]['snapshot'])
    state.apply(capture(observed_at='2026-10-14T09:00:00Z'))
    state.apply(capture(max_id=10))
    state.publish_receipts()
    assert len(statuses(store))==1
    assert statuses(store)[0]['snapshot']==frozen
    assert frozen['delivery_receipt']['state']=='read' and frozen['message']==mid
    with store.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM message_receipts').fetchone()['n']==2


def test_publication_is_bounded_and_resumes_after_commit(receipt_state):
    store,archive,state=receipt_state
    for i in range(1,205):outgoing(archive,i)
    state.apply(capture(max_id=204));assert statuses(store)==[]
    assert state.publish_receipts()==100
    assert len(statuses(store))==100
    assert state.publish_receipts()==100
    assert state.publish_receipts()==4
    assert state.publish_receipts()==0
    assert len(statuses(store))==204
    with store.pool.connection() as c:
        assert c.execute('SELECT max(revision) AS revision FROM messages WHERE account=%s',('receipt-test',)).fetchone()['revision']==1


def test_large_range_publication_uses_ordered_index_and_limits_work(receipt_state):
    store,archive,state=receipt_state;outgoing(archive,1)
    chat=stable_id('chat','receipt-test','100')
    with store.pool.connection() as c:
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'synthetic-receipt-'||n,'receipt-test',%s,n::text,'42','Synthetic performance fixture',
                '2026-10-01T12:00:00Z'::timestamptz,'outgoing' FROM generate_series(2,5000) AS n''',(chat,))
        c.execute('ANALYZE messages')
        plan=c.execute('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+RANGE_SQL,
            ('receipt-test',chat,0,5000,100)).fetchone()['QUERY PLAN'][0]['Plan']
    def nodes(value):
        yield value
        for child in value.get('Plans',[]):yield from nodes(child)
    walked=list(nodes(plan))
    assert plan['Actual Rows']==100
    assert any(node.get('Index Name')=='message_receipt_range' for node in walked)
    assert not any(node['Node Type'] in ('Sort','Seq Scan') for node in walked)
    assert max(node['Actual Rows'] for node in walked)<=100
    state.apply(capture(max_id=5000));assert statuses(store)==[]
    assert state.publish_receipts()==100 and len(statuses(store))==100
    with store.pool.connection() as c:
        assert c.execute('SELECT complete,cursor_id FROM receipt_event_jobs').fetchone()=={'complete':False,'cursor_id':100}


def test_many_telegram_boundaries_use_one_indexed_proof_per_message(receipt_state):
    from messaging_service.receipts import for_reference
    store,archive,_=receipt_state
    mids=[outgoing(archive,n) for n in range(3501,3601)]
    with store.pool.connection() as c:
        c.execute('''INSERT INTO message_receipts(id,account,platform,provider_chat,max_id,state,basis,audience,observed_at)
            SELECT md5(n::text)||md5(n::text),'receipt-test','telegram','100',n,'read',
                'telegram_outbox_boundary','private_peer','2026-10-14T08:00:00Z'::timestamptz+n*interval '1 second'
            FROM generate_series(1,5000) AS n''')
        c.execute('ANALYZE message_receipts')
        plans=[c.execute('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+sql,args).fetchone()['QUERY PLAN'][0]['Plan']
            for sql,args in [(ROW_PROOFS_SQL,(mids,)),(REFERENCE_TG_SQL,('receipt-test','100',4200))]]
        assert for_reference(c,'receipt-test','100','4200','telegram')['observed_at']=='2026-10-14T09:10:00+00:00'
    def nodes(value):
        yield value
        for child in value.get('Plans',[]):yield from nodes(child)
    for plan in plans:
        walked=list(nodes(plan))
        indexed=[node for node in walked if node.get('Index Name')=='receipt_boundary_lookup' and node['Actual Loops']>0]
        assert indexed and all(node['Actual Rows']<=1 for node in indexed)
        assert all(node.get('Rows Removed by Filter',0)<=1 for node in indexed)
        assert not any(node['Node Type']=='Sort' and node['Actual Loops']>0 for node in walked)
        assert not any(node.get('Relation Name')=='message_receipts' and node['Node Type']=='Seq Scan'
            and node['Actual Loops']>0 for node in walked)
    proofs=evidence(store,[{'id':mid} for mid in mids])
    assert len(proofs)==100 and all(proof['state']=='read' for proof in proofs.values())


def test_deleted_message_retains_independent_receipt(receipt_state):
    store,archive,state=receipt_state;mid=outgoing(archive)
    apply_and_publish(state,capture())
    archive.ingest(event(account='receipt-test',direction='outgoing',deleted=True,
        updated_at='2026-10-14T09:00:00Z'))
    assert evidence(store,[{'id':mid}])[mid]['state']=='read'
    before=len(statuses(store));apply_and_publish(state,capture(max_id=18))
    assert len(statuses(store))==before


def test_pending_publication_rechecks_chat_policy(receipt_state):
    store,archive,state=receipt_state
    for i in range(1,102):outgoing(archive,i)
    apply_and_publish(state,capture(max_id=101));assert len(statuses(store))==100
    archive.register('receipt-test','telegram','Synthetic receipts',include_chats=['200'])
    assert state.publish_receipts()==0 and len(statuses(store))==100
    archive.register('receipt-test','telegram','Synthetic receipts',include_chats=['100'])
    assert state.publish_receipts()==1 and len(statuses(store))==101


def test_foreign_excluded_disabled_and_stale_lease_do_not_record(receipt_state):
    store,archive,state=receipt_state
    with pytest.raises(ValueError,match='foreign_capture'):state.apply(capture(account='telegram-demo'))
    archive.register('receipt-test','telegram','Synthetic receipts',include_chats=['100'])
    assert state.apply(capture(chat='200'))['outcome']=='excluded'
    with store.pool.connection() as c:c.execute("UPDATE account_policies SET enabled=false WHERE account='receipt-test'")
    with pytest.raises(RuntimeError,match='collection_disabled'):state.apply(capture())
    with store.pool.connection() as c:c.execute("UPDATE account_policies SET enabled=true WHERE account='receipt-test'")
    archive.release('receipt-test','receipt-worker',state.lease[1])
    with pytest.raises(RuntimeError,match='collector_lease_lost'):state.apply(capture())
    with store.pool.connection() as c:assert c.execute('SELECT count(*) AS n FROM message_receipts').fetchone()['n']==0


def test_receipt_rows_do_not_leak_across_accounts_or_to_incoming(receipt_state):
    store,archive,state=receipt_state;mid=outgoing(archive)
    other=archive.ingest(event(account='receipt-test',chat='200',direction='outgoing'))['message']
    incoming=archive.ingest(event(account='receipt-test',provider_id='9'))['message']
    state.apply(capture())
    result=evidence(store,[{'message':mid},{'id':other},{'id':incoming}])
    assert set(result)=={mid,other,incoming}
    assert result[mid]['state']=='read'
    assert result[other]['state']==result[incoming]['state']=='unknown'
    assert evidence(store,[])=={}


def test_whatsapp_out_of_order_and_aggregate_evidence(seeded):
    store,_,_=seeded;archive=Archive(store)
    archive.register('receipt-test','whatsapp','Synthetic WhatsApp',discover=True)
    epoch=archive.acquire('receipt-test','receipt-worker');state=CollectorState(archive,'receipt-test',('receipt-worker',epoch))
    mid=outgoing(archive,'true_100@c.us_wa-17',platform='whatsapp',chat='100@c.us')
    group=outgoing(archive,'true_100@g.us_wa-17',platform='whatsapp',chat='100@g.us')
    apply_and_publish(state,capture('whatsapp',state='read'))
    apply_and_publish(state,capture('whatsapp',state='delivered',observed_at='2026-10-14T07:00:00Z'))
    apply_and_publish(state,capture('whatsapp',state='server'))
    assert len(statuses(store))==1
    result=evidence(store,[{'id':mid}])[mid]
    assert result['state']=='read' and result['delivered_observed_at']=='2026-10-14T07:00:00+00:00'
    apply_and_publish(state,capture('whatsapp',state='played',observed_at='2026-10-14T09:00:00Z'))
    frozen=statuses(store)[-1]['snapshot']['delivery_receipt']
    assert frozen['state']=='played' and frozen['read_observed_at']=='2026-10-14T08:00:00+00:00'
    assert frozen['delivered_observed_at']=='2026-10-14T07:00:00+00:00'
    apply_and_publish(state,capture('whatsapp',chat='100@g.us',audience='provider_aggregate'))
    assert evidence(store,[{'id':group}])[group]['recipient_delivery_confirmed'] is False
    assert evidence(store,[{'id':group}])[group]['audience']=='provider_aggregate'
    from messaging_service.receipts import for_reference
    with store.pool.connection() as c:
        assert for_reference(c,'receipt-test','100@c.us','true_100@c.us_wa-17','whatsapp')['state']=='played'
        assert for_reference(c,'whatsapp-demo-1','100@c.us','true_100@c.us_wa-17','whatsapp')['state']=='unknown'


def typed_chat(store,provider_chat,chat_type):
    with store.pool.connection() as c:
        c.execute("UPDATE chats SET chat_type=%s WHERE account='receipt-test' AND provider_chat=%s",(chat_type,provider_chat))


# spec: ANYM-868
def test_a_group_boundary_publishes_an_aggregate_read_and_a_channel_or_untyped_chat_is_skipped(receipt_state,tmp_path):
    store,archive,state=receipt_state
    read=outgoing(archive,17,chat='-100');unread=outgoing(archive,18,chat='-100');typed_chat(store,'-100','group')
    channel=outgoing(archive,17,chat='-1000000000200');typed_chat(store,'-1000000000200','channel')
    untyped=outgoing(archive,17,chat='-300');typed_chat(store,'-300',None)
    peer=outgoing(archive,17)
    group=lambda chat:capture(chat=chat,audience='provider_aggregate')
    assert state.apply(group('-1000000000200'))=={'outcome':'receipt_skipped','reason':'receipt_broadcast_channel'}
    assert state.apply(group('-300'))=={'outcome':'receipt_skipped','reason':'receipt_chat_type_unknown'}
    assert state.apply(group('-400'))=={'outcome':'receipt_skipped','reason':'receipt_chat_type_unknown'}
    # The skips are outcomes, not failures: a drain with them in the middle commits every capture after them.
    spool=CaptureSpool(tmp_path/'receipts')
    for value in (group('-1000000000200'),group('-300'),group('-100'),capture()):spool.put(value)
    assert spool.drain(state.apply)==4 and not list(spool.path.glob('*.json'))
    state.publish_receipts()
    with store.pool.connection() as c:
        assert {r['provider_chat'] for r in c.execute("SELECT provider_chat FROM message_receipts")}=={'-100','100'}
    published={e['message']:e['snapshot']['delivery_receipt'] for e in statuses(store)}
    assert set(published)=={read,peer}
    assert published[read]['state']=='read' and published[read]['audience']=='provider_aggregate'
    assert published[read]['basis']=='telegram_outbox_boundary' and published[read]['recipient_delivery_confirmed'] is False
    assert published[peer]['audience']=='private_peer' and published[peer]['recipient_delivery_confirmed'] is True
    found=evidence(store,[{'id':m} for m in (read,unread,channel,untyped)])
    assert [found[m]['state'] for m in (read,unread,channel,untyped)]==['read','unknown','unknown','unknown']
    from messaging_service.receipts import for_reference
    with store.pool.connection() as c:
        proof=for_reference(c,'receipt-test','-100','17','telegram')
        assert (proof['state'],proof['audience'])==('read','provider_aggregate')
        assert for_reference(c,'receipt-test','-100','18','telegram')['state']=='unknown'


def test_self_chat_receipts_are_normalized_whatever_the_device_ack(seeded):
    store,_,_=seeded;archive=Archive(store)
    archive.register('receipt-test','whatsapp','Synthetic WhatsApp',discover=True)
    epoch=archive.acquire('receipt-test','receipt-worker');state=CollectorState(archive,'receipt-test',('receipt-worker',epoch))
    # One account's own devices ACK a note to self; another account's never do.
    acked=outgoing(archive,'true_100@c.us_wa-17',platform='whatsapp',chat='100@c.us',metadata={'is_self_chat':True})
    silent=outgoing(archive,'true_200@lid_wa-18',platform='whatsapp',chat='200@lid')
    with store.pool.connection() as c:
        # The LID self chat is known only from the dialog observation.
        c.execute('''INSERT INTO chat_read_observations(chat,account,observed_at,basis,is_self_chat)
            VALUES(%s,'receipt-test',clock_timestamp(),'whatsapp_cached_chat',true)''',
            (stable_id('chat','receipt-test','200@lid'),))
    apply_and_publish(state,capture('whatsapp',state='delivered'))
    proofs=evidence(store,[{'id':acked},{'id':silent}])
    for proof,raw in [(proofs[acked],'delivered'),(proofs[silent],'unknown')]:
        assert proof['state']=='not_applicable' and proof['basis']=='self_chat' and proof['audience']=='self'
        assert proof['recipient_delivery_confirmed'] is False and proof['provider_state']==raw
    assert statuses(store)==[]  # A note to self has no recipient status to publish.
    from messaging_service.receipts import for_reference
    with store.pool.connection() as c:
        assert for_reference(c,'receipt-test','100@c.us','true_100@c.us_wa-17','whatsapp')['state']=='not_applicable'
        assert for_reference(c,'receipt-test','200@lid','true_200@lid_wa-18','whatsapp')['state']=='not_applicable'
        assert for_reference(c,'receipt-test','300@c.us','true_300@c.us_wa-19','whatsapp')['state']=='unknown'


def test_restricted_receipt_roles_are_not_credential_access(receipt_state):
    import messaging_service.archive as module
    import psycopg
    store,archive,state=receipt_state;mid=outgoing(archive)
    for template,role in [('collector_role.sql','receipt_collector'),('gateway_role.sql','receipt_gateway')]:
        ddl=Path(module.__file__).with_name(template).read_text().replace('messaging_collector' if 'collector' in template else 'messaging_gateway',role)
        with store.pool.connection() as c:c.execute(ddl)
    with store.pool.connection() as c:
        c.execute('SET LOCAL ROLE receipt_collector')
        from messaging_service.receipts import record,publish
        record(c,capture());assert publish(c,'receipt-test')==1
    with store.pool.connection() as c:
        c.execute('SET LOCAL ROLE receipt_gateway')
        from messaging_service.receipts import for_rows
        assert for_rows(c,[{'id':mid}])[mid]['state']=='read'
        with pytest.raises(psycopg.errors.InsufficientPrivilege),c.transaction():
            c.execute('DELETE FROM message_receipts')
