"""ANYM-307: a tampered position field inside a decodable q1 cursor is invalid_cursor, never invalid_filter or invalid_utc_date."""
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from messaging_service.archive import Archive
from messaging_service.store import APIError
from test_archive import event
from test_query_reliability import ranked_directory

BAD_TEXT = ['', 5, None, ['x'], 'a\x00b', 'x'*257, '\ud800', 'a\udfffb']
BAD_DATE = [*BAD_TEXT, 'not-a-date', '2026-10-19T10:00:00', '0001-01-01T00:00:00+01:00', '9999-12-31T23:59:59-01:00']


def tampered(cursor, **fields):
    raw = cursor[3:]
    body = json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw) % 4)))
    assert set(fields) <= set(body['p']), (fields, body['p'])
    body['p'].update(fields)
    return 'q1.'+base64.urlsafe_b64encode(json.dumps(body, separators=(',', ':')).encode()).decode().rstrip('=')


def matrix(cases):
    return [pytest.param(field, value, id=field+'-'+repr(value)[:12]) for field, values in cases for value in values]


def rejects(call, cursor, code='invalid_cursor', **fields):
    with pytest.raises(APIError, match=code):
        call(after=tampered(cursor, **fields))


# spec: ANYM-307
@pytest.mark.parametrize('view', ['archive', 'conversation'])
@pytest.mark.parametrize('field,value', matrix([('id', BAD_TEXT), ('at', BAD_DATE)]))
def test_message_cursor_position_fields_are_invalid_cursor(seeded, view, field, value):
    s, a, _ = seeded
    call = lambda **kw: s.search(a, view=view, limit=1, exclude_channels=True, **kw)
    cursor = call()['next_cursor']
    assert cursor.startswith('q1.')
    rejects(call, cursor, **{field: value})


# spec: ANYM-307
def test_message_cursor_valid_shapes_keep_their_own_codes(seeded):
    s, a, _ = seeded
    call = lambda **kw: s.search(a, limit=1, exclude_channels=True, **kw)
    first = call()
    cursor = first['next_cursor']
    assert call(after=cursor)['items']
    assert s.search(a, limit=1, after=first['items'][0]['id'])['items']  # legacy bare-ID pagination (no type filter)
    rejects(call, cursor, code='not_found', id='no-such-message')
    rejects(call, cursor, code='cursor_changed', revision=99)
    with pytest.raises(APIError, match='invalid_filter'):
        call(after='q1.'+'A'*4094)


CURSOR_EXPECTED = 'a next_cursor returned by the same call with the same filters and order'


def assert_cursor_refusal(failure):
    assert failure.value.code == 'invalid_cursor' and failure.value.status == 400
    assert failure.value.field_errors == [{'field': 'after', 'reason': 'format', 'expected': CURSOR_EXPECTED}]
    assert failure.value.next_action == 'restart_query_without_cursor'


# spec: ANYM-660
@pytest.mark.parametrize('cursor', ['not-a-cursor', 'q1.!!!', 'q1.' + 'A' * 40, 'x1.abcd'], ids=['no-prefix-text', 'bad-base64', 'not-json', 'wrong-prefix'])
def test_malformed_cursor_names_the_cursor_rule_and_the_next_step(seeded, cursor):
    s, a, _ = seeded
    for call in (lambda: s.list_chats(a, after=cursor, order='activity'), lambda: s.search(a, limit=1, exclude_channels=True, after=cursor, query='x', view='conversation')):
        with pytest.raises(APIError, match='invalid_cursor') as failure:
            call()
        assert_cursor_refusal(failure)


# spec: ANYM-660
def test_cursor_made_for_other_filters_gets_the_same_refusal_through_search_search_files_and_list_chats(seeded):
    s, a, _ = seeded
    search_cursor = s.search(a, limit=1, exclude_channels=True)['next_cursor']
    assert search_cursor.startswith('q1.')
    with pytest.raises(APIError, match='invalid_cursor') as changed:
        s.search(a, limit=1, exclude_channels=False, include_channels=True, after=search_cursor)
    assert_cursor_refusal(changed)
    with pytest.raises(APIError, match='invalid_cursor') as files:
        s.search_files(a, 'x', after=search_cursor)
    assert_cursor_refusal(files)
    chats_cursor = s.list_chats(a, limit=1, order='activity')['next_cursor']
    with pytest.raises(APIError, match='invalid_cursor') as chats:
        s.list_chats(a, limit=1, order='activity', query='other', after=chats_cursor)
    assert_cursor_refusal(chats)
    # The error does not say which filter changed, and a tampered cursor reads the same as a changed filter (ANYM-307).
    with pytest.raises(APIError, match='invalid_cursor') as tamper:
        s.list_chats(a, limit=1, order='activity', after=chats_cursor[:-3] + 'AAA')
    assert (tamper.value.field_errors, tamper.value.next_action) == (chats.value.field_errors, chats.value.next_action)


