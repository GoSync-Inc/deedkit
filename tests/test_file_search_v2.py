"""File retrieval contract, local OCR coverage and occurrence-safe reuse."""
import base64
import json
import shutil
import subprocess
import zlib

import pytest
from psycopg.types.json import Jsonb

from messaging_service.file_extract import Extraction, MAX_OCR_PAGES, _pdf, valid_metadata
from messaging_service.file_index import display_name, file_response_fields, get_file_occurrences
from messaging_service.store import APIError
from test_file_index import add_file, drain, extract, ids, original, pdf_bytes, png_text, search, text, worker_for


def test_display_name_decodes_only_explicit_encoding_and_keeps_original():
    source = '=?UTF-8?B?' + base64.b64encode('Договор.pdf'.encode()).decode() + '?='
    assert display_name(source) == 'Договор.pdf'
    assert display_name('=?utf-8?Q?Monthly_Invoice.pdf?=') == 'Monthly Invoice.pdf'
    assert display_name('invoice%20one.pdf') == 'invoice%20one.pdf'
    assert display_name('=?UTF-8?B?!!!?=') == '=?UTF-8?B?!!!?='
    assert display_name('../x\x00\u202E.pdf') == '.._x.pdf'
    row = file_response_fields({'filename': source, 'status': 'unsupported', 'text_chars': 0})
    assert row['filename'] == row['source_filename'] == source
    assert row['display_name'] == 'Договор.pdf'
    assert row['download_status'] == 'ready' and row['extraction_status'] == 'unsupported'
    assert row['enrichment_status'] == 'not_configured'


def scanned_pdf(page_count=1, raster=None):
    """Synthetic image-only PDF; no fonts/text layer or external dependencies."""
    png = png_text(); offset = 8; chunks = []
    while offset < len(png):
        size = int.from_bytes(png[offset:offset+4], 'big')
        if png[offset+4:offset+8] == b'IDAT':
            chunks.append(png[offset+8:offset+8+size])
        offset += size+12
    pixels = zlib.decompress(b''.join(chunks))
    raw = b''.join(pixels[y*421+1:(y+1)*421] for y in range(150))
    width, height = 420, 150
    if raster is not None:
        width, height, raw = raster
    image = zlib.compress(raw)
    content = b'q 600 0 0 200 0 0 cm /Im0 Do Q'
    page_ids = list(range(5, 5+page_count))
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [' + b' '.join(str(n).encode()+b' 0 R' for n in page_ids) +
        b'] /Count ' + str(page_count).encode() + b' >>',
        b'<< /Type /XObject /Subtype /Image /Width ' + str(width).encode() + b' /Height ' + str(height).encode() + b' /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode /Length ' + str(len(image)).encode() + b' >>\nstream\n' + image + b'\nendstream',
        b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n'+content+b'\nendstream']
    objects += [b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 200] /Resources << /XObject << /Im0 3 0 R >> >> /Contents 4 0 R >>'] * page_count
    out = bytearray(b'%PDF-1.4\n'); offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(out)); out.extend(str(number).encode()+b' 0 obj\n'+obj+b'\nendobj\n')
    start = len(out); out.extend(b'xref\n0 '+str(len(offsets)).encode()+b'\n0000000000 65535 f \n')
    for position in offsets[1:]:
        out.extend(f'{position:010} 00000 n \n'.encode())
    out.extend(b'trailer\n<< /Size '+str(len(offsets)).encode()+b' /Root 1 0 R >>\nstartxref\n'+str(start).encode()+b'\n%%EOF\n')
    return bytes(out)


