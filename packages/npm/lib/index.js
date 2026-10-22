import {createHash} from 'node:crypto';
import {constants} from 'node:fs';
import {open} from 'node:fs/promises';
import {basename} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

const UPLOAD_CHUNK_BYTES = 262144;
const UPLOAD_BODY_BYTES = 360448;
const UPLOAD_MAX_BYTES = 32 * 1024 * 1024;
const METHODS = Object.freeze([
  'capabilities', 'list_chats', 'search', 'search_people', 'search_files',
  'get_messages', 'get_context', 'get_sync_status', 'resolve_reference',
  'get_attachment', 'request_file_download', 'get_file_text', 'get_file_segments', 'get_file_occurrences',
  'get_updates', 'dismiss_reply', 'ack_updates', 'get_reviews', 'record_review',
  'snooze_reply', 'restore_reply', 'mute_reply_source', 'unmute_reply_source', 'list_reply_dismissals',
  'preview_message','send_message','get_send_status',
  'list_messenger_operations','preview_messenger_operation','execute_messenger_operation','get_messenger_operation',
  'create_subscription','list_subscriptions','get_subscription','update_subscription','get_subscription_events',
  'fetch_subscription_events','ack_subscription_events','wait_for_subscription_events',
  'configure_subscription_delivery','test_subscription','submit_feedback','list_feedback',
  'prepare_file_upload','upload_file_chunk','get_file_upload',
]);
export const tools = METHODS;
const SAFE_ERRORS = new Set([
  'ambiguous_reference', 'attachment_unavailable', 'body_timeout', 'body_too_large',
  'chat_required', 'consumer_filter_mismatch', 'consumer_limit', 'cursor_changed',
  'disconnected', 'forbidden', 'invalid_body', 'invalid_consumer', 'invalid_cursor',
  'invalid_date_range', 'invalid_filter', 'invalid_fuzzy_query', 'invalid_host',
  'invalid_limit', 'invalid_origin', 'invalid_parameters', 'invalid_query',
  'invalid_receipt', 'invalid_utc_date', 'invalid_version', 'not_found',
  'query_timeout', 'rate_limited', 'service_busy', 'service_unavailable',
  'stale_ack', 'temporarily_unavailable', 'unauthorized', 'version_conflict',
  'invalid_message_text','invalid_request_id','idempotency_conflict','sending_disabled','sender_not_ready',
  'invalid_message_format','unsafe_message_markup','invalid_message_entities',
  'formatting_too_complex','unsupported_message_formatting',
  'lossy_formatting_requires_confirmation','message_too_long','preview_required','preview_mismatch',
  'unsupported_operation','invalid_operation_parameters','operation_disabled','operation_not_ready',
  'operation_target_changed','operation_access_changed','operation_result_unknown','already_member',
  'operation_expired','operation_rejected','invalid_operation_result',
  'reply_target_changed','invalid_subscription','subscription_limit','subscription_conflict','subscription_closed',
  'subscription_access_changed','invalid_subscription_state',
  'invalid_upload','invalid_upload_chunk','upload_limit','upload_capacity',
  'upload_expired','upload_offset_conflict','upload_hash_mismatch',
  'media_request_limit',
  'subscription_wait_timeout','subscription_receipt_limit','invalid_feedback','feedback_limit',
  'invalid_receiver','delivery_not_configured','consumer_already_initialized',
  'send_wait_busy','send_status_timeout','send_access_changed','send_expired','send_result_unknown',
]);

