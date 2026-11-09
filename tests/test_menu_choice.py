"""ANYM-837: an agent answers an archived WhatsApp bot menu with its structured choice (synthetic only: isolated
PostgreSQL, fake adapters; no provider, session or send)."""
import asyncio
import importlib.util
import json
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from messaging_service import menu_choice as menus
from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.envelope import stable_id
from messaging_service.error_details import payload as error_payload
from messaging_service.outgoing import SEND_REFUSALS, OutgoingWorker, SendRejected, dispatch_payload
from messaging_service.store import APIError
from messaging_service.whatsapp_collector import NodeProvider, WhatsAppState
from test_whatsapp import capture

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = 'wa-menu'
BOT = '15550001111@c.us'
MENU_BODY = ('Choose a department\nMenu (Departments):\nAdults:\n1. Cardiology - Adults [id: card_1]\n'
    '2. Dentistry [id: dent_2]\n3. Website [link: https://clinic.example/]\n4. Twin [id: twin]\n5. Twin too [id: twin]')
CARDIOLOGY = 'Cardiology - Adults'


def menu_event(mid, body=MENU_BODY, kind='list', from_me=False, sender=BOT, stamp=0, reply=None):
    value = {'chatId': BOT, 'messageId': f'false_{BOT}_{mid}', 'senderId': sender, 'body': body,
        'timestamp': 1790000000 + stamp, 'fromMe': from_me, 'hasMedia': False, 'messageType': kind}
    if reply: value['replyToId'] = reply
    return value


