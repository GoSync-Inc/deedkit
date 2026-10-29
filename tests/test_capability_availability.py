"""Capabilities report per-account usable operations exactly as list_messenger_operations."""
from messaging_service.envelope import stable_id
from test_messenger_commands import operator


def available(s,key,account):
    return [x['operation'] for x in s.list_messenger_operations(key,account)['items'] if x['available']]


def test_capabilities_available_operations_match_operation_listing(operator):
    s,_,key,_=operator
    row=s.capabilities(key)['messenger_operations']['supported_by_account']['command-fixture']
    assert row['available_operations']==available(s,key,'command-fixture')
    assert row['count']==len(row['operations'])>len(row['available_operations'])
    # Supported and key-permitted, but not opted in for this account.
    assert 'rename_group' in row['operations'] and 'rename_group' not in row['available_operations']
    assert {'edit_message','list_contacts','send_file'}<=set(row['available_operations'])
    s.set_messenger_operations('command-fixture',['rename_group'])
    row=s.capabilities(key)['messenger_operations']['supported_by_account']['command-fixture']
    assert row['available_operations']==['rename_group']==available(s,key,'command-fixture')


def test_chat_scoped_key_excludes_full_account_operations(operator):
    s,_,_,_=operator
    chat=stable_id('chat','command-fixture','100')
    key=s.issue_key('command-chat-agent',['command-fixture'],chats=[chat],
                    permissions=['read','download','send','message-edit','contacts-read'])
    row=s.capabilities(key)['messenger_operations']['supported_by_account']['command-fixture']
    assert row['available_operations']==available(s,key,'command-fixture')
    assert 'edit_message' in row['available_operations'] and 'list_contacts' not in row['available_operations']
    assert 'list_contacts' in row['operations']


def transports(s,key):
    return {t['name']:t for t in s.capabilities(key)['subscriptions']['transports']}


def bound_receiver(store,token,tmp_path):
    from test_event_delivery import call,receiver,subscription
    sid=subscription(store,token);rid,_=receiver(store,token,tmp_path)
    call(store,token,'configure_subscription_delivery',sid,1,rid)
    return rid


def heartbeat(store,age_s):
    with store.pool.connection() as c:
        c.execute('''INSERT INTO subscription_dispatcher_heartbeat(id,beat_at) VALUES(1,clock_timestamp()-make_interval(secs=>%s))
            ON CONFLICT(id) DO UPDATE SET beat_at=EXCLUDED.beat_at''',(age_s,))


# spec: ANYM-518
def test_transports_pull_and_long_poll_follow_permissions(seeded):
    from test_event_delivery import identity
    s,_,_=seeded
    full=transports(s,identity(s))
    assert [n for n in full]==['pull','long_poll','webhook']
    assert full['pull']=={'name':'pull','tools':['fetch_subscription_events','ack_subscription_events'],
        'authorized':True,'configured':True,'operational':True,'reason':None}
    assert full['long_poll']['tools']==['wait_for_subscription_events'] and full['long_poll']['max_wait_s']==25
    assert full['long_poll']['authorized'] and full['long_poll']['reason'] is None
    # Missing permission: no subscription-ack, so pull is not authorized; wait needs read only.
    plain=['read','subscription-write']
    some=transports(s,s.issue_key('plain-reader',['telegram-demo'],permissions=plain,key_permissions=plain))
    assert some['pull']['authorized'] is False and some['pull']['reason']=='not_authorized'
    assert some['pull']['configured'] is True and some['long_poll']['authorized'] is True
    # The key ceiling counts as much as the principal's permissions.
    ceiling=transports(s,s.issue_key('ceiling-reader',['telegram-demo'],
        permissions=['read','subscription-write','subscription-ack'],key_permissions=['read']))
    assert not ceiling['pull']['authorized'] and not ceiling['webhook']['authorized']
    assert ceiling['webhook']['reason']=='not_authorized'
    # The old strings stay for compatibility.
    caps=s.capabilities(identity(s,'compat-reader'))
    assert caps['subscriptions']['push']=='operator_registered_signed_hints'
    assert caps['events']['delivery']=='operator_registered_signed_hints'


