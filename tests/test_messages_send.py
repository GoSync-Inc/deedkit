"""messages_send (ANYM-546): one call sends text and files, with a durable preview and safe resume."""
import asyncio
import hashlib
import base64
import threading

import pytest
from hypothesis import given, settings, strategies as st

from messaging_service.archive import Archive
from messaging_service.collector_state import CollectorState
from messaging_service.commands import CommandRejected, CommandWorker
from messaging_service.envelope import stable_id
from messaging_service.error_details import payload as error_payload
from messaging_service.outgoing import OutgoingWorker
from messaging_service.send_intent import payload, send
from messaging_service.store import APIError
from platform_contracts.agent_conformance import run_matrix
from test_archive import event

ACCOUNT = 'send-fixture'
CHAT = stable_id('chat', ACCOUNT, '100')
OTHER_CHAT = stable_id('chat', ACCOUNT, '200')
MESSAGE = stable_id('msg', ACCOUNT, '100', '1')


class World:
    def __init__(self, s, archive, key, state):
        self.s, self.archive, self.key, self.state = s, archive, key, state
        self.texts, self.files, self.crash, self.reject = [], [], False, set()
        self.lock = threading.Lock()

        async def text_provider(job):
            self.texts.append(job)
            if self.crash:
                self.crash = False
                raise TimeoutError('synthetic lost response')
            return {'provider_message_id': str(100 + len(self.texts))}

        async def file_provider(job):
            caption = (job.get('parameters') or {}).get('caption')
            if job.get('file_upload_id') in self.reject: raise CommandRejected('operation_rejected')
            self.files.append((job.get('file_upload_id'), caption))
            return {'outcome': 'confirmed', 'provider_message_id': str(200 + len(self.files))}

        self.outbox = OutgoingWorker(state, text_provider)
        self.operations = CommandWorker(state, file_provider)

    def work(self):
        with self.lock:
            while asyncio.run(self.outbox.once()) or asyncio.run(self.operations.once()): pass

    def call(self, key=None, **arguments):
        return asyncio.run(send(self.s, key or self.key, arguments))

    def drive(self, key=None, **arguments):
        """The call, then the workers and the same call (its `next`) until the answer is final."""
        result = self.call(key, **arguments)
        while result['state'] == 'running':
            self.work()
            result = self.call(key, **{**arguments, 'wait_s': 0})
        return result

    def upload(self, content, request_id, chat_id=CHAT, key=None):
        digest = hashlib.sha256(content).hexdigest()
        row = self.s.prepare_file_upload(key or self.key, ACCOUNT, chat_id, request_id + '.txt', 'text/plain',
                                         len(content), digest, request_id)
        self.s.upload_file_chunk(key or self.key, row['upload_id'], 0, base64.b64encode(content).decode())
        return row['upload_id']


