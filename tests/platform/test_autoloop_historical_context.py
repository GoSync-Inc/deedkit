"""Historical accepted work reads one fresh main story, never the branch inventory."""
import copy
import hashlib
import json
import subprocess
import types

import pytest

from scripts import autoloop
from test_autoloop_legacy_briefs import FID, REF, command, frozen, historical, publisher  # noqa: F401

triage = autoloop.triage
REAL_TIDEN = triage.tiden
REAL_RUN = triage.run
REAL_COLLECT = triage.collect
PRODUCT = '75bd0580-2af4-4d5a-b236-c5a9d7ff7ce1'
WORKSPACE = '1dc18e09-4a09-4d74-8300-cd4302622357'
MAIN = '22222222-2222-4222-8222-000000000001'
STORY = '22222222-2222-4222-8222-000000000002'


@pytest.fixture
def targeted(historical, monkeypatch):
    r = historical
    # Order and Python str(dict) bytes matter: collect/flatten has always kept them in the digest.
    r.raw = {'id': STORY, 'productId': PRODUCT, 'branchId': MAIN, 'seqNum': int(REF[5:]),
        'type': 'User Story', 'title': 'Synthetic story — группы', 'status': 'Review',
        'content': 'Problem: synthetic membership\r\nExpected: retain all participants.\n',
        'sourceCount': 2, 'sources': [
            {'id': STORY, 'productId': PRODUCT, 'branchId': MAIN, 'requirementId': STORY,
             'sourceType': 'repo_file', 'title': 'Synthetic anchor', 'locator': 'scripts/example.py',
             'url': '', 'repoPath': 'scripts/example.py', 'repository': 'github.com/example/synthetic',
             'excerpt': '  synthetic\r\n', 'metadata': {'z': ['а', 3], 'a': None},
             'createdBy': 'fixture', 'createdAt': '2026-11-01', 'updatedAt': '2026-11-02'},
            {'sourceType': 'text', 'metadata': {'second': True, 'first': False}, 'title': 'Other'}]}
    r.raw['contentSha256'] = hashlib.sha256(r.raw['content'].encode()).hexdigest()
    r.branches = {'branches': [{'id': MAIN, 'productId': PRODUCT, 'name': 'main', 'status': 'open'}]}
    r.cli = []
    def no_inventory(*args, **kwargs):
        pytest.fail('historical path entered slow all-branch collector (83+ sequential inventories)')
    monkeypatch.setattr(triage, 'collect', no_inventory)
    monkeypatch.setattr(triage, 'tiden', REAL_TIDEN)
    def run(argv, timeout, stdin=None):
        if argv[0] != 'tiden':
            return REAL_RUN(argv, timeout, stdin)
        r.cli.append((argv, timeout, stdin))
        common = ['--product-id', PRODUCT, '--workspace-id', WORKSPACE, '--format', 'json']
        if argv == ['tiden', 'spec', 'get', REF, *common]:
            return json.dumps(r.raw, ensure_ascii=False)
        assert argv == ['tiden', 'branch', 'list', *common]
        return json.dumps(r.branches)
    monkeypatch.setattr(triage, 'run', run)
    return r


# spec: ANYM-991
@pytest.mark.parametrize('open_branches', [0, 83, 1000])
def test_historical_assessment_and_publication_use_two_exact_cli_calls_each(targeted, monkeypatch, open_branches):
    r = targeted
    r.branches['branches'] += [{'id': f'33333333-3333-4333-8333-{n:012d}', 'productId': PRODUCT,
                                'name': f'proposal/synthetic-{n}', 'status': 'open'} for n in range(open_branches)]
    before = frozen(r.t)
    assert command(r) == 0
    assert len(r.cli) == 2 and len(r.calls) == 1
    expected = triage.flatten({'requirements': [r.raw], 'pagination': {'totalCount': 1}}, 'fixture')[0]
    assert r.calls[0]['candidates'][FID] == [dict(expected, branch='main')]
    assert triage.load(r.a / autoloop.HISTORICAL, {})[FID]['spec_digest'] == autoloop.projection_digest(expected)
    _, sent, publish = publisher(r, monkeypatch)
    assert publish() == 'historical projection confirmed'
    assert len(r.cli) == 4 and len(sent) == 1 and len(r.calls) == 1
    assert all(timeout == triage.TIDEN_TIMEOUT and stdin is None for _, timeout, stdin in r.cli)
    assert frozen(r.t) == before


# spec: ANYM-991
def test_historical_lookup_works_in_fresh_checkout_without_local_tiden_binding(targeted, monkeypatch, tmp_path):
    r = targeted
    checkout = tmp_path / 'unbound-checkout'
    checkout.mkdir()
    monkeypatch.setattr(autoloop, 'ROOT', checkout)
    assert not (checkout / '.tiden' / 'config.json').exists()
    assert command(r) == 0 and len(r.cli) == 2
    _, sent, publish = publisher(r, monkeypatch)
    assert publish() == 'historical projection confirmed' and len(r.cli) == 4 and len(sent) == 1


