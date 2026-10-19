import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {executeOperation} from '../operations.mjs';

const policy={enabled:true,operations_enabled:['chat_info','get_reactions','send_reaction','forward_message',
  'send_file','edit_message','delete_message','list_contacts','create_group','add_participant','mark_read'],
  discover_chats:true,include_chats:[],exclude_chats:[]};
const job=(operation,parameters={})=>({operation,parameters,provider_chat:'123@c.us',
  provider_message_id:'true_123@c.us_m1',expires_at:Date.now()/1000+300});
const message=(changes={})=>({id:{_serialized:'true_123@c.us_m1',remote:'123@c.us'},fromMe:true,
  body:'original',type:'chat',...changes});

test('disabled, expired, bad namespace, unknown operations and extra params reject before calls',async()=>{
  let calls=0;const client={getChatById:async()=>{calls++;}};
  for(const [j,p] of [[job('chat_info'),{...policy,enabled:false}],
    [job('chat_info'),{...policy,operations_enabled:[]}],
    [{...job('chat_info'),expires_at:0},policy],
    [{...job('chat_info'),provider_chat:'status@broadcast'},policy],
    [job('raw_rpc'),policy],[job('chat_info',{raw:true}),policy],
    [job('chat_info'),{...policy,exclude_chats:['123@c.us']}]])
    await assert.rejects(executeOperation(client,j,()=>p));
  assert.equal(calls,0);
});

test('chat info returns allowlisted typed data, never private objects',async()=>{
  const client={getChatById:async()=>({id:{_serialized:'123@c.us'},name:'Fixture',isGroup:false,
    unreadCount:4,rawData:{token:'secret'},mediaKey:'secret',isMuted:false,pinned:true})};
  const r=await executeOperation(client,job('chat_info'),()=>policy);
  assert.equal(r.outcome,'confirmed');assert.equal(r.data.name,'Fixture');
  assert.equal(r.data.unread_count,4);assert(!JSON.stringify(r).includes('secret'));
});

test('wrong message namespace or returned chat never invokes mutations',async()=>{
  let calls=0;const client={getMessageById:async()=>message({id:{_serialized:'true_999@c.us_m1',remote:'999@c.us'}}),
    sendReaction:async()=>{calls++;}};
  await assert.rejects(executeOperation(client,job('send_reaction',{emoji:'👍'}),()=>policy),/operation_target_invalid/);
  await assert.rejects(executeOperation(client,{...job('send_reaction',{emoji:'👍'}),
    provider_message_id:'true_999@c.us_m1'},()=>policy),/operation_target_invalid/);
  assert.equal(calls,0);
});

test('void reaction/forward and read mutations are submitted, exceptions uncertain',async()=>{
  const calls=[];const m=message({forward:async(...args)=>calls.push(['forward',...args])});
  const client={getMessageById:async()=>m,sendReaction:async(...args)=>calls.push(['react',...args]),
    sendSeen:async()=>true};
  const reaction=await executeOperation(client,job('send_reaction',{emoji:''}),()=>policy);
  assert.equal(reaction.outcome,'submitted');
  const fwd=await executeOperation(client,{...job('forward_message'),
    source_provider_chat:'123@c.us',source_provider_message:'true_123@c.us_m1'},()=>policy);
  assert.equal(fwd.outcome,'submitted');
  assert.equal((await executeOperation(client,job('mark_read'),()=>policy)).outcome,'submitted');
  client.sendReaction=async()=>{throw new Error('private provider credentials');};
  const failed=await executeOperation(client,job('send_reaction',{emoji:'👍'}),()=>policy);
  assert.deepEqual(failed,{outcome:'unknown',data:{code:'provider_operation_uncertain'}});
});

test('fresh policy and expiration are checked again after target lookup',async()=>{
  let current=policy,calls=0;
  const client={getMessageById:async()=>{current={...policy,operations_enabled:[]};return message();},
    sendReaction:async()=>{calls++;}};
  await assert.rejects(executeOperation(client,job('send_reaction',{emoji:'👍'}),()=>current),/operation_disabled/);
  const j=job('send_reaction',{emoji:'👍'});current=policy;
  client.getMessageById=async()=>{j.expires_at=0;return message();};
  await assert.rejects(executeOperation(client,j,()=>current),/operation_expired/);
  assert.equal(calls,0);
});

