"""ANYM-737: body_format=html adds a server-verified, ready-to-send HTML rendering of a Telegram body."""
import hashlib
import json

import psycopg
import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st
from telethon import types

from messaging_service import formatting_readback, result_evidence
from messaging_service.envelope import stable_id
from messaging_service.formatting import render_message
from messaging_service.formatting_readback import build
from messaging_service.incoming_formatting import telegram
from messaging_service.presentation import options
from messaging_service.store import APIError
from test_archive import event
from test_sending import sender as sender

ACCOUNT = 'send-fixture'
CHAT = stable_id('chat', ACCOUNT, '100')


def evidence(body, entities=(), unsupported=(), warnings=(), **changes):
    value = {'version': 1, 'state': 'partial' if warnings else 'observed', 'basis': 'telegram_native_entities',
             'wire_sha256': hashlib.sha256(body.encode()).hexdigest(), 'entities': list(entities),
             'unsupported_entities': list(unsupported), 'warnings': list(warnings)}
    value.update(changes)
    return value


def real(body, native):
    """Evidence produced by the real normalizer from synthetic Telethon entity objects."""
    return telegram(body, native)


def units(text):
    return len(text.encode('utf-16-le')) // 2


def span(kind, offset, length, **extra):
    return {'type': kind, 'offset': offset, 'length': length, **extra}


def reproduces(artifact, body, entities):
    """The sender's own renderer, run on the returned text, gives the stored text and entities."""
    sent = render_message('telegram', artifact['text'], 'html')
    key = lambda e: json.dumps(e, sort_keys=True)  # noqa: E731
    assert sent['rendered_text'] == body
    assert sorted(map(key, sent['entities'])) == sorted(map(key, entities))


# spec: ANYM-737
@pytest.mark.parametrize('kind,tag', [('bold', 'b'), ('italic', 'i'), ('underline', 'u'), ('strikethrough', 's'),
                                      ('spoiler', 'tg-spoiler'), ('code', 'code'), ('blockquote', 'blockquote')])
def test_supported_entities_render_losslessly(kind, tag):
    body = 'one 😀 two'
    # A block quote is a block for the sender: it fills the body, or sits between blank lines (own test).
    entities = [span(kind, 0, units(body))] if kind == 'blockquote' else [span(kind, 4, 6)]
    artifact, reason = build(body, evidence(body, entities))
    assert reason is None and artifact['lossless'] is True and artifact['omitted_types'] == []
    assert artifact['format'] == 'html' and f'<{tag}>' in artifact['text']
    reproduces(artifact, body, entities)


# spec: ANYM-737
def test_pre_with_language_and_text_link_render_losslessly():
    body = 'code\n\nsee link'
    entities = [span('pre', 0, 4, language='python'), span('text_link', 10, 4, url='https://example.com/a?b=1&c="2"')]
    artifact, reason = build(body, evidence(body, entities))
    assert reason is None and artifact['lossless'] is True
    assert artifact['text'].startswith('<pre language="python">code</pre>see <a href=')
    assert 'href="https://example.com/a?b=1&amp;c=&quot;2&quot;"' in artifact['text']
    reproduces(artifact, body, entities)


# spec: ANYM-737
def test_implicit_entities_stay_plain_and_do_not_make_a_row_lossy():
    body = 'mail a@example.com see https://example.com #tag $ABC /start @user +123456789'
    kinds = ['email', 'url', 'hashtag', 'cashtag', 'bot_command', 'mention', 'phone']
    entities = [span(kind, i, 1) for i, kind in enumerate(kinds)]
    artifact, reason = build(body, evidence(body, entities))
    assert reason is None and artifact['lossless'] is True and artifact['text'] == body.replace('&', '&amp;')


