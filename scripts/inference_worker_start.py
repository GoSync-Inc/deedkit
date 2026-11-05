#!/usr/bin/env python3
"""What deedkit-inference-worker.service runs when Docker starts (ANYM-794): the manual recovery of a stopped
enricher-mini (production.md "Stopped worker"), behind its gates and under the backup and release locks.

It starts the existing container once or leaves it stopped, and records which in a receipt of codes and a time. Command
output is never printed or stored: it can hold Compose environment values. Exit 0: started or already running; 1: refused."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

SERVICE, PROFILE, DATABASE = "enricher-mini", "enrichment-mini", "db"
HOLD = Path("/etc/deedkit/inference-worker.hold")
RECEIPT = Path("/var/lib/deedkit/inference-worker.json")
EGRESS_CHECK = ["/usr/bin/python3", "/usr/local/lib/deedkit/inference_egress_check.py", "verify"]
DATABASE_WAIT, CALLS_WAIT, START_WINDOW, POLL, READ_TIMEOUT, START_TIMEOUT = 300, 120, 60, 5, 60, 120
# Every command, wait and sleep ends by then, so the receipt is written and the locks are free inside the unit's
# TimeoutStartSec=600. A start begins only with time left for the command and the whole window after it.
BOUND = 570
START_RESERVE = START_TIMEOUT + START_WINDOW
# What this script uses of the installed production.py; a tool without one of them is too old to start anything.
REQUIRED = ("Refused", "CallActive", "DEADLINE", "HTTP_CONFIG", "active_config", "compose", "config", "legacy_identities",
            "lock", "backup_lock", "read", "run", "snapshot", "unresolved", "wait_for_calls")


class Refusal(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@contextmanager
def step(reason):
    """Any failure inside refuses with this step's code. Only the exception's type is logged, never its text."""
    try:
        yield
    except Refusal:
        raise
    except Exception as exc:
        print(f"inference worker: {reason} ({type(exc).__name__})", file=sys.stderr, flush=True)
        raise Refusal(reason) from None


def import_tool():
    try:
        from scripts import production
    except ImportError:
        import production
    return production


def installed_tool():
    with step("tool_too_old"):
        tool = import_tool()
        if missing := [name for name in REQUIRED if not hasattr(tool, name)]:
            raise AttributeError(", ".join(missing))
        return tool


def containers(tool, project, service):
    return tool.run(["docker", "ps", "--filter", "label=com.docker.compose.oneoff=False",
                     "--filter", "label=com.docker.compose.project=" + project["name"],
                     "--filter", "label=com.docker.compose.service=" + service, "--format", "{{.ID}}"],
                    timeout=READ_TIMEOUT).split()


def field(tool, container, template):
    return tool.run(["docker", "inspect", "--format", template, container], timeout=READ_TIMEOUT).strip()


def http_state(tool, state):
    """Plan B's state directory; a host with no plan B (no default configuration) has none, so its own is searched."""
    path = os.environ.get("DEEDKIT_HTTP_CONFIG") or tool.HTTP_CONFIG
    if path == tool.HTTP_CONFIG and not os.path.lexists(path):
        return state
    return Path(tool.config(path)["state_directory"])


def recorded_identity(tool, state, name):
    """The worker's identity in the accepted receipt, as rollback compares it."""
    with step("identity_unrecorded"):
        current = tool.read(state / "current.json")
        if current["status"] != "applied":
            raise ValueError("no accepted release")
        identity = next(item for item in current["rollback"]["projects"] if item["name"] == name)["running"][SERVICE]
        if set(identity) != {"container", "image", "settings"} or tool.legacy_identities({SERVICE: identity}):
            raise ValueError("legacy identity")
        return identity


def read_identity(tool, worker):
    try:
        return tool.snapshot(worker, stopped=True)[1][SERVICE]
    except tool.Refused as exc:
        if "exactly one" in str(exc):
            raise Refusal("no_container") from None
        raise


def check_identity(tool, worker, recorded):
    with step("identity_mismatch"):
        if read_identity(tool, worker) != recorded:
            raise Refusal("identity_mismatch")


def check_hold():
    if os.path.lexists(HOLD):
        raise Refusal("operator_hold")


def check_calls(tool, settings, deadline):
    try:
        tool.wait_for_calls(settings, deadline)
    except tool.CallActive:
        raise Refusal("active_call") from None
    except Exception:  # CallsUnknown, ProbeStuck: never proceed on doubt
        raise Refusal("call_probe_failed") from None


