"""The `subscriptions` and `events` capability blocks name only calls in the caller's gateway tool list (ANYM-528)."""
import hashlib
import json
import tomllib
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG
from test_diagnostics import configured, HEADERS
from test_policy import config
from test_transport import rpc

PROFILES = sorted((Path(__file__).resolve().parents[3] / "config/profiles").glob("*.toml"))
CALLS = ["fetch_subscription_events", "ack_subscription_events", "wait_for_subscription_events",
         "configure_subscription_delivery", "test_subscription"]
PERMISSIONS = ["read", "subscription-write", "subscription-ack"]
# Synthetic copy of the messaging backend's blocks (store.py, as of f403db5), trimmed to the call-naming fields.
SUBSCRIPTIONS = {"implemented": True, "ack": True, "ack_permission": "subscription-ack",
                 "push": "operator_registered_signed_hints", "wait_max_seconds": 25,
                 "delivery_policy": {"tool": "configure_subscription_delivery", "argument": "delivery_policy",
                                     "requires_receiver": True}}
EVENTS = {"fetch": "fetch_subscription_events", "ack": "ack_subscription_events",
          "wait": "wait_for_subscription_events", "wait_max_seconds": 25, "test": "test_subscription",
          "delivery": "operator_registered_signed_hints", "host_turn_verified": False}
NAMES = {n for n, t in CATALOG.items() if t["module"] == "messaging"} | {
    t["operation"] for t in CATALOG.values() if t["module"] == "messaging"}
MISSING = object()


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def app(tmp_path, tools, *, permissions=PERMISSIONS, manifest=CALLS, subscriptions=SUBSCRIPTIONS, events=EVENTS):
    body = {"permissions": permissions, "tool_manifest": {"version": "synthetic", "names": manifest}}
    for key, block in (("subscriptions", subscriptions), ("events", events)):
        if block is not MISSING:
            body[key] = block

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"synthetic": True, "items": []})
    return configured(tmp_path, backend, module="messaging", tools=tools, grants=permissions)[0]


def observed(tmp_path, tools, **backend):
    with TestClient(app(tmp_path, tools, **backend)) as client:
        listed = {t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]}
        result = rpc(client, "tools/call", {"name": "messages_capabilities",
                                            "arguments": {"sections": ["subscriptions"]}}).json()["result"]
        assert not result.get("isError")
        rest = client.post("/v1/messages_capabilities", json={"sections": ["subscriptions"]}, headers=HEADERS)
        assert rest.status_code == 200 and rest.json() == result["structuredContent"]
        full = client.post("/v1/messages_capabilities", json={"detail": "full"}, headers=HEADERS).json()
        platform = (client.post("/v1/platform_capabilities", json={}, headers=HEADERS).json()
                    if "platform_capabilities" in listed else {})
    assert {k: full[k] for k in ("subscriptions", "events") if k in full} == rest.json()
    return listed, rest.json(), platform


def assert_truthful(listed, blocks, platform):
    """Criterion 1: every call named in the blocks and the platform text is listed, by its public name."""
    # The platform payload's `capability` record names the gateway's own operation, not a messaging call. ANYM-662:
    # `unavailable.required_by` names tools the key's policy allows and the module's build lacks, as "cannot run",
    # never as callable; it is covered by test_stale_client_hint.py.
    text = {k: v for k, v in platform.items() if k not in ("capability", "unavailable")}
    named = {s for s in [*strings(blocks), *strings(text)] if s in NAMES}
    assert named <= listed, named - listed
    subscriptions, events = blocks.get("subscriptions"), blocks.get("events")
    if isinstance(subscriptions, dict):
        assert subscriptions.get("ack", False) is ("messages_ack_subscription_events" in listed)
        assert ("wait_max_seconds" in subscriptions) is ("messages_wait_for_subscription_events" in listed)
        assert ("delivery_policy" in subscriptions) is ("messages_configure_subscription_delivery" in listed)
    if isinstance(events, dict):
        # The wait limit leaves with the wait field, which is kept only when its call is listed.
        assert ("wait_max_seconds" in events) is ("wait" in events)
        # Call fields are checked against tools/list directly, not through the catalog's names.
        assert all(events[f] in listed for f in ("fetch", "ack", "wait", "test") if f in events)
    if isinstance(subscriptions, dict) and "delivery_policy" in subscriptions:
        assert subscriptions["delivery_policy"]["tool"] in listed


