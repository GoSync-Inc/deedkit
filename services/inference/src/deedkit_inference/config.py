"""The service configuration: one private-ish JSON file, validated strictly before anything starts."""

import ipaddress
import json
import os
import re
import stat
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import ConfigError

MIB = 1024 * 1024
SLOTS = ("gpu", "cpu")
TEST_ENV = "DEEDKIT_INFERENCE_TESTING"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PhaseSpec(Strict):
    name: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,32}$")]
    slot: Literal["gpu", "cpu"]
    resident_gb: Annotated[float, Field(ge=0, le=64)]
    share: Annotated[float, Field(gt=0, le=1)] = 1.0
    # A phase that only some jobs run (the engine picks the route: Russian speech takes one, other languages another).
    route: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,16}$")] | None = None
    # A hard per-child memory limit (ANYM-850): the supervisor kills the phase's child (one-shot or warm) whose RSS is
    # above it. Without it a warm worker keeps the MEMORY_SHARE_FACTOR rule and a one-shot child only the hard cap.
    rss_limit_gb: Annotated[float, Field(gt=0, le=64)] | None = None


class AliasSpec(Strict):
    kind: Literal["audio", "chat", "document"]  # document: a file converted to Markdown (ANYM-850)
    scope: Literal["audio", "image", "document"]
    # More scopes the alias serves (a chat alias that reads images and text-only documents); a key needs the
    # scope of the request: `image` when it carries an image, else `document`.
    extra_scopes: Annotated[list[Literal["image", "document"]], Field(max_length=2)] = Field(default_factory=list)
    engine: Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,32}$")]
    engine_options: dict = Field(default_factory=dict)
    timeout_seconds: Annotated[float, Field(ge=0.1, le=7200)]
    default_max_tokens: Annotated[int, Field(ge=1, le=16000)] = 2000
    phases: Annotated[list[PhaseSpec], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def consistent(self):
        if (self.kind == "audio") != (self.scope == "audio"):
            raise ValueError("kind_scope_mismatch")
        routes = {p.route for p in self.phases if p.route}
        shared = sum(p.share for p in self.phases if not p.route)
        for route in routes or {None}:
            if abs(shared + sum(p.share for p in self.phases if route and p.route == route) - 1.0) > 1e-6:
                raise ValueError("phase_shares_must_sum_to_one")
        if self.extra_scopes and self.kind != "chat":
            raise ValueError("extra_scopes_need_a_chat_alias")
        if self.kind == "document" and self.scope != "document":
            raise ValueError("kind_scope_mismatch")
        if len({p.name for p in self.phases}) != len(self.phases):
            raise ValueError("duplicate_phase_name")
        return self

    @property
    def scopes(self):
        return [self.scope, *[s for s in self.extra_scopes if s != self.scope]]


class ProbeFigures(Strict):
    """Static memory and load figures, only in a test-mode config (the busy file is still read)."""

    available_gb: float = 16.0
    load_1m: float = 0.0


def absolute(value):
    if not isinstance(value, str) or not os.path.isabs(value) or "\x00" in value:
        raise ValueError("path_must_be_absolute")
    return value


class Config(Strict):
    version: Literal[1]
    mode: Literal["production", "test"] = "production"
    bind: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 8090
    policy_file: str
    busy_file: str
    work_dir: str
    # The read-only store of verified model files (`deedkit-inference pull` fills it, as another account).
    model_dir: str = "/Users/Shared/deedkit-inference/models"
    models_file: str | None = None  # test mode only: the pins come from the package otherwise
    usage_file: str | None = None
    release: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")] = "unknown"
    queue_size: Annotated[int, Field(ge=1, le=256)] = 16
    max_wait_seconds: Annotated[float, Field(gt=0, le=3600)] = 240
    retry_after_seconds: Annotated[float, Field(gt=0, le=3600)] = 30
    resident_cap_gb: Annotated[float, Field(gt=0, le=64)] = 6.0
    resident_hard_cap_gb: Annotated[float, Field(gt=0, le=64)] = 7.5
    min_free_gb: Annotated[float, Field(ge=0, le=64)] = 3.0
    # The admission floor (ANYM-682/684): a job is admitted only while the free memory, counting what the service's own loaded
    # engines hold, is at least this much; unreadable memory is zero and refuses. Between's runs need the room, now that
    # a light Between no longer closes admission. 0 switches the floor off (the undo, no code).
    admit_min_available_gb: Annotated[float, Field(ge=0, le=64)] = 7.0
    max_load_1m: Annotated[float, Field(gt=0)] = 8.0
    slot_max_load_1m: dict[Literal["gpu", "cpu"], Annotated[float, Field(gt=0)]] = Field(default_factory=dict)
    keep_alive_seconds: Annotated[float, Field(ge=0, le=86400)] = 300
    max_inflight_upload_bytes: Annotated[int, Field(ge=1)] = 128 * MIB
    upload_seconds: Annotated[float, Field(gt=0, le=3600)] = 120
    work_dir_max_bytes: Annotated[int, Field(ge=1)] = 512 * MIB
    audio_body_max_bytes: Annotated[int, Field(ge=1)] = 32 * MIB
    chat_body_max_bytes: Annotated[int, Field(ge=1)] = 44 * MIB
    # A document of at most DOCUMENT_MAX_BYTES (32 MiB) plus its multipart framing (ANYM-850).
    document_body_max_bytes: Annotated[int, Field(ge=1)] = 33 * MIB
    drain_seconds: Annotated[float, Field(ge=0, le=86400)] = 900
    # Housekeeping cadence; tests shorten these, production keeps the defaults.
    pump_interval_seconds: Annotated[float, Field(gt=0, le=60)] = 0.5
    supervise_interval_seconds: Annotated[float, Field(gt=0, le=60)] = 2.0
    evict_settle_seconds: Annotated[float, Field(ge=0, le=60)] = 5.0
    # Empty: the aliases the package ships (aliases.py).
    aliases: dict[Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")], AliasSpec] = Field(default_factory=dict)
    test_probe: ProbeFigures | None = None

    @field_validator("bind")
    @classmethod
    def loopback_only(cls, value):
        try:
            if not ipaddress.ip_address(value).is_loopback:
                raise ValueError
        except ValueError:
            raise ValueError("bind_must_be_a_loopback_ip") from None
        return value

    @field_validator("policy_file", "busy_file", "work_dir", "model_dir")
    @classmethod
    def paths(cls, value):
        return absolute(value)

    @field_validator("usage_file", "models_file")
    @classmethod
    def usage_path(cls, value):
        return None if value is None else absolute(value)

    @model_validator(mode="after")
    def consistent(self):
        if not self.aliases:
            from .aliases import shipped_aliases

            self.aliases = {name: AliasSpec.model_validate(spec) for name, spec in shipped_aliases().items()}
        if self.mode == "test":
            if os.environ.get(TEST_ENV) != "1":
                raise ValueError("test_mode_needs_testing_env")
        else:
            if self.models_file is not None:
                raise ValueError("models_file_in_production_config")
            if any(a.engine == "fake" for a in self.aliases.values()):
                raise ValueError("fake_engine_in_production_config")
            if self.test_probe is not None:
                raise ValueError("test_probe_in_production_config")
        if self.resident_hard_cap_gb < self.resident_cap_gb:
            raise ValueError("hard_cap_below_cap")
        return self


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def reason(error):
    """A machine reason from a pydantic error: locations and types only, never an input value."""
    if isinstance(error, ValidationError):
        first = error.errors(include_input=False, include_url=False)[0]
        where = ".".join(str(part) for part in first["loc"]) or "config"
        message = re.sub(r"^Value error, ", "", first.get("msg", ""))
        return f"{where}: {message if re.fullmatch(r'[a-z0-9_]+', message) else first['type']}"
    return str(error)


def load_config(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ConfigError("config_unreadable") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > 256 * 1024:
            raise ConfigError("config_not_a_plain_file_or_writable_by_others")
        raw = os.read(fd, 256 * 1024 + 1)
    finally:
        os.close(fd)
    try:
        return Config.model_validate(json.loads(raw, object_pairs_hook=_unique))
    except (ValueError, TypeError) as error:
        raise ConfigError(reason(error) if isinstance(error, ValidationError) else "config_invalid_json") from None


def resolve_release(config):
    return os.environ.get("DEEDKIT_INFERENCE_RELEASE") or config.release


def work_path(config):
    return Path(config.work_dir)
