"""The receipt watcher decides LIVE, UNSETTLED, REFUSED, TIMEOUT or UNREACHABLE from production receipts, read-only
(ANYM-440)."""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import scripts.receipt_watch as watcher

SCRIPT = Path(watcher.__file__)
SECRET = "sha256:digest_sentinel_secret"
IDLE = {"present": False, "reason": None}


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="module")
def history(tmp_path_factory):
    """A linear main A <- B <- C and a side commit S that does not contain B."""
    root = tmp_path_factory.mktemp("history")
    git(root, "init", "-q", "-b", "main")
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
        git(root, "config", key, value)
    shas = {}
    for name in "ABC":
        (root / "f").write_text(name)
        git(root, "add", "f")
        git(root, "commit", "-q", "-m", name)
        shas[name] = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "-b", "side", shas["A"])
    (root / "f").write_text("S")
    git(root, "commit", "-q", "-am", "S")
    shas["S"] = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "main")
    return root, shas


def snap(plan=None, http=None, deploy=None, pause=None, unresolved=(), served=(), **extra):
    """A snapshot: plan/http/deploy are (sha, status[, reason]); pause is None, True or the reason text."""
    def row(value):
        return None if value is None else dict(zip(("sha", "status", "reason"), value))
    state = IDLE if pause is None else {"present": True, "reason": None if pause is True else pause}
    return {"plan_a": row(plan), "http": row(http), "deploy": row(deploy), "pause": state,
            "unresolved": list(unresolved), "served": list(served), **extra}


class Probe:
    def __init__(self, *answers):
        self.answers, self.timeouts = list(answers), []

    def __call__(self, host, timeout):
        self.timeouts.append(timeout)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def run(history, probe, sha="B", timeout=600, interval=60):
    root, shas = history
    lines, clock = [], Clock()
    code = watcher.watch(shas.get(sha, sha), "host", timeout, interval, out=lines.append, root=root, probe=probe,
                         clock=clock, sleep=clock.sleep)
    return code, lines, clock


def both(shas, name, plan="applied", http="serving", **extra):
    return snap(plan=(shas[name], plan), http=(shas[name], http), **extra)


# spec: ANYM-440
@pytest.mark.parametrize("name", ["B", "C"])
@pytest.mark.parametrize("http", ["serving", "serving-retirement-pending"])
def test_both_receipts_at_the_sha_or_a_descendant_are_live(history, name, http):
    shas = history[1]
    code, lines, _ = run(history, Probe(both(shas, name, http=http)))
    assert code == 0 and len(lines) == 1 and lines[0].startswith(f"LIVE {shas['B'][:12]} plan-a={shas[name][:12]}")


# spec: ANYM-440
@pytest.mark.parametrize("plan,http,status", [
    ("A", "C", "applied"),                  # plan A is older than the watched SHA
    ("C", "A", "applied"),                  # the HTTP release is older
    ("S", "S", "applied"),                  # a release that does not contain the SHA
    ("C", "C", "rolled-back"),
    ("C", "C", "unhealthy"),
])
def test_an_older_or_foreign_or_rolled_back_receipt_is_not_live(history, plan, http, status):
    shas = history[1]
    code, lines, _ = run(history, Probe(snap(plan=(shas[plan], status), http=(shas[http], "serving"))), timeout=120)
    assert code == 2 and lines[0].startswith("TIMEOUT") and "LIVE" not in lines[0]


# spec: ANYM-440
def test_an_unknown_sha_never_decides(history):
    unknown = "f" * 40
    code, lines, _ = run(history, Probe(snap(plan=(unknown, "applied"), http=(unknown, "serving"))), timeout=120)
    assert code == 2 and lines[0].startswith("TIMEOUT")


# spec: ANYM-440
@pytest.mark.parametrize("reason", [None, "", "halted by hand", "plan A was refused"])
def test_applied_receipts_with_a_pause_are_never_live(history, reason):
    shas = history[1]
    state = both(shas, "B")
    state["pause"] = {"present": True, "reason": reason}
    code, lines, _ = run(history, Probe(state))
    assert code == 1 and lines[0].startswith(f"UNSETTLED {shas['B'][:12]}") and "LIVE" not in lines[0]
    assert reason is None or reason in lines[0]


