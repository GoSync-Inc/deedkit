"""DK-51: a projection read's decision that is valid in every way but its brief's length gets ONE brief-only repair
from the same reader, charged under the same daily caps; anything else keeps the normal refusal."""
from datetime import datetime, timezone
import json

import pytest

import scripts.autoloop as autoloop
from test_autoloop import D1, NOW, configure, run_tick, submission
from test_autoloop import loop as loop  # the tick fixture, re-exported explicitly
from test_autoloop_projection import Recording

triage = autoloop.triage
INIT = json.loads((autoloop.Path(__file__).parent / 'fixtures/autoloop/init-2.1.316.json').read_text())
P1, P2, P3, P4 = ('44444444-4444-4444-8444-%012d' % n for n in (1, 2, 3, 4))
AT = datetime(2026, 11, 16, 11, 0, tzinfo=timezone.utc)
CANARY = 'canary-dk51-model-output'
SNAPSHOT = {'main': [{'ref': 'ANYM-10', 'title': 'Participants of a group chat', 'status': 'Active', 'text': '',
                      'sources': []},
                     {'ref': 'ANYM-11', 'title': 'Contact cards', 'status': 'Done', 'text': '', 'sources': []},
                     {'ref': 'ANYM-12', 'title': 'Read receipts opt-in', 'status': 'Done', 'text': '', 'sources': []}],
            'branches': {}}
SPECS = triage.known(SNAPSHOT)
ACCEPTED = {'specs': [{'ref': 'ANYM-10', 'verdict': 'accepted'}], 'primary': 'ANYM-10'}
# The reader's own words, too long: the only failure. It copies no five words of any item.
LONG = ' '.join(['Group participants vanish from the visible member list whenever the archive refreshes,'] * 8)
SHORT = 'Group participants vanish from the visible member list whenever the archive refreshes.'
OTHER = 'Opening a saved contact card shows an empty page instead of its details.'


def item(i, description, category='bug', minute=0):
    return {'id': i, 'category': category, 'description': description, 'operation': None, 'principal': 'agent-x',
            'key_id': 'k1', 'created_at': f'2026-11-16T10:{minute:02d}:00+00:00'}


INBOX = {P1: item(P1, 'members drop out of a group'), P2: item(P2, 'contact card opens blank', minute=1),
         P3: item(P3, 'how do I list feedback?', 'other', 2), P4: item(P4, 'read receipts do not arrive', minute=3)}


def decision(i, classification='reported-bug', brief=LONG, **extra):
    return dict({'id': i, 'classification': classification, 'reason_code': 'matches-card', 'card_brief': brief},
                **dict(ACCEPTED, **extra))


