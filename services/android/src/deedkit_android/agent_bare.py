"""Ordinary Codex on serial-specific adb, retaining the service's lifecycle and native sandbox."""

import asyncio
import json
import os
from pathlib import Path
import time

from . import agent_diagnostics, agent_learning, engines
from .agent_run import AgentRun, MINUTE_S, MAX_NOTES
from .config import Refused

MODEL = 'gpt-6.1-terra'


def admission(call, config):
    if call.engine not in (None, 'codex'):
        raise Refused('bare_mode_codex_only', 422)
    if call.secrets:
        raise Refused('secrets_not_supported', 422)
    if call.save_as or call.param_names:
        raise Refused('save_as_not_supported', 422)
    engine = config.engines.get('codex')
    if engine is None:
        return None
    return engine.model_copy(update={'model': call.model or engine.model or MODEL,
                                     'reasoning_effort': call.effort or engine.reasoning_effort})


class BareRun(AgentRun):
    def __init__(self, *args):
        super().__init__(*args)
        self.command_starts, self.model_ms = {}, 0.0
        self.skills_updated, self.learning_error = [], None
        self.stderr, self.stderr_reader, self.commands_started = bytearray(), None, 0

    def prompt(self):
        host = self.agents.read_config()
        skills = agent_learning.setup(self.config.codex_home, self.dir, self.request.outputs)
        learned = agent_learning.load(self.config.codex_home, self.request.apps)
        prompt = (f'Android phone on this host: {host.adb} -s {host.devices[self.device]}.\n'
                  "You are the owner's agent that does orders and tasks on this phone.\n"
                  f'Goal: {self.request.goal}\n')
        if self.params:
            prompt += 'Parameters: ' + json.dumps(self.params) + '\n'
        if self.request.apps:
            prompt += 'Apps: ' + ', '.join(self.request.apps) + '\n'
        prompt += '$android-learning\n' + (skills / 'android-learning' / 'SKILL.md').read_text()
        prompt += '\nPrior app navigation: ' + json.dumps(learned)
        prompt += '\nRequested data keys: ' + json.dumps(self.request.outputs)
        return prompt

    async def spawn(self):
        prompt = self.prompt()
        host = self.agents.read_config()
        command = [self.config.binary, 'exec', '--json', '--ephemeral', '--skip-git-repo-check',
                   '--sandbox', 'workspace-write', '-C', str(self.dir), '-m', self.config.model,
                   '-c', 'model_reasoning_effort=' + json.dumps(self.config.reasoning_effort),
                   '-c', 'sandbox_workspace_write.network_access=true', '-c', 'approval_policy=never',
                   '-o', str(self.dir / 'final.json'), '-']
        env = engines.environment('codex', self.config, self.dir)
        env['PATH'] = str(Path(host.adb).parent) + os.pathsep + env['PATH']
        env['ANDROID_SERIAL'] = host.devices[self.device]
        for name in ('TMPDIR', 'LANG'):
            if name in os.environ:
                env[name] = os.environ[name]
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=self.dir, start_new_session=True,
            limit=4 * 1024 * 1024)
        self.process = process
        self.stderr_reader = asyncio.create_task(self.read_stderr())
        if self.state != 'running':
            await engines.kill_group(process)
            raise OSError('run_already_stopped')
        (self.dir / 'marker.json').write_text(json.dumps({'pid': process.pid, 'token': self.dir.name}))
        (self.dir / 'marker.json').chmod(0o600)
        self.process = process  # cancellation can now kill it, including during stdin drain
        process.stdin.write(prompt.encode())
        try:
            await process.stdin.drain()
            process.stdin.close()
        except (OSError, ConnectionError):
            pass
        return process

    async def read_stderr(self):
        while chunk := await self.process.stderr.read(4096):
            self.stderr.extend(chunk)
            del self.stderr[:-16384]  # memory only; no raw diagnostic log or phone dump

    async def supervise(self):
        heartbeat, reader = asyncio.create_task(self.keep_lease()), None
        try:
            async with asyncio.timeout(self.request.max_minutes * MINUTE_S):
                await self.spawn()
                self.engine_started = time.monotonic()
                reader = asyncio.create_task(self.read_stream())
                await self.process.wait()
                await reader  # consume all command completions and token usage before finalizing
                await self.stderr_reader
                if self.state == 'running':
                    try:
                        path = self.dir / 'final.json'
                        if path.stat().st_size > 65536:
                            raise ValueError('result_too_large')
                        outcome, self.learning_error = agent_learning.decode_result(path.read_text())
                        if self.process.returncode == 0:
                            self.finished = outcome
                    except (OSError, ValueError):
                        self.learning_error = 'invalid_or_missing_result'
                    await self.end('succeeded' if self.finished else 'failed', exit_code=self.process.returncode)
        except TimeoutError:
            await self.end('timed_out')
        except (OSError, ValueError):
            await self.end('failed', error='engine_start_failed')
        finally:
            heartbeat.cancel()
            if reader and not reader.done():
                reader.cancel()
            if self.stderr_reader and not self.stderr_reader.done():
                self.stderr_reader.cancel()
            await asyncio.gather(heartbeat, *([reader] if reader else []),
                                 *([self.stderr_reader] if self.stderr_reader else []), return_exceptions=True)

    async def read_stream(self):
        async for line in self.process.stdout:
            try:
                event = json.loads(line)
                item = event.get('item') or {}
                if item.get('type') == 'command_execution':
                    key = item.get('id', '')
                    if event.get('type') == 'item.started':
                        self.commands_started += 1
                        self.command_starts[key] = time.monotonic()
                        if len(self.actions) + len(self.command_starts) > self.request.max_actions:
                            await self.end('action_cap')
                            return
                    elif event.get('type') == 'item.completed':
                        start = self.command_starts.pop(key, time.monotonic())
                        elapsed = round((time.monotonic() - start) * 1000, 2)
                        self.device_ms += elapsed
                        entry = {'tool': 'command', 'command': str(item.get('command', ''))[:300],
                                 'exit_code': item.get('exit_code'), 'elapsed_ms': elapsed}
                        self.actions.append(entry)
                        await self.event('action', **{**entry, 'command': entry['command'][:120]})
            except (ValueError, AttributeError, TypeError):
                continue
            note = self.stream.feed(line)
            if note and self.notes < MAX_NOTES:
                self.notes += 1
                await self.event('note', text=note)

    async def final(self, state, extra):
        finished = self.finished or {}
        if self.stderr_reader and not self.stderr_reader.done():
            try:
                await asyncio.wait_for(self.stderr_reader, 1)
            except (TimeoutError, asyncio.CancelledError):
                pass
        if state == 'succeeded' and not self.learning_error:
            try:
                self.skills_updated = agent_learning.publish(self.config.codex_home, finished.get('lessons', []))
            except (OSError, ValueError):
                self.learning_error = 'skill_publish_failed'
        screenshot = None
        try:
            async with asyncio.timeout(10):
                shot = await self.agents.devices.capture_image(self.owner, self.device, 'file', None)
                screenshot = shot.get('image_file')
        except (Refused, OSError, TimeoutError):
            pass
        data = {key: value for key, value in finished.get('data', {}).items() if value is not None}
        total = round((time.monotonic() - self.started) * 1000, 2)
        usages = [self.stream.final] if self.stream.final else list(self.stream.messages.values())
        observed = any(type(usage.get(key)) is int for usage in usages if isinstance(usage, dict)
                       for keys in engines.TOKEN_KEYS.values() for key in keys)
        return {'summary': finished.get('summary', ''), 'data': data,
                'missing_outputs': [key for key in self.request.outputs if key not in data],
                'screenshots': [screenshot] if screenshot else [], 'final_screenshot': screenshot or 'unavailable',
                'action_log': self.actions, 'finished': self.finished is not None,
                'timings': {'engine_ms': self.engine_ms, 'total_ms': total, 'device_ms': self.device_ms,
                            'commands': len(self.actions), 'commands_started': self.commands_started,
                            'model_ms': max(0, self.engine_ms - self.device_ms)},
                'tokens': self.stream.tokens(), 'token_usage_observed': observed,
                'engine': 'codex', 'model': self.config.model,
                'effort': self.config.reasoning_effort, 'mode': 'bare', 'prompt_version': 1,
                'skills_updated': self.skills_updated, 'learning_error': self.learning_error,
                'engine_errors': agent_diagnostics.receipt(self.stream.errors, bytes(self.stderr),
                    [self.request.goal, self.request.owner_instruction, *self.params.values()]), **extra}
