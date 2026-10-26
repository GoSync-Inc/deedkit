"""The shared rules of every `deedkit admin` write (ANYM-493 (a)-(h), production.md "Admin CLI"), proven on `key issue`
and `undo` over a synthetic host: tests/deedkit_admin_fakes.py."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import pytest

from scripts import deedkit_admin as admin
from scripts import seamless_release as s
from tests.deedkit_admin_fakes import Site, container, private

FAKES = Path(__file__).with_name("deedkit_admin_fakes.py")


class Crash(BaseException):
    pass


@pytest.fixture
def site(tmp_path, monkeypatch):
    umask = os.umask(0o022)
    os.umask(umask)
    yield Site(tmp_path, monkeypatch, admin, s)
    os.umask(umask)


def issue(site, capsys, new="clone"):
    code = site.main("key", "issue", new, "--like", "src", "--days", "30")
    out, err = capsys.readouterr()
    return code, (json.loads(out) if code == 0 else None), err


def undo(site, capsys, operation):
    code = site.main("undo", operation)
    out, err = capsys.readouterr()
    return code, (json.loads(out) if code == 0 else None), err


def entry(document, name):
    return next((row for row in document["principals"] if row["id"] == name), None)


def refused(site, capsys, message):
    before = site.snapshot()
    code, _, err = issue(site, capsys)
    assert code == 2 and message in err and "nothing changed" in err, err
    assert site.snapshot() == before and site.operator.state["issued"] == []


@contextmanager
def held(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


# (a) Locks and unresolved releases

# spec: ANYM-493
@pytest.mark.parametrize("directory", ["state/global", "state/http", "state/a"])
def test_a_release_lock_held_refuses_a_deploy_is_running(site, capsys, directory):
    with held(site.root / directory / "deploy.lock"):
        refused(site, capsys, "a deploy is running")


# spec: ANYM-493
def test_the_calls_registry_writer_lock_held_refuses(site, capsys):
    with held(Path(site.config["calls_lock"])):
        refused(site, capsys, "holds the calls-registry writer lock")


# spec: ANYM-493
def test_locks_are_tried_for_lock_wait_seconds_then_refuse(site, capsys):
    started = time.monotonic()
    with held(site.root / "state/global/deploy.lock"):
        refused(site, capsys, "a deploy is running")
    assert admin.LOCK_WAIT <= time.monotonic() - started < admin.LOCK_WAIT + 5


# spec: ANYM-493
def test_release_locks_come_before_the_calls_registry_lock(site, capsys, monkeypatch):
    order, host_locks, flock = [], site.tool.host_locks, fcntl.flock

    @contextmanager
    def recorded(*args):
        with host_locks(*args):
            order.append("release")
            yield

    def recorded_flock(handle, flags):  # fcntl is one module: the release locks call it too
        if handle.name == site.config["calls_lock"]:
            order.append("calls-registry")
        return flock(handle, flags)
    monkeypatch.setattr(site.tool, "host_locks", recorded)
    monkeypatch.setattr(admin.fcntl, "flock", recorded_flock)
    assert issue(site, capsys)[0] == 0
    assert order[:2] == ["release", "calls-registry"]


def marker(site, directory, name, value):
    private(site.root / directory / ("b" * 40) / name, value)


UNRESOLVED = {
    "plan A apply without result": (lambda site: marker(site, "state/a", "attempted.json", {}),
                                    "an apply or rollback is unfinished"),
    "plan A rollback without result": (lambda site: marker(site, "state/a", "rollback-attempted.json", {}),
                                       "an apply or rollback is unfinished"),
    "HTTP attempt unresolved": (lambda site: marker(site, "state/http", "http-attempted.json", {"status": "prepared"}),
                                "an HTTP release attempt is unresolved"),
    "HTTP serving attempt unpublished": (lambda site: marker(site, "state/http", "http-attempted.json",
                                                             {"status": "serving"}),
                                         "http-current.json does not name it"),
    "deploy pause": (lambda site: site.tool.PAUSE.write_text(""), "deploys are paused"),
}


# spec: ANYM-493
@pytest.mark.parametrize("case", list(UNRESOLVED))
def test_an_unresolved_release_or_pause_refuses(site, capsys, case):
    prepare, message = UNRESOLVED[case]
    prepare(site)
    refused(site, capsys, message)


# (b) Active call

def probe_timeout(settings):
    raise s.p.CallsUnknown("probe_timeout")


def probe_unreadable(settings):
    raise ValueError("malformed")


# spec: ANYM-493
@pytest.mark.parametrize("case", ["active", "timeout", "unreadable"])
def test_an_active_or_unknown_call_state_refuses(site, capsys, monkeypatch, case):
    if case == "active":
        site.calls.append("call-1 (in_progress)")
    else:
        monkeypatch.setattr(site.tool, "active_calls", probe_timeout if case == "timeout" else probe_unreadable)
    refused(site, capsys, "a call is active" if case == "active" else "the call state is unreadable")


# (c) Validation in every consumer's image

# spec: ANYM-493
def test_a_candidate_a_running_image_rejects_is_never_renamed(site, capsys):
    site.docker.images["voice:1"] = lambda path: '"clone"' not in path.read_text()  # the live registry passes
    registry = Path(site.config["calls_registry"]).read_bytes()
    code, _, err = issue(site, capsys)
    assert code == 1 and "refused by image voice:1" in err
    assert Path(site.config["calls_registry"]).read_bytes() == registry and entry(site.policy(), "clone") is None
    assert not [p for p in site.root.rglob(".*admin-*")]


# spec: ANYM-493
def test_a_field_only_a_newer_image_accepts_is_refused_by_the_rollback_image(site, capsys):
    site.principals[0]["note"] = "a field the running gateway:new accepts and gateway:old does not"
    site.write_policy()
    policy = Path(site.config["policy"]).read_bytes()
    code, _, err = issue(site, capsys)
    # The live policy already carries the field, so the preflight refuses before any write.
    assert code == 2 and "refused by image gateway:old" in err, err
    assert Path(site.config["policy"]).read_bytes() == policy and site.operator.state["issued"] == []
    images = [call[call.index("--entrypoint") + 2] for call in site.docker.calls if call[0] == "run"]
    assert {"gateway:new", "gateway:old"} <= set(images)


# (d) Containers

# spec: ANYM-493
def test_it_never_touches_a_service_container(site, capsys):
    containers = json.dumps(site.docker.containers)
    code, result, err = issue(site, capsys)
    assert code == 0, err
    assert undo(site, capsys, result["operation"])[0] == 0
    assert json.dumps(site.docker.containers) == containers
    assert {call[0] for call in site.docker.calls} == {"ps", "inspect", "run"}
    runs = [call for call in site.docker.calls if call[0] == "run"]
    names = [call[call.index("--name") + 1] for call in runs]
    assert all("--rm" in call and "--network" in call and "none" in call for call in runs)
    assert len(set(names)) == len(names) and all(name.startswith("deedkit-admin-") for name in names)


# spec: ANYM-493
def test_a_check_container_that_times_out_is_stopped(tmp_path, monkeypatch):
    log = tmp_path / "docker.log"
    fake = tmp_path / "docker.py"
    fake.write_text("import json, sys, time\nopen(sys.argv[1], 'a').write(json.dumps(sys.argv[2:]) + '\\n')\n"
                    "time.sleep(5) if sys.argv[2] == 'run' else None\n")
    monkeypatch.setattr(admin, "TIMEOUT", 0.5)
    docker = admin.dockers({"docker": [sys.executable, str(fake), str(log)]})
    with pytest.raises(admin.AdminError, match="TimeoutExpired"):
        docker(["run", "--rm", "--name", "deedkit-admin-0123456789ab", "image"], "deedkit-admin-0123456789ab")
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[-1] == ["stop", "--time", "5", "deedkit-admin-0123456789ab"]


# (e) Journal, rerun and undo

# spec: ANYM-493
def test_the_journal_is_private_and_records_every_step(site, capsys):
    code, result, err = issue(site, capsys)
    assert code == 0, err
    path = site.root / "ops" / f"{result['operation']}.json"
    assert path.parent.stat().st_mode & 0o777 == 0o700 and path.stat().st_mode & 0o777 == 0o600
    journal = site.journal(result["operation"])
    assert journal["state"] == "done" and journal["command"]["id"] == "clone"
    assert {name: step["state"] for name, step in journal["steps"].items()} == dict.fromkeys(
        ["key files", "messaging", "messaging-archive", "calendar", "calls archive configuration", "calls registry",
         "gateway policy"], "done")
    assert [journal["steps"][step]["operation_id"] for step in ("messaging", "messaging-archive", "calendar")] == [
        f"{result['operation']}/{step}" for step in ("messaging", "messaging-archive", "calendar")]
    assert journal["steps"]["gateway policy"]["entry"] == admin.entry_digest(entry(site.policy(), "clone"))


# spec: ANYM-493
def test_a_failure_prints_both_choices_and_a_rerun_never_mints_a_new_operation(site, capsys):
    site.docker.images["voice:1"] = lambda path: '"clone"' not in path.read_text()
    code, _, err = issue(site, capsys)
    operation = err.split("operation ", 1)[1].split(" ", 1)[0]
    assert code == 1 and f"--operation {operation}" in err and f"deedkit admin undo {operation}" in err
    assert site.journal(operation)["state"] == "failed"
    issued = list(site.operator.state["issued"])
    site.docker.images["voice:1"] = lambda path: True
    assert site.main("key", "issue", "clone", "--like", "src", "--days", "30", "--operation", operation) == 0
    capsys.readouterr()
    assert site.operator.state["issued"] == issued  # every backend step replayed by its operation ID
    assert [p.stem for p in (site.root / "ops").glob("*.json")] == [operation]


def edit_policy(site, name, change):
    policy = site.policy()
    change(entry(policy, name))
    policy["revision"] += 1
    private(Path(site.config["policy"]), policy)


# spec: ANYM-493
def test_undo_a_keeps_a_later_revoke_of_b(site, capsys):
    code, result, err = issue(site, capsys)
    assert code == 0, err
    edit_policy(site, "other", lambda row: row.update(enabled=False))  # B revoked by someone else afterwards
    site.operator("messaging", ["revoke-key", "--key-id", "key-other"])
    code, _, err = undo(site, capsys, result["operation"])
    assert code == 0, err
    assert entry(site.policy(), "other")["enabled"] is False and entry(site.policy(), "clone")["enabled"] is False
    assert site.operator.state["messaging"]["keys"]["key-other"]["revoked"] is True


# spec: ANYM-493
def test_undo_a_keeps_a_later_issue_of_c(site, capsys):
    code, first, err = issue(site, capsys)
    assert code == 0, err
    code, second, err = issue(site, capsys, new="c")
    assert code == 0, err
    assert undo(site, capsys, first["operation"])[0] == 0
    assert entry(site.policy(), "c")["enabled"] is True and entry(site.registry_doc(), "c")["enabled"] is True
    state = site.operator.state
    assert not any(k["revoked"] for k in state["messaging"]["keys"].values() if k["principal"].startswith("c-")
                   or k["principal"] == "c")
    assert state["calendar"]["agents"]["c"]["revoked"] is False and Path(second["token_file"]).exists()
    assert entry(site.policy(), "clone")["enabled"] is False


# spec: ANYM-493
def test_undo_refuses_when_its_entry_changed_since(site, capsys):
    code, result, err = issue(site, capsys)
    assert code == 0, err
    edit_policy(site, "clone", lambda row: row.update(grants={}))
    before = site.snapshot()
    code, _, err = undo(site, capsys, result["operation"])
    assert code == 2 and "changed since this operation wrote it" in err
    assert site.snapshot() == before


# (f) Exclusive credentials

# spec: ANYM-493
@pytest.mark.parametrize("sharing", ["direct", "nested archive"])
def test_undo_refuses_a_credential_another_principal_uses(site, capsys, sharing):
    code, result, err = issue(site, capsys)
    assert code == 0, err
    if sharing == "direct":
        policy = site.policy()
        policy["principals"].append(dict(entry(policy, "other"), id="borrower", key_sha256="1" * 64,
                                         credentials={"messaging": entry(policy, "clone")["credentials"]["messaging"]}))
        policy["revision"] += 1
        private(Path(site.config["policy"]), policy)
        user = "borrower"
    else:
        registry = site.registry_doc()
        registry["principals"].append(dict(entry(registry, "other"), id="borrower", key_sha256="2" * 64,
                                           archive_config="/run/secrets/clone-calls-archive.json"))
        registry["revision"] += 1
        private(Path(site.config["calls_registry"]), registry)
        user = "borrower (calls)"
    before = site.snapshot()
    code, _, err = undo(site, capsys, result["operation"])
    assert code == 2 and f"is used by {user}" in err
    assert site.snapshot() == before


def crash_at(monkeypatch, point):
    def checkpoint(step, phase):
        if (step, phase) == point:
            raise Crash(point)
    monkeypatch.setattr(admin, "checkpoint", checkpoint)


def compensated(site):
    state = site.operator.state
    assert all(k["revoked"] for k in state["messaging"]["keys"].values() if k["principal"].startswith("clone"))
    assert state["calendar"]["agents"]["clone"]["revoked"] is True
    for document in (site.policy(), site.registry_doc()):
        assert entry(document, "clone") is None or entry(document, "clone")["enabled"] is False
    assert not list(site.keys.glob("clone-*")) and not list(site.voice.glob("clone-*"))
    assert not list((site.root / "admin-tokens").glob("*"))


# spec: ANYM-493
def test_a_crash_before_the_gateway_step_is_compensated(site, capsys, monkeypatch):
    crash_at(monkeypatch, ("calls registry", "renamed"))
    with pytest.raises(Crash):
        site.main("key", "issue", "clone", "--like", "src", "--days", "30")
    [operation] = [p.stem for p in (site.root / "ops").glob("*.json")]
    monkeypatch.setattr(admin, "checkpoint", lambda step, phase: None)
    code, _, err = undo(site, capsys, operation)
    assert code == 0, err
    compensated(site)
    assert entry(site.policy(), "clone") is None


# spec: ANYM-493
@pytest.mark.parametrize("point", [("undo gateway policy", "renamed"), ("undo calls registry", "renamed"),
                                   ("undo messaging", "committed"), ("undo calendar", "committed")])
def test_a_crash_during_undo_is_finished_by_undo_again(site, capsys, monkeypatch, point):
    code, result, err = issue(site, capsys)
    assert code == 0, err
    crash_at(monkeypatch, point)
    with pytest.raises(Crash):
        site.main("undo", result["operation"])
    monkeypatch.setattr(admin, "checkpoint", lambda step, phase: None)
    code, undone, err = undo(site, capsys, result["operation"])
    assert code == 0 and undone["undone"] is True, err
    compensated(site)
    assert site.journal(result["operation"])["state"] == "undone"


# (g) Secrets

# spec: ANYM-493
def test_no_secret_reaches_output_argv_environment_or_the_journal(site, capsys, monkeypatch):
    state = site.root / "operator.json"
    for service in ("messaging", "calendar"):
        site.config[service] = [sys.executable, str(FAKES), str(state), service]
    private(site.root / "admin/config.json", site.config)
    seen = []

    def run(*argv):
        code = site.main(*argv, run=admin.runner(site.config))
        out, err = capsys.readouterr()
        seen.extend([out, err])
        return code, out
    assert run("key", "issue", "clone", "--like", "src", "--days", "30", "--dry-run")[0] == 0
    code, out = run("key", "issue", "clone", "--like", "src", "--days", "30")
    assert code == 0
    result = json.loads(out)
    created = {name: path for name, path in site.journal(result["operation"])["files"].items() if name != "archive-config"}
    secrets = list(site.tokens.values()) + [Path(path).read_text().strip() for path in created.values()]
    site.tool.PAUSE.write_text("")
    assert run("key", "issue", "c2", "--like", "src", "--days", "30")[0] == 2  # a refusal
    site.tool.PAUSE.unlink()
    site.docker.images["voice:1"] = lambda path: '"c3"' not in path.read_text()
    assert run("key", "issue", "c3", "--like", "src", "--days", "30")[0] == 1  # a failure after the journal
    secrets += [Path(p).read_text().strip() for p in (site.root / "admin-tokens").glob("c3*")]
    secrets += [Path(p).read_text().strip() for p in site.keys.glob("c3-*")]
    site.docker.images["voice:1"] = lambda path: True
    assert run("undo", result["operation"])[0] == 0
    calls = (Path(str(state) + ".calls")).read_text()
    assert calls.count('"argv"') > 10  # the real runner started the operator for every command
    haystacks = seen + [calls, json.dumps(site.docker.calls), json.dumps(site.operator.state["log"])] + [
        p.read_text() for p in (site.root / "ops").glob("*.json")]
    assert len(secrets) >= 14
    for secret in secrets:
        for needle in (secret, hashlib.sha256(secret.encode()).hexdigest()):
            assert not [text for text in haystacks if needle in text], secret[:20]


# (h) Versions

# spec: ANYM-493
def test_an_operator_image_without_the_command_refuses_naming_the_service(site, capsys):
    site.operator.change(lambda state: state["missing"].update(messaging=["principal-info"]))
    refused(site, capsys, "the messaging image does not answer `principal-info --check`")


# spec: ANYM-493
def test_a_store_one_migration_behind_refuses_naming_the_service(site, capsys):
    site.operator.change(lambda state: state["schema"].update(calendar="schema_not_ready"))
    refused(site, capsys, "calendar: `agent-scope --check` answered schema_not_ready")


# r1 regressions (Astra, PR #290)

# spec: ANYM-493
def test_an_active_call_in_a_voice_consumer_outside_the_deploy_targets_refuses(site, capsys):
    # Deploy's settings name no calls target, so active_calls finds nothing; the running registry consumer is probed.
    site.voice_calls[site.voice_container["Id"]] = ["call-9 (in_progress)"]
    refused(site, capsys, "a call is active (call-9 (in_progress))")


# spec: ANYM-493
def test_the_host_tool_reads_the_deploy_configuration_autodeploy_selects(tmp_path, monkeypatch):
    tool = tmp_path / "tool"
    tool.mkdir()
    (tool / "production.py").write_text(
        "import os\nclass Refused(Exception): pass\nCONFIG, HTTP_CONFIG = '/etc/default.json', '/etc/http.json'\n"
        "def active_config(): return os.environ.get('DEEDKIT_DEPLOY_CONFIG') or CONFIG\n"
        "def config(path): return {'path': path}\ndef unresolved(*args): return None\n")
    (tool / "seamless_release.py").write_text("def validate_settings(value): return value\n")
    monkeypatch.setenv("DEEDKIT_DEPLOY_CONFIG", "/etc/deedkit/plan-b.json")
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    host = admin.load_host({"tool": str(tool)})
    assert host.settings == {"path": "/etc/deedkit/plan-b.json"} and host.http == {"path": "/etc/http.json"}


# spec: ANYM-493
def test_an_image_without_the_check_that_exits_zero_silently_is_refused(site, capsys):
    site.docker.images["voice:1"] = lambda path: "silent"  # an older voice image: no --check-registry entry point
    code, _, err = issue(site, capsys)
    assert code == 2 and "refused by image voice:1" in err and "has no such check" in err
    # The preflight refuses before any write: no credential, journal, key file or staging file.
    assert entry(site.registry_doc(), "clone") is None and site.operator.state["issued"] == []
    assert not list((site.root / "ops").glob("*.json")) and not list(site.keys.glob("clone-*"))
    assert not [p for p in site.root.rglob("*") if p.name.startswith(".") and ".admin-" in p.name]


# spec: ANYM-493
@pytest.mark.parametrize("case", ["copied archive token", "unreadable archive configuration"])
def test_undo_refuses_a_credential_it_cannot_prove_exclusive(site, capsys, case):
    code, result, err = issue(site, capsys)
    assert code == 0, err
    config = site.voice / "borrowed-archive.json"
    if case == "copied archive token":
        # Another file holds the same token: the same backend credential under a path undo never wrote.
        private(site.voice / "borrowed.token", (site.voice / "clone-calls-archive.token").read_text())
        private(config, {"base_url": "http://messaging.invalid", "token_file": "/run/secrets/borrowed.token"})
    else:
        private(config, "not json")
    registry = site.registry_doc()
    registry["principals"].append(dict(entry(registry, "other"), id="borrower", key_sha256="2" * 64,
                                       archive_config="/run/secrets/borrowed-archive.json"))
    registry["revision"] += 1
    private(Path(site.config["calls_registry"]), registry)
    before = site.snapshot()
    code, _, err = undo(site, capsys, result["operation"])
    assert code == 2 and "is used by borrower (calls" in err, err
    assert site.snapshot() == before


# spec: ANYM-493
def test_undo_checks_ownership_on_the_document_it_replaces(site, capsys, monkeypatch):
    code, result, err = issue(site, capsys)
    assert code == 0, err
    images = admin.rollback_images

    def change_then(host):  # someone edits the clone after undo's preflight, before its compare-and-swap
        edit_policy(site, "clone", lambda row: row.update(grants={}))
        return images(host)
    monkeypatch.setattr(admin, "rollback_images", change_then)
    code, _, err = undo(site, capsys, result["operation"])
    assert code == 1 and "changed since this operation wrote it" in err
    assert entry(site.policy(), "clone")["enabled"] is True and entry(site.policy(), "clone")["grants"] == {}


# spec: ANYM-493
def test_undo_refuses_an_entry_someone_else_disabled_and_changed(site, capsys):
    code, result, err = issue(site, capsys)
    assert code == 0, err
    edit_policy(site, "clone", lambda row: row.update(enabled=False, grants={}))
    before = site.snapshot()
    code, _, err = undo(site, capsys, result["operation"])
    assert code == 2 and "changed since this operation wrote it" in err
    assert site.snapshot() == before


# r2 regressions (Astra, PR #290)

# spec: ANYM-493
def test_an_active_call_in_a_consumer_that_mounts_an_ancestor_directory_refuses(site, capsys):
    second = container("other-voice-1", "voice:1", (site.root, "/srv"))  # reads the registry through /srv/voice
    site.docker.containers.append(second)
    site.voice_calls[second["Id"]] = ["call-7 (in_progress)"]
    refused(site, capsys, "a call is active (call-7 (in_progress))")


# spec: ANYM-493
def test_a_consumer_that_mounts_an_ancestor_directory_checks_the_candidate_through_that_mount(site, capsys):
    site.docker.containers.append(container("other-voice-1", "voice:2", (site.root, "/srv")))
    site.docker.images["voice:2"] = lambda path: True
    code, _, err = issue(site, capsys)
    assert code == 0, err
    runs = [call for call in site.docker.calls if call[0] == "run" and "voice:2" in call]
    assert runs and all(f"source={site.root},target=/srv," in call[call.index("--mount") + 1] for call in runs)
    assert any(part.startswith("/srv/voice/.agents.json.admin-") for call in runs for part in call)
    # A consumer that cannot answer a check refuses instead of going unchecked.
    site.docker.images["voice:2"] = lambda path: False
    code = site.main("key", "issue", "c2", "--like", "src", "--days", "30")
    err = capsys.readouterr().err
    assert code == 2 and "refused by image voice:2" in err, err


# spec: ANYM-493
def test_an_admin_configuration_cannot_override_the_active_deploy_configuration(tmp_path):
    with pytest.raises(admin.AdminError, match="production_config is not an admin setting"):
        admin.load_host({"tool": str(tmp_path), "production_config": "/etc/deedkit/other.json"})


# spec: ANYM-493
@pytest.mark.parametrize("case", ["unreadable borrowed token", "own archive token gone"])
def test_undo_refuses_when_a_token_identity_is_unknown(site, capsys, case):
    code, result, err = issue(site, capsys)
    assert code == 0, err
    borrowed = site.voice / "borrowed.token"
    private(borrowed, (site.voice / "clone-calls-archive.token").read_text())
    private(site.voice / "borrowed-archive.json", {"base_url": "http://messaging.invalid",
                                                   "token_file": "/run/secrets/borrowed.token"})
    registry = site.registry_doc()
    registry["principals"].append(dict(entry(registry, "other"), id="borrower", key_sha256="2" * 64,
                                       archive_config="/run/secrets/borrowed-archive.json"))
    registry["revision"] += 1
    private(Path(site.config["calls_registry"]), registry)
    if case == "unreadable borrowed token":
        borrowed.chmod(0o644)  # not owner-only: its token cannot be read, so it cannot be ruled out
        message = "is used by borrower (calls"
    else:
        (site.voice / "clone-calls-archive.token").unlink()  # nothing left to compare the borrower's token with
        message = "clone-calls-archive.token this operation wrote is unreadable or gone"
    before = site.snapshot()
    code, _, err = undo(site, capsys, result["operation"])
    assert code == 2 and message in err, err
    assert site.snapshot() == before


# r3 regressions (Astra, PR #290)

# spec: ANYM-493
@pytest.mark.parametrize("credential", ["archive token", "calls key"])
def test_undo_refuses_a_readable_token_that_is_not_its_credential(site, capsys, credential):
    code, result, err = issue(site, capsys)
    assert code == 0, err
    files = site.journal(result["operation"])["files"]
    registry = site.registry_doc()
    if credential == "archive token":
        # The borrower holds a copy of the clone's archive token; the clone's own file then gets another token.
        own = Path(files["archive-token"])
        private(site.voice / "borrowed.token", own.read_text())
        private(site.voice / "borrowed-archive.json", {"base_url": "http://messaging.invalid",
                                                       "token_file": "/run/secrets/borrowed.token"})
        registry["principals"].append(dict(entry(registry, "other"), id="borrower", key_sha256="2" * 64,
                                           archive_config="/run/secrets/borrowed-archive.json"))
    else:  # no longer the token the clone's registry pin names
        own = Path(files["calls"])
    registry["revision"] += 1
    private(Path(site.config["calls_registry"]), registry)
    private(own, "a-well-formed-replacement-token")
    own.chmod(0o600)
    before = site.snapshot()
    code, _, err = undo(site, capsys, result["operation"])
    assert code == 2 and f"{own} no longer holds the credential this operation created" in err, err
    assert site.snapshot() == before


# spec: ANYM-493
@pytest.mark.parametrize("image", ["voice:1", "gateway:old"])
def test_undo_refuses_a_validator_without_the_check_before_any_write(site, capsys, image):
    code, result, err = issue(site, capsys)
    assert code == 0, err
    site.docker.images[image] = lambda path: "silent"  # an image that exits 0 having checked nothing
    journal = site.journal(result["operation"])
    before = site.snapshot()
    code, _, err = undo(site, capsys, result["operation"])
    assert code == 2 and f"refused by image {image}" in err and "has no such check; nothing changed" in err, err
    assert site.snapshot() == before and site.journal(result["operation"]) == journal
    assert entry(site.policy(), "clone")["enabled"] is True and entry(site.registry_doc(), "clone")["enabled"] is True
