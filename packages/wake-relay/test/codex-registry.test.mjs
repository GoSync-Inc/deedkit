// ANYM-849: the pull waker in registry mode reads each key's own wake targets (ANYM-848) against the synthetic DeedKit
// MCP server of codex-waker.test.mjs and a fake `codex` first on PATH.
import test,{afterEach} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import {wakeMessage} from '../src/codex.mjs';
import {deedkitClient} from '../src/mcp.mjs';
import {ConfigError,Watcher,challengeMessage,loadWatchConfig,readWatchStatus} from '../src/watch.mjs';
import {SECRET_TEXT,SUB,closeOpened,deedkit,fakeCodex,opened} from './fake-deedkit.mjs';
import {workspace} from './helpers.mjs';

const TOKENS=['dk_synthetic_registry_key_a_0123','dk_synthetic_registry_key_b_4567'];
const THREAD='7d3e2f1a-9b8c-4d6e-a5f4-3c2b1a0e9d8c',THREAD2='Second project chat',THREAD3='Third agent chat';
const SUB2='1c7a6d2f-4b3e-4f9a-8d8c-2b3c4d5e6f70',SUB3='2d8b7e3a-5c4f-4a0b-9e9d-3c4d5e6f7081';
const T1='a1000000-0000-4000-8000-000000000001',T2='a2000000-0000-4000-8000-000000000002',
  T3='a3000000-0000-4000-8000-000000000003';
const LABEL='Synthetic private chat label';
const TOOLS=['messages_get_subscription_events','messages_list_wake_targets','messages_confirm_wake_target'];
afterEach(closeOpened);

// One wake target as wake_targets._view returns it.
const target=(id,thread,subscriptions,fields={})=>({wake_target_id:id,version:1,kind:'codex_thread',thread,label:LABEL,
  hourly_budget:12,state:'enabled',disabled_reason:null,confirmation:null,subscription_ids:subscriptions,
  bound:subscriptions.length,created_at:'2026-11-09T12:00:00Z',updated_at:'2026-11-09T12:00:00Z',...fields});

function setup({keys=[{}],mode='ok'}={}){
  const space=workspace(),codex=fakeCodex(space.dir,mode);
  const files=keys.map((_,index)=>space.file(`agent-${index}.key`,TOKENS[index]+'\n'));
  const raw={database:path.join(space.dir,'watch.sqlite'),deedkit_url:'https://deedkit.example/mcp',codex_path:codex.file,
    keys:keys.map((key,index)=>({key_file:files[index],...key}))};
  const time={t:Date.UTC(2026,10,9,12),now(){return this.t;},advance(ms){this.t+=ms;}};
  const logs=[];
  return {space,codex,files,raw,time,logs,
    async server(){
      this.dk=await deedkit({tools:TOOLS});
      keys.forEach((_,index)=>{this.dk.state.keys[TOKENS[index]]={targets:[]};});
      return this.dk;
    },
    key(index=0){return this.dk.state.keys[TOKENS[index]];},
    start(options={}){
      const config=loadWatchConfig(options.raw??raw);delete options.raw;
      const watcher=new Watcher({config,clock:time,log:entry=>logs.push(entry),
        clientFor:key=>deedkitClient({url:this.dk.url,keyFile:key.keyFile,tool:key.tool,request:http.request}),...options});
      opened.push(()=>{if(!watcher.closed)return watcher.close();});
      return watcher;
    },
    challenges(){return codex.calls().filter(argv=>argv[4].startsWith('DeedKit wake target '));},
    wakes(){return codex.calls().filter(argv=>argv[4].startsWith('DeedKit subscription '));},
    // The chat answers its challenge: it sends the code it received to confirm_wake_target (here: set on the fake).
    answer(index,id,code){
      const row=this.key(index).targets.find(row=>row?.wake_target_id===id);
      row.confirmation=code??this.challenges().reverse().map(argv=>argv[4]).find(text=>text.includes(id)).match(/code ([0-9a-f]{32})/)[1];
    },
    entries(event){return logs.filter(entry=>entry.event===event);}};
}
const held=watcher=>watcher.all(`SELECT subscription_id,state,outcome FROM wakes ORDER BY id`).map(row=>({...row}));

// Registers, challenges and confirms each listed target: after this the targets are rules.
async function confirmed(ctx,watcher,pairs){
  await watcher.pollOnce();
  for(const [index,id] of pairs)ctx.answer(index,id);
  await watcher.pollOnce();
}