# spec: ANYM-518
def test_webhook_needs_a_bound_enabled_receiver_and_a_fresh_dispatcher(seeded,tmp_path):
    from messaging_service.event_delivery import disable_receiver
    from test_event_delivery import identity
    s,_,_=seeded
    token=identity(s)
    hook=transports(s,token)['webhook']
    # No endpoint.
    assert hook['tools']==['configure_subscription_delivery'] and hook['authorized'] is True
    assert (hook['configured'],hook['operational'],hook['reason'])==(False,False,'delivery_disabled')
    assert hook['registration']=='operator_only' and hook['next_step']=='ask the operator to register a receiver'
    rid=bound_receiver(s,token,tmp_path)
    # Absent heartbeat: no dispatcher has run.
    with s.pool.connection() as c:c.execute('DELETE FROM subscription_dispatcher_heartbeat')
    hook=transports(s,token)['webhook']
    assert (hook['configured'],hook['operational'],hook['reason'])==(True,False,'dispatcher_heartbeat_absent')
    # Stale heartbeat.
    heartbeat(s,121)
    hook=transports(s,token)['webhook']
    assert (hook['configured'],hook['operational'],hook['reason'])==(True,False,'dispatcher_heartbeat_stale')
    heartbeat(s,5)
    hook=transports(s,token)['webhook']
    assert (hook['configured'],hook['operational'],hook['reason'])==(True,True,None)
    # Another key of the same principal has no receiver of its own.
    assert transports(s,identity(s))['webhook']['reason']=='delivery_disabled'
    # Paused endpoint: the operator disabled the bound receiver.
    disable_receiver(s,rid)
    hook=transports(s,token)['webhook']
    assert (hook['configured'],hook['operational'],hook['reason'])==(False,False,'delivery_disabled')


# spec: ANYM-518
def test_dispatcher_pass_refreshes_the_heartbeat(seeded):
    from messaging_service.event_delivery import beat,dispatch_hints
    s,_,_=seeded
    with s.pool.connection() as c:c.execute('DELETE FROM subscription_dispatcher_heartbeat')
    dispatch_hints(s)
    with s.pool.connection() as c:
        row=c.execute('SELECT clock_timestamp()-beat_at<interval \'5 seconds\' AS fresh FROM subscription_dispatcher_heartbeat').fetchone()
    assert row['fresh']
    # A failed write (here: no table) never stops a pass.
    class Broken:
        class pool:
            @staticmethod
            def connection():
                import psycopg
                raise psycopg.OperationalError('synthetic')
    assert beat(Broken) is False


# spec: ANYM-518
def test_webhook_ignores_bindings_of_expired_or_changed_subscriptions(seeded,tmp_path):
    from test_event_delivery import identity
    s,_,_=seeded
    token=identity(s);rid=bound_receiver(s,token,tmp_path);heartbeat(s,5)
    assert transports(s,token)['webhook']['reason'] is None
    bound='WHERE id IN (SELECT subscription FROM subscription_delivery WHERE receiver=%s)'
    # Paused, then expired: capabilities never calls it configured, and never mutates it.
    with s.pool.connection() as c:
        c.execute("UPDATE subscriptions SET state='paused',expires_at=clock_timestamp()-interval '1 second' "+bound,(rid,))
    hook=transports(s,token)['webhook']
    assert (hook['configured'],hook['operational'],hook['reason'])==(False,False,'delivery_disabled')
    with s.pool.connection() as c:
        assert c.execute('SELECT state FROM subscriptions '+bound,(rid,)).fetchone()['state']=='paused'
        # Paused, not expired, but recorded under an older key permission version.
        c.execute('UPDATE subscriptions SET expires_at=NULL,key_permission_version=key_permission_version+1 '+bound,(rid,))
    assert transports(s,token)['webhook']['reason']=='delivery_disabled'


