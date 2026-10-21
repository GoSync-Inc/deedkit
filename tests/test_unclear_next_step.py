"""ANYM-454: every unclear conversation block names a next step; WhatsApp catch-up shows its progress."""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from test_conversation_state import NOW, dialogue_archive, message, state  # noqa: F401
from test_whatsapp import wa  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
CATCHUP = {'history': {'gaps': ['catchup_pending'], 'state': 'gaps'}}


def documented_table():
    text = (ROOT / 'CONVERSATION.md').read_text()
    section = text[text.index('## Why a row is unclear'):]
    section = section.split('\n## ', 1)[0]
    return dict(re.findall(r'^\| `([a-z_]+)` \| .*\| `([a-z_]+)` \|$', section, re.M))


def described_table():
    import inspect
    from messaging_service import app
    source = inspect.getsource(app)
    start = source.index('Compact rows omit')
    legend = source[start:source.index('limit defaults', start)]
    pairs = {}
    for sentence in re.split(r'\.\s+', ' '.join(legend.split())):
        match = re.fullmatch(r'next_step: (.+) -> ([a-z_]+)', sentence.strip().rstrip('.'))
        if match:
            for code in match[1].split(', '):
                if code != 'any other code':
                    pairs[code] = match[2]
    return pairs


def drive_every_branch():
    """Basis codes emitted by derive_state for every branch that can end unclear."""
    seen = set()

    def add(result):
        seen.add(result['basis']['code'])
        if 'owner_attention' in result:
            seen.add(result['basis']['code'])
        return result

    incoming = message('in', 'incoming')
    add(state([]))
    add(state([message('a', 'incoming'), message('b', 'outgoing')]))
    add(state([message('a', 'incoming', body='', kind='unknown')]))
    add(state([incoming], truncated=True))
    add(state([incoming], chat_type='stranger'))
    add(state([message('x', 'outgoing', evidence={'revision_changed_during_read': True})]))
    add(state([message('x', 'outgoing', evidence={'active_conflicts': 1})]))
    add(state([incoming], history={'last_head_scan_at': None}))
    add(state([incoming], history={'last_head_scan_at': NOW + timedelta(minutes=1)}))
    add(state([incoming], history={'last_head_scan_at': NOW - timedelta(minutes=6)}))
    add(state([incoming], history={'gaps': ['catchup_gap']}))
    from messaging_service.collector_state import CHAT_SCAN_ERRORS
    for code in CHAT_SCAN_ERRORS:
        add(state([incoming], history={'last_error_code': code}))
    add(state([incoming], chat_type='channel'))
    # Group: owner attention with every uncertainty the private branch has.
    target = message('t', 'incoming', attention_status='open')
    tied = message('u', 'incoming', attention_status='ambiguous')
    add(state([incoming], chat_type='group', attention=[target]))
    add(state([incoming], chat_type='group', attention=[tied]))
    add(state([incoming], chat_type='group', attention=[target], history={'gaps': ['catchup_gap']}))
    add(state([incoming], chat_type='group', attention=[message('v', 'incoming',
        evidence={'active_conflicts': 1})]))
    add(state([incoming], chat_type='group', attention=[]))
    return seen


# spec: ANYM-454
def test_every_unclear_basis_is_documented():
    from messaging_service.conversation_state import NEXT_STEP
    emitted = drive_every_branch()
    unclear_only = emitted - {'last_archived_message_incoming', 'last_archived_message_outgoing',
        'channel_or_self_chat', 'no_observed_owner_attention', 'explicit_owner_attention'}
    assert unclear_only <= set(NEXT_STEP), unclear_only - set(NEXT_STEP)
    from messaging_service.collector_state import CHAT_SCAN_ERRORS
    assert set(CHAT_SCAN_ERRORS) <= set(NEXT_STEP)
    assert documented_table() == NEXT_STEP
    assert described_table() == NEXT_STEP
    catalog_text = (ROOT / 'services/gateway/src/agent_gateway/catalog.json').read_text()
    for code, step in NEXT_STEP.items():
        assert code in catalog_text and step in catalog_text
    assert set(NEXT_STEP.values()) == {'answer_with_caveat', 'reread_now', 'wait_for_provider_cooldown',
        'check_provider_access', 'get_context', 'wait_for_history'}


