"""Additional reviewed gateway contracts; synthetic data, never provider sessions."""
import asyncio
from datetime import datetime
import hashlib
import json
from types import SimpleNamespace

import pytest

from messaging_service.app import Boundary
from messaging_service.commands import CommandWorker, safe_result
from messaging_service.envelope import stable_id
from messaging_service.messenger_operations import operation_manifest, validate_operation
from messaging_service.outgoing import OutgoingWorker
from messaging_service.store import APIError, digest
from test_archive import event
from test_messenger_commands import operator, operation_args
from test_sending import sender, submit


@pytest.mark.parametrize('request_id', ['', 'short', 'x'*129, 'contains space', 'bad/request', True])
def test_send_action_id_is_bounded_before_persistence(sender, request_id):
    store, _, token, _, _ = sender
    with pytest.raises(APIError, match='^invalid_request_id$'):
        store.send_message(token, 'send-fixture', stable_id('chat', 'send-fixture', '100'),
                           'Synthetic draft', request_id)
    with store.pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM outgoing_messages').fetchone()['n'] == 0


def test_send_budget_replay_rotation_and_persisted_authority(sender):
    store, archive, token, _, state = sender
    first = submit(store, token)
    for number in range(1, 60):
        store.send_message(token, 'send-fixture', first['chat_id'], 'Synthetic draft',
                           f'bounded-send-{number:04d}')
    with pytest.raises(APIError, match='^rate_limited$') as failure:
        store.send_message(token, 'send-fixture', first['chat_id'], 'Synthetic draft', 'bounded-send-0060')
    assert failure.value.status == 429
    rotated = store.issue_key('send-agent', ['send-fixture'])
    archive.set_sending('send-fixture', False)
    state.phase('connection', 'retry_wait', 'provider_disconnected')
    assert submit(store, rotated)['send_id'] == first['send_id']
    assert first['automatic_retry'] is False and first['agent_send']['owner_approval_verified_by_server'] is False
    assert first['next_action'] == 'get_send_status'
    # SQL evaluates the two clock_timestamp() defaults independently; allow
    # 10 ms for those reads while verifying the ten-minute enqueue lifetime.
    lifetime = (datetime.fromisoformat(first['expires_at']) - datetime.fromisoformat(first['created_at'])).total_seconds()
    assert lifetime == pytest.approx(600, rel=0, abs=0.01)
    with store.pool.connection() as connection:
        row = connection.execute('SELECT * FROM outgoing_messages WHERE id=%s', (first['send_id'],)).fetchone()
    assert row['principal'] == 'send-agent'
    assert str(row['key_id']) == store.capabilities(token)['credential_id']
    assert row['scope_version'] == store.capabilities(token)['scope_version']
    assert row['key_permission_version'] == store.capabilities(token)['key_permission_version']
    assert row['provider_chat'] == '100'
    assert row['source_text'] == row['render_artifact']['source_text'] == 'Exact synthetic text'
    assert row['body'] == row['render_artifact']['provider_payload']['text'] == 'Exact synthetic text'
    assert row['render_policy'] == {'allow_lossy': False, 'link_preview': False}


def test_reply_target_must_be_live_in_the_destination_and_rechecked(sender):
    store, archive, token, _, state = sender
    foreign = archive.ingest(event(account='send-fixture', chat='200', provider_id='2'))['message']
    with pytest.raises(APIError, match='^not_found$'):
        submit(store, token, reply_to=foreign)
    own = stable_id('msg', 'send-fixture', '100', '1')
    receipt = submit(store, token, reply_to=own)
    archive.ingest(event(account='send-fixture', chat='100', provider_id='1', deleted=True,
                         body='', updated_at='2026-10-01T12:01:00Z'))
    calls = []
    async def provider(job):
        calls.append(job)
        return {'provider_message_id': '42'}
    assert asyncio.run(OutgoingWorker(state, provider).once())
    assert calls == []
    status = store.get_send_status(token, receipt['send_id'])
    assert status['status'] == 'failed' and status['error_code'] == 'send_target_invalid'
    with pytest.raises(APIError, match='^not_found$'):
        store.send_message(token, 'send-fixture', receipt['chat_id'], 'New draft', 'new-deleted-reply', reply_to=own)


