"""ANYM-665: messages_search also finds the text inside a message's photos, PDFs and voice notes.

The media arm is a third source of candidate messages next to the body and the contact card. It reads only the GIN
indexes the file search already uses (attachment_text 013/016, attachment_enrichment 015) and then rechecks the
bounded text itself, because the indexed vector also holds the filename and the decoded display name: a filename is
not extracted text. The title and summary of an image never match (ANYM-490); its visible text, the vision model's
reading of the words printed in it, does (ANYM-987) and takes the place of its OCR text, which matches only for an image
without visible text. Nothing here grants access: every candidate
message still passes the caller's scope clauses of the search.
"""
from __future__ import annotations

import logging
import re

from .file_extract import IMAGE_OCR_EXTRACTOR, OCR_LOW_CONFIDENCE, ocr_quality
from .media_policy import ELIGIBILITY_JOIN, visible_text_ready

log = logging.getLogger(__name__)

MODES = ('all', 'phrase', 'web', 'any')
TEXT_CHARS = 32000       # the indexed bound of attachment_text (016_file_content.sql)
SNIPPET_CHARS = 200
EVIDENCE_CAP = 20
# order=relevance: a media hit takes the rank TIER of the query mode over its bounded text only (all/phrase: literal 2,
# adjacent words 1, other 0; any: the share of distinct terms; web: one tier), without the density a body hit adds, and
# sits one unit under it. So a body hit of the same tier always comes first, media hits of one tier keep the time order,
# and text read with low OCR confidence ranks last. Body scores keep their scale (cursors stay valid).
ESCAPE = " ESCAPE E'\\\\'"
AUDIO = "(a.mime LIKE 'audio/%%' OR a.mime='application/ogg')"
IMAGE = "left(a.mime,6)='image/'"
# ANYM-987: the vision text of attachment a is ready in enrichment row `ev`, current for the OCR row's revision `rev`.
def _vision_of(rev):
    return ("EXISTS(SELECT 1 FROM attachment_enrichment ev WHERE ev.attachment=a.id AND ev.source_digest=a.digest"
            " AND ev.source_revision=" + rev + " AND " + visible_text_ready('a', 'ev') + ")")
assert "'" not in IMAGE_OCR_EXTRACTOR
# ANYM-669's ocr_quality in SQL: low = image OCR with a counted word and a mean confidence under the threshold.
LOW = ("(CASE WHEN f.extractor='" + IMAGE_OCR_EXTRACTOR + "' AND jsonb_typeof(f.extraction_metadata->'ocr_words')='number'"
       " AND (f.extraction_metadata->>'ocr_words') ~ '^[0-9]{1,7}$' THEN CASE WHEN (f.extraction_metadata->>'ocr_words')::int>0"
       " AND jsonb_typeof(f.extraction_metadata->'ocr_confidence')='number'"
       " AND (f.extraction_metadata->>'ocr_confidence') ~ '^-?[0-9]{1,7}$'"
       " THEN (f.extraction_metadata->>'ocr_confidence')::int<" + str(OCR_LOW_CONFIDENCE) + " ELSE false END ELSE false END)")
EXACT_HINT = ('query_mode=exact does not search the text inside photos, PDFs or voice notes. '
              'Use search_files with query_mode=exact for that.')


WEB_HINT = ('A web query that only excludes words, or joins OR with an exclusion, is not searched in the text of photos, '
            'PDFs or voice notes: it would match every attachment without the word. Use search_files, or add a word to require.')
_WEB_TOKEN = re.compile(r'(-?)"([^"]*)"?|(\S+)')


def web_terms(query):
    """websearch_to_tsquery's tokens as (negated, text): a quoted phrase stays whole, `or` is (False, None). None when a
    token mixes a quote into a word, whose reading is not worth guessing."""
    terms = []
    for match in _WEB_TOKEN.finditer(query):
        word = match.group(3)
        if word is None:
            terms.append((match.group(1) == '-', '"' + match.group(2) + '"'))
        elif '"' in word:
            return None
        elif word.lower() == 'or':
            terms.append((False, None))
        elif word.startswith('-') and len(word) > 1:
            terms.append((True, word[1:]))
        else:
            terms.append((False, word))
    return terms


