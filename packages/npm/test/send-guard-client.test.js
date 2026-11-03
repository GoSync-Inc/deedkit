import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {AnyMsgClient,AnyMsgError} from '../lib/index.js';

const options={baseUrl:'https://archive.example/prefix',token:'synthetic-token'};
const declared=readFileSync(new URL('../lib/index.d.ts',import.meta.url),'utf8');
const send={account:'synthetic',chat_id:'chat_synthetic',text:'Confirmed',request_id:'synthetic-send-0001'};

// spec: ANYM-736
test('sendMessage forwards the guard fields unchanged and omits them when absent',async()=>{
  const seen=[];
  const client=new AnyMsgClient({...options,fetch:async(url,request)=>{
    seen.push([String(url),JSON.parse(request.body)]);return Response.json({status:'queued'});
  }});
  await client.sendMessage({...send,expected_last_message_id:'msg_synthetic',guard:'outgoing_only'});
  await client.sendMessage(send);
  assert.deepEqual(seen[0],['https://archive.example/prefix/v1/send_message',
    {...send,expected_last_message_id:'msg_synthetic',guard:'outgoing_only'}]);
  assert.deepEqual(seen[1][1],send);
});

async function refusedSend(code,status){
  let calls=0;
  const client=new AnyMsgClient({...options,fetch:async()=>{
    calls++;return Response.json({error:code,newer_count:2,request_id:'b'.repeat(32)},{status});
  }});
  await assert.rejects(client.sendMessage({...send,expected_last_message_id:'msg_synthetic'}),
    error=>error instanceof AnyMsgError && error.code===code && error.requestId==='b'.repeat(32));
  assert.equal(calls,1);
}

// spec: ANYM-736
test('the guard error chat_changed_since_seen keeps its code and is never retried',()=>refusedSend('chat_changed_since_seen',409));

// spec: ANYM-736
test('the guard error chat_guard_unavailable keeps its code and is never retried',()=>refusedSend('chat_guard_unavailable',503));

// spec: ANYM-736
test('the typings declare the guard fields of a chat_id send',()=>{
  assert.match(declared,/expected_last_message_id\?: string;/);
  assert.match(declared,/guard\?: 'any_message' \| 'outgoing_only';/);
  assert.match(declared,/sendMessage\(args: MessageAction\): Promise<Record<string,unknown>>;/);
});

// spec: ANYM-736
test('getSendStatus forwards a request_id and the typings name exactly one of the two keys',async()=>{
  const seen=[];
  const client=new AnyMsgClient({...options,fetch:async(url,request)=>{
    seen.push(JSON.parse(request.body));return Response.json({status:'queued'});
  }});
  await client.getSendStatus({request_id:'synthetic-send-0001'});
  await client.getSendStatus({send_id:'send_synthetic'});
  assert.deepEqual(seen,[{request_id:'synthetic-send-0001'},{send_id:'send_synthetic'}]);
  assert.match(declared,/getSendStatus\(args: \{request_id:string\}\): Promise<Record<string,unknown>>;/);
});
