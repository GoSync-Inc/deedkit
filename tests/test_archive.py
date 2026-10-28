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
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='55'
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
