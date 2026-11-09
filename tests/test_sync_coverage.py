"""Per-account coverage under the current collection policy (ANYM-812). Synthetic rows only."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

import pytest

from messaging_service import coverage
from messaging_service.capabilities import CAPABILITY_SECTIONS
from messaging_service.collector_state import CollectorState
from messaging_service.enrichment import EnrichmentWorker
from messaging_service.enrichment_health import NO_WORKER_MAX_AGE_S
from messaging_service.enrichment_providers import profile_hash, validate_profile
from messaging_service.envelope import stable_id
from messaging_service.file_index import FileIndexer
from messaging_service.media_policy import recognition_withheld
from messaging_service.presentation import render
from messaging_service.sync_status import get_sync_status
from test_archive import archive as archive, event


ACCOUNT = 'archive-test'
WA = 'coverage-wa'
_serial = iter(range(10**9))


def publish(ar, account=ACCOUNT, *, days=7, since=None, chats=(('100', 'user'), ('200', 'chat'))):
    """The collector run's lease and window, and each chat's confirmed bound."""
    epoch = ar.acquire(account, 'coverage-owner')
    state = CollectorState(ar, account, ('coverage-owner', epoch))
    state.media_policy(days, since)
    with ar.store.pool.connection() as c:
        for chat, kind in chats:
            c.execute('''INSERT INTO collector_chats(account,provider_chat,peer_kind,history_since)
                VALUES(%s,%s,%s,%s) ON CONFLICT(account,provider_chat) DO UPDATE SET peer_kind=excluded.peer_kind,
                history_since=excluded.history_since''', (account, chat, kind, since))
    return state


def message(ar, *, account=ACCOUNT, chat='100', days=0.0, mime='image/jpeg', platform='telegram'):
    n = next(_serial)
    at = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    provider_id = f'cov-{n}' if platform == 'telegram' else f'true_{chat}_3EB0COV{n}'
    sender = {'sender': '901@lid', 'direction': 'incoming'} if platform == 'whatsapp' else {}
    mid = ar.ingest(event(account=account, platform=platform, chat=chat, provider_id=provider_id, sent_at=at, **sender,
        updated_at=at, media=[{'provider_id': f'media-cov-{n}', 'filename': f'cov-{n}.bin', 'mime': mime,
                               'bytes': 4}]))['message']
    with ar.store.pool.connection() as c:
        return c.execute('SELECT id FROM media_jobs WHERE message=%s', (mid,)).fetchone()['id'], mid


def stored(ar, s, mime, *, chat='100', text='ready', content='', enrichment='unconfigured', attempts=0,
           error=None, waited='1 minute', account=ACCOUNT):
    """A downloaded original; the triggers queue its text and enrichment rows, which are then set as named."""
    job, mid = message(ar, account=account, chat=chat, mime=mime)
    digest = hashlib.sha256(job.encode()).hexdigest()
    with s.pool.connection() as c:
        c.execute('''INSERT INTO attachments(id,message,digest,filename,mime,bytes)
            SELECT %s,%s,%s,descriptor->>'filename',%s,4 FROM media_jobs WHERE id=%s''', (job, mid, digest, mime, job))
        c.execute("UPDATE media_jobs SET state='ready',error_code=NULL WHERE id=%s", (job,))
        if text is not None:
            c.execute('UPDATE attachment_text SET state=%s,text_content=%s WHERE attachment=%s', (text, content, job))
        c.execute('''UPDATE attachment_enrichment SET state=%s,attempts=%s,error_code=%s,
            lease_until=CASE WHEN %s='leased' THEN clock_timestamp()+interval '10 minutes' END,
            updated_at=clock_timestamp()-%s::interval WHERE attachment=%s''',
            (enrichment, attempts, error, enrichment, waited, job))
    return job


def evict(s, job):
    with s.pool.connection() as c:
        digest = c.execute('SELECT digest FROM attachments WHERE id=%s', (job,)).fetchone()['digest']
        eviction = uuid4()
        c.execute('''INSERT INTO attachment_eviction_journal(id,digest,bytes,root_device,root_inode,state,completed_at)
            VALUES(%s,%s,4,1,1,'completed',clock_timestamp())''', (eviction, digest))
        c.execute('''UPDATE attachment_residency SET state='evicted',eviction_id=%s,evicted_at=clock_timestamp()
            WHERE digest=%s''', (eviction, digest))


def profiles(s, kinds=('audio', 'image', 'document'), *, account=ACCOUNT, enabled=True, observed='0 seconds'):
    with s.pool.connection() as c:
        for kind in kinds:
            c.execute('''INSERT INTO processing_profiles(account,kind,provider,model,enabled,observed_at,expires_at)
                VALUES(%s,%s,'openai','synthetic',%s,clock_timestamp()-%s::interval,clock_timestamp()+interval '1 hour')
                ON CONFLICT(account,kind) DO UPDATE SET enabled=excluded.enabled,observed_at=excluded.observed_at''',
                (account, kind, enabled, observed))


def group(s, account=ACCOUNT, chat='200'):
    with s.pool.connection() as c:
        c.execute("UPDATE chats SET chat_type='group' WHERE account=%s AND provider_chat=%s", (account, chat))


def status(s, account=ACCOUNT):
    with s.pool.connection() as c:
        result = get_sync_status(c, {'accounts': [account], 'chat_ids': None})
    return next(row for row in result['sources'] if row['account'] == account), result


@pytest.fixture()
def cov(archive):
    ar, s, key, _ = archive
    ar.register(ACCOUNT, 'telegram', 'Synthetic archive', include_chats=['100', '200'])
    with s.pool.connection() as c:
        c.execute('DELETE FROM processing_profiles')
    return ar, s, key


def mixed(ar, s):
    """Criterion 1's account: one row per bucket."""
    publish(ar)
    ar.ingest(event(chat='200', provider_id='cov-group-seed'))
    group(s)
    rows = {'image': stored(ar, s, 'image/jpeg'),
            'voice': stored(ar, s, 'audio/ogg', waited='3 hours'),
            'document': stored(ar, s, 'application/pdf', content='Договор поставки'),
            'withheld': stored(ar, s, 'image/jpeg', chat='200'),
            'evicted': stored(ar, s, 'image/png'),
            'no_text': stored(ar, s, 'application/pdf', text='unsupported'),
            'to_index': stored(ar, s, 'text/plain', text='queued', enrichment='ready')}
    evict(s, rows['evicted'])
    rows['group_job'] = message(ar, chat='200', days=1)[0]
    rows['old_job'] = message(ar, days=30)[0]
    rows['eligible_job'] = message(ar, days=1)[0]
    return rows


