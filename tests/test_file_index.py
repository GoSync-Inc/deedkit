"""Real byte extraction, scoped retrieval, invalidation and crash fencing."""
import hashlib
from contextlib import contextmanager
import io
import os
from pathlib import Path
import shutil
import struct
import zlib
import zipfile

import pytest
import psycopg

from messaging_service.file_extract import (Extraction, MAX_INPUT_BYTES, MAX_TEXT_CHARS,
    _xml, Rejected, extract_original)
from messaging_service.file_index import FileIndexer, get_file_text, search_files
from messaging_service.store import APIError


def original(root, content):
    digest = hashlib.sha256(content).hexdigest()
    path = Path(root) / digest
    path.write_bytes(content)
    return path, digest


def extract(root, content, filename='note.txt', mime='text/plain'):
    path, digest = original(root, content)
    return extract_original(path, digest=digest, bytes=len(content), filename=filename, mime=mime)


def office(parts, compression=zipfile.ZIP_STORED):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression=compression) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return out.getvalue()


def test_extraction_unicode_control_cleaning_and_explicit_truncation(tmp_path):
    result = extract(tmp_path, 'Привет мир\nAlice @example\tinvoice 42\x01'.encode())
    assert result.state == 'ready' and result.text == 'Привет мир\nAlice @example\tinvoice 42'
    assert not result.truncated and result.extractor == 'stdlib-text-v1'
    result = extract(tmp_path, b'A' * (MAX_TEXT_CHARS + 10))
    assert result.text == 'A' * MAX_TEXT_CHARS and result.truncated
    result = extract(tmp_path, 'Привет'.encode('utf-16'))
    assert result.state == 'ready' and result.text == 'Привет'


def test_extraction_checks_digest_size_symlink_and_never_filename_path(tmp_path):
    path, digest = original(tmp_path, b'safe')
    args = {'digest': digest, 'bytes': 4, 'mime': 'text/plain', 'filename': '../../secrets.txt'}
    assert extract_original(path, **args).text == 'safe'
    path.write_bytes(b'evil')
    assert extract_original(path, **args).error_code == 'original_integrity'
    path.unlink()
    target = tmp_path / 'target'
    target.write_bytes(b'safe')
    path.symlink_to(target)
    assert extract_original(path, **args).error_code == 'original_unavailable'
    path.unlink()
    os.mkfifo(path)
    assert extract_original(path, **args).error_code == 'original_integrity'
    assert extract_original(path, **{**args, 'bytes': MAX_INPUT_BYTES + 1}).error_code == 'input_size_limit'


def test_extraction_docx_xlsx_no_macros_no_external_relationships(tmp_path):
    docx = office({'word/document.xml': '<w:document xmlns:w="urn:w"><w:p><w:r>'
        '<w:t>Budget €250</w:t></w:r></w:p></w:document>',
        'word/_rels/document.xml.rels': '<external Target="https://invalid.example/never-fetch"/>'})
    result = extract(tmp_path, docx, 'contract.docx', 'application/octet-stream')
    assert result.state == 'ready' and 'Budget €250' in result.text
    xlsx = office({'xl/sharedStrings.xml': '<sst xmlns="urn:s"><si><t>Invoice Alice</t></si></sst>',
        'xl/worksheets/sheet1.xml': '<worksheet xmlns="urn:s"><sheetData><row>'
        '<c t="s"><v>0</v></c><c><f>WEBSERVICE("https://invalid.example")</f><v>25</v></c>'
        '<c t="inlineStr"><is><t>Paid</t></is></c></row></sheetData></worksheet>'})
    result = extract(tmp_path, xlsx, 'invoice.xlsx', 'application/octet-stream')
    assert result.state == 'ready' and 'Invoice Alice\t25\tPaid' in result.text
    assert 'WEBSERVICE' not in result.text


def test_extraction_archive_bomb_traversal_xml_entities_and_binary_are_rejected(tmp_path):
    bomb = office({'word/document.xml': 'A' * 100000}, zipfile.ZIP_DEFLATED)
    assert extract(tmp_path, bomb, 'bomb.docx').error_code == 'unsafe_archive'
    bad_path = office({'../word/document.xml': '<w/>'})
    assert extract(tmp_path, bad_path, 'path.docx').error_code == 'unsafe_archive'
    for raw in [b'<!DOCTYPE d [<!ENTITY x "unsafe">]><d>&x;</d>',
                '<!DOCTYPE d [<!ENTITY x "unsafe">]><d>&x;</d>'.encode('utf-16')]:
        with pytest.raises(Rejected, match='unsafe_xml'):
            _xml(raw)
    assert extract(tmp_path, b'safe\x00nottext').error_code == 'binary_text'
    assert extract(tmp_path, b'PK\x03\x04broken', 'broken.docx').error_code == 'invalid_document'
    assert extract(tmp_path, b'video', 'movie.mp4', 'video/mp4').state == 'unsupported'