def test_key_ceiling_update_is_independent_and_audited(seeded):
    store, first, _ = seeded
    second = store.issue_key('agent-a', ['telegram-demo', 'whatsapp-demo-1'])
    first_id = store.capabilities(first)['credential_id']
    second_before = store.capabilities(second)
    store.set_key_permissions(first_id, ['read'])
    assert store.capabilities(first)['permissions'] == ['read']
    assert store.capabilities(second)['permissions'] == second_before['permissions']
    assert store.capabilities(second)['key_permission_version'] == second_before['key_permission_version']
    keys = store.list_keys('agent-a')
    updated = next(key for key in keys if key['id'] == first_id)
    assert updated['permission_version'] == 2 and updated['permissions'] == ['read']
    assert first not in json.dumps(keys) and second not in json.dumps(keys)
    audit = next(row for row in store.audit_tail() if row['operation'] == 'set_key_permissions')
    assert audit['actor'] == 'operator' and audit['principal'] == 'agent-a'
    assert audit['key_id'] == first_id and audit['outcome'] == 'ok'


def test_expiring_credentials_store_only_digest_and_fail_when_expired(seeded):
    store, _, _ = seeded
    token = store.issue_key('short-lived-agent', ['telegram-demo'], days=1)
    public_id = store.capabilities(token)['credential_id']
    with store.pool.connection() as connection:
        row = connection.execute('SELECT * FROM api_keys WHERE id=%s', (public_id,)).fetchone()
        assert row['digest'] == digest(token) and token not in str(row)
        connection.execute("UPDATE api_keys SET expires_at=now()-interval '1 second' WHERE id=%s", (public_id,))
    with pytest.raises(APIError, match='^unauthorized$') as failure:
        store.search(token)
    assert failure.value.status == 401


def test_new_account_actions_start_disabled(seeded):
    from messaging_service.archive import Archive
    store, _, _ = seeded
    Archive(store).register('synthetic-opt-in', 'telegram', 'Synthetic opt-in', discover=True)
    with store.pool.connection() as connection:
        row = connection.execute('SELECT enabled,send_enabled,operations_enabled FROM account_policies WHERE account=%s',
                                 ('synthetic-opt-in',)).fetchone()
    assert row == {'enabled': True, 'send_enabled': False, 'operations_enabled': []}


def _invalidate_enqueue_gate(store, state, gate):
    if gate == 'disconnected':
        state.phase('connection', 'retry_wait', 'provider_disconnected')
    else:
        with store.pool.connection() as connection:
            if gate == 'collection_disabled':
                connection.execute('UPDATE account_policies SET enabled=false WHERE account=%s', (state.account,))
            elif gate == 'excluded_chat':
                connection.execute('UPDATE account_policies SET exclude_chats=%s WHERE account=%s', (['100'], state.account))
            elif gate == 'expired_owner':
                connection.execute("UPDATE collector_leases SET expires_at=now()-interval '1 second' WHERE account=%s", (state.account,))
            else:
                raise AssertionError('Unknown synthetic gate')


@pytest.mark.parametrize('gate, code', [('collection_disabled', 'sending_disabled'),
    ('excluded_chat', 'not_found'), ('disconnected', 'sender_not_ready'), ('expired_owner', 'sender_not_ready')])
def test_new_send_requires_current_collection_target_and_ready_owner(sender, gate, code):
    store, _, token, _, state = sender
    _invalidate_enqueue_gate(store, state, gate)
    with pytest.raises(APIError, match='^'+code+'$'):
        submit(store, token)
    with store.pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM outgoing_messages').fetchone()['n'] == 0


@pytest.mark.parametrize('gate, code', [('collection_disabled', 'operation_disabled'),
    ('excluded_chat', 'not_found'), ('disconnected', 'sender_not_ready'), ('expired_owner', 'sender_not_ready')])
def test_new_command_requires_current_collection_target_and_ready_owner(operator, gate, code):
    store, _, token, state = operator
    _invalidate_enqueue_gate(store, state, gate)
    with pytest.raises(APIError, match='^'+code+'$'):
        store.execute_messenger_operation(token, **operation_args(operation='get_message_info'), request_id='synthetic-ready-command')
    with store.pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n'] == 0


