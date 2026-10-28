"""ANYM-588: database copies against the isolated PostgreSQL container through `docker exec`; Docker fakes for the
cleanup read back. No production host, state or provider is touched."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from scripts import production as p

SHA = "c" * 40
ROOT = Path(__file__).resolve().parents[1]
CONTAINER = os.environ.get("ANYMSG_TEST_POSTGRES_CONTAINER")
OWNER = "probe_owner"
PRIVATE = "synthetic-private-value"
PLENTY = (1 << 40, 1 << 40)


@pytest.fixture(autouse=True)
def no_host_state(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "PAUSE", tmp_path / "autodeploy.pause")


def live():
    if not CONTAINER:
        if os.environ.get("CI"):
            pytest.fail("CI must provide the isolated PostgreSQL container")
        pytest.skip("isolated PostgreSQL container unavailable locally")


def psql(database, text):
    return subprocess.run(["docker", "exec", "-i", CONTAINER, "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                           "-U", OWNER, "-d", database], input=text, text=True, capture_output=True, check=True,
                          timeout=60).stdout.strip()


def scratch_sql(copy_id, database, text):
    subprocess.run(["docker", "exec", "-i", "deedkit-copy-" + copy_id, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1",
                    "-h", "127.0.0.1", "-U", p.COPY_USER, "-d", database], input=text, text=True, capture_output=True,
                   check=True, timeout=60)


def copy_sessions(copy_id, step=""):
    return int(psql("postgres", "SELECT count(*) FROM pg_stat_activity WHERE starts_with(application_name, "
                                f"'deedkit-copy-{copy_id}-{step}')"))


def labelled(copy_id):
    return subprocess.run(["docker", "ps", "-a", "-q", "--filter", "label=deedkit-copy=" + copy_id],
                          capture_output=True, text=True, check=True).stdout.strip()


def settings_for(tmp_path, database, **migration):
    state = tmp_path / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    return {"version": 1, "state_directory": str(state), "migration": {
        "databases": {"main": {"container": CONTAINER, "owner": OWNER, "database": database}}, **migration}}


def copies(settings):
    return sorted((Path(settings["state_directory"]) / p.COPIES).iterdir())


@pytest.fixture
def source(tmp_path, monkeypatch):
    """A fresh database in the isolated cluster with a table, an enum, a view and a synthetic private row."""
    live()
    database = "anymsg_copy_" + uuid4().hex[:12] + "_test"
    psql("messaging_probe_test", f"CREATE DATABASE {database}")
    psql(database, "CREATE TYPE mood AS ENUM ('calm', 'busy'); "
                   "CREATE TABLE items (id serial PRIMARY KEY, value text NOT NULL, mood mood DEFAULT 'calm'); "
                   "CREATE TABLE empty_one (id bigint); CREATE VIEW calm AS SELECT id FROM items WHERE mood = 'calm'; "
                   f"INSERT INTO items (value) SELECT 'row ' || n FROM generate_series(1, 19) n; "
                   f"INSERT INTO items (value) VALUES ('{PRIVATE}');")
    monkeypatch.setattr(p, "filesystem", lambda path: PLENTY)
    settings = settings_for(tmp_path, database)
    yield settings, database
    root = Path(settings["state_directory"]) / p.COPIES
    for directory in sorted(root.iterdir()) if root.exists() else ():
        subprocess.run(["docker", "stop", "-t", "1", "deedkit-copy-" + directory.name], capture_output=True)
    psql("messaging_probe_test", f"DROP DATABASE {database} WITH (FORCE)")


def released(settings, copy_id):
    """Every resource of the copy is gone and `released.json` says so."""
    directory = Path(settings["state_directory"]) / p.COPIES / copy_id
    assert (directory / "released.json").exists()
    assert not (directory / "scratch").exists()
    assert not labelled(copy_id) and copy_sessions(copy_id) == 0
    return directory


def wrap_watched(monkeypatch, before=None, after=None):
    original = p.watched
    def watched(copy, argv, **kwargs):
        argv = before(copy, argv) or argv if before else argv
        original(copy, argv, **kwargs)
        if after:
            after(copy, argv)
    monkeypatch.setattr(p, "watched", watched)


# spec: ANYM-588
def test_copy_holds_the_snapshot_counts_while_a_writer_inserts_and_restores_them(source, monkeypatch):
    settings, database = source
    seen = []
    def writer(copy, argv):
        if not seen:
            # Every session the copy opened carries its prefix; the keeper still holds the snapshot.
            seen.append(copy_sessions(copy["id"], "keeper"))
            psql(database, "INSERT INTO items (value) SELECT 'later ' || n FROM generate_series(1, 50) n")
    wrap_watched(monkeypatch, before=writer)
    with p.database_copy(settings, "main", SHA) as record:
        copy_id = record["id"]
        assert record["verified"] and record["counts"] == {"public.empty_one": 0, "public.items": 20}
    assert seen == [1] and psql(database, "SELECT count(*) FROM items") == "70"
    directory = released(settings, copy_id)
    assert copy_id.startswith(SHA[:12] + "-main-") and p.COPY_ID.fullmatch(copy_id)
    assert json.loads((directory / "copy.json").read_text())["counts"] == record["counts"]
    assert (directory / "copy.json").stat().st_mode & 0o777 == 0o600 and directory.stat().st_mode & 0o777 == 0o700
    for name in (f"{database}.dump", "roles.sql"):
        assert (directory / name).stat().st_mode & 0o777 == 0o600
        assert record["files"][name] == p.file_digest(directory / name)
    assert record["server_version"].startswith("18") and record["database_bytes"] > 0


def fail_dump(copy, argv):
    if "-Fc" in argv:
        return [*argv, "--no-such-option"]


def fail_restore(copy, argv):
    if "pg_restore" in argv:
        return [*argv[:-1], "no_such_database"]


def add_row(copy, argv):
    if "pg_restore" in argv:
        scratch_sql(copy["id"], argv[-1], "INSERT INTO items (value) VALUES ('extra')")


def change_schema(copy, argv):
    if "pg_restore" in argv:
        scratch_sql(copy["id"], argv[-1], "COMMENT ON TABLE items IS 'changed'")


# spec: ANYM-588
@pytest.mark.parametrize("code, before, after", [("dump_failed", fail_dump, None), ("restore_failed", fail_restore, None),
                                                 ("count_mismatch", None, add_row),
                                                 ("schema_mismatch", None, change_schema)])
def test_a_failed_dump_restore_or_comparison_ends_with_its_code_and_no_copy_record(source, monkeypatch, code, before,
                                                                                   after):
    settings, _ = source
    wrap_watched(monkeypatch, before, after)
    with pytest.raises(p.CopyFailed) as failed:
        with p.database_copy(settings, "main", SHA):
            pytest.fail("a failed copy yields no record")
    assert failed.value.code == code and str(failed.value).endswith("failed: " + code)
    [directory] = copies(settings)
    assert not (directory / "copy.json").exists()
    released(settings, directory.name)


# spec: ANYM-588
def test_a_failing_restore_never_puts_a_row_value_into_receipts_or_output(source, monkeypatch, tmp_path, capsys):
    settings, _ = source
    project = tmp_path / "compose.json"
    project.write_text("{}")
    configuration = tmp_path / "production.json"
    configuration.write_text(json.dumps({**settings, "projects": [{
        "name": "synthetic", "directory": str(tmp_path), "files": [str(project)], "services": {"api": "runtime"}}]}))
    configuration.chmod(0o600)
    def leak(copy, argv):
        # The helper prints every row of the archive to stdout and stderr, then fails.
        if "pg_restore" in argv:
            return [*argv[:6], "sh", "-c", "pg_restore -f - | tee /dev/stderr; exit 1"]
    wrap_watched(monkeypatch, before=leak)
    with pytest.raises(SystemExit):
        p.main(["copy-database", "--config", str(configuration), "--database", "main", "--sha", SHA])
    out, err = capsys.readouterr()
    assert "failed: restore_failed" in err and PRIVATE not in out + err
    [directory] = copies(settings)
    assert not (directory / "copy.json").exists() and not p.PAUSE.exists()
    for receipt in ("owner.json", "released.json"):
        assert PRIVATE not in (directory / receipt).read_text()
    released(settings, directory.name)


# spec: ANYM-588
def test_too_little_free_space_refuses_before_any_dump(source, monkeypatch):
    settings, database = source
    # Above the 10 GiB reserve, below the reserve plus twice the database.
    monkeypatch.setattr(p, "filesystem", lambda path: ((10 << 30) + (1 << 20), 50 << 30))
    with pytest.raises(p.CopyFailed, match="disk_reserve"):
        with p.database_copy(settings, "main", SHA):
            pass
    [directory] = copies(settings)
    assert not (directory / f"{database}.dump").exists() and not (directory / "copy.json").exists()
    released(settings, directory.name)


# spec: ANYM-588
def test_space_running_out_during_the_restore_cancels_it_and_cleans_up(source, monkeypatch):
    settings, _ = source
    restoring = []
    monkeypatch.setattr(p, "filesystem", lambda path: (1 << 30, 100 << 30) if restoring else PLENTY)
    def low(copy, argv):
        if "pg_restore" in argv:
            restoring.append(True)
    wrap_watched(monkeypatch, before=low)
    with pytest.raises(p.CopyFailed, match="disk_reserve"):
        with p.database_copy(settings, "main", SHA):
            pass
    [directory] = copies(settings)
    assert restoring and not (directory / "copy.json").exists()
    released(settings, directory.name)


# spec: ANYM-588
def test_a_dump_past_its_deadline_is_cancelled(source, monkeypatch):
    settings, database = source
    original = p.watched
    def late(copy, argv, **kwargs):
        if "-Fc" not in argv:
            return original(copy, argv, **kwargs)
        moments = iter(range(0, 10**6, 2000))
        monkeypatch.setattr(p, "copy_clock", lambda: next(moments))
        try:
            return original(copy, argv, **kwargs)
        finally:
            monkeypatch.setattr(p, "copy_clock", time.monotonic)
    monkeypatch.setattr(p, "watched", late)
    with pytest.raises(p.CopyFailed, match="deadline"):
        with p.database_copy(settings, "main", SHA):
            pass
    [directory] = copies(settings)
    assert not (directory / "copy.json").exists()
    released(settings, directory.name)


CHILD = """
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts import production as p
point, settings, marks = sys.argv[2], json.loads(sys.argv[3]), Path(sys.argv[4])
p.filesystem = lambda path: (1 << 40, 1 << 40)
original = p.watched
def hold(name):
    (marks / name).write_text("")
    while not (marks / "go").exists():
        time.sleep(0.1)
