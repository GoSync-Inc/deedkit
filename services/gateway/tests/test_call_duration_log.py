"""One content-free duration line per tool call, through MCP and REST (ANYM-843); synthetic only."""
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from test_policy import config
from test_transport import rpc

ROOT = Path(__file__).resolve().parents[3]
FIELDS = {"tool", "outcome", "duration_ms"}
HEADERS = {"Authorization": "Bearer gateway-a"}


class Lines(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class Raising(logging.Handler):
    def emit(self, record):
        raise RuntimeError("synthetic handler failure")

    def handleError(self, record):
        raise RuntimeError("synthetic handler failure")


@pytest.fixture
def lines():
    logger = logging.getLogger("agent_gateway.calls")
    handler = Lines()
    logger.addHandler(handler)
    yield handler.lines
    logger.removeHandler(handler)


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "policy.json"
    key = tmp_path / "calendar.key"
    key.write_text("backend-calendar-a")
    key.chmod(0o600)
    value = config()
    value["principals"][0]["credentials"]["calendar"]["key_file"] = str(key)
    path.write_text(json.dumps(value))
    path.chmod(0o600)

    def handler(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["agenda:read", "availability:read"]})
        return httpx.Response(200, json={"coverage": "unknown", "calendars": [], "unbound_members": ["synthetic-child"]})

    return create_app(path, "http://testserver", transports={"calendar": httpx.MockTransport(handler)})


def parsed(lines):
    rows = [json.loads(line) for line in lines]
    for row in rows:
        assert set(row) == FIELDS, row
        assert isinstance(row["duration_ms"], int) and row["duration_ms"] >= 0, row
    return rows


def content_free(lines):
    text = "\n".join(lines)
    for leaked in ("agent-a", "gateway-a", "backend-calendar-a", "synthetic-child", "coverage", "Bearer"):
        assert leaked not in text, leaked


# spec: ANYM-843
def test_a_successful_call_logs_one_line_with_exactly_tool_outcome_and_duration_in_both_transports(app, lines):
    with TestClient(app) as client:
        assert not rpc(client, "tools/call", {"name": "calendar_calendars", "arguments": {}}).json()["result"].get(
            "isError")
        assert len(lines) == 1
        assert client.post("/v1/calendar_calendars", json={}, headers=HEADERS).status_code == 200
        assert len(lines) == 2
    rows = parsed(lines)
    assert [(r["tool"], r["outcome"]) for r in rows] == [("calendar_calendars", "ok")] * 2
    content_free(lines)


# spec: ANYM-843
def test_a_denied_call_logs_its_safe_error_code(app, lines):
    with TestClient(app) as client:
        # Catalogued but outside this key's profile: denied with the gateway's own code.
        assert rpc(client, "tools/call", {"name": "calendar_apply", "arguments": {}}).json()["result"]["isError"]
        assert client.post("/v1/calendar_apply", json={}, headers=HEADERS).status_code == 403
        assert client.post("/v1/calendar_calendars", content=b"{", headers=HEADERS).status_code == 422
    rows = parsed(lines)
    assert [(r["tool"], r["outcome"]) for r in rows] == [
        ("calendar_apply", "not_found_or_forbidden"),
        ("calendar_apply", "not_found_or_forbidden"),
        ("calendar_calendars", "invalid_arguments"),
    ]
    content_free(lines)


# spec: ANYM-843
def test_a_call_that_raises_logs_error_and_nothing_of_the_exception(app, lines, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("synthetic secret detail chat_0123")

    monkeypatch.setattr(app.state.dispatch, "call", boom)
    with TestClient(app, raise_server_exceptions=False) as client:
        mcp = rpc(client, "tools/call", {"name": "calendar_calendars", "arguments": {}})
        body = mcp.json() if mcp.headers.get("content-type", "").startswith("application/json") else {}
        # A JSON-RPC error or an HTTP failure; never a successful result.
        assert mcp.status_code >= 500 or "error" in body or body["result"].get("isError"), (mcp.status_code, body)
        assert client.post("/v1/calendar_calendars", json={}, headers=HEADERS).status_code == 500
    rows = parsed(lines)
    assert [(r["tool"], r["outcome"]) for r in rows] == [("calendar_calendars", "error")] * 2
    assert "synthetic secret" not in "\n".join(lines) and "chat_0123" not in "\n".join(lines)


# spec: ANYM-843
def test_a_tool_name_outside_the_catalog_is_logged_as_unknown_never_as_sent(app, lines):
    sent = "chat_0123456789abcdef_owner_secret"
    with TestClient(app) as client:
        assert rpc(client, "tools/call", {"name": sent, "arguments": {}}).json()["result"]["isError"]
        assert client.post("/v1/" + sent, json={}, headers=HEADERS).status_code == 403
    rows = parsed(lines)
    assert [r["tool"] for r in rows] == ["unknown", "unknown"]
    assert all(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", r["outcome"]) for r in rows)
    assert sent not in "\n".join(lines)


# spec: ANYM-843
def test_a_logging_handler_that_raises_does_not_change_the_response(app, lines):
    with TestClient(app) as client:
        before = (
            rpc(client, "tools/call", {"name": "calendar_calendars", "arguments": {}}).json()["result"],
            client.post("/v1/calendar_calendars", json={}, headers=HEADERS),
            client.post("/v1/calendar_apply", json={}, headers=HEADERS),
        )
        logger = logging.getLogger("agent_gateway.calls")
        raising = Raising()
        logger.addHandler(raising)
        try:
            after = (
                rpc(client, "tools/call", {"name": "calendar_calendars", "arguments": {}}).json()["result"],
                client.post("/v1/calendar_calendars", json={}, headers=HEADERS),
                client.post("/v1/calendar_apply", json={}, headers=HEADERS),
            )
        finally:
            logger.removeHandler(raising)
    assert after[0] == before[0]
    for old, new in zip(before[1:], after[1:]):
        assert (new.status_code, new.json()) == (old.status_code, old.json())


def documented_program():
    """The Python program of the one-liner in docs/operations/production.md (Tool call durations)."""
    text = (ROOT / "docs/operations/production.md").read_text()
    section = text[text.index("### Tool call durations (ANYM-843)"):]
    line = next(x for x in section.splitlines() if "python3 -c '" in x)
    return line[line.index("python3 -c '") + len("python3 -c '"):line.rindex("'")]


# spec: ANYM-843
def test_the_documented_one_liner_prints_p50_and_p90_per_tool_from_captured_lines(app, lines):
    with TestClient(app) as client:
        for _ in range(3):
            client.post("/v1/calendar_calendars", json={}, headers=HEADERS)
        client.post("/v1/calendar_apply", json={}, headers=HEADERS)
    rows = parsed(lines)
    # Other container output around the lines is ignored.
    log = "\n".join(["INFO:     Started server process [1]", '{"other":1}', *lines, "gateway key warning"]) + "\n"
    out = subprocess.run([sys.executable, "-c", documented_program()], input=log, capture_output=True, text=True,
                         check=True, timeout=30).stdout.splitlines()
    expected = {}
    for row in rows:
        expected.setdefault(row["tool"], []).append(row["duration_ms"])
    assert out == [
        f"{tool} calls={len(v)} p50_ms={sorted(v)[(len(v) - 1) // 2]} p90_ms={sorted(v)[(9 * len(v) - 1) // 10]}"
        for tool, v in sorted(expected.items())
    ]
    assert [x.split()[0] for x in out] == ["calendar_apply", "calendar_calendars"]
