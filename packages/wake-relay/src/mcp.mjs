// A bounded DeedKit MCP client for the pull waker (ANYM-844): JSON-RPC over HTTPS POST, a 10 s timeout per request,
// no redirects, at most 1 MiB per response. Errors carry fixed codes only, never the key or a response body.
import https from 'node:https';
import {readSecret} from './relay.mjs';

export const REQUEST_TIMEOUT_MS=10_000,RESPONSE_LIMIT=1024*1024;
const PROTOCOL='2025-06-18',TOKEN=/^[\x21-\x7e]{1,4096}$/;
// Server error codes the waker logs as they are; any other value (it is server-controlled) becomes `tool_error`.
export const SERVER_CODES=new Set(['not_found','not_found_or_forbidden','forbidden','invalid_arguments','invalid_parameters',
  'invalid_version','invalid_subscription','invalid_subscription_state','subscription_closed','subscription_access_changed',
  'service_busy','backend_unavailable','module_not_selected','unsupported_by_backend','tool_not_granted','rate_limited',
  'delivery_disabled']);

export class DeedKitError extends Error{
  constructor(code){super(code);this.code=code;}
}

// The key, read only through the relay's descriptor-based readSecret (O_NOFOLLOW open, then fstat on the descriptor).
export function readKey(file){
  let raw;
  try{raw=readSecret(file);}catch{throw new DeedKitError('unsafe_key_file');}
  const token=raw.toString('utf8').trim();
  if(!TOKEN.test(token))throw new DeedKitError('unsafe_key_file');
  return token;
}

export function postJson(url,headers,body,{request=https.request,timeoutMs=REQUEST_TIMEOUT_MS,limit=RESPONSE_LIMIT}={}){
  return new Promise((resolve,reject)=>{
    let done=false,req;
    const finish=(error,value)=>{
      if(done)return;done=true;clearTimeout(timer);
      if(error){req?.destroy();reject(error);}else resolve(value);
    };
    const timer=setTimeout(()=>finish(new DeedKitError('deedkit_timeout')),timeoutMs);
    const data=Buffer.from(JSON.stringify(body));
    try{
      req=request(url,{method:'POST',headers:{...headers,'content-type':'application/json',
        accept:'application/json, text/event-stream','content-length':String(data.length)}},response=>{
        const status=response.statusCode;
        // A redirect is never followed: the key would go to a URL the configuration did not name.
        if(status>=300&&status<400)return finish(new DeedKitError('deedkit_redirect'));
        const chunks=[];let size=0;
        response.on('data',chunk=>{
          size+=chunk.length;
          if(size>limit)return finish(new DeedKitError('deedkit_response_too_large'));
          chunks.push(chunk);
        });
        response.on('end',()=>finish(null,{status,headers:response.headers,body:Buffer.concat(chunks)}));
        response.on('error',()=>finish(new DeedKitError('deedkit_unreachable')));
      });
    }catch{return finish(new DeedKitError('deedkit_unreachable'));}
    req.on('error',()=>finish(new DeedKitError('deedkit_unreachable')));
    req.end(data);
  });
}

// A JSON body, or the JSON-RPC message of the given id in an event stream.
function message(response,id){
  const text=response.body.toString('utf8');
  const candidates=String(response.headers['content-type']??'').includes('text/event-stream')
    ?text.split(/\r?\n/).filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trim()):[text];
  for(const candidate of candidates){
    let parsed;
    try{parsed=JSON.parse(candidate);}catch{continue;}
    if(parsed&&typeof parsed==='object'&&parsed.id===id)return parsed;
  }
  throw new DeedKitError('deedkit_bad_response');
}

// A tool result as an object; a tool error becomes its own code when it is a plain identifier.
function toolResult(result){
  let value=result?.structuredContent;
  if(value===undefined){
    const text=result?.content?.find?.(part=>part?.type==='text'||typeof part?.text==='string')?.text;
    try{value=JSON.parse(text);}catch{throw new DeedKitError('deedkit_bad_response');}
  }
  if(!value||typeof value!=='object'||Array.isArray(value))throw new DeedKitError('deedkit_bad_response');
  if(result.isError||typeof value.error==='string')
    throw new DeedKitError(SERVER_CODES.has(value.error)?value.error:'tool_error');
  return value;
}

export function deedkitClient({url,keyFile,tool='messages_get_subscription_events',request,timeoutMs}){
  const options={request,timeoutMs};
  return {
    tool,
    // One MCP session per poll: the key is read again each time, so a removed or swapped key file stops the next poll.
    async open(){
      const token=readKey(keyFile);
      let id=0,sessionId=null;
      const send=async(method,params,notify=false)=>{
        const headers={authorization:`Bearer ${token}`,'mcp-protocol-version':PROTOCOL};
        if(sessionId)headers['mcp-session-id']=sessionId;
        const body=notify?{jsonrpc:'2.0',method,params}:{jsonrpc:'2.0',id:++id,method,params};
        const response=await postJson(url,headers,body,options);
        if(response.status<200||response.status>=300)throw new DeedKitError(`deedkit_http_${response.status}`);
        if(notify)return null;
        const reply=message(response,body.id);
        if(reply.error)throw new DeedKitError('deedkit_rpc_error');
        const header=response.headers['mcp-session-id'];
        if(typeof header==='string'&&TOKEN.test(header))sessionId=header;
        return reply.result;
      };
      await send('initialize',{protocolVersion:PROTOCOL,capabilities:{},clientInfo:{name:'deedkit-wake-relay',version:'0.1.0'}});
      await send('notifications/initialized',{},true);
      return {
        async tools(){
          const result=await send('tools/list',{});
          if(!Array.isArray(result?.tools))throw new DeedKitError('deedkit_bad_response');
          return result.tools.map(entry=>entry?.name).filter(name=>typeof name==='string');
        },
        async events(subscriptionId,after,limit){
          const result=await send('tools/call',{name:tool,arguments:{subscription_id:subscriptionId,after,limit,detail:'compact'}});
          return toolResult(result);
        },
        // ANYM-849: one other read-only tool (the key's own wake targets); the result is checked by the caller.
        async call(name,args){return toolResult(await send('tools/call',{name,arguments:args}));},
      };
    },
  };
}
