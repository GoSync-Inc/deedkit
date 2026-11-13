#!/usr/bin/env python3
"""Bounded read-only CI lifecycle observer and durable BB outbox (ANYM-996).

One invocation, no sleep/retry/merge loop. The existing BB script scheduler owns the next tick.
Only BB thread tell is a transport mutation; every GitHub operation is a read.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote

VERSION = 1
TERMINAL = {'complete', 'action_required', 'superseded', 'handoff_problem'}
UNRESOLVED = {'dispatching', 'needs-reconciliation', 'queued'}
SHA = re.compile(r'[0-9a-f]{40}')
PR_QUERY = '''query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){
pullRequest(number:$number){headRefOid isInMergeQueue autoMergeRequest{enabledAt}
timelineItems(last:100,itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT,LABELED_EVENT]){nodes{
__typename ... on LabeledEvent{createdAt label{name}}
... on RemovedFromMergeQueueEvent{createdAt reason beforeCommit{oid}}}}}}}'''


class Problem(RuntimeError):
    """Fixed diagnostic codes; never copy CLI auth diagnostics into notifications."""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def identifier(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load(path):
    if not path.exists():
        return {'version': VERSION, 'records': {}, 'cursor': 0}
    if path.is_symlink():
        raise Problem('registry-symlink')
    data = json.loads(path.read_text())
    if data.get('version') != VERSION or not isinstance(data.get('records'), dict):
        raise Problem('registry-format')
    return data


def save(path, data):
    """Fsync the new inode and directory; lock lives in a separate never-replaced inode."""
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(canonical(data) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + '.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Problem('tick-busy') from None
        yield
    finally:
        os.close(fd)


def validate(mapping):
    fields = {'repository', 'pr', 'head', 'thread', 'project', 'phase', 'remaining_action',
              'action_owner', 'deadline'}
    if set(mapping) != fields:
        raise Problem('registration-fields')
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', mapping['repository'])
            or type(mapping['pr']) is not int or mapping['pr'] <= 0
            or not SHA.fullmatch(mapping['head'])
            or not re.fullmatch(r'thr_[a-z0-9]+', mapping['thread'])
            or not re.fullmatch(r'proj_[a-z0-9]+', mapping['project'])
            or mapping['phase'] not in {'merge', 'source'}):
        raise Problem('registration-identity')
    action = mapping['remaining_action']
    if not isinstance(action, str) or not 1 <= len(action) <= 400 or any(ord(c) < 32 for c in action):
        raise Problem('registration-action')
    owner = 'keeper-and-ci' if action == 'none' else mapping['thread']
    if mapping['action_owner'] != owner:
        raise Problem('registration-action-owner')
    try:
        if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', mapping['deadline']):
            raise ValueError()
        deadline = dt.datetime.fromisoformat(mapping['deadline'].replace('Z', '+00:00'))
        if deadline.tzinfo is None or deadline <= dt.datetime.now(dt.timezone.utc):
            raise ValueError()
    except (ValueError, TypeError):
        raise Problem('registration-deadline') from None


def register(path, mapping, bb):
    validate(mapping)
    bb.owner(mapping)
    with locked(path):
        data = load(path)
        identity = {k: mapping[k] for k in ('repository', 'pr', 'head')}
        key = identifier(identity)
        related = [r for r in data['records'].values()
                   if all(r['mapping'][k] == mapping[k] for k in ('repository', 'pr'))]
        for item in related:
            previous = item['mapping']
            if (previous['thread'], previous['project']) != (mapping['thread'], mapping['project']):
                raise Problem('owner-conflict: explicit root handoff required')
        if key in data['records']:
            if data['records'][key]['mapping'] != mapping:
                raise Problem('registration-conflict: exact mapping already registered')
            return key
        if any(item.get('notice') and item['notice']['delivery'] != 'delivered' for item in related):
            raise Problem('unresolved-notice: reconcile before changing registration')
        for item in related:
            if item['state'] not in TERMINAL:
                item['state'] = 'superseded'
                item['superseded_at'] = now()
        data['records'][key] = {'mapping': mapping, 'state': 'watching', 'registered_at': now()}
        save(path, data)
        return key


def transfer(path, key, previous_thread, mapping, bb):
    """Explicit per-record owner handoff; unresolved transport must be reconciled first."""
    validate(mapping)
    bb.owner(mapping)
    with locked(path):
        data = load(path)
        item = data['records'][key]
        previous = item['mapping']
        if previous['thread'] != previous_thread or mapping['thread'] == previous_thread:
            raise Problem('transfer-owner-conflict')
        if any(previous[k] != mapping[k] for k in previous if k not in {'thread', 'project', 'action_owner'}):
            raise Problem('transfer-scope-conflict')
        if item.get('notice') and item['notice']['delivery'] != 'delivered':
            raise Problem('unresolved-notice: reconcile before transfer')
        item.setdefault('owner_history', []).append({'mapping': previous, 'state': item['state'],
                                                     'notice': item.pop('notice', None), 'at': now()})
        item['mapping'] = mapping
        if item['state'] != 'complete':
            item['state'] = 'watching'
        save(path, data)


class Commands:
    def __init__(self, seconds=90):
        self.deadline = time.monotonic() + seconds

    def run(self, argv, *, text=True):
        left = self.deadline - time.monotonic()
        if left < 1:
            raise Problem('tick-budget-exhausted')
        try:
            result = subprocess.run(argv, capture_output=True, text=text, timeout=min(left, 10))
        except (subprocess.TimeoutExpired, OSError):
            raise Problem('command-unreadable') from None
        if result.returncode:
            raise Problem('command-unreadable')
        if len(result.stdout) > 8_000_000:
            raise Problem('command-output-bound')
        return result.stdout

    def json(self, argv):
        try:
            return json.loads(self.run(argv))
        except (ValueError, TypeError):
            raise Problem('command-invalid-json') from None


class BB:
    def __init__(self, commands):
        self.commands = commands
        self.binary = os.environ.get('BB_CLI', 'bb')

    def call(self, *args):
        return self.commands.json([self.binary, 'thread', *args, '--json'])

    def owner(self, mapping):
        thread = self.call('show', mapping['thread'])['thread']
        if (thread['id'] != mapping['thread'] or thread['projectId'] != mapping['project']
                or thread.get('archivedAt') or thread.get('deletedAt')
                or thread['status'] in {'pending', 'starting', 'stopping'}):
            raise Problem('owner-unavailable')

    def reconcile(self, mapping, notice):
        """Search is only an index hint; verify the actual user row and also inspect the queue."""
        queued = self.call('queue', 'list', mapping['thread'])
        found_queue = any(q['threadId'] == mapping['thread'] and ''.join(
            part.get('text', '') for part in q['content'] if part.get('type') == 'text') == notice['body']
            for q in queued)
        results = self.call('search', notice['marker'], '--limit', '50')
        for section in ('active', 'archived'):
            for result in results[section]['results']:
                if result['thread']['id'] != mapping['thread']:
                    continue
                for match in result['matches']:
                    if match.get('sourceKind') != 'user_message' or not match.get('sourceSeq'):
                        continue
                    row = self.call('log', mapping['thread'], '--message', str(match['sourceSeq']))['message']
                    if (row.get('threadId') == mapping['thread'] and row.get('role') == 'user'
                            and row.get('text') == notice['body']):
                        return 'delivered'
        return 'queued' if found_queue else 'unknown'

    def send(self, mapping, notice):
        # No native idempotency key exists. The caller fsyncs dispatching before this call.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as body:
            body.write(notice['body'])
            body.flush()
            response = self.call('tell', mapping['thread'], '--mode', 'auto', '--message-file', body.name)
        if response.get('ok') is not True or response.get('threadId') != mapping['thread']:
            raise Problem('transport-unknown')


def notice_for(item, observation):
    mapping = item['mapping']
    event = {'repository': mapping['repository'], 'pr': mapping['pr'], 'head': mapping['head'],
             'thread': mapping['thread'], 'project': mapping['project'],
             'phase': mapping['phase'], 'reason': observation['reason'],
             'proof': observation.get('proof', {}), 'remaining_action': mapping['remaining_action'],
             'action_owner': mapping['action_owner'], 'retry_authorized': False}
    event_id = identifier(event)
    marker = '[ci-watch:' + event_id + ']'
    body = marker + '\n' + canonical(event) + '\nRead the durable receipt and exact-head evidence. No retry or bypass is authorized.'
    if len(body) > 4096:
        raise Problem('notice-bound')
    return {'id': event_id, 'marker': marker, 'body': body, 'delivery': 'prepared', 'created_at': now()}


def dispatch(path, data, item, bb):
    notice, mapping = item['notice'], item['mapping']
    bb.owner(mapping)
    if notice['delivery'] == 'prepared':
        notice['delivery'] = 'dispatching'
        save(path, data)
        try:
            bb.send(mapping, notice)
        except Problem:
            notice['delivery'] = 'needs-reconciliation'
            save(path, data)
            return
    outcome = bb.reconcile(mapping, notice)
    notice['delivery'] = 'needs-reconciliation' if outcome == 'unknown' else outcome
    if outcome == 'delivered':
        item['state'] = 'action_required'
        notice['delivered_at'] = now()
    save(path, data)


def tick(path, gh, bb):
    """At most four records per tick; unresolved transport is reconciled, never resent."""
    try:
        with locked(path):
            data = load(path)
            records = list(data['records'].values())
            if not records:
                return
            start = data.get('cursor', 0) % len(records)
            for offset in range(min(4, len(records))):
                item = records[(start + offset) % len(records)]
                data['cursor'] = (start + offset + 1) % len(records)
                if item['state'] in TERMINAL:
                    continue
                try:
                    bb.owner(item['mapping'])
                    if item.get('notice'):
                        dispatch(path, data, item, bb)
                        continue
                    observation = gh.observe(item['mapping'])
                    item['last_observation'] = observation
                    item['observed_at'] = now()
                    if observation['state'] == 'pending' and now() >= item['mapping']['deadline']:
                        observation = {'state': 'attention', 'reason': 'deadline',
                                       'proof': observation.get('proof', {})}
                    if observation['state'] == 'complete':
                        item['proof'] = observation.get('proof', {})
                        if item['mapping']['remaining_action'] == 'none':
                            item['state'], item['completed_at'] = 'complete', now()
                        else:
                            observation = {**observation, 'state': 'attention', 'reason': 'remaining-action'}
                    if observation['state'] == 'attention':
                        item['state'] = 'notifying'
                        item['notice'] = notice_for(item, observation)
                    save(path, data)
                    if item.get('notice'):
                        dispatch(path, data, item, bb)
                except (Problem, KeyError, TypeError, ValueError) as error:
                    code = str(error) if isinstance(error, Problem) else 'invalid-evidence'
                    if code == 'tick-budget-exhausted':
                        data['cursor'] = (start + offset) % len(records)
                        break
                    if code == 'owner-unavailable':
                        item['state'], item['problem'] = 'handoff_problem', code
                    elif item.get('notice'):
                        if item['notice']['delivery'] != 'prepared':
                            item['notice']['delivery'] = 'needs-reconciliation'
                        item['problem'] = code
                    else:
                        item['state'] = 'notifying'
                        item['notice'] = notice_for(item, {'state': 'attention', 'reason': 'observation-unreadable'})
                        item['problem'] = code
                    save(path, data)
            save(path, data)
    except Problem as error:
        if str(error) != 'tick-busy':
            raise


class GitHub:
    def __init__(self, commands):
        self.commands = commands
        self.cache = {}

    def api(self, repository, path):
        key = repository + '/' + path
        if key not in self.cache:
            self.cache[key] = self.commands.json(['gh', 'api', '--method', 'GET', 'repos/' + key])
        return self.cache[key]

    def pages(self, repository, path, field=None):
        result = []
        for page in range(1, 6):
            value = self.api(repository, path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
            rows = value[field] if field else value
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise Problem('github-page-bound')

    def required(self, repository):
        rules = self.api(repository, 'rules/branches/main')
        required = [(c['context'], c.get('integration_id')) for r in rules
                    if r['type'] == 'required_status_checks'
                    for c in r['parameters']['required_status_checks']]
        if not {'CI gate', 'checks / verify'} <= {name for name, _ in required}:
            raise Problem('required-checks-unreadable')
        return required

    def run(self, repository, run_id):
        run = self.api(repository, f'actions/runs/{run_id}')
        if run['repository']['full_name'] != repository or run['id'] != run_id:
            raise Problem('run-provenance')
        return run

    def jobs(self, repository, run):
        return self.pages(repository, f'actions/runs/{run["id"]}/attempts/{run["run_attempt"]}/jobs', 'jobs')

    def checks(self, repository, head, expected_run=None):
        """Keeper's newest name/app semantics, with actual latest-attempt job ownership proof."""
        selected, runs = [], {}
        for name, app in self.required(repository):
            checks = self.pages(repository, f'commits/{head}/check-runs?filter=all&check_name={quote(name, safe="")}', 'check_runs')
            checks = [c for c in checks if c['name'] == name and (app is None or c['app']['id'] == app)]
            if not checks:
                return {'state': 'pending', 'proof': {'missing': name}}
            check = max(checks, key=lambda c: c['id'])
            url = re.fullmatch(r'https://github.com/' + re.escape(repository)
                               + r'/actions/runs/([0-9]+)/job/([0-9]+)', check.get('details_url') or '')
            if not url or check['head_sha'] != head or int(url[2]) != check['id']:
                raise Problem('check-provenance')
            run_id = int(url[1])
            run = self.run(repository, run_id)
            if (run['head_sha'] != head or run['path'] != '.github/workflows/ci.yml'
                    or run['check_suite_id'] != check['check_suite']['id']
                    or run['event'] != ('push' if expected_run is not None else 'pull_request')
                    or expected_run is not None and run_id != expected_run):
                raise Problem('check-run-provenance')
            if run_id not in runs:
                runs[run_id] = (run, self.jobs(repository, run))
            jobs = [j for j in runs[run_id][1] if j['id'] == check['id'] and j['name'] == name]
            if len(jobs) != 1:
                return {'state': 'pending', 'proof': {'stale_attempt': run['run_attempt'], 'run': run_id}}
            job = jobs[0]
            if job['status'] != check['status'] or job['conclusion'] != check['conclusion']:
                return {'state': 'pending', 'proof': {'updating': name}}
            selected.append(check)
        proof = {'head': head, 'runs': [{'run': r['id'], 'attempt': r['run_attempt']} for r, _ in runs.values()],
                 'checks': [c['id'] for c in selected]}
        failed = [c for c in selected if c['status'] == 'completed' and c['conclusion'] not in {'success', 'skipped'}]
        if failed:
            return {'state': 'attention', 'reason': 'check-failed', 'proof': proof}
        if any(c['status'] != 'completed' for c in selected):
            return {'state': 'pending', 'proof': proof}
        if any(c['conclusion'] != 'success' for c in selected if c['name'] == 'CI gate'):
            return {'state': 'attention', 'reason': 'gate-not-success', 'proof': proof}
        return {'state': 'complete', 'proof': proof}

    def graph(self, repository, number):
        owner, name = repository.split('/')
        result = self.commands.json(['gh', 'api', 'graphql', '-f', 'query=' + PR_QUERY,
                                     '-f', 'owner=' + owner, '-f', 'name=' + name, '-F', f'number={number}'])
        if result.get('errors'):
            raise Problem('queue-unreadable')
        return result['data']['repository']['pullRequest']

    def covering_main(self, repository, target, merged_at):
        runs = self.pages(repository, 'actions/workflows/ci.yml/runs?event=push&branch=main&created='
                          + quote('>=' + merged_at, safe=''), 'workflow_runs')
        covering = []
        for run in sorted(runs, key=lambda r: (r['created_at'], r['id'])):
            if (run['event'] != 'push' or run['head_branch'] != 'main'
                    or run['repository']['full_name'] != repository
                    or run['path'] != '.github/workflows/ci.yml'):
                raise Problem('main-run-provenance')
            comparison = self.api(repository, f'compare/{target}...{run["head_sha"]}')
            if comparison['status'] in {'identical', 'ahead'} and comparison['merge_base_commit']['sha'] == target:
                covering.append(run)
        # Cancelled/coalesced push may be covered by the next descendant push. A real failure is actionable.
        for candidate in covering:
            run = self.run(repository, candidate['id'])
            if run['conclusion'] == 'cancelled':
                continue
            if run['status'] != 'completed':
                return None
            if run['conclusion'] != 'success':
                return {'state': 'attention', 'reason': 'main-failed',
                        'proof': {'main': target, 'run': run['id'], 'attempt': run['run_attempt']}}
            checks = self.checks(repository, run['head_sha'], run['id'])
            return {**checks, 'main': target, 'main_run': run['id'], 'main_head': run['head_sha']}
        return None

    def sync(self, repository, target, merged_at):
        runs = self.pages(repository, 'actions/workflows/tiden-sync.yml/runs?branch=main&created='
                          + quote('>=' + merged_at, safe=''), 'workflow_runs')
        candidates = [r for r in runs if r['display_title'] == 'Tiden sync ' + target
                      and r['event'] in {'workflow_run', 'workflow_dispatch'}]
        if not candidates:
            return {'state': 'pending', 'proof': {'main': target, 'sync': 'missing'}}
        run = self.run(repository, max(candidates, key=lambda r: r['id'])['id'])
        if (run['path'] != '.github/workflows/tiden-sync.yml' or run['head_branch'] != 'main'
                or run['display_title'] != 'Tiden sync ' + target
                or run['event'] not in {'workflow_run', 'workflow_dispatch'}):
            raise Problem('sync-provenance')
        if run['status'] != 'completed':
            return {'state': 'pending', 'proof': {'main': target, 'sync': run['id']}}
        jobs = self.jobs(repository, run)
        real = [j for j in jobs if j['name'] == 'Tiden sync' and j['conclusion'] == 'success'
                and j['run_id'] == run['id'] and j['run_attempt'] == run['run_attempt']]
        guards = {'Require the main branch and an exact commit', 'Require the commit on main',
                  'Merge the named branch, link the marked tests, anchor the files and set the status'}
        proof = {'main': target, 'sync': run['id'], 'sync_attempt': run['run_attempt']}
        if (run['conclusion'] != 'success' or len(real) != 1 or not guards <= {
                s['name'] for s in real[0].get('steps', []) if s['conclusion'] == 'success'}):
            return {'state': 'attention', 'reason': 'exact-sync-not-executed', 'proof': proof}
        # Actual guarded main job, not a successful workflow whose queue-triggered sync job was skipped.
        return {'state': 'complete', 'proof': proof}

    def observe(self, mapping):
        repo, number, head = (mapping[k] for k in ('repository', 'pr', 'head'))
        pr = self.api(repo, f'pulls/{number}')
        if pr['head']['sha'] != head:
            return {'state': 'attention', 'reason': 'head-changed', 'proof': {'actual_head': pr['head']['sha']}}
        if (pr['base']['ref'] != 'main' or not pr['head']['repo']
                or pr['head']['repo']['full_name'] != repo):
            raise Problem('pull-request-provenance')
        if pr['merged_at']:
            target = pr['merge_commit_sha']
            if not SHA.fullmatch(target):
                raise Problem('merge-sha-unreadable')
            if mapping['phase'] == 'merge':
                return {'state': 'complete', 'proof': {'main': target, 'merged_at': pr['merged_at']}}
            main = self.covering_main(repo, target, pr['merged_at'])
            if main is None:
                return {'state': 'pending', 'proof': {'main': target, 'ci': 'missing-or-pending'}}
            if main['state'] != 'complete':
                return main
            sync = self.sync(repo, target, pr['merged_at'])
            sync['proof'].update({k: main[k] for k in ('main_run', 'main_head')})
            sync['proof']['main_checks'] = main['proof']
            return sync
        if pr['state'] != 'open':
            return {'state': 'attention', 'reason': 'closed-unmerged'}
        graph = self.graph(repo, number)
        if graph['headRefOid'] != head:
            raise Problem('head-read-race')
        if pr.get('mergeable') is False:
            return {'state': 'attention', 'reason': 'merge-conflict'}
        checks = self.checks(repo, head)
        if checks['state'] != 'complete':
            return checks
        if graph['isInMergeQueue']:
            return {'state': 'pending', 'proof': {**checks['proof'], 'queued': True}}
        suites = self.pages(repo, f'commits/{head}/check-suites', 'check_suites')
        if not suites:
            raise Problem('head-time-unreadable')
        head_at = min(s['created_at'] for s in suites)
        events = graph['timelineItems']['nodes']
        removed = [e for e in events if e['__typename'] == 'RemovedFromMergeQueueEvent'
                   and e['createdAt'] >= head_at]
        labels = [e['createdAt'] for e in events if e['__typename'] == 'LabeledEvent'
                  and e['label']['name'] == 'ship']
        latest = max(removed, key=lambda e: e['createdAt'], default=None)
        if latest and latest['reason'].lower() == 'merged':
            return {'state': 'pending', 'proof': {'queue_exit': 'merged'}}
        if latest and latest['createdAt'] >= max(labels, default=''):
            return {'state': 'attention', 'reason': 'dequeued',
                    'proof': {'at': latest['createdAt'], 'queue_reason': latest['reason']}}
        covered = (graph['autoMergeRequest'] is not None or
                   not pr['draft'] and any(label['name'] == 'ship' for label in pr['labels']))
        if not covered:
            return {'state': 'attention', 'reason': 'green-without-merge-owner', 'proof': checks['proof']}
        return {'state': 'pending', 'proof': {**checks['proof'], 'merge_owner': 'keeper'}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('register', 'transfer', 'tick', 'status'), nargs='?', default='tick')
    parser.add_argument('--state', type=Path, default=os.environ.get('CI_WATCH_STATE'))
    parser.add_argument('--mapping', type=Path)
    parser.add_argument('--record')
    parser.add_argument('--from-thread')
    args = parser.parse_args()
    if args.state is None or not args.state.is_absolute():
        parser.error('--state must be an absolute private registry path')
    commands = Commands()
    bb = BB(commands)
    if args.command == 'register':
        if args.mapping is None:
            parser.error('register requires --mapping')
        key = register(args.state, json.loads(args.mapping.read_text()), bb)
        print(canonical({'record': key, 'mapping': load(args.state)['records'][key]['mapping']}))
    elif args.command == 'transfer':
        if not args.mapping or not args.record or not args.from_thread:
            parser.error('transfer requires --mapping, --record and --from-thread')
        transfer(args.state, args.record, args.from_thread, json.loads(args.mapping.read_text()), bb)
        print(canonical(load(args.state)['records'][args.record]))
    elif args.command == 'status':
        print(canonical(load(args.state)))
    else:
        tick(args.state, GitHub(commands), bb)
        if any(r['state'] == 'handoff_problem' or r.get('notice', {}).get('delivery') in UNRESOLVED | {'prepared'}
               for r in load(args.state)['records'].values()):
            raise Problem('handoff-unresolved: inspect registry; never resend without proof')
        # BB captures this as a silent skipped tick; no model call.
        print('{"wakeAgent":false}')


if __name__ == '__main__':
    try:
        main()
    except (Problem, KeyError, TypeError, ValueError) as error:
        print('ci-watch: ' + (str(error) if isinstance(error, Problem) else 'invalid-evidence'), file=sys.stderr)
        raise SystemExit(1)