# spec: ANYM-440
@pytest.mark.parametrize("kind", ["apply", "rollback", "http"])
def test_an_unfinished_attempt_with_applied_receipts_is_unsettled_and_named(history, kind):
    shas = history[1]
    code, lines, _ = run(history, Probe(both(shas, "B", unresolved=[f"{shas['C']}:{kind}"])))
    assert code == 1 and lines[0].startswith("UNSETTLED") and f"{shas['C']}:{kind}" in lines[0]


# spec: ANYM-440
def test_a_serving_http_attempt_that_the_receipt_does_not_contain_is_unsettled(history):
    shas = history[1]
    for served, expected in (([shas["A"]], 0), ([shas["B"]], 0), ([shas["C"]], 1), ([shas["S"]], 1), (["nonsense"], 1)):
        code, lines, _ = run(history, Probe(both(shas, "B", served=served)))
        assert code == expected, served
        assert expected == 0 or "http-unpublished" in lines[0]


# spec: ANYM-440
def test_an_unstable_snapshot_gives_no_verdict_and_the_next_stable_one_does(history):
    shas = history[1]
    code, lines, _ = run(history, Probe({"unstable": True}, both(shas, "B")))
    assert code == 0 and lines[0].startswith("LIVE")
    code, lines, _ = run(history, Probe({"unstable": True}), timeout=120)
    assert code == 2 and "changed between two reads" in lines[0]


# spec: ANYM-440
@pytest.mark.parametrize("reason", ["plan A of x was refused", "", None])
def test_a_pause_with_x_not_reached_is_refused_even_without_receipts(history, reason):
    shas = history[1]
    for state in (snap(pause=reason if reason is not None else True),
                  snap(plan=(shas["A"], "applied"), http=(shas["A"], "serving"), pause=reason or True)):
        code, lines, _ = run(history, Probe(state))
        assert code == 1 and lines[0].startswith(f"REFUSED {shas['B'][:12]} deploys are paused")
        assert reason is None or reason in lines[0]


# spec: ANYM-440
def test_the_deploy_reason_is_used_when_the_pause_has_none(history):
    shas = history[1]
    state = snap(plan=(shas["A"], "applied"), http=(shas["A"], "serving"), pause=True,
                 deploy=(shas["C"], "halted", "plan A was refused"))
    assert "plan A was refused" in run(history, Probe(state))[1][0]


# spec: ANYM-440
@pytest.mark.parametrize("status", ["halted", "rolled-back", "deploying", "deployed", "error", "waiting"])
def test_a_deploy_record_without_a_pause_is_history_and_keeps_waiting(history, status):
    shas = history[1]
    state = snap(plan=(shas["A"], "applied"), http=(shas["A"], "serving"), deploy=(shas["C"], status, "old"))
    code, lines, _ = run(history, Probe(state), timeout=120)
    assert code == 2 and lines[0].startswith("TIMEOUT") and f"deploy={shas['C'][:12]}/{status}" in lines[0]


# spec: ANYM-440
def test_removing_the_pause_and_a_later_promotion_of_the_same_sha_ends_live(history):
    shas = history[1]
    held = snap(plan=(shas["A"], "applied"), http=(shas["A"], "serving"), pause="held",
                deploy=(shas["B"], "halted", "refused"))
    resumed = snap(plan=(shas["A"], "applied"), http=(shas["A"], "serving"), deploy=(shas["B"], "halted", "refused"))
    code, lines, clock = run(history, Probe(resumed, resumed, both(shas, "B")))
    assert code == 0 and lines[0].startswith("LIVE") and len(clock.sleeps) == 2
    assert run(history, Probe(held))[0] == 1


