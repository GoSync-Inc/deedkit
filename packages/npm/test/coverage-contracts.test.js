import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,symlinkSync,mkdirSync,rmSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawn,execFileSync} from 'node:child_process';
import {createServer} from 'node:http';
import {AnyMsgClient,tools} from '../lib/index.js';

const cli=new URL('../bin/anymsg.js',import.meta.url).pathname;
const metadata=JSON.parse(readFileSync(new URL('../package.json',import.meta.url),'utf8'));
const declared=readFileSync(new URL('../lib/index.d.ts',import.meta.url),'utf8');

const actions={
  prepareFileUpload:{account:'synthetic',chat_id:'chat_synthetic',filename:'fixture.txt',mime:'text/plain',
    bytes:1,sha256:'a'.repeat(64),request_id:'synthetic-upload'},
  uploadFileChunk:{upload_id:'up_'+'a'.repeat(32),offset:0,data_base64:'YQ=='},
  getFileUpload:{upload_id:'up_'+'a'.repeat(32)},
  dismissReply:{chat_id:'chat_synthetic',message_id:'msg_synthetic',expected_revision:2},
  previewMessage:{account:'synthetic',chat_id:'chat_synthetic',text:'Synthetic',format:'markdown',
    reply_to:'msg_synthetic',link_preview:false,allow_lossy:true},
  sendMessage:{account:'synthetic',chat_id:'chat_synthetic',text:'Synthetic',format:'html',
    request_id:'synthetic-send-request',preview_digest:'a'.repeat(64)},
  getSendStatus:{send_id:'send_synthetic'},
  listMessengerOperations:{account:'synthetic'},
  previewMessengerOperation:{account:'synthetic',operation:'send_reaction',chat_id:'chat_synthetic',
    message_id:'msg_synthetic',expected_revision:2,parameters:{emoji:'👍'}},
  executeMessengerOperation:{account:'synthetic',operation:'send_reaction',chat_id:'chat_synthetic',
    message_id:'msg_synthetic',expected_revision:2,parameters:{emoji:'👍'},request_id:'synthetic-operation',preview_digest:'b'.repeat(64)},
  getMessengerOperation:{operation_id:'op_synthetic'},
  createSubscription:{chat_ids:['chat_synthetic'],sender_ids:['person_synthetic'],
    kinds:['message.created','file.transcript.ready'],direction:'incoming',origins:['live'],
    label:'synthetic',expires_at:'2030-01-01T00:00:00Z'},
  listSubscriptions:{limit:4},
  getSubscription:{subscription_id:'sub_synthetic'},
  updateSubscription:{subscription_id:'sub_synthetic',version:2,action:'resume',resume_policy:'skip'},
  getSubscriptionEvents:{subscription_id:'sub_synthetic',after:17,limit:4},
  fetchSubscriptionEvents:{subscription_id:'sub_synthetic',limit:4},
  ackSubscriptionEvents:{subscription_id:'sub_synthetic',receipt:'r'.repeat(43),version:2},
  waitForSubscriptionEvents:{subscription_id:'sub_synthetic',timeout:0.25,limit:4},
  configureSubscriptionDelivery:{subscription_id:'sub_synthetic',version:2,receiver_id:null},
  testSubscription:{subscription_id:'sub_synthetic',request_id:'synthetic-probe'},
  registerDeliveryEndpoint:{request_id:'synthetic-endpoint',url:'https://hooks.example/in',signing_secret:'whsec_'+'A'.repeat(32)},
  listDeliveryEndpoints:{},
  updateDeliveryEndpoint:{endpoint_id:'11111111-1111-4111-8111-111111111111',version:2,action:'pause'},
  deleteDeliveryEndpoint:{endpoint_id:'11111111-1111-4111-8111-111111111111',version:2},
  testDeliveryEndpoint:{endpoint_id:'11111111-1111-4111-8111-111111111111',request_id:'synthetic-probe'},
  submitFeedback:{request_id:'synthetic-feedback',category:'quality',description:'Synthetic issue'},
  listFeedback:{limit:4},
};
const defaults=new Set(['capabilities','listChats','search','searchPeople','searchFiles',
  'getSyncStatus','getUpdates','listSubscriptions','listFeedback','listDeliveryEndpoints']);
