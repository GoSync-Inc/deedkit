import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import net from 'node:net';
import {httpsTransport,tlsConnect} from '../src/transport.mjs';
import {RECEIVER,clock,downstream,httpServer,relay,rows,signed,workspace} from './helpers.mjs';

const GROK={type:'grok-automation',endpoint:'https://grok.example.test/automations/webhook/synthetic'};

function setup({adapter,fake,transport,max_hold_s=120,steps=[]}={}){
  const space=workspace(),time=clock();
  fake??=downstream([],{clock:time});
  const adapters=adapter?{claude:{...adapter,credential_file:adapter.type==='grok-automation'?space.grokSecret:space.claudeToken,account:'vendor'}}:undefined;
  const start=()=>relay(space,{clock:time,transport:transport??fake,step:name=>steps.push(name),configOverrides:{max_hold_s,adapters}});
  const instance=start();
  const deliver=(fields={})=>{const h=signed(fields,Math.floor(time.now()/1000));return {answer:instance.receive(RECEIVER,h.raw,h.headers),id:h.hint.hint_id};};
  return {space,time,fake,instance,start,deliver,steps};
}

// A real transport whose sockets come from `connects` in order (each a function returning a socket promise).
function scripted(connects){
  return httpsTransport({responseTimeoutMs:200,connectSocket:()=>connects.shift()()});
}

// spec: ANYM-228
test('connect refused, then 202: one accepted fire, and the failed attempt never reached sending',async()=>{
  const {time,fake,instance,deliver,steps}=setup();
  fake.script.push('refused');
  const {id}=deliver();
  await instance.fireDue();
  assert.deepEqual([instance.row(id).state,instance.row(id).attempts,fake.fires.length,steps.includes('afterSending')],['pending',0,0,false]);
  time.advance(1000);
  await instance.fireDue();
  assert.deepEqual([instance.row(id).state,instance.row(id).attempts,instance.row(id).connects,fake.fires.length],['accepted',1,1,1]);
  await instance.close();
});

// spec: ANYM-228
test('a real TLS handshake failure stays before sending; the next attempt gets 202 and is one accepted fire',async t=>{
  const garbage=net.createServer(socket=>socket.end('not a TLS server\r\n'));
  await new Promise(resolve=>garbage.listen(0,'127.0.0.1',resolve));
  const vendor=await httpServer((req,res)=>{res.writeHead(202);res.end();});
  t.after(async()=>{garbage.close();await vendor.close();});
  const transport=scripted([()=>tlsConnect(`https://localhost:${garbage.address().port}/`,{timeoutMs:2000}),vendor.connect]);
  const {time,instance,deliver}=setup({adapter:GROK,transport});
  const {id}=deliver();
  await instance.fireDue();
  assert.equal(instance.row(id).state,'pending');
  assert.match(instance.row(id).outcome,/^pre_send:/);
  assert.equal(vendor.requests.length,0);
  time.advance(1000);
  await instance.fireDue();
  assert.deepEqual([instance.row(id).state,instance.row(id).attempts,vendor.requests.length],['accepted',1,1]);
  await instance.close();
});

// spec: ANYM-228
test('Anthropic 429 with Retry-After 5 is retried after 5 s; a Retry-After beyond max_hold_s expires with zero further requests',async t=>{
  let answers=[[429,{'retry-after':'5'}],[200,{}],[429,{'retry-after':'500'}]];
  const vendor=await httpServer((req,res)=>{const [status,headers]=answers.shift();res.writeHead(status,headers);res.end('{"claude_code_session_id":"session_01SYNTHETIC"}');});
  t.after(()=>vendor.close());
  const transport=scripted(Array.from({length:4},()=>vendor.connect));
  const {time,instance,deliver}=setup({transport});
  const {id}=deliver();
  await instance.fireDue();
  assert.deepEqual([instance.row(id).state,instance.row(id).outcome,instance.row(id).retry_at-time.now()],['pending','rate_limited',5000]);
  time.advance(4999);await instance.fireDue();
  assert.equal(vendor.requests.length,1);
  time.advance(1);await instance.fireDue();
  assert.deepEqual([instance.row(id).state,instance.row(id).vendor_id,vendor.requests.length],['accepted','session_01SYNTHETIC',2]);
  const late=deliver();
  await instance.fireDue();
  assert.deepEqual([instance.row(late.id).state,instance.row(late.id).outcome],['expired','rate_limited_past_hold']);
  time.advance(600_000);await instance.fireDue();
  assert.equal(vendor.requests.length,3);
  await instance.close();
});

// spec: ANYM-228
test('500, 503, a response timeout after the write and a reset after the write are unknown with zero re-fires',async t=>{
  for(const behaviour of ['500','503','timeout','reset']){
    const vendor=await httpServer((req,res)=>{
      if(behaviour==='reset')return req.socket.destroy();
      if(behaviour!=='timeout'){res.writeHead(Number(behaviour));res.end();}
    });
    const {time,instance,start,deliver}=setup({transport:scripted([vendor.connect,vendor.connect])});
    const {id}=deliver();
    await instance.fireDue();
    assert.equal(instance.row(id).state,'unknown',behaviour);
    time.advance(600_000);await instance.fireDue();
    await instance.close();
    const restarted=start();
    await restarted.fireDue();
    assert.equal(vendor.requests.length,1,behaviour);
    assert.equal(restarted.row(id).state,'unknown');
    await restarted.close();await vendor.close();
  }
});