# spec: ANYM-440
def test_the_output_is_one_ascii_line_with_escaped_and_capped_receipt_text(history):
    reason = "x" * 1000 + "\x1b[31m\nforged line"
    code, lines, _ = run(history, Probe(snap(pause=reason)))
    assert code == 1 and len(lines) == 1 and lines[0].isascii() and "\n" not in lines[0] and len(lines[0]) < 300
    code, lines, _ = run(history, Probe(snap(deploy=("A" * 300, "weird\nstatus\x1b"))), timeout=60)
    assert len(lines) == 1 and "\n" not in lines[0] and "\x1b" not in lines[0] and lines[0].isascii()


# spec: ANYM-440
@pytest.mark.parametrize("failure", [ValueError("ssh exited 255"), ValueError("ssh could not run or timed out")])
def test_five_failed_polls_in_a_row_are_unreachable_and_a_success_resets_the_count(history, failure):
    shas = history[1]
    idle = snap(plan=(shas["A"], "applied"), http=(shas["A"], "serving"))
    probe = Probe(failure)
    code, lines, _ = run(history, probe, timeout=100000)
    assert code == 2 and len(probe.timeouts) == watcher.UNREACHABLE_AFTER and lines[0].startswith("UNREACHABLE")
    errors = [failure] * (watcher.UNREACHABLE_AFTER - 1)
    probe = Probe(*errors, idle, *errors, both(shas, "B"))
    assert run(history, probe, timeout=100000)[0] == 0 and len(probe.timeouts) == 2 * len(errors) + 2


# spec: ANYM-440
def test_the_overall_deadline_wins_over_the_failure_count_and_bounds_every_wait(history):
    probe = Probe(ValueError("ssh exited 255"))
    code, lines, clock = run(history, probe, timeout=150, interval=60)
    assert code == 2 and lines[0].startswith("TIMEOUT") and "UNREACHABLE" not in lines[0]
    assert all(timeout <= watcher.SSH_TIMEOUT for timeout in probe.timeouts)
    assert clock.now == 150 and clock.sleeps == [60, 60, 30]
    idle = Probe(snap())
    code, lines, clock = run(history, idle, timeout=30, interval=60)
    assert code == 2 and clock.sleeps == [30] and idle.timeouts == [30]


# spec: ANYM-440
def test_ssh_and_git_always_get_a_deadline_and_a_timeout_is_a_failed_poll_not_a_hang(history, monkeypatch):
    root, shas = history
    seen = []

    def fake(command, **kwargs):
        seen.append((command[0], kwargs.get("timeout")))
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(ValueError, match="timed out"):
        watcher.probe("host", 7)
    assert seen == [("ssh", 7)]
    seen.clear()
    machine = watcher.Watch(shas["B"], root, lambda: 25)
    assert machine.reaches({"sha": shas["C"]}) is None and machine.fetched
    assert seen == [("git", 25), ("git", 25)] and machine.reaches({"sha": shas["C"]}) is None
    assert len(seen) == 3  # the fetch runs once per run
    clock = Clock()
    lines = []
    code = watcher.watch(shas["B"], "host", 100, 10, out=lines.append, root=root, clock=clock, sleep=clock.sleep)
    assert code == 2 and lines[0].startswith(("UNREACHABLE", "TIMEOUT")) and all(t is not None for _, t in seen)


# spec: ANYM-440
def test_the_ssh_command_and_script_are_constant_and_the_sha_never_reaches_them(monkeypatch):
    seen = []

    def fake(command, **kwargs):
        seen.append((command, kwargs.get("input")))
        return subprocess.CompletedProcess(command, 0, json.dumps(snap()), "")

    monkeypatch.setattr(subprocess, "run", fake)
    for sha in ("a" * 40, "b" * 40):
        clock = Clock()
        watcher.watch(sha, "deedkit-prod", 10, 60, out=lambda line: None, clock=clock, sleep=clock.sleep)
    assert seen[0] == seen[-1] and seen[0][0] == watcher.ssh_command("deedkit-prod")
    assert seen[0][1] == watcher.remote_script()
    assert not any(("a" * 40) in part or ("b" * 40) in part for part in [*seen[0][0], seen[0][1]])


