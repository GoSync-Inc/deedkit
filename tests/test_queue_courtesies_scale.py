"""ANYM-810: courtesies=exclude reads a bounded tail per chat over a large synthetic catalogue; no provider or real data."""
from types import SimpleNamespace

import pytest

from messaging_service import related_chats
from messaging_service.chat_queries import COURTESY_SCAN, list_chats
from test_query_reliability import ChatMeasured
from test_search_performance import scans, visited

CHATS = 2000
GAPS = 100
GAP = 300
BUSY = 100


@pytest.fixture(autouse=True)
def host_speed_apart(monkeypatch):
    """The statement plan is under test, not the host's speed (as in test_search_performance): related_chats' 0.3 s
    budget, spent on a slow runner in one measurement and not the other, dropped its trailing statements from that one
    and broke the plain-versus-exclude statement comparison (merge-group run 42612458422, `31 == 29 + 1`)."""
    monkeypatch.setattr(related_chats, 'DEADLINE_SECONDS', 120.0)


def own_statistics(c):
    """Every table the measured statements read gets its statistics now, the emptied ones included.

    The seeded fixture truncates the tables an earlier test filled; autovacuum may analyze them at any moment
    later, and an estimate that changes between the plain and the exclude measurement compares two plans made
    from different statistics (an extra of -63798 in CI). Analyzed here, a later autovacuum pass finds the same
    contents and changes nothing. ANALYZE reads every row (300 x 1000, above the catalogue's messages): from the
    default 30,000-row sample the plain and the exclude statement could each take the other of two near-equal plans
    (#885: extra 70204 > 63700)."""
    c.execute('SET LOCAL default_statistics_target=1000')
    c.execute('ANALYZE')


class Statements(ChatMeasured):
    """Plans the page statements (ChatMeasured) and the exclude count statement."""
    def __init__(self, c): super().__init__(c); self.sql = []; self.counts = []
    def execute(self, sql, args=None):
        self.sql.append(sql)
        if sql.startswith('SELECT count(*) AS n FROM') and 'closing_from' in sql:
            self.counts.append(self.connection.execute('EXPLAIN (ANALYZE,TIMING OFF,FORMAT JSON) ' + sql, args)
                .fetchone()['QUERY PLAN'][0])
        return super().execute(sql, args)


