"""Stored classes of archive version conflicts (ANYM-557) and the one line an agent reads per account.

A class names the first field that differs, in the order archive.ingest tests them, between the conflict's
original candidate (its receipt envelope) and the revision it contradicted (`observed_revision`), never the
current retained revision. Only a class the reviewed table KNOWN lists is expected-unresolved with no action;
any other class, unclassifiable evidence and a NULL class (rows written by N-1 code) need attention.
"""
from datetime import datetime

from .envelope import validate

REASON_VERSION = 1
UNCLASSIFIED = 'unclassified'
PLATFORMS = {'telegram': 'Telegram', 'whatsapp': 'WhatsApp'}
FIELDS = {'sent_at': 'sent time differs', 'direction': 'direction differs', 'sender': 'sender differs',
          'body': 'text differs', 'media': 'attachments differ',
          'other': 'another field differs (formatting or deletion)'}
RELATIONS = {'equal_version': 'at the same provider version', 'older_version': 'in an older provider observation',
             'newer_version': 'in a newer provider observation'}
# Reviewed disposition (ANYM-530 research, owner Ready 10-26): a real contradiction at one provider version keeps the
# retained revision until later evidence arrives (archive.py, the resolve UPDATE), and no agent or owner step helps.
KNOWN = {
    'telegram_body_equal_version': 'Known Telegram case: two texts at one provider version; the archived revision stands until a later edit.',
    'telegram_direction_equal_version': 'Known Telegram case: the other direction at one provider version; the archived revision stands until later evidence.',
}
EXPLANATIONS = {
    **{f'{platform}_{field}_{relation}': f'{label} {text} {when}; inspect the chat.'
       for platform, label in PLATFORMS.items() for field, text in FIELDS.items()
       for relation, when in RELATIONS.items()},
    **KNOWN,
    UNCLASSIFIED: 'The stored evidence of this conflict could not be classified; inspect the chat.',
}
LISTING_LIMIT = 20
TOP_CLASSES = 3
BACKFILL_BATCH = 500
# The schema that stores classes. Before it (a rollback or an unmigrated test database) every conflict reads as
# unclassified and ingest writes the N-1 row, so no reader or collector depends on the new columns.
CLASS_SCHEMA = 54
CLASS_SQL = "coalesce(mc.reason,'unclassified')"


def stored(c):
    from .store import schema_version
    return schema_version(c) >= CLASS_SCHEMA


def class_sql(c):
    """SQL for a conflict's class: the stored one, a NULL one read as unclassified."""
    return CLASS_SQL if stored(c) else "'unclassified'"


def _time(value):
    if not isinstance(value, str): raise ValueError('time')
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None: raise ValueError('time')
    return parsed


# A revision snapshot is the validated envelope plus these archive fields; its `chat` is the archive chat ID.
SNAPSHOT_FIELDS = ('message', 'provider_chat', 'chat', 'revision')


def _classify(retained, candidate):
    # Both sides pass the ingest validation, so every field is in its domain (direction is incoming or outgoing,
    # times carry a zone); anything else raises and is unclassified, never a known class (Astra r1 F2).
    candidate = validate(candidate)
    retained = validate({**{k: v for k, v in retained.items() if k not in SNAPSHOT_FIELDS},
                         'chat': retained['provider_chat']})
    platform = candidate['platform']
    if platform not in PLATFORMS or retained['platform'] != platform: return UNCLASSIFIED
    # archive.ingest: identity (sent_at, direction, sender) first, then content at one provider version.
    if _time(candidate['sent_at']) != _time(retained['sent_at']): field = 'sent_at'
    elif candidate['direction'] != retained['direction']: field = 'direction'
    elif candidate['sender'] != retained['sender']: field = 'sender'
    elif candidate['body'] != retained['body']: field = 'body'
    elif candidate['media'] != retained['media']: field = 'media'
    else: field = 'other'
    candidate_at, retained_at = _time(candidate['updated_at']), _time(retained['updated_at'])
    relation = ('equal_version' if candidate_at == retained_at else
                'older_version' if candidate_at < retained_at else 'newer_version')
    return f'{platform}_{field}_{relation}'


def classify(retained, candidate):
    """The class of one conflict: `retained` is the observed revision's snapshot, `candidate` the receipt envelope.
    Never raises: ingest calls it inside its transaction, and bad evidence is a class of its own."""
    try:
        if not isinstance(retained, dict) or not isinstance(candidate, dict): return UNCLASSIFIED
        return _classify(retained, candidate)
    except Exception:
        return UNCLASSIFIED


