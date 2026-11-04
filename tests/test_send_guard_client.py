"""ANYM-736: the Python SDK carries the send guard fields and keeps its two error codes."""
import json

import httpx
import pytest

from anymsg import AnyMsgError, Client

SEND = dict(account='synthetic', chat_id='chat_synthetic', text='Confirmed', request_id='synthetic-send-0001')


# spec: ANYM-736
def test_python_send_message_forwards_the_guard_fields_and_omits_them_when_absent():
    seen = []
    def transport(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'status': 'queued'})
    with Client('https://archive.example', 'synthetic-token', transport=httpx.MockTransport(transport)) as client:
        client.send_message(**SEND, expected_last_message_id='msg_synthetic', guard='outgoing_only')
        client.send_message(**SEND)
    assert seen[0]['expected_last_message_id'] == 'msg_synthetic' and seen[0]['guard'] == 'outgoing_only'
    assert 'expected_last_message_id' not in seen[1] and 'guard' not in seen[1]


# spec: ANYM-736
@pytest.mark.parametrize('code,status', [('chat_changed_since_seen', 409), ('chat_guard_unavailable', 503)])
def test_python_guard_errors_keep_their_code_and_are_never_retried(code, status):
    seen = []
    def transport(request):
        seen.append(request.url.path)
        return httpx.Response(status, json={'error': code, 'newer_count': 2, 'request_id': 'b' * 32})
    with Client('https://archive.example', 'synthetic-token', transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(AnyMsgError) as failure:
            client.send_message(**SEND, expected_last_message_id='msg_synthetic')
    assert failure.value.code == code and failure.value.request_id == 'b' * 32
    assert seen == ['/v1/send_message']


# spec: ANYM-748
def test_python_send_message_forwards_allow_duplicate_and_keeps_its_refusal_code():
    seen = []
    def transport(request):
        body = json.loads(request.content)
        seen.append(body)
        if 'allow_duplicate' in body:
            return httpx.Response(200, json={'status': 'queued'})
        return httpx.Response(409, json={'error': 'duplicate_send_unresolved', 'request_id': 'b' * 32,
                                         'earlier_request_id': 'synthetic-send-0000', 'earlier_status': 'unknown'})
    with Client('https://archive.example', 'synthetic-token', transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(AnyMsgError) as failure:
            client.send_message(**SEND)
        client.send_message(**SEND, allow_duplicate=True)
    assert failure.value.code == 'duplicate_send_unresolved' and failure.value.request_id == 'b' * 32
    assert 'allow_duplicate' not in seen[0] and seen[1]['allow_duplicate'] is True and len(seen) == 2


# spec: ANYM-736
def test_python_get_send_status_by_request_id_and_by_send_id():
    seen = []
    def transport(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'status': 'queued'})
    with Client('https://archive.example', 'synthetic-token', transport=httpx.MockTransport(transport)) as client:
        client.get_send_status('send_synthetic')
        client.get_send_status(request_id='synthetic-send-0001')
    assert seen == [{'send_id': 'send_synthetic'}, {'request_id': 'synthetic-send-0001'}]
