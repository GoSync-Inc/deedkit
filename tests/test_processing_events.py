"""Independent file readiness with exact occurrence fences and journal atomicity."""
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import threading

import pytest

from messaging_service.blobs import BlobWriter
from messaging_service.enrichment import EnrichmentWorker
from messaging_service.enrichment_providers import normalize_result
from messaging_service.file_extract import Extraction
from messaging_service.processing_events import artifact_version, safe_identifier
from messaging_service.store import Store
from test_archive import archive as archive, event, media_event
from test_enrichment import add_audio, profile, worker as fixture_worker
from test_file_index import add_file, drain, worker_for


def reset_model_budget(store):
    # The global day ledger has no account FK, so seeded's account cascade
    # intentionally preserves it. Each synthetic model test owns its budget.
    with store.pool.connection() as c:
        c.execute('TRUNCATE enrichment_budget')


def worker(store, tmp_path):
    reset_model_budget(store)
    return fixture_worker(store, tmp_path)


def processing_rows(store, attachment=None):
    with store.pool.connection() as c:
        return c.execute('''SELECT id,kind,revision,origin,snapshot FROM events
            WHERE snapshot ? 'processing' AND (%s::text IS NULL OR
                snapshot->'processing'->>'attachment_id'=%s) ORDER BY id''',
            (attachment, attachment)).fetchall()


def test_artifact_versions_are_occurrence_bound_and_paths_are_not_public():
    row = {'attachment': 'synthetic-a', 'source_revision': 1}
    first = artifact_version(row, 'text', {'text': 'same bytes'})
    assert first == artifact_version(row, 'text', {'text': 'same bytes'})
    assert first != artifact_version({**row, 'attachment': 'synthetic-b'}, 'text', {'text': 'same bytes'})
    assert first != artifact_version({**row, 'source_revision': 2}, 'text', {'text': 'same bytes'})
    assert safe_identifier('/private/models/whisper', 'configured_model') == 'configured_model'


def test_original_ready_does_not_wait_for_text_or_model_processing(archive):
    ar, store, key, _ = archive
    ar.ingest(media_event())
    job = ar.claim_media('synthetic-worker')
    writer = BlobWriter(store.files, reserve_bytes=0)
    blob = writer.write([b'data'])
    ar.finish_media(job, blob, writer)
    events = processing_rows(store, job['id'])
    assert len(events) == 1 and events[0]['kind'] == 'attachment.ready'
    assert events[0]['snapshot']['processing']['stage'] == 'original'
    assert events[0]['snapshot']['processing']['basis'] == 'downloaded'
    assert blob.digest not in json.dumps(events, default=str)
    assert [item['kind'] for item in store.get_updates(key, 'original-stage')['items']] == [
        'message.created', 'attachment.ready']
    with store.pool.connection() as c:
        assert c.execute('SELECT state FROM attachment_text WHERE attachment=%s',
                         (job['id'],)).fetchone()['state'] == 'queued'
        assert c.execute('SELECT state FROM attachment_enrichment WHERE attachment=%s',
                         (job['id'],)).fetchone()['state'] == 'unconfigured'


VOICE = {'provider_id': 'synthetic-voice-1', 'filename': 'synthetic voice.ogg', 'mime': 'audio/ogg', 'bytes': 4}
IMAGE = {'provider_id': 'synthetic-image-1', 'filename': 'synthetic photo.jpg', 'mime': 'image/jpeg', 'bytes': 4}
FILE_STATE = ('media_state', 'next_action', 'processing')


def recent(minutes=1):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


@pytest.fixture()
def watched(archive):
    # A dynamic subscription (every visible chat) names no chat, so a group's files stay on request (ANYM-805).
    ar, store, key, _ = archive
    ar.ingest(event(provider_id='synthetic-seed', sent_at=recent(5), updated_at=recent(5)))
    store.set_dynamic_subscriptions(True)
    try:
        permissions = ['read', 'subscription-write', 'subscription-ack']
        token = store.issue_key('media-state-watcher', ['archive-test'], permissions=permissions,
            key_permissions=permissions)
        sid = store.create_subscription(token, sources={'all_visible_chats': True},
            kinds=['message.created', 'message.edited', 'attachment.ready'])['subscription_id']
        yield ar, store, key, token, sid
    finally:
        store.set_dynamic_subscriptions(False)


