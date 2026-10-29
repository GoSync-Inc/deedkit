"""`deedkit admin key issue --like` and `undo` (ANYM-496) over a synthetic host: tests/deedkit_admin_fakes.py."""
import importlib
import json
import os
from pathlib import Path
import shlex
import tempfile
import time
import uuid

import pytest

from scripts import deedkit_admin as admin
from scripts import seamless_release as s
from tests.deedkit_admin_fakes import CATALOG, LEGACY, PAST, Policy, Site, container, gateway_rule

POINTS = [("key files", name) for name in ("gateway", "messaging", "calendar", "calls", "archive-token")] + [
    ("messaging", "committed"), ("messaging-archive", "committed"), ("calendar", "committed"),
    ("calls archive configuration", "written"), ("calls registry", "renamed"), ("gateway policy", "renamed")]


class Crash(BaseException):
    """The process dies at a checkpoint: no handler of the CLI runs."""


@pytest.fixture
def site(tmp_path, monkeypatch):
    umask = os.umask(0o022)
    os.umask(umask)
    yield Site(tmp_path, monkeypatch, admin, s)
    os.umask(umask)  # main() sets 0077 for its own files


def issue(site, capsys, *extra, new="clone"):
    code = site.main("key", "issue", new, "--like", "src", "--days", "30", *extra)
    out, err = capsys.readouterr()
    return code, (json.loads(out) if code == 0 else None), err


def entry(document, name):
    return next((row for row in document["principals"] if row["id"] == name), None)


def operation_of(err):
    return err.split("operation ", 1)[1].split(" ", 1)[0]


def crash_at(monkeypatch, point):
    def checkpoint(step, phase):
        if (step, phase) == point:
            raise Crash(point)
    monkeypatch.setattr(admin, "checkpoint", checkpoint)


def granted(state, principal, key):
    """What a messaging key may do: the principal's permissions within the key's ceiling (policy.effective_permissions;
    a NULL ceiling is the legacy set)."""
    ceiling = LEGACY if key["permissions"] is None else key["permissions"]
    return sorted(set(state["messaging"]["principals"][principal]["permissions"]) & set(ceiling))


def effective(site, principal):
    """The tools and messenger actions live discovery shows: the policy filter (Policy.visible/actions), then the
    backend-scope filter of services/gateway/src/agent_gateway/dispatch.py:244-252 fed with each backend's own scopes."""
    state, snapshot = site.operator.state, Policy(site.config["policy"]).load()
    agent = entry(snapshot, principal)
    key = next(k for k in state["messaging"]["keys"].values() if k["principal"] == principal and not k["revoked"])
    scopes = {"messaging": set(granted(state, principal, key)), "calendar": set(state["calendar"]["agents"][principal]["scopes"]),
              "calls": set(entry(site.registry_doc(), principal)["scopes"])}
    visible = Policy.visible(snapshot, agent)
    tools = [n for n in visible if CATALOG[n]["module"] == "gateway" or set(CATALOG[n]["scopes"]) <= scopes[CATALOG[n]["module"]]]
    return visible, tools, Policy.actions(snapshot, agent, scopes["messaging"])