# spec: ANYM-887
def test_a_skipped_image_is_not_due_work_in_sync_status(cov):
    ar, s, _ = cov
    profiles(s)
    publish(ar)
    old = stored(ar, s, 'image/jpeg')
    with s.pool.connection() as c:
        c.execute("UPDATE messages m SET sent_at=clock_timestamp()-interval '30 days' FROM attachments a "
                  "WHERE a.id=%s AND m.id=a.message", (old,))
    block = status(s)[0]['coverage']
    assert block['due']['to_recognize']['total'] == 0 and block['state'] == 'done'
    assert block['available_on_request']['stored_not_recognized'] == 1
    with s.pool.connection() as c:  # an agent asked: due work again
        c.execute("UPDATE media_jobs SET request_key_digest='synthetic-key' WHERE id=%s", (old,))
    block = status(s)[0]['coverage']
    assert block['due']['to_recognize']['image'] == 1 and block['available_on_request']['stored_not_recognized'] == 0


# spec: ANYM-890
def test_sync_status_counts_waiting_audio_as_on_request(cov):
    ar, s, _ = cov
    profiles(s)
    publish(ar)
    music = stored(ar, s, 'audio/mpeg')
    with s.pool.connection() as c:
        mid, chat = c.execute('''UPDATE chats SET chat_type='group' FROM attachments a JOIN messages m ON m.id=a.message
            WHERE a.id=%s AND chats.id=m.chat RETURNING m.id, chats.id''', (music,)).fetchone().values()
        c.execute('''INSERT INTO message_metadata(message,revision,metadata)
            SELECT id,revision,'{"media_kind":"audio"}'::jsonb FROM messages WHERE id=%s''', (mid,))
    token = s.issue_key('audio-watcher', [ACCOUNT], permissions=['read', 'subscription-write'],
                        key_permissions=['read', 'subscription-write'])
    s.create_subscription(token, [chat])  # a subscribed group: ANYM-805 does not withhold it
    block = status(s)[0]['coverage']
    assert block['due']['to_recognize']['total'] == 0 and block['state'] == 'done'
    assert block['available_on_request']['stored_not_recognized'] == 1
    with s.pool.connection() as c:  # an agent asked: due work again
        c.execute("UPDATE media_jobs SET request_key_digest='synthetic-key' WHERE id=%s", (music,))
    block = status(s)[0]['coverage']
    assert block['due']['to_recognize']['audio'] == 1 and block['available_on_request']['stored_not_recognized'] == 0


