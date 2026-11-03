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
import yaml

from scripts import production, publish_images


REPO = Path(__file__).resolve().parents[1]
BASE = ["compose.yaml", "compose.collectors.yaml", "compose.whatsapp.yaml", "compose.indexer.yaml",
        "compose.enrichment.yaml", "compose.subscriptions.yaml", "compose.operator.yaml",
        "compose.file-lifecycle.yaml", "compose.file-understanding.yaml", "compose.message-purge.yaml",
        "compose.delivery.yaml"]
NATIVE = "compose.whatsapp-native.yaml"
APPLICATIONS = {"api", "telegram", "whatsapp1", "whatsapp2", "indexer", "enricher", "enricher-local",
                "operator", "subscriptions", "file-lifecycle", "cold-archive", "cold-restore",
                "file-understanding", "message-purge", "delivery", "delivery-egress", "enricher-mini"}
COLLECTORS = {"telegram", "whatsapp1", "whatsapp2"}
SECRET_DIRECTORIES = ["processing-locks", "telegram-session", "whatsapp1-session", "whatsapp2-session",
                      "issued-keys", "cold-archive-identity", "cold-archive-ssh", "enrichment-models", "delivery-receivers", "inference"]
SECRET_FILES = ["app.dsn", "collector.dsn", "indexer.dsn", "owner.dsn", "subscriptions.dsn", "enrichment.dsn",
                "enrichment-local.dsn", "file-lifecycle.dsn", "cold-archive.dsn", "file-understanding.dsn", "message-purge.dsn",
                "delivery.dsn", "delivery-policy.json",
                "enrichment.keys", "enrichment.json", "enrichment-local.json", "enrichment-mini.json", "file-understanding.json",
                "cold-archive.json", "telegram.json", "telegram.credentials.json", "telegram.permit.json",
                "whatsapp1.json", "whatsapp1.permit.json", "whatsapp2.json", "whatsapp2.permit.json"]
# What the generated override may change in every application service; `ports` only for the api and
# `healthcheck` (only its `test`) only where the service declares one.
OVERRIDE_KEYS = {"image", "entrypoint", "command"}
API_PORTS = [{"mode": "ingress", "host_ip": "127.0.0.1", "target": 8080, "published": "18940", "protocol": "tcp"}]
BROWSER_ONLY = {"image", "security_opt", "mem_limit", "pids_limit"}


class Stack:
    def __init__(self, root: Path, image: str):
        self.deploy = root / "deploy"
        self.image = image
        self.project = ""
        # Local private files and `.env` (Compose reads it from the project directory) stay out of the copy.
        shutil.copytree(REPO / "deploy", self.deploy, ignore=shutil.ignore_patterns("private", ".env*"))
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

    def new_project(self):
        # A unique project name per rendering never reuses another run's containers, networks or volumes.
        self.project = f"deedkit-t417-{uuid.uuid4().hex[:12]}"

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
            if name == "api":
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

    def remove(self, files):
        """`down --volumes`; if it fails or times out, remove what carries this project's label."""
        try:
            down = self.compose(files, "down", "--volumes", check=False, timeout=180)
            if down.returncode == 0:
                return None
            reason = down.stderr
        except subprocess.TimeoutExpired:
            reason = "docker compose down timed out"
        label = f"label=com.docker.compose.project={self.project}"
        for kind, remove in (("ps", ["rm", "-f"]), ("network", ["network", "rm"]), ("volume", ["volume", "rm"])):
            list_command = ["ps", "-a"] if kind == "ps" else [kind, "ls"]
            found = self.docker(*list_command, "--filter", label, "-q", check=False).stdout.split()
            if found:
                self.docker(*remove, *found, check=False)
        return reason

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
    assert container["HostConfig"]["Privileged"] is False and not container["HostConfig"]["CapAdd"], name
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


