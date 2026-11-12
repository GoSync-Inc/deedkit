"""The feedback task dispatcher on the Ryzen host: candidates, caps, the start journal, the kill switch (ANYM-991)."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from hypothesis import given, settings, strategies as st
import pytest

import scripts.check_doc_commands as doc_commands
import scripts.feedback_bridge as fb
import scripts.feedback_dispatch as fd

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc).timestamp()
PROJECT = 'proj_nfixav7q2f'
ORCH = 'thr_orch0001'
REPO = Path(__file__).resolve().parents[2]
BRIEF = 'The search tool returns stale chats after a sync, so an agent reads old messages.'
CANARY = 'CANARY-BRIEF-TEXT'
SECRET_SHAPE = re.compile(r'whsec_|Bearer|ghp_|-----BEGIN|[A-Za-z0-9+/]{40,}={0,2}')
ZERO = {'started': 0, 'relaunched': 0, 'reconciled': 0, 'attached': 0, 'commented': 0}


def fid(n):
    return '44444444-4444-4444-8444-%012d' % n


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace('+00:00', 'Z')


def raw_row(n, principal='agent-a', dk_status='received', classification='reported-bug', brief=BRIEF, primary=None,
            new_card=None):
    shown = None if brief is None else {'classification': classification, 'reason_code': 'clear_report',
        'primary_spec': primary or 'ANYM-%d' % (100 + n), 'new_card': new_card, 'released_brief': brief}
    return {'feedback_id': fid(n), 'principal': principal, 'category': 'bug', 'operation': 'messages_search',
        'created_at': iso(NOW - 60), 'status': dk_status, 'fixed_in': None, 'spec_ref': None, 'duplicate_of': None,
        'status_changed_at': None if dk_status == 'received' else iso(NOW - 30), 'projection': shown}


class Crash(BaseException):
    """A process death at one chosen point."""


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class FakeTasks:
    """The tracker calls of `fd.Tasks` over memory, with failure injection; every call is logged."""

    def __init__(self):
        self.tasks, self.comments, self.log, self.attached, self.attach_calls = {}, [], [], [], 0
        self.fail_list = self.fail_attach = None

    def add(self, n, status='todo', labels=('feedback',), description=None, threads=(), key=None, feedback=None, edit=None,
            **row):
        text = fb.render(raw_row(feedback or n, **row))[1] if description is None else description
        key = key or 'DK-%d' % n
        self.tasks[key] = {'id': 'T%03d' % n, 'key': key, 'number': n, 'title': 't', 'status': status,
            'labels': list(labels), 'description': edit(text) if edit else text,
            'taskThreads': [{'threadId': t, 'liveStatus': 'working'} for t in threads]}

    def candidates(self):
        self.log.append(('candidates',))
        if self.fail_list is not None:
            raise self.fail_list
        return [{k: v for k, v in t.items() if k != 'taskThreads'} for t in self.tasks.values()
            if t['status'] in ('backlog', 'todo', 'in_progress') and 'feedback' in t['labels']]

    def show(self, ref):
        self.log.append(('show', ref))
        return dict(self.tasks[ref])

    def comment(self, ref, body):
        self.log.append(('comment', ref))
        self.comments.append((ref, body))

    def attach(self, ref, thread):
        self.log.append(('attach', ref, thread))
        self.attach_calls += 1
        if self.fail_attach == 'not_applied':
            raise fb.Uncertain('tasks_timeout')
        self.attached.append((ref, thread))
        self.tasks[ref]['taskThreads'].append({'threadId': thread, 'liveStatus': 'working'})
        if self.fail_attach == 'applied':
            raise fb.Uncertain('tasks_timeout')


class FakeThreads:
    """`fd.Threads` over memory: a spawn makes a thread row; failure modes stop before or after it."""

    def __init__(self, clock):
        self.clock, self.threads, self.calls, self.mode, self.seq, self.on_spawn = clock, [], [], None, 0, None

    def spawns(self):
        return [c for c in self.calls if c[0] == 'spawn']

    def spawn(self, title, prompt, orchestrator, machine=None):
        self.calls.append(('spawn', title, prompt, orchestrator, machine))
        if self.on_spawn is not None:
            self.on_spawn()
        mode, self.mode = self.mode, None
        if mode == 'crash_before':
            raise Crash
        if mode == 'uncertain_not_created':
            raise fb.Uncertain('spawn_timeout')
        if mode == 'refused':
            raise fb.Refused('invalid_value')
        self.seq += 1
        row = {'id': 'thr_w%05d' % self.seq, 'projectId': PROJECT, 'title': title, 'status': 'active', 'archivedAt': None,
            'deletedAt': None, 'createdAt': int(self.clock() * 1000)}
        self.threads.append(row)
        if mode == 'crash_after':
            raise Crash
        if mode == 'uncertain_created':
            raise fb.Uncertain('spawn_timeout')
        return row['id']

    def rows(self, ids=None):
        self.calls.append(('rows', tuple(ids) if ids else None))
        return [dict(r) for r in self.threads if ids is None or r['id'] in ids]

    def archive(self, thread_id):
        next(r for r in self.threads if r['id'] == thread_id)['archivedAt'] = int(self.clock() * 1000)


def write_config(tmp_path, mode=0o600, present=True, **over):
    briefs = tmp_path / 'briefs'
    briefs.mkdir(exist_ok=True)
    for name in ('common.md', 'bb-overlay.md', 'feedback-task.md'):
        (briefs / name).write_text('x')
    path = tmp_path / 'feedback-dispatch.json'
    value = {'dispatch': True, 'orchestrator_thread': ORCH, 'principals': ['agent-a']} | over
    if present:
        path.write_text(json.dumps({k: v for k, v in value.items() if v is not ...}))
        path.chmod(mode)
    return {'DEEDKIT_FEEDBACK_DISPATCH_CONFIG': str(path), 'DEEDKIT_FEEDBACK_DISPATCH_STATE_DIR': str(tmp_path / 'state'),
        'DEEDKIT_FEEDBACK_DISPATCH_BRIEFS': str(briefs)}


class Rig:
    """One dispatcher over fake tasks and threads, built from a real config file."""

    def __init__(self, tmp_path, **over):
        self.tmp_path, self.clock, self.tasks = tmp_path, Clock(), FakeTasks()
        self.threads = FakeThreads(self.clock)
        self.env = write_config(tmp_path, **over)
        self.cfg = fd.load_config(None, self.env)
        self.d = fd.Dispatcher(self.cfg, self.tasks, self.threads, now=self.clock)
        self.state = self.cfg.state_dir

    def run(self):
        return self.d.run()

    def restart(self):
        self.d = fd.Dispatcher(self.cfg, self.tasks, self.threads, now=self.clock)
        return self.d

    def main(self, *argv):
        return fd.main(list(argv), environ=self.env, tasks=self.tasks, threads=self.threads, now=self.clock)

    def rows(self):
        return [(r['task_key'], r['attempt'], r['state']) for r in self.d.journal.rows()]

    def calls(self):
        return len(self.tasks.log) + len(self.threads.calls)


# spec: ANYM-991
def test_a_permitted_released_task_starts_one_worker_that_is_attached_and_never_started_again(tmp_path):
    rig = Rig(tmp_path)
    rig.tasks.add(1)
    seen = []
    rig.threads.on_spawn = lambda: seen.append([(r['task_key'], r['state']) for r in fd.Journal(rig.state).rows()])
    assert rig.run() == ZERO | {'started': 1, 'attached': 1}
    assert seen == [[('DK-1', 'starting')]]  # the journal row is committed before the spawn
    _, title, prompt, orchestrator, machine = rig.threads.spawns()[0]
    briefs = rig.cfg.briefs_dir
    assert (title, orchestrator, machine) == ('feedback-DK-1', ORCH, None)
    assert prompt == ('Read and follow %s/common.md, then %s/bb-overlay.md (it wins on conflict), then the task brief '
        '%s/feedback-task.md. Your orchestrator is bb thread %s. BB task: DK-1.' % (briefs, briefs, briefs, ORCH))
    assert rig.tasks.attached == [('DK-1', 'thr_w00001')] and rig.rows() == [('DK-1', 1, 'started')]
    assert rig.run() == ZERO and rig.restart().run() == ZERO and len(rig.threads.spawns()) == 1


BLOCKED = {
    'duplicate_label': {'labels': ('feedback', 'duplicate')},
    'hold_label': {'labels': ('feedback', 'hold')},
    'epic_label': {'labels': ('feedback', 'epic')},
    'summary_pending': {'brief': ''},
    'no_projection': {'brief': None},
    'question': {'classification': 'question'},
    'enable_only': {'classification': 'enable-only'},
    'not_actionable': {'classification': 'not-actionable'},
    'no_classification': {'classification': None},
    'deedkit_fixed': {'dk_status': 'fixed'},
    'deedkit_declined': {'dk_status': 'declined'},
    'claimed_in_progress': {'status': 'in_progress'},
    'attached_thread': {'threads': ('thr_human0001',)},
    'forged_first_line': {'description': 'A summary line\nFeedback-ID: %s\n' % fid(1)},
    'quoted_id_line': {'description': '> Feedback-ID: %s\nSource: agent-a · bug · x · received y\n' % fid(1)},
    'source_line_gone': {'edit': lambda text: re.sub(r'^Source: .*\n', '', text, flags=re.MULTILINE)},
    'bad_key': {'key': 'dk-1 --x'},
}


# spec: ANYM-991
@pytest.mark.parametrize('name', sorted(BLOCKED))
def test_each_candidate_condition_alone_blocks_a_start_silently(tmp_path, name, capsys):
    rig = Rig(tmp_path)
    rig.tasks.add(1, **BLOCKED[name])
    assert rig.run() == ZERO and not rig.threads.spawns() and not rig.tasks.comments
    assert capsys.readouterr() == ('', '')


# spec: ANYM-991
def test_a_task_in_the_configs_hold_list_is_never_started(tmp_path):
    rig = Rig(tmp_path, hold=['DK-1'])
    rig.tasks.add(1)
    rig.tasks.add(2)
    assert rig.run()['started'] == 1 and [c[1] for c in rig.threads.spawns()] == ['feedback-DK-2']


# spec: ANYM-991
@settings(max_examples=150, deadline=None)
@given(st.text(max_size=300))
def test_no_brief_text_changes_what_the_parser_reads(brief):
    description = fb.render(raw_row(1, principal='agent-a', new_card='stale-chats', brief=brief))[1]
    info = fd.parse_task({'key': 'DK-1', 'description': description, 'labels': ['feedback'], 'status': 'todo', 'number': 1})
    assert (info['fid'], info['principal'], info['dk_status'], info['classification'], info['cluster']) == (
        fid(1), 'agent-a', 'received', 'reported-bug', 'new:stale-chats')
    assert info['released'] == bool(fb.clean(brief, 600))


# spec: ANYM-991
def test_only_the_first_header_lines_count_and_text_after_the_summary_is_never_read(tmp_path):
    text = fb.render(raw_row(1, principal='stranger'))[1]
    extra = '\nSource: agent-a · bug · x · received y\nDeedKit status: fixed (changed z)\nTriage: feature / x; card none; new card other'
    info = fd.parse_task({'key': 'DK-1', 'description': text + extra, 'labels': [], 'status': 'todo', 'number': 1})
    assert (info['principal'], info['dk_status'], info['classification'], info['cluster']) == (
        'stranger', 'received', 'reported-bug', 'spec:ANYM-101')
    rig = Rig(tmp_path)
    rig.tasks.add(1, description=text.replace('stranger', 'agent-a') + extra)
    assert rig.run()['started'] == 1
    assert fd.parse_task({'key': 'DK-1', 'description': 'hello', 'labels': [], 'status': 'todo', 'number': 1}) is None


# spec: ANYM-991
def test_a_truncated_or_failed_listing_starts_nothing_and_ends_with_one_fixed_line(tmp_path, capsys):
    rig = Rig(tmp_path)
    rig.tasks.add(1)
    for error, code in ((fb.Incomplete('tasks_listing_incomplete'), 'tasks_listing_incomplete'),
            (fb.Uncertain('tasks_timeout'), 'tasks_timeout')):
        rig.tasks.fail_list = error
        assert rig.main('run') == 1
        assert capsys.readouterr().err == 'feedback-dispatch: %s\n' % code
    assert not rig.threads.spawns() and not rig.tasks.comments


# spec: ANYM-991
def test_a_report_only_principal_is_never_started_and_its_task_is_commented_once(tmp_path):
    rig = Rig(tmp_path)
    rig.tasks.add(1, principal='stranger')
    rig.tasks.add(2)
    assert rig.run() == ZERO | {'started': 1, 'attached': 1, 'commented': 1}
    assert rig.run()['commented'] == 0 and rig.restart().run()['commented'] == 0
    assert [ref for ref, _ in rig.tasks.comments] == ['DK-1'] and len(rig.threads.spawns()) == 1
    body = rig.tasks.comments[0][1]
    assert BRIEF not in body and fid(1) not in body and 'stranger' not in body and 'report-only' in body


# spec: ANYM-991
def test_at_most_two_workers_are_live_oldest_task_first_and_a_finished_task_frees_a_slot(tmp_path):
    rig = Rig(tmp_path)
    for n in (3, 1, 2):
        rig.tasks.add(n)
    assert rig.run()['started'] == 2 and [c[1] for c in rig.threads.spawns()] == ['feedback-DK-1', 'feedback-DK-2']
    assert rig.run()['started'] == 0
    rig.tasks.tasks['DK-1']['status'] = 'in_review'
    assert rig.run()['started'] == 1 and rig.threads.spawns()[-1][1] == 'feedback-DK-3'


# spec: ANYM-991
def test_one_start_per_cluster_and_per_feedback_id_ever(tmp_path):
    rig = Rig(tmp_path)
    rig.tasks.add(1, new_card='stale-chats')
    rig.tasks.add(2, new_card='stale-chats')  # the same cluster, another feedback item
    rig.tasks.add(3, feedback=1, new_card='other-slug')  # a second task of feedback item 1
    rig.tasks.add(4, new_card='other-slug')
    assert rig.run()['started'] == 2 and [c[1] for c in rig.threads.spawns()] == ['feedback-DK-1', 'feedback-DK-4']
    for key in ('DK-1', 'DK-4'):
        rig.tasks.tasks[key]['status'] = 'in_review'
    assert rig.run() == ZERO and rig.restart().run() == ZERO and len(rig.threads.spawns()) == 2


# spec: ANYM-991
def test_tasks_of_one_primary_card_without_a_new_card_slug_share_a_cluster(tmp_path):
    rig = Rig(tmp_path)
    rig.tasks.add(1, primary='ANYM-12')
    rig.tasks.add(2, primary='ANYM-12')
    rig.tasks.add(3, primary='ANYM-13')
    assert rig.run()['started'] == 2 and [c[1] for c in rig.threads.spawns()] == ['feedback-DK-1', 'feedback-DK-3']


# spec: ANYM-991
def test_the_daily_start_cap_counts_utc_days(tmp_path):
    rig = Rig(tmp_path, daily_starts=1)
    rig.tasks.add(1)
    rig.tasks.add(2)
    assert rig.run()['started'] == 1
    rig.tasks.tasks['DK-1']['status'] = 'in_review'
    assert rig.run()['started'] == 0
    rig.clock.t += 86400
    assert rig.run()['started'] == 1 and rig.threads.spawns()[-1][1] == 'feedback-DK-2'


# spec: ANYM-991
def test_a_lost_worker_gets_one_relaunch_and_never_a_third_start(tmp_path):
    rig = Rig(tmp_path)
    rig.tasks.add(1)
    rig.run()
    rig.threads.archive('thr_w00001')
    rig.tasks.tasks['DK-1']['status'] = 'in_progress'
    assert rig.run() == ZERO | {'relaunched': 1, 'attached': 1}
    assert rig.rows() == [('DK-1', 1, 'lost'), ('DK-1', 2, 'started')]
    rig.threads.archive('thr_w00002')
    assert rig.run() == ZERO and rig.restart().run() == ZERO
    assert rig.rows() == [('DK-1', 1, 'lost'), ('DK-1', 2, 'lost')] and len(rig.threads.spawns()) == 2


# spec: ANYM-991
def test_a_relaunch_waits_while_a_thread_that_is_not_the_lost_workers_own_is_attached(tmp_path):
    rig = Rig(tmp_path)
    rig.tasks.add(1)
    rig.run()
    rig.threads.archive('thr_w00001')
    rig.tasks.tasks['DK-1']['status'] = 'in_progress'
    rig.tasks.tasks['DK-1']['taskThreads'].append({'threadId': 'thr_human0001', 'liveStatus': 'working'})
    assert rig.run() == ZERO and len(rig.threads.spawns()) == 1


# spec: ANYM-991
@pytest.mark.parametrize('task_status, state', [('in_review', 'finished'), ('done', 'finished'), ('canceled', 'finished'),
    ('in_progress', 'lost')])
def test_an_archived_worker_thread_finishes_or_loses_its_row_by_the_task_status(tmp_path, task_status, state):
    rig = Rig(tmp_path)
    rig.tasks.add(1)
    rig.run()
    rig.threads.archive('thr_w00001')
    rig.tasks.tasks['DK-1']['status'] = task_status
    rig.run()
    assert rig.rows()[0] == ('DK-1', 1, state)


# spec: ANYM-991
@pytest.mark.parametrize('mode', ['crash_before', 'crash_after', 'uncertain_created', 'uncertain_not_created'])
def test_a_crash_or_a_lost_reply_around_the_spawn_never_spawns_twice_for_one_row(tmp_path, mode):
    rig = Rig(tmp_path)
    rig.tasks.add(1)
    rig.threads.mode = mode
    with pytest.raises((Crash, fb.Uncertain)):
        rig.run()
    assert rig.rows() == [('DK-1', 1, 'starting')]
    rig.restart()
    if mode in ('crash_after', 'uncertain_created'):  # the thread exists: the listing finds it by title and time
        assert rig.run() == ZERO | {'reconciled': 1, 'attached': 1}
        assert rig.rows() == [('DK-1', 1, 'started')] and rig.tasks.attached == [('DK-1', 'thr_w00001')]
        assert len(rig.threads.spawns()) == 1
    else:  # no thread yet: the row waits out its grace and nothing is spawned for it meanwhile
        assert rig.run() == ZERO and rig.rows() == [('DK-1', 1, 'starting')] and len(rig.threads.spawns()) == 1
        rig.clock.t += 301
        assert rig.run() == ZERO | {'relaunched': 1, 'attached': 1}
        assert rig.rows() == [('DK-1', 1, 'lost'), ('DK-1', 2, 'started')] and len(rig.threads.spawns()) == 2


# spec: ANYM-991
def test_a_second_attempts_listing_never_claims_the_first_attempts_older_thread(tmp_path):
    rig = Rig(tmp_path)
    rig.tasks.add(1)
    rig.run()
    rig.threads.archive('thr_w00001')
    rig.tasks.tasks['DK-1']['status'] = 'in_progress'
    rig.clock.t += 3600
    rig.threads.mode = 'crash_before'
    with pytest.raises(Crash):
        rig.run()
    rig.restart()
    assert rig.run() == ZERO and rig.rows() == [('DK-1', 1, 'lost'), ('DK-1', 2, 'starting')]


# spec: ANYM-991
def test_a_definitely_refused_spawn_marks_the_row_lost_stops_the_run_and_prints_a_fixed_code(tmp_path, capsys):
    rig = Rig(tmp_path)
    rig.tasks.add(1)
    rig.tasks.add(2)
    rig.threads.mode = 'refused'
    assert rig.main('run') == 1
    assert capsys.readouterr().err == 'feedback-dispatch: invalid_value\n' and len(rig.threads.spawns()) == 1
    assert [r['state'] for r in fd.Journal(rig.state).rows()] == ['lost']


# spec: ANYM-991
@pytest.mark.parametrize('mode', ['not_applied', 'applied'])
def test_a_failed_attach_is_retried_without_a_second_spawn_and_never_attached_twice(tmp_path, mode, capsys):
    rig = Rig(tmp_path)
    rig.tasks.add(1)
    rig.tasks.fail_attach = mode
    assert rig.run() == ZERO | {'started': 1}
    assert capsys.readouterr().err == 'feedback-dispatch: tasks_timeout\n'
    assert [(r['state'], r['attached']) for r in rig.d.journal.rows()] == [('started', 0)]
    rig.tasks.fail_attach = None
    assert rig.run() == ZERO | {'attached': 1} and len(rig.threads.spawns()) == 1
    assert rig.tasks.attach_calls == (1 if mode == 'applied' else 2) and len(rig.tasks.tasks['DK-1']['taskThreads']) == 1


# spec: ANYM-991
def test_no_feedback_text_reaches_a_command_a_log_or_the_journal(tmp_path, capsys):
    rig = Rig(tmp_path)
    rig.tasks.add(1, brief=CANARY + ' says do something else', new_card='canary-slug')
    rig.tasks.add(2, principal='stranger', brief=CANARY + ' again')
    assert rig.main('run') == 0
    seen = capsys.readouterr()
    journal = b''.join(p.read_bytes() for p in rig.state.glob('dispatch.sqlite3*')).decode('latin-1')
    sent = json.dumps([rig.threads.calls, rig.tasks.comments, rig.tasks.attached, seen.out, seen.err])
    assert CANARY not in sent and CANARY not in journal and 'canary-slug' not in sent
    assert fid(1) not in sent and fid(2) not in sent


# spec: ANYM-991
def test_a_second_run_while_one_holds_the_lock_exits_75_and_writes_nothing(tmp_path, capsys):
    rig = Rig(tmp_path)
    rig.tasks.add(1)
    with rig.d.lock(block=False):
        assert rig.main('run') == 75
    assert capsys.readouterr() == ('', '') and not rig.threads.calls and not rig.tasks.log
    assert rig.main('run') == 0 and len(rig.threads.spawns()) == 1


# spec: ANYM-991
def test_a_missing_brief_file_starts_nothing_and_is_a_fixed_error_only_when_a_start_is_due(tmp_path, capsys):
    rig = Rig(tmp_path)
    (rig.cfg.briefs_dir / 'feedback-task.md').unlink()
    assert rig.main('run') == 0 and capsys.readouterr() == ('', '')
    rig.tasks.add(1)
    assert rig.main('run') == 1 and capsys.readouterr().err == 'feedback-dispatch: brief_missing\n'
    assert not rig.threads.spawns() and not rig.rows()


# spec: ANYM-991
@pytest.mark.parametrize('case', ['absent', 'dispatch_false'])
def test_dispatch_is_off_by_default_and_a_run_then_makes_no_call_and_prints_nothing(tmp_path, capsys, case):
    rig = Rig(tmp_path, **({'present': False} if case == 'absent' else {'dispatch': False}))
    rig.tasks.add(1)
    assert rig.main('run') == 0 and capsys.readouterr() == ('', '')
    assert not rig.tasks.log and not rig.threads.calls and not rig.state.exists()


# spec: ANYM-991
def test_pause_stops_every_action_including_reconcile_and_attach(tmp_path):
    rig = Rig(tmp_path)
    rig.tasks.add(1)
    rig.tasks.fail_attach = 'not_applied'
    rig.run()
    rig.tasks.fail_attach = None
    rig.tasks.add(2)
    (rig.state / 'PAUSE').touch()
    before = rig.calls()
    assert rig.run() == ZERO and rig.main('run') == 0 and rig.calls() == before and len(rig.threads.spawns()) == 1
    (rig.state / 'PAUSE').unlink()
    rig.run()
    assert ('DK-1', 'thr_w00001') in rig.tasks.attached and len(rig.threads.spawns()) == 2


# spec: ANYM-991
def test_gate_exits_1_on_pause_hold_or_a_row_that_is_not_live_and_0_for_a_live_one_with_no_bb_call(tmp_path, capsys):
    rig = Rig(tmp_path)
    rig.tasks.add(1)
    assert rig.main('gate', 'DK-1') == 1  # no row yet
    rig.run()
    before = rig.calls()
    assert rig.main('gate', 'DK-1') == 0 and rig.main('gate', 'DK-9') == 1
    (rig.state / 'PAUSE').touch()
    assert rig.main('gate', 'DK-1') == 1
    (rig.state / 'PAUSE').unlink()
    assert rig.main('gate', 'DK-1') == 0
    write_config(tmp_path, hold=['DK-1'])  # the same config file, now holding the key
    assert rig.main('gate', 'DK-1') == 1
    write_config(tmp_path)
    assert rig.main('gate', 'DK-1') == 0 and rig.calls() == before
    rig.threads.archive('thr_w00001')
    rig.tasks.tasks['DK-1']['status'] = 'in_review'
    rig.run()
    assert rig.main('gate', 'DK-1') == 1
    assert all(re.fullmatch(r'go|stop [a-z_]+', line) for line in capsys.readouterr().out.splitlines())


CONFIG_CASES = {
    'loose_mode': {'mode': 0o644},
    'unknown_key': {'surprise': True},
    'dispatch_without_principals': {'principals': []},
    'dispatch_without_orchestrator': {'orchestrator_thread': ...},
    'bad_orchestrator': {'orchestrator_thread': 'orchestrator; rm'},
    'max_live_above_two': {'max_live': 3},
    'bad_hold_entry': {'hold': ['DK-1', 'x y']},
    'bad_daily': {'daily_starts': -1},
    'dispatch_not_bool': {'dispatch': 'yes'},
}


# spec: ANYM-991
@pytest.mark.parametrize('case', sorted(CONFIG_CASES))
def test_a_bad_or_loose_config_is_refused_with_a_fixed_code_and_starts_nothing(tmp_path, capsys, case):
    env = write_config(tmp_path, **CONFIG_CASES[case])
    tasks, threads = FakeTasks(), FakeThreads(Clock())
    tasks.add(1)
    assert fd.main(['run'], environ=env, tasks=tasks, threads=threads, now=Clock()) == 1
    err = capsys.readouterr().err
    assert re.fullmatch(r'feedback-dispatch: [a-z_]+\n', err) and str(tmp_path) not in err
    assert not tasks.log and not threads.calls


# spec: ANYM-991
def test_a_symlinked_config_is_refused_and_a_valid_one_loads_its_defaults(tmp_path):
    env = write_config(tmp_path)
    path = Path(env['DEEDKIT_FEEDBACK_DISPATCH_CONFIG'])
    cfg = fd.load_config(None, env)
    assert (cfg.dispatch, cfg.max_live, cfg.daily_starts, cfg.tracker, cfg.hold, cfg.worker_machine) == (True, 2, 6, 'DK', [], None)
    real = tmp_path / 'real.json'
    path.rename(real)
    path.symlink_to(real)
    with pytest.raises(fb.ConfigError):
        fd.load_config(None, env)
    path.unlink()
    assert fd.load_config(None, env).dispatch is False  # an absent file is all defaults, and dispatch is off


# spec: ANYM-991
def test_a_quiet_run_prints_nothing_and_an_acting_run_prints_one_count_line(tmp_path, capsys):
    rig = Rig(tmp_path)
    assert rig.main('run') == 0 and capsys.readouterr() == ('', '')
    rig.tasks.add(1)
    rig.tasks.add(2, principal='stranger')
    assert rig.main('run') == 0
    assert capsys.readouterr().out == 'feedback-dispatch: started=1 relaunched=0 reconciled=0 attached=1 commented=1\n'
    assert rig.main('run') == 0 and capsys.readouterr() == ('', '')


# spec: ANYM-991
def test_status_prints_the_journal_and_check_is_read_only(tmp_path, capsys):
    rig = Rig(tmp_path)
    rig.tasks.add(1, brief=CANARY)
    rig.run()
    capsys.readouterr()
    assert rig.main('status') == 0
    assert capsys.readouterr().out.splitlines() == ['DK-1 attempt=1 state=started thread=thr_w00001']
    rig.tasks.projects = lambda: [{'prefix': 'DK'}]
    before = rig.calls()
    assert rig.main('check') == 0
    assert capsys.readouterr().out.splitlines() == ['feedback-dispatch: config ok (dispatch on)',
        'feedback-dispatch: tracker DK ok', 'feedback-dispatch: briefs ok']
    assert rig.calls() == before and rig.main('bogus') == 64


def runner_for(script):
    calls = []

    def run(argv, timeout, stdin=None):
        calls.append((argv, stdin))
        return script(argv)
    return run, calls


# spec: ANYM-991
def test_the_spawn_command_is_exactly_the_documented_one_with_the_prompt_on_stdin():
    run, calls = runner_for(lambda argv: (0, json.dumps({'id': 'thr_abc12345', 'status': 'active'})))
    threads = fd.Threads(PROJECT, runner=run)
    assert threads.spawn('feedback-DK-7', 'PROMPT TEXT', ORCH) == 'thr_abc12345'
    assert calls[0] == (['bb', 'thread', 'spawn', '--project', PROJECT, '--parent-thread', ORCH, '--new-environment',
        'worktree', '--base-branch', 'main', '--provider', 'claude-code', '--model', 'claude-sonnet-5-5',
        '--reasoning-level', 'xhigh', '--title', 'feedback-DK-7', '--prompt-file', '-', '--json'], 'PROMPT TEXT')
    threads.spawn('feedback-DK-7', 'P', ORCH, 'ryzen-2')
    assert calls[1][0][-3:] == ['--machine', 'ryzen-2', '--json']


# spec: ANYM-991
def test_the_threads_wrapper_lists_by_project_or_by_id_and_tells_refusals_from_uncertain_outcomes():
    run, calls = runner_for(lambda argv: (0, json.dumps([{'id': 'thr_a'}])))
    threads = fd.Threads(PROJECT, runner=run)
    assert threads.rows() == [{'id': 'thr_a'}]
    assert calls[0][0] == ['bb', 'thread', 'list', '--project', PROJECT, '--include-archived', '--json']
    threads.rows(['thr_a', 'thr_b'])
    assert calls[1][0] == ['bb', 'thread', 'list', '--include-archived', '--thread', 'thr_a', '--thread', 'thr_b', '--json']
    many = ['thr_%05d' % n for n in range(120)]
    threads.rows(many)
    assert [c[0].count('--thread') for c in calls[2:]] == [50, 50, 20]

    def timeout(argv):
        raise subprocess.TimeoutExpired(argv, 1)
    refused = (1, json.dumps({'ok': False, 'error': {'code': 'invalid_value', 'message': 'x'}}))
    for script, expected in ((timeout, fb.Uncertain), (lambda a: (1, 'lost'), fb.Uncertain), (lambda a: (0, 'no json'), fb.Uncertain),
            (lambda a: (0, json.dumps({'status': 'active'})), fb.Uncertain), (lambda a: refused, fb.Refused)):
        with pytest.raises(expected):
            fd.Threads(PROJECT, runner=runner_for(script)[0]).spawn('t', 'p', ORCH)
    for script in (lambda a: (0, json.dumps({'ok': True})), lambda a: (0, 'x')):
        with pytest.raises(fb.Uncertain):
            fd.Threads(PROJECT, runner=runner_for(script)[0]).rows()


# spec: ANYM-991
def test_the_tasks_wrapper_lists_open_feedback_tasks_whole_reads_threads_and_attaches(tmp_path):
    pages = {None: {'tasks': [{'id': 'A', 'key': 'DK-1', 'number': 1, 'labels': ['feedback']}], 'nextCursor': 'c1'},
        'c1': {'tasks': [{'id': 'B', 'key': 'DK-2', 'number': 2, 'labels': ['feedback']}], 'nextCursor': None}}

    def script(argv):
        if argv[2] == 'list':
            return 0, json.dumps(pages[argv[argv.index('--cursor') + 1] if '--cursor' in argv else None])
        if argv[2] == 'show':
            return 0, json.dumps({'task': {'id': 'A', 'key': 'DK-1', 'status': 'todo'}, 'labels': [{'name': 'feedback'}],
                'taskThreads': [{'threadId': 'thr_a', 'liveStatus': 'working'}]})
        return 0, json.dumps({'ok': True})
    run, calls = runner_for(script)
    tasks = fd.Tasks('DK', runner=lambda argv, timeout: run(argv, timeout), state_dir=tmp_path)
    assert [t['key'] for t in tasks.candidates()] == ['DK-1', 'DK-2']
    argv = calls[0][0]
    assert argv[:5] == ['bb', 'tasks', 'list', '--project', 'DK'] and argv[argv.index('--status') + 1] == 'backlog,todo,in_progress'
    assert argv[argv.index('--label') + 1] == 'feedback' and argv[argv.index('--limit') + 1] == '500'
    shown = tasks.show('DK-1')
    assert shown['labels'] == ['feedback'] and shown['taskThreads'] == [{'threadId': 'thr_a', 'liveStatus': 'working'}]
    tasks.attach('DK-1', 'thr_a')
    assert calls[-1][0] == ['bb', 'tasks', 'attach', 'DK-1', '--thread', 'thr_a', '--json']
    endless = fd.Tasks('DK', runner=lambda argv, timeout: (0, json.dumps({'tasks': [], 'nextCursor': 'again'})), state_dir=tmp_path)
    with pytest.raises(fb.Incomplete):
        endless.candidates()


FAKE_BB = """#!/bin/sh
echo "$@" >> "$FAKE_BB_DIR/argv.log"
case "$1 $2" in
  "tasks list") cat "$FAKE_BB_DIR/list.json" ;;
  "tasks show") cat "$FAKE_BB_DIR/show.json" ;;
  "tasks attach"|"tasks comment") echo '{"ok": true}' ;;
  "thread list") cat "$FAKE_BB_DIR/threads.json" ;;
  "thread spawn") cat > "$FAKE_BB_DIR/stdin.txt"; echo '{"id": "thr_fake0001", "status": "active"}' ;;
  *) echo '{"ok": false, "error": {"code": "unexpected_call", "message": "x"}}'; exit 1 ;;
