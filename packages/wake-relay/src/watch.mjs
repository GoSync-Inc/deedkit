// Pull waker (ANYM-844, SUBSCRIPTIONS.md "Codex chat (pull waker)"): passive reads of a DeedKit subscription wake an
// existing Codex chat with one content-free `codex queue`. It never fetches with a receipt, never waits and never
// ACKs; the woken chat consumes the events with its own tools. Every wake row commits before the subprocess starts.
// Registry mode (ANYM-849): the rules are each key's own wake targets (ANYM-848), read at every poll.
import {createHash,randomBytes} from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {challengeMessage,codexQueue,UUID,validThread} from './codex.mjs';
import {DeedKitError,deedkitClient,readKey} from './mcp.mjs';

const HOUR=3600_000,PAGE=50,PAGES=4,MAX_AFTER=2**53-1;
export const WAKE_STATES=['budget_held','calling','queued','queued_not_resumed','failed','unsupported_host','call_unknown',
  'dropped_config'];
export const TOOLS=['messages_get_subscription_events','get_subscription_events'];
export {challengeMessage};
export const TARGET_OUTCOMES=['ok','unconfirmed','invalid_wake_target','thread_not_allowed','call_failed','no_events_yet'];
const MAX_KEYS=32,MAX_ALLOWED=64;
const SCHEMA=`
CREATE TABLE IF NOT EXISTS cursors(subscription_id TEXT PRIMARY KEY, after INTEGER NOT NULL, acked_through INTEGER,
  updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS wakes(id INTEGER PRIMARY KEY AUTOINCREMENT, subscription_id TEXT NOT NULL, thread TEXT NOT NULL,
  position INTEGER NOT NULL, through INTEGER NOT NULL, folded INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL CHECK(state IN (${WAKE_STATES.map(s=>`'${s}'`).join(',')})), outcome TEXT,
  ack_observed INTEGER NOT NULL DEFAULT 0, released INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL, called_at INTEGER, updated_at INTEGER NOT NULL, UNIQUE(subscription_id,position));
CREATE INDEX IF NOT EXISTS wakes_open ON wakes(subscription_id,state);
CREATE TABLE IF NOT EXISTS slots(wake_id INTEGER PRIMARY KEY, thread TEXT NOT NULL, at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS slots_thread ON slots(thread,at);
CREATE TABLE IF NOT EXISTS challenges(target_id TEXT NOT NULL, thread TEXT NOT NULL, code_sha256 TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN (${WAKE_STATES.map(s=>`'${s}'`).join(',')})), outcome TEXT,
  confirmed INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY(target_id,thread));
CREATE INDEX IF NOT EXISTS challenges_thread ON challenges(thread,created_at);
CREATE TABLE IF NOT EXISTS key_polls(key INTEGER PRIMARY KEY, rules INTEGER NOT NULL, last_error TEXT, polled_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS key_budgets(key_ref TEXT NOT NULL, thread TEXT NOT NULL, budget INTEGER NOT NULL,
  PRIMARY KEY(key_ref,thread));
CREATE TABLE IF NOT EXISTS subscription_owners(subscription_id TEXT PRIMARY KEY, key_ref TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS target_outcomes(target_id TEXT PRIMARY KEY, key INTEGER NOT NULL,
  outcome TEXT NOT NULL CHECK(outcome IN (${TARGET_OUTCOMES.map(s=>`'${s}'`).join(',')})), updated_at INTEGER NOT NULL);`;

export class ConfigError extends Error{
  constructor(field,reason,code='invalid_config'){super(`config ${field}: ${reason}`);this.code=code;this.field=field;}
}

// The first executable `codex` on PATH (absolute directories only), resolved once at start.
function onPath(env){
  for(const dir of String(env.PATH??'').split(path.delimiter)){
    if(!path.isAbsolute(dir))continue;
    const file=path.join(dir,'codex');
    try{fs.accessSync(file,fs.constants.X_OK);if(fs.statSync(file).isFile())return file;}catch{}
  }
  return null;
}

