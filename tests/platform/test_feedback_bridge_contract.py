"""The feedback bridge reads what the real server answers, tool by tool (ANYM-990).

The bridge's unit tests run against `FakeGateway`, and a fake that invents a shape passes while the live install loops
(11-12: `messages_list_subscriptions` answers `{items, has_more}`, the fake answered `{subscriptions}`). These tests drive
the bridge against the real store with a key like the bridge's (no `read`), and hold every fake answer to the real one.
"""
import asyncio
import json
import time

import pytest

from messaging_service.store import APIError
from messaging_service.subscription_consumption import wait_for_subscription_events
from messaging_service.subscriptions import match_subscriptions
import scripts.feedback_bridge as fb
from test_event_delivery import identity
from test_feedback_bridge import KEY, SUB, FakeGateway, FakeTasks, fid, projection as fake_projection, row as fake_row
from test_feedback_bridge import isolate_dispatch_wake  # noqa: F401  (autouse fixture: no host dispatcher from fake tasks)
from test_feedback_events import submit
from test_feedback_maintainer_subscription import key, modules  # noqa: F401  (the fixture empties the feedback tables)
from test_feedback_export_metadata import projection as server_projection, record

READ, WRITE = 'feedback-maintainer-read', 'feedback-maintainer-write'
BRIDGE = [READ, WRITE, 'subscription-write', 'subscription-ack']  # the Ryzen bridge key: no `read`
MERGED = 'deedkit@' + 'a' * 40
# agent_gateway.diagnostics.CONSUMPTION_ERRORS (the codes a fetch, wait or ACK refusal keeps; any other becomes backend_denied)
RELAYED = frozenset({'unauthorized', 'forbidden', 'not_found', 'invalid_limit', 'invalid_version', 'invalid_receipt', 'stale_ack',
    'invalid_parameters', 'subscription_access_changed', 'subscription_closed', 'subscription_conflict'})
CONSUMPTION = frozenset({'fetch_subscription_events', 'ack_subscription_events', 'wait_for_subscription_events'})


class StoreGateway:
    """The gateway's REST answer over the real store: the tool's JSON, or the code and status the gateway relays
    (agent_gateway.http_clients: a backend 5xx is HTTP 503; a consumption tool keeps only its listed codes)."""

    def __init__(self, store, token):
        self.store, self.token, self.vanished = store, token, {}  # vanished: an item ID the server has not got, asked instead

    def call(self, tool, args, timeout=None):
        name = tool.removeprefix('messages_')
        try:
            if name == 'wait_for_subscription_events':
                result = asyncio.run(wait_for_subscription_events(self.store, self.token, **args | {'timeout': 1}))
            else:
                args = args | {'feedback_id': self.vanished.get(args['feedback_id'], args['feedback_id'])} if 'feedback_id' in args else args
                result = getattr(self.store, name)(self.token, **args)
        except APIError as error:
            if error.status >= 500:
                raise fb.Uncertain('gateway_http_503', 503) from None
            code = error.code if name not in CONSUMPTION or error.code in RELAYED else 'backend_denied'
            raise fb.Refused(code, error.status) from None
        return json.loads(json.dumps(result))


@pytest.fixture()
def server(modules):  # noqa: F811  (the imported fixture)
    store = modules[0]
    return store, key(store, 'bridge', BRIDGE)


def make(tmp_path, server, name='state', grants=None):
    store, token = server
    if grants is not None:
        token = key(store, 'bridge-' + name, grants)
    tasks = FakeTasks()
    cfg = fb.Config('https://agents.example', KEY, None, 'DK', 14, tmp_path / name)
    return fb.Bridge(cfg, StoreGateway(store, token), tasks, now=time.time), tasks


def status_of(store, fid_):
    with store.pool.connection() as c:
        return c.execute('SELECT status,spec_ref,status_changed_at FROM agent_feedback WHERE id=%s', (fid_,)).fetchone()


def task_for(tasks, feedback_id):
    return next(t for t in tasks.tasks.values() if fb.first_line_id(t['description']) == feedback_id)


def accept(tasks, feedback_id, status='todo'):
    tasks.human(task_for(tasks, feedback_id)['key'], status=status, labels=['feedback', 'spec:ANYM-990'])


# spec: ANYM-990
def test_a_lost_subscription_answer_is_adopted_through_the_real_listing_and_not_created_twice(tmp_path, server):
    store, token = server
    bridge, _ = make(tmp_path, server)
    sid = bridge.ensure_subscription()
    assert [s['subscription_id'] for s in store.list_subscriptions(token, limit=100)['items']] == [sid]
    again, _ = make(tmp_path, server, name='fresh')  # a ledger that lost the answer of its create
    again.ledger.set_meta('subscription_pending', '1')
    assert again.ensure_subscription() == sid
    assert len(store.list_subscriptions(token, limit=100)['items']) == 1


