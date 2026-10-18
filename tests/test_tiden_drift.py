"""The drift watermark job against temporary Git repositories and a fake Tiden CLI; it never calls Tiden."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest
import yaml

import scripts.tiden_drift as drift
from scripts.tiden_drift import EXCLUDED, Refused, check, excluded

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/tiden-drift.yml"
SECRET = "tfy_sentinel_secret_value"
# A stateful stand-in for the Tiden API: the watermark, the anchors and every advance call live in state.json, so a
# rerun sees what an earlier run left on the "server".
FAKE = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys, time
    path = os.environ["FAKE_TIDEN_STATE"]
    state = json.load(open(path))
    args = sys.argv[1:]
    def save():
        json.dump(state, open(path, "w"))
    def fault(stage):
        mode = state.get("fault", {{}}).get(stage)
        if mode == "exit":
            sys.stderr.write("request body {SECRET}\\n"); print("{SECRET}"); sys.exit(3)
        if mode == "garbage":
            print("not json {SECRET}"); sys.exit(0)
        if mode == "sleep":
            time.sleep(5)
    if args[:2] == ["drift", "status"]:
        state["status_calls"] = state.get("status_calls", 0) + 1; save()
        fault("status" if state.get("advances") == [] or "status_after" not in state.get("fault", {{}})
              else "status_after")
        print(json.dumps({{"repository": state.get("repository", "github.com/GoSync-Inc/deedkit"),
                          "watermarkSha": state["watermark"], "state": "drift"}}))
    elif args[:2] == ["spec", "list"]:
        fault("list")
        print(json.dumps(state.get("listing") or {{
            "pagination": {{"nextPageToken": "", "totalCount": 1}},
            "requirements": [{{"id": "spec-1", "sourceCount": len(state["anchors"]), "sources": [
                {{"sourceType": "repo_file", "repoPath": p, "repository": state.get("anchor_repository",
                                                                              "github.com/GoSync-Inc/deedkit")}}
                for p in state["anchors"]]}}]}}))
    elif args[:2] == ["drift", "advance"]:
        options = dict(zip(args[2::2], args[3::2]))
        state.setdefault("advances", []).append(options)
        # As CLI 0.33.0 does: a stale --from is refused with exit 0 and "advanced": false.
        advanced = options["--from"] == state["watermark"]
        if advanced:
            state["watermark"] = state.get("lands_on") or options["--to"]  # lands_on: another writer moved it on
        save()
        fault("advance")
        print(json.dumps({{"advanced": advanced, "watermark": {{"lastReconciledSha": state["watermark"]}}}}))
    else:
        sys.exit(64)
""")


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(root, files, message):
    for name, text in files.items():
        path = root / name
        if text is None:
            git(root, "rm", "-q", "--", name)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(root, "add", "--", name)
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
        git(work, "config", key, value)
    git(work, "remote", "add", "origin", str(origin))
    return work


@pytest.fixture
def server(tmp_path, monkeypatch):
    cli, path = tmp_path / "tiden", tmp_path / "state.json"
    cli.write_text(FAKE)
    cli.chmod(0o755)
    monkeypatch.setenv("FAKE_TIDEN_STATE", str(path))
    monkeypatch.setenv("TIDEN_API_TOKEN", SECRET)

    class Server:
        def __init__(self):
            self.cli = str(cli)

        def set(self, **values):
            state = self.state() if path.exists() else {"advances": []}
            state.update(values)
            path.write_text(json.dumps(state))

        def state(self):
            return json.loads(path.read_text())

    return Server()


def run(repo, server):
    lines = []
    code = check(repo, server.cli, "product", lines.append)
    return code, "\n".join(lines)


def history(repo):
    a = commit(repo, {"services/messaging/app.py": "a"}, "A")
    b = commit(repo, {"services/messaging/app.py": "b", "docs/guide.md": "doc"}, "B")
    c = commit(repo, {"services/messaging/new.py": "c", "tests/test_new.py": "t"}, "C")
    git(repo, "push", "-q", "origin", "main")
    return a, b, c


def test_all_anchored_advances_once_to_the_head_of_main(repo, server):
    a, b, c = history(repo)
    server.set(watermark=a, anchors=["services/messaging/app.py", "services/messaging/new.py"])
    assert run(repo, server)[0] == 0
    state = server.state()
    assert state["watermark"] == c
    assert state["advances"] == [{"--from": a, "--to": c, "--reason": "empty_sync",
                                  "--repo": "github.com/GoSync-Inc/deedkit", "--product-id": "product",
                                  "--format": "json"}]


