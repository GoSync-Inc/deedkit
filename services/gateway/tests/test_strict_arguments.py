"""Reject invalid JSON numbers before backend admission or ambiguous write reporting."""
import json
import re

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.catalog import CATALOG, InvalidArguments, arguments
from agent_gateway.field_errors import REASONS
from test_diagnostics import configured, HEADERS


SEND = {"account": "account-1", "chat_id": "chat-1", "text": "Synthetic", "request_id": "request-1"}
ACK = {"subscription_id": "subscription-1", "receipt": "r" * 43, "version": 1}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("name,args,field", [
    ("messages_send_message", SEND, "wait_s"),
    ("messages_wait_for_subscription_events", {"subscription_id": "subscription-1"}, "timeout"),
])
def test_wait_numbers_are_finite(name, args, field, value):
    with pytest.raises(ValueError, match="invalid_arguments"):
        arguments(CATALOG[name], {**args, field: value})


@pytest.mark.parametrize("name,values", [
    ("messages_ack_subscription_events", {**ACK, "version": 1.0}),
    ("messages_search", {"limit": 1.0}),
    ("messages_upload_file_chunk", {"upload_id": "up_" + "a" * 32, "offset": 0.0, "data_base64": "eA=="}),
])
def test_integer_schema_rejects_integral_floats(name, values):
    with pytest.raises(ValueError, match="invalid_arguments"):
        arguments(CATALOG[name], values)


def test_strict_integer_schema_applies_recursively_through_arrays_and_unions():
    tool = {"operation": "synthetic", "inputSchema": {
        "type": "object", "properties": {"rows": {"type": "array", "items": {
            "type": "object", "properties": {"version": {"anyOf": [{"type": "integer"}, {"type": "null"}]}},
        }}},
    }}
    assert arguments(tool, {"rows": [{"version": 1}, {"version": None}]})["rows"][0]["version"] == 1
    for invalid in (1.0, True):
        with pytest.raises(ValueError, match="invalid_arguments"):
            arguments(tool, {"rows": [{"version": invalid}]})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_numbers_are_rejected_inside_untyped_operation_parameters(value):
    values = {"account": "account-1", "operation": "rename_group", "parameters": {"nested": [{"value": value}]}}
    with pytest.raises(ValueError, match="invalid_arguments"):
        arguments(CATALOG["messages_preview_messenger_operation"], values)


@pytest.mark.parametrize("name,values", [
    ("messages_send_message", {**SEND, "wait_s": float("nan")}),
    ("messages_wait_for_subscription_events", {"subscription_id": "subscription-1", "timeout": float("nan")}),
    ("messages_ack_subscription_events", {**ACK, "version": 1.0}),
])
def test_invalid_numbers_never_reach_backend_capabilities_or_dispatch_over_either_transport(tmp_path, name, values):
    requests = []

    def backend(request):
        requests.append(request.url.path)
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read", "send", "subscription-ack"],
                                            "tool_manifest": {"names": [name.removeprefix("messages_")]}})
        return httpx.Response(200, json={"synthetic": True})

    app, _, _ = configured(tmp_path, backend, module="messaging", tools=[name],
                           grants=["read", "send", "subscription-ack"])
    headers = {**HEADERS, "Content-Type": "application/json"}
    with TestClient(app) as client:
        # Raw JSON represents the incoming wire edge; HTTPX's json= encoder
        # correctly refuses non-finite Python floats before transmission.
        rest = client.post("/v1/" + name, content=json.dumps(values), headers=headers)
        mcp = client.post("/mcp", content=json.dumps({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                          "params": {"name": name, "arguments": values}}),
                          headers={**headers, "Accept": "application/json, text/event-stream"})
    result = mcp.json()["result"]
    assert rest.status_code == 422 and rest.json()["error"] == "invalid_arguments"
    assert result["isError"] and result["structuredContent"] == rest.json()
    assert requests == []


@pytest.mark.parametrize("value", [0, 0.0, 0.5, 25, 25.0])
def test_number_fields_accept_finite_integers_and_floats_without_coercion(value):
    assert arguments(CATALOG["messages_send_message"], {**SEND, "wait_s": value})["wait_s"] == value
    assert arguments(CATALOG["messages_wait_for_subscription_events"],
                     {"subscription_id": "subscription-1", "timeout": value})["timeout"] == value


