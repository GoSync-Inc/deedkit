import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {contactBlock,event} from '../capture.mjs';

const CARD='BEGIN:VCARD\r\nVERSION:3.0\r\nN:;Deskha;;;\r\nFN:Deskha\r\n'+
  'item1.TEL;type=CELL;type=VOICE;waid=6281200000001:+62 812-0000-0001\r\nEND:VCARD';

function message(changes) {
  return {id:{fromMe:false,remote:'111111@c.us',id:'V1'},from:'111111@c.us',fromMe:false,
    timestamp:1800000000,hasMedia:false,type:'vcard',body:CARD,vCards:[CARD],_data:{vcardFormattedName:'Deskha'},...changes};
}

function withCapture(value,run) {
  const previous=process.env.ANYMSG_CAPTURE_CONTACT_CARDS;
  if(value===undefined)delete process.env.ANYMSG_CAPTURE_CONTACT_CARDS;
  else process.env.ANYMSG_CAPTURE_CONTACT_CARDS=value;
  try{return run();}
  finally{
    if(previous===undefined)delete process.env.ANYMSG_CAPTURE_CONTACT_CARDS;
    else process.env.ANYMSG_CAPTURE_CONTACT_CARDS=previous;
  }
}

// spec: ANYM-529
test('the fallback fills the contact block from cached vCards like the native adapter',()=>{
  const e=withCapture('1',()=>event(message()));
  assert.deepEqual(e.contactCard,{status:'parsed',contacts:[{name:'Deskha',
    phones:[{number:'+6281200000001',normalized:true,type:'cell,voice',whatsapp_id:'6281200000001'}]}]});
  const multi=withCapture('1',()=>event(message({type:'multi_vcard',body:'',vCards:[],_data:{vcardList:[
    {displayName:'Deskha',vcard:'BEGIN:VCARD\nFN:Deskha Synthetic\nTEL;CELL:0812-0000-0001\nEND:VCARD'},
    {displayName:'',vcard:'BEGIN:VCARD\nN:Tester;Ann;;Dr.;\nTEL:+7 (900) 000-00-02\nEND:VCARD'}]}})));
  assert.deepEqual(multi.contactCard,{status:'parsed',contacts:[
    {name:'Deskha',vcard_name:'Deskha Synthetic',phones:[{number:'0812-0000-0001',normalized:false,type:'cell'}]},
    {name:'Dr. Ann Tester',phones:[{number:'+79000000002',normalized:true}]}]});
});

// spec: ANYM-529
test('folded lines, malformed cards and missing vCards',()=>{
  const folded=contactBlock([{displayName:'',vcard:'BEGIN:VCARD\r\nFN:Desk\r\n ha\r\nTEL;waid=62812000\r\n 00001:+62812000\r\n\t00001\r\nEND:VCARD'}]);
  assert.deepEqual(folded,{status:'parsed',contacts:[{name:'Deskha',
    phones:[{number:'+6281200000001',normalized:true,whatsapp_id:'6281200000001'}]}]});
  assert.deepEqual(contactBlock([{displayName:'Deskha',vcard:'garbage'}]),{status:'parse_failed',contacts:[{name:'Deskha',phones:[]}]});
  const missing=withCapture('1',()=>event(message({vCards:undefined})));
  assert.deepEqual(missing.contactCard,{status:'unavailable',contacts:[]});
  assert.equal(missing.body,CARD);
});

// spec: ANYM-529
test('bounds truncate contacts, phones and strings',()=>{
  const many=contactBlock(Array.from({length:25},(_,i)=>({displayName:'C'+i,vcard:'BEGIN:VCARD\nTEL:+6281200000001\nEND:VCARD'})));
  assert.equal(many.status,'truncated');assert.equal(many.contacts.length,20);
  const tels=Array.from({length:12},(_,i)=>`TEL:+62812${String(i).padStart(8,'0')}\n`).join('');
  const phones=contactBlock([{displayName:'Deskha',vcard:'BEGIN:VCARD\n'+tels+'END:VCARD'}]);
  assert.equal(phones.status,'truncated');assert.equal(phones.contacts[0].phones.length,10);
  const long='Я'.repeat(300);
  const named=contactBlock([{displayName:long,vcard:'BEGIN:VCARD\nTEL:+6281200000001\nEND:VCARD'}]);
  assert.equal(named.status,'truncated');assert.equal([...named.contacts[0].name].length,256);
  const tel=`TEL;TYPE=${'t'.repeat(300)}:${'9'.repeat(300)}\n`.repeat(10);
  const huge=contactBlock(Array.from({length:20},()=>({displayName:long,vcard:'BEGIN:VCARD\n'+tel+'END:VCARD'})));
  assert.equal(huge.status,'truncated');
  assert.ok(huge.contacts.length>0 && huge.contacts.length<20);
  assert.ok(Buffer.byteLength(JSON.stringify(huge))<=16*1024);
});

// spec: ANYM-529
// The name predates the default flip (a Tiden case ID); it now checks the 0 kill switch.
test('contact capture is off by default, so collection is unchanged',()=>{
  const e=withCapture('0',()=>event(message()));
  assert.equal('contactCard' in e,false);
  assert.equal('contactCard' in withCapture('1',()=>event(message({type:'chat',body:'hi',vCards:[]}))),false);
});

// spec: ANYM-529
test('contact capture is on without the variable',()=>{
  assert.equal(withCapture(undefined,()=>event(message())).contactCard.status,'parsed');
});

// spec: ANYM-529
test('the fallback parser emits the shared fixture blocks the server test ingests',()=>{
  const fixture=JSON.parse(readFileSync(new URL('../../tests/fixtures/contact-card-parser-outputs.json',import.meta.url),'utf8'));
  for(const c of fixture.cases)assert.deepEqual(contactBlock(c.cards),c.block,c.name);
});
