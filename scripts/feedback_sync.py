#!/usr/bin/env python3
"""Feedback follows its card (ANYM-473): apply triage dispositions, then move each status after its card ships.

    feedback_sync.py apply --dispositions FILE [--apply] [--host deedkit-prod] [--state DIR]
    feedback_sync.py follow [--apply] [--host deedkit-prod] [--state DIR]


Both actions first read every disposition from production (the full `export-feedback --changed-since` inventory,
disposition fields only, never a description), plan one target per item and print the plan. Only `--apply` writes,
through `set-feedback-status --unchanged-since T --not-after D` in the operator container of the running API image
(ANYM-133, ANYM-343), grouped by identical arguments; T is the inventory's head minus 10 s, so a disposition changed
after the plan is never overwritten, and D its head plus 15 minutes, so a call commits by D + 5 s or never. `apply` applies the permitted transitions of a triage dispositions file (ANYM-264) and
reports the skipped differences. `follow` reads Tiden main, the merged pull request whose body delivers the card
(`Delivers:`) and the release receipts (scripts/receipt_watch.py): a Done card whose commit is LIVE asks the agent to
re-check (`verify_requested` at the live SHA), a Done card merged but not LIVE is `fixed` at its commit, a card on main
not yet Done makes a `received` item `accepted`. A status never moves down, and agent verdicts (verified, reopened)
are never changed. Every write call is recorded in `pending.json` before it is sent and audited to a private file; a
call with an unknown outcome stops the run, is never retried, and blocks writes until a later inventory shows it
landed or, read after D + 10 s, that it can no longer land. Runs on the orchestrator machine (Tiden token, gh, git, the owner's SSH); exit 0 done,
1 refused or stopped, 2 usage.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

import feedback_triage as triage  # noqa: E402
import receipt_watch  # noqa: E402
import tiden_pr_check  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STATE = Path.home() / '.deedkit' / 'feedback-sync'
HOST = 'deedkit-prod'
SSH_TIMEOUT = 120
GIT_TIMEOUT = 120
LIVE_BUDGET = 300
EPOCH = '1970-01-01T00:00:00+00:00'
BATCH = 100
# The setter's 5 s transaction bound plus margin: a disposition set at or before head - GUARD committed before the
# first export page was read, so it is in the inventory (ANYM-473).
GUARD = timedelta(seconds=10)
WINDOW = timedelta(minutes=15)
REPO = 'GoSync-Inc/deedkit'
PR_LIMIT = 50
# The order a status moves in; declined and duplicate are triage verdicts at the level of accepted.
RANK = {'received': 0, 'accepted': 1, 'declined': 1, 'duplicate': 1, 'fixed': 2, 'verify_requested': 3}
AGENT_VERDICTS = ('verified', 'reopened')
FIELDS = ('status', 'spec_ref', 'duplicate_of', 'fixed_in', 'note')
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
SPEC = re.compile(r'ANYM-[1-9][0-9]{0,5}')
FIXED_IN = re.compile(r'deedkit@[0-9a-f]{40}')
SHA = re.compile(r'[0-9a-f]{40}')
OPERATOR = [('deedkit-feedback-sync' if word == 'deedkit-feedback-export' else word) for word in triage.EXPORT]
Refused = triage.Refused


class Uncertain(Exception):
    pass


def note_ok(note):
    return note is None or (isinstance(note, str) and 1 <= len(note) <= triage.NOTE_LIMIT and bool(note.strip())
                            and not any(ord(ch) < 32 or ord(ch) == 127 for ch in note))


def problems(row):
    """The operator's own refusal rules (ANYM-343), checked before anything is read or written."""
    status, spec, fixed_in = row.get('status'), row.get('spec_ref'), row.get('fixed_in')
    out = []
    if status not in RANK:
        out.append('status')
    if spec is not None and not (isinstance(spec, str) and SPEC.fullmatch(spec)):
        out.append('spec_ref')
    if row.get('duplicate_of') is not None and not (isinstance(row['duplicate_of'], str) and UUID.fullmatch(row['duplicate_of'])):
        out.append('duplicate_of')
    if fixed_in is not None and not (isinstance(fixed_in, str) and FIXED_IN.fullmatch(fixed_in)):
        out.append('fixed_in')
    if not note_ok(row.get('note')):
        out.append('note')
    if status in ('accepted', 'fixed', 'verify_requested') and spec is None:
        out.append('spec_ref required')
    if (status in ('fixed', 'verify_requested')) != (fixed_in is not None):
        out.append('fixed_in only and always for fixed or verify_requested')
    if status == 'duplicate' and row.get('duplicate_of') is None and spec is None:
        out.append('duplicate needs duplicate_of or spec_ref')
    if status != 'duplicate' and row.get('duplicate_of') is not None:
        out.append('duplicate_of only for duplicate')
    return out


