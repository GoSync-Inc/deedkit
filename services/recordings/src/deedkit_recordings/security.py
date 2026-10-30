"""Private configuration and authenticated, grant-bound continuations. No writes."""
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Failure(Exception):
    def __init__(self, code, status=503):
        self.code, self.status = code, status
        super().__init__(code)


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Account(Strict):
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    provider: Literal['omi']
    label: str = Field(min_length=1, max_length=128)
    token_file: str


class Principal(Strict):
    id: str = Field(min_length=1, max_length=128)
    key_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    expires_at: str
    accounts: list[str]
    permissions: list[Literal['recordings:read', 'recordings:content']]


class Document(Strict):
    version: Literal[1]
    cursor_secret_file: str
    accounts: list[Account] = Field(max_length=64)
    principals: list[Principal] = Field(max_length=256)


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def loads(raw):
    return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def private(path, maximum):
    if not Path(path).is_absolute():
        raise ValueError('absolute path required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077 or before.st_size > maximum:
            raise ValueError('private regular file required')
        data = os.read(fd, maximum + 1)
        after = os.fstat(fd)
        if len(data) > maximum or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('file changed')
        return data
    finally:
        os.close(fd)


def configuration(path, key):
    try:
        doc = Document.model_validate(loads(private(str(path), 262144)))
        accounts = {a.id: a for a in doc.accounts}
        if len(accounts) != len(doc.accounts):
            raise ValueError('duplicate account')
        if len({p.id for p in doc.principals}) != len(doc.principals) or len({
                p.key_sha256 for p in doc.principals}) != len(doc.principals):
            raise ValueError('duplicate principal')
        for actor in doc.principals:
            if set(actor.accounts) - accounts.keys() or len(set(actor.accounts)) != len(actor.accounts):
                raise ValueError('account reference')
            if len(set(actor.permissions)) != len(actor.permissions):
                raise ValueError('duplicate permission')
            expiry = datetime.fromisoformat(actor.expires_at.replace('Z', '+00:00'))
            if expiry.tzinfo is None:
                raise ValueError('expiry requires timezone')
        secret = private(doc.cursor_secret_file, 256).strip()
        if len(secret) < 32:
            raise ValueError('short cursor secret')
    except (OSError, ValueError, TypeError):
        raise Failure('configuration_unavailable') from None
    hashed = hashlib.sha256(key.encode()).hexdigest()
    for actor in doc.principals:
        if hmac.compare_digest(actor.key_sha256, hashed):
            expiry = datetime.fromisoformat(actor.expires_at.replace('Z', '+00:00'))
            if expiry > datetime.now(timezone.utc):
                return actor, accounts, secret
    raise Failure('unauthorized', 401)


def provider_token(account):
    try:
        value = private(account.token_file, 1024).decode().strip()
        if not 8 <= len(value) <= 512 or any(c.isspace() for c in value) or not value.isascii():
            raise ValueError('invalid token')
        return value
    except (OSError, ValueError, UnicodeError):
        raise Failure('provider_auth_required') from None


def encoded(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def signature(payload, secret):
    return hmac.new(secret, payload, hashlib.sha256).digest()


def continuation(binding, position, secret, digest=None):
    raw = canonical({'binding': binding, 'position': position, 'digest': digest, 'expires': int(time.time()) + 900})
    token = encoded(raw) + '.' + encoded(signature(raw, secret))
    if len(token) > 8192:
        raise Failure('invalid_provider_response')
    return token


def cursor(token, binding, secret):
    try:
        if len(token) > 8192:
            raise ValueError
        payload, sig = token.split('.')
        raw = base64.b64decode(payload + '=' * (-len(payload) % 4), altchars=b'-_', validate=True)
        received = base64.b64decode(sig + '=' * (-len(sig) % 4), altchars=b'-_', validate=True)
        if not hmac.compare_digest(received, signature(raw, secret)):
            raise ValueError
        value = loads(raw)
        if value['binding'] != binding or type(value['expires']) is not int or value['expires'] < time.time():
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise Failure('invalid_cursor', 400) from None
