"""Owner-made person links across messengers (ANYM-283); synthetic accounts only."""
from contextlib import contextmanager
from copy import copy
import json
from pathlib import Path
import sys
import threading
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
from starlette.testclient import TestClient

from messaging_service import __main__ as operator
from messaging_service import store as store_module
from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.live_search import unsupported
from messaging_service.policy import OPERATION_PERMISSIONS
from messaging_service.store import APIError
from test_archive import event

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(store_module.__file__).parent
WA_STEPAN = '79110000001@c.us'


@pytest.fixture
def people(seeded):
    s, *_ = seeded
    ar = Archive(s)
    ar.register('person-tg', 'telegram', 'Synthetic Telegram', discover=True)
    ar.register('person-wa', 'whatsapp', 'Synthetic WhatsApp', discover=True)
    def say(account, sender, provider_id, body, *, chat='100', name='', platform=None, **changes):
        platform = platform or ('whatsapp' if account.endswith('-wa') else 'telegram')
        if name:
            changes['sender_profile'] = {'display_name': name}
        return ar.ingest(event(account=account, platform=platform, chat=chat, sender=sender, provider_id=provider_id,
            body=body, **changes))['message']
    steven = say('person-tg', '42', '1', 'Steven about the boat', name='Steven')
    stepan = say('person-wa', WA_STEPAN, 'W1', 'Степан про лодку', chat=WA_STEPAN, name='Степан')
    other = say('person-tg', '43', '2', 'Someone else about the boat', name='Other')
    key = s.issue_key('person-reader', ['person-tg', 'person-wa'])
    return s, ar, key, say, {'steven': steven, 'stepan': stepan, 'other': other}


def ids(result):
    return sorted(row['id'] for row in result['items'])


def pairs(result):
    return sorted((row['account'], row['sender_id']) for row in result['items'])


# spec: ANYM-283
def test_without_links_people_rows_have_no_person_and_results_are_unchanged(people):
    s, _, key, _, mid = people
    rows = s.search_people(key)['items']
    assert len(rows) == 3 and not any('person' in row for row in rows)
    assert ids(s.search(key, include_channels=True)) == sorted(mid.values())


# spec: ANYM-283
def test_linked_person_finds_both_messengers_and_keeps_each_source(people):
    s, _, key, _, mid = people
    person = s.link_person('person-tg', '42')['person']
    assert s.link_person('person-wa', WA_STEPAN, person=person)['person'] == person
    found = s.search(key, person=person)
    assert ids(found) == sorted([mid['steven'], mid['stepan']])
    assert {(r['account'], r['chat'], r['sender']) for r in found['items']} == {
        ('person-tg', found['items'][[r['account'] for r in found['items']].index('person-tg')]['chat'], '42'),
        ('person-wa', found['items'][[r['account'] for r in found['items']].index('person-wa')]['chat'], WA_STEPAN)}
    assert ids(s.search(key, 'boat', person=person)) == [mid['steven']]
    directory = s.search_people(key, person=person)
    assert pairs(directory) == [('person-tg', '42'), ('person-wa', WA_STEPAN)]
    assert all(row['person'] == person for row in directory['items'])
    # A name search shows the link, so an agent can continue with person.
    steven = s.search_people(key, 'Steven')['items']
    assert [row['person'] for row in steven] == [person]
    assert 'person' not in s.search_people(key, 'Other')['items'][0]
    grouped = s.search_people(key, person=person, group_by_sender=True)['items']
    assert sorted((row['account'], row['sender_id']) for row in grouped) == [('person-tg', '42'), ('person-wa', WA_STEPAN)]


# spec: ANYM-283
def test_key_scoped_to_one_account_sees_only_its_member_and_no_hint_of_the_other(people):
    s, _, _, _, mid = people
    person = s.link_person('person-tg', '42')['person']
    s.link_person('person-wa', WA_STEPAN, person=person)
    tg = s.issue_key('person-tg-only', ['person-tg'])
    found, directory = s.search(tg, person=person), s.search_people(tg, person=person)
    assert ids(found) == [mid['steven']] and pairs(directory) == [('person-tg', '42')]
    text = json.dumps([found, directory, s.search_people(tg, 'Steven')], default=str)
    assert 'person-wa' not in text and WA_STEPAN not in text and 'Степан' not in text
    # A person with no visible member behaves exactly like an unknown person.
    s.unlink_person('person-tg', '42')
    for value in (person, str(uuid4())):
        for call in (lambda: s.search(tg, person=value), lambda: s.search_people(tg, person=value)):
            with pytest.raises(APIError) as error:
                call()
            assert (error.value.code, error.value.status) == ('not_found', 404)