def test_a_run_from_an_old_event_checks_the_newest_main(repo, server, monkeypatch):
    # Active B, pending C replaced by a rerun of A: the surviving run carries A's event SHA, but the checker never
    # reads it; it checks the checkout of main as it is when the run starts, so C is covered.
    a, b, c = history(repo)
    monkeypatch.setenv("GITHUB_SHA", a)
    server.set(watermark=b, anchors=["services/messaging/new.py"])
    assert run(repo, server)[0] == 0
    assert server.state()["advances"][0]["--to"] == c and server.state()["watermark"] == c


def test_unanchored_code_fails_names_every_file_and_sends_no_advance(repo, server):
    a, b, c = history(repo)
    server.set(watermark=a, anchors=[])
    code, output = run(repo, server)
    assert code == 1
    assert "services/messaging/app.py" in output and "services/messaging/new.py" in output
    assert "docs/guide.md" not in output and "tests/test_new.py" not in output
    assert server.state()["advances"] == [] and server.state()["watermark"] == a


def test_rerun_after_anchors_were_added_advances(repo, server):
    a, b, c = history(repo)
    server.set(watermark=a, anchors=["services/messaging/app.py"])
    assert run(repo, server)[0] == 1
    server.set(anchors=["services/messaging/app.py", "services/messaging/new.py"])
    assert run(repo, server)[0] == 0 and server.state()["watermark"] == c


def test_watermark_at_main_is_a_no_op(repo, server):
    *_, c = history(repo)
    server.set(watermark=c, anchors=[])
    assert run(repo, server)[0] == 0 and server.state()["advances"] == []


@pytest.mark.parametrize("repository", ["github.com/GoSync-Inc/anymsg", None])
def test_anchors_of_the_former_repository_name_or_none_count(repo, server, repository):
    a, b, c = history(repo)
    server.set(watermark=a, anchors=["services/messaging/app.py", "services/messaging/new.py"],
               anchor_repository=repository)
    assert run(repo, server)[0] == 0


def test_an_anchor_in_another_repository_does_not_count(repo, server):
    a, b, c = history(repo)
    server.set(watermark=a, anchors=["services/messaging/app.py", "services/messaging/new.py"],
               anchor_repository="github.com/example/other")
    assert run(repo, server)[0] == 1 and server.state()["advances"] == []


def test_paths_stay_literal_renames_need_their_destination_and_deletions_need_nothing(repo, server):
    a = commit(repo, {"scripts/old.py": "x", "scripts/gone.py": "y"}, "A")
    git(repo, "mv", "scripts/old.py", "scripts/moved.py")
    odd = ["scripts/é.py", "scripts/with space.py", "scripts/new\nline.py"]
    c = commit(repo, {"scripts/gone.py": None, **{name: "z" for name in odd}}, "B")
    git(repo, "push", "-q", "origin", "main")
    server.set(watermark=a, anchors=odd)
    code, output = run(repo, server)
    assert code == 1 and "scripts/moved.py" in output and "gone.py" not in output
    server.set(anchors=[*odd, "scripts/moved.py"])
    assert run(repo, server)[0] == 0 and server.state()["watermark"] == c


@pytest.mark.parametrize("name", ["scripts/carriage\rreturn.py", "scripts/crlf\r\nname.py"])
def test_a_carriage_return_name_is_not_aliased_to_a_newline_name(repo, server, name):
    a = commit(repo, {"base.py": "x"}, "A")
    c = commit(repo, {name: "z"}, "B")
    git(repo, "push", "-q", "origin", "main")
    server.set(watermark=a, anchors=[name.replace("\r\n", "\n").replace("\r", "\n")])
    assert run(repo, server)[0] == 1 and server.state()["advances"] == []
    server.set(anchors=[name])
    assert run(repo, server)[0] == 0 and server.state()["watermark"] == c


def test_a_path_that_is_not_utf8_fails_closed(repo, server):
    a = commit(repo, {"base.py": "x"}, "A")
    try:
        (repo / os.fsdecode(b"bad\xff.py")).write_text("z")
    except OSError:
        pytest.skip("this filesystem refuses non-UTF-8 names")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "B")
    git(repo, "push", "-q", "origin", "main")
    server.set(watermark=a, anchors=[])
    with pytest.raises(Refused, match="UTF-8"):
        run(repo, server)
    assert server.state()["advances"] == []


