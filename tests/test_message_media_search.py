"""ANYM-665: one messages search finds messages by their body or by the text inside their photos, PDFs and voice notes.

Synthetic rows on the disposable database; no provider, no OCR binary (the extracted text is set the way the OCR
tests do)."""
import json
from contextlib import contextmanager

import psycopg.errors
import pytest
from psycopg.types.json import Jsonb

from messaging_service.error_details import payload
from messaging_service.message_queries import filters, search as run_search
from messaging_service.presentation import render
from messaging_service.search_query import cursor_binding, decode_cursor
from messaging_service.store import APIError
from test_file_index import add_file, drain, worker_for
from test_media_text_excerpt import managed_absence
from test_ocr_quality import LIMIT, NOISE, ocr_row
from test_search_performance import Measured
from test_search_v2 import add
from test_voice_transcript_reading import voice

NEEDLE = 'quasarinvoice4417'


def hits(result):
    return [r['id'] for r in result['items']]


def find(s, token, query, **kwargs):
    return s.search(token, query, **kwargs)


def index_nodes(node):
    own = [node['Index Name']] if node.get('Index Name') else []
    return own + sum((index_nodes(child) for child in node.get('Plans', [])), [])


# spec: ANYM-665
def test_search_finds_the_ocr_text_of_a_photo(seeded):
    s, _, b = seeded
    ocr_row(s, 'photo-1', f'Receipt number {NEEDLE} total 42', {**LIMIT, 'ocr_words': 5, 'ocr_confidence': 91})
    result = find(s, b, NEEDLE)
    assert hits(result) == ['demo-tg-1']
    assert result['items'][0]['matched_in'] == ['extracted']
    # The same call before this card found nothing: the body does not hold the word.
    assert hits(find(s, b, NEEDLE, include_media_text=False)) == []


# spec: ANYM-665
def test_search_finds_a_voice_note_by_its_transcript(seeded):
    s, _, b = seeded
    voice(s)
    result = find(s, b, 'пятницу')
    assert hits(result) == ['demo-tg-1']
    row = result['items'][0]
    assert row['matched_in'] == ['transcript']
    assert row['media_matches'][0]['source'] == 'transcript' and row['media_matches'][0]['model_generated'] is True


# spec: ANYM-665
def test_media_text_search_follows_query_mode(seeded):
    s, _, b = seeded
    ocr_row(s, 'photo-1', 'alpha bravo charlie delta', {**LIMIT, 'ocr_words': 4, 'ocr_confidence': 90})
    expected = {('all', 'alpha charlie'): True, ('phrase', 'alpha charlie'): False, ('phrase', 'alpha bravo'): True,
                ('web', '"alpha bravo" -charlie'): False, ('web', '"alpha bravo" -zulu'): True,
                ('any', 'zulu delta'): True, ('any', 'zulu yankee'): False, ('all', 'alpha zulu'): False}
    for (mode, query), found in expected.items():
        assert bool(hits(find(s, b, query, query_mode=mode))) is found, (mode, query)


# spec: ANYM-665
def test_exact_mode_skips_the_media_arm_and_says_so(seeded):
    s, _, b = seeded
    ocr_row(s, 'photo-1', f'Receipt {NEEDLE}', {**LIMIT, 'ocr_words': 2, 'ocr_confidence': 90})
    result = find(s, b, NEEDLE, query_mode='exact')
    assert hits(result) == [] and result['search']['media_text_applied'] is False
    assert 'search_files' in result['search']['media_text_hint'] and 'exact' in result['search']['media_text_hint']
    # The compact answer keeps the statement.
    shown = render('search', find(s, b, NEEDLE, query_mode='exact', view='conversation'), 'compact')
    assert shown['search']['media_text_applied'] is False and 'search_files' in shown['search']['media_text_hint']
    # A body match in exact mode still works and carries no media arm.
    assert hits(find(s, b, 'Маяк: встреча', query_mode='exact')) == ['demo-tg-1']
    assert find(s, b, NEEDLE)['search']['media_text_applied'] is True


