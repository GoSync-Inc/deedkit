"""The Tiden health report against temporary Git repositories and a fake Tiden CLI; it never calls Tiden."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest
import yaml

import scripts.tiden_health as health
from scripts.tiden_health import ENVIRONMENT, FAIL, PASS, UNKNOWN, Tiden, outcome, parse_no_spec_list, report, suite

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/tiden-health.yml"
SECRET = "tfy_sentinel_secret_value"
MAIN_ID = "branch-main"
LIST = "config/tiden-unlinked-tests.json"
# A stateful stand-in for the Tiden API: listings, run timelines and every command line live in state.json.
FAKE = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys
    path = os.environ["FAKE_TIDEN_STATE"]
    state = json.load(open(path))
    args = sys.argv[1:]
    state.setdefault("calls", []).append(args)
    label = " ".join(args[:2])
    def option(name, default=None):
        return args[args.index(name) + 1] if name in args else default
    def count(key):
        state.setdefault("counts", {{}})[key] = state["counts"].get(key, -1) + 1
        return state["counts"][key]
    def emit(value):
        json.dump(state, open(path, "w"))
        mode = state.get("fault", {{}}).get(label)
        if mode == "exit":
            sys.stderr.write("response body {SECRET}\\n"); print("{SECRET}"); sys.exit(3)
        if mode == "garbage":
            print("not json {SECRET}"); sys.exit(0)
        print(json.dumps(value))
    def page(key, rows):
        fault = state.get("page_fault", {{}}).get(key)
        size, token = int(option("--page-size", "50")), option("--page-token", "0")
        if not token.isdigit():
            return emit({{key: [], "pagination": {{"nextPageToken": token, "totalCount": len(rows)}}}})
        start = int(token)
        chunk = list(rows[start:start + size])
        token = str(start + size) if start + size < len(rows) else ""
        total = len(rows) + (1 if fault == "total" else 0)
        if not token and fault == "token":
            token = "more"
        if not token and fault == "duplicate" and rows:
            chunk.append(rows[0])
        emit({{key: chunk, "pagination": {{"nextPageToken": token, "totalCount": total}}}})
    if args[:2] == ["drift", "status"]:
        marks = state["watermarks"]
        emit({{"repository": state.get("repository", "github.com/GoSync-Inc/deedkit"),
               "watermarkSha": marks[min(count("drift"), len(marks) - 1)]}})
    elif args[:2] == ["spec", "list"]:
        specs = state["specs"]
        emit(state.get("spec_listing") or {{"requirements": specs,
                                           "pagination": {{"nextPageToken": "", "totalCount": len(specs)}}}})
    elif args[:2] == ["branch", "list"]:
        emit({{"branches": state.get("branches", [{{"id": "{MAIN_ID}", "name": "main"}},
                                                  {{"id": "branch-intent", "name": "intent/x"}}])}})
    elif args[:2] == ["test", "list"]:
        page("tests", state["tests"])
    elif args[:2] == ["run", "list"]:
        timeline = state["runs"]
        runs = timeline[min(count("runs"), len(timeline) - 1)] if option("--page-token") is None else \\
            timeline[min(state["counts"]["runs"], len(timeline) - 1)]
        runs = [r for r in runs if r["branchName"] == option("--branch") and r["environmentSlug"] == option("--environment")]
        page("runs", sorted(runs, key=lambda r: -r["seqNum"]))
    elif args[:2] == ["run", "results"]:
        page("results", state["results"].get(args[2], []))
    else:
        sys.exit(64)
""")


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(root, files, message, push=True):
    for name, text in files.items():
        if text is None:
            git(root, "rm", "-q", "--", name)
            continue
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(root, "add", "--", name)
    git(root, "commit", "-q", "-m", message)
    if push:
        git(root, "push", "-q", "origin", "main")
    return git(root, "rev-parse", "HEAD")


def no_spec(*entries):
    return json.dumps({"format": 1, "cases": list(entries)})


def entry(case_id, category="missing-spec", reason="no spec states it", external=None):
    return {"caseId": case_id, "externalId": external or f"s:tests.test_x::{case_id}", "category": category,
            "reason": reason}


def case(case_id, path="tests/test_x.py", linked=1, external=None, **fields):
    row = {"id": case_id, "kind": "case", "filePath": path, "externalId": external or f"s:tests.test_x::{case_id}",
           "title": f"test {case_id}", "createdAt": "2026-10-01T00:00:00Z", **fields}
    if linked:
        row["linkedRequirementCount"] = linked  # the JSON API omits a zero count
    return row


def run(seq, sha, status="passed", live="succeeded", branch="main", branch_id=MAIN_ID, environment=ENVIRONMENT):
    return {"id": f"run-{seq}", "seqNum": seq, "buildSha": sha, "status": status, "liveDocStatus": live,
            "branchName": branch, "branchId": branch_id, "environmentSlug": environment,
            "completedAt": "2026-10-18T12:00:00Z", "description": f"https://example.invalid/runs/{seq}"}


def results(*case_ids):
    return [{"id": f"result-{index}", "testId": case_id} for index, case_id in enumerate(case_ids)]


def spec(seq, status="Active", content: str | None = "Acceptance criteria:\n- it works", paths=(),
         repository: str | None = "github.com/GoSync-Inc/deedkit"):
    sources = [{"id": f"source-{seq}-{index}", "sourceType": "repo_file", "repoPath": path, "repository": repository}
               for index, path in enumerate(paths)]
    return {"id": f"spec-{seq}", "seqNum": seq, "title": f"Spec {seq}", "status": status, "content": content,
            **({"sources": sources, "sourceCount": len(sources)} if sources else {})}


@pytest.fixture
def repo(tmp_path):
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
        git(work, "config", key, value)
    git(work, "remote", "add", "origin", str(origin))
    return work


@pytest.fixture(autouse=True)
def audited(monkeypatch):
    monkeypatch.setattr(health, "DONE_AUDIT", "2026-10-18")
    for key in ("UPSTREAM_RUN_URL", "UPSTREAM_CONCLUSION", "UPSTREAM_SHA", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(key, raising=False)


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
            state = json.loads(path.read_text()) if path.exists() else {}
            state.update(values)
            path.write_text(json.dumps(state))

        def state(self):
            return json.loads(path.read_text())

        def world(self, target, cases=None, active=None, **values):
            """A healthy default: watermark at TARGET, one anchored Active spec, every case active in run 10."""
            cases = [case("c1")] if cases is None else cases
            active = [row["id"] for row in cases] if active is None else active
            self.set(watermarks=[target], specs=[spec(1, paths=["services/app.py"])], tests=cases,
                     runs=[[run(10, target)]], results={"10": results(*active)}, counts={}, calls=[])
            self.set(**values)

    return Server()


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0.0, 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.sleeps += 1


def history(repo, entries=()):
    a = commit(repo, {"services/app.py": "a", LIST: no_spec(*entries)}, "A")
    b = commit(repo, {"services/app.py": "b", "tests/test_x.py": "t"}, "B")
    return a, b


def measure(repo, server, target, fetch=False, clock=None, actions=None):
    clock = clock or Clock()
    data = report(repo, Tiden(server.cli, "product", "workspace"), target, "refs/remotes/origin/main", fetch, clock,
                  clock.sleep, actions=actions)
    return outcome(data), data


def states(data):
    return {name: entry["state"] for name, entry in data["metrics"].items()}


# Exit codes and result states.

def test_all_pass_exits_0_and_records_target_and_evidence(repo, server):
    _, b = history(repo)
    server.world(b)
    code, data = measure(repo, server, b)
    assert code == 0 and set(states(data).values()) == {PASS}
    assert data["target"] == b
    assert data["evidence"]["run"] == {"seqNum": 10, "buildSha": b, "environmentSlug": ENVIRONMENT, "status": "passed",
                                       "liveDocStatus": "succeeded", "completedAt": "2026-10-18T12:00:00Z",
                                       "description": "https://example.invalid/runs/10"}
    assert data["observedAt"]
    lines = "\n".join(health.render(data, code))
    assert "## Tiden health: PASS" in lines and b in lines and "Tiden run 10 on main" in lines


def test_any_fail_exits_1_after_listing_every_finding(repo, server):
    _, b = history(repo)
    server.world(b, specs=[spec(1, paths=["services/app.py", "gone/one.py", "gone/two.py"]),
                           spec(2, "Done", "- live check [open: live]")])
    code, data = measure(repo, server, b)
    assert code == 1
    assert states(data) == {"drift": PASS, "unlinked_tests": PASS, "stale_anchors": FAIL, "done_open_checks": FAIL}
    lines = "\n".join(health.render(data, code))
    assert "gone/one.py" in lines and "gone/two.py" in lines and "ANYM-2 Spec 2: - live check [open: live]" in lines


def test_none_failing_with_one_unknown_exits_2(repo, server, monkeypatch):
    _, b = history(repo)
    server.world(b)
    monkeypatch.setattr(health, "DONE_AUDIT", None)
    code, data = measure(repo, server, b)
    assert code == 2 and states(data)["done_open_checks"] == UNKNOWN


def test_a_fail_wins_over_an_unknown(repo, server, monkeypatch):
    _, b = history(repo)
    server.world(b, specs=[spec(1, paths=["gone.py"])])
    monkeypatch.setattr(health, "DONE_AUDIT", None)
    assert measure(repo, server, b)[0] == 1


@pytest.mark.parametrize("fault", ["total", "token", "duplicate"])
def test_an_incomplete_test_listing_makes_the_unlinked_metric_unknown(repo, server, fault):
    _, b = history(repo)
    server.world(b, cases=[case(f"c{index}") for index in range(5)], page_fault={"tests": fault})
    code, data = measure(repo, server, b)
    assert code == 2 and states(data) == {"drift": PASS, "unlinked_tests": UNKNOWN, "stale_anchors": PASS,
                                          "done_open_checks": PASS}
    assert data["metrics"]["unlinked_tests"]["reason"].startswith("tiden test list")


@pytest.mark.parametrize("fault", ["total", "token", "duplicate"])
def test_an_incomplete_results_listing_makes_the_unlinked_metric_unknown(repo, server, fault):
    _, b = history(repo)
    server.world(b, page_fault={"results": fault})
    assert states(measure(repo, server, b)[1])["unlinked_tests"] == UNKNOWN


@pytest.mark.parametrize("listing", [
    {"requirements": [spec(1)], "pagination": {"nextPageToken": "next", "totalCount": 1}},
    {"requirements": [spec(1)], "pagination": {"nextPageToken": "", "totalCount": 2}},
    {"requirements": [spec(1), spec(1)], "pagination": {"nextPageToken": "", "totalCount": 2}},
    {"requirements": [dict(spec(1, paths=["services/app.py"]), sourceCount=2)],
     "pagination": {"nextPageToken": "", "totalCount": 1}},
    {"requirements": [spec(1)]},
])
def test_an_incomplete_spec_listing_makes_both_spec_metrics_unknown(repo, server, listing):
    _, b = history(repo)
    server.world(b, spec_listing=listing)
    code, data = measure(repo, server, b)
    assert code == 2
    assert states(data) == {"drift": PASS, "unlinked_tests": PASS, "stale_anchors": UNKNOWN,
                            "done_open_checks": UNKNOWN}


@pytest.mark.parametrize("command", ["drift status", "spec list", "test list", "run list", "run results",
                                     "branch list"])
@pytest.mark.parametrize("mode", ["exit", "garbage"])
def test_cli_errors_name_the_command_only_never_the_response(repo, server, capsys, command, mode):
    _, b = history(repo)
    server.world(b, fault={command: mode})
    code, data = measure(repo, server, b)
    assert code == 2
    reasons = [entry["reason"] for entry in data["metrics"].values() if entry["state"] == UNKNOWN]
    assert reasons and all(reason.startswith(f"tiden {command}") for reason in reasons)
    text = json.dumps(data) + "\n".join(health.render(data, code)) + "".join(capsys.readouterr())
    assert SECRET not in text


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "success"])
def test_after_any_upstream_conclusion_without_published_evidence_the_report_runs_and_states_unknown(
        repo, server, monkeypatch, conclusion):
    """A failed or cancelled CI run, or a failed Tiden publication: no Tiden run for TARGET ever appears."""
    a, b = history(repo)
    server.world(b, runs=[[run(9, a)]])
    monkeypatch.setenv("UPSTREAM_CONCLUSION", conclusion)
    monkeypatch.setenv("UPSTREAM_SHA", b)
    clock = Clock()
    code, data = measure(repo, server, b, clock=clock)
    assert code == 2
    assert states(data) == {"drift": PASS, "unlinked_tests": UNKNOWN, "stale_anchors": PASS, "done_open_checks": PASS}
    assert clock.now >= health.TESTS_WAIT
    lines = "\n".join(health.render(data, code))
    assert f"Upstream CI: {conclusion} for `{b}`" in lines
    assert f"newest completed main run 9 is for its own SHA `{a}`" in lines


