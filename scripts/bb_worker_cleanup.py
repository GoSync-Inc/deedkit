#!/usr/bin/env python3
"""Archives and stops a BB orchestrator's own finished helper threads that the orchestrator accepted (DK-41, ANYM-991).

    bb_worker_cleanup.py --project P --owner O                 dry run: print what would be released, change nothing
    bb_worker_cleanup.py --project P --owner O --apply         one bounded pass: archive, then stop, each accepted child
    bb_worker_cleanup.py --project P --owner O --clear-unknown THREAD   root's decision after checking an unknown outcome
    bb_worker_cleanup.py --project P --owner O --print-install          the exact `bb automation create` command

Authority is the owner's read-only acceptance manifest (docs/operations/bb-worker-cleanup.md); an idle thread, a
published source or a child's own report never is. BB's idle, queue, background-agent and terminal fields do not show a
provider's own shell jobs, so an entry counts only with root's `resources_closed: true` attestation, and any thread
activity after its `accepted_at` voids it. A child is released only when every fresh read also agrees: it is the
owner's unarchived ordinary child in the project, idle with no queue, interaction, todo, background work, live terminal or
unarchived descendant, and its worktree is clean with HEAD on a remote-tracking ref. The checks run again right before
each mutation. Every mutation is journaled before it runs and read back after; an unknown outcome is never repeated
until root clears it. Only the existing `bb thread list/show/archive/stop`, `bb thread queue list`, `bb thread
interactions list`, `bb terminal list` and `bb tasks show` are used; nothing is deleted and no task changes.
Standard library only. Exit 0 done, 1 error (one line on stderr), 75 busy (another pass holds the lock), 64 usage.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import uuid

SCHEMA = 'deedkit.bb-worker-acceptance/v1'
THREAD = re.compile(r'thr_[a-z0-9]{6,40}')
PROJECT = re.compile(r'proj_[a-z0-9]{6,40}')
TASK = re.compile(r'[A-Z][A-Z0-9]{1,9}-[1-9][0-9]*')
AUTHORITIES = ('accepted', 'task_done')
ACTIVITY = ('activeWorkflowCount', 'activeBackgroundAgentCount', 'activeBackgroundCommandCount', 'activePlanModeCount')
MAX_CHILDREN, MAX_ENTRIES, MAX_ACTIONS, CHUNK, MANIFEST_BYTES = 200, 500, 10, 50, 1 << 20
ENV_PROJECT, ENV_OWNER, ENV_MODE, ENV_STATE = ('DEEDKIT_BB_CLEANUP_PROJECT', 'DEEDKIT_BB_CLEANUP_OWNER',
    'DEEDKIT_BB_CLEANUP_MODE', 'DEEDKIT_BB_CLEANUP_STATE_DIR')
EX_OK, EX_ERROR, EX_USAGE, EX_BUSY = 0, 1, 64, 75


class CleanupError(Exception):
    """A named failure; `code` is a fixed snake_case word, safe for a log line."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Unknown(CleanupError):
    """The call may or may not have taken effect (timeout, crash, unreadable reply)."""


class Refused(CleanupError):
    """BB answered with an explicit error: the call did not take effect."""


