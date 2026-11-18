"""A blocked file says why, in the event stream and the message read alike (ANYM-866).

Each case reaches its state through the real path that produces it: the account policy that excludes the chat,
the collector's local-storage failure (fail_media disk_reserve) and the download size bound, both where the
claim's storage admission refuses the file and where the collector's transfer hits it (fail_media size_limit).
"""
import json

import pytest

from messaging_service.blobs import BlobWriter
from test_archive import archive, media_event  # noqa: F401 (archive is a fixture)
from test_media_eligibility import publish
from test_processing_events import FILE_STATE, read_state, recent, stream, watched  # noqa: F401 (watched is a fixture)

BLOCKED_STAGES = {'text': 'not_applicable', 'transcript': 'not_applicable', 'description': 'not_applicable'}


def blocked(reason):
    return {'media_state': 'blocked', 'next_action': None, 'reason': reason, 'processing': BLOCKED_STAGES}


def file_states(store, key, token, sid, mid):
    """The file as the created event shows it (full and compact) and as the message read shows it."""
    [full] = stream(store, token, sid, mid, 'message.created')['attachments']
    [compact] = stream(store, token, sid, mid, 'message.created', detail='compact')['attachments']
    read = read_state(store, key, mid, 'pending_attachments')
    assert 'private' not in json.dumps([full, compact, read])  # no file name or path
    pick = lambda file: {name: file.get(name) for name in (*FILE_STATE, 'reason')}  # noqa: E731
    return pick(full), pick(compact), pick(read)


def ingest(ar, provider_id):
    sent = recent()
    return ar.ingest(media_event(sent_at=sent, updated_at=sent, provider_id=provider_id))['message']


def job_of(store, mid):
    with store.pool.connection() as c:
        return c.execute('SELECT state,error_code FROM media_jobs WHERE message=%s', (mid,)).fetchone()


# spec: ANYM-866
def test_a_file_of_a_chat_the_policy_excludes_is_blocked_with_the_collection_reason(watched):  # noqa: F811
    ar, store, key, token, sid = watched
    publish(ar)
    mid = ingest(ar, 'blocked-excluded')
    with store.pool.connection() as c:
        c.execute("UPDATE account_policies SET exclude_chats=ARRAY['100'] WHERE account='archive-test'")
    assert job_of(store, mid) == {'state': 'queued', 'error_code': None}
    assert file_states(store, key, token, sid, mid) == (blocked('collection_excluded'),) * 3


# spec: ANYM-866
def test_a_download_paused_for_local_storage_is_blocked_with_the_policy_reason(watched):  # noqa: F811
    ar, store, key, token, sid = watched
    publish(ar)
    mid = ingest(ar, 'blocked-storage')
    ar.fail_media(ar.claim_media('synthetic-worker'), 'disk_reserve')
    assert job_of(store, mid) == {'state': 'queued', 'error_code': 'paused_storage'}
    assert file_states(store, key, token, sid, mid) == (blocked('policy'),) * 3


# spec: ANYM-866
@pytest.mark.parametrize('path', ['claim_admission', 'collector_transfer'])
def test_a_file_over_the_download_size_bound_is_blocked_with_the_size_limit_reason(watched, path):  # noqa: F811
    ar, store, key, token, sid = watched
    publish(ar)
    mid = ingest(ar, 'blocked-size-' + path)
    if path == 'claim_admission':
        # The storage admission refuses a 4-byte original against a 2-byte bound: no claim, the job fails.
        assert ar.claim_media('synthetic-worker', writer=BlobWriter(store.files, max_bytes=2, reserve_bytes=0)) is None
    else:
        # The collector's transfer hit the bound (collector_media: media_size_limit -> size_limit, permanent).
        ar.fail_media(ar.claim_media('synthetic-worker'), 'size_limit', permanent=True)
    assert job_of(store, mid) == {'state': 'failed', 'error_code': 'size_limit'}
    assert file_states(store, key, token, sid, mid) == (blocked('size_limit'),) * 3
