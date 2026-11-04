"""ANYM-756: a read of one Telegram chat says when older history may be missing and names the call that loads it.

Synthetic collector fixture and a disposable database only; no provider."""
import asyncio
import json

from messaging_service.envelope import stable_id
from test_collectors import worker as worker
from test_chat_evidence import history_reader

UNKNOWN = {'coverage': 'unknown'}


def read(s, key, w, chat, **kwargs):
    return s.get_messages(key, stable_id('chat', w.account, chat), **kwargs)['result_evidence']


# spec: ANYM-756
def test_read_of_an_incompletely_traversed_chat_carries_one_unknown_hint_with_the_call(worker):
    w, s, _ = worker
    key = history_reader(w, s)
    asyncio.run(w.sync_once())
    result = s.get_messages(key, stable_id('chat', w.account, '100'))
    hint = result['result_evidence']['history_hint']
    cid = stable_id('chat', w.account, '100')
    assert hint == {**UNKNOWN, 'reason': 'traversal_incomplete', 'next_call': {
        'tool': 'execute_messenger_operation', 'arguments': {
            'account': w.account, 'operation': 'request_chat_history', 'chat_id': cid,
            'parameters': {'before_message': stable_id('msg', w.account, '100', '4'), 'count': 100}},
        'caller_supplies': ['request_id', 'preview_digest']}}
    # The hint is references only: never message text, never a claim that older messages are missing.
    text = json.dumps(hint)
    assert result['items'] and not any(f'message {n}' in text for n in range(1, 6))
    assert 'older_messages_not_archived' not in text


# spec: ANYM-756
def test_read_of_a_not_scanned_or_empty_chat_has_the_unknown_hint_without_a_call(worker):
    w, s, _ = worker
    key = history_reader(w, s)
    w.state.inventory([('200', 'user', 'Waiting'), ('300', 'user', 'Empty')], None)
    with s.pool.connection() as c:
        c.execute("UPDATE collector_chats SET initialized=true WHERE account=%s AND provider_chat='300'", (w.account,))
    assert read(s, key, w, '200')['history_hint'] == {**UNKNOWN, 'reason': 'not_scanned'}
    assert read(s, key, w, '300')['history_hint'] == {**UNKNOWN, 'reason': 'empty_archive'}


# spec: ANYM-756
def test_read_of_a_completely_traversed_chat_has_no_hint(worker):
    w, s, _ = worker
    key = history_reader(w, s)
    w.provider.messages = w.provider.messages[:1]
    asyncio.run(w.sync_once())
    assert 'history_hint' not in read(s, key, w, '100')


# spec: ANYM-756
def test_a_failing_history_pointer_never_costs_the_read(worker, monkeypatch):
    import messaging_service.chat_evidence as evidence
    w, s, _ = worker
    key = history_reader(w, s)
    asyncio.run(w.sync_once())
    def broken(*_):
        raise RuntimeError('synthetic pointer failure')
    monkeypatch.setattr(evidence, 'history_pointer', broken)
    result = s.get_messages(key, stable_id('chat', w.account, '100'))
    assert result['items'] and 'history_hint' not in result['result_evidence']
    status = s.get_sync_status(key, chat_id=stable_id('chat', w.account, '100'))
    assert 'next_call' not in status['history'] and 'history_hint' not in status['history']


# spec: ANYM-756
def test_read_without_coverage_has_no_hint(worker):
    w, s, _ = worker
    key = history_reader(w, s)
    asyncio.run(w.sync_once())
    assert 'history_hint' not in read(s, key, w, '100', include_coverage=False)
