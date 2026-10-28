"""One target for any identifier, candidates for anything else (ANYM-545); synthetic accounts only."""
import string
from uuid import uuid4
import zlib

from hypothesis import HealthCheck, given, settings, strategies as st
from platform_contracts.agent import validate_error
import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.error_details import payload
from messaging_service.resolve_target import classify
from messaging_service.store import APIError
from messaging_service.whatsapp_collector import WhatsAppState
from test_archive import event

TG, WA, NATIVE, NATIVE2 = 'rt-tg', 'rt-wa', 'rt-native-1', 'rt-native-2'
WA_STEPAN = '79110000002@c.us'
PHONE = '12025550177'  # NANP fictional 555-01xx range; no chat anywhere
READ_ONLY = frozenset({'messages_search_people', 'platform_resolve_target'})
PRIVATE = {'chat_type': 'private'}


@pytest.fixture
def world(seeded):
    s, *_ = seeded
    ar = Archive(s)
    ar.register(TG, 'telegram', 'Synthetic Telegram', discover=True)
    ar.register(WA, 'whatsapp', 'Synthetic WhatsApp', discover=True)
    leases = []
    for account in (NATIVE, NATIVE2):
        ar.register(account, 'whatsapp', 'Synthetic native', discover=True)
        epoch = ar.acquire(account, 'resolve-test')
        state = WhatsAppState(ar, account, ('resolve-test', epoch))
        state.phase('connection', 'connected', 'provider_ready')
        state.provider_observation('whatsmeow')
        leases.append((account, state))

    def say(account, sender, provider_id, body, *, chat, name='', username='', at='2026-10-01T12:00:00+00:00',
            metadata=None, title=None):
        profile = {k: v for k, v in (('display_name', name), ('username', username)) if v}
        changes = {'sender_profile': profile} if profile else {}
        if metadata:
            changes['metadata'] = metadata
        return ar.ingest(event(account=account, platform='whatsapp' if account != TG else 'telegram', chat=chat,
                               chat_title=title or name or 'Synthetic room', sender=sender, provider_id=provider_id,
                               body=body, sent_at=at, updated_at=at, **changes))['message']

    mid = {
        'steven': say(TG, '42', '1', 'Steven private', chat='42', name='Steven Boatwright', username='steven_rt',
                      metadata=PRIVATE),
        'group': say(TG, '42', '2', 'Steven in the club', chat='100', title='Synthetic Boat Club',
                     metadata={'chat_type': 'group'}),
        'stepan': say(WA, WA_STEPAN, 'W1', 'Степан лично', chat=WA_STEPAN, name='Степан', metadata=PRIVATE),
    }
    key = s.issue_key('rt-reader', [TG, WA])
    yield s, ar, key, say, mid
    for account, state in leases:
        ar.release(account, 'resolve-test', state.lease[1])


def chat_of(s, account, provider_chat):
    with s.pool.connection() as c:
        return c.execute('SELECT id FROM chats WHERE account=%s AND provider_chat=%s',
                         (account, provider_chat)).fetchone()['id']


def target(s, key, to, **arguments):
    result = s.resolve_target(key, to, **arguments)
    assert result['state'] == 'succeeded' and result['next'] is None
    return result['target']


def refused(s, key, to, **arguments):
    with pytest.raises(APIError) as caught:
        s.resolve_target(key, to, **arguments)
    body = payload(caught.value, 'resolve_target')
    validate_error(body, read_only=READ_ONLY)
    body.pop('request_id', None)
    return body


# spec: ANYM-545
def test_chat_message_and_message_sender_ids_resolve_exactly(world):
    s, _, key, _, mid = world
    steven, club = chat_of(s, TG, '42'), chat_of(s, TG, '100')
    assert target(s, key, steven) == {'account': TG, 'chat_id': steven, 'network': 'telegram',
                                      'label': 'Steven Boatwright', 'how_resolved': 'chat_id'}
    assert target(s, key, mid['group'])['chat_id'] == club
    assert target(s, key, mid['group'])['how_resolved'] == 'message_chat'
    by_sender = target(s, key, mid['group'], sender=True)
    assert by_sender['chat_id'] == steven and by_sender['how_resolved'] == 'message_sender'
    assert refused(s, key, steven, sender=True)['error'] == 'invalid_arguments'


