"""A pull request run of reviewed documentation only skips the test jobs (ANYM-323); synthetic git repositories."""
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from scripts import docs_only

ROOT = Path(__file__).resolve().parents[1]
LISTED = ("docs/guide.md", "NOTES.md", "docs/заметки.md", "README.md", "docs/read.md", "docs/gone.md")
INTENT = "Tiden intent branch: [intent/2026-10-17-x](https://app.tiden.ai/a/products/b/branches/c)"


def git(root, *arguments):
    environment = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": str(root), "PATH": os.environ["PATH"],
                   "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True,
                          env=environment).stdout.strip()


def repository(tmp_path, exclusions="", code=None):
    """A base commit with listed documents and code; returns (root, base branch)."""
    root = tmp_path / "repo"
    (root / ".github").mkdir(parents=True)
    (root / ".github/docs-only-paths.txt").write_text(
        "# reviewed\n" + "\n".join(LISTED) + "\n\n## scan exclusions\n" + exclusions)
    for path in (*LISTED, "UNLISTED.md", "scripts/app.py", "tests/test_app.py"):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(f"{path}\n")
    (root / "tests/test_app.py").write_text('def test_reads():\n    assert open("docs/read.md").read()\n')
    for path, text in (code or {}).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
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


def edit(*paths):
    return lambda root: [(root / path).write_text("edited\n") for path in paths]


def decide(tmp_path, change, event="pull_request", body="", exclusions="", code=None):
    root = repository(tmp_path, exclusions, code)
    head = pull_request(root, change)
    return docs_only.decide(root, event, head, body)


def test_a_change_of_listed_unread_documents_is_docs_only(tmp_path):
    assert decide(tmp_path, edit("docs/guide.md", "NOTES.md", "docs/заметки.md")) == (
        True, "only reviewed documentation changed (3 file(s))")


def test_deleting_a_listed_unread_document_is_docs_only(tmp_path):
    assert decide(tmp_path, lambda root: (root / "docs/gone.md").unlink())[0] is True


@pytest.mark.parametrize("change, reason", [
    (edit("UNLISTED.md"), "not on the reviewed docs-only list: UNLISTED.md"),
    (edit("docs/guide.md", "scripts/app.py"), "not on the reviewed docs-only list: scripts/app.py"),
    (edit("README.md"), "not on the reviewed docs-only list: README.md"),
    (edit("docs/read.md"), "scanned code may read a changed document: tests/test_app.py:2"),
    (lambda root: (root / "docs/read.md").unlink(), "scanned code may read a changed document: tests/test_app.py:2"),
    (lambda root: (root / "docs/new.md").write_text("new\n"), "not a plain document edit or deletion: A"),
    (lambda root: git(root, "mv", "scripts/app.py", "docs/guide.md") if (root / "docs/guide.md").unlink() is None
     else None, "not on the reviewed docs-only list: scripts/app.py"),
    (lambda root: (root / "NOTES.md").chmod(0o755), "not a plain document edit or deletion: M 100644 100755"),
    (lambda root: ((root / "NOTES.md").unlink(), (root / "NOTES.md").symlink_to("docs/guide.md")),
     "not a plain document edit or deletion: T"),
    (lambda root: None, "the pull request changes nothing"),
], ids=["unlisted", "mixed", "readme-even-if-listed", "read-by-a-test", "deleting-a-read-document", "new-file",
        "rename-out-of-a-code-path", "mode-change", "symlink", "empty"])
def test_anything_but_listed_unread_documents_verifies_in_full(tmp_path, change, reason):
    docs_only_run, why = decide(tmp_path, change)
    assert docs_only_run is False and why.startswith(reason), why