# spec: ANYM-812
def test_each_file_lands_in_one_coverage_bucket_and_the_verdict_follows_the_due_work(cov):
    ar, s, _ = cov
    profiles(s)
    rows = mixed(ar, s)
    row, _ = status(s)
    block = row['coverage']
    assert block['state'] == 'behind'
    assert block['due']['to_download'] == 1 and block['due']['to_index'] == 1
    assert block['due']['to_recognize'] == {'audio': 1, 'image': 1, 'document': 1, 'total': 3}
    assert block['available_on_request']['stored_not_recognized'] == 2
    assert block['available_on_request']['not_downloaded'] == {'group_chat_on_request': 1,
                                                               'outside_prefetch_window': 1}
    assert block['available_on_request']['total'] == 4
    assert block['available_on_request']['next_action'] == 'request_file_download'
    assert block['available_on_request']['likely_expired'] is None  # Telegram publishes no media expiry
    assert block['not_recognizable'] == {'no_text': 1, 'unsupported': 0, 'recognition_off': 0}
    assert block['done']['described'] == 1 and block['done']['stored'] == row['files']['stored_files'] == 7
    assert block['health'] == [] and block['as_of'] is not None
    with s.pool.connection() as c:
        oldest = c.execute('SELECT updated_at FROM attachment_enrichment WHERE attachment=%s',
                           (rows['voice'],)).fetchone()['updated_at']
    assert block['since'] == oldest
    policy = block['policy']
    assert policy['files_auto_days'] == 7 and policy['messages_since'] is None
    assert policy['messages_source'] == 'provider_history' and policy['files_other'] == 'on_request'
    assert policy['recognition'] == ['audio', 'document', 'image']
    assert policy['provider_media_retention_days'] is None
    # Everything due is done: the verdict turns, the on-request files stay listed apart.
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_enrichment SET state='ready' WHERE attachment=ANY(%s)",
                  ([rows['image'], rows['voice'], rows['document']],))
        c.execute("UPDATE attachment_text SET state='ready' WHERE attachment=%s", (rows['to_index'],))
        c.execute("UPDATE media_jobs SET state='superseded' WHERE id=%s", (rows['eligible_job'],))
    block = status(s)[0]['coverage']
    assert block['state'] == 'done' and block['since'] is None
    assert block['available_on_request']['total'] == 4


# spec: ANYM-812
def test_an_unpublished_window_or_an_expired_lease_reads_unknown(cov):
    ar, s, _ = cov
    profiles(s)
    stored(ar, s, 'image/jpeg')
    assert status(s)[0]['coverage']['state'] == 'unknown'
    publish(ar)
    assert status(s)[0]['coverage']['state'] == 'behind'
    with s.pool.connection() as c:
        c.execute("UPDATE collector_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE account=%s",
                  (ACCOUNT,))
    assert status(s)[0]['coverage']['state'] == 'unknown'


# spec: ANYM-812
def test_a_kind_without_a_configured_profile_is_recognition_off_and_a_stopped_worker_still_owes_its_files(cov):
    ar, s, _ = cov
    publish(ar)
    profiles(s, ('image',))
    stored(ar, s, 'audio/ogg')
    stored(ar, s, 'image/jpeg')
    block = status(s)[0]['coverage']
    assert block['not_recognizable']['recognition_off'] == 1
    assert block['due']['to_recognize']['total'] == 1 and block['policy']['recognition'] == ['image']
    # A clean stop (enabled=false) is not recognition switched off: the image stays due.
    profiles(s, ('image',), enabled=False)
    block = status(s)[0]['coverage']
    assert block['state'] == 'behind' and block['due']['to_recognize']['image'] == 1
    # A profile row older than the no-worker bound is a decommissioned kind.
    days = coverage.CONFIGURED_DAYS
    assert days * 86400 == NO_WORKER_MAX_AGE_S
    profiles(s, ('image',), enabled=False, observed=f'{days + 1} days')
    block = status(s)[0]['coverage']
    assert block['not_recognizable']['recognition_off'] == 2 and block['state'] == 'done'


