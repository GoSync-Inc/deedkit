from copy import deepcopy
from datetime import datetime,timezone,timedelta
import hashlib
from pathlib import Path

import pytest

from messaging_service.archive import Archive
from messaging_service.blobs import Blob,BlobWriter
from messaging_service.envelope import stable_id,validate
from messaging_service.spool import DurableSpool
from messaging_service.store import APIError


def event(**changes):
    value={'account':'archive-test','platform':'telegram','chat':'100','chat_title':'Fixture room',
           'provider_id':'17','sender':'42','body':'Первый текст lighthouse','sent_at':'2026-10-01T12:00:00+00:00',
           'updated_at':'2026-10-01T12:00:00+00:00','deleted':False,'direction':'incoming','origin':'live','media':[]}
    value.update(changes); return value


@pytest.fixture()
def archive(seeded):
    s,a,b=seeded
    archive=Archive(s); archive.register('archive-test','telegram','Synthetic archive',include_chats=['100'])
    key=s.issue_key('archive-reader',['archive-test'])
    return archive,s,key,b


def test_idempotent_ingest_revision_and_snapshot_history(archive):
    ar,s,key,_=archive
    result=ar.ingest(event()); mid=result['message']
    assert ar.ingest(event())['outcome']=='duplicate'
    assert ar.ingest(event(body='Вторая версия',updated_at='2026-10-01T12:01:00Z'))['outcome']=='edited'
    assert ar.ingest(event(origin='backfill'))['outcome']=='stale'
    assert len(s.search(key)['items'])==1
    changes=s.get_updates(key,'history')['items']
    assert [x['revision'] for x in changes]==[1,2]
    assert changes[0]['body']=='Первый текст lighthouse'
    assert changes[1]['body']=='Вторая версия'
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM message_revisions WHERE message=%s',(mid,)).fetchone()['n']==2


def test_same_version_conflict_is_explicit_no_overwrite(archive):
    ar,s,key,_=archive
    ar.ingest(event())
    assert ar.ingest(event(body='contradiction'))['outcome']=='conflict'
    assert s.search(key)['items'][0]['body']=='Первый текст lighthouse'
    assert s.capabilities(key)['sources'][0]['detail_code']=='version_conflict'
    ar.ingest(event(provider_id='18'))
    assert s.capabilities(key)['sources'][0]['detail_code']=='version_conflict'


def test_sync_conflict_alert_follows_unresolved_conflicts_not_the_sticky_code(archive):
    from messaging_service.sync_status import get_sync_status
    ar,s,key,_=archive
    def status():
        with s.pool.connection() as c:
            return get_sync_status(c,{'accounts':['archive-test'],'chat_ids':None})['sources'][0]
    # A sender contradiction needs attention; a known class raises no alert (ANYM-557).
    ar.ingest(event());ar.ingest(event(sender='43'))
    row=status()
    assert row['active_conflicts']==1 and 'archive_version_conflicts' in row['alerts']
    assert row['archive_detail_code']=='version_conflict'
    # Later provider evidence resolves it; the stored code stays, the alert does not.
    assert ar.ingest(event(body='Вторая версия',updated_at='2026-10-01T12:01:00Z'))['outcome']=='edited'
    row=status()
    assert row['active_conflicts']==0 and row['historical_conflicts']==1
    assert 'archive_version_conflicts' not in row['alerts']
    assert row['archive_detail_code']=='coverage_not_reconciled'


def test_message_identity_cannot_change_on_edit(archive):
    ar,s,key,_=archive
    ar.ingest(event())
    assert ar.ingest(event(sender='another-person',updated_at='2026-10-01T12:01:00Z'))['outcome']=='conflict'
    assert s.search(key)['items'][0]['sender']=='42'


def test_authenticated_private_sender_completion_preserves_revisions_and_conflicts(archive):
    ar,s,key,_=archive
    original=event(sender='',direction='outgoing',metadata={'chat_type':'private'})
    mid=ar.ingest(original)['message']
    correct={**original,'sender':'42','sender_profile':{'display_name':'Synthetic Owner'},
        'metadata':{'chat_type':'private','sender_identity_basis':'authenticated_self'}}
    # Ordinary identity changes conflict until authenticated-self evidence is supplied.
    assert ar.ingest({**correct,'metadata':{'chat_type':'private'}})['outcome']=='conflict'
    assert ar.ingest({**correct,'body':'unrelated conflicting text'})['outcome']=='conflict'
    assert ar.ingest({**correct,'updated_at':'2026-10-01T12:01:00Z'})['outcome']=='conflict'
    assert ar.ingest(correct)['outcome']=='edited'
    assert ar.ingest(correct)['outcome']=='duplicate'
    message=s.get_context(key,mid)['message']
    assert message['sender']=='42' and message['revision']==2
    assert message['sender_name']=='Synthetic Owner'
    assert message['sender_identity_basis']=='authenticated_self'
    assert message['evidence']['active_conflicts']==2
    with s.pool.connection() as c:
        snapshots=c.execute('SELECT snapshot FROM message_revisions WHERE message=%s ORDER BY revision',(mid,)).fetchall()
        assert [r['snapshot']['sender'] for r in snapshots]==['','42']
        assert len({r['snapshot']['updated_at'] for r in snapshots})==1
    assert [r['revision'] for r in s.get_updates(key,'identity-completion')['items']]==[1,2]


@pytest.mark.parametrize('change',[
    {'metadata':{'chat_type':'group','sender_identity_basis':'authenticated_self'}},
    {'direction':'incoming'}, {'body':'changed content'},
    {'updated_at':'2026-10-01T12:01:00Z'}, {'deleted':True}])
def test_sender_completion_does_not_overwrite_uncertain_content(archive,change):
    ar,s,key,_=archive
    original=event(sender='',direction='outgoing',metadata={'chat_type':'private'})
    mid=ar.ingest(original)['message']
    candidate={**original,'sender':'42','metadata':{'chat_type':'private','sender_identity_basis':'authenticated_self'},**change}
    assert ar.ingest(candidate)['outcome']=='conflict'
    assert s.get_context(key,mid)['message']['sender']==''


def whatsapp_event(sender,**changes):
    return event(**{'account':'archive-wa','platform':'whatsapp','chat':'900@lid','provider_id':'true_900@lid_3EB0SYNTH',
        'sender':sender,'direction':'outgoing',**changes})


def wire(body='Первый текст lighthouse'):
    return {'version':1,'state':'wire_observed','basis':'whatsapp_native_wire',
        'wire_sha256':hashlib.sha256(body.encode()).hexdigest(),'entities':None,
        'unsupported_entities':[],'warnings':['syntax_interpretation_unknown']}


def sender_rows(s,mid):
    with s.pool.connection() as c:
        message=c.execute('SELECT sender,revision FROM messages WHERE id=%s',(mid,)).fetchone()
        profiles=c.execute("SELECT sender FROM sender_profiles WHERE account='archive-wa' ORDER BY sender").fetchall()
        open_conflicts=c.execute('SELECT count(*) AS n FROM message_conflicts WHERE message=%s AND resolved_at IS NULL',
            (mid,)).fetchone()['n']
    return message['sender'],message['revision'],[r['sender'] for r in profiles],open_conflicts


# spec: ANYM-612
@pytest.mark.parametrize('retained,candidate',[
    ('900:7@lid','900@lid'),('900@lid','900:7@lid'),('900:1@lid','900:2@lid'),
    ('555:3@c.us','555@c.us'),('555@c.us','555:3@c.us')])
def test_whatsapp_sender_device_number_is_not_a_conflict(archive,retained,candidate):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    mid=ar.ingest(whatsapp_event(retained,formatting=wire()))['message']
    observed=whatsapp_event(candidate,origin='reconcile',formatting=wire(),
        sender_profile={'display_name':'Synthetic Owner'})
    assert ar.ingest(observed)['outcome']=='unchanged'
    # Comparison only: the stored sender, revision and receipt keep their raw values,
    # and the candidate's form gets no sender profile.
    assert sender_rows(s,mid)==(retained,1,[],0)
    with s.pool.connection() as c:
        envelope=c.execute("SELECT envelope FROM ingest_receipts WHERE outcome='unchanged'").fetchone()['envelope']
        snapshots=c.execute('SELECT snapshot FROM message_revisions WHERE message=%s',(mid,)).fetchall()
    assert envelope['sender']==candidate and [r['snapshot']['sender'] for r in snapshots]==[retained]


# spec: ANYM-612
@pytest.mark.parametrize('retained,candidate,change',[
    ('900:7@lid','901@lid',{}),('900:7@lid','9001@lid',{}),('900@lid','900:7@c.us',{}),
    ('900:7@g.us','900@g.us',{}),('900:7@s.whatsapp.net','900@s.whatsapp.net',{}),
    ('900:x@lid','900@lid',{}),('900:7:1@lid','900@lid',{}),(':7@lid','@lid',{}),('900:@lid','900@lid',{}),
    ('900:7@lid','900@lid',{'sent_at':'2026-10-01T11:59:59+00:00'}),
    ('900:7@lid','900@lid',{'direction':'incoming'}),
    ('900:7@lid','900@lid',{'body':'contradiction','formatting':wire('contradiction')}),
    ('900:7@lid','900@lid',{'media':[{'provider_id':'f1','mime':'image/jpeg','bytes':10,'filename':'p.jpg'}]}),
    ('900:7@lid','900@lid',{'formatting':{'version':1,'state':'unknown','basis':None,'wire_sha256':None,
        'entities':None,'unsupported_entities':[],'warnings':['native_entities_not_captured']}}),
    ('900:7@lid','900@lid',{'deleted':True,'body':'','formatting':None})])