# spec: ANYM-496
def test_issue_like_clones_the_source_in_every_store(site, capsys):
    before_policy, before_registry, before = site.policy(), site.registry_doc(), site.operator.state
    code, result, err = issue(site, capsys)
    assert code == 0, err
    policy, registry, state = site.policy(), site.registry_doc(), site.operator.state
    messaging, agents = state["messaging"], state["calendar"]["agents"]
    for clone, source in (("clone", "src"), ("clone-calls-archive", "src-calls-archive")):
        assert messaging["principals"][clone] == messaging["principals"][source]
        keys = {p: [granted(state, p, k) for k in messaging["keys"].values() if k["principal"] == p]
                for p in (clone, source)}
        assert keys[clone] == keys[source]
    assert {k: v for k, v in agents["clone"].items() if k not in ("digest", "expires_at", "operation_id")} == {
        k: v for k, v in agents["src"].items() if k not in ("digest", "expires_at", "operation_id")}
    calls, source_calls = entry(registry, "clone"), entry(registry, "src")
    fields = ("id", "key_sha256", "expires_at", "archive_config")
    assert {k: v for k, v in calls.items() if k not in fields} == {k: v for k, v in source_calls.items() if k not in fields}
    archive = json.loads((site.voice / "clone-calls-archive.json").read_text())
    assert calls["archive_config"] == "/run/secrets/clone-calls-archive.json"
    assert archive == {"base_url": "http://messaging.invalid", "token_file": "/run/secrets/clone-calls-archive.token"}
    gateway, source_gateway = entry(policy, "clone"), entry(policy, "src")
    for field in ("profile", "grants", "actions", "enabled"):
        assert gateway[field] == source_gateway[field]
    assert {b: c["key_file"] for b, c in gateway["credentials"].items()} == {
        b: f"/run/deedkit/keys/clone-{b}.key" for b in ("messaging", "calendar", "calls")}
    # No other principal changed, in any store; nothing was issued for an existing principal.
    assert [row for row in policy["principals"] if row["id"] != "clone"] == before_policy["principals"]
    assert [row for row in registry["principals"] if row["id"] != "clone"] == before_registry["principals"]
    assert (policy["revision"], registry["revision"]) == (before_policy["revision"] + 1, before_registry["revision"] + 1)
    assert {k: v for k, v in messaging["keys"].items() if not v["principal"].startswith("clone")} == \
        before["messaging"]["keys"]
    assert {k: v for k, v in agents.items() if k != "clone"} == before["calendar"]["agents"]
    assert state["violations"] == [] and sorted(state["issued"]) == sorted(
        f"{result['operation']}/{step}" for step in ("messaging", "messaging-archive", "calendar"))
    # The new tokens: owner-only files, the gateway's in a 0700 directory, each pinned where it is used.
    token = Path(result["token_file"])
    assert token.parent.stat().st_mode & 0o777 == 0o700
    for path in [token, *site.keys.glob("clone-*.key"), site.voice / "clone-calls-archive.token"]:
        assert path.stat().st_mode & 0o777 == 0o600
    assert gateway["key_sha256"] == admin.hashlib.sha256(token.read_text().strip().encode()).hexdigest()


# spec: ANYM-496
def test_issue_like_shows_the_clone_the_sources_tools_and_actions(site, capsys):
    assert issue(site, capsys)[0] == 0
    visible, tools, actions = effective(site, "src")
    # The source's backend scopes are narrower than its gateway grants, so the backend filter removes tools.
    assert set(tools) < set(visible) and "messages_prepare_file_upload" not in tools
    assert actions == ["archive_chat"]  # granted add_contact needs contacts-manage, which the backend key lacks
    assert effective(site, "clone") == (visible, tools, actions)


# spec: ANYM-496
def test_issue_like_gateway_never_outlives_a_backend(site, capsys):
    site.operator.change(lambda state: state["offset_hours"].update(calendar=5))
    code, result, err = issue(site, capsys)
    assert code == 0, err
    calendar = next(c["expires_at"] for c in result["credentials"] if c["store"] == "calendar")
    assert entry(site.policy(), "clone")["expires_at"] == calendar == result["expires_at"]
    assert entry(site.registry_doc(), "clone")["expires_at"] == calendar
    assert all(admin.instant(c["expires_at"]) >= admin.instant(calendar) for c in result["credentials"])


# spec: ANYM-496
def test_issue_like_prints_public_ids_the_undo_and_the_mac_step(site, capsys, tmp_path):
    code, result, err = issue(site, capsys)
    assert code == 0, err
    state = site.operator.state
    ids = {c["store"]: c["public_id"] for c in result["credentials"]}
    assert ids == {"messaging": next(k for k, v in state["messaging"]["keys"].items() if v["principal"] == "clone"),
                   "messaging-archive": next(k for k, v in state["messaging"]["keys"].items()
                                             if v["principal"] == "clone-calls-archive"),
                   "calendar": "clone", "calls": "clone"}
    assert result["undo"] == f"deedkit admin undo {result['operation']}"
    assert result["token_file"] == str(site.root / "admin-tokens/clone.token")
    # ANYM-498: the single Mac step is `deedkit key install` (tests/test_deedkit_mac_install.py covers it).
    assert [shlex.split(line) for line in result["next"]] == [
        ["python3", "scripts/deedkit.py", "key", "install", "clone", "--host", "<ssh-destination>",
         "--url", "https://deedkit.test/deedkit/mcp"]]


