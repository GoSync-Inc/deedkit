"""platform_resolve_target's candidates reach the agent through the gateway's error relay (ANYM-545)."""
import json

import httpx
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG
from platform_contracts.agent import validate_error
from test_agent_errors import GRANTS, MODULES, principal
from test_module_selection import call
from test_policy import config

TOOLS = ["platform_capabilities", "platform_resolve_target", "messages_search_people", "messages_send_message"]


def candidate(i):
    return {"id": f"chat_{i:032x}", "label": f"Synthetic Steven {i}", "kind": "chat", "network": "telegram",
            "account": "synthetic", "next": {"tool": "platform_resolve_target", "arguments": {"to": f"chat_{i:032x}"}}}


def gateway(tmp_path, body):
    value = config()
    value["deployment"] = {"modules": list(MODULES), "backends": {m: {"url": f"http://{m}.test"} for m in MODULES}}
    value["profiles"] = {"agent": {"tools": TOOLS, "actions": {}}}
    value["principals"] = [principal(tmp_path, "a")]
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    def handler(module):
        def respond(request):
            operation = request.url.path.removeprefix("/v1/")
            if operation == "capabilities":
                return httpx.Response(200, json={"permissions": GRANTS[module],
                                                 "tool_manifest": {"names": ["resolve_target"]}})
            return httpx.Response(409 if body["error"] == "selection_required" else 404, json=body)
        return httpx.MockTransport(respond)
    return create_app(path, "http://testserver", transports={m: handler(m) for m in MODULES})


# spec: ANYM-545
def test_name_candidates_and_their_read_only_listing_survive_the_relay(tmp_path):
    many = [candidate(i) for i in range(10)]
    body = {"error": "selection_required", "candidates": many, "more": True,
            "next": {"tool": "messages_search_people", "arguments": {"query": "Steven"}}}
    with TestClient(gateway(tmp_path, body)) as client:
        result = call(client, "platform_resolve_target", {"to": "Steven"})
    relayed = result["structuredContent"]
    assert result["isError"] and relayed["candidates"] == many and relayed["more"] is True
    assert relayed["next"] == body["next"]
    validate_error(relayed, read_only=frozenset(n for n in TOOLS if CATALOG[n]["annotations"].get("readOnlyHint")))


# spec: ANYM-545
def test_a_phone_in_to_still_never_reaches_a_relayed_candidate_or_next(tmp_path):
    body = {"error": "selection_required",
            "candidates": [{"id": "synthetic", "label": "+1 202 555 0177", "kind": "account"}],
            "next": {"tool": "messages_search_people", "arguments": {"query": "+12025550177"}}}
    with TestClient(gateway(tmp_path, body)) as client:
        relayed = call(client, "platform_resolve_target", {"to": "+12025550177"})["structuredContent"]
    assert "next" not in relayed and "+1" not in json.dumps(relayed)
