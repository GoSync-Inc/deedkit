"""A policy renamed over the live file reaches the running gateway on its next request (ANYM-424).

Docker runs the checked-in `deploy/compose.gateway.yaml` gateway service with its own mounts and `--policy`
argument. An override changes only what a test must: a gateway image built here from this checkout, no host
port, a unique external network and, for a non-root runner, the container user (the runner's UID, so the
gateway can read the runner's own 0600 files). The private files are synthetic. Set DEEDKIT_TEST_PYTHON_IMAGE
(the CI `python` steps do) to run it; then missing Docker or Compose is a failure, as in the ANYM-417 proof.
No provider, backend, credential or host port is used.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
import time
import uuid

import pytest


REPO = Path(__file__).resolve().parents[1]
KEY = "synthetic-gateway-424"
PUBLIC_URL = "https://gateway.invalid/deedkit"
# Runs inside the gateway container: one authenticated REST call, printing the policy revision it reports.
REQUEST = """
import json, sys, urllib.request
request = urllib.request.Request("http://127.0.0.1:13240/v1/platform_capabilities", data=b"{}", method="POST",
    headers={"Host": "gateway.invalid", "Authorization": "Bearer " + sys.argv[1], "Content-Type": "application/json"})
try:
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
        print(json.load(response)["policy_revision"])
except Exception as error:
    print("unavailable", type(error).__name__, getattr(error, "code", ""))
    sys.exit(1)
"""


def policy(revision):
    return {"version": 1, "revision": revision, "deployment": {"modules": [], "backends": {}},
            "profiles": {"reader": {"tools": ["platform_capabilities"]}},
            "principals": [{"id": "synthetic-agent", "key_sha256": hashlib.sha256(KEY.encode()).hexdigest(),
                            "profile": "reader", "enabled": True, "expires_at": "2099-01-01T00:00:00Z",
                            "grants": {}, "credentials": {}}]}


class Gateway:
    def __init__(self, root: Path, image: str):
        self.deploy, self.image = root / "deploy", image
        self.project = f"deedkit-t424-{uuid.uuid4().hex[:12]}"
        self.network = self.project + "-integration"
        shutil.copytree(REPO / "deploy", self.deploy, ignore=shutil.ignore_patterns("private", ".env*"))
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("COMPOSE_", "DEEDKIT_"))}
        self.env.update(DEEDKIT_PUBLIC_URL=PUBLIC_URL, DEEDKIT_NETWORK=self.network)
        self.files = ["compose.gateway.yaml"]

    def docker(self, *args, check=True, timeout=180):
        result = subprocess.run(["docker", *args], cwd=self.deploy, env=self.env, text=True,
                                capture_output=True, timeout=timeout)
        assert not check or result.returncode == 0, f"docker {' '.join(args)}\n{result.stdout}\n{result.stderr}"
        return result

    def compose(self, *args, check=True, timeout=180):
        command = ["compose", "-p", self.project]
        for name in self.files:
            command += ["-f", name]
        return self.docker(*command, *args, check=check, timeout=timeout)

    def service(self):
        return json.loads(self.compose("config", "--format", "json").stdout)["services"]["gateway"]

    def policy_mount(self):
        unit = self.service()
        target = PurePosixPath(unit["command"][unit["command"].index("--policy") + 1])
        mounts = [m for m in unit["volumes"] if PurePosixPath(m["target"]) in {target, *target.parents}]
        assert len(mounts) == 1, mounts
        assert mounts[0]["type"] == "bind" and mounts[0]["read_only"] is True, mounts[0]
        return target, mounts[0]

    def policy_file(self):
        """The host file the container's `--policy` reads, through whichever read-only bind provides it."""
        target, mount = self.policy_mount()
        return Path(mount["source"]) / target.relative_to(mount["target"])

    def private_files(self):
        private = self.deploy / "private"
        for directory in (private, private / "gateway-policy", private / "gateway-keys"):
            directory.mkdir(mode=0o700)
        (private / "gateway.env").write_text("# synthetic\n")
        (private / "gateway.env").chmod(0o600)

    def override(self, volumes=None):
        lines = ["services:", "  gateway:", f"    image: {self.image}", "    ports: !reset []"]
        if os.geteuid() != 0:
            lines.append(f'    user: "{os.geteuid()}:{os.getegid()}"')
        if volumes is not None:
            lines.append("    volumes: !override")
            lines += [f"      - {value}" for value in volumes]
        (self.deploy / "compose.test-override.yaml").write_text("\n".join(lines) + "\n")
        self.files.append("compose.test-override.yaml")

    def start(self):
        self.docker("network", "create", self.network)
        self.compose("up", "--detach", "--no-build", "--pull", "never", "gateway")
        return self.container()

    def container(self):
        found = self.compose("ps", "--all", "--quiet", "gateway").stdout.split()
        assert len(found) == 1, found
        state = json.loads(self.docker("inspect", found[0]).stdout)[0]
        return state["Id"], state["State"]["StartedAt"], state["RestartCount"]

    def revision(self, container, attempts=1):
        result = None
        for _ in range(attempts):
            result = self.docker("exec", container, "python", "-c", REQUEST, KEY, check=False, timeout=30)
            if result.returncode == 0:
                return int(result.stdout.strip())
            time.sleep(1)
        raise AssertionError(f"gateway did not answer: {result and result.stdout}{result and result.stderr}\n"
                             + self.compose("logs", "gateway", check=False).stdout[-4000:])

    def close(self):
        failures = []
        down = self.compose("down", "--volumes", "--timeout", "5", check=False)
        if down.returncode:
            failures.append(down.stderr)
        network = self.docker("network", "rm", self.network, check=False)
        if network.returncode and "not found" not in network.stderr:
            failures.append(network.stderr)
        assert not failures, failures