# spec: ANYM-990
def test_a_forward_cycle_on_the_real_server_makes_the_tasks_shows_the_release_and_acks_the_page(tmp_path, server):
    store, token = server
    bridge, tasks = make(tmp_path, server)
    agent = identity(store)
    history = submit(store, agent, 99)  # before the stream starts: metadata reads it, the task handler must ignore it
    sid = bridge.ensure_subscription()
    one, two = submit(store, agent, 1), submit(store, agent, 2)
    record(store, server_projection(two, primary_spec='ANYM-12'))
    store.set_feedback_status([one], 'fixed', spec_ref='ANYM-988', fixed_in=MERGED)
    match_subscriptions(store)
    assert bridge.forward() == {'created': 2, 'updated': 1, 'metadata_complete': True, 'missing': 0, 'held': 0}
    assert bridge.ledger.link(history) is None and bridge.ledger.meta('export_cursor') is None
    assert 'Fixed in: ' + MERGED + '\n' in task_for(tasks, one)['description']
    assert 'A synthetic problem in plain words.' in task_for(tasks, two)['description']
    assert store.fetch_subscription_events(token, sid)['module']['items'] == []  # the real server took the ACK
    assert bridge.forward() == {'created': 0, 'updated': 0, 'metadata_complete': True, 'missing': 0, 'held': 0}
    assert len(tasks.tasks) == 2 and bridge.ledger.meta('export_cursor') is None


# spec: ANYM-990
def test_reverse_on_the_real_server_accepts_with_its_card_and_declines_against_the_real_change_time(tmp_path, server):
    store, token = server
    bridge, tasks = make(tmp_path, server)
    bridge.ensure_subscription()
    agent = identity(store)
    one, two = submit(store, agent, 1), submit(store, agent, 2)
    store.set_feedback_status([two], 'accepted', spec_ref='ANYM-12')  # a status with a change time the bridge must hand back
    match_subscriptions(store)
    bridge.forward()
    accept(tasks, one)
    tasks.human(task_for(tasks, two)['key'], status='canceled')
    assert bridge.reverse() == {'written': 2}
    accepted = status_of(store, one)
    assert (accepted['status'], accepted['spec_ref']) == ('accepted', 'ANYM-990') and status_of(store, two)['status'] == 'declined'
    assert bridge.ledger.link(one)['fb_changed_at'] == accepted['status_changed_at'].isoformat()
    assert bridge.reverse() == {'written': 0}
    bridge.forward()  # the forward pass sees `accepted` and `declined` and leaves the tasks where the human put them
    assert [tasks.tasks[task_for(tasks, x)['key']]['status'] for x in (one, two)] == ['todo', 'canceled']


# spec: ANYM-990
def test_a_conflict_on_the_real_server_keeps_the_server_status_and_a_vanished_item_blocks_nothing(tmp_path, server):
    store, token = server
    bridge, tasks = make(tmp_path, server)
    bridge.ensure_subscription()
    agent = identity(store)
    first, second, gone = submit(store, agent, 1), submit(store, agent, 2), submit(store, agent, 3)
    match_subscriptions(store)
    bridge.forward()
    store.set_feedback_status([first], 'declined')  # the operator moves it after the bridge's last read
    bridge.gateway.vanished[gone] = fid(9)  # the server answers not_found, as it does for an item that is no longer there
    for feedback_id in (first, gone, second):
        accept(tasks, feedback_id)
    result = bridge.reverse()
    assert result == {'written': 1}
    assert [status_of(store, x)['status'] for x in (first, second)] == ['declined', 'accepted']
    assert any('not applied' in body for ref, body in tasks.comments if ref == task_for(tasks, first)['key'])
    assert bridge.reverse() == {'written': 0}


# spec: ANYM-990
def test_a_key_without_the_write_permission_leaves_reverse_idle_on_the_real_server(tmp_path, server):
    store, token = server
    bridge, tasks = make(tmp_path, server, name='reader', grants=[READ, 'subscription-write', 'subscription-ack'])
    bridge.ensure_subscription()
    one = submit(store, identity(store), 1)
    match_subscriptions(store)
    bridge.forward()
    accept(tasks, one)
    assert bridge.reverse() == {'written': 0, 'unavailable': 'forbidden'}
    assert status_of(store, one)['status'] == 'received'


def drift(real, fake, exact=(), path='$'):
    """What a fake answers that the server does not: an invented field or another type; under `exact` paths also a field
    the server sends and the fake leaves out. A null on either side fits any type."""
    if isinstance(real, dict) and isinstance(fake, dict):
        found = ['%s.%s is invented' % (path, key) for key in fake if key not in real]
        if path in exact:
            found += ['%s.%s is missing' % (path, key) for key in real if key not in fake]
        for key in fake.keys() & real.keys():
            found += drift(real[key], fake[key], exact, '%s.%s' % (path, key))
        return found
    if isinstance(real, list) and isinstance(fake, list):
        return drift(real[0], fake[0], exact, path + '[]') if real and fake else []
    if real is None or fake is None or type(real) is type(fake):
        return []
    return ['%s is %s, the fake says %s' % (path, type(real).__name__, type(fake).__name__)]


