#!/usr/bin/env python3
"""Merge keeper: enqueue reviewed, green `ship` pull requests so nothing green waits for a person or the morning.

Runs every 5 minutes as a launchd daemon on the Mac mini with the host's existing `gh` login, and as the
report-only GitHub Actions workflow `.github/workflows/merge-keeper.yml` (docs/operations/parallel-development.md,
Merge keeper). Every GitHub call goes through `gh`, which takes `GH_TOKEN` from the environment or the host login,
so the token source is the caller's choice.

For each open, non-draft, same-repository pull request into `main` that carries the label `ship`:
  1. An approval must be recorded for the full head SHA by an account whose effective repository permission is
     write, maintain or admin (collaborators/<login>/permission): an APPROVE review on that commit, or an unedited
     comment, posted after the head arrived, with the line `Review: orchestrator diff read <40-hex sha>`. The
     description is never read for it. An approval of an older SHA: no enqueue, one comment.
  2. Every required check of the live `main` rules (name and app) has its newest run on the head completed: `CI
     gate` success, the others success or skipped (`CI gate` validates their skip; never all skipped).
  3. The new migrations must take the next free numbers on `main`; otherwise one comment with the exact rename.
  4. A merge-queue removal for failed checks gets one comment with the failing jobs. The keeper re-enqueues it
     only when RETRY_FAILED_CHECKS is on (once, RETRY_AFTER minutes after the removal); it is off while the
     harness `wait-merge.sh` keeps its own process-local retry, so the queue has one retry actor (Sol r3 M3). Two
     failures on a head: one comment and the keeper stops. A queue conflict waits until main moves and is retried
     once (wait-merge.sh never retries conflicts); a removal by hand holds the pull request until a new head or the
     `ship` label is added again. The counts come from the pull request's timeline.
Comments carry an HTML marker and are posted once per head. `--dry-run` writes nothing and prints every decision.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.parse import quote

REPOSITORY = os.environ.get('MERGE_KEEPER_REPOSITORY', 'GoSync-Inc/deedkit')
BASE = 'main'
LABEL = 'ship'
GATE = 'CI gate'
MIGRATIONS = 'services/messaging/messaging_service/migrations'
MIGRATION = re.compile(r'^(\d{3})_[A-Za-z0-9_]+\.sql$')
APPROVAL_LINE = re.compile(r'^\s*Review:\s*orchestrator diff read\s+([0-9a-f]{7,40})\b', re.I | re.M)
KEEPER_BOT = 'github-actions[bot]'
RETRY_AFTER = 3  # minutes a failed-checks removal waits before the keeper re-enqueues
# Off until the harness wait-merge.sh skips its re-enqueue for `ship` pull requests: one retry actor per queue.
RETRY_FAILED_CHECKS = False
WRITE = {'admin', 'maintain', 'write'}  # effective repository permissions that may approve
MARK_PREFIX = '<!-- merge-keeper:'
MARK = MARK_PREFIX + '{} -->'
QUEUE_DONE = {'merged'}
QUEUE_RETRYLESS = {'manual', 'merge_conflict'}
HEARTBEAT_VARIABLE = 'MERGE_KEEPER_HEARTBEAT'
STALE_MINUTES = 20
CALL_TIMEOUT = 60


@dataclass
class Facts:
    """What the decision needs about one pull request, read from GitHub (collect) or built by a test."""
    number: int
    head: str
    open: bool = True
    draft: bool = False
    same_repository: bool = True
    base: str = BASE
    labels: tuple = (LABEL,)
    reviews: list = field(default_factory=list)  # {'state','commit_id','user'}
    comments: list = field(default_factory=list)  # {'body','user','created_at','updated_at'}
    writers: frozenset = frozenset()  # logins among the authors above with write, maintain or admin permission
    queued: bool = False
    auto_merge: bool = False
    mergeable: bool | None = True
    head_at: str = ''  # first check suite on the head: when this head arrived
    main_at: str = ''  # when main's tip was committed: a queue conflict is retried only after main moved
    now: str = '9999-12-31T00:00:00Z'  # this tick's time
    removals: list = field(default_factory=list)  # {'at','reason','group'} oldest first
    labeled_at: str = ''  # last time `ship` was added
    required: tuple = ('checks / verify', GATE)
    checks: dict = field(default_factory=dict)  # name -> {'status','conclusion','started_at'} latest run on the head
    added_migrations: tuple = ()  # file names this pull request adds under MIGRATIONS
    main_migrations: tuple = ()  # file names on main
    failing_jobs: list = field(default_factory=list)  # jobs of the last failed group run: 'name <url>'


@dataclass
class Decision:
    action: str  # enqueue | hold | skip
    reason: str
    comment_key: str = ''
    comment: str = ''


def marked(facts, key):
    """A dedup marker counts only from a write-access account or the Actions bot, so an outsider cannot mute one."""
    return any(MARK.format(key) in (c.get('body') or '') for c in facts.comments
               if c.get('user') in facts.writers or c.get('user') == KEEPER_BOT)


def approved_shas(facts, authorizing=True):
    """SHAs approved by a write-access account: APPROVE reviews and diff-read comments. Authorizing comments must
    be unedited and posted after the head arrived; `authorizing=False` keeps every one, for the stale notice."""
    shas = [r['commit_id'] for r in facts.reviews
            if r.get('state') == 'APPROVED' and r.get('user') in facts.writers and r.get('commit_id')]
    for c in facts.comments:
        fresh = c.get('updated_at') == c.get('created_at') and (c.get('created_at') or '') >= (facts.head_at or '')
        if c.get('user') in facts.writers and (fresh or not authorizing):
            shas += [sha.lower() for sha in APPROVAL_LINE.findall(c.get('body') or '')]
    return shas


def head_approved(facts):
    """Only the full head SHA approves: an abbreviation or an approval posted before this head arrived does not."""
    return facts.head.lower() in approved_shas(facts)


def migration_plan(added, on_main):
    """None when the added migrations take the next free numbers on main, else the exact (old, new) renames."""
    numbers = sorted(int(name[:3]) for name in on_main if MIGRATION.match(name))
    top = numbers[-1] if numbers else 0
    mine = sorted((n for n in added if MIGRATION.match(n)), key=lambda n: (int(n[:3]), n))
    wanted = [f'{top + i + 1:03d}{name[3:]}' for i, name in enumerate(mine)]
    return None if wanted == mine else list(zip(mine, wanted))


def migration_moves(plan):
    """`git mv` lines that never move onto another file of the plan: through a temporary name when they would."""
    moves = [(old, new) for old, new in plan if old != new]
    path = f'{MIGRATIONS}/{{}}'.format
    if not {new for _, new in moves} & {old for old, _ in moves}:
        return [f'git mv {path(old)} {path(new)}' for old, new in moves]
    return ([f'git mv {path(old)} {path(old + ".renumber")}' for old, _ in moves]
            + [f'git mv {path(old + ".renumber")} {path(new)}' for old, new in moves])


def migration_comment(facts, plan):
    lines = migration_moves(plan)
    pins = []
    for old, new in plan:
        o, n = int(old[:3]), int(new[:3])
        if o != n:
            pins.append(f'- schema {o} -> {n}: `store.py` `RUNTIME_SCHEMA={n}`, `SCHEMAS=range(2,{n + 1})` in '
                        f'`backup.py` and `backup_r2_verifier.py`, `V{o}_TABLES` -> `V{n}_TABLES`, the schema-version '
                        f'test expectations and the docs that name schema {o} '
                        f"(`git grep -nwE '0?{o}' -- services tests '*.md'`)")
    main_top = max((int(n[:3]) for n in facts.main_migrations if MIGRATION.match(n)), default=0)
    return '\n'.join([
        MARK.format(f'migration:{facts.head}'),
        f'Merge keeper: not enqueued. The new migration numbers must follow main\'s newest, {main_top:03d}, with no '
        'gap or collision (docs/operations/parallel-development.md, Merge keeper). Rename:', '', '```sh', *lines, '```',
        '', 'Then update the schema pins in the same commit (the keeper does not rewrite them: they include comments, '
        'table-set names and test expectations, ANYM-848 #770 changed 15 files):', *pins,
        '', 'Rerun the touched tests, push; the keeper re-reads the new head.'])


def required_state(facts):
    """'' when every required check's newest run on the head passed, else why not. A draft's run counts: this
    repository's drafts run full CI, and `CI gate` fails any skip it cannot validate."""
    if not facts.required:
        return 'no required checks read from the rules'
    states = []
    for name in facts.required:
        run = facts.checks.get(name)
        if not run:
            return f'{name}: no run on the head'
        if run.get('status') != 'completed':
            return f'{name}: {run.get("status")}'
        states.append((name, run.get('conclusion')))
    for name, conclusion in states:
        if name == GATE and conclusion != 'success':
            return f'{name}: {conclusion}'
        if conclusion not in ('success', 'skipped'):
            return f'{name}: {conclusion}'
    if all(c == 'skipped' for _, c in states):
        return 'every required check skipped'
    if GATE not in facts.required and any(c == 'skipped' for _, c in states):
        return 'a skipped required check and no gate check to validate it'
    return ''


