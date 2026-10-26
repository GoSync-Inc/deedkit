#!/usr/bin/env python3
"""Feedback follows its card (ANYM-473): apply triage dispositions, then move each status after its card ships.

    feedback_sync.py apply --dispositions FILE [--apply] [--host deedkit-prod] [--state DIR]
    feedback_sync.py follow [--apply] [--host deedkit-prod] [--state DIR]
    feedback_sync.py auto [--apply] [--triage-state DIR] [--host deedkit-prod] [--state DIR]
    feedback_sync.py auto --retire FILE [--state DIR]
    feedback_sync.py status [--state DIR]


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

`auto` is the unattended hourly run (ANYM-536): every triage dispositions file the ledger `applied.json` does not
record as complete, each row written only when its item is unchanged since the row's `decided_at`; a `received`
item named in the `Feedback:` line of exactly one card on Tiden main is linked to it; then `follow`; a card that left
main is reported, never guessed. All reads finish before the first write, and `--apply` writes `last-run.json`, which
`status` prints (exit 1 when the last good run is older than 3 hours or a call with an unknown outcome blocks writes).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
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
# ANYM-536: the card line that links feedback to it; the ID anywhere else in a card (History, a quote) does not.
FEEDBACK_LINE = re.compile(r'^Feedback: ({0}(?:, {0})*)[ \t]*\r?$'.format(UUID.pattern), re.MULTILINE)
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})')
STALE = timedelta(hours=3)
# Queue outcomes that close a row: written, already so, newer than its decision, or kept by the never-down rule.
CLOSED = ('written', 'unchanged', 'superseded', 'further along', 'agent verdict kept')
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


def dispositions(path, text=None):
    """id -> target; with `text` (the bytes `auto` hashed) also id -> the row's decision time or None (ANYM-536)."""
    if triage.in_git(path):
        raise Refused('the dispositions file must be outside every Git checkout')
    rows = triage.parse(path.read_text() if text is None else text, 'dispositions')
    if not isinstance(rows, list):
        raise Refused('dispositions must be a JSON list')
    targets, decided = {}, {}
    for n, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) - {'decided_at'} != {'id', *FIELDS} or not UUID.fullmatch(str(row['id'])):
            raise Refused(f'row {n}: needs exactly id, {", ".join(FIELDS)} with a lower-case feedback UUID')
        if row['id'] in targets:
            raise Refused(f'row {n}: {row["id"]} appears twice')
        wrong = problems(row)
        if row.get('decided_at') is not None:
            try:
                moment(row['decided_at'], 'decided_at')
            except Refused:
                wrong.append('decided_at')
        if wrong:
            raise Refused(f'row {n} ({row["id"]}): invalid {", ".join(wrong)}')
        targets[row['id']] = {field: row[field] for field in FIELDS}
        decided[row['id']] = row.get('decided_at')
    return targets if text is None else (targets, decided)


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
UNLINKABLE = re.compile(r'refused: (unknown_feedback|changed_since_plan)$')
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


def pending_check(state, state_now, head, settle=True):
    """True when writes may go on: no recorded call, or one this inventory resolves conclusively (ANYM-473
    criterion 8): it landed, or it can no longer land because the head is past its not-after bound plus GUARD.
    `settle` False (an `auto` dry run) only answers and records nothing."""
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
    if not settle:
        return True
    audit(state, dict(summary, outcome=outcome))
    pending_clear(state)
    print(f'the recorded call for {len(ids)} items {outcome}')
    return True


def write(state, host, digest, action, rows, since, until, run=remote_write, done=None):
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
        if done:
            done(ids)


# --- auto (ANYM-536) ---------------------------------------------------------------------------------------------

def digest_of(target):
    return hashlib.sha256(json.dumps({field: target[field] for field in FIELDS}, sort_keys=True).encode()).hexdigest()