# spec: ANYM-665
def test_fuzzy_fallback_does_not_scan_media_text(seeded):
    s, _, b = seeded
    ocr_row(s, 'photo-1', 'Receipt quasarinvoice', {**LIMIT, 'ocr_words': 2, 'ocr_confidence': 90})
    # A typo matches nothing exactly, so fuzzy runs on the body only and never reaches the photo.
    result = find(s, b, 'quasarinvoise', fuzzy=True)
    assert result['search']['fuzzy_used'] is True and hits(result) == []
    # A media hit is a primary hit: fuzzy does not run at all.
    result = find(s, b, 'quasarinvoice', fuzzy=True)
    assert result['search']['fuzzy_used'] is False and hits(result) == ['demo-tg-1']


# spec: ANYM-665
def test_media_text_search_off_is_body_only(seeded):
    s, _, b = seeded
    ocr_row(s, 'photo-1', f'Receipt {NEEDLE}', {**LIMIT, 'ocr_words': 2, 'ocr_confidence': 90})
    result = find(s, b, NEEDLE, include_media_text=False)
    assert hits(result) == [] and result['search']['media_text_applied'] is False
    assert hits(find(s, b, 'Маяк', include_media_text=False)) == ['demo-tg-1']
    assert 'matched_in' in find(s, b, 'Маяк', include_media_text=False)['items'][0]
    for bad in ('false', 0, None):
        with pytest.raises(APIError):
            find(s, b, NEEDLE, include_media_text=bad)


# spec: ANYM-665
def test_search_ignores_image_descriptions(seeded):
    s, _, b = seeded
    # ANYM-987: the title and summary of an image never match; its visible text does (tests/test_image_visible_text.py).
    voice(s, 'photo-described', filename='photo.jpg', mime='image/jpeg', transcript='')
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_enrichment SET summary='Model description of a zeppelin' WHERE attachment='photo-described'")
    assert hits(find(s, b, 'zeppelin')) == []


# spec: ANYM-665
def test_hit_rows_say_where_they_matched(seeded):
    s, _, b = seeded
    ocr_row(s, 'photo-1', f'Маяк на фото {NEEDLE} ' + 'слово ' * 80, {**LIMIT, 'ocr_words': 90, 'ocr_confidence': 88})
    both = find(s, b, 'Маяк')['items'][0]
    assert both['id'] == 'demo-tg-1' and both['matched_in'] == ['body', 'extracted']
    media = find(s, b, NEEDLE)['items'][0]
    assert media['matched_in'] == ['extracted']
    entry = media['media_matches'][0]
    assert set(entry) == {'attachment_id', 'source', 'snippet', 'model_generated', 'untrusted', 'read_more', 'quality'}
    assert entry['attachment_id'] == 'photo-1' and entry['source'] == 'extracted' and entry['model_generated'] is False
    assert entry['untrusted'] is True and entry['quality'] == 'ok' and 0 < len(entry['snippet']) <= 200
    assert '[[' in entry['snippet'] and ']]' in entry['snippet']
    assert entry['read_more'] == {'operation': 'get_file_text', 'attachment_id': 'photo-1', 'offset': 0,
                                  'content': 'extracted'}
    body_only = find(s, b, 'вторник')['items'][0]
    assert body_only['matched_in'] == ['body'] and 'media_matches' not in body_only
    listing = find(s, b, '')['items'][0]
    assert 'matched_in' not in listing and 'media_matches' not in listing
    # The compact conversation answer keeps both fields.
    shown = render('search', find(s, b, NEEDLE, view='conversation'), 'compact')['items'][0]
    assert shown['matched_in'] == ['extracted'] and shown['media_matches'][0]['attachment_id'] == 'photo-1'


# spec: ANYM-665
def test_album_lists_every_matching_attachment(seeded):
    s, _, b = seeded
    for name, text in [('album-1', f'first {NEEDLE}'), ('album-2', 'second page without it'), ('album-3', f'third {NEEDLE}')]:
        ocr_row(s, name, text, {**LIMIT, 'ocr_words': 3, 'ocr_confidence': 90})
    voice(s, 'album-voice', transcript=f'spoken {NEEDLE}')
    row = find(s, b, NEEDLE)['items'][0]
    assert row['matched_in'] == ['extracted', 'transcript']
    assert [(m['attachment_id'], m['source']) for m in row['media_matches']] == [
        ('album-1', 'extracted'), ('album-3', 'extracted'), ('album-voice', 'transcript')]


