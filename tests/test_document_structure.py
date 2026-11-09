"""ANYM-851: structure jobs of the enricher-mini worker (Docling Markdown of a PDF, DOCX or PPTX on the owner's mini).

A stub mini that applies the enricher key's max_concurrent 2 answers in process; no live mini, synthetic files only."""
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb

from messaging_service import document_structure as ds
from messaging_service import enrichment
from messaging_service.enrichment import EnrichmentWorker, configuration, verified_copy
from messaging_service.enrichment_providers import ModelBackpressure, convert_document, normalize_result
from test_enrichment import MINI, PNG_1X1
from test_inference_contract import recorded_results

DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
RESULTS = dict(recorded_results())
ALIASES = {'audio': 'audio-default', 'image': 'vision-default', 'document': 'vision-default'}


def add_document(s, ident, kind='pdf', *, message='demo-tg-2', data=None, metadata=None, size=None):
    """A stored file whose extracted text is in the state file_extract leaves for its kind."""
    data = data if data is not None else ('synthetic ' + ident).encode()
    digest = hashlib.sha256(data).hexdigest()
    (s.files / digest).write_bytes(data)
    filename, mime = {'pdf': (ident + '.pdf', 'application/pdf'), 'docx': (ident + '.docx', DOCX),
                      'pptx': (ident + '.pptx', ds.PPTX_MIME)}.get(kind, (ident + '.txt', 'text/plain'))
    with s.pool.connection() as c:
        c.execute('INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES(%s,%s,%s,%s,%s,%s)',
                  (ident, message, digest, filename, mime, size or len(data)))
        meta = metadata if metadata is not None else (
            {'pages_total': 2, 'pages_processed': 2, 'ocr_pages': 0, 'pdf_page_limit': 100} if kind == 'pdf' else {})
        ready = kind != 'pptx'
        assert c.execute('''UPDATE attachment_text SET state=%s,extractor=%s,extraction_profile=%s,extraction_metadata=%s,
            text_content=%s,error_code=%s,indexed_at=clock_timestamp() WHERE attachment=%s''',
            ('ready' if ready else 'unsupported',
             {'pdf': 'poppler-pdftotext-v2', 'docx': 'stdlib-docx-v1', 'text': 'stdlib-text-v1'}.get(kind),
             {'docx': 'docx', 'text': 'text'}.get(kind, 'magic') + ':chars=100000', Jsonb(meta),  # the indexer's form
             'Item B-2040 12.50' if ready else '', None if ready else 'unsupported_format', ident)).rowcount == 1
    return digest


def evict(s, digest, size):
    with s.pool.connection() as c:
        eviction = uuid4()
        c.execute('INSERT INTO attachment_residency(digest,bytes) VALUES(%s,%s) ON CONFLICT DO NOTHING', (digest, size))
        c.execute('''INSERT INTO attachment_eviction_journal(id,digest,bytes,root_device,root_inode,state,completed_at)
            VALUES(%s,%s,%s,0,0,'completed',clock_timestamp())''', (eviction, digest, size))
        c.execute('''UPDATE attachment_residency SET state='evicted',eviction_id=%s,evicted_at=clock_timestamp()
            WHERE digest=%s''', (eviction, digest))


def write_config(tmp_path, *, kinds=('image',), structure=None, **extra):
    key = tmp_path / 'mini-key'
    if not key.exists():
        key.write_text('synthetic-key'); key.chmod(0o600)
    raw = {'enabled': True, 'accounts': ['telegram-demo'], 'max_jobs_per_day': 100, **extra,
           'profiles': {k: {'provider': 'deedkit_inference', 'local_inference': True, 'base_url': MINI,
                            'api_key_file': str(key), 'model': ALIASES[k]} for k in kinds}}
    if structure is not False:
        raw['structure'] = {'provider': 'deedkit_inference', 'local_inference': True, 'base_url': MINI,
                            'api_key_file': str(key), 'model': 'document-structure', **(structure or {})}
    path = tmp_path / 'enrichment.json'
    path.write_text(json.dumps(raw)); path.chmod(0o600)
    return configuration(path)


