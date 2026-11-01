"""Test support: a raw ASGI client (so a test controls every body chunk, delay and disconnect), the injected
probe, config and policy builders, and a real-subprocess server for the signal tests."""

import asyncio
import base64
import hashlib
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from deedkit_inference.app import create_app
from deedkit_inference.config import Config
from deedkit_inference.policy import write_policy
from deedkit_inference.probe import BusyState

KEY = "dk-test-key-0001"
KEY2 = "dk-test-key-0002"
FAR = "2099-01-01T00:00:00Z"
PAST = "2020-01-01T00:00:00Z"
CONTRACT = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "inference_contract"
DONE = object()


def sha(key):
    return hashlib.sha256(key.encode()).hexdigest()


def fk(**directive):
    """The steering line of the fake engine, as the start of an uploaded file."""
    return b"FAKE " + json.dumps(directive).encode() + b"\n"


def principal(pid="tester", keys=((KEY, FAR),), scopes=("audio", "image", "document"), per_day=100, concurrent=4):
    return {
        "id": pid,
        "keys": [{"sha256": sha(k), "expires_at": e} for k, e in keys],
        "scopes": list(scopes),
        "max_jobs_per_day": per_day,
        "max_concurrent": concurrent,
    }


def policy_doc(principals=None, revision=1):
    return {"version": 1, "revision": revision, "principals": principals or [principal()]}


ALIASES = {
    "audio-default": {
        "kind": "audio", "scope": "audio", "engine": "fake", "timeout_seconds": 5,
        "phases": [{"name": "stt", "slot": "cpu", "resident_gb": 1.0}],
    },
    "audio-two-phase": {
        "kind": "audio", "scope": "audio", "engine": "fake", "timeout_seconds": 5,
        "phases": [
            {"name": "decode", "slot": "cpu", "resident_gb": 0.5, "share": 0.4},
            {"name": "mlx", "slot": "gpu", "resident_gb": 2.0, "share": 0.6},
        ],
    },
    "vision-default": {
        "kind": "chat", "scope": "image", "engine": "fake", "timeout_seconds": 5,
        "phases": [{"name": "vlm", "slot": "gpu", "resident_gb": 2.0}],
    },
    "document-default": {
        "kind": "chat", "scope": "document", "engine": "fake", "timeout_seconds": 5,
        "phases": [{"name": "llm", "slot": "gpu", "resident_gb": 2.0}],
    },
}


class FakeProbe:
    def __init__(self):
        self.available, self.load = 16.0, 0.0
        self.between, self.ci, self.problem, self.busy_age = False, False, None, 0.0
        self.rss_fn = lambda pgid: 0.0
        self.available_hook = None  # called on every memory read: a test makes the probe slow with it

    def available_gb(self):
        if self.available_hook is not None:
            self.available_hook()
        return self.available

    def load_1m(self):
        return self.load

    def read_busy(self):
        if self.problem:
            return BusyState(problem=self.problem)
        return BusyState(ts=time.time() - self.busy_age, between=self.between, deedkit_ci=self.ci)

    def rss_gb(self, pgid):
        return self.rss_fn(pgid)


def merge_aliases(overrides):
    result = {name: {**spec, "phases": [dict(p) for p in spec["phases"]]} for name, spec in ALIASES.items()}
    for name, patch in (overrides or {}).items():
        if patch is None:
            result.pop(name, None)
        else:
            result.setdefault(name, {}).update(patch)
    return result


@dataclass
class Stats:
    receive_calls: int = 0
    chunks_delivered: int = 0
    bytes_delivered: int = 0


@dataclass
class Response:
    status: int = 0
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    error: BaseException | None = None
    stats: Stats = field(default_factory=Stats)

    def json(self):
        return json.loads(self.body)

    @property
    def code(self):
        return self.json()["error"]["code"]


def chat_body(model="vision-default", image=None, text="describe", **extra):
    content = [{"type": "text", "text": text}]
    if image is not None:
        url = "data:image/png;base64," + base64.b64encode(image).decode()
        content.append({"type": "image_url", "image_url": {"url": url}})
    return {"model": model, "messages": [{"role": "user", "content": content}], **extra}


