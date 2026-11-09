"""The HTTP API. The principal comes from the caller key (its SHA-256 in the private config), never from a request
field; a task is visible only to its own principal's keys."""

import hashlib
import hmac
import ipaddress
import json
import uuid
from typing import Annotated, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from .runner import Refused, capabilities, log_page, view

BODY_LIMIT = 64 * 1024


class TaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine: Literal["claude", "codex"]
    instruction: Annotated[str, Field(min_length=1, max_length=8000)]
    owner_instruction: Annotated[str, Field(min_length=3, max_length=1500)]
    request_id: uuid.UUID
    max_minutes: Annotated[int, Field(ge=5, le=120, strict=True)] = 45
    continue_task_id: uuid.UUID | None = None

    def fields(self):
        return {"engine": self.engine, "instruction": self.instruction, "owner_instruction": self.owner_instruction,
                "request_id": str(self.request_id), "max_minutes": self.max_minutes,
                "continue_task_id": str(self.continue_task_id) if self.continue_task_id else None}


def error(status, code, **extra):
    return JSONResponse({"error": code, **extra}, status_code=status)


class PeerGuard:
    """Answers only connections whose peer address is in `allowed`; everything else gets 403 and nothing runs."""

    def __init__(self, app, allowed):
        self.app, self.allowed = app, {ipaddress.ip_address(a) for a in allowed}

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            try:
                peer = ipaddress.ip_address(client[0]) if client else None
            except ValueError:
                peer = None
            if peer not in self.allowed:
                await error(403, "peer_refused")(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(runner, keys):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def caller(request, operation):
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer ") or len(header) > 600:
            return None, error(401, "unauthorized")
        digest = hashlib.sha256(header[7:].strip().encode()).hexdigest()
        key = keys.get(digest)
        if key is None or key.revoked or not hmac.compare_digest(key.sha256, digest):
            return None, error(401, "unauthorized")
        if operation not in key.operations:
            return None, error(403, "forbidden")
        return key, None

    def own(task_id, key):
        try:
            uuid.UUID(task_id)
        except ValueError:
            return None
        return runner.store.get(task_id, key.principal)

    @app.post("/v1/tasks")
    async def start(request: Request):
        key, refused = caller(request, "run")
        if key is None:
            return refused
        body = await request.body()
        if len(body) > BODY_LIMIT:
            return error(413, "request_too_large")
        try:
            task_in = TaskIn.model_validate_json(body)
        except ValidationError as failure:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in failure.errors()})
            return error(422, "invalid_request", fields=fields)
        try:
            task, created = await run_in_threadpool(runner.submit, key.principal, key.key_id, task_in.fields())
        except Refused as refusal:
            return error(refusal.status, refusal.code, **refusal.extra)
        return JSONResponse(view(task, runner.clock, await run_in_threadpool(runner.store.ahead, task)), status_code=201 if created else 200)

    @app.get("/v1/tasks")
    async def by_request(request: Request, request_id: str = ""):
        key, refused = caller(request, "read")
        if key is None:
            return refused
        try:
            request_id = str(uuid.UUID(request_id))
        except ValueError:
            return error(422, "invalid_request", fields=["request_id"])
        task = await run_in_threadpool(runner.store.by_request, key.principal, request_id)
        return JSONResponse(view(task, runner.clock, await run_in_threadpool(runner.store.ahead, task))) if task else error(404, "not_found")

    @app.get("/v1/tasks/{task_id}")
    async def get(request: Request, task_id: str):
        key, refused = caller(request, "read")
        if key is None:
            return refused
        task = await run_in_threadpool(own, task_id, key)
        return JSONResponse(view(task, runner.clock, await run_in_threadpool(runner.store.ahead, task))) if task else error(404, "not_found")

    @app.get("/v1/tasks/{task_id}/log")
    async def task_log(request: Request, task_id: str, cursor: str = "", raw: bool = False):
        key, refused = caller(request, "read")
        if key is None:
            return refused
        task = await run_in_threadpool(own, task_id, key)
        if not task:
            return error(404, "not_found")
        result = await run_in_threadpool(log_page, runner, task["id"], cursor, raw)
        return JSONResponse(result) if result is not None else error(422, "invalid_request", fields=["cursor"])

    @app.post("/v1/tasks/{task_id}/stop")
    async def stop(request: Request, task_id: str):
        key, refused = caller(request, "run")
        if key is None:
            return refused
        task = await run_in_threadpool(own, task_id, key)
        if not task:
            return error(404, "not_found")
        task = await run_in_threadpool(runner.stop, task["id"], key.key_id)
        return JSONResponse(view(task, runner.clock, await run_in_threadpool(runner.store.ahead, task)))

    @app.get("/v1/capabilities")
    async def caps(request: Request):
        key, refused = caller(request, "read")
        if key is None:
            return refused
        return JSONResponse(json.loads(json.dumps(await run_in_threadpool(capabilities, runner))))

    return app
