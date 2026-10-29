import {test} from 'node:test';
import assert from 'node:assert/strict';
import {AnyMsgClient,AnyMsgError,tools} from '../lib/index.js';

const methods={
  fetchSubscriptionEvents:['fetch_subscription_events',{subscription_id:'sub_synthetic',limit:4}],
  ackSubscriptionEvents:['ack_subscription_events',{subscription_id:'sub_synthetic',receipt:'r'.repeat(43),version:2}],
  waitForSubscriptionEvents:['wait_for_subscription_events',{subscription_id:'sub_synthetic',timeout:0.25,limit:4}],
  configureSubscriptionDelivery:['configure_subscription_delivery',{subscription_id:'sub_synthetic',version:2,
    receiver_id:'11111111-1111-4111-8111-111111111111'}],
  testSubscription:['test_subscription',{subscription_id:'sub_synthetic',request_id:'synthetic-probe'}],
  submitFeedback:['submit_feedback',{request_id:'synthetic-feedback',category:'bug',description:'Synthetic issue',
    operation:'get_context',refs:{message_ids:['msg_synthetic'],chat_ids:[],subscription_ids:['sub_synthetic']}}],
  listFeedback:['list_feedback',{limit:4}],
};
const options={baseUrl:'https://archive.example/prefix',token:'synthetic-token'};

test('all seven consumption and feedback helpers preserve wire arguments',async()=>{
  const seen=[];
  const client=new AnyMsgClient({...options,fetch:async(url,request)=>{
    seen.push([String(url),JSON.parse(request.body)]);return Response.json({future_field:{verified:false}});
  }});
  for(const [method,[operation,args]] of Object.entries(methods)){
    assert.equal(typeof client[method],'function',method);
    assert.deepEqual(await client[method](args),{future_field:{verified:false}});
    assert.ok(tools.includes(operation),operation);
  }
  assert.equal(tools.length,52);
  assert.deepEqual(seen,Object.values(methods).map(([operation,args])=>[
    `https://archive.example/prefix/v1/${operation}`,args]));
});

test('wait has an optional cancellation signal and zero send wait stays old-server compatible',async()=>{
  const seen=[];
  const client=new AnyMsgClient({...options,fetch:async(_,request)=>{
    seen.push(JSON.parse(request.body));return Response.json({});
  }});
  await client.waitForSubscriptionEvents({subscription_id:'sub_synthetic'});
  await client.configureSubscriptionDelivery({subscription_id:'sub_synthetic',version:1});
  await client.listFeedback();
  await client.createSubscription({chat_ids:['chat_synthetic']});
  await client.createSubscription({chat_ids:['chat_synthetic'],wake_rules:['reply_to_confirmed_send'],
    reply_to_send:'send_synthetic'});
  const send={account:'synthetic',chat_id:'chat_synthetic',text:'Confirmed',request_id:'synthetic-send'};
  await client.sendMessage({...send,wait_s:0});
  await client.sendMessage({...send,wait_s:0.25});
  assert.deepEqual(seen,[
    {subscription_id:'sub_synthetic'}, {subscription_id:'sub_synthetic',version:1}, {},
    {chat_ids:['chat_synthetic']},
    {chat_ids:['chat_synthetic'],wake_rules:['reply_to_confirmed_send'],reply_to_send:'send_synthetic'},
    send,{...send,wait_s:0.25},
  ]);
  const controller=new AbortController();
  const aborting=new AnyMsgClient({...options,fetch:async(_,request)=>new Promise((_,reject)=>{
    request.signal.addEventListener('abort',()=>reject(Error('private diagnostic')),{once:true});
    controller.abort();
  })});
  await assert.rejects(aborting.waitForSubscriptionEvents({subscription_id:'sub_synthetic'},
    {signal:controller.signal}),error=>error instanceof AnyMsgError && error.code==='request_aborted');
});

