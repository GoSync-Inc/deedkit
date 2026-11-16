"""Release of an orchestrator's accepted, finished BB helper threads: authority, fresh checks, journal (DK-41)."""
import io
import json
import os
from pathlib import Path
import subprocess

import pytest

import scripts.bb_worker_cleanup as wc

PROJECT, OWNER, OTHER = 'proj_test000001', 'thr_root000001', 'thr_other00001'
MUTATIONS = ('archive', 'stop')
ACCEPTED_AT = '2026-11-16T08:00:00Z'
BEFORE = wc.accepted_ms(ACCEPTED_AT) - 60000  # the child's last activity, a minute before root accepted it


def entry(thread, **over):
    return {'thread': thread, 'authority': 'accepted', 'receipt': 'root receipt', 'accepted_at': ACCEPTED_AT,
        'resources_closed': True} | over


class Crash(BaseException):
    """A process death at one chosen point."""


class FakeBb:
    """The `bb` and `git` CLIs over memory, in the JSON shapes BB prints; every argv is logged."""

    SHOW_ONLY = ('queuedMessageCount', 'activeBackgroundAgentCount')
    LIST_ONLY = ('activity', 'queuedWork', 'hasPendingInteraction')

    def __init__(self, tmp_path):
        self.tmp, self.threads, self.calls = tmp_path, {}, []
        self.queue, self.interactions, self.terminals, self.todos, self.tasks, self.git = {}, {}, {}, {}, {}, {}
        self.fail, self.before = {}, []

    def add(self, tid, env=True, **over):
        path = self.tmp / 'wt' / tid
        path.mkdir(parents=True)
        row = {'id': tid, 'projectId': PROJECT, 'title': 't', 'status': 'idle', 'parentThreadId': OWNER,
            'lifecycleOwnerThreadId': OWNER, 'sourceThreadId': None, 'originKind': None, 'originPluginId': None,
            'visibility': 'visible', 'archivedAt': None, 'deletedAt': None, 'createdAt': BEFORE - 9000,
            'updatedAt': BEFORE, 'latestAttentionAt': BEFORE - 5000,
            'runtime': {'displayStatus': 'idle'}, 'queuedMessageCount': 0, 'activeBackgroundAgentCount': 0,
            'activity': {n: 0 for n in wc.ACTIVITY}, 'queuedWork': 'none', 'hasPendingInteraction': False,
            'environmentPath': str(path) if env else None}
        row.update(over)
        self.threads[tid] = row
        self.git[str(path)] = ('', 'refs/remotes/origin/bb/x\n')
        return row

    def mutations(self):
        return [c[2:4] for c in self.calls if c[0] == 'bb' and c[1] == 'thread' and c[2] in MUTATIONS]

    def _list_row(self, r):
        return {k: v for k, v in r.items() if k not in self.SHOW_ONLY}

    def _thread_list(self, a):
        def opt(name):
            return [a[i + 1] for i, x in enumerate(a) if x == name]
        archived = '--include-archived' in a
        ids, project, parent = opt('--thread'), opt('--project'), opt('--parent-thread')
        rows = [r for r in self.threads.values() if r['deletedAt'] is None and (archived or r['archivedAt'] is None)
            and (not ids or r['id'] in ids) and (not project or r['projectId'] == project[0])
            and (not parent or r['parentThreadId'] == parent[0])]
        return [self._list_row(r) for r in rows]

    def _show(self, tid):
        r = self.threads.get(tid)
        if r is None:
            return 1, json.dumps({'ok': False, 'error': {'code': 'thread_not_found', 'message': 'x'}})
        t = {k: v for k, v in r.items() if k not in self.LIST_ONLY and k != 'environmentPath'}
        env = None if r['environmentPath'] is None else {'id': 'env_x', 'path': r['environmentPath'], 'isWorktree': True}
        return 0, json.dumps({'thread': t, 'environment': env, 'pendingTodos': self.todos.get(tid)})

    def _mutate(self, verb, tid):
        mode = self.fail.pop((verb, tid), None)
        if mode == 'refused':
            return 1, json.dumps({'ok': False, 'error': {'code': 'conflict', 'message': 'x'}})
        if mode == 'timeout_not_applied':
            raise subprocess.TimeoutExpired('bb', 60)
        if mode == 'crash':
            raise Crash
        if verb == 'archive':
            self.threads[tid]['archivedAt'] = 99
        if mode == 'timeout_applied':
            raise subprocess.TimeoutExpired('bb', 60)
        return 0, json.dumps({'id': tid})

    def __call__(self, argv, timeout):
        self.calls.append(tuple(argv))
        for hook in list(self.before):
            hook(tuple(argv))
        if argv[0] == 'git':
            porcelain, refs = self.git[argv[2]]
            return 0, porcelain if argv[3] == 'status' else refs
        assert argv[0] == 'bb' and argv[-1] == '--json'
        a = list(argv[1:-1])
        if a[:2] == ['thread', 'list']:
            return 0, json.dumps(self._thread_list(a[2:]))
        if a[:2] == ['thread', 'show']:
            return self._show(a[2])
        if a[:3] == ['thread', 'queue', 'list']:
            return 0, json.dumps(self.queue.get(a[3], []))
        if a[:3] == ['thread', 'interactions', 'list']:
            return 0, json.dumps(self.interactions.get(a[3], []))
        if a[:2] == ['terminal', 'list']:
            return 0, json.dumps({'sessions': self.terminals.get(a[3], [])})
        if a[:2] == ['tasks', 'show']:
            task = self.tasks[a[2]]
            return 0, json.dumps({'task': {'key': a[2], 'status': task[0]},
                'taskThreads': [{'threadId': t, 'liveStatus': 'idle'} for t in task[1]]})
        if a[0] == 'thread' and a[1] in MUTATIONS:
            return self._mutate(a[1], a[2])
        raise AssertionError(argv)