def test_scanned_pdf_native_ocr_with_page_provenance(tmp_path):
    if not all(shutil.which(tool, path='/usr/local/bin:/usr/bin:/bin') for tool in ('pdftotext', 'pdftoppm', 'tesseract')):
        pytest.skip('native indexer image required')
    # Rasterize a standard Helvetica fixture, then embed only its grayscale
    # pixels. This avoids the deliberately coarse 5x7 font used by unit tests.
    source = tmp_path/'font-source.pdf'; source.write_bytes(pdf_bytes('HELLO OCR RECEIPT'))
    subprocess.run(['pdftoppm', '-gray', '-singlefile', '-r', '200', str(source), str(tmp_path/'font')], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pgm = (tmp_path/'font.pgm').read_bytes()
    header, dimensions, maximum, pixels = pgm.split(b'\n', 3)
    assert header == b'P5' and maximum == b'255'
    width, height = map(int, dimensions.split())
    assert len(pixels) == width*height
    result = extract(tmp_path, scanned_pdf(raster=(width,height,pixels)), 'scan.pdf', 'application/pdf')
    assert result.state == 'ready' and 'hello' in result.text.lower()
    assert result.metadata['pages_total'] == result.metadata['pages_processed'] == result.metadata['ocr_pages'] == 1
    assert result.metadata['page_spans'][0]['method'] == 'ocr' and not result.truncated


def test_scanned_pdf_ocr_page_budget_is_explicit(tmp_path, monkeypatch):
    import messaging_service.file_extract as module
    calls = []
    def run(command, **kwargs):
        calls.append(command[0])
        if command[0] == 'pdftotext':
            (tmp_path/'output.txt').write_text('\f'*(MAX_OCR_PAGES+2))
        elif command[0] == 'pdftoppm':
            (tmp_path/'page.png').write_bytes(png_text())
        else:
            (tmp_path/'page-text.txt').write_text('Synthetic OCR receipt')
    monkeypatch.setattr(module, '_run_native', run)
    result = _pdf(tmp_path/'input.pdf', tmp_path)
    assert calls.count('tesseract') == MAX_OCR_PAGES
    assert result.state == 'ready' and result.truncated and result.error_code == 'ocr_page_limit'
    assert result.metadata['pages_total'] == MAX_OCR_PAGES+2
    assert result.metadata['pages_processed'] == MAX_OCR_PAGES
    assert result.metadata['page_spans'][-1]['method'] == 'unavailable'
    assert valid_metadata(result.metadata)
    assert not valid_metadata({'page_spans': [{'page': 0, 'start': 0, 'end': 2, 'method': 'ocr'}]})


def test_file_morphology_centered_snippet_relevance_and_bound_cursor(seeded):
    s, a, b = seeded
    add_file(s, 'file-late-word', 'demo-tg-1', ('Вступление. '*100 + 'Договоры обсуждались с клиентами.').encode(), 'notes.txt')
    add_file(s, 'file-repeated', 'demo-tg-2', 'Клиенты обсуждали договор. Договоры готовы.'.encode(), 'extra.txt')
    drain(worker_for(s))
    result = search(s, b, 'договор', search_language='russian', order='relevance', limit=1)
    assert len(result['items']) == 1 and result['next_cursor'].startswith('q1.')
    assert 'договор' in result['items'][0]['excerpt'].lower() and result['items'][0]['rank'] > 0
    rest = search(s, b, 'договор', search_language='russian', order='relevance', after=result['next_cursor'])
    assert set(ids(result)+ids(rest)) == {'file-late-word', 'file-repeated'}
    assert not search(s, b, 'договорами', search_language='simple')['items']
    assert len(search(s, b, 'договорами', search_language='auto')['items']) == 2
    assert ids(search(s, b, 'Вступление.', query_mode='exact')) == ['file-late-word']
    with pytest.raises(APIError, match='invalid_cursor'):
        search(s, b, 'клиент', search_language='russian', order='relevance', after=result['next_cursor'])
    with pytest.raises(APIError, match='invalid_cursor'):
        search(s, a, 'договор', search_language='russian', order='relevance', after=result['next_cursor'])
    raw = json.loads(base64.urlsafe_b64decode(result['next_cursor'][3:]+'='*(-len(result['next_cursor'][3:])%4)))
    raw['p']['rank'] += 1
    tampered = 'q1.'+base64.urlsafe_b64encode(json.dumps(raw).encode()).decode().rstrip('=')
    with pytest.raises(APIError, match='invalid_cursor'):
        search(s, b, 'договор', search_language='russian', order='relevance', after=tampered)


CLASSES = {  # attachment -> (provider label, MIME, expected media_class)
    'm-voice': ('voice', 'audio/ogg', 'voice'), 'm-voice-mp4': ('voice', 'audio/mp4', 'voice'),
    'm-voice-nolabel': (None, 'audio/ogg', 'voice'), 'm-voice-opus': (None, 'audio/opus', 'voice'),
    'm-voice-app': (None, 'application/ogg', 'voice'),
    'm-audio': ('audio', 'audio/mpeg', 'audio'), 'm-audio-nolabel': (None, 'audio/mpeg', 'audio'),
    'm-photo': ('photo', 'image/jpeg', 'image'), 'm-sticker': ('sticker', 'image/webp', 'image'),
    'm-image-nolabel': (None, 'image/png', 'image'),
    'm-video': ('video', 'video/mp4', 'video'), 'm-animation': ('animation', 'video/mp4', 'video'),
    'm-video-nolabel': (None, 'video/webm', 'video'),
    'm-document': ('document', 'application/pdf', 'document'), 'm-other': ('other', 'application/zip', 'document'),
    'm-text-nolabel': (None, 'text/plain', 'document'), 'm-photo-as-doc': ('document', 'image/jpeg', 'image'),
}


def media_files(s):
    """One message per file (the provider label belongs to the message), then every file indexed."""
    from test_search_v2 import add
    for attachment, (label, mime, _) in CLASSES.items():
        add(s, 'msg-' + attachment, 'caption of ' + attachment)
        if label:
            with s.pool.connection() as c:
                c.execute('INSERT INTO message_metadata(message,revision,metadata) VALUES(%s,1,%s)',
                          ('msg-' + attachment, Jsonb({'media_kind': label})))
        add_file(s, attachment, 'msg-' + attachment, b'synthetic ' + attachment.encode(), attachment + '.bin', mime)
    drain(worker_for(s))


def only(result, prefix):
    return sorted(x['attachment_id'] for x in result['items'] if x['attachment_id'].startswith(prefix))


# spec: ANYM-661
def test_media_kind_filter_classes_provider_labels_mime_fallback_and_aliases(seeded):
    s, a, b = seeded
    media_files(s)
    everything = search(s, b, limit=100)
    assert {x['attachment_id']: x['media_class'] for x in everything['items'] if x['attachment_id'].startswith('m-')} == {
        name: expected for name, (_, _, expected) in CLASSES.items()}
    by_class = lambda *kinds: sorted(n for n, (_, _, c) in CLASSES.items() if c in kinds)
    for kinds, classes in ((['voice'], ('voice',)), (['audio'], ('audio',)), (['voice', 'audio'], ('voice', 'audio')),
                           (['image'], ('image',)), (['photo'], ('image',)), (['sticker', 'image', 'photo'], ('image',)),
                           (['video'], ('video',)), (['animation'], ('video',)), (['document'], ('document',)),
                           (['other'], ('document',)), (['voice', 'voice', 'photo'], ('voice', 'image'))):
        assert only(search(s, b, media_kind=kinds, limit=100), 'm-') == by_class(*classes), kinds
    assert only(search(s, b, media_kind=[], limit=100), 'm-') == by_class('voice', 'audio', 'image', 'video', 'document')
    row = [x for x in search(s, b, media_kind=['photo'], limit=100)['items'] if x['attachment_id'] == 'm-photo'][0]
    assert row['media_class'] == 'image' and row['media_kind'] == 'photo'    # the provider label is unchanged


# spec: ANYM-661
@pytest.mark.parametrize('kwargs,field,reason', [
    ({'media_kind': ['gif']}, 'media_kind', 'enum'), ({'media_kind': ['VOICE']}, 'media_kind', 'enum'),
    ({'media_kind': 'voice'}, 'media_kind', 'type'), ({'media_kind': [1]}, 'media_kind', 'type'),
    ({'ready': True}, 'ready', 'type'), ({'ready': ['transcripts']}, 'ready', 'enum'),
    ({'ready': ['none', 'text']}, 'ready', 'format')])
def test_media_filters_refuse_bad_values_with_field_errors(seeded, kwargs, field, reason):
    s, a, b = seeded
    with pytest.raises(APIError) as error:
        search(s, b, **kwargs)
    assert error.value.code == 'invalid_filter' and error.value.fields == [field]
    entry, = error.value.field_errors
    assert entry['field'] == field and entry['reason'] == reason
    if reason == 'enum':
        assert {'voice', 'photo'} <= set(entry['allowed']) or {'text', 'none'} <= set(entry['allowed'])


READY = {  # attachment -> (MIME, what is ready)
    'r-text': ('text/plain', 'text'), 'r-voice-ok': ('audio/ogg', 'transcript'), 'r-voice-none': ('audio/ogg', None),
    'r-voice-old': ('audio/ogg', 'old-revision'), 'r-voice-super': ('audio/ogg', 'superseded'),
    'r-image-desc': ('image/png', 'enrichment'), 'r-video-und': ('video/mp4', 'understanding'),
    'r-doc-enriched': ('application/pdf', 'enrichment'), 'r-image-none': ('image/png', None)}


def ready_files(s):
    from test_search_v2 import add
    for attachment, (mime, _) in READY.items():
        add(s, 'msg-' + attachment, 'caption of ' + attachment)
        add_file(s, attachment, 'msg-' + attachment, b'synthetic ' + attachment.encode(), attachment + '.bin', mime)
    drain(worker_for(s))
    with s.pool.connection() as c:
        for attachment, (mime, what) in READY.items():
            kind = {'audio/ogg': 'audio', 'image/png': 'image'}.get(mime, 'document')
            if what in ('transcript', 'enrichment', 'old-revision', 'superseded'):
                c.execute("""UPDATE attachment_enrichment SET state=%s,kind=%s,provider='test-local',model='fixture',title='T',
                    summary='S',text_content='synthetic derived text',indexed_at=clock_timestamp(),
                    source_digest=CASE WHEN %s THEN repeat('0',64) ELSE source_digest END WHERE attachment=%s""",
                    ('superseded' if what == 'superseded' else 'ready', kind, what == 'old-revision', attachment))
            if what == 'understanding':
                c.execute("""UPDATE attachment_understanding SET state='ready',source_artifact_hash=repeat('a',64),
                    published_profile_hash=repeat('b',64),title='T',summary='S',indexed_at=clock_timestamp() WHERE attachment=%s""",
                    (attachment,))
            if what == 'text':
                c.execute("UPDATE attachment_text SET state='ready',text_content='synthetic text' WHERE attachment=%s", (attachment,))


# spec: ANYM-661
def test_ready_filter_reads_each_readiness_table_for_the_current_revision_only(seeded):
    s, a, b = seeded
    ready_files(s)
    got = lambda **kw: only(search(s, b, limit=100, **kw), 'r-')
    assert got(ready=['text']) == ['r-text']
    assert got(ready=['transcript']) == ['r-voice-ok']        # a doc/image enrichment is a description, never a transcript
    assert got(ready=['description']) == ['r-doc-enriched', 'r-image-desc', 'r-video-und']
    assert got(ready=['text', 'transcript']) == ['r-text', 'r-voice-ok']
    assert got(ready=['transcript', 'description', 'text']) == ['r-doc-enriched', 'r-image-desc', 'r-text', 'r-video-und', 'r-voice-ok']
    # An older revision of a re-uploaded file and a superseded row never count.
    assert got(ready=['none']) == ['r-image-none', 'r-voice-none', 'r-voice-old', 'r-voice-super']
    assert got(media_kind=['voice'], ready=['transcript']) == ['r-voice-ok']
    assert got(media_kind=['voice'], ready=['none']) == ['r-voice-none', 'r-voice-old', 'r-voice-super']
    assert got(media_kind=['voice', 'audio']) == sorted(n for n, (m, _) in READY.items() if m == 'audio/ogg')
    assert got(ready=[]) == sorted(READY)
    # No match is an empty page, not an error; the original and its availability fields are never hidden by a filter.
    empty = search(s, b, media_kind=['video'], ready=['transcript'])
    assert empty['items'] == [] and empty['next_cursor'] is None
    plain = {x['attachment_id']: x for x in search(s, b, limit=100)['items']}
    fields = ('download_status', 'original_available', 'original_residency', 'extraction_status')
    for item in search(s, b, ready=['description'], limit=100)['items']:
        assert {k: item.get(k) for k in fields} == {k: plain[item['attachment_id']].get(k) for k in fields}


# spec: ANYM-661
def test_media_filters_bind_the_relevance_cursor_both_ways_and_a_keyset_anchor_continues(seeded):
    s, a, b = seeded
    ready_files(s)
    kinds = ['voice', 'audio', 'document']
    first = search(s, b, 'synthetic', order='relevance', limit=1, media_kind=kinds)
    assert first['next_cursor'].startswith('q1.')
    assert search(s, b, 'synthetic', order='relevance', limit=1, media_kind=['audio', 'voice', 'document'],
                  after=first['next_cursor'])['items']
    for changes in ({}, {'media_kind': ['voice']}, {'media_kind': kinds, 'ready': ['text']}):
        with pytest.raises(APIError, match='invalid_cursor'):
            search(s, b, 'synthetic', order='relevance', limit=1, after=first['next_cursor'], **changes)
    plain = search(s, b, 'synthetic', order='relevance', limit=1)
    for changes in ({'ready': ['none']}, {'media_kind': ['voice']}):
        with pytest.raises(APIError, match='invalid_cursor'):
            search(s, b, 'synthetic', order='relevance', limit=1, after=plain['next_cursor'], **changes)
    # A keyset anchor need not pass the new filters: the page continues from its position.
    rest = search(s, b, after='r-image-desc', media_kind=['document'], limit=100)
    assert [x['attachment_id'] for x in rest['items'] if x['attachment_id'].startswith('r-')] == ['r-text']
    assert all(x['media_class'] == 'document' for x in rest['items'])


def test_reuse_context_free_text_and_occurrences_never_leak_hidden_names(seeded):
    s, a, b = seeded
    drain(worker_for(s))
    payload = b'Identical attachment content shared deliberately'
    add_file(s, 'file-private', 'demo-private-1', payload, 'hidden-client-name.txt')
    drain(worker_for(s))
    add_file(s, 'file-visible', 'demo-tg-1', payload, 'public-note.txt')
    calls = []
    def unexpected(*args, **kwargs):
        calls.append(True)
        return Extraction('failed', error_code='extraction_failed')
    assert worker_for(s, extractor=unexpected).run_once()['status'] == 'ready'
    assert not calls
    result = text(s, b, 'file-visible')
    assert result['text'] == payload.decode() and result['source_filename'] == 'public-note.txt'
    assert not search(s, b, filename='hidden-client')['items']
    with s.authorized(b, 'search') as (c, p):
        occurrences = get_file_occurrences(s, c, p, 'file-visible')
    assert ids(occurrences) == ['file-visible']
    with pytest.raises(APIError, match='not_found'):
        with s.authorized(b, 'search') as (c, p):
            get_file_occurrences(s, c, p, 'file-private')
    add_file(s, 'file-second', 'demo-tg-2', payload, 'another.txt')
    drain(worker_for(s))
    with s.authorized(b, 'search') as (c, p):
        first = get_file_occurrences(s, c, p, 'file-visible', limit=1)
        second = get_file_occurrences(s, c, p, 'file-visible', after=first['next_cursor'])
    assert ids(first)+ids(second) == ['file-visible', 'file-second']


def test_reuse_requires_matching_processor_profile_and_current_original_integrity(seeded):
    s, a, b = seeded
    drain(worker_for(s))
    payload = b'cached raw document'
    digest = add_file(s, 'cached', 'demo-tg-1', payload)
    drain(worker_for(s))
    add_file(s, 'different-profile', 'demo-tg-1', payload, 'document.docx')
    assert worker_for(s).run_once()['status'] == 'failed'
    add_file(s, 'corrupt-copy', 'demo-tg-1', payload)
    (s.files/digest).write_bytes(b'x'*len(payload))
    worker = worker_for(s)
    assert worker.run_once()['status'] == 'failed'
    assert text(s, b, 'corrupt-copy')['error_code'] == 'original_integrity'


def test_enriched_transcript_search_paging_scope_and_invalidation(seeded):
    s, a, b = seeded
    add_file(s, 'voice-public', 'demo-tg-1', b'non-audio fixture', 'voice.ogg', 'audio/ogg')
    add_file(s, 'voice-private', 'demo-private-1', b'private fixture', 'voice.ogg', 'audio/ogg')
    drain(worker_for(s))
    assert text(s, b, 'voice-public')['enrichment_status'] == 'not_configured'
    transcript = 'Обсуждали документы и договоры.\n' * 6000
    with s.pool.connection() as c:
        c.execute("""UPDATE attachment_enrichment SET state='ready',kind='audio',provider='test-local',model='fixture',
            title='Встреча по контракту',summary='Синтетическая расшифровка',text_content=%s,metadata=%s,
            truncated=true,indexed_at=clock_timestamp() WHERE attachment IN ('voice-public','voice-private')""",
            [transcript, Jsonb({'source_truncated': True, 'segments': [{'start': 0, 'end': 5, 'text': 'test'}]})])
    result = search(s, b, 'договорами', order='relevance')
    assert ids(result) == ['voice-public']
    item = result['items'][0]
    assert item['download_status'] == 'ready' and item['extraction_status'] == 'unsupported'
    assert item['enrichment_status'] == 'ready' and item['model_generated']
    assert item['title'] == 'Встреча по контракту' and 'договор' in item['excerpt'].lower()
    page = text(s, b, 'voice-public', content='enriched', offset=100100, limit=321)
    assert page['text'] == transcript[100100:100421] and page['next_offset'] == 100421
    assert page['provenance']['provider'] == 'test-local' and page['enrichment_truncated']
    assert page['enrichment_coverage']['has_timestamps'] and page['enrichment_coverage']['source_truncated']
    assert 'segments' not in page['enrichment_coverage']
    with pytest.raises(APIError, match='not_found'):
        text(s, b, 'voice-private', content='enriched')
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET body='Updated caption',revision=revision+1 WHERE id='demo-tg-1'")
    assert ids(search(s, b, 'договорами')) == ['voice-public']
    page = text(s, b, 'voice-public', content='enriched', offset=100100, limit=321)
    assert page['text'] == transcript[100100:100421] and page['next_offset'] == 100421
    assert page['title'] == 'Встреча по контракту' and page['enrichment_status'] == 'ready'
    assert page['provenance']['revision'] == 2 and page['provenance']['provider'] == 'test-local'
    replacement = b'replacement non-audio fixture'
    _, digest = original(s.files, replacement)
    with s.pool.connection() as c:
        c.execute("UPDATE attachments SET digest=%s,bytes=%s WHERE id='voice-public'", (digest, len(replacement)))
    assert not search(s, b, 'договорами')['items']
    page = text(s, b, 'voice-public', content='enriched')
    assert page['text'] == '' and page['enrichment_status'] == 'not_configured' and page['title'] == ''
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET deleted=true WHERE id='demo-tg-1'")
    assert not search(s, b, filename='voice.ogg')['items']
    with pytest.raises(APIError, match='not_found'):
        text(s, b, 'voice-public', content='enriched')


def test_encoded_filename_search_and_stale_extraction_metadata_reset(seeded):
    s, a, b = seeded
    source = '=?UTF-8?B?' + base64.b64encode('Договор.pdf'.encode()).decode() + '?='
    add_file(s, 'encoded', 'demo-tg-1', b'notes', source, 'text/plain')
    drain(worker_for(s))
    result = search(s, b, 'Договор', search_language='simple')
    assert ids(result) == ['encoded'] and result['items'][0]['source_filename'] == source
    assert result['items'][0]['display_name'] == 'Договор.pdf'
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_text SET extraction_metadata=%s WHERE attachment='encoded'", [Jsonb({'pages_total': 5})])
        c.execute("UPDATE messages SET body='Updated caption',revision=revision+1 WHERE id='demo-tg-1'")
    page = text(s, b, 'encoded')
    assert page['extraction_metadata'] == {'pages_total': 5} and page['status'] == 'ready'
    assert page['text'] == 'notes' and page['provenance']['revision'] == 2
    assert ids(search(s, b, 'Договор', search_language='simple')) == ['encoded']
    replacement = b'replacement notes'
    _, digest = original(s.files, replacement)
    with s.pool.connection() as c:
        c.execute("UPDATE attachments SET digest=%s,bytes=%s,filename='replacement.txt' WHERE id='encoded'",
                  (digest, len(replacement)))
    page = text(s, b, 'encoded')
    assert page['extraction_metadata'] == {} and page['status'] == 'queued'
    assert page['text'] == '' and page['source_filename'] == 'replacement.txt'
    assert not search(s, b, 'Договор', search_language='simple')['items']
