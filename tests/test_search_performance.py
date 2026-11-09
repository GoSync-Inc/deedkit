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
    # This test bounds the rows the check reads, not the host's speed; the hint budget (0.3 s, ANYM-878) has its own test below.
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


def seed_catalog(s):
    """1,000 chats: 700 Telegram chats with interleaved histories, 150 verified WhatsApp LID/PN pairs (4 observations each)."""
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='120s'")
        c.execute("""INSERT INTO chats(id,account,provider_chat,title,chat_type)
            SELECT 'pf-'||n,'telegram-demo','pf-'||n,'Synthetic '||n,CASE WHEN n%5=0 THEN 'group' ELSE 'private' END FROM generate_series(0,699) n
            UNION ALL SELECT 'pf-lid-'||n,'whatsapp-demo-1',(910000+n)::text||'@lid',(910000+n)::text||'@lid','private' FROM generate_series(0,149) n
            UNION ALL SELECT 'pf-pn-'||n,'whatsapp-demo-1',(710000+n)::text||'@c.us','Peer '||n,'private' FROM generate_series(0,149) n""")
        # Message k of chat n is k*1000+n minutes old, so every chat keeps older messages below every later chat's head.
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'pf-m-'||n||'-'||k,'telegram-demo','pf-'||n,(n*10+k)::text,CASE WHEN k%2=0 THEN 'peer' ELSE 'owner' END,
              'synthetic text '||n,now()-((k*1000+n*1.4)::int)*interval '1 minute',CASE WHEN k%2=0 THEN 'incoming' ELSE 'outgoing' END
            FROM generate_series(0,699) n,generate_series(0,5) k
            UNION ALL SELECT 'pf-w-'||n||'-'||k,'whatsapp-demo-1',CASE WHEN k<4 THEN 'pf-lid-'||n ELSE 'pf-pn-'||n END,'wamid-'||n||'-'||k,
              CASE WHEN k<4 THEN (910000+n)::text||'@lid' ELSE 'owner' END,'synthetic text '||n,
              now()-((k*1000+n*7+3)::int)*interval '1 minute',CASE WHEN k<4 THEN 'incoming' ELSE 'outgoing' END
            FROM generate_series(0,149) n,generate_series(0,5) k""")
        c.execute("""INSERT INTO whatsapp_identity_observations(account,source_message,identity_id,pn,lid,provenance,first_observed_at,observed_at)
            SELECT 'whatsapp-demo-1','pf-w-'||n||'-'||k,(910000+n)::text||'@lid',(710000+n)::text||'@c.us',(910000+n)::text||'@lid',
              'webjs_contact_cache_v1',now(),now() FROM generate_series(0,149) n,generate_series(0,3) k""")
        for table in ('messages','chats','whatsapp_identity_observations'):c.execute('ANALYZE '+table)


class Plans:
    """EXPLAIN (ANALYZE) of every read a list_chats call makes, with the function that issued it."""
    def __init__(self,connection):self.connection=connection;self.plans=[]
    def execute(self,sql,args=None):
        import traceback
        if sql.lstrip().startswith(('SELECT','WITH')):
            chain=[f.name for f in traceback.extract_stack()[:-1] if '/messaging_service/' in f.filename]
            plan=self.connection.execute('EXPLAIN (ANALYZE,FORMAT JSON) '+sql,args).fetchone()['QUERY PLAN'][0]['Plan']
            self.plans.append((chain,plan))
        return self.connection.execute(sql,args)
    def __getattr__(self,name):return getattr(self.connection,name)


def head_loops(plans):
    """The most times any one messages scan ran in the page's own statements (a per-chat head read runs once per chat)."""
    own=[plan for chain,plan in plans if chain and chain[-1] in ('list_chats','_list_chats','_page_first','_alias_pairs')]
    # The page statement itself must be among them, or a bound would pass on nothing.
    assert any(chain and chain[-1] in ('list_chats','_list_chats') and 'chat_page' in str(plan) for chain,plan in plans),[c[-1:] for c,_ in plans]
    return max((node.get('Actual Loops',1) for plan in own for node in nodes(plan) if node.get('Relation Name')=='messages'),default=0)


