"""ANYM-987: the text an agent reads and searches inside a chat image is the vision model's visible text when it is
ready, and the OCR text only when it is not (synthetic rows on the disposable database; no Tesseract, no mini)."""
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from messaging_service.file_extract import IMAGE_OCR_EXTRACTOR, MEASURED_PROCESSOR_VERSION
from messaging_service.presentation import render
from test_file_index import add_file, search, text
from test_media_display_text import add as add_message, rows as message_rows
from test_media_text_excerpt import read
from test_ocr_quality import LIMIT, ocr_row
from test_voice_transcript_reading import voice

NEEDLE = 'quasarinvoice4417'
OCR = 'Rcceipt nvmber 42 t0tal zxqv kjhw'
VISION = 'Receipt number 42 total 130 EUR'
OCR_OK = {**LIMIT, 'ocr_words': 5, 'ocr_confidence': 92}
OCR_LOW = {**LIMIT, 'ocr_words': 12, 'ocr_confidence': 31}
ROOT = Path(__file__).resolve().parent.parent


def image(s, name, *, ocr=None, vision=None, vision_state='ready', meta=OCR_OK, message='demo-tg-1', truncated=False):
    """An image attachment whose OCR text and vision text are set directly the way the workers' results are stored."""
    add_file(s, name, message, ('synthetic-image-' + name).encode(), name + '.png', 'image/png')
    with s.pool.connection() as c:
        if ocr is not None:
            c.execute("UPDATE attachment_text SET state='ready',text_content=%s,extractor=%s,extraction_metadata=%s,"
                      "processor_version=%s,display_name=%s,indexed_at=clock_timestamp() WHERE attachment=%s",
                      (ocr, IMAGE_OCR_EXTRACTOR, Jsonb(meta), MEASURED_PROCESSOR_VERSION, name + '.png', name))
        if vision is not None:
            c.execute("UPDATE attachment_enrichment SET state=%s,kind='image',provider='deedkit_inference',"
                      "model='vision-default',title='Synthetic title',summary='Synthetic summary',text_content=%s,"
                      "truncated=%s,indexed_at=clock_timestamp() WHERE attachment=%s",
                      (vision_state, vision, truncated, name))


def files(s, token):
    return read(s, token)


def find(s, token, query, **kwargs):
    return s.search(token, query, **kwargs)


# spec: ANYM-987
@pytest.mark.parametrize('state,words', [
    ('queued', VISION), ('leased', VISION), ('failed', VISION), ('unsupported', VISION), ('unconfigured', VISION),
    ('superseded', VISION), ('ready', ''), ('ready', '  \n\t ')])
def test_visible_text_requires_ready_current_nonblank_image_row(seeded, state, words):
    s, _, b = seeded
    image(s, 'photo-1', ocr=OCR, vision=words, vision_state=state)
    page = text(s, b, 'photo-1')
    assert page['text'] == OCR and page['content'] == 'extracted' and 'text_kind' not in page


# spec: ANYM-987
@pytest.mark.parametrize('stale', ['revision', 'digest'])
def test_stale_visible_text_never_replaces_the_ocr_text(seeded, stale):
    s, _, b = seeded
    image(s, 'photo-1', ocr=OCR, vision=VISION)
    assert text(s, b, 'photo-1')['text'] == VISION
    with s.pool.connection() as c:
        if stale == 'revision':
            c.execute("UPDATE attachment_enrichment SET source_revision=2 WHERE attachment='photo-1'")
        else:
            c.execute("UPDATE attachment_enrichment SET source_digest=repeat('a',64) WHERE attachment='photo-1'")
    page = text(s, b, 'photo-1')
    assert page['text'] == OCR and page['content'] == 'extracted' and 'text_kind' not in page


# spec: ANYM-987
def test_visible_text_precedence_is_per_image_not_per_chat(seeded):
    s, _, b = seeded
    image(s, 'photo-new', ocr=OCR, vision=VISION)
    image(s, 'photo-old', ocr='Old scan text', vision=None)
    assert (text(s, b, 'photo-new')['content'], text(s, b, 'photo-new')['text']) == ('enriched', VISION)
    assert (text(s, b, 'photo-old')['content'], text(s, b, 'photo-old')['text']) == ('extracted', 'Old scan text')


