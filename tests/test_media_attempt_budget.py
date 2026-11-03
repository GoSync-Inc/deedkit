"""A transient media failure is retried over more than a day; a provider wait is not an attempt (ANYM-699)."""
from contextlib import contextmanager
from copy import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from messaging_service.attachment_metadata import attachment_fields
from messaging_service.status_details import describe
from test_archive import archive as archive, media_event
from test_attachment_metadata import pending as pending_row
from test_media_requests import pending

SOURCES = Path(__file__).resolve().parents[1] / 'services/messaging/messaging_service'


def budget():
    from messaging_service import media_retry
    return media_retry


def job_row(s, fid):
    with s.pool.connection() as c:
        return c.execute('''SELECT state,error_code,attempts,
            EXTRACT(epoch FROM next_due-clock_timestamp()) AS wait FROM media_jobs WHERE id=%s''',(fid,)).fetchone()


def one_job(ar, s, **changes):
    ar.ingest(media_event(**changes))
    with s.pool.connection() as c:
        return c.execute('SELECT id FROM media_jobs ORDER BY created_at DESC LIMIT 1').fetchone()['id']


def due_now(s, fid):
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET next_due=clock_timestamp()-interval '1 second' WHERE id=%s",(fid,))


# spec: ANYM-699
def test_budget_constants_pin_the_schedule_and_no_module_keeps_the_literal_cap():
    m=budget()
    assert m.MEDIA_MAX_ATTEMPTS==8
    assert m.MEDIA_RETRY_DELAYS==(30,300,1800,7200,21600,43200,43200)
    assert len(m.MEDIA_RETRY_DELAYS)==m.MEDIA_MAX_ATTEMPTS-1
    assert sum(m.MEDIA_RETRY_DELAYS)==117330  # 32.6 hours
    assert m.MEDIA_PERMANENT_CODES=={'unavailable','size_limit'}
    for name in ('archive.py','attachment_metadata.py','media_requests.py'):
        text=(SOURCES/name).read_text()
        assert not re.search(r"attempts'?\]?\)?\s*(>=|<)\s*5\b",text),name


# spec: ANYM-699
@pytest.mark.parametrize('code',['network_error','media_timeout','integrity_error'])
def test_transient_failure_follows_the_long_schedule_then_fails_on_the_last_attempt(archive,code):
    ar,s,_,_=archive; m=budget(); fid=one_job(ar,s)
    for attempt in range(1,m.MEDIA_MAX_ATTEMPTS):
        due_now(s,fid)
        job=ar.claim_media('worker'); assert job and job['attempts']==attempt
        ar.fail_media(job,code)
        row=job_row(s,fid); base=m.MEDIA_RETRY_DELAYS[attempt-1]
        assert (row['state'],row['attempts'],row['error_code'])==('queued',attempt,code)
        assert base-3<=row['wait']<=base*1.1, (attempt,row['wait'])
        assert ar.claim_media('worker') is None
    due_now(s,fid)
    job=ar.claim_media('worker'); assert job['attempts']==m.MEDIA_MAX_ATTEMPTS
    ar.fail_media(job,code)
    row=job_row(s,fid)
    assert (row['state'],row['attempts'],row['error_code'])==('failed',m.MEDIA_MAX_ATTEMPTS,code)


# spec: ANYM-699
@pytest.mark.parametrize('code',['unavailable','size_limit'])
def test_permanent_code_is_terminal_at_the_first_failure(archive,code):
    ar,s,_,_=archive; fid=one_job(ar,s)
    ar.fail_media(ar.claim_media('worker'),code)
    row=job_row(s,fid)
    assert (row['state'],row['attempts'],row['error_code'])==('failed',1,code)


# spec: ANYM-699
def test_provider_wait_spends_no_attempt_until_the_job_is_a_week_old(archive):
    ar,s,_,_=archive; fid=one_job(ar,s)
    job=ar.claim_media('worker'); assert job['attempts']==1
    ar.fail_media(job,'rate_limited',retry_after=900)
    row=job_row(s,fid)
    assert (row['state'],row['attempts'],row['error_code'])==('queued',0,'rate_limited')
    assert 897<=row['wait']<=900*1.1
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET created_at=now()-interval '8 days',next_due=now() WHERE id=%s",(fid,))
    job=ar.claim_media('worker'); assert job['attempts']==1
    ar.fail_media(job,'rate_limited',retry_after=900)
    row=job_row(s,fid)
    assert (row['state'],row['attempts'])==('queued',1) and 897<=row['wait']<=900*1.1


