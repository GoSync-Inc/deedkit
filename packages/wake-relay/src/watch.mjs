// Pull waker (ANYM-844, SUBSCRIPTIONS.md "Codex chat (pull waker)"): passive reads of a DeedKit subscription wake an
// existing Codex chat with one content-free `codex queue`. It never fetches with a receipt, never waits and never
// ACKs; the woken chat consumes the events with its own tools. Every wake row commits before the subprocess starts.
import fs from 'node:fs';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {codexQueue,UUID,validThread} from './codex.mjs';
import {DeedKitError,deedkitClient,readKey} from './mcp.mjs';

const HOUR=3600_000,PAGE=50,PAGES=4,MAX_AFTER=2**53-1;
export const WAKE_STATES=['budget_held','calling','queued','queued_not_resumed','failed','unsupported_host','call_unknown'];
export const TOOLS=['messages_get_subscription_events','get_subscription_events'];
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
CREATE INDEX IF NOT EXISTS slots_thread ON slots(thread,at);`;

export class ConfigError extends Error{
  constructor(field,reason){super(`config ${field}: ${reason}`);this.code='invalid_config';this.field=field;}
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
  try{readKey(config.key_file);}catch{fail('key_file','must be an absolute, regular, owner-only (0600) file of one key, not a symlink');}
  const tool=config.tool??TOOLS[0];
  if(!TOOLS.includes(tool))fail('tool',`must be one of ${TOOLS.join(', ')}`);
  const pollS=config.poll_s??30;
  if(!Number.isInteger(pollS)||pollS<10||pollS>3600)fail('poll_s','must be an integer 10..3600');
  let codexPath=null;
  if(config.codex_path!==undefined){
    if(typeof config.codex_path!=='string'||!path.isAbsolute(config.codex_path))fail('codex_path','must be an absolute path');
    codexPath=config.codex_path;
  }else codexPath=onPath(env);
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
  return {database:config.database,deedkitUrl:url.href,keyFile:config.key_file,tool,pollMs:pollS*1000,codexPath,watches};
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
  return {states,budget_held:states.budget_held??0,unsupported_host:states.unsupported_host??0,watches,
    note:'ack_observed means the subscription ACK position reached the wake; it shows consumption progress, not which chat did it'};
}
// `wake-relay watch-status`: read-only, no schema, no recovery. The database holds no key and no event content.
export function readWatchStatus(database){
  const db=new DatabaseSync(database,{readOnly:true});
  try{return statusOf(db);}finally{db.close();}
}

export class Watcher{
  constructor({config,env=process.env,client,codex,clock=Date,step=()=>{},log=()=>{}}){
    this.config=config.watches?.[0]?.subscriptionId?config:loadWatchConfig(config,{env});
    Object.assign(this,{clock,step,log,closed:false,timer:null});
    this.client=client??deedkitClient({url:this.config.deedkitUrl,keyFile:this.config.keyFile,tool:this.config.tool});
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
  }

  // At start: the key's tool list must hold the passive read (else `tool_not_granted`, a grant for the owner).
  async check(){
    const session=await this.client.open();
    if(!(await session.tools()).includes(this.client.tool))throw new DeedKitError('tool_not_granted');
  }

  slotFree(thread,now){
    const used=this.get('SELECT count(*) AS n FROM slots WHERE thread=? AND at>?',thread,now-HOUR).n;
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

  // Held wakes take a slot as soon as one opens, oldest first.
  async callHeld(){
    for(const row of this.all(`SELECT id,thread FROM wakes WHERE state='budget_held' ORDER BY id`)){
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

  async pollOnce(){
    await this.callHeld();
    let session;
    try{session=await this.client.open();}
    catch(error){this.log({event:'poll_error',error:code(error)});return;}
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
