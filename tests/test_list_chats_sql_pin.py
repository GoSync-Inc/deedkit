"""W5-6: list_chats sends the same statements, text and parameters byte for byte, as today's frozen builder.

A recording connection answers every statement with synthetic rows and stops at the page statement. The live function
and tests/list_chats_legacy.py run on the same filter combination and the same synthetic archive state; their traces
(every statement with its parameters, the page plan's call and the cursor binding, then the outcome or refusal) must be
equal. No database, no provider.
"""
import json
from datetime import datetime, timezone

from hypothesis import HealthCheck, event, given, settings, strategies as st
from psycopg.types.json import Jsonb

from messaging_service import chat_queries as cq
import list_chats_legacy as legacy

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
MANY = [f'c{i}' for i in range(200)]


class PageStatement(Exception):
    """The trace ends at the page statement."""


def canon(value):
    if isinstance(value, Jsonb):return ['Jsonb', json.dumps(value.obj, sort_keys=True, default=str)]
    if isinstance(value, (list, tuple)):return [canon(v) for v in value]
    if isinstance(value, dict):return {k: canon(v) for k, v in value.items()}
    return f'{type(value).__name__}:{value!r}'


class Result:
    def __init__(self, rows):self.rows = rows
    def fetchone(self):return self.rows[0] if self.rows else None
    def fetchall(self):return self.rows
    def __iter__(self):return iter(self.rows)


class Recorder:
    def __init__(self, env):self.env, self.trace = env, []

    def execute(self, sql, params=None):
        self.trace.append(['sql', sql, canon(params)])
        env = self.env
        if 'awaiting_reply_confidence' in sql:raise PageStatement()
        if 'probe_metadata' in sql:return Result([{'value': str(env['schema'])}])
        if 'CURRENT_TIMESTAMP AS t' in sql:return Result([{'t': NOW}])
        if 'FROM reply_snoozes WHERE key_id=%s) AS x' in sql:return Result([{'x': env['snoozes']}])
        if 'FROM reply_mutes WHERE principal=%s) AS x' in sql:return Result([{'x': env['mutes']}])
        if 'owner_message_reads' in sql and ' AS x' in sql:return Result([{'x': env['read_sources']}])
        if 'SELECT DISTINCT a.platform' in sql:return Result([{'k': 'telegram|owner', 'account': 'acc1'}])
        if sql.startswith('SELECT c.id FROM chats c WHERE c.id=ANY'):return Result([{'id': 'L'}] if env['alias_match'] else [])
        if sql.startswith('SELECT 1 FROM chats c WHERE c.id=%s'):return Result([{'?column?': 1}])
        if 'waits_totals t' in sql:return Result([{'totals': {'listed': 1}, 'page': [{'chat_id': 'c1'}]}])
        if 'count(*) AS n' in sql:return Result([{'n': 3, 'unknown': 1}])
        return Result([])


def pairs(env):
    if not env['aliases']:return {}, {}
    return {'L': 'P', 'P': 'L'}, {'P': {'chat': {'id': 'L', 'provider_chat_id': 'l@lid', 'title': 'L', 'last_activity': None,
        'unread_count': None}, 'provenance': 'synthetic'}}


def run(fn, env, principal, kw):
    c = Recorder(env)
    def alias_pairs(accounts, c_, p, chats=None):
        c.trace.append(['alias_pairs', canon(accounts), canon(chats)]);return pairs(env)
    def page_first(c_, p, clauses, args, **k):
        c.trace.append(['page_first', list(clauses), canon(args), canon(k)])
        if not env['page_found']:return None
        aliases, listed = pairs(env)
        return {'found': ['c1', 'c2'], 'excluded': ['L'] if aliases else [], 'aliases': aliases, 'listed': listed,
            'proof': 'NOT EXISTS(SELECT 1 FROM messages pm WHERE pm.sent_at>=%s)', 'proof_args': [NOW, 'acc1']}
    def decode(after, binding):
        c.trace.append(['cursor', after, binding]);return env['pos']
    saved = cq._alias_pairs, cq._page_first, cq.decode_cursor, legacy.decode_cursor
    cq._alias_pairs, cq._page_first, cq.decode_cursor, legacy.decode_cursor = alias_pairs, page_first, decode, decode
    try:
        fn(None, c, dict(principal), **kw)
        outcome = ['returned']
    except PageStatement:outcome = ['page']
    except Exception as e:outcome = ['raised', type(e).__name__, repr(getattr(e, 'args', ()))]
    finally:cq._alias_pairs, cq._page_first, cq.decode_cursor, legacy.decode_cursor = saved
    return [*c.trace, outcome]


