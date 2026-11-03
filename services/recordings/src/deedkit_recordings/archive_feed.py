"""Content-free event feed for the module event router (ANYM-726). References only; no provider, model or network call.

Feed `seq` = incarnation x 10^9 + the offset inside the incarnation's epoch. An epoch is a run of local journal numbers
(`events.sequence`) that share one incarnation; the epochs are listed in `meta.feed_epochs` as `[[first_sequence, incarnation], ...]`
(a key/value row: schema 4 stays, schema 5 belongs to ANYM-730). An event keeps the identity it had when it was first
served, a later bump only gives later events a larger base. A marker file on the archive volume, outside the SQLite file,
holds `{incarnation, high_water}`; it is written (and fsynced) only when the feed is served or maintained, before the data it
covers leaves the process, never inside the transaction that stores recorded text. A restored older database, a lost or
damaged marker, a marker from a newer incarnation, or a reached offset cap starts a new epoch (`needs_bump`), and the feed
stays unavailable (`feed_not_ready`) until the wall clock has reached the new incarnation's minute: two restarts inside one
minute can therefore never hand out the same incarnation twice after anything was published.
"""
import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone

from .security import Failure, loads

EPOCH_ZERO = 1767225600  # 2026-01-01T00:00:00Z: incarnations count minutes from here
BASE = 10 ** 9
LOCAL_CAP = BASE  # a module attribute so a test can lower it
MAX_SEQ = 2 ** 53 - 1
MARKER = 'journal.marker'
SCAN = 500
MAX_LIMIT = 200
REF = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
TEXT_REF = re.compile(r'^[0-9a-f]{32}$')
REASONS = ('permission_removed', 'account_removed', 'namespace_replaced')


