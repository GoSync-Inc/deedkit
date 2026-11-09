// Synthetic DeedKit MCP server and fake `codex` shared by the pull waker tests (ANYM-844 codex-waker.test.mjs,
// ANYM-849 codex-registry.test.mjs). Synthetic fixtures only.
import {randomUUID} from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';

export const SUB='0b6f5c1e-3a2d-4e8f-9c7b-1a2b3c4d5e6f';
export const SECRET_TEXT='synthetic message body that must never leave the waker';
// Every server and watcher a test opens is closed after it, also when an assertion fails first.
export const opened=[];
export async function closeOpened(){for(const close of opened.splice(0).reverse())await Promise.resolve().then(close).catch(()=>{});}

// A DeedKit MCP endpoint with one subscription; its passive page is built like subscriptions.get_subscription_events.
// ANYM-849: `state.keys[token]` gives one key its own tool list (`tools`), wake targets (`targets`, shaped like
// wake_targets._view), a tool error (`error`), a raw answer (`answer`) or an HTTP status for every request (`status`).
export async function deedkit({tools=['messages_get_subscription_events'],acked=0}={}){
  const state={items:[],acked,matched:0,requests:0,calls:[],args:[],version:0,transform:null,respond:null,others:{},keys:{}};
  const server=http.createServer((req,res)=>{
    const chunks=[];
    req.on('data',chunk=>chunks.push(chunk));
    req.on('end',()=>{
      state.requests++;
      state.lastAuthorization=req.headers.authorization;
      if(state.respond)return state.respond(req,res);
      const key=state.keys[String(req.headers.authorization??'').replace(/^Bearer /,'')];
      if(key?.status){res.writeHead(key.status);return res.end();}
      const body=JSON.parse(Buffer.concat(chunks).toString('utf8'));
      const reply=result=>{res.writeHead(200,{'content-type':'application/json','mcp-session-id':'session-1'});
        res.end(JSON.stringify({jsonrpc:'2.0',id:body.id,result}));};
      const tool=(value,isError=false)=>reply({content:[{type:'text',text:JSON.stringify(value)}],structuredContent:value,
        ...(isError?{isError:true}:{})});
      if(body.method==='notifications/initialized'){res.writeHead(202);return res.end();}
      if(body.method==='initialize')return reply({protocolVersion:'2025-06-18',capabilities:{tools:{}},serverInfo:{name:'fake'}});
      if(body.method==='tools/list')return reply({tools:(key?.tools??tools).map(name=>({name}))});
      const {name,arguments:args}=body.params;
      state.calls.push(name);state.args.push(args);
      if(name==='messages_list_wake_targets'){
        if(key?.error)return tool({error:key.error},true);
        return tool(key?.answer??{items:key?.targets??[],has_more:false,max_wake_targets:20});
      }
      if(name==='messages_confirm_wake_target'){
        const target=(key?.targets??[]).find(row=>row?.wake_target_id===args.wake_target_id);
        if(!target)return tool({error:'not_found'},true);
        target.confirmation=args.code;target.version++;
        return tool(target);
      }
      const sub=args.subscription_id===SUB?state:(state.others[args.subscription_id]??={items:[],acked:0,matched:0});
      if(sub.error)return tool({error:sub.error},true);
      const visible=sub.items.filter(item=>item.event_id>args.after).slice(0,args.limit+1);
      const items=visible.slice(0,args.limit);
      let page={subscription:{subscription_id:args.subscription_id,state:'active',acked_through:sub.acked,matched_through:sub.matched},
        items,has_more:visible.length>args.limit,next_after:items.length?items.at(-1).event_id:args.after,
        consumption:'passive_read_no_ack'};
      if(state.transform)page=state.transform(page);
      reply({content:[{type:'text',text:JSON.stringify(page)}],...(state.textOnly?{}:{structuredContent:page})});
    });
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  opened.push(()=>{server.closeAllConnections();server.close();});
  let next=1;
  const item=(fields={})=>({event_id:next++,kind:'message.created',direction:'incoming',origin:'live',historical:false,
    wake_reason:['incoming_message'],message:{text:SECRET_TEXT},chat:'chat_synthetic',...fields});
  return {state,url:`http://127.0.0.1:${server.address().port}/mcp`,
    push(count=1,fields={},id=SUB){
      const sub=id===SUB?state:(state.others[id]??={items:[],acked:0,matched:0});
      for(let i=0;i<count;i++){const value=item(fields);sub.items.push(value);sub.matched=value.event_id;}
    },
    sub:id=>id===SUB?state:(state.others[id]??={items:[],acked:0,matched:0}),
    // Another consumer's fetch issues a receipt bound to the consumption version; its ACK checks that version.
    fetch(){return {receipt:randomUUID(),version:state.version,through:state.matched};},
    ack(receipt){if(receipt.version!==state.version)return false;state.version++;state.acked=receipt.through;return true;},
    close(){server.closeAllConnections();return new Promise(resolve=>server.close(resolve));}};
}

// A fake `codex` that logs its argv; its behaviour is read from a mode file at each run.
export function fakeCodex(dir,mode='ok'){
  const file=path.join(dir,'codex'),log=path.join(dir,'codex-calls.jsonl'),modeFile=path.join(dir,'codex-mode');
  fs.writeFileSync(modeFile,mode);
  fs.writeFileSync(file,`#!${process.execPath}
const fs=require('fs');const args=process.argv.slice(2);const mode=fs.readFileSync(${JSON.stringify(modeFile)},'utf8').trim();
if(args[0]==='queue'&&args[1]==='--help'){process.stdout.write(mode==='nothread'?'Usage: codex queue --message <TEXT>\\n':'Usage: codex queue --thread <THREAD> --message <TEXT> [--resume]\\n');process.exit(0);}
fs.appendFileSync(${JSON.stringify(log)},JSON.stringify(args)+'\\n');
if(mode==='warn'){process.stderr.write('Warning: message was queued, but could not resume thread x: start a local daemon or specify a server with --remote\\n');process.exit(0);}
if(mode==='fail')process.exit(3);
if(mode==='hang')setInterval(()=>{},1000);else process.exit(0);
`,{mode:0o755});
  return {file,setMode:value=>fs.writeFileSync(modeFile,value),calls:()=>fs.existsSync(log)?fs.readFileSync(log,'utf8').trim().split('\n').map(line=>JSON.parse(line)):[]};
}
