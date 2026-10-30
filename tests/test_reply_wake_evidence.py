"""Native adapter reply IDs produce bounded, immutable wake evidence."""
from datetime import datetime, timezone

import pytest

from messaging_service.adapters import telegram_message, whatsapp_message
from messaging_service.archive import Archive
from messaging_service.envelope import stable_id
from messaging_service.subscriptions import match_subscriptions


def tg(account, message_id, *, outgoing=False, reply=None, chat=100):
    from telethon import types
    message = types.Message(id=message_id, peer_id=types.PeerUser(chat), out=outgoing,
        from_id=types.PeerUser(42 if outgoing else chat), message='Synthetic reply fixture',
        date=datetime(2026, 10, 15, tzinfo=timezone.utc),
        reply_to=types.MessageReplyHeader(reply_to_msg_id=reply) if reply else None)
    return telegram_message(account, message)


def wa(account, chat, provider_id, *, outgoing=False, reply=None, sender='owner_A@c.us'):
    return whatsapp_message(account, dict(chatId=chat, messageId=provider_id,
        senderId=sender, body='Synthetic WhatsApp fixture', timestamp=1792022400,
        fromMe=outgoing, hasMedia=False, replyToId=reply, replyToOwner=None))


def key(store, account):
    return store.issue_key('native-reader', [account], permissions=['read', 'subscription-write'],
        key_permissions=['read', 'subscription-write'])


def confirmed_send(store, token, account, provider_chat, provider_id):
    with store.authorized(token) as (_, p):
        owner = p
    with store.pool.connection() as c:
        c.execute('''INSERT INTO outgoing_messages(id,principal,key_id,scope_version,key_permission_version,
            request_id,request_hash,account,chat,provider_chat,body,status,provider_message_id)
            VALUES('send_native',%s,%s,%s,%s,'native-anchor',%s,%s,%s,%s,'Synthetic','sent',%s)''',
            (owner['id'], owner['key_id'], owner['scope_version'], owner['key_permission_version'],
             'a' * 64, account, stable_id('chat', account, provider_chat), provider_chat, provider_id))


def test_telegram_owner_reply_uses_frozen_exact_capture_target(seeded):
    store, _, _ = seeded
    archive = Archive(store)
    archive.register('tg-native', 'telegram', 'Synthetic', discover=True)
    target = archive.ingest(tg('tg-native', 19, outgoing=True))['message']
    token = key(store, 'tg-native')
    sid = store.create_subscription(token, [stable_id('chat', 'tg-native', '100')],
        wake_rules=['reply_to_owner'])['subscription_id']
    incoming = tg('tg-native', 20, reply=19)
    assert incoming['metadata'].get('reply_to_owner') is None
    message = archive.ingest(incoming)['message']
    # Matching must retain the evidence captured before the target disappears.
    with store.pool.connection() as c:
        c.execute('UPDATE messages SET deleted=true,revision=revision+1 WHERE id=%s', (target,))
    match_subscriptions(store)
    page = store.get_subscription_events(token, sid)
    assert [item['message'] for item in page['items']] == [message]
    item = page['items'][0]
    assert 'reply_to_owner' in item['wake_reason']
    assert item['metadata'].get('reply_to_owner') is None
    assert item['reply_evidence'] == dict(basis='exact_native_reply_to_archived_outgoing',
        target_message=target, target_revision=1, target_provider_id='19')
    assert store.get_subscription_events(token, sid)['items'] == page['items']


def test_telegram_missing_or_other_chat_target_is_never_inferred_later(seeded):
    store, _, _ = seeded
    archive = Archive(store)
    archive.register('tg-native', 'telegram', 'Synthetic', discover=True)
    archive.ingest(tg('tg-native', 19, outgoing=True, chat=101))
    archive.ingest(tg('tg-native', 1))
    token = key(store, 'tg-native')
    sid = store.create_subscription(token, [stable_id('chat', 'tg-native', '100')],
        wake_rules=['reply_to_owner'])['subscription_id']
    archive.ingest(tg('tg-native', 20, reply=19))
    archive.ingest(tg('tg-native', 19, outgoing=True))
    match_subscriptions(store)
    assert store.get_subscription_events(token, sid)['items'] == []


