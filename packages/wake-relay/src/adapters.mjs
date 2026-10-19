// Host adapters: one downstream request shape and one outcome table per vendor (SUBSCRIPTIONS.md "Host receivers").
import {createHmac} from 'node:crypto';

export const CLAUDE_TEXT_LIMIT=65536,WEBHOOK_BODY_LIMIT=1024*1024;
const ROUTINE=/^trig_[A-Za-z0-9]{1,64}$/;

export function retryAfterSeconds(value,nowMs){
  if(typeof value!=='string')return null;
  if(/^[0-9]{1,9}$/.test(value.trim()))return Number(value.trim());
  const at=Date.parse(value);
  return Number.isFinite(at)?Math.max(0,Math.ceil((at-nowMs)/1000)):null;
}

// Outcome of one written request. Only statuses the vendor documents as "refused, no run started" are
// definite; everything else after the first byte was written is `unknown` and never re-fired.
function outcome(table,response,nowMs,vendorId){
  const {status}=response;
  if(status>=200&&status<300){
    // A Claude 2xx without the documented session ID is not a confirmed run: `unknown`, never re-fired.
    const id=vendorId(response);
    return id||!table.runId?{state:'accepted',vendorId:id}:{state:'unknown',outcome:`http_${status}_without_session_id`};
  }
  if(status===429&&table.rateLimited)return {state:'pending',retryAfter:retryAfterSeconds(response.headers['retry-after'],nowMs)};
  if(table.revoked.includes(status))return {state:'revoked'};
  if(table.failed.includes(status))return {state:'failed'};
  return {state:'unknown'};
}

function sessionId(response){
  try{
    const id=JSON.parse(response.body.toString('utf8')).claude_code_session_id;
    return typeof id==='string'&&/^[A-Za-z0-9_-]{1,128}$/.test(id)?id:null;
  }catch{return null;}
}

// POST /v1/claude_code/routines/{trig_id}/fire: 400 invalid request or paused routine, 401 token, 403 access,
// 404 no routine, 429 hourly fire limit with Retry-After; 500 and 503 do not state that no run started.
export function claudeRoutine(config){
  if(!ROUTINE.test(config.routine_id??''))throw new Error('claude-routine needs routine_id trig_...');
  const base=config.api_base??'https://api.anthropic.com';
  if(!/^https:\/\/[^/?#]+$/.test(base))throw new Error('claude-routine api_base must be an https origin');
  const table={failed:[400,404],revoked:[401,403],rateLimited:true,runId:true};
  return {
    type:'claude-routine',url:`${base}/v1/claude_code/routines/${config.routine_id}/fire`,
    build(payload,credential){
      const text=JSON.stringify(payload),token=credential.toString('utf8').trim();
      if(text.length>CLAUDE_TEXT_LIMIT)throw new Error('fire_text_too_large');
      // Checked before `sending`: a malformed token must fail as a definite local error, not an unknown send.
      if(!/^[A-Za-z0-9_-]{1,512}$/.test(token))throw new Error('invalid_credential');
      return {url:this.url,headers:{'authorization':`Bearer ${token}`,
        'anthropic-version':'2023-06-01','content-type':'application/json'},body:Buffer.from(JSON.stringify({text}))};
    },
    classify:(response,nowMs)=>outcome(table,response,nowMs,sessionId),
  };
}

// The key is the canonical base64 after `whsec_`; anything else is a local error before `sending`.
export function webhookKey(secret){
  const encoded=String(secret).trim().replace(/^whsec_/,''),key=Buffer.from(encoded,'base64');
  if(!/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)||key.length<16||key.toString('base64')!==encoded)throw new Error('invalid_credential');
  return key;
}

export function standardWebhookSignature(secret,id,timestamp,body){
  const key=Buffer.from(String(secret).trim().replace(/^whsec_/,''),'base64');
  return 'v1,'+createHmac('sha256',key).update(`${id}.${timestamp}.`).update(body).digest('base64');
}

// Grok Automations webhook trigger, signed per Standard Webhooks: 202 accepted, 400 bad headers, 401 signature,
// stale timestamp or trigger not accepting deliveries, 413 body over 1 MiB, 429 rate limited; 503 is ambiguous.
export function grokAutomation(config){
  if(typeof config.endpoint!=='string'||!config.endpoint.startsWith('https://'))throw new Error('grok-automation needs an https endpoint');
  const table={failed:[400,413],revoked:[401],rateLimited:true};
  return {
    type:'grok-automation',url:config.endpoint,
    build(payload,credential,nowMs){
      const body=Buffer.from(JSON.stringify(payload)),timestamp=String(Math.floor(nowMs/1000));
      if(body.length>WEBHOOK_BODY_LIMIT)throw new Error('webhook_body_too_large');
      webhookKey(credential.toString('utf8'));
      return {url:this.url,body,headers:{'content-type':'application/json','webhook-id':payload.hint_id,
        'webhook-timestamp':timestamp,'webhook-signature':standardWebhookSignature(credential.toString('utf8'),payload.hint_id,timestamp,body)}};
    },
    classify:(response,nowMs)=>outcome(table,response,nowMs,()=>null),
  };
}

export function createAdapter(config){
  if(config.type==='claude-routine')return claudeRoutine(config);
  if(config.type==='grok-automation')return grokAutomation(config);
  throw new Error(`unknown adapter type ${JSON.stringify(config.type)}`);
}