# spec: ANYM-987
def test_default_read_of_an_image_returns_its_visible_text_with_provenance(seeded):
    s, _, b = seeded
    image(s, 'photo-1', ocr=OCR, vision=VISION, meta=OCR_LOW)
    page = text(s, b, 'photo-1')
    assert page['text'] == VISION and page['content'] == 'enriched' and page['content_chars'] == len(VISION)
    assert page['text_kind'] == 'visible_text' and page['model_generated'] is True
    assert page['notice'].startswith('Model-generated')
    provenance = page['provenance']
    assert provenance['content'] == 'enriched' and provenance['text_kind'] == 'visible_text'
    assert provenance['provider'] == 'deedkit_inference' and provenance['model'] == 'vision-default'
    assert provenance['indexed_at'] and provenance['extractor'] is None
    # The confidence class and its notice describe the OCR text, never this one.
    assert 'extraction_quality' not in page and 'quality_notice' not in page
    assert page['content_coverage']['retained_chars'] == len(VISION) and page['next_offset'] is None
    assert text(s, b, 'photo-1', limit=10)['next_offset'] == 10
    assert text(s, b, 'photo-1', offset=10)['text'] == VISION[10:]
    assert s.get_file_text(b, 'photo-1')['text'] == VISION


# spec: ANYM-987
def test_explicit_extracted_read_of_the_same_image_is_the_ocr_text(seeded):
    s, _, b = seeded
    image(s, 'photo-1', ocr=OCR, vision=VISION, meta=OCR_LOW)
    ocr = text(s, b, 'photo-1', content='extracted')
    assert ocr['text'] == OCR and ocr['content'] == 'extracted' and 'text_kind' not in ocr
    assert ocr['extraction_quality'] == 'low' and 'quality_notice' in ocr
    vision = text(s, b, 'photo-1', content='enriched')
    assert vision['text'] == VISION and vision['content'] == 'enriched' and vision['text_kind'] == 'visible_text'


# spec: ANYM-987
def test_alternate_text_names_the_ocr_text(seeded):
    s, _, b = seeded
    image(s, 'photo-1', ocr=OCR, vision=VISION)
    image(s, 'photo-2', ocr=None, vision=VISION)
    assert text(s, b, 'photo-1')['alternate_text'] == {
        'content': 'extracted', 'extractor': IMAGE_OCR_EXTRACTOR, 'text_chars': len(OCR)}
    assert 'alternate_text' not in text(s, b, 'photo-2')
    assert 'alternate_text' not in text(s, b, 'photo-1', content='extracted')


# spec: ANYM-987
def test_non_image_default_reads_are_unchanged(seeded):
    s, _, b = seeded
    voice(s)
    add_file(s, 'doc-1', 'demo-tg-1', b'Plain synthetic text body', 'note.txt')
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_text SET state='ready',text_content='Plain synthetic text body',"
                  "indexed_at=clock_timestamp() WHERE attachment='doc-1'")
        c.execute("UPDATE attachment_enrichment SET state='ready',kind='document',provider='synthetic',model='m',"
                  "text_content='Model text of a document' WHERE attachment='doc-1'")
    note = text(s, b, 'voice-note')
    assert note['content'] == 'enriched' and 'text_kind' not in note and 'alternate_text' not in note
    document = text(s, b, 'doc-1')
    assert document['text'] == 'Plain synthetic text body' and document['content'] == 'extracted'
    assert 'text_kind' not in document and 'alternate_text' not in document


# spec: ANYM-987
def test_image_without_visible_text_reads_ocr_as_before(seeded):
    s, _, b = seeded
    image(s, 'photo-ok', ocr=OCR, vision=None)
    image(s, 'photo-low', ocr='zxqv kjhw poster noise', vision=None, meta=OCR_LOW)
    ok = text(s, b, 'photo-ok')
    assert ok['text'] == OCR and ok['content'] == 'extracted' and ok['extraction_quality'] == 'ok'
    assert 'text_kind' not in ok and 'alternate_text' not in ok and 'quality_notice' not in ok
    low = text(s, b, 'photo-low')
    assert low['content'] == 'extracted' and low['extraction_quality'] == 'low' and 'quality_notice' in low


# spec: ANYM-987
@pytest.mark.parametrize('state,status', [('queued', 'queued'), ('failed', 'failed'), ('unconfigured', 'not_configured')])
def test_get_file_text_of_a_pending_image_names_enrichment_status(seeded, state, status):
    s, _, b = seeded
    image(s, 'photo-1', ocr=OCR, vision='', vision_state=state)
    page = text(s, b, 'photo-1')
    assert page['content'] == 'extracted' and page['text'] == OCR and page['enrichment_status'] == status


