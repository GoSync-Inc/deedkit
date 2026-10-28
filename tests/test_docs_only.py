"""A pull request or merge queue run of documentation only skips the test jobs (ANYM-323); synthetic git repositories."""
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from scripts import docs_only

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ("docs/guide.md", "NOTES.md", "docs/заметки.md", "docs/gone.md", ".claude/rules/tests.md",
             ".claude/settings.json", "README.md", "docs/README.md", "TELEMETRY.md", "services/x/GUIDE.md")
INTENT = "Tiden intent branch: [intent/2026-10-17-x](https://app.tiden.ai/a/products/b/branches/c)"


def git(root, *arguments):
    environment = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": str(root), "PATH": os.environ["PATH"],
                   "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True,
                          env=environment).stdout.strip()


def repository(tmp_path):
    """A base commit with documents and code; returns its root."""
    root = tmp_path / "repo"
    for path in (*DOCUMENTS, "scripts/app.py"):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(f"{path}\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def pull_request(root, change):
    """Commit `change` on a head branch and check out GitHub's two-parent merge of it; returns the head SHA."""
    git(root, "switch", "-q", "-c", "feature")
    change(root)
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", "head")
    head = git(root, "rev-parse", "HEAD")
    git(root, "switch", "-q", "main")
    (root / "base-moved.txt").write_text("main moved on\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "main moved on")
    git(root, "merge", "-q", "--no-ff", "--no-edit", "feature")
    # The merge brings base-moved.txt from the first parent, so only the head's change is in the diff.
    return head


def group(root, change):
    """Commit `change` as a single-parent merge queue group commit on main; returns (group SHA, base SHA)."""
    base = git(root, "rev-parse", "HEAD")
    change(root)
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", "group")
    return git(root, "rev-parse", "HEAD"), base


def edit(*paths):
    return lambda root: [(root / path).write_text("edited\n") for path in paths]


def decide(tmp_path, change, event="pull_request", body=""):
    root = repository(tmp_path)
    if event == "merge_group":
        head, base = group(root, change)
        return docs_only.decide(root, event, head, body, base)
    head = pull_request(root, change)
    return docs_only.decide(root, event, head, body)


# spec: ANYM-323
@pytest.mark.parametrize("path, documentation", [
    ("AGENTS.md", True), ("docs/operations/production.md", True), ("docs/заметки.md", True),
    (".claude/rules/tests.md", True), (".claude/settings.json", True), (".claude/hooks/check.sh", True),
    ("README.md", False), ("docs/README.md", False), ("SETUP.md", False), ("THIRD_PARTY_NOTICES.md", False),
    ("TELEMETRY.md", False), ("LICENSE", False), ("LICENSE.md", False), ("docs/COPYING.md", False),
    ("licenses/MIT.md", False), ("services/calendar/AGENTS.md", False), ("packages/npm/CHANGELOG.md", False),
    ("whatsapp/NOTES.md", False), ("scripts/app.py", False), (".claude", False), ("docs/guide.MD", False),
    ("docs/../scripts/app.md", False), ("", False),
], ids=lambda value: str(value) or "empty")
def test_the_glob_rule_accepts_markdown_and_agent_rules_outside_the_deny_list(path, documentation):
    assert docs_only.eligible(path) is documentation


# spec: ANYM-323
@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize("change, count", [
    (edit("docs/guide.md", "NOTES.md", "docs/заметки.md", ".claude/rules/tests.md", ".claude/settings.json"), 5),
    (lambda root: (root / "docs/gone.md").unlink(), 1),
    (lambda root: (root / "docs/new/page.md").parent.mkdir() or (root / "docs/new/page.md").write_text("new\n"), 1),
    (lambda root: git(root, "mv", "docs/gone.md", "docs/moved.md"), 2),
], ids=["edits", "deletion", "new-document", "rename-between-documents"])
def test_a_change_of_documentation_paths_only_is_docs_only(tmp_path, event, change, count):
    assert decide(tmp_path, change, event) == (True, f"only documentation changed ({count} file(s))")


# spec: ANYM-323
@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize("change, reason", [
    (edit("README.md"), "not documentation by the docs-only rule: README.md"),
    (edit("docs/README.md"), "not documentation by the docs-only rule: docs/README.md"),
    (edit("TELEMETRY.md"), "not documentation by the docs-only rule: TELEMETRY.md"),
    (edit("services/x/GUIDE.md"), "not documentation by the docs-only rule: services/x/GUIDE.md"),
    (edit("docs/guide.md", "scripts/app.py"), "not documentation by the docs-only rule: scripts/app.py"),
    (lambda root: git(root, "mv", "scripts/app.py", "docs/app.md"),
     "not documentation by the docs-only rule: scripts/app.py"),
    (lambda root: (root / "NOTES.md").chmod(0o755), "not a plain document addition, edit or deletion: M 100644 100755"),
    (lambda root: ((root / "NOTES.md").unlink(), (root / "NOTES.md").symlink_to("docs/guide.md")),
     "not a plain document addition, edit or deletion: T"),
    (lambda root: (root / "docs/link.md").symlink_to("guide.md"), "not a plain document addition, edit or deletion: A"),
    (lambda root: None, "the change is empty"),
], ids=["readme", "nested-readme", "read-by-a-test", "module-tree", "mixed", "rename-out-of-a-code-path",
        "mode-change", "type-change", "new-symlink", "empty"])
def test_a_denied_path_or_anything_but_a_plain_document_change_verifies_in_full(tmp_path, event, change, reason):
    docs_only_run, why = decide(tmp_path, change, event)
    assert docs_only_run is False and why.startswith(reason), why


# spec: ANYM-323
@pytest.mark.parametrize("event, body", [
    ("push", ""), ("workflow_dispatch", ""), ("", ""), ("pull_request", INTENT),
    ("pull_request", "Summary\n\n  tiden intent branch: [intent/x](link)\n"),
], ids=["push", "dispatch", "no-event", "intent-linked", "intent-line-any-case"])
def test_push_dispatch_and_intent_linked_pull_requests_never_take_the_fast_path(tmp_path, event, body):
    docs_only_run, why = decide(tmp_path, edit("docs/guide.md"), event=event, body=body)
    assert docs_only_run is False and ("always verify in full" in why or "Tiden intent branch" in why), why


def test_an_uncertain_merge_commit_verifies_in_full(tmp_path):
    root = repository(tmp_path)
    head = pull_request(root, edit("docs/guide.md"))
    assert docs_only.decide(root, "pull_request", "b" * 40, "") == (
        False, "the pull request merge commit could not be established")
    git(root, "reset", "-q", "--hard", head)  # a single-parent checkout, not GitHub's merge commit
    assert docs_only.decide(root, "pull_request", head, "")[1] == "the pull request merge commit could not be established"


# spec: ANYM-323
def test_a_merge_queue_group_commit_is_classified_only_against_its_named_base(tmp_path):
    root = repository(tmp_path)
    first, base = group(root, edit("docs/guide.md"))
    second, _ = group(root, edit("NOTES.md"))
    # The second group's own change is documentation, but its first parent is not the base the event names.
    assert docs_only.decide(root, "merge_group", second, "", base) == (
        False, "the merge queue group commit and its base could not be established")
    assert docs_only.decide(root, "merge_group", first, "", base)[1] == (
        "the merge queue group commit and its base could not be established")
    assert docs_only.decide(root, "merge_group", second, "", "")[1] == "the event names no exact commit to classify"
    assert docs_only.decide(root, "merge_group", second, "", first) == (True, "only documentation changed (1 file(s))")


def run_main(root, monkeypatch, tmp_path, event="pull_request", head="", body="", base="", payload=True):
    event_path, output = tmp_path / "event.json", tmp_path / "output"
    if payload:
        event_path.write_text(json.dumps({"pull_request": {"body": body}}))
    output.write_text("")
    for name, value in {"EVENT_NAME": event, "HEAD_SHA": head, "BASE_SHA": base, "GITHUB_EVENT_PATH": str(event_path),
                        "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary")}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(root)
    assert docs_only.main() == 0
    return output.read_text().splitlines()


def test_the_decision_step_reports_the_classified_merge_commit_and_never_fails(tmp_path, monkeypatch):
    root = repository(tmp_path)
    head = pull_request(root, edit("docs/guide.md"))
    merge = git(root, "rev-parse", "HEAD")
    assert run_main(root, monkeypatch, tmp_path, head=head) == ["docs_only=true", f"docs_sha={merge}"]
    assert run_main(root, monkeypatch, tmp_path, head=head, body=INTENT) == ["docs_only=false", "docs_sha="]
    (tmp_path / "event.json").unlink()  # an error inside the decision means full verification
    assert run_main(root, monkeypatch, tmp_path, head=head, payload=False) == ["docs_only=false", "docs_sha="]
    assert "decision failed" in (tmp_path / "summary").read_text()


# spec: ANYM-323
def test_the_decision_step_reports_the_classified_group_commit(tmp_path, monkeypatch):
    root = repository(tmp_path)
    head, base = group(root, edit("docs/guide.md"))
    assert run_main(root, monkeypatch, tmp_path, event="merge_group", head=head, base=base) == [
        "docs_only=true", f"docs_sha={head}"]
    assert run_main(root, monkeypatch, tmp_path, event="merge_group", head=head, base=head) == [
        "docs_only=false", "docs_sha="]


def workflows():
    return (yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text()),
            yaml.safe_load((ROOT / ".github/workflows/checks.yml").read_text()))