def write(path: Path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def replace_by_rename(live: Path, value):
    """The documented procedure (services/gateway/README.md): a private staging file beside the live one,
    renamed over it."""
    staging = live.with_name(".policy.json.staging")
    write(staging, value)
    os.replace(staging, live)


@pytest.fixture(scope="module")
def gateway_image():
    if not os.environ.get("DEEDKIT_TEST_PYTHON_IMAGE"):
        pytest.skip("set DEEDKIT_TEST_PYTHON_IMAGE to run the Docker gateway policy proof")
    assert shutil.which("docker"), "Docker is required when the Docker gateway policy proof is enabled"
    tag = f"deedkit-gateway-t424:{uuid.uuid4().hex[:12]}"
    build = subprocess.run(["docker", "build", "--file", "services/gateway/Dockerfile", "--tag", tag, "."],
                           cwd=REPO, text=True, capture_output=True, timeout=900)
    assert build.returncode == 0, build.stderr[-4000:]
    try:
        yield tag
    finally:
        subprocess.run(["docker", "image", "rm", tag], capture_output=True, timeout=120)


# spec: ANYM-424
def test_policy_renamed_over_the_live_file_reaches_the_running_gateway(gateway_image):
    with tempfile.TemporaryDirectory(prefix="deedkit-t424-") as root:
        gateway = Gateway(Path(root), gateway_image)
        assert gateway.docker("compose", "version", check=False).returncode == 0, "Docker Compose v2 is required"
        gateway.private_files()
        live = gateway.policy_file()
        assert live.resolve() == (gateway.deploy / "private/gateway-policy/policy.json").resolve()
        write(live, policy(1))
        gateway.override()
        try:
            started = gateway.start()
            assert gateway.revision(started[0], attempts=60) == 1
            replace_by_rename(live, policy(2))
            assert gateway.revision(started[0]) == 2
            assert gateway.container() == started, "the gateway must not be recreated or restarted"
            target, mount = gateway.policy_mount()
            assert PurePosixPath(mount["target"]) == target.parent, (
                "the policy must be mounted through its directory, not as a single file", mount)
        finally:
            gateway.close()


# spec: ANYM-424
def test_single_file_policy_mount_never_sees_a_rename(gateway_image):
    """Diagnostic control, the pre-ANYM-424 layout: a single-file bind keeps the inode it started with, so after
    the rename the gateway answers from the old policy (Linux) or fails closed with 503 (Docker Desktop's file
    sharing); it never reports the new revision."""
    with tempfile.TemporaryDirectory(prefix="deedkit-t424-") as root:
        gateway = Gateway(Path(root), gateway_image)
        gateway.private_files()
        live = gateway.policy_file()
        write(live, policy(1))
        gateway.override(["./private/gateway-policy/policy.json:/run/deedkit/policy/policy.json:ro",
                          "./private/gateway-keys:/run/deedkit/keys:ro"])
        try:
            started = gateway.start()
            assert gateway.revision(started[0], attempts=60) == 1
            replace_by_rename(live, policy(2))
            answer = gateway.docker("exec", started[0], "python", "-c", REQUEST, KEY, check=False, timeout=30)
            assert answer.stdout.strip() in {"1", "unavailable HTTPError 503"}, answer.stdout
        finally:
            gateway.close()
