"""ANYM-668: query_mode=any (at least one word), its rank, live rule, cost and the descriptions of every mode."""
import json
from pathlib import Path

import pytest

from messaging_service.search_query import compile_query
from messaging_service.store import APIError
from test_file_index import add_file, drain, search as search_files, worker_for
from test_search_live import live as live, queued, run
from test_search_performance import Measured, scans
from test_search_v2 import add

ROOT = Path(__file__).resolve().parents[1]


def ids(result):
    return [r['id'] for r in result['items']]


def find_key(value, key):
    if isinstance(value, dict):
        if key in value:
            return value[key]
        value = list(value.values())
    for item in value if isinstance(value, list) else []:
        found = find_key(item, key)
        if found is not None:
            return found


def invoices(s):
    add(s, 'any-invoice', 'Please find the invoice attached', day=5)
    add(s, 'any-receipt', 'Your receipt for the order', day=6)
    add(s, 'any-three', 'The bill and the invoice and the receipt', day=7)
    add(s, 'any-none', 'Nothing relevant here', day=8)
    add(s, 'any-two', 'invoice and receipt', day=9)
    add(s, 'any-or', 'plain or text', day=10)
    add(s, 'any-yo', 'Оплатите счёт сегодня', day=11)
    add(s, 'any-ye', 'Оплатите счет сегодня', day=12)


# spec: ANYM-668
def test_query_mode_is_described_with_all_modes_the_traps_and_an_example():
    from messaging_service.tool_types import QUERY_MODE_DESCRIPTION as text
    for needle in ('all', 'any', 'phrase', 'web', 'exact', '`счёт счет invoice`', '"exact phrase" OR other -excluded',
                   'the word `or` itself cannot be searched in web', 'use any or exact', 'order=relevance',
                   'one live search per word'):
        assert needle in text, needle
    # One line per mode, in the order the enum lists them.
    modes = ('all', 'any', 'phrase', 'web', 'exact')
    mode_lines = [line for line in text.split('\n') if line.split(' ')[0].rstrip(':') in modes]
    assert [line.split(' ')[0].rstrip(':') for line in mode_lines] == list(modes)
    catalog = json.loads((ROOT / 'services/gateway/src/agent_gateway/catalog.json').read_text())
    for operation in ('search', 'search_files'):
        # `operation` is a backend path, unique per module only: the messaging tools are the ones under test.
        entry, = [e for e in catalog if e['operation'] == operation and e['module'] == 'messaging']
        mode = entry['inputSchema']['properties']['query_mode']
        assert mode['description'] == text and mode['enum'] == ['all', 'any', 'phrase', 'web', 'exact']
    contracts = json.loads((ROOT / 'tests/platform/fixtures/messaging-contracts.json').read_text())
    for tool in ('search', 'search_files'):
        mode = [t for t in contracts['tools'] if t['name'] == tool][0]['inputSchema']['properties']['query_mode']
        assert mode['description'] == text and 'any' in mode['enum']
    assert '`any`' in (ROOT / 'SEARCH.md').read_text() and 'any' in (ROOT / 'FILE_INDEX.md').read_text()
    snapshot = json.loads((ROOT / 'tests/fixtures/capabilities-full-snapshot.json').read_text())
    assert find_key(snapshot, 'query_modes') == ['all', 'any', 'phrase', 'web', 'exact']


# spec: ANYM-668
def test_web_mode_lowercase_or_is_the_operator(seeded):
    s, a, b = seeded
    invoices(s)
    assert 'any-receipt' in ids(s.search(b, 'invoice or receipt', query_mode='web'))
    assert ids(s.search(b, 'invoice or receipt', query_mode='web')) == ['any-invoice', 'any-receipt', 'any-three', 'any-two']
    assert ids(s.search(b, 'invoice or receipt')) == ['any-three', 'any-two']


# spec: ANYM-668
def test_any_mode_matches_one_of_several_words(seeded):
    s, a, b = seeded
    invoices(s)
    assert ids(s.search(b, 'invoice receipt bill', query_mode='any')) == ['any-invoice', 'any-receipt', 'any-three', 'any-two']
    assert ids(s.search(b, 'счёт счет', query_mode='any')) == ['any-yo', 'any-ye']
    # The Russian dictionary folds ё and е into one lexeme, so the example also works for `all`; the point of
    # `any` is that a spelling the dictionary does not fold (счёт / счет / invoice) needs no query syntax.
    assert ids(s.search(b, 'счёт invoice', query_mode='any')) == ['any-invoice', 'any-three', 'any-two', 'any-yo', 'any-ye']
    assert ids(s.search(b, 'INVOICE   absentword', query_mode='any')) == ['any-invoice', 'any-three', 'any-two']


