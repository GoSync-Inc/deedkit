"""`production.py enroll --plan` (ANYM-745): the module manifest, its refusal codes and the read-only plan."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from scripts import production as p

ROOT = Path(__file__).resolve().parents[1]
SHA, BASE = "a" * 40, "b" * 40
RECORDINGS = (ROOT / "deploy/modules/recordings.json").read_text()
OVERLAY = b"name: deedkit-recordings\nservices:\n  recordings-api: {}\n"
GATEWAY_OVERLAY = b"services:\n  gateway:\n    networks: [recordings-backend]\n"
SECRET = "synthetic-secret-value-0001"
TOKEN_FILE = "/run/deedkit/recordings/omi.key"
STATE_CONFIG = {"accounts": [{"id": "omi-a", "provider": "omi", "token_file": TOKEN_FILE}]}
POLICY = {
    "version": 1, "revision": 22,
    "deployment": {"modules": ["messaging", "calendar"],
                   "backends": {"messaging": {"url": "http://api:8080"}, "calendar": {"url": "http://cal:8081"}}},
    "profiles": {"full": {"tools": ["messaging_send"]}, "ro": {"tools": ["messaging_read"]}},
    "principals": [
        {"id": "owner-claude-code", "key_sha256": "1" * 64, "profile": "full", "enabled": True, "expires_at": "2027-01-01",
         "grants": {"messaging": ["messaging:read"]}, "credentials": {}},
        {"id": "claude-code", "key_sha256": "2" * 64, "profile": "ro", "enabled": True, "expires_at": "2027-01-01",
         "grants": {"messaging": ["messaging:read"]}, "credentials": {}},
        {"id": "other-agent", "key_sha256": "3" * 64, "profile": "ro", "enabled": True, "expires_at": "2027-01-01",
         "grants": {}, "credentials": {}},
    ],
}


def key_credential(principal):
    key = hashlib.sha256((SECRET + principal).encode()).hexdigest()
    return f"principals.{principal}.credentials.recordings = key_file /run/deedkit/keys/recordings-{principal}.key, key_sha256 {key}"


GOLDEN_ADDED = [
    "revision 22 -> 23", "deployment.modules + recordings", "deployment.backends.recordings = http://recordings-api:13234",
    "principals.owner-claude-code.grants.recordings = recordings:read, recordings:content",
    key_credential("owner-claude-code"), "profiles.full.tools + recordings_capabilities",
    "profiles.full.tools + recordings_list", "profiles.full.tools + recordings_read",
    "principals.claude-code.grants.recordings = recordings:read, recordings:content",
    key_credential("claude-code"), "profiles.ro.tools + recordings_capabilities",
    "profiles.ro.tools + recordings_list", "profiles.ro.tools + recordings_read",
]


def blob_id(content):
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def tree(root):
    return sorted((str(path.relative_to(root)), path.stat().st_mtime_ns, path.stat().st_size if path.is_file() else -1)
                  for path in Path(root).rglob("*"))


class Enroller:
    """A synthetic host for `enroll --plan`: a state directory at BASE, the gateway project and its policy, the env file,
    the key files and the private configuration as real temporary files, the repository at SHA as `repo` (path ->
    bytes), and git, docker, Compose and the verify script answered by `run`. Host paths of the repository manifest
    are mapped into the temporary directory."""

    def __init__(self, tmp_path, monkeypatch, *, mutate=None):
        self.root, self.monkeypatch, self.calls = tmp_path, monkeypatch, []
        self.deploy = tmp_path / "deploy"
        self.private = tmp_path / "private"
        self.keys = self.private / "gateway-keys"
        self.state_dir = tmp_path / "recordings-state"
        self.state = tmp_path / "state"
        for directory in (self.deploy, self.private, self.keys, self.state_dir, self.state):
            directory.mkdir(mode=0o700, parents=True)
        (self.deploy / "compose.gateway.yaml").write_text("services: {gateway: {}}\n")
        (self.deploy / "compose.gateway.yaml").chmod(0o640)
        (self.state_dir / "config.json").write_text(json.dumps(STATE_CONFIG))
        self.token = self.state_dir / "omi.key"
        self.token.write_text(SECRET + "-provider\n")
        self.token.chmod(0o600)
        self.env = self.private / "recordings.env"
        self.write_env()
        for principal in ("owner-claude-code", "claude-code"):
            key = self.keys / f"recordings-{principal}.key"
            key.write_text(SECRET + principal + "\n")
            key.chmod(0o600)
        self.policy = tmp_path / "policy.json"
        self.policy.write_text(json.dumps(POLICY))
        gateway = {"name": "deedkit-gateway", "directory": str(self.deploy),
                   "files": [str(self.deploy / "compose.gateway.yaml")], "env_files": [], "services": {"gateway": "gateway"}}
        self.settings = {"version": 1, "state_directory": str(self.state), "projects": [copy.deepcopy(gateway)]}
        self.http = {"version": 1, "state_directory": str(tmp_path / "http-state"), "projects": [copy.deepcopy(gateway)],
                     "http_release": {"gateway_policy": str(self.policy)}}
        p.write(self.state / "current.json", {"sha": BASE, "status": "applied"})
        manifest = RECORDINGS.replace("/opt/deedkit/deploy/private/recordings.env", str(self.env)).replace(
            "/opt/deedkit/deploy/private/gateway-keys", str(self.keys)).replace('"key_uid": 10001', f'"key_uid": {os.getuid()}')
        self.manifest = json.loads(manifest)
        if mutate:
            mutate(self.manifest)
        self.repo = {"deploy/modules/recordings.json": json.dumps(self.manifest).encode(),
                     "deploy/compose.recordings.yaml": OVERLAY, "deploy/compose.gateway-recordings.yaml": GATEWAY_OVERLAY}
        self.changed, self.images, self.gateway_valid, self.verify_output = [], ["deedkit-gateway:old", "deedkit-gateway:new"], True, "ok\n"
        self.compose_error, self.active, self.verified = None, [], True
        monkeypatch.setattr(p, "run", self.run)
        monkeypatch.setattr(p, "promotable", self.promotable)
        monkeypatch.setattr(p, "active_calls", lambda settings: self.active if not isinstance(self.active, Exception)
                            else (_ for _ in ()).throw(self.active))
        monkeypatch.setattr(p, "PAUSE", tmp_path / "autodeploy.pause")
        monkeypatch.setattr(p, "ENROLL_OWNER", os.getuid())

    def write_env(self, **changes):
        variables = {"RECORDINGS_STATE_DIR": str(self.state_dir), "RECORDINGS_RESOLVER": "10.99.0.2",
                     "RECORDINGS_EGRESS_BLOCKED": "192.0.2.254:9,10.99.0.1:22,100.64.0.5:22",
                     "RECORDINGS_EGRESS_OPEN": "api.omi.me:443,dns:10.99.0.2",
                     "INFERENCE_MINI_IP": "10.99.0.9", "OMI_TOKEN": SECRET, **changes}
        self.env.write_text("".join(f"{key}={value}\n" for key, value in variables.items() if value is not None))
        self.env.chmod(0o600)

    def promotable(self, sha):
        if not self.verified:
            raise p.Refused("main does not contain that commit")
        return {"sha": sha}

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == sys.executable:
            if isinstance(self.verify_output, Exception):
                raise self.verify_output
            # The verifier reads `--env-file`, never the environment: record what it would read.
            self.verified_with = (argv, kwargs.get("env"), Path(argv[argv.index("--env-file") + 1]).read_text()
                                  if "--env-file" in argv else None)
            return self.verify_output
        if argv[:2] == ["docker", "compose"]:
            return self.compose(argv)
        if argv[:2] == ["docker", "ps"]:
            return "".join(f"{image}\tgateway\n" for image in self.images) + "other:1\tmessaging\n"
        if argv[:2] == ["docker", "run"]:
            if isinstance(self.gateway_valid, Exception):
                raise self.gateway_valid
            return json.dumps({"valid": self.gateway_valid, "revision": 23}) + "\n"
        assert argv[0] == "git", argv
        command = argv[3:]
        if command[:2] == ["cat-file", "blob"]:
            path = command[2].split(":", 1)[1]
            if path not in self.repo:
                raise p.Refused("git failed (exit 128); production was not accepted")
            return self.repo[path] if kwargs.get("binary") else self.repo[path].decode()
        if command[:1] == ["rev-parse"]:
            path = command[1].split(":", 1)[1]
            if path not in self.repo:
                raise p.Refused("git failed (exit 128); production was not accepted")
            return blob_id(self.repo[path]) + "\n"
        if command[:1] == ["diff"]:
            return "\0".join(self.changed) + "\0"
        if command[:1] == ["archive"]:
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as tar:
                script = b"print('ok')\n"
                info = tarfile.TarInfo("scripts/recordings_egress_check.py")
                info.size = len(script)
                tar.addfile(info, io.BytesIO(script))
            return buffer.getvalue()
        raise AssertionError(argv)

    def compose(self, argv):
        if self.compose_error:
            raise self.compose_error
        env = {}
        if "--env-file" in argv:
            for line in Path(argv[argv.index("--env-file") + 1]).read_text().splitlines():
                key, _, value = line.partition("=")
                env[key] = value
        files = [Path(argv[index + 1]).name for index, item in enumerate(argv) if item == "-f"]
        name = argv[argv.index("--project-name") + 1] if "--project-name" in argv else "recordings"
        services = {"recordings-api": {"volumes": [{"type": "bind", "source": env.get("RECORDINGS_STATE_DIR", "/none"),
                                                     "target": "/run/deedkit/recordings"}]}}
        return json.dumps({"name": name, "files": files, "services": services if "RECORDINGS_STATE_DIR" in env else {}})

    def plan(self):
        return p.enroll_plan(self.settings, self.http, "recordings", SHA, self.root / "mirror")

    def codes(self):
        with pytest.raises(p.Refused) as refused:
            self.plan()
        exc = refused.value
        return [item["code"] for item in exc.failures] if isinstance(exc, p.EnrollRefusals) else [exc.code]


def set_path(*keys, value):
    def mutate(manifest):
        node = manifest
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return mutate


def delete_path(*keys):
    def mutate(manifest):
        node = manifest
        for key in keys[:-1]:
            node = node[key]
        del node[keys[-1]]
    return mutate


MANIFEST_REFUSALS = [
    ("manifest_unknown_key", set_path("extra", value=1)),
    ("manifest_unknown_key", set_path("project", "extra", value=1)),
    ("manifest_unknown_key", set_path("gateway", "policy", "extra", value=1)),
    ("manifest_invalid", delete_path("gateway")),
    ("manifest_invalid", set_path("name", value="other")),
    ("manifest_invalid", set_path("project", "services", value={})),
    ("manifest_invalid", set_path("private_config", "forbidden_keys", value=[])),
    ("manifest_relative_path", set_path("project", "env_file", value="private/recordings.env")),
    ("manifest_relative_path", set_path("gateway", "policy", "key_dir", value="private/keys")),
    ("manifest_relative_path", set_path("private_config", "mount", value="run/recordings")),
    ("manifest_repo_path", set_path("project", "files", value=["/etc/passwd"])),
    ("manifest_repo_path", set_path("project", "files", value=["services/recordings/Dockerfile"])),
    ("manifest_repo_path", set_path("project", "files", value=["deploy/../scripts/production.py"])),
    ("manifest_repo_path", set_path("gateway", "overlay", value="/etc/shadow")),
    ("manifest_forbidden_service", set_path("project", "services", value={"db": "recordings"})),
    ("manifest_forbidden_service", set_path("project", "services", value={"postgres": "recordings"})),
    ("manifest_forbidden_service", set_path("project", "services", value={"operator": "recordings"})),
    ("manifest_grant_wildcard", set_path("gateway", "policy", "principals", "claude-code", "grants", value=["*"])),
    ("manifest_grant_wildcard", set_path("gateway", "policy", "principals", "claude-code", "grants", value=["recordings:*"])),
    ("manifest_egress_incomplete", set_path("egress", value={"outside": True})),
    ("manifest_egress_incomplete", delete_path("egress", "admission_env")),
    ("manifest_verify_script", set_path("egress", "verify", value=["scripts/production.py", "verify"])),
    ("manifest_verify_script", set_path("egress", "verify", value=["../x_egress_check.py"])),
    ("manifest_verify_script", set_path("egress", "verify", value=["scripts/recordings_egress_check.sh"])),
    ("manifest_live_actions", set_path("live_actions", value=["send"])),
    ("manifest_invalid", set_path("project", "services", value={"recordings-api": []})),
    ("manifest_invalid", set_path("project", "services", value={"recordings-api": {}})),
    ("manifest_egress_incomplete", delete_path("egress", "blocked")),
    ("manifest_egress_incomplete", delete_path("egress", "controls")),
    ("manifest_invalid", set_path("egress", "blocked", value=["moon"])),
    ("manifest_invalid", set_path("egress", "blocked", value=[])),
    ("manifest_invalid", set_path("egress", "controls", value={"dns_env": "lower"})),
    ("manifest_invalid", delete_path("private_config", "credential_files")),
    ("manifest_invalid", set_path("private_config", "credential_files", value=[])),
    ("manifest_invalid", set_path("project", "networks", value={"name": "net"})),
    ("manifest_invalid", set_path("project", "networks", value="net")),
    ("manifest_unknown_key", set_path("project", "networks", value=[{"name": "n", "bridge": "b", "extra": 1}])),
    ("manifest_invalid", set_path("project", "networks", value=[{"name": "n", "bridge": "Bad Bridge"}])),
    ("manifest_invalid", set_path("project", "networks", value=[{"name": "n", "bridge": "b", "gateway_port": 0}])),
    ("manifest_invalid", set_path("project", "networks", value=[{"name": "n", "bridge": "b"}, {"name": "n", "bridge": "c"}])),
]


# spec: ANYM-745
@pytest.mark.parametrize("code, mutate", MANIFEST_REFUSALS)
def test_a_manifest_defect_is_refused_with_its_own_code(tmp_path, monkeypatch, code, mutate):
    assert Enroller(tmp_path, monkeypatch, mutate=mutate).codes() == [code]


# spec: ANYM-745
def test_a_manifest_that_is_not_in_the_commit_or_not_json_is_refused(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    del host.repo["deploy/modules/recordings.json"]
    assert host.codes() == ["manifest_missing"]
    host.repo["deploy/modules/recordings.json"] = b"{not json"
    assert host.codes() == ["manifest_invalid"]


# spec: ANYM-745
def test_a_policy_for_a_principal_the_live_policy_does_not_define_is_refused(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch, mutate=set_path("gateway", "policy", "principals", "ghost",
                                                           value={"grants": ["recordings:read"]}))
    assert host.codes() == ["manifest_unknown_principal"]


# spec: ANYM-745
def test_the_repository_manifest_of_recordings_is_valid_and_carries_no_live_action():
    manifest = p.check_manifest(json.loads(RECORDINGS), "recordings")
    assert manifest["live_actions"] == [] and manifest["egress"]["outside"] is True
    assert set(manifest["gateway"]["policy"]["principals"]) == {"owner-claude-code", "claude-code"}
    assert all("recordings:audio" not in section["grants"] for section in manifest["gateway"]["policy"]["principals"].values())


# spec: ANYM-745
def test_the_happy_plan_for_recordings_equals_the_golden_edits(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    plan = host.plan()
    assert plan["status"] == "plan" and plan["module"] == "recordings" and plan["sha"] == SHA and plan["live_actions"] == []
    assert plan["manifest_blob"] == blob_id(host.repo["deploy/modules/recordings.json"])
    owner = os.stat(host.deploy / "compose.gateway.yaml")
    assert plan["overlays"] == [
        {"repo_path": "deploy/compose.recordings.yaml", "host_path": f"{host.deploy}/compose.recordings.yaml",
         "sha256": hashlib.sha256(OVERLAY).hexdigest(), "blob": blob_id(OVERLAY), "owner": owner.st_uid,
         "group": owner.st_gid, "mode": "0o640"},
        {"repo_path": "deploy/compose.gateway-recordings.yaml", "host_path": f"{host.deploy}/compose.gateway-recordings.yaml",
         "sha256": hashlib.sha256(GATEWAY_OVERLAY).hexdigest(), "blob": blob_id(GATEWAY_OVERLAY), "owner": owner.st_uid,
         "group": owner.st_gid, "mode": "0o640"}]
    assert plan["production_json"] == {
        "append_project": {"name": "deedkit-recordings", "directory": str(host.deploy),
                           "files": [f"{host.deploy}/compose.recordings.yaml"], "env_files": [str(host.env)],
                           "services": {"recordings-api": "recordings"}},
        "gateway_project_files": [str(host.deploy / "compose.gateway.yaml"), f"{host.deploy}/compose.gateway-recordings.yaml"]}
    assert plan["http_production_json"] == {"gateway_project_files": plan["production_json"]["gateway_project_files"]}
    assert plan["policy"] == {"base_revision": 22, "revision": 23, "added": GOLDEN_ADDED,
                              "grants": {"owner-claude-code": ["recordings:content", "recordings:read"],
                                         "claude-code": ["recordings:content", "recordings:read"]},
                              "candidate_digest": plan["inputs"]["candidate_policy"]}
    assert sorted(plan["inputs"]["compose"]) == ["gateway", "gateway_http", "project"]
    assert len(plan["owner_steps"]) == 4 and len(plan["plan_digest"]) == 64


# spec: ANYM-745
def test_the_plan_writes_nothing_and_prints_no_secret(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    before = tree(tmp_path)
    plan = host.plan()
    assert tree(tmp_path) == before and not (host.state / SHA).exists()
    printed = json.dumps(plan)
    assert SECRET not in printed and "OMI_TOKEN" not in printed and "10.99.0.9" not in printed
    assert all(SECRET not in json.dumps(call) for call in host.calls)


# spec: ANYM-745
def test_the_candidate_policy_is_checked_in_every_serving_gateway_image(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    host.plan()
    checked = [call[call.index("-v") + 2] for call in host.calls if call[:2] == ["docker", "run"]]
    assert sorted(checked) == ["deedkit-gateway:new", "deedkit-gateway:old"]


# spec: ANYM-745
def test_the_live_policy_is_never_changed_by_the_plan(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    before = host.policy.read_bytes()
    host.plan()
    assert host.policy.read_bytes() == before


def refuse(setup, code):
    def check(tmp_path, monkeypatch):
        host = Enroller(tmp_path, monkeypatch)
        setup(host)
        assert code in host.codes()
    return check


def prepared(host):
    (host.state / SHA).mkdir()


def pause(host):
    p.PAUSE.write_text("paused\n")


def broken_env(host):
    host.env.chmod(0o640)


def missing_env(host):
    host.env.unlink()


def unset_variable(host):
    host.write_env(RECORDINGS_RESOLVER=None)


def missing_canary(host):
    host.write_env(RECORDINGS_EGRESS_BLOCKED="10.99.0.1:22,100.64.0.5:22")


def missing_open(host):
    host.write_env(RECORDINGS_EGRESS_OPEN="dns:10.99.0.2")


def failing_compose(host):
    host.compose_error = p.Refused("docker failed (exit 1)")


def active_call(host):
    host.active = ["call-1"]


def unknown_calls(host):
    host.active = p.CallsUnknown("whether a call is active could not be read")


def failing_verify(host):
    host.verify_output = "dk-recordings0 raw_missing\n"


def failing_gateway(host):
    host.gateway_valid = False


def missing_key(host):
    (host.keys / "recordings-claude-code.key").unlink()


def loose_key(host):
    (host.keys / "recordings-claude-code.key").chmod(0o640)


def empty_key(host):
    (host.keys / "recordings-claude-code.key").write_text("")
    (host.keys / "recordings-claude-code.key").chmod(0o600)


def unreadable_policy(host):
    host.policy.write_text("{broken")


def enrolled(host):
    host.settings["projects"].append({"name": "deedkit-recordings", "directory": str(host.deploy), "files": [],
                                      "services": {"recordings-api": "recordings"}})


def policy_has_module(host):
    value = json.loads(host.policy.read_text())
    value["deployment"]["modules"].append("recordings")
    host.policy.write_text(json.dumps(value))


def sql_change(host):
    host.changed = ["services/messaging/messaging_service/migrations/070_x.sql"]


def backup_change(host):
    host.changed = ["services/messaging/messaging_service/backup_runner.py"]


def backup_unit_change(host):
    host.changed = ["deploy/deedkit-calendar-backup.service"]


def tool_change(host):
    host.changed = ["scripts/seamless_release.py"]
    host.repo["scripts/seamless_release.py"] = b"# the candidate's tool\n"
    host.monkeypatch.setattr(p, "blob", lambda path: "0" * 40)
    host.monkeypatch.setattr(p, "RUNNING", host.root / "production.py")
    host.monkeypatch.setattr(p, "SYSTEMD_DIRECTORY", host.root)


def unverified(host):
    host.verified = False


def no_current(host):
    (host.state / "current.json").unlink()


PLAN_REFUSALS = {
    "sha_already_prepared": prepared, "paused": pause, "env_file_mode": broken_env, "env_file_missing": missing_env,
    "env_variable_unset": unset_variable, "egress_env_missing": missing_canary, "compose_config_failed": failing_compose,
    "call_active": active_call, "calls_unknown": unknown_calls, "egress_verify_failed": failing_verify,
    "policy_check_failed": failing_gateway, "key_file_invalid": missing_key, "policy_unreadable": unreadable_policy,
    "module_enrolled": enrolled, "policy_module_present": policy_has_module, "range_sql_inventory": sql_change,
    "range_backup_changed": backup_change, "no_current_release": no_current,
}


# spec: ANYM-745
@pytest.mark.parametrize("code", sorted(PLAN_REFUSALS))
def test_each_failed_precondition_refuses_with_its_own_code(tmp_path, monkeypatch, code):
    refuse(PLAN_REFUSALS[code], code)(tmp_path, monkeypatch)


# spec: ANYM-745
@pytest.mark.parametrize("setup", [loose_key, empty_key])
def test_a_key_file_that_is_loose_or_empty_is_refused(tmp_path, monkeypatch, setup):
    refuse(setup, "key_file_invalid")(tmp_path, monkeypatch)


# spec: ANYM-745
def test_a_missing_open_target_is_an_egress_env_refusal(tmp_path, monkeypatch):
    refuse(missing_open, "egress_env_missing")(tmp_path, monkeypatch)


# spec: ANYM-745
def test_a_backup_unit_change_in_the_range_refuses(tmp_path, monkeypatch):
    refuse(backup_unit_change, "range_backup_changed")(tmp_path, monkeypatch)


# spec: ANYM-745
def test_a_deploy_tool_not_yet_installed_in_the_range_refuses(tmp_path, monkeypatch):
    refuse(tool_change, "range_tool_pending")(tmp_path, monkeypatch)


# spec: ANYM-745
def test_a_field_of_an_unexpected_type_is_a_refusal_never_an_exception(monkeypatch):
    def boom(value, name):
        raise TypeError("unhashable type: 'list'")
    monkeypatch.setattr(p, "checked_manifest", boom)
    with pytest.raises(p.EnrollRefused) as refused:
        p.check_manifest({}, "recordings")
    assert refused.value.code == "manifest_invalid"


# spec: ANYM-745
def test_the_egress_verifier_reads_a_private_env_file_holding_the_checked_values(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    host.plan()
    argv, env, content = host.verified_with
    assert argv[-2] == "--env-file" and Path(argv[-1]).name == "verify.env"
    assert env == {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}  # nothing is passed through the environment
    assert content == "RECORDINGS_RESOLVER=10.99.0.2\nINFERENCE_MINI_IP=10.99.0.9\n"


# spec: ANYM-745
def test_replacing_a_gateway_key_changes_the_candidate_and_the_plan_digest(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    before = host.plan()
    key = host.keys / "recordings-claude-code.key"
    key.write_text("a rotated key\n")
    key.chmod(0o600)
    after = host.plan()
    assert after["policy"]["candidate_digest"] != before["policy"]["candidate_digest"]
    assert after["plan_digest"] != before["plan_digest"]
    assert any("key_sha256 " + hashlib.sha256(b"a rotated key").hexdigest() in line for line in after["policy"]["added"])


# spec: ANYM-745
def test_the_http_gateway_is_resolved_in_its_own_directory(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    other = tmp_path / "http-deploy"
    other.mkdir()
    host.http["projects"][0]["directory"] = str(other)
    host.plan()
    directories = [call[call.index("--project-directory") + 1] for call in host.calls if call[:2] == ["docker", "compose"]]
    assert str(other) in directories and str(host.deploy) in directories


# spec: ANYM-745
def test_an_installed_seamless_release_tool_is_not_pending_and_a_stale_one_is(tmp_path, monkeypatch):
    from scripts import seamless_release as s
    host = Enroller(tmp_path, monkeypatch)
    host.changed = ["scripts/seamless_release.py"]
    host.repo["scripts/seamless_release.py"] = Path(s.__file__).read_bytes()
    assert host.plan()["status"] == "plan"
    host.repo["scripts/seamless_release.py"] = b"# an older tool\n"
    assert "range_tool_pending" in host.codes()


EGRESS_ENV_REFUSALS = [
    ("egress_blocked_incomplete", {"RECORDINGS_EGRESS_BLOCKED": "192.0.2.254:9,10.99.0.1:22"}),
    ("egress_blocked_incomplete", {"RECORDINGS_EGRESS_BLOCKED": "192.0.2.254:9,100.64.0.5:22"}),
    ("egress_blocked_incomplete", {"RECORDINGS_EGRESS_BLOCKED": "192.0.2.254:9,dns:10.99.0.2"}),
    ("egress_target_invalid", {"RECORDINGS_EGRESS_BLOCKED": "192.0.2.254:9,10.99.0.1:22,100.64.0.5:22,tailnet-node:22"}),
    ("egress_target_invalid", {"RECORDINGS_EGRESS_BLOCKED": "192.0.2.254:9,10.99.0.1:22,100.64.0.5:0"}),
    ("egress_target_invalid", {"RECORDINGS_EGRESS_BLOCKED": "192.0.2.254:9,10.99.0.1:22,100.64.0.5:22,dns:not-an-ip"}),
    ("egress_target_invalid", {"RECORDINGS_EGRESS_BLOCKED": "192.0.2.254:9,10.99.0.1:22,100.64.0.5:22,dns:"}),
    ("egress_target_invalid", {"RECORDINGS_EGRESS_BLOCKED": "192.0.2.254:9,10.99.0.1:22,100.64.0.5:22,dns:[2001:db8::53"}),
    ("egress_target_invalid", {"RECORDINGS_EGRESS_BLOCKED": "192.0.2.254:9,10.99.0.1:22,100.64.0.5:22,dns:[1.1.1.1]:x"}),
    ("egress_target_invalid", {"RECORDINGS_EGRESS_BLOCKED": "192.0.2.254:9,10.99.0.1:22,100.64.0.5:22,dns:1.1.1.1:99999"}),
    ("egress_control_missing", {"RECORDINGS_EGRESS_OPEN": "api.omi.me:443"}),
    ("egress_control_missing", {"RECORDINGS_EGRESS_OPEN": "api.omi.me:443,dns:10.99.0.77"}),
]


# spec: ANYM-745
@pytest.mark.parametrize("code, changes", EGRESS_ENV_REFUSALS)
def test_blocked_targets_and_positive_controls_the_manifest_declares_are_required(tmp_path, monkeypatch, code, changes):
    host = Enroller(tmp_path, monkeypatch)
    host.write_env(**changes)
    assert host.codes() == [code]


def token_missing(host):
    host.token.unlink()


def token_loose(host):
    host.token.chmod(0o640)


def token_empty(host):
    host.token.write_text("")


def token_outside_the_mount(host):
    (host.state_dir / "config.json").write_text(json.dumps({"accounts": [{"id": "a", "token_file": "/etc/passwd"}]}))


def token_missing_on_a_second_account(host):
    (host.state_dir / "config.json").write_text(json.dumps({"accounts": [
        {"id": "omi-a", "token_file": TOKEN_FILE}, {"id": "plaud-a", "provider": "plaud"}]}))


def token_not_named(host):
    (host.state_dir / "config.json").write_text(json.dumps({"accounts": []}))


# spec: ANYM-745
@pytest.mark.parametrize("setup", [token_missing, token_loose, token_empty, token_outside_the_mount, token_not_named,
                                   token_missing_on_a_second_account])
def test_a_provider_credential_file_that_is_missing_loose_empty_or_unnamed_is_refused(tmp_path, monkeypatch, setup):
    host = Enroller(tmp_path, monkeypatch)
    setup(host)
    assert host.codes() == ["credential_file_invalid"]
    with pytest.raises(p.Refused) as refused:
        host.plan()
    assert SECRET not in json.dumps(refused.value.failures)


# spec: ANYM-745
def test_blocked_dns_targets_and_a_second_valid_account_pass(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    host.write_env(RECORDINGS_EGRESS_BLOCKED="192.0.2.254:9,10.99.0.1:22,100.64.0.5:22,dns:1.1.1.1,dns:1.1.1.1:53,"
                                              "dns:2001:db8::53,dns:[2001:db8::53],dns:[2001:db8::53]:53,[2001:db8::1]:443")
    other = host.state_dir / "plaud.key"
    other.write_text(SECRET + "-plaud\n")
    other.chmod(0o600)
    (host.state_dir / "config.json").write_text(json.dumps({"accounts": [
        {"id": "omi-a", "token_file": TOKEN_FILE}, {"id": "plaud-a", "token_file": "/run/deedkit/recordings/plaud.key"}]}))
    assert host.plan()["status"] == "plan"


# spec: ANYM-745
def test_a_provider_credential_file_is_checked_by_metadata_and_never_read(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    opened = []
    real = os.read
    monkeypatch.setattr(p.os, "read", lambda fd, size: opened.append(os.fstat(fd).st_ino) or real(fd, size))
    host.plan()
    assert host.token.stat().st_ino not in opened


EXPIRED = subprocess.TimeoutExpired(["docker"], 120)


# spec: ANYM-745
@pytest.mark.parametrize("code, attribute", [("compose_config_failed", "compose_error"), ("egress_verify_failed", "verify_output"),
                                             ("policy_check_failed", "gateway_valid")])
def test_a_timeout_of_a_probe_is_a_refusal_with_its_own_code(tmp_path, monkeypatch, code, attribute):
    host = Enroller(tmp_path, monkeypatch)
    setattr(host, attribute, EXPIRED)
    assert code in host.codes()


# spec: ANYM-745
def test_a_timeout_of_the_main_verification_is_a_refusal(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    monkeypatch.setattr(p, "promotable", lambda sha: (_ for _ in ()).throw(EXPIRED))
    assert host.codes() == ["sha_not_verified"]


# spec: ANYM-745
def test_a_timeout_outside_a_probe_exits_the_command_with_a_refusal(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    monkeypatch.setattr(p, "config", lambda path: host.settings if path == p.CONFIG else host.http)
    monkeypatch.setattr(p.http_release_tool(), "validate_settings", lambda value: value)
    monkeypatch.setattr(p, "main_history", lambda state: host.root / "mirror")
    monkeypatch.setattr(p, "enroll_plan", lambda *args: (_ for _ in ()).throw(subprocess.TimeoutExpired(["git", "diff"], 5)))
    args = p.argument_parser().parse_args(["enroll", "--module", "recordings", "--sha", SHA, "--plan"])
    assert p.enroll_command(args) == {"status": "refused", "refusals": [{"code": "command_timeout", "detail": "git timed out"}]}


def mirror_listing(monkeypatch, changed, manifests):
    read = []

    def fake(argv, **kwargs):
        command = argv[3:]
        if command[0] == "diff":
            return "\0".join(changed) + "\0"
        if command[0] == "ls-tree":
            return "\0".join(manifests) + "\0"
        read.append(command[2].split(":", 1)[1])
        return manifests[command[2].split(":", 1)[1]]
    monkeypatch.setattr(p, "run", fake)
    return read


# spec: ANYM-745
def test_enrollment_pending_reads_only_the_manifests_the_range_changes(monkeypatch):
    broken = '{"name": "broken", "project": {"services": {"x": []}}}'
    read = mirror_listing(monkeypatch, ["README.md"], {"deploy/modules/broken.json": broken})
    assert p.enrollment_pending(Path("/mirror"), "a" * 40, "b" * 40, {"projects": []}) == [] and read == []
    read = mirror_listing(monkeypatch, ["deploy/modules/recordings.json"],
                          {"deploy/modules/broken.json": broken, "deploy/modules/recordings.json": RECORDINGS})
    assert p.enrollment_pending(Path("/mirror"), "a" * 40, "b" * 40, {"projects": []}) == ["recordings"]
    assert read == ["deploy/modules/recordings.json"]


# spec: ANYM-745
def test_a_changed_malformed_manifest_is_pending_and_never_an_exception(monkeypatch):
    broken = '{"name": "broken", "project": {"services": {"x": []}}}'
    mirror_listing(monkeypatch, ["deploy/modules/broken.json"], {"deploy/modules/broken.json": broken})
    assert p.enrollment_pending(Path("/mirror"), "a" * 40, "b" * 40, {"projects": []}) == ["broken"]


MANIFEST_PATH = "deploy/modules/recordings.json"
BROKEN_MANIFEST = '{"name": "recordings", "project": {"services": {"x": []}}}'
RUNS_RECORDINGS = {"projects": [{"name": "deedkit-recordings"}]}


def revisions(monkeypatch, changed, trees):
    """Git answered from `trees` ({sha: {path: text}}): unlike `mirror_listing`, a manifest differs per revision."""
    def fake(argv, **kwargs):
        command = argv[3:]
        if command[0] == "diff":
            return "\0".join(changed) + "\0"
        if command[0] == "ls-tree":
            return "\0".join(trees[command[-2]]) + "\0"
        sha, path = command[2].split(":", 1)
        return trees[sha][path]
    monkeypatch.setattr(p, "run", fake)


# spec: ANYM-765
@pytest.mark.parametrize("head, code", [(BROKEN_MANIFEST, "manifest_invalid"), ("{not json", "invalid"),
                                        (RECORDINGS.replace('"project"', '"unknown": 1, "project"', 1), "manifest_unknown_key")],
                         ids=["fields", "not_json", "unknown_key"])
def test_a_running_module_with_a_broken_changed_manifest_is_manifest_invalid_with_a_fixed_code(monkeypatch, head, code):
    revisions(monkeypatch, [MANIFEST_PATH], {BASE: {MANIFEST_PATH: RECORDINGS}, SHA: {MANIFEST_PATH: head}})
    pending = p.enrollment_pending(Path("/mirror"), BASE, SHA, RUNS_RECORDINGS)
    assert pending == ["recordings"]
    reason, fields = p.enrollment_halt(pending[0], SHA)
    assert reason == "manifest_invalid: recordings: fix deploy/modules/recordings.json; enroll is not needed"
    assert fields == {"manifest_code": code}


# spec: ANYM-765
def test_the_reason_a_manifest_failed_is_a_code_and_never_the_text_of_the_file(monkeypatch):
    text = "synthetic-parse-text-0001"
    revisions(monkeypatch, [], {SHA: {MANIFEST_PATH: json.dumps({"name": "recordings", text: text})}})
    assert p.module_manifests(Path("/mirror"), SHA) == ({"recordings": None}, {"recordings": "manifest_unknown_key"})
    revisions(monkeypatch, [], {SHA: {MANIFEST_PATH: "{" + text}})
    assert p.module_manifests(Path("/mirror"), SHA) == ({"recordings": None}, {"recordings": "invalid"})


# spec: ANYM-765
def test_a_new_module_with_a_broken_manifest_still_needs_the_enroll_command_and_the_plan_then_refuses_it(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    host.repo[MANIFEST_PATH] = b"{not json"
    assert host.codes() == ["manifest_invalid"]
    for base_tree, settings in (({}, {"projects": []}), ({}, RUNS_RECORDINGS), ({MANIFEST_PATH: RECORDINGS}, {"projects": []})):
        revisions(monkeypatch, [MANIFEST_PATH], {BASE: base_tree, SHA: {MANIFEST_PATH: BROKEN_MANIFEST}})
        pending = p.enrollment_pending(Path("/mirror"), BASE, SHA, settings)
        assert pending == ["recordings"]
        assert p.enrollment_halt(pending[0], SHA) == (p.enroll_required("recordings", SHA), {})


# spec: ANYM-765
@pytest.mark.parametrize("base", [BROKEN_MANIFEST, "{not json"], ids=["fields", "not_json"])
def test_a_module_whose_base_manifest_is_also_broken_is_pending_and_never_an_exception(monkeypatch, base):
    revisions(monkeypatch, [MANIFEST_PATH], {BASE: {MANIFEST_PATH: base}, SHA: {MANIFEST_PATH: BROKEN_MANIFEST}})
    pending = p.enrollment_pending(Path("/mirror"), BASE, SHA, RUNS_RECORDINGS)
    assert pending == ["recordings"]
    assert p.enrollment_halt(pending[0], SHA) == (p.enroll_required("recordings", SHA), {})


# spec: ANYM-765
def test_a_broken_manifest_of_a_running_module_never_lets_an_enrollment_alone_explain_the_difference(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    prepared, http = p.settings_digest(host.settings), {"config_digest": p.digest(host.http)}
    other_path, other = "deploy/modules/other.json", RECORDINGS.replace('"name": "recordings"', '"name": "other"', 1).replace(
        '"name": "deedkit-recordings"', '"name": "deedkit-other"', 1)
    for name, service in (("deedkit-recordings", "recordings-api"), ("deedkit-other", "other-api")):
        host.settings["projects"].append({"name": name, "directory": str(host.deploy), "files": [], "services": {service: "recordings"}})
    (host.state / BASE).mkdir()
    p.write(host.state / BASE / "prepared.json", {"config_digest": prepared})

    def explained(recordings):
        revisions(monkeypatch, [], {SHA: {MANIFEST_PATH: recordings, other_path: other}})
        return p.enrollment_only(Path("/mirror"), SHA, host.settings, host.http, {"sha": BASE}, http)
    assert explained(RECORDINGS) == ["other", "recordings"]  # the control: reversing both reproduces the recorded digests
    assert explained(BROKEN_MANIFEST) == []  # the valid manifest does not explain the module the broken one hides
    host.settings["projects"].pop()
    revisions(monkeypatch, [], {SHA: {MANIFEST_PATH: BROKEN_MANIFEST}})
    assert p.enrollment_only(Path("/mirror"), SHA, host.settings, host.http, {"sha": BASE}, http) == []


# spec: ANYM-745
def test_an_unverified_sha_refuses_before_anything_is_read(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    host.verified = False
    assert host.codes() == ["sha_not_verified"] and host.calls == []


# spec: ANYM-745
def test_every_failed_precondition_is_listed_once(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    pause(host)
    active_call(host)
    prepared(host)
    assert sorted(host.codes()) == ["call_active", "paused", "sha_already_prepared"]


# spec: ANYM-745
@pytest.mark.parametrize("content", [{"archive": {"path": "/data/archive"}}, {"archive": None},
                                     {"accounts": [{"id": "omi-a", "archive_namespace": "ns"}]},
                                     {"accounts": [{"id": "omi-a"}, {"id": "omi-b", "archive_namespace": "ns"}]}])
def test_a_private_configuration_that_turns_the_archive_on_is_refused(tmp_path, monkeypatch, content):
    host = Enroller(tmp_path, monkeypatch)
    (host.state_dir / "config.json").write_text(json.dumps({**STATE_CONFIG, **content}))
    assert host.codes() == ["private_config_forbidden_key"]
    with pytest.raises(p.Refused) as refused:
        host.plan()
    assert refused.value.failures[0]["detail"] in ("archive", "accounts[*].archive_namespace")
    assert "ns" not in json.dumps(refused.value.failures) and "/data/archive" not in json.dumps(refused.value.failures)


# spec: ANYM-745
def test_the_on_demand_private_configuration_passes_and_a_missing_one_is_refused(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    assert host.plan()["status"] == "plan"
    (host.state_dir / "config.json").unlink()
    assert host.codes() == ["private_config_unreadable"]


def another_config_byte(host):
    host.settings["note"] = "x"


def another_grant(host):
    host.manifest["gateway"]["policy"]["principals"]["claude-code"]["grants"].append("recordings:extra")
    host.repo["deploy/modules/recordings.json"] = json.dumps(host.manifest).encode()


def another_live_policy(host):
    value = json.loads(host.policy.read_text())
    value["principals"][2]["expires_at"] = "2027-02-02"
    host.policy.write_text(json.dumps(value))


def another_overlay(host):
    host.repo["deploy/compose.recordings.yaml"] += b"# one more line\n"


def another_env_byte(host):
    host.write_env(OMI_TOKEN=SECRET + "x")


def another_private_byte(host):
    (host.state_dir / "config.json").write_text(json.dumps({**STATE_CONFIG, "note": "x"}))


def another_state_dir(host):
    other = host.root / "other-state"
    other.mkdir(mode=0o700)
    (other / "config.json").write_text(json.dumps(STATE_CONFIG))  # the same bytes, another file
    (other / "omi.key").write_text(host.token.read_text())
    (other / "omi.key").chmod(0o600)
    host.write_env(RECORDINGS_STATE_DIR=str(other))


def another_http_config(host):
    host.http["note"] = "x"


# spec: ANYM-745
def test_the_plan_digest_is_the_same_for_the_same_inputs(tmp_path, monkeypatch):
    host = Enroller(tmp_path, monkeypatch)
    assert host.plan()["plan_digest"] == host.plan()["plan_digest"]


# spec: ANYM-745
@pytest.mark.parametrize("change", [another_config_byte, another_http_config, another_grant, another_live_policy,
                                    another_overlay, another_env_byte, another_private_byte, another_state_dir])
def test_each_reviewed_input_changes_the_plan_digest(tmp_path, monkeypatch, change):
    host = Enroller(tmp_path, monkeypatch)
    before = host.plan()["plan_digest"]
    change(host)
    assert host.plan()["plan_digest"] != before


IMAGE_ID = "sha256:" + "c" * 64
GATEWAY_IP = "172.30.0.5"
OVERLAY_EVENTS = ["overlay", "overlay", "network", "network", "tag", "up", "edit:production", "edit:http", "edit:policy",
                  "prepare", "apply", "seamless prepare", "seamless promote", "status", "drift"]


class Applier(Enroller):
    """`enroll --apply` on a synthetic host: real configuration files, a recorder for every mutation and release step
    (`events`, each with whether the pause existed), and hooks to inject a failure (`failures`) or a call (`triggers`)
    right after a named event."""

    def __init__(self, tmp_path, monkeypatch, **kwargs):
        super().__init__(tmp_path, monkeypatch, **kwargs)  # the manifest as committed, networks included
        from scripts import seamless_release as s
        self.s, self.events, self.triggers, self.failures, self.inputs = s, [], {}, {}, []
        self.http_state, self.tagged, self.networks = tmp_path / "http-state", set(), set()
        for directory in (self.http_state, tmp_path / "global"):
            directory.mkdir(mode=0o700)
        self.identities, self.before_identities = {"deedkit-gateway": "old"}, {"deedkit-gateway": "old"}
        self.restore_leaves, self.inspect_fails = False, False
        self.runtime_networks = {"deedkit-gateway": ["integration"]}  # what the running gateway is attached to
        self.admission, self.isolated, self.confined, self.controlled, self.healthy = "required", True, True, True, True
        self.up = None
        self.repo["scripts/recordings_egress_check.py"] = b"print('ok')\n"
        self.config_path, self.http_path = tmp_path / "production.json", tmp_path / "http-production.json"
        self.sync()
        monkeypatch.setattr(p, "http_release_tool", lambda: s)
        monkeypatch.setattr(p, "main_history", lambda state: self.root / "mirror")
        monkeypatch.setattr(p, "published", lambda target, sha: {
            "reference": f"ghcr.io/gosync-inc/deedkit-{target}@{IMAGE_ID}", "digest": IMAGE_ID, "config_digest": IMAGE_ID})
        monkeypatch.setattr(p, "names_revision", lambda image, sha, dockerfile, recipe=None: True)
        monkeypatch.setattr(p, "run_input", self.run_input)
        monkeypatch.setattr(p, "backup_gate", self.gate)
        monkeypatch.setattr(p, "ENROLL_HEALTH_SECONDS", 0)
        monkeypatch.setattr(p, "ENROLL_HEALTH_POLL", 0)
        monkeypatch.setattr(p, "prepare", self.prepare)
        monkeypatch.setattr(p, "apply", self.release_apply)
        monkeypatch.setattr(p, "rollback", self.release_rollback)
        monkeypatch.setattr(p, "snapshot", self.snapshot)
        monkeypatch.setattr(p, "wait_for_calls", lambda settings, deadline: self.event("calls-wait"))
        real_replace, real_drift = p.replace_file, p.configuration_drift
        def replace(path, content, uid, gid, mode):
            real_replace(path, content, uid, gid, mode)
            self.event({"production.json": "edit:production", "http-production.json": "edit:http",
                        "policy.json": "edit:policy"}.get(Path(path).name, "overlay"))
        def drift(*args):
            self.event("drift")
            return real_drift(*args)
        monkeypatch.setattr(p, "replace_file", replace)
        monkeypatch.setattr(p, "configuration_drift", drift)
        monkeypatch.setattr(s, "validate_settings", lambda value: value)
        monkeypatch.setattr(s, "GLOBAL_LOCK", tmp_path / "global")
        monkeypatch.setattr(s, "locks", lambda settings: contextlib.nullcontext())
        monkeypatch.setattr(s, "prepare", self.http_prepare)
        monkeypatch.setattr(s, "promote", self.http_promote)
        monkeypatch.setattr(s, "status", self.http_status)
        self.baseline()

    def sync(self):
        for path, value in ((self.config_path, self.settings), (self.http_path, self.http)):
            path.write_text(json.dumps(value))
            path.chmod(0o600)

    def baseline(self):
        """BASE as autodeploy leaves it: applied, the HTTP generation serving, nothing drifting."""
        self.record_current(BASE, self.settings)
        p.write(self.http_state / "http-current.json", {"sha": BASE, "status": "serving", "config_digest": p.digest(self.http)})

    def record_current(self, sha, settings):
        (self.state / sha).mkdir(exist_ok=True)
        p.write(self.state / sha / "prepared.json", {"sha": sha, "config_digest": p.settings_digest(settings)})
        projects = [{"name": project["name"], "after_digest": p.digest(json.loads(self.run(
            p.compose(project) + ["config", "--format", "json"])))} for project in settings["projects"]]
        p.write(self.state / "current.json", {"sha": sha, "status": "applied", "rollback": {"version": 1, "projects": projects}})

    def event(self, name):
        self.events.append((name, p.PAUSE.exists()))
        if name in self.triggers:
            self.triggers.pop(name)()
        if name in self.failures:
            raise self.failures.pop(name)

    @contextlib.contextmanager
    def gate(self, settings, bound):
        self.event("gate")
        yield 0

    def run_input(self, argv, data, **kwargs):
        self.inputs.append((argv, data))
        if argv[0] == sys.executable:
            passed = self.controlled
        elif any(item.endswith(":13240") for item in argv):
            passed = self.isolated
        else:
            passed = self.confined
        return json.dumps({"passed": passed})

    def run(self, argv, **kwargs):
        if argv[0] == "docker":
            sub = argv[1]
            if sub == "compose" and any(str(item).endswith("gateway.pre-run.json") for item in argv):
                self.calls.append(argv)
                self.event("restore")
                if "up" in argv and not self.restore_leaves:
                    self.identities = dict(self.before_identities)
                    file = next(item for item in argv if str(item).endswith("gateway.pre-run.json"))
                    self.runtime_networks = {"deedkit-gateway": json.loads(Path(file).read_text())["services"]["gateway"]["networks"]}
                return ""
            if sub == "compose" and "up" in argv:
                self.calls.append(argv)
                self.up = (argv, kwargs.get("env"))
                self.event("up")
                return ""
            if sub == "compose" and "stop" in argv:
                self.calls.append(argv)
                self.event("stop")
                return ""
            if sub == "ps" and "--filter" in argv:
                self.calls.append(argv)
                if any(item.startswith("network=") for item in argv):
                    return "gw1\tgateway\nrouter1\trouter\n"
                return "Up 3 seconds (healthy)\n" if self.healthy else "Up 3 seconds (unhealthy)\n"
            handled = {"pull": "", "inspect": GATEWAY_IP + "\n", "exec": ""}
            if sub in handled:
                self.calls.append(argv)
                if sub == "inspect" and self.inspect_fails:
                    raise p.Refused("docker inspect failed")
                return handled[sub]
            if sub == "tag":
                self.calls.append(argv)
                self.tagged.add(argv[3])
                self.event("tag")
                return ""
            if sub == "image" and argv[2] == "inspect":
                self.calls.append(argv)
                return IMAGE_ID + "\n"
            if sub == "image" and argv[2] == "ls":
                self.calls.append(argv)
                return IMAGE_ID + "\n" if argv[-1] in self.tagged else ""
            if sub == "network":
                self.calls.append(argv)
                if argv[2] == "ls":
                    name = argv[4][len("name=^"):-1]
                    return name + "\n" if name in self.networks else ""
                if argv[2] == "inspect":
                    return "true\n"
                self.networks.add(argv[-1])
                self.event("network")
                return ""
        return super().run(argv, **kwargs)

    def compose(self, argv):
        document = json.loads(super().compose(argv))
        if document["services"]:
            document["services"]["recordings-api"]["environment"] = {"RECORDINGS_EGRESS_ADMISSION": self.admission}
            document["networks"] = {
                "recordings-backend": {"name": "deedkit-recordings-backend", "external": True},
                "recordings-router": {"name": "deedkit-recordings-router", "external": True},
                "recordings-egress": {"name": "deedkit-recordings_recordings-egress", "driver": "bridge"}}
        return json.dumps(document)

    def attachments(self, project):
        """The networks the project's files give its gateway: the module network only with the overlay listed."""
        return ["integration"] + (["recordings-backend"] if any(
            str(path).endswith("compose.gateway-recordings.yaml") for path in project["files"]) else [])

    def snapshot(self, project, **kwargs):
        """The resolved configuration of the project as its files give it now, and what runs."""
        document = {"name": project["name"], "services": {"gateway": {
            "image": "built", "build": {"context": "."}, "networks": self.attachments(project)}}}
        return document, {"gateway": {"image": self.identities.get(project["name"], "enroll")}}

    # plan A and plan B: recorded instead of run
    def prepare(self, settings, sha):
        self.event("prepare")
        (self.state / sha).mkdir()
        p.write(self.state / sha / "prepared.json", {"sha": sha, "config_digest": p.settings_digest(settings)})

    def release_apply(self, settings, sha, **kwargs):
        p.write(self.state / sha / "attempted.json", {"sha": sha})
        self.identities = {name: "new" for name in self.identities}
        # Plan A recreates the gateway from the edited files, healthy or not.
        self.runtime_networks = {project["name"]: self.attachments(project) for project in settings["projects"]
                                 if project["name"] in self.runtime_networks}
        try:
            self.event("apply")
        except p.Refused:
            p.write(self.state / sha / "result.json", {"sha": sha, "status": "unhealthy"})
            raise
        p.write(self.state / sha / "result.json", {"sha": sha, "status": "applied"})
        self.record_current(sha, settings)

    def release_rollback(self, settings, sha, http=None):
        self.event("rollback")
        if not self.restore_leaves:
            self.identities = dict(self.before_identities)
        p.write(self.state / sha / "rollback.json", {"sha": sha, "status": "rolled-back"})

    def http_prepare(self, http, sha):
        self.event("seamless prepare")
        (self.http_state / sha).mkdir()
        p.write(self.http_state / sha / "http-prepared.json", {"sha": sha})

    def http_promote(self, http, sha):
        p.write(self.http_state / sha / "http-attempted.json", {"status": "switching"})
        try:
            self.event("seamless promote")
        except p.Refused:
            p.write(self.http_state / sha / "http-attempted.json", {"status": "recovery-required"})
            raise
        p.write(self.http_state / sha / "http-attempted.json", {"status": "serving"})
        p.write(self.http_state / "http-current.json", {"sha": sha, "status": "serving", "config_digest": p.digest(http)})
        return {"sha": sha, "status": "serving"}

    def http_status(self, http, sha):
        self.event("status")
        return {"sha": sha, "generation": "g2", "status": "serving"}

    # commands
    def command(self, *flags):
        args = p.argument_parser().parse_args(["enroll", "--module", "recordings", "--sha", SHA, "--config", str(self.config_path),
                                               "--http-config", str(self.http_path), *flags])
        return p.enroll_command(args)

    def apply(self, digest=None):
        return self.command("--apply", "--plan-digest", digest or self.plan()["plan_digest"])

    def undo(self):
        return self.command("--undo")

    def receipt(self):
        return json.loads((self.state / "enrollment.json").read_text())

    def names(self):
        return [name for name, _ in self.events]

    def held(self):
        """The events inside the run: while this run held the pause."""
        return [name for name, paused in self.events if paused]

    def files(self):
        return {path.name: path.read_bytes() for path in (self.config_path, self.http_path, self.policy)}

    def mutations(self):
        return [name for name in self.names() if name not in ("gate", "drift")]


