"""ANYM-893: a chat's scheduling signals fold into one negotiation (negotiation_state.evaluate), over synthetic chats."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from messaging_service.negotiation_state import STATES, evaluate
from messaging_service.scheduling_rules import signals
from messaging_service.time_references import owner_zone

FIXTURES = Path(__file__).parent / 'fixtures' / 'scheduling'
CONVERSATIONS = json.loads((FIXTURES / 'conversations.json').read_text(encoding='utf-8'))


def sources_of(conversation):
    zone = owner_zone({'ANYMSG_OWNER_TIMEZONE': conversation['owner_zone']} if conversation['owner_zone'] else {})
    rows = []
    for m in conversation['messages']:
        sent_at = datetime.fromisoformat(m['sent_at'])
        rows.append({'message_id': m['message_id'], 'revision': 1, 'sender': m['sender'], 'is_owner': m['is_owner'],
                     'sent_at': sent_at, 'chat_type': conversation['chat_type'], 'addressed': m['addressed'],
                     'signal': signals(m['text'], sent_at, 'out' if m['is_owner'] else 'in', zone)})
    return rows


def span(entry):return [entry['start'], entry['end']]


def observed(result):
    """The result in the fixture's shape."""
    if result is None:return {'state': None}
    declined = [span(t) for t in result['times'] if t['status'] == 'declined'] + [span(t) for t in result['declined_times']]
    return {'state': result['state'], 'awaiting': result['awaiting'],
            'current': [span(t) for t in result['current_times']],
            'agreed': span(result['agreed_time']) if result['agreed_time'] else None, 'declined': sorted(declined)}


def same_instants(got, expected):
    """Times compare as instants (an offset written differently is the same time)."""
    def norm(value):
        if isinstance(value, list) and value and isinstance(value[0], str):
            return [datetime.fromisoformat(v) for v in value]
        if isinstance(value, list):return [norm(v) for v in value]
        return value
    return {k: norm(v) for k, v in got.items()} == {k: norm(v) for k, v in expected.items()}


# spec: ANYM-893
def test_conversation_fixture_has_the_required_chats():
    ids = [c['id'] for c in CONVERSATIONS]
    assert len(ids) >= 30 and len(set(ids)) == len(ids)
    assert {i.split('-')[0] for i in ids} == {'ru', 'en'}
    states = {m['expect']['state'] for c in CONVERSATIONS for m in c['messages']} | {
        c['final']['expect']['state'] for c in CONVERSATIONS if 'final' in c}
    assert states == set(STATES) | {None}
    for c in CONVERSATIONS:
        assert c['messages'] and all({'sender', 'text', 'sent_at', 'expect'} <= m.keys() for m in c['messages'])
    groups = [c for c in CONVERSATIONS if c['chat_type'] == 'group']
    assert any(len({m['sender'] for m in c['messages']}) >= 3 for c in groups)


# spec: ANYM-893
@pytest.mark.parametrize('conversation', CONVERSATIONS, ids=[c['id'] for c in CONVERSATIONS])
def test_conversation_state_after_every_message(conversation):
    rows = sources_of(conversation)
    for n, message in enumerate(conversation['messages']):
        got = observed(evaluate(rows[:n + 1], rows[n]['sent_at']))
        assert same_instants(got, message['expect']), (message['text'], got)
    if 'final' in conversation:
        got = observed(evaluate(rows, datetime.fromisoformat(conversation['final']['now'])))
        assert same_instants(got, conversation['final']['expect']), got


def by_id(cid):return next(c for c in CONVERSATIONS if c['id'] == cid)


# spec: ANYM-893
def test_the_card_example_agrees_on_wednesday_ten_in_the_owner_zone():
    rows = sources_of(by_id('ru-tue-wed-agreed'))
    result = evaluate(rows, rows[-1]['sent_at'])
    assert result['state'] == 'agreed' and not result['ambiguous']
    assert result['agreed_time']['start'] == '2026-11-11T10:00:00+00:00' and result['agreed_time']['zone'] == 'Europe/Lisbon'
    assert [t['status'] for t in result['times']] == ['countered', 'countered', 'agreed']
    assert result['sources'] == [r['message_id'] for r in rows] and result['trigger_message'] == rows[-1]['message_id']


