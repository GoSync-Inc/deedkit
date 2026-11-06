"""ANYM-810: courtesies=exclude reads a bounded tail per chat over a large synthetic catalogue; no provider or real data."""
from messaging_service.chat_queries import COURTESY_DEPTH, list_chats
from test_query_reliability import ChatMeasured
from test_search_performance import scans, visited

CHATS = 2000


class Statements(ChatMeasured):
    def __init__(self, c): super().__init__(c); self.sql = []
    def execute(self, sql, args=None):
        self.sql.append(sql)
        return super().execute(sql, args)


def courtesy_catalogue(s):
    """Each chat: a request, the owner's answer, then a courtesy-only run of 19 (odd chats) or 25 (even chats)."""
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='120s'")
        c.execute('''INSERT INTO chats(id,account,provider_chat,title,chat_type)
            SELECT 'polite-'||lpad(n::text,4,'0'),'telegram-demo',(60000+n)::text,'Synthetic polite','private'
            FROM generate_series(1,%s) n''', (CHATS,))
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'polite-'||lpad(n::text,4,'0')||'-'||k,'telegram-demo','polite-'||lpad(n::text,4,'0'),k::text,
                CASE WHEN k=2 THEN 'fixture-owner' ELSE 'fixture-peer-'||n END,
                CASE k WHEN 1 THEN 'Synthetic request for the report' WHEN 2 THEN 'Synthetic answer' ELSE 'спасибо' END,
                now()-interval '1 day'+k*interval '1 minute',CASE WHEN k=2 THEN 'outgoing' ELSE 'incoming' END
            FROM generate_series(1,%s) n CROSS JOIN generate_series(1,27) k
            WHERE k<=CASE WHEN n%%2=1 THEN 21 ELSE 27 END''', (CHATS,))
        c.execute('ANALYZE messages'); c.execute('ANALYZE chats')


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
    # Odd chats find the request at depth 20 and leave; even chats find none within 20 and stay.
    left_out, _ = measure(s, b, courtesies='only')
    assert left_out['awaiting_reply_total'] == CHATS // 2 and left_out['items']
    assert all(r['id'].startswith('polite-') and int(r['id'][7:]) % 2 == 1 for r in left_out['items'])
    assert shown['excluded_courtesies_total'] == CHATS // 2
    assert shown['awaiting_reply_total'] + shown['excluded_courtesies_total'] == every['awaiting_reply_total']
    # The default plan is unchanged: no courtesy lookup and no extra statement; exclude adds only its count.
    assert not any('closing_from' in sql for sql in plain.sql)
    assert len(closing.sql) == len(plain.sql) + 1
    # The page statement reads every candidate chat. Its extra message rows per chat stay within the depth of
    # incoming messages plus the owner's messages after X: an unbounded read would take all 26 incoming.
    extra = visited(closing.plans[0]['Plan'], {'messages'}) - visited(plain.plans[0]['Plan'], {'messages'})
    record_property('extra_message_rows_visited', extra)
    bound = CHATS * COURTESY_DEPTH + CHATS // 2 * 1 + CHATS * 2
    assert 0 < extra <= bound, scans(closing.plans[0]['Plan'])
