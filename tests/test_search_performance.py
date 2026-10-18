"""Resource regressions: selective queries must not traverse the whole archive."""
from messaging_service.message_queries import search
from messaging_service.file_index import search_files


class Measured:
    def __init__(self,connection):self.connection=connection;self.plans=[]
    def execute(self,sql,args=None):
        if sql.startswith('WITH search_query'):
            self.plans.append(self.connection.execute('EXPLAIN (ANALYZE,FORMAT JSON) '+sql,args)
                .fetchone()['QUERY PLAN'][0]['Plan'])
        return self.connection.execute(sql,args)
    def __getattr__(self,name):return getattr(self.connection,name)


def visited(node,relations):
    own=(node.get('Actual Rows',0)+node.get('Rows Removed by Filter',0))*node.get('Actual Loops',1)
    return (own if node.get('Relation Name') in relations else 0)+sum(visited(child,relations) for child in node.get('Plans',[]))


def scans(node):
    own=[{k:node[k] for k in ('Node Type','Relation Name','Index Name','Actual Rows','Rows Removed by Filter','Actual Loops','Actual Total Time','Plan Rows','Total Cost') if k in node}] if node.get('Relation Name') else []
    return own+sum((scans(child) for child in node.get('Plans',[])),[])


def ordered_visits(node):
    # Bulk bitmap/seq scans can legitimately win on small tables or a large
    # GIN pending list. The regression is a LIMIT-driven ordered index walk
    # that expects early matches but visits the whole archive when none exist.
    own=visited({**node,'Plans':[]},{'messages'}) if node.get('Node Type') in ('Index Scan','Index Only Scan') and node.get('Index Name') in (
        'message_live_date','message_scope_date','message_live_sender_date') else 0
    return own+sum(ordered_visits(child) for child in node.get('Plans',[]))


def test_absent_lexical_query_reads_bounded_message_candidates(seeded):
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='60s'")
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            SELECT 'background-'||n,'telegram-demo','telegram-demo-chat',n::text,'fixture',
                repeat('ordinary conversation content ',5),now()+n*interval '1 second'
            FROM generate_series(1,4000) n''')
        c.execute('ANALYZE messages')
    with s.authorized(b,'search') as (c,p):
        # Small synthetic tables otherwise legitimately favour a sequential
        # scan. Exercise the indexed path that a large archive relies on.
        c.execute('SET LOCAL enable_seqscan=off')
        measured=Measured(c)
        result=search(s,measured,p,query='absentlexicalneedleqxz')
    assert result['items']==[]
    assert measured.plans and sum(ordered_visits(plan) for plan in measured.plans)<100, [scans(plan) for plan in measured.plans]


# spec: ANYM-272
def test_default_channel_exclusion_keeps_absent_query_candidates_bounded(seeded):
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='60s'")
        c.execute('''INSERT INTO chats(id,account,provider_chat,title,chat_type)
            SELECT 'news-'||n,'telegram-demo','news-'||n,'Synthetic news','channel' FROM generate_series(1,40) n''')
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            SELECT 'news-post-'||n,'telegram-demo','news-'||(n%40+1),n::text,'fixture',
                repeat('ordinary channel content ',5),now()+n*interval '1 second'
            FROM generate_series(1,4000) n''')
        c.execute('ANALYZE messages');c.execute('ANALYZE chats')
    with s.authorized(b,'search') as (c,p):
        c.execute('SET LOCAL enable_seqscan=off')
        measured=Measured(c)
        result=search(s,measured,p,query='absentlexicalneedleqxz',include_channels=False)
    assert result['items']==[]
    assert measured.plans and sum(ordered_visits(plan) for plan in measured.plans)<100, [scans(plan) for plan in measured.plans]


def test_absent_file_query_reads_bounded_file_candidates(seeded):
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='60s'")
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            SELECT 'caption-background-'||n,'telegram-demo','telegram-demo-chat',n::text,'fixture',
                repeat('ordinary caption content ',5),now()+n*interval '1 second'
            FROM generate_series(1,4000) n''')
        c.execute('''INSERT INTO attachments(id,message,digest,filename,mime,bytes)
            SELECT 'file-background-'||n,'demo-tg-1',repeat('1',64),'ordinary.txt','text/plain',1
            FROM generate_series(1,400) n''')
        c.execute("UPDATE attachment_text SET state='ready',text_content='ordinary content' WHERE attachment LIKE 'file-background-%'")
        for table in ('messages','attachments','attachment_text','attachment_text_chunks','attachment_enrichment','attachment_enrichment_chunks'):
            c.execute('ANALYZE '+table)
        indexes=c.execute("SELECT indexname FROM pg_indexes WHERE tablename=ANY(%s) AND indexdef LIKE '%%USING gin%%'",
            (['messages','attachment_text','attachment_text_chunks','attachment_enrichment','attachment_enrichment_chunks'],)).fetchall()
        for index in indexes:c.execute('SELECT gin_clean_pending_list(%s::regclass)',(index['indexname'],))
    with s.authorized(b,'search_files') as (c,p):
        c.execute('SET LOCAL enable_seqscan=off')
        measured=Measured(c)
        result=search_files(s,measured,p,query='absentlexicalneedleqxz')
    assert result['items']==[]
    assert measured.plans and sum(visited(plan,{'attachment_text','attachments','attachment_text_chunks',
        'attachment_enrichment','attachment_enrichment_chunks','messages'}) for plan in measured.plans)<100, [scans(plan) for plan in measured.plans]
