"""ANYM-33 fuzzy fallback bounds through the real search: the fixed 0.6 word-similarity threshold, a candidate pool
of the 200 most similar messages (not the newest, not more), similarity ranking, and the response's fuzzy markers.
Synthetic messages on the disposable test database; the similarities are measured with pg_trgm, not assumed."""
from datetime import datetime, timedelta, timezone

QUERY = 'zarnokletix'  # nonsense word: no stem matches it, so the primary query finds nothing and the fallback runs
STRONG = 'yarnokletix'  # word_similarity 0.75
WEAKER = 'zarnokleqix'  # 0.6667
NEAR = 'zarnoketix'  # 0.6429: just above the 0.6 threshold
BELOW = ('znokletix', 'qawnokletix')  # 0.5833: just below it
START = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def add(s, mid, word, minute):
    with s.pool.connection() as c:
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            VALUES(%s,'telegram-demo','telegram-demo-chat',%s,'person',%s,%s,'incoming')''',
                  (mid, mid, f'memo {word} filed', START + timedelta(minutes=minute)))


def similarity(s, word):
    with s.pool.connection() as c:
        return c.execute('SELECT word_similarity(%s, %s) AS s', (QUERY, f'memo {word} filed')).fetchone()['s']


def every(s, token, **kwargs):
    """All result IDs of a fuzzy search, newest first, page by page; and the last page's search block."""
    ids, after = [], ''
    while True:
        page = s.search(token, QUERY, fuzzy=True, order='desc', limit=100, after=after, **kwargs)
        assert all(item['match_type'] == 'trigram_fallback' for item in page['items'])
        ids += [item['id'] for item in page['items']]
        if not page['next_cursor']:
            return ids, page['search']
        after = page['next_cursor']


# spec: ANYM-33
def test_fuzzy_fallback_keeps_words_at_or_above_the_fixed_threshold_ranked_by_similarity(seeded):
    s, _, b = seeded
    assert similarity(s, STRONG) > similarity(s, WEAKER) > similarity(s, NEAR) > 0.6 > max(map(lambda w: similarity(s, w), BELOW)) > 0.55
    for minute, (mid, word) in enumerate([('near', NEAR), ('below-1', BELOW[0]), ('strong', STRONG),
                                          ('below-2', BELOW[1]), ('weaker', WEAKER)]):
        add(s, mid, word, minute)
    assert s.search(b, QUERY)['items'] == []  # no primary match: the fallback is what is tested
    result = s.search(b, QUERY, fuzzy=True, order='relevance', limit=100)
    assert [item['id'] for item in result['items']] == ['strong', 'weaker', 'near']
    assert {item['match_type'] for item in result['items']} == {'trigram_fallback'}
    assert result['search']['fuzzy_used'] is True and result['search']['fuzzy_candidate_limit'] == 200


# spec: ANYM-33
def test_the_candidate_pool_is_the_200_most_similar_not_the_newest_or_more(seeded):
    s, _, b = seeded
    strong = [f'strong-{n:03d}' for n in range(200)]
    for n, mid in enumerate(strong):
        add(s, mid, STRONG, n)  # older
    for n in range(3):
        add(s, f'weaker-{n}', WEAKER, 300 + n)  # newer, above the threshold, less similar than all 200 strong ones
    ids, search = every(s, b)
    assert ids == strong[::-1]  # the pool holds exactly the 200 most similar; the page order is then the caller's
    assert search['fuzzy_used'] is True and search['fuzzy_candidate_limit'] == 200
    # With the strong ones gone from scope, the weaker ones are found: the pool is bounded, not filtered by recency.
    hidden, _ = every(s, b, from_time=(START + timedelta(minutes=250)).isoformat())
    assert hidden == ['weaker-2', 'weaker-1', 'weaker-0']


# spec: ANYM-33
def test_only_a_used_fallback_reports_a_candidate_limit(seeded):
    s, _, b = seeded
    add(s, 'plain', 'passport', 0)
    for kwargs in ({}, {'fuzzy': True}):
        result = s.search(b, 'passport', **kwargs)  # a primary match: the fallback is not used, even when asked for
        assert [item['id'] for item in result['items']] == ['plain']
        assert result['items'][0]['match_type'] != 'trigram_fallback'
        assert result['search']['fuzzy_used'] is False and result['search']['fuzzy_candidate_limit'] is None
    empty = s.search(b, QUERY)
    assert empty['items'] == [] and empty['search']['fuzzy_candidate_limit'] is None
