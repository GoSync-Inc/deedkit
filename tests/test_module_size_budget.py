"""Size ratchet: no source file of 900+ lines grows past its committed line count or longest function.

config/module-size-budget.json lists each such file's `lines` and `longest_function` (lines of its longest def or
func, the definition line through its end) as a ceiling. A file that grows past it fails until the same PR raises
its entry, so review sees the growth in the diff. A shrink or a neutral edit needs no budget edit (parallel PRs on one
monolith then do not all edit the same entry, ANYM-625); any PR may lower an entry, and a periodic tighten PR sets
them to the current sizes. A source file that reaches the threshold needs a new entry; a deleted file's entry goes.
"""
import ast
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BUDGET = "config/module-size-budget.json"
SUFFIXES = (".py", ".go", ".js", ".mjs", ".ts", ".tsx")
TEST_DIRS = {"tests", "test", "fixtures", "testdata", "node_modules", "vendor"}
BRACE_FUNC = {".go": re.compile(r"func\b"), **dict.fromkeys((".js", ".mjs", ".ts", ".tsx"),
              re.compile(r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\b"))}
ROUTE = ("Prefer moving code out (an MCP tool into app_tools/<domain>.py, a deploy subcommand into its own bundle "
         "module, a collector behaviour into a mixin module). If the growth is deliberate, raise the entry in "
         f"{BUDGET} in this same PR, where review sees it; an open PR that grows a file sets its entry to its own head.")


def is_source(path):
    parts = Path(path).parts
    name = parts[-1]
    return (name.endswith(SUFFIXES) and not name.endswith(".d.ts") and not TEST_DIRS & set(parts[:-1])
            and not name.startswith("test_") and name != "conftest.py" and not name.endswith("_test.go")
            and ".test." not in name and ".spec." not in name)


def longest_function(path, text):
    """Lines of the longest function: Python by its AST, Go and JS from a top-level `func`/`function` line to the
    next line that is only `}` (gofmt and the repository's JS style close a top-level function at column 0)."""
    if path.endswith(".py"):
        return max(((node.end_lineno or node.lineno) - node.lineno + 1 for node in ast.walk(ast.parse(text))
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))), default=0)
    start, lines, best = BRACE_FUNC[Path(path).suffix], text.splitlines(), 0
    for index, line in enumerate(lines):
        if not start.match(line):
            continue
        if line.count("{") and line.count("{") == line.count("}"):
            best = max(best, 1)
            continue
        end = next((n for n in range(index + 1, len(lines)) if lines[n] == "}"), index)
        best = max(best, end - index + 1)
    return best


def measure(path, text):
    return {"lines": len(text.splitlines()), "longest_function": longest_function(path, text)}


def entry_line(path, size):
    return f'"{path}": {{"lines": {size["lines"]}, "longest_function": {size["longest_function"]}}}'


def problems(root, paths, budget, threshold):
    """Every difference between the source files under `root` and the budget, each with the entry line to commit."""
    found, budgeted = [], budget
    for path in sorted(p for p in paths if is_source(p)):
        file = Path(root, path)
        if not file.is_file():
            continue
        size = measure(path, file.read_text(encoding="utf-8"))
        want = budgeted.get(path)
        if want is None:
            if size["lines"] >= threshold:
                found.append(f"{path} has {size['lines']} lines (threshold {threshold}) and no budget entry: "
                             f"add {entry_line(path, size)}. {ROUTE}")
            continue
        for key in ("lines", "longest_function"):
            if size[key] > want[key]:
                found.append(f"{path} grew past its budget: {key} {size[key]} > {want[key]}. {ROUTE} "
                             f"New entry: {entry_line(path, size)}")
    present = {p for p in paths if Path(root, p).is_file()}
    found += [f"{path} is gone or is no source file: remove its entry from {BUDGET}"
              for path in sorted(budgeted) if path not in present or not is_source(path)]
    return found


def load_budget(text):
    document = json.loads(text)
    assert set(document) == {"format", "threshold", "modules"} and document["format"] == 1, document.keys()
    modules = document["modules"]
    assert list(modules) == sorted(modules), f"{BUDGET} modules must be sorted by path"
    assert all(set(v) == {"lines", "longest_function"} and all(type(n) is int for n in v.values())
               for v in modules.values()), f"{BUDGET} entries need exactly integer lines and longest_function"
    return modules, document["threshold"]


def tracked(root):
    listing = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             check=True, capture_output=True, text=True).stdout
    return [p for p in listing.split("\0") if p]