# Test evidence for TARGET.

def test_ci_of_an_older_commit_is_not_evidence_for_target(repo, server):
    a, b = history(repo)
    server.world(b, runs=[[run(11, a)]])
    code, data = measure(repo, server, b)
    assert states(data)["unlinked_tests"] == UNKNOWN and data["evidence"]["older_run"]["buildSha"] == a


@pytest.mark.parametrize("other", [dict(environment="local-synthetic"), dict(branch_id="branch-imposter"),
                                   dict(status="in_progress"), dict(status="aborted")])
def test_runs_that_are_not_completed_main_ci_runs_of_target_are_ignored(repo, server, other):
    _, b = history(repo)
    server.world(b, runs=[[run(12, b, **other)]],
                 branches=[{"id": MAIN_ID, "name": "main"}, {"id": "branch-imposter", "name": "Main"}])
    if other.get("environment"):
        server.set(runs=[[dict(run(12, b), environmentSlug="local-synthetic")]])
    if other.get("branch_id"):
        server.set(runs=[[dict(run(12, b), branchId="branch-imposter")]])
    assert states(measure(repo, server, b)[1])["unlinked_tests"] == UNKNOWN


def test_the_newest_matching_run_by_sequence_is_used(repo, server):
    _, b = history(repo)
    server.world(b, cases=[case("c1"), case("c2", linked=0)], runs=[[run(10, b), run(14, b, status="failed")]],
                 results={"10": results("c1"), "14": results("c1", "c2")})
    code, data = measure(repo, server, b)
    assert data["evidence"]["run"]["seqNum"] == 14 and data["evidence"]["run"]["status"] == "failed"
    assert states(data)["unlinked_tests"] == FAIL  # c2 is active and unlinked only in run 14


@pytest.mark.parametrize("live", ["failed", "skipped"])
def test_a_target_run_whose_live_doc_sync_failed_is_unknown_never_pass(repo, server, live):
    _, b = history(repo)
    # The new unlinked case never reached the inventory, so the run's results name a case it does not hold.
    server.world(b, runs=[[run(10, b, live=live)]], results={"10": results("c1", "new-unlinked")})
    clock = Clock()
    code, data = measure(repo, server, b, clock=clock)
    assert states(data)["unlinked_tests"] == UNKNOWN and clock.sleeps == 0
    assert f"live-doc sync {live}" in data["metrics"]["unlinked_tests"]["reason"]


def test_a_pending_live_doc_sync_is_waited_for_and_then_unknown(repo, server):
    _, b = history(repo)
    server.world(b, runs=[[run(10, b, live="pending")]], results={"10": results("c1", "new-unlinked")})
    clock = Clock()
    code, data = measure(repo, server, b, clock=clock)
    assert states(data)["unlinked_tests"] == UNKNOWN and clock.now >= health.TESTS_WAIT


def test_a_result_naming_a_case_missing_from_the_inventory_is_unknown(repo, server):
    _, b = history(repo)
    server.world(b, results={"10": results("c1", "new-unlinked")})
    data = measure(repo, server, b)[1]
    assert states(data)["unlinked_tests"] == UNKNOWN and "missing from the main inventory" in \
        data["metrics"]["unlinked_tests"]["reason"]


def test_evidence_published_while_waiting_is_used(repo, server):
    _, b = history(repo)
    server.world(b, runs=[[], [run(10, b, status="in_progress", live="pending")], [run(10, b)]])
    clock = Clock()
    code, data = measure(repo, server, b, clock=clock)
    assert code == 0 and clock.sleeps == 2


def test_missing_evidence_is_unknown_only_after_the_bound(repo, server):
    _, b = history(repo)
    server.world(b, runs=[[]])
    clock = Clock()
    data = measure(repo, server, b, clock=clock)[1]
    assert states(data)["unlinked_tests"] == UNKNOWN and "older_run" not in data["evidence"]
    assert health.TESTS_WAIT <= clock.now < health.TESTS_WAIT + health.POLL + 1


def test_results_are_read_over_every_page(repo, server):
    _, b = history(repo)
    cases = [case(f"c{index}") for index in range(450)]
    server.world(b, cases=cases)
    code, data = measure(repo, server, b)
    assert code == 0 and data["metrics"]["unlinked_tests"]["numbers"]["overall"]["active"] == 450
    pages = [call for call in server.state()["calls"] if call[:2] == ["run", "results"]]
    assert len(pages) == 3 and all("--latest-only" in call for call in pages)


# Unlinked tests.

def suite_cases(prefix, active, unlinked, path="services/gateway/tests/test_x.py"):
    return [case(f"{prefix}{index}", path, linked=0 if index < unlinked else 1) for index in range(active)]


@pytest.mark.parametrize(("unlinked", "state"), [(49, PASS), (50, FAIL)])
def test_a_suite_at_4_9_percent_passes_and_at_5_percent_fails(repo, server, unlinked, state):
    cases = suite_cases("g", 1000, unlinked) + suite_cases("r", 1000, 0, "tests/test_x.py")
    a, _ = history(repo, [entry(row["id"], external=row["externalId"]) for row in cases[:unlinked]])
    b = commit(repo, {"services/app.py": "c"}, "C")
    server.world(b, cases=cases)
    data = measure(repo, server, b)[1]
    metric = data["metrics"]["unlinked_tests"]
    assert metric["state"] == state and metric["numbers"]["suites"]["gateway"]["unlinked"] == unlinked
    assert metric["numbers"]["overall"]["share"] < 5
    assert any(item.startswith("gateway:") for item in metric["findings"]) == (state == FAIL)


def test_the_overall_share_fails_by_itself(repo, server):
    cases = []
    for name, path in (("a", "tests/t.py"), ("b", "services/calendar/t.py"), ("c", "services/calls/t.py")):
        cases += suite_cases(name, 100, 4, path)  # 4% each, 4% overall: passes
    history(repo, [entry(row["id"], external=row["externalId"]) for row in cases if "linkedRequirementCount" not in row])
    b = git(repo, "rev-parse", "HEAD")
    server.world(b, cases=cases)
    assert states(measure(repo, server, b)[1])["unlinked_tests"] == PASS
    cases += suite_cases("d", 4, 4, "services/gateway/t.py")  # 16 of 304 = 5.26% overall; gateway itself fails too
    server.world(b, cases=cases)
    findings = measure(repo, server, b)[1]["metrics"]["unlinked_tests"]["findings"]
    assert any(item.startswith("overall: 16 of 304") for item in findings)


def test_one_unlisted_unlinked_case_fails_although_the_share_is_below_one_percent(repo, server):
    _, b = history(repo)
    cases = suite_cases("r", 101, 1, "tests/test_x.py")
    server.world(b, cases=cases)
    metric = measure(repo, server, b)[1]["metrics"]["unlinked_tests"]
    assert metric["state"] == FAIL and metric["numbers"]["overall"]["share"] == 0.99
    assert metric["findings"] == ["unlinked and not in the no-spec list: r0 tests/test_x.py test r0"]


def test_a_case_created_before_the_list_but_first_active_on_main_now_fails_until_linked(repo, server):
    _, b = history(repo)
    old = case("old", linked=0, createdAt="2026-01-01T00:00:00Z")
    server.world(b, cases=[case("c1"), old], active=["c1"])
    assert states(measure(repo, server, b)[1])["unlinked_tests"] == PASS  # stale: no result on main yet
    server.world(b, cases=[case("c1"), old])
    assert states(measure(repo, server, b)[1])["unlinked_tests"] == FAIL  # first active on main
    assert states(measure(repo, server, b)[1])["unlinked_tests"] == FAIL  # a rerun fails again
    server.world(b, cases=[case("c1"), dict(old, linkedRequirementCount=1)])
    assert states(measure(repo, server, b)[1])["unlinked_tests"] == PASS


def test_a_listed_case_passes_the_list_rule_and_still_counts_in_the_share(repo, server):
    cases = suite_cases("r", 100, 1, "tests/test_x.py")
    history(repo, [entry("r0", "helper", external=cases[0]["externalId"])])
    b = git(repo, "rev-parse", "HEAD")
    server.world(b, cases=cases)
    metric = measure(repo, server, b)[1]["metrics"]["unlinked_tests"]
    assert metric["state"] == PASS and metric["numbers"]["overall"]["unlinked"] == 1


@pytest.mark.parametrize("bad", [
    {"caseId": "r0", "externalId": "s:tests.test_x::r0", "category": "helper"},
    {"caseId": "r0", "externalId": "s:tests.test_x::r0", "category": "obsolete", "reason": "x"},
    {"caseId": "r0", "externalId": "s:tests.test_x::r0", "category": "helper", "reason": " "},
    {"caseId": "r0", "externalId": "s:tests.test_x::r0", "category": "helper", "reason": "x", "extra": "y"},
    {"caseId": "r0", "externalId": "s:tests.test_x::other", "category": "helper", "reason": "x"},
])
def test_a_malformed_or_mismatched_list_entry_fails(repo, server, bad):
    history(repo, [bad])
    b = git(repo, "rev-parse", "HEAD")
    server.world(b, cases=[case("r0", linked=0)] + suite_cases("r", 100, 0, "tests/test_x.py")[1:])
    metric = measure(repo, server, b)[1]["metrics"]["unlinked_tests"]
    assert metric["state"] == FAIL and any(LIST in item or "no-spec list" in item for item in metric["findings"])


@pytest.mark.parametrize("text", [None, "not json", json.dumps({"format": 2, "cases": []}),
                                  json.dumps({"format": 1, "cases": [entry("x"), entry("x")]})])
def test_a_missing_or_invalid_list_fails(repo, server, text):
    commit(repo, {"services/app.py": "a", "keep.txt": "k", **({LIST: text} if text else {})}, "A")
    b = git(repo, "rev-parse", "HEAD")
    server.world(b)
    metric = measure(repo, server, b)[1]["metrics"]["unlinked_tests"]
    assert metric["state"] == FAIL and metric["findings"][0].startswith(LIST)


def test_a_stale_unlinked_case_does_not_count_but_is_listed(repo, server):
    _, b = history(repo)
    server.world(b, cases=[case("c1"), case("gone", linked=0)], active=["c1"])
    metric = measure(repo, server, b)[1]["metrics"]["unlinked_tests"]
    assert metric["state"] == PASS and metric["numbers"]["overall"] == {"cases": 2, "active": 1, "stale": 1,
                                                                        "unlinked": 0, "share": 0.0}
    assert metric["stale"] == ["gone tests/test_x.py test gone"]


def test_an_unassigned_path_fails(repo, server):
    _, b = history(repo)
    server.world(b, cases=[case("c1"), case("odd", "tools/test_odd.py")])
    metric = measure(repo, server, b)[1]["metrics"]["unlinked_tests"]
    assert metric["state"] == FAIL and metric["findings"] == ["unassigned path: odd tools/test_odd.py test odd"]


def test_a_suite_with_rows_and_no_active_case_fails_as_not_reporting(repo, server):
    _, b = history(repo)
    server.world(b, cases=[case("c1"), case("go1", "whatsapp/whatsmeow/send_test.go")], active=["c1"])
    metric = measure(repo, server, b)[1]["metrics"]["unlinked_tests"]
    assert metric["state"] == FAIL and metric["findings"] == ["whatsmeow-go: not reporting (1 case rows, none active "
                                                              "in run 10)"]


@pytest.mark.parametrize(("path", "external", "name"), [
    ("tests/test_a.py", "", "root-python"),
    ("services/calendar/tests/test_a.py", "", "calendar"),
    ("services/calls/frontend/src/a.test.ts", "", "calls-frontend"),
    ("services/calls/tests/test_voice.py", "", "calls"),
    ("", "s:tests.test_voice::test_a", "calls"),
    ("", "s:tests.test_webrtc::test_a", "calls"),
    ("", "s:tests.test_other::test_a", "unassigned"),
    ("services/gateway/tests/test_a.py", "", "gateway"),
    ("whatsapp/whatsmeow/send_test.go", "", "whatsmeow-go"),
    ("send_test.go", "", "whatsmeow-go"),
    ("internal/send_test.go", "", "unassigned"),
    ("whatsapp/tests/a.test.mjs", "", "whatsapp-node"),
    ("packages/npm/test/client.test.js", "", "npm"),
    (".github/workflows/checks.yml", "", "ci"),
    ("test_history_plan.py", "", "unassigned"),
    ("services/messaging/tests/test_a.py", "", "unassigned"),
])
def test_each_case_belongs_to_one_fixed_suite(path, external, name):
    assert suite({"filePath": path, "externalId": external}) == name