def minutes_between(earlier, later):
    parse = lambda t: dt.datetime.fromisoformat(t.replace('Z', '+00:00'))  # noqa: E731
    return (parse(later) - parse(earlier)).total_seconds() / 60


def decide(facts):
    """The keeper's one decision for one pull request; pure, so tests read every branch."""
    if facts.base != BASE or LABEL not in facts.labels:
        return Decision('skip', 'not a ship pull request into main')
    if not facts.open:
        return Decision('skip', 'not open')
    if facts.draft:
        return Decision('skip', 'draft')
    if not facts.same_repository:
        return Decision('skip', 'head in a fork: never enqueued by the keeper')
    since = [r for r in facts.removals if (r.get('at') or '') >= (facts.head_at or '')]
    if facts.queued:
        return Decision('skip', 'in the merge queue')
    if facts.auto_merge:
        return Decision('skip', 'auto-merge armed: GitHub enqueues it')
    manual = [r for r in since if r.get('reason') == 'manual']
    failures = [r for r in since if r.get('reason') not in QUEUE_DONE | QUEUE_RETRYLESS]
    if manual and (facts.labeled_at or '') <= manual[-1]['at']:
        return Decision('hold', 'removed from the queue by hand on this head; push or add the ship label again')
    conflicts = [r for r in since if r.get('reason') == 'merge_conflict']
    jobs = '\n'.join(f'- {job}' for job in facts.failing_jobs) or '- (no failed job found in the group run)'
    if len(failures) >= 2:
        return Decision('hold', f'{len(failures)} merge-queue failures on this head', f'stop:{facts.head}', '\n'.join([
            MARK.format(f'stop:{facts.head}'),
            f'Merge keeper: dequeued {len(failures)} times on head `{facts.head[:12]}` '
            f'(last: `{failures[-1].get("reason")}`), so it stops retrying. Failing jobs of the last group run:',
            jobs, '', 'Fix the cause (a flake: harness orchestration.md, Merging) and push; a new head resets the '
            'retry.']))
    if len(conflicts) >= 2:
        return Decision('hold', 'two queue conflicts on this head', f'queue-conflict:{facts.head}', '\n'.join([
            MARK.format(f'queue-conflict:{facts.head}'),
            f'Merge keeper: removed from the queue twice for a conflict on head `{facts.head[:12]}`, so it stops. '
            'Rebase onto main, rerun the local checks, push.']))
    if conflicts and (facts.main_at or '') < conflicts[-1]['at']:
        return Decision('hold', 'queue conflict: waits for main to move past the entry ahead')
    if not head_approved(facts):
        historical = [sha for sha in approved_shas(facts, authorizing=False) if not facts.head.startswith(sha)]
        if historical:
            old = ', '.join(sorted({s[:12] for s in historical}))
            return Decision('hold', 'approval is for an older head', f'stale:{facts.head}', '\n'.join([
                MARK.format(f'stale:{facts.head}'),
                f'Merge keeper: not enqueued. The recorded approval names `{old}`, but the head is '
                f'`{facts.head[:12]}`. The new commits need their review (an APPROVE on the head, or a new comment '
                f'`Review: orchestrator diff read {facts.head}`).']))
        return Decision('hold', 'no approval recorded for the head')
    pending = required_state(facts)
    if pending:
        return Decision('hold', f'required checks: {pending}')
    if facts.mergeable is None:
        return Decision('hold', 'mergeability not computed yet')
    if facts.mergeable is False:
        return Decision('hold', 'conflicts with main', f'conflict:{facts.head}', '\n'.join([
            MARK.format(f'conflict:{facts.head}'),
            'Merge keeper: not enqueued, the head conflicts with main. Rebase, rerun the local checks, push.']))
    plan = migration_plan(facts.added_migrations, facts.main_migrations)
    if plan:
        return Decision('hold', 'migration numbers do not follow main', f'migration:{facts.head}',
                        migration_comment(facts, plan))
    if failures and not RETRY_FAILED_CHECKS:
        return Decision('hold', 'dequeued for failed checks: wait-merge.sh or a person re-enqueues',
                        f'dequeued:{facts.head}', '\n'.join([
                            MARK.format(f'dequeued:{facts.head}'),
                            f'Merge keeper: dequeued on head `{facts.head[:12]}` (`{failures[-1].get("reason")}`). '
                            'Failing jobs of that group run:', jobs, '',
                            'The keeper does not re-enqueue a failed-checks removal: the worker\'s wait-merge.sh '
                            'retries a flake once, or fix and push (a new head is enqueued again).']))
    if failures and minutes_between(failures[-1]['at'], facts.now) < RETRY_AFTER:
        return Decision('hold', f'dequeued for failed checks; re-enqueue after {RETRY_AFTER} min unless a worker does')
    if failures:
        return Decision('enqueue', 'retry after one merge-queue failure', f'retry:{facts.head}', '\n'.join([
            MARK.format(f'retry:{facts.head}'),
            f'Merge keeper: dequeued once on this head (`{failures[-1].get("reason")}`); enqueued again, which reruns '
            'the whole group. Failing jobs of that group run:', jobs, '',
            'A second failure on this head stops the keeper.']))
    return Decision('enqueue', 'approved, green and numbered')