def listed(s,token,indexed=False,**kw):
    from messaging_service.chat_queries import list_chats
    with s.authorized(token,'list_chats') as (c,p):
        # A 600-row table legitimately favours a sequential scan; exercise the indexed path a large archive relies on.
        if indexed:c.execute('SET LOCAL enable_seqscan=off')
        measured=Plans(c)
        result=list_chats(s,measured,p,kw.pop('after',''),25,order='activity',view='conversation',include_coverage=False,**kw)
    return result,measured.plans


# spec: ANYM-878
def test_default_activity_page_reads_heads_for_the_page_not_the_catalog(seeded):
    s,a,b=seeded
    seed_catalog(s)
    limit=25
    restricted=s.issue_key('page-first-ten',['telegram-demo'],chats=['pf-'+str(n) for n in range(0,100,10)])
    cases={'owner':listed(s,a),'restricted':listed(s,restricted),'account':listed(s,a,accounts=['telegram-demo'])}
    first,_=cases['owner']
    cases['page 2']=listed(s,a,after=first['next_cursor'])
    loops={name:head_loops(plans) for name,(_,plans) in cases.items()}
    assert first['items'][0]['id']=='pf-0',first['items'][:3]
    assert len(cases['restricted'][0]['items'])==10
    # Today's plan read a head per catalog chat (1,000); the page plan reads at most 2x(limit+1), a restricted key at most its 10 chats.
    assert loops['restricted']<=10 and all(loops[name]<=2*(limit+1) for name in ('owner','account','page 2')),loops
    # A filter that recent messages never meet examines a bounded index range before the fallback, not the archive.
    from messaging_service.chat_queries import PAGE_WINDOW_CAP
    empty,plans=listed(s,a,chat_types=['channel'])
    examined=sum(visited(plan,{'messages'}) for chain,plan in plans if chain and chain[-1]=='_page_first')
    assert empty['items']==[] and 0<examined<=PAGE_WINDOW_CAP,examined


class ViewStatements:
    """Counts the statements the conversation view issues (the per-chat function or the page's batch)."""
    def __init__(self,connection):self.connection=connection;self.view=0
    def execute(self,sql,args=None):
        import traceback
        if any(f.name in ('conversation_state','conversation_states') for f in traceback.extract_stack()[:-1]
                if '/messaging_service/' in f.filename):self.view+=1
        return self.connection.execute(sql,args)
    def __getattr__(self,name):return getattr(self.connection,name)


# ANYM-889: the per-chat reads ran 321 statements for 25 rows and 1,285 for 100; the page batch runs 18 for both
# (worst case: fallbacks on both platforms, reply targets, conflict classes add a few, never per row).
CONVERSATION_VIEW_STATEMENTS = 24


