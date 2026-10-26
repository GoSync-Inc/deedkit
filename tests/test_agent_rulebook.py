"""Messaging send and operation statuses map onto the rulebook `state` (ANYM-544); a send wait holds no DB between polls."""
import asyncio
from pathlib import Path
import re

import pytest

from platform_contracts.agent import STATES, STATUS_STATES, state_of

MIGRATIONS = Path(__file__).resolve().parents[1] / "services/messaging/messaging_service/migrations"
TABLE = re.compile(r"\b(?:CREATE|ALTER)\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.I)
CHECK = re.compile(r"\bstatus\s+IN\s*\(([^)]*)\)", re.I)


def checked_statuses(table):
    """Every value any migration's `status IN (...)` CHECK allows on `table`, attributed to the nearest table."""
    values = set()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text()
        for match in CHECK.finditer(text):
            tables = TABLE.findall(text[:match.start()])
            if tables and tables[-1] == table:
                values |= set(re.findall(r"'([a-z_]+)'", match.group(1)))
    return values


# spec: ANYM-544
@pytest.mark.parametrize("kind, table", [("messaging.send", "outgoing_messages"),
                                         ("messaging.operation", "messenger_commands")])
def test_every_messaging_status_maps_onto_exactly_one_state(kind, table):
    statuses = checked_statuses(table)
    assert statuses, table
    assert statuses == set(STATUS_STATES[kind]), "a status without a mapping, or a mapping of a removed status"
    for status in statuses:
        assert state_of(kind, status) in STATES
    assert state_of(kind, "unknown") == "unknown"
    assert state_of(kind, "queued") == "pending"


# spec: ANYM-544
def test_a_send_wait_holds_no_database_connection_between_polls(monkeypatch):
    from messaging_service import send_wait

    held, observed = [False], []
    statuses = iter(["queued", "dispatching", "sent"])

    class Store:
        def get_send_status(self, token, send_id):
            held[0] = True
            try:
                return {"send_id": send_id, "status": next(statuses)}
            finally:
                held[0] = False

    sleep = asyncio.sleep

    async def watched(delay):
        observed.append(held[0])
        await sleep(0)

    monkeypatch.setattr(send_wait.asyncio, "sleep", watched)
    result = asyncio.run(send_wait.wait_for_send(Store(), "token", "send-synthetic", 5))
    assert result["status"] == "sent" and observed == [False, False]