def members_of(containers):
    return {name: networks_of(container) for name, container in containers.items()}


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
    assert networks_of(db) == {f"{stack.project}_backend", f"{stack.project}_delivery_db", f"{stack.project}_inference_db"}
    assert stack.network("backend")["Internal"] is True
    # ANYM-534/581: the dispatcher joins only internal networks; only the egress proxy joins the egress bridge.
    members = members_of(containers)
    assert {name for name, joined in members.items() if f"{stack.project}_delivery_db" in joined} == {"db", "delivery"}
    assert networks_of(containers["delivery"]) == {f"{stack.project}_delivery_db", f"{stack.project}_delivery_proxy"}
    assert {name for name, joined in members.items() if f"{stack.project}_delivery_proxy" in joined} == {"delivery", "delivery-egress"}
    assert {name for name, joined in members.items() if f"{stack.project}_delivery_egress" in joined} == {"delivery-egress"}
    assert stack.network("delivery_db")["Internal"] is True and stack.network("delivery_proxy")["Internal"] is True
    assert stack.network("delivery_egress")["Internal"] is False
    # ANYM-688: the mini-facing worker joins only its internal database network and its own egress bridge.
    members = members_of(containers)
    assert networks_of(containers["enricher-mini"]) == {f"{stack.project}_inference_db", f"{stack.project}_inference_egress"}
    assert {name for name, joined in members.items() if f"{stack.project}_inference_db" in joined} == {"db", "enricher-mini"}
    assert {name for name, joined in members.items() if f"{stack.project}_inference_egress" in joined} == {"enricher-mini"}
    assert stack.network("inference_db")["Internal"] is True and stack.network("inference_egress")["Internal"] is False
    members = members_of(containers)
    assert {name for name, joined in members.items() if f"{stack.project}_collector_egress" in joined} == COLLECTORS
    assert {name for name, joined in members.items() if f"{stack.project}_cold_archive_egress" in joined} == {"cold-archive", "cold-restore"}
    assert stack.network("collector_egress")["Internal"] is False
    assert stack.network("cold_archive_egress")["Internal"] is False
    profile = json.loads((stack.deploy / "chromium-seccomp.json").read_text())
    for name in ("whatsapp1", "whatsapp2"):
        options = containers[name]["HostConfig"]["SecurityOpt"]
        if native:
            assert options == ["no-new-privileges:true"], name
        else:
            seccomp = [item for item in options if item.startswith("seccomp=")]
            assert sorted(set(options) - set(seccomp)) == ["no-new-privileges:true"] and len(seccomp) == 1, name
            assert seccomp[0] != "seccomp=unconfined" and json.loads(seccomp[0].split("=", 1)[1]) == profile, name
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


class ComposeLoader(yaml.SafeLoader):
    """Reads Compose's `!override` and `!reset` tags without constructing Python objects."""


ComposeLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node)
                              if isinstance(node, yaml.SequenceNode) else loader.construct_mapping(node))
ComposeLoader.add_constructor("!reset", lambda loader, node: None)


def overlay(name="compose.enrichment.yaml"):
    return yaml.load((REPO / "deploy" / name).read_text(), Loader=ComposeLoader)


def mini_service():
    return overlay()["services"]["enricher-mini"]


def services_on(network):
    """Every service of any overlay that joins `network` (a service's lists merge across overlays)."""
    found = set()
    for path in sorted((REPO / "deploy").glob("compose*.yaml")):
        for name, service in (yaml.load(path.read_text(), Loader=ComposeLoader).get("services") or {}).items():
            if network in (service.get("networks") or []):
                found.add(name)
    return found


# spec: ANYM-697
def test_enricher_mini_has_a_tmpfs_for_a_200_mib_original_and_the_others_keep_128m():
    assert mini_service()["tmpfs"] == ["/tmp:size=320m,noexec,nosuid"]
    assert overlay()["x-enrichment-common"]["tmpfs"] == ["/tmp:size=128m,noexec,nosuid"]


# spec: ANYM-688
def test_enricher_mini_uses_a_published_build_target():
    service = mini_service()
    assert service["profiles"] == ["enrichment-mini"]
    target = service["build"]["target"]
    assert target == "enricher-local" and target in production.BUILDS and target in publish_images.TARGETS
    assert service["image"].startswith("anymsg-enricher-local:")
    # The `enricher` target is neither built nor published, and the mini service does not use it.
    assert "enricher" not in production.BUILDS and "enricher" not in publish_images.TARGETS