class Rig:
    def __init__(self, tmp_path):
        self.bb, self.state = FakeBb(tmp_path), tmp_path / 'state'
        self.state.mkdir(mode=0o700)
        self.manifest = self.state / 'acceptance.json'

    def accept(self, *entries, mode=0o600, **over):
        doc = {'schema': wc.SCHEMA, 'owner': OWNER, 'project': PROJECT, 'entries': [
            e if isinstance(e, dict) else entry(e) for e in entries]}
        doc.update(over)
        self.manifest.write_text(json.dumps(doc))
        self.manifest.chmod(mode)

    def main(self, *extra):
        out = io.StringIO()
        argv = ['--project', PROJECT, '--owner', OWNER, '--state-dir', str(self.state), '--manifest', str(self.manifest),
            *extra]
        code = wc.main(argv, environ={}, runner=self.bb, out=out)
        self.out = out.getvalue()
        return code

    def journal(self, tid=None):
        path = self.state / 'journal.jsonl'
        recs = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
        return [(r['action'], r['state']) for r in recs if tid is None or r['thread'] == tid]


# spec: ANYM-991
def test_an_accepted_finished_child_is_archived_then_stopped_once_and_a_rerun_changes_nothing(tmp_path):
    rig = Rig(tmp_path)
    rig.bb.add('thr_child00001')
    rig.accept('thr_child00001')
    assert rig.main('--apply') == 0
    assert rig.bb.mutations() == [('archive', 'thr_child00001'), ('stop', 'thr_child00001')]
    assert rig.journal() == [('archive', 'intent'), ('archive', 'done'), ('stop', 'intent'), ('stop', 'done')]
    assert 'thr_child00001 released' in rig.out
    receipt = json.loads((rig.state / 'last-run.json').read_text())
    assert receipt['apply'] is True and receipt['results'][0]['outcome'] == 'released'
    assert oct((rig.state / 'journal.jsonl').stat().st_mode & 0o777) == '0o600'
    assert rig.main('--apply') == 0
    assert len(rig.bb.mutations()) == 2 and 'thr_child00001 already_archived' in rig.out


