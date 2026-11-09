"""ANYM-907: each chat's negotiation (ANYM-893 evaluate) stored with versions that are never reused.

Only two kinds of process write these tables: the one negotiation worker (ANYM-908: cleanup, apply, publish) and the
marks tools (ANYM-910). Every writing transaction first takes the chat's advisory lock, before any row lock;
collectors and the message purge never take it, so they never wait on this code. Several chats: ascending key order.

A message purge (ANYM-510) is unchanged: reads hide a negotiation with a purged source at once (VISIBLE). `apply` and
`clean_if_purged` (before a publish) clean their own chat or negotiation first; the worker's cleanup reaches the rest
from a durable cursor in bounded slices. Cleaning deletes or blanks to ids everything derived from the message.
Stored: states, times, zones and message IDs. No message text is ever written here.
"""
import hashlib
import json
import logging
import os
import secrets
import time
from datetime import timedelta

from . import negotiation_state, scheduling_rules
from .conversation_state import group_attention_sql
from .time_references import owner_zone

log = logging.getLogger(__name__)
TABLES = ('negotiations', 'negotiation_sources', 'negotiation_times', 'negotiation_transitions', 'negotiation_rebuild',
          'negotiation_cursor')
PRIVILEGES = ('SELECT', 'INSERT', 'UPDATE', 'DELETE')
WINDOW = timedelta(days=14)
MAX_SOURCES = 200
BATCH = 200
# Source rows one checkpoint reads at most; the next one goes on from the cursor. A worker's first call reads a lap.
SWEEP = 2000
TIMEOUT = '10s'
# A test hook, called inside the apply transaction with a stage name ('evaluated', 'written'); None in production.
hook = None

# A negotiation read leaves out every negotiation one of whose sources is a purged message (alias n).
VISIBLE = '''NOT EXISTS (SELECT 1 FROM negotiation_sources hidden_s JOIN messages hidden_m ON hidden_m.id=hidden_s.message
    WHERE hidden_s.negotiation=n.id AND hidden_m.purged)'''

SOURCES_SQL = f'''SELECT m.id, m.revision, m.sender, m.direction, m.body, m.sent_at, c.chat_type,
    coalesce({group_attention_sql('m', 'md', 'c')}, false) AS addressed
  FROM messages m JOIN chats c ON c.id=m.chat AND c.account=m.account
  LEFT JOIN message_metadata md ON md.message=m.id
  WHERE m.account=%s AND m.chat=%s AND NOT m.deleted AND NOT m.purged AND m.sent_at>=%s
  ORDER BY m.sent_at DESC, m.id DESC LIMIT %s'''

# Purge discovery reads source rows by primary key from the durable cursor (negotiation_cursor), one bounded
# transaction of `batch` rows each; a message that is gone counts as not purged, as VISIBLE reads it.
SWEEP_SQL = '''SELECT s.message, s.negotiation, n.account, n.chat, coalesce(m.purged, false) AS purged
  FROM negotiation_sources s JOIN negotiations n ON n.id=s.negotiation LEFT JOIN messages m ON m.id=s.message
  WHERE (s.message, s.negotiation) > (%s, %s) ORDER BY s.message, s.negotiation LIMIT %s'''
# A chat's or one negotiation's purged sources, removed ones included: index paths only (negotiation_chat,
# negotiation_source_owner, the messages key).
CHAT_PURGED_SQL = '''SELECT DISTINCT s.negotiation FROM negotiations n JOIN negotiation_sources s ON s.negotiation=n.id
  JOIN messages m ON m.id=s.message WHERE n.account=%s AND n.chat=%s AND m.purged ORDER BY s.negotiation'''
NEGOTIATION_PURGED_SQL = '''SELECT EXISTS (SELECT 1 FROM negotiation_sources s JOIN messages m ON m.id=s.message
  WHERE s.negotiation=%s AND m.purged) AS hit'''


def negotiation_lock_key(account, chat):
    """The chat's advisory lock key: a signed 64-bit number from its account and chat IDs."""
    digest = hashlib.sha256(f'negotiation:{account}\0{chat}'.encode()).digest()
    return int.from_bytes(digest[:8], 'big', signed=True)