# spec: ANYM-949
def test_every_large_source_file_matches_its_size_budget():
    modules, threshold = load_budget((ROOT / BUDGET).read_text())
    found = problems(ROOT, tracked(ROOT), modules, threshold)
    assert not found, "\n".join(found)


# spec: ANYM-949
def test_the_archive_tools_live_in_their_app_tools_module_not_in_app_py():
    package = ROOT / "services/messaging/messaging_service"
    def tools(path):
        return {node.name for node in ast.walk(ast.parse(path.read_text()))
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and any(ast.unparse(d).startswith("mcp.tool(") for d in node.decorator_list)}
    archive = {"search_people", "search", "search_files", "get_file_text", "get_messages", "get_context",
               "get_file_segments", "get_sync_status", "resolve_reference", "resolve_target", "get_file_occurrences",
               "get_reviews", "record_review", "get_attachment", "request_file_download"}
    assert tools(package / "app_tools/archive.py") == archive
    assert not tools(package / "app.py") & archive


# spec: ANYM-949
def test_the_subscription_and_delivery_tools_live_in_their_app_tools_module_not_in_app_py():
    package = ROOT / "services/messaging/messaging_service"
    def tools(path):
        return {node.name for node in ast.walk(ast.parse(path.read_text()))
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and any(ast.unparse(d).startswith("mcp.tool(") for d in node.decorator_list)}
    moved = {"create_subscription", "list_subscriptions", "get_subscription", "update_subscription",
             "get_subscription_events", "fetch_subscription_events", "ack_subscription_events",
             "wait_for_subscription_events", "configure_subscription_delivery", "test_subscription",
             "register_delivery_endpoint", "list_delivery_endpoints", "update_delivery_endpoint",
             "list_delivery_attempts", "delete_delivery_endpoint", "test_delivery_endpoint", "register_wake_target",
             "list_wake_targets", "update_wake_target", "delete_wake_target", "confirm_wake_target"}
    assert len(moved) == 21
    assert tools(package / "app_tools/subscriptions.py") == moved
    assert not tools(package / "app.py") & moved


# spec: ANYM-949
def test_the_conversation_and_update_tools_live_in_their_app_tools_modules_not_in_app_py():
    package = ROOT / "services/messaging/messaging_service"
    def tools(path):
        return {node.name for node in ast.walk(ast.parse(path.read_text()))
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and any(ast.unparse(d).startswith("mcp.tool(") for d in node.decorator_list)}
    conversations = {"list_chats", "dismiss_reply", "snooze_reply", "restore_reply", "mute_reply_source",
                     "unmute_reply_source", "list_reply_dismissals", "list_negotiations"}
    updates = {"get_updates", "ack_updates"}
    assert tools(package / "app_tools/conversations.py") == conversations
    assert tools(package / "app_tools/updates.py") == updates
    assert not tools(package / "app.py") & (conversations | updates)


# spec: ANYM-949
def test_ratchet_flags_growth_a_new_large_file_and_a_stale_entry_but_not_a_shrink(tmp_path):
    def write(name, text):
        Path(tmp_path, name).parent.mkdir(parents=True, exist_ok=True)
        Path(tmp_path, name).write_text(text)
    body = "def big():\n" + "    x = 1\n" * 5 + "\n\ndef small():\n    return 2\n"
    write("app.py", body)
    write("grown.py", body.replace("x = 1", "x = 1\n    y = 2", 1))
    write("shrunk.py", body)
    write("new.go", "package x\n\nfunc init() { run() }\nfunc Long() {\n\ta()\n\tb()\n}\n")
    write("tests/test_big.py", body * 3)
    exact = measure("app.py", body)
    assert exact == {"lines": 10, "longest_function": 6}
    assert measure("new.go", Path(tmp_path, "new.go").read_text()) == {"lines": 7, "longest_function": 4}
    budget = {"app.py": exact, "gone.py": exact, "grown.py": exact, "shrunk.py": {"lines": 11, "longest_function": 7}}
    paths = ["app.py", "grown.py", "shrunk.py", "new.go", "tests/test_big.py"]
    found = problems(tmp_path, paths, budget, threshold=7)
    assert [line.split(":")[0] for line in found] == [
        "grown.py grew past its budget", "grown.py grew past its budget",
        "new.go has 7 lines (threshold 7) and no budget entry", "gone.py is gone or is no source file"], found
    assert found[0].startswith("grown.py grew past its budget: lines 11 > 10") and BUDGET in found[0]
    assert found[1].startswith("grown.py grew past its budget: longest_function 7 > 6")
    assert found[1].endswith('New entry: "grown.py": {"lines": 11, "longest_function": 7}')
    assert problems(tmp_path, ["app.py"], {"app.py": exact}, threshold=7) == []