def stream(store, token, sid, message, kind, **kwargs):
    from messaging_service.subscriptions import match_subscriptions
    match_subscriptions(store)
    items = store.get_subscription_events(token, sid, limit=50, **kwargs)['items']
    found = [item for item in items if item.get('message') == message and item['kind'] == kind]
    assert len(found) == 1, [(item['kind'], item.get('message')) for item in items]
    return found[0]


def download(ar, store):
    job = ar.claim_media('synthetic-worker')
    writer = BlobWriter(store.files, reserve_bytes=0)
    ar.finish_media(job, writer.write([b'data']), writer)
    return job


def read_state(store, key, mid, group):
    [file] = store.get_context(key, mid)[group]
    return file['media']


def state_of(file):
    return {name: file[name] for name in FILE_STATE}


# spec: ANYM-866
def test_each_file_in_the_stream_carries_the_state_the_message_read_shows(watched):
    ar, store, key, token, sid = watched
    sent = recent()
    mid = ar.ingest(media_event(sent_at=sent, updated_at=sent))['message']
    [waiting] = stream(store, token, sid, mid, 'message.created')['attachments']
    assert waiting == {'attachment_id': waiting['attachment_id'], 'mime': 'text/plain', 'bytes': 4,
        'media_state': 'queued', 'next_action': None,
        'processing': {'text': 'queued', 'transcript': 'not_applicable', 'description': 'not_applicable'}}
    assert read_state(store, key, mid, 'pending_attachments') == state_of(waiting)
    job = download(ar, store)
    [ready] = stream(store, token, sid, mid, 'attachment.ready')['attachments']
    assert ready['attachment_id'] == job['id'] == waiting['attachment_id']
    assert state_of(ready) == {'media_state': 'ready', 'next_action': 'get_attachment',
        'processing': {'text': 'queued', 'transcript': 'not_applicable', 'description': 'not_applicable'}}
    assert read_state(store, key, mid, 'attachments') == state_of(ready)
    # The created event is read now too: the same file, the same current state.
    assert stream(store, token, sid, mid, 'message.created')['attachments'] == [ready]
    compact = stream(store, token, sid, mid, 'attachment.ready', detail='compact')
    assert compact['attachments'] == [ready]
    assert 'private' not in json.dumps([waiting, ready])  # no file name or path


# spec: ANYM-866
def test_a_voice_note_of_an_on_request_group_waits_for_a_request_in_the_stream_and_the_read(watched):
    from test_media_eligibility import publish
    from test_media_policy import set_kind
    ar, store, key, token, sid = watched
    publish(ar)  # a collector run's published window: the read knows the chat's download rule
    sent = recent()
    mid = ar.ingest(event(media=[VOICE], sent_at=sent, updated_at=sent))['message']
    set_kind(store, mid, 'group')
    [file] = stream(store, token, sid, mid, 'message.created')['attachments']
    assert state_of(file) == {'media_state': 'available_on_request', 'next_action': 'request_file_download',
        'processing': {'text': 'available_on_request', 'transcript': 'not_applicable', 'description': 'not_applicable'}}
    assert read_state(store, key, mid, 'pending_attachments') == state_of(file)


# spec: ANYM-866
def test_a_download_that_failed_for_good_shows_failed_and_no_action_in_the_stream_and_the_read(watched):
    ar, store, key, token, sid = watched
    sent = recent()
    mid = ar.ingest(media_event(sent_at=sent, updated_at=sent))['message']
    with store.pool.connection() as c:
        c.execute("UPDATE media_jobs SET state='failed',error_code='network_error' WHERE message=%s", (mid,))
    [file] = stream(store, token, sid, mid, 'message.created')['attachments']
    assert state_of(file) == {'media_state': 'failed', 'next_action': None,
        'processing': {'text': 'failed', 'transcript': 'not_applicable', 'description': 'not_applicable'}}
    assert read_state(store, key, mid, 'pending_attachments') == state_of(file)