const camel=operation=>operation.replace(/_([a-z])/g,(_,letter)=>letter.toUpperCase());

test('every public convenience helper delegates the exact operation and retains server JSON',async()=>{
  const client=new AnyMsgClient({baseUrl:'https://archive.example',token:'synthetic',
    fetch:()=>{throw new Error('delegation probe must not reach transport');}});
  const seen=[];
  const server={items:[{id:'synthetic',state:'partial'}],nested:{unknownFutureField:true}};
  client.call=async (operation,args)=>{seen.push([operation,args]);return server;};
  for(const operation of tools){
    const name=camel(operation);
    const args=actions[name]??{synthetic:'argument'};
    assert.equal(typeof client[name],'function',name);
    assert.strictEqual(await client[name](args),server,name);
    assert.deepEqual(seen.at(-1),[operation,args],name);
    const resultType=['prepareFileUpload','uploadFileChunk','getFileUpload'].includes(name)?'FileUploadStatus':
      name==='listChats'?'ListChatsResult':'Record<string,\\s*unknown>';
    const method=new RegExp(`\\b${name}\\(args(\\?)?:[^\\n]+Promise<${resultType}>;`).exec(declared);
    assert.ok(method,`typed declaration missing for ${name}`);
    assert.equal(Boolean(method[1]),defaults.has(name),name);
  }
  for(const name of defaults){
    assert.strictEqual(await client[name](),server);
    assert.deepEqual(seen.at(-1),[tools.find(operation=>camel(operation)===name),{}]);
  }
});