# spec: ANYM-990
def test_every_fake_gateway_answer_has_the_shape_the_real_server_sends(server):
    store, token = server
    real, fake = StoreGateway(store, token), FakeGateway()
    listing, create = ('messages_list_subscriptions', {'limit': 100}), (
        'messages_create_subscription', {'feedback': {'maintainer': True}, 'label': SUB})
    assert drift(real.call(*listing), fake.call(*listing), {'$'}) == []  # the empty listing: `{items, has_more}`
    made, imitated = real.call(*create), fake.call(*create)
    assert drift(made, imitated) == [] and {'subscription_id', 'label', 'state'} <= imitated.keys()
    listed, listed_fake = real.call(*listing), fake.call(*listing)
    assert drift(listed, listed_fake, {'$'}) == [] and listed['items'] and listed_fake['items']
    agent = identity(store)
    one, two = submit(store, agent, 1), submit(store, agent, 2)
    record(store, server_projection(two))
    match_subscriptions(store)
    fake.submit(fake_row(1))
    fake.submit(fake_row(2, projection=fake_projection()))
    fake.events.append(('feedback.projection_recorded', fid(2)))
    sid = listed['items'][0]['subscription_id']
    ask = {'subscription_id': sid, 'limit': 100, 'detail': 'compact'}
    page_paths = {'$', '$.subscription', '$.module', '$.positions', '$.module.items[]'}
    waited, imitated = real.call('messages_wait_for_subscription_events', ask | {'timeout': 25}), fake.call(
        'messages_wait_for_subscription_events', ask | {'timeout': 25})
    assert drift(waited, imitated, page_paths) == [] and waited['wait_status'] == imitated['wait_status'] == 'events_available'
    tool = 'messages_fetch_subscription_events'
    got, imitated = real.call(tool, ask), fake.call(tool, ask)
    assert drift(got, imitated, page_paths) == []
    assert {x['type'] for x in got['module']['items']} == {x['type'] for x in imitated['module']['items']}
    for item in got['module']['items']:  # the data of each event kind, key for key
        other = next(x for x in imitated['module']['items'] if x['type'] == item['type'] and x['subject'] in (fid(1), fid(2)))
        assert item['data'].keys() == other['data'].keys(), item['type']
    acked = real.call('messages_ack_subscription_events', {'subscription_id': sid, 'receipt': got['receipt'],
        'version': got['ack_version']})
    mirrored = fake.call('messages_ack_subscription_events', {'subscription_id': sid, 'receipt': 'r' * 43, 'version': 7})
    assert drift(acked, mirrored, {'$'}) == []
    assert drift(real.call(tool, ask), fake.call(tool, ask), page_paths) == []  # the page after the ACK: no receipt
    export = {'limit': 100}
    exported, imitated = real.call('messages_export_feedback_metadata', export), fake.call('messages_export_feedback_metadata', export)
    assert drift(exported, imitated, {'$', '$.items[]', '$.items[].projection'}) == []
    assert [x['projection'] is None for x in exported['items']] == [x['projection'] is None for x in imitated['items']]
    assert drift(exported['items'][1], imitated['items'][1], {'$', '$.projection'}) == []
    write = {'status': 'accepted', 'expected_status': 'received', 'expected_changed_at': None, 'request_id': 'request-0001',
        'spec_ref': 'ANYM-990'}
    wrote, mirrored = real.call('messages_set_feedback_triage', write | {'feedback_id': one}), fake.call(
        'messages_set_feedback_triage', write | {'feedback_id': fid(1)})
    assert drift(wrote, mirrored, {'$'}) == []
    again, mirrored = real.call('messages_set_feedback_triage', write | {'feedback_id': one}), fake.call(
        'messages_set_feedback_triage', write | {'feedback_id': fid(1)})
    assert drift(again, mirrored, {'$'}) == [] and again['replayed'] is mirrored['replayed'] is True


# spec: ANYM-990
def test_every_refusal_the_fake_raises_is_one_the_real_server_sends(server):
    store, token = server
    real, fake = StoreGateway(store, token), FakeGateway()
    bad = {'feedback_id': fid(9), 'status': 'accepted', 'expected_status': 'received', 'expected_changed_at': None,
        'request_id': 'request-0002'}
    for gateway in (real, fake):
        with pytest.raises(fb.Refused) as caught:
            gateway.call('messages_set_feedback_triage', bad)  # accepted without its card
        assert (caught.value.code, caught.value.status) == ('invalid_feedback', 400)
        with pytest.raises(fb.Refused) as caught:
            gateway.call('messages_set_feedback_triage', bad | {'spec_ref': 'ANYM-990'})  # no such item
        assert (caught.value.code, caught.value.status) == ('not_found', 404)
        with pytest.raises(fb.Refused) as caught:
            gateway.call('messages_export_feedback_metadata', {'limit': 101})
        assert (caught.value.code, caught.value.status) == ('invalid_limit', 400)