# spec: ANYM-518
def test_webhook_status_read_failure_keeps_capabilities(seeded,tmp_path):
    from test_event_delivery import identity
    s,_,_=seeded
    token=identity(s);bound_receiver(s,token,tmp_path);heartbeat(s,5)
    full=s.capabilities(token)
    # A failure confined to the delivery-status read (here: its table is gone).
    with s.pool.connection() as c:c.execute('ALTER TABLE subscription_dispatcher_heartbeat RENAME TO heartbeat_moved')
    try:
        caps=s.capabilities(token)
    finally:
        with s.pool.connection() as c:c.execute('ALTER TABLE heartbeat_moved RENAME TO subscription_dispatcher_heartbeat')
    rows={t['name']:t for t in caps['subscriptions']['transports']}
    assert (rows['webhook']['configured'],rows['webhook']['operational'],rows['webhook']['reason'])==(
        False,False,'delivery_status_unavailable')
    assert rows['pull']==next(t for t in full['subscriptions']['transports'] if t['name']=='pull')
    # Everything else is as before, including the operations the gateway admits by.
    assert {k:v for k,v in caps.items() if k!='subscriptions'}=={k:v for k,v in full.items() if k!='subscriptions'}
    assert s.capabilities(token)['subscriptions']['transports']==full['subscriptions']['transports']


# spec: ANYM-518
def test_locked_heartbeat_answers_capabilities_well_inside_the_discovery_deadline(seeded,tmp_path):
    import time
    from test_event_delivery import identity
    s,_,_=seeded
    token=identity(s);bound_receiver(s,token,tmp_path);heartbeat(s,5)
    full=s.capabilities(token)
    # Another session holds the heartbeat table; the gateway's discovery gives capabilities 2 s.
    with s.pool.connection() as holder:
        with holder.transaction():
            holder.execute('LOCK TABLE subscription_dispatcher_heartbeat IN ACCESS EXCLUSIVE MODE')
            started=time.monotonic();caps=s.capabilities(token);elapsed=time.monotonic()-started
    rows={t['name']:t for t in caps['subscriptions']['transports']}
    assert rows['webhook']['reason']=='delivery_status_unavailable' and rows['pull']['operational'] is True
    assert elapsed<1.5,elapsed
    assert {k:v for k,v in caps.items() if k!='subscriptions'}=={k:v for k,v in full.items() if k!='subscriptions'}
    # The caller's own limits are back after the diagnostic reads.
    from messaging_service.event_delivery import delivery_transports
    with s.authorized(token,'capabilities') as (c,p):
        c.execute("SET LOCAL statement_timeout='4s'")
        assert delivery_transports(c,p)==full['subscriptions']['transports']
        assert c.execute("SELECT current_setting('statement_timeout') AS t").fetchone()['t']=='4s'


def endpoint_policy(tmp_path,monkeypatch):
    import json
    path=tmp_path/'delivery-policy.json';path.write_text(json.dumps({'allow_hosts':['example.test']}));path.chmod(0o600)
    monkeypatch.setenv('DELIVERY_POLICY_FILE',str(path))


# spec: ANYM-519
def test_webhook_gate_off_is_delivery_disabled_even_with_a_bound_endpoint(seeded,tmp_path,monkeypatch):
    from test_delivery_endpoints import identity,register
    s,_,_=seeded
    endpoint_policy(tmp_path,monkeypatch);heartbeat(s,5)
    token=identity(s)
    s.set_delivery_features(True)
    try:
        endpoint=register(s,token)
        s.create_subscription(token,['telegram-demo-chat'],delivery={'endpoint_id':endpoint['endpoint_id']})
        assert transports(s,token)['webhook']['reason'] is None
    finally:
        s.set_delivery_features(False)
    hook=transports(s,token)['webhook']
    assert (hook['configured'],hook['operational'],hook['reason'])==(False,False,'delivery_disabled')
    assert hook['registration']=='operator_only' and hook['self_service']['enabled'] is False
    assert hook['self_service']['policy_set'] is True and hook['self_service']['max_endpoints']==4


