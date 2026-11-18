"""People search across scripts: who turns transliteration on, how far spellings expand, and what a hit claims (ANYM-45).

A synthetic account holds one Latin and two Cyrillic observations of the same name under three different sender IDs.
The same query runs through the Store API (default off), the REST route and the MCP tool (both default on) and with
explicit overrides. Existing tests already pin ranking, cursors, ё/е folding and grouping across accounts.
"""
import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.name_variants import patterns
from test_archive import event

ACCOUNT = 'translit-contract'
NAMES = {'latin-1': 'Yulia Primer', 'cyrillic-1': 'Юлия Пример', 'cyrillic-2': 'Юлия Пример',
         'ending-first': 'Maria Liliya Nataliya Kseniya', 'ending-third': 'Mariya Liliya Natalia Kseniya'}


@pytest.fixture()
def people(seeded):
    store, _, _ = seeded
    archive = Archive(store)
    archive.register(ACCOUNT, 'telegram', 'Transliteration contract', discover=True)
    for number, (sender, name) in enumerate(NAMES.items(), start=1):
        archive.ingest(event(account=ACCOUNT, chat='100', provider_id=f'translit-{number}', sender=sender,
                             sender_profile={'display_name': name, 'username': ''}))
    token = store.issue_key('translit-reader', [ACCOUNT])
    with TestClient(create_app(store, ['testserver'])) as client:
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream',
                   'MCP-Protocol-Version': '2025-11-25'}

        def rest(**arguments):
            response = client.post('/v1/search_people', headers={'Authorization': 'Bearer ' + token}, json=arguments)
            assert response.status_code == 200, response.text
            return response.json()

        def mcp(**arguments):
            reply = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'search_people', 'arguments': arguments}}).json()['result']
            assert not reply.get('isError'), reply
            return reply['structuredContent']

        def store_api(**arguments):
            return store.search_people(token, **arguments)
        yield {'store': store_api, 'rest': rest, 'mcp': mcp}


def found(result):
    return sorted((row['sender_id'], row['match_type']) for row in result['items'])


LATIN_ONLY = [('latin-1', 'literal')]
CROSS_SCRIPT = [('cyrillic-1', 'transliteration_candidate'), ('cyrillic-2', 'transliteration_candidate'),
                ('latin-1', 'literal')]


# spec: ANYM-45
def test_rest_and_mcp_transliterate_by_default_and_the_store_api_does_not(people):
    assert found(people['store'](query='Yulia')) == LATIN_ONLY
    assert found(people['rest'](query='Yulia')) == CROSS_SCRIPT
    assert found(people['mcp'](query='Yulia')) == CROSS_SCRIPT


# spec: ANYM-45
def test_an_explicit_transliterate_value_wins_on_every_surface(people):
    for surface in ('store', 'rest', 'mcp'):
        assert found(people[surface](query='Yulia', transliterate=False)) == LATIN_ONLY, surface
        assert found(people[surface](query='Yulia', transliterate=True)) == CROSS_SCRIPT, surface


# spec: ANYM-45
def test_a_cyrillic_query_finds_the_latin_spelling_and_every_response_disclaims_identity_proof(people):
    result = people['rest'](query='Юлия')
    # The Cyrillic observations match literally; the Latin one only through the ia/iya/ya spelling variants.
    assert found(result) == [('cyrillic-1', 'literal'), ('cyrillic-2', 'literal'),
                             ('latin-1', 'transliteration_candidate')]
    for surface in ('store', 'rest', 'mcp'):
        for transliterate in (True, False):
            body = people[surface](query='Yulia', transliterate=transliterate)
            assert body['transliteration_is_identity_proof'] is False and body['names_are_verified_identity'] is False


# spec: ANYM-45
def test_one_transliterated_name_never_merges_different_sender_ids(people):
    grouped = people['rest'](query='Yulia', group_by_sender=True)
    assert sorted(row['sender_id'] for row in grouped['items']) == ['cyrillic-1', 'cyrillic-2', 'latin-1']
    assert all({observation['sender_id'] for observation in row['observations']} == {row['sender_id']}
               for row in grouped['items'])


# spec: ANYM-45
def test_spelling_variants_change_only_the_first_two_endings_and_stay_within_eight(people):
    query = 'Мария Лилия Наталия Ксения'  # four terminal -ия endings
    first = patterns('мария лилия наталия ксения')
    assert first == patterns('мария лилия наталия ксения') and first == sorted(first)  # deterministic
    assert 1 < len(first) <= 8
    # Every variant keeps the third and fourth endings as the letter table writes them.
    assert all(variant.endswith(' nataliya kseniya%') for variant in first)
    assert '%maria liliya nataliya kseniya%' in first and '%mariya lilia nataliya kseniya%' in first
    # Through the database: a first-ending variant is found; a third-ending one is outside the bound.
    assert [row['sender_id'] for row in people['rest'](query=query)['items']] == ['ending-first']
