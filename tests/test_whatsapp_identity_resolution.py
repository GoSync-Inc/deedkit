"""Bound alias resolution by candidate identities, not archive size squared."""
from time import monotonic

import pytest

from messaging_service.store import APIError
from messaging_service.whatsapp_identity import resolve_alias
from test_whatsapp_identity import identity_archive, observed


def _unrelated_archive(c, account, count=10000):
    # Owner identity is observed in every group, as it is for outgoing messages.
    # Each group also supplies unrelated identity evidence. Neither the repeated
    # owner pair nor unrelated evidence may multiply normalization of all chats.
    c.execute('''INSERT INTO chats(id,account,title,provider_chat)
        SELECT 'alias-noise-chat-'||n,%s,'Synthetic group',n||'@g.us'
        FROM generate_series(1,%s) n''', (account, count))
    c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
        SELECT 'alias-noise-owner-'||n,%s,'alias-noise-chat-'||n,'owner-'||n,
            '900@lid','Synthetic owner message',now()
        FROM generate_series(1,%s) n''', (account, count))
    c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
        SELECT 'alias-noise-other-'||n,%s,'alias-noise-chat-'||n,'other-'||n,
            (100000+n)||'@lid','Synthetic unrelated message',now()
        FROM generate_series(1,%s) n''', (account, count))
    c.execute('''INSERT INTO whatsapp_identity_observations
        (account,source_message,identity_id,pn,lid,provenance,first_observed_at,observed_at)
        SELECT %s,'alias-noise-owner-'||n,'900@lid','1234567890@c.us','900@lid',
            'whatsmeow_message_alt_v1',now(),now() FROM generate_series(1,%s) n
        UNION ALL SELECT %s,'alias-noise-other-'||n,(100000+n)||'@lid',
            (200000+n)||'@c.us',(100000+n)||'@lid','whatsmeow_lid_store_v1',now(),now()
        FROM generate_series(1,%s) n''', (account, count, account, count))
    for table in ('chats', 'messages', 'whatsapp_identity_observations'):
        c.execute('ANALYZE '+table)


def test_alias_resolution_with_repeated_owner_evidence_is_not_quadratic(identity_archive):
    s, _, state, key = identity_archive
    first = state.apply(observed(chat='900:2@lid'))
    s.set_access('identity-reader', accounts=['wa-identities', 'telegram-demo', 'whatsapp-demo-1'])
    with s.authorized(key) as (c, p):
        c.execute("SET LOCAL statement_timeout='60s'")
        _unrelated_archive(c, 'wa-identities')
        # The production statement budget, not a microbenchmark wall-clock
        # threshold. The old evidence×chats join exhausts it on this archive.
        c.execute("SET LOCAL statement_timeout='5s'")
        started = monotonic()
        row = resolve_alias(c, p, 'wa-identities', '1234567890:4@c.us')
        elapsed = monotonic()-started
        assert row['provider_chat'] == '900:2@lid'
        assert elapsed < 5
        assert resolve_alias(c, p, 'wa-identities', '9999999999@c.us') is None
    assert s.get_context(key, first['message'])['message']['provider_id'] == 'false_900:2@lid_000001'


@pytest.mark.parametrize('hidden_deleted', [False, True])
@pytest.mark.parametrize('same_phone', [False, True])
def test_hidden_historic_conflicts_still_block_alias(identity_archive, hidden_deleted, same_phone):
    s, _, state, key = identity_archive
    first = state.apply(observed())
    conflict = state.apply(observed(mid=2, chat='999@g.us',
        sender='901@lid' if same_phone else '900@lid',
        pn='1234567890@c.us' if same_phone else '1111111111@c.us'))
    visible_chat = s.get_context(key, first['message'])['message']['chat']
    if hidden_deleted:
        with s.pool.connection() as c:
            c.execute('UPDATE messages SET deleted=true WHERE id=%s', (conflict['message'],))
    s.set_access('identity-reader', chats=[visible_chat])
    with pytest.raises(APIError, match='not_found'):
        s.resolve_reference(key, 'wa-identities', '1234567890@c.us')
    assert s.resolve_reference(key, 'wa-identities', '900@lid')['chat']['id'] == visible_chat