def envelope(status, code, **headers):
    return lambda request: httpx.Response(status, headers=headers, json={'error': {'code': code, 'message': 'synthetic'}})


class Mini:
    """The owner's mini as the enricher key sees it: at most two requests at once (max_concurrent 2), a 429 beyond."""

    def __init__(self, answer=None):
        self.lock = threading.Lock()
        self.inflight = self.peak = self.refused = 0
        self.paths = []
        self.answer = answer or (lambda request: httpx.Response(200, json=RESULTS['table.pdf']))
        self.holds = {}  # path suffix -> Event the request waits for
        self.entered = {}

    def handle(self, request):
        with self.lock:
            if self.inflight >= 2:
                self.refused += 1
                return httpx.Response(429, headers={'Retry-After': '5'})
            self.inflight += 1; self.peak = max(self.peak, self.inflight); self.paths.append(request.url.path)
        try:
            for suffix, hold in self.holds.items():
                if request.url.path.endswith(suffix):
                    self.entered.setdefault(suffix, threading.Event()).set()
                    hold.wait(10)
            if request.url.path.endswith('/documents/convert'):
                return self.answer(request)
            return httpx.Response(200, json={})
        finally:
            with self.lock:
                self.inflight -= 1

    def processor(self):
        transport = httpx.MockTransport(self.handle)

        def run(files, row, profile):
            with tempfile.TemporaryDirectory() as directory:
                path = verified_copy(files, row, directory, profile['max_input_bytes'])
                if row['kind'] == 'structure':
                    return convert_document(profile, path, row['document_kind'], transport=transport)
                suffix = '/audio/transcriptions' if row['kind'] == 'audio' else '/chat/completions'
                with httpx.Client(transport=transport) as client:
                    if client.post(profile['base_url'] + suffix).status_code == 429:
                        raise ModelBackpressure(5)
            if row['kind'] == 'audio':
                return normalize_result({'text': 'Расшифровка', 'segments': [{'start': 0, 'end': 1, 'text': 'Расшифровка'}]}, audio=True)
            return normalize_result({'title': 'Фото', 'summary': 'Синтетическое фото', 'text': ''})
        return run


def make(s, cfg, mini=None, **kwargs):
    """A worker that has published, as run() does before its first claim."""
    worker = EnrichmentWorker(s.pool.conninfo, s.files, cfg, processor=(mini or Mini()).processor(), **kwargs)
    worker.publish_profiles()
    return worker


def row_of(s, ident):
    with s.pool.connection() as c:
        return c.execute('SELECT * FROM attachment_structure WHERE attachment=%s', (ident,)).fetchone()


def due_now(s, ident):
    with s.pool.connection() as c:
        c.execute('UPDATE attachment_structure SET next_due=clock_timestamp() WHERE attachment=%s', (ident,))


@pytest.fixture()
def clean(seeded):
    s = seeded[0]
    with s.pool.connection() as c:
        c.execute('TRUNCATE enrichment_budget,processing_budget,processing_control,structure_profiles')
    return seeded


# spec: ANYM-851
@pytest.mark.parametrize('override', [{'timeout_seconds': 151}, {'model': 'vision-default'}, {'provider': 'openai'},
    {'base_url': 'http://deedkit-inference.tail1234.ts.net/v1'}, {'local_inference': None}, {'max_jobs_per_day': 0},
    {'allow_cloud': True}])
def test_the_structure_key_accepts_only_the_mini_and_the_document_alias(tmp_path, override):
    with pytest.raises(ValueError):
        write_config(tmp_path, structure=override)
    cfg = write_config(tmp_path)
    assert cfg['structure']['timeout_seconds'] == 150 and cfg['structure']['max_jobs_per_day'] == 1000
    # The structure key is proven by the confinement check as the profiles are.
    from messaging_service import inference_confinement
    only = write_config(tmp_path, kinds=('audio',))
    assert inference_confinement.required(only) and inference_confinement.mini_hosts(only) == ['deedkit-inference.tail1234.ts.net']
    assert enrichment.audio_lane(only) and not enrichment.audio_lane(write_config(tmp_path, kinds=('audio',), structure=False))


