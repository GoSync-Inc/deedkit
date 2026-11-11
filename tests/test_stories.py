"""post_story and delete_story through the command store (ANYM-932): preview, digest, refusals, the unknown guard and
receipts. A synthetic Telegram account and fake providers only; nothing reaches Telegram."""
import asyncio
import hashlib

import pytest

from messaging_service.archive import Archive
from messaging_service.collector_state import CollectorState
from messaging_service.envelope import stable_id
from messaging_service.store import APIError
from test_archive import event

ACCOUNT = 'story-fixture'
POSTER = ['read', 'download', 'read-live', 'stories-post']


@pytest.fixture
def poster(seeded):
    s, _, _ = seeded
    archive = Archive(s)
    archive.register(ACCOUNT, 'telegram', 'Synthetic stories', discover=True)
    key = s.issue_key('story-agent', [ACCOUNT], permissions=POSTER)
    epoch = archive.acquire(ACCOUNT, 'synthetic-story-worker')
    state = CollectorState(archive, ACCOUNT, ('synthetic-story-worker', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    s.set_messenger_operations(ACCOUNT, ['post_story', 'delete_story'])
    yield s, archive, key, state
    archive.release(ACCOUNT, 'synthetic-story-worker', epoch)


def original(s, archive, provider_id, content, mime, **metadata):
    """An archived message of the owner's Saved Messages with one registered original; returns (attachment, sha)."""
    archive.ingest(event(account=ACCOUNT, chat='7', provider_id=provider_id, direction='outgoing',
                         body='Synthetic media ' + provider_id, **({'metadata': metadata} if metadata else {})))
    sha = hashlib.sha256(content).hexdigest()
    s.files.mkdir(parents=True, exist_ok=True)
    (s.files / sha).write_bytes(content)
    aid = stable_id('att', ACCOUNT, '7', provider_id, 'synthetic-file')
    with s.pool.connection() as c:
        c.execute('INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES(%s,%s,%s,%s,%s,%s)',
                  (aid, stable_id('msg', ACCOUNT, '7', provider_id), sha, 'synthetic', mime, len(content)))
    return aid, sha


def photo(s, archive, provider_id='1', content=b'\xff\xd8 synthetic photo'):
    return original(s, archive, provider_id, content, 'image/jpeg', media_kind='photo')


def story_args(aid, sha, **parameters):
    return {'account': ACCOUNT, 'operation': 'post_story',
            'parameters': {'attachment_id': aid, 'sha256': sha, 'audience': 'contacts', **parameters}}


def post(s, key, args, request_id='synthetic-story-0001'):
    draft = s.preview_messenger_operation(key, **args)
    return s.execute_messenger_operation(key, **args, request_id=request_id, preview_digest=draft['preview_digest'])


def drain(state, answer):
    from messaging_service.commands import CommandWorker
    jobs = []
    async def provider(job):
        jobs.append(job)
        if isinstance(answer, Exception):
            raise answer
        return answer
    asyncio.run(CommandWorker(state, provider).once())
    return jobs


def commands(s, operation):
    with s.pool.connection() as c:
        return c.execute('SELECT status FROM messenger_commands WHERE operation=%s', (operation,)).fetchall()


# spec: ANYM-932
def test_a_story_preview_shows_media_caption_audience_and_lifetime_and_its_digest_binds_them(poster):
    s, archive, key, _ = poster
    aid, sha = photo(s, archive)
    draft = s.preview_messenger_operation(key, **story_args(aid, sha, caption='Синтетика'))
    shown = draft['story_preview']
    assert shown['media'] == {'sha256': sha, 'kind': 'photo', 'duration_s': None, 'bytes': len(b'\xff\xd8 synthetic photo'),
                              'mime': 'image/jpeg', 'source': 'attachment_id'}
    assert (shown['caption'], shown['audience'], shown['lifetime'], shown['lifetime_s']) == (
        'Синтетика', 'contacts', '24 hours', 86400)
    assert 'contacts' in shown['audience_words'] and 'under the owner\'s name' in shown['effect']
    assert draft['story'] == {'media_digest': sha, 'media_kind': 'photo', 'duration_s': None,
                              'bytes': len(b'\xff\xd8 synthetic photo'), 'audience': 'contacts', 'caption': 'Синтетика',
                              'period_s': 86400}
    assert (draft['mutation'], draft['permission'], draft['owner_approval_verified_by_server']) == (
        True, 'stories-post', False)
    assert s.preview_messenger_operation(key, **story_args(aid, sha, caption='Синтетика'))['preview_digest'] == \
        draft['preview_digest']
    other, other_sha = photo(s, archive, '2', b'\xff\xd8 another synthetic photo')
    changed = [story_args(aid, sha, caption='Другая'), story_args(aid, sha, caption='Синтетика', audience='everyone'),
               story_args(other, other_sha, caption='Синтетика')]
    assert len({s.preview_messenger_operation(key, **args)['preview_digest'] for args in changed} |
               {draft['preview_digest']}) == 4
    # A confirmed digest of one audience does not post to another.
    with pytest.raises(APIError, match='^preview_mismatch$'):
        s.execute_messenger_operation(key, **story_args(aid, sha, caption='Синтетика', audience='everyone'),
                                      request_id='synthetic-story-0009', preview_digest=draft['preview_digest'])
    assert commands(s, 'post_story') == []


# spec: ANYM-932
def test_unsuitable_media_is_refused_with_its_reason_before_the_owner_is_asked(poster):
    s, archive, key, _ = poster
    video, video_sha = original(s, archive, '3', b'synthetic video 12s', 'video/mp4', media_kind='video',
                                media_duration_seconds=12.4)
    draft = s.preview_messenger_operation(key, **story_args(video, video_sha))
    assert (draft['story']['media_kind'], draft['story']['duration_s']) == ('video', 13)
    cases = [
        (original(s, archive, '4', b'synthetic video 61s', 'video/mp4', media_kind='video', media_duration_seconds=61),
         'duration'),
        (original(s, archive, '5', b'synthetic video without duration', 'video/mp4', media_kind='video'), 'duration'),
        (original(s, archive, '6', b'%PDF synthetic', 'application/pdf', media_kind='document'), 'kind'),
        (original(s, archive, '7', b'RIFF synthetic sticker', 'image/webp', media_kind='sticker'), 'kind'),
        (original(s, archive, '8', b'GIF89a synthetic', 'image/gif', media_kind='photo'), 'kind'),
        (original(s, archive, '9', b'\xff\xd8' + b'0' * (10 * 1024**2), 'image/jpeg', media_kind='photo'), 'size'),
    ]
    for (aid, sha), reason in cases:
        with pytest.raises(APIError, match='^media_unsuitable$') as failure:
            s.preview_messenger_operation(key, **story_args(aid, sha))
        assert failure.value.reason == reason
    assert commands(s, 'post_story') == [] and commands(s, 'get_story_limits') == []


# spec: ANYM-932
def test_story_operations_need_stories_post_and_a_full_account_key(poster):
    s, archive, key, _ = poster
    aid, sha = photo(s, archive)
    plain = s.issue_key('plain-agent', [ACCOUNT], permissions=['read', 'download', 'read-live', 'send'])
    with pytest.raises(APIError, match='^forbidden$'):
        s.preview_messenger_operation(plain, **story_args(aid, sha))
    scoped = s.issue_key('scoped-agent', [ACCOUNT], chats=[stable_id('chat', ACCOUNT, '7')], permissions=POSTER)
    with pytest.raises(APIError, match='^forbidden$'):
        s.preview_messenger_operation(scoped, **story_args(aid, sha))
    rows = {r['operation']: r for r in s.list_messenger_operations(key, ACCOUNT)['items']}
    assert all(rows[name]['key_permitted'] and rows[name]['available']
               for name in ('post_story', 'delete_story', 'get_story_limits', 'list_own_stories'))
    others = {r['operation']: r for r in s.list_messenger_operations(plain, ACCOUNT)['items']}
    assert not any(others[name]['key_permitted'] for name in ('post_story', 'delete_story', 'get_story_limits'))
    # The story reads ride post_story's opt-in: without it none of the four is available.
    s.set_messenger_operations(ACCOUNT, ['delete_story'])
    rows = {r['operation']: r for r in s.list_messenger_operations(key, ACCOUNT)['items']}
    assert not any(rows[name]['account_enabled'] for name in ('post_story', 'get_story_limits', 'list_own_stories'))
    with pytest.raises(APIError, match='^operation_disabled$'):
        s.preview_messenger_operation(key, **story_args(aid, sha))


# spec: ANYM-932
def test_an_upload_only_key_posts_a_photo_upload_whose_original_stays_until_the_story_is_dispatched(poster):
    import base64
    s, archive, _, state = poster
    archive.set_sending(ACCOUNT, True)
    s.set_messenger_operations(ACCOUNT, ['post_story', 'delete_story', 'send_file'])  # an upload is send_file's
    key = s.issue_key('upload-agent', [ACCOUNT], permissions=['read', 'send', 'upload', 'stories-post'])
    archive.ingest(event(account=ACCOUNT, chat='7', provider_id='1', direction='outgoing', body='Synthetic'))
    rows = {r['operation']: r for r in s.list_messenger_operations(key, ACCOUNT)['items']}
    assert rows['post_story']['key_permitted'] and rows['post_story']['available']  # Astra r1 M3
    assert rows['post_story']['source_permissions']['upload_id'] == ['read', 'send', 'stories-post', 'upload']
    content = b'\xff\xd8 synthetic uploaded photo'
    sha = hashlib.sha256(content).hexdigest()
    upload = s.prepare_file_upload(key, account=ACCOUNT, chat_id=stable_id('chat', ACCOUNT, '7'), filename='p.jpg',
                                   mime='image/jpeg', bytes=len(content), sha256=sha, request_id='synthetic-upload-01')
    s.upload_file_chunk(key, upload['upload_id'], 0, base64.b64encode(content).decode())
    args = {'account': ACCOUNT, 'operation': 'post_story',
            'parameters': {'upload_id': upload['upload_id'], 'sha256': sha, 'audience': 'close_friends'}}
    assert s.preview_messenger_operation(key, **args)['story_preview']['media']['source'] == 'upload_id'
    queued = post(s, key, args)
    # The upload expires while the story waits: the queued story keeps it (Astra r1 M2).
    with s.pool.connection() as c:
        c.execute("UPDATE file_uploads SET expires_at=clock_timestamp()-interval '1 second'")
    s.cleanup_file_uploads()
    (job,) = drain(state, {'outcome': 'confirmed', 'data': {'story_id': 56, 'expires_at': '2026-11-12T09:00:00Z'}})
    assert job['registered_file_path'].endswith(upload['upload_id'] + '/' + sha)
    assert s.get_messenger_operation(key, queued['operation_id'])['status'] == 'succeeded'


# spec: ANYM-932
def test_a_limits_read_that_never_answers_refuses_the_preview(poster, monkeypatch):
    from messaging_service import stories
    monkeypatch.setattr(stories, 'LIMITS_WAIT_S', 0.2)  # no collector runs here: the read stays queued
    monkeypatch.setattr(stories, 'LIMITS_FRESH_S', 10**9)  # one window for the whole test, never a boundary
    s, archive, key, _ = poster
    aid, sha = photo(s, archive)
    with pytest.raises(APIError, match='^provider_timeout$') as failure:
        asyncio.run(stories.preview_with_limits(s, key, story_args(aid, sha)))
    assert 'nothing was posted' in failure.value.next_step
    # One limits read per account in a 30 s window, however often the preview is asked.
    with pytest.raises(APIError, match='^provider_timeout$'):
        asyncio.run(stories.preview_with_limits(s, key, story_args(aid, sha, caption='again')))
    assert len(commands(s, 'get_story_limits')) == 1 and commands(s, 'post_story') == []


# spec: ANYM-932
def test_the_preview_reads_the_live_limits_and_refuses_a_reached_limit_or_a_longer_caption(poster, monkeypatch):
    from messaging_service import stories
    s, archive, key, _ = poster
    aid, sha = photo(s, archive)
    reads = []
    def answer(data, status='succeeded', error_code=None):
        def execute(token, account, operation, chat_id='', parameters=None, request_id=''):
            reads.append((operation, parameters, request_id))
            return {'status': status, 'result': {'data': data}, 'error_code': error_code}
        monkeypatch.setattr(s, 'execute_messenger_operation', execute)
    answer({'allowed': True, 'count_remains': 3, 'caption_limit': 10})
    draft = asyncio.run(stories.preview_with_limits(s, key, story_args(aid, sha, caption='0123456789')))
    assert draft['story_preview']['limits'] == {'state': 'allowed', 'basis': 'live_provider_observation',
                                                'caption_limit': 10, 'count_remains': 3}
    assert reads[0][:2] == ('get_story_limits', {}) and reads[0][2].startswith('story-limits:')
    assert 'limits' not in draft['story']  # shown beside the digest, never in it
    with pytest.raises(APIError, match='^caption_too_long$') as failure:
        asyncio.run(stories.preview_with_limits(s, key, story_args(aid, sha, caption='01234567890')))
    assert [(e['field'], e['reason']) for e in failure.value.field_errors] == [('caption', 'max_length')]
    for data, code, retry in (({'allowed': False, 'code': 'stories_limit_reached', 'caption_limit': 200},
                               'stories_limit_reached', None),
                              ({'allowed': False, 'code': 'story_flood_wait', 'retry_after_s': 3600,
                                'caption_limit': 200}, 'story_flood_wait', 3600),
                              ({'allowed': False, 'code': 'premium_required', 'caption_limit': 200},
                               'premium_required', None)):
        answer(data)
        with pytest.raises(APIError, match='^' + code + '$') as failure:
            asyncio.run(stories.preview_with_limits(s, key, story_args(aid, sha)))
        assert getattr(failure.value, 'retry_after_s', None) == retry
    answer({'code': 'rate_limited'}, status='failed', error_code='rate_limited')
    with pytest.raises(APIError, match='^rate_limited$'):
        asyncio.run(stories.preview_with_limits(s, key, story_args(aid, sha)))
    reads.clear()
    with pytest.raises(APIError, match='^media_unsuitable$'):  # a refused preview reads nothing live
        asyncio.run(stories.preview_with_limits(s, key, story_args(*original(
            s, archive, '6', b'%PDF synthetic', 'application/pdf'))))
    assert reads == []


# spec: ANYM-932
def test_a_confirmed_post_names_its_story_and_expiry_and_a_refusal_posts_nothing(poster):
    from messaging_service.commands import CommandRejected
    s, archive, key, state = poster
    aid, sha = photo(s, archive)
    queued = post(s, key, story_args(aid, sha))
    (job,) = drain(state, {'outcome': 'confirmed', 'data': {'story_id': 55, 'expires_at': '2026-11-12T09:00:00Z'}})
    assert job['story']['media_digest'] == sha and job['registered_file_path'].endswith(sha)
    receipt = s.get_messenger_operation(key, queued['operation_id'])
    assert receipt['status'] == 'succeeded' and 'verify' not in receipt
    assert (receipt['result']['data']['story_id'], receipt['result']['data']['expires_at']) == (
        55, '2026-11-12T09:00:00Z')
    for request, code, data in (('synthetic-story-0002', 'story_flood_wait', {'retry_after_s': 60}),
                                ('synthetic-story-0003', 'media_rejected', {'provider_code': 'MEDIA_INVALID'})):
        queued = post(s, key, story_args(aid, sha, caption=request), request)
        drain(state, CommandRejected(code, data=data))
        receipt = s.get_messenger_operation(key, queued['operation_id'])
        assert (receipt['status'], receipt['error_code'], receipt['result']['data']) == (
            'failed', code, {'code': code, **data})


# spec: ANYM-932
def test_an_unknown_post_names_its_verify_read_and_blocks_the_same_media_until_it_answers(poster):
    s, archive, key, state = poster
    aid, sha = photo(s, archive)
    other, other_sha = photo(s, archive, '2', b'\xff\xd8 another synthetic photo')
    queued = post(s, key, story_args(aid, sha))
    # Pending: a second post of the same media is refused at preview and at execute.
    with pytest.raises(APIError, match='^operation_result_unknown$'):
        s.preview_messenger_operation(key, **story_args(aid, sha, audience='everyone'))
    drain(state, {'outcome': 'unknown', 'data': {'error': 'provider_result_unverified'}})
    receipt = s.get_messenger_operation(key, queued['operation_id'])
    assert receipt['status'] == 'unknown' and receipt['automatic_retry'] is False
    assert receipt['verify'] == {'operation': 'list_own_stories', 'requires': 'read-live', 'decides': 'posted|not_posted'}
    with pytest.raises(APIError, match='^operation_result_unknown$'):
        s.preview_messenger_operation(key, **story_args(aid, sha, caption='a new request'))
    with pytest.raises(APIError, match='^operation_result_unknown$'):
        s.execute_messenger_operation(key, **story_args(aid, sha), request_id='synthetic-story-0002',
                                      preview_digest='0' * 64)
    assert s.preview_messenger_operation(key, **story_args(other, other_sha))['preview_digest']
    # The same request ID returns the same unknown receipt; it is never posted again.
    assert s.execute_messenger_operation(key, **story_args(aid, sha), request_id='synthetic-story-0001',
                                         preview_digest=queued['preview_digest'])['status'] == 'unknown'
    read = s.execute_messenger_operation(key, account=ACCOUNT, operation='list_own_stories',
                                         request_id='synthetic-own-stories-01')
    drain(state, {'outcome': 'confirmed', 'data': {'stories': [], 'count': 0}})
    assert s.get_messenger_operation(key, read['operation_id'])['status'] == 'succeeded'
    assert s.preview_messenger_operation(key, **story_args(aid, sha))['preview_digest']


# spec: ANYM-932
def test_delete_story_takes_only_a_story_this_account_posted_here(poster):
    s, archive, key, state = poster
    def delete_args(story_id):
        return {'account': ACCOUNT, 'operation': 'delete_story', 'parameters': {'story_id': story_id}}
    with pytest.raises(APIError, match='^invalid_operation_parameters$') as failure:
        s.preview_messenger_operation(key, **delete_args(55))
    assert [(e['field'], e['reason']) for e in failure.value.field_errors] == [('story_id', 'not_posted_here')]
    aid, sha = photo(s, archive)
    posted = post(s, key, story_args(aid, sha))
    drain(state, {'outcome': 'confirmed', 'data': {'story_id': 55, 'expires_at': '2026-11-12T09:00:00Z'}})
    draft = s.preview_messenger_operation(key, **delete_args(55))
    assert draft['story'] == {'story_id': 55, 'posted_by': posted['operation_id']}
    assert draft['story_preview']['story_id'] == 55 and 'cannot be restored' in draft['story_preview']['effect']
    queued = s.execute_messenger_operation(key, **delete_args(55), request_id='synthetic-story-delete-01',
                                           preview_digest=draft['preview_digest'])
    (job,) = drain(state, {'outcome': 'confirmed', 'data': {'story_id': 55, 'state': 'already_gone', 'changed': False}})
    assert job['story'] == {'story_id': 55, 'posted_by': posted['operation_id']}
    receipt = s.get_messenger_operation(key, queued['operation_id'])
    assert (receipt['status'], receipt['result']['data']['state']) == ('succeeded', 'already_gone')
    with pytest.raises(APIError, match='^invalid_operation_parameters$'):
        s.preview_messenger_operation(key, **delete_args(56))