# spec: ANYM-812
def test_the_files_histogram_keeps_each_row_once_while_due_work_is_a_separate_backlog(cov):
    ar, s, _ = cov
    profiles(s)
    publish(ar)
    stored(ar, s, 'image/jpeg', enrichment='leased')
    stored(ar, s, 'image/jpeg', enrichment='failed', attempts=3, error='model_timeout')
    stored(ar, s, 'image/jpeg', enrichment='failed', attempts=1, error='model_timeout')
    stored(ar, s, 'image/jpeg', enrichment='failed', attempts=1, error='original_size_limit')
    stored(ar, s, 'image/jpeg', enrichment='unsupported')
    stored(ar, s, 'image/jpeg')
    with s.pool.connection() as c:
        # A reached daily cap delays the claim; the row is still due.
        c.execute('INSERT INTO enrichment_budget(day,attempts) VALUES(current_date,0) ON CONFLICT DO NOTHING')
        c.execute('UPDATE enrichment_budget SET attempts=100000 WHERE day=current_date')
    row, _ = status(s)
    files, block = row['files'], row['coverage']
    assert sum(files['enrichment'].values()) == files['stored_files'] == 6
    assert sum(files['extraction'].values()) == 6
    assert {k: v for k, v in files['enrichment'].items() if v} == {'queued': 1, 'leased': 1, 'failed': 3,
                                                                  'unsupported': 1}
    # Due: the queued row, the active lease and the retryable failure; two failures are final.
    assert block['due']['to_recognize']['image'] == 3 and block['due']['failed']['recognize'] == 2
    assert block['not_recognizable']['unsupported'] == 1
    with s.pool.connection() as c:
        c.execute('DELETE FROM enrichment_budget WHERE day=current_date')


# spec: ANYM-812
def test_a_policy_exclusion_never_hides_a_failure_lease_or_ready_row_in_the_histogram(cov):
    ar, s, _ = cov
    profiles(s)
    publish(ar)
    ar.ingest(event(chat='200', provider_id='cov-group-seed'))
    group(s)
    failed = stored(ar, s, 'image/jpeg', chat='200', enrichment='failed', attempts=1, error='model_timeout')
    leased = stored(ar, s, 'image/jpeg', enrichment='leased')
    evict(s, leased)
    stored(ar, s, 'image/jpeg', chat='200')
    row = status(s)[0]
    files, block = row['files'], row['coverage']
    assert {k: v for k, v in files['enrichment'].items() if v} == {'failed': 1, 'leased': 1, 'available_on_request': 1}
    # Coverage still files the retryable failure of the group chat under on request, and the lease under due.
    assert block['available_on_request']['stored_not_recognized'] == 2
    assert block['due']['to_recognize']['image'] == 1
    assert failed


# spec: ANYM-812
def test_a_leased_download_keeps_the_account_behind_with_its_waiting_time(cov):
    ar, s, _ = cov
    profiles(s)
    publish(ar)
    job = message(ar, days=1)[0]
    with s.pool.connection() as c:
        c.execute('''UPDATE media_jobs SET state='leased',lease_owner='coverage-owner',
            lease_until=clock_timestamp()+interval '5 minutes' WHERE id=%s''', (job,))
        created = c.execute('SELECT created_at FROM media_jobs WHERE id=%s', (job,)).fetchone()['created_at']
    block = status(s)[0]['coverage']
    assert block['state'] == 'behind' and block['due']['to_download'] == 1 and block['since'] == created


def model_profile(tmp_path, kind):
    key = tmp_path / 'model-key'
    key.write_text('synthetic-key')
    key.chmod(0o600)
    return validate_profile({'provider': 'openai', 'model': 'fixture-model', 'allow_cloud': True,
                             'api_key_file': str(key)}, kind)


