import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {AnyMsgClient, AnyMsgError, tools} from '../lib/index.js';

const declared=readFileSync(new URL('../lib/index.d.ts',import.meta.url),'utf8');

function client(seen,status=200,body='{"state":"previewed"}') {
  return new AnyMsgClient({baseUrl:'https://archive.example',token:'synthetic',
    fetch:async (url,init)=>{seen.push([new URL(url).pathname,JSON.parse(init.body)]);
      return new Response(body,{status,headers:{'content-type':'application/json'}});}});
}

// spec: ANYM-547
test('messagesSend posts the same arguments as the tool, with an inline file in the same call',async()=>{
  const seen=[];
  const file={name:'synthetic.bin',mime:'application/octet-stream',base64:Buffer.alloc(65536,7).toString('base64')};
  const args={to:'chat_synthetic',text:'Synthetic caption',files:[file],request_id:'synthetic-inline-1',wait_s:5};
  assert.deepEqual(await client(seen).messagesSend(args),{state:'previewed'});
  assert.deepEqual(seen,[['/v1/messages_send',args]]);
  assert.ok(tools.includes('messages_send'));
  assert.match(declared,/messagesSend\(args: MessagesSendArguments\)/);
  assert.match(declared,/\{name:string; mime:string; base64:string\}/);
});

// spec: ANYM-547
test('messagesSend takes the raised body only for itself; other calls keep their cap',async()=>{
  const seen=[];
  const near={name:'synthetic.bin',mime:'application/octet-stream',base64:Buffer.alloc(262144,1).toString('base64')};
  await client(seen).messagesSend({to:'chat_synthetic',files:[near],dry_run:true});
  await assert.rejects(client(seen).messagesSend({to:'chat_synthetic',files:[{...near,base64:'A'.repeat(524288)}]}),TypeError);
  await assert.rejects(client(seen).previewMessage({account:'a',chat_id:'c',text:'x'.repeat(20000)}),TypeError);
  assert.equal(seen.length,1);
  // An old call is unchanged.
  await client(seen).sendMessage({account:'a',chat_id:'chat_synthetic',text:'Synthetic',request_id:'synthetic-chat-1'});
  assert.deepEqual(seen[1],['/v1/send_message',{account:'a',chat_id:'chat_synthetic',text:'Synthetic',request_id:'synthetic-chat-1'}]);
});

// spec: ANYM-547
test('a messages_send refusal keeps its own code',async()=>{
  const body=JSON.stringify({error:'invalid_arguments',request_id:'a'.repeat(32),field_errors:[{field:'files.0.base64',reason:'max_bytes'}]});
  await assert.rejects(client([],400,body).messagesSend({to:'chat_synthetic',files:[]}),
    error=>error instanceof AnyMsgError && error.code==='invalid_arguments');
});
