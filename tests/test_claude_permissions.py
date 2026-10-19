"""Claude Code permission lists classify every DeedKit connector tool once, following owner Rule B (ANYM-309); reads files only."""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG_FILE = ROOT / "services/gateway/src/agent_gateway/catalog.json"
CATALOG_CODE = ROOT / "services/gateway/src/agent_gateway/catalog.py"
SETTINGS_FILE = ROOT / ".claude/settings.json"
PREFIX = "mcp__claude_ai_Deedkit__"
# The gateway adds this tool in code (services/gateway/src/agent_gateway/catalog.py), outside catalog.json, read-only.
PLATFORM_CAPABILITIES = {"name": "platform_capabilities", "annotations": {"readOnlyHint": True}}

# Owner Rule B (PR #74, 2026-10-17, owner: "Да, календарь без подтверждения"): provider reads and internal bookkeeping
# count as reads and stay in `allow`; these four are read-annotated but stay in `ask`. Changing either table needs a PR
# that cites a recorded owner decision.
RULE_B_ALLOW = {"calendar_agenda", "calendar_slots", "messages_list_subscriptions", "messages_get_subscription",
                "messages_get_subscription_events"}
RULE_B_ASK = {"messages_fetch_subscription_events", "messages_wait_for_subscription_events", "messages_get_attachment",
              "calls_preflight_call"}
# A readOnlyHint=false tool in `allow` needs its own recorded owner decision.
OWNER_ALLOWED_WRITES = {"calls_raise_call_cap": 'PR #109, owner 2026-10-18: "Да, для всех агентов"'}


def load():
    return json.loads(CATALOG_FILE.read_text()) + [PLATFORM_CAPABILITIES], json.loads(SETTINGS_FILE.read_text())


def hint_of(tool):
    """The gateway treats a missing hint as a write (services/gateway/src/agent_gateway/dispatch.py)."""
    return tool.get("annotations", {}).get("readOnlyHint", False)


def problems(catalog, settings):
    """Every way the permission lists disagree with the catalog and the pinned decisions."""
    read_only = {tool["name"]: hint_of(tool) for tool in catalog}
    lists = settings["permissions"]
    found = []
    seen = {}
    for kind in ("allow", "ask"):
        for entry in lists.get(kind, []):
            if not entry.startswith("mcp__") or "deedkit" not in entry.casefold():
                continue
            name = entry[len(PREFIX):]
            if not entry.startswith(PREFIX):
                found.append(f"{entry} in {kind} names the DeedKit server outside the pinned prefix")
                continue
            if name not in read_only:
                found.append(f"{entry} in {kind} names no catalog tool")
                continue
            if name in seen:
                found.append(f"{name} is classified twice ({seen[name]}, {kind})")
            seen[name] = kind
    for entry in lists.get("deny", []):
        if entry.startswith("mcp__") and "deedkit" in entry.casefold():
            found.append(f"{entry} in deny: DeedKit tools are classified only in allow and ask")
    for name, hint in read_only.items():
        if name not in seen:
            found.append(f"{name} is unclassified")
        elif seen[name] == "allow" and hint is not True and name not in OWNER_ALLOWED_WRITES:
            found.append(f"{name} (readOnlyHint={hint}) is in allow without a recorded owner decision")
    for name, kind in sorted(seen.items()):
        if kind == "ask" and read_only[name] is True and name not in RULE_B_ASK:
            found.append(f"{name} is read-annotated in ask outside the Rule B table")
    for table, kind in ((RULE_B_ALLOW, "allow"), (RULE_B_ASK, "ask")):
        for name in sorted(table):
            if seen.get(name) != kind:
                found.append(f"Rule B: {name} must be in {kind}, found {seen.get(name)}")
            if read_only.get(name) is not True:
                found.append(f"Rule B: {name} is no longer read-annotated; its row needs an owner decision")
    for name in OWNER_ALLOWED_WRITES:
        if seen.get(name) != "allow" or read_only.get(name) is not False:
            found.append(f"owner exception {name} no longer matches the catalog and allow")
    return found


def move(settings, name, source, target):
    settings = copy.deepcopy(settings)
    settings["permissions"][source].remove(PREFIX + name)
    if target:
        settings["permissions"][target].append(PREFIX + name)
    return settings


# spec: ANYM-309
def test_current_files_classify_every_tool_once():
    catalog, settings = load()
    assert len(catalog) == len({tool["name"] for tool in catalog}), "duplicate catalog names"
    assert problems(catalog, settings) == []


# spec: ANYM-309
def test_rule_b_table_is_exact_and_disjoint():
    catalog, settings = load()
    assert not RULE_B_ALLOW & RULE_B_ASK
    assert (len(RULE_B_ALLOW), len(RULE_B_ASK), set(OWNER_ALLOWED_WRITES)) == (5, 4, {"calls_raise_call_cap"})
    assert problems(catalog, settings) == []