# spec: ANYM-991
def test_without_apply_the_pass_only_reports_what_it_would_release(tmp_path):
    rig = Rig(tmp_path)
    rig.bb.add('thr_child00001')
    rig.accept('thr_child00001')
    assert rig.main() == 0
    assert rig.bb.mutations() == [] and rig.journal() == []
    assert 'thr_child00001 would_release' in rig.out


def _blocker(name, rig, tid):
    bb, row = rig.bb, rig.bb.threads[tid]
    if name == 'foreign_parent':
        row['parentThreadId'] = OTHER
    elif name == 'foreign_lifecycle_owner':
        row['lifecycleOwnerThreadId'] = OTHER
    elif name == 'user_created':
        row['lifecycleOwnerThreadId'], row['originKind'] = None, 'user'
    elif name == 'plugin_created':
        row['originPluginId'] = 'automations'
    elif name == 'hidden':
        row['visibility'] = 'hidden'
    elif name == 'not_idle':
        row['status'], row['runtime'] = 'active', {'displayStatus': 'active'}
    elif name == 'queued_message':
        row['queuedMessageCount'] = 1
    elif name == 'queued_work':
        row['queuedWork'] = 'queued'
    elif name == 'queue_list':
        bb.queue[tid] = [{'id': 'q1'}]
    elif name == 'background_agent':
        row['activeBackgroundAgentCount'] = 1
    elif name == 'background_command':
        row['activity']['activeBackgroundCommandCount'] = 1
    elif name == 'pending_interaction':
        bb.interactions[tid] = [{'id': 'i1'}]
    elif name == 'pending_todos':
        bb.todos[tid] = [{'content': 'x', 'status': 'pending'}]
    elif name == 'live_terminal':
        bb.terminals[tid] = [{'id': 'term1', 'status': 'running', 'exitCode': None}]
    elif name == 'active_child':
        bb.add('thr_grand00001', parentThreadId=tid, lifecycleOwnerThreadId=tid, status='active',
            runtime={'displayStatus': 'active'})
    elif name == 'idle_unarchived_child':
        bb.add('thr_grand00001', parentThreadId=tid, lifecycleOwnerThreadId=tid)
    elif name == 'dirty_worktree':
        bb.git[row['environmentPath']] = (' M scripts/x.py\n', 'refs/remotes/origin/main\n')
    elif name == 'untracked_file':
        bb.git[row['environmentPath']] = ('?? notes.md\n', 'refs/remotes/origin/main\n')
    elif name == 'unpushed_head':
        bb.git[row['environmentPath']] = ('', '')
    elif name == 'missing_worktree':
        os.rmdir(row['environmentPath'])
    elif name == 'missing_acceptance':
        rig.accept()
    elif name == 'empty_receipt':
        rig.accept(entry(tid, receipt=' '))
    elif name == 'duplicate_entry':
        rig.accept(tid, tid)
    elif name == 'unknown_authority':
        rig.accept(entry(tid, authority='source_done'))
    elif name == 'task_done_not_armed':
        bb.tasks['DK-7'] = ('done', [tid])
        rig.accept(entry(tid, authority='task_done', task='DK-7'))
    elif name == 'other_project':
        row['projectId'] = 'proj_other00001'
    elif name == 'resources_not_attested':  # idle, no queue, no background agent, no terminal: still not proof
        rig.accept(entry(tid, resources_closed=None))
    elif name == 'resources_attested_as_text':
        rig.accept(entry(tid, resources_closed='true'))
    elif name == 'accepted_at_missing':
        rig.accept(entry(tid, accepted_at=None))
    elif name == 'accepted_at_without_zone':
        rig.accept(entry(tid, accepted_at='2026-11-16T08:00:00'))
    elif name == 'turn_after_acceptance':
        row['updatedAt'] = BEFORE + 120000
    elif name == 'attention_after_acceptance':
        row['latestAttentionAt'] = BEFORE + 120000