# spec: ANYM-283
def test_agent_keys_have_no_way_to_change_links(people):
    s, _, key, _, _ = people
    names = ('link_person', 'unlink_person', 'delete_person', 'list_people')
    assert not any(name in OPERATION_PERMISSIONS for name in names)
    catalog = json.loads((ROOT / 'services/gateway/src/agent_gateway/catalog.json').read_text())
    contracts = json.loads((ROOT / 'tests/platform/fixtures/messaging-contracts.json').read_text())
    assert not any(tool.get('operation') in names for tool in catalog)
    assert not any(tool['name'] in names for tool in contracts['tools'])
    with TestClient(create_app(s, ['testserver'])) as client:
        headers = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json, text/event-stream'}
        for name in names:
            assert client.post('/v1/' + name, json={'account': 'person-tg', 'sender': '42'},
                               headers=headers).status_code in (403, 404)
        listed = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).json()
        assert not any(tool['name'] in names for tool in listed['result']['tools'])
    assert s.list_people() == {'people': []}


# spec: ANYM-283
def test_gateway_role_reads_links_but_cannot_write_them(people):
    s, *_ = people
    s.link_person('person-tg', '42')
    role = 'test_person_gateway_' + uuid4().hex[:8]
    with s.pool.connection() as c:
        c.execute(SOURCE.joinpath('gateway_role.sql').read_text().replace('messaging_gateway', role))
    class RestrictedPool:
        @contextmanager
        def connection(self):
            with s.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                yield c
    gateway = copy(s)
    gateway.pool = RestrictedPool()
    try:
        with gateway.pool.connection() as c:
            assert c.execute('SELECT count(*) AS n FROM person_links').fetchone()['n'] == 1
        for statement in ("INSERT INTO person_links(person,account,sender) VALUES(gen_random_uuid(),'person-tg','43')",
                          "UPDATE person_links SET sender='43'", 'DELETE FROM person_links'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with gateway.pool.connection() as c:
                    c.execute(statement)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            gateway.link_person('person-tg', '43')
    finally:
        with s.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


# spec: ANYM-283
def test_deleting_a_person_removes_its_links_and_leaves_sources_unchanged(people):
    s, _, key, _, mid = people
    person = s.link_person('person-tg', '42')['person']
    s.link_person('person-wa', WA_STEPAN, person=person)
    before = [s.get_context(key, value)['message'] for value in (mid['steven'], mid['stepan'])]
    profiles = s.search_people(key)['items']
    assert s.delete_person(person) == {'person': person, 'unlinked': 2}
    assert s.list_people() == {'people': []}
    assert [s.get_context(key, value)['message'] for value in (mid['steven'], mid['stepan'])] == before
    assert s.search_people(key)['items'] == [{k: v for k, v in row.items() if k != 'person'} for row in profiles]
    with pytest.raises(APIError, match='not_found'):
        s.search(key, person=person)
    with pytest.raises(ValueError, match='unknown_person'):
        s.delete_person(person)


# spec: ANYM-283
def test_person_filter_matches_exact_pairs_and_intersects_with_filters(people):
    s, ar, _, say, mid = people
    ar.register('person-tg2', 'telegram', 'Second Telegram', discover=True)
    a42, b43 = mid['steven'], say('person-tg2', '43', '5', 'B43 text', name='B43')
    a43, b42 = mid['other'], say('person-tg2', '42', '6', 'B42 text')
    side = say('person-tg', '42', '7', 'Steven in a group', chat='200')
    key = s.issue_key('person-cross', ['person-tg', 'person-tg2'])
    person = s.link_person('person-tg', '42')['person']
    s.link_person('person-tg2', '43', person=person)
    assert ids(s.search(key, person=person)) == sorted([a42, side, b43])
    assert a43 not in ids(s.search(key, person=person)) and b42 not in ids(s.search(key, person=person))
    assert ids(s.search(key, person=person, account='person-tg2')) == [b43]
    assert ids(s.search(key, person=person, accounts=['person-tg'])) == sorted([a42, side])
    chat = s.get_context(key, side)['message']['chat']
    assert ids(s.search(key, person=person, chat=chat)) == [side]
    assert ids(s.search(key, person=person, sender='42')) == sorted([a42, side])
    assert ids(s.search(key, person=person, senders=['99'])) == []
    assert pairs(s.search_people(key, person=person, accounts=['person-tg2'])) == [('person-tg2', '43')]


# spec: ANYM-283
def test_hidden_chat_deleted_source_and_revoked_scope_hide_members(people):
    s, ar, _, say, mid = people
    hidden = say('person-tg', '42', '8', 'Steven in a hidden chat', chat='300', name='Steven Hidden')
    person = s.link_person('person-tg', '42')['person']
    s.link_person('person-wa', WA_STEPAN, person=person)
    visible_chat = s.get_context(people[2], mid['steven'])['message']['chat']
    wa_chat = s.get_context(people[2], mid['stepan'])['message']['chat']
    key = s.issue_key('person-chat', ['person-tg', 'person-wa'], chats=[visible_chat, wa_chat])
    assert hidden not in ids(s.search(key, person=person))
    assert [row['chat'] for row in s.search_people(key, person=person)['items'] if row['account'] == 'person-tg'] == [visible_chat]
    assert 'Steven Hidden' not in json.dumps(s.search_people(key, person=person), default=str)
    # A deleted source observation no longer makes its member visible.
    ar.ingest(event(account='person-wa', platform='whatsapp', chat=WA_STEPAN, sender=WA_STEPAN, provider_id='W1',
                    body='Степан про лодку', deleted=True, updated_at='2026-10-02T00:00:00Z'))
    assert ids(s.search(key, person=person)) == [mid['steven']]
    assert pairs(s.search_people(key, person=person)) == [('person-tg', '42')]
    # Authorization is read on each request: a revoked scope stops results at once.
    s.set_access('person-chat', chats=[wa_chat])
    for call in (lambda: s.search(key, person=person), lambda: s.search_people(key, person=person)):
        with pytest.raises(APIError, match='not_found'):
            call()


# spec: ANYM-283
def test_cursors_bind_the_person_and_its_visible_members(people):
    s, ar, key, say, mid = people
    say('person-tg', '42', '9', 'Steven again', name='Steven')
    person = s.link_person('person-tg', '42')['person']
    first = s.search(key, person=person, limit=1)
    assert first['next_cursor']
    # The cursor cannot be reused without the person or for another person.
    with pytest.raises(APIError, match='invalid_cursor'):
        s.search(key, after=first['next_cursor'], limit=1)
    # A member the key cannot read changes nothing for it.
    ar.register('person-tg3', 'telegram', 'Hidden Telegram', discover=True)
    say('person-tg3', '77', '1', 'Unseen', name='Unseen')
    s.link_person('person-tg3', '77', person=person)
    assert len(s.search(key, person=person, after=first['next_cursor'], limit=1)['items']) == 1
    people_first = s.search_people(key, person=person, limit=1)
    assert people_first['next_cursor'] is None or s.search_people(key, person=person, after=people_first['next_cursor'])
    # A visible link change returns cursor_changed for both tools.
    s.link_person('person-wa', WA_STEPAN, person=person)
    people_page = s.search_people(key, person=person, limit=1)
    with pytest.raises(APIError) as error:
        s.search(key, person=person, after=first['next_cursor'], limit=1)
    assert (error.value.code, error.value.status) == ('cursor_changed', 409)
    s.unlink_person('person-wa', WA_STEPAN)
    with pytest.raises(APIError, match='cursor_changed'):
        s.search_people(key, person=person, after=people_page['next_cursor'], limit=1)


# spec: ANYM-283
def test_unlinking_one_member_keeps_the_card_and_a_second_link_is_refused(people):
    s, *_ = people
    person = s.link_person('person-tg', '42')['person']
    s.link_person('person-wa', WA_STEPAN, person=person)
    s.link_person('person-tg', '43', person=person)
    assert s.unlink_person('person-tg', '43') == {'person': person, 'account': 'person-tg', 'sender': '43'}
    assert [(m['account'], m['sender']) for m in s.list_people(person)['people'][0]['members']] == [
        ('person-tg', '42'), ('person-wa', WA_STEPAN)]
    # Repeating a link is a no-op; an identity of another person is refused.
    assert s.link_person('person-tg', '42', person=person)['linked'] is False
    other = s.link_person('person-tg', '43')['person']
    for target in (None, other):
        with pytest.raises(ValueError, match='^identity_already_linked$'):
            s.link_person('person-tg', '42', person=target)
    s.delete_person(other)
    with pytest.raises(ValueError, match='^unknown_identity$'):
        s.link_person('person-tg', '999')
    with pytest.raises(ValueError, match='^unknown_person$'):
        s.link_person('person-tg', '43', person=str(uuid4()))
    with pytest.raises(ValueError, match='^invalid_person$'):
        s.link_person('person-tg', '43', person='not-a-uuid')
    with pytest.raises(ValueError, match='^not_linked$'):
        s.unlink_person('person-tg', '43')


# spec: ANYM-283
def test_concurrent_links_of_one_identity_have_exactly_one_winner(people):
    s, *_ = people
    barrier, results = threading.Barrier(4), []
    def link():
        barrier.wait()
        try:
            results.append(s.link_person('person-tg', '43')['person'])
        except ValueError as error:
            results.append(str(error))
    threads = [threading.Thread(target=link) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count('identity_already_linked') == 3
    assert len(s.list_people()['people']) == 1


# spec: ANYM-283
@pytest.mark.parametrize('value', [True, 'x', 'x' * 300, str(uuid4()).upper() + '\x00'])
def test_malformed_person_is_invalid_filter(people, value):
    s, _, key, _, _ = people
    for call in (lambda: s.search(key, person=value), lambda: s.search_people(key, person=value)):
        with pytest.raises(APIError, match='invalid_filter'):
            call()


# spec: ANYM-283
def test_person_keeps_live_search_archive_only_and_reaches_mcp_and_rest(people):
    s, _, key, _, mid = people
    person = s.link_person('person-tg', '42')['person']
    s.link_person('person-wa', WA_STEPAN, person=person)
    assert 'person' in unsupported({'query': 'boat', 'person': person})
    assert 'person' not in unsupported({'query': 'boat'})
    with TestClient(create_app(s, ['testserver'])) as client:
        headers = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json, text/event-stream'}
        rest = client.post('/v1/search_people', json={'person': person}, headers=headers).json()
        rpc = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'search_people', 'arguments': {'person': person}}}).json()['result']['structuredContent']
        assert pairs(rest) == pairs(rpc) == [('person-tg', '42'), ('person-wa', WA_STEPAN)]
        found = client.post('/v1/search', json={'person': person}, headers=headers).json()
        assert sorted(r['id'] for r in found['items']) == sorted([mid['steven'], mid['stepan']])
        tool = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
            'params': {'name': 'search', 'arguments': {'person': person, 'detail': 'full'}}}).json()['result']
        assert sorted(r['id'] for r in tool['structuredContent']['items']) == sorted([mid['steven'], mid['stepan']])
    catalog = {tool['name']: tool for tool in json.loads(
        (ROOT / 'services/gateway/src/agent_gateway/catalog.json').read_text())}
    for name in ('messages_search', 'messages_search_people'):
        assert catalog[name]['inputSchema']['properties']['person']['type'] == 'string'