# spec: ANYM-228
def test_wake_relay_cases_belong_to_the_npm_suite_that_runs_them():
    assert suite({"filePath": "packages/wake-relay/test/state.test.mjs", "externalId": ""}) == "npm"


# spec: ANYM-360
def test_shared_contract_package_cases_belong_to_their_own_suite():
    assert suite({"filePath": "packages/platform-contracts/tests/test_agent.py", "externalId": ""}) == "platform-contracts"


# Stale anchors.

ODD = ("services/café.py", "services/with space.py", "services/new\nline.py", 'services/quo"te.py')


def test_anchors_on_tracked_names_pass_and_moved_or_deleted_paths_are_listed(repo, server):
    commit(repo, {"services/app.py": "a", "services/old.py": "o", "services/deleted.py": "d", LIST: no_spec(),
                  **{name: "x" for name in ODD}}, "A")
    git(repo, "mv", "services/old.py", "services/moved.py")
    b = commit(repo, {"services/deleted.py": None}, "B")
    server.world(b, specs=[
        spec(1, paths=["services/app.py", "services/moved.py", *ODD]),
        spec(2, paths=["services/old.py", "services/deleted.py"]),
        spec(3, paths=["services/gone.py"], repository="github.com/GoSync-Inc/anymsg"),
        spec(4, paths=["services/gone-too.py"], repository=None),
        spec(5, paths=["services/elsewhere.py"], repository="github.com/example/other"),
    ])
    metric = measure(repo, server, b)[1]["metrics"]["stale_anchors"]
    assert metric["state"] == FAIL
    assert metric["findings"] == ["ANYM-2 source source-2-0: services/old.py",
                                  "ANYM-2 source source-2-1: services/deleted.py",
                                  "ANYM-3 source source-3-0: services/gone.py",
                                  "ANYM-4 source source-4-0: services/gone-too.py"]
    assert metric["numbers"] == {"anchors": 10, "stale": 4, "stale_paths": 4, "historical": 0}


def test_an_odd_untracked_name_is_escaped_for_display(repo, server):
    _, b = history(repo)
    server.world(b, specs=[spec(1, paths=["services/app.py", "services/fake\n- ANYM-9 forged.py"])])
    finding = measure(repo, server, b)[1]["metrics"]["stale_anchors"]["findings"]
    assert finding == ["ANYM-1 source source-1-1: services/fake\\n- ANYM-9 forged.py"]


def test_a_near_miss_name_is_not_tracked(repo, server):
    commit(repo, {"services/app.py": "a", LIST: no_spec(), "services/with space.py": "x"}, "A")
    b = git(repo, "rev-parse", "HEAD")
    server.world(b, specs=[spec(1, paths=["services/with  space.py", "services/app.py "])])
    assert measure(repo, server, b)[1]["metrics"]["stale_anchors"]["numbers"]["stale"] == 2


def test_a_repo_file_source_without_a_path_is_unknown(repo, server):
    _, b = history(repo)
    server.world(b, specs=[dict(spec(1), sources=[{"id": "s", "sourceType": "repo_file"}], sourceCount=1)])
    assert states(measure(repo, server, b)[1])["stale_anchors"] == UNKNOWN


TRACKED = {"id": "s1", "sourceType": "repo_file", "repoPath": "services/app.py"}


@pytest.mark.parametrize("sources", [
    [TRACKED, dict(TRACKED)],  # a repeated tracked source standing in for an omitted stale one
    [TRACKED, {"sourceType": "repo_file", "repoPath": "gone.py"}],
    [TRACKED, {"id": "", "sourceType": "repo_file", "repoPath": "gone.py"}],
    [TRACKED, {"id": "s2", "repoPath": "gone.py"}],
    [TRACKED, {"id": "s2", "sourceType": None, "repoPath": "gone.py"}],
    [TRACKED, {"id": "s2", "sourceType": "", "repoPath": "gone.py"}],
], ids=["duplicate", "missing-id", "empty-id", "missing-type", "null-type", "empty-type"])
def test_a_source_with_a_duplicate_or_missing_id_or_type_makes_both_spec_metrics_unknown(repo, server, sources):
    _, b = history(repo)
    server.world(b, specs=[dict(spec(1), sources=sources, sourceCount=len(sources))])
    data = measure(repo, server, b)[1]
    assert states(data)["stale_anchors"] == UNKNOWN and states(data)["done_open_checks"] == UNKNOWN


def test_long_stale_paths_sharing_a_display_prefix_count_separately_and_stay_whole_in_the_json(repo, server):
    _, b = history(repo)
    prefix = "services/" + "nested/" * 50
    long_paths = [prefix + "one.py", prefix + "two.py"]
    server.world(b, specs=[spec(1, paths=["services/app.py", *long_paths])])
    metric = measure(repo, server, b)[1]["metrics"]["stale_anchors"]
    assert metric["numbers"] == {"anchors": 3, "stale": 2, "stale_paths": 2, "historical": 0}
    assert metric["paths"] == long_paths and all(item.endswith("...") for item in metric["findings"])


# spec: ANYM-420
def test_a_router_case_with_a_bare_path_belongs_to_the_router_suite_and_a_whatsmeow_one_stays():
    router = "s:github.com/GoSync-Inc/deedkit/services/release-router::TestConfigCASRejectsStaleETagWithoutChangingRoutes"
    whatsmeow = "s:github.com/GoSync-Inc/anymsg/whatsapp/whatsmeow::TestNativeTextValidatesBeforeSend/empty"
    assert suite({"filePath": "router_test.go", "externalId": router}) == "release-router-go"
    assert suite({"filePath": "services/release-router/router_test.go", "externalId": ""}) == "release-router-go"
    assert suite({"filePath": "send_test.go", "externalId": whatsmeow}) == "whatsmeow-go"


# spec: ANYM-420
@pytest.mark.parametrize("path", ["tests/router_test.go", "services/calendar/router_test.go",
                                  "services/calls/frontend/router_test.go", "services/calls/router_test.go",
                                  "services/gateway/router_test.go", "whatsapp/whatsmeow/router_test.go"])
def test_a_router_signature_wins_over_any_path_rule_and_its_suite_still_fails_as_not_reporting(repo, server, path):
    router = "s:github.com/GoSync-Inc/deedkit/services/release-router::TestConfigCASRejectsStaleETagWithoutChangingRoutes"
    assert suite({"filePath": path, "externalId": router}) == "release-router-go"
    _, b = history(repo)
    server.world(b, cases=[case("c1"), case("go1", path, external=router)], active=["c1"])
    metric = measure(repo, server, b)[1]["metrics"]["unlinked_tests"]
    assert metric["state"] == FAIL and metric["findings"] == ["release-router-go: not reporting (1 case rows, none "
                                                              "active in run 10)"]


FORMER = "github.com/GoSync-Inc/anymsg"


def provenance(seq, path, repository=FORMER, metadata=None, twins=("services/messaging/{path}",)):
    """A spec as the 2026-10-16 reconciliation left it: a former-repository anchor with its provenance receipt and the
    moved path anchored again (the live listing shape of its 2026-10-16 records)."""
    metadata = {"historical_source_id": "179b9124-b71c-4be0-866a-b0382d97fdb9",
                "historical_metadata_json": '{"reverse_pr":{"status":"modified"}}'} if metadata is None else metadata
    sources = [{"id": f"source-{seq}-old", "sourceType": "repo_file", "repoPath": path, "repository": repository,
                "metadata": metadata}]
    sources += [{"id": f"source-{seq}-{index}", "sourceType": "repo_file", "repoPath": twin.format(path=path),
                 "repository": "github.com/GoSync-Inc/deedkit"} for index, twin in enumerate(twins)]
    return dict(spec(seq), sources=sources, sourceCount=len(sources))


# spec: ANYM-420
def test_a_historical_anchor_whose_moved_path_is_anchored_and_tracked_is_counted_historical_not_stale(repo, server):
    commit(repo, {"services/app.py": "a", LIST: no_spec(), "services/messaging/messaging_service/policy.py": "p"}, "A")
    b = git(repo, "rev-parse", "HEAD")
    server.world(b, specs=[spec(1, paths=["services/app.py"]), provenance(2, "messaging_service/policy.py")])
    data = measure(repo, server, b)[1]
    metric = data["metrics"]["stale_anchors"]
    assert metric["state"] == PASS and metric["findings"] == []
    assert metric["numbers"] == {"anchors": 3, "stale": 0, "stale_paths": 0, "historical": 1}
    assert metric["historical"] == ["ANYM-2 source source-2-old: messaging_service/policy.py"]
    assert any("1 historical anchor(s)" in line for line in health.render(data, 0))


# spec: ANYM-420
@pytest.mark.parametrize("change", [
    {"twins": ()},  # no moved twin on the spec
    {"twins": ("services/messaging/other/{path}",)},  # a twin elsewhere is no moved path
    {"twins": ("services/messaging/{path}x",)},  # the moved path is anchored but not tracked
    {"metadata": {}},  # no provenance receipt
    {"metadata": {"historical_source_id": ""}},
    {"metadata": {"historical_source_id": 7}},
    {"metadata": None, "repository": "github.com/GoSync-Inc/deedkit"},  # an anchor of this repository
    {"metadata": None, "repository": None},  # an anchor without a repository
], ids=["no-twin", "other-twin", "untracked-twin", "no-receipt", "empty-receipt", "typed-receipt", "this-repository",
        "no-repository"])
def test_an_untracked_anchor_without_provenance_and_a_tracked_moved_twin_is_still_stale(repo, server, change):
    commit(repo, {"services/app.py": "a", LIST: no_spec(), "services/messaging/messaging_service/policy.py": "p",
                  "services/messaging/other/messaging_service/policy.py": "o"}, "A")
    b = git(repo, "rev-parse", "HEAD")
    options = dict(change)
    if options.get("metadata", {}) is None:
        options["metadata"] = {"historical_source_id": "179b9124-b71c-4be0-866a-b0382d97fdb9"}
    server.world(b, specs=[spec(1, paths=["services/app.py"]), provenance(2, "messaging_service/policy.py", **options)])
    metric = measure(repo, server, b)[1]["metrics"]["stale_anchors"]
    assert metric["state"] == FAIL and metric["numbers"]["historical"] == 0
    # An untracked twin is listed as stale too.
    assert metric["findings"][0] == "ANYM-2 source source-2-old: messaging_service/policy.py"


# spec: ANYM-420
def test_a_moved_twin_on_another_spec_does_not_make_an_anchor_historical(repo, server):
    commit(repo, {"services/app.py": "a", LIST: no_spec(), "services/messaging/messaging_service/policy.py": "p"}, "A")
    b = git(repo, "rev-parse", "HEAD")
    server.world(b, specs=[spec(1, paths=["services/app.py", "services/messaging/messaging_service/policy.py"]),
                           provenance(2, "messaging_service/policy.py", twins=())])
    metric = measure(repo, server, b)[1]["metrics"]["stale_anchors"]
    assert metric["state"] == FAIL and metric["numbers"]["stale"] == 1 and metric["numbers"]["historical"] == 0


# Drift.

def test_a_slow_drift_run_passes_once_the_watermark_reaches_target(repo, server):
    a, b = history(repo)
    server.world(b, watermarks=[a, a, a, b])
    clock = Clock()
    code, data = measure(repo, server, b, clock=clock)
    assert data["metrics"]["drift"]["state"] == PASS and clock.sleeps == 3


def test_a_watermark_beyond_target_passes_after_fetching_main(repo, server):
    _, b = history(repo)
    clone = repo.parent / "clone"
    subprocess.run(["git", "clone", "-q", str(repo.parent / "origin.git"), str(clone)], check=True)
    c = commit(repo, {"services/app.py": "c"}, "C")  # merged after the clone's checkout
    server.world(b, watermarks=[c])
    assert states(measure(clone, server, b)[1])["drift"] == UNKNOWN  # read-only: the clone does not know C
    code, data = measure(clone, server, b, fetch=True)
    assert data["metrics"]["drift"]["state"] == PASS
    assert data["metrics"]["drift"]["numbers"]["after_target"] == {"head": c, "commits": 1, "covered": True}


