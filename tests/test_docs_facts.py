"""Documentation facts that drifted from the code stay corrected (ANYM-776): each test reads a document and its source."""
import ast
import json
from pathlib import Path
import re

from scripts import deedkit_admin as admin
from scripts import docs_only
from scripts import production

ROOT = Path(__file__).resolve().parents[1]
RECORDINGS_CONFIG = "/opt/deedkit/recordings-private/config.json"


# reads: services/gateway/README.md
# reads: TIDEN-CI.md
# reads: .claude/rules/migrations.md
def flat(path):
    """A document with its line wrapping undone, so a line break cannot hide a phrase."""
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


def module_purposes():
    """The gateway's module table, by source: the root suite does not install the gateway package."""
    tree = ast.parse((ROOT / "services/gateway/src/agent_gateway/dispatch.py").read_text(encoding="utf-8"))
    return next(ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "MODULE_PURPOSES")


def optional_admin_keys():
    """Every literal key the admin CLI reads with `config.get("...")`, with its fallback expression (None: no fallback)."""
    tree = ast.parse((ROOT / "scripts/deedkit_admin.py").read_text(encoding="utf-8"))
    return {node.args[0].value: node.args[1] if len(node.args) > 1 else None for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "config"
            and node.args and isinstance(node.args[0], ast.Constant)}


# spec: ANYM-776
def test_the_gateway_readme_lists_the_modules_the_gateway_accepts():
    readme = flat("services/gateway/README.md")
    sentence = re.search(r"with `m` from (.*?) is the same MCP endpoint", readme)
    assert sentence, "the module sentence of 'Choosing modules' moved"
    assert set(re.findall(r"`([a-z]+)`", sentence.group(1))) == set(module_purposes())
    assert "all the modules the key can use" in readme and "all three modules" not in readme


# spec: ANYM-776
def test_the_gateway_readme_shows_a_recordings_path_and_what_a_key_sees():
    readme = flat("services/gateway/README.md")
    assert "/mcp/modules/calendar,messaging" in readme and "/mcp/modules/recordings" in readme
    assert "a key sees a module's tools only when its credentials include that module" in readme


# spec: ANYM-776
def test_the_codex_budget_section_points_at_the_meter_and_holds_no_numbers():
    text = (ROOT / "docs/operations/review-protocol.md").read_text(encoding="utf-8")
    section = " ".join(text.split("## Codex budget\n", 1)[1].split("\n## ", 1)[0].split())
    assert "4 points" not in section and "68%" not in section
    assert "`~/deedkit-orchestrator/bin/codex-meter.sh`" in section
    assert "`CODEX_DAILY_POINTS`" in section and "`CODEX_LINE_OFFSET`" in section
    assert "release, deploy and `production.py`, sends and live provider-session code" in section


# spec: ANYM-776
def test_a_local_tiden_write_needs_the_orchestrators_window_not_the_owners():
    document = flat("TIDEN-CI.md")
    assert "agreed with the owner" not in document
    assert "a maintenance window the orchestrator opens (tiden-product-workflow.md)" in document


# spec: ANYM-776
def test_every_script_a_rule_file_names_exists():
    named = {name for path in (ROOT / ".claude/rules").glob("*.md")
             for name in re.findall(r"(?<![\w./-])scripts/([\w./-]+\.py)", path.read_text(encoding="utf-8"))}
    assert "production.py" in named  # the scan still finds a path
    assert sorted(name for name in named if not (ROOT / "scripts" / name).is_file()) == []


# spec: ANYM-776
def test_the_migration_rule_holds_no_number_that_goes_stale():
    rule = flat(".claude/rules/migrations.md")
    assert not re.search(r"\b06[0-9]_|version>\d|schema \d", rule)
    assert "the highest numbered file in `services/messaging/messaging_service/migrations/`" in rule
    assert "`RUNTIME_SCHEMA` in `services/messaging/messaging_service/store.py`" in rule
    store = (ROOT / "services/messaging/messaging_service/store.py").read_text(encoding="utf-8")
    assert re.search(r"^RUNTIME_SCHEMA=\d+", store, re.M)
    assert "section 3 and the signal grep in docs/operations/review-protocol.md" in rule


# spec: ANYM-776
def test_the_admin_example_holds_every_optional_key_the_admin_cli_reads():
    example = json.loads((ROOT / "deploy/admin.example.json").read_text(encoding="utf-8"))
    optional = optional_admin_keys()
    assert "recordings_config" in optional  # the scan still finds a key
    assert sorted(optional.keys() - example.keys()) == []
    assert example["recordings_config"] == RECORDINGS_CONFIG


# spec: ANYM-776
def test_the_admin_example_gives_a_defaulted_key_the_value_the_cli_defaults_to():
    example = json.loads((ROOT / "deploy/admin.example.json").read_text(encoding="utf-8"))
    fallback = optional_admin_keys()
    defaults = {"tool": str(admin.TOOL), "http_config": production.HTTP_CONFIG, "calls_lock": str(admin.CALLS_LOCK),
                "docker": ast.literal_eval(fallback["docker"]), "validators": ast.literal_eval(fallback["validators"]),
                "journal": str(admin.JOURNAL), "tokens": str(admin.TOKENS)}
    assert {key: example.get(key) for key in defaults} == defaults


# spec: ANYM-776
def test_this_module_keeps_unrelated_documents_in_the_docs_only_skip():
    reads = docs_only.scan_python("tests/test_docs_facts.py", (ROOT / "tests/test_docs_facts.py").read_bytes())
    assert reads.dynamic == {".claude/rules", "scripts"}  # no read of the repository root
    assert {"services/gateway/README.md", "TIDEN-CI.md", "docs/operations/review-protocol.md"} <= reads.names
