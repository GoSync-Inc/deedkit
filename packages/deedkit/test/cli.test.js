import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFile} from 'node:child_process';
import {createHash} from 'node:crypto';
import {chmodSync, existsSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync} from 'node:fs';
import {createServer} from 'node:http';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {tools} from '../lib/index.js';

const bin = new URL('../bin/deedkit.js', import.meta.url).pathname;
// The gateway catalog plus platform_capabilities, so a new gateway tool needs no edit here (catalog.test.js
// checks the names); outside a repository checkout, the packaged list.
const catalogUrl = new URL('../../../services/gateway/src/agent_gateway/catalog.json', import.meta.url);
const toolCount = existsSync(catalogUrl) ? JSON.parse(readFileSync(catalogUrl, 'utf8')).length + 1 : tools.length;
const key = 'synthetic-gateway-key';

function run(args, env = {}) {
  return new Promise(resolve => execFile(process.execPath, [bin, ...args], {env: {PATH: process.env.PATH, ...env}},
    (error, stdout, stderr) => resolve({code: error ? error.code : 0, stdout, stderr})));
}

// A loopback fixture gateway: no external network, no real provider.
async function gateway(handler) {
  const requests = [];
  const server = createServer((request, response) => {
    let body = '';
    request.on('data', chunk => { body += chunk; });
    request.on('end', () => {
      requests.push({url: request.url, method: request.method, authorization: request.headers.authorization, body});
      const [status, value] = handler(request.url);
      response.writeHead(status, {'content-type': 'application/json'});
      response.end(JSON.stringify(value));
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return {url: `http://127.0.0.1:${server.address().port}`, requests, close: () => new Promise(r => { server.close(r); server.closeAllConnections(); })};
}

function fixture() {
  const dir = mkdtempSync(join(tmpdir(), 'deedkit-cli-'));
  const tokenFile = join(dir, 'agent.key');
  writeFileSync(tokenFile, key + '\n', {mode: 0o600});
  return {dir, tokenFile};
}

test('calls a tool with the private token file and prints the result', async () => {
  const {dir, tokenFile} = fixture();
  const server = await gateway(() => [200, {service: 'deedkit', tools: ['messages_search']}]);
  try {
    const result = await run(['platform_capabilities', '--url', server.url, '--token-file', tokenFile]);
    assert.equal(result.code, 0, result.stderr);
    assert.equal(JSON.parse(result.stdout).service, 'deedkit');
    assert.deepEqual(server.requests, [{url: '/v1/platform_capabilities', method: 'POST',
      authorization: `Bearer ${key}`, body: '{}'}]);
    const env = await run(['messages_search', '--json', '{"query":"x"}'], {DEEDKIT_URL: server.url, DEEDKIT_TOKEN_FILE: tokenFile});
    assert.equal(env.code, 0, env.stderr);
    assert.equal(server.requests[1].body, '{"query":"x"}');
  } finally { await server.close(); rmSync(dir, {recursive: true, force: true}); }
});

test('an unknown mutation outcome exits 3 with a do-not-retry result', async () => {
  const {dir, tokenFile} = fixture();
  const server = await gateway(() => [503, {schema_version: 1, error: 'mutation_outcome_unknown', outcome: 'unknown',
    automatic_retry: false}]);
  try {
    const result = await run(['messages_send_message', '--url', server.url, '--token-file', tokenFile,
      '--json', '{"request_id":"synthetic-0001"}']);
    assert.equal(result.code, 3);
    const output = JSON.parse(result.stderr);
    assert.equal(output.error, 'mutation_outcome_unknown');
    assert.equal(output.outcome, 'unknown');
    assert.equal(output.retryable, false);
    assert.equal(output.automatic_retry, false);
    assert.match(output.notice, /Do not retry/);
    assert.equal(server.requests.length, 1);
  } finally { await server.close(); rmSync(dir, {recursive: true, force: true}); }
});

test('a rejected call exits 1 with the gateway code unchanged', async () => {
  const {dir, tokenFile} = fixture();
  const server = await gateway(() => [403, {error: 'not_found_or_forbidden'}]);
  try {
    const result = await run(['calendar_agenda', '--url', server.url, '--token-file', tokenFile]);
    assert.equal(result.code, 1);
    assert.deepEqual(JSON.parse(result.stderr), {error: 'not_found_or_forbidden', status: 403, tool: 'calendar_agenda',
      outcome: 'rejected', retryable: false, automatic_retry: false});
  } finally { await server.close(); rmSync(dir, {recursive: true, force: true}); }
});

test('download writes the verified file named by --output', async () => {
  const {dir, tokenFile} = fixture();
  const ticket = 'Q'.repeat(43);
  const bytes = '{"synthetic":"file"}';
  let server, sha256 = '0'.repeat(64);
  server = await gateway(url => url.startsWith('/v1/files/') ? [200, {synthetic: 'file'}] : [200, {
    download_url: `${server.url}/v1/files/${ticket}`, bytes: bytes.length, filename: 'fixture.json', sha256}]);
  const download = output => run(['download', '--url', server.url, '--token-file', tokenFile, '--output', output,
    '--json', '{"attachment_id":"fixture"}']);
  try {
    // A wrong digest first: no file may remain.
    const output = join(dir, 'out.json');
    const bad = await download(output);
    assert.equal(bad.code, 1);
    assert.equal(JSON.parse(bad.stderr).error, 'download_integrity_error');
    assert.throws(() => readFileSync(output));
    assert.equal(server.requests[1].authorization, undefined);
    sha256 = createHash('sha256').update(bytes).digest('hex');
    const good = await download(output);
    assert.equal(good.code, 0, good.stderr);
    assert.deepEqual(JSON.parse(good.stdout), {path: output, bytes: bytes.length, sha256, filename: 'fixture.json'});
    assert.equal(readFileSync(output, 'utf8'), bytes);
  } finally { await server.close(); rmSync(dir, {recursive: true, force: true}); }
});

test('the token must be a private regular file and is never echoed', async () => {
  const {dir, tokenFile} = fixture();
  try {
    chmodSync(tokenFile, 0o644);
    const open = await run(['messages_search', '--url', 'https://gateway.example', '--token-file', tokenFile]);
    assert.equal(open.code, 2);
    assert.match(JSON.parse(open.stderr).detail, /chmod 600/);
    chmodSync(tokenFile, 0o600);
    const link = join(dir, 'link.key');
    symlinkSync(tokenFile, link);
    const linked = await run(['messages_search', '--url', 'https://gateway.example', '--token-file', link]);
    assert.equal(linked.code, 2);
    const large = join(dir, 'large.key');
    writeFileSync(large, key + ' '.repeat(4097 - key.length), {mode: 0o600});
    const oversized = await run(['messages_search', '--url', 'https://gateway.example', '--token-file', large]);
    assert.equal(oversized.code, 2);
    assert.match(JSON.parse(oversized.stderr).detail, /4096 bytes/);
    for (const result of [open, linked, oversized]) assert.ok(!(result.stdout + result.stderr).includes(key));
  } finally { rmSync(dir, {recursive: true, force: true}); }
});

test('usage errors exit 2 before any request', async () => {
  const {dir, tokenFile} = fixture();
  try {
    for (const args of [
      ['search', '--url', 'https://gateway.example', '--token-file', tokenFile],
      ['messages_search', '--url', 'http://remote.example', '--token-file', tokenFile],
      ['messages_search', '--url', 'https://gateway.example/mcp', '--token-file', tokenFile],
      ['messages_search', '--url', 'https://gateway.example', '--token-file', tokenFile, '--json', '{bad'],
      ['messages_search', '--url', 'https://gateway.example', '--token-file', tokenFile, '--json', '[1]'],
      ['messages_search', '--token-file', tokenFile],
      ['download', '--url', 'https://gateway.example', '--token-file', tokenFile],
    ]) {
      const result = await run(args);
      assert.equal(result.code, 2, args.join(' '));
      assert.equal(JSON.parse(result.stderr).error, 'usage_error');
    }
  } finally { rmSync(dir, {recursive: true, force: true}); }
});

test('tools, help and version need no network or token', async () => {
  const listed = JSON.parse((await run(['tools'])).stdout);
  assert.equal(listed.length, toolCount);
  assert.deepEqual(listed.find(row => row.name === 'messages_send_message'), {name: 'messages_send_message', read_only: false});
  assert.match((await run(['--help'])).stdout, /outcome unknown/);
  const version = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')).version;
  assert.equal((await run(['--version'])).stdout.trim(), version);
});
