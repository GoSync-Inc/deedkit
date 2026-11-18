import {test} from 'node:test';
import assert from 'node:assert/strict';
import {AnyMsgClient} from '../lib/index.js';

// Synthetic endpoint and fake key only: every response below is built locally.
const options = {baseUrl: 'https://archive.example', token: 'synthetic-key'};
const JSON_TYPE = {'content-type': 'application/json'};

// A body that records how many chunks the client pulled and whether it cancelled the stream.
function trackedBody(chunks) {
  const state = {pulled: 0, cancelled: false};
  const encoded = chunks.map(chunk => typeof chunk === 'string' ? new TextEncoder().encode(chunk) : chunk);
  const stream = new ReadableStream({
    pull(controller) {
      if (state.pulled === encoded.length) { controller.close(); return; }
      controller.enqueue(encoded[state.pulled++]);
    },
    cancel() { state.cancelled = true; },
  }, {highWaterMark: 0});
  return {stream, state};
}

const answer = response => new AnyMsgClient({...options, maxResponseBytes: 16, fetch: async () => response});

// spec: ANYM-149
test('a declared Content-Length over the limit is refused and the body is cancelled unread', async () => {
  const {stream, state} = trackedBody(['{"a":1}']);
  const client = answer(new Response(stream, {headers: {...JSON_TYPE, 'content-length': '17'}}));
  await assert.rejects(client.capabilities(), {code: 'response_too_large', status: 200});
  assert.deepEqual(state, {pulled: 0, cancelled: true});
  // The declared length at the limit itself is read normally.
  const fits = trackedBody(['{"a":1}']);
  const atLimit = answer(new Response(fits.stream, {headers: {...JSON_TYPE, 'content-length': '16'}}));
  assert.deepEqual(await atLimit.capabilities(), {a: 1});
  assert.equal(fits.state.cancelled, false);
});

// spec: ANYM-149
test('without Content-Length reading stops at the first chunk past the limit and cancels the rest', async () => {
  const {stream, state} = trackedBody(['{"a":"', '0123456', '789', '"}', 'never read']);
  const client = answer(new Response(stream, {headers: JSON_TYPE}));
  await assert.rejects(client.capabilities(), {code: 'response_too_large'});
  // 6 + 7 + 3 = 16 bytes fit the 16-byte limit; the fourth chunk makes 18, so the fifth is never pulled.
  assert.deepEqual(state, {pulled: 4, cancelled: true});
});

// spec: ANYM-149
test('a body that is not declared as JSON is cancelled unread', async () => {
  for (const headers of [{}, {'content-type': 'text/plain'}, {'content-type': 'text/html; charset=utf-8'}]) {
    const {stream, state} = trackedBody(['{"a":1}']);
    await assert.rejects(answer(new Response(stream, {headers})).capabilities(), {code: 'invalid_response'});
    assert.deepEqual(state, {pulled: 0, cancelled: true}, JSON.stringify(headers));
  }
});

// spec: ANYM-149
test('a JSON response without a body stream is an invalid response', async () => {
  for (const status of [200, 500]) {
    const client = answer(new Response(null, {status, headers: JSON_TYPE}));
    await assert.rejects(client.capabilities(), {code: 'invalid_response', status});
  }
});

// spec: ANYM-149
test('bytes that are not strict UTF-8 are an invalid response, never replaced', async () => {
  for (const bytes of [[0x7b, 0x22, 0x61, 0x22, 0x3a, 0x22, 0xff, 0x22, 0x7d], // {"a":"\xff"}
    [0x7b, 0x22, 0x61, 0x22, 0x3a, 0x22, 0xc3, 0x22, 0x7d], // a truncated two-byte sequence
    [0x7b, 0x22, 0x61, 0x22, 0x3a, 0x22, 0xed, 0xa0, 0x80, 0x22, 0x7d], // an encoded surrogate
    [0x7b, 0x22, 0x61, 0x22, 0x3a, 0x22, 0xc0, 0xaf, 0x22, 0x7d]]) { // an overlong '/'
    const client = answer(new Response(new Uint8Array(bytes), {headers: JSON_TYPE}));
    await assert.rejects(client.capabilities(), {code: 'invalid_response'}, bytes.join(','));
  }
  // The same shape in valid UTF-8 is read.
  const valid = answer(new Response(new TextEncoder().encode('{"a":"é"}'), {headers: JSON_TYPE}));
  assert.deepEqual(await valid.capabilities(), {a: 'é'});
});

// spec: ANYM-149
test('only a JSON object is a response: primitives and null are invalid', async () => {
  for (const body of ['1', '"text"', 'true', 'null']) {
    const client = answer(new Response(body, {headers: JSON_TYPE}));
    await assert.rejects(client.capabilities(), {code: 'invalid_response'}, body);
  }
});