BLOCKERS = ('foreign_parent', 'foreign_lifecycle_owner', 'user_created', 'plugin_created', 'hidden', 'not_idle',
    'queued_message', 'queued_work', 'queue_list', 'background_agent', 'background_command', 'pending_interaction',
    'pending_todos', 'live_terminal', 'active_child', 'idle_unarchived_child', 'dirty_worktree', 'untracked_file',
    'unpushed_head', 'missing_worktree', 'missing_acceptance', 'empty_receipt', 'duplicate_entry', 'unknown_authority',
    'task_done_not_armed', 'other_project', 'resources_not_attested', 'resources_attested_as_text', 'accepted_at_missing',
    'accepted_at_without_zone', 'turn_after_acceptance', 'attention_after_acceptance')


# spec: ANYM-991
@pytest.mark.parametrize('name', BLOCKERS)
def test_each_unfinished_or_foreign_condition_alone_keeps_the_thread(tmp_path, name):
    rig = Rig(tmp_path)
    rig.bb.add('thr_child00001')
    rig.accept('thr_child00001')
    _blocker(name, rig, 'thr_child00001')
    assert rig.main('--apply') == 0
    assert rig.bb.mutations() == [] and rig.journal() == []
    assert 'released' not in rig.out and rig.bb.threads['thr_child00001']['archivedAt'] is None


# spec: ANYM-991
def test_the_owner_itself_and_a_users_own_chat_are_never_selected_even_when_listed_in_the_manifest(tmp_path):
    rig = Rig(tmp_path)
    rig.bb.add(OWNER, parentThreadId=OWNER, lifecycleOwnerThreadId=OWNER)
    rig.bb.add('thr_user000001', parentThreadId=None, lifecycleOwnerThreadId=None)
    rig.accept(OWNER, 'thr_user000001')
    assert rig.main('--apply') == 0
    assert rig.bb.mutations() == []


# spec: ANYM-991
def test_a_task_done_entry_releases_only_when_root_armed_it_and_the_attached_task_is_done(tmp_path):
    rig = Rig(tmp_path)
    rig.bb.add('thr_child00001')
    rig.bb.add('thr_child00002')
    rig.bb.tasks.update({'DK-7': ('done', ['thr_child00001']), 'DK-8': ('in_progress', ['thr_child00002'])})
    rig.accept(entry('thr_child00001', authority='task_done', task='DK-7'),
        entry('thr_child00002', authority='task_done', task='DK-8'))
    assert rig.main('--apply', '--allow-task-done') == 0
    assert rig.bb.mutations() == [('archive', 'thr_child00001'), ('stop', 'thr_child00001')]
    assert 'thr_child00002 skip task_not_done' in rig.out


# spec: ANYM-991
def test_an_unknown_archive_is_never_sent_again_until_root_clears_it(tmp_path):
    rig = Rig(tmp_path)
    rig.bb.add('thr_child00001')
    rig.accept('thr_child00001')
    rig.bb.fail[('archive', 'thr_child00001')] = 'timeout_not_applied'
    assert rig.main('--apply') == 0
    assert rig.journal() == [('archive', 'intent'), ('archive', 'unknown')]
    assert rig.bb.mutations() == [('archive', 'thr_child00001')]
    assert rig.main('--apply') == 0
    assert rig.bb.mutations() == [('archive', 'thr_child00001')] and 'skip unknown_needs_root' in rig.out
    assert rig.main('--clear-unknown', 'thr_child00001') == 0
    assert rig.main('--apply') == 0
    assert rig.bb.mutations()[1:] == [('archive', 'thr_child00001'), ('stop', 'thr_child00001')]