# spec: ANYM-893
def test_a_rejected_time_is_never_current_and_a_counter_keeps_it_declined():
    rows = sources_of(by_id('ru-decline-and-counter'))
    result = evaluate(rows, rows[-1]['sent_at'])
    tuesday = [t for t in result['times'] if t['start'].startswith('2026-11-10')]
    assert tuesday and all(t['status'] == 'declined' for t in tuesday)
    assert [t['start'][:10] for t in result['current_times']] == ['2026-11-11']


# spec: ANYM-893
def test_a_reschedule_after_agreement_keeps_the_previous_agreed_time():
    rows = sources_of(by_id('ru-reschedule-after-agreed'))
    result = evaluate(rows[:3], rows[2]['sent_at'])
    assert result['state'] == 'counter' and result['rescheduled']
    assert result['previous_agreed_time']['start'] == '2026-11-10T10:00:00+00:00' and result['agreed_time'] is None


# spec: ANYM-893
def test_several_current_times_and_a_plain_accept_agree_with_no_time_and_ambiguous():
    rows = sources_of(by_id('ru-several-times-plain-ok-is-ambiguous'))
    result = evaluate(rows, rows[-1]['sent_at'])
    assert result['state'] == 'agreed' and result['ambiguous'] and result['agreed_time'] is None


# spec: ANYM-893
def test_a_new_negotiation_opens_only_after_a_closed_one():
    rows = sources_of(by_id('ru-new-after-cancel'))
    first, second = evaluate(rows[:2], rows[1]['sent_at']), evaluate(rows, rows[-1]['sent_at'])
    assert first['state'] == 'cancelled' and second['state'] == 'proposed'
    assert second['opened_at'] == rows[2]['sent_at'] and second['sources'] == [rows[2]['message_id']]
    # An open negotiation takes a second proposal as part of itself: no new one opens.
    rows = sources_of(by_id('ru-same-sender-adds-a-time'))
    assert evaluate(rows[:2], rows[1]['sent_at'])['opened_at'] == rows[0]['sent_at']
    # An agreement whose time is not yet 24 h past takes a new proposal as a reschedule.
    rows = sources_of(by_id('ru-reschedule-after-agreed'))
    assert evaluate(rows[:3], rows[2]['sent_at'])['opened_at'] == rows[0]['sent_at']


# Synthetic signals for the properties: a small pool of windows, so times overlap and repeat.
BASE = datetime(2026, 11, 9, 10, tzinfo=timezone.utc)
WINDOWS = [(BASE + timedelta(days=d, hours=h), BASE + timedelta(days=d, hours=h + w))
           for d, h, w in ((1, 0, 1), (1, 0, 4), (2, 2, 1), (9, 0, 1))]


def window(i, polarity):
    start, end = WINDOWS[i]
    return {'start': start.isoformat(), 'end': end.isoformat(), 'granularity': 'time', 'zone': 'UTC',
            'zone_source': 'owner', 'assumed': [], 'polarity': polarity}


SIGNALS = st.builds(
    lambda intent, confidence, medium, times: {'intent': intent, 'confidence': confidence, 'medium': medium,
                                               'times': [window(i, p) for i, p in times]},
    st.sampled_from(('ask_availability', 'propose', 'accept', 'decline', 'cancel', 'reschedule', 'none')),
    st.sampled_from(('low', 'medium', 'high')), st.sampled_from(('call', 'meeting', 'unknown')),
    st.lists(st.tuples(st.integers(0, len(WINDOWS) - 1), st.sampled_from(('offered', 'rejected'))), max_size=3))


@st.composite
def chats(draw, senders=('owner', 'anna', 'boris')):
    """A chat's current sources: distinct messages, each at one revision, minutes apart over up to two weeks."""
    count = draw(st.integers(0, 9))
    chat = draw(st.sampled_from(('private', 'group')))
    minutes = draw(st.lists(st.integers(0, 20_000), min_size=count, max_size=count))
    rows = []
    for n, minute in enumerate(minutes):
        sender = draw(st.sampled_from(senders))
        rows.append({'message_id': f'm{n}', 'revision': draw(st.integers(1, 3)), 'sender': sender,
                     'is_owner': sender == 'owner', 'sent_at': BASE + timedelta(minutes=minute), 'chat_type': chat,
                     'addressed': draw(st.booleans()), 'signal': draw(SIGNALS)})
    return rows