def watched(copy, argv, **kwargs):
    if point == "keeper" and "--schema-only" in argv:
        hold("ready"); time.sleep(600)
    if point == "dump" and "-Fc" in argv:
        # A dump still reading: a session of the dump step that does not end by itself.
        argv = [*argv[:6], "psql", "-X", "-q", "-U", argv[-3], "-d", argv[-1], "-c", "SELECT pg_sleep(600)"]
        hold("ready")
    if point == "scratch" and "127.0.0.1" in argv:
        hold("ready"); time.sleep(600)
    return original(copy, argv, **kwargs)
p.watched = watched
with p.database_copy(settings, "main", sys.argv[5], keep=point == "kept"):
    hold("ready"); time.sleep(600)
"""


# spec: ANYM-588
@pytest.mark.parametrize("point", ["keeper", "dump", "scratch", "kept"])
def test_a_copy_killed_at_any_point_is_stopped_and_released_by_the_next_run(source, tmp_path, point):
    settings, database = source
    marks = tmp_path / "marks"
    marks.mkdir()
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(ROOT), point, json.dumps(settings), str(marks), SHA],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 120
        while not (marks / "ready").exists():
            assert child.poll() is None and time.monotonic() < deadline
            time.sleep(0.1)
        [directory] = copies(settings)
        copy_id = directory.name
        (marks / "go").write_text("")
        if point == "dump":
            while copy_sessions(copy_id, "dump") == 0:
                assert time.monotonic() < deadline
                time.sleep(0.2)
        if point in ("scratch", "kept"):
            assert labelled(copy_id)
        if point == "kept":
            assert json.loads((directory / "copy.json").read_text())["verified"]
        assert copy_sessions(copy_id) == {"keeper": 1, "dump": 2}.get(point, 0)
        child.send_signal(signal.SIGKILL)
        child.wait(30)
        assert (directory / "owner.json").exists() and not (directory / "released.json").exists()
        assert p.reconcile_copies(Path(settings["state_directory"])) == [copy_id]
        released(settings, copy_id)
        if point in ("scratch", "kept"):
            assert (directory / f"{database}.dump").exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


# spec: ANYM-588
def test_copy_database_refuses_without_the_migration_section(tmp_path):
    settings = {"version": 1, "state_directory": str(tmp_path / "state"), "projects": []}
    with pytest.raises(p.Refused, match="no migration section"):
        with p.database_copy(settings, "main", SHA):
            pass
    assert not (tmp_path / "state").exists()


class FakeDocker:
    """Answers the cleanup's `docker` commands: `stop` loses its answer, the read back tells."""

    def __init__(self, *, stops=True):
        self.stops, self.calls, self.running = stops, [], True

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[:2] == ["docker", "stop"]:
            self.running = self.running and not self.stops
            raise p.Refused("docker failed (exit 1); production was not accepted")
        if argv[:2] == ["docker", "ps"]:
            return "0123456789ab\n" if self.running else ""
        return "0"


