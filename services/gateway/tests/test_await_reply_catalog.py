"""ANYM-986: the gateway passes await_reply to messaging only when the caller sets it (synthetic, no backend)."""
import pytest

from agent_gateway.catalog import CATALOG, arguments

ARGS = {'messages_send_message': {'account': 'tg-synthetic', 'chat_id': 'chat_synthetic', 'text': 'Synthetic',
            'request_id': 'await-request-0001', 'preview_digest': 'a' * 64},
        'messages_preview_message': {'account': 'tg-synthetic', 'chat_id': 'chat_synthetic', 'text': 'Synthetic'}}


# spec: ANYM-986
@pytest.mark.parametrize('name', sorted(ARGS))
def test_the_catalog_passes_await_reply_unchanged_and_never_inserts_it(name):
    schema = CATALOG[name]['inputSchema']['properties']['await_reply']
    assert 'default' not in schema and 'silence timer' in schema['description']
    asked = {'within': '6h'}
    assert arguments(CATALOG[name], {**ARGS[name], 'await_reply': asked})['await_reply'] == asked
    # An omitted await_reply stays omitted: a messaging backend without the argument keeps taking every send.
    assert 'await_reply' not in arguments(CATALOG[name], ARGS[name])
    with pytest.raises(ValueError, match='invalid_arguments'):
        arguments(CATALOG[name], {**ARGS[name], 'await_reply': 'six hours'})
