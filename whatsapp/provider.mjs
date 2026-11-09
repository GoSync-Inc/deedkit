import {recoverNavigation} from './navigation.mjs';
// Private child process. No TCP listener, HTTP API or agent credentials.
import {sendText,sendReceiptEvent} from './outgoing.mjs';
import {executeOperation,OPERATION_ERRORS} from './operations.mjs';
import fs from 'node:fs';
import path from 'node:path';
import {runLane,sendingSocket} from './ipc.mjs';
import {DiskCapture,event,asId,chatId,allows,cursor,compare,readPolicy,envelopeText} from './capture.mjs';
import {download} from './media.mjs';
import {cachedSenderNames,cachedOwnerIds} from './sender.mjs';
import {readPage} from './history.mjs';
import {cachedDialogs} from './read-state.mjs';
import {createPageBounds,PageEvaluationStalled} from './bounds.mjs';
import {CONNECTION_ERRORS,connectClient,writeFailure} from './transport-state.mjs';
import {IdentityCache,cachedIdentityObservations,cachedBidirectionalPair} from './identity.mjs';
import {bindReceiptCapture} from './receipts.mjs';

process.umask(0o077);
const SESSION_LOCKS='/run/deedkit/session-locks';
// Libraries must not mix sensitive diagnostics into the bounded private protocol.
console.log=console.warn=console.error=()=>{};
const cfg=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
// ANYM-914: the browser fallback is not claimed; it never starts where the canonical session lock directory (or
// the configured one) exists, also when run directly rather than through the Python parent's refusal.
if([SESSION_LOCKS,cfg.session_lock_directory].some(p=>typeof p==='string'&&fs.existsSync(p))){
  process.stderr.write('session_claim_unsupported\n');process.exit(1);
}
cfg.max_media_bytes ??= 512*1024**2;
let client,policy,spool,sendReceipts,stopping=false,ready=false,verified=false,ownerIds=[];
let failureRecorded=false;
function failed(code) {
  ready=false;
  if(!failureRecorded){failureRecorded=true;try{writeFailure(cfg.events_directory,code);}catch{}}
}
const pageBounds=createPageBounds();
const identities=new IdentityCache();
function currentPolicy(){
  return readPolicy(cfg.events_directory);
}
async function stop(code=0) {
  if(stopping)return;stopping=true;
  const timer=setTimeout(()=>process.exit(code),10000);timer.unref();
  try{if(client)await client.destroy();}catch{}
  process.exit(code);
}
for(const sig of ['SIGTERM','SIGINT'])process.on(sig,()=>void stop(0));
process.on('uncaughtException',()=>{failed('provider_disconnected');void stop(1);});
process.on('unhandledRejection',()=>{failed('provider_disconnected');void stop(1);});
process.stdin.on('end',()=>void stop(0));
function capture(message,change='message',body=undefined) {
  if(!verified || stopping)return;
  try {
    const chat=chatId(message);
    if(!allows(currentPolicy(),chat))return;
    const value=event(message,body,ownerIds);
    value.identityObservations=identities.forEvent(value);
    spool.put({kind:'whatsapp_event',account:cfg.account,event:value,change,
      observed_at:new Date().toISOString(),origin:'live'});
  } catch(e) {
    // Status broadcasts/newsletters are outside current supported source types.
    // A single message the archive can never represent must not stop capture.
    if(SKIPPED_CAPTURE.has(e.message))return;
    failed(CONNECTION_ERRORS.has(e.message)?e.message:'durable_capture_failed');void stop(1);
  }
}
const SKIPPED_CAPTURE=new Set(['unsupported_chat','invalid_provider_message','invalid_provider_time','provider_id_unavailable']);
function deleted(after,before) {
  if(!verified || stopping)return;
  try {
    const original=after.protocolMessageKey || before?.id || after.id;
    const message=before || {...after,id:original};
    const chat=chatId(message);
    if(!allows(currentPolicy(),chat))return;
    spool.put({kind:'whatsapp_delete',account:cfg.account,chat,ids:[asId(original)],observed_at:new Date().toISOString()});
  }catch(e){if(e.message!=='unsupported_chat'){failed(CONNECTION_ERRORS.has(e.message)?e.message:'durable_capture_failed');void stop(1);}}
}
async function start(initialPolicy) {
  policy=initialPolicy;
  spool=new DiskCapture(cfg.events_directory,{reserveBytes:cfg.disk_reserve_bytes});
  sendReceipts=new DiskCapture(path.join(cfg.events_directory,'send-receipts'),{
    maxBytes:4*1024**2,maxEntries:1000,reserveBytes:cfg.disk_reserve_bytes});
  const {default:pkg}=await import('whatsapp-web.js');
  const {Client,LocalAuth}=pkg;
  // Run as unprivileged UID with Chromium's own sandbox. No --no-sandbox escape.
  client=new Client({authStrategy:new LocalAuth({dataPath:cfg.session_directory}),
    authTimeoutMs:60000,qrMaxRetries:1,takeoverOnConflict:false,
    puppeteer:{headless:true,executablePath:cfg.chromium_executable,args:['--disable-dev-shm-usage']},
    webVersionCache:{type:'local',path:cfg.session_directory+'/web-cache'},
    deviceName:'Personal OS archive',browserName:'Chrome'});
  recoverNavigation(client);
  const connected=connectClient(client,async()=>{
    if(asId(client.info.wid)!==cfg.expected_wid)throw new Error('whatsapp_identity_mismatch');
    ownerIds=[cfg.expected_wid,...await client.pupPage.evaluate(cachedOwnerIds)];
    verified=true;ready=true;
  },failed,stop);
  client.on('message_create',msg=>capture(msg));
  client.on('message_edit',(msg,body)=>capture(msg,'edit',body));
  client.on('message_revoke_everyone',deleted);
  bindReceiptCapture(client,{account:cfg.account,currentPolicy,spool,
    isVerified:()=>verified,isStopping:()=>stopping,
    onFailure:error=>{failed(CONNECTION_ERRORS.has(error.message)?error.message:'durable_capture_failed');void stop(1);}});
  // Local deletion is deliberately not a global tombstone (linked-device history differs).
  try{await connected;}catch(error){failed(error.message);throw error;}
  return {ready:true};
}
async function dialogs(limit) {
  return client.pupPage.evaluate(cachedDialogs,{limit,ownerIds});
}
async function page(chat,limit,before) {
  if(!allows(currentPolicy(),chat))throw new Error('chat_excluded');
  const result=await pageBounds.read(()=>client.pupPage.evaluate(readPage,{chatId:chat,limit,before}));
  const {default:pkg}=await import('whatsapp-web.js');
  const messages=result.raw.map(raw=>event(new pkg.Message(client,raw),undefined,ownerIds)).sort((a,b)=>compare(cursor(a),cursor(b)));
  const names=await pageBounds.names(messages.filter(m=>!m.senderName).map(m=>m.senderId),
    ids=>client.pupPage.evaluate(cachedSenderNames,ids));
  for(const message of messages)if(!message.senderName)
    message.senderName=typeof names[message.senderId]==='string'?envelopeText(names[message.senderId],512):'';
  const observed=await pageBounds.names([...new Set(messages.flatMap(m=>[m.senderId,m.chatId]))],
    ids=>client.pupPage.evaluate(cachedIdentityObservations,ids));
  if(Array.isArray(observed))identities.remember(observed);
  for(const message of messages)message.identityObservations=identities.forEvent(message);
  // A few long bodies can exceed the private protocol frame even at limit=50.
  // Keep the newest contiguous suffix; the next older cursor will fetch the rest.
  while(Buffer.byteLength(JSON.stringify(messages))>900*1024 && messages.length>1){
    messages.shift();result.exhausted=false;
  }
  return {messages,exhausted:result.exhausted,pending:result.pending};
}
async function metadata(chat,id,descriptor) {
  if(!allows(currentPolicy(),chat))throw new Error('chat_excluded');
  const result=await client.pupPage.evaluate(async id=>{
    const collection=window.require('WAWebCollections').Msg;
    const m=collection.get(id) || (await collection.getMessagesById([id]))?.messages?.[0];
    if(!m)return null;
    // Private keys/directPath stay inside this child; never returned to gateway or DB.
    return {model:window.WWebJS.getMessageModel(m),metadata:{mediaKey:m.mediaKey,directPath:m.directPath,
      filehash:m.filehash,encFilehash:m.encFilehash,type:m.type,size:m.size}};
  },id);
  if(!result)throw new Error('media_unavailable');
  const {default:pkg}=await import('whatsapp-web.js');
  const current=event(new pkg.Message(client,result.model),undefined,ownerIds);
  const filename=current.filename || current.messageId.slice(0,120)+'.bin';
  const cleanName=filename.replace(/[\x00-\x1f\x7f/\\]/g,'_');
  if(current.chatId!==chat || current.messageId!==id || !current.hasMedia ||
      descriptor.provider_id!==id || descriptor.bytes!==current.filesize ||
      descriptor.filename!==cleanName || descriptor.mime!==(current.mimetype || 'application/octet-stream'))
    throw new Error('media_reference_changed');
  return result.metadata;
}
const SAFE=new Set([...CONNECTION_ERRORS,...OPERATION_ERRORS,'media_unavailable','media_timeout','media_network_error',
  'media_metadata_unavailable','media_reference_changed','unsupported_media_type','media_hash_mismatch',
  'media_size_mismatch','media_size_limit','invalid_media_path','chat_excluded','provider_response_too_large',
  'provider_page_stalled','send_expired','sending_disabled','send_target_invalid','send_receipt_mismatch']);
