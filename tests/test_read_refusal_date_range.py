"""ANYM-660: every read tool that refuses from_time >= to_time names the field, the rule and the next step."""
import pytest

from messaging_service.store import APIError

FROM = '2026-10-01T00:00:00Z'
EXPECTED = [{'field': 'to_time', 'reason': 'minimum', 'expected': 'later than from_time'}]
HINT = 'swap the bounds or widen the range; the range is half-open and must not be empty'

CALLS = {
    'list_chats': lambda s, key, **kw: s.list_chats(key, **kw),
    'search': lambda s, key, **kw: s.search(key, 'hello', **kw),
    'get_messages': lambda s, key, **kw: s.get_messages(key, 'whatsapp-demo-1-chat', **kw),
    'get_updates': lambda s, key, **kw: s.get_updates(key, 'refusal-consumer', **kw),
    'search_files': lambda s, key, **kw: s.search_files(key, 'hello', **kw),
    'get_sync_status': lambda s, key, **kw: s.get_sync_status(key, chat_id='whatsapp-demo-1-chat', **kw),
}


# spec: ANYM-660
@pytest.mark.parametrize('bounds', [{'from_time': FROM, 'to_time': FROM}, {'from_time': '2026-10-02T00:00:00Z', 'to_time': FROM}],
    ids=['equal', 'reversed'])
@pytest.mark.parametrize('tool', list(CALLS))
def test_invalid_date_range_names_the_field_the_rule_and_the_next_step(seeded, tool, bounds):
    s, a, _ = seeded
    with pytest.raises(APIError, match='invalid_date_range') as failure:
        CALLS[tool](s, a, **bounds)
    assert failure.value.status == 400
    assert failure.value.field_errors == EXPECTED and failure.value.hint == HINT


# spec: ANYM-660
@pytest.mark.parametrize('tool', list(CALLS))
def test_a_valid_date_range_is_not_refused(seeded, tool):
    s, a, _ = seeded
    CALLS[tool](s, a, from_time=FROM, to_time='2026-10-02T00:00:00Z')
