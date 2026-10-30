"""Shared PostgreSQL lexical search; SQL identifiers here are internal constants.

PostgreSQL 17 ships russian/english/indonesian Snowball configurations. The
russian configuration also stems ASCII English. Auto tries each complete query
with the same configuration as its vector; it is not language detection.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json

LANGUAGES = ('simple', 'russian', 'english', 'indonesian')
FUNCTIONS = {'all': 'plainto_tsquery', 'phrase': 'phraseto_tsquery', 'web': 'websearch_to_tsquery'}


def invalid(code='invalid_filter'):
    from .store import APIError
    raise APIError(code)


def valid_text(value, maximum=256, *, empty=True):
    if not isinstance(value, str) or len(value)>maximum or '\x00' in value or (not empty and not value):
        invalid()
    return value


def position_text(value):
    """A text field inside a decoded cursor position: any defect is invalid_cursor."""
    if not isinstance(value, str) or not value or len(value)>256 or '\x00' in value:
        invalid('invalid_cursor')
    try: value.encode()
    except UnicodeEncodeError: invalid('invalid_cursor')
    return value


def position_date(value):
    """A timestamp inside a decoded cursor position; the empty string is not a missing bound."""
    from .store import APIError, date_bound
    position_text(value)
    try: return date_bound(value)
    except (APIError, OverflowError): invalid('invalid_cursor')


def filter_values(values):
    if values is None:
        return None
    if not isinstance(values, list) or len(values)>100:
        invalid()
    return sorted(set(valid_text(v, empty=False) for v in values))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def cursor_binding(p, kind, filters):
    return fingerprint({'principal':p['id'], 'scope_version':p['scope_version'], 'kind':kind, 'filters':filters})


def encode_cursor(binding, position):
    return 'q1.'+base64.urlsafe_b64encode(json.dumps({'b':binding,'p':position},
        separators=(',', ':'), default=str).encode()).decode().rstrip('=')


def decode_cursor(value, binding):
    """Opaque, bounded keyset position, never an authorization credential.

    Every caller still applies current ACL and validates any resource anchor.
    Binding prevents accidental reuse under a different query or policy.
    """
    valid_text(value, 4096, empty=False)
    if not value.startswith('q1.'):
        invalid('invalid_cursor')
    try:
        body=json.loads(base64.b64decode(value[3:]+'='*(-len(value[3:])%4), altchars=b'-_', validate=True))
        if not isinstance(body,dict) or set(body)!= {'b','p'} or body['b']!=binding or not isinstance(body['p'],dict):
            raise ValueError
        return body['p']
    except (ValueError, TypeError, UnicodeError, RecursionError):
        invalid('invalid_cursor')


@dataclass(frozen=True)
class SearchPlan:
    query: str
    mode: str
    languages: tuple[str, ...]
    cte: str
    params: list

    @staticmethod
    def vector(alias, language):
        return alias+'.'+('document' if language=='simple' else 'document_'+language)

    def predicate(self, alias, text_sql=None):
        if not self.query:
            return 'true'
        if self.mode=='exact':
            return 'lower('+(text_sql or alias+'.body')+") LIKE lower(q.literal_pattern) ESCAPE E'\\\\'"
        return '('+' OR '.join(self.vector(alias,lang)+' @@ q.'+lang for lang in self.languages)+')'

    def text_predicate(self, text_sql):
        """The query mode's predicate over an unindexed text expression (ANYM-529: contact names)."""
        if self.mode=='exact':
            return self.predicate(None,text_sql)
        return '('+' OR '.join("to_tsvector('"+lang+"',"+text_sql+') @@ q.'+lang for lang in self.languages)+')'

    def rank(self, alias, text_sql=None):
        if not self.query or self.mode=='exact':
            return '0::bigint'
        base='greatest('+','.join('ts_rank_cd('+self.vector(alias,lang)+',q.'+lang+',32)' for lang in self.languages)+')'
        if text_sql is None or self.mode not in ('all','phrase'):
            return '(round(('+base+'*1000000)::numeric))::bigint'
        # Stemming merges e.g. договор/договориться, so cover density alone can
        # rank scattered word forms above the phrase. Tiers dominate: the typed
        # query as a case-insensitive substring (2), its words adjacent in order
        # in any form (1), other matches (0). (tier+[0,1) rank)/3 stays within
        # the 0..1000000 cursor bound.
        phrase=' OR '.join(self.vector(alias,lang)+' @@ q.'+('' if self.mode=='phrase' else 'phrase_')+lang
            for lang in self.languages)
        tier=('(CASE WHEN lower('+text_sql+") LIKE lower(q.literal_pattern) ESCAPE E'\\\\' THEN 2 WHEN "+
            phrase+' THEN 1 ELSE 0 END)')
        return '(round((('+tier+'+'+base+'::float8)/3*1000000)::numeric))::bigint'

    def snippet(self, text_sql):
        if not self.query:
            return 'left('+text_sql+',320)'
        if self.mode=='exact':
            return 'substring('+text_sql+' from greatest(1,strpos(lower('+text_sql+'),lower(q.literal))-100) for 320)'
        # Pick the strongest matching configuration. Markers are plain text;
        # clients must still escape all snippets before displaying them as HTML.
        ranks=["ts_rank_cd(to_tsvector('"+lang+"',"+text_sql+'),q.'+lang+',32)' for lang in self.languages]
        best='greatest('+','.join(ranks)+')' if len(ranks)>1 else ranks[0]
        choices=[]
        for lang,rank in zip(self.languages,ranks):
            choices.append('WHEN '+rank+'='+best+" THEN ts_headline('"+lang+"',"+text_sql+',q.'+lang+
                ", 'MaxWords=45,MinWords=15,MaxFragments=1,StartSel=[[,StopSel=]]')")
        return '(CASE '+' '.join(choices)+' ELSE left('+text_sql+',320) END)'

    def match_type(self, alias):
        if not self.query:
            return "'history'::text"
        if self.mode=='exact':
            return "'exact'::text"
        return '(CASE '+' '.join('WHEN '+self.vector(alias,lang)+' @@ q.'+lang+" THEN '"+
            ('simple' if lang=='simple' else 'morphology_'+lang)+"'" for lang in self.languages)+" END)"


