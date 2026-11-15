"""Historical assessment publication is an initial CAS, separate from released-brief refinement."""
import copy
from datetime import datetime, timedelta, timezone
import threading
import types

import pytest

from messaging_service import feedback_projection as fp
from scripts import autoloop

FID = '11111111-1111-4111-8111-000000000033'
NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)


class MemoryProjection:
    """Bounded SQL stand-in for local contract tests; real row-lock races run in the CI database tests below."""
    def __init__(self, row, prior=None, now=NOW):
        self.row, self.prior, self.now = copy.deepcopy(row), copy.deepcopy(prior), now
        self.statements = []
        self.audits = []

    def execute(self, query, args=None):
        self.statements.append(query)
        if query.startswith('SELECT clock_timestamp()'):
            return types.SimpleNamespace(fetchone=lambda: {'now': self.now})
        if 'FROM agent_feedback WHERE id=ANY' in query:
            return types.SimpleNamespace(fetchall=lambda: [copy.deepcopy(self.row)])
        if ' FROM feedback_projections WHERE feedback_id=' in query:
            return types.SimpleNamespace(fetchone=lambda: copy.deepcopy(self.prior))
        if 'INSERT INTO feedback_projections' in query:
            self.prior = dict(zip(fp.PROJECTION_FIELDS, args[1:]))
            return types.SimpleNamespace(fetchone=lambda: {'feedback_id': self.row['id'], 'recorded_at': self.now})
        assert query.startswith('SET LOCAL transaction_timeout=')


def memory_projection(monkeypatch, observed):
    db = MemoryProjection({'id': observed['feedback_id'], **{k: observed[k] for k in fp.IDENTITY_FIELDS}},
                          observed['projection'])
    monkeypatch.setattr(fp, 'audit', lambda *args: db.audits.append(args[-2:]))
    monkeypatch.setattr(fp, 'publish_projection_recorded', lambda *args: None)
    return db


def initial(row, prior=None):
    item = {'feedback_id': str(row['id']), 'classification': 'reported-bug', 'reason_code': 'matches-card',
        'primary_spec': row['spec_ref'], 'released_brief': 'A safely paraphrased symptom needs correction.',
        'released_at': NOW.isoformat(), 'reader_pin': autoloop.READER_PIN}
    guard = {'identity': {k: row[k] for k in fp.IDENTITY_FIELDS}, 'prior': None if prior is None else {
        'release': prior['released_at'], 'digest': fp.projection_digest(prior)}}
    return {'projections': [item], 'expected_initial': guard}


@pytest.fixture
def connection(monkeypatch):
    observed = {'feedback_id': FID, 'principal': 'agent-fixture', 'key_id': 'synthetic-key',
        'created_at': '2026-10-25T01:00:00+00:00', 'status': 'accepted',
        'status_changed_at': '2026-11-14T00:00:00+00:00', 'spec_ref': 'ANYM-988', 'duplicate_of': None,
        'projection': None}
    return memory_projection(monkeypatch, observed)


# spec: ANYM-988
@pytest.mark.parametrize('prior_exists', [False, True])
def test_initial_guard_publishes_absence_or_exact_brief_less_prior_with_row_locks(connection, prior_exists):
    db = connection
    if prior_exists:
        db.prior = {**initial(db.row)['projections'][0], 'new_card': None, 'released_brief': None,
                    'released_at': (NOW - timedelta(days=1)).isoformat()}
        db.prior.pop('feedback_id')
    document = initial(db.row, db.prior)
    assert fp.projection_errors(document) == []
    before = copy.deepcopy(db.row)
    assert fp.record_projections(db, document) == {'recorded': 1, 'unchanged': 0}
    assert db.row == before and db.audits == [('record_feedback_projection', 'ok')]
    assert any('FROM agent_feedback' in q and 'FOR UPDATE' in q for q in db.statements)
    assert any('FROM feedback_projections' in q and 'FOR UPDATE' in q for q in db.statements)
    with pytest.raises(ValueError, match='stale_initial_projection'):
        fp.record_projections(db, document)  # absence/digest cannot be replayed


# spec: ANYM-988
@pytest.mark.parametrize('field,value', [('principal', 'other'), ('key_id', 'other'),
    ('created_at', '2026-10-26T00:00:00+00:00'), ('status', 'declined'),
    ('status_changed_at', '2026-11-15T01:00:00+00:00'), ('spec_ref', 'ANYM-990'), ('duplicate_of', FID)])
def test_initial_guard_rejects_every_identity_or_disposition_mutation(connection, field, value):
    document = initial(connection.row)
    connection.row[field] = value
    with pytest.raises(ValueError, match='stale_initial_projection'):
        fp.record_projections(connection, document)
    assert connection.prior is None and not connection.audits


# spec: ANYM-988
@pytest.mark.parametrize('fault', ['appeared', 'changed-digest', 'complete', 'classification', 'optin', 'unknown',
                                  'primary', 'new-card', 'no-brief', 'old-time'])
