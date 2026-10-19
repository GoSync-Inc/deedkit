"""open_chat_by_phone: check, confirm, create, then the unchanged send_message (synthetic only)."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from messaging_service.archive import Archive
from messaging_service.commands import CommandRejected, CommandWorker
from messaging_service.envelope import stable_id
from messaging_service.error_details import payload as error_payload
from messaging_service.messenger_operations import operation_manifest, phone_digits, validate_operation
from messaging_service.store import APIError
from messaging_service.whatsapp_collector import NodeProvider, WhatsAppState
from test_whatsapp import capture, event

ACCOUNT = 'wa-phone'
PHONE = '12025550123'  # NANP fictional 555-01xx range
PN = PHONE+'@c.us'
LID = '987654321012345@lid'


@pytest.fixture
def native(seeded):
    s, *_ = seeded
    archive = Archive(s)
    archive.register(ACCOUNT, 'whatsapp', 'Synthetic native', discover=True)
    epoch = archive.acquire(ACCOUNT, 'phone-test')
    state = WhatsAppState(archive, ACCOUNT, ('phone-test', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    state.provider_observation('whatsmeow')
    s.set_messenger_operations(ACCOUNT, ['open_chat_by_phone'])
    key = s.issue_key('phone-agent', [ACCOUNT], permissions=['read', 'send'])
    yield s, archive, state, key
    archive.release(ACCOUNT, 'phone-test', state.lease[1])


def args(phone=PHONE, create=False):
    return {'account': ACCOUNT, 'operation': 'open_chat_by_phone', 'parameters': {'phone': phone, 'create': create}}


def answer(lid=LID, *, on=True, name=None, lookup=True):
    data = {'on_whatsapp': on, 'verified_name': name, 'provider_lookup': lookup}
    if on: data['provider_chat'] = lid
    return {'outcome': 'confirmed', 'data': data}


def run(s, key, request_id, result, **changes):
    """Queue one command and dispatch it to a fake adapter that answers like operations.go."""
    queued = s.execute_messenger_operation(key, **args(**changes), request_id=request_id)
    jobs = []
    async def execute(job):
        jobs.append(job)
        if isinstance(result, Exception): raise result
        return result(job) if callable(result) else result
    assert asyncio.run(CommandWorker(run.state, execute).once())
    return s.get_messenger_operation(key, queued['operation_id']), jobs


def chats(s):
    with s.pool.connection() as c:
        return {r['provider_chat']: r for r in c.execute('SELECT * FROM chats WHERE account=%s', (ACCOUNT,)).fetchall()}


def archive_chat(state, chat, provider_id):
    item = event(1); item['chatId'] = chat; item['senderId'] = chat; item['messageId'] = 'false_'+chat+'_'+provider_id
    state.apply(capture(item, account=ACCOUNT))


@pytest.fixture(autouse=True)
def bind_state(request):
    if 'native' in request.fixturenames:
        run.state = request.getfixturevalue('native')[2]


# spec: ANYM-430
@pytest.mark.parametrize('text,digits', [
    ('+1 (202) 555-0123', PHONE), ('001 202 555 0123', PHONE), ('+1.202.555.01.23', PHONE),
    ('+44 7700\u00a0900123', '447700900123'), ('+44 (7700) 900\u2011123', '447700900123'), (PHONE, PHONE),
    ('447700900123', '447700900123')])
def test_google_maps_style_numbers_normalize_to_digits(text, digits):
    assert phone_digits(text) == (digits, None)
    assert validate_operation('whatsapp', 'open_chat_by_phone', {'phone': text}, provider='whatsmeow') == \
        {'phone': digits, 'create': False}


# spec: ANYM-430
@pytest.mark.parametrize('text,hint', [
    ('07700 900123', 'country_code_required'), ('8 (900) 555-01-23', 'country_code_required'),
    ('+07700 900123', None), ('+1 202', None), ('+1 202 555 0123 4567 89', None), ('tel:+12025550123', None),
    ('+1 202 555 0123 ext 2', None), ('+１２０２５５５０１２３', None), (PN, None), ('', None)])
def test_other_inputs_are_invalid_phone_without_echoing_the_value(text, hint):
    with pytest.raises(ValueError, match='invalid_phone') as caught:
        validate_operation('whatsapp', 'open_chat_by_phone', {'phone': text}, provider='whatsmeow')
    assert getattr(caught.value, 'hint', None) == hint
    assert caught.value.field_errors[0]['field'] == 'phone' and (not text or text not in json.dumps(caught.value.field_errors))


# spec: ANYM-430
def test_check_writes_nothing_and_create_returns_the_canonical_lid_chat(native):
    s, archive, state, key = native
    before = chats(s)
    receipt, jobs = run(s, key, 'phone-check-0001', answer(name='Synthetic Bakery'))
    assert receipt['status'] == 'succeeded' and chats(s) == before
    assert receipt['result']['data'] == {'on_whatsapp': True, 'verified_name': 'Synthetic Bakery', 'provider_lookup': True}
    assert jobs[0]['parameters'] == {'phone': PHONE, 'create': False} and 'provider_chat' not in receipt['result']['data']
    receipt, _ = run(s, key, 'phone-create-0001', answer(name='Synthetic Bakery'), create=True)
    chat_id = stable_id('chat', ACCOUNT, LID)
    assert receipt['result']['data'] == {'on_whatsapp': True, 'verified_name': 'Synthetic Bakery',
                                         'provider_lookup': True, 'chat_id': chat_id, 'existing': False}
    created = chats(s)[LID]
    assert created['id'] == chat_id and created['title'] == 'Synthetic Bakery' and created['chat_type'] == 'private'
    # The pinned sender routes to the LID; its echo is archived in that same chat.
    archive_chat(state, LID, 'ECHO1')
    assert set(chats(s)) == set(before) | {LID}
    with s.pool.connection() as c:
        assert c.execute('SELECT chat FROM messages WHERE provider_id LIKE %s', ('%ECHO1',)).fetchone()['chat'] == chat_id
    draft = s.preview_message(key, account=ACCOUNT, chat_id=chat_id, text='Synthetic first message')
    assert draft['owner_approval_verified_by_server'] is False


# spec: ANYM-430
@pytest.mark.parametrize('archived', ['pn_only', 'lid_only', 'both', 'neither'])
def test_every_archive_state_ends_in_one_canonical_chat(native, archived):
    s, archive, state, key = native
    if archived in ('pn_only', 'both'): archive_chat(state, PN, 'PN1')
    if archived in ('lid_only', 'both'): archive_chat(state, LID, 'LID1')
    before = chats(s)
    receipt, _ = run(s, key, 'phone-create-0002', answer(), create=True)
    data = receipt['result']['data']
    assert data['chat_id'] == stable_id('chat', ACCOUNT, LID) and data['existing'] is (LID in before)
    assert set(chats(s)) == set(before) | {LID}
    assert chats(s)[LID]['title'] == (before[LID]['title'] if LID in before else '+'+PHONE)
    again, _ = run(s, key, 'phone-create-0003', answer(), create=True)
    assert again['result']['data']['chat_id'] == data['chat_id'] and again['result']['data']['existing'] is True
    assert set(chats(s)) == set(before) | {LID}


# spec: ANYM-430
def test_unregistered_or_unresolved_numbers_get_no_chat(native):
    s, archive, state, key = native
    before = chats(s)
    receipt, _ = run(s, key, 'phone-negative-0001', answer(on=False), create=True)
    assert receipt['status'] == 'succeeded' and receipt['result']['data'] == \
        {'on_whatsapp': False, 'verified_name': None, 'provider_lookup': True}
    receipt, _ = run(s, key, 'phone-unresolved-0001', CommandRejected('identity_unresolved'), create=True)
    assert receipt['status'] == 'failed' and receipt['error_code'] == 'identity_unresolved' and receipt['result'] is None
    # A malformed adapter answer is never materialized.
    receipt, _ = run(s, key, 'phone-malformed-0001', answer(lid=PN), create=True)
    assert receipt['status'] == 'failed' and chats(s) == before


# spec: ANYM-430
def test_known_mapping_is_passed_for_a_local_answer_that_does_not_count(native):
    s, archive, state, key = native
    archive_chat(state, LID, 'LID1')
    with s.pool.connection() as c:
        source = c.execute('SELECT id FROM messages WHERE provider_id LIKE %s', ('%LID1',)).fetchone()['id']
        c.execute('''INSERT INTO whatsapp_identity_observations(account,source_message,identity_id,pn,lid,provenance,
            first_observed_at,observed_at) VALUES(%s,%s,%s,%s,%s,'whatsmeow_lid_store_v1',now(),now())''',
            (ACCOUNT, source, LID, PN, LID))
    for n in range(12):
        receipt, jobs = run(s, key, 'phone-local-%04d' % n, answer(lookup=False))
        assert jobs[0]['known_chats'] == [LID] and receipt['result']['data']['existing'] is True
    # A created chat without identity evidence is also offered for the local answer.
    with s.pool.connection() as c: c.execute('DELETE FROM whatsapp_identity_observations')
    other = '111222333444555@lid'
    run(s, key, 'phone-create-0004', answer(lid=other), create=True)
    _, jobs = run(s, key, 'phone-local-0100', answer(lid=other, lookup=False))
    assert jobs[0]['known_chats'] == [other, LID]  # LID: the earlier receipts name its chat


# spec: ANYM-430
def test_chat_scoped_key_and_missing_grants_are_refused_before_any_query(native):
    s, archive, state, key = native
    archive_chat(state, PN, 'PN1')
    s.set_access('phone-agent', chats=[stable_id('chat', ACCOUNT, PN)])
    with pytest.raises(APIError, match='forbidden'):
        s.preview_messenger_operation(key, **args())
    with pytest.raises(APIError, match='forbidden'):
        s.execute_messenger_operation(key, **args(), request_id='phone-scoped-0001')
    s.set_access('phone-agent', chats=None)
    reader = s.issue_key('phone-reader', [ACCOUNT], permissions=['read'])
    with pytest.raises(APIError, match='forbidden'):
        s.execute_messenger_operation(reader, **args(), request_id='phone-reader-0001')
    s.set_messenger_operations(ACCOUNT, [])
    with pytest.raises(APIError, match='operation_disabled'):
        s.execute_messenger_operation(key, **args(), request_id='phone-disabled-0001')
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n'] == 0


# spec: ANYM-430
def test_created_chat_is_visible_to_no_chat_scoped_key(native):
    s, archive, state, key = native
    archive_chat(state, PN, 'PN1')
    scoped = s.issue_key('phone-scoped', [ACCOUNT], permissions=['read', 'send'])
    s.set_access('phone-scoped', chats=[stable_id('chat', ACCOUNT, PN)])
    receipt, _ = run(s, key, 'phone-create-0005', answer(), create=True)
    with pytest.raises(APIError, match='not_found'):
        s.preview_message(scoped, account=ACCOUNT, chat_id=receipt['result']['data']['chat_id'], text='Synthetic')
    with pytest.raises(APIError, match='not_found'):
        s.resolve_reference(scoped, ACCOUNT, LID)


# spec: ANYM-430
@pytest.mark.parametrize('platform', ['webjs', 'telegram'])
def test_other_adapters_report_unsupported_and_name_the_supported_kind(seeded, platform):
    s, *_ = seeded
    archive = Archive(s)
    account = 'phone-'+platform
    archive.register(account, 'telegram' if platform == 'telegram' else 'whatsapp', 'Synthetic', discover=True)
    key = s.issue_key('phone-other', [account], permissions=['read', 'send'])
    with pytest.raises(APIError, match='unsupported_operation') as caught:
        s.preview_messenger_operation(key, account=account, operation='open_chat_by_phone', parameters={'phone': PHONE})
    assert error_payload(caught.value)['hint'] == 'native_whatsapp_accounts_only'
    rows = {r['operation']: r for r in operation_manifest('telegram' if platform == 'telegram' else 'whatsapp')}
    assert not rows['open_chat_by_phone']['supported'] and 'whatsmeow' in rows['open_chat_by_phone']['limitation']


# spec: ANYM-430
def test_collection_policy_is_conservative_before_and_after_the_query(native):
    s, archive, state, key = native
    with s.pool.connection() as c:
        c.execute('UPDATE account_policies SET discover_chats=false,include_chats=%s WHERE account=%s', ([PN, LID], ACCOUNT))
    with pytest.raises(APIError, match='chat_not_collected'):
        s.preview_messenger_operation(key, **args())
    with s.pool.connection() as c:
        c.execute('UPDATE account_policies SET discover_chats=true,include_chats=%s,exclude_chats=%s WHERE account=%s',
                  ([], [PN], ACCOUNT))
    with pytest.raises(APIError, match='chat_not_collected'):
        s.execute_messenger_operation(key, **args(), request_id='phone-excluded-0001')
    # The LID learned only from the query is excluded: refused, no row, no chat_id.
    with s.pool.connection() as c:
        c.execute('UPDATE account_policies SET exclude_chats=%s WHERE account=%s', ([LID], ACCOUNT))
    before = chats(s)
    receipt, _ = run(s, key, 'phone-excluded-0002', answer(), create=True)
    assert receipt['status'] == 'failed' and receipt['error_code'] == 'chat_not_collected'
    assert receipt['result'] is None and chats(s) == before


# spec: ANYM-430
@pytest.mark.parametrize('revoke', ['operation', 'exclusion', 'key'])
def test_policy_or_grant_revoked_during_the_lookup_creates_nothing(native, revoke):
    s, archive, state, key = native
    before = chats(s)
    def during(job):
        if revoke == 'operation': s.set_messenger_operations(ACCOUNT, [])
        elif revoke == 'exclusion':
            with s.pool.connection() as c:
                c.execute('UPDATE account_policies SET exclude_chats=%s WHERE account=%s', ([LID], ACCOUNT))
        else: s.set_key_permissions(s.capabilities(key)['credential_id'], ['read'])
        return answer()
    queued = s.execute_messenger_operation(key, **args(create=True), request_id='phone-revoke-0001')
    async def execute(job): return during(job)
    assert asyncio.run(CommandWorker(state, execute).once())
    with s.pool.connection() as c:
        row = c.execute('SELECT status,error_code,result FROM messenger_commands WHERE id=%s', (queued['operation_id'],)).fetchone()
    assert row['status'] == 'failed' and row['result'] is None and chats(s) == before
    assert row['error_code'] == {'operation': 'operation_disabled', 'exclusion': 'chat_not_collected', 'key': 'forbidden'}[revoke]


# spec: ANYM-430
def test_crash_before_commit_leaves_no_row_and_no_receipt(native, monkeypatch):
    import messaging_service.commands as commands
    s, archive, state, key = native
    before = chats(s)
    def crash(*values): raise RuntimeError('synthetic crash before insert')
    monkeypatch.setattr(commands, 'stable_id', crash)
    queued = s.execute_messenger_operation(key, **args(create=True), request_id='phone-crash-0001')
    async def execute(job): return answer()
    with pytest.raises(RuntimeError, match='synthetic crash'):
        asyncio.run(CommandWorker(state, execute).once())
    assert chats(s) == before
    assert s.get_messenger_operation(key, queued['operation_id'])['status'] == 'dispatching'
    # The next lease turns the interrupted read into unknown; it is never replayed.
    monkeypatch.undo()
    archive.release(ACCOUNT, 'phone-test', state.lease[1])
    state.lease = ('phone-test', archive.acquire(ACCOUNT, 'phone-test'))
    calls = []
    async def again(job): calls.append(job); return answer()
    asyncio.run(CommandWorker(state, again).once())
    receipt = s.get_messenger_operation(key, queued['operation_id'])
    assert receipt['status'] == 'unknown' and receipt['result'] is None and not calls and chats(s) == before


# spec: ANYM-430
def test_after_commit_the_same_request_returns_the_saved_receipt(native):
    s, archive, state, key = native
    first, _ = run(s, key, 'phone-commit-0001', answer(), create=True)
    # The reply was lost after the commit: the same request ID returns the saved receipt, no new lookup.
    replay = s.execute_messenger_operation(key, **args(create=True), request_id='phone-commit-0001')
    assert replay['operation_id'] == first['operation_id'] and replay['result'] == first['result']
    calls = []
    async def execute(job): calls.append(job); return answer()
    assert not asyncio.run(CommandWorker(state, execute).once()) and not calls
    # A new request repeats the read-only lookup and reuses the row.
    second, jobs = run(s, key, 'phone-commit-0002', answer(), create=True)
    assert len(jobs) == 1 and second['result']['data']['chat_id'] == first['result']['data']['chat_id']
    assert second['result']['data']['existing'] is True and list(chats(s)).count(LID) == 1


def set_created(s, ages):
    with s.pool.connection() as c:
        ids = [r['id'] for r in c.execute('SELECT id FROM messenger_commands ORDER BY created_at,id').fetchall()]
        for i, age in zip(ids, ages, strict=True):
            c.execute("UPDATE messenger_commands SET created_at=clock_timestamp()-%s*interval '1 second' WHERE id=%s", (age, i))


# spec: ANYM-430
def test_ten_lookups_per_rolling_hour_then_rate_limited_before_any_query(native):
    s, archive, state, key = native
    other = s.issue_key('phone-other-agent', [ACCOUNT], permissions=['read', 'send'])
    for n in range(10):
        run(s, key if n % 2 else other, 'phone-bound-%04d' % n, answer(on=False) if n % 3 else CommandRejected('provider_timeout'))
    # Idempotent replays do not count, and they still answer.
    assert s.execute_messenger_operation(other, **args(), request_id='phone-bound-0000')['status'] == 'failed'
    set_created(s, [3599-n for n in range(10)])
    for agent in (key, other):
        with pytest.raises(APIError, match='rate_limited') as caught:
            s.execute_messenger_operation(agent, **args(), request_id='phone-bound-0010')
        detail = error_payload(caught.value)
        assert detail['error'] == 'rate_limited' and 1 <= detail['resets_in_seconds'] <= 2
        assert PHONE not in json.dumps(detail)
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n'] == 10
    set_created(s, [3601]+[3500-n for n in range(9)])
    receipt, jobs = run(s, key, 'phone-bound-0011', answer(on=False))
    assert receipt['status'] == 'succeeded' and len(jobs) == 1
    with pytest.raises(APIError, match='rate_limited'):
        s.execute_messenger_operation(key, **args(), request_id='phone-bound-0012')


# spec: ANYM-430
def test_provider_timeout_is_a_failed_retryable_read(native):
    s, archive, state, key = native
    receipt, _ = run(s, key, 'phone-timeout-0001', CommandRejected('provider_timeout'))
    assert receipt['status'] == 'failed' and receipt['error_code'] == 'provider_timeout'
    assert receipt['result'] == {'data': {'code': 'provider_timeout', 'retry': True}}
    assert receipt['next_action'] == 'none'


class Writer:
    def __init__(self): self.lines = []
    def write(self, data): self.lines.append(json.loads(data))
    async def drain(self): pass
    def close(self): pass
    async def wait_closed(self): pass


# spec: ANYM-430
def test_stalled_lookup_keeps_the_adapter_connected_for_the_next_send(tmp_path):
    async def check():
        provider = NodeProvider({}, tmp_path/'unused', 3)
        provider.process = SimpleNamespace(returncode=None, stdout=asyncio.StreamReader())
        provider.send_reader, provider.send_writer = asyncio.StreamReader(), Writer()
        provider.send_reader.feed_data(json.dumps({'id': 1, 'error': 'provider_timeout'}).encode()+b'\n')
        provider.send_reader.feed_data(json.dumps({'id': 2, 'result': {'provider_message_id': 'true_1@c.us_X'}}).encode()+b'\n')
        disconnected = []
        async def disconnect(): disconnected.append(True)
        provider.disconnect = disconnect
        with pytest.raises(CommandRejected, match='provider_timeout'):
            await provider._call_unlocked('messenger_operation', {'operation': 'open_chat_by_phone'}, sending=True)
        sent = await provider._call_unlocked('send_text', {'chat': '1@c.us', 'text': 'Synthetic'}, sending=True)
        assert sent == {'provider_message_id': 'true_1@c.us_X'} and not disconnected
    asyncio.run(check())


# spec: ANYM-430
@pytest.mark.parametrize('reference,hinted', [('+'+PHONE, True), (PHONE+'@s.whatsapp.net', True), (PN, True),
    (PHONE, True), ('chat_'+'0'*32, False), ('123@g.us', False)])
def test_phone_shaped_not_found_names_the_operation(native, reference, hinted):
    s, archive, state, key = native
    for call in (lambda: s.preview_message(key, account=ACCOUNT, chat_id=reference, text='Synthetic'),
                 lambda: s.resolve_reference(key, ACCOUNT, reference)):
        with pytest.raises(APIError, match='not_found') as caught:
            call()
        assert error_payload(caught.value).get('hint') == ('open_chat_by_phone' if hinted else None)


# spec: ANYM-430
def test_no_phone_number_in_audit_or_errors(native):
    s, archive, state, key = native
    run(s, key, 'phone-audit-0001', answer(), create=True)
    run(s, key, 'phone-audit-0002', CommandRejected('identity_unresolved'))
    with pytest.raises(APIError) as caught:
        s.preview_messenger_operation(key, **args(phone='07700 900123'))
    assert '7700900123' not in json.dumps(error_payload(caught.value))
    with s.pool.connection() as c:
        rows = c.execute('SELECT to_jsonb(a) AS row FROM audit_log a').fetchall()
    assert rows and not any(PHONE in json.dumps(r['row']) for r in rows)