def test_successful_provider_send_survives_capture_failure_without_resend():
    from messaging_service.telegram_collector import TelegramCollector
    calls = []
    receipt = {'provider_message_id': '42', 'message': SimpleNamespace(id=42)}
    async def provider(job):
        calls.append('native-send')
        return receipt
    async def failed_capture(event):
        calls.append('capture')
        raise RuntimeError('synthetic capture database outage')
    collector = TelegramCollector.__new__(TelegramCollector)
    collector.provider = SimpleNamespace(send_text=provider)
    collector.capture = failed_capture
    collector.fail = lambda code: calls.append(code)
    assert asyncio.run(collector.send_outgoing({'id': 'synthetic-job'})) is receipt
    assert calls == ['native-send', 'capture', 'durable_capture_failed']


@pytest.mark.parametrize('code, next_action', [
    ('subscription_conflict', 'get_subscription_then_retry_with_current_version'),
    ('subscription_access_changed', 'create_new_subscription_after_checking_scope'),
    ('subscription_closed', 'create_new_subscription_after_checking_scope'),
    ('subscription_limit', 'ask_operator_to_review_subscription_budget'),
])
def test_subscription_errors_have_specific_non_retryable_recovery(code, next_action):
    from messaging_service.error_details import payload
    result = payload(APIError(code), 'create_subscription')
    assert result['error'] == code and result['next_action'] == next_action
    assert result['retryable'] is False and 'retry_after_seconds' not in result


def test_unexpected_rest_and_mcp_failures_return_safe_machine_errors(caplog):
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    private = 'PRIVATE_SQL select token from credentials PRIVATE_FILE /sessions/account'
    class Store:
        public_url = 'http://testserver'
        def authenticate(self, token): return 'synthetic-principal'
        def audit_boundary(self, code): pass
        def search(self, *args, **kwargs): raise RuntimeError(private)
    headers = {'Authorization': 'Bearer synthetic-token', 'Accept': 'application/json, text/event-stream'}
    with TestClient(create_app(Store(), ['testserver']), raise_server_exceptions=False) as client:
        rest = client.post('/v1/search', headers=headers, json={})
        mcp = client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'search', 'arguments': {}}})
    assert rest.status_code == 503
    detail = rest.json()
    assert detail['error'] == 'temporarily_unavailable' and detail['request_id']
    assert detail['results_state'] == 'not_returned_due_to_error'
    assert detail['next_action'] == 'narrow_account_chat_or_date_filters'
    assert rest.headers['X-Request-ID'] == detail['request_id']
    result = mcp.json()['result']
    assert result['isError'] is True
    assert 'temporarily_unavailable' in str(result) and 'request_id' in str(result)
    assert private not in rest.text + mcp.text + caplog.text
    assert 'PRIVATE_SQL' not in rest.text + mcp.text + caplog.text


def test_compact_sync_retains_actionable_observation_fields_without_mutating_cache():
    from messaging_service.presentation import render
    source = {'account': 'synthetic-account', 'platform': 'telegram', 'status': 'degraded',
              'detail_code': 'history_gap', 'archive_detail_code': 'collector_busy',
              'oldest_message_at': '2026-07-01T00:00:00Z', 'newest_message_at': '2026-10-15T00:00:00Z',
              'last_ingest_at': '2026-10-15T00:01:00Z', 'alerts': ['history_gap'],
              'freshness': {'state': 'recent_scan', 'last_successful_scan_at': '2026-10-15T00:00:00Z',
                            'scan_age_seconds': 60, 'basis': 'dialog_scan'},
              'history': {'observed_chats': 5, 'unknown_chats': 2, 'full_history_verified': False,
                          'gap_chats': 1, 'catchup_pending_chats': 2,
                          'configured_window': {'from_time': '2026-07-15T00:00:00Z'},
                          'last_scan_at': '2026-10-15T00:00:00Z'},
              'operations': {'media': {'state': 'retry_wait', 'reason_code': 'rate_limited',
                                      'pending_events': 3, 'collector_running': True,
                                      'retry_at': '2026-10-15T00:02:00Z', 'next_action': 'wait_for_retry'}},
              'files': {'download': {'queued': 3}, 'download_errors': {'rate_limited': 1},
                        'extraction': {'queued': 2}, 'enrichment': {'not_configured': 1}}}
    full = {'sources': [{**source, 'detail': {'long_diagnostics': 'Synthetic description'}}]}
    snapshot = json.dumps(full, sort_keys=True)
    compact = render('get_sync_status', full, 'compact')
    assert compact['sources'] == [source]
    assert compact['next_action'] == 'get_sync_status_detail_full_or_chat_id_for_diagnostics'
    assert compact['detail'] == 'compact' and render('get_sync_status', full, 'full') is full
    assert json.dumps(full, sort_keys=True) == snapshot


