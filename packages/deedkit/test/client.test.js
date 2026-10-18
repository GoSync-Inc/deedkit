import {test} from 'node:test';
import assert from 'node:assert/strict';
import {inspect} from 'node:util';
import {DeedKitClient, DeedKitError, tools} from '../lib/index.js';

const token = 'synthetic-gateway-key';
const options = {baseUrl: 'https://gateway.example/deedkit', token};
const response = (body, status = 200) => Response.json(body, {status});

test('every catalog tool is a bearer POST to /v1/{tool} on the gateway prefix', async () => {
  const seen = [];
  const client = new DeedKitClient({...options, fetch: async (url, request) => {
    seen.push(String(url));
    assert.equal(request.method, 'POST');
    assert.equal(request.redirect, 'error');
    assert.equal(request.headers.Authorization, `Bearer ${token}`);
    assert.equal(request.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(request.body), {probe: 'synthetic'});
    return response({ok: true});
  }});
  for (const tool of tools) assert.deepEqual(await client.call(tool, {probe: 'synthetic'}), {ok: true});
  assert.deepEqual(seen, tools.map(tool => `https://gateway.example/deedkit/v1/${tool}`));
  assert.equal(tools.length, 67);
});

test('platform_capabilities helper sends an empty object', async () => {
  const client = new DeedKitClient({...options, fetch: async (url, request) => {
    assert.equal(String(url), 'https://gateway.example/deedkit/v1/platform_capabilities');
    assert.equal(request.body, '{}');
    return response({service: 'deedkit', tools: []});
  }});
  assert.equal((await client.platformCapabilities()).service, 'deedkit');
});

test('unknown tools and invalid arguments are rejected before any request', async () => {
  let calls = 0;
  const client = new DeedKitClient({...options, fetch: async () => { calls++; return response({}); }});
  for (const tool of ['search', 'send_message', 'messages_unknown', '../v1/messages_search', '', undefined])
    await assert.rejects(client.call(tool, {}), TypeError);
  for (const args of [[], null, 'x', {token: 'x'}, {value: NaN}, {nested: {value: Infinity}}, new Date()])
    await assert.rejects(client.call('messages_search', args), TypeError);
  await assert.rejects(client.call('messages_search', {query: 'x'.repeat(65536)}), TypeError);
  assert.equal(calls, 0);
});

test('only the upload chunk tool gets the larger request envelope', async () => {
  const client = new DeedKitClient({...options, fetch: async () => response({offset: 1})});
  const data = 'A'.repeat(300000);
  await client.call('messages_upload_file_chunk', {upload_id: 'up_' + '0'.repeat(32), offset: 0, data_base64: data});
  await assert.rejects(client.call('messages_get_file_upload', {upload_id: data}), TypeError);
  await assert.rejects(client.call('messages_upload_file_chunk', {data_base64: 'A'.repeat(360448)}), TypeError);
});

test('unsafe gateway URLs and malformed keys are refused before sending credentials', () => {
  for (const baseUrl of ['http://remote.example', 'https://user:pass@example.com', 'https://example.com/?key=abc',
    'https://example.com/#x', 'file:///tmp', 'https://example.com/mcp', 'https://example.com/v1/', 'not a url'])
    assert.throws(() => new DeedKitClient({...options, baseUrl}), TypeError);
  for (const baseUrl of ['http://127.0.0.1:9000', 'http://localhost:9000', 'http://[::1]:9000'])
    assert.doesNotThrow(() => new DeedKitClient({...options, baseUrl}));
  for (const bad of ['', 'two words', 'x'.repeat(513), 'tab\tkey', undefined])
    assert.throws(() => new DeedKitClient({...options, token: bad}), TypeError);
  for (const timeoutMs of [0, 120001, 1.5]) assert.throws(() => new DeedKitClient({...options, timeoutMs}), TypeError);
});

test('the key is not enumerable or printed', () => {
  const client = new DeedKitClient(options);
  assert.ok(!inspect(client).includes(token));
  assert.ok(!JSON.stringify(client).includes(token));
});

test('responses are bounded and must be a JSON object', async () => {
  const small = new DeedKitClient({...options, maxResponseBytes: 16, fetch: async () => new Response(
    new ReadableStream({start(controller) { controller.enqueue(new TextEncoder().encode('{"a":"' + 'x'.repeat(64) + '"}')); controller.close(); }}),
    {headers: {'content-type': 'application/json'}})});
  await assert.rejects(small.call('messages_search', {}), {code: 'response_too_large', outcome: 'rejected'});
  for (const body of [new Response('[]', {headers: {'content-type': 'application/json'}}),
    new Response('<html>', {headers: {'content-type': 'text/html'}})]) {
    const client = new DeedKitClient({...options, fetch: async () => body});
    await assert.rejects(client.call('messages_search', {}), error => error instanceof DeedKitError &&
      error.code === 'invalid_response' && error.outcome === 'rejected');
  }
});

test('timeouts and caller cancellation abort the request', async () => {
  const hang = async (url, request) => new Promise((resolve, reject) =>
    request.signal.addEventListener('abort', () => reject(new Error('aborted'))));
  const client = new DeedKitClient({...options, timeoutMs: 20, fetch: hang});
  await assert.rejects(client.call('messages_search', {}), {code: 'request_aborted', outcome: 'rejected'});
  const controller = new AbortController();
  controller.abort();
  let calls = 0;
  const idle = new DeedKitClient({...options, fetch: async () => { calls++; return response({}); }});
  await assert.rejects(idle.call('messages_send_message', {}, {signal: controller.signal}),
    {code: 'request_aborted', outcome: 'rejected'});
  assert.equal(calls, 0);
});
