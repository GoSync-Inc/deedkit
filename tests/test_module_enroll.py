"""`production.py enroll --plan` (ANYM-745): the module manifest, its refusal codes and the read-only plan."""
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


def token_not_named(host):
    (host.state_dir / "config.json").write_text(json.dumps({"accounts": []}))


# spec: ANYM-745
@pytest.mark.parametrize("setup", [token_missing, token_loose, token_empty, token_outside_the_mount, token_not_named])
def test_a_provider_credential_file_that_is_missing_loose_empty_or_unnamed_is_refused(tmp_path, monkeypatch, setup):
    host = Enroller(tmp_path, monkeypatch)
    setup(host)
    assert host.codes() == ["credential_file_invalid"]
    with pytest.raises(p.Refused) as refused:
        host.plan()
    assert SECRET not in json.dumps(refused.value.failures)


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