const UPLOAD_FIELDS = Object.freeze({
  prepare_file_upload:['account','chat_id','filename','mime','bytes','sha256','request_id'],
  upload_file_chunk:['upload_id','offset','data_base64'],
  get_file_upload:['upload_id'],
});
function uploadArguments(method,args) {
  const fields=UPLOAD_FIELDS[method];
  if (!plainObject(args) || !operationJson(args) || fields.some(key=>!Object.hasOwn(args,key)) ||
      Reflect.ownKeys(args).some(key=>!fields.includes(key))) throw new TypeError('Invalid upload arguments');
  for (const [name,value] of Object.entries(args)) {
    let valid;
    if (['account','chat_id'].includes(name)) valid=subscriptionReference(value);
    else if (name==='filename') valid=typeof value==='string' && value.isWellFormed() &&
      [...value].length>=1 && [...value].length<=255 && !['.','..'].includes(value) && !/[\x00-\x1f\x7f/\\]/.test(value);
    else if (name==='mime') valid=typeof value==='string' && value.length<=128 &&
      /^[a-zA-Z0-9!#$&^_.+-]+\/[a-zA-Z0-9!#$&^_.+-]+$/.test(value);
    else if (name==='bytes') valid=Number.isSafeInteger(value) && value>=1 && value<=UPLOAD_MAX_BYTES;
    else if (name==='sha256') valid=typeof value==='string' && /^[a-f0-9]{64}$/.test(value);
    else if (name==='request_id') valid=typeof value==='string' && /^[a-zA-Z0-9_.:-]{8,128}$/.test(value);
    else if (name==='upload_id') valid=typeof value==='string' && /^up_[a-f0-9]{32}$/.test(value);
    else if (name==='offset') valid=Number.isSafeInteger(value) && value>=0 && value<=UPLOAD_MAX_BYTES;
    else {
      valid=typeof value==='string' && value.length>=1 && value.length<=349528;
      if (valid) {
        const decoded=Buffer.from(value,'base64');
        valid=decoded.length>=1 && decoded.length<=UPLOAD_CHUNK_BYTES && decoded.toString('base64')===value;
      }
    }
    if (!valid) throw new TypeError('Invalid upload arguments');
  }
  return args;
}

function uploadReceipt(value,metadata,{uploadId,offset}={}) {
  if (!plainObject(value) || Object.entries(metadata).some(([key,item])=>key!=='request_id' && value[key]!==item) ||
      typeof value.upload_id!=='string' || !/^up_[a-f0-9]{32}$/.test(value.upload_id) ||
      uploadId!==undefined && value.upload_id!==uploadId ||
      !Number.isSafeInteger(value.offset) || value.offset<0 || value.offset>metadata.bytes ||
      offset!==undefined && value.offset!==offset ||
      value.chunk_bytes!==UPLOAD_CHUNK_BYTES || value.changes_provider_state!==false ||
      value.owner_approval_verified_by_server!==false ||
      !['uploading','ready','expired'].includes(value.status) ||
      value.status!=='expired' && (value.offset===metadata.bytes)!==(value.status==='ready'))
    throw new AnyMsgError('invalid_upload_response');
  if (value.status==='expired') throw new AnyMsgError('upload_expired');
  return value;
}

async function fileSnapshot(handle) {
  const value=await handle.stat({bigint:true});
  if (!value.isFile() || value.size<1n || value.size>BigInt(UPLOAD_MAX_BYTES))
    throw new AnyMsgError('invalid_upload_file');
  return {size:Number(value.size),identity:[value.dev,value.ino,value.size,value.mtimeNs,value.ctimeNs].join(':')};
}
function uploadAbort(signal) {
  if (signal?.aborted) throw new AnyMsgError('request_aborted');
}
async function hashFile(handle,size,signal) {
  const hash=createHash('sha256');
  const buffer=Buffer.allocUnsafe(UPLOAD_CHUNK_BYTES);
  let position=0;
  while(position<size) {
    uploadAbort(signal);
    const length=Math.min(buffer.length,size-position);
    const {bytesRead}=await handle.read(buffer,0,length,position);
    if (bytesRead!==length) throw new AnyMsgError('upload_file_changed');
    hash.update(buffer.subarray(0,bytesRead));
    position+=bytesRead;
  }
  return hash.digest('hex');
}

const MESSENGER_FIELDS = Object.freeze({
  list_messenger_operations: [['account'],['account']],
  preview_messenger_operation: [['account','operation'],
    ['account','operation','chat_id','message_id','expected_revision','parameters']],
  execute_messenger_operation: [['account','operation','request_id'],
    ['account','operation','chat_id','message_id','expected_revision','parameters','request_id','preview_digest']],
  get_messenger_operation: [['operation_id'],['operation_id']],
});
const SUBSCRIPTION_FIELDS = Object.freeze({
  create_subscription: [['chat_ids'],['chat_ids','sender_ids','kinds','direction','origins','label','expires_at',
    'wake_rules','reply_to_send']],
  list_subscriptions: [[],['limit']],
  get_subscription: [['subscription_id'],['subscription_id']],
  update_subscription: [['subscription_id','version','action'],['subscription_id','version','action','resume_policy']],
  get_subscription_events: [['subscription_id'],['subscription_id','after','limit']],
  fetch_subscription_events: [['subscription_id'],['subscription_id','limit']],
  ack_subscription_events: [['subscription_id','receipt','version'],['subscription_id','receipt','version']],
  wait_for_subscription_events: [['subscription_id'],['subscription_id','timeout','limit']],
  configure_subscription_delivery: [['subscription_id','version'],['subscription_id','version','receiver_id']],
  test_subscription: [['subscription_id','request_id'],['subscription_id','request_id']],
});
const FEEDBACK_STATUSES = Object.freeze(['received','accepted','declined','duplicate','fixed','verify_requested','verified','reopened']);
const FEEDBACK_FIELDS = Object.freeze({
  submit_feedback: [['request_id','category','description'],['request_id','category','description','operation','refs']],
  list_feedback: [[],['limit','status']],
});
const SUBSCRIPTION_KINDS = new Set(['message.created','message.edited','message.deleted',
  'message.status','attachment.ready','file.text.ready','file.transcript.ready',
  'file.description.ready','file.processing.failed']);
const SUBSCRIPTION_ORIGINS = new Set(['live','backfill','reconcile','fixture']);
const WAKE_RULES = new Set(['incoming_message','owner_mention','reply_to_owner','reply_to_confirmed_send']);
function plainObject(value) {
  return value !== null && typeof value === 'object' &&
    [Object.prototype,null].includes(Object.getPrototypeOf(value));
}
function operationJson(value,ancestors=new Set(),depth=0) {
  if (depth>32) return false;
  if (value===null || typeof value==='string' || typeof value==='boolean') return true;
  if (typeof value==='number') return Number.isFinite(value);
  if (!Array.isArray(value) && !plainObject(value) || ancestors.has(value)) return false;
  ancestors.add(value);
  let valid;
  if (Array.isArray(value)) {
    const keys=Reflect.ownKeys(value);
    valid=keys.length===value.length+1 && keys.every(key=>{
      if (key==='length') return true;
      const descriptor=Object.getOwnPropertyDescriptor(value,key);
      return typeof key==='string' && /^(0|[1-9]\d*)$/.test(key) &&
        Number(key)<value.length && descriptor && 'value' in descriptor &&
        operationJson(descriptor.value,ancestors,depth+1);
    });
  }
  else valid=Reflect.ownKeys(value).every(key=> {
    const descriptor=Object.getOwnPropertyDescriptor(value,key);
    return typeof key==='string' && descriptor && 'value' in descriptor &&
      operationJson(descriptor.value,ancestors,depth+1);
  });
  ancestors.delete(value);
  return valid;
}
function messengerArguments(method,args) {
  const [required,allowed] = MESSENGER_FIELDS[method];
  if (!plainObject(args) || required.some(key=>!Object.hasOwn(args,key)) ||
      Reflect.ownKeys(args).some(key=>!allowed.includes(key) ||
        !('value' in Object.getOwnPropertyDescriptor(args,key))))
    throw new TypeError('Invalid messenger operation arguments');
  for (const [key,value] of Object.entries(args)) {
    if (key === 'expected_revision') {
      if (!Number.isSafeInteger(value) || value < 0) throw new TypeError('Invalid operation revision');
    } else if (key === 'parameters') {
      if (!plainObject(value) || !operationJson(value))
        throw new TypeError('Operation parameters must be an object with string keys');
    } else if (typeof value !== 'string') throw new TypeError('Operation fields must be strings');
  }
  return args;
}
function subscriptionReference(value) {
  return typeof value==='string' && value.length>0 && [...value].length<=256 && !/[\x00-\x20\x7f]/.test(value);
}
function subscriptionExpiry(value) {
  if (typeof value!=='string' || value.length<20 || value.length>64) return false;
  const parts=/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|([+-])(\d{2}):(\d{2}))$/.exec(value);
  if (!parts) return false;
  const [year,month,day,hour,minute,second]=parts.slice(1,7).map(Number);
  const leap=year%4===0 && (year%100!==0 || year%400===0);
  const days=[31,leap?29:28,31,30,31,30,31,31,30,31,30,31];
  return year>=1 && month>=1 && month<=12 && day>=1 && day<=days[month-1] &&
    hour<=23 && minute<=59 && second<=59 && Number(parts[8]||0)<=23 &&
    Number(parts[9]||0)<=59 && Number.isFinite(Date.parse(value));
}
function waitSeconds(value) {
  if (typeof value!=='number' || !Number.isFinite(value) || value<0 || value>25)
    throw new TypeError('Wait must be a number from 0 to 25 seconds');
  return value;
}
function requestId(value) {
  return typeof value==='string' && /^[a-zA-Z0-9_.:-]{8,128}$/.test(value);
}
function sendWaitArguments(args) {
  if (args && typeof args==='object' && Object.hasOwn(args,'wait_s')) {
    const descriptor=Object.getOwnPropertyDescriptor(args,'wait_s');
    if (!descriptor || !('value' in descriptor)) throw new TypeError('Invalid send wait');
    waitSeconds(descriptor.value);
    if (descriptor.value===0) {
      const {wait_s,...compatible}=args;
      return compatible;
    }
  }
  return args;
}
function subscriptionArguments(method,args) {
  const [required,allowed]=SUBSCRIPTION_FIELDS[method];
  if (!plainObject(args) || !operationJson(args) || required.some(key=>!Object.hasOwn(args,key)) ||
      Reflect.ownKeys(args).some(key=>!allowed.includes(key)))
    throw new TypeError('Invalid subscription arguments');
  for (const [key,value] of Object.entries(args)) {
    let valid=true;
    if (['chat_ids','sender_ids','kinds','origins','wake_rules'].includes(key)) {
      if (value===null && key!=='chat_ids') continue;
      const maximum=key==='kinds'?9:['origins','wake_rules'].includes(key)?4:64;
      valid=Array.isArray(value) && value.length>=1 && value.length<=maximum &&
        value.every(item=>['chat_ids','sender_ids'].includes(key)?subscriptionReference(item):
          typeof item==='string' && (key==='kinds'?SUBSCRIPTION_KINDS:
            key==='origins'?SUBSCRIPTION_ORIGINS:WAKE_RULES).has(item));
    } else if (['limit','version','after'].includes(key)) {
      const [minimum,maximum]={limit:[1,100],version:[1,Number.MAX_SAFE_INTEGER],after:[0,Number.MAX_SAFE_INTEGER]}[key];
      valid=Number.isSafeInteger(value) && value>=minimum && value<=maximum;
    } else if (key==='subscription_id') valid=subscriptionReference(value);
    else if (key==='receipt') valid=typeof value==='string' && [...value].length>=32 && [...value].length<=128;
    else if (key==='timeout') waitSeconds(value);
    else if (key==='request_id') valid=requestId(value);
    else if (key==='receiver_id') valid=value===null || typeof value==='string' &&
      /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(value);
    else if (key==='reply_to_send') valid=value===null || typeof value==='string' && [...value].length>=1 && [...value].length<=128;
    else if (key==='label') valid=typeof value==='string' && [...value].length<=128 && !/[\x00-\x1f\x7f]/.test(value);
    else if (['action','direction','resume_policy'].includes(key))
      valid=typeof value==='string' && {action:['pause','resume','close'],
        direction:['','incoming','outgoing'],resume_policy:['catch_up','skip']}[key].includes(value);
    else if (key==='expires_at') {
      if (value===null) continue;
      valid=subscriptionExpiry(value);
    }
    if (!valid) throw new TypeError('Invalid subscription arguments');
  }
  if (args.wake_rules?.includes('reply_to_confirmed_send') && args.reply_to_send==null)
    throw new TypeError('Confirmed-send wake rule requires a send reference');
  return args;
}
function feedbackArguments(method,args) {
  const [required,allowed]=FEEDBACK_FIELDS[method];
  if (!plainObject(args) || !operationJson(args) || required.some(key=>!Object.hasOwn(args,key)) ||
      Reflect.ownKeys(args).some(key=>!allowed.includes(key))) throw new TypeError('Invalid feedback arguments');
  for (const [key,value] of Object.entries(args)) {
    let valid;
    if (key==='request_id') valid=requestId(value);
    else if (key==='limit') valid=Number.isSafeInteger(value) && value>=1 && value<=100;
    else if (key==='status') valid=typeof value==='string' && FEEDBACK_STATUSES.includes(value);
    else if (key==='category') valid=typeof value==='string' && ['bug','feature','quality','other'].includes(value);
    else if (key==='description') valid=typeof value==='string' && [...value].length>=1 &&
      [...value].length<=4000 && value.trim().length>0 && !/[\x00-\x08\x0b-\x1f\x7f]/.test(value);
    else if (key==='operation') valid=value===null || typeof value==='string' && /^[a-z][a-z0-9_]{0,63}$/.test(value);
    else valid=value===null || plainObject(value) && Object.keys(value).every(name=>
      ['message_ids','chat_ids','subscription_ids'].includes(name) && Array.isArray(value[name]) &&
      value[name].length<=32 && value[name].every(item=>typeof item==='string' &&
        [...item].length>=1 && [...item].length<=256 && !/[\x00-\x1f\x7f]/.test(item))) &&
        Buffer.byteLength(JSON.stringify(value))<=8192;
    if (!valid) throw new TypeError('Invalid feedback arguments');
  }
  return args;
}