# spec: ANYM-737
def test_telegram_normalizer_unsupported_types_are_omitted_and_listed_by_type_only():
    body = 'bold hello custom'
    native = [types.MessageEntityBold(0, 4), types.MessageEntityMentionName(5, 5, 999),
              types.MessageEntityCustomEmoji(11, 6, 777), types.MessageEntityUnknown(0, 4),
              types.MessageEntityBlockquote(0, 4, collapsed=True)]
    stored = real(body, native)
    assert stored['state'] == 'partial'
    artifact, reason = build(body, stored)
    assert reason is None and artifact['lossless'] is False
    assert artifact['omitted_types'] == ['custom_emoji', 'expandable_blockquote', 'mention_name', 'unknown']
    reproduces(artifact, body, [span('bold', 0, 4)])
    assert '999' not in json.dumps(artifact) and '777' not in json.dumps(artifact)


# spec: ANYM-737
def test_a_link_the_sender_rejects_is_never_emitted():
    body = 'site link'
    # Passes the archive's safe_url, fails the sender's _url (a backslash).
    stored = evidence(body, [span('text_link', 0, 4, url='https://example.com/a\\b')])
    artifact, reason = build(body, stored)
    assert reason is None and artifact['omitted_types'] == ['text_link'] and artifact['lossless'] is False
    assert '<a' not in artifact['text'] and 'example.com' not in artifact['text']
    native = real(body, [types.MessageEntityTextUrl(0, 4, 'file:///secret')])
    artifact, reason = build(body, native)
    assert artifact['omitted_types'] == ['text_link'] and 'secret' not in artifact['text']


# spec: ANYM-737
@pytest.mark.parametrize('body,entities', [
    ('bold ', [span('bold', 0, 5)]),  # a span ending in whitespace
    ('abcdef', [span('bold', 0, 4), span('italic', 2, 4)]),  # crossing spans
    ('abcdef', [span('code', 0, 4), span('code', 1, 2)]),  # nested code
    ('abcdef', [span('bold', 0, 4), span('code', 1, 2)]),  # code inside a style
    ('abcdef', [span('bold', 0, 4), span('bold', 0, 4)]),  # identical spans
])
def test_layouts_the_sender_rejects_are_not_reproducible(body, entities):
    assert build(body, evidence(body, entities)) == (None, 'not_reproducible')


# spec: ANYM-737
def test_evidence_states_and_their_precedence_with_the_real_normalizer():
    body = 'hello world'
    assert build(body, real(body, None)) == (None, 'unknown_formatting')
    assert build(body, {'state': 'wire_observed', 'basis': 'whatsapp_native_wire'}) == (None, 'no_entities')
    # Listed unsupported spans: the supported ones render, lossless is false.
    listed = real(body, [types.MessageEntityBold(0, 5), types.MessageEntityCustomEmoji(6, 5, 1)])
    assert listed['warnings'] == ['native_entities_unsupported']
    artifact, reason = build(body, listed)
    assert reason is None and artifact['lossless'] is False and artifact['omitted_types'] == ['custom_emoji']
    # A dropped, unlisted span or a capped list is partial evidence, even beside the listed warnings.
    invalid = real(body, [types.MessageEntityBold(0, 5), types.MessageEntityBold(1, 100), types.MessageEntityCustomEmoji(6, 5, 1)])
    assert 'native_entities_invalid' in invalid['warnings'] and 'native_entities_unsupported' in invalid['warnings']
    assert build(body, invalid) == (None, 'partial_evidence')
    capped = real(body, [types.MessageEntityBold(0, 5)] * 257)
    assert build(body, capped) == (None, 'partial_evidence')
    # The stored body no longer matches the evidence's digest.
    assert build(body + '!', evidence(body, [span('bold', 0, 5)])) == (None, 'evidence_mismatch')
    # Observed formatting with no entities: the escaped plain text.
    artifact, reason = build('a < b & c', evidence('a < b & c'))
    assert artifact == {'format': 'html', 'text': 'a &lt; b &amp; c', 'lossless': True, 'omitted_types': []}