test('global delete rejects rather than invoking unsafe public fallback',async()=>{
  let calls=0;const client={getMessageById:async()=>{calls++;return message({delete:async()=>{calls++;}});}};
  await assert.rejects(executeOperation(client,job('delete_message',{revoke:true}),()=>policy),/operation_unsupported/);
  assert.equal(calls,0);
});

test('reaction reads preserve unknown and bounded observed actors without raw payload',async()=>{
  const client={getMessageById:async()=>message({getReactions:async()=>undefined})};
  assert.equal((await executeOperation(client,job('get_reactions'),()=>policy)).data.state,'unknown');
  client.getMessageById=async()=>message({getReactions:async()=>[{id:'👍',hasReactionByMe:true,
    senders:Array.from({length:120},(_,i)=>({senderId:`${i+1}@c.us`,timestamp:123,rawData:{token:'secret'}}))}]});
  const r=await executeOperation(client,job('get_reactions',{limit:10,offset:0}),()=>policy);
  assert.equal(r.data.items.length,10);assert.equal(r.data.has_more,true);
  assert.equal(r.data.items[0].actor_id,'1@c.us');assert(!JSON.stringify(r).includes('secret'));
});

test('a group reaction by self does not identify every actor as self',async()=>{
  const client={info:{wid:{_serialized:'123@c.us'}},getMessageById:async()=>message({getReactions:async()=>[
    {id:'👍',hasReactionByMe:true,senders:[{senderId:'123@c.us',timestamp:1},{senderId:'456@c.us',timestamp:2}]}]})};
  const r=await executeOperation(client,job('get_reactions'),()=>policy);
  assert.equal(r.data.items[0].is_by_self,true);assert.equal(r.data.items[1].is_by_self,false);
});

test('participant administration works for a group whose private member chats are excluded',async()=>{
  const group='123@g.us',calls=[];
  const p={...policy,discover_chats:false,include_chats:[group],exclude_chats:['456@c.us']};
  const client={getChatById:async()=>({id:{_serialized:group},isGroup:true,
    addParticipants:async(...args)=>{calls.push(args);return {'456@c.us':{code:200}};}})};
  const r=await executeOperation(client,{...job('add_participant',{user_id:'456@c.us'}),provider_chat:group},()=>p);
  assert.equal(r.outcome,'submitted');assert.deepEqual(calls,[[['456@c.us'],{autoSendInviteV4:false}]]);
});

test('creating a group disables private invitations and does not expose unauthorized new group IDs',async()=>{
  const calls=[],created='999@g.us';
  const client={createGroup:async(...args)=>{calls.push(args);return {gid:{_serialized:created},private:'secret'};}};
  const p={...policy,discover_chats:false,include_chats:['123@g.us']};
  const r=await executeOperation(client,{...job('create_group',{title:'Fixture',user_ids:['456@c.us']}),
    provider_chat:''},()=>p);
  assert.equal(r.outcome,'submitted');assert.deepEqual(calls,[['Fixture',['456@c.us'],{autoSendInviteV4:false}]]);
  assert(!JSON.stringify(r).includes(created));assert(!JSON.stringify(r).includes('secret'));
});

test('missing native mutation method is rejected pre-call rather than claimed submitted',async()=>{
  const client={getMessageById:async()=>message()};
  await assert.rejects(executeOperation(client,job('send_reaction',{emoji:'👍'}),()=>policy),/operation_unsupported/);
});

test('message acceptance requires an ID matching the returned destination namespace',async()=>{
  class Poll {constructor(...args){this.args=args;}}
  const client={sendMessage:async()=>message({id:{_serialized:'true_999@c.us_m1',remote:'123@c.us'}})};
  const p={...policy,operations_enabled:[...policy.operations_enabled,'send_poll']};
  const r=await executeOperation(client,job('send_poll',{question:'Q?',options:['A','B'],multiple:false}),
    ()=>p,{library:{Poll}});
  assert.equal(r.outcome,'unknown');assert.equal(r.data.code,'operation_receipt_unverified');
});

