"""Related-chat hint on chat rows (ANYM-656): a new private chat that repeats an owner message from an older one."""
import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from messaging_service import related_chats

TG,WA='telegram-demo','whatsapp-demo-1'
TEXT='Please send the signed lease scan before Friday noon'
HINT_KEYS={'related_chats','continued_by'}


def when(days):return datetime.now(timezone.utc)-timedelta(days=days)


def chat(s,account,cid,kind='private',title=None):
    with s.pool.connection() as c:
        c.execute('INSERT INTO chats(id,account,provider_chat,title,chat_type) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
            (cid,account,cid,title or 'Synthetic '+cid,kind))


def say(s,account,cid,mid,body,days,direction='incoming',deleted=False,origin='live'):
    with s.pool.connection() as c:
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction,deleted)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(mid,account,cid,mid,'owner' if direction=='outgoing' else 'peer',body,
            when(days),direction,deleted))
        s.append_event(c,account,mid,origin=origin)


def pair(s,body=TEXT,*,old_kind='private',new_kind='private',copy_days=1,source_days=3,account=TG,source_account=None,
        old='old',new='new',deleted=False):
    chat(s,source_account or account,old,old_kind);chat(s,account,new,new_kind)
    say(s,source_account or account,old,old+'-out',body,source_days,'outgoing',deleted=deleted)
    say(s,account,new,new+'-in',body,copy_days)


def rows(s,token,**kw):
    return {r['id']:r for r in s.list_chats(token,limit=100,view='archive',**kw)['items']}


def updates(s,token,**kw):
    page=s.get_updates(token,'related',view='by_chat',from_time=when(40).isoformat(),**kw)
    return {r['chat']:r for r in page['chats']}


def hint(row):return {k:row[k] for k in HINT_KEYS if k in row}


# spec: ANYM-656
@pytest.mark.parametrize('read',[rows,updates])
def test_a_copied_owner_message_links_the_new_chat_to_the_older_one_and_back(seeded,read):
    s,a,_=seeded;pair(s)
    # The overview lists only chats with new incoming messages, so the older chat needs one to appear there.
    say(s,TG,'old','old-in','Thanks, that arrived on my side too',2.5)
    page=read(s,a)
    link=page['new']['related_chats']
    assert link==[{'chat_id':'old','title':'Synthetic old','message_id':'new-in','source_message_id':'old-out',
        'matched_at':link[0]['matched_at'],'heuristic':True}]
    back=page['old']['continued_by']
    assert [(x['chat_id'],x['message_id'],x['source_message_id'],x['heuristic']) for x in back]==[('new','new-in','old-out',True)]
    assert 'continued_by' not in page['new'] and 'related_chats' not in page['old']


# spec: ANYM-656
def test_normalization_matches_case_and_spacing(seeded):
    s,a,_=seeded
    chat(s,TG,'old');chat(s,TG,'new')
    say(s,TG,'old','old-out',TEXT,3,'outgoing')
    say(s,TG,'new','new-in','  PLEASE send   the signed lease scan before Friday noon\n',1)
    for read in (rows,updates):
        page=read(s,a)
        assert page['new']['related_chats'][0]['source_message_id']=='old-out'


# spec: ANYM-656
def test_unicode_compatibility_forms_are_different_text_not_a_hidden_broadcast(seeded):
    s,a,_=seeded;pair(s)
    chat(s,TG,'third');say(s,TG,'third','third-out',TEXT.replace('lease','ｌｅａｓｅ'),2,'outgoing')
    # The lookup sees lowercase and spacing only; a compatibility variant is not a copy, so the hint stands.
    assert rows(s,a)['new']['related_chats'][0]['chat_id']=='old'


