"""Authenticated HTTP backend for the gateway's reviewed android_* tools."""

import base64
from contextlib import asynccontextmanager
import functools
import hashlib
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, Field, ValidationError
from typing import Literal

from .agent_run import AgentCall, Agents, install_routes
from . import courier_chat
from .app_maps import compile_scenario, load, validate_map
from .config import Configuration, Refused, Strict
from .device import COMMANDS, Devices
from .field_errors import entries as field_errors
from .guard import Rules
from .store import Store, identifier
from .uiautomator import reference as ui_reference


class Device(Strict):
    device_id: str
    session_id: str


class OrderSourceRead(Strict):
    device_id: str
    source_id: str = Field(min_length=1, max_length=256)
    auth_epoch: int = Field(ge=1)
    after: int = Field(default=0, ge=0, le=9223372036854775807)
    limit: int = Field(default=4, ge=1, le=16)


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


class CourierChat(Device):  # ANYM-975: preview, then send; ANYM-978: goal, reply, read, handover, summary
    session_id: str | None = None  # the actions that need no phone (goal, handover, summary) take none
    request_id: str
    action: Literal["preview", "send", "goal", "reply", "read", "handover", "summary"]
    app: Literal["gojek", "grab"]
    order: str = Field(min_length=3, max_length=80, pattern=r"^[^\r\n]+$")
    message: str | None = Field(default=None, min_length=1, max_length=500, pattern=r"^[^\r\n]+$")
    preview_digest: str | None = Field(default=None, max_length=200)
    owner_instruction: str | None = Field(default=None, min_length=3, max_length=1500)
    goal: str | None = Field(default=None, min_length=1, max_length=300)
    max_messages: int | None = Field(default=None, ge=1, le=12)
    ttl_min: int | None = Field(default=None, ge=1, le=10080)
    min_interval_s: int | None = Field(default=None, ge=10, le=300)
    standing: bool = False
    reason: str | None = Field(default=None, min_length=1, max_length=200)
    timeout_s: int = Field(default=60, ge=10, le=180)
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
    "courier_chat": CourierChat, "agent": AgentCall,
}
# Where runs reach this service; the command line sets it from --host and --port.
AGENT_URL = None
READS = {"capabilities", "observe", "get_operation", "download", "maestro"}


