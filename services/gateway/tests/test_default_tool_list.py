"""The default tool list is the everyday layer; low-level tools stay reachable on an `advanced` path (ANYM-550)."""
import hashlib
import itertools
import json
import tomllib
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import ACTIONS, CATALOG, StrictValidator, arguments, public
from agent_gateway.diagnostics import with_catalog
from agent_gateway.dispatch import MODULE_PURPOSES, Route
from agent_gateway.policy import Denied
from agent_gateway.tiers import EVERYDAY, REPLACED_BY, default_list
from test_policy import config

ROOT = Path(__file__).resolve().parents[3]
ACCEPT = {"Accept": "application/json, text/event-stream"}
HEADERS = {"Authorization": "Bearer gateway-a"}
MODULES = ("messaging", "calls")
RISK_TOOLS = [n for n in EVERYDAY if CATALOG[n].get("routes_actions")]
# The synthetic everyday agent: send and the risk-class (ANYM-549) operations, with the everyday tools and the
# low-level twins they replace.
FIXTURE_TOOLS = [n for n in EVERYDAY if CATALOG[n]["module"] in MODULES + ("gateway",)] + list(REPLACED_BY)
FIXTURE_ACTIONS = sorted({a for n in RISK_TOOLS for a in CATALOG[n]["routes_actions"]})


def scopes(module, tools, actions=()):
    return sorted({s for n in tools if CATALOG[n]["module"] == module for s in CATALOG[n]["scopes"]}
                  | ({s for a in actions for s in ACTIONS[a]} if module == "messaging" else set()))


def gateway(tmp_path, tools, actions):
    value = config()
    value["deployment"] = {"modules": list(MODULES), "backends": {m: {"url": f"http://{m}.test"} for m in MODULES}}
    value["profiles"]["reader"] = {"tools": tools, "actions": {"messenger_operations": actions}}
    agent = value["principals"][0]
    agent["grants"] = {m: scopes(m, tools, actions) for m in MODULES}
    agent["actions"] = {"messenger_operations": actions}
    agent["credentials"] = {}
    for module in MODULES:
        key = tmp_path / f"{module}.key"
        key.write_text(f"backend-{module}")
        key.chmod(0o600)
        agent["credentials"][module] = {"key_file": str(key), "key_sha256": hashlib.sha256(key.read_bytes()).hexdigest()}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    calls = []

    def handler(module):
        def respond(request):
            if request.url.path == "/v1/capabilities":
                names = [t["operation"] for t in CATALOG.values() if t["module"] == module]
                return httpx.Response(200, json={
                    "permissions": agent["grants"][module], "tool_manifest": {"names": names},
                    "messenger_operations": {"supported_by_account": {"synthetic": {"operations": sorted(ACTIONS)}}}})
            calls.append((module, request.url.path))
            return httpx.Response(200, json={"status": "ok"})
        return httpx.MockTransport(respond)

    return create_app(path, "http://testserver", transports={m: handler(m) for m in MODULES}), calls


def rpc(client, path, method, params=None):
    response = client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                           headers={**HEADERS, **ACCEPT})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def listing(client, path):
    return [t["name"] for t in rpc(client, path, "tools/list")["tools"]]


def size(names, actions):
    return len(json.dumps([public(CATALOG[n], actions) for n in names], ensure_ascii=False).encode())


# spec: ANYM-550
def test_the_everyday_fixture_default_list_has_at_most_15_tools_and_advanced_keeps_the_rest(tmp_path):
    app, _ = gateway(tmp_path, FIXTURE_TOOLS, FIXTURE_ACTIONS)
    with TestClient(app) as client:
        default, advanced = listing(client, "/mcp"), listing(client, "/mcp/modules/advanced")
    assert set(advanced) == set(FIXTURE_TOOLS)
    assert default == [n for n in advanced if n in EVERYDAY]
    assert len(default) <= 15
    # The baseline the PR records: before is today's list, the advanced path.
    print("before", len(advanced), size(advanced, FIXTURE_ACTIONS), "after", len(default),
          size(default, FIXTURE_ACTIONS))


def published_example(tool):
    description = tool["description"]
    assert len(description.split()) <= 80, tool["name"]
    assert description.count("Example: ") == 1, tool["name"]
    example, _ = json.JSONDecoder().raw_decode(description, description.index("Example: ") + len("Example: "))
    assert isinstance(example, dict), tool["name"]
    return example


# spec: ANYM-550
def test_every_everyday_description_is_short_and_its_one_example_passes_the_schema():
    for name in EVERYDAY:
        # A risk-class tool publishes its example per key: check every single-operation grant it routes.
        grants = [[op] for op in CATALOG[name].get("routes_actions") or []] or [None]
        for actions in grants:
            published = with_catalog(public(CATALOG[name], actions), {"catalog": "0" * 12})
            example = published_example(published)
            # The published schema this key receives, then the gateway's own admission.
            StrictValidator(published["inputSchema"]).validate(example)
            assert arguments(CATALOG[name], example) is not None, (name, actions)
            if actions:
                assert example["operation"] == actions[0]


