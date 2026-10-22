import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const declared=readFileSync(new URL('../lib/index.d.ts',import.meta.url),'utf8');
const store=readFileSync(new URL('../../../services/messaging/messaging_service/store.py',import.meta.url),'utf8');

// spec: ANYM-270
test('capabilities declares detail and the backend section names',()=>{
  assert.match(declared,/capabilities\(args\?: CapabilitiesArgs\): Promise<Record<string, unknown>>;/);
  assert.match(declared,/detail\?: 'summary' \| 'full';/);
  const union=declared.slice(declared.indexOf('export type CapabilitySection =')).split(';',1)[0];
  const block=store.slice(store.indexOf('CAPABILITY_SECTIONS={')).split('}',1)[0];
  const names=[...block.matchAll(/'([a-z_]+)':\(/g)].map(match=>match[1]);
  assert.equal(names.length,11);
  assert.deepEqual([...union.matchAll(/'([a-z_]+)'/g)].map(match=>match[1]),names);
});
