"""ANYM-667: a compact read is the rows (or the text), the cursor and the signals an agent acts on."""
import json
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.presentation import render
from test_archive import event
from test_client_contract import rpc
from test_file_index import add_file, drain, worker_for

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / 'tests/fixtures/compact-reads-before-anym-667.json'
# Values that follow the wall clock, not the projection.
VOLATILE = {'first_observed_at', 'last_ingest_at', 'as_of', 'indexed_at'}
FILE_TEXT = 'Synthetic paragraph about the needle. ' * 250
SUMMARY = {'empty_reason', 'requested_range_traversed', 'active_conflicts_in_range', 'next_action'}
VIEW = {'contract_version', 'order', 'content_is_untrusted'}
FILE_KEYS = {'text', 'content', 'offset', 'next_offset', 'content_chars', 'truncated', 'extraction_status',
    'enrichment_status', 'download_status', 'message_id', 'mime', 'display_name', 'model_generated', 'notice',
    'content_coverage', 'provenance', 'detail'}


def masked(value):
    if isinstance(value, dict):
        return {k: '<volatile>' if k in VOLATILE else masked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [masked(v) for v in value]
    return value


def structured(response):
    body = response.json()
    assert 'error' not in body and not body['result'].get('isError'), body
    return body['result']['structuredContent']


class World:
    def __init__(self, store, key, client, ids, chat):
        self.store, self.key, self.client, self.ids, self.chat = store, key, client, ids, chat

    def call(self, name, **arguments):
        return structured(rpc(self.client, self.key, name, arguments))

    def case(self, name):
        return {'search': {'query': 'needle', 'limit': 20}, 'get_messages': {'chat_id': self.chat, 'limit': 20},
            'get_context': {'message_id': self.ids[10], 'before_count': 3, 'after_count': 3},
            'get_file_text': {'attachment_id': 'lean-file', 'limit': 4000}}[name]


@pytest.fixture()
def world(seeded):
    """22 synthetic messages in one chat, one 9500-character text file on the sixth."""
    s, _, _ = seeded
    ar = Archive(s)
    ar.register('lean-tg', 'telegram', 'Synthetic lean', discover=True)
    key = s.issue_key('lean-reader', ['lean-tg'])
    ids = []
    for n in range(1, 23):
        at = '2026-01-15T09:%02d:00+00:00' % n
        ids.append(ar.ingest(event(account='lean-tg', chat='lean-chat', chat_title='Synthetic lean room',
            provider_id=str(n), sender='42', sent_at=at, updated_at=at, body='needle number %d' % n))['message'])
    add_file(s, 'lean-file', ids[5], FILE_TEXT.encode(), 'lean.txt')
    drain(worker_for(s))
    with TestClient(create_app(s, ['testserver'])) as client:
        chat = structured(rpc(client, key, 'search', {'query': 'needle', 'limit': 1}))['items'][0]['chat']
        yield World(s, key, client, ids, chat)


# spec: ANYM-667
@pytest.mark.parametrize('tool', ['search', 'get_messages'])
def test_compact_search_key_set(world, tool):
    result = world.call(tool, **world.case(tool))
    assert len(result['items']) == 20 and result['next_cursor']
    assert set(result) == {'items', 'next_cursor', 'detail', 'coverage', 'synthetic', 'result_evidence', 'conversation_view'}
    assert set(result['result_evidence']) == SUMMARY
    assert set(result['conversation_view']) == VIEW and result['conversation_view']['content_is_untrusted'] is True
    assert 'search' not in result


# spec: ANYM-667
def test_compact_search_keeps_the_anym_53_summary(world):
    empty = world.call('search', query='absent-word-xyz')
    assert empty['items'] == []
    assert {k: empty['result_evidence'][k] for k in SUMMARY} == {'empty_reason': 'history_not_scanned',
        'requested_range_traversed': False, 'active_conflicts_in_range': 0,
        'next_action': 'read_history_or_wait_for_collection'}
    assert set(empty['result_evidence']) - {'suggested_call'} == SUMMARY
    # A caller's own empty filter names the field to fix (ANYM-660).
    filtered = world.call('search', query='needle', chats=[])
    assert filtered['result_evidence']['empty_reason'] == 'empty_filter'
    assert filtered['result_evidence']['empty_fields'] == ['chats'] and filtered['result_evidence']['next_action'] == 'fix_filter'
    # Warnings stay when there are any.
    view = {'view': 'conversation', 'contract_version': 1, 'order': 'asc', 'system_filter': 'x',
        'warnings': ['same_timestamp_order_unknown'], 'content_is_untrusted': True}
    shaped = render('search', {'items': [], 'conversation_view': view, 'search': {'fuzzy_used': False}}, 'compact')
    assert shaped['conversation_view'] == {'contract_version': 1, 'order': 'asc', 'content_is_untrusted': True,
        'warnings': ['same_timestamp_order_unknown']}


# spec: ANYM-667
def test_compact_fuzzy_match_is_marked(world):
    near = world.call('search', query='needlee', fuzzy=True)
    assert near['items'] and near['search'] == {'fuzzy_used': True}
    exact = world.call('search', query='needle', fuzzy=True)
    assert exact['items'] and 'search' not in exact
    full = world.call('search', query='needlee', fuzzy=True, detail='full')
    assert full['search']['fuzzy_used'] is True and full['search']['query_mode'] == 'all'


# spec: ANYM-667
def test_compact_keeps_the_live_suggestion_for_the_gateway():
    evidence = {'basis': 'archived_data', 'requested_range_traversed': False, 'full_history_verified': False,
        'empty_reason': 'history_loading', 'active_conflicts_in_range': 0, 'message_index': 'transactional_with_message',
        'next_action': 'read_history_or_wait_for_collection',
        'suggested_call': {'tool': 'messages_search', 'args': {'live': 'on'}},
        'live': [{'account': 'synthetic', 'state': 'done', 'stored_new': 0, 'hits': []}], 'live_wait': 'failed',
        'archive_reread': 'failed'}
    view = {'view': 'conversation', 'contract_version': 1, 'order': 'asc', 'content_is_untrusted': True}
    for method in ('search', 'get_messages'):
        shaped = render(method, {'items': [], 'result_evidence': dict(evidence), 'conversation_view': dict(view)}, 'compact')
        assert set(shaped['result_evidence']) == SUMMARY | {'suggested_call', 'live', 'live_wait', 'archive_reread'}
        assert shaped['result_evidence']['suggested_call'] == evidence['suggested_call']
        assert shaped['result_evidence']['live'] == evidence['live']


# spec: ANYM-667
@pytest.mark.parametrize('tool', ['search', 'get_messages'])
def test_compact_with_include_coverage_keeps_the_coverage_blocks(world, tool):
    plain = world.call(tool, **world.case(tool))
    covered = world.call(tool, include_coverage=True, **world.case(tool))
    evidence = covered['result_evidence']
    assert {'chats_considered', 'chats_not_scanned', 'chats_loading', 'chats_with_gaps'} <= set(evidence)
    assert not {'basis', 'full_history_verified', 'message_index'} & set(evidence)
    assert set(evidence) - SUMMARY - {'chats'} == {'chats_considered', 'chats_not_scanned', 'chats_loading', 'chats_with_gaps'}
    assert set(covered['conversation_view']) == VIEW and 'search' not in covered
    assert covered['coverage'] == plain['coverage'] and covered['items'] == plain['items']
    # A named chat adds its per-chat statuses.
    named = world.call('search', query='needle', chats=[world.chat], include_coverage=True)
    assert named['result_evidence']['chats'] and 'basis' not in named['result_evidence']
    context = world.call('get_context', include_coverage=True, **world.case('get_context'))
    assert set(context['conversation_view']) == VIEW and 'chat_sync' not in context


# spec: ANYM-667
@pytest.mark.parametrize('tool', ['search', 'get_messages', 'get_context', 'get_file_text'])
def test_full_detail_output_is_unchanged(world, tool):
    snapshot = json.loads(SNAPSHOT.read_text())
    assert snapshot['ids'] == world.ids and snapshot['chat'] == world.chat
    full = world.call(tool, detail='full', **world.case(tool))
    assert masked(full) == snapshot[tool + '/full']


# spec: ANYM-667
@pytest.mark.parametrize('tool', ['search', 'get_messages', 'get_context'])
def test_archive_view_compact_is_unchanged(world, tool):
    snapshot = json.loads(SNAPSHOT.read_text())
    archive = world.call(tool, detail='compact', view='archive', **world.case(tool))
    assert masked(archive) == snapshot[tool + '/archive']
    if tool != 'get_context':
        assert archive['result_evidence']['basis'] == 'archived_data' and archive['search']['language'] == 'auto'


# spec: ANYM-667
def test_compact_get_context_key_set(world):
    result = world.call('get_context', **world.case('get_context'))
    assert set(result) == {'message', 'context', 'context_window', 'conversation', 'conversation_view', 'coverage',
        'synthetic', 'detail'}
    assert set(result['conversation_view']) == VIEW and result['conversation_view']['content_is_untrusted'] is True
    assert {'older_cursor', 'newer_cursor', 'has_more_before', 'has_more_after', 'anchor_index', 'anchor_hidden',
        'older_order', 'newer_order'} <= set(result['context_window'])
    assert len(result['context']) == 6
    # Attachments appear once the anchor carries a file.
    with_file = world.call('get_context', message_id=world.ids[5], before_count=1, after_count=1)
    assert 'attachments' in with_file or any('attachments' in row for row in [with_file['message'], *with_file['context']])


# spec: ANYM-667
def test_compact_file_text_key_set(world):
    result = world.call('get_file_text', **world.case('get_file_text'))
    assert set(result) == FILE_KEYS
    assert result['content_coverage'] == {'retained_chars': 9500, 'truncated': False, 'complete': True}
    assert set(result['provenance']) == {'attachment_id', 'message_id', 'revision', 'content', 'extractor',
        'processor_version', 'sha256'}
    assert result['truncated'] is False and result['next_offset'] == 4000
    full = world.call('get_file_text', detail='full', **world.case('get_file_text'))
    for key in ('understanding', 'extraction_coverage', 'enrichment_notice', 'coverage', 'synthetic'):
        assert key in full and key not in result
    assert 'notice' in full['content_coverage']


# spec: ANYM-667
def test_compact_file_text_pages_to_the_end_with_next_offset(world):
    pages, offset, page = [], 0, {}
    while offset is not None:
        page = world.call('get_file_text', attachment_id='lean-file', offset=offset, limit=4000)
        assert set(page) == FILE_KEYS and page['offset'] == offset
        pages.append(page['text'])
        offset = page['next_offset']
    assert len(pages) == 3 and ''.join(pages) == FILE_TEXT[:9500]
    assert page['next_offset'] is None


# spec: ANYM-667
def test_compact_file_text_keeps_page_spans_and_drops_cell_spans(world):
    spans = {'page_spans': [{'page': 1, 'start': 0, 'end': 2000}],
        'sheet_spans': [{'sheet_index': 0, 'sheet_name': 'Synthetic', 'start': 0, 'end': 3000}],
        'cell_spans': [{'sheet_index': 0, 'row': n, 'column': 1, 'start': 10 * n, 'end': 10 * n + 9} for n in range(1, 300)]}
    with world.store.pool.connection() as c:
        c.execute('UPDATE attachment_text SET extraction_metadata=extraction_metadata||%s WHERE attachment=%s',
            (Jsonb(spans), 'lean-file'))
    compact = world.call('get_file_text', **world.case('get_file_text'))
    assert [s['kind'] for s in compact['source_spans']] == ['page', 'sheet']
    assert 'source_spans_truncated' not in compact
    full = world.call('get_file_text', detail='full', **world.case('get_file_text'))
    assert {s['kind'] for s in full['source_spans']} == {'page', 'sheet', 'cell'}
    assert len(full['source_spans']) == 200 and full['source_spans_truncated'] is True


# spec: ANYM-667
def test_descriptions_name_the_compact_shape():
    tools = {t['name']: t['description'] for t in json.loads(
        (ROOT / 'tests/platform/fixtures/messaging-contracts.json').read_text())['tools']}
    for name in ('search', 'get_messages', 'get_context', 'get_file_text'):
        assert 'detail=full' in tools[name], name
    assert 'include_coverage=true' in tools['search']