# spec: ANYM-737
@pytest.mark.parametrize('body,expected', [
    ('x' * 4000, None), ('x' * 4001, 'too_long'),
    ('x' * 3999, None), ('x' * 4000 + '😀', 'too_long'), ('😀' * 2000, None), ('😀' * 2001, 'too_long'),
    # 5 source units each: the sender allows 16,000 source units, but a reusable text must fit one 16 KiB request.
    ('&' * 3000, None), ('&' * 3021, 'too_long'), ('&' * 3201, 'too_long'),
    ('   \n ', 'not_reproducible'), ('a\x01b', 'not_reproducible'),
])
def test_length_and_character_boundaries(body, expected):
    artifact, reason = build(body, evidence(body))
    assert reason == expected and (artifact is None) == (expected is not None)
    if artifact:
        reproduces(artifact, body, [])


# spec: ANYM-737
def test_link_destination_boundary():
    body = 'link'
    base = 'https://example.com/'
    ok = base + 'a' * (2048 - len(base))
    artifact, reason = build(body, evidence(body, [span('text_link', 0, 4, url=ok)]))
    assert reason is None and artifact['lossless'] is True
    # The archive caps links at 2,048 too, so a longer destination arrives only as unsupported evidence.
    artifact, reason = build(body, evidence(body, [span('text_link', 0, 4, url=ok + 'a')]))
    assert reason is None and artifact['omitted_types'] == ['text_link'] and artifact['lossless'] is False


# spec: ANYM-737
@pytest.mark.parametrize('count,expected', [(128, None), (129, 'not_reproducible')])
def test_entity_count_boundary(count, expected):
    body = 'ab' * count
    entities = [span('bold', 2 * i, 1) for i in range(count)]
    artifact, reason = build(body, evidence(body, entities))
    assert reason == expected


# The sender counts its root in the 32-level cap, so 31 nested spans are the most it accepts.
# spec: ANYM-737
@pytest.mark.parametrize('depth,expected', [(31, None), (32, 'not_reproducible')])
def test_nesting_boundary(depth, expected):
    body = 'x' * (depth + 1)
    kinds = ['bold', 'italic', 'underline', 'strikethrough', 'spoiler']
    entities = [span(kinds[i % 5], 0, depth + 1 - i) for i in range(depth)]
    artifact, reason = build(body, evidence(body, entities))
    assert reason == expected


HOSTILE = ['<b>bold</b>', '&amp;', '"onclick=alert(1)', '</a>', '||spoiler||', 'back\\slash', '```fence```', '`tick`', '<a href="x">',
           '&lt;script&gt;', "<!-- c -->", '<tg-spoiler>', 'a & b < c > d']


# spec: ANYM-737
@pytest.mark.parametrize('body', HOSTILE)
def test_markup_characters_in_plain_text_stay_text(body):
    artifact, reason = build(body, evidence(body))
    assert reason is None
    reproduces(artifact, body, [])
    for entity in (span('bold', 0, 1), span('italic', 0, len(body))):
        shaped = body if body[0].strip() else 'x' + body
        found, why = build(shaped, evidence(shaped, [entity]))
        assert why is None
        reproduces(found, shaped, [entity])


# spec: ANYM-737
def test_link_labels_with_markup_and_attribute_quoting():
    body = '<b>x</b> & "y"'
    url = 'https://example.com/?q="a"&r=<b>'
    artifact, reason = build(body, evidence(body, [span('text_link', 0, len(body), url=url)]))
    assert reason is None and artifact['lossless'] is True
    assert artifact['text'] == '<a href="https://example.com/?q=&quot;a&quot;&amp;r=&lt;b&gt;">&lt;b&gt;x&lt;/b&gt; &amp; "y"</a>'
    reproduces(artifact, body, [span('text_link', 0, len(body), url=url)])


