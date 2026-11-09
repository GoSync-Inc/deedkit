"""ANYM-878: the page-first activity order returns today's rows, order and cursors."""
import itertools
from datetime import datetime, timedelta, timezone

from hypothesis import HealthCheck, given, settings, strategies as st

from messaging_service import chat_queries

KINDS = ('text', 'deleted', 'service', 'media', 'empty')
MESSAGE = st.tuples(st.integers(0, 6), st.sampled_from(KINDS), st.booleans())
CHAT = st.tuples(st.sampled_from(('tg-private', 'tg-group', 'tg-channel', 'wa-group')), st.lists(MESSAGE, max_size=4))
PAIR = st.tuples(st.integers(0, 6), st.lists(MESSAGE, max_size=3), st.lists(MESSAGE, max_size=3))
keys = itertools.count()


def build(s, chats, pairs):
    """Synthetic catalog: Telegram and WhatsApp chats, verified LID/PN pairs; times in a 7-minute span for ties."""
    base = datetime.now(timezone.utc)-timedelta(days=1)
    rows, messages, observations = [], [], []
    numbers = itertools.count(1)
    def add(chat, account, spec):
        minute, kind, outgoing = spec
        n = next(numbers)
        provider = str(n) if account == 'telegram-demo' else 'wamid-'+str(n)
        body = '' if kind in ('media', 'empty') else 'synthetic text '+str(n)
        metadata = {'service': '{"is_service":true}', 'media': '{"media_kind":"photo"}'}.get(kind)
        messages.append(('h-m-'+str(n), account, chat, provider, 'owner' if outgoing else 'peer', body,
            base+timedelta(minutes=minute), 'outgoing' if outgoing else 'incoming', kind == 'deleted', metadata))
        return 'h-m-'+str(n)
    for i, (kind, specs) in enumerate(chats):
        account = 'whatsapp-demo-1' if kind == 'wa-group' else 'telegram-demo'
        chat_type = kind.split('-')[1]
        rows.append(('h-'+str(i), account, 'h-'+str(i)+('@g.us' if kind == 'wa-group' else ''), 'Synthetic '+str(i), chat_type))
        for spec in specs: add('h-'+str(i), account, spec)
    for i, (minute, lid_specs, pn_specs) in enumerate(pairs):
        lid, pn = str(900000+i)+'@lid', str(700000+i)+'@c.us'
        rows += [('h-lid-'+str(i), 'whatsapp-demo-1', lid, lid, 'private'), ('h-pn-'+str(i), 'whatsapp-demo-1', pn, 'Peer '+str(i), 'private')]
        source = add('h-lid-'+str(i), 'whatsapp-demo-1', (minute, 'text', False))
        messages[-1] = (*messages[-1][:4], lid, *messages[-1][5:])
        observations.append((source, lid, pn))
        for spec in lid_specs: add('h-lid-'+str(i), 'whatsapp-demo-1', spec)
        for spec in pn_specs: add('h-pn-'+str(i), 'whatsapp-demo-1', spec)
    with s.pool.connection() as c:
        c.execute("DELETE FROM messages WHERE chat LIKE 'h-%'");c.execute("DELETE FROM chats WHERE id LIKE 'h-%'")
        for row in rows:c.execute('INSERT INTO chats(id,account,provider_chat,title,chat_type) VALUES(%s,%s,%s,%s,%s)', row)
        for m in messages:
            c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction,deleted)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''', m[:9])
            if m[9]:c.execute('INSERT INTO message_metadata(message,revision,metadata) SELECT id,revision,%s::jsonb FROM messages WHERE id=%s', (m[9], m[0]))
        for source, lid, pn in observations:
            c.execute('''INSERT INTO whatsapp_identity_observations(account,source_message,identity_id,pn,lid,provenance,
                first_observed_at,observed_at) VALUES('whatsapp-demo-1',%s,%s,%s,%s,'webjs_contact_cache_v1',now(),now())''', (source, lid, pn, lid))
    return [r[0] for r in rows]


def walk(s, c, p, *, limit, view, accounts, page_first, pages=6, **filters):
    """Every page of the catalog walk (rows and cursors), page-first or forced onto today's per-chat plan (the oracle)."""
    real, pairs = chat_queries._page_first, chat_queries._alias_pairs
    heads = []
    def counted(accounts, c, p, chats=None):
        if chats is not None:heads.append(len(chats))
        return pairs(accounts, c, p, chats=chats)
    if not page_first:chat_queries._page_first = lambda *a, **k: None
    chat_queries._alias_pairs = counted
    try:
        out, after = [], ''
        for _ in range(pages):
            heads.clear()
            result = chat_queries.list_chats(s, c, p, after, limit, order='activity', view=view, accounts=accounts,
                include_coverage=False, **filters)
            # Each page-first try reads the heads of its candidates (one pair read per try): 7x(limit+1) in all.
            assert sum(heads) <= 7*(limit+1), heads
            out.append(result)
            after = result['next_cursor']
            if not after:break
        return out
    finally:
        chat_queries._page_first, chat_queries._alias_pairs = real, pairs


def both(s, token, **kw):
    # One transaction, so CURRENT_TIMESTAMP-derived fields are the same in both walks.
    with s.authorized(token, 'list_chats') as (c, p):
        return walk(s, c, p, page_first=True, **kw), walk(s, c, p, page_first=False, **kw)


# spec: ANYM-878
@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(st.lists(CHAT, max_size=12), st.lists(PAIR, max_size=3), st.integers(1, 3), st.sampled_from(('conversation', 'archive')),
       st.booleans(), st.sampled_from((None, ['telegram-demo'])), st.data())
def test_page_first_activity_pages_equal_the_per_chat_plan(seeded, chats, pairs, limit, view, restricted, accounts, data):
    s, a, _ = seeded
    ids = build(s, chats, pairs)
    token = a
    if restricted and ids:
        # A restricted principal sees a drawn subset; it never gets more rows than its chats.
        allowed = data.draw(st.lists(st.sampled_from(ids), min_size=1, unique=True))
        token = s.issue_key('page-first-'+str(next(keys)), ['telegram-demo', 'whatsapp-demo-1'], chats=allowed)
    got, oracle = both(s, token, limit=limit, view=view, accounts=accounts)
    assert got == oracle
    if restricted and ids:
        assert {r['id'] for page in got for r in page['items']} <= set(allowed)


# spec: ANYM-878
def test_page_first_second_page_after_a_new_message_matches_the_per_chat_plan(seeded):
    s, a, _ = seeded
    specs = [('tg-private', [(m, 'text', m % 2 == 0)]) for m in range(7)]
    build(s, specs*3, [(3, [(5, 'text', True)], [(6, 'media', False)])])
    with s.authorized(a, 'list_chats') as (c, p):
        first = chat_queries.list_chats(s, c, p, '', 4, order='activity', view='conversation', include_coverage=False)
    # A message arrives between the two reads, in a chat the first page did not show.
    shown = {r['id'] for r in first['items']}
    target = next(i for i in ('h-'+str(n) for n in range(21)) if i not in shown)
    with s.pool.connection() as c:
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            VALUES('h-m-late','telegram-demo',%s,'999','peer','synthetic late text',now(),'incoming')''', (target,))
    found = []
    real = chat_queries._page_first
    def spy(*args, **kw):
        found.append(real(*args, **kw));return found[-1]
    def second(c, p, plan):
        chat_queries._page_first = plan
        try:return chat_queries.list_chats(s, c, p, first['next_cursor'], 4, order='activity', view='conversation', include_coverage=False)
        finally:chat_queries._page_first = real
    with s.authorized(a, 'list_chats') as (c, p):
        got, oracle = second(c, p, spy), second(c, p, lambda *a, **k: None)
    # The second page came from the page-first plan, not from its fallback.
    assert found and found[0] is not None
    assert got['items'] and got == oracle