# spec: ANYM-545
def test_person_jid_phone_and_username_resolve_to_their_one_chat(world):
    s, _, key, _, _ = world
    stepan, steven = chat_of(s, WA, WA_STEPAN), chat_of(s, TG, '42')
    person = s.link_person(WA, WA_STEPAN)['person']
    found = target(s, key, person)
    assert (found['chat_id'], found['person'], found['how_resolved']) == (stepan, person, 'person')
    assert found['phone'] == '+79110000002'
    for to, how in ((WA_STEPAN, 'jid'), ('+7 911 000-00-02', 'phone'), ('0079110000002', 'phone')):
        found = target(s, key, to)
        assert (found['chat_id'], found['how_resolved'], found['person']) == (stepan, how, person)
    found = target(s, key, '@steven_rt')
    assert (found['chat_id'], found['how_resolved']) == (steven, 'username')


# spec: ANYM-545
def test_a_phone_without_a_chat_opens_one_on_the_single_opted_in_account_else_lists_accounts(world):
    s, _, _, _, _ = world
    key = s.issue_key('rt-sender', [TG, WA, NATIVE, NATIVE2], permissions=['read', 'send'])
    assert refused(s, key, '+' + PHONE)['hint'] == 'no_chat_and_no_opted_in_account'
    s.set_messenger_operations(NATIVE, ['open_chat_by_phone'])
    assert target(s, key, '+' + PHONE) == {'account': NATIVE, 'chat_id': None, 'phone': '+' + PHONE,
                                           'network': 'whatsapp', 'label': 'WhatsApp +' + PHONE,
                                           'how_resolved': 'phone_opens_chat', 'opens_chat': True}
    s.set_messenger_operations(NATIVE2, ['open_chat_by_phone'])
    body = refused(s, key, '+' + PHONE)
    assert body['error'] == 'selection_required' and 'next' not in body
    assert [(c['id'], c['kind']) for c in body['candidates']] == [(NATIVE, 'account'), (NATIVE2, 'account')]
    assert not any('next' in c or PHONE in str(c) for c in body['candidates'])
    assert target(s, key, '+' + PHONE, account=NATIVE2)['account'] == NATIVE2
    # A key without send has no usable opt-in: the same not_found as no account at all.
    assert refused(s, s.issue_key('rt-plain', [NATIVE]), '+' + PHONE)['error'] == 'not_found'


# spec: ANYM-545
def test_a_name_with_one_match_still_asks_and_each_candidate_names_its_exact_id(world):
    s, _, key, _, _ = world
    body = refused(s, key, 'boatwright')
    steven = chat_of(s, TG, '42')
    assert body['error'] == 'selection_required' and 'next' not in body
    assert body['candidates'] == [{'id': steven, 'label': 'Steven Boatwright', 'kind': 'chat', 'network': 'telegram',
                                   'account': TG, 'last_activity': '2026-10-01T12:00:00+00:00',
                                   'next': {'tool': 'platform_resolve_target',
                                            'arguments': {'to': steven, 'purpose': 'message'}}}]
    assert target(s, key, body['candidates'][0]['next']['arguments']['to'])['chat_id'] == steven


# spec: ANYM-545
def test_a_person_with_two_usable_channels_returns_one_candidate_per_channel(world):
    s, _, key, _, _ = world
    person = s.link_person(TG, '42')['person']
    s.link_person(WA, WA_STEPAN, person=person)
    body = refused(s, key, person)
    assert body['error'] == 'selection_required'
    assert sorted((c['network'], c['id']) for c in body['candidates']) == sorted(
        [('telegram', chat_of(s, TG, '42')), ('whatsapp', chat_of(s, WA, WA_STEPAN))])
    assert target(s, key, person, account=WA)['chat_id'] == chat_of(s, WA, WA_STEPAN)


