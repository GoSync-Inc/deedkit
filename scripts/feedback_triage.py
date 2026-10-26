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
CLASSES = ('reported-bug', 'feature', 'question', 'not-actionable', 'enable-only')
VERDICTS = ('accepted', 'declined')
HOLD = 'waits for the owner: opt-in'  # an enable-only item's note: the sync tool never links or follows it (ANYM-570)
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
        # stdin is never the terminal, so an unattended run cannot wait on a prompt (ANYM-536).
        done = subprocess.run(argv, input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)
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
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as handle:  # mkstemp: mode 0600
        json.dump(value, handle, indent=1, sort_keys=True)
        handle.write('\n')
    os.replace(handle.name, path)


def when(text):
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise ValueError('time without offset')
    return value.astimezone(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat()


def in_git(path):
    path = path.resolve()
    return any((parent / '.git').exists() for parent in (path, *path.parents))


def private_write(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(text)


def fresh_name(directory, stem, suffix):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for n in range(1000):
        path = directory / (stem + (f'-{n}' if n else '') + suffix)
        if not path.exists():
            return path
    raise Refused('too many reports with one timestamp')


@contextmanager
def locked(state):
    if in_git(state):
        raise Refused('the state directory must be outside every Git checkout')
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
    # ANYM-265: `--since` with `--overlap` on the first page, then only `--page` (they are exclusive).
    command = EXPORT + [image, 'python', '-m', 'messaging_service', 'export-feedback']
    first = ['--since', stamp(since), '--overlap', str(OVERLAP_MINUTES)] if since else []
    page, items, head = None, [], None
    for _ in range(MAX_PAGES):
        reply = parse(ssh(host, command + (['--page', page] if page else first)), 'export')
        if not isinstance(reply, dict) or reply.get('mode') != 'created' or not isinstance(reply.get('submissions'), list) \
                or not isinstance(reply.get('dispositions'), list) or not isinstance(reply.get('export_head'), str):
            raise Refused('export: unexpected reply')
        status = {row.get('id'): row for row in reply['dispositions'] if isinstance(row, dict)}
        items += [dict(status.get(row.get('id'), {}), **row) for row in reply['submissions'] if isinstance(row, dict)]
        head = reply['export_head']
        if not reply.get('has_more'):
            return items, head
        page = reply.get('next_page')
        if not isinstance(page, str) or not PAGE.fullmatch(page):
            raise Refused('export: bad next_page')
    raise Refused(f'export: more than {MAX_PAGES} pages')


def since(state):
    position = load(state / 'state.json', {}).get('position')
    return when(position) if position else None


def pull(state, host, ssh=None):
    return merge(state, *export_pages(host, since(state), ssh))


def merge(state, items, head):
    """The export's new items into the inbox; `export_pages` reads and writes nothing (ANYM-569)."""
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
    complete(reply, len(specs), what)
    return [{'ref': f"ANYM-{spec['seqNum']}", 'title': spec.get('title') or '', 'status': spec.get('status'),
             'text': spec.get('content') or '', 'sources': [str(source) for source in spec.get('sources') or []]}
            for spec in specs]


def complete(reply, count, what):
    pagination = reply.get('pagination')
    if not isinstance(pagination, dict) or pagination.get('nextPageToken') or pagination.get('totalCount') != count:
        raise Refused(f'{what}: truncated or without pagination')


def snapshot(state, read=None):
    snap = collect(read)
    save(state / 'snapshot.json', snap)
    return len(snap['main']), len(snap['branches'])


def collect(read=None):
    """Tiden main and every open branch, read whole; nothing is written (ANYM-569)."""
    read = read or tiden
    main = flatten(read('spec', 'list', '--branch', 'main', '--include-sources'), 'main')
    branches = read('branch', 'list')
    if not isinstance(branches, dict) or not isinstance(branches.get('branches'), list):
        raise Refused('branch list: unexpected reply')
    if branches.get('pagination') is not None:
        complete(branches, len(branches['branches']), 'branch list')
    open_names = sorted(b['name'] for b in branches['branches'] if b.get('status') == 'open' and b.get('name') != 'main')
    # Every open branch is kept whole, branch versions of main specs too; the file is replaced only after all reads.
    branch_specs = {name: flatten(read('spec', 'list', '--branch', name), name) for name in open_names}
    return {'main': main, 'branches': branch_specs}


def known(snap):
    specs = {spec['ref']: dict(spec, branch='main') for spec in snap['main']}
    for name, listed in snap['branches'].items():
        for spec in listed:
            specs.setdefault(spec['ref'], dict(spec, branch=name))
    return specs


def variants(snap):
    return [dict(spec, branch='main') for spec in snap['main']] + \
        [dict(spec, branch=name) for name, listed in snap['branches'].items() for spec in listed]


def words(text):
    return {w for w in re.findall(r'[a-z_]{4,}', text.lower())}


def candidates(item, versions, limit=5):
    operation = item.get('operation') or ''
    mine = words(item.get('description') or '')
    best = {}
    for spec in versions:
        body = spec['title'] + ' ' + spec['text'] + ' ' + ' '.join(spec['sources'])
        score = (10 if operation and operation in body else 0) + len(mine & words(spec['title'] + ' ' + spec['text']))
        if score > best.get(spec['ref'], 0):
            best[spec['ref']] = score
    return [ref for _, ref in sorted((-score, ref) for ref, score in best.items())[:limit]]


def match(state):
    snap = load(state / 'snapshot.json', None)
    if snap is None:
        raise Refused('no snapshot: run `snapshot` first')
    specs = known(snap)
    inbox = load(state / 'inbox.json', {})
    out = [{'id': i, 'category': item.get('category'), 'operation': item.get('operation'),
            'candidates': [{'ref': ref, 'title': specs[ref]['title'], 'status': specs[ref]['status'],
                            'branch': specs[ref]['branch']} for ref in candidates(item, variants(snap))]}
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
    if decision['classification'] == 'enable-only':
        # Delivered but off: the item waits for the owner, not for its card (ANYM-570); no link, so no follow.
        return {'status': 'received', 'spec_ref': None, 'duplicate_of': None,
                'note': f'{HOLD} ({primary})'}
    if decision.get('answer'):
        return {'status': 'declined', 'spec_ref': None, 'duplicate_of': None,
                'note': 'answered, no change needed: ' + decision['answer']}
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


def checked(decision, specs, keys):
    if not isinstance(decision, dict) or decision.get('id') not in keys:
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
    if decision['classification'] == 'enable-only' and (len(matched) != 1 or matched[0]['verdict'] != 'accepted'):
        raise Refused(f"{decision['id']}: enable-only needs exactly one accepted spec")
    answer = decision.get('answer')
    if answer is not None and (decision['classification'] != 'question' or matched or not isinstance(answer, str)
                               or not answer.strip() or '\n' in answer or len(answer) > 250):
        raise Refused(f"{decision['id']}: answer is one line of 1-250 characters on a question without specs")
    duplicate = decision.get('duplicate_of')
    if duplicate is not None and (duplicate == decision['id'] or keys.get(duplicate) is None
                                  or keys[duplicate] != keys[decision['id']]):
        raise Refused(f"{decision['id']}: duplicate_of must be another known feedback ID of the same key")
    target = decision.get('verdict_of')
    if target is not None and (target == decision['id'] or target not in keys):
        raise Refused(f"{decision['id']}: verdict_of must be another known feedback ID")
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


def checked_batch(decisions, specs, inbox, ledger):
    """Every decision checked before any is recorded: one bad decision refuses the whole batch."""
    if not isinstance(decisions, list):
        raise Refused('decisions must be a JSON list')
    keys = {i: entry.get('key_id') for i, entry in ledger.items()} | {i: item.get('key_id') for i, item in inbox.items()}
    chosen = {d['id']: d for d in (checked(d, specs, keys) for d in decisions)}
    for i, decision in chosen.items():
        target = decision.get('verdict_of')
        if target and target not in ledger and (target not in chosen or chosen[target].get('verdict_of')):
            raise Refused(f'{i}: verdict_of names an item that has no cluster yet')
    return chosen


def record(state, decisions, now=None):
    now = now or datetime.now(timezone.utc)
    snap = load(state / 'snapshot.json', None)
    if snap is None:
        raise Refused('no snapshot: run `snapshot` first')
    specs, inbox = known(snap), load(state / 'inbox.json', {})
    ledger, meta = load(state / 'ledger.json', {}), load(state / 'state.json', {})
    chosen = checked_batch(decisions, specs, inbox, ledger)
    if set(inbox) & set(ledger):
        # A run stopped after a ledger write: that entry is complete, so the item leaves the inbox.
        inbox = {i: item for i, item in inbox.items() if i not in ledger}
        save(state / 'inbox.json', inbox)
        meta['position'] = position(inbox, meta)
        save(state / 'state.json', meta)
    # Verdict rows (proposal/2026-10-17-feedback-reverify) join their target's cluster, so targets go first.
    for i, decision in sorted(chosen.items(), key=lambda pair: (bool(pair[1].get('verdict_of')), pair[0])):
        if i in ledger:
            continue
        item = inbox[i]
        disp = disposition(decision, specs)
        disp['note'] = clean(disp['note']) if disp['note'] else None
        cluster = ledger[decision['verdict_of']]['cluster'] if decision.get('verdict_of') else \
            decision.get('primary') if decision.get('specs') else \
            'new:' + decision['new_card'] if decision.get('new_card') else 'unmatched:' + decision['classification']
        ledger[i] = {'principal': item.get('principal'), 'key_id': item.get('key_id'), 'created_at': item.get('created_at'),
                     'classification': decision['classification'], 'reason': clean(decision['reason'], 200),
                     'specs': [{'ref': s['ref'], 'verdict': s['verdict']} for s in decision.get('specs') or []],
                     'primary': decision.get('primary'), 'cluster': cluster, 'disposition': disp,
                     'verdict_of': decision.get('verdict_of'), 'reported': False,
                     # The decision time travels with the entry, so a report republished after a crash keeps it and
                     # the sync tool never overwrites a disposition changed after it (ANYM-536).
                     'decided_at': stamp(now)}
        write_ledger(state, ledger)
        inbox.pop(i)
        save(state / 'inbox.json', inbox)
        meta['position'] = position(inbox, meta)
        save(state / 'state.json', meta)
    # Entries of a stopped run that never reached a report are reported now.
    done = sorted(i for i, entry in ledger.items() if entry.get('reported') is False)
    path = report(state, ledger, done, inbox, now)
    for i in done:
        ledger[i]['reported'] = True
    write_ledger(state, ledger)
    return path


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
    name = now.strftime('%Y-%m-%dT%H%M%S.%fZ')
    rows = {i: ledger[i] for i in done}
    grouped = {}
    for i, entry in sorted(rows.items()):
        disp = entry['disposition']
        grouped.setdefault((disp['status'], disp['spec_ref'] or '', disp['duplicate_of'] or '', disp['note'] or ''),
                           (disp, []))[1].append(i)
    commands = [command(disp, ids[k:k + 100]) for _, (disp, ids) in sorted(grouped.items())
                for k in range(0, len(ids), 100)]
    dispositions = [{'id': i, 'status': e['disposition']['status'], 'spec_ref': e['disposition']['spec_ref'],
                     'duplicate_of': e['disposition']['duplicate_of'], 'fixed_in': None, 'note': e['disposition']['note'],
                     'decided_at': e.get('decided_at')}
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
    path = fresh_name(state / 'reports', name, '.md')
    if rows:
        save(fresh_name(state / 'dispositions', path.stem, '.json'), dispositions)
    private_write(path, '\n'.join(lines) + '\n')
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
                if in_git(args.decisions):
                    raise Refused('the decisions file must be outside every Git checkout (use the state directory)')
                print(f'report {record(args.state, parse(args.decisions.read_text(), "decisions"))}')
    except Refused as error:
        print(f'REFUSED {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