@pytest.mark.parametrize("code, hit", [
    ({"scripts/glob.py": 'import pathlib\nfiles = list(pathlib.Path(".").rglob("*.md"))\n'}, "scripts/glob.py:2"),
    ({"tests/test_dir.py": 'import os\nnames = os.listdir("docs")\n'}, "tests/test_dir.py:2"),
    ({"tests/test_join.py": 'ROOT = None\nGUIDE = ROOT / "docs" / "guide.md"\n'}, "tests/test_join.py:2"),
    ({"services/x/pyproject.toml": '[tool.hatch.build]\ninclude = ["docs"]\n'}, "services/x/pyproject.toml:2"),
    ({"services/x/Dockerfile": "FROM scratch\nCOPY docs/guide.md /app/\n"}, "services/x/Dockerfile:2"),
    ({".github/workflows/x.yml": "jobs:\n  x:\n    run: cat NOTES.md\n"}, ".github/workflows/x.yml:3"),
    ({"packages/x/package.json": '{"files": ["docs/guide.md"]}\n'}, "packages/x/package.json:1"),
    ({"services/x/tests/test_doc.py": 'TEXT = open("docs/guide.md").read()\n'}, "services/x/tests/test_doc.py:1"),
    ({"whatsapp/tests/doc.test.mjs": 'import fs from "node:fs";\nfs.readFileSync("NOTES.md");\n'},
     "whatsapp/tests/doc.test.mjs:2"),
    ({"scripts/hash.py": 'prefix = " # "; text = open("docs/guide.md").read()\n'}, "scripts/hash.py:1"),
    ({"scripts/slash.mjs": 'const prefix = " // "; const text = read("NOTES.md");\n'}, "scripts/slash.mjs:1"),
    ({"scripts/split.py": 'import pathlib\nfiles = pathlib.Path(".").rglob(\n    "*.md"\n)\n'}, "scripts/split.py:2"),
    ({"packages/y/package.json": '{\n  "files": [\n    "docs"\n  ]\n}\n'}, "packages/y/package.json:2"),
    ({"services/y/Dockerfile": "FROM scratch\nCOPY docs/ /app/docs/\n"}, "services/y/Dockerfile:2"),
    ({"deploy/config.json": '{"guide": "docs/guide.md"}\n'}, "deploy/config.json:1"),
    ({"scripts/broken.py": 'TEXT = open("docs/guide.md"  # unterminated\n'}, "scripts/broken.py:1"),
], ids=["glob", "docs-directory", "composed-path", "packaging-include", "dockerfile", "workflow", "package-files",
        "module-test", "module-node-test", "hash-in-a-string", "slashes-in-a-string", "split-glob-call",
        "split-json-files-list", "docker-directory-copy", "other-file-type", "unparsable-python-keeps-comments"])
def test_a_consumer_found_by_the_scan_means_full_verification(tmp_path, code, hit):
    docs_only_run, why = decide(tmp_path, edit("docs/guide.md", "NOTES.md"), code=code)
    assert docs_only_run is False and why.startswith("scanned code may read a changed document: " + hit), why


def test_matches_in_comments_and_reviewed_exclusions_do_not_count(tmp_path):
    # Python comments are told apart from strings by the tokenizer; an exclusion skips only the directory check.
    code = {"scripts/commented.py": "# docs/guide.md is described in a comment only\nvalue = 1  # see NOTES.md\n",
            "tests/fixture.py": 'import pathlib\nroot = pathlib.Path("synthetic")\n\n\n\n\nfiles = list((root / "docs").iterdir())\n'}
    exclusions = "tests/fixture.py | builds a synthetic repository under tmp_path\n"
    assert decide(tmp_path, edit("docs/guide.md", "NOTES.md"), code=code, exclusions=exclusions)[0] is True
    assert decide(tmp_path / "unexcluded", edit("docs/guide.md"), code=code)[1] == (
        "scanned code may read a changed document: tests/fixture.py:7")
    # An excluded file is still refused when it names a changed document.
    named = {**code, "tests/fixture.py": code["tests/fixture.py"] + 'GUIDE = "docs/guide.md"\n'}
    assert decide(tmp_path / "named", edit("docs/guide.md"), code=named, exclusions=exclusions)[1] == (
        "scanned code may read a changed document: tests/fixture.py:8")


