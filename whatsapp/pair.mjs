import {recoverNavigation} from './navigation.mjs';
import {createPairingCoordinator} from './pairing.mjs';
// Owner-terminal-only QR. No HTTP pairing endpoint, file/log QR or collector loop.
import fs from 'node:fs';
import pkg from 'whatsapp-web.js';
import qr from 'qrcode-terminal';
process.umask(0o077);
const SESSION_LOCKS='/run/deedkit/session-locks';
if(!process.stderr.isTTY)process.exit(1);
console.log=console.warn=console.error=()=>{};
const cfg=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
// ANYM-914: the browser fallback is not claimed; it never starts where the canonical session lock directory (or
// the configured one) exists, also when run directly rather than through the Python parent's refusal.
if([SESSION_LOCKS,cfg.session_lock_directory].some(p=>typeof p==='string'&&fs.existsSync(p))){
  process.stderr.write('session_claim_unsupported\n');process.exit(1);
}
const pairing=createPairingCoordinator({expectedWid:cfg.expected_wid,
  createClient:()=>recoverNavigation(new pkg.Client({
    authStrategy:new pkg.LocalAuth({dataPath:cfg.session_directory}),
    authTimeoutMs:60000,qrMaxRetries:3,takeoverOnConflict:false,
    puppeteer:{headless:true,executablePath:cfg.chromium_executable,args:['--disable-dev-shm-usage']},
    webVersionCache:{type:'local',path:cfg.session_directory+'/web-cache'},
    deviceName:'Personal OS archive',browserName:'Chrome'})),
  onQr:value=>qr.generate(value,{small:true},rendered=>process.stderr.write(rendered+'\n'))});
let stopping=false;
async function stop(code){
  if(stopping)return;stopping=true;
  const deadline=setTimeout(()=>process.exit(1),10000);deadline.unref();
  try{await pairing.stop();}catch{code=1;}
  clearTimeout(deadline);
  process.exit(code);
}
for(const sig of ['SIGTERM','SIGINT'])process.on(sig,()=>void stop(1));
process.on('uncaughtException',()=>void stop(1));process.on('unhandledRejection',()=>void stop(1));
const parent=process.ppid;
setInterval(()=>{if(process.ppid!==parent)void stop(1);},2000).unref();
void pairing.run().then(()=>stop(0),error=>stop(error?.message==='whatsapp_identity_mismatch'?2:1));