def test_a_read_back_on_a_commit_off_main_fails(repo, server):
    a, b, c = history(repo)
    git(repo, "checkout", "-q", "-b", "side")
    off = commit(repo, {"services/messaging/side.py": "s"}, "off main")
    git(repo, "checkout", "-q", "main")
    server.set(watermark=a, anchors=["services/messaging/app.py", "services/messaging/new.py"], lands_on=off)
    with pytest.raises(Refused, match="later merge on main"):
        run(repo, server)
    assert len(server.state()["advances"]) == 1


def test_a_read_back_on_a_later_merge_this_checkout_lacks_passes(repo, server, tmp_path):
    a, b, c = history(repo)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)], check=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
        git(other, "config", key, value)
    d = commit(other, {"services/messaging/later.py": "d"}, "D")
    git(other, "push", "-q", "origin", "main")
    server.set(watermark=a, anchors=["services/messaging/app.py", "services/messaging/new.py"], lands_on=d)
    assert run(repo, server)[0] == 0 and len(server.state()["advances"]) == 1


def test_unanchored_odd_names_are_escaped_in_the_report(repo, server):
    a = commit(repo, {"base.py": "x"}, "A")
    commit(repo, {"scripts/new\nforged line.py": "z"}, "B")
    git(repo, "push", "-q", "origin", "main")
    server.set(watermark=a, anchors=[])
    code, output = run(repo, server)
    assert code == 1 and "- scripts/new\\nforged line.py" in output
    assert not any(line.startswith("forged line") for line in output.splitlines())


@pytest.mark.parametrize("stage", ["advance", "status_after"])
@pytest.mark.parametrize("mode", ["exit", "garbage", "sleep"])
def test_an_advance_with_an_unknown_result_fails_and_a_rerun_sends_no_second_advance(repo, server, monkeypatch,
                                                                                   stage, mode):
    monkeypatch.setattr(drift, "TIMEOUT", 2)
    a, b, c = history(repo)
    server.set(watermark=a, anchors=["services/messaging/app.py", "services/messaging/new.py"],
               fault={stage: mode})
    with pytest.raises(Refused, match="unknown"):
        run(repo, server)
    assert len(server.state()["advances"]) == 1 and server.state()["watermark"] == c
    server.set(fault={})
    assert run(repo, server)[0] == 0
    assert len(server.state()["advances"]) == 1  # the rerun read the server and sent nothing


def test_a_rejected_compare_and_set_fails_and_the_rerun_reconciles(repo, server):
    a, b, c = history(repo)
    server.set(watermark=a, anchors=["services/messaging/app.py", "services/messaging/new.py"])
    real = drift.watermark
    calls = []

    def stale(cli, product):
        calls.append(1)
        value = real(cli, product)
        if len(calls) == 1:
            server.set(watermark=b)  # another writer advances between this read and the advance
        return value

    with monkeypatch_context() as patch:
        patch.setattr(drift, "watermark", stale)
        with pytest.raises(Refused, match="did not advance"):
            run(repo, server)
    assert server.state()["watermark"] == b
    assert run(repo, server)[0] == 0 and server.state()["watermark"] == c


def monkeypatch_context():
    return pytest.MonkeyPatch.context()


@pytest.mark.parametrize("stage", ["status", "list"])
@pytest.mark.parametrize("mode", ["exit", "garbage", "sleep"])
def test_failures_before_the_advance_send_nothing(repo, server, monkeypatch, stage, mode):
    monkeypatch.setattr(drift, "TIMEOUT", 2)
    a, b, c = history(repo)
    server.set(watermark=a, anchors=["services/messaging/app.py", "services/messaging/new.py"], fault={stage: mode})
    with pytest.raises(Refused):
        run(repo, server)
    assert server.state()["advances"] == []


