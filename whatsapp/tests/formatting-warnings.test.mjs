// ANYM-189: each normalized event carries exactly one formatting warning. Observed wire text is
// syntax_interpretation_unknown; text that was not observed, or had to be sanitized or truncated to the
// archive envelope, is native_entities_not_captured. Pure event(); no client, browser, session or network.
import test from 'node:test';
import assert from 'node:assert/strict';
import {event} from '../capture.mjs';

function message(body) {
  const value = {id: {_serialized: 'false_100@c.us_17', remote: '100@c.us'}, from: '100@c.us',
    fromMe: false, timestamp: 1790856000, hasMedia: false,
    getContact() { throw new Error('network forbidden'); }, getQuotedMessage() { throw new Error('network forbidden'); }};
  if (body !== undefined) value.body = body;
  return value;
}

const OBSERVED = ['syntax_interpretation_unknown'];
const NOT_CAPTURED = ['native_entities_not_captured'];

// spec: ANYM-189
test('observed wire text, cached or an explicit edit body, warns only that its syntax is not interpreted', () => {
  for (const [raw, body] of [[message('*bold* _it_'), undefined], [message('old'), '_new_'], [message('old'), ''],
                             [message(''), undefined], [message('x'.repeat(65536)), undefined]]) {
    const value = event(raw, body);
    assert.equal(value.formatting.state, 'wire_observed');
    assert.deepEqual(value.formatting.warnings, OBSERVED);
  }
});

// spec: ANYM-189
test('text that was not observed or could not be kept exact warns that native entities were not captured', () => {
  const cases = {
    'no public body': [message(undefined), undefined],
    'a NUL removed': [message('a\0b'), undefined],
    'a lone surrogate replaced': [message('a\uD800b'), undefined],
    'truncated to the envelope': [message('x'.repeat(65537)), undefined],
  };
  for (const [name, [raw, body]] of Object.entries(cases)) {
    let value;
    try { value = event(raw, body); } catch (error) { assert.fail(`${name}: ${error.message}`); }
    assert.equal(value.formatting.state, 'unknown', name);
    assert.deepEqual(value.formatting.warnings, NOT_CAPTURED, name);
    assert.equal(value.formatting.basis, null, name);
    assert.equal(value.formatting.wire_sha256, null, name);
    assert.equal(value.formatting.entities, null, name);
  }
});
