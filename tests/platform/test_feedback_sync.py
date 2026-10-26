"""Feedback follows its card: apply triage dispositions, follow the card to fixed or re-check (ANYM-473)."""
from datetime import datetime, timedelta, timezone
import fcntl
import json
import subprocess

import pytest

import scripts.feedback_sync as sync

IMAGE = 'sha256:' + 'c' * 64
IDS = ['22222222-2222-4222-8222-%012d' % n for n in range(1, 160)]
A, B, C, D, E, F, G, H, I, J = IDS[:10]  # noqa: E741
LIVE, MERGED, OLD = 'a' * 40, 'b' * 40, 'd' * 40
START = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)


def disp(status='received', spec=None, dup=None, fixed=None, note=None):
    return {'status': status, 'spec_ref': spec, 'duplicate_of': dup, 'fixed_in': fixed, 'note': note}


def flags(argv):
    out, key = {'--id': []}, None
    for word in argv[argv.index('set-feedback-status') + 1:]:
        if word.startswith('--'):
            key = word
        elif key == '--id':
            out['--id'].append(word)
        else:
            out[key] = word
    return out


class Prod:
    """The operator commands over SSH: the changed-mode export (paged by 2, one head per export) and the atomic
    conditional setter. A minute passes between every export and write; `at` None is an item never set."""

    def __init__(self, rows):
        self.rows = {i: dict(d, at=d.get('at', START)) for i, d in rows.items()}
        self.calls, self.writes, self.clock, self.head = [], [], START + timedelta(hours=1), None

    def tick(self):
        self.clock += timedelta(minutes=1)
        return self.clock

    def ssh(self, host, argv):
        self.calls.append(argv)
        if argv[:2] == ['docker', 'inspect']:
            return IMAGE + '\n'
        args = argv[argv.index('export-feedback') + 1:]
        assert args == ['--changed-since', sync.EPOCH] or (len(args) == 2 and args[0] == '--page')
        if args[0] != '--page':
            self.head = self.tick()
        ids = sorted(i for i, d in self.rows.items() if d['at'] is not None and d['at'] <= self.head)
        start = int(args[1]) if args[0] == '--page' else 0
        page, more = ids[start:start + 2], start + 2 < len(ids)
        rows = [{'id': i, 'status': self.rows[i]['status'], 'spec_ref': self.rows[i]['spec_ref'],
                 'duplicate_of': self.rows[i]['duplicate_of'], 'fixed_in': self.rows[i]['fixed_in'],
                 'status_note': self.rows[i]['note'], 'status_changed_at': self.rows[i]['at'].isoformat()} for i in page]
        return json.dumps({'untrusted_content': True, 'mode': 'changed', 'export_head': self.head.isoformat(),
                           'dispositions': rows, 'has_more': more, 'next_page': str(start + 2) if more else None})

    def run(self, host, argv):
        self.writes.append(argv)
        given = flags(argv)
        since, until = datetime.fromisoformat(given['--unchanged-since']), datetime.fromisoformat(given['--not-after'])
        if self.clock > until:
            raise sync.Refused('set-feedback-status exited 1')
        if any(self.rows.get(i, {}).get('at') and self.rows[i]['at'] > since for i in given['--id']):
            raise sync.Refused('set-feedback-status exited 1')
        at = self.tick()
        for i in given['--id']:
            self.rows[i] = dict(disp(given['--status'], given.get('--spec'), given.get('--duplicate-of'),
                                     given.get('--fixed-in'), given.get('--note')), at=at)
        return json.dumps({'updated': len(given['--id']), 'status': given['--status'], 'feedback_ids': sorted(given['--id'])})


def state_dir(tmp_path):
    return tmp_path / 'sync-state'


def dispositions_file(tmp_path, rows):
    path = tmp_path / 'dispositions.json'
    path.write_text(json.dumps([dict(d, id=i) for i, d in rows.items()]))
    return path


def apply(tmp_path, prod, rows, *extra, run=None):
    return sync.main(['apply', '--dispositions', str(dispositions_file(tmp_path, rows)),
                      '--state', str(state_dir(tmp_path)), *extra], ssh=prod.ssh, run=run or prod.run)


def audited(tmp_path):
    return [json.loads(line) for line in (state_dir(tmp_path) / 'audit.jsonl').read_text().splitlines()]


def shown(prod, i):
    return {key: value for key, value in prod.rows[i].items() if key != 'at'}


# spec: ANYM-473
@pytest.mark.parametrize('row', [
    dict(disp('verified', 'ANYM-10')), dict(disp('reopened')), dict(disp('bogus')),
    dict(disp('accepted', 'anym-10')), dict(disp('accepted')), dict(disp('accepted', 'ANYM-10', fixed='deedkit@' + LIVE)),
    dict(disp('verify_requested', 'ANYM-10')), dict(disp('fixed', 'ANYM-10', fixed='deedkit@abc')),
    dict(disp('received', note='two\nlines')), dict(disp('received', note='x' * 281)), dict(disp('received', note='  ')),
    dict(disp('duplicate')), dict(disp('accepted', 'ANYM-10', dup=B)), dict(disp('duplicate', dup='not-a-uuid')),
    dict(disp('received'), extra=1),
], ids=['verified', 'reopened', 'unknown', 'spec-case', 'accepted-no-spec', 'fixed-in-on-accepted',
        'verify-no-fixed-in', 'fixed-in-format', 'note-control', 'note-long', 'note-blank', 'duplicate-no-target',
        'duplicate-of-on-accepted', 'duplicate-of-format', 'extra-key'])
