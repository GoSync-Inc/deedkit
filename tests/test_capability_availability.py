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
    assert (hook['configured'],hook['operational'],hook['reason'])==(False,False,'no_receiver_bound')
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
    assert transports(s,identity(s))['webhook']['reason']=='no_receiver_bound'
    # Paused endpoint: the operator disabled the bound receiver.
    disable_receiver(s,rid)
    hook=transports(s,token)['webhook']
    assert (hook['configured'],hook['operational'],hook['reason'])==(False,False,'receiver_disabled')


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
