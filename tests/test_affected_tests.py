import json
from pathlib import Path
import subprocess
import sys

from hypothesis import given, settings, strategies as st
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import affected_tests as a  # noqa: E402

WEIGHTS = {"default": 3, "seconds": {}}
FILES = {
    "pkg/app/__init__.py": "",
    "pkg/app/store.py": "from . import archive\n",
    "pkg/app/archive.py": "VALUE = 1\n",
    "pkg/app/people.py": "VALUE = 2\n",
    "pkg/app/migrations/001.sql": "select 1;\n",
    "deploy/compose.json": "{}\n",
    "scripts/tool.py": "X = 1\n",
    "tests/conftest.py": "from app.store import VALUE\n",
    "tests/test_archive.py": "from app.archive import VALUE\n",
    "tests/test_people.py": "from app import people\n",
    "tests/test_deploy.py": "PATH = 'deploy/compose.json'\n",
    "tests/test_tool.py": "import importlib\nTOOL = importlib.import_module('tool')\n",
    "tests/test_alone.py": "X = 1\n",
    "services/gateway/x.py": "",
}


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    root = tmp_path_factory.mktemp("repo")
    for path, text in FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    for command in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                                   "commit", "-qm", "synthetic"]):
        subprocess.run(["git", "-C", str(root), *command], check=True)
    return a.Tree(root)


def selected(tree, *changed):
    return a.plan(tree, list(changed), WEIGHTS)


# spec: ANYM-323
def test_a_changed_module_selects_its_importers_and_the_conftest_store_reach_only_one_level(tree):
    # The conftest imports the store directly: a store change reaches every test, an archive change only its importer.
    assert selected(tree, "pkg/app/store.py")["python"] == sorted(p for p in FILES if p.startswith("tests/test_"))
    assert selected(tree, "pkg/app/archive.py")["python"] == ["tests/test_archive.py"]
    assert selected(tree, "pkg/app/people.py")["python"] == ["tests/test_people.py"]


# spec: ANYM-323
def test_literal_paths_module_strings_and_package_data_select_their_readers(tree):
    assert selected(tree, "deploy/compose.json")["python"] == ["tests/test_deploy.py"]
    assert selected(tree, "scripts/tool.py")["python"] == ["tests/test_tool.py"]
    # A non-Python file inside a package belongs to the package, which every importer reaches.
    assert "tests/test_archive.py" in selected(tree, "pkg/app/migrations/001.sql")["python"]
    assert selected(tree, "tests/test_alone.py")["python"] == ["tests/test_alone.py"]


# spec: ANYM-323
@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "scripts/affected_tests.py", "pyproject.toml",
                                  "uv.lock", "tools/dev/pyproject.toml"])
def test_a_full_suite_path_or_an_empty_change_plans_the_full_suite(tree, path):
    assert selected(tree, path)["mode"] == "full"
    assert selected(tree)["mode"] == "full"


# spec: ANYM-323
def test_module_suites_follow_their_trees_and_a_large_selection_runs_in_full(tree):
    plan = selected(tree, "services/gateway/x.py")
    assert plan["mode"] == "affected" and {"gateway", "calendar", "calls", "load", "release"} <= set(plan["suites"])
    heavy = a.plan(tree, ["pkg/app/store.py"], {"default": a.MAX_AFFECTED_SECONDS, "seconds": {}})
    assert heavy["mode"] == "full"


# spec: ANYM-323
@settings(max_examples=40, deadline=None)
@given(st.sets(st.sampled_from(sorted(p for p in FILES if not p.startswith("services/"))), min_size=1),
       st.sampled_from(sorted(FILES)))
def test_more_changed_paths_never_select_fewer_tests_or_suites(tree, changed, extra):
    small, large = selected(tree, *sorted(changed)), selected(tree, *sorted(changed | {extra}))
    assert set(small["python"]) <= set(large["python"]) and set(small["suites"]) <= set(large["suites"])


