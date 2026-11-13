"""One native app-server process/thread per owner and phone; MCP requests are turns, not sessions."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import time

from . import engines
from .agent_bare import BareRun
from .agent_run import MAX_NOTES, MINUTE_S
from .config import Refused


class CodexSession:
    def __init__(self, directory, config, host, device):
        self.dir, self.config, self.host, self.device = directory, config, host, device
        self.process, self.reader, self.stderr_reader = None, None, None
        self.pending, self.serial = {}, 0
        self.thread_id, self.turn_id, self.events = None, None, None
        self.last_turn_id = None
        self.done, self.lock = asyncio.Event(), asyncio.Lock()
        self.stderr = bytearray()
        self.errors = []
        self.request_id, self.stopping = None, False
        self.interactions, self.interaction_lock = {}, asyncio.Lock()
        self.journal = directory.parent / (directory.name + '.dialogue.json')  # outside native writable cwd

    def save_interactions(self):
        """Metadata only; commit the no-replay intent before writing anything to native stdin."""
        path = self.journal
        fd = os.open(path.with_suffix('.tmp'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(self.interactions, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path.with_suffix('.tmp'), path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def finish_questions(self, state):
        changed = False
        for row in self.interactions.values():
            if row['kind'] == 'question' and row['state'] == 'waiting':
                row['state'], changed = state, True
        if changed:
            self.save_interactions()

    @staticmethod
    def interaction_key(kind, request_id, thread_id, turn_id, ident):
        # JSON distinguishes a numeric native RPC id from the same characters in a string.
        return hashlib.sha256(json.dumps([kind, request_id, thread_id, turn_id, ident]).encode()).hexdigest()

    @staticmethod
    def receipt(row):
        status = row.get('status', 'pending' if row['state'] == 'waiting' else 'refused')
        return {key: row[key] for key in ('original_request_id', 'native_thread_id', 'native_turn_id',
                'native_request_id', 'item_id', 'question_ids', 'message_id') if key in row} | {
                    'status': status, 'native_state': row['state'], 'submission_intent': 'digest' in row}

    def receipts(self, request_id):
        return [self.receipt(row) for row in self.interactions.values() if row['original_request_id'] == request_id]

    def load_interactions(self):
        if self.journal.exists():
            try:
                if self.journal.stat().st_size > 1024 * 1024:
                    raise ValueError()
                self.interactions = json.loads(self.journal.read_text())
                self.finish_questions('expired')  # a detached/restarted adapter never replays old questions
            except (ValueError, KeyError, TypeError, AttributeError):
                raise OSError('native_dialogue_journal_invalid') from None

    def active(self, request_id, thread_id, turn_id):
        if (request_id != self.request_id or thread_id != self.thread_id or turn_id != self.turn_id
                or self.events is None or self.done.is_set() or self.stopping):
            raise Refused('native_turn_mismatch')

    def question(self, message):
        params, ident = message.get('params') or {}, message['id']
        self.active(self.request_id, params.get('threadId'), params.get('turnId'))
        questions = params.get('questions')
        if (type(ident) not in (str, int) or not isinstance(params.get('itemId'), str)
                or type(params.get('isBlocking')) is not bool or not isinstance(questions, list) or not questions
                or not all(isinstance(q, dict) and isinstance(q.get('id'), str) and q['id']
                           and isinstance(q.get('question'), str) for q in questions)):
            raise Refused('invalid_native_question')
        ids = [q['id'] for q in questions]
        if len(set(ids)) != len(ids):
            raise Refused('invalid_native_question')
        if any(q.get('isSecret') for q in questions):
            raise Refused('protected_input_unavailable')
        delay = params.get('autoResolutionMs')
        if delay is not None and (type(delay) is not int or delay < 0):
            raise Refused('invalid_native_question')
        key = self.interaction_key('question', self.request_id, self.thread_id, self.turn_id, ident)
        if key in self.interactions:
            return  # keep the original pending/tombstone; never answer a duplicate native request
        self.interactions[key] = {'kind': 'question', 'original_request_id': self.request_id,
            'native_thread_id': self.thread_id, 'native_turn_id': self.turn_id, 'native_request_id': ident,
            'item_id': params['itemId'], 'question_ids': ids, 'state': 'waiting',
            'expires_at': time.time() + delay / 1000 if delay is not None else None}
        self.save_interactions()
        self.events.put_nowait(message)

    async def reply(self, *, request_id, thread_id, turn_id, native_request_id, item_id, answers, admit=None):
        """Explicit ordinary answers only. Submitted bytes have no positive native contents-acceptance ACK."""
        async with self.interaction_lock:
            if admit:
                admit(False)
            if type(native_request_id) not in (str, int) or (self.thread_id is not None and thread_id != self.thread_id):
                raise Refused('native_question_mismatch')
            key = self.interaction_key('question', request_id, thread_id, turn_id, native_request_id)
            row = self.interactions.get(key)
            if row is None or item_id != row['item_id']:
                raise Refused('native_question_mismatch')
            if (not isinstance(answers, dict) or set(answers) != set(row['question_ids']) or not all(
                    isinstance(values, list) and all(isinstance(text, str) for text in values) for values in answers.values())):
                raise Refused('native_answers_mismatch')
            digest = hashlib.sha256(json.dumps(answers, sort_keys=True).encode()).hexdigest()
            if 'digest' in row:
                if row['digest'] != digest:
                    raise Refused('native_reply_conflict')
                return self.receipt(row)  # submitted/unknown is never sent again, including after restart
            if row['state'] != 'waiting' or (row['expires_at'] is not None and time.time() >= row['expires_at']):
                row['state'] = 'expired' if row['state'] == 'waiting' else row['state']
                self.save_interactions()
                raise Refused('question_expired')
            self.active(request_id, thread_id, turn_id)
            if admit:
                admit(True)
            if self.process.returncode is not None or self.process.stdin.is_closing():
                raise Refused('native_session_disconnected')  # proven refusal before any write
            row.update(digest=digest, state='unknown', status='unknown')
            self.save_interactions()
            try:
                self.send({'id': native_request_id, 'result': {'answers': {
                    name: {'answers': values} for name, values in answers.items()}}})
                await self.process.stdin.drain()
                if row['state'] == 'unknown':
                    row['state'] = 'submitted'
                self.save_interactions()
            except (OSError, ConnectionError):
                pass  # committed intent survives an uncertain write; no auto retry or invented ACK
            return self.receipt(row)

    async def steer(self, *, request_id, thread_id, turn_id, message_id, text, admit=None):
        async with self.interaction_lock:
            if admit:
                admit(False)
            if not isinstance(message_id, str) or not message_id or not isinstance(text, str) or not text:
                raise Refused('invalid_native_input')
            key = self.interaction_key('steer', request_id, thread_id, turn_id, message_id)
            digest = hashlib.sha256(text.encode()).hexdigest()
            row = self.interactions.get(key)
            if row is not None:
                if row['digest'] != digest:
                    raise Refused('native_input_conflict')
                return self.receipt(row)
            self.active(request_id, thread_id, turn_id)
            if admit:
                admit(True)
            if self.process.returncode is not None or self.process.stdin.is_closing():
                raise Refused('native_session_disconnected')
            if any(r['kind'] == 'steer' and r['original_request_id'] == request_id
                   and r['native_turn_id'] == turn_id and r.get('status') == 'unknown'
                   for r in self.interactions.values()):
                raise Refused('native_input_unresolved')  # a fresh message id cannot bypass an uncertain write
            row = {'kind': 'steer', 'original_request_id': request_id, 'native_thread_id': thread_id,
                   'native_turn_id': turn_id, 'message_id': message_id, 'digest': digest,
                   'state': 'unknown', 'status': 'unknown'}
            self.interactions[key] = row
            self.save_interactions()
            try:
                result = await self.rpc('turn/steer', {'threadId': thread_id, 'expectedTurnId': turn_id,
                    'clientUserMessageId': message_id, 'input': [{'type': 'text', 'text': text}]})
                if result.get('turnId') == turn_id:
                    row.update(state='accepted', status='accepted')
                    self.save_interactions()
            except (OSError, TimeoutError):
                pass
            return self.receipt(row)

    async def connect(self):
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.load_interactions()
        # App-server has no -C argument carrying our marker token. A verified executable link
        # keeps that token in argv so restart recovery can identify the group without trusting a PID.
        binary = self.dir / 'codex'
        if binary.is_symlink():
            if binary.resolve() != Path(self.config.binary).resolve():
                raise OSError('native_binary_link_changed')
        elif binary.exists():
            raise OSError('native_binary_link_changed')
        else:
            binary.symlink_to(Path(self.config.binary).resolve())
        env = engines.environment('codex', self.config, self.dir)
        env['PATH'] = str(Path(self.host.adb).parent) + os.pathsep + env['PATH']
        env['ANDROID_SERIAL'] = self.host.devices[self.device]
        for name in ('TMPDIR', 'LANG'):
            if name in os.environ:
                env[name] = os.environ[name]
        self.process = await asyncio.create_subprocess_exec(
            str(binary), 'app-server', '--strict-config',
            '-c', 'sandbox_mode="workspace-write"', '-c', 'approval_policy="never"',
            '-c', 'sandbox_workspace_write.network_access=true',
            '-c', 'skills.bundled.enabled=false', '-c', 'features.skip_host_skill_discovery=true',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env, cwd=self.dir, start_new_session=True, limit=4 * 1024 * 1024)
        marker = self.dir / 'marker.json'
        marker.write_text(json.dumps({'pid': self.process.pid, 'token': self.dir.name}))
        marker.chmod(0o600)
        self.reader = asyncio.create_task(self.read())
        self.stderr_reader = asyncio.create_task(self.read_stderr())
        try:
            await self.rpc('initialize', {'clientInfo': {'name': 'deedkit_android', 'version': '0.1.0'}})
            self.send({'method': 'initialized', 'params': {}})
            params = {'cwd': str(self.dir), 'model': self.config.model,
                      'sandbox': 'workspace-write', 'approvalPolicy': 'never'}
            saved = self.dir / 'thread.json'
            if saved.exists():
                self.thread_id = json.loads(saved.read_text())['thread_id']
                response = await self.rpc('thread/resume', {**params, 'threadId': self.thread_id})
            else:
                response = await self.rpc('thread/start', {**params, 'ephemeral': False})
            self.thread_id = response['thread']['id']
            saved.write_text(json.dumps({'thread_id': self.thread_id}))
            saved.chmod(0o600)
        except BaseException:
            await self.close()
            raise

    def send(self, message):
        if self.process.returncode is not None:
            raise OSError('native_session_disconnected')
        self.process.stdin.write(json.dumps(message).encode() + b'\n')

    async def rpc(self, method, params):
        self.serial += 1
        ident = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            self.send({'id': ident, 'method': method, 'params': params})
            await self.process.stdin.drain()
            async with asyncio.timeout(30):
                return await future
        finally:
            self.pending.pop(ident, None)

    async def read(self):
        try:
            async for line in self.process.stdout:
                message = json.loads(line)
                if 'id' in message and 'method' not in message:
                    future = self.pending.get(message['id']) if type(message['id']) is int else None
                    if future is not None and not future.done():
                        if 'error' in message:
                            self.errors.append(str(message['error'].get('message', 'native_rpc_error'))[:1000])
                            future.set_exception(OSError('native_rpc_error'))
                        else:
                            result = message.get('result', {})
                            if self.events is not None and self.turn_id is None and isinstance(result.get('turn'), dict):
                                self.turn_id = result['turn']['id']
                            future.set_result(result)
                elif 'id' in message:
                    try:
                        if message.get('method') != 'item/tool/requestUserInput':
                            raise Refused('unsupported_native_request')
                        self.question(message)
                    except Refused as error:
                        # Approval/elicitation/secret requests never become ordinary clarification answers.
                        self.send({'id': message['id'], 'error': {'code': -32601, 'message': error.code}})
                elif self.events is not None:
                    params = message.get('params') or {}
                    if params.get('threadId') != self.thread_id:
                        continue
                    turn = params.get('turnId') or (params.get('turn') or {}).get('id')
                    if turn and (self.turn_id is None or turn != self.turn_id):
                        continue  # late/unrelated turn events cannot complete or mutate this turn
                    if message.get('method') == 'turn/started':
                        self.turn_id = params['turn']['id']
                    if message.get('method') == 'turn/completed':
                        self.done.set()
                        self.finish_questions('expired')
                    if message.get('method') == 'serverRequest/resolved':
                        ident = params.get('requestId')
                        for row in self.interactions.values():
                            if (row['kind'] == 'question' and row['native_turn_id'] == self.turn_id
                                    and type(ident) is type(row['native_request_id']) and ident == row['native_request_id']):
                                if row['state'] == 'waiting':
                                    row['state'] = 'expired'
                                elif row['state'] in ('unknown', 'submitted'):
                                    row['state'] = 'resolved'
                        self.save_interactions()  # cleared is not proof that the submitted answer contents were used
                    self.events.put_nowait(message)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        finally:
            try:
                self.finish_questions('expired')
            except OSError:
                pass
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(OSError('native_session_disconnected'))
            if self.events is not None:
                self.events.put_nowait(None)

    async def read_stderr(self):
        while chunk := await self.process.stderr.read(4096):
            self.stderr.extend(chunk)
            del self.stderr[:-16384]

    async def turn(self, prompt, config, consume, *, request_id=None):
        async with self.lock:
            self.events = asyncio.Queue()
            self.turn_id = None
            self.request_id, self.stopping = request_id, False
            self.done.clear()
            try:
                response = await self.rpc('turn/start', {
                    'threadId': self.thread_id, 'input': [{'type': 'text', 'text': prompt}],
                    'model': config.model, 'effort': config.reasoning_effort,
                    'approvalPolicy': 'never',
                    'sandboxPolicy': {'type': 'workspaceWrite', 'writableRoots': [str(self.dir)],
                                      'networkAccess': True}})
                self.turn_id = response['turn']['id']
                self.last_turn_id = self.turn_id
                while True:
                    event = await self.events.get()
                    if event is None:
                        raise OSError('native_session_disconnected')
                    await consume(event)
                    if event.get('method') == 'turn/completed':
                        return event['params']['turn']['status']
            except BaseException:
                await self.interrupt()
                raise
            finally:
                self.events = None
                self.turn_id = None
                self.request_id = None

    async def interrupt(self):
        self.stopping = True
        try:
            self.finish_questions('cancelled')
        except OSError:
            pass
        if self.events is None or self.done.is_set():
            return
        try:
            async with asyncio.timeout(10):
                while self.turn_id is None:
                    await asyncio.sleep(.01)
                await self.rpc('turn/interrupt', {'threadId': self.thread_id, 'turnId': self.turn_id})
                await self.done.wait()
        except (OSError, TimeoutError):
            await self.close()  # uncertain cancellation must stop commands before releasing the device

    async def close(self):
        self.stopping = True
        try:
            self.finish_questions('cancelled')
        except OSError:
            pass
        if self.process is not None:
            await engines.kill_group(self.process)
        for task in (self.reader, self.stderr_reader):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*[t for t in (self.reader, self.stderr_reader) if t], return_exceptions=True)
        (self.dir / 'marker.json').unlink(missing_ok=True)


class PersistentRun(BareRun):
    def __init__(self, *args):
        super().__init__(*args)
        self.session = None

    def skill_files(self):
        return {}

    async def supervise(self):
        heartbeat = asyncio.create_task(self.keep_lease())
        try:
            async with asyncio.timeout(self.request.max_minutes * MINUTE_S):
                key = (self.owner, self.device)
                session = self.agents.sessions.get(key)
                if session is None or session.process.returncode is not None:
                    if session is not None:
                        await session.close()
                    digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()
                    directory = self.agents.root / 'sessions' / ('run-live-' + digest)
                    session = CodexSession(directory, self.config, self.agents.read_config(), self.device)
                    self.session = session
                    await session.connect()
                    self.agents.sessions[key] = session
                self.session, self.process = session, session.process
                self.stderr = session.stderr
                self.stderr.clear()
                if self.state != 'running':
                    return
                self.engine_started = time.monotonic()
                status = await session.turn(self.prompt(), self.config, self.consume, request_id=self.id)
                if self.state == 'running':
                    await self.end('succeeded' if status == 'completed' and self.finished else 'failed',
                                   exit_code=0 if status == 'completed' else 1)
        except TimeoutError:
            await self.end('timed_out')
        except (OSError, ValueError, KeyError):
            self.stream.errors.append('native_session_disconnected_or_start_failed')
            if self.session:
                self.stream.errors.extend(self.session.errors)
            await self.end('failed', error='native_session_failed')
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def stop_engine(self):
        await self.session.interrupt()

    async def consume(self, event):
        method, params = event.get('method'), event.get('params') or {}
        item = params.get('item') or {}
        if method == 'turn/started':
            await self.event('turn', phase='started', native_thread_id=params['threadId'], native_turn_id=params['turn']['id'])
        elif method == 'item/tool/requestUserInput':
            await self.event('question', native_request_id=event['id'], native_thread_id=params['threadId'],
                             native_turn_id=params['turnId'], item_id=params['itemId'],
                             questions=params['questions'], is_blocking=params['isBlocking'])
        elif method == 'serverRequest/resolved':
            await self.event('question_resolved', native_thread_id=params['threadId'], native_request_id=params['requestId'])
        elif method in ('item/started', 'item/completed') and item.get('type') == 'commandExecution':
            ident = item['id']
            if method == 'item/started':
                self.commands_started += 1
                self.command_starts[ident] = time.monotonic()
                if self.commands_started > self.request.max_actions:
                    await self.end('action_cap')
            else:
                elapsed = round((time.monotonic() - self.command_starts.pop(ident, time.monotonic())) * 1000, 2)
                self.device_ms += elapsed
                entry = {'tool': 'command', 'command': str(item.get('command', ''))[:300],
                         'exit_code': item.get('exitCode'), 'elapsed_ms': elapsed}
                self.actions.append(entry)
                await self.event('action', **{**entry, 'command': entry['command'][:120]})
        elif method == 'item/completed' and item.get('type') == 'agentMessage':
            text = item.get('text', '')
            if item.get('phase') in (None, 'final_answer') and text.strip():
                self.finished = {'summary': text, 'data': {}}
            if self.notes < MAX_NOTES:
                self.notes += 1
                await self.event('note', text=text[:300])
        elif method == 'thread/tokenUsage/updated':
            usage = (params.get('tokenUsage') or {}).get('last') or {}
            self.stream.final = {target: usage[source] for source, target in (
                ('inputTokens', 'input_tokens'), ('cachedInputTokens', 'cached_input_tokens'),
                ('outputTokens', 'output_tokens')) if type(usage.get(source)) is int}
        elif method == 'turn/completed':
            await self.event('turn', phase='completed', native_thread_id=params['threadId'],
                             native_turn_id=params['turn']['id'], status=params['turn']['status'])
            if params['turn'].get('error'):
                self.stream.errors.append(str(params['turn']['error'].get('message', 'native_turn_failed')))

    async def final(self, state, extra):
        # No skill directory is exposed or inspected; the native thread owns conversational history.
        result = await super().final(state, extra)
        result['skills_updated'] = []
        if self.session and self.session.process:
            result['native_session'] = {'thread_id': self.session.thread_id, 'pid': self.session.process.pid,
                                        'turn_id': self.session.turn_id or self.session.last_turn_id, 'persistent': True}
        return result