# spec: ANYM-889
def test_conversation_view_runs_a_fixed_number_of_statements_for_25_and_100_rows(seeded):
    from messaging_service.chat_queries import list_chats
    from test_file_index import add_file, worker_for
    from test_file_retention_api import managed_absence
    s,a,_=seeded
    seed_catalog(s)
    # A text file on each of the 60 newest Telegram heads (all on both pages' rows or the 100-row page), each original
    # gone: lost on disk (no excerpt) or by a completed managed eviction (excerpt shown). The presence rule reads no
    # statement per file.
    digests=[add_file(s,'pf-file-'+str(n),'pf-m-'+str(n)+'-0',('Synthetic note '+str(n)).encode(),'note.txt') for n in range(60)]
    worker=worker_for(s)
    for _ in range(200):
        if not worker.run_once()['claimed']:break
    for n,digest in enumerate(digests):
        if n%2:(s.files/digest).unlink()
        else:managed_absence(s,digest,len(('Synthetic note '+str(n)).encode()))
    counts={}
    for limit in (25,100):
        with s.authorized(a,'list_chats') as (c,p):
            counted=ViewStatements(c)
            result=list_chats(s,counted,p,'',limit,order='activity',view='conversation',include_coverage=False)
        assert len(result['items'])==limit and all(row['conversation'] for row in result['items'])
        assert {row['type'] for row in result['items']}>={'private','group'}
        quoted={row['id']:(row['conversation']['latest_known_meaningful_message'] or {}).get('display_text_media')
            for row in result['items']}
        # The managed-evicted original's text is quoted in its head; the lost one's is not.
        assert quoted['pf-0'][0]['attachment_id']=='pf-file-0' and quoted['pf-1'] is None,(quoted['pf-0'],quoted['pf-1'])
        counts[limit]=counted.view
    assert 0<counts[25]<=CONVERSATION_VIEW_STATEMENTS and 0<counts[100]<=CONVERSATION_VIEW_STATEMENTS,counts


class AttentionPlans:
    """EXPLAIN (ANALYZE) of every statement that reads group attention, with the function that issued it."""
    def __init__(self,connection):self.connection=connection;self.plans=[]
    def execute(self,sql,args=None):
        import traceback
        if 'thread_messages AS MATERIALIZED' in sql:
            chain=[f.name for f in traceback.extract_stack()[:-1] if '/messaging_service/' in f.filename]
            plan=self.connection.execute('EXPLAIN (ANALYZE,FORMAT JSON) '+sql,args).fetchone()['QUERY PLAN'][0]['Plan']
            self.plans.append((chain[-1] if chain else 'test',plan))
        return self.connection.execute(sql,args)
    def __getattr__(self,name):return getattr(self.connection,name)


ATTENTION_CTES=('CTE attention_window','CTE attention_candidates','CTE thread_messages')


def attention_reads(node,inside=False):
    """Rows the group-attention CTEs emit from messages and message_metadata scans (actual rows x loops)."""
    inside=inside or node.get('Subplan Name') in ATTENTION_CTES
    own=node.get('Actual Rows',0)*node.get('Actual Loops',1) if inside and node.get('Relation Name') in ('messages','message_metadata') else 0
    return own+sum(attention_reads(child,inside) for child in node.get('Plans',[]))


