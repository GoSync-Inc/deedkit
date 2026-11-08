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
from .store import ACTIVE, SETTLED, TERMINAL, Refused, Store, launch_lock

log = logging.getLogger(__name__)
HOST_WAIT_S = 3600
RAW_LIMIT = 16 * 1024
PROGRESS_EVERY_S = 2.0
LOGIN_TTL_S = 600
PREAMBLE_NAME = "preamble.md"
START_CONFIRM_S = 120.0
START_POLL_S = 0.05
HYGIENE_PENDING = "codex-hygiene-pending"


def load_preamble():
    here = Path(__file__).with_name(PREAMBLE_NAME)
    path = here if here.exists() else Path(__file__).resolve().parents[2] / PREAMBLE_NAME
    text = path.read_text()
    return text, hashlib.sha256(text.encode()).hexdigest()


def iso(value):
    if value is None:
        return None
    return datetime.datetime.fromtimestamp(value, datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class LoginUnavailable(ValueError):
    """The runner's own copy of an engine's login is missing or unreadable."""


def claude_token(config):
    try:
        value = read_private(config.engines.claude.token_file, 4096).decode().strip()
    except (OSError, UnicodeDecodeError, ConfigError):
        raise LoginUnavailable("claude_token_unreadable") from None
    if not value or any(c.isspace() for c in value):
        raise LoginUnavailable("claude_token_unreadable")
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
        try:  # no login of any kind: a missing or broken login never disables an engine or stops the service
            code, out, err = self.docker.oneoff(
                dk.oneoff_argv(self.config, engine, dk.HELP_ARGV[engine], network=False, login=False), engine,
                login=False)
        except (dk.DockerError, ConfigError, ValueError, OSError):
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
        remaining = {name for name, _ in self.docker.labelled()}
        self.codex_hygiene()  # the last Codex task's files, before any slot frees
        for task in self.store.unreconciled():
            if task["container"] not in remaining:
                self.store.update(task["id"], container_gone=1)
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
        # Every terminal task whose container is not confirmed gone (a stop, a timeout, a failed stop earlier)
        # is stopped and removed here; its engine's slot stays held until then.
        for task in self.store.unreconciled():
            self._spawn_once(f"reconcile-{task['id']}", self._reconcile, task["id"], task["container"])
        if (self.state_dir / HYGIENE_PENDING).exists() and not self.store.slot_held("codex"):
            self._spawn_once("codex-hygiene", self.codex_hygiene)
        if self.paused():
            return
        for engine in ENGINES:
            queued = [t for t in self.store.rows("queued") if t["engine"] == engine]
            if self.store.slot_held(engine) or not queued:
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

    def _spawn_once(self, key, target, *args):
        """Spawn unless a thread with this key is still alive."""
        with self._lock:
            thread = self.threads.get(key)
            if thread is not None and thread.is_alive():
                return
        self._spawn(key, target, *args)

    def _reconcile(self, task_id, name):
        if self.docker.running(name) not in ("missing", "exited", "dead"):
            self.docker.stop(name)
        self._end(task_id, name)

    def codex_hygiene(self):
        """Empty the Codex login volume of everything but a regular auth.json. True when it verifiably did.

        Under the runner's lock, and never while a Codex task runs: a late cleanup of an earlier task can then
        not touch the files of the task that uses the volume now (that task's own launch emptied it before)."""
        if self.config.engine("codex") is None:
            return True
        with launch_lock(self.state_dir):
            if any(t["engine"] == "codex" for t in self.store.rows("running")):
                return True
            try:
                code, _, _ = self.docker.oneoff(
                    dk.oneoff_argv(self.config, "codex", dk.CODEX_HYGIENE, network=False), "codex")
            except (dk.DockerError, OSError):
                code = -1
        pending = self.state_dir / HYGIENE_PENDING
        if code:
            # Retried on every tick until it succeeds; until then every Codex task fails before it starts.
            log.warning("codex login hygiene failed (exit %s)", code)
            pending.touch(mode=0o600, exist_ok=True)
        else:
            pending.unlink(missing_ok=True)
        return code == 0

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
            if engine == "codex" and not self.codex_hygiene():
                raise dk.DockerError("codex_login_hygiene_failed")
            if self.preparer is not None:
                self.preparer(task, dk.workspace_volume(task_id))
            self.docker.create(dk.task_create_argv(self.config, task_id, engine), engine)
        except LoginUnavailable as error:  # the engine's login file is missing or unreadable
            log.warning("task %s did not start: %s", task_id, error)
            self.store.finish(task_id, "failed", "login_required", counts_toward_cap=0)
            self._end(task_id, name)
            return
        except Exception as error:  # Docker, the volumes, or a preparer hook: never left in `starting`
            log.warning("task %s did not start: %s", task_id, error)
            self.store.finish(task_id, "failed", "container_failed")
            self._end(task_id, name)
            return
        process = None
        try:
            # The re-check and the start under the runner's lock: a stop or stop-all either lands first (the
            # re-check sees it and nothing runs) or waits until the container runs (and then stops it).
            with launch_lock(self.state_dir):
                if not self.store.commit_launch(task_id, self.paused):
                    return
                process = self.docker.start_attached(name)
                if not self._confirm_start(name, process):
                    # The start's outcome is unknown: end the request and remove the created container while
                    # still holding the lock, so no delayed start can land after a stop-all that follows.
                    process.kill()
                    self.docker.remove(name)
                    if self.store.finish(task_id, "failed", "start_unconfirmed"):
                        self._audit(task_id)
                    return
            self._audit(task_id)
            if self.store.task(task_id)["state"] == "running":
                try:
                    process.stdin.write(f"{self.preamble}\n\n{task['instruction']}\n".encode())
                except OSError:
                    pass
            try:
                process.stdin.close()
            except OSError:
                pass
            self.monitor(task_id, engine, name, process)
        except Exception:
            log.exception("task %s: launch or monitoring failed", task_id)
            if self.store.finish(task_id, "failed", "runner_error"):
                self._audit(task_id)
            self.docker.stop(name)
        finally:
            self._end(task_id, name)

    def _confirm_start(self, name, process):
        """True once the container has left `created` (it runs, or already ran and exited). False when the
        start request ended without starting it, or when nothing is confirmed within START_CONFIRM_S."""
        deadline = time.monotonic() + START_CONFIRM_S
        while time.monotonic() < deadline:
            state = self.docker.running(name)
            if state in ("running", "exited", "dead", "paused", "restarting"):
                return True
            if state == "created" and process.poll() is not None:
                return False  # `docker start` gave up; the container never started
            time.sleep(START_POLL_S)
        return False

    def _lines(self, stream):
        """The engine's output in lines of at most RAW_LIMIT bytes: a longer line is cut there and the rest of
        it read and dropped, so a container that never prints a newline cannot grow the runner's memory."""
        while True:
            chunk = stream.readline(RAW_LIMIT)
            if not chunk:
                return
            if chunk.endswith(b"\n"):
                yield chunk[:-1].decode(errors="replace"), False
                continue
            while True:
                rest = stream.readline(RAW_LIMIT)
                if not rest or rest.endswith(b"\n"):
                    break
            yield chunk.decode(errors="replace"), True

    def monitor(self, task_id, engine, name, process):
        stream = engines.Stream(engine)
        task_log = TaskLog(self.state_dir / "logs" / f"{task_id}.jsonl", self.clock)
        last = 0.0
        for line, cut in self._lines(process.stdout):
            entries = [("tool", engines.clip(line, engines.TEXT_LIMIT) + " [line cut]")] if cut else stream.feed(line)
            for kind, text in entries:
                task_log.write(kind, text, line)
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

    def _end(self, task_id, name):
        """The container goes at the terminal state; its volumes when publication settles. The engine's slot is
        released only once the container is confirmed gone, and for Codex after the login volume is emptied."""
        task = self.store.get(task_id)
        if task is None:
            return
        if name:
            self.docker.remove(name)
        if not name or self.docker.running(name) == "missing":
            if task["engine"] == "codex":
                self.codex_hygiene()  # a failure here fails the next task's own hygiene, before it starts
            self.store.update(task_id, container_gone=1)
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
        if not self.store.slot_held("codex"):
            self.codex_hygiene()  # nothing a Codex task wrote outlives it, even with no later Codex task
        for task_id in self.store.expired_content():
            (self.state_dir / "logs" / f"{task_id}.jsonl").unlink(missing_ok=True)
            shutil.rmtree(self.state_dir / "patches" / task_id, ignore_errors=True)
            self.store.delete_content(task_id)
        self.store.delete_expired_rows()
        self.audit.prune()

    # -- stop ------------------------------------------------------------------------------------------------
    def stop(self, task_id, key_id=None):
        with launch_lock(self.state_dir):
            before = self.store.request_stop(task_id)
        task = self.store.task(task_id)
        if before in ACTIVE:
            self.audit.record(task, task["state"], key_id)
            if before == "running" and task["container"]:
                self._spawn_once(f"reconcile-{task_id}", self._reconcile, task_id, task["container"])
            elif before == "queued":
                self.settle(task_id)
        return task


def stop_all(store, docker, pause_file):
    """`agent-runner stop-all`: under the runner's lock, the pause file first, then every queued, starting and
    running task is marked stopped; then the running containers are stopped. Runs in its own process."""
    stopped, running = [], []
    with launch_lock(pause_file.parent):
        pause_file.touch(mode=0o600, exist_ok=True)
        for task in store.rows(*ACTIVE):
            before = store.request_stop(task["id"])
            if before == "running" and task["container"]:
                running.append(task["container"])
            if before in ACTIVE:
                stopped.append(task["id"])
    for name in running:
        docker.stop(name)
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
                except LoginUnavailable:  # the runner's own login file is missing or unreadable
                    state = "login_required"
                except (dk.DockerError, OSError):
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