def unreleased(state, copy_id):
    state.mkdir(mode=0o700, exist_ok=True)
    root = p.copies_root(state, create=True)
    directory = root / copy_id
    directory.mkdir(mode=0o700)
    copy = {"id": copy_id, "directory": str(directory), "container": "deedkit-copy-" + copy_id,
            "application_name": "deedkit-copy-" + copy_id, "resources": [], "created_at": p.now(),
            "source": {"container": "db", "owner": "probe", "database": "probe"}}
    p.write_record(directory / "owner.json", copy)
    (directory / "probe.dump").write_bytes(b"synthetic")
    return directory


def stamp(hours):
    return f"{SHA[:12]}-main-{datetime.now(timezone.utc) - timedelta(hours=hours):%Y%m%dT%H%M%SZ}"


# spec: ANYM-588
def test_a_lost_docker_stop_answer_is_read_back_before_the_run_goes_on(tmp_path, monkeypatch):
    docker = FakeDocker()
    monkeypatch.setattr(p, "run", docker)
    directory = unreleased(tmp_path, stamp(1))
    assert p.reconcile_copies(tmp_path) == [directory.name]
    stop = next(index for index, argv in enumerate(docker.calls) if argv[:2] == ["docker", "stop"])
    assert any(argv[:2] == ["docker", "ps"] for argv in docker.calls[stop:])
    assert (directory / "released.json").exists() and (directory / "probe.dump").exists()