# spec: ANYM-737
def test_top_level_block_quote_keeps_the_sender_blank_line_separators():
    body = 'intro\n\nquoted text\n\noutro'
    entities = [span('bold', 0, 5), span('blockquote', 7, 11)]
    artifact, reason = build(body, evidence(body, entities))
    assert reason is None, reason
    assert artifact['text'] == '<b>intro</b><blockquote>quoted text</blockquote>outro'
    reproduces(artifact, body, entities)
    # Without the blank lines the sender would add them: the row says so instead of changing the text.
    tight = 'intro\nquoted'
    assert build(tight, evidence(tight, [span('blockquote', 6, 6)])) == (None, 'not_reproducible')


STYLES = ['bold', 'italic', 'underline', 'strikethrough', 'spoiler']
TEXT = st.text(alphabet=st.sampled_from(list('ab é😀 \n&<>"\\`|') + ['́', '漢']), min_size=1, max_size=6)


@st.composite
def layouts(draw, depth=0, parent=None):
    """A list of nodes: plain text or a styled run whose last character is never whitespace."""
    nodes = []
    for _ in range(draw(st.integers(1, 3))):
        if depth < 3 and draw(st.booleans()):
            kind = draw(st.sampled_from([k for k in STYLES if k != parent]))
            children = draw(layouts(depth + 1, kind)) + [('text', '.')]
            nodes.append(('style', kind, children))
        else:
            nodes.append(('text', draw(TEXT)))
    return nodes


def flatten(nodes, out, entities):
    for node in nodes:
        if node[0] == 'text':
            out.append(node[1])
            continue
        start = units(''.join(out))
        flatten(node[2], out, entities)
        entities.append(span(node[1], start, units(''.join(out)) - start))


@st.composite
def documents(draw):
    out, entities = ['H'], []
    flatten(draw(layouts()), out, entities)
    if draw(st.booleans()):
        # A code run and a link at the top level only: the sender refuses code that overlaps anything.
        out.append(' ')
        start = units(''.join(out))
        out.append(draw(st.sampled_from(['x = 1', '<>&', '😀 a'])))
        entities.append(span('code', start, units(''.join(out)) - start))
        out.append(' ')
        start = units(''.join(out))
        out.append(draw(st.sampled_from(['label', '😀', '<b>'])))
        entities.append(span('text_link', start, units(''.join(out)) - start, url='https://example.com/p?a=1&b=2'))
    return ''.join(out), entities


# spec: ANYM-737
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(documents())
@example(('H<b>&amp;</b>.', [span('bold', 1, 12)]))
def test_property_every_generated_lossless_layout_round_trips_through_the_sender(document):
    body, entities = document
    artifact, reason = build(body, evidence(body, entities))
    # Mandatory success: the layouts come from the lossless table, so a refusal is a bug, not a pass.
    assert reason is None, (body, entities, reason)
    assert artifact['lossless'] is True and artifact['omitted_types'] == []
    reproduces(artifact, body, entities)
    # Nothing the evidence lacks: no entity, no URL.
    assert artifact['text'].count('href=') == sum(e['type'] == 'text_link' for e in entities)


# --- the read tools -------------------------------------------------------------------------------------------

def ingest(archive, provider_id, body, entities=(), **changes):
    return archive.ingest(event(account=ACCOUNT, chat='100', provider_id=str(provider_id), body=body,
                                formatting=evidence(body, entities), **changes))['message']


def rows_of(result):
    return result.get('items') or result['context']


def count_queries(monkeypatch):
    seen = []
    original = psycopg.Connection.execute

    def counting(self, query, *args, **kwargs):
        seen.append(query)
        return original(self, query, *args, **kwargs)
    monkeypatch.setattr(psycopg.Connection, 'execute', counting)
    return seen