def pdf_bytes(text='Synthetic archive invoice 123'):
    stream = ('BT /F1 18 Tf 20 150 Td (' + text + ') Tj ET').encode()
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream']
    out = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out.extend(str(number).encode() + b' 0 obj\n' + obj + b'\nendobj\n')
    start = len(out)
    out.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets[1:]:
        out.extend(f'{offset:010} 00000 n \n'.encode())
    out.extend(b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n' + str(start).encode() + b'\n%%EOF\n')
    return bytes(out)


def test_native_pdf_extracts_text_and_input_over_four_megabytes(tmp_path):
    if not shutil.which('pdftotext', path='/usr/local/bin:/usr/bin:/bin'):
        pytest.skip('poppler is required in the indexer/test image')
    result = extract(tmp_path, pdf_bytes() + b'\n%' + b' ' * (5 * 1024**2), 'large.pdf', 'application/pdf')
    assert result.state == 'ready' and 'Synthetic archive invoice 123' in result.text
    assert not result.truncated


def png_text():
    alphabet = {
        'H': ['10001','10001','10001','11111','10001','10001','10001'],
        'E': ['11111','10000','10000','11110','10000','10000','11111'],
        'L': ['10000','10000','10000','10000','10000','10000','11111'],
        'O': ['01110','10001','10001','10001','10001','10001','01110'],
    }
    width, height, scale = 420, 150, 10
    pixels = [bytearray([255] * width) for _ in range(height)]
    for i, char in enumerate('HELLO'):
        for y, line in enumerate(alphabet[char]):
            for x, bit in enumerate(line):
                if bit == '1':
                    for yy in range(35+y*scale, 35+(y+1)*scale):
                        pixels[yy][35+i*70+x*scale:35+i*70+(x+1)*scale] = bytes([0] * scale)
    raw = b''.join(b'\0' + row for row in pixels)
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind+data))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width,height,8,0,0,0,0)) + \
        chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


def test_native_ocr_png_and_rejects_pixel_bomb(tmp_path):
    if not shutil.which('tesseract', path='/usr/local/bin:/usr/bin:/bin'):
        pytest.skip('tesseract eng+rus is required in the indexer/test image')
    result = extract(tmp_path, png_text(), 'photo.png', 'image/png')
    assert result.state == 'ready' and 'hello' in result.text.lower()
    bomb = b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\rIHDR' + struct.pack('>II', 50000, 50000)
    assert extract(tmp_path, bomb, 'huge.png', 'image/png').error_code == 'image_size_limit'


def worker_for(s, **kwargs):
    return FileIndexer(s.pool.conninfo, s.files, **kwargs)


def drain(worker):
    result = []
    for _ in range(30):
        item = worker.run_once()
        if not item['claimed']:
            return result
        result.append(item)
    raise AssertionError('queue did not drain')


def add_file(s, attachment, message, content, filename='invoice.txt', mime='text/plain'):
    _, digest = original(s.files, content)
    with s.pool.connection() as c:
        c.execute('INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES(%s,%s,%s,%s,%s,%s)',
                  (attachment, message, digest, filename, mime, len(content)))
    return digest


def search(s, token, *args, **kwargs):
    with s.authorized(token, 'search') as (c, p):
        return search_files(s, c, p, *args, **kwargs)


def text(s, token, attachment, **kwargs):
    with s.authorized(token, 'search') as (c, p):
        return get_file_text(s, c, p, attachment, **kwargs)


def ids(result):
    return [x['attachment_id'] for x in result['items']]


