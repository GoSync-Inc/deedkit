"""Explicit device and principal configuration, reloaded on each request."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import stat

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Principal(Strict):
    id: str = Field(min_length=1, max_length=128)
    key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    devices: list[str]
    permissions: list[str]


PACKAGE = r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$"
Package = Annotated[str, Field(pattern=PACKAGE, max_length=256)]
Phrase = Annotated[str, Field(min_length=1, max_length=128)]


class GuardRules(Strict):
    """Additions to the built-in guard (guard.py). No field removes or disables a built-in rule."""
    stop_phrases: list[Phrase] = Field(default_factory=list, max_length=500)
    pin_phrases: list[Phrase] = Field(default_factory=list, max_length=500)
    stop_ids: dict[Package, list[Annotated[str, Field(min_length=1, max_length=256)]]] = Field(
        default_factory=dict, max_length=200)
    protected_packages: list[Package] = Field(default_factory=list, max_length=500)
    write_denied_packages: list[Package] = Field(default_factory=list, max_length=500)
    stop_activities: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9_.$/]+$", max_length=256)]] = Field(
        default_factory=list, max_length=500)


class EngineConfig(Strict):
    """One driving CLI of the local agent (ANYM-971). The login stays in a 0600 file or a private directory of the host:
    Claude reads `token_file`, Codex its `auth.json` in `codex_home`. `version` pins the binary's version."""
    binary: str
    version: str = Field(min_length=1, max_length=64)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    reasoning_effort: Literal['minimal', 'low', 'medium', 'high'] = 'medium'
    token_file: str | None = None
    codex_home: str | None = None


class AgentConfig(Strict):
    # Off until set: while false no run starts and no engine login file is read (ANYM-971 criterion 10).
    enabled: bool = False
    guarded: bool = False
    persistent: bool = False


class Config(Strict):
    adb: str
    state_dir: str
    engines: dict[Literal["claude", "codex"], EngineConfig] = Field(default_factory=dict)
    default_engine: Literal["claude", "codex"] | None = None
    agent: AgentConfig = Field(default_factory=AgentConfig)
    # Removed engine (owner 11-11): a config that still names it fails at start.
    maestro: str | None = None
    java_home: str | None = None
    uiautomator2_jar: str | None = None
    uiautomator2_jar_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    ocr_command: str | None = None
    ocr_languages: str = Field(default="eng", pattern=r"^[A-Za-z_]{3,16}(\+[A-Za-z_]{3,16}){0,7}$")
    devices: dict[str, str]
    principals: list[Principal]
    guard: GuardRules = Field(default_factory=GuardRules)


class Refused(Exception):
    def __init__(self, code, status=409, field_errors=None):
        self.code, self.status = code, status
        if field_errors:  # ANYM-984: an `invalid_arguments` refusal names the fields (field_errors.entries)
            self.field_errors = field_errors
        super().__init__(code)


class Configuration:
    def __init__(self, path):
        self.path = Path(path)
        self.initial = self.read()
        for value in (self.initial.adb, self.initial.state_dir, self.initial.uiautomator2_jar, self.initial.ocr_command):
            if value is not None and not Path(value).is_absolute():
                raise ValueError("absolute_runtime_paths_required")

        if self.initial.uiautomator2_jar:
            digest = hashlib.sha256(Path(self.initial.uiautomator2_jar).read_bytes()).hexdigest()
            if digest != self.initial.uiautomator2_jar_sha256:
                raise ValueError("uiautomator2_artifact_hash_mismatch")

    def read(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
                raise ValueError("private_config_required")
            config = Config.model_validate(json.loads(os.read(fd, 65537)))
        finally:
            os.close(fd)
        if config.maestro is not None or config.java_home is not None:
            raise ValueError("maestro_engine_removed")
        for name, engine in config.engines.items():
            paths = (engine.binary, engine.token_file, engine.codex_home)
            if any(value is not None and not Path(value).is_absolute() for value in paths):
                raise ValueError("absolute_runtime_paths_required")
            if (name == "claude" and engine.codex_home) or (name == "codex" and engine.token_file):
                raise ValueError("invalid_engine_login")
        if config.default_engine is not None and config.default_engine not in config.engines:
            raise ValueError("default_engine_not_configured")
        ids = [p.id for p in config.principals]
        keys = [p.key_sha256 for p in config.principals]
        if len(set(config.devices.values())) != len(config.devices) or any(
            not label or not serial for label, serial in config.devices.items()
        ):
            raise ValueError("device_serials_must_be_unique_and_nonempty")
        if len(ids) != len(set(ids)) or len(keys) != len(set(keys)):
            raise ValueError("duplicate_principal")
        for principal in config.principals:
            if set(principal.devices) - config.devices.keys() or set(principal.permissions) - {
                "android:read", "android:control",
            }:
                raise ValueError("invalid_grants")
        return config

    def authenticate(self, authorization):
        try:
            config = self.read()
        except (OSError, ValueError):
            raise Refused("configuration_unavailable", 503) from None
        if (config.adb, config.state_dir, config.devices, config.uiautomator2_jar, config.uiautomator2_jar_sha256,
            config.ocr_command, config.ocr_languages, config.guard) != (
            self.initial.adb, self.initial.state_dir, self.initial.devices, self.initial.uiautomator2_jar, self.initial.uiautomator2_jar_sha256,
            self.initial.ocr_command, self.initial.ocr_languages, self.initial.guard,
        ):
            raise Refused("runtime_configuration_changed", 503)
        if not authorization.startswith("Bearer "):
            raise Refused("unauthorized", 401)
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        for principal in config.principals:
            if hmac.compare_digest(digest, principal.key_sha256):
                return principal
        raise Refused("unauthorized", 401)
