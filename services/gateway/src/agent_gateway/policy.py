"""Owner-managed atomic policy snapshots, checked for every request/admission."""

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
import stat
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .catalog import CATALOG, ACTIONS, routed


class Denied(Exception):
    def __init__(self, code="not_found_or_forbidden", status=403):
        self.code, self.status = code, status
        super().__init__(code)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Backend(Strict):
    url: str
    host_header: str | None = None
    download_origin: str | None = None


class Deployment(Strict):
    modules: list[str]
    backends: dict[str, Backend]


class Profile(Strict):
    tools: list[str]
    actions: dict[str, list[str]] = Field(default_factory=dict)


class Credential(Strict):
    key_file: str
    key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Principal(Strict):
    id: str = Field(min_length=1, max_length=128)
    key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile: str
    enabled: bool
    expires_at: str
    grants: dict[str, list[str]]
    credentials: dict[str, Credential]
    actions: dict[str, list[str]] = Field(default_factory=dict)


class Document(Strict):
    version: int
    revision: int = Field(ge=1)
    deployment: Deployment
    profiles: dict[str, Profile]
    principals: list[Principal]


def read_private(path, limit=1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077 or before.st_size > limit:
            raise Denied("policy_unavailable", 503)
        value = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if len(value) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise Denied("policy_unavailable", 503)
        return value
    finally:
        os.close(fd)


def key_from_file(path, expected_sha256):
    try:
        value = read_private(path, 1024).decode().strip()
        if not value or len(value) > 512 or any(c.isspace() for c in value):
            raise ValueError
        if not hmac.compare_digest(hashlib.sha256(value.encode()).hexdigest(), expected_sha256):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise Denied("backend_unavailable", 503) from None


class Policy:
    def __init__(self, path):
        self.path = Path(path)
        self.revision, self.digest = 0, ""

    def load(self):
        try:
            raw = read_private(self.path)

            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result

            doc = Document.model_validate(json.loads(raw, object_pairs_hook=unique)).model_dump()
            if doc["version"] != 1:
                raise ValueError
            digest = hashlib.sha256(raw).hexdigest()
            if doc["revision"] < self.revision or (
                doc["revision"] == self.revision and digest != self.digest
            ):
                raise ValueError
            modules = doc["deployment"]["modules"]
            if set(modules) - {"calendar", "messaging", "calls"} or len(modules) != len(set(modules)):
                raise ValueError
            if set(modules) - doc["deployment"]["backends"].keys():
                raise ValueError
            for target in doc["deployment"]["backends"].values():
                url = urlsplit(target["url"])
                if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password:
                    raise ValueError
                if url.query or url.fragment or url.path not in ("", "/"):
                    raise ValueError
                if target["host_header"] and any(c in target["host_header"] for c in "\r\n /\\"):
                    raise ValueError
                if target["download_origin"]:
                    download = urlsplit(target["download_origin"])
                    if (
                        download.scheme not in ("http", "https")
                        or not download.hostname
                        or download.username
                        or download.password
                        or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*/?", download.path)
                        or download.query
                        or download.fragment
                    ):
                        raise ValueError
            ids, keys = set(), set()
            for profile in doc["profiles"].values():
                if set(profile["tools"]) - CATALOG.keys() or len(profile["tools"]) != len(
                    set(profile["tools"])
                ):
                    raise ValueError
                self.validate_actions(profile["actions"])
            for agent in doc["principals"]:
                if (
                    agent["id"] in ids
                    or agent["key_sha256"] in keys
                    or agent["profile"] not in doc["profiles"]
                ):
                    raise ValueError
                ids.add(agent["id"])
                keys.add(agent["key_sha256"])
                expiry = datetime.fromisoformat(agent["expires_at"].replace("Z", "+00:00"))
                self.validate_actions(agent["actions"])
                if expiry.tzinfo is None:
                    raise ValueError
                if any(not Path(v["key_file"]).is_absolute() for v in agent["credentials"].values()):
                    raise ValueError
            self.revision, self.digest = doc["revision"], digest
            return doc
        except (OSError, ValueError, TypeError, ValidationError, KeyError):
            raise Denied("policy_unavailable", 503) from None

    def authenticate(self, key):
        return self.authenticate_digest(hashlib.sha256(key.encode()).hexdigest())

    def authenticate_digest(self, hashed):
        snapshot = self.load()
        for agent in snapshot["principals"]:
            if hmac.compare_digest(hashed, agent["key_sha256"]):
                expiry = datetime.fromisoformat(agent["expires_at"].replace("Z", "+00:00"))
                if agent["enabled"] and expiry > datetime.now(timezone.utc):
                    return snapshot, agent
        raise Denied("unauthorized", 401)

    @staticmethod
    def validate_actions(actions):
        if set(actions) - {"messenger_operations"}:
            raise ValueError("unknown action group")
        values = actions.get("messenger_operations", [])
        if set(values) - ACTIONS.keys() or len(values) != len(set(values)):
            raise ValueError("unknown action or wildcard")

    @staticmethod
    def actions(snapshot, agent, backend_permissions=None):
        profile = snapshot["profiles"][agent["profile"]]
        selected = set(profile["actions"].get("messenger_operations", [])) & set(
            agent["actions"].get("messenger_operations", [])
        )
        scopes = set(agent["grants"].get("messaging", []))
        if backend_permissions is not None:
            scopes &= set(backend_permissions)
        return sorted(action for action in selected if set(ACTIONS[action]) <= scopes)

    @staticmethod
    def visible(snapshot, agent):
        profile = snapshot["profiles"][agent["profile"]]
        modules = {"gateway", *snapshot["deployment"]["modules"]}
        return [
            name
            for name in profile["tools"]
            if CATALOG[name]["module"] in modules
            and (CATALOG[name]["module"] == "gateway" or CATALOG[name]["module"] in agent["credentials"])
            and set(CATALOG[name]["scopes"]) <= set(agent["grants"].get(CATALOG[name]["module"], []))
            and (not CATALOG[name].get("action_group") or bool(routed(CATALOG[name], Policy.actions(snapshot, agent))))
            and (
                not CATALOG[name].get("required_action")
                or CATALOG[name]["required_action"] in Policy.actions(snapshot, agent)
            )
        ]

    def authorize(self, key, name):
        snapshot, agent = self.authenticate(key)
        return self.authorize_agent(snapshot, agent, name)

    def authorize_digest(self, hashed, name):
        return self.authorize_agent(*self.authenticate_digest(hashed), name)

    def authorize_agent(self, snapshot, agent, name):
        if name not in self.visible(snapshot, agent):
            raise Denied()
        return snapshot, agent, CATALOG[name]
