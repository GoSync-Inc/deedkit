"""The Python client's messages_send (ANYM-547): MockTransport only, no server or provider."""
import base64
import json

import httpx
import pytest

from anymsg import AnyMsgError, Client


def client(seen, status=200, body=None):
    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(status, json=body or {'state': 'previewed'})
    return Client('https://archive.example', 'synthetic-token', transport=httpx.MockTransport(handler))


# spec: ANYM-547
def test_messages_send_posts_the_tool_arguments_with_an_inline_file_in_the_same_call():
    seen = []
    item = {'name': 'synthetic.bin', 'mime': 'application/octet-stream', 'base64': base64.b64encode(b'\x07' * 65536).decode()}
    assert client(seen).messages_send(to='chat_synthetic', text='Synthetic caption', files=[item],
                                      request_id='synthetic-inline-1', wait_s=5) == {'state': 'previewed'}
    # Arguments left as None are not sent.
    assert seen == [('/v1/messages_send', {'to': 'chat_synthetic', 'text': 'Synthetic caption', 'files': [item],
                                           'request_id': 'synthetic-inline-1', 'wait_s': 5})]


# spec: ANYM-547
def test_only_messages_send_takes_the_raised_body_and_old_calls_are_unchanged():
    seen = []
    near = {'name': 'synthetic.bin', 'mime': 'application/octet-stream', 'base64': base64.b64encode(b'\x01' * 262144).decode()}
    client(seen).messages_send(to='chat_synthetic', files=[near], dry_run=True)
    with pytest.raises(ValueError, match='request_too_large'):
        client(seen).messages_send(to='chat_synthetic', files=[{**near, 'base64': 'A' * 524288}])
    with pytest.raises(ValueError, match='request_too_large'):
        client(seen).preview_message(account='a', chat_id='chat_synthetic', text='x' * 20000)
    with pytest.raises(ValueError):
        client(seen).messages_send(to='chat_synthetic', text='x', wait_s=30)
    assert len(seen) == 1
    client(seen).send_message(account='a', chat_id='chat_synthetic', text='Synthetic', request_id='synthetic-chat-1')
    assert seen[1] == ('/v1/send_message', {'account': 'a', 'chat_id': 'chat_synthetic', 'text': 'Synthetic',
                                            'request_id': 'synthetic-chat-1', 'reply_to': ''})


# spec: ANYM-547
def test_a_messages_send_refusal_keeps_its_own_code():
    body = {'error': 'invalid_arguments', 'request_id': 'a' * 32,
            'field_errors': [{'field': 'files.0.base64', 'reason': 'max_bytes'}]}
    with pytest.raises(AnyMsgError) as error:
        client([], 400, body).messages_send(to='chat_synthetic', text='x', request_id='synthetic-inline-2')
    assert error.value.code == 'invalid_arguments'
