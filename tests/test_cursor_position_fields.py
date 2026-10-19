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
