"""Explicit historical compatibility uses structural authority, never historical text as a reason code."""
import copy
import json
from pathlib import Path
import types

import pytest

from scripts import autoloop, feedback_bridge, feedback_dispatch
from messaging_service import feedback_projection

REAL_RUNNER = autoloop.run_reader
triage = autoloop.triage
FID = '11111111-1111-4111-8111-000000000033'
OTHER = '11111111-1111-4111-8111-000000000034'
REF = 'ANYM-988'
NOW = autoloop.datetime(2026, 11, 16, tzinfo=autoloop.timezone.utc)
INIT = json.loads((Path(__file__).parent / 'fixtures/autoloop/init-2.1.316.json').read_text())


def stream(response):
    answer = json.dumps(response)
    return '\n'.join(json.dumps(e) for e in [INIT,
        {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': answer}]}},
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1, 'result': answer}])


def frozen(directory):
    return {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*')
            if p.is_file() and p.name not in ('lock', '.lock')}


@pytest.fixture(params=['missing-ledger', 'legacy-reason'])
def historical(tmp_path, monkeypatch, request):
    class Clock(autoloop.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(autoloop, 'datetime', Clock)
    a, t, sync = (tmp_path / name for name in ('autoloop', 'triage', 'sync'))
    source = {'id': FID, 'principal': 'agent-fixture', 'key_id': 'synthetic-key',
        'created_at': '2026-10-25T01:00:00+00:00', 'status_changed_at': '2026-11-14T00:00:00+00:00',
        'status': 'accepted', 'spec_ref': REF, 'duplicate_of': None, 'category': 'bug', 'operation': 'search',
        'description': 'Synthetic original text describing group members vanishing after synchronization.'}
    entry = {k: source[k] for k in ('principal', 'key_id', 'created_at')}
    entry.update(classification='reported-bug', reason='Synthetic legacy provenance; never infer a code from this.',
        specs=[{'ref': REF, 'verdict': 'accepted'}], primary=REF, cluster=REF, verdict_of=None,
        disposition={'status': 'accepted', 'spec_ref': REF, 'duplicate_of': None, 'note': 'Synthetic old note'},
        decided_at='2026-10-26T00:00:00+00:00', reported=True)
    ledger = {} if request.param == 'missing-ledger' else {FID: entry}
    snap = {'main': [{'ref': REF, 'title': 'Synthetic story', 'status': 'Active', 'text': '', 'sources': []}],
            'branches': {}}
    for path, value in [(t / 'ledger.json', ledger), (t / 'inbox.json', {FID: source} if not ledger else {}),
            (t / 'snapshot.json', snap), (t / 'state.json', {'position': 'old-cursor'}),
            (a / 'config.json', {'live': True, 'record_projection': True, 'principals': ['agent-fixture']}),
            (a / 'refused.json', {FID: {'count': 2, 'at': 'old', 'why': 'old'}}),
            (a / 'briefs.json', {OTHER: {'release': 1, 'card_brief': 'Synthetic unrelated brief.'}}),
            (a / 'decisions.json', [{'id': OTHER, 'classification': 'not-actionable', 'reason': 'old'}]),
            (a / 'release-history.json', {OTHER: [{'digest': 'old-unchanged'}]}),
            (a / 'ledger.json', {'days': {'2026-11-15': {'reader_runs': 18, 'items': {OTHER: 'agent-fixture'}}}})]:
        triage.save(path, value)
    monkeypatch.setattr(autoloop, 'SYNC_STATE', sync)
    monkeypatch.setattr(triage, 'collect', lambda: copy.deepcopy(snap))
    rows, calls = [source], []
    monkeypatch.setattr(triage, 'export_pages', lambda *args: (copy.deepcopy(rows), '2026-11-16T00:00:00+00:00'))
    observed = {'feedback_id': FID, **{k: source[k] for k in
        ('principal', 'key_id', 'created_at', 'status', 'status_changed_at', 'spec_ref', 'duplicate_of')}, 'projection': None}
    monkeypatch.setattr(autoloop, 'current_projection', lambda *args, **kwargs: copy.deepcopy(observed))
    response = {'decisions': [{'id': FID, 'classification': 'reported-bug', 'reason_code': 'matches-card',
        'specs': [{'ref': REF, 'verdict': 'accepted'}], 'primary': REF,
        'card_brief': 'Opening a synchronized group hides its membership; all participants should remain visible.'}]}
    def reader(projected):
        calls.append(projected)
        return stream(response)
    monkeypatch.setattr(autoloop, 'run_reader', reader)
    return types.SimpleNamespace(a=a, t=t, sync=sync, source=source, entry=entry, rows=rows, calls=calls,
        observed=observed, response=response, snap=snap, kind=request.param)


def command(r, *flags):
    return autoloop.main(['repair-brief', FID, '--historical-accepted', '--state', str(r.a),
                         '--triage-state', str(r.t), *flags])


def seam(r, projection):
    assert feedback_projection.projection_errors({'projections': [projection]}) == []
    title, description = feedback_bridge.render({'feedback_id': FID, **r.source, 'projection': projection})
    task = {'key': 'DK-33', 'number': 33, 'status': 'todo', 'labels': ['feedback'],
            'title': title, 'description': description}
    dispatcher = feedback_dispatch.Dispatcher(types.SimpleNamespace(principals=['agent-fixture'], hold=[]), None, None)
    return dispatcher.judge(task)


# spec: ANYM-991
def test_explicit_historical_assessment_releases_only_new_projection_without_mutating_old_state(historical, capsys):
    r = historical
    old_triage, old_refusals = frozen(r.t), (r.a / 'refused.json').read_bytes()
    old_usage = triage.load(r.a / 'ledger.json', {})
    old_state = frozen(r.a)
    assert command(r) == 0
    pending = triage.load(r.a / autoloop.PROJECTION, {})[FID]
    assert seam(r, pending)[1] == 'start'
    assert pending['classification'] == 'reported-bug' and pending['primary_spec'] == REF
    assert pending['reason_code'] == 'matches-card' and len(r.calls) == 1
    assert r.calls[0]['mode'] == 'historical_assessment'
    assert r.calls[0]['max_characters'] == 600 and r.calls[0]['max_consecutive_source_words'] == 4
    assert frozen(r.t) == old_triage and (r.a / 'refused.json').read_bytes() == old_refusals
    assert triage.load(r.a / 'ledger.json', {})['days']['2026-11-15'] == old_usage['days']['2026-11-15']
    assert autoloop.committed_briefs(r.a, r.t) == {}
    assert r.entry['reason'] not in (r.a / autoloop.PROJECTION).read_text()
    for path, data in old_state.items():
        if path != 'ledger.json':
            assert (r.a / path).read_bytes() == data
    usage = triage.load(r.a / 'ledger.json', {})['days']['2026-11-16']
    assert usage['reader_runs'] == 1 and usage['items'] == {FID: r.source['principal']}
    assert command(r) == 1 and len(r.calls) == 1  # no implicit retry of the new durable assessment
    assert 'historical: assessment already exists' in capsys.readouterr().err


# spec: ANYM-991
def test_default_paths_remain_conservative_for_both_historical_originals(historical):
    r = historical
    with pytest.raises(autoloop.Refused):
        autoloop.repair_brief(r.a, r.t, FID)
    with pytest.raises(autoloop.Refused):
        autoloop.read_selected(r.a, r.t, FID)
    assert not r.calls


def publisher(r, monkeypatch, *, lost=False):
    from test_feedback_projection_history import memory_projection
    db = memory_projection(monkeypatch, r.observed)
    sent = []
    def lookup(*args, **kwargs):
        from messaging_service.store import serial
        return {**r.observed, 'projection': serial(copy.deepcopy(db.prior))}
    monkeypatch.setattr(autoloop, 'current_projection', lookup)
    def ssh(host, argv, stdin=None):
        if 'inspect' in argv:
            return 'sha256:' + 'a' * 64
        document = json.loads(stdin)
        sent.append(document)
        result = feedback_projection.record_projections(db, document)
        if lost:
            raise autoloop.Refused('synthetic lost reply')
        return json.dumps(result)
    def publish():
        return autoloop.record_projection(r.a, 'synthetic-host', ssh, NOW, False, {FID}, triage_state=r.t,
                                         sync_state=r.sync)
    return db, sent, publish


# spec: ANYM-991
def test_historical_assessment_runs_through_atomic_publisher_then_real_bridge_dispatch(historical, monkeypatch):
    r = historical
    old_triage, old_usage = frozen(r.t), triage.load(r.a / 'ledger.json', {})
    assert command(r) == 0
    candidates = autoloop.projection_candidates(r.a, r.t)
    assert candidates[FID]['primary'] == REF and candidates[FID]['key_id'] == r.source['key_id']
    db, sent, publish = publisher(r, monkeypatch)
    assert publish() == 'historical projection confirmed'
    assert sent[0]['expected_initial']['prior'] is None
    assert sent[0]['expected_initial']['identity']['status'] == 'accepted'
    assert seam(r, {'feedback_id': FID, **db.prior})[1] == 'start'
    assert frozen(r.t) == old_triage
    assert triage.load(r.a / 'ledger.json', {})['days']['2026-11-15'] == old_usage['days']['2026-11-15']
    assert not autoloop.pending_projections(r.a)
    assert autoloop.record_projection(r.a, 'synthetic', lambda *args: pytest.fail('replayed'), NOW, False) is None
    assert len(sent) == 1
    assert autoloop.triage.load(r.a / autoloop.HISTORICAL, {})[FID]['phase'] == 'published'


# spec: ANYM-991
def test_historical_unknown_write_reconciles_only_exact_result_and_never_sends_twice(historical, monkeypatch):
    r = historical
    assert command(r) == 0
    db, sent, publish = publisher(r, monkeypatch, lost=True)
    with pytest.raises(autoloop.Refused, match='lost reply'):
        publish()
    exact = copy.deepcopy(db.prior)
    db.prior = None
    with pytest.raises(autoloop.Refused, match='outcome unknown'):
        publish()
    assert len(sent) == 1 and len(r.calls) == 1
    db.prior = exact
    assert publish() == 'historical projection confirmed'
    assert len(sent) == 1 and not autoloop.pending_projections(r.a)


# spec: ANYM-991
@pytest.mark.parametrize('field,value', [('principal', 'other'), ('key_id', 'other'),
    ('created_at', '2026-10-26T01:00:00+00:00'), ('status', 'received'), ('status', 'duplicate'),
    ('spec_ref', 'ANYM-990'), ('duplicate_of', OTHER), ('verdict_of', OTHER)])
def test_historical_source_mutation_never_reaches_reader(historical, field, value, capsys):
    r = historical
    before, budget = frozen(r.t), (r.a / 'ledger.json').read_bytes()
    r.rows[0][field] = value
    assert command(r) == 1
    assert r.calls == [] and not (r.a / autoloop.PROJECTION).exists()
    assert frozen(r.t) == before and (r.a / 'ledger.json').read_bytes() == budget
    assert 'REFUSED historical:' in capsys.readouterr().err


# spec: ANYM-991
@pytest.mark.parametrize('fault', ['duplicate-source', 'stop', 'drop', 'pause', 'card-hold', 'item-hold',
    'unknown-intent', 'sync-pending', 'refinement-pending', 'disabled', 'principal', 'cap', 'missing-main', 'verdict'])
def test_historical_uses_current_holds_caps_authority_and_pending_guards(historical, fault):
    r = historical
    if fault == 'duplicate-source':
        r.rows.append(copy.deepcopy(r.source))
    elif fault in ('stop', 'drop', 'verdict'):
        row = {**r.source, 'id': OTHER, 'description': 'AUTOLOOP STOP' if fault == 'stop' else 'AUTOLOOP DROP ' + FID}
        if fault == 'verdict':
            row['verdict_of'] = FID
        r.rows.append(row)
    elif fault == 'pause':
        (r.a / 'PAUSE').touch()
    elif fault in ('card-hold', 'item-hold'):
        triage.save(r.a / 'holds.json', {'cards': {REF: 'held'} if fault == 'card-hold' else {},
                                      'items': {FID: 'held'} if fault == 'item-hold' else {}})
    elif fault == 'unknown-intent':
        triage.save(r.a / 'intents' / 'autoloop-build-synthetic.json',
                    {'name': 'autoloop-build-synthetic', 'status': 'unknown'})
    elif fault == 'sync-pending':
        triage.save(r.sync / 'pending.json', {})
    elif fault == 'refinement-pending':
        triage.save(r.a / 'refinement.json', {OTHER: {'sent': True}})
    elif fault in ('disabled', 'principal', 'cap'):
        cfg = triage.load(r.a / 'config.json', {})
        cfg.update({'live': False} if fault == 'disabled' else {'principals': []} if fault == 'principal'
                   else {'caps': {**autoloop.CAPS, 'reader_runs': 0}})
        triage.save(r.a / 'config.json', cfg)
    elif fault == 'missing-main':
        r.snap['main'].clear()
    before = frozen(r.t)
    assert command(r) == 1 and not r.calls
    assert frozen(r.t) == before and not (r.a / autoloop.PROJECTION).exists()


# spec: ANYM-991
@pytest.mark.parametrize('fault', ['optin', 'already-live', 'unknown', 'enable-only', 'fixed-class', 'primary',
    'duplicate', 'new-card', 'declined-verdict', 'unsafe', 'copied', 'copied-reason', 'no-brief', 'extra', 'foreign', 'two-turn'])
def test_historical_closed_assessment_refusals_are_durable_and_do_not_publish(historical, fault, monkeypatch):
    r = historical
    d = r.response['decisions'][0]
    if fault == 'optin':
        d['reason_code'] = 'needs-opt-in'
    elif fault == 'already-live':
        d['reason_code'] = 'already-live'
    elif fault == 'unknown':
        d['reason_code'] = 'unclear'
    elif fault == 'enable-only':
        d.update(classification='enable-only', card_brief=None, reason_code='needs-opt-in')
    elif fault == 'fixed-class':
        if r.kind == 'missing-ledger':
            d.update(classification='question', specs=[], primary=None, card_brief=None)
        else:
            d['classification'] = 'feature'
    elif fault == 'primary':
        d['primary'] = 'ANYM-990'
    elif fault == 'duplicate':
        d['duplicate_of'] = OTHER
    elif fault == 'new-card':
        d['new_card'] = 'another-card'
    elif fault == 'declined-verdict':
        d.update(specs=[{'ref': REF, 'verdict': 'declined'}], card_brief=None)
    elif fault == 'unsafe':
        d['card_brief'] = 'https://private.invalid /private/file `shell`'
    elif fault == 'copied':
        d['card_brief'] = r.source['description']
    elif fault == 'copied-reason':
        if r.kind == 'missing-ledger':
            d['card_brief'] = r.source['description']
        else:
            d['card_brief'] = r.entry['reason']
    elif fault == 'no-brief':
        d.pop('card_brief')
    elif fault == 'extra':
        d['authorverdict'] = 'accepted'
    elif fault == 'foreign':
        d['id'] = OTHER
    elif fault == 'two-turn':
        def reader(projected):
            r.calls.append(projected)
            return stream(r.response).replace('"num_turns": 1', '"num_turns": 2')
        monkeypatch.setattr(autoloop, 'run_reader', reader)
    old = frozen(r.t)
    assert command(r) == 1 and len(r.calls) == 1
    assert frozen(r.t) == old and not (r.a / autoloop.PROJECTION).exists()
    assert triage.load(r.a / autoloop.HISTORICAL, {})[FID]['phase'] == 'refused'
    assert command(r) == 1 and len(r.calls) == 1
    assert autoloop.parked(r.a, {FID: r.source})[FID] >= 2
    with pytest.raises(autoloop.Refused, match='historical assessment exists'):
        autoloop.read_selected(r.a, r.t, FID)
    assert autoloop.read(r.a, r.t, runner=lambda *args: pytest.fail('implicit model replay')) == (0, None)


# spec: ANYM-991
@pytest.mark.parametrize('fault', ['identity', 'prior', 'story', 'origin', 'author-verdict', 'hold', 'unknown'])
def test_historical_publication_revalidates_authority_without_reader_replay(historical, monkeypatch, fault):
    r = historical
    assert command(r) == 0
    db, sent, publish = publisher(r, monkeypatch)
    if fault == 'identity':
        r.observed['status_changed_at'] = '2026-11-15T01:00:00+00:00'
    elif fault == 'prior':
        db.prior = {'competing': True}
    elif fault == 'story':
        r.snap['main'][0]['status'] = 'Done'
    elif fault == 'origin':
        path = r.t / ('ledger.json' if r.kind == 'legacy-reason' else 'inbox.json')
        saved = triage.load(path, {})
        saved[FID]['key_id'] = 'changed'
        triage.save(path, saved)
    elif fault == 'author-verdict':
        saved = triage.load(r.t / 'ledger.json', {})
        saved[OTHER] = {**r.entry, 'verdict_of': FID}
        triage.save(r.t / 'ledger.json', saved)
    elif fault == 'hold':
        triage.save(r.a / 'holds.json', {'cards': {REF: 'held'}, 'items': {}})
    elif fault == 'unknown':
        triage.save(r.sync / 'pending.json', {'uncertain': True})
    with pytest.raises(autoloop.Refused, match='historical:'):
        publish()
    assert not sent and len(r.calls) == 1


# spec: ANYM-991
@pytest.mark.parametrize('kind', ['triage', 'sync', 'autoloop'])
def test_historical_assessment_obeys_all_three_real_nonblocking_locks(historical, kind):
    r = historical
    lock = autoloop.own_lock(r.a) if kind == 'autoloop' else triage.locked(r.t if kind == 'triage' else r.sync)
    with lock:
        assert command(r) == 1
    assert not r.calls and not (r.a / autoloop.HISTORICAL).exists()


# spec: ANYM-991
def test_historical_pinned_reader_fixture_retains_tool_less_single_turn_isolation(historical, monkeypatch, tmp_path):
    r = historical
    import os
    import sys
    output, log, cli = (tmp_path / name for name in ('stream.jsonl', 'reader-log.json', 'claude'))
    output.write_text(stream(r.response))
    cli.write_text(f'''#!{sys.executable}
import json, os, sys
if sys.argv[1:] == ['--version']:
    print({autoloop.CLI_VERSION!r} + ' (Claude Code)'); sys.exit(0)
json.dump({{'argv': sys.argv[1:], 'env': dict(os.environ), 'cwd': os.getcwd(), 'files': os.listdir('.'),
    'input': sys.stdin.read()}}, open({str(log)!r}, 'w'))
sys.stdout.write(open({str(output)!r}).read())
''')
    cli.chmod(0o500)
    real_runner = REAL_RUNNER
    monkeypatch.setattr(autoloop, 'run_reader', lambda projected: real_runner(projected, cli, autoloop.digest(cli)))
    assert command(r) == 0
    seen = json.loads(log.read_text())
    assert seen['argv'][seen['argv'].index('--tools') + 1] == ''
    assert seen['argv'][seen['argv'].index('--setting-sources') + 1] == ''
    assert '--no-session-persistence' in seen['argv']
    assert seen['argv'][seen['argv'].index('--mcp-config') + 1] == '{"mcpServers":{}}'
    assert seen['files'] == [] and not os.path.exists(seen['cwd'])
    assert json.loads(seen['input'])['mode'] == 'historical_assessment'
    assert 'historical_reason' in json.loads(seen['input'])['items'][0]
    assert {k: v for k, v in seen['env'].items() if k != 'LC_CTYPE'} == autoloop.environment()


# spec: ANYM-991
def test_historical_saved_exact_intent_survives_queue_write_failure_without_another_assessment(historical, monkeypatch):
    r = historical
    real_save = triage.save
    def fail_queue(path, value):
        if path == r.a / autoloop.PROJECTION:
            raise OSError('synthetic queue write failure')
        real_save(path, value)
    monkeypatch.setattr(triage, 'save', fail_queue)
    with pytest.raises(OSError, match='synthetic queue'):
        command(r)
    assert len(r.calls) == 1 and triage.load(r.a / autoloop.HISTORICAL, {})[FID]['phase'] == 'queued'
    monkeypatch.setattr(triage, 'save', real_save)
    assert FID in autoloop.pending_projections(r.a) and command(r) == 1 and len(r.calls) == 1
    _, sent, publish = publisher(r, monkeypatch)
    assert publish() == 'historical projection confirmed' and len(sent) == 1


# spec: ANYM-991
def test_historical_exact_brief_less_projection_uses_a_digest_guard(historical, monkeypatch):
    r = historical
    prior = {'classification': 'reported-bug', 'reason_code': 'matches-card', 'primary_spec': REF,
        'new_card': None, 'released_brief': None, 'released_at': '2026-11-14T00:00:00+00:00',
        'reader_pin': autoloop.READER_PIN}
    r.observed['projection'] = prior
    assert command(r) == 0
    db, sent, publish = publisher(r, monkeypatch)
    assert publish() == 'historical projection confirmed'
    assert sent[0]['expected_initial']['prior'] == {'release': prior['released_at'],
                                                  'digest': autoloop.projection_digest(prior)}
    assert seam(r, {'feedback_id': FID, **db.prior})[1] == 'start'


# spec: ANYM-991
@pytest.mark.parametrize('fault', ['complete', 'unknown', 'optin', 'enable-only', 'other-primary', 'malformed'])
def test_historical_existing_projection_must_be_explicitly_incomplete_and_actionable(historical, fault):
    r = historical
    prior = {'classification': 'reported-bug', 'reason_code': 'matches-card', 'primary_spec': REF,
        'new_card': None, 'released_brief': None, 'released_at': '2026-11-14T00:00:00+00:00',
        'reader_pin': autoloop.READER_PIN}
    if fault == 'complete':
        prior['released_brief'] = 'Already released brief.'
    elif fault == 'unknown':
        prior['reason_code'] = 'unclear'
    elif fault == 'optin':
        prior['reason_code'] = 'needs-opt-in'
    elif fault == 'enable-only':
        prior['classification'] = 'enable-only'
    elif fault == 'other-primary':
        prior['primary_spec'] = 'ANYM-990'
    elif fault == 'malformed':
        prior['released_at'] = 'bad-time'
    r.observed['projection'] = prior
    assert command(r) == 1 and not r.calls
    assert not (r.a / autoloop.PROJECTION).exists()
