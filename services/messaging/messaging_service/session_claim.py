"""One provider-session owner per account across hosts (ANYM-914).

Order for every opener: the canonical host lock (one host directory mounted into every session container), then the
existing per-directory session locks, then the durable claim row, then the session store. Within one kernel boot the
flock proves a previous owner dead, so the row may pass to the same `host_id`, `boot_id` and lock file at once.
Anything else (another host, a clone, a copied lock directory, a reboot after an unclean stop, a restored database)
needs a clean release or `deedkit admin session-claim release`. Refreshing `seen_at` is visibility only: a database
error never stops a connection; only a positive answer that the row changed is `session_claim_lost`.

The statements are string constants here, never a `.sql` file: a `.sql` path is a hand-applied change to the
release planner (scripts/production.py SQL_INVENTORY). ANYM-898's Go caller uses the same text.
"""
import asyncio,fcntl,logging,os,re,secrets,stat,threading,time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .collector_state import private_session_directory
from .identifiers import ACCOUNT_RE

LOCK_DIRECTORY=Path('/run/deedkit/session-locks')  # inside the /run/deedkit mount root (production.py MOUNT_ROOTS)
BOOT_ID=Path('/proc/sys/kernel/random/boot_id')
CLAIM_FD_ENV='ANYMSG_SESSION_CLAIM_FD'
REFRESH_SECONDS=60
RELEASE_WAIT_S,RELEASE_BOUND_S=2,5  # shutdown release: pool wait and statement each, and the whole call
BACKOFF=(5,300)  # seconds: first wait, ceiling
LOG_EVERY=3600
HEX32=re.compile(r'[0-9a-f]{32}')
KINDS=('collector','pair','holder')
CODES=('session_already_owned','session_claimed_elsewhere','session_claim_restore_hold','session_claim_lost',
       'session_claim_required','session_claim_unsupported','host_id_invalid')
log=logging.getLogger(__name__)

ACQUIRE_SQL='''INSERT INTO session_claims(account,host_id,boot_id,lock_ref,holder_id,holder_kind,generation,claimed_at,seen_at)
SELECT %(account)s,%(host)s,%(boot)s,%(lock)s,%(holder)s,%(kind)s,1,clock_timestamp(),clock_timestamp()
WHERE EXISTS(SELECT 1 FROM session_claims WHERE account=%(account)s)
   OR NOT EXISTS(SELECT 1 FROM source_status WHERE account=%(account)s AND status='restored_disabled')
ON CONFLICT(account) DO UPDATE SET host_id=excluded.host_id,boot_id=excluded.boot_id,lock_ref=excluded.lock_ref,
 holder_id=excluded.holder_id,holder_kind=excluded.holder_kind,generation=session_claims.generation+1,
 claimed_at=excluded.claimed_at,seen_at=excluded.seen_at,released_at=NULL,released_by=NULL,
 waiting_host_id=NULL,waiting_since=NULL
WHERE NOT session_claims.restore_hold AND (session_claims.released_at IS NOT NULL OR (
 session_claims.host_id=excluded.host_id AND session_claims.boot_id=excluded.boot_id
 AND session_claims.lock_ref=excluded.lock_ref))
RETURNING generation'''
REFRESH_SQL='''UPDATE session_claims SET seen_at=clock_timestamp() WHERE account=%s AND generation=%s AND holder_id=%s
 AND released_at IS NULL AND NOT restore_hold'''
RELEASE_SQL='''UPDATE session_claims SET released_at=clock_timestamp(),released_by='holder'
 WHERE account=%s AND generation=%s AND holder_id=%s AND released_at IS NULL'''
WAITING_SQL='''UPDATE session_claims SET waiting_host_id=%s,waiting_since=clock_timestamp()
 WHERE account=%s AND waiting_host_id IS DISTINCT FROM %s'''
ROW_SQL='''SELECT account,host_id,boot_id,lock_ref,holder_id,holder_kind,generation,claimed_at,seen_at,released_at,
 released_by,restore_hold,waiting_host_id,waiting_since FROM session_claims WHERE account=%s'''
RESTORED_SQL="SELECT 1 FROM source_status WHERE account=%s AND status='restored_disabled'"
# Operator release: compare-and-set against the row the operator confirmed; also clears a restore hold.
ADMIN_RELEASE_SQL='''UPDATE session_claims SET released_at=coalesce(released_at,clock_timestamp()),
 released_by=coalesce(released_by,'admin'),restore_hold=false
 WHERE account=%s AND generation=%s AND holder_id=%s AND host_id=%s'''
# A pre-claim backup restored and migrated has no row: the operator release writes a released one.
ADMIN_RESTORE_SQL='''INSERT INTO session_claims(account,host_id,boot_id,lock_ref,holder_id,holder_kind,generation,
 claimed_at,seen_at,released_at,released_by) VALUES(%s,'00000000000000000000000000000000','admin-release','0:0',
 '00000000000000000000000000000000','holder',1,clock_timestamp(),clock_timestamp(),clock_timestamp(),'admin')
 ON CONFLICT(account) DO NOTHING'''