def test_dispositions_file_is_validated_before_any_read(tmp_path, row):
    prod = Prod({})
    assert apply(tmp_path, prod, {A: row}) == 1
    assert prod.calls == [] and prod.writes == []


# spec: ANYM-473
def test_a_duplicated_id_an_upper_case_id_or_a_file_in_git_refuses_the_whole_file(tmp_path):
    prod = Prod({})
    path = tmp_path / 'dispositions.json'
    for rows in ([dict(disp(), id=A), dict(disp(), id=A)], [dict(disp(), id='ABCDEF01-2222-4222-8222-000000000001')], {'id': A}):
        path.write_text(json.dumps(rows))
        assert sync.main(['apply', '--dispositions', str(path), '--state', str(state_dir(tmp_path))],
                         ssh=prod.ssh, run=prod.run) == 1
    checkout = tmp_path / 'checkout'
    (checkout / '.git').mkdir(parents=True)
    (checkout / 'd.json').write_text('[]')
    assert sync.main(['apply', '--dispositions', str(checkout / 'd.json'), '--state', str(state_dir(tmp_path))],
                     ssh=prod.ssh, run=prod.run) == 1
    assert prod.calls == []


# spec: ANYM-473
def test_decision_table_never_moves_down_and_keeps_agent_verdicts():
    for now in (*sync.RANK, *sync.AGENT_VERDICTS):
        for status in sync.RANK:
            target = disp(status, None if status in ('received', 'declined') else 'ANYM-10',
                          fixed='deedkit@' + LIVE if status in ('fixed', 'verify_requested') else None, note='new')
            chosen, reason = sync.decide(disp(now, 'ANYM-10'), target)
            if now in sync.AGENT_VERDICTS:
                assert (chosen, reason) == (None, 'agent verdict kept')
            elif sync.RANK[status] < sync.RANK[now]:
                assert (chosen, reason) == (None, 'further along'), (now, status)
            else:  # up, or a triage correction at the same rank
                assert chosen == target and reason == 'write', (now, status)
    assert sync.decide(disp('accepted', 'ANYM-10', note='n'), disp('accepted', 'ANYM-10', note='n')) == (None, 'unchanged')


# spec: ANYM-473
def test_apply_dry_run_writes_nothing_and_a_second_apply_is_a_no_op(tmp_path, capsys):
    prod = Prod({A: dict(disp(), at=None), B: disp('accepted', 'ANYM-10', note='kept'),
                 C: disp('verify_requested', 'ANYM-10', fixed='deedkit@' + LIVE), D: disp('verified', 'ANYM-10'),
                 E: disp('declined', 'ANYM-12', note='no')})
    rows = {A: disp('accepted', 'ANYM-10', note='matches'), B: disp('accepted', 'ANYM-10', note='kept'),
            C: disp('accepted', 'ANYM-10'), D: disp('accepted', 'ANYM-10'), E: disp('duplicate', dup=A)}
    assert apply(tmp_path, prod, rows) == 0
    assert prod.writes == []
    out = capsys.readouterr().out
    assert f'{A} received -> accepted ANYM-10 - (write)' in out  # absent from the inventory: never set
    assert f'{C} verify_requested -> no change (further along)' in out and f'{D} verified -> no change (agent verdict kept)' in out
    assert 'items 5; writes 2; agent verdict kept 1, further along 1, unchanged 1, write 2' in out
    assert 'dry run: nothing written' in out
    assert apply(tmp_path, prod, rows, '--apply') == 0
    assert len(prod.writes) == 2 and prod.rows[A]['status'] == 'accepted' and prod.rows[E]['duplicate_of'] == A
    prod.writes.clear()
    assert apply(tmp_path, prod, rows, '--apply') == 0
    assert prod.writes == [] and 'writes 0;' in capsys.readouterr().out


# spec: ANYM-473
def test_writes_are_grouped_with_the_exact_argv_and_split_at_100(tmp_path):
    prod = Prod({})
    many = {i: disp('accepted', 'ANYM-10', note='one note') for i in IDS[:150]}
    assert apply(tmp_path, prod, many, '--apply') == 0
    assert [len(flags(argv)['--id']) for argv in prod.writes] == [100, 50]
    first = prod.writes[0]
    assert first[:len(sync.OPERATOR)] == sync.OPERATOR and 'deedkit-feedback-sync' in first
    assert first[len(sync.OPERATOR):len(sync.OPERATOR) + 5] == [IMAGE, 'python', '-m', 'messaging_service',
                                                                 'set-feedback-status']
    head = START + timedelta(hours=1, minutes=1)
    assert first[first.index('--status'):] == ['--status', 'accepted', '--unchanged-since',
                                               (head - timedelta(seconds=10)).isoformat(), '--not-after',
                                               (head + timedelta(minutes=15)).isoformat(), '--spec', 'ANYM-10',
                                               '--note', 'one note']


# spec: ANYM-473
def test_a_disposition_changed_during_the_read_or_after_the_plan_is_never_overwritten(tmp_path, capsys):
    prod = Prod({A: dict(disp(), at=None), B: disp('accepted', 'ANYM-10')})
    # B changed 5 s before the head: inside the guard, so it may still be committing; skipped.
    prod.rows[B]['at'] = START + timedelta(hours=1, minutes=1) - timedelta(seconds=5)
    assert apply(tmp_path, prod, {A: disp('declined'), B: disp('declined')}, '--apply') == 0
    assert f'{B} accepted -> no change (changed during read)' in capsys.readouterr().out
    assert [flags(argv)['--id'] for argv in prod.writes] == [[A]]
    # An operator changes A after the plan: the conditional write refuses and the run stops, nothing resent.
    prod.writes.clear()

    def racing(host, argv):
        prod.rows[A] = dict(disp('accepted', 'ANYM-11'), at=prod.tick())
        return prod.run(host, argv)
    assert apply(tmp_path, prod, {A: disp('declined', note='again')}, '--apply', run=racing) == 1
    assert len(prod.writes) == 1 and prod.rows[A]['status'] == 'accepted'
    assert audited(tmp_path)[-1]['outcome'] == 'refused' and not (state_dir(tmp_path) / 'pending.json').exists()