def lock_chats(c, chats):
    """Take each chat's lock in ascending key order, so two writers never deadlock."""
    for key in sorted({negotiation_lock_key(account, chat) for account, chat in chats}):
        c.execute('SELECT pg_advisory_xact_lock(%s)', (key,))


def schema_ready(c):
    """Every table present and each privilege this role needs held, each checked alone; never a schema number."""
    for table in TABLES:
        if c.execute('SELECT to_regclass(%s) IS NOT NULL AS ok', (f'public.{table}',)).fetchone()['ok'] is not True:
            return False
        for privilege in PRIVILEGES:
            if c.execute('SELECT has_table_privilege(%s,%s) AS ok', (f'public.{table}', privilege)).fetchone()['ok'] is not True:
                return False
    return True


def _bounded(c):
    c.execute(f"SET LOCAL transaction_timeout='{TIMEOUT}'")
    c.execute(f"SET LOCAL idle_in_transaction_session_timeout='{TIMEOUT}'")


def _stage(name):
    if hook is not None:hook(name)


def _sources(c, account, chat, now, zone):
    rows = c.execute(SOURCES_SQL, (account, chat, now - WINDOW, MAX_SOURCES)).fetchall()
    sources = []
    for r in rows:
        outgoing = r['direction'] == 'outgoing'
        sources.append({'message_id': r['id'], 'revision': r['revision'], 'sender': r['sender'], 'is_owner': outgoing,
                        'sent_at': r['sent_at'], 'chat_type': r['chat_type'] or 'unknown', 'addressed': bool(r['addressed']),
                        'signal': scheduling_rules.signals(r['body'], r['sent_at'], 'out' if outgoing else 'in', zone)})
    return sources


def _fingerprint(result):
    return hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()


def _kind(before, after):
    if after in ('agreed', 'cancelled', 'stale') and before != after:return after
    return 'changed'


def _columns(result):
    agreed, previous = result['agreed_time'] or {}, result['previous_agreed_time'] or {}
    return {'state': result['state'], 'medium': result['medium'], 'confidence': result['confidence'],
            'awaiting': result['awaiting'], 'rescheduled': result['rescheduled'], 'ambiguous': result['ambiguous'],
            'agreed_start': agreed.get('start'), 'agreed_end': agreed.get('end'), 'agreed_zone': agreed.get('zone'),
            'previous_agreed_start': previous.get('start'), 'previous_agreed_end': previous.get('end'),
            'previous_agreed_zone': previous.get('zone'), 'fingerprint': _fingerprint(result),
            'opened_at': result['opened_at'], 'last_source_at': result['last_source_at']}


def _transition(c, negotiation, version, kind, trigger, state, awaiting, now):
    c.execute('''INSERT INTO negotiation_transitions(negotiation,version,kind,trigger_message,state,awaiting,created_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s)''', (negotiation, version, kind, trigger, state, awaiting, now))