test('forward source scope is rechecked after lookup and before the mutation',async()=>{
  let current=policy,calls=0;const source='456@c.us',sourceID='false_456@c.us_m2';
  const client={getMessageById:async()=>{current={...policy,exclude_chats:[source]};
    return message({id:{_serialized:sourceID,remote:source},fromMe:false,from:source,
      forward:async()=>{calls++;}});}};
  await assert.rejects(executeOperation(client,{...job('forward_message'),source_provider_chat:source,
    source_provider_message:sourceID},()=>current),/operation_target_invalid/);
  assert.equal(calls,0);
});

test('both profile fields validate before any mutation and partial second failure remains unknown',async()=>{
  const p={...policy,operations_enabled:[...policy.operations_enabled,'update_profile']};
  const calls=[];const client={setDisplayName:async name=>{calls.push(name);return true;},
    setStatus:async()=>{throw new Error('private diagnostics');}};
  const r=await executeOperation(client,{...job('update_profile',{name:'Fixture',status:'status'}),provider_chat:''},()=>p);
  assert.equal(r.outcome,'unknown');assert.deepEqual(calls,['Fixture']);
  await assert.rejects(executeOperation(client,{...job('update_profile',{name:'Fixture',status:'x'.repeat(140)}),
    provider_chat:''},()=>p));assert.equal(calls.length,1);
});

test('addressbook contact creation uses the exact vetted phone, disables phone sync and remains submitted',async()=>{
  const calls=[],p={...policy,operations_enabled:[...policy.operations_enabled,'add_contact']};
  const client={saveOrEditAddressbookContact:async(...args)=>{calls.push(args);}};
  const j={...job('add_contact',{phone:'+123456789',first_name:'Fixture',last_name:'Contact'}),provider_chat:''};
  const r=await executeOperation(client,j,()=>p);
  assert.deepEqual(r,{outcome:'submitted',data:{}});
  assert.deepEqual(calls,[['123456789','Fixture','Contact',false]]);
  for(const bad of [{phone:'not-a-number',first_name:'Fixture'},
    {phone:'+123456789',first_name:'x'.repeat(65)},
    {phone:'+123456789',first_name:'Fixture',syncToAddressbook:true}])
    await assert.rejects(executeOperation(client,{...j,parameters:bad},()=>p));
  await assert.rejects(executeOperation(client,j,()=>({...p,exclude_chats:['123456789@c.us']})),/operation_target_invalid/);
  assert.equal(calls.length,1);
});

test('contact listing filters scope before pagination and projects a bounded page',async()=>{
  const client={getContacts:async()=>[{id:{_serialized:'123@c.us'},name:'Visible'},
    {id:{_serialized:'999@c.us'},name:'Forbidden'}]};
  const p={...policy,discover_chats:false,include_chats:['123@c.us']};
  const r=await executeOperation(client,job('list_contacts',{limit:100}),()=>p);
  assert.equal(r.data.items.length,1);assert.equal(r.data.items[0].name,'Visible');
  assert(!JSON.stringify(r).includes('Forbidden'));
});

test('live reads recheck operation access and current contact scope after provider lookup',async()=>{
  let current=policy;
  const client={getChatById:async()=>{current={...policy,operations_enabled:[]};
    return {id:{_serialized:'123@c.us'},name:'Private'};},
    getContacts:async()=>{current={...policy,exclude_chats:['999@c.us']};
      return [{id:{_serialized:'999@c.us'},name:'Newly forbidden'}];}};
  await assert.rejects(executeOperation(client,job('chat_info'),()=>current),/operation_disabled/);
  current=policy;
  const r=await executeOperation(client,{...job('list_contacts'),provider_chat:''},()=>current);
  assert.equal(r.data.items.length,0);assert(!JSON.stringify(r).includes('Newly forbidden'));
});