def seed_attention_groups(s):
    """ANYM-915 criterion 1. Shape A: a 50,000-message group (Telegram and WhatsApp) whose owner wrote only before all
    of it; 40% replies to other incoming messages, 3 replies to the owner, 5 mentions; 400 boolean-key rows elsewhere.
    Shape B: an owner-active Telegram group (2,000 owner messages, the last before the newest 100 incoming)."""
    wa='120363000000000001@g.us'
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='120s'")
        c.execute("""INSERT INTO chats(id,account,provider_chat,title,chat_type) VALUES
            ('ga-tg','telegram-demo','-100915','Synthetic busy group','group'),
            ('ga-wa','whatsapp-demo-1',%s,'Synthetic busy group','group'),
            ('gb-tg','telegram-demo','-100916','Synthetic owner group','group')""",(wa,))
        c.execute("""INSERT INTO chats(id,account,provider_chat,title,chat_type)
            SELECT 'gn-'||n,CASE WHEN n%2=0 THEN 'telegram-demo' ELSE 'whatsapp-demo-1' END,
              CASE WHEN n%2=0 THEN '-10092'||n ELSE '1203630000000009'||n||'@g.us' END,'Synthetic noise '||n,'group'
            FROM generate_series(0,19) n""")
        # Shape A. The owner's only message is older than every incoming one.
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction) VALUES
            ('ga-tg-o','telegram-demo','ga-tg','1','owner','Synthetic owner note',now()-interval '30 days','outgoing'),
            ('ga-wa-o','whatsapp-demo-1','ga-wa','true_'||%s||'_OWNERSTANZA','owner','Synthetic owner note',
              now()-interval '30 days','outgoing')""",(wa,))
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'ga-tg-'||n,'telegram-demo','ga-tg',(n+1)::text,'peer-'||(n%%40),'Synthetic chatter '||n,
              now()-(50001-n)*interval '1 second','incoming' FROM generate_series(1,50000) n
            UNION ALL SELECT 'ga-wa-'||n,'whatsapp-demo-1','ga-wa','false_'||%s||'_S'||n||'_'||(n%%40)||'@lid',
              'peer-'||(n%%40),'Synthetic chatter '||n,now()-(50001-n)*interval '1 second','incoming'
            FROM generate_series(1,50000) n""",(wa,))
        c.execute("""INSERT INTO message_metadata(message,revision,metadata)
            SELECT 'ga-'||p||'-'||n,1,CASE
              WHEN n IN (100,20000,49990) THEN jsonb_build_object('reply_to_provider_id',CASE WHEN p='tg' THEN '1' ELSE 'OWNERSTANZA' END)
              WHEN n IN (7,3001,25003,40009,49999) THEN '{"mentions_owner":true}'::jsonb
              ELSE jsonb_build_object('reply_to_provider_id',CASE WHEN p='tg' THEN n::text ELSE 'S'||(n-1) END) END
            FROM generate_series(1,50000) n,unnest(ARRAY['tg','wa']) p
            WHERE n IN (100,20000,49990,7,3001,25003,40009,49999) OR (n%5 IN (0,1) AND n>=5)""")
        # 400 boolean-key rows in 20 other groups of both accounts.
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'gn-'||n||'-'||k,CASE WHEN n%2=0 THEN 'telegram-demo' ELSE 'whatsapp-demo-1' END,'gn-'||n,
              CASE WHEN n%2=0 THEN k::text ELSE 'false_1203630000000009'||n||'@g.us_N'||k||'_1@lid' END,'peer','Synthetic noise '||k,
              now()-k*interval '1 minute','incoming' FROM generate_series(0,19) n,generate_series(1,20) k""")
        c.execute("""INSERT INTO message_metadata(message,revision,metadata)
            SELECT 'gn-'||n||'-'||k,1,CASE WHEN k%2=0 THEN '{"mentions_owner":true}'::jsonb ELSE '{"reply_to_owner":true}'::jsonb END
            FROM generate_series(0,19) n,generate_series(1,20) k""")
        # Shape B: 2,000 owner messages interleaved with 2,000 incoming, then the newest 100 incoming.
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'gb-tg-'||t,'telegram-demo','gb-tg',t::text,CASE WHEN t<=4000 AND t%2=0 THEN 'owner' ELSE 'peer-'||(t%30) END,
              'Synthetic talk '||t,now()-(4101-t)*interval '1 minute',
              CASE WHEN t<=4000 AND t%2=0 THEN 'outgoing' ELSE 'incoming' END FROM generate_series(1,4100) t""")
        c.execute("""INSERT INTO message_metadata(message,revision,metadata)
            SELECT 'gb-tg-'||t,1,CASE WHEN t IN (4030,4077) THEN '{"mentions_owner":true}'::jsonb
              ELSE jsonb_build_object('reply_to_provider_id',CASE WHEN t>4000 THEN '3998' ELSE (t-1)::text END) END
            FROM generate_series(1,4100) t WHERE t IN (4030,4077,4050) OR (t<=4000 AND t%2=1 AND t%13 IN (1,2))""")
        c.execute('ANALYZE messages');c.execute('ANALYZE message_metadata');c.execute('ANALYZE chats')


def forget_attention_groups(s):
    """Leave the shared database's statistics as the fixture's baseline: the 100k seeded rows were ANALYZEd, and a
    later plan test in the same shard must not plan its small tables from those numbers (#748)."""
    with s.pool.connection() as c:
        c.execute('TRUNCATE accounts,principals,api_key_operations,feed_counter,audit_log CASCADE')
        c.execute('INSERT INTO feed_counter(id) VALUES(1)')
    s.seed()
    with s.pool.connection() as c:
        c.execute('ANALYZE messages');c.execute('ANALYZE message_metadata');c.execute('ANALYZE chats')