def ledger_load(state):
    ledger = triage.load(state / 'applied.json', {'files': {}})
    decided = ledger.get('decided', {}) if isinstance(ledger, dict) else None
    if not isinstance(ledger, dict) or not isinstance(ledger.get('files'), dict) or not isinstance(decided, dict) or \
            not all(isinstance(keys, dict) and all(isinstance(text, str) for text in keys.values())
                    for keys in decided.values()):
        raise Refused('applied.json is malformed; nothing is written until it is inspected')
    return ledger


def queue(directory, ledger):
    """The triage dispositions files: (open {name: (sha256, targets, decided)}, legacy names, refused {name: why}).
    A ledgered file is hashed first and refused when it changed; a complete one is not read further."""
    files, legacy, refused = {}, [], {}
    for path in sorted(directory.glob('*.json')) if directory.is_dir() else []:
        raw = path.read_bytes()
        sha, known = hashlib.sha256(raw).hexdigest(), ledger['files'].get(path.name)
        if known and known.get('sha256') != sha:
            refused[path.name] = 'changed after it was ledgered'
        elif not (known and known.get('complete')):
            try:
                targets, decided = dispositions(path, raw.decode())
            except (Refused, UnicodeDecodeError) as error:
                refused[path.name] = str(error)
                continue
            if any(value is None for value in decided.values()):
                legacy.append(path.name)  # written before decided_at: a hand `apply` or `auto --retire`
            else:
                files[path.name] = (sha, targets, decided)
    return files, legacy, refused


def plan_queue(files, ledger, state_now, since):
    """One entry per item with an open row: (i, now, target, reason, unchanged_since, members, older). `members` are
    the open rows equal to the decision that stands, `older` the open rows a newer decision for the item replaced.
    The earliest decision time of every row seen goes to the ledger's `decided`, saved before any call, so it
    outlives a refused or removed source file."""
    earliest = {}

    def seen(key, text):
        value = moment(text, 'decided_at')
        earliest[key] = min(earliest.get(key, value), value)
    for i, keys in ledger.get('decided', {}).items():
        for key, text in keys.items():
            seen((i, key), text)
    for entry in ledger['files'].values():
        for i, row in (entry.get('rows') or {}).items():
            if row.get('decided_at'):
                seen((i, row['target']), row['decided_at'])
    opened = {}
    for name, (_, targets, decided) in files.items():
        done = (ledger['files'].get(name) or {}).get('rows') or {}
        for i, target in targets.items():
            seen((i, digest_of(target)), decided[i])  # a republished equal row keeps the earliest decision time
            if i not in done:
                opened.setdefault(i, []).append((name, digest_of(target), target))
    remembered = {}
    for (i, key), value in sorted(earliest.items()):
        remembered.setdefault(i, {})[key] = value.isoformat()
    if remembered:
        ledger['decided'] = remembered
    bound, out = datetime.fromisoformat(since), []
    for i, rows in sorted(opened.items()):
        name, key, target = max(rows, key=lambda row: (earliest[i, row[1]], row[0]))
        decided = earliest[i, key]
        members = [(n, k, decided) for n, k, _ in rows if k == key]
        older = [(n, k, earliest[i, k]) for n, k, _ in rows if k != key]
        now = state_now.get(i, received())
        chosen, reason = decide(now, target) if settled(now, since) else (None, 'changed during read')
        if chosen and now['changed_at'] is not None and now['changed_at'] > decided:
            chosen, reason = None, 'superseded'  # a hand correction or a later run stands
        out.append((i, now, chosen, 'disposition' if chosen else reason, min(decided, bound).isoformat(), members, older))
    return out


def ledger_mark(ledger, files, name, i, key, decided, outcome):
    entry = ledger['files'].setdefault(name, {'sha256': files[name][0], 'complete': False, 'rows': {}})
    entry['rows'][i] = {'outcome': outcome, 'decided_at': decided.isoformat(), 'target': key}
    entry['complete'] = set(entry['rows']) >= set(files[name][1])