# spec: ANYM-737
@pytest.mark.parametrize('view', ['archive', 'conversation'])
def test_get_messages_search_and_context_add_the_rendering_only_when_asked(sender, view):
    s, archive, key, _, _ = sender
    body = 'one two three'
    mid = ingest(archive, 5, body, [span('bold', 0, 3), span('italic', 4, 3)])
    wanted = {'text': '<b>one</b> <i>two</i> three', 'format': 'html', 'lossless': True, 'omitted_types': []}
    for compact in (True, False):
        plain = s.get_messages(key, CHAT, compact=compact, view=view)
        asked = s.get_messages(key, CHAT, compact=compact, view=view, body_format='html')
        assert 'body_rendered' not in json.dumps(plain) and '_revision_body' not in json.dumps(asked)
        assert s.get_messages(key, CHAT, compact=compact, view=view, body_format='') == plain
        row = next(r for r in rows_of(asked) if r['id'] == mid)
        assert row['body_rendered'] == wanted
        found = s.search(key, query='three', compact=compact, view=view, body_format='html')
        assert next(r for r in found['items'] if r['id'] == mid)['body_rendered'] == wanted
        context = s.get_context(key, mid, compact=compact, view=view, body_format='html')
        assert context['message']['body_rendered'] == wanted
        assert '_revision_body' not in json.dumps(context)
        # The unchanged read stays free of it, row for row.
        assert 'body_rendered' not in json.dumps(s.get_context(key, mid, compact=compact, view=view))


# spec: ANYM-737
def test_a_truncated_read_still_renders_the_complete_body(sender):
    s, archive, key, _, _ = sender
    body = ' '.join('word%02d' % n for n in range(10))
    mid = ingest(archive, 6, body, [span('bold', 35, 6)])
    for call in (lambda **kw: s.get_messages(key, CHAT, body_chars=10, **kw),
                 lambda **kw: s.search(key, query='word05', body_chars=10, **kw)):
        row = next(r for r in call(body_format='html')['items'] if r['id'] == mid)
        assert row['body_rendered']['text'] == body[:35] + '<b>word05</b>' + body[41:]
        assert len(row.get('body', '')) <= 10


# spec: ANYM-737
def test_detail_transcript_is_refused_and_a_bad_value_is_invalid():
    with pytest.raises(APIError, match='invalid_parameters'):
        options('get_messages', {'detail': 'transcript', 'view': 'conversation', 'body_format': 'html'})
    options('get_messages', {'detail': 'transcript', 'view': 'conversation', 'body_format': ''})
    for bad in ('markdown', 'HTML', True):
        with pytest.raises(APIError, match='invalid_parameters'):
            formatting_readback.option(bad)


# spec: ANYM-737
def test_query_delta_is_at_most_one_batched_query_and_zero_per_row(sender, monkeypatch):
    s, archive, key, _, _ = sender
    for n in range(10, 30):
        ingest(archive, n, f'row {n} text', [span('bold', 0, 3)])
    seen = count_queries(monkeypatch)
    s.get_messages(key, CHAT, limit=20)
    base = len(seen)
    seen.clear()
    s.get_messages(key, CHAT, limit=20, body_format='html')
    assert len(seen) == base
    seen.clear()
    mid = ingest(archive, 40, 'anchor text', [span('bold', 0, 6)])
    seen.clear()
    s.get_context(key, mid, limit=20)
    base = len(seen)
    seen.clear()
    s.get_context(key, mid, limit=20, body_format='html')
    assert len(seen) - base <= 1