def attention_ids(c,account,chat,provider_chat,platform,query):
    return [r['id'] for r in c.execute('''WITH group_scope AS (SELECT %s::text AS account,%s::text AS chat,
        %s::text AS provider_chat,%s::text AS platform) SELECT threads.id FROM group_scope gs CROSS JOIN LATERAL ('''+query(
            'm.id',account='gs.account',chat='gs.chat',provider_chat='gs.provider_chat',platform='gs.platform',limit=17)+
        ') threads',(account,chat,provider_chat,platform)).fetchall()]


def attention_site_reads(s,key,account,chat):
    """Each read site's group-attention rows: the per-chat view, the page batch, the page statement and the count."""
    from messaging_service.chat_queries import list_chats
    from messaging_service.conversation_state import conversation_state,conversation_states
    with s.authorized(key,'list_chats') as (c,p):
        measured=AttentionPlans(c)
        state=conversation_state(measured,p,account,chat)
        conversation_states(measured,p,[(account,chat)])
        page=list_chats(s,measured,p,'',25,order='activity',view='conversation',include_coverage=False,
            awaiting_reply=True,awaiting_reply_days=0)
    return state,page,[(site,attention_reads(plan)) for site,plan in measured.plans]


def legacy_attention(monkeypatch):
    from group_attention_legacy import legacy_group_thread_query
    from messaging_service import chat_queries,conversation_state
    monkeypatch.setattr(conversation_state,'group_thread_query',legacy_group_thread_query)
    monkeypatch.setattr(chat_queries,'group_thread_query',legacy_group_thread_query)


def timeless(value):
    """The answer without its read time: the two routes run in separate transactions."""
    if isinstance(value,dict):return {k:timeless(v) for k,v in value.items() if k!='as_of'}
    if isinstance(value,list):return [timeless(v) for v in value]
    return value

# spec: ANYM-915
def test_group_attention_reads_its_candidates_not_the_group_history(seeded,monkeypatch):
    s,_,_=seeded
    seed_attention_groups(s)
    try:check_attention_groups(s,monkeypatch)
    finally:forget_attention_groups(s)


def check_attention_groups(s,monkeypatch):
    from group_attention_legacy import legacy_group_thread_query
    from messaging_service.conversation_state import group_thread_query
    wa='120363000000000001@g.us'
    expected={chat:[chat+'-'+str(n) for n in (49999,49990,40009,25003,20000,3001,100,7)] for chat in ('ga-tg','ga-wa')}
    with s.pool.connection() as c:
        for account,chat,provider_chat,platform in (('telegram-demo','ga-tg','-100915','telegram'),
                ('whatsapp-demo-1','ga-wa',wa,'whatsapp')):
            assert attention_ids(c,account,chat,provider_chat,platform,legacy_group_thread_query)==expected[chat]
            assert attention_ids(c,account,chat,provider_chat,platform,group_thread_query)==expected[chat]
    live={}
    for account,chat in (('telegram-demo','ga-tg'),('whatsapp-demo-1','ga-wa'),('telegram-demo','gb-tg')):
        key=s.issue_key('attention-'+chat,[account],chats=[chat])
        live[chat]=attention_site_reads(s,key,account,chat)
    with monkeypatch.context() as patched:
        legacy_attention(patched)
        old={chat:attention_site_reads(s,s.issue_key('legacy-'+chat,[account],chats=[chat]),account,chat)
            for account,chat in (('telegram-demo','ga-tg'),('whatsapp-demo-1','ga-wa'),('telegram-demo','gb-tg'))}
    for chat in live:
        state,page,reads=live[chat]
        old_state,old_page,old_reads=old[chat]
        # Same answer from every site; the four sites each ran (the per-chat view, the page batch, the page, the count).
        assert timeless(state)==timeless(old_state) and timeless(page['items'])==timeless(old_page['items']),chat
        sites={site for site,_ in reads}
        assert {'conversation_state','conversation_states','_list_chats','_enrich_page'}<=sites,(chat,sites)
        if chat=='gb-tg':
            # Shape B: an owner-active group reads no more rows than today's walk since the owner's message.
            assert [site for site,_ in reads]==[site for site,_ in old_reads],(reads,old_reads)
            assert all(n<=o for (_,n),(_,o) in zip(reads,old_reads)),(reads,old_reads)
        else:
            assert all(n<=1000 for _,n in reads),(chat,reads)
            assert max(n for _,n in old_reads)>20000,(chat,old_reads)