@pytest.mark.parametrize("listing", [
    {"pagination": {"nextPageToken": "more", "totalCount": 1}, "requirements": [{"id": "s", "sources": []}]},
    {"pagination": {"nextPageToken": "", "totalCount": 2}, "requirements": [{"id": "s", "sources": []}]},
    {"pagination": {"nextPageToken": "", "totalCount": "1"}, "requirements": [{"id": "s", "sources": []}]},
    {"pagination": {"totalCount": 1}, "requirements": [{"id": "s", "sources": []}]},
    {"requirements": [{"id": "s", "sources": []}]},
    {"pagination": {"nextPageToken": "", "totalCount": 2}, "requirements": [{"id": "s"}, {"id": "s"}]},
    {"pagination": {"nextPageToken": "", "totalCount": 1}, "requirements": [{"sources": []}]},
    {"pagination": {"nextPageToken": "", "totalCount": 1}, "requirements": [{"id": "s", "sources": {}}]},
    {"pagination": {"nextPageToken": "", "totalCount": 1}, "requirements": [{"id": "s", "sources": ["x"]}]},
    {"pagination": {"nextPageToken": "", "totalCount": 1},
     "requirements": [{"id": "s", "sourceCount": 2, "sources": [{"sourceType": "repo_file", "repoPath": "a.py"}]}]},
    {"pagination": {"nextPageToken": "", "totalCount": 1}, "requirements": [{"id": "s", "sourceCount": 1}]},
    {"pagination": {"nextPageToken": "", "totalCount": 1}, "requirements": [{"id": "", "sources": []}]},
    {"pagination": {"nextPageToken": "", "totalCount": 1},
     "requirements": [{"id": "s", "sourceCount": True, "sources": [{"sourceType": "repo_file", "repoPath": "a.py"}]}]},
    {"pagination": {"nextPageToken": "", "totalCount": 1},
     "requirements": [{"id": "s", "sourceCount": 1, "sources": [{"sourceType": "repo_file"}]}]},
])
def test_an_incomplete_or_malformed_listing_fails_closed(repo, server, listing):
    a, *_ = history(repo)
    server.set(watermark=a, anchors=[], listing=listing)
    with pytest.raises(Refused):
        run(repo, server)
    assert server.state()["advances"] == []


@pytest.mark.parametrize("repository", [False, [], {}, 0])
def test_a_malformed_source_repository_fails_closed_instead_of_counting_as_legacy(repo, server, repository):
    a, *_ = history(repo)
    paths = ["services/messaging/app.py", "services/messaging/new.py"]
    server.set(watermark=a, anchors=[], listing={
        "pagination": {"nextPageToken": "", "totalCount": 1},
        "requirements": [{"id": "s", "sourceCount": 2, "sources": [
            {"sourceType": "repo_file", "repoPath": path, "repository": repository} for path in paths]}]})
    with pytest.raises(Refused, match="repository"):
        run(repo, server)
    assert server.state()["advances"] == []


@pytest.mark.parametrize("value", ["", "abc", "A" * 40, "0" * 40])
def test_an_invalid_or_missing_watermark_commit_fails_closed(repo, server, value):
    history(repo)
    server.set(watermark=value, anchors=[])
    with pytest.raises(Refused):
        run(repo, server)
    assert server.state()["advances"] == []


def test_a_status_for_another_repository_fails_closed(repo, server):
    a, *_ = history(repo)
    server.set(watermark=a, anchors=[], repository="github.com/example/other")
    with pytest.raises(Refused):
        run(repo, server)


def test_a_watermark_advanced_past_this_checkout_on_main_is_a_no_op(repo, server, tmp_path):
    a, b, c = history(repo)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)], check=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
        git(other, "config", key, value)
    d = commit(other, {"services/messaging/later.py": "d"}, "D")
    git(other, "push", "-q", "origin", "main")
    server.set(watermark=d, anchors=[])
    assert run(repo, server)[0] == 0 and server.state()["advances"] == []


def test_a_rewound_main_fails_without_an_advance(repo, server):
    a, b, c = history(repo)
    git(repo, "push", "-q", "--force", "origin", f"{b}:refs/heads/main")
    git(repo, "reset", "-q", "--hard", b)
    server.set(watermark=c, anchors=[])
    with pytest.raises(Refused, match="diverged"):
        run(repo, server)
    assert server.state()["advances"] == []


def test_a_divergent_watermark_fails_without_an_advance(repo, server):
    a, b, c = history(repo)
    git(repo, "checkout", "-q", "-b", "side", a)
    side = commit(repo, {"services/messaging/side.py": "s"}, "side")
    git(repo, "checkout", "-q", "main")
    server.set(watermark=side, anchors=[])
    with pytest.raises(Refused, match="diverged"):
        run(repo, server)
    assert server.state()["advances"] == []