class ClaimRefused(RuntimeError):
    def __init__(self,code,detail=None):
        super().__init__(code); self.code=code; self.detail=detail or {}


@dataclass
class HostLock:
    account:str
    fd:int
    host_id:str
    boot_id:str
    lock_ref:str
    directory:Path


@dataclass(frozen=True)
class Claim:
    account:str
    generation:int
    holder_id:str
    kind:str


def account_name(account):
    # The canonical account form; a leading dot would name `.`, `..` or a hidden file as the lock.
    if not isinstance(account,str) or not ACCOUNT_RE.fullmatch(account) or account.startswith('.'):
        raise RuntimeError('live_account_required')
    return account


def read_boot_id(path=None):
    try: value=Path(path or BOOT_ID).read_text().strip()
    except OSError: raise RuntimeError('host_id_invalid') from None
    if not 1<=len(value)<=64 or not value.isprintable(): raise RuntimeError('host_id_invalid')
    return value


def host_id(directory_fd):
    """The host's stable ID: created once by the first opener, never overwritten."""
    try:
        fd=os.open('host_id',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory_fd)
    except FileNotFoundError:
        temporary=f'.host_id.{os.getpid()}.{secrets.token_hex(4)}'
        fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600,dir_fd=directory_fd)
        try:
            os.write(fd,(secrets.token_hex(16)+'\n').encode()); os.fsync(fd)
        finally: os.close(fd)
        try: os.link(temporary,'host_id',src_dir_fd=directory_fd,dst_dir_fd=directory_fd)
        except FileExistsError: pass  # another opener won; read its ID
        finally: os.unlink(temporary,dir_fd=directory_fd)
        os.fsync(directory_fd)
        fd=os.open('host_id',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory_fd)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077: raise RuntimeError('host_id_invalid')
        value=os.read(fd,64).decode('ascii','replace').strip()
    finally: os.close(fd)
    if not HEX32.fullmatch(value): raise RuntimeError('host_id_invalid')
    return value