class GitHub:
    """`gh api` with the caller's token: GH_TOKEN in the environment (Actions) or the host login (the mini)."""

    def __init__(self, repository=REPOSITORY, dry_run=True):
        self.repository, self.dry_run = repository, dry_run
        self.owner, self.name = repository.split('/')
        self.permissions = {}

    def api(self, path, *arguments, paginate=False) -> Any:  # Any: GitHub's JSON, read by key where used
        command = ['gh', 'api', *(['--paginate', '--slurp'] if paginate else []), path, *arguments]
        out = subprocess.run(command, check=True, capture_output=True, text=True, timeout=CALL_TIMEOUT).stdout
        data = json.loads(out) if out.strip() else None
        if paginate and isinstance(data, list) and all(isinstance(page, list) for page in data):
            return [item for page in data for item in page]
        return data

    def graphql(self, query, **variables) -> Any:  # Any: GitHub's JSON, read by key where used
        arguments = ['-f', f'query={query}']
        for key, value in variables.items():
            arguments += ['-F' if isinstance(value, int) else '-f', f'{key}={value}']
        data = self.api('graphql', *arguments)
        if data.get('errors'):
            raise RuntimeError(json.dumps(data['errors'])[:500])
        return data['data']

    def repo(self, path):
        return f'repos/{self.repository}/{path}'

    def can_write(self, login):
        """The account's effective permission on this repository (organization roles and teams included)."""
        cache = self.permissions
        if login not in cache:
            try:
                cache[login] = self.api(self.repo(f'collaborators/{quote(login)}/permission'))['permission'] in WRITE
            except subprocess.CalledProcessError as error:
                if 'HTTP 404' not in (error.stderr or ''):
                    raise  # any other failure is this pull request's error in the report, not a silent "no"
                cache[login] = False  # 404: not a collaborator
        return cache[login]