def test_unknown_view_is_rejected_without_search_dispatch():
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    calls = []
    class Store:
        public_url = 'http://testserver'
        def authenticate(self, token): return 'synthetic-principal'
        def audit_boundary(self, code): pass
        def search(self, *args, **kwargs): calls.append('must not dispatch')
    with TestClient(create_app(Store(), ['testserver'])) as client:
        response = client.post('/v1/search', headers={'Authorization': 'Bearer synthetic-token'},
                               json={'detail': 'invented-view'})
    assert response.status_code == 400 and response.json()['fields'] == ['detail']
    assert calls == []


def test_compact_chat_overview_omits_reply_only_fields_without_scope():
    from messaging_service.presentation import render
    full = {'items': [{'id': 'synthetic-chat', 'title': 'A chat', 'awaiting_reply_days': 14,
                       'awaiting_reply_basis': 'private_incoming', 'sync': {'private': 'detail'}}],
            'awaiting_reply_scope': None, 'awaiting_reply_total': 2,
            'awaiting_reply_days': 14, 'bot_filter_coverage': {'known': 1}}
    compact = render('list_chats', full, 'compact')
    assert set(compact['items'][0]) == {'id', 'title'}
    for field in ['awaiting_reply_scope', 'awaiting_reply_total', 'awaiting_reply_days', 'bot_filter_coverage']:
        assert field not in compact
    assert 'sync' in full['items'][0] and render('list_chats', full, 'full') is full


def _scope():
    return {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'method': 'POST', 'scheme': 'http', 'path': '/v1/search',
            'raw_path': b'/v1/search', 'root_path': '', 'query_string': b'',
            'client': ('127.0.0.1', 1234), 'server': ('testserver', 80),
            'headers': [(b'host', b'testserver'), (b'authorization', b'Bearer synthetic-token')]}


def test_slow_request_body_has_a_ten_second_deadline_and_never_reaches_handler(monkeypatch):
    timeouts, sent, called = [], [], []
    class Store:
        public_url = 'http://testserver'
        def authenticate(self, token): return 'synthetic-principal'
        def audit_boundary(self, code): pass
    async def handler(*args): called.append('handler')
    async def receive():
        await asyncio.Event().wait()
    async def send(message): sent.append(message)
    async def deadline(coroutine, timeout):
        timeouts.append(timeout)
        coroutine.close()
        raise TimeoutError('PRIVATE_DIAGNOSTIC_SENTINEL')
    monkeypatch.setattr(asyncio, 'wait_for', deadline)
    asyncio.run(Boundary(handler, Store(), ['testserver'])(_scope(), receive, send))
    assert timeouts == [10] and called == []
    assert sent[0]['status'] == 408
    body = json.loads(sent[1]['body'])
    assert body['error'] == 'body_timeout' and body['retryable'] is False
    assert 'PRIVATE_' not in str(sent)


def test_authenticated_boundary_rate_limit_is_scoped_and_does_not_dispatch_excess():
    sent, called = [], []
    class Store:
        public_url = 'http://testserver'
        def authenticate(self, token): return token
        def audit_boundary(self, code): pass
    async def handler(scope, receive, send):
        called.append(scope['headers'])
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'{}'})
    async def receive(): return {'type': 'http.request', 'body': b'{}', 'more_body': False}
    async def send(message): sent.append(message)
    boundary = Boundary(handler, Store(), ['testserver'])
    async def requests():
        for _ in range(181): await boundary(_scope(), receive, send)
        another = _scope()
        another['headers'] = [(b'host', b'testserver'), (b'authorization', b'Bearer different-synthetic-token')]
        await boundary(another, receive, send)
    asyncio.run(requests())
    statuses = [message['status'] for message in sent if message['type'] == 'http.response.start']
    assert statuses == [200]*180 + [429, 200]
    assert len(called) == 181