# spec: ANYM-668
def test_any_mode_treats_operators_as_text(seeded):
    s, a, b = seeded
    invoices(s)
    # In web mode `-receipt` would exclude; here every term is its own plain query.
    assert ids(s.search(b, 'bill -receipt', query_mode='any')) == ['any-receipt', 'any-three', 'any-two']
    assert ids(s.search(b, '"receipt"', query_mode='any')) == ['any-receipt', 'any-three', 'any-two']
    assert ids(s.search(b, 'OR', query_mode='any')) == ['any-or']
    assert ids(s.search(b, 'bill OR receipt', query_mode='any')) == ['any-receipt', 'any-three', 'any-two', 'any-or']
    assert ids(s.search(b, '"unmatched " OR', query_mode='any')) == ['any-or']


# spec: ANYM-668
def test_any_mode_stop_word_term_does_not_break_the_others(seeded):
    s, a, b = seeded
    invoices(s)
    # `the` is an English stop word: its term compiles to an empty query that adds nothing.
    assert ids(s.search(b, 'the invoice', query_mode='any', search_language='english')) == ['any-invoice', 'any-three', 'any-two']
    assert ids(s.search(b, 'the', query_mode='any', search_language='english')) == []


# spec: ANYM-668
def test_any_mode_empty_query_lists_as_in_other_modes(seeded):
    s, a, b = seeded
    invoices(s)
    for mode in ('all', 'phrase', 'web', 'exact'):
        assert ids(s.search(b, '', query_mode='any')) == ids(s.search(b, '', query_mode=mode))
    assert ids(s.search(b, '   ', query_mode='any')) == ids(s.search(b, '   ', query_mode='all')) == []


# spec: ANYM-668
def test_any_mode_ranks_more_matching_terms_first(seeded):
    s, a, b = seeded
    invoices(s)
    ranked = s.search(b, 'invoice receipt bill', query_mode='any', order='relevance')
    assert ids(ranked)[:2] == ['any-three', 'any-two'] and set(ids(ranked)[2:]) == {'any-invoice', 'any-receipt'}
    assert 0 < ranked['items'][-1]['score'] < ranked['items'][1]['score'] < ranked['items'][0]['score'] <= 1000000
    # Duplicate terms count once; a stop word cannot outrank a real term.
    assert ids(s.search(b, 'invoice INVOICE receipt', query_mode='any', order='relevance'))[0] in ('any-three', 'any-two')
    assert compile_query('a A b', 'any').terms == 2


# spec: ANYM-668
def test_any_mode_with_fuzzy_is_invalid(seeded):
    s, a, b = seeded
    invoices(s)
    with pytest.raises(APIError, match='invalid_fuzzy_query'):
        s.search(b, 'invoice', query_mode='any', fuzzy=True)


# spec: ANYM-668
def test_any_mode_cursor_pages_and_refuses_a_mode_change(seeded):
    s, a, b = seeded
    invoices(s)
    seen, page = [], s.search(b, 'invoice receipt bill', query_mode='any', order='relevance', limit=1)
    first = page['next_cursor']
    while True:
        seen += ids(page)
        if not page['next_cursor']:
            break
        page = s.search(b, 'invoice receipt bill', query_mode='any', order='relevance', limit=1, after=page['next_cursor'])
    assert seen == ids(s.search(b, 'invoice receipt bill', query_mode='any', order='relevance')) and len(seen) == 4
    for other in ('all', 'web'):
        with pytest.raises(APIError, match='invalid_cursor'):
            s.search(b, 'invoice receipt bill', query_mode=other, order='relevance', after=first)
    page = s.search(b, 'invoice receipt bill', query_mode='any', limit=2)
    rest = s.search(b, 'invoice receipt bill', query_mode='any', limit=2, after=page['next_cursor'])
    assert ids(page) + ids(rest) == ['any-invoice', 'any-receipt', 'any-three', 'any-two']


def files(s):
    add_file(s, 'any-file-both', 'demo-tg-1', b'unicorn and zebra together', 'both.txt')
    add_file(s, 'any-file-one', 'demo-tg-2', b'only a unicorn here', 'one.txt')
    add_file(s, 'any-file-none', 'demo-wa-1', b'nothing at all', 'none.txt')
    drain(worker_for(s))


