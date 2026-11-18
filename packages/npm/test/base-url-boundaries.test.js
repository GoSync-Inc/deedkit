import {test} from 'node:test';
import assert from 'node:assert/strict';
import {AnyMsgClient} from '../lib/index.js';

// Synthetic hosts and a fake key only. Every fetch is a local recorder, so a refused base URL proves no request
// (and no credential) left the constructor. client.test.js already refuses the six baseline unsafe URLs and accepts
// loopback http; these pin what it does not.
const token = 'synthetic-key';

function recorder() {
  const seen = [];
  const fetch = async (url, request) => {
    seen.push({url: String(url), authorization: request.headers.Authorization});
    return Response.json({ok: true});
  };
  return {seen, fetch};
}

function refusedBeforeAnyRequest(baseUrl, message) {
  const {seen, fetch} = recorder();
  assert.throws(() => new AnyMsgClient({baseUrl, token, fetch}), {name: 'TypeError', message}, String(baseUrl));
  assert.deepEqual(seen, []);
}

// spec: ANYM-145
test('a base URL that does not parse is refused with exactly "Invalid AnyMsg base URL"', () => {
  for (const baseUrl of [undefined, '', 'not a url', 'archive.example/base', 'https://', '://archive.example',
    'https://archive example', 'http://[::1', 42])
    refusedBeforeAnyRequest(baseUrl, 'Invalid AnyMsg base URL');
});

// spec: ANYM-145
test('a base URL with a fragment is refused before any request, also beside a valid prefix', () => {
  for (const baseUrl of ['https://archive.example/base#fragment', 'https://archive.example#top',
    'https://archive.example/base/#/v1', 'http://127.0.0.1:9000/#synthetic-key'])
    refusedBeforeAnyRequest(baseUrl, 'Base URL must not contain credentials, query or fragment');
});

// spec: ANYM-145
test('trailing slashes become exactly one, so the proxy prefix is kept and operations land on <base>/v1/<op>', async () => {
  for (const [baseUrl, expected] of [
    ['https://archive.example', 'https://archive.example/v1/capabilities'],
    ['https://archive.example/', 'https://archive.example/v1/capabilities'],
    ['https://archive.example//', 'https://archive.example/v1/capabilities'],
    ['https://archive.example/base', 'https://archive.example/base/v1/capabilities'],
    ['https://archive.example/base/', 'https://archive.example/base/v1/capabilities'],
    ['https://archive.example/base///', 'https://archive.example/base/v1/capabilities'],
    ['https://archive.example/team/anymsg//', 'https://archive.example/team/anymsg/v1/capabilities'],
    ['http://localhost:9000/base/', 'http://localhost:9000/base/v1/capabilities']]) {
    const {seen, fetch} = recorder();
    const client = new AnyMsgClient({baseUrl, token, fetch});
    assert.deepEqual(await client.capabilities(), {ok: true});
    assert.deepEqual(seen, [{url: expected, authorization: `Bearer ${token}`}], baseUrl);
  }
});

// spec: ANYM-145
test('the /mcp and /v1 refusal holds after trailing slashes are removed', () => {
  for (const baseUrl of ['https://archive.example/mcp/', 'https://archive.example/v1//', 'https://archive.example/base/mcp///',
    'https://archive.example/base/v1/'])
    refusedBeforeAnyRequest(baseUrl, 'Base URL is the server root, without /mcp or /v1');
  // A prefix that only contains those letters is a prefix, not the endpoint.
  for (const baseUrl of ['https://archive.example/mcp-proxy/', 'https://archive.example/v1beta'])
    assert.doesNotThrow(() => new AnyMsgClient({baseUrl, token, fetch: recorder().fetch}));
});
