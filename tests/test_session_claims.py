"""ANYM-914: one provider-session owner per account across hosts (session_claim.py, migration 078)."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace as NS

import psycopg
import pytest

from messaging_service import session_claim as sc

ACCOUNT='telegram-demo'


@pytest.fixture()
def locks(tmp_path):
    directory=tmp_path/'session-locks'; directory.mkdir(mode=0o700)
    boot=tmp_path/'boot_id'; boot.write_text('boot-one\n')
    return directory,boot


def held(directory,boot,account=ACCOUNT):
    return sc.host_lock(account,directory,boot_id_path=boot)


def claims(store):
    with store.pool.connection() as c: return c.execute('SELECT * FROM session_claims ORDER BY account').fetchall()


class Stop(Exception): pass


def stopping_sleep(limit,delays):
    async def sleep(seconds):
        delays.append(seconds)
        if len(delays)>=limit: raise Stop()
    return sleep


class FailingPool:
    def connection(self): raise psycopg.OperationalError('synthetic outage')


# spec: ANYM-914
def test_host_lock_creates_one_private_host_id_and_refuses_a_second_opener(locks):
    directory,boot=locks
    with held(directory,boot) as one:
        assert sc.HEX32.fullmatch(one.host_id) and one.boot_id=='boot-one'
        assert (directory/'host_id').stat().st_mode&0o777==0o600
        info=os.fstat(one.fd); assert one.lock_ref==f'{info.st_dev}:{info.st_ino}'
        # A second copy of the session directory on this host still meets the canonical lock.
        with pytest.raises(RuntimeError,match='session_already_owned'):
            with held(directory,boot): pytest.fail('second opener')
    with held(directory,boot) as again: assert again.host_id==one.host_id
    (directory/'host_id').write_text('not-an-id\n')
    with pytest.raises(RuntimeError,match='host_id_invalid'):
        with held(directory,boot): pytest.fail('malformed host_id')


# spec: ANYM-914
def test_same_kernel_reacquires_at_once_and_other_lock_domains_are_refused(seeded,locks,tmp_path):
    store,*_=seeded; directory,boot=locks
    with held(directory,boot) as lock: first=sc.acquire(store.pool,lock,'collector')
    # A crash: the row stays unreleased, the kernel dropped the flock; the same boot and lock file reacquire.
    with held(directory,boot) as lock: second=sc.acquire(store.pool,lock,'collector')
    assert (first.generation,second.generation)==(1,2) and first.holder_id!=second.holder_id
    # A clone or copied lock directory carries the same host_id but another lock file: refused.
    copy=tmp_path/'copied-locks'; copy.mkdir(mode=0o700)
    (copy/'host_id').write_bytes((directory/'host_id').read_bytes()); (copy/'host_id').chmod(0o600)
    with held(copy,boot) as lock:
        with pytest.raises(sc.ClaimRefused) as refused: sc.acquire(store.pool,lock,'collector')
    assert refused.value.code=='session_claimed_elsewhere' and refused.value.detail['same_host_id'] is True
    # The same directory after a reboot (another boot_id) after an unclean stop: refused, the safe direction.
    boot.write_text('boot-two\n')
    with held(directory,boot) as lock:
        with pytest.raises(sc.ClaimRefused,match='session_claimed_elsewhere'): sc.acquire(store.pool,lock,'collector')
    assert claims(store)[0]['generation']==2


# spec: ANYM-914
def test_clean_release_lets_another_host_acquire_and_stale_operations_change_nothing(seeded,locks,tmp_path):
    store,*_=seeded; directory,boot=locks
    other=tmp_path/'other-host'; other.mkdir(mode=0o700); other_boot=tmp_path/'other-boot'; other_boot.write_text('b2')
    with held(directory,boot) as lock: first=sc.acquire(store.pool,lock,'collector')
    with held(other,other_boot) as lock:
        with pytest.raises(sc.ClaimRefused,match='session_claimed_elsewhere'): sc.acquire(store.pool,lock,'holder')
    assert sc.release(store.pool,first) is True
    with held(other,other_boot) as lock: successor=sc.acquire(store.pool,lock,'holder')
    # A delayed release or refresh of the earlier generation never touches the successor's row.
    assert sc.release(store.pool,first) is False and sc.refresh(store.pool,first) is False
    row=claims(store)[0]
    assert (row['generation'],row['holder_id'],row['released_at'])==(successor.generation,successor.holder_id,None)
    assert sc.refresh(store.pool,successor) is True


# spec: ANYM-914
def test_restore_hold_and_pre_claim_restore_refuse_until_the_admin_release(seeded,locks):
    store,*_=seeded; directory,boot=locks
    with held(directory,boot) as lock: claim=sc.acquire(store.pool,lock,'collector')
    sc.release(store.pool,claim)
    with store.pool.connection() as c:
        c.execute('UPDATE session_claims SET restore_hold=true')  # what quarantine_restore does at schema 78
    with held(directory,boot) as lock:
        with pytest.raises(sc.ClaimRefused,match='session_claim_restore_hold'): sc.acquire(store.pool,lock,'collector')
    row=sc.row(store.pool,ACCOUNT)
    with store.pool.connection() as c:
        assert c.execute(sc.ADMIN_RELEASE_SQL,(ACCOUNT,row['generation'],row['holder_id'],row['host_id'])).rowcount==1
    with held(directory,boot) as lock: assert sc.acquire(store.pool,lock,'collector').generation==2
    # A pre-claim backup restored and migrated: no row, source status restored_disabled.
    with store.pool.connection() as c:
        c.execute('DELETE FROM session_claims')
        c.execute('''INSERT INTO source_status(account,status) VALUES(%s,'restored_disabled')
            ON CONFLICT(account) DO UPDATE SET status='restored_disabled' ''',(ACCOUNT,))
    with held(directory,boot) as lock:
        with pytest.raises(sc.ClaimRefused,match='session_claim_restore_hold'): sc.acquire(store.pool,lock,'collector')
    assert claims(store)==[]
    with store.pool.connection() as c: c.execute(sc.ADMIN_RESTORE_SQL,(ACCOUNT,))
    with held(directory,boot) as lock: assert sc.acquire(store.pool,lock,'collector').generation==2


# spec: ANYM-914
def test_quarantine_restore_holds_every_claim(seeded,locks,monkeypatch):
    from messaging_service import backup
    store,*_=seeded; directory,boot=locks
    with held(directory,boot) as lock: sc.acquire(store.pool,lock,'collector')
    executed=[]
    class Recording:
        def __init__(self,c): self.c=c
        def execute(self,sql,*args):
            executed.append(sql)
            if 'session_claims' in sql: return self.c.execute(sql,*args)
            if 'current_database' in sql: return NS(fetchone=lambda:{'name':'x_restore_test'})
            if 'schema_version' in sql: return NS(fetchone=lambda:{'value':'79'})
            return NS(fetchone=lambda:None,fetchall=lambda:[],rowcount=0)
    class Pool:
        def connection(self):
            from contextlib import contextmanager
            @contextmanager
            def connect():
                with store.pool.connection() as c: yield Recording(c)
            return connect()
    monkeypatch.setattr(backup,'lifecycle_lock',lambda c: None,raising=False)
    import messaging_service.file_lifecycle as lifecycle
    monkeypatch.setattr(lifecycle,'lifecycle_lock',lambda c: None)
    backup.quarantine_restore(NS(pool=Pool()))
    assert 'UPDATE session_claims SET restore_hold=true' in executed
    assert [r['restore_hold'] for r in claims(store)]==[True]
    assert claims(store)[0]['host_id']  # identity untouched


# spec: ANYM-914
def test_refused_opener_waits_with_bounded_backoff_and_one_waiting_mark(seeded,locks,tmp_path):
    store,*_=seeded; directory,boot=locks
    other=tmp_path/'other-host'; other.mkdir(mode=0o700)
    with held(other,boot) as lock: sc.acquire(store.pool,lock,'collector')
    delays=[]
    with held(directory,boot) as lock:
        with pytest.raises(Stop):
            asyncio.run(sc.acquire_waiting(store.pool,lock,'collector',sleep=stopping_sleep(9,delays)))
        mine=lock.host_id
    assert delays==[5,10,20,40,80,160,300,300,300]
    row=claims(store)[0]
    assert row['waiting_host_id']==mine and row['generation']==1 and row['released_at'] is None


# spec: ANYM-914
def test_database_down_at_acquire_refuses_and_while_held_never_stops(locks):
    directory,boot=locks; delays=[]
    with held(directory,boot) as lock:
        with pytest.raises(Stop):
            asyncio.run(sc.acquire_waiting(FailingPool(),lock,'collector',sleep=stopping_sleep(3,delays)))
    assert delays==[5,10,20]
    claim=sc.Claim(ACCOUNT,1,'0'*32,'collector'); ticks=[]
    with pytest.raises(Stop): asyncio.run(sc.keep(FailingPool(),claim,sleep=stopping_sleep(6,ticks)))
    assert len(ticks)==6  # six failed refreshes, no session_claim_lost


# spec: ANYM-914
def test_the_shutdown_release_is_bounded_and_a_timeout_keeps_the_row(seeded,locks):
    import time
    store,*_=seeded; directory,boot=locks
    with held(directory,boot) as lock: claim=sc.acquire(store.pool,lock,'collector')
    class Hung:
        def connection(self,timeout=None): time.sleep(3); raise psycopg.OperationalError('synthetic')
    started=time.monotonic()
    assert asyncio.run(sc.release_bounded(Hung(),claim,seconds=0.3)) is False
    assert time.monotonic()-started<2 and claims(store)[0]['released_at'] is None
    assert asyncio.run(sc.release_bounded(store.pool,claim)) is True and claims(store)[0]['released_by']=='holder'


# spec: ANYM-914
def test_positive_change_is_session_claim_lost(seeded,locks):
    store,*_=seeded; directory,boot=locks
    with held(directory,boot) as lock: claim=sc.acquire(store.pool,lock,'collector')
    with store.pool.connection() as c: c.execute('UPDATE session_claims SET generation=generation+1')
    with pytest.raises(RuntimeError,match='session_claim_lost'):
        asyncio.run(sc.keep(store.pool,claim,sleep=stopping_sleep(99,[])))


# spec: ANYM-914
def test_descendant_holding_the_inherited_lock_blocks_release_and_successors(locks):
    directory,boot=locks
    with held(directory,boot) as lock:
        child=subprocess.Popen([sys.executable,'-c','import sys,time; sys.stdin.read()'],stdin=subprocess.PIPE,
            pass_fds=(lock.fd,),start_new_session=True)
        try:
            # The parent dies, the child keeps the open file description: no successor, no release proof.
            assert sc.descendants_gone(lock) is False and lock.fd==-1
            with pytest.raises(RuntimeError,match='session_already_owned'):
                with held(directory,boot): pytest.fail('successor while the child lives')
        finally:
            child.stdin.close(); child.wait(10)
    with held(directory,boot) as lock: assert sc.descendants_gone(lock) is True


# spec: ANYM-914
def test_telegram_run_config_with_a_foreign_row_opens_no_session_store(seeded,locks,tmp_path,monkeypatch):
    from messaging_service import telegram_collector as tc
    store,*_=seeded; directory,boot=locks
    other=tmp_path/'other-host'; other.mkdir(mode=0o700)
    with held(other,boot) as lock: sc.acquire(store.pool,lock,'collector')
    sessions=tmp_path/'sessions'; sessions.mkdir(mode=0o700)
    session=sessions/'telegram.session'; session.write_bytes(b'FAKE'); session.chmod(0o600)
    monkeypatch.setattr(tc,'configuration',lambda _:({'account':ACCOUNT,'session_file':str(session),
        'session_lock_directory':str(directory)},{}))
    monkeypatch.setattr(sc,'BOOT_ID',boot)
    monkeypatch.setattr(tc,'Store',lambda *_: NS(pool=store.pool,close=lambda:None))
    monkeypatch.setattr(tc,'TelethonProvider',lambda *_: pytest.fail('session store opened'))
    real=sc.acquire_waiting
    monkeypatch.setattr(sc,'acquire_waiting',lambda pool,lock,kind: real(pool,lock,kind,sleep=stopping_sleep(1,[])))
    with store.pool.connection() as c:
        c.execute("UPDATE accounts SET synthetic=false WHERE id=%s",(ACCOUNT,))
    dsn=tmp_path/'synthetic.dsn'; dsn.write_text('unused')
    monkeypatch.setenv('DATABASE_URL_FILE',str(dsn)); monkeypatch.setenv('PROBE_FILES','/unused')
    with pytest.raises(Stop): asyncio.run(tc.run_config('/unused/synthetic.json'))
    assert claims(store)[0]['waiting_host_id'] is not None


def telegram_entry(monkeypatch,tmp_path,directory,boot,store_factory,module='telegram_collector'):
    """A Telegram opener (`run_config` or `pair_config`) on a private synthetic session; the provider fails the test."""
    import importlib
    tc=importlib.import_module('messaging_service.'+module)
    sessions=tmp_path/'sessions'; sessions.mkdir(mode=0o700,exist_ok=True)
    session=sessions/'telegram.session'; session.write_bytes(b'FAKE'); session.chmod(0o600)
    monkeypatch.setattr(tc,'configuration',lambda _:({'account':ACCOUNT,'session_file':str(session),
        'session_lock_directory':str(directory)},{}))
    monkeypatch.setattr(sc,'BOOT_ID',boot)
    monkeypatch.setattr(tc,'Store',store_factory)
    monkeypatch.setattr(tc,'TelethonProvider',lambda *_,**__: pytest.fail('session store opened'))
    if module=='telegram_pair': monkeypatch.setattr(tc,'require_owner_terminal',lambda: None)
    dsn=tmp_path/'synthetic.dsn'; dsn.write_text('unused')
    monkeypatch.setenv('DATABASE_URL_FILE',str(dsn)); monkeypatch.setenv('PROBE_FILES','/unused')
    return tc,session


# spec: ANYM-914
def test_database_down_at_start_waits_before_the_claim_and_never_crash_loops(locks,tmp_path,monkeypatch):
    from psycopg_pool import PoolTimeout
    directory,boot=locks; opened=[]
    def outage(*_): opened.append(1); raise PoolTimeout('synthetic outage')
    tc,_=telegram_entry(monkeypatch,tmp_path,directory,boot,outage)
    real,delays=sc.open_waiting,[]
    monkeypatch.setattr(sc,'open_waiting',lambda *a: real(*a,sleep=stopping_sleep(3,delays)))
    with pytest.raises(Stop): asyncio.run(tc.run_config('/unused/synthetic.json'))
    # Three waits inside one process (no exit for Compose to restart), no claim and no session store opened.
    assert delays==[5,10,20] and len(opened)==3
    # A store whose account check fails on the outage is closed before the next try; a refusal is not waited out.
    closed,checks=[],[]
    def check(store):
        checks.append(1)
        raise psycopg.OperationalError('synthetic') if len(checks)==1 else RuntimeError('live_account_required')
    with pytest.raises(RuntimeError,match='live_account_required'):
        asyncio.run(real(lambda: NS(close=lambda: closed.append(1)),check,ACCOUNT,sleep=stopping_sleep(9,[])))
    assert len(closed)==2 and len(checks)==2


# spec: ANYM-914
def test_telegram_pairing_with_a_foreign_row_is_refused_at_once_and_opens_no_session_store(seeded,locks,tmp_path,
                                                                                          monkeypatch):
    store,*_=seeded; directory,boot=locks
    other=tmp_path/'other-host'; other.mkdir(mode=0o700)
    with held(other,boot) as lock: sc.acquire(store.pool,lock,'collector')
    with store.pool.connection() as c: c.execute("UPDATE accounts SET synthetic=false WHERE id=%s",(ACCOUNT,))
    tc,_=telegram_entry(monkeypatch,tmp_path,directory,boot,lambda *_: NS(pool=store.pool,close=lambda:None),
                        module='telegram_pair')
    with pytest.raises(RuntimeError,match='session_claimed_elsewhere'): asyncio.run(tc.pair_config('/unused/synthetic.json'))
    row=claims(store)[0]
    assert row['generation']==1 and row['holder_kind']=='collector' and row['released_at'] is None


# spec: ANYM-914
@pytest.mark.parametrize('entry',['collector','pair'])
def test_native_whatsapp_openers_with_a_foreign_row_spawn_no_child(seeded,locks,tmp_path,monkeypatch,entry):
    from messaging_service import whatsapp_collector as wa, whatsapp_pair as wp
    store,*_=seeded; directory,boot=locks; account='whatsapp-demo-1'
    other=tmp_path/'other-host'; other.mkdir(mode=0o700)
    with held(other,boot,account) as lock: sc.acquire(store.pool,lock,'collector')
    with store.pool.connection() as c: c.execute("UPDATE accounts SET synthetic=false WHERE id=%s",(account,))
    sessions=tmp_path/'wa'; sessions.mkdir(mode=0o700)
    session=sessions/'whatsapp'; session.mkdir(mode=0o700)
    (session/'auth.sqlite').write_bytes(b'FAKE'); (session/'auth.sqlite').chmod(0o600)
    cfg={'account':account,'provider':'whatsmeow','session_directory':str(session),'native_executable':'/bin/false',
         'session_lock_directory':str(directory)}
    module=wa if entry=='collector' else wp
    monkeypatch.setattr(module,'configuration',lambda _: cfg)
    monkeypatch.setattr(sc,'BOOT_ID',boot)
    monkeypatch.setattr(module,'Store',lambda *_: NS(pool=store.pool,close=lambda:None))
    async def spawn(*args,**kwargs): pytest.fail('native child spawned')
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    dsn=tmp_path/'synthetic.dsn'; dsn.write_text('unused')
    monkeypatch.setenv('DATABASE_URL_FILE',str(dsn)); monkeypatch.setenv('PROBE_FILES','/unused')
    if entry=='collector':
        real=sc.acquire_waiting
        monkeypatch.setattr(sc,'acquire_waiting',lambda pool,lock,kind: real(pool,lock,kind,sleep=stopping_sleep(1,[])))
        with pytest.raises(Stop): asyncio.run(wa.run_config('/unused/synthetic.json'))
    else:
        monkeypatch.setattr(wp,'require_owner_terminal',lambda: None)
        with pytest.raises(RuntimeError,match='session_claimed_elsewhere'): asyncio.run(wp.pair_config('/unused/synthetic.json'))
    row=claims(store)[0]
    assert row['account']==account and row['generation']==1 and row['released_at'] is None


# spec: ANYM-914
@pytest.mark.parametrize('first',['old','new'])
def test_an_old_opener_with_only_the_directory_flock_and_a_new_opener_never_both_open_the_store(seeded,locks,tmp_path,
                                                                                                monkeypatch,first):
    from messaging_service.collector_state import session_lock
    store,*_=seeded; directory,boot=locks
    with store.pool.connection() as c: c.execute("UPDATE accounts SET synthetic=false WHERE id=%s",(ACCOUNT,))
    tc,session=telegram_entry(monkeypatch,tmp_path,directory,boot,lambda *_: NS(pool=store.pool,close=lambda:None))
    if first=='old':  # a pre-claim collector: the per-directory flock only, no canonical lock, no row
        with session_lock(session):
            with pytest.raises(RuntimeError,match='session_already_owned'): asyncio.run(tc.run_config('/unused/x.json'))
        assert claims(store)==[]
    else:  # a new opener holds both locks and the row; an old one meets the directory flock
        with held(directory,boot) as lock, session_lock(session):
            sc.acquire(store.pool,lock,'collector')
            with pytest.raises(RuntimeError,match='session_already_owned'):
                with session_lock(session): pytest.fail('old opener on a held directory')


# spec: ANYM-914
def test_a_restored_database_holds_a_new_opener_while_the_original_client_keeps_its_claim(seeded,locks,tmp_path,
                                                                                          monkeypatch):
    store,*_=seeded; directory,boot=locks
    original=tmp_path/'original-host'; original.mkdir(mode=0o700)
    with store.pool.connection() as c: c.execute("UPDATE accounts SET synthetic=false WHERE id=%s",(ACCOUNT,))
    with held(original,boot) as lock:  # the original environment's client stays connected throughout
        claim=sc.acquire(store.pool,lock,'collector')
        # This database is now the restored copy: quarantine_restore marked every row.
        with store.pool.connection() as c: c.execute('UPDATE session_claims SET restore_hold=true')
        tc,_=telegram_entry(monkeypatch,tmp_path,directory,boot,lambda *_: NS(pool=store.pool,close=lambda:None))
        real=sc.acquire_waiting
        monkeypatch.setattr(sc,'acquire_waiting',lambda pool,lock,kind: real(pool,lock,kind,sleep=stopping_sleep(1,[])))
        with pytest.raises(Stop): asyncio.run(tc.run_config('/unused/synthetic.json'))
        row=claims(store)[0]
        assert row['holder_id']==claim.holder_id and row['generation']==claim.generation and row['restore_hold']
        assert row['released_at'] is None and row['waiting_host_id'] is not None


# spec: ANYM-914
def test_whatsapp_browser_fallback_refuses_while_claims_are_on(tmp_path,monkeypatch):
    from messaging_service import whatsapp_collector as wa
    directory=tmp_path/'session-locks'; directory.mkdir(mode=0o700)
    spawned=[]
    async def spawn(*args,**kwargs): spawned.append(args)
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    with pytest.raises(RuntimeError,match='session_claim_unsupported'):
        wa.refuse_unclaimed_browser({'provider':'webjs','session_lock_directory':str(directory)})
    wa.refuse_unclaimed_browser({'provider':'whatsmeow','session_lock_directory':str(directory)})
    wa.refuse_unclaimed_browser({'provider':'webjs','session_lock_directory':str(tmp_path/'not-mounted')})
    assert spawned==[]


# spec: ANYM-914
def test_an_override_never_hides_the_canonical_lock_mount_from_the_browser_refusal(tmp_path,monkeypatch):
    from messaging_service import whatsapp_collector as wa
    canonical=tmp_path/'run-deedkit-session-locks'; canonical.mkdir(mode=0o700)
    monkeypatch.setattr(sc,'LOCK_DIRECTORY',canonical)  # the overlay's mount is present
    with pytest.raises(RuntimeError,match='session_claim_unsupported'):
        wa.refuse_unclaimed_browser({'provider':'webjs','session_lock_directory':str(tmp_path/'does-not-exist')})


# spec: ANYM-914
def test_native_child_inherits_the_canonical_lock_descriptor(tmp_path,monkeypatch):
    from messaging_service import whatsapp_collector as wa
    seen={}
    class Stopped(Exception): pass
    async def spawn(*args,**kwargs):
        seen.update(kwargs); raise Stopped()
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    events=tmp_path/'events'
    provider=wa.WhatsmeowProvider({'native_executable':'/bin/false','events_directory':str(events),
        'provider':'whatsmeow'},tmp_path/'config.json',lock_fd=11,claim_fd=12)
    with pytest.raises(Stopped): asyncio.run(provider.connect())
    assert seen['env'][sc.CLAIM_FD_ENV]=='12' and seen['pass_fds'][0]==11 and seen['pass_fds'][-1]==12


# spec: ANYM-914
def test_sync_status_shows_the_session_owner_only_to_full_detail(seeded,locks):
    from messaging_service.sync_status import get_sync_status
    store,*_=seeded; directory,boot=locks
    with store.pool.connection() as c:
        assert get_sync_status(c,{'accounts':[ACCOUNT],'chat_ids':None})['sources'][0]['session_owner'] is None
    with held(directory,boot) as lock: claim=sc.acquire(store.pool,lock,'collector'); host=lock.host_id
    with store.pool.connection() as c:
        owner=get_sync_status(c,{'accounts':[ACCOUNT],'chat_ids':None})['sources'][0]['session_owner']
        scoped=get_sync_status(c,{'accounts':[ACCOUNT],'chat_ids':['100']})['sources']
    assert (owner['host_id'],owner['holder_kind'],owner['generation'],owner['released_at'])==(host,'collector',claim.generation,None)
    assert set(owner)=={'host_id','holder_kind','generation','seen_at','released_at','restore_hold','waiting_host_id'}
    assert all('session_owner' not in row for row in scoped)


# spec: ANYM-914
def test_collectors_table_names_every_claim_code():
    table=(Path(__file__).parents[1]/'COLLECTORS.md').read_text()
    for code in (*sc.CODES,'foreign_session_opener','pre_claim_rollback_refused','session_locks_unsafe','session_locks_unmounted'):
        assert f'| `{code}` |' in table, code
