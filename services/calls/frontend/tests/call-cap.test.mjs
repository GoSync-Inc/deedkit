import test from 'node:test';
import assert from 'node:assert/strict';
import { capText } from '../src/call-cap.mjs';

test('owner header shows the effective cap and each raise with the quoted words', () => {
  const expires = '2026-10-19T09:00:00+00:00';
  const view = capText({ daily_call_units: 17, daily_calls_remaining: 12,
    raises: [{ extra_units: 7, expires_at: expires, owner_instruction: 'Ок, ещё 7 звонков <b>' }] });
  assert.equal(view.label, 'Звонков на сутки: осталось 12 из 17');
  assert.deepEqual(view.raises, [`+7 до ${new Date(expires).toLocaleString('ru-RU')} · слова владельца: «Ок, ещё 7 звонков <b>»`]);
  assert.deepEqual(capText(undefined), { label: 'Лимит звонков недоступен', raises: [] });
});