# spec: ANYM-699
def test_provider_wait_longer_than_the_schedule_is_honored_exactly_as_a_floor(archive):
    ar,s,_,_=archive; fid=one_job(ar,s)
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET attempts=3 WHERE id=%s",(fid,))
    job=ar.claim_media('worker')
    ar.fail_media(job,'network_error',retry_after=40000)
    row=job_row(s,fid)
    assert (row['attempts'],row['state'])==(4,'queued') and 39997<=row['wait']<=40000*1.1


# spec: ANYM-699
def test_expired_last_lease_fails_with_retry_exhausted_but_an_earlier_one_is_reclaimed(archive):
    ar,s,key,_=archive; m=budget(); fid=one_job(ar,s)
    ar.claim_media('worker')
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET attempts=5,lease_until=now()-interval '1 second' WHERE id=%s",(fid,))
    again=ar.claim_media('worker')
    assert again and again['id']==fid and again['attempts']==6
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET attempts=%s,lease_until=now()-interval '1 second' WHERE id=%s",
            (m.MEDIA_MAX_ATTEMPTS,fid))
    ar._media_sweeps.clear()
    assert ar.claim_media('worker') is None
    row=job_row(s,fid)
    assert (row['state'],row['error_code'])==('failed','retry_exhausted')


# spec: ANYM-699
def test_job_waiting_a_long_backoff_does_not_block_a_job_that_is_due(archive):
    ar,s,_,_=archive
    waiting=one_job(ar,s,provider_id='31')
    ready=one_job(ar,s,provider_id='32')
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET next_due=now()+interval '6 hours',error_code='network_error',attempts=4 WHERE id=%s",(waiting,))
    job=ar.claim_media('worker')
    assert job['id']==ready
    ar.fail_media(job,'unavailable')
    assert ar.claim_media('worker') is None


@contextmanager
def gateway(s):
    """The request path as the gateway role runs it in production: gateway_role.sql grants only."""
    role='media_gateway_'+uuid4().hex
    ddl=(SOURCES/'gateway_role.sql').read_text().replace('messaging_gateway',role)
    class RestrictedPool:
        @contextmanager
        def connection(self):
            with s.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                yield c
    restricted=copy(s);restricted.pool=RestrictedPool();restricted.role=role
    with s.pool.connection() as c:c.execute(ddl)
    try:yield restricted
    finally:
        with s.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


# spec: ANYM-699
def test_request_wakes_a_job_in_a_long_backoff_without_spending_an_attempt(archive):
    ar,s,key,_=archive; fid=pending(ar,s)
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET next_due=now()+interval '12 hours',error_code='network_error',attempts=4 WHERE id=%s",(fid,))
    with gateway(s) as restricted:
        result=restricted.request_file_download(key,fid)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with restricted.pool.connection() as c:c.execute("UPDATE media_jobs SET next_due=now()")
    assert result['request_status']=='queued' and result['next_due'] is not None
    row=job_row(s,fid)
    assert row['attempts']==4 and row['wait']<=0
    assert datetime.fromisoformat(str(result['next_due']).replace('Z','+00:00'))<=datetime.now(timezone.utc)+timedelta(seconds=2)
    job=ar.claim_media('worker')
    assert job and job['id']==fid and job['attempts']==5


# spec: ANYM-699
def test_request_made_before_a_long_backoff_wakes_the_job_on_the_next_request(archive):
    ar,s,key,_=archive; fid=pending(ar,s)
    with gateway(s) as restricted:
        assert restricted.request_file_download(key,fid)['request_status']=='queued'
        job=ar.claim_media('worker'); assert job['id']==fid
        with s.pool.connection() as c:c.execute("UPDATE media_jobs SET attempts=3 WHERE id=%s",(fid,))
        ar.fail_media(job,'network_error')
        with s.pool.connection() as c:
            c.execute("UPDATE media_jobs SET next_due=now()+interval '12 hours',attempts=4 WHERE id=%s",(fid,))
        assert job_row(s,fid)['wait']>11*3600
        # The request window is still open, so the answer is the coalesced one, and the backoff still yields.
        again=restricted.request_file_download(key,fid)
    assert again['request_status']=='coalesced'
    row=job_row(s,fid)
    assert row['attempts']==4 and row['wait']<=0


