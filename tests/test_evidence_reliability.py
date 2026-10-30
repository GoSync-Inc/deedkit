"""Synthetic regressions for bounded, current agent-facing evidence."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.types.json import Jsonb

from messaging_service.attachment_metadata import attachment_fields
from messaging_service.file_index import file_response_fields
from messaging_service.status_details import describe, freshness
from test_archive import archive as archive, event
from test_collector_operations import source
from test_whatsapp import wa as wa, capture, event as wa_event


def test_freshness_preserves_scan_age_but_reports_capture_backlog_and_lease():
    now = datetime.now(timezone.utc)
    scanned = now-timedelta(seconds=2)
    result = freshness(scanned, now, operational={
        'live_capture': {'state': 'backlogged', 'pending_events': 44,
            'collector_running': True, 'last_commit': {'committed_by': (now-timedelta(days=1)).isoformat()}}})
    assert result['state'] == 'capture_backlogged'
    assert result['scan_state'] == 'recent_scan' and result['scan_age_seconds'] == 2
    assert result['pending_capture_events'] == 44
    assert result['last_capture_commit_at'] == (now-timedelta(days=1)).isoformat()
    stopped = freshness(scanned, now, operational={
        'live_capture': {'state': 'not_running', 'collector_running': False}})
    assert stopped['state'] == 'collector_not_running' and stopped['scan_state'] == 'recent_scan'


def test_idle_capture_old_commit_and_storage_pause_do_not_invent_message_lag():
    now = datetime.now(timezone.utc)
    result = freshness(now-timedelta(seconds=2), now, operational={
        'live_capture': {'state': 'receiving', 'pending_events': 0, 'collector_running': True,
            'last_commit': {'committed_by': (now-timedelta(days=10)).isoformat()}},
        'media': {'storage_paused_jobs': 3}})
    assert result['state'] == result['scan_state'] == 'recent_scan'
    assert result['qualifications'] == ['media_storage_paused']


def test_status_combines_recent_scan_with_live_backlog_without_leaking_to_chat_scope(wa):
    w, s, _ = wa
    asyncio.run(w.sync_once())
    w.state.capture_progress(None, 0, 44)
    result = source(w, s)
    assert result['freshness']['state'] == 'capture_backlogged'
    assert result['freshness']['scan_state'] == 'recent_scan'
    assert result['freshness']['pending_capture_events'] == 44
    assert 'capture_backlog' in result['alerts']
    w.archive.release(w.account, w.owner, w.state.lease[1])
    assert source(w, s)['freshness']['state'] == 'collector_not_running'
    chat = s.search(s.issue_key('synthetic-evidence-reader', [w.account]))['items'][0]['chat']
    assert 'freshness' not in source(w, s, [chat])


@pytest.mark.parametrize('state,action,retry', [
    ('queued', 'wait_for_media_retry', True),
    ('leased', 'get_attachment', False),
    ('failed', 'inspect_download_failure', False),
])
def test_media_diagnostics_distinguish_retry_from_terminal_failure(state, action, retry):
    result = attachment_fields({'attachment_id': 'synthetic-job', 'job_state': state,
        'descriptor': {'filename': 'photo.jpg', 'mime': 'image/jpeg', 'bytes': 10},
        'error_code': 'network_error', 'attempts': 5 if state == 'failed' else 1})
    assert result['next_action'] == action
    assert result['download_retry_scheduled'] is retry
    assert result['error_detail']['retry_scheduled'] is retry


def test_media_storage_pause_and_terminal_failures_remain_independent(wa):
    w, s, _ = wa
    for number in (1, 2, 3):
        w.state.apply(capture(wa_event(number, media=True)))
    with s.pool.connection() as c:
        jobs = c.execute('SELECT id FROM media_jobs ORDER BY id').fetchall()
        c.execute("UPDATE media_jobs SET error_code='paused_storage' WHERE id=%s", (jobs[0]['id'],))
        c.execute("UPDATE media_jobs SET state='failed',error_code='network_error',attempts=5 WHERE id=%s", (jobs[1]['id'],))
        c.execute("UPDATE media_jobs SET error_code='network_error',attempts=1 WHERE id=%s", (jobs[2]['id'],))
    media = source(w, s)['operations']['media']
    assert media['storage_paused_jobs'] == media['terminal_failed_jobs'] == media['retry_queued_jobs'] == 1
    assert media['needs_attention'] is True and media['storage_paused'] is True
    network = [d for d in media['diagnostics'] if d['reason_code'] == 'network_error']
    assert {(d['job_state'], d['retry_scheduled'], d['count']) for d in network} == {
        ('queued', True, 1), ('failed', False, 1)}
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET error_code='storage_unavailable' WHERE id=%s", (jobs[0]['id'],))
    unavailable = source(w, s)['operations']['media']
    assert unavailable['state'] == 'storage_unavailable'
    assert unavailable['storage_unavailable_jobs'] == unavailable['terminal_failed_jobs'] == 1
    assert unavailable['storage_paused_jobs'] == 0 and unavailable['retry_queued_jobs'] == 1


def test_attachment_and_text_coverage_agree_and_stale_derivatives_are_excluded(archive):
    from messaging_service.blobs import BlobWriter
    ar, s, key, _ = archive
    ar.ingest(event(media=[{'provider_id': 'synthetic-media', 'filename': 'note.txt',
        'mime': 'text/plain', 'bytes': 69}]))['message']
    job = ar.claim_media('synthetic-fixture')
    writer = BlobWriter(s.files, reserve_bytes=0)
    blob = writer.write([b'x'*69])
    ar.finish_media(job, blob, writer=writer)
    with s.pool.connection() as c:
        c.execute("""UPDATE attachment_text SET state='ready',text_content=%s,
            processor_version='bounded-v2',truncated=true,extraction_metadata=%s
            WHERE attachment=%s""", ('x'*69, Jsonb({'pages_total': 2, 'page_spans': [{'start': 0, 'end': 69}]}), job['id']))
    text = s.get_file_text(key, job['id'])
    for result in (s.get_attachment(key, job['id']), s.request_file_download(key, job['id'])):
        assert result['extraction_coverage'] == text['extraction_coverage']
        assert result['extraction_coverage']['text_chars'] == 69
        assert result['extraction_coverage']['text_char_limit'] == 100000
        assert result['extraction_coverage']['truncated'] is True
        assert 'page_spans' not in result['extraction_coverage']
    (s.files/blob.digest).unlink()
    assert s.get_attachment(key, job['id'])['extraction_coverage'] == text['extraction_coverage']
    with s.pool.connection() as c:
        c.execute('UPDATE attachment_text SET source_revision=999 WHERE attachment=%s', (job['id'],))
    stale = s.get_attachment(key, job['id'])
    assert stale['extraction_status'] == 'unknown'
    assert stale['extraction_coverage']['text_chars'] == 0
    assert 'pages_total' not in stale['extraction_coverage']


def test_generated_whatsapp_filename_is_presentation_only_and_mime_bounded():
    legacy = 'false_1234567890@c.us_SYNTHETIC.bin'
    row = {'attachment_id': 'synthetic-photo', 'filename': legacy, 'mime': 'image/jpeg',
        'provider_platform': 'whatsapp', 'provider_message_id': legacy[:-4], 'media_kind': 'photo'}
    result = file_response_fields(row)
    assert result['filename'] == result['source_filename'] == legacy
    assert result['display_name'] == result['download_filename'] == 'attachment-synthetic-photo.jpg'
    assert result['display_name_basis'] == 'generated_from_media_type'
    for filename, mime in [('sample.bin', 'image/jpeg'), (legacy, 'application/octet-stream')]:
        result = file_response_fields({**row, 'filename': filename, 'mime': mime})
        assert result['display_name'] == result['download_filename'] == filename
        assert result['display_name_basis'] == 'source_filename'


@pytest.mark.parametrize('changes', [
    {'provider_platform': 'telegram'}, {'media_kind': 'document'},
    {'media_kind': None}, {'provider_message_id': 'another-provider-message'},
    {'provider_platform': None},
])
def test_matching_shaped_real_or_uncertain_filenames_are_never_replaced(changes):
    filename = 'false_1234567890@c.us_SYNTHETIC.bin'
    row = {'attachment_id': 'synthetic-photo', 'filename': filename, 'mime': 'image/jpeg',
        'provider_platform': 'whatsapp', 'provider_message_id': filename[:-4], 'media_kind': 'photo', **changes}
    assert file_response_fields(row)['display_name'] == filename


def test_fifth_leased_media_attempt_is_still_an_active_transfer():
    result = attachment_fields({'attachment_id': 'synthetic-job', 'job_state': 'leased',
        'descriptor': {'filename': 'photo.jpg', 'mime': 'image/jpeg', 'bytes': 10},
        'error_code': 'network_error', 'attempts': 5})
    assert result['download_status'] == 'processing'
    assert result['next_action'] == 'get_attachment'
    assert result['error_detail']['retry_scheduled'] is False


@pytest.mark.parametrize('platform,kind,generated', [
    ('whatsapp', 'photo', True), ('whatsapp', 'document', False), ('telegram', 'photo', False),
])
def test_registered_filename_presentation_uses_current_occurrence_evidence(archive, platform, kind, generated):
    from messaging_service.blobs import BlobWriter
    ar, s, _, _ = archive
    account = 'synthetic-filename-'+platform
    ar.register(account, platform, 'Synthetic filename fixture', discover=True)
    key = s.issue_key('synthetic-filename-reader', [account], permissions=['read', 'download'])
    provider = 'false_1234567890@c.us_SYNTHETIC'
    ar.ingest(event(account=account, platform=platform, provider_id=provider, metadata={'media_kind': kind},
        media=[{'provider_id': 'synthetic-photo', 'filename': provider+'.bin', 'mime': 'image/jpeg', 'bytes': 4}]))
    job = ar.claim_media('synthetic-fixture', account=account)
    writer = BlobWriter(s.files, reserve_bytes=0)
    ar.finish_media(job, writer.write([b'xxxx']), writer=writer)
    for result in (s.get_attachment(key, job['id']), s.request_file_download(key, job['id']),
            s.get_file_text(key, job['id'])):
        assert result['source_filename'] == provider+'.bin'
        assert result['display_name_basis'] == ('generated_from_media_type' if generated else 'source_filename')
        assert result['download_filename'] == ('attachment-'+job['id']+'.jpg' if generated else provider+'.bin')


def test_version_conflict_recommends_an_available_read_operation():
    # get_sync_status with a chat_id lists the open conflicts; get_context needs an ID the summary lacks (ANYM-557).
    assert describe('version_conflict')['next_action'] == 'inspect_chat'


def test_compact_status_preserves_freshness_qualifications_and_independent_media_counts():
    from messaging_service.presentation import render
    full = {'sources': [{'freshness': {'state': 'capture_backlogged', 'scan_state': 'recent_scan',
        'scan_age_seconds': 2, 'pending_capture_events': 44,
        'qualifications': ['media_storage_paused']}, 'operations': {'media': {
            'state': 'paused_storage', 'storage_paused': True, 'storage_paused_jobs': 3,
            'terminal_failed_jobs': 2, 'retry_queued_jobs': 1, 'needs_attention': True,
            'diagnostics': [{'explanation': 'verbose detail'}]}},
        'files': {'download_error_states': {'network_error': {'failed': 2, 'queued': 1}}}}]}
    result = render('get_sync_status', full, 'compact')['sources'][0]
    assert result['freshness'] == full['sources'][0]['freshness']
    assert result['operations']['media'] == {
        k: v for k, v in full['sources'][0]['operations']['media'].items() if k != 'diagnostics'}
    assert result['files']['download_error_states'] == {'network_error': {'failed': 2, 'queued': 1}}
    assert 'diagnostics' in full['sources'][0]['operations']['media']