def positive(query):
    """The indexed prefilter of a web query: the query itself when it excludes nothing; with exclusions ANDed to required
    words, the required words alone (a superset of the matches: the indexed vector also holds the filename and the
    enrichment title, where an excluded word may sit; the full query runs on the text afterwards). None when no sound
    prefilter exists: only exclusions, or OR beside an exclusion."""
    terms = web_terms(query)
    if terms is None:
        return None
    if not any(negated for negated, _ in terms):
        return query
    if any(text is None for _, text in terms) or all(negated for negated, _ in terms):
        return None
    return ' '.join(text for negated, text in terms if not negated)


def applies(plan, include_media_text):
    """The media arm runs for a query in the modes with a text index; exact has no index to serve LIKE, and a web query
    without a sound prefilter would match every attachment."""
    if not (bool(plan.query) and include_media_text and plan.mode in MODES):
        return False
    return plan.mode != 'web' or positive(plan.query) is not None


def hint(plan, include_media_text):
    """Why the media arm was skipped on purpose, when it was."""
    if not (plan.query and include_media_text):
        return None
    if plan.mode == 'exact':
        return EXACT_HINT
    return WEB_HINT if plan.mode == 'web' and positive(plan.query) is None else None


def _prefilter(plan, alias):
    if plan.mode != 'web':
        return plan.predicate(alias), []
    return ('(' + ' OR '.join(plan.vector(alias, lang) + " @@ websearch_to_tsquery('" + lang + "',%s)"
                              for lang in plan.languages) + ')', [positive(plan.query)] * len(plan.languages))


def _tier(plan, text):
    """The rank tier of the query mode over a bounded text, as a bigint on the 0..1000000 scale of the body rank."""
    if plan.mode in ('all', 'phrase'):
        phrase = ('q.phrase_' if plan.mode == 'all' else 'q.')
        adjacent = ' OR '.join("to_tsvector('" + lang + "'," + text + ') @@ ' + phrase + lang for lang in plan.languages)
        literal = 'lower(' + text + ') LIKE lower(q.literal_pattern)' + ESCAPE
        return ('(round(((CASE WHEN ' + literal + ' THEN 2 WHEN ' + adjacent + ' THEN 1 ELSE 0 END)::float8/3*1000000)::numeric))::bigint')
    if plan.mode == 'any':
        # Distinct matching terms over the text alone (the stored vector would count filename terms).
        vectors = ','.join("to_tsvector('" + lang + "'," + text + ') AS v' + str(n) for n, lang in enumerate(plan.languages))
        hit = ' OR '.join('tv.v' + str(n) + ' @@ t.c' + str(n) for n in range(len(plan.languages)))
        return ('(SELECT round((count(*)::float8/' + str(plan.terms) + '*1000000)::numeric)::bigint FROM unnest('
                + ','.join('q.terms_' + lang for lang in plan.languages) + ') AS t(' +
                ','.join('c' + str(n) for n in range(len(plan.languages))) + '),(SELECT ' + vectors + ' OFFSET 0) tv WHERE ' + hit + ')')
    return '0::bigint'


def ctes(plan, tiered=False):
    """(CTE statements, their parameters): index-served candidate messages, one row per matching attachment. The
    prefilter uses the same expressions as the indexes so the planner stays on the GIN indexes; the join to
    attachments is by primary key (never ARRAY(subquery) over the unindexed attachments.message, ANYM-579) and
    carries the freshness keys the message read uses (result_evidence.decorate). The full query then runs over the
    bounded text alone, so a filename or title never makes a hit."""
    limit = str(TEXT_CHARS)
    text, voice = 'left(f.text_content,' + limit + ')', 'left(e.text_content,' + limit + ')'
    pre_text, params_text = _prefilter(plan, 'f')
    pre_voice, params_voice = _prefilter(plan, 'e')
    text_rank = _tier(plan, text) if tiered else '0::bigint'
    voice_rank = _tier(plan, voice) if tiered else '0::bigint'
    sql = ['media_text AS MATERIALIZED (SELECT a.message,f.source_revision,' + LOW + ' AS low,' + text_rank + ' AS relevance FROM attachment_text f'
        ' JOIN attachments a ON a.id=f.attachment AND a.active AND f.source_digest=a.digest AND f.source_filename=a.filename'
        " AND f.source_mime=a.mime AND f.source_bytes=a.bytes CROSS JOIN search_query q WHERE f.state='ready' AND NOT "
        + _vision_of('f.source_revision') + ' AND ' + pre_text + ' AND ' + plan.text_predicate(text) +
        ' UNION ALL SELECT a.message,e.source_revision,false,' + voice_rank + ' FROM attachment_enrichment e'
        ' JOIN attachments a ON a.id=e.attachment AND a.active AND e.source_digest=a.digest CROSS JOIN search_query q'
        " WHERE (" + AUDIO + ' OR ' + IMAGE + ") AND e.state='ready' AND " + pre_voice + ' AND ' + plan.text_predicate(voice) + ')']
    return sql, [*params_text, *params_voice]