# spec: ANYM-688
def test_mini_example_config_has_only_deedkit_inference_profiles(tmp_path):
    from messaging_service.enrichment_providers import validate_profile
    config = json.loads((REPO / "deploy" / "enrichment-mini.example.json").read_text())
    assert config["enabled"] is False and set(config["profiles"]) == {"audio", "image", "document"}
    key = tmp_path / "key"
    key.write_text("synthetic-key")
    key.chmod(0o600)
    for kind, profile in config["profiles"].items():
        assert profile["provider"] == "deedkit_inference" and profile["local_inference"] is True, kind
        assert "allow_cloud" not in profile and profile["api_key_file"] == "/run/secrets/inference/inference.key", kind
        # The real profile validation (ANYM-685) accepts it with the key path pointed at a synthetic file.
        validate_profile({**profile, "api_key_file": str(key)}, kind)


# spec: ANYM-688
def test_enricher_mini_joins_only_inference_db_and_inference_egress():
    service = mini_service()
    assert sorted(service["networks"]) == ["inference_db", "inference_egress"]
    assert "network_mode" not in service
    egress = overlay()["networks"]["inference_egress"]
    assert egress["driver"] == "bridge" and not egress.get("internal") and egress["enable_ipv6"] is False
    assert egress["driver_opts"]["com.docker.network.bridge.name"] == "dk-inference0"
    # No other service joins the egress bridge, and the worker is on neither `backend` nor `frontend`.
    assert services_on("inference_egress") == {"enricher-mini"}
    assert not {"backend", "frontend"} & set(service["networks"])


# spec: ANYM-688
def test_inference_db_is_internal_isolated_and_holds_only_db_and_the_worker():
    network = overlay()["networks"]["inference_db"]
    assert network["internal"] is True and network["enable_ipv6"] is False
    assert network["driver_opts"] == {"com.docker.network.bridge.name": "dk-inferencedb0",
                                      "com.docker.network.bridge.gateway_mode_ipv4": "isolated"}
    assert overlay()["services"]["db"]["networks"] == ["inference_db"]
    assert services_on("inference_db") == {"db", "enricher-mini"}


# spec: ANYM-688
def test_enricher_mini_has_no_ports_no_socket_ro_mounts_and_the_hardening():
    service = mini_service()
    assert not service.get("ports") and not service.get("expose")
    assert not {"privileged", "cap_add", "pid", "ipc", "devices"} & set(service)
    mounts = [mount for mount in service["volumes"] if isinstance(mount, dict)] + [
        {"source": item.split(":")[0], "target": item.split(":")[1], "read_only": item.endswith(":ro")}
        for item in service["volumes"] if isinstance(item, str)]
    assert not any("docker.sock" in str(mount.get("source")) for mount in mounts)
    by_target = {mount["target"]: mount for mount in mounts}
    assert set(by_target) == {"/run/secrets/database_url", "/run/secrets/enrichment_config", "/run/secrets/inference",
                              "/data", "/processing-locks"}
    # Same as enricher-local: the files volume and the secrets read-only; the lock directory is the one writable bind.
    for target in ("/run/secrets/database_url", "/run/secrets/enrichment_config", "/run/secrets/inference", "/data"):
        assert by_target[target]["read_only"] is True, target
    for target in ("/run/secrets/database_url", "/run/secrets/enrichment_config", "/run/secrets/inference", "/processing-locks"):
        assert by_target[target]["bind"] == {"create_host_path": False}, target
    assert not by_target["/processing-locks"].get("read_only")
    assert by_target["/run/secrets/database_url"]["source"] == "./private/enrichment-local.dsn"
    assert by_target["/run/secrets/enrichment_config"]["source"] == "./private/enrichment-mini.json"
    assert service["read_only"] is True and service["user"] == "10001:10001" and service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["mem_limit"] and 0 < service["pids_limit"] <= 256 and service["cpus"]
    assert all("noexec" in mount and "nosuid" in mount and "size=" in mount for mount in service["tmpfs"])
    assert service["logging"] == {"driver": "json-file", "options": {"max-size": "5m", "max-file": "2"}}


# spec: ANYM-688
def test_key_is_mounted_as_a_directory_not_a_single_file():
    key = [m for m in mini_service()["volumes"] if isinstance(m, dict) and m["target"].startswith("/run/secrets/inference")]
    # A bind-mounted file keeps the old inode after an atomic replace; a directory shows the rotated key.
    assert len(key) == 1 and key[0]["source"] == "./private/inference" and key[0]["target"] == "/run/secrets/inference"
    assert not any(str(m.get("target", "")).endswith(".key") for m in mini_service()["volumes"] if isinstance(m, dict))
    config = json.loads((REPO / "deploy" / "enrichment-mini.example.json").read_text())
    assert {p["api_key_file"] for p in config["profiles"].values()} == {"/run/secrets/inference/inference.key"}