# spec: ANYM-496
def test_issue_dry_run_writes_nothing(site, capsys):
    before = site.snapshot()
    code, result, err = issue(site, capsys, "--dry-run")
    assert code == 0, err
    assert site.snapshot() == before and site.operator.state["issued"] == []
    assert not (site.root / "ops").exists() and not (site.root / "admin-tokens").exists()
    assert result["dry_run"] is True and result["gateway_policy"]["revision"] == site.policy()["revision"] + 1
    assert result["gateway_policy"]["add"]["key_sha256"] == "<sha256 of the new key>"
    assert {c["key_sha256"] for c in result["gateway_policy"]["add"]["credentials"].values()} == {"<sha256 of the new key>"}
    assert result["calls_registry"]["add"]["key_sha256"] == "<sha256 of the new key>"
    assert result["steps"] == ["key files", "messaging", "messaging-archive", "calendar", "calls archive configuration",
                               "calls registry", "gateway policy"]


def disable_source(site):
    site.principals[0]["enabled"] = False
    site.write_policy()


def expire_source(site):
    site.principals[0]["expires_at"] = PAST
    site.write_policy()


def single_file(site, what):
    path = site.config["policy" if what == "policy" else "calls_registry"]
    site.docker.containers.append(container(f"pinned-{what}", "gateway:new", (Path(path), "/etc/pinned.json")))


def registry_holds_clone(site):
    site.registry.append(dict(site.registry[1], id="clone", key_sha256="0" * 64))
    Path(site.config["calls_registry"]).write_text(json.dumps({"version": 1, "revision": 5, "principals": site.registry}))


def store_holds(service, name):
    def prepare(site):
        def edit(state):
            rows = state["messaging"]["principals"] if service == "messaging" else state["calendar"]["agents"]
            rows[name] = rows["src"]
        site.operator.change(edit)
    return prepare


def nothing(site):
    pass


ARGS = ("clone", "src", "30")
REFUSALS = {  # case: (prepare, (new ID, source, days), refusal)
    "unknown source": (nothing, ("clone", "nobody", "30"), "no gateway principal"),
    "disabled source": (disable_source, ARGS, "disabled or expired"),
    "expired source": (expire_source, ARGS, "disabled or expired"),
    "id in the gateway policy": (nothing, ("other", "src", "30"), "exists in gateway policy"),
    "id in the calls registry": (registry_holds_clone, ARGS, "exists in calls registry"),
    "id in messaging": (store_holds("messaging", "clone"), ARGS, "exists in messaging (clone)"),
    "archive id in messaging": (store_holds("messaging", "clone-calls-archive"), ARGS,
                                "exists in messaging (clone-calls-archive)"),
    "id in calendar": (store_holds("calendar", "clone"), ARGS, "exists in calendar"),
    "zero days": (nothing, ("clone", "src", "0"), "--days is 1-30"),
    "31 days": (nothing, ("clone", "src", "31"), "--days is 1-30"),
    "single-file policy mount": (lambda site: single_file(site, "policy"), ARGS,
                                 "mounts the gateway policy as a single file"),
    "single-file registry mount": (lambda site: single_file(site, "registry"), ARGS,
                                   "mounts the calls registry as a single file"),
    "a shared rule": (lambda site: site.tool.PAUSE.write_text(""), ARGS, "deploys are paused"),
}


# spec: ANYM-496
@pytest.mark.parametrize("case", list(REFUSALS))
def test_issue_refuses_before_any_write(site, capsys, case):
    prepare, (new, like, days), message = REFUSALS[case]
    prepare(site)
    before = site.snapshot()
    code = site.main("key", "issue", new, "--like", like, "--days", days)
    err = capsys.readouterr().err
    assert code == 2 and message in err, err
    assert site.snapshot() == before and site.operator.state["issued"] == []
    assert site.operator.state["violations"] == [] and not (site.root / "ops").exists()


# spec: ANYM-496
@pytest.mark.parametrize("point", POINTS, ids=[f"{step}-{phase}" for step, phase in POINTS])
def test_issue_crash_then_rerun_finishes_without_issuing_twice(site, capsys, monkeypatch, point):
    crash_at(monkeypatch, point)
    with pytest.raises(Crash):
        site.main("key", "issue", "clone", "--like", "src", "--days", "30")
    capsys.readouterr()
    [journal] = list((site.root / "ops").glob("*.json"))
    operation = journal.stem
    monkeypatch.setattr(admin, "checkpoint", lambda step, phase: None)
    code, result, err = issue(site, capsys, "--operation", operation)
    assert code == 0, err
    state = site.operator.state
    assert sorted(state["issued"]) == sorted(f"{operation}/{step}" for step in ("messaging", "messaging-archive", "calendar"))
    assert state["violations"] == []
    assert entry(site.policy(), "clone")["enabled"] is True and entry(site.registry_doc(), "clone")["enabled"] is True
    assert [row["id"] for row in site.policy()["principals"]] == ["src", "other", "clone"]
    assert site.journal(operation)["state"] == "done" and result["operation"] == operation
    # A rerun of a finished operation only prints its result again.
    assert issue(site, capsys, "--operation", operation)[1] == result