PR_QUERY = '''query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){
pullRequest(number:$number){id isInMergeQueue autoMergeRequest{enabledAt}
timelineItems(last:100,itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT,LABELED_EVENT]){nodes{
__typename ... on LabeledEvent{createdAt label{name}}
... on RemovedFromMergeQueueEvent{createdAt reason beforeCommit{oid}}}}}}}'''


def collect(gh, pr, required, main_at=''):
    """Facts for one REST pull request object."""
    number, head = pr['number'], pr['head']['sha']
    detail = gh.api(gh.repo(f'pulls/{number}'))
    graph = gh.graphql(PR_QUERY, owner=gh.owner, name=gh.name, number=number)['repository']['pullRequest']
    events = graph['timelineItems']['nodes']
    labeled = [e['createdAt'] for e in events if e['__typename'] == 'LabeledEvent' and e['label']['name'] == LABEL]
    removals = [{'at': e['createdAt'], 'reason': (e.get('reason') or '').lower(),
                 'group': (e.get('beforeCommit') or {}).get('oid')}
                for e in events if e['__typename'] == 'RemovedFromMergeQueueEvent']
    suites = gh.api(gh.repo(f'commits/{head}/check-suites?per_page=100'))['check_suites']
    checks = {}
    for name, app in required:
        runs = gh.api(gh.repo(f'commits/{head}/check-runs?per_page=100&filter=all&check_name={quote(name)}'))
        runs = [r for r in runs['check_runs'] if app is None or (r.get('app') or {}).get('id') == app]
        if runs:  # check run ids grow with creation, so the newest attempt wins even before it starts
            latest = max(runs, key=lambda r: r['id'])
            checks[name] = {k: latest.get(k) for k in ('status', 'conclusion', 'started_at')}
    files = gh.api(gh.repo(f'pulls/{number}/files?per_page=100'), paginate=True)
    added = tuple(Path(f['filename']).name for f in files if f['status'] in ('added', 'renamed', 'copied')
                  and str(Path(f['filename']).parent) == MIGRATIONS)
    on_main = tuple(item['name'] for item in gh.api(gh.repo(f'contents/{MIGRATIONS}?ref={BASE}'))) if added else ()
    facts = Facts(
        number=number, head=head, open=pr['state'] == 'open', draft=pr['draft'], same_repository=pr['head']['repo'] is not None
        and pr['head']['repo']['full_name'] == gh.repository, base=pr['base']['ref'],
        labels=tuple(label['name'] for label in pr['labels']),
        reviews=[{'state': r.get('state'), 'commit_id': r.get('commit_id'), 'user': (r.get('user') or {}).get('login')}
                 for r in gh.api(gh.repo(f'pulls/{number}/reviews?per_page=100'), paginate=True)],
        comments=[{'body': c.get('body') or '', 'user': (c.get('user') or {}).get('login'), 'created_at': c.get('created_at'),
                   'updated_at': c.get('updated_at')}
                  for c in gh.api(gh.repo(f'issues/{number}/comments?per_page=100'), paginate=True)
                  if MARK_PREFIX in (c.get('body') or '') or APPROVAL_LINE.search(c.get('body') or '')],
        queued=graph['isInMergeQueue'], auto_merge=graph['autoMergeRequest'] is not None,
        mergeable=detail.get('mergeable'), main_at=main_at, now=github_time(now()),
        head_at=min((s['created_at'] for s in suites), default=github_time(now())), removals=removals,
        labeled_at=max(labeled, default=''), required=tuple(name for name, _ in required), checks=checks, added_migrations=added,
        main_migrations=on_main)
    facts.writers = frozenset(login for login in {c['user'] for c in facts.comments}
                              | {r['user'] for r in facts.reviews if r['state'] == 'APPROVED'}
                              if login and gh.can_write(login))
    failures = [r for r in removals if r['at'] >= facts.head_at and r['reason'] not in QUEUE_DONE | QUEUE_RETRYLESS]
    if failures and failures[-1]['group']:
        facts.failing_jobs = failing_jobs(gh, failures[-1]['group'])
    return facts, graph['id']