// spec: ANYM-228
test('documented rejections 400 and 404 (Anthropic) and 400 and 413 (Grok webhook) are failed with zero re-fires',async()=>{
  for(const [adapter,status] of [[undefined,400],[undefined,404],[GROK,400],[GROK,413]]){
    const {time,fake,instance,deliver}=setup({adapter});
    fake.script.push({status});
    const {id}=deliver();
    await instance.fireDue();
    time.advance(600_000);await instance.fireDue();
    assert.deepEqual([instance.row(id).state,instance.row(id).outcome,fake.fires.length],['failed',`http_${status}`,1]);
    await instance.close();
  }
});

// spec: ANYM-228
test('401 or 403 revoke the adapter: no further fires to it until the credential changes',async()=>{
  for(const status of [401,403]){
    const {space,fake,instance,deliver}=setup();
    fake.script.push({status});
    const {id}=deliver();
    await instance.fireDue();
    assert.equal(instance.row(id).state,'revoked');
    const after=deliver();
    assert.deepEqual([after.answer.status,after.answer.body.error,instance.row(after.id)],[503,'adapter_unavailable',undefined]);
    assert.equal(instance.status().revocations[0].status,status);
    await instance.fireDue();
    assert.equal(fake.fires.length,1);
    fs.writeFileSync(space.claudeToken,'sk-ant-oat01-regenerated-synthetic-token\n',{mode:0o600});
    const rotated=deliver();
    await instance.fireDue();
    assert.deepEqual([rotated.answer.status,instance.row(rotated.id).state,fake.fires.length],[202,'accepted',2]);
    await instance.close();
  }
});

// spec: ANYM-228
test('a pending hint whose adapter is then disabled locally or loses its credential fires nothing',async()=>{
  const {space,fake,instance,deliver}=setup();
  const {id}=deliver();
  await instance.close();
  const disabled=relay(space,{clock:clock(),transport:fake,autoFire:false,configOverrides:{adapters:{claude:{type:'claude-routine',
    routine_id:'trig_01SYNTHETICROUTINE',credential_file:space.claudeToken,enabled:false}}}});
  await disabled.fireDue();
  assert.deepEqual([disabled.row(id).state,disabled.row(id).outcome,fake.fires.length],['revoked','adapter_disabled',0]);
  await disabled.close();
  const other=setup();
  const pending=other.deliver();
  fs.rmSync(other.space.claudeToken);
  await other.instance.fireDue();
  assert.deepEqual([other.instance.row(pending.id).state,other.fake.fires.length],['revoked',0]);
  await other.instance.close();
});

// spec: ANYM-228
test('a pending hint past max_hold_s expires with zero fires',async()=>{
  const {time,fake,instance,deliver}=setup();
  const {id}=deliver();
  time.advance(120_000);
  await instance.fireDue();
  assert.deepEqual([instance.row(id).state,instance.row(id).outcome,fake.fires.length,fake.connects],['expired','hold_elapsed',0,0]);
  assert.equal(rows(instance).length,1);
  await instance.close();
});

// spec: ANYM-228
test('a credential removed while the connection is being set up stops the fire before sending',async()=>{
  const {space,instance,deliver}=setup();
  let release,writes=0;
  instance.transport={connect:()=>new Promise(resolve=>{release=resolve;}),send:async()=>{writes++;return {status:200,headers:{},body:Buffer.alloc(0)};},close(){}};
  const {id}=deliver();
  const firing=instance.fireDue();
  await new Promise(resolve=>setImmediate(resolve));
  fs.rmSync(space.claudeToken);
  release({});
  await firing;
  assert.deepEqual([instance.row(id).state,instance.row(id).outcome,instance.row(id).attempts,writes],['revoked','adapter_disabled',0,0]);
  await instance.close();
});

// spec: ANYM-228
test('a malformed Grok secret fails locally with no sending transition and nothing written',async()=>{
  for(const secret of ['whsec_!!!','whsec_','whsec_c2hvcnQ=']){
    const {space,fake,instance,deliver}=setup({adapter:GROK});
    fs.writeFileSync(space.grokSecret,secret,{mode:0o600});
    const {id}=deliver();
    await instance.fireDue();
    assert.deepEqual([instance.row(id).state,instance.row(id).outcome,instance.row(id).attempts,fake.fires.length],['failed','invalid_credential',0,0],secret);
    await instance.close();
  }
});

// spec: ANYM-228
test('a Claude 2xx without a valid session ID is unknown, not accepted, and is never re-fired',async()=>{
  for(const body of ['','not json','{"claude_code_session_id":""}','{"type":"routine_fire"}']){
    const {time,fake,instance,deliver}=setup();
    fake.script.push({status:200,body:Buffer.from(body)});
    const {id}=deliver();
    await instance.fireDue();
    time.advance(600_000);await instance.fireDue();
    assert.deepEqual([instance.row(id).state,instance.row(id).outcome,instance.row(id).vendor_id,fake.fires.length],['unknown','http_200_without_session_id',null,1],body);
    await instance.close();
  }
});
