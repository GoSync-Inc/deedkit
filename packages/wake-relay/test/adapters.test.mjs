import test from 'node:test';
import assert from 'node:assert/strict';
import {createHmac} from 'node:crypto';
import {CLAUDE_TEXT_LIMIT,WEBHOOK_BODY_LIMIT,claudeRoutine,standardWebhookSignature} from '../src/adapters.mjs';
import {httpsTransport} from '../src/transport.mjs';
import {GROK_SECRET,RECEIVER,ROUTINE,clock,httpServer,relay,signed,workspace} from './helpers.mjs';

const HINT_FIELDS=['hint_id','host_turn_verified','kind','subscription_id','subscription_version','through'];
// Content a future or compromised signer might add; none of it may leave through the relay.
const CONTENT={text:'synthetic message body',chat_title:'Synthetic family chat',sender_name:'Synthetic Sender'};

async function capture(t,adapter){
  const vendor=await httpServer((req,res)=>{res.writeHead(adapter.type==='grok-automation'?202:200);res.end('{"claude_code_session_id":"session_01SYNTHETIC"}');});
  t.after(()=>vendor.close());
  const space=workspace(),time=clock();
  const credential_file=adapter.type==='grok-automation'?space.grokSecret:space.claudeToken;
  const instance=relay(space,{clock:time,transport:httpsTransport({connectSocket:vendor.connect}),
    configOverrides:{adapters:{claude:{...adapter,credential_file}}}});
  const hint=signed({...CONTENT,through:42},Math.floor(time.now()/1000));
  assert.equal(instance.receive(RECEIVER,hint.raw,hint.headers).status,202);
  await instance.fireDue();
  await instance.close();
  assert.equal(vendor.requests.length,1);
  return {request:vendor.requests[0],hint:hint.hint,time};
}

// spec: ANYM-228
test('the Claude routine fire request has the documented path, Bearer token, anthropic-version and a text of at most 65,536 characters',async t=>{
  const {request,hint}=await capture(t,{type:'claude-routine',routine_id:ROUTINE});
  assert.equal(request.method,'POST');
  assert.equal(request.url,`/v1/claude_code/routines/${ROUTINE}/fire`);
  assert.equal(request.headers.host,'api.anthropic.com');
  assert.equal(request.headers.authorization,'Bearer sk-ant-oat01-synthetic-token');
  assert.equal(request.headers['anthropic-version'],'2023-06-01');
  assert.equal(request.headers['content-type'],'application/json');
  const body=JSON.parse(request.body);
  assert.deepEqual(Object.keys(body),['text']);
  assert.ok(body.text.length<=CLAUDE_TEXT_LIMIT);
  const text=JSON.parse(body.text);
  assert.deepEqual(Object.keys(text).sort(),HINT_FIELDS);
  assert.deepEqual([text.hint_id,text.through,text.host_turn_verified],[hint.hint_id,42,false]);
  assert.throws(()=>claudeRoutine({routine_id:ROUTINE}).build({kind:'x'.repeat(CLAUDE_TEXT_LIMIT)},Buffer.from('t')),/fire_text_too_large/);
});

// spec: ANYM-228
test('the Standard Webhooks signature matches the reference library test vector',()=>{
  // standard-webhooks libraries/python/tests/test_webhooks.py::test_sign_function
  assert.equal(standardWebhookSignature('whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw','msg_p5jXN8AQM9LWM0D4loKWxJek',1614265330,
    Buffer.from('{"test": 2432232314}')),'v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE=');
});

// spec: ANYM-228
test('the Grok webhook request carries webhook-id, webhook-timestamp and a v1 signature over id.timestamp.body, with a body under 1 MiB',async t=>{
  const {request,hint,time}=await capture(t,{type:'grok-automation',endpoint:'https://grok.example.test/automations/hooks/synthetic'});
  assert.equal(request.url,'/automations/hooks/synthetic');
  assert.equal(request.headers['content-type'],'application/json');
  assert.equal(request.headers['webhook-id'],hint.hint_id);
  assert.equal(request.headers['webhook-timestamp'],String(Math.floor(time.now()/1000)));
  const key=Buffer.from(GROK_SECRET.slice('whsec_'.length),'base64');
  const expected='v1,'+createHmac('sha256',key).update(`${hint.hint_id}.${request.headers['webhook-timestamp']}.`).update(request.body).digest('base64');
  assert.equal(request.headers['webhook-signature'],expected);
  assert.ok(request.body.length<=WEBHOOK_BODY_LIMIT);
  assert.deepEqual(Object.keys(JSON.parse(request.body)).sort(),HINT_FIELDS);
});

// spec: ANYM-228
test('only the content-free hint fields leave the relay, even when the signed body carries message content',async t=>{
  for(const adapter of [{type:'claude-routine',routine_id:ROUTINE},{type:'grok-automation',endpoint:'https://grok.example.test/hook'}]){
    const {request}=await capture(t,adapter);
    const wire=request.body.toString('utf8')+JSON.stringify(request.headers);
    for(const value of Object.values(CONTENT))assert.ok(!wire.includes(value),`${adapter.type} leaked ${value}`);
    for(const key of Object.keys(CONTENT))assert.ok(!wire.includes(`"${key}"`)||key==='text'&&adapter.type==='claude-routine',`${adapter.type} leaked ${key}`);
  }
});