const invalid=[
  ['fetchSubscriptionEvents',{subscription_id:''}],
  ['fetchSubscriptionEvents',{limit:true}],['fetchSubscriptionEvents',{limit:101}],
  ['ackSubscriptionEvents',{receipt:'r'.repeat(31)}],['ackSubscriptionEvents',{receipt:'r'.repeat(129)}],
  ['ackSubscriptionEvents',{receipt:false}],['ackSubscriptionEvents',{version:true}],
  ['ackSubscriptionEvents',{version:0}],['ackSubscriptionEvents',{version:Number.MAX_SAFE_INTEGER+1}],
  ...[-1,25.1,true,'1',NaN,Infinity].map(timeout=>['waitForSubscriptionEvents',{timeout}]),
  ['waitForSubscriptionEvents',{limit:0}],['configureSubscriptionDelivery',{version:false}],
  ...['',false,'https://receiver.example/hook','11111111-1111-4111-8111-11111111111z']
    .map(receiver_id=>['configureSubscriptionDelivery',{receiver_id}]),
  ...['short','x'.repeat(129),'private\ncontrol',true].map(request_id=>['testSubscription',{request_id}]),
  ...['unknown',false,['bug']].map(category=>['submitFeedback',{category}]),
  ...['',' ','x'.repeat(4001),'private\x00control',false].map(description=>['submitFeedback',{description}]),
  ...[false,'private\noperation','x'.repeat(130)].map(operation=>['submitFeedback',{operation}]),
  ...[[],{unknown:[]},{message_ids:'message'},{message_ids:Array(33).fill('x')},
    {chat_ids:[false]},{subscription_ids:['']}].map(refs=>['submitFeedback',{refs}]),
  ['listFeedback',{limit:0}],
];
for(const [index,[method,extra]] of invalid.entries())test(`audit input bound ${index}`,async()=>{
  let calls=0;
  const client=new AnyMsgClient({...options,fetch:async()=>{calls++;return Response.json({});}});
  const [operation,args]=methods[method];
  assert.throws(()=>client[method]({...args,...extra}),TypeError);
  await assert.rejects(client.call(operation,{...args,...extra}),TypeError);
  assert.equal(calls,0);
});

for(const [method,[operation,args]] of Object.entries(methods))
  test(`${method} rejects unverified task and URL extras`,async()=>{
    const client=new AnyMsgClient({...options,fetch:async()=>assert.fail('invalid input reached transport')});
    for(const extra of [{task_id:'unverified-task'},{url:'https://receiver.example'}]){
      assert.throws(()=>client[method]({...args,...extra}),TypeError);
      await assert.rejects(client.call(operation,{...args,...extra}),TypeError);
    }
  });

for(const referenceKind of ['message_ids','chat_ids','subscription_ids'])
  for(const [controlName,control] of [['NUL','\x00'],['LF','\n'],['US','\x1f'],['DEL','\x7f']])
    test(`feedback ${referenceKind} rejects ${controlName} before transport`,async()=>{
      let calls=0;
      const client=new AnyMsgClient({...options,fetch:async()=>{calls++;return Response.json({});}});
      const args={...methods.submitFeedback[1],refs:{[referenceKind]:['private'+control+'control']}};
      assert.throws(()=>client.submitFeedback(args),TypeError);
      await assert.rejects(client.call('submit_feedback',args),TypeError);
      assert.equal(calls,0);
    });

for(const [index,extra] of [
  {wake_rules:[]},{wake_rules:['unknown']},{wake_rules:[false]},
  {wake_rules:Array(5).fill('incoming_message')},{reply_to_send:''},
  {reply_to_send:'x'.repeat(129)},{reply_to_send:true},{wake_rules:['reply_to_confirmed_send']},
].entries())test(`wake selector validation ${index}`,()=>{
  const client=new AnyMsgClient({...options,fetch:async()=>assert.fail('invalid input reached transport')});
  assert.throws(()=>client.createSubscription({chat_ids:['chat_synthetic'],...extra}),TypeError);
});

for(const wait_s of [-1,25.1,false,true,'1',NaN,Infinity])test(`invalid send wait ${typeof wait_s}:${wait_s}`,async()=>{
  const client=new AnyMsgClient({...options,fetch:async()=>assert.fail('invalid input reached transport')});
  assert.throws(()=>client.sendMessage({wait_s}),TypeError);
  await assert.rejects(client.call('send_message',{wait_s}),TypeError);
});

for(const code of ['subscription_wait_timeout','subscription_receipt_limit','invalid_feedback',
  'feedback_limit','invalid_receiver','delivery_not_configured','consumer_already_initialized',
  'send_wait_busy','send_status_timeout','send_access_changed','send_expired','send_result_unknown'])
  test(`safe audit error ${code}, no retry`,async()=>{
    let calls=0;
    const client=new AnyMsgClient({...options,fetch:async()=>{
      calls++;return Response.json({error:code,detail:'synthetic-private-source',request_id:'b'.repeat(32)},{status:409});
    }});
    await assert.rejects(client.listFeedback(),error=>error instanceof AnyMsgError && error.code===code &&
      error.requestId==='b'.repeat(32) && !String(error).includes('synthetic-private-source'));
    assert.equal(calls,1);
  });