def _run(argv, timeout):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return done.returncode, done.stdout


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Bb:
    """The local `bb` CLI, JSON only. A reply with `ok: false` is Refused; anything unreadable is Unknown."""

    def __init__(self, project, runner=None):
        self.project, self.runner = project, runner or _run

    def call(self, *argv, timeout=60):
        try:
            code, out = self.runner(['bb', *argv, '--json'], timeout)
        except subprocess.TimeoutExpired:
            raise Unknown('bb_timeout') from None
        except OSError:
            raise Refused('bb_unavailable') from None
        try:
            data = json.loads(out)
        except ValueError:
            raise Unknown('bb_bad_output') from None
        if isinstance(data, dict) and data.get('ok') is False:
            raise Refused('bb_refused')
        if code != 0:
            raise Unknown('bb_failed')
        return data

    def _rows(self, argv):
        data = self.call('thread', 'list', *argv)
        if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
            raise Unknown('bb_bad_output')
        return data

    def children(self, parent, hidden=False):
        return self._rows(['--project', self.project, '--parent-thread', parent] + (['--include-hidden'] if hidden else []))

    def rows(self, ids):
        found = []
        for n in range(0, len(ids), CHUNK):
            found += self._rows(['--include-archived'] + [x for i in ids[n:n + CHUNK] for x in ('--thread', i)])
        return found

    def show(self, thread):
        data = self.call('thread', 'show', thread)
        if not isinstance(data, dict) or not isinstance(data.get('thread'), dict) or data['thread'].get('id') != thread:
            raise Unknown('bb_bad_output')
        return data

    def empty(self, *argv):
        data = self.call(*argv)
        if not isinstance(data, list):
            raise Unknown('bb_bad_output')
        return not data

    def terminals(self, thread):
        data = self.call('terminal', 'list', '--thread', thread)
        if not isinstance(data, dict) or not isinstance(data.get('sessions'), list):
            raise Unknown('bb_bad_output')
        return data['sessions']

    def task(self, key):
        data = self.call('tasks', 'show', key)
        if not isinstance(data, dict) or not isinstance(data.get('task'), dict):
            raise Unknown('bb_bad_output')
        return data

    def archive(self, thread):
        return self.call('thread', 'archive', thread)

    def stop(self, thread):
        return self.call('thread', 'stop', thread)


def git_clean(path, runner=None):
    """None when the worktree is clean and HEAD sits on a remote-tracking ref; else a reason word."""
    runner = runner or _run
    try:
        if not Path(path).is_dir():
            return 'worktree_unreadable'
        code, out = runner(['git', '-C', path, 'status', '--porcelain', '--untracked-files=all'], 60)
        if code != 0:
            return 'worktree_unreadable'
        if out.strip():
            return 'worktree_dirty'
        code, out = runner(['git', '-C', path, 'for-each-ref', '--count=1', '--contains', 'HEAD', 'refs/remotes'], 60)
    except (OSError, subprocess.TimeoutExpired):
        return 'worktree_unreadable'
    if code != 0:
        return 'worktree_unreadable'
    return None if out.strip() else 'head_not_on_remote'


