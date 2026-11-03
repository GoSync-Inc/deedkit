"""Private, bounded SQLite text state. No provider or messaging dependencies."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time
import uuid

from .security import Failure, canonical, loads

MAX_TEXT = 2 * 1024 ** 2
HEADROOM = 64 * 1024 ** 2
QUEUE_LIMIT = 10000
HEAD_SLICE = 100
FAR = 1e18  # `due` of a record that waits for a changed list fingerprint instead of a clock
WINDOW = 3600
SLACK = 75  # dispatch drift inside one pass (60 s) plus a provider timeout: never reuse a call that may still count
APPLICATION_ID = 0x444B5243
SCHEMA_VERSION = 3
SCHEMA = '''
BEGIN IMMEDIATE;
CREATE TABLE accounts(namespace TEXT PRIMARY KEY, checkpoint TEXT, last_success REAL, error TEXT, work_turn INTEGER NOT NULL DEFAULT 0,
 throttled_until REAL, traversal_state TEXT NOT NULL DEFAULT 'not_started', passes_completed INTEGER NOT NULL DEFAULT 0,
 last_page_at REAL, started_at REAL, last_head_at REAL, head_cursor TEXT, head_backlog_since REAL, credential TEXT,
 head_floor INTEGER, text_bytes INTEGER NOT NULL DEFAULT 0);
CREATE TABLE records(
 ref TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider_id TEXT NOT NULL,
 metadata TEXT NOT NULL, parts TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 attempted REAL, due REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, fingerprint TEXT,
 UNIQUE(namespace, provider_id));
CREATE INDEX records_due ON records(namespace, due, attempted);
CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 namespace TEXT NOT NULL, ref TEXT NOT NULL, revision INTEGER NOT NULL, occurred REAL NOT NULL);
CREATE VIRTUAL TABLE texts USING fts5(ref UNINDEXED, namespace UNINDEXED, title, transcript, summary, tokenize='unicode61');
CREATE TABLE ledger(credential TEXT NOT NULL, bucket TEXT NOT NULL, window_start REAL NOT NULL, count INTEGER NOT NULL,
 updated REAL NOT NULL, PRIMARY KEY(credential, bucket, window_start));
CREATE TABLE meta(key TEXT PRIMARY KEY, value);  -- key/value rows: the backup tooling reads archive_id and incarnation (ANYM-714)
INSERT INTO meta(key,value) VALUES('archive_id','{archive}'),('incarnation',1),('journal_id','{journal}'),('text_reconciled',1);
PRAGMA application_id=1145786947;
PRAGMA user_version=3;
COMMIT;
'''
UPGRADE_INDEX = '''
BEGIN IMMEDIATE;
DROP TABLE texts;
CREATE VIRTUAL TABLE texts USING fts5(ref UNINDEXED, namespace UNINDEXED, title, transcript, summary, tokenize='unicode61');
INSERT INTO texts(ref,namespace,title,transcript,summary)
 SELECT ref,namespace,COALESCE(NULLIF(json_extract(metadata,'$.title'),''),json_extract(metadata,'$.name'),''),
 COALESCE(json_extract(parts,'$.transcript.text'),''),COALESCE(json_extract(parts,'$.summary.text'),'') FROM records;
PRAGMA user_version=2;
COMMIT;
'''
# Schema 3 is additive: new tables and nullable/defaulted columns only. No index rebuild, no row rewritten.
UPGRADE_V3 = '''
BEGIN IMMEDIATE;
{alters}CREATE TABLE IF NOT EXISTS ledger(credential TEXT NOT NULL, bucket TEXT NOT NULL, window_start REAL NOT NULL,
 count INTEGER NOT NULL, updated REAL NOT NULL, PRIMARY KEY(credential, bucket, window_start));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value);
INSERT OR IGNORE INTO meta(key,value) VALUES('archive_id','{archive}'),('incarnation',1),('journal_id','{journal}');
PRAGMA user_version=3;
COMMIT;
'''
V3_COLUMNS = (
    ('accounts', 'throttled_until', 'REAL'),
    ('accounts', 'traversal_state', "TEXT NOT NULL DEFAULT 'not_started'"),
    ('accounts', 'passes_completed', 'INTEGER NOT NULL DEFAULT 0'),
    ('accounts', 'last_page_at', 'REAL'),
    ('accounts', 'started_at', 'REAL'),
    ('accounts', 'last_head_at', 'REAL'),
    ('accounts', 'head_cursor', 'TEXT'),
    ('accounts', 'head_backlog_since', 'REAL'),
    ('accounts', 'credential', 'TEXT'),
    ('accounts', 'head_floor', 'INTEGER'),
    ('accounts', 'text_bytes', 'INTEGER NOT NULL DEFAULT 0'),
    ('records', 'fingerprint', 'TEXT'),
)


def upgrade_v3(db):
    """Apply schema 3 in one transaction: add only the columns still missing, then the new tables."""
    alters = ''
    for table, column, declaration in V3_COLUMNS:
        if column not in {row[1] for row in db.execute(f'PRAGMA table_info({table})')}:
            alters += f'ALTER TABLE {table} ADD COLUMN {column} {declaration};\n'
    db.executescript(with_ids(UPGRADE_V3, alters=alters))


def with_ids(template, **extra):
    return template.format(archive=uuid.uuid4().hex, journal=uuid.uuid4().hex, **extra)


def provider_of(account):
    return account['provider'] if isinstance(account, dict) else account.provider


def pending_gap(metadata, now):
    """Seconds until a record with a pending part may be read again: 3 h in the first day, daily to 14 days, then never."""
    created = metadata.get('created_at')
    try:
        age = now - datetime.fromisoformat(created.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError, TypeError, OverflowError):
        age = 0
    if age < 86400:
        return 3 * 3600
    return 86400 if age < 14 * 86400 else None


def fixed_bytes():
    """Measured size of the empty schema, the fixed part of the capacity projection."""
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript(with_ids(SCHEMA))
        return db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0]


def title_of(metadata):
    """The recording title both providers retain: Omi `title`, PLAUD `name`. Text only, never a non-string value."""
    for key in ('title', 'name'):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return ''


def namespace(account):
    if isinstance(account, dict):
        identity = [account['provider'], account['id'], account.get('archive_namespace')]
    else:
        identity = [account.provider, account.id, account.archive_namespace]
    return hashlib.sha256(canonical(identity)).hexdigest()


def private_directory(value, *, create=False):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path:
        raise Failure('archive_unavailable')
    if create:
        private_directory(str(path.parent))
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise Failure('archive_unavailable')
    return path


def check_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise Failure('archive_unavailable')


def backup_archive(settings, destination):
    """Online Backup against a running writer; no provider auth or writer lock."""
    store = Store.__new__(Store)
    store.settings, store.lock = settings, None
    store.directory = private_directory(settings.directory)
    store.path = store.directory / 'archive.sqlite'
    try:
        with store.connection() as db:
            if db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or db.execute(
                    'PRAGMA user_version').fetchone()[0] not in (1, 2, 3):
                raise Failure('archive_schema_unsupported')
        return store.run('backup', destination)
    except (OSError, sqlite3.Error, ValueError):
        raise Failure('archive_unavailable') from None


class Store:
    def __init__(self, settings):
        self.settings, self.lock = settings, None
        try:
            self.directory = private_directory(settings.directory, create=True)
            self.path = self.directory / 'archive.sqlite'
            lock_path = self.directory / '.lock'
            self.lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            check_file(lock_path)
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not self.path.exists():
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            self.capacity()
            with self.connection(readonly=False) as db:
                empty = self.path.stat().st_size == 0
                if empty:
                    db.executescript(with_ids(SCHEMA))
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or version not in (1, 2, 3):
                    raise Failure('archive_schema_unsupported')
                if version == 1:
                    # Only this derived index is rebuilt. Accepted records, refs,
                    # revisions and journal survive atomically; interruption rolls back.
                    db.executescript(UPGRADE_INDEX)
                    version = 2
                if version == 2:
                    # Additive only; any failure rolls the whole script back to a usable schema 2.
                    upgrade_v3(db)
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise Failure('archive_unavailable')
                db.execute('PRAGMA journal_mode=WAL')
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def files(self):
        private_directory(str(self.directory))
        result = []
        for suffix in ('', '-wal', '-shm', '-journal'):
            path = Path(str(self.path) + suffix)
            if path.exists() or path.is_symlink():
                check_file(path)
                result.append(path)
        return result

    def capacity(self):
        # Reserve a whole bounded text/index transaction, in addition to filesystem
        # reserve. The page ceiling leaves room for a full WAL alongside the DB.
        used = sum(path.stat().st_size for path in self.files())
        if used + HEADROOM > self.settings.max_bytes or shutil.disk_usage(self.directory).free < (
                self.settings.free_reserve_bytes + HEADROOM):
            raise Failure('archive_capacity')

    @contextmanager
    def connection(self, *, readonly=True):
        self.files()
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=0.1) if readonly else sqlite3.connect(self.path, timeout=0.1)
        try:
            db.row_factory = sqlite3.Row
            until = time.monotonic() + 2
            db.set_progress_handler(lambda: int(time.monotonic() > until), 1000)
            db.execute('PRAGMA temp_store=MEMORY')
            db.execute('PRAGMA trusted_schema=OFF')
            if not readonly:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('PRAGMA wal_autocheckpoint=1')
                size = db.execute('PRAGMA page_size').fetchone()[0]
                db.execute(f'PRAGMA max_page_count={self.settings.max_bytes // (3 * size)}')
            yield db
        finally:
            db.close()

    def run(self, name, *args):
        try:
            return getattr(self, name)(*args)
        except sqlite3.Error as error:
            full = getattr(error, 'sqlite_errorcode', 0) & 255 == sqlite3.SQLITE_FULL
            if full or str(error) == 'database or disk is full':
                raise Failure('archive_capacity') from None
            raise Failure('archive_unavailable') from None
        except (OSError, ValueError, TypeError, KeyError):
            raise Failure('archive_unavailable') from None

    def write(self, change, guard=lambda: None):
        self.capacity()
        with self.connection(readonly=False) as db:
            try:
                db.execute('BEGIN IMMEDIATE')
                result = change(db)
                guard()  # Re-read account/namespace/token authorization immediately before commit.
                db.commit()
            except BaseException:
                db.rollback()
                raise
            # A short reader may postpone this. Admission still counts the retained WAL.
            try:
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            except sqlite3.Error:
                pass
            return result

    def checkpoint(self, account):
        with self.connection() as db:
            row = db.execute('SELECT checkpoint FROM accounts WHERE namespace=?', (namespace(account),)).fetchone()
            return loads(row[0]) if row and row[0] is not None else None

    def queue_cap(self):
        return getattr(self.settings, 'max_queued', None) or QUEUE_LIMIT

    def head_slice(self):
        return min(HEAD_SLICE, self.queue_cap() // 2)

    def plan(self, account, credential, now):
        """Everything one collector pass needs to choose its provider calls, in one short write."""
        ns = namespace(account)
        def change(db):
            db.execute('INSERT OR IGNORE INTO accounts(namespace) VALUES(?)', (ns,))
            db.execute('UPDATE accounts SET credential=?,started_at=COALESCE(started_at,?) WHERE namespace=?',
                       (credential, now, ns))
            if db.execute("SELECT 1 FROM meta WHERE key='text_reconciled'").fetchone() is None:
                # An archive upgraded from schema 2: count the text it already holds once (account counters only; no
                # record is rewritten), so the capacity projection sees accepted text saved before the upgrade.
                db.execute("UPDATE accounts SET text_bytes=(SELECT COALESCE(SUM(length(CAST(json_extract(parts,'$.transcript.text') AS BLOB))"
                           "+length(CAST(json_extract(parts,'$.summary.text') AS BLOB))),0) FROM records "
                           "WHERE records.namespace=accounts.namespace)")
                db.execute("INSERT INTO meta(key,value) VALUES('text_reconciled',1)")
            row = db.execute('SELECT * FROM accounts WHERE namespace=?', (ns,)).fetchone()
            queued = db.execute('SELECT count(*) FROM records WHERE namespace=? AND attempted IS NULL', (ns,)).fetchone()[0]
            checkpoint = loads(row['checkpoint']) if row['checkpoint'] is not None else None
            state = row['traversal_state']
            if state == 'not_started' and checkpoint is not None:
                state = 'in_progress'  # an archive written before schema 3 resumes its stored cursor
            return {'state': state, 'checkpoint': checkpoint, 'throttled_until': row['throttled_until'],
                    'last_head_at': row['last_head_at'], 'last_page_at': row['last_page_at'], 'head_cursor': row['head_cursor'],
                    'queued': queued,
                    'cap': self.queue_cap(), 'slice': self.head_slice()}
        return self.write(change)

    def discover(self, account, items, checkpoint, now, guard=lambda: None, kind='history', fingerprints=None,
                 complete=True, page_cursor=None):
        """Commit one listed page with its traversal position. kind: `first` (opening page, also the head),
        `history` (cursor pages; `first` and `history` stay behind the head slice), `head` (newest pages, may resume),
        `legacy` (PLAUD). `complete` False: a truncated or unproven page, re-read later and never taken as the end;
        `page_cursor` is the cursor this page was read with."""
        ns = namespace(account)
        fingerprints = fingerprints or {}
        def change(db):
            cap = self.queue_cap()
            room = cap if kind in ('head', 'legacy') else cap - self.head_slice()
            queued = db.execute('SELECT count(*) FROM records WHERE namespace=? AND attempted IS NULL', (ns,)).fetchone()[0]
            floor_now = db.execute('SELECT COALESCE(max(rowid),0) FROM records').fetchone()[0]
            unique = {item['id']: item for item in items}
            known = {}
            for ident in unique:
                row = db.execute('SELECT rowid AS rid,* FROM records WHERE namespace=? AND provider_id=?', (ns, ident)).fetchone()
                if row is not None:
                    known[ident] = row
            new = [item for ident, item in unique.items() if ident not in known]
            if queued + len(new) > room:
                raise Failure('archive_queue_full')
            db.execute('INSERT OR IGNORE INTO accounts(namespace) VALUES(?)', (ns,))
            for item in new:
                ref = uuid.uuid4().hex
                db.execute('INSERT INTO records(ref,namespace,provider_id,metadata,parts,fingerprint) VALUES(?,?,?,?,?,?)',
                    (ref, ns, item['id'], canonical(item).decode(), '{}', fingerprints.get(item['id'])))
                db.execute('INSERT INTO texts(ref,namespace,title,transcript,summary) VALUES(?,?,?,?,?)',
                           (ref, ns, title_of(item), '', ''))
            if kind != 'legacy':
                for ident, row in known.items():
                    self.refresh(db, ns, row, unique[ident], fingerprints.get(ident), now)
            if kind == 'legacy':
                db.execute('UPDATE accounts SET checkpoint=?,error=NULL WHERE namespace=?', (canonical(checkpoint).decode(), ns))
            elif kind == 'head':
                acc = db.execute('SELECT head_cursor,head_floor,head_backlog_since FROM accounts WHERE namespace=?', (ns,)).fetchone()
                # "Known" means known before this catch-up began: IDs inserted by an earlier (truncated) read of
                # the same traversal must not end it while unseen IDs wait behind another cursor.
                floor = acc['head_floor'] if acc['head_cursor'] is not None and acc['head_floor'] is not None else floor_now
                seen_before = any(row['rid'] <= floor for row in known.values())
                since = acc['head_backlog_since'] if acc['head_backlog_since'] is not None else now
                if not complete:
                    db.execute('UPDATE accounts SET head_cursor=?,head_floor=?,head_backlog_since=?,error=NULL WHERE namespace=?',
                               (page_cursor or '', floor, since, ns))
                else:
                    resume = checkpoint is not None and not seen_before
                    db.execute('UPDATE accounts SET head_cursor=?,head_floor=?,head_backlog_since=?,last_head_at=?,error=NULL '
                               'WHERE namespace=?', (checkpoint if resume else None, floor if resume else None,
                                                     since if resume else None, now, ns))
            elif complete:
                db.execute('UPDATE accounts SET traversal_state=?,checkpoint=?,last_page_at=?,passes_completed=passes_completed+1,'
                           'last_head_at=CASE WHEN ?=\'first\' THEN ? ELSE last_head_at END,error=NULL WHERE namespace=?',
                    ('in_progress' if checkpoint is not None else 'reached_end',
                     canonical(checkpoint).decode() if checkpoint is not None else None, now, kind, now, ns))
            else:
                db.execute('UPDATE accounts SET error=NULL WHERE namespace=?', (ns,))
            return {'inserted': len(new), 'known': len(known)}
        return self.write(change, guard)

    @staticmethod
    def refresh(db, ns, row, item, fingerprint, now):
        """A listed row of a known record: a changed fingerprint makes a saved record due; a changed title updates the
        stored title and index in place (no provider call, no text-change event)."""
        if fingerprint is not None and row['fingerprint'] != fingerprint:
            # A record saved before schema 3 has no fingerprint yet: remember it without a re-read.
            if row['fingerprint'] is not None and row['attempted'] is not None and not row['failures']:
                db.execute('UPDATE records SET fingerprint=?,due=MIN(due,?) WHERE ref=?', (fingerprint, now, row['ref']))
            else:
                db.execute('UPDATE records SET fingerprint=? WHERE ref=?', (fingerprint, row['ref']))
        title = item.get('title')
        stored = loads(row['metadata'])
        if 'title' in item and stored.get('title') != title:
            stored['title'] = title
            stored.pop('title_truncated', None)
            if item.get('title_truncated'):
                stored['title_truncated'] = True
            parts = loads(row['parts'])
            db.execute('UPDATE records SET metadata=?,revision=revision+?  WHERE ref=?',
                       (canonical(stored).decode(), int(row['revision'] > 0), row['ref']))
            db.execute('DELETE FROM texts WHERE ref=?', (row['ref'],))
            db.execute('INSERT INTO texts(ref,namespace,title,transcript,summary) VALUES(?,?,?,?,?)',
                (row['ref'], ns, title_of(stored), parts.get('transcript', {}).get('text', ''), parts.get('summary', {}).get('text', '')))

    def rotate(self, account):
        ns = namespace(account)
        self.write(lambda db: db.execute('UPDATE accounts SET work_turn=(work_turn+1)%3 WHERE namespace=?', (ns,)))

    def head_blocked(self, account, now):
        ns = namespace(account)
        self.write(lambda db: db.execute('UPDATE accounts SET head_backlog_since=COALESCE(head_backlog_since,?) WHERE namespace=?', (now, ns)))

    def throttle(self, credential, namespaces, until):
        """A 429: no collector call for this credential before `until` (checked inside every reservation)."""
        def change(db):
            db.execute("DELETE FROM ledger WHERE credential=? AND bucket='throttle'", (credential,))
            db.execute("INSERT INTO ledger(credential,bucket,window_start,count,updated) VALUES(?,'throttle',?,0,?)",
                       (credential, until, until))
            for ns in namespaces:
                db.execute('INSERT INTO accounts(namespace,throttled_until) VALUES(?,?) ON CONFLICT(namespace) '
                           'DO UPDATE SET throttled_until=excluded.throttled_until', (ns, until))
        self.write(change)

    def ledger_change(self, credential, specs, now, strict):
        """Count one use of each bucket in a single transaction. The window is the last hour (plus a drift margin)
        before `now`, not a fixed window of ours: Omi's window starts at its first counted request, which a crash
        or a failed connection can place after our own reservation, and a sliding hour is sound for every such start.
        With limits (strict) the set is granted or refused whole; without (foreground) it is only recorded.
        Any doubt about the rows is `ledger_uncertain`."""
        def change(db):
            used, latest, held = {}, None, None
            for bucket, start, count in db.execute('SELECT bucket,window_start,count FROM ledger WHERE credential=?',
                                                   (credential,)).fetchall():
                if type(start) not in (int, float) or type(count) is not int or count < 0:
                    raise Failure('ledger_uncertain')
                if bucket == 'throttle':
                    held = start if held is None else max(held, start)
                    continue
                latest = start if latest is None else max(latest, start)
                if start > now - WINDOW - SLACK:
                    used[bucket] = used.get(bucket, 0) + count
            if strict:
                if latest is not None and latest > now + 60:
                    raise Failure('ledger_uncertain')  # the clock went backwards past a recorded use
                if held is not None and held > now:
                    return False  # a 429 pause is in force for this credential
            for bucket, limit in specs:
                if limit is not None and used.get(bucket, 0) + 1 > limit:
                    return False
            for bucket, _ in specs:
                db.execute('INSERT INTO ledger(credential,bucket,window_start,count,updated) VALUES(?,?,?,1,?) '
                           'ON CONFLICT(credential,bucket,window_start) DO UPDATE SET count=count+1,updated=excluded.updated',
                           (credential, bucket, now, now))
            db.execute("DELETE FROM ledger WHERE credential=? AND bucket<>'throttle' AND window_start<=?",
                       (credential, now - 2 * WINDOW))
            return True
        for attempt in range(20):
            try:
                return self.write(change)
            except sqlite3.OperationalError as error:
                if 'locked' in str(error) and attempt < 19:
                    time.sleep(0.05)
                    continue
                if getattr(error, 'sqlite_errorcode', 0) & 255 == sqlite3.SQLITE_FULL or str(error) == 'database or disk is full':
                    raise
                raise Failure('ledger_uncertain') from None
            except sqlite3.Error as error:
                if getattr(error, 'sqlite_errorcode', 0) & 255 == sqlite3.SQLITE_FULL:
                    raise
                raise Failure('ledger_uncertain') from None

    def reserve(self, credential, specs, now):
        return self.ledger_change(credential, specs, now, True)

    def record_use(self, credential, buckets, now):
        return self.ledger_change(credential, [(bucket, None) for bucket in buckets], now, False)

    def work(self, account, now, limit=8, guard=lambda: None):
        return [ident for ident, _ in self.work_items(account, now, limit, guard)]

    def work_items(self, account, now, limit=8, guard=lambda: None, rotate=True):
        """Provider ids to read now with a `fresh` flag. Omi: never-fetched records newest first (last 24 h ahead of the
        backlog), then failed records by backoff, then records waiting on a pending part or a changed list row.
        PLAUD keeps its earlier due-time order and hourly revisit."""
        ns = namespace(account)
        omi = provider_of(account) == 'omi'
        cutoff = datetime.fromtimestamp(now - 86400, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        def select(db):
            db.execute('INSERT OR IGNORE INTO accounts(namespace) VALUES(?)',(ns,))
            turn = db.execute('SELECT work_turn FROM accounts WHERE namespace=?',(ns,)).fetchone()[0]
            groups = []
            for condition, budget in (('attempted IS NULL', 3),
                                      ('attempted IS NOT NULL AND failures>0', 3),
                                      ('attempted IS NOT NULL AND failures=0', 2)):
                if omi and condition == 'attempted IS NULL':
                    created = "COALESCE(substr(json_extract(metadata,'$.created_at'),1,19),'')"
                    rows = db.execute(f'SELECT provider_id,{created}>=? AS fresh FROM records WHERE namespace=? AND due<=? AND '
                        + condition + f' ORDER BY (CASE WHEN {created}>=? THEN 0 ELSE 1 END),{created} DESC,rowid LIMIT ?',
                        (cutoff, ns, now, cutoff, budget)).fetchall()
                    groups.append([(row[0], bool(row[1])) for row in rows])
                else:
                    groups.append([(row[0], False) for row in db.execute('SELECT provider_id FROM records WHERE namespace=? AND due<=? AND ' + condition +
                        ' ORDER BY due,ref LIMIT ?', (ns, now, budget))])
            # Persist priority before execution: even a pass interrupted after
            # its first slow record serves another class on its next attempt. A caller that may be denied
            # its budget rotates only once a call is actually dispatched (`rotate`).
            if rotate:
                db.execute('UPDATE accounts SET work_turn=? WHERE namespace=?',((turn+1)%3,ns))
            result = []
            for index in range(3):
                for offset in range(3):
                    group = groups[(turn+offset)%3]
                    if index < len(group):
                        result.append(group[index])
            return result[:limit]
        return self.write(select,guard)

    @staticmethod
    def decoded(row):
        return {'ref': row['ref'], 'provider_id': row['provider_id'], 'metadata': loads(row['metadata']),
                'parts': loads(row['parts']), 'revision': row['revision'], 'attempted': row['attempted']}

    def record(self, account, provider_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM records WHERE namespace=? AND provider_id=?',
                             (namespace(account), provider_id)).fetchone()
            return self.decoded(row) if row else None

    def observe(self, account, provider_id, metadata, observations, now, guard=lambda: None, consistency=None):
        ns = namespace(account)
        def change(db):
            row = db.execute('SELECT * FROM records WHERE namespace=? AND provider_id=?', (ns, provider_id)).fetchone()
            if row is None:
                raise Failure('archive_unavailable')
            parts, usable_change = loads(row['parts']), False
            text_before = sum(len(p.get('text', '').encode()) for p in parts.values())
            failed = False
            for kind, observation in observations.items():
                state, text, error = observation
                part = parts.get(kind, {'state':'unavailable', 'text':'', 'digest':None, 'last_success':None})
                if state == 'available' and text:
                    digest = hashlib.sha256(text.encode()).hexdigest()
                    usable_change |= part['digest'] != digest
                    part.update(state='available', text=text, digest=digest, last_success=now)
                    if consistency and kind in consistency:
                        part['consistency'] = consistency[kind]
                part.update(attempt_state=state, attempt_error=error, attempted_at=now)
                parts[kind] = part
            states = [part.get('attempt_state') for part in parts.values()]
            failed = any(state in ('pending','unavailable','error') for state in states)
            text_after = sum(len(p['text'].encode()) for p in parts.values())
            if text_after > MAX_TEXT:
                raise Failure('archive_content_limit')
            body = canonical(metadata).decode()
            revision = row['revision'] + int(usable_change or (row['revision'] > 0 and body != row['metadata']))
            if provider_of(account) == 'omi':
                # Only a failed read backs off; a part that is still pending waits on a bounded schedule, and a
                # complete record waits for a changed list row. Nothing is re-read hourly.
                failures = min(row['failures'] + 1, 10) if 'error' in states else 0
                gap = pending_gap(metadata, now) if failures == 0 and failed else None
                due = (now + min(3600, 60 * 2 ** (failures - 1)) if failures else
                       now + gap if gap else FAR)
            else:
                failures = min(row['failures'] + 1, 10) if failed else 0
                due = now + min(3600, 60 * 2 ** (failures - 1)) if failed else now + 3600
            db.execute('UPDATE records SET metadata=?,parts=?,revision=?,attempted=?,due=?,failures=? WHERE ref=?',
                (body, canonical(parts).decode(), revision, now, due, failures, row['ref']))
            if usable_change or body != row['metadata']:
                db.execute('DELETE FROM texts WHERE ref=?', (row['ref'],))
                db.execute('INSERT INTO texts(ref,namespace,title,transcript,summary) VALUES(?,?,?,?,?)',
                    (row['ref'], ns, title_of(metadata), parts.get('transcript', {}).get('text', ''), parts.get('summary', {}).get('text', '')))
            if usable_change:
                db.execute('INSERT INTO events(event_id,namespace,ref,revision,occurred) VALUES(?,?,?,?,?)',
                           (uuid.uuid4().hex, ns, row['ref'], revision, now))
            db.execute('UPDATE accounts SET last_success=COALESCE(?,last_success),error=?,text_bytes=MAX(0,text_bytes+?) '
                       'WHERE namespace=?',
                (now if any(o[0] == 'available' for o in observations.values()) else None,
                 next((o[2] for o in observations.values() if o[2]), None), text_after - text_before, ns))
        self.write(change, guard)

    def account_error(self, account, code):
        self.write(lambda db: db.execute('INSERT INTO accounts(namespace,error) VALUES(?,?) ON CONFLICT(namespace) DO UPDATE SET error=excluded.error',
                                        (namespace(account), code)))

    def status(self, account, now=None, budget=None):
        """Counts and last success; with `budget` (Omi) also the backfill progress, bucket usage and capacity.
        Never text, titles or tokens."""
        ns = namespace(account)
        now = time.time() if now is None else now
        with self.connection() as db:
            row = db.execute('SELECT * FROM accounts WHERE namespace=?', (ns,)).fetchone()
            counts = db.execute('SELECT count(*),sum(attempted IS NULL),sum(failures>0),sum(revision>0),'
                                'sum(attempted IS NOT NULL AND failures=0 AND due<?) FROM records WHERE namespace=?',
                                (FAR / 10, ns)).fetchone()
            result = {'coverage':'unknown', 'record_count':counts[0], 'queued_count':counts[1] or 0,
                      'retry_count':counts[2] or 0, 'last_success':row['last_success'] if row else None,
                      'error':row['error'] if row else None}
            if budget is None:
                return result
            total, queued, failed, with_text, pending = counts[0], counts[1] or 0, counts[2] or 0, counts[3] or 0, counts[4] or 0
            state = row['traversal_state'] if row else 'not_started'
            if state == 'not_started' and row and row['checkpoint'] is not None:
                state = 'in_progress'
            usage, uncertain = {}, False
            try:
                live = {'reads': [], 'transcripts': []}
                for bucket, start, count in db.execute('SELECT bucket,window_start,count FROM ledger WHERE credential=?',
                                                       (row['credential'] if row else None,)).fetchall():
                    if type(start) not in (int, float) or type(count) is not int or count < 0:
                        raise ValueError
                    if bucket in live and start > now - WINDOW - SLACK:
                        live[bucket].append((start, count))
                for bucket, limit in (('reads', budget.reads_per_hour), ('transcripts', budget.transcript_per_hour)):
                    used = sum(count for _, count in live[bucket])
                    # resets_at: when the oldest counted call leaves the sliding window and a slot frees.
                    usage[bucket] = {'used': used, 'limit': limit,
                                     'resets_at': min(start for start, _ in live[bucket]) + WINDOW + SLACK if used else None}
            except (sqlite3.Error, ValueError):
                usage, uncertain = None, True
            size = db.execute('PRAGMA page_size').fetchone()[0]
            pages = db.execute('PRAGMA page_count').fetchone()[0] * size
            fixed = fixed_bytes()
            sample = db.execute('SELECT avg(length(metadata)) FROM (SELECT metadata FROM records WHERE namespace=? AND revision=0 LIMIT 200)',
                                (ns,)).fetchone()[0]
            projected = None
            if with_text:
                # Measured, not promised. Two estimates of what one text-bearing record costs in pages; the larger wins,
                # so a large metadata backlog cannot shrink the projection to the empty schema: the occupied pages minus
                # an estimate of the queued rows, and 3 x the accepted text bytes per record (kept in the record, the
                # index content and the index) plus 1 KiB of row overhead.
                by_pages = max(pages - fixed - 4 * (sample or 200) * (total - with_text), 0) / with_text
                by_text = 3 * ((row['text_bytes'] if row else 0) / with_text) + 1024
                projected = int(fixed + max(by_pages, by_text) * total)
            started = row['started_at'] if row else None
            fetched = total - queued
            eta = None
            if started is not None and now - started >= 3600 and fetched > 0 and queued > 0:
                eta = int(queued / (fetched / (now - started)))
            since = row['head_backlog_since'] if row else None
            throttled = row['throttled_until'] if row else None
            result.update(
                traversal={'state': state, 'discovered_total': total, 'passes_completed': row['passes_completed'] if row else 0,
                           'last_page_at': row['last_page_at'] if row else None},
                with_text=with_text, queued=queued, pending=pending, failed=failed,
                throttled_until=throttled if throttled is not None and throttled > now else None,
                usage=usage, db_bytes=sum(path.stat().st_size for path in self.files()),
                bytes_ceiling=self.settings.max_bytes, projected_db_bytes=projected,
                capacity_warning=projected is not None and projected > self.settings.max_bytes / 3, eta=eta,
                head_backlog=since is not None, head_slice_full=since is not None and now - since > 3600)
            if uncertain:
                result['error'] = 'ledger_uncertain'
            return result

    def search(self, account, expression, limit):
        ns = namespace(account)
        with self.connection() as db:
            # FTS MATCH narrows by indexed terms; both sides of the join retain
            # the current namespace. Never materialize accepted text for all hits.
            # The order must not depend on other namespaces: bm25() uses statistics of the whole FTS table, so a document
            # of a forbidden account could change which permitted hit comes first. The rank here is a title hit, then a
            # summary hit, then a transcript hit, then the newer revision, then the reference: each fact is a property of the
            # row itself (MATCH membership), never a corpus statistic.
            rows = db.execute('''SELECT r.ref,r.provider_id,r.revision,
                substr(COALESCE(NULLIF(json_extract(r.metadata,'$.title'),''),json_extract(r.metadata,'$.name'),''),1,256) AS title,
                json_remove(r.parts,'$.transcript.text','$.summary.text') AS evidence,
                substr(snippet(texts,-1,'[',']','…',32),1,320) AS snippet
                FROM texts JOIN records r ON r.ref=texts.ref
                WHERE texts MATCH ? AND texts.namespace=? AND r.namespace=?
                ORDER BY CASE WHEN r.ref IN (SELECT ref FROM texts WHERE texts MATCH ? AND namespace=?) THEN 0
                              WHEN r.ref IN (SELECT ref FROM texts WHERE texts MATCH ? AND namespace=?) THEN 1 ELSE 2 END,
                         r.revision DESC, r.ref LIMIT ?''',
                (expression, ns, ns, f'title : ({expression})', ns, f'summary : ({expression})', ns, limit))
            return [{'recording_ref': row['ref'], 'provider_recording_id': row['provider_id'],
                     'revision': row['revision'], 'title': row['title'], 'snippet': row['snippet'],
                     'parts': loads(row['evidence'])} for row in rows]

    def context(self, account, ref):
        with self.connection() as db:
            row = db.execute('SELECT * FROM records WHERE namespace=? AND ref=?', (namespace(account), ref)).fetchone()
            if row is None:
                raise Failure('not_found_or_forbidden', 403)
            return self.decoded(row)

    def events(self, after, limit):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM events WHERE sequence>? ORDER BY sequence LIMIT ?', (after, limit))]

    def backup(self, destination):
        destination = Path(destination)
        private_directory(str(destination.parent))
        if destination.is_relative_to(self.directory):
            raise Failure('archive_unavailable')
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        try:
            with self.connection() as source, sqlite3.connect(destination) as target:
                until = time.monotonic() + 5
                def progress(*_):
                    if time.monotonic() > until:
                        raise Failure('archive_unavailable')
                source.backup(target, pages=64, progress=progress, sleep=0.01)
        except BaseException:
            destination.unlink()
            raise
        return {'state':'saved'}
