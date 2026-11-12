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