# spec: ANYM-699
def test_wake_function_touches_only_a_queued_job_with_an_open_request_that_is_not_rate_limited(archive):
    ar,s,key,_=archive
    cases={}
    for name in ('queued','cooldown','ready','failed','leased','no_request','expired_request','due'):
        cases[name]=pending(ar,s,mid='w-'+name)
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET requested_until=now()+interval '1 hour',next_due=now()+interval '5 hours',error_code='network_error'")
        c.execute("UPDATE media_jobs SET error_code='rate_limited' WHERE id=%s",(cases['cooldown'],))
        c.execute("UPDATE media_jobs SET state='ready' WHERE id=%s",(cases['ready'],))
        c.execute("UPDATE media_jobs SET state='failed' WHERE id=%s",(cases['failed'],))
        c.execute("UPDATE media_jobs SET state='leased',lease_until=now()+interval '1 hour' WHERE id=%s",(cases['leased'],))
        c.execute("UPDATE media_jobs SET requested_until=NULL WHERE id=%s",(cases['no_request'],))
        c.execute("UPDATE media_jobs SET requested_until=now()-interval '1 second' WHERE id=%s",(cases['expired_request'],))
        c.execute("UPDATE media_jobs SET next_due=now()-interval '1 minute' WHERE id=%s",(cases['due'],))
        woken={name:c.execute('SELECT public.wake_media_job(%s) AS due',(fid,)).fetchone()['due'] is not None
            for name,fid in cases.items()}
        untouched=c.execute('SELECT count(*) AS n FROM media_jobs WHERE next_due>clock_timestamp()').fetchone()['n']
    assert woken=={'queued':True,'cooldown':False,'ready':False,'failed':False,'leased':False,
        'no_request':False,'expired_request':False,'due':False}
    assert untouched==6


# spec: ANYM-699
def test_restore_without_acls_does_not_leave_wake_media_job_open_to_public(archive):
    _,s,_,_=archive
    wake="'public.wake_media_job(text)'::regprocedure"
    role='media_ungranted_'+uuid4().hex
    try:
        with s.pool.connection() as c:c.execute(sql.SQL('CREATE ROLE {}').format(sql.Identifier(role)))
        # The gateway role exists with its grant before the ACLs are lost and stays through both migrations.
        with gateway(s) as restricted:
            with s.pool.connection() as c:
                c.execute('GRANT EXECUTE ON FUNCTION public.wake_media_job(text) TO PUBLIC')  # what a restore does
                assert c.execute(f"SELECT has_function_privilege('public',{wake},'EXECUTE') AS open").fetchone()['open'] is True
            s.migrate();s.migrate()
            with s.pool.connection() as c:
                assert c.execute(f"SELECT has_function_privilege('public',{wake},'EXECUTE') AS open").fetchone()['open'] is False
            with restricted.pool.connection() as c:
                assert c.execute(f"SELECT has_function_privilege({wake},'EXECUTE') AS ok").fetchone()['ok'] is True
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with s.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                c.execute("SELECT public.wake_media_job('none')")
    finally:
        with s.pool.connection() as c:
            c.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))


# spec: ANYM-699
def test_restore_without_acls_does_not_leave_queue_managed_original_open_to_public(archive):
    _,s,_,_=archive
    queue="'public.queue_managed_original(text)'::regprocedure"
    role='media_ungranted_'+uuid4().hex
    try:
        with s.pool.connection() as c:c.execute(sql.SQL('CREATE ROLE {}').format(sql.Identifier(role)))
        with gateway(s) as restricted:
            with s.pool.connection() as c:
                c.execute('GRANT EXECUTE ON FUNCTION public.queue_managed_original(text) TO PUBLIC')  # what a restore does
                assert c.execute(f"SELECT has_function_privilege('public',{queue},'EXECUTE') AS open").fetchone()['open'] is True
            s.migrate();s.migrate()
            with s.pool.connection() as c:
                assert c.execute(f"SELECT has_function_privilege('public',{queue},'EXECUTE') AS open").fetchone()['open'] is False
            with restricted.pool.connection() as c:
                assert c.execute(f"SELECT has_function_privilege({queue},'EXECUTE') AS ok").fetchone()['ok'] is True
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with s.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                c.execute("SELECT public.queue_managed_original('none')")
    finally:
        with s.pool.connection() as c:
            c.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))


