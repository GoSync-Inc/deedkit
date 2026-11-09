"""The HTTP surface: /v1 routes, the error envelope, one log line per request."""

import asyncio
import base64
import binascii
import json
import logging
import math
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse

from .admission import Admission
from .config import resolve_release, work_path
from .engines import EngineEnv, JobInput, Rejected, build_engine
from .errors import ConfigError, ServiceError
from . import pausestate
from .models import ModelError, load_models, phase_key
from .policy import PolicyStore, bearer
from .probe import StaticProbe, SystemProbe
from .quota import Quota
from .scheduler import Job, Scheduler
from .schemas import (DOCUMENT_MAX_BYTES, ChatRequest, DocumentForm, TranscriptionForm, audio_response, chat_response,
                      document_response)
from .uploads import read_audio_form, read_document_form, stream_to_file
from .yielder import Yielder, YieldSettings
from .workdir import WorkDir

ACCESS = logging.getLogger("deedkit_inference.access")
MAX_DEADLINE_SECONDS = 86400.0
POLL_DISCONNECT_SECONDS = 0.25
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


def invalid(message):
    return ServiceError(422, "invalid_request", message)


def validation_message(error):
    """Locations and error types only: pydantic's own text can carry the input value."""
    first = error.errors(include_input=False, include_url=False)[0]
    return "invalid field " + ".".join(str(part) for part in first["loc"]) + ": " + first["type"]


class Context:
    """What one request tells the log line, and nothing else."""

    def __init__(self):
        self.rid = uuid.uuid4().hex[:16]
        self.principal = "-"
        self.alias = "-"
        self.bytes_in = 0
        self.started = time.monotonic()
        self.refusal = "-"
        self.job = None  # the Job this request made, once it exists: the response reports what yielding did to it


class Reservation:
    """Everything a request holds before and while its job runs; `close` gives all of it back."""

    def __init__(self, service, auth, declared, factor):
        self.service, self.auth = service, auth
        self.declared, self.factor = declared, factor
        self.pool_held = declared
        self.work_held = declared * factor
        self.directory = None
        self.closed = False

    def make_directory(self):
        self.directory = self.service.workdir.new_request_dir()
        return self.directory

    def upload_done(self):
        self.service.pool_used -= self.pool_held
        self.pool_held = 0

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.service.pool_used -= self.pool_held
        self.service.workdir.release(self.work_held)
        self.service.quota.leave(self.auth.id)
        if self.directory is not None:
            self.service.workdir.remove(self.directory)


