"""Authenticated HTTP backend for the gateway's reviewed android_* tools."""

import base64
from contextlib import asynccontextmanager
import hashlib
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError

from .config import Configuration, Refused, Strict
from .device import COMMANDS, Devices
from .store import Store, identifier


class Device(Strict):
    device_id: str
    session_id: str


class Observe(Device):
    screenshot: bool = True
    hierarchy: bool = True


class Execute(Device):
    request_id: str
    argv: list[str] = Field(min_length=1, max_length=128)
    timeout_s: int = Field(default=30, ge=1, le=600)
    wait_s: int = Field(default=15, ge=0, le=25)


class Receipt(Strict):
    request_id: str
    wait_s: int = Field(default=0, ge=0, le=25)


class Session(Strict):
    device_id: str
    action: str = Field(pattern="^(acquire|renew|release)$")
    session_id: str | None = None
    seconds: int = Field(default=300, ge=30, le=900)


class Upload(Strict):
    file_id: str
    offset: int = Field(ge=0)
    data_base64: str = Field(max_length=43692)
    final: bool = False


class FileRef(Strict):
    file_id: str


class Download(FileRef):
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=32768, ge=1, le=32768)


SCHEMAS = {
    "capabilities": Strict, "observe": Observe, "execute": Execute, "get_operation": Receipt,
    "session": Session, "upload": Upload, "download": Download, "delete_file": FileRef,
}
READS = {"capabilities", "observe", "get_operation", "download"}


def create_app(config_path):
    configuration = Configuration(config_path)

    @asynccontextmanager
    async def lifespan(app):
        store = Store(configuration.initial.state_dir)
        devices = Devices(configuration.initial, store)
        app.state.devices, app.state.store = devices, store
        try:
            yield
        finally:
            await devices.close()
            store.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(Refused)
    async def refused(request, error):
        return JSONResponse({"error": error.code}, status_code=error.status,
                            headers={"Cache-Control": "no-store"})

    @app.post("/v1/{operation}")
    async def call(operation: str, request: Request):
        devices, store = request.app.state.devices, request.app.state.store
        principal = configuration.authenticate(request.headers.get("authorization", ""))
        if operation not in SCHEMAS:
            raise Refused("not_found", 404)
        permission = "android:read" if operation in READS else "android:control"
        if permission not in principal.permissions:
            raise Refused("forbidden", 403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise Refused("request_too_large", 413)
        try:
            args = SCHEMAS[operation].model_validate(json.loads(body)).model_dump()
        except (ValidationError, ValueError):
            raise Refused("invalid_arguments", 422) from None
        device = args.get("device_id")
        if device is not None and device not in principal.devices:
            raise Refused("not_found", 404)
        owner = principal.id
        if operation == "capabilities":
            result = {"service": "deedkit-android", "permissions": principal.permissions,
                      "devices": await devices.status(principal.devices), "adb_commands": sorted(COMMANDS),
                      "tool_manifest": {"version": 1, "names": list(SCHEMAS)},
                      "limits": {"command_seconds": 600, "file_bytes": 268435456, "chunk_bytes": 32768}}
        elif operation == "session":
            if devices.locks[device].locked() and args["action"] != "renew":
                raise Refused("device_busy")
            result = store.reserve(owner, device, args["action"], args["session_id"], args["seconds"])
        elif operation == "observe":
            result = await devices.observe(owner, device, args["session_id"], args["screenshot"], args["hierarchy"])
        elif operation == "execute":
            if any(len(arg) > 16384 or "\x00" in arg for arg in args["argv"]):
                raise Refused("invalid_arguments", 422)
            result = await devices.execute(owner, device, args["session_id"], identifier(args["request_id"]),
                                           args["argv"], args["timeout_s"], args["wait_s"])
        elif operation == "get_operation":
            result = await devices.receipt(owner, identifier(args["request_id"]), args["wait_s"])
        elif operation == "upload":
            result = store.upload(owner, identifier(args["file_id"]), args["offset"],
                                  args["data_base64"], args["final"])
        elif operation == "delete_file":
            if any(lock.locked() for lock in devices.locks.values()):
                raise Refused("device_busy")
            result = store.delete_file(owner, identifier(args["file_id"]))
        else:
            path = store.file(owner, identifier(args["file_id"]))
            size = path.stat().st_size
            if args["offset"] > size:
                raise Refused("invalid_offset", 422)
            with path.open("rb") as stream:
                stream.seek(args["offset"])
                content = stream.read(args["length"])
            result = {"file_id": args["file_id"], "offset": args["offset"], "size": size,
                      "data_base64": base64.b64encode(content).decode(),
                      "next_offset": args["offset"] + len(content),
                      "eof": args["offset"] + len(content) == size,
                      "chunk_sha256": hashlib.sha256(content).hexdigest()}
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return app
