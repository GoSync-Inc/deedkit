import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import {createServer} from '../src/server.mjs';
import {RECEIVER,clock,downstream,lower,relay,rows,signed,vector,workspace} from './helpers.mjs';

function post(port,raw,headers,receiver=RECEIVER){
  return new Promise((resolve,reject)=>{
    const req=http.request({host:'127.0.0.1',port,method:'POST',path:`/hints/${receiver}`,headers:{...headers,'content-length':raw.length}},res=>{
      const chunks=[];res.on('data',c=>chunks.push(c));res.on('end',()=>resolve({status:res.statusCode,headers:res.headers,body:JSON.parse(Buffer.concat(chunks))}));
    });
    req.on('error',reject);req.end(raw);
  });
}

async function serve(t){
  const space=workspace(),time=clock(),fake=downstream([],{clock:time}),instance=relay(space,{clock:time,transport:fake});
  const server=createServer(instance);
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  t.after(async()=>{await new Promise(resolve=>server.close(resolve));await instance.close();});
  return {space,time,fake,instance,port:server.address().port};
}

// spec: ANYM-228
test('the dispatcher vector is accepted once; a resend with an altered header hint ID is rejected; an unchanged resend fires nothing',async t=>{
  const {fake,instance,port}=await serve(t);
  const raw=Buffer.from(vector.body),headers=lower(vector.headers);
  const first=await post(port,raw,headers);
  assert.equal(first.status,202);assert.equal(first.body.result,'accepted');
  const altered=await post(port,raw,{...headers,'x-deedkit-hint-id':'00000000-0000-4000-8000-000000000000'});
  assert.equal(altered.status,401);assert.equal(altered.body.error,'hint_id_header_mismatch');
  const again=await post(port,raw,headers);
  assert.equal(again.status,200);assert.equal(again.body.result,'duplicate');
  await instance.fireDue();await instance.fireDue();
  assert.equal(fake.fires.length,1);
  assert.equal(fake.fires[0].payload.hint_id,JSON.parse(vector.body).hint_id);
  assert.deepEqual(rows(instance).map(r=>r.state),['accepted']);
});

// spec: ANYM-228
test('a bad signature, a timestamp outside -300 s/+60 s, a body over 64 KiB and a mismatched header hint ID store nothing and fire nothing',async t=>{
  const {time,fake,instance,port}=await serve(t);
  const now=Math.floor(time.now()/1000);
  const forged=signed({},now,{secret:Buffer.from('another synthetic secret that is long enough')});
  assert.deepEqual([(await post(port,forged.raw,forged.headers)).status,(await post(port,forged.raw,forged.headers)).body.error],[401,'bad_signature']);
  const tampered=signed({},now);
  const changed=Buffer.from(tampered.raw.toString().replace('"through":1','"through":9'));
  assert.equal((await post(port,changed,tampered.headers)).body.error,'bad_signature');
  for(const offset of [-301,61]){
    const stale=signed({},now+offset);
    const response=await post(port,stale.raw,stale.headers);
    assert.deepEqual([response.status,response.body.error],[401,'timestamp_outside_window']);
  }
  // The window edges pass verification; a hint signed before the hold window is refused unstored, so the
  // dispatcher retries it with a fresh timestamp.
  const edge=signed({},now-300),future=signed({},now+60);
  assert.deepEqual((await post(port,edge.raw,edge.headers)).body,{error:'hold_elapsed'});
  assert.equal((await post(port,future.raw,future.headers)).status,202);
  const large=signed({padding:'x'.repeat(64*1024)},now);
  assert.equal((await post(port,large.raw,large.headers)).status,413);
  const mismatch=signed({},now);
  const response=await post(port,mismatch.raw,{...mismatch.headers,'x-deedkit-hint-id':signed({},now).hint.hint_id});
  assert.deepEqual([response.status,response.body.error],[401,'hint_id_header_mismatch']);
  const missing=signed({},now);
  delete missing.headers['x-deedkit-signature'];
  assert.equal((await post(port,missing.raw,missing.headers)).status,401);
  const unknown=signed({},now);
  assert.equal((await post(port,unknown.raw,unknown.headers,'not-a-receiver')).status,404);
  assert.equal(rows(instance).length,1);
  await instance.fireDue();
  assert.equal(fake.fires.length,1);
});

// spec: ANYM-228
test('an unauthenticated request target the URL parser rejects gets 404 and the relay keeps serving',async t=>{
  const {port}=await serve(t);
  const raw=await new Promise((resolve,reject)=>{
    const socket=net.connect(port,'127.0.0.1',()=>socket.write('GET // HTTP/1.1\r\nHost: relay\r\nConnection: close\r\n\r\n'));
    const chunks=[];socket.on('data',c=>chunks.push(c));socket.on('end',()=>resolve(Buffer.concat(chunks).toString()));socket.on('error',reject);
  });
  assert.match(raw,/^HTTP\/1\.1 404 /);
  const hint=signed({},Number(vector.headers['X-DeedKit-Timestamp']));
  assert.equal((await post(port,hint.raw,hint.headers)).status,202);
});
