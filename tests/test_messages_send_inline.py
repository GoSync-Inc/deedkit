"""messages_send inline files (ANYM-547): small files go in the same call, staged through the upload store."""
import base64
import hashlib
import json
import logging
from pathlib import Path

import jsonschema
import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.outgoing import CHILD_KEY_PREFIX
from messaging_service.send_intent import INLINE_BYTES, INLINE_PLACEHOLDER, SEND_BODY_BYTES
from messaging_service.store import APIError
from platform_contracts.agent import PLACEHOLDER_REQUEST_ID, child_key
from test_messages_send import CHAT, world  # noqa: F401  (the fixture)

CATALOG = Path(__file__).resolve().parents[1] / 'services/gateway/src/agent_gateway/catalog.json'


def inline(size, name='synthetic.bin', seed=b'synthetic-inline'):
    data = (seed * (size // len(seed) + 1))[:size]
    return {'name': name, 'mime': 'application/octet-stream', 'base64': base64.b64encode(data).decode()}, data


def uploads(w):
    with w.s.pool.connection() as c:
        return c.execute('SELECT id,request_id,state,expires_at>clock_timestamp() AS live FROM file_uploads '
                         'ORDER BY created_at').fetchall()


def prepare_schema():
    catalog = json.loads(CATALOG.read_text())
    tools = catalog if isinstance(catalog, list) else catalog.get('tools', catalog.values())
    return next(t for t in tools if isinstance(t, dict) and t.get('name') == 'messages_prepare_file_upload')['inputSchema']


# spec: ANYM-547
def test_one_call_sends_a_64_kib_file_with_its_caption_and_deletes_the_staged_bytes(world):
    item, data = inline(65536)
    result = world.drive(to=CHAT, text='Synthetic caption', files=[item], request_id='inline-send-0001', wait_s=0)
    assert result['state'] == 'succeeded'
    assert len(world.files) == 1 and world.files[0][1] == 'Synthetic caption' and world.texts == []
    [row] = uploads(world)
    principal = world.s.authenticate(world.key)
    # The ANYM-544 child key of this call names the staged upload.
    assert row['request_id'] == CHILD_KEY_PREFIX + child_key(principal, 'messages_send', 'inline-send-0001', 'inline1', 1)
    assert row['state'] == 'expired' and not (world.s.files / '.uploads' / row['id']).exists()
    # A repeat of the same call reads the record: one send, no second upload.
    assert world.drive(to=CHAT, text='Synthetic caption', files=[item], request_id='inline-send-0001')['state'] == 'succeeded'
    assert len(world.files) == 1 and len(uploads(world)) == 1


# spec: ANYM-547
def test_a_dry_run_preview_and_its_confirm_token_send_the_inline_file_once(world):
    item, data = inline(4096, name='synthetic-note.txt')
    first = world.call(to=CHAT, files=[item], dry_run=True)
    again = world.call(to=CHAT, files=[item], dry_run=True)
    assert first['preview']['files'][0] == {'name': 'synthetic-note.txt', 'bytes': 4096, 'mode': 'document',
        'caption': '', 'sha256': hashlib.sha256(data).hexdigest(), 'mime': 'application/octet-stream'}
    # Repeated dry runs of one payload reuse their staged upload (a key holds at most 4 active ones).
    assert len(uploads(world)) == 1 and again['preview'] == first['preview']
    changed = {**item, 'base64': base64.b64encode(data[:-1] + b'!').decode()}
    with pytest.raises(APIError) as error:
        world.call(to=CHAT, files=[changed], confirm_token=first['confirm_token'])
    assert error.value.code == 'idempotency_conflict'
    result = world.drive(to=CHAT, files=[item], confirm_token=first['confirm_token'])
    assert result['state'] == 'succeeded' and len(world.files) == 1


# spec: ANYM-547
def test_a_failed_inline_send_keeps_its_staged_bytes_until_they_expire_like_uploads(world):
    item, _ = inline(2048)
    assert world.call(to=CHAT, files=[item], request_id='inline-fail-0001')['state'] == 'running'
    world.reject.update(row['id'] for row in uploads(world))
    result = world.drive(to=CHAT, files=[item], request_id='inline-fail-0001')
    assert result['state'] == 'failed' and result['resumable'] is True
    [row] = uploads(world)
    assert row['state'] == 'ready' and row['live'] and (world.s.files / '.uploads' / row['id']).exists()


def http(world):
    return TestClient(create_app(world.s, ['testserver'])), {
        'Authorization': 'Bearer ' + world.key, 'Accept': 'application/json, text/event-stream'}


def mcp_call(name, arguments):
    return {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}


def mcp_error(response):
    body = response.json()
    assert body['result']['isError'] is True
    text = body['result']['content'][0]['text']
    return json.loads(text[text.index('{'):])


# spec: ANYM-547
@pytest.mark.parametrize('size', [65536, INLINE_BYTES])
def test_http_and_mcp_take_inline_files_up_to_the_cap(world, size):
    item, data = inline(size)
    client, headers = http(world)
    with client:
        rest = client.post('/v1/messages_send', headers=headers, json={'to': CHAT, 'files': [item], 'dry_run': True})
        assert rest.status_code == 200 and rest.json()['preview']['files'][0]['bytes'] == size
        rpc = client.post('/mcp', headers=headers, json=mcp_call('messages_send', {'to': CHAT, 'files': [item],
                                                                                    'dry_run': True}))
        assert rpc.status_code == 200 and not rpc.json()['result']['isError']


# spec: ANYM-547
def test_one_byte_over_the_cap_names_the_upload_call_for_that_file_and_no_bytes(world, caplog):
    caplog.set_level(logging.DEBUG)
    small, _ = inline(1024, name='synthetic-small.txt')
    item, data = inline(INLINE_BYTES + 1 - 1024, name='synthetic-large.bin')
    arguments = {'to': CHAT, 'text': 'Synthetic caption', 'files': [small, item], 'request_id': 'inline-over-0001'}
    schema = prepare_schema()
    client, headers = http(world)
    with client:
        rest = client.post('/v1/messages_send', headers=headers, json=arguments)
        rpc = client.post('/mcp', headers=headers, json=mcp_call('messages_send', arguments))
    for detail in (rest.json(), mcp_error(rpc)):
        assert detail['error'] == 'invalid_arguments'
        assert detail['field_errors'] == [{'field': 'files.1.base64', 'reason': 'max_bytes'}]
        assert detail['next']['tool'] == 'messages_prepare_file_upload'
        args = detail['next']['arguments']
        assert args == {'account': 'send-fixture', 'chat_id': CHAT, 'filename': 'synthetic-large.bin',
                        'mime': 'application/octet-stream', 'bytes': len(data),
                        'sha256': hashlib.sha256(data).hexdigest(), 'request_id': PLACEHOLDER_REQUEST_ID}
        jsonschema.validate(args, schema)
    assert rest.status_code == 400
    for text in (rest.text, rpc.text, caplog.text):
        assert item['base64'][:64] not in text and small['base64'][:64] not in text
    assert uploads(world) == [] and world.files == []


# spec: ANYM-547
def test_only_messages_send_gets_the_raised_cap_and_errors_carry_no_bytes(world, caplog):
    caplog.set_level(logging.DEBUG)
    item, _ = inline(20000)
    client, headers = http(world)
    with client:
        # Every other route keeps its cap.
        assert client.post('/v1/send_message', headers=headers, json={'account': 'send-fixture', 'chat_id': CHAT,
            'text': item['base64'], 'request_id': 'inline-other-0001'}).status_code == 413
        assert client.post('/mcp', headers=headers, json=mcp_call('preview_message', {'account': 'send-fixture',
            'chat_id': CHAT, 'text': item['base64']})).status_code == 413
        over = {'to': CHAT, 'files': [{**item, 'base64': 'A' * SEND_BODY_BYTES}], 'dry_run': True}
        assert client.post('/v1/messages_send', headers=headers, json=over).status_code == 413
        # A live call without its key: the `next` repeats the call without the file's bytes.
        missing = client.post('/v1/messages_send', headers=headers, json={'to': CHAT, 'files': [item]})
        assert missing.status_code == 400
        assert missing.json()['next']['arguments']['files'] == [{**item, 'base64': INLINE_PLACEHOLDER}]
        # Rich format needs a preview: its `next` names the staged upload, not the bytes.
        rich = client.post('/v1/messages_send', headers=headers, json={'to': CHAT, 'files': [item], 'text': '*x*',
                                                                        'format': 'markdown', 'request_id': 'inline-rich-0001'})
        assert rich.json()['error'] == 'preview_required'
        [row] = uploads(world)
        assert rich.json()['next']['arguments']['files'] == [{'upload_id': row['id']}]
        # The staged form is the server's own: a caller's `inline` never reaches a send.
        spoof = client.post('/v1/messages_send', headers=headers, json={'to': CHAT, 'request_id': 'inline-spoof-0001',
            'files': [{'upload_id': row['id'], 'inline': {'name': 'x', 'mime': 'a/b', 'bytes': 1, 'sha256': '0' * 64}}]})
        assert spoof.status_code == 400 and spoof.json()['field_errors'][0]['field'].startswith('files')
    with pytest.raises(APIError) as error:
        world.call(to=CHAT, request_id='inline-spoof-0002', files=[{'upload_id': row['id'], 'inline': 'x'}])
    assert error.value.field_errors == [{'field': 'files.0.inline', 'reason': 'unknown_field'}]
    for text in (missing.text, rich.text, caplog.text):
        assert item['base64'][:64] not in text