# spec: ANYM-851
def test_structure_work_has_its_own_cap_and_publication_and_the_profiles_stay_as_they_are(clean, tmp_path):
    s, _, _ = clean
    add_document(s, 'cap-a'); add_document(s, 'cap-b')
    kinds = ('audio', 'image', 'document')
    plain = make(s, write_config(tmp_path, kinds=kinds, structure=False))
    plain.publish_profiles()
    query = '''SELECT account,kind,provider,model,enabled,pause_reason,paused_until FROM processing_profiles
        WHERE account='telegram-demo' ORDER BY kind'''
    with s.pool.connection() as c:
        before = c.execute(query).fetchall()
    w = make(s, write_config(tmp_path, kinds=kinds, structure={'max_jobs_per_day': 1}))
    w.publish_profiles()
    with s.pool.connection() as c:
        assert c.execute(query).fetchall() == before and [r['kind'] for r in before] == ['audio', 'document', 'image']
        assert c.execute("SELECT enabled FROM structure_profiles WHERE account='telegram-demo'").fetchone()['enabled'] is True
    assert w.structure.step() is True
    assert w.structure.step() is False  # the cap of 1 attempt today
    restarted = make(s, write_config(tmp_path, kinds=kinds, structure={'max_jobs_per_day': 1}))
    assert restarted.structure.claim() is None  # the count lives in the rows: a restart keeps it
    with s.pool.connection() as c:
        # The processing kinds' budgets are not spent by structure work.
        assert c.execute('SELECT count(*) AS n FROM processing_budget').fetchone()['n'] == 0
        assert c.execute('SELECT coalesce(sum(attempts),0) AS n FROM enrichment_budget').fetchone()['n'] == 0
    restarted.unpublish_profiles()
    with s.pool.connection() as c:
        assert c.execute("SELECT enabled FROM structure_profiles WHERE account='telegram-demo'").fetchone()['enabled'] is False


# spec: ANYM-851
def test_only_eligible_files_get_a_row(clean, tmp_path):
    s, _, _ = clean
    for ident, kind in (('el-pdf', 'pdf'), ('el-docx', 'docx'), ('el-pptx', 'pptx'), ('el-text', 'text')):
        add_document(s, ident, kind)
    add_document(s, 'el-big', size=33 * 1024 ** 2)
    # file_extract omits pages_total for a PDF of more than 100 pages (it reads 100 and marks the text truncated).
    add_document(s, 'el-pages', metadata={'pages_processed': 100, 'ocr_pages': 0, 'pdf_page_limit': 100})
    add_document(s, 'el-scan', metadata={'pages_total': 2, 'pages_processed': 2, 'ocr_pages': 2})
    add_document(s, 'el-mixed', metadata={'pages_total': 2, 'pages_processed': 2, 'ocr_pages': 1})
    add_document(s, 'el-other', message='demo-wa-1')
    evict(s, add_document(s, 'el-evicted'), len(b'synthetic el-evicted'))
    add_document(s, 'el-group', message='demo-tg-1')
    with s.pool.connection() as c:
        # An on-request group's file waits for an agent's request (ANYM-805).
        c.execute('''INSERT INTO media_jobs(id,account,message,descriptor,state)
            VALUES('el-group','telegram-demo','demo-tg-1','{}'::jsonb,'ready')''')
        c.execute("UPDATE chats SET chat_type='group' FROM messages m WHERE m.id='demo-tg-1' AND chats.id=m.chat")
        image, audio = (hashlib.sha256(x).hexdigest() for x in (PNG_1X1, b'synthetic-audio'))
        (s.files / image).write_bytes(PNG_1X1); (s.files / audio).write_bytes(b'synthetic-audio')
        c.execute("""INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES
            ('el-image','demo-tg-2',%s,'photo.png','image/png',%s),('el-audio','demo-tg-2',%s,'voice.wav','audio/wav',15)""",
            (image, len(PNG_1X1), audio))
    w = make(s, write_config(tmp_path))
    w.publish_profiles(); w.structure.sweep()
    with s.pool.connection() as c:
        rows = {r['attachment']: r for r in c.execute('SELECT * FROM attachment_structure').fetchall()}
    assert set(rows) == {'el-pdf', 'el-docx', 'el-pptx', 'el-mixed'}
    assert all(r['state'] == 'queued' and r['attempts'] == 0 for r in rows.values())
    with s.pool.connection() as c:
        states = {ident: ds.read_state(c, ident) for ident in
                  ('el-big', 'el-pages', 'el-scan', 'el-evicted', 'el-group', 'el-other', 'el-text', 'el-pdf')}
    assert {k: v.get('reason') for k, v in states.items() if v} == {'el-big': 'size_limit', 'el-pages': 'page_limit',
        'el-scan': 'scanned_pdf', 'el-evicted': 'original_evicted', 'el-group': 'withheld',
        'el-other': 'account_not_configured', 'el-pdf': None}
    assert states['el-text'] is None and states['el-pdf']['state'] == 'structure_pending'
    assert states['el-group']['next_action'].startswith('request_file_download')