def due_ids(s, stage):
    """The rows the coverage expression calls due, read the way sync status reads them."""
    with s.pool.connection() as c:
        withheld = recognition_withheld(c)
        bucket = coverage.recognition_bucket() if stage == 'recognition' else coverage.extraction_bucket()
        due = coverage.RECOGNITION_DUE if stage == 'recognition' else coverage.EXTRACTION_DUE
        return {r['id'] for r in c.execute('WITH ' + coverage.policy_sets(withheld) + ''' SELECT a.id,''' + bucket + ''' AS bucket
            FROM attachments a JOIN messages m ON m.id=a.message
            LEFT JOIN attachment_text f ON f.attachment=a.id AND f.source_digest=a.digest
                AND f.source_revision=m.revision AND f.source_filename=a.filename
            LEFT JOIN attachment_enrichment e ON e.attachment=a.id AND e.source_digest=a.digest
                AND e.source_revision=m.revision AND e.state<>'superseded' ''' + coverage.POLICY_JOINS + '''
            WHERE a.active AND NOT m.deleted AND m.account=%s''', (ACCOUNT,)) if r['bucket'] in due}


# spec: ANYM-812
def test_with_runtime_filters_lifted_the_claims_take_exactly_the_due_rows(cov, tmp_path):
    ar, s, _ = cov
    publish(ar)
    ar.ingest(event(chat='200', provider_id='cov-group-seed'))
    group(s)
    stored(ar, s, 'image/jpeg')
    stored(ar, s, 'audio/ogg', text='unsupported')
    stored(ar, s, 'application/pdf', content='Счёт на оплату')
    stored(ar, s, 'application/pdf', text='unsupported')
    stored(ar, s, 'image/jpeg', chat='200')
    requested = stored(ar, s, 'image/jpeg', chat='200')
    with s.pool.connection() as c:
        # An agent asked for this group file (ANYM-805): it is due, not withheld.
        c.execute("UPDATE media_jobs SET request_key_digest='synthetic-request' WHERE id=%s", (requested,))
    evict(s, stored(ar, s, 'image/png'))
    tried = [stored(ar, s, 'image/jpeg', enrichment='failed', attempts=1, error='model_timeout'),
             stored(ar, s, 'image/jpeg', enrichment='failed', attempts=3, error='model_timeout'),
             stored(ar, s, 'image/jpeg', enrichment='failed', attempts=1, error='original_size_limit'),
             stored(ar, s, 'image/jpeg', enrichment='unsupported')]
    configured = {kind: model_profile(tmp_path, kind) for kind in ('audio', 'image', 'document')}
    with s.pool.connection() as c:
        # Tried under the worker's current profile: a changed profile retries them, which the card leaves to the
        # worker (no bucket depends on the profile's identity).
        c.execute("UPDATE attachment_enrichment SET kind='image',profile_hash=%s WHERE attachment=ANY(%s)",
                  (profile_hash(configured['image']), tried))
    stored(ar, s, 'text/plain', text='queued', enrichment='ready')
    stored(ar, s, 'text/plain', text='queued', enrichment='ready', chat='200')
    evict(s, stored(ar, s, 'text/plain', text='queued', enrichment='ready'))
    stored(ar, s, 'text/plain', text='failed', enrichment='ready')
    worker = EnrichmentWorker(s.pool.conninfo, s.files, {'accounts': [ACCOUNT], 'max_jobs_per_day': 1000,
        'profiles': configured},
        lock_directory=str(tmp_path))
    worker.publish_profiles()
    with s.pool.connection() as c:
        # Runtime filters lifted: no daily cap reached (budgets are per day, not per test database).
        c.execute('DELETE FROM enrichment_budget WHERE day=current_date')
        c.execute('DELETE FROM processing_budget WHERE day=current_date')
    expected = due_ids(s, 'recognition')
    assert len(expected) == 5 and requested in expected
    claimed = set()
    for _ in range(20):
        item = worker.claim()
        if item is None:
            break
        claimed.add(item[0]['attachment'])
    assert claimed == expected
    indexer = FileIndexer(s.pool.conninfo, s.files, lock_directory=str(tmp_path))
    expected = due_ids(s, 'extraction')
    assert len(expected) == 1
    claimed = set()
    for _ in range(20):
        item = indexer.claim()
        if item is None:
            break
        if item['account'] == ACCOUNT:  # the indexer serves every account; the seeded demo rows are not ours
            claimed.add(item['attachment'])
        if item.get('_resource_lease'):
            item['_resource_lease'].close()
    assert claimed == expected