def dispositions(path):
    if triage.in_git(path):
        raise Refused('the dispositions file must be outside every Git checkout')
    rows = triage.parse(path.read_text(), 'dispositions')
    if not isinstance(rows, list):
        raise Refused('dispositions must be a JSON list')
    targets = {}
    for n, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {'id', *FIELDS} or not UUID.fullmatch(str(row['id'])):
            raise Refused(f'row {n}: needs exactly id, {", ".join(FIELDS)} with a lower-case feedback UUID')
        if row['id'] in targets:
            raise Refused(f'row {n}: {row["id"]} appears twice')
        wrong = problems(row)
        if wrong:
            raise Refused(f'row {n} ({row["id"]}): invalid {", ".join(wrong)}')
        targets[row['id']] = {field: row[field] for field in FIELDS}
    return targets


# --- reads -------------------------------------------------------------------------------------------------------

def image(host, ssh):
    digest = ssh(host, ['docker', 'inspect', '--format', '{{.Image}}', triage.API_CONTAINER]).strip()
    if not triage.IMAGE.fullmatch(digest):
        raise Refused('the API container image is not a digest')
    return digest


def moment(text, what):
    try:
        value = datetime.fromisoformat(text) if isinstance(text, str) and len(text) <= 64 else None
    except ValueError:
        value = None
    if value is None or value.tzinfo is None:
        raise Refused(f'export: malformed {what}')
    return value


def current(host, digest, ssh):
    """(id -> disposition of every item ever set, export_head). An item absent from the inventory was never set
    (`received`) or was set after head - GUARD, which the conditional write refuses (ANYM-473 criteria 2, 3)."""
    command = triage.EXPORT + [digest, 'python', '-m', 'messaging_service', 'export-feedback']
    page, out, head = None, {}, None
    for _ in range(triage.MAX_PAGES):
        reply = triage.parse(ssh(host, command + (['--page', page] if page else ['--changed-since', EPOCH])), 'export')
        if not isinstance(reply, dict) or reply.get('mode') != 'changed' or not isinstance(reply.get('dispositions'), list):
            raise Refused('export: unexpected reply')
        if head is None:
            head = reply.get('export_head')
        if reply.get('export_head') != head:
            raise Refused('export: the pages name different heads')
        bound = moment(head, 'export_head')
        for row in reply['dispositions']:
            if not isinstance(row, dict) or not UUID.fullmatch(str(row.get('id'))) or \
                    row.get('status') not in (*RANK, *AGENT_VERDICTS) or row['id'] in out:
                raise Refused('export: malformed disposition')
            changed = moment(row.get('status_changed_at'), 'status_changed_at')
            if changed > bound:
                raise Refused('export: a disposition after export_head')
            out[row['id']] = {'status': row['status'], 'spec_ref': row.get('spec_ref'),
                              'duplicate_of': row.get('duplicate_of'), 'fixed_in': row.get('fixed_in'),
                              'note': row.get('status_note'), 'changed_at': changed}
        more, page = reply.get('has_more'), reply.get('next_page')
        if more is False and page is None:
            return out, bound
        if more is not True or not isinstance(page, str) or not triage.PAGE.fullmatch(page):
            raise Refused('export: malformed pagination')
    raise Refused(f'export: more than {triage.MAX_PAGES} pages')


def received():
    return {'status': 'received', 'spec_ref': None, 'duplicate_of': None, 'fixed_in': None, 'note': None,
            'changed_at': None}


def git(root, *argv, timeout=GIT_TIMEOUT):
    return triage.run(['git', '-C', str(root), *argv], timeout)


def delivers(body):
    """The cards a pull request body delivers, by sync B's own parser (the `Delivers:` lines it sets Done from)."""
    return {f'ANYM-{n}' for n in tiden_pr_check.parse_body(body if isinstance(body, str) else '').delivers}


def ancestor(root, sha):
    """True when the commit is on origin/main; a commit the fetched history does not hold is not."""
    try:
        if subprocess.run(['git', '-C', str(root), 'cat-file', '-e', sha + '^{commit}'], capture_output=True,
                          timeout=GIT_TIMEOUT).returncode:
            return False
        done = subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', sha, 'origin/main'],
                              capture_output=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused('git merge-base failed') from None
    if done.returncode not in (0, 1):
        raise Refused(f'git merge-base exited {done.returncode}')
    return done.returncode == 0


