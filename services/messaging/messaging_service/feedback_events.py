"""Feedback status events (ANYM-962): the reporting key is woken when the operator changes its report's status.

`set_feedback_status` publishes one module event (source deedkit:feedback, scope_ref the row's key_id) per item whose
status changes, in the operator's transaction, each in a savepoint of its own: a failed publish is logged as a fixed
code and counted, and the status write commits. A subscription option (`feedback`) attaches the source scoped to the
caller's own key. Events carry the feedback ID, two status words, the Tiden ID and the release only: never the note,
the description, a principal name or a message reference.

A maintainer key (ANYM-989, permission `feedback-maintainer-read`) attaches the source `feedback:all` instead and is
woken for every key's items: by a submission (`feedback.received`: feedback ID, status and category, published by the
definer function `publish_feedback_received`, scope_ref `feedback:submitted`), by a recorded projection
(`feedback.projection_recorded`: feedback ID and the reader's classification, same scope_ref) and by every status
change. The event is a wake-up: the item is read with `export_feedback_metadata`, never from the event.
"""
from __future__ import annotations

from datetime import timezone

SOURCE = 'deedkit:feedback'
# The operator's six statuses (feedback.STATUSES without the agent verdicts verified and reopened, ANYM-344).
STATUSES = ('received', 'accepted', 'declined', 'duplicate', 'fixed', 'verify_requested')
# `feedback: true`: ready to verify (fixed, verify_requested) and closed (declined, duplicate).
DEFAULT_STATUSES = ('fixed', 'verify_requested', 'declined', 'duplicate')
FAILURE_KIND = 'feedback.status'
# ANYM-989: the maintainer source's scope, the fixed scope_ref of a submission and of a recorded projection (never a
# key ID, so an own-key source sees exactly what it saw before) and the types the maintainer source wants.
MAINTAINER_SCOPE = 'feedback:all'
SUBMITTED_SCOPE = 'feedback:submitted'
MAINTAINER_PERMISSION = 'feedback-maintainer-read'
MAINTAINER_TYPES = sorted(['feedback.' + status for status in STATUSES] + ['feedback.projection_recorded'])
SUBMISSION_FAILURE_KIND = 'feedback.received'
PROJECTION_FAILURE_KIND = 'feedback.projection_recorded'


def subscription_types(value):
    """The module types of a create_subscription `feedback` value's own-key part; anything else is invalid_filter.

    `{'maintainer': True}` (ANYM-989) has no own-key part (an empty list); with `statuses` beside it, the own-key part is
    those statuses.
    """
    from .store import APIError
    if maintainer_requested(value):
        if set(value) - {'maintainer', 'statuses'}:
            raise APIError('invalid_filter')
        if 'statuses' not in value:
            return []
        value = {'statuses': value['statuses']}
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


def maintainer_requested(value):
    """Whether a `feedback` value asks for the maintainer source (the value may still be invalid)."""
    return isinstance(value, dict) and 'maintainer' in value and value['maintainer'] is True


def maintainer_alone(value):
    """`feedback={'maintainer': True}` and nothing else: the one subscription a key without `read` may create."""
    return isinstance(value, dict) and value == {'maintainer': True}


def check_create(p, feedback, selection, direction):
    """Who may ask create_subscription for the maintainer source (ANYM-989), before anything is written.

    A key without `read` (the bridge key) may create one definition only: the maintainer source alone, no chat, source,
    sender, kind, origin, wake rule, send reference, webhook or module source beside it. A key with `read` needs the
    permission for the maintainer source: `read` does not stand for it. `selection` holds the message-selection arguments.
    """
    from .store import APIError
    if 'read' not in p['permissions']:
        if (not maintainer_alone(feedback) or MAINTAINER_PERMISSION not in p['permissions'] or
                any(value is not None for value in selection) or direction != 'incoming'):
            raise APIError('forbidden', 403)
    elif maintainer_requested(feedback) and MAINTAINER_PERMISSION not in p['permissions']:
        raise APIError('forbidden', 403)


