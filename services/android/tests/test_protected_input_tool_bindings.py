"""Owned blocking tool bindings and released journal compatibility; synthetic inputs only."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
import logging
import sqlite3

import pytest

from deedkit_android.protected_input import (
    Completion, InputPurpose, InputTransport, ManualProtectedInput, OwnedToolInputBinding,
    ProtectedInputError, ProtectedInputRequest,
)
from deedkit_android.protected_input_transport import LocalInputReference, enter_password_once
from test_protected_input import binding, request
from test_protected_input_transport import CANARY, fixture, run


def tool_binding(**changes):
    return replace(OwnedToolInputBinding(
        owner='owner', device_id='phone', task_id='task-1', native_thread_id='thread-1',
        native_turn_id='turn-1', native_request_id=12, native_call_id='call-1',
        package='example.shop', target_id='field-1', purpose=InputPurpose.LOGIN), **changes)


def tool_request(**changes):
    return replace(ProtectedInputRequest('request-1', 'operation-1', tool_binding(), 200), **changes)


def open_tool(broker, item=None):
    item = item or tool_request()
    return broker.request(item, current=item.binding, capture_suspended=True)


# spec: ANYM-998
@pytest.mark.parametrize('rpc_id', [12, '12'])
@pytest.mark.parametrize('namespace', [None, 'android'])
def test_owned_call_completion_keeps_real_typed_ids_without_question_or_item(tmp_path, rpc_id, namespace):
    item = tool_request(binding=tool_binding(native_request_id=rpc_id, native_namespace=namespace))
    broker = ManualProtectedInput(tmp_path / 'journal', clock=lambda: 100)
    assert open_tool(broker, item).status == 'awaiting_owner'
    result = broker.complete(item.request_id, current=item.binding, outcome=Completion.DONE,
                             capture_suspended=True, safe_to_resume=True)
    assert result.resume_once
    metadata = result.metadata()
    assert metadata == dict(status='owner_done', request_id='request-1', operation_id='operation-1',
        native_thread_id='thread-1', native_turn_id='turn-1', native_request_id=rpc_id,
        resume_once=True, binding_kind='owned_tool_call', binding_version=2, task_id='task-1',
        native_call_id='call-1', native_namespace=namespace, tool_name='android_order_control',
        operation='request_protected_input')
    assert type(metadata['native_request_id']) is type(rpc_id)
    assert 'question_id' not in asdict(item.binding) and 'native_item_id' not in asdict(item.binding)
    restarted = ManualProtectedInput(tmp_path / 'journal', clock=lambda: 101)
    assert not open_tool(restarted, item).resume_once
    assert not restarted.complete(item.request_id, current=item.binding, outcome=Completion.DONE,
                                  capture_suspended=True, safe_to_resume=True).resume_once


# spec: ANYM-998
@pytest.mark.parametrize('field,value', [
    ('tool_name', 'arbitrary_rpc'), ('operation', 'purchase'), ('native_request_id', True),
    ('binding_kind', 'request_user_input'), ('binding_version', 1), ('binding_version', True),
    ('native_call_id', ''), ('task_id', ''), ('native_namespace', ''),
])
def test_owned_binding_rejects_unregistered_or_untyped_identity(field, value):
    with pytest.raises(ProtectedInputError, match='^protected_input_invalid$'):
        tool_binding(**{field: value})


# spec: ANYM-998
@pytest.mark.parametrize('field,value', [
    ('owner', 'other'), ('device_id', 'other'), ('task_id', 'other'),
    ('native_thread_id', 'other'), ('native_turn_id', 'other'), ('native_request_id', '12'),
    ('native_request_id', 13), ('native_call_id', 'other'), ('native_namespace', 'other'),
])
def test_other_task_namespace_or_call_cannot_complete_owned_input(tmp_path, field, value):
    broker = ManualProtectedInput(tmp_path / 'journal', clock=lambda: 100)
    open_tool(broker)
    result = broker.complete('request-1', current=tool_binding(**{field: value}), outcome=Completion.DONE,
                             capture_suspended=True, safe_to_resume=True)
    assert result.status == 'binding_mismatch' and not result.resume_once
    assert result.operation_id is None and result.native_call_id is None
    assert broker.complete('request-1', current=tool_binding(), outcome=Completion.DONE,
                            capture_suspended=True, safe_to_resume=True).resume_once


def released_journal(directory, status):
    """Actual v1 wire shape: no discriminator/version tags and unchanged four-element question key."""
    directory.mkdir(mode=0o700)
    path = directory / 'protected-input.sqlite3'
    legacy = {'request_id': 'request-1', 'operation_id': 'operation-1', 'binding': {
        'owner': 'owner', 'device_id': 'phone', 'native_thread_id': 'thread-1',
        'native_turn_id': 'turn-1', 'question_id': 'question-1', 'package': 'example.shop',
        'target_id': 'field-1', 'purpose': 'login', 'native_request_id': 12, 'native_item_id': 'item-1'},
        'expires_at': 200, 'transport': 'manual'}
    wire = json.dumps(legacy)
    question_key = json.dumps(['turn-1', 12, 'item-1', 'question-1'])
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE inputs (
            request_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
            owner TEXT NOT NULL, device TEXT NOT NULL, thread TEXT NOT NULL, question_key TEXT NOT NULL,
            request TEXT NOT NULL, status TEXT NOT NULL, UNIQUE(owner, device, thread, question_key))''')
        db.execute('INSERT INTO inputs VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                   ('request-1', 'operation-1', 'owner', 'phone', 'thread-1', question_key, wire, status))
    path.chmod(0o600)
    return wire, question_key


# spec: ANYM-998
@pytest.mark.parametrize('status', ['awaiting_owner', 'unknown', 'rejected', 'cancelled', 'revoked', 'owner_done'])
def test_released_legacy_rows_and_tombstones_are_not_rewritten_or_coerced(tmp_path, status):
    directory = tmp_path / 'journal'
    wire, key = released_journal(directory, status)
    broker = ManualProtectedInput(directory, clock=lambda: 100)
    legacy = broker.request(request(), current=binding(), capture_suspended=True)
    assert legacy.status == status and not legacy.resume_once
    assert 'binding_kind' not in legacy.metadata() and 'native_call_id' not in legacy.metadata()
    assert open_tool(broker).status == 'identity_conflict'
    assert open_tool(broker, tool_request(request_id='other')).status == 'identity_conflict'
    if status in {'awaiting_owner', 'unknown', 'rejected', 'cancelled'}:
        other = tool_request(request_id='other', operation_id='other')
        assert open_tool(broker, other).status == 'device_busy'
    with sqlite3.connect(broker.path) as db:
        assert db.execute('SELECT request, question_key, status FROM inputs').fetchone() == (wire, key, status)
    restarted = ManualProtectedInput(directory, clock=lambda: 101)
    assert restarted.request(request(), current=binding(), capture_suspended=True).status == status


# spec: ANYM-998
def test_tool_tombstone_and_question_tombstone_stay_distinct_but_share_operation_exclusion(tmp_path):
    broker = ManualProtectedInput(tmp_path / 'journal', clock=lambda: 100)
    open_tool(broker)
    broker.revoke('request-1', current=tool_binding())
    assert open_tool(broker, tool_request(request_id='new', operation_id='new')).status == 'identity_conflict'
    assert broker.request(request(request_id='new'), current=binding(), capture_suspended=True).status == 'identity_conflict'
    legacy = request(request_id='legacy', operation_id='legacy')
    assert broker.request(legacy, current=legacy.binding, capture_suspended=True).status == 'awaiting_owner'
    assert broker.complete('legacy', current=tool_binding(), outcome=Completion.DONE,
                            capture_suspended=True, safe_to_resume=True).status == 'binding_mismatch'


# spec: ANYM-998
@pytest.mark.parametrize('corruption', ['unknown-version', 'missing-kind', 'missing-version'])
def test_unknown_or_incomplete_persisted_kind_never_becomes_legacy(tmp_path, corruption):
    broker = ManualProtectedInput(tmp_path / 'journal', clock=lambda: 100)
    open_tool(broker)
    with sqlite3.connect(broker.path) as db:
        data = json.loads(db.execute('SELECT request FROM inputs').fetchone()[0])
        if corruption == 'unknown-version':
            data['binding']['binding_version'] = 99
        else:
            data['binding'].pop('binding_kind' if corruption == 'missing-kind' else 'binding_version')
        db.execute('UPDATE inputs SET request=?', (json.dumps(data),))
    restarted = ManualProtectedInput(tmp_path / 'journal', clock=lambda: 101)
    with pytest.raises(ProtectedInputError, match='^protected_input_invalid$'):
        open_tool(restarted)
    other = tool_request(request_id='other', operation_id='other', binding=tool_binding(native_call_id='other'))
    assert open_tool(restarted, other).status == 'device_busy'


# spec: ANYM-998
def test_owned_completion_is_atomic_once_and_unknown_stays_held_after_restart(tmp_path):
    directory = tmp_path / 'journal'
    broker = ManualProtectedInput(directory, clock=lambda: 100)
    open_tool(broker)
    def complete(_):
        return ManualProtectedInput(directory, clock=lambda: 100).complete(
            'request-1', current=tool_binding(), outcome=Completion.DONE,
            capture_suspended=True, safe_to_resume=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(result.resume_once for result in pool.map(complete, range(8))) == 1
    other = tool_request(request_id='other', operation_id='other', binding=tool_binding(native_call_id='other'))
    open_tool(broker, other)
    broker.complete('other', current=other.binding, outcome=Completion.UNKNOWN, capture_suspended=True)
    restarted = ManualProtectedInput(directory, clock=lambda: 101)
    assert restarted.complete('other', current=other.binding, outcome=Completion.DONE,
                               capture_suspended=True, safe_to_resume=True).status == 'unknown'
    assert open_tool(restarted, replace(other, request_id='retry', operation_id='retry')).status == 'identity_conflict'


# spec: ANYM-998
def test_owned_call_without_actual_window_never_reads_reference_or_constructs_channel(tmp_path, monkeypatch, caplog):
    from deedkit_android import protected_input_transport as transport
    canary = 'fixture-private-value-never-export'
    def forbidden(*args, **kwargs):
        raise AssertionError(canary)
    monkeypatch.setattr(transport, '_read_reference', forbidden)
    monkeypatch.setattr(transport.ExistingInputChannel, '__init__', forbidden)
    journal = ManualProtectedInput(tmp_path / 'journal', clock=lambda: 100)
    item = tool_request(transport=InputTransport.LOCAL_REFERENCE)
    reference = LocalInputReference('reference-1', item.operation_id, item.binding, 200)
    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(enter_password_once(journal, item, reference, private_directory=tmp_path / 'absent'))
    assert result.status == 'protected_window_unavailable' and not result.resume_once
    assert canary not in repr(result) + json.dumps(result.metadata()) + caplog.text
    assert all(canary.encode() not in p.read_bytes() for p in tmp_path.rglob('*') if p.is_file())


# spec: ANYM-998
@pytest.mark.parametrize('lost_reply', [False, True])
def test_owned_tool_raw_fixture_correlates_original_call_and_never_replays_unknown(tmp_path, lost_reply, caplog):
    fx = fixture(tmp_path, lost_reply=lost_reply)
    directory, journal, old_item, old_reference, window, received, thread = fx
    item = replace(old_item, binding=tool_binding())
    reference = replace(old_reference, binding=item.binding)
    window.binding = item.binding
    with caplog.at_level(logging.DEBUG):
        result = run((directory, journal, item, reference, window, received, thread))
    metadata = result.metadata()
    assert metadata['native_call_id'] == 'call-1' and metadata['native_request_id'] == 12
    assert 'question_id' not in metadata and 'native_item_id' not in metadata
    assert len(received) == 1 and CANARY not in caplog.text + json.dumps(metadata)
    assert result.status == ('unknown' if lost_reply else 'owner_done')
    assert result.resume_once is (not lost_reply)
    if lost_reply:
        restarted = ManualProtectedInput(tmp_path / 'journal', clock=lambda: 101)
        retry = replace(item, request_id='new', operation_id='new')
        assert restarted._request(retry, current=item.binding, capture_suspended=True).status == 'identity_conflict'
        assert window.held and not window.released