@pytest.mark.parametrize('chat,stanza,sender,with_participant', [
    ('111111@c.us', 'STANZA_A_B', 'owner_A@c.us', False),
    ('111111@lid', 'STANZA_A_B', 'owner_A@lid', False),
    ('team_A@g.us', 'STANZA_A_B', 'owner_A@c.us', True),
    ('team_A@g.us', 'STANZA_A_B', 'owner_A@lid', True),
    ('team_A@g.us', 'STANZA_A_B', 'owner_A@c.us', False),
])
def test_whatsapp_native_stanza_matches_frozen_confirmed_receipt(seeded, chat, stanza, sender, with_participant):
    store, _, _ = seeded
    archive = Archive(store)
    archive.register('wa-native', 'whatsapp', 'Synthetic', discover=True)
    provider_id = f'true_{chat}_{stanza}' + ('_' + sender if with_participant else '')
    target = archive.ingest(wa('wa-native', chat, provider_id, outgoing=True, sender=sender))['message']
    token = key(store, 'wa-native')
    confirmed_send(store, token, 'wa-native', chat, provider_id)
    sid = store.create_subscription(token, [stable_id('chat', 'wa-native', chat)],
        wake_rules=['reply_to_confirmed_send'], reply_to_send='send_native')['subscription_id']
    incoming = wa('wa-native', chat, f'false_{chat}_INCOMING', reply=stanza, sender='other@c.us')
    assert incoming['metadata']['reply_to_provider_id'] == stanza
    result = archive.ingest(incoming)
    # Projection and identity proof are immutable once the watch is created.
    with store.pool.connection() as c:
        c.execute('UPDATE messages SET deleted=true,revision=revision+1 WHERE id=%s', (target,))
    match_subscriptions(store)
    page = store.get_subscription_events(token, sid)
    assert [item['message'] for item in page['items']] == [result['message']]
    assert 'reply_to_confirmed_send' in page['items'][0]['wake_reason']
    with store.pool.connection() as c:
        assert c.execute('SELECT provider_id FROM messages WHERE id=%s', (target,)).fetchone()['provider_id'] == provider_id
        anchor = c.execute('SELECT selector FROM subscriptions WHERE id=%s', (sid,)).fetchone()['selector']['reply_anchor']
        assert anchor['provider_id'] == provider_id and anchor['native_reply_id'] == stanza
        assert anchor['provider_chat'] == chat and anchor['target_revision'] == 1


@pytest.mark.parametrize('target_state', ['missing', 'ambiguous', 'wrong_sender'])
def test_whatsapp_unknown_projection_cannot_guess_group_stanza(seeded, target_state):
    store, _, _ = seeded
    archive = Archive(store)
    archive.register('wa-native', 'whatsapp', 'Synthetic', discover=True)
    chat, stanza, sender = 'team_A@g.us', 'STANZA_A', 'owner_A@c.us'
    provider_id = f'true_{chat}_{stanza}_{sender}'
    archive.ingest(wa('wa-native', chat, f'false_{chat}_FIRST'))
    if target_state != 'missing':
        archive.ingest(wa('wa-native', chat, provider_id, outgoing=True,
            sender='' if target_state == 'ambiguous' else 'unknown@c.us'))
    token = key(store, 'wa-native')
    confirmed_send(store, token, 'wa-native', chat, provider_id)
    sid = store.create_subscription(token, [stable_id('chat', 'wa-native', chat)],
        wake_rules=['reply_to_confirmed_send'], reply_to_send='send_native')['subscription_id']
    archive.ingest(wa('wa-native', chat, f'false_{chat}_REPLY', reply=stanza))
    match_subscriptions(store)
    assert store.get_subscription_events(token, sid)['items'] == []


