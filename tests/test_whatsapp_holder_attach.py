"""ANYM-937 part 2: the WhatsApp collector attached to its session holder (COLLECTORS.md, Session holders).

A fake holder (an asyncio unix server speaking the holder's frames) drives the collector's side; the real
`anymsg-whatsmeow --serve` binary checks the handshake and the lease across both languages. No provider: nothing here
calls the holder's `start` against the real binary, so no WhatsApp contact can happen.
"""
import asyncio
import json
import tempfile
import time
from pathlib import Path

import pytest

from messaging_service import whatsapp_holder as wh
from messaging_service.whatsapp_collector import configuration
from test_messenger_commands import operation_args, operator, queue  # noqa: F401  (fixture)
from test_session_holder_role import holders  # noqa: F401  (fixture)
from test_whatsmeow_contract import native_fixture_binary  # noqa: F401  (fixture)
from test_whatsmeow_holder_claim import holder_login  # noqa: F401  (fixture)

ACCOUNT = 'whatsapp-demo-1'
METHODS = ['start', 'policy', 'dialogs', 'page', 'media', 'request_history', 'gap_repair', 'gap_repair_status',
           'send_text', 'messenger_operation']
POLICY = {'enabled': True, 'send_enabled': False, 'operations_enabled': ['delete_message'], 'discover_chats': True,
          'include_chats': [], 'exclude_chats': ['555@g.us']}
ROW = 'op_' + 'a' * 32


def private_dir():
    # A unix socket path has a 104-byte limit on macOS; the pytest directory can be longer.
    return Path(tempfile.mkdtemp(prefix='hs'))


class FakeHolder:
    """The holder's side of both lanes: hello, the collector's hello, then keepalives and requests."""

    def __init__(self, directory, *, supported=(1, 1), methods=METHODS, refuse=None, answers=None):
        self.directory, self.supported, self.methods, self.refuse = directory, list(supported), methods, refuse
        self.answers = answers or {}
        self.hellos, self.keepalives, self.requests, self.writers = [], [], [], []
        self.token = 'f' * 64
        path = directory / 'token'
        path.write_text(self.token + '\n')
        path.chmod(0o600)
        self.servers = []

    async def __aenter__(self):
        for lane, name in (('control', 'control.sock'), ('send', 'send.sock')):
            self.servers.append(await asyncio.start_unix_server(
                lambda r, w, lane=lane: self.serve(lane, r, w), path=str(self.directory / name)))
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def close(self):
        for server in self.servers:
            server.close()
        for writer in self.writers:
            writer.close()
        self.servers = []

    async def serve(self, lane, reader, writer):
        self.writers.append(writer)

        def send(frame):
            writer.write(json.dumps(frame).encode() + b'\n')
        send({'hello': {'protocol': self.supported[1], 'supported': self.supported, 'holder_source_sha': 'abc1234',
                        'account': ACCOUNT, 'methods': self.methods, 'lane': lane}})
        line = await reader.readline()
        if not line:
            writer.close()
            return
        self.hellos.append(json.loads(line))
        if self.refuse:
            send({'error': self.refuse})
            writer.close()
            return
        send({'attached': {'protocol': 1, 'lane': lane, 'account': ACCOUNT, 'generation': 0, 'connected': False,
                           'terminal': ''}})
        while line := await reader.readline():
            frame = json.loads(line)
            if 'keepalive' in frame:
                self.keepalives.append(frame['keepalive'])
                continue
            self.requests.append(frame)
            answer = self.answers.get(frame['method'], {'result': {'ready': True, 'generation': 3}})
            if answer == 'close':
                writer.close()
                return
            send({'id': frame['id'], **answer})
            await writer.drain()


class FakeState:
    def __init__(self, epoch=7):
        self.lease = ('collector-one', epoch)
        self.observed, self.providers = [], []

    def holder_observation(self, **fields):
        self.observed.append(fields)

    def provider_observation(self, name):
        self.providers.append(name)


def provider(directory, tmp_path, state=None):
    events = tmp_path / 'events'
    events.mkdir(mode=0o700, exist_ok=True)
    p = wh.HolderProvider({'account': ACCOUNT, 'holder_socket_directory': str(directory),
                           'native_executable': '/nonexistent/anymsg-whatsmeow', 'events_directory': str(events),
                           'provider': 'whatsmeow'}, tmp_path / 'config.json')
    p.bind_state(state or FakeState())
    return p