# spec: ANYM-991
@pytest.mark.parametrize('stage', ['assessment', 'publication'])
@pytest.mark.parametrize('fault', ['wrong-ref', 'bool-seq', 'wrong-branch', 'wrong-product', 'bad-uuid',
    'missing-main', 'duplicate-main', 'shadow-main-id', 'bad-main-id', 'wrong-main-product', 'closed-main',
    'branch-shape', 'row-shape', 'branch-next-page', 'branch-short-count', 'missing-content', 'empty-content',
    'bad-content', 'missing-hash', 'truncated-content', 'missing-title', 'bad-status', 'missing-sources',
    'bad-sources', 'short-sources', 'missing-source-count', 'bool-source-count', 'not-story', 'get-shape'])
def test_historical_incomplete_or_wrong_story_refuses_without_state_changes(targeted, monkeypatch, stage, fault):
    r = targeted
    if stage == 'publication':
        assert command(r) == 0
    _, sent, publish = publisher(r, monkeypatch)
    old_a, old_t = frozen(r.a), frozen(r.t)
    if fault == 'wrong-ref':
        r.raw['seqNum'] += 1
    elif fault == 'bool-seq':
        r.raw['seqNum'] = True
    elif fault in ('wrong-branch', 'wrong-product', 'bad-uuid'):
        r.raw[{'wrong-branch': 'branchId', 'wrong-product': 'productId', 'bad-uuid': 'id'}[fault]] = STORY
        if fault == 'bad-uuid':
            r.raw['id'] = 'not-a-uuid'
    elif fault == 'missing-main':
        r.branches['branches'].clear()
    elif fault == 'duplicate-main':
        r.branches['branches'].append(copy.deepcopy(r.branches['branches'][0]))
    elif fault == 'shadow-main-id':
        r.branches['branches'].append({**r.branches['branches'][0], 'name': 'proposal/shadow'})
    elif fault in ('bad-main-id', 'wrong-main-product', 'closed-main'):
        key, value = {'bad-main-id': ('id', 'bad'), 'wrong-main-product': ('productId', STORY),
                      'closed-main': ('status', 'closed')}[fault]
        r.branches['branches'][0][key] = value
    elif fault == 'branch-shape':
        r.branches = []
    elif fault == 'row-shape':
        r.branches['branches'].append(None)
    elif fault in ('branch-next-page', 'branch-short-count'):
        r.branches['pagination'] = {'totalCount': 1, 'nextPageToken': 'more'} if fault == 'branch-next-page' \
            else {'totalCount': 2}
    elif fault.startswith('missing-'):
        r.raw.pop({'missing-content': 'content', 'missing-hash': 'contentSha256', 'missing-title': 'title',
                   'missing-sources': 'sources', 'missing-source-count': 'sourceCount'}[fault])
    elif fault == 'empty-content':
        r.raw['content'] = ''
        r.raw['contentSha256'] = hashlib.sha256(b'').hexdigest()
    elif fault == 'bad-content':
        r.raw['content'] = ['truncated']
    elif fault == 'truncated-content':
        r.raw['content'] = r.raw['content'][:10]  # original content hash remains
    elif fault == 'bad-status':
        r.raw['status'] = None
    elif fault == 'bad-sources':
        r.raw['sources'] = {}
    elif fault == 'short-sources':
        r.raw['sources'].pop()
    elif fault == 'bool-source-count':
        r.raw['sourceCount'] = True
    elif fault == 'not-story':
        r.raw['type'] = 'Epic'
    elif fault == 'get-shape':
        r.raw = []
    if stage == 'assessment':
        assert command(r) == 1 and not r.calls
    else:
        with pytest.raises((autoloop.Refused, triage.Refused)):
            publish()
        assert len(r.calls) == 1
    assert not sent and len(r.cli) <= (2 if stage == 'assessment' else 4)
    assert frozen(r.a) == old_a and frozen(r.t) == old_t


# spec: ANYM-991
@pytest.mark.parametrize('field', ['title', 'status', 'content', 'sources-order', 'source-metadata', 'source-key-order'])
def test_publication_rechecks_every_normalized_field_and_preserves_queued_digest(targeted, monkeypatch, field):
    r = targeted
    assert command(r) == 0
    _, sent, publish = publisher(r, monkeypatch)
    before = frozen(r.a)
    if field in ('title', 'status', 'content'):
        r.raw[field] += ' changed'
        r.raw['contentSha256'] = hashlib.sha256(r.raw['content'].encode()).hexdigest()
    elif field == 'sources-order':
        r.raw['sources'].reverse()
    elif field == 'source-metadata':
        r.raw['sources'][0]['metadata']['new'] = 'changed'
    elif field == 'source-key-order':
        r.raw['sources'][0] = dict(reversed(list(r.raw['sources'][0].items())))
    with pytest.raises(autoloop.Refused, match='current story changed'):
        publish()
    assert not sent and len(r.calls) == 1 and frozen(r.a) == before


