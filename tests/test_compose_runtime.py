"""Docker applies the merged messaging Compose stack: sandbox, limits, networks and logs.

Set DEEDKIT_TEST_PYTHON_IMAGE to the immutable Python image (the CI `python` step does). When it is set,
missing Docker, Compose or images are failures, like the real router proof. The stack is a copy of
`deploy/` with synthetic private files under a unique project name. PostgreSQL is the real pinned
image; every application service runs the Python test image under `sleep`, so the real application
images stay with images.yml. No host ports, credentials or provider sessions are used.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid

import pytest


REPO = Path(__file__).resolve().parents[1]
BASE = ["compose.yaml", "compose.collectors.yaml", "compose.whatsapp.yaml", "compose.indexer.yaml",
        "compose.enrichment.yaml", "compose.subscriptions.yaml", "compose.operator.yaml",
        "compose.file-lifecycle.yaml", "compose.file-understanding.yaml"]
NATIVE = "compose.whatsapp-native.yaml"
APPLICATIONS = {"api", "telegram", "whatsapp1", "whatsapp2", "indexer", "enricher", "enricher-local",
                "operator", "subscriptions", "file-lifecycle", "cold-archive", "file-understanding"}
COLLECTORS = {"telegram", "whatsapp1", "whatsapp2"}
SECRET_DIRECTORIES = ["processing-locks", "telegram-session", "whatsapp1-session", "whatsapp2-session",
                      "issued-keys", "cold-archive-identity", "cold-archive-ssh", "enrichment-models"]
SECRET_FILES = ["app.dsn", "collector.dsn", "indexer.dsn", "owner.dsn", "subscriptions.dsn", "enrichment.dsn",
                "enrichment-local.dsn", "file-lifecycle.dsn", "cold-archive.dsn", "file-understanding.dsn",
                "enrichment.keys", "enrichment.json", "enrichment-local.json", "file-understanding.json",
                "cold-archive.json", "telegram.json", "telegram.credentials.json", "telegram.permit.json",
                "whatsapp1.json", "whatsapp1.permit.json", "whatsapp2.json", "whatsapp2.permit.json"]
# What the generated override may change; healthcheck is checked apart (only its `test`).
OVERRIDE_KEYS = {"image", "entrypoint", "command", "ports"}


class Stack:
    def __init__(self, root: Path, image: str):
        self.deploy = root / "deploy"
        self.image = image
        # A unique project name never reuses another run's containers, networks or volumes.
        self.project = f"deedkit-t417-{uuid.uuid4().hex[:12]}"
        shutil.copytree(REPO / "deploy", self.deploy, ignore=shutil.ignore_patterns("private"))
        private = self.deploy / "private"
        private.mkdir()
        for name in SECRET_DIRECTORIES:
            (private / name).mkdir()
        for name in SECRET_FILES:
            (private / name).write_text("{}\n" if name.endswith(".json") else "synthetic\n")
        (private / "postgres.env").write_text(
            f"POSTGRES_USER=probe_owner\nPOSTGRES_PASSWORD={uuid.uuid4().hex}\nPOSTGRES_DB=messaging_probe\n")
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("COMPOSE_", "PROBE_", "MESSAGING_")) and key not in {"POSTGRES_IMAGE", "PYTHON_IMAGE"}}
        self.env.update(PROBE_PUBLIC_URL="https://stack.invalid", PROBE_ALLOWED_HOSTS="stack.invalid")

    def docker(self, *args, check=True, timeout=180):
        result = subprocess.run(["docker", *args], cwd=self.deploy, env=self.env, text=True,
                                capture_output=True, timeout=timeout)
        assert not check or result.returncode == 0, f"docker {' '.join(args)}\n{result.stdout}\n{result.stderr}"
        return result

    def compose(self, files, *args, check=True, timeout=180):
        command = ["compose", "-p", self.project, "--profile", "*"]
        for name in files:
            command += ["-f", name]
        return self.docker(*command, *args, check=check, timeout=timeout)

    def render(self, files):
        return json.loads(self.compose(files, "config", "--format", "json").stdout)

    def override(self, rendered):
        lines = ["services:"]
        for name, service in sorted(rendered["services"].items()):
            if name == "db":
                continue
            lines += [f"  {name}:", f"    image: {self.image}", "    entrypoint: !override [sleep]",
                      '    command: !override ["3600"]']
            if "healthcheck" in service:
                lines += ["    healthcheck:", '      test: !override [CMD, "true"]']
            if service.get("ports"):
                lines.append("    ports: !reset []")
        path = self.deploy / "compose.test-override.yaml"
        path.write_text("\n".join(lines) + "\n")
        return path.name

    def containers(self):
        ids = self.docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={self.project}").stdout.split()
        if not ids:
            return {}
        found = json.loads(self.docker("inspect", *ids).stdout)
        return {item["Config"]["Labels"]["com.docker.compose.service"]: item for item in found}

    def network(self, name):
        return json.loads(self.docker("network", "inspect", f"{self.project}_{name}").stdout)[0]

    def leftovers(self):
        label = f"label=com.docker.compose.project={self.project}"
        return {kind: self.docker(*command, "--filter", label, "-q").stdout.split()
                for kind, command in {"containers": ["ps", "-a"], "networks": ["network", "ls"],
                                      "volumes": ["volume", "ls"]}.items()}


def sandbox_facts(container):
    host = container["HostConfig"]
    return {"read_only": host["ReadonlyRootfs"], "user": container["Config"]["User"], "cap_drop": host["CapDrop"],
            "no_new_privileges": "no-new-privileges:true" in host["SecurityOpt"],
            "tmpfs": host["Tmpfs"], "pids": host["PidsLimit"], "memory": host["Memory"], "log": host["LogConfig"]}


def assert_application_bounds(name, container, declared):
    facts = sandbox_facts(container)
    assert container["State"]["Running"], name
    assert facts["read_only"] is True, name
    assert facts["user"] == "10001:10001", name
    assert facts["cap_drop"] == ["ALL"], name
    assert facts["no_new_privileges"], name
    assert facts["pids"] == declared["pids_limit"] > 0, name
    assert facts["memory"] == int(declared["mem_limit"]) > 0, name
    assert set(facts["tmpfs"]) == {"/tmp"}, name
    options = set(facts["tmpfs"]["/tmp"].split(","))
    assert {"noexec", "nosuid"} <= options and any(option.startswith("size=") for option in options), (name, options)
    assert options == set(declared["tmpfs"][0].split(":", 1)[1].split(",")), name
    if name == "operator":
        assert facts["log"]["Type"] == "none"
    else:
        assert facts["log"] == {"Type": "json-file", "Config": {"max-size": "5m", "max-file": "2"}}, name


def networks_of(container):
    return set(container["NetworkSettings"]["Networks"])


def assert_runtime(stack, declared, native):
    containers = stack.containers()
    assert set(containers) == APPLICATIONS | {"db"}
    for name in APPLICATIONS:
        assert_application_bounds(name, containers[name], declared["services"][name])
        assert not containers[name]["HostConfig"]["PortBindings"], name
        assert not containers[name]["NetworkSettings"]["Ports"], name
    db = containers["db"]
    assert db["State"]["Running"]
    assert db["HostConfig"]["LogConfig"] == {"Type": "json-file", "Config": {"max-size": "5m", "max-file": "2"}}
    assert networks_of(db) == {f"{stack.project}_backend"}
    assert stack.network("backend")["Internal"] is True
    members = {name: networks_of(container) for name, container in containers.items()}
    assert {name for name, joined in members.items() if f"{stack.project}_collector_egress" in joined} == COLLECTORS
    assert {name for name, joined in members.items() if f"{stack.project}_cold_archive_egress" in joined} == {"cold-archive"}
    assert stack.network("collector_egress")["Internal"] is False
    assert stack.network("cold_archive_egress")["Internal"] is False
    for name in ("whatsapp1", "whatsapp2"):
        seccomp = [item for item in containers[name]["HostConfig"]["SecurityOpt"] if item.startswith("seccomp=")]
        assert bool(seccomp) is not native, name
    worker = containers["subscriptions"]
    assert worker["Id"] != containers["api"]["Id"]
    assert networks_of(worker) == {f"{stack.project}_backend"}
    assert not worker["HostConfig"]["PortBindings"] and not worker["NetworkSettings"]["Ports"]
    secret = [mount for mount in worker["Mounts"] if mount["Destination"] == "/run/secrets/database_url"]
    assert len(secret) == 1 and secret[0]["Type"] == "bind" and secret[0]["RW"] is False
    assert secret[0]["Source"].endswith("/private/subscriptions.dsn")
    assert (worker["HostConfig"]["Memory"], worker["HostConfig"]["PidsLimit"], worker["HostConfig"]["CpuShares"]) == (
        256 * 1024 * 1024, 32, 512)
    assert worker["HostConfig"]["Tmpfs"] == {"/tmp": "size=16m,noexec,nosuid"}
    return containers


# spec: ANYM-417
def test_merged_compose_stack_applies_sandbox_limits_networks_and_logs():
    image = os.environ.get("DEEDKIT_TEST_PYTHON_IMAGE")
    if not image:
        pytest.skip("set DEEDKIT_TEST_PYTHON_IMAGE to run the merged Compose stack")
    assert re.search(r"@sha256:[0-9a-f]{64}$", image), "the Python test image must be immutable"
    assert shutil.which("docker"), "Docker is required when the Compose runtime proof is enabled"
    with tempfile.TemporaryDirectory(prefix="deedkit-compose-") as root:
        stack = Stack(Path(root), image)
        assert stack.docker("compose", "version", check=False).returncode == 0, "Docker Compose v2 is required"
        browser = {}
        for native in (False, True):
            files = BASE + ([NATIVE] if native else [])
            declared = stack.render(files)
            assert set(declared["services"]) == APPLICATIONS | {"db"}
            for reference in (image, declared["services"]["db"]["image"]):
                stack.docker("image", "inspect", reference)
            tested = files + [stack.override(declared)]
            merged = stack.render(tested)
            for name, service in declared["services"].items():
                changed = {key for key in set(service) | set(merged["services"][name]) if service.get(key) != merged["services"][name].get(key)}
                if name == "db":
                    assert not changed
                    continue
                assert changed <= OVERRIDE_KEYS | {"healthcheck"}, (name, changed)
                assert merged["services"][name]["image"] == image
                assert merged["services"][name]["entrypoint"] == ["sleep"] and merged["services"][name]["command"] == ["3600"]
                assert not merged["services"][name].get("ports")
                if "healthcheck" in service:
                    assert {key: value for key, value in service["healthcheck"].items() if key != "test"} == {
                        key: value for key, value in merged["services"][name]["healthcheck"].items() if key != "test"}
            assert merged["networks"] == declared["networks"] and merged["volumes"] == declared["volumes"]
            try:
                stack.compose(tested, "up", "-d", "--no-build", "--pull", "never", timeout=300)
                containers = assert_runtime(stack, merged, native)
                if native:
                    for name in ("whatsapp1", "whatsapp2"):
                        before, after = sandbox_facts(browser[name]), sandbox_facts(containers[name])
                        assert {key: value for key, value in after.items() if key not in {"pids", "memory"}} == {
                            key: value for key, value in before.items() if key not in {"pids", "memory"}}
                        assert 0 < after["pids"] <= before["pids"] and 0 < after["memory"] <= before["memory"]
                else:
                    browser = containers
            finally:
                down = stack.compose(tested, "down", "--volumes", check=False, timeout=180)
                leftovers = stack.leftovers()
            assert down.returncode == 0, down.stderr
            assert not any(leftovers.values()), leftovers