@contextmanager
def host_lock(account,directory=None,*,boot_id_path=None):
    """Hold `<lock directory>/<account>.lock` for the whole session lifetime. Never unlink it."""
    account=account_name(account); directory=Path(directory or LOCK_DIRECTORY)
    directory_fd=private_session_directory(directory/'host_id')
    try:
        ident=host_id(directory_fd); boot=read_boot_id(boot_id_path)
        fd=os.open(account+'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=directory_fd)
    finally: os.close(directory_fd)
    lock=None
    try:
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('session_already_owned') from None
        info=os.fstat(fd)
        lock=HostLock(account,fd,ident,boot,f'{info.st_dev}:{info.st_ino}',directory)
        yield lock
    finally:
        if lock is None or lock.fd>=0: os.close(fd)


def descendants_gone(lock):
    """After every child was reaped: True only when a fresh descriptor takes the canonical lock, so no descendant
    still holds the inherited one. The caller's own descriptor is closed first; the fresh one closes on return."""
    if lock.fd>=0: os.close(lock.fd); lock.fd=-1
    directory_fd=private_session_directory(lock.directory/'host_id')
    try: fd=os.open(lock.account+'.lock',os.O_RDWR|os.O_NOFOLLOW,dir_fd=directory_fd)
    except OSError: return False
    finally: os.close(directory_fd)
    try:
        info=os.fstat(fd)
        if f'{info.st_dev}:{info.st_ino}'!=lock.lock_ref: return False
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return False
        return True
    finally: os.close(fd)


def child_environment(lock):
    """The variable that names the inherited canonical lock descriptor (pass it in pass_fds too)."""
    return {CLAIM_FD_ENV:str(lock.fd)}


def row(pool,account):
    with pool.connection() as c: return c.execute(ROW_SQL,(account,)).fetchone()


def acquire(pool,lock,kind):
    if kind not in KINDS: raise ValueError('invalid_claim_kind')
    holder=secrets.token_hex(16)
    with pool.connection() as c:
        got=c.execute(ACQUIRE_SQL,{'account':lock.account,'host':lock.host_id,'boot':lock.boot_id,
            'lock':lock.lock_ref,'holder':holder,'kind':kind}).fetchone()
        if got: return Claim(lock.account,int(got['generation']),holder,kind)
        current=c.execute(ROW_SQL,(lock.account,)).fetchone()
        if current is None or current['restore_hold']:
            raise ClaimRefused('session_claim_restore_hold',{'restore_hold':True})
        raise ClaimRefused('session_claimed_elsewhere',{
            'host_id':current['host_id'],'holder_kind':current['holder_kind'],'generation':current['generation'],
            'seen_at':current['seen_at'].isoformat() if current['seen_at'] else None,
            'same_host_id':current['host_id']==lock.host_id})


def refresh(pool,claim):
    """True while the row is ours; False is the positive answer that it changed. Database errors propagate."""
    with pool.connection() as c:
        return c.execute(REFRESH_SQL,(claim.account,claim.generation,claim.holder_id)).rowcount==1


def release(pool,claim):
    """Call only after verified disconnect. Best effort: a failure keeps the row (the same kernel reacquires).
    Bounded for the shutdown grace: at most RELEASE_WAIT_S for a pool connection and as much for the statement."""
    try:
        with pool.connection(timeout=RELEASE_WAIT_S) as c:
            c.execute(f"SET LOCAL statement_timeout='{int(RELEASE_WAIT_S*1000)}ms'")
            return c.execute(RELEASE_SQL,(claim.account,claim.generation,claim.holder_id)).rowcount==1
    except Exception:
        log.warning('session_claim.release_failed account=%s generation=%s',claim.account,claim.generation)
        return False


async def release_bounded(pool,claim,*,seconds=None):
    """The shutdown's release (criterion 6): right after the verified disconnect and before any other cleanup, never
    longer than RELEASE_BOUND_S (15 s disconnect + this stays inside the 25 s stop grace). A timeout keeps the row.
    The call runs in a daemon thread, not the loop's executor, so a hung database never holds the process exit."""
    loop=asyncio.get_running_loop(); done=loop.create_future()
    def settle(result):
        if not done.done(): done.set_result(result)
    def work():
        result=release(pool,claim)
        try: loop.call_soon_threadsafe(settle,result)
        except RuntimeError: pass  # the loop already ended: the bound had passed
    threading.Thread(target=work,name='session-claim-release',daemon=True).start()
    try: return await asyncio.wait_for(done,seconds or RELEASE_BOUND_S)
    except asyncio.TimeoutError:
        log.warning('session_claim.release_timeout account=%s generation=%s',claim.account,claim.generation)
        return False


def mark_waiting(pool,lock):
    with pool.connection() as c: c.execute(WAITING_SQL,(lock.host_id,lock.account,lock.host_id))


def database_error(exc):
    # Any database fault, a missing table (schema 75) included, and a saturated pool: refuse and retry.
    import psycopg
    from psycopg_pool import PoolTimeout
    return isinstance(exc,(psycopg.Error,PoolTimeout))


async def open_waiting(open_store,check,account,*,sleep=asyncio.sleep,clock=time.monotonic):
    """The collector's store and its account check before any claim or session store (criterion 3): a database outage
    at start waits in-process with the same backoff, never a crash loop; any other error (live_account_required)
    raises at once. A store whose check failed is closed before the next try."""
    delay,logged=BACKOFF[0],None
    while True:
        store=None
        try:
            store=await asyncio.to_thread(open_store)
            await asyncio.to_thread(check,store)
            return store
        except Exception as exc:
            if store is not None: store.close()
            if not database_error(exc): raise
            error=type(exc).__name__
        now=clock()
        if logged is None or now-logged>=LOG_EVERY:
            log.warning('session_claim.store_waiting account=%s error=%s',account,error); logged=now
        await sleep(delay); delay=min(BACKOFF[1],delay*2)


async def acquire_waiting(pool,lock,kind,*,sleep=asyncio.sleep,clock=time.monotonic):
    """Acquire, or wait in-process with bounded backoff, holding the locks and no session store open.

    A refusal or a database error (including a schema-75 database without the table) refuses the same way:
    fail-closed at start, no provider contact, no process exit."""
    delay,logged,marked=BACKOFF[0],None,False
    while True:
        try: return await asyncio.to_thread(acquire,pool,lock,kind)
        except ClaimRefused as refused:
            code,detail=refused.code,refused.detail
            if not marked:
                try: await asyncio.to_thread(mark_waiting,pool,lock); marked=True
                except Exception as exc:
                    if not database_error(exc): raise
        except Exception as exc:
            if not database_error(exc): raise
            code,detail='session_claim_unavailable',{}
        now=clock()
        if logged is None or now-logged>=LOG_EVERY:
            log.warning('session_claim.waiting account=%s code=%s detail=%s',lock.account,code,detail); logged=now
        await sleep(delay); delay=min(BACKOFF[1],delay*2)


async def keep(pool,claim,*,interval=REFRESH_SECONDS,sleep=asyncio.sleep,clock=time.monotonic):
    """Refresh until the row is positively someone else's, then raise session_claim_lost. Nothing else escapes."""
    logged=None
    while True:
        await sleep(interval)
        try: ours=await asyncio.to_thread(refresh,pool,claim)
        except asyncio.CancelledError: raise
        except Exception as exc:
            now=clock()
            if logged is None or now-logged>=LOG_EVERY:
                log.warning('session_claim.refresh_failed account=%s error=%s',claim.account,type(exc).__name__)
                logged=now
            continue
        if not ours:
            log.warning('session_claim.lost account=%s generation=%s',claim.account,claim.generation)
            raise RuntimeError('session_claim_lost')