export function loadWatchConfig(config,{env=process.env}={}){
  const fail=(field,reason)=>{throw new ConfigError(field,reason);};
  if(!config||typeof config!=='object')fail('config','must be an object');
  if(typeof config.database!=='string'||!path.isAbsolute(config.database))fail('database','must be an absolute path');
  let url;
  try{url=new URL(config.deedkit_url);}catch{fail('deedkit_url','must be a URL');}
  if(url.protocol!=='https:')fail('deedkit_url','must be https');
  if(url.username||url.password)fail('deedkit_url','must have no userinfo');
  if(url.search||String(config.deedkit_url).includes('?'))fail('deedkit_url','must have no query');
  if(url.hash||String(config.deedkit_url).includes('#'))fail('deedkit_url','must have no fragment');
  const keyFile=(field,file)=>{
    try{readKey(file);}catch{fail(field,'must be an absolute, regular, owner-only (0600) file of one key, not a symlink');}
    return file;
  };
  // Registry mode never also runs static rules: a static watch could keep waking a target the registry turned off.
  if(config.keys!==undefined&&(config.watches!==undefined||config.key_file!==undefined))
    throw new ConfigError('keys','registry mode (keys) has no key_file or watches','watches_with_keys');
  const tool=config.tool??TOOLS[0];
  if(!TOOLS.includes(tool))fail('tool',`must be one of ${TOOLS.join(', ')}`);
  const pollS=config.poll_s??30;
  if(!Number.isInteger(pollS)||pollS<10||pollS>3600)fail('poll_s','must be an integer 10..3600');
  let codexPath=null;
  if(config.codex_path!==undefined){
    if(typeof config.codex_path!=='string'||!path.isAbsolute(config.codex_path))fail('codex_path','must be an absolute path');
    codexPath=config.codex_path;
  }else codexPath=onPath(env);
  const common={database:config.database,deedkitUrl:url.href,tool,pollMs:pollS*1000,codexPath};
  if(config.keys!==undefined){
    if(!Array.isArray(config.keys)||config.keys.length<1||config.keys.length>MAX_KEYS)fail('keys',`must be a list of 1..${MAX_KEYS} keys`);
    const keys=config.keys.map((key,index)=>{
      const at=`keys[${index}]`;
      if(!key||typeof key!=='object'||Array.isArray(key))fail(at,'must be an object');
      const allowed=key.allowed_threads;
      if(allowed!==undefined&&(!Array.isArray(allowed)||allowed.length<1||allowed.length>MAX_ALLOWED||!allowed.every(validThread)))
        fail(`${at}.allowed_threads`,`must be a list of 1..${MAX_ALLOWED} threads (UUIDs or names matching ^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$)`);
      // `ref` names the key across restarts and reorders: a digest of its file path, never of the key itself.
      return {index,keyFile:keyFile(`${at}.key_file`,key.key_file),tool,allowedThreads:allowed===undefined?null:[...allowed],
        ref:createHash('sha256').update(path.resolve(key.key_file)).digest('hex').slice(0,32)};
    });
    return {mode:'registry',...common,registryTool:tool===TOOLS[1]?'list_wake_targets':'messages_list_wake_targets',keys,watches:[]};
  }
  keyFile('key_file',config.key_file);
  if(!Array.isArray(config.watches)||config.watches.length<1)fail('watches','must be a non-empty list');
  const seen=new Set();
  const watches=config.watches.map((watch,index)=>{
    const at=`watches[${index}]`;
    if(!watch||typeof watch!=='object')fail(at,'must be an object');
    if(typeof watch.subscription_id!=='string'||!UUID.test(watch.subscription_id))fail(`${at}.subscription_id`,'must be a UUID');
    const subscriptionId=watch.subscription_id.toLowerCase();
    if(seen.has(subscriptionId))fail(`${at}.subscription_id`,'is listed twice');
    seen.add(subscriptionId);
    if(!validThread(watch.thread))fail(`${at}.thread`,'must be a UUID or a name matching ^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$');
    const budget=watch.hourly_budget??12;
    if(!Number.isInteger(budget)||budget<1||budget>600)fail(`${at}.hourly_budget`,'must be a positive integer');
    return {subscriptionId,thread:watch.thread,budget};
  });
  return {mode:'legacy',...common,keyFile:config.key_file,watches};
}