# spec: ANYM-440
@pytest.mark.parametrize("stdout", ["not json", "[]", '{"plan_a": null}', json.dumps({**snap(), "extra": 1}),
                                    json.dumps({**snap(), "unresolved": "x"}),
                                    json.dumps({**snap(), "pause": {"present": "yes", "reason": None}}),
                                    json.dumps({**snap(), "plan_a": {"sha": ["x"]}})])
def test_a_bad_answer_from_the_host_is_a_failed_poll(monkeypatch, stdout):
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout, ""))
    with pytest.raises(ValueError):
        watcher.probe("host")
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 255, "", ""))
    with pytest.raises(ValueError, match="255"):
        watcher.probe("host")


# --- the real remote script, run on a synthetic host tree -------------------------------------------------------

def tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(f"{path.relative_to(root)}:{path.stat().st_mode}".encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value))


@pytest.fixture
def host_tree(tmp_path):
    """/etc/deedkit and the two state directories of a synthetic host; every file also carries secret-looking data."""
    etc, plan, http = tmp_path / "etc", tmp_path / "plan", tmp_path / "http"
    write(etc / "autodeploy.env", f"DEEDKIT_DEPLOY_CONFIG={etc}/release.json\nOTHER_TOKEN={SECRET}\n")
    write(etc / "release.json", {"state_directory": str(plan), "token": SECRET, "projects": [{"env": SECRET}]})
    write(etc / "http-production.json", {"state_directory": str(http), "token": SECRET})
    write(plan / "current.json", {"sha": "a" * 40, "status": "applied", "applied_at": "t", "images": {"x": SECRET},
                                  "ci": {"url": SECRET}, "rollback": {"projects": SECRET}})
    write(plan / "deploy.json", {"sha": "b" * 40, "status": "halted", "reason": "refused", "at": "t", "base": SECRET})
    write(http / "http-current.json", {"sha": "a" * 40, "status": "serving", "private_files": {"k": SECRET}})
    return tmp_path


def run_remote(tree, source=None):
    source = source or watcher.remote_script(str(tree / "etc"))
    completed = subprocess.run([sys.executable, "-I", "-"], input=source, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def inspect(tree, source=None):
    """Problems of a remote script: static (imports, mutating calls) and dynamic (output, tree changes)."""
    source = source or watcher.remote_script(str(tree / "etc"))
    problems, parsed = [], ast.parse(source)
    for node in ast.walk(parsed):
        if isinstance(node, ast.Import | ast.ImportFrom):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module]
            problems += [f"import {name}" for name in names if name not in ("json", "os")]
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name in {"exec", "eval", "compile", "__import__", "getattr", "setattr", "system", "popen", "remove",
                        "unlink", "rmdir", "rename", "mkdir", "makedirs", "chmod", "touch", "write", "write_text",
                        "write_bytes", "truncate", "symlink", "Path"}:
                problems.append(f"call {name}")
            if name == "open" and (len(node.args) != 1 or node.keywords):
                problems.append("open with a mode")
    before = tree_digest(tree)
    completed = subprocess.run([sys.executable, "-I", "-"], input=source, capture_output=True, text=True, timeout=60)
    output = completed.stdout
    if completed.returncode:
        problems.append("the script failed")
    if tree_digest(tree) != before:
        problems.append("the tree changed")
    if SECRET in output:
        problems.append("a secret reached the output")
    return problems, output


# spec: ANYM-440
def test_the_real_remote_script_reads_only_and_prints_only_the_projection(host_tree):
    problems, output = inspect(host_tree)
    state = json.loads(output)
    assert problems == [] and watcher.valid(state)
    assert state["plan_a"] == {"sha": "a" * 40, "status": "applied"} and state["http"]["status"] == "serving"
    assert state["deploy"] == {"sha": "b" * 40, "status": "halted", "reason": "refused", "at": "t"}
    assert state["pause"] == IDLE and state["unresolved"] == []