// spec: ANYM-849
test('an unchanged ANYM-844 config against a gateway without the registry tools wakes as before',async()=>{
  const space=workspace(),codex=fakeCodex(space.dir),key=space.file('deedkit.key',TOKENS[0]+'\n');
  const raw={database:path.join(space.dir,'watch.sqlite'),deedkit_url:'https://deedkit.example/mcp',key_file:key,
    watches:[{subscription_id:SUB,thread:THREAD}]};
  const config=loadWatchConfig(raw,{env:{PATH:space.dir}});
  assert.equal(config.mode,'legacy');
  const dk=await deedkit();
  const watcher=new Watcher({config,clock:{now:()=>Date.UTC(2026,10,9,12)},
    client:deedkitClient({url:dk.url,keyFile:key,request:http.request})});
  opened.push(()=>{if(!watcher.closed)return watcher.close();});
  await watcher.check();
  await watcher.pollOnce();dk.push(1);await watcher.pollOnce();
  assert.deepEqual(codex.calls(),[['queue','--thread',THREAD,'--message',wakeMessage(SUB,1),'--resume']]);
  assert.ok(!dk.state.calls.some(name=>name.includes('wake_target')),'legacy mode never calls a registry tool');
});

// spec: ANYM-849
test('a file with both keys and watches is refused at load; keys and allowed_threads are checked like the 844 fields',()=>{
  const ctx=setup();
  const both={...ctx.raw,watches:[{subscription_id:SUB,thread:THREAD}]};
  assert.throws(()=>loadWatchConfig(both),error=>error instanceof ConfigError&&error.code==='watches_with_keys');
  assert.throws(()=>loadWatchConfig({...ctx.raw,key_file:ctx.files[0]}),error=>error.code==='watches_with_keys');
  const link=path.join(ctx.space.dir,'key-link');fs.symlinkSync(ctx.files[0],link);
  for(const [keys,field] of [[[],'keys'],[[{key_file:link}],'keys[0].key_file'],
    [[{key_file:ctx.files[0],allowed_threads:['-c']}],'keys[0].allowed_threads'],
    [[{key_file:ctx.files[0],allowed_threads:'x'}],'keys[0].allowed_threads']]){
    assert.throws(()=>loadWatchConfig({...ctx.raw,keys}),error=>error instanceof ConfigError&&error.field===field,field);
  }
  const config=loadWatchConfig({...ctx.raw,keys:[{key_file:ctx.files[0],allowed_threads:[THREAD]}]});
  assert.equal(config.mode,'registry');
  assert.deepEqual(config.keys.map(key=>key.allowedThreads),[[THREAD]]);
});

// spec: ANYM-849
test('two keys, one without messages_list_wake_targets: that key is skipped tool_not_granted, the other key runs',async()=>{
  const ctx=setup({keys:[{},{}]});const dk=await ctx.server();
  ctx.key(0).tools=['messages_get_subscription_events'];
  ctx.key(0).targets=[target(T1,THREAD,[SUB])];
  ctx.key(1).targets=[target(T2,THREAD2,[SUB2])];
  const watcher=ctx.start();
  await confirmed(ctx,watcher,[[1,T2]]);
  dk.push(1,{},SUB2);dk.push(1,{},SUB);
  await watcher.pollOnce();
  assert.deepEqual(ctx.wakes(),[['queue','--thread',THREAD2,'--message',wakeMessage(SUB2,1),'--resume']]);
  assert.ok(ctx.entries('key_skipped').every(entry=>entry.key===0&&entry.error==='tool_not_granted'));
  assert.equal(ctx.entries('key_skipped').length,3,'logged at each poll');
  assert.ok(!ctx.challenges().some(argv=>argv[4].includes(T1)));
});

