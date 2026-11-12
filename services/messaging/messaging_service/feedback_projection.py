"""The feedback loop's released summary and a maintainer key's read of it (ANYM-988).

`feedback_projections` (schema 94) holds, per feedback item the tool-less reader decided, its closed answer and the
released card brief: classification, reason code, primary card, new-card slug, the brief (a scrubbed, number-redacted
paraphrase that repeats no five consecutive words of the item, scripts/autoloop.py `released`), when it was released and
which reader produced it. Only the operator command `record-feedback-projection` writes it (the owner connection; no
agent path); `export_feedback_metadata` reads it for a key that holds `feedback-maintainer-read`.

The read shows other keys' items, so it returns metadata only: never `description`, `refs`, `status_note`, a request ID
or a key ID. The brief is model output derived from untrusted text and travels under `untrusted_content: true`.
"""
from __future__ import annotations

import base64
from datetime import timedelta
import json
import re

from .feedback import SPEC_REF, UUID, _timestamp
from .policy import audit, targets

# The reader's closed sets (scripts/autoloop.py CLASSES, REASONS, NEW_CARD, BRIEF_LIMIT); migration 094 holds the same
# CHECKs and tests/test_feedback_export_metadata.py pins all of them to the script's.
CLASSES = ('reported-bug', 'feature', 'question', 'not-actionable', 'already-live', 'enable-only')
REASONS = ('matches-card', 'new-idea', 'duplicate', 'already-live', 'needs-opt-in', 'question-answered',
           'not-actionable', 'out-of-scope', 'unclear')
NEW_CARD = re.compile(r'[a-z0-9][a-z0-9-]{2,39}')
BRIEF_LIMIT = 600
PIN_LIMIT = 128
MAX_ITEMS = 100
CONTROL = re.compile(r'[\x00-\x1f\x7f]')
ITEM_KEYS = frozenset(('feedback_id', 'classification', 'reason_code', 'primary_spec', 'new_card', 'released_brief',
                       'released_at', 'reader_pin'))
REQUIRED = frozenset(('feedback_id', 'classification', 'reason_code', 'released_at', 'reader_pin'))
# A release time may run this far ahead of the database clock (clock skew between the controller and the database),
# never further: a future time would keep every later release of the item out.
CLOCK_SKEW = timedelta(minutes=10)
# The read's overlap: the final cursor of an export starts the next one this far behind its head, so a row committed
# late behind the head (a submission's transaction ends within 60 s, feedback.SUBMIT_TRANSACTION_TIMEOUT) is returned
# by the next poll; the caller deduplicates by feedback ID.
OVERLAP = timedelta(minutes=10)
NIL = '00000000-0000-0000-0000-000000000000'
CURSOR_KEYS = {'v', 't', 'i', 'h'}


def _item_codes(item):
    """The fixed refusal codes of one projection item, never an input value."""
    if not isinstance(item, dict) or not REQUIRED <= set(item) <= ITEM_KEYS:
        return ['invalid_projection']
    codes = []
    if not (isinstance(item['feedback_id'], str) and UUID.fullmatch(item['feedback_id'])):
        codes.append('invalid_feedback_id')
    if item['classification'] not in CLASSES:
        codes.append('invalid_classification')
    if item['reason_code'] not in REASONS:
        codes.append('invalid_reason_code')
    spec = item.get('primary_spec')
    if spec is not None and not (isinstance(spec, str) and SPEC_REF.fullmatch(spec)):
        codes.append('invalid_primary_spec')
    card = item.get('new_card')
    if card is not None and not (isinstance(card, str) and NEW_CARD.fullmatch(card)):
        codes.append('invalid_new_card')
    brief = item.get('released_brief')
    if brief is not None and not (isinstance(brief, str) and 1 <= len(brief) <= BRIEF_LIMIT and brief.strip()
                                  and not CONTROL.search(brief)):
        codes.append('invalid_released_brief')
    pin = item['reader_pin']
    if not (isinstance(pin, str) and 1 <= len(pin) <= PIN_LIMIT and not CONTROL.search(pin)):
        codes.append('invalid_reader_pin')
    return codes


def projection_errors(document):
    """The refusal codes of a whole file, in one fixed order; empty when it is valid."""
    if not isinstance(document, dict) or set(document) != {'projections'} or not isinstance(document['projections'], list):
        return ['invalid_projection_file']
    items = document['projections']
    if not 1 <= len(items) <= MAX_ITEMS:
        return ['projections_1_to_100']
    codes = []
    for item in items:
        codes += [code for code in _item_codes(item) if code not in codes]
    if not codes:
        names = [item['feedback_id'].lower() for item in items]
        if len(set(names)) != len(names):
            codes.append('duplicate_feedback_id')
    return codes


