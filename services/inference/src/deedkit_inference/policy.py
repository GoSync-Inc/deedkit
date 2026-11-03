"""Per-service keys: a private JSON policy (the pattern of services/gateway/.../policy.py), a revision that
only grows, constant-time key comparison and the one-writer replace."""

import fcntl
import hashlib
import hmac
import json
import logging
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from .errors import PolicyError

LIMIT = 64 * 1024
LOG = logging.getLogger("deedkit_inference.policy")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KeySpec(Strict):
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expires_at: AwareDatetime


class PrincipalSpec(Strict):
    id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
    keys: Annotated[list[KeySpec], Field(min_length=1, max_length=2)]
    scopes: Annotated[list[Literal["audio", "image", "document"]], Field(max_length=3)]
    max_jobs_per_day: Annotated[int, Field(ge=1, le=1_000_000)]
    max_concurrent: Annotated[int, Field(ge=1, le=64)]

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.scopes)) != len(self.scopes) or len({k.sha256 for k in self.keys}) != len(self.keys):
            raise ValueError("duplicate")
        return self


class PolicyDoc(Strict):
    version: Literal[1]
    revision: Annotated[int, Field(ge=1)]
    principals: Annotated[list[PrincipalSpec], Field(max_length=32)]

    @model_validator(mode="after")
    def unique(self):
        ids = [p.id for p in self.principals]
        digests = [k.sha256 for p in self.principals for k in p.keys]
        if len(set(ids)) != len(ids) or len(set(digests)) != len(digests):
            raise ValueError("duplicate")
        return self


def read_private(path, limit=LIMIT):
    """The check of services/gateway policy.py:72: a regular file, no symlink, no group or other bits, bounded."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise PolicyError("policy_unreadable") from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077 or before.st_size > limit:
            raise PolicyError("policy_not_private_or_too_large")
        value = os.read(fd, limit + 1)
        after = os.fstat(fd)
        same = (before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if len(value) > limit or not same:
            raise PolicyError("policy_changed_while_reading")
        return value
    finally:
        os.close(fd)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def parse(raw):
    try:
        return PolicyDoc.model_validate(json.loads(raw, object_pairs_hook=_unique))
    except (ValueError, TypeError, ValidationError):
        raise PolicyError("policy_invalid") from None


class Authenticated:
    def __init__(self, principal, key):
        self.principal, self.key = principal, key

    @property
    def id(self):
        return self.principal.id

    @property
    def scopes(self):
        return set(self.principal.scopes)


class PolicyStore:
    """Holds the policy in force. A reload that rolls the revision back, or changes the content at the same
    revision, is refused and the old policy stays (the rule of gateway policy.py:119-125)."""

    def __init__(self, path):
        self.path = Path(path)
        self.doc = None
        self.revision, self.digest = 0, ""
        self._seen = None

    def load_initial(self):
        self._apply(read_private(self.path))
        self._seen = self._stat_key()

    def _stat_key(self):
        try:
            info = os.lstat(self.path)
            return (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        except OSError:
            return None

    def _apply(self, raw):
        doc = parse(raw)
        digest = hashlib.sha256(raw).hexdigest()
        if doc.revision < self.revision or (doc.revision == self.revision and digest != self.digest):
            raise PolicyError("policy_rollback")
        self.doc, self.revision, self.digest = doc, doc.revision, digest

    def current(self):
        key = self._stat_key()
        if key != self._seen:
            self._seen = key
            try:
                self._apply(read_private(self.path))
            except PolicyError as error:
                LOG.warning("policy reload refused code=%s revision_in_force=%s", error, self.revision)
        return self.doc

    def authenticate(self, token):
        """Compare against every key of every principal, no early exit. A missing or malformed token still
        hashes something, so every failure takes the same path and ends in the same 401."""
        doc = self.current()
        digest = hashlib.sha256((token if token is not None else "").encode()).hexdigest()
        now, found = datetime.now(UTC), None
        for principal in doc.principals:
            for key in principal.keys:
                same = hmac.compare_digest(digest, key.sha256)
                if same and token is not None and key.expires_at > now and found is None:
                    found = Authenticated(principal, key)
        return found


def bearer(header):
    """The token of an `Authorization: Bearer` header, or None when it is absent or malformed."""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value or len(value) > 512 or not value.isascii():
        return None
    if not value.isprintable() or any(c.isspace() for c in value):
        return None
    return value


def write_policy(path, document):
    """The one writer: a lock file, a strictly greater revision, a 0600 temp file, an atomic replace."""
    path = Path(path)
    new = PolicyDoc.model_validate(document)
    lock = os.open(str(path) + ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists() and new.revision <= parse(read_private(path)).revision:
            raise PolicyError("policy_rollback")
        temp = Path(f"{path}.{os.getpid()}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps(new.model_dump(mode="json"), indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(lock)
