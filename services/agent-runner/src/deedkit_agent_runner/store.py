"""Task state in SQLite. Every admission, launch re-check and state change is one `BEGIN IMMEDIATE` transaction,
which serializes this process's threads and the `agent-runner stop-all` CLI alike."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

ACTIVE = ("queued", "starting", "running")
TERMINAL = ("succeeded", "failed", "stopped", "timed_out", "lost")
SETTLED = ("published", "no_changes", "refused", "failed")
QUEUED_PER_ENGINE = 2
CONTENT_DAYS = 14
DEDUP_DAYS = 365

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  principal TEXT NOT NULL,
  request_id TEXT NOT NULL,
  digest TEXT NOT NULL,
  engine TEXT NOT NULL,
  state TEXT NOT NULL,
  reason TEXT,
  outcome TEXT NOT NULL DEFAULT 'unknown',
  max_minutes INTEGER NOT NULL,
  continue_task_id TEXT,
  preamble_sha256 TEXT NOT NULL,
  instruction TEXT,
  owner_instruction TEXT,
  summary TEXT,
  progress TEXT NOT NULL DEFAULT '{}',
  container TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  exit_code INTEGER,
  counts_toward_cap INTEGER NOT NULL DEFAULT 1,
  publish_state TEXT NOT NULL DEFAULT 'pending',
  publish_reason TEXT,
  branch TEXT,
  compare_url TEXT,
  volumes_removed INTEGER NOT NULL DEFAULT 0,
  content_deleted INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  starting_at REAL,
  started_at REAL,
  finished_at REAL,
  UNIQUE (principal, request_id)
);
CREATE INDEX IF NOT EXISTS tasks_state ON tasks (engine, state, created_at);
"""


class Refused(Exception):
    def __init__(self, status, code, **extra):
        super().__init__(code)
        self.status, self.code, self.extra = status, code, extra