def _times(c, negotiation, result):
    c.execute('DELETE FROM negotiation_times WHERE negotiation=%s', (negotiation,))
    entries = [(t, t['status']) for t in result['times']] + [(t, 'declined_only') for t in result['declined_times']]
    for ord_, (t, status) in enumerate(entries):
        c.execute('''INSERT INTO negotiation_times(negotiation,ord,message,status,starts_at,ends_at,granularity,zone,
            zone_source,assumed) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (negotiation, ord_, t['message_id'], status, t['start'], t['end'], t['granularity'], t['zone'],
             t['zone_source'], list(t.get('assumed') or [])))


def _blank_sources(c, negotiation, messages, now):
    """A negotiation's sources that no longer feed it keep only ids: negotiation, message, revision and removed_at.
    The link itself stays, so a purge of the message still finds this negotiation."""
    if messages:
        c.execute('''UPDATE negotiation_sources SET removed_at=%s,intent=NULL,confidence=NULL,medium=NULL,cue_start=NULL,
            cue_end=NULL WHERE negotiation=%s AND message=ANY(%s) AND removed_at IS NULL''', (now, negotiation, list(messages)))


def _tombstone(c, negotiation, now, trigger=None):
    """A content-free `removed` tombstone, its version moved on by one and its transition appended."""
    row = c.execute('''UPDATE negotiations SET state='removed',medium=NULL,confidence=NULL,awaiting=NULL,rescheduled=NULL,
        ambiguous=NULL,agreed_start=NULL,agreed_end=NULL,agreed_zone=NULL,previous_agreed_start=NULL,previous_agreed_end=NULL,
        previous_agreed_zone=NULL,fingerprint=NULL,last_source_at=NULL,version=version+1,updated_at=%s
        WHERE id=%s AND state<>'removed' RETURNING version''', (now, negotiation)).fetchone()
    if row:
        c.execute('DELETE FROM negotiation_times WHERE negotiation=%s', (negotiation,))
        # Every link left keeps only ids: a tombstone has no live source.
        c.execute('''UPDATE negotiation_sources SET removed_at=%s,intent=NULL,confidence=NULL,medium=NULL,cue_start=NULL,
            cue_end=NULL WHERE negotiation=%s AND removed_at IS NULL''', (now, negotiation))
        _transition(c, negotiation, row['version'], 'removed', trigger, 'removed', None, now)


def _write(c, account, chat, now, result, sources):
    signals = {s['message_id']: s for s in sources}
    fetched = set(signals)
    # A message read in the window that carries no scheduling signal any more (edited to plain text) feeds nothing.
    silent = {m for m, s in signals.items() if (s['signal'] or {}).get('intent', 'none') == 'none'}
    stored = c.execute('''SELECT * FROM negotiations WHERE account=%s AND chat=%s AND state<>'removed'
        ORDER BY opened_at DESC, id DESC''', (account, chat)).fetchall()
    rows = c.execute('''SELECT message, negotiation FROM negotiation_sources WHERE negotiation=ANY(%s)
        AND removed_at IS NULL''', ([n['id'] for n in stored],)).fetchall() if stored else []
    active = {}
    for r in rows:active.setdefault(r['negotiation'], set()).add(r['message'])
    unseen = sorted({r['message'] for r in rows} - fetched)
    alive = fetched | ({r['id'] for r in c.execute('''SELECT id FROM messages WHERE id=ANY(%s) AND NOT deleted
        AND NOT purged''', (unseen,)).fetchall()} if unseen else set())
    changed, target, feeding = 0, None, set()
    if result is not None:
        feeding = set(result['sources'])
        target = next((n for n in stored if active.get(n['id'], set()) & feeding), None)
        columns = _columns(result)
        if target is None:
            negotiation = 'neg_' + secrets.token_hex(16)
            c.execute(f'''INSERT INTO negotiations(id,account,chat,version,updated_at,{','.join(columns)})
                VALUES(%s,%s,%s,1,%s,{','.join(['%s'] * len(columns))})''', (negotiation, account, chat, now, *columns.values()))
            _transition(c, negotiation, 1, 'opened', result['trigger_message'], result['state'], result['awaiting'], now)
            _times(c, negotiation, result)
            changed += 1
        else:
            negotiation = target['id']
            if target['fingerprint'] != columns['fingerprint']:
                version = target['version'] + 1
                c.execute(f'''UPDATE negotiations SET {','.join(f'{k}=%s' for k in columns)},version=%s,updated_at=%s
                    WHERE id=%s''', (*columns.values(), version, now, negotiation))
                _transition(c, negotiation, version, _kind(target['state'], result['state']), result['trigger_message'],
                            result['state'], result['awaiting'], now)
                _times(c, negotiation, result)
                changed += 1
        for message in result['sources']:
            s = signals[message]
            cue = s['signal'].get('cue_span') or (None, None)
            c.execute('''INSERT INTO negotiation_sources(message,negotiation,revision,removed_at,intent,confidence,medium,
                cue_start,cue_end) VALUES(%s,%s,%s,NULL,%s,%s,%s,%s,%s) ON CONFLICT (message,negotiation) DO UPDATE SET
                revision=EXCLUDED.revision,removed_at=NULL,intent=EXCLUDED.intent,
                confidence=EXCLUDED.confidence,medium=EXCLUDED.medium,cue_start=EXCLUDED.cue_start,cue_end=EXCLUDED.cue_end''',
                (message, negotiation, s['revision'], s['signal'].get('intent'), s['signal'].get('confidence'),
                 s['signal'].get('medium'), cue[0], cue[1]))
        # The target's sources that no longer feed it: gone from the archive, or read in the window and left out.
        _blank_sources(c, negotiation, {m for m in active.get(negotiation, set()) - feeding
                                        if m not in alive or m in fetched}, now)
    # Every other negotiation is one evaluate no longer folds, so it is never refolded: when it loses a source (its
    # message deleted, no scheduling signal any more, or now feeding the current negotiation), or has none, what it
    # holds is out of date and it becomes removed. Its links stay, blanked to ids (a link never moves to another
    # negotiation, so a purge finds both).
    for n in stored:
        if target is not None and n['id'] == target['id']:continue
        messages = active.get(n['id'], set())
        if not messages or (messages - alive) | (messages & silent) | (messages & feeding):
            _tombstone(c, n['id'], now)
            changed += 1
    return changed


def apply(c, account, chat, now, zone=None):
    """One transaction: the chat lock, its live sources, evaluate, and a new version only when the result changed.
    Returns the number of negotiations that changed. A timeout rolls everything back."""
    zone = zone or owner_zone(os.environ)
    with c.transaction():
        _bounded(c)
        lock_chats(c, [(account, chat)])
        # A purge of this chat's sources that no cleanup has reached yet is cleaned first, under the same lock.
        purged = sum(_purge_one(c, r['negotiation'], now) for r in c.execute(CHAT_PURGED_SQL, (account, chat)).fetchall())
        sources = _sources(c, account, chat, now, zone)
        result = negotiation_state.evaluate(sources, now)
        _stage('evaluated')
        changed = purged + _write(c, account, chat, now, result, sources)
        _stage('written')
    return changed


def _purge_one(c, negotiation, now):
    """Everything derived from a negotiation with a purged source: its sources, times and mark go, its unpublished
    transitions are invalidated, its history blanked to ids, and it becomes the removed tombstone; its chat rebuilds."""
    row = c.execute('SELECT account, chat FROM negotiations WHERE id=%s', (negotiation,)).fetchone()
    if row is None:return False
    c.execute('DELETE FROM negotiation_sources WHERE negotiation=%s', (negotiation,))
    c.execute('DELETE FROM negotiation_times WHERE negotiation=%s', (negotiation,))
    if c.execute("SELECT to_regclass('public.negotiation_marks') IS NOT NULL AS ok").fetchone()['ok']:
        c.execute('DELETE FROM negotiation_marks WHERE negotiation=%s', (negotiation,))  # ANYM-910
    c.execute('''UPDATE negotiation_transitions SET invalidated_at=%s WHERE negotiation=%s AND published_at IS NULL
        AND invalidated_at IS NULL''', (now, negotiation))
    c.execute('''UPDATE negotiation_transitions SET state=NULL,awaiting=NULL,trigger_message=NULL
        WHERE negotiation=%s''', (negotiation,))
    if c.execute("SELECT to_regprocedure('public.scrub_negotiation_events(text)') IS NOT NULL AS ok").fetchone()['ok']:
        c.execute('SELECT public.scrub_negotiation_events(%s)', (negotiation,))  # ANYM-911
    _tombstone(c, negotiation, now)
    c.execute('''INSERT INTO negotiation_rebuild(account,chat,queued_at) VALUES(%s,%s,%s)
        ON CONFLICT (account,chat) DO NOTHING''', (row['account'], row['chat'], now))
    return True


def cleanup(c, now, batch=BATCH, budget=SWEEP):
    """Find every source row whose message is purged, removed sources included, and clean its negotiation.

    Discovery reads source rows by primary key from the durable cursor, `batch` rows per bounded transaction and at most
    `budget` rows a call (None: one lap over every row present at the start); at the end it wraps to the start. Each
    negotiation hit is cleaned in its own bounded transaction under its chat's lock, and only then does the cursor move
    on, so a stop resumes where it left off. `apply` and `clean_if_purged` clean their own chat or negotiation exactly,
    whatever the cursor. Returns the count; logs only the count."""
    total, scanned = 0, 0
    if budget is None:
        with c.transaction():
            _bounded(c)
            budget = c.execute('SELECT count(*) AS n FROM negotiation_sources').fetchone()['n']
    while scanned < budget:
        with c.transaction():
            _bounded(c)
            row = c.execute('SELECT purge_message, purge_negotiation FROM negotiation_cursor WHERE id=1').fetchone()
            after = (row['purge_message'], row['purge_negotiation']) if row else ('', '')
            chunk = c.execute(SWEEP_SQL, (*after, batch)).fetchall()
        hits = {(r['account'], r['chat'], r['negotiation']) for r in chunk if r['purged']}
        for account, chat, negotiation in sorted(hits, key=lambda h: (negotiation_lock_key(h[0], h[1]), h[2])):
            with c.transaction():
                _bounded(c)
                lock_chats(c, [(account, chat)])
                total += _purge_one(c, negotiation, now)
        last = (chunk[-1]['message'], chunk[-1]['negotiation']) if len(chunk) == batch else ('', '')
        with c.transaction():
            _bounded(c)
            c.execute('''INSERT INTO negotiation_cursor(id,updated_at,purge_message,purge_negotiation) VALUES(1,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET purge_message=EXCLUDED.purge_message,
                purge_negotiation=EXCLUDED.purge_negotiation''', (now, *last))
        scanned += len(chunk)
        if not chunk and after == ('', ''):break  # nothing stored
    if total:log.info('negotiations_purged count=%d', total)
    return total


def clean_if_purged(c, negotiation, now):
    """Before a publish (ANYM-908), inside the caller's transaction: when a source of the negotiation is purged, clean
    it under its chat's lock and return True (nothing of it may publish); else False."""
    row = c.execute('SELECT account, chat FROM negotiations WHERE id=%s', (negotiation,)).fetchone()
    if row is None:return False
    lock_chats(c, [(row['account'], row['chat'])])
    if not c.execute(NEGOTIATION_PURGED_SQL, (negotiation,)).fetchone()['hit']:return False
    return _purge_one(c, negotiation, now)


class Checkpoint:
    """The worker's cleanup checkpoint: run before each apply and publish transaction and between backoffs, at most
    once per interval (one 10 s apply plus 1 s bounds the time between checks while the worker runs). The first call
    of a started worker reads a whole lap; later calls go on from the cursor, at most SWEEP rows each."""

    def __init__(self, interval=1.0, clock=time.monotonic):
        self.interval, self.clock, self.last = interval, clock, None

    def __call__(self, c, now):
        at = self.clock()
        if self.last is not None and at - self.last < self.interval:return 0
        # The time counts only after a cleanup that finished: a failed first call keeps the full lap for its retry.
        count = cleanup(c, now, budget=None if self.last is None else SWEEP)
        self.last = at
        return count


def take_rebuild(c, limit=BATCH):
    """The queued chats, oldest first, removed from the queue in the caller's transaction."""
    return [(r['account'], r['chat']) for r in c.execute('''DELETE FROM negotiation_rebuild WHERE (account,chat) IN (
        SELECT account,chat FROM negotiation_rebuild ORDER BY queued_at,account,chat LIMIT %s FOR UPDATE SKIP LOCKED)
        RETURNING account,chat,queued_at''', (limit,)).fetchall()]


def visible(c, account, chat):
    """A chat's negotiations as a read shows them: no removed tombstone and none with a purged source."""
    return c.execute(f'''SELECT * FROM negotiations n WHERE account=%s AND chat=%s AND state<>'removed' AND {VISIBLE}
        ORDER BY opened_at DESC, id DESC''', (account, chat)).fetchall()
