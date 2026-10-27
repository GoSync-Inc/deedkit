import {test} from 'node:test';
import assert from 'node:assert/strict';
import {AnyMsgClient, AnyMsgError, tools} from '../lib/index.js';

const methods = {
  listMessengerOperations: ['list_messenger_operations', {account:'synthetic'}],
  previewMessengerOperation: ['preview_messenger_operation', {account:'synthetic',
    operation:'react',chat_id:'chat_synthetic',message_id:'msg_synthetic',
    expected_revision:3,parameters:{emoji:'👍'}}],
  executeMessengerOperation: ['execute_messenger_operation', {account:'synthetic',
    operation:'react',chat_id:'chat_synthetic',message_id:'msg_synthetic',expected_revision:3,
    parameters:{emoji:'👍'},request_id:'synthetic-action',preview_digest:'a'.repeat(64)}],
  getMessengerOperation: ['get_messenger_operation', {operation_id:'op_synthetic'}],
};
const options = {baseUrl:'https://archive.example/base',token:'synthetic-token'};

test('all operation SDK methods preserve exact payload and expose the current manifest',async()=>{
  const captured=[];
  const client=new AnyMsgClient({...options,fetch:async(url,request)=>{
    captured.push([String(url),JSON.parse(request.body)]);
    return Response.json({status:'submitted'});
  }});
  for(const [name,[method,args]] of Object.entries(methods)){
    assert.equal((await client[name](args)).status,'submitted');
    assert.ok(tools.includes(method));
  }
  assert.equal(tools.length,51);
  assert.deepEqual(captured,Object.values(methods).map(([method,args])=>[
    `https://archive.example/base/v1/${method}`,args]));
});

for(const [name,[method,args]] of Object.entries(methods))
  test(`${name} rejects unknown outer arguments before transport`,async()=>{
    let calls=0;
    const client=new AnyMsgClient({...options,fetch:async()=>{calls++;return Response.json({});}});
    assert.throws(()=>client[name]({...args,approved:true}),TypeError);
    await assert.rejects(client.call(method,{...args,approved:true}),TypeError);
    assert.equal(calls,0);
  });

const malformed=[{account:1},{operation:null},{chat_id:false},{message_id:4},
  {expected_revision:true},{expected_revision:'3'},{expected_revision:-1},
  {expected_revision:1.5},{expected_revision:NaN},{parameters:null},{parameters:[]},
  {request_id:123},{preview_digest:null},{parameters:{[Symbol('hidden')]:true}},
  {parameters:{caption:undefined}},{parameters:{caption:()=> 'private'}},
  {parameters:{latitude:NaN}},{parameters:{toJSON:()=>({emoji:'different'})}},
  {parameters:{get caption(){throw new Error('private getter');}}}];
for(const extra of malformed)test(`strict operation inputs: ${String(Object.keys(extra)[0])}`,()=>{
  let calls=0;
  const client=new AnyMsgClient({...options,fetch:async()=>{calls++;return Response.json({});}});
  assert.throws(()=>client.executeMessengerOperation({...methods.executeMessengerOperation[1],...extra}),TypeError);
  assert.equal(calls,0);
});

test('missing action identity is rejected; an uncertain receipt is never retried',async()=>{
  let calls=0;
  const client=new AnyMsgClient({...options,fetch:async()=>{
    calls++;return Response.json({status:'unknown',operation_id:'op_synthetic'});
  }});
  assert.throws(()=>client.executeMessengerOperation({account:'synthetic',operation:'mark_read'}),TypeError);
  assert.equal((await client.executeMessengerOperation(methods.executeMessengerOperation[1])).status,'unknown');
  assert.equal(calls,1);
});

for(const code of ['unsupported_operation','invalid_operation_parameters','operation_disabled',
  'sender_not_ready','preview_required','preview_mismatch','operation_target_changed',
  'operation_access_changed','operation_result_unknown'])
  test(`safe operation error ${code}, no retry`,async()=>{
    let calls=0;
    const client=new AnyMsgClient({...options,fetch:async()=>{
      calls++;return Response.json({error:code,detail:'synthetic-private-content'},{status:400});
    }});
    await assert.rejects(client.executeMessengerOperation(methods.executeMessengerOperation[1]),
      error=>error instanceof AnyMsgError&&error.code===code&&!String(error).includes('synthetic-private-content'));
    assert.equal(calls,1);
  });
