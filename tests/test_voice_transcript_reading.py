"""Voice notes are read through their transcript by default (synthetic rows, no provider)."""
import pytest

from messaging_service.presentation import render
from messaging_service.store import APIError
from test_file_index import add_file, drain, search, text, worker_for

TRANSCRIPT = 'Синтетическая расшифровка голосового сообщения про встречу в пятницу'


def voice(s, attachment='voice-note', message='demo-tg-1', *, state='ready', transcript=TRANSCRIPT,
          error_code=None, filename='voice-memo.ogg', mime='audio/ogg'):
    add_file(s, attachment, message, ('synthetic-audio-' + attachment).encode(), filename, mime)
    drain(worker_for(s))
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_enrichment SET state=%s,kind=%s,title='Synthetic title',summary='Synthetic summary',"
                  "provider='synthetic',model='synthetic-model',text_content=%s,error_code=%s WHERE attachment=%s",
                  (state, 'audio' if state == 'ready' else None, transcript if state == 'ready' else '', error_code, attachment))


def profile(s, *, enabled=True, current=True, paused=False):
    with s.pool.connection() as c:
        c.execute("INSERT INTO processing_profiles(account,kind,provider,model,enabled,expires_at,pause_reason) "
                  "VALUES('telegram-demo','audio','synthetic','synthetic-model',%s,"
                  "clock_timestamp()+CASE WHEN %s THEN interval '1 hour' ELSE interval '-1 hour' END,%s)",
                  (enabled, current, 'budget_exhausted' if paused else None))


# spec: ANYM-453
def test_get_file_text_defaults_to_the_ready_transcript(seeded):
    s, _, b = seeded
    voice(s)
    add_file(s, 'plain-note', 'demo-tg-1', b'Plain synthetic text body', 'note.txt')
    drain(worker_for(s))
    page = text(s, b, 'voice-note')
    assert page['text'] == TRANSCRIPT and page['content'] == 'enriched'
    assert page['provenance']['content'] == 'enriched' and page['provenance']['model']
    assert page['notice'].startswith('Model-generated')
    assert page['content_coverage']['retained_chars'] == len(TRANSCRIPT) and page['next_offset'] is None
    assert text(s, b, 'voice-note', limit=10)['next_offset'] == 10
    assert text(s, b, 'voice-note', offset=10)['text'] == TRANSCRIPT[10:]
    # Explicit choices and non-audio files are unchanged.
    explicit = text(s, b, 'voice-note', content='extracted')
    assert explicit['text'] == '' and explicit['content'] == 'extracted'
    assert text(s, b, 'voice-note', content='enriched')['text'] == TRANSCRIPT
    plain = text(s, b, 'plain-note')
    assert plain['text'] == 'Plain synthetic text body' and plain['content'] == 'extracted'
    assert 'empty_reason' not in plain
    with pytest.raises(APIError, match='invalid_filter'):
        text(s, b, 'plain-note', offset=1_000_001)
    # The REST and MCP surfaces omit content the same way.
    assert s.get_file_text(b, 'voice-note')['text'] == TRANSCRIPT


# spec: ANYM-453
@pytest.mark.parametrize('case,reason', [
    ('pending', 'processing_pending'),
    ('paused', 'processing_paused'),
    ('worker_unavailable', 'processing_worker_unavailable'),
    ('failed', 'processing_failed'),
    ('not_configured', 'processing_not_configured'),
])
def test_unready_transcript_names_the_reason(seeded, case, reason):
    s, _, b = seeded
    voice(s, state='failed' if case == 'failed' else 'unconfigured',
          error_code='provider_rejected' if case == 'failed' else None)
    if case != 'not_configured':
        profile(s, current=case != 'worker_unavailable', paused=case == 'paused')
    with s.pool.connection() as c:
        assert c.execute("SELECT kind FROM attachment_enrichment WHERE attachment='voice-note'").fetchone()['kind'] is None
    page = text(s, b, 'voice-note')
    assert page['text'] == '' and page['content'] == 'enriched'
    assert page['empty_reason'] == reason
    assert page['enrichment_status'] == {'failed': 'failed', 'not_configured': 'not_configured'}.get(case, 'queued')
    if case == 'failed':
        assert page['failure_reason'] == 'provider_rejected'
    if case == 'not_configured':
        assert page['empty_reason_detail'] == 'only the owner can enable transcription for this account'
    # An explicit extracted read keeps its old reply.
    assert 'empty_reason' not in text(s, b, 'voice-note', content='extracted')