def private_file(path, uid):
    """The bytes of a regular, non-symlink file the caller owns that no one else may write; else CleanupError."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0))
    except FileNotFoundError:
        raise CleanupError('manifest_missing') from None
    except OSError:
        raise CleanupError('manifest_unsafe') from None
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_mode & 0o022 or st.st_size > MANIFEST_BYTES:
            raise CleanupError('manifest_unsafe')
        return f.read(MANIFEST_BYTES + 1)


def private_dir(path, uid):
    """Create or check a 0700 directory the caller owns; a symlink or a foreign or shared directory is refused."""
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or st.st_mode & 0o077:
        raise CleanupError('state_dir_unsafe')
    return path


def accepted_ms(value):
    """Epoch milliseconds of an ISO 8601 time with a zone (BB's timestamps are milliseconds); else None."""
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return None if at.tzinfo is None else int(at.timestamp() * 1000)


def load_manifest(path, project, owner, allow_task_done, uid):
    """{thread: entry} of the usable entries; a manifest for another owner or project is refused whole."""
    try:
        data = json.loads(private_file(path, uid))
    except ValueError:
        raise CleanupError('manifest_invalid') from None
    if not isinstance(data, dict) or data.get('schema') != SCHEMA or not isinstance(data.get('entries'), list):
        raise CleanupError('manifest_invalid')
    if data.get('owner') != owner or data.get('project') != project:
        raise CleanupError('manifest_other_owner')
    if len(data['entries']) > MAX_ENTRIES:
        raise CleanupError('manifest_too_large')
    seen, usable = {}, {}
    for e in data['entries']:
        thread = e.get('thread') if isinstance(e, dict) else None
        if isinstance(thread, str) and THREAD.fullmatch(thread):
            seen[thread] = seen.get(thread, 0) + 1
            usable[thread] = e
    out = {}
    for thread, e in usable.items():
        receipt, authority = e.get('receipt'), e.get('authority')
        if seen[thread] != 1 or thread == owner or not isinstance(receipt, str) or not receipt.strip():
            continue
        if e.get('resources_closed') is not True or accepted_ms(e.get('accepted_at')) is None:
            continue
        if authority == 'accepted' or (authority == 'task_done' and allow_task_done
                and isinstance(e.get('task'), str) and TASK.fullmatch(e['task'])):
            out[thread] = e
    return out


class Journal:
    """Append-only JSON lines, fsynced before the mutation they announce. The last record per thread and action wins."""

    def __init__(self, state_dir):
        self.path = Path(state_dir) / 'journal.jsonl'
        self.last = {}
        if self.path.exists():
            if self.path.is_symlink():
                raise CleanupError('state_dir_unsafe')
            text = self.path.read_text()
            lines = text.split('\n')
            for n, line in enumerate(lines):
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    if n == len(lines) - 1:  # a torn last line: its fsync never finished, so its mutation never ran
                        continue
                    raise CleanupError('journal_unreadable') from None
                if isinstance(rec, dict) and 'thread' in rec and 'action' in rec:
                    self.last[(rec['thread'], rec['action'])] = rec

    def state(self, thread, action):
        rec = self.last.get((thread, action))
        return rec.get('state') if rec else None

    def write(self, run, thread, action, state, reason=None):
        rec = {'at': now_iso(), 'run': run, 'thread': thread, 'action': action, 'state': state}
        if reason:
            rec['reason'] = reason
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as f:
            f.write(json.dumps(rec, sort_keys=True) + '\n')
            f.flush()
            os.fsync(f.fileno())
        self.last[(thread, action)] = rec

    def blocked(self, thread):
        """An action whose outcome nobody confirmed: an intent without a result (a crash) or an unknown."""
        return any(self.state(thread, a) in ('intent', 'unknown') for a in ('archive', 'stop'))


def identity_reason(t, project, owner):
    """None when the row is the owner's own ordinary, unarchived child in the project."""
    if t.get('id') == owner or not isinstance(t.get('id'), str) or not THREAD.fullmatch(t['id']):
        return 'not_a_child'
    if t.get('projectId') != project:
        return 'other_project'
    if t.get('parentThreadId') != owner or t.get('lifecycleOwnerThreadId') != owner:
        return 'other_owner'
    if t.get('originKind') is not None or t.get('originPluginId') is not None or t.get('visibility') != 'visible':
        return 'not_ordinary'
    if t.get('deletedAt') is not None:
        return 'deleted'
    if t.get('archivedAt') is not None:
        return 'archived'
    return None


def quiet_reason(t):
    """None when a thread row or `show` thread says idle with nothing queued, pending or running."""
    if t.get('status') != 'idle':
        return 'not_idle'
    runtime = t.get('runtime')
    if not isinstance(runtime, dict) or runtime.get('displayStatus') != 'idle':
        return 'not_idle'
    for name in ('queuedMessageCount', 'activeBackgroundAgentCount'):
        if name in t and t[name] != 0:
            return 'queued_or_background'
    if 'queuedWork' in t and t['queuedWork'] != 'none':
        return 'queued_or_background'
    if 'hasPendingInteraction' in t and t['hasPendingInteraction'] is not False:
        return 'pending_interaction'
    activity = t.get('activity')
    if activity is not None and (not isinstance(activity, dict)
            or any(type(activity.get(n)) is not int or activity[n] != 0 for n in ACTIVITY)):
        return 'queued_or_background'
    return None


class Cleanup:
    def __init__(self, project, owner, manifest, state_dir, *, bb=None, runner=None, apply=False, max_actions=MAX_ACTIONS,
                 out=None):
        self.project, self.owner, self.manifest, self.apply = project, owner, manifest, apply
        self.bb = bb or Bb(project, runner)
        self.runner, self.max_actions, self.out = runner, max_actions, out or sys.stdout
        self.journal = Journal(state_dir)
        self.state_dir, self.run_id, self.results = Path(state_dir), uuid.uuid4().hex[:12], []

    def note(self, thread, outcome, reason=None):
        self.results.append({'thread': thread, 'outcome': outcome, 'reason': reason})
        print('%s %s%s' % (thread, outcome, ' ' + reason if reason else ''), file=self.out)

    def authority_reason(self, thread):
        entry = self.manifest.get(thread)
        if entry is None:
            return 'no_acceptance'
        shown = self.bb.show(thread)['thread']
        stamps = [shown.get(k) for k in ('updatedAt', 'latestAttentionAt')]
        if any(type(s) is not int for s in stamps) or max(stamps) > accepted_ms(entry['accepted_at']):
            return 'activity_after_acceptance'
        if entry['authority'] == 'accepted':
            return None
        data = self.bb.task(entry['task'])
        linked = data.get('taskThreads')
        if data['task'].get('status') != 'done' or not isinstance(linked, list) \
                or not any(isinstance(x, dict) and x.get('threadId') == thread for x in linked):
            return 'task_not_done'
        return None

    def check(self, thread):
        """(reason or None, fingerprint) from fresh reads of everything that must hold before a mutation."""
        shown = self.bb.show(thread)
        t = shown['thread']
        reason = identity_reason(t, self.project, self.owner) or quiet_reason(t)
        if reason:
            return reason, None
        if shown.get('pendingTodos'):
            return 'pending_todos', None
        rows = self.bb.rows([thread])
        if len(rows) != 1 or rows[0].get('id') != thread:
            return 'unreadable', None
        reason = identity_reason(rows[0], self.project, self.owner) or quiet_reason(rows[0])
        if reason:
            return reason, None
        if not self.bb.empty('thread', 'queue', 'list', thread):
            return 'queued_or_background', None
        if not self.bb.empty('thread', 'interactions', 'list', thread):
            return 'pending_interaction', None
        if any(not isinstance(s, dict) or s.get('status') != 'exited' for s in self.bb.terminals(thread)):
            return 'live_terminal', None
        if self.bb.children(thread, hidden=True):
            return 'live_descendant', None
        env = shown.get('environment')
        if env is not None:
            path = env.get('path') if isinstance(env, dict) else None
            if not isinstance(path, str) or not path.startswith('/'):
                return 'worktree_unreadable', None
            reason = git_clean(path, self.runner)
            if reason:
                return reason, None
        reason = self.authority_reason(thread)
        return reason, (None if reason else (t.get('updatedAt'), rows[0].get('updatedAt')))

    def mutate(self, thread, action, verify):
        """Journal the intent, call BB once, read back; a result nobody can confirm is recorded unknown."""
        self.journal.write(self.run_id, thread, action, 'intent')
        failure = None
        try:
            getattr(self.bb, action)(thread)
        except Refused as e:
            failure = ('refused', e.code)
        except Unknown as e:
            failure = ('unknown', e.code)
        try:
            ok = verify(self.bb.show(thread)['thread'])
        except CleanupError:
            ok = False
        if ok:
            self.journal.write(self.run_id, thread, action, 'done')
            return True
        state, code = failure or ('unknown', 'readback_mismatch')
        self.journal.write(self.run_id, thread, action, state, code)
        return False

    def release(self, thread):
        """Archive (history kept), then stop the loaded runtime; each step re-checked right before it."""
        if self.journal.state(thread, 'archive') != 'done':
            reason, before = self.check(thread)
            if reason:
                return self.note(thread, 'skip', reason)
            reason, again = self.check(thread)
            if reason or again != before:
                return self.note(thread, 'skip', reason or 'changed')
            if not self.mutate(thread, 'archive', lambda t: t.get('archivedAt') is not None):
                return self.note(thread, self.journal.state(thread, 'archive'), 'archive')
        self.stop_archived(thread)

    def stop_archived(self, thread):
        t = self.bb.show(thread)['thread']
        if t.get('archivedAt') is None or quiet_reason(t) or not self.bb.empty('thread', 'queue', 'list', thread) \
                or self.bb.children(thread, hidden=True):
            return self.note(thread, 'skip', 'changed_after_archive')
        stopped = self.mutate(thread, 'stop', lambda t: t.get('archivedAt') is not None and t.get('status') != 'active'
            and (t.get('runtime') or {}).get('displayStatus') != 'active')
        self.note(thread, 'released' if stopped else self.journal.state(thread, 'stop'), None if stopped else 'stop')

    def reconcile(self):
        """Settle intents a crash left behind by reading back; finish a stop after a confirmed archive. Never resend."""
        pending = sorted({t for (t, a), rec in self.journal.last.items() if rec.get('state') == 'intent'})
        for thread in pending:
            try:
                t = self.bb.show(thread)['thread']
            except CleanupError:
                continue
            if self.journal.state(thread, 'archive') == 'intent':
                self.journal.write(self.run_id, thread, 'archive', 'done' if t.get('archivedAt') is not None else 'unknown',
                    None if t.get('archivedAt') is not None else 'crash_before_readback')
            if self.journal.state(thread, 'stop') == 'intent':
                self.journal.write(self.run_id, thread, 'stop', 'unknown', 'crash_before_readback')

    def run(self):
        self.reconcile()
        children = self.bb.children(self.owner)
        if len(children) > MAX_CHILDREN:
            raise CleanupError('too_many_children')
        listed = {r.get('id') for r in children}
        actions = 0
        for row in sorted(children, key=lambda r: str(r.get('id'))):
            thread = row.get('id')
            if self.journal.blocked(thread):
                self.note(thread, 'skip', 'unknown_needs_root')
                continue
            reason = identity_reason(row, self.project, self.owner) or quiet_reason(row) or (
                None if thread in self.manifest else 'no_acceptance')
            if reason:
                self.note(thread, 'skip', reason)
                continue
            if actions >= self.max_actions:
                self.note(thread, 'skip', 'action_cap')
                continue
            try:
                if not self.apply:
                    reason, _ = self.check(thread)
                    self.note(thread, 'skip' if reason else 'would_release', reason)
                    continue
                actions += 1
                self.release(thread)
            except CleanupError as e:
                self.note(thread, 'skip', 'unreadable_' + e.code)
        self.settle_archived([t for t in sorted(self.manifest) if t not in listed])
        self.write_receipt()
        return self.results

    def settle_archived(self, threads):
        """Accepted threads that are no longer unarchived children: never open their worktree or restart them."""
        if not threads:
            return
        try:
            rows = {r.get('id'): r for r in self.bb.rows(threads)}
        except CleanupError as e:
            return self.note('manifest', 'skip', 'unreadable_' + e.code)
        for thread in threads:
            row = rows.get(thread)
            if row is None or row.get('deletedAt') is not None:
                self.note(thread, 'gone')
            elif row.get('archivedAt') is None:
                self.note(thread, 'skip', identity_reason(row, self.project, self.owner) or 'not_listed')
            elif self.apply and self.journal.state(thread, 'archive') == 'done' \
                    and self.journal.state(thread, 'stop') in (None, 'refused'):
                try:
                    self.stop_archived(thread)
                except CleanupError as e:
                    self.note(thread, 'skip', 'unreadable_' + e.code)
            else:
                self.note(thread, 'already_archived')

    def write_receipt(self):
        receipt = {'run': self.run_id, 'at': now_iso(), 'project': self.project, 'owner': self.owner, 'apply': self.apply,
            'results': self.results}
        tmp = self.state_dir / ('.last-run.%s.json' % self.run_id)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(receipt, f, sort_keys=True, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_dir / 'last-run.json')


def default_state_dir(owner, environ):
    if environ.get(ENV_STATE):
        return Path(environ[ENV_STATE])
    return Path(environ.get('HOME') or Path.home()) / '.local/state/deedkit/bb-worker-cleanup' / owner


def install_command(project, owner, script):
    env = json.dumps({ENV_PROJECT: project, ENV_OWNER: owner, ENV_MODE: 'apply'}, sort_keys=True)
    return shlex.join(['bb', 'automation', 'create', '--project', project, '--name', 'DeedKit BB worker cleanup',
        '--cron', '*/10 * * * *', '--timezone', 'UTC', '--script-file', script, '--interpreter', 'python3',
        '--timeout', '300000', '--env-json', env, '--working-directory', 'automation-storage'])


def parse(argv, environ):
    p = argparse.ArgumentParser(prog='bb_worker_cleanup.py', description='Release accepted finished BB helper threads.')
    p.add_argument('--project', default=environ.get(ENV_PROJECT))
    p.add_argument('--owner', default=environ.get(ENV_OWNER))
    p.add_argument('--apply', action='store_true', default=environ.get(ENV_MODE) == 'apply')
    p.add_argument('--manifest')
    p.add_argument('--state-dir')
    p.add_argument('--allow-task-done', action='store_true')
    p.add_argument('--max-actions', type=int, default=MAX_ACTIONS)
    p.add_argument('--clear-unknown', metavar='THREAD')
    p.add_argument('--print-install', action='store_true')
    a = p.parse_args(argv)
    if not (isinstance(a.project, str) and PROJECT.fullmatch(a.project) and isinstance(a.owner, str)
            and THREAD.fullmatch(a.owner) and 1 <= a.max_actions <= 50):
        p.error('--project proj_... and --owner thr_... are required; --max-actions 1..50')
    if a.clear_unknown is not None and not THREAD.fullmatch(a.clear_unknown):
        p.error('--clear-unknown takes a thread id')
    return a


def main(argv=None, environ=None, runner=None, bb=None, uid=None, out=None):
    environ = os.environ if environ is None else environ
    try:
        a = parse(sys.argv[1:] if argv is None else argv, environ)
    except SystemExit:
        return EX_USAGE
    out = out or sys.stdout
    if a.print_install:
        print(install_command(a.project, a.owner, str(Path(__file__).resolve())), file=out)
        return EX_OK
    uid = os.getuid() if uid is None else uid
    try:
        state = private_dir(a.state_dir or default_state_dir(a.owner, environ), uid)
        lock = os.open(state / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except (CleanupError, OSError) as e:
        print('error %s' % getattr(e, 'code', 'state_dir_unsafe'), file=sys.stderr)
        return EX_ERROR
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print('busy', file=sys.stderr)
            return EX_BUSY
        if a.clear_unknown:
            journal = Journal(state)
            for action in ('archive', 'stop'):
                if journal.state(a.clear_unknown, action) in ('intent', 'unknown'):
                    journal.write('root', a.clear_unknown, action, 'cleared')
            print('%s cleared' % a.clear_unknown, file=out)
            return EX_OK
        manifest = load_manifest(a.manifest or state / 'acceptance.json', a.project, a.owner, a.allow_task_done, uid)
        Cleanup(a.project, a.owner, manifest, state, bb=bb, runner=runner, apply=a.apply, max_actions=a.max_actions,
            out=out).run()
        return EX_OK
    except CleanupError as e:
        print('error %s' % e.code, file=sys.stderr)
        return EX_ERROR
    finally:
        os.close(lock)


if __name__ == '__main__':
    sys.exit(main())