# spec: ANYM-699
def test_restore_without_acls_does_not_leave_the_lifecycle_functions_of_033_open_to_public(archive):
    _,s,_,_=archive
    names=('touch_original(text,bigint)','begin_backup_fence(text)','end_backup_fence(text)',
        'register_original_residency()','register_resident_original(text,bigint)','lock_lifecycle_sources(text)')
    def regproc(name):return f"'public.{name}'::regprocedure"
    def open_to_public(c):
        return [n for n in names if c.execute(f"SELECT has_function_privilege('public',{regproc(n)},'EXECUTE') AS open").fetchone()['open']]
    def grants(c):  # every explicit non-PUBLIC ACL entry: the roles' own grants must survive the repair
        return {n:c.execute(f"""SELECT a.grantee::regrole::text AS grantee,a.grantor::regrole::text AS grantor,
            a.privilege_type,a.is_grantable FROM pg_proc p
            CROSS JOIN LATERAL aclexplode(p.proacl) a WHERE p.oid={regproc(n)} AND a.grantee<>0
            ORDER BY 1,2,3""").fetchall() for n in names}
    with gateway(s) as restricted:  # the gateway role holds EXECUTE on touch_original (gateway_role.sql:75)
        try:
            with s.pool.connection() as c:
                before=grants(c)
                assert open_to_public(c)==[]
                for n in names:c.execute(f'GRANT EXECUTE ON FUNCTION public.{n} TO PUBLIC')  # what a restore does
                assert open_to_public(c)==list(names)
            s.migrate();s.migrate()
            with s.pool.connection() as c:
                assert open_to_public(c)==[]
                assert grants(c)==before
            with restricted.pool.connection() as c:
                assert c.execute(f"SELECT has_function_privilege({regproc(names[0])},'EXECUTE') AS ok").fetchone()['ok'] is True
        finally:  # the session database outlives a failing test: close what the simulated restore opened
            with s.pool.connection() as c:
                for n in names:c.execute(f'REVOKE ALL ON FUNCTION public.{n} FROM PUBLIC')


# spec: ANYM-699
@pytest.mark.parametrize('case',['function_missing','grant_missing'])
def test_request_is_recorded_and_the_job_keeps_waiting_when_the_wake_is_not_available(archive,case):
    ar,s,key,_=archive; fid=pending(ar,s)
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET next_due=now()+interval '12 hours',error_code='network_error',attempts=4 WHERE id=%s",(fid,))
    text=(SOURCES/'migrations/066_media_attempt_budget.sql').read_text()
    try:
        with gateway(s) as restricted:
            with s.pool.connection() as c:
                if case=='function_missing':c.execute('DROP FUNCTION public.wake_media_job(text)')
                else:c.execute(sql.SQL('REVOKE EXECUTE ON FUNCTION public.wake_media_job(text) FROM {}').format(sql.Identifier(restricted.role)))
            result=restricted.request_file_download(key,fid)
    finally:
        with s.pool.connection() as c:c.execute(text)
    assert result['request_status']=='queued' and result['requested_until']
    row=job_row(s,fid)
    assert row['attempts']==4 and row['wait']>11*3600


# spec: ANYM-699
@pytest.mark.parametrize('attempts',[5,7])
def test_evicted_original_that_succeeded_late_can_still_be_requested_again(archive,attempts):
    from test_file_lifecycle import lifecycle,processed
    ar,s,key,_=archive; fid,blob=processed(archive)
    with s.pool.connection() as c:c.execute('UPDATE media_jobs SET attempts=%s WHERE id=%s',(attempts,fid))
    assert lifecycle().LifecycleWorker(s).run_once(apply=True)['evicted']==1
    with gateway(s) as restricted:
        result=restricted.request_file_download(key,fid)
    assert result['request_status']=='queued' and result['reason']=='managed_original_evicted'
    job=ar.claim_media('fixture')
    assert job['id']==fid and job['attempts']==attempts+1