def stream(text, extra=()):
    events = [INIT, {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}, *extra,
              {'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1, 'result': text}]
    return '\n'.join(json.dumps(e) for e in events) + '\n'


class Reader:
    """The pinned reader's stand-in: the normal answer first, then each brief_repair reply in turn."""

    def __init__(self, decisions, *repairs):
        self.decisions, self.repairs, self.calls = list(decisions), list(repairs), []

    def __call__(self, projected):
        self.calls.append(projected)
        if projected.get('mode') != 'brief_repair':
            return stream(json.dumps({'decisions': self.decisions}))
        reply = self.repairs.pop(0)
        return reply(projected) if callable(reply) else reply

    @property
    def modes(self):
        return [call.get('mode') for call in self.calls]


def repaired(i, brief=SHORT, **extra):
    return stream(json.dumps(dict({'id': i, 'card_brief': brief}, **extra)))


@pytest.fixture
def states(tmp_path):
    t, a = tmp_path / 'triage', tmp_path / 'autoloop'
    t.mkdir()
    a.mkdir(mode=0o700)
    triage.save(t / 'snapshot.json', SNAPSHOT)
    triage.save(t / 'inbox.json', INBOX)
    triage.save(t / 'candidates.json', [{'id': i, 'candidates': []} for i in INBOX])
    triage.save(a / 'config.json', {'live': True, 'record_projection': True, 'principals': ['agent-x']})
    return t, a


def read(states, reader, ids=(P1,)):
    t, a = states
    return autoloop.read(a, t, reader, list(ids), AT)


def day(a):
    return triage.load(a / 'ledger.json', {}).get('days', {}).get('2026-11-16', {})


def files(*roots):
    return ''.join(p.read_text() for root in roots for p in root.rglob('*.json'))


def assert_unrecorded(states, refusals_before=0):
    t, a = states
    assert P1 in triage.load(t / 'inbox.json', {}) and P1 not in triage.load(t / 'ledger.json', {})
    assert P1 not in triage.load(a / 'briefs.json', {}) and P1 not in triage.load(a / autoloop.PROJECTION, {})
    entry = autoloop.refusals(a)[P1]
    assert entry['count'] == refusals_before + 1 and entry['check'] == 'card_brief_length'


# spec: ANYM-988
def test_a_selected_read_records_the_same_decision_after_one_charged_brief_only_repair(states, monkeypatch):
    t, a = states
    triage.save(a / 'refused.json', {P1: {'count': 3, 'at': 'old', 'why': 'old', 'check': 'card_brief_length'}})
    monkeypatch.setattr(triage, 'export_pages', lambda *args: ([dict(INBOX[P1], status='received')], None))
    monkeypatch.setattr(autoloop, 'SYNC_STATE', t.parent / 'sync')
    reader = Reader([decision(P1)], repaired(P1))
    assert autoloop.read_selected(a, t, P1, runner=reader, now=AT)[0] == 1
    # Two calls of the same reader: the normal decision, then a brief-only paraphrase of that item alone.
    assert reader.modes == [None, 'brief_repair']
    assert reader.calls[1] == {'mode': 'brief_repair', 'max_characters': autoloop.BRIEF_LIMIT,
                               'max_consecutive_source_words': autoloop.VERBATIM - 1, 'items': reader.calls[0]['items']}
    assert day(a)['reader_runs'] == 2 and day(a)['items'] == {P1: 'agent-x'}  # both charged; the item counts once
    # Exactly the decision a first answer with a short brief would have given, through normal triage.
    expected, _ = autoloop.checked_answer([decision(P1, brief=SHORT)], INBOX, SPECS)
    assert triage.load(a / 'decisions.json', []) == expected
    entry = triage.load(t / 'ledger.json', {})[P1]
    assert entry['primary'] == 'ANYM-10' and entry['disposition']['status'] == 'accepted' and P1 not in \
        triage.load(t / 'inbox.json', {})
    assert triage.load(a / 'briefs.json', {})[P1] == {'new_card': None, 'card_brief': SHORT, 'release': 1,
                                                      'primary': 'ANYM-10'}
    queued = triage.load(a / autoloop.PROJECTION, {})[P1]
    assert queued['released_brief'] == SHORT and queued['primary_spec'] == 'ANYM-10'
    assert queued['reader_pin'] == autoloop.READER_PIN
    # History stays; the journal holds IDs, times and fixed words only, never the overlong brief.
    assert autoloop.refusals(a)[P1]['count'] == 3
    assert triage.load(a / autoloop.LENGTH_REPAIRS, {}) == {P1: {'at': triage.stamp(AT), 'attempts': 1,
                                                                 'result': 'released'}}
    assert 'whenever the archive refreshes, Group' not in files(a, t)


# spec: ANYM-988
@pytest.mark.parametrize('bad', [
    decision(P1, classification='question', specs=[], primary=None),
    decision(P1, specs=[{'ref': 'ANYM-99', 'verdict': 'accepted'}], primary='ANYM-99'),
    decision(P1, specs=[{'ref': 'ANYM-10', 'verdict': 'declined'}, {'ref': 'ANYM-11', 'verdict': 'accepted'}]),
    decision(P1, reason_code='already-live'),
    decision(P1, reason_code='needs-opt-in'),
    decision(P1, classification='enable-only', reason_code='needs-opt-in',
             specs=[{'ref': 'ANYM-12', 'verdict': 'accepted'}], primary='ANYM-12'),
    decision(P1, reason_code='duplicate', duplicate_of=P2),
    decision(P1, new_card='member-drop'),
    decision(P1, brief=None),
    decision(P1, brief=LONG + ' members drop out of a group'),
    decision(P1, brief=' '.join(['/private/path `shell`'] * 60)),
    'held-card', 'held-item',
], ids=['decision', 'ref', 'primary', 'reason', 'optin-reason', 'enable-only', 'duplicate', 'newcard', 'missing',
        'copied', 'unsafe', 'held-card', 'held-item'])
def test_only_a_decision_valid_but_for_its_brief_length_gets_the_repair(states, bad):
    t, a = states
    if isinstance(bad, str):
        triage.save(a / 'holds.json', {'cards': {'ANYM-10': 'held'} if bad == 'held-card' else {},
                                       'items': {P1: 'held'} if bad == 'held-item' else {}})
    reader = Reader([decision(P1) if isinstance(bad, str) else bad], repaired(P1))
    assert autoloop.read(a, t, reader, [P1], AT, held_cards=('ANYM-10',) if bad == 'held-card' else ())[0] == 0
    assert reader.modes == [None] and not (a / autoloop.LENGTH_REPAIRS).exists() and not day(a)
    assert P1 in triage.load(t / 'inbox.json', {}) and triage.load(t / 'ledger.json', {}) == {}
    assert P1 in autoloop.refusals(a)


# spec: ANYM-988
@pytest.mark.parametrize('text', [stream('{"decisions": ['), stream('{"decisions": []}', extra=[{'type': 'user'}])],
                         ids=['json', 'stream'])
def test_an_unknown_normal_answer_refuses_the_run_with_no_repair(states, text):
    t, a = states
    calls = []
    with pytest.raises(autoloop.Refused):
        autoloop.read(a, t, lambda p: calls.append(p) or text, [P1], AT)
    assert len(calls) == 1 and not (a / autoloop.LENGTH_REPAIRS).exists() and P1 in triage.load(t / 'inbox.json', {})


# spec: ANYM-988
@pytest.mark.parametrize('used, caps, repairs', [
    ({'reader_runs': 1, 'items': {P1: 'agent-x'}}, {'reader_runs': 1}, 0),
    ({'reader_runs': 0, 'items': {P2: 'agent-x'}}, {'items_per_principal': 1}, 0),
    ({'reader_runs': 1, 'items': {P1: 'agent-x'}}, {'reader_runs': 2, 'items_per_principal': 1}, 1),
], ids=['reader-cap', 'principal-cap', 'within-caps'])
def test_the_second_call_is_charged_first_under_the_same_daily_caps(states, used, caps, repairs):
    t, a = states
    triage.save(a / 'config.json', {'live': True, 'record_projection': True, 'principals': ['agent-x'], 'caps': caps})
    triage.save(a / 'ledger.json', {'days': {'2026-11-16': dict({'card': 0, 'build': 0, 'seconds': 0, 'refunds': {}},
                                                                **used)}})
    reader = Reader([decision(P1)], repaired(P1))
    assert read(states, reader)[0] == repairs
    assert reader.modes == [None] + ['brief_repair'] * repairs
    assert day(a)['reader_runs'] == used['reader_runs'] + repairs  # never refunded, never a new budget
    if not repairs:
        assert_unrecorded(states)
        assert not (a / autoloop.LENGTH_REPAIRS).exists()


# spec: ANYM-988
@pytest.mark.parametrize('reply, result', [
    (repaired(P1, 'x' * 601), 'invalid_brief'),
    (repaired(P1, CANARY + ' and then members drop out of a group'), 'card_brief_copied'),
    (repaired(P1, '/private/' + CANARY + ' `shell`'), 'card_brief_unsafe'),
    (repaired(P2, CANARY), 'foreign_id'),
    (repaired(P1, CANARY, note='x'), 'extra_fields'),
    (repaired(P1, CANARY, primary='ANYM-11'), 'extra_fields'),
    (repaired(P1, None), 'missing_brief'),
    (stream(json.dumps({'id': P1, 'refusal': 'unsafe_source'})), 'unsafe_source'),
    (stream(CANARY + ' not json'), 'invalid_json'),
    (stream(json.dumps({'id': P1, 'card_brief': CANARY}), extra=[{'type': 'user', 'message': {}}]), 'reader'),
    (stream(json.dumps({'decisions': [decision(P1, brief=SHORT)]})), 'extra_fields'),
], ids=['length', 'copied', 'unsafe', 'foreign-id', 'unknown-field', 'changed-decision', 'missing', 'refusal',
        'json', 'stream', 'second-decision'])
def test_a_bad_repair_reply_keeps_the_normal_refusal_and_records_nothing(states, reply, result):
    t, a = states
    triage.save(a / 'refused.json', {P1: {'count': 3, 'at': 'old', 'why': 'old'}, P4: {'count': 1, 'at': 'x', 'why': 'y'}})
    reader = Reader([decision(P1)], reply)
    assert read(states, reader)[0] == 0 and reader.modes == [None, 'brief_repair']
    assert_unrecorded(states, 3)
    assert autoloop.refusals(a)[P1]['why'].endswith(f'at most {autoloop.BRIEF_LIMIT} characters')
    assert autoloop.refusals(a)[P4] == {'count': 1, 'at': 'x', 'why': 'y'}
    assert day(a)['reader_runs'] == 1  # the second call stays charged
    assert triage.load(a / autoloop.LENGTH_REPAIRS, {})[P1]['result'] == result
    assert CANARY not in files(a, t)


# spec: ANYM-988
def test_a_source_or_hold_change_during_the_repair_records_nothing(states):
    t, a = states
    def change_source(projected):
        triage.save(t / 'inbox.json', dict(INBOX, **{P1: dict(INBOX[P1], key_id='k2')}))
        return repaired(P1)
    reader = Reader([decision(P1)], change_source)
    assert read(states, reader)[0] == 0 and triage.load(t / 'ledger.json', {}) == {}
    assert triage.load(a / autoloop.LENGTH_REPAIRS, {})[P1]['result'] == 'source_changed'
    triage.save(t / 'inbox.json', INBOX)
    def hold_card(projected):
        triage.save(a / 'holds.json', {'cards': {'ANYM-10': 'owner hold'}, 'items': {}})
        return repaired(P1)
    reader = Reader([decision(P1)], hold_card)
    assert read(states, reader)[0] == 0 and triage.load(t / 'ledger.json', {}) == {}
    assert triage.load(a / autoloop.LENGTH_REPAIRS, {})[P1] == {'at': triage.stamp(AT), 'attempts': 2,
                                                                'result': 'source_changed'}
    assert autoloop.refusals(a)[P1]['count'] == 2


class Killed(BaseException):
    pass


# spec: ANYM-988
def test_an_interrupted_repair_is_settled_as_refused_and_never_sent_again(states, monkeypatch):
    t, a = states
    def killed(projected):
        raise Killed()
    with pytest.raises(Killed):
        read(states, Reader([decision(P1)], killed))
    assert triage.load(a / autoloop.LENGTH_REPAIRS, {})[P1]['result'] is None and day(a)['reader_runs'] == 1
    assert triage.load(t / 'ledger.json', {}) == {} and not (a / 'refused.json').exists()
    # A restarted selected read settles the unknown attempt and stops: no charge, no call, no replay.
    monkeypatch.setattr(triage, 'export_pages', lambda *args: ([dict(INBOX[P1], status='received')], None))
    monkeypatch.setattr(autoloop, 'SYNC_STATE', t.parent / 'sync')
    calls = []
    with pytest.raises(autoloop.Refused, match='never replay'):
        autoloop.read_selected(a, t, P1, runner=lambda p: calls.append(p), now=AT)
    assert calls == [] and day(a)['reader_runs'] == 1
    assert triage.load(a / autoloop.LENGTH_REPAIRS, {})[P1]['result'] == 'interrupted'
    assert_unrecorded(states)
    # A later normal read sees a settled attempt: the item gets an ordinary new read like any refused one.
    reader = Reader([decision(P1, brief=SHORT)])
    assert read(states, reader)[0] == 1 and reader.modes == [None]


# spec: ANYM-988
def test_an_unknown_attempt_is_left_out_of_a_batch_read_and_a_recorded_one_is_settled_as_recorded(states):
    t, a = states
    question = decision(P3, classification='question', reason_code='question-answered', specs=[], primary=None,
                        brief=None)
    assert read(states, Reader([question]), ids=(P3,))[0] == 1  # recorded, then its attempt left unknown
    triage.save(a / autoloop.LENGTH_REPAIRS, {P1: {'at': 'x', 'attempts': 1, 'result': None},
                                              P3: {'at': 'x', 'attempts': 1, 'result': None}})
    reader = Reader([decision(P2, brief=OTHER)])
    assert read(states, reader, ids=(P1, P2))[0] == 1
    assert [row['id'] for row in reader.calls[0]['items']] == [P2]
    attempts = triage.load(a / autoloop.LENGTH_REPAIRS, {})
    assert attempts[P1]['result'] == 'interrupted' and attempts[P3]['result'] == 'recorded'
    assert autoloop.refusals(a)[P1]['check'] == 'card_brief_length' and P3 not in autoloop.refusals(a)


# spec: ANYM-988
def test_a_batch_repairs_each_item_at_most_once_and_leaves_the_others_as_they_were(states):
    t, a = states
    config = (a / 'config.json').read_bytes()
    triage.save(a / 'refused.json', {P4: {'count': 1, 'at': 'old', 'why': 'old'}})
    good = decision(P2, brief=OTHER)
    refused = decision(P3, classification='question', specs=[], primary=None, brief=SHORT)
    second = decision(P4, brief=LONG, specs=[{'ref': 'ANYM-10', 'verdict': 'accepted'}])
    reader = Reader([decision(P1), good, refused, second], repaired(P1),
                    repaired(P4, 'x' * 601))  # P4's one repair fails; it is not asked again
    assert read(states, reader, ids=(P1, P2, P3, P4))[0] == 2
    assert reader.modes == [None, 'brief_repair', 'brief_repair']
    assert [c['items'][0]['id'] for c in reader.calls[1:]] == [P1, P4] and day(a)['reader_runs'] == 2
    assert set(triage.load(t / 'ledger.json', {})) == {P1, P2}
    briefs = triage.load(a / 'briefs.json', {})
    assert briefs[P1]['card_brief'] == SHORT and briefs[P2]['card_brief'] == OTHER
    journal = autoloop.refusals(a)
    assert journal[P3]['check'] == 'card_brief_classification' and journal[P4]['count'] == 2
    assert journal[P4]['check'] == 'card_brief_length' and P1 not in journal and P2 not in journal
    assert (a / 'config.json').read_bytes() == config
    items = autoloop.projection_items(a, AT)
    assert {i: item['reader_pin'] for i, item in items.items()} == {P1: autoloop.READER_PIN, P2: autoloop.READER_PIN}


# spec: ANYM-988
def test_a_good_first_answer_and_a_legacy_read_make_no_repair_call(states):
    t, a = states
    reader = Reader([decision(P1, brief=SHORT)])
    assert read(states, reader)[0] == 1 and reader.modes == [None] and not (a / autoloop.LENGTH_REPAIRS).exists()
    triage.save(t / 'inbox.json', INBOX)
    triage.save(t / 'ledger.json', {})
    triage.save(a / 'config.json', {'live': True, 'record_projection': False, 'principals': ['agent-x']})
    reader = Reader([decision(P2, brief=LONG)], repaired(P2))
    assert read(states, reader, ids=(P2,))[0] == 0 and reader.modes == [None]
    assert autoloop.refusals(a)[P2]['check'] == 'card_brief_length' and not (a / autoloop.LENGTH_REPAIRS).exists()


# spec: ANYM-988
def test_the_hourly_tick_charges_both_calls_and_publishes_the_repaired_brief(loop):
    t, a, s = loop
    configure(a, live=True, record_projection=True)
    fakes = Recording([submission(D1, 'members drop out of a group')])
    calls = []
    def runner(projected):
        calls.append(projected.get('mode'))
        if projected.get('mode') == 'brief_repair':
            return repaired(D1)
        return stream(json.dumps({'decisions': [{'id': D1, 'classification': 'reported-bug', 'reason_code': 'new-idea',
                                                 'new_card': 'member-list', 'card_brief': LONG}]}))
    fakes.runner = runner
    code, _ = run_tick(loop, fakes)
    assert code == 0 and calls == [None, 'brief_repair']
    today = triage.load(a / 'ledger.json', {})['days'][NOW.date().isoformat()]
    assert today['reader_runs'] == 2 and list(today['items']) == [D1]
    assert triage.load(a / 'plan.json', {})['today']['reader_runs'] == 2
    (sent,) = fakes.records
    (projected,) = json.loads(sent['stdin'])['projections']
    assert projected['released_brief'] == SHORT and projected['new_card'] == 'member-list'
    assert triage.load(t / 'ledger.json', {})[D1]['cluster'] == 'new:member-list'