# spec: ANYM-851
def test_backpressure_costs_no_attempt_and_the_row_is_claimed_again(clean, tmp_path):
    s, _, _ = clean
    add_document(s, 'bp-pdf')
    answers = [envelope(503, 'queue_full', **{'Retry-After': '30'}), lambda r: httpx.Response(200, json=RESULTS['text.pdf'])]
    mini = Mini(lambda request: answers.pop(0)(request))
    w = make(s, write_config(tmp_path), mini)
    assert w.structure.step() is True
    row = row_of(s, 'bp-pdf')
    assert (row['state'], row['attempts'], row['day_attempts'], row['error_code']) == ('queued', 0, 0, 'model_backpressure')
    with s.pool.connection() as c:
        assert c.execute("SELECT next_due>clock_timestamp()+interval '20 seconds' AS later FROM attachment_structure WHERE attachment='bp-pdf'").fetchone()['later']
        assert ds.read_state(c, 'bp-pdf')['state'] == 'processing_paused'
    assert w.structure.step() is False and w.structure.paused_until > time.monotonic() + 20  # the lane's own pause
    w.structure.paused_until = 0; due_now(s, 'bp-pdf')
    assert w.structure.step() is True
    row = row_of(s, 'bp-pdf')
    assert (row['state'], row['attempts'], row['day_attempts']) == ('ready', 1, 1)
    assert row['markdown'] == RESULTS['text.pdf']['markdown'] and row['metadata']['page_spans'] == RESULTS['text.pdf']['page_spans']


# spec: ANYM-851
def test_a_failure_is_retried_an_hour_later_once_and_then_stays_failed(clean, tmp_path):
    s, _, _ = clean
    add_document(s, 'retry-pdf')
    mini = Mini(envelope(502, 'engine_failed'))
    w = make(s, write_config(tmp_path), mini)
    assert w.structure.step() is True
    with s.pool.connection() as c:
        row = c.execute('''SELECT state,attempts,error_code,next_due>clock_timestamp()+interval '59 minutes' AS hour
            FROM attachment_structure WHERE attachment='retry-pdf' ''').fetchone()
    assert row == {'state': 'failed', 'attempts': 1, 'error_code': 'engine_failed', 'hour': True}
    assert w.structure.claim() is None  # not due within the hour
    due_now(s, 'retry-pdf')
    assert w.structure.step() is True and row_of(s, 'retry-pdf')['attempts'] == 2
    due_now(s, 'retry-pdf')
    assert w.structure.claim() is None and len(mini.paths) == 2
    with s.pool.connection() as c:
        assert ds.read_state(c, 'retry-pdf') == {'state': 'structure_failed', 'failure_reason': 'engine_failed',
            'retryable': False, 'next_action': ds.EXTRACTED_ACTION}