def test_file_search_content_filename_scope_filters_and_paging(seeded):
    s, a, b = seeded
    add_file(s, 'file-alpha', 'demo-tg-1', b'Long confidential unicorn invoice paid', 'Alpha_100%.txt')
    add_file(s, 'file-beta', 'demo-wa-1', b'unicorn secret beta', 'beta.txt')
    assert all(r['status'] == 'ready' for r in drain(worker_for(s)))
    assert ids(search(s, a, 'unicorn', chats=['telegram-demo-chat', 'whatsapp-demo-1-chat'])) == ['file-alpha','file-beta']
    assert ids(search(s, b, 'unicorn')) == ['file-alpha']
    assert ids(search(s, a, filename='_100%')) == ['file-alpha']
    assert ids(search(s, a, 'unicorn invoice', query_mode='phrase')) == ['file-alpha']
    assert ids(search(s, a, 'unicorn', senders=['demo-person-1'], accounts=['whatsapp-demo-1'])) == ['file-beta']
    assert ids(search(s, a, 'unicorn', senders=['demo-person-2'])) == []
    assert ids(search(s, a, 'unicorn', accounts=[])) == []
    first = search(s, a, 'unicorn', order='desc', limit=1)
    assert ids(first) == ['file-beta']
    assert ids(search(s, a, 'unicorn', order='desc', after=first['next_cursor'])) == ['file-alpha']
    with pytest.raises(APIError, match='not_found'):
        search(s, b, after='file-beta')
    s.set_access('agent-a', chats=['whatsapp-demo-1-chat'])
    assert ids(search(s, a, 'unicorn')) == ['file-beta']
    with pytest.raises(APIError, match='not_found'):
        text(s, a, 'file-alpha')


def test_file_text_provenance_bounded_pages_revocation_and_read_without_download(seeded):
    s, a, b = seeded
    digest = add_file(s, 'file-pages', 'demo-tg-1', b'ABCDEF' * 5000)
    drain(worker_for(s))
    s.set_access('agent-b', permissions=['read'])
    result = text(s, b, 'file-pages', limit=100)
    assert len(result['text']) == 100 and result['next_offset'] == 100
    assert text(s, b, 'file-pages', offset=100, limit=100)['text'] == ('ABCDEF' * 5000)[100:200]
    assert result['provenance']['sha256'] == digest and result['provenance']['revision'] == 1
    for params in [{'offset': -1}, {'offset': True}, {'offset': MAX_TEXT_CHARS+1}, {'limit': 8001}]:
        with pytest.raises(APIError):
            text(s, b, 'file-pages', **params)
    with pytest.raises(APIError, match='not_found'):
        text(s, b, 'bad\x00id')
    s.set_access('agent-b', permissions=[])
    with pytest.raises(APIError, match='forbidden'):
        text(s, b, 'file-pages')


def test_file_index_revision_digest_delete_and_inactive_invalidate_immediately(seeded):
    s, a, b = seeded
    add_file(s, 'file-stale', 'demo-tg-1', b'oldconfidential')
    worker = worker_for(s)
    drain(worker)
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET revision=revision+1 WHERE id='demo-tg-1'")
    assert ids(search(s, b, 'oldconfidential')) == ['file-stale']
    result = text(s, b, 'file-stale')
    assert result['status'] == 'ready' and result['text'] == 'oldconfidential' and result['revision'] == 2
    drain(worker)
    _, new_digest = original(s.files, b'newconfidential')
    with s.pool.connection() as c:
        c.execute("UPDATE attachments SET digest=%s,bytes=15 WHERE id='file-stale'", (new_digest,))
    assert ids(search(s, b, 'oldconfidential')) == []
    drain(worker)
    assert ids(search(s, b, 'newconfidential')) == ['file-stale']
    with s.pool.connection() as c:
        c.execute("UPDATE attachments SET active=false WHERE id='file-stale'")
    with pytest.raises(APIError, match='not_found'):
        text(s, b, 'file-stale')
    with s.pool.connection() as c:
        c.execute("UPDATE attachments SET active=true WHERE id='file-stale'")
    drain(worker)
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET deleted=true WHERE id='demo-tg-1'")
    assert ids(search(s, b, 'newconfidential')) == []
    with pytest.raises(APIError, match='not_found'):
        text(s, b, 'file-stale')


def test_file_index_lease_crash_reclaim_and_old_worker_cannot_publish(seeded):
    s, a, b = seeded
    worker = worker_for(s)
    drain(worker)
    add_file(s, 'file-race', 'demo-tg-1', b'race')
    old = worker.claim()
    assert old['attachment'] == 'file-race'
    assert worker.claim() is None
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_text SET lease_until=clock_timestamp()-interval '1 second' WHERE attachment='file-race'")
    new = worker.claim()
    assert new['lease_token'] != old['lease_token'] and new['attempts'] == 2
    assert worker.finish(old, Extraction('ready', 'stalecontent', 'test-v1')) == 'superseded'
    assert worker.finish(new, Extraction('ready', 'currentcontent', 'test-v1')) == 'ready'
    assert text(s, b, 'file-race')['text'] == 'currentcontent'
    with s.pool.connection() as c:
        c.execute("UPDATE attachments SET active=true WHERE id='file-race'")
    pending = worker.claim()
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET revision=revision+1 WHERE id='demo-tg-1'")
    assert worker.finish(pending, Extraction('ready', 'oldrevision', 'test-v1')) == 'superseded'
    assert ids(search(s, b, 'oldrevision')) == []