# spec: ANYM-545
def test_more_than_ten_matches_return_the_ten_latest_and_a_read_only_listing(world):
    s, _, key, say, _ = world
    for i in range(12):
        say(TG, str(500 + i), f'c{i}', 'crowd', chat=str(500 + i), name=f'Synthetic Crowd {i}',
            at=f'2026-10-02T12:{i:02d}:00+00:00', metadata=PRIVATE)
    body = refused(s, key, 'Crowd')
    assert body['more'] is True and body['next'] == {'tool': 'messages_search_people', 'arguments': {'query': 'Crowd'}}
    assert [c['label'] for c in body['candidates']] == [f'Synthetic Crowd {i}' for i in range(11, 1, -1)]


# spec: ANYM-545
def test_hidden_identities_are_the_same_not_found_as_missing_ones(world):
    s, _, _, say, _ = world
    say(WA, '79110000003@c.us', 'W9', 'hidden', chat='79110000003@c.us', name='Hidden Twin', metadata=PRIVATE)
    person = s.link_person(TG, '42')['person']
    s.link_person(WA, WA_STEPAN, person=person)
    hidden = s.link_person(WA, '79110000003@c.us')['person']
    tg_only = s.issue_key('rt-tg-only', [TG])
    # Mixed members: the visible one is the only channel; the hidden one is not a candidate.
    assert target(s, tg_only, person)['chat_id'] == chat_of(s, TG, '42')
    missing = refused(s, tg_only, str(uuid4()))
    assert refused(s, tg_only, hidden) == missing and missing['next']['tool'] == 'messages_search_people'
    # Each hidden form answers exactly as a missing one of the same form (a name's listing repeats the query only).
    for alias, absent in (('Hidden Twin', 'Absent Twin'), ('+79110000003', '+79110000009'),
                          ('79110000003@c.us', '79110000009@c.us'), (chat_of(s, WA, '79110000003@c.us'), 'chat_' + '0' * 32)):
        body, other = refused(s, tg_only, alias), refused(s, tg_only, absent)
        assert body['error'] == 'not_found' and body['next']['tool'] == 'messages_search_people'
        assert {**body, 'next': body['next']['tool']} == {**other, 'next': other['next']['tool']}
    # A name shared by a visible chat and a hidden one lists only the visible one.
    say(TG, '77', 'twin', 'visible twin', chat='77', name='Hidden Twin', metadata=PRIVATE)
    assert [c['id'] for c in refused(s, tg_only, 'Hidden Twin')['candidates']] == [chat_of(s, TG, '77')]
    assert refused(s, tg_only, 'Hidden', account=WA) == refused(s, tg_only, 'Hidden', account='rt-missing')


# spec: ANYM-545
def test_access_revoked_between_two_calls_turns_the_target_into_not_found(world):
    s, _, _, _, mid = world
    steven, club = chat_of(s, TG, '42'), chat_of(s, TG, '100')
    key = s.issue_key('rt-chats', [TG], chats=[steven, club])
    assert target(s, key, mid['group'], sender=True)['chat_id'] == steven
    s.set_access('rt-chats', chats=[club])
    assert refused(s, key, mid['group'], sender=True)['error'] == 'not_found'
    assert refused(s, key, steven)['error'] == 'not_found'
    assert refused(s, key, '@steven_rt')['error'] == 'not_found'


# spec: ANYM-545
def test_contact_cards_resolve_to_their_phone_or_telegram_user_and_cards_without_fields_say_so(world):
    s, _, key, say, _ = world
    def card(provider_id, *contacts, account=TG, chat='42'):
        return say(account, '42' if account == TG else WA_STEPAN, provider_id, 'card', chat=chat,
                   metadata={**PRIVATE, 'message_kind': 'contact', 'contact': {'status': 'parsed',
                                                                                'contacts': list(contacts)}})
    telegram = card('k1', {'name': 'Synthetic Friend', 'telegram_user_id': '42', 'phones': []})
    phone = card('k2', {'name': 'Synthetic Stepan', 'phones': [{'number': '+79110000002', 'normalized': True}]})
    empty = card('k3', {'name': 'Only A Name', 'phones': [{'number': '0812-0000', 'normalized': False}]})
    found = target(s, key, telegram)
    assert (found['chat_id'], found['how_resolved']) == (chat_of(s, TG, '42'), 'contact_card_telegram_user')
    found = target(s, key, phone)
    assert (found['chat_id'], found['how_resolved']) == (chat_of(s, WA, WA_STEPAN), 'contact_card_phone')
    body = refused(s, key, empty)
    assert (body['error'], body['hint'], body['next']['tool']) == ('not_found', 'contact_card_fields_missing',
                                                                    'messages_search_people')


