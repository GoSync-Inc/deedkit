"""Real HTTP release-router proof with isolated, synthetic backends only.

Set DEEDKIT_TEST_ROUTER_IMAGE to the immutable image built from this checkout.
When enabled, missing Go/Docker/images are failures, including in CI. The proof
client runs inside Linux so Docker Desktop does not need to expose a Unix socket
to the macOS host. No host ports, credentials or provider sessions are used.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from unittest.mock import patch
import uuid

import pytest

from scripts import seamless_release as release


PYTHON_IMAGE = "docker.io/library/python@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641"
GENERATION = "123456abcdef"
RETAINED = "fedcba654321"
TOKEN = "a" * 43
STREAM_TOKEN = "s" * 43
BACKEND = r'''
import http.server, json, pathlib, sys, time
label, port = sys.argv[1:]
root = pathlib.Path('/proof')
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass
    def do_GET(self):
        if self.path in {'/stream', '/v1/files/' + 's'*43}:
            chunks = [(label + ':first\n').encode(), (label + ':last\n').encode()]
            self.send_response(200)
            self.send_header('Content-Length', str(sum(map(len, chunks))))
            self.end_headers()
            self.wfile.write(chunks[0]); self.wfile.flush()
            (root / 'stream-started').write_text(label)
            deadline = time.monotonic() + 45
            while not (root / 'finish-stream').exists():
                if time.monotonic() >= deadline:
                    return
                time.sleep(.02)
            self.wfile.write(chunks[1]); self.wfile.flush()
            return
        body = json.dumps({'backend': label, 'path': self.path,
                           'host': self.headers.get('Host')}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)
http.server.ThreadingHTTPServer(('0.0.0.0', int(port)), Handler).serve_forever()
'''
CLIENT = r'''
import http.client, json, pathlib, socket, sys, time
sys.path.insert(0, '/proof/source')
from scripts import seamless_release as release
settings = {'http_release': {'router': {'admin_socket': '/run/deedkit-router/admin.sock'}}}
operation = sys.argv[1]
if operation == 'get':
    value, etag = release.router_get(settings)
    print(json.dumps({'value': value, 'etag': etag}))
elif operation == 'cas':
    value = json.loads(pathlib.Path('/proof/next.json').read_text())
    release.router_cas(settings, value, sys.argv[2])
    print(json.dumps({'ok': True}))
elif operation == 'inflight':
    print(json.dumps({'count': release.router_inflight(settings, sys.argv[2], sys.argv[3])}))
elif operation == 'fence':
    value = release.router_fence(settings, sys.argv[2], sys.argv[3])
    _, etag = release.router_get(settings)
    print(json.dumps({'value': value, 'etag': etag}))
elif operation == 'status':
    value, _ = release.router_request(settings, 'GET', path='/status')
    print(json.dumps(value))
elif operation == 'persist':
    value = json.loads(pathlib.Path('/proof/persist.json').read_text())
    pathlib.Path('/run/deedkit-router').chmod(0o700)
    release.durable_write('/run/deedkit-router/router.json', value)
    print(json.dumps({'ok': True}))
elif operation == 'persisted':
    print(pathlib.Path('/run/deedkit-router/router.json').read_text())
elif operation == 'stale':
    class UnixHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.settimeout(5)
            self.sock.connect(settings['http_release']['router']['admin_socket'])
    connection = UnixHTTP('127.0.0.1', timeout=5)
    body = pathlib.Path('/proof/stale.json').read_text()
    connection.request('POST', '/config/', body,
                       {'Content-Type': 'application/json', 'If-Match': sys.argv[2]})
    response = connection.getresponse(); response.read()
    print(json.dumps({'status': response.status})); connection.close()
elif operation == 'request':
    connection = http.client.HTTPConnection('release-router', 8080, timeout=45)
    connection.request('GET', sys.argv[2], headers={'Host': 'deedkit.synthetic:443'})
    response = connection.getresponse()
    print(json.dumps({'status': response.status, 'body': response.read().decode()}))
    connection.close()
elif operation == 'slow-headers':
    connection = socket.create_connection(('release-router', 8080), timeout=25)
    connection.sendall(b'GET /deedkit/tools HTTP/1.1\r\nHost: deedkit.synthetic:443\r\n')
    root = pathlib.Path('/proof')
    (root / 'headers-started').touch()
    deadline = time.monotonic() + 20
    while not (root / 'finish-headers').exists():
        if time.monotonic() >= deadline:
            raise AssertionError('headers were never released')
        time.sleep(.02)
    connection.sendall(b'Connection: close\r\n\r\n')
    response = http.client.HTTPResponse(connection); response.begin()
    print(json.dumps({'status': response.status, 'body': response.read().decode()}))
    connection.close()
else:
    raise AssertionError(operation)
'''


class RouterProof:
    def __init__(self, directory, image):
        self.directory = directory.resolve()
        directory = self.directory
        self.image = image
        self.prefix = "dk-router-proof-" + uuid.uuid4().hex[:12]
        self.network = self.prefix + "-net"
        self.router = self.prefix + "-router"
        self.client = self.prefix + "-old-gateway"
        self.containers = []
        self.network_created = False
        self.volume = self.prefix + "-socket"
        self.volume_created = False
        self.native_bind = sys.platform != "darwin"
        self.client_started = False
        self.settings = {"version": 1, "state_directory": str(directory / "state"),
            "projects": [
                {"name": "messages", "services": {"api": "runtime"}},
                {"name": "gateway", "services": {"gateway": "gateway"}},
            ], "http_release": {
            "api": {"project": "messages", "service": "api"},
            "gateway": {"project": "gateway", "service": "gateway"},
            "gateway_policy": str(directory / "policy.json"),
            "gateway_public_url": "https://deedkit.synthetic/deedkit",
            "router": {"listen": 8080, "admin_socket": str(directory / "router" / "admin.sock"),
                       "directory": str(directory / "router"),
                       "config_file": str(directory / "router" / "router.json"),
                       "container": self.router, "network": self.network,
                       "image": "sha256:" + image.rsplit("sha256:", 1)[1]},
            "legacy": {"api": "legacy-api:8080", "gateway": "legacy-gateway:13240"},
        }}

    def router_mount(self):
        if self.native_bind:
            return f"type=bind,source={self.directory / 'router'},target=/run/deedkit-router"
        return f"type=volume,source={self.volume},target=/run/deedkit-router"

    def fixture_user(self):
        # Linux CI keeps the private bind directory owned by its runner. A
        # Docker Desktop volume is Linux-owned and must use its default root.
        return ["--user", f"{os.getuid()}:{os.getgid()}"] if self.native_bind else []

    @staticmethod
    def docker(*arguments, check=True):
        result = subprocess.run(["docker", *map(str, arguments)], capture_output=True,
                                text=True, timeout=45)
        if check:
            assert result.returncode == 0, result.stderr or result.stdout
        return result

    def start(self):
        self.docker("info", "--format", "{{.ServerVersion}}")
        for image in [self.image, PYTHON_IMAGE]:
            self.docker("image", "inspect", image)
        metadata = json.loads(self.docker("image", "inspect", self.image).stdout)[0]
        sha = metadata.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision")
        assert isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha), \
            "integration router image needs its tested checkout revision label"
        self.settings["http_release"]["router"]["image"] = metadata["Id"]
        self.directory.mkdir(exist_ok=True)
        (self.directory / "router").mkdir(mode=0o700)
        (self.directory / "state").mkdir(mode=0o700)
        (self.directory / "policy.json").write_text('{}')
        (self.directory / "policy.json").chmod(0o600)
        scripts = self.directory / "source" / "scripts"
        scripts.mkdir(parents=True)
        source = Path(release.__file__).parent
        for name in ["seamless_release.py", "production.py"]:
            shutil.copyfile(source / name, scripts / name)
        (self.directory / "backend.py").write_text(BACKEND)
        (self.directory / "client.py").write_text(CLIENT)
        build = self.directory / "state" / ("router-" + sha)
        build.mkdir(mode=0o700)
        # This is a synthetic bootstrap receipt for the local test image. It
        # grants no production authority; real image identity/labels are checked.
        release.durable_write(build / "router-build.json", {
            "version": 1, "target": "http-router", "sha": sha,
            "image": metadata["Id"], "ci": {"sha": sha, "ci": "synthetic"},
        })
        # Only the main membership and attestation lookup is synthetic. Bootstrap
        # still checks the actual immutable image, revision/source labels and generated unit.
        with patch.object(release.p, "promotable", return_value={"sha": sha, "ci": "synthetic"}):
            artifact = release.bootstrap(self.settings, sha)
        unit = json.loads(Path(artifact["compose"]).read_text())["services"]["router"]
        assert not unit.get("ports")
        assert unit["entrypoint"] == ["/deedkit-release-router"]
        assert unit["read_only"] is True
        assert unit["cap_drop"] == ["ALL"]
        assert unit["security_opt"] == ["no-new-privileges:true"]
        self.docker("network", "create", "--internal", self.network)
        self.network_created = True
        if not self.native_bind:
            self.docker("volume", "create", self.volume)
            self.volume_created = True
        backends = [
            ("old-gateway", "13240", ["legacy-gateway", "http-gateway-" + RETAINED]),
            ("old-api", "8080", ["legacy-api", "http-api-" + RETAINED]),
            ("new-gateway", "13240", ["http-gateway-" + GENERATION]),
            ("new-api", "8080", ["http-api-" + GENERATION]),
        ]
        for label, port, aliases in backends:
            name = self.prefix + "-" + label
            self.containers.append(name)
            self.docker("run", "--detach", "--pull=never", "--name", name,
                        "--network", self.network,
                        *[option for alias in aliases for option in ["--network-alias", alias]],
                        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                        "--memory=64m", "--cpus=0.5", "--pids-limit=32",
                        *self.fixture_user(),
                        "--mount", f"type=bind,source={self.directory},target=/proof",
                        "--mount", self.router_mount(),
                        PYTHON_IMAGE, "python", "-B", "/proof/backend.py", label, port)
        self.client_started = True
        self.persist(self.document())
        self.containers.append(self.router)
        self.docker("run", "--detach", "--pull=never", "--name", self.router,
                    "--network", self.network, "--network-alias", "release-router",
                    "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
                    *["--cap-add=" + cap for cap in unit.get("cap_add", [])],
                    *["--tmpfs=" + item for item in unit.get("tmpfs", [])],
                    "--memory=128m", "--cpus=1", "--pids-limit=64",
                    *self.fixture_user(), "--entrypoint=" + unit["entrypoint"][0],
                    "--mount", self.router_mount(), unit["image"], *unit["command"])
        self.wait_ready()
        if self.native_bind:
            assert release.router_identity(self.settings)

    def document(self, generation=None, retained=None):
        return release.route_document(self.settings, generation, retained)

    def persist(self, document):
        if self.client_started:
            (self.directory / "persist.json").write_text(json.dumps(document))
            self.client_call("persist")
        else:
            release.durable_write(self.directory / "router" / "router.json", document)

    def client_call(self, *arguments, check=True):
        result = self.docker("exec", self.client, "python", "-B", "/proof/client.py", *arguments,
                             check=check)
        return json.loads(result.stdout) if check else result

    def wait_ready(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = self.client_call("get", check=False)
            if result.returncode == 0:
                result = self.client_call("request", "/deedkit/healthz", check=False)
                if result.returncode == 0 and json.loads(result.stdout)["status"] == 200:
                    return
            time.sleep(.05)
        logs = self.docker("logs", "--tail", "8", self.router)
        pytest.fail("isolated router did not become ready: " + logs.stderr + result.stderr)

    def get(self):
        return self.client_call("get")

    def switch(self, document, etag=None):
        (self.directory / "next.json").write_text(json.dumps(document))
        self.client_call("cas", etag or self.get()["etag"])

    def request(self, path):
        result = self.client_call("request", path)
        assert result["status"] == 200, result
        return json.loads(result["body"])

    def close(self):
        # Only uniquely owned objects are touched, even when startup failed.
        failures = []
        for name in reversed(self.containers):
            self.docker("stop", "--time", "3", name, check=False)
            result = self.docker("rm", "--force", "--volumes", name, check=False)
            if result.returncode and "No such container" not in result.stderr:
                failures.append(result.stderr)
        if self.network_created:
            result = self.docker("network", "rm", self.network, check=False)
            if result.returncode:
                failures.append(result.stderr)
        if self.volume_created:
            result = self.docker("volume", "rm", self.volume, check=False)
            if result.returncode:
                failures.append(result.stderr)
        assert not failures, failures


@pytest.fixture(scope="module")
def real_router(tmp_path_factory):
    image = os.environ.get("DEEDKIT_TEST_ROUTER_IMAGE")
    if not image:
        pytest.skip("set DEEDKIT_TEST_ROUTER_IMAGE for real router integration proof")
    assert re.search(r"(?:@|^)sha256:[0-9a-f]{64}$", image), "router image must be immutable"
    assert shutil.which("docker"), "Docker is required when real router proof is enabled"
    proof = RouterProof(tmp_path_factory.mktemp("release-router"), image)
    try:
        proof.start()
        yield proof
    finally:
        proof.close()


@pytest.fixture
def router(real_router):
    baseline = real_router.document()
    # Each case is an independent synthetic deployment. Restarting its isolated
    # fixture resets history; real CAS must never delete or reopen tombstones.
    real_router.persist(baseline)
    real_router.docker("restart", "--time", "3", real_router.router)
    real_router.wait_ready()
    return real_router


def test_generated_router_validates_and_preserves_host_and_ticket_issuers(router):
    document = router.document(GENERATION, [RETAINED])
    assert router.client_call("status") == {"protocol": 1, "kind": "deedkit-release-router"}
    assert router.request("/deedkit/tools")["backend"] == "old-gateway"
    router.switch(document)
    cases = [
        ("/deedkit/tools?limit=2", "new-gateway", "/tools?limit=2"),
        ("/pos-probe/healthz", "new-api", "/healthz"),
        (f"/deedkit/v1/files/{TOKEN}", "old-gateway", f"/v1/files/{TOKEN}"),
        (f"/deedkit/_releases/{GENERATION}/v1/files/{TOKEN}", "new-gateway", f"/v1/files/{TOKEN}"),
        (f"/deedkit/_releases/{RETAINED}/v1/files/{TOKEN}", "old-gateway", f"/v1/files/{TOKEN}"),
    ]
    for path, backend, stripped in cases:
        assert router.request(path) == {"backend": backend, "path": stripped,
                                        "host": "deedkit.synthetic:443"}
    for path in [f"/deedkit/_releases/aaaaaaaaaaaa/v1/files/{TOKEN}",
                 f"/deedkit/_releases/{GENERATION}/v1/files/invalid",
                 f"/deedkit/v1/files/{TOKEN}/extra"]:
        assert router.client_call("request", path)["status"] == 404
    for role in ["api", "gateway"]:
        assert router.client_call("inflight", RETAINED, role)["count"] == 0


def test_router_rejects_stale_etag_with_412_and_keeps_current_routes(router):
    baseline = router.get()
    promoted = router.document(GENERATION)
    router.switch(promoted, baseline["etag"])
    router.persist(promoted)
    current = router.get()
    source = router.client_call("persisted")
    assert current["etag"] != baseline["etag"]
    (router.directory / "stale.json").write_text(json.dumps(baseline["value"]))
    assert router.client_call("stale", baseline["etag"])["status"] == 412
    assert router.get() == current
    assert router.client_call("persisted") == source
    assert router.request("/deedkit/tools")["backend"] == "new-gateway"
    rejected = router.client_call("cas", baseline["etag"], check=False)
    assert rejected.returncode != 0
    assert "Refused" in rejected.stderr
    assert router.get() == current
    assert router.client_call("persisted") == source


def test_persisted_router_configuration_survives_container_restart(router):
    promoted = router.document(GENERATION, [RETAINED])
    router.switch(promoted)
    router.persist(promoted)
    router.docker("restart", "--time", "3", router.router)
    router.wait_ready()
    assert router.get()["value"] == promoted
    assert router.request("/deedkit/tools")["backend"] == "new-gateway"
    assert router.request(f"/deedkit/v1/files/{TOKEN}")["backend"] == "old-gateway"


def test_router_recovers_private_socket_after_abrupt_restart(router):
    promoted = router.document(GENERATION, [RETAINED])
    router.switch(promoted)
    router.persist(promoted)
    router.docker("kill", "--signal", "KILL", router.router)
    router.docker("start", router.router)
    router.wait_ready()
    assert router.get()["value"] == promoted
    assert router.request("/deedkit/tools")["backend"] == "new-gateway"


def test_active_http_stream_finishes_on_issuer_during_route_switch(router):
    router.switch(router.document(RETAINED))
    (router.directory / "stream-started").unlink(missing_ok=True)
    finish = router.directory / "finish-stream"
    finish.unlink(missing_ok=True)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(router.client_call, "request", "/deedkit/stream")
        try:
            deadline = time.monotonic() + 10
            while not (router.directory / "stream-started").exists():
                assert not future.done(), "stream ended before the switch"
                assert time.monotonic() < deadline, "synthetic stream did not start"
                time.sleep(.02)
            assert (router.directory / "stream-started").read_text() == "old-gateway"
            assert router.client_call("inflight", RETAINED, "gateway")["count"] == 1
            router.switch(router.document(GENERATION, [RETAINED]))
            assert router.request("/deedkit/tools")["backend"] == "new-gateway"
            assert not future.done(), "routing switch prematurely finished the old stream"
        finally:
            finish.touch()
        assert future.result(timeout=10) == {
            "status": 200, "body": "old-gateway:first\nold-gateway:last\n"}
    assert router.client_call("inflight", RETAINED, "gateway")["count"] == 0


def test_slow_headers_finish_after_switch_using_current_generation(router):
    (router.directory / "headers-started").unlink(missing_ok=True)
    finish = router.directory / "finish-headers"
    finish.unlink(missing_ok=True)
    router.switch(router.document(RETAINED))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(router.client_call, "slow-headers")
        try:
            deadline = time.monotonic() + 10
            while not (router.directory / "headers-started").exists():
                assert not future.done(), "partial headers unexpectedly completed"
                assert time.monotonic() < deadline, "partial request did not start"
                time.sleep(.02)
            # TCP was established before CAS, but admission happens only once
            # the request headers finish. No retired server may own this socket.
            router.switch(router.document(GENERATION, [RETAINED]))
            assert not future.done()
        finally:
            finish.touch()
        response = future.result(timeout=10)
    assert response["status"] == 200
    assert json.loads(response["body"]) == {
        "backend": "new-gateway", "path": "/tools", "host": "deedkit.synthetic:443"}
    assert router.client_call("inflight", RETAINED, "gateway")["count"] == 0


def test_retirement_fence_blocks_new_issuer_requests_until_existing_lease_finishes(router):
    router.switch(router.document(RETAINED))
    (router.directory / "stream-started").unlink(missing_ok=True)
    finish = router.directory / "finish-stream"
    finish.unlink(missing_ok=True)
    path = f"/deedkit/_releases/{RETAINED}/v1/files/{STREAM_TOKEN}"
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(router.client_call, "request", path)
        try:
            deadline = time.monotonic() + 10
            while not (router.directory / "stream-started").exists():
                assert not future.done(), "issuer stream ended before retirement"
                assert time.monotonic() < deadline, "issuer stream did not start"
                time.sleep(.02)
            router.switch(router.document(GENERATION, [RETAINED]))
            before = router.get()
            fenced = router.client_call("fence", RETAINED, before["etag"])
            assert fenced["etag"] != before["etag"]
            assert router.get()["value"]["generations"][RETAINED]["admit_files"] is False
            assert router.client_call("inflight", RETAINED, "gateway")["count"] == 1
            assert router.client_call("request", path)["status"] == 410
            # Neither reopening a fence nor deleting its tombstone is allowed.
            for invalid in [before["value"], router.document(GENERATION)]:
                (router.directory / "stale.json").write_text(json.dumps(invalid))
                assert router.client_call("stale", fenced["etag"])["status"] == 409
            assert router.request("/deedkit/tools")["backend"] == "new-gateway"
            assert not future.done(), "fencing must retain the already admitted transfer"
        finally:
            finish.touch()
        assert future.result(timeout=10) == {
            "status": 200, "body": "old-gateway:first\nold-gateway:last\n"}
    assert router.client_call("inflight", RETAINED, "gateway")["count"] == 0


def test_router_go_contract():
    if not os.environ.get("DEEDKIT_TEST_ROUTER_IMAGE"):
        pytest.skip("set DEEDKIT_TEST_ROUTER_IMAGE for the release-router Go race contract")
    assert shutil.which("go"), "Go is required when router verification is enabled"
    source = Path(__file__).resolve().parents[1] / "services" / "release-router"
    result = subprocess.run(["go", "test", "-race", "-count=1", "./..."], cwd=source,
                            text=True, capture_output=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