def arm(order_sql, clauses, extra_join, tiered):
    """Candidate messages with a current attachment whose ready text or transcript matched. `tiered` (order=relevance)
    ranks them after the body hits, OCR noise last; other orders do not read the score."""
    score = 'CASE WHEN t.low THEN 0 ELSE greatest(t.relevance-1,1) END' if tiered else '0'
    return ('SELECT m.id,m.sent_at,m.revision,' + order_sql + ' AS source_order,(' + score + ')::bigint AS score'
            ' FROM messages m ' + extra_join + ' JOIN media_text t ON t.message=m.id AND t.source_revision=m.revision'
            ' WHERE ' + ' AND '.join(clauses))


def timeout(error):
    """A canceled media search names the cheaper call; psycopg's error keeps its class and so its existing code."""
    error.next_action = 'retry_search_with_include_media_text=false'
    return error


def clip(snippet):
    """At most SNIPPET_CHARS around the first marker, with every marker pair closed."""
    if len(snippet) <= SNIPPET_CHARS:
        return snippet
    opening = snippet.find('[[')
    if opening < 0:
        return snippet[:SNIPPET_CHARS]
    start = max(0, opening - 60)
    window = snippet[start:start + SNIPPET_CHARS]
    last = window.rfind('[[')
    if last > window.rfind(']]'):
        # The cut fell inside a highlight: drop a later one, or close the first at the edge.
        window = window[:last].rstrip() if last > opening - start else window[:SNIPPET_CHARS - 2] + ']]'
    return window


def _headline(plan, text):
    """The first configuration whose vector matches decides the excerpt, so one 32,000-character text is not
    parsed once per language."""
    choices = ["WHEN to_tsvector('" + lang + "'," + text + ') @@ q.' + lang + " THEN ts_headline('" + lang + "'," + text +
               ',q.' + lang + ", 'MaxWords=25,MinWords=10,MaxFragments=1,StartSel=[[,StopSel=]]')" for lang in plan.languages]
    return '(CASE ' + ' '.join(choices) + ' ELSE left(' + text + ',' + str(SNIPPET_CHARS) + ') END)'