def _shared_peer_boundary():
    """Synthetic agents behind one gateway address; 'revoked' keys fail authentication."""
    authenticated, called = [], []
    class Store:
        public_url = 'http://testserver'
        def authenticate(self, token):
            authenticated.append(token)
            if token.startswith('revoked'): raise APIError('unauthorized', 401)
            return token
        def audit_boundary(self, code): pass
    async def handler(scope, receive, send):
        called.append(scope['path'])
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'{}'})
    boundary = Boundary(handler, Store(), ['testserver'])
    async def request(token, path='/v1/search', body=b'{}', method='POST'):
        sent = []
        scope = {**_scope(), 'method': method, 'path': path, 'raw_path': path.encode()}
        scope['headers'] = [(b'host', b'testserver')]
        if token: scope['headers'].append((b'authorization', b'Bearer ' + token.encode()))
        async def receive(): return {'type': 'http.request', 'body': body, 'more_body': False}
        async def send(message): sent.append(message)
        await boundary(scope, receive, send)
        return sent[0]['status']
    return request, authenticated, called


def test_agents_behind_one_gateway_address_have_separate_budgets():
    request, authenticated, called = _shared_peer_boundary()
    async def run():
        # More than the old shared 600/min peer budget, from one address.
        statuses = [await request(f'agent-{n}') for n in range(4) for _ in range(180)]
        assert statuses == [200]*720
        assert await request('agent-0') == 429
        assert await request('agent-4') == 200
    asyncio.run(run())
    assert len(called) == 721


def test_gateway_upload_with_capability_prechecks_fits_principal_budget():
    request, _, called = _shared_peer_boundary()
    chunk = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                        'params': {'name': 'upload_file_chunk', 'arguments': {}}}).encode()
    async def run():
        # A 32 MiB upload through the gateway: a capabilities pre-check before
        # prepare, each of 128 chunks and the final status read.
        statuses = []
        for path in ['/v1/prepare_file_upload'] + ['/v1/upload_file_chunk']*128 + ['/v1/get_file_upload']:
            statuses.append(await request('uploader', '/v1/capabilities'))
            statuses.append(await request('uploader', path))
        assert statuses == [200]*260
        # The general budget is untouched: 2 of 180 used.
        assert [await request('uploader') for _ in range(178)] == [200]*178
        assert await request('uploader') == 429
        # Chunk calls share one budget across REST and MCP: two full uploads a minute.
        assert [await request('uploader', '/v1/upload_file_chunk') for _ in range(127)] == [200]*127
        assert await request('uploader', '/mcp', chunk) == 200
        assert await request('uploader', '/mcp', chunk) == 429
        assert await request('uploader', '/v1/upload_file_chunk') == 429
        assert await request('uploader', '/v1/capabilities') == 200
    asyncio.run(run())


def test_unauthenticated_work_remains_bounded_per_peer_before_database_access():
    request, authenticated, called = _shared_peer_boundary()
    async def run():
        statuses = [await request('revoked-synthetic-key') for _ in range(590)]
        statuses += [await request('') for _ in range(5)]
        statuses += [await request(None, '/v1/download/' + 'a'*43, b'', 'GET') for _ in range(5)]
        assert statuses == [401]*595 + [200]*5
        authenticated.clear()
        assert await request('revoked-synthetic-key') == 429
        assert await request(None, '/v1/download/' + 'a'*43, b'', 'GET') == 429
        # Rejected traffic from this peer also blocks its valid agents until the
        # window resets: bounding unauthenticated work requires a peer limit.
        assert await request('agent-0') == 429
        assert authenticated == []
    asyncio.run(run())
    assert called == ['/v1/download/' + 'a'*43]*5


