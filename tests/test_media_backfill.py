"""Old voice notes and small files download by themselves, paced; fresh media keeps priority."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from messaging_service.blobs import BlobWriter
from messaging_service.telegram_collector import configuration
from test_archive import archive, event
from test_collectors import worker
from test_live_media import config_path
from test_media_claim_performance import ACCOUNT, BOUND, backlog, explained, grow_dormant, index_tables, work


DAY=86400
SMALL=512*1024
LARGE=2*1024**2


def job(ar, provider_id, age, *, mime='audio/ogg', size=4, account=ACCOUNT):
    at=(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat()
    return ar.ingest(event(account=account,provider_id=provider_id,sent_at=at,updated_at=at,
        media=[{'provider_id':'media-'+provider_id,'filename':provider_id,'mime':mime,'bytes':size}]))['message']


def state(s, mid):
    with s.pool.connection() as c:
        return c.execute('SELECT state,attempts FROM media_jobs WHERE message=%s',(mid,)).fetchone()


# spec: ANYM-472
def test_window_claims_voice_then_small_then_rest_newest_first(backlog):
    ar,s,claim,_=backlog
    large=job(ar,'large-new',3600,mime='video/mp4',size=LARGE)
    small_old=job(ar,'small-old',3*DAY,mime='image/jpeg',size=SMALL)
    small_new=job(ar,'small-new',2*DAY,mime='image/jpeg',size=SMALL)
    voice_old=job(ar,'voice-old',5*DAY)
    voice_new=job(ar,'voice-new',4*DAY,mime='audio/opus')
    # Without the live tier every window job is ordered by kind, then by message age.
    order=[claim(live_writer=None)['message'] for _ in range(5)]
    assert order==[voice_new,voice_old,small_new,small_old,large]
    assert claim(live_writer=None) is None


# spec: ANYM-472
def test_backfill_claims_old_voice_then_small_never_large(backlog):
    ar,s,claim,_=backlog
    large=job(ar,'old-large',40*DAY,mime='video/mp4',size=LARGE)
    small=job(ar,'old-small',30*DAY,mime='image/jpeg',size=SMALL)
    voice=job(ar,'old-voice',60*DAY)
    assert claim() is None  # out of the window, the ordinary claim leaves them dormant
    assert [claim(backfill=True)['message'] for _ in range(2)]==[voice,small]
    assert claim(backfill=True) is None
    assert state(s,large)=={'state':'queued','attempts':0}


# spec: ANYM-472
def test_backfill_keeps_window_and_requests_first(backlog):
    ar,s,claim,_=backlog
    old=job(ar,'old-voice',30*DAY); recent=job(ar,'recent-large',3600,mime='video/mp4',size=LARGE)
    wanted=job(ar,'wanted-large',90*DAY,mime='video/mp4',size=LARGE)
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET requested_until=clock_timestamp()+interval '1 hour' WHERE message=%s",(wanted,))
    assert [claim(backfill=True,live_writer=None)['message'] for _ in range(3)]==[wanted,recent,old]


# spec: ANYM-472
@pytest.mark.parametrize('change',['before-since','deleted','excluded','disabled','cooldown','attempts'])
def test_backfill_respects_history_bound_and_exclusions(backlog, change):
    ar,s,claim,_=backlog
    mid=job(ar,'old-voice',250*DAY if change=='before-since' else 30*DAY)
    with s.pool.connection() as c:
        if change=='deleted':c.execute('UPDATE messages SET deleted=true WHERE id=%s',(mid,))
        elif change=='excluded':c.execute("UPDATE account_policies SET exclude_chats=ARRAY['100'] WHERE account=%s",(ACCOUNT,))
        elif change=='disabled':c.execute('UPDATE account_policies SET enabled=false WHERE account=%s',(ACCOUNT,))
        elif change=='cooldown':c.execute("UPDATE media_jobs SET next_due=clock_timestamp()+interval '1 hour' WHERE message=%s",(mid,))
        elif change=='attempts':c.execute('UPDATE media_jobs SET attempts=5 WHERE message=%s',(mid,))
    assert claim(backfill=True) is None


# spec: ANYM-472
@pytest.mark.parametrize('days',[0,None])
def test_backfill_needs_a_bounded_window(backlog, days):
    ar,s,claim,_=backlog
    mid=job(ar,'old-voice',30*DAY)
    job_=claim(backfill=True,media_prefetch_days=days)
    # 0 means explicit requests only; null already claims the whole history.
    assert (job_ is None) if days==0 else job_['message']==mid
    with pytest.raises(ValueError,match='invalid_backfill'):claim(backfill=1)


def backfill_claim_work(ar, s, claim, name):
    """Claims a fresh out-of-window voice note through backfill; returns the locked statement's work."""
    newest=job(ar,name,8*DAY-3600)
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='120s'"); c.execute('ANALYZE media_jobs'); c.execute('ANALYZE messages')
    assert claim() is None
    plans=[]
    with explained(ar,plans): got=claim(backfill=True)
    assert got['message']==newest and got['attempts']==1
    # The candidate read runs first, before the sweep, reaper and locks, and hands over at most 16 ids.
    picks=[i for i,(query,_) in enumerate(plans) if 'LIMIT %(limit)s' in query]
    assert picks==[0] and plans[0][1]['Actual Rows']<=16
    indexes=index_tables(s)
    for query,plan in plans[1:]:
        totals=work(plan,indexes)  # also refuses a sequential scan of media_jobs or messages
        assert all(entry['tuples']<=BOUND for entry in totals.values()),(query,totals)
    return next(work(plan,indexes) for query,plan in plans if 'FOR UPDATE OF j SKIP LOCKED' in query)