def delivering(refs, root=ROOT, fetch=True, gh=None):
    """card -> the merge commit of the one merged pull request whose body delivers it, on origin/main; a card with
    none or more than one such pull request is absent (no change)."""
    if fetch:
        git(root, 'fetch', '-q', 'origin', 'main')
    gh = gh or (lambda argv: triage.run(argv, GIT_TIMEOUT))
    out = {}
    for ref in sorted(refs):
        reply = triage.parse(gh(['gh', 'pr', 'list', '-R', REPO, '--state', 'merged', '--search',
                                 f'"{ref}" in:body', '--json', 'number,body,mergeCommit',
                                 '--limit', str(PR_LIMIT)]), 'gh pr list')
        if not isinstance(reply, list) or len(reply) >= PR_LIMIT:
            raise Refused(f'gh pr list: unexpected or truncated reply for {ref}')
        shas = {((pr.get('mergeCommit') or {}).get('oid') or '') for pr in reply
                if isinstance(pr, dict) and ref in delivers(pr.get('body'))}
        if len(shas) == 1 and SHA.fullmatch(sha := shas.pop()) and ancestor(root, sha):
            out[ref] = sha
    return out


def cards(read=None):
    return {spec['ref']: spec['status'] for spec in triage.flatten((read or triage.tiden)('spec', 'list', '--branch', 'main'),
                                                                   'main')}


def live_probe(host, root=ROOT, probe=None):
    """A function commit -> live SHA or None, from one read-only receipt snapshot (receipt_watch's LIVE rule)."""
    snapshot = (probe or receipt_watch.probe)(host)
    if not receipt_watch.valid(snapshot) or snapshot == {'unstable': True}:
        raise Refused('the release receipts are unstable; run again later')
    plan = (snapshot['plan_a'] or {}).get('sha', '')
    deadline = time.monotonic() + LIVE_BUDGET

    def live(commit):
        verdict = receipt_watch.Watch(commit, root, lambda: deadline - time.monotonic()).verdict(snapshot)
        return plan if verdict and verdict[0] == 'LIVE' and SHA.fullmatch(plan) else None
    return live


# --- plan --------------------------------------------------------------------------------------------------------

def decide(now, target):
    """(target or None, reason): the never-down and idempotency rules shared by both actions."""
    if now['status'] in AGENT_VERDICTS:
        return None, 'agent verdict kept'
    if all(now[field] == target[field] for field in FIELDS):
        return None, 'unchanged'
    if RANK[target['status']] < RANK[now['status']]:
        return None, 'further along'
    return target, 'write'


def follow_target(now, status, commit, live):
    """The card-driven target of one item, or (None, reason)."""
    ref = now['spec_ref']
    if not ref:
        return None, 'no card'
    if now['status'] in (*AGENT_VERDICTS, 'declined', 'duplicate'):
        return None, f'{now["status"]} kept'
    if status is None:
        return None, 'card not on main'
    if status != 'Done':
        if now['status'] != 'received':
            return None, 'card not delivered'
        return dict(now, status='accepted', note=f'Accepted as {ref}; it follows that card.'), 'card accepted'
    if commit is None:
        return None, 'no delivering commit on origin/main'
    sha = live(commit)
    if sha:
        target = dict(now, status='verify_requested', fixed_in='deedkit@' + sha,
                      note=f'Delivered by {ref}; live in deedkit@{sha[:12]}. Please re-check.')
    else:
        target = dict(now, status='fixed', fixed_in='deedkit@' + commit,
                      note=f'Delivered by {ref} in deedkit@{commit[:12]}; not live yet.')
    # follow only moves forward: a second deploy never rewrites an earlier fixed_in.
    return (target, 'card delivered') if RANK[target['status']] > RANK[now['status']] else (None, 'already ' + now['status'])


def settled(now, since):
    return now['changed_at'] is None or now['changed_at'] <= datetime.fromisoformat(since)


def plan_apply(state_now, targets, since):
    rows = []
    for i, target in sorted(targets.items()):
        now = state_now.get(i, received())
        chosen, reason = decide(now, target) if settled(now, since) else (None, 'changed during read')
        rows.append((i, now, chosen, reason))
    return rows