TABLE_CASES = [
    ([], {}, 'no_archived_messages', 'wait_for_history'),
    ([message('a', 'incoming'), message('b', 'outgoing')], {}, 'ambiguous_message_order', 'answer_with_caveat'),
    ([message('a', 'incoming', body='', kind='unknown')], {}, 'unclassified_latest_message', 'get_context'),
    ([message('x', 'outgoing', evidence={'revision_changed_during_read': True})], {},
        'message_revision_changed_during_read', 'reread_now'),
    ([message('x', 'outgoing', evidence={'active_conflicts': 1})], {}, 'message_version_conflict', 'reread_now'),
    ([message('x', 'outgoing')], {'history': {'last_head_scan_at': None}}, 'provider_head_not_verified',
        'answer_with_caveat'),
    ([message('x', 'outgoing')], {'history': {'last_head_scan_at': NOW - timedelta(minutes=6)}},
        'provider_head_stale', 'answer_with_caveat'),
    ([message('x', 'outgoing')], {'history': {'last_head_scan_at': NOW + timedelta(minutes=1)}},
        'provider_head_not_verified', 'answer_with_caveat'),
    ([message('x', 'outgoing')], {'history': {'gaps': ['catchup_pending']}}, 'history_has_gaps',
        'answer_with_caveat'),
    ([message('x', 'outgoing')], {'history': {'last_error_code': 'rate_limited'}}, 'rate_limited',
        'wait_for_provider_cooldown'),
    ([message('x', 'outgoing')], {'history': {'last_error_code': 'provider_temporarily_unavailable'}},
        'provider_temporarily_unavailable', 'wait_for_provider_cooldown'),
    ([message('x', 'outgoing')], {'history': {'last_error_code': 'provider_unavailable'}}, 'provider_unavailable',
        'check_provider_access'),
    ([message('x', 'outgoing')], {'history': {'last_error_code': 'provider_chat_error'}}, 'provider_chat_error',
        'check_provider_access'),
    ([message('x', 'outgoing')], {'chat_type': 'stranger'}, 'chat_type_unknown', 'get_context'),
]


# spec: ANYM-454
@pytest.mark.parametrize('messages,changes,code,step', TABLE_CASES)
def test_unclear_row_names_next_step(messages, changes, code, step):
    result = state(messages, **changes)
    assert result['suggested_action'] == 'unclear' and result['basis']['code'] == code
    assert result['next_step'] == step


# spec: ANYM-454
def test_unclear_truncated_head_names_next_step_and_assured_rows_have_none():
    assert state([message('a', 'incoming')], truncated=True)['next_step'] == 'answer_with_caveat'
    for result in (state([message('a', 'incoming')]), state([message('a', 'outgoing')]),
            state([message('a', 'incoming')], chat_type='channel')):
        assert result['suggested_action'] != 'unclear' and 'next_step' not in result


# spec: ANYM-454
def test_group_unclear_attention_carries_next_step_on_both_blocks():
    target = message('t', 'incoming', attention_status='open')
    tied = message('u', 'incoming', attention_status='ambiguous')
    result = state([message('h', 'incoming')], chat_type='group', attention=[tied])
    assert result['basis']['code'] == 'ambiguous_attention_reply_order'
    assert result['next_step'] == result['owner_attention']['next_step'] == 'answer_with_caveat'
    stale = state([message('h', 'incoming')], chat_type='group', attention=[target],
        history={'last_error_code': 'provider_unavailable'})
    assert stale['next_step'] == stale['owner_attention']['next_step'] == 'check_provider_access'
    clear = state([message('h', 'incoming')], chat_type='group', attention=[target])
    assert 'next_step' not in clear and 'next_step' not in clear['owner_attention']
    quiet = state([message('h', 'incoming')], chat_type='group', attention=[])
    assert 'next_step' not in quiet and 'next_step' not in quiet['owner_attention']


# spec: ANYM-454
def test_unclear_row_keeps_next_step_when_compact_omits_the_basis():
    from messaging_service.presentation import lean_conversation, lean_queue_conversation
    result = state([message('x', 'outgoing')], history={'last_error_code': 'provider_unavailable'})
    last = {'id': 'x'}
    hint = {'message_id': 'x', 'confidence': 'provider_unavailable'}
    compact = lean_queue_conversation(result, last, hint)
    assert 'basis' not in compact and compact['suggested_action'] == 'unclear'
    assert compact['next_step'] == 'check_provider_access'
    assert lean_conversation(result)['next_step'] == 'check_provider_access'
    group = state([message('h', 'incoming')], chat_type='group',
        attention=[message('u', 'incoming', attention_status='ambiguous')])
    lean = lean_conversation(group)
    assert lean['next_step'] == lean['owner_attention']['next_step'] == 'answer_with_caveat'
    queue = lean_queue_conversation(group, {'id': 'h'}, {'message_id': 'u'})
    assert queue['next_step'] == 'answer_with_caveat' and 'next_step' not in queue['owner_attention']
    assert 'next_step' not in lean_conversation(state([message('x', 'outgoing')]))