def exposed(profile):
    """A repository profile's tools this synthetic messaging-only deployment can list."""
    tools = tomllib.loads(profile.read_text())["exposure"]["tools"]
    return [n for n in tools if n in CATALOG and CATALOG[n]["module"] in ("messaging", "gateway")]


# spec: ANYM-528
@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.stem)
def test_repository_profiles_name_only_listed_subscription_calls(tmp_path, profile):
    tools = exposed(profile)
    if "messages_capabilities" not in tools:
        # Nothing to name: the profile has no messaging capabilities call.
        assert not {"messages_" + n for n in CALLS} & set(tools)
        return
    assert_truthful(*observed(tmp_path, tools))


SOME = ["platform_capabilities", "messages_capabilities", "messages_fetch_subscription_events", "messages_ack_subscription_events"]
ALL = ["platform_capabilities", "messages_capabilities", "messages_get_subscription"] + ["messages_" + n for n in CALLS]


# spec: ANYM-528
@pytest.mark.parametrize("tools,backend,expected", [
    # All tools: every call kept, under its public gateway name.
    (ALL, {}, {"messages_" + n for n in CALLS}),
    # Some tools by the profile.
    (SOME, {}, {"messages_fetch_subscription_events", "messages_ack_subscription_events"}),
    # Some tools by the key's scopes: no subscription-ack, no ack call.
    (ALL, {"permissions": ["read", "subscription-write"]},
     {"messages_" + n for n in CALLS} - {"messages_ack_subscription_events"}),
    # Some tools by the backend manifest.
    (ALL, {"manifest": ["fetch_subscription_events"]}, {"messages_fetch_subscription_events"}),
    # No subscription tools.
    (["platform_capabilities", "messages_capabilities", "messages_list_subscriptions"], {}, set()),
], ids=["all", "some_by_profile", "some_by_key_scopes", "some_by_manifest", "none"])
def test_blocks_follow_the_callers_tool_list(tmp_path, tools, backend, expected):
    listed, blocks, platform = observed(tmp_path, tools, **backend)
    assert_truthful(listed, blocks, platform)
    assert {s for s in strings(blocks) if s in NAMES} == expected
    # Fields that name no call pass through with their types.
    assert blocks["subscriptions"]["push"] == "operator_registered_signed_hints"
    assert blocks["subscriptions"]["ack_permission"] == "subscription-ack"
    assert blocks["events"]["delivery"] == "operator_registered_signed_hints"
    assert blocks["events"]["host_turn_verified"] is False
    assert isinstance(blocks["subscriptions"]["ack"], bool)


# spec: ANYM-528
@pytest.mark.parametrize("backend", [
    {"subscriptions": MISSING, "events": MISSING},
    {"events": MISSING},
    {"subscriptions": "operator_registered_signed_hints", "events": ["ack_subscription_events"]},
    {"subscriptions": None, "events": {"ack": 7, "wait": ["wait_for_subscription_events"]}},
    {"subscriptions": {"ack": "yes", "delivery_policy": "configure_subscription_delivery"}},
], ids=["both_missing", "events_missing", "wrong_types", "null_and_bad_fields", "bad_fields"])
def test_missing_or_malformed_blocks_never_break_capabilities(tmp_path, backend):
    tools = ["messages_capabilities", "messages_list_subscriptions", "messages_fetch_subscription_events"]
    listed, blocks, platform = observed(tmp_path, tools, **backend)
    assert_truthful(listed, blocks, platform)
    for name, block in blocks.items():
        assert isinstance(block, dict), name
    with TestClient(app(tmp_path, tools, **backend)) as client:
        other = client.post("/v1/messages_list_subscriptions", json={}, headers=HEADERS)
    assert other.status_code == 200 and other.json()["synthetic"] is True


# spec: ANYM-528
@pytest.mark.parametrize("backend", [
    {"subscriptions": {"ack": True, "wait_max_seconds": 25,
                       "delivery_policy": {"tool": "configure_subscription_delivery_v2"}},
     "events": {"wait": "wait_for_subscription_events_v2", "wait_max_seconds": 25,
                "fetch": "messages_fetch_subscription_events_v2"}},
    {"subscriptions": {"wait_max_seconds": 25, "delivery_policy": {"tool": 7}},
     "events": {"ack": None, "test": "test_subscription"}},
], ids=["unknown_operations", "malformed_call_fields"])
def test_call_fields_never_name_an_unlisted_or_unknown_call(tmp_path, backend):
    tools = ["platform_capabilities", "messages_capabilities", "messages_list_subscriptions",
             "messages_wait_for_subscription_events"]
    listed, blocks, platform = observed(tmp_path, tools, **backend)
    assert_truthful(listed, blocks, platform)
    assert blocks["events"] == {} and "delivery_policy" not in blocks["subscriptions"]