def seed_page_proof(s):
    """ANYM-950 criterion 1: 60,000 messages over 400 chats (Telegram and WhatsApp), metadata rows for 80% of them.
    Message k of chat n is k*400+n minutes old, so the newest 400 messages are the chats' heads, chat 0's newest."""
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='120s'")
        c.execute("""INSERT INTO chats(id,account,provider_chat,title,chat_type)
            SELECT 'bp-'||n,CASE WHEN n%2=0 THEN 'telegram-demo' ELSE 'whatsapp-demo-1' END,
              CASE WHEN n%2=0 THEN 'bp-'||n ELSE (720000+n)::text||'@c.us' END,'Synthetic '||n,'private'
            FROM generate_series(0,399) n""")
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'bp-m-'||n||'-'||k,CASE WHEN n%2=0 THEN 'telegram-demo' ELSE 'whatsapp-demo-1' END,'bp-'||n,
              CASE WHEN n%2=0 THEN (n*1000+k)::text ELSE 'wamid-bp-'||n||'-'||k END,CASE WHEN k%2=0 THEN 'peer' ELSE 'owner' END,
              'synthetic text '||k,now()-(k*400+n)*interval '1 minute',CASE WHEN k%2=0 THEN 'incoming' ELSE 'outgoing' END
            FROM generate_series(0,399) n,generate_series(0,149) k""")
        c.execute("""INSERT INTO message_metadata(message,revision,metadata)
            SELECT 'bp-m-'||n||'-'||k,1,'{"synthetic":true}'::jsonb FROM generate_series(0,399) n,generate_series(0,149) k
            WHERE (n+k)%5<>0""")
        c.execute('ANALYZE messages');c.execute('ANALYZE message_metadata');c.execute('ANALYZE chats')


def page_proof_reads(plans):
    """Rows the page statement's proof read from messages (actual and filtered rows x loops). One page statement ran:
    a failed proof would add the per-chat plan's own chat_page statement."""
    page=[plan for chain,plan in plans if chain and chain[-1] in ('list_chats','_list_chats') and 'chat_page' in str(plan)]
    assert len(page)==1 and 'page_proof' in str(page[0]),[chain[-1:] for chain,_ in plans]
    proofs=[node for node in nodes(page[0]) if node.get('Subplan Name')=='CTE page_proof']
    assert len(proofs)==1,proofs
    return visited(proofs[0],{'messages'})


# spec: ANYM-950
def test_page_proof_reads_the_messages_after_the_page_floor_not_the_archive(seeded,monkeypatch):
    from page_proof_legacy import with_legacy_proof
    from messaging_service import chat_queries
    s,a,_=seeded
    seed_page_proof(s)
    try:
        got,plans=listed(s,a)
        with monkeypatch.context() as patched:
            patched.setattr(chat_queries,'_page_first',with_legacy_proof(chat_queries._page_first))
            old,old_plans=listed(s,a)
        # Both proofs passed: the page is the 25 newest chats, from the page-first plan in both routes.
        assert [r['id'] for r in got['items']]==['bp-'+str(n) for n in range(25)]
        assert timeless(got)==timeless(old)
        reads,old_reads=page_proof_reads(plans),page_proof_reads(old_plans)
        assert reads<=1000 and old_reads>20000,(reads,old_reads)
    finally:forget_attention_groups(s)