# spec: ANYM-737
def test_a_missing_revision_and_an_edit_between_page_and_batch(sender, monkeypatch):
    s, archive, key, _, _ = sender
    old = 'first version'
    mid = ingest(archive, 50, old, [span('bold', 0, 5)])
    original = result_evidence.formatting_evidence

    def edited(c, rows, **kwargs):
        if not getattr(edited, 'done', False):
            edited.done = True
            archive.ingest(event(account=ACCOUNT, chat='100', provider_id='50', body='second version',
                                 updated_at='2026-10-01T12:05:00Z', formatting=evidence('second version', [span('italic', 0, 6)])))
        return original(c, rows, **kwargs)
    monkeypatch.setattr(result_evidence, 'formatting_evidence', edited)
    page = s.get_messages(key, CHAT, body_format='html')
    row = next(r for r in rows_of(page) if r['id'] == mid)
    # The read row carries the old revision; its rendering is that revision's own, never the new text.
    assert row['body_rendered']['text'] == '<b>first</b> version'
    assert 'second' not in json.dumps(row['body_rendered'])
    monkeypatch.undo()
    # A revision the batch cannot find.
    with s.pool.connection() as c:
        c.execute('UPDATE message_revisions SET snapshot=snapshot-%s WHERE message=%s', ('formatting', mid))
        c.execute('UPDATE message_revisions SET snapshot=jsonb_set(snapshot,%s,%s::jsonb) WHERE message=%s',
                  ('{body}', json.dumps('second version'), mid))
    again = s.get_messages(key, CHAT, body_format='html')
    row = next(r for r in rows_of(again) if r['id'] == mid)
    assert row.get('body_rendered_unavailable') in ('unknown_formatting', 'evidence_mismatch')


# spec: ANYM-737
def test_response_budget_counts_each_message_revision_once_and_bounds_the_response(sender):
    s, archive, key, _, _ = sender
    body = '&' * 3000  # verifies: 15,000 source units
    for n in range(100, 200):
        ingest(archive, n, body + str(n))
    plain = s.get_messages(key, CHAT, limit=100, body_chars=0, compact=True)
    asked = s.get_messages(key, CHAT, limit=100, body_chars=0, compact=True, body_format='html')
    rows = rows_of(asked)
    good = [r for r in rows if 'body_rendered' in r]
    late = [r for r in rows if r.get('body_rendered_unavailable') == 'response_budget']
    size = len(json.dumps(good[0]['body_rendered'], ensure_ascii=False, separators=(',', ':')).encode())
    assert len(good) == formatting_readback.BUDGET_BYTES // size and good and late
    other = [r for r in rows if r not in good and r not in late]
    # Only the fixture's own earlier message, which has no known formatting.
    assert len(good) + len(late) == 99 and [r['body_rendered_unavailable'] for r in other] == ['unknown_formatting']
    assert rows.index(late[0]) == len(good) and all('body_rendered' not in r for r in rows[len(good):])
    dumped = json.dumps(asked, ensure_ascii=False, separators=(',', ':')).encode()
    base = json.dumps(plain, ensure_ascii=False, separators=(',', ':')).encode()
    assert len(dumped) <= len(base) + formatting_readback.BUDGET_BYTES + 64 * len(rows)
    assert len(dumped) < 8 * 1024 * 1024
    # A message that repeats in one context window is rendered once.
    mid = rows[0]['id']
    context = s.get_context(key, mid, limit=2, view='archive', compact=False, body_format='html')
    assert context['message']['body_rendered'] == next(r for r in context['context'] if r['id'] == mid)['body_rendered']


# spec: ANYM-737
def test_a_rendering_failure_costs_only_that_row(sender, monkeypatch):
    s, archive, key, _, _ = sender
    bad = ingest(archive, 60, 'SYNTHETIC-SECRET-TEXT', [span('bold', 0, 3)])
    good = ingest(archive, 61, 'other row', [span('bold', 0, 5)])
    original = formatting_readback.build

    def failing(body, stored):
        if 'SYNTHETIC-SECRET-TEXT' in body:
            raise RuntimeError(body)
        return original(body, stored)
    monkeypatch.setattr(formatting_readback, 'build', failing)
    page = s.get_messages(key, CHAT, body_format='html')
    by_id = {r['id']: r for r in rows_of(page)}
    assert by_id[bad]['body_rendered_unavailable'] == 'render_failed' and by_id[bad]['body'] == 'SYNTHETIC-SECRET-TEXT'
    assert by_id[good]['body_rendered']['text'] == '<b>other</b> row'