@pytest.mark.parametrize('request_id', ['', 'short', 'x'*129, 'bad/request', True])
def test_command_action_id_is_bounded_before_persistence(operator, request_id):
    store, _, token, _ = operator
    with pytest.raises(APIError, match='^invalid_request_id$'):
        store.execute_messenger_operation(token, **operation_args(), request_id=request_id)
    with store.pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n'] == 0


def test_command_budget_bounds_new_work_but_preserves_exact_receipt(operator):
    store, _, token, _ = operator
    args = operation_args(operation='get_message_info')
    first = store.execute_messenger_operation(token, **args, request_id='bounded-command-0000')
    for number in range(1, 60):
        store.execute_messenger_operation(token, **args, request_id=f'bounded-command-{number:04d}')
    with pytest.raises(APIError, match='^rate_limited$') as failure:
        store.execute_messenger_operation(token, **args, request_id='bounded-command-0060')
    assert failure.value.status == 429
    assert store.execute_messenger_operation(token, **args, request_id='bounded-command-0000')['operation_id'] == first['operation_id']
    # As for sends, separate SQL clock reads need not be microsecond-identical.
    lifetime = (datetime.fromisoformat(first['expires_at']) - datetime.fromisoformat(first['created_at'])).total_seconds()
    assert lifetime == pytest.approx(600, rel=0, abs=0.01)
    with pytest.raises(APIError, match='^invalid_operation_parameters$'):
        store.execute_messenger_operation(token, **operation_args(parameters={'text': 'x'*16001}),
                                         request_id='oversized-command')


def test_command_dispatch_honors_cooldown_creation_order_and_audit(operator):
    store, _, token, state = operator
    args = operation_args(operation='get_message_info')
    first = store.execute_messenger_operation(token, **args, request_id='ordered-command-0001')
    second = store.execute_messenger_operation(token, **args, request_id='ordered-command-0002')
    calls = []
    async def provider(job):
        calls.append(job['id'])
        return {'outcome': 'confirmed'}
    worker = CommandWorker(state, provider)
    state.defer(30)
    assert not asyncio.run(worker.once()) and calls == []
    assert store.get_messenger_operation(token, first['operation_id'])['status'] == 'queued'
    with store.pool.connection() as connection:
        connection.execute("UPDATE collector_health SET cooldown_until=now()-interval '1 second' WHERE account=%s", (state.account,))
    assert asyncio.run(worker.once()) and asyncio.run(worker.once()) and not asyncio.run(worker.once())
    assert calls == [first['operation_id'], second['operation_id']]
    audit = [row for row in store.audit_tail() if row['operation'] == 'messenger_dispatch']
    assert len(audit) == 2 and all(row['actor'] == 'collector' and row['outcome'] == 'ok' for row in audit)


@pytest.mark.parametrize('kind', ['empty', 'oversized', 'symlink'])
def test_registered_file_preflight_rejects_empty_oversized_or_symlink_originals(tmp_path, kind):
    from messaging_service.commands import _registered_file
    content = b'Synthetic original'
    sha = hashlib.sha256(content).hexdigest()
    row = {'id': 'synthetic-file', 'message': 'synthetic-message', 'digest': sha,
           'account': 'synthetic-account', 'chat': 'synthetic-chat', 'bytes': len(content)}
    if kind == 'empty': row['bytes'] = 0
    elif kind == 'oversized': row['bytes'] = 32*1024**2 + 1
    else:
        actual = tmp_path/'actual'
        actual.write_bytes(content)
        (tmp_path/sha).symlink_to(actual)
    class Connection:
        def execute(self, *args): return self
        def fetchone(self): return row
    with pytest.raises(APIError, match='^attachment_unavailable$'):
        _registered_file(SimpleNamespace(files=tmp_path), Connection(),
                         {'accounts': ['synthetic-account'], 'chat_ids': ['synthetic-chat']},
                         'synthetic-account', {'attachment_id': row['id'], 'sha256': sha}, verify=True)


@pytest.mark.parametrize('data', [
    {'nested': [[[[[[[[[1]]]]]]]]]}, {'items': list(range(101))},
    {'number': float('nan')}, {'number': float('inf')}, {'number': 2**64},
    {'text': 'bad\0value'}, {'traceback': 'private'}, {'session': 'private'},
    {'lots': ['x'*4096]*15},
])
def test_command_results_reject_unsafe_depth_size_values_and_diagnostics(data):
    with pytest.raises(ValueError, match='^invalid_operation_result$'):
        safe_result({'outcome': 'confirmed', 'data': data})