const code=error=>error instanceof DeedKitError||error instanceof ConfigError?error.code:
  String(error?.code??'error').slice(0,64).replace(/[^A-Za-z0-9_.-]/g,'_');
const integer=value=>Number.isSafeInteger(value)&&value>=0&&value<=MAX_AFTER;
// A hostile or malformed page is refused whole: no cursor moves and nothing is called.
function ackedOf(page){
  const value=page?.subscription?.acked_through??page?.positions?.acked_through;
  return value===undefined||value===null?null:integer(value)?value:bad();
}
function bad(){throw new DeedKitError('deedkit_bad_response');}
// Historical items never wake. With wake_rules the server returns only matching items, so any other item wakes;
// without wake_rules any non-historical item wakes. The reason text itself never leaves this function.
export function wakes(item){
  if(item.historical===true)return false;
  return !(Array.isArray(item.wake_reason)&&item.wake_reason.includes('historical_event'));
}

function statusOf(db){
  const all=(sql,...args)=>db.prepare(sql).all(...args);
  const states=Object.fromEntries(all('SELECT state, count(*) AS n FROM wakes GROUP BY state').map(r=>[r.state,r.n]));
  const watches=all('SELECT subscription_id,after,acked_through,updated_at FROM cursors ORDER BY subscription_id').map(cursor=>({
    ...cursor,last_wake:db.prepare(`SELECT state,outcome,thread,position,through,folded,ack_observed,created_at,updated_at FROM wakes
      WHERE subscription_id=? ORDER BY id DESC LIMIT 1`).get(cursor.subscription_id)??null}));
  // Registry mode (ANYM-849): per key the rules read at its last poll and its last error code, per wake target ID its
  // last outcome code. A database an ANYM-844 waker made has no such tables yet.
  const has=name=>db.prepare(`SELECT 1 FROM sqlite_master WHERE type='table' AND name=?`).get(name)!==undefined;
  const keys=has('key_polls')?all('SELECT key,rules,last_error,polled_at FROM key_polls ORDER BY key'):[];
  const targets=has('target_outcomes')?all(`SELECT target_id AS wake_target_id,key,outcome,updated_at FROM target_outcomes
    ORDER BY key,target_id`):[];
  return {states,budget_held:states.budget_held??0,unsupported_host:states.unsupported_host??0,watches,keys,wake_targets:targets,
    note:'ack_observed means the subscription ACK position reached the wake; it shows consumption progress, not which chat did it'};
}
// `wake-relay watch-status`: read-only, no schema, no recovery. The database holds no key and no event content.
export function readWatchStatus(database){
  const db=new DatabaseSync(database,{readOnly:true});
  try{return statusOf(db);}finally{db.close();}
}

export class Watcher{
  constructor({config,env=process.env,client,clientFor,codex,clock=Date,step=()=>{},log=()=>{}}){
    this.config=config.mode?config:loadWatchConfig(config,{env});
    Object.assign(this,{clock,step,log,closed:false,timer:null});
    if(this.config.mode==='registry'){
      // One client per key; each opens its own session per poll and reads its key file again each time.
      clientFor??=key=>deedkitClient({url:this.config.deedkitUrl,keyFile:key.keyFile,tool:key.tool});
      this.clients=this.config.keys.map(key=>clientFor(key));
      // Last successful read per key: its targets' subscriptions (whose held wakes it decides) and its budgets.
      // Kept in SQLite (key_budgets, subscription_owners), so a restart with a key skipped keeps both.
      this.refs=new Map(this.config.keys.map(key=>[key.ref,key.index]));
    }else this.client=client??deedkitClient({url:this.config.deedkitUrl,keyFile:this.config.keyFile,tool:this.config.tool});
    this.codex=codex??codexQueue({codexPath:this.config.codexPath});
    // One watch process per database: an exclusive SQLite lock on a sibling file, held for the process lifetime and
    // released by the OS on any exit; a second start fails instead of calling twice.
    this.lock=new DatabaseSync(`${this.config.database}.lock`);
    try{
      this.lock.exec('PRAGMA busy_timeout=0; PRAGMA locking_mode=EXCLUSIVE;');
      this.lock.exec('BEGIN EXCLUSIVE; CREATE TABLE IF NOT EXISTS held(x); COMMIT;');
    }catch{
      this.lock.close();
      throw Object.assign(new Error('watch_already_running'),{code:'watch_already_running'});
    }
    this.db=new DatabaseSync(this.config.database);
    this.db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;');
    this.db.exec(SCHEMA);
    this.budgets=new Map();
    for(const watch of this.config.watches)this.budgets.set(watch.thread,Math.min(this.budgets.get(watch.thread)??Infinity,watch.budget));
    this.recover();
  }