# spec: ANYM-866
def test_a_downloaded_voice_note_of_an_on_request_group_without_an_audio_profile_expects_no_transcript(watched):
    from test_media_policy import set_kind
    ar, store, key, token, sid = watched
    sent = recent()
    mid = ar.ingest(event(media=[VOICE], sent_at=sent, updated_at=sent))['message']
    download(ar, store)
    set_kind(store, mid, 'group')  # e.g. its subscription ended: recognition now waits for a request (ANYM-805)
    [file] = stream(store, token, sid, mid, 'attachment.ready')['attachments']
    assert state_of(file) == {'media_state': 'ready', 'next_action': 'get_attachment',
        'processing': {'text': 'available_on_request', 'transcript': 'not_applicable',
            'description': 'not_applicable'}}
    assert read_state(store, key, mid, 'attachments') == state_of(file)


# spec: ANYM-866
@pytest.mark.parametrize('media,profiles,later', [
    (IMAGE, ['image'], {'transcript': 'not_applicable', 'description': 'queued'}),
    (VOICE, [], {'transcript': 'not_applicable', 'description': 'not_applicable'}),
    (VOICE, ['audio'], {'transcript': 'queued', 'description': 'not_applicable'}),
], ids=['image', 'voice_without_audio_profile', 'voice_with_audio_profile'])
def test_the_later_stages_are_only_the_ones_a_file_of_its_kind_gets(watched, media, profiles, later):
    ar, store, key, token, sid = watched
    with store.pool.connection() as c:
        for kind in profiles:
            c.execute('''INSERT INTO processing_profiles(account,kind,provider,model,enabled,expires_at)
                VALUES('archive-test',%s,'openai','fixture-model',true,now()+interval '1 day')''', (kind,))
    sent = recent()
    mid = ar.ingest(event(media=[media], sent_at=sent, updated_at=sent))['message']
    [waiting] = stream(store, token, sid, mid, 'message.created')['attachments']
    assert waiting['processing'] == {'text': 'queued', **later}
    download(ar, store)
    [ready] = stream(store, token, sid, mid, 'attachment.ready')['attachments']
    assert ready['media_state'] == 'ready' and ready['processing'] == {'text': 'queued', **later}
    assert read_state(store, key, mid, 'attachments') == state_of(ready)


# spec: ANYM-866
def test_an_edit_that_replaces_a_file_shows_the_old_one_superseded_and_the_new_one_in_the_edit(watched):
    ar, store, key, token, sid = watched
    sent = recent(2)
    mid = ar.ingest(event(media=[IMAGE], sent_at=sent, updated_at=sent))['message']
    assert ar.ingest(event(media=[VOICE], body='synthetic replacement', sent_at=sent,
        updated_at=recent()))['outcome'] == 'edited'
    [old] = stream(store, token, sid, mid, 'message.created')['attachments']
    [new] = stream(store, token, sid, mid, 'message.edited')['attachments']
    assert old['attachment_id'] != new['attachment_id']
    assert state_of(old) == {'media_state': 'superseded', 'next_action': None,
        'processing': {'text': 'not_applicable', 'transcript': 'not_applicable', 'description': 'not_applicable'}}
    assert new['media_state'] == 'queued' and new['mime'] == 'audio/ogg'
    assert read_state(store, key, mid, 'pending_attachments') == state_of(new)


