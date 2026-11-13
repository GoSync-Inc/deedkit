"""Dedicated protected password-EditText source transport (ANYM-998).

Unavailable without the native owner's real protected_window. Uses an already
connected existing UI server socket: no SDK, ADB command, process, clipboard,
server startup, IME, logging or mutation retry. Local references are read-only
owner-supplied files; this module never creates a credential store.
"""

import asyncio
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
from typing import AsyncContextManager, Callable, Protocol

from .protected_input import (
    InputBinding, InputPurpose, InputTransport, ManualProtectedInput,
    ProtectedInputError, ProtectedInputRequest, ProtectedInputResult, _identifier,
)


@dataclass(frozen=True, slots=True)
class FocusedPasswordTarget:
    """Trusted metadata from the fenced host's actual target; no field text or raw UI."""

    package: str
    resource_id: str
    target_id: str
    class_name: str
    focused: bool
    password: bool
    enabled: bool

    def matches(self, binding):
        return (self.package == binding.package and self.target_id == binding.target_id
                and self.class_name == 'android.widget.EditText' and self.focused is True
                and self.password is True and self.enabled is True
                and isinstance(self.resource_id, str)
                and bool(re.fullmatch(re.escape(binding.package) + r':id/[A-Za-z0-9_]+', self.resource_id)))

    def selector(self):
        # Exact selector/masks from pinned uiautomator2 3.7.1, without its request logging/retry wrapper.
        return dict(mask=0x10 | 0x8000 | 0x020000 | 0x080000 | 0x200000,
                    childOrSibling=[], childOrSiblingSelector=[], className=self.class_name,
                    enabled=True, focused=True, packageName=self.package, resourceId=self.resource_id)


@dataclass(frozen=True, slots=True)
class LocalInputReference:
    """Opaque reference descriptor from trusted owner provisioning; never a value/path from a model."""

    reference_id: str
    operation_id: str
    binding: InputBinding
    expires_at: float

    def __post_init__(self):
        _identifier(self.reference_id)
        _identifier(self.operation_id)
        if (not isinstance(self.binding, InputBinding) or type(self.expires_at) not in (float, int)
                or not math.isfinite(self.expires_at) or self.expires_at <= 0):
            raise ProtectedInputError('protected_input_invalid')