# spec: ANYM-665
def test_contact_card_hit_is_matched_in_contact(seeded):
    s, _, b = seeded
    add(s, 'card-1', 'Synthetic Friend')
    with s.pool.connection() as c:
        c.execute("INSERT INTO message_metadata(message,revision,metadata) VALUES('card-1',1,%s)",
                  (Jsonb({'message_kind': 'contact', 'contact': {'status': 'parsed',
                          'contacts': [{'name': 'Zanzibarname Friend', 'phones': []}]}}),))
    row = find(s, b, 'Zanzibarname')['items'][0]
    assert row['id'] == 'card-1' and row['matched_in'] == ['contact'] and 'media_matches' not in row


# spec: ANYM-665
def test_unreadable_original_hides_the_media_snippet(seeded):
    s, _, b = seeded
    lost = add_file(s, 'lost-photo', 'demo-tg-1', f'Lost {NEEDLE}'.encode(), 'lost.txt')
    kept = add_file(s, 'evicted-photo', 'demo-tg-1', f'Evicted {NEEDLE}'.encode(), 'evicted.txt')
    drain(worker_for(s))
    (s.files / lost).unlink()
    managed_absence(s, kept, len(f'Evicted {NEEDLE}'.encode()))
    row = find(s, b, NEEDLE)['items'][0]
    matches = {m['attachment_id']: m for m in row['media_matches']}
    assert matches['lost-photo'] == {'attachment_id': 'lost-photo', 'source': 'extracted', 'unreadable': True}
    assert 'snippet' in matches['evicted-photo'] and 'unreadable' not in matches['evicted-photo']


# spec: ANYM-665
def test_media_text_of_a_deleted_message_never_matches(seeded):
    s, _, b = seeded
    ocr_row(s, 'photo-1', f'Receipt {NEEDLE}', {**LIMIT, 'ocr_words': 2, 'ocr_confidence': 90})
    ocr_row(s, 'photo-2', f'Second {NEEDLE}', {**LIMIT, 'ocr_words': 2, 'ocr_confidence': 90}, 'demo-tg-2')
    ocr_row(s, 'photo-3', f'Third {NEEDLE}', {**LIMIT, 'ocr_words': 2, 'ocr_confidence': 90}, 'demo-tg-2')
    assert sorted(hits(find(s, b, NEEDLE))) == ['demo-tg-1', 'demo-tg-2']
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET deleted=true WHERE id='demo-tg-1'")
        c.execute("UPDATE attachments SET active=false WHERE id='photo-2'")
        c.execute("UPDATE attachment_text SET state='superseded' WHERE attachment='photo-3'")
    assert hits(find(s, b, NEEDLE)) == []


# spec: ANYM-665
def test_media_text_search_stays_inside_the_callers_scope(seeded):
    s, a, b = seeded
    ocr_row(s, 'photo-wa', f'Receipt {NEEDLE}', {**LIMIT, 'ocr_words': 2, 'ocr_confidence': 90}, 'demo-wa-1')
    # Key a reads whatsapp-demo-1 and telegram-demo; key b reads telegram-demo only.
    assert hits(find(s, a, NEEDLE)) == ['demo-wa-1']
    assert hits(find(s, b, NEEDLE)) == []


# spec: ANYM-665
def test_old_cursor_still_pages_after_the_new_parameter(seeded):
    s, _, b = seeded
    ocr_row(s, 'photo-1', f'one {NEEDLE}', {**LIMIT, 'ocr_words': 2, 'ocr_confidence': 90})
    ocr_row(s, 'photo-2', f'two {NEEDLE}', {**LIMIT, 'ocr_words': 2, 'ocr_confidence': 90}, 'demo-tg-2')
    # A time-ordered cursor is a plain message ID, as before.
    page = find(s, b, NEEDLE, limit=1)
    assert hits(page) == ['demo-tg-1'] and page['next_cursor'] == 'demo-tg-1'
    assert hits(find(s, b, NEEDLE, limit=1, after=page['next_cursor'])) == ['demo-tg-2']
    # A relevance cursor keeps the binding it had before the parameter existed (default value adds nothing).
    ranked = find(s, b, NEEDLE, limit=1, order='relevance')
    with s.authorized(b, 'search') as (c, p):
        _, _, normalized = filters(p, include_channels=False)
    old = cursor_binding(p, 'messages', {**normalized, 'query': NEEDLE, 'query_mode': 'all', 'language': 'auto',
                                         'order': 'relevance', 'fuzzy': False})
    assert decode_cursor(ranked['next_cursor'], old)['id'] in ('demo-tg-1', 'demo-tg-2')


