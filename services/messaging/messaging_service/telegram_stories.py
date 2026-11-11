"""post_story, delete_story, list_own_stories and get_story_limits on Telegram (ANYM-932), beside telegram_operations.

Every request names the owner's own peer (InputPeerSelf); nothing reads or touches another person's stories. A post
asks stories.canSendStory first and posts nothing on a refusal; the file parts are uploaded as requests that publish
nothing, and only stories.sendStory is the mutation, with period 86400, the confirmed audience's one privacy rule
and one random_id derived from the request ID. A provider error that names the refusal (limits, media, caption, a
flood wait) posts nothing; any other failure after sendStory left is unknown and is never retried.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import os
import re
import secrets

from telethon import errors, functions, types

from .messenger_operations import (DELETE_STORY, OWN_STORIES, POST_STORY, STORY_CAPTION_MAX, STORY_LIMITS,
                                   STORY_PERIOD_S)

STORY_OPERATIONS = (POST_STORY, DELETE_STORY, OWN_STORIES, STORY_LIMITS)
RULES = {'everyone': types.InputPrivacyValueAllowAll, 'contacts': types.InputPrivacyValueAllowContacts,
         'close_friends': types.InputPrivacyValueAllowCloseFriends}
DEFAULT_CAPTION_LIMITS = (200, 2048)  # Telegram's documented story caption limits without and with Premium
LIMIT_CODES = {'STORIES_TOO_MUCH': 'stories_limit_reached', 'PREMIUM_ACCOUNT_REQUIRED': 'premium_required'}
FLOOD = re.compile(r'STORY_SEND_FLOOD_(?:WEEKLY|MONTHLY)_([0-9]{1,9})')
CAPTION_CODES = frozenset(('MEDIA_CAPTION_TOO_LONG', 'CAPTION_TOO_LONG'))
MEDIA_PREFIXES = ('MEDIA_', 'VIDEO_', 'PHOTO_', 'IMAGE_', 'FILE_PART')
GONE_CODES = frozenset(('STORY_ID_EMPTY', 'STORY_ID_INVALID'))
NAMES = {cls: name for name, cls in errors.rpcerrorlist.rpc_errors_dict.items()}
MAX_LISTED = 100
PART_SIZE = 512 * 1024


def rejected(code, *, data=None, retry_after=None):
    from .commands import CommandRejected
    return CommandRejected(code, data=data, retry_after=retry_after)


def provider_code(error):
    """The Telegram error name of an RPC error, from Telethon's class or the raw message; None when it has none."""
    name = NAMES.get(type(error))
    if name:
        return name
    message = getattr(error, 'message', None)
    return message if isinstance(message, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', message) else None


def limit_refusal(code):
    """(code, data) of a reached story limit, or None."""
    if code in LIMIT_CODES:
        return LIMIT_CODES[code], {}
    flood = FLOOD.fullmatch(code or '')
    if flood:
        return 'story_flood_wait', {'retry_after_s': max(1, int(flood.group(1)))}
    return None


def utc(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if isinstance(value, datetime) else None


async def stories(op):
    """The one handler of the four operations; a flood wait before the post is rate_limited, nothing posted."""
    from .telegram_governor import ProviderRateLimited
    try:
        return await {POST_STORY: post, DELETE_STORY: delete, OWN_STORIES: own_stories,
                      STORY_LIMITS: limits}[op.operation](op)
    except ProviderRateLimited as error:
        raise rejected('rate_limited', retry_after=error.seconds) from None


async def can_send(op):
    """(None, {count_remains}) when the owner may post a story now, else (refusal code, data)."""
    try:
        answer = await op.invoke(functions.stories.CanSendStoryRequest(types.InputPeerSelf()))
    except errors.RPCError as error:
        refused = limit_refusal(provider_code(error))
        if refused:
            return refused
        raise
    if answer is False:  # an older layer answers a Bool
        return 'stories_limit_reached', {}
    remains = getattr(answer, 'count_remains', None)
    return None, {'count_remains': remains if type(remains) is int and remains >= 0 else None}


async def caption_limit(op):
    """The account's story caption limit (UTF-16 code units) from the app config, Premium or not."""
    try:
        users = await op.invoke(functions.users.GetUsersRequest([types.InputUserSelf()]))
        premium = bool(users) and getattr(users[0], 'premium', None) is True
        config = await op.invoke(functions.help.GetAppConfigRequest(hash=0))
    except errors.RPCError:
        return DEFAULT_CAPTION_LIMITS[0]
    key = 'story_caption_length_limit_' + ('premium' if premium else 'default')
    value = getattr(getattr(config, 'config', None), 'value', None)
    for item in value if isinstance(value, list) else ():
        if isinstance(item, types.JsonObjectValue) and item.key == key and isinstance(item.value, types.JsonNumber):
            number = item.value.value
            if isinstance(number, (int, float)) and 1 <= number <= STORY_CAPTION_MAX and number == int(number):
                return int(number)
    return DEFAULT_CAPTION_LIMITS[premium]


async def limits(op):
    code, data = await can_send(op)
    return {'outcome': 'confirmed', 'basis': 'live_provider_observation', 'allowed': code is None,
            **({'code': code} if code else {}), **data, 'caption_limit': await caption_limit(op)}


def checked_story(job):
    story = job.get('story')
    if (not isinstance(story, dict) or story.get('audience') not in RULES or story.get('period_s') != STORY_PERIOD_S or
            story.get('media_kind') not in ('photo', 'video') or story.get('media_digest') != job.get('file_digest') or
            not isinstance(story.get('caption'), str) or len(story['caption'].encode('utf-16-le')) // 2 > STORY_CAPTION_MAX
            or story.get('media_kind') == 'video' and (type(story.get('duration_s')) is not int or
                                                      not 1 <= story['duration_s'] <= 60)):
        raise rejected('invalid_parameters')
    return story


def random_id(job):
    """One random_id per confirmed request: a resend of the same request is the same story to Telegram. A request
    ID is unique per key, so the key is part of it (Astra r1 M1): two keys' equal request IDs are two stories."""
    request, key = job.get('request_id'), job.get('key_id')
    if (not isinstance(request, str) or not request or not isinstance(job.get('account'), str) or
            not isinstance(job.get('principal'), str) or key is None or not str(key)):
        raise rejected('invalid_parameters')
    digest = hashlib.sha256(('post_story\0%s\0%s\0%s\0%s' % (job['account'], job['principal'], key, request))
                            .encode()).digest()
    return int.from_bytes(digest[:8], 'big') & (2**63 - 1) or 1


async def upload(op, story):
    """The verified original as uploaded parts; the parts publish nothing, so they are not the mutation."""
    job, info, stream = op.job, op.info, op.stream
    name, mime = job.get('file_name'), job.get('file_mime')
    if (not isinstance(name, str) or not 1 <= len(name) <= 255 or any(ord(c) < 32 or c in '/\\' for c in name) or
            not isinstance(mime, str) or not re.fullmatch(r'[\w.+-]+/[\w.+-]+', mime) or
            not mime.startswith('image/' if story['media_kind'] == 'photo' else 'video/')):
        raise rejected('attachment_unavailable')
    sha = hashlib.sha256()
    remaining = info.st_size
    while remaining:
        chunk = stream.read(min(PART_SIZE, remaining))
        if not chunk:
            raise rejected('attachment_unavailable')
        sha.update(chunk)
        remaining -= len(chunk)
    after = os.fstat(stream.fileno())
    if (stream.read(1) or sha.hexdigest() != story['media_digest'] or
            (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns)):
        raise rejected('attachment_unavailable')
    stream.seek(0)
    file_id, parts, large = secrets.randbits(63), (info.st_size + PART_SIZE - 1) // PART_SIZE, info.st_size > 10 * 1024**2
    md5, sha = hashlib.md5(usedforsecurity=False), hashlib.sha256()
    for index in range(parts):
        chunk = stream.read(PART_SIZE)
        if len(chunk) != min(PART_SIZE, info.st_size - index * PART_SIZE):
            raise rejected('attachment_unavailable')
        md5.update(chunk)
        sha.update(chunk)
        request = functions.upload.SaveBigFilePartRequest(file_id, index, parts, chunk) if large else \
            functions.upload.SaveFilePartRequest(file_id, index, chunk)
        if await op.invoke(request) is not True:
            raise rejected('attachment_unavailable')
    if stream.read(1) or sha.hexdigest() != story['media_digest']:
        raise rejected('attachment_unavailable')
    uploaded = types.InputFileBig(file_id, parts, name) if large else types.InputFile(file_id, parts, name, md5.hexdigest())
    if story['media_kind'] == 'photo':
        return types.InputMediaUploadedPhoto(uploaded)
    return types.InputMediaUploadedDocument(uploaded, mime, [types.DocumentAttributeFilename(name),
        types.DocumentAttributeVideo(story['duration_s'], 1, 1, supports_streaming=True)])


def post_refusal(code):
    """(code, data) of a sendStory error that names a refusal: nothing was posted. None for any other."""
    refused = limit_refusal(code)
    if refused:
        return refused
    if code in CAPTION_CODES:
        return 'caption_too_long', {'provider_code': code}
    if code and code.startswith(MEDIA_PREFIXES):
        return 'media_rejected', {'provider_code': code}
    return None


async def post(op):
    from .telegram_governor import ProviderRateLimited
    story, request_random = checked_story(op.job), random_id(op.job)
    code, data = await can_send(op)
    if code:
        raise rejected(code, data=data, retry_after=data.get('retry_after_s'))
    media = await upload(op, story)
    try:
        response = await op.invoke(functions.stories.SendStoryRequest(
            types.InputPeerSelf(), media, [RULES[story['audience']]()], caption=story['caption'] or None,
            random_id=request_random, period=STORY_PERIOD_S), mutation=True)
    except ProviderRateLimited as error:
        op.clear_started()  # Telegram answered with a flood wait: the story was not posted
        raise rejected('rate_limited', retry_after=error.seconds) from None
    except errors.RPCError as error:
        refused = post_refusal(provider_code(error))
        if refused is None:
            raise
        op.clear_started()
        raise rejected(refused[0], data=refused[1], retry_after=refused[1].get('retry_after_s')) from None
    updates = getattr(response, 'updates', None)
    updates = updates if isinstance(updates, list) else []
    ids = {u.id for u in updates[:100] if isinstance(u, types.UpdateStoryID) and u.random_id == request_random}
    story_id = next(iter(ids)) if len(ids) == 1 else None
    if type(story_id) is not int or not 0 < story_id < 2**31:
        return {'outcome': 'unknown', 'error': 'provider_result_unverified'}
    item = next((u.story for u in updates[:100] if isinstance(u, types.UpdateStory) and
                 isinstance(u.story, types.StoryItem) and u.story.id == story_id), None)
    expires = utc(getattr(item, 'expire_date', None))
    basis = 'provider_story'
    if expires is None:
        posted = getattr(item, 'date', None)
        expires = utc((posted if isinstance(posted, datetime) else datetime.now(timezone.utc)) +
                      timedelta(seconds=STORY_PERIOD_S))
        basis = 'requested_period'
    return {'outcome': 'confirmed', 'basis': 'provider_story_id', 'story_id': story_id, 'expires_at': expires,
            'expires_basis': basis, 'audience': story['audience'], 'period_s': STORY_PERIOD_S}


async def delete(op):
    story = op.job.get('story')
    story_id = story.get('story_id') if isinstance(story, dict) else None
    if type(story_id) is not int or not 0 < story_id < 2**31 or op.parameters.get('story_id') != story_id:
        raise rejected('invalid_parameters')
    try:
        answer = await op.invoke(functions.stories.DeleteStoriesRequest(types.InputPeerSelf(), [story_id]), mutation=True)
    except errors.RPCError as error:
        if provider_code(error) not in GONE_CODES:
            raise
        answer = []
    if isinstance(answer, list) and all(type(x) is int for x in answer) and len(answer) <= 1:
        if answer == [story_id]:
            return {'outcome': 'confirmed', 'basis': 'provider_deleted_ids', 'story_id': story_id, 'state': 'deleted',
                    'changed': True}
        if not answer:
            return {'outcome': 'confirmed', 'basis': 'provider_deleted_ids', 'story_id': story_id,
                    'state': 'already_gone', 'changed': False}
    return {'outcome': 'unknown', 'error': 'provider_result_unverified'}


def audience(item):
    if item.public:
        return 'everyone'
    if item.close_friends:
        return 'close_friends'
    if item.selected_contacts:
        return 'selected'
    return 'contacts' if item.contacts else 'unknown'


async def own_stories(op):
    """Every active story of the owner, or a refusal: the list decides an unknown post (posted|not_posted) and a
    delete, so an incomplete one must not answer (Astra r1 B1). A skipped entry is an active story whose body
    Telegram left out: it is read by ID; one still missing, a malformed answer or more than MAX_LISTED refuses."""
    answer = await op.invoke(functions.stories.GetPeerStoriesRequest(types.InputPeerSelf()))
    items = getattr(getattr(answer, 'stories', None), 'stories', None)
    if not isinstance(items, list) or len(items) > MAX_LISTED:
        raise rejected('operation_rejected')
    skipped = [item.id for item in items if isinstance(item, types.StoryItemSkipped)]
    if skipped:
        fetched = await op.invoke(functions.stories.GetStoriesByIDRequest(types.InputPeerSelf(), skipped))
        full = {s.id: s for s in getattr(fetched, 'stories', None) or [] if isinstance(s, types.StoryItem)}
        if set(full) != set(skipped):
            raise rejected('operation_rejected')
        items = [full[item.id] if isinstance(item, types.StoryItemSkipped) else item for item in items]
    rows = []
    for item in items:
        if isinstance(item, types.StoryItemDeleted):
            continue
        if not isinstance(item, types.StoryItem) or type(item.id) is not int:
            raise rejected('operation_rejected')
        media = item.media
        kind = 'photo' if isinstance(media, types.MessageMediaPhoto) else \
            'video' if isinstance(media, types.MessageMediaDocument) else 'other'
        caption = item.caption if isinstance(item.caption, str) else ''
        rows.append({'story_id': item.id, 'kind': kind, 'posted_at': utc(item.date), 'expires_at': utc(item.expire_date),
                     'audience': audience(item), 'caption': caption[:STORY_CAPTION_MAX], 'pinned': item.pinned is True})
    return {'outcome': 'confirmed', 'basis': 'live_provider_observation', 'stories': rows, 'count': len(rows),
            'untrusted': ['stories.caption']}


STORY_HANDLERS = dict.fromkeys(STORY_OPERATIONS, stories)
