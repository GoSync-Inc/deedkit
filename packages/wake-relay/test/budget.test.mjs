import test from 'node:test';
import assert from 'node:assert/strict';
import {RECEIVER,ROUTINE,clock,downstream,relay,rows,signed,workspace} from './helpers.mjs';


const HOUR=3600_000;
const maxInSlidingHour=times=>Math.max(0,...times.map(end=>times.filter(at=>at>end-HOUR&&at<=end).length));

// spec: ANYM-228
test('200 hints across 10 subscriptions over two hours stay within 20 fires per sliding hour; refusals get 503 with Retry-After and store no row',async()=>{
  const space=workspace(),time=clock(),fake=downstream([],{clock:time});
  const instance=relay(space,{clock:time,transport:fake});
  const subscriptions=Array.from({length:10},()=>crypto.randomUUID());
  const refused=[];let accepted=0;
  for(let i=0;i<200;i++){
    const hint=signed({subscription_id:subscriptions[i%10],through:i},Math.floor(time.now()/1000));
    const answer=instance.receive(RECEIVER,hint.raw,hint.headers);
    if(answer.status===503){
      assert.equal(answer.body.error,'budget_refused');
      assert.match(answer.headers['retry-after'],/^[1-9][0-9]*$/);
      refused.push(hint.hint.hint_id);
    }else{assert.equal(answer.status,202);accepted++;}
    await instance.fireDue();
    time.advance(36_000);
  }
  assert.ok(refused.length>0);
  assert.equal(fake.fires.length,accepted);
  assert.ok(maxInSlidingHour(fake.fires.map(f=>f.at))<=20);
  for(const id of refused)assert.equal(instance.row(id),undefined);
  assert.equal(rows(instance).filter(r=>r.state==='expired').length,0);
  assert.equal(instance.get(`SELECT sum(count) AS n FROM refusals WHERE reason='budget_refused'`).n,refused.length);
  await instance.close();
});

// spec: ANYM-228
test('two targets on one vendor account stay within the account budget of 60 fires per sliding hour',async()=>{
  const space=workspace(),time=clock(),fake=downstream([],{clock:time});
  const adapters={a:{type:'claude-routine',routine_id:ROUTINE,credential_file:space.claudeToken,account:'owner',hourly_budget:40},
    b:{type:'claude-routine',routine_id:'trig_02SYNTHETICROUTINE',credential_file:space.claudeToken,account:'owner',hourly_budget:40}};
  const second='5a1c2b3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d';
  const receivers={[RECEIVER]:{secret_file:space.receiverSecret,adapter:'a'},[second]:{secret_file:space.receiverSecret,adapter:'b'}};
  const instance=relay(space,{clock:time,transport:fake,configOverrides:{adapters,receivers}});
  for(let i=0;i<240;i++){
    const hint=signed({through:i},Math.floor(time.now()/1000));
    instance.receive(i%2?second:RECEIVER,hint.raw,hint.headers);
    await instance.fireDue();
    time.advance(30_000);
  }
  const times=fake.fires.map(f=>f.at);
  assert.equal(maxInSlidingHour(times),60);
  for(const routine of [ROUTINE,'trig_02SYNTHETICROUTINE'])
    assert.ok(maxInSlidingHour(fake.fires.filter(f=>f.url.includes(routine)).map(f=>f.at))<=40);
  await instance.close();
});

// spec: ANYM-228
test('when the quota outlasts the hold, the third hint is refused on all 8 dispatcher attempts, never fires and is counted as budget_refused',async()=>{
  const space=workspace(),time=clock(),fake=downstream([],{clock:time});
  const instance=relay(space,{clock:time,transport:fake,configOverrides:{max_hold_s:120,adapters:{claude:{type:'claude-routine',
    routine_id:ROUTINE,credential_file:space.claudeToken,account:'anthropic',hourly_budget:2}}}});
  const start=time.now(),[one,two,three]=Array.from({length:3},()=>crypto.randomUUID());
  for(const subscription_id of [one,two]){
    const hint=signed({subscription_id},Math.floor(time.now()/1000));
    assert.equal(instance.receive(RECEIVER,hint.raw,hint.headers).status,202);
  }
  await instance.fireDue();
  // Dispatcher stub with the event_delivery.py backoff: a fresh signature per attempt, 2**(attempts+1) s apart,
  // dead letter once the 8th attempt fails.
  const fields={subscription_id:three,hint_id:crypto.randomUUID()};
  let attempts=0,state='pending';
  while(state==='pending'){
    const hint=signed(fields,Math.floor(time.now()/1000));
    const answer=instance.receive(RECEIVER,hint.raw,hint.headers);
    assert.equal(answer.status,503);
    if(attempts+1>=8)state='dead_letter';
    else time.advance(2**(attempts+1)*1000);
    attempts++;
    await instance.fireDue();
  }
  time.t=start+30*60_000;
  await instance.fireDue();
  assert.equal(attempts,8);
  assert.equal(fake.fires.length,2);
  assert.ok(fake.fires.every(f=>f.payload.subscription_id!==three));
  assert.equal(instance.row(fields.hint_id),undefined);
  assert.equal(instance.get(`SELECT count FROM refusals WHERE subscription_id=? AND reason='budget_refused'`,three).count,8);
  await instance.close();
});