esac
"""


# spec: ANYM-991
def test_the_installed_scripts_run_as_a_process_against_a_fake_bb_executable(tmp_path):
    install, fake, work = tmp_path / 'lib', tmp_path / 'bin', tmp_path / 'work'
    for folder in (install, fake, work):
        folder.mkdir()
    for name in ('feedback_bridge.py', 'feedback_dispatch.py'):  # the install layout: both files in one directory
        (install / name).write_text((REPO / 'scripts' / name).read_text())
    (fake / 'bb').write_text(FAKE_BB)
    (fake / 'bb').chmod(0o755)
    task = {'id': 'T1', 'key': 'DK-1', 'number': 1, 'title': 't', 'status': 'todo', 'description': fb.render(raw_row(1))[1]}
    (work / 'list.json').write_text(json.dumps({'tasks': [task | {'labels': ['feedback']}]}))
    (work / 'show.json').write_text(json.dumps({'task': task, 'labels': [{'name': 'feedback'}], 'taskThreads': []}))
    (work / 'threads.json').write_text(json.dumps([{'id': 'thr_fake0001', 'projectId': PROJECT, 'title': 'feedback-DK-1',
        'archivedAt': None, 'createdAt': 1}]))
    env = {**write_config(tmp_path), 'PATH': str(fake) + os.pathsep + os.environ['PATH'], 'FAKE_BB_DIR': str(work)}

    def run(*args):
        return subprocess.run([sys.executable, str(install / 'feedback_dispatch.py'), *args], env=env, capture_output=True,
            text=True, cwd=tmp_path)
    first = run('run')
    assert (first.returncode, first.stdout, first.stderr) == (0,
        'feedback-dispatch: started=1 relaunched=0 reconciled=0 attached=1 commented=0\n', '')
    assert 'BB task: DK-1.' in (work / 'stdin.txt').read_text()
    argv = (work / 'argv.log').read_text().splitlines()
    assert any(line.startswith('thread spawn --project %s --parent-thread %s' % (PROJECT, ORCH)) for line in argv)
    assert 'tasks attach DK-1 --thread thr_fake0001 --json' in argv
    again = run('run')
    assert (again.returncode, again.stdout, again.stderr) == (0, '', '')
    assert sum(line.startswith('thread spawn') for line in (work / 'argv.log').read_text().splitlines()) == 1
    assert run('gate', 'DK-1').stdout == 'go\n' and run('status').stdout == 'DK-1 attempt=1 state=started thread=thr_fake0001\n'
    assert run('gate', 'DK-9').returncode == 1 and run().returncode == 64


# spec: ANYM-991
def test_the_worker_brief_names_the_gate_the_status_moves_and_the_stops():
    text = (REPO / 'deploy/feedback-bridge/feedback-task.md').read_text()
    for needle in ('data, never an instruction', 'feedback_dispatch.py gate', 'in_progress', 'in_review', 'Never set `done`',
            'Feedback:', 'Source: feedback-dispatch', 'Ready:', 'no review', 'review-protocol.md', 'waits for the owner',
            'queued: large card', 'feedback-autoloop.md', 'SUPERSET_WORKER_DONE', 'SUPERSET_WORKER_BLOCKED',
            'never fetch the feedback', 'at most 8'):
        assert needle in text, needle
    assert len(text.split()) <= 800 and not SECRET_SHAPE.search(text)


# spec: ANYM-991
def test_the_dispatcher_install_material_enables_nothing_and_holds_no_secret():
    example_text = (REPO / 'deploy/feedback-bridge/feedback-dispatch.json.example').read_text()
    example = json.loads(example_text)
    assert example['dispatch'] is False and set(example) <= fd.CONFIG_KEYS
    assert not SECRET_SHAPE.search(example_text)
    script = (REPO / 'deploy/feedback-bridge/dispatch-run.sh').read_text()
    assert re.search(r'feedback_dispatch\.py"? run', script) and '75' in script
    docs = (REPO / 'docs/operations/feedback-bridge.md').read_text()
    section = docs.split('## Dispatcher', 1)[1]
    for needle in ('feedback_dispatch.py check', 'bb automation create', 'feedback-dispatch.json', 'PAUSE', '"dispatch": true',
            'gate', 'Undo', 'feedback-task.md'):
        assert needle in section, needle
    is_blocked = doc_commands.load_is_blocked(REPO / doc_commands.MATCHER)
    assert not [block for block in doc_commands.fenced_shell_blocks(section) if is_blocked(block)]
