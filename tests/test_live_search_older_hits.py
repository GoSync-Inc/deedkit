# ruff: noqa: F811
"""ANYM-1000: live hits the archive does not keep, the next live page and whole-word snippets (synthetic only).

No test reaches a provider: fake providers stand in for the collector's live search.
"""
import asyncio

from messaging_service import live_search
from messaging_service.commands import CommandWorker
from messaging_service.telegram_operations import LIVE_TEXT
from telethon.tl import types
from test_archive import event
from test_live_search import Client, job, message, page
from test_live_search import run as run_operation
from test_search_live import ACCOUNT, chat, live, queued_parameters, run  # noqa: F401 (live is a fixture)


CURSOR = 'synthetic-next-cursor-1'


def provider(archive, *, next_cursor=CURSOR):
    """A fake live search: one hit it archives, one older hit outside the collection window it cannot keep."""
    async def search(job):
        await asyncio.to_thread(archive.ingest, event(account=ACCOUNT, chat='100', provider_id='7',
                                                      direction='incoming', body='needle kept'))
        kept = {'chat': chat('100'), 'chat_title': 'Synthetic group', 'chat_type': 'group', 'provider_id': '7',
                'time': '2026-11-01T10:00:00Z', 'sender': {'id': '11', 'name': 'Synthetic'},
                'text': 'needle kept', 'stored': True, 'message_id': 'msg_synthetic'}
        older = {'chat': chat('300'), 'chat_title': 'Synthetic person', 'chat_type': 'private', 'provider_id': '3',
                 'time': '2019-01-01T10:00:00Z', 'sender': {'id': '12', 'name': 'Synthetic person'},
                 'text': 'needle older', 'stored': False, 'reason': 'outside_history_window'}
        return {'outcome': 'confirmed', 'data': {'hits': [older, kept], 'next_cursor': next_cursor,
                                                 'provider_total_approx': 40, 'stored_new': 1}}
    return search


# spec: ANYM-1000
def test_a_hit_the_archive_does_not_keep_is_listed_in_live_items(live):
    s, archive, key, state, _ = live
    result = run(s, key, {'query': 'needle'}, [CommandWorker(state, provider(archive))], live='on', wait_s=10,
                 request_id='synthetic-older-1')
    assert [m['body'] for m in result['items']] == ['needle kept']
    item, = result['live_items']
    assert item['account'] == ACCOUNT and item['chat'] == chat('300') and item['provider_id'] == '3'
    assert item['reason'] == 'outside_history_window' and item['stored'] is False
    assert item['text'] == 'needle older' and item['time'] == '2019-01-01T10:00:00Z'
    assert 'live_items' not in s.search(key, 'needle')  # live: off is unchanged


# spec: ANYM-1000
def test_a_live_call_without_a_provider_search_has_empty_live_items(live):
    s, _, key, _, _ = live
    result = run(s, key, {'query': 'synthetic'}, live='auto')
    assert result['items'] and result['live_items'] == []
    pending = run(s, key, {'query': 'needle'}, live='on', wait_s=0, request_id='synthetic-older-2')
    assert pending['result_evidence']['live'][0]['state'] == 'pending' and pending['live_items'] == []


# spec: ANYM-1000
def test_live_items_are_newest_first_across_finished_entries():
    entries = [{'account': 'a', 'state': 'done', 'hits': [
                   {'time': '2020-01-01T00:00:00Z', 'stored': False, 'reason': 'not_stored'},
                   {'time': '2025-01-01T00:00:00Z', 'stored': True}]},
               {'account': 'b', 'state': 'done', 'hits': [{'time': '2024-01-01T00:00:00Z', 'stored': False,
                                                            'reason': 'deadline'}]},
               {'account': 'c', 'state': 'pending', 'hits': [{'time': '2026-01-01T00:00:00Z', 'stored': False}]}]
    assert [(i['account'], i['reason']) for i in live_search.unarchived(entries)] == [('b', 'deadline'),
                                                                                     ('a', 'not_stored')]


# spec: ANYM-1000
def test_next_call_continues_the_same_live_search_with_its_cursor(live):
    s, archive, key, state, _ = live
    arguments = {'query': 'needle', 'chats': [chat('100')], 'from_time': '2018-01-01T00:00:00Z', 'limit': 5}
    result = run(s, key, arguments, [CommandWorker(state, provider(archive))], live='on', wait_s=10,
                 request_id='synthetic-older-3')
    entry, = result['result_evidence']['live']
    call = entry['next_call']
    assert call['tool'] == 'messages_execute_messenger_operation'
    args = call['args']
    assert args['account'] == ACCOUNT and args['operation'] == 'search_messages_live' and args['chat_id'] == chat('100')
    first, = queued_parameters(s)
    assert args['parameters'] == {**{k: v for k, v in arguments.items() if k != 'chats'}, 'cursor': CURSOR}
    again = run(s, key, arguments, [CommandWorker(state, provider(archive))], live='on', wait_s=10,
                request_id='synthetic-older-3')
    assert again['result_evidence']['live'][0]['next_call'] == call  # the same suggestion asks nothing new
    s.execute_messenger_operation(key, **args)
    s.execute_messenger_operation(key, **args)
    _, following = queued_parameters(s)
    assert following == {**first, 'cursor': CURSOR}  # the cursor binding (account, chat, fields) matches


# spec: ANYM-1000
def test_no_next_call_without_a_cursor(live):
    s, archive, key, state, _ = live
    result = run(s, key, {'query': 'needle'}, [CommandWorker(state, provider(archive, next_cursor=None))],
                 live='on', wait_s=10, request_id='synthetic-older-4')
    entry, = result['result_evidence']['live']
    assert entry['state'] == 'done' and 'next_call' not in entry


# spec: ANYM-1000
def test_a_long_hit_is_cut_at_a_word_boundary_and_marked():
    words = ' '.join(['synthetic'] * 60)
    short = 'short synthetic text'
    data = run_operation(Client(page([message(types.PeerChat(22), 2, words), message(types.PeerChat(22), 1, short)])),
                         job({'limit': 5}))['data']
    long_hit, short_hit = data['hits']
    assert long_hit['text_truncated'] is True and long_hit['text'].endswith('synthetic…')
    assert len(long_hit['text']) <= LIVE_TEXT and words.startswith(long_hit['text'][:-1])
    assert short_hit['text'] == short and 'text_truncated' not in short_hit


# spec: ANYM-1000
def test_snippet_without_a_late_space_keeps_the_width():
    from messaging_service.live_text import snippet
    assert snippet('x' * 400, 300) == {'text': 'x' * 299 + '…', 'text_truncated': True}
    assert snippet('a ' + 'x' * 400, 300)['text'] == 'a ' + 'x' * 297 + '…'  # no word boundary in the second half
    assert snippet('word ' * 10, 300) == {'text': 'word ' * 10}
    assert snippet('exactly', 7) == {'text': 'exactly'}
    assert snippet('some words', 0) == {'text': '', 'text_truncated': True}
    assert snippet('', 0) == {'text': ''}