# spec: ANYM-656
def test_a_body_too_long_to_compare_whole_never_matches_on_a_prefix(seeded):
    s,a,_=seeded
    head='request '*300
    chat(s,TG,'old');chat(s,TG,'new');chat(s,TG,'near')
    say(s,TG,'old','old-out',head+'alpha ending',3,'outgoing')
    say(s,TG,'near','near-in',head+'beta ending',2)
    say(s,TG,'new','new-in',head+'alpha ending',1)
    assert not any(HINT_KEYS&set(r) for r in rows(s,a).values())
    # The 2 KB limit counts bytes. The same shape under the limit does hint (the ASCII token is searchable), so only the
    # byte guard stops the 2400-byte body: 1600 characters of Cyrillic text.
    for name,repeat in (('short',300),('long',800)):
        chat(s,TG,'old-'+name);chat(s,TG,'new-'+name);body='confirmation '+'ж '*repeat
        say(s,TG,'old-'+name,'old-'+name+'-out',body,3,'outgoing');say(s,TG,'new-'+name,'new-'+name+'-in',body,1)
    page=rows(s,a)
    assert 'related_chats' in page['new-short'] and 'related_chats' not in page['new-long']


# spec: ANYM-656
def test_case_variants_the_database_does_not_fold_are_not_a_copy_and_the_ones_it_folds_are_a_broadcast(seeded):
    s,a,_=seeded;pair(s)
    kelvin=TEXT.replace('Friday','Fri'+chr(0x212a)+'day')
    chat(s,TG,'fourth');say(s,TG,'fourth','fourth-out',kelvin,2,'outgoing')
    with s.pool.connection() as c:
        same=c.execute("SELECT lower(%s)=lower(%s) AS x",(kelvin,TEXT)).fetchone()['x']
    # The equality is the database's own lower(): a variant it folds to the same text is a second recipient (a
    # broadcast, no hint); one it leaves different (ASCII-only collations) is another text, so the hint stands.
    assert ('related_chats' not in rows(s,a)['new'])==same


# spec: ANYM-656
def test_cyrillic_text_matches_only_where_the_database_lowercases_it(seeded):
    s,a,_=seeded;text='Пожалуйста пришлите подписанный договор до пятницы'
    chat(s,TG,'old');chat(s,TG,'new')
    say(s,TG,'old','old-out',text,3,'outgoing');say(s,TG,'new','new-in',text.upper(),1)
    with s.pool.connection() as c:folds=c.execute("SELECT lower('Я')='я' AS x").fetchone()['x']
    assert ('related_chats' in rows(s,a)['new'])==folds


# spec: ANYM-656
@pytest.mark.parametrize('case',['short','bare_url','group_new','group_old','channel_old','backfilled','outside_window','deleted'])
def test_skips_never_produce_a_hint(seeded,case):
    s,a,_=seeded
    if case=='short':pair(s,'Send me the scan')
    elif case=='bare_url':pair(s,'https://example.invalid/some/long/shared/document-path')
    elif case=='group_new':pair(s,new_kind='group')
    elif case=='group_old':pair(s,old_kind='group')
    elif case=='channel_old':pair(s,old_kind='channel')
    elif case=='backfilled':pair(s,copy_days=1);say(s,TG,'new','new-first','Hello there, first archived words',20,origin='backfill')
    elif case=='outside_window':pair(s,source_days=9)
    else:pair(s,deleted=True)
    for read in (rows,updates):
        assert not any(HINT_KEYS&set(r) for r in read(s,a).values()),case


# spec: ANYM-656
def test_a_text_sent_to_two_visible_chats_is_a_broadcast_and_a_hidden_copy_changes_nothing(seeded):
    s,a,b=seeded;pair(s)
    chat(s,TG,'third');say(s,TG,'third','third-out',TEXT,2,'outgoing')
    assert not any(HINT_KEYS&set(r) for r in rows(s,a).values())
    # The same data seen by a key that cannot read the third chat is identical to a world without that copy.
    s.set_access('agent-b',chats=['old','new'])
    scoped=rows(s,b)
    assert scoped['new']['related_chats'][0]['chat_id']=='old' and set(scoped)=={'old','new'}
    with s.pool.connection() as c:c.execute("UPDATE messages SET deleted=true WHERE id='third-out'")
    assert hint(rows(s,b)['new'])==hint(scoped['new'])


