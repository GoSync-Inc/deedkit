"""ANYM-756: a read of one Telegram chat says when older history may be missing and names the call that loads it.

Synthetic collector fixture and a disposable database only; no provider."""
import ast
import asyncio
import json
from pathlib import Path

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
    def broken(c, *_):
        # A real statement error: without the savepoint it would abort the read's transaction.
        c.execute('SELECT 1/0')
    monkeypatch.setattr(evidence, 'history_pointer', broken)
    cid = stable_id('chat', w.account, '100')
    result = s.get_messages(key, cid)
    assert result['items'] and 'history_hint' not in result['result_evidence']
    assert result['result_evidence']['next_action']
    status = s.get_sync_status(key, chat_id=cid)
    assert 'next_call' not in status['history'] and 'history_hint' not in status['history']
    assert status['messages']['messages'] == 2 and 'items' in status['conflicts']


# spec: ANYM-756
def test_read_without_coverage_keeps_the_hint(worker):
    w, s, _ = worker
    key = history_reader(w, s)
    asyncio.run(w.sync_once())
    hint = read(s, key, w, '100', include_coverage=False)['history_hint']
    assert hint['reason'] == 'traversal_incomplete' and hint['next_call']['tool'] == 'execute_messenger_operation'


# spec: ANYM-763
def test_messaging_docstrings_name_the_pointer_fields_their_catalog_descriptions_name():
    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root / 'services/gateway/src/agent_gateway/catalog.json').read_text())
    named = {}
    for tool in catalog:
        fields = sorted(f for f in ('history_hint', 'next_call') if f in tool['description'])
        if tool['module'] == 'messaging' and fields:
            named[tool['operation']] = fields
    # The tools checked, by name: a tool added to the catalog with one of these fields fails here until it is listed.
    assert named == {
        'get_messages': ['history_hint', 'next_call'], 'get_sync_status': ['history_hint', 'next_call'],
        'resolve_reference': ['next_call'], 'send_message': ['next_call']}
    source = (root / 'services/messaging/messaging_service/app.py').read_text()
    docstrings = {
        node.name: ast.get_docstring(node) or '' for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(ast.unparse(d).startswith('mcp.tool(') for d in node.decorator_list)}
    missing = {name: [f for f in fields if f not in docstrings.get(name, '')] for name, fields in named.items()}
    assert not {name: fields for name, fields in missing.items() if fields}


# spec: ANYM-756
def test_default_compact_mcp_reads_carry_the_pointer(worker):
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    from test_client_contract import rpc
    from test_compact_reads_lean import structured
    w, s, _ = worker
    key = history_reader(w, s)
    asyncio.run(w.sync_once())
    cid = stable_id('chat', w.account, '100')
    with TestClient(create_app(s, ['testserver'])) as client:
        read = structured(rpc(client, key, 'get_messages', {'chat_id': cid}))
        status = structured(rpc(client, key, 'get_sync_status', {'chat_id': cid}))
    assert read['detail'] == 'compact' and read['result_evidence']['history_hint']['reason'] == 'traversal_incomplete'
    step = read['result_evidence']['history_hint']['next_call']
    assert step['arguments']['parameters'] == {'before_message': stable_id('msg', w.account, '100', '4'), 'count': 100}
    assert status['history']['next_call'] == step
    assert status['history']['history_hint'] == {**UNKNOWN, 'reason': 'traversal_incomplete'}