@pytest.mark.parametrize("code, hit", [
    ({"scripts/report.mjs": 'const report = `\n// ${fs.readFileSync("docs/guide.md", "utf8")}\n`;\n'}, "scripts/report.mjs:2"),
    ({".github/workflows/x.yml": "jobs:\n  x:\n    run: |\n      # cat NOTES.md\n"}, ".github/workflows/x.yml:4"),
    ({"packages/z/package.json": '{\n  "files": [\n    "src",\n    "lib",\n    "assets",\n    "types",\n    "docs"\n  ]\n}\n'},
     "packages/z/package.json:2"),
    ({"scripts/large.py": "PAD = 1\n" * (1024 * 1024 + 1) + 'TEXT = open("docs/guide.md").read()\n'},
     "scripts/large.py:1048578"),
], ids=["template-string", "shell-comment-in-yaml", "long-json-files-list", "past-8-mib"])
def test_comment_like_lines_outside_python_and_far_apart_or_late_matches_still_count(tmp_path, code, hit):
    docs_only_run, why = decide(tmp_path, edit("docs/guide.md", "NOTES.md"), code=code)
    assert docs_only_run is False and why.startswith("scanned code may read a changed document: " + hit), why


def test_a_scan_exclusion_without_a_reason_is_refused(tmp_path):
    with pytest.raises(ValueError, match="without a reason"):
        decide(tmp_path, edit("docs/guide.md"), exclusions="tests/fixture.py\n")


@pytest.mark.parametrize("event, body", [
    ("merge_group", ""), ("push", ""), ("workflow_dispatch", ""), ("pull_request", INTENT),
    ("pull_request", "Summary\n\n  tiden intent branch: [intent/x](link)\n"),
], ids=["merge-queue", "push", "dispatch", "intent-linked", "intent-line-any-case"])
def test_other_events_and_intent_linked_pull_requests_never_take_the_fast_path(tmp_path, event, body):
    docs_only_run, why = decide(tmp_path, edit("docs/guide.md"), event=event, body=body)
    assert docs_only_run is False and ("always verify in full" in why or "Tiden intent branch" in why), why


def test_an_uncertain_merge_commit_verifies_in_full(tmp_path):
    root = repository(tmp_path)
    head = pull_request(root, edit("docs/guide.md"))
    assert docs_only.decide(root, "pull_request", "b" * 40, "") == (
        False, "the pull request merge commit could not be established")
    git(root, "reset", "-q", "--hard", head)  # a single-parent checkout, not GitHub's merge commit
    assert docs_only.decide(root, "pull_request", head, "")[1] == "the pull request merge commit could not be established"


def run_main(root, monkeypatch, tmp_path, event="pull_request", head="", body=""):
    payload, output = tmp_path / "event.json", tmp_path / "output"
    payload.write_text(json.dumps({"pull_request": {"body": body}}))
    output.write_text("")
    for name, value in {"EVENT_NAME": event, "HEAD_SHA": head, "GITHUB_EVENT_PATH": str(payload),
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
    (root / ".github/docs-only-paths.txt").unlink()  # an error inside the decision means full verification
    assert run_main(root, monkeypatch, tmp_path, head=head) == ["docs_only=false", "docs_sha="]
    assert "decision failed" in (tmp_path / "summary").read_text()


def test_the_reviewed_list_classifies_its_documents_as_docs_only_on_the_real_repository():
    documents, exclusions = docs_only.read_list(ROOT)
    tracked = set(docs_only.git(ROOT, "ls-files", "-z").split("\0"))
    assert documents and all(docs_only.listable(path) and path in tracked for path in documents)
    assert not documents & {"README.md", "SETUP.md", "THIRD_PARTY_NOTICES.md", "TIDEN-CI.md",
                            "docs/operations/production.md"}
    assert all(path in tracked and reason for path, reason in exclusions.items())
    # The guard runs over the repository's real scan inputs and finds no consumer of any listed document,
    # so it cannot silently turn the fast path off; a consumer added later makes such a run verify in full.
    # One scan for all of them: a hit names the file and line that reads one.
    assert docs_only.consumers(ROOT, sorted(documents), exclusions) == []
    scanned = docs_only.scan_inputs(ROOT)
    assert {".github/workflows/checks.yml", "scripts/production.py", "tests/test_docs_only.py",
            "services/messaging/hatch_build.py", "services/messaging/Dockerfile"} <= set(scanned)


def workflows():
    return (yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text()),
            yaml.safe_load((ROOT / ".github/workflows/checks.yml").read_text()))


