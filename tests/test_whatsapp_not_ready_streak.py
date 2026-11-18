"""ANYM-486: what ends a provider_not_ready streak and what does not, and the short retry of a dialogs read.

The real provider call and reconcile loop with the existing fake child provider and patched clock
(tests/test_whatsapp_reconnect.py): no process, session, network or real sleep.
"""
import asyncio

import psycopg
import pytest

from messaging_service import whatsapp_collector as wa
from test_whatsapp_reconnect import NOT_READY, Stop, fake_provider, page_worker

ERROR_ANSWERS = {'operation error': {'error': 'provider_operation_failed'},  # ProviderOperationError
                 'unavailable': {'error': 'media_unavailable'}}  # ProviderUnavailable
EVERY_MINUTE = [60 * n for n in range(11)]


def stopped_not_ready(worker):
    with pytest.raises(RuntimeError) as stopped:
        asyncio.run(worker.reconcile_loop())
    assert type(stopped.value) is RuntimeError and str(stopped.value) == 'provider_not_ready'


# spec: ANYM-486
@pytest.mark.parametrize('answer', sorted(ERROR_ANSWERS))
def test_an_error_answer_to_a_history_read_ends_the_streak_and_a_later_unbroken_streak_still_stops(
        tmp_path, monkeypatch, answer):
    provider = fake_provider(tmp_path, [NOT_READY] * 4 + [ERROR_ANSWERS[answer]] + [NOT_READY] * 10)
    worker, statuses, sleeps, attempts = page_worker(provider, monkeypatch, 20)
    worker.interval = 60
    stopped_not_ready(worker)
    # Not ready at 0-180 s; at 240 s the provider answers (with an error), so the streak ends there. The next one
    # starts at 300 s and stops 300 s later, at 600 s: not at 300 s, as an unbroken streak from 0 would.
    assert attempts == EVERY_MINUTE and provider.disconnects == 0
    assert ('status', 'degraded', 'provider_temporarily_unavailable') in statuses


# spec: ANYM-486
def test_a_database_error_retry_keeps_the_not_ready_streak(tmp_path, monkeypatch):
    provider = fake_provider(tmp_path, [NOT_READY] * 10)
    worker, statuses, sleeps, attempts = page_worker(provider, monkeypatch, 20)
    worker.interval = 60
    calls, read = [0], worker.sync_once

    async def sync_once():  # the fourth pass fails on the database before it asks the provider
        calls[0] += 1
        if calls[0] == 4:
            raise psycopg.OperationalError('synthetic database outage')
        await read()
    worker.sync_once = sync_once
    stopped_not_ready(worker)
    # The streak from 0 s survives the database retry (2 s) and stops at the first not-ready read 300 s after it.
    assert attempts == [0, 60, 120, 182, 242, 302] and sleeps == [60, 60, 60, 2, 60, 60]
    assert provider.disconnects == 0


# spec: ANYM-486
def test_a_failing_wait_report_neither_ends_nor_restarts_the_streak(tmp_path, monkeypatch):
    provider = fake_provider(tmp_path, [NOT_READY] * 10)
    worker, statuses, sleeps, attempts = page_worker(provider, monkeypatch, 20)
    worker.interval = 60

    def status(*_):
        raise psycopg.OperationalError('synthetic outage')
    worker.state.status = status
    stopped_not_ready(worker)
    assert attempts == [0, 60, 120, 180, 240, 300] and provider.disconnects == 0


# spec: ANYM-486
def test_a_dialogs_read_during_a_reconnect_retries_after_the_shorter_scan_interval(tmp_path, monkeypatch):
    provider = fake_provider(tmp_path, [NOT_READY, {'result': {'chats': []}}])
    worker, statuses, sleeps, attempts = page_worker(provider, monkeypatch, 2)
    worker.interval = 30  # below 60 s: the retry waits the scan interval, not 60 s

    async def sync_once():
        attempts.append(wa.time.monotonic())
        if len(attempts) > 2:
            raise Stop()
        await provider.dialogs(limit=1)
    worker.sync_once = sync_once
    with pytest.raises(Stop):
        asyncio.run(worker.reconcile_loop())
    assert provider.disconnects == 0 and attempts == [0, 30, 60] and sleeps == [30, 30]
    assert statuses == [('status', 'degraded', 'provider_temporarily_unavailable'),
                        ('phase', 'history', 'error', 'provider_temporarily_unavailable')]
    assert provider.not_ready_since is None  # the dialogs answer ended the streak
