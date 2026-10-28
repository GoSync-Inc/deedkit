"""ANYM-449: unattended additive migrations in `deploy`. Git, Docker and the database are fakes in the deploy tests;
the catalog and rehearsal tests use the isolated PostgreSQL container (ANYMSG_TEST_POSTGRES_CONTAINER) like ANYM-588's."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import pytest

from messaging_service import __version__
from messaging_service.archive import Archive
from messaging_service.store import RUNTIME_SCHEMA
from scripts import production as p
from tests.test_production import BASE, NEWEST, NEWER, ROLLOUT, Deployer, no_host_state  # noqa: F401

PACKAGE, MIGRATIONS = p.PACKAGE, p.PACKAGE + "migrations/"
CONTAINER = os.environ.get("ANYMSG_TEST_POSTGRES_CONTAINER")
OWNER = "probe_owner"
REVISION = "9" * 40
STORE = "self.pool = ConnectionPool(dsn, min_size=1,\n    kwargs={'row_factory': dict_row, 'prepare_threshold': None})\n"
EXISTING = {f"{n:03d}_synthetic_{n}.sql": f"-- synthetic {n}\n".encode() for n in range(1, 51)}
ROLE = b"GRANT SELECT ON messages TO messaging_gateway;\n"
NOTE = b"-- rollout: expand\nALTER TABLE messages ADD COLUMN synthetic_note text;\n"
TABLE = b"-- rollout: expand\nCREATE TABLE synthetic_marks (id text PRIMARY KEY);\n"
GRANT = b"-- synthetic\nGRANT SELECT(id) ON synthetic_marks TO messaging_gateway;\n"
DSN_MARKER = "marker-dsn-password-7c2e"
SHAPE_BEFORE = [json.dumps({"k": ["relation", "public.messages"], "v": ["r"]})]
SHAPE_AFTER = SHAPE_BEFORE + [json.dumps({"k": ["column", "public.messages", "synthetic_note"],
                                          "v": ["text", False, None, "", "", "default", None]})]


EXPAND_RUN = p.expand_run  # MigrationHost replaces it; these tests call the real one


class Died(BaseException):
    """A crash: `deploy` handles every Exception, not this."""


def sha(data):
    return hashlib.sha256(data).hexdigest()


class MigrationHost(Deployer):
    """A messaging range NEWER..NEWEST: BASE has migrations 001-050, gateway_role.sql and #352's Store; the range adds
    `files` and appends `roles` (name: bytes). The live database, the copy, the rehearsal and the migration container
    are fakes; `events` records each phase."""

    def __init__(self, tmp_path, monkeypatch, files, *, roles=None, others=(), modified=(), deleted=(), configured=True):
        roles = roles or {}
        super().__init__(tmp_path, monkeypatch, changes={NEWER: [MIGRATIONS + name for name in files]
                                                         + [PACKAGE + name for name in roles] + list(others)})
        self.modified, self.deleted = {PACKAGE + name for name in roles} | set(modified), set(deleted)
        after = {**EXISTING, **files}
        self.trees = {(BASE, PACKAGE + "migrations"): set(EXISTING), (NEWEST, PACKAGE + "migrations"): set(after),
                      (BASE, PACKAGE): {"gateway_role.sql", "store.py"}, (NEWEST, PACKAGE): {"gateway_role.sql", "store.py"}}
        self.contents = {f"{BASE}:{MIGRATIONS}{name}": data for name, data in EXISTING.items()}
        self.contents.update({f"{NEWEST}:{MIGRATIONS}{name}": data for name, data in after.items()})
        self.contents.update({f"{BASE}:{PACKAGE}gateway_role.sql": ROLE, f"{NEWEST}:{PACKAGE}gateway_role.sql": ROLE,
                              f"{NEWEST}:{p.CLASSIFIER}": ROLLOUT.read_bytes(), f"{BASE}:{p.STORE_SOURCE}": STORE.encode(),
                              f"{REVISION}:{p.BACKUP_SOURCE}": b"SCHEMAS=range(2,52)\n"})
        for name, data in roles.items():
            self.contents[f"{NEWEST}:{PACKAGE}{name}"] = ROLE + data
        (tmp_path / "owner.dsn").write_text(f"postgresql://probe_owner:{DSN_MARKER}@anymsg-db-1/messaging_probe\n")
        (tmp_path / "runner.json").write_text(json.dumps({"image": "sha256:" + "5" * 64}))
        if configured:
            self.settings["migration"] = {
                "databases": {"messaging": {"container": "anymsg-db-1", "owner": OWNER, "database": "messaging_probe"}},
                "unattended": {"network": "anymsg_default", "dsn_file": str(tmp_path / "owner.dsn"),
                               "runner_config": str(tmp_path / "runner.json")},
                "receiver_schema_max": 51}
        self.version, self.applied = 50, {str(n): sha(f"-- synthetic {n}\n".encode()) for n in range(2, 51)}
        self.events, self.calls, self.die, self.answer, self.copy_failure = [], [], None, "applied", None
        self.shape, self.rehearsal = list(SHAPE_BEFORE), {"took_ms": 12, "difference": p.catalog_difference(
            p.catalog_map(SHAPE_BEFORE), p.catalog_map(SHAPE_AFTER))}
        self.pending = {f"{int(name[:3])}": sha(data) for name, data in files.items()}
        for name, fake in (("wait_for_calls", self.wait_for_calls), ("live_lines", self.live_lines),
                           ("database_copy", self.database_copy), ("rehearse", self.rehearse),
                           ("expand_run", self.expand_run), ("build_image", self.build_image)):
            monkeypatch.setattr(p, name, fake)

    def run(self, argv, **kwargs):
        if argv[:3] == ["docker", "image", "inspect"]:
            self.events.append("producer")
            return REVISION + "\n"
        command = argv[3:]
        if command[:1] == ["ls-tree"]:
            sha_, path = command[-1].split(":", 1)
            return "\0".join(sorted(self.trees[sha_, path])) + "\0"
        if command[:1] == ["diff"] and any(option.startswith("--diff-filter=") for option in command):
            kind = next(option for option in command if option.startswith("--diff-filter="))[-1]
            paths = [path for path in self.changes.get(NEWER, ()) if path not in self.deleted]
            return "\0".join(path for path in paths if (path in self.modified) == (kind == "M")) + "\0"
        found = super().run(argv, **kwargs)
        return found.decode() if isinstance(found, bytes) and not kwargs.get("binary") else found

    def wait_for_calls(self, settings, deadline):
        self.events.append("calls")
        if self.calls:
            raise p.CallActive("a call is active: " + ", ".join(self.calls))

    def live_lines(self, source, step, text):
        self.events.append("live " + step)
        if step == "ledger":
            return [json.dumps({"schema_version": str(self.version), "applied": self.applied})]
        return list(self.shape)

    @contextmanager
    def database_copy(self, settings, name, sha_, *, keep=False, catalog=None):
        assert (name, keep, catalog) == ("messaging", True, p.CATALOG)
        self.events.append("copy")
        if self.copy_failure:
            raise p.CopyFailed("copy449", self.copy_failure)
        yield {"id": "copy449", "catalog": list(self.shape), "database": "messaging_probe",
               "catalog_sha256": hashlib.sha256("\n".join(self.shape).encode()).hexdigest()}

    def rehearse(self, copied, plan, image, limit):
        self.events.append("rehearse")
        return self.rehearsal

    def build_image(self, source, sha_, target, **kwargs):
        assert target == "runtime"
        return "sha256:" + "4" * 64

    def expand_run(self, name, image, network, dsn_file, plan_file, *, label=None, status=False):
        assert network == "anymsg_default" and dsn_file == self.settings["migration"]["unattended"]["dsn_file"]
        self.events.append("status" if status else "migrate")
        if status:
            return {"schema_version": self.version}
        assert name == "deedkit-migrate-" + NEWEST[:12] and json.loads(Path(plan_file).read_text())["target"] == 51
        if self.die == "before":
            raise Died()
        if self.answer in ("applied", "lost"):
            self.version, self.applied, self.shape = 51, {**self.applied, **self.pending}, list(SHAPE_AFTER)
        if self.die == "after":
            raise Died()
        return {"schema_version": 51} if self.answer == "applied" else None if self.answer in ("lost", "silent") \
            else {"error": "lock_timeout"}

    def migration(self):
        path = self.state / NEWEST / "migration.json"
        return p.read(path) if path.exists() else None

    def plan(self):
        return p.unattended_plan(self.settings, self.state, BASE, NEWEST,
                                 [path for path in self.changes[NEWER] if p.SQL_INVENTORY.search(path)])


def phases(host):
    return [event for event in host.events if event != "calls"] + [name for name, _ in host.steps]


# spec: ANYM-449
def test_a_qualifying_range_runs_every_phase_in_order_and_records_the_migration(tmp_path, monkeypatch):
    host = MigrationHost(tmp_path, monkeypatch, {"051_synthetic_note.sql": NOTE})
    outcome = host.deploy()
    assert outcome["status"] == "deployed" and host.paused() is None
    assert phases(host) == ["live ledger", "producer", "copy", "rehearse", "live ledger", "producer", "live shape",
                            "migrate", "status", "live ledger", "live effect", "prepare", "apply", "seamless prepare",
                            "seamless promote"]
    receipt = host.migration()
    assert receipt["status"] == "migrated" and (receipt["base"], receipt["target"]) == (50, 51)
    assert receipt["files"] == ["051_synthetic_note.sql"] and receipt["copy"] == "copy449"
    plan = json.loads((host.state / NEWEST / "migrate-plan.json").read_text())
    assert plan == {"base": 50, "target": 51, "roles": {"gateway_role.sql": {"bytes": len(ROLE), "sha256": sha(ROLE)}}}


# spec: ANYM-449
def test_a_new_table_and_its_appended_grant_qualify_with_the_ranges_new_tables(tmp_path, monkeypatch):
    host = MigrationHost(tmp_path, monkeypatch, {"051_synthetic_marks.sql": TABLE}, roles={"gateway_role.sql": GRANT})
    plan = host.plan()
    assert plan["tables"] == ["synthetic_marks"] and plan["roles_changed"] == ["gateway_role.sql"]
    assert plan["roles"]["gateway_role.sql"] == {"bytes": len(ROLE), "sha256": sha(ROLE)}
    assert plan["pending"] == {"51": sha(TABLE)} and plan["ledger"] == host.applied


# spec: ANYM-449
@pytest.mark.parametrize("case", [
    "no-migration-section", "edited-applied-file", "check-widening-pair", "expand-plus-manual", "revoke",
    "gap", "schema-sql", "calendar-sql", "deploy-role-file", "new-role-file", "deleted-role-file", "no-header"])
def test_a_range_that_does_not_qualify_pauses_with_todays_text(tmp_path, monkeypatch, case):
    files, roles, others, modified, deleted = {"051_synthetic_note.sql": NOTE}, {}, [], [], []
    configured = case != "no-migration-section"
    if case == "edited-applied-file":
        others = modified = [MIGRATIONS + "039_synthetic_39.sql"]
    elif case == "check-widening-pair":
        files = {"051_synthetic_widen.sql": b"-- rollout: expand\nALTER TABLE messages DROP CONSTRAINT messages_kind_check;\n"
                 b"ALTER TABLE messages ADD CONSTRAINT messages_kind_check CHECK(kind IN ('a','b','c')) NOT VALID;\n"}
    elif case == "expand-plus-manual":
        files["052_synthetic_manual.sql"] = b"-- rollout: manual\nUPDATE messages SET body = 'marker-1';\n"
    elif case == "revoke":
        roles = {"gateway_role.sql": b"REVOKE SELECT ON messages FROM messaging_gateway;\n"}
    elif case == "gap":
        files = {"052_synthetic_note.sql": NOTE}
    elif case == "no-header":
        files = {"051_synthetic_note.sql": NOTE.split(b"\n", 1)[1]}
    elif case in ("schema-sql", "calendar-sql", "deploy-role-file"):
        others = modified = [{"schema-sql": PACKAGE + "schema.sql", "deploy-role-file": "deploy/file_understanding_role.sql",
                              "calendar-sql": "services/calendar/family_calendar/service/schema.sql"}[case]]
    elif case == "new-role-file":
        others = [PACKAGE + "synthetic_role.sql"]
    else:
        others = deleted = [PACKAGE + "gateway_role.sql"]
    host = MigrationHost(tmp_path, monkeypatch, files, roles=roles, others=others, modified=modified, deleted=deleted,
                         configured=configured)
    if case == "new-role-file":
        host.trees[NEWEST, PACKAGE].add("synthetic_role.sql")
    if case == "deleted-role-file":
        host.trees[NEWEST, PACKAGE].discard("gateway_role.sql")
    assert host.plan() is None
    outcome = host.deploy()
    sql = [path for path in host.changes[NEWER] if p.SQL_INVENTORY.search(path)]
    assert outcome["status"] == "halted" and host.steps == [] and host.events == []
    assert outcome["reason"].startswith(f"the SQL inventory changed ({', '.join(sql)}); migrate by hand (production.md)")
    assert host.migration() is None and "marker" not in outcome["reason"]


# spec: ANYM-449
@pytest.mark.parametrize("gate", ["call", "behind", "ahead", "store", "receiver"])
def test_an_admission_gate_waits_or_pauses_with_no_copy_container_or_production_write(tmp_path, monkeypatch, gate):
    host = MigrationHost(tmp_path, monkeypatch, {"051_synthetic_note.sql": NOTE})
    if gate == "call":
        host.calls = ["synthetic-call"]
    elif gate in ("behind", "ahead"):
        host.version = 49 if gate == "behind" else 51
        host.applied.pop("50") if gate == "behind" else host.applied.update(host.pending)
    elif gate == "store":
        host.contents[f"{BASE}:{p.STORE_SOURCE}"] = STORE.replace("None", "0").encode()
    else:
        del host.settings["migration"]["receiver_schema_max"]
    outcome = host.deploy()
    assert host.steps == [] and not {"copy", "migrate", "status"} & set(host.events) and host.migration() is None
    if gate == "call":
        assert outcome["status"] == "waiting" and outcome["call_waits"]["count"] == 1 and host.paused() is None
    elif gate == "receiver":
        assert outcome["status"] == "waiting" and outcome["migration_waits"]["count"] == 1 and host.paused() is None
        assert outcome["reason"].startswith("the backup receiver does not read schema 51 yet")
    else:
        assert outcome["status"] == "halted" and host.paused() == outcome["reason"]
        assert outcome["reason"] == ("the running release would fail on the new columns: deploy #352 first"
                                     if gate == "store" else
                                     "the live messaging ledger is not the base's schema 50; migrate by hand (production.md)")


# spec: ANYM-449
def test_the_third_wait_in_a_row_for_the_receiver_pauses(tmp_path, monkeypatch):
    host = MigrationHost(tmp_path, monkeypatch, {"051_synthetic_note.sql": NOTE})
    host.settings["migration"]["receiver_schema_max"] = 50
    assert [host.deploy()["status"] for _ in range(2)] == ["waiting", "waiting"]
    outcome = host.deploy()
    assert outcome["status"] == "halted" and "waited 3 runs in a row: the backup receiver does not read schema 51" \
        in outcome["reason"] and host.steps == [] and "copy" not in host.events


# spec: ANYM-449
def test_an_incompatible_producer_pin_waits_after_preparation_and_a_compatible_one_reaches_the_migration(tmp_path,
                                                                                                         monkeypatch):
    host = MigrationHost(tmp_path, monkeypatch, {"051_synthetic_note.sql": NOTE})
    host.contents[f"{REVISION}:{p.BACKUP_SOURCE}"] = b"SCHEMAS=range(2,51)\n"
    outcome = host.deploy()
    assert outcome["status"] == "waiting" and outcome["reason"].startswith(
        "the backup producer does not read schema 51 yet") and outcome["migration_waits"]["count"] == 1
    assert phases(host) == ["live ledger", "producer", "prepare"] and host.migration() is None
    # The operator moves the pin (ANYM-589 does it later): the next run takes the prepared release on.
    host.contents[f"{REVISION}:{p.BACKUP_SOURCE}"] = b"SCHEMAS=range(2,52)\n"
    assert host.deploy()["status"] == "deployed" and host.migration()["status"] == "migrated"
    assert [name for name, _ in host.steps].count("prepare") == 1


# spec: ANYM-449
@pytest.mark.parametrize("failure", ["copy", "rehearsal", "shape", "final-call"])
def test_a_failed_copy_rehearsal_or_final_gate_pauses_before_the_migration_and_is_not_retried(tmp_path, monkeypatch,
                                                                                              failure):
    host = MigrationHost(tmp_path, monkeypatch, {"051_synthetic_note.sql": NOTE})
    if failure == "copy":
        host.copy_failure = "count_mismatch"
    elif failure == "rehearsal":
        def refuse(copied, plan, image, limit):
            raise p.MigrationRefused("not additive: added index public.synthetic_body")
        monkeypatch.setattr(p, "rehearse", refuse)
    elif failure == "shape":
        real = host.live_lines
        def changed(source, step, text):
            found = real(source, step, text)
            return found + ['{"k": ["relation", "public.late"], "v": ["r"]}'] if step == "shape" else found
        monkeypatch.setattr(p, "live_lines", changed)
    else:
        real = host.rehearse
        def call_meanwhile(*args):
            host.calls = ["synthetic-call"]
            return real(*args)
        monkeypatch.setattr(p, "rehearse", call_meanwhile)
    outcome = host.deploy()
    assert "migrate" not in host.events and host.version == 50 and "apply" not in [name for name, _ in host.steps]
    if failure == "final-call":
        # A call that starts meanwhile is waited for: nothing changed, the next run copies again.
        assert outcome["status"] == "waiting" and host.migration() is None
        return
    reason = {"copy": "the pre-migration copy of messaging failed: count_mismatch",
              "rehearsal": "not additive: added index public.synthetic_body",
              "shape": "the database changed shape since its copy"}[failure]
    assert outcome["status"] == "halted" and outcome["reason"] == reason == host.paused()
    assert host.migration()["status"] == "refused" and host.migration()["reason"] == reason
    p.PAUSE.unlink()
    again = host.deploy()
    assert again["status"] == "halted" and again["reason"].startswith(f"the unattended migration of {NEWEST} ended refused")
    assert host.events.count("copy") == 1


# spec: ANYM-449
@pytest.mark.parametrize("point", ["before", "after"])
def test_a_run_killed_around_the_migration_container_is_settled_by_the_next_run_from_status(tmp_path, monkeypatch,
                                                                                            point):
    host = MigrationHost(tmp_path, monkeypatch, {"051_synthetic_note.sql": NOTE})
    host.die = point
    with pytest.raises(Died):
        host.deploy()
    assert host.migration()["status"] == "planned"
    host.die, host.events[:] = None, []
    outcome = host.deploy()
    # Nothing is retried: the next run reads --status and the ledger, never runs migrate-expand again.
    assert "migrate" not in host.events and host.events[:2] == ["status", "live ledger"]
    if point == "before":
        assert outcome["status"] == "halted" and outcome["reason"] == f"migration of {NEWEST} did not apply"
        assert host.migration()["status"] == "not-applied" and "apply" not in [name for name, _ in host.steps]
    else:
        assert outcome["status"] == "deployed" and host.migration()["status"] == "migrated"


# spec: ANYM-449
def test_a_run_killed_after_the_migrated_receipt_continues_with_plan_a(tmp_path, monkeypatch):
    host = MigrationHost(tmp_path, monkeypatch, {"051_synthetic_note.sql": NOTE})
    def killed(settings, sha_):
        raise Died()
    monkeypatch.setattr(p, "apply", killed)
    with pytest.raises(Died):
        host.deploy()
    assert host.migration()["status"] == "migrated"
    monkeypatch.setattr(p, "apply", host.step(False, "apply"))
    host.events[:] = []
    assert host.deploy()["status"] == "deployed" and not {"copy", "migrate", "status"} & set(host.events)


# spec: ANYM-449
@pytest.mark.parametrize("answer", ["lost", "silent", "refused", "uncertain", "diverged"])
def test_a_lost_or_refused_answer_is_decided_by_status_and_the_ledger(tmp_path, monkeypatch, answer):
    host = MigrationHost(tmp_path, monkeypatch, {"051_synthetic_note.sql": NOTE})
    host.answer = {"uncertain": "applied", "diverged": "applied"}.get(answer, answer)
    if answer == "uncertain":
        real = host.expand_run
        def half(*args, **kwargs):
            found = real(*args, **kwargs)
            if not kwargs.get("status"):
                host.applied.pop("51")
            return found
        monkeypatch.setattr(p, "expand_run", half)
    if answer == "diverged":
        host.rehearsal = {**host.rehearsal, "difference": {"removed": [], "changed": [], "added": []}}
    outcome = host.deploy()
    status = {"lost": "migrated", "silent": "not-applied", "refused": "not-applied", "uncertain": "uncertain",
              "diverged": "diverged"}[answer]
    assert host.migration()["status"] == status and host.events.count("migrate") == 1
    if status == "migrated":
        assert outcome["status"] == "deployed"
        return
    assert outcome["status"] == "halted" and "apply" not in [name for name, _ in host.steps]
    assert outcome["reason"] == {
        "not-applied": f"migration of {NEWEST} did not apply" + (": lock_timeout" if answer == "refused" else ""),
        "uncertain": f"the migration of {NEWEST} is uncertain; inspect the ledger before any recovery",
        "diverged": f"the migration of {NEWEST} changed messaging differently from its rehearsal; inspect it before "
                    "any recovery"}[status]
    if answer == "refused":
        assert host.migration()["code"] == "lock_timeout"


# spec: ANYM-449
def test_receipts_and_the_pause_hold_no_dsn_row_value_or_postgresql_message(tmp_path, monkeypatch):
    host = MigrationHost(tmp_path, monkeypatch, {"051_synthetic_note.sql": NOTE})
    host.answer = "refused"
    host.deploy()
    written = "".join(path.read_text() for path in [p.PAUSE, *host.state.rglob("*.json")])
    assert DSN_MARKER not in written and "postgresql://" not in written and "marker" not in written
    # The container's own output: a version or a fixed code, never PostgreSQL's text.
    printed = iter([(1, 'ERROR: relation "messages" marker-row-value\n{"error": "deadlock on marker-row-value"}\n'),
                    (0, '{"schema_version": 51}\n'), (None, "")])
    killed = []
    monkeypatch.setattr(p, "bounded", lambda argv, **kwargs: next(printed))
    monkeypatch.setattr(p, "run", lambda argv, **kwargs: killed.append(argv) or "")
    assert EXPAND_RUN("c", "i", "n", "/d", "/p") == {"error": "failed"}
    assert EXPAND_RUN("c", "i", "n", "/d", "/p") == {"schema_version": 51}
    assert EXPAND_RUN("c", "i", "n", "/d", "/p") is None and killed == [["docker", "kill", "c"]]


# spec: ANYM-449
def test_the_migration_container_runs_the_candidate_image_with_the_bounded_flags_and_read_only_mounts(monkeypatch):
    seen = []
    monkeypatch.setattr(p, "bounded", lambda argv, **kwargs: seen.append((argv, kwargs)) or (0, '{"schema_version": 51}'))
    EXPAND_RUN("deedkit-migrate-" + NEWEST[:12], "sha256:" + "4" * 64, "anymsg_default", "/opt/owner.dsn", "/s/plan.json")
    argv, kwargs = seen[0]
    assert kwargs == {"timeout": 30} and argv[:5] == ["docker", "run", "--rm", "--name", "deedkit-migrate-" + NEWEST[:12]]
    joined = " ".join(argv)
    for flag in ("--log-driver none", "--read-only", "--user 10001:10001", "--cap-drop ALL",
                 "--security-opt no-new-privileges", "--pids-limit 64", "--memory 512m", "--network anymsg_default",
                 "type=bind,src=/opt/owner.dsn,dst=/run/secrets/database_url,readonly",
                 "type=bind,src=/s/plan.json,dst=/run/deedkit/plan.json,readonly"):
        assert flag in joined
    assert argv[-7:] == ["sha256:" + "4" * 64, "python", "-m", "messaging_service", "migrate-expand", "--plan",
                         "/run/deedkit/plan.json"]


# spec: ANYM-449
def test_a_health_failure_after_the_migration_rolls_back_to_the_base_on_the_target_schema(tmp_path, monkeypatch):
    host = MigrationHost(tmp_path, monkeypatch, {"051_synthetic_note.sql": NOTE})
    def unhealthy(sha_):
        p.write(host.state / sha_ / "result.json", {"sha": sha_, "status": "unhealthy"})
        raise p.Refused("replacement did not become healthy")
    host.fail_apply = unhealthy
    outcome = host.deploy()
    assert outcome["status"] == "rolled-back" and ("rollback", NEWEST) in host.steps
    assert outcome["reason"] == (f"plan A of {NEWEST} failed its health wait after migrating schema 50 to 51; rolled "
                                 f"back to {BASE} on schema 51") == host.paused()
    assert host.migration()["status"] == "migrated" and host.events.count("migrate") == 1


# spec: ANYM-449
def test_a_dry_run_names_the_migration_and_changes_nothing(tmp_path, monkeypatch):
    host = MigrationHost(tmp_path, monkeypatch, {"051_synthetic_note.sql": NOTE})
    outcome = host.deploy(dry_run=True)
    assert outcome["status"] == "plan" and outcome["steps"][0] == (
        "migrate messaging schema 50 to 51 after a verified copy and its rehearsal")
    assert host.events == [] and host.steps == [] and host.migration() is None


# Catalog and rehearsal on the isolated PostgreSQL container.

def live():
    if not CONTAINER:
        if os.environ.get("CI"):
            pytest.fail("CI must provide the isolated PostgreSQL container")
        pytest.skip("isolated PostgreSQL container unavailable locally")


def psql(database, text):
    return subprocess.run(["docker", "exec", "-i", CONTAINER, "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                           "-U", OWNER, "-d", database], input=text, capture_output=True, text=True, check=True).stdout


SETUP = """CREATE TYPE mood AS ENUM ('calm', 'busy');
CREATE TABLE probe_metadata (name text PRIMARY KEY, value text NOT NULL);
INSERT INTO probe_metadata VALUES ('schema_version', '50');
CREATE TABLE archive_migrations (version integer PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE messages (id serial PRIMARY KEY, body text NOT NULL, mood mood DEFAULT 'calm');
INSERT INTO messages (body) SELECT 'synthetic ' || n FROM generate_series(1, 5) n;
CREATE TABLE empty_one (id bigint);
CREATE VIEW calm AS SELECT id FROM messages WHERE mood = 'calm';
DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'synthetic_reader') THEN CREATE ROLE synthetic_reader; END IF; END $$;
GRANT SELECT ON messages TO synthetic_reader;
"""
ADDITIVE = """CREATE TABLE synthetic_items (id bigserial PRIMARY KEY, message_id integer REFERENCES messages(id),
  note text NOT NULL DEFAULT 'none', at timestamptz DEFAULT now());
