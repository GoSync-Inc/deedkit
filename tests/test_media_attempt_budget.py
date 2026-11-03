"""A transient media failure is retried over more than a day; a provider wait is not an attempt (ANYM-699)."""
from pathlib import Path
import re

import pytest

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


# spec: ANYM-699
def test_request_wakes_a_job_in_a_long_backoff_without_spending_an_attempt(archive):
    ar,s,key,_=archive; fid=pending(ar,s)
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET next_due=now()+interval '12 hours',error_code='network_error',attempts=4 WHERE id=%s",(fid,))
    result=s.request_file_download(key,fid)
    assert result['request_status']=='queued'
    row=job_row(s,fid)
    assert row['attempts']==4 and row['wait']<=0
    job=ar.claim_media('worker')
    assert job and job['id']==fid and job['attempts']==5


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
