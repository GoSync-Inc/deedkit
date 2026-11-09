import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const provider=new URL('../provider.mjs',import.meta.url).pathname;

// spec: ANYM-914
test('the browser provider run directly refuses where a session lock directory exists and opens nothing',()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'anym914-'));
  const locks=path.join(root,'session-locks');fs.mkdirSync(locks,{mode:0o700});
  const session=path.join(root,'session');const events=path.join(root,'events');
  const config=path.join(root,'config.json');
  fs.writeFileSync(config,JSON.stringify({session_directory:session,events_directory:events,session_lock_directory:locks}));
  const run=spawnSync(process.execPath,[provider,config],{encoding:'utf8',timeout:20000,stdio:['ignore','pipe','pipe']});
  assert.equal(run.status,1);
  assert.equal(run.stderr,'session_claim_unsupported\n');
  // No session directory, profile or event file was created: the refusal comes before any client or spool.
  assert.deepEqual(fs.readdirSync(root).sort(),['config.json','session-locks']);
  fs.rmSync(root,{recursive:true});
});
