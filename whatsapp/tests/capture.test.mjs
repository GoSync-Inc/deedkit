import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {DiskCapture,event,asId,allows,ownerAttention} from '../capture.mjs';

test('cached service and verified self-chat flags require no contact fetch',()=>{
  const msg={id:{fromMe:false,remote:'42@c.us',id:'N'},from:'42@c.us',fromMe:false,
    body:'Disappearing messages enabled',timestamp:1800000000,type:'notification',hasMedia:false};
  const e=event(msg,undefined,['42@c.us']);
  assert.equal(e.isService,true);assert.equal(e.isSelfChat,true);
  assert.equal(e.messageType,'notification');
  msg.id.remote='99@lid';msg.from='99@lid';
  assert.equal(event(msg,undefined,['42@c.us']).isSelfChat,null);
});

test('durable captures are hashed, private, ordered and retained after reopen',()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'wa-spool-'));
  try {
    const spool=new DiskCapture(dir,{reserveBytes:0});
    const first=spool.put({test:'first'}),second=spool.put({test:'second'});
    assert.ok(first<second);
    for(const name of [first,second]){
      const payload=fs.readFileSync(path.join(dir,name));
      assert.equal(name.slice(21,-5),createHash('sha256').update(payload).digest('hex'));
      assert.equal(fs.statSync(path.join(dir,name)).mode&0o777,0o600);
    }
    const reopened=new DiskCapture(dir,{reserveBytes:0,maxEntries:2});
    assert.throws(()=>reopened.put({third:true}),/spool_full/);
    assert.equal(fs.readdirSync(dir).length,2);
  }finally{fs.rmSync(dir,{recursive:true});}
});
test('byte quota and symlink cannot silently drop events',()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'wa-spool-'));
  try{
    const spool=new DiskCapture(dir,{reserveBytes:0,maxBytes:2});
    assert.throws(()=>spool.put({test:1}),/spool_full/);
    fs.symlinkSync('/dev/null',path.join(dir,'unexpected'));
    assert.throws(()=>spool.put({}),/spool_symlink/);
  }finally{fs.rmSync(dir,{recursive:true});}
});
test('stable WhatsApp ID reconstruction preserves account chat participant namespace',()=>{
  assert.equal(asId({fromMe:false,remote:'123@g.us',id:'ABC',participant:'42@lid'}),'false_123@g.us_ABC_42@lid');
  assert.throws(()=>asId({id:'ABC'}),/provider_id_unavailable/);
  const msg={id:{fromMe:true,remote:'123@c.us',id:'ABC'},fromMe:true,from:'42@c.us',to:'123@c.us',
    timestamp:1800000000,body:'hello',hasMedia:true,_data:{size:100,mediaKey:'secret',directPath:'/secret',filename:'doc.pdf'}};
  const e=event(msg);
  assert.equal(e.chatId,'123@c.us');assert.equal(e.filesize,100);
  assert.ok(!JSON.stringify(e).includes('secret'));
  assert.equal(allows({discover_chats:true,exclude_chats:['123@c.us'],include_chats:[]},e.chatId),false);
});

test('capture policy expires independently from long-running media RPC',async()=>{
  const {readPolicy}=await import('../capture.mjs');
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'wa-policy-'));
  const file=path.join(dir,'.policy');
  try {
    const policy={discover_chats:true,include_chats:[],exclude_chats:['123@c.us']};
    fs.writeFileSync(file,JSON.stringify({expires_at:50,policy}),{mode:0o600});
    assert.deepEqual(readPolicy(dir,40000),policy);
    assert.throws(()=>readPolicy(dir,51000),/policy_expired/);
    fs.chmodSync(file,0o644);assert.throws(()=>readPolicy(dir,40000),/invalid_policy/);
  }finally{fs.rmSync(dir,{recursive:true});}
});

test('voice-note codec parameters normalize consistently for captures and media refresh',()=>{
  const message={id:{fromMe:false,remote:'123@c.us',id:'VOICE'},fromMe:false,from:'123@c.us',
    timestamp:1800000000,body:'',hasMedia:true,_data:{size:10,mimetype:'Audio/Ogg; codecs=opus'}};
  assert.equal(event(message).mimetype,'audio/ogg');
  // Changed assertion: this input used to throw invalid_provider_mime, which
  // stopped the provider. Untrusted control characters now normalize to the
  // generic type and never reach the archive.
  message._data.mimetype='audio/ogg; codecs=opus\r\nInjected: header';
  assert.equal(event(message).mimetype,'application/octet-stream');
  assert.ok(!JSON.stringify(event(message)).includes('Injected'));
});

