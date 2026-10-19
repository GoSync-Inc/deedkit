// Wake relay: one durable row per signed hint_id, at most one downstream fire per row (SUBSCRIPTIONS.md "Host receivers").
// Every transition commits before the action that follows it. After `sending`, only a documented 429 refusal
// returns a row to `pending`; every other outcome is final.
import {createHash} from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {verifyHint} from './hint.mjs';
import {createAdapter} from './adapters.mjs';
import {httpsTransport} from './transport.mjs';

const HOUR=3600_000,DEFAULT_RETRY_AFTER_S=60;
export const STATES=['pending','sending','accepted','unknown','failed','expired','revoked','folded'];
const SCHEMA=`
CREATE TABLE IF NOT EXISTS hints(
  hint_id TEXT PRIMARY KEY, receiver TEXT NOT NULL, adapter TEXT NOT NULL, subscription_id TEXT NOT NULL,
  subscription_version INTEGER NOT NULL, kind TEXT NOT NULL, through INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN (${STATES.map(s=>`'${s}'`).join(',')})), folded_into TEXT,
  signed_at INTEGER NOT NULL, received_at INTEGER NOT NULL, hold_until INTEGER NOT NULL, retry_at INTEGER NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, connects INTEGER NOT NULL DEFAULT 0, outcome TEXT, vendor_id TEXT,
  updated_at INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS hints_one_active ON hints(receiver,subscription_id) WHERE state IN ('pending','sending');
CREATE INDEX IF NOT EXISTS hints_due ON hints(state,retry_at);
CREATE TABLE IF NOT EXISTS slots(hint_id TEXT PRIMARY KEY, target TEXT NOT NULL, account TEXT NOT NULL, at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS slots_target ON slots(target,at);
CREATE INDEX IF NOT EXISTS slots_account ON slots(account,at);
CREATE TABLE IF NOT EXISTS refusals(receiver TEXT NOT NULL, subscription_id TEXT NOT NULL, reason TEXT NOT NULL,
  count INTEGER NOT NULL, last_at INTEGER NOT NULL, PRIMARY KEY(receiver,subscription_id,reason));
CREATE TABLE IF NOT EXISTS revocations(adapter TEXT NOT NULL, credential TEXT NOT NULL, status INTEGER, at INTEGER NOT NULL,
  PRIMARY KEY(adapter,credential));`;

// Same rules as the DeedKit dispatcher's receiver secret: absolute, regular, owner-only, bounded.
export function readSecret(file,{min=1,max=4096}={}){
  if(typeof file!=='string'||!path.isAbsolute(file))throw new Error('unsafe_secret_file');
  const fd=fs.openSync(file,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW);
  try{
    const info=fs.fstatSync(fd);
    if(!info.isFile()||info.mode&0o077||info.size<min||info.size>max)throw new Error('unsafe_secret_file');
    const value=Buffer.alloc(max+1),size=fs.readSync(fd,value,0,max+1,0);
    if(size<min||size>max)throw new Error('unsafe_secret_file');
    return value.subarray(0,size);
  }finally{fs.closeSync(fd);}
}

export function loadConfig(config){
  const maxHoldS=config.max_hold_s??120;
  if(!Number.isInteger(maxHoldS)||maxHoldS<1||maxHoldS>600)throw new Error('max_hold_s must be 1..600');
  const accounts=config.accounts??{},adapters=new Map(),targets=new Set();
  for(const [name,value] of Object.entries(config.adapters??{})){
    const budget=value.hourly_budget??20,account=value.account??name;
    const accountBudget=accounts[account]?.hourly_budget??60;
    if(!Number.isInteger(budget)||budget<1||!Number.isInteger(accountBudget)||accountBudget<1)throw new Error(`adapter ${name}: budgets must be positive integers`);
    const impl=createAdapter(value);
    // The target budget belongs to the vendor target, so two adapter names may not share one routine or webhook.
    if(targets.has(impl.url))throw new Error(`adapter ${name}: another adapter already fires ${impl.url}`);
    targets.add(impl.url);
    adapters.set(name,{name,impl,target:impl.url,enabled:value.enabled!==false,credentialFile:value.credential_file,
      budget,account,accountBudget});
  }
  const receivers=new Map();
  for(const [id,value] of Object.entries(config.receivers??{})){
    if(!/^[A-Za-z0-9-]{1,64}$/.test(id))throw new Error(`receiver id ${JSON.stringify(id)} must be [A-Za-z0-9-]{1,64}`);
    if(!adapters.has(value.adapter))throw new Error(`receiver ${id}: unknown adapter ${JSON.stringify(value.adapter)}`);
    receivers.set(id,{id,secretFile:value.secret_file,adapter:value.adapter});
  }
  return {database:config.database,maxHoldS,adapters,receivers};
}

