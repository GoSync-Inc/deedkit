"""Message reads carry a bounded excerpt of a file's text (synthetic rows, no provider)."""
from messaging_service.presentation import render
from test_file_index import add_file, drain, text, worker_for
from test_file_retention_api import managed_absence
from test_voice_transcript_reading import TRANSCRIPT, voice

LONG = ' '.join(['Синтетический счёт номер 42 за маяк.'] * 20)
CHAT = 'telegram-demo-chat'


def files(rows):
    return {f['id']: f for row in rows for f in row.get('attachments') or []}


def read(s, token, *, view='archive', detail='full'):
    return files(render('get_messages', s.get_messages(token, CHAT, view=view, limit=50), detail)['items'])


# spec: ANYM-490
def test_message_attachment_carries_a_bounded_excerpt_with_a_pointer(seeded):
    s, _, b = seeded
    add_file(s, 'scan-pdf', 'demo-tg-1', LONG.encode(), 'scan.txt')
    add_file(s, 'short-note', 'demo-tg-1', b'Short synthetic note', 'note.txt')
    drain(worker_for(s))
    excerpt = read(s, b)['scan-pdf']['text_excerpt']
    assert excerpt['source'] == 'extracted' and excerpt['model_generated'] is False and excerpt['untrusted'] is True
    assert 0 < excerpt['excerpt_chars'] == len(excerpt['text']) <= 300 and LONG.startswith(excerpt['text'])
    assert excerpt['excerpt_chars'] >= 260 and excerpt['text'] == excerpt['text'].rstrip()
    assert excerpt['total_chars'] == len(LONG) and excerpt['complete'] is False
    pointer = excerpt['read_more']
    assert pointer == {'tool': 'get_file_text', 'attachment_id': 'scan-pdf', 'offset': excerpt['excerpt_chars'],
                       'content': 'extracted'}
    rest = text(s, b, 'scan-pdf', offset=pointer['offset'], limit=8000, content=pointer['content'])
    assert excerpt['text'] + rest['text'] == LONG
    short = read(s, b)['short-note']['text_excerpt']
    assert short['text'] == 'Short synthetic note' and short['complete'] is True and short['read_more'] is None
    # Every message read surface shows the same excerpt.
    for view, detail in [('archive', 'compact'), ('conversation', 'compact'), ('conversation', 'transcript')]:
        if detail == 'transcript':
            shown = {f['id']: f for f in render('get_messages', s.get_messages(b, CHAT, view=view, limit=50),
                                                detail)['files']}
        else:
            shown = read(s, b, view=view, detail=detail)
        assert shown['short-note']['text_excerpt'] == short, (view, detail)
    hit = files(render('search', s.search(b, 'Маяк', view='conversation'), 'compact')['items'])
    assert hit['short-note']['text_excerpt'] == short
    context = s.get_context(b, 'demo-tg-1')
    assert {f['id']: f for f in context['message']['attachments']}['short-note']['text_excerpt'] == short


# spec: ANYM-490
def test_voice_note_excerpt_is_its_model_transcript(seeded):
    s, _, b = seeded
    voice(s)
    excerpt = read(s, b)['voice-note']['text_excerpt']
    assert excerpt['text'] == TRANSCRIPT and excerpt['source'] == 'transcript'
    assert excerpt['model_generated'] is True and excerpt['complete'] is True and excerpt['read_more'] is None
    voice(s, 'voice-long', transcript='слово ' * 100)
    pointer = read(s, b)['voice-long']['text_excerpt']['read_more']
    assert pointer['content'] == 'enriched' and pointer['offset'] > 0


# spec: ANYM-490
def test_no_excerpt_without_ready_text_or_for_image_descriptions(seeded):
    s, _, b = seeded
    voice(s, 'voice-pending', state='unconfigured')
    voice(s, 'photo-described', filename='photo.jpg', mime='image/jpeg', transcript='Model description of a photo')
    add_file(s, 'clip', 'demo-tg-1', b'abc', 'clip.mp4', 'video/mp4')
    add_file(s, 'queued-note', 'demo-tg-1', b'Not yet extracted text', 'queued.txt')
    shown = read(s, b)
    for attachment in ('voice-pending', 'photo-described', 'clip', 'queued-note'):
        assert 'text_excerpt' not in shown[attachment], attachment
    # The existing statuses still say why there is no excerpt yet.
    assert shown['queued-note']['extraction_status'] != 'ready' and 'transcript_ready' not in shown['voice-pending']


# spec: ANYM-490
def test_excerpt_follows_the_file_text_presence_rule(seeded):
    s, _, b = seeded
    lost = add_file(s, 'lost-note', 'demo-tg-1', b'Lost synthetic text', 'lost.txt')
    kept = add_file(s, 'evicted-note', 'demo-tg-1', b'Evicted synthetic text', 'evicted.txt')
    drain(worker_for(s))
    (s.files / lost).unlink()
    managed_absence(s, kept, len(b'Evicted synthetic text'))
    shown = read(s, b)
    assert 'text_excerpt' not in shown['lost-note']
    assert shown['evicted-note']['text_excerpt']['text'] == 'Evicted synthetic text'


# spec: ANYM-490
def test_excerpts_share_one_bounded_budget_per_response(seeded):
    s, _, b = seeded
    for n in range(12):
        add_file(s, f'bulk-{n:02d}', 'demo-tg-1', (f'{n:02d}' + 'Z' * 398).encode(), f'bulk-{n}.txt')
    drain(worker_for(s))
    shown = [f for name, f in sorted(read(s, b).items()) if name.startswith('bulk-')]
    excerpts = [f['text_excerpt'] for f in shown]
    assert sum(len(e.get('text', '')) for e in excerpts) <= 3000
    omitted = [e for e in excerpts if 'text' not in e]
    assert omitted and all(e['omitted'] == 'response_budget' and e['read_more']['offset'] == 0 for e in omitted)
    assert all(e['total_chars'] == 400 for e in omitted)
    # The budget is spent in the returned order: only the trailing attachments are omitted.
    assert excerpts[-len(omitted):] == omitted
    assert all(len(e['text']) == 300 for e in excerpts if 'text' in e)