def test_only_a_pull_request_run_decides_and_a_failed_decision_verifies_in_full():
    _, checks = workflows()
    job = checks["jobs"]["documentation"]
    assert job["if"] == ("${{ github.event_name == 'pull_request' && inputs.source_ref == '' "
                         "&& !inputs.reuse_queue_evidence }}")
    assert "permissions" not in job and "secrets." not in json.dumps(job)
    decide = next(step for step in job["steps"] if step.get("id") == "decide")
    assert decide["continue-on-error"] is True and decide["run"] == "python3 scripts/docs_only.py"
    assert decide["env"] == {"EVENT_NAME": "${{ github.event_name }}", "HEAD_SHA": "${{ github.event.pull_request.head.sha }}"}
    assert job["outputs"]["docs_only"] == "${{ steps.decide.outcome == 'success' && steps.decide.outputs.docs_only || 'false' }}"
    outputs = checks.get("on", checks.get(True))["workflow_call"]["outputs"]
    assert outputs["docs_only"]["value"] == "${{ jobs.documentation.outputs.docs_only }}"
    assert outputs["docs_sha"]["value"] == "${{ jobs.documentation.outputs.docs_sha }}"
    skip = "needs.documentation.outputs.docs_only != 'true'"
    for name in ("verify", "load", "evidence"):
        assert skip in checks["jobs"][name]["if"] and "documentation" in checks["jobs"][name]["needs"]


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
    assert run(config["run"], EVENT_NAME="merge_group", SOURCE_BRANCH="gh-readonly-queue/main/x", DOCS_ONLY="true")[1] == [
        "enabled=false", "queue=true"]
    assert run(config["run"], EVENT_NAME="pull_request", SOURCE_BRANCH="docs/edit", DOCS_ONLY="false",
               **configured)[1] == ["enabled=true"]
    record = steps["Record that a docs-only pull request run publishes nothing"]
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
        "REF": "refs/pull/7/merge", "EXPECTED_SHA": SHA, "DOCS_ONLY": "true", "DOCS_SHA": SHA}


@pytest.mark.parametrize("environment, passes", [
    (DOCS, True),
    ({**DOCS, "DOCS_SHA": "b" * 40}, False),
    ({**DOCS, "DOCS_SHA": ""}, False),
    ({**DOCS, "EVENT_NAME": "merge_group"}, False),
    ({**DOCS, "EVENT_NAME": "push", "REF": "refs/heads/main"}, False),
    ({**DOCS, "VERIFIED_SHA": SHA}, False),
    ({**DOCS, "DOCS_ONLY": "false"}, False),
], ids=["docs-only", "other-commit", "no-commit", "merge-queue", "main-push", "verify-also-ran", "not-docs-only"])
def test_ci_gate_passes_a_skipped_verify_on_a_pull_request_only_for_its_docs_only_decision(environment, passes):
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
    for name in ("native", "images"):
        assert "if" not in ci["jobs"][name] and "needs" not in ci["jobs"][name]
    assert ci["jobs"]["gate"]["needs"] == ["reuse", "checks", "native", "images"]
    images = yaml.safe_load((ROOT / ".github/workflows/images.yml").read_text())
    assert "docs_only" not in json.dumps(images) and "docs_only" not in json.dumps(ci["jobs"]["publish"])


def test_a_scanned_file_that_cannot_be_read_means_full_verification(tmp_path):
    def dangling(root):
        (root / "scripts/missing.py").symlink_to("nowhere.py")
    root = repository(tmp_path)
    dangling(root)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "a tracked link to nothing")
    head = pull_request(root, edit("docs/guide.md"))
    assert docs_only.decide(root, "pull_request", head, "") == (
        False, "scanned code may read a changed document: scripts/missing.py: not scannable")