# spec: ANYM-866
def test_a_corrupt_job_row_degrades_only_its_own_file_and_is_counted(watched, capsys):
    from messaging_service.attachment_metadata import MEDIA_STATE_ERRORS
    ar, store, _, token, sid = watched
    sent = recent()
    mid = ar.ingest(event(media=[IMAGE, VOICE], sent_at=sent, updated_at=sent))['message']
    other = ar.ingest(event(provider_id='synthetic-other', media=[VOICE], sent_at=sent, updated_at=sent))['message']
    with store.pool.connection() as c:
        c.execute('''UPDATE media_jobs SET descriptor='"synthetic-corrupt"'::jsonb
            WHERE message=%s AND descriptor->>'mime'='audio/ogg' ''', (mid,))
    before = MEDIA_STATE_ERRORS['AttributeError']
    capsys.readouterr()
    image, broken = stream(store, token, sid, mid, 'message.created')['attachments']
    assert MEDIA_STATE_ERRORS['AttributeError'] == before + 1  # one page read, one unreadable file
    assert '"attachment_media_state_unavailable"' in capsys.readouterr().out
    assert image['media_state'] == 'queued' and image['mime'] == 'image/jpeg'
    assert broken == {'attachment_id': broken['attachment_id'], 'mime': None, 'bytes': None, 'media_state': None,
        'next_action': None, 'processing': None, 'media_error': 'AttributeError'}
    assert [f['media_state'] for f in stream(store, token, sid, other, 'message.created')['attachments']] == ['queued']


# spec: ANYM-866
def test_a_failed_media_lookup_never_fails_the_event_page(watched, monkeypatch, capsys):
    from messaging_service import result_evidence
    from messaging_service.attachment_metadata import MEDIA_STATE_ERRORS
    ar, store, _, token, sid = watched
    sent = recent()
    mid = ar.ingest(media_event(sent_at=sent, updated_at=sent))['message']

    def broken(c, items):
        c.execute('SELECT 1/0')
    monkeypatch.setattr(result_evidence, 'event_attachments', broken)
    before = MEDIA_STATE_ERRORS['lookup_failed']
    capsys.readouterr()
    item = stream(store, token, sid, mid, 'message.created')
    assert 'attachments' not in item and item['message'] == mid
    assert MEDIA_STATE_ERRORS['lookup_failed'] == before + 1
    assert '"lookup_failed"' in capsys.readouterr().out


# spec: ANYM-866
@pytest.mark.parametrize('method', ['get_subscription_events', 'fetch_subscription_events',
    'wait_for_subscription_events'])
def test_a_held_lock_on_a_table_the_lookup_reads_never_holds_the_page_its_receipt_or_the_ack(watched, method):
    import asyncio
    import time
    import psycopg
    from messaging_service.attachment_metadata import MEDIA_STATE_ERRORS
    from messaging_service.subscription_consumption import wait_for_subscription_events
    from messaging_service.subscriptions import match_subscriptions
    ar, store, _, token, sid = watched
    sent = recent()
    mid = ar.ingest(media_event(sent_at=sent, updated_at=sent))['message']
    match_subscriptions(store)
    before = MEDIA_STATE_ERRORS['lookup_failed']
    with psycopg.connect(store.pool.conninfo) as holder:
        holder.execute('LOCK TABLE processing_profiles IN ACCESS EXCLUSIVE MODE')
        started = time.monotonic()
        if method == 'wait_for_subscription_events':
            page = asyncio.run(wait_for_subscription_events(store, token, sid, timeout=0))
        else:
            page = getattr(store, method)(token, sid)
        elapsed = time.monotonic() - started
        holder.rollback()
    assert elapsed < 2.5  # the lookup's own 500 ms budgets, not the pool's 5 s statement timeout
    [item] = [i for i in page['items'] if i.get('message') == mid and i['kind'] == 'message.created']
    assert 'attachments' not in item and MEDIA_STATE_ERRORS['lookup_failed'] == before + 1
    if method != 'get_subscription_events':
        store.ack_subscription_events(token, sid, page['receipt'], page['ack_version'])
        assert store.get_subscription_events(token, sid)['subscription']['acked_through'] >= item['event_id']
    # The next page, with no lock held, carries the files again.
    [again] = [i for i in store.get_subscription_events(token, sid)['items'] if i.get('event_id') == item['event_id']]
    assert again['attachments'][0]['media_state'] == 'queued'