# spec: ANYM-812
def test_an_old_whatsapp_file_not_downloaded_is_likely_expired_not_requestable(cov):
    ar, s, _ = cov
    ar.register(WA, 'whatsapp', 'Synthetic WhatsApp', include_chats=['900@lid'])
    publish(ar, WA, days=7, chats=(('900@lid', 'user'),))
    message(ar, account=WA, chat='900@lid', platform='whatsapp', days=40)
    message(ar, account=WA, chat='900@lid', platform='whatsapp', days=10)
    block = status(s, WA)[0]['coverage']
    assert block['policy']['provider_media_retention_days'] == 30
    assert block['policy']['messages_source'] == 'phone_pairing_batch_and_live'
    assert block['available_on_request']['not_downloaded'] == {'outside_prefetch_window': 2}
    assert block['available_on_request']['likely_expired'] == 1
    assert block['available_on_request']['total'] == 1


# spec: ANYM-812
def test_compact_status_shows_the_verdict_and_one_on_request_total(cov):
    ar, s, _ = cov
    profiles(s)
    mixed(ar, s)
    _, result = status(s)
    compact = render('get_sync_status', result, 'compact')
    block = next(row for row in compact['sources'] if row['account'] == ACCOUNT)['coverage']
    full = status(s)[0]['coverage']
    assert block == {'state': 'behind', 'since': full['since'], 'available_on_request': 4,
                     'due': {'to_download': 1, 'to_index': 1, 'to_recognize': 3}}
    # A done account names its on-request total; an unknown one stays out of the compact budget.
    assert coverage.compact({**full, 'state': 'done'}) == {'state': 'done', 'available_on_request': 4}
    assert coverage.compact({**full, 'state': 'unknown'}) is None
    json.dumps(compact, default=str)


# spec: ANYM-812
def test_a_chat_scoped_key_gets_no_coverage_block_and_no_file_policy(cov):
    ar, s, key = cov
    profiles(s)
    publish(ar)
    stored(ar, s, 'image/jpeg')
    assert s.capabilities(key, sections=['coverage'])['file_policy'][ACCOUNT]['status_call'] == 'get_sync_status'
    chat = stable_id('chat', ACCOUNT, '100')
    scoped = s.issue_key('coverage-chat-812', [ACCOUNT])
    s.set_access('coverage-chat-812', accounts=[ACCOUNT], chats=[chat])
    row = next(r for r in s.get_sync_status(scoped, refresh=True)['sources'] if r['account'] == ACCOUNT)
    assert 'coverage' not in row
    assert 'file_policy' not in s.capabilities(scoped, sections=['coverage'])


# spec: ANYM-812
def test_capabilities_name_each_accounts_file_policy_from_the_published_window_only(cov):
    ar, s, key = cov
    profiles(s, ('audio',))
    since = datetime(2026, 7, 12, tzinfo=timezone.utc)
    publish(ar, days=7, since=since)
    result = s.capabilities(key, sections=['coverage'])
    policy = result['file_policy'][ACCOUNT]
    assert policy['published'] is True and policy['files_auto_days'] == 7
    assert datetime.fromisoformat(policy['messages_since']) == since and policy['recognition'] == ['audio']
    assert policy['files_request_call'] == 'request_file_download'
    assert policy['older_messages_call'] == 'request_chat_history'
    assert 'coverage' in s.capabilities(key, detail='summary')['sections']
    assert 'file_policy' in CAPABILITY_SECTIONS['coverage']


# spec: ANYM-812
def test_the_tool_description_says_on_request_files_are_not_waiting_work_in_every_copy():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root / 'services/gateway/src/agent_gateway/catalog.json').read_text())
    contracts = json.loads((root / 'tests/platform/fixtures/messaging-contracts.json').read_text())
    for tool in (next(t for t in catalog if t.get('operation') == 'get_sync_status'),
                 next(t for t in contracts['tools'] if t['name'] == 'get_sync_status')):
        second = tool['description'].splitlines()[1]
        assert second.startswith('Files: available_on_request is not work waiting')


BULK = 70000