def failing_jobs(gh, group_sha):
    jobs = []
    for run in gh.api(gh.repo(f'actions/runs?head_sha={group_sha}&per_page=20'))['workflow_runs']:
        if run.get('conclusion') not in ('failure', 'timed_out', 'cancelled'):
            continue
        for job in gh.api(gh.repo(f'actions/runs/{run["id"]}/jobs?filter=latest&per_page=100'))['jobs']:
            if job.get('conclusion') in ('failure', 'timed_out', 'cancelled'):
                jobs.append(f'{run["name"]} / {job["name"]}: {job["conclusion"]} {job["html_url"]}')
    return jobs


def required_checks(gh):
    rules = gh.api(gh.repo(f'rules/branches/{BASE}'))
    return [(c['context'], c.get('integration_id')) for r in rules if r['type'] == 'required_status_checks'
            for c in r['parameters']['required_status_checks']]


def main_time(gh):
    return gh.api(gh.repo(f'commits/{BASE}'))['commit']['committer']['date']


ENQUEUE = '''mutation($id:ID!,$head:GitObjectID!){enqueuePullRequest(input:{pullRequestId:$id,expectedHeadOid:$head}){
mergeQueueEntry{position state}}}'''


def act(gh, facts, node_id, decision):
    """Post the decision's comment once, then enqueue; returns what was done."""
    done = []
    if decision.comment_key and not marked(facts, decision.comment_key):
        if not gh.dry_run:
            gh.api(gh.repo(f'issues/{facts.number}/comments'), '-f', f'body={decision.comment}')
        done.append(f'comment {decision.comment_key}')
    if decision.action == 'enqueue':
        if not gh.dry_run:
            entry = gh.graphql(ENQUEUE, id=node_id, head=facts.head)['enqueuePullRequest']['mergeQueueEntry']
            done.append(f'enqueued at position {entry and entry.get("position")}')
        else:
            done.append('would enqueue')
    return done


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def github_time(moment):
    """GitHub's timestamp form (`2026-11-09T00:18:20Z`), so string order is time order."""
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def run(arguments):
    gh = GitHub(arguments.repository, dry_run=arguments.dry_run)
    started, results, errors = now(), [], []
    required, main_at = required_checks(gh), main_time(gh)
    if arguments.pr:
        pulls = [gh.api(gh.repo(f'pulls/{n}')) for n in arguments.pr]
    else:
        pulls = [p for p in gh.api(gh.repo(f'pulls?state=open&base={BASE}&per_page=100'), paginate=True)
                 if any(label['name'] == LABEL for label in p['labels'])]
    for pr in pulls:
        try:
            facts, node_id = collect(gh, pr, required, main_at)
            if arguments.ignore_label and LABEL not in facts.labels:
                facts.labels = (*facts.labels, LABEL)
            decision = decide(facts)
            done = act(gh, facts, node_id, decision) if decision.action != 'skip' else []
            results.append({'pr': facts.number, 'head': facts.head[:12], 'action': decision.action,
                            'reason': decision.reason, 'done': done,
                            **({'comment': decision.comment} if arguments.dry_run and decision.comment else {})})
        except (subprocess.SubprocessError, RuntimeError, KeyError, TypeError, ValueError) as error:
            errors.append({'pr': pr.get('number'), 'error': f'{type(error).__name__}: {str(error)[:300]}'})
    report = {'at': github_time(started), 'repository': gh.repository, 'dry_run': gh.dry_run,
              'host': arguments.host, 'results': results, 'errors': errors}
    print(json.dumps(report, ensure_ascii=False, indent=1 if arguments.dry_run else None))
    if arguments.heartbeat_file or arguments.heartbeat_variable:
        heartbeat(gh, arguments, report)
    return 1 if errors else 0


