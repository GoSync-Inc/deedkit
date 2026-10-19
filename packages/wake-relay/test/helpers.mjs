// Synthetic fixtures only: a fake clock, scripted fake downstreams and hints signed like the DeedKit dispatcher.
import {createHmac,randomUUID} from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import {Relay} from '../src/relay.mjs';

export const vector=JSON.parse(fs.readFileSync(new URL('./fixtures/deedkit-hint-vector.json',import.meta.url),'utf8'));
export const SECRET=Buffer.from(vector.secret);
export const RECEIVER='4f6d7a3e-0b5c-4a51-9d7e-3c2b1a0f9e8d';
export const ROUTINE='trig_01SYNTHETICROUTINE';
export const GROK_SECRET='whsec_'+Buffer.from('synthetic grok automation signing key').toString('base64');

export function clock(seconds=Number(vector.headers['X-DeedKit-Timestamp'])){
  return {t:seconds*1000,now(){return this.t;},advance(ms){this.t+=ms;}};
}

// Canonical body bytes and signature exactly as event_delivery.py builds them (sorted keys, compact separators).
export function signed(fields,timestampS,{secret=SECRET}={}){
  const hint={hint_id:randomUUID(),host_turn_verified:false,kind:'events',subscription_id:randomUUID(),subscription_version:1,through:1,...fields};
  const raw=Buffer.from(JSON.stringify(Object.fromEntries(Object.keys(hint).sort().map(key=>[key,hint[key]]))));
  const ts=String(timestampS);
  const signature='sha256='+createHmac('sha256',secret).update(ts+'.').update(raw).digest('hex');
  return {hint,raw,headers:{'content-type':'application/json','x-deedkit-hint-id':hint.hint_id,'x-deedkit-timestamp':ts,'x-deedkit-signature':signature}};
}
export const lower=headers=>Object.fromEntries(Object.entries(headers).map(([key,value])=>[key.toLowerCase(),value]));

const spaces=[];
process.once('exit',()=>{for(const dir of spaces)fs.rmSync(dir,{recursive:true,force:true});});
export function workspace(){
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'wake-relay-'));
  spaces.push(dir);
  const file=(name,value)=>{const target=path.join(dir,name);fs.writeFileSync(target,value,{mode:0o600});return target;};
  return {dir,file,receiverSecret:file('receiver.key',SECRET),claudeToken:file('claude.token','sk-ant-oat01-synthetic-token\n'),
    grokSecret:file('grok.secret',GROK_SECRET)};
}

export function config(space,{adapters,receivers,...rest}={}){
  return {database:path.join(space.dir,'relay.sqlite'),max_hold_s:120,
    adapters:adapters??{claude:{type:'claude-routine',routine_id:ROUTINE,credential_file:space.claudeToken,account:'anthropic'}},
    receivers:receivers??{[RECEIVER]:{secret_file:space.receiverSecret,adapter:'claude'}},...rest};
}

const refused=()=>Object.assign(new Error('connect ECONNREFUSED'),{code:'ECONNREFUSED'});
export const SESSION={status:200,headers:{},body:Buffer.from('{"type":"routine_fire","claude_code_session_id":"session_01SYNTHETIC"}')};

// Each fire attempt takes one script entry: 'refused' fails the connect; 'hang' and 'reset' happen after the
// request is written; an object is the response. The fake counts a fire only when a request is written.
export function downstream(script=[],{clock:time}={}){
  const fake={connects:0,fires:[],script:[...script],
    async connect(url){
      fake.connects++;
      const next=fake.script.length?fake.script.shift():SESSION;
      if(next==='refused')throw refused();
      return {url,next};
    },
    async send(socket,request){
      fake.fires.push({...request,at:time?.now(),payload:payloadOf(request)});
      if(socket.next==='hang')return new Promise(()=>{});
      if(socket.next==='reset')throw Object.assign(new Error('socket hang up'),{code:'ECONNRESET'});
      return {headers:{},body:Buffer.alloc(0),...socket.next};
    },
    close(){},
  };
  return fake;
}
export function payloadOf(request){
  const body=JSON.parse(request.body.toString('utf8'));
  return typeof body.text==='string'?JSON.parse(body.text):body;
}

export function relay(space,options={}){
  const {configOverrides,...rest}=options;
  return new Relay({config:config(space,configOverrides),autoFire:false,...rest});
}
// A process death: no shutdown, the in-flight work is abandoned and the database handle released.
export function crash(instance){instance.closed=true;clearTimeout(instance.timer);instance.db.close();}
export const rows=instance=>instance.all('SELECT * FROM hints ORDER BY received_at,hint_id');

// A local HTTP server for the real `send` path; `handler(req,res,body)` decides the response.
export async function httpServer(handler){
  const requests=[];
  const server=http.createServer((req,res)=>{
    const chunks=[];
    req.on('data',chunk=>chunks.push(chunk));
    req.on('end',()=>{const body=Buffer.concat(chunks);requests.push({method:req.method,url:req.url,headers:req.headers,body});handler(req,res,body);});
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const port=server.address().port;
  return {requests,port,connect:()=>new Promise((resolve,reject)=>{const s=net.connect(port,'127.0.0.1',()=>resolve(s));s.once('error',reject);}),
    close:()=>{server.closeAllConnections();return new Promise(resolve=>server.close(resolve));}};
}
