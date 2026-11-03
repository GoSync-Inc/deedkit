"""Recordings continuity (ANYM-728): did a valid candidate archive lose content the protected point had?

`integrity_check` cannot tell: a restarted recordings service recreates a missing archive with a valid schema, so an
empty or partly refilled archive is a healthy SQLite file. The restore helper therefore reduces the decrypted
candidate to hashes (`archive_facts`), the verifier keeps the same reduction of the protected point as its baseline
and `judge` compares them with zero tolerance. Facts hold refs, revisions and SHA-256 values; never a word of text.

Tombstone contract (ANYM-730 writes it): from schema 5 the archive has `tombstones(provider_hash TEXT PRIMARY KEY)`,
`provider_hash` being `provider_hash(namespace, provider_id)`. A tombstone present in the candidate and absent from
the baseline is the only thing that permits a baseline record to be missing; there is no separate token.
"""
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

from .backup_recordings import APPLICATION_ID, ARCHIVE_ID, MAX_INT, inspect_archive

KINDS = ('transcript', 'summary')
REF = ARCHIVE_ID
HASH = re.compile('[a-f0-9]{64}')
SYNTHETIC_ARCHIVE_ID = 'arch-0123456789abcdef'
# The refusal codes (verifier failure_code safe set); they name no ref and no content.
REFUSALS = ('archive_identity_changed', 'archive_incarnation_lower', 'archive_sequence_lower', 'archive_content_lost',
            'archive_tombstone_dropped', 'archive_baseline_missing')
ARCHIVE_KEYS = {'archive_id', 'incarnation', 'sequence', 'records'}
FACT_KEYS = {'v', 'archive', 'parts', 'providers', 'tombstones'}


class ContinuityRefused(RuntimeError):
    """The candidate lost something the baseline had; the protected point stays."""
    def __init__(self, code):
        if code not in REFUSALS:
            raise ValueError('invalid_refusal_code')
        super().__init__(code)
        self.code = code


def provider_hash(namespace, provider_id):
    return hashlib.sha256((namespace + '\x00' + provider_id).encode('utf-8', 'surrogatepass')).hexdigest()


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()


def _connect(path):
    return sqlite3.connect('file:' + quote(str(path)) + '?mode=ro&immutable=1', uri=True)


def archive_facts(path):
    """The hash-only reduction of an archive file (helper side). Identity, schema and integrity come from
    `inspect_archive`; the parts are the accepted non-empty texts, hashed here from the text itself."""
    base = inspect_archive(path)
    parts, providers, tombstones = [], [], []
    try:
        db = _connect(path)
        try:
            for ref, namespace, provider_id, revision, raw in db.execute(
                    'SELECT ref,namespace,provider_id,revision,parts FROM records ORDER BY ref'):
                providers.append([ref, provider_hash(namespace, provider_id)])
                try:
                    stored = json.loads(raw)
                except (TypeError, ValueError, RecursionError):
                    stored = None
                for kind in KINDS:
                    part = stored.get(kind) if isinstance(stored, dict) else None
                    text = part.get('text') if isinstance(part, dict) else None
                    if isinstance(text, str) and text:
                        parts.append([ref, kind, revision, text_hash(text)])
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='tombstones' AND type='table'").fetchone():
                tombstones = sorted(row[0] for row in db.execute('SELECT provider_hash FROM tombstones'))
        finally:
            db.close()
    except (sqlite3.Error, TypeError, AttributeError):
        raise ValueError('invalid_continuity') from None
    return validate_facts({'v': 1, 'archive': base['archive'], 'parts': parts, 'providers': providers,
                           'tombstones': tombstones})


def _count(value, low=0):
    return type(value) is int and low <= value <= MAX_INT


def validate_facts(value):
    """Facts from the helper (or the baseline file) in exactly this shape, or ValueError."""
    def bad():
        raise ValueError('invalid_continuity')
    if not isinstance(value, dict) or set(value) != FACT_KEYS or type(value['v']) is not int or value['v'] != 1:
        bad()
    archive = value['archive']
    if (not isinstance(archive, dict) or set(archive) != ARCHIVE_KEYS or not isinstance(archive['archive_id'], str) or
            not ARCHIVE_ID.fullmatch(archive['archive_id']) or not _count(archive['incarnation'], 1) or
            not _count(archive['sequence']) or not _count(archive['records'])):
        bad()
    seen = set()
    if not isinstance(value['parts'], list):
        bad()
    for item in value['parts']:
        if (not isinstance(item, list) or len(item) != 4 or not isinstance(item[0], str) or not REF.fullmatch(item[0]) or
                item[1] not in KINDS or not _count(item[2]) or not isinstance(item[3], str) or not HASH.fullmatch(item[3]) or
                (item[0], item[1]) in seen):
            bad()
        seen.add((item[0], item[1]))
    if not isinstance(value['providers'], list) or any(
            not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str) or not REF.fullmatch(item[0]) or
            not isinstance(item[1], str) or not HASH.fullmatch(item[1]) for item in value['providers']):
        bad()
    if not isinstance(value['tombstones'], list) or any(
            not isinstance(item, str) or not HASH.fullmatch(item) for item in value['tombstones']):
        bad()
    return value