for(const method of ['ackSubscriptionEvents','configureSubscriptionDelivery','testSubscription','submitFeedback'])
  test(`${method} uncertain mutation never retries`,async()=>{
    let calls=0;
    const client=new AnyMsgClient({...options,fetch:async()=>{calls++;throw Error('private diagnostic');}});
    await assert.rejects(client[method](methods[method][1]),error=>error instanceof AnyMsgError &&
      error.code==='transport_error' && !String(error).includes('private'));
    assert.equal(calls,1);
  });

// spec: ANYM-343
test('listFeedback forwards a status filter and rejects an unknown status before transport',async()=>{
  const seen=[];
  const client=new AnyMsgClient({...options,fetch:async(url,request)=>{
    seen.push(JSON.parse(request.body));return Response.json({items:[{id:'synthetic',status:'verify_requested',
      fixed_in:'deedkit@'+'b'.repeat(40),spec_ref:'ANYM-274',status_note:null,duplicate_of:null,
      status_changed_at:'2026-10-21T00:00:00Z'}],has_more:false});
  }});
  const page=await client.listFeedback({limit:5,status:'verify_requested'});
  assert.equal(page.items[0].status,'verify_requested');
  for(const status of ['planned','',null,7,['fixed']])
    assert.throws(()=>client.listFeedback({status}),TypeError);
  assert.deepEqual(seen,[{limit:5,status:'verify_requested'}]);
});

// spec: ANYM-342
test('feedback operation accepts the longest tool name before transport',async()=>{
  const seen=[];
  const client=new AnyMsgClient({...options,fetch:async(url,init)=>{seen.push(JSON.parse(init.body));return Response.json({});}});
  const [,args]=methods.submitFeedback;
  await client.submitFeedback({...args,operation:'calendar_'+'a'.repeat(120)});
  assert.deepEqual(seen.map(body=>body.operation),['calendar_'+'a'.repeat(120)]);
});


const ENDPOINT='11111111-1111-4111-8111-111111111111';
const SECRET='whsec_'+'c3ludGhldGljLXNlY3JldC1vbmx5LWZvci10ZXN0cw==';
const endpointMethods={
  registerDeliveryEndpoint:['register_delivery_endpoint',{request_id:'synthetic-endpoint',url:'https://hooks.example/in',
    signing_secret:SECRET,label:'synthetic',headers:{'X-Api-Key':'synthetic-canary-value'}}],
  listDeliveryEndpoints:['list_delivery_endpoints',{}],
  updateDeliveryEndpoint:['update_delivery_endpoint',{endpoint_id:ENDPOINT,version:2,action:'rotate_secret',new_secret:SECRET}],
  deleteDeliveryEndpoint:['delete_delivery_endpoint',{endpoint_id:ENDPOINT,version:3}],
  testDeliveryEndpoint:['test_delivery_endpoint',{endpoint_id:ENDPOINT,request_id:'synthetic-probe'}],
};

// spec: ANYM-520
test('the attempt listing helper sends its wire arguments and checks them before transport',async()=>{
  const seen=[];
  const client=new AnyMsgClient({...options,fetch:async(url,request)=>{
    seen.push([String(url),JSON.parse(request.body)]);return Response.json({items:[]});
  }});
  assert.ok(tools.includes('list_delivery_attempts'));
  await client.listDeliveryAttempts({endpoint_id:ENDPOINT});
  await client.listDeliveryAttempts({endpoint_id:ENDPOINT,limit:5,before:ENDPOINT});
  for(const args of [{endpoint_id:ENDPOINT,limit:51},{endpoint_id:ENDPOINT,before:'attempt-1'},{limit:5}])
    await assert.rejects(async()=>client.listDeliveryAttempts(args),TypeError);
  assert.deepEqual(seen,[['https://archive.example/prefix/v1/list_delivery_attempts',{endpoint_id:ENDPOINT}],
    ['https://archive.example/prefix/v1/list_delivery_attempts',{endpoint_id:ENDPOINT,limit:5,before:ENDPOINT}]]);
});