# spec: ANYM-545
def test_not_found_carries_a_read_only_next(world):
    s, _, key, _, _ = world
    for to in ('chat_' + '0' * 32, 'msg_' + '0' * 32, str(uuid4()), '@nobody_rt', 'Nobody Synthetic'):
        body = refused(s, key, to)
        assert body['error'] == 'not_found' and body['next']['tool'] in READ_ONLY


# spec: ANYM-545
def test_calls_refuse_group_chats_and_rest_mcp_agree(world):
    s, _, key, _, mid = world
    assert refused(s, key, mid['group'], purpose='call')['error'] == 'unsupported_target'
    headers = {'Authorization': 'Bearer ' + key}
    with TestClient(create_app(s, ['testserver'])) as client:
        rest = client.post('/v1/resolve_target', json={'to': 'boatwright'}, headers=headers)
        rpc = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                          'params': {'name': 'resolve_target', 'arguments': {'to': 'boatwright'}}}).json()
    assert rest.status_code == 409 and rest.json()['candidates'][0]['id'] == chat_of(s, TG, '42')
    assert rpc['result']['isError'] and 'selection_required' in str(rpc['result'])




def observe(s, account, source, pn, lid):
    """A verified PN/LID observation, as whatsapp_identity.record() stores it."""
    with s.pool.connection() as c:
        sender = c.execute('SELECT sender FROM messages WHERE id=%s', (source,)).fetchone()['sender']
        c.execute('''INSERT INTO whatsapp_identity_observations(account,source_message,identity_id,
            pn,lid,provenance,first_observed_at,observed_at) VALUES(%s,%s,%s,%s,%s,'whatsmeow_message_alt_v1',now(),now())''',
            (account, source, sender, pn, lid))


# spec: ANYM-545
def test_an_accounts_own_phone_chat_wins_over_its_lid_alias_chat(world):
    s, _, key, say, mid = world
    say(WA, '555000111@lid', 'L1', 'lid chat', chat='555000111@lid', name='Степан LID', metadata=PRIVATE)
    observe(s, WA, mid['stepan'], WA_STEPAN, '555000111@lid')
    found = target(s, key, '+79110000002')
    assert (found['chat_id'], found['how_resolved']) == (chat_of(s, WA, WA_STEPAN), 'phone')


# spec: ANYM-545
def test_a_hidden_exclusion_of_the_numbers_lid_answers_like_an_unknown_number(world):
    s, _, _, say, _ = world
    key = s.issue_key('rt-excl', [NATIVE], permissions=['read', 'send'])
    s.set_messenger_operations(NATIVE, ['open_chat_by_phone'])
    hidden = say(NATIVE, '555000222@lid', 'H1', 'gone', chat='555000222@lid', metadata=PRIVATE)
    observe(s, NATIVE, hidden, PHONE + '@c.us', '555000222@lid')
    with s.pool.connection() as c:
        c.execute('UPDATE messages SET deleted=true WHERE id=%s', (hidden,))
        c.execute('UPDATE account_policies SET exclude_chats=%s WHERE account=%s', (['555000222@lid'], NATIVE))
    excluded, unknown = target(s, key, '+' + PHONE), target(s, key, '+12025550178')
    assert excluded['opens_chat'] and unknown['opens_chat']
    assert {**excluded, 'phone': None, 'label': None} == {**unknown, 'phone': None, 'label': None}