# spec: ANYM-550
def test_a_delete_only_key_gets_a_delete_example_in_its_tool_list(tmp_path):
    app, _ = gateway(tmp_path, FIXTURE_TOOLS, ["delete_message"])
    with TestClient(app) as client:
        tools = {t["name"]: t for t in rpc(client, "/mcp", "tools/list")["tools"]}
    tool = tools["messages_change_message"]
    assert tool["inputSchema"]["properties"]["operation"]["enum"] == ["delete_message"]
    example = published_example(tool)
    assert example["operation"] == "delete_message"
    StrictValidator(tool["inputSchema"]).validate(example)


# spec: ANYM-550
def test_each_low_level_tool_leaves_the_default_list_only_for_its_everyday_twin():
    assert set(EVERYDAY) <= CATALOG.keys() and set(REPLACED_BY) <= CATALOG.keys()
    assert not set(EVERYDAY) & set(REPLACED_BY)
    for old, new in REPLACED_BY.items():
        assert new in EVERYDAY and CATALOG[old]["module"] == CATALOG[new]["module"], old
        # Without its twin the low-level tool stays; with it, it leaves.
        assert default_list([old]) == [old] and default_list([old, new]) == [new]
    # Calendar keeps every tool until find-time lands (ANYM-553); a tool with no twin never leaves.
    everything = list(CATALOG)
    assert set(everything) - set(default_list(everything)) == set(REPLACED_BY)
    assert all(CATALOG[n]["module"] in ("messaging", "calls") for n in REPLACED_BY)


# spec: ANYM-550
def test_every_checked_in_profile_names_only_existing_tools():
    profiles = sorted((ROOT / "config/profiles").glob("*.toml"))
    assert profiles
    for path in profiles:
        tools = tomllib.loads(path.read_text())["exposure"]["tools"]
        assert set(tools) <= CATALOG.keys(), (path.name, sorted(set(tools) - CATALOG.keys()))


# spec: ANYM-550
def test_platform_capabilities_names_every_tool_with_the_path_that_lists_it(tmp_path):
    app, _ = gateway(tmp_path, FIXTURE_TOOLS, FIXTURE_ACTIONS)
    with TestClient(app) as client:
        result = rpc(client, "/mcp", "tools/call", {"name": "platform_capabilities", "arguments": {}})
        rows = result["structuredContent"]["tool_paths"]
        assert sorted(r["tool"] for r in rows) == sorted(FIXTURE_TOOLS)
        assert result["structuredContent"]["tools"] == listing(client, "/mcp")
        for row in rows:
            assert row["tier"] == ("everyday" if row["tool"] in EVERYDAY else "advanced"), row
            assert row["tool"] in listing(client, row["path"]), row
            assert "next" not in row, row
            assert row.get("replaced_by") == REPLACED_BY.get(row["tool"]), row


# spec: ANYM-550
def test_an_old_tool_stays_callable_on_the_default_path(tmp_path):
    app, calls = gateway(tmp_path, FIXTURE_TOOLS, FIXTURE_ACTIONS)
    with TestClient(app) as client:
        assert "calls_get_call_analysis" not in listing(client, "/mcp")
        result = rpc(client, "/mcp", "tools/call", {"name": "calls_get_call_analysis", "arguments": {"call_id": "c-1"}})
    assert not result.get("isError"), result
    assert ("calls", "/v1/get_call_analysis") in calls


# spec: ANYM-550
def test_every_module_path_parses_with_and_without_advanced_in_any_order():
    names = sorted(MODULE_PURPOSES)
    for count in range(len(names) + 1):
        for chosen in itertools.combinations(names, count):
            for extra in ((), ("advanced",)):
                for order in itertools.permutations(chosen + extra):
                    if not order:
                        continue
                    route = Route.parse("/mcp/modules/" + ",".join(order))
                    assert route.modules == (frozenset(chosen) or None) and route.advanced is bool(extra)
                    assert Route.parse(route.path).path == route.path
    for bad in ("advanced,advanced", "advanced,", "Advanced", "messaging,advanced,messaging"):
        with pytest.raises(Denied):
            Route.parse("/mcp/modules/" + bad)
    assert Route.parse("/mcp").advanced is False


# spec: ANYM-550
def test_a_tool_path_narrowed_by_a_selection_names_the_selection_that_lists_it_again(tmp_path):
    app, _ = gateway(tmp_path, FIXTURE_TOOLS + ["platform_select_modules"], FIXTURE_ACTIONS)
    with TestClient(app) as client:
        rpc(client, "/mcp/modules/messaging", "tools/call",
            {"name": "platform_select_modules", "arguments": {"modules": ["calls"]}})
        rows = {r["tool"]: r for r in rpc(client, "/mcp", "tools/call", {
            "name": "platform_capabilities", "arguments": {}})["structuredContent"]["tool_paths"]}
        row = rows["messages_send"]
        assert row["path"] == "/mcp/modules/messaging" and "messages_send" not in listing(client, row["path"])
        assert row["next"] == {"tool": "platform_select_modules", "arguments": {"modules": ["calls", "messaging"]}}
        rpc(client, row["path"], "tools/call", {"name": "platform_select_modules", "arguments": row["next"]["arguments"]})
        assert "messages_send" in listing(client, row["path"])
        assert "next" not in rows["calls_call"]