# spec: ANYM-323
def test_pull_request_and_merge_queue_runs_decide_and_a_failed_decision_verifies_in_full():
    _, checks = workflows()
    job = checks["jobs"]["documentation"]
    assert job["if"] == ("${{ (github.event_name == 'pull_request' || github.event_name == 'merge_group') "
                         "&& inputs.source_ref == '' && !inputs.reuse_queue_evidence && !inputs.reuse_pr_verification }}")
    assert "permissions" not in job and "secrets." not in json.dumps(job)
    checkout = job["steps"][0]
    assert checkout["with"] == {"persist-credentials": False, "fetch-depth": 2}
    decide = next(step for step in job["steps"] if step.get("id") == "decide")
    assert decide["continue-on-error"] is True and decide["run"] == "python3 scripts/docs_only.py"
    assert decide["env"] == {"EVENT_NAME": "${{ github.event_name }}",
                             "HEAD_SHA": "${{ github.event.pull_request.head.sha || github.event.merge_group.head_sha }}",
                             "BASE_SHA": "${{ github.event.merge_group.base_sha }}"}
    assert job["outputs"]["docs_only"] == "${{ steps.decide.outcome == 'success' && steps.decide.outputs.docs_only || 'false' }}"
    outputs = checks.get("on", checks.get(True))["workflow_call"]["outputs"]
    assert outputs["docs_only"]["value"] == "${{ jobs.documentation.outputs.docs_only }}"
    assert outputs["docs_sha"]["value"] == "${{ jobs.documentation.outputs.docs_sha }}"
    skip = "needs.documentation.outputs.docs_only != 'true'"
    for name in ("verify", "load", "python-2", "python-3", "python-4", "evidence"):
        assert skip in checks["jobs"][name]["if"] and "documentation" in checks["jobs"][name]["needs"]
    assert not (ROOT / ".github/docs-only-paths.txt").exists()


