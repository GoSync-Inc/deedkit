"""deedkit-inference-worker.service: a fake runner stands for docker; the locks, the config and `snapshot` are real."""
import fcntl
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
import yaml

from scripts import inference_worker_start as start
from scripts import production as p

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "sha256:" + "b" * 64
CONTAINER = "abc123"
INSPECTED = {"Image": IMAGE, "Config": {"Image": "x"}, "HostConfig": {"Memory": 1}, "Mounts": [],
             "State": {"Status": "exited"}}


class Host:
    """The docker commands of one run, recorded in order; `on` runs a test's action when a command matches."""

    def __init__(self, tmp_path):
        self.commands, self.steps, self.on = [], [], {}
        self.running, self.containers, self.inspected = [], [CONTAINER], dict(INSPECTED)
        self.exit_code, self.db_health, self.egress, self.after_start = "0", ["healthy"], "ok\n", ["true 0"]
        self.fail = set()
        self.tmp_path = tmp_path

    def name(self, argv):
        text = " ".join(argv)
        if "config --format json" in text:
            return "config"
        if argv[:2] == ["docker", "ps"]:
            if "label=com.docker.compose.service=db" in argv:
                return "db_ps"
            return "snapshot_ps" if "--all" in argv else "running_ps"
        if argv[:2] == ["docker", "inspect"]:
            if len(argv) == 3:
                return "snapshot_inspect"
            return {"{{.State.ExitCode}}": "exit_code", "{{.State.Health.Status}}": "db_health"}.get(argv[3], "window")
        if "inference_egress_check.py" in text:
            return "egress"
        if argv[-2:] == ["start", "enricher-mini"]:
            return "start"
        raise AssertionError(f"unexpected command {argv}")

    def run(self, argv, **kwargs):
        name = self.name(argv)
        self.commands.append(argv)
        self.steps.append(name)
        if action := self.on.get(name):
            action()
        if name in self.fail:
            raise p.Refused("failed")
        if name == "config":
            return json.dumps({"services": {"enricher-mini": {}}})
        if name == "running_ps":
            return "\n".join(self.running)
        if name == "snapshot_ps":
            return "\n".join(self.containers)
        if name == "snapshot_inspect":
            return json.dumps([self.inspected])
        if name == "exit_code":
            return self.exit_code + "\n"
        if name == "db_ps":
            return "dbid\n"
        if name == "db_health":
            return self.db_health.pop(0) + "\n" if len(self.db_health) > 1 else self.db_health[0] + "\n"
        if name == "egress":
            return self.egress
        if name == "window":
            return self.after_start.pop(0) + "\n" if len(self.after_start) > 1 else self.after_start[0] + "\n"
        return ""


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A host with one stopped worker whose identity the accepted receipt records, and every gate passing."""
    state, http = tmp_path / "state", tmp_path / "http"
    state.mkdir(mode=0o700)
    http.mkdir(mode=0o700)
    directory = tmp_path / "project"
    directory.mkdir()
    project = {"name": "probe", "directory": str(directory), "files": [str(directory)], "env_files": [str(directory)],
               "services": {"api": "runtime", "enricher-mini": "enricher-local"}}
    for path, value in ((tmp_path / "production.json", {"version": 1, "state_directory": str(state), "projects": [project]}),
                        (tmp_path / "http.json", {"version": 1, "state_directory": str(http),
                                                  "projects": [{**project, "services": {"api": "runtime"}}]})):
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    identity = {"container": CONTAINER, "image": IMAGE, "settings": p.container_settings(INSPECTED)}
    (state / "current.json").write_text(json.dumps({"status": "applied", "rollback": {"version": 1, "projects": [
        {"name": "probe", "running": {"enricher-mini": identity}}]}}))
    host, clock = Host(tmp_path), SimpleNamespace(now=1000.0)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(p, "DEADLINE", None)
    monkeypatch.setattr(p, "run", host.run)
    monkeypatch.setattr(p, "validate_runtime", lambda unit: None)
    monkeypatch.setattr(start, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setenv("DEEDKIT_DEPLOY_CONFIG", str(tmp_path / "production.json"))
    monkeypatch.setenv("DEEDKIT_HTTP_CONFIG", str(tmp_path / "http.json"))
    monkeypatch.setenv("DEEDKIT_BACKUP_LOCK", str(tmp_path / "runner.lock"))
    monkeypatch.setattr(start, "HOLD", tmp_path / "hold")
    monkeypatch.setattr(start, "RECEIPT", tmp_path / "lib" / "inference-worker.json")
    calls = SimpleNamespace(deadlines=[], results=[], settings=[])

    def wait_for_calls(settings, deadline):
        host.steps.append("calls")
        calls.deadlines.append(deadline)
        calls.settings.append(settings)
        if calls.results and (result := calls.results.pop(0)):
            raise result

    monkeypatch.setattr(p, "wait_for_calls", wait_for_calls)
    return SimpleNamespace(host=host, calls=calls, clock=clock, state=state, http=http, tmp_path=tmp_path, identity=identity)


def receipt():
    return json.loads(start.RECEIPT.read_text())


FULL = ["running_ps", "config", "snapshot_ps", "snapshot_inspect", "exit_code", "db_ps", "db_health", "calls", "egress",
        "config", "snapshot_ps", "snapshot_inspect", "calls", "start", "window"]


def held(path):
    """Whether another holder has `path` locked: a second descriptor's non-waiting flock is what a backup tries."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