def prose(text):
    """The lines of a card outside fenced code (CommonMark: a fence closes on the same character, at least as long,
    with nothing after it) and HTML comments; a line that opens, holds or closes a comment is dropped."""
    fence, comment = None, False
    for line in text.splitlines():
        mark = FENCE.match(line)
        if fence:
            if mark and mark.group(1)[0] == fence[0] and len(mark.group(1)) >= len(fence) and \
                    not line[mark.end():].strip():
                fence = None
            continue
        if mark and not comment and not (mark.group(1)[0] == '`' and '`' in line[mark.end():]):
            fence = mark.group(1)  # a backtick fence's info string holds no backtick
            continue
        rest, touched = line, comment or '<!--' in line
        while True:
            cut = rest.find('-->' if comment else '<!--')
            if cut < 0:
                break
            rest, comment = rest[cut + (3 if comment else 4):], not comment
        if not touched:
            yield line


def links(main):
    """feedback ID -> the cards on main whose own `Feedback: <id>[, <id>]` line names it; a line in a fenced block,
    an HTML comment or a quote is an example, not a link."""
    out = {}
    for spec in main:
        for line in FEEDBACK_LINE.finditer('\n'.join(prose(spec['text']))):
            for i in line.group(1).split(', '):
                out.setdefault(i, set()).add(spec['ref'])
    return out


def plan_link(state_now, linked, since, busy):
    """An item the inventory lacks was never set (or set after the head, which the setter refuses)."""
    rows = []
    for i in sorted(linked):
        now = state_now.get(i, received())
        if i in busy or now['status'] != 'received' or now['spec_ref']:
            continue
        refs = sorted(linked[i])
        if len(refs) > 1:
            rows.append((i, now, None, 'several cards'))
        elif not settled(now, since):
            rows.append((i, now, None, 'changed during read'))
        else:
            rows.append((i, now, dict(now, status='accepted', spec_ref=refs[0],
                                      note=f'Carded as {refs[0]}; it follows that card.'), 'carded'))
    return rows


def age(now, head):
    return round((head - now['changed_at']).total_seconds() / 3600, 1) if now['changed_at'] else None


def auto(args, ssh, read, live, gh, run, root, fetch):
    """The unattended run under the sync lock; `--apply` always ends by writing last-run.json."""
    state, started = args.state, datetime.now(timezone.utc)
    try:
        previous = triage.load(state / 'last-run.json', {})
    except ValueError:
        previous = {}  # a damaged report never stops the run that replaces it
    report = {'version': 1, 'at': started.isoformat(), 'exit': 1, 'export_head': None, 'written': 0, 'error': None,
              'reasons': {}, 'waiting_for_card': [], 'waiting_for_main': [], 'pending': None,
              'files': {'complete': [], 'open': [], 'legacy': [], 'refused': {}},
              'last_ok': previous.get('last_ok') if isinstance(previous, dict) else None}
    try:
        code = auto_run(args, ssh, read, live, gh, run, root, fetch, report)
    except (Refused, ValueError) as error:
        print(f'REFUSED {error}', file=sys.stderr)
        report['error'], code = str(error), 1
    except Exception as error:  # a malformed read never skips the report; only the type, never read content
        print(f'REFUSED unexpected {type(error).__name__}', file=sys.stderr)
        report['error'], code = f'unexpected {type(error).__name__}', 1
    if args.apply:
        if (state / 'pending.json').exists():
            try:
                entry = triage.load(state / 'pending.json', {})
            except ValueError:
                entry = None
            report['pending'] = {key: entry.get(key) for key in ('action', 'ids', 'to', 'not_after')} \
                if isinstance(entry, dict) else {'malformed': True}
        report['exit'] = code
        if code == 0:
            report['last_ok'] = report['at']
        triage.save(state / 'last-run.json', report)
    return code