# spec: ANYM-496
def test_issue_failure_undo_retires_the_id_and_a_new_id_succeeds(site, capsys):
    # The running gateway refuses the policy candidate (the live policy passes the preflight).
    site.docker.images["gateway:new"] = lambda path: '"clone"' not in path.read_text()
    code = site.main("key", "issue", "clone", "--like", "src", "--days", "30")
    err = capsys.readouterr().err
    assert code == 1 and "deedkit admin undo" in err and "refused by image gateway:new" in err
    operation = operation_of(err)
    assert entry(site.policy(), "clone") is None and entry(site.registry_doc(), "clone")["enabled"] is True
    assert site.main("undo", operation) == 0
    undone = json.loads(capsys.readouterr().out)
    assert undone["undone"] is True and "another ID" in undone["note"]
    state = site.operator.state
    assert all(k["revoked"] for k in state["messaging"]["keys"].values() if k["principal"].startswith("clone"))
    assert state["calendar"]["agents"]["clone"]["revoked"] is True
    assert entry(site.registry_doc(), "clone")["enabled"] is False and entry(site.policy(), "clone") is None
    assert not list(site.keys.glob("clone-*")) and not list(site.voice.glob("clone-*"))
    assert not (site.root / "admin-tokens/clone.token").exists()
    site.docker.images["gateway:new"] = gateway_rule(strict=False)
    code, _, err = issue(site, capsys)
    assert code == 2 and "is retired (revoked records stay for audit): use another ID" in err
    code, result, err = issue(site, capsys, new="clone2")
    assert code == 0, err
    assert entry(site.policy(), "clone2")["enabled"] is True


# spec: ANYM-496
def test_revoking_the_clones_archive_credential_leaves_the_sources_archive(site, capsys):
    source_archive = (site.voice / "src-archive.json").read_bytes()
    code, result, err = issue(site, capsys)
    assert code == 0, err
    archive_key = next(c["public_id"] for c in result["credentials"] if c["store"] == "messaging-archive")
    site.operator("messaging", ["revoke-key", "--key-id", archive_key])
    token = (site.voice / "src-archive.token").read_text().strip()
    reply = site.operator("messaging", ["key-info", "--key-file", "/dev/stdin"], (token + "\n").encode())
    assert reply["keys"][0]["found"] and not reply["keys"][0]["revoked"]
    assert reply["keys"][0]["principal"] == "src-calls-archive"
    assert (site.voice / "src-archive.json").read_bytes() == source_archive
    assert entry(site.registry_doc(), "src")["archive_config"] == "/run/secrets/src-archive.json"


t424 = importlib.import_module("tests.test_gateway_policy_rename")
gateway_image = t424.gateway_image  # the ANYM-424 harness: a gateway image built from this checkout
REGISTRY_CHECK = ("import json, sys\ndoc = json.load(open(sys.argv[1]))\nrows = doc['principals']\n"
                  "fields = {'id', 'key_sha256', 'enabled', 'expires_at', 'scopes', 'channels', 'destinations', "
                  "'archive_config'}\nok = doc['version'] == 1 and all(set(r) <= fields for r in rows) "
                  "and len({r['id'] for r in rows}) == len(rows)\n"
                  "print(json.dumps({'valid': ok}))\nsys.exit(0 if ok else 1)\n")  # agent_api.py --check-registry's answer