def multipart(fields, file=None, filename="recording.mp3", boundary="TESTBOUNDARY1234"):
    out = b""
    for name, value in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
    if file is not None:
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: audio/mpeg\r\n\r\n").encode() + file + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


class Client:
    def __init__(self, app):
        self.app = app

    async def request(self, method, path, *, headers=None, body=b"", chunks=None, content_length="auto",
                      query="", key=KEY, disconnect_after_chunks=None, disconnect_after_body=None):
        stats, response = Stats(), Response()
        response.stats = stats
        raw_headers = {k.lower(): v for k, v in (headers or {}).items()}
        if key is not None and "authorization" not in raw_headers:
            raw_headers["authorization"] = f"Bearer {key}"
        items = [body] if chunks is None else chunks
        if content_length == "auto":
            content_length = None if chunks is not None else len(body)
        if content_length is not None:
            raw_headers["content-length"] = str(content_length)
        iterator = iter(items)

        def pull():
            item = next(iterator, DONE)
            if item is DONE:
                return DONE
            return item if isinstance(item, tuple) else (0.0, item)

        pending = pull()
        state = {"pending": pending, "first": pending is DONE}
        gone = asyncio.Event()

        async def receive():
            stats.receive_calls += 1
            if gone.is_set() or (disconnect_after_chunks is not None
                                 and stats.chunks_delivered >= disconnect_after_chunks):
                return {"type": "http.disconnect"}
            if state["pending"] is not DONE:
                delay, data = state["pending"]
                state["pending"] = pull()
                if delay:
                    await asyncio.sleep(delay)
                stats.chunks_delivered += 1
                stats.bytes_delivered += len(data)
                return {"type": "http.request", "body": data, "more_body": state["pending"] is not DONE}
            if state["first"]:
                state["first"] = False
                return {"type": "http.request", "body": b"", "more_body": False}
            if disconnect_after_body is not None and not state.get("scheduled"):
                state["scheduled"] = True
                asyncio.get_running_loop().call_later(disconnect_after_body, gone.set)
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                response.status = message["status"]
                response.headers = {k.decode().lower(): v.decode() for k, v in message["headers"]}
            elif message["type"] == "http.response.body":
                response.body += message.get("body", b"")
                if not message.get("more_body", False):
                    gone.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
            "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
            "root_path": "", "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8090), "state": {},
            "headers": [(k.encode(), v.encode()) for k, v in raw_headers.items()],
        }
        try:
            await self.app(scope, receive, send)
        except Exception as error:  # a handled 500 is re-raised by the server middleware, as in uvicorn
            response.error = error
        return response

    async def get(self, path, **kw):
        return await self.request("GET", path, **kw)

    async def chat(self, body=None, *, headers=None, **kw):
        payload = json.dumps(body if body is not None else chat_body(image=fk(label="-"))).encode()
        return await self.request("POST", "/v1/chat/completions", body=payload, key=kw.pop("key", KEY),
                                  headers={"content-type": "application/json", **(headers or {})}, **kw)

    async def transcribe(self, audio=None, *, model="audio-default", filename="recording.mp3", headers=None,
                         key=KEY, extra_fields=None, **kw):
        fields = {"model": model, **(extra_fields or {})}
        body, content_type = multipart(fields, fk(label="-") if audio is None else audio, filename)
        return await self.request("POST", "/v1/audio/transcriptions", body=body, key=key,
                                  headers={"content-type": content_type, **(headers or {})}, **kw)


class Running:
    def __init__(self, app, probe, env):
        self.app, self.probe, self.env = app, probe, env
        self.service = app.state.service
        self.client = Client(app)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def work_entries(self):
        own = (".deedkit-inference-work", ".deedkit-inference-lock")
        return sorted(p.name for p in self.env.work.iterdir() if p.name not in own)

    async def until(self, predicate, timeout=5.0):
        end = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > end:
                raise AssertionError("condition not reached in time")
            await asyncio.sleep(0.01)

    def spawn(self, coro):
        return asyncio.create_task(coro)

    async def submit(self, coro, principal="tester"):
        """Start a request and wait until its job is accepted, so arrival order is the call order."""
        before = self.service.quota.used_today(principal)
        task = asyncio.create_task(coro)
        await self.until(lambda: self.service.quota.used_today(principal) > before)
        return task