// spec: ANYM-849
test('hostile threads, non-UUID subscriptions and malformed targets never reach argv; the next valid target still runs',async()=>{
  const ctx=setup();const dk=await ctx.server();
  ctx.key().targets=[
    target(T1,'-c',[SUB]),
    target('b1000000-0000-4000-8000-000000000001','$(touch x)',[SUB]),
    target('b2000000-0000-4000-8000-000000000002','x'.repeat(65),[SUB]),
    target('b3000000-0000-4000-8000-000000000003',THREAD,['not-a-uuid']),
    target('b4000000-0000-4000-8000-000000000004',THREAD,[SUB],{hourly_budget:61}),
    target('line\nbreak',THREAD,[SUB]),
    target({id:1},THREAD,[SUB]),
    null,
    target('b5000000-0000-4000-8000-000000000005',THREAD,SUB),
    target(T2,THREAD2,[SUB2,SUB2]),
  ];
  const watcher=ctx.start();
  await confirmed(ctx,watcher,[[0,T2]]);
  dk.push(1,{},SUB2);
  await watcher.pollOnce();
  assert.deepEqual(ctx.challenges(),[['queue','--thread',THREAD2,'--message',ctx.challenges()[0][4],'--resume']]);
  assert.deepEqual(ctx.wakes(),[['queue','--thread',THREAD2,'--message',wakeMessage(SUB2,1),'--resume']],
    'a subscription listed twice is used once');
  const dropped=ctx.entries('invalid_wake_target').slice(0,9);
  assert.equal(dropped.length,9);
  assert.deepEqual(dropped.map(entry=>entry.wake_target_id),[T1,'b1000000-0000-4000-8000-000000000001',
    'b2000000-0000-4000-8000-000000000002','b3000000-0000-4000-8000-000000000003',
    'b4000000-0000-4000-8000-000000000004',undefined,undefined,undefined,'b5000000-0000-4000-8000-000000000005']);
  assert.ok(!fs.existsSync(path.join(process.cwd(),'x')));
  const logged=JSON.stringify(ctx.logs);
  assert.ok(!logged.includes('line\n')&&!logged.includes('touch')&&!logged.includes(LABEL)&&!logged.includes(TOKENS[0]));
  // A malformed answer drops the key's registry for the poll.
  for(const answer of [{items:'x'},{items:null},[],{}]){
    ctx.key().answer=answer;ctx.logs.length=0;
    await watcher.pollOnce();
    assert.deepEqual(ctx.entries('key_skipped').map(entry=>entry.error),['invalid_registry'],JSON.stringify(answer));
  }
});

// spec: ANYM-849
test('a target whose chat never confirms gets one challenge and no wake; a wrong code waits; the right code makes it a rule',async()=>{
  const ctx=setup();const dk=await ctx.server();
  ctx.key().targets=[target(T1,THREAD,[SUB])];
  const watcher=ctx.start();
  for(let i=0;i<3;i++){dk.push(1);await watcher.pollOnce();}
  assert.equal(ctx.challenges().length,1,'one challenge per target and thread');
  const [argv]=ctx.challenges(),code=argv[4].match(/code ([0-9a-f]{32})/)[1];
  assert.deepEqual(argv,['queue','--thread',THREAD,'--message',challengeMessage(T1,code),'--resume']);
  assert.equal(argv[4],`DeedKit wake target ${T1} was registered for this chat. Only if this chat registered it, call `+
    `confirm_wake_target with wake_target_id ${T1} and code ${code}; otherwise ignore this message.`);
  assert.equal(ctx.wakes().length,0,'no event wake before the chat confirms');
  assert.ok(!JSON.stringify(watcher.all('SELECT * FROM challenges')).includes(code),'only the SHA-256 is kept');
  ctx.answer(0,T1,'0'.repeat(32));
  await watcher.pollOnce();
  assert.equal(ctx.wakes().length,0,'a wrong code keeps it unconfirmed');
  assert.equal(watcher.status().wake_targets.find(row=>row.wake_target_id===T1).outcome,'unconfirmed');
  ctx.answer(0,T1,code);
  await watcher.pollOnce();
  dk.push(1);await watcher.pollOnce();
  assert.equal(ctx.wakes().length,1);
  assert.equal(ctx.challenges().length,1);
  // An overwrite after the local confirmation changes nothing.
  ctx.answer(0,T1,'f'.repeat(32));
  dk.state.acked=dk.state.matched;await watcher.pollOnce();
  dk.push(1);await watcher.pollOnce();
  assert.equal(ctx.wakes().length,2);
  assert.equal(ctx.challenges().length,1);
});

// spec: ANYM-849
test('allowed_threads blocks a thread outside it: no challenge, no wake, thread_not_allowed',async()=>{
  const ctx=setup({keys:[{allowed_threads:[THREAD]}]});const dk=await ctx.server();
  ctx.key().targets=[target(T1,THREAD2,[SUB]),target(T2,THREAD,[SUB2])];
  const watcher=ctx.start();
  await confirmed(ctx,watcher,[[0,T2]]);
  dk.push(1,{},SUB);dk.push(1,{},SUB2);await watcher.pollOnce();
  assert.ok(ctx.codex.calls().every(argv=>argv[2]===THREAD),'nothing reaches the thread outside the list');
  assert.equal(ctx.wakes().length,1);
  assert.equal(watcher.status().wake_targets.find(row=>row.wake_target_id===T1).outcome,'thread_not_allowed');
});