def heartbeat(gh, arguments, report):
    """Last run and last clean run, in a local file and in a repository Actions variable the watchdog reads."""
    previous = {}
    path = Path(arguments.heartbeat_file).expanduser() if arguments.heartbeat_file else None
    if path and path.exists():
        try:
            previous = json.loads(path.read_text())
        except ValueError:
            previous = {}
    beat = {'at': report['at'], 'ok_at': report['at'] if not report['errors'] else previous.get('ok_at', ''),
            'host': arguments.host, 'prs': len(report['results']), 'errors': len(report['errors']),
            'enqueued': sum(1 for r in report['results'] for d in r['done'] if d.startswith('enqueued'))}
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(beat) + '\n')
    if arguments.heartbeat_variable and not gh.dry_run:
        value = json.dumps(beat, separators=(',', ':'))
        try:
            gh.api(gh.repo(f'actions/variables/{HEARTBEAT_VARIABLE}'), '-X', 'PATCH', '-f', f'value={value}')
        except subprocess.CalledProcessError:
            gh.api(gh.repo('actions/variables'), '-X', 'POST', '-f', f'name={HEARTBEAT_VARIABLE}',
                   '-f', f'value={value}')


def stale(value, at=None, minutes=STALE_MINUTES):
    """Why the heartbeat is stale, or '' when the keeper ran cleanly within `minutes`."""
    if not value:
        return 'no heartbeat recorded'
    try:
        beat = json.loads(value)
        ok = dt.datetime.fromisoformat(beat['ok_at'])
    except (ValueError, KeyError, TypeError):
        return f'unreadable heartbeat: {value[:120]}'
    age = ((at or now()) - ok).total_seconds() / 60
    return f'last clean run {age:.0f} min ago on {beat.get("host")} (limit {minutes})' if age > minutes else ''