class Env:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.work = tmp_path / "work"
        self.policy_path = tmp_path / "policy.json"
        self.busy_path = tmp_path / "busy.json"

    def write_policy(self, principals=None, revision=1):
        write_policy(self.policy_path, policy_doc(principals, revision))

    def config_dict(self, aliases=None, **over):
        base = {
            "version": 1, "mode": "test", "policy_file": str(self.policy_path),
            "busy_file": str(self.busy_path), "work_dir": str(self.work), "release": "rel-abc123",
            "max_wait_seconds": 50, "pump_interval_seconds": 0.02, "supervise_interval_seconds": 0.05,
            "evict_settle_seconds": 0.2, "aliases": merge_aliases(aliases),
        }
        base.update(over)
        return base

    def config(self, aliases=None, **over):
        return Config.model_validate(self.config_dict(aliases, **over))

    @asynccontextmanager
    async def running(self, *, probe=None, principals=None, revision=1, aliases=None, **over):
        if not self.policy_path.exists():
            self.write_policy(principals, revision)
        elif principals is not None:
            self.write_policy(principals, revision)
        probe = probe or FakeProbe()
        app = create_app(self.config(aliases, **over), probe)
        async with app.router.lifespan_context(app):
            yield Running(app, probe, self)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerProc:
    """`deedkit-inference serve` as a real subprocess with a test-mode config (static probe, fake engine)."""

    def __init__(self, env, **over):
        env.write_policy()
        self.env, self.port = env, free_port()
        config = env.config_dict(port=self.port, test_probe={"available_gb": 16.0, "load_1m": 0.0}, **over)
        self.config_path = env.tmp / "config.json"
        self.config_path.write_text(json.dumps(config))
        self.config_path.chmod(0o600)
        self.proc = None
        self._stop_busy = threading.Event()

    def write_busy(self):
        temp = self.env.busy_path.with_suffix(".tmp")
        temp.write_text(json.dumps({"ts": time.time(), "between": False, "deedkit_ci": False}))
        os.replace(temp, self.env.busy_path)

    def start(self):
        self.write_busy()

        def refresh():
            while not self._stop_busy.wait(1.0):
                self.write_busy()

        threading.Thread(target=refresh, daemon=True).start()
        environment = {**os.environ, "DEEDKIT_INFERENCE_TESTING": "1"}
        self.log = open(self.env.tmp / "server.log", "w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "deedkit_inference", "serve", "--config", str(self.config_path)],
            env=environment, stdout=self.log, stderr=self.log, text=True)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise AssertionError(f"server exited early: {self.proc.returncode}")
            try:
                if self.http("GET", "/health")[0] == 200:
                    return self
            except OSError:
                time.sleep(0.05)
        raise AssertionError("server did not start")

    def http(self, method, path, body=None, headers=None, key=KEY, timeout=30):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            merged = dict(headers or {})
            if key:
                merged.setdefault("Authorization", f"Bearer {key}")
            connection.request(method, path, body=body, headers=merged)
            reply = connection.getresponse()
            return reply.status, {k.lower(): v for k, v in reply.getheaders()}, reply.read()
        finally:
            connection.close()

    def chat(self, image, **kw):
        body = json.dumps(chat_body(image=image)).encode()
        return self.http("POST", "/v1/chat/completions", body, {"Content-Type": "application/json"}, **kw)

    def signal(self, sig=signal.SIGTERM):
        self.proc.send_signal(sig)

    def wait(self, timeout):
        return self.proc.wait(timeout=timeout)

    def stop(self):
        self._stop_busy.set()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        if self.proc:
            self.log.close()