# spec: ANYM-937
@pytest.mark.parametrize(('holder', 'collector', 'agreed', 'code'), [
    ((1, 1), (1, 1), 1, None),                               # N/N
    ((1, 2), (1, 1), 1, None),                               # holder N, collector N-1
    ((1, 1), (1, 2), 1, None),                               # holder N-1, collector N
    ((3, 3), (1, 1), None, 'collector_protocol_unsupported'),  # collector N-2: refused
    ((1, 1), (3, 3), None, 'holder_protocol_unsupported'),     # holder N-2: refused
])
def test_the_collector_agrees_the_higher_common_protocol_and_refuses_a_holder_two_behind(holder, collector, agreed,
                                                                                         code):
    assert wh.negotiate(holder, collector) == (agreed, code)


# spec: ANYM-937
def test_the_collector_attaches_with_token_and_lease_and_starts_under_a_fresh_versioned_policy(tmp_path):
    async def run():
        directory = private_dir()
        async with FakeHolder(directory) as holder:
            p = provider(directory, tmp_path)
            await p.set_policy(POLICY)  # before the lease: kept, nothing sent
            await p.connect()
            assert [h['lane'] for h in holder.hellos] == ['control', 'send']
            assert all(h['token'] == holder.token and h['lease'] == {'owner': 'collector-one', 'epoch': 7}
                       and h['hello'] == {'protocol': 1, 'supported': [1, 1]} for h in holder.hellos)
            # The keepalive with the policy precedes `start`: the holder connects only under a fresh policy.
            assert holder.requests[0]['method'] == 'start' and len(holder.keepalives) == 1
            policy = holder.keepalives[0]['policy']
            assert set(policy) == set(POLICY) | {'version', 'expires_at'}
            assert policy['version'] == (7 << 20) | 1 and 0 < policy['expires_at'] - time.time() <= 45
            assert p.state.observed[-1] == {'state': 'attached', 'generation': 3, 'protocol': 1,
                                            'holder_source_sha': 'abc1234'}
            assert p.state.providers == ['whatsmeow'] and p.self_provider_id() is None  # no expected_wid in cfg
            # The heartbeat's keepalive: same content keeps the version; a change raises it.
            await p.set_policy(POLICY)
            await p.set_policy({**POLICY, 'exclude_chats': ['555@g.us', '777@g.us']})
            await asyncio.sleep(0.05)  # the fake holder reads them
            versions = [k['policy']['version'] for k in holder.keepalives]
            assert versions == [(7 << 20) | 1, (7 << 20) | 1, (7 << 20) | 2]
            # A planned stop sends one more keepalive right before the lanes close.
            await p.disconnect()
            await asyncio.sleep(0.05)
            assert len(holder.keepalives) == 4
        # A successor collector (a higher lease epoch) is always newer, whatever its own change count.
        successor = provider(private_dir(), tmp_path, FakeState(epoch=8))
        successor.policy_changes = 1
        assert successor.policy_version() > versions[-1]
    asyncio.run(run())


# spec: ANYM-937
def test_messenger_operations_carry_the_command_row_journal_key_and_outcome_unknown_is_never_a_failure(tmp_path):
    from messaging_service.commands import CommandRejected

    async def run():
        directory = private_dir()
        answers = {'messenger_operation': {'error': 'operation_outcome_unknown'}}
        async with FakeHolder(directory, answers=answers) as holder:
            p = provider(directory, tmp_path)
            await p.set_policy(POLICY)
            await p.connect()
            job = {'id': ROW, 'operation': 'delete_message', 'parameters': {}, 'provider_chat': '1@c.us',
                   'expires_at': time.time() + 60}
            async with p.send_gate:
                with pytest.raises(wh.OperationOutcomeUnknown, match='^operation_result_unknown$'):
                    await p.execute_operation(job)
            sent = holder.requests[-1]['params']
            assert sent['command_row_id'] == ROW and sent['step'] == 0 and sent['expires_at'] == job['expires_at']
            assert p.process.returncode is None  # a complete error frame: the lanes stay attached
            # A command row ID of another shape never reaches the holder.
            async with p.send_gate:
                with pytest.raises(CommandRejected, match='^operation_invalid_parameters$'):
                    await p.execute_operation({**job, 'id': 'synthetic-request-1'})
            assert len(holder.requests) == 2
            await p.disconnect()
    asyncio.run(run())