def test_alias_requires_visible_source_even_when_original_target_is_visible(identity_archive):
    s, _, state, key = identity_archive
    source = state.apply(observed(chat='999@g.us'))
    target = observed(mid=2)
    del target['event']['identityObservations']
    result = state.apply(target)
    target_chat = s.get_context(key, result['message'])['message']['chat']
    s.set_access('identity-reader', chats=[target_chat])
    with pytest.raises(APIError, match='not_found'):
        s.resolve_reference(key, 'wa-identities', '1234567890@c.us')
    assert s.resolve_reference(key, 'wa-identities', '900@lid')['chat']['id'] == target_chat
    s.set_access('identity-reader', chats=None)
    assert s.resolve_reference(key, 'wa-identities', '1234567890@c.us')['chat']['id'] == target_chat
    with s.pool.connection() as c:
        c.execute('UPDATE messages SET deleted=true WHERE id=%s', (source['message'],))
    with pytest.raises(APIError, match='not_found'):
        s.resolve_reference(key, 'wa-identities', '1234567890@c.us')


def test_alias_ambiguity_only_counts_authorized_original_chats(identity_archive):
    s, _, state, key = identity_archive
    first = state.apply(observed())
    state.apply(observed(mid=2, chat='1234567890@c.us'))
    chat = s.get_context(key, first['message'])['message']['chat']
    s.set_access('identity-reader', chats=[chat])
    assert s.resolve_reference(key, 'wa-identities', '1234567890:2@c.us')['chat']['id'] == chat
    s.set_access('identity-reader', accounts=['telegram-demo'], chats=None)
    with pytest.raises(APIError, match='not_found'):
        s.resolve_reference(key, 'wa-identities', '1234567890:2@c.us')