# spec: ANYM-851
def test_an_expired_lease_is_recovered_and_one_expired_at_the_last_attempt_ends_failed(clean, tmp_path):
    s, _, _ = clean
    add_document(s, 'lease-pdf')
    w = make(s, write_config(tmp_path))
    w.structure.sweep()
    first = w.structure.claim()
    assert first['attempts'] == 1 and w.structure.claim() is None
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_structure SET lease_until=clock_timestamp()-interval '1 second' WHERE attachment='lease-pdf'")
    second = w.structure.claim()
    assert second['attachment'] == 'lease-pdf' and second['attempts'] == 2
    assert w.structure.store(second, ds.normalize_document(RESULTS['table.pdf'])) is True
    assert w.structure.store(first, ds.normalize_document(RESULTS['text.pdf'])) is False  # the old lease writes nothing
    assert row_of(s, 'lease-pdf')['markdown'] == RESULTS['table.pdf']['markdown']
    add_document(s, 'lease-last')
    w.structure.sweep()
    with s.pool.connection() as c:
        c.execute('''UPDATE attachment_structure SET state='leased',attempts=2,lease_token=gen_random_uuid(),
            lease_until=clock_timestamp()-interval '1 second' WHERE attachment='lease-last' ''')
    assert w.structure.claim() is None
    w.structure.sweep()
    row = row_of(s, 'lease-last')
    assert (row['state'], row['error_code'], row['lease_token']) == ('failed', 'lease_expired', None)
    due_now(s, 'lease-last')
    assert w.structure.claim() is None


# spec: ANYM-851
def test_the_daily_cap_counts_two_attempts_of_one_row_as_two(clean, tmp_path):
    s, _, _ = clean
    add_document(s, 'day-pdf')
    w = make(s, write_config(tmp_path, structure={'max_jobs_per_day': 2}), Mini(envelope(504, 'engine_timeout')))
    assert w.structure.step() is True
    due_now(s, 'day-pdf')
    assert w.structure.step() is True
    assert (row_of(s, 'day-pdf')['day_attempts'], row_of(s, 'day-pdf')['error_code']) == (2, 'engine_timeout')
    add_document(s, 'day-next')
    w.structure.swept_at = None
    assert w.structure.step() is False and row_of(s, 'day-next')['state'] == 'queued'


# spec: ANYM-851
@pytest.mark.parametrize('code,state', [('unsupported_format', 'unsupported'), ('page_limit', 'failed'),
                                        ('invalid_document', 'failed')])
def test_a_refusal_is_final_and_a_terminal_row_is_never_claimed_again(clean, tmp_path, code, state):
    s, _, _ = clean
    add_document(s, 'final-pdf'); add_document(s, 'final-ready', 'docx')
    mini = Mini(lambda request: (envelope(422, code) if len(mini.paths) == 1 else
                                 lambda r: httpx.Response(200, json=RESULTS['doc.docx']))(request))
    w = make(s, write_config(tmp_path), mini)
    assert w.structure.step() is True and w.structure.step() is True
    assert {row_of(s, 'final-pdf')['state'], row_of(s, 'final-ready')['state']} == {state, 'ready'}
    due_now(s, 'final-pdf'); due_now(s, 'final-ready')
    assert w.structure.claim() is None and len(mini.paths) == 2
    with s.pool.connection() as c:
        refused = 'final-pdf' if row_of(s, 'final-pdf')['state'] == state else 'final-ready'
        found = ds.read_state(c, refused)
    assert found['state'] == ('structure_unsupported' if state == 'unsupported' else 'structure_failed')
    assert found['failure_reason'] == code and found.get('retryable', False) is False


# spec: ANYM-851
def test_a_duplicate_digest_is_copied_without_a_request(clean, tmp_path):
    s, _, _ = clean
    add_document(s, 'dup-a', data=b'synthetic duplicate'); add_document(s, 'dup-b', message='demo-tg-1', data=b'synthetic duplicate')
    mini = Mini()
    w = make(s, write_config(tmp_path), mini)
    assert w.structure.step() is True
    w.structure.swept_at = None
    assert w.structure.step() is False
    a, b = row_of(s, 'dup-a'), row_of(s, 'dup-b')
    assert a['state'] == b['state'] == 'ready' and a['markdown'] == b['markdown'] and a['engine'] == b['engine']
    assert len(mini.paths) == 1 and a['attempts'] + b['attempts'] == 1


def refuse_connection(request):
    raise httpx.ConnectError('synthetic refusal', request=request)


