"""Synthetic large-catalogue reply queue regressions; no provider or real data."""
import pytest

from messaging_service.chat_queries import list_chats
from test_query_reliability import ChatMeasured
from test_search_performance import scans, visited


class QueueMeasured(ChatMeasured):
    def __init__(self,c):super().__init__(c);self.counts=0
    def execute(self,sql,args=None):
        self.counts+=sql.startswith('SELECT count(*) AS n,count(*) FILTER')
        return super().execute(sql,args)


def quiet_catalogue(s, quiet=200, active=3):
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='60s'")
        c.execute('''INSERT INTO chats(id,account,provider_chat,title,chat_type)
            SELECT 'quiet-'||lpad(n::text,4,'0'),'telegram-demo',(50000+n)::text,'Synthetic quiet','private'
            FROM generate_series(1,%s) n UNION ALL
            SELECT 'active-'||n,'telegram-demo',(90000+n)::text,'Synthetic active','private'
            FROM generate_series(1,%s) n''',(quiet,active))
        # Quiet chats end with an old incoming question; active ones are recent.
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'quiet-'||lpad(n::text,4,'0')||'-'||k,'telegram-demo','quiet-'||lpad(n::text,4,'0'),k::text,
                'fixture-'||n,'Synthetic old question?',now()-interval '30 days'+k*interval '1 minute',
                CASE WHEN k%%3=0 THEN 'outgoing' ELSE 'incoming' END
            FROM generate_series(1,%s) n CROSS JOIN generate_series(1,20) k WHERE k<>18 UNION ALL
            SELECT 'active-'||n||'-'||k,'telegram-demo','active-'||n,k::text,'fixture-active-'||n,
                'Synthetic recent question?',now()-interval '2 hours'+(n*10+k)*interval '1 second','incoming'
            FROM generate_series(1,%s) n CROSS JOIN generate_series(1,3) k''',(quiet,active))
        c.execute('ANALYZE messages');c.execute('ANALYZE chats')


@pytest.mark.parametrize('view',['archive','conversation'])
def test_reply_queue_skips_quiet_chats_before_head_lookups(seeded,record_property,view):
    s,a,b=seeded
    quiet_catalogue(s)
    with s.authorized(b,'list_chats') as (c,p):
        measured=QueueMeasured(c)
        page=list_chats(s,measured,p,limit=2,awaiting_reply=True,include_coverage=False,view=view)
    assert [row['id'] for row in page['items']]==['active-3','active-2']
    assert page['awaiting_reply_total']==3 and page['read_state_unknown_total']==3
    # One pass sorts and counts the first page; conversation heads are per row.
    assert measured.counts==0
    visits=visited(measured.plans[0]['Plan'],{'messages'})
    record_property('message_rows_visited',visits)
    # Quiet chats still match an old incoming message, but none can be within
    # the attention window, so their head lookups must not run at all.
    assert visits<60,scans(measured.plans[0]['Plan'])
    with s.authorized(b,'list_chats') as (c,p):
        rest=list_chats(s,c,p,limit=2,awaiting_reply=True,include_coverage=False,view=view,after=page['next_cursor'])
    assert [row['id'] for row in rest['items']]==['active-1']
    assert rest['awaiting_reply_total']==3 and rest['next_cursor'] is None


def test_reply_queue_without_age_limit_keeps_old_attention(seeded):
    s,a,b=seeded
    quiet_catalogue(s,quiet=4,active=1)
    page=s.list_chats(b,awaiting_reply=True,awaiting_reply_days=0,include_coverage=False,limit=10)
    assert {row['id'] for row in page['items']}=={'active-1','quiet-0001','quiet-0002','quiet-0003','quiet-0004'}
    assert page['awaiting_reply_total']==5
    unread=s.list_chats(b,awaiting_reply=True,only_unread=True,include_coverage=False)
    assert unread['items']==[] and unread['awaiting_reply_total']==0 and unread['read_state_unknown_total']==1


def test_reply_priority_reasons_match_the_actual_waiting_time(seeded):
    s,a,b=seeded
    quiet_catalogue(s,quiet=1,active=1)
    rows={row['id']:row for row in s.list_chats(b,awaiting_reply=True,awaiting_reply_days=0,
        order='priority',include_coverage=False)['items']}
    recent,old=rows['active-1'],rows['quiet-0001']
    # ANYM-276: the active chats have no owner message, so the one-way demotion (-60) applies; the old chat has one.
    assert recent['awaiting_reply_waiting_hours']==1 and recent['awaiting_reply_priority']==66
    assert recent['awaiting_reply_priority_reasons']==['private_chat','question_mark','waiting_age','one_way_chat']
    assert old['awaiting_reply_waiting_hours']>=700 and old['awaiting_reply_priority']==145
    assert old['awaiting_reply_priority_reasons']==['private_chat','question_mark','waiting_age_capped']


def test_rejected_search_parameters_explain_the_format_instead_of_narrowing():
    from messaging_service.error_details import payload
    from messaging_service.store import APIError
    date=payload(APIError('invalid_utc_date'),'search')
    assert date['next_action']=='check_parameters' and 'fallback' not in date
    assert date['expected'].startswith('ISO 8601') and date['results_state']=='not_returned_due_to_error'
    assert payload(APIError('invalid_limit'),'list_chats')['expected'].startswith('integer from 1 to 100')
    slow=payload(APIError('query_timeout'),'search')
    assert slow['next_action']=='narrow_account_chat_or_date_filters' and slow['retryable'] is True