const fingerprint=value=>createHash('sha256').update(value).digest('hex');
const errorCode=error=>String(error?.code??error?.message??'error').slice(0,64).replace(/[^A-Za-z0-9_.-]/g,'_');
function statusOf(db){
  const all=sql=>db.prepare(sql).all();
  return {
    states:Object.fromEntries(all('SELECT state, count(*) AS n FROM hints GROUP BY state').map(r=>[r.state,r.n])),
    refusals:all('SELECT receiver,subscription_id,reason,count,last_at FROM refusals ORDER BY last_at DESC LIMIT 50'),
    revocations:all('SELECT adapter,status,at FROM revocations ORDER BY at DESC'),
    recent:all(`SELECT hint_id,subscription_id,state,outcome,through,attempts,vendor_id,updated_at FROM hints
      WHERE state<>'folded' ORDER BY updated_at DESC LIMIT 20`),
  };
}
// Read-only inspection for `wake-relay status`: no schema, no recovery, so a live `sending` row stays untouched.
export function readStatus(database){
  const db=new DatabaseSync(database,{readOnly:true});
  try{return statusOf(db);}finally{db.close();}
}

export const monotonicClock={now:()=>Math.floor(performance.timeOrigin+performance.now())};

export class Relay{
  constructor({config,clock=monotonicClock,transport=httpsTransport(),step=()=>{},log=()=>{},autoFire=true,dropAckOnce=false}){
    this.config=config.adapters instanceof Map?config:loadConfig(config);
    Object.assign(this,{clock,transport,step,log,autoFire,dropAckOnce,inflight:new Map(),timer:null,closed:false});
    this.db=new DatabaseSync(this.config.database);
    this.db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;');
    this.db.exec(SCHEMA);
    this.recover();
  }

  tx(fn){
    this.db.exec('BEGIN IMMEDIATE');
    try{const value=fn();this.db.exec('COMMIT');return value;}catch(error){this.db.exec('ROLLBACK');throw error;}
  }
  run(sql,...args){return this.db.prepare(sql).run(...args);}
  get(sql,...args){return this.db.prepare(sql).get(...args);}
  all(sql,...args){return this.db.prepare(sql).all(...args);}
  row(id){return this.get('SELECT * FROM hints WHERE hint_id=?',id);}

  // Restart: nothing in `pending` was written downstream, so it fires; `sending` may have started a run, so it is `unknown`.
  recover(){
    const now=this.clock.now();
    this.tx(()=>{
      this.run(`UPDATE hints SET state='unknown',outcome='interrupted_after_send',updated_at=? WHERE state='sending'`,now);
      for(const adapter of this.config.adapters.values())
        if(!adapter.enabled)this.run(`UPDATE hints SET state='revoked',outcome='adapter_disabled',updated_at=? WHERE state='pending' AND adapter=?`,now,adapter.name);
    });
  }

  credential(adapter){
    if(!adapter?.enabled)return null;
    try{return readSecret(adapter.credentialFile);}catch{return null;}
  }
  revoked(adapter,credential){
    return Boolean(this.get('SELECT 1 FROM revocations WHERE adapter=? AND credential=?',adapter.name,fingerprint(credential)));
  }
  // Seconds until a slot opens, or 0 when the target and its account both have one now. A slot is dated at
  // acknowledgement and moves to the time of each send, so it counts in every sliding hour that holds its fire;
  // an unsent slot is younger than max_hold_s and therefore always counted.
  budgetWait(adapter,now){
    for(const [column,key,limit] of [['target',adapter.target,adapter.budget],['account',adapter.account,adapter.accountBudget]]){
      const used=this.get(`SELECT count(*) AS n, min(at) AS first FROM slots WHERE ${column}=? AND at>?`,key,now-HOUR);
      if(used.n>=limit)return Math.max(1,Math.ceil((used.first+HOUR-now)/1000));
    }
    return 0;
  }
  refuse(receiver,hint,reason,retryAfter,now){
    this.run(`INSERT INTO refusals VALUES(?,?,?,1,?) ON CONFLICT(receiver,subscription_id,reason)
      DO UPDATE SET count=count+1,last_at=excluded.last_at`,receiver.id,hint.subscription_id,reason,now);
    this.log({event:'refused',reason,subscription_id:hint.subscription_id});
    return {status:503,headers:{'retry-after':String(retryAfter)},body:{error:reason}};
  }