# spec: ANYM-991
def test_an_archive_that_timed_out_but_applied_is_confirmed_by_readback_and_the_stop_follows(tmp_path):
    rig = Rig(tmp_path)
    rig.bb.add('thr_child00001')
    rig.accept('thr_child00001')
    rig.bb.fail[('archive', 'thr_child00001')] = 'timeout_applied'
    assert rig.main('--apply') == 0
    assert rig.journal() == [('archive', 'intent'), ('archive', 'done'), ('stop', 'intent'), ('stop', 'done')]


# spec: ANYM-991
def test_a_refused_archive_is_definite_and_a_later_pass_may_try_again(tmp_path):
    rig = Rig(tmp_path)
    rig.bb.add('thr_child00001')
    rig.accept('thr_child00001')
    rig.bb.fail[('archive', 'thr_child00001')] = 'refused'
    assert rig.main('--apply') == 0
    assert rig.journal() == [('archive', 'intent'), ('archive', 'refused')]
    assert rig.main('--apply') == 0
    assert rig.bb.threads['thr_child00001']['archivedAt'] is not None and rig.journal()[-1] == ('stop', 'done')


# spec: ANYM-991
def test_a_crash_after_the_journaled_intent_is_reconciled_by_readback_and_never_resent(tmp_path):
    rig = Rig(tmp_path)
    rig.bb.add('thr_child00001')
    rig.accept('thr_child00001')
    rig.bb.fail[('archive', 'thr_child00001')] = 'crash'
    with pytest.raises(Crash):
        rig.main('--apply')
    assert rig.journal() == [('archive', 'intent')]
    assert rig.main('--apply') == 0
    assert rig.journal()[-1] == ('archive', 'unknown') and rig.bb.mutations() == [('archive', 'thr_child00001')]


# spec: ANYM-991
@pytest.mark.parametrize('late', ('message_queued', 'turn_started', 'child_spawned', 'terminal_opened'))
def test_a_thread_that_gets_work_between_the_two_checks_is_left_alone(tmp_path, late):
    rig = Rig(tmp_path)
    rig.bb.add('thr_child00001')
    rig.accept('thr_child00001')
    shows = []

    def hook(argv):
        if argv[1:3] == ('thread', 'show') and argv[3] == 'thr_child00001':
            shows.append(1)
            if len(shows) == 2:
                row = rig.bb.threads['thr_child00001']
                if late == 'message_queued':
                    rig.bb.queue['thr_child00001'] = [{'id': 'q'}]
                elif late == 'turn_started':
                    row['status'], row['runtime'], row['updatedAt'] = 'active', {'displayStatus': 'active'}, 3
                elif late == 'child_spawned':
                    rig.bb.add('thr_grand00001', parentThreadId='thr_child00001', lifecycleOwnerThreadId='thr_child00001')
                else:
                    rig.bb.terminals['thr_child00001'] = [{'id': 't', 'status': 'running'}]
    rig.bb.before.append(hook)
    assert rig.main('--apply') == 0
    assert rig.bb.mutations() == [] and rig.journal() == []


# spec: ANYM-991
def test_a_turn_that_starts_right_after_the_archive_prevents_the_stop(tmp_path):
    rig = Rig(tmp_path)
    rig.bb.add('thr_child00001')
    rig.accept('thr_child00001')

    def hook(argv):
        if argv[1:3] == ('thread', 'archive'):
            rig.bb.threads['thr_child00001'].update(status='active', runtime={'displayStatus': 'active'})
    rig.bb.before.append(hook)
    assert rig.main('--apply') == 0
    assert rig.bb.mutations() == [('archive', 'thr_child00001')] and 'skip changed_after_archive' in rig.out


