"""Exact owner-attention contracts, using isolated PostgreSQL and fake providers."""

import asyncio

import pytest

from messaging_service.collector_control import resume
from messaging_service.collector_state import CollectorState
from test_collectors import worker as worker
from test_whatsapp import wa as wa


@pytest.mark.parametrize(
    "fixture_name, reason_code",
    [
        ("worker", "telegram_identity_mismatch"),
        ("wa", "whatsapp_identity_mismatch"),
    ],
)
def test_identity_mismatch_blocks_restarted_collector_until_owner_resume(
    request, monkeypatch, fixture_name, reason_code
):
    collector, store, reader = request.getfixturevalue(fixture_name)
    collector.state.connection_failed(reason_code)
    collector.state = CollectorState(
        collector.archive, collector.account, collector.state.lease
    )
    collector.state.status("starting", "requires_reconciliation")

    operations = store.get_sync_status(reader, refresh=True)["sources"][0]["operations"]
    connection = operations["connection"]
    assert connection["state"] == "blocked"
    assert connection["reason_code"] == reason_code
    assert connection["next_action"] == "check_configured_account_identity"
    assert connection["retry_at"] is None and connection["retry_attempts"] == 1
    assert operations["live_capture"]["state"] == "paused"
    assert operations["live_capture"]["reason_code"] == reason_code
    assert collector.state.connection_gate() == {
        "blocked": True, "remaining_seconds": 0
    }

    provider_calls = []

    async def connect():
        provider_calls.append("connect")

    monkeypatch.setattr(collector.provider, "connect", connect)

    async def attempt_while_blocked():
        checked = asyncio.Event()
        loop = asyncio.get_running_loop()
        original_gate = collector.state.connection_gate

        def observed_gate():
            result = original_gate()
            loop.call_soon_threadsafe(checked.set)
            return result

        monkeypatch.setattr(collector.state, "connection_gate", observed_gate)
        task = asyncio.create_task(collector.connect_provider())
        try:
            await asyncio.wait_for(checked.wait(), 3)
            collector.stopping.set()
            await asyncio.wait_for(task, 3)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            monkeypatch.setattr(collector.state, "connection_gate", original_gate)

    asyncio.run(attempt_while_blocked())
    assert provider_calls == []
    assert store.get_sync_status(reader, refresh=True)["sources"][0]["operations"][
        "connection"
    ]["state"] == "blocked"

    with store.pool.connection() as connection:
        resume(connection, collector.account)
    collector.stopping.clear()
    asyncio.run(collector.connect_provider())
    assert provider_calls == ["connect"]
    operations = store.get_sync_status(reader, refresh=True)["sources"][0]["operations"]
    assert operations["connection"]["state"] == "connected"
    assert operations["connection"]["reason_code"] == "provider_ready"
    assert operations["connection"]["retry_attempts"] == 0
    assert operations["live_capture"]["state"] == "receiving"