def fail_at(host, point, error="boom"):
    host.failures[point] = p.Refused(error)
    return host.apply()


# spec: ANYM-746
@pytest.mark.parametrize("change", [another_config_byte, another_http_config, another_grant, another_live_policy,
                                    another_overlay, another_env_byte, another_private_byte, another_state_dir])
def test_apply_refuses_a_plan_that_changed_since_it_was_approved_before_any_write(tmp_path, monkeypatch, change):
    host = Applier(tmp_path, monkeypatch)
    approved = host.plan()["plan_digest"]
    change(host)
    host.sync()
    before = host.files()
    result = host.apply(approved)
    assert result == {"status": "refused", "refusals": [{"code": "plan_changed", "detail": result["refusals"][0]["detail"]}]}
    assert host.mutations() == [] and host.files() == before
    assert not p.PAUSE.exists() and not (host.state / "enrollment.json").exists()


# spec: ANYM-746
def test_apply_needs_the_reviewed_digest_and_exactly_one_mode(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    for flags in ([], ["--apply"], ["--apply", "--plan-digest", "x"], ["--plan", "--plan-digest", "a" * 64],
                  ["--plan", "--undo"]):
        with pytest.raises(p.Refused):
            host.command(*flags)
    assert host.apply("0" * 64)["refusals"][0]["code"] == "plan_changed" and host.mutations() == []


# spec: ANYM-746
def test_apply_runs_every_step_in_one_order_under_the_pause_and_removes_it_last(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    before = host.files()
    result = host.apply()
    assert result["status"] == "enrolled", result
    assert host.names()[0] == "gate" and host.held() == OVERLAY_EVENTS  # backup wait first; every step under the pause
    assert not p.PAUSE.exists() and host.names()[-1] == "drift"
    receipt = host.receipt()
    assert receipt["status"] == "applied" and receipt["steps"] == [
        "backup", "overlays", "first_start", "configuration", "policy", "plan_a", "plan_b", "status", "isolation", "checks",
        "complete"]
    assert (host.state / "enrollment.json").stat().st_mode & 0o777 == 0o600
    for name, path in (("production", host.config_path), ("http", host.http_path), ("policy", host.policy)):
        copy = Path(receipt["files"][name]["backup"])
        assert copy.read_bytes() == before[path.name] and copy.stat().st_mode & 0o777 == 0o600
        assert receipt["files"][name]["after_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert json.loads(host.policy.read_text())["revision"] == 23
    assert [project["name"] for project in json.loads(host.config_path.read_text())["projects"]] == [
        "deedkit-gateway", "deedkit-recordings"]
    assert (host.deploy / "compose.gateway-recordings.yaml").read_bytes() == GATEWAY_OVERLAY


# spec: ANYM-746
def test_the_first_start_uses_the_attested_image_under_an_enroll_tag_and_never_builds(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    assert host.apply()["status"] == "enrolled"
    argv, env = host.up
    assert "--no-build" in argv and argv[argv.index("--pull") + 1] == "never" and "--build" not in argv
    assert argv[argv.index("up"):] == ["up", "-d", "--no-build", "--pull", "never", "--no-deps", "recordings-api"]
    assert env["DEEDKIT_VERSION"] == f"enroll-{SHA}"
    assert host.tagged == {f"deedkit-recordings:enroll-{SHA}"}
    tag = next(call for call in host.calls if call[:2] == ["docker", "tag"])
    assert tag[2] == f"ghcr.io/gosync-inc/deedkit-recordings@{IMAGE_ID}"
    created = [call for call in host.calls if call[:3] == ["docker", "network", "create"]]
    assert [call[-1] for call in created] == ["deedkit-recordings-backend", "deedkit-recordings-router"]
    assert all("--internal" in call for call in created)
    assert not any(call[:2] in (["docker", "build"], ["docker", "rm"]) or "down" in call for call in host.calls)


# spec: ANYM-746
def test_apply_waits_for_a_running_backup_before_it_writes_anything(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    @contextlib.contextmanager
    def busy(settings, bound):
        raise p.BackupRunning("deedkit-backup.service", 0)
        yield
    monkeypatch.setattr(p, "backup_gate", busy)
    with pytest.raises(p.BackupRunning):
        host.apply()
    assert host.mutations() == [] and not p.PAUSE.exists() and not (host.state / "enrollment.json").exists()


# spec: ANYM-746
def test_a_call_active_at_apply_is_refused_before_the_pause(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    plan = host.plan()["plan_digest"]
    host.active = ["call-1"]
    assert host.apply(plan)["refusals"][0]["code"] == "call_active"
    assert host.mutations() == [] and not p.PAUSE.exists()


class NetworkHost(Applier):
    """`Applier` whose project document gains the `extra` networks and whose existing networks may be not internal."""
    extra, internal = {}, True

    def compose(self, argv):
        document = json.loads(super().compose(argv))
        if document["services"]:
            document["networks"].update(self.extra)
        return json.dumps(document)

    def run(self, argv, **kwargs):
        if argv[:3] == ["docker", "network", "inspect"]:
            self.calls.append(argv)
            return ("true" if self.internal else "false") + "\n"
        return super().run(argv, **kwargs)


def refused_before_the_first_write(host, result, code, files, before):
    assert {item["code"] for item in result["refusals"]} == {code}, result
    assert not p.PAUSE.exists() and not (host.state / "enrollment.json").exists()
    # The host lock is taken before the check, in the state directory, and holds nothing of the enrollment.
    written = [item for item in set(tree(host.root)) ^ set(before) if item[0] != "state" and not item[0].endswith(".lock")]
    assert host.files() == files and written == [] and host.mutations() == [] and host.up is None
    assert not any(call[:3] == ["docker", "network", "create"]
                   or (call[:2] == ["docker", "compose"] and {"up", "stop", "down", "restart", "start"} & set(call))
                   for call in host.calls)


UNDECLARED = [{"undeclared": {"name": "deedkit-undeclared", "external": True}}, {"deedkit-undeclared": {"external": True}}]


# spec: ANYM-765
@pytest.mark.parametrize("network", UNDECLARED, ids=["named", "key_only"])
def test_an_undeclared_external_network_refuses_apply_before_the_first_write(tmp_path, monkeypatch, network):
    host = NetworkHost(tmp_path, monkeypatch)
    approved = host.plan()["plan_digest"]
    host.extra = network
    files, before = host.files(), tree(host.root)
    result = host.apply(approved)
    refused_before_the_first_write(host, result, "network_unsafe", files, before)
    assert "deedkit-undeclared" in result["refusals"][0]["detail"]


# spec: ANYM-765
@pytest.mark.parametrize("network", UNDECLARED, ids=["named", "key_only"])
def test_the_plan_reports_an_undeclared_external_network(tmp_path, monkeypatch, network):
    host = NetworkHost(tmp_path, monkeypatch)
    host.extra = network
    assert host.codes() == ["network_unsafe"]


# spec: ANYM-765
def test_a_declared_network_that_exists_and_is_not_internal_refuses_apply_before_the_first_write(tmp_path, monkeypatch):
    host = NetworkHost(tmp_path, monkeypatch)
    approved = host.plan()["plan_digest"]
    host.networks, host.internal = {"deedkit-recordings-backend"}, False
    files, before = host.files(), tree(host.root)
    result = host.apply(approved)
    refused_before_the_first_write(host, result, "network_unsafe", files, before)
    assert "deedkit-recordings-backend" in result["refusals"][0]["detail"]


# spec: ANYM-765
def test_a_plan_that_cannot_resolve_the_document_says_so_and_apply_writes_nothing(tmp_path, monkeypatch):
    host = NetworkHost(tmp_path, monkeypatch)
    approved = host.plan()["plan_digest"]
    host.compose_error = p.Refused("docker failed (exit 1); production was not accepted")
    files, before = host.files(), tree(host.root)
    refused_before_the_first_write(host, host.apply(approved), "compose_config_failed", files, before)


# spec: ANYM-765
@pytest.mark.parametrize("networks, undeclared", [
    ({"backend": {"name": "declared", "external": True}}, None),
    ({"declared": {"external": True}}, None),
    ({"backend": {"name": "other", "external": True}}, "other"),
    ({"other": {"external": True}}, "other"),
    ({"other": {"name": "other", "driver": "bridge"}, "local": {"external": False}}, None),
], ids=["named_declared", "key_declared", "named_undeclared", "key_undeclared", "not_external"])
def test_the_external_names_come_from_the_name_or_the_compose_key(networks, undeclared):
    project = {"networks": [{"name": "declared", "bridge": "dk-declared0"}]}
    if undeclared is None:
        p.check_external_networks(project, {"networks": networks})
        return
    with pytest.raises(p.EnrollRefused) as refused:
        p.check_external_networks(project, {"networks": networks})
    assert refused.value.code == "network_unsafe" and undeclared in refused.value.detail


# spec: ANYM-765
def test_declared_and_non_external_networks_pass_and_only_the_first_start_creates_the_missing_one(tmp_path, monkeypatch):
    host = NetworkHost(tmp_path, monkeypatch)
    host.networks = {"deedkit-recordings-backend"}
    host.extra = {"deedkit-recordings-router": {"external": True}, "local": {"driver": "bridge"}}
    assert host.apply()["status"] == "enrolled"
    created = [call[-1] for call in host.calls if call[:3] == ["docker", "network", "create"]]
    assert created == ["deedkit-recordings-router"] and ("network", True) in host.events


# spec: ANYM-765
def test_a_network_that_turns_unsafe_after_the_check_stops_the_first_start_with_the_pause_kept(tmp_path, monkeypatch):
    host = NetworkHost(tmp_path, monkeypatch)
    host.networks = {"deedkit-recordings-backend"}
    host.triggers["overlay"] = lambda: setattr(host, "internal", False)
    result = host.apply()
    assert result["status"] == "stopped" and result["code"] == "network_unsafe", result
    assert "up" not in host.names() and "network" not in host.names() and p.PAUSE.exists()


# spec: ANYM-746
@pytest.mark.parametrize("after, stopped_before, started", [("overlay", "up", False), ("up", "edit:production", True),
                                                             ("edit:policy", "prepare", True)])
def test_a_call_that_starts_between_steps_stops_the_run_there_with_the_pause_kept(tmp_path, monkeypatch, after, stopped_before,
                                                                                  started):
    host = Applier(tmp_path, monkeypatch)
    before = host.files()
    host.triggers[after] = lambda: setattr(host, "active", ["call-1"])
    result = host.apply()
    assert result["status"] == "stopped" and result["code"] == "active_call", result
    assert stopped_before not in host.names() and p.PAUSE.exists()
    assert ("stop" in host.names()) is started
    assert host.files()["production.json"] == before["production.json"] and host.files()["http-production.json"] == before["http-production.json"]
    assert host.receipt()["status"] == "stopped"


# spec: ANYM-746
@pytest.mark.parametrize("point, started, rolled", [
    ("overlay", False, False), ("network", False, False), ("tag", False, False), ("up", True, False),
    ("edit:production", True, False), ("edit:http", True, False), ("edit:policy", True, False), ("apply", True, True),
    ("seamless prepare", True, True)])
def test_a_failure_before_promotion_restores_the_projects_the_files_and_the_policy_and_keeps_the_pause(
        tmp_path, monkeypatch, point, started, rolled):
    host = Applier(tmp_path, monkeypatch)
    before = host.files()
    result = fail_at(host, point)
    assert result["status"] == "stopped" and result["failed_step"] and "recovery" in result, result
    assert p.PAUSE.exists() and host.receipt()["status"] == "stopped"
    assert host.files()["production.json"] == before["production.json"]
    assert host.files()["http-production.json"] == before["http-production.json"]
    restored = json.loads(host.policy.read_text())
    if point in ("edit:policy", "apply", "seamless prepare"):  # a revision above anything a gateway may have loaded
        assert restored["revision"] == 24 and {**restored, "revision": 22} == POLICY
    else:
        assert host.files()["policy.json"] == before["policy.json"]
    assert host.names().count("stop") == (1 if started else 0)
    assert host.names().count("rollback") == (1 if rolled else 0)
    assert host.identities == host.before_identities
    assert host.names().count("restore") == (2 if rolled else 0)  # the gateway, from its pre-run configuration
    assert "prepare" not in host.names() or point in ("apply", "seamless prepare")


# spec: ANYM-746
def test_a_failed_recovery_says_the_state_is_unknown_and_changes_nothing_more(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    host.failures["rollback"] = p.Refused("docker is gone")
    result = fail_at(host, "apply")
    edited = host.files()
    assert result["status"] == "stopped" and "production state is unknown, inspect it" in json.dumps(result)
    assert host.receipt()["status"] == "unknown" and p.PAUSE.exists()
    assert json.loads(host.config_path.read_text())["projects"][-1]["name"] == "deedkit-recordings"  # not restored
    assert "stop" not in host.names() and edited == host.files()


# spec: ANYM-746
@pytest.mark.parametrize("point", ["seamless promote", "status"])
def test_a_failure_at_or_after_promotion_changes_nothing_more_and_says_http_recovery_is_manual(tmp_path, monkeypatch, point):
    host = Applier(tmp_path, monkeypatch)
    result = fail_at(host, point)
    assert result["status"] == "stopped" and "HTTP recovery is manual, inspect it" in result["recovery"]
    assert set(host.names()[host.names().index(point) + 1:]) <= {"status"} and p.PAUSE.exists()  # a read, no mutation
    assert host.receipt()["status"] == "http-manual" and "http" in host.receipt()
    assert json.loads(host.config_path.read_text())["projects"][-1]["name"] == "deedkit-recordings"
    assert json.loads(host.policy.read_text())["revision"] == 23


# spec: ANYM-746
def test_completion_needs_the_real_drift_helper_and_a_pause_this_run_owns(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    original = host.release_apply
    def drifting(settings, sha, **kwargs):
        original(settings, sha, **kwargs)
        current = p.read(host.state / "current.json")
        current["rollback"]["projects"][0]["after_digest"] = "0" * 64  # the applied Compose configuration differs
        p.write(host.state / "current.json", current)
    monkeypatch.setattr(p, "apply", drifting)
    result = host.apply()
    assert result["status"] == "stopped" and "Compose configuration" in result["reason"] and p.PAUSE.exists()


# spec: ANYM-746
def test_the_pause_is_left_when_another_writer_replaced_it_during_the_run(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    host.triggers["status"] = lambda: p.PAUSE.write_text("someone else paused autodeploy\n")
    result = host.apply()
    assert result["status"] == "stopped" and p.PAUSE.read_text() == "someone else paused autodeploy\n"


# spec: ANYM-746
def test_a_module_whose_admission_is_not_required_is_never_started(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    host.admission = "optional"
    result = host.apply()
    assert result["code"] == "egress_admission_not_required" and "up" not in host.names() and p.PAUSE.exists()


# spec: ANYM-746
def test_an_archive_key_added_after_the_plan_refuses_the_start(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    host.triggers["tag"] = lambda: (host.state_dir / "config.json").write_text(json.dumps({**STATE_CONFIG, "archive": {"path": "/x"}}))
    result = host.apply()
    assert result["code"] == "private_config_forbidden_key" and "up" not in host.names()


# spec: ANYM-746
def test_the_confinement_check_runs_in_the_container_with_a_host_control_per_blocked_target(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    assert host.apply()["status"] == "enrolled"
    inside = next(argv for argv, _ in host.inputs if argv[0] == "docker" and "check" in argv and not any(a.endswith(":13240") for a in argv))
    assert inside[inside.index("exec"):inside.index("exec") + 3] == ["exec", "-T", "recordings-api"]
    assert ["--blocked", "192.0.2.254:9"] == inside[inside.index("--blocked"):inside.index("--blocked") + 2]  # the canary is probed
    host_check = next(argv for argv, _ in host.inputs if argv[0] == sys.executable)
    controls = [host_check[index + 1] for index, item in enumerate(host_check) if item == "--control"]
    assert controls == ["10.99.0.1:22", "100.64.0.5:22"]  # each blocked target but the canary


# spec: ANYM-746
@pytest.mark.parametrize("attribute, code", [("confined", "confinement_check_failed"), ("controlled", "confinement_check_failed")])
def test_a_failed_confinement_check_or_positive_control_stops_before_the_configuration_is_edited(tmp_path, monkeypatch, attribute, code):
    host = Applier(tmp_path, monkeypatch)
    setattr(host, attribute, False)
    result = host.apply()
    assert result["code"] == code and "edit:production" not in host.names() and host.names().count("stop") == 1


# spec: ANYM-746
def test_a_gateway_check_that_fails_after_the_configuration_edit_restores_everything(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    before = host.files()
    host.triggers["edit:http"] = lambda: setattr(host, "gateway_valid", False)
    result = host.apply()
    assert result["code"] == "policy_check_failed" and host.files()["production.json"] == before["production.json"]
    assert host.files()["policy.json"] == before["policy.json"] and "edit:policy" not in host.names()


# spec: ANYM-746
@pytest.mark.parametrize("attribute, value", [("isolated", False), ("inspect_fails", True)])
def test_a_failed_isolation_probe_stops_the_module_and_keeps_the_pause_and_no_address_is_written(
        tmp_path, monkeypatch, attribute, value):
    host = Applier(tmp_path, monkeypatch)
    setattr(host, attribute, value)
    result = host.apply()
    assert result["code"] == "isolation_check_failed" and host.names().count("stop") == 1 and p.PAUSE.exists()
    for text in [path.read_text() for path in (host.config_path, host.http_path, host.policy, host.state / "enrollment.json")]:
        assert GATEWAY_IP not in text and SECRET not in text


# spec: ANYM-746
def test_the_isolation_probe_covers_every_serving_gateway_but_writes_no_address(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    assert host.apply()["status"] == "enrolled"
    probes = [argv for argv, _ in host.inputs if any(a.endswith(":13240") for a in argv)]
    assert len(probes) == 1 and f"{GATEWAY_IP}:13240" in probes[0]
    assert any(call[:2] == ["docker", "exec"] and GATEWAY_IP in call for call in host.calls)  # the positive control
    for path in (host.config_path, host.http_path, host.policy, host.state / "enrollment.json"):
        assert GATEWAY_IP not in path.read_text() and SECRET not in path.read_text()


# spec: ANYM-746
@pytest.mark.parametrize("point", ["apply", "seamless prepare"])
def test_the_running_gateway_returns_to_its_pre_run_networks_after_plan_a_ran(tmp_path, monkeypatch, point):
    host = Applier(tmp_path, monkeypatch)
    assert host.runtime_networks == {"deedkit-gateway": ["integration"]}
    result = fail_at(host, point)
    assert result["status"] == "stopped" and host.receipt()["status"] == "stopped"
    # Plan A ran from the edited files and `rollback` keeps that topology; the restoration puts the saved one back.
    assert host.runtime_networks == {"deedkit-gateway": ["integration"]}


# spec: ANYM-746
def test_without_the_restoration_the_gateway_would_keep_the_module_network(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    monkeypatch.setattr(p.Enrollment, "restore_gateway", lambda self: None)
    assert fail_at(host, "seamless prepare")["status"] == "stopped"
    assert host.runtime_networks == {"deedkit-gateway": ["integration", "recordings-backend"]}  # what the fake would show


# spec: ANYM-746
def test_the_gateway_is_recreated_from_its_pre_run_configuration_not_the_edited_one(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    fail_at(host, "seamless prepare")
    item = host.receipt()["pre_run"]
    document = json.loads(Path(item["file"]).read_text())
    assert document == {"name": "deedkit-gateway", "services": {"gateway": {"image": "old", "networks": ["integration"]}}}
    restore = [argv for argv in host.calls if item["file"] in argv]
    assert [argv[argv.index(item["file"]) + 1] for argv in restore] == ["stop", "up"] and "--no-build" in restore[1]
    assert host.names().index("rollback") < host.names().index("restore")


# spec: ANYM-746
def test_a_rollback_that_leaves_an_existing_project_on_another_image_is_an_unknown_state(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    host.restore_leaves = True
    result = fail_at(host, "seamless prepare")
    assert host.receipt()["status"] == "unknown" and "unknown, inspect it" in result["recovery"]
    assert json.loads(host.config_path.read_text())["projects"][-1]["name"] == "deedkit-recordings"  # not restored
    assert p.PAUSE.exists() and "stop" not in host.names()


# spec: ANYM-746
def test_undo_waits_for_the_backup_gate_before_its_first_write(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    assert host.apply()["status"] == "enrolled"
    after = host.files()
    host.events.clear()
    @contextlib.contextmanager
    def busy(settings, bound):
        raise p.BackupRunning("deedkit-backup.service", 0)
        yield
    monkeypatch.setattr(p, "backup_gate", busy)
    with pytest.raises(p.BackupRunning):
        host.undo()
    assert host.files() == after and host.mutations() == [] and not p.PAUSE.exists()
    assert host.receipt()["status"] == "applied"


# spec: ANYM-746
def test_a_second_apply_refuses_until_the_first_is_undone_or_inspected(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    approved = host.plan()["plan_digest"]
    fail_at(host, "up")
    assert host.apply(approved)["refusals"][0]["code"] == "enrollment_unfinished"


# spec: ANYM-746
def test_undo_restores_the_files_byte_for_byte_raises_the_policy_revision_and_leaves_autodeploy_paused(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    before = host.files()
    assert host.apply()["status"] == "enrolled"
    host.events.clear()
    result = host.undo()
    assert result["status"] == "undone" and "newer green main SHA" in result["note"], result
    assert host.files()["production.json"] == before["production.json"]
    assert host.files()["http-production.json"] == before["http-production.json"]
    restored = json.loads(host.policy.read_text())
    assert restored["revision"] == 24 and {**restored, "revision": 22} == POLICY
    assert p.PAUSE.exists() and host.receipt()["status"] == "undone"
    assert host.names()[-1] == "stop"
    assert (host.deploy / "compose.gateway-recordings.yaml").exists() and host.networks  # nothing else is removed
    assert not any(call[:3] in (["docker", "network", "rm"], ["docker", "image", "rm"]) or "down" in call for call in host.calls)


# spec: ANYM-746
def test_undo_refuses_during_a_call_and_changes_nothing(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    assert host.apply()["status"] == "enrolled"
    after = host.files()
    host.events.clear()
    host.active = ["call-1"]
    assert host.undo()["refusals"][0]["code"] == "active_call"
    assert host.files() == after and host.mutations() == [] and not p.PAUSE.exists()


# spec: ANYM-746
@pytest.mark.parametrize("edit, code", [("production", "configuration_changed"), ("policy", "policy_changed")])
def test_undo_refuses_when_a_file_changed_since_the_enrollment(tmp_path, monkeypatch, edit, code):
    host = Applier(tmp_path, monkeypatch)
    assert host.apply()["status"] == "enrolled"
    target = host.config_path if edit == "production" else host.policy
    target.write_text(target.read_text() + " ")
    after = host.files()
    assert host.undo()["refusals"][0]["code"] == code and host.files() == after and not p.PAUSE.exists()


# spec: ANYM-746
def test_undo_refuses_without_an_applied_enrollment(tmp_path, monkeypatch):
    host = Applier(tmp_path, monkeypatch)
    assert host.undo()["refusals"][0]["code"] == "enrollment_not_applied"
    fail_at(host, "up")
    assert host.undo()["refusals"][0]["code"] == "enrollment_not_applied"


# spec: ANYM-746
def test_the_recordings_manifest_uses_only_keys_the_installed_host_tool_reads_and_the_networks_key_is_optional(tmp_path, monkeypatch):
    # Incident 11-04 (#632): a key the installed tool does not know is a malformed manifest to it, so every deploy paused
    # with `enroll_required` for the running recordings project. A key reaches the manifest after the tool that reads it
    # is installed on the host: `networks` since b755b45d (installed 11-04 14:43Z), restored by the second PR.
    installed = {"name", "files", "env_file", "env_required", "services", "networks"}
    assert set(json.loads(RECORDINGS)["project"]) == installed
    legacy = Enroller(tmp_path, monkeypatch, mutate=delete_path("project", "networks"))
    assert legacy.plan()["status"] == "plan"  # a manifest without the optional key still plans


# spec: ANYM-746
def test_enroll_reads_the_deploy_configuration_the_way_status_does(tmp_path, monkeypatch, capsys):
    host = Enroller(tmp_path, monkeypatch)
    from scripts import seamless_release as s
    seen = []
    monkeypatch.setattr(p.os, "geteuid", lambda: 0)
    monkeypatch.setenv("DEEDKIT_DEPLOY_CONFIG", "/etc/deedkit/release-test.json")
    monkeypatch.setattr(p, "config", lambda path: seen.append(path) or (host.settings if path != p.HTTP_CONFIG else host.http))
    monkeypatch.setattr(s, "validate_settings", lambda value: value)
    monkeypatch.setattr(p, "main_history", lambda state: host.root / "mirror")
    p.main(["enroll", "--module", "recordings", "--sha", SHA, "--plan"])
    assert seen[0] == "/etc/deedkit/release-test.json" and json.loads(capsys.readouterr().out)["status"] == "plan"


# spec: ANYM-746
def test_the_enrollment_flags_and_codes_are_documented_and_the_manifest_declares_its_networks():
    docs = " ".join((ROOT / "docs/operations/production.md").read_text().split())
    for text in ("--apply --plan-digest", "--undo", "enrollment.json", "HTTP recovery is manual, inspect it",
                 "production state is unknown, inspect it"):
        assert text in docs
    assert "`networks`" in docs  # the key is optional: the recordings manifest of the enrolled module declares none


# spec: ANYM-745
def test_the_command_line_prints_the_plan_and_exits_2_on_a_refusal(tmp_path, monkeypatch, capsys):
    host = Enroller(tmp_path, monkeypatch)
    from scripts import seamless_release as s
    monkeypatch.setattr(p.os, "geteuid", lambda: 0)
    monkeypatch.setattr(p, "config", lambda path: host.settings if path == p.CONFIG else host.http)
    monkeypatch.setattr(s, "validate_settings", lambda value: value)
    monkeypatch.setattr(p, "main_history", lambda state: host.root / "mirror")
    p.main(["enroll", "--module", "recordings", "--sha", SHA, "--plan"])
    assert json.loads(capsys.readouterr().out)["status"] == "plan"
    pause(host)
    with pytest.raises(SystemExit) as stopped:
        p.main(["enroll", "--module", "recordings", "--sha", SHA, "--plan"])
    assert stopped.value.code == 2
    assert json.loads(capsys.readouterr().out) == {"status": "refused", "refusals": [
        {"code": "paused", "detail": "the autodeploy pause file is present"}]}
    with pytest.raises(SystemExit) as stopped:
        p.main(["enroll", "--module", "recordings", "--sha", SHA])
    assert stopped.value.code == 1 and "--plan" in capsys.readouterr().err