# spec: ANYM-454
def test_unknown_basis_code_falls_back_without_failing_the_queue():
    from messaging_service.presentation import lean_queue_conversation
    from messaging_service.collector_state import CHAT_SCAN_ERRORS
    assert 'provider_synthetic_new_error' not in CHAT_SCAN_ERRORS
    result = state([message('x', 'outgoing')], history={'last_error_code': 'provider_synthetic_new_error'})
    assert result['basis']['code'] == 'provider_synthetic_new_error'
    assert result['next_step'] == 'get_context'
    assert lean_queue_conversation(result, {'id': 'x'}, None)['next_step'] == 'get_context'


def retry_state(seconds, **changes):
    return state([message('x', 'outgoing')], history={'last_error_code': 'rate_limited'},
        collector={'status': 'degraded', 'detail_code': 'rate_limited',
            'cooldown_until': NOW + timedelta(seconds=seconds)}, **changes)


# spec: ANYM-454
def test_rate_limited_row_gives_retry_after_only_while_the_cooldown_runs():
    from messaging_service.presentation import lean_conversation
    result = retry_state(120)
    assert result['next_step'] == 'wait_for_provider_cooldown' and result['retry_after_s'] == 120
    assert lean_conversation(result)['retry_after_s'] == 120
    assert 'retry_after_s' not in retry_state(-5)
    other = state([message('x', 'outgoing')], history={'last_error_code': 'provider_unavailable'},
        collector={'status': 'degraded', 'detail_code': 'rate_limited',
            'cooldown_until': NOW + timedelta(seconds=90)})
    assert 'retry_after_s' not in other


def catchup(progress=None, *, attempt=None, platform='whatsapp', collector=None, history=None):
    marker = {} if progress is None else {'catchup_progress_at': progress.timestamp()}
    return state([message('x', 'outgoing')], platform=platform, collector={**marker, **(collector or {})},
        history={**CATCHUP['history'], 'last_attempt_at': attempt, **(history or {})}).get('catchup')


# spec: ANYM-454
def test_catchup_row_states():
    moving = catchup(NOW - timedelta(minutes=10), attempt=NOW - timedelta(minutes=1))
    assert moving == {'state': 'moving', 'last_progress_at': NOW - timedelta(minutes=10)}
    # Recent scans without a moved cursor past the window: the chat is stuck.
    old = NOW - timedelta(minutes=45)
    stalled = catchup(old, attempt=NOW - timedelta(minutes=1))
    assert stalled == {'state': 'stalled', 'last_progress_at': old}
    # No attempt on this chat since its last progress: waiting its turn.
    assert catchup(old, attempt=old - timedelta(minutes=5)) == {'state': 'queued', 'last_progress_at': old}
    assert catchup(old, attempt=None) == {'state': 'queued', 'last_progress_at': old}
    # No marker: rows from before the deploy.
    assert catchup(None, attempt=NOW - timedelta(minutes=1)) == {'state': 'unknown', 'last_progress_at': None}
    assert catchup(None, attempt=None) == {'state': 'queued', 'last_progress_at': None}
    # A rate-limited account is stuck whatever its attempts say.
    limited = catchup(old, attempt=old - timedelta(minutes=5), collector={'status': 'degraded',
        'detail_code': 'rate_limited', 'cooldown_until': NOW + timedelta(seconds=60)})
    assert limited == {'state': 'stalled', 'last_progress_at': old, 'cause': 'rate_limited'}
    chat_error = catchup(old, attempt=NOW, history={'last_error_code': 'provider_unavailable'})
    assert chat_error['state'] == 'stalled' and chat_error['cause'] == 'provider_unavailable'
    # A recent marker beats an error: pages still moved.
    recent = catchup(NOW - timedelta(minutes=2), attempt=NOW, history={'last_error_code': 'provider_unavailable'})
    assert recent['state'] == 'moving' and 'cause' not in recent