def test_a_docs_only_run_records_a_skipped_route_and_stays_green(tmp_path):
    _, checks = workflows()
    steps = {step.get("name") or step["uses"].split("@")[0]: step for step in checks["jobs"]["tiden"]["steps"]}
    config = steps["Check optional Tiden configuration"]
    assert config["env"]["DOCS_ONLY"] == "${{ needs.documentation.outputs.docs_only }}"

    def run(script, **env):
        output = tmp_path / "output"
        output.write_text("")
        completed = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True, env={
            "PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            "RUNNER_TEMP": str(tmp_path), **env})
        return completed, output.read_text().split()
    configured = {"TIDEN_API_TOKEN": "synthetic-token", "TIDEN_PRODUCT_ID": "p", "TIDEN_WORKSPACE_ID": "w"}
    assert run(config["run"], EVENT_NAME="pull_request", SOURCE_BRANCH="docs/edit", DOCS_ONLY="true",
               **configured)[1] == ["enabled=false", "docs_only=true"]
    # A docs-only queue run assembled no evidence, so it records the skipped route instead of the queue receipt.
    assert run(config["run"], EVENT_NAME="merge_group", SOURCE_BRANCH="gh-readonly-queue/main/x", DOCS_ONLY="true")[1] == [
        "enabled=false", "docs_only=true"]
    assert run(config["run"], EVENT_NAME="merge_group", SOURCE_BRANCH="gh-readonly-queue/main/x", DOCS_ONLY="false")[1] == [
        "enabled=false", "queue=true"]
    assert run(config["run"], EVENT_NAME="pull_request", SOURCE_BRANCH="docs/edit", DOCS_ONLY="false",
               **configured)[1] == ["enabled=true"]
    record = steps["Record that a docs-only run publishes nothing"]
    assert record["if"] == "steps.config.outputs.docs_only == 'true'"
    completed, _ = run(record["run"], REASON=record["env"]["REASON"])
    assert completed.returncode == 0 and "Tiden evidence not published" in completed.stdout
    assert json.loads((tmp_path / "ci-evidence/tiden-route.json").read_text()) == {
        "branch": None, "source": "skipped", "reason": record["env"]["REASON"]}
    assert "steps.config.outputs.docs_only == 'true'" in steps["Retain Tiden association and verdict receipt"]["if"]
    for name in ("actions/checkout", "actions/download-artifact", "Install pinned Tiden CLI with integrity check",
                 "Publish and verify branch association"):
        assert "docs_only" not in steps[name]["if"]


SHA = "a" * 40
DOCS = {"DECISION": "", "REUSE_RESULT": "skipped", "REUSED_SHA": "", "VERIFIED_SHA": "", "EVENT_NAME": "pull_request",
        "REF": "refs/pull/7/merge", "EXPECTED_SHA": SHA, "DOCS_ONLY": "true", "DOCS_SHA": SHA, "PR_DECISION": "",
        "PR_REUSE_RESULT": "skipped", "PR_REUSED_SHA": ""}
QUEUE = {**DOCS, "EVENT_NAME": "merge_group", "REF": "refs/heads/gh-readonly-queue/main/pr-7-" + "b" * 40,
         "PR_DECISION": "full", "PR_REUSE_RESULT": "success"}


# spec: ANYM-323
@pytest.mark.parametrize("environment, passes", [
    (DOCS, True),
    (QUEUE, True),
    ({**DOCS, "DOCS_SHA": "b" * 40}, False),
    ({**QUEUE, "DOCS_SHA": "b" * 40}, False),
    ({**DOCS, "DOCS_SHA": ""}, False),
    ({**DOCS, "EVENT_NAME": "push", "REF": "refs/heads/main"}, False),
    ({**DOCS, "EVENT_NAME": "workflow_dispatch", "REF": "refs/heads/main"}, False),
    ({**DOCS, "VERIFIED_SHA": SHA}, False),
    ({**QUEUE, "VERIFIED_SHA": SHA}, False),
    ({**DOCS, "DOCS_ONLY": "false"}, False),
    ({**QUEUE, "DOCS_ONLY": "false"}, False),
], ids=["pull-request", "merge-queue", "other-commit", "queue-other-commit", "no-commit", "main-push", "dispatch",
        "verify-also-ran", "queue-verify-also-ran", "not-docs-only", "queue-not-docs-only"])
def test_ci_gate_passes_a_skipped_verify_only_for_the_docs_only_decision_of_this_pull_request_or_group_commit(
        environment, passes):
    ci, _ = workflows()
    step = ci["jobs"]["gate"]["steps"][1]
    assert set(step["env"]) == set(DOCS)
    assert step["env"]["DOCS_ONLY"] == "${{ needs.checks.outputs.docs_only }}"
    assert step["env"]["DOCS_SHA"] == "${{ needs.checks.outputs.docs_sha }}"
    completed = subprocess.run(["bash", "-e", "-c", step["run"]], env={"PATH": "/usr/bin:/bin", **environment},
                               capture_output=True)
    assert (completed.returncode == 0) == passes


def test_native_and_image_rules_are_untouched_by_the_docs_only_decision():
    ci, _ = workflows()
    assert "if" not in ci["jobs"]["images"] and "needs" not in ci["jobs"]["images"]
    # native skips only on the queue's pull request reuse (tests/test_ci_reuse.py), never on the docs-only decision.
    assert ci["jobs"]["native"]["needs"] == ["pr_reuse"] and "docs_only" not in json.dumps(ci["jobs"]["native"])
    assert ci["jobs"]["gate"]["needs"] == ["reuse", "pr_reuse", "checks", "native", "images"]
    images = yaml.safe_load((ROOT / ".github/workflows/images.yml").read_text())
    assert "docs_only" not in json.dumps(images) and "docs_only" not in json.dumps(ci["jobs"]["publish"])


# spec: ANYM-323
def test_the_shared_tiden_cli_pin_is_not_documentation():
    # CI installs the Tiden CLI from this file's version and checksum, so a change to it verifies in full.
    assert docs_only.eligible("docs/harness/tiden.md") is False
    assert docs_only.eligible("docs/harness/process.md") is True