def wait_for_database(tool, project, last):
    deadline = min(time.monotonic() + DATABASE_WAIT, last)
    while True:
        try:
            found = containers(tool, project, DATABASE)
            if len(found) == 1 and field(tool, found[0], "{{.State.Health.Status}}") == "healthy":
                return
        except Exception:  # not created yet, no health state yet: the bound decides
            pass
        if (left := deadline - time.monotonic()) <= 0:
            raise Refusal("database_not_ready")
        time.sleep(min(POLL, left))


def watch_start(tool, container, last):
    """Read the started container for START_WINDOW seconds. It is never started again here."""
    deadline, known = min(time.monotonic() + START_WINDOW, last), False
    while True:
        try:
            running, code = field(tool, container, "{{.State.Running}} {{.State.ExitCode}}").split()
            known = True
        except Exception:
            running, code, known = "true", "0", False
        if running != "true":
            raise Refusal("confinement_not_proved" if code == "78" else f"exited_{int(code)}")
        if (left := deadline - time.monotonic()) <= 0:
            break
        time.sleep(min(POLL, left))
    if not known:
        raise Refusal("start_failed")


def attempt():
    """The steps of the card, a to k; returns the outcome, raises Refusal with the reason."""
    tool = installed_tool()
    last = tool.DEADLINE = time.monotonic() + BOUND
    with step("config_unreadable"):
        settings = tool.config(tool.active_config())
        state = Path(settings["state_directory"])
    project = next((item for item in settings["projects"] if SERVICE in item["services"]), None)
    if project is None:
        raise Refusal("no_container")
    worker = {**project, "services": {SERVICE: project["services"][SERVICE]}}
    with step("docker_unreadable"):  # a
        if containers(tool, project, SERVICE):
            return "already_running"
    with ExitStack() as held:
        with step("backup_running"):  # b: the deploy's order; both stay held until the end of this block
            held.enter_context(tool.backup_lock())
        with step("release_running"):
            held.enter_context(tool.lock(state))
        with step("release_unresolved"):  # c
            if tool.unresolved(state, http_state(tool, state)):
                raise Refusal("release_unresolved")
        with step("identity_mismatch"):  # d
            found = read_identity(tool, worker)
        recorded = recorded_identity(tool, state, project["name"])
        if found != recorded:
            raise Refusal("identity_mismatch")
        check_hold()  # e
        with step("previous_exit_78"):  # f
            if int(field(tool, found["container"], "{{.State.ExitCode}}")) == 78:
                raise Refusal("previous_exit_78")
        wait_for_database(tool, project, last)  # g
        deadline = min(time.monotonic() + CALLS_WAIT, last - START_RESERVE)
        check_calls(tool, settings, deadline)  # h
        with step("egress_rules_not_verified"):  # i
            if tool.run(EGRESS_CHECK, timeout=READ_TIMEOUT).strip() != "ok":
                raise Refusal("egress_rules_not_verified")
        check_hold()  # j
        check_identity(tool, worker, recorded)
        check_calls(tool, settings, deadline)
        if last - time.monotonic() < START_RESERVE:
            raise Refusal("start_failed")
        command = tool.compose(project)
        command[command.index("*")] = PROFILE
        with step("start_failed"):  # k
            tool.run(command + ["start", SERVICE], timeout=START_TIMEOUT)
        watch_start(tool, found["container"], last)
    return "started"


def record(outcome, reason):
    """Atomically; a failed write is logged and changes nothing else."""
    try:
        RECEIPT.parent.mkdir(parents=True, exist_ok=True)
        temporary = RECEIPT.with_name(RECEIPT.name + ".tmp")
        temporary.write_text(json.dumps({"checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                         "outcome": outcome, "reason": reason}) + "\n")
        temporary.chmod(0o644)
        os.replace(temporary, RECEIPT)
    except OSError as exc:
        print(f"inference worker: receipt not written ({type(exc).__name__})", file=sys.stderr, flush=True)


def main():
    try:
        outcome, reason = attempt(), None
    except Refusal as exc:
        outcome, reason = "refused", exc.reason
    print(f"inference worker: {outcome} {reason or ''}".rstrip(), flush=True)
    record(outcome, reason)
    return 1 if reason else 0


if __name__ == "__main__":
    sys.exit(main())
