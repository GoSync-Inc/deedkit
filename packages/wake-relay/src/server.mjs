// HTTP front of the relay: POST /hints/<receiver-id>. TLS terminates in front (Caddy or a tunnel) on public 443.
import http from 'node:http';
import {MAX_BODY} from './hint.mjs';

function reply(res,{status,headers={},body}){
  res.writeHead(status,{'content-type':'application/json',...headers});
  res.end(JSON.stringify(body));
}

export function createServer(relay){
  const server=http.createServer((req,res)=>{
    const match=/^\/hints\/([A-Za-z0-9-]{1,64})$/.exec(new URL(req.url,'http://relay').pathname);
    if(req.method!=='POST'||!match){req.resume();return reply(res,{status:404,body:{error:'not_found'}});}
    if(Number(req.headers['content-length']??0)>MAX_BODY){req.resume();return reply(res,{status:413,body:{error:'body_too_large'}});}
    const chunks=[];let size=0,over=false;
    req.on('data',chunk=>{size+=chunk.length;if(size>MAX_BODY)over=true;else chunks.push(chunk);});
    req.on('end',()=>{
      if(over)return reply(res,{status:413,body:{error:'body_too_large'}});
      let result;
      try{result=relay.receive(match[1],Buffer.concat(chunks),req.headers);}
      catch(error){relay.log({event:'receive_error',error:String(error?.code??error?.message).slice(0,64)});return req.socket.destroy();}
      // --drop-ack-once: the row is committed but the dispatcher sees no 2xx and redelivers the same signed hint.
      if(result.drop)return req.socket.destroy();
      reply(res,result);
    });
  });
  server.requestTimeout=10_000;server.headersTimeout=5_000;
  return server;
}