# spec: ANYM-937
def test_a_method_absent_from_the_holders_hello_is_operation_unsupported_and_never_called(tmp_path):
    from messaging_service.commands import CommandRejected

    async def run():
        directory = private_dir()
        async with FakeHolder(directory, methods=[m for m in METHODS if m != 'gap_repair']) as holder:
            p = provider(directory, tmp_path)
            await p.set_policy(POLICY)
            await p.connect()
            with pytest.raises(CommandRejected, match='^operation_unsupported$'):
                await p.call('gap_repair', {})
            assert [r['method'] for r in holder.requests] == ['start']
            await p.disconnect()
    asyncio.run(run())


# spec: ANYM-937
def test_a_protocol_mismatch_is_reported_once_and_retried_every_60_s_with_no_token_sent(tmp_path):
    async def run():
        directory = private_dir()
        async with FakeHolder(directory, supported=(3, 3)) as holder:
            p = provider(directory, tmp_path)
            waits = []

            async def sleep(seconds):
                waits.append(seconds)
                if len(waits) == 3:
                    raise asyncio.CancelledError
            p.sleep = sleep
            with pytest.raises(asyncio.CancelledError):
                await p.connect()
            assert waits == [60, 60, 60]
            assert p.state.observed == [{'state': 'refused', 'code': 'collector_protocol_unsupported'}]
            assert holder.hellos == [] and holder.requests == []  # no token, no lease, no method
    asyncio.run(run())


# spec: ANYM-937
def test_an_expired_lease_refusal_at_attach_stops_the_collector_and_busy_is_retried(tmp_path):
    async def run():
        directory = private_dir()
        async with FakeHolder(directory, refuse='collector_lease_invalid'):
            with pytest.raises(RuntimeError, match='^collector_lease_lost$'):
                await provider(directory, tmp_path).connect()
        directory = private_dir()
        async with FakeHolder(directory, refuse='holder_busy'):
            p = provider(directory, tmp_path)
            waits = []

            async def sleep(seconds):
                waits.append(seconds)
                if len(waits) == 4:
                    raise asyncio.CancelledError
            p.sleep = sleep
            with pytest.raises(asyncio.CancelledError):
                await p.connect()
            assert waits == [5, 10, 20, 40]
            assert p.state.observed == [{'state': 'refused', 'code': 'holder_busy'}]
    asyncio.run(run())


# spec: ANYM-937
@pytest.mark.parametrize(('marker', 'reason'), [(False, 'holder_restart'), (True, 'session_window')])
def test_a_holder_that_ends_is_provider_disconnected_with_its_reason(tmp_path, marker, reason):
    async def run():
        directory = private_dir()
        holder = FakeHolder(directory)
        await holder.__aenter__()
        p = provider(directory, tmp_path)
        await p.set_policy(POLICY)
        await p.connect()
        waiting = asyncio.create_task(p.wait_disconnected())
        await asyncio.sleep(0.05)
        assert not waiting.done()
        if marker:
            (directory / wh.PLANNED_STOP_FILE).write_text(json.dumps(
                {'reason': 'session_window', 'planned_end': '2026-11-10T04:30:00+00:00'}))
        await holder.close()
        with pytest.raises(RuntimeError, match='^provider_disconnected$') as stop:
            await asyncio.wait_for(waiting, 5)
        assert stop.value.detail == reason
        observed = p.state.observed[-1]
        assert observed['state'] == 'disconnected' and observed['reason'] == reason
        assert observed.get('planned_end') == ('2026-11-10T04:30:00+00:00' if marker else None)
        await p.disconnect()
    asyncio.run(run())