# spec: ANYM-991
def test_threads_root_already_archived_are_reported_without_opening_their_worktree_or_restarting_them(tmp_path):
    rig = Rig(tmp_path)
    for n in range(1, 4):
        rig.bb.add('thr_old0000%d' % n, archivedAt=1794813949614)
    os.rmdir(rig.bb.threads['thr_old00001']['environmentPath'])
    rig.accept('thr_old00001', 'thr_old00002', 'thr_old00003', 'thr_gone000001')
    assert rig.main('--apply') == 0
    assert rig.bb.mutations() == [] and not any(c[0] == 'git' for c in rig.bb.calls)
    assert not any(c[1:3] == ('thread', 'show') for c in rig.bb.calls)
    assert rig.out.count('already_archived') == 3 and 'thr_gone000001 gone' in rig.out


# spec: ANYM-991
@pytest.mark.parametrize('bad', ('symlink', 'group_writable', 'other_owner', 'other_project', 'wrong_schema'))
def test_an_unsafe_or_foreign_manifest_stops_the_whole_pass(tmp_path, bad):
    rig = Rig(tmp_path)
    rig.bb.add('thr_child00001')
    if bad == 'symlink':
        real = tmp_path / 'real.json'
        rig.accept('thr_child00001')
        rig.manifest.rename(real)
        rig.manifest.symlink_to(real)
    elif bad == 'group_writable':
        rig.accept('thr_child00001', mode=0o620)
    elif bad == 'other_owner':
        rig.accept('thr_child00001', owner=OTHER)
    elif bad == 'other_project':
        rig.accept('thr_child00001', project='proj_other00001')
    else:
        rig.accept('thr_child00001', schema='deedkit.bb-worker-acceptance/v0')
    assert rig.main('--apply') == 1
    assert rig.bb.calls == []


# spec: ANYM-991
def test_a_second_pass_while_one_holds_the_lock_exits_busy(tmp_path):
    import fcntl
    rig = Rig(tmp_path)
    rig.accept()
    fd = os.open(rig.state / 'lock', os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert rig.main('--apply') == 75
    finally:
        os.close(fd)
    assert rig.bb.calls == []


# spec: ANYM-991
def test_a_torn_last_journal_line_is_ignored_and_a_torn_middle_line_stops_the_pass(tmp_path):
    rig = Rig(tmp_path)
    rig.accept()
    journal = rig.state / 'journal.jsonl'
    journal.write_text('{"thread": "thr_child00001", "action": "archive", "state": "done"}\n{"thread": "thr_')
    assert rig.main('--apply') == 0
    journal.write_text('{"thread": "thr_\n{"thread": "thr_child00001", "action": "archive", "state": "done"}\n')
    assert rig.main('--apply') == 1


# spec: ANYM-991
def test_the_install_command_is_the_supported_script_automation_with_fixed_project_and_owner(tmp_path):
    rig = Rig(tmp_path)
    assert rig.main('--print-install') == 0
    line = rig.out.strip()
    assert line.startswith('bb automation create --project %s ' % PROJECT)
    assert "--script-file %s " % Path(wc.__file__).resolve() in line and '--interpreter python3' in line
    env = json.loads(line.split("--env-json '")[1].split("'")[0])
    assert env == {wc.ENV_PROJECT: PROJECT, wc.ENV_OWNER: OWNER, wc.ENV_MODE: 'apply'}
    assert rig.bb.calls == []


# spec: ANYM-991
def test_the_automation_environment_supplies_project_owner_and_apply(tmp_path):
    rig = Rig(tmp_path)
    rig.bb.add('thr_child00001')
    rig.accept('thr_child00001')
    environ = {wc.ENV_PROJECT: PROJECT, wc.ENV_OWNER: OWNER, wc.ENV_MODE: 'apply', wc.ENV_STATE: str(rig.state)}
    assert wc.main([], environ=environ, runner=rig.bb, out=io.StringIO()) == 0
    assert rig.bb.mutations() == [('archive', 'thr_child00001'), ('stop', 'thr_child00001')]
    assert wc.main([], environ={}, runner=rig.bb, out=io.StringIO()) == 64