def minutes(now):
    return int((now - EPOCH_ZERO) // 60)


def capacity(incarnation):
    """The largest offset an epoch of this incarnation may reach: below 10^9 and with the whole seq at most 2^53-1."""
    return min(LOCAL_CAP - 1, MAX_SEQ - incarnation * BASE)


def safe_ref(value):
    """The identifier itself when it is a valid reference (`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`), else `h-` and the first 24
    hex characters of its SHA-256. This encoding is the contract for every consumer that keys on these references."""
    if isinstance(value, str) and REF.fullmatch(value):
        return value
    return 'h-' + hashlib.sha256(str(value).encode()).hexdigest()[:24]


def rfc3339(moment):
    return datetime.fromtimestamp(moment, tz=timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def meta_get(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return default if row is None else row[0]


def meta_set(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (key, value))


def head_sequence(db):
    row = db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
    return row[0] if row else 0


def read_epochs(db):
    raw = meta_get(db, 'feed_epochs')
    if raw is None:
        return []
    try:
        value = loads(raw)
        if not isinstance(value, list) or not all(
                isinstance(row, list) and len(row) == 2 and all(type(item) is int and item >= 1 for item in row) for row in value):
            raise ValueError
        for before, after in zip(value, value[1:]):
            if not (before[0] < after[0] and before[1] < after[1]):
                raise ValueError
        return value
    except (ValueError, TypeError, RecursionError):
        raise Failure('archive_unavailable') from None


def seq_of(epochs, sequence):
    """The feed seq of a local journal number; None before the first epoch starts."""
    found = None
    for start, incarnation in epochs:
        if start <= sequence:
            found = (start, incarnation)
    return None if found is None else found[1] * BASE + (sequence - found[0] + 1)


def head_of(epochs, head):
    if not epochs:
        return 0
    start, incarnation = epochs[-1]
    return incarnation * BASE + max(0, head - start + 1)


def local_after(epochs, after, head):
    """The local journal number a cursor `after` (a feed seq) stands for: rows above it are the ones to serve."""
    incarnation, offset = divmod(after, BASE)
    for index, (start, known) in enumerate(epochs):
        if known == incarnation:
            local = start - 1 if offset == 0 else start + offset - 1
            if index + 1 < len(epochs):
                local = min(local, epochs[index + 1][0] - 1)
            return local
        if known > incarnation:
            return start - 1
    return head  # ahead of everything this journal knows: nothing to serve


def read_marker(directory):
    try:
        fd = os.open(directory / MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
            return None
        value = loads(os.read(fd, 4097))
        if (isinstance(value, dict) and set(value) == {'incarnation', 'high_water'}
                and all(type(value[key]) is int and value[key] >= 0 for key in value)):
            return value
    except (OSError, ValueError, TypeError, RecursionError):
        pass
    finally:
        os.close(fd)
    return None


def write_marker(directory, incarnation, high_water):
    """Write, fsync, rename and fsync the directory."""
    temporary = directory / (MARKER + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, json.dumps({'incarnation': incarnation, 'high_water': high_water}, separators=(',', ':')).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, directory / MARKER)
    folder = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


def inspect(db, directory, now, handled=None):
    """The journal identity's state. Reads only (the marker file is read, never written). Raises `sequence_space_exhausted`
    when the current epoch can hold no further offset and no incarnation is left."""
    stored = int(meta_get(db, 'incarnation', 1) or 1)
    epochs = read_epochs(db)
    head = head_sequence(db)
    marker = read_marker(directory)
    last = epochs[-1][1] if epochs else 0
    # `handled` is process memory, never the database: a bump made inside a text write remembers the marker it reacted to, so
    # the next rows of the same run do not bump again for the same marker. A new process, or a different marker (the volume was
    # restored again), judges afresh, so nothing stored in a restored database can suppress a needed bump.
    unverified = bool(handled) and 'marker' in handled
    capped = bool(epochs) and head - epochs[-1][0] + 1 >= capacity(last)
    if epochs and not capped and capacity(last) < 1:
        raise Failure('sequence_space_exhausted')
    # Identity problems that need a new epoch whatever the marker says: first use, a database whose incarnation and epoch
    # list disagree, an epoch at its cap. Marker problems (lost, damaged, ahead of the database in rows or incarnation) need
    # one too, unless a bump already reacted to this very marker inside a text write and only its marker is still to be written.
    if unverified:
        marker_stale = marker != handled['marker']
    else:
        marker_stale = marker is None or marker['high_water'] > head or marker['incarnation'] > stored
    bump_now = not epochs or stored != last or capped or marker_stale
    new = max(stored, last, marker['incarnation'] if marker else 0, minutes(now)) + 1 if bump_now else None
    return {'epochs': epochs, 'head': head, 'marker': marker, 'bump': bump_now, 'new': new, 'unverified': unverified, 'last': last}


def bump(db, new):
    """Start a new epoch (database rows only; no file I/O). The first start partitions an existing backlog so that no epoch
    holds more than `capacity` rows. The marker is written later, by `sync`."""
    epochs, head = read_epochs(db), head_sequence(db)
    if not epochs:
        start, incarnation = 1, new
        while True:
            if capacity(incarnation) < 1:
                raise Failure('sequence_space_exhausted')
            epochs.append([start, incarnation])
            if head - start + 1 <= capacity(incarnation):
                break
            start, incarnation = start + capacity(incarnation), incarnation + 1
    else:
        start = head + 1
        if capacity(new) < 1:
            raise Failure('sequence_space_exhausted')
        if epochs[-1][0] == start:
            epochs[-1][1] = new  # nothing was written under the last epoch yet: nothing was served from it
        else:
            epochs.append([start, new])
    meta_set(db, 'feed_epochs', json.dumps(epochs, separators=(',', ':')))
    meta_set(db, 'incarnation', epochs[-1][1])


def prepare(db, directory, now, handled):
    """Inside the transaction that is about to insert a journal row, while the feed is enabled: when the identity is stale
    the new epoch starts BEFORE the row, so the row can never take an identity the router may already have consumed. Database
    work only; the marker file is not written here, so a marker failure can never stop text from being archived. An
    exhausted sequence space is not an error here: collection goes on and the feed refuses to serve."""
    try:
        state = inspect(db, directory, now, handled)
        if state['bump']:
            bump(db, state['new'])
            handled['marker'] = state['marker']
    except Failure:
        return


def access_view(doc, namespace):
    """What the access-change events compare: per principal its read/content entitlement and, per account in its ceiling,
    the account's namespace (None when the account has no archive). Plus the namespace -> account map for text events."""
    namespaces = {account.id: namespace(account) if account.archive_namespace else None for account in doc.accounts}
    principals = {}
    for actor in doc.principals:
        principals[actor.id] = {'read': 'recordings:read' in actor.permissions, 'content': 'recordings:content' in actor.permissions,
                                'accounts': {name: namespaces[name] for name in actor.accounts}}
    return {'principals': principals, 'namespaces': {value: key for key, value in namespaces.items() if value}}


def changes(old, new):
    """(reason, principal id, account id, old namespace) for every entitlement that existed and no longer does."""
    result = []
    for name, before in old.items():
        now = new.get(name)
        for account, namespace in before['accounts'].items():
            if namespace is None or not before['read']:
                continue  # no scope was ever shown for it
            if now is None or not now['read'] or (before['content'] and not now['content']):
                reason = 'permission_removed'
            elif account not in now['accounts']:
                reason = 'account_removed'
            elif now['accounts'][account] != namespace:
                reason = 'namespace_replaced'
            else:
                continue
            result.append((reason, name, account, namespace))
    return result


def sync_needed(db, directory, view, now, handled):
    state = inspect(db, directory, now, handled)
    desired = state['marker'] is not None and state['marker']['high_water'] == state['head'] and state['marker']['incarnation'] == state['last']
    stored = meta_get(db, 'access_view')
    return state['bump'] or state['unverified'] or not desired or stored is None or loads(stored) != view['principals']


def sync(db, directory, view, now, handled):
    """One write transaction: the identity bump (if due), the access-change events for what the configuration removed since
    the stored view, and then the marker (written and fsynced before this transaction commits and before any page covering
    these rows is served). Idempotent: the view is stored in the same transaction as its events."""
    state = inspect(db, directory, now, handled)
    if state['bump']:
        bump(db, state['new'])
    stored = meta_get(db, 'access_view')
    try:
        old = loads(stored) if stored is not None else None
    except (ValueError, RecursionError):
        old = None  # unreadable: re-baseline without events rather than guess
    if old is not None:
        for reason, name, account, namespace in changes(old, view['principals']):
            ref = '|'.join(('access', reason, safe_ref(name), account))
            db.execute('INSERT INTO events(event_id,namespace,ref,revision,occurred) VALUES(?,?,?,?,?)',
                       (hashlib.sha256(f'{reason}|{name}|{account}|{namespace}|{now}'.encode()).hexdigest()[:32] + os.urandom(2).hex(),
                        namespace, ref, 0, now))
    meta_set(db, 'access_view', json.dumps(view['principals'], sort_keys=True, separators=(',', ':')))
    epochs = read_epochs(db)
    write_marker(directory, epochs[-1][1], head_sequence(db))
    handled.clear()  # the marker now speaks for this database


def ready(epochs, now):
    """False until the clock has reached the current incarnation's minute (see the module docstring)."""
    return bool(epochs) and minutes(now) >= epochs[-1][1]


def envelope(row, seq, namespaces):
    """The event for one journal row, or None when it is not served (a text event of an account that is gone or whose
    namespace was replaced)."""
    ns, ref = row['namespace'], row['ref']
    if TEXT_REF.fullmatch(ref):
        account = namespaces.get(ns)
        if account is None:
            return None
        return {'specversion': '1.0', 'id': f'recordings:{seq}', 'source': 'deedkit:recordings', 'type': 'recording.text.archived',
                'subject': ref, 'time': rfc3339(row['occurred']), 'urgency': 'batch', 'expires_at': None,
                'scope_ref': f'recordings:{account}.{ns[:16]}',
                'data': {'recording_ref': ref, 'account_ref': safe_ref(account), 'revision': row['revision']}}
    parts = ref.split('|')
    if len(parts) == 4 and parts[0] == 'access' and parts[1] in REASONS:
        _, reason, principal, account = parts
        return {'specversion': '1.0', 'id': f'recordings:{seq}', 'source': 'deedkit:recordings', 'type': 'recording.access.changed',
                'subject': safe_ref(account), 'time': rfc3339(row['occurred']), 'urgency': 'now', 'expires_at': None,
                'scope_ref': f'recordings:{account}.{ns[:16]}',
                'data': {'account_ref': safe_ref(account), 'principal_ref': principal, 'reason': reason}}
    return None


def page(db, view, after, limit, now):
    epochs = read_epochs(db)
    if not ready(epochs, now):
        raise Failure('feed_not_ready')
    head = head_sequence(db)
    rows = db.execute('SELECT sequence,event_id,namespace,ref,revision,occurred FROM events WHERE sequence>? '
                      'ORDER BY sequence LIMIT ?', (local_after(epochs, after, head), max(SCAN, limit * 5))).fetchall()
    events, last_local, last_seq = [], None, None
    for row in rows:
        seq = seq_of(epochs, row['sequence'])
        last_local, last_seq = row['sequence'], seq
        built = envelope(row, seq, view['namespaces'])
        if built is not None:
            events.append(built)
            if len(events) == limit:
                break
    more = last_local is not None and db.execute('SELECT 1 FROM events WHERE sequence>? LIMIT 1', (last_local,)).fetchone() is not None
    floor = meta_get(db, 'feed_retention_floor', 0)
    return {'events': events, 'next_after': max(after, last_seq) if last_seq is not None else after, 'has_more': more,
            'retention_floor': floor if type(floor) is int else 0, 'journal_id': meta_get(db, 'journal_id')}


def head_page(db, now):
    epochs = read_epochs(db)
    if not ready(epochs, now):
        raise Failure('feed_not_ready')
    return {'journal_id': meta_get(db, 'journal_id'), 'head': head_of(epochs, head_sequence(db))}
