"""Resource regressions: selective queries must not traverse the whole archive."""
from messaging_service.message_queries import search
from messaging_service.file_index import search_files


class Measured:
    def __init__(self,connection):self.connection=connection;self.plans=[];self.jit=[]
    def execute(self,sql,args=None):
        if sql.startswith('WITH search_query'):
            explained=self.connection.execute('EXPLAIN (ANALYZE,FORMAT JSON) '+sql,args).fetchone()['QUERY PLAN'][0]
            self.plans.append(explained['Plan']);self.jit.append(explained.get('JIT'))
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


# spec: ANYM-656
def test_related_chat_candidates_stay_on_the_text_index_when_a_date_walk_looks_cheaper(seeded,monkeypatch):
    from datetime import datetime,timezone
    from messaging_service import related_chats
    s,a,b=seeded
    monkeypatch.setattr(related_chats,'DEADLINE_SECONDS',120.0)  # the plan is under test, not the host's speed
    class Candidates:
        def __init__(self,connection):self.connection=connection;self.plans=[]
        def execute(self,sql,args=None):
            if 'related_chats:candidates' in sql:
                self.plans.append(self.connection.execute('EXPLAIN (ANALYZE,FORMAT JSON) '+sql,args).fetchone()['QUERY PLAN'][0]['Plan'])
            return self.connection.execute(sql,args)
        def __getattr__(self,name):return getattr(self.connection,name)
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='120s'")
        c.execute("""INSERT INTO chats(id,account,provider_chat,title,chat_type)
            SELECT 'busy-'||n,'telegram-demo','busy-'||n,'Busy '||n,'private' FROM generate_series(0,99) n
            UNION ALL SELECT 'fresh','telegram-demo','fresh','Fresh','private'""")
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'bulk-'||n,'telegram-demo','busy-'||(n%100),n::text,'owner','unique-token-'||n||'-end ordinary words around the lease',
                now()-n*interval '3 seconds','outgoing' FROM generate_series(1,20000) n""")
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            VALUES('fresh-1','telegram-demo','fresh','1','peer','unique-token-50-end ordinary words around the lease',now()-interval '1 second','incoming')""")
        c.execute('ANALYZE messages');c.execute('ANALYZE chats')
    with s.authorized(a,'list_chats') as (c,p):
        # Sorting priced out is the plan a run met by chance (#646, run 41953348270): the ordered date walk wins whenever it can exist.
        c.execute('SET LOCAL enable_sort=off');c.execute('SET LOCAL enable_seqscan=off')
        measured=Candidates(c)
        found=related_chats.hints(measured,p,[{'id':'fresh','type':'private','last_activity':datetime.now(timezone.utc)}])
    assert found['fresh']['related_chats'][0]['source_message_id']=='bulk-50'
    assert len(measured.plans)==1 and visited(measured.plans[0],{'messages'})<2000,[scans(plan) for plan in measured.plans]


# spec: ANYM-656
def test_related_chat_check_reads_bounded_messages_in_a_200k_archive(seeded,monkeypatch):
    from datetime import datetime,timezone
    from messaging_service import related_chats
    s,a,b=seeded
    # This test bounds the rows the check reads, not the host's speed; the 1.0 s budget has its own test below.
    monkeypatch.setattr(related_chats,'DEADLINE_SECONDS',120.0)
    class Explained:
        def __init__(self,connection):self.connection=connection;self.plans=[]
        def execute(self,sql,args=None):
            if 'related_chats:' in sql:
                self.plans.append(self.connection.execute('EXPLAIN (ANALYZE,FORMAT JSON) '+sql,args).fetchone()['QUERY PLAN'][0]['Plan'])
            return self.connection.execute(sql,args)
        def __getattr__(self,name):return getattr(self.connection,name)
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='120s'")
        # 200k messages over 1000 chats, as an archive spreads them (not one giant chat), all inside the 7-day window.
        c.execute("""INSERT INTO chats(id,account,provider_chat,title,chat_type)
            SELECT 'busy-'||n,'telegram-demo','busy-'||n,'Busy '||n,'private' FROM generate_series(0,999) n
            UNION ALL SELECT 'fresh','telegram-demo','fresh','Fresh','private'""")
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'bulk-'||n,'telegram-demo','busy-'||(n%1000),n::text,'owner','unique-token-'||n||'-end ordinary words around the lease',
                now()-n*interval '3 seconds','outgoing' FROM generate_series(1,200000) n""")
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            VALUES('fresh-1','telegram-demo','fresh','1','peer','unique-token-50-end ordinary words around the lease',now()-interval '1 second','incoming')""")
        c.execute('ANALYZE messages');c.execute('ANALYZE chats')
    with s.authorized(a,'list_chats') as (c,p):
        measured=Explained(c)
        found=related_chats.hints(measured,p,[{'id':'fresh','type':'private','last_activity':datetime.now(timezone.utc)}])
    assert found['fresh']['related_chats'][0]['source_message_id']=='bulk-50'
    assert len(measured.plans)==4 and sum(visited(plan,{'messages'}) for plan in measured.plans)<2000,[(visited(plan,{'messages'}),[(x['Node Type'],x.get('Index Name'),x.get('Actual Rows'),x.get('Rows Removed by Filter'),x.get('Actual Loops')) for x in scans(plan) if x.get('Relation Name')=='messages']) for plan in measured.plans]


