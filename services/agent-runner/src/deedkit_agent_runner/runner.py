"""One task = one engine run in one fresh container. The scheduler, the launch, the monitor, stop, crash recovery,
retention and capabilities.

Hooks for the later slices: `preparer(task, workspace_volume)` fills the workspace with the clean clone (S4,
ANYM-822 criterion 1); `publisher(task)` settles publication (S4). Until S4 is wired, nothing is cloned and
publication settles `refused` with reason `publication_not_configured`.
"""

import datetime
import hashlib
import json
import logging
import shutil
import threading
import time
from pathlib import Path

from . import docker as dk
from . import engines
from .config import ENGINES, ConfigError, read_private
from .journal import Audit, TaskLog, page
from .store import ACTIVE, SETTLED, TERMINAL, Refused, Store

log = logging.getLogger(__name__)
HOST_WAIT_S = 3600
RAW_LIMIT = 16 * 1024
PROGRESS_EVERY_S = 2.0
LOGIN_TTL_S = 600
PREAMBLE_NAME = "preamble.md"


def load_preamble():
    here = Path(__file__).with_name(PREAMBLE_NAME)
    path = here if here.exists() else Path(__file__).resolve().parents[2] / PREAMBLE_NAME
    text = path.read_text()
    return text, hashlib.sha256(text.encode()).hexdigest()