def _read_reference(directory, reference):
    """No writes/env/vault lookup. Return a bounded PIN in memory only, or a fixed error code."""
    parent = fd = None
    try:
        parent = os.open(Path(directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(parent)
        if info.st_mode & 0o077 or info.st_uid != os.getuid():
            return None, 'reference_unavailable'
        fd = os.open(reference.reference_id, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid()
                or info.st_nlink != 1):
            return None, 'reference_unavailable'
        if not 4 <= info.st_size <= 12:
            return None, 'reference_invalid'
        data = os.read(fd, 13)
        # Scope is a numeric app PIN in an actual password EditText, never arbitrary typing/shell input.
        if not re.fullmatch(rb'[0-9]{4,12}', data):
            return None, 'reference_invalid'
        # Detect a concurrent replacement/removal/revocation before allowing this value to leave the host.
        current = os.stat(reference.reference_id, dir_fd=parent, follow_symlinks=False)
        if (current.st_ino, current.st_dev, current.st_mtime_ns, current.st_size) != (
                info.st_ino, info.st_dev, info.st_mtime_ns, info.st_size):
            return None, 'reference_unavailable'
        return data.decode('ascii'), None
    except (OSError, ValueError, UnicodeError):
        return None, 'reference_unavailable'
    finally:
        if fd is not None:
            os.close(fd)
        if parent is not None:
            os.close(parent)


class ExistingInputChannel:
    """One raw setText request on an already-connected authorized UI server socket.

    Core supplies the device-bound socket while holding protected_window. This
    never connects/reconnects or starts UiAutomation, and closes the socket on
    every outcome. Generic HTTP clients' debug/audit request-body paths are avoided.
    """

    def __init__(self, device_id: str, connected_socket: socket.socket, *, timeout: float = 3):
        _identifier(device_id)
        if not isinstance(connected_socket, socket.socket) or not 0 < timeout <= 3:
            raise ProtectedInputError('protected_input_invalid')
        self.device_id, self.socket, self.timeout = device_id, connected_socket, timeout
        self.used = False

    def close(self):
        self.socket.close()

    async def set_password_once(self, request_id, target, value):
        if self.used:
            return 'unknown'
        self.used = True  # Even a failed/partial send must never use this channel again.
        try:
            self.socket.setblocking(False)
            body = json.dumps(dict(jsonrpc='2.0', id=request_id, method='setText',
                                   params=[target.selector(), value]), separators=(',', ':')).encode()
            wire = (b'POST /jsonrpc/0 HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n'
                    b'Connection: close\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
            async with asyncio.timeout(self.timeout):
                loop = asyncio.get_running_loop()
                await loop.sock_sendall(self.socket, wire)
                received = bytearray()
                while b'\r\n\r\n' not in received:
                    chunk = await loop.sock_recv(self.socket, 4096)
                    if not chunk or len(received) + len(chunk) > 8192:
                        return 'unknown'
                    received.extend(chunk)
                headers, payload = received.split(b'\r\n\r\n', 1)
                lines = headers.split(b'\r\n')
                if lines[0] not in (b'HTTP/1.1 200 OK', b'HTTP/1.0 200 OK'):
                    return 'unknown'
                fields = [line.lower().split(b':', 1) for line in lines[1:] if b':' in line]
                lengths = [int(v.strip()) for k, v in fields if k == b'content-length']
                if (len(lengths) != 1 or not 0 < lengths[0] <= 4096
                        or any(k in (b'transfer-encoding', b'content-encoding') for k, _ in fields)):
                    return 'unknown'
                while len(payload) < lengths[0]:
                    chunk = await loop.sock_recv(self.socket, min(4096, lengths[0] - len(payload)))
                    if not chunk:
                        return 'unknown'
                    payload.extend(chunk)
                if len(payload) != lengths[0]:
                    return 'unknown'
                response = json.loads(payload)
                if (not isinstance(response, dict) or response.get('jsonrpc') != '2.0'
                        or response.get('id') != request_id or 'error' in response):
                    return 'unknown'  # Raw error/message/params are never exported.
                return ('input_accepted' if response.get('result') is True else
                        'rejected' if response.get('result') is False else 'unknown')
        except BaseException:  # Includes cancellation after send: outcome unknown, never retry or raw exception.
            return 'unknown'
        finally:
            self.close()


class ProtectedWindow(Protocol):
    """A implements this against the real native/device state, never caller assertions.

    Acquire capture/read fence, blocking original native request, no in-flight
    commands, no auto-resolution; prevent replies/steer/new turns. Retain the
    fence on every exit unless allow_resume() follows verified safe postconditions.
    Reconstruct holds after restart before permitting any native observation.
    Verify reference-directory isolation from native's OS identity/read sandbox;
    a shared-UID 0600 file alone does not satisfy this dependency.
    """

    binding: InputBinding
    channel: ExistingInputChannel
    basket_approved: bool  # Existing complete-basket provenance, not a new Pay-button gate.

    def hold(self) -> None: ...
    def allow_resume(self) -> None: ...
    async def recheck(self) -> FocusedPasswordTarget: ...
    async def sensitive_field_gone(self) -> bool: ...


async def enter_password_once(
    journal: ManualProtectedInput, item: ProtectedInputRequest, reference: LocalInputReference, *,
    private_directory: Path,
    protected_window: Callable[[InputBinding], AsyncContextManager[ProtectedWindow]] | None = None,
) -> ProtectedInputResult:
    """Source-only until core supplies its actual protected_window and authorized existing channel.

    No value enters an external tool argument, native answer, result or journal.
    Every unsuccessful outcome holds the original question; automatic entry is
    unavailable for custom keypads/phone unlock and undocumented Grok references.
    """
    def result(status):
        return journal._result(item, status)
    if protected_window is None:
        return result('protected_window_unavailable')
    if item.transport != InputTransport.LOCAL_REFERENCE or item.binding.purpose == InputPurpose.PHONE_UNLOCK:
        return result('input_target_unsupported')
    claimed, channel = False, None
    try:
        async with protected_window(item.binding) as window:
            window.hold()
            channel = window.channel
            if window.binding != item.binding or channel.device_id != item.binding.device_id:
                return result('binding_mismatch')
            registered = journal._request(item, current=window.binding, capture_suspended=True,
                                          basket_approved=window.basket_approved)
            if registered.status != 'awaiting_owner' or not journal._claim_write(item):
                return registered
            claimed = True  # Durable UNKNOWN precedes reference read and any possible socket send.
            target = await window.recheck()
            if not isinstance(target, FocusedPasswordTarget) or not target.matches(item.binding):
                return journal._write_result(item, 'input_target_unsupported')
            status = None
            if reference.binding != item.binding or reference.operation_id != item.operation_id:
                status = 'reference_mismatch'
            elif journal.clock() >= reference.expires_at:
                status = 'reference_expired'
            if status:
                return journal._write_result(item, status)
            value, error = _read_reference(private_directory, reference)
            if error:
                return journal._write_result(item, error)
            # Recheck binding/focus/quiescence after reading the reference, immediately before dispatch.
            checked = await window.recheck()
            if window.binding != item.binding or checked != target or not checked.matches(item.binding):
                return journal._write_result(item, 'target_changed')
            if journal.clock() >= min(reference.expires_at, item.expires_at):
                return journal._write_result(item, 'expired')
            if item.binding.purpose == InputPurpose.PAYMENT and window.basket_approved is not True:
                return journal._write_result(item, 'basket_unapproved')
            refreshed, error = _read_reference(private_directory, reference)
            if error or refreshed != value:
                return journal._write_result(item, error or 'reference_unavailable')
            refreshed = None
            status = await window.channel.set_password_once(item.request_id, checked, value)
            value = None  # No memory-erasure promise; drop this reference promptly.
            if status == 'input_accepted' and await window.sensitive_field_gone() is True:
                if window.binding != item.binding:
                    return journal._write_result(item, 'target_changed')
                receipt = journal._write_result(item, 'owner_done')
                if receipt.resume_once:
                    window.allow_resume()
                return receipt
            return journal._write_result(item, status)
    except BaseException:  # No source/transport/guard exception value or stack reaches diagnostics/model.
        if claimed:
            try:
                return journal._write_result(item, 'unknown')
            except ProtectedInputError:
                pass
        return result('unknown')
    finally:
        if channel is not None:
            try:
                channel.close()
            except OSError:
                pass
