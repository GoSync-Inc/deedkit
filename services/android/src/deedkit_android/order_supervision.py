"""Bounded same-generation supervision of an already admitted lease and native conversation."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time

from .agent_run import MINUTE_S
from .codex_session import PersistentRun
from .config import Refused
from .secrets import Run as SecretRun

TICK_S = 5
BATCH = 32


@dataclass(repr=False)
class Handle:
    owner: str
    device: str
    task: str
    request: object
    config: object
    authorize: object
    session: object
    process: object
    thread: str
    turn: str
    source: object
    boundary: str
    time_limit: float
    until: float
    window: float
    commands: int = 0
    reason: str | None = None


class Supervision:
    def __init__(self, agents, *, clock=time.monotonic, wall=time.time):
        self.agents, self.clock, self.wall = agents, clock, wall
        self.handles, self.active, self.demand = {}, {}, {}
        self.budgets, self.refusals = {}, {}  # metadata only; digests cannot renew/adopt a token or create a live handle
        self.task, self.closing, self.cursor, self.handle_cursor = None, False, 0, 0
        self.db = agents.db.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS android_order_native_inputs
            (wake_id TEXT PRIMARY KEY,owner TEXT,device TEXT,request_id TEXT,
             digest TEXT,receipt TEXT)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS android_order_native_heads
            (owner TEXT,device TEXT,original_request_id TEXT,from_turn_id TEXT,
             foreground_request_id TEXT,foreground_turn_id TEXT,native_thread_id TEXT,boundary TEXT,
             PRIMARY KEY(owner,original_request_id,foreground_turn_id))''')
        self.db.commit()

    def admitted(self, run):
        self.budgets[(run.owner, run.id)] = {'seconds_limit': run.request.max_minutes * MINUTE_S, 'seconds_used': 0,
            'limit': run.request.max_actions, 'used': 0,
            'token_digest': hashlib.sha256(run.request.session_id.encode()).digest()}

    def budget(self, owner, task, token):
        budget = self.budgets.get((owner, task))
        if budget and budget.get('uncertain'):
            raise Refused('supervision_native_work_unconfirmed')
        if (budget is None or budget['token_digest'] != hashlib.sha256(token.encode()).digest()
                or budget['seconds_used'] >= budget['seconds_limit'] or budget['used'] >= budget['limit']):
            raise Refused('supervision_budget_unavailable')
        return budget

    def remaining(self, run):
        budget = self.budget(run.owner, run.id, run.request.session_id)
        occupied = 0 if getattr(run, '_supervision_accounted', False) else time.monotonic() - run.started
        return max(0, budget['seconds_limit'] - budget['seconds_used'] - occupied)

    def native_budget(self, run):
        budget = self.budget(run.owner, run.id, run.request.session_id)
        return {'current_task_id': run.id, 'active_remaining_ms': round(self.remaining(run) * 1000),
                'commands_remaining': max(0, budget['limit'] - budget['used'] - run.commands_started),
                'idle_interval_max_s': self.agents.store.reservation_window(run.owner, run.device, run.request.session_id)}

    def boundary(self, session, owner, device):
        row = self.agents.db.native_binding(owner, device)
        if not row or row['thread'] != session.thread_id or not row['boundary'] or not session.order_control_registered:
            raise Refused('supervision_boundary_unavailable')
        digest = hashlib.sha256(row['boundary'].encode()).hexdigest()
        if digest != getattr(session, 'boundary_receipt', None):
            raise Refused('supervision_boundary_changed')
        return digest

    def start(self):
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self.tick()
            except (OSError, ValueError, Refused, sqlite3.Error):
                # No payload/exception logging or authority reconstruction on a failed metadata tick.
                for handle in self.handles.values():
                    handle.reason = 'supervision_metadata_unavailable'
                continue

    def refs(self, handle):
        return self.db.execute('''SELECT order_id,next_check_at FROM android_order_obligations WHERE owner=?
            AND device_id=? AND original_request_id=? AND native_thread_id=? AND task_only=0
            AND lifecycle='active' AND responsible='codex' AND source_id=? AND auth_epoch=?''',
            (handle.owner, handle.device, handle.task, handle.thread, handle.source.source_id, handle.source.auth_epoch)).fetchall()

    def finished(self, run):
        try:
            self.retain(run)
        except (Refused, sqlite3.Error) as error:
            self.handles.pop((run.owner, run.id), None)  # optional retention failure cannot hide the native result
            self.refusals[(run.owner, run.id)] = error.code if isinstance(error, Refused) else 'supervision_metadata_unavailable'

    def retain(self, run):
        control = self.agents.order_control()
        if control is None or run.authorize is None:
            return
        if not isinstance(run, WakeRun):
            self.advance_heads(run)
        key, now = (run.owner, run.id), self.clock()
        budget = self.budget(run.owner, run.id, run.request.session_id)
        handle = self.handles.get(key)
        if handle is None:
            session = run.session
            if session is None or session.process is None or session.process.returncode is not None:
                return
            try:
                window = self.agents.store.reservation_window(run.owner, run.device, run.request.session_id)
            except Refused:
                return
            request = run.request.model_copy(update={'max_actions': budget['limit']})
            handle = Handle(run.owner, run.device, run.id, request, run.config, run.authorize,
                session, session.process, session.thread_id, session.last_turn_id,
                self.agents.read_config().agent.order_events, run.native_boundary, budget['seconds_limit'], now + window, window)
        if not self.refs(handle):
            self.handles.pop(key, None)
            return
        handle.commands = budget['used']
        handle.turn = run.session.last_turn_id
        handle.reason = None
        handle.until = now + handle.window  # only this task's genuinely successful completion establishes an interval
        self.handles[key] = handle
        try:
            self.authority(handle)
            self.agents.store.touch(handle.owner, handle.device, handle.request.session_id,
                until=self.wall() + handle.until - self.clock())
        except Refused:
            self.handles.pop(key, None)

    def authority(self, handle):
        if self.closing or self.handles.get((handle.owner, handle.task)) is not handle:
            raise Refused('supervision_unavailable')
        now = self.clock()
        self.budget(handle.owner, handle.task, handle.request.session_id)
        active = self.active.get(handle.device)
        own_active = active is not None and active.handle is handle and active.committed
        if (not own_active and now >= handle.until) or handle.commands >= handle.request.max_actions:
            raise Refused('supervision_budget_expired')
        principal = handle.authorize()
        config = self.agents.read_config()
        if (principal.id != handle.owner or handle.device not in principal.devices
                or 'android:control' not in principal.permissions or not config.agent.enabled
                or config.agent.guarded or not config.agent.persistent or config.agent.order_events != handle.source):
            raise Refused('supervision_access_changed')
        if (self.agents.sessions.get((handle.owner, handle.device)) is not handle.session
                or handle.session.process is not handle.process or handle.process.returncode is not None
                or handle.session.thread_id != handle.thread or handle.session.disconnected):
            raise Refused('supervision_conversation_unavailable')
        if not handle.session.order_control_registered:
            raise Refused('supervision_control_unregistered')
        if not handle.session.policy_proven:
            raise Refused('supervision_policy_unproven')
        self.agents.store.check_reservation(handle.owner, handle.device, handle.request.session_id)
        if self.agents.store.reservation_window(handle.owner, handle.device, handle.request.session_id) != handle.window:
            raise Refused('supervision_lease_changed')
        if self.boundary(handle.session, handle.owner, handle.device) != handle.boundary:
            raise Refused('supervision_boundary_changed')
        return principal

    def advance_heads(self, run):
        session = run.session
        if (not session.lock.locked() or not self.agents.devices.locks[run.device].locked()
                or self.agents.runs.get((run.owner, run.id)) is not run or run.native_completed_turn is None
                or run.native_completed_turn != run.native_admitted_turn or session.last_turn_id != run.native_completed_turn
                or session.process is not run.process or session.disconnected or session.events is not None
                or not session.policy_proven or not session.turn_policy_proven):
            return
        principal = run.authorize()
        if principal.id != run.owner or run.device not in principal.devices or 'android:control' not in principal.permissions:
            return
        self.agents.store.check_reservation(run.owner, run.device, run.request.session_id)
        if any(row['original_request_id'] == run.id and row.get('status') == 'unknown'
                and not (row['kind'] == 'control' and row['state'] == 'resolved' and row.get('operation_status') == 'applied')
                for row in session.interactions.values()):
            return
        advances = []
        for key, earlier in self.handles.items():
            if (key == (run.owner, run.id) or earlier.owner != run.owner or earlier.device != run.device
                    or earlier.session is not session or earlier.process is not run.process or earlier.thread != session.thread_id
                    or earlier.turn != run.native_previous_turn or earlier.request.session_id != run.request.session_id
                    or earlier.boundary != run.native_boundary):
                continue
            try:
                self.authority(earlier)  # the earlier key/scope, not merely the new foreground caller
            except Refused:
                continue
            advances.append(earlier)
        for earlier in advances:
            self.db.execute('INSERT OR IGNORE INTO android_order_native_heads VALUES(?,?,?,?,?,?,?,?)',
                (earlier.owner, earlier.device, earlier.task, earlier.turn, run.id, run.native_completed_turn,
                 earlier.thread, earlier.boundary))
        self.db.commit()
        for earlier in advances:
            earlier.turn = run.native_completed_turn  # correlation only: no budget, question, receipt or responsibility mutation

    def ready(self, handle, run=None):
        self.authority(handle)
        if (run is not None and run.committed and self.active.get(handle.device) is run
                and self.agents.runs.get((handle.owner, handle.task)) is run
                and handle.session.request_id == handle.task and handle.session.turn_id == handle.turn):
            return  # current admitted metadata replies are not another autonomous turn
        if self.demand.get(handle.device) or (handle.session.events is not None and (
                run is None or handle.session.request_id != handle.task or self.active.get(handle.device) is not run)):
            raise Refused('device_busy')
        if handle.session.last_turn_id != handle.turn:
            raise Refused('supervision_intervening_turn')
        for row in handle.session.interactions.values():
            if row['kind'] in ('question', 'async_question') and row['state'] == 'waiting':
                raise Refused('supervision_question_pending')
            if row['original_request_id'] != handle.task:
                continue  # retain other tasks' unknowns; never reinterpret or replay them
            if row.get('status') == 'unknown' and not (row['kind'] == 'control'
                    and row['state'] == 'resolved' and row.get('operation_status') == 'applied'):
                raise Refused('supervision_input_unknown')
        if handle.session.pending_commands:
            raise Refused('supervision_command_unfinished')

    def touch(self, handle, *, admission=None):
        self.authority(handle)
        active = self.active.get(handle.device)
        work = active is not None and active.handle is handle and (active.committed or active is admission)
        until = self.clock() + self.remaining(active) if work else handle.until
        self.agents.store.touch(handle.owner, handle.device, handle.request.session_id,
            until=self.wall() + until - self.clock())

    def account(self, run):
        if not getattr(run, '_supervision_accounted', False):
            run._supervision_accounted = True
            budget = self.budgets.get((run.owner, run.id))
            if budget:
                budget['used'] += run.commands_started
                budget['seconds_used'] += max(0, time.monotonic() - run.started)
                if run.session and run.id in run.session.pending_commands.values():
                    budget['uncertain'] = True  # a terminal turn with live commands is not completed delivery-idle work
                handle = self.handles.get((run.owner, run.id))
                if handle:
                    handle.commands = budget['used']

    async def forget(self, owner, task):
        self.handles.pop((owner, task), None)
        runs = [r for r in self.active.values() if r.owner == owner and r.id == task]
        for run in runs:
            run.supervisor_task.cancel()
        await asyncio.gather(*(r.supervisor_task for r in runs), return_exceptions=True)

    async def released(self, owner, device, token):
        for key, handle in list(self.handles.items()):
            if handle.owner == owner and handle.device == device and handle.request.session_id == token:
                await self.forget(*key)

    async def release_pending(self, owner, device, token):
        run = self.active.get(device)
        if run is not None and not run.committed and run.owner == owner and run.request.session_id == token:
            await self.forget(owner, run.id)

    @asynccontextmanager
    async def foreground(self, device):
        self.demand[device] = self.demand.get(device, 0) + 1
        try:
            run = self.active.get(device)
            if run is not None and not run.committed:
                run.supervisor_task.cancel()
                await asyncio.gather(run.supervisor_task, return_exceptions=True)
            yield
        finally:
            self.demand[device] -= 1

    def receipt(self, wake_id):
        row = self.db.execute('SELECT receipt FROM android_order_native_inputs WHERE wake_id=?', (wake_id,)).fetchone()
        return json.loads(row['receipt']) if row else None

    def status(self, owner, task):
        handle = self.handles.get((owner, task))
        budget = self.budgets.get((owner, task))
        run = self.agents.runs.get((owner, task))
        occupied = time.monotonic() - run.started if run and not getattr(run, '_supervision_accounted', False) else 0
        spent = budget['seconds_used'] + occupied if budget else 0
        observed = budget is not None and not budget.get('uncertain')
        return {'state': ('held' if handle.reason else 'retained') if handle else 'unavailable',
                'reason': handle.reason if handle else self.refusals.get((owner, task), 'no_live_admitted_handle'),
                'remaining_ms': max(0, round((handle.until - self.clock()) * 1000)) if handle else 0,
                'active_budget_status': 'observed' if observed else 'unconfirmed' if budget else 'unavailable',
                'active_spent_ms': round(spent * 1000) if observed else None,
                'active_remaining_ms': max(0, round((budget['seconds_limit'] - spent) * 1000)) if observed else None}

    async def tick(self):
        if self.closing:
            return
        handles = list(self.handles.items())
        offset = self.handle_cursor % len(handles) if handles else 0
        page = (handles + handles)[offset:offset + min(BATCH, len(handles))]
        self.handle_cursor = offset + BATCH
        for key, handle in page:
            try:
                self.authority(handle)
                refs = self.refs(handle)
                if not refs:
                    active = self.active.get(handle.device)
                    if active is not None and active.handle is handle and active.committed:
                        continue  # stop renewing, but let the admitted terminal metadata reply drain
                    self.handles.pop(key, None)
                    continue
                latest = self.wall() + handle.until - self.clock()
                if all(row['next_check_at'] is not None and row['next_check_at'] >= latest for row in refs):
                    handle.reason = 'next_check_beyond_authority'
                active = self.active.get(handle.device)
                if ((not self.agents.devices.locks[handle.device].locked() and not self.demand.get(handle.device))
                        or (active is not None and active.handle is handle and active.committed)):
                    self.touch(handle)
            except Refused as error:
                self.handles.pop(key, None)
                run = self.active.get(handle.device)
                if run is not None and run.handle is handle:
                    run.timing.request_stop('deadline' if error.code == 'supervision_budget_expired' else 'supervision_revoked')
                    run.supervisor_task.cancel()
        for key, budget in list(self.budgets.items())[:BATCH]:
            session = next((s for (owner, _), s in self.agents.sessions.items() if owner == key[0]
                and any(r['original_request_id'] == key[1] and r['state'] == 'waiting' for r in s.interactions.values())), None)
            if key not in self.handles and key not in self.agents.runs and session is None:
                self.budgets.pop(key, None)  # absence refuses continuations; it never reconstructs authority
        control = self.agents.order_control()
        if control is None:
            return
        # B's unchanged staging/coalescing logic, with bounded host scans rather than an unbounded timer walk.
        due = self.db.execute('''SELECT o.* FROM android_order_obligations o WHERE lifecycle='active'
            AND next_check_at<=? AND NOT EXISTS(SELECT 1 FROM android_order_wakes w WHERE w.owner=o.owner
            AND w.order_id=o.order_id AND w.kind='observation' AND w.state IN ('pending','sending','unknown','accepted'))
            ORDER BY next_check_at LIMIT ?''', (control.events.clock(), BATCH)).fetchall()
        with control.events.tx():
            for row in due:
                control.events._stage(dict(row))
        pending = self.db.execute('''SELECT rowid AS position,* FROM android_order_wakes WHERE rowid>?
            AND state='pending' AND kind='observation' AND target='codex' ORDER BY rowid LIMIT ?''',
            (self.cursor, BATCH)).fetchall()
        self.cursor = pending[-1]['position'] if pending else 0
        for row in pending:
            wake = dict(row)
            handle = self.handles.get((wake['owner'], wake['original_request_id']))
            reason = self.refusals.get((wake['owner'], wake['original_request_id']), 'lease_unavailable')
            if handle is not None:
                try:
                    self.ready(handle)
                    if self.agents.devices.locks[handle.device].locked():
                        raise Refused('device_busy')
                    await self.admit_due(handle, wake)
                    continue
                except Refused as error:
                    reason, handle.reason = error.code, error.code
            control.events.hold(wake['wake_id'], reason)

    async def admit_due(self, handle, wake):
        self.ready(handle)
        if (wake['owner'] != handle.owner or wake['original_request_id'] != handle.task
                or wake['device_id'] != handle.device or wake['native_thread_id'] != handle.thread
                or wake['kind'] != 'observation' or wake['target'] != 'codex'
                or not any(r['order_id'] == wake['order_id'] for r in self.refs(handle))):
            raise Refused('supervision_wake_mismatch')
        lock = self.agents.devices.locks[handle.device]
        if lock.locked() or handle.device in self.active:
            raise Refused('device_busy')
        await lock.acquire()
        run = None
        try:
            self.ready(handle)
            directory = Path(tempfile.mkdtemp(prefix='run-', dir=self.agents.root))
            directory.chmod(0o700)
            run = WakeRun(self, handle, wake, directory)
            self.active[handle.device] = run
            task = asyncio.create_task(run.supervise())
            run.supervisor_task = task
            self.agents.tasks.add(task)
            task.add_done_callback(self.agents.tasks.discard)
        except BaseException:
            if run is not None:
                self.agents.release(run)
            else:
                lock.release()
            raise

    async def close(self):
        self.closing = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.handles.clear()
        tasks = [r.supervisor_task for r in self.active.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.budgets.clear()


class WakeRun(PersistentRun):
    """One same-process input, no connect/start fallback and no reset of original task limits."""

    def __init__(self, supervision, handle, wake, directory):
        self.supervision, self.handle, self.wake = supervision, handle, wake
        self.committed, self.supervisor_task = False, None
        request = handle.request.model_copy(update={'max_actions': handle.request.max_actions - handle.commands})
        super().__init__(supervision.agents, handle.owner, request, 'codex', handle.config,
                         directory, SecretRun(supervision.agents.devices.secrets), (None, ''))
        self.session, self.process = handle.session, handle.process
        self.authorize = handle.authorize
        self.stderr = self.session.stderr
        self.engine_started = time.monotonic()
        self.input_admit = lambda: supervision.ready(handle, self)
        self.input_started = self.accepted
        self.control = supervision.agents.order_control()
        self.freeze_input()

    def freeze_input(self):
        envelope = self.control.events.envelope(self.wake['wake_id'])
        original = json.loads(self.agents.db.row(self.owner, self.id)['request'])
        body = {'wake_id': self.wake['wake_id'], 'obligation_ref': self.wake['order_id'],
                'original_request_id': self.id,
                'original_request': {key: original[key] for key in ('goal', 'owner_instruction', 'params', 'apps', 'outputs')},
                'observation': envelope['payload']}
        self.input_text = 'Order supervision wake (untrusted observation metadata):\n' + json.dumps(body, ensure_ascii=False)
        self.input_receipt = {key: envelope[key] for key in ('original_request_id', 'native_thread_id', 'native_turn_id', 'question_id')}
        for key in ('native_request_id', 'item_id', 'message_id', 'native_call_id', 'native_namespace', 'question_ids'):
            if key in envelope['payload']:
                self.input_receipt[key] = envelope['payload'][key]

    def commit(self):
        self.supervision.ready(self.handle, self)
        current = self.control.events.wake(self.wake['wake_id'])
        if any(current[key] != self.wake[key] for key in ('owner', 'device_id', 'original_request_id',
                'native_thread_id', 'native_turn_id', 'order_id', 'kind', 'target')):
            raise Refused('supervision_wake_mismatch')
        self.supervision.touch(self.handle, admission=self)
        if self.supervision.receipt(self.wake['wake_id']) or not self.control.events.claim(self.wake['wake_id']):
            raise Refused('supervision_wake_not_pending')
        self.committed = True
        self.freeze_input()  # coalesced observations may have advanced while selection awaited
        self.input_receipt.update(status='unknown', continuation_turn_id=None)
        self.agents.db.db.execute('INSERT INTO android_order_native_inputs VALUES(?,?,?,?,?,?)',
            (self.wake['wake_id'], self.owner, self.device, self.id,
             hashlib.sha256(self.input_text.encode()).hexdigest(), json.dumps(self.input_receipt)))
        self.agents.db.db.execute("UPDATE agent_runs SET state='running',ended=NULL,result=NULL WHERE owner=? AND id=?", (self.owner, self.id))
        self.agents.db.db.commit()
        self.agents.runs[(self.owner, self.id)] = self
        return self.input_text

    def accepted(self, turn_id):
        if (not isinstance(turn_id, str) or not turn_id or self.session.thread_id != self.handle.thread
                or self.session.last_turn_id != turn_id):
            raise OSError('supervision_turn_receipt_invalid')
        self.input_receipt.update(status='accepted', continuation_turn_id=turn_id)
        self.agents.db.db.execute('UPDATE android_order_native_inputs SET receipt=? WHERE wake_id=?',
                                  (json.dumps(self.input_receipt), self.wake['wake_id']))
        self.agents.db.db.commit()
        self.control.events.complete(self.wake['wake_id'], self.input_receipt)
        self.supervision.authority(self.handle)  # positive input receipt does not extend revoked/expired authority
        self.handle.turn = turn_id
        self.handle.reason = None

    async def supervise(self):
        try:
            async with asyncio.timeout(self.supervision.remaining(self)):
                status = await self.execute(self.session, before_submit=self.commit)
            await self.end('succeeded' if status == 'completed' and self.finished else 'failed')
        except asyncio.CancelledError:
            if self.committed:
                await self.end('lost' if self.supervision.closing else 'stopped')
            else:
                self.control.events.hold(self.wake['wake_id'], 'device_busy' if self.supervision.demand.get(self.device) else 'lease_unavailable')
        except TimeoutError:
            if self.committed:
                await self.end('timed_out', error='supervision_budget_expired')
            else:
                self.control.events.hold(self.wake['wake_id'], 'supervision_budget_expired')
        except (Refused, OSError, ValueError, KeyError, sqlite3.Error):
            if self.committed:
                await self.end('failed', error='supervision_input_unconfirmed')
            else:
                self.control.events.hold(self.wake['wake_id'], 'supervision_preflight_refused')
        finally:
            if self.committed:
                if self.state == 'running':
                    await self.end('failed', error='supervision_input_unconfirmed')
                self.control.events.unknown(self.wake['wake_id'])  # only sending can become unknown, never accepted
            else:
                self.agents.release(self)
            self.supervision.account(self)  # even unsubmitted preflight occupied original task work
            if self.committed and self.state != 'succeeded':
                self.supervision.handles.pop((self.owner, self.id), None)  # no idle reauthorization after an uncertain/failed turn
            if self.supervision.active.get(self.device) is self:
                self.supervision.active.pop(self.device, None)