// spec: ANYM-849
test('a paused, disabled, deleted or unbound target turns its held wakes into dropped_config rule_removed',async()=>{
  for(const change of ['paused','operator_disabled','deleted','unbound']){
    const ctx=setup();const dk=await ctx.server();
    ctx.key().targets=[target(T1,THREAD,[SUB],{hourly_budget:1})];
    const watcher=ctx.start();
    await watcher.pollOnce();ctx.answer(0,T1);
    ctx.time.advance(3600_001);await watcher.pollOnce();        // the challenge's slot has expired
    dk.push(1);await watcher.pollOnce();                         // takes the one slot
    dk.state.acked=dk.state.matched;await watcher.pollOnce();
    dk.push(1);await watcher.pollOnce();                         // held
    assert.equal(held(watcher).at(-1).state,'budget_held',change);
    const row=ctx.key().targets[0];
    if(change==='deleted')ctx.key().targets=[];
    else if(change==='unbound'){row.subscription_ids=[];row.bound=0;}
    else row.state=change;
    ctx.time.advance(3600_001);await watcher.pollOnce();
    assert.deepEqual(held(watcher).at(-1),{subscription_id:SUB,state:'dropped_config',outcome:'rule_removed'},change);
    assert.equal(ctx.wakes().length,1,`${change}: the held wake is never called`);
    await watcher.close();
  }
});

// spec: ANYM-849
test('a thread change drops held wakes and challenges the new thread before any wake',async()=>{
  const ctx=setup();const dk=await ctx.server();
  ctx.key().targets=[target(T1,THREAD,[SUB],{hourly_budget:1})];
  const watcher=ctx.start();
  await watcher.pollOnce();ctx.answer(0,T1);
  ctx.time.advance(3600_001);await watcher.pollOnce();
  dk.push(1);await watcher.pollOnce();
  dk.state.acked=dk.state.matched;await watcher.pollOnce();
  dk.push(1);await watcher.pollOnce();
  assert.equal(held(watcher).at(-1).state,'budget_held');
  ctx.key().targets[0].thread=THREAD2;ctx.key().targets[0].confirmation=null;
  await watcher.pollOnce();
  assert.deepEqual(held(watcher).at(-1),{subscription_id:SUB,state:'dropped_config',outcome:'rule_removed'});
  assert.deepEqual(ctx.challenges().map(argv=>argv[2]),[THREAD,THREAD2]);
  dk.push(1);await watcher.pollOnce();
  assert.ok(ctx.wakes().every(argv=>argv[2]===THREAD),'no wake on the new thread before it confirms');
  ctx.answer(0,T1);ctx.time.advance(3600_001);await watcher.pollOnce();   // the new thread's challenge slot has expired
  assert.equal(ctx.wakes().at(-1)[2],THREAD2,'the event held back while unconfirmed wakes the new thread');
});

// spec: ANYM-849
test('delivery_disabled from the switch stops every wake of that key until it lifts',async()=>{
  const ctx=setup({keys:[{},{}]});const dk=await ctx.server();
  ctx.key(0).targets=[target(T1,THREAD,[SUB],{hourly_budget:1})];
  ctx.key(1).targets=[target(T2,THREAD2,[SUB2])];
  const watcher=ctx.start();
  await watcher.pollOnce();ctx.answer(0,T1);ctx.answer(1,T2);
  ctx.time.advance(3600_001);await watcher.pollOnce();
  dk.push(1,{},SUB);await watcher.pollOnce();
  dk.sub(SUB).acked=dk.sub(SUB).matched;await watcher.pollOnce();
  dk.push(1,{},SUB);await watcher.pollOnce();
  assert.equal(held(watcher).at(-1).state,'budget_held');
  const wakesBefore=ctx.wakes().length;
  ctx.key(0).error='delivery_disabled';
  ctx.time.advance(3600_001);
  dk.push(1,{},SUB);dk.push(1,{},SUB2);
  await watcher.pollOnce();await watcher.pollOnce();
  assert.deepEqual(ctx.wakes().slice(wakesBefore).map(argv=>argv[2]),[THREAD2],'only the other key wakes');
  assert.equal(held(watcher).find(row=>row.subscription_id===SUB&&row.state==='budget_held')?.state,'budget_held',
    'the held wake is kept, not called');
  assert.ok(ctx.entries('key_skipped').some(entry=>entry.key===0&&entry.error==='delivery_disabled'));
  delete ctx.key(0).error;
  await watcher.pollOnce();
  assert.ok(ctx.wakes().slice(wakesBefore).some(argv=>argv[2]===THREAD),'it wakes again once the switch lifts');
});