def test_file_index_retry_exhaustion_unsupported_and_missing_original(seeded):
    s, a, b = seeded
    drain(worker_for(s))
    add_file(s, 'file-fails', 'demo-tg-1', b'abc')
    worker = worker_for(s, max_attempts=2,
        extractor=lambda *_a, **_kw: Extraction('failed', error_code='extraction_timeout'))
    assert worker.run_once()['status'] == 'retry'
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_text SET next_due=clock_timestamp() WHERE attachment='file-fails'")
    assert worker.run_once()['status'] == 'failed'
    assert worker.run_once()['status'] == 'idle'
    assert text(s, b, 'file-fails')['error_code'] == 'extraction_timeout'
    add_file(s, 'file-unsupported', 'demo-tg-1', b'abc', 'clip.mp4', 'video/mp4')
    assert worker_for(s).run_once()['status'] == 'unsupported'
    assert ids(search(s, b, filename='clip.mp4')) == ['file-unsupported']
    digest = add_file(s, 'file-missing', 'demo-tg-1', b'onlyoriginal')
    drain(worker_for(s))
    (s.files / digest).unlink()
    assert ids(search(s, b, 'onlyoriginal')) == []
    with pytest.raises(APIError, match='attachment_unavailable'):
        text(s, b, 'file-missing')


def test_file_index_separate_roles_trigger_enqueue_and_worker_grants(seeded):
    import messaging_service.store as module
    s, a, b = seeded
    drain(worker_for(s))
    collector, indexer = 'test_file_collector', 'test_file_indexer'
    for name, role in [('collector', collector), ('indexer', indexer)]:
        ddl = Path(module.__file__).with_name(name + '_role.sql').read_text().replace('messaging_' + name, role)
        with s.pool.connection() as c:
            c.execute(ddl)
    @contextmanager
    def restricted(role):
        with s.pool.connection() as c:
            c.execute('SET LOCAL ROLE ' + role)
            yield c
    worker = worker_for(s)
    worker.connection = lambda: restricted(indexer)
    try:
        _, digest = original(s.files, b'role-separated text')
        with restricted(collector) as c:
            c.execute('''INSERT INTO attachments(id,message,digest,filename,mime,bytes)
                VALUES('file-roles','demo-tg-1',%s,'roles.txt','text/plain',19)''', (digest,))
        assert worker.run_once()['status'] == 'ready'
        assert text(s, b, 'file-roles')['text'] == 'role-separated text'
        with restricted(collector) as c:
            c.execute("UPDATE messages SET revision=revision+1 WHERE id='demo-tg-1'")
        retained = text(s, b, 'file-roles')
        assert retained['status'] == 'ready' and retained['revision'] == 2
        assert retained['text'] == 'role-separated text'
        assert worker.run_once()['status'] == 'idle'
        with restricted(collector) as c:
            c.execute("UPDATE attachments SET active=false WHERE id='file-roles'")
        with pytest.raises(APIError, match='not_found'):
            text(s, b, 'file-roles')
        denied = [(collector, 'SELECT * FROM attachment_text'),
                  (collector, "UPDATE attachment_text SET text_content='injected'"),
                  (indexer, 'SELECT * FROM api_keys'),
                  (indexer, "UPDATE messages SET body='changed'"),
                  (indexer, 'DELETE FROM attachment_text')]
        for role, sql in denied:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with restricted(role) as c:
                    c.execute(sql)
    finally:
        for role in (collector, indexer):
            with s.pool.connection() as c:
                c.execute('DROP OWNED BY ' + role)
                c.execute('DROP ROLE ' + role)


@pytest.mark.parametrize('params', [
    {'query_mode': {}}, {'filename': 'x\x00y'}, {'accounts': 'account'},
    {'senders': [True]}, {'after': True}, {'after': 'a', 'before': 'b'},
    {'limit': True}, {'query': 'x\x00y'}, {'from_time': 'tomorrow'},
    {'after': 'x\x00y'}, {'before': 'x\x00y'},
])
def test_file_search_invalid_parameters_are_safe(seeded, params):
    s, a, b = seeded
    with pytest.raises(APIError):
        search(s, b, **params)