  tx(fn){
    this.db.exec('BEGIN IMMEDIATE');
    try{const value=fn();this.db.exec('COMMIT');return value;}catch(error){this.db.exec('ROLLBACK');throw error;}
  }
  run(sql,...args){return this.db.prepare(sql).run(...args);}
  get(sql,...args){return this.db.prepare(sql).get(...args);}
  all(sql,...args){return this.db.prepare(sql).all(...args);}
  now(){return this.clock.now();}

  // A `calling` row may have reached Codex before the crash: its outcome is unknown and it is never called again.
  recover(){
    this.run(`UPDATE wakes SET state='call_unknown',outcome='interrupted',updated_at=? WHERE state='calling'`,this.now());
    this.run(`UPDATE challenges SET state='call_unknown',outcome='interrupted',updated_at=? WHERE state='calling'`,this.now());
  }

  // At start: the key's tool list must hold the passive read (else `tool_not_granted`, a grant for the owner).
  // In registry mode every key is checked at each poll instead, so one key without the tools never stops the others.
  async check(){
    if(this.config.mode==='registry')return;
    const session=await this.client.open();
    if(!(await session.tools()).includes(this.client.tool))throw new DeedKitError('tool_not_granted');
  }

  // A thread's hourly budget counts its wakes and its challenges (ANYM-849), of every key.
  slotFree(thread,now){
    const used=this.get('SELECT count(*) AS n FROM slots WHERE thread=? AND at>?',thread,now-HOUR).n+
      this.get(`SELECT count(*) AS n FROM challenges WHERE thread=? AND created_at>? AND state<>'unsupported_host'`,thread,now-HOUR).n;
    return used<(this.budgets.get(thread)??12);
  }

  // Inside the page transaction: fold into an open wake, or add a wake as `calling` (a slot taken) or `budget_held`.
  record(watch,position,through,acked,now){
    const open=this.get(`SELECT id FROM wakes WHERE subscription_id=? AND (state IN ('budget_held','calling') OR
      (state IN ('queued','queued_not_resumed') AND released=0 AND position>?)) ORDER BY id DESC LIMIT 1`,
      watch.subscriptionId,acked??-1);
    if(open){
      this.run('UPDATE wakes SET through=max(through,?),folded=folded+1,updated_at=? WHERE id=?',through,now,open.id);
      return null;
    }
    if(this.get('SELECT 1 FROM wakes WHERE subscription_id=? AND position=?',watch.subscriptionId,position))return null;
    const free=this.slotFree(watch.thread,now);
    const {lastInsertRowid:id}=this.run(`INSERT INTO wakes(subscription_id,thread,position,through,state,created_at,updated_at)
      VALUES(?,?,?,?,?,?,?)`,watch.subscriptionId,watch.thread,position,through,free?'calling':'budget_held',now,now);
    if(!free)return null;
    this.run('INSERT INTO slots VALUES(?,?,?)',id,watch.thread,now);
    this.run('DELETE FROM slots WHERE at<=?',now-2*HOUR);
    return Number(id);
  }

