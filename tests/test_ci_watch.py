"""Durable observer contract; no GitHub mutation or provider call in these fixtures."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import json
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ci_watch.py'
spec = importlib.util.spec_from_file_location('ci_watch', SCRIPT)
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)
HEAD, MAIN = 'a' * 40, 'b' * 40
MAPPING = dict(repository='GoSync-Inc/deedkit', pr=7, head=HEAD, thread='thr_worker',
               project='proj_test', phase='source', remaining_action='none',
               action_owner='keeper-and-ci', deadline='2099-01-01T00:00:00Z')


class BB:
    def __init__(self):
        self.messages, self.queued, self.sent = [], [], []
        self.available, self.lost_reply = True, False

    def owner(self, mapping):
        if not self.available:
            raise watch.Problem('owner-unavailable')

    def reconcile(self, mapping, notice):
        if notice['body'] in self.messages:
            return 'delivered'
        if notice['body'] in self.queued:
            return 'queued'
        return 'unknown'

    def send(self, mapping, notice):
        self.sent.append(notice['body'])
        self.messages.append(notice['body'])
        if self.lost_reply:
            raise watch.Problem('transport-unknown')


class GitHub:
    def __init__(self):
        self.result = {'state': 'complete', 'proof': {'main': MAIN, 'sync': 55}}
        self.calls = 0

    def observe(self, mapping):
        self.calls += 1
        return deepcopy(self.result)


def register(tmp_path, bb=None, mapping=None):
    path = tmp_path / 'registry.json'
    watch.register(path, deepcopy(mapping or MAPPING), bb or BB())
    return path


def record(path):
    return next(iter(watch.load(path)['records'].values()))


# spec: ANYM-996
def test_completed_source_is_durable_quiet_and_terminal(tmp_path):
    bb, gh = BB(), GitHub()
    path = register(tmp_path, bb)
    watch.tick(path, gh, bb)
    assert record(path)['state'] == 'complete'
    assert record(path)['proof'] == gh.result['proof']
    assert not bb.sent
    watch.tick(path, gh, bb)
    assert gh.calls == 1


# spec: ANYM-996
def test_real_post_green_obligation_wakes_once_and_is_not_deployed(tmp_path):
    bb, gh = BB(), GitHub()
    mapping = {**MAPPING, 'remaining_action': 'Install and read back the bounded BB runtime',
               'action_owner': 'thr_worker'}
    path = register(tmp_path, bb, mapping)
    watch.tick(path, gh, bb)
    watch.tick(path, gh, bb)
    assert len(bb.sent) == 1
    assert record(path)['state'] == 'action_required'
    assert 'Install and read back' in bb.sent[0]
    assert json.loads(bb.sent[0].splitlines()[1])['retry_authorized'] is False


# spec: ANYM-996
@pytest.mark.parametrize('reason', ['check-failed', 'merge-conflict', 'dequeued', 'head-changed'])
def test_actionable_failures_are_stable_and_bounded(tmp_path, reason):
    bb, gh = BB(), GitHub()
    gh.result = {'state': 'attention', 'reason': reason, 'proof': {'run': 12, 'attempt': 2}}
    path = register(tmp_path, bb)
    watch.tick(path, gh, bb)
    body = record(path)['notice']['body']
    assert len(body) < 4096 and reason in body
    watch.tick(path, gh, bb)
    assert bb.sent == [body]


# spec: ANYM-996
def test_accepted_lost_reply_reconciles_without_another_send(tmp_path):
    bb, gh = BB(), GitHub()
    bb.lost_reply = True
    gh.result = {'state': 'attention', 'reason': 'check-failed', 'proof': {'run': 12}}
    path = register(tmp_path, bb)
    watch.tick(path, gh, bb)
    assert record(path)['notice']['delivery'] == 'needs-reconciliation'
    watch.tick(path, gh, bb)
    assert record(path)['notice']['delivery'] == 'delivered'
    assert len(bb.sent) == 1


# spec: ANYM-996
def test_unknown_and_queued_delivery_never_resend_or_claim_success(tmp_path):
    bb, gh = BB(), GitHub()
    path = register(tmp_path, bb)
    gh.result = {'state': 'attention', 'reason': 'check-failed'}
    bb.lost_reply = True
    watch.tick(path, gh, bb)
    data = watch.load(path)
    item = next(iter(data['records'].values()))
    body = item['notice']['body']
    item['notice']['delivery'] = 'dispatching'
    watch.save(path, data)
    bb.messages.clear()
    watch.tick(path, gh, bb)
    assert record(path)['notice']['delivery'] == 'needs-reconciliation'
    bb.queued.append(body)
    watch.tick(path, gh, bb)
    assert record(path)['notice']['delivery'] == 'queued'
    bb.queued.clear()
    bb.messages.append(body)
    watch.tick(path, gh, bb)
    assert record(path)['notice']['delivery'] == 'delivered'
    assert len(bb.sent) == 1


# spec: ANYM-996
def test_registration_is_idempotent_refuses_owner_conflict_and_keeps_history(tmp_path):
    bb = BB()
    path = register(tmp_path, bb)
    original = path.read_bytes()
    watch.register(path, MAPPING, bb)
    assert path.read_bytes() == original
    with pytest.raises(watch.Problem, match='owner-conflict'):
        watch.register(path, {**MAPPING, 'thread': 'thr_other'}, bb)
    watch.register(path, {**MAPPING, 'head': 'c' * 40}, bb)
    entries = list(watch.load(path)['records'].values())
    assert len(entries) == 2 and entries[0]['state'] == 'superseded'


# spec: ANYM-996
def test_unavailable_owner_never_completes_even_when_ci_is_green(tmp_path):
    bb, gh = BB(), GitHub()
    path = register(tmp_path, bb)
    bb.available = False
    watch.tick(path, gh, bb)
    assert record(path)['state'] == 'handoff_problem'
    assert not bb.sent and gh.calls == 0


def api_fixture():
    required = [{'type': 'required_status_checks', 'parameters': {'required_status_checks': [
        {'context': name, 'integration_id': 15368} for name in ('CI gate', 'checks / verify')]}}]
    run = dict(id=12, run_attempt=2, head_sha=HEAD, path='.github/workflows/ci.yml',
               repository={'full_name': MAPPING['repository']}, check_suite_id=99,
               status='completed', conclusion='success', head_branch='main', event='pull_request',
               created_at='2026-11-13T08:00:01Z')
    checks = [dict(id=100 + i, name=name, app={'id': 15368}, head_sha=HEAD,
                   details_url=f'https://github.com/GoSync-Inc/deedkit/actions/runs/12/job/{100 + i}',
                   check_suite={'id': 99}, status='completed', conclusion='success')
              for i, name in enumerate(('CI gate', 'checks / verify'))]
    jobs = [{**c, 'run_id': 12, 'run_attempt': 2} for c in checks]
    return required, run, checks, jobs


class Api(watch.GitHub):
    def __init__(self):
        super().__init__(None)
        self.required_rows, self.workflow, self.check_rows, self.job_rows = api_fixture()
        self.pr = dict(head={'sha': HEAD, 'repo': {'full_name': MAPPING['repository']}},
                       base={'ref': 'main'}, merged_at=None, state='open', draft=False,
                       labels=[{'name': 'ship'}], mergeable=True)
        self.queue = dict(headRefOid=HEAD, isInMergeQueue=False, autoMergeRequest=None,
                          timelineItems={'nodes': []})
        self.main_runs, self.sync_runs = [], []
        self.sync_jobs = []
        self.comparisons = {}

    def api(self, repository, path):
        if path == 'rules/branches/main':
            return self.required_rows
        if path == 'pulls/7':
            return self.pr
        if path == 'actions/runs/12':
            return self.workflow
        if path == 'actions/runs/55':
            return self.sync_runs[0]
        if path.startswith('compare/'):
            return self.comparisons[path]
        raise AssertionError('Unexpected read: ' + path)

    def pages(self, repository, path, field=None):
        if path.startswith('commits/') and 'check-runs' in path:
            from urllib.parse import unquote
            name = unquote(path.split('check_name=')[1])
            return [c for c in self.check_rows if c['name'] == name]
        if path.startswith('commits/') and 'check-suites' in path:
            return [{'created_at': '2026-11-13T07:00:00Z'}]
        if path.startswith('actions/runs/12/'):
            return self.job_rows
        if path.startswith('actions/runs/55/'):
            return self.sync_jobs
        if path.startswith('actions/workflows/ci.yml/'):
            return self.main_runs
        if path.startswith('actions/workflows/tiden-sync.yml/'):
            return self.sync_runs
        raise AssertionError('Unexpected paged read: ' + path)

    def graph(self, repository, number):
        return self.queue


# spec: ANYM-996
@pytest.mark.parametrize('change', ['missing', 'wrong-app', 'new-attempt', 'pending', 'all-skipped',
                                   'wrong-head', 'wrong-run', 'failed'])
def test_missing_stale_or_unproven_required_checks_never_become_green(change):
    gh = Api()
    if change == 'missing':
        gh.check_rows.clear()
    elif change == 'wrong-app':
        gh.check_rows[0]['app']['id'] = 7
    elif change == 'new-attempt':
        gh.job_rows.clear()  # Check ids belong to attempt 1; latest attempt 2 has not published them.
    elif change == 'pending':
        gh.check_rows[0]['status'] = gh.job_rows[0]['status'] = 'in_progress'
        gh.check_rows[0]['conclusion'] = gh.job_rows[0]['conclusion'] = None
    elif change == 'all-skipped':
        for c in gh.check_rows + gh.job_rows:
            c['conclusion'] = 'skipped'
    elif change == 'wrong-head':
        gh.check_rows[0]['head_sha'] = MAIN
    elif change == 'wrong-run':
        gh.workflow['check_suite_id'] = 4
    elif change == 'failed':
        gh.check_rows[0]['conclusion'] = gh.job_rows[0]['conclusion'] = 'failure'
    try:
        assert gh.checks(MAPPING['repository'], HEAD)['state'] != 'complete'
    except watch.Problem as error:
        assert str(error) in {'check-provenance', 'check-run-provenance'}


# spec: ANYM-996
def test_required_skip_semantics_match_keeper_and_queue_is_silent():
    gh = Api()
    gh.check_rows[1]['conclusion'] = gh.job_rows[1]['conclusion'] = 'skipped'
    assert gh.checks(MAPPING['repository'], HEAD)['state'] == 'complete'
    gh.queue['isInMergeQueue'] = True
    result = gh.observe(MAPPING)
    assert result['state'] == 'pending' and result['proof']['queued']
    # autoMergeRequest is null while queued. It must never imply a dequeue.
    assert gh.queue['autoMergeRequest'] is None


# spec: ANYM-996
def test_current_head_conflict_dequeue_and_no_keeper_are_actionable():
    gh = Api()
    gh.pr['head']['sha'] = MAIN
    assert gh.observe(MAPPING)['reason'] == 'head-changed'
    gh.pr['head']['sha'], gh.pr['mergeable'] = HEAD, False
    assert gh.observe(MAPPING)['reason'] == 'merge-conflict'
    gh.pr['mergeable'] = True
    gh.queue['timelineItems']['nodes'] = [dict(__typename='RemovedFromMergeQueueEvent',
        reason='CI_FAILURE', createdAt='2026-11-13T08:00:00Z')]
    assert gh.observe(MAPPING)['reason'] == 'dequeued'
    gh.queue['timelineItems']['nodes'][0]['reason'] = 'MERGED'
    assert gh.observe(MAPPING)['state'] == 'pending'
    gh.queue['timelineItems']['nodes'].clear()
    gh.pr['labels'].clear()
    assert gh.observe(MAPPING)['reason'] == 'green-without-merge-owner'


def merged_fixture():
    gh = Api()
    gh.workflow['event'] = 'push'
    gh.pr.update(merged_at='2026-11-13T08:00:00Z', merge_commit_sha=MAIN, state='closed')
    gh.main_runs = [gh.workflow]
    # A coalesced descendant push includes the squash, proven by GitHub compare, not latest-run guessing.
    gh.comparisons[f'compare/{MAIN}...{HEAD}'] = {'status': 'ahead', 'merge_base_commit': {'sha': MAIN}}
    sync = dict(id=55, run_attempt=1, head_sha=HEAD, head_branch='main', event='workflow_run',
                path='.github/workflows/tiden-sync.yml', display_title='Tiden sync ' + MAIN,
                repository={'full_name': MAPPING['repository']}, status='completed', conclusion='success')
    gh.sync_runs = [sync]
    steps = [{'name': name, 'conclusion': 'success'} for name in (
        'Require the main branch and an exact commit', 'Require the commit on main',
        'Merge the named branch, link the marked tests, anchor the files and set the status')]
    gh.sync_jobs = [dict(name='Tiden sync', conclusion='success', run_id=55, run_attempt=1, steps=steps)]
    return gh


# spec: ANYM-996
def test_covering_main_and_exact_executed_sync_complete_without_release_claim():
    gh = merged_fixture()
    result = gh.observe(MAPPING)
    assert result['state'] == 'complete'
    assert result['proof']['main'] == MAIN and result['proof']['main_head'] == HEAD
    assert result['proof']['sync'] == 55
    assert 'deployed' not in result['proof']


# spec: ANYM-996
@pytest.mark.parametrize('change', ['unrelated-main', 'cancelled-main', 'failed-main', 'wrong-sync-sha',
                                   'skipped-sync', 'wrong-sync-attempt', 'missing-guard'])
def test_unrelated_or_cancelled_push_and_skipped_queue_sync_never_finish_source(change):
    gh = merged_fixture()
    if change == 'unrelated-main':
        gh.comparisons[f'compare/{MAIN}...{HEAD}']['status'] = 'diverged'
    elif change == 'cancelled-main':
        gh.workflow['conclusion'] = 'cancelled'
    elif change == 'failed-main':
        gh.workflow['conclusion'] = 'failure'
    elif change == 'wrong-sync-sha':
        gh.sync_runs[0]['display_title'] = 'Tiden sync ' + HEAD
    elif change == 'skipped-sync':
        gh.sync_jobs[0]['conclusion'] = 'skipped'
    elif change == 'wrong-sync-attempt':
        gh.sync_jobs[0]['run_attempt'] = 0
    elif change == 'missing-guard':
        gh.sync_jobs[0]['steps'].pop()
    assert gh.observe(MAPPING)['state'] != 'complete'


# spec: ANYM-996
def test_native_bb_reconciliation_respects_cli_bounds_and_verifies_actual_user_row():
    class Native:
        def json(self, argv):
            if 'queue' in argv:
                return [{'threadId': MAPPING['thread'], 'content': [{'type': 'text', 'text': 'body'}]}]
            if 'search' in argv:
                assert int(argv[argv.index('--limit') + 1]) <= 50
                return {'active': {'results': [{'thread': {'id': MAPPING['thread']},
                    'matches': [{'sourceKind': 'user_message', 'sourceSeq': 42}]}]},
                    'archived': {'results': []}}
            if 'log' in argv:
                return {'message': {'threadId': MAPPING['thread'], 'role': 'user', 'text': 'body'}}
            raise AssertionError(argv)
    assert watch.BB(Native()).reconcile(MAPPING, {'body': 'body', 'marker': 'marker'}) == 'delivered'


FAKE_CLI = '''#!/usr/bin/env python3
import json, os, pathlib, sys, time
root = pathlib.Path(os.environ['CI_WATCH_FIXTURE'])
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps([pathlib.Path(sys.argv[0]).name, args]) + '\\n')
if pathlib.Path(sys.argv[0]).name == 'gh':
    assert args[:3] == ['api', '--method', 'GET'], 'GitHub mutation forbidden'
    assert args[3].endswith('/pulls/7')
    time.sleep(0.1)  # Labelled fake read barrier for concurrent process proof; no observer sleep.
    out = dict(head=dict(sha='a'*40, repo=dict(full_name='GoSync-Inc/deedkit')),
               base=dict(ref='main'), merged_at=None, state='closed')
else:
    assert args[0] == 'thread'
    verb = args[1]
    bodypath = root / 'accepted.txt'
    if verb == 'show':
        out = dict(thread=dict(id='thr_worker', projectId='proj_test', archivedAt=None, deletedAt=None, status='idle'))
    elif verb == 'queue':
        out = []
    elif verb == 'search':
        assert int(args[args.index('--limit')+1]) <= 50
        matches = [dict(sourceKind='user_message', sourceSeq=42)] if bodypath.exists() else []
        out = dict(active=dict(results=[dict(thread=dict(id='thr_worker'), matches=matches)]), archived=dict(results=[]))
    elif verb == 'log':
        out = dict(message=dict(threadId='thr_worker', role='user', text=bodypath.read_text()))
    elif verb == 'tell':
        assert not bodypath.exists(), 'duplicate transport forbidden'
        bodypath.write_text(pathlib.Path(args[args.index('--message-file')+1]).read_text())
        if (root / 'lose-reply').exists():
            sys.exit(2)  # Transport accepted; CLI reply lost.
        out = dict(ok=True, threadId='thr_worker', delivery='steered')
    else:
        raise AssertionError(args)
print(json.dumps(out))
'''


def process_fixture(tmp_path):
    import os
    for binary in ('bb', 'gh'):
        command = tmp_path / binary
        command.write_text(FAKE_CLI)
        command.chmod(0o700)
    return {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
            'BB_CLI': str(tmp_path / 'bb'), 'CI_WATCH_FIXTURE': str(tmp_path)}


def invoke_tick(path, env):
    import subprocess
    import sys
    return subprocess.run([sys.executable, str(SCRIPT), 'tick', '--state', str(path)],
                          env=env, capture_output=True, text=True, timeout=10)


# spec: ANYM-996
def test_two_real_tick_processes_have_one_transport_and_only_read_github(tmp_path):
    import subprocess
    import sys
    path = register(tmp_path)
    env = process_fixture(tmp_path)
    argv = [sys.executable, str(SCRIPT), 'tick', '--state', str(path)]
    first = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    outputs = [p.communicate(timeout=10) for p in (first, second)]
    assert first.returncode == second.returncode == 0, outputs
    calls = [json.loads(line) for line in (tmp_path / 'calls.jsonl').read_text().splitlines()]
    assert len([c for c in calls if c[0] == 'bb' and c[1][1] == 'tell']) == 1
    assert record(path)['notice']['delivery'] == 'delivered'
    assert all(c[1][:3] == ['api', '--method', 'GET'] for c in calls if c[0] == 'gh')


# spec: ANYM-996
def test_real_process_accepted_lost_reply_recovers_after_restart(tmp_path):
    path = register(tmp_path)
    env = process_fixture(tmp_path)
    (tmp_path / 'lose-reply').touch()
    first = invoke_tick(path, env)
    assert first.returncode == 1 and 'handoff-unresolved' in first.stderr
    assert record(path)['notice']['delivery'] == 'needs-reconciliation'
    second = invoke_tick(path, env)
    assert second.returncode == 0, second.stderr
    assert record(path)['notice']['delivery'] == 'delivered'
    calls = [json.loads(line) for line in (tmp_path / 'calls.jsonl').read_text().splitlines()]
    assert len([c for c in calls if c[0] == 'bb' and c[1][1] == 'tell']) == 1


# spec: ANYM-996
@pytest.mark.parametrize('boundary', ['before-intent', 'after-intent', 'before-transport', 'after-transport'])
def test_process_death_at_durable_boundaries_is_recoverable_without_blind_resend(tmp_path, boundary):
    import subprocess
    import sys
    path = register(tmp_path)
    env = process_fixture(tmp_path)
    harness = '''
import importlib.util, os, pathlib, sys
s=importlib.util.spec_from_file_location('w',sys.argv[1]);w=importlib.util.module_from_spec(s);s.loader.exec_module(w)
path=pathlib.Path(sys.argv[2]); boundary=sys.argv[3]; original=w.save

def save(path, data):
    item=next(iter(data['records'].values())); delivery=item.get('notice',{}).get('delivery')
    if boundary=='before-intent' and delivery=='prepared': os._exit(71)
    original(path,data)
    if boundary=='after-intent' and delivery=='prepared': os._exit(71)
    if boundary=='before-transport' and delivery=='dispatching': os._exit(71)
w.save=save
c=w.Commands(); bb=w.BB(c); transport=bb.send

def send(mapping, notice):
    transport(mapping, notice)
    if boundary=='after-transport': os._exit(71)
bb.send=send
w.tick(path,w.GitHub(c),bb)
'''
    died = subprocess.run([sys.executable, '-c', harness, str(SCRIPT), str(path), boundary],
                          env=env, capture_output=True, text=True, timeout=10)
    assert died.returncode == 71, died.stderr
    resumed = invoke_tick(path, env)
    state = record(path)
    if boundary == 'before-transport':
        assert resumed.returncode == 1
        assert state['notice']['delivery'] == 'needs-reconciliation'
        assert not (tmp_path / 'accepted.txt').exists()
    else:
        assert resumed.returncode == 0, resumed.stderr
        assert state['notice']['delivery'] == 'delivered'
        assert (tmp_path / 'accepted.txt').exists()
    calls = [json.loads(line) for line in (tmp_path / 'calls.jsonl').read_text().splitlines()]
    assert len([c for c in calls if c[0] == 'bb' and c[1][1] == 'tell']) <= 1


# spec: ANYM-996
def test_repeat_registration_during_unknown_delivery_is_still_idempotent(tmp_path):
    bb, gh = BB(), GitHub()
    bb.lost_reply = True
    gh.result = {'state': 'attention', 'reason': 'check-failed'}
    path = register(tmp_path, bb)
    watch.tick(path, gh, bb)
    original = path.read_bytes()
    watch.register(path, MAPPING, bb)
    assert path.read_bytes() == original
    with pytest.raises(watch.Problem, match='unresolved-notice'):
        watch.register(path, {**MAPPING, 'head': MAIN}, bb)


# spec: ANYM-996
def test_explicit_owner_transfer_retains_receipt_and_refuses_unknown_transport(tmp_path):
    bb, gh = BB(), GitHub()
    bb.lost_reply = True
    gh.result = {'state': 'attention', 'reason': 'check-failed'}
    path = register(tmp_path, bb)
    watch.tick(path, gh, bb)
    key = next(iter(watch.load(path)['records']))
    target = {**MAPPING, 'thread': 'thr_newowner'}
    with pytest.raises(watch.Problem, match='unresolved-notice'):
        watch.transfer(path, key, 'thr_worker', target, bb)
    watch.tick(path, gh, bb)
    old_notice = record(path)['notice']
    watch.transfer(path, key, 'thr_worker', target, bb)
    assert record(path)['owner_history'][0]['notice'] == old_notice
    assert record(path)['mapping']['thread'] == 'thr_newowner'
    watch.tick(path, gh, bb)
    assert record(path)['notice']['id'] != old_notice['id']
    assert len(bb.sent) == 2  # Explicit new destination, one event to each owner.


# spec: ANYM-996
def test_malformed_read_is_visible_once_and_budget_exhaustion_is_only_deferred(tmp_path):
    class Broken(GitHub):
        def observe(self, mapping):
            raise KeyError('missing response field')
    bb = BB()
    path = register(tmp_path, bb)
    watch.tick(path, Broken(), bb)
    assert record(path)['problem'] == 'invalid-evidence'
    assert record(path)['notice']['delivery'] == 'prepared'
    watch.tick(path, Broken(), bb)
    assert len(bb.sent) == 1 and record(path)['state'] == 'action_required'
    watch.register(path, {**MAPPING, 'head': MAIN}, bb)
    class Busy(GitHub):
        def observe(self, mapping):
            raise watch.Problem('tick-budget-exhausted')
    watch.tick(path, Busy(), bb)
    current = next(r for r in watch.load(path)['records'].values() if r['mapping']['head'] == MAIN)
    assert current['state'] == 'watching' and 'notice' not in current


# spec: ANYM-996
def test_an_assistant_echo_or_other_thread_never_acknowledges_transport():
    class Echo:
        def json(self, argv):
            if 'queue' in argv:
                return []
            if 'search' in argv:
                return {'active': {'results': [
                    {'thread': {'id': 'thr_other'}, 'matches': [{'sourceKind': 'user_message', 'sourceSeq': 8}]},
                    {'thread': {'id': MAPPING['thread']}, 'matches': [
                        {'sourceKind': 'assistant_message', 'sourceSeq': 9},
                        {'sourceKind': 'user_message', 'sourceSeq': 10}]}]}, 'archived': {'results': []}}
            if 'log' in argv:
                return {'message': {'threadId': MAPPING['thread'], 'role': 'assistant', 'text': 'body'}}
            raise AssertionError(argv)
    assert watch.BB(Echo()).reconcile(MAPPING, {'marker': 'marker', 'body': 'body'}) == 'unknown'