# spec: ANYM-794
def test_a_clean_host_runs_every_step_in_order_and_starts_the_worker_once(world):
    assert start.main() == 0
    assert world.host.steps[:len(FULL)] == FULL
    assert world.host.steps.count("start") == 1
    assert set(receipt()) == {"checked_at", "outcome", "reason"}
    assert (receipt()["outcome"], receipt()["reason"]) == ("started", None)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", receipt()["checked_at"])


# spec: ANYM-794
def test_the_start_is_compose_start_of_the_one_service_with_its_profile_and_the_configured_files(world):
    start.main()
    command = next(argv for argv in world.host.commands if argv[-1] == "enricher-mini" and "start" in argv)
    assert command[:2] == ["docker", "compose"] and command[-2:] == ["start", "enricher-mini"]
    assert command[command.index("--profile") + 1] == "enrichment-mini" and "*" not in command
    assert command[command.index("--project-name") + 1] == "probe"
    assert command[command.index("-f") + 1] and "--env-file" in command


# spec: ANYM-794
def test_a_running_worker_is_already_running_and_nothing_else_runs(world):
    world.host.running = [CONTAINER]
    assert start.main() == 0
    assert world.host.steps == ["running_ps"]
    assert (receipt()["outcome"], receipt()["reason"]) == ("already_running", None)
    assert not (world.state / "deploy.lock").exists()


def hold(world):
    start.HOLD.write_text("")


def attempted(world):
    release = world.state / ("a" * 40)
    release.mkdir()
    (release / "attempted.json").write_text("{}")


def http_attempt(world):
    release = world.http / ("a" * 40)
    release.mkdir()
    (release / "http-attempted.json").write_text(json.dumps({"status": "starting"}))


def current(world, value):
    (world.state / "current.json").write_text(value if isinstance(value, str) else json.dumps(value))


def with_identity(world, **changed):
    return {"status": "applied", "rollback": {"version": 1, "projects": [
        {"name": "probe", "running": {"enricher-mini": {**world.identity, **changed}}}]}}


def fail(name):
    return lambda world: world.host.fail.add(name)


def setter(**values):
    return lambda world: [setattr(world.host, key, value) for key, value in values.items()]


def call_result(*results):
    return lambda world: world.calls.results.extend(results)