def known_classes():
    return sorted(KNOWN)


def explanation(name):
    return EXPLANATIONS.get(name, EXPLANATIONS[UNCLASSIFIED])


def report(counts):
    """The account or chat line from {class: open conflicts}: attention, known, top classes and one step."""
    known = sum(n for name, n in counts.items() if name in KNOWN)
    attention = sum(counts.values()) - known
    top = sorted((item for item in counts.items() if item[1]), key=lambda item: (-item[1], item[0] in KNOWN, item[0]))
    return {'attention': attention, 'known': known,
            'top_classes': [{'class': name, 'count': n, 'explanation': explanation(name)} for name, n in top[:TOP_CLASSES]],
            'next_step': 'inspect_chat' if attention else 'none_required'}


def message_conflict(name):
    """What a message with an open conflict carries: its class and the step, never a tool name."""
    return {'class': name, 'next_step': 'none_required' if name in KNOWN else 'inspect_chat'}


def grouped(c, column, values):
    """{account or chat: (open by class, active, historical)}, counted per stored class. `column` is a constant."""
    if column not in ('account', 'chat'): raise ValueError('invalid_conflict_group')
    result = {}
    for row in c.execute(f'''SELECT m.{column} AS key,{class_sql(c)} AS class,
            count(*) FILTER(WHERE mc.resolved_at IS NULL) AS active,
            count(*) FILTER(WHERE mc.resolved_at IS NOT NULL) AS historical
            FROM message_conflicts mc JOIN messages m ON m.id=mc.message
            WHERE m.{column}=ANY(%s) GROUP BY 1,2''', (list(values),)):
        open_by_class, active, historical = result.get(row['key'], ({}, 0, 0))
        if row['active']: open_by_class[row['class']] = row['active']
        result[row['key']] = (open_by_class, active + row['active'], historical + row['historical'])
    return result


def listing(c, chat):
    """At most LISTING_LIMIT open conflicts of one permitted chat, attention first; no message content."""
    name = class_sql(c)
    rows = c.execute(f'''SELECT mc.message AS message_id,{name} AS class,mc.observed_at
        FROM message_conflicts mc JOIN messages m ON m.id=mc.message
        WHERE m.chat=%s AND mc.resolved_at IS NULL
        ORDER BY {name}=ANY(%s),mc.observed_at DESC,mc.receipt_digest LIMIT %s''',
        (chat, known_classes(), LISTING_LIMIT)).fetchall()
    return [{'message_id': r['message_id'], 'class': r['class'], 'explanation': explanation(r['class']),
             'observed_at': r['observed_at']} for r in rows]


# One backfill batch is its own short transaction: a lock wait or a slow batch fails it instead of holding
# collectors behind it (Astra r1 F1). A failed batch leaves its rows NULL (read as unclassified); rerun to resume.
BATCH_LOCK_TIMEOUT = "SET LOCAL lock_timeout='2s'"
BATCH_STATEMENT_TIMEOUT = "SET LOCAL statement_timeout='10s'"


def backfill(pool, batch=BACKFILL_BATCH, *, after_batch=None):
    """Classifies rows without a stored class with the insert rule, after migrate() has committed. Bounded (one
    keyset pass, each batch committed on its own), idempotent (only NULL classes) and non-resolving (resolved_at and
    resolved_revision are never written). Returns the number of rows classified; `after_batch()` is a test hook."""
    after, classified = '', 0
    while True:
        with pool.connection() as c:
            if not stored(c): return classified
            c.execute(BATCH_LOCK_TIMEOUT);c.execute(BATCH_STATEMENT_TIMEOUT)
            rows = c.execute('''SELECT mc.receipt_digest,r.envelope,mr.snapshot FROM message_conflicts mc
                LEFT JOIN ingest_receipts r ON r.digest=mc.receipt_digest
                LEFT JOIN message_revisions mr ON mr.message=mc.message AND mr.revision=mc.observed_revision
                WHERE mc.reason IS NULL AND mc.receipt_digest>%s ORDER BY mc.receipt_digest LIMIT %s''',
                (after, batch)).fetchall()
            if not rows: return classified
            with c.cursor() as cur:
                cur.executemany('''UPDATE message_conflicts SET reason=%s,reason_version=%s
                    WHERE receipt_digest=%s AND reason IS NULL''',
                    [(classify(r['snapshot'], r['envelope']), REASON_VERSION, r['receipt_digest']) for r in rows])
        classified += len(rows); after = rows[-1]['receipt_digest']
        if after_batch: after_batch()