  // Answers the dispatcher. A 2xx is returned only after the hint's row (or its fold) has committed.
  receive(receiverId,raw,headers){
    const receiver=this.config.receivers.get(receiverId);
    if(!receiver)return {status:404,body:{error:'unknown_receiver'}};
    let secret;
    try{secret=readSecret(receiver.secretFile,{min:32});}catch{return {status:503,headers:{'retry-after':'60'},body:{error:'receiver_secret_unavailable'}};}
    const now=this.clock.now(),verified=verifyHint(raw,headers,secret,now);
    if(verified.error)return {status:verified.status,body:{error:verified.error}};
    const {hint,signedAt}=verified,adapter=this.config.adapters.get(receiver.adapter);
    const result=this.tx(()=>{
      if(this.row(hint.hint_id))return {status:200,body:{result:'duplicate'}};
      const active=this.get(`SELECT * FROM hints WHERE receiver=? AND subscription_id=? AND state IN ('pending','sending')`,receiver.id,hint.subscription_id);
      if(active){
        this.run(`INSERT INTO hints(hint_id,receiver,adapter,subscription_id,subscription_version,kind,through,state,folded_into,
          signed_at,received_at,hold_until,retry_at,updated_at) VALUES(?,?,?,?,?,?,?,'folded',?,?,?,?,?,?)`,hint.hint_id,receiver.id,
          adapter.name,hint.subscription_id,hint.subscription_version,hint.kind,hint.through,active.hint_id,signedAt*1000,now,
          active.hold_until,active.retry_at,now);
        this.run(`UPDATE hints SET through=max(through,?),subscription_version=max(subscription_version,?),updated_at=? WHERE hint_id=?`,
          hint.through,hint.subscription_version,now,active.hint_id);
        this.step('beforePendingCommit');
        return {status:202,body:{result:'folded'}};
      }
      const credential=this.credential(adapter);
      if(!credential||this.revoked(adapter,credential))return this.refuse(receiver,hint,'adapter_unavailable',DEFAULT_RETRY_AFTER_S,now);
      const holdUntil=signedAt*1000+this.config.maxHoldS*1000;
      if(now>=holdUntil)return this.refuse(receiver,hint,'hold_elapsed',1,now);
      const wait=this.budgetWait(adapter,now);
      if(wait)return this.refuse(receiver,hint,'budget_refused',wait,now);
      this.run(`INSERT INTO hints(hint_id,receiver,adapter,subscription_id,subscription_version,kind,through,state,signed_at,
        received_at,hold_until,retry_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?,?)`,hint.hint_id,receiver.id,adapter.name,
        hint.subscription_id,hint.subscription_version,hint.kind,hint.through,signedAt*1000,now,holdUntil,now,now);
      this.run('INSERT INTO slots VALUES(?,?,?,?)',hint.hint_id,adapter.target,adapter.account,now);
      this.run('DELETE FROM slots WHERE at<=?',now-2*HOUR);
      this.step('beforePendingCommit');
      return {status:202,body:{result:'accepted'}};
    });
    if(result.status===202){
      this.log({event:result.body.result,hint_id:hint.hint_id,subscription_id:hint.subscription_id});
      this.step('afterPendingCommit');
      this.schedule(0);
      if(this.dropAckOnce&&result.body.result==='accepted'){this.dropAckOnce=false;return {...result,drop:true};}
    }
    return result;
  }

  transition(id,from,state,outcome,extra={}){
    const now=this.clock.now(),sets=Object.keys(extra).map(key=>`,${key}=?`).join('');
    const changed=this.run(`UPDATE hints SET state=?,outcome=?,updated_at=?${sets} WHERE hint_id=? AND state=?`,
      state,outcome,now,...Object.values(extra),id,from).changes===1;
    if(changed)this.log({event:'transition',hint_id:id,state,outcome});
    return changed;
  }

  fireDue(){
    const now=this.clock.now(),work=[];
    for(const {hint_id} of this.all(`SELECT hint_id FROM hints WHERE state='pending' AND retry_at<=? ORDER BY retry_at`,now)){
      if(!this.inflight.has(hint_id)){
        const job=this.fire(hint_id).finally(()=>this.inflight.delete(hint_id));
        this.inflight.set(hint_id,job);
      }
      work.push(this.inflight.get(hint_id));
    }
    return Promise.all(work);
  }