def annotate(c, plan, items, flags, *, fuzzy_used):
    """matched_in on every hit row, media_matches on the rows a photo, PDF or voice note matched. `flags` maps a shown
    row to (body, contact, media candidate), read in the page statement. The evidence is bound to the (message, revision)
    each row shows: a revision that changed after the page was chosen has no media text here, as it has no attachments
    in decorate. Returns how many rows are hits only through low-quality OCR text."""
    from psycopg.errors import QueryCanceled
    from .result_evidence import _readable
    found = {}
    shown = [(i['id'], i['revision']) for i in items if flags.get(i['id'], (0, 0, 0))[2]]
    if shown:
        text, voice = 'left(f.text_content,' + str(TEXT_CHARS) + ')', 'left(e.text_content,' + str(TEXT_CHARS) + ')'
        ids, revisions = [m for m, _ in shown], [r for _, r in shown]
        pairs = 'unnest(%s::text[],%s::integer[]) AS r(message,revision) JOIN attachments a ON a.message=r.message AND a.active'
        try:
            rows = c.execute(
                'WITH ' + plan.cte + " SELECT a.message,a.id AS attachment_id,'extracted' AS source," + _headline(plan, text) +
                " AS snippet,a.digest,a.bytes,f.extractor,jsonb_strip_nulls(jsonb_build_object('ocr_words',"
                "f.extraction_metadata->'ocr_words','ocr_confidence',f.extraction_metadata->'ocr_confidence')) AS ocr_metadata"
                ' FROM ' + pairs + ' JOIN messages m ON m.id=a.message AND m.revision=r.revision AND NOT m.deleted'
                ' JOIN attachment_text f ON f.attachment=a.id AND f.source_digest=a.digest AND f.source_revision=r.revision'
                ' AND f.source_filename=a.filename AND f.source_mime=a.mime AND f.source_bytes=a.bytes CROSS JOIN search_query q'
                " WHERE f.state='ready' AND NOT " + _vision_of('r.revision') + ' AND ' + plan.text_predicate(text) +
                " UNION ALL SELECT a.message,a.id,CASE WHEN " + AUDIO + " THEN 'transcript' ELSE 'visible_text' END," + _headline(plan, voice) + ',a.digest,a.bytes,NULL,NULL'
                ' FROM ' + pairs + ' JOIN messages m ON m.id=a.message AND m.revision=r.revision AND NOT m.deleted'
                ' JOIN attachment_enrichment e ON e.attachment=a.id AND e.source_digest=a.digest AND e.source_revision=r.revision'
                ' CROSS JOIN search_query q WHERE (' + AUDIO + ' OR ' + IMAGE + ") AND e.state='ready' AND "
                + plan.text_predicate(voice) + ' ORDER BY 2,3', [*plan.params, ids, revisions, ids, revisions]).fetchall()
        except QueryCanceled as error:
            raise timeout(error)
        for row in rows:
            found.setdefault(row['message'], []).append(row)
    low_only = 0
    for item in items:
        body, contact, _ = flags.get(item['id'], (False, False, False))
        entries = []
        for row in found.get(item['id'], []):
            entry = {'attachment_id': row['attachment_id'], 'source': row['source']}
            pointer = {'operation': 'get_file_text', 'attachment_id': row['attachment_id'], 'offset': 0,
                       'content': 'extracted' if row['source'] == 'extracted' else 'enriched'}
            quality = ocr_quality(row['extractor'], row['ocr_metadata']) if row['source'] == 'extracted' else None
            if not _readable(c, {'digest': row['digest'], 'bytes': row['bytes']}):
                entry['unreadable'] = True
            elif quality == 'low':
                entry.update(quality='low', read_more=pointer)
            else:
                entry.update(snippet=clip(row['snippet']), model_generated=row['source'] != 'extracted', untrusted=True,
                             read_more=pointer, **({'quality': quality} if quality else {}))
            entries.append(entry)
        sources = {e['source'] for e in entries}
        item['matched_in'] = [name for name, on in (('body', fuzzy_used or body), ('contact', contact),
            ('extracted', 'extracted' in sources), ('visible_text', 'visible_text' in sources),
            ('transcript', 'transcript' in sources)) if on]
        if entries:
            item['media_matches'] = entries
            if not (fuzzy_used or body or contact):
                item['match_type'] = 'media_text'
                if all(e.get('quality') == 'low' for e in entries):
                    low_only += 1
    return low_only


# --- result_evidence: media whose text is not searchable yet (criterion 7) ---------------------------------------

_PROFILE = ("EXISTS(SELECT FROM processing_profiles pp WHERE pp.account=m.account AND pp.enabled AND pp.kind="
            "CASE WHEN " + AUDIO + " THEN 'audio' WHEN a.mime LIKE 'image/%%' THEN 'image' ELSE 'document' END)")
# One verdict per media: NULL (text ready, or nothing to wait for), pending:<stage> or unavailable:<reason>.
_VERDICT = """(CASE
    WHEN jstate IN ('queued','leased','failed') AND error_code='size_limit' THEN 'unavailable:size_limit'
    WHEN jstate='failed' THEN 'unavailable:failed'
    WHEN jstate IN ('queued','leased') THEN CASE WHEN occurrence IN ('collection_excluded','outside_history_bounds')
        THEN 'unavailable:'||occurrence ELSE 'pending:download' END
    WHEN voice THEN CASE WHEN estate='ready' THEN NULL WHEN estate='failed' THEN 'unavailable:failed'
        WHEN estate IN ('queued','leased') OR coalesce(estate,'unconfigured')='unconfigured' AND profile THEN 'pending:transcript'
        WHEN coalesce(estate,'unconfigured')='unconfigured' THEN 'unavailable:unconfigured' END
    WHEN fstate='ready' THEN NULL WHEN fstate='failed' THEN 'unavailable:failed'
    WHEN fstate='unsupported' THEN 'unavailable:unsupported'
    WHEN fstate IN ('queued','leased') OR fstate IS NULL AND has_file THEN 'pending:extraction' END)"""
