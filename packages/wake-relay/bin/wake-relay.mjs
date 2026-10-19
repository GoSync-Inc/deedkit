#!/usr/bin/env node
// wake-relay serve --config <file> [--drop-ack-once] | wake-relay status --config <file>
import fs from 'node:fs';
import {parseArgs} from 'node:util';
import {Relay,loadConfig,readStatus} from '../src/relay.mjs';
import {createServer} from '../src/server.mjs';

const {positionals,values}=parseArgs({allowPositionals:true,options:{config:{type:'string'},'drop-ack-once':{type:'boolean',default:false}}});
const command=positionals[0];
if(!values.config||!['serve','status'].includes(command)){
  console.error('usage: wake-relay serve|status --config <file> [--drop-ack-once]');process.exit(2);
}
const config=JSON.parse(fs.readFileSync(values.config,'utf8'));
const log=entry=>process.stderr.write(JSON.stringify({at:new Date().toISOString(),...entry})+'\n');
if(command==='status'){
  process.stdout.write(JSON.stringify(readStatus(loadConfig(config).database),null,2)+'\n');
}else{
  const relay=new Relay({config,log,dropAckOnce:values['drop-ack-once']}).start();
  const server=createServer(relay);
  const {host='127.0.0.1',port=8787}=config.listen??{};
  server.listen(port,host,()=>log({event:'listening',host,port}));
  const stop=()=>server.close(()=>relay.close().then(()=>process.exit(0)));
  process.once('SIGTERM',stop);process.once('SIGINT',stop);
}