# spec: ANYM-454
def test_catchup_appears_only_for_a_pending_whatsapp_catchup():
    assert catchup(NOW, platform='telegram') is None
    assert catchup(NOW, history={'gaps': ['catchup_gap']}) is None
    assert catchup(NOW, history={'gaps': []}) is None
    group = state([message('h', 'incoming')], chat_type='group', attention=[],
        history=CATCHUP['history'], collector={'catchup_progress_at': NOW.timestamp()})
    assert group['catchup']['state'] == 'moving'


# spec: ANYM-454
def test_catchup_shows_beside_coverage_in_compact_rows():
    from messaging_service.presentation import lean_conversation
    result = state([message('x', 'outgoing')], history=CATCHUP['history'],
        collector={'catchup_progress_at': (NOW - timedelta(minutes=3)).timestamp()})
    lean = lean_conversation(result)
    assert lean['catchup'] == {'state': 'moving', 'last_progress_at': NOW - timedelta(minutes=3)}
    assert lean['coverage']['gaps'] == ['catchup_pending']
    assert 'catchup' not in lean_conversation(state([message('x', 'outgoing')]))


# spec: ANYM-454
@pytest.mark.parametrize('marker', ['soon', True, float('nan'), float('inf'), -1, {}, [1], 10**400,
    (NOW + timedelta(days=1)).timestamp()])
def test_malformed_catchup_marker_reads_as_absent(marker):
    result = state([message('x', 'outgoing')], history={**CATCHUP['history'], 'last_attempt_at': NOW},
        collector={'catchup_progress_at': marker})
    assert result['catchup'] == {'state': 'unknown', 'last_progress_at': None}


# spec: ANYM-454
def test_next_step_failure_omits_the_field(monkeypatch):
    from messaging_service import conversation_state

    class Broken(dict):
        def get(self, *args):
            raise RuntimeError('synthetic')

        def __getitem__(self, key):
            raise RuntimeError('synthetic')

    monkeypatch.setattr(conversation_state, 'NEXT_STEP', Broken(conversation_state.NEXT_STEP))
    result = state([message('x', 'outgoing')], history={'last_error_code': 'provider_unavailable'})
    assert result['suggested_action'] == 'unclear' and 'next_step' not in result
    group = state([message('h', 'incoming')], chat_type='group',
        attention=[message('u', 'incoming', attention_status='ambiguous')])
    assert 'next_step' not in group and 'next_step' not in group['owner_attention']

    def boom(*args, **kwargs):
        raise RuntimeError('synthetic')

    monkeypatch.setattr(conversation_state, 'catchup_state', boom)
    broken = state([message('x', 'outgoing')], history=CATCHUP['history'])
    assert 'catchup' not in broken and broken['basis']['code'] == 'history_has_gaps'
    monkeypatch.setattr(conversation_state, 'retry_after', boom)
    assert 'retry_after_s' not in retry_state(30)


# spec: ANYM-454
def test_choice_of_action_and_basis_is_unchanged_by_the_new_fields():
    plain = state([message('x', 'outgoing')], history={'gaps': ['catchup_pending'], 'last_error_code': 'rate_limited'})
    rich = state([message('x', 'outgoing')], history={'gaps': ['catchup_pending'], 'last_error_code': 'rate_limited'},
        collector={'catchup_progress_at': NOW.timestamp(), 'status': 'degraded', 'detail_code': 'rate_limited',
            'cooldown_until': NOW + timedelta(seconds=9)})
    for key in ('suggested_action', 'basis', 'last_turn', 'head_freshness', 'coverage'):
        assert plain[key] == rich[key]


# spec: ANYM-454
def test_sync_chat_records_catchup_progress_when_the_cursor_moves(wa):
    from test_whatsapp import event
    w, s, key = wa
    asyncio.run(w.sync_once())
    first = w.state.progress('123@c.us', 'user')
    assert 'catchup_progress_at' not in first
    w.provider.messages.extend(event(i) for i in range(6, 13))
    asyncio.run(w.sync_once())
    started = w.state.progress('123@c.us', 'user')
    assert started['catchup_before'] and type(started['catchup_progress_at']) is float
    asyncio.run(w.sync_once())
    moved = w.state.progress('123@c.us', 'user')
    assert moved['catchup_before'] < started['catchup_before']
    assert moved['catchup_progress_at'] >= started['catchup_progress_at']
    # Same (database) clock as the attempt stamp: a pass stamps its progress after it began.
    assert moved['catchup_progress_at'] >= moved['last_attempt_at']
    for _ in range(6):
        asyncio.run(w.sync_once())
    done = w.state.progress('123@c.us', 'user')
    assert not done.get('catchup_before') and done['catchup_progress_at'] >= moved['catchup_progress_at']


