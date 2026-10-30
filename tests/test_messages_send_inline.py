"""messages_send inline files (ANYM-547): small files go in the same call, staged through the upload store."""
import asyncio
import base64
import hashlib
import json
import logging
from pathlib import Path

import httpx
import jsonschema
import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.outgoing import CHILD_KEY_PREFIX
from messaging_service.send_intent import INLINE_BYTES, INLINE_PLACEHOLDER, SEND_BODY_BYTES
from messaging_service.store import APIError
from platform_contracts.agent import PLACEHOLDER_REQUEST_ID, child_key
from test_messages_send import CHAT, world as world  # noqa: F401  (the fixture)

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


# spec: ANYM-547
def test_a_send_keeps_a_shared_staged_upload_that_another_live_preview_still_names(world):
    item, _ = inline(4096)
    first = world.call(to=CHAT, text='Synthetic caption one', files=[item], dry_run=True)
    second = world.call(to=CHAT, text='Synthetic caption two', files=[item], dry_run=True)
    [shared] = uploads(world)
    assert world.drive(confirm_token=first['confirm_token'])['state'] == 'succeeded'
    [row] = uploads(world)
    assert row['state'] == 'ready' and row['live'] and (world.s.files / '.uploads' / row['id']).exists()
    # The other preview still sends; then nothing names the upload and it is deleted.
    assert world.drive(confirm_token=second['confirm_token'])['state'] == 'succeeded'
    assert [caption for _, caption in world.files] == ['Synthetic caption one', 'Synthetic caption two']
    [row] = uploads(world)
    assert row['id'] == shared['id'] and row['state'] == 'expired'
    assert not (world.s.files / '.uploads' / row['id']).exists()


# spec: ANYM-547
def test_a_fresh_dry_run_after_a_send_stages_the_released_file_again(world):
    item, _ = inline(4096)
    first = world.call(to=CHAT, files=[item], dry_run=True)
    assert world.drive(confirm_token=first['confirm_token'])['state'] == 'succeeded'
    fresh = world.call(to=CHAT, files=[item], dry_run=True)
    released, staged = uploads(world)
    assert released['state'] == 'expired' and staged['state'] == 'ready' and staged['live']
    assert world.drive(confirm_token=fresh['confirm_token'])['state'] == 'succeeded'
    assert [upload for upload, _ in world.files] == [released['id'], staged['id']]


# spec: ANYM-547
def test_any_number_of_preview_and_send_cycles_of_one_file_stage_it_afresh(world):
    item, _ = inline(1024)
    for cycle in range(18):
        preview = world.call(to=CHAT, text=f'Synthetic caption {cycle}', files=[item], dry_run=True)
        assert world.drive(confirm_token=preview['confirm_token'])['state'] == 'succeeded'
    rows = uploads(world)
    assert len(world.files) == 18 and len(rows) == 18 and all(row['state'] == 'expired' for row in rows)


# spec: ANYM-547
def test_following_a_preview_required_next_still_deletes_the_staged_bytes_after_the_send(world):
    item, _ = inline(2048)
    with pytest.raises(APIError) as error:
        world.call(to=CHAT, files=[item], text='*Synthetic*', format='markdown', request_id='inline-next-0001')
    assert error.value.code == 'preview_required'
    [row] = uploads(world)
    follow = error.value.next['arguments']
    assert follow['files'] == [{'upload_id': row['id']}]
    preview = world.call(**follow)
    assert world.drive(confirm_token=preview['confirm_token'])['state'] == 'succeeded'
    [row] = uploads(world)
    assert row['state'] == 'expired' and not (world.s.files / '.uploads' / row['id']).exists()
    # Only staging makes a child-key upload: a caller's prepare cannot claim one.
    with pytest.raises(APIError) as error:
        world.s.prepare_file_upload(world.key, 'send-fixture', CHAT, 'a.txt', 'text/plain', 1, '0' * 64,
                                    CHILD_KEY_PREFIX + 'a' * 64)
    assert error.value.code == 'invalid_upload'


# spec: ANYM-547
def test_a_full_payload_retry_of_a_completed_inline_send_replays_without_resolving_again(world, monkeypatch):
    import messaging_service.resolve_target as targets
    item, _ = inline(2048)
    call = {'to': CHAT, 'text': 'Synthetic caption', 'files': [item], 'request_id': 'inline-replay-0001'}
    assert world.drive(**call)['state'] == 'succeeded'

    def ambiguous(*args, **kwargs): raise APIError('ambiguous_target', 409)
    # The recipient no longer resolves (say, a second chat took its name): the bound intent still replays.
    monkeypatch.setattr(targets, 'resolve_target', ambiguous)
    assert world.call(**call)['state'] == 'succeeded'
    assert len(world.files) == 1 and len(uploads(world)) == 1


def received(app, path, body, token, step=4096):
    """The status and how many bytes of `body` the app read, sent in `step`-byte pieces with no Content-Length."""
    count = 0

    async def pieces():
        nonlocal count
        for start in range(0, len(body), step):
            count += len(body[start:start + step])
            yield body[start:start + step]

    async def post():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            return (await client.post(path, content=pieces(), headers={
                'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                'Accept': 'application/json, text/event-stream'})).status_code
    return asyncio.run(post()), count


# spec: ANYM-547
def test_more_than_16_kib_is_read_only_for_a_large_tool_of_a_key_that_may_upload(world):
    app = create_app(world.s, ['testserver'])
    item, _ = inline(40000)
    send_only = world.s.issue_key('send-only-agent', ['send-fixture'], permissions=['read', 'send'])
    call = json.dumps(mcp_call('messages_send', {'to': CHAT, 'files': [item], 'dry_run': True})).encode()
    listing = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list',
                          'params': {'_meta': {'pad': 'x' * 40000}}}).encode()
    rest = json.dumps({'to': CHAT, 'files': [item], 'dry_run': True}).encode()
    late = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
        'arguments': {'to': CHAT, 'files': [item], 'dry_run': True}, 'name': 'messages_send'}}).encode()
    # Nested metadata that looks like a large call claims nothing: only the top-level method and params.name count.
    nested = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {'_meta': {
        'method': 'tools/call', 'name': 'messages_send', 'pad': 'x' * 40000}}}).encode()
    for path, body, token in (('/mcp', call, send_only), ('/v1/messages_send', rest, send_only),
                              ('/mcp', listing, world.key), ('/mcp', late, world.key), ('/mcp', nested, world.key)):
        status, count = received(app, path, body, token)
        assert status == 413 and count <= 16384 + 4096, (path, token == world.key, count)
