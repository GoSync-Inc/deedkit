"""ANYM-927: the gateway passes silent to messaging only when the caller sets it (synthetic, no backend)."""
import pytest

from agent_gateway.catalog import CATALOG, arguments

ARGS = {'messages_send_message': {'account': 'tg-synthetic', 'chat_id': 'chat_synthetic', 'text': 'Synthetic',
            'request_id': 'silent-request-0001', 'preview_digest': 'a' * 64},
        'messages_preview_message': {'account': 'tg-synthetic', 'chat_id': 'chat_synthetic', 'text': 'Synthetic'}}


# spec: ANYM-927
@pytest.mark.parametrize('name', sorted(ARGS))
def test_the_catalog_passes_silent_unchanged_and_never_inserts_it(name):
    schema = CATALOG[name]['inputSchema']['properties']['silent']
    assert 'default' not in schema and 'notification sound' in schema['description']
    for value in (True, False):
        assert arguments(CATALOG[name], {**ARGS[name], 'silent': value})['silent'] is value
    # An omitted silent stays omitted: a messaging backend without the argument keeps taking every send.
    assert 'silent' not in arguments(CATALOG[name], ARGS[name])
    with pytest.raises(ValueError, match='invalid_arguments'):
        arguments(CATALOG[name], {**ARGS[name], 'silent': 'true'})
