"""Occurrence-scoped journal references for fenced file processing results.

No generated text, global blob identity or processing credentials enter the
event payload. Publication uses the caller's result transaction and account
lock, so a ready row and its readiness event become visible together.
"""
from __future__ import annotations

import hashlib
import json
import re


READY_KINDS = {
    'text': 'file.text.ready',
    'transcript': 'file.transcript.ready',
    'description': 'file.description.ready',
}


def artifact_version(row, stage, identity):
    """An opaque version specific to one occurrence and source revision.

    ``identity`` may include private processor settings and the validated
    artifact content for hashing, but neither is disclosed in the event.
    Salt with the occurrence ID to avoid a cross-chat equality oracle.
    """
    encoded = json.dumps([row['attachment'], row['source_revision'], stage, identity],
                         ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return 'artifact_' + hashlib.sha256(encoded.encode()).hexdigest()


def safe_identifier(value, fallback):
    """Local model paths and arbitrary provider strings stay in the worker."""
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) else fallback


def publish(c, row, *, stage, state, identity, basis, truncated=False,
            text_chars=0, extractor=None, provider=None, model=None,
            error_code=None, retryable=False):
    """Publish one idempotent readiness/failure event for a current occurrence.

    Caller must hold the ``archive:<account>`` transaction advisory lock before
    changing the processing row. A second source fence here makes misuse fail
    closed, rather than attaching a stale artifact to the current revision.
    """
    from .store import Store

    if stage not in READY_KINDS or state not in ('ready', 'failed'):
        raise ValueError('invalid_processing_event')
    if basis not in ('extracted', 'model_generated'):
        raise ValueError('invalid_processing_event')
    if type(text_chars) is not int or not 0 <= text_chars <= 1_000_000:
        raise ValueError('invalid_processing_event')
    source = c.execute('''SELECT m.account,m.id AS message,m.revision,
        coalesce(r.snapshot->>'origin','reconcile') AS origin
        FROM attachments a JOIN messages m ON m.id=a.message
        LEFT JOIN message_revisions r ON r.message=m.id AND r.revision=m.revision
        WHERE a.id=%s AND a.active AND NOT m.deleted AND m.account=%s
        AND m.revision=%s AND a.digest=%s''',
        (row['attachment'], row['account'], row['source_revision'], row['source_digest'])).fetchone()
    if not source:
        return None
    processing = {
        'attachment_id': row['attachment'],
        'stage': stage,
        'state': state,
        'artifact_version': artifact_version(row, stage, identity),
        'basis': basis,
        'source_revision': source['revision'],
        'truncated': bool(truncated),
        'text_chars': text_chars,
    }
    if extractor is not None:
        processing['extractor'] = safe_identifier(extractor, 'configured_extractor')
    if provider is not None:
        processing['provider'] = safe_identifier(provider, 'configured_provider')
    if model is not None:
        processing['model'] = safe_identifier(model, 'configured_model')
    if state == 'failed':
        processing.update(error_code=error_code if isinstance(error_code, str)
            and re.fullmatch(r'[a-z_]{1,64}', error_code) else 'processing_failed',
            retryable=bool(retryable))
    kind = READY_KINDS[stage] if state == 'ready' else 'file.processing.failed'
    # Account locking serializes checks for this occurrence. A stale worker or
    # replay never advances the committed publication frontier a second time.
    event_key = 'processing_' + hashlib.sha256(json.dumps(
        [kind, processing], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    previous = c.execute('SELECT id FROM events WHERE event_key=%s', (event_key,)).fetchone()
    if previous:
        return previous['id']
    return Store.append_event(c, source['account'], source['message'], kind,
        source['origin'], event_key, snapshot_metadata={'processing': processing},
        publisher='publish_file_text_event' if stage == 'text' else 'publish_file_enrichment_event')