# spec: ANYM-665
def test_cursor_refuses_a_changed_media_filter(seeded):
    s, _, b = seeded
    add(s, 'body-1', f'{NEEDLE} in a body', day=5)
    add(s, 'body-2', f'{NEEDLE} in another body', day=6)
    off = find(s, b, NEEDLE, limit=1, include_media_text=False)
    assert off['next_cursor'].startswith('q1.')
    assert hits(find(s, b, NEEDLE, limit=1, include_media_text=False, after=off['next_cursor'])) == ['body-2']
    with pytest.raises(APIError):
        find(s, b, NEEDLE, limit=1, after=off['next_cursor'])
    on = find(s, b, NEEDLE, limit=1, order='relevance')
    with pytest.raises(APIError):
        find(s, b, NEEDLE, limit=1, order='relevance', include_media_text=False, after=on['next_cursor'])
    # A bare ID is refused when the media arm is off (it would not bind the value).
    with pytest.raises(APIError):
        find(s, b, NEEDLE, limit=1, include_media_text=False, after='body-1')


# spec: ANYM-665
def test_relevance_order_puts_media_only_hits_after_body_hits(seeded):
    s, _, b = seeded
    add(s, 'body-late', f'{NEEDLE} in the body', day=20)
    add(s, 'media-early', 'plain words', day=6)
    add(s, 'media-late', 'other plain words', day=7)
    for message in ('media-early', 'media-late'):
        ocr_row(s, 'photo-' + message, f'Receipt {NEEDLE}', {**LIMIT, 'ocr_words': 2, 'ocr_confidence': 90}, message)
    assert hits(find(s, b, NEEDLE, order='relevance')) == ['body-late', 'media-late', 'media-early']
    assert hits(find(s, b, NEEDLE, order='asc')) == ['media-early', 'media-late', 'body-late']
    page = find(s, b, NEEDLE, order='relevance', limit=2)
    assert hits(find(s, b, NEEDLE, order='relevance', limit=2, after=page['next_cursor'])) == ['media-early']


# spec: ANYM-665
def test_low_quality_media_hits_rank_last_and_are_counted(seeded):
    s, _, b = seeded
    add(s, 'media-ok', 'plain words', day=6)
    add(s, 'media-low', 'other plain words', day=7)
    ocr_row(s, 'photo-ok', f'Receipt {NEEDLE}', {**LIMIT, 'ocr_words': 2, 'ocr_confidence': 92}, 'media-ok')
    ocr_row(s, 'photo-low', f'{NOISE} {NEEDLE}', {**LIMIT, 'ocr_words': 12, 'ocr_confidence': 31}, 'media-low')
    result = find(s, b, NEEDLE, order='relevance')
    assert hits(result) == ['media-ok', 'media-low']
    low = result['items'][1]
    assert low['matched_in'] == ['extracted']
    assert low['media_matches'] == [{'attachment_id': 'photo-low', 'source': 'extracted', 'quality': 'low',
        'read_more': {'operation': 'get_file_text', 'attachment_id': 'photo-low', 'offset': 0, 'content': 'extracted'}}]
    assert result['search']['media_low_quality_hits'] == 1
    assert render('search', find(s, b, NEEDLE, view='conversation'), 'compact')['search']['media_low_quality_hits'] == 1
    assert 'snippet' not in json.dumps(low['media_matches'])


