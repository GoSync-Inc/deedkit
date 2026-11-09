#!/usr/bin/env node
// wake-relay serve --config <file> [--drop-ack-once] | wake-relay status --config <file>
// wake-relay watch --config <file> | wake-relay watch-status --config <file>   (Codex chat pull waker, ANYM-844)
import fs from 'node:fs';
import {parseArgs} from 'node:util';
import {Relay,loadConfig,readStatus} from '../src/relay.mjs';
import {createServer} from '../src/server.mjs';
import {Watcher,loadWatchConfig,readWatchStatus} from '../src/watch.mjs';

const {positionals,values}=parseArgs({allowPositionals:true,options:{config:{type:'string'},'drop-ack-once':{type:'boolean',default:false}}});
const command=positionals[0];
if(!values.config||!['serve','status','watch','watch-status'].includes(command)){
  console.error('usage: wake-relay serve|status|watch|watch-status --config <file> [--drop-ack-once]');process.exit(2);
}
const config=JSON.parse(fs.readFileSync(values.config,'utf8'));
const log=entry=>process.stderr.write(JSON.stringify({at:new Date().toISOString(),...entry})+'\n');
// Start refusals print a fixed code and the config field only, never a key or a response body.
const refuse=error=>{log({event:'refused',error:error.code??'error',...(error.field?{field:error.field,reason:error.message}:{})});process.exit(2);};
if(command==='status'){
  process.stdout.write(JSON.stringify(readStatus(loadConfig(config).database),null,2)+'\n');
}else if(command==='watch-status'){
  let database;
  try{database=loadWatchConfig(config).database;}catch(error){refuse(error);}
  process.stdout.write(JSON.stringify(readWatchStatus(database),null,2)+'\n');
}else if(command==='watch'){
  let watcher;
  try{watcher=new Watcher({config,log});await watcher.check();}catch(error){watcher?.close();refuse(error);}
  log({event:'watching',mode:watcher.config.mode,...(watcher.config.mode==='registry'?{keys:watcher.config.keys.length}:
    {watches:watcher.config.watches.length}),codex:watcher.config.codexPath?'configured':'missing'});
  watcher.start();
  const stop=()=>watcher.close().then(()=>process.exit(0));
  process.once('SIGTERM',stop);process.once('SIGINT',stop);
}else{
  const relay=new Relay({config,log,dropAckOnce:values['drop-ack-once']}).start();
  const server=createServer(relay);
  const {host='127.0.0.1',port=8787}=config.listen??{};
  server.listen(port,host,()=>log({event:'listening',host,port}));
  const stop=()=>server.close(()=>relay.close().then(()=>process.exit(0)));
  process.once('SIGTERM',stop);process.once('SIGINT',stop);
}