class Service:
    def __init__(self, config, probe=None, wall=time.time, yield_settings=None, sampler=None):
        self.config = config
        self.release = resolve_release(config)
        if probe is None:
            if config.test_probe is not None:
                probe = StaticProbe(config.busy_file, config.test_probe.available_gb, config.test_probe.load_1m)
            else:
                probe = SystemProbe(config.busy_file)
        self.probe = probe
        self.policy = PolicyStore(config.policy_file)
        self.policy.load_initial()
        self.quota = Quota(config.usage_file)
        self.workdir = WorkDir(work_path(config), config.work_dir_max_bytes)
        self.workdir.prepare()
        self.engines = {}
        self.models = None
        if any(spec.engine != "fake" for spec in config.aliases.values()):
            try:
                self.models = load_models(config.models_file)
            except (ModelError, OSError) as error:
                raise ConfigError(error.code if isinstance(error, ModelError) else "models_unreadable") from None
        for alias, spec in config.aliases.items():
            env = EngineEnv(alias, spec, Path(config.model_dir), self.models, work_path(config), config.mode == "test")
            engine = build_engine(spec, env)
            if engine is None:
                raise ConfigError(f"aliases.{alias}.engine: unknown_engine")
            if getattr(engine, "worker", False):
                for phase in spec.phases:
                    if phase_key(alias, phase.name) not in self.models["phases"]:
                        raise ConfigError(f"aliases.{alias}.phases.{phase.name}: model_not_pinned")
            self.engines[alias] = engine
        self.admission = Admission(config, probe, wall=wall)
        self.scheduler = Scheduler(config, self.admission, probe)
        # Yielding needs the ledger (a stop is never made without one), which lives in the state directory.
        self.yielder = None
        if config.usage_file:
            ledger = pausestate.Ledger(pausestate.ledger_path(Path(config.usage_file).parent), uuid.uuid4().hex)
            self.yielder = Yielder(self.scheduler, yield_settings or YieldSettings(), probe, ledger, sampler=sampler,
                                   wall=wall)
        self.draining = False
        self.pool_used = 0
        self.jobs_inflight = 0

    # --- shutdown ------------------------------------------------------------------------------------------------
    async def drain(self):
        """Close admission, finish every queued and running job and every response, or give up at the
        deadline and kill what is left. True when everything finished."""
        self.draining = True
        self.scheduler.draining = True
        if self.yielder is not None:
            self.yielder.disable()  # a stopped phase never finishes: the drain would wait out drain_seconds
        deadline = time.monotonic() + self.config.drain_seconds
        while (not self.scheduler.idle() or self.jobs_inflight > 0) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        clean = self.scheduler.idle() and self.jobs_inflight == 0
        if not clean:
            self.scheduler.kill_all()
            await asyncio.sleep(0.2)
        return clean

    # --- the pieces of a request --------------------------------------------------------------------------------
    def authenticate(self, request):
        auth = self.policy.authenticate(bearer(request.headers.get("authorization")))
        if auth is None:
            raise ServiceError(401, "unauthorized", "missing or invalid key", close=True)
        return auth

    def preflight(self, request, ctx, kind):
        """Everything that may be decided from the headers, before one body byte is read."""
        cfg = self.config
        if self.draining:
            raise ServiceError(503, "draining", "the service is shutting down",
                               retry_after=cfg.retry_after_seconds, close=True)
        auth = self.authenticate(request)
        ctx.principal = auth.id
        wanted = {"audio": {"audio"}, "document": {"document"}}.get(kind, {"image", "document"})
        if not auth.scopes & wanted:
            raise ServiceError(403, "scope_denied", "the key has no scope for this route", close=True)
        deadline = parse_deadline(request)
        request.state.deadline = deadline
        limit = {"audio": cfg.audio_body_max_bytes, "document": cfg.document_body_max_bytes}.get(
            kind, cfg.chat_body_max_bytes)
        declared = parse_length(request, limit)
        principal = auth.principal
        if self.quota.concurrency_exhausted(principal):
            raise ServiceError(429, "concurrency_exceeded", "too many requests in flight for this key",
                               retry_after=cfg.retry_after_seconds, close=True)
        if self.quota.daily_exhausted(principal):
            raise ServiceError(429, "quota_exceeded", "the daily job limit is reached",
                               retry_after=Scheduler.until_utc_midnight(), close=True)
        factor = 2 if kind == "chat" else 1
        full = ServiceError(503, "upload_capacity", "too many uploads in flight",
                            retry_after=cfg.retry_after_seconds, close=True)
        if self.pool_used + declared > cfg.max_inflight_upload_bytes:
            raise full
        if not self.workdir.reserve(declared * factor):
            raise full
        self.pool_used += declared
        self.quota.enter(auth.id)
        return Reservation(self, auth, declared, factor), limit, declared

    def resolve_alias(self, name, kind, auth, ctx, wanted=None):
        """`wanted` is the scope of this request when the alias serves several (image or document)."""
        spec = self.config.aliases.get(name) if isinstance(name, str) else None
        if spec is None or spec.kind != kind:
            raise ServiceError(404, "unknown_model", "no such model alias")
        ctx.alias = name
        needed = wanted if wanted in spec.scopes and len(spec.scopes) > 1 else spec.scope
        if needed not in auth.scopes:
            raise ServiceError(403, "scope_denied", "the key has no scope for this model")
        engine = self.engines[name]
        if engine.unavailable() is not None:
            raise ServiceError(503, "engine_unavailable", "the engine cannot serve this request now",
                               retry_after=self.config.retry_after_seconds)
        return spec, engine

    async def precheck(self, engine, paths):
        try:
            for path in paths:
                await asyncio.to_thread(engine.precheck, path)
        except Rejected as error:
            raise ServiceError(422, error.code, "the input is refused before any model loads") from None

    async def await_job(self, request, task):
        """Wait for the job; when the client goes away, cancel it (queued: leave; running: kill the group)."""

        async def watch():
            while not await request.is_disconnected():
                await asyncio.sleep(POLL_DISCONNECT_SECONDS)

        watcher = asyncio.create_task(watch())
        try:
            done, _ = await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                if task.cancelled():
                    raise ServiceError(503, "draining", "the service stopped before the job finished")
                return task.result()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise ClientDisconnect()
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def new_job(self, ctx, auth, spec, engine, job_input, deadline):
        estimate = max(0.0, float(await asyncio.to_thread(engine.estimate, job_input.primary)))
        if not math.isfinite(estimate):
            estimate = 0.0
        return Job(ctx.rid, auth.id, ctx.alias, spec, engine, job_input, estimate, deadline, ctx.started)


def parse_deadline(request):
    raw = request.headers.get("x-request-deadline-seconds")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise invalid("X-Request-Deadline-Seconds must be a number") from None
    if not math.isfinite(value) or not 0 < value <= MAX_DEADLINE_SECONDS:
        raise invalid("X-Request-Deadline-Seconds is out of range")
    return value