# spec: ANYM-851
@pytest.mark.parametrize('answer,expected', [
    (refuse_connection, ('queued', 0, 'model_backpressure')),
    (lambda r: httpx.Response(429), ('queued', 0, 'model_backpressure')),
    (envelope(503, 'queue_full'), ('queued', 0, 'model_backpressure')),
    (envelope(503, 'slot_busy'), ('queued', 0, 'model_backpressure')),
    (envelope(503, 'engine_unavailable'), ('queued', 0, 'model_backpressure')),
    (lambda r: httpx.Response(502, text='<html>proxy</html>'), ('queued', 0, 'model_backpressure')),
    (lambda r: httpx.Response(504), ('queued', 0, 'model_backpressure')),
    (envelope(404, 'not_found'), ('queued', 0, 'model_backpressure')),
    (envelope(504, 'engine_timeout'), ('failed', 1, 'engine_timeout')),
    (envelope(502, 'engine_failed'), ('failed', 1, 'engine_failed')),
    (lambda r: httpx.Response(200, json={**RESULTS['table.pdf'], 'extra': 1}), ('failed', 1, 'invalid_model_response')),
    (lambda r: httpx.Response(200, text='not json'), ('failed', 1, 'model_request_failed')),
    (envelope(422, 'unsupported_format'), ('unsupported', 1, 'unsupported_format')),
    (envelope(422, 'page_limit'), ('failed', 1, 'page_limit')),
    (envelope(422, 'invalid_document'), ('failed', 1, 'invalid_document')),
], ids=['refused', '429', 'queue_full', 'slot_busy', 'engine_unavailable', 'proxy_502', 'proxy_504', 'no_route',
        'engine_timeout', 'engine_failed', 'invalid_answer', 'not_json', 'unsupported_format', 'page_limit', 'invalid_document'])
def test_each_mini_outcome_maps_to_its_row_state(clean, tmp_path, answer, expected):
    s, _, _ = clean
    add_document(s, 'outcome-pptx', 'pptx')
    mini = Mini(answer)
    w = make(s, write_config(tmp_path), mini)
    assert w.structure.step() is True
    row = row_of(s, 'outcome-pptx')
    assert (row['state'], row['attempts'], row['error_code']) == expected and row['lease_token'] is None
    assert row['markdown'] == ''
    # The upload names the stored kind, never the chat's filename.
    assert mini.paths == ['/v1/documents/convert']


# spec: ANYM-851
def test_the_request_carries_the_alias_the_deadline_and_the_file(clean, tmp_path):
    s, _, _ = clean
    add_document(s, 'shape-docx', 'docx', data=b'synthetic docx bytes')
    seen = []

    def answer(request):
        seen.append(request)
        return httpx.Response(200, json=RESULTS['doc.docx'])
    w = make(s, write_config(tmp_path, structure={'timeout_seconds': 120}), Mini(answer))
    assert w.structure.step() is True
    from test_enrichment import multipart_parts
    fields, file = multipart_parts(seen[0])
    assert fields == {'model': 'document-structure'}
    assert file == {'field': 'file', 'filename': 'document.docx', 'content_type': DOCX, 'bytes': b'synthetic docx bytes'}
    assert seen[0].headers['X-Request-Deadline-Seconds'] == '120' and seen[0].headers['Authorization'] == 'Bearer synthetic-key'


# spec: ANYM-851
def test_a_lost_lease_or_a_purge_during_the_job_writes_nothing(clean, tmp_path):
    s, _, _ = clean
    add_document(s, 'lost-pdf'); add_document(s, 'purged-pdf', message='demo-tg-1')
    mini = Mini()
    events = {'lost-pdf': "UPDATE attachment_structure SET lease_token=gen_random_uuid() WHERE attachment='lost-pdf'",
              'purged-pdf': "UPDATE messages SET deleted=true WHERE id='demo-tg-1'"}
    inner = mini.processor()

    def processor(files, row, profile):
        result = inner(files, row, profile)
        with s.pool.connection() as c:
            c.execute(events[row['attachment']])
        return result
    w = EnrichmentWorker(s.pool.conninfo, s.files, write_config(tmp_path), processor=processor)
    assert w.structure.step() is True and w.structure.step() is True
    lost, purged = row_of(s, 'lost-pdf'), row_of(s, 'purged-pdf')
    assert lost['state'] == 'leased' and lost['markdown'] == '' and purged['state'] == 'leased' and purged['markdown'] == ''


