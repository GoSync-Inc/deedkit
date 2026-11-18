"""The snapshot's Tiden reads through the real CLI wrapper against a fake `tiden` on PATH (ANYM-569, ANYM-264).

The fake follows the CLI's documented paging (`--all` is the default at 200 per request, `--page-size` with `--all` is
the size of each request, `--include-sources` implies `--all`; the output is the flat list and the last page's
pagination) over a synthetic product, and models the per-request deadline: a request that carries more sourced specs
than the deadline allows fails as the CLI does, `context deadline exceeded`, exit 1.
"""
import json
import os
import sys

import pytest

import scripts.feedback_triage as triage

FAKE = r'''#!@PYTHON@
import json, sys
argv = sys.argv[1:]
world = json.load(open(@WORLD@))
log = open(@LOG@, 'a')


def flag(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


def out(value):
    sys.stdout.write(json.dumps(value))
    sys.exit(0)


assert flag('--format') == 'json' and sys.stdin.read() == ''
if argv[:2] == ['branch', 'list']:
    log.write(json.dumps({'read': 'branch list'}) + '\n')
    out({'branches': [{'name': 'main', 'status': 'open'}, {'name': 'proposal/old', 'status': 'merged'}]
         + [{'name': name, 'status': 'open'} for name in world['branches']]})
assert argv[:2] == ['spec', 'list'], argv
branch, sources = flag('--branch', 'main'), '--include-sources' in argv
if branch in world['gone']:
    sys.stderr.write(f"error: HTTP 400: branch '{world['gone'][branch]}' is merged\n")
    sys.exit(1)
if branch in world['fail']:
    sys.stderr.write('error: HTTP 500: synthetic failure\n')
    sys.exit(1)
size = int(flag('--page-size', 200))
every = '--all' in argv or sources or '--page-size' not in argv
listing = list(world['main'])
if branch != 'main':
    changed = world['branches'][branch]
    listing = [changed.get(spec['id'], spec) for spec in listing] + [s for i, s in changed.items() if i not in
                                                                       {spec['id'] for spec in listing}]
token, rows, total, requests = 0, [], None, 0
while True:
    requests += 1
    if branch == 'main' and world['mode'] == 'insert' and requests == 2:  # a spec created while paging
        listing.insert(0, {'id': 'new', 'seqNum': 999, 'title': 'Created meanwhile', 'status': 'Draft', 'content': ''})
    page = listing[token:token + size]
    sourced = len(page) if sources else 0
    log.write(json.dumps({'read': branch, 'size': size, 'rows': len(page), 'sourced': sourced}) + '\n')
    if sourced > world['deadline_sourced']:
        sys.stderr.write('error: Get "https://api.example.invalid/v1/products/p/requirements": '
                         'context deadline exceeded (Client.Timeout exceeded while awaiting headers)\n')
        sys.exit(1)
    rows += [dict(spec, sources=spec.get('sources', [])) if sources else
             {k: v for k, v in spec.items() if k != 'sources'} for spec in page]
    total = len(listing) - (1 if world['mode'] == 'shrink' and requests > 1 else 0)
    token = token if world['mode'] == 'stuck' else token + size
    more = token < len(listing)
    if not more or not every or len(rows) >= 20000:  # the CLI's own cap, then exit 0 with the token left
        out({'requirements': rows, 'pagination': {'nextPageToken': str(token) if more else '', 'totalCount': total}})
'''


def story(n, parent=None):
    spec = {'id': f'id-{n}', 'seqNum': n, 'title': f'Story {n}', 'status': 'Active', 'content': f'operation op_{n}',
            'sources': [{'locator': f'services/messaging/file_{n}.py', 'sourceType': 'repo_file'}]}
    return dict(spec, parentId=parent) if parent else spec


def world(tmp_path, monkeypatch, *, specs=120, deadline_sourced=10 ** 6, mode='ok', gone=None, fail=()):
    """A synthetic product: main, one branch adding a card, one modifying a main card, one only inheriting main."""
    main = [story(n, parent=None if n % 10 == 1 else f'id-{n - n % 10 + 1}') for n in range(1, specs + 1)]
    modified = dict(main[2], title='Story 3, reworded on the branch', content='operation op_3 and op_3b')
    branches = {'proposal/added': {'id-new-card': {'id': 'id-new-card', 'seqNum': 900, 'title': 'Proposed card',
                                                   'status': 'Draft', 'content': 'operation op_new'}},
                'proposal/modified': {modified['id']: {k: v for k, v in modified.items() if k != 'sources'}},
                'review/inherited': {}}
    data = {'main': main, 'branches': branches, 'deadline_sourced': deadline_sourced, 'mode': mode,
            'gone': gone or {}, 'fail': list(fail)}
    (tmp_path / 'world.json').write_text(json.dumps(data))
    bin_ = tmp_path / 'bin'
    bin_.mkdir(exist_ok=True)
    fake = bin_ / 'tiden'
    fake.write_text(FAKE.replace('@PYTHON@', sys.executable).replace('@WORLD@', repr(str(tmp_path / 'world.json')))
                    .replace('@LOG@', repr(str(tmp_path / 'log'))))
    fake.chmod(0o755)
    monkeypatch.setenv('PATH', f'{bin_}{os.pathsep}{os.environ["PATH"]}')
    monkeypatch.setattr(triage, 'tiden', REAL_TIDEN)
    return data


