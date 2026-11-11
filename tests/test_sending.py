"""Outbound contracts use isolated PostgreSQL and synthetic providers only."""
import asyncio
from datetime import datetime, timezone

import pytest
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.collector_state import CollectorState
from messaging_service.envelope import stable_id
from messaging_service.store import APIError
from test_archive import event


@pytest.fixture
def sender(seeded):
    s, reader, _ = seeded
    archive = Archive(s)
    archive.register('send-fixture', 'telegram', 'Synthetic sender', discover=True)
    archive.ingest(event(account='send-fixture', chat='100', provider_id='1', body='Existing synthetic message'))
    key = s.issue_key('send-agent', ['send-fixture'], permissions=['read', 'send'])
    epoch = archive.acquire('send-fixture', 'synthetic-send-worker')
    state = CollectorState(archive, 'send-fixture', ('synthetic-send-worker', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    archive.set_sending('send-fixture', True)
    yield s, archive, key, reader, state
    archive.release('send-fixture', 'synthetic-send-worker', epoch)


def submit(s, key, **changes):
    return s.send_message(key, account='send-fixture',
        chat_id=stable_id('chat', 'send-fixture', '100'), text='Exact synthetic text',
        request_id='synthetic-request-0001', **changes)


def test_read_only_key_cannot_send(seeded):
    s, key, _ = seeded
    with pytest.raises(APIError) as error:
        s.send_message(key, account='telegram-demo', chat_id='telegram-demo-chat',
            text='Synthetic', request_id='synthetic-request-0001')
    assert error.value.code == 'forbidden'


def test_idempotency_and_account_chat_scope(sender):
    s, _, key, reader, _ = sender
    first = submit(s, key)
    assert first['status'] == 'queued' and first['provider_message_id'] is None
    assert submit(s, key)['send_id'] == first['send_id']
    with pytest.raises(APIError, match='idempotency_conflict'):
        s.send_message(key, account='send-fixture', chat_id=first['chat_id'],
            text='Different synthetic text', request_id='synthetic-request-0001')
    with pytest.raises(APIError, match='forbidden'):
        s.get_send_status(reader, first['send_id'])
    other = s.issue_key('other-send-agent', ['send-fixture'], permissions=['read', 'send'])
    with pytest.raises(APIError, match='not_found'):
        s.get_send_status(other, first['send_id'])
    s.set_access('send-agent', chats=[])
    with pytest.raises(APIError, match='not_found'):
        submit(s, key)
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM outgoing_messages').fetchone()['n'] == 1
        rows = c.execute("SELECT resource_ids FROM audit_log WHERE operation='send_message'").fetchall()
        assert 'Exact synthetic text' not in str(rows)


@pytest.mark.parametrize('text', ['', '  ', '\0', '😀' * 2001])
def test_invalid_text_never_queues(sender, text):
    s, _, key, _, _ = sender
    with pytest.raises(APIError, match='invalid_message_text'):
        s.send_message(key, account='send-fixture', chat_id=stable_id('chat','send-fixture','100'),
            text=text, request_id='synthetic-request-0001')


def test_operator_account_gate_and_reply_target(sender):
    s, archive, key, _, _ = sender
    archive.set_sending('send-fixture', False)
    with pytest.raises(APIError, match='sending_disabled'):
        submit(s, key)
    archive.set_sending('send-fixture', True)
    with pytest.raises(APIError, match='not_found'):
        submit(s, key, reply_to='demo-tg-1')
    result = submit(s, key, reply_to=stable_id('msg', 'send-fixture', '100', '1'))
    assert result['status'] == 'queued'


def preview_of(s, key, **changes):
    return s.preview_message(key, account='send-fixture', chat_id=stable_id('chat', 'send-fixture', '100'),
        text='Exact synthetic text', **changes)


def queued_count(s):
    with s.pool.connection() as c:
        return c.execute('SELECT count(*) AS n FROM outgoing_messages').fetchone()['n']


# spec: ANYM-927
def test_a_silent_preview_says_so_and_its_digest_binds_silent_both_ways(sender):
    s, _, key, _, _ = sender
    silent, loud = preview_of(s, key, silent=True), preview_of(s, key)
    assert silent['silent'] is True and silent['render_policy']['silent'] is True
    assert 'silent' not in loud and 'silent' not in loud['render_policy']
    assert preview_of(s, key, silent=False) == loud
    assert silent['preview_digest'] != loud['preview_digest']
    with pytest.raises(APIError, match='preview_required'):
        submit(s, key, silent=True)
    with pytest.raises(APIError, match='preview_mismatch'):
        submit(s, key, silent=True, preview_digest=loud['preview_digest'])
    with pytest.raises(APIError, match='preview_mismatch'):
        submit(s, key, preview_digest=silent['preview_digest'])
    assert queued_count(s) == 0
    for bad in ('true', 1, None):
        with pytest.raises(APIError, match='invalid_parameters'):
            submit(s, key, silent=bad, preview_digest=silent['preview_digest'])
    receipt = submit(s, key, silent=True, preview_digest=silent['preview_digest'])
    assert receipt['status'] == 'queued'
    # An exact retry returns the same send; the same request_id sent loud is a conflict.
    assert submit(s, key, silent=True, preview_digest=silent['preview_digest'])['send_id'] == receipt['send_id']
    with pytest.raises(APIError, match='idempotency_conflict'):
        submit(s, key, preview_digest=silent['preview_digest'])
    with s.pool.connection() as c:
        row = c.execute('SELECT render_policy FROM outgoing_messages').fetchone()
    assert row['render_policy'] == {'link_preview': False, 'allow_lossy': False, 'silent': True}


# spec: ANYM-927
def test_silent_on_whatsapp_is_the_field_error_silent_unsupported_and_queues_nothing(sender):
    s, archive, _, _, _ = sender
    archive.register('wa-silent', 'whatsapp', 'Synthetic WhatsApp', discover=True)
    archive.ingest(event(account='wa-silent', platform='whatsapp', chat='fixture@c.us', provider_id='1',
        body='Existing synthetic message'))
    archive.set_sending('wa-silent', True)
    key = s.issue_key('wa-silent-agent', ['wa-silent'], permissions=['read', 'send'])
    chat = stable_id('chat', 'wa-silent', 'fixture@c.us')
    calls = [lambda: s.preview_message(key, account='wa-silent', chat_id=chat, text='Synthetic', silent=True),
             lambda: s.send_message(key, account='wa-silent', chat_id=chat, text='Synthetic',
                 request_id='wa-silent-request-1', silent=True, preview_digest='a' * 64)]
    for call in calls:
        with pytest.raises(APIError, match='silent_unsupported') as refused:
            call()
        assert refused.value.field_errors == [{'field': 'silent', 'reason': 'enum',
            'expected': 'false: WhatsApp has no silent send', 'allowed': [False]}]
    assert queued_count(s) == 0
    assert s.preview_message(key, account='wa-silent', chat_id=chat, text='Synthetic', silent=False)['account'] == 'wa-silent'


GROUP = stable_id('chat', 'send-fixture', '-500')


def tg_group(archive):
    """A Telegram group where Анна (42) wrote today and Борис (99) wrote a year ago."""
    for provider_id, sender, name, at in (('1', '42', 'Анна', '2026-11-10T12:00:00+00:00'),
                                          ('2', '99', 'Борис', '2025-11-10T12:00:00+00:00')):
        archive.ingest(event(account='send-fixture', chat='-500', chat_title='Synthetic family', provider_id=provider_id,
            sender=sender, body='Synthetic hello', sent_at=at, updated_at=at, metadata={'chat_type': 'group'},
            sender_profile={'display_name': name}))
    return GROUP


def members_read(s, key, account, chat, ids, *, minutes_ago=0, complete=True, request_id='synthetic-members-0001'):
    """A finished list_participants read of the chat by this key (synthetic: the row a collector would leave)."""
    s.set_messenger_operations(account, ['list_participants'])
    queued = s.execute_messenger_operation(key, account, 'list_participants', chat, parameters={'limit': 100},
                                           request_id=request_id)
    data = {'participants': [{'id': i, 'first_name': 'Member'} for i in ids], 'next_offset': None if complete else 100}
    with s.pool.connection() as c:
        c.execute('''UPDATE messenger_commands SET status='succeeded',result=%s,finished_at=clock_timestamp(),
            created_at=clock_timestamp()-make_interval(mins=>%s) WHERE id=%s''',
            (Jsonb({'outcome': 'confirmed', 'data': data}), minutes_ago, queued['operation_id']))


def group_key(s):
    return s.issue_key('mention-agent', ['send-fixture'], permissions=['read', 'send', 'groups-read'])


def mention_preview(s, key, text, mentions, chat=GROUP, account='send-fixture', **changes):
    return s.preview_message(key, account=account, chat_id=chat, text=text, mentions=mentions, **changes)


def mention_send(s, key, text, mentions, digest, request_id='mention-request-0001', chat=GROUP, account='send-fixture',
                 **changes):
    return s.send_message(key, account=account, chat_id=chat, text=text, request_id=request_id, mentions=mentions,
                          preview_digest=digest, **changes)


# spec: ANYM-928
def test_a_telegram_mention_preview_names_who_is_notified_and_its_digest_binds_the_mentions(sender):
    s, archive, _, _, _ = sender
    tg_group(archive)
    key = group_key(s)
    text, anna = 'Привет, @Анна! Спроси @school_bot', [{'sender': '42', 'offset': 8, 'length': 5}]
    draft = mention_preview(s, key, text, anna)
    assert draft['mentions_notified'] == [{'sender': '42', 'display_name': 'Анна', 'span_text': '@Анна'}]
    assert draft['membership_basis'] == 'archive' and draft['notified_by_username'] == ['school_bot']
    assert draft['mentions'] == [{'offset': 8, 'length': 5, 'user_id': '42'}]
    assert draft['provider_payload'] == {'text': text, 'entities': [], 'link_preview': False}
    plain = mention_preview(s, key, text, None)
    assert 'mentions' not in plain and 'mentions_notified' not in plain and mention_preview(s, key, text, []) == plain
    moved = mention_preview(s, key, text, [{'sender': '42', 'offset': 9, 'length': 4}])
    added = mention_preview(s, key, text, [*anna, {'sender': '99', 'offset': 0, 'length': 6}])
    assert len({draft['preview_digest'], plain['preview_digest'], moved['preview_digest'], added['preview_digest']}) == 4
    with pytest.raises(APIError, match='preview_required'):
        mention_send(s, key, text, anna, '')
    for digest in (plain['preview_digest'], moved['preview_digest'], added['preview_digest']):
        with pytest.raises(APIError, match='preview_mismatch'):
            mention_send(s, key, text, anna, digest)
    with pytest.raises(APIError, match='preview_mismatch'):
        mention_send(s, key, text, None, draft['preview_digest'])
    assert queued_count(s) == 0
    receipt = mention_send(s, key, text, anna, draft['preview_digest'])
    assert receipt['status'] == 'queued'
    assert mention_send(s, key, text, anna, draft['preview_digest'])['send_id'] == receipt['send_id']
    with pytest.raises(APIError, match='idempotency_conflict'):
        mention_send(s, key, text, None, draft['preview_digest'])
    with s.pool.connection() as c:
        row = c.execute('SELECT render_policy,render_artifact FROM outgoing_messages').fetchone()
    assert row['render_policy'] == {'link_preview': False, 'allow_lossy': False, 'mentions': anna}
    assert row['render_artifact']['mentions'] == draft['mentions']


# spec: ANYM-928
def test_mention_spans_are_checked_against_the_text_and_refusals_queue_nothing(sender):
    s, archive, _, _, _ = sender
    tg_group(archive)
    key = group_key(s)
    cases = [('Hi @Анна', [{'sender': '42', 'offset': 3, 'length': 6}], 'mention_out_of_range', 0),
             ('😀 @Анна', [{'sender': '42', 'offset': 1, 'length': 2}], 'mention_out_of_range', 0),
             ('Hi @Анна', [{'sender': '42', 'offset': 3, 'length': 5}, {'sender': '99', 'offset': 4, 'length': 2}],
              'mention_overlap', 1),
             ('Hi @Анна ', [{'sender': '42', 'offset': 3, 'length': 6}], 'mention_out_of_range', 0)]
    for text, mentions, code, index in cases:
        with pytest.raises(APIError, match=code) as refused:
            mention_preview(s, key, text, mentions)
        assert refused.value.index == index and refused.value.text_utf16_length == len(text.encode('utf-16-le')) // 2
        with pytest.raises(APIError, match=code):
            mention_send(s, key, text, mentions, 'a' * 64)
    link = mention_preview(s, key, '[@Анна](https://example.org) и **@Анна**', None, format='markdown')
    assert link['rendered_text'] == '@Анна и @Анна'
    with pytest.raises(APIError, match='mention_overlap'):
        mention_preview(s, key, '[@Анна](https://example.org) и **@Анна**', [{'sender': '42', 'offset': 1, 'length': 5}],
                        format='markdown')
    bold = mention_preview(s, key, '[@Анна](https://example.org) и **@Анна**', [{'sender': '42', 'offset': 33, 'length': 5}],
                           format='markdown')
    assert bold['mentions'] == [{'offset': 8, 'length': 5, 'user_id': '42'}]
    assert bold['mentions_notified'][0]['span_text'] == '@Анна'
    for bad in ('x', [{'sender': '42', 'offset': 0}], [{'sender': '42', 'offset': -1, 'length': 1}],
                [{'sender': '42', 'offset': 0, 'length': 1}] * 21):
        with pytest.raises(APIError, match='invalid_parameters'):
            mention_preview(s, key, 'Hi @Анна', bad)
    with pytest.raises(APIError, match='mentions_not_group'):
        mention_preview(s, key, 'Hi @Анна', [{'sender': '42', 'offset': 3, 'length': 5}],
                        chat=stable_id('chat', 'send-fixture', '100'))
    assert queued_count(s) == 0
    with TestClient(create_app(s, ['testserver'])) as client:
        body = client.post('/v1/preview_message', headers={'Authorization': 'Bearer ' + key},
            json={'account': 'send-fixture', 'chat_id': GROUP, 'text': 'Hi @Анна',
                  'mentions': [{'sender': '42', 'offset': 3, 'length': 9}]}).json()
        private = client.post('/v1/preview_message', headers={'Authorization': 'Bearer ' + key},
            json={'account': 'send-fixture', 'username': '@synthetic_person', 'text': 'Hi @Анна',
                  'mentions': [{'sender': '42', 'offset': 3, 'length': 5}]}).json()
    assert body['error'] == 'mention_out_of_range' and body['index'] == 0 and body['text_utf16_length'] == 8
    assert body['field_errors'][0]['field'] == 'mentions/0' and 'Анна' not in str(body)
    assert private['error'] == 'mentions_not_group'


# spec: ANYM-928
def test_membership_is_the_participants_read_and_only_without_one_the_archive(sender):
    s, archive, _, _, _ = sender
    tg_group(archive)
    key = group_key(s)
    newcomer, gone = [{'sender': '77', 'offset': 3, 'length': 5}], [{'sender': '99', 'offset': 3, 'length': 5}]
    # No read held: the archive decides. The newcomer never wrote; the member of a year ago did.
    with pytest.raises(APIError, match='mention_not_member') as refused:
        mention_preview(s, key, 'Hi @Вова', newcomer)
    assert refused.value.next_call == {'tool': 'list_participants',
                                       'arguments': {'account': 'send-fixture', 'chat_id': GROUP}}
    assert mention_preview(s, key, 'Hi @Боря', gone)['membership_basis'] == 'archive'
    # A read of a minute ago: the member added a minute ago passes, the one who left fails.
    members_read(s, key, 'send-fixture', GROUP, ['42', '77'], minutes_ago=1)
    draft = mention_preview(s, key, 'Hi @Вова', newcomer)
    assert draft['membership_basis'] == 'participants'
    assert draft['mentions_notified'] == [{'sender': '77', 'display_name': 'Member', 'span_text': '@Вова'}]
    with pytest.raises(APIError, match='mention_not_member'):
        mention_preview(s, key, 'Hi @Боря', gone)
    assert mention_send(s, key, 'Hi @Вова', newcomer, draft['preview_digest'])['status'] == 'queued'
    for sender_id in ('-1001234', 'channel', '0'):
        with pytest.raises(APIError, match='mention_unresolvable') as refused:
            mention_preview(s, key, 'Hi @Вова', [{'sender': sender_id, 'offset': 3, 'length': 5}])
        assert refused.value.next_call['tool'] == 'list_participants'
    assert queued_count(s) == 1


# spec: ANYM-928
def test_a_preview_with_mentions_queues_one_participants_read_per_chat_and_ten_minutes(sender, monkeypatch):
    from messaging_service import group_mentions
    s, archive, _, _, _ = sender
    tg_group(archive)
    key = group_key(s)
    s.set_messenger_operations('send-fixture', ['list_participants'])
    monkeypatch.setattr(group_mentions, 'READ_WAIT_S', 0.2)
    monkeypatch.setattr(group_mentions, '_now', lambda: 1800000000.0)  # one 10-minute bucket for the whole test
    args = {'account': 'send-fixture', 'chat_id': GROUP, 'text': 'Hi @Анна',
            'mentions': [{'sender': '42', 'offset': 3, 'length': 5}]}
    with TestClient(create_app(s, ['testserver'])) as client:
        headers = {'Authorization': 'Bearer ' + key}
        first = client.post('/v1/preview_message', headers=headers, json=args).json()
        second = client.post('/v1/preview_message', headers=headers, json=args).json()
        client.post('/v1/preview_message', headers=headers, json={**args, 'mentions': None})
    # Nobody answered the read in time: the archive decided, and the second preview queued no second read.
    assert first['membership_basis'] == second['membership_basis'] == 'archive'
    with s.pool.connection() as c:
        reads = c.execute("SELECT request_id,payload FROM messenger_commands WHERE operation='list_participants'").fetchall()
    assert len(reads) == 1 and reads[0]['request_id'].startswith('mention-members:')
    assert reads[0]['payload']['parameters']['limit'] == 100


def mention_job(platform, artifact, policy):
    return {'provider_chat': '-500' if platform == 'telegram' else 'family@g.us', 'body': artifact['provider_text'],
            'source_text': artifact['source_text'], 'source_format': artifact['format'],
            'render_artifact': {k: v for k, v in artifact.items() if k in (
                'format', 'source_text', 'rendered_text', 'provider_text', 'entities', 'provider_payload', 'warnings',
                'links', 'utf16_units', 'renderer_version', 'mentions')},
            'render_policy': policy, 'preview_digest': 'f' * 64,
            'expires_at': datetime.now(timezone.utc).timestamp() + 300}


# spec: ANYM-928
def test_a_telegram_mention_reaches_telethon_as_input_message_entity_mention_name(sender):
    from types import SimpleNamespace
    from telethon import types
    from messaging_service.outgoing import SendRejected
    from messaging_service.telegram_collector import TelethonProvider
    s, archive, _, _, _ = sender
    tg_group(archive)
    key = group_key(s)
    members_read(s, key, 'send-fixture', GROUP, ['42', '77'])
    mentions = [{'sender': '77', 'offset': 10, 'length': 5}, {'sender': '42', 'offset': 0, 'length': 5}]
    draft = mention_preview(s, key, '@Анна и **@Вова**', mentions, format='markdown')
    job = mention_job('telegram', draft, draft['render_policy'])
    calls, cached = [], {42: 4242}

    class Client:
        async def get_input_entity(self, peer):
            if peer.user_id not in cached: raise ValueError('not cached')
            return types.InputPeerUser(peer.user_id, cached[peer.user_id])
        async def get_participants(self, chat, limit):
            calls.append(('participants', chat, limit)); cached[77] = 7777
        async def send_message(self, chat, text, **options):
            calls.append(('send', chat, text, options)); return SimpleNamespace(id=42, chat_id=-500)

    provider = TelethonProvider.__new__(TelethonProvider)
    provider.client = Client()
    asyncio.run(provider.send_text(job))
    assert calls[0] == ('participants', -500, 200)
    _, chat, text, options = calls[1]
    assert (chat, text) == (-500, '@Анна и @Вова')
    entities = options['formatting_entities']
    assert [type(e).__name__ for e in entities] == ['InputMessageEntityMentionName', 'MessageEntityBold',
                                                     'InputMessageEntityMentionName']
    assert [(e.offset, e.length) for e in entities] == [(0, 5), (8, 5), (8, 5)]
    assert (entities[0].user_id.user_id, entities[0].user_id.access_hash) == (42, 4242)
    assert (entities[2].user_id.user_id, entities[2].user_id.access_hash) == (77, 7777)
    # A user the participants read does not cache either is refused before the send, never sent as plain text.
    calls.clear(); cached.clear()
    with pytest.raises(SendRejected, match='mention_unresolvable'):
        asyncio.run(provider.send_text(job))
    assert [call[0] for call in calls] == ['participants']
    # A stored mention that is not the previewed one is refused before any provider call.
    for policy in ({**draft['render_policy'], 'mentions': mentions[:1]}, {'link_preview': False, 'allow_lossy': False}):
        with pytest.raises(SendRejected, match='send_target_invalid'):
            asyncio.run(provider.send_text(mention_job('telegram', draft, policy)))
    assert [call[0] for call in calls] == ['participants']


def wa_group(s, archive, provider='whatsmeow'):
    archive.register('wa-group', 'whatsapp', 'Synthetic WhatsApp', discover=True)
    archive.ingest(event(account='wa-group', platform='whatsapp', chat='family@g.us', provider_id='1',
        sender='15559876543@c.us', body='Synthetic hello', sender_profile={'display_name': 'Анна'}))
    archive.set_sending('wa-group', True)
    epoch = archive.acquire('wa-group', 'synthetic-wa-worker')
    state = CollectorState(archive, 'wa-group', ('synthetic-wa-worker', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    with s.pool.connection() as c:
        c.execute('''UPDATE collector_health SET operations=operations||%s WHERE account='wa-group' ''',
                  (Jsonb({'provider': {'name': provider, 'contract_version': 1}}),))
    return s.issue_key('wa-mention-agent', ['wa-group'], permissions=['read', 'send']), stable_id('chat', 'wa-group', 'family@g.us')


# spec: ANYM-928
def test_a_whatsapp_mention_becomes_at_number_with_mentioned_jid_and_plain_at_digits_warn(sender):
    from messaging_service.outgoing import dispatch_payload
    s, archive, _, _, _ = sender
    key, chat = wa_group(s, archive)
    common = {'account': 'wa-group', 'chat': chat}
    draft = mention_preview(s, key, 'Привет, Анна!', [{'sender': '15559876543@c.us', 'offset': 8, 'length': 4}], **common)
    assert draft['provider_text'] == 'Привет, @15559876543!' and draft['rendered_text'] == 'Привет, Анна!'
    assert draft['mentions'] == [{'offset': 8, 'length': 12, 'jid': '15559876543@c.us'}]
    assert draft['mentions_notified'] == [{'sender': '15559876543@c.us', 'display_name': 'Анна', 'span_text': 'Анна'}]
    assert 'warning' not in draft
    assert mention_preview(s, key, 'Позвони @15550001111', None, **common)['warning'] == 'plain_at_is_not_a_mention'
    with pytest.raises(APIError, match='mention_overlap'):
        mention_preview(s, key, 'mailАнна', [{'sender': '15559876543@c.us', 'offset': 4, 'length': 4}], **common)
    receipt = mention_send(s, key, 'Привет, Анна!', [{'sender': '15559876543@c.us', 'offset': 8, 'length': 4}],
                           draft['preview_digest'], **common)
    with s.pool.connection() as c:
        row = c.execute('SELECT * FROM outgoing_messages WHERE id=%s', (receipt['send_id'],)).fetchone()
    assert row['body'] == 'Привет, @15559876543!'
    payload = dispatch_payload(row, 'whatsapp')
    assert payload['text'] == 'Привет, @15559876543!' and payload['mentions'] == ['15559876543@c.us']


# spec: ANYM-928
def test_the_whatsapp_browser_fallback_refuses_mentions_unsupported(sender):
    s, archive, _, _, _ = sender
    key, chat = wa_group(s, archive, provider='webjs')
    with pytest.raises(APIError, match='mentions_unsupported'):
        mention_preview(s, key, 'Привет, Анна!', [{'sender': '15559876543@c.us', 'offset': 8, 'length': 4}],
                        account='wa-group', chat=chat)
    assert queued_count(s) == 0


# spec: ANYM-928
def test_mentions_with_a_menu_choice_conflict_and_an_allow_duplicate_resend_keeps_its_mentions(sender):
    s, archive, _, _, _ = sender
    tg_group(archive)
    key = group_key(s)
    anna = [{'sender': '42', 'offset': 3, 'length': 5}]
    menu = {'message_id': stable_id('msg', 'send-fixture', '-500', '1'), 'option_id': '1'}
    with pytest.raises(APIError, match='mentions_conflict'):
        mention_preview(s, key, 'Hi @Анна', anna, menu_choice=menu)
    with pytest.raises(APIError, match='mentions_conflict'):
        mention_send(s, key, 'Hi @Анна', anna, 'a' * 64, menu_choice=menu)
    draft = mention_preview(s, key, 'Hi @Анна', anna)
    mention_send(s, key, 'Hi @Анна', anna, draft['preview_digest'], duplicate_guard=True)
    with pytest.raises(APIError, match='duplicate_send_unresolved'):
        mention_send(s, key, 'Hi @Анна', anna, draft['preview_digest'], 'mention-request-0002', duplicate_guard=True)
    again = mention_send(s, key, 'Hi @Анна', anna, draft['preview_digest'], 'mention-request-0002', duplicate_guard=True,
                         allow_duplicate=True)
    with s.pool.connection() as c:
        row = c.execute('SELECT render_policy,render_artifact FROM outgoing_messages WHERE id=%s',
                        (again['send_id'],)).fetchone()
    assert row['render_policy']['mentions'] == anna and row['render_artifact']['mentions'] == draft['mentions']
    assert queued_count(s) == 2


# spec: ANYM-928
def test_mcp_send_message_and_preview_take_mentions_with_no_schema_default(sender):
    s, _, key, _, _ = sender
    headers = {'Authorization': 'Bearer ' + key}
    with TestClient(create_app(s, ['testserver'])) as client:
        tools = client.post('/mcp', headers={**headers, 'Accept': 'application/json, text/event-stream'},
            json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}).json()['result']['tools']
    for name in ('preview_message', 'send_message'):
        schema = next(t for t in tools if t['name'] == name)['inputSchema']
        assert 'default' not in schema['properties']['mentions'] and 'mention_not_member' in schema['properties']['mentions']['description']
        assert set(schema['$defs']['Mention']['properties']) == {'sender', 'offset', 'length'}


def test_worker_sends_once_and_rechecks_revocation(sender):
    from messaging_service.outgoing import OutgoingWorker
    s, _, key, _, state = sender
    calls = []
    async def provider(job):
        calls.append(job)
        return {'provider_message_id': '42'}
    worker = OutgoingWorker(state, provider)
    result = submit(s, key)
    assert asyncio.run(worker.once()) is True
    assert asyncio.run(worker.once()) is False
    receipt = s.get_send_status(key, result['send_id'])
    assert receipt['status'] == 'sent' and receipt['provider_message_id'] == '42'
    assert receipt['recipient_delivery_confirmed'] is False
    assert receipt['agent_send']['principal']=='send-agent'
    assert receipt['agent_send']['request_id']=='synthetic-request-0001'
    assert receipt['agent_send']['owner_approval_verified_by_server'] is False
    assert submit(s, key)['send_id'] == result['send_id']
    assert len(calls) == 1
    queued = s.send_message(key, account='send-fixture', chat_id=result['chat_id'],
        text='Second synthetic', request_id='synthetic-request-0002')
    s.set_access('send-agent', permissions=['read'])
    asyncio.run(worker.once())
    assert len(calls) == 1
    with s.pool.connection() as c:
        assert c.execute('SELECT status FROM outgoing_messages WHERE id=%s',
            (queued['send_id'],)).fetchone()['status'] == 'cancelled'


def test_agent_send_provenance_is_scoped_and_bounded_in_compact_reads(sender):
    from messaging_service.outgoing import OutgoingWorker
    from messaging_service.presentation import render
    s,archive,key,_,state=sender
    action=submit(s,key)
    async def provider(job):return {'provider_message_id':'42'}
    asyncio.run(OutgoingWorker(state,provider).once())
    mid=archive.ingest(event(account='send-fixture',provider_id='42',direction='outgoing',
        body='Exact synthetic text'))['message']
    reader=s.issue_key('scoped-reader',['send-fixture'],chats=[stable_id('chat','send-fixture','100')])
    result=s.get_context(reader,mid,before_count=0,after_count=0)
    compact=render('get_context',result,'compact')['message']
    assert compact['agent_send']=={'send_id':action['send_id'],'principal':'send-agent',
        'basis':'confirmed_outbox_receipt','body_matches_sent_text':True,'owner_approval_verified_by_server':False}
    assert 'key_id' not in str(compact['agent_send']) and 'request_id' not in compact['agent_send']
    assert next(r for r in render('search',s.search(reader),'compact')['items'] if r['id']==mid)['agent_send']['send_id']==action['send_id']
    updates=render('get_updates',s.get_updates(reader,'send-provenance'),'compact')['items']
    assert next(r for r in updates if r['message']==mid)['agent_send']['send_id']==action['send_id']
    archive.ingest(event(account='send-fixture',provider_id='42',direction='outgoing',
        body='edited synthetic text',updated_at='2026-10-01T12:01:00Z'))
    assert not s.get_context(reader,mid)['message']['agent_send']['body_matches_sent_text']
    s.set_access('scoped-reader',chats=[])
    with pytest.raises(APIError,match='not_found'):s.get_context(reader,mid)


def test_duplicate_provider_receipts_do_not_attribute_an_agent_by_guessing(sender):
    from messaging_service.outgoing import OutgoingWorker
    s,archive,key,_,state=sender
    submit(s,key)
    s.send_message(key,account='send-fixture',chat_id=stable_id('chat','send-fixture','100'),
        text='Second fixture',request_id='second-synthetic-request')
    async def provider(job):return {'provider_message_id':'42'}
    worker=OutgoingWorker(state,provider)
    asyncio.run(worker.once());asyncio.run(worker.once())
    mid=archive.ingest(event(account='send-fixture',provider_id='42',direction='outgoing',body='Exact synthetic text'))['message']
    assert s.get_context(key,mid)['message']['agent_send']=={'basis':'ambiguous_outbox_receipts',
        'candidate_count':2,'owner_approval_verified_by_server':False}


def test_ambiguous_provider_failure_never_retries(sender):
    from messaging_service.outgoing import OutgoingWorker
    s, _, key, _, state = sender
    calls = []
    async def provider(job):
        calls.append(job)
        raise TimeoutError('synthetic secret diagnostic must not be returned')
    worker = OutgoingWorker(state, provider)
    queued = submit(s, key)
    asyncio.run(worker.once())
    assert s.get_send_status(key, queued['send_id'])['status'] == 'unknown'
    asyncio.run(worker.once())
    assert len(calls) == 1
    assert submit(s, key)['status'] == 'unknown'


def test_crash_after_dispatch_is_unknown_not_replayed(sender):
    from messaging_service.outgoing import OutgoingWorker
    s, _, key, _, state = sender
    queued = submit(s, key)
    with s.pool.connection() as c:
        c.execute("UPDATE outgoing_messages SET status='dispatching',lease_epoch=0 WHERE id=%s",
                  (queued['send_id'],))
    calls = []
    async def provider(job): calls.append(job)
    asyncio.run(OutgoingWorker(state, provider).once())
    assert not calls
    assert s.get_send_status(key, queued['send_id'])['status'] == 'unknown'


def test_busy_provider_gate_leaves_send_queued(sender):
    from messaging_service.outgoing import OutgoingWorker
    s, _, key, _, state = sender
    queued = submit(s, key)
    calls = []
    async def scenario():
        gate = asyncio.Lock()
        async def provider(job):
            calls.append(job)
            return {'provider_message_id':'43'}
        await gate.acquire()
        task = asyncio.create_task(OutgoingWorker(state, provider, gate).once())
        await asyncio.sleep(.05)
        assert not calls and s.get_send_status(key,queued['send_id'])['status']=='queued'
        gate.release()
        assert await task is True
    asyncio.run(scenario())
    assert len(calls)==1 and s.get_send_status(key,queued['send_id'])['status']=='sent'


@pytest.mark.parametrize('change,expected', [('expiry','expired'),('revoke','cancelled'),('disable','cancelled')])
def test_pending_send_rechecks_expiry_key_and_account(sender,change,expected):
    from messaging_service.outgoing import OutgoingWorker
    s, archive, key, _, state = sender
    queued = submit(s,key)
    if change=='expiry':
        with s.pool.connection() as c:
            c.execute("UPDATE outgoing_messages SET expires_at=now()-interval '1 second'")
        assert s.get_send_status(key,queued['send_id'])['status']=='expired'
    elif change=='revoke':s.revoke(key)
    else:archive.set_sending('send-fixture',False)
    calls = []
    async def provider(job):calls.append(job)
    assert asyncio.run(OutgoingWorker(state,provider).once()) is True
    assert not calls
    with s.pool.connection() as c:
        assert c.execute('SELECT status FROM outgoing_messages WHERE id=%s',
            (queued['send_id'],)).fetchone()['status']==expected


def test_send_works_with_restricted_gateway_and_collector_roles(sender):
    from contextlib import contextmanager
    from copy import copy
    from pathlib import Path
    import psycopg
    import messaging_service.store as module
    from messaging_service.outgoing import OutgoingWorker
    s, _, key, _, state = sender
    roles={'gateway':'send_test_gateway','collector':'send_test_collector'}
    class RestrictedPool:
        def __init__(self,role):self.role=role
        @contextmanager
        def connection(self):
            with s.pool.connection() as c:
                c.execute('SET LOCAL ROLE '+self.role)
                yield c
    for kind,role in roles.items():
        ddl=Path(module.__file__).with_name(kind+'_role.sql').read_text().replace('messaging_'+kind,role)
        with s.pool.connection() as c:c.execute(ddl)
    gateway=copy(s);gateway.pool=RestrictedPool(roles['gateway'])
    collector=copy(s);collector.pool=RestrictedPool(roles['collector'])
    restricted_state=CollectorState(Archive(collector),state.account,state.lease)
    calls=[]
    async def provider(job):
        calls.append(job)
        return {'provider_message_id':'44'}
    try:
        queued=submit(gateway,key)
        assert asyncio.run(OutgoingWorker(restricted_state,provider).once())
        assert gateway.get_send_status(key,queued['send_id'])['status']=='sent'
        assert len(calls)==1
        with s.pool.connection() as c:
            c.execute("UPDATE outgoing_messages SET status='dispatching',lease_epoch=0 WHERE id=%s",(queued['send_id'],))
        assert gateway.get_send_status(key,queued['send_id'])['status']=='unknown'
        for pool,sql in [(gateway.pool,"UPDATE outgoing_messages SET status='sent'"),
                         (collector.pool,'SELECT digest FROM api_keys'),
                         (collector.pool,'UPDATE account_policies SET send_enabled=true')]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with pool.connection() as c:c.execute(sql)
    finally:
        for role in roles.values():
            with s.pool.connection() as c:
                c.execute('DROP OWNED BY '+role);c.execute('DROP ROLE '+role)


def test_mcp_and_rest_send_contract(sender):
    s, _, key, _, _ = sender
    with TestClient(create_app(s, ['testserver'])) as client:
        headers = {'Authorization': 'Bearer ' + key}
        args = {'account':'send-fixture','chat_id':stable_id('chat','send-fixture','100'),
                'text':'Synthetic HTTP message','request_id':'synthetic-http-request'}
        response = client.post('/v1/send_message', headers=headers, json=args)
        assert response.status_code == 200
        receipt = response.json()
        assert receipt['status'] == 'queued'
        assert client.post('/v1/get_send_status', headers=headers,
            json={'send_id':receipt['send_id']}).json()['send_id'] == receipt['send_id']
        assert client.post('/v1/send_message', headers=headers,
            json={**args,'approved':True}).status_code == 400
        tools = client.post('/mcp', headers={**headers,'Accept':'application/json, text/event-stream'},
            json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}}).json()['result']['tools']
        tool = next(t for t in tools if t['name'] == 'send_message')
        assert tool['annotations']['readOnlyHint'] is False
        assert tool['annotations']['openWorldHint'] is True
        assert 'confirm' in tool['description'].lower()


# spec: ANYM-927
def test_mcp_send_message_and_preview_take_silent_with_no_schema_default(sender):
    s, _, key, _, _ = sender
    headers = {'Authorization': 'Bearer ' + key}
    with TestClient(create_app(s, ['testserver'])) as client:
        tools = client.post('/mcp', headers={**headers,'Accept':'application/json, text/event-stream'},
            json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}}).json()['result']['tools']
        args = {'account':'send-fixture','chat_id':stable_id('chat','send-fixture','100'),'text':'Synthetic silent'}
        preview = client.post('/v1/preview_message', headers=headers, json={**args,'silent':True}).json()
        sent = client.post('/v1/send_message', headers=headers, json={**args,'silent':True,
            'request_id':'synthetic-silent-http','preview_digest':preview['preview_digest']})
    for name in ('preview_message', 'send_message'):
        schema = next(t for t in tools if t['name'] == name)['inputSchema']['properties']['silent']
        assert 'default' not in schema and 'notification sound' in schema['description']
    assert preview['silent'] is True and sent.status_code == 200 and sent.json()['status'] == 'queued'


