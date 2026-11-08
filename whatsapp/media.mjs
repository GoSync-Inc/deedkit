// WhatsApp's documented-by-implementation media format (Baileys messages-media):
// HKDF-SHA256(112), AES-256-CBC, HMAC-SHA256(iv+ciphertext)[:10].
// A caller MUST publish the original only after this iterator finishes normally.
import {hkdfSync,createDecipheriv,createHash,createHmac,timingSafeEqual} from 'node:crypto';
import https from 'node:https';

const INFO={image:'Image',sticker:'Image',video:'Video',gif:'Video',ptv:'Video',
  audio:'Audio',ptt:'Audio',document:'Document'};
function bytes32(value) {
  if(typeof value!=='string' || !/^[A-Za-z0-9+/]{43}=$/.test(value)) throw new Error('media_metadata_unavailable');
  const result=Buffer.from(value,'base64');
  if(result.length!==32) throw new Error('media_metadata_unavailable');
  return result;
}
export function keys(metadata) {
  if(!INFO[metadata.type])throw new Error('unsupported_media_type');
  const expanded=Buffer.from(hkdfSync('sha256',bytes32(metadata.mediaKey),Buffer.alloc(32),
    Buffer.from(`WhatsApp ${INFO[metadata.type]} Keys`),112));
  return {iv:expanded.subarray(0,16),cipher:expanded.subarray(16,48),mac:expanded.subarray(48,80)};
}
export function mediaUrl(directPath) {
  if(typeof directPath!=='string' || !/^\/(?:o[12]\/)?v\//.test(directPath) || directPath.length>8192 || /[\\\s#]/.test(directPath))
    throw new Error('invalid_media_path');
  const url=new URL('https://mmg.whatsapp.net'+directPath);
  if(url.hostname!=='mmg.whatsapp.net' || url.port || url.username || url.password)throw new Error('invalid_media_path');
  return url;
}
export async function* decrypt(source,metadata,maxBytes) {
  const key=keys(metadata),expectedPlain=bytes32(metadata.filehash),expectedEnc=bytes32(metadata.encFilehash);
  const aes=createDecipheriv('aes-256-cbc',key.cipher,key.iv);
  const mac=createHmac('sha256',key.mac).update(key.iv),encryptedHash=createHash('sha256'),plainHash=createHash('sha256');
  let tail=Buffer.alloc(0),received=0,total=0;
  for await(const chunk of source) {
    // Upstream HTTPS uses bounded chunks; split injected/test streams too.
    for(let offset=0;offset<chunk.length;offset+=65536) {
      const part=chunk.subarray(offset,offset+65536); received+=part.length;
      if(received>maxBytes+26)throw new Error('media_size_limit');
      encryptedHash.update(part);
      const buffer=Buffer.concat([tail,part]);
      const length=Math.floor(Math.max(0,buffer.length-10)/16)*16;
      const ciphertext=buffer.subarray(0,length); tail=Buffer.from(buffer.subarray(length));
      mac.update(ciphertext);
      const plain=aes.update(ciphertext);total+=plain.length;
      if(total>maxBytes)throw new Error('media_size_limit');
      plainHash.update(plain);if(plain.length)yield plain;
    }
  }
  if(tail.length!==10 || received<26 || !timingSafeEqual(mac.digest().subarray(0,10),tail) ||
      !timingSafeEqual(encryptedHash.digest(),expectedEnc))throw new Error('media_hash_mismatch');
  let final;try{final=aes.final();}catch{throw new Error('media_hash_mismatch');}
  total+=final.length;plainHash.update(final);
  if(total>maxBytes)throw new Error('media_size_limit');
  if(!timingSafeEqual(plainHash.digest(),expectedPlain))throw new Error('media_hash_mismatch');
  if(metadata.size!==null && metadata.size!==undefined && total!==metadata.size)throw new Error('media_size_mismatch');
  if(final.length)yield final;
}
// ANYM-815: 403, 404 and 410 all mean the CDN no longer serves this copy (whatsmeow
// treats them alike); any other non-200 answer stays a network error.
export function statusError(status) {
  if(status===200)return null;
  return status===403 || status===404 || status===410?'media_unavailable':'media_network_error';
}
export async function* download(metadata,maxBytes) {
  // No cookies, auth headers, proxies, arbitrary hosts, redirects or full buffers.
  keys(metadata); bytes32(metadata.filehash);bytes32(metadata.encFilehash);
  if(metadata.size>maxBytes)throw new Error('media_size_limit');
  const url=mediaUrl(metadata.directPath);
  const request=https.get(url,{headers:{Origin:'https://web.whatsapp.com'},timeout:60000});
  let response;
  request.on('timeout',()=>request.destroy(new Error('media_timeout')));
  // Socket timeout alone is an idle limit. A trickling response must not keep
  // this account's background transfer occupied indefinitely.
  const deadline=setTimeout(()=>{
    const error=new Error('media_timeout');
    response?.destroy(error);request.destroy(error);
  },60000);
  try {
    response=await new Promise((resolve,reject)=>{request.once('response',resolve);request.once('error',reject);});
    const failure=statusError(response.statusCode);
    if(failure)throw new Error(failure);
    const length=Number(response.headers['content-length']);
    if(Number.isFinite(length) && length>maxBytes+26)throw new Error('media_size_limit');
    yield* decrypt(response,metadata,maxBytes);
  } catch(error) {
    const safe=new Set(['media_timeout','media_network_error','media_unavailable','media_size_limit',
      'media_hash_mismatch','media_size_mismatch','media_metadata_unavailable','invalid_media_path','unsupported_media_type']);
    throw new Error(safe.has(error.message)?error.message:'media_network_error');
  } finally {clearTimeout(deadline);response?.destroy();request.destroy();}
}
