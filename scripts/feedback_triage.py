#!/usr/bin/env python3
"""Owner-started private triage of agent feedback: pull, local match, ledger, clusters, report (ANYM-264).

    feedback_triage.py pull [--host deedkit-prod] [--state DIR]
    feedback_triage.py snapshot [--state DIR]
    feedback_triage.py match [--state DIR]
    feedback_triage.py record --decisions FILE [--state DIR]

The triager is the owner-run coding-agent session (owner decision D1, ANYM-294); this helper does the deterministic
parts and never calls a model. `pull` reads the owner export (ANYM-265) over the owner's SSH access into a private
inbox; `snapshot` saves Tiden main and every open branch locally; `match` lists candidate specs per item from that
snapshot only; `record` takes the session's decisions, writes one ledger entry per item, advances the position past
complete entries and writes the private report and a dispositions file with the `set-feedback-status` commands for
the owner (ANYM-343). Feedback text is data: nothing derived from it reaches Tiden, GitHub, git or the remote command.
The only external calls are `ssh <host>` with a constant export command and the read-only `tiden spec list` and
`tiden branch list`. State lives outside Git (default ~/.deedkit/feedback-triage). Exit 0 done, 1 refused, 64 usage.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
STATE = Path.home() / '.deedkit' / 'feedback-triage'
HOST = 'deedkit-prod'
SSH_TIMEOUT = 120
TIDEN_TIMEOUT = 120
OVERLAP_MINUTES = 10
MAX_PAGES = 100
CLASSES = ('reported-bug', 'feature', 'question', 'not-actionable')
VERDICTS = ('accepted', 'declined')
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
SPEC = re.compile(r'ANYM-[1-9][0-9]*')
SLUG = re.compile(r'[a-z0-9][a-z0-9-]{2,60}')
IMAGE = re.compile(r'sha256:[0-9a-f]{64}')
PAGE = re.compile(r'[A-Za-z0-9_=.:+/-]{1,512}')
NOTE_LIMIT = 280
# The operator container of the running API image with the owner DSN (ANYM-133), the shape of the
# production.md operator steps. Only validated tokens (image digest, time, page token) are added to it.
API_CONTAINER = 'personal-os-probe-api-1'
EXPORT = ['docker', 'run', '--rm', '--name', 'deedkit-feedback-export', '--network', 'personal-os-probe_backend',
          '--user', '10001:10001', '--read-only', '--tmpfs', '/tmp:size=16m,noexec,nosuid', '--cap-drop', 'ALL',
          '--security-opt', 'no-new-privileges', '-e', 'DATABASE_URL_FILE=/run/secrets/database_url',
          '-e', 'PROBE_FILES=/tmp/files', '--mount',
          'type=bind,source=/opt/personal-os-probe/private/owner.dsn,target=/run/secrets/database_url,readonly']


class Refused(Exception):
    pass


def run(argv, timeout, stdin=None):
    try:
        done = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refused(f'{argv[0]} failed: {type(error).__name__}') from None
    if done.returncode:
        raise Refused(f'{argv[0]} {argv[1] if len(argv) > 1 else ""} exited {done.returncode}')
    return done.stdout


def parse(text, what):
    try:
        return json.loads(text)
    except ValueError:
        raise Refused(f'{what}: not JSON') from None


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as handle:
        json.dump(value, handle, indent=1, sort_keys=True)
        handle.write('\n')
    os.chmod(handle.name, 0o600)
    os.replace(handle.name, path)


def when(text):
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise ValueError('time without offset')
    return value.astimezone(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat()


@contextmanager
def locked(state):
    if state.resolve().is_relative_to(ROOT.resolve()):
        raise Refused('the state directory must be outside the repository')
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(state / 'lock', 'w') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('another triage run holds the lock') from None
        yield


# --- pull --------------------------------------------------------------------------------------------------------

def remote(host, argv):
    return run(['ssh', '-o', 'BatchMode=yes', host, shlex.join(argv)], SSH_TIMEOUT)


def export_pages(host, since, ssh=None):
    ssh = ssh or remote
    image = ssh(host, ['docker', 'inspect', '--format', '{{.Image}}', API_CONTAINER]).strip()
    if not IMAGE.fullmatch(image):
        raise Refused('the API container image is not a digest')
    command = EXPORT + [image, 'python', '-m', 'messaging_service', 'export-feedback',
                        '--overlap', str(OVERLAP_MINUTES)] + (['--since', stamp(since)] if since else [])
    page, items, head = None, [], None
    for _ in range(MAX_PAGES):
        reply = parse(ssh(host, command + (['--page', page] if page else [])), 'export')
        if not isinstance(reply, dict) or reply.get('mode') != 'submissions' or not isinstance(reply.get('items'), list):
            raise Refused('export: unexpected reply')
        items += reply['items']
        head = reply['export_head']
        if not reply.get('has_more'):
            return items, head
        page = reply.get('next_page')
        if not isinstance(page, str) or not PAGE.fullmatch(page):
            raise Refused('export: bad next_page')
    raise Refused(f'export: more than {MAX_PAGES} pages')


def pull(state, host, ssh=None):
    position = load(state / 'state.json', {}).get('position')
    items, head = export_pages(host, when(position) if position else None, ssh)
    ledger, inbox = load(state / 'ledger.json', {}), load(state / 'inbox.json', {})
    fresh = 0
    for item in items:
        if not UUID.fullmatch(str(item.get('id'))):
            raise Refused('export: item without a feedback ID')
        if item['id'] not in ledger and item['id'] not in inbox:
            inbox[item['id']] = item
            fresh += 1
    if fresh:
        # Everything created up to the head is in the ledger or the inbox now (late commits: the overlap).
        save(state / 'inbox.json', inbox)
        save(state / 'state.json', dict(load(state / 'state.json', {}), head=head))
    return fresh, len(inbox)


# --- snapshot and match ------------------------------------------------------------------------------------------

def tiden(*argv):
    return parse(run(['tiden', *argv, '--format', 'json'], TIDEN_TIMEOUT), 'tiden ' + ' '.join(argv[:2]))


def flatten(reply, what):
    if not isinstance(reply, dict) or not isinstance(reply.get('requirements'), list):
        raise Refused(f'{what}: unexpected reply')
    specs, stack = [], list(reply['requirements'])
    while stack:
        spec = stack.pop()
        specs.append(spec)
        stack += spec.get('children') or []
    pagination = reply.get('pagination') or {}
    if pagination.get('nextPageToken') or pagination.get('totalCount', len(specs)) != len(specs):
        raise Refused(f'{what}: truncated')
    return [{'ref': f"ANYM-{spec['seqNum']}", 'title': spec.get('title') or '', 'status': spec.get('status'),
             'text': (spec.get('content') or '')[:4000],
             'sources': [str(source) for source in spec.get('sources') or []][:50]} for spec in specs]


def snapshot(state, read=None):
    read = read or tiden
    main = flatten(read('spec', 'list', '--branch', 'main', '--include-sources'), 'main')
    branches = read('branch', 'list')
    if not isinstance(branches, dict) or not isinstance(branches.get('branches'), list):
        raise Refused('branch list: unexpected reply')
    open_names = sorted(b['name'] for b in branches['branches'] if b.get('status') == 'open' and b.get('name') != 'main')
    taken = {spec['ref'] for spec in main}
    branch_specs = {}
    for name in open_names:
        added = [spec for spec in flatten(read('spec', 'list', '--branch', name), name) if spec['ref'] not in taken]
        if added:
            branch_specs[name] = added
    save(state / 'snapshot.json', {'main': main, 'branches': branch_specs})
    return len(main), sum(len(v) for v in branch_specs.values())


def known(snap):
    specs = {spec['ref']: dict(spec, branch='main') for spec in snap['main']}
    for name, added in snap['branches'].items():
        for spec in added:
            specs.setdefault(spec['ref'], dict(spec, branch=name))
    return specs


def words(text):
    return {w for w in re.findall(r'[a-z_]{4,}', text.lower())}


def candidates(item, specs, limit=5):
    operation = item.get('operation') or ''
    mine = words(item.get('description') or '')
    scored = []
    for spec in specs.values():
        body = spec['title'] + ' ' + spec['text'] + ' ' + ' '.join(spec['sources'])
        score = (10 if operation and operation in body else 0) + len(mine & words(spec['title'] + ' ' + spec['text']))
        if score:
            scored.append((-score, spec['ref']))
    return [ref for _, ref in sorted(scored)[:limit]]


def match(state):
    snap = load(state / 'snapshot.json', None)
    if snap is None:
        raise Refused('no snapshot: run `snapshot` first')
    specs = known(snap)
    inbox = load(state / 'inbox.json', {})
    out = [{'id': i, 'category': item.get('category'), 'operation': item.get('operation'),
            'candidates': [{'ref': ref, 'title': specs[ref]['title'], 'status': specs[ref]['status'],
                            'branch': specs[ref]['branch']} for ref in candidates(item, specs)]}
           for i, item in sorted(inbox.items(), key=lambda pair: (pair[1].get('created_at') or '', pair[0]))]
    save(state / 'candidates.json', out)
    return out


# --- decisions, dispositions, ledger -----------------------------------------------------------------------------

def clean(text, limit=NOTE_LIMIT):
    text = re.sub(r'[\x00-\x1f\x7f]+', ' ', str(text)).strip()
    return text if len(text) <= limit else text[:limit - 1] + '…'


def disposition(decision, specs):
    """The status hand-off of one item (ANYM-343 vocabulary; `fixed` and later come from the sync tool)."""
    reason, primary = decision['reason'], decision.get('primary')
    if decision.get('duplicate_of'):
        return {'status': 'duplicate', 'spec_ref': primary, 'duplicate_of': decision['duplicate_of'], 'note': reason}
    matched = decision.get('specs') or []
    if not matched:
        status = 'declined' if decision['classification'] in ('question', 'not-actionable') else 'received'
        note = reason if status == 'declined' or not decision.get('new_card') else \
            f"no matching card; proposed new card {decision['new_card']}: {reason}"
        return {'status': status, 'spec_ref': None, 'duplicate_of': None, 'note': note}
    accepted = [s['ref'] for s in matched if s['verdict'] == 'accepted']
    declined = [s['ref'] for s in matched if s['verdict'] == 'declined']
    if not accepted:
        return {'status': 'declined', 'spec_ref': primary, 'duplicate_of': None, 'note': reason}
    parts = []
    if declined:
        parts.append('declined part: ' + ', '.join(declined))
    done = [ref for ref in accepted if specs.get(ref, {}).get('status') == 'Done' and specs[ref]['branch'] == 'main']
    if done and len(done) < len(accepted):
        parts.append('delivered: ' + ', '.join(done) + '; waiting for ' + ', '.join(r for r in accepted if r not in done))
    return {'status': 'accepted', 'spec_ref': primary, 'duplicate_of': None, 'note': '; '.join(parts + [reason])}


def checked(decision, specs, ids):
    if not isinstance(decision, dict) or decision.get('id') not in ids:
        raise Refused('decision for an unknown feedback ID')
    if decision.get('classification') not in CLASSES:
        raise Refused(f"{decision['id']}: classification must be one of {', '.join(CLASSES)}")
    reason = decision.get('reason')
    if not isinstance(reason, str) or not reason.strip() or '\n' in reason or len(reason) > 200:
        raise Refused(f"{decision['id']}: reason must be one line of 1-200 characters")
    matched = decision.get('specs') or []
    for spec in matched:
        if not isinstance(spec, dict) or spec.get('ref') not in specs or spec.get('verdict') not in VERDICTS:
            raise Refused(f"{decision['id']}: each spec needs a ref from the snapshot and a verdict")
    refs = [spec['ref'] for spec in matched]
    primary = decision.get('primary')
    if matched and primary not in refs:
        raise Refused(f"{decision['id']}: primary must be one of its specs")
    if not matched and primary is not None:
        raise Refused(f"{decision['id']}: primary without specs")
    if matched and any(s['verdict'] == 'accepted' for s in matched) and \
            next(s for s in matched if s['ref'] == primary)['verdict'] != 'accepted':
        raise Refused(f"{decision['id']}: the primary spec must be an accepted one")
    duplicate = decision.get('duplicate_of')
    if duplicate is not None and (not UUID.fullmatch(str(duplicate)) or duplicate == decision['id']):
        raise Refused(f"{decision['id']}: duplicate_of must be another feedback ID")
    if duplicate is None and decision['classification'] in ('reported-bug', 'feature') and not matched \
            and not SLUG.fullmatch(str(decision.get('new_card'))):
        raise Refused(f"{decision['id']}: an unmatched bug or feature needs new_card (a slug)")
    return decision


def command(disp, ids):
    argv = ['python', '-m', 'messaging_service', 'set-feedback-status', '--id', *ids, '--status', disp['status']]
    argv += ['--spec', disp['spec_ref']] if disp['spec_ref'] else []
    argv += ['--duplicate-of', disp['duplicate_of']] if disp['duplicate_of'] else []
    argv += ['--note', disp['note']] if disp['note'] else []
    return shlex.join(argv)


def write_ledger(state, ledger):
    save(state / 'ledger.json', ledger)


def record(state, decisions, now=None):
    snap = load(state / 'snapshot.json', None)
    if snap is None:
        raise Refused('no snapshot: run `snapshot` first')
    specs, inbox = known(snap), load(state / 'inbox.json', {})
    ledger, meta = load(state / 'ledger.json', {}), load(state / 'state.json', {})
    if not isinstance(decisions, list):
        raise Refused('decisions must be a JSON list')
    chosen = {d['id']: d for d in (checked(d, specs, set(inbox) | set(ledger)) for d in decisions)}
    if set(inbox) & set(ledger):
        # A run stopped after a ledger write: that entry is complete, so the item leaves the inbox.
        inbox = {i: item for i, item in inbox.items() if i not in ledger}
        save(state / 'inbox.json', inbox)
        meta['position'] = position(inbox, meta)
        save(state / 'state.json', meta)
    done = []
    for i, decision in sorted(chosen.items()):
        if i in ledger:
            continue
        item = inbox[i]
        disp = disposition(decision, specs)
        disp['note'] = clean(disp['note']) if disp['note'] else None
        cluster = decision.get('primary') if decision.get('specs') else \
            'new:' + decision['new_card'] if decision.get('new_card') else 'unmatched:' + decision['classification']
        ledger[i] = {'principal': item.get('principal'), 'key_id': item.get('key_id'), 'created_at': item.get('created_at'),
                     'classification': decision['classification'], 'reason': clean(decision['reason'], 200),
                     'specs': [{'ref': s['ref'], 'verdict': s['verdict']} for s in decision.get('specs') or []],
                     'primary': decision.get('primary'), 'cluster': cluster, 'disposition': disp}
        write_ledger(state, ledger)
        inbox.pop(i)
        save(state / 'inbox.json', inbox)
        meta['position'] = position(inbox, meta)
        save(state / 'state.json', meta)
        done.append(i)
    return report(state, ledger, done, inbox, now or datetime.now(timezone.utc))


def position(inbox, meta):
    # Items left in the inbox hold the position at their own creation time; an empty inbox moves it to the head.
    pending = [item.get('created_at') for item in inbox.values() if item.get('created_at')]
    return min(pending, key=when) if pending else meta.get('head', meta.get('position'))


def clusters(ledger, ids=None):
    groups = {}
    for i, entry in ledger.items():
        if ids is None or i in ids:
            groups.setdefault(entry['cluster'], []).append((i, entry))
    return {name: {'feedback_ids': sorted(i for i, _ in rows), 'reports': len(rows),
                   'principals': len({e['principal'] for _, e in rows}), 'keys': len({e['key_id'] for _, e in rows})}
            for name, rows in sorted(groups.items())}


def report(state, ledger, done, inbox, now):
    name = now.strftime('%Y-%m-%dT%H%M%SZ')
    rows = {i: ledger[i] for i in done}
    grouped = {}
    for i, entry in sorted(rows.items()):
        disp = entry['disposition']
        grouped.setdefault((disp['status'], disp['spec_ref'] or '', disp['duplicate_of'] or '', disp['note'] or ''),
                           (disp, []))[1].append(i)
    commands = [command(disp, ids[k:k + 100]) for _, (disp, ids) in sorted(grouped.items())
                for k in range(0, len(ids), 100)]
    dispositions = [{'id': i, 'status': e['disposition']['status'], 'spec_ref': e['disposition']['spec_ref'],
                     'duplicate_of': e['disposition']['duplicate_of'], 'fixed_in': None, 'note': e['disposition']['note']}
                    for i, e in sorted(rows.items())]
    lines = [f'# Feedback triage {name}', '', f'Processed: {len(rows)}. Still in the inbox: {len(inbox)}.',
             'Agents are counted by key: the store has no agent session ID.', '']
    if rows:
        lines += ['## Items', '', '| Feedback | Class | Cluster | Specs | Status | Reason |', '|---|---|---|---|---|---|']
        for i, e in sorted(rows.items()):
            refs = ', '.join(f"{s['ref']} ({s['verdict']})" for s in e['specs']) or 'no match'
            lines.append(f"| {i} | {e['classification']} | {e['cluster']} | {refs} | {e['disposition']['status']} | "
                         f"{e['reason'].replace('|', '/')} |")
        lines += ['', '## Clusters (all runs)', '', '| Cluster | Reports | Principals | Keys |', '|---|---|---|---|']
        lines += [f"| {k} | {v['reports']} | {v['principals']} | {v['keys']} |" for k, v in clusters(ledger).items()]
        owner = [i for i, e in sorted(rows.items()) if e['cluster'].startswith('new:') or
                 any(s['verdict'] == 'declined' for s in e['specs'])]
        lines += ['', '## Needs the owner', ''] + ([f'- {i}: {rows[i]["cluster"]}' for i in owner] or ['- none'])
        lines += ['', '## Status commands (operator container, ANYM-133)', '', '```sh'] + commands + ['```']
    (state / 'reports').mkdir(exist_ok=True, mode=0o700)
    path = state / 'reports' / f'{name}.md'
    path.write_text('\n'.join(lines) + '\n')
    os.chmod(path, 0o600)
    if rows:
        save(state / 'dispositions' / f'{name}.json', dispositions)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description='Owner-started private triage of agent feedback (ANYM-264).')
    parser.add_argument('action', choices=['pull', 'snapshot', 'match', 'record'])
    parser.add_argument('--state', type=Path, default=Path(os.environ.get('FEEDBACK_TRIAGE_STATE', STATE)))
    parser.add_argument('--host', default=HOST)
    parser.add_argument('--decisions', type=Path)
    args = parser.parse_args(argv)
    try:
        with locked(args.state):
            if args.action == 'pull':
                fresh, waiting = pull(args.state, args.host)
                if not fresh and not waiting:
                    path = report(args.state, load(args.state / 'ledger.json', {}), [], {}, datetime.now(timezone.utc))
                    print(f'no new feedback; empty report {path}')
                else:
                    print(f'pulled {fresh} new; inbox {waiting}')
            elif args.action == 'snapshot':
                print('snapshot: main %d specs, open branches %d more' % snapshot(args.state))
            elif args.action == 'match':
                print(f'candidates for {len(match(args.state))} items in {args.state / "candidates.json"}')
            else:
                if not args.decisions:
                    parser.error('record needs --decisions')
                print(f'report {record(args.state, parse(args.decisions.read_text(), "decisions"))}')
    except Refused as error:
        print(f'REFUSED {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
