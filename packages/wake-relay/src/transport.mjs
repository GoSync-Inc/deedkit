// Two-phase HTTPS: `connect` (DNS, TCP, TLS) can fail only before any byte of the request exists on the wire;
// `send` writes the request on the established socket. The relay commits `sending` between the two.
import http from 'node:http';
import tls from 'node:tls';

const RESPONSE_LIMIT=64*1024;

export function tlsConnect(url,{timeoutMs}){
  const target=new URL(url);
  if(target.protocol!=='https:')return Promise.reject(new Error('downstream_not_https'));
  return new Promise((resolve,reject)=>{
    const socket=tls.connect({host:target.hostname,port:Number(target.port||443),servername:target.hostname,ALPNProtocols:['http/1.1']});
    const timer=setTimeout(()=>socket.destroy(new Error('connect_timeout')),timeoutMs);
    socket.once('secureConnect',()=>{clearTimeout(timer);socket.removeListener('error',reject);resolve(socket);});
    socket.once('error',error=>{clearTimeout(timer);reject(error);});
  });
}

export function send(socket,request,{timeoutMs}){
  const target=new URL(request.url);
  return new Promise((resolve,reject)=>{
    let done=false;
    const finish=(error,value)=>{if(done)return;done=true;clearTimeout(timer);socket.destroy();error?reject(error):resolve(value);};
    const timer=setTimeout(()=>{req.destroy();finish(new Error('response_timeout'));},timeoutMs);
    const req=http.request({createConnection:()=>socket,method:'POST',path:target.pathname+target.search,
      headers:{...request.headers,host:target.host,'content-length':String(request.body.length),connection:'close'}},response=>{
      const chunks=[];let size=0;
      // An oversized response is an error, never a truncated body whose prefix could read as a success.
      response.on('data',chunk=>{size+=chunk.length;if(size>RESPONSE_LIMIT)return finish(new Error('response_too_large'));chunks.push(chunk);});
      response.on('end',()=>finish(null,{status:response.statusCode,headers:response.headers,body:Buffer.concat(chunks)}));
      response.on('error',error=>finish(error));
    });
    req.on('error',error=>finish(error));
    req.end(request.body);
  });
}

export function httpsTransport({connectSocket=tlsConnect,connectTimeoutMs=10_000,responseTimeoutMs=30_000}={}){
  return {
    connect:url=>connectSocket(url,{timeoutMs:connectTimeoutMs}),
    send:(socket,request)=>send(socket,request,{timeoutMs:responseTimeoutMs}),
    close:socket=>socket.destroy(),
  };
}
