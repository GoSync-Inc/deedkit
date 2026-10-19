import test from 'node:test';
import assert from 'node:assert/strict';
import {readStatus} from '../src/relay.mjs';
import {RECEIVER,clock,crash,downstream,relay,rows,signed,workspace} from './helpers.mjs';

class Crash extends Error{}
const crashAt=name=>step=>{if(step===name)throw new Crash(name);};
const until=async predicate=>{while(!predicate())await new Promise(resolve=>setImmediate(resolve));};

function setup(){
  const space=workspace(),time=clock(),fake=downstream([],{clock:time});
  const start=(options={})=>relay(space,{clock:time,transport:fake,...options});
  const hint=signed({},Math.floor(time.now()/1000));
  return {space,time,fake,start,hint,deliver:instance=>instance.receive(RECEIVER,hint.raw,hint.headers)};
}

// spec: ANYM-228
test('a crash before the pending commit leaves no row and the dispatcher retry fires once',async()=>{
  const {fake,start,deliver}=setup();
  const first=start({step:crashAt('beforePendingCommit')});
  assert.throws(()=>deliver(first),Crash);
  assert.equal(rows(first).length,0);
  crash(first);
  const restarted=start();
  assert.equal(deliver(restarted).status,202);
  await restarted.fireDue();await restarted.fireDue();
  assert.equal(fake.fires.length,1);
  await restarted.close();
});

// spec: ANYM-228
test('a crash after the pending commit but before the 2xx: the redelivery gets 2xx and the restart fires once',async()=>{
  const {fake,start,deliver}=setup();
  const first=start({step:crashAt('afterPendingCommit')});
  assert.throws(()=>deliver(first),Crash);
  crash(first);
  const restarted=start();
  assert.deepEqual(deliver(restarted),{status:200,body:{result:'duplicate'}});
  await restarted.fireDue();await restarted.fireDue();
  assert.equal(fake.fires.length,1);
  assert.equal(rows(restarted)[0].state,'accepted');
  await restarted.close();
});

// spec: ANYM-228
test('a crash after pending but before sending wrote nothing downstream; the restart fires once',async()=>{
  const {fake,start,deliver}=setup();
  const first=start({step:crashAt('beforeSending')});
  assert.equal(deliver(first).status,202);
  await assert.rejects(first.fireDue(),Crash);
  assert.equal(fake.fires.length,0);
  assert.equal(rows(first)[0].state,'pending');
  crash(first);
  const restarted=start();
  await restarted.fireDue();await restarted.fireDue();
  assert.equal(fake.fires.length,1);
  assert.equal(rows(restarted)[0].state,'accepted');
  await restarted.close();
});

// spec: ANYM-228
test('a crash after sending but before the response turns the row unknown with zero re-fires',async()=>{
  const {fake,start,deliver}=setup();
  fake.script.push('hang');
  const first=start();
  deliver(first);
  first.fireDue();
  await until(()=>fake.fires.length===1);
  assert.equal(rows(first)[0].state,'sending');
  crash(first);
  const restarted=start();
  assert.deepEqual([rows(restarted)[0].state,rows(restarted)[0].outcome],['unknown','interrupted_after_send']);
  assert.equal(deliver(restarted).status,200);
  await restarted.fireDue();
  assert.equal(fake.fires.length,1);
  await restarted.close();
});

// spec: ANYM-228
test('an accepted fire stores the vendor session ID and is never fired again, across redelivery and restart',async()=>{
  const {fake,start,deliver}=setup();
  const first=start();
  deliver(first);
  await first.fireDue();
  await first.close();
  const restarted=start();
  assert.equal(deliver(restarted).status,200);
  await restarted.fireDue();
  assert.equal(fake.fires.length,1);
  assert.deepEqual([rows(restarted)[0].state,rows(restarted)[0].vendor_id],['accepted','session_01SYNTHETIC']);
  await restarted.close();
});

// spec: ANYM-228
test('--drop-ack-once commits the row, drops one 2xx, and the redelivery of the same signed hint fires nothing more',async()=>{
  const {fake,start,deliver}=setup();
  const instance=start({dropAckOnce:true});
  assert.equal(deliver(instance).drop,true);
  assert.equal(deliver(instance).status,200);
  await instance.fireDue();
  assert.equal(fake.fires.length,1);
  await instance.close();
});

// spec: ANYM-228
test('50 hints for one subscription during one pending fire produce one fire carrying the highest through',async()=>{
  const {time,fake,start}=setup();
  const instance=start(),subscription_id=crypto.randomUUID(),now=Math.floor(time.now()/1000);
  const throughs=Array.from({length:50},(_,i)=>(i*37)%50+100);
  const answers=throughs.map(through=>{const h=signed({subscription_id,through},now);return instance.receive(RECEIVER,h.raw,h.headers).body.result;});
  assert.deepEqual([answers[0],new Set(answers.slice(1))],['accepted',new Set(['folded'])]);
  await instance.fireDue();await instance.fireDue();
  assert.equal(fake.fires.length,1);
  assert.equal(fake.fires[0].payload.through,Math.max(...throughs));
  assert.equal(instance.get('SELECT count(*) AS n FROM slots').n,1);
  const late=signed({subscription_id,through:999},now);
  assert.equal(instance.receive(RECEIVER,late.raw,late.headers).body.result,'accepted');
  await instance.close();
});

// spec: ANYM-228
test('a hint that arrives while its subscription is sending folds into that fire instead of starting a second',async()=>{
  const {time,fake,start}=setup();
  fake.script.push('hang');
  const instance=start(),subscription_id=crypto.randomUUID(),now=Math.floor(time.now()/1000);
  const first=signed({subscription_id,through:1},now),second=signed({subscription_id,through:2},now);
  instance.receive(RECEIVER,first.raw,first.headers);
  instance.fireDue();
  await until(()=>fake.fires.length===1);
  assert.equal(instance.receive(RECEIVER,second.raw,second.headers).body.result,'folded');
  await Promise.race([instance.fireDue(),new Promise(resolve=>setTimeout(resolve,20))]);
  assert.equal(fake.fires.length,1);
  crash(instance);
});

// spec: ANYM-228
test('wake-relay status reads without recovery: a live sending row stays sending and still records its session ID',async()=>{
  const {fake,start,deliver}=setup();
  let respond;
  fake.send=async request=>{fake.fires.push(request);return new Promise(resolve=>{respond=resolve;});};
  const instance=start();
  deliver(instance);
  const firing=instance.fireDue();
  await until(()=>fake.fires.length===1);
  assert.deepEqual(readStatus(instance.config.database).states,{sending:1});
  respond({status:200,headers:{},body:Buffer.from('{"claude_code_session_id":"session_01SYNTHETIC"}')});
  await firing;
  assert.deepEqual([rows(instance)[0].state,rows(instance)[0].vendor_id],['accepted','session_01SYNTHETIC']);
  await instance.close();
});