# spec: ANYM-454
def test_conversation_state_reads_the_catchup_marker_and_account_cooldown(dialogue_archive):
    s, key, add, query, catalog = dialogue_archive
    chat = add('m1', 'incoming', chat='4915@c.us')
    now = datetime.now(timezone.utc)
    with s.pool.connection() as c:
        c.execute('''INSERT INTO whatsapp_checkpoints(account,provider_chat,progress)
            VALUES('dialogue-wa','4915@c.us',%s) ON CONFLICT(account,provider_chat) DO UPDATE SET progress=%s''',
            [Jsonb({'head': [1, 'a'], 'catchup_before': [1, 'b'],
                'catchup_progress_at': (now - timedelta(minutes=5)).timestamp(),
                'last_attempt_at': now.timestamp()})] * 2)
    result = query('dialogue-wa', chat)
    assert result['catchup']['state'] == 'moving'
    assert abs((result['catchup']['last_progress_at'] - (now - timedelta(minutes=5))).total_seconds()) < 1
    with s.pool.connection() as c:
        c.execute('''UPDATE whatsapp_checkpoints SET progress=progress-'catchup_progress_at'
            WHERE account='dialogue-wa' ''')
        c.execute('''INSERT INTO collector_health(account,status,detail_code,cooldown_until)
            VALUES('dialogue-wa','degraded','rate_limited',clock_timestamp()+interval '90 seconds')
            ON CONFLICT(account) DO UPDATE SET status='degraded',detail_code='rate_limited',
            cooldown_until=excluded.cooldown_until''')
        c.execute('''UPDATE collector_chats SET last_error_code='rate_limited',last_error_at=clock_timestamp()
            WHERE account='dialogue-wa' AND provider_chat='4915@c.us' ''')
    limited = query('dialogue-wa', chat)
    assert limited['catchup'] == {'state': 'unknown', 'last_progress_at': None}
    assert limited['next_step'] == 'wait_for_provider_cooldown'
    assert 60 < limited['retry_after_s'] <= 90


# spec: ANYM-454
@pytest.mark.parametrize('savepoint', [True, False])
def test_failed_health_read_leaves_the_transaction_usable_and_keeps_the_marker(dialogue_archive, savepoint):
    from contextlib import nullcontext
    from messaging_service.conversation_state import collector_facts
    import psycopg

    class FailingHealth:
        def __init__(self, c):
            self.c = c

        def execute(self, sql, *args):
            # A real server-side error: the query reaches PostgreSQL (no placeholders, so no parameters).
            return self.c.execute('SELECT 1/0') if 'collector_health' in sql else self.c.execute(sql, *args)

        def transaction(self):
            return self.c.transaction() if savepoint else nullcontext()

    s, *_ = dialogue_archive
    with s.pool.connection() as c:
        c.execute('SELECT 1')  # the caller's transaction is already open, as in the queue
        row = {'wa_progress': {'catchup_progress_at': 5.0}}
        facts = collector_facts(FailingHealth(c), {'chat_ids': None}, 'dialogue-wa', row)
        assert facts == {'catchup_progress_at': 5.0}
        if savepoint:
            assert c.execute('SELECT 1 AS one').fetchone()['one'] == 1
        else:  # without the savepoint the failed statement poisons the transaction (the r1 B1 defect)
            with pytest.raises(psycopg.errors.InFailedSqlTransaction):
                c.execute('SELECT 1')
        c.rollback()


# spec: ANYM-454
def test_chat_restricted_reader_gets_no_account_wide_health(dialogue_archive):
    from messaging_service.conversation_state import collector_facts
    s, *_ = dialogue_archive
    with s.pool.connection() as c:
        c.execute("""INSERT INTO collector_health(account,status,detail_code,cooldown_until)
            VALUES('dialogue-wa','degraded','rate_limited',clock_timestamp()+interval '90 seconds')
            ON CONFLICT(account) DO UPDATE SET status='degraded',detail_code='rate_limited',
            cooldown_until=excluded.cooldown_until""")
        row = {'wa_progress': {'catchup_progress_at': 7.0}}
        assert collector_facts(c, {'chat_ids': ['some-chat']}, 'dialogue-wa', row) == {'catchup_progress_at': 7.0}
        open_facts = collector_facts(c, {'chat_ids': None}, 'dialogue-wa', row)
        assert open_facts['status'] == 'degraded' and open_facts['catchup_progress_at'] == 7.0
