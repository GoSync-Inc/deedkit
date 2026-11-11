"""Per-module recipes and the catalog changelog behind platform_capabilities(module, since) (ANYM-964)."""
from copy import deepcopy
from importlib.resources import files
import importlib.util
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from test_module_selection import call

pytest_plugins = ("test_module_selection",)

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "catalog_changelog.py"


def catalog_rows():
    return json.loads(files("agent_gateway").joinpath("catalog.json").read_text())


def helper():
    spec = importlib.util.spec_from_file_location("catalog_changelog", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def recipe_file():
    return json.loads(files("agent_gateway").joinpath("recipes.json").read_text())


def changelog_file():
    return json.loads(files("agent_gateway").joinpath("changelog.json").read_text())


# spec: ANYM-964
def test_reviewed_recipes_cover_the_first_modules_within_bounds():
    from agent_gateway.dispatch import MODULE_PURPOSES
    from agent_gateway.recipes import RECIPES

    assert set(RECIPES) <= set(MODULE_PURPOSES)
    assert {"messaging", "calendar", "calls", "android"} <= set(RECIPES)
    ids = [recipe["id"] for rows in RECIPES.values() for recipe in rows]
    assert len(ids) == len(set(ids))
    assert {"read_a_chat", "send_safely"} <= {r["id"] for r in RECIPES["messaging"]}
    assert {"find_free_time", "plan_a_change"} <= {r["id"] for r in RECIPES["calendar"]}
    assert {"one_call"} <= {r["id"] for r in RECIPES["calls"]}
    assert {"run_saved_flow"} <= {r["id"] for r in RECIPES["android"]}
    for rows in RECIPES.values():
        assert len(rows) <= 6
        for recipe in rows:
            assert len(recipe["title"]) <= 80 and 2 <= len(recipe["steps"]) <= 8
            assert all(len(step["note"]) <= 160 for step in recipe["steps"])


def broken(change):
    value = recipe_file()
    change(value)
    return value


# spec: ANYM-964
@pytest.mark.parametrize("change", [
    lambda v: v["messaging"][0]["steps"][0].update(tool="messages_no_such_tool"),
    lambda v: v["messaging"][0]["steps"][0].update(arguments={"no_such_field": 1}),
    lambda v: v["messaging"][0]["steps"][0].update(note="x" * 161),
    lambda v: v["messaging"][0].update(title="x" * 81),
    lambda v: v["messaging"][0].update(steps=v["messaging"][0]["steps"][:1]),
    lambda v: v["messaging"][0].update(steps=v["messaging"][0]["steps"] * 9),
    lambda v: v["messaging"].extend(deepcopy(v["messaging"][0]) | {"id": f"extra_{i}"} for i in range(6)),
    lambda v: v["messaging"].append(deepcopy(v["messaging"][0])),
    lambda v: v["messaging"][0]["steps"][0].update(tool="calendar_calendars"),
    lambda v: v["messaging"][0].update(extra=True),
    lambda v: v["messaging"][0].pop("when"),
], ids=["unknown-tool", "schema", "note", "title", "one-step", "nine-steps", "seven-recipes", "duplicate-id",
        "other-module", "unknown-key", "missing-key"])
def test_a_malformed_recipe_file_fails_to_load(change):
    from agent_gateway.recipes import load_recipes

    load_recipes(recipe_file())
    with pytest.raises(ValueError):
        load_recipes(broken(change))


# spec: ANYM-964
def test_a_recipe_is_served_whole_or_not_at_all():
    from agent_gateway.catalog import CATALOG
    from agent_gateway.recipes import recipes_for

    everything = list(CATALOG)
    full = {r["id"] for r in recipes_for("messaging", everything)}
    assert "send_safely" in full
    without_send = recipes_for("messaging", [n for n in everything if n != "messages_send"])
    assert "send_safely" not in {r["id"] for r in without_send}
    assert all(step["tool"] != "messages_send" for r in without_send for step in r["steps"])
    assert recipes_for("recordings", everything) == [] or all(
        step["tool"] in CATALOG for r in recipes_for("recordings", everything) for step in r["steps"])


# spec: ANYM-964
def test_a_module_recipes_part_stays_under_16_kib():
    from agent_gateway.catalog import CATALOG
    from agent_gateway.dispatch import MODULE_PURPOSES
    from agent_gateway.recipes import recipes_for

    for module in MODULE_PURPOSES:
        size = len(json.dumps(recipes_for(module, list(CATALOG)), ensure_ascii=False, separators=(",", ":")).encode())
        assert size < 16 * 1024, (module, size)


# spec: ANYM-964
def test_the_newest_changelog_entry_pins_the_packaged_catalog():
    from agent_gateway.recipes import CHANGELOG, catalog_sha256

    digest = catalog_sha256(catalog_rows())
    assert CHANGELOG[-1]["catalog_sha256"] == digest, (
        "catalog.json changed without a changelog entry: run python3 scripts/catalog_changelog.py --add --note '...'")
    assert helper().catalog_sha256(catalog_rows()) == digest
    assert [entry["revision"] for entry in CHANGELOG] == list(range(1, len(CHANGELOG) + 1))
    first = CHANGELOG[0]
    assert (first["added"], first["changed"], first["removed"]) == ([], [], [])


# spec: ANYM-964
def test_the_digest_covers_names_and_schemas_not_descriptions():
    from agent_gateway.recipes import catalog_sha256

    rows = catalog_rows()
    digest = catalog_sha256(rows)
    described = deepcopy(rows)
    described[0]["description"] += " More words."
    assert catalog_sha256(described) == digest
    renamed = deepcopy(rows)
    renamed[0]["name"] += "_v2"
    assert catalog_sha256(renamed) != digest
    reshaped = deepcopy(rows)
    reshaped[0]["inputSchema"]["properties"]["added_field"] = {"type": "string"}
    assert catalog_sha256(reshaped) != digest


# spec: ANYM-964
@pytest.mark.parametrize("change", [
    lambda v: v.append(dict(v[-1], revision=v[-1]["revision"] + 2)),
    lambda v: v[-1].update(note="x" * 201),
    lambda v: v[-1].update(catalog_sha256="abc"),
    lambda v: v[-1].update(date="11/11/2026"),
    lambda v: v[-1].update(added="messages_send"),
    lambda v: v[-1].pop("module"),
    lambda v: v.clear(),
], ids=["revision-gap", "note", "digest", "date", "added-not-list", "missing-key", "empty"])
def test_a_malformed_changelog_fails_to_load(change):
    from agent_gateway.recipes import load_changelog

    load_changelog(changelog_file())
    value = changelog_file()
    change(value)
    with pytest.raises(ValueError):
        load_changelog(value)


# spec: ANYM-964
def test_the_helper_writes_the_diff_part_of_the_next_entry():
    script = helper()
    old = [{"name": "m_a", "module": "messaging", "inputSchema": {"type": "object"}},
           {"name": "m_b", "module": "messaging", "inputSchema": {"type": "object"}},
           {"name": "m_d", "module": "messaging", "inputSchema": {"type": "object"}, "description": "old"}]
    new = [{"name": "m_a", "module": "messaging", "inputSchema": {"type": "object", "properties": {}}},
           {"name": "m_c", "module": "messaging", "inputSchema": {"type": "object"}},
           {"name": "m_d", "module": "messaging", "inputSchema": {"type": "object"}, "description": "new"}]
    changelog = [{"revision": 1, "date": "2026-11-11", "module": "gateway", "added": [], "changed": [],
                  "removed": [], "note": "Baseline.", "catalog_sha256": script.catalog_sha256(old)}]
    entry = script.next_entry(old, new, changelog, note="Chats gain m_c.", today="2026-11-12")
    assert entry == {"revision": 2, "date": "2026-11-12", "module": "messaging", "added": ["m_c"],
                     "changed": ["m_a"], "removed": ["m_b"], "note": "Chats gain m_c.",
                     "catalog_sha256": script.catalog_sha256(new)}
    with pytest.raises(ValueError):  # the base catalog is not the one the newest entry pins
        script.next_entry(new, new + [], changelog, note="x", today="2026-11-12")
    mixed = new + [{"name": "c_x", "module": "calendar", "inputSchema": {"type": "object"}}]
    with pytest.raises(ValueError):  # two modules need an explicit --module
        script.next_entry(old, mixed, changelog, note="x", today="2026-11-12")
    assert script.next_entry(old, mixed, changelog, note="x", today="2026-11-12", module="messaging")["added"] == [
        "c_x", "m_c"]


# spec: ANYM-964
def test_capabilities_module_argument_names_the_deployable_modules():
    from agent_gateway.catalog import CATALOG, arguments
    from agent_gateway.dispatch import MODULE_PURPOSES

    schema = CATALOG["platform_capabilities"]["inputSchema"]
    assert schema["properties"]["module"]["enum"] == sorted(MODULE_PURPOSES)
    assert arguments(CATALOG["platform_capabilities"], {}) == {}
    description = CATALOG["platform_capabilities"]["description"]
    assert "module" in description and "since" in description and "recipes" in description
    assert "whats_new" in description


def with_send(value):
    value["profiles"]["reader"]["tools"] += ["messages_get_messages", "messages_send"]
    value["principals"][0]["grants"]["messaging"] = ["read", "send"]


# spec: ANYM-964
def test_an_empty_call_answers_as_before_plus_recipe_ids_and_revision(gateway):
    from agent_gateway.recipes import CHANGELOG

    app, _, write, _ = gateway
    write(with_send)
    with TestClient(app) as client:
        result = call(client, "platform_capabilities")["structuredContent"]
        other = call(client, "platform_capabilities", key="b")["structuredContent"]
    assert result["changelog_revision"] == CHANGELOG[-1]["revision"]
    assert "recipes" not in result and "whats_new" not in result
    rows = {row["id"]: row for row in result["modules_available"]}
    assert set(rows["messaging"]) == {"id", "purpose", "tool_count", "selected", "path", "recipe_ids"}
    assert {"read_a_chat", "send_safely"} <= set(rows["messaging"]["recipe_ids"])
    # Key b has the same profile but no send grant: no send recipe, the read recipe stays.
    others = {row["id"]: row for row in other["modules_available"]}
    assert "send_safely" not in others["messaging"]["recipe_ids"]
    assert "read_a_chat" in others["messaging"]["recipe_ids"]


# spec: ANYM-964
def test_module_recipes_are_identical_on_every_path_and_filtered_by_key(gateway):
    app, _, write, _ = gateway
    write(lambda v: (with_send(v), v["profiles"]["reader"]["tools"].append("platform_call_tool")))
    with TestClient(app) as client:
        answers = [call(client, "platform_capabilities", {"module": "messaging"}, path=path)["structuredContent"]
                   for path in ("/mcp/compact", "/mcp/modules/messaging", "/mcp")]
        forwarded = call(client, "platform_call_tool", {"name": "platform_capabilities",
                                                        "arguments": {"module": "messaging"}}, path="/mcp/compact")
        without = call(client, "platform_capabilities", {"module": "messaging"}, key="b")["structuredContent"]
        invalid = call(client, "platform_capabilities", {"module": "telepathy"})
    recipes = answers[0]["recipes"]
    assert all(answer["recipes"] == recipes for answer in answers)
    assert not forwarded.get("isError"), forwarded
    assert forwarded["structuredContent"]["recipes"] == recipes
    assert "send_safely" in {r["id"] for r in recipes}
    assert "send_safely" not in {r["id"] for r in without["recipes"]}
    assert "messages_send" not in json.dumps(without["recipes"])
    assert invalid["isError"] and invalid["structuredContent"]["error"] == "invalid_arguments"


SYNTHETIC = [
    {"revision": 1, "date": "2026-11-11", "module": "gateway", "added": [], "changed": [], "removed": [],
     "note": "Baseline.", "catalog_sha256": "0" * 64},
    {"revision": 2, "date": "2026-11-12", "module": "messaging", "added": [], "changed": ["messages_get_messages"],
     "removed": ["messages_old_read"], "note": "Reads gain a field.", "catalog_sha256": "1" * 64},
    {"revision": 3, "date": "2026-11-13", "module": "messaging", "added": ["messages_send"], "changed": [],
     "removed": [], "note": "A new send tool.", "catalog_sha256": "2" * 64},
]


# spec: ANYM-964
def test_whats_new_lists_only_tools_the_key_can_see(gateway, monkeypatch):
    from agent_gateway import recipes

    monkeypatch.setattr(recipes, "CHANGELOG", deepcopy(SYNTHETIC))
    app, _, write, _ = gateway
    write(with_send)
    with TestClient(app) as client:
        owner = call(client, "platform_capabilities", {"since": 1})["structuredContent"]
        reader = call(client, "platform_capabilities", {"since": 0}, key="b")["structuredContent"]
        current = call(client, "platform_capabilities", {"since": 3}, key="b")["structuredContent"]
    assert owner["changelog_revision"] == 3
    assert [(e["revision"], e["added"], e["changed"]) for e in owner["whats_new"]] == [
        (2, [], ["messages_get_messages"]), (3, ["messages_send"], [])]
    assert owner["whats_new"][0]["removed"] == ["messages_old_read"]
    # The forbidden tool added in revision 3 never appears for a key without it; its entry is dropped whole.
    assert [e["revision"] for e in reader["whats_new"]] == [1, 2]
    assert "messages_send" not in json.dumps(reader)
    assert current["whats_new"] == []


# spec: ANYM-964
@pytest.mark.parametrize("since", [4, -1])
def test_since_outside_the_changelog_names_the_current_revision(gateway, monkeypatch, since):
    from agent_gateway import recipes

    monkeypatch.setattr(recipes, "CHANGELOG", deepcopy(SYNTHETIC))
    app, _, _, requests = gateway
    with TestClient(app) as client:
        result = call(client, "platform_capabilities", {"since": since})
    assert result["isError"]
    detail = result["structuredContent"]
    assert detail["error"] == "invalid_arguments" and detail["changelog_revision"] == 3
    assert detail["field_errors"] == [{"field": "since", "reason": "maximum" if since > 0 else "minimum"}]
    assert requests == []