@pytest.fixture
def native(seeded):
    s, *_ = seeded
    archive = Archive(s)
    archive.register(ACCOUNT, 'whatsapp', 'Synthetic native', discover=True)
    epoch = archive.acquire(ACCOUNT, 'menu-test')
    state = WhatsAppState(archive, ACCOUNT, ('menu-test', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    state.provider_observation('whatsmeow')
    archive.set_sending(ACCOUNT, True)
    state.apply(capture(menu_event('MENU1'), account=ACCOUNT))
    key = s.issue_key('menu-agent', [ACCOUNT], permissions=['read', 'send'])
    yield s, archive, state, key
    archive.release(ACCOUNT, 'menu-test', state.lease[1])


CHAT = stable_id('chat', ACCOUNT, BOT)


def message(mid):
    return stable_id('msg', ACCOUNT, BOT, f'false_{BOT}_{mid}')


def choice(option='card_1', mid='MENU1'):
    return {'message_id': message(mid), 'option_id': option}


def preview(s, key, text='', **changes):
    return s.preview_message(key, ACCOUNT, CHAT, text, menu_choice=changes.pop('menu_choice', choice()), **changes)


def send(s, key, request_id='menu-request-0001', text='', digest=None, **changes):
    menu = changes.pop('menu_choice', choice())
    if digest is None: digest = s.preview_message(key, ACCOUNT, CHAT, text, menu_choice=menu)['preview_digest']
    return s.send_message(key, ACCOUNT, CHAT, text, request_id, preview_digest=digest, menu_choice=menu,
        duplicate_guard=True, **changes)


def refused(call):
    with pytest.raises(APIError) as caught:call()
    return caught.value


def row(s, send_id):
    with s.pool.connection() as c:
        return c.execute('SELECT * FROM outgoing_messages WHERE id=%s', (send_id,)).fetchone()


# spec: ANYM-837
def test_a_preview_shows_the_menu_option_and_risk_and_binds_menu_revision_option_and_label(native):
    s, _, state, key = native
    shown = preview(s, key)
    block = shown['menu_choice']
    assert shown['source_text'] == CARDIOLOGY and shown['reply_to'] == message('MENU1')
    assert block['chosen_option'] == {'number': 1, 'label': CARDIOLOGY, 'option_id': 'card_1'}
    assert block['other_options'] == ['Dentistry', 'Website', 'Twin', 'Twin too']
    assert block['menu_text'] == {'text': MENU_BODY, 'format': 'plain', 'trust': 'untrusted_bot_content'}
    assert block['risk'] == menus.RISK and block['menu_sender'] == BOT and block['menu_type'] == 'list'
    assert shown['owner_approval_verified_by_server'] is False
    assert 'newer_menu_message_id' not in block and 'already_answered' not in block
    # Empty text, the label and the title alone are one choice; another option or label is another digest.
    digest = shown['preview_digest']
    assert preview(s, key, CARDIOLOGY)['preview_digest'] == preview(s, key, 'Cardiology')['preview_digest'] == digest
    assert preview(s, key, menu_choice=choice('dent_2'))['preview_digest'] != digest
    # An edited menu (new revision, other label) changes the digest; a newer menu and an answer are hinted.
    state.apply(capture({**menu_event('MENU1', MENU_BODY.replace('Adults [id', 'Grown-ups [id')),
        'editedTimestampMs': 1790000100000}, account=ACCOUNT, change='edit'))
    edited = preview(s, key)
    assert edited['source_text'] == 'Cardiology - Grown-ups' and edited['preview_digest'] != digest
    state.apply(capture(menu_event('MENU2', stamp=5), account=ACCOUNT))
    state.apply(capture(menu_event('ANSWER', 'Chose menu option: Dentistry [id: dent_2]', 'list_response', True,
        stamp=6, reply=f'false_{BOT}_MENU1'), account=ACCOUNT))
    hinted = preview(s, key)['menu_choice']
    assert hinted['newer_menu_message_id'] == message('MENU2')
    assert hinted['already_answered']['label'] == 'Dentistry'


# spec: ANYM-837
def test_each_preview_refusal_names_its_code_before_the_owner_is_asked(native):
    s, archive, state, key = native
    state.apply(capture(menu_event('TEXT', 'plain words', 'chat', stamp=1), account=ACCOUNT))
    state.apply(capture(menu_event('OLD', 'A menu archived before option capture', stamp=2), account=ACCOUNT))
    state.apply(capture(menu_event('FLOW', 'Pick\nMenu:\n1. Hours [id: hours]', 'interactive', stamp=3), account=ACCOUNT))
    state.apply(capture(menu_event('MINE', MENU_BODY, from_me=True, stamp=4), account=ACCOUNT))
    cases = {'menu_not_found': choice(mid='MISSING'), 'menu_not_a_menu': choice(mid='TEXT'),
        'menu_options_not_captured': choice(mid='OLD'), 'menu_option_not_found': choice('website'),
        'menu_option_ambiguous': choice('twin'), 'menu_reply_shape_unverified': choice('hours', 'FLOW')}
    for code, menu in cases.items():
        failure = refused(lambda: preview(s, key, menu_choice=menu))
        assert failure.code == code, code
        assert failure.code != 'menu_reply_shape_unverified' or 'text reply is the fallback' in failure.next_action
    assert refused(lambda: preview(s, key, menu_choice=choice(mid='MINE'))).code == 'menu_not_a_menu'
    mismatch = refused(lambda: preview(s, key, 'Dentistry'))
    assert mismatch.code == 'menu_option_mismatch'
    assert error_payload(mismatch)['archived_label'] == CARDIOLOGY
    option = refused(lambda: preview(s, key, menu_choice=choice('website')))
    assert 'only options shown with [id: ...]' in error_payload(option)['next_action']
    for changes in ({'format': 'markdown'}, {'link_preview': True}, {'allow_lossy': True}, {'reply_to': message('TEXT')},
                    {'menu_choice': {'message_id': message('MENU1')}}, {'menu_choice': {**choice(), 'x': 'y'}}):
        assert refused(lambda: preview(s, key, **changes)).code == 'invalid_parameters', changes
    # whatsapp-web.js does not answer menus.
    state.provider_observation('webjs')
    assert refused(lambda: preview(s, key)).code == 'menu_choice_unsupported'
    # Telegram: press_button is its bot-button tool.
    archive.register('tg-menu', 'telegram', 'Synthetic telegram', discover=True)
    from test_archive import event
    archive.ingest(event(account='tg-menu', chat='100', provider_id='1', body=MENU_BODY))
    tg = s.issue_key('menu-tg', ['tg-menu'], permissions=['read', 'send'])
    failure = refused(lambda: s.preview_message(tg, 'tg-menu', stable_id('chat', 'tg-menu', '100'), '',
        menu_choice={'message_id': stable_id('msg', 'tg-menu', '100', '1'), 'option_id': 'card_1'}))
    assert failure.code == 'menu_choice_unsupported' and 'press_button' in failure.next_action


# spec: ANYM-837
def test_a_send_needs_its_preview_stores_the_label_and_replays_or_conflicts_by_menu_and_option(native):
    s, _, _, key = native
    assert refused(lambda: send(s, key, digest='')).code == 'preview_required'
    assert refused(lambda: send(s, key, digest='0'*64)).code == 'preview_mismatch'
    first = send(s, key, text='Cardiology')
    saved = row(s, first['send_id'])
    assert first['status'] == 'queued' and first['menu_choice'] == {'message_id': message('MENU1'),
        'option_id': 'card_1', 'label': CARDIOLOGY}
    assert saved['body'] == saved['source_text'] == CARDIOLOGY and saved['reply_provider_id'] == f'false_{BOT}_MENU1'
    assert saved['menu_choice'] == {'message_id': message('MENU1'), 'provider_message_id': f'false_{BOT}_MENU1',
        'menu_type': 'list'}
    assert saved['render_artifact']['menu_choice'] == {'option_id': 'card_1', 'label': CARDIOLOGY, 'menu_type': 'list'}
    # An exact retry returns the saved send; another option under the same request_id is a conflict.
    assert send(s, key, text='Cardiology', digest=saved['preview_digest'])['send_id'] == first['send_id']
    assert send(s, key, text=CARDIOLOGY, digest=saved['preview_digest'])['send_id'] == first['send_id']
    assert refused(lambda: send(s, key, menu_choice=choice('dent_2'))).code == 'idempotency_conflict'
    assert refused(lambda: s.send_message(key, ACCOUNT, CHAT, 'plain', 'menu-request-0001')).code == 'idempotency_conflict'
    # Phone, username and messages_send recipients are refused.
    with TestClient(create_app(s, ['testserver'])) as client:
        headers = {'Authorization': 'Bearer ' + key}
        for recipient in ({'phone': '+12025550123'}, {'username': '@synthetic_user'}):
            answer = client.post('/v1/send_message', headers=headers, json={'account': ACCOUNT, 'text': '',
                'request_id': 'menu-request-http1', 'menu_choice': choice(), **recipient})
            assert answer.status_code == 400 and answer.json()['error'] == 'invalid_parameters', recipient
        answer = client.post('/v1/messages_send', headers=headers, json={'to': CHAT, 'text': 'x', 'menu_choice': choice()})
        assert answer.status_code == 400


# spec: ANYM-837
def test_a_send_only_key_cannot_resolve_a_new_menu_choice_or_read_its_labels(native):
    s, _, _, _ = native
    send_only = s.issue_key('menu-send-only', [ACCOUNT], permissions=['send'])
    for text, digest in (('Dentistry', ''), ('', ''), ('', '0' * 64)):
        failure = refused(lambda: s.send_message(send_only, ACCOUNT, CHAT, text, 'menu-request-0101',
            preview_digest=digest, menu_choice=choice(), duplicate_guard=True))
        assert (failure.code, failure.status) == ('forbidden', 403)
        assert 'archived_label' not in error_payload(failure) and CARDIOLOGY not in json.dumps(error_payload(failure))
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM outgoing_messages').fetchone()['n'] == 0


# spec: ANYM-837
def test_the_duplicate_guard_keys_on_menu_and_option(native):
    s, _, _, key = native
    send(s, key)
    again = refused(lambda: send(s, key, 'menu-request-0002'))
    assert again.code == 'duplicate_send_unresolved' and again.earlier_request_id == 'menu-request-0001'
    assert send(s, key, 'menu-request-0003', menu_choice=choice('dent_2'))['status'] == 'queued'
    assert send(s, key, 'menu-request-0004', allow_duplicate=True)['status'] == 'queued'
    with s.pool.connection() as c:
        c.execute("UPDATE outgoing_messages SET status='sent',provider_message_id='p' WHERE request_id LIKE 'menu-request-000%%'")
    assert send(s, key, 'menu-request-0005')['status'] == 'queued'


# spec: ANYM-837
@pytest.mark.parametrize('status', ['queued', 'dispatching', 'unknown'])
def test_a_renamed_option_is_still_the_unresolved_duplicate(native, status):
    s, _, state, key = native
    send(s, key)
    with s.pool.connection() as c:
        c.execute('UPDATE outgoing_messages SET status=%s WHERE request_id=%s', (status, 'menu-request-0001'))
    # The bot renames the option behind the same menu and ID; a fresh preview binds the new label.
    state.apply(capture({**menu_event('MENU1', MENU_BODY.replace('Cardiology - Adults', 'Cardiology - Everyone')),
        'editedTimestampMs': 1790000100000}, account=ACCOUNT, change='edit'))
    again = refused(lambda: send(s, key, 'menu-request-0002'))
    assert again.code == 'duplicate_send_unresolved' and again.earlier_request_id == 'menu-request-0001'
    assert again.earlier_status == status


def worker(state, reply):
    calls = []
    async def provider(job):
        calls.append(dispatch_payload(job, 'whatsapp'))
        if isinstance(reply, Exception): raise reply
        return reply
    return OutgoingWorker(state, provider), calls


# spec: ANYM-837
def test_the_worker_passes_the_choice_and_says_sent_is_not_the_bots_answer(native):
    s, _, state, key = native
    queued = send(s, key)
    runner, calls = worker(state, {'provider_message_id': 'true_answer'})
    assert asyncio.run(runner.once()) is True
    assert calls == [{'text': CARDIOLOGY, 'entities': [], 'link_preview': False,
        'menu_choice': {'option_id': 'card_1', 'label': CARDIOLOGY, 'menu_type': 'list'}}]
    status = s.get_send_status(key, queued['send_id'])
    assert status['status'] == 'sent' and status['menu_choice']['label'] == CARDIOLOGY
    assert status['next_action'].startswith('the bot answers within seconds')


# spec: ANYM-837
@pytest.mark.parametrize('code', [c for c in SEND_REFUSALS if c.startswith('menu_')])
def test_each_menu_refusal_ends_the_send_failed(native, code):
    s, _, state, key = native
    queued = send(s, key)
    runner, _ = worker(state, SendRejected(code))
    asyncio.run(runner.once())
    saved = row(s, queued['send_id'])
    assert (saved['status'], saved['error_code']) == ('failed', code)
    # Each refusal names its recovery; a text fallback always says it needs the owner's confirmation.
    status = s.get_send_status(key, queued['send_id'])
    assert status['next_action'] == menus.FAILED_NEXT[code] != 'none'
    assert "needs the owner's confirmation" in status['next_action']


# spec: ANYM-837
def test_a_deleted_menu_fails_in_preflight_with_no_adapter_call(native):
    s, _, state, key = native
    queued = send(s, key)
    state.apply(capture(menu_event('MENU1'), account=ACCOUNT, change='delete'))
    runner, calls = worker(state, {'provider_message_id': 'never'})
    asyncio.run(runner.once())
    assert calls == []
    saved = row(s, queued['send_id'])
    assert (saved['status'], saved['error_code']) == ('failed', 'menu_unavailable')


def collector(provider, sent):
    adapter = NodeProvider({'provider': provider}, '/nonexistent/config.json', -1)
    async def call(method, params, sending=False):
        sent.append((method, params))
        return {'provider_message_id': 'true_x'}
    adapter._call_unlocked = call
    return adapter


async def through(adapter, job):
    async with adapter.send_lock:return await adapter.send_text(job)


# spec: ANYM-837
def test_only_the_native_adapter_gets_the_choice_and_a_provider_switch_or_telegram_refuses_it(native):
    s, _, _, key = native
    job = dict(row(s, send(s, key)['send_id']));job['expires_at'] = job['expires_at'].timestamp()
    sent = []
    asyncio.run(through(collector('whatsmeow', sent), dict(job)))
    assert sent[0][0] == 'send_text' and sent[0][1]['menu_choice'] == {'option_id': 'card_1', 'label': CARDIOLOGY,
        'menu_type': 'list'}
    assert sent[0][1]['text'] == CARDIOLOGY and sent[0][1]['reply_provider_id'] == f'false_{BOT}_MENU1'
    # The native adapter decodes these params strictly (send_test.go reads the same file).
    fixture = json.loads((ROOT / 'tests/fixtures/menu-send-text.json').read_text())
    assert {**sent[0][1], 'send_id': 'synthetic', 'expires_at': 1} == fixture
    sent.clear()
    with pytest.raises(SendRejected, match='menu_choice_unsupported'):
        asyncio.run(through(collector('webjs', sent), dict(job)))
    with pytest.raises(SendRejected, match='menu_choice_unsupported'):
        dispatch_payload(dict(job), 'telegram')
    assert sent == []
    # A plain send is unchanged: no menu_choice key reaches the adapter.
    plain = dict(row(s, s.send_message(key, ACCOUNT, CHAT, 'Plain words', 'plain-request-0001')['send_id']))
    plain['expires_at'] = plain['expires_at'].timestamp()
    asyncio.run(through(collector('webjs', sent), plain))
    assert 'menu_choice' not in sent[0][1]


def base_code():
    spec = importlib.util.spec_from_file_location('base_menu_dispatch', ROOT / 'tests/fixtures/base_menu_dispatch.py')
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    from messaging_service import outgoing, whatsapp_collector
    space = {'__name__': 'messaging_service.base_copy', '__package__': 'messaging_service', 're': re,
        'APIError': APIError, 'SendRejected': SendRejected, 'SEND_ID_RE': whatsapp_collector.SEND_ID_RE}
    exec(module.DISPATCH_PAYLOAD, space)
    exec('class Base:\n' + module.SEND_TEXT, space)
    assert outgoing.dispatch_payload.__code__ != space['dispatch_payload'].__code__
    return space


# spec: ANYM-837
def test_the_base_commits_dispatch_and_send_text_refuse_a_menu_row_with_no_adapter_call(native):
    s, _, _, key = native
    job = dict(row(s, send(s, key)['send_id']));job['expires_at'] = job['expires_at'].timestamp()
    space = base_code()
    with pytest.raises(SendRejected, match='send_target_invalid'):
        space['dispatch_payload'](dict(job), 'whatsapp')
    sent = []
    adapter = collector('webjs', sent)
    import messaging_service.outgoing as outgoing
    original = outgoing.dispatch_payload
    outgoing.dispatch_payload = space['dispatch_payload']
    try:
        async def run():
            async with adapter.send_lock:return await space['Base'].send_text(adapter, dict(job))
        with pytest.raises(SendRejected, match='send_target_invalid'):asyncio.run(run())
    finally:outgoing.dispatch_payload = original
    assert sent == []


# spec: ANYM-837
def test_a_database_without_the_column_queues_nothing_and_plain_sends_and_purges_still_work(native):
    s, _, _, key = native
    digest = preview(s, key)['preview_digest']
    migration = (ROOT / 'services/messaging/messaging_service/migrations/070_menu_choice.sql').read_text()
    with s.pool.connection() as c:c.execute('ALTER TABLE outgoing_messages DROP COLUMN menu_choice')
    try:
        failure = refused(lambda: send(s, key, digest=digest))
        assert (failure.code, failure.status) == ('menu_choice_unavailable', 503)
        with s.pool.connection() as c:
            assert c.execute('SELECT count(*) AS n FROM outgoing_messages').fetchone()['n'] == 0
        assert s.send_message(key, ACCOUNT, CHAT, 'Plain words', 'plain-request-0002')['status'] == 'queued'
    finally:
        with s.pool.connection() as c:c.execute(migration)


# spec: ANYM-837
@pytest.mark.parametrize('order', ['purged_before_finish', 'purged_after_finish'])
def test_a_deleted_menu_answer_loses_its_label_and_option_whichever_comes_first(native, order):
    from messaging_service.message_purge import Purger
    s, _, state, key = native
    with s.pool.connection() as c:
        c.execute("""UPDATE purge_state SET value=CASE name WHEN 'event_cursor' THEN '0' WHEN 'receipt_cursor' THEN ''
            WHEN 'receipt_backfill' THEN 'pending' ELSE 'true' END""")
    queued = send(s, key)
    runner = OutgoingWorker(state, None)
    job = runner.claim()
    assert job['id'] == queued['send_id']
    answer = menu_event('ANSWER', 'Chose menu option: Cardiology - Adults [id: card_1]', 'list_response', True,
        stamp=7, reply=f'false_{BOT}_MENU1')
    state.apply(capture(answer, account=ACCOUNT))
    state.apply(capture(answer, account=ACCOUNT, change='delete'))
    if order == 'purged_after_finish': runner.finish(job, 'sent', answer['messageId'])
    assert Purger(s).run_once()['purged'] == 1
    if order == 'purged_before_finish': runner.finish(job, 'sent', answer['messageId'])
    saved = row(s, queued['send_id'])
    assert saved['status'] == 'sent' and saved['body'] == saved['source_text'] == '[deleted]'
    assert saved['render_artifact'] == {} and 'Cardiology' not in json.dumps(dict(saved), default=str)
    assert 'menu_choice' not in s.get_send_status(key, queued['send_id'])


# spec: ANYM-837
@pytest.mark.parametrize('value', [
    {'message_id': 'm', 'provider_message_id': 'p', 'menu_type': None},
    {'message_id': 'm', 'provider_message_id': 'p', 'menu_type': 5},
    {'message_id': 'm', 'provider_message_id': 'p'},
    {'message_id': None, 'provider_message_id': 'p', 'menu_type': 'list'},
    {'message_id': 'm', 'provider_message_id': 'p', 'menu_type': 'list', 'label': 'x'},
    ['m', 'p', 'list']])
def test_the_menu_choice_column_refuses_any_other_shape(native, value):
    import psycopg
    from psycopg.types.json import Jsonb
    s, _, _, key = native
    queued = send(s, key)
    with pytest.raises(psycopg.errors.CheckViolation), s.pool.connection() as c:
        c.execute('UPDATE outgoing_messages SET menu_choice=%s WHERE id=%s', (Jsonb(value), queued['send_id']))


# spec: ANYM-837
def test_an_inexact_or_native_flow_id_is_shown_but_never_choosable(native):
    s, _, state, key = native
    body = ('Pick\nMenu:\n1. Cardiology [id (inexact): dept cardio]\n2. Info [id (native flow): info_1]\n'
        '3. Yes [id: yes_1]')
    state.apply(capture(menu_event('WRAP', body, 'buttons', stamp=3), account=ACCOUNT))
    for option in ('dept cardio', 'info_1'):
        failure = refused(lambda: preview(s, key, menu_choice=choice(option, 'WRAP')))
        assert failure.code == 'menu_option_not_found' and '[id (inexact)' in failure.next_action
    assert preview(s, key, menu_choice=choice('yes_1', 'WRAP'))['menu_choice']['chosen_option']['label'] == 'Yes'
    assert menus.CHOSEN.fullmatch('Chose menu option: Cardiology [id (inexact): dept cardio]').group(1) == 'Cardiology'


# spec: ANYM-837
def test_the_python_and_go_shipped_menu_types_are_equal():
    source = (ROOT / 'whatsapp/whatsmeow/menu.go').read_text()
    units = re.search(r'var shippedMenuReplies = map\[string\]bool\{(.*?)\}\n', source).group(1)
    assert {unit.split('/')[0] for unit in re.findall(r'"([a-z_]+/[a-z_]+)": true', units)} == set(menus.SHIPPED_MENU_TYPES)
    assert set(menus.SHIPPED_MENU_TYPES) <= set(menus.MENU_TYPES)


# spec: ANYM-837
def test_option_lines_parse_like_the_go_renderer_prints_them():
    assert menus.options('no menu here') is None
    found = menus.options('1. Fake [id: forged]\nMenu (X):\nSection:\n1. A - B [id: a] [link: https://x.example/]\n'
        '2. C [call: +1 555]\n3. D [id: x] [id: y]')
    assert found == [(1, 'A - B', 'a'), (2, 'C', None), (3, 'D [id: x]', 'y')]