# spec: ANYM-987
def test_message_row_excerpt_of_an_image_is_its_visible_text(seeded):
    s, _, b = seeded
    longer = ' '.join(['Synthetic printed line number %d of the chat photo.' % n for n in range(20)])
    image(s, 'photo-short', ocr=OCR, vision=VISION)
    image(s, 'photo-long', ocr=OCR, vision=longer)
    image(s, 'photo-cut', ocr=OCR, vision=VISION, truncated=True)
    shown = files(s, b)
    short = shown['photo-short']['text_excerpt']
    assert short == {'text': VISION, 'source': 'visible_text', 'model_generated': True, 'untrusted': True,
                     'excerpt_chars': len(VISION), 'total_chars': len(VISION), 'complete': True, 'read_more': None}
    long = shown['photo-long']['text_excerpt']
    assert long['source'] == 'visible_text' and 0 < long['excerpt_chars'] <= 300 and 'quality' not in long
    assert long['total_chars'] == len(longer) and long['complete'] is False
    pointer = long['read_more']
    assert pointer == {'operation': 'get_file_text', 'attachment_id': 'photo-long', 'offset': long['excerpt_chars'],
                       'content': 'enriched'}
    rest = text(s, b, 'photo-long', offset=pointer['offset'], limit=8000, content=pointer['content'])
    assert long['text'] + rest['text'] == longer
    cut = shown['photo-cut']['text_excerpt']
    assert cut['complete'] is False and cut['read_more'] is None and cut['text'] == VISION


# spec: ANYM-987
def test_low_quality_ocr_does_not_hide_ready_visible_text(seeded):
    s, _, b = seeded
    image(s, 'photo-vision', ocr='zxqv kjhw poster noise', vision=VISION, meta=OCR_LOW)
    image(s, 'photo-ocr', ocr='zxqv kjhw poster noise', vision=None, meta=OCR_LOW)
    shown = files(s, b)
    assert shown['photo-vision']['text_excerpt']['text'] == VISION
    assert 'omitted' not in shown['photo-vision']['text_excerpt']
    assert shown['photo-ocr']['text_excerpt']['omitted'] == 'ocr_low_quality'
    assert shown['photo-ocr']['text_excerpt']['source'] == 'extracted'


# spec: ANYM-987
def test_excerpt_budget_is_shared_with_visible_text(seeded):
    s, _, b = seeded
    for n in range(12):
        image(s, f'bulk-{n:02d}', ocr=OCR, vision=f'{n:02d}' + 'V' * 398)
    excerpts = [f['text_excerpt'] for name, f in sorted(files(s, b).items()) if name.startswith('bulk-')]
    assert sum(len(e.get('text', '')) for e in excerpts) <= 3000
    omitted = [e for e in excerpts if 'text' not in e]
    assert omitted and all(e['omitted'] == 'response_budget' and e['source'] == 'visible_text' for e in omitted)
    assert all(e['read_more']['offset'] == 0 and e['read_more']['content'] == 'enriched' for e in omitted)
    assert all(e['total_chars'] == 400 for e in omitted)


# spec: ANYM-987
def test_display_text_media_names_visible_text_source(seeded):
    s, _, b = seeded
    add_message(s, 'photo-msg', 'photo')
    image(s, 'photo-1', ocr=OCR, vision=VISION, message='photo-msg')
    row = message_rows(s, b, 'full')['photo-msg']
    assert row['display_text_media'] == [{'attachment_id': 'photo-1', 'source': 'visible_text',
                                          'model_generated': True, 'untrusted': True}]
    assert VISION in row['display_text'] and OCR not in row['display_text']


# spec: ANYM-987
def test_messages_search_finds_words_in_image_visible_text(seeded):
    s, _, b = seeded
    image(s, 'photo-1', ocr='unrelated scan words', vision=f'Receipt {NEEDLE} total 42')
    shown = find(s, b, NEEDLE)['items']
    assert [r['id'] for r in shown] == ['demo-tg-1']
    row = shown[0]
    assert row['matched_in'] == ['visible_text'] and row['match_type'] == 'media_text'
    entry = row['media_matches'][0]
    assert set(entry) == {'attachment_id', 'source', 'snippet', 'model_generated', 'untrusted', 'read_more'}
    assert entry['attachment_id'] == 'photo-1' and entry['source'] == 'visible_text'
    assert entry['model_generated'] is True and entry['untrusted'] is True
    assert '[[' in entry['snippet'] and ']]' in entry['snippet'] and len(entry['snippet']) <= 200
    assert entry['read_more'] == {'operation': 'get_file_text', 'attachment_id': 'photo-1', 'offset': 0,
                                  'content': 'enriched'}
    compact = render('search', find(s, b, NEEDLE, view='conversation'), 'compact')['items'][0]
    assert compact['matched_in'] == ['visible_text'] and compact['media_matches'][0]['source'] == 'visible_text'