@pytest.fixture
def world(seeded):
    s, _, _ = seeded
    archive = Archive(s)
    archive.register(ACCOUNT, 'telegram', 'Synthetic sender', discover=True)
    archive.ingest(event(account=ACCOUNT, chat='100', provider_id='1', body='Existing synthetic message'))
    archive.ingest(event(account=ACCOUNT, chat='200', provider_id='2', body='Second synthetic chat'))
    key = s.issue_key('send-agent', [ACCOUNT], permissions=['read', 'send', 'upload'])
    epoch = archive.acquire(ACCOUNT, 'synthetic-send-worker')
    state = CollectorState(archive, ACCOUNT, ('synthetic-send-worker', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    archive.set_sending(ACCOUNT, True)
    s.set_messenger_operations(ACCOUNT, ['send_file'])
    yield World(s, archive, key, state)
    archive.release(ACCOUNT, 'synthetic-send-worker', epoch)


def refused(fn, *args, **kwargs):
    with pytest.raises(APIError) as error: fn(*args, **kwargs)
    return error.value


def intents(w):
    with w.s.pool.connection() as c:
        return c.execute('SELECT count(*) AS n FROM send_intents').fetchone()['n']


# spec: ANYM-546
def test_plain_text_succeeds_within_wait_s_with_provider_reference(world):
    async def both():
        task = asyncio.create_task(send(world.s, world.key, {'to': CHAT, 'text': 'Synthetic hello',
                                                             'request_id': 'synthetic-send-0001', 'wait_s': 10}))
        while not task.done():
            await asyncio.to_thread(world.work)
            await asyncio.sleep(0.05)
        return await task
    result = asyncio.run(both())
    assert result['state'] == 'succeeded' and result['request_id'] == 'synthetic-send-0001'
    assert result['steps'][0]['receipt']['provider_message_id'] == '101'
    assert len(world.texts) == 1 and world.texts[0]['body'] == 'Synthetic hello'
    # The same key replays the saved result with no second effect.
    assert world.drive(to=CHAT, text='Synthetic hello', request_id='synthetic-send-0001') == result
    assert len(world.texts) == 1


# spec: ANYM-546
def test_reply_to_alone_sends_into_its_chat_and_other_chat_is_field_error(world):
    result = world.drive(reply_to=MESSAGE, text='Synthetic reply', request_id='synthetic-reply-0001')
    assert result['state'] == 'succeeded' and world.texts[0]['provider_chat'] == '100'
    assert world.texts[0]['reply_provider_id'] == '1'
    error = refused(world.call, to=OTHER_CHAT, reply_to=MESSAGE, text='Synthetic', request_id='synthetic-reply-0002')
    assert error.code == 'invalid_arguments' and error.field_errors[0]['field'] == 'reply_to'
    assert len(world.texts) == 1


# spec: ANYM-546
def test_dry_run_returns_the_full_owner_visible_preview(world):
    upload = world.upload(b'Synthetic file one', 'synthetic-upload-0001')
    text = 'Synthetic caption ' * 70  # over the 1024-character caption limit: file, then text
    result = world.call(to=CHAT, text=text, files=[{'upload_id': upload}], dry_run=True)
    assert result['state'] == 'previewed' and result['confirm_token']
    preview = result['preview']
    assert preview['recipient'] == {'account': ACCOUNT, 'network': 'telegram', 'label': preview['recipient']['label'],
                                    'chat_id': CHAT}
    assert preview['text']['visible'] == text and preview['text']['format'] == 'plain'
    assert {'warnings', 'links'} <= set(preview['text'])
    assert preview['files'] == [{'name': 'synthetic-upload-0001.txt', 'bytes': 18, 'mode': 'document', 'caption': '',
                                 'sha256': hashlib.sha256(b'Synthetic file one').hexdigest(), 'mime': 'text/plain'}]
    assert preview['caption'] == 'separate_text' and preview['steps'] == ['file1', 'text']
    assert preview['owner_approval_verified_by_server'] is False
    short = world.call(to=CHAT, text='Short caption', files=[{'upload_id': upload}], dry_run=True)['preview']
    assert short['caption'] == 'with_first_file' and short['files'][0]['caption'] == 'Short caption'
    linked = world.call(reply_to=MESSAGE, text='See [x](https://example.com/x)', format='markdown', dry_run=True)['preview']
    assert linked['reply_to'] == MESSAGE and linked['text']['links']
    with world.s.pool.connection() as c:
        stored = c.execute('SELECT token_hash FROM send_previews').fetchall()
    assert result['confirm_token'] not in str(stored)
    assert not world.texts and not world.files


# spec: ANYM-546
def test_a_changed_target_file_hash_or_scope_is_preview_stale_and_sends_nothing(world):
    token = world.call(to=CHAT, text='Synthetic', dry_run=True)['confirm_token']
    with world.s.pool.connection() as c:
        c.execute("UPDATE chats SET provider_chat='999' WHERE id=%s", (CHAT,))
    stale = refused(world.call, confirm_token=token)
    assert stale.code == 'preview_stale' and stale.changed == ['target'] and stale.confirm_token != token
    shown = error_payload(stale)
    assert shown['changed'] == ['target'] and shown['confirm_token'] == stale.confirm_token and 'preview' in shown
    with world.s.pool.connection() as c:
        c.execute("UPDATE chats SET provider_chat='100' WHERE id=%s", (CHAT,))
    token = world.call(to=CHAT, text='Synthetic', dry_run=True)['confirm_token']
    world.s.set_access('send-agent', chats=[CHAT, OTHER_CHAT])
    assert refused(world.call, confirm_token=token).changed == ['scope']
    token = world.call(to=CHAT, text='Synthetic', dry_run=True)['confirm_token']
    with world.s.pool.connection() as c:
        c.execute("UPDATE send_previews SET expires_at=expires_at-interval '11 minutes'")
    expired = refused(world.call, confirm_token=token)
    assert expired.code == 'preview_expired' and expired.next['arguments']['dry_run'] is True
    world.work()
    assert not world.texts and intents(world) == 0


# spec: ANYM-546
def test_a_changed_file_hash_is_preview_stale(world):
    upload = world.upload(b'Synthetic file', 'synthetic-upload-0001')
    token = world.call(to=CHAT, files=[{'upload_id': upload}], dry_run=True)['confirm_token']
    # An upload's bytes are immutable: the previewed hash stands in for a file replaced since the preview.
    with world.s.pool.connection() as c:
        c.execute("UPDATE send_previews SET resolution=jsonb_set(resolution,'{files,0,sha256}',%s) "
                  "WHERE intent IS NULL", ('"' + 'b' * 64 + '"',))
    error = refused(world.call, confirm_token=token)
    assert error.code == 'preview_stale' and error.changed == ['files']
    world.work()
    assert not world.files


# spec: ANYM-546
def test_live_send_needs_a_key_and_rich_format_needs_a_preview(world):
    missing = refused(world.call, to=CHAT, text='Synthetic')
    assert missing.code == 'invalid_arguments' and missing.field_errors[0]['field'] == 'request_id'
    assert missing.next['arguments']['request_id'] == 'REPLACE_WITH_YOUR_ID'
    rich = refused(world.call, to=CHAT, text='**Synthetic**', format='markdown', request_id='synthetic-rich-0001')
    assert rich.code == 'preview_required' and rich.next == {
        'tool': 'messages_send', 'arguments': {'to': CHAT, 'account': '', 'reply_to': '', 'text': '**Synthetic**',
                                               'files': [], 'format': 'markdown', 'dry_run': True}}
    token = world.call(**rich.next['arguments'])['confirm_token']
    assert world.drive(confirm_token=token)['state'] == 'succeeded'
    assert len(world.texts) == 1 and intents(world) == 1


# spec: ANYM-546
def test_conformance_matrix_runs_against_the_preview_record(world):
    keys = {'p1': world.key, 'p2': world.s.issue_key('send-agent-two', [ACCOUNT], permissions=['read', 'send'])}

    class Harness:
        effects = property(lambda self: len(world.texts))

        def advance(self, seconds):
            with world.s.pool.connection() as c:
                c.execute("UPDATE send_previews SET expires_at=expires_at-%s*interval '1 second'", (seconds,))

        def drift(self):
            with world.s.pool.connection() as c:
                c.execute("UPDATE chats SET provider_chat=provider_chat||'0' WHERE id=%s", (CHAT,))
            return ['target']

        def revoke(self, principal, tool):
            world.s.set_access('send-agent' if principal == 'p1' else 'send-agent-two', permissions=['read'])

        def crash_after_binding(self):
            world.crash = True

        def call(self, principal, tool, arguments):
            import messaging_service.send_intent as intent
            values = {('to' if k == 'target' else k): (CHAT if k == 'target' else v) for k, v in arguments.items()}
            original = intent.TOOL
            if tool != 't_send': intent.TOOL = 'messages_other'
            try:
                return world.drive(keys[principal], **values)
            except APIError as error:
                answer = error_payload(error)
                if answer['error'] in ('not_found', 'forbidden'): answer['error'] = 'not_found_or_forbidden'
                return {k: v for k, v in answer.items() if k not in ('expected',)}
            finally:
                intent.TOOL = original

    def make():
        with world.s.pool.connection() as c:
            c.execute('DELETE FROM send_previews')
            c.execute("UPDATE chats SET provider_chat='100' WHERE id=%s", (CHAT,))
        world.s.set_access('send-agent', permissions=['read', 'send', 'upload'])
        world.texts.clear()
        return Harness()

    assert len(run_matrix(make)) == 12


# spec: ANYM-546
def test_multi_file_partial_failure_resumes_only_the_rest(world):
    a, b, c = (world.upload(f'Synthetic file {n}'.encode(), f'synthetic-upload-000{n}') for n in (1, 2, 3))
    world.reject.add(c)
    files = [{'upload_id': a}, {'upload_id': b}, {'upload_id': c}]
    token = world.call(to=CHAT, text='Synthetic caption', files=files, dry_run=True)['confirm_token']
    failed = world.drive(confirm_token=token, request_id='synthetic-multi-0001')
    assert failed['state'] == 'failed' and failed['resumable'] is True and failed['reason'] == 'step_failed'
    assert [s['state'] for s in failed['steps']] == ['succeeded', 'succeeded', 'failed']
    assert failed['next'] == {'tool': 'messages_send', 'arguments': {
        'request_id': 'synthetic-multi-0001', 'confirm_token': token, 'resume': True}}
    assert [f[0] for f in world.files] == [a, b] and world.files[0][1] == 'Synthetic caption'
    # A plain repeat by request_id or by the consumed token returns the saved result with no effect.
    assert world.drive(confirm_token=token) == {k: v for k, v in failed.items() if k != 'next'} | {
        'next': {'tool': 'messages_send', 'arguments': {'confirm_token': token, 'resume': True}}}
    assert world.drive(request_id='synthetic-multi-0001', to=CHAT, text='Synthetic caption', files=files)['state'] == 'failed'
    world.work()
    assert len(world.files) == 2
    world.reject.clear()
    # Two concurrent resumes: one advances, the other is in_progress or reads the same intent.
    answers = []
    def resume():
        try: answers.append(world.call(confirm_token=token, resume=True))
        except APIError as error: answers.append({'state': error.code})
    threads = [threading.Thread(target=resume) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(30)
    assert all(a['state'] in ('running', 'in_progress', 'succeeded') for a in answers)
    done = world.drive(confirm_token=token, resume=True)
    assert done['state'] == 'succeeded' and [s['state'] for s in done['steps']] == ['succeeded'] * 3
    assert [f[0] for f in world.files] == [a, b, c]
    # The finished intent keeps no text.
    with world.s.pool.connection() as conn:
        row = conn.execute('SELECT plan FROM send_intents').fetchone()
    assert 'Synthetic caption' not in str(row['plan'])


# spec: ANYM-546
def test_lost_text_response_is_unknown_and_never_resumed(world):
    world.crash = True
    first = world.drive(to=CHAT, text='Synthetic', request_id='synthetic-crash-0001')
    assert first['state'] == 'unknown' and first['steps'][0]['state'] == 'unknown' and first.get('next') is None
    assert world.drive(to=CHAT, text='Synthetic', request_id='synthetic-crash-0001', resume=True) == first
    assert len(world.texts) == 1


# spec: ANYM-546
def test_crash_between_files_continues_from_the_record(world):
    a, b = (world.upload(f'Synthetic file {n}'.encode(), f'synthetic-upload-000{n}') for n in (1, 2))
    files = [{'upload_id': a}, {'upload_id': b}]
    running = world.call(to=CHAT, files=files, request_id='synthetic-between-0001')
    assert running['state'] == 'running' and running['next']['arguments'] == {
        'request_id': 'synthetic-between-0001', 'wait_s': 10}
    world.work()  # file1 sent; the caller died before reading it
    done = world.drive(to=CHAT, files=files, request_id='synthetic-between-0001')
    assert done['state'] == 'succeeded' and [f[0] for f in world.files] == [a, b]


# spec: ANYM-546
def test_offline_account_is_failed_account_offline_and_resumable(world):
    world.state.phase('connection', 'retry_wait', 'provider_temporarily_unavailable')
    result = world.drive(to=CHAT, text='Synthetic', request_id='synthetic-offline-0001')
    assert result['state'] == 'failed' and result['reason'] == 'account_offline' and result['resumable'] is True
    assert result['next']['arguments'] == {'request_id': 'synthetic-offline-0001', 'resume': True}
    world.state.phase('connection', 'connected', 'provider_ready')
    done = world.drive(to=CHAT, text='Synthetic', request_id='synthetic-offline-0001', resume=True)
    assert done['state'] == 'succeeded' and len(world.texts) == 1


# spec: ANYM-546
def test_authority_refusals_equal_the_low_level_route(world):
    reader = world.s.issue_key('send-reader', [ACCOUNT], permissions=['read'])
    low = refused(world.s.send_message, reader, account=ACCOUNT, chat_id=CHAT, text='x', request_id='synthetic-low-0001')
    assert refused(world.call, reader, to=CHAT, text='x', request_id='synthetic-auth-0001').code == low.code
    other = world.s.issue_key('send-other', [ACCOUNT], permissions=['read', 'send', 'upload'])
    foreign = world.upload(b'Foreign file', 'synthetic-foreign-01', key=other)
    assert refused(world.call, to=CHAT, files=[{'upload_id': foreign}], request_id='synthetic-auth-0002').code == 'not_found'
    elsewhere = world.upload(b'Elsewhere file', 'synthetic-elsewhere1', chat_id=OTHER_CHAT)
    assert refused(world.call, to=CHAT, files=[{'upload_id': elsewhere}], request_id='synthetic-auth-0003').code == 'not_found'
    upload = world.upload(b'Opt-out file', 'synthetic-optout-01')
    world.s.set_messenger_operations(ACCOUNT, [])
    assert refused(world.call, to=CHAT, files=[{'upload_id': upload}], dry_run=True).code == 'operation_disabled'
    world.s.set_messenger_operations(ACCOUNT, ['send_file'])
    # A scope narrowed between preview and execute: the same not_found as the low-level route.
    token = world.call(to=CHAT, files=[{'upload_id': upload}], dry_run=True)['confirm_token']
    world.s.set_access('send-agent', chats=[OTHER_CHAT])
    assert refused(world.call, confirm_token=token).code == refused(
        world.s.send_message, world.key, account=ACCOUNT, chat_id=CHAT, text='x', request_id='synthetic-low-0002').code
    world.work()
    assert not world.files and not world.texts and intents(world) == 0


# spec: ANYM-546
def test_caller_routes_refuse_the_child_key_prefix(world):
    error = refused(world.s.send_message, world.key, account=ACCOUNT, chat_id=CHAT, text='x',
                    request_id='agent-child:' + 'a' * 64)
    assert error.code == 'invalid_request_id' or error.code == 'invalid_arguments'


files_strategy = st.lists(st.fixed_dictionaries({'upload_id': st.text('abc_', min_size=1, max_size=8)},
                                                optional={'mode': st.sampled_from(['document', 'photo'])}),
                          max_size=3, unique_by=lambda item: tuple(sorted(item.items())))


# spec: ANYM-546
@settings(max_examples=200, deadline=None)
@given(to=st.sampled_from(['', CHAT]), reply=st.sampled_from(['', MESSAGE]), text=st.text(max_size=40),
       files=files_strategy, fmt=st.sampled_from(['plain', 'markdown', 'html']), extra=st.booleans())
def test_payload_is_canonical_and_idempotent(to, reply, text, files, fmt, extra):
    arguments = {'to': to, 'reply_to': reply, 'text': text, 'files': files, 'format': fmt}
    if extra: arguments.update(request_id='synthetic-pbt-0001', wait_s=5)
    try:
        value = payload(arguments)
    except APIError as error:
        assert error.code == 'invalid_arguments' and error.field_errors[0]['reason']
        return
    assert payload(value) == value
    assert payload(dict(reversed(list(arguments.items())))) == value
    assert value['files'] == [dict(sorted(item.items())) for item in files]


@pytest.fixture
def phone_world(seeded):
    from test_phone_send import ACCOUNT as WA, connect
    s, *_ = seeded
    archive = Archive(s)
    state = connect(s, archive, WA)
    key = s.issue_key('phone-send-agent', [WA], permissions=['read', 'send'])
    yield s, state, key
    archive.release(WA, 'send-test', state.lease[1])


# spec: ANYM-546
def test_phone_target_opens_the_chat_and_sends_under_the_gateway_grant(phone_world):
    from test_phone_send import LID, PHONE, answer, local
    s, state, key = phone_world
    jobs, texts = [], []

    async def adapter(job):
        jobs.append(job)
        return answer(name='Synthetic Bakery') if job['parameters'].get('create') is not True else local(job)

    async def text_provider(job):
        texts.append(job)
        return {'provider_message_id': 'wa-1'}

    async def with_workers(arguments):
        task = asyncio.create_task(send(s, key, arguments))
        while not task.done():
            await CommandWorker(state, adapter).once()
            await OutgoingWorker(state, text_provider).once()
            await asyncio.sleep(0.05)
        return await task

    refusal = refused(asyncio.run, with_workers({'to': '+' + PHONE, 'text': 'Synthetic', 'dry_run': True,
                                                  'allow_open_chat': False}))
    assert refusal.code == 'forbidden' and not jobs
    previewed = asyncio.run(with_workers({'to': '+' + PHONE, 'text': 'Synthetic', 'dry_run': True}))
    assert previewed['preview']['recipient']['opens_chat'] is True and previewed['preview']['steps'] == ['phone']
    token = previewed['confirm_token']
    assert refused(asyncio.run, with_workers({'confirm_token': token, 'allow_open_chat': False})).code == 'forbidden'
    result = asyncio.run(with_workers({'confirm_token': token, 'wait_s': 10, 'allow_open_chat': True}))
    assert result['state'] == 'succeeded' and result['steps'][0]['receipt']['provider_message_id'] == 'wa-1'
    assert len(texts) == 1 and texts[0]['provider_chat'] == LID and texts[0]['body'] == 'Synthetic'