def test_whatsapp_sender_device_rule_keeps_other_differences_in_conflict(archive,retained,candidate,change):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    mid=ar.ingest(whatsapp_event(retained,formatting=wire()))['message']
    observed=whatsapp_event(candidate,**{'formatting':wire(),**change})
    if observed['formatting'] is None:del observed['formatting']
    assert ar.ingest(observed)['outcome']=='conflict'
    assert sender_rows(s,mid)==(retained,1,[],1)


# spec: ANYM-612
def test_whatsapp_sender_device_rule_needs_known_formatting_on_both_sides(archive):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    mid=ar.ingest(whatsapp_event('900:7@lid'))['message']
    assert ar.ingest(whatsapp_event('900@lid',formatting=wire()))['outcome']=='conflict'
    # Two observations that state no formatting do not differ in it.
    assert ar.ingest(whatsapp_event('900@lid'))['outcome']=='unchanged'
    assert sender_rows(s,mid)==('900:7@lid',1,[],1)


# spec: ANYM-612
def test_whatsapp_sender_device_reread_resolves_no_open_conflict(archive):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    mid=ar.ingest(whatsapp_event('900:7@lid'))['message']
    assert ar.ingest(whatsapp_event('900@lid',formatting=wire()))['outcome']=='conflict'
    assert ar.ingest(whatsapp_event('900:7@lid',body='contradiction'))['outcome']=='conflict'
    # A later reread in the other device form states nothing about either open conflict.
    assert ar.ingest(whatsapp_event('900@lid',updated_at='2026-10-01T12:01:00+00:00'))['outcome']=='unchanged'
    assert sender_rows(s,mid)==('900:7@lid',1,[],2)


# spec: ANYM-612
def test_telegram_sender_with_a_colon_is_not_a_whatsapp_device(archive):
    ar,s,key,_=archive
    ar.ingest(event(sender='900:7@lid'))
    assert ar.ingest(event(sender='900@lid'))['outcome']=='conflict'
    assert s.search(key)['items'][0]['sender']=='900:7@lid'


def jitter_event(**changes):
    # A WhatsApp message nobody edited carries its sent time as its provider version.
    if 'sent_at' in changes:changes.setdefault('updated_at',changes['sent_at'])
    value=whatsapp_event(changes.pop('sender','900@lid'),**{'formatting':wire(),'metadata':{'timestamp_resolution':'second'},
        **changes})
    if not value['metadata']:del value['metadata']
    if value['formatting'] is None:del value['formatting']
    return value


def shifted(seconds):
    return (datetime(2026,10,1,12,tzinfo=timezone.utc)+timedelta(seconds=seconds)).isoformat()


def stored_conflicts(s,mid):
    with s.pool.connection() as c:
        return [r['reason'] for r in c.execute('SELECT reason FROM message_conflicts WHERE message=%s',(mid,))]


def retained_row(s,mid):
    with s.pool.connection() as c:
        message=c.execute('SELECT sent_at,provider_updated_at FROM messages WHERE id=%s',(mid,)).fetchone()
        snapshots=c.execute('SELECT snapshot FROM message_revisions WHERE message=%s',(mid,)).fetchall()
        metadata=c.execute('SELECT metadata FROM message_metadata WHERE message=%s',(mid,)).fetchone()
    return message['sent_at'],message['provider_updated_at'],[r['snapshot']['sent_at'] for r in snapshots],metadata['metadata']


# spec: ANYM-558
@pytest.mark.parametrize('seconds,updated',[(1,1),(6,6),(-1,0),(-6,0)])
def test_whatsapp_sent_at_jitter_is_not_a_conflict(archive,seconds,updated):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    mid=ar.ingest(jitter_event())['message']
    before=retained_row(s,mid)
    observed=jitter_event(sent_at=shifted(seconds),updated_at=shifted(updated),origin='backfill',
        sender_profile={'display_name':'Synthetic Owner'},metadata={'timestamp_resolution':'second','message_kind':'text'})
    assert ar.ingest(observed)['outcome']=='unchanged'
    # The retained revision, its sent time and version stay; nothing is recorded as a conflict. What the
    # re-read newly states about the same revision (metadata, the sender's profile) is kept, as for any re-read.
    assert sender_rows(s,mid)==('900@lid',1,['900@lid'],0) and stored_conflicts(s,mid)==[]
    after=retained_row(s,mid)
    assert after[:3]==before[:3]==(datetime(2026,10,1,12,tzinfo=timezone.utc),datetime(2026,10,1,12,tzinfo=timezone.utc),
        ['2026-10-01T12:00:00+00:00'])
    assert before[3]=={'timestamp_resolution':'second'} and after[3]=={'timestamp_resolution':'second','message_kind':'text'}


# spec: ANYM-558
@pytest.mark.parametrize('seconds',[-1,-6])
def test_whatsapp_sent_at_jitter_in_an_older_observation_is_stale(archive,seconds):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    mid=ar.ingest(jitter_event())['message']
    before=retained_row(s,mid)
    assert ar.ingest(jitter_event(sent_at=shifted(seconds),origin='backfill'))['outcome']=='stale'
    assert sender_rows(s,mid)==('900@lid',1,[],0) and stored_conflicts(s,mid)==[] and retained_row(s,mid)==before


# spec: ANYM-558
def test_whatsapp_sent_at_jitter_is_no_later_evidence(archive):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    mid=ar.ingest(jitter_event())['message']
    assert ar.ingest(jitter_event(body='contradiction',formatting=wire('contradiction')))['outcome']=='conflict'
    # Its version is the jittered sent time, not a later provider state: the open conflict stays,
    # and a contradiction at the retained version is still a conflict, not stale.
    assert ar.ingest(jitter_event(sent_at=shifted(1)))['outcome']=='unchanged'
    assert sender_rows(s,mid)==('900@lid',1,[],1)
    assert ar.ingest(jitter_event(body='another contradiction',formatting=wire('another contradiction')))['outcome']=='conflict'
    # A real later edit still applies and resolves both.
    assert ar.ingest(jitter_event(body='Вторая версия',formatting=wire('Вторая версия'),
        updated_at='2026-10-01T12:01:00+00:00'))['outcome']=='edited'
    assert sender_rows(s,mid)==('900@lid',2,[],0)


# spec: ANYM-558
def test_whatsapp_sent_at_jitter_with_a_later_edit_time_is_later_evidence(archive):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    mid=ar.ingest(jitter_event())['message']
    assert ar.ingest(jitter_event(body='contradiction',formatting=wire('contradiction')))['outcome']=='conflict'
    # The same text with an edit time beyond the offset: the stored version moves on, as for an exact
    # re-read, and the older contradiction is resolved.
    assert ar.ingest(jitter_event(sent_at=shifted(1),updated_at=shifted(60)))['outcome']=='unchanged'
    assert retained_row(s,mid)[:3]==(datetime(2026,10,1,12,tzinfo=timezone.utc),datetime(2026,10,1,12,1,tzinfo=timezone.utc),
        ['2026-10-01T12:00:00+00:00'])
    assert sender_rows(s,mid)==('900@lid',1,[],0)
    # Another text at an earlier version is then stale, not an edit.
    assert ar.ingest(jitter_event(body='Вторая версия',formatting=wire('Вторая версия'),
        updated_at=shifted(30)))['outcome']=='stale'
    assert sender_rows(s,mid)==('900@lid',1,[],0)


# spec: ANYM-558
@pytest.mark.parametrize('retained,candidate',[
    ({},{'sent_at':shifted(7)}),({},{'sent_at':shifted(-7)}),({},{'sent_at':shifted(60)}),
    ({'metadata':{}},{'sent_at':shifted(1)}),
    ({'metadata':{'timestamp_resolution':'unknown'}},{'sent_at':shifted(1)}),
    ({},{'sent_at':shifted(1),'metadata':{'timestamp_resolution':'unknown'}}),
    ({},{'sent_at':shifted(1),'metadata':{}}),
    ({},{'sent_at':shifted(1),'direction':'incoming'}),
    ({},{'sent_at':shifted(1),'sender':'901@lid'}),
    ({},{'sent_at':shifted(1),'sender':'900:7@lid'}),
    ({},{'sent_at':shifted(1),'sender':''}),
    ({},{'sent_at':shifted(1),'body':'contradiction','formatting':wire('contradiction')}),
    ({},{'sent_at':shifted(1),'media':[{'provider_id':'f1','mime':'image/jpeg','bytes':10,'filename':'p.jpg'}]}),
    ({'formatting':{'version':1,'state':'unknown','basis':None,'wire_sha256':None,'entities':None,
        'unsupported_entities':[],'warnings':['native_entities_not_captured']}},{'sent_at':shifted(1)}),
    ({'formatting':None},{'sent_at':shifted(1)}),
    ({},{'sent_at':shifted(1),'deleted':True,'body':'','formatting':None}),
    ({},{'sent_at':shifted(1),'updated_at':shifted(60),'body':'contradiction','formatting':wire('contradiction')})])
def test_whatsapp_sent_at_jitter_rule_keeps_other_differences_in_conflict(archive,retained,candidate):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    mid=ar.ingest(jitter_event(**retained))['message']
    assert ar.ingest(jitter_event(**candidate))['outcome']=='conflict'
    # The class names the sent time, the first field ingest compares (ANYM-557).
    assert sender_rows(s,mid)==('900@lid',1,[],1)
    relation='older' if candidate['sent_at']<shifted(0) else 'newer'
    assert stored_conflicts(s,mid)==[f'whatsapp_sent_at_{relation}_version']


# spec: ANYM-558
def test_whatsapp_sent_at_jitter_rule_is_whatsapp_only(archive):
    ar,s,key,_=archive
    mid=ar.ingest(event(metadata={'timestamp_resolution':'second'}))['message']
    assert ar.ingest(event(sent_at=shifted(1),updated_at=shifted(1),
        metadata={'timestamp_resolution':'second'}))['outcome']=='conflict'
    assert stored_conflicts(s,mid)==['telegram_sent_at_newer_version']


