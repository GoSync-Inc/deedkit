"""post_story, delete_story, list_own_stories and get_story_limits on Telegram (ANYM-932): synthetic Telethon objects
only; never a provider session."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import time

import pytest
from telethon import errors, functions, types

from messaging_service.commands import CommandRejected
from messaging_service.telegram_operations import execute

OWNER = 7
POSTED = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)


def rpc_error(name):
    """The error Telethon raises for a provider error name: its class when it knows one, else a plain RPCError."""
    known = errors.rpcerrorlist.rpc_errors_dict.get(name)
    return known(request=None) if known else errors.BadRequestError(request=None, message=name, code=400)


def item(story_id, **fields):
    return types.StoryItem(id=story_id, date=POSTED, expire_date=POSTED + timedelta(days=1),
                           media=types.MessageMediaPhoto(), **fields)


class Client:
    """Records every request. `raises` maps a request name to what it raises (a list: one per call, in order);
    `answers` maps it to what it returns."""
    def __init__(self, raises=None, answers=None, story_id=55, premium=False, config=None):
        self.raises, self.answers, self.calls = raises or {}, answers or {}, []
        self.story_id, self.premium, self.config = story_id, premium, config

    async def __call__(self, request):
        name = type(request).__name__
        self.calls.append((name, request))
        raised = self.raises.get(name)
        if isinstance(raised, list) and raised:
            raised = raised.pop(0)
        if isinstance(raised, BaseException):
            raise raised
        if name in self.answers:
            return self.answers[name]
        if isinstance(request, functions.stories.CanSendStoryRequest):
            return types.stories.CanSendStoryCount(count_remains=2)
        if isinstance(request, (functions.upload.SaveFilePartRequest, functions.upload.SaveBigFilePartRequest)):
            return True
        if isinstance(request, functions.stories.SendStoryRequest):
            return types.Updates(updates=[types.UpdateStoryID(id=self.story_id, random_id=request.random_id),
                                          types.UpdateStory(peer=types.PeerUser(OWNER), story=item(self.story_id))],
                                 users=[], chats=[], date=POSTED, seq=0)
        if isinstance(request, functions.stories.DeleteStoriesRequest):
            return list(request.id)
        if isinstance(request, functions.users.GetUsersRequest):
            return [types.User(id=OWNER, premium=self.premium)]
        if isinstance(request, functions.help.GetAppConfigRequest):
            return types.help.AppConfig(hash=1, config=types.JsonObject(self.config or []))
        if isinstance(request, functions.stories.GetPeerStoriesRequest):
            return types.stories.PeerStories(stories=types.PeerStories(peer=types.PeerUser(OWNER), stories=[
                item(1, public=True, caption='Синтетика'), item(2, close_friends=True), item(3, contacts=True),
                item(4, selected_contacts=True), types.StoryItemDeleted(id=5)]), chats=[], users=[])
        raise AssertionError('unexpected request ' + name)


def original(tmp_path, data=b'\xff\xd8synthetic photo'):
    path = tmp_path / 'original'
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


def job(operation, tmp_path=None, *, audience='close_friends', caption='', request_id='req-story-0001', kind='photo',
        **extra):
    base = {'operation': operation, 'account': 'telegram-synthetic', 'request_id': request_id,
            'principal': 'story-agent', 'key_id': '00000000-0000-4000-8000-000000000001',
            'expires_at': time.time() + 60,
            '_policy': {'enabled': True, 'discover_chats': True, 'include_chats': [], 'exclude_chats': []}}
    if operation == 'post_story':
        path, digest = original(tmp_path)
        base.update(parameters={'attachment_id': 'att_' + 'a' * 32, 'sha256': digest, 'caption': caption,
                                'audience': audience},
                    registered_file_path=str(path), file_digest=digest, file_bytes=path.stat().st_size,
                    file_mime='image/jpeg' if kind == 'photo' else 'video/mp4', file_name='story.jpg',
                    story={'media_digest': digest, 'media_kind': kind, 'duration_s': 12 if kind == 'video' else None,
                           'bytes': path.stat().st_size, 'audience': audience, 'caption': caption,
                           'period_s': 86400})
    elif operation == 'delete_story':
        base.update(parameters={'story_id': 55}, story={'story_id': 55, 'posted_by': 'op_' + '1' * 32})
    else:
        base['parameters'] = {}
    base.update(extra)
    return base


def run(client, candidate):
    return asyncio.run(execute(client, candidate))


def names(client):
    return [name for name, _ in client.calls]


def refused(client, candidate):
    with pytest.raises(CommandRejected) as failure:
        run(client, candidate)
    return str(failure.value), failure.value.data, failure.value.retry_after


def sent(client):
    return [request for name, request in client.calls if name == 'SendStoryRequest']


# spec: ANYM-932
def test_a_story_checks_the_limits_uploads_and_posts_once_for_24_hours_to_the_confirmed_audience(tmp_path):
    client = Client()
    result = run(client, job('post_story', tmp_path, caption='Синтетическая подпись'))
    assert names(client) == ['CanSendStoryRequest', 'SaveFilePartRequest', 'SendStoryRequest']
    assert result['outcome'] == 'confirmed'
    assert (result['data']['story_id'], result['data']['expires_at'], result['data']['audience']) == (
        55, '2026-11-12T09:00:00Z', 'close_friends')
    (request,) = sent(client)
    assert request.peer == types.InputPeerSelf() and request.period == 86400
    assert request.privacy_rules == [types.InputPrivacyValueAllowCloseFriends()]
    assert request.caption == 'Синтетическая подпись' and isinstance(request.media, types.InputMediaUploadedPhoto)
    assert client.calls[0][1].peer == types.InputPeerSelf()
    for audience, rule in (('contacts', types.InputPrivacyValueAllowContacts()),
                           ('everyone', types.InputPrivacyValueAllowAll())):
        other = Client()
        run(other, job('post_story', tmp_path, audience=audience))
        assert sent(other)[0].privacy_rules == [rule] and sent(other)[0].caption is None


# spec: ANYM-932
def test_one_random_id_per_request_id_so_a_resend_is_the_same_story(tmp_path):
    first, again, other = Client(), Client(), Client()
    run(first, job('post_story', tmp_path))
    run(again, job('post_story', tmp_path))
    run(other, job('post_story', tmp_path, request_id='req-story-0002'))
    assert sent(first)[0].random_id == sent(again)[0].random_id != sent(other)[0].random_id
    assert 0 < sent(first)[0].random_id < 2**63
    # A request ID is unique per key only: another key's equal request ID is another story (Astra r1 M1).
    another = Client()
    run(another, job('post_story', tmp_path, key_id='00000000-0000-4000-8000-000000000002'))
    assert sent(another)[0].random_id != sent(first)[0].random_id


# spec: ANYM-932
def test_a_video_story_carries_its_archived_duration(tmp_path):
    client = Client()
    run(client, job('post_story', tmp_path, kind='video'))
    media = sent(client)[0].media
    assert isinstance(media, types.InputMediaUploadedDocument) and media.mime_type == 'video/mp4'
    (video,) = [a for a in media.attributes if isinstance(a, types.DocumentAttributeVideo)]
    assert video.duration == 12 and video.supports_streaming is True


# spec: ANYM-932
@pytest.mark.parametrize('provider,code,data', [
    ('STORIES_TOO_MUCH', 'stories_limit_reached', {}),
    ('STORY_SEND_FLOOD_WEEKLY_3600', 'story_flood_wait', {'retry_after_s': 3600}),
    ('STORY_SEND_FLOOD_MONTHLY_86400', 'story_flood_wait', {'retry_after_s': 86400}),
    ('PREMIUM_ACCOUNT_REQUIRED', 'premium_required', {}),
])
def test_a_reached_limit_posts_nothing_and_names_the_limit(tmp_path, provider, code, data):
    before = Client(raises={'CanSendStoryRequest': rpc_error(provider)})
    assert refused(before, job('post_story', tmp_path))[:2] == (code, data)
    assert names(before) == ['CanSendStoryRequest']
    # The same answer to the post itself: nothing was posted, so it is a refusal, never unknown.
    after = Client(raises={'SendStoryRequest': rpc_error(provider)})
    rejected, details, retry = refused(after, job('post_story', tmp_path))
    assert (rejected, details, retry) == (code, data, data.get('retry_after_s'))
    assert names(after).count('SendStoryRequest') == 1


# spec: ANYM-932
@pytest.mark.parametrize('provider', ['MEDIA_INVALID', 'VIDEO_FILE_INVALID', 'PHOTO_INVALID_DIMENSIONS',
                                      'IMAGE_PROCESS_FAILED'])
def test_a_provider_media_rejection_is_media_rejected_with_its_code_and_nothing_posted(tmp_path, provider):
    client = Client(raises={'SendStoryRequest': rpc_error(provider)})
    assert refused(client, job('post_story', tmp_path))[:2] == ('media_rejected', {'provider_code': provider})
    assert names(client).count('SendStoryRequest') == 1
    caption = Client(raises={'SendStoryRequest': rpc_error('MEDIA_CAPTION_TOO_LONG')})
    assert refused(caption, job('post_story', tmp_path))[0] == 'caption_too_long'


# spec: ANYM-932
def test_a_post_that_may_have_happened_is_unknown_and_never_sent_again(tmp_path):
    for raised in (asyncio.TimeoutError(), ConnectionError('synthetic'), rpc_error('RANDOM_ID_DUPLICATE')):
        client = Client(raises={'SendStoryRequest': raised})
        result = run(client, job('post_story', tmp_path))
        assert result['outcome'] == 'unknown' and names(client).count('SendStoryRequest') == 1
    unmatched = Client(answers={'SendStoryRequest': types.Updates(updates=[], users=[], chats=[], date=POSTED, seq=0)})
    assert run(unmatched, job('post_story', tmp_path))['outcome'] == 'unknown'


# spec: ANYM-932
def test_a_failure_before_the_post_left_is_a_refusal_not_unknown(tmp_path):
    upload = Client(raises={'SaveFilePartRequest': ConnectionError('synthetic')})
    assert refused(upload, job('post_story', tmp_path))[0] == 'provider_preflight_failed'
    assert 'SendStoryRequest' not in names(upload)
    changed = job('post_story', tmp_path)
    changed['story'] = {**changed['story'], 'media_digest': '0' * 64}
    client = Client()
    assert refused(client, changed)[0] == 'invalid_parameters' and client.calls == []
    wrong = job('post_story', tmp_path)
    wrong['story'] = {**wrong['story'], 'period_s': 172800}
    assert refused(Client(), wrong)[0] == 'invalid_parameters'


# spec: ANYM-932
def test_delete_story_deletes_the_one_story_or_reports_it_already_gone():
    client = Client()
    result = run(client, job('delete_story'))
    (request,) = [r for name, r in client.calls if name == 'DeleteStoriesRequest']
    assert request.peer == types.InputPeerSelf() and request.id == [55]
    assert (result['outcome'], result['data']['state'], result['data']['changed']) == ('confirmed', 'deleted', True)
    for gone in (Client(answers={'DeleteStoriesRequest': []}),
                 Client(raises={'DeleteStoriesRequest': rpc_error('STORY_ID_INVALID')})):
        result = run(gone, job('delete_story'))
        assert (result['outcome'], result['data']['state'], result['data']['changed']) == (
            'confirmed', 'already_gone', False)
    timeout = Client(raises={'DeleteStoriesRequest': asyncio.TimeoutError()})
    assert run(timeout, job('delete_story'))['outcome'] == 'unknown'
    mismatch = job('delete_story')
    mismatch['parameters'] = {'story_id': 56}
    assert refused(Client(), mismatch)[0] == 'invalid_parameters'


# spec: ANYM-932
def test_list_own_stories_reads_only_the_owners_stories_with_their_audience():
    client = Client()
    result = run(client, job('list_own_stories'))
    (request,) = [r for name, r in client.calls if name == 'GetPeerStoriesRequest']
    assert request.peer == types.InputPeerSelf()
    rows = result['data']['stories']
    assert [(r['story_id'], r['audience']) for r in rows] == [
        (1, 'everyone'), (2, 'close_friends'), (3, 'contacts'), (4, 'selected')]
    assert rows[0] == {'story_id': 1, 'kind': 'photo', 'posted_at': '2026-11-11T09:00:00Z',
                       'expires_at': '2026-11-12T09:00:00Z', 'audience': 'everyone', 'caption': 'Синтетика',
                       'pinned': False}
    assert result['data']['untrusted'] == ['stories.caption'] and result['data']['count'] == 4


def peer_stories(items):
    return types.stories.PeerStories(stories=types.PeerStories(peer=types.PeerUser(OWNER), stories=items),
                                     chats=[], users=[])


# spec: ANYM-932
def test_list_own_stories_reads_a_skipped_story_by_id_and_refuses_an_incomplete_list():
    # A skipped entry is an active story whose body Telegram left out (Astra r1 B1): read by ID, never dropped.
    skipped = types.StoryItemSkipped(id=9, date=POSTED, expire_date=POSTED + timedelta(days=1))
    client = Client(answers={'GetPeerStoriesRequest': peer_stories([item(1, contacts=True), skipped]),
                             'GetStoriesByIDRequest': types.stories.Stories(count=1, stories=[item(9, public=True)],
                                                                            chats=[], users=[])})
    result = run(client, job('list_own_stories'))
    assert [(r['story_id'], r['audience']) for r in result['data']['stories']] == [(1, 'contacts'), (9, 'everyone')]
    (request,) = [r for name, r in client.calls if name == 'GetStoriesByIDRequest']
    assert request.peer == types.InputPeerSelf() and request.id == [9]
    # Still missing, malformed or past the bound: the read refuses, so it never settles an unknown post.
    missing = Client(answers={'GetPeerStoriesRequest': peer_stories([skipped]),
                              'GetStoriesByIDRequest': types.stories.Stories(count=0, stories=[], chats=[], users=[])})
    assert refused(missing, job('list_own_stories'))[0] == 'operation_rejected'
    malformed = Client(answers={'GetPeerStoriesRequest': types.stories.PeerStories(
        stories=types.PeerStories(peer=types.PeerUser(OWNER), stories=None), chats=[], users=[])})
    assert refused(malformed, job('list_own_stories'))[0] == 'operation_rejected'
    many = Client(answers={'GetPeerStoriesRequest': peer_stories([item(n) for n in range(1, 102)])})
    assert refused(many, job('list_own_stories'))[0] == 'operation_rejected'


# spec: ANYM-932
def test_get_story_limits_reads_whether_a_post_is_allowed_and_the_accounts_caption_limit():
    def limit(name, value):
        return types.JsonObjectValue(key=name, value=types.JsonNumber(value=value))
    config = [limit('story_caption_length_limit_default', 200), limit('story_caption_length_limit_premium', 2048)]
    result = run(Client(config=config), job('get_story_limits'))['data']
    assert (result['allowed'], result['count_remains'], result['caption_limit']) == (True, 2, 200)
    assert run(Client(config=config, premium=True), job('get_story_limits'))['data']['caption_limit'] == 2048
    assert run(Client(), job('get_story_limits'))['data']['caption_limit'] == 200  # no config: the default
    flood = run(Client(raises={'CanSendStoryRequest': rpc_error('STORY_SEND_FLOOD_WEEKLY_60')}),
                job('get_story_limits'))['data']
    assert (flood['allowed'], flood['code'], flood['retry_after_s']) == (False, 'story_flood_wait', 60)
    client = Client()
    run(client, job('get_story_limits'))
    assert 'SendStoryRequest' not in names(client)
