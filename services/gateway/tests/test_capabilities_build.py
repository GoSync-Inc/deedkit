"""messages_capabilities over the gateway pass the backend's `build` block through unchanged (ANYM-592)."""
import json

import httpx
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from test_action_ceiling import setup
from test_capabilities_sections import SUPPORTED, call
from test_capability_actions import HEADERS


def build_app(tmp_path, state):
    """The gateway against a messaging backend that answers with `state["build"]` at the time of each read;
    None leaves `build` out, as a backend from before ANYM-592 does."""
    _, value, path, _ = setup(tmp_path, SUPPORTED)
    value["profiles"]["reader"]["tools"].append("messages_capabilities")
    value["revision"] += 1
    path.write_text(json.dumps(value))

    def backend(request):
        body = {"service": "anymsg", "version": "0.9.19", "permissions": value["principals"][0]["grants"]["messaging"],
                "chat_scope": {"chat_ids": None}, "messenger_operations": {"supported_by_account": SUPPORTED}}
        build = state["build"]
        return httpx.Response(200, json=body if build is None else body | {"build": build})
    return create_app(path, "http://testserver", transports={"messaging": httpx.MockTransport(backend)})


# spec: ANYM-592
def test_gateway_keeps_the_backend_build_in_summary_identity_and_full(tmp_path):
    first = {"source_sha": "0123456789abcdef0123456789abcdef01234567", "started_at": "2026-10-28T09:30:00.000000Z"}
    second = {"source_sha": None, "started_at": "2026-10-28T10:00:00.000000Z"}
    state = {"build": first}
    app = build_app(tmp_path, state)
    with TestClient(app) as client:
        summary, error = call(client, {})
        assert not error and summary["build"] == first and summary["version"] == "0.9.19"
        identity, error = call(client, {"sections": ["identity"]})
        assert not error and identity["build"] == first
        assert client.post("/v1/messages_capabilities", json={"detail": "full"}, headers=HEADERS).json()["build"] == first
        # No gateway cache: the next read shows the backend's new block.
        state["build"] = second
        assert call(client, {})[0]["build"] == second
        # A backend without `build` still answers; the gateway adds none.
        state["build"] = None
        old, error = call(client, {})
        assert not error and "build" not in old
        assert "build" not in call(client, {"sections": ["identity"]})[0]
