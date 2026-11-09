"""ANYM-837: the gateway passes menu_choice to messaging only from this release on (synthetic, no backend)."""
import json
from pathlib import Path

import pytest

from agent_gateway.catalog import CATALOG, arguments

# The base commit's (1f9a4641) messages_send_message and messages_preview_message entries, frozen.
BASE = {t['name']: t for t in json.loads((Path(__file__).parent / 'fixtures/base-send-catalog.json').read_text())}
CHOICE = {'message_id': 'msg_synthetic', 'option_id': 'card_1'}
ARGS = {'messages_send_message': {'account': 'wa-synthetic', 'chat_id': 'chat_synthetic', 'text': '',
            'request_id': 'menu-request-0001', 'preview_digest': 'a' * 64},
        'messages_preview_message': {'account': 'wa-synthetic', 'chat_id': 'chat_synthetic', 'text': ''}}


# spec: ANYM-837
@pytest.mark.parametrize('name', sorted(ARGS))
def test_the_base_catalog_refuses_menu_choice_and_never_strips_it_to_a_text_send(name):
    with pytest.raises(ValueError, match='invalid_arguments'):
        arguments(BASE[name], {**ARGS[name], 'menu_choice': CHOICE})
    assert 'menu_choice' not in arguments(BASE[name], ARGS[name])


# spec: ANYM-837
@pytest.mark.parametrize('name', sorted(ARGS))
def test_the_catalog_passes_menu_choice_unchanged(name):
    assert arguments(CATALOG[name], {**ARGS[name], 'menu_choice': CHOICE})['menu_choice'] == CHOICE
    assert 'menu_choice' not in arguments(CATALOG[name], ARGS[name])