  // Inside the page transaction: the ACK position reached these wakes. A queued wake it passes stops folding;
  // items folded into it after its position and not yet ACKed get a wake of their own, so none is left unwoken.
  observe(watch,acked,now){
    if(acked===null)return [];
    this.run('UPDATE wakes SET ack_observed=1,updated_at=? WHERE subscription_id=? AND ack_observed=0 AND position<=?',
      now,watch.subscriptionId,acked);
    const calls=[];
    for(const row of this.all(`SELECT id,through FROM wakes WHERE subscription_id=? AND released=0 AND
      state IN ('queued','queued_not_resumed') AND position<=?`,watch.subscriptionId,acked)){
      this.run('UPDATE wakes SET released=1,updated_at=? WHERE id=?',now,row.id);
      if(row.through>acked){const id=this.record(watch,row.through,row.through,acked,now);if(id)calls.push(id);}
    }
    return calls;
  }

  async call(id){
    const row=this.get('SELECT * FROM wakes WHERE id=?',id);
    if(row?.state!=='calling'||this.closed)return;
    this.step('beforeExec');
    let result;
    try{result=await this.codex.queue({thread:row.thread,subscriptionId:row.subscription_id,position:row.position});}
    catch(error){result={state:'failed',outcome:code(error)};}
    this.step('afterExec');
    const now=this.now();
    this.run(`UPDATE wakes SET state=?,outcome=?,called_at=?,updated_at=? WHERE id=? AND state='calling'`,
      result.state,result.outcome,now,now,id);
    this.log({event:'wake',subscription_id:row.subscription_id,position:row.position,state:result.state,outcome:result.outcome});
  }

  // Held wakes follow the current configuration: a subscription no longer watched ends `dropped_config` (never
  // called); a watch whose thread changed moves its held wake to the configured thread.
  reconcile(){
    const now=this.now(),threads=new Map(this.config.watches.map(watch=>[watch.subscriptionId,watch.thread]));
    for(const row of this.all(`SELECT id,subscription_id,thread FROM wakes WHERE state='budget_held'`)){
      const thread=threads.get(row.subscription_id);
      if(thread===undefined)this.run(`UPDATE wakes SET state='dropped_config',outcome='subscription_not_watched',updated_at=? WHERE id=?`,now,row.id);
      else if(thread!==row.thread)this.run(`UPDATE wakes SET thread=?,outcome='thread_changed',updated_at=? WHERE id=?`,thread,now,row.id);
    }
  }

  // Held wakes take a slot as soon as one opens, oldest first. Registry mode calls only the wakes of a current rule.
  async callHeld(rules=null){
    if(rules)this.reconcileRules(rules);else this.reconcile();
    for(const row of this.all(`SELECT id,subscription_id,thread FROM wakes WHERE state='budget_held' ORDER BY id`)){
      if(rules&&rules.get(row.subscription_id)?.thread!==row.thread)continue;
      const now=this.now();
      const id=this.tx(()=>{
        if(!this.slotFree(row.thread,now))return null;
        if(this.run(`UPDATE wakes SET state='calling',updated_at=? WHERE id=? AND state='budget_held'`,now,row.id).changes!==1)return null;
        this.run('INSERT OR REPLACE INTO slots VALUES(?,?,?)',row.id,row.thread,now);
        return row.id;
      });
      if(id)await this.call(id);
    }
  }

