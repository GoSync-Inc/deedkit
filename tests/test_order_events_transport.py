"""Real metadata/control/feed/gateway/outbox chain; fake external egress/native reply only."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from messaging_service import android_wake as server
from messaging_service import heartbeats
from messaging_service.event_delivery import dispatch_hints
from messaging_service.store import APIError
from test_delivery_vendor import PUBLIC, gate, identity, policy, register  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def scoped_helpers():
    """Unique package bindings, no sys.path edits and no removal of another importer's modules."""
    prefix = '_dk7_' + uuid4().hex
    before_path = sys.path[:]
    package = ROOT / 'services/android/src/deedkit_android'
    spec = importlib.util.spec_from_file_location(prefix, package / '__init__.py', submodule_search_locations=[str(package)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[prefix] = module
    try:
        spec.loader.exec_module(module)
        events = importlib.import_module(prefix + '.order_events')
        control = importlib.import_module(prefix + '.order_events_control')
        feed = importlib.import_module(prefix + '.order_events_feed')
        name = prefix + '_gateway'
        gateway_spec = importlib.util.spec_from_file_location(name, ROOT / 'services/gateway/src/agent_gateway/android_wake_bridge.py')
        gateway = importlib.util.module_from_spec(gateway_spec)
        sys.modules[name] = gateway
        gateway_spec.loader.exec_module(gateway)
        yield SimpleNamespace(events=events, control=control, feed=feed, gateway=gateway)
    finally:
        for name in list(sys.modules):
            if name == prefix or name.startswith(prefix + '.') or name == prefix + '_gateway':
                del sys.modules[name]
        assert sys.path == before_path
        assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules)


@pytest.fixture
def chain(request, tmp_path):
    request.getfixturevalue('policy')
    store = request.getfixturevalue('gate')
    token = identity(store, 'native-owner')
    endpoint = register(store, token)
    # Existing actual registration/test fences run against this disposable DB, fake public DNS/egress only.
    assert dispatch_hints(store, transport=lambda *a, **kw: 200, resolver=PUBLIC)['endpoint_tests']['delivered'] == 1
    for component in heartbeats.COMPONENTS:
        assert heartbeats.record(store, component)
    with store.authorized(token) as (c, actor):
        c.execute('''INSERT INTO android_wake_bindings(principal,key_id,device_id,owner_id,source_id,auth_epoch,
            endpoint,state,routine_verified,installed_verified,valid_until) VALUES(%s,%s,'phone','native-owner',
            'native',1,%s,'active',true,true,clock_timestamp()+interval '1 hour')''',
            (actor['id'], actor['key_id'], endpoint['endpoint_id']))
    with scoped_helpers() as modules:
        principal = SimpleNamespace(id='native-owner')
        def authorize(p, owner, device, order, source, epoch):
            return getattr(p, 'id', p) == owner == 'native-owner' and device == 'phone' and source == 'native' and epoch == 1
        events = modules.events.OrderEvents(sqlite3.connect(tmp_path / 'source.sqlite'), authorize)
        control = modules.control.OrderControl(events, source_id='native', auth_epoch=1,
                                              original_words=lambda c: 'Keep my original words before any order exists.')
        feed = modules.feed.OrderFeed(control)
        # Pinned item/tool/call fixture: actual call/RPC IDs; no fabricated native item.
        context = modules.control.NativeOrderContext(principal, 'native-owner', 'phone', 'task-preorder',
            'request-preorder', 'original-thread', 'original-turn', 'actual-call', 42)
        route = modules.gateway.Route('native-owner', 'phone', 'native', 1, enabled=True)
        yield SimpleNamespace(store=store, token=token, modules=modules, principal=principal,
                              events=events, control=control, feed=feed, context=context, route=route)
        events.db.close()


async def produce(chain, *, lost_ingestion_reply=False):
    async def cursor(device): return server.call(chain.store, chain.token, 'state', {'device_id': device})
    async def source(after, limit): return chain.feed.read_source(chain.principal, 'native-owner', 'phone', after, limit)
    async def receipt(wake): return server.call(chain.store, chain.token, 'receipt', {'wake_id': wake})
    async def stage(payload):
        result = server.call(chain.store, chain.token, 'stage', payload)
        if lost_ingestion_reply:
            raise TimeoutError('synthetic lost ingestion reply after commit')
        return result
    return await chain.modules.gateway.step(chain.route, read_cursor=cursor, read_source=source,
                                             stage=stage, receipt=receipt)


# spec: ANYM-997
def test_preorder_async_question_crosses_actual_projection_outbox_fetch_and_original_native_reply(chain):
    ctx = replace(chain.context, native_call_id=None, native_request_id=None)
    questions = [{'title': 'Which address before checkout?', 'options': None}]
    wake = chain.control.observe_native_question(ctx, item_id='actual-question-item', message_id='actual-question-message',
                                                 questions=questions)
    assert asyncio.run(produce(chain))['status'] == 'staged'
    attempts = []
    def http(url, raw, headers, **kwargs):
        attempts.append(json.loads(raw))
        assert headers['webhook-id'] == wake
        return 200
    assert server.dispatch(chain.store, transport=http, resolver=PUBLIC) == {'delivered': 1}
    assert attempts == [dict(module='android', type='android_wake', wake_id=wake, kind='question', next_call='android_agent')]
    # Transport acceptance never claims owner read, native input acceptance, basket approval or goal completion.
    assert chain.events.wake(wake)['state'] == 'pending'
    full = chain.feed.fetch(chain.principal, 'native-owner', 'phone', wake)
    assert full['metadata']['order_id'] is None and full['origin']['task_id'] == 'task-preorder'
    assert full['payload']['questions'] == questions
    read = chain.feed.ack_read(chain.principal, 'native-owner', 'phone', wake)
    assert read['native_status'] == 'not_observed'
    replies = []
    async def native_reply(**params):
        replies.append(params)
        return dict(status='unknown', native_state='submitted', submission_intent=True)
    reply = asyncio.run(native_reply(request_id=full['metadata']['request_id'], device_id='phone',
        thread_id=full['metadata']['native_thread_id'], turn_id=full['metadata']['native_turn_id'],
        item_id=full['metadata']['item_id'], message_id='owner-answer-one', text='Use the address from my task.'))
    assert replies == [dict(request_id='request-preorder', device_id='phone', thread_id='original-thread',
                            turn_id='original-turn', item_id='actual-question-item', message_id='owner-answer-one',
                            text='Use the address from my task.')]
    assert full['metadata']['native_request_id'] is None and full['metadata']['question_ids'] == []
    assert reply['status'] == 'unknown' and read['native_status'] == 'not_observed'


# spec: ANYM-997
def test_lost_ingestion_external_reply_and_changed_rpc_cannot_duplicate_one_original_native_notice(chain):
    notice = {'message': 'Agent-chosen preorder status'}
    wake = asyncio.run(chain.control(chain.context, 'notify', notice))['wake_id']
    refused = False
    try:
        asyncio.run(chain.control(replace(chain.context, native_request_id='42'), 'notify', notice))
    except chain.modules.events.OrderRefused as error:
        refused = str(error) == 'control_identity_conflict'
    first = asyncio.run(produce(chain, lost_ingestion_reply=True))
    assert first['status'] == 'unknown' and first['items'][0]['wake_id'] == wake
    assert refused
    assert asyncio.run(produce(chain))['status'] == 'readback'
    attempts = []
    def accepted_lost(*args, **kwargs):
        attempts.append(json.loads(args[1]))
        raise TimeoutError('synthetic accepted external request with lost reply')
    server.dispatch(chain.store, transport=accepted_lost, resolver=PUBLIC)
    server.dispatch(chain.store, transport=accepted_lost, resolver=PUBLIC)
    receipt = server.call(chain.store, chain.token, 'receipt', {'wake_id': wake})
    assert len(attempts) == 1 and receipt['transport_state'] == 'unknown'
    assert receipt['native_status'] == 'not_observed'
    with chain.store.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM android_wake_intents').fetchone()['n'] == 1
    assert chain.feed.fetch(chain.principal, 'native-owner', 'phone', wake)['payload']['message'] == notice['message']


# spec: ANYM-997
def test_actual_chain_refuses_unrelated_owner_device_and_never_moves_an_existing_source_cursor(chain):
    wake = asyncio.run(chain.control(chain.context, 'notify', {'message': 'Original owner only'}))['wake_id']
    other = identity(chain.store, 'unrelated-owner')
    assert server.call(chain.store, other, 'receipt', {'wake_id': wake}) == {'transport_state': 'not_found'}
    with pytest.raises(chain.modules.events.OrderRefused):
        chain.feed.fetch(SimpleNamespace(id='unrelated-owner'), 'native-owner', 'phone', wake)
    with pytest.raises(chain.modules.events.OrderRefused):
        chain.feed.fetch(chain.principal, 'native-owner', 'different-phone', wake)
    page = chain.feed.read_source(chain.principal, 'native-owner', 'phone', 0, 4)
    with pytest.raises(APIError, match='android_wake_unavailable'):
        server.call(chain.store, other, 'stage', dict(device_id='phone', journal_id=page['journal_id'],
            after=0, next_after=1, events=page['events']))
    assert server.call(chain.store, chain.token, 'state', {'device_id': 'phone'})['after'] == 0


# spec: ANYM-997
def test_actual_handoff_hint_read_and_explicit_ack_preserve_nextcheck_while_a_new_task_runs(chain):
    at = time.time() + 300
    ref = asyncio.run(chain.control(chain.context, 'bind', {'next_check_at': at, 'state': {'pending': 'long order'}}))['obligation_ref']
    handctx = replace(chain.context, native_call_id='handoff-call', native_request_id=43)
    # This synthetic routine has the same authenticated Android owner; responsibility is a separate explicit role.
    wake = asyncio.run(chain.control(handctx, 'handoff', {'obligation_ref': ref, 'target_principal': 'native-owner'}))['wake_id']
    assert asyncio.run(produce(chain))['status'] == 'staged'
    server.dispatch(chain.store, transport=lambda *a, **kw: 200, resolver=PUBLIC)
    content = chain.feed.fetch(chain.principal, 'native-owner', 'phone', wake)
    assert chain.events._order('native-owner', ref)['responsible'] == 'codex'
    chain.feed.ack_read(chain.principal, 'native-owner', 'phone', wake)
    assert chain.events._order('native-owner', ref)['responsible'] == 'codex'
    newer = replace(handctx, task_id='new-task', request_id='new-request', native_turn_id='new-turn',
                    native_call_id='new-task-call', native_request_id=44)
    other = asyncio.run(chain.control(newer, 'bind', {'next_check_at': at+30, 'state': {}}))['obligation_ref']
    chain.feed.ack_handoff(chain.principal, 'native-owner', 'phone', wake,
                           snapshot_digest=content['payload']['snapshot_digest'], next_check_at=at)
    assert chain.events._order('native-owner', ref)['responsible'] == 'grok'
    assert chain.events._order('native-owner', ref)['next_check_at'] == at
    assert chain.events._order('native-owner', other)['responsible'] == 'codex'
    assert server.call(chain.store, chain.token, 'receipt', {'wake_id': wake})['native_status'] == 'not_observed'
