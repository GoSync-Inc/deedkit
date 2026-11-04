import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {mkdtempSync,readFileSync,rmSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

// Test-only compiler, never a dependency of the package: the exact release is fetched with npm pack and its
// tarball hash is checked against this pin before anything of it runs.
const TYPESCRIPT='typescript@5.9.3';
const INTEGRITY='sha512-jl1vZzPDinLr9eUt3J/t7V6FgNEw9QjvBPdysz9KfQDD41fQrC2Y4vKQdiaUpFT4bXlb1RHhLpp8wtm6M5TgSw==';
const library=fileURLToPath(new URL('../lib/index.js',import.meta.url)).replaceAll('\\','/');

function compiler(directory){
  const name=JSON.parse(execFileSync('npm',['pack',TYPESCRIPT,'--pack-destination',directory,'--json','--ignore-scripts'],
    {encoding:'utf8',cwd:directory}))[0].filename;
  const archive=join(directory,name);
  assert.equal('sha512-'+createHash('sha512').update(readFileSync(archive)).digest('base64'),INTEGRITY);
  execFileSync('tar',['-xzf',archive,'-C',directory]);
  return join(directory,'package','bin','tsc');
}

const consumer=`import {AnyMsgClient} from '${library}';
const client=new AnyMsgClient({baseUrl:'https://archive.example',token:'synthetic-token'});
const send={account:'synthetic',chat_id:'chat_synthetic',text:'Confirmed',request_id:'synthetic-send-0001'};
// A guarded chat_id send, and the two ways to read a send's state.
client.sendMessage({...send,expected_last_message_id:'msg_synthetic'});
client.sendMessage({...send,expected_last_message_id:'msg_synthetic',guard:'outgoing_only'});
client.sendMessage({...send,guard:'any_message'});
// ANYM-748: the opt-out of the unresolved-duplicate refusal.
client.sendMessage({...send,allow_duplicate:true});
client.getSendStatus({send_id:'send_synthetic'});
client.getSendStatus({request_id:'synthetic-send-0001'});
// @ts-expect-error guard names one of two values
client.sendMessage({...send,expected_last_message_id:'msg_synthetic',guard:'newest'});
// @ts-expect-error the expected ID is a string
client.sendMessage({...send,expected_last_message_id:7});
// @ts-expect-error allow_duplicate is a boolean
client.sendMessage({...send,allow_duplicate:'yes'});
// @ts-expect-error exactly one of send_id and request_id
client.getSendStatus({send_id:'send_synthetic',request_id:'synthetic-send-0001'});
// @ts-expect-error one of them is required
client.getSendStatus({});
`;

// spec: ANYM-736
test('a TypeScript consumer compiles guarded sends and status reads, and the wrong ones fail',t=>{
  const directory=mkdtempSync(join(tmpdir(),'anymsg-types-'));
  try{
    let tsc;
    try{tsc=compiler(directory);}
    catch(error){
      // CI (GitHub Actions sets CI) must fetch the compiler; a machine without registry access skips, visibly.
      if(process.env.CI||error instanceof assert.AssertionError)throw error;
      return t.skip('no npm registry access: the TypeScript consumer check ran only in CI');
    }
    writeFileSync(join(directory,'consumer.ts'),consumer);
    execFileSync(process.execPath,[tsc,'--noEmit','--strict','--target','es2022','--module','esnext',
      '--moduleResolution','bundler','--lib','es2023,dom','consumer.ts'],
      {cwd:directory,encoding:'utf8',stdio:'pipe'});
  }finally{rmSync(directory,{recursive:true,force:true});}
});