def test_a_watermark_behind_target_fails_at_the_deadline_and_later_merges_do_not_decide(repo, server):
    a, b = history(repo)
    c = commit(repo, {"services/app.py": "c"}, "C")
    server.world(b, watermarks=[a])
    clock = Clock()
    code, data = measure(repo, server, b, clock=clock)
    drift = data["metrics"]["drift"]
    assert drift["state"] == FAIL and drift["numbers"]["behind"] == 1
    assert health.DRIFT_WAIT <= clock.now < health.DRIFT_WAIT + health.POLL + 1
    assert drift["numbers"]["after_target"] == {"head": c, "commits": 1, "covered": False}
    assert "After TARGET (context only, never decides the result): 1 commit(s)" in "\n".join(health.render(data, code))


def test_a_divergent_watermark_is_unknown(repo, server):
    a, b = history(repo)
    git(repo, "checkout", "-q", "-b", "side", a)
    side = commit(repo, {"services/side.py": "s"}, "S", push=False)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "M", "side")
    git(repo, "push", "-q", "origin", "main")  # S is on main's history, but neither it nor B descends from the other
    server.world(b, watermarks=[side])
    drift = measure(repo, server, b)[1]["metrics"]["drift"]
    assert drift["state"] == UNKNOWN and drift["reason"] == f"the watermark {side} and TARGET have diverged"


def test_a_watermark_off_main_is_unknown(repo, server):
    a, b = history(repo)
    git(repo, "checkout", "-q", "-b", "side", a)
    side = commit(repo, {"services/side.py": "s"}, "S", push=False)
    server.world(b, watermarks=[side])
    drift = measure(repo, server, b)[1]["metrics"]["drift"]
    assert drift["state"] == UNKNOWN and drift["reason"] == f"the watermark {side} is not on main's history"


@pytest.mark.parametrize("values", [dict(repository="github.com/example/other"), dict(watermarks=["not-a-sha"]),
                                    dict(fault={"drift status": "exit"})])
def test_an_unreadable_drift_status_is_unknown(repo, server, values):
    _, b = history(repo)
    server.world(b, **values)
    assert states(measure(repo, server, b)[1])["drift"] == UNKNOWN


def test_a_target_off_main_makes_every_metric_unknown(repo, server):
    a, _ = history(repo)
    git(repo, "checkout", "-q", "-b", "feature", a)
    feature = commit(repo, {"services/f.py": "f"}, "F", push=False)
    server.world(feature)
    code, data = measure(repo, server, feature)
    assert code == 2 and set(states(data).values()) == {UNKNOWN}


# Done specs with open checks.

@pytest.mark.parametrize(("specs", "state", "findings", "warnings"), [
    ([spec(2, "Done", "Evidence:\n- [open: live] recheck on the installation")], FAIL, 1, 0),
    ([spec(2, "Done", "- measured latency [open: measured]")], FAIL, 1, 0),
    ([spec(2, "Review", "- [open: live] recheck on the installation")], PASS, 0, 0),
    ([spec(2, "Done", "Delivered (merged source; live recheck pending)")], PASS, 0, 1),
    ([spec(2, "Done", "Not run: the live preview")], PASS, 0, 1),
    ([spec(2, "Done", "Evidence: live recheck passed 2026-10-19, run 600")], PASS, 0, 0),
])
def test_done_specs_with_open_check_markers(repo, server, specs, state, findings, warnings):
    _, b = history(repo)
    server.world(b, specs=[spec(1, paths=["services/app.py"]), *specs])
    metric = measure(repo, server, b)[1]["metrics"]["done_open_checks"]
    assert (metric["state"], len(metric["findings"]), len(metric["warnings"])) == (state, findings, warnings)


def test_done_spec_text_in_code_documents_a_marker_without_being_one(repo, server):
    _, b = history(repo)
    documented = "\n".join([
        "Convention: an open check is written `[open: live]` or ``[open: measured]``.",
        "```",
        "- [open: live] recheck on the installation",
        "Not run: the live preview",
        "```",
        "~~~~text",
        "```",
        "- [open: measured] still inside the longer fence",
        "~~~~",
        "A span across lines: `see",
        "[open: live]` for the syntax.",
        "The recheck remains open `elsewhere`.",
    ])
    real = "Delivered.\nUse `code` first, then:\n- recheck on the installation [open: live]\n```\nfenced\n```"
    server.world(b, specs=[spec(1, paths=["services/app.py"]), spec(2, "Done", documented), spec(3, "Done", real)])
    metric = measure(repo, server, b)[1]["metrics"]["done_open_checks"]
    assert metric["findings"] == ["ANYM-3 Spec 3: - recheck on the installation [open: live]"]
    assert [item.split(":")[0] for item in metric["warnings"]] == ["ANYM-2 Spec 2"]
    assert (metric["state"], metric["numbers"]["marked"]) == (FAIL, 1)


def test_done_spec_text_only_in_code_passes(repo, server):
    _, b = history(repo)
    only_code = "Marker syntax: `[open: live]`, `[open: measured]`; phrases: `Not run:` and ``live recheck pending``."
    server.world(b, specs=[spec(1, paths=["services/app.py"]), spec(2, "Done", only_code)])
    metric = measure(repo, server, b)[1]["metrics"]["done_open_checks"]
    assert (metric["state"], metric["findings"], metric["warnings"]) == (PASS, [], [])


@pytest.mark.parametrize("content", [None, 5, ["- [open: live]"], "absent"])
def test_a_done_spec_without_readable_text_is_unknown_never_pass(repo, server, content):
    _, b = history(repo)
    done = spec(2, "Done")
    if content == "absent":
        del done["content"]
    else:
        done["content"] = content
    server.world(b, specs=[spec(1, paths=["services/app.py"]), done, spec(3, "Active", None)])
    code, data = measure(repo, server, b)
    assert code == 2 and states(data)["done_open_checks"] == UNKNOWN
    assert data["metrics"]["done_open_checks"]["reason"] == "tiden spec list has no readable text for Done spec ANYM-2"


def test_before_the_audit_date_is_set_the_done_metric_is_unknown(repo, server, monkeypatch):
    _, b = history(repo)
    server.world(b, specs=[spec(1, paths=["services/app.py"]), spec(2, "Done", "- [open: live]")])
    monkeypatch.setattr(health, "DONE_AUDIT", None)
    metric = measure(repo, server, b)[1]["metrics"]["done_open_checks"]
    assert metric["state"] == UNKNOWN and len(metric["findings"]) == 1


# The measuring command.

def test_the_command_reports_locally_read_only_with_the_same_result(repo, server, tmp_path):
    _, b = history(repo)
    server.world(b, specs=[spec(1, paths=["services/app.py", "gone.py"])])
    refs = git(repo, "for-each-ref")
    output = tmp_path / "health.json"
    completed = subprocess.run([sys.executable, str(ROOT / "scripts/tiden_health.py"), "--product-id", "product",
                                "--workspace-id", "workspace", "--target", b, "--root", str(repo), "--cli", server.cli,
                                "--output", str(output)], capture_output=True, text=True,
                               env={"PATH": "/usr/bin:/bin", "FAKE_TIDEN_STATE": str(tmp_path / "state.json"),
                                    "TIDEN_API_TOKEN": SECRET})
    assert completed.returncode == 1, completed.stderr
    data = json.loads(output.read_text())
    assert data["exit"] == 1 and data["target"] == b and data["metrics"]["stale_anchors"]["state"] == FAIL
    assert "## Tiden health: FAIL" in completed.stdout and SECRET not in completed.stdout + completed.stderr
    reads = {" ".join(call[:2]) for call in server.state()["calls"]}
    assert reads == {"drift status", "spec list", "branch list", "run list", "test list", "run results"}
    assert git(repo, "for-each-ref") == refs


# Runs the command with SYNC_BACKLOG_SINCE set to a commit of the temporary repository (ANYM-503).
SINCE_COMMAND = ("import sys; sys.path.insert(0, sys.argv.pop(1)); import scripts.tiden_health as health; "
                 "health.SYNC_BACKLOG_SINCE = sys.argv.pop(1); sys.exit(health.main())")


def script(since=None):
    return [sys.executable, str(ROOT / "scripts/tiden_health.py")] if since is None else \
        [sys.executable, "-c", SINCE_COMMAND, str(ROOT), since]


def command(repo, tmp_path, cli, *extra, env=None, since=None) -> tuple[subprocess.CompletedProcess, dict]:
    """The measuring command as the workflow runs it; it must always leave its JSON report behind."""
    output = tmp_path / "health.json"
    completed = subprocess.run([*script(since), "--root", str(repo), "--cli",
                                cli, "--output", str(output), *extra], capture_output=True, text=True,
                               env={"PATH": "/usr/bin:/bin", "FAKE_TIDEN_STATE": str(tmp_path / "state.json"),
                                    "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"), **(env or {})})
    assert output.exists(), completed.stderr
    return completed, json.loads(output.read_text())


@pytest.mark.parametrize(("extra", "env", "reason"), [
    (("--product-id", "product", "--workspace-id", "workspace", "--require-token"), {}, "TIDEN_API_TOKEN is not set"),
    (("--workspace-id", "workspace", "--require-token"), {"TIDEN_API_TOKEN": SECRET}, "no product or workspace ID"),
    (("--product-id", "product", "--require-token"), {"TIDEN_API_TOKEN": SECRET}, "no product or workspace ID"),
])
def test_missing_configuration_still_writes_a_full_report_with_every_metric_unknown(repo, server, tmp_path, extra,
                                                                                    env, reason):
    _, b = history(repo)
    server.world(b)
    completed, data = command(repo, tmp_path, server.cli, "--target", b, *extra, env=env)
    assert completed.returncode == 2, completed.stderr
    assert data["exit"] == 2 and data["target"] == b and set(states(data).values()) == {UNKNOWN}
    assert all(reason in entry["reason"] for entry in data["metrics"].values())
    assert "## Tiden health: UNKNOWN" in (tmp_path / "summary.md").read_text()
    assert server.state()["calls"] == []  # nothing is asked of Tiden without configuration


def test_an_unavailable_cli_leaves_every_metric_unknown_and_the_report_is_written(repo, tmp_path):
    _, b = history(repo)
    completed, data = command(repo, tmp_path, str(tmp_path / "missing-cli"), "--product-id", "product",
                              "--workspace-id", "workspace", "--target", b, "--require-token",
                              env={"TIDEN_API_TOKEN": SECRET})
    assert completed.returncode == 2 and "Traceback" not in completed.stderr
    assert data["target"] == b and set(states(data).values()) == {UNKNOWN}
    assert all("could not run" in entry["reason"] for entry in data["metrics"].values())
    assert SECRET not in completed.stdout + completed.stderr + json.dumps(data)


def test_a_git_fetch_timeout_leaves_drift_unknown_and_the_other_metrics_measured(repo, server, monkeypatch):
    _, b = history(repo)
    server.world(b, watermarks=["f" * 40])  # not in the clone, so the report fetches main
    real = subprocess.run

    def stalled(arguments, **options):
        if arguments[:1] == ["git"] and "fetch" in arguments:
            raise subprocess.TimeoutExpired(arguments, health.TIMEOUT, output=SECRET.encode())
        return real(arguments, **options)

    monkeypatch.setattr(health.subprocess, "run", stalled)
    code, data = measure(repo, server, b, fetch=True)
    assert code == 2
    assert states(data) == {"drift": UNKNOWN, "unlinked_tests": PASS, "stale_anchors": PASS, "done_open_checks": PASS}
    assert data["metrics"]["drift"]["reason"] == "git fetch timed out" and SECRET not in json.dumps(data)


def test_an_unexpected_defect_in_one_metric_leaves_only_it_unknown(repo, server, monkeypatch):
    _, b = history(repo)
    server.world(b)

    def broken(specs):
        raise KeyError(SECRET)

    monkeypatch.setattr(health, "done_metric", broken)
    code, data = measure(repo, server, b)
    assert code == 2 and states(data)["done_open_checks"] == UNKNOWN and states(data)["stale_anchors"] == PASS
    assert data["metrics"]["done_open_checks"]["reason"] == "unexpected KeyError while measuring"
    assert SECRET not in json.dumps(data)