def check(arguments):
    value = arguments.value
    if value is None:
        gh = GitHub(arguments.repository)
        try:
            value = gh.api(gh.repo(f'actions/variables/{HEARTBEAT_VARIABLE}'))['value']
        except subprocess.CalledProcessError:
            value = ''
    problem = stale(value, minutes=arguments.minutes)
    print(problem or f'merge keeper alive: {value}')
    return 1 if problem else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=(__doc__ or '').split('\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    r = sub.add_parser('run', help='decide, comment and enqueue for every open ship pull request')
    r.add_argument('--repository', default=REPOSITORY)
    r.add_argument('--dry-run', action='store_true', help='write nothing; print each decision and comment')
    r.add_argument('--pr', type=int, action='append', help='only these pull requests (repeatable)')
    r.add_argument('--ignore-label', action='store_true', help='with --dry-run: decide as if labelled ship')
    r.add_argument('--host', default=os.uname().nodename.split('.')[0])
    r.add_argument('--heartbeat-file', help='write the last run here')
    r.add_argument('--heartbeat-variable', action='store_true', help=f'write the repository variable '
                   f'{HEARTBEAT_VARIABLE} (needs a token that may write Actions variables)')
    c = sub.add_parser('heartbeat-check', help=f'exit 1 when the last clean run is older than {STALE_MINUTES} min')
    c.add_argument('--repository', default=REPOSITORY)
    c.add_argument('--value', help='the heartbeat JSON (default: read the repository variable)')
    c.add_argument('--minutes', type=int, default=STALE_MINUTES)
    arguments = parser.parse_args(argv)
    if arguments.command == 'run':
        if arguments.ignore_label and not arguments.dry_run:
            parser.error('--ignore-label needs --dry-run')
        return run(arguments)
    return check(arguments)


if __name__ == '__main__':
    sys.exit(main())