# spec: ANYM-991
@pytest.mark.parametrize('stage', ['assessment', 'publication'])
@pytest.mark.parametrize('endpoint', ['get', 'list'])
@pytest.mark.parametrize('fault', ['timeout', 'os-error', 'exit', 'truncated-json'])
def test_historical_transport_failures_keep_journal_and_unknown_state(targeted, monkeypatch, stage, endpoint, fault):
    r = targeted
    if stage == 'publication':
        assert command(r) == 0
    _, sent, publish = publisher(r, monkeypatch)
    before_a, before_t = frozen(r.a), frozen(r.t)
    real_subprocess = subprocess.run
    calls = []
    def process(argv, **kwargs):
        if argv[0] != 'tiden':
            return real_subprocess(argv, **kwargs)
        calls.append(argv)
        assert kwargs['timeout'] == triage.TIDEN_TIMEOUT and kwargs['stdin'] == subprocess.DEVNULL
        if argv[2] == endpoint:
            if fault == 'timeout':
                raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
            if fault == 'os-error':
                raise OSError('synthetic missing executable')
            return types.SimpleNamespace(returncode=1 if fault == 'exit' else 0, stderr='synthetic failure', stdout='{')
        return types.SimpleNamespace(returncode=0, stderr='', stdout=json.dumps(r.raw))
    monkeypatch.setattr(triage, 'run', REAL_RUN)
    monkeypatch.setattr(subprocess, 'run', process)
    if stage == 'assessment':
        assert command(r) == 1 and not r.calls
    else:
        with pytest.raises(autoloop.Refused, match='historical: current story lookup failed'):
            publish()
        assert len(r.calls) == 1
    assert len(calls) == (1 if endpoint == 'get' else 2) and not sent
    assert frozen(r.a) == before_a and frozen(r.t) == before_t


# spec: ANYM-991
def test_preexisting_collect_digest_publishes_with_targeted_lookup_without_reader_or_journal_reset(targeted, monkeypatch):
    r = targeted
    # Recreate the OLD collector's normal main tree, including nested stories and full source metadata.
    reads = []
    def old_read(*args):
        reads.append(args)
        if args[:2] == ('branch', 'list'):
            return copy.deepcopy(r.branches)
        assert args == ('spec', 'list', '--branch', 'main', '--include-sources')
        return {'requirements': [{'seqNum': 1, 'title': 'Epic', 'children': [copy.deepcopy(r.raw)]}],
                'pagination': {'totalCount': 2}}
    old = next(s for s in REAL_COLLECT(old_read)['main'] if s['ref'] == REF)
    assert old == {'ref': REF, 'title': r.raw['title'], 'status': r.raw['status'], 'text': r.raw['content'],
                   'sources': [str(s) for s in r.raw['sources']]}
    item = {'feedback_id': FID, 'classification': 'reported-bug', 'reason_code': 'matches-card',
        'primary_spec': REF, 'new_card': None, 'released_at': '2026-11-15T17:55:39.980023Z',
        'reader_pin': 'synthetic-old-pin',
        'released_brief': 'Opening a synchronized group hides its membership; all participants should remain visible.'}
    original = r.entry if r.kind == 'legacy-reason' else r.source
    attempt = {'identity': {k: r.observed[k] for k in autoloop.IDENTITY_FIELDS},
        'origin': 'ledger' if r.kind == 'legacy-reason' else 'inbox',
        'origin_digest': autoloop.projection_digest(original), 'spec_digest': autoloop.projection_digest(old),
        'prior': None, 'at': '2026-11-15T18:01:44Z', 'phase': 'queued', 'item': item}
    autoloop.save_historical(r.a, {FID: attempt})
    triage.save(r.a / autoloop.PROJECTION, {FID: item})
    before_t, budget = frozen(r.t), (r.a / 'ledger.json').read_bytes()
    _, sent, publish = publisher(r, monkeypatch)
    assert publish() == 'historical projection confirmed'
    assert len(r.cli) == 2 and len(reads) == 2 and len(sent) == 1 and not r.calls
    assert triage.load(r.a / autoloop.HISTORICAL, {}) == {FID: dict(attempt, phase='published')}
    assert frozen(r.t) == before_t and (r.a / 'ledger.json').read_bytes() == budget


# spec: ANYM-991
def test_dispatching_unknown_keeps_exact_journal_and_does_not_lookup_or_resend(targeted, monkeypatch):
    r = targeted
    assert command(r) == 0
    _, sent, publish = publisher(r, monkeypatch, lost=True)
    with pytest.raises(autoloop.Refused, match='synthetic lost reply'):
        publish()
    attempts = triage.load(r.a / autoloop.HISTORICAL, {})
    assert attempts[FID]['phase'] == 'dispatching'
    before = frozen(r.a)
    r.raw['sources'].clear()  # unknown write reconciliation reads only the exact committed projection
    r.observed['status_changed_at'] = 'changed'
    with pytest.raises(autoloop.Refused, match='authoritative identity changed'):
        publish()
    assert frozen(r.a) == before and len(r.cli) == 4 and len(sent) == 1 and len(r.calls) == 1
