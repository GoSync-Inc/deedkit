import asyncio
import json
import ast
import os
import signal
import threading
import time
from pathlib import Path

import pytest
from deedkit_inference.engines import fake
from support import ServerProc, chat_body, fk

pytestmark = pytest.mark.anyio

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "deedkit_inference"


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def read_pids(path):
    parts = path.read_text().split() if path.exists() else []
    return [int(p) for p in parts] if len(parts) == 2 else None


def wait_dead(pids, seconds=4.0):
    end = time.monotonic() + seconds
    while any(alive(p) for p in pids) and time.monotonic() < end:
        time.sleep(0.02)
    return not any(alive(p) for p in pids)


# spec: ANYM-682
async def test_hung_engine_is_killed_and_the_next_job_runs(env):
    pidfile = env.tmp / "hang.pids"
    async with env.running(aliases={"vision-default": {"timeout_seconds": 0.6}}) as s:
        hung = s.spawn(s.chat(chat_body(image=fk(label="hung", mode="hang", pidfile=str(pidfile)))))
        await s.until(lambda: read_pids(pidfile) is not None)
        following = s.spawn(s.chat(chat_body(image=fk(label="next"))))  # waits behind it in the gpu slot
        reply = await hung
        assert reply.status == 504 and reply.code == "engine_timeout"
        assert (await following).status == 200
        assert (await s.chat(chat_body(image=fk(label="after")))).status == 200
    assert wait_dead(read_pids(pidfile))  # the child and the grandchild: the whole group
    assert [e[1] for e in fake.EVENTS if e[0] == "start"] == ["hung", "next", "after"]


# spec: ANYM-682
async def test_crashing_engine_fails_only_its_request(env):
    async with env.running() as s:
        healthy = s.spawn(s.transcribe(fk(label="healthy", sleep=0.4, estimate=0.4)))  # the other slot
        for mode in ("crash", "garbage", "exit"):
            reply = await s.chat(chat_body(image=fk(label=mode, mode=mode)))
            assert reply.status == 502 and reply.code == "engine_failed", mode
            assert ("vision-default", "vlm") not in s.service.admission.loaded  # a dead engine holds nothing
            assert set(reply.json()["error"]) == {"code", "message"}
            nxt = await s.chat(chat_body(image=fk(label=f"after-{mode}")))
            assert nxt.status == 200, mode
        assert (await healthy).status == 200
        assert s.service.scheduler.active() == 0


# spec: ANYM-682
async def test_health_answers_while_an_engine_hangs(env):
    pidfile = env.tmp / "health.pids"
    async with env.running(aliases={"vision-default": {"timeout_seconds": 30}}) as s:
        hung = s.spawn(s.chat(chat_body(image=fk(label="hung", mode="hang", pidfile=str(pidfile))),
                              disconnect_after_body=1.0))
        await s.until(lambda: read_pids(pidfile) is not None)
        for _ in range(15):
            started = time.monotonic()
            reply = await s.get("/health", key=None)
            assert reply.status == 200 and time.monotonic() - started < 0.2
            assert reply.json()["queue"]["active"] == 1 and reply.json()["status"] == "ok"
            await asyncio.sleep(0.03)
        assert (await hung).status == 499  # ended by the client going away; the child group is killed
    assert wait_dead(read_pids(pidfile))