# spec: ANYM-453
def test_search_excerpt_of_a_voice_note_is_its_transcript(seeded):
    s, _, b = seeded
    voice(s)
    add_file(s, 'plain-note', 'demo-tg-1', b'Plain synthetic text body', 'voice-memo-notes.txt')
    drain(worker_for(s))
    for kwargs in [{}, {'query': 'voice-memo.ogg'}, {'query': 'voice-memo', 'query_mode': 'exact'}]:
        result = search(s, b, **kwargs)
        hit = next(r for r in result['items'] if r['attachment_id'] == 'voice-note')
        assert hit['excerpt'].startswith(TRANSCRIPT[:40]), kwargs
        assert hit['excerpt_source'] == 'transcript'
        if kwargs:
            assert hit['matched_content'] == 'extracted'
        plain = next((r for r in result['items'] if r['attachment_id'] == 'plain-note'), None)
        assert plain is None or 'excerpt_source' not in plain
        compact = render('search_files', {**result, 'items': [dict(r) for r in result['items']]}, 'compact')
        row = next(r for r in compact['items'] if r['attachment_id'] == 'voice-note')
        assert row['snippet'] == TRANSCRIPT[:160] and row['excerpt_source'] == 'transcript'


# spec: ANYM-453
def test_message_row_says_transcript_ready(seeded):
    s, _, b = seeded
    voice(s)
    voice(s, 'voice-pending', 'demo-tg-1', state='unconfigured')
    def attachments(rows):
        return {f['id']: f for row in rows for f in row.get('attachments') or []}
    full = s.get_messages(b, 'telegram-demo-chat', view='archive')
    for detail, view in [('full', 'archive'), ('compact', 'archive'), ('compact', 'conversation')]:
        rows = render('get_messages', s.get_messages(b, 'telegram-demo-chat', view=view), detail)['items']
        files = attachments(rows)
        assert files['voice-note']['transcript_ready'] is True, (detail, view)
        assert 'transcript_ready' not in files['voice-pending'], (detail, view)
    assert attachments(full['items'])['voice-note']['transcript_ready'] is True


# spec: ANYM-453
def test_voice_excerpt_with_empty_ready_extraction_and_dual_matches(seeded):
    s, _, b = seeded
    voice(s)
    voice(s, 'voice-dual', filename='meeting.ogg', transcript='Synthetic meeting notes')
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_text SET state='ready',error_code=NULL WHERE attachment='voice-note'")
    hit = search(s, b, 'voice-memo.ogg')['items'][0]
    assert hit['excerpt'].startswith(TRANSCRIPT[:40]) and hit['excerpt_source'] == 'transcript'
    # The transcript also matches the name: the existing snippet is kept.
    dual = search(s, b, 'meeting', query_mode='exact')['items'][0]
    assert dual['attachment_id'] == 'voice-dual' and 'excerpt_source' not in dual


# spec: ANYM-453
def test_failed_transcript_keeps_its_reason_under_a_paused_profile(seeded):
    s, _, b = seeded
    voice(s, state='failed', error_code='provider_rejected')
    profile(s, paused=True)
    page = text(s, b, 'voice-note')
    assert page['empty_reason'] == 'processing_failed' and page['failure_reason'] == 'provider_rejected'