@pytest.fixture()
def pair_archive(seeded):
    from datetime import datetime, timedelta, timezone
    from messaging_service.archive import Archive
    from test_archive import event
    s, *_ = seeded
    ar = Archive(s)
    ar.register('wa-pair', 'whatsapp', 'Synthetic pair', discover=True)
    key = s.issue_key('wa-pair-agent', ['wa-pair'])
    def add(chat, n, hours, direction='incoming', sender=None, chat_type='private', title=None, at=None):
        at = at or (datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat()
        return ar.ingest(event(account='wa-pair', platform='whatsapp', chat=chat, chat_title=title or chat,
            sender=sender or (chat if direction == 'incoming' else '555@c.us'), direction=direction,
            provider_id=f'{str(direction == "outgoing").lower()}_{chat}_{n}', body=f'Synthetic {n}',
            sent_at=at, updated_at=at, metadata={'chat_type': chat_type}))['message']
    def link(source, pn, lid, provenance='whatsmeow_message_alt_v1', identity=None):
        with s.pool.connection() as c:
            sender = identity or c.execute('SELECT sender FROM messages WHERE id=%s', (source,)).fetchone()['sender']
            if identity is None:
                # As whatsapp_identity.record(): sender evidence also gives a directory entry.
                c.execute('''INSERT INTO sender_profiles(id,account,chat,sender,source_message,source_sent_at)
                    SELECT 'person-'||m.id,m.account,m.chat,m.sender,m.id,m.sent_at FROM messages m WHERE m.id=%s
                    ON CONFLICT(account,chat,sender) DO NOTHING''', (source,))
            c.execute('''INSERT INTO whatsapp_identity_observations(account,source_message,identity_id,
                pn,lid,provenance,first_observed_at,observed_at) VALUES('wa-pair',%s,%s,%s,%s,%s,now(),now())''',
                (source, sender, pn, lid, provenance))
    def chats(**kwargs):
        rows = s.list_chats(key, include_coverage=False, **kwargs)['items']
        return {r['provider_chat_id']: r for r in rows if r['account'] == 'wa-pair'}
    def chat_id(message):
        return s.get_context(key, message)['message']['chat']
    return s, key, add, link, chats, chat_id


# spec: ANYM-278
@pytest.mark.parametrize('order', ['id', 'activity'])
def test_pn_and_lid_chats_of_one_peer_are_listed_once_without_rekeying(pair_archive, order):
    s, key, add, link, chats, chat_id = pair_archive
    old = add('111@c.us', 1, 5, title='Alice Synthetic')
    new = add('222@lid', 2, 1)
    link(new, '111@c.us', '222@lid')
    rows = chats(order=order)
    assert list(rows) == ['222@lid']
    row = rows['222@lid']
    assert row['id'] == chat_id(new)
    assert [(a['id'], a['provider_chat_id']) for a in row['alias_chats']] == [(chat_id(old), '111@c.us')]
    assert row['alias_chats'][0]['last_activity'] is not None
    assert (row['alias_provenance'], row['alias_confidence']) == ('whatsmeow_message_alt_v1', 'provider_cache_observation')
    # Stored chats and messages keep their own IDs and provider IDs.
    assert s.get_context(key, old)['message']['provider_id'] == 'false_111@c.us_1'
    assert s.get_context(key, new)['message']['provider_id'] == 'false_222@lid_2'
    from messaging_service.presentation import render
    lean = render('list_chats', s.list_chats(key, include_coverage=False, view='conversation'), 'compact')['items']
    assert [(r['alias_chats'][0]['id'], r['alias_provenance'], r['alias_confidence']) for r in lean if r['id'] == row['id']] == [
        (chat_id(old), 'whatsmeow_message_alt_v1', 'provider_cache_observation')]
    # The hidden chat's title still finds the conversation.
    assert list(chats(query='Alice Synthetic')) == ['222@lid']
    # A newer message in the phone-number chat makes it the listed chat.
    add('111@c.us', 3, 0.5)
    rows = chats(order=order)
    assert list(rows) == ['111@c.us'] and rows['111@c.us']['alias_chats'][0]['provider_chat_id'] == '222@lid'


# spec: ANYM-278
def test_conflicting_link_keeps_both_chats_and_shows_no_phone(pair_archive):
    s, key, add, link, chats, chat_id = pair_archive
    add('111@c.us', 1, 5)
    new = add('222@lid', 2, 1)
    other = add('333@lid', 3, 2)
    link(new, '111@c.us', '222@lid')
    link(other, '111@c.us', '333@lid')
    rows = chats()
    assert set(rows) == {'111@c.us', '222@lid', '333@lid'}
    for row in rows.values():
        assert 'alias_chats' not in row and 'peer_phone' not in row


# spec: ANYM-278
def test_grouping_needs_both_chats_and_a_visible_source_in_scope(pair_archive):
    s, key, add, link, chats, chat_id = pair_archive
    old = add('111@c.us', 1, 5)
    new = add('222@lid', 2, 1)
    group = add('999@g.us', 3, 3, sender='222@lid', chat_type='group')
    link(group, '111@c.us', '222@lid')
    assert set(chats()) == {'222@lid', '999@g.us'}
    old, new = chat_id(old), chat_id(new)
    s.set_access('wa-pair-agent', chats=[old])
    rows = chats(awaiting_reply=True, awaiting_reply_days=0)
    assert list(rows) == ['111@c.us'] and 'alias_chats' not in rows['111@c.us']
    assert s.list_chats(key, include_coverage=False, awaiting_reply=True, awaiting_reply_days=0)['awaiting_reply_total'] == 1
    # Both chats visible, but the only evidence sits in a chat the key cannot read.
    s.set_access('wa-pair-agent', chats=[old, new])
    rows = chats()
    assert set(rows) == {'111@c.us', '222@lid'}
    assert all('alias_chats' not in r and 'peer_phone' not in r for r in rows.values())


# spec: ANYM-278
def test_lid_chat_shows_its_verified_phone_number(pair_archive):
    s, key, add, link, chats, chat_id = pair_archive
    lid = add('222@lid', 1, 1)
    plain = add('444@lid', 2, 1)
    link(lid, '111@c.us', '222@lid', 'webjs_contact_cache_v1')
    rows = chats()
    assert (rows['222@lid']['peer_phone'], rows['222@lid']['peer_phone_provenance']) == ('+111', 'webjs_contact_cache_v1')
    assert 'peer_phone' not in rows['444@lid'] and 'alias_chats' not in rows['222@lid']
    link(plain, '111@c.us', '444@lid')
    assert all('peer_phone' not in r for r in chats().values())


# spec: ANYM-278
def test_lid_chat_phone_has_search_people_visibility(pair_archive):
    s, key, add, link, chats, chat_id = pair_archive
    from messaging_service.presentation import render
    # A recipient-only observation on the owner's message names no peer profile.
    sent = add('222@lid', 1, 2, direction='outgoing')
    link(sent, '111@c.us', '222@lid', identity='222@lid')
    assert 'peer_phone' not in chats()['222@lid']
    assert not [p for p in s.search_people(key, '111')['items'] if p.get('identity', {}).get('pn')]
    # The peer's own profile in that chat makes the same evidence searchable and shown.
    profile = add('222@lid', 2, 1)
    link(profile, '111@c.us', '222@lid')
    assert [p['identity']['pn'] for p in s.search_people(key, '111')['items'] if p.get('identity', {}).get('pn')] == ['111@c.us']
    row = chats()['222@lid']
    assert (row['peer_phone'], row['peer_phone_provenance']) == ('+111', 'whatsmeow_message_alt_v1')
    lean = render('list_chats', s.list_chats(key, include_coverage=False, view='conversation'), 'compact')['items']
    assert [(r['peer_phone'], r['peer_phone_provenance']) for r in lean if r['id'] == row['id']] == [('+111', 'whatsmeow_message_alt_v1')]
    # A deleted profile source hides the phone, as search_people hides the person.
    with s.pool.connection() as c:
        c.execute('UPDATE messages SET deleted=true WHERE id=%s', (profile,))
    assert 'peer_phone' not in chats()['222@lid']
    assert not [p for p in s.search_people(key, '111')['items'] if p.get('identity', {}).get('pn')]


# spec: ANYM-278
def test_device_qualified_phone_chat_groups_with_its_lid_chat(pair_archive):
    s, key, add, link, chats, chat_id = pair_archive
    old = add('111:4@c.us', 1, 5)
    new = add('222@lid', 2, 1)
    link(new, '111@c.us', '222@lid')
    rows = chats()
    assert list(rows) == ['222@lid'] and rows['222@lid']['alias_chats'][0]['id'] == chat_id(old)
    # Two stored chats with one normalized phone are ambiguous: nothing is grouped.
    add('111@c.us', 3, 4)
    assert set(chats()) == {'111:4@c.us', '111@c.us', '222@lid'}
    # Two stored LID chats with one normalized LID group nothing but keep their phone.
    device = add('222:4@lid', 4, 3)
    link(device, '111@c.us', '222@lid')
    rows = chats()
    assert set(rows) == {'111:4@c.us', '111@c.us', '222@lid', '222:4@lid'}
    assert all('alias_chats' not in r for r in rows.values())
    assert rows['222@lid']['peer_phone'] == rows['222:4@lid']['peer_phone'] == '+111'


# spec: ANYM-278
@pytest.mark.parametrize('incoming_chat', ['111@c.us', '222@lid'])
def test_same_second_tie_keeps_the_incoming_hint(pair_archive, incoming_chat):
    from datetime import datetime, timedelta, timezone
    s, key, add, link, chats, chat_id = pair_archive
    at = (datetime.now(timezone.utc)-timedelta(hours=1)).replace(microsecond=0).isoformat()
    outgoing_chat = '222@lid' if incoming_chat == '111@c.us' else '111@c.us'
    incoming = add(incoming_chat, 1, 0, at=at)
    add(outgoing_chat, 2, 0, direction='outgoing', at=at)
    link(incoming, '111@c.us', '222@lid', identity=incoming_chat)
    assert list(chats()) == [incoming_chat]
    queue = s.list_chats(key, awaiting_reply=True, awaiting_reply_days=0, include_coverage=False)
    assert [r['awaiting_reply_message_id'] for r in queue['items']] == [incoming] and queue['awaiting_reply_total'] == 1


# spec: ANYM-278
@pytest.mark.parametrize('stored', ['', '222', '+222', '222@lid'])
def test_number_only_title_borrows_the_alias_chat_name(pair_archive, stored):
    s, key, add, link, chats, chat_id = pair_archive
    add('111@c.us', 1, 5, title='Alice Synthetic')
    new = add('222@lid', 2, 1, title=stored or ' ')
    link(new, '111@c.us', '222@lid')
    row = chats()['222@lid']
    assert (row['title'], row['title_basis']) == ('Alice Synthetic', 'alias_chat')


# spec: ANYM-278
def test_linked_pages_exclusions_and_selected_chat_operations(pair_archive):
    from datetime import datetime, timedelta, timezone
    s, key, add, link, chats, chat_id = pair_archive
    old = add('111@c.us', 1, 5)
    new = add('222@lid', 2, 1)
    others = [add(f'{n}@c.us', 10+n, n) for n in (2, 3, 4)]
    link(new, '111@c.us', '222@lid')
    seen, after = [], ''
    while True:
        page = s.list_chats(key, include_coverage=False, limit=1, after=after, accounts=['wa-pair'])
        seen += [r['id'] for r in page['items']];after = page['next_cursor']
        assert len(page['items']) == 1
        if not after:break
    assert sorted(seen) == sorted([chat_id(new), *map(chat_id, others)])
    # Excluding either chat of the conversation excludes the conversation.
    for excluded in (chat_id(old), chat_id(new)):
        assert '222@lid' not in chats(exclude_chats=[excluded]) and '111@c.us' not in chats(exclude_chats=[excluded])
    # dismiss and snooze select the listed chat by ID; the hidden chat is not listed.
    queue = s.list_chats(key, awaiting_reply=True, awaiting_reply_days=0, include_coverage=False, accounts=['wa-pair'])
    assert chat_id(old) not in [r['id'] for r in queue['items']]
    assert s.dismiss_reply(key, chat_id(new), new, 1)['dismissed'] is True
    queue = s.list_chats(key, awaiting_reply=True, awaiting_reply_days=0, include_coverage=False, accounts=['wa-pair'])
    assert sorted(r['id'] for r in queue['items']) == sorted(map(chat_id, others))
    # A newer incoming message in the hidden phone chat makes it the listed chat; snooze it.
    pn_new = add('111@c.us', 20, 0.5)
    queue = s.list_chats(key, awaiting_reply=True, awaiting_reply_days=0, include_coverage=False, accounts=['wa-pair'])
    assert chat_id(new) not in [r['id'] for r in queue['items']] and chat_id(old) in [r['id'] for r in queue['items']]
    until = (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
    assert s.snooze_reply(key, chat_id(old), pn_new, 1, until)['snoozed'] is True
    queue = s.list_chats(key, awaiting_reply=True, awaiting_reply_days=0, include_coverage=False, accounts=['wa-pair'])
    assert sorted(r['id'] for r in queue['items']) == sorted(map(chat_id, others))


# spec: ANYM-458
def test_incoming_overview_counts_a_linked_pn_and_lid_conversation_once(pair_archive):
    from datetime import datetime, timedelta, timezone
    s, key, add, link, chats, chat_id = pair_archive
    since = (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
    def overview():
        page = s.get_updates(key, 'overview', view='by_chat', from_time=since, accounts=['wa-pair'])
        return page, {r['chat']: r for r in page['chats']}
    old = add('111@c.us', 1, 5)
    new = add('222@lid', 2, 1)
    other = add('333@c.us', 3, 2)
    # Without a verified link the archive chats stay apart.
    page, rows = overview()
    assert set(rows) == {chat_id(old), chat_id(new), chat_id(other)} and page['chats_total'] == 3
    link(new, '111@c.us', '222@lid')
    page, rows = overview()
    assert set(rows) == {chat_id(new), chat_id(other)}
    assert (page['chats_total'], page['live_total']) == (2, 3)
    row = rows[chat_id(new)]
    assert (row['live'], row['newest']['message']) == (2, new)
    assert row['alias_chats'] == [{'id': chat_id(old)}]
    assert (row['alias_provenance'], row['alias_confidence']) == ('whatsmeow_message_alt_v1', 'provider_cache_observation')
    assert 'owner_replied_after_newest' not in row and 'alias_chats' not in rows[chat_id(other)]
    # A newer message in the phone chat shows the conversation under it; an owner reply in either chat counts.
    pn_new = add('111@c.us', 4, 0.5)
    add('222@lid', 5, 0.25, direction='outgoing')
    page, rows = overview()
    row = rows[chat_id(old)]
    assert chat_id(new) not in rows and (row['live'], row['newest']['message']) == (3, pn_new)
    assert row['alias_chats'] == [{'id': chat_id(new)}] and row['owner_replied_after_newest'] is True
