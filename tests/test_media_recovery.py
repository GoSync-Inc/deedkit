"""Operator recovery of failed WhatsApp originals (ANYM-816): own state, lowest claim priority, one outcome each."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace as NS

from hypothesis import given, strategies as st
import psycopg
from psycopg.rows import dict_row
import pytest

from messaging_service import media_recovery as mr
from messaging_service.archive import Archive
from messaging_service.blobs import BlobWriter
from messaging_service.migration_rollout import new_tables, role_class, unattended_class
from messaging_service.media_retry import MEDIA_MAX_ATTEMPTS
from test_archive import archive as archive, event
from test_db_role_isolation import FORBIDDEN, RolePool, as_role, install, remove

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'services/messaging/messaging_service'
MIGRATION = SOURCE/'migrations/069_media_recovery.sql'
ACCOUNT = 'recovery-wa'
CHAT = '15551234567@c.us'


@pytest.fixture()
def wa(archive):
    ar, s, _, _ = archive
    ar.register(ACCOUNT, 'whatsapp', 'Synthetic recovery source', discover=True)
    return ar, s


def failed(ar, s, mid, *, days=40, code='media_network_error', chat=CHAT, size=4, attempts=MEDIA_MAX_ATTEMPTS):
    at = (datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
    message = ar.ingest(event(account=ACCOUNT, platform='whatsapp', chat=chat, provider_id=mid, sent_at=at,
        updated_at=at, media=[{'provider_id': 'media-'+mid, 'filename': 'synthetic.bin', 'mime': 'image/jpeg',
                               'bytes': size}]))['message']
    with s.pool.connection() as c:
        fid = c.execute('SELECT id FROM media_jobs WHERE message=%s', (message,)).fetchone()['id']
        c.execute("UPDATE media_jobs SET state='failed',error_code=%s,attempts=%s WHERE id=%s", (code, attempts, fid))
    return fid


def live(ar, s, mid):
    at = datetime.now(timezone.utc).isoformat()
    message = ar.ingest(event(account=ACCOUNT, platform='whatsapp', chat=CHAT, provider_id=mid, sent_at=at,
        updated_at=at, media=[{'provider_id': 'voice-'+mid, 'filename': 'voice.ogg', 'mime': 'audio/ogg',
                               'bytes': 4}]))['message']
    with s.pool.connection() as c:
        return c.execute('SELECT id FROM media_jobs WHERE message=%s', (message,)).fetchone()['id']


def recover(s, **options):
    with s.pool.connection() as c:
        return mr.recover(c, ACCOUNT, **options)


def recovery(s, fid):
    with s.pool.connection() as c:
        return c.execute('SELECT * FROM media_recovery WHERE job=%s', (fid,)).fetchone()


def job(s, fid):
    with s.pool.connection() as c:
        return c.execute('SELECT state,error_code,attempts,request_key_digest,requested_until FROM media_jobs WHERE id=%s',
            (fid,)).fetchone()


def due(s, fid):
    with s.pool.connection() as c:
        c.execute('UPDATE media_recovery SET next_due=clock_timestamp() WHERE job=%s', (fid,))


def delete(s, fid):
    # The archive's delete state (a provider delete event sets it with its own revision).
    with s.pool.connection() as c:
        c.execute('UPDATE messages SET deleted=true FROM media_jobs j WHERE j.id=%s AND messages.id=j.message', (fid,))


def expire(s, fid):
    # A recovery lease expires in media_recovery; media_jobs keeps its far-future hold (LEASE_HOLD).
    with s.pool.connection() as c:
        c.execute("UPDATE media_recovery SET lease_until=clock_timestamp()-interval '1 second' WHERE job=%s", (fid,))


def claim(ar, owner='worker'):
    return ar.claim_media(owner, account=ACCOUNT)


def kind(s, fid, chat_type):
    with s.pool.connection() as c:
        c.execute('''UPDATE chats SET chat_type=%s FROM media_jobs j JOIN messages m ON m.id=j.message
            WHERE j.id=%s AND chats.id=m.chat''', (chat_type, fid))


# spec: ANYM-816
def test_migration_is_expand_and_role_files_only_append_grants():
    text = MIGRATION.read_text()
    assert unattended_class(text, {'media_jobs', 'accounts'}) == 'expand'
    created = new_tables(text)
    assert set(created) == {'media_recovery'}
    assert created['media_recovery'] == {'job', 'account', 'run_id', 'attempts', 'next_due', 'lease_until', 'state',
                                         'refusal_reason', 'last_code', 'created_at', 'updated_at'}
    for name, line, marker in [
            ('collector_role.sql', 'GRANT SELECT,UPDATE(state,attempts,next_due,lease_until,refusal_reason,last_code,updated_at) '
             'ON media_recovery TO messaging_collector;', '-- Schema 69'),
            ('gateway_role.sql', 'GRANT SELECT ON media_recovery TO messaging_gateway;', '-- Schema 69')]:
        text = (SOURCE/name).read_text()
        # The file through this schema's section; a later schema's section (gateway_role.sql: ANYM-824, 70) may follow.
        start = text.rindex(marker)
        current = text[:start] + text[start:].split('\n-- Schema ')[0].rstrip('\n') + '\n'
        assert current.endswith(line+'\n')
        base = current[:current.rindex(marker)]
        assert role_class(base, current, created) == 'additive'


# spec: ANYM-816
def test_schema_68_has_the_table_and_the_collector_reads_and_updates_only_what_it_needs(wa):
    ar, s = wa
    fid = failed(ar, s, '1')
    with s.pool.connection() as c:
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '88'
    recover(s)
    role = install(s, 'collector_role.sql', 'messaging_collector', account=ACCOUNT)
    try:
        with as_role(s, role) as c:
            assert c.execute('SELECT state FROM media_recovery WHERE job=%s', (fid,)).fetchone()['state'] == 'queued'
            c.execute("UPDATE media_recovery SET state='running' WHERE job=%s", (fid,))
        for statement in ["INSERT INTO media_recovery(job,account,run_id,state) SELECT id,account,repeat('a',32),'queued' "
                          "FROM media_jobs LIMIT 1", "UPDATE media_recovery SET run_id=repeat('b',32)",
                          "UPDATE media_recovery SET account='archive-test'", 'DELETE FROM media_recovery']:
            with pytest.raises(FORBIDDEN):
                with as_role(s, role) as c: c.execute(statement)
    finally:
        remove(s, role)


# spec: ANYM-816
def test_dry_run_counts_jobs_and_bytes_and_changes_nothing(wa):
    ar, s = wa
    a, b = failed(ar, s, '1', size=10), failed(ar, s, '2', size=32, code='media_http_404')
    group = failed(ar, s, '3', chat='120363000000000001@g.us'); kind(s, group, 'group')
    before = [job(s, fid) for fid in (a, b, group)]
    result = recover(s, dry_run=True)
    assert result['matching'] == 3 and result['bytes'] == 46 and result['queued'] == 2
    assert result['refused'] == {'on_request_chat': 1} and result['run_id'] is None
    assert [job(s, fid) for fid in (a, b, group)] == before
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM media_recovery').fetchone()['n'] == 0
    assert recover(s, dry_run=True, codes=['media_http_404'])['matching'] == 1
    assert recover(s, dry_run=True, limit=1)['matching'] == 1
    assert recover(s, dry_run=True, sent_before=datetime.now(timezone.utc)-timedelta(days=60))['matching'] == 0


# spec: ANYM-816
@pytest.mark.parametrize('limit', [0, 51, '5'])
def test_limit_is_at_most_fifty(wa, limit):
    with pytest.raises(ValueError, match='invalid_limit'): recover(wa[1], limit=limit)


# spec: ANYM-816
def test_each_failed_check_is_refused_with_its_reason(wa):
    ar, s = wa
    deleted = failed(ar, s, '1')
    excluded = failed(ar, s, '2', chat='15550000002@c.us')
    leased = failed(ar, s, '3')
    good = failed(ar, s, '4')
    delete(s, deleted)
    with s.pool.connection() as c:
        c.execute('UPDATE account_policies SET exclude_chats=%s WHERE account=%s', (['15550000002@c.us'], ACCOUNT))
        c.execute("UPDATE media_jobs SET lease_until=clock_timestamp()+interval '1 hour' WHERE id=%s", (leased,))
    result = recover(s)
    assert result['refused'] == {'message_deleted': 1, 'chat_not_collected': 1, 'lease_running': 1}
    assert {fid: (recovery(s, fid)['state'], recovery(s, fid)['refusal_reason']) for fid in (deleted, excluded, leased, good)} == {
        deleted: ('refused', 'message_deleted'), excluded: ('refused', 'chat_not_collected'),
        leased: ('refused', 'lease_running'), good: ('queued', None)}
    other = failed(ar, s, '5')
    with s.pool.connection() as c:
        c.execute('UPDATE account_policies SET enabled=false WHERE account=%s', (ACCOUNT,))
    assert recover(s)['refused'].get('collection_disabled') == 2  # the new job and the refused excluded one
    assert recovery(s, other)['refusal_reason'] == 'collection_disabled'


# spec: ANYM-816
def test_a_storage_pause_refuses_and_a_rerun_after_it_queues(wa):
    ar, s = wa
    fid = failed(ar, s, '1')
    paused = live(ar, s, '9')
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET error_code='paused_storage' WHERE id=%s", (paused,))
    assert recover(s)['refused'] == {'storage_paused': 1}
    assert recovery(s, fid)['refusal_reason'] == 'storage_paused'
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET error_code=NULL WHERE id=%s", (paused,))
    assert recover(s)['queued'] == 1 and recovery(s, fid)['state'] == 'queued'


# spec: ANYM-816
def test_a_failed_group_job_is_refused_and_a_request_on_it_still_answers_failed(wa):
    ar, s = wa
    fid = failed(ar, s, '1', chat='120363000000000001@g.us'); kind(s, fid, 'group')
    assert recover(s)['refused'] == {'on_request_chat': 1}
    assert recovery(s, fid)['refusal_reason'] == 'on_request_chat'
    key = s.issue_key('recovery-reader', [ACCOUNT])
    answer = s.request_file_download(key, fid)
    assert answer['request_status'] == 'failed'
    # No request recorded and no quota spent: the request columns stay empty.
    assert job(s, fid)['request_key_digest'] is None and job(s, fid)['requested_until'] is None
    assert claim(ar) is None


# spec: ANYM-816
def test_a_fresh_live_voice_note_is_claimed_before_fifty_recovery_jobs(wa):
    ar, s = wa
    jobs = [failed(ar, s, str(n)) for n in range(50)]
    assert recover(s)['queued'] == 50
    voice = live(ar, s, 'v')
    first = ar.claim_media('worker', account=ACCOUNT, media_prefetch_days=7)
    assert first['id'] == voice and not mr.recovery_lease(first)
    second = claim(ar)
    assert second['id'] in jobs and mr.recovery_lease(second)
    assert recovery(s, second['id'])['state'] == 'running' and recovery(s, second['id'])['attempts'] == 1
    # The job's own budget and recorded cause are not used for the recovery.
    assert job(s, second['id'])['attempts'] == MEDIA_MAX_ATTEMPTS
    assert job(s, second['id'])['error_code'] == 'media_network_error'


# spec: ANYM-816
def test_an_old_private_file_below_the_history_bound_is_stored_and_leaves_failed(wa):
    ar, s = wa
    fid = failed(ar, s, '1', days=400)
    recover(s)
    since = datetime.now(timezone.utc)-timedelta(days=30)
    leased = ar.claim_media('worker', account=ACCOUNT, since=since, media_prefetch_days=7)
    assert leased['id'] == fid
    writer = BlobWriter(s.files, reserve_bytes=0)
    ar.finish_media(leased, writer.write([b'data'], expected_bytes=4), writer)
    assert job(s, fid)['state'] == 'ready' and job(s, fid)['error_code'] is None
    assert recovery(s, fid)['state'] == 'recovered'
    key = s.issue_key('recovery-reader', [ACCOUNT])
    assert s.get_attachment(key, fid)['status'] == 'ready'


# spec: ANYM-816
def test_budget_exhaustion_ends_still_failing_on_the_schedule(wa):
    ar, s = wa
    fid = failed(ar, s, '1')
    recover(s)
    for attempt in range(1, mr.RECOVERY_BUDGET+1):
        leased = claim(ar); assert leased['id'] == fid
        ar.fail_media(leased, 'media_network_error')
        row = recovery(s, fid)
        assert row['attempts'] == attempt and job(s, fid)['state'] == 'failed'
        if attempt < mr.RECOVERY_BUDGET:
            assert row['state'] == 'queued' and claim(ar) is None  # waits out the ANYM-699 backoff
            due(s, fid)
    assert recovery(s, fid)['state'] == 'still_failing' and recovery(s, fid)['last_code'] == 'media_network_error'
    assert job(s, fid)['attempts'] == MEDIA_MAX_ATTEMPTS and job(s, fid)['error_code'] == 'media_network_error'
    assert claim(ar) is None


def failure_events(s, fid):
    with s.pool.connection() as c:
        return c.execute('''SELECT e.event_key,e.snapshot->'processing' AS processing FROM events e
            JOIN media_jobs j ON j.message=e.message WHERE j.id=%s AND e.kind='attachment.failed' ORDER BY e.id''',
            (fid,)).fetchall()


def failed_for_good(ar, s, mid):
    # A job that failed for good through the archive's own sweep: its first attachment.failed episode.
    fid = failed(ar, s, mid)
    with s.pool.connection() as c:
        c.execute("""UPDATE media_jobs SET state='leased',lease_owner='worker',lease_until=now()-interval '1 second'
            WHERE id=%s""", (fid,))
    ar._media_sweeps.clear()
    claim(ar)
    assert job(s, fid)['state'] == 'failed' and len(failure_events(s, fid)) == 1
    return fid


# spec: ANYM-867
def test_an_operator_requeue_that_fails_again_commits_a_new_failure_and_one_that_succeeds_commits_ready(wa):
    ar, s = wa
    fid = failed_for_good(ar, s, '1')
    recover(s)
    ar.fail_media(claim(ar), 'media_unavailable', permanent=True)
    assert recovery(s, fid)['state'] == 'provider_unavailable'
    first, second = failure_events(s, fid)
    assert first['event_key'] != second['event_key']
    assert second['processing']['failed'] == [{'attachment': fid, 'reason': 'retry_exhausted', 'next_action': None}]
    # A retry inside the recovery budget is not terminal: nothing more is published.
    other = failed_for_good(ar, s, '2')
    recover(s)
    ar.fail_media(claim(ar), 'media_network_error')
    assert recovery(s, other)['state'] == 'queued' and len(failure_events(s, other)) == 1
    due(s, other)
    writer = BlobWriter(s.files, reserve_bytes=0)
    ar.finish_media(claim(ar), writer.write([b'data'], expected_bytes=4), writer)
    assert recovery(s, other)['state'] == 'recovered' and len(failure_events(s, other)) == 1
    with s.pool.connection() as c:
        assert c.execute('''SELECT count(*) AS n FROM events e JOIN media_jobs j ON j.message=e.message
            WHERE j.id=%s AND e.kind='attachment.ready' ''', (other,)).fetchone()['n'] == 1


# spec: ANYM-867
def test_a_recovery_that_ends_on_a_size_refusal_or_a_spent_lease_commits_its_failure_once(wa):
    ar, s = wa
    big = failed_for_good(ar, s, '1')
    recover(s)
    assert ar.claim_media('worker', account=ACCOUNT, writer=BlobWriter(s.files, max_bytes=3, reserve_bytes=0)) is None
    assert recovery(s, big)['state'] == 'still_failing' and recovery(s, big)['last_code'] == 'size_limit'
    assert [e['processing']['failed'][0]['reason'] for e in failure_events(s, big)] == ['retry_exhausted', 'size_limit']
    # The last recovery attempt's lease ran out: the claim ends it still_failing and commits its failure once.
    spent = failed_for_good(ar, s, '2')
    recover(s)
    assert claim(ar)['id'] == spent
    with s.pool.connection() as c:
        c.execute("UPDATE media_recovery SET attempts=%s,lease_until=now()-interval '1 second' WHERE job=%s",
            (mr.RECOVERY_BUDGET, spent))
    assert claim(ar) is None and claim(ar) is None
    assert recovery(s, spent)['state'] == 'still_failing' and recovery(s, spent)['last_code'] == 'lease_expired'
    assert len(failure_events(s, spent)) == 2


# spec: ANYM-816
@pytest.mark.parametrize('status', ['403', '404', '410'])
def test_a_cdn_refusal_ends_provider_unavailable(wa, status):
    # The native adapter maps 403/404/410 to media_unavailable (whatsapp/whatsmeow/media.go).
    ar, s = wa
    fid = failed(ar, s, '1')
    recover(s)
    ar.fail_media(claim(ar), 'media_unavailable', permanent=True)
    assert recovery(s, fid)['state'] == 'provider_unavailable' and recovery(s, fid)['attempts'] == 1
    assert job(s, fid)['state'] == 'failed'


# spec: ANYM-816
def test_a_rerun_does_not_touch_final_or_active_recoveries(wa):
    ar, s = wa
    done, active = failed(ar, s, '1'), failed(ar, s, '2')
    recover(s)
    with s.pool.connection() as c:
        c.execute("UPDATE media_recovery SET state='still_failing',attempts=3 WHERE job=%s", (done,))
    first = recovery(s, active)
    assert recover(s)['matching'] == 0
    assert recovery(s, done)['state'] == 'still_failing' and recovery(s, active) == first


# spec: ANYM-816
def test_two_commands_at_once_serialize_on_the_advisory_lock(wa):
    ar, s = wa
    jobs = [failed(ar, s, str(n)) for n in range(4)]
    results, entered = [], threading.Event()  # entered is never set: a bounded wait
    with s.pool.connection() as c:
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('media-recovery:'+ACCOUNT,))
        workers = [threading.Thread(target=lambda: results.append(recover(s, limit=4))) for _ in range(2)]
        for worker in workers: worker.start()
        entered.wait(0.5)
        assert not results  # both wait for the lock this transaction holds
    for worker in workers: worker.join(10)
    assert sorted(r['queued'] for r in results) == [0, 4]
    with s.pool.connection() as c:
        assert c.execute('SELECT count(DISTINCT run_id) AS n FROM media_recovery').fetchone()['n'] == 1
    assert all(recovery(s, fid)['state'] == 'queued' for fid in jobs)


# spec: ANYM-816
def test_a_lease_expiry_or_restart_resumes_from_the_table(wa):
    ar, s = wa
    fid = failed(ar, s, '1', attempts=2)
    recover(s)
    lost = claim(ar, 'before-restart'); assert lost['id'] == fid
    expire(s, fid)
    # The ordinary claim and sweep leave an expired recovery lease alone, even below the eight-attempt cap.
    assert ar.claim_media('other', account=ACCOUNT, media_prefetch_days=None) is not None
    with s.pool.connection() as c:
        row = c.execute('SELECT lease_owner,state,error_code,attempts FROM media_jobs WHERE id=%s', (fid,)).fetchone()
    assert row['lease_owner'] == mr.LEASE_PREFIX+'other' and row['attempts'] == 2
    assert recovery(s, fid)['attempts'] == 2
    with pytest.raises(RuntimeError, match='media_lease_lost'): ar.fail_media(lost, 'network_error')
    expire(s, fid)
    third = claim(ar, 'after-restart'); assert third['id'] == fid and recovery(s, fid)['attempts'] == 3
    expire(s, fid)
    assert claim(ar) is None
    assert recovery(s, fid)['state'] == 'still_failing' and recovery(s, fid)['last_code'] == 'lease_expired'
    assert job(s, fid)['state'] == 'failed' and job(s, fid)['error_code'] == 'media_network_error'


# spec: ANYM-816
def test_a_queued_recovery_whose_message_is_deleted_is_refused_at_claim(wa):
    ar, s = wa
    fid = failed(ar, s, '1')
    recover(s)
    delete(s, fid)
    assert claim(ar) is None
    assert recovery(s, fid)['refusal_reason'] == 'message_deleted'


# spec: ANYM-816
def test_status_counts_recovery_states_and_a_recovered_job_leaves_failed(wa):
    from messaging_service.sync_status import get_sync_status
    ar, s = wa
    stored, refused = failed(ar, s, '1'), failed(ar, s, '2', chat='120363000000000001@g.us')
    kind(s, refused, 'group')
    recover(s)
    leased = claim(ar)
    writer = BlobWriter(s.files, reserve_bytes=0)
    ar.finish_media(leased, writer.write([b'data'], expected_bytes=4), writer)
    with s.pool.connection() as c:
        status = get_sync_status(c, {'accounts': [ACCOUNT], 'chat_ids': None})
    source = next(x for x in status['sources'] if x['account'] == ACCOUNT)
    media = source['operations']['media']
    assert media['recovery'] == {'queued': 0, 'running': 0, 'recovered': 1, 'provider_unavailable': 0,
                                 'still_failing': 0, 'refused': 1}
    assert media['counts'].get('failed') == 1 and media['counts'].get('ready') == 1
    assert stored != refused


# spec: ANYM-816
def test_a_collector_role_without_the_new_grant_still_claims_ordinary_jobs(wa):
    ar, s = wa
    failed(ar, s, '1'); recover(s)
    voice = live(ar, s, 'v')
    text = (SOURCE/'collector_role.sql').read_text()
    template = SOURCE/'tmp_collector_role_069.sql'
    template.write_text(text[:text.rindex('-- Schema 69')])
    try:
        role = install(s, template.name, 'messaging_collector', account=ACCOUNT)
    finally:
        template.unlink()
    try:
        bound = Archive(NS(pool=RolePool(s, role), append_event=s.append_event, files=s.files))
        assert bound.claim_media('old-grant', account=ACCOUNT)['id'] == voice
        assert bound.claim_media('old-grant', account=ACCOUNT) is None  # the recovery step is skipped, not raised
    finally:
        remove(s, role)


# spec: ANYM-816
def test_the_command_prints_its_summary_and_fixed_errors(wa):
    ar, s = wa
    failed(ar, s, '1')
    dsn = os.environ['PROBE_TEST_DSN_FILE']
    base = [sys.executable, '-m', 'messaging_service.collector_control', '--dsn-file', dsn, '--account']
    run = lambda *args: subprocess.run(base+list(args), capture_output=True, text=True, timeout=60)
    dry = run(ACCOUNT, '--media-recover', '--dry-run')
    assert dry.returncode == 0 and json.loads(dry.stdout)['matching'] == 1
    with psycopg.connect(Path(dsn).read_text().strip(), row_factory=dict_row) as c:
        assert c.execute('SELECT count(*) AS n FROM media_recovery').fetchone()['n'] == 0
    real = run(ACCOUNT, '--media-recover', '--limit', '5')
    assert real.returncode == 0 and json.loads(real.stdout)['queued'] == 1
    assert run(ACCOUNT, '--media-recover', '--limit', '51').stdout.strip() == 'invalid_limit'
    assert run('archive-test', '--media-recover').stdout.strip() == 'not_whatsapp_account'


# spec: ANYM-816
def test_a_schema_67_collector_never_takes_or_sweeps_a_held_recovery_lease(wa):
    # The schema-67 claim and its lease-expiry sweep select a leased job only by lease_until<clock_timestamp()
    # (archive.py at 5c65f1c9); a recovery lease keeps media_jobs.lease_until far ahead, also once it expired.
    ar, s = wa
    fid = failed(ar, s, '1', attempts=2)
    recover(s)
    leased = claim(ar)
    expire(s, fid)
    with s.pool.connection() as c:
        assert c.execute("""SELECT count(*) AS n FROM media_jobs WHERE state='leased'
            AND lease_until<clock_timestamp()""").fetchone()['n'] == 0
        assert c.execute("SELECT lease_until>clock_timestamp()+interval '1000 years' AS held FROM media_jobs WHERE id=%s",
            (fid,)).fetchone()['held']
    # The new paths read the real expiry: the stale worker is fenced, renew refuses, and the job resumes.
    with pytest.raises(RuntimeError, match='media_lease_lost'): ar.renew_media(leased)
    with pytest.raises(RuntimeError, match='media_lease_lost'): ar.fail_media(leased, 'network_error')
    again = claim(ar)
    assert again['id'] == fid and recovery(s, fid)['attempts'] == 2
    ar.renew_media(again)
    assert job(s, fid)['error_code'] == 'media_network_error' and job(s, fid)['attempts'] == 2


# spec: ANYM-816
def test_status_of_a_chat_restricted_key_carries_no_account_wide_recovery(wa):
    from messaging_service.sync_status import get_sync_status
    ar, s = wa
    failed(ar, s, '1'); other = failed(ar, s, '2', chat='15550000002@c.us')
    recover(s)
    with s.pool.connection() as c:
        chat = c.execute('SELECT m.chat FROM media_jobs j JOIN messages m ON m.id=j.message WHERE j.id=%s',
            (other,)).fetchone()['chat']
        scoped = get_sync_status(c, {'accounts': [ACCOUNT], 'chat_ids': [chat]})
        full = get_sync_status(c, {'accounts': [ACCOUNT], 'chat_ids': None})
    assert 'download_recovery' not in json.dumps(scoped, default=str)
    assert all('recovery' not in (x.get('operations') or {}).get('media', {}) for x in scoped['sources'])
    source = next(x for x in full['sources'] if x['account'] == ACCOUNT)
    assert source['operations']['media']['recovery']['queued'] == 2


# spec: ANYM-816
def test_status_counts_recovery_of_an_account_whose_only_file_is_deleted(wa):
    from messaging_service.sync_status import get_sync_status
    ar, s = wa
    fid = failed(ar, s, '1')
    delete(s, fid)
    assert recover(s)['refused'] == {'message_deleted': 1}
    with s.pool.connection() as c:
        status = get_sync_status(c, {'accounts': [ACCOUNT], 'chat_ids': None})
    source = next(x for x in status['sources'] if x['account'] == ACCOUNT)
    assert source['operations']['media']['recovery']['refused'] == 1


# spec: ANYM-816
def test_recovery_status_without_the_table_grant_is_unknown_and_the_rest_still_reads(wa):
    from messaging_service.operational import operations
    from messaging_service.sync_status import empty_files
    ar, s = wa
    failed(ar, s, '1'); recover(s)
    text = (SOURCE/'gateway_role.sql').read_text()
    template = SOURCE/'tmp_gateway_role_069.sql'
    template.write_text(text[:text.rindex('-- Schema 69')])
    try:
        role = install(s, template.name, 'messaging_gateway')
    finally:
        template.unlink()
    try:
        with as_role(s, role) as c:
            assert mr.counts(c, [ACCOUNT]) is None
            # The status transaction goes on after the refused read.
            assert c.execute('SELECT count(*) AS n FROM media_jobs WHERE account=%s', (ACCOUNT,)).fetchone()['n'] == 1
    finally:
        remove(s, role)
    files = {**empty_files(), 'download': {'queued': 0, 'leased': 0, 'failed': 1}, 'download_recovery': None}
    media = operations({}, {}, files)['media']
    assert media['recovery'] is None and media['state'] == 'needs_attention'


codes = st.sampled_from(sorted({'media_network_error', 'media_timeout', 'media_http_500', 'media_http_429',
    'network_error', 'media_unavailable', 'unavailable', 'media_decrypt_failed', 'size_limit', 'disk_reserve',
    'paused_storage', 'storage_unavailable', 'rate_limited'}))


# spec: ANYM-816
@given(st.integers(1, mr.RECOVERY_BUDGET), codes, st.booleans(), st.none() | st.integers(1, 10**6))
def test_an_outcome_never_exceeds_the_budget_and_ends_once_it_is_spent(attempts, code, permanent, retry_after):
    local = code in ('disk_reserve', 'paused_storage', 'storage_unavailable')
    state, spent, wait = mr.outcome(attempts, code, permanent=permanent, retry_after=retry_after, local=local)
    assert state in ('queued', 'provider_unavailable', 'still_failing') and 0 <= spent <= mr.RECOVERY_BUDGET
    if local: assert (state, spent) == ('queued', attempts-1)
    elif state == 'queued':
        assert spent == attempts < mr.RECOVERY_BUDGET and not permanent and wait >= (retry_after or 0)
    else:
        assert wait == 0 and spent == attempts
        assert (state == 'provider_unavailable') == (code in mr.PROVIDER_UNAVAILABLE_CODES)
    if attempts == mr.RECOVERY_BUDGET and not local: assert state != 'queued'