def test_non_file_command_timeout_and_unconfirmed_read_are_not_success(monkeypatch):
    pending, finishes, timeouts = [True], [], []
    class Worker(CommandWorker):
        def claim(self):
            if not pending: return None
            pending.pop()
            return {'id': 'synthetic-command', 'mutation': False, 'operation': 'chat_info'}
        def preflight(self, job, *, verify_file=True): return job
        def finish(self, job, status, result=None, code=None): finishes.append((status, code))
    original = asyncio.wait_for
    async def observe(task, timeout):
        timeouts.append(timeout)
        return await original(task, timeout)
    monkeypatch.setattr(asyncio, 'wait_for', observe)
    async def provider(job): return {'outcome': 'submitted'}
    worker = Worker(SimpleNamespace(), provider)
    assert asyncio.run(worker._once()) and not asyncio.run(worker._once())
    assert timeouts == [30] and finishes == [('failed', 'operation_result_unknown')]


@pytest.mark.parametrize('receipt', [None, {}, {'provider_message_id': ''}, {'provider_message_id': 42}])
def test_missing_send_receipt_is_unknown_and_has_no_second_attempt(receipt, monkeypatch):
    pending, finishes, timeouts, calls = [True], [], [], []
    class Worker(OutgoingWorker):
        def claim(self):
            if not pending: return None
            pending.pop()
            return {'id': 'synthetic-send'}
        def preflight(self, job): return job
        def finish(self, job, status, provider_id=None, code=None): finishes.append((status, code))
    original = asyncio.timeout
    def observe(delay):
        timeouts.append(delay)
        return original(delay)
    monkeypatch.setattr(asyncio, 'timeout', observe)
    async def provider(job):
        calls.append('native-send')
        return receipt
    worker = Worker(SimpleNamespace(), provider)
    assert asyncio.run(worker._once()) and not asyncio.run(worker._once())
    assert calls == ['native-send'] and timeouts == [30]
    assert finishes == [('unknown', 'send_receipt_missing')]