def post_in_thread(server, label, seconds, results, **directive):
    def run():
        try:
            results[label] = server.chat(fk(label=label, sleep=seconds, estimate=seconds, **directive))
        except OSError as error:
            results[label] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def draining_seen(server, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        status, _, body = server.http("GET", "/health", key=None)
        if b'"draining"' in body:
            return body
        time.sleep(0.05)
    raise AssertionError("never saw draining")


# spec: ANYM-682
def test_sigterm_finishes_active_and_queued_jobs_then_exits(env):
    server = ServerProc(env)
    server.start()
    try:
        results, threads = {}, []
        for label in "ABC":
            threads.append(post_in_thread(server, label, 0.7, results))
            time.sleep(0.15)
        end = time.monotonic() + 10
        while time.monotonic() < end:
            queue = json.loads(server.http("GET", "/health", key=None)[2])["queue"]
            if queue["depth"] == 2 and queue["active"] == 1:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("queue never filled")
        server.signal(signal.SIGTERM)
        body = draining_seen(server)
        assert b'"admission":"closed"' in body.replace(b" ", b"")
        for thread in threads:
            thread.join(timeout=60)
        assert {label: results[label][0] for label in "ABC"} == {"A": 200, "B": 200, "C": 200}
        assert server.wait(30) == 0  # drained: queue and running jobs both empty
    finally:
        server.stop()


# spec: ANYM-682
def test_arrivals_after_sigterm_are_503_draining(env):
    server = ServerProc(env)
    server.start()
    try:
        results = {}
        thread = post_in_thread(server, "A", 1.5, results)
        end = time.monotonic() + 10
        while json.loads(server.http("GET", "/health", key=None)[2])["queue"]["active"] != 1:
            assert time.monotonic() < end
            time.sleep(0.05)
        server.signal(signal.SIGTERM)
        draining_seen(server)
        status, headers, body = server.chat(fk(label="late"))
        error = json.loads(body)
        assert status == 503 and error == {"error": {"code": "draining", "message": error["error"]["message"]}}
        assert int(headers["retry-after"]) >= 1
        assert server.http("GET", "/v1/models")[0] == 200  # reads still answer
        assert json.loads(server.http("GET", "/health", key=None)[2])["status"] == "draining"
        thread.join(timeout=60)
        assert results["A"][0] == 200  # the job that was running was not cut
        assert server.wait(30) == 0
    finally:
        server.stop()


# spec: ANYM-682
def test_drain_timeout_kills_the_rest(env):
    pidfile = env.tmp / "drain.pids"
    server = ServerProc(env, drain_seconds=1.0, aliases={"vision-default": {"timeout_seconds": 60}})
    server.start()
    try:
        results = {}
        thread = post_in_thread(server, "stuck", 30, results, pidfile=str(pidfile))
        end = time.monotonic() + 10
        while read_pids(pidfile) is None:
            assert time.monotonic() < end
            time.sleep(0.05)
        started = time.monotonic()
        server.signal(signal.SIGTERM)
        code = server.wait(30)
        assert code == 3 and time.monotonic() - started < 15  # killed after drain_seconds, exits non-zero
        assert wait_dead(read_pids(pidfile))
        thread.join(timeout=30)
    finally:
        server.stop()


# spec: ANYM-682
def test_service_imports_no_outbound_http_client():
    forbidden = {"httpx", "httpx2", "requests", "aiohttp", "urllib3", "httpcore", "http.client", "urllib.request",
                 "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc", "websockets", "websocket", "grpc",
                 "pycurl", "treq", "socket"}
    seen = []
    # ANYM-683: the three modules that exist to talk to the network are never imported by the serving path:
    # the model download (`pull`), the canary client and the egress probe. `cli.py` loads them inside commands only.
    outside = {"pull.py", "canary.py", "egress_probe.py"}
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text())
        if path.name in outside:
            continue
        top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        assert not [n for n in top if isinstance(n, ast.ImportFrom) and n.level == 1
                    and (n.module or "").split(".")[0] in {"pull", "canary", "egress_probe"}], path.name
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
            elif isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("__import__", "import_module"):
                names = [ast.unparse(node)]
            for name in names:
                if any(name == bad or name.startswith(bad + ".") for bad in forbidden):
                    seen.append((path.name, name))
    assert seen == []
    assert len(list(PACKAGE.rglob("*.py"))) >= 14  # the scan really looked at the package
    pyproject = (PACKAGE.parents[1] / "pyproject.toml").read_text()
    assert "httpx" not in pyproject.split("[dependency-groups]")[0]
