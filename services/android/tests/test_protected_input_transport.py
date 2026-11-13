"""Synthetic existing-server sockets and private reference files; never ADB/phone/vault/model."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import logging
import socket
import threading

import pytest

from deedkit_android.protected_input import InputTransport, ManualProtectedInput
from deedkit_android.protected_input_transport import (
    ExistingInputChannel, FocusedPasswordTarget, LocalInputReference, enter_password_once,
)
from test_protected_input import binding, request

CANARY = '592184'  # Synthetic, never a real PIN.


class Window:
    def __init__(self, channel, *, changed=False, safe=True, fail=False):
        self.binding = binding()
        self.channel = channel
        self.basket_approved = False
        self.held, self.released, self.checks = False, False, 0
        self.changed, self.safe, self.fail = changed, safe, fail

    def hold(self):
        self.held = True

    def allow_resume(self):
        assert self.held
        self.held, self.released = False, True

    async def recheck(self):
        assert self.held  # Capture fence must precede every reference read/write.
        self.checks += 1
        if self.fail:
            raise RuntimeError(CANARY)
        return FocusedPasswordTarget('example.shop', 'example.shop:id/password',
                                     'changed' if self.changed and self.checks > 1 else 'field-1',
                                     'android.widget.EditText', True, True, True)

    async def sensitive_field_gone(self):
        assert self.held
        return self.safe

    @asynccontextmanager
    async def open(self, expected):
        assert expected == self.binding
        try:
            yield self
        finally:
            # Mirrors A's required contract: unresolved exits retain the actual fence.
            assert self.held or self.released


def fixture(tmp_path, *, response=True, lost_reply=False, delay_reply=False, **window_options):
    directory = tmp_path / 'refs'
    directory.mkdir(mode=0o700)
    path = directory / 'ref-1'
    path.write_text(CANARY)
    path.chmod(0o600)
    journal = ManualProtectedInput(tmp_path / 'journal', clock=lambda: 100)
    item = request(transport=InputTransport.LOCAL_REFERENCE)
    ref = LocalInputReference('ref-1', item.operation_id, item.binding, 200)
    client, server = socket.socketpair()
    channel = ExistingInputChannel('phone', client, timeout=0.1)
    window = Window(channel, **window_options)
    received = []

    def serve():
        with server:
            server.settimeout(0.2)
            data = bytearray()
            try:
                while b'\r\n\r\n' not in data:
                    chunk = server.recv(4096)
                    if not chunk:
                        return
                    data.extend(chunk)
                headers, body = data.split(b'\r\n\r\n', 1)
                size = int(next(line.split(b':', 1)[1] for line in headers.split(b'\r\n')
                                if line.lower().startswith(b'content-length:')))
                while len(body) < size:
                    body.extend(server.recv(4096))
                received.append(json.loads(body))
                if lost_reply:
                    return
                if delay_reply:
                    threading.Event().wait(0.15)
                payload = json.dumps(dict(jsonrpc='2.0', id=item.request_id, result=response)).encode()
                server.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(payload)).encode()
                               + b'\r\nConnection: close\r\n\r\n' + payload)
            except (OSError, StopIteration):
                pass

    thread = threading.Thread(target=serve)
    thread.start()
    return directory, journal, item, ref, window, received, thread


def run(fx, **options):
    directory, journal, item, ref, window, _, thread = fx
    try:
        return asyncio.run(enter_password_once(journal, item, ref, private_directory=directory,
                                              protected_window=window.open, **options))
    finally:
        window.channel.close()
        thread.join(timeout=1)
        assert not thread.is_alive()


# spec: ANYM-998
def test_raw_password_write_uses_body_only_exact_selector_and_resumes_same_question(tmp_path, caplog):
    # Falsifies use of generic retrying/debug SDK, unsafe selector and uncorrelated continuation.
    fx = fixture(tmp_path)
    with caplog.at_level(logging.DEBUG):
        result = run(fx)
    assert result.resume_once and result.status == 'owner_done'
    assert result.native_request_id == 12 and result.native_item_id == 'item-1'
    assert result.question_id == 'question-1' and fx[4].released
    assert len(fx[5]) == 1 and fx[5][0]['method'] == 'setText'
    selector, text = fx[5][0]['params']
    assert text == CANARY and selector['focused'] is True and selector['enabled'] is True
    assert selector['packageName'] == 'example.shop' and selector['resourceId'] == 'example.shop:id/password'
    assert CANARY not in caplog.text + repr(result)
    assert not any(CANARY.encode() in p.read_bytes() for p in tmp_path.rglob('*')
                   if p.is_file() and p.parent.name != 'refs')
    directory, journal, item, ref, window, _, _ = fx
    replay = asyncio.run(enter_password_once(journal, item, ref, private_directory=directory,
                                             protected_window=window.open))
    assert not replay.resume_once and len(fx[5]) == 1


# spec: ANYM-998
@pytest.mark.parametrize('options,status', [({'changed': True}, 'target_changed'),
                                          ({'fail': True}, 'unknown'),
                                          ({'safe': False}, 'input_accepted'),
                                          ({'lost_reply': True}, 'unknown'),
                                          ({'response': False}, 'rejected')])
def test_changed_focus_failed_process_lost_reply_and_unknown_hold_without_retry(tmp_path, options, status,
                                                                            capsys, caplog):
    # Failure/cancellation diagnostics carry no value; a committed one-shot cannot be retried after restart.
    fx = fixture(tmp_path, **options)
    with caplog.at_level(logging.DEBUG):
        result = run(fx)
    assert result.status == status and not result.resume_once and fx[4].held
    assert len(fx[5]) <= 1
    directory, _, item, ref, window, seen, _ = fx
    journal = ManualProtectedInput(tmp_path / 'journal', clock=lambda: 100)
    replay = asyncio.run(enter_password_once(journal, item, ref, private_directory=directory,
                                             protected_window=window.open))
    assert not replay.resume_once and len(seen) <= 1
    output = capsys.readouterr()
    assert CANARY not in output.out + output.err + caplog.text + repr(result) + repr(replay)


# spec: ANYM-998
def test_missing_real_window_never_reads_reference_or_opens_channel(tmp_path):
    # No caller booleans or fake success can replace the mandatory core protected_window hook.
    journal = ManualProtectedInput(tmp_path / 'journal', clock=lambda: 100)
    item = request(transport=InputTransport.LOCAL_REFERENCE)
    ref = LocalInputReference('ref-1', item.operation_id, item.binding, 200)
    result = asyncio.run(enter_password_once(journal, item, ref, private_directory=tmp_path / 'missing'))
    assert result.status == 'protected_window_unavailable' and not result.resume_once


# spec: ANYM-998
@pytest.mark.parametrize('change,status', [('expired', 'reference_expired'), ('removed', 'reference_unavailable'),
                                          ('open', 'reference_unavailable'), ('symlink', 'reference_unavailable'),
                                          ('binding', 'reference_mismatch'), ('value', 'reference_invalid')])
def test_private_reference_invalid_expired_revoked_or_wrong_binding_never_writes(tmp_path, change, status):
    # Local owner-supplied references have expiry/binding/private-file controls; no duplicated vault.
    fx = list(fixture(tmp_path))
    path = fx[0] / 'ref-1'
    if change == 'expired':
        fx[3] = replace(fx[3], expires_at=100)
    elif change == 'removed':
        path.unlink()
    elif change == 'open':
        path.chmod(0o644)
    elif change == 'symlink':
        path.unlink()
        path.symlink_to(tmp_path / 'outside')
    elif change == 'binding':
        fx[3] = replace(fx[3], binding=binding(owner='other'))
    elif change == 'value':
        path.write_text('invalid-value')
    result = run(fx)
    assert result.status == status and not result.resume_once and not fx[5] and fx[4].held


# spec: ANYM-998
def test_cancel_after_consumption_holds_without_exception_content(tmp_path):
    # Cancelled native tasks retain the capture fence; the secret never enters a propagated exception.
    fx = fixture(tmp_path)
    window = fx[4]
    original = window.recheck
    async def cancel():
        if window.checks:
            raise asyncio.CancelledError(CANARY)
        return await original()
    window.recheck = cancel
    result = run(fx)
    assert result.status == 'unknown' and not result.resume_once and window.held and not fx[5]


# spec: ANYM-998
def test_timeout_and_error_body_do_not_retry_or_export_values(tmp_path, capsys, caplog):
    # Accepted-but-timed-out or value-echoing server bodies never become retry permission or diagnostic text.
    for name, options in [('timeout', {'delay_reply': True}), ('body', {'response': {'error': CANARY}})]:
        directory = tmp_path / name
        directory.mkdir()
        fx = fixture(directory, **options)
        with caplog.at_level(logging.DEBUG):
            result = run(fx)
        assert result.status == 'unknown' and not result.resume_once and fx[4].held and len(fx[5]) == 1
    output = capsys.readouterr()
    assert CANARY not in output.out + output.err + caplog.text + repr(result)


# spec: ANYM-998
def test_reference_revocation_after_preflight_prevents_dispatch(tmp_path):
    # Deleting a reference while core rechecks focus must still prevent the already-loaded value from being sent.
    fx = fixture(tmp_path)
    window = fx[4]
    original = window.recheck
    async def recheck():
        target = await original()
        if window.checks == 2:
            (fx[0] / 'ref-1').unlink()
        return target
    window.recheck = recheck
    result = run(fx)
    assert result.status == 'reference_unavailable' and not result.resume_once and not fx[5] and window.held


# spec: ANYM-998
def test_unsupported_target_never_reads_value_and_postcondition_cannot_resume_wrong_turn(tmp_path, monkeypatch):
    # No keypad/unfocused/plain field fallback; a changed native question after accepted input cannot resume.
    from deedkit_android import protected_input_transport as transport
    fx = fixture(tmp_path)
    async def not_password():
        return FocusedPasswordTarget('example.shop', 'example.shop:id/password', 'field-1',
                                     'android.widget.EditText', True, False, True)
    fx[4].recheck = not_password
    with monkeypatch.context() as scoped:
        def forbidden(*args):
            raise RuntimeError(CANARY)
        scoped.setattr(transport, '_read_reference', forbidden)
        result = run(fx)
    assert result.status == 'input_target_unsupported' and not fx[5]
    second = tmp_path / 'second'
    second.mkdir()
    fx = fixture(second)
    async def changed_question():
        fx[4].binding = binding(native_turn_id='another-turn')
        return True
    fx[4].sensitive_field_gone = changed_question
    result = run(fx)
    assert result.status == 'target_changed' and not result.resume_once and fx[4].held
