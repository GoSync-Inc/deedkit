"""Authenticated HTTP backend for the gateway's reviewed android_* tools."""

import base64
from collections import defaultdict
from contextlib import asynccontextmanager
import hashlib
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, Field, ValidationError
from typing import Literal

from .app_maps import compile_scenario, load, validate_map
from .config import Configuration, Refused, Strict
from .device import COMMANDS, Devices
from .flows import BARE, FIELDS, compile_flow, reference
from .store import Store, identifier
from .uiautomator import reference as ui_reference


class Device(Strict):
    device_id: str
    session_id: str


class Crop(Strict):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(ge=1, le=16384)
    h: int = Field(ge=1, le=16384)


class Observe(Device):
    screenshot: bool = True
    hierarchy: bool = True
    image_mode: Literal["inline", "file", "both"] = "inline"
    crop: Crop | None = None


class Execute(Device):
    request_id: str
    argv: list[str] = Field(min_length=1, max_length=128)
    timeout_s: int = Field(default=30, ge=1, le=600)
    wait_s: int = Field(default=15, ge=0, le=25)


class RunFlow(Device):
    request_id: str
    yaml: str | None = Field(default=None, max_length=32768)
    name: str | None = None
    version: str | None = None
    parameters: dict[str, str | int | bool] = Field(default_factory=dict)
    timeout_s: int = Field(default=120, ge=1, le=600)
    wait_s: int = Field(default=15, ge=0, le=25)
    element_timeout_ms: int = Field(default=5000, ge=100, le=60000)
    optional_timeout_ms: int = Field(default=1000, ge=100, le=60000)
    stage: Literal["published", "draft"] = "published"


class Flows(Strict):
    action: str = Field(pattern="^(list|get|put|delete|draft|publish|rollback|history)$")
    kind: Literal["flow", "map"] = "flow"
    stage: Literal["published", "draft"] = "published"
    base_version: str | None = None
    test_request_id: str | None = None
    message: str = Field(default="", max_length=1024)
    limit: int = Field(default=50, ge=1, le=100)
    before: int | None = Field(default=None, ge=1)
    name: str | None = None
    yaml: str | None = Field(default=None, max_length=32768)
    version: str | None = None


class MaestroInfo(Strict):
    action: Literal["reference", "validate"] = "reference"
    stage: Literal["published", "draft"] = "published"
    yaml: str | None = Field(default=None, max_length=32768)
    name: str | None = None
    version: str | None = None
    parameters: dict[str, str | int | bool] = Field(default_factory=dict)


class Receipt(Strict):
    request_id: str
    wait_s: int = Field(default=0, ge=0, le=25)


class Session(Strict):
    device_id: str
    action: str = Field(pattern="^(acquire|renew|release)$")
    session_id: str | None = None
    seconds: int = Field(default=900, ge=30, le=900, validation_alias=AliasChoices('seconds', 'lease_s'))


class Upload(Strict):
    file_id: str
    offset: int = Field(ge=0)
    data_base64: str = Field(max_length=43692)
    final: bool = False


class FileRef(Strict):
    file_id: str


class Download(FileRef):
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=32768, ge=1, le=4194304)