# spec: ANYM-496
def test_a_clone_authenticates_on_the_next_request_of_the_same_gateway_container(gateway_image, monkeypatch, capsys):
    """The checked-in gateway service (directory mounts) and a calls consumer stand-in mounting the voice secrets
    directory run in Docker; `key issue --like` validates each candidate in their images and the rollback image with
    real `docker run`, renames it in place, and the clone's key answers on the next request, no container recreated."""
    python = os.environ["DEEDKIT_TEST_PYTHON_IMAGE"]
    with tempfile.TemporaryDirectory(prefix="deedkit-t496-") as root:
        gateway = t424.Gateway(Path(root), gateway_image)
        site = Site(gateway.deploy / "private", monkeypatch, admin, s, "gateway-policy", "gateway-keys")
        (site.root / "gateway.env").write_text("# synthetic\n")
        (site.root / "gateway.env").chmod(0o600)
        sha = json.loads((site.root / "state/a/current.json").read_text())["sha"]
        (site.root / "state/a" / sha / "deedkit.before.json").write_text(
            json.dumps({"services": {"gateway": {"image": gateway_image}}}))
        site.config["validators"] = {"registry": ["python", "-c", REGISTRY_CHECK, "{path}"]}
        (site.root / "admin/config.json").write_text(json.dumps(site.config))
        gateway.override()
        voice = f"deedkit-t496-voice-{uuid.uuid4().hex[:12]}"
        user = [] if os.geteuid() == 0 else ["--user", f"{os.geteuid()}:{os.getegid()}"]
        try:
            started = gateway.start()
            src = site.tokens["src-gateway"]
            for _ in range(60):  # up: the source's own key answers
                answer = gateway.docker("exec", started[0], "python", "-c", t424.REQUEST, src, check=False, timeout=30)
                if answer.returncode == 0:
                    break
                time.sleep(1)
            assert answer.returncode == 0, answer.stdout + gateway.compose("logs", "gateway", check=False).stdout[-3000:]
            stand_in = gateway.docker("run", "--detach", "--rm", "--name", voice, "--network", "none", *user,
                                      "--mount", f"type=bind,source={site.voice},target=/run/secrets,readonly",
                                      python, "sleep", "600").stdout.strip()
            revision = site.policy()["revision"]
            code = admin.main(["--config", str(site.root / "admin/config.json"), "key", "issue", "clone", "--like",
                               "src", "--days", "30"], run=site.operator, docker=admin.dockers(site.config),
                              host=site.host)
            out, err = capsys.readouterr()
            assert code == 0, err
            token = Path(json.loads(out)["token_file"]).read_text().strip()
            answer = gateway.docker("exec", started[0], "python", "-c", t424.REQUEST, token, check=False, timeout=30)
            assert answer.returncode == 0 and int(answer.stdout.strip()) == revision + 1, answer.stdout
            assert gateway.container() == started, "the gateway must not be recreated or restarted"
            seen = json.loads(gateway.docker("exec", voice, "cat", "/run/secrets/agents.json").stdout)
            assert [row["id"] for row in seen["principals"]][-1] == "clone"
            assert json.loads(gateway.docker("inspect", voice).stdout)[0]["Id"] == stand_in
            leftovers = gateway.docker("ps", "--all", "--quiet", "--filter", "name=deedkit-admin-").stdout.split()
            assert leftovers == [], "no one-shot check container may remain"
        finally:
            gateway.docker("stop", "--time", "1", voice, check=False)
            gateway.close()


# r1 regressions (Astra, PR #290)

def crashed(site, capsys, monkeypatch, point):
    crash_at(monkeypatch, point)
    with pytest.raises(Crash):
        site.main("key", "issue", "clone", "--like", "src", "--days", "30")
    capsys.readouterr()
    monkeypatch.setattr(admin, "checkpoint", lambda step, phase: None)
    return next((site.root / "ops").glob("*.json")).stem


# spec: ANYM-496
def test_a_rerun_never_issues_into_a_principal_created_after_the_crash(site, capsys, monkeypatch):
    operation = crashed(site, capsys, monkeypatch, ("key files", "archive-token"))
    store_holds("messaging", "clone")(site)  # another operator creates the ID before the rerun
    before = site.operator.state["messaging"]["principals"]["clone"]
    code, _, err = issue(site, capsys, "--operation", operation)
    assert code == 2 and "issue-key" in err and "nothing changed" in err, err  # its --check refuses first
    state = site.operator.state
    assert state["violations"] == [] and state["messaging"]["principals"]["clone"] == before
    assert entry(site.policy(), "clone") is None


# spec: ANYM-496
def test_a_crash_inside_a_token_write_is_finished_by_the_rerun(site, capsys, monkeypatch):
    operation = crashed(site, capsys, monkeypatch, ("clone-messaging.key", "staged"))
    assert not (site.keys / "clone-messaging.key").exists()
    leftover = site.keys / ".clone-messaging.key.admin-0123456789ab"  # a real crash leaves its staging file
    leftover.write_text("partial")
    code, result, err = issue(site, capsys, "--operation", operation)
    assert code == 0, err
    assert not leftover.exists() and (site.keys / "clone-messaging.key").read_text().strip()
    assert site.operator.state["violations"] == []