# spec: ANYM-937
@pytest.mark.parametrize(('code', 'reason'), [('provider_disconnected', 'provider'), ('policy_expired', 'policy_expired')])
def test_a_holder_without_a_provider_connection_answers_with_the_disconnect_reason(tmp_path, code, reason):
    async def run():
        directory = private_dir()
        async with FakeHolder(directory, answers={'dialogs': {'error': code}}):
            p = provider(directory, tmp_path)
            await p.set_policy(POLICY)
            await p.connect()
            with pytest.raises(RuntimeError, match='^provider_disconnected$') as stop:
                await p.dialogs(10)
            assert stop.value.detail == reason and p.state.observed[-1]['reason'] == reason
            assert p.process.returncode == 0  # the collector detached; its run ends and reattaches with backoff
    asyncio.run(run())


# spec: ANYM-937
def test_holder_mode_refuses_a_shared_token_or_socket_directory_and_never_spawns_a_child(tmp_path):
    directory = private_dir()
    FakeHolder(directory)
    (directory / 'token').chmod(0o644)
    with pytest.raises(RuntimeError, match='^holder_token_invalid$'):
        wh.read_token(directory)
    directory.chmod(0o750)
    with pytest.raises(RuntimeError, match='^holder_socket_directory_invalid$'):
        wh.private_socket_directory(directory)
    with pytest.raises(RuntimeError, match='^holder_attached$'):
        provider(directory, tmp_path).command()


def holder_config(tmp_path, **changes):
    for name in ('session', 'events', 'pages'):
        (tmp_path / name).mkdir(mode=0o700, exist_ok=True)
    permit = tmp_path / 'permit.json'
    permit.write_text(json.dumps({'account': ACCOUNT, 'expected_wid': '12345@c.us', 'prior_clients_stopped': True}))
    permit.chmod(0o600)
    cfg = {'enabled': True, 'account': ACCOUNT, 'expected_wid': '12345@c.us', 'permit_file': str(permit),
           'session_directory': str(tmp_path / 'session'), 'events_directory': str(tmp_path / 'events'),
           'pages_directory': str(tmp_path / 'pages'), 'provider': 'whatsmeow',
           'native_executable': '/usr/local/bin/anymsg-whatsmeow', 'holder_socket_directory': '/run/deedkit/holder',
           **changes}
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(cfg))
    path.chmod(0o600)
    return path


# spec: ANYM-937
def test_the_holder_socket_directory_is_native_only_absolute_and_apart_from_the_other_directories(tmp_path):
    assert configuration(holder_config(tmp_path))['holder_socket_directory'] == '/run/deedkit/holder'
    with pytest.raises(ValueError, match='^invalid_private_path$'):
        configuration(holder_config(tmp_path, holder_socket_directory='relative/holder'))
    with pytest.raises(ValueError, match='^invalid_private_path$'):
        configuration(holder_config(tmp_path, provider='webjs', chromium_executable='/usr/bin/chromium',
                                    node_executable='/usr/bin/node'))
    with pytest.raises(ValueError, match='^collector_directories_overlap$'):
        configuration(holder_config(tmp_path, holder_socket_directory=str(tmp_path / 'events' / 'holder')))


def journal(events, command_id, **record):
    directory = events / 'operation-outcomes'
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / f'{command_id}.0.json'
    path.write_text(json.dumps({'kind': 'messenger_operation', 'account': 'command-fixture',
                                'command_row_id': command_id, 'step': 0, 'payload_digest': 'b' * 64,
                                'expires_at': time.time() + 60, 'observed_at': '2026-11-10T00:00:00Z', **record}))
    path.chmod(0o600)


