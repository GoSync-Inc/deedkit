"""One native app-server process/thread per owner and phone; MCP requests are turns, not sessions."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import time

from . import engines
from .agent_bare import BareRun, PERSISTENT_MODEL, PERSISTENT_TIER
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
        self.policy_proven, self.turn_policy_proven = False, False
        self.selection, self.version_verified = {}, False
        self.settings_applied = None
        self.disconnected = False
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

    def finish_questions(self, state, *, include_async=True, request_id=None):
        changed = False
        for row in self.interactions.values():
            if (row['kind'] in (('question', 'async_question') if include_async else ('question',))
                    and row['state'] == 'waiting' and (request_id is None or row['original_request_id'] == request_id)):
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
                'native_request_id', 'item_id', 'question_ids', 'message_id', 'continuation_turn_id') if key in row} | {
                    'status': status, 'native_state': row['state'], 'submission_intent': 'digest' in row}

    def receipts(self, request_id):
        return [self.receipt(row) for row in self.interactions.values() if row['original_request_id'] == request_id]

    def unresolved_input(self, request_id, turn_id):
        if any(r['kind'] in ('steer', 'async_question') and r['original_request_id'] == request_id
               and r['native_turn_id'] == turn_id and r.get('status') == 'unknown' for r in self.interactions.values()):
            raise Refused('native_input_unresolved')

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
        # 0.166 MCP approvals share this method, but native refuses them before dispatch
        # under effective never (core/src/mcp_tool_call.rs1655). Method/labels alone prove nothing.
        if not self.turn_policy_proven:
            raise Refused('native_question_policy_unproven')
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

    def async_question(self, params):
        item = params.get('item') or {}
        questions = item.get('questions')
        if (not self.turn_policy_proven or not isinstance(item.get('id'), str)
                or not isinstance(questions, list) or not questions or not all(
                    isinstance(q, dict) and isinstance(q.get('title'), str) and q['title'] for q in questions)):
            return
        key = self.interaction_key('async_question', self.request_id, self.thread_id, self.turn_id, item['id'])
        if key not in self.interactions:
            self.interactions[key] = {'kind': 'async_question', 'original_request_id': self.request_id,
                'native_thread_id': self.thread_id, 'native_turn_id': self.turn_id, 'item_id': item['id'],
                'question_count': len(questions), 'state': 'waiting'}
            self.save_interactions()  # native async questions have no JSONRPC or per-question IDs

    async def reply_async(self, *, request_id, thread_id, turn_id, item_id, message_id, text, admit, resume):
        async with self.interaction_lock:
            admit(False)
            key = self.interaction_key('async_question', request_id, thread_id, turn_id, item_id)
            row = self.interactions.get(key)
            if row is None:
                raise Refused('native_question_mismatch')
            digest = hashlib.sha256(text.encode()).hexdigest()
            if 'digest' in row:
                if row['digest'] != digest or row['message_id'] != message_id:
                    raise Refused('native_reply_conflict')
                return self.receipt(row)
            if row['state'] != 'waiting':
                raise Refused('question_expired')
            if not self.policy_proven:
                raise Refused('native_question_policy_unproven')
            if self.thread_id != thread_id or self.last_turn_id != turn_id:
                raise Refused('native_turn_mismatch')
            if self.disconnected or self.process is None or self.process.returncode is not None or self.process.stdin.is_closing():
                raise Refused('native_session_disconnected')
            self.unresolved_input(request_id, turn_id)

            def commit():
                row.update(digest=digest, message_id=message_id, state='unknown', status='unknown')
                self.save_interactions()

            def started(new_turn_id):
                row.update(state='accepted', status='accepted', continuation_turn_id=new_turn_id)
                self.save_interactions()  # turn/start receipt acknowledges input, never goal/action success

            def refused():
                row.update(state='refused', status='refused')
                self.save_interactions()  # admission failed before a native write; retain the original intent

            if self.events is None:
                await resume(text, commit, started, refused)
            else:
                self.active(request_id, thread_id, turn_id)
                admit(True)
                commit()
                try:
                    result = await self.rpc('turn/steer', {'threadId': thread_id, 'expectedTurnId': turn_id,
                        'clientUserMessageId': message_id, 'input': [{'type': 'text', 'text': text}]})
                    if result.get('turnId') == turn_id:
                        row.update(state='accepted', status='accepted')
                        self.save_interactions()
                except (OSError, TimeoutError):
                    pass
            return self.receipt(row)

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
            if not self.turn_policy_proven:
                raise Refused('native_question_policy_unproven')
            if admit:
                admit(True)
            if self.disconnected or self.process.returncode is not None or self.process.stdin.is_closing():
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
            if self.disconnected or self.process.returncode is not None or self.process.stdin.is_closing():
                raise Refused('native_session_disconnected')
            self.unresolved_input(request_id, turn_id)  # changing the input route cannot bypass an uncertain write
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
        pinned = (self.config.version == '0.166.0' and await asyncio.to_thread(
            engines.installed_version, self.config.binary) == '0.166.0')
        if not pinned:
            raise Refused('native_version_unsupported')
        self.version_verified = True
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
            await self.rpc('initialize', {'clientInfo': {'name': 'deedkit_android', 'version': '0.1.0'},
                                          'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized', 'params': {}})
            params = {'cwd': str(self.dir), 'model': self.config.model or PERSISTENT_MODEL,
                      'sandbox': 'workspace-write', 'approvalPolicy': 'never'}
            saved = self.dir / 'thread.json'
            if saved.exists():
                self.thread_id = json.loads(saved.read_text())['thread_id']
                response = await self.rpc('thread/resume', {**params, 'threadId': self.thread_id})
            else:
                response = await self.rpc('thread/start', {**params, 'ephemeral': False})
            self.thread_id = response['thread']['id']
            self.policy_proven = pinned and response.get('approvalPolicy') == 'never'
            saved.write_text(json.dumps({'thread_id': self.thread_id}))
            saved.chmod(0o600)
        except BaseException:
            await self.close()
            raise

    def send(self, message):
        if self.disconnected or self.process.returncode is not None:
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
                                self.turn_policy_proven = self.policy_proven
                            future.set_result(result)
                elif 'id' in message:
                    try:
                        if message.get('method') != 'item/tool/requestUserInput':
                            raise Refused('unsupported_native_request')
                        self.question(message)
                    except Refused as error:
                        # Approval/elicitation/secret requests never become ordinary clarification answers.
                        self.send({'id': message['id'], 'error': {'code': -32601, 'message': error.code}})
                elif message.get('method') == 'thread/settings/updated':
                    params = message.get('params') or {}
                    if params.get('threadId') != self.thread_id:
                        continue
                    if (params.get('threadSettings') or {}).get('approvalPolicy') != 'never':
                        self.policy_proven, self.turn_policy_proven = False, False
                    if self.settings_applied is not None and not self.settings_applied.done():
                        self.settings_applied.set_result(None)  # wake hint; a fresh readback must still confirm values
                    if self.events is not None:
                        self.events.put_nowait(message)
                elif self.events is not None:
                    params = message.get('params') or {}
                    if params.get('threadId') != self.thread_id:
                        continue
                    turn = params.get('turnId') or (params.get('turn') or {}).get('id')
                    if turn and (self.turn_id is None or turn != self.turn_id):
                        continue  # late/unrelated turn events cannot complete or mutate this turn
                    if message.get('method') == 'turn/started':
                        self.turn_id = params['turn']['id']
                    if message.get('method') == 'model/rerouted':
                        self.stopping = True  # refuse fresh owner input while the turn is being cancelled
                    if message.get('method') == 'item/completed':
                        item = params.get('item') or {}
                        if item.get('type') == 'agentMessage' and item.get('delivery') == 'async':
                            self.async_question(params)
                    if message.get('method') == 'turn/completed':
                        self.done.set()
                        self.finish_questions('expired', include_async=False)
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
            self.disconnected = True
            if self.settings_applied is not None and not self.settings_applied.done():
                self.settings_applied.set_result(None)  # wake only; the required readback will fail closed
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

    async def select(self, config):
        """Validate against this process's native catalog, then read effective settings on the SAME thread."""
        if not self.version_verified:
            raise Refused('native_version_unsupported')
        requested = {'model': config.model or PERSISTENT_MODEL, 'effort': config.reasoning_effort,
                     'service_tier': config.service_tier or PERSISTENT_TIER}
        self.selection = {'requested': requested, 'effective': None, 'configuration_status': 'unconfirmed',
                          'provider_service_tier': None, 'provider_tier_observed': False,
                          'native_thread_id': self.thread_id, 'source': None, 'timings_ms': {}}
        phase = time.monotonic()
        cursor, seen, model = None, set(), None
        while True:
            page = await self.rpc('model/list', {'limit': 100, 'includeHidden': True, **({'cursor': cursor} if cursor else {})})
            model = next((row for row in page.get('data', []) if row.get('model') == requested['model']), None)
            cursor = page.get('nextCursor')
            if model or not cursor:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise Refused('native_catalog_unconfirmed')
            seen.add(cursor)
        self.selection['timings_ms']['catalog'] = round((time.monotonic() - phase) * 1000, 2)
        if model is None:
            raise Refused('native_model_unsupported')
        if requested['effort'] not in [row.get('reasoningEffort') for row in model.get('supportedReasoningEfforts', [])]:
            raise Refused('native_effort_unsupported')
        if requested['service_tier'] != 'default' and requested['service_tier'] not in [
                row.get('id') for row in model.get('serviceTiers', [])]:
            raise Refused('native_service_tier_unsupported')
        expected = {**requested, 'service_tier': None if requested['service_tier'] == 'default' else requested['service_tier']}
        self.settings_applied = asyncio.get_running_loop().create_future()
        try:
            phase = time.monotonic()
            await self.rpc('thread/settings/update', {'threadId': self.thread_id, 'model': requested['model'],
                           'effort': requested['effort'], 'serviceTier': requested['service_tier'], 'approvalPolicy': 'never'})
            self.selection['timings_ms']['settings'] = round((time.monotonic() - phase) * 1000, 2)
            for attempt in range(2):
                phase = time.monotonic()
                receipt = await self.rpc('thread/resume', {'threadId': self.thread_id, 'excludeTurns': True})
                elapsed = round((time.monotonic() - phase) * 1000, 2)
                self.selection['timings_ms']['receipt'] = self.selection['timings_ms'].get('receipt', 0) + elapsed
                effective = {'model': receipt.get('model'), 'effort': receipt.get('reasoningEffort'),
                             'service_tier': receipt.get('serviceTier')}
                self.selection.update(effective=effective, source='thread/resume')
                self.policy_proven = receipt.get('approvalPolicy') == 'never'
                if (receipt.get('thread') or {}).get('id') != self.thread_id or not self.policy_proven:
                    raise Refused('native_selection_unconfirmed')
                if effective == expected:
                    break
                if attempt:
                    raise Refused('native_selection_unconfirmed')
                phase = time.monotonic()
                async with asyncio.timeout(30):
                    await self.settings_applied
                self.selection['timings_ms']['applied_wait'] = round((time.monotonic() - phase) * 1000, 2)
        except TimeoutError:
            raise Refused('native_selection_unconfirmed') from None
        finally:
            if not self.settings_applied.done():
                self.settings_applied.cancel()
            self.settings_applied = None
        self.selection['configuration_status'] = 'confirmed'
        return requested

    async def turn(self, prompt, config, consume, *, request_id=None, on_started=None, admit=None):
        async with self.lock:
            if admit:
                admit()
            selected = await self.select(config)
            if admit:
                admit()  # discovery/settings reads may await; recheck immediately before goal dispatch
            self.finish_questions('expired')  # a later turn cannot answer an older unresolved question
            self.events = asyncio.Queue()
            self.turn_id = None
            self.turn_policy_proven = False
            self.request_id, self.stopping = request_id, False
            self.done.clear()
            try:
                response = await self.rpc('turn/start', {
                    'threadId': self.thread_id, 'input': [{'type': 'text', 'text': prompt}],
                    'model': selected['model'], 'effort': selected['effort'], 'serviceTier': selected['service_tier'],
                    'approvalPolicy': 'never',
                    'sandboxPolicy': {'type': 'workspaceWrite', 'writableRoots': [str(self.dir)],
                                      'networkAccess': True}})
                self.turn_id = response['turn']['id']
                self.last_turn_id = self.turn_id
                if on_started:
                    on_started(self.turn_id)
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
        if self.events is None or self.done.is_set():
            return  # normal completion leaves async questions pending on this conversation
        if self.disconnected:
            await self.close()
            return
        self.stopping = True
        try:
            self.finish_questions('cancelled')
        except OSError:
            pass
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
        self.input_text, self.input_started, self.input_admit, self.input_refused = None, None, None, None

    def skill_files(self):
        return {}

    async def supervise(self):
        heartbeat = asyncio.create_task(self.keep_lease())
        try:
            async with asyncio.timeout(self.request.max_minutes * MINUTE_S):
                key = (self.owner, self.device)
                session = self.agents.sessions.get(key)
                if self.input_text is not None and (session is None or session.process is None or session.process.returncode is not None):
                    raise Refused('native_session_disconnected')  # a queued answer cannot start a replacement process
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

                def admit():
                    if self.state != 'running' or not self.agents.devices.locks[self.device].locked():
                        raise Refused('native_turn_mismatch')
                    current = self.agents.read_config().agent
                    if not current.enabled or current.guarded or not current.persistent:
                        raise Refused('agent_disabled')
                    if self.authorize:
                        principal = self.authorize()
                        if principal.id != self.owner or self.device not in principal.devices:
                            raise Refused('not_found', 404)
                        if 'android:control' not in principal.permissions:
                            raise Refused('forbidden', 403)
                    self.agents.store.check_reservation(self.owner, self.device, self.request.session_id)
                    if self.input_admit:
                        self.input_admit()

                status = await session.turn(self.input_text if self.input_text is not None else self.prompt(),
                                            self.config, self.consume, request_id=self.id, on_started=self.input_started,
                                            admit=admit)
                if self.state == 'running':
                    await self.end('succeeded' if status == 'completed' and self.finished else 'failed',
                                   exit_code=0 if status == 'completed' else 1)
        except TimeoutError:
            await self.end('timed_out')
        except Refused as error:
            if self.input_refused:
                self.input_refused()
            await self.end('failed', error=error.code)
        except (OSError, ValueError, KeyError):
            self.stream.errors.append('native_session_disconnected_or_start_failed')
            if self.session:
                self.stream.errors.extend(self.session.errors)
                if self.session.disconnected:
                    await self.session.close()
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
            await self.event('selection', **self.session.selection)
            await self.event('turn', phase='started', native_thread_id=params['threadId'], native_turn_id=params['turn']['id'])
        elif method == 'model/rerouted':
            self.session.selection.update(configuration_status='rerouted', rerouted_to=params.get('toModel'))
            self.stream.model = params.get('toModel')
            await self.event('selection', **self.session.selection)
            await self.end('failed', error='native_model_rerouted')
        elif method == 'item/tool/requestUserInput':
            await self.event('question', native_request_id=event['id'], native_thread_id=params['threadId'],
                             native_turn_id=params['turnId'], item_id=params['itemId'],
                             questions=params['questions'], is_blocking=params['isBlocking'], source='rpc')
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
            key = self.session.interaction_key('async_question', self.id, params['threadId'], params.get('turnId'), item['id'])
            if item.get('delivery') == 'async' and key in self.session.interactions:
                await self.event('question', source='async', native_thread_id=params['threadId'],
                                 native_turn_id=params['turnId'], item_id=item['id'], questions=item['questions'])
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
        result['native_selection'] = dict(self.session.selection) if self.session else {}
        result['skills_updated'] = []
        if self.session and self.session.process:
            result['native_session'] = {'thread_id': self.session.thread_id, 'pid': self.session.process.pid,
                                        'turn_id': self.session.turn_id or self.session.last_turn_id, 'persistent': True}
        return result