NOW = st.integers(0, 30_000).map(lambda m: BASE + timedelta(minutes=m))


# spec: ANYM-893
@settings(max_examples=300, deadline=None)
@given(rows=chats(), now=NOW, data=st.data())
def test_evaluate_is_deterministic_and_order_insensitive(rows, now, data):
    before = copy.deepcopy(rows)
    result = evaluate(rows, now)
    assert rows == before  # the input is never changed
    assert evaluate(data.draw(st.permutations(rows)), now) == result == evaluate(copy.deepcopy(rows), now)


# spec: ANYM-893
@settings(max_examples=300, deadline=None)
@given(rows=chats(), now=NOW, data=st.data())
def test_removing_a_source_equals_evaluating_the_list_without_it(rows, now, data):
    if not rows:return
    i = data.draw(st.integers(0, len(rows) - 1))
    without = rows[:i] + rows[i + 1:]
    alone = evaluate(copy.deepcopy(without), now)
    evaluate(rows, now)  # an earlier call on the full list leaves nothing behind
    assert evaluate(without, now) == alone
    if alone is not None:assert rows[i]['message_id'] not in alone['sources']


# spec: ANYM-893
@settings(max_examples=300, deadline=None)
@given(rows=chats(), now=NOW, data=st.data())
def test_an_edited_source_equals_evaluating_with_only_its_new_revision(rows, now, data):
    if not rows:return
    i = data.draw(st.integers(0, len(rows) - 1))
    older = dict(rows[i], revision=rows[i]['revision'] - 1, signal=data.draw(SIGNALS),
                 sent_at=rows[i]['sent_at'] - timedelta(minutes=data.draw(st.integers(0, 60))))
    assert evaluate(rows + [older], now) == evaluate(rows, now) == evaluate([older] + rows, now)


# spec: ANYM-893
@settings(max_examples=300, deadline=None)
@given(rows=chats(), now=NOW, data=st.data())
def test_two_copies_of_one_revision_give_the_same_result_in_either_order(rows, now, data):
    # Astra r1 M6: conflicting copies of one message at one revision never let input order decide.
    if not rows:return
    i = data.draw(st.integers(0, len(rows) - 1))
    twin = dict(rows[i], signal=data.draw(SIGNALS))
    assert evaluate(rows + [twin], now) == evaluate([twin] + rows, now)


# spec: ANYM-893
@settings(max_examples=300, deadline=None)
@given(rows=chats(), now=NOW, signal=SIGNALS, opener=st.sampled_from(('propose', 'ask_availability')),
       confidence=st.sampled_from(('medium', 'high')))
def test_an_opening_source_before_all_others_always_gives_a_negotiation(rows, now, signal, opener, confidence):
    # Not vacuous: the properties above would pass an evaluator that always returned None.
    first = {'message_id': 'opener', 'revision': 1, 'sender': 'anna', 'is_owner': False,
             'sent_at': BASE - timedelta(minutes=1), 'chat_type': 'private', 'addressed': False,
             'signal': dict(signal, intent=opener, confidence=confidence)}
    result = evaluate([first] + rows, now)
    # A later negotiation may replace it after a close, but some negotiation always remains.
    assert result is not None and result['state'] in STATES


# spec: ANYM-893
@settings(max_examples=300, deadline=None)
@given(rows=chats(senders=('anna',)) | chats(senders=('owner',)), now=NOW)
def test_agreed_never_results_from_a_single_sender(rows, now):
    result = evaluate(rows, now)
    assert result is None or result['state'] != 'agreed'


# spec: ANYM-893
@settings(max_examples=300, deadline=None)
@given(rows=chats(), now=NOW)
def test_the_state_is_one_of_the_five_or_none(rows, now):
    result = evaluate(rows, now)
    assert result is None or (result['state'] in STATES and result['awaiting'] in ('owner', 'peer', 'others', 'none')
                              and set(result['sources']) <= {r['message_id'] for r in rows})
