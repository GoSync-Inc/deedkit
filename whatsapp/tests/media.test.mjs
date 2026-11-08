import test from 'node:test';
import assert from 'node:assert/strict';
import {createCipheriv,createHash,createHmac,hkdfSync} from 'node:crypto';
import {decrypt,mediaUrl,statusError} from '../media.mjs';

// Test encryption is independent from the production keys() helper.
function encrypted(plain,type='Document') {
  const mediaKey=Buffer.alloc(32,7),expanded=Buffer.from(hkdfSync('sha256',mediaKey,Buffer.alloc(0),`WhatsApp ${type} Keys`,112));
  const iv=expanded.subarray(0,16),aes=createCipheriv('aes-256-cbc',expanded.subarray(16,48),iv);
  const cipher=Buffer.concat([aes.update(plain),aes.final()]);
  const mac=createHmac('sha256',expanded.subarray(48,80)).update(iv).update(cipher).digest().subarray(0,10);
  const bytes=Buffer.concat([cipher,mac]);
  const metadata={type:'document',mediaKey:mediaKey.toString('base64'),size:plain.length,
    filehash:createHash('sha256').update(plain).digest('base64'),encFilehash:createHash('sha256').update(bytes).digest('base64')};
  return {bytes,metadata};
}
async function* parts(bytes,size){for(let p=0;p<bytes.length;p+=size)yield bytes.subarray(p,p+size);}
async function collect(input,metadata,limit){const out=[];for await(const chunk of decrypt(input,metadata,limit))out.push(chunk);return Buffer.concat(out);}
for(const size of [0,1,15,16,17,10000]) test('verified media at arbitrary cipher boundaries: '+size,async()=>{
  const input=Buffer.alloc(size,49),{bytes,metadata}=encrypted(input);
  assert.deepEqual(await collect(parts(bytes,7),metadata,size+1),input);
});
test('ciphertext, MAC, plaintext hash, truncation, wrong type and size fail closed',async()=>{
  const input=Buffer.alloc(1000,49),{bytes,metadata}=encrypted(input);
  const tampered=Buffer.from(bytes);tampered[50]^=1;
  const cases=[[tampered,metadata],[bytes.subarray(0,-1),metadata],[bytes,{...metadata,filehash:Buffer.alloc(32).toString('base64')}],
    [bytes,{...metadata,type:'image'}],[bytes,{...metadata,size:999}]];
  for(const [data,meta] of cases)await assert.rejects(()=>collect(parts(data,13),meta,2000),/media_(hash|size)_mismatch/);
  await assert.rejects(()=>collect(parts(bytes,64),metadata,500),/media_size_limit/);
});
test('host is fixed and redirect/URL injection is rejected',()=>{
  for(const path of ['/v/t62.7118-24/123.enc?x=1','/o1/v/t62.7118-24/123.enc?x=1','/o2/v/t62.7118-24/123.enc?x=1'])
    assert.equal(mediaUrl(path).hostname,'mmg.whatsapp.net');
  for(const path of ['https://evil.test/a','//evil.test','/v/\\evil','/v/file\nHost: evil','/v/file#frag'])
    assert.throws(()=>mediaUrl(path),/invalid_media_path/);
});
// spec: ANYM-815
test('403, 404 and 410 mean the copy is gone; other answers stay network errors',()=>{
  assert.equal(statusError(200),null);
  for(const status of [403,404,410])assert.equal(statusError(status),'media_unavailable');
  for(const status of [301,400,429,500,503])assert.equal(statusError(status),'media_network_error');
});
test('32MiB original emits bounded chunks and verifies hashes',async()=>{
  const {execFile}=await import('node:child_process');
  const {promisify}=await import('node:util');
  const {fileURLToPath}=await import('node:url');
  const {stdout}=await promisify(execFile)(process.execPath,[fileURLToPath(new URL('../media-benchmark.mjs',import.meta.url)),'32']);
  const result=JSON.parse(stdout);
  assert.equal(result.bytes,32*1024**2);assert.ok(result.max_chunk_bytes<=65536);
});
