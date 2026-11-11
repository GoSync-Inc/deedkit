"""ANYM-986: a send that asks to be woken when no reply comes (`await_reply`).

The argument is `{"within": "<n>m|h|d"}`, 60 seconds to 14 days. It is stored with the outgoing row as
`{"within_s": n}` and is part of the preview digest (its own bound field, never `render_policy`, so the wire payload and
the artifact stay as they are). The collector alone sets `sent`, and no worker role may create a subscription, so the
API process runs `process` (an app lifespan job): it reads a row whose message is `sent`, rebuilds the sender's
principal from the stored key, and creates one `reply_to_send` subscription with the silence timer and the key's single
enabled delivery endpoint, through `create_subscription`. The row is the queue: `await_state` pending, then created
(with the subscription ID) or refused (with a fixed reason code), once, because the marker update and the subscription
share one transaction and the update is conditional on `pending`. A failure here never touches the send.
"""
import logging
import re
from collections import Counter

log = logging.getLogger(__name__)

MIN_S, MAX_S = 60, 14 * 86400
PERIOD_S = 15  # the job's pass; a silence timer is 60 seconds at the least
UNITS = {'m': 60, 'h': 3600, 'd': 86400}
WITHIN = re.compile(r'([1-9][0-9]{0,5})([mhd])')
EXPECTED = 'an object {"within": "<n>m", "<n>h" or "<n>d"}, 60 seconds to 14 days'
DESCRIPTION = ('chat_id sends: {"within": "6h"} (60 seconds to 14 days). Once the message is sent, the server creates a '
               'reply_to_send subscription with a silence timer on your one enabled delivery endpoint, so you stop '
               'polling. Preview and send must match: the digest binds it. The send result shows await_reply.state.')
REFUSED = {'forbidden': 'permission_changed', 'subscription_limit': 'subscription_limit'}
TERMINAL = ('failed', 'expired', 'cancelled')
NEEDS = ('read', 'subscription-write', 'delivery-write')
FAILURES = Counter()  # job passes that raised, by fixed code (as subscriptions.JOURNAL_FAILURES)


def normalized(value):
    """None when the argument is absent, else `{'within_s': n}`; anything else is invalid_parameters naming the field."""
    from .field_errors import refusal
    if value is None:
        return None
    given = isinstance(value, dict) and set(value) == {'within'} and isinstance(value['within'], str)
    found = WITHIN.fullmatch(value['within']) if given else None
    seconds = int(found[1]) * UNITS[found[2]] if found else 0
    if not MIN_S <= seconds <= MAX_S:
        raise refusal('invalid_parameters', [('await_reply/within' if given else 'await_reply', 'format', EXPECTED)])
    return {'within_s': seconds}


def unsupported():
    """A WhatsApp menu answer is a reply to the bot's own message: it takes no await_reply."""
    from .field_errors import refusal
    return refusal('invalid_parameters', [('await_reply', 'format', 'omitted on a menu_choice answer')])


def preview_block(value):
    """What the owner confirms: the wake rule in plain numbers."""
    return {'await_reply': {**value, 'creates': 'reply_to_send subscription with a silence timer, after the send'}}


def result_fields(row):
    """`await_reply: {state, subscription_id?, reason?}` read from the row; empty for a send that asked nothing."""
    if not isinstance(row.get('await_reply'), dict):
        return {}
    state = row.get('await_state') or 'pending'
    return {'await_reply': {'state': state, **({'subscription_id': row['await_subscription_id']}
            if row.get('await_subscription_id') else {}), **({'reason': row['await_reason']} if row.get('await_reason') else {})}}


def _principal(c, row):
    """The sender's principal as the key resolves now, or the refusal reason; never raises for a changed key."""
    from .policy import effective_permissions, lock_principal
    lock_principal(c, row['principal'], shared=True)
    p = c.execute('''SELECT p.*,k.digest AS key_digest,k.id AS key_id,k.permissions AS key_permissions,
        k.permission_version AS key_permission_version,k.expires_at AS key_expires_at FROM api_keys k
        JOIN principals p ON p.id=k.principal WHERE k.id=%s AND k.principal=%s AND NOT k.revoked
        AND k.expires_at>clock_timestamp() AND p.enabled''', (row['key_id'], row['principal'])).fetchone()
    if p is None:
        return None, 'key_inactive'
    p['principal_permissions'] = p['permissions']
    p['permissions'] = effective_permissions(p['permissions'], p['key_permissions'])
    if (p['scope_version'] != row['scope_version'] or p['key_permission_version'] != row['key_permission_version']
            or any(name not in p['permissions'] for name in NEEDS)):
        return None, 'permission_changed'
    return p, None


def _subscribe(store, c, row):
    """`(subscription_id, None)` or `(None, reason)` for one sent row."""
    from .store import APIError
    from .subscriptions import create_subscription
    p, reason = _principal(c, row)
    if p is None:
        return None, reason
    endpoints = c.execute("SELECT id FROM delivery_endpoints WHERE principal=%s AND key_id=%s AND state='enabled' ORDER BY id",
        (p['id'], p['key_id'])).fetchall()
    if len(endpoints) != 1:
        return None, 'no_delivery_endpoint' if not endpoints else 'ambiguous_delivery_endpoint'
    try:
        with c.transaction():  # a savepoint: a refusal leaves no partial subscription
            made = create_subscription(store, c, p, [row['chat']], reply_to_send=str(row['id']),
                label='await_reply:' + str(row['id'])[:100], delivery={'endpoint_id': str(endpoints[0]['id']),
                'delivery_policy': {'expect_reply_within_s': row['await_reply']['within_s']}})
    except APIError as error:
        return None, REFUSED.get(error.code, 'subscription_refused')
    return str(made.get('subscription_id') or made.get('id')), None


def settle(store, c, send_id):
    """One pending row, inside the caller's transaction; the marker and the subscription commit together."""
    row = c.execute("SELECT * FROM outgoing_messages WHERE id=%s AND await_state='pending' FOR UPDATE SKIP LOCKED",
        (send_id,)).fetchone()
    if row is None or row['status'] not in ('sent', *TERMINAL):
        return None  # settled by another process, or not sent yet (queued, dispatching, scheduled, unknown)
    made, reason = (None, 'send_not_sent') if row['status'] in TERMINAL else _subscribe(store, c, row)
    c.execute("UPDATE outgoing_messages SET await_state=%s,await_subscription_id=%s,await_reason=%s "
              "WHERE id=%s AND await_state='pending'", ('refused' if reason else 'created', made, reason, send_id))
    return 'refused' if reason else 'created'


def process(store, limit=25):
    """One pass over the pending rows; returns `{created, refused}` counts and never raises."""
    done = Counter()
    try:
        with store.pool.connection() as c:
            ids = [r['id'] for r in c.execute("""SELECT id FROM outgoing_messages WHERE await_state='pending'
                AND status IN ('sent','failed','expired','cancelled') ORDER BY created_at LIMIT %s""", (limit,)).fetchall()]
    except Exception:
        FAILURES['await_reply_scan'] += 1
        log.error('await_reply_scan_failed')
        return done
    for send_id in ids:
        try:
            with store.pool.connection() as c, c.transaction():
                outcome = settle(store, c, send_id)
            if outcome:
                done[outcome] += 1
        except Exception:
            FAILURES['await_reply_settle'] += 1
            log.error('await_reply_settle_failed')
    return done
