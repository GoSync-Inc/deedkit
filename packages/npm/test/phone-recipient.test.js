import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {AnyMsgClient} from '../lib/index.js';

const declared=readFileSync(new URL('../lib/index.d.ts',import.meta.url),'utf8');

// spec: ANYM-532
test('a message draft takes exactly one recipient, chat_id or phone',()=>{
  const union=/export type MessageRecipient = ([^;\n]+(?:;[^;\n]+)*);\n/.exec(declared)?.[1];
  assert.equal(union,'{chat_id: string; phone?: never} | {phone: string; chat_id?: never}');
  assert.match(declared,/export type MessageDraft = MessageContent & MessageRecipient;/);
  assert.match(declared,/export type MessageAction = MessageDraft & MessageActionFields;/);
  const content=/interface MessageContent \{([\s\S]*?)\n\}/.exec(declared)[1];
  assert.ok(!content.includes('chat_id') && !content.includes('phone'));
});

// spec: ANYM-532
test('preview and send pass a phone recipient and an old chat_id call unchanged',async()=>{
  const seen=[];
  const client=new AnyMsgClient({baseUrl:'https://archive.example',token:'synthetic',
    fetch:async (url,init)=>{seen.push([new URL(url).pathname,JSON.parse(init.body)]);
      return new Response('{"status":"queued"}',{status:200,headers:{'content-type':'application/json'}});}});
  const phone={account:'wa-synthetic',phone:'+12025550147',text:'Synthetic'};
  await client.previewMessage(phone);
  await client.sendMessage({...phone,request_id:'synthetic-phone-1',preview_digest:'a'.repeat(64)});
  await client.sendMessage({account:'wa-synthetic',chat_id:'chat_synthetic',text:'Synthetic',request_id:'synthetic-chat-1'});
  assert.deepEqual(seen.map(([path])=>path),['/v1/preview_message','/v1/send_message','/v1/send_message']);
  assert.deepEqual(seen[0][1],phone);
  assert.equal(seen[1][1].phone,'+12025550147');
  assert.ok(!('phone' in seen[2][1]) && seen[2][1].chat_id==='chat_synthetic');
});