def spied():
    """Wrap _page_first; the list holds what each call returned."""
    found, real = [], chat_queries._page_first
    def spy(*args, **kw):
        found.append(real(*args, **kw));return found[-1]
    return found, spy, real


# spec: ANYM-878
def test_page_first_role_filter_keeps_the_unknown_role_count(seeded):
    s, a, _ = seeded
    build(s, [('tg-channel', [(m % 7, 'text', False)]) for m in range(30)], [])
    with s.pool.connection() as c:
        c.execute("UPDATE chats SET my_role='admin',can_post=true,role_observed_at=now() WHERE id LIKE 'h-%' AND id<>'h-29'")
        c.execute("UPDATE messages SET sent_at=sent_at-interval '30 days' WHERE chat='h-29'")
    found, spy, real = spied()
    chat_queries._page_first = spy
    try:
        with s.authorized(a, 'list_chats') as (c, p):
            got, oracle = (walk(s, c, p, limit=25, view='conversation', accounts=None, page_first=True, pages=1, my_roles=['admin']),
                walk(s, c, p, limit=25, view='conversation', accounts=None, page_first=False, pages=1, my_roles=['admin']))
    finally:
        chat_queries._page_first = real
    # The page came from the page-first plan, and the count of unknown-role channels still reads the whole catalog.
    assert found and found[0] is not None
    assert got == oracle and got[0]['unknown_role_rows'] == 1