def test_whatsapp_projection_does_not_cross_chats_or_pn_lid_namespaces(seeded):
    store, _, _ = seeded
    archive = Archive(store)
    archive.register('wa-native', 'whatsapp', 'Synthetic', discover=True)
    provider_id = 'true_111111@c.us_STANZA_A'
    archive.ingest(wa('wa-native', '111111@c.us', provider_id, outgoing=True))
    archive.ingest(wa('wa-native', '111111@lid', 'false_111111@lid_FIRST'))
    token = key(store, 'wa-native')
    confirmed_send(store, token, 'wa-native', '111111@c.us', provider_id)
    sid = store.create_subscription(token, [stable_id('chat', 'wa-native', chat)
        for chat in ('111111@c.us', '111111@lid')], wake_rules=['reply_to_confirmed_send'],
        reply_to_send='send_native')['subscription_id']
    archive.ingest(wa('wa-native', '111111@lid', 'false_111111@lid_REPLY', reply='STANZA_A'))
    match_subscriptions(store)
    assert store.get_subscription_events(token, sid)['items'] == []


def test_whatsapp_bare_legacy_sender_cannot_be_removed_from_stanza(seeded):
    store, _, _ = seeded
    archive = Archive(store)
    archive.register('wa-native', 'whatsapp', 'Synthetic', discover=True)
    chat = 'team_A@g.us'
    provider_id = f'true_{chat}_STANZA_B'
    archive.ingest(wa('wa-native', chat, provider_id, outgoing=True, sender='B'))
    token = key(store, 'wa-native')
    confirmed_send(store, token, 'wa-native', chat, provider_id)
    sid = store.create_subscription(token, [stable_id('chat', 'wa-native', chat)],
        wake_rules=['reply_to_confirmed_send'], reply_to_send='send_native')['subscription_id']
    archive.ingest(wa('wa-native', chat, f'false_{chat}_REPLY', reply='STANZA'))
    match_subscriptions(store)
    assert store.get_subscription_events(token, sid)['items'] == []


def test_whatsapp_projected_stanza_rejects_explicit_other_participant(seeded):
    store, _, _ = seeded
    archive = Archive(store)
    archive.register('wa-native', 'whatsapp', 'Synthetic', discover=True)
    chat = 'team_A@g.us'
    provider_id = f'true_{chat}_STANZA_owner_A@c.us'
    archive.ingest(wa('wa-native', chat, provider_id, outgoing=True))
    token = key(store, 'wa-native')
    confirmed_send(store, token, 'wa-native', chat, provider_id)
    sid = store.create_subscription(token, [stable_id('chat', 'wa-native', chat)],
        wake_rules=['reply_to_confirmed_send'], reply_to_send='send_native')['subscription_id']
    incoming = wa('wa-native', chat, f'false_{chat}_REPLY', reply='STANZA')
    incoming['metadata']['reply_to_owner'] = False
    archive.ingest(incoming)
    match_subscriptions(store)
    assert store.get_subscription_events(token, sid)['items'] == []


def test_reply_evidence_cannot_be_supplied_in_envelopes_or_malformed_event_metadata(seeded):
    from messaging_service.envelope import validate
    store, _, _ = seeded
    with pytest.raises(ValueError, match='unknown_envelope_fields'):
        validate({**tg('tg-native', 20, reply=19), 'reply_evidence': {'basis': 'invented'}})
    for proof in [None, {'basis': 'invented'}, dict(basis='exact_native_reply_to_archived_outgoing',
            target_message='demo-tg-1', target_revision=True, target_provider_id='19')]:
        with store.pool.connection() as c, pytest.raises(ValueError, match='invalid_event_metadata'):
            store.append_event(c, 'telegram-demo', 'demo-tg-1', snapshot_metadata={'reply_evidence': proof})
