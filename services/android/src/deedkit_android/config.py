"""Explicit device and principal configuration, reloaded on each request."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import stat

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Principal(Strict):
    id: str = Field(min_length=1, max_length=128)
    key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    devices: list[str]
    permissions: list[str]


class Config(Strict):
    adb: str
    state_dir: str
    maestro: str | None = None
    java_home: str | None = None
    devices: dict[str, str]
    principals: list[Principal]


class Refused(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


class Configuration:
    def __init__(self, path):
        self.path = Path(path)
        self.initial = self.read()
        for value in (self.initial.adb, self.initial.state_dir, self.initial.maestro, self.initial.java_home):
            if value is not None and not Path(value).is_absolute():
                raise ValueError("absolute_runtime_paths_required")

    def read(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
                raise ValueError("private_config_required")
            config = Config.model_validate(json.loads(os.read(fd, 65537)))
        finally:
            os.close(fd)
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
        if (config.adb, config.state_dir, config.devices, config.maestro, config.java_home) != (
            self.initial.adb, self.initial.state_dir, self.initial.devices, self.initial.maestro, self.initial.java_home,
        ):
            raise Refused("runtime_configuration_changed", 503)
        if not authorization.startswith("Bearer "):
            raise Refused("unauthorized", 401)
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        for principal in config.principals:
            if hmac.compare_digest(digest, principal.key_sha256):
                return principal
        raise Refused("unauthorized", 401)