_STATES = ("f.state AS fstate,e.state AS estate," + AUDIO + " AS voice," + _PROFILE + " AS profile,a.id IS NOT NULL AS has_file")
_JOINS = (" LEFT JOIN attachment_text f ON f.attachment=a.id AND f.source_digest=a.digest AND f.source_revision=m.revision"
          " AND f.source_filename=a.filename AND f.source_mime=a.mime AND f.source_bytes=a.bytes AND f.state<>'superseded'"
          " LEFT JOIN attachment_enrichment e ON e.attachment=a.id AND e.source_digest=a.digest"
          " AND e.source_revision=m.revision AND e.state<>'superseded'")


def pending_evidence(c, clauses, args, extra_join, evidence):
    """On an empty result: media of the search's own scope (every filter, not only the chats) whose text may still
    arrive (media_text_pending) or will not arrive on its own (media_text_unavailable). At most EVIDENCE_CAP per
    group; the optional fields never cost the answer (a savepoint, its own 2 s budget)."""
    where = ' AND '.join(clauses)
    jobs = ('SELECT m.account,m.sent_at,j.state AS jstate,j.error_code,el.occurrence,' + _STATES +
            ' FROM media_jobs j JOIN messages m ON m.id=j.message ' + extra_join + ' JOIN chats ch ON ch.id=m.chat AND ch.account=m.account'
            ' LEFT JOIN attachments a ON a.id=j.id AND a.active ' + ELIGIBILITY_JOIN + _JOINS +
            " WHERE j.state<>'superseded' AND " + where)
    # Fixtures and restored originals may predate media jobs (the decorate rule of result_evidence).
    legacy = ('SELECT m.account,m.sent_at,\'ready\' AS jstate,NULL AS error_code,NULL AS occurrence,' + _STATES +
              ' FROM attachments a JOIN messages m ON m.id=a.message ' + extra_join + _JOINS +
              ' WHERE a.active AND NOT EXISTS(SELECT 1 FROM media_jobs j WHERE j.id=a.id) AND ' + where)
    # Each group (a verdict: stage or reason) is read and capped on its own: a crowd of one kind never hides another.
    sql = ('SELECT account,sent_at,verdict FROM (SELECT *,row_number() OVER (PARTITION BY verdict ORDER BY sent_at DESC) AS n'
           ' FROM (SELECT *,' + _VERDICT + ' AS verdict FROM (' + jobs + ' UNION ALL ' + legacy +
           ') u) v WHERE verdict LIKE %s) w WHERE n<=' + str(EVIDENCE_CAP + 1) + ' ORDER BY sent_at DESC')
    try:
        with c.transaction():
            previous = c.execute("SELECT current_setting('statement_timeout') AS v").fetchone()['v']
            c.execute("SELECT set_config('statement_timeout','2s',true)")
            pending = c.execute(sql, [*args, *args, 'pending:%']).fetchall()
            unavailable = c.execute(sql, [*args, *args, 'unavailable:%']).fetchall()
            c.execute("SELECT set_config('statement_timeout',%s,true)", (previous,))
    except Exception as error:
        log.warning('media_text_evidence_failed exception_type=%s', type(error).__name__)
        return
    if pending:
        evidence['media_text_pending'] = {'count': min(len(pending), EVIDENCE_CAP), 'newest_sent_at': pending[0]['sent_at'],
            **({'capped': True} if len(pending) > EVIDENCE_CAP else {}),
            'next_action': 'search_again_later_or_use_search_files'}
    if unavailable:
        groups = {}
        for row in unavailable:
            group = groups.setdefault(row['verdict'].split(':', 1)[1], {'count': 0, 'accounts': set()})
            group['count'] += 1
            group['accounts'].add(row['account'])
        reasons = []
        for reason, group in groups.items():
            entry = {'reason': reason, 'count': min(group['count'], EVIDENCE_CAP),
                     **({'capped': True} if group['count'] > EVIDENCE_CAP else {})}
            if reason == 'unconfigured':
                entry.update(accounts=sorted(group['accounts']),
                             note='Only the owner can enable transcription for the account; voice notes stay unsearchable until then.')
            reasons.append(entry)
        # Every reason found is listed with its own count (a lower bound past 20); the field caps at 20 as a whole.
        total = sum(r['count'] for r in reasons)
        evidence['media_text_unavailable'] = {'count': min(total, EVIDENCE_CAP), 'reasons': reasons,
            **({'capped': True} if total > EVIDENCE_CAP or any(r.get('capped') for r in reasons) else {})}