def maintainer_selector(value, **others):
    """The marker create_subscription stores in a definition's selector (ANYM-989): `only` when the maintainer source is the
    whole definition (no chat, source, sender, wake rule or module source beside it, no own-key part), `with_others` when it
    shares the definition. Every role that reads subscriptions can read it, so the liveness rule needs no other table."""
    return 'only' if maintainer_alone(value) and all(other is None for other in others.values()) else 'with_others'


def maintainer_only(row):
    """A definition made with the maintainer source alone: the stored marker decides, no query."""
    return (row['selector'] or {}).get('feedback_maintainer') == 'only'


def attach(c, p, subscription_id, value):
    """The feedback sources of a new subscription: the caller's own key is the own-key scope (the caller names none), and
    the maintainer source (every key's events) needs the permission `feedback-maintainer-read`."""
    from .module_events import attach_sources
    from .store import APIError
    sources, own = [], subscription_types(value)
    if own:
        sources.append((SOURCE, str(p['key_id']), own))
    if maintainer_requested(value):
        if MAINTAINER_PERMISSION not in p['permissions']:
            raise APIError('forbidden', 403)
        sources.append((SOURCE, MAINTAINER_SCOPE, MAINTAINER_TYPES))
    attach_sources(c, subscription_id, sources)


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


def publish_received(c, feedback_id):
    """The event of a new submission (ANYM-989), in a savepoint of its own: an error is logged as a fixed code and
    counted, and the submission still commits (the item is stored and an export finds it; the event is a wake-up, never
    the only path).

    The API role cannot publish: the definer function `publish_feedback_received` reads the row's category and builds the
    event, so this caller supplies no data. The matcher wakes after the commit.
    """
    try:
        with c.transaction():
            c.execute('SELECT public.publish_feedback_received(%s) AS seq', (feedback_id,))
            from .wake import EVENTS, mark
            mark(c, EVENTS)  # ANYM-841: the matcher wakes after the commit; its poll covers any other connection
        return True
    except Exception as error:
        from .subscriptions import _journal_failed
        _journal_failed(SUBMISSION_FAILURE_KIND, error)
        return False


def publish_projection_recorded(c, feedback_id, classification, recorded_at):
    """One `feedback.projection_recorded` event (ANYM-989) on the operator's connection, in a savepoint of its own: an
    error is logged as a fixed code and counted and never undoes the projection write."""
    try:
        with c.transaction():
            from . import module_events
            # The journal counter's row lock, taken here and kept to commit: the event's number is its journal seq.
            seq = c.execute('SELECT value FROM module_event_counter WHERE id=1 FOR UPDATE').fetchone()['value'] + 1
            event = {'specversion': '1.0', 'id': 'feedback:%d' % seq, 'source': SOURCE,
                'type': 'feedback.projection_recorded', 'subject': str(feedback_id),
                'time': recorded_at.astimezone(timezone.utc).isoformat(),
                'data': {'feedback_id': str(feedback_id), 'classification': classification}, 'urgency': 'now',
                'expires_at': None}
            module_events.publish(c, event, SUBMITTED_SCOPE)
            from .wake import EVENTS, mark
            mark(c, EVENTS)
        return True
    except Exception as error:
        from .subscriptions import _journal_failed
        _journal_failed(PROJECTION_FAILURE_KIND, error)
        return False


def webhook_reference(item):
    """A feedback event in a webhook hint: these keys only (no previous status, category, Tiden ID, release or scope).
    A `projection_recorded` reference has no status, so it holds the first four keys only."""
    occurred = item['occurred_at'].astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    reference = {'event_id': item['event_key'], 'type': item['type'], 'occurred_at': occurred,
        'feedback_id': item['data']['feedback_id']}
    if 'status' in item['data']:
        reference['status'] = item['data']['status']
    return reference
