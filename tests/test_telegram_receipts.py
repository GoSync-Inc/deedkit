"""Synthetic Telethon read boundaries; no provider sessions or RPCs."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading
from types import SimpleNamespace as NS

import pytest
from telethon import events, types

from messaging_service.collector_state import CaptureSpool
from messaging_service import telegram_collector
from messaging_service.telegram_collector import TelegramCollector, TelethonProvider
from test_collectors import message


OBSERVED = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def update(peer=None, max_id=23):
    return types.UpdateReadHistoryOutbox(peer or types.PeerUser(100), max_id, 10, 1)


def normalize(event, owner=None, expected=42):
    from messaging_service.telegram_receipts import telegram_receipt
    return telegram_receipt('tg-fixture', event,
        verified_self=owner or types.User(id=42, is_self=True),
        expected_user_id=expected, observed_at=OBSERVED)


def test_provider_subscribes_optional_raw_receipt_callback_without_network():
    registered = []
    provider = TelethonProvider.__new__(TelethonProvider)
    provider.client = NS(add_event_handler=lambda callback, event: registered.append((callback, event)))
    capture, deleted, receipt = object(), object(), object()
    provider.subscribe(capture, deleted, receipt)
    assert provider.supports_receipts is True
    assert len(registered) == 4
    assert registered[-1][0] is receipt
    assert isinstance(registered[-1][1], events.Raw)
    assert registered[-1][1].types is types.UpdateReadHistoryOutbox


def test_existing_two_callback_provider_subscription_remains_supported():
    registered = []
    provider = TelethonProvider.__new__(TelethonProvider)
    provider.client = NS(add_event_handler=lambda callback, event: registered.append((callback, event)))
    provider.subscribe(object(), object())
    assert len(registered) == 3


def test_private_outbox_boundary_has_no_invented_provider_timestamp():
    assert normalize(update()) == {
        'kind': 'message_receipt', 'account': 'tg-fixture', 'platform': 'telegram',
        'chat': '100', 'provider_id': None, 'max_id': 23, 'state': 'read',
        'basis': 'telegram_outbox_boundary', 'audience': 'private_peer',
        'observed_at': OBSERVED.isoformat(), 'provider_at': None,
    }


@pytest.mark.parametrize('event', [
    types.UpdateReadHistoryInbox(types.PeerUser(100), 23, 0, 10, 1),
    types.UpdateReadChannelOutbox(100, 23),
    types.UpdateReadMessagesContents([23], 10, 1),
    update(types.PeerChat(100)), update(types.PeerChannel(100)),
    update(types.PeerUser(42)), update(types.PeerUser(0)),
    update(types.PeerUser(True)), update(types.PeerUser(-100)),
    update(max_id=True), update(max_id=0), update(max_id=-1),
    update(max_id=2**63), update(max_id='23'), NS(peer=types.PeerUser(100), max_id=23),
])
def test_non_private_and_malformed_updates_do_not_claim_peer_read(event):
    assert normalize(event) is None


@pytest.mark.parametrize('owner,expected', [
    (types.User(id=42), 42), (types.User(id=99, is_self=True), 42),
    (types.User(id=42, is_self=True), True), (NS(id=42, is_self=True), 42),
])
def test_receipt_requires_cached_verified_owner(owner, expected):
    assert normalize(update(), owner=owner, expected=expected) is None


def test_receipt_is_not_captured_before_owner_verification():
    from messaging_service.telegram_receipts import telegram_receipt
    assert telegram_receipt('tg-fixture', update(), verified_self=None,
        expected_user_id=42, observed_at=OBSERVED) is None


class State:
    account = 'tg-fixture'

    def __init__(self):
        self.applied = []; self.progress = []; self.publications = []

    @staticmethod
    def allows(policy, chat):
        return chat not in policy['exclude_chats'] and (
            policy['discover_chats'] or chat in policy['include_chats'])

    def apply(self, item): self.applied.append(item)
    def capture_progress(self, last, count, pending, **health): self.progress.append((last, count, pending))
    def publish_receipts(self, *, limit): self.publications.append(limit)


def collector(tmp_path, **spool_options):
    provider = NS(verified_self=types.User(id=42, is_self=True), expected_user_id=42)
    spool = CaptureSpool(tmp_path / 'captures', reserve_bytes=0, **spool_options)
    worker = TelegramCollector(None, 'tg-fixture', provider, spool, writer=object())
    worker.state = State()
    worker.policy = {'account': 'tg-fixture', 'discover_chats': True,
        'include_chats': [], 'exclude_chats': []}
    return worker


def test_capture_is_durable_replayed_after_restart_then_publishes(tmp_path):
    worker = collector(tmp_path)
    asyncio.run(worker.receipt(update()))
    assert not worker.failed.is_set() and worker.capture_count == 0
    assert worker.state.applied == []
    assert len(list(worker.spool.path.glob('*.json'))) == 1
    restarted = collector(tmp_path)
    asyncio.run(restarted.drain_captures([restarted.spool]))
    assert restarted.state.applied[0]['kind'] == 'message_receipt'
    assert restarted.state.applied[0]['chat'] == '100'
    assert restarted.state.publications == [100]
    assert not list(restarted.spool.path.glob('*.json'))


def test_failed_archive_commit_keeps_receipt_for_restart_replay(tmp_path):
    worker = collector(tmp_path)
    asyncio.run(worker.receipt(update()))
    def fail(_): raise RuntimeError('synthetic_database_failure')
    worker.state.apply = fail
    with pytest.raises(RuntimeError, match='synthetic_database_failure'):
        asyncio.run(worker.drain_captures([worker.spool]))
    assert len(list(worker.spool.path.glob('*.json'))) == 1
    assert worker.state.publications == []
    restarted = collector(tmp_path)
    asyncio.run(restarted.drain_captures([restarted.spool]))
    assert len(restarted.state.applied) == 1
    assert not list(restarted.spool.path.glob('*.json'))


@pytest.mark.parametrize('policy_change', [
    {'exclude_chats': ['100']}, {'discover_chats': False}, {'account': 'tg-other'},
])
def test_receipt_capture_honors_account_and_chat_policy(tmp_path, policy_change):
    worker = collector(tmp_path); worker.policy.update(policy_change)
    asyncio.run(worker.receipt(update()))
    assert not list(worker.spool.path.glob('*.json'))
    assert not worker.failed.is_set() and worker.capture_count == 0


def test_receipt_capture_ignores_unverified_and_malformed_updates(tmp_path):
    worker = collector(tmp_path)
    asyncio.run(worker.receipt(update(max_id=True)))
    worker.provider.verified_self = None
    asyncio.run(worker.receipt(update()))
    assert not list(worker.spool.path.glob('*.json')) and not worker.failed.is_set()


def test_receipt_capture_does_not_use_other_account_state(tmp_path):
    worker = collector(tmp_path); worker.state.account = 'tg-other'
    asyncio.run(worker.receipt(update()))
    assert not list(worker.spool.path.glob('*.json')) and not worker.failed.is_set()


def test_shared_capture_overload_fails_closed(tmp_path):
    # ANYM-291: 16 writes run and 512 callbacks wait; the next one fails closed without a write.
    worker = collector(tmp_path); release = threading.Event(); put = worker.spool.put
    worker.spool.put = lambda item: (release.wait(10), put(item))[1]
    async def overload():
        tasks = [asyncio.create_task(worker.receipt(update(max_id=n + 1)))
                 for n in range(16 + 512)]
        await asyncio.sleep(0.05)
        assert worker.capture_count == 16 and len(worker.capture_waiters) == 512
        assert not worker.failed.is_set()
        await worker.receipt(update(max_id=9999))
        assert worker.failed.is_set() and worker.failure_code == 'capture_overload'
        assert worker.failure_detail == 'waiters_limit'
        release.set(); await asyncio.gather(*tasks)
    asyncio.run(overload())
    # Only the 16 writes already in flight complete; waiters see the stop and write nothing.
    assert len(list(worker.spool.path.glob('*.json'))) == 16
    assert worker.capture_count == 0 and not worker.capture_waiters


# spec: ANYM-291
def test_capture_burst_waits_for_slots_and_spools_every_update(tmp_path):
    worker = collector(tmp_path); put = worker.spool.put; lock = threading.Lock()
    running = [0, 0]; full = threading.Event()
    def slow_put(item):
        with lock:
            running[0] += 1; running[1] = max(running)
            if running[0] == 16: full.set()
        full.wait(5)  # The first 16 writes overlap, so a 17th concurrent write would be seen.
        try: return put(item)
        finally:
            with lock: running[0] -= 1
    worker.spool.put = slow_put
    async def burst():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(64))
        await asyncio.gather(*[worker.capture(NS(message=message(n, chat=str(100 + n))))
                               for n in range(1, 101)],
                             *[worker.receipt(update(max_id=n)) for n in range(1, 101)])
    asyncio.run(burst())
    assert not worker.failed.is_set() and worker.failure_code is None
    assert len(list(worker.spool.path.glob('*.json'))) == 200
    assert running[1] == 16 and worker.capture_count == 0 and not worker.capture_waiters


# spec: ANYM-291
def test_capture_slot_wait_longer_than_bound_fails_closed(tmp_path):
    worker = collector(tmp_path); worker.capture_count = 16; worker.capture_wait_seconds = 0.01
    asyncio.run(worker.capture(NS(message=message(12))))
    assert worker.failed.is_set() and worker.failure_code == 'capture_overload'
    assert worker.failure_detail == 'slot_wait_timeout'
    assert not list(worker.spool.path.glob('*.json'))
    assert worker.capture_count == 16 and not worker.capture_waiters
    assert (telegram_collector.CAPTURE_WRITES, telegram_collector.CAPTURE_WAITERS,
            telegram_collector.CAPTURE_WAIT_SECONDS) == (16, 512, 30)


# spec: ANYM-291
def test_cancelled_capture_waiter_never_leaks_a_slot(tmp_path):
    worker = collector(tmp_path)
    async def scenario():
        worker.capture_count = 16
        first = asyncio.create_task(worker.receipt(update(max_id=1)))
        second = asyncio.create_task(worker.receipt(update(max_id=2)))
        await asyncio.sleep(0)
        assert len(worker.capture_waiters) == 2
        # The first waiter is granted a slot and cancelled in the same tick; the slot passes on.
        worker.release_capture(); first.cancel()
        with pytest.raises(asyncio.CancelledError): await first
        await second
        worker.capture_count -= 15
    asyncio.run(scenario())
    assert worker.capture_count == 0 and not worker.capture_waiters and not worker.failed.is_set()
    assert len(list(worker.spool.path.glob('*.json'))) == 1


def test_unsupported_receipt_does_not_overload_message_capture(tmp_path):
    worker = collector(tmp_path); worker.capture_count = 16
    asyncio.run(worker.receipt(update(types.PeerChat(100))))
    assert not worker.failed.is_set() and worker.capture_count == 16


def test_receipt_spool_failure_fails_closed_without_claiming_capture(tmp_path):
    worker = collector(tmp_path, max_entries=0)
    asyncio.run(worker.receipt(update()))
    assert worker.failed.is_set() and worker.failure_code == 'durable_capture_failed'
    assert worker.capture_count == 0 and not list(worker.spool.path.glob('*.json'))


@pytest.mark.parametrize('condition', ['stopping', 'failed'])
def test_receipt_does_not_write_after_shutdown_or_failure(tmp_path, condition):
    worker = collector(tmp_path); getattr(worker, condition).set()
    asyncio.run(worker.receipt(update()))
    assert not list(worker.spool.path.glob('*.json'))


def test_pending_receipts_are_published_even_without_new_captures(tmp_path):
    worker = collector(tmp_path)
    asyncio.run(worker.drain_captures([worker.spool]))
    assert worker.state.publications == [100]


def test_publication_failure_is_not_swallowed(tmp_path):
    worker = collector(tmp_path)
    def fail(*, limit): raise RuntimeError('synthetic_publication_failure')
    worker.state.publish_receipts = fail
    with pytest.raises(RuntimeError, match='synthetic_publication_failure'):
        asyncio.run(worker.drain_captures([worker.spool]))


def test_fake_state_without_receipt_publisher_remains_compatible(tmp_path):
    worker = collector(tmp_path)
    worker.state = NS(apply=worker.state.apply, capture_progress=worker.state.capture_progress)
    asyncio.run(worker.drain_captures([worker.spool]))


@pytest.mark.parametrize('supports', [True, False, 1, None])
def test_receipt_callback_is_subscribed_before_connect_with_explicit_capability(tmp_path, supports):
    worker = collector(tmp_path); subscribed = []; lifecycle = []
    def subscribe(*callbacks):
        lifecycle.append('subscribe'); subscribed.extend(callbacks)
    async def connect():
        lifecycle.append('connect')
        assert len(subscribed) == (3 if supports is True else 2)
        worker.stopping.set()
    async def disconnect(): lifecycle.append('disconnect')
    async def heartbeat(): await asyncio.Event().wait()
    worker.provider = NS(supports_receipts=supports, subscribe=subscribe, connect=connect,
        disconnect=disconnect)
    state = NS(policy=lambda: worker.policy, status=lambda *args: None,
        phase=lambda *args, **identity: None, connection_gate=lambda: {'blocked': False, 'remaining_seconds': 0},
        connection_failed=lambda code: pytest.fail(code))
    worker.state_class = lambda *args: state
    worker.archive = NS(acquire=lambda *args: 1, release=lambda *args: lifecycle.append('release'))
    worker.heartbeat = heartbeat
    asyncio.run(worker.run())
    assert lifecycle == ['subscribe', 'connect', 'disconnect', 'release']
    if supports is True: assert subscribed[2] == worker.receipt


def test_subscription_type_error_is_not_misinterpreted_as_old_provider_signature(tmp_path):
    worker = collector(tmp_path); calls = []
    def subscribe(*callbacks):
        calls.append(callbacks); raise TypeError('synthetic_subscription_bug')
    async def disconnect(): pass
    worker.provider = NS(supports_receipts=True, subscribe=subscribe, disconnect=disconnect)
    state = NS(policy=lambda: worker.policy, status=lambda *args: None,
        connection_failed=lambda *args: None)
    worker.state_class = lambda *args: state
    worker.archive = NS(acquire=lambda *args: 1, release=lambda *args: None)
    with pytest.raises(TypeError, match='synthetic_subscription_bug'):
        asyncio.run(worker.run())
    assert len(calls) == 1 and len(calls[0]) == 3