def test_the_reviewed_no_spec_list_in_the_repository_is_valid():
    entries, errors = parse_no_spec_list((ROOT / LIST).read_bytes())
    assert errors == [] and entries
    assert all(value["externalId"].startswith("s:") for value in entries.values())


# Waiting for the sync B run of TARGET (ANYM-438).
FAKE_GH = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys, time
    path = os.environ["FAKE_GH_STATE"]
    state = json.load(open(path))
    args = sys.argv[1:]
    state.setdefault("calls", []).append(args)
    json.dump(state, open(path, "w"))
    mode = state.get("mode")
    if mode == "exit":
        sys.stderr.write("response body {SECRET}\\n"); print("{SECRET}"); sys.exit(1)
    if mode == "garbage":
        print("not json {SECRET}"); sys.exit(0)
    if mode == "scalar":
        print("[1]"); sys.exit(0)
    page = int(next(arg for arg in args if arg.startswith("page=")).split("=")[1])
    cutoff = next(arg for arg in args if arg.startswith("created=>=")).split(">=")[1]
    workflow = args[3].split("/")[-2]
    rows = [row for row in state.get("by_workflow", {{}}).get(workflow, state["rows"]) if row.get("created_at", "z") >= cutoff]
    chunk = rows[(page - 1) * 100:page * 100]
    body = {{"total_count": state.get("total", len(rows)), "workflow_runs": chunk}}
    if mode == "no-total":
        del body["total_count"]
    print(json.dumps(body))