# spec: ANYM-668
def test_search_files_any_mode_matches_one_of_several_words_and_empty_query_lists(seeded):
    s, a, b = seeded
    files(s)
    names = lambda r: sorted(x['attachment_id'] for x in r['items'])
    assert names(search_files(s, b, 'unicorn zebra', query_mode='any')) == ['any-file-both', 'any-file-one']
    assert names(search_files(s, b, 'unicorn zebra')) == ['any-file-both']
    assert names(search_files(s, b, '"zebra" OR', query_mode='any')) == ['any-file-both']
    assert names(search_files(s, b, '', query_mode='any')) == names(search_files(s, b, '', query_mode='all'))


# spec: ANYM-668
def test_search_files_any_mode_ranks_by_distinct_terms(seeded):
    s, a, b = seeded
    files(s)
    ranked = search_files(s, b, 'unicorn zebra', query_mode='any', order='relevance')
    assert [x['attachment_id'] for x in ranked['items']] == ['any-file-both', 'any-file-one']
    assert ranked['items'][0]['rank'] > ranked['items'][1]['rank'] > 0
    page = search_files(s, b, 'unicorn zebra', query_mode='any', order='relevance', limit=1)
    rest = search_files(s, b, 'unicorn zebra', query_mode='any', order='relevance', limit=1, after=page['next_cursor'])
    assert [x['attachment_id'] for x in page['items'] + rest['items']] == ['any-file-both', 'any-file-one']
    with pytest.raises(APIError, match='invalid_cursor'):
        search_files(s, b, 'unicorn zebra', query_mode='all', order='relevance', after=page['next_cursor'])


# spec: ANYM-668
def test_search_files_any_mode_counts_terms_across_text_and_caption(seeded):
    s, a, b = seeded
    add(s, 'any-cap-a', 'receipt')
    add(s, 'any-cap-b', 'nothing here')
    add_file(s, 'any-x-a', 'any-cap-a', b'invoice attached', 'a.txt')     # invoice in the text, receipt in the caption
    add_file(s, 'any-x-b', 'any-cap-b', b'invoice invoice invoice invoice', 'b.txt')
    drain(worker_for(s))
    ranked = search_files(s, b, 'invoice receipt', query_mode='any', order='relevance')
    assert [x['attachment_id'] for x in ranked['items']] == ['any-x-a', 'any-x-b']
    assert ranked['items'][0]['rank'] > ranked['items'][1]['rank']
    page = search_files(s, b, 'invoice receipt', query_mode='any', order='relevance', limit=1)
    rest = search_files(s, b, 'invoice receipt', query_mode='any', order='relevance', limit=1, after=page['next_cursor'])
    assert [x['attachment_id'] for x in page['items'] + rest['items']] == ['any-x-a', 'any-x-b']


# spec: ANYM-668
@pytest.mark.parametrize('query', ['Straße STRASSE', 'STRASSE Straße'])
def test_any_mode_keeps_spellings_that_differ_after_lowercasing(seeded, query):
    s, a, b = seeded
    add(s, 'any-ss-1', 'Straße', day=5)
    add(s, 'any-ss-2', 'STRASSE', day=6)
    add_file(s, 'any-ss-f1', 'demo-tg-1', 'Straße'.encode(), 'f1.txt')
    add_file(s, 'any-ss-f2', 'demo-tg-2', 'STRASSE'.encode(), 'f2.txt')
    drain(worker_for(s))
    # The message search under test reads bodies; the files above belong to the file search below (ANYM-665).
    assert ids(s.search(b, query, query_mode='any', search_language='simple', include_media_text=False)) == ['any-ss-1', 'any-ss-2']
    assert sorted(x['attachment_id'] for x in search_files(s, b, query, query_mode='any', search_language='simple')['items']
                  if x['attachment_id'].startswith('any-ss-f')) == ['any-ss-f1', 'any-ss-f2']
    assert compile_query(query, 'any').terms == 2 and compile_query('invoice INVOICE Invoice', 'any').terms == 1


# spec: ANYM-668
def test_any_mode_with_live_auto_searches_the_archive_only(live):
    s, _, key, _, _ = live
    # The archive has a hit: auto never goes live.
    hit = run(s, key, {'query': 'synthetic needle', 'query_mode': 'any'}, live='auto')
    assert hit['items'] and hit['result_evidence']['live'] == [] and queued(s) == 0
    # The archive is thin: the evidence says the mode is not supported live, and nothing is queued.
    thin = run(s, key, {'query': 'needle thread', 'query_mode': 'any'}, live='auto', wait_s=0)
    entry, = thin['result_evidence']['live']
    assert entry['state'] == 'unsupported_filter' and entry['fields'] == ['query_mode'] and queued(s) == 0