  async pollWatch(session,watch){
    let cursor=this.get('SELECT after FROM cursors WHERE subscription_id=?',watch.subscriptionId)?.after;
    if(cursor===undefined){
      // The inspection cursor starts at the subscription's ACK position (read with an empty page above every event).
      const acked=ackedOf(await session.events(watch.subscriptionId,MAX_AFTER,1));
      if(acked===null)bad();
      this.run('INSERT INTO cursors VALUES(?,?,?,?)',watch.subscriptionId,acked,acked,this.now());
      cursor=acked;
    }
    for(let page=0;page<PAGES&&!this.closed;page++){
      const result=await session.events(watch.subscriptionId,cursor,PAGE);
      const acked=ackedOf(result),items=result.items;
      if(!Array.isArray(items))bad();
      const next=result.next_after===undefined?cursor:result.next_after;
      if(!integer(next)||next<cursor)bad();
      let first=null,last=null;
      for(const item of items){
        if(!item||typeof item!=='object'||!integer(item.event_id)||item.event_id<=cursor||item.event_id>next)bad();
        if(wakes(item)){first??=item.event_id;last=item.event_id;}
      }
      const now=this.now();
      this.step('beforePageCommit');
      const calls=this.tx(()=>{
        this.run('UPDATE cursors SET after=?,acked_through=coalesce(?,acked_through),updated_at=? WHERE subscription_id=?',
          next,acked,now,watch.subscriptionId);
        const ids=this.observe(watch,acked,now);
        if(first!==null){const id=this.record(watch,first,last,acked,now);if(id)ids.push(id);}
        return ids;
      });
      cursor=next;
      for(const id of calls)await this.call(id);
      if(result.has_more!==true)break;
    }
  }

  // --- Registry mode (ANYM-849) ---

  // One key's wake targets, checked field by field: server fields are untrusted. An invalid target is dropped with a
  // fixed code (its ID only when that ID is a UUID); the rest are returned with the key's allowed_threads applied.
  targetsOf(key,items){
    const out=[],seen=new Set();
    for(const item of items){
      const id=typeof item?.wake_target_id==='string'&&UUID.test(item.wake_target_id)?item.wake_target_id.toLowerCase():null;
      const ok=id&&typeof item==='object'&&!Array.isArray(item)&&typeof item.kind==='string'&&typeof item.state==='string'&&
        validThread(item.thread)&&Array.isArray(item.subscription_ids)&&
        item.subscription_ids.every(sub=>typeof sub==='string'&&UUID.test(sub))&&
        Number.isInteger(item.hourly_budget)&&item.hourly_budget>=1&&item.hourly_budget<=60&&
        (item.confirmation===null||item.confirmation===undefined||typeof item.confirmation==='string');
      if(!ok){
        this.log({event:'invalid_wake_target',key:key.index,...(id?{wake_target_id:id}:{})});
        if(id&&!seen.has(id)){seen.add(id);out.push({id,outcome:'invalid_wake_target',subscriptions:[]});}
        continue;
      }
      if(seen.has(id))continue;
      seen.add(id);
      const subscriptions=[...new Set(item.subscription_ids.map(sub=>sub.toLowerCase()))];
      const target={id,thread:item.thread,budget:item.hourly_budget,subscriptions,confirmation:item.confirmation??null};
      if(item.kind!=='codex_thread'||item.state!=='enabled')out.push({...target,outcome:null});
      else if(key.allowedThreads&&!key.allowedThreads.includes(item.thread))out.push({...target,outcome:'thread_not_allowed'});
      else out.push({...target,outcome:'active'});
    }
    return out;
  }

  // A key's session and its targets, or a fixed error code: that key is skipped for this poll and its held wakes stay.
  async readRegistry(key){
    try{
      const session=await this.clients[key.index].open();
      const tools=await session.tools();
      if(!tools.includes(key.tool)||!tools.includes(this.config.registryTool))throw new DeedKitError('tool_not_granted');
      let answer;
      try{answer=await session.call(this.config.registryTool,{});}
      catch(error){throw error?.code==='deedkit_bad_response'?new DeedKitError('invalid_registry'):error;}
      if(!Array.isArray(answer?.items))throw new DeedKitError('invalid_registry');
      return {session,targets:this.targetsOf(key,answer.items)};
    }catch(error){return {error:code(error)};}
  }

  // Confirmation: a target becomes a rule on a thread once the latest code it reads back hashes to the one the waker
  // sent there; that pair then stays confirmed locally. Returns 'confirmed', 'challenge' (none sent yet) or 'unconfirmed'.
  confirmation(target){
    const row=this.get('SELECT * FROM challenges WHERE target_id=? AND thread=?',target.id,target.thread);
    if(row?.confirmed)return 'confirmed';
    if(!row||row.state==='unsupported_host')return 'challenge';
    if(typeof target.confirmation==='string'&&createHash('sha256').update(target.confirmation).digest('hex')===row.code_sha256){
      this.run('UPDATE challenges SET confirmed=1,updated_at=? WHERE target_id=? AND thread=?',this.now(),target.id,target.thread);
      return 'confirmed';
    }
    return 'unconfirmed';
  }