def digest(fields):
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class Store:
    def __init__(self, path, clock):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def get(self, task_id, principal=None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or principal is not None and row["principal"] != principal:
            return None
        return dict(row)

    def task(self, task_id) -> dict:
        row = self.get(task_id)
        if row is None:
            raise KeyError(task_id)
        return row

    def by_request(self, principal, request_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE principal = ? AND request_id = ?",
                             (principal, request_id)).fetchone()
        return dict(row) if row else None

    def rows(self, *states):
        with self.connect() as db:
            marks = ",".join("?" * len(states))
            return [dict(r) for r in db.execute(
                f"SELECT * FROM tasks WHERE state IN ({marks}) ORDER BY created_at, id", states)]

    def admit(self, principal, fields, preamble_sha256, *, paused, refusal, daily_cap):
        """Dedup, pause, engine/host refusals, caps and insert in one transaction. Returns (task, created)."""
        value = digest(fields)
        now = self.clock()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE principal = ? AND request_id = ?",
                             (principal, fields["request_id"])).fetchone()
            if row is not None:
                if row["digest"] != value:
                    raise Refused(409, "request_id_conflict")
                return dict(row), False
            if paused():
                raise Refused(503, "runner_paused")
            refused = refusal(fields["engine"])
            if refused:
                raise Refused(*refused)
            if fields.get("continue_task_id"):
                base = db.execute("SELECT state, branch FROM tasks WHERE id = ? AND principal = ?",
                                  (fields["continue_task_id"], principal)).fetchone()
                if base is None or base["state"] not in TERMINAL or not base["branch"]:
                    raise Refused(422, "continue_task_invalid")
            queued = db.execute("SELECT COUNT(*) FROM tasks WHERE engine = ? AND state = 'queued'",
                                (fields["engine"],)).fetchone()[0]
            if queued >= QUEUED_PER_ENGINE:
                running = db.execute("SELECT MIN(started_at + max_minutes * 60) FROM tasks WHERE engine = ? AND "
                                     "state IN ('starting', 'running')", (fields["engine"],)).fetchone()[0]
                raise Refused(429, "runner_busy", retry_after_s=max(30, int((running or now + 300) - now)))
            counted = [r[0] for r in db.execute(
                "SELECT created_at FROM tasks WHERE counts_toward_cap = 1 AND created_at > ? ORDER BY created_at",
                (now - 86400,))]
            if len(counted) >= daily_cap:
                raise Refused(429, "daily_cap_reached", next_slot_at=counted[len(counted) - daily_cap] + 86400)
            task_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO tasks (id, principal, request_id, digest, engine, state, max_minutes, continue_task_id, "
                "preamble_sha256, instruction, owner_instruction, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
                (task_id, principal, fields["request_id"], value, fields["engine"], fields["max_minutes"],
                 fields.get("continue_task_id"), preamble_sha256, fields["instruction"],
                 fields["owner_instruction"], now))
            return dict(db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()), True

    def claim(self, task_id, container):
        """queued -> starting, with the container name recorded before anything runs."""
        with self.transaction() as db:
            changed = db.execute("UPDATE tasks SET state = 'starting', container = ?, starting_at = ? "
                                 "WHERE id = ? AND state = 'queued' AND cancel_requested = 0",
                                 (container, self.clock(), task_id)).rowcount
        return bool(changed)

    def commit_launch(self, task_id, paused):
        """The launch re-check right before the container starts: still `starting`, not cancelled, no pause file.
        starting -> running in the same transaction; otherwise the task ends `stopped` without a run."""
        with self.transaction() as db:
            row = db.execute("SELECT state, cancel_requested FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None or row["state"] != "starting":
                return False
            if row["cancel_requested"] or paused():
                db.execute("UPDATE tasks SET state = 'stopped', reason = ?, finished_at = ? WHERE id = ?",
                           ("paused" if not row["cancel_requested"] else "stop_requested", self.clock(), task_id))
                return False
            db.execute("UPDATE tasks SET state = 'running', started_at = ? WHERE id = ?", (self.clock(), task_id))
            return True

    def finish(self, task_id, state, reason=None, **fields):
        """Move a non-terminal task to a terminal state; a terminal state never changes. Returns True if moved."""
        assert state in TERMINAL
        with self.transaction() as db:
            sets = ", ".join(f"{k} = ?" for k in fields)
            changed = db.execute(
                f"UPDATE tasks SET state = ?, reason = ?, finished_at = ?{', ' + sets if sets else ''} "
                "WHERE id = ? AND state IN ('queued', 'starting', 'running')",
                (state, reason, self.clock(), *fields.values(), task_id)).rowcount
        return bool(changed)

    def request_stop(self, task_id):
        """Queued or starting: `stopped` at once (the launch re-check sees it). Running: flagged; the caller stops
        the container. Returns the state before."""
        with self.transaction() as db:
            row = db.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return None
            if row["state"] in ACTIVE:
                db.execute("UPDATE tasks SET state = 'stopped', reason = 'stop_requested', cancel_requested = 1, "
                           "finished_at = ? WHERE id = ?", (self.clock(), task_id))
            return row["state"]

    def update(self, task_id, **fields):
        """Fields that are not the state (exit code, summary, outcome, progress, publication)."""
        assert "state" not in fields
        with self.transaction() as db:
            sets = ", ".join(f"{k} = ?" for k in fields)
            db.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*fields.values(), task_id))

    def set_progress(self, task_id, progress):
        self.update(task_id, progress=json.dumps(progress, sort_keys=True))

    def starts_left(self, daily_cap):
        with self.connect() as db:
            used = db.execute("SELECT COUNT(*) FROM tasks WHERE counts_toward_cap = 1 AND created_at > ?",
                              (self.clock() - 86400,)).fetchone()[0]
        return max(0, daily_cap - used)

    def counts(self, engine):
        with self.connect() as db:
            rows = db.execute("SELECT state, COUNT(*) FROM tasks WHERE engine = ? AND state IN "
                              "('queued', 'starting', 'running') GROUP BY state", (engine,)).fetchall()
        result = {state: count for state, count in rows}
        return {"queued": result.get("queued", 0),
                "running": result.get("starting", 0) + result.get("running", 0)}

    def expired_content(self):
        cutoff = self.clock() - CONTENT_DAYS * 86400
        with self.connect() as db:
            return [r[0] for r in db.execute("SELECT id FROM tasks WHERE content_deleted = 0 AND finished_at < ?",
                                             (cutoff,))]

    def delete_content(self, task_id):
        with self.transaction() as db:
            db.execute("UPDATE tasks SET instruction = NULL, owner_instruction = NULL, summary = NULL, "
                       "progress = '{}', content_deleted = 1 WHERE id = ?", (task_id,))

    def delete_expired_rows(self):
        """Dedup rows (principal, request id, digest, task id, state) leave 365 days after the terminal state."""
        with self.transaction() as db:
            return db.execute("DELETE FROM tasks WHERE content_deleted = 1 AND finished_at < ?",
                              (self.clock() - DEDUP_DAYS * 86400,)).rowcount
