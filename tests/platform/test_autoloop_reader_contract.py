"""The normal reader's documented output contract matches `checked_answer`, and each refusal names its predicate."""
import json

import pytest

from scripts import autoloop

triage = autoloop.triage
P1, P2, P3 = ('33333333-3333-4333-8333-%012d' % n for n in (1, 2, 3))
LEGACY = f'card_brief only with new_card or an actionable accepted primary, at most {autoloop.BRIEF_LIMIT} characters'
SNAPSHOT = {'main': [{'ref': 'ANYM-10', 'title': 'Participants of a group chat', 'status': 'Active', 'text': '',
                      'sources': []},
                     {'ref': 'ANYM-11', 'title': 'Contact cards', 'status': 'Done', 'text': '', 'sources': []},
                     {'ref': 'ANYM-12', 'title': 'Read receipts opt-in', 'status': 'Done', 'text': '', 'sources': []}],
            'branches': {}}
SPECS = triage.known(SNAPSHOT)
SAFE = 'Group participants vanish from the list after a sync.'
ACCEPTED = {'specs': [{'ref': 'ANYM-10', 'verdict': 'accepted'}], 'primary': 'ANYM-10'}


def item(i, description, category='bug', minute=0):
    return {'id': i, 'category': category, 'description': description, 'operation': None, 'principal': 'agent-x',
            'key_id': 'k1', 'created_at': f'2026-11-16T10:{minute:02d}:00+00:00'}


INBOX = {P1: item(P1, 'members drop out of a group'), P2: item(P2, 'how do I list feedback?', 'other', 1),
         P3: item(P3, 'read receipts do not arrive', 'bug', 2)}


def decision(i, classification='feature', **extra):
    return dict({'id': i, 'classification': classification, 'reason_code': 'matches-card'}, **extra)


@pytest.fixture
def states(tmp_path):
    t, a = tmp_path / 'triage', tmp_path / 'autoloop'
    t.mkdir()
    triage.save(t / 'snapshot.json', SNAPSHOT)
    triage.save(t / 'inbox.json', INBOX)
    triage.save(t / 'candidates.json', [{'id': i, 'candidates': []} for i in INBOX])
    return t, a