class RecordingStore:
    calls = []

    def __init__(self, *args):
        pass

    def link_person(self, account, sender, person=None):
        self.calls.append(('link_person', account, sender, person))
        return {'person': person or '00000000-0000-4000-8000-000000000001', 'account': account, 'sender': sender}

    def unlink_person(self, account, sender):
        self.calls.append(('unlink_person', account, sender))
        return {'person': '00000000-0000-4000-8000-000000000001', 'account': account, 'sender': sender}

    def delete_person(self, person):
        self.calls.append(('delete_person', person))
        return {'person': person, 'unlinked': 1}

    def list_people(self, person=None):
        self.calls.append(('list_people', person))
        return {'people': []}

    def close(self):
        self.calls.append(('close',))


# spec: ANYM-283
def test_cli_person_commands_forward_to_the_owner_store(monkeypatch, tmp_path, capsys):
    RecordingStore.calls = []
    monkeypatch.setattr(operator, 'Store', RecordingStore)
    dsn = tmp_path / 'synthetic.dsn'
    dsn.write_text('postgresql://synthetic.invalid/messaging_probe_test')
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn))
    person = '00000000-0000-4000-8000-000000000002'
    for arguments in (['link-person', '--account', 'tg', '--sender', '42'],
                      ['link-person', '--account', 'wa', '--sender', WA_STEPAN, '--person', person],
                      ['unlink-person', '--account', 'wa', '--sender', WA_STEPAN],
                      ['delete-person', '--person', person], ['list-people'], ['list-people', '--person', person]):
        monkeypatch.setattr(sys, 'argv', ['anymsg', *arguments])
        operator.main()
        json.loads(capsys.readouterr().out)
    assert [call for call in RecordingStore.calls if call != ('close',)] == [
        ('link_person', 'tg', '42', None), ('link_person', 'wa', WA_STEPAN, person),
        ('unlink_person', 'wa', WA_STEPAN), ('delete_person', person), ('list_people', None), ('list_people', person)]
    monkeypatch.setattr(sys, 'argv', ['anymsg', 'link-person', '--account', 'tg'])
    with pytest.raises(SystemExit):
        operator.main()