# Each failure records its reason, and the commands of every later step never run. `last` is the last step that ran.
REFUSALS = {
    "docker_unreadable": (fail("running_ps"), "running_ps"),
    "release_unresolved_plan_a": (attempted, "running_ps"),
    "release_unresolved_http": (http_attempt, "running_ps"),
    "no_container_none": (setter(containers=[]), "snapshot_ps"),
    "no_container_two": (setter(containers=["a1", "a2"]), "snapshot_ps"),
    "identity_mismatch_read_failed": (fail("snapshot_inspect"), "snapshot_inspect"),
    "identity_mismatch_same_image_replacement": (setter(containers=["other1"]), "snapshot_inspect"),
    "identity_mismatch_settings": (setter(inspected={**INSPECTED, "HostConfig": {"Memory": 2}}), "snapshot_inspect"),
    "identity_mismatch_image": (setter(inspected={**INSPECTED, "Image": "sha256:" + "c" * 64}), "snapshot_inspect"),
    "identity_unrecorded_no_rollback": (lambda world: current(world, {"status": "rolled-back"}), "snapshot_inspect"),
    "identity_unrecorded_bad_json": (lambda world: current(world, "{"), "snapshot_inspect"),
    "identity_unrecorded_legacy": (lambda world: current(world, with_identity(world, settings="unversioned")), "snapshot_inspect"),
    "identity_unrecorded_absent": (lambda world: (world.state / "current.json").unlink(), "snapshot_inspect"),
    "operator_hold": (hold, "snapshot_inspect"),
    "previous_exit_78": (setter(exit_code="78"), "exit_code"),
    "previous_exit_78_no_read": (fail("exit_code"), "exit_code"),
    "database_not_ready": (setter(db_health=["starting"]), "db_health"),
    "database_not_ready_no_health": (fail("db_health"), "db_health"),
    "active_call": (call_result(p.CallActive("a call")), "calls"),
    "call_probe_failed_unknown": (call_result(p.CallsUnknown("x")), "calls"),
    "call_probe_failed_stuck": (call_result(p.ProbeStuck("x")), "calls"),
    "egress_rules_not_verified": (setter(egress="mini_differs\n"), "egress"),
    "egress_rules_not_verified_exit": (fail("egress"), "egress"),
    "active_call_again": (call_result(None, p.CallActive("a call")), "calls"),
    "call_probe_failed_again": (call_result(None, p.CallsUnknown("x")), "calls"),
    "start_failed": (fail("start"), "start"),
}


# spec: ANYM-794
@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_a_failed_step_records_its_reason_and_runs_no_later_step(world, case):
    arrange, last = REFUSALS[case]
    arrange(world)
    assert start.main() == 1
    expected = re.sub(r"_(plan_a|http|none|two|read_failed|same_image_replacement|settings|image|no_rollback|bad_json|"
                      r"legacy|absent|no_read|no_health|unknown|stuck|exit|again)$", "", case)
    assert (receipt()["outcome"], receipt()["reason"]) == ("refused", expected)
    assert world.host.steps[-1] == last
    if case != "start_failed":
        assert "start" not in world.host.steps
    assert "window" not in world.host.steps