def page_first_heads(plans):
    """Head reads the page-first plan added: per statement the most-run messages scan, summed over its statements."""
    return sum(max((node.get('Actual Loops',1) for node in nodes(plan) if node.get('Relation Name')=='messages'),default=0)
        for chain,plan in plans if chain and chain[-1]=='_page_first')


# spec: ANYM-878
def test_deep_activity_cursor_returns_the_per_chat_answer_within_its_extra_head_allowance(seeded):
    from messaging_service import chat_queries
    s,a,b=seeded
    seed_catalog(s)
    limit=25
    with s.authorized(a,'list_chats') as (c,p):
        after=''
        for _ in range(6):  # 600 of 1,000 chats: past half of the catalog
            after=chat_queries.list_chats(s,c,p,after,100,order='activity',view='conversation',include_coverage=False)['next_cursor']
        measured=Plans(c)
        got=chat_queries.list_chats(s,measured,p,after,limit,order='activity',view='conversation',include_coverage=False)
        real=chat_queries._page_first;chat_queries._page_first=lambda *x,**k:None
        try:oracle=chat_queries.list_chats(s,c,p,after,limit,order='activity',view='conversation',include_coverage=False)
        finally:chat_queries._page_first=real
    assert got==oracle and len(got['items'])==limit
    # The narrowed criterion 2: at most today's plan plus 7x(limit+1) head reads for the page-first tries.
    assert page_first_heads(measured.plans)<=7*(limit+1),page_first_heads(measured.plans)


# spec: ANYM-878
def test_a_large_timestamp_tie_falls_back_before_any_head_read(seeded):
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("""INSERT INTO chats(id,account,provider_chat,title,chat_type)
            SELECT 'tie-'||n,'telegram-demo','tie-'||n,'Synthetic '||n,'private' FROM generate_series(0,299) n""")
        # 300 chats whose only message has one shared timestamp: the floor tie is larger than the candidate cap.
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'tie-m-'||n,'telegram-demo','tie-'||n,n::text,'peer','synthetic text',date_trunc('minute',now()),'incoming'
            FROM generate_series(0,299) n""")
        c.execute('ANALYZE messages')
    result,plans=listed(s,a)
    assert len(result['items'])==25 and [r['id'] for r in result['items']][:2]==['tie-99','tie-98']
    # The window and the bounded tie read ran; no candidate head was read before the per-chat fallback.
    own=[plan for chain,plan in plans if chain and chain[-1]=='_page_first']
    assert own and all(node.get('Actual Loops',1)<=1 for plan in own for node in nodes(plan) if node.get('Relation Name')=='messages'),[scans(plan) for plan in own]


# spec: ANYM-878
def test_default_activity_page_reads_whatsapp_identity_rows_for_the_page_only(seeded):
    s,a,b=seeded
    seed_catalog(s)
    result,plans=listed(s,a,indexed=True)
    paired=sum(1 for r in result['items'] if r['id'].startswith('pf-lid-') or r['id'].startswith('pf-pn-'))
    # The related-chat hint keeps its corpus-wide alias set (ANYM-656) inside its own 0.3 s budget; it is not page work.
    steps={}
    for chain,plan in plans:
        if 'hints' not in chain:steps['>'.join(chain[-3:])]=steps.get('>'.join(chain[-3:]),0)+visited(plan,{'whatsapp_identity_observations'})
    read=sum(steps.values())
    # 600 observations in the catalog (150 pairs, 4 each); today's catalog-wide pair read alone takes about 16 per pair (2,400).
    # Each page step (pairs, titles, senders, peers) reads only the page's pairs: their observations, conflict and source probes.
    assert 0<paired<=26 and read<=100*paired,(paired,read,{k:v for k,v in steps.items() if v})


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