def test_text_ready_dedup_reuse_is_scoped_and_legacy_updates_are_unchanged(seeded):
    store, _, key = seeded
    drain(worker_for(store))
    digest = add_file(store, 'ready-public', 'demo-tg-1', b'synthetic duplicate source')
    add_file(store, 'ready-other', 'demo-wa-1', b'synthetic duplicate source')
    calls = []
    def extractor(*_, **__):
        calls.append(True)
        return Extraction('ready', 'SEARCH_TEXT_SENTINEL', 'fixture-text')
    drain(worker_for(store, extractor=extractor))
    assert len(calls) == 1  # The second occurrence reuses a verified parser artifact.
    public = processing_rows(store, 'ready-public')
    other = processing_rows(store, 'ready-other')
    assert len(public) == len(other) == 1
    assert public[0]['kind'] == other[0]['kind'] == 'file.text.ready'
    first, second = public[0]['snapshot']['processing'], other[0]['snapshot']['processing']
    assert first['artifact_version'] != second['artifact_version']
    assert first['basis'] == 'extracted' and first['text_chars'] == len('SEARCH_TEXT_SENTINEL')
    assert first['source_revision'] == public[0]['revision'] == 1
    payload = json.dumps(public + other, default=str)
    assert digest not in payload and 'SEARCH_TEXT_SENTINEL' not in payload
    assert 'ready-other' not in json.dumps(public, default=str)
    assert all(not item['kind'].startswith('file.') for item in store.get_updates(key, 'legacy')['items'])


def test_text_result_and_event_rollback_together_then_finish_is_once(seeded, monkeypatch):
    store, _, _ = seeded
    drain(worker_for(store))
    add_file(store, 'atomic-ready', 'demo-tg-1', b'atomic fixture')
    indexer = worker_for(store)
    job = indexer.claim()
    append = Store.append_event
    def fail(*_, **__):
        raise RuntimeError('synthetic-publication-crash')
    monkeypatch.setattr(Store, 'append_event', staticmethod(fail))
    with pytest.raises(RuntimeError, match='synthetic-publication-crash'):
        indexer.finish(job, Extraction('ready', 'retained'))
    with store.pool.connection() as c:
        row = c.execute("SELECT state,text_content FROM attachment_text WHERE attachment='atomic-ready'").fetchone()
    assert row == {'state': 'leased', 'text_content': ''}
    assert not processing_rows(store, 'atomic-ready')
    monkeypatch.setattr(Store, 'append_event', staticmethod(append))
    assert indexer.finish(job, Extraction('ready', 'retained')) == 'ready'
    assert indexer.finish(job, Extraction('ready', 'retained')) == 'superseded'
    assert len(processing_rows(store, 'atomic-ready')) == 1


@pytest.mark.parametrize('change', ["revision=revision+1", "deleted=true", "revision=revision+1,deleted=true"])
def test_obsolete_text_and_enrichment_results_publish_no_ready_event(seeded, tmp_path, change):
    store, _, _ = seeded
    drain(worker_for(store))
    add_file(store, 'obsolete-text', 'demo-tg-2', b'obsolete fixture')
    add_audio(store)
    indexer, enricher = worker_for(store), worker(store, tmp_path)
    job = indexer.claim()
    model_job, _ = enricher.claim()
    with store.pool.connection() as c:
        c.execute("UPDATE messages SET " + change + " WHERE id='demo-tg-2'")
    assert indexer.finish(job, Extraction('ready', 'must not appear')) == 'superseded'
    assert enricher.finish(model_job, normalize_result({'text': 'must not appear'}, audio=True)) is False
    assert not processing_rows(store, 'obsolete-text')
    assert not processing_rows(store, 'enrich-audio')


@pytest.mark.parametrize('kind,mime,expected', [('audio', 'audio/wav', 'file.transcript.ready'),
    ('image', 'image/png', 'file.description.ready')])