# spec: ANYM-473
@pytest.mark.parametrize('failure, outcome', [(sync.Uncertain('TimeoutExpired'), 'uncertain'),
                                              (sync.Refused('set-feedback-status exited 1'), 'refused')])
def test_every_write_is_audited_and_a_failed_or_unknown_call_stops_the_run(tmp_path, failure, outcome):
    prod = Prod({})
    rows = {A: disp('accepted', 'ANYM-10', note='Private triage note'), B: disp('declined', 'ANYM-12')}
    assert apply(tmp_path, prod, {C: disp('accepted', 'ANYM-11')}, '--apply') == 0
    calls = []

    def failing(host, argv):
        calls.append(argv)
        raise failure
    assert apply(tmp_path, prod, rows, '--apply', run=failing) == 1
    assert len(calls) == 1
    lines = audited(tmp_path)
    assert [line['outcome'] for line in lines] == ['started', 'ok', 'started', outcome]
    assert lines[1]['ids'] == [C] and lines[1]['from'] == {C: 'received'} and lines[1]['to'] == 'accepted'
    audit = state_dir(tmp_path) / 'audit.jsonl'
    assert oct(audit.stat().st_mode & 0o777) == '0o600' and 'Private triage note' not in audit.read_text()
    assert (state_dir(tmp_path) / 'pending.json').exists() == (outcome == 'uncertain')


# spec: ANYM-473
def test_an_uncertain_call_blocks_writes_until_it_lands_or_can_no_longer_land(tmp_path, capsys):
    prod = Prod({})
    rows = {A: disp('accepted', 'ANYM-10'), B: disp('declined', 'ANYM-12')}

    def crash(host, argv):
        raise KeyboardInterrupt  # the process dies before any reply
    with pytest.raises(KeyboardInterrupt):
        apply(tmp_path, prod, rows, '--apply', run=crash)
    pending = state_dir(tmp_path) / 'pending.json'
    assert json.loads(pending.read_text())['ids'] == [A] and oct(pending.stat().st_mode & 0o777) == '0o600'
    # Not landed and not expired: the next run plans but writes nothing, and never resends.
    assert apply(tmp_path, prod, rows, '--apply') == 1
    assert prod.writes == [] and 'PENDING' in capsys.readouterr().err
    # The call lands late: the next run sees it, clears the record and goes on.
    prod.rows[A] = dict(disp('accepted', 'ANYM-10'), at=prod.tick())
    assert apply(tmp_path, prod, rows, '--apply') == 0
    assert not pending.exists() and [flags(argv)['--id'] for argv in prod.writes] == [[B]]
    assert [line['outcome'] for line in audited(tmp_path)][-3:] == ['landed', 'started', 'ok']
    # A call that never lands: blocked until an export head is past its not-after bound plus 10 s.
    prod.writes.clear()

    def timeout(host, argv):
        raise sync.Uncertain('TimeoutExpired')
    assert apply(tmp_path, prod, {C: disp('declined')}, '--apply', run=timeout) == 1
    until = datetime.fromisoformat(json.loads(pending.read_text())['not_after'])
    prod.clock = until - timedelta(minutes=1)
    assert apply(tmp_path, prod, {D: disp('declined')}, '--apply') == 1 and prod.writes == []  # head = until
    prod.clock = until + timedelta(seconds=1)
    with pytest.raises(sync.Refused):  # the original command, delivered late, can no longer commit
        prod.run('deedkit-prod', sync.set_command(IMAGE, disp('declined'), [C], START.isoformat(), until.isoformat()))
    prod.writes.clear()
    assert apply(tmp_path, prod, {D: disp('declined')}, '--apply') == 0  # head = until + 1 min
    assert [flags(argv)['--id'] for argv in prod.writes] == [[D]] and prod.rows.get(C) is None
    assert [line['outcome'] for line in audited(tmp_path)][-4:] == ['uncertain', 'expired', 'started', 'ok']


