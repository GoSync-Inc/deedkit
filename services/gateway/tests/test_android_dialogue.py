"""Original native question IDs and natural answers traverse the existing Android tool only."""

import uuid

import pytest
from starlette.testclient import TestClient

from test_android import setup
from test_android_agent import call


# spec: ANYM-993
@pytest.mark.parametrize('native_id', [12, '12'])
def test_reply_keeps_typed_native_ids_and_natural_answer_without_injected_defaults(tmp_path, native_id):
    app, calls = setup(tmp_path)
    args = dict(action='reply', request_id=str(uuid.uuid4()), device_id='home', session_id='own-lease',
                native_thread_id='original-thread', native_turn_id='original-turn', native_request_id=native_id,
                item_id='original-item', answers={'original-question': ['Natural owner words.']})
    with TestClient(app) as client:
        result = call(client, **args)
        assert not result.get('isError'), result
        assert calls[-1] == ('android', 'agent', args)
        assert type(calls[-1][2]['native_request_id']) is type(native_id)


# spec: ANYM-993
def test_steer_and_unknown_reply_keep_original_request_readback_without_resend(tmp_path):
    app, calls = setup(tmp_path, interrupted=True)
    request_id = str(uuid.uuid4())
    with TestClient(app) as client:
        result = call(client, action='steer', request_id=request_id, device_id='home', session_id='lease',
                      native_thread_id='thread', native_turn_id='turn', message_id='original-message', text='Read status only.')
        assert result['isError']
        assert result['structuredContent']['request_id'] == request_id
        assert result['structuredContent']['next_action'] == 'read_android_agent_get_with_original_request_id'
        assert len(calls) == 1


# spec: ANYM-993
@pytest.mark.parametrize('extra', [{'native_request_id': True}, {'method': 'item/commandExecution/requestApproval'}])
def test_gateway_refuses_boolean_rpc_ids_and_generic_rpc_fields(tmp_path, extra):
    app, calls = setup(tmp_path)
    args = dict(action='reply', request_id=str(uuid.uuid4()), device_id='home', session_id='lease',
                native_thread_id='thread', native_turn_id='turn', native_request_id=12, item_id='item', answers={'q': ['yes']})
    with TestClient(app) as client:
        assert call(client, **{**args, **extra})['isError']
        assert calls == []


# spec: ANYM-993
def test_async_question_reply_has_native_message_binding_and_natural_text_without_fake_rpc(tmp_path):
    app, calls = setup(tmp_path)
    args = dict(action='reply',request_id=str(uuid.uuid4()),device_id='home',session_id='lease',
                native_thread_id='thread',native_turn_id='original-turn',item_id='original-async-message',
                message_id='stable-answer',text='  Natural exact owner words.\nKeep whitespace.  ')
    with TestClient(app) as client:
        result = call(client,**args)
        assert not result.get('isError'), result
        assert calls == [('android','agent',args)]
        assert 'native_request_id' not in calls[0][2] and 'answers' not in calls[0][2]