export class AnyMsgError extends Error {
  constructor(code, {status = 0, requestId = null, retryable = false} = {}) {
    super(`AnyMsg request failed: ${code}`);
    this.name = 'AnyMsgError';
    this.code = code;
    this.status = status;
    this.requestId = requestId;
    this.retryable = retryable;
  }
}

function base(value) {
  let url;
  try { url = new URL(value); } catch { throw new TypeError('Invalid AnyMsg base URL'); }
  if (url.username || url.password || url.search || url.hash)
    throw new TypeError('Base URL must not contain credentials, query or fragment');
  const local = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && local))
    throw new TypeError('Use HTTPS, or HTTP on loopback for local development');
  url.pathname = url.pathname.replace(/\/+$/, '') + '/';
  if (/\/(mcp|v1)\/$/.test(url.pathname))
    throw new TypeError('Base URL is the server root, without /mcp or /v1');
  return url;
}

async function boundedJson(response, maximum) {
  const length = response.headers.get('content-length');
  if (length && /^\d+$/.test(length) && Number(length) > maximum) {
    await response.body?.cancel();
    throw new AnyMsgError('response_too_large', {status: response.status});
  }
  if (!response.headers.get('content-type')?.toLowerCase().includes('application/json')) {
    await response.body?.cancel();
    throw new AnyMsgError('invalid_response', {status: response.status});
  }
  const reader = response.body?.getReader();
  if (!reader) throw new AnyMsgError('invalid_response', {status: response.status});
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maximum) {
        await reader.cancel();
        throw new AnyMsgError('response_too_large', {status: response.status});
      }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  try {
    const value = JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(bytes));
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error();
    return value;
  } catch { throw new AnyMsgError('invalid_response', {status: response.status}); }
}