# spec: ANYM-309
def test_new_or_renamed_catalog_tool_without_classification_fails():
    catalog, settings = load()
    added = catalog + [{"name": "messages_brand_new", "annotations": {"readOnlyHint": True}}]
    assert problems(added, settings) == ["messages_brand_new is unclassified"]
    renamed = [dict(tool, name="messages_search_all") if tool["name"] == "messages_search" else tool for tool in catalog]
    assert problems(renamed, settings) == [f"{PREFIX}messages_search in allow names no catalog tool",
                                           "messages_search_all is unclassified"]


# spec: ANYM-309
def test_changed_read_only_hint_on_a_classified_tool_fails():
    catalog, settings = load()
    flipped = [dict(tool, annotations={"readOnlyHint": False}) if tool["name"] == "messages_search" else tool
               for tool in catalog]
    assert problems(flipped, settings) == ["messages_search (readOnlyHint=False) is in allow without a recorded owner decision"]
    # A new owner-excepted tool needs its own recorded decision, and the excepted tool must stay a write in `allow`.
    healed = [dict(tool, annotations={"readOnlyHint": True}) if tool["name"] == "calls_raise_call_cap" else tool
              for tool in catalog]
    assert problems(healed, settings) == ["owner exception calls_raise_call_cap no longer matches the catalog and allow"]


# spec: ANYM-309
def test_double_or_missing_classification_and_rule_b_moves_fail():
    catalog, settings = load()
    twice = copy.deepcopy(settings)
    twice["permissions"]["ask"].append(PREFIX + "messages_search")
    assert problems(catalog, twice) == ["messages_search is classified twice (allow, ask)",
                                        "messages_search is read-annotated in ask outside the Rule B table"]
    assert problems(catalog, move(settings, "messages_search", "allow", None)) == ["messages_search is unclassified"]
    assert problems(catalog, move(settings, "calendar_agenda", "allow", "ask")) == [
        "calendar_agenda is read-annotated in ask outside the Rule B table",
        "Rule B: calendar_agenda must be in allow, found ask"]
    assert problems(catalog, move(settings, "calls_preflight_call", "ask", "allow")) == [
        "Rule B: calls_preflight_call must be in ask, found allow"]
    assert problems(catalog, move(settings, "messages_send_message", "ask", "allow")) == [
        "messages_send_message (readOnlyHint=False) is in allow without a recorded owner decision"]
    denied = copy.deepcopy(settings)
    denied["permissions"]["deny"].append(PREFIX + "messages_search")
    assert problems(catalog, denied) == [f"{PREFIX}messages_search in deny: DeedKit tools are classified only in allow and ask"]


# spec: ANYM-309
def test_other_spellings_of_the_server_and_a_missing_hint_fail():
    catalog, settings = load()
    for entry in (PREFIX.rstrip("_"), "mcp__deedkit__messages_send_message", "mcp__claude_ai_DeedKit__messages_search"):
        granted = copy.deepcopy(settings)
        granted["permissions"]["allow"].append(entry)
        assert problems(catalog, granted) == [f"{entry} in allow names the DeedKit server outside the pinned prefix"]
    unhinted = [dict(tool, annotations={}) if tool["name"] == "messages_search" else tool for tool in catalog]
    assert problems(unhinted, settings) == ["messages_search (readOnlyHint=False) is in allow without a recorded owner decision"]
    assert problems(catalog, move(settings, "messages_search", "allow", "ask")) == [
        "messages_search is read-annotated in ask outside the Rule B table"]
    flipped = [dict(tool, annotations={"readOnlyHint": True}) if tool["name"] == "messages_send_message" else tool
               for tool in catalog]
    assert problems(flipped, settings) == ["messages_send_message is read-annotated in ask outside the Rule B table"]


# spec: ANYM-309
def test_platform_capabilities_constant_matches_the_gateway_code():
    source = CATALOG_CODE.read_text()
    block = source[source.index('CATALOG["platform_capabilities"]'):]
    assert '"name": "platform_capabilities"' in block and '"readOnlyHint": True' in block


# spec: ANYM-309
def test_renamed_ask_tool_and_foreign_ask_entries_are_listed_not_raised():
    catalog, settings = load()
    renamed = [dict(tool, name="calls_stop_call_now") if tool["name"] == "calls_stop_call" else tool for tool in catalog]
    assert problems(renamed, settings) == [f"{PREFIX}calls_stop_call in ask names no catalog tool",
                                           "calls_stop_call_now is unclassified"]
    foreign = copy.deepcopy(settings)
    foreign["permissions"]["ask"].append("mcp__deedkit__messages_search")
    assert problems(catalog, foreign) == ["mcp__deedkit__messages_search in ask names the DeedKit server outside the pinned prefix"]
