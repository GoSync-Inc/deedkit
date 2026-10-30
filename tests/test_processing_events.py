"""Independent file readiness with exact occurrence fences and journal atomicity."""
import json
from contextlib import contextmanager
import threading

import pytest

from messaging_service.blobs import BlobWriter
from messaging_service.enrichment import EnrichmentWorker
from messaging_service.enrichment_providers import normalize_result
from messaging_service.file_extract import Extraction
from messaging_service.processing_events import artifact_version, safe_identifier
from messaging_service.store import Store
from test_archive import archive as archive, media_event
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
