"""Private, bounded SQLite text state. No provider or messaging dependencies."""
from contextlib import contextmanager
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
APPLICATION_ID = 0x444B5243
SCHEMA = '''
BEGIN IMMEDIATE;
CREATE TABLE accounts(namespace TEXT PRIMARY KEY, checkpoint TEXT, last_success REAL, error TEXT, work_turn INTEGER NOT NULL DEFAULT 0);
CREATE TABLE records(
 ref TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider_id TEXT NOT NULL,
 metadata TEXT NOT NULL, parts TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 attempted REAL, due REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
 UNIQUE(namespace, provider_id));
CREATE INDEX records_due ON records(namespace, due, attempted);
CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 namespace TEXT NOT NULL, ref TEXT NOT NULL, revision INTEGER NOT NULL, occurred REAL NOT NULL);
CREATE VIRTUAL TABLE texts USING fts5(ref UNINDEXED, namespace UNINDEXED, transcript, summary, tokenize='unicode61');
PRAGMA application_id=1145786947;
PRAGMA user_version=1;
COMMIT;
'''


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
                    'PRAGMA user_version').fetchone()[0] != 1:
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
                    db.executescript(SCHEMA)
                if db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or db.execute(
                        'PRAGMA user_version').fetchone()[0] != 1:
                    raise Failure('archive_schema_unsupported')
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

    def discover(self, account, items, checkpoint, now, guard=lambda: None):
        ns = namespace(account)
        def change(db):
            queued = db.execute('SELECT count(*) FROM records WHERE namespace=? AND attempted IS NULL', (ns,)).fetchone()[0]
            unique = {item['id']: item for item in items}
            new = [item for item in unique.values() if not db.execute('SELECT 1 FROM records WHERE namespace=? AND provider_id=?',
                (ns, item['id'])).fetchone()]
            if queued + len(new) > QUEUE_LIMIT:
                raise Failure('archive_queue_full')
            db.execute('INSERT OR IGNORE INTO accounts(namespace) VALUES(?)', (ns,))
            for item in new:
                db.execute('INSERT INTO records(ref,namespace,provider_id,metadata,parts) VALUES(?,?,?,?,?)',
                    (uuid.uuid4().hex, ns, item['id'], canonical(item).decode(), '{}'))
            db.execute('UPDATE accounts SET checkpoint=?,error=NULL WHERE namespace=?',
                       (canonical(checkpoint).decode(), ns))
        self.write(change, guard)

    def work(self, account, now, limit=8, guard=lambda: None):
        ns = namespace(account)
        def select(db):
            db.execute('INSERT OR IGNORE INTO accounts(namespace) VALUES(?)',(ns,))
            turn = db.execute('SELECT work_turn FROM accounts WHERE namespace=?',(ns,)).fetchone()[0]
            groups = []
            for condition, budget in (('attempted IS NULL', 3),
                                      ('attempted IS NOT NULL AND failures>0', 3),
                                      ('attempted IS NOT NULL AND failures=0', 2)):
                groups.append([row[0] for row in db.execute('SELECT provider_id FROM records WHERE namespace=? AND due<=? AND ' + condition +
                    ' ORDER BY due,ref LIMIT ?', (ns, now, budget))])
            # Persist priority before execution: even a pass interrupted after
            # its first slow record serves another class on its next attempt.
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
            failed = any(part.get('attempt_state') in ('pending','unavailable','error') for part in parts.values())
            if sum(len(p['text'].encode()) for p in parts.values()) > MAX_TEXT:
                raise Failure('archive_content_limit')
            body = canonical(metadata).decode()
            revision = row['revision'] + int(usable_change or (row['revision'] > 0 and body != row['metadata']))
            failures = min(row['failures'] + 1, 10) if failed else 0
            due = now + min(3600, 60 * 2 ** (failures - 1)) if failed else now + 3600
            db.execute('UPDATE records SET metadata=?,parts=?,revision=?,attempted=?,due=?,failures=? WHERE ref=?',
                (body, canonical(parts).decode(), revision, now, due, failures, row['ref']))
            if usable_change:
                db.execute('DELETE FROM texts WHERE ref=?', (row['ref'],))
                db.execute('INSERT INTO texts(ref,namespace,transcript,summary) VALUES(?,?,?,?)',
                    (row['ref'], ns, parts.get('transcript', {}).get('text', ''), parts.get('summary', {}).get('text', '')))
                db.execute('INSERT INTO events(event_id,namespace,ref,revision,occurred) VALUES(?,?,?,?,?)',
                           (uuid.uuid4().hex, ns, row['ref'], revision, now))
            db.execute('UPDATE accounts SET last_success=COALESCE(?,last_success),error=? WHERE namespace=?',
                (now if any(o[0] == 'available' for o in observations.values()) else None,
                 next((o[2] for o in observations.values() if o[2]), None), ns))
        self.write(change, guard)

    def account_error(self, account, code):
        self.write(lambda db: db.execute('INSERT INTO accounts(namespace,error) VALUES(?,?) ON CONFLICT(namespace) DO UPDATE SET error=excluded.error',
                                        (namespace(account), code)))

    def status(self, account):
        ns = namespace(account)
        with self.connection() as db:
            row = db.execute('SELECT last_success,error FROM accounts WHERE namespace=?', (ns,)).fetchone()
            counts = db.execute('SELECT count(*),sum(attempted IS NULL),sum(failures>0) FROM records WHERE namespace=?', (ns,)).fetchone()
            return {'coverage':'unknown', 'record_count':counts[0], 'queued_count':counts[1] or 0,
                    'retry_count':counts[2] or 0, 'last_success':row[0] if row else None, 'error':row[1] if row else None}

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