""")
SINCE = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)
NO_SYNC = {"checked": True, "target": None, "run_id": None, "run_attempt": None, "url": None, "conclusion": None,
           "completed_at": None, "inventory_read_at": None}


def sync_row(run_id, sha, conclusion="success", updated="2026-10-19T10:00:00Z", attempt=1, status="completed",
             title=None, head="f" * 40, **fields):
    return {**fields, "id": run_id, "display_title": f"Tiden sync {sha}" if title is None else title, "head_sha": head,
            "status": status, "conclusion": conclusion if status == "completed" else None, "updated_at": updated,
            "run_attempt": attempt, "html_url": f"https://example.invalid/runs/{run_id}"}


class FakeActions:
    """Scripted `Actions.runs()` snapshots, the last one repeating; an exception in the script is raised."""

    def __init__(self, *snapshots, server=None, hook=None):
        self.snapshots, self.calls, self.seen, self.server, self.hook = snapshots, 0, [], server, hook
        self.since = None

    def runs(self, since, within=None):
        self.since, self.within = since, within
        if self.hook:
            self.hook(self.calls)
        if self.server and not self.calls:  # what had been read when the first lookup ran
            self.seen = [call[:2] for call in self.server.state()["calls"]]
        snapshot = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
        self.calls += 1
        if isinstance(snapshot, Exception):
            raise snapshot
        return snapshot


def synced(repo, server, rows_for, clock=None, hook=None, **world):
    """A healthy world at B and the `Actions` script `rows_for(a, b)` builds; returns (a, b, clock, actions, result)."""
    a, b = history(repo)
    server.world(b, **world)
    clock, actions = clock or Clock(), FakeActions(*rows_for(a, b), server=server, hook=hook)
    return a, b, clock, actions, measure(repo, server, b, clock=clock, actions=actions)


def decided(data, run_id, attempt=1, conclusion="success", updated="2026-10-19T10:00:00Z"):
    sync = data["sync"]
    assert (sync["checked"], sync["run_id"], sync["run_attempt"], sync["conclusion"], sync["completed_at"]) == \
        (True, run_id, attempt, conclusion, updated)
    assert sync["target"] == data["target"] and sync["url"] == f"https://example.invalid/runs/{run_id}"


# spec: ANYM-438
def test_a_successful_sync_of_target_is_read_before_the_inventory_and_recorded(repo, server):
    _, b, clock, actions, (code, data) = synced(
        repo, server, lambda a, b: [[sync_row(7, b, updated="2026-10-19T10:05:00Z", attempt=2)]])
    assert code == 0 and clock.sleeps == 0 and actions.calls == 2  # the look before and the look after the read
    decided(data, 7, attempt=2, updated="2026-10-19T10:05:00Z")
    assert ["run", "list"] in actions.seen and ["test", "list"] not in actions.seen
    assert data["sync"]["inventory_read_at"] and data["sync"]["inventory_read_at"] >= "2026-10-19T10:05:00+00:00"
    assert json.loads(json.dumps(data))["sync"] == data["sync"]


# spec: ANYM-438
def test_a_running_sync_is_waited_for_and_then_a_success_passes(repo, server):
    _, b, clock, actions, (code, data) = synced(
        repo, server, lambda a, b: [[sync_row(7, b, status="in_progress")], [sync_row(7, b, status="queued")],
                                    [sync_row(7, b, updated="2026-10-19T10:09:00Z")]])
    assert code == 0 and clock.sleeps == 2 and actions.calls == 4
    decided(data, 7, updated="2026-10-19T10:09:00Z")


# spec: ANYM-438
def test_a_sync_published_while_waiting_is_used(repo, server):
    _, b, clock, _, (code, data) = synced(repo, server, lambda a, b: [[], [], [sync_row(7, b)]])
    assert code == 0 and clock.sleeps == 2
    decided(data, 7)


# spec: ANYM-438
def test_a_failed_sync_followed_by_a_successful_recovery_run_passes(repo, server):
    _, b, _, _, (code, data) = synced(repo, server, lambda a, b: [[
        sync_row(1, b, conclusion="failure"), sync_row(2, b, updated="2026-10-19T10:05:00Z")]])
    assert code == 0
    decided(data, 2, updated="2026-10-19T10:05:00Z")


# spec: ANYM-438
def test_a_failure_after_a_success_decides_and_the_unlinked_metric_is_unknown_with_the_run(repo, server):
    _, b, _, _, (code, data) = synced(repo, server, lambda a, b: [[
        sync_row(1, b), sync_row(2, b, conclusion="failure", updated="2026-10-19T10:05:00Z")]])
    assert code == 2 and states(data)["unlinked_tests"] == UNKNOWN
    reason = data["metrics"]["unlinked_tests"]["reason"]
    assert "https://example.invalid/runs/2" in reason and "failure" in reason and "links" in reason
    decided(data, 2, conclusion="failure", updated="2026-10-19T10:05:00Z")
    assert data["sync"]["inventory_read_at"] is None


# spec: ANYM-438
def test_a_newer_running_run_after_a_success_is_waited_for(repo, server):
    _, b, clock, _, (code, data) = synced(repo, server, lambda a, b: [
        [sync_row(1, b), sync_row(2, b, status="in_progress")],
        [sync_row(1, b), sync_row(2, b, conclusion="failure", updated="2026-10-19T10:30:00Z")]])
    assert clock.sleeps == 1 and states(data)["unlinked_tests"] == UNKNOWN
    decided(data, 2, conclusion="failure", updated="2026-10-19T10:30:00Z")


# spec: ANYM-438
@pytest.mark.parametrize(("rows", "run_id", "attempt", "conclusion", "updated"), [
    # R1 created first and succeeded, R2 succeeded later, then R1 was rerun and failed: the failure is the latest.
    ([(1, "failure", "10:10", 2), (2, "success", "10:05", 1)], 1, 2, "failure", "10:10"),
    # R2 failed, then R1 (the older run ID) was rerun and succeeded: the success is the latest.
    ([(1, "success", "10:10", 2), (2, "failure", "10:05", 1)], 1, 2, "success", "10:10"),
    # R1 was rerun and failed again after R2 succeeded.
    ([(1, "failure", "10:20", 3), (2, "success", "10:15", 1)], 1, 3, "failure", "10:20"),
    # Equal completion times: the greater run ID decides, either way.
    ([(1, "failure", "10:05", 1), (2, "success", "10:05", 1)], 2, 1, "success", "10:05"),
    ([(2, "failure", "10:05", 1), (1, "success", "10:05", 1)], 2, 1, "failure", "10:05"),
])
def test_the_latest_completion_decides_even_for_a_rerun_of_an_older_run_id(repo, server, rows, run_id, attempt,
                                                                          conclusion, updated):
    day = "2026-10-19T{}:00Z"
    _, b, _, _, (code, data) = synced(repo, server, lambda a, b: [[
        sync_row(number, b, conclusion=kind, updated=day.format(moment), attempt=tries)
        for number, kind, moment, tries in rows]])
    assert (code == 0) == (conclusion == "success")
    decided(data, run_id, attempt, conclusion, day.format(updated))


# spec: ANYM-438
def test_a_tie_on_the_completion_time_and_the_run_id_is_broken_by_the_attempt():
    rows = [sync_row(5, "a" * 40, conclusion="failure", attempt=1), sync_row(5, "a" * 40, attempt=2)]
    assert health.decisive(rows)["run_attempt"] == 2 and health.decisive(rows[::-1])["run_attempt"] == 2


# spec: ANYM-438
def test_runs_titled_for_another_commit_are_ignored_even_when_their_head_is_target(repo, server):
    # A delayed CI of A completes after main moved to B: the sync run's head_sha is B, its title names A.
    a, b, clock, _, (code, data) = synced(repo, server, lambda a, b: [[
        sync_row(1, a, head=b), sync_row(2, a, status="in_progress", head=b),
        sync_row(3, b, updated="2026-10-19T09:00:00Z")]])
    assert code == 0 and clock.sleeps == 0
    decided(data, 3, updated="2026-10-19T09:00:00Z")


# spec: ANYM-438
def test_only_runs_of_other_commits_leave_the_unlinked_metric_unknown_at_the_deadline(repo, server):
    _, b, clock, _, (code, data) = synced(repo, server, lambda a, b: [[sync_row(1, a, head=b)]])
    assert code == 2 and states(data) == {"drift": PASS, "unlinked_tests": UNKNOWN, "stale_anchors": PASS,
                                          "done_open_checks": PASS}
    assert data["metrics"]["unlinked_tests"]["reason"] == "no finished Tiden sync run of TARGET"
    assert data["sync"] == NO_SYNC and health.TESTS_WAIT <= clock.now < health.TESTS_WAIT + health.POLL + 1


# spec: ANYM-438
def test_a_dispatch_for_a_started_from_main_b_is_found_for_target_a(repo, server):
    a, b = history(repo)
    server.world(a)
    actions = FakeActions([sync_row(1, a, head=b), sync_row(2, b, head=b, conclusion="failure")])
    code, data = measure(repo, server, a, actions=actions)
    assert code == 0 and data["target"] == a
    decided(data, 1)


# spec: ANYM-438
def test_a_skipped_run_is_ignored_and_only_skipped_runs_mean_no_sync(repo, server):
    _, b, clock, _, (code, data) = synced(repo, server, lambda a, b: [[
        sync_row(1, b), sync_row(2, b, conclusion="skipped", updated="2026-10-19T10:10:00Z")]])
    assert code == 0 and clock.sleeps == 0
    decided(data, 1)
    *_, (code, data) = synced(repo, server, lambda a, b: [[sync_row(2, b, conclusion="skipped")]], clock=Clock())
    assert code == 2 and data["metrics"]["unlinked_tests"]["reason"] == "no finished Tiden sync run of TARGET"
    assert data["sync"] == NO_SYNC


# spec: ANYM-438
def test_a_sync_still_running_at_the_deadline_is_unknown_never_the_earlier_success(repo, server):
    _, b, clock, _, (code, data) = synced(repo, server, lambda a, b: [[
        sync_row(1, b), sync_row(2, b, status="in_progress")]])
    assert code == 2 and states(data)["unlinked_tests"] == UNKNOWN and states(data)["drift"] == PASS
    assert data["metrics"]["unlinked_tests"]["reason"] == "no finished Tiden sync run of TARGET"
    assert data["sync"] == NO_SYNC and health.TESTS_WAIT <= clock.now < health.TESTS_WAIT + health.POLL + 1


# spec: ANYM-438
def test_the_sync_wait_shares_the_evidence_deadline_and_neither_wait_resets_the_other(repo, server):
    a, b = history(repo)
    # Drift waits 15 minutes, the evidence appears 5 minutes later, the sync never finishes.
    server.world(b, watermarks=[a] * 30 + [b], runs=[[]] * 10 + [[run(10, b)]])
    clock = Clock()
    code, data = measure(repo, server, b, clock=clock, actions=FakeActions([sync_row(1, b, status="queued")]))
    assert states(data) == {"drift": PASS, "unlinked_tests": UNKNOWN, "stale_anchors": PASS, "done_open_checks": PASS}
    assert health.TESTS_WAIT <= clock.now < health.TESTS_WAIT + health.POLL + 1
    assert (health.TESTS_WAIT + health.POLL) / 60 < workflow()["jobs"]["report"]["timeout-minutes"]


# spec: ANYM-438
@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required", "neutral", "stale"])
def test_any_other_conclusion_makes_only_the_unlinked_metric_unknown_and_the_others_stay_as_measured(
        repo, server, conclusion):
    a, b = history(repo)
    server.world(b, specs=[spec(1, paths=["services/app.py", "gone.py"])])
    baseline = measure(repo, server, b)[1]["metrics"]
    server.world(b, specs=[spec(1, paths=["services/app.py", "gone.py"])])
    code, data = measure(repo, server, b, actions=FakeActions([sync_row(4, b, conclusion=conclusion)]))
    assert code == 1  # the stale anchor still fails
    assert states(data)["unlinked_tests"] == UNKNOWN and conclusion in data["metrics"]["unlinked_tests"]["reason"]
    for name in ("drift", "stale_anchors", "done_open_checks"):
        assert data["metrics"][name] == baseline[name]
    decided(data, 4, conclusion=conclusion)
    summary = "\n".join(health.render(data, code))
    assert "https://example.invalid/runs/4" in summary and conclusion in summary


# spec: ANYM-438
@pytest.mark.parametrize(("script", "reason"), [
    (lambda b: [health.Unknown("gh api timed out")], "gh api timed out"),
    (lambda b: [[sync_row(1, b, status="queued")], health.Unknown("gh api exited 1")], "gh api exited 1"),
])
def test_a_lookup_error_makes_only_the_unlinked_metric_unknown_and_the_report_is_still_complete(repo, server, script,
                                                                                               reason):
    _, b, _, _, (code, data) = synced(repo, server, lambda a, b: script(b))
    assert code == 2 and states(data) == {"drift": PASS, "unlinked_tests": UNKNOWN, "stale_anchors": PASS,
                                          "done_open_checks": PASS}
    assert data["metrics"]["unlinked_tests"]["reason"] == reason
    assert data["sync"] == NO_SYNC and json.dumps(data)


# spec: ANYM-438
def test_the_summary_states_the_sync_that_was_waited_for(repo, server):
    _, b, _, _, (code, data) = synced(repo, server, lambda a, b: [[sync_row(7, b)]])
    text = "\n".join(health.render(data, code))
    assert "Tiden sync" in text and "https://example.invalid/runs/7" in text and "success" in text
    _, _, _, _, (code, data) = synced(repo, server, lambda a, b: [[]], clock=Clock())
    assert "no finished" in "\n".join(health.render(data, code))


# spec: ANYM-438
def test_without_a_sync_lookup_the_report_is_as_before_and_says_so(repo, server):
    _, b = history(repo)
    server.world(b)
    code, data = measure(repo, server, b)
    assert code == 0 and data["sync"] == {**NO_SYNC, "checked": False}


# spec: ANYM-438
def test_the_lookup_window_starts_one_day_before_the_commit_of_target(repo, server):
    _, b, _, actions, _ = synced(repo, server, lambda a, b: [[sync_row(1, b)]])
    committed = datetime.fromisoformat(git(repo, "show", "-s", "--format=%cI", b))
    assert actions.since == committed - timedelta(days=1)


# spec: ANYM-438
def test_a_rerun_after_the_sync_snapshot_but_during_the_inventory_read_is_waited_for_and_the_inventory_read_again(
        repo, server):
    _, b, clock, actions, (code, data) = synced(repo, server, lambda a, b: [
        [sync_row(1, b)], [sync_row(1, b, status="in_progress", attempt=2)],
        [sync_row(1, b, attempt=2, updated="2026-10-19T10:30:00Z")]])
    assert code == 0 and actions.calls == 4 and clock.sleeps == 1
    decided(data, 1, attempt=2, updated="2026-10-19T10:30:00Z")
    reads = [call for call in server.state()["calls"] if call[:2] == ["test", "list"]]
    assert len(reads) == 2  # the first inventory was discarded


# spec: ANYM-438
def test_a_new_sync_run_started_during_the_inventory_read_that_never_finishes_leaves_the_metric_unknown(repo, server):
    _, b, clock, _, (code, data) = synced(repo, server, lambda a, b: [
        [sync_row(1, b)], [sync_row(1, b), sync_row(2, b, status="queued")]])
    assert code == 2 and states(data)["unlinked_tests"] == UNKNOWN
    assert data["metrics"]["unlinked_tests"]["reason"] == "no finished Tiden sync run of TARGET"
    assert data["sync"] == NO_SYNC and clock.now >= health.TESTS_WAIT


# spec: ANYM-438
def test_a_sync_that_changes_during_the_inventory_read_at_the_deadline_is_unknown_and_its_record_is_dropped(
        repo, server):
    clock = Clock()

    def late(calls):
        if calls == 1:  # the look after the inventory read happens at the deadline
            clock.now = health.TESTS_WAIT

    _, b, _, _, (code, data) = synced(repo, server, lambda a, b: [
        [sync_row(1, b)], [sync_row(1, b, attempt=2, updated="2026-10-19T10:30:00Z")]], clock=clock, hook=late)
    assert code == 2 and states(data)["unlinked_tests"] == UNKNOWN
    assert "changed while the case inventory was read" in data["metrics"]["unlinked_tests"]["reason"]
    assert data["sync"] == NO_SYNC


# spec: ANYM-438
def test_the_pages_of_one_lookup_share_one_time_budget(monkeypatch):
    now, timeouts = [0.0], []

    def slow(arguments, **options):
        timeouts.append(options["timeout"])
        now[0] += 50
        page = int(next(arg for arg in arguments if arg.startswith("page=")).split("=")[1])
        rows = [sync_row(page * 1000 + index, "a" * 40) for index in range(100)]
        return subprocess.CompletedProcess(arguments, 0, json.dumps({"total_count": 1000, "workflow_runs": rows}), "")

    monkeypatch.setattr(health.subprocess, "run", slow)
    with pytest.raises(health.Unknown) as raised:
        health.Actions("o/r", clock=lambda: now[0]).runs(SINCE)
    assert str(raised.value) == "gh api timed out" and timeouts == [health.TIMEOUT, health.TIMEOUT - 50,
                                                                   health.TIMEOUT - 100]


# spec: ANYM-438
def test_a_lookup_that_would_end_after_the_report_deadline_is_unknown_and_the_report_is_complete(repo, server,
                                                                                               monkeypatch):
    _, b = history(repo)
    # The evidence appears at second 1770; the sync lookup may take 30 more seconds and each call would take 80.
    server.world(b, runs=[[]] * 59 + [[run(10, b)]])
    clock, timeouts, real = Clock(), [], subprocess.run

    def slow(arguments, **options):
        if arguments[:1] != ["gh"]:
            return real(arguments, **options)
        timeouts.append(options["timeout"])
        clock.now += min(80, options["timeout"])
        if options["timeout"] < 80:
            raise subprocess.TimeoutExpired(arguments, options["timeout"])
        return subprocess.CompletedProcess(arguments, 0, json.dumps({"total_count": 1, "workflow_runs": [
            sync_row(1, b)]}), "")

    monkeypatch.setattr(health.subprocess, "run", slow)
    code, data = measure(repo, server, b, clock=clock, actions=health.Actions("o/r", clock=clock))
    assert timeouts == [health.TESTS_WAIT - 59 * health.POLL]
    assert code == 2 and states(data) == {"drift": PASS, "unlinked_tests": UNKNOWN, "stale_anchors": PASS,
                                          "done_open_checks": PASS}
    assert data["metrics"]["unlinked_tests"]["reason"] == "gh api timed out" and data["sync"] == NO_SYNC


@pytest.fixture
def gh(tmp_path, monkeypatch):
    cli, path = tmp_path / "gh", tmp_path / "gh-state.json"
    cli.write_text(FAKE_GH)
    cli.chmod(0o755)
    monkeypatch.setenv("FAKE_GH_STATE", str(path))

    class Gh:
        def __init__(self):
            self.cli, self.state = str(cli), str(path)

        def set(self, **values):
            path.write_text(json.dumps(values))

        def calls(self):
            return json.loads(path.read_text()).get("calls", []) if path.exists() else []

    return Gh()


# spec: ANYM-438
def test_the_lookup_reads_the_sync_workflow_runs_of_main_over_every_page_with_get_queries(gh):
    gh.set(rows=[sync_row(number, "a" * 40) for number in range(1, 251)])
    rows = health.Actions("o/r", gh.cli).runs(SINCE)
    assert [row["id"] for row in rows] == list(range(1, 251))
    calls = gh.calls()
    assert [next(arg for arg in call if arg.startswith("page=")) for call in calls] == ["page=1", "page=2", "page=3"]
    first = calls[0]
    assert first[:4] == ["api", "--method", "GET", "repos/o/r/actions/workflows/tiden-sync.yml/runs"]
    assert "branch=main" in first and "per_page=100" in first
    assert "created=>=2026-10-19T09:00:00Z" in first and first.count("-f") == 2 and first.count("-F") == 2


# spec: ANYM-438
def test_runs_created_before_the_window_are_not_listed_and_a_rerun_of_a_run_inside_it_is(gh):
    gh.set(rows=[sync_row(1, "a" * 40, created_at="2026-10-19T08:59:59Z"),
                 sync_row(2, "a" * 40, created_at="2026-10-19T09:00:00Z", attempt=3, updated="2026-10-22T10:00:00Z")])
    assert [row["id"] for row in health.Actions("o/r", gh.cli).runs(SINCE)] == [2]


# spec: ANYM-438
@pytest.mark.parametrize(("values", "reason"), [
    ({"rows": [], "mode": "exit"}, "gh api exited 1"),
    ({"rows": [], "mode": "garbage"}, "gh api returned malformed JSON"),
    ({"rows": [], "mode": "scalar"}, "gh api returned no run page"),
    ({"rows": [], "mode": "no-total"}, "gh api returned no run page"),
    ({"rows": [sync_row(1, "a" * 40)], "total": 2}, "gh api pagination is incomplete"),
    ({"rows": [sync_row(1, "a" * 40), sync_row(1, "a" * 40)]}, "gh api returned a malformed or duplicate run"),
    ({"rows": [{"id": 1}]}, "gh api returned a malformed or duplicate run"),
    ({"rows": [{**sync_row(1, "a" * 40), "updated_at": "yesterday"}]}, "gh api returned a malformed or duplicate run"),
])
def test_a_lookup_that_is_not_a_complete_valid_run_list_is_a_named_error_without_the_response(gh, values, reason):
    gh.set(**values)
    with pytest.raises(health.Unknown) as raised:
        health.Actions("o/r", gh.cli).runs(SINCE)
    assert str(raised.value) == reason and SECRET not in str(raised.value)


# spec: ANYM-438
@pytest.mark.parametrize("repository", ["", "o", "o/r/x", "--paginate", "o/r?x=1", "o/ r"])
def test_a_repository_that_is_not_owner_and_name_is_a_lookup_error_before_any_call(gh, repository):
    gh.set(rows=[])
    with pytest.raises(health.Unknown, match="repository"):
        health.Actions(repository, gh.cli).runs(SINCE)
    assert gh.calls() == []


# spec: ANYM-438
def test_a_missing_gh_and_a_stalled_gh_are_named_errors(tmp_path, monkeypatch):
    with pytest.raises(health.Unknown) as raised:
        health.Actions("o/r", str(tmp_path / "missing-gh")).runs(SINCE)
    assert str(raised.value) == "gh api could not run"

    def stalled(arguments, **options):
        raise subprocess.TimeoutExpired(arguments, health.TIMEOUT, output=SECRET.encode())

    monkeypatch.setattr(health.subprocess, "run", stalled)
    with pytest.raises(health.Unknown) as raised:
        health.Actions("o/r").runs(SINCE)
    assert str(raised.value) == "gh api timed out" and SECRET not in str(raised.value)


# spec: ANYM-438
def test_a_lookup_failure_through_the_real_command_leaves_the_other_metrics_and_the_artifact(repo, server, gh):
    _, b = history(repo)
    server.world(b)
    gh.set(rows=[], mode="exit")
    actions = health.Actions("o/r", gh.cli)
    code, data = measure(repo, server, b, actions=actions)
    assert code == 2 and states(data)["unlinked_tests"] == UNKNOWN and data["sync"] == NO_SYNC
    assert data["metrics"]["unlinked_tests"]["reason"] == "gh api exited 1" and SECRET not in json.dumps(data)


# spec: ANYM-438
def test_the_command_takes_the_repository_for_the_sync_lookup_and_an_empty_one_is_a_lookup_error(repo, server, gh,
                                                                                                 tmp_path):
    a, b = history(repo)
    server.world(b)
    common = ("--product-id", "product", "--workspace-id", "workspace", "--target", b, "--require-token", "--gh",
              gh.cli)
    gh.set(rows=[sync_row(3, b)])
    completed, data = command(repo, tmp_path, server.cli, *common, "--sync-repository", "o/r",
                              env={"TIDEN_API_TOKEN": SECRET, "FAKE_GH_STATE": gh.state}, since=a)
    assert completed.returncode == 0, completed.stderr
    decided(data, 3)
    # Before and after the inventory read; the sync backlog (ANYM-503) reads its own listing last.
    assert "Tiden sync" in completed.stdout and len(gh.calls()) == 3
    assert data["metrics"].pop("sync_backlog")["state"] == PASS
    server.world(b)
    gh.set(rows=[sync_row(3, b)])
    completed, data = command(repo, tmp_path, server.cli, *common, "--sync-repository", "",
                              env={"TIDEN_API_TOKEN": SECRET, "FAKE_GH_STATE": gh.state}, since=a)
    assert completed.returncode == 2 and data["sync"] == NO_SYNC and gh.calls() == []
    assert data["metrics"].pop("sync_backlog")["state"] == UNKNOWN
    assert states(data) == {"drift": PASS, "unlinked_tests": UNKNOWN, "stale_anchors": PASS, "done_open_checks": PASS}


# The sync backlog of earlier main commits (ANYM-503).
START = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
SHA_C = "c" * 40


def ago(minutes):
    return (START - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync_attempt(run_id, sha, conclusion="success", minutes=20, status="completed", attempt=1):
    """A `Tiden sync` run as GitHub lists it: its latest attempt started and last updated MINUTES before START."""
    return sync_row(run_id, sha, conclusion, ago(minutes), attempt, status, run_started_at=ago(minutes),
                    created_at=ago(minutes + 5))


def ci_attempt(run_id, sha, conclusion="success", minutes=40, status="completed", started=None, created=None,
               event="push", attempt=1):
    return {**sync_row(run_id, sha, conclusion, ago(minutes), attempt, status, title="CI", head=sha),
            "event": event, "run_started_at": ago(started if started is not None else minutes + 10),
            "created_at": ago(created if created is not None else (started or minutes) + 10)}


def decide(syncs=(), ci=(), committed=120):
    state, reason, url = health.backlog_decision(SHA_C, START - timedelta(minutes=committed), list(syncs), list(ci),
                                                 START)
    return state, reason, url


# spec: ANYM-503
@pytest.mark.parametrize(("syncs", "ci", "committed", "state", "reason"), [
    pytest.param([sync_attempt(1, SHA_C, "failure")], [ci_attempt(9, SHA_C)], 120, "unsynced",
                 "last sync failure; CI push run success at", id="failed-only"),
    pytest.param([sync_attempt(1, SHA_C, "cancelled")], [ci_attempt(9, SHA_C)], 120, "unsynced",
                 "last sync cancelled", id="cancelled-only"),
    pytest.param([], [ci_attempt(9, SHA_C)], 120, "unsynced", "no sync run; CI push run success", id="no-run"),
    pytest.param([sync_attempt(1, SHA_C, "failure", 50), sync_attempt(2, SHA_C, "success", 30)], [], 120, "synced",
                 "", id="success-after-failure"),
    pytest.param([sync_attempt(1, SHA_C, "failure", 35, attempt=2)], [ci_attempt(9, SHA_C, minutes=60)], 120,
                 "unsynced", "last sync failure", id="success-then-failed-rerun"),
    pytest.param([sync_attempt(1, SHA_C, status="queued", minutes=10)], [], 120, "pending", "Tiden sync run queued",
                 id="queued"),
    pytest.param([sync_attempt(1, SHA_C, status="waiting", minutes=46)], [ci_attempt(9, SHA_C)], 120, "unsynced",
                 "stuck waiting", id="waiting-past-45-min"),
    pytest.param([sync_attempt(1, SHA_C, "failure", 25)], [ci_attempt(9, SHA_C, minutes=29)], 120, "pending",
                 "CI push run completed less than 30 min ago", id="inside-grace"),
    pytest.param([sync_attempt(1, SHA_C, "failure", 25)], [ci_attempt(9, SHA_C, minutes=30)], 120, "unsynced",
                 "last sync failure", id="grace-expired"),
    pytest.param([], [ci_attempt(8, SHA_C, minutes=10, created=30, attempt=2), ci_attempt(9, SHA_C, minutes=200,
                                                                                         created=20)],
                 240, "unsynced", "CI push run success at " + ago(200), id="ci-rerun-newest-run-decides"),
    pytest.param([], [ci_attempt(9, SHA_C, minutes=10, created=300, attempt=2)], 400, "pending",
                 "less than 30 min", id="ci-rerun-latest-attempt-decides"),
    pytest.param([], [], 59, "pending", "no CI push run yet", id="no-ci-59-min"),
    pytest.param([], [], 61, "unsynced", "no sync run; no CI push run", id="no-ci-61-min"),
    pytest.param([], [ci_attempt(9, SHA_C, status="in_progress", minutes=1, started=89)], 120, "pending",
                 "CI push run in_progress", id="ci-running-89-min"),
    pytest.param([], [ci_attempt(9, SHA_C, status="in_progress", minutes=1, started=91)], 120, "unsynced",
                 "CI attempt running over 90 min", id="ci-running-91-min"),
    pytest.param([sync_attempt(1, SHA_C, "skipped", 50)], [ci_attempt(9, SHA_C)], 120, "unsynced",
                 "no sync run", id="skipped-only-is-no-run"),
    pytest.param([], [ci_attempt(9, SHA_C, event="pull_request")], 61, "unsynced", "no CI push run",
                 id="a-pull-request-ci-run-is-not-evidence"),
])
def test_the_sync_backlog_decides_each_commit_by_its_latest_attempts(syncs, ci, committed, state, reason):
    found, why, url = decide(syncs, ci, committed)
    assert found == state and reason in why
    kept = [run for run in syncs if run["conclusion"] != "skipped"]
    assert url == (max(kept, key=lambda run: run["updated_at"])["html_url"] if kept else None)


def dated(repo, message, when):
    """A main commit with its committer date at WHEN, pushed."""
    (repo / "log.txt").write_text(message)
    git(repo, "add", "log.txt")
    stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True,
                   env={**os.environ, "GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp})
    git(repo, "push", "-q", "origin", "main")
    return git(repo, "rev-parse", "HEAD")


class FakeBacklog:
    """The sync backlog's own listings: rows per workflow, or an exception; records each call."""

    def __init__(self, syncs=(), ci=(), error=None, elapse=0.0):
        self.rows, self.error, self.calls = {"tiden-sync.yml": list(syncs), "ci.yml": list(ci)}, error, []
        self.now, self.elapse = 1000.0, elapse

    def clock(self):
        return self.now

    def listing(self, since, end, workflow="tiden-sync.yml", filters=()):
        self.calls.append((workflow, since, end, filters))
        self.now += self.elapse  # each listing returns ELAPSE seconds later
        if self.error:
            raise self.error
        return self.rows[workflow]


