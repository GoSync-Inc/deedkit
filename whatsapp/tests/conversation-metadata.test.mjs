import test from 'node:test';
import assert from 'node:assert/strict';
import {event} from '../capture.mjs';
import {readPage} from '../history.mjs';

function message(type='ptt', duration) {
  if(arguments.length<2)duration=12;
  return {id:{fromMe:false,remote:'42@c.us',id:'SYNTHETIC'},from:'42@c.us',
    fromMe:false,body:'',timestamp:1800000000,type,hasMedia:true,
    duration,_data:{duration,mediaKey:'secret',directPath:'/secret'}};
}

test('capture retains observed voice duration without provider private fields',()=>{
  const result=event(message());
  assert.equal(result.durationSeconds,12);
  assert.equal(event(message('ptt','12.5')).durationSeconds,12.5);
  assert.equal(result.timestampResolution,'second');
  assert.ok(!JSON.stringify(result).includes('secret'));
  assert.equal(Object.hasOwn(result,'orderId'),false);
});

test('capture does not invent a duration or precise creation time',()=>{
  for(const duration of [undefined,null,true,-1,Infinity,86401]) {
    assert.equal(event(message('ptt',duration)).durationSeconds,null);
  }
  assert.equal(event(message('document',12)).durationSeconds,null);
});

test('capture keeps a round video (ptv) as a video with its duration',()=>{
  const result=event(message('ptv',9));
  assert.equal(result.mediaKind,'video');
  assert.equal(result.durationSeconds,9);
  assert.equal(result.messageType,'ptv');
});

test('provider snapshot ties do not have a verified phone order contract',async()=>{
  // Cached order, lexical IDs and times disagree; the history transport must not
  // smuggle a guessed orderId/seq/verified order into normalized source metadata.
  const models=[{t:100,id:{_serialized:'Z'}},{t:100,id:{_serialized:'A'}}];
  globalThis.window={WWebJS:{getMessageModel:value=>value},require(name){
    if(name==='WAWebCollections')return {Chat:{get:()=>({msgs:{getModelsArray:()=>models}})}};
    throw new Error('unexpected provider read');
  }};
  try {
    const result=await readPage({chatId:'42@c.us',limit:2});
    assert.deepEqual(result.raw.map(x=>x.id._serialized),['A','Z']);
    assert.ok(result.raw.every(x=>!Object.hasOwn(x,'orderId') && !Object.hasOwn(x,'seq')));
  }finally{delete globalThis.window;}
});
