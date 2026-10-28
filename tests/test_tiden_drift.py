"""The drift watermark job against temporary Git repositories and a fake Tiden CLI; it never calls Tiden."""
from datetime import datetime, timedelta, timezone
import inspect
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


UPDATED = datetime(2026, 10, 28, 10, tzinfo=timezone.utc)


def sync(sha, conclusion="success", number=1, attempt=1, status="completed", title=None):
    """One `Tiden sync` run as the GitHub run listing returns it."""
    return {"id": number, "run_attempt": attempt, "display_title": title or f"Tiden sync {sha}",
            "html_url": f"https://github.invalid/runs/{number}", "status": status,
            "conclusion": conclusion if status == "completed" else None,
            "updated_at": f"{UPDATED + timedelta(minutes=number, seconds=attempt):%Y-%m-%dT%H:%M:%SZ}",
            "run_started_at": "2026-10-28T10:00:00Z"}


def listing(*runs):
    return lambda since, success: [run for run in runs if not success or run["conclusion"] == "success"]


FAKE_GH = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys
    with open(os.environ["FAKE_GH_CALLS"], "a") as calls:
        calls.write(json.dumps(sys.argv[1:]) + "\\n")
    runs = json.load(open(os.environ["FAKE_GH_RUNS"]))
    if runs is None:
        sys.stderr.write("HTTP 502 {SECRET}\\n"); sys.exit(1)
    print(json.dumps({{"total_count": len(runs), "workflow_runs": runs}}))
