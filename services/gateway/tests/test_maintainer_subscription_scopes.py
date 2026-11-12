"""ANYM-989: a maintainer key without `read` runs the subscription calls of its own maintainer-only definition through the
gateway; the catalog names that as `alt_scopes`, and every other tool keeps its scopes (synthetic, no backend)."""
import hashlib
import json

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG, scopes_met
from agent_gateway.policy import Policy
from test_policy import config

PERMISSION = "feedback-maintainer-read"
SUBSCRIPTION = "11111111-1111-4111-8111-111111111111"
BRIDGE = [PERMISSION, "subscription-write", "subscription-ack"]
CALLS = {
    "messages_list_subscriptions": {},
    "messages_get_subscription": {"subscription_id": SUBSCRIPTION},
    "messages_get_subscription_events": {"subscription_id": SUBSCRIPTION},
    "messages_fetch_subscription_events": {"subscription_id": SUBSCRIPTION},
    "messages_wait_for_subscription_events": {"subscription_id": SUBSCRIPTION, "timeout": 1},
    "messages_ack_subscription_events": {"subscription_id": SUBSCRIPTION, "receipt": "r" * 43, "version": 1},
    "messages_create_subscription": {"feedback": {"maintainer": True}},
    "messages_update_subscription": {"subscription_id": SUBSCRIPTION, "version": 1, "action": "pause"},
    "messages_export_feedback_metadata": {},
}
HEADERS = {"Authorization": "Bearer gateway-a"}


# spec: ANYM-989
def test_the_catalog_names_alt_scopes_for_the_six_subscription_reads_only():
    with_alt = {name for name, tool in CATALOG.items() if tool.get("alt_scopes")}
    assert with_alt == {"messages_list_subscriptions", "messages_get_subscription", "messages_get_subscription_events",
                        "messages_fetch_subscription_events", "messages_wait_for_subscription_events",
                        "messages_ack_subscription_events"}
    for name in with_alt:
        assert CATALOG[name]["alt_scopes"][0] == PERMISSION and "read" in CATALOG[name]["scopes"]
    assert CATALOG["messages_ack_subscription_events"]["alt_scopes"] == [PERMISSION, "subscription-ack"]
    for name in ("messages_configure_subscription_delivery", "messages_test_subscription", "messages_list_feedback",
                 "messages_search", "messages_submit_feedback"):
        assert not CATALOG[name].get("alt_scopes"), name


# spec: ANYM-989
def test_scopes_are_met_by_all_of_the_scopes_or_all_of_the_alt_scopes_and_nothing_less():
    wait = CATALOG["messages_wait_for_subscription_events"]
    ack = CATALOG["messages_ack_subscription_events"]
    assert scopes_met(wait, ["read"]) and scopes_met(wait, [PERMISSION]) and scopes_met(wait, ["read", PERMISSION])
    assert not scopes_met(wait, []) and not scopes_met(wait, ["subscription-write", "subscription-ack"])
    assert scopes_met(ack, ["read", "subscription-ack"]) and scopes_met(ack, [PERMISSION, "subscription-ack"])
    assert not scopes_met(ack, [PERMISSION]) and not scopes_met(ack, ["subscription-ack"]) and not scopes_met(ack, ["read"])
    search = CATALOG["messages_search"]  # a tool without alt_scopes: its scopes only
    assert scopes_met(search, ["read"]) and not scopes_met(search, [PERMISSION, "subscription-write", "subscription-ack"])


# spec: ANYM-989
@pytest.mark.parametrize("grants, expected", [
    (BRIDGE, set(CALLS) | set()),
    (["read", "subscription-write", "subscription-ack"], {n for n in CALLS if n != "messages_export_feedback_metadata"}),
    (["subscription-write", "subscription-ack"], {"messages_create_subscription", "messages_update_subscription"}),
    ([PERMISSION], {"messages_list_subscriptions", "messages_get_subscription", "messages_get_subscription_events",
                    "messages_fetch_subscription_events", "messages_wait_for_subscription_events",
                    "messages_export_feedback_metadata"}),
])
def test_an_agents_grants_make_these_tools_visible_with_or_without_read(grants, expected):
    snapshot = {"profiles": {"maintainer": {"tools": sorted(CALLS) + ["messages_search"]}},
                "deployment": {"modules": ["messaging"]}}
    agent = {"profile": "maintainer", "credentials": {"messaging": "synthetic"}, "grants": {"messaging": grants}}
    shown = set(Policy.visible(snapshot, agent))
    assert shown == expected | ({"messages_search"} if "read" in grants else set())