@contextmanager
def bulk(s, needle=None):
    """3,000 messages with a common body, each with a text file and a voice note; one in twenty of them says the
    common sentence, the rest are filler (a common word is not in every attachment)."""
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='120s'")
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            SELECT 'bg-'||n,'telegram-demo','telegram-demo-chat','bg-'||n,'fixture','ordinary common content',
                now()+n*interval '1 second' FROM generate_series(1,3000) n''')
        c.execute('''INSERT INTO attachments(id,message,digest,filename,mime,bytes)
            SELECT 'bg-file-'||n,'bg-'||n,repeat('a',64),'bg-'||n||'.txt','text/plain',10 FROM generate_series(1,3000) n''')
        c.execute('''INSERT INTO attachments(id,message,digest,filename,mime,bytes)
            SELECT 'bg-voice-'||n,'bg-'||n,repeat('b',64),'bg-'||n||'.ogg','audio/ogg',10 FROM generate_series(1,3000) n''')
        c.execute("UPDATE attachment_text SET state='ready',text_content=CASE WHEN substr(attachment,9)::int%20=0 "
                  "THEN 'ordinary common content' ELSE 'unrelated filler sentence' END WHERE attachment LIKE 'bg-file-%'")
        c.execute("UPDATE attachment_enrichment SET state='ready',kind='audio',text_content=CASE WHEN substr(attachment,10)::int%20=0 "
                  "THEN 'ordinary common content' ELSE 'unrelated filler sentence' END WHERE attachment LIKE 'bg-voice-%'")
        if needle:
            c.execute("UPDATE attachment_text SET text_content=%s WHERE attachment='bg-file-7'", ('ordinary ' + needle,))
            c.execute("UPDATE attachment_enrichment SET text_content=%s WHERE attachment='bg-voice-9'", ('ordinary ' + needle,))
        for table in ('messages', 'attachments', 'attachment_text', 'attachment_enrichment'):
            c.execute('ANALYZE ' + table)
        for index in ('attachment_text_document_bounded', 'attachment_text_document_russian_bounded',
                      'attachment_text_document_english_bounded', 'attachment_text_document_indonesian_bounded',
                      'attachment_enrichment_document_bounded', 'attachment_enrichment_document_russian_bounded',
                      'attachment_enrichment_document_english_bounded', 'attachment_enrichment_document_indonesian_bounded'):
            c.execute('SELECT gin_clean_pending_list(%s::regclass)', (index,))
    try:
        yield
    finally:
        # The statistics outlive TRUNCATE: later tests must not plan against 6,000 rows that are gone.
        with s.pool.connection() as c:
            c.execute("DELETE FROM attachments WHERE id LIKE 'bg-%'")
            c.execute("DELETE FROM messages WHERE id LIKE 'bg-%'")
            for table in ('messages', 'attachments', 'attachment_text', 'attachment_enrichment'):
                c.execute('ANALYZE ' + table)


# spec: ANYM-665
def test_media_arm_uses_the_gin_indexes(seeded):
    s, _, b = seeded
    with bulk(s, 'scarcemedianeedle'), s.authorized(b, 'search') as (c, p):
        c.execute('SET LOCAL enable_seqscan=off')
        measured = Measured(c)
        result = run_search(s, measured, p, query='scarcemedianeedle')
    assert sorted(hits(result)) == ['bg-7', 'bg-9']
    names = [name for plan in measured.plans for name in index_nodes(plan)]
    assert any(n.startswith('attachment_text_document') for n in names), names
    assert any(n.startswith('attachment_enrichment_document') for n in names), names


# spec: ANYM-665
def test_common_word_with_media_text_stays_inside_the_limit(seeded):
    s, _, b = seeded
    with bulk(s):
        for include in (False, True):
            with s.authorized(b, 'search') as (c, p):
                c.execute("SET LOCAL statement_timeout='5s'")
                result = run_search(s, c, p, query='ordinary', limit=20, include_media_text=include, order='relevance')
            assert len(result['items']) == 20, include
        # The media arm reads both GIN indexes in every mode that has a text index, inside the pool's 5-second limit.
        for mode, query in [('all', 'ordinary content'), ('phrase', 'ordinary common'), ('web', 'ordinary -zulu'),
                            ('any', 'ordinary zulu')]:
            with s.authorized(b, 'search') as (c, p):
                c.execute("SET LOCAL statement_timeout='5s'")
                c.execute('SET LOCAL enable_seqscan=off')
                measured = Measured(c)
                try:
                    result = run_search(s, measured, p, query=query, query_mode=mode, limit=20, order='relevance')
                except psycopg.errors.QueryCanceled:
                    pytest.fail('timeout in mode ' + mode)
            names = [name for plan in measured.plans for name in index_nodes(plan)]
            assert len(result['items']) == 20, mode
            assert any(n.startswith('attachment_text_document') for n in names), (mode, names)
            assert any(n.startswith('attachment_enrichment_document') for n in names), (mode, names)


# spec: ANYM-665
@pytest.mark.parametrize('where', ['page statement', 'evidence statement'])
def test_media_search_timeout_names_include_media_text_false(seeded, monkeypatch, where):
    import messaging_service.message_media_search as module
    s, _, b = seeded
    ocr_row(s, 'photo-1', f'Receipt {NEEDLE}', {**LIMIT, 'ocr_words': 2, 'ocr_confidence': 90})
    cancel = '(CASE WHEN pg_cancel_backend(pg_backend_pid()) THEN %s END)'
    # The database cancels its own statement: the same QueryCanceled the pool's 5-second limit raises.
    if where == 'page statement':
        monkeypatch.setattr(module, 'LOW', cancel % 'true')
    else:
        monkeypatch.setattr(module, '_headline', lambda plan, text: cancel % "'x'")
    with pytest.raises(APIError) as error:
        s.search(b, NEEDLE)
    detail = payload(error.value, 'search')
    assert detail['error'] == 'query_timeout' and 'include_media_text=false' in detail['next_action']
    # A body-only search keeps the generic advice.
    assert 'include_media_text' not in payload(psycopg.errors.QueryCanceled('x'), 'search')['next_action']


def pending_of(result):
    return result['result_evidence'].get('media_text_pending'), result['result_evidence'].get('media_text_unavailable')


# spec: ANYM-665
def test_empty_search_reports_pending_media_text_inside_the_filters(seeded):
    s, _, b = seeded
    drain(worker_for(s))  # the seeded probe file becomes ready; only the file added below waits
    # Queued extraction: the original is stored, the text has not arrived.
    add_file(s, 'queued-note', 'demo-tg-1', b'Not yet extracted text', 'queued.txt')
    result = find(s, b, NEEDLE)
    pending, unavailable = pending_of(result)
    assert hits(result) == [] and pending['count'] == 1 and pending['newest_sent_at'].startswith('2026-10-01')
    assert 'search_files' in pending['next_action'] or 'later' in pending['next_action']
    assert unavailable is None
    # Every filter of the search applies, never only the chats.
    for narrow in ({'chats': ['telegram-demo-chat'], 'direction': 'outgoing'}, {'senders': ['someone-else']},
                   {'accounts': ['whatsapp-demo-1']}):
        assert pending_of(find(s, b if 'accounts' not in narrow else s.issue_key('agent-c', ['whatsapp-demo-1']),
                               NEEDLE, **narrow))[0] is None, narrow
    # A hit makes the result non-empty: no flag.
    assert 'media_text_pending' not in find(s, b, 'Маяк')['result_evidence']
    # Queued voice transcription is pending too once an audio profile is enabled.
    assert find(s, b, NEEDLE, include_media_text=False)['result_evidence'].get('media_text_pending') is None


# spec: ANYM-665
def test_pending_flag_ignores_media_outside_the_date_range(seeded):
    s, _, b = seeded
    drain(worker_for(s))
    add_file(s, 'queued-note', 'demo-tg-2', b'Not yet extracted text', 'queued.txt')
    assert pending_of(find(s, b, NEEDLE, from_time='2026-10-02T00:00:00Z', to_time='2026-10-03T00:00:00Z'))[0]['count'] == 1
    assert pending_of(find(s, b, NEEDLE, from_time='2026-10-03T00:00:00Z'))[0] is None
    assert pending_of(find(s, b, NEEDLE, to_time='2026-10-02T00:00:00Z'))[0] is None


# spec: ANYM-665
def test_unconfigured_audio_is_reported_as_unavailable_not_pending(seeded):
    s, _, b = seeded
    voice(s, 'voice-pending', state='unconfigured')
    pending, unavailable = pending_of(find(s, b, NEEDLE))
    assert pending is None and unavailable['count'] == 1
    group = unavailable['reasons'][0]
    assert group['reason'] == 'unconfigured' and group['count'] == 1 and group['accounts'] == ['telegram-demo']
    assert 'owner' in group['note']
    # A queued download outside the collection policy is unavailable too, not pending.
    with s.pool.connection() as c:
        c.execute('''INSERT INTO media_jobs(id,account,message,descriptor,state)
            VALUES('job-excluded','telegram-demo','demo-tg-2','{}'::jsonb,'queued')''')
    reasons = {g['reason'] for g in pending_of(find(s, b, NEEDLE))[1]['reasons']}
    assert reasons == {'unconfigured', 'collection_excluded'}
    # A failed extraction is a reason of its own.
    add_file(s, 'broken', 'demo-tg-1', b'x', 'broken.txt')
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_text SET state='failed' WHERE attachment='broken'")
    assert 'failed' in {g['reason'] for g in pending_of(find(s, b, NEEDLE))[1]['reasons']}


# spec: ANYM-665
def test_no_flag_when_media_text_is_ready(seeded):
    s, _, b = seeded
    ocr_row(s, 'photo-1', 'Receipt without the word', {**LIMIT, 'ocr_words': 3, 'ocr_confidence': 90})
    voice(s)
    result = find(s, b, NEEDLE)
    assert hits(result) == [] and pending_of(result) == (None, None)


# spec: ANYM-665
def test_evidence_binds_to_the_displayed_revision(seeded):
    from messaging_service.message_media_search import annotate
    from messaging_service.search_query import compile_query
    s, _, b = seeded
    ocr_row(s, 'photo-1', f'Receipt {NEEDLE}', {**LIMIT, 'ocr_words': 2, 'ocr_confidence': 90})
    plan = compile_query(NEEDLE)
    flags = {'demo-tg-1': (False, False, True)}
    with s.authorized(b, 'search') as (c, p):
        current = annotate(c, plan, [{'id': 'demo-tg-1', 'revision': 1}], flags, fuzzy_used=False)
        assert current == 0
        # A second revision and its extraction became ready after revision 1 was chosen for the page.
        c.execute("UPDATE messages SET revision=2 WHERE id='demo-tg-1'")
        c.execute("UPDATE attachment_text SET source_revision=2,state='ready' WHERE attachment='photo-1'")
        stale = {'id': 'demo-tg-1', 'revision': 1}
        annotate(c, plan, [stale], flags, fuzzy_used=False)
        assert stale['matched_in'] == [] and 'media_matches' not in stale
        fresh = {'id': 'demo-tg-1', 'revision': 2}
        annotate(c, plan, [fresh], flags, fuzzy_used=False)
        assert fresh['matched_in'] == ['extracted'] and fresh['media_matches'][0]['attachment_id'] == 'photo-1'


# spec: ANYM-665
def test_web_exclusions_apply_to_the_text_not_the_filename(seeded):
    s, _, b = seeded
    # The filename holds the excluded word; the text does not.
    ocr_row(s, 'draft-photo', 'invoice approved', {**LIMIT, 'ocr_words': 2, 'ocr_confidence': 90})
    voice(s, 'draft-voice', transcript='invoice approved today', filename='draft-voice.ogg')
    row = find(s, b, 'invoice -draft', query_mode='web', search_language='simple')['items'][0]
    assert row['matched_in'] == ['extracted', 'transcript']
    # The enrichment title ("Synthetic title") is not the transcript either.
    assert hits(find(s, b, 'invoice -synthetic', query_mode='web', search_language='simple')) == ['demo-tg-1']
    # An exclusion that is in the text still excludes.
    assert hits(find(s, b, 'invoice -approved', query_mode='web', search_language='simple')) == []
    assert hits(find(s, b, '"invoice approved" -"approved today"', query_mode='web', search_language='simple')) == ['demo-tg-1']


# spec: ANYM-665
def test_media_rank_ignores_filenames_and_density(seeded):
    s, _, b = seeded
    add(s, 'body-once', f'{NEEDLE} once in the body', day=6)
    add(s, 'media-dense', 'plain words', day=7)
    ocr_row(s, 'photo-dense', f'{NEEDLE} {NEEDLE} {NEEDLE} {NEEDLE} {NEEDLE}', {**LIMIT, 'ocr_words': 5, 'ocr_confidence': 90}, 'media-dense')
    # A repeated literal in the text is still the same tier as one in a body: the body comes first.
    assert hits(find(s, b, NEEDLE, order='relevance')) == ['body-once', 'media-dense']
    # any mode counts the terms of the text only: the second term sits in the filename.
    ocr_row(s, 'beta-file', 'alpha only here', {**LIMIT, 'ocr_words': 3, 'ocr_confidence': 90}, 'demo-tg-2')
    row = find(s, b, 'alpha beta', query_mode='any', order='relevance')['items'][0]
    assert row['id'] == 'demo-tg-2' and row['score'] == 500000 - 1


# spec: ANYM-665
def test_one_kind_of_media_evidence_never_hides_the_other(seeded):
    s, _, b = seeded
    drain(worker_for(s))
    add_file(s, 'old-queued', 'demo-tg-1', b'Not yet extracted text', 'old.txt')
    with s.pool.connection() as c:
        for n in range(25):
            add(s, f'ex-{n}', 'plain', day=10 + n % 15)
            c.execute("INSERT INTO media_jobs(id,account,message,descriptor,state) VALUES(%s,'telegram-demo',%s,'{}'::jsonb,'queued')",
                      (f'job-ex-{n}', f'ex-{n}'))
    pending, unavailable = pending_of(find(s, b, NEEDLE))
    assert pending['count'] == 1 and pending['newest_sent_at'].startswith('2026-10-01')
    assert unavailable['reasons'] == [{'reason': 'collection_excluded', 'count': 20, 'capped': True}]


# spec: ANYM-665
def test_snippet_clip_keeps_marker_pairs_closed():
    from messaging_service.message_media_search import clip
    for snippet in ('[[alpha]] ' + 'x' * 185 + ' [[bravo]] suffix', 'y' * 90 + ' [[alpha beta gamma]] ' + 'z' * 300,
                    '[[a]] ' + 'x' * 190 + ' [[b' + 'x' * 30 + ']]'):
        clipped = clip(snippet)
        assert len(clipped) <= 200 and clipped.count('[[') == clipped.count(']]') and '[[' in clipped, clipped


# spec: ANYM-665
def test_web_prefilter_keeps_required_words_and_quoted_phrases():
    from messaging_service.message_media_search import positive
    assert positive('invoice receipt') == 'invoice receipt' and positive('"foo -bar" baz') == '"foo -bar" baz'
    assert positive('invoice -draft') == 'invoice' and positive('"a b" -c d -"e f"') == '"a b" d'
    assert positive('a or b') == 'a or b'
    for unsound in ('-draft', 'a or -b', '-a -"b c"', 'x"y z" -w'):
        assert positive(unsound) is None, unsound


# spec: ANYM-665
def test_web_queries_without_a_sound_prefilter_skip_the_media_arm_and_say_so(seeded):
    s, _, b = seeded
    add(s, 'draft-body', 'draft only here', day=6)
    ocr_row(s, 'photo-1', 'approved receipt', {**LIMIT, 'ocr_words': 2, 'ocr_confidence': 90}, 'demo-tg-2')
    for query in ('receipt OR -draft', '-draft'):
        result = find(s, b, query, query_mode='web')
        assert result['search']['media_text_applied'] is False and 'search_files' in result['search']['media_text_hint']
        assert all('media_matches' not in row for row in result['items']), query
    # A required word makes the exclusion safe: the text is judged, the filename and the body are not.
    assert hits(find(s, b, 'receipt -draft', query_mode='web')) == ['demo-tg-2']


# spec: ANYM-665
def test_quoted_web_phrases_match_media_text_as_they_match_bodies(seeded):
    s, _, b = seeded
    add(s, 'body-twin', 'foo bar here', day=6)
    ocr_row(s, 'photo-1', 'foo bar here', {**LIMIT, 'ocr_words': 3, 'ocr_confidence': 90}, 'demo-tg-2')
    for query in ('"foo -bar"', '"foo bar" -zulu', 'foo "bar here"'):
        by_body = bool(hits(find(s, b, query, query_mode='web', include_media_text=False)))
        by_media = 'demo-tg-2' in hits(find(s, b, query, query_mode='web'))
        assert by_media is by_body, query


# spec: ANYM-665
def test_unavailable_count_caps_as_a_whole(seeded):
    s, _, b = seeded
    drain(worker_for(s))
    with s.pool.connection() as c:
        for n in range(25):
            add(s, f'ex-{n}', 'plain', day=10 + n % 15)
            state = 'queued' if n < 12 else 'failed'
            c.execute("INSERT INTO media_jobs(id,account,message,descriptor,state) VALUES(%s,'telegram-demo',%s,'{}'::jsonb,%s)",
                      (f'job-ex-{n}', f'ex-{n}', state))
        for n in range(25, 50):
            add(s, f'ex-{n}', 'plain', day=10 + n % 15)
            c.execute("INSERT INTO media_jobs(id,account,message,descriptor,state) VALUES(%s,'telegram-demo',%s,'{}'::jsonb,'queued')",
                      (f'job-ex-{n}', f'ex-{n}'))
    unavailable = pending_of(find(s, b, NEEDLE))[1]
    assert {r['reason']: r['count'] for r in unavailable['reasons']} == {'collection_excluded': 20, 'failed': 13}
    assert unavailable['count'] == 20 and unavailable['capped'] is True
