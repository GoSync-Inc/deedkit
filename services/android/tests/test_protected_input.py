"""Manual protected-input correlation only; no phone, model, vault or real secret (ANYM-998)."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
import logging
import os
import subprocess

import pytest

from deedkit_android.protected_input import (
    Completion, InputBinding, InputPurpose, InputTransport, ManualProtectedInput,
    ProtectedInputError, ProtectedInputRequest,
)


def binding(**changes):
    return replace(InputBinding('owner', 'phone', 'thread-1', 'turn-1', 'question-1',
                                'example.shop', 'field-1', InputPurpose.LOGIN, 12, 'item-1'), **changes)


def request(**changes):
    return replace(ProtectedInputRequest('request-1', 'operation-1', binding(), 200), **changes)


def open_input(broker, item=None, **options):
    item = item or request()
    return broker.request(item, current=item.binding, capture_suspended=True, **options)


def done(broker, item=None, **options):
    item = item or request()
    return broker.complete(item.request_id, current=item.binding, outcome=Completion.DONE,
                           capture_suspended=True, safe_to_resume=True, **options)


# spec: ANYM-998
def test_done_resumes_only_original_question_once_across_restart(tmp_path):
    # Falsifies duplicate native continuation and loss of original IDs after process restart.
    broker = ManualProtectedInput(tmp_path / 'input', clock=lambda: 100)
    assert open_input(broker).status == 'awaiting_owner'
    result = done(broker)
    assert result.status == 'owner_done' and result.resume_once
    assert result.metadata() == dict(status='owner_done', request_id='request-1', operation_id='operation-1',
                                     native_thread_id='thread-1', native_turn_id='turn-1',
                                     question_id='question-1', native_request_id=12,
                                     native_item_id='item-1', resume_once=True)
    restarted = ManualProtectedInput(tmp_path / 'input', clock=lambda: 101)
    assert not done(restarted).resume_once
    assert not open_input(restarted).resume_once
    assert open_input(restarted).status == 'owner_done'


# spec: ANYM-998
@pytest.mark.parametrize('field,value', [('owner', 'other'), ('device_id', 'other'),
                                        ('native_thread_id', 'other'), ('native_turn_id', 'other'),
                                        ('question_id', 'other'), ('native_item_id', 'other'),
                                        ('native_request_id', 13), ('native_request_id', '12')])
def test_unrelated_context_neither_consumes_nor_discloses_receipt(tmp_path, field, value):
    # Falsifies unauthorized or stale question completion; its failure must not cancel the owner's handoff.
    broker = ManualProtectedInput(tmp_path / 'input', clock=lambda: 100)
    open_input(broker)
    result = broker.complete('request-1', current=binding(**{field: value}), outcome=Completion.DONE,
                             capture_suspended=True, safe_to_resume=True)
    assert result.status == 'binding_mismatch' and not result.resume_once
    assert result.operation_id is None and result.question_id is None
    assert done(broker).resume_once


# spec: ANYM-998
@pytest.mark.parametrize('field,value', [('package', 'example.other'), ('target_id', 'field-2'),
                                        ('purpose', InputPurpose.PAYMENT)])
def test_changed_input_target_invalidates_without_retry(tmp_path, field, value):
    # A focused app/field/purpose change invalidates this attempt, even if the target later changes back.
    broker = ManualProtectedInput(tmp_path / 'input', clock=lambda: 100)
    open_input(broker)
    result = broker.complete('request-1', current=binding(**{field: value}), outcome=Completion.DONE,
                             capture_suspended=True, safe_to_resume=True, basket_approved=True)
    assert result.status == 'target_changed' and not result.resume_once
    assert done(broker).status == 'target_changed'
    assert open_input(broker, request(request_id='new-id')).status == 'identity_conflict'


# spec: ANYM-998
@pytest.mark.parametrize('outcome', [Completion.REJECTED, Completion.FAILED, Completion.CANCELLED,
                                   Completion.TIMED_OUT, Completion.UNKNOWN])
def test_failed_cancelled_and_uncertain_input_cannot_retry_with_new_ids(tmp_path, outcome):
    # Falsifies automatic retry/new-ID bypass after any unsuccessful or uncertain owner input.
    directory = tmp_path / 'input'
    broker = ManualProtectedInput(directory, clock=lambda: 100)
    open_input(broker)
    result = broker.complete('request-1', current=binding(), outcome=outcome, capture_suspended=True)
    assert result.status == outcome.value and not result.resume_once
    broker = ManualProtectedInput(directory, clock=lambda: 101)
    assert done(broker).status == outcome.value and not done(broker).resume_once
    assert open_input(broker, request(request_id='new-id')).status == 'identity_conflict'
    assert open_input(broker, request(request_id='new-id', operation_id='new-operation')).status \
        == 'identity_conflict'
    changed_question = request(request_id='new-id', binding=binding(question_id='question-2'))
    assert open_input(broker, changed_question).status == 'identity_conflict'


# spec: ANYM-998
def test_concurrent_completion_is_one_shot_and_device_cannot_have_two_handoffs(tmp_path):
    # Separate helper instances serialize at the durable journal, rather than an in-memory lock.
    directory = tmp_path / 'input'
    broker = ManualProtectedInput(directory, clock=lambda: 100)
    open_input(broker)
    other = request(request_id='other', operation_id='other', binding=binding(question_id='question-2'))
    assert open_input(broker, other).status == 'device_busy'
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: done(ManualProtectedInput(directory, clock=lambda: 100)), range(8)))
    assert sum(result.resume_once for result in results) == 1


# spec: ANYM-998
@pytest.mark.parametrize('kind', [Completion.UNKNOWN, Completion.TIMED_OUT])
def test_unknown_and_timeout_hold_device_until_explicit_reconciliation(tmp_path, kind):
    # An uncertain handoff continues to block another question and cannot become DONE implicitly.
    broker = ManualProtectedInput(tmp_path / 'input', clock=lambda: 100)
    open_input(broker)
    broker.complete('request-1', current=binding(), outcome=kind, capture_suspended=True)
    other = request(request_id='other', operation_id='other', binding=binding(question_id='question-2'))
    assert open_input(broker, other).status == 'device_busy'
    assert broker.revoke('request-1', current=binding()).status == 'revoked'
    assert done(broker).status == 'revoked'
    assert open_input(broker, other).status == 'awaiting_owner'


# spec: ANYM-998
def test_invalid_expired_and_revoked_handoff_references_never_resume(tmp_path):
    # These are metadata receipt references, never credential references or value lookups.
    now = [100]
    broker = ManualProtectedInput(tmp_path / 'input', clock=lambda: now[0])
    assert broker.complete('missing', current=binding(), outcome=Completion.DONE).status == 'not_found'
    open_input(broker)
    now[0] = 200  # exact expiry boundary
    assert done(broker).status == 'expired' and not done(broker).resume_once
    assert open_input(broker, request(request_id='new-id')).status == 'identity_conflict'
    assert broker.revoke('request-1', current=binding()).status == 'revoked'
    assert done(broker).status == 'revoked'


# spec: ANYM-998
def test_capture_window_and_safe_exit_are_required(tmp_path):
    # A metadata DONE must never restart screenshot/model sampling of a secret still visible on the phone.
    broker = ManualProtectedInput(tmp_path / 'input', clock=lambda: 100)
    assert broker.request(request(), current=binding()).status == 'capture_not_suspended'
    open_input(broker)
    assert broker.complete('request-1', current=binding(), outcome=Completion.DONE,
                            capture_suspended=True).status == 'unknown'
    assert not done(broker).resume_once


# spec: ANYM-998
def test_payment_scope_uses_basket_approval_and_usb_loss_never_restores_channel(tmp_path):
    # A PIN handoff isn't purchase permission; login/unlock carry no approval of a basket.
    broker = ManualProtectedInput(tmp_path / 'input', clock=lambda: 100)
    payment = request(binding=binding(purpose=InputPurpose.PAYMENT))
    assert open_input(broker, payment).status == 'basket_unapproved'
    assert open_input(broker, payment, basket_approved=True).status == 'awaiting_owner'
    result = broker.complete('request-1', current=payment.binding, outcome=Completion.DONE,
                             capture_suspended=True, safe_to_resume=True)
    assert result.status == 'basket_unapproved' and not result.resume_once
    assert done(broker, payment, basket_approved=True).status == 'basket_unapproved'
    second = request(request_id='unlock', operation_id='unlock',
                     binding=binding(device_id='phone-2', purpose=InputPurpose.PHONE_UNLOCK,
                                     question_id='unlock-question'))
    assert broker.request(second, current=second.binding, channel_available=False,
                          capture_suspended=True).status == 'channel_unavailable'
    assert done(broker, second).status == 'channel_unavailable'


# spec: ANYM-998
def test_no_secret_transport_or_executor_or_secret_artifacts(tmp_path, monkeypatch, capsys, caplog):
    # No secret value enters this module, including when a hypothetical source/executor would fail or cancel.
    canary = 'fixture-protected-value-do-not-echo'
    def forbidden(*args, **kwargs):
        raise RuntimeError(canary)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(os, 'getenv', forbidden)
    broker = ManualProtectedInput(tmp_path / 'input', clock=lambda: 100)
    with pytest.raises(TypeError) as invalid:
        ProtectedInputRequest('request-1', 'operation-1', binding(), 200, value=canary)
    with caplog.at_level(logging.DEBUG):
        result = open_input(broker, request(transport=InputTransport.SECRET_REFERENCE))
        assert result.status == 'reference_transport_unavailable' and not result.resume_once
        open_input(broker)
        broker.complete('request-1', current=binding(), outcome=Completion.CANCELLED,
                         capture_suspended=True)
    shown = capsys.readouterr()
    all_artifacts = b''.join(path.read_bytes() for path in tmp_path.rglob('*') if path.is_file())
    assert canary not in str(invalid.value) + caplog.text + shown.out + shown.err + json.dumps(asdict(result))
    assert canary.encode() not in all_artifacts
    assert set(path.name for path in (tmp_path / 'input').iterdir()) == {'protected-input.sqlite3'}


# spec: ANYM-998
def test_safe_errors_and_private_metadata_journal(tmp_path):
    # Invalid metadata and storage failures must not echo raw request/error content.
    directory = tmp_path / 'input'
    broker = ManualProtectedInput(directory, clock=lambda: 100)
    open_input(broker)
    assert directory.stat().st_mode & 0o077 == 0
    assert (directory / 'protected-input.sqlite3').stat().st_mode & 0o077 == 0
    with pytest.raises(ProtectedInputError, match='^protected_input_invalid$'):
        binding(owner='raw private value\n')
    with pytest.raises(ProtectedInputError, match='^protected_input_invalid$'):
        request(expires_at=float('nan'))
    linked = tmp_path / 'linked'
    linked.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ProtectedInputError, match='^protected_input_unavailable$'):
        ManualProtectedInput(linked)