# spec: ANYM-496
def test_an_undo_before_any_backend_step_retires_the_id(site, capsys, monkeypatch):
    operation = crashed(site, capsys, monkeypatch, ("key files", "gateway"))
    assert site.main("undo", operation) == 0
    undone = json.loads(capsys.readouterr().out)
    assert undone["revoked"] == [] and undone["disabled"] == [] and len(undone["deleted_files"]) == 1
    code, _, err = issue(site, capsys)
    assert code == 2 and f"operation {operation}" in err and "is retired" in err
    assert issue(site, capsys, new="clone2")[0] == 0


# spec: ANYM-496
def test_undo_reports_what_it_revoked_disabled_and_deleted(site, capsys):
    code, result, err = issue(site, capsys)
    assert code == 0, err
    assert site.main("undo", result["operation"]) == 0
    undone = json.loads(capsys.readouterr().out)
    assert sorted(r["store"] for r in undone["revoked"]) == ["calendar", "messaging", "messaging-archive"]
    assert undone["disabled"] == ["gateway policy", "calls registry"]
    assert result["token_file"] in undone["deleted_files"] and undone["next"] == "on the Mac: python3 scripts/deedkit.py key uninstall clone"


# spec: ANYM-496
def test_a_rerun_uses_the_journaled_files_after_the_source_key_moves(site, capsys, monkeypatch):
    operation = crashed(site, capsys, monkeypatch, ("messaging", "committed"))
    token = (site.keys / "clone-messaging.key").read_text()
    (site.keys / "moved").mkdir(mode=0o700)
    (site.keys / "src-messaging.key").rename(site.keys / "moved/src-messaging.key")
    edit = site.policy()
    entry(edit, "src")["credentials"]["messaging"]["key_file"] = "/run/deedkit/keys/moved/src-messaging.key"
    edit["revision"] += 1
    Path(site.config["policy"]).write_text(json.dumps(edit))
    code, result, err = issue(site, capsys, "--operation", operation)
    assert code == 0, err
    assert (site.keys / "clone-messaging.key").read_text() == token and not (site.keys / "moved/clone-messaging.key").exists()
    assert entry(site.policy(), "clone")["credentials"]["messaging"]["key_file"] == "/run/deedkit/keys/clone-messaging.key"
    assert site.operator.state["violations"] == []


# spec: ANYM-496
@pytest.mark.parametrize("store", ["gateway policy", "calls registry", "calendar"])
def test_the_archive_id_taken_in_any_store_refuses(site, capsys, store):
    if store == "gateway policy":
        policy = site.policy()
        policy["principals"].append(dict(entry(policy, "other"), id="clone-calls-archive"))
        Path(site.config["policy"]).write_text(json.dumps(policy))
    elif store == "calls registry":
        registry = site.registry_doc()
        registry["principals"].append(dict(entry(registry, "other"), id="clone-calls-archive", key_sha256="3" * 64))
        Path(site.config["calls_registry"]).write_text(json.dumps(registry))
    else:
        store_holds("calendar", "clone-calls-archive")(site)
    before = site.snapshot()
    code, _, err = issue(site, capsys)
    assert code == 2 and f"exists in {store} (clone-calls-archive)" in err, err
    assert site.snapshot() == before and not (site.root / "ops").exists()


# spec: ANYM-496
def test_an_invalid_label_refuses_before_any_write(site, capsys):
    before = site.snapshot()
    code, _, err = issue(site, capsys, "--label", "x" * 81)
    assert code == 2 and "a label is 1-80 printable characters" in err
    assert site.snapshot() == before and site.operator.state["issued"] == [] and not (site.root / "ops").exists()


# spec: ANYM-496
@pytest.mark.parametrize("ceiling", [None, ["read", "send", "upload"], []], ids=["null", "wider", "empty"])
def test_a_clone_key_keeps_the_source_key_stored_ceiling(site, capsys, ceiling):
    # A NULL legacy ceiling, one wider than the principal's permissions and an empty one are repeated as stored, so
    # the clone may do exactly what the source may, now and after any later equal grant.
    site.operator.change(lambda state: state["messaging"]["keys"]["key-src"].update(permissions=ceiling))
    code, _, err = issue(site, capsys)
    assert code == 0, err
    messaging = site.operator.state["messaging"]
    assert [k["permissions"] for k in messaging["keys"].values() if k["principal"] == "clone"] == [ceiling]
    assert messaging["principals"]["clone"] == messaging["principals"]["src"]
