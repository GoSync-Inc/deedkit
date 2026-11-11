"""ANYM-464: the gateway passes schedule_at to messaging only when the caller sets it (synthetic, no backend)."""
import pytest

from agent_gateway.catalog import CATALOG, arguments

ARGS = {'messages_send_message': {'account': 'tg-synthetic', 'chat_id': 'chat_synthetic', 'text': 'Synthetic',
            'request_id': 'scheduled-request-0001', 'preview_digest': 'a' * 64},
        'messages_preview_message': {'account': 'tg-synthetic', 'chat_id': 'chat_synthetic', 'text': 'Synthetic'},
        'messages_send': {'to': 'chat_synthetic', 'text': 'Synthetic', 'dry_run': True}}


# spec: ANYM-464
@pytest.mark.parametrize('name', sorted(ARGS))
def test_the_catalog_passes_schedule_at_unchanged_and_never_inserts_it(name):
    schema = CATALOG[name]['inputSchema']['properties']['schedule_at']
    assert 'default' not in schema and 'RFC 3339' in schema['description']
    at = '2026-11-12T09:00:00+03:00'
    assert arguments(CATALOG[name], {**ARGS[name], 'schedule_at': at})['schedule_at'] == at
    # An omitted schedule_at stays omitted: a messaging backend without the argument keeps taking every send.
    assert 'schedule_at' not in arguments(CATALOG[name], ARGS[name])
    with pytest.raises(ValueError, match='invalid_arguments'):
        arguments(CATALOG[name], {**ARGS[name], 'schedule_at': 1})
