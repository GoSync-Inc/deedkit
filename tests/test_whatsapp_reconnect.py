"""A short WhatsApp reconnect is waited for, not turned into a collector restart (issue 485a6e1b)."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from messaging_service import whatsapp_collector as wa
from messaging_service.whatsapp_collector import NodeProvider


class Stop(BaseException): pass


def fake_provider(tmp_path, answers):
    """A provider whose child answers each request with the next frame; never starts a process."""
    p = NodeProvider({'events_directory': str(tmp_path)}, tmp_path / 'unused', 3)
    frames = iter(answers)
    class Reader:
        async def readline(self):
            return json.dumps({'id': p.counter, **next(frames)}).encode() + b'\n'
    class Writer:
        def write(self, _): pass
        async def drain(self): pass
    p.process = SimpleNamespace(returncode=None, stdin=Writer(), stdout=Reader())
    p.disconnects = 0
    async def disconnect(): p.disconnects += 1
    p.disconnect = disconnect
    return p


NOT_READY = {'error': 'provider_not_ready'}
PAGE = {'result': {'messages': [], 'exhausted': True, 'pending': False}}


def page_worker(provider, monkeypatch, attempts_before_stop):
    statuses, sleeps, now = [], [], [0.0]
    async def sleep(delay): sleeps.append(delay); now[0] += delay
    monkeypatch.setattr(wa.asyncio, 'sleep', sleep)
    monkeypatch.setattr(wa, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    worker = wa.WhatsAppCollector.__new__(wa.WhatsAppCollector)
    worker.provider, worker.interval, worker.database_retry = provider, 600, 2
    worker.state = SimpleNamespace(status=lambda *a: statuses.append(('status', *a)),
        phase=lambda *a: statuses.append(('phase', *a)))
    attempts = []
    async def sync_once():
        attempts.append(now[0])
        if len(attempts) > attempts_before_stop: raise Stop()
        await provider.page('123@c.us', limit=1)
    worker.sync_once = sync_once
    return worker, statuses, sleeps, attempts


# spec: ANYM-116
def test_page_during_auto_reconnect_degrades_history_and_keeps_the_provider(tmp_path, monkeypatch):
    provider = fake_provider(tmp_path, [NOT_READY, PAGE])
    worker, statuses, sleeps, attempts = page_worker(provider, monkeypatch, 2)
    with pytest.raises(Stop): asyncio.run(worker.reconcile_loop())
    # The reconnect in progress is not cut off, and the retry does not wait a whole long interval.
    assert provider.disconnects == 0 and len(attempts) == 3
    assert statuses == [('status', 'degraded', 'provider_temporarily_unavailable'),
        ('phase', 'history', 'error', 'provider_temporarily_unavailable')]
    assert sleeps == [60, 600]


# spec: ANYM-116
def test_provider_still_not_ready_after_five_minutes_stops_the_collector(tmp_path, monkeypatch):
    provider = fake_provider(tmp_path, [NOT_READY] * 10)
    worker, statuses, sleeps, attempts = page_worker(provider, monkeypatch, 10)
    with pytest.raises(RuntimeError) as stopped: asyncio.run(worker.reconcile_loop())
    assert type(stopped.value) is RuntimeError and str(stopped.value) == 'provider_not_ready'
    assert attempts == [0, 60, 120, 180, 240, 300] and provider.disconnects == 0


# spec: ANYM-116
def test_recorded_provider_failure_still_stops_at_once(tmp_path, monkeypatch):
    (tmp_path / '.transport-failure').write_text(json.dumps({'code': 'policy_expired'}))
    provider = fake_provider(tmp_path, [NOT_READY])
    worker, statuses, sleeps, attempts = page_worker(provider, monkeypatch, 5)
    with pytest.raises(RuntimeError, match='^provider_not_ready$'): asyncio.run(worker.reconcile_loop())
    assert provider.disconnects == 1 and len(attempts) == 1 and statuses == []


# spec: ANYM-116
@pytest.mark.parametrize('method', ['media', 'request_history', 'policy'])
def test_not_ready_outside_history_reads_keeps_disconnecting(tmp_path, method):
    provider = fake_provider(tmp_path, [NOT_READY])
    with pytest.raises(RuntimeError, match='^provider_not_ready$'):
        asyncio.run(provider.call(method, {}))
    assert provider.disconnects == 1