# spec: ANYM-660
def test_list_chats_position_checks_return_the_cursor_refusal(seeded):
    s, a, _ = seeded
    cursor = s.list_chats(a, limit=1, order='activity')['next_cursor']
    raw = cursor[3:]
    body = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
    body['p']['unexpected'] = 1
    forged = 'q1.' + base64.urlsafe_b64encode(json.dumps(body, separators=(',', ':')).encode()).decode().rstrip('=')
    with pytest.raises(APIError, match='invalid_cursor') as failure:
        s.list_chats(a, limit=1, order='activity', after=forged)
    assert_cursor_refusal(failure)


# spec: ANYM-660
def test_limit_is_not_part_of_what_a_cursor_binds(seeded):
    s, a, _ = seeded
    for call in (lambda limit, **kw: s.search(a, limit=limit, exclude_channels=True, **kw), lambda limit, **kw: s.list_chats(a, limit=limit, order='activity', **kw)):
        cursor = call(1)['next_cursor']
        assert call(2, after=cursor)['items']


# spec: ANYM-307
@pytest.mark.parametrize('grouped', [False, True])
@pytest.mark.parametrize('field,value', matrix([('account', BAD_TEXT), ('sender', BAD_TEXT), ('rank', [-1, 8, '1', None, True, 1.5]),
                                                ('id', BAD_TEXT)]))
def test_people_cursor_position_fields_are_invalid_cursor(seeded, grouped, field, value):
    s, ar, key = ranked_directory(seeded)
    call = lambda **kw: s.search_people(key, 'Агус', transliterate=True, group_by_sender=grouped, limit=1, **kw)
    cursor = call()['next_cursor']
    body = json.loads(base64.urlsafe_b64decode(cursor[3:]+'='*(-len(cursor[3:]) % 4)))
    if field in body['p']:
        rejects(call, cursor, **{field: value})
        return
    # The field belongs to the other people cursor shape: the extra key alone makes the position invalid,
    # whatever its value (the shape check compares key sets before any field is read).
    body['p'][field] = value
    with pytest.raises(APIError, match='invalid_cursor'):
        call(after='q1.'+base64.urlsafe_b64encode(json.dumps(body, separators=(',', ':')).encode()).decode().rstrip('='))


# spec: ANYM-307
@pytest.mark.parametrize('grouped,field', [(False, 'id'), (True, 'sender')])
def test_people_cursor_valid_shapes_keep_their_own_codes(seeded, grouped, field):
    s, ar, key = ranked_directory(seeded)
    call = lambda **kw: s.search_people(key, 'Агус', transliterate=True, group_by_sender=grouped, limit=1, **kw)
    cursor = call()['next_cursor']
    assert call(after=cursor)['items']
    rejects(call, cursor, code='not_found', **{field: 'no-such-sender'})
    rejects(call, cursor, code='cursor_changed', rank=7)


@pytest.fixture()
def chats(seeded):
    s, _, _ = seeded
    ar = Archive(s)
    ar.register('cursor-tg', 'telegram', 'Synthetic cursor chats', discover=True)
    key = s.issue_key('cursor-agent', ['cursor-tg'])
    for n in range(3):
        at = (datetime.now(timezone.utc)-timedelta(hours=n+1)).isoformat()
        ar.ingest(event(account='cursor-tg', chat='cursor-chat-'+str(n), provider_id='1', sent_at=at, updated_at=at,
            direction='incoming', body='Synthetic question?', sender='42', metadata={'chat_type': 'private'}))
    return s, key


# spec: ANYM-307
@pytest.mark.parametrize('order,field,value', [
    *[('activity', f, v) for f, vs in [('id', BAD_TEXT), ('at', [x for x in BAD_DATE if x is not None])] for v in vs],
    *[('priority', f, v) for f, vs in [('id', BAD_TEXT), ('at', BAD_DATE), ('as_of', BAD_DATE),
                                        ('score', ['1', None, True, 1.5])] for v in vs]])
def test_chat_cursor_position_fields_are_invalid_cursor(chats, order, field, value):
    s, key = chats
    call = lambda **kw: s.list_chats(key, order=order, limit=1, awaiting_reply=order == 'priority', include_coverage=False, **kw)
    cursor = call()['next_cursor']
    assert cursor.startswith('q1.')
    rejects(call, cursor, **{field: value})


# spec: ANYM-307
def test_chat_cursor_valid_shapes_keep_their_own_codes(chats):
    s, key = chats
    activity = lambda **kw: s.list_chats(key, order='activity', limit=1, include_coverage=False, **kw)
    priority = lambda **kw: s.list_chats(key, order='priority', limit=1, awaiting_reply=True, include_coverage=False, **kw)
    cursor = activity()['next_cursor']
    activity(after=tampered(cursor, at=None))  # a chat without a head has at: null; the position is valid
    assert activity(after=cursor)['items']
    rejects(activity, cursor, code='not_found', id='no-such-chat')
    assert priority(after=priority()['next_cursor'])['items']
    rejects(priority, priority()['next_cursor'], code='not_found', id='no-such-chat')
    with pytest.raises(APIError, match='invalid_filter'):
        activity(after='q1.'+'A'*4094)
    assert s.list_chats(key, limit=1, include_coverage=False, after=s.list_chats(key, limit=1, include_coverage=False)['items'][0]['id'])
