"""Private, bounded SQLite text state. No provider or messaging dependencies."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time
import uuid

from . import archive_feed
from .security import Failure, canonical, loads

MAX_TEXT = 2 * 1024 ** 2
HEADROOM = 64 * 1024 ** 2
QUEUE_LIMIT = 10000
HEAD_SLICE = 100
FAR = 1e18  # `due` of a record that waits for a changed list fingerprint instead of a clock
STAGES = (6 * 3600, 48 * 3600)  # seconds after creation at which a saved Omi record gets its scheduled second looks
WEEK = 7 * 86400  # a provider_missing Omi record is checked (without a transcript) at most this often
DAY = 86400  # `response_too_large` backs off this long: the same body will not shrink within the hour
# An Omi record that answered 404 twice in a row (merged away, deleted or locked) is `provider_missing`: its stored text
# stays (docs/DATA-POLICY.md), it costs no transcript read, and a list row with its ID makes it due again. The mark is
# the parts' `attempt_state`, so no schema change is needed.
MISSING = "CASE WHEN json_valid(parts) THEN json_extract(parts,'$.transcript.attempt_state')='provider_missing' ELSE 0 END"
WINDOW = 3600
SLACK = 75  # dispatch drift inside one pass (60 s) plus a provider timeout: never reuse a call that may still count
APPLICATION_ID = 0x444B5243
SCHEMA_VERSION = 5
DELETE_BATCH = 200  # records one deletion transaction removes (ANYM-730)
INDEX_BATCH_BYTES = 4 * 1024 ** 2  # stored bytes one build batch reads and indexes (at least one record)
INDEX_BATCH_ROWS = 500
INDEX_STEP_SECONDS = 10  # one collector pass spends at most this long building the schema-4 index
MAX_METADATA = 64 * 1024  # bytes: a stored metadata document above this is skipped by the index build, never parsed
MAX_LABEL = 64
MAX_LABELS = 256
MAX_RESTARTS = 3
HIT_SPEAKERS = 8
BYTES = 'length(CAST(metadata AS BLOB))+length(CAST(parts AS BLOB))'  # stored bytes of a record (SQLite length(TEXT) counts characters)
# Schema 4 (ANYM-725): derived tables only. `texts4` holds plain text (no JSON keys, no labels), `speakers` the provider
# labels, `index_rows` what each indexed row was built from (verified against `records` before publication), and
# `records.created_at`/`duration_s` the normalized filter values. Records, refs, revisions, parts and events are untouched.
INDEX_TABLES = '''
CREATE VIRTUAL TABLE IF NOT EXISTS texts4 USING fts5(ref UNINDEXED, namespace UNINDEXED, title, transcript, summary, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS speakers(ref TEXT NOT NULL, label TEXT NOT NULL, display TEXT NOT NULL, PRIMARY KEY(ref, label)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS speakers_label ON speakers(label, ref);
CREATE TABLE IF NOT EXISTS index_rows(ref TEXT PRIMARY KEY, revision INTEGER NOT NULL, digest TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS index_skips(ref TEXT PRIMARY KEY) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS index_dirty(ref TEXT PRIMARY KEY) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS records_created ON records(namespace, created_at);
'''
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
 created_at TEXT, duration_s INTEGER, part_digest TEXT,
 UNIQUE(namespace, provider_id));
CREATE INDEX records_due ON records(namespace, due, attempted);
CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 namespace TEXT NOT NULL, ref TEXT NOT NULL, revision INTEGER NOT NULL, occurred REAL NOT NULL);
{index_tables}CREATE TABLE ledger(credential TEXT NOT NULL, bucket TEXT NOT NULL, window_start REAL NOT NULL, count INTEGER NOT NULL,
 updated REAL NOT NULL, PRIMARY KEY(credential, bucket, window_start));
CREATE TABLE meta(key TEXT PRIMARY KEY, value);  -- key/value rows: the backup tooling reads archive_id and incarnation (ANYM-714)
CREATE TABLE tombstones(provider_hash TEXT PRIMARY KEY);  -- ANYM-730: SHA-256 of namespace NUL provider_id, no content
INSERT INTO meta(key,value) VALUES('archive_id','{archive}'),('incarnation',1),('journal_id','{journal}'),('text_reconciled',1),
 ('index_state','ready');
PRAGMA application_id=1145786947;
PRAGMA user_version=5;
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


# Schema 4 upgrade of a schema-3 archive is additive and quick: new columns and tables, and a build state in `meta`. The
# index itself is filled in bounded batches by `index_step`, and `PRAGMA user_version=4` is set only by its publication.
UPGRADE_V4 = '''
BEGIN IMMEDIATE;
{alters}{index_tables}INSERT OR REPLACE INTO meta(key,value) VALUES('index_state','building');
-- Whatever writes metadata, parts or revision while the index builds (this package, or an older one that does not know
-- the index) marks the record; the build rebuilds every marked record behind the cursor before it may publish.
CREATE TRIGGER IF NOT EXISTS records_dirty_update AFTER UPDATE OF metadata, parts, revision ON records
 WHEN (SELECT value FROM meta WHERE key='index_state')='building' BEGIN INSERT OR IGNORE INTO index_dirty(ref) VALUES(NEW.ref); END;
CREATE TRIGGER IF NOT EXISTS records_dirty_insert AFTER INSERT ON records
 WHEN (SELECT value FROM meta WHERE key='index_state')='building' BEGIN INSERT OR IGNORE INTO index_dirty(ref) VALUES(NEW.ref); END;
INSERT OR IGNORE INTO meta(key,value) VALUES('index_cursor',0);
INSERT OR REPLACE INTO meta(key,value) VALUES('index_restarts',0);
COMMIT;
'''
V4_COLUMNS = (('records', 'created_at', 'TEXT'), ('records', 'duration_s', 'INTEGER'), ('records', 'part_digest', 'TEXT'))


def upgrade_v4(db):
    alters = ''
    for table, column, declaration in V4_COLUMNS:
        if column not in {row[1] for row in db.execute(f'PRAGMA table_info({table})')}:
            alters += f'ALTER TABLE {table} ADD COLUMN {column} {declaration};\n'
    db.executescript(UPGRADE_V4.format(alters=alters, index_tables=INDEX_TABLES))


# Schema 5 (ANYM-730) is additive: one table of provider-ID hashes (no content). The backup tooling reads it as the authorization
# a deletion needs (backup_recordings_continuity.py); an older package refuses a schema-5 archive (`archive_schema_unsupported`).
UPGRADE_V5 = '''
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS tombstones(provider_hash TEXT PRIMARY KEY);
PRAGMA user_version=5;
COMMIT;
'''


def upgrade_v5(db):
    db.executescript(UPGRADE_V5)


def provider_hash(ns, provider_id):
    return hashlib.sha256((ns + '\x00' + provider_id).encode('utf-8', 'surrogatepass')).hexdigest()


def with_ids(template, **extra):
    return template.format(archive=uuid.uuid4().hex, journal=uuid.uuid4().hex, index_tables=INDEX_TABLES, **extra)


def provider_of(account):
    return account['provider'] if isinstance(account, dict) else account.provider


def provider_missing(parts):
    """True for the parts of a provider_missing record; a stored document that does not parse is not one."""
    if isinstance(parts, str):
        try:
            parts = loads(parts)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return False
    return isinstance(parts, dict) and isinstance(parts.get('transcript'), dict) and \
        parts['transcript'].get('attempt_state') == 'provider_missing'


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


def stage_due(metadata, now):
    """When a saved Omi record is read again: the first stage after `now`, FAR when none is left or `created_at` is unusable.
    Computed from the clock alone, so a read at or after a stage time, whoever caused it, counts as that stage."""
    created = parse_time(metadata.get('created_at'))
    if created is None:
        return FAR
    moment = datetime.fromisoformat(created.replace('Z', '+00:00')).timestamp()
    return next((moment + stage for stage in STAGES if moment + stage > now), FAR)


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


SECONDS_2000, SECONDS_2101 = 946684800, 4133980800
SPEAKER_KEYS = ('speaker_name', 'speaker_id', 'speaker')  # the order omi.content and the PLAUD projection use
TRANSCRIPT_KEYS = frozenset(('text', 'content', 'speaker', 'speaker_name', 'speaker_id', 'start', 'end', 'start_time', 'end_time'))


def utterance(row):
    """True for a stored transcript object: only known keys, and `placed` only as omi.content's unplaced marker
    (`false` with null offsets). Any other object is ordinary text and stays searchable as it is."""
    if not isinstance(row, dict):
        return False
    if 'placed' in row:
        if not (row['placed'] is False and row.get('start') is None and row.get('end') is None):
            return False
        row = {key: value for key, value in row.items() if key != 'placed'}
    return set(row) <= TRANSCRIPT_KEYS


def utc_text(moment):
    """Fixed-width UTC text (`2026-01-11T07:30:00.000000Z`): string order is time order, so a plain index can filter it."""
    return moment.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def parse_time(value):
    """A provider timestamp as fixed-width UTC text, or None. Strings are ISO-8601 (no zone means UTC, as Omi documents).
    PLAUD numbers have no documented unit: below 10^11 they are Unix seconds, from 10^11 up milliseconds (a heuristic),
    accepted only inside the years 2000-2100."""
    try:
        if isinstance(value, str):
            moment = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return utc_text(moment)
        if type(value) in (int, float) and math.isfinite(value):
            seconds = value / 1000 if value >= 1e11 else value
            if SECONDS_2000 <= seconds < SECONDS_2101:
                return utc_text(datetime.fromtimestamp(seconds, tz=timezone.utc))
    except (ValueError, OverflowError, OSError):
        pass
    return None


def derive_metadata(metadata):
    """The filter values of one stored list row: created_at, started_at and duration_s (whole seconds). Omi: finished_at -
    started_at. PLAUD: `duration` in milliseconds. Null, unparsable or negative values are None."""
    created = parse_time(metadata.get('created_at'))
    started = parse_time(next((metadata[key] for key in ('started_at', 'start_at') if metadata.get(key) is not None), None))
    duration = None
    if 'duration' in metadata:
        value = metadata['duration']
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            duration = int(value // 1000)
    elif started is not None and (finished := parse_time(metadata.get('finished_at'))) is not None:
        seconds = (datetime.fromisoformat(finished.replace('Z', '+00:00')) - datetime.fromisoformat(started.replace('Z', '+00:00'))).total_seconds()
        duration = int(seconds) if seconds >= 0 else None
    return {'created_at': created, 'started_at': started, 'duration_s': duration}


def encodable(value):
    """False for a string SQLite cannot bind (a lone surrogate that a JSON escape such as \\ud800 decodes to)."""
    try:
        value.encode('utf-8')
        return True
    except UnicodeEncodeError:
        return False


def plain_text(kind, text):
    """(plain text, speaker labels) of a stored part. A transcript is stored as one JSON object per utterance; the plain
    text is the spoken words only, and the labels (`speaker_name`, `speaker_id` or `speaker`, at most 64 characters,
    first spelling kept, case-insensitive duplicates dropped) are kept apart. A line that is not an utterance object
    (an older plain line, or a summary sentence) stays as it is."""
    if not text:
        return '', []
    out, labels, seen = [], [], set()
    for line in text.split('\n'):
        row = None
        if line.startswith('{'):
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                row = None  # not an utterance object (or too deeply nested to be one): ordinary text
        if kind == 'transcript':
            body = row.get('text', row.get('content')) if utterance(row) else None
        else:
            body = row['text'] if isinstance(row, dict) and set(row) == {'type', 'text'} else None
        if not isinstance(body, str) or not encodable(body):
            out.append(line)  # the stored line itself (ASCII-escaped, as accepted) when its decoded words cannot be stored
            continue
        if body:
            out.append(body)
        label = next((row[key] for key in SPEAKER_KEYS if row.get(key) is not None and row[key] != ''), None)
        if type(label) in (str, int) and len(labels) < MAX_LABELS:
            label = str(label).strip()[:MAX_LABEL]
            if label and encodable(label) and label.lower() not in seen:
                seen.add(label.lower())
                labels.append(label)
    return '\n'.join(out), labels


def part_digest(parts):
    return hashlib.sha256('|'.join(str(parts.get(kind, {}).get('digest') or '') for kind in ('transcript', 'summary')).encode()).hexdigest()


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
                    'PRAGMA user_version').fetchone()[0] not in (1, 2, 3, 4, 5):
                raise Failure('archive_schema_unsupported')
        return store.run('backup', destination)
    except (OSError, sqlite3.Error, ValueError):
        raise Failure('archive_unavailable') from None


class Store:
    feed_enabled = False  # set by the runtime while the configuration has `event_router` (ANYM-726)
    feed_handled = None  # process memory of the marker a feed bump reacted to (see archive_feed.inspect)
    clock = staticmethod(time.time)

    def __init__(self, settings):
        self.settings, self.lock = settings, None
        self.feed_handled = {}
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
                if db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or version not in (1, 2, 3, 4, 5):
                    raise Failure('archive_schema_unsupported')
                if version == 1:
                    # Only this derived index is rebuilt. Accepted records, refs,
                    # revisions and journal survive atomically; interruption rolls back.
                    db.executescript(UPGRADE_INDEX)
                    version = 2
                if version == 2:
                    # Additive only; any failure rolls the whole script back to a usable schema 2.
                    upgrade_v3(db)
                    version = 3
                if version == 3:
                    # Additive only: new columns and tables. The index fills in bounded batches (`index_step`) and
                    # `user_version` becomes 4 only when it is published; schema 3 stays fully usable until then.
                    upgrade_v4(db)
                if version == 4:
                    upgrade_v5(db)  # additive; a schema-3 archive reaches it after its index is published and the next open
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
                # A record with one part only has NULL for the other: each part counts as 0 when absent.
                db.execute("UPDATE accounts SET text_bytes=(SELECT COALESCE(SUM("
                           "COALESCE(length(CAST(json_extract(parts,'$.transcript.text') AS BLOB)),0)"
                           "+COALESCE(length(CAST(json_extract(parts,'$.summary.text') AS BLOB)),0)),0) FROM records "
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
            fenced = 0
            if self.tombstones_exist(db):  # ANYM-730: a deleted recording is never archived again
                for ident in list(unique):
                    if db.execute('SELECT 1 FROM tombstones WHERE provider_hash=?', (provider_hash(ns, ident),)).fetchone():
                        del unique[ident]
                        fenced += 1
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
                created = db.execute('INSERT INTO records(ref,namespace,provider_id,metadata,parts,fingerprint) VALUES(?,?,?,?,?,?)',
                    (ref, ns, item['id'], canonical(item).decode(), '{}', fingerprints.get(item['id'])))
                self.put_text(db, ns, created.lastrowid, ref, item, {}, 0, new=True)
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
            return {'inserted': len(new), 'known': len(known), 'fenced': fenced}
        return self.write(change, guard)

    def refresh(self, db, ns, row, item, fingerprint, now):
        """A listed row of a known record: a changed fingerprint makes a saved record due; a changed title updates the
        stored title and index in place (no provider call, no text-change event). A provider_missing record whose ID is
        listed again is due now."""
        if provider_missing(row['parts']):
            self.reactivate(db, row['ref'], loads(row['parts']), now)
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
            revision = row['revision'] + int(row['revision'] > 0)
            db.execute('UPDATE records SET metadata=?,revision=?  WHERE ref=?', (canonical(stored).decode(), revision, row['ref']))
            self.put_text(db, ns, row['rid'], row['ref'], stored, parts, revision)

    @staticmethod
    def reactivate(db, ref, parts, now):
        """Clear the provider_missing mark (text untouched): the next pass reads the record in full."""
        for part in parts.values():
            if isinstance(part, dict) and part.get('attempt_state') == 'provider_missing':
                part.update(attempt_state=None, attempt_error=None)
        db.execute('UPDATE records SET parts=?,due=?,failures=0 WHERE ref=?', (canonical(parts).decode(), now, ref))

    def probe(self, account, provider_id, result, now, guard=lambda: None):
        """The weekly check of a provider_missing Omi record, made without a transcript. `found`: due now for a full
        read; anything else (404, timeout, outage, a malformed answer): checked again in a week, never sooner."""
        ns = namespace(account)
        def change(db):
            row = db.execute('SELECT * FROM records WHERE namespace=? AND provider_id=?', (ns, provider_id)).fetchone()
            if row is None:
                raise Failure('archive_unavailable')
            parts = loads(row['parts'])
            if not provider_missing(parts):
                return
            if result == 'found':
                self.reactivate(db, row['ref'], parts, now)
            else:
                db.execute('UPDATE records SET attempted=?,due=? WHERE ref=?', (now, now + WEEK, row['ref']))
        self.write(change, guard)

    @staticmethod
    def index_mode(db):
        """(state, cursor): `building` while the schema-4 index is filled, `ready` once published. The cursor is the
        highest `records` rowid whose row is already in the new index."""
        meta = {row[0]: row[1] for row in db.execute("SELECT key,value FROM meta WHERE key IN ('index_state','index_cursor')")}
        return meta.get('index_state', 'ready'), int(meta.get('index_cursor') or 0)

    def put_text(self, db, ns, rid, ref, metadata, parts, revision, *, new=False):
        """Every write of searchable text goes through here. While the index is built the old `texts` row is kept exactly as
        before (reads use it), and the new index is written too for a record at or behind the cursor; a record ahead of
        the cursor is built later from its then-current data. After publication only the new index exists."""
        state, cursor = self.index_mode(db)
        if state != 'ready':
            if not new:
                db.execute('DELETE FROM texts WHERE ref=?', (ref,))
            db.execute('INSERT INTO texts(ref,namespace,title,transcript,summary) VALUES(?,?,?,?,?)',
                       (ref, ns, title_of(metadata), parts.get('transcript', {}).get('text', ''), parts.get('summary', {}).get('text', '')))
        if state == 'ready' or rid <= cursor:
            self.index_record(db, ns, rid, ref, metadata, parts, revision)

    @staticmethod
    def derive_index(metadata, parts):
        """Everything the new index stores for one record, derived without touching the database; None when this record
        cannot be derived (any failure: the caller counts it as skipped, so one record never stops a write or the build)."""
        try:
            transcript, labels = plain_text('transcript', parts.get('transcript', {}).get('text', ''))
            summary, _ = plain_text('summary', parts.get('summary', {}).get('text', ''))
            found = {'title': title_of(metadata), 'transcript': transcript, 'summary': summary, 'labels': labels,
                     'derived': derive_metadata(metadata), 'digest': part_digest(parts)}
            for value in (found['title'], transcript, summary, *labels):
                value.encode('utf-8')  # anything SQLite could not bind is a skip here, never an error at the write
            return found
        except Exception:
            return None

    def index_record(self, db, ns, rid, ref, metadata, parts, revision):
        """Replace the new-index rows of one record from its current stored data (idempotent)."""
        found = self.derive_index(metadata, parts)
        if found is None:
            self.index_skip(db, rid, ref)
            return
        db.execute('DELETE FROM texts4 WHERE rowid=?', (rid,))
        db.execute('INSERT INTO texts4(rowid,ref,namespace,title,transcript,summary) VALUES(?,?,?,?,?,?)',
                   (rid, ref, ns, found['title'], found['transcript'], found['summary']))
        db.execute('DELETE FROM speakers WHERE ref=?', (ref,))
        db.executemany('INSERT OR IGNORE INTO speakers(ref,label,display) VALUES(?,?,?)',
                       [(ref, label.lower(), label) for label in found['labels']])
        db.execute('UPDATE records SET created_at=?,duration_s=?,part_digest=? WHERE ref=?',
                   (found['derived']['created_at'], found['derived']['duration_s'], found['digest'], ref))
        db.execute('INSERT OR REPLACE INTO index_rows(ref,revision,digest) VALUES(?,?,?)', (ref, revision, found['digest']))
        db.execute('DELETE FROM index_skips WHERE ref=?', (ref,))
        db.execute('DELETE FROM index_dirty WHERE ref=?', (ref,))

    @staticmethod
    def index_skip(db, rid, ref):
        """A stored row the build cannot read (malformed or oversized): counted, never indexed, never blocking."""
        db.execute('DELETE FROM texts4 WHERE rowid=?', (rid,))
        db.execute('DELETE FROM speakers WHERE ref=?', (ref,))
        db.execute('DELETE FROM index_rows WHERE ref=?', (ref,))
        db.execute('UPDATE records SET created_at=NULL,duration_s=NULL,part_digest=NULL WHERE ref=?', (ref,))
        db.execute('INSERT OR IGNORE INTO index_skips(ref) VALUES(?)', (ref,))
        db.execute('DELETE FROM index_dirty WHERE ref=?', (ref,))

    def index_row(self, db, row):
        try:
            metadata_bytes = len(row['metadata'].encode('utf-8', 'replace'))
            # A record over the batch budget is skipped (counted), so no batch ever exceeds INDEX_BATCH_BYTES.
            if metadata_bytes > MAX_METADATA or metadata_bytes + len(row['parts'].encode('utf-8', 'replace')) > INDEX_BATCH_BYTES:
                raise ValueError
            metadata, parts = loads(row['metadata']), loads(row['parts'])
            if not isinstance(metadata, dict) or not isinstance(parts, dict) or any(
                    not isinstance(part, dict) or not isinstance(part.get('text', ''), str) for part in parts.values()):
                raise ValueError
        except (ValueError, TypeError, RecursionError, UnicodeError):
            self.index_skip(db, row['rid'], row['ref'])
            return
        self.index_record(db, row['namespace'], row['rid'], row['ref'], metadata, parts, row['revision'])

    def index_rows_of(self, db, rids):
        """Build the given records (a bounded batch chosen by `index_take`). The stored size is measured in SQL first: a
        record over the budget is skipped (counted) without its metadata or parts ever being read into memory."""
        for rid in rids:
            head = db.execute(f'SELECT ref, length(CAST(metadata AS BLOB)), {BYTES} FROM records WHERE rowid=?', (rid,)).fetchone()
            if head is None:
                continue
            if head[1] > MAX_METADATA or head[2] > INDEX_BATCH_BYTES:
                self.index_skip(db, rid, head[0])
                continue
            row = db.execute('SELECT rowid AS rid,* FROM records WHERE rowid=?', (rid,)).fetchone()
            if row is not None:
                self.index_row(db, row)

    @staticmethod
    def index_take(sizes):
        taken, total = [], 0
        for rid, size in sizes:
            if taken and (total + size > INDEX_BATCH_BYTES or len(taken) >= INDEX_BATCH_ROWS):
                break
            taken.append(rid)
            total += size
        return taken

    def index_cursor_save(self, db, cursor):
        db.execute("UPDATE meta SET value=? WHERE key='index_cursor'", (cursor,))

    MISMATCH = (f'''SELECT r.rowid AS rid, {BYTES} AS size FROM records r
        LEFT JOIN index_rows i ON i.ref=r.ref
        WHERE NOT EXISTS (SELECT 1 FROM index_skips k WHERE k.ref=r.ref)
          AND (i.ref IS NULL OR i.revision<>r.revision OR i.digest IS NOT r.part_digest) ORDER BY r.rowid LIMIT ?''')

    def index_batch(self, db):
        """One bounded write transaction: build the next records, repair rows that no longer match `records`, or - when
        the index agrees with `records` row for row - publish it. Returns built | published | dropped | idle | stuck."""
        state, cursor = self.index_mode(db)
        if state == 'ready':
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='texts'").fetchone() is None:
                return 'idle'
            db.execute('DROP TABLE texts')  # the superseded index; a separate transaction from the publication
            return 'dropped'
        sizes = db.execute(f'SELECT rowid, {BYTES} FROM records WHERE rowid>? ORDER BY rowid LIMIT ?',
                           (cursor, INDEX_BATCH_ROWS)).fetchall()
        if sizes:
            taken = self.index_take(sizes)
            self.index_rows_of(db, taken)
            self.index_cursor_save(db, taken[-1])
            return 'built'
        # Every record is behind the cursor now. First the records any writer touched while the index was built (the dirty
        # marks of the triggers: this package's dual write clears its own, an older package's stay), then the rows whose
        # revision or accepted-part digest differs from `records`: rebuild exactly those, never publish over them.
        dirty = self.index_take([(row[0], row[1]) for row in db.execute(
            f'SELECT r.rowid, {BYTES} FROM index_dirty d JOIN records r ON r.ref=d.ref ORDER BY r.rowid LIMIT ?', (INDEX_BATCH_ROWS,))])
        if dirty:
            self.index_rows_of(db, dirty)
            return 'built'
        stale = self.index_take([(row[0], row[1]) for row in db.execute(self.MISMATCH, (INDEX_BATCH_ROWS,))])
        if stale:
            self.index_rows_of(db, stale)
            return 'built'
        db.execute('DELETE FROM index_dirty WHERE ref NOT IN (SELECT ref FROM records)')
        records = db.execute('SELECT count(*) FROM records').fetchone()[0]
        built = db.execute('SELECT count(*) FROM index_rows').fetchone()[0]
        skipped = db.execute('SELECT count(*) FROM index_skips').fetchone()[0]
        if built + skipped != records or db.execute('SELECT count(*) FROM texts4').fetchone()[0] != built:
            restarts = int(db.execute("SELECT value FROM meta WHERE key='index_restarts'").fetchone()[0]) + 1
            db.execute("UPDATE meta SET value=? WHERE key='index_restarts'", (restarts,))
            if restarts > MAX_RESTARTS:
                return 'stuck'
            db.execute('DELETE FROM texts4 WHERE rowid NOT IN (SELECT rowid FROM records)')
            db.execute('DELETE FROM index_rows WHERE ref NOT IN (SELECT ref FROM records)')
            self.index_cursor_save(db, 0)
            return 'built'
        db.execute("UPDATE meta SET value='ready' WHERE key='index_state'")
        db.execute('PRAGMA user_version=4')
        return 'published'

    def index_pending(self):
        with self.connection() as db:
            state, _ = self.index_mode(db)
            return state != 'ready' or db.execute("SELECT 1 FROM sqlite_master WHERE name='texts'").fetchone() is not None

    def index_step(self, batches=None, seconds=INDEX_STEP_SECONDS):
        """Advance the schema-4 build, one bounded write transaction per batch, for at most `batches` batches or
        `seconds`. Each batch is its own connection and deadline, so an archive of any size upgrades across passes."""
        deadline, outcome = time.monotonic() + seconds, 'idle'
        if not self.index_pending():
            return outcome
        done = 0
        while True:
            outcome = self.write(self.index_batch)
            done += 1
            if outcome == 'stuck':
                raise Failure('index_verification_failed')
            if outcome == 'idle' or (batches is not None and done >= batches) or time.monotonic() > deadline:
                return outcome

    def version(self):
        with self.connection() as db:
            return db.execute('PRAGMA user_version').fetchone()[0]

    def index_state(self):
        with self.connection() as db:
            return self.index_summary(db)

    def index_summary(self, db, ns=None):
        """Counts of the whole archive, or of one namespace (what an account's status may show)."""
        state, cursor = self.index_mode(db)
        scope, args = ('WHERE r.namespace=?', (ns,)) if ns is not None else ('', ())
        def one(sql, *values):
            return db.execute(sql, values).fetchone()[0]
        return {'state': state, 'cursor': cursor, 'total': one(f'SELECT count(*) FROM records r {scope}', *args),
                'built': one(f'SELECT count(*) FROM index_rows i JOIN records r ON r.ref=i.ref {scope}', *args),
                'skipped': one(f'SELECT count(*) FROM index_skips k JOIN records r ON r.ref=k.ref {scope}', *args),
                'restarts': int(one("SELECT value FROM meta WHERE key='index_restarts'") or 0) if state != 'ready' else 0}

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
        return [ident for ident, *_ in self.work_items(account, now, limit, guard)]

    def work_items(self, account, now, limit=8, guard=lambda: None, rotate=True):
        """Provider ids to read now with `fresh` and `probe` flags. Omi: never-fetched records newest first (last 24 h ahead
        of the backlog), then failed records by backoff, then records waiting on a pending part or a changed list row and
        the weekly checks of provider_missing records (`probe`: no transcript).
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
                    groups.append([(row[0], bool(row[1]), False) for row in rows])
                else:
                    groups.append([(row[0], False, omi and bool(row[1])) for row in db.execute(
                        f'SELECT provider_id,COALESCE({MISSING},0) FROM records WHERE namespace=? AND due<=? AND ' + condition +
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
        trial = {}
        def change(db):
            row = db.execute('SELECT rowid AS rid,* FROM records WHERE namespace=? AND provider_id=?', (ns, provider_id)).fetchone()
            if row is None:
                raise Failure('archive_unavailable')
            parts, usable_change = loads(row['parts']), False
            previous = {kind: part.get('attempt_error') for kind, part in parts.items() if isinstance(part, dict)}
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
            error = next((o[2] for o in observations.values() if o[2]), None)
            if provider_of(account) == 'omi':
                # Only a failed read backs off; a part that is still pending waits on a bounded schedule, and a
                # complete record waits for a changed list row. Nothing is re-read hourly.
                codes = {o[2] for o in observations.values()}
                failures = min(row['failures'] + 1, 10) if 'error' in states else 0
                gap = pending_gap(metadata, now) if failures == 0 and failed else None
                due = (now + (DAY if 'response_too_large' in codes else min(3600, 60 * 2 ** (failures - 1))) if failures else
                       now + gap if gap else FAR)
                if codes == {'not_found_or_forbidden'} and row['failures'] > 0 and \
                        all(previous.get(kind) == 'not_found_or_forbidden' for kind in observations):
                    # The second 404 in a row: gone at the provider (merged away, deleted or locked). Kept, not retried.
                    for kind in observations:
                        parts[kind]['attempt_state'] = 'provider_missing'
                    failures, due, error = 0, now + WEEK, None
            else:
                failures = min(row['failures'] + 1, 10) if failed else 0
                due = now + min(3600, 60 * 2 ** (failures - 1)) if failed else now + 3600
            db.execute('UPDATE records SET metadata=?,parts=?,revision=?,attempted=?,due=?,failures=? WHERE ref=?',
                (body, canonical(parts).decode(), revision, now, due, failures, row['ref']))
            if usable_change or body != row['metadata']:
                self.put_text(db, ns, row['rid'], row['ref'], metadata, parts, revision)
            if usable_change:
                if self.feed_enabled:  # database work only: a feed problem can never stop text from being archived (ANYM-726)
                    archive_feed.prepare(db, self.directory, self.clock(), self.feed_handled, trial)
                db.execute('INSERT INTO events(event_id,namespace,ref,revision,occurred) VALUES(?,?,?,?,?)',
                           (uuid.uuid4().hex, ns, row['ref'], revision, now))
            db.execute('UPDATE accounts SET last_success=COALESCE(?,last_success),error=?,text_bytes=MAX(0,text_bytes+?) '
                       'WHERE namespace=?',
                (now if any(o[0] == 'available' for o in observations.values()) else None,
                 error, text_after - text_before, ns))
        self.write(change, guard)
        self.feed_handled.update(trial)  # only a committed bump counts as handled

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
                                f'sum(attempted IS NOT NULL AND failures=0 AND due<? AND NOT COALESCE({MISSING},0)),'
                                f'sum(COALESCE({MISSING},0)) FROM records WHERE namespace=?',
                                (FAR / 10, ns)).fetchone()
            result = {'coverage':'unknown', 'record_count':counts[0], 'queued_count':counts[1] or 0,
                      'retry_count':counts[2] or 0, 'last_success':row['last_success'] if row else None,
                      'error':row['error'] if row else None}
            index = self.index_summary(db, ns)
            # `undated`: records without a usable provider creation time (excluded by date filters). Known once the index is published.
            result['index'] = {key: index[key] for key in ('state', 'built', 'total', 'skipped')}
            result['undated'] = (db.execute('SELECT count(*) FROM records WHERE namespace=? AND created_at IS NULL', (ns,)).fetchone()[0]
                                 if index['state'] == 'ready' else None)
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
                with_text=with_text, queued=queued, pending=pending, failed=failed, provider_missing=counts[5] or 0,
                throttled_until=throttled if throttled is not None and throttled > now else None,
                usage=usage, db_bytes=sum(path.stat().st_size for path in self.files()),
                bytes_ceiling=self.settings.max_bytes, projected_db_bytes=projected,
                capacity_warning=projected is not None and projected > self.settings.max_bytes / 3, eta=eta,
                head_backlog=since is not None, head_slice_full=since is not None and now - since > 3600)
            if uncertain:
                result['error'] = 'ledger_uncertain'
            return result

    def search(self, account, expression, limit, created_after=None, created_before=None, speaker=None, order='relevance'):
        """`expression` None is browse (newest first). Filters and browse need the published schema-4 index; until then
        a plain query reads the old index, and a filter is refused with `index_building`."""
        ns = namespace(account)
        filtered = created_after is not None or created_before is not None or speaker is not None
        with self.connection() as db:
            db.execute('BEGIN')  # one snapshot: the index state read below and the query agree across a publication
            state, _ = self.index_mode(db)
            if state != 'ready' and (filtered or expression is None or order == 'newest'):
                raise Failure('index_building', 409)  # `records.created_at` is complete only once the index is published
            # FTS MATCH narrows by indexed terms; both sides of the join retain
            # the current namespace. Never materialize accepted text for all hits.
            # The order must not depend on other namespaces: bm25() uses statistics of the whole FTS table, so a document
            # of a forbidden account could change which permitted hit comes first. The rank here is a title hit, then a
            # summary hit, then a transcript hit, then the newer revision, then the reference: each fact is a property of the
            # row itself (MATCH membership), never a corpus statistic. `newest` and browse order by the stored creation time.
            columns = '''r.rowid AS rid,r.ref,r.provider_id,r.revision,r.metadata,
                substr(COALESCE(NULLIF(json_extract(r.metadata,'$.title'),''),json_extract(r.metadata,'$.name'),''),1,256) AS title,
                json_remove(r.parts,'$.transcript.text','$.summary.text') AS evidence'''
            if expression is None:
                where, params = ['r.namespace=?'], [ns]
                source, snippet = 'records r INDEXED BY records_created', "''"
            else:
                table = 'texts4' if state == 'ready' else 'texts'
                join = 'r.rowid=texts4.rowid' if state == 'ready' else 'r.ref=texts.ref'
                where, params = [f'{table} MATCH ?', f'{table}.namespace=?', 'r.namespace=?'], [expression, ns, ns]
                source, snippet = f'{table} JOIN records r ON {join}', f"substr(snippet({table},-1,'[',']','…',32),1,320)"
            if created_after is not None:
                where.append('r.created_at>=?')
                params.append(created_after)
            if created_before is not None:
                where.append('r.created_at<=?')
                params.append(created_before)
            if speaker is not None:
                where.append('EXISTS (SELECT 1 FROM speakers s WHERE s.ref=r.ref AND s.label=?)')
                params.append(speaker)
            if expression is None or order == 'newest':
                ranking, rank_params = 'r.created_at DESC, r.ref', []
            else:
                ranking = '''CASE WHEN r.ref IN (SELECT ref FROM {t} WHERE {t} MATCH ? AND namespace=?) THEN 0
                              WHEN r.ref IN (SELECT ref FROM {t} WHERE {t} MATCH ? AND namespace=?) THEN 1 ELSE 2 END,
                         r.revision DESC, r.ref'''.format(t=table)
                rank_params = [f'title : ({expression})', ns, f'summary : ({expression})', ns]
            rows = db.execute(f'SELECT {columns},{snippet} AS snippet FROM {source} WHERE ' + ' AND '.join(where)
                              + f' ORDER BY {ranking} LIMIT ?', (*params, *rank_params, limit)).fetchall()
            hits = []
            for row in rows:
                try:
                    derived = derive_metadata(loads(row['metadata']))
                except (ValueError, TypeError, RecursionError):
                    derived = {'created_at': None, 'started_at': None, 'duration_s': None}
                text = row['snippet']
                if expression is None and state == 'ready':  # browse has no match to excerpt: the start of the summary or transcript
                    found = db.execute("SELECT substr(COALESCE(NULLIF(summary,''),transcript),1,320) FROM texts4 WHERE rowid=?", (row['rid'],)).fetchone()
                    text = found[0] if found else ''
                labels = [label[0] for label in db.execute('SELECT display FROM speakers WHERE ref=? ORDER BY label LIMIT ?', (row['ref'], HIT_SPEAKERS))]
                hits.append({'recording_ref': row['ref'], 'provider_recording_id': row['provider_id'], 'revision': row['revision'],
                             'title': row['title'], 'snippet': text, 'parts': loads(row['evidence']), **derived, 'speakers': labels})
            return hits

    def context(self, account, ref):
        with self.connection() as db:
            row = db.execute('SELECT * FROM records WHERE namespace=? AND ref=?', (namespace(account), ref)).fetchone()
            if row is None:
                raise Failure('not_found_or_forbidden', 403)
            return self.decoded(row)

    def feed_sync(self, view, now=None):
        """Start or repair the feed identity and record the access changes the configuration made (one write, only if due)."""
        now = self.clock() if now is None else now
        with self.connection() as db:
            due = archive_feed.sync_needed(db, self.directory, view, now, self.feed_handled)
        if due:
            self.write(lambda db: archive_feed.sync(db, self.directory, view, now, self.feed_handled))
            self.feed_handled.clear()  # committed: the marker now speaks for this database

    def feed_page(self, view, after, limit):
        now = self.clock()
        self.feed_sync(view, now)
        with self.connection() as db:
            db.execute('BEGIN')
            return archive_feed.page(db, self.directory, view, after, limit, now)

    def feed_head(self, view):
        now = self.clock()
        self.feed_sync(view, now)
        with self.connection() as db:
            db.execute('BEGIN')
            return archive_feed.head_page(db, self.directory, now)

    def events(self, after, limit):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM events WHERE sequence>? ORDER BY sequence LIMIT ?', (after, limit))]

    @staticmethod
    def sanitize_marked(db):
        """ANYM-730: a deletion started and its rewrite is not finished. Schemas 1-2 have no `meta` table: never marked."""
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone() is None:
            return False
        return db.execute("SELECT 1 FROM meta WHERE key='sanitize_pending'").fetchone() is not None

    def backup(self, destination):
        with self.connection() as db:
            if self.sanitize_marked(db):
                raise Failure('archive_sanitization_pending')  # an early, cheap refusal; the copy is validated below as well
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
            # The guard above and the copy are separate operations and the backup takes no writer lock: a deletion may have
            # committed in between. The copy is one consistent state; it is complete only if that state carries no mark.
            with closing(sqlite3.connect(destination.as_uri() + '?mode=ro', uri=True)) as copy:
                if self.sanitize_marked(copy):
                    raise Failure('archive_sanitization_pending')
        except BaseException:
            destination.unlink()
            raise
        return {'state':'saved'}

    # ---- ANYM-730: owner-requested deletion (operator command, service stopped; the single-writer lock is held by this Store)

    @staticmethod
    def tombstones_exist(db):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tombstones'").fetchone() is not None

    @contextmanager
    def operator_connection(self):
        """A writer for the deletion command: no 2 s progress deadline and no page ceiling (VACUUM needs a copy of the
        file), `secure_delete` on so freed pages are overwritten. The caller holds the single-writer lock."""
        self.files()
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.isolation_level = None
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA secure_delete=ON')
            yield db
        finally:
            db.close()

    def deletion_capacity(self):
        """VACUUM rewrites the file: it needs about the file's size again plus the WAL it may grow, and the usual reserve."""
        used = sum(path.stat().st_size for path in self.files())
        if shutil.disk_usage(self.directory).free < 2 * used + HEADROOM + self.settings.free_reserve_bytes:
            raise Failure('archive_capacity')

    def deletion_targets(self, db, refs, namespaces, created_from, created_to):
        ranged = bool(namespaces) or created_from is not None or created_to is not None
        if refs and ranged:
            raise Failure('deletion_selector_ambiguous')
        if not refs and not ranged:
            raise Failure('deletion_selector_required')
        if refs:
            if any(not isinstance(ref, str) or len(ref) != 32 or any(c not in '0123456789abcdef' for c in ref) for ref in refs):
                raise Failure('deletion_selector_invalid')
            marks = ','.join('?' * len(refs))
            return db.execute(f'SELECT rowid AS rid, ref, namespace, provider_id FROM records WHERE ref IN ({marks}) ORDER BY rowid',
                              tuple(refs)).fetchall()
        if not namespaces or (created_from is None and created_to is None):
            raise Failure('deletion_selector_invalid')
        low = parse_time(created_from) if created_from is not None else None
        high = parse_time(created_to) if created_to is not None else None
        if (created_from is not None and low is None) or (created_to is not None and high is None):
            raise Failure('deletion_selector_invalid')
        marks = ','.join('?' * len(namespaces))
        sql = f'SELECT rowid AS rid, ref, namespace, provider_id FROM records WHERE namespace IN ({marks}) AND created_at IS NOT NULL'
        values = list(namespaces)
        if low is not None:
            sql += ' AND created_at>=?'
            values.append(low)
        if high is not None:
            sql += ' AND created_at<=?'
            values.append(high)
        return db.execute(sql + ' ORDER BY rowid', values).fetchall()

    def delete_records(self, *, refs=None, namespaces=None, created_from=None, created_to=None, apply=False, snapshot_to=None):
        """Remove the selected recordings (rows, index entries, speaker labels, journal events), record a tombstone for each
        provider ID, rewrite the file so the bytes are gone (FTS5 rebuild, `secure_delete`, VACUUM, WAL truncated), and
        write the post-deletion snapshot (`snapshot_to`, required with `apply`). Preview unless `apply`. The receipt holds counts and identity only."""
        try:
            return self.delete_unchecked(refs, namespaces, created_from, created_to, apply, snapshot_to)
        except sqlite3.Error as error:
            full = getattr(error, 'sqlite_errorcode', 0) & 255 == sqlite3.SQLITE_FULL
            raise Failure('archive_capacity' if full else 'archive_unavailable') from None
        except (OSError, ValueError, TypeError, KeyError):
            raise Failure('archive_unavailable') from None

    def delete_unchecked(self, refs, namespaces, created_from, created_to, apply, snapshot_to):
        with self.operator_connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION or not self.tombstones_exist(db):
                raise Failure('archive_index_building')
            state, _ = self.index_mode(db)
            if state != 'ready' or db.execute("SELECT 1 FROM sqlite_master WHERE name='texts'").fetchone() is not None:
                raise Failure('archive_index_building')
            if db.execute("SELECT 1 FROM meta WHERE key='feed_epochs'").fetchone() is not None or (
                    self.directory / archive_feed.MARKER).exists():
                raise Failure('deletion_feed_unsupported')  # the event feed owns the journal identity (ANYM-726); not yet supported
            targets = self.deletion_targets(db, refs, namespaces, created_from, created_to)
            if apply and snapshot_to is None:
                raise Failure('deletion_snapshot_required')
            receipt = {'v': 1, 'applied': False, 'selected': len(targets), 'deleted': 0, 'batches': 0,
                       'tombstones': db.execute('SELECT count(*) FROM tombstones').fetchone()[0], 'tombstones_added': 0,
                       'sanitized': False, 'incarnation': int(db.execute("SELECT value FROM meta WHERE key='incarnation'").fetchone()[0]),
                       'snapshot': None, 'backup_replacement': 'not_needed'}
            if not apply:
                return receipt
            # With --yes the rewrite below always runs, also for a selection that is already gone: it finishes a run that
            # a crash stopped between the last batch and the VACUUM.
            self.deletion_capacity()
            added = 0
            for start in range(0, len(targets), DELETE_BATCH):
                chunk = targets[start:start + DELETE_BATCH]
                db.execute('BEGIN IMMEDIATE')
                try:
                    if receipt['batches'] == 0:
                        # A bundle sealed before the deletion carries the old incarnation: the verifier refuses it
                        # (`archive_incarnation_lower`), so a pending pre-deletion bundle is cancelled by this one write.
                        db.execute("UPDATE meta SET value=value+1 WHERE key='incarnation'")
                        # Durable "the rewrite is not finished": deleted text may still sit in free pages and FTS5 storage, so
                        # no snapshot is taken (`backup`) until the rewrite below has cleared it (`secure_delete` alone does not).
                        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('sanitize_pending',1)")
                    for row in chunk:
                        added += db.execute('INSERT OR IGNORE INTO tombstones(provider_hash) VALUES(?)',
                                            (provider_hash(row['namespace'], row['provider_id']),)).rowcount
                        db.execute('DELETE FROM texts4 WHERE rowid=?', (row['rid'],))
                        for table in ('speakers', 'index_rows', 'index_skips', 'index_dirty', 'events'):
                            db.execute(f'DELETE FROM {table} WHERE ref=?', (row['ref'],))
                        db.execute('DELETE FROM records WHERE ref=?', (row['ref'],))
                    db.execute("UPDATE accounts SET text_bytes=(SELECT COALESCE(SUM("
                               "COALESCE(length(CAST(json_extract(parts,'$.transcript.text') AS BLOB)),0)"
                               "+COALESCE(length(CAST(json_extract(parts,'$.summary.text') AS BLOB)),0)),0) FROM records "
                               "WHERE records.namespace=accounts.namespace)")
                    db.execute('COMMIT')
                except BaseException:
                    db.execute('ROLLBACK')
                    raise
                receipt['batches'] += 1
                receipt['deleted'] += len(chunk)
            # Sanitization: the index is rebuilt and merged, then the file is rewritten so no freed page keeps text. Every
            # check must hold before the durable mark is cleared; otherwise the command fails and a repeat finishes it.
            db.execute("INSERT INTO texts4(texts4) VALUES('rebuild')")
            db.execute("INSERT INTO texts4(texts4) VALUES('optimize')")
            db.execute('VACUUM')
            busy = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]
            wal = Path(str(self.path) + '-wal')
            clean = (busy == 0 and db.execute('PRAGMA freelist_count').fetchone()[0] == 0 and
                     db.execute('PRAGMA quick_check').fetchone()[0] == 'ok' and (not wal.exists() or wal.stat().st_size == 0))
            if not clean:
                raise Failure('deletion_sanitization_incomplete')  # a pinned reader, a busy checkpoint: nothing is snapshotted
            db.execute("DELETE FROM meta WHERE key='sanitize_pending'")
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')  # the mark's own change only; no text is left to reach the WAL
            receipt.update(applied=True, sanitized=True, tombstones_added=added, backup_replacement='backup_replacement_pending',
                           tombstones=db.execute('SELECT count(*) FROM tombstones').fetchone()[0],
                           incarnation=int(db.execute("SELECT value FROM meta WHERE key='incarnation'").fetchone()[0]))
        self.backup(snapshot_to)
        with closing(sqlite3.connect(Path(snapshot_to).as_uri() + '?mode=ro', uri=True)) as snapshot:
            receipt['snapshot'] = self.snapshot_identity(snapshot)  # the identity of the file just written, not of the live archive
        return receipt

    @staticmethod
    def snapshot_identity(db):
        """The archive's identity counters (the shape the verifier's proof carries); no title, no text."""
        meta = {row[0]: row[1] for row in db.execute("SELECT key,value FROM meta WHERE key IN ('archive_id','incarnation')")}
        return {'archive_id': meta['archive_id'], 'incarnation': int(meta['incarnation']),
                'sequence': db.execute('SELECT COALESCE(MAX(sequence),0) FROM events').fetchone()[0],
                'records': db.execute('SELECT count(*) FROM records').fetchone()[0]}

    def lift_tombstone(self, ns, provider_id, apply=False):
        """Lift one tombstone so the recording may be archived again. The next backup then needs a reasoned re-baseline
        (the continuity rule `archive_tombstone_dropped`, BACKUP_R2.md)."""
        try:
            with self.operator_connection() as db:
                if not self.tombstones_exist(db):
                    raise Failure('archive_index_building')
                found = db.execute('SELECT count(*) FROM tombstones WHERE provider_hash=?', (provider_hash(ns, provider_id),)).fetchone()[0]
                if apply and found:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('DELETE FROM tombstones WHERE provider_hash=?', (provider_hash(ns, provider_id),))
                    db.execute('COMMIT')
                return {'v': 1, 'lifted': found if apply else 0, 'applied': bool(apply),
                        'tombstones': db.execute('SELECT count(*) FROM tombstones').fetchone()[0]}
        except sqlite3.Error:
            raise Failure('archive_unavailable') from None