# spec: ANYM-545
def test_a_contact_cards_phone_keeps_the_requested_account(world):
    s, _, key, say, _ = world
    card = say(TG, '42', 'k9', 'card', chat='42', metadata={**PRIVATE, 'message_kind': 'contact', 'contact': {
        'status': 'parsed', 'contacts': [{'name': 'Synthetic Stepan',
                                          'phones': [{'number': '+79110000002', 'normalized': True}]}]}})
    assert target(s, key, card)['account'] == WA
    assert refused(s, key, card, account=TG)['error'] == 'not_found'


# spec: ANYM-545
@pytest.mark.parametrize('text', ['+44 7700 900123', '+44 (7700) 900‑123', '0044 7700 900123'])
def test_every_phone_format_the_send_normalizer_accepts_is_exact(text):
    assert classify(text) == ('phone', '447700900123')


# spec: ANYM-545
def test_phone_shaped_titles_never_reach_a_candidate_label(world):
    s, _, key, say, _ = world
    say(TG, '61', 'p1', 'phone title', chat='61', name='Synthetic +1  202  555  0177', metadata=PRIVATE)
    body = refused(s, key, 'Synthetic +1')
    assert [c['label'] for c in body['candidates']] == ['telegram chat'] and '555' not in str(body)


# spec: ANYM-545
def test_a_matching_private_chat_is_found_behind_many_matching_identities_without_one(world):
    s, _, key, say, _ = world
    for i in range(205):
        say(TG, str(1000 + i), f'g{i}', 'group', chat='300', name=f'Synthetic Many {i}', title='Group room',
            metadata={'chat_type': 'group'})
    # The private chat's sender sorts after every group sender (the old scan stopped at 200 identities).
    say(TG, '9999', 'last', 'private', chat='9999', name='Synthetic Many Last', title='Private room', metadata=PRIVATE,
        at='2026-09-01T12:00:00+00:00')
    assert [c['id'] for c in refused(s, key, 'Synthetic Many')['candidates']] == [chat_of(s, TG, '9999')]


# ASCII and basic Cyrillic letters: Python and PostgreSQL lower-case them alike.
LETTERS = st.sampled_from(string.ascii_letters + ''.join(map(chr, range(0x410, 0x450))))
NON_EXACT = st.text(alphabet=st.one_of(LETTERS, st.sampled_from(" .,'")), max_size=30)


# spec: ANYM-545
@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(names=st.lists(st.builds(lambda a, b: a + b, st.text(LETTERS, min_size=1, max_size=1), NON_EXACT),
                      min_size=1, max_size=3), data=st.data())
def test_property_non_exact_input_never_returns_a_single_target_and_errors_follow_the_rulebook(world, names, data):
    """Fixtures vary per example; inputs are drawn without the resolver's classifier (no digit, @, _ or -)."""
    s, _, key, say, _ = world
    for name in names:
        sender = str(10_000 + zlib.crc32(name.encode()))
        say(TG, sender, 'h' + sender, 'synthetic', chat=sender, name=name, metadata=PRIVATE)
    chosen = data.draw(st.sampled_from(names))
    start = data.draw(st.integers(0, len(chosen) - 1))
    fragment = chosen[start:data.draw(st.integers(start + 1, len(chosen)))]
    to = data.draw(st.one_of(st.just(fragment), st.builds(lambda a, b: a + b, st.text(LETTERS, min_size=1,
                                                                                    max_size=1), NON_EXACT)))
    if not to.strip():
        to = chosen
    with pytest.raises(APIError) as caught:
        s.resolve_target(key, to)
    body = payload(caught.value, 'resolve_target')
    validate_error(body, read_only=READ_ONLY)
    assert body['error'] in ('selection_required', 'not_found') and 'target' not in body
    if body['error'] == 'selection_required':
        assert all(c['next'] == {'tool': 'platform_resolve_target', 'arguments': {'to': c['id'], 'purpose': 'message'}}
                   for c in body['candidates'])
    if to.strip().lower() in chosen.lower():
        assert body['error'] == 'selection_required'