def parse_length(request, limit):
    raw = request.headers.get("content-length")
    if raw is None or not raw.isascii() or not raw.isdigit():
        raise ServiceError(411, "length_required", "Content-Length is required", close=True)
    declared = int(raw)
    if declared > limit:
        raise ServiceError(413, "payload_too_large", "the request body is larger than allowed", close=True)
    return declared


def endpoint(counts_job=False):
    """Wrap a route: context, error envelope, request id header, one log line, drain bookkeeping."""

    def decorate(fn):
        async def run(request: Request):
            service = request.app.state.service
            ctx = Context()
            request.state.ctx = ctx
            if counts_job:
                service.jobs_inflight += 1
            status = 500
            try:
                response = await fn(request, service, ctx)
                status = response.status_code
            except ServiceError as error:
                response, status, ctx.refusal = error.response(), error.status, error.code
                if error.headers.get("X-Admission-Reason"):
                    ctx.refusal += ":" + error.headers["X-Admission-Reason"]
                if error.headers.get("X-Engine-Kill-Reason"):
                    ctx.refusal += ":" + error.headers["X-Engine-Kill-Reason"]
            except ClientDisconnect:
                response = ServiceError(499, "client_disconnected", "the client went away").response()
                status, ctx.refusal = 499, "client_disconnected"
            except asyncio.CancelledError:
                ctx.refusal = "cancelled"
                raise
            except Exception:
                response = ServiceError(500, "internal_error", "internal error").response()
                status, ctx.refusal = 500, "internal_error"
            finally:
                if counts_job:
                    service.jobs_inflight -= 1
                log_line(ctx, status)
            response.headers["X-Request-Id"] = ctx.rid
            if ctx.job is not None:
                response.headers["X-Paused-Seconds"] = f"{service.scheduler.paused_seconds(ctx.job):.3f}"
            return response

        run.__name__ = fn.__name__
        return run

    return decorate


def log_line(ctx, status):
    ACCESS.info(
        "rid=%s principal=%s alias=%s bytes_in=%d seconds=%.3f status=%d refusal=%s",
        ctx.rid, ctx.principal, ctx.alias, ctx.bytes_in, time.monotonic() - ctx.started, status, ctx.refusal,
    )