""")


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """A fake `gh api` on PATH that answers every run listing with the runs set here."""
    directory = tmp_path / "gh-bin"
    directory.mkdir()
    (directory / "gh").write_text(FAKE_GH)
    (directory / "gh").chmod(0o755)
    runs, calls = tmp_path / "gh-runs.json", tmp_path / "gh-calls.jsonl"
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_RUNS", str(runs))
    monkeypatch.setenv("FAKE_GH_CALLS", str(calls))

    class Gh:
        def set(self, value):
            runs.write_text(json.dumps(value))

        def calls(self):
            return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []

    return Gh()


def run(repo, server, syncs=None):
    if syncs is None:  # every first-parent commit of main has a successful sync
        shas = git(repo, "rev-list", "--first-parent", "HEAD").split()
        syncs = listing(*(sync(sha, number=number) for number, sha in enumerate(shas, 1)))
    lines = []
    code = check(repo, server.cli, "product", lines.append, syncs)
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
def test_the_token_and_cli_error_bodies_never_reach_the_output(repo, server, monkeypatch, capsys, stage, mode, gh):
    a, b, c = history(repo)
    gh.set([sync(b, number=1), sync(c, number=2)])
    summary = repo.parent / "summary.md"
    server.set(watermark=a, anchors=["services/messaging/app.py", "services/messaging/new.py"], fault={stage: mode})
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("TIDEN_PRODUCT_ID", "product")
    monkeypatch.setattr(sys, "argv", ["tiden_drift.py", "--root", str(repo), "--cli", server.cli,
                                      "--sync-repository", "GoSync-Inc/deedkit"])
    assert drift.main() == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err + summary.read_text()
    assert "request body" not in captured.out + summary.read_text()


def test_the_exclusion_categories_are_exactly_the_reviewed_set():
    assert [pattern for pattern, _ in EXCLUDED] == [
        "*.md", "docs/*", ".claude/*", "tests/*", "*/tests/*", "*_test.go", "*.test.mjs", "*.test.js", "*.test.ts",
        "*/fixtures/*", "*/testdata/*", "uv.lock", "*/uv.lock", "package-lock.json", "*/package-lock.json",
        "*/go.sum", "*/go.mod", "*/requirements*.in", "*/requirements*.txt", "*/requirements*.lock",
        "LICENSE", "*/LICENSE", "*-LICENSE"]
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


LICENCE_TEXTS = {"LICENSE", "services/messaging/LICENSE", "services/calendar/LICENSE", "services/calls/LICENSE",
                 "packages/npm/LICENSE", "packages/platform-contracts/LICENSE", "packages/deedkit/LICENSE",
                 "licenses/age-LICENSE", "deploy/MOBY-LICENSE"}


# spec: ANYM-504
def test_the_licence_patterns_match_exactly_the_nine_licence_texts():
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], check=True, capture_output=True,
                             text=True).stdout.split("\0")
    from fnmatch import fnmatchcase
    licence = [pattern for pattern, reason in EXCLUDED if reason == "licence text"]
    assert licence == ["LICENSE", "*/LICENSE", "*-LICENSE"]
    assert {path for path in tracked if any(fnmatchcase(path, pattern) for pattern in licence)} == LICENCE_TEXTS
    assert all(excluded(path) for path in LICENCE_TEXTS)
    # Build contexts, enforcement and manifests stay checked.
    for path in ("Dockerfile", ".dockerignore", ".gitignore", ".github/dco.yml", "deploy/.env.example",
                 "pyproject.toml", "services/gateway/Dockerfile", "services/gateway/Dockerfile.dockerignore"):
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
    assert triggers["workflow_run"] == {"workflows": ["Tiden sync"], "types": ["completed"]}
    assert "workflow_dispatch" in triggers and set(triggers) == {"workflow_run", "workflow_dispatch"}
    assert value["permissions"] == {"contents": "read", "actions": "read"}
    assert value["concurrency"] == {"group": "tiden-drift", "cancel-in-progress": False}
    (job,) = value["jobs"].values()
    assert "permissions" not in job and job["if"] == ("github.ref == 'refs/heads/main' && (github.event_name == "
                                                      "'workflow_dispatch' || github.event_name == 'workflow_run')")
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


ANCHORED = ["services/messaging/app.py", "services/messaging/new.py"]


# spec: ANYM-593
def test_a_merge_whose_sync_is_pending_blocks_the_check_until_its_sync_succeeds(repo, server):
    # A < B after the watermark W; B's sync finished first and A's is still running.
    w, a, b = history(repo)
    server.set(watermark=w, anchors=[])  # no anchor yet: a check would fail
    code, output = run(repo, server, listing(sync(b, number=2), sync(a, number=1, status="in_progress")))
    assert code == 0 and server.state()["advances"] == []
    assert f"- {a}: in_progress (https://github.invalid/runs/1)" in output and f"- {b}: synced" in output
    assert "no Tiden source anchor" not in output
    # A's sync succeeds and anchored the files: one run checks W..B and advances to B.
    server.set(anchors=ANCHORED)
    code, output = run(repo, server, listing(sync(b, number=2), sync(a, number=1)))
    assert code == 0 and server.state()["watermark"] == b
    assert [(call["--from"], call["--to"]) for call in server.state()["advances"]] == [(w, b)]


# spec: ANYM-593
@pytest.mark.parametrize("other", [
    sync("", title="Tiden sync ready merge proposal/2026-10-28-card", number=3),
    sync("", conclusion="skipped", number=3),
    sync("0" * 40, number=3),
], ids=["ready-merge", "skipped", "unrelated-commit"])
def test_a_ready_merge_skipped_or_unrelated_sync_run_counts_for_no_commit(repo, server, other):
    w, a, b = history(repo)
    other = other | ({"display_title": f"Tiden sync {a}"} if other["conclusion"] == "skipped" else {})
    server.set(watermark=w, anchors=ANCHORED)
    code, output = run(repo, server, listing(other, sync(b, number=2)))
    assert code == 0 and server.state()["advances"] == []
    assert f"- {a}: no sync run" in output
    # The checker takes no upstream run at all: whatever completion triggered it, it reads main and the run list.
    assert list(inspect.signature(check).parameters) == ["root", "cli", "product", "report", "syncs"]


# spec: ANYM-593
def test_the_newest_run_covers_merges_whose_waiting_drift_run_was_dropped(repo, server):
    w = commit(repo, {"base.py": "w"}, "W")
    a = commit(repo, {"services/messaging/app.py": "a"}, "A")
    b = commit(repo, {"services/messaging/new.py": "b"}, "B")
    c = commit(repo, {"services/messaging/later.py": "c"}, "C")
    git(repo, "push", "-q", "origin", "main")
    server.set(watermark=w, anchors=[*ANCHORED, "services/messaging/later.py"])
    # Drift for B's sync: A's sync still running, C's queued.
    code, output = run(repo, server, listing(sync(a, number=1, status="in_progress"), sync(b, number=2),
                                             sync(c, number=3, status="queued")))
    assert code == 0 and server.state()["advances"] == []
    assert f"- {c}: queued" in output and f"- {a}: in_progress" in output
    # Drift for C's sync was replaced in the queue by drift for A's late sync: that run sees all three done.
    code, output = run(repo, server, listing(sync(c, number=3), sync(a, number=1, attempt=1), sync(b, number=2)))
    assert code == 0 and server.state()["watermark"] == c
    assert [(call["--from"], call["--to"]) for call in server.state()["advances"]] == [(w, c)]


# spec: ANYM-593
@pytest.mark.parametrize("retry", ["rerun", "dispatch"])
def test_a_failed_sync_blocks_the_advance_and_its_later_success_unblocks_it(repo, server, retry):
    w, a, b = history(repo)
    server.set(watermark=w, anchors=ANCHORED)
    failed = sync(a, conclusion="failure", number=1)
    code, output = run(repo, server, listing(failed, sync(b, number=2)))
    assert code == 0 and server.state()["advances"] == []
    assert f"- {a}: failure (https://github.invalid/runs/1)" in output
    # A rerun keeps the run ID with a new attempt; a dispatch by SHA is a new run of the same title.
    later = sync(a, number=1, attempt=2) if retry == "rerun" else sync(a, number=3)
    runs = [later, sync(b, number=2)] + ([failed] if retry == "dispatch" else [])
    assert run(repo, server, listing(*runs))[0] == 0 and server.state()["watermark"] == b


# spec: ANYM-593
def test_a_failed_run_listing_checks_and_advances_nothing_and_fails(repo, server):
    w, a, b = history(repo)
    server.set(watermark=w, anchors=ANCHORED)

    def broken(since, success):
        raise drift.Unknown("gh api exited 1")

    with pytest.raises(Refused, match="Tiden sync runs could not be read"):
        run(repo, server, broken)
    assert server.state()["advances"] == []


# spec: ANYM-593
def test_the_runs_are_listed_from_main_through_gh_api_and_a_failed_read_is_named(repo, server, monkeypatch, capsys,
                                                                                gh):
    w, a, b = history(repo)
    server.set(watermark=w, anchors=ANCHORED)
    monkeypatch.setenv("TIDEN_PRODUCT_ID", "product")
    monkeypatch.setattr(sys, "argv", ["tiden_drift.py", "--root", str(repo), "--cli", server.cli,
                                      "--sync-repository", "GoSync-Inc/deedkit"])
    gh.set(None)
    assert drift.main() == 1 and server.state()["advances"] == []
    out = capsys.readouterr().out
    assert "Refused: the Tiden sync runs could not be read (gh api exited 1)" in out and SECRET not in out
    gh.set([sync(a, number=1), sync(b, number=2)])
    assert drift.main() == 0 and server.state()["watermark"] == b
    endpoint, branch = gh.calls()[-1][3], gh.calls()[-1][5]
    assert endpoint == "repos/GoSync-Inc/deedkit/actions/workflows/tiden-sync.yml/runs" and branch == "branch=main"


def guard_step():
    steps = workflow()["jobs"]["anchors"]["steps"]
    names = [step.get("name") for step in steps]
    index = names.index("Require a Tiden sync run of this repository or a dispatch")
    checkout = next(i for i, step in enumerate(steps) if str(step.get("uses", "")).startswith("actions/checkout@"))
    holder = names.index("Require an anchor for every changed code file, then advance the watermark")
    return steps[index], index < checkout < holder


REPOSITORY = "GoSync-Inc/deedkit"
SYNC_RUN = {"EVENT": "workflow_run", "UPSTREAM_EVENT": "workflow_run", "UPSTREAM_REPOSITORY": REPOSITORY,
            "UPSTREAM_HEAD_REPOSITORY": REPOSITORY, "REPOSITORY": REPOSITORY}


# spec: ANYM-593
@pytest.mark.parametrize(("env", "passes"), [
    ({"EVENT": "workflow_dispatch", "REPOSITORY": REPOSITORY}, True),
    (SYNC_RUN, True),
    (SYNC_RUN | {"UPSTREAM_EVENT": "workflow_dispatch"}, True),
    (SYNC_RUN | {"UPSTREAM_REPOSITORY": "example/deedkit"}, False),
    (SYNC_RUN | {"UPSTREAM_HEAD_REPOSITORY": "example/deedkit"}, False),
    (SYNC_RUN | {"UPSTREAM_REPOSITORY": "gosync-inc/deedkit"}, False),
    (SYNC_RUN | {"UPSTREAM_EVENT": "push"}, False),
    (SYNC_RUN | {"UPSTREAM_EVENT": "pull_request"}, False),
    (SYNC_RUN | {"UPSTREAM_EVENT": "Workflow_Run"}, False),
    (SYNC_RUN | {"UPSTREAM_EVENT": ""}, False),
    ({key: value for key, value in SYNC_RUN.items() if key != "UPSTREAM_REPOSITORY"}, False),
    (SYNC_RUN | {"EVENT": "push"}, False),
], ids=["dispatch", "sync-by-ci", "sync-dispatched", "foreign-repository", "foreign-head", "case-variant",
        "push", "pull-request", "event-case", "event-empty", "repository-missing", "drift-push"])
def test_a_malformed_or_foreign_upstream_run_stops_the_job_before_the_token(env, passes):
    step, ordered = guard_step()
    assert ordered and step["if"] == "steps.config.outputs.enabled == 'true'"
    assert all("secrets." not in str(value) for value in step["env"].values())
    completed = subprocess.run(["bash", "-e", "-c", step["run"]], env={**env, "PATH": "/usr/bin:/bin"})
    assert (completed.returncode == 0) == passes


# spec: ANYM-593
def test_the_checker_reads_no_upstream_run_data():
    steps = workflow()["jobs"]["anchors"]["steps"]
    checker = next(step for step in steps if step.get("name", "").startswith("Require an anchor"))
    assert checker["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert checker["run"] == ('python3 scripts/tiden_drift.py --cli "$RUNNER_TEMP/tiden-bin/tiden" '
                              '--sync-repository "$GITHUB_REPOSITORY"')
    assert "github.event" not in json.dumps(checker) and ">> \"$GITHUB_PATH\"" not in WORKFLOW.read_text()


class Pages(drift.Actions):
    """Tiden health's run listing with GitHub's pages faked: a `created` qualifier (`A..B` or `>=A`) and `status`
    select ROWS by `created_at` and conclusion; like GitHub, a listing exposes at most 1,000 matches in 100-row pages
    and reports at most 1,000 as `total_count`."""

    def __init__(self, rows):
        super().__init__("GoSync-Inc/deedkit", clock=lambda: 0.0)
        self.rows, self.queries = rows, []

    def page(self, number, created, end, workflow="tiden-sync.yml", filters=()):
        assert workflow == "tiden-sync.yml" and end > 0
        self.queries.append((created, filters, number))
        low, _, high = created.removeprefix(">=").partition("..")
        matches = [row for row in self.rows if datetime.fromisoformat(low) <= datetime.fromisoformat(row["created_at"])
                   and (not high or datetime.fromisoformat(row["created_at"]) <= datetime.fromisoformat(high))
                   and ("status=success" not in filters or row["conclusion"] == "success")][:1000]
        return {"total_count": len(matches), "workflow_runs": matches[(number - 1) * 100:number * 100]}


NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


def spread(shas, start, count, hours, conclusion="success"):
    """COUNT runs over HOURS from START, newest first as GitHub lists them, cycling the titles of SHAS."""
    rows = []
    for number in range(count):
        created = start + timedelta(hours=hours) * number / count
        rows.append(sync(shas[number % len(shas)], conclusion=conclusion, number=number + 1)
                    | {"created_at": f"{created:%Y-%m-%dT%H:%M:%SZ}"})
    return rows[::-1]


# spec: ANYM-593
def test_a_watermark_stalled_past_the_listing_cap_still_reads_every_run_by_day(repo, server):
    w, a, b = history(repo)
    server.set(watermark=w, anchors=ANCHORED)
    since = NOW - timedelta(days=4)
    # 2,400 successful runs over four days, about 600 a day, plus one on a window boundary: no single listing could
    # return them all.
    rows = spread([a, b, "0" * 40], since, 2400, 4 * 24)
    rows.append(sync(a, number=9999) | {"created_at": f"{since + timedelta(days=2):%Y-%m-%dT%H:%M:%SZ}"})
    pages = Pages(rows)
    syncs = drift.SyncRuns(pages, now=lambda: NOW)
    listed = syncs(since, True)
    assert len(listed) == len(rows) and {row["id"] for row in listed} == {row["id"] for row in rows}
    windows = [f"{since + timedelta(days=day):%Y-%m-%dT%H:%M:%SZ}..{since + timedelta(days=day + 1):%Y-%m-%dT%H:%M:%SZ}"
               for day in range(5)]
    assert [created for created, _, number in pages.queries if number == 1] == windows
    assert {filters for _, filters, _ in pages.queries} == {("status=success",)}
    # The oldest window is read too: with the syncs of A and B only on the first day, the run still advances.
    assert run(repo, server, lambda start, success: syncs(since, success))[0] == 0
    assert server.state()["watermark"] == b
    old = {row["id"] for row in rows if row["display_title"] != f"Tiden sync {'0' * 40}"
           and datetime.fromisoformat(row["created_at"]) < since + timedelta(days=1)}
    server.set(watermark=w)
    pages.rows = [row for row in rows if row["id"] in old]
    assert run(repo, server, lambda start, success: syncs(since, success))[0] == 0
    assert server.state()["watermark"] == b


# spec: ANYM-593
def test_a_day_that_reaches_the_listing_cap_is_unknown_never_synced(repo, server):
    w, a, b = history(repo)
    server.set(watermark=w, anchors=ANCHORED)
    since = NOW - timedelta(hours=12)
    syncs = drift.SyncRuns(Pages(spread([a, b], since, 1200, 1)), now=lambda: NOW)
    with pytest.raises(Refused, match="listing cap"):
        run(repo, server, lambda start, success: syncs(since, success))
    assert server.state()["advances"] == []


# spec: ANYM-593
def test_the_state_listing_reads_all_runs_only_when_a_commit_waits(repo, server):
    w, a, b = history(repo)
    server.set(watermark=w, anchors=ANCHORED)
    calls = []
    runs = [sync(a, number=1), sync(b, number=2, status="in_progress")]

    def syncs(since, success):
        calls.append(success)
        return listing(*runs)(since, success)

    code, output = run(repo, server, syncs)
    assert code == 0 and calls == [True, False] and f"- {b}: in_progress" in output
    runs[1] = sync(b, number=2)
    calls.clear()
    assert run(repo, server, syncs)[0] == 0 and calls == [True] and server.state()["watermark"] == b