def gateway(tmp_path, grants, permissions, answers=None):
    value = config()
    value["deployment"] = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://messaging.test"}}}
    value["profiles"] = {"agent": {"tools": ["platform_capabilities", *sorted(CALLS), "messages_search",
                                            "messages_set_feedback_triage"], "actions": {}}}
    key = tmp_path / "a-messaging.key"
    key.write_text("backend-messaging-a")
    key.chmod(0o600)
    value["principals"] = [{"id": "a", "key_sha256": hashlib.sha256(b"gateway-a").hexdigest(), "profile": "agent",
                            "enabled": True, "expires_at": "2099-01-01T00:00:00Z", "grants": {"messaging": grants},
                            "credentials": {"messaging": {"key_file": str(key),
                                                          "key_sha256": hashlib.sha256(key.read_bytes()).hexdigest()}},
                            "actions": {}}]
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    reached = []

    def respond(request):
        operation = request.url.path.removeprefix("/v1/")
        if operation == "capabilities":
            return httpx.Response(200, json={"permissions": permissions, "tool_manifest": {"names": sorted(
                [name.removeprefix("messages_") for name in CALLS] + ["set_feedback_triage"])}})
        reached.append(operation)
        if answers and operation in answers:
            return httpx.Response(answers[operation][0], json=answers[operation][1])
        return httpx.Response(200, json={"items": [], "synthetic": True})
    app = create_app(path, "http://testserver", transports={"messaging": httpx.MockTransport(respond)})
    return app, reached


# spec: ANYM-989
def test_the_gateway_admits_a_maintainer_key_without_read_to_its_subscription_calls_and_refuses_the_rest(tmp_path):
    app, reached = gateway(tmp_path, BRIDGE, BRIDGE)
    with TestClient(app) as client:
        shown = client.post("/v1/platform_capabilities", json={}, headers=HEADERS).json()["tools"]
        assert set(CALLS) <= set(shown) and "messages_search" not in shown
        for name, arguments in CALLS.items():
            answer = client.post("/v1/" + name, json=arguments, headers=HEADERS)
            assert answer.status_code == 200, (name, answer.text)
        refused = client.post("/v1/messages_search", json={"query": "synthetic"}, headers=HEADERS)
    assert refused.status_code in (403, 404) or refused.json().get("isError")
    assert sorted(reached) == sorted(name.removeprefix("messages_") for name in CALLS)


# spec: ANYM-989
def test_a_key_that_lost_the_permission_at_the_backend_is_refused_though_the_grant_stays(tmp_path):
    app, reached = gateway(tmp_path, BRIDGE, ["subscription-write", "subscription-ack"])
    with TestClient(app) as client:
        for name in ("messages_fetch_subscription_events", "messages_wait_for_subscription_events",
                     "messages_export_feedback_metadata"):
            answer = client.post("/v1/" + name, json=CALLS[name], headers=HEADERS)
            assert answer.status_code != 200, name
        assert client.post("/v1/messages_create_subscription", json=CALLS["messages_create_subscription"],
                           headers=HEADERS).status_code == 200  # the backend decides the rest
    assert "fetch_subscription_events" not in reached and "wait_for_subscription_events" not in reached


TRIAGE = {"feedback_id": SUBSCRIPTION, "status": "accepted", "expected_status": "received", "expected_changed_at": None,
          "request_id": "gateway-triage-1", "spec_ref": "ANYM-992"}


# spec: ANYM-992
def test_the_gateway_admits_the_triage_write_by_its_own_scope_and_relays_a_lost_race_and_a_replay(tmp_path):
    assert CATALOG["messages_set_feedback_triage"]["scopes"] == ["feedback-maintainer-write"]
    assert not CATALOG["messages_set_feedback_triage"].get("alt_scopes")
    assert CATALOG["messages_set_feedback_triage"]["annotations"]["readOnlyHint"] is False
    answers = {"set_feedback_triage": (409, {"error": "version_conflict"})}
    app, reached = gateway(tmp_path, ["feedback-maintainer-write"], ["feedback-maintainer-write"], answers)
    with TestClient(app) as client:
        assert "messages_set_feedback_triage" in client.post("/v1/platform_capabilities", json={}, headers=HEADERS).json()["tools"]
        lost = client.post("/v1/messages_set_feedback_triage", json=TRIAGE, headers=HEADERS)
        assert lost.status_code == 409 and lost.json()["error"] == "version_conflict"
        for bad in ({**TRIAGE, "status": "fixed"}, {**TRIAGE, "extra": 1}, {**TRIAGE, "spec_ref": "card"}):
            assert client.post("/v1/messages_set_feedback_triage", json=bad, headers=HEADERS).status_code in (400, 422)
    assert reached == ["set_feedback_triage"]
    # without the grant the tool is neither listed nor reached; the read permission alone never stands for it
    app, reached = gateway(tmp_path, [PERMISSION, "subscription-write"], [PERMISSION, "subscription-write"])
    with TestClient(app) as client:
        assert "messages_set_feedback_triage" not in client.post("/v1/platform_capabilities", json={},
                                                                headers=HEADERS).json()["tools"]
        assert client.post("/v1/messages_set_feedback_triage", json=TRIAGE, headers=HEADERS).status_code in (403, 404)
    assert "set_feedback_triage" not in reached
