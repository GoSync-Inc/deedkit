import assert from 'node:assert/strict';
import test from 'node:test';
import {AnyMsgClient} from '../lib/index.js';

const options={baseUrl:'https://archive.example/prefix',token:'synthetic-token'};

// spec: ANYM-988
test('exportFeedbackMetadata forwards a cursor and a limit and rejects a bad one before transport',async()=>{
  const seen=[];
  const client=new AnyMsgClient({...options,fetch:async(url,request)=>{
    seen.push([String(url),JSON.parse(request.body)]);
    return Response.json({untrusted_content:true,items:[],has_more:false,next_cursor:'next'});
  }});
  assert.equal((await client.exportFeedbackMetadata()).next_cursor,'next');
  await client.exportFeedbackMetadata({cursor:'synthetic-cursor',limit:100});
  for(const args of [{cursor:''},{cursor:'x'.repeat(513)},{cursor:7},{cursor:null},{limit:0},{limit:101},{status:'fixed'},{extra:1}])
    assert.throws(()=>client.exportFeedbackMetadata(args),TypeError,JSON.stringify(args));
  assert.deepEqual(seen,[
    ['https://archive.example/prefix/v1/export_feedback_metadata',{}],
    ['https://archive.example/prefix/v1/export_feedback_metadata',{cursor:'synthetic-cursor',limit:100}]]);
});

// spec: ANYM-992
test('setFeedbackTriage sends the guarded write with a null change time and rejects a bad one before transport',async()=>{
  const seen=[];
  const client=new AnyMsgClient({...options,fetch:async(url,request)=>{
    seen.push([String(url),JSON.parse(request.body)]);
    return Response.json({status:'accepted',replayed:false});
  }});
  const good={feedback_id:'11111111-1111-4111-8111-111111111111',status:'accepted',expected_status:'received',
    expected_changed_at:null,request_id:'synthetic-triage-1',spec_ref:'ANYM-992'};
  assert.equal((await client.setFeedbackTriage(good)).status,'accepted');
  await client.setFeedbackTriage({...good,status:'duplicate',spec_ref:null,duplicate_of:'22222222-2222-4222-8222-222222222222',
    expected_changed_at:'2026-11-12T09:00:00.123456+00:00',note:'Same as another report'});
  for(const args of [{...good,status:'fixed'},{...good,status:'verified'},{...good,expected_status:'closed'},
    {...good,feedback_id:'not-a-uuid'},{...good,spec_ref:'card'},{...good,note:''},{...good,note:'x'.repeat(281)},
    {...good,request_id:'short'},{...good,expected_changed_at:7},{...good,extra:1},
    Object.fromEntries(Object.entries(good).filter(([name])=>name!=='expected_changed_at'))])
    assert.throws(()=>client.setFeedbackTriage(args),TypeError,JSON.stringify(args));
  assert.deepEqual(seen.map(([url])=>url),Array(2).fill('https://archive.example/prefix/v1/set_feedback_triage'));
  assert.equal(seen[0][1].expected_changed_at,null);
});