# spec: ANYM-440
@pytest.mark.parametrize("old,new,problem", [
    ("import os\n", "import os\nimport subprocess\n", "import subprocess"),
    ('        with open(path) as stream:\n', '        open(path + ".x", "w").write("x")\n        with open(path) as stream:\n',
     "open with a mode"),
    ("def has(path):\n    try:\n", "def has(path):\n    os.remove(path)\n    return True\n    try:\n", "call remove"),
    ("def has(path):\n    try:\n", "def has(path):\n    open(path).write('x')\n    return True\n    try:\n",
     "call write"),
    ("    return {key: value[key][:200] for key in (\"sha\", \"status\", \"reason\", \"at\") if isinstance(value.get(key), str)}",
     "    return value", "a secret reached the output"),
])
def test_a_mutated_remote_script_that_writes_or_leaks_is_caught(host_tree, old, new, problem):
    source = watcher.remote_script(str(host_tree / "etc"))
    assert old in source
    problems, _ = inspect(host_tree, source.replace(old, new, 1))
    assert problem in problems


# spec: ANYM-440
@pytest.mark.parametrize("kind,files,expected", [
    ("apply in progress", {"plan/r1/attempted.json": {"status": "applying"}}, ["r1:apply"]),
    ("unhealthy, no rollback", {"plan/r1/attempted.json": {}, "plan/r1/result.json": {"status": "unhealthy"}},
     ["r1:apply"]),
    ("unhealthy, rolled back", {"plan/r1/attempted.json": {}, "plan/r1/result.json": {"status": "unhealthy"},
                                "plan/r1/rollback.json": {}}, []),
    ("applied", {"plan/r1/attempted.json": {}, "plan/r1/result.json": {"status": "applied"}}, []),
    ("rollback in progress", {"plan/r1/rollback-attempted.json": {}}, ["r1:rollback"]),
    ("http recovery required", {"http/h1/http-attempted.json": {"status": "recovery-required"}}, ["h1:http"]),
    ("http serving", {"http/h1/http-attempted.json": {"status": "serving"}}, []),
    ("http failed before admission", {"http/h1/http-attempted.json": {"status": "startup-failed"}}, []),
    ("http malformed", {"http/h1/http-attempted.json": "not json"}, ["h1:http"]),
])
def test_the_real_remote_script_names_the_attempts_production_treats_as_unfinished(host_tree, kind, files, expected):
    for name, value in files.items():
        write(host_tree / name, value)
    problems, output = inspect(host_tree)
    assert problems == [] and json.loads(output)["unresolved"] == expected


# spec: ANYM-440
def test_the_real_remote_script_names_a_rollback_whose_current_receipt_was_never_rewritten(host_tree):
    write(host_tree / "plan/" / ("a" * 40) / "rollback-attempted.json", {})
    write(host_tree / "plan" / ("a" * 40) / "rollback.json", {})
    problems, output = inspect(host_tree)
    assert problems == [] and json.loads(output)["unresolved"] == ["a" * 40 + ":rolled-back-but-current-applied"]
    write(host_tree / "plan/current.json", {"sha": "a" * 40, "status": "rolled-back"})
    assert json.loads(run_remote(host_tree))["unresolved"] == []


# spec: ANYM-440
def test_the_real_remote_script_lists_serving_http_attempts(host_tree):
    write(host_tree / "http" / ("c" * 40) / "http-attempted.json", {"status": "serving"})
    write(host_tree / "http" / ("d" * 40) / "http-attempted.json", {"status": "startup-failed"})
    problems, output = inspect(host_tree)
    assert problems == [] and json.loads(output)["served"] == ["c" * 40]


# spec: ANYM-440
def test_the_real_remote_script_lists_every_serving_attempt_not_only_the_last_twenty(host_tree):
    names = [f"{number:040x}" for number in range(21)]
    for name in names:
        write(host_tree / "http" / name / "http-attempted.json", {"status": "serving"})
    problems, output = inspect(host_tree)
    assert problems == [] and json.loads(output)["served"] == names


# spec: ANYM-440
@pytest.mark.parametrize("text", ["x" * 500, "\u00e9" * 500, "\x1b[31m" * 100])
def test_every_dynamic_field_is_at_most_the_limit_after_escaping(text):
    assert len(watcher.shown(text)) <= watcher.TEXT_LIMIT
    assert len(watcher.shown(ValueError(text))) <= watcher.TEXT_LIMIT


