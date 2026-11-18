"""The fuzzy fallback's 0.6 word-similarity threshold at its edge (ANYM-33), as a precision follow-up to
test_fuzzy_search_bounds, whose nearest words sit at 0.5833 and 0.6429.

Each case is one legal single-word query (letters only, 9 to 17 characters) and one synthetic message. The pairs
were found on PostgreSQL itself. word_similarity is a ratio of trigram counts, and a 3-64 character word has at
most 65 trigrams, so 0.59, 0.6 and 0.61 cannot share one query; each band gets its own.
The expected answer is never assumed. It is PostgreSQL's own pg_trgm %> operator, evaluated at a transaction-local
threshold of 0.6 on a separate connection: whether the message is a fallback hit. The real Store.search must agree.
At exactly 0.6 the measured value is float4 0.6000000238, which the operator includes.
"""
from datetime import datetime, timezone

import pytest

CASES = {  # band: (query, message body, measured similarity band)
    'below': ('aqxzuvubqnjpxsvi', 'memo aqxbuvuubqnjpxsvi filed', (0.59, 0.6)),
    'at': ('ajndcubkq', 'memo aahndcubkq filed', (0.6, 0.6)),
    'above': ('exdquwwmkmvhvfpfh', 'memo exdquwwmkmmhgvfpfh filed', (0.6, 0.61)),
}


def reference(s, query, body):
    """PostgreSQL's own answer at a fixed 0.6: the measured similarity and whether %> admits the body."""
    with s.pool.connection() as c:  # one transaction; the setting is local to it and ends with it
        c.execute("SELECT set_config('pg_trgm.word_similarity_threshold','0.6',true)")
        row = c.execute('SELECT word_similarity(lower(%s),lower(%s)) AS s, lower(%s) %%> lower(%s) AS hit',
                        (query, body, body, query)).fetchone()
    return row['s'], row['hit']


# spec: ANYM-33
@pytest.mark.parametrize('band', list(CASES))
def test_the_fallback_admits_exactly_what_postgresql_admits_at_a_fixed_0_6(seeded, band):
    s, _, b = seeded
    query, body, (low, high) = CASES[band]
    similarity, hit = reference(s, query, body)
    # The pair sits where its band says, in PostgreSQL's real (float4) precision.
    if band == 'at':
        assert similarity == pytest.approx(0.6, abs=1e-6)
    else:
        assert low < similarity < high and similarity != pytest.approx(0.6, abs=1e-6)
    assert hit is (band != 'below')
    with s.pool.connection() as c:
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            VALUES('edge','telegram-demo','telegram-demo-chat','edge','person',%s,%s,'incoming')''',
                  (body, datetime(2026, 10, 1, 12, tzinfo=timezone.utc)))
    assert s.search(b, query)['items'] == []  # no primary match: the fallback decides
    result = s.search(b, query, fuzzy=True, order='relevance', limit=100)
    assert result['search']['fuzzy_used'] is True
    assert [item['id'] for item in result['items']] == (['edge'] if hit else [])
    assert all(item['match_type'] == 'trigram_fallback' for item in result['items'])


# spec: ANYM-33
def test_the_reference_threshold_never_leaks_past_its_own_transaction(seeded):
    s, _, _ = seeded
    with s.pool.connection() as c:
        before = c.execute("SELECT current_setting('pg_trgm.word_similarity_threshold') AS v").fetchone()['v']
    reference(s, *CASES['at'][:2])
    with s.pool.connection() as c:
        assert c.execute("SELECT current_setting('pg_trgm.word_similarity_threshold') AS v").fetchone()['v'] == before