# spec: ANYM-503
def test_the_backlog_scope_starts_after_the_rollout_commit_and_within_seven_days(repo, monkeypatch):
    before = dated(repo, "before", START - timedelta(days=9))
    boundary = dated(repo, "boundary", START - timedelta(days=9))
    old = dated(repo, "old child", START - timedelta(days=8))
    child = dated(repo, "child", START - timedelta(days=6))
    head = dated(repo, "head", START - timedelta(hours=1))
    monkeypatch.setattr(health, "SYNC_BACKLOG_SINCE", boundary)
    commits, older = health.backlog_scope(repo, head, START)
    assert [sha for sha, _ in commits] == [head, child] and older == 1
    assert before not in dict(commits) and boundary not in dict(commits) and old not in dict(commits)
    monkeypatch.setattr(health, "SYNC_BACKLOG_SINCE", child)
    assert [sha for sha, _ in health.backlog_scope(repo, head, START)[0]] == [head]  # the boundary's child is checked
    monkeypatch.setattr(health, "SYNC_BACKLOG_SINCE", "f" * 40)
    with pytest.raises(health.Unknown, match="rollout commit"):
        health.backlog_scope(repo, head, START)
    monkeypatch.setattr(health, "SYNC_BACKLOG_SINCE", boundary)
    entry = health.backlog_metric(repo, FakeBacklog(syncs=[sync_attempt(1, head), sync_attempt(2, child)]), head, START)
    assert entry["reason"] == (f"0 unsynced, 0 pending, 2 synced of 2 main commit(s) after {boundary[:12]}; not checked: "
                               f"{boundary[:12]} and earlier, 1 later commit(s) older than 7 days")
    assert old not in {row["sha"] for row in entry["commits"]} and entry["numbers"]["not_checked"] == 1
    entry = health.backlog_metric(repo, FakeBacklog(), boundary, START)  # TARGET is the rollout commit itself
    assert entry["state"] == PASS and entry["commits"] == [] and entry["reason"] == (
        f"no main commit in scope; not checked: {boundary[:12]} and earlier, 0 later commit(s) older than 7 days")


# spec: ANYM-503
def test_pending_and_unsynced_commits_together_fail_and_both_are_listed_with_dates_out_of_order(repo, server,
                                                                                               monkeypatch):
    history(repo)
    boundary = dated(repo, "boundary", START - timedelta(days=2))
    lost = dated(repo, "lost", START - timedelta(minutes=10))  # committed after its first-parent child
    fresh = dated(repo, "fresh", START - timedelta(hours=5))
    done = dated(repo, "done", START - timedelta(hours=3))
    monkeypatch.setattr(health, "SYNC_BACKLOG_SINCE", boundary)
    server.world(done)
    backlog = FakeBacklog(syncs=[sync_attempt(1, lost, "failure", 100), sync_attempt(2, done),
                                 sync_attempt(3, fresh, status="in_progress", minutes=5)],
                          ci=[ci_attempt(9, lost, minutes=100)])
    clock = Clock()
    data = report(repo, Tiden(server.cli, "product", "workspace"), done, "refs/remotes/origin/main", False, clock,
                  clock.sleep, backlog=backlog, now=lambda: START)
    code = outcome(data)
    entry = data["metrics"].pop("sync_backlog")
    assert code == 1 and set(states(data).values()) == {PASS}
    assert entry["state"] == FAIL and [row["sha"] for row in entry["commits"]] == [done, fresh, lost]
    assert [row["state"] for row in entry["commits"]] == ["synced", "pending", "unsynced"]
    assert entry["findings"] == [f"{lost}: last sync failure; CI push run success at {ago(100)}; last run "
                                 f"https://example.invalid/runs/1; gh workflow run tiden-sync.yml --ref main -f sha={lost}"]
    assert entry["warnings"] == [f"{fresh}: Tiden sync run in_progress"]
    assert entry["numbers"] == {"since": boundary, "head": done, "in_scope": 3, "not_checked": 0, "synced": 1,
                                     "pending": 1, "unsynced": 1}
    data["metrics"]["sync_backlog"] = entry
    text = "\n".join(health.render(data, code))
    assert "| sync_backlog | fail | 1 unsynced, 1 pending, 1 synced of 3 main commit(s)" in text
    assert f"gh workflow run tiden-sync.yml --ref main -f sha={lost}" in text and "### sync_backlog: 1 warning(s)" in text
    lower = START - timedelta(hours=5) - timedelta(days=1)
    assert [(call[0], call[1], call[2], call[3]) for call in backlog.calls] == [
        ("tiden-sync.yml", lower, 1120.0, ()), ("ci.yml", lower, 1120.0, ("event=push",))]  # one 120 s budget
    assert health.backlog_lines(entry) == [
        f"synced {done}", f"pending {fresh} Tiden sync run in_progress",
        f"unsynced {lost} last sync failure; CI push run success at {ago(100)}; last run "
        f"https://example.invalid/runs/1; gh workflow run tiden-sync.yml --ref main -f sha={lost}"]


