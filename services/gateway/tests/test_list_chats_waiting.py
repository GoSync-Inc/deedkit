"""ANYM-537: the gateway forwards the owner-waits list arguments only when a caller sends them."""
import json

import httpx

from starlette.testclient import TestClient

from test_diagnostics import HEADERS, configured
from test_transport import rpc

WAITING = {"since": "2026-10-26T09:00:00+00:00", "count": 2, "receipt": "server", "suggested_action": "check_delivery"}
PAGE = {"items": [{"id": "chat_1", "title": "Synthetic", "type": "private", "last_activity": "2026-10-27T09:00:00+00:00",
    "last_message": {"id": "msg_2", "direction": "outgoing", "display_text": "ping"}, "waiting": WAITING}],
    "next_cursor": None, "order": "waiting_longest", "uncertain_total": 1, "awaiting_their_reply_total": 1,
    "accounts": [{"account": "synthetic-tg", "status": "running"}], "detail": "compact"}


def forwarded(tmp_path, arguments):
    bodies = []

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read"]})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=PAGE)

    app, _, _ = configured(tmp_path, backend, module="messaging", tools=["messages_list_chats"], grants=["read"])
    with TestClient(app) as client:
        rest = client.post("/v1/messages_list_chats", json=arguments, headers=HEADERS)
        mcp = rpc(client, "tools/call", {"name": "messages_list_chats", "arguments": arguments}).json()["result"]
    return bodies, rest, mcp


# spec: ANYM-537
def test_a_legacy_list_chats_call_forwards_no_new_argument(tmp_path):
    bodies, rest, mcp = forwarded(tmp_path, {})
    assert rest.status_code == 200 and not mcp.get("isError")
    for body in bodies:
        assert not {"awaiting_their_reply", "min_wait_hours"} & set(body)
        assert body["order"] == "activity" and body["only_questions"] is False


# spec: ANYM-537
def test_the_queue_arguments_and_waiting_rows_pass_through_unchanged(tmp_path):
    arguments = {"awaiting_their_reply": True, "order": "waiting_longest", "only_questions": True, "min_wait_hours": 48}
    bodies, rest, mcp = forwarded(tmp_path, arguments)
    assert len(bodies) == 2 and all(arguments.items() <= body.items() for body in bodies)
    for page in (rest.json(), mcp["structuredContent"]):
        assert page["items"][0]["waiting"] == WAITING and page["uncertain_total"] == 1
