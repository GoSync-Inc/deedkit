"""calendar_find_time through the gateway: name candidates and their retry survive the payload filter (ANYM-553)."""
import asyncio
import json

import pytest

from agent_gateway.catalog import CATALOG
from agent_gateway.dispatch import Dispatch
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
        "people": [member_id, "Masha"], "window": window, "duration": 45, "exclude_me": False}}}


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