// spec: ANYM-228
test('a slot is dated by its actual send, so fires delayed inside the hold still count in the next sliding hour',async()=>{
  const space=workspace(),time=clock(),fake=downstream([],{clock:time});
  const instance=relay(space,{clock:time,transport:fake});
  const deliver=()=>{const h=signed({},Math.floor(time.now()/1000));return instance.receive(RECEIVER,h.raw,h.headers);};
  const start=time.now();
  for(let i=0;i<20;i++)assert.equal(deliver().status,202);
  time.advance(119_000);
  await instance.fireDue();
  time.t=start+3600_000;
  assert.equal(deliver().status,503);
  time.t=start+119_000+3600_000+1;
  assert.equal(deliver().status,202);
  await instance.fireDue();
  assert.equal(fake.fires.length,21);
  assert.ok(maxInSlidingHour(fake.fires.map(f=>f.at))<=20);
  await instance.close();
});

// spec: ANYM-228
test('two adapter names for one vendor target are rejected, so a target budget cannot be doubled by aliasing',()=>{
  const space=workspace();
  const adapters={a:{type:'claude-routine',routine_id:ROUTINE,credential_file:space.claudeToken},
    b:{type:'claude-routine',routine_id:ROUTINE,credential_file:space.claudeToken}};
  assert.throws(()=>relay(space,{configOverrides:{adapters,receivers:{[RECEIVER]:{secret_file:space.receiverSecret,adapter:'a'}}}}),/already fires/);
});

// spec: ANYM-228
test('equivalent spellings of one vendor target are one target, and a URL fragment or userinfo is refused',()=>{
  const space=workspace(),receivers={[RECEIVER]:{secret_file:space.receiverSecret,adapter:'a'}};
  const claude=api_base=>({type:'claude-routine',routine_id:ROUTINE,credential_file:space.claudeToken,api_base});
  const grok=endpoint=>({type:'grok-automation',endpoint,credential_file:space.grokSecret});
  const load=(a,b)=>()=>relay(space,{configOverrides:{adapters:{a,b},receivers}});
  assert.throws(load(claude('https://api.anthropic.com'),claude('https://API.ANTHROPIC.COM:443')),/adapter b: adapter a already fires the same target/);
  assert.throws(load(grok('https://grok.example.test/hook'),grok('https://GROK.example.test:443/hook')),/already fires the same target/);
  for(const endpoint of ['https://grok.example.test/hook#same','https://grok.example.test/hook#','https://user@grok.example.test/hook'])
    assert.throws(load(grok(endpoint),claude('https://api.anthropic.com')),/no userinfo or fragment/,endpoint);
  assert.throws(load(claude('https://user@api.anthropic.com'),grok('https://grok.example.test/hook')),/no userinfo or fragment/);
});

// spec: ANYM-228
test('slots an earlier build stored under the adapter name still count after upgrade, and the target is kept only as a fingerprint',async()=>{
  const space=workspace(),time=clock(),fake=downstream([],{clock:time});
  const first=relay(space,{clock:time,transport:fake});
  for(let i=0;i<20;i++)first.run('INSERT INTO slots VALUES(?,?,?,?)',crypto.randomUUID(),i%2?'claude':`https://api.anthropic.com/v1/claude_code/routines/${ROUTINE}/fire`,'anthropic',time.now());
  await first.close();
  const instance=relay(space,{clock:time,transport:fake});
  const hint=signed({},Math.floor(time.now()/1000));
  assert.equal(instance.receive(RECEIVER,hint.raw,hint.headers).status,503);
  assert.deepEqual(instance.all('SELECT DISTINCT target FROM slots').map(r=>/^[0-9a-f]{64}$/.test(r.target)),[true]);
  await instance.close();
});

// spec: ANYM-228
test('a 429-refused request starts no run: its retry is the same fire, reusing the one slot re-dated to the retry',async()=>{
  const space=workspace(),time=clock(),fake=downstream([{status:429,headers:{'retry-after':'5'}},{status:429,headers:{'retry-after':'5'}}],{clock:time});
  const instance=relay(space,{clock:time,transport:fake,configOverrides:{adapters:{claude:{type:'claude-routine',routine_id:ROUTINE,
    credential_file:space.claudeToken,account:'anthropic',hourly_budget:2}}}});
  const deliver=()=>{const h=signed({},Math.floor(time.now()/1000));return instance.receive(RECEIVER,h.raw,h.headers);};
  assert.deepEqual([deliver().status,deliver().status],[202,202]);
  await instance.fireDue();
  time.advance(5000);
  await instance.fireDue();
  assert.equal(deliver().status,503);
  assert.deepEqual(rows(instance).map(r=>[r.state,r.attempts]),[['accepted',2],['accepted',2]]);
  assert.equal(fake.fires.length,4);
  assert.deepEqual(instance.all('SELECT at FROM slots').map(r=>r.at),[time.now(),time.now()]);
  await instance.close();
});
