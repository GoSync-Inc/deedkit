"""The runner's private configuration: one 0600 JSON file, validated strictly before anything starts.

The file is read with the gateway's private-file rules (services/gateway/src/agent_gateway/policy.py,
`read_private`): a regular file, not a symlink, no group or other bits, bounded size, unchanged while read.
"""

import ipaddress
import json
import os
import re
import stat
import threading
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PORT = 8448
ENGINES = ("claude", "codex")
# Tailscale's address ranges: the CGNAT block for IPv4 and the ULA prefix it assigns for IPv6.
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
WORKSPACE_LIMIT = 3 * 1024**3
OUT_LIMIT = 16 * 1024**2
# A repository digest, or a local image ID (as immutable a pin, for an image built on the host itself).
DIGEST_IMAGE = re.compile(r"^(?:[a-z0-9][a-z0-9._/:-]{0,200}@)?sha256:[0-9a-f]{64}$")
NAME = r"^[a-z0-9][a-z0-9_.-]{0,62}$"


class ConfigError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(f"{code} {detail}".strip())
        self.code, self.detail = code, detail


def read_private(path, limit=1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077 or before.st_size > limit:
            raise ConfigError("private_file_refused", str(path))
        value = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if len(value) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns,
        ):
            raise ConfigError("private_file_refused", str(path))
        return value
    finally:
        os.close(fd)


def is_tailnet(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in TAILNET)


def size_bytes(text):
    match = re.fullmatch(r"(\d+)([kmg]?)b?", str(text).strip().lower())
    if not match:
        return None
    return int(match.group(1)) * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[match.group(2)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Listen(Strict):
    tailnet_host: str
    port: int = PORT
    tls_cert: Path
    tls_key: Path
    loopback_host: str = "127.0.0.1"
    loopback_port: Annotated[int, Field(ge=1024, le=65535)] = 8449

    @model_validator(mode="after")
    def only_tailnet_and_loopback(self):
        if not is_tailnet(self.tailnet_host):
            raise ValueError("bind_must_be_the_tailnet_address")
        if self.port != PORT:
            raise ValueError("bind_port_must_be_8448")
        try:
            loopback = ipaddress.ip_address(self.loopback_host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("cli_bind_must_be_loopback")
        if self.loopback_port == PORT:
            raise ValueError("cli_port_must_differ")
        return self


class Key(Strict):
    key_id: Annotated[str, Field(pattern=NAME)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    principal: Annotated[str, Field(pattern=NAME)]
    operations: list[Literal["read", "run"]]
    revoked: bool = False

    @field_validator("operations")
    @classmethod
    def read_or_read_run(cls, value):
        if sorted(value) not in (["read"], ["read", "run"]):
            raise ValueError("operations_must_be_read_or_read_run")
        return value


class Command(Strict):
    argv: Annotated[list[str], Field(min_length=1, max_length=32)]
    timeout_s: Annotated[float, Field(gt=0, le=60)] = 10


class Volume(Strict):
    driver: Annotated[str, Field(pattern=NAME)] = "local"
    options: dict[str, str]

    def limit(self):
        """The size limit the driver options set, in bytes (`size=` alone or inside `o=`), or None."""
        values = []
        if "size" in self.options:
            values.append(self.options["size"])
        for part in self.options.get("o", "").split(","):
            if part.startswith("size="):
                values.append(part[5:])
        sizes = [size_bytes(v) for v in values]
        known = [s for s in sizes if s is not None]
        return min(known) if known and len(known) == len(sizes) else None


class Claude(Strict):
    enabled: bool = True
    token_file: Path


class Codex(Strict):
    enabled: bool = True
    login_volume: Annotated[str, Field(pattern=NAME)]


class Engines(Strict):
    claude: Claude | None = None
    codex: Codex | None = None


class Docker(Strict):
    binary: str = "docker"
    host: str | None = None  # DOCKER_HOST of the VM's socket; None uses the CLI's default


class Config(Strict):
    version: Literal[1]
    listen: Listen
    production_peer: str
    state_dir: Path
    image: str
    task_user: Annotated[str, Field(pattern=r"^[1-9][0-9]{2,5}:[1-9][0-9]{2,5}$")] = "1000:1000"
    network: Annotated[str, Field(pattern=NAME)]
    docker: Docker = Docker()
    engines: Engines
    workspace_volume: Volume
    out_volume: Volume
    daily_starts: Annotated[int, Field(ge=1, le=20)] = 10
    host_gate: Command
    host_check: Command
    keys: list[Key]

    @model_validator(mode="after")
    def consistent(self):
        if not is_tailnet(self.production_peer):
            raise ValueError("production_peer_must_be_a_tailnet_address")
        if not DIGEST_IMAGE.match(self.image):
            raise ValueError("image_must_be_pinned_by_digest")
        workspace, out = self.workspace_volume.limit(), self.out_volume.limit()
        if workspace is None or workspace > WORKSPACE_LIMIT:
            raise ValueError("workspace_volume_needs_a_size_limit_of_at_most_3g")
        if out is None or out > OUT_LIMIT:
            raise ValueError("out_volume_needs_a_size_limit_of_at_most_16m")
        if len({k.sha256 for k in self.keys}) != len(self.keys) or len({k.key_id for k in self.keys}) != len(self.keys):
            raise ValueError("duplicate_key")
        return self

    def engine(self, name):
        value = getattr(self.engines, name, None)
        return value if value is not None and value.enabled else None


def parse(raw):
    try:
        return Config.model_validate(json.loads(raw))
    except ValidationError as error:
        detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors())
        raise ConfigError("config_invalid", detail[:500]) from None
    except ValueError:
        raise ConfigError("config_invalid", "not JSON") from None


def load(path):
    try:
        return parse(read_private(path))
    except OSError as error:
        raise ConfigError("config_unreadable", error.strerror or "") from None


class Keys:
    """The key table, re-read when the file changes, so a revocation takes effect without a restart.

    An unreadable or invalid file after a change authorizes nobody until it is valid again. The table and its
    change mark come from this object's own reads (mark first, then the file), never from an earlier load, so a
    change made while the service was starting is seen at the first request.
    """

    def __init__(self, path):
        self.path, self._lock = Path(path), threading.Lock()
        self._mark = self._stat()
        self._keys = self._read()

    def _read(self):
        try:
            return {k.sha256: k for k in load(self.path).keys}
        except ConfigError:
            return {}

    def _stat(self):
        try:
            value = os.stat(self.path, follow_symlinks=False)
            return value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
        except OSError:
            return None

    def get(self, sha256):
        with self._lock:
            mark = self._stat()
            if mark != self._mark:
                self._mark = mark
                self._keys = self._read()
            return self._keys.get(sha256)
