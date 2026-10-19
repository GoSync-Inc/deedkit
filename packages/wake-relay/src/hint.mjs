// DeedKit signed hint verification (SUBSCRIPTIONS.md "Host receivers").
import {createHmac,timingSafeEqual} from 'node:crypto';

export const MAX_BODY=64*1024,PAST_S=300,FUTURE_S=60;
const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const KIND=/^[a-z][a-z0-9._-]{0,63}$/;
const reject=(status,error)=>({status,error});

// Only these content-free fields ever leave the relay; anything else in a signed body is dropped.
export function project(body){
  if(body===null||typeof body!=='object'||Array.isArray(body))return null;
  const {hint_id,subscription_id,subscription_version,through,kind,host_turn_verified}=body;
  if(typeof hint_id!=='string'||!UUID.test(hint_id)||typeof subscription_id!=='string'||!UUID.test(subscription_id))return null;
  if(!Number.isSafeInteger(subscription_version)||subscription_version<1||!Number.isSafeInteger(through)||through<0)return null;
  if(typeof kind!=='string'||!KIND.test(kind)||host_turn_verified!==false)return null;
  return {hint_id,subscription_id,subscription_version,through,kind,host_turn_verified:false};
}

// The signature covers timestamp and body only, so the header hint ID must equal the signed body hint_id.
export function verifyHint(raw,headers,secret,nowMs){
  if(raw.length>MAX_BODY)return reject(413,'body_too_large');
  const ts=headers['x-deedkit-timestamp'],signature=headers['x-deedkit-signature'],header=headers['x-deedkit-hint-id'];
  if(typeof ts!=='string'||!/^[0-9]{1,12}$/.test(ts)||typeof signature!=='string'||typeof header!=='string')
    return reject(401,'signature_headers_missing');
  const signedAt=Number(ts),now=Math.floor(nowMs/1000);
  if(signedAt<now-PAST_S||signedAt>now+FUTURE_S)return reject(401,'timestamp_outside_window');
  const expected=Buffer.from('sha256='+createHmac('sha256',secret).update(ts+'.').update(raw).digest('hex'));
  const given=Buffer.from(signature);
  if(given.length!==expected.length||!timingSafeEqual(given,expected))return reject(401,'bad_signature');
  let body;
  try{body=JSON.parse(raw.toString('utf8'));}catch{return reject(400,'body_not_json');}
  const hint=project(body);
  if(!hint)return reject(400,'hint_fields_invalid');
  if(header!==hint.hint_id)return reject(401,'hint_id_header_mismatch');
  return {hint,signedAt};
}