  async fire(id){
    let row=this.row(id);
    if(row?.state!=='pending'||this.closed)return;
    const adapter=this.config.adapters.get(row.adapter);
    if(!this.fenced(id,adapter))return;
    if(this.clock.now()>=row.hold_until)return this.transition(id,'pending','expired','hold_elapsed');
    let socket;
    try{socket=await this.transport.connect(adapter.impl.url);}
    catch(error){return this.preSendFailure(row,errorCode(error));}
    let sending=false;
    try{
      this.step('beforeSending');
      row=this.row(id);
      const now=this.clock.now();
      if(row?.state!=='pending'||this.closed)return;
      // The credential is read again after the connect: one removed, rotated or revoked meanwhile is honoured.
      const credential=this.fenced(id,adapter);
      if(!credential)return;
      if(now>=row.hold_until)return this.transition(id,'pending','expired','hold_elapsed');
      const payload={hint_id:row.hint_id,subscription_id:row.subscription_id,subscription_version:row.subscription_version,
        through:row.through,kind:row.kind,host_turn_verified:false};
      let request;
      try{request=adapter.impl.build(payload,credential,now);}catch(error){return this.transition(id,'pending','failed',errorCode(error));}
      sending=this.tx(()=>{
        const changed=this.run(`UPDATE hints SET state='sending',attempts=attempts+1,updated_at=? WHERE hint_id=? AND state='pending'`,
          now,id).changes===1;
        if(changed)this.run('UPDATE slots SET at=max(at,?) WHERE hint_id=?',now,id);
        return changed;
      });
      if(!sending)return;
      this.step('afterSending');
      let response;
      try{response=await this.transport.send(socket,request);}
      catch(error){return this.transition(id,'sending','unknown',errorCode(error));}
      return this.settle(row,adapter,credential,response);
    }finally{if(!sending)this.transport.close(socket);}
  }

  // The current credential, or null after moving the row to `revoked` (adapter disabled, credential gone or revoked).
  fenced(id,adapter){
    const credential=this.credential(adapter);
    if(!credential){this.transition(id,'pending','revoked','adapter_disabled');return null;}
    if(this.revoked(adapter,credential)){this.transition(id,'pending','revoked','adapter_revoked');return null;}
    return credential;
  }

  preSendFailure(row,code){
    const now=this.clock.now(),delay=Math.min(30,2**row.connects)*1000;
    if(now+delay>=row.hold_until)return this.transition(row.hint_id,'pending','expired',`pre_send:${code}`,{connects:row.connects+1});
    return this.transition(row.hint_id,'pending','pending',`pre_send:${code}`,{connects:row.connects+1,retry_at:now+delay});
  }

  settle(row,adapter,credential,response){
    const now=this.clock.now(),result=adapter.impl.classify(response,now),status=result.outcome??`http_${response.status}`;
    if(result.state==='accepted')return this.transition(row.hint_id,'sending','accepted',status,{vendor_id:result.vendorId});
    if(result.state==='pending'){
      const retryAt=now+(result.retryAfter??DEFAULT_RETRY_AFTER_S)*1000;
      if(retryAt>=row.hold_until)return this.transition(row.hint_id,'sending','expired','rate_limited_past_hold');
      return this.transition(row.hint_id,'sending','pending','rate_limited',{retry_at:retryAt});
    }
    if(result.state==='revoked')return this.tx(()=>{
      this.run('INSERT OR REPLACE INTO revocations VALUES(?,?,?,?)',adapter.name,fingerprint(credential),response.status,now);
      this.run(`UPDATE hints SET state='revoked',outcome='adapter_revoked',updated_at=? WHERE state='pending' AND adapter=?`,now,adapter.name);
      this.log({event:'adapter_revoked',adapter:adapter.name,status:response.status});
      return this.transition(row.hint_id,'sending','revoked',status);
    });
    return this.transition(row.hint_id,'sending',result.state,status);
  }

  schedule(delay){
    if(!this.autoFire||this.closed)return;
    if(delay===undefined){
      const next=this.get(`SELECT min(retry_at) AS at FROM hints WHERE state='pending'`).at;
      if(next===null)return;
      delay=Math.max(0,next-this.clock.now());
    }
    clearTimeout(this.timer);
    this.timer=setTimeout(()=>this.fireDue().catch(error=>this.log({event:'fire_error',error:errorCode(error)}))
      .finally(()=>this.schedule()),Math.min(delay,2**31-1));
  }

  start(){this.schedule();return this;}

  status(){return statusOf(this.db);}

  async close(){
    this.closed=true;clearTimeout(this.timer);
    await Promise.allSettled([...this.inflight.values()]);
    this.db.close();
  }
}
