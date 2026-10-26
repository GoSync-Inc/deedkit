"""Messaging send and operation statuses map onto the rulebook `state` (ANYM-544); a send wait holds no DB between polls."""
import asyncio
from contextlib import contextmanager, nullcontext
from pathlib import Path
import re
from types import SimpleNamespace

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


class Pool:
    """A stand-in for the store's psycopg pool that counts checked-out connections; `leak` withholds release."""

    def __init__(self, leak=False):
        self.out, self.leak = 0, leak

    @contextmanager
    def connection(self):
        self.out += 1
        try:
            yield SimpleNamespace(closed=False, transaction=nullcontext)
        finally:
            if not self.leak:
                self.out -= 1


def connections_during_poll_sleeps(monkeypatch, pool):
    """Run a real `Store.get_send_status` (authorization context included) under `wait_for_send` and return the
    pool's checked-out count at each poll sleep. Only the SQL is replaced: identity, audit and the status query."""
    from messaging_service import outgoing, send_wait, store
    from messaging_service.policy import OPERATION_PERMISSIONS

    statuses, observed = iter(["queued", "dispatching", "sent"]), []
    principal = {"id": "principal-synthetic", "permissions": set(OPERATION_PERMISSIONS.values())}
    monkeypatch.setattr(store.Store, "identity", lambda self, c, token: principal)
    monkeypatch.setattr(store, "audit", lambda c, *entry: None)
    monkeypatch.setattr(outgoing, "status", lambda c, p, send_id: {"send_id": send_id, "status": next(statuses)})
    real = store.Store.__new__(store.Store)
    real.pool, real.files = pool, Path("/nonexistent-synthetic")
    sleep = asyncio.sleep

    async def watched(delay):
        observed.append(pool.out)
        await sleep(0)

    monkeypatch.setattr(send_wait.asyncio, "sleep", watched)
    result = asyncio.run(send_wait.wait_for_send(real, "token", "send-synthetic", 5))
    assert result["status"] == "sent"
    return observed


# spec: ANYM-544
def test_a_send_wait_holds_no_database_connection_between_polls(monkeypatch):
    pool = Pool()
    assert connections_during_poll_sleeps(monkeypatch, pool) == [0, 0] and pool.out == 0


# spec: ANYM-544
def test_the_poll_check_fails_when_the_store_withholds_its_connection(monkeypatch):
    """The instrument above can see a retained connection: a pool that never takes one back shows it at every sleep."""
    assert connections_during_poll_sleeps(monkeypatch, Pool(leak=True)) == [1, 2]