REAL_TIDEN = triage.tiden


def requests(tmp_path):
    path = tmp_path / 'log'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def expected(data):
    """The whole intended snapshot, built from the synthetic product directly (not through any read)."""
    def norm(spec, sources):
        return {'ref': f"ANYM-{spec['seqNum']}", 'title': spec['title'], 'status': spec['status'],
                'text': spec['content'], 'sources': [str(s) for s in spec.get('sources', [])] if sources else []}

    def view(name):
        changed = data['branches'][name]
        listing = [changed.get(s['id'], s) for s in data['main']] + [s for i, s in changed.items()
                                                                     if i not in {m['id'] for m in data['main']}]
        return sorted((norm(s, False) for s in listing), key=lambda s: s['ref'])
    return {'main': sorted((norm(s, True) for s in data['main']), key=lambda s: s['ref']),
            'branches': {name: view(name) for name in sorted(data['branches'])}}


def ordered(snap):
    return {'main': sorted(snap['main'], key=lambda s: s['ref']),
            'branches': {name: sorted(listed, key=lambda s: s['ref']) for name, listed in snap['branches'].items()}}


# spec: ANYM-569
def test_main_is_read_whole_in_small_requests_when_large_sourced_requests_miss_the_per_request_deadline(
        tmp_path, monkeypatch):
    # 120 sourced main specs: one default request of 200 carries all of them and exceeds the modelled per-request
    # deadline; requests of 50 each stay inside it. The snapshot is the whole intended one either way.
    data = world(tmp_path, monkeypatch, deadline_sourced=60)
    snap = triage.collect()
    assert ordered(snap) == expected(data)
    main = [r for r in requests(tmp_path) if r['read'] == 'main']
    assert [r['rows'] for r in main] == [50, 50, 20] and max(r['sourced'] for r in main) <= 50
    # Branch reads keep the CLI default request size: smaller pages do not shrink a branch request's cost.
    assert {r['size'] for r in requests(tmp_path) if r['read'] not in ('main', 'branch list')} == {200}
    assert [r['read'] for r in requests(tmp_path) if r['read'] != 'main'] == [
        'branch list', 'proposal/added', 'proposal/modified', 'review/inherited']


# spec: ANYM-569
def test_the_small_request_snapshot_equals_the_whole_product_with_added_modified_and_inherited_branch_cards(
        tmp_path, monkeypatch):
    data = world(tmp_path, monkeypatch, specs=230)
    snap = triage.collect()
    assert ordered(snap) == expected(data)
    added, modified = snap['branches']['proposal/added'], snap['branches']['proposal/modified']
    assert {s['ref'] for s in added} - {s['ref'] for s in snap['main']} == {'ANYM-900'}
    assert next(s for s in modified if s['ref'] == 'ANYM-3')['title'] == 'Story 3, reworded on the branch'
    assert next(s for s in snap['main'] if s['ref'] == 'ANYM-3')['title'] == 'Story 3'
    assert len(snap['branches']['review/inherited']) == 230
    assert all(s['sources'] for s in snap['main'])
    assert len([r for r in requests(tmp_path) if r['read'] == 'main']) == 5  # 230 in requests of at most 50


# spec: ANYM-569
@pytest.mark.parametrize('mode', ['shrink', 'stuck', 'insert'])
def test_a_main_listing_that_changes_or_stops_advancing_while_paging_refuses_and_writes_nothing(
        tmp_path, monkeypatch, mode):
    world(tmp_path, monkeypatch, mode=mode)
    state = tmp_path / 'state'
    state.mkdir()
    with pytest.raises(triage.Refused, match='main'):
        triage.snapshot(state)
    assert not (state / 'snapshot.json').exists()
    assert sum(r['read'] == 'main' for r in requests(tmp_path)) <= 20000 // 50 + 1


# spec: ANYM-569
def test_a_failed_or_unknown_gone_branch_and_a_main_deadline_refuse_the_whole_snapshot_and_keep_the_old_file(
        tmp_path, monkeypatch):
    world(tmp_path, monkeypatch)
    state = tmp_path / 'state'
    state.mkdir()
    assert triage.snapshot(state) == (120, 3)
    before = (state / 'snapshot.json').read_bytes()
    for kwargs in ({'fail': ['proposal/modified']}, {'gone': {'proposal/added': 'proposal/other'}},
                   {'deadline_sourced': 40}):
        (tmp_path / 'log').unlink()
        world(tmp_path, monkeypatch, **kwargs)
        with pytest.raises(triage.Refused):
            triage.snapshot(state)
        assert (state / 'snapshot.json').read_bytes() == before
    # A branch Tiden names as merged since `branch list` is left out and main is read whole again.
    (tmp_path / 'log').unlink()
    data = world(tmp_path, monkeypatch, gone={'proposal/added': 'proposal/added'})
    snap = triage.collect()
    want = expected(data)
    del want['branches']['proposal/added']
    assert ordered(snap) == want
    assert [r['read'] for r in requests(tmp_path)].count('main') == 6  # 120 rows in 3 requests, twice