# spec: ANYM-878
def test_page_first_reruns_on_the_per_chat_plan_when_a_chat_gets_newer_inside_the_call(seeded):
    s, a, _ = seeded
    build(s, [('tg-private', [(m, 'text', False)]) for m in range(7)]*2, [])
    found, real = [], chat_queries._page_first
    def racing(*args, **kw):
        # Another writer commits after the candidates were read: a chat outside them becomes the newest.
        result = real(*args, **kw);found.append(result)
        if result and len(found) == 1:
            outside = next(i for i in ('h-'+str(n) for n in range(14)) if i not in result['found'])
            with s.pool.connection() as w:
                w.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
                    VALUES('h-m-race','telegram-demo',%s,'998','peer','synthetic racing text',now(),'incoming')''', (outside,))
        return result
    chat_queries._page_first = racing
    try:
        with s.authorized(a, 'list_chats') as (c, p):
            got = chat_queries.list_chats(s, c, p, '', 3, order='activity', view='conversation', include_coverage=False)
            chat_queries._page_first = lambda *a, **k: None
            oracle = chat_queries.list_chats(s, c, p, '', 3, order='activity', view='conversation', include_coverage=False)
    finally:
        chat_queries._page_first = real
    # The proof failed in the page statement's snapshot, so the call answered from the per-chat plan: the racing chat leads.
    assert found and found[0] is not None
    assert got == oracle and got['items'][0]['last_message_id'] == 'h-m-race'


# spec: ANYM-878
def test_page_first_reruns_on_the_per_chat_plan_when_candidate_heads_vanish_inside_the_call(seeded):
    s, _, _ = seeded
    # Two chats with messages and, after them in id order, chats without any: the per-chat plan lists those by id.
    ids = build(s, [('tg-private', [(5, 'text', False)]), ('tg-private', [(4, 'text', False)])]+[('tg-private', [])]*8, [])
    # A key for just these 10 chats (more than the first window's 8), so no other chat has a message to fail the proof.
    token = s.issue_key('page-first-vanish', ['telegram-demo'], chats=ids)
    found, real = [], chat_queries._page_first
    def deleting(*args, **kw):
        result = real(*args, **kw);found.append(result)
        if result and len(found) == 1:
            with s.pool.connection() as w:
                w.execute("UPDATE messages SET deleted=true WHERE chat=ANY(%s)", (result['found'],))
        return result
    chat_queries._page_first = deleting
    try:
        with s.authorized(token, 'list_chats') as (c, p):
            got = chat_queries.list_chats(s, c, p, '', 1, order='activity', view='conversation', include_coverage=False)
            chat_queries._page_first = lambda *a, **k: None
            oracle = chat_queries.list_chats(s, c, p, '', 1, order='activity', view='conversation', include_coverage=False)
    finally:
        chat_queries._page_first = real
    assert oracle['items'][0]['id'] == 'h-9'
    # The candidates' heads became NULL; ordering among headless chats is by id, which no message proof covers.
    assert found and found[0] is not None
    assert got == oracle