export class AnyMsgClient {
  #base;
  #token;
  #fetch;
  #timeout;
  #maximum;
  constructor({baseUrl, token, timeoutMs = 30000, maxResponseBytes = 4 * 1024 * 1024,
    fetch: fetchImpl = globalThis.fetch} = {}) {
    this.#base = base(baseUrl);
    if (typeof token !== 'string' || !/^[!-~]{1,4096}$/.test(token))
      throw new TypeError('A non-empty Bearer token is required');
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 120000)
      throw new TypeError('timeoutMs must be an integer from 1 to 120000');
    if (!Number.isInteger(maxResponseBytes) || maxResponseBytes < 1 || maxResponseBytes > 32 * 1024 * 1024)
      throw new TypeError('maxResponseBytes must be between 1 and 33554432');
    if (typeof fetchImpl !== 'function') throw new TypeError('fetch is required');
    this.#token = token;
    this.#fetch = fetchImpl;
    this.#timeout = timeoutMs;
    this.#maximum = maxResponseBytes;
  }
  async call(method, arguments_ = {}, {signal} = {}) {
    if (!METHODS.includes(method)) throw new TypeError('Unknown AnyMsg operation');
    if (!arguments_ || typeof arguments_ !== 'object' || Array.isArray(arguments_) || 'token' in arguments_)
      throw new TypeError('Arguments must be an object without a token field');
    if (Object.hasOwn(MESSENGER_FIELDS,method)) messengerArguments(method,arguments_);
    if (Object.hasOwn(SUBSCRIPTION_FIELDS,method)) subscriptionArguments(method,arguments_);
    if (Object.hasOwn(FEEDBACK_FIELDS,method)) feedbackArguments(method,arguments_);
    if (method==='send_message') arguments_=sendWaitArguments(arguments_);
    if (Object.hasOwn(UPLOAD_FIELDS,method)) uploadArguments(method,arguments_);
    if (method==='request_file_download' && (!plainObject(arguments_) || !operationJson(arguments_) ||
        Reflect.ownKeys(arguments_).length!==1 || !Object.hasOwn(arguments_,'attachment_id') ||
        typeof arguments_.attachment_id!=='string' || [...arguments_.attachment_id].length<1 ||
        [...arguments_.attachment_id].length>256)) throw new TypeError('Invalid download request arguments');
    let body;
    try { body = JSON.stringify(arguments_); } catch { throw new TypeError('Arguments must be JSON serializable'); }
    const requestMaximum=method==='upload_file_chunk'?UPLOAD_BODY_BYTES:16384;
    if (!body || Buffer.byteLength(body)>requestMaximum) throw new TypeError(`Request exceeds ${requestMaximum} bytes`);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.#timeout);
    const activeSignal = signal ? AbortSignal.any([signal, controller.signal]) : controller.signal;
    try {
      const response = await this.#fetch(new URL(`v1/${method}`, this.#base), {
        method: 'POST', redirect: 'error', signal: activeSignal,
        headers: {'Authorization': `Bearer ${this.#token}`, 'Content-Type': 'application/json', 'Accept': 'application/json'}, body,
      });
      if (response.status >= 300 && response.status < 400) {
        await response.body?.cancel();
        throw new AnyMsgError('redirect_refused', {status: response.status});
      }
      const value = await boundedJson(response, this.#maximum);
      if (!response.ok) {
        const code = typeof value.error === 'string' && SAFE_ERRORS.has(value.error)
          ? value.error : 'http_error';
        const requestId = typeof value.request_id === 'string' && /^[0-9a-f]{32}$/.test(value.request_id)
          ? value.request_id : null;
        throw new AnyMsgError(code, {status: response.status, requestId, retryable: value.retryable === true});
      }
      return value;
    } catch (error) {
      if (error instanceof AnyMsgError) throw error;
      // Do not expose URLs, auth headers or arbitrary server error text.
      throw new AnyMsgError(activeSignal.aborted ? 'request_aborted' : 'transport_error');
    } finally { clearTimeout(timer); }
  }
  capabilities(args = {}) { return this.call('capabilities', args); }
  prepareFileUpload(args,options) {
    return this.call('prepare_file_upload',uploadArguments('prepare_file_upload',args),options);
  }
  uploadFileChunk(args,options) {
    return this.call('upload_file_chunk',uploadArguments('upload_file_chunk',args),options);
  }
  getFileUpload(args,options) {
    return this.call('get_file_upload',uploadArguments('get_file_upload',args),options);
  }
  async uploadFile(file,options={}) {
    // Paths are consumed only here. The HTTP protocol contains a basename and bytes.
    if (typeof file!=='string' || !file || file.includes('://') || file.includes('\x00') || !plainObject(options) ||
        Reflect.ownKeys(options).some(key=>!['account','chat_id','request_id','filename','mime','signal'].includes(key) ||
          !('value' in Object.getOwnPropertyDescriptor(options,key)))) throw new TypeError('Invalid local upload arguments');
    const {account,chat_id,request_id,filename=basename(file),mime='application/octet-stream',signal}=options;
    if (signal!==undefined && !(signal instanceof AbortSignal)) throw new TypeError('Invalid upload signal');
    uploadArguments('prepare_file_upload',{account,chat_id,request_id,filename,mime,bytes:1,sha256:'0'.repeat(64)});
    let handle;
    try {
      uploadAbort(signal);
      if (!Number.isInteger(constants.O_NOFOLLOW)) throw new AnyMsgError('upload_file_unavailable');
      handle=await open(file,constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);
      const snapshot=await fileSnapshot(handle);
      const digest=await hashFile(handle,snapshot.size,signal);
      if ((await fileSnapshot(handle)).identity!==snapshot.identity) throw new AnyMsgError('upload_file_changed');
      const metadata={account,chat_id,request_id,filename,mime:mime.toLowerCase(),bytes:snapshot.size,sha256:digest};
      let receipt=uploadReceipt(await this.prepareFileUpload(metadata,{signal}),metadata);
      const uploadId=receipt.upload_id;
      let offset=receipt.offset;
      let previous=0;
      const buffer=Buffer.allocUnsafe(UPLOAD_CHUNK_BYTES);
      while(offset<snapshot.size) {
        uploadAbort(signal);
        if ((await fileSnapshot(handle)).identity!==snapshot.identity) throw new AnyMsgError('upload_file_changed');
        const length=Math.min(receipt.chunk_bytes,snapshot.size-offset);
        const {bytesRead}=await handle.read(buffer,0,length,offset);
        if (bytesRead!==length) throw new AnyMsgError('upload_file_changed');
        const remaining=150-(performance.now()-previous);
        if (previous && remaining>0) await delay(remaining,undefined,{signal});
        previous=performance.now();
        receipt=uploadReceipt(await this.uploadFileChunk({upload_id:uploadId,offset,
          data_base64:buffer.subarray(0,bytesRead).toString('base64')},{signal}),metadata,
          {uploadId,offset:offset+bytesRead});
        offset=receipt.offset;
      }
      if ((await fileSnapshot(handle)).identity!==snapshot.identity ||
          await hashFile(handle,snapshot.size,signal)!==digest ||
          (await fileSnapshot(handle)).identity!==snapshot.identity) throw new AnyMsgError('upload_file_changed');
      uploadAbort(signal);
      return receipt;
    } catch(error) {
      if (error instanceof AnyMsgError) throw error;
      throw new AnyMsgError(signal?.aborted?'request_aborted':'upload_file_unavailable');
    } finally { await handle?.close(); }
  }
  dismissReply(args) { return this.call('dismiss_reply', args); }
  snoozeReply(args) { return this.call('snooze_reply', args); }
  restoreReply(args) { return this.call('restore_reply', args); }
  muteReplySource(args) { return this.call('mute_reply_source', args); }
  unmuteReplySource(args) { return this.call('unmute_reply_source', args); }
  listReplyDismissals(args) { return this.call('list_reply_dismissals', args); }
  listChats(args = {}) { return this.call('list_chats', args); }
  search(args = {}) { return this.call('search', args); }
  searchPeople(args = {}) { return this.call('search_people', args); }
  searchFiles(args = {}) { return this.call('search_files', args); }
  getMessages(args) { return this.call('get_messages', args); }
  getContext(args) { return this.call('get_context', args); }
  getSyncStatus(args = {}) { return this.call('get_sync_status', args); }
  resolveReference(args) { return this.call('resolve_reference', args); }
  getAttachment(args) { return this.call('get_attachment', args); }
  requestFileDownload(args) { return this.call('request_file_download', args); }
  getFileText(args) { return this.call('get_file_text', args); }
  getFileSegments(args) { return this.call('get_file_segments', args); }
  getFileOccurrences(args) { return this.call('get_file_occurrences', args); }
  getUpdates(args = {}) { return this.call('get_updates', args); }
  ackUpdates(args) { return this.call('ack_updates', args); }
  getReviews(args) { return this.call('get_reviews', args); }
  recordReview(args) { return this.call('record_review', args); }
  previewMessage(args) { return this.call('preview_message',args); }
  sendMessage(args) { return this.call('send_message',sendWaitArguments(args)); }
  getSendStatus(args) { return this.call('get_send_status',args); }
  listMessengerOperations(args) {
    return this.call('list_messenger_operations',messengerArguments('list_messenger_operations',args));
  }
  previewMessengerOperation(args) {
    return this.call('preview_messenger_operation',messengerArguments('preview_messenger_operation',args));
  }
  executeMessengerOperation(args) {
    return this.call('execute_messenger_operation',messengerArguments('execute_messenger_operation',args));
  }
  getMessengerOperation(args) {
    return this.call('get_messenger_operation',messengerArguments('get_messenger_operation',args));
  }
  createSubscription(args) {
    return this.call('create_subscription',subscriptionArguments('create_subscription',args));
  }
  listSubscriptions(args={}) {
    return this.call('list_subscriptions',subscriptionArguments('list_subscriptions',args));
  }
  getSubscription(args) {
    return this.call('get_subscription',subscriptionArguments('get_subscription',args));
  }
  updateSubscription(args) {
    return this.call('update_subscription',subscriptionArguments('update_subscription',args));
  }
  getSubscriptionEvents(args) {
    return this.call('get_subscription_events',subscriptionArguments('get_subscription_events',args));
  }
  fetchSubscriptionEvents(args,options) {
    return this.call('fetch_subscription_events',subscriptionArguments('fetch_subscription_events',args),options);
  }
  ackSubscriptionEvents(args,options) {
    return this.call('ack_subscription_events',subscriptionArguments('ack_subscription_events',args),options);
  }
  waitForSubscriptionEvents(args,options) {
    return this.call('wait_for_subscription_events',subscriptionArguments('wait_for_subscription_events',args),options);
  }
  configureSubscriptionDelivery(args,options) {
    return this.call('configure_subscription_delivery',subscriptionArguments('configure_subscription_delivery',args),options);
  }
  testSubscription(args,options) {
    return this.call('test_subscription',subscriptionArguments('test_subscription',args),options);
  }
  submitFeedback(args,options) {
    return this.call('submit_feedback',feedbackArguments('submit_feedback',args),options);
  }
  listFeedback(args={},options) {
    return this.call('list_feedback',feedbackArguments('list_feedback',args),options);
  }
}