# ANYM-909: list_negotiations items carry `next` read calls of any module; the gateway keeps only the caller's.
GET_CONTEXT = {"tool": "get_context", "arguments": {"message_id": "msg_" + "a" * 32}}
FIND_TIME = {"tool": "calendar_find_time", "arguments": {
    "people": ["6f1c2a3e-4b5d-4e6f-8a7b-9c0d1e2f3a4b"], "duration": 30,
    "window": {"from": "2026-11-10T14:30:00+01:00", "to": "2026-11-10T16:00:00+01:00"}}}
CHANGED = {"count": 1, "next": {"tool": "list_negotiations", "arguments": {"updated_since": "2026-11-09T10:00:00Z"}}}


def negotiations(tmp_path, tools, items):
    """A messaging and calendar deployment; the messaging backend answers list_negotiations with `items`."""
    value = config()
    value["deployment"] = {"modules": ["messaging", "calendar"], "backends": {
        "messaging": {"url": "http://messaging.test"}, "calendar": {"url": "http://calendar.test"}}}
    value["profiles"]["reader"]["tools"] = tools
    principal = value["principals"][0]
    principal["grants"] = {"messaging": ["read"], "calendar": ["agenda:read", "availability:read"]}
    principal["credentials"] = {}
    for module in ("messaging", "calendar"):
        key = tmp_path / f"{module}.key"
        key.write_text("synthetic-backend-key-" + module)
        key.chmod(0o600)
        principal["credentials"][module] = {"key_file": str(key), "key_sha256": hashlib.sha256(key.read_bytes()).hexdigest()}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    calendar_calls = []

    def messaging(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read"]})
        return httpx.Response(200, json={"items": items, "as_of": "2026-11-09T10:00:00Z", "cursor": None,
                                         "changed": dict(CHANGED)})

    def calendar(request):
        calendar_calls.append(request.url.path)
        return httpx.Response(500)
    app = create_app(path, "http://testserver", transports={"messaging": httpx.MockTransport(messaging),
                                                            "calendar": httpx.MockTransport(calendar)})
    with TestClient(app) as client:
        rest = client.post("/v1/messages_list_negotiations", json={}, headers=HEADERS)
        mcp = rpc(client, "tools/call", {"name": "messages_list_negotiations", "arguments": {}}).json()["result"]
    assert rest.status_code == 200 and not mcp.get("isError") and rest.json() == mcp["structuredContent"]
    assert calendar_calls == []
    return rest.json()


READER = ["messages_capabilities", "messages_list_negotiations", "messages_get_context"]


MODULE_EVENTS = {"calls": {"source": "deedkit:calls", "scope": "calls:own", "kinds": ["call.failed", "call.finished"],
                           "create": "create_subscription", "argument": "module_sources",
                           "consume": "fetch_subscription_events", "refused_without_link": "module_not_linked"}}
WITH_MODULE_EVENTS = {**SUBSCRIPTIONS, "module_events": MODULE_EVENTS}


# spec: ANYM-566
@pytest.mark.parametrize("extra,kept", [
    # Both calls listed: the block stays, each call under its public gateway name.
    (["messages_create_subscription", "messages_fetch_subscription_events"],
     {"create": "messages_create_subscription", "consume": "messages_fetch_subscription_events"}),
    # No fetch: the kinds stay (a push hint still reaches the key), the call it cannot make leaves.
    (["messages_create_subscription"], {"create": "messages_create_subscription"}),
    # No create: nothing in the block could be used, so no kind is named.
    (["messages_fetch_subscription_events"], None),
    ([], None),
], ids=["both", "create_only", "fetch_only", "neither"])
def test_module_event_kinds_follow_the_callers_tool_list(tmp_path, extra, kept):
    tools = ["platform_capabilities", "messages_capabilities", *extra]
    listed, blocks, platform = observed(tmp_path, tools, subscriptions=WITH_MODULE_EVENTS)
    assert_truthful(listed, blocks, platform)
    block = blocks["subscriptions"].get("module_events")
    if kept is None:
        assert block is None and "call.finished" not in json.dumps([blocks, platform])
        return
    expected = {k: v for k, v in MODULE_EVENTS["calls"].items() if k not in ("create", "consume")} | kept
    assert block == {"calls": expected}


# spec: ANYM-566
@pytest.mark.parametrize("bad", ["calls", [MODULE_EVENTS["calls"]], {"calls": "create_subscription"},
                                 {"calls": {"kinds": ["call.finished"]}}, {"calls": {**MODULE_EVENTS["calls"], "create": 7}},
                                 {"calls": {**MODULE_EVENTS["calls"], "create": "create_subscription_v2"}}, None],
                         ids=["string", "list", "string_entry", "no_create", "bad_create_type", "unknown_create", "null"])
def test_a_malformed_module_event_block_is_dropped_and_never_breaks_capabilities(tmp_path, bad):
    tools = ["platform_capabilities", "messages_capabilities", "messages_create_subscription",
             "messages_fetch_subscription_events"]
    listed, blocks, platform = observed(tmp_path, tools, subscriptions={**SUBSCRIPTIONS, "module_events": bad})
    assert_truthful(listed, blocks, platform)
    assert "module_events" not in blocks["subscriptions"] and "call.finished" not in json.dumps(blocks)
    assert blocks["subscriptions"]["push"] == "operator_registered_signed_hints"


# spec: ANYM-909
def test_negotiation_next_keeps_only_the_callers_calls_of_any_module(tmp_path):
    item = {"id": "neg_" + "1" * 32, "next": [GET_CONTEXT, FIND_TIME]}
    # A caller without calendar_find_time gets only get_context, under its public name.
    without = negotiations(tmp_path, READER, [dict(item)])
    assert without["items"][0]["next"] == [{"tool": "messages_get_context", "arguments": GET_CONTEXT["arguments"]}]
    # A caller with it gets both entries, the calendar call unchanged.
    both = negotiations(tmp_path, [*READER, "calendar_find_time"], [dict(item)])
    assert both["items"][0]["next"] == [{"tool": "messages_get_context", "arguments": GET_CONTEXT["arguments"]},
                                        FIND_TIME]
    # Without messages_get_context nothing but the calendar call is left.
    only = negotiations(tmp_path, ["messages_list_negotiations", "calendar_find_time"], [dict(item)])
    assert only["items"][0]["next"] == [FIND_TIME]
    # The last page's re-read is kept for a caller that lists it.
    assert both["changed"]["next"] == {"tool": "messages_list_negotiations", "arguments": CHANGED["next"]["arguments"]}


# spec: ANYM-909
def test_negotiation_next_leaves_no_malformed_entry(tmp_path):
    malformed = [
        {**FIND_TIME, "fill": {"people": "participants"}},  # a field to fill is never passed on
        {"tool": "calendar_plan", "arguments": {}},  # a tool the caller does not list
        {"tool": "calendar_unknown_v2", "arguments": {}},  # a tool no catalog names
        {"tool": 7, "arguments": {}}, {"tool": "calendar_find_time"}, {"tool": "get_context", "arguments": "x"},
        "get_context", None,
    ]
    items = [{"id": "neg_" + "2" * 32, "next": [*malformed, GET_CONTEXT]}, {"id": "neg_" + "3" * 32, "next": "x"}]
    seen = negotiations(tmp_path, [*READER, "calendar_find_time"], items)
    assert seen["items"][0]["next"] == [{"tool": "messages_get_context", "arguments": GET_CONTEXT["arguments"]}]
    assert seen["items"][1]["next"] == []
    for entry in (e for i in seen["items"] for e in i["next"]):
        assert set(entry) == {"tool", "arguments"} and entry["tool"] in CATALOG


# spec: ANYM-909
def test_the_deployed_gateways_catalog_keeps_working_with_the_new_messaging_api(tmp_path, monkeypatch):
    """The previous catalog (this one without the new entry) against a messaging backend that has the tool: the
    gateway lists and routes only what it knows, so the tool is simply absent until the gateway deploys."""
    monkeypatch.delitem(CATALOG, "messages_list_negotiations")
    tools = ["platform_capabilities", "messages_capabilities", "messages_list_reply_dismissals"]
    listed, blocks, platform = observed(tmp_path, tools, permissions=["read"],
                                        manifest=[*CALLS, "list_negotiations", "list_reply_dismissals"])
    assert "messages_list_negotiations" not in listed and listed >= {"messages_list_reply_dismissals"}
    with TestClient(app(tmp_path, tools, permissions=["read"])) as client:
        unknown = rpc(client, "tools/call", {"name": "messages_list_negotiations", "arguments": {}}).json()
        other = client.post("/v1/messages_list_reply_dismissals", json={}, headers=HEADERS)
    assert "error" in unknown or unknown["result"].get("isError")
    assert other.status_code == 200 and other.json()["synthetic"] is True