def auto_run(args, ssh, read, live, gh, run, root, fetch, report):
    state, files_report = args.state, report['files']
    # An unattended run never waits on a prompt: ssh runs with BatchMode, git and gh with prompts off.
    os.environ['GIT_TERMINAL_PROMPT'], os.environ['GH_PROMPT_DISABLED'] = '0', '1'
    ledger = ledger_load(state)
    files, files_report['legacy'], files_report['refused'] = queue(args.triage_state / 'dispositions', ledger)
    known = triage.load(state / 'cards.json', {'seen': {}})
    if not isinstance(known, dict) or not isinstance(known.get('seen'), dict):
        raise Refused('cards.json is malformed')
    # Every read comes before the first write: inventory, Tiden main, gh and git, the receipts.
    digest = image(args.host, ssh)
    state_now, head = current(args.host, digest, ssh)
    report['export_head'] = head.isoformat()
    since, until = (head - GUARD).isoformat(), (head + WINDOW).isoformat()
    free = pending_check(state, state_now, head, settle=args.apply)
    main = triage.flatten((read or triage.tiden)('spec', 'list', '--branch', 'main'), 'main')
    status = {spec['ref']: spec['status'] for spec in main}
    queued = plan_queue(files, ledger, state_now, since)
    busy = {entry[0] for entry in queued if entry[2]}
    cards_of = links(main)
    linked = plan_link(state_now, cards_of, since, busy)
    rest = {i: now for i, now in state_now.items() if i not in busy | {row[0] for row in linked}}
    refs = {ref for ref in wanted(rest, since) if status.get(ref) == 'Done'}
    followed = []
    for i, now, target, reason in plan_follow(rest, status, delivering(refs, root, fetch, gh) if refs else {},
                                              live or live_probe(args.host, root), since):
        if reason == 'card not on main' and now['status'] in ('received', 'accepted'):
            reason = 'card missing' if now['spec_ref'] in known['seen'] else 'waiting for main'
            if reason == 'waiting for main':
                report['waiting_for_main'].append({'feedback': i, 'card': now['spec_ref'], 'age_hours': age(now, head)})
        elif reason == 'no card' and now['status'] == 'received':
            reason = 'waiting for a card'
            report['waiting_for_card'].append({'feedback': i, 'age_hours': age(now, head)})
        followed.append((i, now, target, reason))
    sections = (('apply queue', [entry[:4] for entry in queued]), ('link', linked), ('follow', followed))
    writes = 0
    for title, rows in sections:
        print(f'## {title}')
        writes += show(rows)
        for i, now, target, reason in rows:
            row = {'feedback': i, 'card': (target or now)['spec_ref']}
            report['reasons'].setdefault(reason, []).append(dict(row, cards=sorted(cards_of[i])) if
                                                            reason == 'several cards' else row)
    code = 1 if files_report['refused'] else 0
    for name, why in sorted(files_report['refused'].items()):
        print(f'REFUSED {name}: {why}', file=sys.stderr)
    if not args.apply:
        print('dry run: nothing written or recorded (add --apply)')
        return code
    known['seen'].update({ref: head.isoformat() for ref in status if ref not in known['seen']})
    triage.save(state / 'cards.json', known)
    members = {}
    for i, _, target, reason, _, rows, older in queued:
        members[i] = rows
        for name, key, decided in older:
            ledger_mark(ledger, files, name, i, key, decided, 'superseded')
        for name, key, decided in rows if reason in CLOSED else ():
            ledger_mark(ledger, files, name, i, key, decided, reason)
    for name in files:
        ledger['files'].setdefault(name, {'sha256': files[name][0], 'complete': not files[name][1], 'rows': {}})
    triage.save(state / 'applied.json', ledger)
    try:
        if writes and not free:
            raise Refused('a call with an unknown outcome blocks writes')

        def done(ids):
            for i in ids:
                for name, key, decided in members.get(i, ()):
                    ledger_mark(ledger, files, name, i, key, decided, 'written')
            triage.save(state / 'applied.json', ledger)
            counted(ids)

        def counted(ids):  # link and follow calls close no queue row
            report['written'] += len(ids)
        buckets = {}
        for i, now, target, _, unchanged, _, _ in queued:
            if target:
                buckets.setdefault(unchanged, []).append((i, now, target, 'disposition'))
        for unchanged in sorted(buckets, key=datetime.fromisoformat):
            write(state, args.host, digest, 'apply', buckets[unchanged], unchanged, until, run, done)
        known_rows = [row for row in linked if row[0] in state_now]
        if any(row[2] for row in known_rows):
            write(state, args.host, digest, 'link', known_rows, since, until, run, counted)
        for row in linked:
            if row[2] and row[0] not in state_now:  # one call each: an unknown ID refuses only its own call
                try:
                    write(state, args.host, digest, 'link', [row], since, until, run, counted)
                except Refused as error:
                    if not UNLINKABLE.search(str(error)):
                        raise
                    report['reasons'].setdefault('not linked', []).append({'feedback': row[0], 'card': row[2]['spec_ref']})
        if any(row[2] for row in followed):
            write(state, args.host, digest, 'follow', followed, since, until, run, counted)
    finally:
        for name in sorted(files):
            files_report['complete' if ledger['files'][name]['complete'] else 'open'].append(name)
    print(f'written {report["written"]}')
    return code


