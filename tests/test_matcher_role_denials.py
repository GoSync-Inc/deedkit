"""The subscription matcher role never reaches hint receivers, dispatch, consumption writes or provider sessions.

ANYM-185: the matcher stages opaque hints; only the separate dispatcher role reads endpoints and secret paths and
moves a hint through delivery. The role is installed from the shipped subscription_role.sql under a unique name.
Every refusal sits beside a granted read or write on the same table or binding, so a refusal shows a missing
privilege, not a broken statement or an empty table. Denials other tests already pin (credentials, messages,
UPDATE of consumption or outbox state, receipt_hash, wake targets) are not repeated.
"""
from uuid import uuid4

import pytest

from test_db_role_isolation import as_role, install, refused, remove
from test_subscription_backup_roles import _subscription


@pytest.fixture()
def matcher_world(seeded):
    """A delivery-bound subscription with a receiver, an endpoint, a staged hint, consumption, a receipt and a
    claimed provider session, all synthetic; plus the matcher role installed from the shipped SQL."""
    store, _, _ = seeded
    token, definition, _ = _subscription(store, 'synthetic-anym185-' + uuid4().hex[:8])
    sid = definition['subscription_id']
    key_id = store.capabilities(token)['credential_id']
    principal = store.capabilities(token)['principal']
    receiver, receipt = uuid4(), 'd' * 64
    with store.pool.connection() as c:
        c.execute('''INSERT INTO subscription_receivers(id,principal,key_id,endpoint,secret_file)
            VALUES(%s,%s,%s,'https://receiver.invalid/wake','/private/synthetic-receiver.key')''',
            (receiver, principal, key_id))
        c.execute('INSERT INTO subscription_delivery(subscription,receiver) VALUES(%s,%s)', (sid, receiver))
        endpoint = c.execute('''INSERT INTO delivery_endpoints(principal,key_id,request_id,request_hash,url,host,
            signing_secret) VALUES(%s,%s,'synthetic-endpoint-185',%s,'https://hooks.invalid/185','hooks.invalid',
            'whsec_synthetic') RETURNING id''', (principal, key_id, 'e' * 64)).fetchone()['id']
        hint = c.execute('''INSERT INTO subscription_outbox(subscription,subscription_version,receiver,through_position,
            kind) VALUES(%s,1,%s,1,'events') RETURNING id''', (sid, receiver)).fetchone()['id']
        c.execute('INSERT INTO subscription_consumption(subscription,acked_through) VALUES(%s,0)', (sid,))
        c.execute('''INSERT INTO subscription_receipts(receipt_hash,subscription,principal,key_id,scope_version,
            key_permission_version,subscription_version,consumption_version,after_position,through_position)
            VALUES(%s,%s,%s,%s,1,1,1,1,0,1)''', (receipt, sid, principal, key_id))
        c.execute('''INSERT INTO session_claims(account,host_id,boot_id,lock_ref,holder_id,holder_kind,generation,
            claimed_at,seen_at) VALUES('telegram-demo',%s,'synthetic-boot','1:2',%s,'collector',1,
            clock_timestamp(),clock_timestamp())''', ('a' * 32, 'b' * 32))
        c.execute('''INSERT INTO collector_leases(account,owner,epoch,expires_at)
            VALUES('telegram-demo','synthetic-collector',1,clock_timestamp()+interval '1 hour')
            ON CONFLICT(account) DO NOTHING''')
    role = install(store, 'subscription_role.sql', 'messaging_subscriptions')
    try:
        yield store, role, {'subscription': sid, 'receiver': receiver, 'endpoint': endpoint, 'hint': hint,
                            'receipt': receipt, 'principal': principal, 'key_id': key_id}
    finally:
        remove(store, role)


def granted(store, role, statement, params=()):
    with as_role(store, role) as c:
        cursor = c.execute(statement, params)
        return cursor.fetchall() if cursor.description else cursor.rowcount


# spec: ANYM-185
def test_the_matcher_reads_a_delivery_binding_but_never_the_receivers_endpoint_or_secret_path(matcher_world):
    store, role, ids = matcher_world
    # Granted: which receiver a subscription is bound to, and whether an endpoint is enabled.
    assert granted(store, role, 'SELECT receiver FROM subscription_delivery WHERE subscription=%s',
                   (ids['subscription'],)) == [{'receiver': ids['receiver']}]
    assert granted(store, role, 'SELECT id,state FROM delivery_endpoints WHERE id=%s',
                   (ids['endpoint'],)) == [{'id': ids['endpoint'], 'state': 'enabled'}]
    # Refused: the receiver row itself (endpoint URL and secret file path), and every endpoint column beyond id
    # and state (URL, host, secrets, header values).
    for column in ('endpoint', 'secret_file', 'id', '*'):
        refused(store, role, f'SELECT {column} FROM subscription_receivers')
    for column in ('url', 'host', 'signing_secret', 'pending_secret', 'header_values', '*'):
        refused(store, role, f'SELECT {column} FROM delivery_endpoints WHERE id=%s', (ids['endpoint'],))
    refused(store, role, "UPDATE subscription_receivers SET enabled=false WHERE id=%s", (ids['receiver'],))
    refused(store, role, "UPDATE delivery_endpoints SET state='enabled' WHERE id=%s", (ids['endpoint'],))