  // At most one challenge per target and thread, counted in the thread's budget; only its SHA-256 is kept.
  async challenge(target){
    const now=this.now(),secret=randomBytes(16).toString('hex');
    const sent=this.tx(()=>{
      if(!this.slotFree(target.thread,now))return false;
      this.run(`DELETE FROM challenges WHERE target_id=? AND thread=? AND state='unsupported_host'`,target.id,target.thread);
      this.run(`INSERT INTO challenges(target_id,thread,code_sha256,state,created_at,updated_at) VALUES(?,?,?,'calling',?,?)`,
        target.id,target.thread,createHash('sha256').update(secret).digest('hex'),now,now);
      return true;
    });
    if(!sent||this.closed)return;
    this.step('beforeChallenge');
    let result;
    try{result=await this.codex.challenge({thread:target.thread,targetId:target.id,code:secret});}
    catch(error){result={state:'failed',outcome:code(error)};}
    this.run(`UPDATE challenges SET state=?,outcome=?,updated_at=? WHERE target_id=? AND thread=? AND state='calling'`,
      result.state,result.outcome,this.now(),target.id,target.thread);
    this.log({event:'challenge',wake_target_id:target.id,state:result.state,outcome:result.outcome});
  }

  // Held wakes follow the rules: a wake whose subscription has no rule on its thread any more ends `dropped_config`
  // (`rule_removed`, never called), unless the key it belongs to was skipped this poll (then it is kept, not called).
  reconcileRules(rules){
    const now=this.now();
    // A queued wake on a thread the rule left stops folding, so the next event wakes the rule's current thread.
    for(const row of this.all(`SELECT id,subscription_id,thread FROM wakes WHERE state IN ('queued','queued_not_resumed')
      AND released=0`)){
      const rule=rules.get(row.subscription_id);
      if(rule&&rule.thread!==row.thread)this.run(`UPDATE wakes SET released=1,updated_at=? WHERE id=?`,now,row.id);
    }
    for(const row of this.all(`SELECT id,subscription_id,thread FROM wakes WHERE state='budget_held'`)){
      if(rules.get(row.subscription_id)?.thread===row.thread)continue;
      const ref=this.get('SELECT key_ref FROM subscription_owners WHERE subscription_id=?',row.subscription_id)?.key_ref;
      const owner=ref===undefined?undefined:this.refs.get(ref);
      // Unknown owner: decided only when no key was skipped. A key no longer configured can never wake it again.
      if(ref===undefined?this.skipped.size>0:owner!==undefined&&this.skipped.has(owner))continue;
      this.run(`UPDATE wakes SET state='dropped_config',outcome='rule_removed',updated_at=? WHERE id=?`,now,row.id);
    }
  }

  outcomeOf(target,failed){
    if(target.subscriptions.some(sub=>failed.has(sub)))return 'call_failed';
    const last=this.get(`SELECT state FROM wakes WHERE subscription_id IN (SELECT value FROM json_each(?)) AND thread=?
      ORDER BY id DESC LIMIT 1`,JSON.stringify(target.subscriptions),target.thread);
    if(!last)return 'no_events_yet';
    return ['failed','call_unknown','unsupported_host'].includes(last.state)?'call_failed':'ok';
  }