def test_catalog_covers_explicit_families_and_provider_differences():
    telegram = {row['operation']: row for row in operation_manifest('telegram')}
    whatsapp = {row['operation']: row for row in operation_manifest('whatsapp')}
    families = {
        'read-live': ['chat_info', 'get_reactions', 'get_message_info', 'get_pinned_messages', 'request_chat_history',
                      'search_messages_live'],
        'groups-read': ['list_participants', 'list_topics', 'list_join_requests', 'check_chat_link'],
        'profile-read': ['get_profile'], 'contacts-read': ['list_contacts', 'get_contact', 'list_blocked', 'get_labels'],
        'send': ['send_file', 'forward_message', 'send_poll', 'send_contact', 'send_location', 'open_chat_by_phone',
                 'open_chat_by_username'],
        'message-edit': ['edit_message'], 'message-delete': ['delete_message'],
        'message-react': ['send_reaction', 'vote_poll'], 'message-pin': ['pin_message', 'unpin_message'],
        'read-state': ['mark_read', 'mark_unread'],
        'chat-manage': ['archive_chat', 'unarchive_chat', 'mute_chat', 'unmute_chat', 'pin_chat', 'unpin_chat',
                        'star_message', 'unstar_message', 'set_chat_labels'],
        'groups-manage': ['create_group', 'rename_group', 'set_group_description', 'add_participant',
                          'remove_participant', 'promote_participant', 'demote_participant', 'leave_group',
                          'get_invite_link', 'revoke_invite_link', 'set_group_messages_admins_only',
                          'set_group_info_admins_only', 'set_slow_mode', 'set_join_approval',
                          'approve_join_requests', 'reject_join_requests', 'join_chat'],
        'contacts-manage': ['add_contact', 'delete_contact', 'block_contact', 'unblock_contact'],
        'profile-manage': ['update_profile'],
    }
    assert set(telegram) == set(whatsapp) == {name for names in families.values() for name in names}
    for permission, names in families.items():
        for name in names:
            assert telegram[name]['permission'] == whatsapp[name]['permission'] == permission
            for row in (telegram[name], whatsapp[name]):
                assert row['target'] in {'account', 'chat', 'message'}
                assert type(row['mutation']) is bool and type(row['full_account']) is bool
                assert isinstance(row['parameters_schema'], dict) and isinstance(row['limitation'], str)
    for name in ['get_profile', 'list_contacts', 'get_contact', 'list_blocked', 'add_contact',
                 'delete_contact', 'block_contact', 'unblock_contact', 'update_profile', 'create_group']:
        assert telegram[name]['full_account'] and whatsapp[name]['full_account']
    for name in ['list_topics', 'vote_poll', 'search_messages_live']:
        assert telegram[name]['supported'] and not whatsapp[name]['supported']
    for name in ['get_pinned_messages', 'pin_chat', 'unpin_chat', 'star_message', 'unstar_message',
                 'get_labels', 'set_chat_labels']:
        assert whatsapp[name]['supported'] and not telegram[name]['supported']
    assert not whatsapp['delete_message']['supported'] and 'local deletion' in whatsapp['delete_message']['limitation']
    assert set(telegram['update_profile']['parameters_schema']) == {'first_name', 'last_name', 'bio'}
    assert set(whatsapp['update_profile']['parameters_schema']) == {'name', 'status'}
    assert set(telegram['send_contact']['parameters_schema']) == {'phone', 'first_name', 'last_name'}
    assert set(whatsapp['send_contact']['parameters_schema']) == {'contact_ids'}
    for catalog in (telegram, whatsapp):
        assert set(catalog['edit_message']['parameters_schema']) == {'text'}
        assert 'Own outgoing' in catalog['edit_message']['limitation']
        assert 'exact archive revision' in catalog['edit_message']['limitation']
        assert 'plain text only' in catalog['edit_message']['limitation']
        assert set(catalog['forward_message']['parameters_schema']) == {'source_message_id', 'source_revision'}
        assert set(catalog['send_file']['parameters_schema']) == {'attachment_id', 'upload_id', 'sha256', 'mode', 'caption'}
        assert catalog['send_file']['parameters_schema']['sha256']['required'] is True
        assert '32 MiB' in catalog['send_file']['limitation']
    for platform in ('telegram','whatsapp'):
        for source in ({'attachment_id':'att_'+'a'*32},{'upload_id':'up_'+'c'*32}):
            parameters=validate_operation(platform,'send_file',{**source,'sha256':'b'*64})
            assert all(parameters[key]==value for key,value in source.items())
            assert parameters['sha256']=='b'*64
        for source in ({},{'attachment_id':'att_'+'a'*32,'upload_id':'up_'+'c'*32}):
            with pytest.raises(ValueError,match='^invalid_operation_parameters$'):
                validate_operation(platform,'send_file',{**source,'sha256':'b'*64})


@pytest.mark.parametrize('operation, parameters', [
    ('list_participants', {'limit': 0}), ('list_participants', {'offset': 100001}),
    ('send_poll', {'question': 'Q', 'options': ['one']}),
    ('send_poll', {'question': 'Q', 'options': [str(i) for i in range(11)]}),
    ('send_poll', {'question': 'Q', 'options': ['one', '']}),
    ('send_location', {'latitude': 91, 'longitude': 0}),
    ('send_location', {'latitude': 0, 'longitude': -181}),
    ('send_location', {'latitude': 0, 'longitude': float('inf')}),
    ('rename_group', {'title': '😀'*65}), ('update_profile', {}),
    ('create_group', {'title': 'Q', 'user_ids': ['123', '123']}),
    ('vote_poll', {'option_indices': [1, 1]}),
    ('edit_message', {'text': 'Synthetic', 'format': 'html'}),
    ('send_file', {'attachment_id': 'att_'+'a'*32, 'sha256': 'b'*64, 'mode': 'execute'}),
])
def test_catalog_rejects_semantic_limit_violations(operation, parameters):
    with pytest.raises(ValueError, match='^invalid_operation_parameters$'):
        validate_operation('telegram', operation, parameters)