def create_app(config, probe=None, wall=time.time, yield_settings=None, sampler=None):
    service = Service(config, probe, wall, yield_settings, sampler)

    @asynccontextmanager
    async def lifespan(app):
        service.scheduler.start()
        if service.yielder is not None:
            service.yielder.start()
        try:
            yield
        finally:
            if service.yielder is not None:
                await service.yielder.stop()  # continues every group it stopped, before the scheduler kills them
            await service.scheduler.stop()
            service.workdir.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        code = {404: "not_found", 405: "method_not_allowed"}.get(error.status_code, "http_error")
        return ServiceError(error.status_code, code, "no such route or method",
                            headers=getattr(error, "headers", None)).response()

    @app.exception_handler(Exception)
    async def crash(request, error):
        return ServiceError(500, "internal_error", "internal error").response()

    @app.get("/health")
    async def health():
        scheduler = service.scheduler
        return {
            "status": "draining" if service.draining else "ok",
            "queue": {"depth": scheduler.depth(), "active": scheduler.active(), "capacity": config.queue_size},
            "admission": "closed" if service.draining else service.admission.overview(),
        }

    @app.get("/v1/models")
    @endpoint()
    async def models(request, service, ctx):
        auth = service.authenticate(request)
        ctx.principal = auth.id
        data = [
            {"id": name, "object": "model", "scopes": spec.scopes, "engine": spec.engine,
             "revision": service.engines[name].revision(),
             # ANYM-850: false while the engine cannot serve (a venv, a package or a tool missing or off its pin).
             "available": service.engines[name].unavailable() is None}
            for name, spec in sorted(config.aliases.items())
            if auth.scopes & set(spec.scopes)
        ]
        return JSONResponse({
            "object": "list",
            "data": data,
            "key_expires_at": auth.key.expires_at.isoformat().replace("+00:00", "Z"),
            "release": service.release,
        })

    @app.post("/v1/audio/transcriptions")
    @endpoint(counts_job=True)
    async def transcriptions(request, service, ctx):
        reservation, limit, declared = service.preflight(request, ctx, "audio")
        try:
            directory = reservation.make_directory()
            fields, path, total = await read_audio_form(
                request, directory, limit=limit, declared=declared, seconds=config.upload_seconds)
            ctx.bytes_in = total
            reservation.upload_done()
            try:
                form = TranscriptionForm.model_validate(fields)
            except ValidationError as error:
                raise invalid(validation_message(error)) from None
            spec, engine = service.resolve_alias(form.model, "audio", reservation.auth, ctx)
            job_input = JobInput("audio", directory, [path], path,
                                 {"language": form.language, "response_format": form.response_format})
            await service.precheck(engine, job_input.paths)
            job = await service.new_job(ctx, reservation.auth, spec, engine, job_input, request.state.deadline)
            ctx.job = job
            task = service.scheduler.admit(job, reservation.auth.principal, service.quota)
            result = await service.await_job(request, task)
            return JSONResponse(audio_response(result, form.response_format == "verbose_json"))
        finally:
            reservation.close()

    @app.post("/v1/documents/convert")
    @endpoint(counts_job=True)
    async def documents(request, service, ctx):
        """ANYM-850: a PDF, DOCX or PPTX to Markdown with page spans (`document-structure`)."""
        reservation, limit, declared = service.preflight(request, ctx, "document")
        try:
            directory = reservation.make_directory()
            fields, path, total = await read_document_form(
                request, directory, limit=limit, declared=declared, seconds=config.upload_seconds)
            ctx.bytes_in = total
            reservation.upload_done()
            try:
                form = DocumentForm.model_validate(fields)
            except ValidationError as error:
                raise invalid(validation_message(error)) from None
            if path.stat().st_size > DOCUMENT_MAX_BYTES:
                raise ServiceError(413, "payload_too_large", "the document is larger than 32 MiB")
            spec, engine = service.resolve_alias(form.model, "document", reservation.auth, ctx)
            job_input = JobInput("document", directory, [path], path, {})
            await service.precheck(engine, job_input.paths)
            job = await service.new_job(ctx, reservation.auth, spec, engine, job_input, request.state.deadline)
            ctx.job = job
            task = service.scheduler.admit(job, reservation.auth.principal, service.quota)
            result = await service.await_job(request, task)
            return JSONResponse(document_response(result))
        finally:
            reservation.close()

    @app.post("/v1/chat/completions")
    @endpoint(counts_job=True)
    async def chat(request, service, ctx):
        reservation, limit, declared = service.preflight(request, ctx, "chat")
        try:
            directory = reservation.make_directory()
            if "application/json" not in request.headers.get("content-type", "").lower():
                raise ServiceError(415, "unsupported_media_type", "application/json is required", close=True)
            body_path = directory / "body.json"
            ctx.bytes_in = await stream_to_file(
                request, body_path, limit=limit, declared=declared, seconds=config.upload_seconds)
            reservation.upload_done()
            payload = load_json(body_path)
            body_path.unlink()
            try:
                chat_request = ChatRequest.model_validate(payload)
            except ValidationError as error:
                raise invalid(validation_message(error)) from None
            del payload
            spec, engine = service.resolve_alias(chat_request.model, "chat", reservation.auth, ctx,
                                                 "image" if chat_request.image_count() else "document")
            images, prompt = extract_inputs(chat_request, directory)
            (directory / "prompt.txt").write_text(prompt)
            (directory / "messages.json").write_text(json.dumps(message_texts(chat_request)))
            primary = images[0] if images else directory / "prompt.txt"
            job_input = JobInput("chat", directory, images, primary,
                                 {"max_tokens": chat_request.max_tokens or spec.default_max_tokens})
            await service.precheck(engine, images)
            job = await service.new_job(ctx, reservation.auth, spec, engine, job_input, request.state.deadline)
            ctx.job = job
            task = service.scheduler.admit(job, reservation.auth.principal, service.quota)
            result = await service.await_job(request, task)
            return JSONResponse(chat_response(ctx.rid, int(time.time()), ctx.alias, result))
        finally:
            reservation.close()

    return app


def load_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    try:
        return json.loads(path.read_bytes(), object_pairs_hook=unique)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise invalid("the body is not valid JSON") from None


def message_texts(chat_request):
    """[{role, text}]: the text parts of each message joined, for an engine that builds its own chat prompt."""
    result = []
    for message in chat_request.messages:
        parts = [message.content] if isinstance(message.content, str) else message.content
        text = "\n".join(p if isinstance(p, str) else p.text for p in parts if isinstance(p, str) or p.type == "text")
        result.append({"role": message.role, "text": text})
    return result


def extract_inputs(chat_request, directory):
    """Decode every image data URL to its own file and gather the text. The file names are ours."""
    images, texts = [], []
    for message in chat_request.messages:
        parts = [message.content] if isinstance(message.content, str) else message.content
        for part in parts:
            if isinstance(part, str):
                texts.append(f"{message.role}: {part}")
            elif part.type == "text":
                texts.append(f"{message.role}: {part.text}")
            else:
                if len(images) >= 5:
                    raise invalid("too many images")
                mime, _, data = part.image_url.url[5:].partition(";base64,")
                try:
                    raw = base64.b64decode(data, validate=True)
                except (binascii.Error, ValueError):
                    raise invalid("image data is not valid base64") from None
                path = directory / f"image-{len(images)}.{EXTENSIONS[mime]}"
                path.write_bytes(raw)
                images.append(path)
    return images, "\n".join(texts)
