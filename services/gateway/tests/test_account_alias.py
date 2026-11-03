"""ANYM-646: the gateway folds `account` into `accounts` itself, so a messaging server without the field takes both tools."""
import json

import httpx
import pytest

from agent_gateway.catalog import CATALOG
from test_diagnostics import configured
from test_strict_arguments import post_both

TOOLS = ["messages_list_chats", "messages_search_people"]


def stub(tmp_path, tool, seen):
    """A backend whose schemas lack `account`: it answers only what it was sent, and records it."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    operation = tool.removeprefix("messages_")

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read"], "tool_manifest": {"names": [operation]}})
        body = json.loads(request.content)
        assert "account" not in body, "the backend never sees the singular field"
        seen.append(body)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    return configured(tmp_path, backend, module="messaging", tools=[tool], grants=["read"])[0]


# spec: ANYM-646
@pytest.mark.parametrize("tool", TOOLS)
def test_account_is_folded_into_accounts_before_the_backend_sees_it(tmp_path, tool):
    seen = []
    for number, (values, accounts) in enumerate([
        ({"account": "synthetic-a"}, ["synthetic-a"]),
        ({"account": "synthetic-a", "accounts": ["synthetic-a", "synthetic-b"]}, ["synthetic-a"]),
        ({"account": "synthetic-b", "accounts": ["synthetic-a"]}, []),
        ({"account": "", "accounts": ["synthetic-a"]}, ["synthetic-a"]),
        ({"account": ""}, None),
        ({"accounts": ["synthetic-a"]}, ["synthetic-a"]),
    ]):
        seen.clear()
        # One application per case: a TestClient runs the application's lifespan once.
        rest, mcp = post_both(stub(tmp_path / str(number), tool, seen), tool, values)
        assert rest.status_code == 200 and not mcp["isError"]
        assert len(seen) == 2 and seen[0] == seen[1]  # REST and MCP forward the same arguments
        assert seen[0].get("accounts") == accounts, values


# spec: ANYM-646
@pytest.mark.parametrize("tool", TOOLS)
def test_both_tools_expose_account_as_a_string(tool):
    properties = CATALOG[tool]["inputSchema"]["properties"]
    assert properties["account"]["type"] == "string" and "accounts" in properties
    assert "account" in CATALOG[tool]["description"] and "accounts" in CATALOG[tool]["description"]
    assert CATALOG[tool]["inputSchema"]["additionalProperties"] is False