SCHEMAS = {
    "capabilities": Strict, "maestro": MaestroInfo, "observe": Observe, "execute": Execute, "get_operation": Receipt,
    "run_flow": RunFlow, "flows": Flows, "session": Session, "upload": Upload, "download": Download, "delete_file": FileRef,
}
READS = {"capabilities", "observe", "get_operation", "download", "maestro"}


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
            try:
                await devices.close()
            finally:
                store.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(Refused)
    async def refused(request, error):
        return JSONResponse({"error": error.code, **({'hint': error.hint} if hasattr(error, 'hint') else {})}, status_code=error.status,
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
        def compile_source(source, parameters, validate_only=False):
            if devices.ui:
                return compile_scenario(store.library, owner, source, parameters, validate_only=validate_only)[0]
            commands = compile_flow(source, parameters, fields=None,
                resolve=lambda name, version: store.flow(owner, "get", name, None, version)["yaml"])
            return commands
        if operation == "capabilities":
            result = {"service": "deedkit-android", "permissions": principal.permissions,
                      "devices": await devices.status(principal.devices), "adb_commands": sorted(COMMANDS),
                      "tool_manifest": {"version": 1, "names": list(SCHEMAS)},
                      "input_schemas": {name: schema.model_json_schema() for name, schema in SCHEMAS.items()},
                      "ui": {"engine": "uiautomator2" if devices.ui else "maestro",
                             "reference_tool": "android_maestro", "batch_tool": "android_run_flow"},
                      "maestro": {"configured": bool(configuration.initial.maestro),
                                  "commands": sorted(FIELDS.keys() | BARE), "dialect_version": 2, "checkpoint": "explicit_agent_handoff"},
                      "session_policy": {"renew_on_activity": True, "release_explicitly": True,
                                         "maximum_idle_seconds": 900},
                      "limits": {"command_seconds": 600, "file_bytes": 268435456, "chunk_bytes": 32768, "download_chunk_bytes": 4194304}}
        elif operation == "session":
            if devices.locks[device].locked() and args["action"] != "renew":
                raise Refused("device_busy")
            result = store.reserve(owner, device, args["action"], args["session_id"], args["seconds"])
        elif operation == "observe":
            result = await devices.observe(owner, device, args["session_id"], args["screenshot"], args["hierarchy"],
                                           args["image_mode"], args["crop"])
        elif operation == "execute":
            if any(len(arg) > 16384 or "\x00" in arg for arg in args["argv"]):
                raise Refused("invalid_arguments", 422)
            result = await devices.execute(owner, device, args["session_id"], identifier(args["request_id"]),
                                           args["argv"], args["timeout_s"], args["wait_s"])
        elif operation == "maestro":
            if args["action"] == "reference":
                result = ui_reference() if devices.ui else reference()
            else:
                if (args["yaml"] is None) == (args["name"] is None):
                    raise Refused("provide_yaml_or_flow_name", 422)
                source = args["yaml"]
                if source is None and not devices.ui:
                    source = store.flow(owner, "get", args["name"], None, args["version"])["yaml"]
                if devices.ui:
                    commands, metadata = compile_scenario(store.library, owner, source, args['parameters'],
                        stage=args['stage'], root={'name': args['name'], 'version': args['version']} if args['name'] else None)
                else:
                    commands, metadata = compile_source(source, args['parameters']), {}
                result = {"valid": True, "dialect_version": 4 if devices.ui else 2, "commands": commands, 'library': metadata}
        elif operation == "run_flow":
            if (args["yaml"] is None) == (args["name"] is None):
                raise Refused("provide_yaml_or_flow_name", 422)
            request_id = identifier(args['request_id'])
            identity = {key: value for key, value in args.items() if key not in {'request_id', 'session_id', 'wait_s'}}
            previous = store.replay(owner, request_id, identity)
            if previous is not None:
                return JSONResponse(previous, headers={"Cache-Control": "no-store"})
            source = args["yaml"]
            if devices.ui:
                commands, metadata = compile_scenario(store.library, owner, source, args['parameters'],
                    stage=args['stage'], root={'name': args['name'], 'version': args['version']} if args['name'] else None)
            else:
                if source is None:
                    source = store.flow(owner, 'get', args['name'], None, args['version'])['yaml']
                commands, metadata = compile_source(source, args['parameters']), {}
            result = await devices.execute(owner, device, args["session_id"], request_id,
                                           None, args["timeout_s"], args["wait_s"], flow=commands,
                                           element_timeout_ms=args['element_timeout_ms'],
                                           optional_timeout_ms=args['optional_timeout_ms'],
                                           identity=identity, metadata={'library': metadata})
        elif operation == "flows":
            if args['action'] in {'put', 'draft'}:
                if args['kind'] == 'map':
                    if args['action'] == 'put':
                        raise Refused('map_requires_draft', 422)
                    validate_map(args['yaml'])
                elif args['action'] == 'put':
                    if not isinstance(load(args['yaml']), list):
                        raise Refused('scenario_requires_draft', 422)
                    compile_source(args['yaml'], defaultdict(lambda: 'placeholder'), validate_only=True)
                else:
                    compile_scenario(store.library, owner, args['yaml'], {}, stage='draft', validate_only=True)
            if args['kind'] == 'flow' and args['action'] in {'put', 'delete'}:
                result = store.flow(owner, args['action'], args['name'], args['yaml'], args['version'])
            else:
                options = {key: value for key, value in args.items() if key != 'yaml'}
                result = store.library.manage(owner, source=args['yaml'], **options)
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