# spec: ANYM-987
def test_messages_search_does_not_match_the_description_of_an_image(seeded):
    s, _, b = seeded
    image(s, 'photo-1', ocr='unrelated scan words', vision='')
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_enrichment SET title='A zeppelin over a lighthouse',"
                  "summary='Model description of a zeppelin' WHERE attachment='photo-1'")
    assert [r['id'] for r in find(s, b, 'zeppelin')['items']] == []
    assert [r['attachment_id'] for r in search(s, b, 'zeppelin')['items']] == ['photo-1']


# spec: ANYM-987
def test_ocr_text_is_ignored_for_an_image_with_visible_text(seeded):
    s, _, b = seeded
    image(s, 'photo-1', ocr=f'Receipt {NEEDLE}', vision='Receipt without the word')
    assert [r['id'] for r in find(s, b, NEEDLE)['items']] == []
    assert [r['id'] for r in find(s, b, 'Receipt')['items']] == ['demo-tg-1']
    assert find(s, b, 'Receipt')['items'][0]['media_matches'][0]['source'] == 'visible_text'


# spec: ANYM-987
@pytest.mark.parametrize('state,words', [('queued', ''), ('failed', ''), ('ready', ' '), ('unconfigured', '')])
def test_ocr_text_still_matches_an_image_without_visible_text(seeded, state, words):
    s, _, b = seeded
    image(s, 'photo-1', ocr=f'Receipt {NEEDLE}', vision=words, vision_state=state)
    row = find(s, b, NEEDLE)['items'][0]
    assert row['id'] == 'demo-tg-1' and row['matched_in'] == ['extracted']
    assert row['media_matches'][0]['source'] == 'extracted'


# spec: ANYM-987
def test_visible_text_hit_entry_shape_and_matched_in_order(seeded):
    s, _, b = seeded
    ocr_row(s, 'scan-1', f'Scan {NEEDLE}', {**LIMIT, 'ocr_words': 2, 'ocr_confidence': 90})
    image(s, 'photo-1', ocr='unrelated', vision=f'Printed {NEEDLE}')
    voice(s, 'voice-1', transcript=f'Spoken {NEEDLE}')
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET body=%s WHERE id='demo-tg-1'", (f'Body {NEEDLE}',))
    row = find(s, b, NEEDLE)['items'][0]
    assert row['matched_in'] == ['body', 'extracted', 'visible_text', 'transcript']
    assert {e['attachment_id']: e['source'] for e in row['media_matches']} == {
        'scan-1': 'extracted', 'photo-1': 'visible_text', 'voice-1': 'transcript'}


# spec: ANYM-987
def test_visible_text_hit_scope_and_stale_revision_are_fenced(seeded):
    s, a, b = seeded
    image(s, 'photo-wa', vision=f'Receipt {NEEDLE}', message='demo-wa-1')
    image(s, 'photo-gone', vision=f'Second {NEEDLE}', message='demo-tg-2')
    image(s, 'photo-stale', vision=f'Third {NEEDLE}', message='demo-tg-2')
    assert sorted(r['id'] for r in find(s, a, NEEDLE)['items']) == ['demo-tg-2', 'demo-wa-1']
    assert [r['id'] for r in find(s, b, NEEDLE)['items']] == ['demo-tg-2']
    with s.pool.connection() as c:
        c.execute("UPDATE attachments SET active=false WHERE id='photo-gone'")
        c.execute("UPDATE attachment_enrichment SET source_revision=2 WHERE attachment='photo-stale'")
    assert [r['id'] for r in find(s, b, NEEDLE)['items']] == []