def iso(value):
    if value is None:
        return None
    return datetime.datetime.fromtimestamp(value, datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def claude_token(config):
    try:
        value = read_private(config.engines.claude.token_file, 4096).decode().strip()
    except (OSError, UnicodeDecodeError):
        raise ValueError("claude_token_unreadable") from None
    if not value or any(c.isspace() for c in value):
        raise ValueError("claude_token_unreadable")
    return {"CLAUDE_CODE_OAUTH_TOKEN": value}


def not_configured(task):
    return {"publish_state": "refused", "publish_reason": "publication_not_configured"}


class Runner:
    def __init__(self, config, docker, gate, *, clock=time.time, publisher=not_configured, preparer=None):
        self.config, self.docker, self.gate, self.clock = config, docker, gate, clock
        self.publisher, self.preparer = publisher, preparer
        self.state_dir = Path(config.state_dir)
        for sub in ("", "logs", "patches"):
            (self.state_dir / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = Store(self.state_dir / "tasks.sqlite3", clock)
        self.audit = Audit(self.state_dir / "audit.jsonl", clock)
        self.pause_file = self.state_dir / "pause"
        self.preamble, self.preamble_sha256 = load_preamble()
        self.configured, self.host_check, self.logins = {}, "failed", {}
        self.threads, self.settling, self._lock, self._stop = {}, {}, threading.Lock(), threading.Event()

    # -- startup ---------------------------------------------------------------------------------------------
    def startup(self):
        self.recover()
        for engine in ENGINES:
            self.configured[engine] = self._help_check(engine)
        self.host_check = self.gate.self_probe()

    def _help_check(self, engine):
        if self.config.engine(engine) is None:
            return False
        try:
            code, out, err = self.docker.oneoff(dk.oneoff_argv(self.config, engine, dk.HELP_ARGV[engine], False),
                                                engine)
        except dk.DockerError:
            return False
        ok = code == 0 and engines.help_ok(engine, (out + err).decode(errors="replace"), dk.REQUIRED_FLAGS)
        if not ok:
            log.warning("engine %s disabled: a pinned flag is missing from --help", engine)
        return ok

    def recover(self):
        """After a restart: every non-terminal task with a container is `lost` (its volumes stay for the export),
        every labelled container is killed and removed, `queued` stays queued. Nothing runs twice."""
        for task in self.store.rows("starting", "running"):
            if self.store.finish(task["id"], "lost", "runner_restarted"):
                self._audit(task["id"])
        for name, _task_id in self.docker.labelled():
            self.docker.kill(name)
            self.docker.remove(name)
        for task in self.store.rows(*TERMINAL):
            if task["publish_state"] == "pending" or not task["volumes_removed"]:
                self.settle(task["id"])

    # -- admission -------------------------------------------------------------------------------------------
    def paused(self):
        return self.pause_file.exists()

    def refusal(self, engine):
        if not self.configured.get(engine):
            return 409, "engine_not_configured"
        if self.host_check != "ok":
            return 503, "host_check_failed"
        return None

    def submit(self, principal, key_id, fields):
        task, created = self.store.admit(principal, fields, self.preamble_sha256, paused=self.paused,
                                         refusal=self.refusal, daily_cap=self.config.daily_starts)
        if created:
            self.audit.record(task, "queued", key_id)
        return task, created

    # -- scheduling ------------------------------------------------------------------------------------------
    def tick(self):
        now = self.clock()
        for task in self.store.rows("running"):
            if task["started_at"] and now >= task["started_at"] + task["max_minutes"] * 60:
                if self.store.finish(task["id"], "timed_out", "max_minutes"):
                    self._audit(task["id"])
                    self._spawn(f"stop-{task['id']}", self.docker.stop, task["container"])
        if self.paused():
            return
        for engine in ENGINES:
            counts = self.store.counts(engine)
            queued = [t for t in self.store.rows("queued") if t["engine"] == engine]
            if counts["running"] or not queued:
                continue
            task = queued[0]
            if not self.configured.get(engine):
                continue
            # A failed startup self-probe holds the queue like a busy host: it never launches, and ends host_busy.
            state, reason = ("busy", "host_check") if self.host_check != "ok" else self.gate.read()
            if state != "ok":
                since = json.loads(task["progress"]).get("host_wait_since") or now
                if now - since >= HOST_WAIT_S:
                    if self.store.finish(task["id"], "failed", "host_busy"):
                        self._audit(task["id"])
                        self.settle(task["id"])
                else:
                    self._progress(task["id"], waiting_for="host", host_reason=reason, host_wait_since=since)
                continue
            name = dk.container_name(task["id"])
            if self.store.claim(task["id"], name):
                self._audit(task["id"])
                self._spawn(task["id"], self.launch, task["id"])

    def _spawn(self, key, target, *args):
        thread = threading.Thread(target=self._guard, args=(target, *args), name=f"agent-{key}", daemon=True)
        with self._lock:
            self.threads[key] = thread
        thread.start()

    def _guard(self, target, *args):
        try:
            target(*args)
        except Exception:
            log.exception("agent runner thread failed")

    def join(self, timeout=30):
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                alive = [t for t in self.threads.values() if t.is_alive()]
            if not alive:
                return True
            if time.monotonic() > deadline:
                return False
            alive[0].join(0.05)

    def serve_forever(self, interval=1.0):
        last_sweep = 0.0
        while not self._stop.is_set():
            try:
                self.tick()
                if time.monotonic() - last_sweep > 3600:
                    self.sweep()
                    last_sweep = time.monotonic()
            except Exception:
                log.exception("agent runner tick failed")
            self._stop.wait(interval)

    def shutdown(self):
        self._stop.set()

    # -- one task --------------------------------------------------------------------------------------------
    def launch(self, task_id):
        task = self.store.task(task_id)
        engine, name = task["engine"], task["container"]
        try:
            self.docker.volume_create(dk.workspace_volume(task_id), self.config.workspace_volume, task_id)
            self.docker.volume_create(dk.out_volume(task_id), self.config.out_volume, task_id)
            if engine == "codex":
                code, _, _ = self.docker.oneoff(dk.oneoff_argv(self.config, engine, dk.CODEX_HYGIENE, False), engine)
                if code:
                    raise dk.DockerError("codex_login_hygiene_failed")
            if self.preparer is not None:
                self.preparer(task, dk.workspace_volume(task_id))
            self.docker.create(dk.task_create_argv(self.config, task_id, engine), engine)
        except (ConfigError, ValueError) as error:  # the engine's login file is missing or unreadable
            log.warning("task %s did not start: %s", task_id, error)
            self.store.finish(task_id, "failed", "login_required", counts_toward_cap=0)
            self._end(task_id, name)
            return
        except (dk.DockerError, OSError) as error:
            log.warning("task %s did not start: %s", task_id, error)
            self.store.finish(task_id, "failed", "container_failed")
            self._end(task_id, name)
            return
        if not self.store.commit_launch(task_id, self.paused):
            self._end(task_id, name)
            return
        self._audit(task_id)
        process = self.docker.start_attached(name)
        try:
            process.stdin.write(f"{self.preamble}\n\n{task['instruction']}\n".encode())
            process.stdin.close()
        except OSError:
            pass
        # A stop or stop-all that landed between the re-check and the start: the container is stopped now.
        for _ in range(200):
            if self.docker.running(name) != "created":
                break
            time.sleep(0.05)
        if self.store.task(task_id)["state"] != "running":
            self.docker.stop(name)
        self.monitor(task_id, engine, name, process)

    def monitor(self, task_id, engine, name, process):
        stream = engines.Stream(engine)
        task_log = TaskLog(self.state_dir / "logs" / f"{task_id}.jsonl", self.clock)
        last = 0.0
        for raw in process.stdout:
            line = raw.decode(errors="replace").rstrip("\n")
            for kind, text in stream.feed(line):
                task_log.write(kind, text, line[:RAW_LIMIT])
            if time.monotonic() - last > PROGRESS_EVERY_S:
                self._progress(task_id, **stream.progress())
                last = time.monotonic()
        process.wait()
        exit_code, oom = self.docker.exit_code(name)
        result = self.docker.copy_out(name, "/out/result.txt", engines.SUMMARY_LIMIT * 4) if engine == "codex" else None
        summary = engines.summary(stream, result)
        fields = {"exit_code": exit_code, "summary": summary, "outcome": engines.outcome(summary)}
        if exit_code == 0:
            moved = self.store.finish(task_id, "succeeded", None, **fields)
        elif stream.login_required(exit_code):
            moved = self.store.finish(task_id, "failed", "login_required", counts_toward_cap=0, **fields)
        else:
            moved = self.store.finish(task_id, "failed", "memory_limit" if oom else "engine_exit", **fields)
        if not moved:  # stopped or timed out meanwhile: the state stays, the facts are recorded
            self.store.update(task_id, **fields)
        self._progress(task_id, **stream.progress())
        self._audit(task_id)
        self._end(task_id, name)

    def _end(self, task_id, name):
        """The container goes at the terminal state; its volumes when publication settles."""
        if name:
            self.docker.remove(name)
        self.settle(task_id)

    def settle(self, task_id):
        with self._lock:
            lock = self.settling.setdefault(task_id, threading.Lock())
        with lock:  # the publisher runs at most once per task in this process
            self._settle(task_id)

    def _settle(self, task_id):
        task = self.store.get(task_id)
        if task is None or task["state"] not in TERMINAL:
            return
        if task["publish_state"] not in SETTLED:
            try:
                result = self.publisher(task)
            except Exception:
                log.exception("publication of %s failed", task_id)
                result = {"publish_state": "failed", "publish_reason": "publisher_error"}
            allowed = {k: result.get(k) for k in ("publish_state", "publish_reason", "branch", "compare_url")
                       if k in result}
            self.store.update(task_id, **allowed)
            task = self.store.task(task_id)
        if task["publish_state"] in SETTLED and not task["volumes_removed"]:
            self.docker.volume_rm(dk.workspace_volume(task_id))
            self.docker.volume_rm(dk.out_volume(task_id))
            self.store.update(task_id, volumes_removed=1)

    def _progress(self, task_id, **values):
        task = self.store.get(task_id)
        progress = json.loads(task["progress"]) if task else {}
        if "waiting_for" not in values:
            for key in ("waiting_for", "host_reason", "host_wait_since"):
                progress.pop(key, None)
        progress.update(values)
        progress["last_activity_at"] = self.clock()
        self.store.set_progress(task_id, progress)

    def _audit(self, task_id, key_id=None):
        task = self.store.get(task_id)
        if task:
            self.audit.record(task, task["state"], key_id)

    def sweep(self):
        """Retention: texts, logs and patches 14 days after the terminal state; dedup rows 365 days; audit 365
        days. Also settles terminal tasks another process stopped (stop-all) that no thread here is ending."""
        with self._lock:
            busy = {key for key, thread in self.threads.items() if thread.is_alive()}
        for task in self.store.rows(*TERMINAL):
            if task["id"] not in busy and (task["publish_state"] not in SETTLED or not task["volumes_removed"]):
                self.settle(task["id"])
        for task_id in self.store.expired_content():
            (self.state_dir / "logs" / f"{task_id}.jsonl").unlink(missing_ok=True)
            shutil.rmtree(self.state_dir / "patches" / task_id, ignore_errors=True)
            self.store.delete_content(task_id)
        self.store.delete_expired_rows()
        self.audit.prune()

    # -- stop ------------------------------------------------------------------------------------------------
    def stop(self, task_id, key_id=None):
        before = self.store.request_stop(task_id)
        task = self.store.task(task_id)
        if before in ACTIVE:
            self.audit.record(task, task["state"], key_id)
            if before == "running" and task["container"]:
                self._spawn(f"stop-{task_id}", self.docker.stop, task["container"])
            elif before == "queued":
                self.settle(task_id)
        return task


def stop_all(store, docker, pause_file):
    """`agent-runner stop-all`: the pause file first, then every queued, starting and running task is stopped.
    Runs in its own process; the server's launch re-check and post-start check see what it wrote."""
    pause_file.touch(mode=0o600, exist_ok=True)
    stopped = []
    for task in store.rows(*ACTIVE):
        before = store.request_stop(task["id"])
        if before in ("starting", "running") and task["container"]:
            docker.stop(task["container"])
        if before in ACTIVE:
            stopped.append(task["id"])
    return stopped


def view(task, clock):
    progress = json.loads(task["progress"] or "{}")
    progress["last_activity_at"] = iso(progress.get("last_activity_at"))
    progress.pop("host_wait_since", None)
    progress.setdefault("last_text", None)
    progress.setdefault("commits", 0)
    progress.setdefault("files_changed", 0)
    started = task["started_at"]
    settled = task["state"] in TERMINAL and task["publish_state"] in SETTLED
    return {
        "id": task["id"], "state": task["state"], "reason": task["reason"], "outcome": task["outcome"],
        "engine": task["engine"], "request_id": task["request_id"], "continue_task_id": task["continue_task_id"],
        "created_at": iso(task["created_at"]), "started_at": iso(started), "finished_at": iso(task["finished_at"]),
        "expected_finish_by": iso(started + task["max_minutes"] * 60) if started else None,
        "exit_code": task["exit_code"], "summary": task["summary"], "progress": progress,
        "publish_state": task["publish_state"], "publish_reason": task["publish_reason"], "branch": task["branch"],
        "compare_url": task["compare_url"], "next_action": "tell_owner" if settled else "wait",
    }


def log_page(runner, task_id, cursor, raw):
    return page(runner.state_dir / "logs" / f"{task_id}.jsonl", cursor, raw)


def capabilities(runner):
    gate_state, gate_reason = runner.gate.read()
    now = runner.clock()
    result = {}
    for engine in ENGINES:
        cached = runner.logins.get(engine)
        if cached is None or now - cached[1] > LOGIN_TTL_S:
            state = "unknown"
            if runner.configured.get(engine):
                try:
                    code, out, err = runner.docker.oneoff(
                        dk.oneoff_argv(runner.config, engine, dk.LOGIN_ARGV[engine]), engine, timeout=30)
                    state = engines.login_state(code, (out + err).decode(errors="replace"))
                except (dk.DockerError, OSError, ValueError):
                    state = "unknown"
            cached = runner.logins[engine] = (state, now)
        counts = runner.store.counts(engine)
        result[engine] = {"configured": bool(runner.configured.get(engine)), "login_state": cached[0],
                          "max_running": 1, "max_queued": 2, **counts}
    return {
        "engines": result, "daily_starts": runner.config.daily_starts,
        "starts_left": runner.store.starts_left(runner.config.daily_starts), "paused": runner.paused(),
        "host_gate": {"state": gate_state, **({"reason": gate_reason} if gate_reason else {})},
        "host_check": runner.host_check,
    }


__all__ = ["Runner", "Refused", "stop_all", "view", "log_page", "capabilities", "claude_token"]