# spec: ANYM-440
@pytest.mark.parametrize("content,reason", [
    ('{"status": "halted", "reason": "plan A was refused", "at": "t"}', "plan A was refused"),
    ("", None), ("held by hand for the backup", "held by hand for the backup"), ("[1, 2]", "[1, 2]"),
    ('{"status": "halted"}', None),
])
def test_the_real_remote_script_reports_a_pause_of_any_content(host_tree, content, reason):
    write(host_tree / "etc/autodeploy.pause", content)
    problems, output = inspect(host_tree)
    assert problems == [] and json.loads(output)["pause"] == {"present": True, "reason": reason}


# spec: ANYM-440
def test_the_real_remote_script_survives_missing_receipts_and_configs(tmp_path):
    (tmp_path / "etc").mkdir()
    problems, output = inspect(tmp_path)
    state = json.loads(output)
    assert problems == [] and watcher.valid(state) and state["plan_a"] is None and state["pause"] == IDLE
    write(tmp_path / "etc/autodeploy.pause", "")
    assert json.loads(run_remote(tmp_path))["pause"]["present"] is True


# spec: ANYM-440
def test_the_real_remote_script_follows_the_env_file_and_the_defaults(host_tree):
    etc = host_tree / "etc"
    other = host_tree / "other-plan"
    write(other / "current.json", {"sha": "c" * 40, "status": "applied"})
    write(etc / "alt.json", {"state_directory": str(other)})
    write(etc / "autodeploy.env", f"DEEDKIT_DEPLOY_CONFIG='{etc}/alt.json'\n")
    assert json.loads(run_remote(host_tree))["plan_a"]["sha"] == "c" * 40
    (etc / "autodeploy.env").unlink()
    write(etc / "production.json", {"state_directory": str(other)})
    assert json.loads(run_remote(host_tree))["plan_a"]["sha"] == "c" * 40


# spec: ANYM-440
@pytest.mark.parametrize("argument,extra", [("abc", []), ("A" * 40, []), ("a" * 39, []), ("a" * 41, []), ("main", []),
                                            ("a" * 40, ["--timeout", "0"]), ("a" * 40, ["--interval", "-1"]),
                                            ("a" * 40, ["--timeout", "nan"]), ("a" * 40, ["--timeout", "inf"]),
                                            ("a" * 40, ["--interval", "nan"]), ("a" * 40, ["--interval", "inf"])])