// spec: ANYM-519
test('the five endpoint helpers send their wire arguments to the REST operations',async()=>{
  const seen=[];
  const client=new AnyMsgClient({...options,fetch:async(url,request)=>{
    seen.push([String(url),JSON.parse(request.body)]);return Response.json({});
  }});
  for(const [method,[operation,args]] of Object.entries(endpointMethods)){
    assert.ok(tools.includes(operation),operation);
    await client[method](args);
  }
  await client.listDeliveryEndpoints();
  await client.createSubscription({chat_ids:['chat_synthetic'],delivery:{endpoint_id:ENDPOINT,delivery_policy:{debounce_s:5}}});
  await client.configureSubscriptionDelivery({subscription_id:'sub_synthetic',version:2,endpoint_id:ENDPOINT});
  assert.deepEqual(seen,[...Object.values(endpointMethods).map(([operation,args])=>[
    `https://archive.example/prefix/v1/${operation}`,args]),
    ['https://archive.example/prefix/v1/list_delivery_endpoints',{}],
    ['https://archive.example/prefix/v1/create_subscription',{chat_ids:['chat_synthetic'],
      delivery:{endpoint_id:ENDPOINT,delivery_policy:{debounce_s:5}}}],
    ['https://archive.example/prefix/v1/configure_subscription_delivery',{subscription_id:'sub_synthetic',version:2,endpoint_id:ENDPOINT}]]);
});

const ENDPOINT_ARGUMENT_CASES=[
  ['registerDeliveryEndpoint',{request_id:'synthetic-endpoint',url:'https://hooks.example',signing_secret:'plain-secret'}],
  ['registerDeliveryEndpoint',{request_id:'synthetic-endpoint',url:'',signing_secret:SECRET}],
  ['registerDeliveryEndpoint',{request_id:'synthetic-endpoint',url:'https://hooks.example/'+'x'.repeat(2048),signing_secret:SECRET}],
  ['registerDeliveryEndpoint',{request_id:'synthetic-endpoint',url:'https://hooks.example',signing_secret:SECRET,
    headers:{a:'1',b:'2',c:'3',d:'4',e:'5'}}],
  ['registerDeliveryEndpoint',{request_id:'synthetic-endpoint',url:'https://hooks.example',signing_secret:SECRET,headers:{a:1}}],
  ['registerDeliveryEndpoint',{url:'https://hooks.example',signing_secret:SECRET}],
  ['registerDeliveryEndpoint',{request_id:'synthetic-endpoint',url:'https://hooks.example',signing_secret:SECRET,receiver_id:ENDPOINT}],
  ['updateDeliveryEndpoint',{endpoint_id:ENDPOINT,version:1,action:'close'}],
  ['updateDeliveryEndpoint',{endpoint_id:'not-a-uuid',version:1,action:'pause'}],
  ['updateDeliveryEndpoint',{endpoint_id:ENDPOINT,version:0,action:'pause'}],
  ['deleteDeliveryEndpoint',{endpoint_id:null,version:1}],
  ['testDeliveryEndpoint',{endpoint_id:ENDPOINT,request_id:'short'}],
  ['listDeliveryEndpoints',{limit:5}],
  ['createSubscription',{chat_ids:['chat_synthetic'],delivery:{endpoint_id:'x'}}],
  ['createSubscription',{chat_ids:['chat_synthetic'],delivery:{endpoint_id:ENDPOINT,url:'https://hooks.example'}}],
  ['configureSubscriptionDelivery',{subscription_id:'sub_synthetic',version:1,endpoint_id:'x'}],
];
// spec: ANYM-519
test('endpoint arguments are validated before the transport',()=>{
  const client=new AnyMsgClient({...options,fetch:async()=>assert.fail('invalid input reached transport')});
  for(const [index,[method,args]] of ENDPOINT_ARGUMENT_CASES.entries())
    assert.throws(()=>client[method](args),error=>error instanceof TypeError && !String(error).includes(SECRET),
      'case '+index);
});

// spec: ANYM-519
test('update_subscription keeps its own actions and endpoint errors are safe codes',async()=>{
  const client=new AnyMsgClient({...options,fetch:async()=>Response.json(
    {error:'reserved_header',detail:'synthetic-private-source',request_id:'b'.repeat(32)},{status:400})});
  assert.throws(()=>client.updateSubscription({subscription_id:'sub_synthetic',version:1,action:'rotate_secret'}),TypeError);
  await assert.rejects(client.listDeliveryEndpoints(),error=>error instanceof AnyMsgError && error.code==='reserved_header' &&
    !String(error).includes('synthetic-private-source'));
});
