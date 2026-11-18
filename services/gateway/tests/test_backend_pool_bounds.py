"""A module's ordinary pool through the real gateway (ANYM-393): 8 run, the next 24 wait without reaching the backend,
and with 32 pending the 33rd is refused at once with backend_busy (HTTP 503); after the release every admitted request
reaches the backend exactly once and the pool is empty again.

Requests go through the ASGI app (lifespan, REST route, dispatch, Clients) into an in-memory calendar backend that holds
each operation on an event. Dispatch first reads the backend's capabilities through the same ordinary pool, so the 24
waiters queue at that read and reach the backend not at all. No socket, network or real backend. The bounds are the
spec's, written out here.

Why the refusal is immediate without measuring time: a waiter is refused after 0.25 s without a slot, so a 33rd request
that waited instead would be answered only after the 24 earlier waiters had been refused too. The 24 are still pending
when the 33rd is answered."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json

import httpx

from agent_gateway.app import create_app
from test_policy import config

ACTIVE, WAITING = 8, 24  # ANYM-393 c1
HEADERS = {"Authorization": "Bearer gateway-a"}


def gateway(tmp_path, handler):
    value = config()
    value["deployment"] = {"modules": ["calendar"], "backends": {"calendar": {"url": "http://calendar.test"}}}
    key = tmp_path / "backend.key"
    key.write_text("synthetic-backend-key")
    key.chmod(0o600)
    value["principals"][0]["credentials"] = {"calendar": {
        "key_file": str(key), "key_sha256": hashlib.sha256(key.read_bytes()).hexdigest()}}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return create_app(path, "http://testserver", transports={"calendar": httpx.MockTransport(handler)})


@asynccontextmanager
async def started(app):
    """The app's own ASGI lifespan: the gateway opens its clients on startup and closes them on shutdown."""
    inbox, outbox = asyncio.Queue(), asyncio.Queue()
    task = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, inbox.get, outbox.put))
    await inbox.put({"type": "lifespan.startup"})
    assert (await outbox.get())["type"] == "lifespan.startup.complete"
    try:
        yield
    finally:
        await inbox.put({"type": "lifespan.shutdown"})
        assert (await outbox.get())["type"] == "lifespan.shutdown.complete"
        await task


async def until(condition, what):
    """Bounded supervision: yield to the loop until the condition holds; a stuck pool fails instead of hanging."""
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0)
    for _ in range(200):  # let anything that could still move (a ninth runner, a 33rd pending) do so
        await asyncio.sleep(0)
    assert condition(), what


# spec: ANYM-393
def test_eight_run_twenty_four_wait_and_the_thirty_third_is_refused_at_once(tmp_path):
    reads, entered, release = [], [], asyncio.Event()

    async def backend(request):
        if request.url.path == "/v1/capabilities":  # each REST call reads these first, in the same pool
            reads.append(request.url.path)
            return httpx.Response(200, json={"permissions": ["agenda:read", "availability:read"]})
        entered.append(request.url.path)
        await release.wait()
        return httpx.Response(200, json={"calendars": [], "coverage": "complete"})

    async def run():
        app = gateway(tmp_path, backend)
        async with started(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            budget = app.state.clients.budgets["calendar"]

            def call():
                return asyncio.create_task(client.post("/v1/calendar_calendars", json={}, headers=HEADERS))

            running = [call() for _ in range(ACTIVE)]
            await until(lambda: len(entered) == ACTIVE and budget.pending == ACTIVE, "eight run")
            waiting = [call() for _ in range(WAITING)]
            await until(lambda: budget.pending == ACTIVE + WAITING, "twenty-four wait")
            # The waiters hold a place in the pool but reached neither the operation nor the capabilities read.
            assert (len(entered), len(reads)) == (ACTIVE, ACTIVE)

            refused = await client.post("/v1/calendar_calendars", json={}, headers=HEADERS)
            assert (refused.status_code, refused.json()) == (503, {"error": "backend_busy"})
            assert budget.pending == ACTIVE + WAITING and (len(entered), len(reads)) == (ACTIVE, ACTIVE)
            assert not any(task.done() for task in running + waiting)

            release.set()
            answers = await asyncio.gather(*running, *waiting)
            assert [a.status_code for a in answers] == [200] * (ACTIVE + WAITING)
            # Each admitted request reached the backend exactly once; the refused one never did.
            assert entered == ["/v1/calendars"] * (ACTIVE + WAITING) and len(reads) == ACTIVE + WAITING
            assert budget.pending == 0
            again = await client.post("/v1/calendar_calendars", json={}, headers=HEADERS)
            assert again.status_code == 200 and len(entered) == ACTIVE + WAITING + 1 and budget.pending == 0

    asyncio.run(run())