# spec: ANYM-927
def test_a_silent_telegram_send_reaches_telethon_as_silent():
    from types import SimpleNamespace
    from messaging_service.telegram_collector import TelethonProvider
    calls = []
    class Client:
        async def send_message(self, chat, text, **options):
            calls.append(options)
            return SimpleNamespace(id=42, chat_id=100)
    provider = TelethonProvider.__new__(TelethonProvider)
    provider.client = Client()
    text = 'Synthetic quiet'
    def job(policy):
        artifact = {'format':'plain','source_text':text,'rendered_text':text,'provider_text':text,'entities':[],
            'provider_payload':{'text':text,'entities':[],'link_preview':False},'warnings':[],'links':[],
            'utf16_units':len(text),'renderer_version':'anymsg-renderer-1'}
        return {'provider_chat':'100','body':text,'source_text':text,'source_format':'plain','render_artifact':artifact,
            'render_policy':policy,'preview_digest':'f'*64,'expires_at':datetime.now(timezone.utc).timestamp()+300}
    asyncio.run(provider.send_text(job({'link_preview':False,'allow_lossy':False,'silent':True})))
    asyncio.run(provider.send_text(job({'link_preview':False,'allow_lossy':False})))
    assert calls[0]['silent'] is True and 'silent' not in calls[1]
    assert {k: v for k, v in calls[0].items() if k != 'silent'} == calls[1]
    # A stored policy may carry silent only as true; anything else is refused before the provider.
    from messaging_service.outgoing import SendRejected, dispatch_payload
    for policy in ({'link_preview':False,'allow_lossy':False,'silent':False},
                   {'link_preview':False,'allow_lossy':False,'silent':'yes'}):
        with pytest.raises(SendRejected):
            asyncio.run(provider.send_text(job(policy)))
    assert len(calls) == 2
    with pytest.raises(SendRejected):
        dispatch_payload(job({'link_preview':False,'allow_lossy':False,'silent':True}), 'whatsapp')


def test_telegram_send_plain_text_through_existing_client():
    from types import SimpleNamespace
    from messaging_service.telegram_collector import TelethonProvider
    calls = []
    class Client:
        async def send_message(self, chat, text, **options):
            calls.append((chat, text, options))
            return SimpleNamespace(id=42, chat_id=100)
    provider = TelethonProvider.__new__(TelethonProvider)
    provider.client = Client()
    result = asyncio.run(provider.send_text({'provider_chat':'100','body':'Synthetic **literal**',
        'reply_provider_id':'1','expires_at':datetime.now(timezone.utc).timestamp()+300}))
    assert result['provider_message_id'] == '42'
    assert calls == [(100, 'Synthetic **literal**', {
        'parse_mode':None,'link_preview':False,'reply_to':1,'clear_draft':False})]
