"""Operator path end to end: register an account, enforce its chat policy, block, resume (synthetic only)."""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.blobs import BlobWriter
from messaging_service.collector_state import CaptureSpool, CollectorState
from messaging_service.telegram_collector import TelegramCollector
from test_collectors import FakeProvider, message

ACCOUNT = 'tg-flow'
CHATS = {'100': 'Chat A', '200': 'Chat B', '300': 'Chat C'}


class FlowProvider(FakeProvider):
    """Lists chats A, B and C and serves each chat only its own messages."""
    def __init__(self, messages=(), identity_mismatch=False):
        super().__init__(messages)
        self.connects = 0
        self.identity_mismatch = identity_mismatch

    async def connect(self):
        self.connects += 1
        if self.identity_mismatch:
            raise RuntimeError('telegram_identity_mismatch')
        await super().connect()

    async def dialogs(self, limit):
        return [(chat, 'user', title) for chat, title in CHATS.items()][:limit]

    async def page(self, chat, *, limit, min_id=0, before=0, reverse=False):
        self.reads.append(chat)
        own = [m for m in self.messages if m.chat_id == int(chat) and m.id > min_id and (not before or m.id < before)]
        return sorted(own, key=lambda m: m.id, reverse=not reverse)[:limit]


class GateCountingState(CollectorState):
    gate_checks = 0

    def connection_gate(self):
        gate = super().connection_gate()
        type(self).gate_checks += 1
        return gate


def messages(*ids):
    return [message(i, chat, body=f'{title[-1]}-{i}') for chat, title in CHATS.items() for i in ids]


def cli(module, args, env):
    return subprocess.run([sys.executable, '-m', module, *args], env=env, capture_output=True, text=True, timeout=60)