def test_transcript_and_description_readiness_have_independent_artifact_evidence(seeded, tmp_path, kind, mime, expected):
    store, _, _ = seeded
    add_file(store, 'model-ready', 'demo-tg-2', b'fake model source', mime=mime)
    cfg = {'accounts': ['telegram-demo'], 'max_jobs_per_day': 20,
        'profiles': {kind: profile(tmp_path, kind=kind)}}
    reset_model_budget(store)
    result = normalize_result({'title': 'PRIVATE_MODEL_TITLE', 'summary': 'PRIVATE_MODEL_SUMMARY',
        'text': 'PRIVATE_MODEL_TEXT'}, audio=kind == 'audio')
    enricher = EnrichmentWorker(store.pool.conninfo, store.files, cfg, processor=lambda *_: result)
    assert enricher.once()
    ready = processing_rows(store, 'model-ready')
    assert len(ready) == 1 and ready[0]['kind'] == expected
    metadata = ready[0]['snapshot']['processing']
    assert metadata['basis'] == 'model_generated' and metadata['state'] == 'ready'
    assert metadata['text_chars'] == len('PRIVATE_MODEL_TEXT')
    assert metadata['provider'] == 'openai' and metadata['model'] == 'fixture-model'
    assert 'PRIVATE_MODEL_' not in json.dumps(ready, default=str)
    assert not any(row['kind'] == 'file.text.ready' for row in ready)


def test_failed_attempt_reports_retryability_and_expired_final_lease_once(seeded, tmp_path):
    store, _, _ = seeded
    drain(worker_for(store))
    add_file(store, 'retry-text', 'demo-tg-1', b'retry fixture')
    indexer = worker_for(store)
    job = indexer.claim()
    assert indexer.finish(job, Extraction('failed', error_code='extraction_timeout')) == 'retry'
    failed = processing_rows(store, 'retry-text')
    assert failed[0]['kind'] == 'file.processing.failed'
    assert failed[0]['snapshot']['processing']['retryable'] is True
    with store.pool.connection() as c:
        c.execute("UPDATE attachment_text SET next_due=clock_timestamp() WHERE attachment='retry-text'")
    job = indexer.claim()
    with store.pool.connection() as c:
        c.execute("UPDATE attachment_text SET attempts=3,lease_until=now()-interval '1 second' WHERE attachment='retry-text'")
    assert indexer.claim() is None
    assert indexer.claim() is None
    final = processing_rows(store, 'retry-text')
    assert len(final) == 2
    assert final[-1]['snapshot']['processing']['error_code'] == 'retry_exhausted'
    assert final[-1]['snapshot']['processing']['retryable'] is False
    assert indexer.finish(job, Extraction('ready', 'stale final result')) == 'superseded'
    add_audio(store)
    enricher = worker(store, tmp_path)
    row, _ = enricher.claim()
    with store.pool.connection() as c:
        c.execute("UPDATE attachment_enrichment SET attempts=3,lease_until=now()-interval '1 second' WHERE attachment='enrich-audio'")
    assert enricher.claim() is None
    assert enricher.claim() is None
    final = processing_rows(store, 'enrich-audio')
    assert len(final) == 1 and final[0]['kind'] == 'file.processing.failed'
    assert final[0]['snapshot']['processing']['retryable'] is False


def test_enrichment_event_failure_rolls_back_ready_state(seeded, tmp_path, monkeypatch):
    store, _, _ = seeded
    add_audio(store)
    enricher = worker(store, tmp_path)
    job, _ = enricher.claim()
    def fail(*_, **__):
        raise RuntimeError('synthetic-enrichment-publication-crash')
    monkeypatch.setattr(Store, 'append_event', staticmethod(fail))
    with pytest.raises(RuntimeError, match='synthetic-enrichment-publication-crash'):
        enricher.finish(job, normalize_result({'text': 'retained'}, audio=True))
    with store.pool.connection() as c:
        row = c.execute("SELECT state,text_content FROM attachment_enrichment WHERE attachment='enrich-audio'").fetchone()
    assert row == {'state': 'leased', 'text_content': ''}
    assert not processing_rows(store, 'enrich-audio')