# spec: ANYM-472
def test_backfill_claim_in_a_dormant_backlog_picks_the_newest_voice_note(backlog):
    ar,s,claim,_=backlog
    grow_dormant(s,30_000)  # 4-byte voice notes from 8 to 308 days old
    claim()  # the first claim of the run sweeps
    small=backfill_claim_work(ar,s,claim,'newest-old-voice')
    grow_dormant(s,30_000,offset=30_000)
    large=backfill_claim_work(ar,s,claim,'newest-old-voice-2')
    # Under the locks the claim looks up only its 16 candidates: the work does not grow with the backlog.
    for relation,entry in large.items():
        assert entry['tuples']<=small[relation]['tuples']+16 and entry['buffers']<=small[relation]['buffers']+32,(small,large)


# spec: ANYM-472
def test_backfill_reclaims_an_expired_lease_and_fences_the_old_one(backlog):
    ar,s,claim,_=backlog
    mid=job(ar,'old-voice',30*DAY)
    crashed=claim(backfill=True); assert crashed['message']==mid
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE message=%s",(mid,))
    assert claim() is None  # out of the window, only backfill reaches the expired lease
    retry=claim(backfill=True)
    assert retry['message']==mid and retry['attempts']==2
    writer=BlobWriter(s.files,reserve_bytes=0); blob=writer.write([b'data'],expected_bytes=4)
    with pytest.raises(RuntimeError,match='media_lease_lost'): ar.finish_media(crashed,blob,writer)


# spec: ANYM-472
def test_backfill_claim_keeps_requests_in_due_order(backlog):
    ar,s,claim,_=backlog
    voice=job(ar,'wanted-voice',30*DAY); large=job(ar,'wanted-large',40*DAY,mime='video/mp4',size=LARGE)
    with s.pool.connection() as c:
        c.execute('''UPDATE media_jobs SET requested_until=clock_timestamp()+interval '1 hour',
            next_due=clock_timestamp()-CASE WHEN message=%s THEN interval '2 hours' ELSE interval '1 hour' END
            WHERE message=ANY(%s)''',(voice,[voice,large]))
    # The voice note is also a backfill candidate; among requests only next_due, id decide.
    assert [claim(backfill=True)['message'] for _ in range(2)]==[voice,large]


# spec: ANYM-472
def test_backfill_candidates_break_ties_by_next_due(backlog):
    ar,s,claim,_=backlog
    mids=[job(ar,f'tie-{n}',30*DAY) for n in range(17)]
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET sent_at=now()-interval '30 days' WHERE id=ANY(%s)",(mids,))
        last=c.execute('SELECT message FROM media_jobs WHERE message=ANY(%s) ORDER BY id DESC LIMIT 1',(mids,)).fetchone()['message']
        c.execute('''UPDATE media_jobs SET next_due=clock_timestamp()-CASE WHEN message=%s THEN interval '2 hours'
            ELSE interval '1 hour' END WHERE message=ANY(%s)''',(last,mids))
    # Seventeen equal voice notes: the earliest due one (highest id) is still among the 16 candidates.
    assert claim(backfill=True)['message']==last


@pytest.fixture()
def paced(worker):
    w,s,_=worker
    w.media_prefetch_days=7; w.media_backfill=True
    return w,s


def due_in(w):
    return w._backfill_due-time.monotonic()


# spec: ANYM-472
def test_collector_paces_backfill_and_idles_when_empty(paced):
    # The pause is checked on the collector's own deadline; a frozen clock would freeze asyncio too.
    w,s=paced
    first=job(w.archive,'91',40*DAY,account=w.account); second=job(w.archive,'92',50*DAY,account=w.account)
    assert asyncio.run(w.media_once()) is True and state(s,first)['state']=='ready'
    assert 10<due_in(w)<=15
    assert asyncio.run(w.media_once()) is False and state(s,second)=={'state':'queued','attempts':0}
    w._backfill_due=0
    assert asyncio.run(w.media_once()) is True and state(s,second)['state']=='ready'
    w._backfill_due=0
    assert asyncio.run(w.media_once()) is False  # nothing left: the next look is after the idle pause
    assert 595<due_in(w)<=600
    third=job(w.archive,'93',45*DAY,account=w.account)
    assert asyncio.run(w.media_once()) is False and state(s,third)['attempts']==0
    w._backfill_due=0
    assert asyncio.run(w.media_once()) is True and state(s,third)['state']=='ready'


# spec: ANYM-472
def test_collector_without_backfill_leaves_old_media_dormant(paced):
    w,s=paced
    w.media_backfill=False  # the constructor default, also what WhatsApp collectors use
    mid=job(w.archive,'91',40*DAY,account=w.account)
    assert asyncio.run(w.media_once()) is False
    assert state(s,mid)=={'state':'queued','attempts':0}


# spec: ANYM-472
def test_media_backfill_configuration(tmp_path):
    assert configuration(config_path(tmp_path))[0]['media_backfill'] is True
    assert configuration(config_path(tmp_path,media_backfill=False))[0]['media_backfill'] is False
    for value in (1,'true',None):
        with pytest.raises(ValueError,match='invalid_media_backfill'):
            configuration(config_path(tmp_path,media_backfill=value))