def grow(s):
    """BULK stored originals, half in the group chat, each with its media job; triggers queue their rows."""
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='600s'")
        # register_resident_original() looks attachments up by digest and bytes, which no index covers: each of
        # the BULK inserts would scan the table (quadratic, over 600 s on a loaded runner). A transaction-local
        # index turns the setup linear; it is dropped before commit, so the measured plans never see it. The
        # triggers' cached plans are made from whatever statistics earlier tests left (a one-row attachments
        # table picks a scan), so the setup also pins index access; the measured transaction keeps the defaults.
        c.execute('CREATE INDEX grow_attachment_digest ON attachments(digest,bytes)')
        c.execute("SET LOCAL enable_seqscan=off")
        for provider_chat in ('100', '200'):
            c.execute('''INSERT INTO chats(id,account,title,provider_chat) VALUES(%s,%s,'Fixture room',%s)
                ON CONFLICT DO NOTHING''', (stable_id('chat', ACCOUNT, provider_chat), ACCOUNT, provider_chat))
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            SELECT 'bulk-msg-'||n,%s,CASE WHEN n%%2=0 THEN %s ELSE %s END,'bulk-'||n,'42','',
                clock_timestamp()-(n%%400)*interval '1 day'
            FROM generate_series(1,%s) n''',
            (ACCOUNT, stable_id('chat', ACCOUNT, '100'), stable_id('chat', ACCOUNT, '200'), BULK))
        c.execute('''INSERT INTO media_jobs(id,account,message,descriptor,state)
            SELECT 'bulk-att-'||n,%s,'bulk-msg-'||n,jsonb_build_object('provider_id','bulk-'||n),'ready'
            FROM generate_series(1,%s) n''', (ACCOUNT, BULK))
        c.execute('''INSERT INTO attachments(id,message,digest,filename,mime,bytes)
            SELECT 'bulk-att-'||n,'bulk-msg-'||n,md5(n::text)||md5('x'||n),'bulk-'||n||'.bin',
                (ARRAY['image/jpeg','audio/ogg','application/pdf'])[1+n%%3],4
            FROM generate_series(1,%s) n''', (BULK,))
        c.execute('DROP INDEX grow_attachment_digest')
        for table in ('messages', 'media_jobs', 'attachments', 'attachment_text', 'attachment_enrichment',
                      'attachment_residency', 'processing_profiles', 'chats', 'collector_chats'):
            c.execute('ANALYZE ' + table)


def scans(plan, relation):
    """(node type, actual rows x loops) of every node on `relation` in the plan."""
    found = []

    def visit(node):
        if node.get('Relation Name') == relation:
            found.append((node['Node Type'], node.get('Actual Rows', 0) * node.get('Actual Loops', 1)))
        for child in node.get('Plans', []):
            visit(child)
    visit(plan)
    return found


# spec: ANYM-812
def test_the_coverage_counts_add_no_scan_and_no_per_row_sequential_subquery(cov):
    from test_media_claim_performance import explained, outline
    ar, s, _ = cov
    profiles(s)
    publish(ar)
    group(s)
    grow(s)
    plans = []
    with explained(ar, plans):
        with s.pool.connection() as c:
            # This test measures plan shape, not speed: a loaded CI runner may need more than the serving timeout.
            c.execute("SET LOCAL statement_timeout='120s'")
            result = get_sync_status(c, {'accounts': [ACCOUNT], 'chat_ids': None})
        row = next(r for r in result['sources'] if r['account'] == ACCOUNT)
    assert row['coverage']['done']['stored'] == BULK
    # The group's files wait for a request (ANYM-805); so do the other chat's images older than 7 days (ANYM-887).
    old_images = sum(1 for n in range(1, BULK + 1) if n % 2 == 0 and n % 3 == 0 and n % 400 >= 7)
    assert row['coverage']['available_on_request']['stored_not_recognized'] == BULK // 2 + old_images
    full = 0
    for query, plan in plans:
        def visit(node, inside=False):
            # A correlated predicate may probe an index per row; it must never scan a table per row.
            # A set computed once may scan per chat (ON_REQUEST_CHATS's subscription check); never per file row.
            assert not (inside and 'Seq Scan' in node['Node Type'] and node.get('Actual Loops', 1) > BULK // 100), \
                outline(plan)
            for child in node.get('Plans', []):
                visit(child, inside or child.get('Parent Relationship') == 'SubPlan')
        visit(plan)
        if any(rows >= BULK for _, rows in scans(plan, 'attachments')):
            full += 1
    # _files reads attachments in its download pass and its grouped file pass; coverage adds no third.
    assert full <= 2, [query[:120] for query, _ in plans]