def judge(baseline, candidate):
    """Raise ContinuityRefused when `candidate` lost anything of `baseline`; both are validated facts."""
    old, new = baseline['archive'], candidate['archive']
    if old['archive_id'] != new['archive_id']:
        raise ContinuityRefused('archive_identity_changed')
    if new['incarnation'] < old['incarnation']:
        raise ContinuityRefused('archive_incarnation_lower')
    if new['incarnation'] == old['incarnation'] and new['sequence'] < old['sequence']:
        raise ContinuityRefused('archive_sequence_lower')
    kept = set(baseline['tombstones'])
    now = set(candidate['tombstones'])
    if not kept <= now:
        raise ContinuityRefused('archive_tombstone_dropped')
    authorized = now - kept
    provider = {ref: digest for ref, digest in baseline['providers']}
    parts = {(ref, kind): (revision, digest) for ref, kind, revision, digest in candidate['parts']}
    for ref, kind, revision, digest in baseline['parts']:
        if provider.get(ref) in authorized:
            continue
        found = parts.get((ref, kind))
        if found is None or found[0] < revision or (found[0] == revision and found[1] != digest):
            raise ContinuityRefused('archive_content_lost')


def readback(path):
    """A search and a context read on the decrypted archive: the FTS index answers a synthetic query and the newest
    records' stored parts read back as JSON text. Nothing read is kept or returned."""
    try:
        db = _connect(path)
        try:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            table = 'texts4' if version >= 4 else 'texts'
            db.execute('SELECT count(*) FROM ' + table + ' WHERE ' + table + ' MATCH ?', ('deedkitsyntheticprobe',)).fetchone()
            for (raw,) in db.execute('SELECT parts FROM records ORDER BY rowid DESC LIMIT 3'):
                stored = json.loads(raw)
                if not isinstance(stored, dict) or any(
                        not isinstance(stored[kind].get('text', ''), str) for kind in KINDS if isinstance(stored.get(kind), dict)):
                    raise ValueError()
        finally:
            db.close()
    except (sqlite3.Error, TypeError, ValueError, RecursionError):
        raise ValueError('readback_failed') from None


def synthetic_archive(path, records=None, *, archive_id=SYNTHETIC_ARCHIVE_ID, incarnation=1, user_version=None,
                      tombstones=(), events=None):
    """A synthetic archive in the recordings store's table shapes (tests and the follow fixture; no provider data).

    `records`: dicts with ref, provider_id and optional namespace, revision, transcript, summary. `events` is the
    highest journal sequence (default one per record)."""
    records = records or []
    user_version = (5 if tombstones else 4) if user_version is None else user_version
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.executescript(
        'CREATE TABLE records(ref TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider_id TEXT NOT NULL, metadata TEXT NOT NULL,'
        ' parts TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, UNIQUE(namespace, provider_id));'
        'CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT);'
        'CREATE TABLE meta(key TEXT PRIMARY KEY, value);'
        "CREATE VIRTUAL TABLE texts4 USING fts5(ref UNINDEXED, namespace UNINDEXED, title, transcript, summary, tokenize='unicode61');")
    if user_version >= 5:
        db.execute('CREATE TABLE tombstones(provider_hash TEXT PRIMARY KEY)')
        db.executemany('INSERT INTO tombstones VALUES(?)', [(item,) for item in tombstones])
    for item in records:
        namespace = item.get('namespace', 'omi:synthetic')
        parts = {kind: {'text': item.get(kind, ''), 'digest': text_hash(item.get(kind, ''))} for kind in KINDS}
        db.execute('INSERT INTO records(ref,namespace,provider_id,metadata,parts,revision) VALUES(?,?,?,?,?,?)',
                   (item['ref'], namespace, item['provider_id'], '{}', json.dumps(parts), item.get('revision', 1)))
        db.execute('INSERT INTO texts4(ref,namespace,title,transcript,summary) VALUES(?,?,?,?,?)',
                   (item['ref'], namespace, '', item.get('transcript', ''), item.get('summary', '')))
    db.executemany('INSERT INTO events(ref) VALUES(?)', [(None,)] * (len(records) if events is None else events))
    db.executemany('INSERT INTO meta VALUES(?,?)', [('archive_id', archive_id), ('incarnation', incarnation),
                                                    ('journal_id', 'journal-synthetic')])
    db.commit()
    db.execute(f'PRAGMA application_id={APPLICATION_ID}')
    db.execute(f'PRAGMA user_version={user_version}')
    db.commit()
    db.close()
    return path
