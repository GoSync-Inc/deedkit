import importlib.util
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "stack_check", ROOT / "scripts/check_stack.py"
)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_common_locks_are_equal_with_independent_domain_dependencies():
    result = checker.check(ROOT)
    assert result["errors"] == []
    assert {r["module"] for r in result["modules"]} == {
        "messaging",
        "calendar",
        "calls",
        "gateway",
        "recordings",
    }


def test_resolved_drift_cannot_hide_behind_declared_version(tmp_path):
    for relative in [
        "config/stack.toml",
        "services/messaging/uv.lock",
        *[f"services/{m}/uv.lock" for m in ("calendar", "calls", "gateway", "recordings")],
    ]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, path)
    path = tmp_path / "services/calls/uv.lock"
    path.write_text(
        path.read_text().replace(
            'name = "mcp"\nversion = "2.3.0"', 'name = "mcp"\nversion = "0.0.0"'
        )
    )
    result = checker.check(tmp_path)
    assert result["status"] == "failed" and result["errors"] == [
        "calls: mcp must resolve to 2.3.0"
    ]
