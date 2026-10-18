// One Node writer per directory; Python only consumes atomically published files.
// No provider credentials, keys, direct paths or raw Message objects are persisted.
import fs from 'node:fs';
import path from 'node:path';
import {createHash, randomBytes} from 'node:crypto';

export function asId(id) {
  if (typeof id==='string') return id;
  if (id?._serialized) return id._serialized;
  if (id?.id && id?.remote && typeof id.fromMe==='boolean') {
    const remote=asId(id.remote);
    if (remote) return `${id.fromMe}_${remote}_${id.id}${id.participant?'_'+asId(id.participant):''}`;
  }
  throw new Error('provider_id_unavailable');
}
export function chatId(message) {
  const remote=message.id?.remote || message._data?.id?.remote;
  const value=remote ? asId(remote) : asId(message.fromMe ? message.to : message.from);
  if (!/^[a-zA-Z0-9_.:-]{1,140}@(c\.us|g\.us|lid)$/.test(value)) throw new Error('unsupported_chat');
  return value;
}
function personalId(value) {
  const id=typeof value==='string'?value:value?._serialized;
  const match=typeof id==='string' && /^(\d+)(?::\d+)?@(c\.us|lid)$/.exec(id);
  return match?match[1]+'@'+match[2]:null;
}
export function ownerAttention(message,ownerIds=[]) {
  const own=new Set(ownerIds.map(personalId).filter(Boolean));
  function isOwner(id) {
    const normalized=personalId(id);
    if(!normalized || !own.size)return null;
    if(own.has(normalized))return true;
    return [...own].some(me=>me.split('@')[1]===normalized.split('@')[1])?false:null;
  }
  const raw=message._data || message;
  const mentions=message.mentionedIds ?? raw.mentionedJidList;
  const hits=Array.isArray(mentions)?mentions.map(isOwner):null;
  return {mentionsOwner:hits===null?null:hits.includes(true)?true:hits.includes(null)?null:false,
    replyToOwner:typeof raw.quotedStanzaID==='string'?isOwner(raw.quotedParticipant):null};
}
// Archive envelope bounds (services/messaging envelope.validate). Python counts
// code points, rejects NUL and cannot encode lone surrogates. Sender-controlled
// display fields are sanitized; references are omitted, never shortened.
const MAX_BODY=65536,MAX_NAME=512,MAX_FILENAME=255,MAX_REFERENCE=256,MAX_SENDER=160;
const MAX_MEDIA_BYTES=8*1024**3,MAX_UNIX=253402300799;
export function envelopeText(value,limit) {
  const text=value.toWellFormed();
  if(text===value && text.length<=limit && !text.includes('\0'))return text;
  let result='',count=0;
  for(const char of text) {
    if(count>=limit)break;
    if(char==='\0')continue;
    result+=char;count++;
  }
  return result;
}
function envelopeReference(value,limit=MAX_REFERENCE) {
  return typeof value==='string' && value.length>0 && value.length<=limit && value.isWellFormed() && !value.includes('\0');
}
function envelopeMime(value) {
  if(!value)return null;
  if(typeof value!=='string' || /[\x00-\x1f\x7f]/.test(value))return 'application/octet-stream';
  const mime=value.split(';',1)[0].trim().toLowerCase();
  return mime.length<=127 && /^[a-z0-9_.+-]+\/[a-z0-9_.+-]+$/.test(mime)?mime:'application/octet-stream';
}
function envelopeFilename(value) {
  if(typeof value!=='string' || !value)return null;
  const name=value.toWellFormed().replace(/[\x00-\x1f\x7f/\\]/g,'_');
  if(name.length<=MAX_FILENAME)return name;
  const dot=name.lastIndexOf('.'),ext=dot>0 && [...name.slice(dot)].length<=16?name.slice(dot):'';
  return envelopeText(name.slice(0,name.length-ext.length),MAX_FILENAME-[...ext].length)+ext;
}
export function event(message, body=undefined, ownerIds=[]) {
  const raw=message._data || message;
  const mime=envelopeMime(raw.mimetype);
  const chat=chatId(message);
  const own=new Set(ownerIds.map(personalId).filter(Boolean));
  const peer=personalId(chat);
  const self=peer && own.size ? own.has(peer)?true:
    [...own].some(id=>id.split('@')[1]===peer.split('@')[1])?false:null:null;
  const type=typeof message.type==='string'?message.type:null;
  // Public Message.duration is seconds (number or numeric string depending on
  // provider version). It never supplies finer creation-time resolution.
  const duration=message.duration;
  const numericDuration=typeof duration==='number'?duration:
    typeof duration==='string' && /^\d+(?:\.\d+)?$/.test(duration)?Number(duration):null;
  const durationSeconds=['ptt','audio','video','gif','ptv'].includes(type) &&
    Number.isFinite(numericDuration) && numericDuration>=0 && numericDuration<=86400?numericDuration:null;
  const value={chatId:chat,messageId:asId(message.id),
    messageType:type,
    timestampResolution:'second',durationSeconds,
    isService:['notification','e2e_notification','call_log','gp2','protocol','ciphertext','revoked'].includes(type),
    isSelfChat:self,
    chatName:chatId(message),senderId:message.author || message.from || '',
    senderName:typeof raw.notifyName==='string'?envelopeText(raw.notifyName,MAX_NAME):'',
    body:body===undefined ? message.body || '' : body,timestamp:message.timestamp,
    fromMe:Boolean(message.fromMe),hasMedia:Boolean(message.hasMedia),
    filename:envelopeFilename(raw.filename),mimetype:mime,
    // Only cached metadata: never fetch a quoted message/contact during capture.
    replyToId:envelopeReference(raw.quotedStanzaID)?raw.quotedStanzaID:null,
    ...ownerAttention(message,ownerIds),
    forwarded:typeof message.isForwarded==='boolean'?message.isForwarded:null,
    mediaKind:({image:'photo',video:'video',ptv:'video',audio:'audio',ptt:'voice',document:'document',sticker:'sticker',gif:'animation'})[message.type] || (message.hasMedia?'other':null),
    filesize:Number.isSafeInteger(raw.size) && raw.size>=0 && raw.size<=MAX_MEDIA_BYTES?raw.size:null,
    editedTimestampMs:message.latestEditSenderTimestampMs || raw.latestEditSenderTimestampMs || null};
  if (!Number.isSafeInteger(value.timestamp) || value.timestamp<=0 || value.timestamp>MAX_UNIX)
    throw new Error('invalid_provider_time');
  if (!envelopeReference(value.messageId) || (value.senderId!=='' && !envelopeReference(value.senderId,MAX_SENDER)) ||
      typeof value.body!=='string')
    throw new Error('invalid_provider_message');
  if (value.replyToId===null) value.replyToOwner=null;
  const edited=value.editedTimestampMs;
  // Sender-controlled edit time must not precede the send or exceed archive time.
  if (!Number.isSafeInteger(edited) || edited<value.timestamp*1000 || edited>MAX_UNIX*1000) value.editedTimestampMs=null;
  const wire=value.body;
  value.body=envelopeText(wire,MAX_BODY);
  // Public cached body is native wire evidence; no inferred entity parse or
  // undocumented private formatting fields are treated as reliable metadata.
  // A sanitized body is no longer the exact wire and makes no digest claim.
  const observed=typeof (body===undefined?message.body:body)==='string' && value.body===wire;
  value.formatting={version:1,state:observed?'wire_observed':'unknown',
    basis:observed?'whatsapp_native_wire':null,
    wire_sha256:observed?createHash('sha256').update(value.body,'utf8').digest('hex'):null,
    entities:null,unsupported_entities:[],
    warnings:observed?['syntax_interpretation_unknown']:['native_entities_not_captured']};
  return value;
}
export function cursor(e) { return [e.timestamp,e.messageId]; }
export function compare(a,b) { return a[0]-b[0] || (a[1]<b[1]?-1:a[1]>b[1]?1:0); }
export function allows(policy,chat) {
  return !policy.exclude_chats.includes(chat) && (policy.discover_chats || policy.include_chats.includes(chat));
}
export function readPolicy(directory,now=Date.now()) {
  const fd=fs.openSync(path.join(directory,'.policy'),fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW|fs.constants.O_NONBLOCK);
  try {
    const info=fs.fstatSync(fd);
    if(!info.isFile() || info.size>1024*1024 || (info.mode&0o077))throw new Error('invalid_policy');
    const saved=JSON.parse(fs.readFileSync(fd,'utf8'));
    if(!Number.isFinite(saved.expires_at) || saved.expires_at*1000<now)throw new Error('policy_expired');
    const p=saved.policy;
    if(typeof p?.discover_chats!=='boolean' || !Array.isArray(p.include_chats) || !Array.isArray(p.exclude_chats))
      throw new Error('invalid_policy');
    return p;
  }finally{fs.closeSync(fd);}
}
export class DiskCapture {
  constructor(directory,{maxBytes=256*1024**2,maxEntries=10000,reserveBytes=1024**3}={}) {
    this.directory=directory; this.maxBytes=maxBytes; this.maxEntries=maxEntries; this.reserveBytes=reserveBytes;
    fs.mkdirSync(directory,{recursive:true,mode:0o700});
    const info=fs.lstatSync(directory);
    if (info.isSymbolicLink()) throw new Error('spool_symlink');
    if (!info.isDirectory() || (info.mode&0o077)) throw new Error('private_spool_required');
    this.sequence=0n;
  }
  put(value) {
    const payload=Buffer.from(JSON.stringify(value));
    if (payload.length>512*1024) throw new Error('capture_too_large');
    let bytes=0,entries=0;
    for (const name of fs.readdirSync(this.directory)) {
      try {const info=fs.lstatSync(path.join(this.directory,name));
        if(info.isSymbolicLink()) throw new Error('spool_symlink');
        if(info.isFile()) bytes+=info.size;
        if(name.endsWith('.json')) entries++;
      } catch(e) {if(e.code!=='ENOENT') throw e;}
    }
    if(bytes+payload.length>this.maxBytes || entries>=this.maxEntries) throw new Error('spool_full');
    const disk=fs.statfsSync(this.directory);
    if(disk.bavail*disk.bsize-payload.length<this.reserveBytes) throw new Error('disk_reserve');
    const now=BigInt(Date.now())*1000000n;
    this.sequence=now>this.sequence ? now : this.sequence+1n;
    const digest=createHash('sha256').update(payload).digest('hex');
    const name=`${this.sequence.toString().padStart(20,'0')}-${digest}.json`;
    const tmp=path.join(this.directory,'.pending-'+randomBytes(12).toString('hex'));
    let fd;
    try {
      fd=fs.openSync(tmp,'wx',0o600); fs.writeFileSync(fd,payload); fs.fsyncSync(fd); fs.closeSync(fd);fd=undefined;
      fs.renameSync(tmp,path.join(this.directory,name));
      const dirfd=fs.openSync(this.directory,'r'); try {fs.fsyncSync(dirfd);} finally {fs.closeSync(dirfd);}
    } finally {if(fd!==undefined)fs.closeSync(fd); try{fs.unlinkSync(tmp);}catch(e){if(e.code!=='ENOENT')throw e;}}
    return name;
  }
}