# spec: ANYM-185
def test_the_matcher_stages_a_hint_but_never_leases_dispatches_or_finishes_one(matcher_world):
    store, role, ids = matcher_world
    # Granted: read and stage an opaque hint for the bound receiver in the matching transaction.
    assert granted(store, role, 'SELECT state FROM subscription_outbox WHERE id=%s', (ids['hint'],)) == [
        {'state': 'pending'}]
    assert granted(store, role, '''INSERT INTO subscription_outbox(subscription,subscription_version,receiver,
        through_position,kind) VALUES(%s,1,%s,2,'events')''', (ids['subscription'], ids['receiver'])) == 1
    # Refused: every column the dispatcher moves to lease, attempt, admit and finish a hint.
    for assignment in ('lease_token=gen_random_uuid()', "lease_until=clock_timestamp()+interval '1 minute'",
                       'attempts=attempts+1', 'next_attempt_at=clock_timestamp()', 'admitted_at=clock_timestamp()',
                       'finished_at=clock_timestamp()', "error_code='synthetic'"):
        refused(store, role, f'UPDATE subscription_outbox SET {assignment} WHERE id=%s', (ids['hint'],))
    # Refused: the dispatcher's definer function that builds a hint's references.
    refused(store, role, 'SELECT * FROM public.subscription_hint_references(%s,0,10,%s,NULL)',
            (ids['subscription'], ['events']))


# spec: ANYM-185
def test_the_matcher_reads_consumption_and_receipt_ownership_but_never_writes_a_receipt_or_consumption(matcher_world):
    store, role, ids = matcher_world
    # Granted (retention, ANYM-446/447): the ACK position and which subscription a receipt belongs to.
    assert granted(store, role, 'SELECT acked_through FROM subscription_consumption WHERE subscription=%s',
                   (ids['subscription'],)) == [{'acked_through': 0}]
    assert [str(row['subscription']) for row in granted(store, role,
        'SELECT subscription FROM subscription_receipts WHERE subscription=%s', (ids['subscription'],))] == [
        ids['subscription']]
    # Refused: a receipt's holder, positions and ACK result, any new receipt or consumption, and a receipt change.
    for column in ('principal', 'key_id', 'after_position', 'through_position', 'ack_result', 'expires_at'):
        refused(store, role, f'SELECT {column} FROM subscription_receipts WHERE subscription=%s', (ids['subscription'],))
    refused(store, role, '''INSERT INTO subscription_receipts(receipt_hash,subscription,principal,key_id,scope_version,
        key_permission_version,subscription_version,consumption_version,after_position,through_position)
        VALUES(%s,%s,%s,%s,1,1,1,1,0,1)''', ('f' * 64, ids['subscription'], ids['principal'], ids['key_id']))
    refused(store, role, "UPDATE subscription_receipts SET ack_result='{}' WHERE subscription=%s", (ids['subscription'],))
    refused(store, role, "UPDATE subscription_receipts SET expires_at=clock_timestamp() WHERE subscription=%s",
            (ids['subscription'],))
    refused(store, role, '''INSERT INTO subscription_consumption(subscription,acked_through) VALUES(%s,0)
        ON CONFLICT(subscription) DO NOTHING''', (ids['subscription'],))


# spec: ANYM-185
def test_the_matcher_has_no_provider_session_or_collector_lease_access(matcher_world):
    store, role, _ = matcher_world
    # Granted: the account platform the matcher needs, read on the same connection path.
    assert {'id': 'telegram-demo', 'platform': 'telegram'} in granted(store, role, 'SELECT id,platform FROM accounts')
    for statement in ('SELECT account FROM session_claims', 'SELECT holder_id,lock_ref FROM session_claims',
                      "UPDATE session_claims SET released_at=clock_timestamp() WHERE account='telegram-demo'",
                      "DELETE FROM session_claims WHERE account='telegram-demo'",
                      '''INSERT INTO session_claims(account,host_id,boot_id,lock_ref,holder_id,holder_kind,generation,
                          claimed_at,seen_at) SELECT account,host_id,boot_id,lock_ref,holder_id,holder_kind,generation+1,
                          claimed_at,seen_at FROM session_claims WHERE false''',
                      'SELECT owner,epoch FROM collector_leases',
                      "UPDATE collector_leases SET expires_at=clock_timestamp() WHERE account='telegram-demo'",
                      "DELETE FROM collector_leases WHERE account='telegram-demo'"):
        refused(store, role, statement)