# spec: ANYM-688
def test_enricher_mini_pins_the_mini_name_and_sets_dns():
    from ipaddress import ip_address, ip_network
    from urllib.parse import urlsplit
    service = mini_service()
    config = json.loads((REPO / "deploy" / "enrichment-mini.example.json").read_text())
    host = urlsplit(config["profiles"]["audio"]["base_url"]).hostname
    assert len(service["extra_hosts"]) == 1
    pinned = re.fullmatch(r"([^:]+):\$\{INFERENCE_MINI_IP:-([0-9.]+)\}", service["extra_hosts"][0].replace(
        "${INFERENCE_MINI_NAME:-" + host + "}", host))
    assert pinned and pinned.group(1) == host and host.endswith(".ts.net")
    assert ip_address(pinned.group(2)) in ip_network("100.64.0.0/10")
    # The resolver is the mini's address too: any lookup but the pinned one goes where the host rules drop it.
    assert service["dns"] == ["${INFERENCE_MINI_IP:-" + pinned.group(2) + "}"]


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
        browser, browser_declared = {}, {}
        for native in (False, True):
            stack.new_project()
            files = BASE + ([NATIVE] if native else [])
            declared = stack.render(files)
            assert set(declared["services"]) == APPLICATIONS | {"db"}
            # Published ports are checked on the declared files: only the api publishes one, on loopback.
            assert {name: service["ports"] for name, service in declared["services"].items() if service.get("ports")} == {"api": API_PORTS}
            for reference in (image, declared["services"]["db"]["image"]):
                stack.docker("image", "inspect", reference)
            tested = files + [stack.override(declared)]
            merged = stack.render(tested)
            for name, service in declared["services"].items():
                changed = {key for key in set(service) | set(merged["services"][name]) if service.get(key) != merged["services"][name].get(key)}
                if name == "db":
                    assert not changed
                    continue
                allowed = OVERRIDE_KEYS | ({"ports"} if name == "api" else set()) | ({"healthcheck"} if "healthcheck" in service else set())
                assert changed <= allowed, (name, changed)
                assert merged["services"][name]["image"] == image
                assert merged["services"][name]["entrypoint"] == ["sleep"] and merged["services"][name]["command"] == ["3600"]
                assert not merged["services"][name].get("ports")
                if "healthcheck" in service:
                    assert {key: value for key, value in service["healthcheck"].items() if key != "test"} == {
                        key: value for key, value in merged["services"][name]["healthcheck"].items() if key != "test"}
            assert merged["networks"] == declared["networks"] and merged["volumes"] == declared["volumes"]
            try:
                # The stand-in image has no /data/files/.uploads, which the real image creates for the nested
                # `file_uploads` mount. A collector (rw `files`) starts first and creates the mountpoint;
                # otherwise the api's read-only `files` mount races with it. `up telegram` also starts db.
                stack.compose(tested, "up", "-d", "--no-build", "--pull", "never", "telegram", timeout=300)
                stack.compose(tested, "up", "-d", "--no-build", "--pull", "never", timeout=300)
                containers = assert_runtime(stack, merged, native)
                if native:
                    for name in ("whatsapp1", "whatsapp2"):
                        r1, r2 = browser_declared["services"][name], declared["services"][name]
                        assert {key for key in set(r1) | set(r2) if r1.get(key) != r2.get(key)} <= BROWSER_ONLY, name
                        assert r2["security_opt"] == ["no-new-privileges:true"], name
                        before, after = sandbox_facts(browser[name]), sandbox_facts(containers[name])
                        assert {key: value for key, value in after.items() if key not in {"pids", "memory"}} == {
                            key: value for key, value in before.items() if key not in {"pids", "memory"}}
                        assert 0 < after["pids"] <= before["pids"] and 0 < after["memory"] <= before["memory"]
                else:
                    browser, browser_declared = containers, declared
            finally:
                failure = stack.remove(tested)
                leftovers = stack.leftovers()
            assert failure is None, failure
            assert not any(leftovers.values()), leftovers