def record_projections(c, document):
    """Operator-only (the owner connection): store the reader's decisions and released briefs, atomically.

    The whole file is refused (ValueError with fixed codes, nothing written) on any malformed value or unknown feedback
    ID. An item replaces the stored one only when its `released_at` is newer, so a rerun of the same file, a retried
    tick and an older release written late all leave the table as it is; the reply counts only.
    """
    errors = projection_errors(document)
    if errors:
        raise ValueError(','.join(errors))
    items = {item['feedback_id'].lower(): item for item in document['projections']}
    released = {name: _timestamp(item['released_at'], 'invalid_released_at') for name, item in items.items()}
    # The same 5 s bound as an operator status change, so this never holds feedback row locks longer than a submission.
    c.execute("SET LOCAL transaction_timeout='5s'")
    now = c.execute('SELECT clock_timestamp() AS now').fetchone()['now']
    if any(moment > now + CLOCK_SKEW for moment in released.values()):
        raise ValueError('invalid_released_at')
    names = sorted(items)
    rows = c.execute('SELECT id,principal,key_id FROM agent_feedback WHERE id=ANY(%s::uuid[]) ORDER BY id FOR KEY SHARE',
        (names,)).fetchall()
    if len(rows) != len(names):
        raise ValueError('unknown_feedback: %d of %d IDs' % (len(names) - len(rows), len(names)))
    recorded = 0
    for row in rows:
        item = items[str(row['id'])]
        written = c.execute('''INSERT INTO feedback_projections(feedback_id,classification,reason_code,primary_spec,
            new_card,released_brief,released_at,reader_pin,recorded_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp())
            ON CONFLICT(feedback_id) DO UPDATE SET classification=excluded.classification,reason_code=excluded.reason_code,
            primary_spec=excluded.primary_spec,new_card=excluded.new_card,released_brief=excluded.released_brief,
            released_at=excluded.released_at,reader_pin=excluded.reader_pin,recorded_at=excluded.recorded_at
            WHERE feedback_projections.released_at<excluded.released_at RETURNING feedback_id''',
            (row['id'], item['classification'], item['reason_code'], item.get('primary_spec'), item.get('new_card'),
             item.get('released_brief'), released[str(row['id'])], item['reader_pin'])).fetchone()
        if written:
            recorded += 1
            # One content-free operator audit row per written item: the fixed operation code and the feedback ID only.
            audit(c, {'id': row['principal'], 'key_id': row['key_id'], '_audit_targets': [str(row['id'])]},
                'record_feedback_projection', 'ok')
    return {'recorded': recorded, 'unchanged': len(rows) - recorded}


def _cursor(head, moment, last):
    position = {'v': 1, 'h': None if head is None else head.isoformat(), 't': moment.isoformat(), 'i': last}
    return base64.urlsafe_b64encode(json.dumps(position, separators=(',', ':')).encode()).decode().rstrip('=')


def _read_cursor(token):
    """(head or None, time, id) of a cursor this read issued; anything else is invalid_cursor."""
    from .store import APIError
    try:
        raw = base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)) if isinstance(token, str) and 0 < len(token) <= 512 else b''
        position = json.loads(raw)
        if not (isinstance(position, dict) and set(position) == CURSOR_KEYS and position['v'] == 1
                and isinstance(position['i'], str) and UUID.fullmatch(position['i'])):
            raise ValueError
        head = None if position['h'] is None else _timestamp(position['h'], 'invalid_cursor')
        return head, _timestamp(position['t'], 'invalid_cursor'), position['i']
    except (ValueError, TypeError):
        raise APIError('invalid_cursor') from None


def export_feedback_metadata(store, c, p, *, cursor=None, limit=50):
    """Every principal's feedback in one keyset order, oldest change first, with the stored projection.

    The order is (changed_at, id), changed_at the greatest of the submission time, the status change time and the
    projection's recording time, so a poller sees new items and changed ones in one pass and keeps one cursor. Pages of
    one export are bounded by the head the first page fixed; the cursor after the last page starts the next export an
    overlap behind that head. Pages are separate transactions, not a snapshot: the caller deduplicates by feedback ID.
    """
    from .store import bounded, serial
    bounded(limit)
    start = None if cursor is None else _read_cursor(cursor)
    head = start[0] if start and start[0] is not None else c.execute('SELECT clock_timestamp() AS head').fetchone()['head']
    rows = c.execute('''SELECT * FROM (SELECT f.id,f.principal,f.category,f.operation,f.created_at,f.status,f.fixed_in,
            f.spec_ref,f.duplicate_of,f.status_changed_at,
            GREATEST(f.created_at,f.status_changed_at,j.recorded_at) AS changed_at,
            j.feedback_id IS NOT NULL AS projected,j.classification,j.reason_code,j.primary_spec,j.new_card,
            j.released_brief,j.released_at,j.reader_pin
        FROM agent_feedback f LEFT JOIN feedback_projections j ON j.feedback_id=f.id) x
        WHERE changed_at<=%(head)s AND (%(t)s::timestamptz IS NULL OR (changed_at,id)>(%(t)s,%(i)s::uuid))
        ORDER BY changed_at,id LIMIT %(limit)s''',
        {'head': head, 't': start[1] if start else None, 'i': start[2] if start else NIL, 'limit': limit + 1}).fetchall()
    more, rows = len(rows) > limit, rows[:limit]
    items = [{'feedback_id': row['id'], 'principal': row['principal'], 'category': row['category'],
              'operation': row['operation'], 'created_at': row['created_at'], 'status': row['status'],
              'fixed_in': row['fixed_in'], 'spec_ref': row['spec_ref'], 'duplicate_of': row['duplicate_of'],
              'status_changed_at': row['status_changed_at'],
              'projection': {name: row[name] for name in ('classification', 'reason_code', 'primary_spec', 'new_card',
                  'released_brief', 'released_at', 'reader_pin')} if row['projected'] else None} for row in rows]
    targets(p, [str(row['id']) for row in rows])
    # A page that ends the export hands back the start of the next one; a page that does not hands back its own end.
    token = _cursor(head, rows[-1]['changed_at'], str(rows[-1]['id'])) if more else _cursor(None, head - OVERLAP, NIL)
    return serial({'untrusted_content': True, 'items': items, 'has_more': more, 'next_cursor': token,
                   'export_head': head})