# spec: ANYM-737
def test_without_the_option_neither_the_converter_nor_the_body_batch_runs(sender, monkeypatch):
    s, archive, key, _, _ = sender
    ingest(archive, 70, 'plain row', [span('bold', 0, 5)])
    monkeypatch.setattr(formatting_readback, 'build', lambda *a: pytest.fail('converter reached'))
    seen = []
    original = result_evidence.formatting_evidence
    monkeypatch.setattr(result_evidence, 'formatting_evidence',
                        lambda c, rows, **kw: (seen.append(kw), original(c, rows, **kw))[1])
    s.get_messages(key, CHAT)
    s.search(key, query='plain')
    assert seen and all(not kw.get('with_body') for kw in seen)


# spec: ANYM-737
def test_the_rendering_is_accepted_by_preview_and_send(sender):
    s, archive, key, _, _ = sender
    body = 'Title\n\nquoted line\n\nsee docs'
    entities = [span('bold', 0, 5), span('blockquote', 7, 11), span('text_link', 24, 4, url='https://example.com/docs')]
    mid = ingest(archive, 80, body, entities)
    row = next(r for r in rows_of(s.get_messages(key, CHAT, body_format='html')) if r['id'] == mid)
    text = row['body_rendered']['text']
    preview = s.preview_message(key, ACCOUNT, CHAT, text, format='html')
    assert preview['rendered_text'] == body and preview['entities'] == sorted(
        entities, key=lambda e: (e['offset'], -e['length'], e['type']))
    sent = s.send_message(key, ACCOUNT, CHAT, text, 'synthetic-rendered-request', format='html',
                          preview_digest=preview['preview_digest'])
    assert sent and sent.get('error') is None


# spec: ANYM-737
def test_a_text_the_request_limit_would_refuse_is_never_returned_as_ready_to_send():
    body = 'abc'
    url = 'https://example.com/' + '漢' * 2000
    entities = [span('text_link', i, 1, url=url) for i in range(3)]
    # Valid for the sender by UTF-16 units (6,108), but 18,108 UTF-8 bytes against a 16,384-byte request.
    assert build(body, evidence(body, entities)) == (None, 'too_long')
    artifact, reason = build(body, evidence(body, entities[:1]))
    assert reason is None and len(json.dumps(artifact['text'], ensure_ascii=False).encode()) <= formatting_readback.MAX_TEXT_BYTES


# spec: ANYM-737
def test_a_fault_inside_the_converter_is_render_failed_not_a_property_of_the_row(sender, monkeypatch):
    s, archive, key, _, _ = sender
    mid = ingest(archive, 90, 'fault row', [span('bold', 0, 5)])
    monkeypatch.setattr(formatting_readback, 'render_message', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    row = next(r for r in rows_of(s.get_messages(key, CHAT, body_format='html')) if r['id'] == mid)
    assert row['body_rendered_unavailable'] == 'render_failed' and row['body'] == 'fault row'


# spec: ANYM-737
def test_a_saturated_full_context_stays_within_the_budget_by_serialized_size(sender):
    s, archive, key, _, _ = sender
    ids = [ingest(archive, n, '&' * 3000 + str(n)[-1]) for n in range(300, 351)]
    # The middle message is the anchor; 25 neighbours each side, enough rows to pass the cap.
    plain = s.get_context(key, ids[25], before_count=25, after_count=25, view='archive', compact=False)
    asked = s.get_context(key, ids[25], before_count=25, after_count=25, view='archive', compact=False, body_format='html')
    pack = lambda v: json.dumps(v, ensure_ascii=False, separators=(',', ':')).encode()  # noqa: E731
    occurrences = [asked['message'], *asked['context']]
    charged = [r['body_rendered'] for r in occurrences if 'body_rendered' in r]
    assert sum(len(pack(a)) for a in charged) <= formatting_readback.BUDGET_BYTES
    assert any(r.get('body_rendered_unavailable') == 'response_budget' for r in occurrences)
    assert len(pack(asked)) <= len(pack(plain)) + formatting_readback.BUDGET_BYTES + 64 * len(occurrences)