# spec: ANYM-851
def test_a_stop_during_a_structure_job_gives_it_back_without_its_attempt(clean, tmp_path):
    s, _, _ = clean
    add_document(s, 'stop-pdf')

    def stopping(files, row, profile):
        raise enrichment.WorkerStopping
    w = EnrichmentWorker(s.pool.conninfo, s.files, write_config(tmp_path), processor=stopping)
    assert w.structure.step() is True and w.requeueing is True
    row = row_of(s, 'stop-pdf')
    # The attempt is given back; the day's count stays spent, as the kinds' stop path keeps its budget.
    assert (row['state'], row['attempts'], row['day_attempts'], row['error_code'], row['lease_token']) == (
        'queued', 0, 1, 'worker_stopping', None)


# spec: ANYM-851
def test_a_stale_or_narrowed_publication_stops_counting_as_configured(clean, tmp_path):
    s, _, _ = clean
    add_document(s, 'fresh-pdf'); add_document(s, 'fresh-wa', message='demo-wa-1')
    wide = make(s, write_config(tmp_path, accounts=['telegram-demo', 'whatsapp-demo-1']))
    with s.pool.connection() as c:
        assert ds.read_state(c, 'fresh-wa')['state'] == 'structure_pending'
    # The worker restarts with one account fewer: that account is no longer served.
    make(s, write_config(tmp_path))
    with s.pool.connection() as c:
        assert ds.read_state(c, 'fresh-wa') == {'state': 'not_eligible', 'reason': 'account_not_configured',
                                                'next_action': ds.EXTRACTED_ACTION}
        assert ds.read_state(c, 'fresh-pdf')['state'] == 'structure_pending'
        # A worker that died without unpublishing (and came back without the key) stops counting once stale.
        c.execute("UPDATE structure_profiles SET expires_at=clock_timestamp()-interval '1 second'")
        assert ds.read_state(c, 'fresh-pdf')['state'] == 'processing_not_configured'
    assert wide.structure.fresh_seconds == 150 + 600


# spec: ANYM-851
def test_a_docx_and_a_pptx_the_real_indexer_stored_are_found(clean, tmp_path):
    from test_file_index import add_file, drain, office, worker_for
    s, _, _ = clean
    body = '<w:document xmlns:w="urn:w"><w:p><w:r><w:t>Позиция B-2040</w:t></w:r></w:p></w:document>'
    add_file(s, 'real-docx', 'demo-tg-2', office({'word/document.xml': body}), 'price.docx', 'application/octet-stream')
    add_file(s, 'real-pptx', 'demo-tg-2', office({'ppt/presentation.xml': '<p:presentation xmlns:p="urn:p"/>'}),
             'deck.pptx', ds.PPTX_MIME)
    drain(worker_for(s))
    with s.pool.connection() as c:
        stored = {r['attachment']: r for r in c.execute(
            "SELECT attachment,state,extraction_profile FROM attachment_text WHERE attachment IN ('real-docx','real-pptx')")}
    assert stored['real-docx']['state'] == 'ready' and stored['real-docx']['extraction_profile'].startswith('docx:')
    assert stored['real-pptx']['state'] == 'unsupported'
    w = make(s, write_config(tmp_path))
    w.structure.sweep()
    assert row_of(s, 'real-docx')['state'] == row_of(s, 'real-pptx')['state'] == 'queued'
    assert {w.structure.claim()['document_kind'], w.structure.claim()['document_kind']} == {'docx', 'pptx'}


# spec: ANYM-851
def test_a_hung_mini_ends_at_the_timeout_plus_ten_seconds(clean, tmp_path, monkeypatch):
    s, _, _ = clean
    add_document(s, 'hung-pdf')
    real = subprocess.Popen

    def hung(command, **kwargs):  # the child that waits for an answer that never comes
        return real([sys.executable, '-c', 'import time;time.sleep(120)'], **kwargs)
    monkeypatch.setattr(enrichment.subprocess, 'Popen', hung)
    w = EnrichmentWorker(s.pool.conninfo, s.files, write_config(tmp_path, structure={'timeout_seconds': 1}))
    started = time.monotonic()
    assert w.structure.step() is True
    assert 10.5 <= time.monotonic() - started < 30
    row = row_of(s, 'hung-pdf')
    assert (row['state'], row['attempts'], row['error_code']) == ('failed', 1, 'model_timeout')


