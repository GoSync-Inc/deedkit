"""ANYM-449: unattended additive migrations in `deploy`. Git, Docker and the database are fakes in the deploy tests;
the catalog and rehearsal tests use the isolated PostgreSQL container (ANYMSG_TEST_POSTGRES_CONTAINER) like ANYM-588's."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from messaging_service import __version__
from messaging_service.archive import Archive
from messaging_service.event_delivery import dispatch_hints
from messaging_service.subscriptions import match_subscriptions
from messaging_service.store import RUNTIME_SCHEMA, Store
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
        # NEWER has the range's content too, so either commit can be the candidate.
        self.trees.update({(NEWER, path): names for (sha_, path), names in list(self.trees.items()) if sha_ == NEWEST})
        self.contents.update({NEWER + key[40:]: data for key, data in list(self.contents.items()) if key[:40] == NEWEST})
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
        self.settled, self.identity, self.reached = True, ["7000000000000000001", "messaging_probe", "16384"], None
        self.shape, self.shape_after = list(SHAPE_BEFORE), list(SHAPE_AFTER)
        self.rehearsal = {"took_ms": 12, "difference": p.catalog_difference(
            p.digested(p.catalog_map(SHAPE_BEFORE)), p.digested(p.catalog_map(SHAPE_AFTER)))}
        self.pending = {f"{int(name[:3])}": sha(data) for name, data in files.items()}
        self.target = 50 + len(files)
        for name, fake in (("wait_for_calls", self.wait_for_calls), ("live_lines", self.live_lines),
                           ("database_copy", self.database_copy), ("rehearse", self.rehearse),
                           ("expand_run", self.expand_run), ("build_image", self.build_image),
                           ("migration_settled", lambda source, name: self.settled),
                           ("database_identity", lambda name, image, network, dsn_file: self.reached or self.identity)):
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
        if step == "identity":
            return [json.dumps(self.identity)]
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
        written = json.loads(Path(plan_file).read_text())
        assert name.startswith("deedkit-migrate-") and written["target"] == self.target
        assert written["identity"] == self.identity  # its connection checks the copied database (Astra r2 F3)
        if self.die == "before":
            raise Died()
        if self.answer in ("applied", "lost"):
            self.version, self.applied, self.shape = self.target, {**self.applied, **self.pending}, list(self.shape_after)
        if self.die == "after":
            raise Died()
        return {"schema_version": self.target} if self.answer == "applied" else None if self.answer in ("lost", "silent") \
            else {"error": "lock_timeout"}

    def migration(self, release=NEWEST):
        path = self.state / release / "migration.json"
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
                            "live identity", "migrate", "status", "live ledger", "live effect", "prepare", "apply", "seamless prepare",
                            "seamless promote"]
    receipt = host.migration()
    assert receipt["status"] == "migrated" and (receipt["base"], receipt["target"]) == (50, 51)
    assert receipt["files"] == ["051_synthetic_note.sql"] and receipt["copy"] == "copy449"
    plan = json.loads((host.state / NEWEST / "migrate-plan.json").read_text())
    assert plan == {"base": 50, "target": 51, "roles": {"gateway_role.sql": {"bytes": len(ROLE), "sha256": sha(ROLE)}},
                    "identity": host.identity}


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
CREATE TYPE synthetic_pair AS (a integer);
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


# spec: ANYM-449
def test_literal_defaults_with_the_casts_postgresql_adds_are_constant(source_db):
    before = shape(source_db)
    psql(source_db, "ALTER TABLE messages ADD COLUMN s_big bigint NOT NULL DEFAULT 0, ADD COLUMN s_small smallint DEFAULT 1,"
                    " ADD COLUMN s_num numeric(10,2) NOT NULL DEFAULT 0, ADD COLUMN s_neg integer DEFAULT -1,"
                    " ADD COLUMN s_text varchar(20) NOT NULL DEFAULT 'pending', ADD COLUMN s_json jsonb DEFAULT '{}',"
                    " ADD COLUMN s_flag boolean NOT NULL DEFAULT false, ADD COLUMN s_mood mood DEFAULT 'busy';")
    after = shape(source_db)
    assert p.not_additive(before, after) is None
    assert len([key for key, _ in p.catalog_difference(before, after)["added"] if key[0] == "column"]) == 8


# spec: ANYM-449
def test_the_identity_the_dsn_reaches_is_the_identity_docker_exec_reads(source_db, tmp_path):
    live()
    dsn = Path(os.environ["PROBE_TEST_DSN_FILE"]).read_text().strip().rsplit("/", 1)[0] + "/" + source_db
    (tmp_path / "dsn").write_text(dsn + "\n")
    reached = subprocess.run([sys.executable, "-c", p.IDENTITY], env={**os.environ, "DATABASE_URL_FILE": str(tmp_path / "dsn")},
                             capture_output=True, text=True, check=True).stdout
    found = json.loads(p.live_lines({"container": CONTAINER, "owner": OWNER, "database": source_db}, "identity",
                                    p.IDENTITY_SQL + ";")[0])
    assert json.loads(reached) == found and found[1] == source_db and len(found) == 3


# spec: ANYM-449
def test_a_default_behind_a_users_assignment_cast_is_not_constant(source_db):
    # Astra r2 F5: pg_get_expr prints an implicit cast's argument only, so this default reads `0`.
    psql(source_db, "CREATE TYPE synthetic_wrap AS (n integer); CREATE FUNCTION synthetic_wrap_of(integer) RETURNS "
                    "synthetic_wrap VOLATILE LANGUAGE sql AS 'SELECT ROW($1)::synthetic_wrap'; CREATE CAST (integer AS "
                    "synthetic_wrap) WITH FUNCTION synthetic_wrap_of(integer) AS ASSIGNMENT;")
    before = shape(source_db)
    psql(source_db, "ALTER TABLE messages ADD COLUMN s_wrap synthetic_wrap DEFAULT 0;")
    after = shape(source_db)
    added = dict((tuple(key), value) for key, value in p.catalog_difference(before, after)["added"])
    assert added[("column", "public.messages", "s_wrap")][2] == "0"
    assert p.not_additive(before, after) == "added column public.messages s_wrap"


# spec: ANYM-449
def test_a_constant_default_of_a_quoted_type_name_pauses_as_a_known_case(source_db):
    # Astra r3 F11, narrowed: the printed text must match LITERAL_DEFAULT as well as the vetted tree, so a quoted
    # type name pauses and the release goes the manual path.
    before = shape(source_db)
    psql(source_db, 'ALTER TABLE messages ADD COLUMN s_char "char" DEFAULT \'x\';')
    after = shape(source_db)
    added = dict((tuple(key), value) for key, value in p.catalog_difference(before, after)["added"])
    assert added[("column", "public.messages", "s_char")][2] == "'x'::\"char\""
    assert added[("column", "public.messages", "s_char")][6] is True
    assert p.not_additive(before, after) == "added column public.messages s_char"


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
    "composite-attribute": ("ALTER TYPE synthetic_pair ADD ATTRIBUTE b text;", "added column public.synthetic_pair b"),
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
        calls.append((name, network, label, json.loads(Path(plan_file).read_text())["identity"]))
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
            record = p.rehearse(copied, plan, scratch_image(), 4000)  # docker teardown sits inside the window
        else:
            with pytest.raises(p.MigrationRefused) as refused:
                p.rehearse(copied, plan, scratch_image(), 1000)
        network = subprocess.run(["docker", "network", "inspect", "--format", "{{.Internal}}", "deedkit-copy-" + copy_id],
                                 capture_output=True, text=True, check=True).stdout.strip()
        owner = json.loads((Path(copied["copy"]["directory"]) / "owner.json").read_text())
    assert network == "true" and calls[0][1:3] == ("deedkit-copy-" + copy_id, "deedkit-copy=" + copy_id)
    # The plan names the scratch database it rehearses on (Astra r2 F3).
    assert len(calls[0][3]) == 3 and calls[0][3][1] == copied["database"]
    assert {"kind": "network", "name": "deedkit-copy-" + copy_id} in owner["resources"]
    assert {"kind": "container", "name": f"deedkit-copy-{copy_id}-migrate"} in owner["resources"]
    assert labelled(copy_id, "container") == [] and labelled(copy_id, "network") == []
    if case == "additive":
        assert 0 <= record["took_ms"] <= 4000
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
    dsn=Path(os.environ['PROBE_TEST_DSN_FILE']).read_text().strip()
    ar=Archive(s)
    ar.register('synthetic-449','telegram','Synthetic 449',include_chats=['449'])
    key=s.issue_key('synthetic-449-reader',['synthetic-449'])
    permissions=['read','subscription-write','subscription-ack','delivery-write']
    token=s.issue_key('synthetic-449-agent',['telegram-demo'],permissions=permissions,key_permissions=permissions)
    secret='whsec_c3ludGhldGljLTQ0OS1zZWNyZXQtMDEyMzQ1Njc4OQ=='
    public=lambda *a,**k:['93.184.216.34']  # noqa: E731
    policy=tmp_path/'delivery-policy.json'
    policy.write_text(json.dumps({'allow_hosts':['example.test']}))
    policy.chmod(0o600)
    monkeypatch.setenv('DELIVERY_POLICY_FILE',str(policy))
    ahead=RUNTIME_SCHEMA+1
    with s.pool.connection() as c:
        row=c.execute("SELECT value FROM probe_metadata WHERE name='delivery_features'").fetchone()
        recorded=c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']
    previous=row and row['value']
    restarted=None
    try:
        # Before: SELECT * read-for-update (create, then edit), a search, and delivery enabled with a bound subscription.
        assert ar.ingest(event())['outcome']=='created'
        assert ar.ingest(event(body='edited before',updated_at='2026-10-01T12:01:00+00:00'))['outcome']=='edited'
        assert [x['body'] for x in s.search(key)['items']]==['edited before']
        with s.pool.connection() as c:
            for name in ('api','matcher','dispatcher'):
                c.execute('''INSERT INTO component_heartbeats(component,version,schema_version,beat_at)
                    VALUES(%s,%s,%s,clock_timestamp()) ON CONFLICT(component) DO UPDATE SET version=EXCLUDED.version,
                    schema_version=EXCLUDED.schema_version,beat_at=EXCLUDED.beat_at''',(name,__version__,RUNTIME_SCHEMA))
        assert s.set_delivery_features(True,verify_components=True)=={'delivery_features':'enabled','cancelled':0}
        endpoint=s.register_delivery_endpoint(token,'synthetic-449-request','https://hooks.example.test/synthetic',secret,resolver=public)
        sid=s.create_subscription(token,['telegram-demo-chat'],delivery={'endpoint_id':endpoint['endpoint_id']})['subscription_id']
        # The migration the new release applies: additive DDL, its ledger row and the advanced schema_version.
        with s.pool.connection() as c:
            c.execute('ALTER TABLE messages ADD COLUMN synthetic_449 text')
            c.execute('ALTER TABLE delivery_endpoints ADD COLUMN synthetic_449 text')
            c.execute('CREATE TABLE synthetic_449_items (id bigserial PRIMARY KEY, note text)')
            c.execute("INSERT INTO synthetic_449_items(note) VALUES('synthetic')")
            c.execute('INSERT INTO archive_migrations(version,sha256) VALUES(%s,%s)',(ahead,hashlib.sha256(b'synthetic-449').hexdigest()))
            c.execute("UPDATE probe_metadata SET value=%s WHERE name='schema_version'",(str(ahead),))
        # The base runtime restarts on the advanced ledger: a new Store, and serve never runs migrate().
        restarted=Store(dsn,tmp_path/'restarted-originals','http://testserver')
        ar2=Archive(restarted)
        assert [x['body'] for x in restarted.search(key)['items']]==['edited before']
        assert ar2.ingest(event(body='edited after',updated_at='2026-10-01T12:02:00+00:00'))['outcome']=='edited'
        assert ar2.ingest(event(provider_id='2',body='created after'))['outcome']=='created'
        assert sorted(x['body'] for x in restarted.search(key)['items'])==['created after','edited after']
        with restarted.pool.connection() as c:
            assert c.execute("SELECT count(*) AS n FROM messages WHERE synthetic_449 IS NULL AND body IN ('edited after','created after')").fetchone()['n']==2
            assert c.execute('SELECT note FROM synthetic_449_items').fetchall()==[{'note':'synthetic'}]
            assert c.execute("SELECT value FROM probe_metadata WHERE name='delivery_features'").fetchone()['value']=='enabled'
        # Delivery stayed enabled: one synthetic event is matched, staged and sent to the fake receiver by the restarted runtime.
        assert [x['endpoint_id'] for x in restarted.list_delivery_endpoints(token)['items']]==[endpoint['endpoint_id']]
        with restarted.pool.connection() as c:
            restarted.append_event(c,'telegram-demo','demo-tg-1',origin='live')
        match_subscriptions(restarted)
        received=[]
        assert dispatch_hints(restarted,transport=lambda url,body,headers,**k:received.append((url,headers,body)) or 204,
            resolver=public)['delivered']==1
        # The registration's queued endpoint test goes out in the same pass; the hint is the one naming the subscription.
        assert {url for url,_,_ in received}=={'https://hooks.example.test/synthetic'}
        assert [json.loads(body).get('subscription_id') for _,_,body in received].count(sid)==1
        assert restarted.set_delivery_features(False)['delivery_features']=='disabled'
    finally:
        if restarted is not None:
            restarted.close()
        with s.pool.connection() as c:
            c.execute('DROP TABLE IF EXISTS synthetic_449_items')
            c.execute('ALTER TABLE messages DROP COLUMN IF EXISTS synthetic_449')
            c.execute('ALTER TABLE delivery_endpoints DROP COLUMN IF EXISTS synthetic_449')
            c.execute('DELETE FROM archive_migrations WHERE version=%s',(ahead,))
            c.execute("UPDATE probe_metadata SET value=%s WHERE name='schema_version'",(recorded,))
            c.execute("DELETE FROM component_heartbeats WHERE component IN ('api','matcher','dispatcher')")
            if previous is None:
                c.execute("DELETE FROM probe_metadata WHERE name='delivery_features'")
            else:
                c.execute("UPDATE probe_metadata SET value=%s WHERE name='delivery_features'",(previous,))


ROLE_ONLY = {"gateway_role.sql": b"GRANT INSERT ON messages TO messaging_gateway;\n"}


# spec: ANYM-449
@pytest.mark.parametrize("point", ["before", "after"])
def test_a_role_only_release_killed_around_its_container_is_settled_by_the_catalog_effect(tmp_path, monkeypatch, point):
    host = MigrationHost(tmp_path, monkeypatch, {}, roles=ROLE_ONLY)
    host.die = point
    with pytest.raises(Died):
        host.deploy()
    assert host.migration()["status"] == "planned" and (host.migration()["base"], host.migration()["target"]) == (50, 50)
    host.die = None
    outcome = host.deploy()
    # The ledger is the base's either way; only the catalog says whether the grants were applied (Astra r1 F2).
    if point == "before":
        assert outcome["status"] == "halted" and outcome["reason"] == f"migration of {NEWEST} did not apply"
    else:
        assert outcome["status"] == "deployed" and host.migration()["status"] == "migrated"
    assert host.events.count("migrate") == 1


# spec: ANYM-449
def test_a_lost_answer_while_its_session_may_still_run_stays_uncertain(tmp_path, monkeypatch):
    host = MigrationHost(tmp_path, monkeypatch, {"051_synthetic_note.sql": NOTE})
    host.answer, host.settled = "silent", False
    outcome = host.deploy()
    assert outcome["status"] == "halted" and outcome["reason"] == (
        f"the migration of {NEWEST} is uncertain; inspect the ledger before any recovery")
    # Never `not-applied` while the container or its session may still commit (Astra r1 F1).
    assert host.migration()["status"] == "uncertain" and "status" not in host.events


# spec: ANYM-449
def test_a_dsn_that_reaches_another_database_pauses_before_the_migration(tmp_path, monkeypatch):
    host = MigrationHost(tmp_path, monkeypatch, {"051_synthetic_note.sql": NOTE})
    host.reached = ["7000000000000000001", "messaging_other", "16999"]  # the same ledger, another database
    outcome = host.deploy()
    assert outcome["status"] == "halted" and outcome["reason"] == "the owner DSN file does not reach the copied database"
    assert "migrate" not in host.events and host.migration()["status"] == "refused" and host.version == 50


# spec: ANYM-449
@pytest.mark.parametrize("point", ["after", "migrated"])
def test_an_unsettled_migration_is_finished_before_a_newer_candidate(tmp_path, monkeypatch, point):
    host = MigrationHost(tmp_path, monkeypatch, {"051_synthetic_note.sql": NOTE})
    host.green = {NEWER}
    if point == "after":
        host.die = "after"
    else:
        monkeypatch.setattr(p, "apply", lambda settings, sha_: (_ for _ in ()).throw(Died()))
    with pytest.raises(Died):
        host.deploy()
    assert host.migration(NEWER)["status"] == ("planned" if point == "after" else "migrated")
    host.die, host.green = None, {NEWER, NEWEST}
    monkeypatch.setattr(p, "apply", host.step(False, "apply"))
    outcome = host.deploy()
    # NEWEST is attested meanwhile, yet the run finishes NEWER, whose migration may have changed the database.
    assert outcome["status"] == "deployed" and outcome["sha"] == NEWER and host.migration(NEWER)["status"] == "migrated"
    assert ("apply", NEWER) in host.steps and host.events.count("migrate") == 1


# spec: ANYM-449
def test_a_network_that_appears_after_its_copy_was_released_is_removed(copy_settings):
    with p.database_copy(copy_settings, "messaging", NEWEST) as copied:
        copy_id = copied["id"]
    # A network create a killed rehearsal left in flight completes after the release (Astra r1 F7).
    subprocess.run(["docker", "network", "create", "--internal", "--label", "deedkit-copy=" + copy_id,
                    "deedkit-copy-" + copy_id], capture_output=True, check=True)
    assert labelled(copy_id, "network")
    p.tend_copies(copy_settings, locked=False)
    assert labelled(copy_id, "network") == []


# spec: ANYM-449
def test_the_recovery_catalog_holds_digests_never_a_catalog_value(tmp_path, monkeypatch):
    host = MigrationHost(tmp_path, monkeypatch, {"051_synthetic_note.sql": NOTE})
    setting = json.dumps({"k": ["role_setting", "", OWNER], "v": [f"app.dsn=postgresql://u:{DSN_MARKER}@h/d"]})
    host.shape, host.shape_after = [*SHAPE_BEFORE, setting], [*SHAPE_AFTER, setting]
    host.rehearsal["difference"] = p.catalog_difference(p.digested(p.catalog_map(host.shape)),
                                                        p.digested(p.catalog_map(host.shape_after)))
    assert host.deploy()["status"] == "deployed" and host.migration()["status"] == "migrated"
    written = (host.state / NEWEST / "migration-catalog.json").read_text()
    # A role setting, a default or a policy expression reaches the file only as a digest (Astra r2 F10).
    assert DSN_MARKER not in written and "postgresql://" not in written and "synthetic_note" in written


# ANYM-586 K3: calendar on the same path, and a range touching both databases.

CALENDAR_PACKAGE = p.CALENDAR_PACKAGE
CALENDAR_ROLLOUT = ROLLOUT.parents[3] / "services/calendar/family_calendar/service/migration_rollout.py"
CALENDAR_FILES = {"schema.sql": b"CREATE TABLE people (id text PRIMARY KEY, label text);\n",
                  **{f"schema_v{n}.sql": f"-- synthetic calendar {n}\n".encode() for n in range(2, 6)}}
CALENDAR_ROLE = b"GRANT SELECT ON people TO fcs_gateway, fcs_worker;\n"
CALENDAR_TABLE = b"-- rollout: expand\nCREATE TABLE synthetic_marks (id text PRIMARY KEY, label text);\n"
CALENDAR_GRANT = b"GRANT SELECT ON synthetic_marks TO fcs_gateway;\n"
CALENDAR_STORE = ('self.pool = ConnectionPool(\n    dsn,\n    min_size=0,\n    kwargs={"row_factory": dict_row, '
                  '"prepare_threshold": None, "connect_timeout": 10},\n)\n')
CALENDAR_SHAPE = [json.dumps({"k": ["relation", "fcs.people"], "v": ["r"]})]
CALENDAR_SHAPE_AFTER = CALENDAR_SHAPE + [json.dumps({"k": ["relation", "fcs.synthetic_marks"], "v": ["r"]})]
DRIFT_MARKER = "missing fcs_gateway SELECT fcs.marker_drift_table"


class CalendarHost(MigrationHost):
    """MigrationHost plus calendar: BASE has calendar schema 1-5, roles.sql and K0's Store; the range adds
    `calendar` files (name: bytes) and appends `calendar_roles` to roles.sql, besides any messaging `files`. The
    calendar database and its containers are fakes on `calendar_run`; `events` records each calendar phase."""

    def __init__(self, tmp_path, monkeypatch, calendar, *, files=None, calendar_roles=b"", calendar_modified=(),
                 calendar_configured=True):
        paths = [CALENDAR_PACKAGE + name for name in calendar] + list(calendar_modified)
        if calendar_roles:
            paths.append(p.CALENDAR_ROLES)
        super().__init__(tmp_path, monkeypatch, files or {}, others=paths,
                         modified=([p.CALENDAR_ROLES] if calendar_roles else []) + list(calendar_modified))
        after = {**CALENDAR_FILES, **calendar}
        for sha_, names in ((BASE, CALENDAR_FILES), (NEWER, after), (NEWEST, after)):
            self.trees[sha_, CALENDAR_PACKAGE] = set(names) | {"roles.sql", "store.py", "migration_rollout.py"}
            for name, data in names.items():
                self.contents[f"{sha_}:{CALENDAR_PACKAGE}{name}"] = data
            self.contents[f"{sha_}:{p.CALENDAR_ROLES}"] = CALENDAR_ROLE + (calendar_roles if sha_ != BASE else b"")
            self.contents[f"{sha_}:{p.CALENDAR_CLASSIFIER}"] = CALENDAR_ROLLOUT.read_bytes()
        self.contents[f"{BASE}:{p.CALENDAR_STORE}"] = CALENDAR_STORE.encode()
        (tmp_path / "runtime.json").write_text(json.dumps({"dsn": f"postgresql://fcs_owner:{DSN_MARKER}@db/fc"}))
        if calendar_configured:
            self.settings.setdefault("migration", {})
            self.settings["migration"].setdefault("databases", {})["calendar"] = {
                "container": "deedkit-calendar-calendar-db-1", "owner": "fcs_owner", "database": "family_calendar"}
            self.settings["migration"]["unattended_calendar"] = {
                "network": "deedkit-calendar_calendar-backend", "runtime_file": str(tmp_path / "runtime.json")}
        self.cal_version = 5
        self.cal_applied = {str(n): sha(CALENDAR_FILES["schema.sql" if n == 1 else f"schema_v{n}.sql"])
                            for n in range(1, 6)}
        self.cal_pending = {name[8:-4]: sha(data) for name, data in calendar.items() if name.startswith("schema_v")}
        self.cal_target = 5 + len(self.cal_pending)
        self.cal_identity = ["7000000000000000002", "family_calendar", "16390"]
        self.cal_shape, self.cal_shape_after = list(CALENDAR_SHAPE), list(CALENDAR_SHAPE_AFTER)
        self.cal_rehearsal = {"took_ms": 7, "difference": p.catalog_difference(
            p.digested(p.catalog_map(CALENDAR_SHAPE)), p.digested(p.catalog_map(CALENDAR_SHAPE_AFTER)))}
        self.cal_answer, self.roles_failure, self.copy_failures, self.crash_at = "applied", None, {}, None
        self.containers = []
        monkeypatch.setattr(p, "calendar_run", self.calendar_run)
        monkeypatch.setattr(p, "expand_run", self.counted_expand_run)

    def live_lines(self, source, step, text):
        if not step.startswith("calendar "):
            return super().live_lines(source, step, text)
        assert source["container"] == "deedkit-calendar-calendar-db-1"
        self.events.append("live " + step)
        if step == "calendar ledger":
            return [json.dumps({"schema_version": self.cal_version, "applied": self.cal_applied})]
        if step == "calendar identity":
            return [json.dumps(self.cal_identity)]
        return list(self.cal_shape)

    @contextmanager
    def database_copy(self, settings, name, sha_, *, keep=False, catalog=None):
        assert (keep, catalog) == (True, p.CATALOG) and name in ("messaging", "calendar")
        self.events.append("copy" if name == "messaging" else "calendar copy")
        if name in self.copy_failures:
            raise p.CopyFailed("copy586", self.copy_failures[name])
        if name == "messaging":
            shape = self.shape
        else:
            shape = self.cal_shape
        yield {"id": "copy586-" + name, "catalog": list(shape), "database": name,
               "catalog_sha256": hashlib.sha256("\n".join(shape).encode()).hexdigest()}

    def rehearse(self, copied, plan, image, limit):
        if plan["database"] != "calendar":
            return super().rehearse(copied, plan, image, limit)
        assert image == "sha256:" + "6" * 64
        self.events.append("calendar rehearse")
        return self.cal_rehearsal

    def build_image(self, source, sha_, target, **kwargs):
        assert target in ("runtime", "calendar")
        return "sha256:" + ("4" if target == "runtime" else "6") * 64

    def crash(self, label):
        self.containers.append(label)
        if self.crash_at == len(self.containers):
            raise Died()

    def counted_expand_run(self, name, image, network, dsn_file, plan_file, *, label=None, status=False):
        self.crash("status" if status else "migrate")
        return self.expand_run(name, image, network, dsn_file, plan_file, label=label, status=status)

    def calendar_run(self, name, image, network, runtime_file, arguments, *, label=None, mounts=(), entrypoint=None):
        assert image == "sha256:" + "6" * 64 and network == "deedkit-calendar_calendar-backend"
        assert runtime_file == self.settings["migration"]["unattended_calendar"]["runtime_file"]
        if entrypoint:
            self.crash("calendar identity")
            return 0, self.cal_identity
        if arguments == ["migrate-expand", "--status"]:
            self.crash("calendar status")
            self.events.append("calendar status")
            return 0, {"schema_version": self.cal_version}
        if arguments[:1] == ["migrate-expand"]:
            self.crash("calendar migrate")
            self.events.append("calendar migrate")
            plan = json.loads(Path(mounts[1].split(",")[1][4:]).read_text())
            assert name == "deedkit-calendar-migrate-" + NEWEST[:12] and plan["identity"] == self.cal_identity
            assert plan["base"] == 5 and plan["target"] == self.cal_target
            if self.cal_answer == "applied":
                self.cal_version, self.cal_shape = self.cal_target, list(self.cal_shape_after)
                self.cal_applied = {**self.cal_applied, **self.cal_pending}
                return 0, {"schema_version": self.cal_target}
            return 1, {"error": "lock_timeout"}
        label_ = "calendar check" if arguments == ["db-roles", "--check"] else "calendar roles"
        self.crash(label_)
        self.events.append(label_)
        if self.roles_failure == label_:
            return 1, ({"error": "db_roles_drift", "schema_version": self.cal_target, "drift": [DRIFT_MARKER]}
                       if label_ == "calendar check" else {"error": "failed"})
        return 0, ({"db_roles_check": "ok", "schema_version": self.cal_version} if label_ == "calendar check"
                   else {"db_roles": ["gateway", "worker"], "provider_secrets_in_gateway": False})

    def plans(self):
        return p.migration_plans(self.settings, self.state, BASE, NEWEST,
                                 [path for path in self.changes[NEWER] if p.SQL_INVENTORY.search(path)])


def calendar_events(host):
    return [event for event in host.events if event != "calls"]


# spec: ANYM-586
def test_a_calendar_range_runs_every_phase_then_db_roles_and_its_check_and_records_its_phase(tmp_path, monkeypatch):
    host = CalendarHost(tmp_path, monkeypatch, {"schema_v6.sql": CALENDAR_TABLE}, calendar_roles=CALENDAR_GRANT)
    outcome = host.deploy()
    assert outcome["status"] == "deployed" and host.paused() is None
    assert calendar_events(host) == ["live calendar ledger", "calendar copy", "calendar rehearse", "live calendar ledger",
                                     "live calendar shape", "live calendar identity", "calendar migrate",
                                     "calendar status", "live calendar ledger", "live calendar effect", "calendar roles",
                                     "calendar check"]
    assert [name for name, _ in host.steps] == ["prepare", "apply", "seamless prepare", "seamless promote"]
    receipt = host.migration()
    assert receipt["status"] == "migrated" and receipt["database"] == "calendar"
    assert (receipt["base"], receipt["target"], receipt["files"], receipt["tables"]) == (5, 6, ["schema_v6.sql"],
                                                                                        ["synthetic_marks"])
    assert receipt["phases"] == {"calendar": {"status": "migrated", "copy": "copy586-calendar", "rehearsal_ms": 7,
                                              "roles": "ok"}}
    plan = json.loads((host.state / NEWEST / "calendar-migrate-plan.json").read_text())
    assert plan == {"base": 5, "target": 6, "roles": {"bytes": len(CALENDAR_ROLE), "sha256": sha(CALENDAR_ROLE)},
                    "identity": host.cal_identity}
    assert (host.state / NEWEST / "calendar-migration-catalog.json").exists()


# spec: ANYM-586
def test_the_calendar_plan_holds_the_bases_ledger_from_version_one_and_the_ranges_new_tables(tmp_path, monkeypatch):
    host = CalendarHost(tmp_path, monkeypatch, {"schema_v6.sql": CALENDAR_TABLE}, calendar_roles=CALENDAR_GRANT)
    [plan] = host.plans()
    assert plan["ledger"] == host.cal_applied and sorted(plan["ledger"]) == ["1", "2", "3", "4", "5"]
    assert plan["pending"] == {"6": sha(CALENDAR_TABLE)} and plan["tables"] == ["synthetic_marks"]
    assert plan["roles_changed"] == ["roles.sql"] and (plan["database"], plan["base"], plan["target"]) == ("calendar", 5, 6)


# spec: ANYM-586
def test_a_range_touching_both_databases_rehearses_both_then_applies_calendar_then_messaging(tmp_path, monkeypatch):
    host = CalendarHost(tmp_path, monkeypatch, {"schema_v6.sql": CALENDAR_TABLE}, calendar_roles=CALENDAR_GRANT,
                        files={"051_synthetic_note.sql": NOTE})
    assert [plan["database"] for plan in host.plans()] == ["calendar", "messaging"]
    outcome = host.deploy()
    assert outcome["status"] == "deployed"
    events = calendar_events(host)
    order = ["calendar copy", "calendar rehearse", "copy", "rehearse", "calendar migrate", "calendar roles",
             "calendar check", "migrate"]
    assert [events.index(name) for name in order] == sorted(events.index(name) for name in order)
    receipt = host.migration()
    assert receipt["status"] == "migrated" and receipt["database"] == "messaging" and receipt["copy"] == "copy586-messaging"
    assert receipt["phases"]["calendar"]["status"] == receipt["phases"]["messaging"]["status"] == "migrated"
    assert (host.state / NEWEST / "migrate-plan.json").exists() and (host.state / NEWEST / "calendar-migrate-plan.json").exists()


# spec: ANYM-586
def test_a_dry_run_names_both_migrations_in_their_order(tmp_path, monkeypatch):
    host = CalendarHost(tmp_path, monkeypatch, {"schema_v6.sql": CALENDAR_TABLE}, files={"051_synthetic_note.sql": NOTE})
    outcome = host.deploy(dry_run=True)
    assert outcome["steps"][:2] == [
        "migrate calendar schema 5 to 6 after a verified copy and its rehearsal, then db-roles and db-roles --check",
        "migrate messaging schema 50 to 51 after a verified copy and its rehearsal"]
    assert host.events == [] and host.migration() is None


# spec: ANYM-586
@pytest.mark.parametrize("failing", ["calendar", "messaging"])
def test_a_copy_failure_of_either_database_applies_neither_and_is_not_retried(tmp_path, monkeypatch, failing):
    host = CalendarHost(tmp_path, monkeypatch, {"schema_v6.sql": CALENDAR_TABLE}, files={"051_synthetic_note.sql": NOTE})
    host.copy_failures[failing] = "count_mismatch"
    outcome = host.deploy()
    assert not {"calendar migrate", "migrate", "calendar roles"} & set(host.events)
    assert (host.cal_version, host.version) == (5, 50) and "apply" not in [name for name, _ in host.steps]
    reason = f"the pre-migration copy of {failing} failed: count_mismatch"
    assert outcome["status"] == "halted" and outcome["reason"] == reason == host.paused()
    receipt = host.migration()
    assert receipt["status"] == "refused" and receipt["phases"][failing] == {"status": "refused", "phase": "copy"}
    p.PAUSE.unlink()
    again = host.deploy()
    assert again["reason"].startswith(f"the unattended migration of {NEWEST} ended refused")
    assert host.events.count("calendar copy") == 1


# spec: ANYM-586
def test_a_messaging_failure_after_calendars_commit_pauses_naming_the_calendar_schema(tmp_path, monkeypatch):
    host = CalendarHost(tmp_path, monkeypatch, {"schema_v6.sql": CALENDAR_TABLE}, files={"051_synthetic_note.sql": NOTE})
    host.answer = "refused"
    outcome = host.deploy()
    assert outcome["status"] == "halted" and outcome["reason"] == "calendar migrated to 6; messaging not applied: lock_timeout"
    assert host.paused() == outcome["reason"] and "apply" not in [name for name, _ in host.steps]
    receipt = host.migration()
    assert receipt["status"] == "not-applied" and receipt["code"] == "lock_timeout"
    assert receipt["phases"]["calendar"]["status"] == "migrated" and receipt["phases"]["calendar"]["roles"] == "ok"
    assert receipt["phases"]["messaging"]["status"] == "not-applied"
    assert host.cal_version == 6 and host.version == 50


# spec: ANYM-586
@pytest.mark.parametrize("failing", ["calendar roles", "calendar check"])
def test_a_db_roles_or_check_failure_after_calendars_commit_pauses_before_messaging(tmp_path, monkeypatch, failing):
    host = CalendarHost(tmp_path, monkeypatch, {"schema_v6.sql": CALENDAR_TABLE}, calendar_roles=CALENDAR_GRANT,
                        files={"051_synthetic_note.sql": NOTE})
    host.roles_failure = failing
    outcome = host.deploy()
    code = "failed" if failing == "calendar roles" else "db_roles_drift"
    assert outcome["status"] == "halted" and outcome["reason"] == f"calendar migrated to 6; roles: {code}" == host.paused()
    assert "migrate" not in host.events and host.version == 50 and "apply" not in [name for name, _ in host.steps]
    assert ("calendar check" in host.events) == (failing == "calendar check")
    receipt = host.migration()
    assert receipt["status"] == "roles-failed" and receipt["phases"]["calendar"]["roles"] == code
    written = "".join(path.read_text() for path in [p.PAUSE, *host.state.rglob("*.json")])
    assert DRIFT_MARKER not in written and DSN_MARKER not in written
    p.PAUSE.unlink()
    assert host.deploy()["reason"].startswith(f"the unattended migration of {NEWEST} ended roles-failed")
    assert host.events.count("calendar roles") == 1


# spec: ANYM-586
def test_a_run_killed_between_calendars_commit_and_messaging_pauses_and_never_applies_messaging(tmp_path, monkeypatch):
    host = CalendarHost(tmp_path, monkeypatch, {"schema_v6.sql": CALENDAR_TABLE}, files={"051_synthetic_note.sql": NOTE})
    real = p.migration_receipt
    def killed(release, plan, status, **fields):
        if fields["phases"].get("messaging", {}).get("status") == "planned":
            raise Died()  # the process dies before messaging's planned receipt is written
        return real(release, plan, status, **fields)
    monkeypatch.setattr(p, "migration_receipt", killed)
    with pytest.raises(Died):
        host.deploy()
    assert host.migration()["status"] == "planned" and host.migration()["phases"]["calendar"]["roles"] == "ok"
    monkeypatch.setattr(p, "migration_receipt", real)
    outcome = host.deploy()
    assert outcome["status"] == "halted" and outcome["reason"] == "calendar migrated to 6; messaging not applied: interrupted"
    assert "migrate" not in host.events and host.events.count("calendar migrate") == 1 and host.version == 50
    assert {key: host.migration()["phases"]["messaging"][key] for key in ("status", "code")} == {
        "status": "not-applied", "code": "interrupted"}


CONTAINER_STEPS = ["calendar identity", "identity", "calendar migrate", "calendar status", "calendar roles",
                   "calendar check", "migrate", "status"]


# spec: ANYM-586
@pytest.mark.parametrize("crash", range(1, len(CONTAINER_STEPS) + 1))
def test_a_run_killed_at_any_container_step_never_starts_a_migration_or_db_roles_twice(tmp_path, monkeypatch, crash):
    host = CalendarHost(tmp_path, monkeypatch, {"schema_v6.sql": CALENDAR_TABLE}, calendar_roles=CALENDAR_GRANT,
                        files={"051_synthetic_note.sql": NOTE})
    monkeypatch.setattr(p, "database_identity", lambda name, image, network, dsn_file: host.crash("identity") or host.identity)
    host.crash_at = crash
    with pytest.raises(Died):
        host.deploy()
    host.crash_at = None
    outcome = host.deploy()
    assert outcome["status"] in ("deployed", "halted")
    for step in ("calendar migrate", "migrate", "calendar roles", "calendar check"):
        assert host.containers.count(step) <= 1, (step, host.containers)
    # The invariant also holds for what the next run decided: deployed means both databases reached their targets.
    if outcome["status"] == "deployed":
        assert (host.cal_version, host.version) == (6, 51) and host.migration()["status"] == "migrated"
    else:
        assert "apply" not in [name for name, _ in host.steps]


# spec: ANYM-586
def test_a_clean_two_database_run_reaches_each_container_step_once_in_order(tmp_path, monkeypatch):
    host = CalendarHost(tmp_path, monkeypatch, {"schema_v6.sql": CALENDAR_TABLE}, calendar_roles=CALENDAR_GRANT,
                        files={"051_synthetic_note.sql": NOTE})
    monkeypatch.setattr(p, "database_identity", lambda name, image, network, dsn_file: host.crash("identity") or host.identity)
    assert host.deploy()["status"] == "deployed" and host.containers == CONTAINER_STEPS


# spec: ANYM-586
@pytest.mark.parametrize("case", ["not-configured", "edited-applied-file", "gap", "no-header", "manual", "revoke",
                                  "index-on-existing", "other-calendar-sql"])
def test_a_calendar_range_that_does_not_qualify_pauses_with_todays_text(tmp_path, monkeypatch, case):
    calendar, roles, modified = {"schema_v6.sql": CALENDAR_TABLE}, b"", []
    if case == "edited-applied-file":
        modified = [CALENDAR_PACKAGE + "schema_v4.sql"]
    elif case == "gap":
        calendar = {"schema_v7.sql": CALENDAR_TABLE}
    elif case == "no-header":
        calendar = {"schema_v6.sql": CALENDAR_TABLE.split(b"\n", 1)[1]}
    elif case == "manual":
        calendar = {"schema_v6.sql": b"-- rollout: manual\nUPDATE people SET label = 'marker';\n"}
    elif case == "revoke":
        roles = b"REVOKE SELECT ON people FROM fcs_worker;\n"
    elif case == "index-on-existing":
        calendar = {"schema_v6.sql": b"-- rollout: expand\nCREATE INDEX people_label ON people (label);\n"}
    elif case == "other-calendar-sql":
        modified = ["services/calendar/family_calendar/service/extra.sql"]
    host = CalendarHost(tmp_path, monkeypatch, calendar, calendar_roles=roles, calendar_modified=modified,
                        calendar_configured=case != "not-configured")
    if case == "edited-applied-file":
        host.contents[f"{NEWEST}:{CALENDAR_PACKAGE}schema_v4.sql"] = b"-- synthetic calendar 4, edited\n"
    assert host.plans() is None
    outcome = host.deploy()
    sql = [path for path in host.changes[NEWER] if p.SQL_INVENTORY.search(path)]
    assert outcome["status"] == "halted" and host.steps == [] and host.events == [] and host.migration() is None
    assert outcome["reason"].startswith(f"the SQL inventory changed ({', '.join(sql)}); migrate by hand (production.md)")


# spec: ANYM-586
@pytest.mark.parametrize("gate", ["behind", "ahead", "store"])
def test_a_calendar_admission_gate_pauses_with_no_copy_container_or_production_write(tmp_path, monkeypatch, gate):
    host = CalendarHost(tmp_path, monkeypatch, {"schema_v6.sql": CALENDAR_TABLE})
    if gate == "behind":
        host.cal_version = 4
        host.cal_applied.pop("5")
    elif gate == "ahead":
        host.cal_version, host.cal_applied["6"] = 6, sha(CALENDAR_TABLE)
    else:
        host.contents[f"{BASE}:{p.CALENDAR_STORE}"] = CALENDAR_STORE.replace("None", "0").encode()
    outcome = host.deploy()
    assert host.steps == [] and not {"calendar copy", "calendar migrate"} & set(host.events) and host.migration() is None
    assert outcome["status"] == "halted" and outcome["reason"] == host.paused() == (
        "the running calendar release would fail on the new columns: deploy ANYM-586 K0 first" if gate == "store"
        else "the live calendar ledger is not the base's schema 5; migrate by hand (production.md)")


# spec: ANYM-586
@pytest.mark.parametrize("point", ["before", "after"])
def test_a_run_killed_around_the_calendar_container_is_settled_from_status_and_then_runs_db_roles(tmp_path,
                                                                                                  monkeypatch, point):
    host = CalendarHost(tmp_path, monkeypatch, {"schema_v6.sql": CALENDAR_TABLE})
    real = host.calendar_run
    def dying(name, image, network, runtime_file, arguments, **kwargs):
        if arguments[:2] == ["migrate-expand", "--plan"] and point == "before":
            raise Died()
        found = real(name, image, network, runtime_file, arguments, **kwargs)
        if arguments[:2] == ["migrate-expand", "--plan"]:
            raise Died()
        return found
    monkeypatch.setattr(p, "calendar_run", dying)
    with pytest.raises(Died):
        host.deploy()
    assert host.migration()["phases"]["calendar"]["status"] == "planned"
    monkeypatch.setattr(p, "calendar_run", real)
    host.events[:] = []
    outcome = host.deploy()
    assert "calendar migrate" not in host.events and host.events[:2] == ["calendar status", "live calendar ledger"]
    if point == "before":
        assert outcome["reason"] == f"the calendar migration of {NEWEST} did not apply" and "calendar roles" not in host.events
        assert host.migration()["status"] == "not-applied"
    else:
        assert outcome["status"] == "deployed" and host.events[-2:] == ["calendar roles", "calendar check"]
        assert host.migration()["phases"]["calendar"] == {"status": "migrated", "copy": "copy586-calendar",
                                                          "rehearsal_ms": 7, "roles": "ok"}


# spec: ANYM-586
@pytest.mark.parametrize("call", ["active", "unknown"])
def test_a_resumed_db_roles_waits_for_an_active_or_unknown_call(tmp_path, monkeypatch, call):
    host = CalendarHost(tmp_path, monkeypatch, {"schema_v6.sql": CALENDAR_TABLE}, files={"051_synthetic_note.sql": NOTE})
    host.crash_at = 3  # the calendar migrate-expand commits, the run dies before its --status
    with pytest.raises(Died):
        host.deploy()
    host.crash_at = None
    real = host.wait_for_calls
    def calls(settings, deadline):
        if call == "unknown":
            raise p.CallsUnknown("the calls service could not be read")
        host.calls = ["synthetic-call"]
        return real(settings, deadline)
    monkeypatch.setattr(p, "wait_for_calls", calls)
    steps = list(host.steps)
    outcome = host.deploy()
    assert outcome["status"] == "waiting" and outcome["call_waits"]["count"] == 1 and host.paused() is None
    assert "calendar roles" not in host.events and "migrate" not in host.events and host.steps == steps
    assert host.migration()["status"] == "planned" and host.migration()["phases"]["calendar"]["status"] == "planned"
    host.calls = []
    monkeypatch.setattr(p, "wait_for_calls", real)
    assert host.deploy()["reason"] == "calendar migrated to 6; messaging not applied: interrupted"
    assert host.containers.count("calendar roles") == 1 and host.containers.count("calendar migrate") == 1


# spec: ANYM-586
def test_a_run_killed_during_db_roles_pauses_and_never_reruns_it(tmp_path, monkeypatch):
    host = CalendarHost(tmp_path, monkeypatch, {"schema_v6.sql": CALENDAR_TABLE})
    host.crash_at = 4  # calendar identity, migrate, status, then db-roles
    with pytest.raises(Died):
        host.deploy()
    assert host.containers[-1] == "calendar roles" and host.migration()["phases"]["calendar"]["roles"] == "running"
    host.crash_at = None
    outcome = host.deploy()
    assert outcome["reason"] == "calendar migrated to 6; roles: interrupted" and host.containers.count("calendar roles") == 1


# spec: ANYM-586
def test_a_health_failure_after_both_migrations_rolls_back_naming_both_schemas(tmp_path, monkeypatch):
    host = CalendarHost(tmp_path, monkeypatch, {"schema_v6.sql": CALENDAR_TABLE}, files={"051_synthetic_note.sql": NOTE})
    def unhealthy(sha_):
        p.write(host.state / sha_ / "result.json", {"sha": sha_, "status": "unhealthy"})
        raise p.Refused("replacement did not become healthy")
    host.fail_apply = unhealthy
    outcome = host.deploy()
    assert outcome["status"] == "rolled-back" and outcome["reason"] == (
        f"plan A of {NEWEST} failed its health wait after migrating calendar schema 5 to 6 and messaging schema 50 to "
        f"51; rolled back to {BASE} on calendar schema 6 and messaging schema 51")


# spec: ANYM-586
def test_the_calendar_containers_run_the_candidate_image_with_the_bounded_flags_and_read_only_mounts(monkeypatch):
    seen, printed = [], iter([(0, '{"schema_version": 6}\n'), (1, '{"error": "deadlock on marker-row-value"}\n'),
                              (0, '{\n  "db_roles": ["gateway", "worker"]\n}\n'),
                              (1, json.dumps({"error": "db_roles_drift", "schema_version": 6, "drift": [DRIFT_MARKER]},
                                             indent=2)),
                              (0, '["7000000000000000002", "family_calendar", "16390"]\n')])
    monkeypatch.setattr(p, "bounded", lambda argv, **kwargs: seen.append((argv, kwargs)) or next(printed))
    image = "sha256:" + "6" * 64
    assert p.calendar_expand("deedkit-calendar-migrate-x", image, "cal_net", "/s/runtime.json", "/s/plan.json") == {
        "schema_version": 6}
    assert p.calendar_expand("deedkit-calendar-migrate-x", image, "cal_net", "/s/runtime.json", "/s/plan.json") == {
        "error": "failed"}
    settings = {"migration": {"unattended_calendar": {"network": "cal_net", "runtime_file": "/s/runtime.json"}}}
    assert p.calendar_roles(settings, NEWEST, image, 6) == "db_roles_drift"
    assert p.calendar_identity("deedkit-calendar-identity-x", image, "cal_net", "/s/runtime.json") == [
        "7000000000000000002", "family_calendar", "16390"]
    argv, kwargs = seen[0]
    assert kwargs == {"timeout": 30} and argv[:5] == ["docker", "run", "--rm", "--name", "deedkit-calendar-migrate-x"]
    joined = " ".join(argv)
    for flag in ("--log-driver none", "--read-only", "--user 10001:10001", "--cap-drop ALL",
                 "--security-opt no-new-privileges", "--pids-limit 64", "--memory 512m", "--network cal_net",
                 "-e PGAPPNAME=deedkit-calendar-migrate-x",
                 "type=bind,src=/s/runtime.json,dst=/data/service/runtime.json,readonly",
                 "type=bind,src=/s/plan.json,dst=/run/deedkit/plan.json,readonly"):
        assert flag in joined
    assert argv[-4:] == [image, "migrate-expand", "--plan", "/run/deedkit/plan.json"]
    assert [call[0][-2:] for call in seen[2:4]] == [[image, "db-roles"], ["db-roles", "--check"]]
    assert seen[4][0][-4:] == ["python", image, "-c", p.CALENDAR_IDENTITY]


CALENDAR_SETUP = """CREATE SCHEMA fcs;
CREATE TABLE fcs.migrations (version int PRIMARY KEY, checksum text NOT NULL);
INSERT INTO fcs.migrations SELECT n, repeat('c', 64) FROM generate_series(1, 5) n;
CREATE TABLE fcs.people (id text PRIMARY KEY, label text);
INSERT INTO fcs.people SELECT 'p' || n, 'synthetic' FROM generate_series(1, 5) n;
"""
CALENDAR_ADDITIVE = "CREATE TABLE fcs.synthetic_marks (id text PRIMARY KEY, label text); "
CALENDAR_LEDGER_ROW = "INSERT INTO fcs.migrations VALUES (6, '{digest}'); "


@pytest.fixture
def calendar_copy_settings(tmp_path, monkeypatch, source_db):
    psql(source_db, CALENDAR_SETUP)
    monkeypatch.setattr(p, "filesystem", lambda path: (1 << 40, 1 << 40))
    # The rehearsal's runtime.json belongs to the image user 10001; tests do not run as root.
    monkeypatch.setattr(p.os, "chown", lambda path, uid, gid: None)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    settings = {"version": 1, "state_directory": str(state), "migration": {
        "databases": {"calendar": {"container": CONTAINER, "owner": OWNER, "database": source_db}}}}
    yield settings
    for directory in sorted((state / p.COPIES).iterdir()) if (state / p.COPIES).exists() else ():
        subprocess.run(["docker", "stop", "-t", "1", "deedkit-copy-" + directory.name], capture_output=True)


def fake_calendar_migrate(migration, *, slow=False):
    """calendar migrate-expand as one psql session on the copy over the rehearsal network: the migration and its
    ledger row in one transaction, the DSN read from the mounted runtime.json."""
    calls = []
    def calendar_expand(name, image, network, runtime_file, plan_file, *, label=None, status=False):
        runtime = json.loads(Path(runtime_file).read_text())
        calls.append((network, label, json.loads(Path(plan_file).read_text())["identity"],
                      oct(Path(runtime_file).stat().st_mode & 0o777)))
        host, database = runtime["dsn"].split("@")[1].split(":")[0], runtime["dsn"].rsplit("/", 1)[1]
        text = "BEGIN; " + migration + CALENDAR_LEDGER_ROW.format(digest="a" * 64) + (
            " SELECT pg_sleep(1.2);" if slow else "") + " COMMIT;"
        subprocess.run(["docker", "run", "--rm", "--network", network, "--label", label, "-i", image, "psql", "-X", "-q",
                        "-v", "ON_ERROR_STOP=1", "-h", host, "-U", OWNER, "-d", database], input=text,
                       capture_output=True, text=True, check=True)
        return {"schema_version": 6}
    return calendar_expand, calls


# spec: ANYM-586
@pytest.mark.parametrize("case", ["additive", "existing-table", "slow"])
def test_the_calendar_rehearsal_runs_on_the_copy_times_its_transaction_and_checks_its_data(calendar_copy_settings,
                                                                                          monkeypatch, case):
    migration = CALENDAR_ADDITIVE + ("UPDATE fcs.people SET label = label WHERE id = 'p1'; "
                                     if case == "existing-table" else "")
    calendar_expand, calls = fake_calendar_migrate(migration, slow=case == "slow")
    monkeypatch.setattr(p, "calendar_expand", calendar_expand)
    plan = {"database": "calendar", "base": 5, "target": 6, "pending": {"6": "a" * 64}, "roles": {}}
    with p.database_copy(calendar_copy_settings, "calendar", NEWEST, keep=True, catalog=p.CATALOG) as copied:
        copy_id = copied["id"]
        if case == "additive":
            record = p.rehearse(copied, plan, scratch_image(), 4000)
        else:
            with pytest.raises(p.MigrationRefused) as refused:
                p.rehearse(copied, plan, scratch_image(), 1000)
    assert calls[0][:2] == ("deedkit-copy-" + copy_id, "deedkit-copy=" + copy_id) and calls[0][3] == "0o600"
    assert len(calls[0][2]) == 3 and calls[0][2][1] == copied["database"]
    assert labelled(copy_id, "container") == [] and labelled(copy_id, "network") == []
    if case == "additive":
        assert 0 <= record["took_ms"] <= 1000
        assert ["relation", "fcs.synthetic_marks"] in [key for key, _ in record["difference"]["added"]]
    else:
        assert str(refused.value).startswith({"existing-table": "not additive: rows of fcs.people were written",
                                              "slow": "too slow for an unattended lock: the rehearsal took"}[case])
    if case == "slow":
        assert int(str(refused.value).split("took ")[1].split(" ")[0]) >= 1200
    # The live database itself never changed.
    assert psql(calendar_copy_settings["migration"]["databases"]["calendar"]["database"],
                "SELECT max(version) FROM fcs.migrations;").strip() == "5"


# spec: ANYM-586
def test_the_calendar_ledger_and_identity_read_the_live_calendar_database(source_db, tmp_path):
    psql(source_db, CALENDAR_SETUP)
    source = {"container": CONTAINER, "owner": OWNER, "database": source_db}
    assert p.calendar_ledger(source) == (5, {str(n): "c" * 64 for n in range(1, 6)})
    # The image's identity script reads the DSN from runtime.json, as the calendar CLI does.
    dsn = Path(os.environ["PROBE_TEST_DSN_FILE"]).read_text().strip().rsplit("/", 1)[0] + "/" + source_db
    (tmp_path / "runtime.json").write_text(json.dumps({"dsn": dsn, "port": 1}))
    script = p.CALENDAR_IDENTITY.replace(p.CALENDAR_RUNTIME, str(tmp_path / "runtime.json"))
    reached = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True).stdout
    found = json.loads(p.live_lines(source, "calendar identity", p.IDENTITY_SQL + ";")[0])
    assert json.loads(reached) == found and found[1] == source_db