# spec: ANYM-699
def test_migration_066_pins_the_cap_is_repeatable_and_keeps_the_acl(archive):
    _,s,_,_=archive
    text=(SOURCES/'migrations/066_media_attempt_budget.sql').read_text()
    assert f'j.attempts<{budget().MEDIA_MAX_ATTEMPTS} ' in text
    code='\n'.join(line for line in text.splitlines() if not line.startswith('--'))
    assert not re.search(r'\b(CREATE\s+TABLE|CREATE\s+INDEX|ALTER\s+TABLE|DROP|INSERT|DELETE)\b|\bGRANT\b',code,re.I)
    queue="'public.queue_managed_original(text)'::regprocedure"
    wake="'public.wake_media_job(text)'::regprocedure"
    with s.pool.connection() as c:
        before=c.execute(f'SELECT proacl::text AS acl,proowner::regrole::text AS owner FROM pg_proc WHERE oid={queue}').fetchone()
        c.execute(text);c.execute(text)
        assert c.execute(f'SELECT proacl::text AS acl,proowner::regrole::text AS owner FROM pg_proc WHERE oid={queue}').fetchone()==before
        assert c.execute(f'SELECT prosecdef FROM pg_proc WHERE oid={wake}').fetchone()['prosecdef'] is True
        assert c.execute(f"SELECT has_function_privilege('public',{wake},'EXECUTE') AS open").fetchone()['open'] is False
    assert 'GRANT EXECUTE ON FUNCTION public.wake_media_job(text) TO messaging_gateway' in (SOURCES/'gateway_role.sql').read_text()


# spec: ANYM-699
def test_request_keeps_a_failed_job_failed_and_a_provider_cooldown_running(archive):
    ar,s,key,_=archive; fid=pending(ar,s); other=pending(ar,s,mid='202')
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET next_due=now()+interval '3 hours',error_code='rate_limited' WHERE id=%s",(fid,))
        c.execute("UPDATE media_jobs SET state='failed',error_code='network_error',attempts=8 WHERE id=%s",(other,))
    held=s.request_file_download(key,fid)
    assert held['request_status']=='deferred' and job_row(s,fid)['wait']>3*3600-60
    failed=s.request_file_download(key,other)
    assert failed['request_status']=='failed' and failed['reason']=='network_error'
    assert job_row(s,other)['state']=='failed'


# spec: ANYM-699
def test_status_reports_queued_while_attempts_remain_and_failed_after_the_last(archive):
    ar,s,key,_=archive; m=budget()
    queued=pending_row(attempts=5,error_code='network_error')
    assert attachment_fields(queued)['download_status']=='queued'
    expired=pending_row(job_state='leased',transfer_active=False)
    assert attachment_fields({**expired,'attempts':5})['download_status']=='queued'
    last=attachment_fields({**expired,'attempts':m.MEDIA_MAX_ATTEMPTS})
    assert last['download_status']=='failed' and last['reason']=='retry_exhausted'
    fid=pending(ar,s)
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET attempts=5,error_code='network_error',next_due=now()+interval '2 hours' WHERE id=%s",(fid,))
    assert s.get_attachment(key,fid)['status']=='queued'


# spec: ANYM-699
def test_network_error_text_says_when_the_last_retry_is_due():
    text=describe('network_error',job_state='queued')
    assert text['retry_scheduled'] is True
    assert '32' in describe('network_error')['explanation'] and '8' in describe('network_error')['explanation']


# spec: ANYM-699
def test_a_job_that_is_already_failed_stays_failed(archive):
    ar,s,key,_=archive; fid=pending(ar,s)
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET state='failed',error_code='network_error',attempts=5 WHERE id=%s",(fid,))
    ar._media_sweeps.clear()
    assert ar.claim_media('worker') is None
    assert s.request_file_download(key,fid)['request_status']=='failed'
    row=job_row(s,fid)
    assert (row['state'],row['attempts'],row['error_code'])==('failed',5,'network_error')