// spec: ANYM-849
test('two keys with one failing: a swapped key file or an HTTP error skips that key, the other key still runs',async()=>{
  const ctx=setup({keys:[{},{}]});const dk=await ctx.server();
  ctx.key(0).targets=[target(T1,THREAD,[SUB])];
  ctx.key(1).targets=[target(T2,THREAD2,[SUB2]),target(T3,THREAD3,[SUB3])];
  const watcher=ctx.start();
  await confirmed(ctx,watcher,[[0,T1],[1,T2],[1,T3]]);
  dk.sub(SUB2).error='backend_unavailable';                     // one target's failing subscription read
  dk.push(1,{},SUB3);await watcher.pollOnce();
  assert.deepEqual(ctx.wakes().map(argv=>argv[2]),[THREAD3],'the next target still runs');
  delete dk.sub(SUB2).error;
  const other=ctx.space.file('other.key','dk_other_token_value');
  fs.rmSync(ctx.files[0]);fs.symlinkSync(other,ctx.files[0]);
  dk.push(1,{},SUB);dk.push(1,{},SUB2);
  await watcher.pollOnce();
  assert.deepEqual(ctx.wakes().map(argv=>argv[2]),[THREAD3,THREAD2]);
  assert.ok(ctx.entries('key_skipped').some(entry=>entry.key===0&&entry.error==='unsafe_key_file'));
  dk.state.keys.dk_other_token_value={status:500};
  fs.rmSync(ctx.files[0]);fs.writeFileSync(ctx.files[0],'dk_other_token_value\n',{mode:0o600});
  dk.sub(SUB3).acked=dk.sub(SUB3).matched;dk.push(1,{},SUB3);
  await watcher.pollOnce();
  assert.ok(ctx.entries('key_skipped').some(entry=>entry.key===0&&entry.error==='deedkit_http_500'));
  assert.ok(!ctx.wakes().some(argv=>argv[2]===THREAD),'the failing key wakes nothing');
  assert.equal(ctx.wakes().at(-1)[2],THREAD3);
  assert.ok(!JSON.stringify(ctx.logs).includes('dk_other_token_value'));
});

// spec: ANYM-849
test('two keys sharing a thread: the hourly budget counts every key and every challenge, the lowest budget wins',async()=>{
  const ctx=setup({keys:[{},{}]});const dk=await ctx.server();
  ctx.key(0).targets=[target(T1,THREAD,[SUB],{hourly_budget:3})];
  ctx.key(1).targets=[target(T2,THREAD,[SUB2],{hourly_budget:30})];
  const watcher=ctx.start();
  await confirmed(ctx,watcher,[[0,T1],[1,T2]]);
  assert.equal(ctx.challenges().length,2,'two challenges take two of the three slots');
  dk.push(1,{},SUB);dk.push(1,{},SUB2);await watcher.pollOnce();
  assert.equal(ctx.wakes().length,1,'one slot left for both keys');
  assert.equal(held(watcher).filter(row=>row.state==='budget_held').length,1);
});

// spec: ANYM-849
test('watch-status shows per key its rules and last error, per target its outcome, never the key, label or content',async()=>{
  const ctx=setup({keys:[{},{allowed_threads:[THREAD]}]});const dk=await ctx.server();
  ctx.key(0).targets=[target(T1,THREAD2,[SUB])];
  ctx.key(1).targets=[target(T2,THREAD3,[SUB2]),target(T3,THREAD,[SUB3])];
  const watcher=ctx.start();
  await confirmed(ctx,watcher,[[0,T1],[1,T3]]);
  dk.push(1,{},SUB);await watcher.pollOnce();
  await watcher.close();
  const status=readWatchStatus(ctx.raw.database);
  assert.deepEqual(status.keys.map(({key,rules,last_error})=>({key,rules,last_error})),
    [{key:0,rules:1,last_error:null},{key:1,rules:1,last_error:null}]);
  assert.deepEqual(Object.fromEntries(status.wake_targets.map(row=>[row.wake_target_id,row.outcome])),
    {[T1]:'ok',[T2]:'thread_not_allowed',[T3]:'no_events_yet'});
  const text=JSON.stringify(status);
  for(const secret of [...TOKENS,LABEL,SECRET_TEXT])assert.ok(!text.includes(secret),secret);
});