@pytest.mark.parametrize('kind', ['text', 'transcript'])
def test_expired_multi_account_publication_does_not_deadlock_ingestion_or_queue_claim(seeded, tmp_path, kind):
    """An ingest owns B while expiry work starts with A; both need the journal.

    Publishing A before locking B would form account-B/feed-counter deadlock.
    Continuing to claim arbitrary rows in that same transaction can create a
    second row/counter cycle. Verify real concurrency and transaction boundaries.
    """
    store, _, _ = seeded
    drain(worker_for(store))
    mime = 'text/plain' if kind == 'text' else 'audio/wav'
    for attachment, message in [('expired-a', 'demo-tg-1'), ('expired-b', 'demo-wa-1')]:
        add_file(store, attachment, message, b'concurrent fixture', mime=mime)
    if kind == 'text':
        target = worker_for(store)
        table = 'attachment_text'
    else:
        cfg = {'accounts': ['telegram-demo', 'whatsapp-demo-1'], 'max_jobs_per_day': 20,
            'profiles': {'audio': profile(tmp_path, kind='audio')}}
        reset_model_budget(store)
        target = EnrichmentWorker(store.pool.conninfo, store.files, cfg)
        table = 'attachment_enrichment'
    assert target.claim() is not None and target.claim() is not None
    with store.pool.connection() as c:
        c.execute('UPDATE ' + table + " SET attempts=3,lease_until=now()-interval '1 second' WHERE attachment=ANY(%s)",
                  (['expired-a', 'expired-b'],))
    held, first_feed = threading.Event(), threading.Event()
    failures, trace = [], []
    original_connection = target.connection
    @contextmanager
    def observed_connection():
        transaction_id = len({item[0] for item in trace})
        with original_connection() as c:
            class Observed:
                def __getattr__(self, name):
                    return getattr(c, name)
                def execute(self, query, params=None, **kwargs):
                    if 'pg_advisory_xact_lock(hashtextextended' in query:
                        lock_kind = 'lifecycle' if params[0] == 'attachment-residency-v1' else 'account'
                        trace.append((transaction_id, lock_kind, params[0]))
                    # Schema 37 advances the counter inside the fenced publication function.
                    elif query.startswith('UPDATE feed_counter') or query.startswith('SELECT public.publish_file_'):
                        trace.append((transaction_id, 'feed', None))
                        first_feed.set()
                    elif 'FOR UPDATE' in query and 'SKIP LOCKED' in query:
                        trace.append((transaction_id, 'candidate', None))
                    return c.execute(query, params, **kwargs)
            yield Observed()
    target.connection = observed_connection
    def ingest_b():
        try:
            with store.pool.connection() as c:
                c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('archive:whatsapp-demo-1',))
                held.set()
                # Under the safe ordering expiry cannot publish until we leave
                # B; under the broken ordering publication wakes this contender.
                first_feed.wait(0.3)
                Store.append_event(c, 'whatsapp-demo-1', 'demo-wa-1', 'message.status')
        except Exception as exc:
            failures.append(exc)
            held.set()
    contender = threading.Thread(target=ingest_b)
    contender.start()
    try:
        assert held.wait(2)
        assert target.claim() is None
    finally:
        first_feed.set()
        contender.join(6)
    assert not contender.is_alive() and not failures
    assert len(processing_rows(store, 'expired-a')) == len(processing_rows(store, 'expired-b')) == 1
    feed_index = next(i for i, item in enumerate(trace) if item[1] == 'feed')
    assert [(item[1], item[2]) for item in trace[:feed_index]] == [
        ('lifecycle', 'attachment-residency-v1'),
        ('account', 'archive:telegram-demo'), ('account', 'archive:whatsapp-demo-1')]
    assert {item[0] for item in trace if item[1] == 'feed'} == {0}
    assert {item[0] for item in trace if item[1] == 'candidate'} == {1}
    assert next(item[1:] for item in trace if item[0] == 1) == ('lifecycle', 'attachment-residency-v1')