# spec: ANYM-987
def test_visible_text_hit_is_not_ranked_last_like_low_quality_ocr(seeded):
    s, _, b = seeded
    image(s, 'photo-vision', vision=f'Receipt {NEEDLE}', message='demo-tg-1')
    image(s, 'photo-noise', ocr=f'zxqv kjhw {NEEDLE}', vision=None, meta=OCR_LOW, message='demo-tg-2')
    by_time = [r['id'] for r in find(s, b, NEEDLE, order='desc')['items']]
    assert by_time == ['demo-tg-2', 'demo-tg-1']
    ranked = [r['id'] for r in find(s, b, NEEDLE, order='relevance')['items']]
    assert ranked == ['demo-tg-1', 'demo-tg-2']


# spec: ANYM-987
def test_search_files_finds_the_visible_text_of_an_image(seeded):
    s, _, b = seeded
    image(s, 'photo-1', ocr=None, vision=f'Receipt {NEEDLE}')
    item = search(s, b, NEEDLE)['items'][0]
    assert item['attachment_id'] == 'photo-1' and item['matched_content'] == 'enriched'
    assert 'description' in item['matched_in'] and 'enriched' in item['match_sources']


# spec: ANYM-987
def test_search_files_excerpt_prefers_visible_text(seeded):
    s, _, b = seeded
    image(s, 'photo-1', ocr=f'{NEEDLE} {NEEDLE} {NEEDLE} {NEEDLE} garbled misread tokens', vision=f'A clean sentence with {NEEDLE} intact')
    item = search(s, b, NEEDLE)['items'][0]
    assert 'clean sentence' in item['excerpt'] and 'garbled' not in item['excerpt']
    assert item['excerpt_source'] == 'description'
    exact = search(s, b, NEEDLE, query_mode='exact')['items'][0]
    assert 'clean sentence' in exact['excerpt'] and exact['excerpt_source'] == 'description'


# spec: ANYM-987
def test_search_files_ocr_only_match_keeps_its_ocr_excerpt(seeded):
    s, _, b = seeded
    image(s, 'photo-1', ocr=f'Receipt {NEEDLE} misread', vision='Receipt without the word')
    item = search(s, b, NEEDLE)['items'][0]
    assert 'misread' in item['excerpt'] and item['excerpt_source'] == 'extracted'
    assert item['matched_in'] == ['extracted']


# spec: ANYM-987
def test_search_files_ocr_only_deep_match_keeps_its_passage(seeded):
    s, _, b = seeded
    deep = 'filler ' * 7000 + f'tail {NEEDLE} end'
    image(s, 'photo-1', ocr=deep, vision='Receipt without the word')
    item = search(s, b, NEEDLE)['items'][0]
    assert item['matched_content'] == 'extracted' and NEEDLE in item['excerpt'] and item['chunk_start'] > 0
    both = f'{NEEDLE} also printed here'
    image(s, 'photo-2', ocr=deep, vision=both)
    other = {i['attachment_id']: i for i in search(s, b, NEEDLE)['items']}['photo-2']
    assert other['matched_content'] == 'enriched' and 'also printed' in other['excerpt']


# spec: ANYM-987
def test_content_searched_counts_visible_text(seeded):
    s, _, b = seeded
    image(s, 'photo-1', ocr=None, vision=f'Receipt {NEEDLE}')
    image(s, 'photo-2', ocr=None, vision='')
    items = {i['attachment_id']: i for i in search(s, b, '')['items']}
    assert items['photo-1']['content_searched'] is True
    assert items['photo-2']['content_searched'] is False


# spec: ANYM-987
def test_reads_and_searches_do_not_write_or_queue_enrichment(seeded):
    s, _, b = seeded
    image(s, 'photo-1', ocr=OCR, vision=None)
    image(s, 'photo-2', ocr=OCR, vision=VISION)
    snapshot = ("SELECT attachment,state,text_content,updated_at FROM attachment_enrichment ORDER BY attachment",
                "SELECT attachment,state,text_content,updated_at FROM attachment_text ORDER BY attachment")

    def state():
        with s.pool.connection() as c:
            return [c.execute(q).fetchall() for q in snapshot]
    before = state()
    for name in ('photo-1', 'photo-2'):
        text(s, b, name)
    search(s, b, 'Receipt')
    find(s, b, 'Receipt')
    read(s, b)
    assert state() == before


# spec: ANYM-987
def test_docs_name_visible_text_precedence():
    assert 'visible_text' in (ROOT / 'FILE_INDEX.md').read_text()
    assert 'visible_text' in (ROOT / 'SEARCH.md').read_text()
    assert 'visible_text' in (ROOT / 'ENRICHMENT.md').read_text()