def stream(*decisions):
    text = json.dumps({'decisions': list(decisions)})
    init = json.loads((autoloop.Path(__file__).parent / 'fixtures/autoloop/init-2.1.316.json').read_text())
    events = [init, {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}},
              {'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1, 'result': text}]
    return '\n'.join(json.dumps(e) for e in events) + '\n'


def read(states, *decisions, projection=True):
    t, a = states
    if projection:
        triage.save(a / 'config.json', {'record_projection': True})
    return autoloop.read(a, t, runner=lambda _: stream(*decisions))[0]


def normal_prompt():
    return autoloop.SYSTEM.split('If mode is brief_repair')[0]


# spec: ANYM-991
def test_the_normal_reader_prompt_states_every_checked_brief_predicate():
    text = ' '.join(normal_prompt().split())
    assert f'At most {autoloop.BRIEF_LIMIT} characters' in text
    assert 'required for' in text  # projection recording refuses an actionable item without a released brief
    for word in ('question', 'not-actionable', 'already-live', 'enable-only', 'duplicate', 'declined'):
        assert word in text.split('card_brief:')[1].split('No other field.')[0]
    assert f'{("", "one", "two", "three", "four", "five")[autoloop.VERBATIM]} consecutive words' in text
    assert "any item's text" in text  # the copy check reads every input item, not only the decided one
    assert 'already-live: exactly one spec, a Done spec on main, accepted, with reason_code already-live' in text


# spec: ANYM-991
def test_already_live_with_another_reason_code_is_refused_not_recorded_as_work(states):
    t, a = states
    live = decision(P1, 'already-live', specs=[{'ref': 'ANYM-11', 'verdict': 'accepted'}], primary='ANYM-11')
    checks = {}
    out, briefs, refused = autoloop.sorted_answer([live], INBOX, SPECS, {i: 'k1' for i in INBOX}, checks)
    assert out == [] and briefs == {} and checks == {P1: 'already_live_reason'}
    assert refused[P1] == f'{P1}: already-live needs reason_code already-live'
    assert read(states, live) == 0 and triage.load(t / 'ledger.json', {}) == {}
    assert autoloop.refusals(a)[P1]['check'] == 'already_live_reason'
    # The truthful form is recorded and needs no brief under projection recording.
    assert read(states, live | {'reason_code': 'already-live'}) == 1
    entry = triage.load(t / 'ledger.json', {})[P1]
    assert entry['reason'] == autoloop.REASONS['already-live'] and triage.load(a / 'briefs.json', {}) == {}


# spec: ANYM-991
@pytest.mark.parametrize('bad, check', [
    (decision(P2, 'question', reason_code='question-answered', card_brief=SAFE), 'card_brief_classification'),
    (decision(P1, 'not-actionable', reason_code='unclear', card_brief=SAFE), 'card_brief_classification'),
    (decision(P1, 'enable-only', reason_code='needs-opt-in', specs=[{'ref': 'ANYM-12', 'verdict': 'accepted'}],
              primary='ANYM-12', card_brief=SAFE), 'card_brief_classification'),
    (decision(P1, 'already-live', reason_code='already-live', specs=[{'ref': 'ANYM-11', 'verdict': 'accepted'}],
              primary='ANYM-11', card_brief=SAFE), 'card_brief_classification'),
    (decision(P1, reason_code='duplicate', duplicate_of=P3, card_brief=SAFE, **ACCEPTED), 'card_brief_duplicate'),
    (decision(P1, 'reported-bug', specs=[{'ref': 'ANYM-10', 'verdict': 'declined'}], primary='ANYM-10',
              card_brief=SAFE), 'card_brief_no_accepted_primary'),
    (decision(P1, 'reported-bug', card_brief=SAFE), 'card_brief_no_accepted_primary'),
    (decision(P1, reason_code='new-idea', new_card='member-drop', card_brief='x' * 601), 'card_brief_length'),
    (decision(P1, card_brief='x' * 601, **ACCEPTED), 'card_brief_length'),
], ids=['question', 'not-actionable', 'optin', 'already-live', 'duplicate', 'declined', 'unmatched', 'new-card-long',
        'accepted-long'])
def test_a_brief_refusal_names_its_failed_predicate_and_keeps_the_legacy_wording(states, bad, check):
    t, a = states
    with pytest.raises(autoloop.Refused) as error:
        autoloop.checked_answer([bad], INBOX, SPECS)
    assert str(error.value) == f"{bad['id']}: {LEGACY}" and error.value.check == check
    assert read(states, bad) == 0 and triage.load(t / 'ledger.json', {}) == {}
    entry = autoloop.refusals(a)[bad['id']]
    assert set(entry) == {'count', 'at', 'why', 'check'} and entry['why'] == f"{bad['id']}: {LEGACY}"
    assert entry['check'] == check


# spec: ANYM-991
@pytest.mark.parametrize('brief, check', [
    (None, 'card_brief_missing'),
    ('Users see that read receipts do not arrive at all.', 'card_brief_copied'),  # five words of another item
    ('/private/file `shell` https://private.invalid', 'card_brief_unsafe'),
    ('', 'card_brief_unsafe'),
], ids=['missing', 'copied', 'unsafe', 'empty'])
def test_an_actionable_item_without_a_released_brief_names_why(states, brief, check):
    t, a = states
    before = triage.load(t / 'inbox.json', {})
    for d in (decision(P1, 'reported-bug', card_brief=brief, **ACCEPTED),
              decision(P1, reason_code='new-idea', new_card='member-drop', card_brief=brief)):
        assert read(states, d) == 0 and triage.load(t / 'inbox.json', {}) == before
        entry = autoloop.refusals(a)[P1]
        assert entry['why'] == f'{P1}: actionable projection needs a released card_brief' and entry['check'] == check
    assert triage.load(t / 'ledger.json', {}) == {} and triage.load(a / 'briefs.json', {}) == {}


# spec: ANYM-991
def test_accepted_primary_and_new_card_briefs_are_released_together(states):
    t, a = states
    count = read(states, decision(P1, 'reported-bug', card_brief=SAFE, **ACCEPTED),
                 decision(P3, reason_code='new-idea', new_card='receipt-delivery', card_brief=SAFE))
    briefs = triage.load(a / 'briefs.json', {})
    assert count == 2 and autoloop.refusals(a) == {}
    assert briefs[P1] == {'new_card': None, 'card_brief': SAFE, 'release': autoloop.RELEASE, 'primary': 'ANYM-10'}
    assert briefs[P3] == {'new_card': 'receipt-delivery', 'card_brief': SAFE, 'release': autoloop.RELEASE}


# spec: ANYM-991
@pytest.mark.parametrize('bad, why', [
    ({'decisions': [decision(P1, new_card='member-drop', card_brief=SAFE, **ACCEPTED)]}, 'new_card only'),
    ({'decisions': [decision(P1, reason_code='owner-approved', card_brief=SAFE, **ACCEPTED)]}, 'outside the enum'),
    ({'decisions': [decision(P1, card_brief=SAFE, owner_action='enable', **ACCEPTED)]}, 'unknown field'),
], ids=['wrong-routing', 'owner', 'unknown'])
def test_routing_owner_and_unknown_refusals_carry_no_brief_check(states, bad, why):
    with pytest.raises(autoloop.Refused, match=why) as error:
        autoloop.checked_answer(bad['decisions'], INBOX, SPECS)
    assert getattr(error.value, 'check', None) is None
    t, a = states
    checks = {}
    if why == 'unknown field':
        with pytest.raises(autoloop.Refused, match=why):
            autoloop.sorted_answer(bad['decisions'], INBOX, SPECS, {i: 'k1' for i in INBOX}, checks)
        assert checks == {}
        return
    assert read(states, *bad['decisions']) == 0
    assert set(autoloop.refusals(a)[P1]) == {'count', 'at', 'why'}


# spec: ANYM-991
def test_old_defaults_and_old_refusal_entries_keep_their_behaviour(states):
    t, a = states
    # The default config does not record projections: an actionable answer without a brief is recorded as before.
    assert read(states, decision(P1, 'reported-bug', **ACCEPTED), projection=False) == 1
    assert autoloop.sorted_answer([decision(P2, 'question', reason_code='question-answered')], INBOX, SPECS,
                                  {i: 'k1' for i in INBOX})[2] == {}  # the four-argument call still works
    triage.save(a / 'refused.json', {P3: {'count': 2, 'at': 'old', 'why': 'old'}})
    assert autoloop.parked(a, {P3: INBOX[P3]}) == {P3: 2}