def rpc(client, key, name, arguments):
    response = client.post('/mcp', headers={'Authorization': 'Bearer ' + key,
        'Accept': 'application/json, text/event-stream'}, json={'jsonrpc': '2.0', 'id': 1,
        'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
    assert response.status_code == 200 and not response.json()['result'].get('isError'), response.text
    return response.json()['result']['structuredContent']


def visible(store, key):
    with TestClient(create_app(store, ['testserver'])) as client:
        bodies = {item['body'] for item in rpc(client, key, 'search', {'detail': 'full', 'limit': 50, 'view': 'archive'})['items']}
        titles = {item['title'] for item in rpc(client, key, 'list_chats', {'limit': 50, 'view': 'archive'})['items']}
    return bodies, titles


def connection(store, key):
    with TestClient(create_app(store, ['testserver'])) as client:
        status = rpc(client, key, 'get_sync_status', {'refresh': True})
    operations = status['sources'][0]['operations']
    return operations['connection'], operations['live_capture']


class WaitCountingEvent(asyncio.Event):
    waits = 0

    async def wait(self):
        self.waits += 1
        return await super().wait()


def run_collector(store, tmp_path, provider, until, state_class=CollectorState, blocked=False):
    """Build a collector and run it until `until()` holds, then stop it.

    The stop waits for real progress: a collection pass that returned from `sync_once()`, or, for a
    blocked connection, the collector sitting in its blocked wait after it read the gate. A delay would
    only guess that a late provider read or connect had not happened yet.
    """
    archive = Archive(store)
    spool = CaptureSpool(tmp_path / ('capture-' + str(time.monotonic_ns())), reserve_bytes=0)
    collector = TelegramCollector(archive, ACCOUNT, provider, spool, batch_size=2, interval=60,
        writer=BlobWriter(store.files, reserve_bytes=0), owner='flow-' + str(time.monotonic_ns()))
    collector.state_class = state_class
    collector.stopping = WaitCountingEvent()
    passes = []
    pass_once = collector.sync_once

    async def counted_pass():
        await pass_once()
        passes.append(True)
    collector.sync_once = counted_pass

    def progressed():
        # run() waits on `stopping` once; the blocked branch of connect_provider is the second waiter.
        return collector.stopping.waits >= 2 if blocked else bool(passes)

    async def scenario():
        running = asyncio.create_task(collector.run())
        try:
            deadline = time.monotonic() + 20
            while not (progressed() and until()):
                assert not running.done(), running.exception()
                assert time.monotonic() < deadline, 'collector did not reach the expected state'
                await asyncio.sleep(.05)
            assert not running.done(), running.exception()
        finally:
            collector.stopping.set()
            await asyncio.wait_for(running, 15)
    asyncio.run(scenario())


def blocked_restart_probe(dsn_file, files, capture, key):
    """Child-process body: a restarted collector with its own Store, observed while it runs."""
    from messaging_service.store import Store
    store = Store(Path(dsn_file).read_text().strip(), Path(files), 'http://testserver')
    GateCountingState.gate_checks = 0
    provider = FlowProvider(messages(1, 2, 3))
    seen = []

    def gate_checked_while_running():
        if GateCountingState.gate_checks:
            seen.append(connection(store, key))
        return bool(seen)
    try:
        run_collector(store, Path(capture), provider, gate_checked_while_running, GateCountingState, blocked=True)
    finally:
        store.close()
    return {'connection': seen[-1][0], 'live_capture': seen[-1][1], 'connects': provider.connects, 'reads': provider.reads}


# spec: ANYM-415
def test_registered_policy_collects_allowed_chats_blocks_on_identity_mismatch_and_reconnects_after_resume(seeded, tmp_path):
    store, _, _ = seeded
    dsn_file = tmp_path / 'dsn'
    dsn_file.write_text(Path(os.environ['PROBE_TEST_DSN_FILE']).read_text().strip())
    dsn_file.chmod(0o600)
    env = {**os.environ, 'DATABASE_URL_FILE': str(dsn_file), 'PROBE_FILES': str(tmp_path / 'files')}
    register = ['register-account', '--account', ACCOUNT, '--platform', 'telegram', '--label', 'Synthetic flow']

    # Registration: the output, no collector side effects, and the two refusals (uncaught ValueError, ANYM-80).
    done = cli('messaging_service', [*register, '--include-chats', '100', '--exclude-chats', '200'], env)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {'registered': True, 'account': ACCOUNT, 'provider_connected': False}
    with store.pool.connection() as c:
        assert c.execute('SELECT 1 FROM collector_leases WHERE account=%s', (ACCOUNT,)).fetchone() is None
        assert c.execute('SELECT 1 FROM collector_health WHERE account=%s', (ACCOUNT,)).fetchone() is None
    empty = cli('messaging_service', register, env)
    assert empty.returncode != 0 and 'Traceback' in empty.stderr
    assert empty.stderr.rstrip().splitlines()[-1] == 'ValueError: empty_collection_policy'
    other_platform = cli('messaging_service', ['register-account', '--account', ACCOUNT, '--platform', 'whatsapp',
        '--label', 'Synthetic flow', '--discover-chats'], env)
    assert other_platform.returncode != 0 and 'Traceback' in other_platform.stderr
    assert other_platform.stderr.rstrip().splitlines()[-1] == 'ValueError: account_identity_change'
    with store.pool.connection() as c:
        policy = c.execute('SELECT * FROM account_policies WHERE account=%s', (ACCOUNT,)).fetchone()
        assert (c.execute('SELECT platform FROM accounts WHERE id=%s', (ACCOUNT,)).fetchone()['platform']) == 'telegram'
    assert (policy['include_chats'], policy['exclude_chats'], policy['discover_chats']) == (['100'], ['200'], False)

    # Include A and exclude B without discovery: only A is read and visible.
    key = store.issue_key('flow-reader', [ACCOUNT])
    provider = FlowProvider(messages(1, 2))
    run_collector(store, tmp_path, provider, lambda: {'A-1', 'A-2'} <= visible(store, key)[0])
    bodies, titles = visible(store, key)
    assert bodies == {'A-1', 'A-2'} and titles == {'Chat A'}
    assert set(provider.reads) == {'100'}

    # Discovery adds C, rebuilt collector reads the new policy; B stays unread.
    done = cli('messaging_service', [*register, '--include-chats', '100', '--exclude-chats', '200', '--discover-chats'], env)
    assert done.returncode == 0, done.stderr
    provider = FlowProvider(messages(1, 2))
    run_collector(store, tmp_path, provider, lambda: {'A-1', 'A-2', 'C-1', 'C-2'} <= visible(store, key)[0])
    bodies, titles = visible(store, key)
    assert bodies == {'A-1', 'A-2', 'C-1', 'C-2'} and titles == {'Chat A', 'Chat C'}
    assert '200' not in provider.reads

    # Identity mismatch: recorded by the collector, blocked for a fresh process that never connects.
    run_collector(store, tmp_path, FlowProvider(messages(1, 2, 3), identity_mismatch=True),
        lambda: connection(store, key)[0]['state'] == 'blocked', blocked=True)
    # The restart is a real child process: own interpreter, own Store, nothing shared with this one.
    probe = subprocess.run([sys.executable, '-c',
        'import json, sys, test_account_policy_flow as t; print(json.dumps(t.blocked_restart_probe(*sys.argv[1:])))',
        str(dsn_file), str(tmp_path / 'files'), str(tmp_path), key],
        env={**env, 'PYTHONPATH': str(Path(__file__).parent)}, capture_output=True, text=True, timeout=60)
    assert probe.returncode == 0, probe.stderr
    observed = json.loads(probe.stdout.strip().splitlines()[-1])
    state, live = observed['connection'], observed['live_capture']
    assert state['state'] == 'blocked' and state['reason_code'] == 'telegram_identity_mismatch'
    assert live['state'] == 'paused' and live['reason_code'] == 'telegram_identity_mismatch'
    assert observed['connects'] == 0 and observed['reads'] == []
    assert connection(store, key)[0]['state'] == 'blocked'

    # Owner resume: recorded once, refused the second time, then the collector connects and collects.
    resume = ['--dsn-file', str(dsn_file), '--account', ACCOUNT, '--resume']
    resumed = cli('messaging_service.collector_control', resume, env)
    assert resumed.returncode == 0 and resumed.stdout.strip() == 'connection_resume_recorded', resumed.stderr
    with store.pool.connection() as c:
        stored = c.execute('SELECT operations FROM collector_health WHERE account=%s', (ACCOUNT,)).fetchone()['operations']['connection']
    assert stored['state'] == 'stopped' and stored['reason_code'] == 'operator_resumed'
    again = cli('messaging_service.collector_control', resume, env)
    assert again.returncode != 0 and 'connection_not_blocked' in again.stderr
    provider = FlowProvider(messages(1, 2, 3))
    run_collector(store, tmp_path, provider, lambda: {'A-3', 'C-3'} <= visible(store, key)[0])
    bodies, titles = visible(store, key)
    assert provider.connects == 1 and bodies == {'A-1', 'A-2', 'A-3', 'C-1', 'C-2', 'C-3'}
    assert titles == {'Chat A', 'Chat C'} and '200' not in provider.reads
    assert connection(store, key)[0]['reason_code'] != 'telegram_identity_mismatch'