# spec: ANYM-656
def test_scope_decides_what_a_key_can_link(seeded):
    s,a,b=seeded;pair(s,account=TG,source_account=WA,old='wa-old',new='tg-new')
    assert rows(s,a)['tg-new']['related_chats'][0]['chat_id']=='wa-old'
    assert 'related_chats' not in rows(s,b).get('tg-new',{})
    s.set_access('agent-b',chats=['tg-new'])
    assert 'related_chats' not in rows(s,b)['tg-new']
    s.set_access('agent-b',chats=[])
    assert rows(s,b)=={}


# spec: ANYM-656
def test_a_purged_older_chat_never_enters_a_candidate(seeded):
    s,a,_=seeded;pair(s)
    with s.pool.connection() as c:c.execute("UPDATE chats SET purged_at=now() WHERE id='old'")
    assert not any(HINT_KEYS&set(r) for r in rows(s,a).values())


def alias_pair(s,copies=0):
    """A verified WhatsApp pair on whatsapp-demo-1: the LID chat is the alias; `copies` owner copies of TEXT sit in it."""
    with s.pool.connection() as c:
        c.execute("""INSERT INTO chats(id,account,provider_chat,title,chat_type) VALUES
            ('lid-chat','whatsapp-demo-1','900@lid','Synthetic lid','private'),
            ('pn-chat','whatsapp-demo-1','1234567890@c.us','Synthetic pn','private')""")
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            VALUES('lid-src','whatsapp-demo-1','lid-chat','lid-src','900@lid','Synthetic hello from the peer',%s,'incoming')""",(when(30),))
        c.execute("""INSERT INTO whatsapp_identity_observations(account,source_message,identity_id,pn,lid,provenance,first_observed_at,observed_at)
            VALUES('whatsapp-demo-1','lid-src','900@lid','1234567890@c.us','900@lid','whatsmeow_message_alt_v1',now(),now())""")
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'lid-out-'||n,'whatsapp-demo-1','lid-chat','lid-out-'||n,'owner',%s,%s,'outgoing' FROM generate_series(1,%s) n""",
            (TEXT,when(2),copies))


# spec: ANYM-656
@pytest.mark.parametrize('filters',[{},{'accounts':[TG]}])
def test_alias_copies_neither_create_nor_suppress_a_hint_and_page_filters_change_nothing(seeded,filters):
    s,a,_=seeded;pair(s)
    alias_pair(s,copies=205)  # more than the candidate cap: counted before alias removal they would silence the hint
    for read in (rows,updates):
        assert read(s,a,**filters)['new']['related_chats'][0]['chat_id']=='old'
    with s.pool.connection() as c:c.execute("UPDATE messages SET deleted=true WHERE id LIKE 'lid-out-%'")
    assert rows(s,a,**filters)['new']['related_chats'][0]['chat_id']=='old'