# spec: ANYM-588
def test_a_container_still_there_after_stop_pauses_with_the_copy_id(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "run", FakeDocker(stops=False))
    monkeypatch.setattr(p, "COPY_STOP_READBACK", 0)
    directory = unreleased(tmp_path, stamp(1))
    with pytest.raises(p.CopyStuck, match="a database copy could not be stopped: " + directory.name):
        p.reconcile_copies(tmp_path)
    assert not (directory / "released.json").exists()


# spec: ANYM-588
def test_copies_expire_after_48_hours_on_a_paused_run(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "run", FakeDocker())
    settings = {"state_directory": str(tmp_path / "state")}
    old, kept, _ = (unreleased(tmp_path / "state", stamp(hours)) for hours in (49, 47, 50))
    for directory in (old, kept):
        p.write_record(directory / "released.json", {"id": directory.name})
    p.PAUSE.write_text('{"status": "halted", "reason": "synthetic"}\n')
    assert p.deploy(settings, {})["status"] == "paused"
    # The unreleased old copy was released first, then expired with the rest.
    assert [path.name for path in copies(settings)] == [kept.name]
    assert (kept / "probe.dump").exists()


# spec: ANYM-588
def test_an_expired_copy_whose_resources_are_unconfirmed_keeps_only_its_owner_record(tmp_path):
    directory = unreleased(tmp_path, stamp(49))
    (directory / "scratch").mkdir()
    assert p.expire_copies(tmp_path) == [directory.name]
    assert [path.name for path in directory.iterdir()] == ["owner.json"]


# spec: ANYM-588
def test_a_planted_symlink_is_never_followed_or_removed_through(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep").write_text("synthetic")
    (tmp_path / "state").mkdir(mode=0o700)
    root = p.copies_root(tmp_path / "state", create=True)
    (root / stamp(60)).symlink_to(target)
    assert p.expire_copies(tmp_path / "state") == [] and p.reconcile_copies(tmp_path / "state") == []
    assert (target / "keep").exists()
    with pytest.raises(FileExistsError):
        p.new_file(root / stamp(60))
    other = tmp_path / "other"
    other.mkdir()
    (other / p.COPIES).symlink_to(target)
    with pytest.raises(p.Refused, match="private operator-owned directory"):
        p.copies_root(other)
