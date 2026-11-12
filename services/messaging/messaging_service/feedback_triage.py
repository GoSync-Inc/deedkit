"""A maintainer key's guarded status write (ANYM-992): accepted, declined or duplicate, nothing else.

The BB task bridge turns a task's status change into this call, so the reporting agent sees the item move. The write is the
operator's `set-feedback-status` for three statuses, reached by a key (permission `feedback-maintainer-write`) through the
definer function `public.set_feedback_triage`: the API role keeps no UPDATE on `agent_feedback`, and the function re-checks
everything this module checks, so a direct call is no wider than the tool.

compare-and-set: `expected_status` and `expected_changed_at` (null for an item whose status never changed) must equal the stored
ones, else `version_conflict` and nothing is written. idempotent: the same (key, request ID) with the same payload returns the
stored answer (`replayed`), another payload is `idempotency_conflict`. `fixed`, `verify_requested` and `verified` items are
evidence-driven and never overwritten (`version_conflict`). The event is the ANYM-962 one, published in the same transaction.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

import psycopg

from .feedback import SPEC_REF, STATUSES, UUID, _invalid, _timestamp
from .policy import targets

TRIAGE_STATUSES = ('accepted', 'declined', 'duplicate')
REQUEST_ID = re.compile(r'[a-zA-Z0-9_.:-]{8,128}')
CODES = {'version_conflict': 409, 'idempotency_conflict': 409, 'not_found': 404, 'invalid_feedback': 400}
# A note is read by the reporting agent (list_feedback): no link or file path may ride in it (ANYM-992).
URL = re.compile(r'(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.)\S+')
PATH = re.compile(r'[A-Za-z]:\\\S*|(?<![\w/:.])(?:~|\.{1,2})?/[\w.~-]+(?:/[\w.~-]*)*|\b[\w.-]+(?:/[\w.-]+){2,}')
CONTROL = re.compile(r'[\x00-\x1f\x7f]')


def scrub_note(note):
    """The note with every URL and file path removed and its spaces collapsed; None when nothing is left."""
    text = ' '.join(PATH.sub(' ', URL.sub(' ', note)).split())
    return text or None


def _errors(feedback_id, status, expected_status, expected_changed_at, request_id, spec_ref, duplicate_of, note):
    errors = []
    if not (isinstance(feedback_id, str) and UUID.fullmatch(feedback_id)):
        errors.append(('feedback_id', 'pattern', 'a feedback ID (UUID)'))
    if status not in TRIAGE_STATUSES:
        errors.append(('status', 'enum', None, TRIAGE_STATUSES))
    if expected_status not in STATUSES:
        errors.append(('expected_status', 'enum', None, STATUSES))
    if not (isinstance(request_id, str) and REQUEST_ID.fullmatch(request_id)):
        errors.append(('request_id', 'pattern', '8–128 characters: letters, digits and _ . : -'))
    if spec_ref is not None and not (isinstance(spec_ref, str) and SPEC_REF.fullmatch(spec_ref)):
        errors.append(('spec_ref', 'pattern', 'ANYM-<n>'))
    if duplicate_of is not None and not (isinstance(duplicate_of, str) and UUID.fullmatch(duplicate_of)):
        errors.append(('duplicate_of', 'pattern', 'a feedback ID (UUID)'))
    if note is not None and not (isinstance(note, str) and 1 <= len(note) <= 280 and note.strip() and not CONTROL.search(note)):
        errors.append(('note', 'max_length', '1–280 characters without control characters'))
    if status == 'accepted' and spec_ref is None:
        errors.append(('spec_ref', 'required', 'set for accepted'))
    if status == 'duplicate' and duplicate_of is None and spec_ref is None:
        errors.append(('duplicate_of', 'required', 'duplicate_of or spec_ref for duplicate'))
    if status in TRIAGE_STATUSES and status != 'duplicate' and duplicate_of is not None:
        errors.append(('duplicate_of', 'unknown_field', 'only for duplicate'))
    return errors


def set_feedback_triage(store, c, p, feedback_id, status, expected_status, expected_changed_at, request_id, *,
                        spec_ref=None, duplicate_of=None, note=None):
    errors = _errors(feedback_id, status, expected_status, expected_changed_at, request_id, spec_ref, duplicate_of, note)
    if expected_changed_at is not None:
        try:
            expected_at = _timestamp(expected_changed_at, 'invalid_expected_changed_at')
        except ValueError:
            errors.append(('expected_changed_at', 'pattern', 'an ISO time with an offset, or null'))
    if errors:
        raise _invalid(errors)
    expected_at = None if expected_changed_at is None else expected_at
    feedback_id = feedback_id.lower()
    duplicate_of = None if duplicate_of is None else duplicate_of.lower()
    payload = {'feedback_id': feedback_id, 'status': status, 'expected_status': expected_status, 'spec_ref': spec_ref,
        'expected_changed_at': None if expected_at is None else expected_at.astimezone(timezone.utc).isoformat(),
        'duplicate_of': duplicate_of, 'note': note}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    # The same 5 s bound as an operator status change, so this never holds agent_feedback row locks longer than a submission.
    c.execute("SELECT set_config('transaction_timeout','5s',true)")
    try:
        result = c.execute('SELECT public.set_feedback_triage(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS result',
            (p['key_id'], request_id, digest, feedback_id, status, expected_status, expected_at, spec_ref, duplicate_of,
             None if note is None else scrub_note(note))).fetchone()['result']
    except psycopg.errors.RaiseException as error:
        from .store import APIError
        code = error.diag.message_primary
        if code not in CODES:
            raise
        raise APIError(code, CODES[code]) from None
    targets(p, [feedback_id])
    if result['published'] is False:
        # The write committed; its event did not publish (an older database during a rolling upgrade, a timeout).
        from .feedback_events import FAILURE_KIND
        from .subscriptions import _journal_failed
        _journal_failed(FAILURE_KIND, RuntimeError('publish_failed'))
    # jsonb trims trailing zeros of the time; the export prints it with isoformat, and so does the answer.
    changed = datetime.fromisoformat(result['status_changed_at']).isoformat()
    return {'feedback_id': result['feedback_id'], 'status': result['status'], 'status_changed_at': changed,
        'spec_ref': result['spec_ref'], 'duplicate_of': result['duplicate_of'], 'replayed': result['replayed']}