# spec: ANYM-794
def test_a_held_backup_lock_or_deploy_lock_refuses_before_any_later_step(world):
    fd = os.open(os.environ["DEEDKIT_BACKUP_LOCK"], os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    assert start.main() == 1
    assert receipt()["reason"] == "backup_running" and world.host.steps == ["running_ps"]
    os.close(fd)
    world.host.steps.clear()
    fd = os.open(world.state / "deploy.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    assert start.main() == 1
    assert receipt()["reason"] == "release_running" and world.host.steps == ["running_ps"]
    os.close(fd)


# spec: ANYM-794
def test_both_locks_stay_held_through_the_start_window_and_a_backup_is_refused_during_the_database_wait(world):
    seen = []

    def probe():
        seen.append((held(os.environ["DEEDKIT_BACKUP_LOCK"]), held(world.state / "deploy.lock")))

    world.host.on = {"db_health": probe, "window": probe}
    assert start.main() == 0
    assert seen and set(seen) == {(True, True)}
    assert not held(os.environ["DEEDKIT_BACKUP_LOCK"]) and not held(world.state / "deploy.lock")


# spec: ANYM-794
def test_a_hold_file_created_during_the_database_wait_stops_at_the_second_check(world):
    world.host.on = {"db_health": lambda: hold(world)}
    assert start.main() == 1
    assert receipt()["reason"] == "operator_hold"
    assert "egress" in world.host.steps and "start" not in world.host.steps and "calls" in world.host.steps


# spec: ANYM-794
def test_a_replacement_during_the_database_wait_stops_at_the_second_identity_check(world):
    world.host.on = {"db_health": lambda: setattr(world.host, "containers", ["other1"])}
    assert start.main() == 1
    assert receipt()["reason"] == "identity_mismatch" and "start" not in world.host.steps


# spec: ANYM-794
def test_a_call_that_starts_during_the_egress_check_stops_at_the_second_call_check_on_the_same_deadline(world):
    world.calls.results.extend([None, p.CallActive("a call")])
    assert start.main() == 1
    assert receipt()["reason"] == "active_call"
    assert world.host.steps.index("egress") < len(world.host.steps) - 1 and "start" not in world.host.steps
    first, second = world.calls.deadlines
    assert first == second


# spec: ANYM-794
def test_the_call_deadline_is_120_seconds_from_the_first_check_and_covers_every_project(world):
    world.host.db_health = ["starting", "starting", "healthy"]
    start.main()
    assert world.calls.deadlines[0] == world.clock.now - start.START_WINDOW + start.CALLS_WAIT
    assert [project["name"] for project in world.calls.settings[0]["projects"]] == ["probe"]


# spec: ANYM-794
def test_the_database_wait_gives_up_after_300_seconds_and_accepts_a_late_healthy(world):
    world.host.db_health = ["starting"]
    before = world.clock.now
    assert start.main() == 1
    assert world.clock.now - before == 300 and receipt()["reason"] == "database_not_ready"


# spec: ANYM-794
def test_a_database_that_turns_healthy_inside_the_wait_lets_the_start_go(world):
    world.host.db_health = ["starting", "starting", "healthy"]
    assert start.main() == 0 and receipt()["outcome"] == "started"


# spec: ANYM-794
def test_an_exit_78_inside_the_window_is_not_proved_and_the_next_run_stops_at_the_last_exit_code(world):
    world.host.after_start = ["true 0", "false 78"]
    assert start.main() == 1
    assert receipt()["reason"] == "confinement_not_proved"
    assert world.host.steps.count("start") == 1
    world.host.steps.clear()
    world.host.exit_code = "78"
    assert start.main() == 1
    assert receipt()["reason"] == "previous_exit_78" and "start" not in world.host.steps


# spec: ANYM-794
def test_another_exit_inside_the_window_records_its_code_and_never_starts_again(world):
    world.host.after_start = ["false 1"]
    assert start.main() == 1
    assert receipt()["reason"] == "exited_1" and world.host.steps.count("start") == 1


# spec: ANYM-794
def test_a_worker_alive_for_the_whole_window_is_started_and_the_window_is_60_seconds(world):
    before = {}
    world.host.on = {"start": lambda: before.setdefault("at", world.clock.now)}
    assert start.main() == 0
    assert world.clock.now - before["at"] == 60


# spec: ANYM-794
def test_a_window_that_could_not_read_the_container_is_not_called_started(world):
    world.host.fail.add("window")
    assert start.main() == 1
    assert receipt()["reason"] == "start_failed" and world.host.steps.count("start") == 1


# spec: ANYM-794
def test_a_tool_that_lacks_a_function_records_tool_too_old_and_starts_nothing(world, monkeypatch):
    monkeypatch.delattr(p, "unresolved")
    assert start.main() == 1
    assert receipt()["reason"] == "tool_too_old" and world.host.steps == []


# spec: ANYM-794
def test_a_tool_that_cannot_be_imported_records_tool_too_old(world, monkeypatch):
    monkeypatch.setattr(start, "import_tool", lambda: 1 / 0)
    assert start.main() == 1
    assert receipt()["reason"] == "tool_too_old" and world.host.steps == []


# spec: ANYM-794
def test_a_configuration_that_cannot_be_read_starts_nothing(world, monkeypatch):
    monkeypatch.setenv("DEEDKIT_DEPLOY_CONFIG", str(world.tmp_path / "missing.json"))
    assert start.main() == 1
    assert receipt()["reason"] == "config_unreadable" and world.host.steps == []


# spec: ANYM-794
@pytest.mark.parametrize("late_step,lost,expected", [("exit_code", 500, "database_not_ready"),
                                                     ("egress", 400, "start_failed"),
                                                     ("start", 530, None)])
def test_every_command_is_bounded_so_the_receipt_is_written_before_the_unit_timeout(world, late_step, lost, expected):
    begun = world.clock.now
    world.host.db_health = ["starting"] if expected == "database_not_ready" else ["healthy"]
    world.host.on = {late_step: lambda: setattr(world.clock, "now", begun + lost)}
    assert start.main() == (1 if expected else 0)
    assert receipt()["reason"] == expected
    assert world.clock.now - begun <= start.BOUND < 600
    assert p.DEADLINE == begun + start.BOUND
    assert ("start" in world.host.steps) == (expected is None)
    assert not held(os.environ["DEEDKIT_BACKUP_LOCK"]) and not held(world.state / "deploy.lock")


# spec: ANYM-794
def test_a_failed_receipt_write_is_logged_and_changes_nothing_else(world, monkeypatch, capsys):
    monkeypatch.setattr(start, "RECEIPT", world.tmp_path / "absent-file" / "x" / "receipt.json")
    (world.tmp_path / "absent-file").write_text("a file where a directory belongs")
    assert start.main() == 0
    assert world.host.steps.count("start") == 1
    assert "receipt not written" in capsys.readouterr().err


# spec: ANYM-794
def test_nothing_printed_or_stored_holds_an_environment_value(world, capsys):
    world.host.inspected = {**INSPECTED, "Config": {"Env": ["SECRET_VALUE=hunter2"]}}
    (world.state / "current.json").write_text(json.dumps(with_identity(
        world, settings=p.container_settings(world.host.inspected))))
    start.main()
    printed = capsys.readouterr()
    assert "hunter2" not in printed.out + printed.err + start.RECEIPT.read_text()


# spec: ANYM-794
def test_the_script_source_has_no_command_that_recreates_or_removes_a_container():
    source = (ROOT / "scripts/inference_worker_start.py").read_text()
    assert not re.search(r"\b(up|restart|rm)\b", source)


# spec: ANYM-794
def test_the_unit_is_a_oneshot_nothing_depends_on_with_no_retry_or_timer():
    unit = (ROOT / "deploy/deedkit-inference-worker.service").read_text()
    lines = [line for line in unit.splitlines() if line and not line.startswith("#")]
    for expected in ("Type=oneshot", "User=root", "After=ufw.service docker.service", "WantedBy=docker.service",
                     "TimeoutStartSec=600", "Environment=DEEDKIT_DEPLOY_CONFIG=/etc/deedkit/production.json",
                     "Environment=DEEDKIT_BACKUP_LOCK=/opt/personal-os-probe/backups/.runner.lock",
                     "EnvironmentFile=-/etc/deedkit/autodeploy.env",
                     "ExecStart=/usr/bin/python3 /usr/local/lib/deedkit/inference_worker_start.py"):
        assert expected in lines
    assert not [line for line in lines if re.match(r"(Restart|Before|RequiredBy|Requires)\b", line)]
    assert not (ROOT / "deploy/deedkit-inference-worker.timer").exists()


# spec: ANYM-794
def test_the_worker_keeps_restart_policy_no_and_the_comment_names_the_unit():
    text = (ROOT / "deploy/compose.enrichment.yaml").read_text()
    assert yaml.safe_load(text)["services"]["enricher-mini"]["restart"] == "no"
    assert "deedkit-inference-worker.service" in text


# spec: ANYM-794
def test_the_runbook_names_the_unit_the_hold_file_and_the_check_before_enabling():
    text = (ROOT / "docs/operations/production.md").read_text()
    section = text[text.index("### Stopped worker"):text.index("### Working space and audio limit")]
    for name in ("deedkit-inference-worker.service", "/etc/deedkit/inference-worker.hold",
                 "/var/lib/deedkit/inference-worker.json", "ANYM-786", "4017891b", "systemctl enable"):
        assert name in section
    assert "--now" not in section
    assert "deedkit-inference-guard" not in text.replace("/usr/local/sbin/deedkit-inference-guard", "")
    undo = section[section.index("Undo:"):]
    assert "/etc/deedkit/inference-worker.hold" in undo


# spec: ANYM-794
def test_the_receipt_path_is_the_one_status_reads():
    assert str(start.RECEIPT) == str(p.INFERENCE_WORKER_RECEIPT)
