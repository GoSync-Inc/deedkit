"""ANYM-660: the backend's field_errors, hint and next_action on a refused read reach the agent unchanged.

Layers: the gateway schema (ANYM-369) refuses a wrong type or enum item before the backend (chat_types, can_post, an
empty `sections`); the gateway serves messages_capabilities itself (test_capabilities_sections.py); the backend
refuses an empty my_roles, an empty date range and a bad cursor, and the gateway relays that body for these read tools.
"""
import httpx
import pytest

from test_strict_arguments import post_both
from test_diagnostics import configured

BODIES = {
    ("messages_list_chats", "my_roles"): (
        {"my_roles": []}, {"error": "invalid_filter", "request_id": "r1", "retryable": False,
        "field_errors": [{"field": "my_roles", "reason": "min_items", "allowed": ["creator", "admin", "member", "left", "unknown"]}],
        "hint": "omit my_roles for all roles", "next_action": "check_parameters"}),
    ("messages_list_chats", "date"): (
        {"from_time": "2026-10-02T00:00:00Z", "to_time": "2026-10-01T00:00:00Z"}, {"error": "invalid_date_range", "request_id": "r1",
        "retryable": False, "field_errors": [{"field": "to_time", "reason": "minimum", "expected": "later than from_time"}],
        "hint": "swap the bounds or widen the range; the range is half-open and must not be empty", "next_action": "check_parameters"}),
    ("messages_search", "cursor"): (
        {"query": "synthetic", "after": "q1.bad"}, {"error": "invalid_cursor", "request_id": "r1", "retryable": False,
        "field_errors": [{"field": "after", "reason": "format",
            "expected": "a next_cursor returned by the same call with the same filters and order"}],
        "next_action": "restart_query_without_cursor"}),
}


def app_with_refusing_backend(tmp_path, tool, body):
    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read"], "tool_manifest": {"names": [tool.removeprefix("messages_")]}})
        return httpx.Response(400, json=body)

    return configured(tmp_path, backend, module="messaging", tools=[tool], grants=["read"])[0]


# spec: ANYM-660
@pytest.mark.parametrize("tool,case", list(BODIES))
def test_backend_refusal_bodies_reach_the_agent_with_field_errors_hint_and_next_action(tmp_path, tool, case):
    values, body = BODIES[tool, case]
    rest, mcp = post_both(app_with_refusing_backend(tmp_path, tool, body), tool, values)
    assert rest.status_code == 400
    for key in ("error", "field_errors", "next_action", "hint"):
        assert rest.json().get(key) == body.get(key), key
    assert mcp["isError"] and mcp["structuredContent"]["field_errors"] == body["field_errors"]
    assert mcp["structuredContent"].get("hint") == body.get("hint")


# spec: ANYM-660
@pytest.mark.parametrize("tool,values,field", [
    ("messages_list_chats", {"chat_types": ["nope"]}, "chat_types/0"),
    ("messages_list_chats", {"can_post": "yes"}, "can_post"),
    ("messages_capabilities", {"sections": []}, "sections"),
])
def test_gateway_schema_refuses_these_cases_before_the_backend(tmp_path, tool, values, field):
    requests = []

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read"], "tool_manifest": {"names": [tool.removeprefix("messages_")]}})
        requests.append(request.url.path)
        return httpx.Response(200, json={})

    app = configured(tmp_path, backend, module="messaging", tools=[tool], grants=["read"])[0]
    rest, mcp = post_both(app, tool, values)
    assert rest.status_code == 422 and rest.json()["error"] == "invalid_arguments"
    assert field in [e["field"] for e in rest.json()["field_errors"]] and mcp["isError"] and requests == []


# spec: ANYM-660
@pytest.mark.parametrize("case,values", [
    ("date", {"query": "range", "from_time": "2026-10-02T00:00:00Z", "to_time": "2026-10-01T00:00:00Z"}),
    ("my_roles", {"query": "roles", "my_roles": []}),
])
def test_reviewed_hint_survives_a_query_word_that_occurs_in_it(tmp_path, case, values):
    # The payload-overlap check drops a backend hint that repeats the caller's words; these exact constants cannot.
    _, body = BODIES["messages_list_chats", case if case == "date" else "my_roles"]
    rest, mcp = post_both(app_with_refusing_backend(tmp_path, "messages_list_chats", body), "messages_list_chats", values)
    assert rest.status_code == 400 and rest.json()["hint"] == body["hint"]
    assert mcp["isError"] and mcp["structuredContent"]["hint"] == body["hint"]


# spec: ANYM-795
def test_upload_limit_fields_and_hint_survive_a_payload_that_contains_the_word_upload(tmp_path):
    tool = "messages_prepare_file_upload"
    body = {"error": "upload_limit", "request_id": "r1", "retryable": False, "next_action": "check_parameters",
            "limit": "key", "active": 10, "max": 10, "resets_in_seconds": 90,
            "hint": "An upload frees its slot only when it expires (get_file_upload shows expires_at); an expired "
                    "upload still being sent keeps its slot until the send ends."}

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read", "send", "upload"],
                                             "tool_manifest": {"names": ["prepare_file_upload"]}})
        return httpx.Response(409, json=body)

    app = configured(tmp_path, backend, module="messaging", tools=[tool], grants=["read", "send", "upload", "send_file"])[0]
    values = {"account": "synthetic", "chat_id": "chat_synthetic", "filename": "upload", "mime": "text/plain", "bytes": 1,
              "sha256": "a" * 64, "request_id": "synthetic-request"}
    rest, mcp = post_both(app, tool, values)
    assert rest.status_code == 409 and mcp["isError"]
    for key in ("error", "limit", "active", "max", "resets_in_seconds", "hint", "retryable"):
        assert rest.json().get(key) == body[key], key
        assert mcp["structuredContent"].get(key) == body[key], key


# spec: ANYM-660
def test_an_unreviewed_hint_that_repeats_the_query_is_still_dropped(tmp_path):
    body = {"error": "invalid_filter", "request_id": "r1", "retryable": False, "hint": "swap the range bounds"}
    rest, mcp = post_both(app_with_refusing_backend(tmp_path, "messages_list_chats", body), "messages_list_chats", {"query": "range"})
    assert "hint" not in rest.json() and "hint" not in mcp["structuredContent"]
