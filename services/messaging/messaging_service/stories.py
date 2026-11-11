"""post_story and delete_story (ANYM-932): the archive side, beside commands.py.

`bind` puts the reviewed story into the payload, so `preview_digest` binds the media digest and kind, the caption,
the audience and the lifetime. The media is checked from its stored metadata before the owner is asked: a photo
(JPEG, PNG or WebP, at most 10 MiB) or a video of at most 60 s by its archived duration (an upload has none) within
the send_file bound; anything else is media_unsuitable with its reason. While a post of the same media on this
account is pending or unknown and no later list_own_stories read has answered, a new post_story is refused with
operation_result_unknown (the join_chat rule). delete_story takes only a story ID of a post_story receipt of this
account. `preview_with_limits` adds one get_story_limits read: a reached limit or a caption over the account's limit
refuses the preview; the limits are shown and never part of the digest.
"""
import asyncio
import hashlib
import math
import time

from .field_errors import refusal
from .messenger_operations import (DELETE_STORY, MAX_REGISTERED_FILE_BYTES, POST_STORY, STORY_LIMITS, STORY_PERIOD_S,
                                   STORY_VIDEO_MAX_S)
from .store import APIError

PHOTO_MIMES = ('image/jpeg', 'image/png', 'image/webp')
PHOTO_MAX_BYTES = 10 * 1024**2
NOT_PHOTO_OR_VIDEO = ('sticker', 'animation', 'voice', 'audio')
LIMITS_WAIT_S = 10
LIMITS_FRESH_S = 30  # one live limits read per account in this window, however often the preview is asked
AUDIENCE_WORDS = {'contacts': 'the owner\'s Telegram contacts', 'close_friends': 'the owner\'s Close Friends list',
                  'everyone': 'everyone on Telegram, contacts or not'}
PUBLICATION = ('A story is published under the owner\'s name to this audience; Telegram removes it after 24 hours. '
               'Confirm the exact media, caption and audience.')
DELETE_EFFECT = 'Deletes this story for everyone who can see it; it cannot be restored.'
NOT_POSTED_HERE = ('the story_id of a post_story receipt of this account; a story posted from the phone is deleted '
                   'in the Telegram app')
LIMIT_REFUSALS = ('stories_limit_reached', 'story_flood_wait', 'premium_required')
# A limits read that did not answer refuses the preview with its own code, when the agent can act on it.
LIMITS_UNAVAILABLE = ('session_unavailable', 'sender_not_ready', 'operation_disabled', 'rate_limited', 'forbidden')


def unsuitable(reason):
    failure = APIError('media_unsuitable', 409)
    setattr(failure, 'reason', reason)
    return failure


def bind(store, c, p, account, operation, parameters, payload, saved, *, verify_file=False, retained_upload=False):
    """The reviewed story into the payload; returns (archive references, provider chats the policy must allow)."""
    if operation == DELETE_STORY:
        return _bind_delete(c, account, parameters, payload)
    refs, chats, duration, media_kind = [], [], None, None
    if 'upload_id' in parameters:
        from .uploads import owned, source
        upload = owned(c, p, parameters['upload_id'])
        if upload['account'] != account:
            raise APIError('not_found', 404)
        original = source(store, c, p, account, upload['chat'], parameters, verify=verify_file, retained=retained_upload)
        payload.update(file_upload_id=original['id'], file_digest=original['sha256'], file_bytes=original['bytes'],
                       file_mime=original['mime'].lower(), file_name=original['filename'])
        refs.append(original['id'])
    else:
        from .commands import _chat, _registered_file
        original = _registered_file(store, c, p, account, parameters, verify=verify_file)
        source_chat = _chat(c, p, account, original['chat'])
        payload.update(file_message_id=original['message'], file_message_revision=original['revision'],
                       file_chat_id=original['chat'], file_digest=original['digest'], file_bytes=original['bytes'],
                       file_mime=original['mime'].lower(), file_name=original['filename'])
        chats.append(source_chat['provider_chat'])
        refs.extend((original['chat'], original['message'], original['id']))
        meta = c.execute('''SELECT metadata->'media_duration_seconds' AS duration,metadata->>'media_kind' AS kind
            FROM message_metadata WHERE message=%s AND revision=%s''', (original['message'], original['revision'])).fetchone()
        if meta:
            duration, media_kind = meta['duration'], meta['kind']
    kind, seconds = _media(payload['file_mime'], payload['file_bytes'], duration, media_kind)
    payload['story'] = {'media_digest': payload['file_digest'], 'media_kind': kind, 'duration_s': seconds,
                        'bytes': payload['file_bytes'], 'audience': parameters['audience'],
                        'caption': parameters.get('caption', ''), 'period_s': STORY_PERIOD_S}
    if saved is None:
        guard(c, account, payload['file_digest'])
    return refs, chats


def _media(mime, size, duration, media_kind):
    """(kind, video seconds) of a suitable story medium, else media_unsuitable with its reason."""
    kind = 'photo' if mime.startswith('image/') else 'video' if mime.startswith('video/') else None
    if kind is None or media_kind in NOT_PHOTO_OR_VIDEO or kind == 'photo' and mime not in PHOTO_MIMES:
        raise unsuitable('kind')
    if size > (PHOTO_MAX_BYTES if kind == 'photo' else MAX_REGISTERED_FILE_BYTES):
        raise unsuitable('size')
    if kind == 'photo':
        return kind, None
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= STORY_VIDEO_MAX_S:
        raise unsuitable('duration')  # an unknown duration is not shown to be within 60 s
    return kind, max(1, math.ceil(duration))