def courtesy_catalogue(s):
    """Polite chats: a request, the owner's answer, then a courtesy-only run of 19 (odd) or 25 (even).
    Gap chats: a request, the owner's answer, GAP stored empty incoming rows, then a courtesy.
    Busy chats: a request, GAP owner messages, then a courtesy."""
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='120s'")
        c.execute('''INSERT INTO chats(id,account,provider_chat,title,chat_type)
            SELECT 'polite-'||lpad(n::text,4,'0'),'telegram-demo',(60000+n)::text,'Synthetic polite','private'
            FROM generate_series(1,%s) n UNION ALL
            SELECT 'gap-'||lpad(n::text,4,'0'),'telegram-demo',(80000+n)::text,'Synthetic gap','private'
            FROM generate_series(1,%s) n UNION ALL
            SELECT 'busy-'||lpad(n::text,4,'0'),'telegram-demo',(85000+n)::text,'Synthetic busy','private'
            FROM generate_series(1,%s) n''', (CHATS, GAPS, BUSY))
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'polite-'||lpad(n::text,4,'0')||'-'||k,'telegram-demo','polite-'||lpad(n::text,4,'0'),k::text,
                CASE WHEN k=2 THEN 'fixture-owner' ELSE 'fixture-peer-'||n END,
                CASE k WHEN 1 THEN 'Synthetic request for the report' WHEN 2 THEN 'Synthetic answer' ELSE 'спасибо' END,
                now()-interval '1 day'+k*interval '1 minute',CASE WHEN k=2 THEN 'outgoing' ELSE 'incoming' END
            FROM generate_series(1,%s) n CROSS JOIN generate_series(1,27) k
            WHERE k<=CASE WHEN n%%2=1 THEN 21 ELSE 27 END UNION ALL
            SELECT 'gap-'||lpad(n::text,4,'0')||'-'||k,'telegram-demo','gap-'||lpad(n::text,4,'0'),k::text,
                CASE WHEN k=2 THEN 'fixture-owner' ELSE 'fixture-gap-'||n END,
                CASE WHEN k=1 THEN 'Synthetic request for the report' WHEN k=2 THEN 'Synthetic answer'
                  WHEN k=%s THEN 'спасибо' ELSE '' END,
                now()-interval '1 day'+k*interval '1 second',CASE WHEN k=2 THEN 'outgoing' ELSE 'incoming' END
            FROM generate_series(1,%s) n CROSS JOIN generate_series(1,%s) k UNION ALL
            SELECT 'busy-'||lpad(n::text,4,'0')||'-'||k,'telegram-demo','busy-'||lpad(n::text,4,'0'),k::text,
                CASE WHEN k IN (1,%s) THEN 'fixture-busy-'||n ELSE 'fixture-owner' END,
                CASE WHEN k=1 THEN 'Synthetic request for the report' WHEN k=%s THEN 'спасибо' ELSE 'Synthetic answer' END,
                now()-interval '1 day'+k*interval '1 second',CASE WHEN k IN (1,%s) THEN 'incoming' ELSE 'outgoing' END
            FROM generate_series(1,%s) n CROSS JOIN generate_series(1,%s) k''',
            (CHATS, GAP + 3, GAPS, GAP + 3, GAP + 2, GAP + 2, GAP + 2, BUSY, GAP + 2))
        own_statistics(c)


def measure(s, key, **kwargs):
    with s.authorized(key, 'list_chats') as (c, p):
        measured = Statements(c)
        page = list_chats(s, measured, p, limit=5, awaiting_reply=True, include_coverage=False, **kwargs)
    return page, measured


# spec: ANYM-810
def test_courtesies_exclude_reads_at_most_the_depth_per_chat(seeded, record_property):
    s, _, b = seeded
    courtesy_catalogue(s)
    every, plain = measure(s, b)
    shown, closing = measure(s, b, courtesies='exclude')
    # Odd chats find the request at depth 20 and leave; even chats find none within 20 and stay; a gap chat's
    # request lies past the stored rows read, and so do a busy chat's owner messages: both stay.
    left_out, _ = measure(s, b, courtesies='only')
    assert left_out['awaiting_reply_total'] == CHATS // 2 and left_out['items']
    assert all(r['id'].startswith('polite-') and int(r['id'][7:]) % 2 == 1 for r in left_out['items'])
    assert shown['excluded_courtesies_total'] == CHATS // 2
    assert shown['awaiting_reply_total'] + shown['excluded_courtesies_total'] == every['awaiting_reply_total']
    # The default plan is unchanged: no courtesy lookup and no extra statement; exclude adds only its count.
    assert not any('closing_from' in sql for sql in plain.sql)
    assert len(closing.sql) == len(plain.sql) + 1 and len(closing.counts) == 1
    # Per chat the lookups read at most the stored incoming rows (all 20 or 26 of a polite chat, COURTESY_SCAN of a
    # gap chat, 2 of a busy chat) and the owner's rows from X on (1 in an odd chat, COURTESY_SCAN+1 in a busy chat).
    # An unbounded read takes all GAP+1 gap rows and all GAP busy owner rows.
    bound = (CHATS // 2 * 21 + CHATS // 2 * 26 + GAPS * COURTESY_SCAN + BUSY * (2 + COURTESY_SCAN + 1)
        + 2 * (CHATS + GAPS + BUSY))
    base = visited(plain.plans[0]['Plan'], {'messages'})
    extra = visited(closing.plans[0]['Plan'], {'messages'}) - base
    counted = visited(closing.counts[0]['Plan'], {'messages'})
    record_property('extra_message_rows_visited', extra)
    record_property('count_message_rows_visited', counted)
    assert 0 < extra <= bound, scans(closing.plans[0]['Plan'])
    assert counted <= base + bound, scans(closing.counts[0]['Plan'])


# spec: ANYM-810
def test_the_catalogue_gets_the_same_statistics_on_every_analyze(seeded):
    """Regression (#885: extra 70204 > 63700): ANALYZE sampled 30,000 of the catalogue's messages, so the plain and
    the exclude page statement could each take the other of two near-equal plans for the same rows (85207 or 115105
    rows visited), and the difference counted as extra rows."""
    s, _, _ = seeded
    courtesy_catalogue(s)
    def statistics():
        with s.pool.connection() as c:
            own_statistics(c)
            return c.execute('''SELECT attname,null_frac,n_distinct,correlation,most_common_vals::text,
                histogram_bounds::text FROM pg_stats WHERE tablename='messages' ORDER BY attname''').fetchall()
    first = statistics()
    assert first and all(statistics() == first for _ in range(3))



# spec: ANYM-810
def test_a_slow_host_during_one_measurement_leaves_the_statement_comparison_unchanged(seeded, monkeypatch):
    """Regression (merge-group run 42612458422, `31 == 29 + 1`): a slow runner spent related_chats' budget
    (DEADLINE_SECONDS) during the plain measurement only, so its trailing statements were missing from that one."""
    s, _, b = seeded
    courtesy_catalogue(s)
    clock = SimpleNamespace(t=0.0, step=0.0)

    def monotonic():
        clock.t += clock.step
        return clock.t
    monkeypatch.setattr(related_chats, 'time', SimpleNamespace(monotonic=monotonic))
    clock.step = 0.2  # every related_chats clock read costs 0.2 s: the slow host
    _, plain = measure(s, b)
    clock.step = 0.0
    _, closing = measure(s, b, courtesies='exclude')
    steps = [q.split(' */')[0] for q in plain.sql if q.startswith('/* related_chats:')]
    assert steps == [q.split(' */')[0] for q in closing.sql if q.startswith('/* related_chats:')] and steps
    assert len(closing.sql) == len(plain.sql) + 1 and len(closing.counts) == 1

TIES = 100


def tied_catalogue(s):
    """WhatsApp and Telegram chats (nonnumeric IDs): a request, the owner's answer, then GAP courtesies at one second."""
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='120s'")
        c.execute('''INSERT INTO chats(id,account,provider_chat,title,chat_type)
            SELECT 'tie-wa-'||lpad(n::text,4,'0'),'whatsapp-demo-1',(20000000+n)||'@c.us','Synthetic tie','private'
            FROM generate_series(1,%s) n UNION ALL
            SELECT 'tie-tg-'||lpad(n::text,4,'0'),'telegram-demo',(90000+n)::text,'Synthetic tie','private'
            FROM generate_series(1,%s) n''', (TIES, TIES))
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'tie-'||side||'-'||lpad(n::text,4,'0')||'-'||k,CASE side WHEN 'wa' THEN 'whatsapp-demo-1' ELSE 'telegram-demo' END,
                'tie-'||side||'-'||lpad(n::text,4,'0'),'z'||k,CASE WHEN k=2 THEN 'fixture-owner' ELSE 'fixture-tie-'||n END,
                CASE k WHEN 1 THEN 'Synthetic request for the report' WHEN 2 THEN 'Synthetic answer' ELSE 'спасибо' END,
                date_trunc('second',now())-interval '1 day'+least(k,3)*interval '1 second',
                CASE WHEN k=2 THEN 'outgoing' ELSE 'incoming' END
            FROM generate_series(1,%s) n CROSS JOIN generate_series(1,%s) k CROSS JOIN unnest(ARRAY['wa','tg']) side''',
            (TIES, GAP + 2))
        own_statistics(c)


# spec: ANYM-810
def test_courtesy_tails_stay_bounded_on_timestamp_ties(seeded, record_property):
    s, a, _ = seeded
    tied_catalogue(s)
    every, plain = measure(s, a)
    shown, closing = measure(s, a, courtesies='exclude')
    # GAP courtesies share one second past the scan: the request cannot be proven X, so every tied chat stays.
    left_out, _ = measure(s, a, courtesies='only')
    assert not any(r['id'].startswith('tie-') for r in left_out['items'])
    assert shown['awaiting_reply_total'] + shown.get('excluded_courtesies_total', 0) == every['awaiting_reply_total']
    # Tied rows are read in the index's key order, never sorted as a whole timestamp group.
    bound = 2 * TIES * (COURTESY_SCAN + 2)
    extra = visited(closing.plans[0]['Plan'], {'messages'}) - visited(plain.plans[0]['Plan'], {'messages'})
    counted = visited(closing.counts[0]['Plan'], {'messages'})
    record_property('extra_message_rows_visited', extra)
    assert extra <= bound, scans(closing.plans[0]['Plan'])
    assert counted <= visited(plain.plans[0]['Plan'], {'messages'}) + bound, scans(closing.counts[0]['Plan'])