# spec: ANYM-519
def test_webhook_policy_unset_then_a_bound_self_service_endpoint(seeded,tmp_path,monkeypatch):
    from test_delivery_endpoints import identity,register
    s,_,_=seeded
    monkeypatch.delenv('DELIVERY_POLICY_FILE',raising=False)
    heartbeat(s,5)
    s.set_delivery_features(True)
    try:
        token=identity(s)
        hook=transports(s,token)['webhook']
        assert (hook['configured'],hook['operational'],hook['reason'])==(False,False,'delivery_policy_unset')
        assert hook['registration']=='operator_only' and hook['self_service']['policy_set'] is False
        endpoint_policy(tmp_path,monkeypatch)
        hook=transports(s,token)['webhook']
        assert hook['reason']=='no_receiver_bound' and hook['registration']=='self_service'
        assert hook['self_service']['tools']==['register_delivery_endpoint','list_delivery_endpoints',
            'update_delivery_endpoint','delete_delivery_endpoint','test_delivery_endpoint','list_delivery_attempts']
        endpoint=register(s,token)
        s.create_subscription(token,['telegram-demo-chat'],delivery={'endpoint_id':endpoint['endpoint_id']})
        hook=transports(s,token)['webhook']
        assert (hook['configured'],hook['operational'],hook['reason'])==(True,True,None)
        s.update_delivery_endpoint(token,endpoint['endpoint_id'],1,'pause')
        hook=transports(s,token)['webhook']
        assert (hook['configured'],hook['operational'],hook['reason'])==(False,False,'receiver_disabled')
        caps=s.capabilities(token)
        assert {'register_delivery_endpoint','test_delivery_endpoint'}<=set(caps['available'])
        assert 'register_delivery_endpoint' not in s.capabilities(s.issue_key('endpoint-legacy',['telegram-demo']))['available']
    finally:
        s.set_delivery_features(False)


# spec: ANYM-520
def test_webhook_shows_the_keys_hourly_hint_budget_and_the_slots_left(seeded,tmp_path,monkeypatch):
    from test_event_delivery import endpoint_setup
    s,_,_=seeded
    monkeypatch.setenv('SUBSCRIPTION_MAX_HINTS_PER_HOUR','5')
    token,_,_,_=endpoint_setup(s,tmp_path,monkeypatch)
    try:
        # The matcher committed one hint for this key; another key has its whole budget.
        assert transports(s,token)['webhook']['hint_budget']=={'per_hour':5,'remaining_this_hour':4,
            'window':'rolling_hour'}
        other=s.issue_key('budget-other',['telegram-demo'],permissions=['read'])
        assert transports(s,other)['webhook']['hint_budget']['remaining_this_hour']==5
        # An admission counts in the same rolling hour; an hour later the slot is back.
        with s.pool.connection() as c:
            c.execute("UPDATE subscription_outbox SET created_at=clock_timestamp()-interval '61 minutes',"
                "admitted_at=clock_timestamp()")
        assert transports(s,token)['webhook']['hint_budget']['remaining_this_hour']==4
        with s.pool.connection() as c:
            c.execute("UPDATE subscription_outbox SET admitted_at=clock_timestamp()-interval '61 minutes'")
        assert transports(s,token)['webhook']['hint_budget']['remaining_this_hour']==5
    finally:
        s.set_delivery_features(False)


# spec: ANYM-534
def test_a_lane_worker_pulse_keeps_webhook_operational_for_120_seconds(seeded,tmp_path):
    import time
    from messaging_service import delivery_worker
    from test_event_delivery import identity
    s,_,_=seeded
    token=identity(s)
    bound_receiver(s,token,tmp_path)
    with s.pool.connection() as c:c.execute('DELETE FROM subscription_dispatcher_heartbeat WHERE id=1')
    now=time.monotonic()
    assert delivery_worker.pulse(s,{'operator':now,'endpoint-0':now},now=now)
    assert transports(s,token)['webhook']['operational'] is True
    heartbeat(s,delivery_worker.HEARTBEAT_S*3)
    assert transports(s,token)['webhook']['operational'] is True
    heartbeat(s,121)
    webhook=transports(s,token)['webhook']
    assert webhook['operational'] is False and webhook['reason']=='dispatcher_heartbeat_stale'