# spec: ANYM-668
def test_any_mode_with_live_forced_is_unsupported_filter_on_query_mode(live):
    s, _, key, _, _ = live
    result = run(s, key, {'query': 'needle thread', 'query_mode': 'any'}, live='on', wait_s=0)
    entry, = result['result_evidence']['live']
    assert entry['state'] == 'unsupported_filter' and entry['fields'] == ['query_mode'] and 'items' in result and queued(s) == 0


# 99 distinct one-character terms: 197 characters, the longest query the 200-character bound allows.
SHORT_TERMS = ([chr(c) for c in range(0x430, 0x450)] + [chr(c) for c in range(0x61, 0x7b)] + list('0123456789')
               + [chr(c) for c in range(0x3b1, 0x3ca) if c != 0x3c2] + [chr(c) for c in range(0x5d0, 0x5eb)])[:99]


def bulk(s):
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='60s'")
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            SELECT 'anybulk-'||n,'telegram-demo','telegram-demo-chat','anybulk-'||n,'fixture',
                %s||' '||CASE WHEN n%%400=7 THEN 'scarcelexicalneedle ' ELSE '' END||repeat('ordinary conversation content ',5),
                now()+n*interval '1 second'
            FROM generate_series(1,40000) n''', [' '.join(SHORT_TERMS[:6])])
        c.execute('ANALYZE messages')
        for index in ('message_fts', 'message_fts_english', 'message_fts_indonesian', 'message_fts_russian'):
            c.execute('SELECT gin_clean_pending_list(%s::regclass)', (index,))


def measured_search(s, token, forced=False, **arguments):
    from messaging_service.message_queries import search
    with s.authorized(token, 'search') as (c, p):
        if forced:
            c.execute('SET LOCAL enable_seqscan=off')
        measured = Measured(c)
        result = search(s, measured, p, **arguments)
    return result, measured.plans


# spec: ANYM-668
def test_any_mode_common_words_stay_inside_the_limit(seeded):
    s, a, b = seeded
    bulk(s)
    # The pool's 5 s statement limit applies: a slow plan raises here instead of returning.
    result, plans = measured_search(s, b, query='ordinary conversation', query_mode='any', order='relevance', limit=20)
    # Every row matches, so a bitmap or sequential read is the right plan here; the cost is bounded by the matches.
    assert len(result['items']) == 20 and plans and plans[0]['Actual Total Time'] < 4000, [scans(plan) for plan in plans]


# spec: ANYM-668
def test_any_mode_many_short_terms_stay_inside_the_limit(seeded):
    s, a, b = seeded
    bulk(s)
    query = ' '.join(f'w{n:02d}' for n in range(40)) + ' scarcelexicalneedle'
    assert 150 <= len(query) <= 200
    result, plans = measured_search(s, b, query=query, query_mode='any', order='relevance', limit=20)
    # At this volume the planner may prefer a heap read to 41 x 4 index lookups; either way it is far inside the limit.
    assert len(result['items']) == 20 and plans and plans[0]['Actual Total Time'] < 2000, [scans(plan) for plan in plans]


# spec: ANYM-668
def test_any_mode_rare_terms_use_the_existing_text_indexes(seeded):
    s, a, b = seeded
    bulk(s)
    result, plans = measured_search(s, b, forced=True, query='scarcelexicalneedle absentqxzword', query_mode='any', order='relevance', limit=20)
    assert len(result['items']) == 20 and plans
    assert 'message_fts' in json.dumps(plans), [scans(plan) for plan in plans]


# spec: ANYM-668
def test_any_mode_boundary_query_over_broad_matches_stays_inside_the_limit(seeded):
    s, a, b = seeded
    bulk(s)
    query = ' '.join(SHORT_TERMS)
    assert len(query) == 197 and len(SHORT_TERMS) == 99
    # Six of the 99 terms are in every one of 40,000 rows: all of them match and are ranked before the page is cut.
    result, plans = measured_search(s, b, query=query, query_mode='any', order='relevance', limit=20)
    assert len(result['items']) == 20 and plans and plans[0]['Actual Total Time'] < 4000, [scans(plan) for plan in plans]
    assert result['items'][0]['score'] == round(6 / 99 * 1000000)
