import {test} from 'node:test';
import assert from 'node:assert/strict';
import {AnyMsgClient,AnyMsgError,tools} from '../lib/index.js';

const methods={
  createSubscription:['create_subscription',{chat_ids:['chat_synthetic'],sender_ids:['sender_synthetic'],
    kinds:['message.created','file.transcript.ready'],direction:'incoming',origins:['live','reconcile'],
    label:'synthetic source set',expires_at:'2030-01-01T00:00:00Z'}],
  listSubscriptions:['list_subscriptions',{limit:4}],
  getSubscription:['get_subscription',{subscription_id:'sub_synthetic'}],
  updateSubscription:['update_subscription',{subscription_id:'sub_synthetic',version:2,
    action:'resume',resume_policy:'catch_up'}],
  getSubscriptionEvents:['get_subscription_events',{subscription_id:'sub_synthetic',after:17,limit:4}],
};
const options={baseUrl:'https://archive.example/base',token:'synthetic-token'};

test('all five subscription helpers preserve exact selection and expose the manifest',async()=>{
  const seen=[];
  const client=new AnyMsgClient({...options,fetch:async(url,request)=>{
    seen.push([String(url),JSON.parse(request.body)]);return Response.json({synthetic:true});
  }});
  for(const [method,[operation,args]] of Object.entries(methods)){
    assert.deepEqual(await client[method](args),{synthetic:true});
    assert.ok(tools.includes(operation));
  }
  assert.equal(tools.length,46);
  assert.deepEqual(seen,Object.values(methods).map(([operation,args])=>[
    `https://archive.example/base/v1/${operation}`,args]));
  assert.equal(tools.includes('ack_subscription_events'),true);
});

for(const [method,[operation,args]] of Object.entries(methods))
  test(`${method} rejects unknown task binding before transport`,async()=>{
    let calls=0;
    const client=new AnyMsgClient({...options,fetch:async()=>{calls++;return Response.json({});}});
    assert.throws(()=>client[method]({...args,task_id:'private-unverified-task'}),TypeError);
    await assert.rejects(client.call(operation,{...args,task_id:'private-unverified-task'}),TypeError);
    assert.equal(calls,0);
  });

const invalid=[
  ['createSubscription',{chat_ids:null}],['createSubscription',{chat_ids:[]}],
  ['createSubscription',{chat_ids:Array(65).fill('x')}],
  ['createSubscription',{chat_ids:[1]}],['createSubscription',{chat_ids:['bad\nreference']}],
  ['createSubscription',{chat_ids:Array(1)}],
  ['createSubscription',{chat_ids:Object.defineProperty(['x'],'0',{get(){throw Error('private');}})}],
  ['createSubscription',{sender_ids:[]}],['createSubscription',{sender_ids:true}],
  ['createSubscription',{kinds:['private-kind']}],['createSubscription',{kinds:[]}],
  ['createSubscription',{origins:['unknown']}],['createSubscription',{origins:[]}],
  ['createSubscription',{direction:false}],['createSubscription',{label:'x'.repeat(129)}],
  ['createSubscription',{label:'private\ncontrol'}],
  ['createSubscription',{expires_at:'2030-01-01T00:00:00'}],
  ['createSubscription',{expires_at:'2030-02-30T00:00:00Z'}],
  ['createSubscription',{expires_at:'0000-01-01T00:00:00Z'}],
  ['createSubscription',{expires_at:true}],
  ['listSubscriptions',{limit:0}],['listSubscriptions',{limit:101}],['listSubscriptions',{limit:true}],
  ['getSubscription',{subscription_id:''}],['getSubscription',{subscription_id:1}],
  ['updateSubscription',{version:true}],['updateSubscription',{version:'2'}],
  ['updateSubscription',{version:0}],['updateSubscription',{action:'change_sources'}],
  ['updateSubscription',{resume_policy:'unbounded'}],
  ['getSubscriptionEvents',{after:true}],['getSubscriptionEvents',{after:-1}],
  ['getSubscriptionEvents',{after:Number.MAX_SAFE_INTEGER+1}],['getSubscriptionEvents',{limit:101}],
];
for(const [index,[method,extra]] of invalid.entries())test(`subscription input bound ${index}`,async()=>{
  let calls=0;
  const client=new AnyMsgClient({...options,fetch:async()=>{calls++;return Response.json({});}});
  const [operation,args]=methods[method];
  assert.throws(()=>client[method]({...args,...extra}),TypeError);
  await assert.rejects(client.call(operation,{...args,...extra}),TypeError);
  assert.equal(calls,0);
});

test('local SDK accepts independent readiness kinds and explicit zoned expiry',async()=>{
  const seen=[];
  const client=new AnyMsgClient({...options,fetch:async(_,request)=>{
    seen.push(JSON.parse(request.body));return Response.json({});
  }});
  const args={chat_ids:['chat_synthetic'],sender_ids:null,kinds:['file.text.ready','file.processing.failed'],
    origins:['fixture'],direction:'',label:'🪪'.repeat(128),expires_at:'2032-02-29T00:00:00+08:00'};
  await client.createSubscription(args);
  assert.deepEqual(seen,[args]);
});

for(const code of ['subscription_conflict','subscription_limit','subscription_closed',
  'subscription_access_changed','invalid_subscription_state','invalid_subscription'])
  test(`safe subscription error ${code}, no retry`,async()=>{
    let calls=0;
    const client=new AnyMsgClient({...options,fetch:async()=>{
      calls++;return Response.json({error:code,detail:'synthetic-private-source'},{status:409});
    }});
    await assert.rejects(client.createSubscription({chat_ids:['chat_synthetic']}),
      error=>error instanceof AnyMsgError && error.code===code && !String(error).includes('synthetic-private-source'));
    assert.equal(calls,1);
  });

test('uncertain subscription creation does not retry',async()=>{
  let calls=0;
  const client=new AnyMsgClient({...options,fetch:async()=>{calls++;throw new Error('synthetic-private-diagnostic');}});
  await assert.rejects(client.createSubscription({chat_ids:['chat_synthetic']}),
    error=>error instanceof AnyMsgError && error.code==='transport_error' && !String(error).includes('private'));
  assert.equal(calls,1);
});