test('edit only own exact target confirms exact returned message, else submitted',async()=>{
  const client={getMessageById:async()=>message({edit:async(text)=>message({body:text})})};
  const r=await executeOperation(client,job('edit_message',{text:'new'}),()=>policy);
  assert.equal(r.outcome,'confirmed');assert.equal(r.provider_message_id,'true_123@c.us_m1');
  client.getMessageById=async()=>message({fromMe:false,edit:async()=>{throw new Error('must not call');}});
  await assert.rejects(executeOperation(client,job('edit_message',{text:'new'}),()=>policy),/operation_target_invalid/);
});

test('group admin change validates group namespace and IDs before lookup',async()=>{
  let calls=0;const client={getChatById:async()=>{calls++;}};
  await assert.rejects(executeOperation(client,job('add_participant',{user_id:'123@c.us'}),()=>policy));
  assert.equal(calls,0);
});

test('file send rechecks registered CAS hash/bytes and preserves no-seen send options',async()=>{
  const dir=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'anymsg-operation-')));
  try{
    const bytes=Buffer.from('Synthetic file');const digest=createHash('sha256').update(bytes).digest('hex');
    const filename=path.join(dir,digest);fs.writeFileSync(filename,bytes);
    const j={...job('send_file',{mode:'document',caption:'Caption'}),registered_file_path:filename,
      trusted_blob_directory:dir,file_digest:digest,file_bytes:bytes.length,file_mime:'text/plain',file_name:'fixture.txt'};
    const calls=[];class MessageMedia{constructor(...args){this.args=args;}}
    const client={sendMessage:async(...args)=>{calls.push(args);return message();}};
    const r=await executeOperation(client,j,()=>policy,{library:{MessageMedia}});
    assert.equal(r.outcome,'confirmed');assert.equal(calls[0][2].sendSeen,false);
    assert.equal(calls[0][2].sendMediaAsDocument,true);assert.equal(calls[0][2].caption,'Caption');
    await executeOperation(client,{...j,file_name:null,file_mime:null},()=>policy,{library:{MessageMedia}});
    assert.equal(calls[1][1].args[0],'application/octet-stream');
    assert.equal(calls[1][1].args[2],`attachment-${digest.slice(0,12)}`);
    fs.writeFileSync(filename,Buffer.from('corrupted file'));
    await assert.rejects(executeOperation(client,j,()=>policy,{library:{MessageMedia}}),/registered_file_invalid/);
    assert.equal(calls.length,2);
  }finally{fs.rmSync(dir,{recursive:true,force:true});}
});

test('symlink CAS, oversized file and caller path/URL are rejected without reading or sending',async()=>{
  const dir=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'anymsg-operation-')));
  try{
    const digest='a'.repeat(64);fs.symlinkSync('/etc/passwd',path.join(dir,digest));
    const j={...job('send_file',{mode:'document'}),registered_file_path:path.join(dir,digest),
      trusted_blob_directory:dir,file_digest:digest,file_bytes:1,file_mime:'text/plain',file_name:'file.txt'};
    await assert.rejects(executeOperation({},j,()=>policy),/registered_file_invalid/);
    await assert.rejects(executeOperation({}, {...j,file_bytes:32*1024*1024+1},()=>policy));
    await assert.rejects(executeOperation({}, job('send_file',{mode:'document',path:'/etc/passwd'}),()=>policy));
  }finally{fs.rmSync(dir,{recursive:true,force:true});}
});

// spec: ANYM-430
test('open_chat_by_phone is native-only: the web fallback never resolves a number',async()=>{
  let calls=0;const probe=async()=>{calls++;return {_serialized:'12025550123@c.us'};};
  const client={getNumberId:probe,isRegisteredUser:probe,getContactById:probe,getChatById:probe,
    pupPage:{evaluate:probe}};
  const opPolicy={...policy,operations_enabled:[...policy.operations_enabled,'open_chat_by_phone']};
  for(const parameters of [{phone:'12025550123'},{phone:'12025550123',create:true}])
    await assert.rejects(executeOperation(client,{...job('open_chat_by_phone',parameters),provider_chat:''},()=>opPolicy),
      /operation_unsupported/);
  assert.equal(calls,0);
});