DIAL = {"brief_id": "brief-1", "authorization_reference": "Call the clinic for me"}
CANARY = "CANARY-secret-4f9c"


def calls_app(tmp_path, requests):
    def backend(request):
        requests.append(request.url.path)
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["calls:dial"], "tool_manifest": {"names": ["dial_call"]}})
        return httpx.Response(200, json={"synthetic": True})

    return configured(tmp_path, backend, module="calls", tools=["calls_dial_call"], grants=["calls:dial"])[0]


def post_both(app, name, values):
    headers = {**HEADERS, "Content-Type": "application/json"}
    with TestClient(app) as client:
        rest = client.post("/v1/" + name, content=json.dumps(values), headers=headers)
        mcp = client.post("/mcp", content=json.dumps({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                          "params": {"name": name, "arguments": values}}),
                          headers={**headers, "Accept": "application/json, text/event-stream"})
    return rest, mcp.json()["result"]


def test_dial_without_destination_source_names_the_field_over_both_transports(tmp_path):
    requests = []
    rest, mcp = post_both(calls_app(tmp_path, requests), "calls_dial_call", DIAL)
    expected = {"error": "invalid_arguments", "next_action": "check_parameters",
                "field_errors": [{"field": "destination_source", "reason": "required"}]}
    assert rest.status_code == 422 and rest.json() == expected
    assert mcp["isError"] and mcp["structuredContent"] == expected
    assert "/v1/dial_call" not in requests


def test_field_errors_enumerate_all():
    tool = CATALOG["calls_dial_call"]
    with pytest.raises(InvalidArguments) as caught:
        arguments(tool, {"brief_id": "", "authorization_reference": "ab", "destination_source": "someone",
                         "destination_evidence": 5, "extra": 1})
    assert caught.value.field_errors == [
        {"field": "brief_id", "reason": "blank", "expected": "length at least 1"},
        {"field": "authorization_reference", "reason": "min_length", "expected": "length at least 3"},
        {"field": "destination_source", "reason": "enum", "allowed": ["owner_named", "agent_found"]},
        {"field": "destination_evidence", "reason": "type", "expected": "string"},
        {"field": "extra", "reason": "unknown_field"},
    ]


def test_field_errors_never_echo_input(tmp_path):
    tool = CATALOG["calls_dial_call"]
    values = {"brief_id": 7, "authorization_reference": [CANARY], "destination_source": CANARY,
              "destination_evidence": {CANARY: CANARY}, CANARY: CANARY, "ok_but_unknown": CANARY}
    with pytest.raises(InvalidArguments) as caught:
        arguments(tool, values)
    assert CANARY not in json.dumps(caught.value.field_errors)
    assert {"field": "$", "reason": "unknown_field"} in caught.value.field_errors
    assert {"field": "ok_but_unknown", "reason": "unknown_field"} in caught.value.field_errors
    rest, mcp = post_both(calls_app(tmp_path, []), "calls_dial_call", values)
    assert CANARY not in rest.text and CANARY not in json.dumps(mcp)


def test_every_catalog_tool_yields_bounded_field_errors_from_the_closed_vocabulary():
    for name, tool in CATALOG.items():
        for values in ({}, {CANARY: CANARY, "x" * 200: 1},
                       {key: CANARY for key in tool["inputSchema"].get("properties", {})}):
            try:
                arguments(tool, values)
            except InvalidArguments as error:
                assert 0 < len(error.field_errors) <= 20, name
                assert len({e["field"] for e in error.field_errors}) == len(error.field_errors), name
                for entry in error.field_errors:
                    assert entry["reason"] in REASONS, (name, entry)
                    assert entry["field"] == "$" or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(/[A-Za-z0-9_]+)*", entry["field"]), (name, entry)
                    assert len(entry["field"]) <= 128 and CANARY not in json.dumps(entry), (name, entry)
            except ValueError as error:
                pytest.fail(f"{name}: bare ValueError {error}")


def test_non_schema_rejections_and_valid_calls_keep_their_existing_shape():
    with pytest.raises(ValueError) as caught:
        arguments(CATALOG["messages_send_message"], {**SEND, "wait_s": float("nan")})
    assert not isinstance(caught.value, InvalidArguments) and str(caught.value) == "invalid_arguments"
    assert arguments(CATALOG["calls_dial_call"], {**DIAL, "destination_source": "owner_named"})["destination_source"] == "owner_named"