def add_media(s):
    image, audio = hashlib.sha256(PNG_1X1).hexdigest(), hashlib.sha256(b'synthetic-audio').hexdigest()
    (s.files / image).write_bytes(PNG_1X1); (s.files / audio).write_bytes(b'synthetic-audio')
    with s.pool.connection() as c:
        c.execute("""INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES
            ('lane-image','demo-tg-2',%s,'photo.png','image/png',%s),('lane-audio','demo-tg-2',%s,'voice.wav','audio/wav',15)""",
            (image, len(PNG_1X1), audio))
        c.execute("UPDATE messages SET sent_at=clock_timestamp() WHERE id='demo-tg-2'")


def enrichment_state(s, ident):
    with s.pool.connection() as c:
        return c.execute('SELECT state FROM attachment_enrichment WHERE attachment=%s', (ident,)).fetchone()['state']


# spec: ANYM-851
def test_a_structure_database_error_pauses_structure_work_only(clean, tmp_path, monkeypatch, capsys):
    s, _, _ = clean
    add_media(s); add_document(s, 'err-pdf')
    cfg = write_config(tmp_path, kinds=('audio', 'image'))
    w = make(s, cfg)

    def denied(self):
        raise psycopg.errors.InsufficientPrivilege('synthetic denial naming row err-pdf')
    monkeypatch.setattr(ds.StructureWork, 'claim', denied)
    w.kinds = frozenset({'image'})  # run() splits the audio lane off this way
    w.structure_turn = True
    assert w.once() is True  # the structure step failed and paused; the image job ran
    assert enrichment_state(s, 'lane-image') == 'ready'
    assert w.structure.paused_until > time.monotonic() + 500
    assert w.structure.step() is False
    logged = capsys.readouterr().err
    assert '"structure_paused"' in logged and 'err-pdf' not in logged and 'denial' not in logged
    from messaging_service.telemetry import safe_event
    assert safe_event(psycopg.errors.InsufficientPrivilege('x'), 'enricher', 'structure_failed') is not None
    lane = EnrichmentWorker(s.pool.conninfo, s.files, cfg, processor=Mini().processor(), kinds={'audio'})
    assert lane.structure is None and lane.once() is True and enrichment_state(s, 'lane-audio') == 'ready'
    monkeypatch.undo()
    w.structure.paused_until = 0
    assert w.structure.step() is True and row_of(s, 'err-pdf')['state'] == 'ready'


# spec: ANYM-851
@pytest.mark.parametrize('held', ['/chat/completions', '/documents/convert'])
def test_a_fresh_voice_note_is_sent_at_once_beside_image_and_structure_work(clean, tmp_path, held):
    s, _, _ = clean
    add_media(s); add_document(s, 'beside-pdf')
    mini = Mini()
    cfg = write_config(tmp_path, kinds=('audio', 'image'))
    w = make(s, cfg, mini)
    w.kinds = frozenset({'image'})
    w.structure_turn = held == '/documents/convert'
    mini.holds[held] = threading.Event()
    main = threading.Thread(target=w.once)
    main.start()
    try:
        mini.entered.setdefault(held, threading.Event()).wait(10)
        assert mini.inflight == 1
        lane = EnrichmentWorker(s.pool.conninfo, s.files, cfg, processor=mini.processor(), kinds={'audio'})
        started = time.monotonic()
        assert lane.once() is True and time.monotonic() - started < 5
    finally:
        mini.holds[held].set(); main.join(20)
    assert enrichment_state(s, 'lane-audio') == 'ready'
    assert mini.refused == 0 and mini.peak == 2
    # The main lane then takes the other kind of job: one request at a time from it.
    assert w.once() is True and mini.refused == 0
    assert enrichment_state(s, 'lane-image') == 'ready' and row_of(s, 'beside-pdf')['state'] == 'ready'
