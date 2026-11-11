"""ANYM-928: the gateway passes mentions to messaging only when the caller sets it (synthetic, no backend)."""
import pytest

from agent_gateway.catalog import CATALOG, arguments

ARGS = {'messages_send_message': {'account': 'tg-synthetic', 'chat_id': 'chat_synthetic', 'text': 'Hi @Anna',
            'request_id': 'mention-request-0001', 'preview_digest': 'a' * 64},
        'messages_preview_message': {'account': 'tg-synthetic', 'chat_id': 'chat_synthetic', 'text': 'Hi @Anna'}}
MENTION = {'sender': '42', 'offset': 3, 'length': 5}


# spec: ANYM-928
@pytest.mark.parametrize('name', sorted(ARGS))
def test_the_catalog_lists_mentions_passes_them_unchanged_and_never_inserts_them(name):
    schema = CATALOG[name]['inputSchema']
    assert 'default' not in schema['properties']['mentions']
    assert 'mention_not_member' in schema['properties']['mentions']['description']
    assert arguments(CATALOG[name], {**ARGS[name], 'mentions': [MENTION]})['mentions'] == [MENTION]
    # Omitted stays omitted: a messaging backend without the argument keeps taking every send.
    assert 'mentions' not in arguments(CATALOG[name], ARGS[name])
    for bad in ([{**MENTION, 'offset': '3'}], [{'sender': '42', 'offset': 3}], [{**MENTION, 'extra': 1}], MENTION):
        with pytest.raises(ValueError, match='invalid_arguments'):
            arguments(CATALOG[name], {**ARGS[name], 'mentions': bad})