def test_bad_arguments_exit_64_before_any_ssh_or_git_call(argument, extra, tmp_path):
    log = tmp_path / "calls"
    for name in ("ssh", "git"):
        fake = tmp_path / name
        fake.write_text(f"#!/bin/sh\necho {name} >> {log}\n")
        fake.chmod(0o755)
    completed = subprocess.run([sys.executable, str(SCRIPT), argument, *extra], capture_output=True, text=True,
                               env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    assert completed.returncode == 64 and not log.exists() and completed.stdout == ""


# spec: ANYM-440
def test_the_command_line_prints_one_line_and_exits_with_the_verdict(history, tmp_path):
    shas = history[1]
    ssh = tmp_path / "ssh"
    ssh.write_text("#!/bin/sh\ncat > /dev/null\necho '" + json.dumps(both(shas, "B")) + "'\n")
    ssh.chmod(0o755)
    completed = subprocess.run([sys.executable, str(SCRIPT), shas["B"]], capture_output=True, text=True,
                               env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    assert completed.returncode == 0 and completed.stdout.startswith("LIVE ") and completed.stdout.count("\n") == 1


# spec: ANYM-440
def test_the_live_line_is_exactly_the_specified_one(history):
    shas = history[1]
    code, lines, _ = run(history, Probe(both(shas, "C")))
    assert code == 0 and lines == [f"LIVE {shas['B'][:12]} plan-a={shas['C'][:12]} http={shas['C'][:12]}"]


# spec: ANYM-440
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_the_real_remote_script_does_not_turn_a_failed_directory_scan_into_an_empty_inventory(host_tree):
    plan = host_tree / "plan"
    write(plan / ("c" * 40) / "attempted.json", {})  # an abandoned apply that the scan would have named
    plan.chmod(0o100)  # the receipts inside stay readable, the listing fails
    try:
        problems, output = inspect(host_tree)
    finally:
        plan.chmod(0o700)
    assert problems == [] and "unstable" not in json.loads(output)
    assert json.loads(output)["unresolved"] == ["plan:unreadable"]


# spec: ANYM-440
@pytest.mark.parametrize("content", ["not json", "[1]", '{"status": "applied"' + " " * 70000 + "}",
                                     '{"status": "applied"}' + " " * 65536 + "not-json"])
def test_the_real_remote_script_names_an_existing_result_it_cannot_read(host_tree, content):
    write(host_tree / "plan" / ("c" * 40) / "attempted.json", {})
    write(host_tree / "plan" / ("c" * 40) / "result.json", content)
    problems, output = inspect(host_tree)
    assert problems == [] and json.loads(output)["unresolved"] == ["c" * 40 + ":result-unreadable"]


# spec: ANYM-440
def test_the_real_remote_script_names_a_release_directory_whose_metadata_cannot_be_read(host_tree):
    name = "c" * 40
    write(host_tree / "plan" / name / "attempted.json", {})
    fault = ("import os\n_stat = os.stat\n"
             "def failing(path, *args, **kwargs):\n"
             f"    if str(path).endswith('/{name}'):\n        raise OSError(5, 'input/output error')\n"
             "    return _stat(path, *args, **kwargs)\n"
             "os.stat = failing\n")
    output = run_remote(host_tree, fault + watcher.remote_script(str(host_tree / "etc")))
    assert json.loads(output)["unresolved"] == [name + ":unreadable"]


# spec: ANYM-440
def test_historical_serving_attempts_are_settled_even_when_the_receipts_name_a_commit_not_fetched_yet(
        history, tmp_path):
    root, shas = history
    origin, worker = tmp_path / "origin", tmp_path / "worker"
    git(tmp_path, "clone", "-q", str(root), str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(worker))
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
        git(origin, "config", key, value)
    (origin / "f").write_text("D")
    git(origin, "commit", "-q", "-am", "D")
    newest = git(origin, "rev-parse", "HEAD")
    assert subprocess.run(["git", "-C", str(worker), "cat-file", "-e", newest]).returncode != 0
    snapshot = snap(plan=(newest, "applied"), http=(newest, "serving"), served=[shas["C"], newest])
    code, lines, _ = run((worker, shas), Probe(snapshot))
    assert code == 0 and lines[0].startswith(f"LIVE {shas['B'][:12]}")


# spec: ANYM-440
def test_no_git_process_starts_once_the_deadline_has_passed(history, monkeypatch):
    root, shas = history
    started = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: started.append(args))
    machine = watcher.Watch(shas["B"], root, lambda: 0)
    assert machine.related(shas["B"], shas["C"]) is None and machine.reaches({"sha": shas["C"]}) is None
    assert started == []
    machine = watcher.Watch(shas["B"], root, lambda: 0.4)
    assert machine.budget(60) == 0.4


# spec: ANYM-440
@pytest.mark.parametrize("snapshot_of,named", [(lambda shas: snap(pause="held"), "REFUSED"),
                                                (lambda shas: both(shas, "C"), None)])
def test_a_verdict_decided_after_the_deadline_is_a_timeout(history, snapshot_of, named):
    shas = history[1]
    clock, lines = Clock(), []

    def late(host, timeout):
        clock.now += timeout + 1  # ssh used the last of the budget
        return snapshot_of(shas)

    code = watcher.watch(shas["B"], "host", 10, 5, out=lines.append, root=history[0], probe=late, clock=clock,
                         sleep=clock.sleep)
    assert code == 2 and lines[0].startswith("TIMEOUT ") and len(lines) == 1
    assert named is None or named in lines[0]
