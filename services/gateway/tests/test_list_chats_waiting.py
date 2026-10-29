"""ANYM-537: the gateway forwards the owner-waits list arguments only when a caller sends them."""
import json

import httpx
import pytest

from starlette.testclient import TestClient

from test_diagnostics import HEADERS, configured
from test_transport import rpc

WAITING = {"since": "2026-10-26T09:00:00+00:00", "count": 2, "receipt": "server", "suggested_action": "check_delivery"}
PAGE = {"items": [{"id": "chat_1", "title": "Synthetic", "type": "private", "last_activity": "2026-10-27T09:00:00+00:00",
    "last_message": {"id": "msg_2", "direction": "outgoing", "display_text": "ping"}, "waiting": WAITING}],
    "next_cursor": None, "order": "waiting_longest", "uncertain_total": 1, "awaiting_their_reply_total": 1,
    "accounts": [{"account": "synthetic-tg", "status": "running"}], "detail": "compact"}


def forwarded(tmp_path, arguments, page=PAGE):
    bodies = []

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read"]})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=page)

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


RUN = {"since": "2026-10-26T09:00:00+00:00", "count": 1, "receipt": "server", "suggested_action": "check_delivery"}
# Messaging's rendering of one row per view and detail (presentation.waiting_summary): the run's last message is
# msg_2; the archive head is a later reaction (msg_3), so only archive rows name msg_2 inside waiting.
RENDERED = {
    ("archive", "compact"): {"id": "chat_1", "type": "private", "last_activity": "2026-10-28T09:00:00+00:00",
        "last_message_id": "msg_3", "last_message_direction": "incoming",
        "waiting": {**RUN, "last_message_id": "msg_2", "last_at": "2026-10-26T09:00:00+00:00"}},
    ("archive", "full"): {"id": "chat_1", "type": "private", "last_activity": "2026-10-28T09:00:00+00:00",
        "last_message_id": "msg_3", "awaiting_reply": False, "awaiting_reply_message_id": None,
        "waiting": {**RUN, "last_message_id": "msg_2", "last_at": "2026-10-26T09:00:00+00:00"}},
    ("conversation", "compact"): {"id": "chat_1", "type": "private", "last_activity": "2026-10-26T09:00:00+00:00",
        "last_message": {"id": "msg_2", "direction": "outgoing", "display_text": "Synthetic question?"},
        "conversation": {"suggested_action": "await_peer", "basis": "last_archived_message_outgoing"}, "waiting": RUN},
    ("conversation", "full"): {"id": "chat_1", "type": "private", "last_activity": "2026-10-26T09:00:00+00:00",
        "last_message_id": "msg_2", "conversation": {"last_turn": "owner", "suggested_action": "await_peer",
            "latest_known_meaningful_message": {"id": "msg_2"}}, "waiting": RUN},
}


# spec: ANYM-537
@pytest.mark.parametrize("view,detail", sorted(RENDERED))
def test_each_view_and_detail_reaches_rest_and_mcp_with_waiting_once(tmp_path, view, detail):
    row = RENDERED[(view, detail)]
    page = {"items": [row], "next_cursor": None, "order": "waiting_longest", "uncertain_total": 0,
        "awaiting_their_reply_total": 1, "accounts": [{"account": "synthetic-tg"}], "detail": detail}
    arguments = {"awaiting_their_reply": True, "order": "waiting_longest", "view": view, "detail": detail}
    bodies, rest, mcp = forwarded(tmp_path, arguments, page)
    assert all(body["view"] == view and body["detail"] == detail for body in bodies)
    for result in (rest.json(), mcp["structuredContent"]):
        item = result["items"][0]
        assert item == row and item["waiting"]["suggested_action"] == "check_delivery"
        head = item.get("last_message", {}).get("id") or item["last_message_id"]
        # The run's last message is named once: by the row's head, or inside waiting beside another head.
        assert ("last_message_id" in item["waiting"]) == (head != "msg_2")
