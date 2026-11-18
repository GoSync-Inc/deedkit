import {test} from 'node:test';
import assert from 'node:assert/strict';
import {AnyMsgClient} from '../lib/index.js';

// Synthetic endpoint and fake key only: every fetch below is a local stub.
const options = {baseUrl: 'https://archive.example', token: 'synthetic-key'};
const MIB = 1024 * 1024;
const ok = () => Response.json({ok: true});
const tokenError = {name: 'TypeError', message: 'A non-empty Bearer token is required'};

// A JSON object of exactly `size` bytes, streamed in chunks and sent without a Content-Length.
function objectOfSize(size) {
  const head = new TextEncoder().encode('{"a":1'), tail = new TextEncoder().encode('}');
  let left = size - head.length - tail.length;
  const pad = new Uint8Array(64 * 1024).fill(0x20);
  return new Response(new ReadableStream({
    start(controller) { controller.enqueue(head); },
    pull(controller) {
      if (left <= 0) { controller.enqueue(tail); controller.close(); return; }
      const step = Math.min(left, pad.length); left -= step; controller.enqueue(pad.subarray(0, step));
    },
  }), {headers: {'content-type': 'application/json'}});
}

// spec: ANYM-146
test('the token must be 1 to 4096 printable ASCII characters without whitespace', async () => {
  for (const token of [undefined, null, '', ' ', 'two words', 'tab\there', 'line\nbreak', 'trailing ',
    'ключ', 'café', 'zero​width', 'nul\u0000', 'x'.repeat(4097), 42, ['synthetic-key']])
    assert.throws(() => new AnyMsgClient({...options, token}), tokenError, JSON.stringify(token));
  assert.throws(() => new AnyMsgClient({baseUrl: options.baseUrl}), tokenError);
  // The accepted edges reach the server exactly as given, as the Bearer credential.
  for (const token of ['!', '~', '!~', 'x'.repeat(4096)]) {
    const sent = [];
    const client = new AnyMsgClient({...options, token, fetch: async (_url, request) => {
      sent.push(request.headers.Authorization); return ok();
    }});
    assert.deepEqual(await client.capabilities(), {ok: true});
    assert.deepEqual(sent, [`Bearer ${token}`]);
  }
});

// spec: ANYM-146
test('timeoutMs must be an integer from 1 to 120000', () => {
  for (const timeoutMs of [0, -1, 120001, 1.5, NaN, Infinity, '1000', null])
    assert.throws(() => new AnyMsgClient({...options, timeoutMs}),
      {name: 'TypeError', message: 'timeoutMs must be an integer from 1 to 120000'}, String(timeoutMs));
  for (const timeoutMs of [1, 120000]) assert.doesNotThrow(() => new AnyMsgClient({...options, timeoutMs}));
});

// A request that waits for its abort signal; reports when the client gave up.
function waitingFetch(state) {
  return async (_url, {signal}) => new Promise((_resolve, reject) => {
    state.started = true;
    signal.addEventListener('abort', () => { state.aborted = true; reject(new Error('aborted')); });
  });
}

async function abortsAt(t, clientOptions, milliseconds) {
  t.mock.timers.enable({apis: ['setTimeout']});
  const state = {started: false, aborted: false};
  const client = new AnyMsgClient({...options, ...clientOptions, fetch: waitingFetch(state)});
  const settled = client.capabilities().then(() => 'resolved', error => error);
  const turn = () => new Promise(resolve => setImmediate(resolve));
  await turn();
  assert.ok(state.started);
  t.mock.timers.tick(milliseconds - 1);
  await turn();
  const early = state.aborted;
  t.mock.timers.tick(1);
  await turn();
  const onTime = state.aborted;
  if (!onTime) t.mock.timers.tick(10 * 60 * 1000);  // release a request a wrong deadline still holds
  const outcome = await settled;
  assert.equal(early, false, `aborted before ${milliseconds} ms`);
  assert.equal(onTime, true, `still waiting at ${milliseconds} ms`);
  assert.equal(outcome.code, 'request_aborted');
}

// spec: ANYM-146
test('without timeoutMs a request is aborted after 30 seconds, not before', async t => {
  await abortsAt(t, {}, 30000);
});

// spec: ANYM-146
test('an explicit timeoutMs replaces the default deadline', async t => {
  await abortsAt(t, {timeoutMs: 120000}, 120000);
});

// spec: ANYM-146
test('maxResponseBytes must be an integer from 1 to 33554432', async () => {
  for (const maxResponseBytes of [0, -1, 32 * MIB + 1, 1.5, NaN, '1024', null])
    assert.throws(() => new AnyMsgClient({...options, maxResponseBytes}),
      {name: 'TypeError', message: 'maxResponseBytes must be between 1 and 33554432'}, String(maxResponseBytes));
  // The upper edge is accepted and is the bound itself: a declared length of exactly 32 MiB is not refused,
  // one byte more is refused before any body is read.
  const declared = length => async () => new Response('{"declared":true}',
    {headers: {'content-type': 'application/json', 'content-length': String(length)}});
  const atMaximum = new AnyMsgClient({...options, maxResponseBytes: 32 * MIB, fetch: declared(32 * MIB)});
  assert.deepEqual(await atMaximum.capabilities(), {declared: true});
  const over = new AnyMsgClient({...options, maxResponseBytes: 32 * MIB, fetch: declared(32 * MIB + 1)});
  await assert.rejects(over.capabilities(), {code: 'response_too_large'});
  // The lower edge: a one-byte limit refuses even the smallest object.
  const one = new AnyMsgClient({...options, maxResponseBytes: 1, fetch: async () => objectOfSize(8)});
  await assert.rejects(one.capabilities(), {code: 'response_too_large'});
});

// spec: ANYM-146
test('without maxResponseBytes a streamed answer may be 4 MiB and not one byte more', async () => {
  const exact = new AnyMsgClient({...options, fetch: async () => objectOfSize(4 * MIB)});
  assert.deepEqual(await exact.capabilities(), {a: 1});
  const over = new AnyMsgClient({...options, fetch: async () => objectOfSize(4 * MIB + 1)});
  await assert.rejects(over.capabilities(), {code: 'response_too_large'});
});

// spec: ANYM-146
test('fetch must be a function and defaults to the global fetch at construction', async () => {
  for (const fetch of [null, 'fetch', {}, 42, true])
    assert.throws(() => new AnyMsgClient({...options, fetch}), {name: 'TypeError', message: 'fetch is required'});
  const original = Object.getOwnPropertyDescriptor(globalThis, 'fetch');
  const seen = [];
  try {
    globalThis.fetch = async url => { seen.push(String(url)); return ok(); };
    const client = new AnyMsgClient(options);
    assert.deepEqual(await client.capabilities(), {ok: true});
    assert.deepEqual(seen, ['https://archive.example/v1/capabilities']);
    delete globalThis.fetch;
    assert.throws(() => new AnyMsgClient(options), {name: 'TypeError', message: 'fetch is required'});
  } finally { Object.defineProperty(globalThis, 'fetch', original); }
  assert.equal(globalThis.fetch, original.value);
});