def test_initial_guard_refuses_competing_or_nonactionable_projections(connection, fault):
    db = connection
    prior = {**initial(db.row)['projections'][0], 'new_card': None, 'released_brief': None,
             'released_at': (NOW - timedelta(days=1)).isoformat()}
    prior.pop('feedback_id')
    db.prior = copy.deepcopy(prior)
    document = initial(db.row, db.prior)
    item = document['projections'][0]
    if fault == 'appeared':
        document['expected_initial']['prior'] = None
    elif fault == 'changed-digest':
        db.prior['reader_pin'] = 'competing-reader'
    elif fault == 'complete':
        db.prior['released_brief'] = 'Existing complete brief.'
        document = initial(db.row, db.prior)
    elif fault == 'classification':
        db.prior['classification'] = 'enable-only'
        document = initial(db.row, db.prior)
    elif fault == 'optin':
        item['reason_code'] = 'needs-opt-in'
    elif fault == 'unknown':
        item['reason_code'] = 'unclear'
    elif fault == 'primary':
        item['primary_spec'] = 'ANYM-990'
    elif fault == 'new-card':
        item['new_card'] = 'other-card'
    elif fault == 'no-brief':
        item.pop('released_brief')
    elif fault == 'old-time':
        item['released_at'] = prior['released_at']
    before = copy.deepcopy(db.prior)
    with pytest.raises(ValueError, match='stale_initial_projection'):
        fp.record_projections(db, document)
    assert db.prior == before and not db.audits


# spec: ANYM-988
@pytest.mark.parametrize('fault', ['no-identity', 'no-prior', 'unknown-key', 'mixed-guards', 'bad-digest', 'batch'])
def test_initial_guard_has_an_explicit_closed_shape(connection, fault):
    document = initial(connection.row)
    guard = document['expected_initial']
    if fault == 'no-identity':
        guard.pop('identity')
    elif fault == 'no-prior':
        guard.pop('prior')
    elif fault == 'unknown-key':
        guard['wildcard'] = True
    elif fault == 'mixed-guards':
        document['expected_prior'] = guard
    elif fault == 'bad-digest':
        guard['prior'] = {'release': NOW.isoformat(), 'digest': 'unknown'}
    elif fault == 'batch':
        document['projections'] *= 2
    assert fp.projection_errors(document)


# spec: ANYM-988
def test_old_nonempty_refinement_contract_does_not_accept_an_initial_guard(connection):
    document = initial(connection.row)
    document['expected_prior'] = {'identity': document.pop('expected_initial')['identity'],
        'release': (NOW - timedelta(days=1)).isoformat(), 'digest': 'a' * 64}
    with pytest.raises(ValueError, match='stale_projection'):
        fp.record_projections(connection, document)
    assert connection.prior is None


# spec: ANYM-988
@pytest.mark.parametrize('race', ['status', 'projection'])
def test_database_initial_publication_serializes_competing_changes(seeded, race):
    """CI PostgreSQL proof: a concurrent row change commits first, then the guarded writer must refuse."""
    store, *_ = seeded
    token = store.issue_key('historical-fixture', ['telegram-demo'], permissions=['read', 'feedback-write'],
                            key_permissions=['read', 'feedback-write'])
    fid = store.submit_feedback(token, 'synthetic-history-race', 'bug', 'Synthetic historical original')['feedback_id']
    store.set_feedback_status([fid], 'accepted', spec_ref='ANYM-988')
    observed = store.get_feedback_projection(fid)
    document = initial({'id': fid, **{k: observed[k] for k in fp.IDENTITY_FIELDS}})
    document['projections'][0]['released_at'] = datetime.now(timezone.utc).isoformat()
    started, errors = threading.Event(), []
    def guarded():
        started.set()
        try:
            store.record_feedback_projection(document)
        except ValueError as error:
            errors.append(str(error))
    with store.pool.connection() as c:
        c.execute('SELECT id FROM agent_feedback WHERE id=%s FOR UPDATE', (fid,))
        if race == 'status':
            c.execute("UPDATE agent_feedback SET status='declined',spec_ref=NULL,status_changed_at=clock_timestamp() WHERE id=%s", (fid,))
        else:
            fp.record_projections(c, {'projections': document['projections']})
        worker = threading.Thread(target=guarded)
        worker.start()
        assert started.wait(1)
    worker.join(5)
    assert not worker.is_alive() and errors == ['stale_initial_projection']
    after = store.get_feedback_projection(fid)
    assert after['status'] == ('declined' if race == 'status' else 'accepted')
    assert (after['projection'] is None) == (race == 'status')


# spec: ANYM-988
@pytest.mark.parametrize('prior_exists', [False, True])
def test_database_initial_assessment_writes_only_projection_and_preserves_original(seeded, prior_exists):
    store, *_ = seeded
    token = store.issue_key('historical-fixture', ['telegram-demo'], permissions=['read', 'feedback-write'],
                            key_permissions=['read', 'feedback-write'])
    fid = store.submit_feedback(token, 'synthetic-history-first', 'bug', 'Synthetic historical source')['feedback_id']
    store.set_feedback_status([fid], 'accepted', spec_ref='ANYM-988')
    observed = store.get_feedback_projection(fid)
    if prior_exists:
        prior = initial({'id': fid, **{k: observed[k] for k in fp.IDENTITY_FIELDS}})['projections'][0]
        prior.pop('released_brief')
        prior['released_at'] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        store.record_feedback_projection({'projections': [prior]})
        observed = store.get_feedback_projection(fid)
    document = initial({'id': fid, **{k: observed[k] for k in fp.IDENTITY_FIELDS}}, observed['projection'])
    document['projections'][0]['released_at'] = datetime.now(timezone.utc).isoformat()
    with store.pool.connection() as c:
        before = c.execute('SELECT * FROM agent_feedback WHERE id=%s', (fid,)).fetchone()
    assert store.record_feedback_projection(document) == {'recorded': 1, 'unchanged': 0}
    with store.pool.connection() as c:
        after = c.execute('SELECT * FROM agent_feedback WHERE id=%s', (fid,)).fetchone()
    assert after == before
    assert store.get_feedback_projection(fid)['projection']['released_brief'] == document['projections'][0]['released_brief']
    with pytest.raises(ValueError, match='stale_initial_projection'):
        store.record_feedback_projection(document)