# spec: ANYM-558
def test_whatsapp_sent_at_jitter_rule_is_a_pure_function_of_two_observations():
    from messaging_service.archive import whatsapp_sent_at_jitter
    retained={**validate(jitter_event()),'message':'m','provider_chat':'900@lid','chat':'c','revision':1}
    assert whatsapp_sent_at_jitter(retained,validate(jitter_event(sent_at=shifted(2))))
    assert not whatsapp_sent_at_jitter(retained,validate(jitter_event(sent_at=shifted(7))))
    assert not whatsapp_sent_at_jitter(retained,validate(jitter_event()))
    # Evidence it cannot read is no tolerance, never an exception inside ingest.
    for broken in (None,{},{**retained,'sent_at':'not a time'},{**retained,'metadata':'second'}):
        assert not whatsapp_sent_at_jitter(broken,validate(jitter_event(sent_at=shifted(1))))


def test_delete_tombstone_does_not_resurrect_and_search_hides(archive):
    ar,s,key,_=archive
    mid=ar.ingest(event())['message']
    ar.ingest(event(deleted=True,updated_at='2026-10-01T12:02:00Z'))
    assert s.search(key)['items']==[]
    assert s.get_context(key,mid)['message']['deleted']
    assert ar.ingest(event(body='late backfill',updated_at='2026-10-02T12:00:00Z',origin='backfill'))['outcome']=='tombstoned'
    assert len(s.get_updates(key,'main')['items'])==2


def test_account_policy_and_historical_label(archive):
    ar,s,key,_=archive
    assert ar.ingest(event(chat='excluded'))['outcome']=='excluded'
    ar.ingest(event(origin='backfill'))
    assert s.get_updates(key,'main')['items'][0]['historical'] is True
    ar.register('archive-test','telegram','Synthetic archive',discover=True,exclude_chats=['excluded'])
    assert ar.ingest(event(chat='new-chat'))['outcome']=='created'
    assert ar.ingest(event(chat='excluded'))['outcome']=='excluded'
    with s.pool.connection() as c: c.execute("UPDATE account_policies SET enabled=false WHERE account='archive-test'")
    with pytest.raises(ValueError,match='collection_disabled'): ar.ingest(event(provider_id='99'))


def test_scopes_also_cover_ingested_refs_and_counts(archive):
    ar,s,key,other=archive
    mid=ar.ingest(event())['message']
    assert s.search(other,'lighthouse')['items']  # Existing independent synthetic file caption.
    assert all(m['account']!='archive-test' for m in s.search(other)['items'])
    with pytest.raises(APIError): s.get_context(other,mid)
    assert len(s.capabilities(key)['sources'])==1
    assert s.capabilities(key)['coverage']=='partial_or_unknown'


def test_collector_lease_fencing(archive):
    ar,s,key,_=archive
    epoch=ar.acquire('archive-test','worker-a')
    with pytest.raises(RuntimeError,match='already_owned'): ar.acquire('archive-test','worker-b')
    ar.renew('archive-test','worker-a',epoch)
    ar.ingest(event(),lease=('worker-a',epoch))
    with s.pool.connection() as c: c.execute("UPDATE collector_leases SET expires_at=now()-interval '1 second'")
    newer=ar.acquire('archive-test','worker-b'); assert newer>epoch
    with pytest.raises(RuntimeError,match='lease_lost'): ar.ingest(event(provider_id='99'),lease=('worker-a',epoch))
    with pytest.raises(RuntimeError,match='lease_lost'): ar.renew('archive-test','worker-a',epoch)
    assert len(s.search(key)['items'])==1


def test_spool_survives_db_failure_restart_and_commit_before_delete(archive,tmp_path):
    ar,s,key,_=archive
    queue=DurableSpool(tmp_path/'spool',reserve_bytes=0)
    first=queue.put(event()); assert queue.put(event())==first
    def crash(_): raise RuntimeError('db_down')
    with pytest.raises(RuntimeError): queue.drain(crash)
    assert (queue.path/first).exists()
    def committed_then_crashed(value):
        ar.ingest(value); raise RuntimeError('crash_after_commit')
    with pytest.raises(RuntimeError): queue.drain(committed_then_crashed)
    restarted=DurableSpool(queue.path,reserve_bytes=0)
    assert restarted.drain(ar.ingest)==1
    assert restarted.drain(ar.ingest)==0
    assert len(s.get_updates(key,'main')['items'])==1


def test_spool_quota_and_corruption_fail_closed(tmp_path):
    queue=DurableSpool(tmp_path/'spool',max_entries=1,reserve_bytes=0)
    name=queue.put(event())
    with pytest.raises(RuntimeError,match='spool_full'): queue.put(event(provider_id='18'))
    (queue.path/name).write_text('{}')
    with pytest.raises(RuntimeError,match='spool_corrupt'): queue.drain(lambda _:None)
    assert (queue.path/name).exists()


@pytest.mark.parametrize('change',[{'sent_at':'2026-10-01'},{'body':'a'*65537},{'direction':'invalid'},
    {'deleted':'false'},{'media':[{'provider_id':'x','filename':'x','mime':'x\r\nheader','bytes':None}]},
    {'raw_token':'must_not_store'},{'media':[{'provider_id':'x','filename':'x','mime':'text/plain','bytes':-1}]}])
def test_envelope_validation(change):
    with pytest.raises(ValueError): validate(event(**change))


def media_event(**changes):
    return event(media=[{'provider_id':'media-42','filename':'../private\r\n.txt','mime':'text/plain','bytes':4}],**changes)


def test_media_ready_only_after_bytes_and_lease_fencing(archive,tmp_path):
    ar,s,key,other=archive
    mid=ar.ingest(media_event())['message']
    item=s.get_context(key,mid)['pending_attachments'][0]
    assert s.get_attachment(key,item['id'])['status']=='queued'
    with pytest.raises(APIError): s.get_attachment(other,item['id'])
    job=ar.claim_media('worker'); assert job
    assert ar.claim_media('second') is None
    writer=BlobWriter(s.files,reserve_bytes=0); blob=writer.write([b'data'],expected_bytes=4)
    # Simulated crash after durable bytes: no ready metadata until retry commits.
    assert s.get_attachment(key,job['id'])['status']=='leased'
    with s.pool.connection() as c: c.execute("UPDATE media_jobs SET lease_until=now()-interval '1 second'")
    newer=ar.claim_media('second')
    with pytest.raises(RuntimeError,match='media_lease_lost'): ar.finish_media(job,blob,writer)
    ar.finish_media(newer,blob,writer)
    attachment=s.get_attachment(key,job['id']); assert attachment['status']=='ready'
    assert '/' not in attachment['filename'] and '\r' not in attachment['filename']
    grant=attachment['download_url'].rsplit('/',1)[-1]
    assert s.download(grant)[1].read_bytes()==b'data'
    assert [e['kind'] for e in s.get_updates(key,'main')['items']]==['message.created','attachment.ready']


def test_media_edit_supersedes_lease_and_old_grant(archive):
    ar,s,key,_=archive
    ar.ingest(media_event()); job=ar.claim_media('worker')
    writer=BlobWriter(s.files,reserve_bytes=0); blob=writer.write([b'data'])
    ar.finish_media(job,blob,writer)
    grant=s.get_attachment(key,job['id'])['download_url'].rsplit('/',1)[-1]
    ar.ingest(event(body='file removed',updated_at='2026-10-01T12:02:00Z'))
    with pytest.raises(APIError): s.download(grant)
    with pytest.raises(APIError): s.get_attachment(key,job['id'])


def test_media_delete_before_worker_commits(archive):
    ar,s,key,_=archive
    ar.ingest(media_event()); job=ar.claim_media('worker')
    writer=BlobWriter(s.files,reserve_bytes=0); blob=writer.write([b'data'])
    ar.ingest(event(deleted=True,updated_at='2026-10-01T12:02:00Z'))
    with pytest.raises(RuntimeError,match='lease_lost'): ar.finish_media(job,blob,writer)
    with pytest.raises(APIError): s.get_attachment(key,job['id'])


def test_media_retry_backoff_and_exhausted_crash(archive):
    ar,s,key,_=archive
    ar.ingest(media_event()); job=ar.claim_media('worker')
    ar.fail_media(job,'network_error')
    assert ar.claim_media('worker') is None
    with s.pool.connection() as c: c.execute("UPDATE media_jobs SET next_due=now()-interval '1 second'")
    job=ar.claim_media('worker'); assert job['attempts']==2
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET attempts=5,lease_until=now()-interval '1 second'")
    assert ar.claim_media('worker') is None
    assert s.get_attachment(key,job['id'])['status']=='failed'


def test_blob_limits_integrity_and_interrupted_stream(tmp_path):
    writer=BlobWriter(tmp_path/'blobs',max_bytes=8,reserve_bytes=0)
    with pytest.raises(ValueError,match='size_mismatch'): writer.write([b'a'],expected_bytes=2)
    with pytest.raises(ValueError,match='size_limit'): writer.write([b'123456789'])
    with pytest.raises(ValueError,match='hash_mismatch'): writer.write([b'a'],expected_sha256='0'*64)
    def interrupted():
        yield b'a'; raise RuntimeError('disconnected')
    with pytest.raises(RuntimeError): writer.write(interrupted())
    assert list(writer.root.iterdir())==[]
    blob=writer.write([b'ab',b'cd']); assert writer.write([b'abcd'])==blob
    (writer.root/blob.digest).write_bytes(b'xxxx')
    with pytest.raises(RuntimeError,match='corrupt'): writer.verify(blob)
    # A verified download of the same digest repairs the damaged copy.
    assert writer.write([b'abcd'])==blob and writer.verify(blob).read_bytes()==b'abcd'


def test_blob_disk_reserve(tmp_path,monkeypatch):
    import shutil
    monkeypatch.setattr(shutil,'disk_usage',lambda _:type('Usage',(),{'free':0})())
    writer=BlobWriter(tmp_path/'blobs',reserve_bytes=1)
    with pytest.raises(RuntimeError,match='disk_reserve'): writer.write([b'x'])
    assert list(writer.root.iterdir())==[]