def create_app(config_path, agent_url=None):
    agent_url = agent_url or AGENT_URL
    configuration = Configuration(config_path)

    @asynccontextmanager
    async def lifespan(app):
        store = Store(configuration.initial.state_dir)
        devices = Devices(configuration.initial, store)
        # The local agent (ANYM-971): runs reach this service on agent_url with their own per-run bearer.
        agents = Agents(devices, store, configuration, agent_url)
        app.state.devices, app.state.store, app.state.agents = devices, store, agents
        agents.supervision.start()
        try:
            yield
        finally:
            try:
                await agents.close()
                await devices.close()
            finally:
                store.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    install_routes(app)

    @app.post('/internal/v1/order-source')
    async def order_source(request: Request):
        from .order_events import OrderRefused
        principal = configuration.authenticate(request.headers.get('authorization', ''))
        if 'android:read' not in principal.permissions:
            raise Refused('forbidden', 403)
        body = await request.body()
        if len(body) > 4096:
            raise Refused('request_too_large', 413)
        try:
            args = OrderSourceRead.model_validate_json(body)
        except ValidationError as invalid:
            raise Refused('invalid_arguments', 422, field_errors(invalid, OrderSourceRead)) from None
        principal = configuration.authenticate(request.headers.get('authorization', ''))
        if 'android:read' not in principal.permissions:
            raise Refused('forbidden', 403)
        if args.device_id not in principal.devices:
            raise Refused('not_found', 404)
        control = request.app.state.agents.order_control()
        if control is None:
            raise Refused('order_source_unavailable')
        if (args.source_id, args.auth_epoch) != (control.source_id, control.auth_epoch):
            raise Refused('order_source_changed')
        try:
            return control.feed.read_source(principal, principal.id, args.device_id, args.after, args.limit)
        except OrderRefused as error:
            raise Refused(str(error)) from None

    @app.exception_handler(Refused)
    async def refused(request, error):
        # ANYM-984: no `invalid_arguments` goes out bare; one raised without a list names the request itself.
        fields = getattr(error, "field_errors", None) or (
            [{"field": "$", "reason": "format"}] if error.code == "invalid_arguments" else None)
        return JSONResponse({"error": error.code, **({'hint': error.hint} if hasattr(error, 'hint') else {}),
                             **getattr(error, 'extra', {}),
                             **({"field_errors": fields, "next_action": "check_parameters"} if fields else {})},
                            status_code=error.status, headers={"Cache-Control": "no-store"})

    @app.post("/v1/{operation}")
    async def call(operation: str, request: Request):
        devices, store = request.app.state.devices, request.app.state.store
        # The stored secret names, read once and only when a flow holds a reference (ANYM-969).
        known = functools.cache(devices.secrets.packages)
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
            raw = json.loads(body)
            args = SCHEMAS[operation].model_validate(raw).model_dump()
        except ValidationError as invalid:
            raise Refused("invalid_arguments", 422, field_errors(invalid, SCHEMAS[operation])) from None
        except ValueError:
            raise Refused("invalid_arguments", 422, [{"field": "$", "reason": "format"}]) from None
        device = args.get("device_id")
        if device is not None and device not in principal.devices:
            raise Refused("not_found", 404)
        owner = principal.id
        if operation == "capabilities":
            try:
                secrets = {"secrets": devices.secrets.listing()}  # Names and packages only, never a value.
            except Refused:
                secrets = {"secrets": [], "secrets_error": "unavailable"}
            result = {**secrets, "service": "deedkit-android", "permissions": principal.permissions,
                      "devices": [{**row, "session": store.session_state(owner, row["device_id"])}
                                  for row in await devices.status(principal.devices)], "adb_commands": sorted(COMMANDS),
                      "tool_manifest": {"version": 1, "names": list(SCHEMAS)},
                      "input_schemas": {name: schema.model_json_schema() for name, schema in SCHEMAS.items()},
                      "ui": {"engine": "uiautomator2", "configured": bool(devices.ui),
                             "reference_tool": "android_maestro", "batch_tool": "android_run_flow"},
                      # Where a run stops (ANYM-968): built-in rules plus the host's additions.
                      "guard": Rules(configuration.initial.guard.model_dump()).describe(),
                      # Compatibility key: the Maestro engine was removed (owner 11-11).
                      "maestro": {"configured": False, "removed": True, "engine": "uiautomator2"},
                      "session_policy": {"renew_on_activity": True, "release_explicitly": True,
                                         "maximum_idle_seconds": 900},
                      "limits": {"command_seconds": 600, "file_bytes": 268435456, "chunk_bytes": 32768, "download_chunk_bytes": 4194304}}
            try:
                result["agent"] = await request.app.state.agents.describe()
            except Refused:
                result["agent"] = {"enabled": False, "reason": "unavailable"}
        elif operation == "session":
            if args['action'] == 'release':
                store.check_reservation(owner, device, args['session_id'])
                await request.app.state.agents.supervision.release_pending(owner, device, args['session_id'])
                current = configuration.authenticate(request.headers.get('authorization', ''))
                if current.id != owner or device not in current.devices or 'android:control' not in current.permissions:
                    raise Refused('forbidden', 403)
            if devices.locks[device].locked() and args["action"] != "renew":
                raise Refused("device_busy")
            result = store.reserve(owner, device, args["action"], args["session_id"], args["seconds"])
            if args['action'] == 'release':
                await request.app.state.agents.supervision.released(owner, device, args['session_id'])
        elif operation == "observe":
            result = await devices.observe(owner, device, args["session_id"], args["screenshot"], args["hierarchy"],
                                           args["image_mode"], args["crop"])
        elif operation == "execute":
            if any(len(arg) > 16384 or "\x00" in arg for arg in args["argv"]):
                raise Refused("invalid_arguments", 422, [{"field": "argv", "reason": "size"}])
            result = await devices.execute(owner, device, args["session_id"], identifier(args["request_id"]),
                                           args["argv"], args["timeout_s"], args["wait_s"])
        elif operation == "courier_chat":
            result = await courier_chat.handle(devices, owner, args)
        elif operation == "maestro":
            if args["action"] == "reference":
                result = ui_reference()
            else:
                if (args["yaml"] is None) == (args["name"] is None):
                    raise Refused("provide_yaml_or_flow_name", 422)
                # Authoring needs no device and no configured engine.
                commands, metadata = compile_scenario(store.library, owner, args["yaml"], args['parameters'],
                    stage=args['stage'], root={'name': args['name'], 'version': args['version']} if args['name'] else None,
                    secrets=known)
                result = {"valid": True, "dialect_version": 4, "commands": commands, 'library': metadata}
        elif operation == "run_flow":
            if (args["yaml"] is None) == (args["name"] is None):
                raise Refused("provide_yaml_or_flow_name", 422)
            request_id = identifier(args['request_id'])
            identity = {key: value for key, value in args.items() if key not in {'request_id', 'session_id', 'wait_s'}}
            previous = store.replay(owner, request_id, identity)
            if previous is not None:
                return JSONResponse(previous, headers={"Cache-Control": "no-store"})
            commands, metadata = compile_scenario(store.library, owner, args["yaml"], args['parameters'],
                stage=args['stage'], root={'name': args['name'], 'version': args['version']} if args['name'] else None,
                secrets=known)
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
                    compile_scenario(store.library, owner, args['yaml'], {}, validate_only=True, secrets=known)
                else:
                    compile_scenario(store.library, owner, args['yaml'], {}, stage='draft', validate_only=True,
                                     secrets=known)
            if args['kind'] == 'flow' and args['action'] in {'put', 'delete'}:
                result = store.flow(owner, args['action'], args['name'], args['yaml'], args['version'])
            else:
                options = {key: value for key, value in args.items() if key != 'yaml'}
                result = store.library.manage(owner, source=args['yaml'], **options)
        elif operation == "agent":
            result = await request.app.state.agents.handle(owner, AgentCall.model_validate(raw),
                authorize=lambda: configuration.authenticate(request.headers.get('authorization', '')))
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
            if not result["eof"]:
                result.update(next_action="read_next_chunk", max_length=4194304)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return app