def plan_follow(state_now, card_status, commits, live, since):
    rows = []
    for i, now in sorted(state_now.items()):
        target, reason = follow_target(now, card_status.get(now['spec_ref']), commits.get(now['spec_ref']), live)
        if target is not None:
            chosen, why = decide(now, target)
            target, reason = (chosen, reason) if chosen else (None, why)
        if target is not None and not settled(now, since):
            target, reason = None, 'changed during read'
        rows.append((i, now, target, reason))
    return rows


def wanted(state_now, since):
    """The cards `follow` asks GitHub about: Done cards of items it could still move."""
    return {now['spec_ref'] for now in state_now.values() if now['spec_ref'] and settled(now, since)
            and now['status'] in ('received', 'accepted', 'fixed')}


def show(rows, out=print):
    totals = {}
    for i, now, target, reason in rows:
        totals[reason] = totals.get(reason, 0) + 1
        to = f"{target['status']} {target['spec_ref'] or '-'} {target['fixed_in'] or '-'}" if target else 'no change'
        out(f"{i} {now['status']} -> {to} ({reason})")
    writes = sum(1 for row in rows if row[2])
    out(f'items {len(rows)}; writes {writes}; ' + ', '.join(f'{k} {v}' for k, v in sorted(totals.items())))
    return writes


# --- writes ------------------------------------------------------------------------------------------------------

