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


class CodexSession:
    def __init__(self, directory, config, host, device):
        self.dir, self.config, self.host, self.device = directory, config, host, device
        self.process, self.reader, self.stderr_reader = None, None, None
        self.pending, self.serial = {}, 0
        self.thread_id, self.turn_id, self.events = None, None, None
        self.done, self.lock = asyncio.Event(), asyncio.Lock()
        self.stderr = bytearray()
        self.errors = []

    async def connect(self):
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        env = engines.environment('codex', self.config, self.dir)
        env['PATH'] = str(Path(self.host.adb).parent) + os.pathsep + env['PATH']
        env['ANDROID_SERIAL'] = self.host.devices[self.device]
        for name in ('TMPDIR', 'LANG'):
            if name in os.environ:
                env[name] = os.environ[name]
        self.process = await asyncio.create_subprocess_exec(
            self.config.binary, 'app-server', '--strict-config',
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
                    future = self.pending.get(message['id'])
                    if future is not None and not future.done():
                        if 'error' in message:
                            self.errors.append(str(message['error'].get('message', 'native_rpc_error'))[:1000])
                            future.set_exception(OSError('native_rpc_error'))
                        else:
                            future.set_result(message.get('result', {}))
                elif 'id' in message:
                    # No implicit approval, elicitation answer or second client is introduced.
                    self.send({'id': message['id'], 'error': {'code': -32601, 'message': 'Unsupported request'}})
                elif self.events is not None:
                    params = message.get('params') or {}
                    if params.get('threadId') != self.thread_id:
                        continue
                    if message.get('method') == 'turn/started':
                        self.turn_id = params['turn']['id']
                    if message.get('method') == 'turn/completed':
                        self.done.set()
                    self.events.put_nowait(message)
        except (OSError, ValueError, TypeError):
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(OSError('native_session_disconnected'))
            if self.events is not None:
                self.events.put_nowait(None)

    async def read_stderr(self):
        while chunk := await self.process.stderr.read(4096):
            self.stderr.extend(chunk)
            del self.stderr[:-16384]

    async def turn(self, prompt, config, consume):
        async with self.lock:
            self.events = asyncio.Queue()
            self.turn_id = None
            self.done.clear()
            try:
                response = await self.rpc('turn/start', {
                    'threadId': self.thread_id, 'input': [{'type': 'text', 'text': prompt}],
                    'model': config.model, 'effort': config.reasoning_effort,
                    'approvalPolicy': 'never',
                    'sandboxPolicy': {'type': 'workspaceWrite', 'writableRoots': [str(self.dir)],
                                      'networkAccess': True}})
                self.turn_id = response['turn']['id']
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

    async def interrupt(self):
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
                status = await session.turn(self.prompt(), self.config, self.consume)
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
        if method in ('item/started', 'item/completed') and item.get('type') == 'commandExecution':
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
        elif method == 'turn/completed' and params.get('turn', {}).get('error'):
            self.stream.errors.append(str(params['turn']['error'].get('message', 'native_turn_failed')))

    async def final(self, state, extra):
        # No skill directory is exposed or inspected; the native thread owns conversational history.
        result = await super().final(state, extra)
        result['skills_updated'] = []
        if self.session and self.session.process:
            result['native_session'] = {'thread_id': self.session.thread_id, 'pid': self.session.process.pid,
                                        'turn_id': self.session.turn_id, 'persistent': True}
        return result