  async pollRegistry(){
    const reads=[];
    this.skipped=new Set();
    for(const key of this.config.keys){
      if(this.closed)return;
      const read=await this.readRegistry(key);
      reads.push(read);
      if(read.error){
        this.skipped.add(key.index);
        this.log({event:'key_skipped',key:key.index,error:read.error});
        continue;
      }
      this.tx(()=>{
        for(const target of read.targets)for(const sub of target.subscriptions)
          this.run(`INSERT INTO subscription_owners VALUES(?,?) ON CONFLICT(subscription_id) DO UPDATE SET key_ref=excluded.key_ref`,sub,key.ref);
        const budgets=new Map();
        for(const target of read.targets)if(target.outcome==='active')
          budgets.set(target.thread,Math.min(budgets.get(target.thread)??Infinity,target.budget));
        this.run('DELETE FROM key_budgets WHERE key_ref=?',key.ref);
        for(const [thread,budget] of budgets)this.run('INSERT INTO key_budgets VALUES(?,?,?)',key.ref,thread,budget);
      });
    }
    // The lowest budget among a thread's rules of every configured key wins (a skipped key keeps its last read).
    this.budgets=new Map();
    for(const row of this.all('SELECT key_ref,thread,budget FROM key_budgets'))
      if(this.refs.has(row.key_ref))this.budgets.set(row.thread,Math.min(this.budgets.get(row.thread)??Infinity,row.budget));
    // Rules: each enabled, confirmed codex_thread target and each of its subscriptions; a subscription is used once.
    const rules=new Map(),challenges=[];
    for(const [index,read] of reads.entries()){
      if(read.error)continue;
      for(const target of read.targets){
        if(target.outcome!=='active')continue;
        const state=this.confirmation(target);
        target.outcome=state==='confirmed'?'rule':'unconfirmed';
        if(state==='challenge')challenges.push(target);
        if(state!=='confirmed')continue;
        for(const sub of target.subscriptions)if(!rules.has(sub))rules.set(sub,{subscriptionId:sub,thread:target.thread,key:index});
      }
    }
    for(const target of challenges){if(this.closed)return;await this.challenge(target);}
    await this.callHeld(rules);
    const failed=new Set();
    for(const rule of rules.values()){
      if(this.closed)return;
      try{await this.pollWatch(reads[rule.key].session,rule);}
      catch(error){failed.add(rule.subscriptionId);this.log({event:'poll_error',subscription_id:rule.subscriptionId,error:code(error)});}
    }
    const now=this.now();
    this.tx(()=>{
      for(const [index,read] of reads.entries()){
        const count=read.error?0:[...rules.values()].filter(rule=>rule.key===index).length;
        this.run(`INSERT INTO key_polls VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET rules=excluded.rules,
          last_error=excluded.last_error,polled_at=excluded.polled_at`,index,count,read.error??null,now);
        if(read.error)continue;
        this.run('DELETE FROM target_outcomes WHERE key=?',index);
        for(const target of read.targets){
          if(target.outcome===null)continue;
          const outcome=target.outcome==='rule'?this.outcomeOf(target,failed):target.outcome;
          this.run(`INSERT INTO target_outcomes VALUES(?,?,?,?) ON CONFLICT(target_id) DO UPDATE SET key=excluded.key,
            outcome=excluded.outcome,updated_at=excluded.updated_at`,target.id,index,outcome,now);
        }
      }
    });
  }

  async pollOnce(){
    if(this.config.mode==='registry')return this.pollRegistry();
    // The session reads the key first: with an unreadable or swapped key file nothing is called, held wakes included.
    let session;
    try{session=await this.client.open();}
    catch(error){this.log({event:'poll_error',error:code(error)});return;}
    await this.callHeld();
    for(const watch of this.config.watches){
      if(this.closed)return;
      try{await this.pollWatch(session,watch);}
      catch(error){this.log({event:'poll_error',subscription_id:watch.subscriptionId,error:code(error)});}
    }
  }

  start(){
    const loop=()=>{
      if(this.closed)return;
      this.polling=this.pollOnce().catch(error=>this.log({event:'poll_error',error:code(error)}))
        .finally(()=>{if(!this.closed)this.timer=setTimeout(loop,this.config.pollMs);});
    };
    loop();
    return this;
  }

  status(){return statusOf(this.db);}

  async close(){
    this.closed=true;clearTimeout(this.timer);
    await this.polling;
    this.db.close();this.lock.close();
  }
}