async function handle(request,emit) {
    const {method,params={}}=request;let result;
    if(method==='start' && !client)result=await start(params.policy);
    else if(!ready)throw new Error('provider_not_ready');
    else if(method==='policy'){policy=params.policy;result={updated:true};}
    else if(method==='dialogs')result=await dialogs(Math.min(10000,params.limit));
    else if(method==='page')result=await page(params.chat,Math.min(100,params.limit),params.before);
    else if(method==='send_text'){
      const receipt=await sendText(client,params,currentPolicy,{ownerIds,
        verifyAlias:pair=>pageBounds.names([pair],pairs=>client.pupPage.evaluate(cachedBidirectionalPair,pairs[0]))});
      // This exact receipt is fsynced before capture/IPC. Recovery never searches
      // bodies or infers a successful send from an unrelated phone message.
      if(params.send_id)sendReceipts.put(sendReceiptEvent(cfg.account,params,receipt));
      capture(receipt.message);
      result={provider_message_id:receipt.provider_message_id,archive_reference:receipt.archive_reference};
    }
    else if(method==='messenger_operation')result=await executeOperation(client,params,currentPolicy);
    else if(method==='media'){
      const meta=await metadata(params.chat,params.message,params.descriptor);
      for await(const chunk of download(meta,cfg.max_media_bytes))await emit({id:request.id,chunk:chunk.toString('base64')});
      result={verified:true};
    } else throw new Error('unsupported_method');
    return result;
}
async function onError(error) {
  // An uncancellable CDP evaluation still requires restarting this one client.
  if(error instanceof PageEvaluationStalled){failed('provider_page_stalled');await stop(1);}
}
const sending=sendingSocket();
sending.on('end',()=>void stop(0));
try {
  await Promise.all([
    runLane(process.stdin,process.stdout,{methods:['start','policy','dialogs','page','media'],handle,safe:SAFE,onError}),
    runLane(sending,sending,{methods:['send_text','messenger_operation'],handle,safe:SAFE,onError}),
  ]);
}catch{failed('provider_disconnected');await stop(1);}