def retire(state, path):
    if triage.in_git(path):
        raise Refused('the dispositions file must be outside every Git checkout')
    ledger = ledger_load(state)
    ledger['files'][path.name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'complete': True,
                                  'retired': True, 'rows': {}}
    triage.save(state / 'applied.json', ledger)
    print(f'retired {path.name}: `auto` never applies it')


def status_report(state, now=None):
    """Prints last-run.json; exit 1 when the last exit-0 `auto --apply` run is older than STALE or a call with an
    unknown outcome blocks writes."""
    now, path = now or datetime.now(timezone.utc), state / 'last-run.json'
    last = triage.parse(path.read_text(), 'last-run.json') if path.exists() else {}
    print(json.dumps(last, indent=1, sort_keys=True))
    alerts = []
    try:
        ok = triage.when(last['last_ok']) if isinstance(last, dict) and last.get('last_ok') else None
    except (TypeError, ValueError):
        ok = None
    if ok is None or now - ok > STALE:
        alerts.append(f'no exit-0 `auto --apply` run in the last {STALE.total_seconds() / 3600:.0f} hours')
    if (state / 'pending.json').exists() or (isinstance(last, dict) and last.get('pending')):
        alerts.append('a call with an unknown outcome blocks writes')
    for alert in alerts:
        print(f'ATTENTION {alert}', file=sys.stderr)
    return 1 if alerts else 0


def main(argv=None, ssh=None, read=None, live=None, gh=None, run=remote_write, root=ROOT, fetch=True):
    parser = argparse.ArgumentParser(description='Feedback follows its card (ANYM-473, ANYM-536).')
    parser.add_argument('action', choices=['apply', 'follow', 'auto', 'status'])
    parser.add_argument('--dispositions', type=Path)
    parser.add_argument('--apply', action='store_true', help='write; without it the run only prints the plan')
    parser.add_argument('--state', type=Path, default=Path(os.environ.get('FEEDBACK_SYNC_STATE', STATE)))
    parser.add_argument('--triage-state', type=Path, default=Path(os.environ.get('FEEDBACK_TRIAGE_STATE', triage.STATE)))
    parser.add_argument('--retire', type=Path, help='auto: record a legacy dispositions file complete without a write')
    parser.add_argument('--host', default=HOST)
    args = parser.parse_args(argv)
    if (args.action == 'apply') != bool(args.dispositions):
        parser.error('apply needs --dispositions, the other actions take none')
    if args.retire and args.action != 'auto':
        parser.error('--retire is an auto option')
    if args.action == 'status':
        return status_report(args.state)
    ssh = ssh or triage.remote
    try:
        with triage.locked(args.state):
            if args.action == 'auto':
                if args.retire:
                    retire(args.state, args.retire)
                    return 0
                return auto(args, ssh, read, live, gh, run, root, fetch)
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
