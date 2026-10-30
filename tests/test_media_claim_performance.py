"""An idle media claim reaches only claimable jobs, however large the dormant backlog."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import re
import time

import pytest

from messaging_service import archive as archive_module
from messaging_service.blobs import BlobWriter
from messaging_service.envelope import stable_id
from test_archive import archive as archive, event


ACCOUNT='archive-test'
DAYS=7
LIVE_MAX=32*1024**2
LIVE_WINDOW=7*86400
BOUND=100
TOUCHES=re.compile(r'\b(media_jobs|messages)\b')


def message(ar, provider_id, age, *, account=ACCOUNT, chat='100'):
    at=(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat()
    return ar.ingest(event(account=account,chat=chat,provider_id=provider_id,sent_at=at,updated_at=at,
        media=[{'provider_id':'media-'+provider_id,'filename':'voice.ogg','mime':'audio/ogg','bytes':4}]))['message']


def grow_dormant(s, count, offset=0):
    """Adds out-of-window, unrequested or expired-request jobs; none is claimable or needs housekeeping."""
    with s.pool.connection() as c:
        # Loading the synthetic backlog is setup, not a claim: a slow runner may take longer
        # than the store's 5 s serving timeout, which the claims below still run under.
        c.execute("SET LOCAL statement_timeout='120s'")
        chat=stable_id('chat',ACCOUNT,'100')
        c.execute('''INSERT INTO chats(id,account,title,provider_chat) VALUES(%s,%s,'Fixture room','100')
            ON CONFLICT DO NOTHING''',(chat,ACCOUNT))
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            SELECT 'dormant-msg-'||n,%s,%s,'dormant-'||n,'42','',
                clock_timestamp()-interval '8 days'-(n%%300)*interval '1 day'-(n%%1000)*interval '1 second'
            FROM generate_series(%s::int,%s::int) n''',(ACCOUNT,chat,offset+1,offset+count))
        # Explicit composition by n%%4: 0 queued/no request, 1 queued/expired request,
        # 2 queued after earlier attempts, 3 expired lease below the attempt limit.
        c.execute('''INSERT INTO media_jobs(id,account,message,descriptor,state,attempts,next_due,
                lease_owner,lease_epoch,lease_until,error_code,requested_until)
            SELECT 'dormant-job-'||n,%s,'dormant-msg-'||n,
                jsonb_build_object('provider_id','dormant-'||n,'mime','audio/ogg','bytes',4),
                CASE WHEN n%%4=3 THEN 'leased' ELSE 'queued' END,
                CASE n%%4 WHEN 2 THEN 1+n%%4 WHEN 3 THEN 1+n%%4 ELSE 0 END,
                clock_timestamp()-interval '1 minute',
                CASE WHEN n%%4=3 THEN 'old-worker' END,CASE WHEN n%%4=3 THEN 1 ELSE 0 END,
                CASE WHEN n%%4=3 THEN clock_timestamp()-interval '1 hour' END,
                CASE WHEN n%%2=0 THEN 'paused_storage' END,
                CASE WHEN n%%4 IN (1,3) THEN clock_timestamp()-interval '1 hour' END
            FROM generate_series(%s::int,%s::int) n''',(ACCOUNT,offset+1,offset+count))
        c.execute('ANALYZE messages'); c.execute('ANALYZE media_jobs'); c.execute('ANALYZE media_reservations')


def dormant_digest(s):
    with s.pool.connection() as c:
        return c.execute('''SELECT md5(string_agg(concat_ws('|',id,state,attempts,next_due,lease_owner,
            lease_epoch,lease_until,error_code,requested_until,descriptor::text),',' ORDER BY id)) AS digest
            FROM media_jobs WHERE id LIKE 'dormant-job-%' ''').fetchone()['digest']


def dormant_composition(s):
    with s.pool.connection() as c:
        return c.execute('''SELECT state,attempts,requested_until IS NOT NULL AS expired_request,
            count(*) AS jobs FROM media_jobs WHERE id LIKE 'dormant-job-%'
            GROUP BY 1,2,3 ORDER BY 1,2,3''').fetchall()


@contextmanager
def explained(ar, plans):
    """Runs EXPLAIN ANALYZE of each claim statement on media_jobs/messages in a rolled-back savepoint first."""
    pool=ar.store.pool; real=pool.connection

    class Explaining:
        def __init__(self,c): self.c=c
        def __getattr__(self,name): return getattr(self.c,name)
        def execute(self,query,params=None,**kwargs):
            if TOUCHES.search(query) and not query.lstrip().startswith('EXPLAIN'):
                self.c.execute('SAVEPOINT explain_claim')
                # Instrumentation is slower than the statement; the rollback restores the 5 s timeout.
                self.c.execute("SET LOCAL statement_timeout='120s'")
                plan=self.c.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) '+query,params).fetchone()
                self.c.execute('ROLLBACK TO SAVEPOINT explain_claim')
                plans.append((query,next(iter(plan.values()))[0]['Plan']))
                if 'FOR UPDATE OF j SKIP LOCKED' in query:
                    assert self.c.execute('SHOW plan_cache_mode').fetchone()['plan_cache_mode']=='force_custom_plan'
            return self.c.execute(query,params,**kwargs)

    @contextmanager
    def connection(*args,**kwargs):
        with real(*args,**kwargs) as c: yield Explaining(c)

    pool.connection=connection
    try: yield plans
    finally: pool.connection=real


def index_tables(s):
    with s.pool.connection() as c:
        return {r['indexname']:r['tablename'] for r in c.execute(
            "SELECT indexname,tablename FROM pg_indexes WHERE tablename IN ('media_jobs','messages')").fetchall()}


def outline(node, depth=0):
    line=('  '*depth+node['Node Type']+' '+str(node.get('Relation Name') or node.get('Index Name') or '')
        +f" est={node.get('Plan Rows')} rows={node.get('Actual Rows')}x{node.get('Actual Loops')}"
        +f" {node.get('Index Cond') or ''} {node.get('Filter') or ''} {node.get('Hash Cond') or ''}"[:300])
    return '\n'.join([line]+[outline(child,depth+1) for child in node.get('Plans',[])])


def work(plan, indexes):
    """Tuples examined (incl. filtered and rechecked rows, times loops) and buffers per relation."""
    totals={}
    def visit(node):
        relation=node.get('Relation Name') or indexes.get(node.get('Index Name'))
        if relation in ('media_jobs','messages'):
            assert 'Seq Scan' not in node['Node Type'],outline(plan)
            rows=node.get('Actual Rows',0)+node.get('Rows Removed by Filter',0)+node.get('Rows Removed by Index Recheck',0)
            entry=totals.setdefault(relation,{'tuples':0,'buffers':0})
            entry['tuples']+=rows*node.get('Actual Loops',1)
            if node['Node Type'] not in ('ModifyTable','LockRows'):
                entry['buffers']+=node.get('Shared Hit Blocks',0)+node.get('Shared Read Blocks',0)
        for child in node.get('Plans',[]): visit(child)
    visit(plan)
    return totals


def assert_bounded(plans, indexes):
    measured=[]
    for query,plan in plans:
        totals=work(plan,indexes)
        for relation,entry in totals.items():
            assert entry['tuples']<=BOUND and entry['buffers']<=BOUND,(query,relation,entry)
        measured.append((' '.join(query.split())[:60],totals))
    return measured


def selection_is_indexed(plans):
    query,plan=next((q,p) for q,p in plans if 'FOR UPDATE OF j SKIP LOCKED' in q)
    text=json.dumps(plan)
    assert 'media_requested' in text and 'message_scope_date' in text and 'media_message' in text
    return query


@pytest.fixture()
def backlog(archive, monkeypatch):
    ar,s,_,_=archive
    # A fixed sweep clock: only the first claim sweeps, however slow the runner is.
    monkeypatch.setattr(archive_module,'monotonic',lambda:1000.0)
    owner='perf-collector'; epoch=ar.acquire(ACCOUNT,owner)
    writer=BlobWriter(s.files,max_bytes=512*1024**2,reserve_bytes=0)
    live=BlobWriter(s.files,max_bytes=LIVE_MAX,reserve_bytes=0)
    since=datetime.now(timezone.utc)-timedelta(days=200)
    # Another account's recent job is claimable for it, never for this collector.
    ar.register('archive-other','telegram','Synthetic other',include_chats=['100'])
    other=message(ar,'other-1',300,account='archive-other')
    def claim(**changes):
        options={'account':ACCOUNT,'collector_lease':(owner,epoch),'since':since,'writer':writer,
            'live_writer':live,'live_window_seconds':LIVE_WINDOW,'media_prefetch_days':DAYS,**changes}
        return ar.claim_media(owner,**options)
    return ar,s,claim,other


def housekeeping(ar, s):
    """A small fixed population that the claim must still clean up outside the window.

    Returns the exhausted lease's message, the jobs of an expired and of an unexpired
    stale reservation (both reaped) and the job whose valid reservation is kept.
    """
    exhausted=message(ar,'exhausted',40*86400)
    jobs=[]
    with s.pool.connection() as c:
        c.execute('''UPDATE media_jobs SET state='leased',attempts=5,lease_owner='crashed',lease_epoch=5,
            lease_until=clock_timestamp()-interval '1 minute' WHERE message=%s''',(exhausted,))
        for name,expires in (('reaped-expired','-1 minute'),('reaped-stale','1 hour'),('kept','1 hour')):
            mid=message(ar,name,50*86400)
            job=c.execute('SELECT id FROM media_jobs WHERE message=%s',(mid,)).fetchone()['id']
            if name=='kept':c.execute('''UPDATE media_jobs SET state='leased',attempts=1,lease_owner='live-worker',
                lease_epoch=2,lease_until=clock_timestamp()+interval '1 hour' WHERE id=%s''',(job,))
            # The stale reservation is unexpired, but its job holds no matching lease.
            c.execute('''INSERT INTO media_reservations(job,volume_key,lease_epoch,reserved_bytes,expires_at)
                VALUES(%s,'fs:synthetic',2,4,clock_timestamp()+%s::interval)''',(job,expires))
            jobs.append(job)
    return exhausted,*jobs


def idle_claim(ar, s, claim, plans):
    indexes=index_tables(s)
    started=time.perf_counter()
    with explained(ar,plans): result=claim()
    elapsed=time.perf_counter()-started
    assert result is None
    return assert_bounded(plans,indexes),elapsed


def reaper_lookups(measured):
    return next(totals for query,totals in measured if query.startswith('DELETE FROM media_reservations'))


def test_idle_claim_work_does_not_grow_with_the_dormant_backlog(backlog, record_property):
    ar,s,claim,_=backlog
    exhausted,expired,stale,kept=housekeeping(ar,s)
    grow_dormant(s,30_000)
    composition=dormant_composition(s)
    assert sum(r['jobs'] for r in composition)==30_000
    before=dormant_digest(s)
    assert {(r['state'],r['attempts'],r['expired_request']) for r in composition}=={
        ('queued',0,False),('queued',0,True),('queued',3,False),('leased',4,True)}
    # First claim of the fixture: the sweep and the reaper act, and their plans are bounded too.
    first=[]
    with explained(ar,first): assert claim() is None
    assert_bounded([p for p in first if 'retry_exhausted' not in p[0]],index_tables(s))
    with s.pool.connection() as c:
        assert c.execute('SELECT state,error_code FROM media_jobs WHERE message=%s',(exhausted,)).fetchone()=={
            'state':'failed','error_code':'retry_exhausted'}
        assert [r['job'] for r in c.execute('SELECT job FROM media_reservations').fetchall()]==[kept]
    assert expired!=stale
    # Later idle claims skip the sweep; every remaining statement is index-driven and bounded.
    plans=[]; small,elapsed_small=idle_claim(ar,s,claim,plans)
    assert not any('retry_exhausted' in q for q,_ in plans)
    selection_is_indexed(plans)
    # The kept reservation makes the reaper look its job up on every measured claim.
    assert reaper_lookups(small)['media_jobs']['tuples']>=1
    retained={'30000':[{'query':q,'plan':p} for q,p in plans]}
    assert dormant_digest(s)==before
    grow_dormant(s,30_000,offset=30_000)
    grown=dormant_digest(s)
    assert sum(r['jobs'] for r in dormant_composition(s))==60_000
    plans=[]; large,elapsed_large=idle_claim(ar,s,claim,plans)
    selection_is_indexed(plans)
    assert reaper_lookups(large)['media_jobs']['tuples']>=1
    retained['60000']=[{'query':q,'plan':p} for q,p in plans]
    for (query,before),(_,after) in zip(small,large):
        for relation in set(before)|set(after):
            b=before.get(relation,{'tuples':0,'buffers':0}); a=after.get(relation,{'tuples':0,'buffers':0})
            assert a['tuples']==b['tuples'] and a['buffers']<=b['buffers']+2,(query,relation,b,a)
    # Dormant jobs are kept as they were: no attempts, reservations or rewritten errors,
    # provider references, due times, leases or requests.
    assert dormant_digest(s)==grown
    with s.pool.connection() as c:
        assert c.execute('''SELECT count(*) AS n FROM media_jobs WHERE id LIKE 'dormant-job-%' AND (
            attempts<>CASE WHEN replace(id,'dormant-job-','')::int%4 IN (2,3)
                THEN 1+replace(id,'dormant-job-','')::int%4 ELSE 0 END
            OR error_code IS DISTINCT FROM CASE WHEN replace(id,'dormant-job-','')::int%2=0 THEN 'paused_storage' END
            OR state NOT IN ('queued','leased'))''').fetchone()['n']==0
        assert [r['job'] for r in c.execute('SELECT job FROM media_reservations').fetchall()]==[kept]
        settings=c.execute("SELECT version() AS v,current_setting('work_mem') AS w,current_setting('random_page_cost') AS r").fetchone()
    # Both complete JSON plans are retained in the JUnit report (CI evidence artifact).
    record_property('media_claim_plans',json.dumps({'settings':settings,'cache':'warm',
        'composition':composition,'elapsed_ms':{'30000':elapsed_small*1000,'60000':elapsed_large*1000},
        'work':{'30000':small,'60000':large},'plans':retained},default=str))
    print(f'\n{settings} warm cache; 30k idle claim {elapsed_small*1000:.1f} ms, 60k {elapsed_large*1000:.1f} ms;'
        f' composition {composition}; work 30k {small}; 60k {large}')


def test_sweep_runs_again_after_the_interval(backlog, monkeypatch):
    ar,s,claim,_=backlog
    clock=[1000.0]; monkeypatch.setattr(archive_module,'monotonic',lambda:clock[0])
    assert claim() is None
    exhausted,*_=housekeeping(ar,s)
    clock[0]+=archive_module.MEDIA_SWEEP_SECONDS-1
    assert claim() is None
    with s.pool.connection() as c:
        assert c.execute('SELECT state FROM media_jobs WHERE message=%s',(exhausted,)).fetchone()['state']=='leased'
    clock[0]+=1
    assert claim() is None
    with s.pool.connection() as c:
        assert c.execute('SELECT state,error_code FROM media_jobs WHERE message=%s',(exhausted,)).fetchone()=={
            'state':'failed','error_code':'retry_exhausted'}


def test_failed_claim_does_not_count_as_a_sweep(backlog):
    ar,s,claim,_=backlog
    exhausted,*_=housekeeping(ar,s)
    with pytest.raises(RuntimeError,match='collector_lease_lost'): claim(collector_lease=('perf-collector',999))
    assert claim() is None
    with s.pool.connection() as c:
        assert c.execute('SELECT state FROM media_jobs WHERE message=%s',(exhausted,)).fetchone()['state']=='failed'


@pytest.mark.parametrize('kind',['requested','recent'])
def test_claimable_job_in_a_dormant_backlog_is_claimed_with_bounded_plans(backlog, kind):
    ar,s,claim,_=backlog
    grow_dormant(s,30_000)
    assert claim() is None  # runs the sweep, so the measured claim is an ordinary one
    if kind=='requested':
        target=message(ar,'wanted',90*86400)
        with s.pool.connection() as c:
            c.execute("UPDATE media_jobs SET requested_until=clock_timestamp()+interval '1 hour' WHERE message=%s",(target,))
    else: target=message(ar,'recent',3600)
    with s.pool.connection() as c: c.execute('ANALYZE media_jobs'); c.execute('ANALYZE messages')
    plans=[]
    with explained(ar,plans): job=claim()
    assert job['message']==target and job['attempts']==1
    assert job['_live_media'] is (kind=='recent')
    assert_bounded(plans,index_tables(s)); selection_is_indexed(plans)


def test_job_found_by_both_paths_is_claimed_once_and_locked_rows_are_skipped(backlog):
    ar,s,claim,other=backlog
    both=message(ar,'both',3600); second=message(ar,'second',7200)
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET requested_until=clock_timestamp()+interval '1 hour' WHERE message=%s",(both,))
    with s.pool.connection() as lock:
        top=lock.execute('SELECT id FROM media_jobs WHERE message=%s FOR UPDATE',(both,)).fetchone()['id']
        job=claim()
        assert job['message']==second
    job=claim()
    assert job['id']==top and job['attempts']==1
    assert claim() is None
    with s.pool.connection() as c:
        assert c.execute('SELECT state,attempts FROM media_jobs WHERE message=%s',(other,)).fetchone()=={
            'state':'queued','attempts':0}


def test_ordering_ties_follow_next_due_then_id(backlog):
    ar,s,claim,_=backlog
    ids=[]
    for name in ('tie-a','tie-b','tie-c'):
        mid=message(ar,name,40*86400)
        with s.pool.connection() as c:
            c.execute('''UPDATE media_jobs SET requested_until=clock_timestamp()+interval '1 hour',
                next_due='2026-01-01T00:00:00+00:00' WHERE message=%s''',(mid,))
            ids.append(c.execute('SELECT id FROM media_jobs WHERE message=%s',(mid,)).fetchone()['id'])
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET next_due='2025-12-31T00:00:00+00:00' WHERE id=%s",(ids[2],))
    claimed=[claim()['id'] for _ in ids]
    assert claimed==[ids[2]]+sorted(ids[:2])


@pytest.mark.parametrize('days',[0,DAYS,None])
def test_account_scoped_and_unscoped_claims_keep_eligibility(backlog, days):
    ar,s,claim,other=backlog
    old=message(ar,'old',30*86400); recent=message(ar,'recent',3600)
    before=message(ar,'before-since',250*86400)
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET requested_until=clock_timestamp()+interval '1 hour' WHERE message=%s",(before,))
    scoped=[]
    while (job:=claim(media_prefetch_days=days)): scoped.append(job['message'])
    expected={0:[],DAYS:[recent],None:[recent,old]}[days]
    assert scoped==expected
    # Without an account the claim reaches every account but keeps the same window rules.
    unscoped=[]
    while (job:=ar.claim_media('offline',media_prefetch_days=days)): unscoped.append(job['message'])
    remaining={0:[before],DAYS:[before,other],None:[before,other]}[days]
    assert sorted(unscoped)==sorted(remaining)


def test_window_path_keeps_the_future_time_bound(backlog):
    ar,s,claim,_=backlog
    near=message(ar,'near-future',-120); far=message(ar,'far-future',-600)
    assert claim()['message']==near
    assert claim() is None
    with s.pool.connection() as c:
        assert c.execute('SELECT state,attempts FROM media_jobs WHERE message=%s',(far,)).fetchone()=={
            'state':'queued','attempts':0}


@pytest.mark.parametrize('change',['deleted','excluded','disabled','superseded','cooldown','attempts'])
def test_window_path_keeps_collection_safety(backlog, change):
    ar,s,claim,_=backlog;mid=message(ar,'recent',3600)
    with s.pool.connection() as c:
        if change=='deleted':c.execute('UPDATE messages SET deleted=true WHERE id=%s',(mid,))
        elif change=='excluded':c.execute("UPDATE account_policies SET exclude_chats=ARRAY['100'] WHERE account=%s",(ACCOUNT,))
        elif change=='disabled':c.execute('UPDATE account_policies SET enabled=false WHERE account=%s',(ACCOUNT,))
        elif change=='superseded':c.execute("UPDATE media_jobs SET state='superseded' WHERE message=%s",(mid,))
        elif change=='cooldown':c.execute("UPDATE media_jobs SET next_due=clock_timestamp()+interval '1 hour' WHERE message=%s",(mid,))
        elif change=='attempts':c.execute('UPDATE media_jobs SET attempts=5 WHERE message=%s',(mid,))
    assert claim() is None


def test_concurrent_claims_do_not_repeat_a_due_sweep(backlog):
    ar,_,_,_=backlog
    with ar._media_sweep(ACCOUNT) as first:
        with ar._media_sweep(ACCOUNT) as second: assert first and not second
    with pytest.raises(RuntimeError):
        with ar._media_sweep('archive-other') as due:
            assert due; raise RuntimeError('rolled_back')
    with ar._media_sweep('archive-other') as due: assert due


def test_exhausted_lease_is_marked_within_one_interval_and_one_claim_cycle(backlog, monkeypatch):
    ar,s,claim,_=backlog
    cycle=1.3  # the collector's 1 s idle sleep plus claim time
    clock=[1000.0]; monkeypatch.setattr(archive_module,'monotonic',lambda:clock[0])
    assert claim() is None
    # Worst case: the lease expires right after a sweep.
    exhausted,*_=housekeeping(ar,s); expired_at=clock[0]
    while True:
        clock[0]+=cycle
        assert claim() is None
        with s.pool.connection() as c:
            state=c.execute('SELECT state FROM media_jobs WHERE message=%s',(exhausted,)).fetchone()['state']
        if state=='failed': break
        assert clock[0]-expired_at<60
    # The card's bound, independent of the implementation constant.
    assert clock[0]-expired_at<=60+cycle