CREATE INDEX synthetic_items_note ON synthetic_items (note);
ALTER TABLE synthetic_items ENABLE ROW LEVEL SECURITY;
CREATE POLICY synthetic_items_read ON synthetic_items FOR SELECT TO synthetic_reader USING (true);
GRANT SELECT ON synthetic_items TO synthetic_reader;
ALTER TABLE messages ADD COLUMN synthetic_note text;
ALTER TABLE messages ADD COLUMN synthetic_rank integer NOT NULL DEFAULT 0;
GRANT SELECT (body) ON messages TO synthetic_reader;
"""


@pytest.fixture
def source_db():
    live()
    name = "anymsg_auto_" + uuid4().hex[:12] + "_test"
    psql("messaging_probe_test", f"CREATE DATABASE {name}")
    psql(name, SETUP)
    yield name
    psql("messaging_probe_test", f"DROP DATABASE {name} WITH (FORCE)")


def shape(database):
    return p.catalog_map(p.live_lines({"container": CONTAINER, "owner": OWNER, "database": database}, "test", p.CATALOG))


# spec: ANYM-449
def test_a_complete_new_table_and_nullable_or_constant_default_columns_are_additive(source_db):
    before = shape(source_db)
    psql(source_db, ADDITIVE)
    after = shape(source_db)
    assert p.not_additive(before, after) is None
    added = {tuple(key) for key, _ in p.catalog_difference(before, after)["added"]}
    for key in [("relation", "public.synthetic_items"), ("relation", "public.synthetic_items_id_seq"),
                ("index", "public.synthetic_items_note"), ("policy", "public.synthetic_items", "synthetic_items_read"),
                ("column", "public.messages", "synthetic_rank"), ("sequence", "public.synthetic_items_id_seq")]:
        assert key in added
    assert any(key[0] == "acl" and "synthetic_items" in key[1] and key[3] == "synthetic_reader" for key in added)
    assert p.not_additive(after, after) is None


FORBIDDEN = {
    "drop-column": ("ALTER TABLE messages DROP COLUMN body;", "removed column public.messages body"),
    "rename-column": ("ALTER TABLE messages RENAME COLUMN body TO text_body;", "removed column public.messages body"),
    "revoke": ("REVOKE SELECT ON messages FROM synthetic_reader;", "removed acl"),
    "enum-value": ("ALTER TYPE mood ADD VALUE 'tired';", "changed type public.mood"),
    "view-change": ("CREATE OR REPLACE VIEW calm AS SELECT id, body FROM messages WHERE mood = 'calm';",
                    "changed rule public.calm _RETURN"),
    "default-acl": ("ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO synthetic_reader;", "added default_acl"),
    "role-setting": ("ALTER ROLE synthetic_reader IN DATABASE {database} SET work_mem = '8MB';", "added role_setting"),
    "extension": ("CREATE EXTENSION citext;", "changed count"),
    "index-on-existing": ("CREATE INDEX synthetic_body ON messages (body);", "added index public.synthetic_body"),
    "constraint-on-existing": ("ALTER TABLE messages ADD CONSTRAINT synthetic_body_check CHECK (body <> '') NOT VALID;",
                               "added constraint public.messages synthetic_body_check"),
    "volatile-default": ("ALTER TABLE messages ADD COLUMN synthetic_at timestamptz DEFAULT now();",
                         "added column public.messages synthetic_at"),
    "not-null-without-default": ("ALTER TABLE empty_one ADD COLUMN must integer NOT NULL;",
                                 "added column public.empty_one must"),
    "function-and-trigger": ("CREATE FUNCTION synthetic_touch() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; "
                             "END $$; CREATE TRIGGER synthetic_touch BEFORE UPDATE ON messages FOR EACH ROW EXECUTE "
                             "FUNCTION synthetic_touch();", "added function public.synthetic_touch()"),
    "new-schema": ("CREATE SCHEMA synthetic;", "added schema synthetic"),
    "policy-on-existing": ("CREATE POLICY synthetic_all ON messages USING (true);",
                           "added policy public.messages synthetic_all"),
    "restrictive-policy": ("CREATE TABLE synthetic_new (id int); CREATE POLICY synthetic_only ON synthetic_new AS "
                           "RESTRICTIVE USING (true);", "added policy public.synthetic_new synthetic_only"),
    "new-role": ("CREATE ROLE synthetic_extra_{suffix};", "added role synthetic_extra_"),
}


# spec: ANYM-449
@pytest.mark.parametrize("case", sorted(FORBIDDEN))
def test_each_forbidden_catalog_change_is_not_additive(source_db, case):
    text, reason = FORBIDDEN[case]
    suffix = uuid4().hex[:8]
    before = shape(source_db)
    psql(source_db, text.format(database=source_db, suffix=suffix))
    try:
        found = p.not_additive(before, shape(source_db))
    finally:
        if case == "new-role":
            psql(source_db, f"DROP ROLE synthetic_extra_{suffix};")
    assert found is not None and found.startswith(reason)


def data(database):
    lines = p.live_lines({"container": CONTAINER, "owner": OWNER, "database": database}, "test", p.MIGRATION_DATA)
    for _ in range(10):
        time.sleep(0.5)
        again = p.live_lines({"container": CONTAINER, "owner": OWNER, "database": database}, "test", p.MIGRATION_DATA)
        if again == lines:
            break
        lines = again
    return p.catalog_map(lines)


LEDGER_ROW = "INSERT INTO archive_migrations (version, sha256) VALUES (51, '{digest}'); " \
             "UPDATE probe_metadata SET value = '51' WHERE name = 'schema_version';"
PLAN = {"base": 50, "target": 51, "pending": {"51": "a" * 64}}


# spec: ANYM-449
@pytest.mark.parametrize("write", ["none", "delivery-features", "existing-table"])
def test_the_data_check_allows_only_the_migrations_own_ledger_rows(source_db, write):
    before = data(source_db)
    extra = {"none": "", "delivery-features": "INSERT INTO probe_metadata VALUES ('delivery_features', 'on');",
             "existing-table": "UPDATE messages SET body = body WHERE id = 1;"}[write]
    psql(source_db, "BEGIN; " + LEDGER_ROW.format(digest="a" * 64) + extra + " SELECT pg_stat_force_next_flush(); COMMIT;")
    found = p.data_unchanged(before, data(source_db), PLAN)
    assert found == {"none": None, "delivery-features": "probe_metadata changed beyond schema_version",
                     "existing-table": "rows of public.messages were written"}[write]


@pytest.fixture
def copy_settings(tmp_path, monkeypatch, source_db):
    monkeypatch.setattr(p, "filesystem", lambda path: (1 << 40, 1 << 40))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    settings = {"version": 1, "state_directory": str(state), "migration": {
        "databases": {"messaging": {"container": CONTAINER, "owner": OWNER, "database": source_db}}}}
    yield settings
    for directory in sorted((state / p.COPIES).iterdir()) if (state / p.COPIES).exists() else ():
        subprocess.run(["docker", "stop", "-t", "1", "deedkit-copy-" + directory.name], capture_output=True)


def labelled(copy_id, kind):
    listing = ["docker", "ps", "-a", "-q"] if kind == "container" else ["docker", "network", "ls", "-q"]
    return subprocess.run([*listing, "--filter", "label=deedkit-copy=" + copy_id], capture_output=True, text=True,
                          check=True).stdout.split()


def fake_migrate(migration, *, slow=False):
    """migrate-expand as a psql session on the copy over the rehearsal network: the migration, its ledger row, the
    schema version, in one transaction."""
    calls = []
    def expand_run(name, image, network, dsn_file, plan_file, *, label=None, status=False):
        calls.append((name, network, label))
        host = Path(dsn_file).read_text().split("@")[1].split(":")[0]
        text = "BEGIN; " + migration + LEDGER_ROW.format(digest="a" * 64) + (" SELECT pg_sleep(1.2);" if slow else "") \
            + " COMMIT;"
        subprocess.run(["docker", "run", "--rm", "--network", network, "--label", label, "-i", image, "psql", "-X", "-q",
                        "-v", "ON_ERROR_STOP=1", "-h", host, "-U", OWNER, "-d", Path(dsn_file).read_text().strip()
                        .rsplit("/", 1)[1]], input=text, capture_output=True, text=True, check=True)
        return {"schema_version": 51}
    return expand_run, calls


def scratch_image():
    return json.loads(subprocess.run(["docker", "inspect", "--format", "{{json .Image}}", CONTAINER], capture_output=True,
                                     text=True, check=True).stdout)


# spec: ANYM-449
@pytest.mark.parametrize("case", ["additive", "delivery-features", "slow"])
def test_the_rehearsal_runs_on_the_copy_over_an_internal_network_and_leaves_nothing(copy_settings, monkeypatch, case):
    migration = ADDITIVE + ("INSERT INTO probe_metadata VALUES ('delivery_features', 'on');"
                            if case == "delivery-features" else "")
    expand_run, calls = fake_migrate(migration, slow=case == "slow")
    monkeypatch.setattr(p, "expand_run", expand_run)
    plan = {**PLAN, "roles": {}}
    with p.database_copy(copy_settings, "messaging", NEWEST, keep=True, catalog=p.CATALOG) as copied:
        copy_id = copied["id"]
        if case == "additive":
            record = p.rehearse(copied, plan, scratch_image(), 1000)
        else:
            with pytest.raises(p.MigrationRefused) as refused:
                p.rehearse(copied, plan, scratch_image(), 1000)
        network = subprocess.run(["docker", "network", "inspect", "--format", "{{.Internal}}", "deedkit-copy-" + copy_id],
                                 capture_output=True, text=True, check=True).stdout.strip()
        owner = json.loads((Path(copied["copy"]["directory"]) / "owner.json").read_text())
    assert network == "true" and calls[0][1:] == ("deedkit-copy-" + copy_id, "deedkit-copy=" + copy_id)
    assert {"kind": "network", "name": "deedkit-copy-" + copy_id} in owner["resources"]
    assert {"kind": "container", "name": f"deedkit-copy-{copy_id}-migrate"} in owner["resources"]
    assert labelled(copy_id, "container") == [] and labelled(copy_id, "network") == []
    if case == "additive":
        assert 0 <= record["took_ms"] <= 1000
        assert ["relation", "public.synthetic_items"] in [key for key, _ in record["difference"]["added"]]
    else:
        assert str(refused.value).startswith({"delivery-features": "not additive: probe_metadata changed beyond "
                                                                  "schema_version",
                                              "slow": "too slow for an unattended lock: the rehearsal took"}[case])
    # The live database itself never changed.
    assert psql(copy_settings["migration"]["databases"]["messaging"]["database"],
                "SELECT value FROM probe_metadata WHERE name = 'schema_version';").strip() == "50"


# spec: ANYM-449
def test_a_rehearsal_killed_midway_leaves_resources_the_next_run_removes(copy_settings, monkeypatch):
    release = p.release_copy
    monkeypatch.setattr(p, "release_copy", lambda copy: None)  # the process dies: its finally never runs
    with p.database_copy(copy_settings, "messaging", NEWEST, keep=True) as copied:
        copy, network = copied["copy"], "deedkit-copy-" + copied["id"]
        p.claim(copy, "network", network)
        subprocess.run(["docker", "network", "create", "--internal", "--label", "deedkit-copy=" + copy["id"], network],
                       capture_output=True, check=True)
        p.claim(copy, "container", network + "-migrate")
        subprocess.run(["docker", "create", "--name", network + "-migrate", "--label", "deedkit-copy=" + copy["id"],
                        "--network", network, scratch_image(), "true"], capture_output=True, check=True)
    assert labelled(copy["id"], "network") and len(labelled(copy["id"], "container")) == 2
    monkeypatch.setattr(p, "release_copy", release)
    p.tend_copies(copy_settings, locked=False)
    assert labelled(copy["id"], "container") == [] and labelled(copy["id"], "network") == []
    assert (Path(copy["directory"]) / "released.json").exists()



def event(**changes):
    value={'account':'synthetic-449','platform':'telegram','chat':'449','chat_title':'Synthetic 449 room',
           'provider_id':'1','sender':'42','body':'before the migration','sent_at':'2026-10-01T12:00:00+00:00',
           'updated_at':'2026-10-01T12:00:00+00:00','deleted':False,'direction':'incoming','origin':'live','media':[]}
    value.update(changes)
    return value


# spec: ANYM-449
def test_the_base_runtime_reads_writes_and_delivers_on_a_schema_one_additive_migration_ahead(seeded,tmp_path,monkeypatch):
    s,_,_=seeded
    ar=Archive(s)
    ar.register('synthetic-449','telegram','Synthetic 449',include_chats=['449'])
    key=s.issue_key('synthetic-449-reader',['synthetic-449'])
    permissions=['read','subscription-write','subscription-ack','delivery-write']
    token=s.issue_key('synthetic-449-agent',['synthetic-449'],permissions=permissions,key_permissions=permissions)
    policy=tmp_path/'delivery-policy.json'
    policy.write_text(json.dumps({'allow_hosts':['example.test']}))
    policy.chmod(0o600)
    monkeypatch.setenv('DELIVERY_POLICY_FILE',str(policy))
    with s.pool.connection() as c:
        row=c.execute("SELECT value FROM probe_metadata WHERE name='delivery_features'").fetchone()
    previous=row and row['value']
    try:
        # Before: the SELECT * read-for-update (create, then edit) and a search run on the pooled connections.
        assert ar.ingest(event())['outcome']=='created'
        assert ar.ingest(event(body='edited before',updated_at='2026-10-01T12:01:00+00:00'))['outcome']=='edited'
        assert [x['body'] for x in s.search(key)['items']]==['edited before']
        # One synthetic additive migration: a new column on tables the runtime reads with SELECT *, and a new table.
        with s.pool.connection() as c:
            c.execute('ALTER TABLE messages ADD COLUMN synthetic_449 text')
            c.execute('ALTER TABLE delivery_endpoints ADD COLUMN synthetic_449 text')
            c.execute('CREATE TABLE synthetic_449_items (id bigserial PRIMARY KEY, note text)')
            c.execute("INSERT INTO synthetic_449_items(note) VALUES('synthetic')")
        # After: the same read, a write that reads the old row with SELECT * (edit), and a new message.
        assert [x['body'] for x in s.search(key)['items']]==['edited before']
        assert ar.ingest(event(body='edited after',updated_at='2026-10-01T12:02:00+00:00'))['outcome']=='edited'
        assert ar.ingest(event(provider_id='2',body='created after'))['outcome']=='created'
        assert sorted(x['body'] for x in s.search(key)['items'])==['created after','edited after']
        with s.pool.connection() as c:
            assert c.execute("SELECT count(*) AS n FROM messages WHERE synthetic_449 IS NULL AND body IN ('edited after','created after')").fetchone()['n']==2
            assert c.execute('SELECT note FROM synthetic_449_items').fetchall()==[{'note':'synthetic'}]
            for name in ('api','matcher','dispatcher'):
                c.execute('''INSERT INTO component_heartbeats(component,version,schema_version,beat_at)
                    VALUES(%s,%s,%s,clock_timestamp()) ON CONFLICT(component) DO UPDATE SET version=EXCLUDED.version,
                    schema_version=EXCLUDED.schema_version,beat_at=EXCLUDED.beat_at''',(name,__version__,RUNTIME_SCHEMA))
        # An enabled delivery: the heartbeat-gated enable, an endpoint registered and listed, then disabled again.
        assert s.set_delivery_features(True,verify_components=True)=={'delivery_features':'enabled','cancelled':0}
        created=s.register_delivery_endpoint(token,'synthetic-449-request','https://hooks.example.test/synthetic',
            'whsec_c3ludGhldGljLTQ0OS1zZWNyZXQtMDEyMzQ1Njc4OQ==',resolver=lambda *a,**k:['93.184.216.34'])
        assert [x['endpoint_id'] for x in s.list_delivery_endpoints(token)['items']]==[created['endpoint_id']]
        assert s.set_delivery_features(False)['delivery_features']=='disabled'
    finally:
        with s.pool.connection() as c:
            c.execute('DROP TABLE IF EXISTS synthetic_449_items')
            c.execute('ALTER TABLE messages DROP COLUMN IF EXISTS synthetic_449')
            c.execute('ALTER TABLE delivery_endpoints DROP COLUMN IF EXISTS synthetic_449')
            c.execute("DELETE FROM component_heartbeats WHERE component IN ('api','matcher','dispatcher')")
            if previous is None:
                c.execute("DELETE FROM probe_metadata WHERE name='delivery_features'")
            else:
                c.execute("UPDATE probe_metadata SET value=%s WHERE name='delivery_features'",(previous,))
