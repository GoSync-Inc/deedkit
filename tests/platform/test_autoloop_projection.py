"""The autoloop records the reader's decisions and released briefs in DeedKit, behind a config flag (ANYM-988)."""
from datetime import timedelta
import json
from pathlib import Path
import re

import pytest

import scripts.autoloop as autoloop
from messaging_service import feedback_projection
from test_autoloop import (  # the fixtures and helpers of the tick tests; `x as x` re-exports a fixture explicitly
    D1, NOW, P1, P2, P3, Fakes, answer, configure, decision, run_read, run_tick, stream, submission, triage)
from test_autoloop import loop as loop, states as states

RECORDED = json.dumps({'recorded': 1, 'unchanged': 0})
PRIVATE = 'canary-private-description-7f3a'
BRIEF = 'A synthetic problem in the readers own words'
ROOT = Path(__file__).resolve().parents[2]


class Recording(Fakes):
    """The export over ssh as Fakes has it, and the operator container's `record-feedback-projection`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records, self.record_error = [], None

    def ssh(self, host, argv, stdin=None):
        if 'record-feedback-projection' in argv:
            self.records.append({'argv': argv, 'stdin': stdin})
            if self.record_error:
                raise triage.Refused(self.record_error)
            return RECORDED
        return super().ssh(host, argv)


def reader_answers(fakes):
    def runner(projected):
        fakes.runner_calls.append([row['id'] for row in projected['items']])
        return stream(answer(*[decision(row['id'], 'reported-bug', reason_code='new-idea', new_card='synthetic-card',
            card_brief=BRIEF) for row in projected['items']]))
    fakes.runner = runner


# spec: ANYM-988
def test_the_closed_sets_are_the_readers_and_the_migrations():
    assert feedback_projection.CLASSES == autoloop.CLASSES
    assert feedback_projection.REASONS == tuple(autoloop.REASONS)
    assert feedback_projection.NEW_CARD.pattern == autoloop.NEW_CARD.pattern
    assert feedback_projection.BRIEF_LIMIT == autoloop.BRIEF_LIMIT
    assert autoloop.SPEC_REF.pattern == feedback_projection.SPEC_REF.pattern
    migration = (ROOT / 'services/messaging/messaging_service/migrations/094_feedback_projections.sql').read_text()
    for word in (*autoloop.CLASSES, *autoloop.REASONS):
        assert "'%s'" % word in migration


# spec: ANYM-988
def test_flag_off_the_tick_makes_no_call_and_writes_no_projection(loop):
    t, a, s = loop
    configure(a, live=True)
    fakes = Recording([submission(D1, PRIVATE)])
    reader_answers(fakes)
    code, _ = run_tick(loop, fakes)
    assert code == 0 and fakes.runner_calls == [[D1]]
    assert fakes.records == [] and not (a / 'projection.json').exists()
    assert 'projection' not in triage.load(a / 'plan.json', {})
    assert not any('record-feedback-projection' in ' '.join(call[1]) for call in fakes.calls)


# spec: ANYM-988
def test_flag_on_the_decisions_and_released_briefs_go_to_the_command_on_stdin_and_no_text(loop):
    t, a, s = loop
    configure(a, live=True, record_projection=True)
    fakes = Recording([submission(D1, PRIVATE)])
    reader_answers(fakes)
    code, _ = run_tick(loop, fakes)
    assert code == 0 and len(fakes.records) == 1
    (sent,) = fakes.records
    assert sent['argv'][:3] == ['docker', 'run', '--rm'] and '-i' in sent['argv'][:5]
    assert sent['argv'][-6:-1] == ['python', '-m', 'messaging_service', 'record-feedback-projection', '--file'] \
        and sent['argv'][-1] == '/dev/stdin'
    assert 'deedkit-feedback-projection' in sent['argv'] and PRIVATE not in ' '.join(sent['argv'])
    document = json.loads(sent['stdin'])
    assert set(document) == {'projections'} and len(document['projections']) == 1
    (item,) = document['projections']
    assert item == {'feedback_id': D1, 'classification': 'reported-bug', 'reason_code': 'new-idea',
        'new_card': 'synthetic-card', 'released_brief': autoloop.released(BRIEF, {}), 'released_at': triage.stamp(NOW),
        'reader_pin': autoloop.READER_PIN}
    assert item['released_brief']
    assert PRIVATE not in sent['stdin']
    # The server's own validation accepts what the controller sends.
    assert feedback_projection.projection_errors(document) == []
    assert triage.load(a / 'plan.json', {})['projection'] == 'recorded 1, unchanged 0'
    assert triage.load(a / 'projection.json', None) == {}
    # A later tick with nothing new decided sends nothing.
    run_tick(loop, fakes, now=NOW + timedelta(hours=1))
    assert len(fakes.records) == 1


# spec: ANYM-988
def test_a_failed_call_never_fails_the_tick_and_the_next_tick_sends_the_same_item_again(loop):
    t, a, s = loop
    configure(a, live=True, record_projection=True)
    fakes = Recording([submission(D1, PRIVATE)])
    reader_answers(fakes)
    fakes.record_error = 'ssh exited 1'
    code, _ = run_tick(loop, fakes)
    assert code == 0 and triage.load(a / 'plan.json', {})['projection'] == 'projection: ssh exited 1'
    assert triage.load(t / 'ledger.json', {})[D1]['classification'] == 'reported-bug'  # the reader's own record stands
    first = json.loads(fakes.records[0]['stdin'])
    assert list(triage.load(a / 'projection.json', {})) == [D1]
    fakes.record_error = None
    code, _ = run_tick(loop, fakes, now=NOW + timedelta(hours=1))
    assert code == 0 and triage.load(a / 'plan.json', {})['projection'] == 'recorded 1, unchanged 0'
    # The retried item is the same item, with the release time of its decision, so a landed first call stays a no-op.
    assert json.loads(fakes.records[1]['stdin']) == first and triage.load(a / 'projection.json', None) == {}


# spec: ANYM-988
def test_a_brief_the_server_would_refuse_is_recorded_as_none_and_never_holds_the_others_back(states):
    t, a = states
    run_read(states, decision(P1, 'reported-bug', reason_code='new-idea', new_card='group-receipts', card_brief='Receipts stop.'),
        decision(P2, 'reported-bug', reason_code='new-idea', new_card='group-names', card_brief='Names are lost.'),
        decision(P3, 'question', reason_code='question-answered'))
    briefs = triage.load(a / 'briefs.json', {})
    briefs[P1]['card_brief'] = 'two\nlines'                    # a control character
    briefs[P2]['release'] = autoloop.RELEASE - 1               # stored before the current checks
    triage.save(a / 'briefs.json', briefs)
    items = autoloop.projection_items(a, NOW)
    assert set(items) == {P1, P2, P3}
    assert all('released_brief' not in item for item in items.values())
    assert items[P3]['reason_code'] == 'question-answered' and 'new_card' not in items[P3]
    assert feedback_projection.projection_errors({'projections': list(items.values())}) == []
    # A brief that passes is kept as it is.
    briefs[P1]['card_brief'] = 'Receipts stop in groups.'
    triage.save(a / 'briefs.json', briefs)
    assert autoloop.projection_items(a, NOW)[P1]['released_brief'] == 'Receipts stop in groups.'
    assert re.fullmatch(r'claude-code@\d+\.\d+\.\d+/release-\d+', autoloop.READER_PIN)


# spec: ANYM-988
def test_config_accepts_the_flag_only_as_a_boolean_and_an_item_older_than_a_week_is_dropped(loop):
    t, a, s = loop
    configure(a, record_projection=True)
    assert autoloop.config(a)['record_projection'] is True
    configure(a, record_projection='yes')
    with pytest.raises(autoloop.Refused):
        autoloop.config(a)
    configure(a)
    assert autoloop.config(a)['record_projection'] is False
    old = {'feedback_id': P1, 'classification': 'feature', 'reason_code': 'unclear',
        'released_at': triage.stamp(NOW - timedelta(days=8)), 'reader_pin': 'pin'}
    triage.save(a / 'projection.json', {P1: old})
    calls = []
    assert autoloop.record_projection(a, 'host', lambda *args, **kwargs: calls.append(args), NOW, fresh=False) is None
    assert calls == [] and triage.load(a / 'projection.json', None) == {}