@pytest.mark.parametrize(("stage", "mode"), [("status", "exit"), ("list", "garbage"), ("advance", "exit")])
def test_the_token_and_cli_error_bodies_never_reach_the_output(repo, server, monkeypatch, capsys, stage, mode):
    a, b, c = history(repo)
    summary = repo.parent / "summary.md"
    server.set(watermark=a, anchors=["services/messaging/app.py", "services/messaging/new.py"], fault={stage: mode})
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("TIDEN_PRODUCT_ID", "product")
    monkeypatch.setattr(sys, "argv", ["tiden_drift.py", "--root", str(repo), "--cli", server.cli])
    assert drift.main() == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err + summary.read_text()
    assert "request body" not in captured.out + summary.read_text()


def test_the_exclusion_categories_are_exactly_the_reviewed_set():
    assert [pattern for pattern, _ in EXCLUDED] == [
        "*.md", "docs/*", ".claude/*", "tests/*", "*/tests/*", "*_test.go", "*.test.mjs", "*.test.js", "*.test.ts",
        "*/fixtures/*", "*/testdata/*", "uv.lock", "*/uv.lock", "package-lock.json", "*/package-lock.json",
        "*/go.sum", "*/go.mod", "*/requirements*.in", "*/requirements*.txt"]
    assert all(reason for _, reason in EXCLUDED)


def test_no_tracked_code_file_is_excluded():
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], check=True, capture_output=True,
                             text=True).stdout.split("\0")
    code = [path for path in tracked if path and not excluded(path)]
    for manifest in ("pyproject.toml", "services/calendar/pyproject.toml", "packages/npm/package.json"):
        assert manifest in code
    for path in ("scripts/tiden_drift.py", ".github/workflows/tiden-drift.yml", "deploy/main-ruleset.json",
                 "whatsapp/whatsmeow/main.go", "services/messaging/messaging_service/app.py"):
        assert not excluded(path)


@pytest.mark.parametrize("pattern", ["*", "services/*", "*.py", "*.go", "scripts/*"])
def test_a_broad_pattern_would_exclude_tracked_code(pattern):
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], check=True, capture_output=True,
                             text=True).stdout.split("\0")
    from fnmatch import fnmatchcase
    # The reviewed set above would have to change for such a pattern; these show what it would hide.
    assert any(fnmatchcase(path, pattern) and not excluded(path) for path in tracked)


def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def test_the_workflow_runs_after_every_merge_serialized_and_read_only():
    value = workflow()
    triggers = value[True]  # yaml reads the `on` key as a boolean
    assert triggers["push"] == {"branches": ["main"]} and "workflow_dispatch" in triggers
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert value["permissions"] == {"contents": "read"}
    assert value["concurrency"] == {"group": "tiden-drift", "cancel-in-progress": False}
    (job,) = value["jobs"].values()
    assert "permissions" not in job and job["if"] == "github.ref == 'refs/heads/main'"
    steps = job["steps"]
    assert steps[0]["run"] == 'test "$REF" = refs/heads/main'
    checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"] == {"persist-credentials": False, "ref": "main", "fetch-depth": 0}
    holders = [step["name"] for step in steps if "${{ secrets.TIDEN_API_TOKEN }}" in step.get("env", {}).values()]
    assert holders == ["Require an anchor for every changed code file, then advance the watermark"]
    config = next(step for step in steps if step.get("id") == "config")
    assert config["env"]["HAS_TOKEN"] == "${{ secrets.TIDEN_API_TOKEN != '' }}"
    assert all("secrets." not in step.get("run", "") for step in steps)


def test_the_workflow_installs_the_same_pinned_cli_as_ci():
    def install(text):
        return next(line.strip() for line in text.splitlines() if "sha256sum --check" in line)
    assert install(WORKFLOW.read_text()) == install((ROOT / ".github/workflows/ci.yml").read_text())


def test_the_workflow_passes_without_a_token_like_the_release_job():
    steps = next(iter(workflow()["jobs"].values()))["steps"]
    config = next(step for step in steps if step.get("id") == "config")
    assert "Tiden reporting is not configured" in config["run"]
    gated = [step for step in steps[2:]]
    assert all(step.get("if") == "steps.config.outputs.enabled == 'true'" for step in gated)


@pytest.mark.parametrize(("ref", "passes"), [("refs/heads/main", True), ("refs/heads/feature", False),
                                             ("refs/heads/MAIN", False), ("refs/tags/main", False)])
def test_the_ref_guard_admits_main_exactly(ref, passes):
    guard = workflow()["jobs"]["anchors"]["steps"][0]["run"]
    completed = subprocess.run(["bash", "-e", "-c", guard], env={"REF": ref, "PATH": "/usr/bin:/bin"})
    assert (completed.returncode == 0) == passes
