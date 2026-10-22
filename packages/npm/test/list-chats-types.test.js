import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const declared=readFileSync(new URL('../lib/index.d.ts',import.meta.url),'utf8');

// spec: ANYM-427
test('listChats declares the promotional filter and its result fields',()=>{
  assert.match(declared,/listChats\(args\?: ListChatsArgs\): Promise<ListChatsResult>;/);
  assert.match(declared,/export type PromotionalFilter = 'exclude' \| 'include' \| 'only';/);
  assert.match(declared,/promotional\?: PromotionalFilter/);
  for(const field of ['excluded_promotional_total?: number','likely_promotional?: true',
    "promotional_basis?: 'business_sender' | 'repeated_days'",'awaiting_reply_likely_promotional?: true',
    "awaiting_reply_promotional_basis?: 'business_sender' | 'repeated_days'"])assert.ok(declared.includes(field),field);
});