test('sender-controlled fields are bounded to the archive envelope contract',()=>{
  const base=()=>({id:{fromMe:false,remote:'123@c.us',id:'BOUNDS'},fromMe:false,from:'123@c.us',
    timestamp:1800000000,body:'ok',hasMedia:true,type:'document',_data:{size:10,mimetype:'application/pdf',filename:'a.pdf'}});
  const codePoints=value=>[...value].length;
  let message=base();
  message.body='\u{1F30D}'.repeat(70000);
  message._data.notifyName='\u0000'+'\u{1F600}'.repeat(600)+'\ud800';
  message._data.filename='я'.repeat(300)+'\u0000/\n.pdf';
  message._data.quotedStanzaID='A'.repeat(257);message._data.quotedParticipant='123@c.us';
  message._data.size=8*1024**3+1;
  let e=event(message,undefined,['999@c.us']);
  assert.equal(codePoints(e.body),65536);assert.ok(e.body.isWellFormed());
  assert.equal(e.formatting.state,'unknown');assert.equal(e.formatting.wire_sha256,null);
  assert.equal(codePoints(e.senderName),512);assert.ok(e.senderName.isWellFormed() && !e.senderName.includes('\0'));
  assert.equal(codePoints(e.filename),255);assert.ok(e.filename.endsWith('.pdf') && !/[\x00-\x1f\x7f/\\]/.test(e.filename));
  assert.equal(e.replyToId,null);assert.equal(e.replyToOwner,null);assert.equal(e.filesize,null);
  message=base();message.body='a\u0000b';
  e=event(message);
  assert.equal(e.body,'ab');assert.equal(e.formatting.state,'unknown');
  for(const [mime,want] of [['Image/JPEG; q=1','image/jpeg'],['text/plain\u0000','application/octet-stream'],
      ['изображение/jpeg','application/octet-stream'],['a/'+'b'.repeat(200),'application/octet-stream']]) {
    message=base();message._data.mimetype=mime;
    assert.equal(event(message).mimetype,want);
  }
  message=base();message._data.size=8*1024**3;message._data.latestEditSenderTimestampMs=1799999999999;
  e=event(message);
  assert.equal(e.filesize,8*1024**3);assert.equal(e.editedTimestampMs,null);
  assert.equal(e.formatting.state,'wire_observed');
  message=base();message.timestamp=253402300800;
  assert.throws(()=>event(message),/invalid_provider_time/);
  message=base();message.id.id='X'.repeat(300);
  assert.throws(()=>event(message),/invalid_provider_message/);
});

test('sender lookup uses only requested cached identities and does not fetch a contact book',async()=>{
  const {cachedSenderNames}=await import('../sender.mjs');
  const lookups=[];
  const values={'42@lid':{name:'Alice'},'43@c.us':{pushname:'Bob'},'group@g.us':{name:'Private group'}};
  globalThis.window={require(name){
    if(name==='WAWebCollections')return {Contact:{get(id){lookups.push(id);return values[id];}}};
    if(name==='WAWebContactGetters')return {getName:c=>c.name,getPushname:c=>c.pushname};
    throw new Error('unexpected module');
  }};
  try {
    assert.deepEqual(cachedSenderNames(['42@lid','43@c.us','unknown@lid','42@lid','group@g.us']),
      {'42@lid':'Alice','43@c.us':'Bob'});
    assert.deepEqual(lookups,['42@lid','43@c.us','unknown@lid']);
    window.require=()=>{throw new Error('provider cache unavailable');};
    assert.deepEqual(cachedSenderNames(['42@lid']),{});
  }finally{delete globalThis.window;}
});

test('group attention matches owner phone and LID separately without names or remote lookups',()=>{
  const own=['42@c.us','99@lid'];
  assert.deepEqual(ownerAttention({mentionedIds:['42:7@c.us'],_data:{}},own),
    {mentionsOwner:true,replyToOwner:null});
  assert.deepEqual(ownerAttention({mentionedIds:['99@lid'],_data:{quotedStanzaID:'ABC',quotedParticipant:'42@c.us'}},own),
    {mentionsOwner:true,replyToOwner:true});
  assert.deepEqual(ownerAttention({mentionedIds:['42@lid'],_data:{quotedStanzaID:'ABC',quotedParticipant:'123@c.us'}},own),
    {mentionsOwner:false,replyToOwner:false});
  assert.equal(ownerAttention({mentionedIds:['99@lid']},['42@c.us']).mentionsOwner,null);
  assert.equal(ownerAttention({mentionedIds:[]},own).mentionsOwner,false);
  assert.equal(ownerAttention({_data:{body:'@42'}},own).mentionsOwner,null);
});

test('owner cache lookup reads only authenticated account identities',async()=>{
  const {cachedOwnerIds}=await import('../sender.mjs');
  const calls=[];
  globalThis.window={require(name){calls.push(name);return {
    getMaybeMePnUser:()=>({_serialized:'42@c.us'}),getMaybeMeLidUser:()=>({_serialized:'99@lid'})};}};
  try {
    assert.deepEqual(cachedOwnerIds(),['42@c.us','99@lid']);
    assert.deepEqual(calls,['WAWebUserPrefsMeUser']);
    window.require=()=>{throw new Error('missing optional cache');};
    assert.deepEqual(cachedOwnerIds(),[]);
  }finally{delete globalThis.window;}
});

test('a forwarded message states an unknown origin with its cached score only when capture is enabled',()=>{
  const msg={id:{fromMe:false,remote:'42@c.us',id:'F'},from:'42@c.us',fromMe:false,body:'forwarded words',
    timestamp:1800000000,type:'chat',hasMedia:false,isForwarded:true,forwardingScore:4};
  const saved=process.env.ANYMSG_CAPTURE_FORWARD_ORIGIN;
  try {
    delete process.env.ANYMSG_CAPTURE_FORWARD_ORIGIN;
    assert.equal('forwardOrigin' in event(msg),false);
    process.env.ANYMSG_CAPTURE_FORWARD_ORIGIN='1';
    assert.deepEqual(event(msg).forwardOrigin,{kind:'unknown',basis:'whatsapp_web_js_message',forwarding_score:4});
    assert.deepEqual(event({...msg,forwardingScore:undefined}).forwardOrigin,{kind:'unknown',basis:'whatsapp_web_js_message'});
    assert.equal('forwardOrigin' in event({...msg,isForwarded:false}),false);
  } finally {
    if(saved===undefined)delete process.env.ANYMSG_CAPTURE_FORWARD_ORIGIN;else process.env.ANYMSG_CAPTURE_FORWARD_ORIGIN=saved;
  }
});
