"""calendar_find_time through the gateway: name candidates and their retry survive the payload filter (ANYM-553)."""
import asyncio
import json

import httpx
import pytest

from agent_gateway.catalog import CATALOG
from agent_gateway.dispatch import Dispatch
from agent_gateway.http_clients import Clients, admission, wait_tools
from agent_gateway.policy import Denied, Policy
from test_policy import config

SCOPES = ["availability:read", "agenda:read"]
TOOLS = ["calendar_capabilities", "calendar_members", "calendar_find_time"]


def gateway(tmp_path, answer):
    key = tmp_path / "backend.key"
    key.write_text("backend-calendar-a")
    key.chmod(0o600)
    document = config()
    document["profiles"]["reader"]["tools"] = TOOLS
    document["principals"][0]["grants"]["calendar"] = SCOPES
    document["principals"][0]["credentials"]["calendar"]["key_file"] = str(key)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document))
    path.chmod(0o600)

    class Clients:
        deployment = Policy(path).load()["deployment"]

        async def call(self, module, operation, credential, arguments):
            if operation == "capabilities":
                return {"permissions": SCOPES, "calendars": [], "features": {}, "constraints": {}}
            error = Denied("backend_denied", 409)
            error.detail = answer
            raise error

    return Dispatch(Policy(path), Clients())


def member(member_id, label, window):
    return {"id": member_id, "kind": "member", "label": label, "next": {"tool": "calendar_find_time", "arguments": {
        "people": [member_id, "child"], "window": window, "duration": 45, "exclude_me": False}}}


# spec: ANYM-553
def test_find_time_row_is_read_only_under_availability_read():
    row = CATALOG["calendar_find_time"]
    assert row["module"] == "calendar" and row["operation"] == "find_time"
    assert row["scopes"] == ["availability:read"] and row["annotations"]["readOnlyHint"] is True


# spec: ANYM-553
@pytest.mark.parametrize("window", ["tomorrow", {"from": "2026-10-21T09:00", "to": "2026-10-21T18:00"}])
def test_name_candidates_keep_their_label_and_retry_but_never_the_title(tmp_path, window):
    candidates = [member("adult-b", "Anna", window), member("helper", "Anna Petrova", window)]
    request = {"people": ["Ann", "Masha"], "window": window, "duration": 45, "title": "Swimming"}
    titled = {**candidates[1], "next": {**candidates[1]["next"], "arguments": {
        **candidates[1]["next"]["arguments"], "title": "Swimming"}}}
    with pytest.raises(Denied) as caught:
        asyncio.run(gateway(tmp_path, {"error": "selection_required", "candidates": [candidates[0], titled]})
                    .call("gateway-a", "calendar_find_time", request))
    relayed = caught.value.detail["candidates"]
    # The label repeats the asked name by design; a retry that repeats the title loses its next.
    assert relayed == [candidates[0], {k: v for k, v in candidates[1].items() if k != "next"}]


# spec: ANYM-553
def test_an_unknown_name_relays_its_calendar_members_next(tmp_path):
    answer = {"error": "not_found", "next": {"tool": "calendar_members", "arguments": {}}}
    with pytest.raises(Denied) as caught:
        asyncio.run(gateway(tmp_path, answer).call("gateway-a", "calendar_find_time",
                                                   {"people": ["Zoe"], "window": "today"}))
    assert caught.value.detail["next"] == answer["next"]


# spec: ANYM-553
def test_caller_text_outside_member_names_and_checked_windows_stays_payload(tmp_path):
    marker = "confidential salary marker"
    good = member("adult-b", "Anna", "tomorrow")
    trailing = {**good, "next": {**good["next"], "arguments": {**good["next"]["arguments"],
                                                                "people": ["adult-b", marker]}}}
    hinted = {"error": "selection_required", "hint": f"Ann or {marker}", "candidates": [
        trailing, member("helper", "Anna Petrova", "tomorrow")]}
    with pytest.raises(Denied) as caught:
        asyncio.run(gateway(tmp_path, hinted).call("gateway-a", "calendar_find_time",
                                                   {"people": ["Ann", marker], "window": "tomorrow"}))
    # An unknown trailing name is still payload: the retry repeating it and the hint are dropped; a retry by
    # member IDs is kept.
    assert caught.value.detail.get("hint") is None
    assert caught.value.detail["candidates"] == [{k: v for k, v in good.items() if k != "next"},
                                                 member("helper", "Anna Petrova", "tomorrow")]
    window = {"from": "private medical marker", "to": "another secret marker"}
    leaked = {"error": "selection_required", "candidates": [member("adult-b", "Anna", window)]}
    with pytest.raises(Denied) as caught:
        asyncio.run(gateway(tmp_path, leaked).call("gateway-a", "calendar_find_time",
                                                   {"people": ["Ann"], "window": window}))
    # A window that is no named window or timestamps stays payload: the retry repeating it is dropped.
    assert caught.value.detail["candidates"] == [{"id": "adult-b", "kind": "member", "label": "Anna"}]


# spec: ANYM-553
def test_a_refreshing_find_time_waits_in_the_calendar_wait_pool_and_leaves_control_reads_admitted():
    tools = wait_tools(CATALOG)
    assert admission(tools, "calendar", "find_time", {"people": ["Ann"], "window": "today"}) == "waits"
    assert admission(tools, "calendar", "find_time", {"people": ["Ann"], "window": "today", "refresh": False}) \
        == "metadata"
    deployment = {"modules": ["calendar"], "backends": {"calendar": {"url": "http://calendar"}}}

    async def run():
        release = asyncio.Event()

        async def handler(request):
            if request.url.path.endswith("/find_time"):
                await release.wait()
            return httpx.Response(200, json={"ok": True})

        async with Clients(deployment, {"calendar": httpx.MockTransport(handler)}) as clients:
            budget = clients.wait_budgets["calendar"]
            held = [asyncio.create_task(clients.call("calendar", "find_time", "synthetic",
                                                     {"people": ["Ann"], "window": "today"})) for _ in range(8)]
            try:
                await asyncio.sleep(0.05)
                assert budget.pending == budget.capacity
                async with asyncio.timeout(1):
                    for operation in ("capabilities", "operation"):
                        assert (await clients.call("calendar", operation, "synthetic", {}))["ok"] is True
                assert clients.budgets["calendar"].pending == 0
            finally:
                release.set()
                await asyncio.gather(*held, return_exceptions=True)
            assert budget.pending == 0

    asyncio.run(run())


# spec: ANYM-553
def test_only_a_candidates_own_label_exempts_a_name_in_its_retry(tmp_path):
    marker = "confidential salary marker"
    good = member("adult-b", "Anna", "tomorrow")
    trailing = {**good, "next": {**good["next"], "arguments": {**good["next"]["arguments"],
                                                                "people": ["adult-b", marker]}}}
    # An invalid candidate whose label is the marker is discarded and exempts nothing for the others.
    invalid = {"id": "", "kind": "member", "label": marker}
    second = {**good, "next": {**good["next"], "arguments": {**good["next"]["arguments"], "people": ["adult-b", "Mash"]}}}
    answer = {"error": "selection_required", "candidates": [trailing, invalid, second]}
    with pytest.raises(Denied) as caught:
        asyncio.run(gateway(tmp_path, answer).call("gateway-a", "calendar_find_time",
                                                   {"people": ["Ann", marker, "Mash"], "window": "tomorrow"}))
    # A second unresolved name ("Mash") is not in Anna's label: that retry is dropped too, id and label stay.
    bare = {k: v for k, v in good.items() if k != "next"}
    assert caught.value.detail["candidates"] == [bare, bare]