# spec: ANYM-473
def test_one_run_at_a_time_and_the_exit_codes(tmp_path):
    prod = Prod({})
    state = state_dir(tmp_path)
    state.mkdir(mode=0o700)
    with open(state / 'lock', 'w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert apply(tmp_path, prod, {A: disp('declined')}, '--apply') == 1
    assert prod.calls == [] and prod.writes == []
    assert apply(tmp_path, prod, {A: disp('declined')}, '--apply') == 0
    for argv in (['apply'], ['follow', '--dispositions', 'x.json'], ['merge']):
        with pytest.raises(SystemExit) as usage:
            sync.main(argv + ['--state', str(state)], ssh=prod.ssh, run=prod.run)
        assert usage.value.code == 2


def git(root, *argv):
    return subprocess.run(['git', '-C', str(root), *argv], check=True, capture_output=True, text=True).stdout.strip()


def repo(tmp_path, subjects):
    """A git fixture: one empty commit per subject on origin/main; returns subject -> SHA."""
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-q')
    shas = {}
    for subject in subjects:
        git(root, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '--allow-empty',
            '-m', subject)
        shas[subject] = git(root, 'rev-parse', 'HEAD')
    git(root, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    return root, shas


def pull_requests(prs):
    """A `gh pr list` stub: the merged pull requests the search returns, per card."""
    asked = []

    def gh(argv):
        query = argv[argv.index('--search') + 1]
        ref = query.split('"')[1]
        assert query == f'"{ref}" in:body'
        asked.append(ref)
        return json.dumps([{'number': n, 'body': body, 'mergeCommit': {'oid': sha}} for n, body, sha in prs.get(ref, [])])
    return gh, asked


def spec(n, status):
    return {'seqNum': n, 'title': f'card {n}', 'status': status, 'content': '', 'children': []}


MAIN = {'requirements': [spec(10, 'Done'), spec(11, 'Done'), spec(12, 'Active'), spec(13, 'Done'), spec(14, 'Done'),
                         spec(15, 'Done')],
        'pagination': {'nextPageToken': '', 'totalCount': 6}}


def follow(tmp_path, prod, root, gh, live, main=MAIN):
    def read(*argv):
        assert argv == ('spec', 'list', '--branch', 'main')
        return main
    return sync.main(['follow', '--state', str(state_dir(tmp_path)), '--apply'], ssh=prod.ssh, read=read, live=live,
                     gh=gh, run=prod.run, root=root, fetch=False)


# spec: ANYM-473
def test_follow_moves_each_status_after_its_card(tmp_path):
    root, shas = repo(tmp_path, ['one', 'two', 'three'])
    one, two, three = shas['one'], shas['two'], shas['three']
    gh, asked = pull_requests({
        'ANYM-10': [(1, 'Spec: ANYM-10\nDelivers: ANYM-10', one), (9, 'Spec: ANYM-10, ANYM-100', OLD)],
        'ANYM-11': [(2, 'Spec: ANYM-9\n  Delivers: ANYM-9, ANYM-11', two)],  # indented and second: sync B's grammar
        'ANYM-13': [(3, 'Delivers: ANYM-13', one), (4, 'Delivers: ANYM-13', three)],  # two deliveries: ambiguous
        'ANYM-14': [(5, 'fix(x): mentions ANYM-14 only in the title', three)],
        'ANYM-15': [(6, 'Delivers: ANYM-15', MERGED)],  # not on origin/main
    })
    prod = Prod({A: disp('accepted', 'ANYM-10', note='triage'), B: disp('accepted', 'ANYM-11'),
                 C: disp('received', 'ANYM-12', note='Reporter text kept by triage'), D: disp('accepted', 'ANYM-12'), E: disp('accepted', 'ANYM-20'),
                 F: disp('accepted', 'ANYM-13'), G: disp('verified', 'ANYM-10'), H: disp('declined', 'ANYM-10'),
                 I: disp('verify_requested', 'ANYM-10', fixed='deedkit@' + OLD, note='earlier'),
                 J: disp('fixed', 'ANYM-10', fixed='deedkit@' + MERGED)})
    prod.rows[IDS[10]] = dict(disp('accepted', 'ANYM-14'), at=START)
    prod.rows[IDS[11]] = dict(disp('accepted', 'ANYM-15'), at=START)
    assert follow(tmp_path, prod, root, gh, lambda commit: LIVE if commit == one else None) == 0
    assert sorted(asked) == ['ANYM-10', 'ANYM-11', 'ANYM-13', 'ANYM-14', 'ANYM-15']  # Done cards with movable items
    assert shown(prod, A) == disp('verify_requested', 'ANYM-10', fixed='deedkit@' + LIVE,
                                  note=f'Delivered by ANYM-10; live in deedkit@{LIVE[:12]}. Please re-check.')
    assert shown(prod, B) == disp('fixed', 'ANYM-11', fixed='deedkit@' + two,
                                  note=f'Delivered by ANYM-11 in deedkit@{two[:12]}; not live yet.')
    assert shown(prod, C) == disp('accepted', 'ANYM-12', note='Accepted as ANYM-12; it follows that card.')
    assert prod.rows[J]['status'] == 'verify_requested'
    for unchanged, status in ((D, 'accepted'), (E, 'accepted'), (F, 'accepted'), (G, 'verified'), (H, 'declined'),
                              (IDS[10], 'accepted'), (IDS[11], 'accepted')):
        assert prod.rows[unchanged]['status'] == status
    assert prod.rows[I]['fixed_in'] == 'deedkit@' + OLD and prod.rows[I]['note'] == 'earlier'  # first live SHA kept
    for argv in prod.writes:
        assert 'triage' not in argv  # follow writes its own note, never kept feedback-derived text
    prod.writes.clear()
    assert follow(tmp_path, prod, root, gh, lambda commit: LIVE) == 0
    assert [flags(argv)['--status'] for argv in prod.writes] == ['verify_requested']  # B is live now
    prod.writes.clear()
    assert follow(tmp_path, prod, root, gh, lambda commit: LIVE) == 0 and prod.writes == []


# spec: ANYM-473
@pytest.mark.parametrize('bad', ['truncated', 'export', 'head', 'gh'])
def test_a_failed_or_malformed_read_stops_follow_before_any_write(tmp_path, bad):
    root, shas = repo(tmp_path, ['one'])
    prod = Prod({A: disp('accepted', 'ANYM-10')})
    main = dict(MAIN, pagination={'nextPageToken': 'more', 'totalCount': 9}) if bad == 'truncated' else MAIN
    if bad == 'export':
        prod.rows[A]['status'] = 'nonsense'
    if bad == 'head':
        prod.rows[A]['at'] = START + timedelta(days=1)
        prod.head = START + timedelta(days=2)
        prod.ssh = lambda host, argv, ssh=prod.ssh: ssh(host, argv) if 'export-feedback' not in argv else json.dumps(
            {'untrusted_content': True, 'mode': 'changed', 'export_head': START.isoformat(), 'has_more': False,
             'next_page': None, 'dispositions': [{'id': A, 'status': 'accepted', 'spec_ref': 'ANYM-10',
                                                  'duplicate_of': None, 'fixed_in': None, 'status_note': None,
                                                  'status_changed_at': (START + timedelta(days=1)).isoformat()}]})
    gh, _ = pull_requests({'ANYM-10': [(1, 'Delivers: ANYM-10', shas['one'])] * (sync.PR_LIMIT if bad == 'gh' else 1)})
    assert follow(tmp_path, prod, root, gh, lambda commit: LIVE, main) == 1
    assert prod.writes == []


# spec: ANYM-473
def test_live_follows_the_receipt_watch_verdict(tmp_path):
    root, shas = repo(tmp_path, ['old', 'delivered', 'plan'])
    old, delivered, plan = shas['old'], shas['delivered'], shas['plan']
    snapshot = {'plan_a': {'sha': plan, 'status': 'applied'}, 'http': {'sha': plan, 'status': 'serving'},
                'deploy': None, 'pause': {'present': False, 'reason': None}, 'unresolved': [], 'served': []}
    live = sync.live_probe('deedkit-prod', root, lambda host: snapshot)
    assert live(delivered) == plan and live(plan) == plan
    http_old = dict(snapshot, http={'sha': old, 'status': 'serving'})
    assert sync.live_probe('deedkit-prod', root, lambda host: http_old)(delivered) is None
    paused = dict(snapshot, pause={'present': True, 'reason': 'drill'})
    assert sync.live_probe('deedkit-prod', root, lambda host: paused)(plan) is None
    unfinished = dict(snapshot, unresolved=['attempt-7'])
    assert sync.live_probe('deedkit-prod', root, lambda host: unfinished)(plan) is None
    with pytest.raises(sync.Refused):
        sync.live_probe('deedkit-prod', root, lambda host: {'unstable': True})


# spec: ANYM-473
@pytest.mark.parametrize('pagination', [{'has_more': None, 'next_page': 'x'}, {}, {'has_more': False, 'next_page': '2'},
                                        {'has_more': True, 'next_page': None}],
                         ids=['null-with-token', 'missing', 'false-with-token', 'true-without-token'])
def test_malformed_pagination_refuses_the_inventory_before_any_write(tmp_path, pagination):
    prod = Prod({A: disp('verify_requested', 'ANYM-10', fixed='deedkit@' + LIVE), B: disp('accepted', 'ANYM-11')})
    reads = prod.ssh

    def ssh(host, argv):
        reply = reads(host, argv)
        if 'export-feedback' not in argv:
            return reply
        reply = {k: v for k, v in json.loads(reply).items() if k not in ('has_more', 'next_page')}
        return json.dumps(dict(reply, dispositions=reply['dispositions'][:1], **pagination))
    prod.ssh = ssh
    assert apply(tmp_path, prod, {B: disp('accepted', 'ANYM-11', note='n')}, '--apply') == 1
    assert prod.writes == []


class Completed:
    def __init__(self, returncode, stdout='', stderr=''):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


# spec: ANYM-473
@pytest.mark.parametrize('completed, outcome', [
    (Completed(0, '{"updated": 1}'), 'ok'),
    (Completed(1, stderr='Traceback (most recent call last):\nValueError: changed_since_plan: 1 of 2 IDs\n'), 'refused'),
    (Completed(1, stderr='ValueError: not_after_passed\n'), 'refused'),
    (Completed(2, stderr='usage: x\npython -m messaging_service: error: unrecognized arguments: --unchanged-since T\n'),
     'refused'),
    (Completed(1, stderr='psycopg.OperationalError: server closed the connection unexpectedly\n'), 'uncertain'),
    (Completed(255), 'uncertain'), (Completed(-15), 'uncertain'), (Completed(125), 'uncertain'),
    (Completed(2, stderr='python: error: something else\n'), 'uncertain'),
], ids=['ok', 'changed-since-plan', 'not-after', 'old-release', 'database-error', 'ssh', 'signal', 'docker', 'other-2'])
def test_only_an_explicit_setter_refusal_is_definite(monkeypatch, completed, outcome):
    monkeypatch.setattr(sync.subprocess, 'run', lambda argv, **options: completed)
    if outcome == 'ok':
        assert sync.remote_write('deedkit-prod', ['true']) == completed.stdout
        return
    with pytest.raises(sync.Uncertain if outcome == 'uncertain' else sync.Refused) as failure:
        sync.remote_write('deedkit-prod', ['true'])
    assert 'connection' not in str(failure.value)  # remote output is matched, never shown


# spec: ANYM-473
@pytest.mark.parametrize('reply', ['not json', '{"feedback_ids": []}'], ids=['unreadable', 'unexpected'])
def test_an_unconfirmed_reply_keeps_the_pending_record(tmp_path, reply):
    prod = Prod({})
    assert apply(tmp_path, prod, {A: disp('declined')}, '--apply', run=lambda host, argv: reply) == 1
    assert (state_dir(tmp_path) / 'pending.json').exists() and audited(tmp_path)[-1]['outcome'] == 'uncertain'
    assert apply(tmp_path, prod, {B: disp('declined')}, '--apply') == 1 and prod.writes == []


# spec: ANYM-473
@pytest.mark.parametrize('record', [
    {'ids': [], 'target': disp('declined')},
    {'ids': [A, A], 'target': disp('declined')},
    {'ids': ['not-a-uuid'], 'target': disp('declined')},
    {'ids': [A], 'target': {}},
    {'ids': [A], 'target': disp('verified')},
    {'ids': [A], 'target': disp('declined'), 'not_after': 'soon'},
], ids=['no-ids', 'repeated-id', 'bad-id', 'empty-target', 'agent-verdict', 'bad-bound'])
def test_a_malformed_pending_record_never_unblocks_writes(tmp_path, record):
    prod = Prod({})
    state = state_dir(tmp_path)
    state.mkdir(mode=0o700)
    bound = (START + timedelta(hours=2)).isoformat()
    (state / 'pending.json').write_text(json.dumps(dict({'unchanged_since': START.isoformat(), 'not_after': bound},
                                                        **record)))
    assert apply(tmp_path, prod, {B: disp('declined')}, '--apply') == 1
    assert prod.writes == [] and (state / 'pending.json').exists()


# --- auto, status (ANYM-536) -------------------------------------------------------------------------------------

def triage_dir(tmp_path):
    return tmp_path / 'triage-state'


def publish(tmp_path, name, rows, decided=None, legacy=False):
    """A dispositions file as triage publishes it; `legacy` is the ANYM-473 shape without `decided_at`."""
    directory = triage_dir(tmp_path) / 'dispositions'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps([dict(d, id=i) if legacy else dict(d, id=i, decided_at=decided.isoformat())
                                for i, d in rows.items()]))
    return path


def main_of(*specs):
    return {'requirements': list(specs), 'pagination': {'nextPageToken': '', 'totalCount': len(specs)}}


def carded(n, content, status='Active'):
    return dict(spec(n, status), content=content)


def auto(tmp_path, prod, *extra, main=MAIN, run=None, gh=None):
    def read(*argv):
        assert argv == ('spec', 'list', '--branch', 'main')
        return main
    return sync.main(['auto', '--state', str(state_dir(tmp_path)), '--triage-state', str(triage_dir(tmp_path)), *extra],
                     ssh=prod.ssh, read=read, live=lambda commit: None, gh=gh or (lambda argv: '[]'),
                     run=run or prod.run, root=tmp_path, fetch=False)


def synced(tmp_path, name):
    return json.loads((state_dir(tmp_path) / name).read_text())


def written_ids(prod):
    return [flags(argv)['--id'] for argv in prod.writes]


def triage_state(tmp_path, ids):
    path = triage_dir(tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    sync.triage.snapshot(path, lambda *argv: {'branches': []} if argv[:2] == ('branch', 'list')
                         else main_of(spec(10, 'Active')))
    sync.triage.save(path / 'inbox.json', {i: {'id': i, 'key_id': 'k1', 'principal': 'agent-a',
                                               'created_at': START.isoformat()} for i in ids})
    return path


def decision(i):
    return {'id': i, 'classification': 'reported-bug', 'reason': 'contradicts the card',
            'specs': [{'ref': 'ANYM-10', 'verdict': 'accepted'}], 'primary': 'ANYM-10'}


def crash_on_second_call(prod):
    sent = []

    def run(host, argv):
        sent.append(argv)
        if len(sent) == 2:
            raise KeyboardInterrupt  # the process dies before the second call is answered
        return prod.run(host, argv)
    return run


# spec: ANYM-536
def test_auto_lists_a_file_without_decided_at_as_legacy_and_writes_nothing(tmp_path):
    prod = Prod({A: dict(disp(), at=None)})
    path = publish(tmp_path, '2026-10-26T051125.282458Z.json', {A: disp('declined', note='hand-made')}, legacy=True)
    assert auto(tmp_path, prod, '--apply') == 0
    assert prod.writes == [] and synced(tmp_path, 'last-run.json')['files']['legacy'] == [path.name]
    assert synced(tmp_path, 'applied.json') == {'files': {}}
    # It stays a hand `apply`; `auto --retire` records it complete without a write.
    assert auto(tmp_path, prod, '--retire', str(path)) == 0
    assert auto(tmp_path, prod, '--apply') == 0
    assert prod.writes == [] and synced(tmp_path, 'last-run.json')['files']['legacy'] == []
    assert synced(tmp_path, 'applied.json')['files'][path.name]['retired'] is True


# spec: ANYM-536
def test_a_legacy_decision_republished_after_a_triage_crash_writes_nothing(tmp_path, monkeypatch):
    state = triage_state(tmp_path, [A])
    report = sync.triage.report

    def crash(*argv):
        raise KeyboardInterrupt
    monkeypatch.setattr(sync.triage, 'report', crash)
    with pytest.raises(KeyboardInterrupt):
        sync.triage.record(state, [decision(A)], now=START)  # decided 09:00, crash before publication
    monkeypatch.setattr(sync.triage, 'report', report)
    ledger = json.loads((state / 'ledger.json').read_text())
    del ledger[A]['decided_at']  # an entry written before this card
    sync.triage.save(state / 'ledger.json', ledger)
    prod = Prod({A: disp('declined', note='hand correction')})  # at 10:00
    sync.triage.record(state, [], now=START + timedelta(hours=2))  # published at 11:00
    (published,) = (state / 'dispositions').iterdir()
    assert json.loads(published.read_text())[0]['decided_at'] is None
    assert auto(tmp_path, prod, '--apply') == 0
    assert prod.writes == [] and synced(tmp_path, 'last-run.json')['files']['legacy'] == [published.name]
    assert shown(prod, A) == disp('declined', note='hand correction')


# spec: ANYM-536
def test_a_crash_after_the_first_of_two_batches_writes_only_the_second_next_run(tmp_path):
    prod = Prod({A: dict(disp(), at=None), B: dict(disp(), at=None)})
    path = publish(tmp_path, 'one.json', {A: disp('accepted', 'ANYM-12'), B: disp('declined')}, START)
    with pytest.raises(KeyboardInterrupt):
        auto(tmp_path, prod, '--apply', run=crash_on_second_call(prod))
    assert written_ids(prod) == [[A]]
    assert synced(tmp_path, 'applied.json')['files'][path.name]['rows'][A]['outcome'] == 'written'
    prod.writes.clear()
    assert auto(tmp_path, prod, '--apply') == 1 and prod.writes == []  # the unknown call blocks writes
    prod.clock += timedelta(minutes=20)  # past its not-after bound: it can no longer land
    assert auto(tmp_path, prod, '--apply') == 0
    assert written_ids(prod) == [[B]] and shown(prod, B) == disp('declined')
    assert synced(tmp_path, 'applied.json')['files'][path.name]['complete'] is True
    assert synced(tmp_path, 'last-run.json')['files']['complete'] == [path.name]


# spec: ANYM-536
def test_a_hand_correction_between_runs_is_kept(tmp_path):
    prod = Prod({A: dict(disp(), at=None), B: dict(disp(), at=None)})
    path = publish(tmp_path, 'one.json', {A: disp('accepted', 'ANYM-12'), B: disp('declined')}, START)
    with pytest.raises(KeyboardInterrupt):
        auto(tmp_path, prod, '--apply', run=crash_on_second_call(prod))
    prod.rows[B] = dict(disp('accepted', 'ANYM-11', note='hand correction'), at=prod.tick())
    prod.writes.clear()
    prod.clock += timedelta(minutes=20)
    assert auto(tmp_path, prod, '--apply') == 0
    assert prod.writes == [] and shown(prod, B) == disp('accepted', 'ANYM-11', note='hand correction')
    entry = synced(tmp_path, 'applied.json')['files'][path.name]
    assert entry['rows'][B]['outcome'] == 'superseded' and entry['complete'] is True


# spec: ANYM-536
def test_a_row_republished_after_apply_and_a_hand_correction_writes_nothing(tmp_path):
    state = triage_state(tmp_path, [A])
    sync.triage.record(state, [decision(A)], now=START)
    ledger = json.loads((state / 'ledger.json').read_text())
    ledger[A]['reported'] = False  # triage stopped after publication, before it marked the entry reported
    sync.triage.save(state / 'ledger.json', ledger)
    prod = Prod({A: dict(disp(), at=None)})
    assert auto(tmp_path, prod, '--apply') == 0
    assert shown(prod, A) == disp('accepted', 'ANYM-10', note='contradicts the card')
    prod.rows[A] = dict(disp('declined', 'ANYM-10', note='hand correction'), at=prod.tick())
    sync.triage.record(state, [], now=START + timedelta(hours=2))  # the same row under a newer file name
    first, second = sorted((state / 'dispositions').iterdir())
    assert json.loads(second.read_text())[0]['decided_at'] == START.isoformat()
    prod.writes.clear()
    assert auto(tmp_path, prod, '--apply') == 0
    assert prod.writes == [] and shown(prod, A) == disp('declined', 'ANYM-10', note='hand correction')
    files = synced(tmp_path, 'applied.json')['files']
    assert files[second.name]['rows'][A]['outcome'] == 'superseded' and files[second.name]['complete'] is True


# spec: ANYM-536
def test_a_guard_window_skip_completes_on_the_next_run(tmp_path, capsys):
    prod = Prod({A: disp('received', note='earlier')})
    prod.rows[A]['at'] = START + timedelta(hours=1, minutes=1) - timedelta(seconds=5)  # 5 s before the head
    path = publish(tmp_path, 'one.json', {A: disp('declined')}, START + timedelta(hours=1, minutes=1))
    assert auto(tmp_path, prod, '--apply') == 0
    assert prod.writes == [] and f'{A} received -> no change (changed during read)' in capsys.readouterr().out
    assert synced(tmp_path, 'last-run.json')['files']['open'] == [path.name]
    assert auto(tmp_path, prod, '--apply') == 0
    assert written_ids(prod) == [[A]] and shown(prod, A) == disp('declined')
    assert synced(tmp_path, 'last-run.json')['files']['complete'] == [path.name]


# spec: ANYM-536
def test_an_edited_ledgered_file_is_refused_while_a_second_file_applies(tmp_path, capsys):
    prod = Prod({A: dict(disp(), at=None), B: dict(disp(), at=None)})
    first = publish(tmp_path, 'one.json', {A: disp('declined')}, START)
    assert auto(tmp_path, prod, '--apply') == 0 and written_ids(prod) == [[A]]
    publish(tmp_path, 'one.json', {A: disp('declined', note='edited later')}, START)
    publish(tmp_path, 'two.json', {B: disp('accepted', 'ANYM-12')}, START)
    prod.writes.clear()
    assert auto(tmp_path, prod, '--apply') == 1
    assert written_ids(prod) == [[B]] and shown(prod, A) == disp('declined')
    report = synced(tmp_path, 'last-run.json')
    assert list(report['files']['refused']) == [first.name] and f'REFUSED {first.name}' in capsys.readouterr().err
    assert report['exit'] == 1 and report['files']['complete'] == ['two.json']


# spec: ANYM-536
def test_an_auto_dry_run_records_nothing_and_hand_apply_takes_a_timed_file(tmp_path, capsys):
    prod = Prod({A: dict(disp(), at=None)})
    path = publish(tmp_path, 'one.json', {A: disp('declined')}, START)
    assert auto(tmp_path, prod) == 0
    assert prod.writes == [] and 'dry run' in capsys.readouterr().out
    assert sorted(p.name for p in state_dir(tmp_path).iterdir()) == ['lock']
    assert sync.main(['apply', '--dispositions', str(path), '--state', str(state_dir(tmp_path)), '--apply'],
                     ssh=prod.ssh, run=prod.run) == 0
    assert shown(prod, A) == disp('declined')


LINK = main_of(carded(30, f'Problem: x\nFeedback: {A}, {B}\nHistory: owner 10-26'),
               carded(31, f'History: carded from {C}\n> Feedback: {E}\nFeedback: {D}'),
               carded(32, f'Feedback: {D}'))


# spec: ANYM-536
def test_a_feedback_line_on_exactly_one_card_links_and_other_mentions_do_not(tmp_path):
    prod = Prod({i: disp('received', note='proposed new card agent-platform') for i in (A, B, C, D, E)})
    assert auto(tmp_path, prod, '--apply', main=LINK) == 0
    note = 'Carded as ANYM-30; it follows that card.'
    assert written_ids(prod) == [[A, B]]
    assert shown(prod, A) == shown(prod, B) == disp('accepted', 'ANYM-30', note=note)
    for unlinked in (C, D, E):  # an ID in History or a quote, and an ID on two cards
        assert prod.rows[unlinked]['status'] == 'received'
    report = synced(tmp_path, 'last-run.json')
    assert report['reasons']['several cards'] == [{'feedback': D, 'card': None}]
    assert [row['feedback'] for row in report['waiting_for_card']] == [C, E]
    assert 'proposed new card' not in (state_dir(tmp_path) / 'last-run.json').read_text()  # no note text


# spec: ANYM-536
def test_a_card_seen_then_absent_is_missing_and_a_never_seen_card_waits_for_main(tmp_path):
    prod = Prod({A: disp('accepted', 'ANYM-40'), B: disp('accepted', 'ANYM-41')})
    assert auto(tmp_path, prod, '--apply', main=main_of(spec(40, 'Active'))) == 0
    assert set(synced(tmp_path, 'cards.json')['seen']) == {'ANYM-40'}
    assert auto(tmp_path, prod, '--apply', main=main_of(spec(39, 'Active'))) == 0
    report = synced(tmp_path, 'last-run.json')
    assert report['reasons']['card missing'] == [{'feedback': A, 'card': 'ANYM-40'}]
    assert report['waiting_for_main'] == [{'feedback': B, 'card': 'ANYM-41', 'age_hours': 1.0}]
    assert prod.writes == [] and prod.rows[A]['status'] == prod.rows[B]['status'] == 'accepted'


# spec: ANYM-536
@pytest.mark.parametrize('bad', ['truncated', 'not-json'])
def test_a_failed_tiden_read_with_a_queued_disposition_writes_and_records_nothing(tmp_path, bad):
    prod = Prod({A: dict(disp(), at=None)})
    publish(tmp_path, 'one.json', {A: disp('declined')}, START)
    if bad == 'truncated':
        main = {'requirements': [spec(10, 'Done')], 'pagination': {'nextPageToken': 'more', 'totalCount': 2}}
        assert auto(tmp_path, prod, '--apply', main=main) == 1
    else:
        def read(*argv):
            raise sync.Refused('tiden spec list: not JSON')
        assert sync.main(['auto', '--apply', '--state', str(state_dir(tmp_path)), '--triage-state',
                          str(triage_dir(tmp_path))], ssh=prod.ssh, read=read, run=prod.run) == 1
    assert prod.writes == [] and not (state_dir(tmp_path) / 'applied.json').exists()
    assert not (state_dir(tmp_path) / 'cards.json').exists()
    report = synced(tmp_path, 'last-run.json')
    assert report['exit'] == 1 and report['error'] and report['last_ok'] is None


# spec: ANYM-536
def test_status_exits_1_when_the_last_good_run_is_stale_or_a_call_blocks_writes(tmp_path, capsys):
    prod = Prod({})
    state = str(state_dir(tmp_path))
    assert sync.main(['status', '--state', state]) == 1  # no run yet
    assert auto(tmp_path, prod, '--apply') == 0
    assert sync.main(['status', '--state', state]) == 0
    report = synced(tmp_path, 'last-run.json')
    later = datetime.fromisoformat(report['last_ok']) + timedelta(hours=3, minutes=1)
    capsys.readouterr()
    assert sync.status_report(state_dir(tmp_path), now=later) == 1
    assert 'ATTENTION no exit-0' in capsys.readouterr().err
    # A failed run keeps the last good time; a call with an unknown outcome blocks writes.
    publish(tmp_path, 'one.json', {A: disp('declined')}, START)

    def timeout(host, argv):
        raise sync.Uncertain('TimeoutExpired')
    assert auto(tmp_path, prod, '--apply', run=timeout) == 1
    blocked = synced(tmp_path, 'last-run.json')
    assert blocked['last_ok'] == report['last_ok'] and blocked['pending']['ids'] == [A]
    assert sync.main(['status', '--state', state]) == 1
    assert 'ATTENTION a call with an unknown outcome blocks writes' in capsys.readouterr().err
    assert oct((state_dir(tmp_path) / 'last-run.json').stat().st_mode & 0o777) == '0o600'
