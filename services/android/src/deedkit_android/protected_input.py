"""Metadata-only manual protected-input handoff (ANYM-998), not a secret store or input executor.

The native owner must hold its device/question lock, suspend all observation before
request(), and prove the sensitive screen has gone before completing DONE. All
context/booleans come from that trusted integration, NEVER from tool arguments.
No method accepts a secret, reads an environment value, runs a process or captures UI.
"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time


class ProtectedInputError(Exception):
    """Fixed codes only; no request values or lower-layer exception strings."""


def _invalid():
    raise ProtectedInputError('protected_input_invalid')


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        _invalid()


class InputPurpose(StrEnum):
    LOGIN = 'login'
    PAYMENT = 'payment'
    PHONE_UNLOCK = 'phone_unlock'


class InputTransport(StrEnum):
    MANUAL = 'manual'
    SECRET_REFERENCE = 'secret_reference'  # Explicit unavailable capability; no value/reference payload.
    LOCAL_REFERENCE = 'local_reference'  # Only the dedicated guarded writer may register this transport.


class Completion(StrEnum):
    DONE = 'owner_done'  # Owner reports completion, NOT proof of login/payment or purchase permission.
    REJECTED = 'rejected'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    TIMED_OUT = 'timed_out'
    UNKNOWN = 'unknown'


@dataclass(frozen=True, slots=True)
class InputBinding:
    owner: str
    device_id: str
    native_thread_id: str
    native_turn_id: str
    question_id: str
    package: str
    target_id: str  # Opaque host-minted field/screen identity, never field text/XML/screenshot.
    purpose: InputPurpose
    native_request_id: str | int  # Original JSON-RPC id, retain its type.
    native_item_id: str

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name == 'native_request_id' and type(value) is int:
                continue
            if name != 'purpose':
                _identifier(value)
        if not isinstance(self.purpose, InputPurpose):
            _invalid()

    def identity(self):
        return (self.owner, self.device_id, self.native_thread_id, self.native_turn_id,
                type(self.native_request_id), self.native_request_id, self.native_item_id, self.question_id)


@dataclass(frozen=True, slots=True)
class ProtectedInputRequest:
    request_id: str
    operation_id: str  # Original protected operation; changing question/turn never mints another attempt.
    binding: InputBinding
    expires_at: float
    transport: InputTransport = InputTransport.MANUAL

    def __post_init__(self):
        _identifier(self.request_id)
        _identifier(self.operation_id)
        if not isinstance(self.binding, InputBinding) or not isinstance(self.transport, InputTransport):
            _invalid()
        if (type(self.expires_at) not in (float, int) or not math.isfinite(self.expires_at)
                or self.expires_at <= 0):
            _invalid()


@dataclass(frozen=True, slots=True)
class ProtectedInputResult:
    status: str
    request_id: str
    operation_id: str | None = None
    native_thread_id: str | None = None
    native_turn_id: str | None = None
    question_id: str | None = None
    native_request_id: str | int | None = None
    native_item_id: str | None = None
    resume_once: bool = False

    def metadata(self):
        """For the authorized result consumer only; webhooks still carry their existing reference-only hints."""
        return asdict(self)


class ManualProtectedInput:
    """A private durable receipt journal. No secret/reference vault, CLI, network route or phone primitive.

    Persist DONE before emitting its one resume hint. A crash after commit may lose
    the hint: reconcile the original native question, NEVER repeat complete/resume
    or input automatically. Failed/uncertain attempts remain held until revoke().
    """

    def __init__(self, directory, *, clock=time.time):
        self.directory, self.clock = Path(directory), clock
        self.path = self.directory / 'protected-input.sqlite3'
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid()):
                raise ProtectedInputError('protected_input_unavailable')
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid()
                        or info.st_nlink != 1):
                    raise ProtectedInputError('protected_input_unavailable')
            finally:
                os.close(fd)
            with self._transaction() as db:
                db.execute('''CREATE TABLE IF NOT EXISTS inputs (
                    request_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
                    owner TEXT NOT NULL, device TEXT NOT NULL, thread TEXT NOT NULL, question_key TEXT NOT NULL,
                    request TEXT NOT NULL, status TEXT NOT NULL,
                    UNIQUE(owner, device, thread, question_key))''')
                db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS one_held_device ON inputs(device)
                    WHERE status NOT IN ('owner_done', 'revoked')''')
            # Include the new directory entry in the receipt's crash-durability boundary.
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except (OSError, sqlite3.Error):
            raise ProtectedInputError('protected_input_unavailable') from None

    @contextmanager
    def _transaction(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=5)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            raise ProtectedInputError('protected_input_unavailable') from None
        finally:
            if db is not None:
                db.close()  # Rolls back an interrupted/failed transaction, never a new attempt.

    @staticmethod
    def _result(item, status, *, resume_once=False):
        b = item.binding
        return ProtectedInputResult(status, item.request_id, item.operation_id, b.native_thread_id,
                                    b.native_turn_id, b.question_id, b.native_request_id,
                                    b.native_item_id, resume_once)

    @staticmethod
    def _load(row):
        data = json.loads(row['request'])
        data['binding']['purpose'] = InputPurpose(data['binding']['purpose'])
        data['binding'] = InputBinding(**data['binding'])
        data['transport'] = InputTransport(data['transport'])
        return ProtectedInputRequest(**data)

    @staticmethod
    def _set(db, request_id, status):
        db.execute('UPDATE inputs SET status=? WHERE request_id=?', (status, request_id))

    @staticmethod
    def _question_key(current):
        return json.dumps([current.native_turn_id, current.native_request_id,
                           current.native_item_id, current.question_id])

    def request(self, item: ProtectedInputRequest, *, current: InputBinding, channel_available: bool = True,
                capture_suspended: bool = False, basket_approved: bool = False) -> ProtectedInputResult:
        """Register the active manual question. All safety inputs are trusted native state under its lock."""
        if not isinstance(item, ProtectedInputRequest) or not isinstance(current, InputBinding):
            _invalid()
        if item.binding != current:
            return ProtectedInputResult('binding_mismatch', item.request_id)
        if item.transport != InputTransport.MANUAL:
            return self._result(item, 'reference_transport_unavailable')
        return self._request(item, current=current, channel_available=channel_available,
                             capture_suspended=capture_suspended, basket_approved=basket_approved)

    def _request(self, item, *, current, channel_available=True, capture_suspended=False, basket_approved=False):
        # Internal transport entry; callers must first acquire/recheck the trusted protected window.
        with self._transaction() as db:
            row = db.execute('SELECT * FROM inputs WHERE request_id=?', (item.request_id,)).fetchone()
            if row:
                if self._load(row) != item:
                    return ProtectedInputResult('identity_conflict', item.request_id)
                status = row['status']
                if status == 'awaiting_owner' and self.clock() >= item.expires_at:
                    status = 'expired'
                    self._set(db, item.request_id, status)
                return self._result(item, status)
            collision = db.execute('''SELECT 1 FROM inputs WHERE operation_id=? OR
                (owner=? AND device=? AND thread=? AND question_key=?)''',
                (item.operation_id, current.owner, current.device_id, current.native_thread_id,
                 self._question_key(current))).fetchone()
            if collision:
                return ProtectedInputResult('identity_conflict', item.request_id)
            held = db.execute('''SELECT 1 FROM inputs WHERE device=?
                AND status NOT IN ('owner_done', 'revoked')''',
                (current.device_id,)).fetchone()
            if held:
                return self._result(item, 'device_busy')
            if capture_suspended is not True:
                return self._result(item, 'capture_not_suspended')
            if current.purpose == InputPurpose.PAYMENT and basket_approved is not True:
                return self._result(item, 'basket_unapproved')
            status = ('channel_unavailable' if channel_available is not True else
                      'expired' if self.clock() >= item.expires_at else 'awaiting_owner')
            db.execute('INSERT INTO inputs VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (item.request_id, item.operation_id, current.owner, current.device_id,
                        current.native_thread_id, self._question_key(current), json.dumps(asdict(item)), status))
            return self._result(item, status)

    def _claim_write(self, item):
        """Commit uncertainty BEFORE resolving a value or sending bytes; no restart/retry can claim twice."""
        with self._transaction() as db:
            row = db.execute('SELECT * FROM inputs WHERE request_id=?', (item.request_id,)).fetchone()
            if (row is None or self._load(row) != item or row['status'] != 'awaiting_owner'
                    or self.clock() >= item.expires_at or item.transport != InputTransport.LOCAL_REFERENCE):
                return False
            self._set(db, item.request_id, 'unknown')
            return True

    def _write_result(self, item, status):
        with self._transaction() as db:
            row = db.execute('SELECT * FROM inputs WHERE request_id=?', (item.request_id,)).fetchone()
            if row is None or self._load(row) != item:
                return ProtectedInputResult('identity_conflict', item.request_id)
            if row['status'] != 'unknown':
                return self._result(item, row['status'])
            self._set(db, item.request_id, status)
            return self._result(item, status, resume_once=status == 'owner_done')

    def complete(self, request_id: str, *, current: InputBinding, outcome: Completion,
                 capture_suspended: bool = False, safe_to_resume: bool = False,
                 channel_available: bool = True, basket_approved: bool = False) -> ProtectedInputResult:
        """One correlated owner outcome. DONE permits only resuming the original protected question."""
        if not isinstance(outcome, Completion):
            _invalid()
        return self._finish(request_id, current, outcome.value, capture_suspended=capture_suspended,
                            safe_to_resume=safe_to_resume, channel_available=channel_available,
                            basket_approved=basket_approved)

    def revoke(self, request_id: str, *, current: InputBinding) -> ProtectedInputResult:
        """Native owner revokes before releasing/changing the question; reconcile uncertain input first.

        This releases the device hold, but permanently keeps the operation/question tombstone.
        Revocation authorizes no second input and emits no native resume hint.
        """
        return self._finish(request_id, current, 'revoked')

    def _finish(self, request_id, current, status, *, capture_suspended=False, safe_to_resume=False,
                channel_available=True, basket_approved=False):
        _identifier(request_id)
        if not isinstance(current, InputBinding):
            _invalid()
        with self._transaction() as db:
            row = db.execute('SELECT * FROM inputs WHERE request_id=?', (request_id,)).fetchone()
            if row is None:
                return ProtectedInputResult('not_found', request_id)
            item = self._load(row)
            if current.identity() != item.binding.identity():
                return ProtectedInputResult('binding_mismatch', request_id)
            previous = row['status']
            if status == 'revoked':
                self._set(db, request_id, status)
                return self._result(item, status)
            if previous != 'awaiting_owner':
                return self._result(item, previous)  # Never replay a consumed/held result.
            if self.clock() >= item.expires_at:
                status = 'expired'
            elif current != item.binding:
                status = 'target_changed'
            elif channel_available is not True:
                status = 'channel_unavailable'
            elif current.purpose == InputPurpose.PAYMENT and basket_approved is not True:
                status = 'basket_unapproved'
            elif capture_suspended is not True or (status == 'owner_done' and safe_to_resume is not True):
                status = 'unknown'
            self._set(db, request_id, status)
            return self._result(item, status, resume_once=status == 'owner_done')