SHA = "a" * 40
DRAFT_RUN = {"DECISION": "", "REUSE_RESULT": "skipped", "REUSED_SHA": "", "PR_DECISION": "", "PR_REUSE_RESULT": "skipped",
             "PR_REUSED_SHA": "", "VERIFIED_SHA": "", "DOCS_ONLY": "false", "DOCS_SHA": "", "AFFECTED": "true",
             "AFFECTED_SHA": SHA, "DRAFT": "true", "EVENT_NAME": "pull_request", "REF": "refs/pull/1/merge",
             "EXPECTED_SHA": SHA}


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


# spec: ANYM-323
@pytest.mark.parametrize("change, passes", [
    ({}, True), ({"DRAFT": "false"}, False), ({"DRAFT": ""}, False), ({"EVENT_NAME": "merge_group"}, False),
    ({"EVENT_NAME": "push", "REF": "refs/heads/main"}, False), ({"AFFECTED_SHA": ""}, False),
    ({"AFFECTED_SHA": "b" * 40}, False), ({"VERIFIED_SHA": SHA}, False), ({"AFFECTED": "false"}, False),
], ids=["draft", "ready", "no-draft-flag", "queue", "main-push", "affected-job-skipped", "other-commit",
        "verify-also-ran", "not-affected"])
def test_ci_gate_passes_an_affected_only_run_only_for_a_draft_whose_affected_job_tested_this_commit(change, passes):
    step = workflow("ci.yml")["jobs"]["gate"]["steps"][1]
    assert step["env"]["AFFECTED"] == "${{ needs.checks.outputs.affected }}"
    assert step["env"]["AFFECTED_SHA"] == "${{ needs.checks.outputs.affected_sha }}"
    assert step["env"]["DRAFT"] == "${{ github.event.pull_request.draft }}"
    completed = subprocess.run(["bash", "-e", "-c", step["run"]], env={"PATH": "/usr/bin:/bin", **DRAFT_RUN, **change},
                               capture_output=True)
    assert (completed.returncode == 0) == passes


# spec: ANYM-323
def test_only_a_draft_plans_affected_tests_with_the_base_selector_and_the_full_jobs_then_skip():
    ci, checks, images = workflow("ci.yml"), workflow("checks.yml"), workflow("images.yml")
    assert "ready_for_review" in ci[True]["pull_request"]["types"]
    documentation = checks["jobs"]["documentation"]
    plan = next(step for step in documentation["steps"] if step.get("id") == "plan")
    assert plan["if"] == "github.event_name == 'pull_request' && github.event.pull_request.draft == true"
    assert plan["continue-on-error"] is True
    assert "git show HEAD^1:scripts/affected_tests.py" in plan["run"] and "HEAD^1:scripts/python_shards.json" in plan["run"]
    assert documentation["outputs"]["affected"].startswith("${{ steps.plan.outcome == 'success' &&")
    skip = "needs.documentation.outputs.affected != 'true'"
    for name in ("verify", "load", "go", "added", "evidence", "python-2", "python-3", "python-4", "python-5", "python-6"):
        assert skip in checks["jobs"][name]["if"], name
    affected = checks["jobs"]["affected"]
    assert affected["if"] == ("${{ !cancelled() && needs.documentation.outputs.affected == 'true' "
                              "&& needs.documentation.outputs.docs_only != 'true' }}")
    assert checks[True]["workflow_call"]["outputs"]["affected_sha"]["value"] == "${{ jobs.affected.outputs.source_sha }}"
    # No new secret, permission or artifact: the job inherits the workflow's read-only token and uploads nothing.
    assert "permissions" not in affected and "secrets" not in json.dumps(affected)
    assert all("upload-artifact" not in str(step.get("uses", "")) for step in affected["steps"])
    assert "DRAFT" in next(step for step in images["jobs"]["changes"]["steps"] if step.get("id") == "decide")["env"]


# spec: ANYM-323
@pytest.mark.parametrize("module", ["tests/test_backup_r2_verifier_native.py", "tests/test_collectors.py",
                                    "tests/test_audio_native.py"])
def test_the_affected_job_runs_the_sudo_and_image_nodes_the_shards_leave_out_when_their_module_is_selected(module):
    steps = workflow("checks.yml")["jobs"]["affected"]["steps"]
    assert any(f"' {module} '" in str(step.get("if", "")) for step in steps)