# spec: ANYM-937
def test_a_restarted_collector_takes_the_holders_verified_outcome_and_leaves_a_pending_one_unknown(operator, tmp_path):  # noqa: F811
    from messaging_service.commands import CommandWorker
    s, _, key, state = operator
    events = tmp_path / 'events'
    events.mkdir(mode=0o700)
    done = queue(s, key)['operation_id']
    with s.pool.connection() as c:  # the predecessor dispatched it and ended before its receipt
        c.execute("UPDATE messenger_commands SET status='dispatching',lease_epoch=0 WHERE id=%s", (done,))
    journal(events, done, pending=False, result={'outcome': 'confirmed', 'data': {'deleted': True}})
    assert wh.reconcile_operation_outcomes(state, events) == 1
    row = s.get_messenger_operation(key, done)
    assert row['status'] == 'succeeded'
    # A key the holder left pending (it ended mid-call) has no verified outcome: unknown, never replayed.
    args = operation_args()
    draft = s.preview_messenger_operation(key, **args)
    pending = s.execute_messenger_operation(key, **args, request_id='synthetic-operation-0002',
                                            preview_digest=draft['preview_digest'])['operation_id']
    with s.pool.connection() as c:
        c.execute("UPDATE messenger_commands SET status='dispatching',lease_epoch=0 WHERE id=%s", (pending,))
    journal(events, pending, pending=True)
    assert wh.reconcile_operation_outcomes(state, events) == 0
    calls = []

    async def execute(job):
        calls.append(job)
        return {'outcome': 'confirmed'}
    assert not asyncio.run(CommandWorker(state, execute).once())
    assert not calls and s.get_messenger_operation(key, pending)['status'] == 'unknown'


# spec: ANYM-937
def test_the_attachment_generation_and_disconnect_reason_are_in_source_status(operator):  # noqa: F811
    s, archive, _, state = operator
    holder_state = wh.HolderState(archive, state.account, state.lease)
    holder_state.holder_observation(state='disconnected', code='provider_disconnected', reason='session_window',
                                    planned_end='2026-11-10T04:30:00+00:00')
    holder_state.holder_observation(state='attached', generation=4, protocol=1, holder_source_sha='abc1234')
    with s.pool.connection() as c:
        value = c.execute('SELECT operations FROM collector_health WHERE account=%s', (state.account,)).fetchone()
    holder = value['operations']['holder']
    assert holder['generation'] == 4 and holder['state'] == 'attached' and holder['protocol'] == 1
    assert 'reason' not in holder and 'planned_end' not in holder and holder['observed_at']
    with pytest.raises(ValueError, match='^invalid_holder_observation$'):
        holder_state.holder_observation(state='connected')


# spec: ANYM-937
def test_the_collector_attaches_to_the_real_holder_and_its_lease_is_checked_under_the_holder_login(
        holder_login, native_fixture_binary, tmp_path):  # noqa: F811
    from messaging_service.archive import Archive
    from test_whatsmeow_holder_claim import Holder
    store, _, dsn = holder_login
    holder = Holder(native_fixture_binary, tmp_path / 'real', dsn)
    archive = Archive(store)
    try:
        holder.wait_for('holder.claimed')
        deadline = time.monotonic() + 10  # the sockets follow the claim
        while not all((holder.sockets / n).exists() for n in ('control.sock', 'send.sock', 'token')):
            assert time.monotonic() < deadline and holder.process.poll() is None
            time.sleep(0.05)
        epoch = archive.acquire(ACCOUNT, 'collector-real')

        async def run():
            state = FakeState()
            state.lease = ('collector-real', epoch)
            p = provider(holder.sockets, tmp_path, state)
            p.process = await p.attach()  # never `start`: that would contact WhatsApp
            p.send_reader, p.send_writer = p.process.send, p.process.send.writer
            await p.set_policy(POLICY)
            # No provider connection: the real holder answers provider_disconnected; the collector detaches.
            with pytest.raises(RuntimeError, match='^provider_disconnected$') as stop:
                await p.dialogs(5)
            assert stop.value.detail == 'provider'
            # Reattached, then the holder stops: holder_restart.
            p.process = await p.attach()
            p.send_reader, p.send_writer = p.process.send, p.process.send.writer
            p.reason = None
            waiting = asyncio.create_task(p.wait_disconnected())
            await asyncio.sleep(0.2)
            assert await asyncio.to_thread(holder.stop) == 0
            with pytest.raises(RuntimeError, match='^provider_disconnected$') as stop:
                await asyncio.wait_for(waiting, 10)
            assert stop.value.detail == 'holder_restart'
            await p.disconnect()
        asyncio.run(run())
        attached = [e for e in holder.events if e.get('event') == 'holder.attached']
        assert len(attached) == 2 and all(e['epoch'] == epoch for e in attached)
        assert not any(e.get('event') == 'holder.provider_connected' for e in holder.events)
    finally:
        if holder.process.poll() is None:
            holder.stop()
