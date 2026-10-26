"""The `subscriptions` and `events` capability blocks name only calls in the caller's gateway tool list (ANYM-528)."""
import tomllib
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.catalog import CATALOG
from test_diagnostics import configured, HEADERS
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
    # The platform payload's `capability` record names the gateway's own operation, not a messaging call.
    text = {k: v for k, v in platform.items() if k != "capability"}
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