def guard(c, account, digest):
    """A post of this media on the account that may have happened blocks a new one until a later list_own_stories
    read has answered; no time limit ends it."""
    if c.execute('''SELECT 1 FROM messenger_commands j WHERE j.account=%s AND j.operation=%s
        AND j.payload->'story'->>'media_digest'=%s AND (j.status IN ('queued','dispatching') OR j.status='unknown'
        AND NOT EXISTS (SELECT 1 FROM messenger_commands k WHERE k.account=j.account AND k.operation='list_own_stories'
            AND k.status='succeeded' AND k.started_at>coalesce(j.finished_at,j.started_at,j.created_at))) LIMIT 1''',
                 (account, POST_STORY, digest)).fetchone():
        raise APIError('operation_result_unknown', 409)


def _bind_delete(c, account, parameters, payload):
    story_id = parameters['story_id']
    row = c.execute('''SELECT id FROM messenger_commands WHERE account=%s AND operation=%s AND status='succeeded'
        AND result->'data'->>'story_id'=%s ORDER BY finished_at DESC,id LIMIT 1''',
                    (account, POST_STORY, str(story_id))).fetchone()
    if not row:
        raise refusal('invalid_operation_parameters', [('story_id', 'not_posted_here', NOT_POSTED_HERE)])
    payload['story'] = {'story_id': story_id, 'posted_by': row['id']}
    return [], []


def shown(payload):
    """What the owner confirms."""
    story = payload['story']
    if payload['operation'] == DELETE_STORY:
        return {'story_id': story['story_id'], 'posted_by_operation_id': story['posted_by'], 'effect': DELETE_EFFECT}
    parameters = payload['parameters']
    return {'media': {'sha256': story['media_digest'], 'kind': story['media_kind'], 'duration_s': story['duration_s'],
                      'bytes': story['bytes'], 'mime': payload['file_mime'],
                      'source': 'upload_id' if 'upload_id' in parameters else 'attachment_id'},
            'caption': story['caption'], 'audience': story['audience'],
            'audience_words': AUDIENCE_WORDS[story['audience']], 'lifetime': '24 hours',
            'lifetime_s': story['period_s'], 'effect': PUBLICATION,
            'limits': {'state': 'unknown', 'basis': 'not_read'}}


async def preview_with_limits(store, token, arguments):
    """The post_story preview, then one live limits read: a reached limit or a longer caption refuses it."""
    draft = await asyncio.to_thread(store.preview_messenger_operation, token, **arguments)
    read = await live_limits(store, token, draft['account'])
    return apply_limits(draft, read)


async def live_limits(store, token, account):
    from .send_wait import wait_for_operation
    window = int(time.time() // LIMITS_FRESH_S)
    key = hashlib.sha256(f'{account}\0{window}'.encode()).hexdigest()[:40]
    try:
        receipt = await asyncio.to_thread(store.execute_messenger_operation, token, account, STORY_LIMITS,
                                          parameters={}, request_id='story-limits:' + key)
        if receipt.get('status') in ('queued', 'dispatching'):
            receipt = await wait_for_operation(store, token, receipt['operation_id'], LIMITS_WAIT_S)
    except asyncio.CancelledError:
        raise
    except APIError as error:
        return {'unavailable': error.code}
    except Exception:
        return {'unavailable': 'limits_read_failed'}
    data = (receipt.get('result') or {}).get('data') if receipt.get('status') == 'succeeded' else None
    if not isinstance(data, dict) or type(data.get('allowed')) is not bool or type(data.get('caption_limit')) is not int:
        failed = (receipt.get('result') or {}).get('data') or {}
        return {'unavailable': failed.get('code') if isinstance(failed, dict) and failed.get('code') else
                receipt.get('error_code') or 'limits_read_failed'}
    return data


def apply_limits(draft, read):
    if 'unavailable' in read:
        failure = APIError(read['unavailable'] if read['unavailable'] in LIMITS_UNAVAILABLE else 'provider_timeout', 409)
        setattr(failure, 'next_step', 'preview again once the Telegram session answers; nothing was posted')
        raise failure
    if not read['allowed']:
        code = read.get('code') if read.get('code') in LIMIT_REFUSALS else 'stories_limit_reached'
        failure = APIError(code, 409)
        if type(read.get('retry_after_s')) is int:
            setattr(failure, 'retry_after_s', read['retry_after_s'])
        raise failure
    caption, limit = draft['parameters'].get('caption', ''), read['caption_limit']
    if len(caption.encode('utf-16-le')) // 2 > limit:
        raise refusal('caption_too_long', [('caption', 'max_length', 'at most %d UTF-16 code units for this account; '
                                            'most emoji count as 2' % limit)])
    draft['story_preview']['limits'] = {'state': 'allowed', 'basis': 'live_provider_observation', 'caption_limit': limit,
                                'count_remains': read.get('count_remains')}
    return draft