@pytest.mark.parametrize('size_mib',[32,256])
def test_large_original_streaming(size_mib,tmp_path):
    writer=BlobWriter(tmp_path/'blobs',reserve_bytes=0)
    chunk=b'lighthouse-42---'*(1024*1024//16)
    expected=hashlib.sha256()
    for _ in range(size_mib): expected.update(chunk)
    blob=writer.write((chunk for _ in range(size_mib)),expected_bytes=size_mib*1024**2,
                      expected_sha256=expected.hexdigest())
    assert writer.verify(blob).stat().st_size==size_mib*1024**2


def test_migration_checksum_and_restart_preserve_revisions(archive):
    ar,s,key,_=archive
    ar.ingest(event()); s.migrate(); s.migrate()
    assert len(s.get_updates(key,'main')['items'])==1
    with s.pool.connection() as c:
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='58'
        saved=c.execute('SELECT sha256 FROM archive_migrations WHERE version=2').fetchone()['sha256']
        c.execute("UPDATE archive_migrations SET sha256='wrong' WHERE version=2")
    try:
        with pytest.raises(RuntimeError,match='checksum'): s.migrate()
    finally:
        with s.pool.connection() as c: c.execute('UPDATE archive_migrations SET sha256=%s WHERE version=2',(saved,))


UNREADABLE_FORMAT={'version':1,'state':'unknown','basis':None,'wire_sha256':None,'entities':None,
    'unsupported_entities':[],'warnings':['native_entities_not_captured']}


def wa_event(**changes):
    body=changes.get('body','Первый текст lighthouse')
    return event(**{'account':'archive-wa','platform':'whatsapp','chat':'900@lid','provider_id':'false_900@lid_3EB0SYNTH',
        'sender':'900@lid','formatting':{'version':1,'state':'wire_observed','basis':'whatsapp_native_wire',
            'wire_sha256':hashlib.sha256(body.encode()).hexdigest(),'entities':None,
            'unsupported_entities':[],'warnings':['syntax_interpretation_unknown']},
        'metadata':{'message_kind':'text','chat_type':'private'},**changes})


def unreadable(**changes):
    return wa_event(**{'body':'','media':[],'formatting':UNREADABLE_FORMAT,'origin':'reconcile',
        'metadata':{'message_kind':'unknown','chat_type':'private'},
        'sender_profile':{'display_name':'Synthetic Sender'},**changes})


def archive_state(s,mid):
    with s.pool.connection() as c:
        return (c.execute('SELECT sender,body,revision,deleted,content_hash FROM messages WHERE id=%s',(mid,)).fetchone(),
            [r['snapshot'] for r in c.execute('SELECT snapshot FROM message_revisions WHERE message=%s ORDER BY revision',(mid,))],
            c.execute('SELECT revision,metadata FROM message_metadata WHERE message=%s',(mid,)).fetchone(),
            [dict(r) for r in c.execute("SELECT id,state FROM media_jobs WHERE message=%s ORDER BY id",(mid,))],
            c.execute('SELECT count(*) AS n FROM events WHERE message=%s',(mid,)).fetchone()['n'],
            c.execute("SELECT count(*) AS n FROM sender_profiles WHERE account='archive-wa'").fetchone()['n'],
            c.execute('SELECT count(*) AS n FROM message_conflicts WHERE message=%s',(mid,)).fetchone()['n'])


# spec: ANYM-613
@pytest.mark.parametrize('media',[[],[{'provider_id':'f1','mime':'image/jpeg','bytes':10,'filename':'p.jpg'}]])
def test_unreadable_whatsapp_observation_is_not_a_conflict(archive,media):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    key=s.issue_key('wa-reader',['archive-wa'])
    mid=ar.ingest(wa_event(media=media))['message']
    before=archive_state(s,mid); shown=conversation_state(s,key,mid)
    assert ar.ingest(unreadable())['outcome']=='unchanged'
    # The retained revision, text, formatting, attachments and metadata stay; no revision,
    # no event, no conflict and no sender profile from the empty observation.
    assert archive_state(s,mid)==before
    assert before[2]['metadata']['message_kind']=='text' and before[5]==0 and before[6]==0
    assert conversation_state(s,key,mid)==shown
    assert shown[0]['body']=='Первый текст lighthouse' and len(before[3])==len(media)


def conversation_state(s,key,mid):
    shown=s.get_context(key,mid,view='conversation')
    with s.pool.connection() as c:
        chat=c.execute('SELECT title,chat_type FROM chats c JOIN messages m ON m.chat=c.id WHERE m.id=%s',(mid,)).fetchone()
    return shown['message'],shown['conversation_view'],chat


# spec: ANYM-613
def test_unreadable_whatsapp_observation_keeps_chat_and_presentation(archive):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    key=s.issue_key('wa-reader',['archive-wa'])
    mid=ar.ingest(wa_event(chat_title='Synthetic Contact'))['message']
    before=archive_state(s,mid); shown=conversation_state(s,key,mid)
    candidate=unreadable(chat_title='Other synthetic title',metadata={'message_kind':'unknown','chat_type':'group'})
    assert ar.ingest(candidate)['outcome']=='unchanged'
    assert archive_state(s,mid)==before and conversation_state(s,key,mid)==shown
    assert shown[2]=={'title':'Synthetic Contact','chat_type':'private'}


# spec: ANYM-613
def test_unreadable_whatsapp_observation_of_a_textless_message_stays_conflict(archive):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    ar.ingest(wa_event(body='',formatting=UNREADABLE_FORMAT,metadata={'message_kind':'photo','chat_type':'private'},
        media=[{'provider_id':'f1','mime':'image/jpeg','bytes':10,'filename':'p.jpg'}]))
    assert ar.ingest(unreadable())['outcome']=='conflict'


# spec: ANYM-613
@pytest.mark.parametrize('change,outcome',[
    ({'metadata':{'message_kind':'text','chat_type':'private'}},'conflict'),
    ({'metadata':{'message_kind':'document','chat_type':'private'}},'conflict'),
    ({'metadata':{'chat_type':'private'}},'conflict'),
    ({'metadata':{}},'conflict'),
    ({'media':[{'provider_id':'f2','mime':'image/jpeg','bytes':10,'filename':'q.jpg'}]},'conflict'),
    ({'body':'Другой текст','formatting':UNREADABLE_FORMAT},'conflict'),
    ({'deleted':True},'deleted'),
    ({'sent_at':'2026-10-01T11:59:59+00:00'},'conflict'),
    ({'direction':'outgoing'},'conflict'),
    ({'sender':'901@lid'},'conflict'),
    ({'updated_at':'2026-10-01T12:01:00+00:00'},'edited')])
def test_unreadable_whatsapp_rule_keeps_other_shapes(archive,change,outcome):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    ar.ingest(wa_event())
    candidate=unreadable(**change)
    if not candidate['metadata']:del candidate['metadata']
    assert ar.ingest(candidate)['outcome']==outcome


# spec: ANYM-613
def test_unreadable_whatsapp_observation_keeps_deletion_precedence(archive):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    deleted=ar.ingest(wa_event(provider_id='false_900@lid_3EB0GONE'))['message']
    assert ar.ingest(wa_event(provider_id='false_900@lid_3EB0GONE',deleted=True,body='',formatting=UNREADABLE_FORMAT,
        updated_at='2026-10-01T12:01:00+00:00'))['outcome']=='deleted'
    assert ar.ingest(unreadable(provider_id='false_900@lid_3EB0GONE',updated_at='2026-10-01T12:01:00+00:00'))['outcome']=='tombstoned'
    mid=ar.ingest(wa_event())['message']
    with s.pool.connection() as c:
        c.execute('''INSERT INTO provider_tombstones(account,provider_chat,provider_id,observed_at)
            VALUES('archive-wa','900@lid','false_900@lid_3EB0SYNTH','2026-10-01T12:00:00+00:00')''')
    assert ar.ingest(unreadable())['outcome']=='deleted'
    assert archive_state(s,mid)[0]['deleted'] and archive_state(s,deleted)[0]['deleted']


# spec: ANYM-613
def test_unreadable_observation_rule_is_whatsapp_only(archive):
    ar,s,key,_=archive
    ar.ingest(event(metadata={'message_kind':'text'}))
    assert ar.ingest(event(body='',metadata={'message_kind':'unknown'}))['outcome']=='conflict'


BUSINESS_TEXT='Ваш заказ 4821 готов lighthouse'
DOCUMENT=[{'provider_id':'f9','mime':'application/pdf','bytes':2048,'filename':'report.pdf'}]


def business(kind='template',**changes):
    body=changes.get('body',BUSINESS_TEXT)
    return wa_event(**{'body':body,'formatting':wire(body),'origin':'reconcile',
        'metadata':{'message_kind':'text','provider_message_type':kind,'chat_type':'private'},**changes})


def message_events(s,mid):
    with s.pool.connection() as c:
        return [(r['kind'],r['revision']) for r in c.execute('SELECT kind,revision FROM events WHERE message=%s ORDER BY id',(mid,))]


def open_conflicts(s,mid):
    with s.pool.connection() as c:
        return [r['reason'] for r in c.execute('''SELECT reason FROM message_conflicts WHERE message=%s
            AND resolved_at IS NULL ORDER BY observed_at''',(mid,))]


# spec: ANYM-560
@pytest.mark.parametrize('kind',['template','interactive','buttons','list','product','group_invite'])
@pytest.mark.parametrize('retained_kind',[{'message_kind':'unknown','chat_type':'private'},{'chat_type':'private'}])
def test_whatsapp_business_text_completes_an_empty_unknown_revision(archive,kind,retained_kind):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    key=s.issue_key('wa-reader',['archive-wa'])
    mid=ar.ingest(unreadable(origin='live',metadata=retained_kind))['message']
    assert ar.ingest(business(kind))['outcome']=='edited'
    # A completion revision: the empty one stays readable, the text one is current and searchable.
    message,snapshots,metadata,_,_,_,conflicts=archive_state(s,mid)
    assert message['body']==BUSINESS_TEXT and message['revision']==2 and conflicts==0
    assert [r['body'] for r in snapshots]==['',BUSINESS_TEXT]
    assert snapshots[0]['updated_at']==snapshots[1]['updated_at'] and snapshots[1]['formatting']==wire(BUSINESS_TEXT)
    assert metadata['revision']==2 and metadata['metadata']['provider_message_type']==kind
    assert message_events(s,mid)==[('message.created',1),('message.edited',2)]
    assert [i['id'] for i in s.search(key,'заказ')['items']]==[mid]
    # A replay is idempotent: the same receipt is a duplicate, another copy of the text is unchanged.
    assert ar.ingest(business(kind))['outcome']=='duplicate'
    assert ar.ingest(business(kind,origin='backfill'))['outcome']=='unchanged'
    assert archive_state(s,mid)[1]==snapshots and message_events(s,mid)==[('message.created',1),('message.edited',2)]


# spec: ANYM-560
@pytest.mark.parametrize('retained,candidate,outcome',[
    # Kinds outside the explicit list, including a plain chat message and a document (criterion 6).
    ({},{'metadata':{'message_kind':'text','provider_message_type':'chat','chat_type':'private'}},'conflict'),
    ({},{'metadata':{'message_kind':'document','provider_message_type':'document','chat_type':'private'}},'conflict'),
    ({},{'metadata':{'message_kind':'text','chat_type':'private'}},'conflict'),
    ({},{'metadata':{}},'conflict'),
    # The retained revision is typed, or already has text: never replaced.
    ({'metadata':{'message_kind':'text','chat_type':'private'}},{},'conflict'),
    ({'metadata':{'message_kind':'document','chat_type':'private'}},{},'conflict'),
    ({'body':'Старый текст','formatting':wire('Старый текст')},{},'conflict'),
    # Another identity, attachment set or provider time.
    ({},{'sender':'901@lid'},'conflict'),
    ({},{'direction':'outgoing'},'conflict'),
    ({},{'sent_at':'2026-10-01T11:59:59+00:00'},'conflict'),
    ({},{'media':DOCUMENT},'conflict'),
    ({'media':DOCUMENT},{},'conflict'),
    ({'updated_at':'2026-10-01T12:01:00+00:00'},{},'stale'),
    ({},{'updated_at':'2026-10-01T12:01:00+00:00'},'edited'),
    ({},{'deleted':True,'body':'','formatting':UNREADABLE_FORMAT},'deleted')])
def test_whatsapp_business_text_completion_keeps_other_shapes(archive,retained,candidate,outcome):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    mid=ar.ingest(unreadable(**{'origin':'live',**retained}))['message']
    before=archive_state(s,mid)[1]
    assert ar.ingest(business(**candidate))['outcome']==outcome
    if outcome in ('conflict','stale'):
        assert archive_state(s,mid)[1]==before and message_events(s,mid)==[('message.created',1)]
    if outcome=='conflict':assert len(open_conflicts(s,mid))==1


# spec: ANYM-560
def test_whatsapp_business_text_completion_needs_text_and_a_whatsapp_source(archive):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    # A candidate without text and a retained text are a conflict, the unreadable rule aside (ANYM-613).
    mid=ar.ingest(wa_event(metadata={'message_kind':'text','provider_message_type':'template','chat_type':'private'}))['message']
    assert ar.ingest(business(body='',formatting=UNREADABLE_FORMAT,media=DOCUMENT))['outcome']=='conflict'
    assert archive_state(s,mid)[0]['body']=='Первый текст lighthouse'
    # Telegram is not in scope.
    tg=ar.ingest(event(body='',metadata={'message_kind':'unknown'}))['message']
    assert ar.ingest(event(body=BUSINESS_TEXT,metadata={'message_kind':'text','provider_message_type':'template'}))['outcome']=='conflict'
    assert archive_state(s,tg)[0]['body']==''


# spec: ANYM-560
def test_whatsapp_business_text_completion_keeps_unrelated_open_conflicts(archive):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    mid=ar.ingest(unreadable(origin='live',updated_at='2026-10-01T12:00:30+00:00'))['message']
    # One contradiction at the retained version and one older one, both about other fields.
    assert ar.ingest(unreadable(sender='901@lid',updated_at='2026-10-01T12:00:30+00:00'))['outcome']=='conflict'
    assert ar.ingest(unreadable(direction='outgoing'))['outcome']=='conflict'
    before=open_conflicts(s,mid)
    assert before==['whatsapp_sender_equal_version','whatsapp_direction_older_version']
    assert ar.ingest(business(updated_at='2026-10-01T12:00:30+00:00'))['outcome']=='edited'
    # The completion is no later provider evidence: neither conflict is resolved.
    assert open_conflicts(s,mid)==before and archive_state(s,mid)[0]['revision']==2


# spec: ANYM-560
@pytest.mark.parametrize('order',['whatsmeow_first','older_adapter_first'])
def test_whatsapp_document_file_name_is_never_stored_as_completed_text(archive,order):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    # whatsmeow: an empty-caption document; the older adapter: its file name as the body (class C-62a).
    caption=wa_event(body='',formatting=UNREADABLE_FORMAT,media=DOCUMENT,
        metadata={'message_kind':'document','provider_message_type':'document','chat_type':'private'})
    named=wa_event(body='report.pdf',formatting=UNREADABLE_FORMAT,media=DOCUMENT,origin='backfill',metadata={'chat_type':'private'})
    first,second=(caption,named) if order=='whatsmeow_first' else (named,caption)
    mid=ar.ingest(first)['message']
    assert ar.ingest(second)['outcome']=='conflict'
    assert archive_state(s,mid)[0]['body']==first['body'] and message_events(s,mid)==[('message.created',1)]
    assert open_conflicts(s,mid)==['whatsapp_body_equal_version']


# spec: ANYM-560
def test_whatsapp_body_completion_rule_is_a_pure_function_of_two_observations():
    from messaging_service.archive import whatsapp_body_completion
    def snapshot(value):
        return {**validate(value),'message':'m','provider_chat':'900@lid','chat':'c','revision':1}
    retained=snapshot(unreadable(origin='live'))
    assert whatsapp_body_completion(retained,validate(business()))
    assert not whatsapp_body_completion(retained,validate(business('chat')))
    assert not whatsapp_body_completion(snapshot(business()),validate(business()))
    # A retained formatting that states text the revision does not have contradicts the completion.
    assert not whatsapp_body_completion({**retained,'formatting':wire('report.pdf')},validate(business()))
    for broken in (None,{},{**retained,'metadata':'unknown'},{**retained,'updated_at':'not a time'}):
        assert not whatsapp_body_completion(broken,validate(business()))


def group_formatting(entities=None,body='Первый текст lighthouse'):
    return {'version':1,'state':'observed','basis':'telegram_native_entities',
        'wire_sha256':hashlib.sha256(body.encode()).hexdigest(),
        'entities':entities if entities is not None else [{'type':'bold','offset':0,'length':6}],
        'unsupported_entities':[],'warnings':[]}


PREVIEW_PHOTO=[{'provider_id':'300','mime':'image/jpeg','bytes':4,'filename':'17.jpg'}]
PREVIEW_DOCUMENT=[{'provider_id':'200','mime':'application/pdf','bytes':1234,'filename':'document.pdf'}]


def preview(page='20',kind='webpage',**changes):
    """A Telegram message with a link preview, its photo or document stored as the message's media."""
    metadata={'message_kind':'photo','provider_message_type':kind,'chat_type':'private',
        **({'web_preview_id':page} if page is not None else {})}
    return event(**{'media':PREVIEW_PHOTO,'formatting':group_formatting([]),'metadata':metadata,**changes})


# spec: ANYM-611
@pytest.mark.parametrize('retained_media,candidate_media',[(PREVIEW_PHOTO,PREVIEW_DOCUMENT),([],PREVIEW_PHOTO),(PREVIEW_PHOTO,[])],
    ids=['another_attachment','attachment_added','attachment_dropped'])
def test_refreshed_telegram_link_preview_of_the_same_page_is_unchanged(archive,retained_media,candidate_media):
    ar,s,_,_=archive
    mid=ar.ingest(preview(media=retained_media))['message']
    before=archive_state(s,mid)
    candidate=preview(media=candidate_media,origin='reconcile',
        metadata={'message_kind':'document','provider_message_type':'webpage','chat_type':'private','web_preview_id':'20'})
    assert ar.ingest(candidate)['outcome']=='unchanged'
    # The retained revision, its metadata, media and attachments stay; no revision, no event, no conflict.
    assert archive_state(s,mid)==before and before[1][0]['media']==retained_media
    assert message_events(s,mid)==[('message.created',1)] and open_conflicts(s,mid)==[]
    assert ar.ingest(candidate)['outcome']=='duplicate'


# spec: ANYM-611
def test_link_preview_refresh_keeps_the_downloaded_attachment(archive):
    ar,s,key,_=archive
    mid=ar.ingest(preview())['message']
    job=ar.claim_media('worker'); writer=BlobWriter(s.files,reserve_bytes=0)
    ar.finish_media(job,writer.write([b'jpeg'],expected_bytes=4),writer)
    def attachments():
        with s.pool.connection() as c:
            return [dict(r) for r in c.execute('SELECT id,digest,filename,mime,bytes,active FROM attachments WHERE message=%s',(mid,))]
    before=attachments(); state=archive_state(s,mid)
    assert len(before)==1 and before[0]['active'] and s.get_attachment(key,job['id'])['status']=='ready'
    assert ar.ingest(preview(media=PREVIEW_DOCUMENT,origin='reconcile'))['outcome']=='unchanged'
    # The retained attachment stays active and downloadable; no job for the refreshed preview is queued.
    assert attachments()==before and archive_state(s,mid)==state and ar.claim_media('worker') is None
    assert s.get_attachment(key,job['id'])['status']=='ready'


# spec: ANYM-611
def test_link_preview_refresh_compares_the_stored_provider_version(archive):
    ar,s,_,_=archive
    # An identical re-read at a later edit time advances the stored provider version past the snapshot's.
    mid=ar.ingest(preview())['message']
    assert ar.ingest(preview(updated_at='2026-10-01T12:00:30+00:00',origin='backfill'))['outcome']=='unchanged'
    before=archive_state(s,mid)
    assert ar.ingest(preview(media=PREVIEW_DOCUMENT,updated_at='2026-10-01T12:00:30+00:00'))['outcome']=='unchanged'
    assert archive_state(s,mid)==before and open_conflicts(s,mid)==[]
    # The snapshot's own time is now older than the stored version: stale, as before.
    assert ar.ingest(preview(media=PREVIEW_DOCUMENT,origin='reconcile'))['outcome']=='stale'


# spec: ANYM-611
def test_link_preview_refresh_rule_is_a_pure_function_of_two_observations():
    from messaging_service.archive import telegram_web_preview_refresh
    def snapshot(value):
        return {**validate(value),'message':'m','provider_chat':'100','chat':'c','revision':1}
    retained=snapshot(preview()); version=datetime(2026,10,1,12,tzinfo=timezone.utc)
    assert telegram_web_preview_refresh(retained,validate(preview(media=PREVIEW_DOCUMENT)),version)
    assert not telegram_web_preview_refresh(retained,validate(preview()),version)
    assert not telegram_web_preview_refresh(retained,validate(preview(media=PREVIEW_DOCUMENT)),None)
    assert not telegram_web_preview_refresh(retained,validate(preview(media=PREVIEW_DOCUMENT)),version+timedelta(seconds=1))
    for broken in (None,{},{**retained,'metadata':'webpage'},{**retained,'media':None},{**retained,'deleted':True}):
        assert not telegram_web_preview_refresh(broken,validate(preview(media=PREVIEW_DOCUMENT)),version)


# spec: ANYM-611
@pytest.mark.parametrize('retained,candidate,outcome',[
    ({},{'page':'21'},'conflict'),
    ({},{'page':None},'conflict'),
    ({'page':None},{},'conflict'),
    ({},{'kind':'photo'},'conflict'),
    ({'kind':'photo'},{},'conflict'),
    ({},{'sent_at':'2026-10-01T11:59:59+00:00'},'conflict'),
    ({},{'direction':'outgoing'},'conflict'),
    ({},{'sender':'43'},'conflict'),
    ({},{'body':'Другой текст','formatting':group_formatting([],'Другой текст')},'conflict'),
    ({},{'formatting':group_formatting()},'conflict'),
    ({},{'updated_at':'2026-10-01T12:01:00+00:00'},'edited'),
    ({'updated_at':'2026-10-01T12:01:00+00:00'},{},'stale'),
    ({},{'deleted':True,'body':'','media':[],'formatting':group_formatting([],'')},'deleted')],
    ids=['another_page','candidate_without_page','retained_without_page','candidate_not_webpage','retained_not_webpage',
        'sent_at','direction','sender','body','formatting','newer_version','older_version','deleted'])
def test_link_preview_refresh_needs_the_same_page_and_nothing_else_changed(archive,retained,candidate,outcome):
    ar,s,_,_=archive
    mid=ar.ingest(preview(**retained))['message']
    before=archive_state(s,mid)[1]
    assert ar.ingest(preview(**{'media':PREVIEW_DOCUMENT,'origin':'reconcile',**candidate}))['outcome']==outcome
    if outcome=='conflict':
        assert archive_state(s,mid)[1]==before and message_events(s,mid)==[('message.created',1)]
        assert len(open_conflicts(s,mid))==1


# spec: ANYM-611
def test_link_preview_refresh_keeps_deletion_precedence_and_open_conflicts(archive):
    ar,s,_,_=archive
    # A deleted retained message stays deleted: a refresh does not resurrect it.
    gone=ar.ingest(preview(provider_id='18'))['message']
    assert ar.ingest(preview(provider_id='18',deleted=True,body='',media=[],formatting=group_formatting([],''),
        updated_at='2026-10-01T12:01:00+00:00'))['outcome']=='deleted'
    assert ar.ingest(preview(provider_id='18',media=PREVIEW_DOCUMENT,updated_at='2026-10-01T12:01:00+00:00'))['outcome']=='tombstoned'
    # A provider tombstone turns the refresh into a deletion, as before.
    mid=ar.ingest(preview())['message']
    with s.pool.connection() as c:
        c.execute('''INSERT INTO provider_tombstones(account,provider_chat,provider_id,observed_at)
            VALUES('archive-test','100','17','2026-10-01T12:00:00+00:00')''')
    assert ar.ingest(preview(media=PREVIEW_DOCUMENT))['outcome']=='deleted'
    assert archive_state(s,mid)[0]['deleted'] and archive_state(s,gone)[0]['deleted']
    # A refresh is no later evidence: an open older contradiction stays open.
    other=ar.ingest(preview(provider_id='19',updated_at='2026-10-01T12:00:30+00:00'))['message']
    assert ar.ingest(preview(provider_id='19',sender='43'))['outcome']=='conflict'
    assert ar.ingest(preview(provider_id='19',media=PREVIEW_DOCUMENT,updated_at='2026-10-01T12:00:30+00:00'))['outcome']=='unchanged'
    assert len(open_conflicts(s,other))==1


GROUP_SELF={'chat_type':'group','sender_identity_basis':'authenticated_self'}


# spec: ANYM-620
@pytest.mark.parametrize('retained_type',[None,'group'])
def test_authenticated_group_sender_completion_resolves_its_conflict(archive,retained_type):
    ar,s,key,_=archive
    original=event(sender='',direction='outgoing',metadata={'chat_type':retained_type},formatting=group_formatting())
    mid=ar.ingest(original)['message']
    completed={**original,'sender':'42','sender_profile':{'display_name':'Synthetic Owner'},'metadata':GROUP_SELF}
    # Today's class E: the same content naming a sender without the authenticated basis.
    assert ar.ingest({**completed,'metadata':{'chat_type':'group','sender_identity_basis':None}})['outcome']=='conflict'
    assert ar.ingest({**completed,'body':'unrelated conflicting text',
        'formatting':group_formatting(body='unrelated conflicting text')})['outcome']=='conflict'
    assert ar.ingest(completed)['outcome']=='edited'
    assert ar.ingest(completed)['outcome']=='duplicate'
    message=s.get_context(key,mid)['message']
    assert message['sender']=='42' and message['revision']==2
    assert message['sender_identity_basis']=='authenticated_self'
    assert message['formatting']['entities']==group_formatting()['entities']
    assert message['evidence']['active_conflicts']==1
    with s.pool.connection() as c:
        snapshots=c.execute('SELECT snapshot FROM message_revisions WHERE message=%s ORDER BY revision',(mid,)).fetchall()
        conflicts=c.execute('''SELECT r.envelope->>'body' AS body,mc.resolved_revision FROM message_conflicts mc
            JOIN ingest_receipts r ON r.digest=mc.receipt_digest WHERE mc.message=%s ORDER BY mc.observed_at''',(mid,)).fetchall()
    assert [r['snapshot']['sender'] for r in snapshots]==['','42']
    assert [(r['body'],r['resolved_revision']) for r in conflicts]==[
        ('Первый текст lighthouse',2),('unrelated conflicting text',None)]
    assert [r['revision'] for r in s.get_updates(key,'history')['items']]==[1,2]


# spec: ANYM-620
@pytest.mark.parametrize('change',[
    {'metadata':{'chat_type':'group'}},
    {'sender':'77','metadata':{'chat_type':'group'}},
    {'sender':'-100300','metadata':{'chat_type':'group'}},
    {'metadata':{'chat_type':'channel','sender_identity_basis':'authenticated_self'}},
    {'direction':'incoming'}, {'body':'changed content','formatting':group_formatting(body='changed content')},
    {'media':[{'provider_id':'f1','mime':'image/jpeg','bytes':10,'filename':'p.jpg'}]},
    {'updated_at':'2026-10-01T12:01:00Z'}, {'deleted':True},
    {'formatting':group_formatting([{'type':'italic','offset':0,'length':6}])}],
    ids=['no_basis','other_user','anonymous_admin','channel','incoming','body','media','version','deleted','formatting'])
def test_group_sender_completion_keeps_other_differences_in_conflict(archive,change):
    ar,s,key,_=archive
    original=event(sender='',direction='outgoing',metadata={'chat_type':None},formatting=group_formatting())
    mid=ar.ingest(original)['message']
    assert ar.ingest({**original,'sender':'42','metadata':GROUP_SELF,**change})['outcome']=='conflict'
    message=s.get_context(key,mid)['message']
    assert message['sender']=='' and message['revision']==1


# spec: ANYM-620
@pytest.mark.parametrize('sender,chat_type',[('77','group'),('','private'),('','channel')])
def test_group_sender_completion_needs_an_empty_group_sender(archive,sender,chat_type):
    ar,s,key,_=archive
    original=event(sender=sender,direction='outgoing',metadata={'chat_type':chat_type})
    mid=ar.ingest(original)['message']
    assert ar.ingest({**original,'sender':'42','metadata':GROUP_SELF})['outcome']=='conflict'
    assert s.get_context(key,mid)['message']['sender']==sender


# spec: ANYM-620
def test_group_sender_completion_never_completes_a_deleted_message(archive):
    ar,s,key,_=archive
    original=event(sender='',direction='outgoing',metadata={'chat_type':None})
    mid=ar.ingest(original)['message']
    assert ar.ingest({**original,'deleted':True,'body':'','updated_at':'2026-10-01T12:01:00Z'})['outcome']=='deleted'
    # A retained deletion keeps its tombstone precedence: the candidate writes no revision and no sender.
    candidate={**original,'sender':'42','metadata':GROUP_SELF,'updated_at':'2026-10-01T12:01:00Z'}
    assert ar.ingest(candidate)['outcome']=='tombstoned'
    with s.pool.connection() as c:
        row=c.execute('SELECT sender,revision,deleted FROM messages WHERE id=%s',(mid,)).fetchone()
    assert (row['sender'],row['revision'],row['deleted'])==('',2,True)


PN='79990000001@c.us'
LID='310000000001@lid'


def observe(s,source,pn=PN,lid=LID,account='archive-wa',provenance='whatsmeow_lid_store_v1'):
    # As whatsapp_identity.record stores it: the observed identity is the source message's sender.
    from messaging_service.whatsapp_identity import personal_id
    with s.pool.connection() as c:
        sender=c.execute('SELECT sender FROM messages WHERE id=%s',(source,)).fetchone()['sender']
        assert personal_id(sender) in (pn,lid)
        c.execute('''INSERT INTO whatsapp_identity_observations(account,source_message,identity_id,pn,lid,provenance,
            first_observed_at,observed_at) VALUES(%s,%s,%s,%s,%s,%s,'2026-10-01T11:00:00Z','2026-10-01T11:00:00Z')''',
            (account,source,sender,pn,lid,provenance))


def alias_event(sender,**changes):
    return whatsapp_event(sender,**{'direction':'incoming','formatting':wire(),**changes})


# spec: ANYM-559
@pytest.mark.parametrize('retained,candidate',[(PN,LID),(LID,PN),('79990000001:4@c.us',LID),(PN,'310000000001:2@lid')])
def test_whatsapp_sender_in_two_address_forms_is_not_a_conflict(archive,retained,candidate):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    mid=ar.ingest(alias_event(retained))['message']
    observe(s,mid)
    observed=alias_event(candidate,origin='reconcile',sender_profile={'display_name':'Synthetic Sender'})
    assert ar.ingest(observed)['outcome']=='unchanged'
    # The retained sender and revision stay, the receipt keeps the raw form, and the other form gets no profile.
    assert sender_rows(s,mid)==(retained,1,[],0)
    with s.pool.connection() as c:
        envelope=c.execute("SELECT envelope FROM ingest_receipts WHERE outcome='unchanged'").fetchone()['envelope']
        snapshots=c.execute('SELECT snapshot FROM message_revisions WHERE message=%s',(mid,)).fetchall()
    assert envelope['sender']==candidate and [r['snapshot']['sender'] for r in snapshots]==[retained]


# spec: ANYM-559
@pytest.mark.parametrize('evidence',['none','other_account','two_lids','two_pns','deleted_source_conflict','lid_only'])
def test_whatsapp_sender_alias_needs_admissible_evidence(archive,evidence):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    ar.register('archive-wb','whatsapp','Other synthetic WhatsApp',include_chats=['900@lid'])
    mid=ar.ingest(alias_event(PN))['message']
    if evidence=='lid_only':
        # Two LID forms are no phone-number/LID pair (class F, ANYM-561).
        lid_mid=ar.ingest(alias_event(LID,provider_id='true_900@lid_3EB0LIDONLY',direction='outgoing'))['message']
        observe(s,lid_mid)
        assert ar.ingest(alias_event('310000000002@lid',provider_id='true_900@lid_3EB0LIDONLY',direction='outgoing'))['outcome']=='conflict'
        assert sender_rows(s,lid_mid)==(LID,1,[],1)
        return
    if evidence=='other_account':
        observe(s,ar.ingest(alias_event(PN,account='archive-wb'))['message'],account='archive-wb')
    elif evidence!='none':observe(s,mid)
    if evidence=='two_lids':observe(s,mid,lid='310000000002@lid')
    if evidence=='two_pns':
        observe(s,ar.ingest(alias_event(LID,provider_id='true_900@lid_3EB0TWOPN'))['message'],pn='79990000002@c.us')
    if evidence=='deleted_source_conflict':
        # A conflicting mapping whose source message was deleted still fences the pair.
        gone=ar.ingest(alias_event(PN,provider_id='true_900@lid_3EB0GONE'))['message']
        observe(s,gone,lid='310000000002@lid')
        deletion=alias_event(PN,provider_id='true_900@lid_3EB0GONE',deleted=True,body='',updated_at='2026-10-01T12:01:00+00:00')
        del deletion['formatting']
        assert ar.ingest(deletion)['outcome']=='deleted'
    assert ar.ingest(alias_event(LID))['outcome']=='conflict'
    assert sender_rows(s,mid)==(PN,1,[],1)
    assert stored_conflicts(s,mid)==['whatsapp_sender_equal_version']


# spec: ANYM-559
def test_whatsapp_unknown_sender_is_not_a_conflict_and_never_replaces_the_sender(archive):
    ar,s,key,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    mid=ar.ingest(alias_event(PN))['message']
    assert ar.ingest(alias_event('',origin='backfill'))['outcome']=='unchanged'
    assert sender_rows(s,mid)==(PN,1,[],0)
    # A later legitimate edit without a sender keeps the known sender in its revision.
    assert ar.ingest(alias_event('',body='Вторая версия',formatting=wire('Вторая версия'),
        updated_at='2026-10-01T12:01:00+00:00'))['outcome']=='edited'
    assert sender_rows(s,mid)==(PN,2,[],0)
    with s.pool.connection() as c:
        snapshots=c.execute('SELECT snapshot FROM message_revisions WHERE message=%s ORDER BY revision',(mid,)).fetchall()
    assert [r['snapshot']['sender'] for r in snapshots]==[PN,PN]


# spec: ANYM-559
@pytest.mark.parametrize('change,outcome',[
    ({'body':'contradiction','formatting':wire('contradiction')},'conflict'),
    ({'media':[{'provider_id':'f1','mime':'image/jpeg','bytes':10,'filename':'p.jpg'}]},'conflict'),
    ({'sent_at':'2026-10-01T12:00:07+00:00','updated_at':'2026-10-01T12:00:07+00:00',
        'metadata':{'timestamp_resolution':'second'}},'conflict'),
    ({'sent_at':'2026-10-01T12:00:01+00:00','updated_at':'2026-10-01T12:00:01+00:00',
        'metadata':{'timestamp_resolution':'second'}},'conflict'),
    ({'direction':'outgoing'},'conflict'),
    ({'formatting':'retained_unknown'},'conflict'),
    ({'deleted':True,'body':'','formatting':None},'conflict'),
    ({'body':'older text','formatting':wire('older text'),'retained_later':True},'stale')])
@pytest.mark.parametrize('sender',['',LID])
def test_whatsapp_sender_rule_lifts_only_the_sender_difference(archive,change,outcome,sender):
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    retained=alias_event(PN,metadata={'timestamp_resolution':'second'});change=dict(change)
    if change.get('formatting')=='retained_unknown':
        retained['formatting']=UNREADABLE_FORMAT;change={}
    if change.pop('retained_later',False):retained['updated_at']='2026-10-01T12:01:00+00:00'
    mid=ar.ingest(retained)['message']
    observe(s,mid)
    observed={**alias_event(sender),**change}
    if observed.get('formatting') is None:observed.pop('formatting',None)
    assert ar.ingest(observed)['outcome']==outcome
    assert sender_rows(s,mid)[:2]==(PN,1)


# spec: ANYM-559
def test_whatsapp_sender_rule_keeps_open_conflicts_and_telegram_completion(archive):
    ar,s,key,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    mid=ar.ingest(alias_event(PN))['message']
    observe(s,mid)
    assert ar.ingest(alias_event('901@lid'))['outcome']=='conflict'
    # A re-read in the other form at a later version states nothing about the open conflict.
    assert ar.ingest(alias_event(LID,updated_at='2026-10-01T12:01:00+00:00'))['outcome']=='unchanged'
    assert sender_rows(s,mid)==(PN,1,[],1)
    # Telegram's sender completion is unchanged: private outgoing, authenticated self only.
    original=event(sender='',direction='outgoing',metadata={'chat_type':'private'})
    tg=ar.ingest(original)['message']
    assert ar.ingest({**original,'sender':'42','metadata':{'chat_type':'group','sender_identity_basis':'authenticated_self'}})['outcome']=='conflict'
    assert ar.ingest({**original,'sender':'42','metadata':{'chat_type':'private','sender_identity_basis':'authenticated_self'}})['outcome']=='edited'
    assert s.get_context(key,tg)['message']['sender']=='42'
    # A Telegram candidate without a sender is not covered by the WhatsApp rule.
    tg2=ar.ingest(event(provider_id='19'))['message']
    assert ar.ingest(event(provider_id='19',sender=''))['outcome']=='conflict'
    assert s.get_context(key,tg2)['message']['sender']=='42'


# spec: ANYM-559
def test_identity_record_waits_for_the_account_lock(archive):
    import threading
    from messaging_service.whatsapp_identity import record
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    envelope=validate(alias_event(PN))
    mid=ar.ingest(envelope)['message']
    values=[{'identity_id':PN,'pn':PN,'lid':LID,'provenance':'whatsmeow_lid_store_v1','observed_at':'2026-10-01T11:00:00+00:00'}]
    done=threading.Event()
    with s.pool.connection() as c:
        # The lock ingest holds while it reads the account's observations.
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:archive-wa',))
        writer=threading.Thread(target=lambda:(record(ar,envelope,values,None),done.set()))
        writer.start()
        assert not done.wait(1.5)
        with s.pool.connection() as probe:
            assert probe.execute('SELECT count(*) AS n FROM whatsapp_identity_observations').fetchone()['n']==0
    writer.join(10)
    assert done.is_set()
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM whatsapp_identity_observations WHERE source_message=%s',(mid,)).fetchone()['n']==1


# spec: ANYM-559
def test_whatsapp_alias_capture_with_a_contradicting_observation_stays_a_conflict_and_fences(archive):
    from messaging_service.whatsapp_collector import WhatsAppState
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    state=WhatsAppState(ar,'archive-wa',('fixture',ar.acquire('archive-wa','fixture')))
    def message(sender,pn,**changes):
        return {'kind':'message','peer_kind':'user','envelope':alias_event(sender,**changes),
            'identity_observations':[{'identity_id':sender,'pn':pn,'lid':LID,'provenance':'whatsmeow_message_alt_v1',
                'observed_at':'2026-10-01T11:00:00Z'}]}
    mid=state.apply(message(PN,PN))['message']
    assert state.apply(message(LID,PN,origin='reconcile'))['outcome']=='unchanged'
    # An edit in the LID form whose own observation links the LID to another phone number: the
    # capture's evidence decides with the recorded one, and its contradiction is kept as a fence.
    edit=message(LID,'79990000002@c.us',body='Вторая версия',formatting=wire('Вторая версия'),
        updated_at='2026-10-01T12:01:00+00:00')
    assert state.apply(edit)['outcome']=='conflict'
    assert state.apply(edit)['outcome']=='duplicate'
    with s.pool.connection() as c:
        fence=c.execute("SELECT source_message,identity_id FROM whatsapp_identity_observations WHERE pn='79990000002@c.us'").fetchall()
    assert [(r['source_message'],r['identity_id']) for r in fence]==[(mid,LID)]
    assert sender_rows(s,mid)[:2]==(PN,1)
    # Every later re-read in the other form now stays a conflict, also with consistent evidence.
    assert state.apply(message(LID,PN,origin='backfill'))['outcome']=='conflict'


# spec: ANYM-559
def test_whatsapp_sender_alias_lookup_is_bounded_by_index_ranges(archive):
    import json
    from messaging_service.whatsapp_identity import SAME_SENDER_SQL,PROVENANCES,same_sender
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    sources=[ar.ingest(alias_event(PN,provider_id=f'true_900@lid_3EB0{n:04d}'))['message'] for n in range(120)]
    for source in sources:observe(s,source)
    with s.pool.connection() as c:
        # Many repeats of one pair are linked and not fenced.
        assert same_sender(c,'archive-wa',PN,LID)
        c.execute('SET LOCAL enable_seqscan=off');c.execute('SET LOCAL enable_bitmapscan=off')
        plan=c.execute('EXPLAIN (ANALYZE, FORMAT JSON) '+SAME_SENDER_SQL,{'account':'archive-wa','pn':PN,'lid':LID,
            'provenances':sorted(PROVENANCES)}).fetchone()
        nodes=[];pending=[(plan.get('QUERY PLAN') or next(iter(plan.values())))[0]['Plan']]
        while pending:
            node=pending.pop();nodes.append(node);pending.extend(node.get('Plans',[]))
        scans=[n for n in nodes if n.get('Relation Name')=='whatsapp_identity_observations']
        # Each probe reads at most its first row; none filters through the pair's repeats.
        assert len(scans)==5 and all(n['Actual Rows']<=1 and n.get('Rows Removed by Filter',0)==0 for n in scans),json.dumps(scans)
    observe(s,sources[1],lid='310000000002@lid')
    with s.pool.connection() as c:
        assert not same_sender(c,'archive-wa',PN,LID)


def alias_capture(sender,pn,**changes):
    return {'kind':'message','peer_kind':'user','envelope':alias_event(sender,**changes),
        'identity_observations':[{'identity_id':sender,'pn':pn,'lid':LID,'provenance':'whatsmeow_message_alt_v1',
            'observed_at':'2026-10-01T11:00:00Z'}]}


def fence_rows(s):
    with s.pool.connection() as c:
        return [(r['source_message'],r['identity_id'],r['pn']) for r in c.execute(
            "SELECT source_message,identity_id,pn FROM whatsapp_identity_observations WHERE pn<>%s ORDER BY 1,2,3",(PN,))]


# spec: ANYM-559
def test_whatsapp_duplicate_capture_with_new_contradicting_evidence_still_fences(archive):
    from messaging_service.whatsapp_collector import WhatsAppState
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    state=WhatsAppState(ar,'archive-wa',('fixture',ar.acquire('archive-wa','fixture')))
    mid=state.apply(alias_capture(PN,PN))['message']
    assert state.apply(alias_capture(LID,PN,origin='reconcile'))['outcome']=='unchanged'
    # The same envelope again, now with evidence linking the LID to another phone number: the receipt
    # is a duplicate, the contradiction is still kept, once, and replaying it changes nothing.
    changed=alias_capture(LID,'79990000002@c.us',origin='reconcile')
    assert state.apply(changed)['outcome']=='duplicate'
    assert state.apply(changed)['outcome']=='duplicate'
    assert fence_rows(s)==[(mid,LID,'79990000002@c.us')]
    with s.pool.connection() as c:
        from messaging_service.whatsapp_identity import same_sender
        assert not same_sender(c,'archive-wa',PN,LID)
    # A later alias edit is then a conflict, not an edit under the old mapping.
    assert state.apply(alias_capture(LID,PN,body='Вторая версия',formatting=wire('Вторая версия'),
        updated_at='2026-10-01T12:01:00+00:00'))['outcome']=='conflict'
    assert sender_rows(s,mid)[:2]==(PN,1)


# spec: ANYM-559
def test_whatsapp_capture_evidence_without_a_recorded_pair_is_no_fence_and_no_link(archive):
    from messaging_service.whatsapp_collector import WhatsAppState
    from messaging_service.whatsapp_identity import same_sender
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    state=WhatsAppState(ar,'archive-wa',('fixture',ar.acquire('archive-wa','fixture')))
    # A message with no evidence, then a re-read in the LID form carrying a pair nobody recorded yet:
    # nothing contradicts, so ingest stores nothing, and the alias is not accepted on its own evidence.
    mid=ar.ingest(alias_event(PN))['message']
    assert state.apply(alias_capture(LID,'79990000002@c.us',body='Вторая версия',formatting=wire('Вторая версия'),
        updated_at='2026-10-01T12:01:00+00:00'))['outcome']=='conflict'
    assert fence_rows(s)==[]
    with s.pool.connection() as c:
        assert not same_sender(c,'archive-wa','79990000002@c.us',LID) and not same_sender(c,'archive-wa',PN,LID)
    assert sender_rows(s,mid)[:2]==(PN,1)


# spec: ANYM-559
@pytest.mark.parametrize('removal',['deleted','provider_tombstone'])
def test_whatsapp_deleted_message_supplies_no_new_identity_fence(archive,removal):
    from messaging_service.whatsapp_collector import WhatsAppState
    from messaging_service.whatsapp_identity import same_sender
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    state=WhatsAppState(ar,'archive-wa',('fixture',ar.acquire('archive-wa','fixture')))
    mid=state.apply(alias_capture(PN,PN))['message']
    other=state.apply(alias_capture(PN,PN,provider_id='true_900@lid_3EB0OTHER'))['message']
    assert state.apply(alias_capture(LID,PN,provider_id='true_900@lid_3EB0OTHER',origin='reconcile'))['outcome']=='unchanged'
    if removal=='deleted':
        deletion=alias_event(PN,provider_id='true_900@lid_3EB0OTHER',deleted=True,body='',updated_at='2026-10-01T12:01:00+00:00')
        del deletion['formatting']
        assert ar.ingest(deletion)['outcome']=='deleted'
    else:
        with s.pool.connection() as c:
            c.execute('''INSERT INTO provider_tombstones(account,provider_chat,provider_id,observed_at)
                VALUES('archive-wa','900@lid','true_900@lid_3EB0OTHER','2026-10-01T12:01:00+00:00')''')
    # Replays of that message with new contradicting evidence store nothing, so the valid pair stays.
    assert state.apply(alias_capture(LID,'79990000002@c.us',provider_id='true_900@lid_3EB0OTHER',
        origin='reconcile'))['outcome']=='duplicate'
    assert state.apply(alias_capture(LID,'79990000002@c.us',provider_id='true_900@lid_3EB0OTHER',
        origin='backfill'))['outcome']==('tombstoned' if removal=='deleted' else 'deleted')
    assert fence_rows(s)==[] and other!=mid
    with s.pool.connection() as c:
        assert same_sender(c,'archive-wa',PN,LID)


# spec: ANYM-559
def test_whatsapp_tombstoned_message_replay_supplies_no_new_identity_fence(archive):
    from messaging_service.whatsapp_collector import WhatsAppState
    from messaging_service.whatsapp_identity import same_sender
    ar,s,_,_=archive
    ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    state=WhatsAppState(ar,'archive-wa',('fixture',ar.acquire('archive-wa','fixture')))
    capture=alias_capture(PN,PN)
    mid=state.apply(capture)['message']
    # The provider removed the message; the local row is not deleted yet.
    with s.pool.connection() as c:
        c.execute('''INSERT INTO provider_tombstones(account,provider_chat,provider_id,observed_at)
            VALUES('archive-wa','900@lid',%s,'2026-10-01T12:01:00+00:00')''',(capture['envelope']['provider_id'],))
    # The identical envelope again, with evidence linking its sender to another LID, stores nothing.
    replay={**capture,'identity_observations':[{**capture['identity_observations'][0],'lid':'310000000002@lid'}]}
    assert state.apply(replay)['outcome']=='duplicate'
    with s.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM whatsapp_identity_observations WHERE lid<>%s",(LID,)).fetchone()['n']==0
        assert same_sender(c,'archive-wa',PN,LID)
    assert sender_rows(s,mid)[:2]==(PN,1)