# spec: ANYM-656
def test_a_new_chats_own_alias_twin_is_not_another_chat(seeded):
    s,a,_=seeded
    alias_pair(s,copies=0)
    chat(s,TG,'old');say(s,TG,'old','old-out',TEXT,3,'outgoing')
    with s.pool.connection() as c:
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            VALUES('pn-in','whatsapp-demo-1','pn-chat','pn-in','1234567890@c.us',%s,%s,'incoming'),
                  ('lid-out-x','whatsapp-demo-1','lid-chat','lid-out-x','owner',%s,%s,'outgoing')""",(TEXT,when(1),TEXT,when(1.5)))
    # The owner's copy in the twin of the new chat is the same conversation, not a second recipient.
    assert rows(s,a)['pn-chat']['related_chats'][0]['chat_id']=='old'


# spec: ANYM-656
def test_continued_by_needs_the_new_chat_in_the_same_response_and_adds_no_lookup(seeded):
    s,a,_=seeded;pair(s)
    tagged=[]
    class Spy:
        def __init__(self,c):self.c=c
        def execute(self,sql,*args,**kw):
            if 'related_chats:' in str(sql):tagged.append(str(sql).split('related_chats:')[1].split(' ')[0])
            return self.c.execute(sql,*args,**kw)
        def __getattr__(self,name):return getattr(self.c,name)
    original=s.authorized
    @contextmanager
    def spied(token,operation=None):
        with original(token,operation) as (c,p):yield Spy(c),p
    s.authorized=spied
    try:
        # Only the older chat is on this page: its new-chat check may run, nothing looks the older chat up.
        only_old=s.list_chats(a,limit=100,view='archive',query='Synthetic old')
        assert [r['id'] for r in only_old['items']]==['old'] and not HINT_KEYS&set(only_old['items'][0])
        assert set(tagged)<={'new','first'} and 'candidates' not in tagged
        tagged.clear()
        both=s.list_chats(a,limit=100,view='archive')
        assert 'candidates' in tagged
    finally:del s.authorized
    assert 'continued_by' in {r['id']:r for r in both['items']}['old']


# spec: ANYM-656
def test_the_conversation_view_and_compact_detail_carry_both_keys(seeded):
    from messaging_service.presentation import render
    s,a,_=seeded;pair(s)
    say(s,TG,'old','old-in','Thanks, that arrived on my side too',2.5)
    compact=render('list_chats',s.list_chats(a,limit=100,view='conversation'),'compact')
    by_id={r['id']:r for r in compact['items']}
    assert by_id['new']['related_chats'][0]['chat_id']=='old' and by_id['old']['continued_by'][0]['chat_id']=='new'
    assert 'related_chats' not in by_id['old'] and 'continued_by' not in by_id['new']
    flat=render('list_chats',s.list_chats(a,limit=100,view='archive'),'compact')
    assert 'related_chats' in {r['id']:r for r in flat['items']}['new']


# spec: ANYM-656
def test_the_gateway_catalog_and_the_backend_publish_the_same_heuristic_wording():
    from pathlib import Path
    from messaging_service.app import RELATED_CHATS_NOTE,RELATED_PROPERTIES
    catalog={t['name']:t for t in json.loads((Path(__file__).resolve().parents[1]/'services/gateway/src/agent_gateway/catalog.json').read_text())}
    listed=catalog['messages_list_chats']['outputSchema']['properties']['items']['items']['properties']
    overview=catalog['messages_get_updates']['outputSchema']['properties']['chats']['items']['properties']
    for published in (listed,overview):
        assert {k:published[k]['description'] for k in RELATED_PROPERTIES}=={k:v['description'] for k,v in RELATED_PROPERTIES.items()}
    assert 'Absence of `related_chats` means nothing was detected, not that the chats are unrelated' in RELATED_CHATS_NOTE
    assert 'ANYM-283' in RELATED_CHATS_NOTE and 'after 7 days' in RELATED_CHATS_NOTE


# spec: ANYM-656
def test_at_most_five_new_chats_are_checked_per_page(seeded):
    s,a,_=seeded
    for n in range(8):pair(s,TEXT+' variant number %d with more words'%n,old='old%d'%n,new='new%d'%n)
    page=rows(s,a)
    assert sum('related_chats' in page['new%d'%n] for n in range(8))==5
    assert not any('related_chats' in page['old%d'%n] for n in range(8))
    assert sum('continued_by' in page['old%d'%n] for n in range(8))==5


# spec: ANYM-656
def test_a_row_lists_at_most_three_related_chats_within_600_characters(seeded):
    s,a,_=seeded;chat(s,TG,'new')
    for n in range(4):
        chat(s,TG,'old%d'%n,title='T'*80);body='Unique request number %d about the signed lease scan'%n
        say(s,TG,'old%d'%n,'o%d'%n,body,3,'outgoing');say(s,TG,'new','n%d'%n,body,1-n/100)
    listed=rows(s,a)['new']['related_chats']
    assert len(listed)==3 and all(len(x['title'])<=60 for x in listed)
    assert len(json.dumps(listed,ensure_ascii=False,separators=(',',':')))<=600


# spec: ANYM-656
def test_the_overview_drops_hints_before_it_shortens_a_base_row(seeded):
    s,a,_=seeded;pair(s)
    full=s.get_updates(a,'budget',view='by_chat',from_time=when(40).isoformat())
    shrunk=related_chats_within_budget(full,limit=len(json.dumps(full,ensure_ascii=False,default=str))-5)
    assert not any(HINT_KEYS&set(r) for r in shrunk['chats']) and shrunk['shortened_for_size']=='hints'
    assert all('chat_title' in r for r in shrunk['chats'])


def related_chats_within_budget(page,limit):
    from messaging_service import update_queries
    original=update_queries.OVERVIEW_BUDGET;update_queries.OVERVIEW_BUDGET=limit
    try:return update_queries.within_budget(json.loads(json.dumps(page,default=str)))
    finally:update_queries.OVERVIEW_BUDGET=original


class Slow:
    """A connection that delays the helper's own statements: `before` runs inside the statement timeout the helper set
    (the database enforces it), `after` is client time between statements (only the elapsed-time deadline sees it)."""
    def __init__(self,c,before,after):self.c=c;self.before=before;self.after=after
    def execute(self,sql,*args,**kw):
        tagged='related_chats:' in str(sql)
        if tagged and self.before:self.c.execute('SELECT pg_sleep(%s)',(self.before,))
        result=self.c.execute(sql,*args,**kw)
        if tagged and self.after:time.sleep(self.after)
        return result
    def __getattr__(self,name):return getattr(self.c,name)


@contextmanager
def slow(s,before=0,after=0):
    original=s.authorized
    @contextmanager
    def wrapped(token,operation=None):
        with original(token,operation) as (c,p):yield Slow(c,before,after),p
    s.authorized=wrapped
    try:yield
    finally:del s.authorized


def audited(s):
    with s.pool.connection() as c:
        return c.execute("SELECT operation,outcome,resource_ids FROM audit_log WHERE operation IN ('list_chats','get_updates') ORDER BY id").fetchall()


def stripped(page):
    page=json.loads(json.dumps(page,default=str))
    for key in ('items','chats'):
        for row in page.get(key,[]):
            for hint_key in HINT_KEYS:row.pop(hint_key,None)
    return page


# spec: ANYM-656
@pytest.mark.parametrize('before,after,why',[(5,0,'QueryCanceled'),(0,0.55,'Deadline')])
def test_a_slow_or_failing_helper_returns_the_same_page_without_hints(seeded,caplog,before,after,why):
    s,a,_=seeded;pair(s)
    for n in range(3):pair(s,TEXT+' extra %d with enough words'%n,old='o%d'%n,new='n%d'%n)
    baseline_list=stripped(s.list_chats(a,limit=3,view='archive',order='id'))
    baseline_updates=stripped(s.get_updates(a,'slow',view='by_chat',from_time=when(40).isoformat()))
    seen=len(audited(s))
    with slow(s,before,after),caplog.at_level(logging.WARNING):
        listed=s.list_chats(a,limit=3,view='archive',order='id')
        overview=s.get_updates(a,'slow2',view='by_chat',from_time=when(40).isoformat())
    assert stripped(listed)==baseline_list and listed['next_cursor']==baseline_list['next_cursor']
    assert not any(HINT_KEYS&set(r) for r in listed['items'])
    assert stripped({**overview,'consumer':'slow'})==baseline_updates
    assert not any(HINT_KEYS&set(r) for r in overview['chats'])
    # Exactly one audit record per call, as for an ordinary read; one content-free log line per failure.
    assert len(audited(s))-seen==2 and all(r['outcome']=='ok' for r in audited(s))
    # The same targets an ordinary read records (the helper adds none).
    # Each call's record names the same targets as the ordinary read before it (audit ids are not ordered).
    from collections import Counter
    assert set(Counter((r['operation'],tuple(r['resource_ids'])) for r in audited(s)).values())=={2}
    lines=[r for r in caplog.records if r.name==related_chats.log.name]
    assert len(lines)==2 and all(TEXT not in r.getMessage() and 'Synthetic' not in r.getMessage() for r in lines)
    assert all(why in r.getMessage() for r in lines)