def audit(state, entry):
    descriptor = os.open(state / 'audit.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, 'a') as handle:
        handle.write(json.dumps(dict(entry, at=datetime.now(timezone.utc).isoformat()), sort_keys=True) + '\n')


def groups(rows):
    grouped = {}
    for i, _, target, _ in rows:
        if target:
            grouped.setdefault(tuple(target[field] for field in FIELDS), []).append(i)
    return [(dict(zip(FIELDS, key)), ids[k:k + BATCH]) for key, ids in sorted(grouped.items(), key=lambda kv: str(kv[0]))
            for k in range(0, len(ids), BATCH)]


def set_command(digest, target, ids, since, until):
    argv = OPERATOR + [digest, 'python', '-m', 'messaging_service', 'set-feedback-status', '--id', *ids,
                       '--status', target['status'], '--unchanged-since', since, '--not-after', until]
    for flag, field in (('--spec', 'spec_ref'), ('--duplicate-of', 'duplicate_of'), ('--fixed-in', 'fixed_in'),
                        ('--note', 'note')):
        argv += [flag, target[field]] if target[field] else []
    return argv


# The setter's own refusals: raised before its UPDATE or rolled back with it, so nothing was written. Exit 1 with
# `ValueError: <code>` from the operator CLI, or exit 2 from its argument parser (an older release without the guards).
REFUSALS = re.compile(r'^ValueError: (changed_since_plan|not_after_passed|unknown_feedback|duplicate_of_\w+|'
                      r'invalid_\w+|\w+_required|feedback_ids_1_to_100|status_reserved_for_reporting_agent)\b',
                      re.MULTILINE)
USAGE = re.compile(r': error: unrecognized arguments: --(unchanged-since|not-after)')


def remote_write(host, argv):
    """Exit 0: done. Only the setter's explicit refusal is a definite `Refused` (nothing written); any other failure
    (timeout, SSH or docker failure, a signal) leaves the outcome unknown. Remote output is matched, never shown."""
    try:
        done = subprocess.run(['ssh', '-o', 'BatchMode=yes', host, shlex.join(argv)], capture_output=True, text=True,
                              timeout=SSH_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Uncertain(type(error).__name__) from None
    if done.returncode == 0:
        return done.stdout
    refusal = REFUSALS.search(done.stderr) if done.returncode == 1 else None
    if refusal:
        raise Refused('set-feedback-status refused: ' + refusal.group(1))
    if done.returncode == 2 and USAGE.search(done.stderr):
        raise Refused('set-feedback-status rejected its arguments (is the release older than ANYM-473?)')
    raise Uncertain(f'exited {done.returncode}')


def pending_record(state, entry):
    """Written and flushed before a call is sent, so an unknown outcome survives the process (ANYM-473 criterion 8)."""
    path, temporary = state / 'pending.json', state / 'pending.json.tmp'
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(json.dumps(entry, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(state, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def pending_clear(state):
    (state / 'pending.json').unlink(missing_ok=True)


def pending_check(state, state_now, head):
    """True when writes may go on: no recorded call, or one this inventory resolves conclusively (ANYM-473
    criterion 8): it landed, or it can no longer land because the head is past its not-after bound plus GUARD."""
    path = state / 'pending.json'
    if not path.exists():
        return True
    entry = triage.parse(path.read_text(), 'pending.json')
    try:
        ids, target, deadline = entry['ids'], entry['target'], moment(entry['not_after'], 'pending not_after')
        moment(entry['unchanged_since'], 'pending unchanged_since')
        assert isinstance(ids, list) and 1 <= len(ids) <= BATCH and len(set(ids)) == len(ids)
        assert all(isinstance(i, str) and UUID.fullmatch(i) for i in ids)
        assert isinstance(target, dict) and set(target) == set(FIELDS) and not problems(target)
    except (TypeError, KeyError, AssertionError, Refused):
        raise Refused('pending.json is malformed; nothing is written until it is inspected') from None
    summary = {key: entry.get(key) for key in ('action', 'ids', 'to', 'spec_ref', 'fixed_in', 'not_after')}
    if all(all(state_now.get(i, received()).get(field) == target.get(field) for field in FIELDS) for i in ids):
        outcome = 'landed'
    elif head > deadline + GUARD:
        outcome = 'expired'
    else:
        print(f'PENDING a call for {len(ids)} items has an unknown outcome and has not landed; writes stay blocked '
              f'until it lands or the export head passes {entry["not_after"]} + 10 s', file=sys.stderr)
        return False
    audit(state, dict(summary, outcome=outcome))
    pending_clear(state)
    print(f'the recorded call for {len(ids)} items {outcome}')
    return True


def write(state, host, digest, action, rows, since, until, run=remote_write):
    before = {i: now['status'] for i, now, target, _ in rows if target}
    for target, ids in groups(rows):
        entry = {'action': action, 'ids': ids, 'from': {i: before[i] for i in ids}, 'to': target['status'],
                 'spec_ref': target['spec_ref'], 'fixed_in': target['fixed_in'], 'duplicate_of': target['duplicate_of'],
                 'unchanged_since': since, 'not_after': until}
        pending_record(state, dict(entry, target=target))
        audit(state, dict(entry, outcome='started'))
        try:
            text = run(host, set_command(digest, target, ids, since, until))
            try:
                reply = json.loads(text)
            except (TypeError, ValueError):
                raise Uncertain('unreadable reply') from None
        except Uncertain as error:
            audit(state, dict(entry, outcome='uncertain', detail=str(error)))
            raise Refused(f'outcome unknown for {len(ids)} items ({error}); not retried. Writes stay blocked until '
                          'a later run sees it landed or expired') from None
        except Refused as error:
            pending_clear(state)
            audit(state, dict(entry, outcome='refused', detail=str(error)))
            raise
        if not isinstance(reply, dict) or sorted(reply.get('feedback_ids') or []) != sorted(ids):
            audit(state, dict(entry, outcome='uncertain', detail='unexpected reply'))
            raise Refused('set-feedback-status: unexpected reply; not retried')
        pending_clear(state)
        audit(state, dict(entry, outcome='ok'))


def main(argv=None, ssh=None, read=None, live=None, gh=None, run=remote_write, root=ROOT, fetch=True):
    parser = argparse.ArgumentParser(description='Feedback follows its card (ANYM-473).')
    parser.add_argument('action', choices=['apply', 'follow'])
    parser.add_argument('--dispositions', type=Path)
    parser.add_argument('--apply', action='store_true', help='write; without it the run only prints the plan')
    parser.add_argument('--state', type=Path, default=Path(os.environ.get('FEEDBACK_SYNC_STATE', STATE)))
    parser.add_argument('--host', default=HOST)
    args = parser.parse_args(argv)
    if (args.action == 'apply') != bool(args.dispositions):
        parser.error('apply needs --dispositions, follow takes none')
    ssh = ssh or triage.remote
    try:
        with triage.locked(args.state):
            targets = dispositions(args.dispositions) if args.action == 'apply' else None
            digest = image(args.host, ssh)
            state_now, head = current(args.host, digest, ssh)
            since, until = (head - GUARD).isoformat(), (head + WINDOW).isoformat()
            free = pending_check(args.state, state_now, head)
            if args.action == 'apply':
                rows = plan_apply(state_now, targets, since)
            else:
                status = cards(read)
                refs = {ref for ref in wanted(state_now, since) if status.get(ref) == 'Done'}
                rows = plan_follow(state_now, status, delivering(refs, root, fetch, gh),
                                   live or live_probe(args.host, root), since)
            writes = show(rows)
            if writes and not args.apply:
                print('dry run: nothing written (add --apply)')
            elif writes and not free:
                return 1
            elif writes:
                write(args.state, args.host, digest, args.action, rows, since, until, run)
                print(f'written {writes}')
    except (Refused, ValueError) as error:
        print(f'REFUSED {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