test('typed action definitions retain required identities and optional reviewed fields',()=>{
  const fields={
    MessageContent:['account: string','text: string','reply_to?: string',
      'format?: MessageFormat','link_preview?: boolean','allow_lossy?: boolean'],
    MessageActionFields:['request_id: string','preview_digest?: string'],
    MessengerOperationPreview:['account: string','operation: string','chat_id?: string',
      'message_id?: string','expected_revision?: number','parameters?: Arguments'],
    MessengerOperationAction:['request_id: string','preview_digest?: string'],
    SubscriptionSelection:['chat_ids: string[]','sender_ids?: string[] | null',
      'kinds?: SubscriptionEventKind[] | null','direction?: \'\' | \'incoming\' | \'outgoing\'',
      'origins?: SubscriptionOrigin[] | null','label?: string','expires_at?: string | null'],
    SubscriptionLifecycleAction:['subscription_id: string','version: number',
      "action: 'pause' | 'resume' | 'close'","resume_policy?: 'catch_up' | 'skip'"],
  };
  for(const [name,required] of Object.entries(fields)){
    const body=new RegExp(`interface ${name}(?: extends \\w+)? \\{([\\s\\S]*?)\\n\\}`).exec(declared)?.[1];
    assert.ok(body,name);
    for(const field of required)assert.ok(body.includes(field+';'),`${name}.${field}`);
  }
  const unions={
    MessageFormat:['plain','markdown','html'],
    SubscriptionEventKind:['message.created','message.edited','message.deleted','message.status',
      'attachment.ready','file.text.ready','file.transcript.ready','file.description.ready','file.processing.failed'],
    SubscriptionOrigin:['live','backfill','reconcile','fixture'],
  };
  for(const [name,members] of Object.entries(unions)){
    const body=new RegExp(`export type ${name} = ([\\s\\S]*?);`).exec(declared)?.[1];
    assert.ok(body,name);
    assert.deepEqual(body.split('|').map(member=>member.trim()),members.map(member=>`'${member}'`),name);
  }
  const methodArguments={
    dismissReply:'args: {chat_id: string; message_id: string; expected_revision: number; reason?: ReplyReason; note?: string}',
    previewMessage:'args: MessageDraft',sendMessage:'args: MessageAction',
    getSendStatus:'args: {send_id:string}',listMessengerOperations:'args: {account:string}',
    previewMessengerOperation:'args: MessengerOperationPreview',
    executeMessengerOperation:'args: MessengerOperationAction',getMessengerOperation:'args: {operation_id:string}',
    createSubscription:'args: SubscriptionSelection',listSubscriptions:'args?: {limit?:number}',
    getSubscription:'args: {subscription_id:string}',updateSubscription:'args: SubscriptionLifecycleAction',
    getSubscriptionEvents:'args: {subscription_id:string; after?:number; limit?:number}',
  };
  const compact=value=>value.replace(/\s+/g,'');
  for(const [name,expected] of Object.entries(methodArguments)){
    const actual=new RegExp(`\\b${name}\\(([^\\n]*)\\): Promise<Record<string,\\s*unknown>>;`).exec(declared)?.[1];
    assert.ok(actual,`typed method missing: ${name}`);
    assert.equal(compact(actual),compact(expected),`typed arguments: ${name}`);
  }
  for(const name of ['code','status','requestId','retryable'])assert.match(declared,new RegExp(`readonly ${name}:`));
  assert.match(declared,/call<T = Record<string, unknown>>/);
  assert.match(declared,/export type Json = null \| boolean \| number \| string \| Json\[\]/);
  assert.match(declared,/export type Arguments = \{\[key: string\]: Json\}/);
  assert.match(declared,/constructor\(options: \{baseUrl: string; token: string; timeoutMs\?: number;/);
});

function run(args){
  return new Promise((resolve,reject)=>{
    const child=spawn(process.execPath,[cli,...args],{stdio:['ignore','pipe','pipe']});
    let stdout='',stderr='';
    child.stdout.setEncoding('utf8');child.stderr.setEncoding('utf8');
    child.stdout.on('data',chunk=>stdout+=chunk);child.stderr.on('data',chunk=>stderr+=chunk);
    const timeout=setTimeout(()=>{child.kill('SIGTERM');reject(new Error('synthetic CLI deadline'));},10000);
    child.once('error',error=>{clearTimeout(timeout);reject(error);});
    child.once('close',code=>{clearTimeout(timeout);resolve({code,stdout,stderr});});
  });
}

test('CLI private token file is trimmed and success/error envelopes use real loopback HTTP',async()=>{
  const directory=mkdtempSync(join(tmpdir(),'anymsg-cli-contract-'));
  const key=join(directory,'private.token');
  writeFileSync(key,'  synthetic-private-token\n',{mode:0o600});
  const requests=[];
  const server=createServer((request,response)=>{
    let body='';request.on('data',chunk=>body+=chunk);
    request.on('end',()=>{
      requests.push({url:request.url,auth:request.headers.authorization,body:JSON.parse(body)});
      response.setHeader('content-type','application/json');
      if(request.url==='/v1/search'){
        response.statusCode=409;
        response.end(JSON.stringify({error:'version_conflict',request_id:'a'.repeat(32),retryable:true,
          detail:'synthetic-private-body'}));
      }else response.end(JSON.stringify({synthetic:true,nested:{state:'partial'}}));
    });
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const url=`http://127.0.0.1:${server.address().port}`;
  try{
    const success=await run(['capabilities','--url',url,'--token-file',key]);
    assert.equal(success.code,0);assert.equal(success.stderr,'');
    assert.equal(success.stdout,JSON.stringify({synthetic:true,nested:{state:'partial'}},null,2)+'\n');
    assert.deepEqual(requests[0],{url:'/v1/capabilities',auth:'Bearer synthetic-private-token',body:{}});
    const failure=await run(['search','--url',url,'--token-file',key,'--json','{"query":"synthetic"}']);
    assert.equal(failure.code,1);assert.equal(failure.stdout,'');
    assert.deepEqual(JSON.parse(failure.stderr),{error:'version_conflict',status:409,
      request_id:'a'.repeat(32),retryable:true});
    assert.deepEqual(requests[1].body,{query:'synthetic'});
    assert.ok(!JSON.stringify([success,failure]).includes('synthetic-private'));
    assert.equal(requests.length,2);
  }finally{
    await new Promise(resolve=>server.close(resolve));
    rmSync(directory,{recursive:true,force:true});
  }
});

test('CLI rejects symlink, public, oversized and nonregular token files before HTTP',async()=>{
  const directory=mkdtempSync(join(tmpdir(),'anymsg-cli-file-'));
  const target=join(directory,'private.token');writeFileSync(target,'synthetic',{mode:0o600});
  const symlink=join(directory,'symlink.token');symlinkSync(target,symlink);
  const publicKey=join(directory,'public.token');writeFileSync(publicKey,'synthetic',{mode:0o644});
  const oversized=join(directory,'oversized.token');writeFileSync(oversized,'x'.repeat(4097),{mode:0o600});
  const nonregular=join(directory,'directory.token');mkdirSync(nonregular,{mode:0o700});
  try{
    for(const path of [symlink,...(process.platform==='win32'?[]:[publicKey]),oversized,nonregular]){
      const result=await run(['capabilities','--url','http://127.0.0.1:1','--token-file',path]);
      assert.equal(result.code,1);assert.equal(result.stdout,'');
      assert.deepEqual(JSON.parse(result.stderr),{error:'invalid_arguments',
        next_action:'Run anymsg --help; use a private token file and valid JSON.'});
    }
    for(const args of [['__raw_rpc__'],['search','extra'],['search','--url','http://127.0.0.1:1'],
      ['search','--url','http://127.0.0.1:1','--token-file',target,'--json','{']]){
      const result=await run(args);assert.equal(result.code,1);
      assert.equal(JSON.parse(result.stderr).error,'invalid_arguments');
    }
    const help=await run([]);assert.equal(help.code,0);
    for(const operation of tools)assert.ok(help.stdout.includes(operation));
    assert.equal((await run(['-v'])).stdout.trim(),metadata.version);
    assert.equal((await run(['-h'])).code,0);
  }finally{rmSync(directory,{recursive:true,force:true});}
});

test('npm dry-run inventory is dependency-free ESM client with no provider or runtime data',()=>{
  assert.equal(metadata.type,'module');
  assert.deepEqual(metadata.exports,{'.':{types:'./lib/index.d.ts',import:'./lib/index.js'}});
  assert.deepEqual(metadata.bin,{anymsg:'bin/anymsg.js'});
  assert.equal(metadata.engines.node,'>=22.14.0');
  assert.equal(metadata.dependencies,undefined);
  assert.equal(metadata.license,'MIT');assert.equal(metadata.author,'GoSync');
  assert.deepEqual(metadata.publishConfig,{access:'public',registry:'https://registry.npmjs.org/'});
  assert.equal(metadata.scripts.test,'node --test test/*.test.js');
  const output=JSON.parse(execFileSync('npm',['pack','--dry-run','--json','--ignore-scripts'],
    {cwd:new URL('..',import.meta.url),encoding:'utf8'}));
  assert.equal(output.length,1);
  const allowed=new Set(['package.json','README.md','LICENSE','THIRD_PARTY_NOTICES.md']);
  const files=output[0].files.map(item=>item.path);
  for(const file of files)assert.ok(allowed.has(file)||file.startsWith('lib/')||file.startsWith('bin/'),file);
  for(const file of ['lib/index.js','lib/index.d.ts','bin/anymsg.js',...allowed])assert.ok(files.includes(file),file);
  assert.deepEqual(output[0].bundled,[]);
});