def compile_query(query='', query_mode='all', search_language='auto'):
    if not isinstance(query,str) or len(query)>200 or '\x00' in query:
        invalid('invalid_query')
    if not isinstance(query_mode,str) or query_mode not in (*FUNCTIONS,'exact'):
        invalid('invalid_query')
    if not isinstance(search_language,str) or search_language not in ('auto',*LANGUAGES):
        invalid('invalid_query')
    languages=LANGUAGES if search_language=='auto' else (search_language,)
    columns=['%s::text AS literal','%s::text AS literal_pattern']
    pattern='%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
    params=[query,pattern]
    if query_mode!='exact':
        for lang in languages:
            columns.append(FUNCTIONS[query_mode]+"('"+lang+"',%s) AS "+lang); params.append(query)
    if query_mode=='all':
        for lang in languages:
            columns.append("phraseto_tsquery('"+lang+"',%s) AS phrase_"+lang); params.append(query)
    # This CTE is referenced by filtering, ranking and presentation. A shared
    # materialized row hides constant tsqueries from GIN index conditions and
    # can turn a selective LIMIT query into a date-ordered archive traversal.
    return SearchPlan(query,query_mode,languages,'search_query AS NOT MATERIALIZED (SELECT '+','.join(columns)+')',params)


def output_options(compact=False, body_chars=None, include_coverage=True):
    if type(compact) is not bool or type(include_coverage) is not bool:
        invalid()
    if body_chars is not None and (type(body_chars) is not int or not 0<=body_chars<=20000):
        invalid()
    return 320 if compact and body_chars is None else body_chars


def shape_messages(rows, *, compact=False, body_chars=None):
    for row in rows:
        if isinstance(row.get('formatting'),dict):
            # Native spans remain tied to the complete captured body, even when
            # the caller requests a shorter body or a compact snippet.
            row['formatting']={**row['formatting'],'offsets_reference':'full_provider_body_utf16'}
        body=row.get('body')
        if body is not None and body_chars is not None:
            row['body_truncated']=len(body)>body_chars
            row['body']=body[:body_chars]
        if 'contact' in row:
            # ANYM-529: no block is "not captured", never "the card had no number".
            if row['contact'] is None:
                row.pop('contact')
                if row.get('message_kind')=='contact' and not compact:row['contact_details']='unavailable'
        if compact:
            if body is not None:row.setdefault('snippet',body[:320])
            row.pop('body',None)
        # Never return generated index vectors or internal cursor values.
        for key in list(row):
            if key=='document' or key.startswith('document_'):
                row.pop(key)
    return rows


def coverage(store,c,p,include=True):
    data=store.dataset(c,p)
    return data if include else {'coverage':data['coverage'],'synthetic':data['synthetic']}