# spec: ANYM-656
def test_related_chat_check_returns_no_hint_when_its_deadline_is_spent(seeded,monkeypatch,caplog):
    from datetime import datetime,timezone
    from messaging_service import related_chats
    s,a,b=seeded
    body='unique-token-end ordinary words around the lease'
    with s.pool.connection() as c:
        c.execute("""INSERT INTO chats(id,account,provider_chat,title,chat_type) VALUES
            ('older','telegram-demo','older','Older','private'),('fresh','telegram-demo','fresh','Fresh','private')""")
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction) VALUES
            ('older-1','telegram-demo','older','1','owner',%s,now()-interval '3 days','outgoing'),
            ('fresh-1','telegram-demo','fresh','1','peer',%s,now()-interval '1 day','incoming')""",(body,body))
    def check():
        with s.authorized(a,'list_chats') as (c,p):
            found=related_chats.hints(c,p,[{'id':'fresh','type':'private','last_activity':datetime.now(timezone.utc)}])
            assert c.execute('SELECT 1 AS alive').fetchone()['alive']==1  # the caller's transaction stays usable
        return found
    # The same rows give a hint while the budget lasts, so the {} below can only come from the deadline.
    monkeypatch.setattr(related_chats,'DEADLINE_SECONDS',120.0)
    assert check()['fresh']['related_chats'][0]['source_message_id']=='older-1'
    monkeypatch.setattr(related_chats,'DEADLINE_SECONDS',0)
    with caplog.at_level('WARNING',logger=related_chats.log.name):
        assert check()=={}
    assert [r.getMessage() for r in caplog.records if r.name==related_chats.log.name]==['related_chats_abandoned Deadline']


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


# spec: ANYM-501
def test_default_channel_exclusion_reads_matches_through_text_indexes(seeded):
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='60s'")
        c.execute('''INSERT INTO chats(id,account,provider_chat,title,chat_type)
            SELECT 'people-'||n,'telegram-demo','people-'||n,'Synthetic people',CASE WHEN n%2=0 THEN 'private' ELSE 'group' END
            FROM generate_series(1,400) n''')
        # 20 of 8,000 messages in 400 private and group chats carry the term.
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            SELECT 'people-post-'||n,'telegram-demo','people-'||(n%400+1),n::text,'fixture',
                CASE WHEN n%400=7 THEN 'scarcelexicalneedle ' ELSE '' END||repeat('ordinary private content ',5),
                now()+n*interval '1 second'
            FROM generate_series(1,8000) n''')
        c.execute('ANALYZE messages');c.execute('ANALYZE chats')
        for index in ('message_fts','message_fts_english','message_fts_indonesian','message_fts_russian'):
            c.execute('SELECT gin_clean_pending_list(%s::regclass)',(index,))
    with s.authorized(b,'search') as (c,p):
        c.execute('SET LOCAL enable_seqscan=off')
        measured=Measured(c)
        result=search(s,measured,p,query='scarcelexicalneedle',include_channels=False)
    assert len(result['items'])==20
    # coalesce(chat_type) estimated ~1.5% of the chats, so the plan walked every message of all 400 chats (8,000 rows).
    assert measured.plans and sum(visited(plan,{'messages'}) for plan in measured.plans)<500, [scans(plan) for plan in measured.plans]


# spec: ANYM-663
def test_people_yo_ye_fold_query_reads_no_more_names_than_the_literal_query(seeded):
    from messaging_service.people_candidates import search_people
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='60s'")
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            SELECT 'fold-post-'||n,'telegram-demo','telegram-demo-chat','fold-'||n,'fold-sender-'||n,'x',now()+n*interval '1 second'
            FROM generate_series(1,4000) n''')
        c.execute('''INSERT INTO sender_profiles(id,account,chat,sender,display_name,source_message,source_sent_at)
            SELECT 'fold-profile-'||n,'telegram-demo','telegram-demo-chat','fold-sender-'||n,
                CASE WHEN n%1000=7 THEN 'Фёдор Синтетика' ELSE 'Имя'||n||' Фамилия'||n*7 END,'fold-post-'||n,now()
            FROM generate_series(1,4000) n''')
        c.execute('ANALYZE sender_profiles');c.execute('ANALYZE messages')
    class People(Measured):
        def execute(self,sql,args=None):
            if 'observed AS MATERIALIZED' in sql and not sql.startswith('EXPLAIN'):
                self.plans.append(self.connection.execute('EXPLAIN (ANALYZE,FORMAT JSON) '+sql,args).fetchone()['QUERY PLAN'][0]['Plan'])
            return self.connection.execute(sql,args)
    def plans(query):
        with s.authorized(b,'search_people') as (c,p):
            c.execute('SET LOCAL enable_seqscan=off')
            measured=People(c)
            result=search_people(s,measured,p,query=query)
        return result,measured.plans
    # Today's LIKE path is the baseline: its OR with the identity cache keeps it from the GIN too (card candidate),
    # so the fold must read no more rows than it does and return the same rows.
    folded,fold_plans=plans('Федор');literal,literal_plans=plans('Фамилия7000')
    assert [r['match_type'] for r in folded['items']]==['literal_folded']*4
    assert len(literal['items'])==1 and fold_plans and literal_plans
    read=lambda found:sum(visited(plan,{'sender_profiles'}) for plan in found)
    assert read(fold_plans)<=read(literal_plans),[scans(plan) for plan in fold_plans]


# spec: ANYM-501
def test_message_search_runs_without_jit_compilation(seeded):
    s,a,b=seeded
    with s.authorized(b,'search') as (c,p):
        # Every plan crosses the threshold, as a common word over a large archive does.
        c.execute('SET LOCAL jit_above_cost=0')
        measured=Measured(c)
        search(s,measured,p,query='synthetic')
    assert measured.jit and measured.jit==[None]*len(measured.jit), measured.jit


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


# spec: ANYM-661
def test_file_media_and_ready_filters_keep_absent_query_candidates_bounded(seeded):
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='60s'")
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            SELECT 'caption-filter-'||n,'telegram-demo','telegram-demo-chat',n::text,'fixture',
                repeat('ordinary caption content ',5),now()+n*interval '1 second'
            FROM generate_series(1,4000) n''')
        c.execute('''INSERT INTO attachments(id,message,digest,filename,mime,bytes)
            SELECT 'file-filter-'||n,'demo-tg-1',repeat('2',64),'ordinary.ogg','audio/ogg',1
            FROM generate_series(1,400) n''')
        c.execute("UPDATE attachment_text SET state='ready',text_content='ordinary content' WHERE attachment LIKE 'file-filter-%'")
        for table in ('messages','attachments','attachment_text','attachment_text_chunks','attachment_enrichment','attachment_enrichment_chunks'):
            c.execute('ANALYZE '+table)
        indexes=c.execute("SELECT indexname FROM pg_indexes WHERE tablename=ANY(%s) AND indexdef LIKE '%%USING gin%%'",
            (['messages','attachment_text','attachment_text_chunks','attachment_enrichment','attachment_enrichment_chunks'],)).fetchall()
        for index in indexes:c.execute('SELECT gin_clean_pending_list(%s::regclass)',(index['indexname'],))
    with s.authorized(b,'search_files') as (c,p):
        c.execute('SET LOCAL enable_seqscan=off')
        measured=Measured(c)
        result=search_files(s,measured,p,query='absentlexicalneedleqxz',media_kind=['voice','audio'],ready=['transcript','description'])
    assert result['items']==[]
    # The same bound as without the filters: the query candidates are found through the text indexes first.
    assert measured.plans and sum(visited(plan,{'attachment_text','attachments','attachment_text_chunks',
        'attachment_enrichment','attachment_enrichment_chunks','messages'}) for plan in measured.plans)<100, [scans(plan) for plan in measured.plans]


def nodes(node):
    return [node]+sum((nodes(child) for child in node.get('Plans',[])),[])


# spec: ANYM-579
def test_common_caption_file_query_joins_caption_candidates(seeded):
    from test_file_index import add_file
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='60s'")
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            SELECT 'caption-common-'||n,'telegram-demo','telegram-demo-chat','caption-common-'||n,'fixture',
                'commoncaptionneedle report',now()+n*interval '1 second'
            FROM generate_series(1,3000) n''')
        # Distinct digests: a shared digest would let attachment_occurrences seek by message.
        c.execute('''INSERT INTO attachments(id,message,digest,filename,mime,bytes)
            SELECT 'caption-file-'||n,'caption-common-'||n,md5(n::text)||md5('d'||n),'scan.jpg','image/jpeg',1
            FROM generate_series(2,3000) n''')
    add_file(s,'caption-visible','caption-common-1',b'synthetic caption scan','scan.jpg','image/jpeg')
    with s.pool.connection() as c:
        for table in ('messages','attachments','attachment_text'):c.execute('ANALYZE '+table)
    with s.authorized(b,'search_files') as (c,p):
        measured=Measured(c)
        result=search_files(s,measured,p,query='commoncaptionneedle')
    assert [item['attachment_id'] for item in result['items']]==['caption-visible']
    assert result['items'][0]['match_sources']==['message_context']
    found=[node for plan in measured.plans for node in nodes(plan) if node.get('Relation Name')=='attachments']
    # A per-row array compare costs attachments x caption matches (5.9 s in production, ANYM-579).
    assert not [node for node in found if 'message = ANY' in node.get('Filter','')+node.get('Index Cond','')], [scans(plan) for plan in measured.plans]
    caption_scans=[node for node in found if node.get('Alias')=='ca']
    assert caption_scans and all(node.get('Actual Loops')==1 for node in caption_scans), caption_scans