# spec: ANYM-503
def test_a_pending_commit_alone_passes_with_a_warning_and_an_all_synced_backlog_skips_the_ci_listing(repo, server,
                                                                                                     monkeypatch):
    boundary = dated(repo, "boundary", START - timedelta(days=1))
    head = dated(repo, "head", START - timedelta(minutes=20))
    monkeypatch.setattr(health, "SYNC_BACKLOG_SINCE", boundary)
    entry = health.backlog_metric(repo, FakeBacklog(ci=[ci_attempt(9, head, minutes=5)]), head, START)
    assert entry["state"] == PASS and entry["warnings"] == [f"{head}: CI push run completed less than 30 min ago"]
    backlog = FakeBacklog(syncs=[sync_attempt(1, head)])
    entry = health.backlog_metric(repo, backlog, head, START)
    assert entry["state"] == PASS and not entry["warnings"] and [call[0] for call in backlog.calls] == ["tiden-sync.yml"]


# spec: ANYM-503
@pytest.mark.parametrize(("error", "reason"), [
    (health.Unknown("gh api pagination is incomplete"), "gh api pagination is incomplete"),
    (health.Unknown("gh api exited 1"), "gh api exited 1"),
    (health.Unknown("gh api timed out"), "gh api timed out"),
    (KeyError(SECRET), "unexpected KeyError while measuring"),
])
def test_a_backlog_listing_error_makes_only_the_backlog_unknown(repo, server, monkeypatch, error, reason):
    a, b = history(repo)
    monkeypatch.setattr(health, "SYNC_BACKLOG_SINCE", a)
    server.world(b)
    _, before = measure(repo, server, b)
    clock = Clock()
    data = report(repo, Tiden(server.cli, "product", "workspace"), b, "refs/remotes/origin/main", False, clock,
                  clock.sleep, backlog=FakeBacklog(error=error), now=lambda: datetime.now(timezone.utc))
    entry = data["metrics"].pop("sync_backlog")
    assert entry["state"] == UNKNOWN and entry["reason"] == reason and SECRET not in json.dumps(entry)
    assert outcome({"metrics": {"sync_backlog": entry}}) == 2
    assert {name: (value["state"], value["reason"]) for name, value in data["metrics"].items()} == \
        {name: (value["state"], value["reason"]) for name, value in before["metrics"].items()}


# spec: ANYM-503
def test_a_backlog_listing_that_returns_after_its_120_s_budget_makes_only_the_backlog_unknown(repo, server,
                                                                                               monkeypatch):
    a = dated(repo, "boundary", START - timedelta(days=1))
    b = dated(repo, "head", START - timedelta(hours=2))
    monkeypatch.setattr(health, "SYNC_BACKLOG_SINCE", a)
    server.world(b)
    _, before = measure(repo, server, b)
    now = START
    rows = {"syncs": [sync_attempt(1, b, "failure")], "ci": [ci_attempt(9, b)]}
    assert health.backlog_metric(repo, FakeBacklog(**rows, elapse=59), b, now)["state"] == FAIL  # 118 s: in budget
    clock = Clock()
    data = report(repo, Tiden(server.cli, "product", "workspace"), b, "refs/remotes/origin/main", False, clock,
                  clock.sleep, backlog=FakeBacklog(**rows, elapse=60), now=lambda: now)  # the last page at 120 s
    entry = data["metrics"].pop("sync_backlog")
    assert (entry["state"], entry["reason"]) == (UNKNOWN, "gh api timed out")
    assert {name: (value["state"], value["reason"]) for name, value in data["metrics"].items()} == \
        {name: (value["state"], value["reason"]) for name, value in before["metrics"].items()}


# spec: ANYM-503
def test_a_truncated_backlog_listing_through_gh_is_unknown(repo, server, gh, monkeypatch):
    a, b = history(repo)
    monkeypatch.setattr(health, "SYNC_BACKLOG_SINCE", a)
    gh.set(rows=[sync_row(1, "a" * 40, created_at="2099-01-01T00:00:00Z")], total=2)
    with pytest.raises(health.Unknown, match="pagination is incomplete"):
        health.backlog_metric(repo, health.Actions("o/r", gh.cli), b, datetime.now(timezone.utc))


# spec: ANYM-503
def test_the_backlog_is_measured_without_test_evidence_and_without_tiden_configuration(repo, server, monkeypatch):
    a, b = history(repo)
    monkeypatch.setattr(health, "SYNC_BACKLOG_SINCE", a)
    server.world(b, runs=[[]])  # no Tiden run of TARGET: the unlinked tests stay unknown after the bound
    clock = Clock()
    backlog = FakeBacklog(syncs=[sync_attempt(1, b)])
    now = datetime.now(timezone.utc)
    data = report(repo, Tiden(server.cli, "product", "workspace"), b, "refs/remotes/origin/main", False, clock,
                  clock.sleep, backlog=backlog, now=lambda: now)
    assert states(data)["unlinked_tests"] == UNKNOWN and data["metrics"]["sync_backlog"]["state"] == PASS
    server.set(calls=[])
    data = report(repo, Tiden(server.cli, "product", "workspace"), b, "refs/remotes/origin/main", False, clock,
                  clock.sleep, unconfigured="Tiden is not configured", backlog=backlog, now=lambda: now)
    assert data["metrics"].pop("sync_backlog")["state"] == PASS and set(states(data).values()) == {UNKNOWN}
    assert server.state()["calls"] == []


def watcher(repo, gh, since, *extra):
    return subprocess.run([*script(since), "--root", str(repo), "--gh", gh.cli, "--sync-backlog-only", *extra],
                          capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "FAKE_GH_STATE": gh.state})


# spec: ANYM-503
def test_the_watcher_mode_fetches_main_needs_no_tiden_and_exits_0_1_or_2(repo, gh):
    a, b = history(repo)
    git(repo, "update-ref", "refs/remotes/origin/main", a)  # stale until the watcher fetches main
    now = datetime.now(timezone.utc)
    stamp = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh.set(by_workflow={"tiden-sync.yml": [sync_row(5, b, updated=stamp, created_at=stamp, run_started_at=stamp)]},
           rows=[])
    completed = watcher(repo, gh, a, "--sync-repository", "o/r")
    assert (completed.returncode, completed.stdout, completed.stderr) == (0, f"synced {b}\n", "")
    gh.set(by_workflow={"tiden-sync.yml": [sync_row(5, b, "failure", stamp, created_at=stamp, run_started_at=stamp)],
                        "ci.yml": [{**sync_row(9, b, updated=stamp, title="CI", head=b), "event": "push",
                                    "created_at": stamp, "run_started_at": stamp}]}, rows=[])
    completed = watcher(repo, gh, a, "--sync-repository", "o/r")
    assert completed.returncode == 1 and completed.stdout == (
        f"unsynced {b} last sync failure; CI push run success at {stamp}; last run https://example.invalid/runs/5; "
        f"gh workflow run tiden-sync.yml --ref main -f sha={b}\n")
    gh.set(rows=[], mode="exit")
    completed = watcher(repo, gh, a, "--sync-repository", "o/r")
    assert (completed.returncode, completed.stdout) == (2, "") and completed.stderr == "unknown: gh api exited 1\n"
    completed = watcher(repo, gh, a)
    assert completed.returncode == 2 and "needs --sync-repository" in completed.stderr
    assert SECRET not in completed.stdout + completed.stderr


# The workflow.

def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def test_the_workflow_runs_after_every_main_ci_run_and_on_dispatch_only():
    data = workflow()
    triggers = data.get("on", data.get(True))
    assert triggers == {"workflow_run": {"workflows": ["CI"], "types": ["completed"]}, "workflow_dispatch": None}
    assert data["permissions"] == {"contents": "read", "actions": "read"}  # ANYM-438: the sync run lookup, no write scope
    assert data["concurrency"] == {"group": "tiden-health", "cancel-in-progress": False}
    job = data["jobs"]["report"]
    condition = " ".join(job["if"].split())
    assert condition == ("github.ref == 'refs/heads/main' && (github.event_name == 'workflow_dispatch' || "
                         "(github.event.workflow_run.event == 'push' && github.event.workflow_run.head_branch == "
                         "'main'))")
    assert "permissions" not in job


def test_the_workflow_checks_out_main_without_credentials_and_pins_the_cli_as_ci():
    steps = workflow()["jobs"]["report"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"] == {"persist-credentials": False, "ref": "main", "fetch-depth": 0}
    install = next(step for step in steps if step.get("name") == "Install pinned Tiden CLI with integrity check")
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    for line in install["run"].splitlines()[:2]:
        assert line.strip() in ci
    assert "v0.41.0" in install["run"]
    assert not any(name in step.get("run", "") for step in steps for name in ("GITHUB_PATH", "GITHUB_ENV"))
    # The token reaches the report step alone; no step reads the secret otherwise.
    holders = [step["name"] for step in steps
               if "${{ secrets.TIDEN_API_TOKEN }}" in step.get("env", {}).values()]
    assert holders == ["Measure drift, unlinked tests, stale anchors and Done specs with open checks"]
    assert sum("secrets." in json.dumps(step) for step in steps) == 1
    report_step = next(step for step in steps if step.get("name") == holders[0])
    assert report_step["env"]["TIDEN_API_TOKEN"] == "${{ secrets.TIDEN_API_TOKEN }}"
    assert report_step["run"].startswith("python3 scripts/tiden_health.py --cli \"$RUNNER_TEMP/tiden-bin/tiden\" "
                                         "--require-token --fetch")
    # Missing configuration must not skip the report: every step after the guard runs, the upload even after a failure.
    assert all("if" not in step for step in steps[1:-1])
    upload = steps[-1]
    assert upload["uses"].startswith("actions/upload-artifact@") and upload["if"].startswith("always()")
    assert upload["with"]["name"] == "tiden-health" and upload["with"]["overwrite"] is True


# spec: ANYM-438
def test_the_job_token_reaches_the_report_step_alone_and_the_lookup_names_the_repository():
    steps = workflow()["jobs"]["report"]["steps"]
    holders = [step["name"] for step in steps if "github.token" in json.dumps(step)]
    assert holders == ["Measure drift, unlinked tests, stale anchors and Done specs with open checks"]
    report_step = next(step for step in steps if step.get("name") == holders[0])
    assert report_step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert report_step["run"].endswith('--fetch --sync-repository "$GITHUB_REPOSITORY" --output '
                                       '"$RUNNER_TEMP/tiden-health.json"')
    assert "GH_TOKEN" not in json.dumps(workflow().get("env", {})) and "env" not in workflow()["jobs"]["report"]
    assert workflow()["jobs"]["report"]["timeout-minutes"] == 45


def test_the_workflow_is_not_a_required_check():
    ruleset = (ROOT / "deploy/main-ruleset.json").read_text()
    assert "Tiden health" not in ruleset and "tiden-health" not in ruleset
    for path in (".github/workflows/ci.yml", ".github/workflows/checks.yml", "scripts/production.py"):
        text = (ROOT / path).read_text()
        assert "tiden-health" not in text and "tiden_health" not in text and "Tiden health" not in text


def guard(tmp_path, **env):
    script = workflow()["jobs"]["report"]["steps"][0]["run"]
    return subprocess.run(["bash", "-e", "-c", script], env={"PATH": "/usr/bin:/bin", **env},
                          capture_output=True).returncode


@pytest.mark.parametrize(("env", "allowed"), [
    (dict(REF="refs/heads/main", EVENT="workflow_run", UPSTREAM_EVENT="push", UPSTREAM_BRANCH="main"), True),
    (dict(REF="refs/heads/main", EVENT="workflow_dispatch"), True),
    (dict(REF="refs/heads/main", EVENT="workflow_run", UPSTREAM_EVENT="pull_request", UPSTREAM_BRANCH="main"), False),
    (dict(REF="refs/heads/main", EVENT="workflow_run", UPSTREAM_EVENT="push", UPSTREAM_BRANCH="feature"), False),
    (dict(REF="refs/heads/main", EVENT="workflow_run", UPSTREAM_EVENT="push", UPSTREAM_BRANCH="MAIN"), False),
    (dict(REF="refs/heads/main", EVENT="workflow_run", UPSTREAM_EVENT="merge_group", UPSTREAM_BRANCH="main"), False),
    (dict(REF="refs/heads/feature", EVENT="workflow_dispatch"), False),
    (dict(REF="refs/tags/main", EVENT="workflow_dispatch"), False),
    (dict(REF="refs/heads/main", EVENT="push"), False),
])
def test_the_shell_guard_admits_only_main_pushes_and_dispatch_from_main(tmp_path, env, allowed):
    assert (guard(tmp_path, **env) == 0) == allowed
