// The daily call cap and every owner raise with the owner's quoted words (ANYM-353).
export function capText(cap) {
  if (!cap) return { label: 'Лимит звонков недоступен', raises: [] };
  return {
    label: `Звонков на сутки: осталось ${cap.daily_calls_remaining} из ${cap.daily_call_units}`,
    raises: (cap.raises || []).map(r => `+${r.extra_units} до ${new Date(r.expires_at).toLocaleString('ru-RU')} · слова владельца: «${r.owner_instruction}»`),
  };
}