common = dict(
    query=st.sampled_from(['', 'ab', '@a_%\\x', '@']), chat_types=st.sampled_from([None, ['private', 'group']]),
    accounts=st.sampled_from([None, ['acc1']]), exclude_channels=st.booleans(),
    exclude_chats=st.sampled_from([None, ['L', 'x'], ['P']]), my_roles=st.sampled_from([None, None, ['admin', 'member'], []]),
    can_post=st.sampled_from([None, None, True, False]),
    from_time=st.sampled_from(['', '', '2026-01-01T00:00:00Z']), to_time=st.sampled_from(['', '', '2026-11-01T00:00:00Z']),
    awaiting_reply_days=st.sampled_from([0, 14]), selected_chat=st.sampled_from([None, None, 'P']),
    view=st.sampled_from(['archive', 'conversation']), limit=st.sampled_from([1, 25]), after=st.sampled_from(['', 'q1.x']),
    only_unread=st.booleans(), only_questions=st.booleans(), include_dismissed=st.booleans())
modes = {
    'catalog': dict(order=st.sampled_from(['id', 'activity', 'last_activity_desc']),
        exclude_senders=st.sampled_from([None, None, ['s1']]), only_unread=st.just(False), only_questions=st.just(False)),
    'queue': dict(awaiting_reply=st.just(True), order=st.sampled_from(['id', 'activity', 'priority']),
        exclude_likely_broadcasts=st.booleans(), include_muted=st.booleans(),
        promotional=st.sampled_from(['exclude', 'include', 'only']), courtesies=st.sampled_from(['include', 'exclude', 'only']),
        exclude_senders=st.sampled_from([None, ['s1']])),
    'waits': dict(awaiting_their_reply=st.just(True), order=st.just('waiting_longest'),
        min_wait_hours=st.sampled_from([None, 0, 6]), include_muted=st.booleans(), only_unread=st.just(False),
        from_time=st.just(''), to_time=st.just(''), my_roles=st.just(None), can_post=st.just(None)),
    # Every filter drawn on its own: the refusals are pinned too.
    'raw': dict(awaiting_reply=st.booleans(), awaiting_their_reply=st.booleans(),
        order=st.sampled_from(['id', 'activity', 'priority', 'waiting_longest', 'bogus']),
        exclude_likely_broadcasts=st.booleans(), include_muted=st.booleans(), min_wait_hours=st.sampled_from([None, 6]),
        promotional=st.sampled_from(['exclude', 'include', 'only']), courtesies=st.sampled_from(['include', 'exclude', 'only']),
        exclude_senders=st.sampled_from([None, ['s1']])),
}
requests = st.one_of(*(st.fixed_dictionaries({**common, **extra}) for extra in modes.values()))
envs = st.fixed_dictionaries(dict(
    schema=st.sampled_from([82, 45]), read_sources=st.booleans(), snoozes=st.booleans(), mutes=st.booleans(),
    aliases=st.booleans(), alias_match=st.booleans(), page_found=st.booleans(),
    pos=st.sampled_from([{'id': 'c1', 'at': '2026-11-01T00:00:00+00:00'}, {'id': 'c1', 'at': None},
        {'id': 'c1', 'at': '2026-11-01T00:00:00+00:00', 'score': 12, 'as_of': '2026-11-09T00:00:00+00:00'}])))
principals = st.fixed_dictionaries(dict(
    id=st.just('principal-1'), key_id=st.just('00000000-0000-4000-8000-000000000001'), scope_version=st.just(1),
    accounts=st.sampled_from([['acc1'], ['acc1', 'acc2']]), chat_ids=st.sampled_from([None, ['c1'], MANY])))


# spec: ANYM-878
@settings(max_examples=1500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(kw=requests, env=envs, principal=principals)
def test_list_chats_statements_match_the_frozen_builder(kw, env, principal):
    live = run(cq._list_chats, env, principal, kw)
    old = run(legacy.legacy_list_chats, env, principal, kw)
    event(f'outcome: {old[-1][0]}{":" + old[-1][1] if old[-1][0] == "raised" else ""}')
    assert live == old
