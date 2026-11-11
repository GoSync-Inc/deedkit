"""Feedback status events (ANYM-962): the reporting key is woken when the operator changes its report's status.

`set_feedback_status` publishes one module event (source deedkit:feedback, scope_ref the row's key_id) per item whose
status changes, in the operator's transaction, each in a savepoint of its own: a failed publish is logged as a fixed
code and counted, and the status write commits. A subscription option (`feedback`) attaches the source scoped to the
caller's own key. Events carry the feedback ID, two status words, the Tiden ID and the release only: never the note,
the description, a principal name or a message reference.
"""
from __future__ import annotations

from datetime import timezone

SOURCE = 'deedkit:feedback'
# The operator's six statuses (feedback.STATUSES without the agent verdicts verified and reopened, ANYM-344).
STATUSES = ('received', 'accepted', 'declined', 'duplicate', 'fixed', 'verify_requested')
# `feedback: true`: ready to verify (fixed, verify_requested) and closed (declined, duplicate).
DEFAULT_STATUSES = ('fixed', 'verify_requested', 'declined', 'duplicate')
FAILURE_KIND = 'feedback.status'


def subscription_types(value):
    """The module types of a create_subscription `feedback` value; anything else is invalid_filter."""
    from .store import APIError
    if value is True:
        statuses = DEFAULT_STATUSES
    elif isinstance(value, dict) and set(value) == {'statuses'} and isinstance(value['statuses'], (list, tuple)):
        statuses = value['statuses']
        if (not 1 <= len(statuses) <= len(STATUSES) or len(set(map(repr, statuses))) != len(statuses) or
                any(not isinstance(status, str) or status not in STATUSES for status in statuses)):
            raise APIError('invalid_filter')
    else:
        raise APIError('invalid_filter')
    return sorted('feedback.' + status for status in statuses)


def attach(c, p, subscription_id, value):
    """The feedback source of a new subscription: the caller's own key is the scope; the caller names none."""
    from .module_events import attach_sources
    attach_sources(c, subscription_id, [(SOURCE, str(p['key_id']), subscription_types(value))])


def _event(seq, row, status, changed_at, spec_ref, fixed_in):
    data = {'feedback_id': str(row['id']), 'status': status, 'previous_status': row['status']}
    if spec_ref is not None:
        data['spec_ref'] = spec_ref
    if fixed_in is not None:
        data['fixed_in'] = fixed_in
    return {'specversion': '1.0', 'id': 'feedback:%d' % seq, 'source': SOURCE, 'type': 'feedback.' + status,
        'subject': data['feedback_id'], 'time': changed_at.astimezone(timezone.utc).isoformat(), 'data': data,
        'urgency': 'now', 'expires_at': None}


def publish_changes(c, rows, status, changed_at, *, spec_ref=None, fixed_in=None):
    """One event per locked row whose status changes; a row whose status stays publishes nothing.

    rows: the locked rows with their old status. Each event has its own savepoint, so an error (an older schema, a
    statement timeout) rolls back that event only, is logged as a fixed code and counted, and never the status write.
    """
    published = 0
    for row in rows:
        if row['status'] == status:
            continue
        try:
            with c.transaction():
                publish_one(c, row, status, changed_at[str(row['id'])], spec_ref, fixed_in)
            published += 1
        except Exception as error:
            from .subscriptions import _journal_failed
            _journal_failed(FAILURE_KIND, error)
    return published


def publish_one(c, row, status, changed_at, spec_ref, fixed_in):
    from . import module_events
    # The journal counter's row lock, taken here and kept to commit (publish_module_event takes it next): the event's
    # number is its journal seq, so no restore of the journal can hand out a feedback ID twice.
    seq = c.execute('SELECT value FROM module_event_counter WHERE id=1 FOR UPDATE').fetchone()['value'] + 1
    event = _event(seq, row, status, changed_at, spec_ref, fixed_in)
    stored = module_events.publish(c, event, str(row['key_id']))
    from .wake import EVENTS, mark
    mark(c, EVENTS)  # ANYM-841: the matcher wakes after the commit; its poll covers any other connection
    return stored


def webhook_reference(item):
    """A feedback event in a webhook hint: these five keys only (no previous status, Tiden ID, release or scope)."""
    occurred = item['occurred_at'].astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    return {'event_id': item['event_key'], 'type': item['type'], 'occurred_at': occurred,
        'feedback_id': item['data']['feedback_id'], 'status': item['data']['status']}
