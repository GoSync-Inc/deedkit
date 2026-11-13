"""Shared local-agent lifecycle, guarded compatibility executor and admission to bare Codex."""

import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Literal

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError, model_validator
import yaml

from . import engines
from .agent_save import MAX_REPAIRS, SaveError, Saver, resolve_point, settle
from .agent_screen import cap_bytes, compact, render_lines
from .agent_tools import MODELS, target
from .app_maps import compile_scenario
from .config import Package, Refused, Strict
from .flows import VARIABLE
from .guard import Rules, Stop
from .secrets import REFERENCE, Run as SecretRun, references
from .store import identifier
from .uiautomator import DATA_KEY, UiRunner, collect_data, validate_commands

MINUTE_S = 60
FINISH_GRACE_S = 10
TOOL_TIMEOUT_S = 90
# Surfaces an action may leave the phone on without leaving the run's apps: home screens, the system UI, permission
# prompts and keyboards. Any other foreground app after an action ends the run (rule foreign_app).
NEUTRAL_PACKAGES = frozenset({
    'com.android.systemui', 'com.android.launcher3', 'com.google.android.apps.nexuslauncher', 'com.sec.android.app.launcher',
    'com.miui.home', 'com.android.permissioncontroller', 'com.google.android.permissioncontroller',
    'com.google.android.inputmethod.latin', 'com.samsung.android.honeyboard', 'com.android.inputmethod.latin'})
RETENTION_S = 14 * 24 * 3600
MAX_NOTES = 200
STATES = ('running', 'succeeded', 'stopped_by_guard', 'timed_out', 'action_cap', 'failed', 'stopped', 'lost')
NAME = r'^[A-Z][A-Z0-9_]{0,63}$'
POINT = re.compile(r'\s*\d+(?:\.\d+)?%?\s*,\s*\d+(?:\.\d+)?%?\s*')
START_ONLY = ('device_id', 'session_id', 'goal', 'owner_instruction', 'apps', 'params', 'secrets', 'outputs',
              'max_minutes', 'max_actions', 'engine', 'model', 'effort', 'save_as', 'param_names')


def preamble():
    """(version, text) of the versioned preamble the engine gets: the rules it follows beside the guard's."""
    for base in (Path(__file__).parent, Path(__file__).resolve().parents[2]):
        path = base / 'agent_preamble.md'
        if path.is_file():
            text = path.read_text()
            found = re.match(r'Preamble version: (\d+)', text)
            if found:
                return int(found[1]), text
    raise Refused('agent_preamble_missing', 503)


# --- the operation's arguments ------------------------------------------------------------------------------------


class AgentCall(Strict):
    action: Literal['start', 'get', 'stop']
    request_id: str
    device_id: str | None = None
    session_id: str | None = None
    goal: str | None = Field(default=None, min_length=1, max_length=2000)
    owner_instruction: str | None = Field(default=None, min_length=3, max_length=1500)
    apps: list[Package] | None = Field(default=None, min_length=1, max_length=5)
    params: dict[str, str | int | bool] = Field(default_factory=dict, max_length=20)
    secrets: list[str] = Field(default_factory=list, max_length=5)
    outputs: list[str] = Field(default_factory=list, max_length=10)
    max_minutes: int = Field(default=5, ge=1, le=15)
    max_actions: int = Field(default=120, ge=10, le=300)
    engine: Literal['claude', 'codex'] | None = None
    model: str | None = Field(default=None, pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
    effort: Literal['minimal', 'low', 'medium', 'high'] | None = None
    save_as: str | None = Field(default=None, pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$')
    param_names: list[str] = Field(default_factory=list, max_length=20)
    after: int = Field(default=0, ge=0)
    wait_s: int = Field(default=0, ge=0, le=25)

    @model_validator(mode='after')
    def fields_of_the_action(self):
        given = self.model_fields_set
        if self.action == 'start':
            missing = {'device_id', 'session_id', 'goal', 'owner_instruction'} - given
            if missing or self.after or self.wait_s:
                raise ValueError('start_fields')
        elif given & set(START_ONLY) or (self.action == 'stop' and given & {'after', 'wait_s'}):
            raise ValueError('action_fields')
        if (self.param_names and self.save_as is None) or not set(self.param_names) <= set(self.params) or len(
                set(self.param_names)) != len(self.param_names):
            raise ValueError('param_names')
        if any(not re.fullmatch(NAME, name) for name in (*self.secrets, *self.params)) or any(
                not DATA_KEY.fullmatch(name) for name in self.outputs) or len(set(self.secrets)) != len(
                self.secrets) or len(set(self.outputs)) != len(self.outputs):
            raise ValueError('names')
        if any(len(value) > 4096 or '$' + '{' in value for value in map(str, self.params.values())):
            raise ValueError('values')
        return self

    def identity(self):
        """The canonical fields of a start: the same request_id with these returns its run, others conflict."""
        body = self.model_dump(exclude={'action', 'request_id', 'session_id', 'after', 'wait_s'})
        body.update(apps=sorted(set(body['apps'] or ())), secrets=sorted(self.secrets), outputs=sorted(self.outputs),
                    params={name: str(value) for name, value in self.params.items()})
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


# --- storage: runs, events and action logs in the service's SQLite state --------------------------------------------


class AgentStore:
    def __init__(self, store):
        self.db = store.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS agent_runs (
                owner TEXT, id TEXT, identity TEXT, state TEXT, device TEXT, engine TEXT, model TEXT,
                started REAL, ended REAL, request TEXT, result TEXT, PRIMARY KEY(owner, id));
            CREATE TABLE IF NOT EXISTS agent_events (
                owner TEXT, id TEXT, seq INTEGER, at REAL, kind TEXT, body TEXT, PRIMARY KEY(owner, id, seq));
        """)
        self.db.commit()

    def row(self, owner, run_id):
        return self.db.execute('SELECT * FROM agent_runs WHERE owner=? AND id=?', (owner, run_id)).fetchone()

    def insert(self, owner, run_id, identity, device, engine, model, request):
        self.db.execute("INSERT INTO agent_runs VALUES(?,?,?,'running',?,?,?,?,NULL,?,NULL)",
                        (owner, run_id, identity, device, engine, model, time.time(), json.dumps(request)))
        self.db.commit()

    def set_model(self, owner, run_id, model):
        self.db.execute('UPDATE agent_runs SET model=? WHERE owner=? AND id=?', (model, owner, run_id))
        self.db.commit()

    def finish(self, owner, run_id, state, result):
        self.db.execute('UPDATE agent_runs SET state=?, ended=?, result=? WHERE owner=? AND id=?',
                        (state, time.time(), json.dumps(result), owner, run_id))
        self.db.commit()

    def add_event(self, owner, run_id, kind, body):
        seq = (self.db.execute('SELECT COALESCE(MAX(seq), 0) FROM agent_events WHERE owner=? AND id=?',
                               (owner, run_id)).fetchone()[0]) + 1
        self.db.execute('INSERT INTO agent_events VALUES(?,?,?,?,?,?)', (owner, run_id, seq, time.time(), kind, json.dumps(body)))
        self.db.commit()
        return seq

    def events(self, owner, run_id, after):
        rows = self.db.execute('SELECT seq, at, kind, body FROM agent_events WHERE owner=? AND id=? AND seq>? ORDER BY seq LIMIT 500',
                               (owner, run_id, after))
        return [{'seq': row['seq'], 'at': row['at'], 'kind': row['kind'], **json.loads(row['body'])} for row in rows]

    def mark_lost(self):
        """A run that was `running` when the service stopped is `lost`: its engine cannot be adopted."""
        rows = self.db.execute("SELECT owner, id FROM agent_runs WHERE state='running'").fetchall()
        for row in rows:
            self.finish(row['owner'], row['id'], 'lost', {'error': 'service_restarted', 'finished': False})
            self.add_event(row['owner'], row['id'], 'state', {'state': 'lost'})

    def purge(self, before):
        """The card's Deletes: runs, events and action logs 14 days after the run ended."""
        old = self.db.execute('SELECT owner, id FROM agent_runs WHERE ended IS NOT NULL AND ended<?', (before,)).fetchall()
        for row in old:
            self.db.execute('DELETE FROM agent_events WHERE owner=? AND id=?', (row['owner'], row['id']))
            self.db.execute('DELETE FROM agent_runs WHERE owner=? AND id=?', (row['owner'], row['id']))
        self.db.commit()
        return len(old)


# --- one run --------------------------------------------------------------------------------------------------------


class AgentRun:
    def __init__(self, agents, owner, request, engine, config, run_dir, secrets_run, preamble_text):
        self.agents, self.owner, self.request, self.engine, self.config = agents, owner, request, engine, config
        self.id, self.device, self.dir = request.request_id, request.device_id, Path(run_dir)
        self.bearer, self.secrets = os.urandom(32).hex(), secrets_run
        self.preamble_version, self.system_prompt = preamble_text
        self.state, self.process, self.stream = 'running', None, engines.Stream(engine)
        self.apps, self.params = set(request.apps or []), {name: str(value) for name, value in request.params.items()}
        self.actions, self.screenshots, self.images, self.notes = [], [], 0, 0
        self.finished, self.finish_tries, self.guard, self.op, self.error = None, 0, None, None, {}
        self.saver, self.resolved, self.read_value, self.pending = Saver(self) if request.save_as else None, None, None, None
        self.busy, self.cond, self.seq = asyncio.Lock(), asyncio.Condition(), 0
        self.concluded, self.tasks = asyncio.Event(), []
        self.started, self.started_at, self.engine_started, self.engine_ms, self.device_ms = time.monotonic(), time.time(), None, 0.0, 0.0

    # events and the progress cursor
    async def event(self, kind, **body):
        seq = self.agents.db.add_event(self.owner, self.id, kind, body)
        async with self.cond:
            self.seq = seq
            self.cond.notify_all()

    def declared(self):
        return {name: entry['package'] for name, entry in self.secrets.bound.items()}

    def send(self, command, **options):
        return self.agents.devices.ui.call(self.device, command, **options)

    def call(self, command, **options):
        return self.secrets.call(self.send, command, **{'element_timeout_ms': 5000, 'optional_timeout_ms': 1000, **options})

    # the engine's tool calls
    async def tool(self, name, arguments):
        """One tool call of the engine: returns {text, image?, is_error?}. A bearer that is not this run's `running` one
        never gets here (the route refuses it); the state is checked again under the lock."""
        if name not in MODELS:
            raise Refused('not_found', 404)
        try:
            args = MODELS[name].model_validate(arguments)
        except (ValidationError, ValueError):
            raise Refused('invalid_arguments', 422) from None
        async with self.busy:
            if self.state != 'running' or self.finished is not None:
                raise Refused('unauthorized', 401)
            self.op = asyncio.create_task(self.perform(name, args))
            try:
                return await self.op
            except asyncio.CancelledError:
                if (asyncio.current_task() or self.op).cancelling():
                    raise
                raise Refused('unauthorized', 401) from None

    async def perform(self, name, args):
        if name == 'finish':
            return await self.take_finish(args)
        if len(self.actions) >= self.request.max_actions:
            self.tasks.append(asyncio.create_task(self.end('action_cap')))
            return {'text': 'error: action limit reached, the run ends', 'is_error': True}
        entry = {'n': len(self.actions) + 1, 'tool': name, 'arguments': self.secrets.mask(args.model_dump(exclude_none=True)),
                 'verdict': 'allowed'}
        started = time.monotonic()
        stopped = None
        self.resolved = self.read_value = None
        try:
            async with asyncio.timeout(TOOL_TIMEOUT_S):
                reply = await getattr(self, 'do_' + name)(args)
            if self.saver is not None:  # What a scenario saved from this run needs of the action (ANYM-973).
                entry.update({key: value for key, value in (('resolved', self.resolved), ('read', self.read_value)) if value})
        except Refused as error:
            if error.code == 'guard_stopped' and getattr(error, 'guard', None):
                stopped = error
                entry['verdict'] = 'stopped:' + error.guard['rule']
                reply = {'text': 'STOPPED by the guard (' + error.guard['rule'] + '): the run ends here. Do nothing more.',
                         'is_error': True}
            else:
                entry.update(verdict='refused', error=error.code)
                details = getattr(error, 'details', None)
                reply = {'text': self.secrets.mask('error: ' + error.code + (' ' + json.dumps(details) if details else '')
                                                   + (' hint: ' + error.hint if getattr(error, 'hint', None) else '')),
                         'is_error': True}
        except TimeoutError:
            entry.update(verdict='refused', error='command_timeout')
            reply = {'text': 'error: command_timeout', 'is_error': True}
        entry['elapsed_ms'] = round((time.monotonic() - started) * 1000, 2)
        self.device_ms += entry['elapsed_ms']
        self.actions.append(entry)
        await self.event('action', tool=name, target=self.secrets.mask(target(name, entry['arguments'])),
                         verdict=entry['verdict'], elapsed_ms=entry['elapsed_ms'])
        if stopped is not None:
            self.guard = {**{key: stopped.guard.get(key) for key in ('rule', 'matched', 'when', 'package')},
                          'sensitive': stopped.guard.get('sensitive', True), 'action': entry['n'], 'tool': name,
                          'step_path': getattr(stopped, 'command_path', None)}
            self.tasks.append(asyncio.create_task(self.end('stopped_by_guard')))
        return reply

    async def take_finish(self, args):
        missing = [name for name in self.request.outputs if name not in args.data]
        self.finish_tries += 1
        if missing and self.finish_tries < 2:
            return {'text': 'outputs still missing: ' + ', '.join(missing) + '. Call finish again with them, or without '
                    'a value you could not read.', 'is_error': True}
        data = {name: args.data[name] for name in self.request.outputs if name in args.data}
        if len(json.dumps(data, default=str)) > 16384:
            raise Refused('invalid_arguments', 422)
        if self.saver is None:
            return self.conclude(args.summary, data)
        if self.pending is not None:  # a repair is under way: the draft is repaired with flow_edit and flow_test
            return {'text': 'error: the scenario draft is not published yet; use flow_edit and flow_test', 'is_error': True}
        self.pending = {'summary': args.summary, 'data': data}
        try:
            self.saver.draft_from_log(data)  # the service's SQLite connection is this thread's
        except SaveError as error:  # The run's own result stays succeeded; only the saving fails.
            self.saver.error = error.code + (f' (step {error.step})' if error.step else '')
            return self.conclude(**self.pending)
        except Refused as error:
            self.saver.error = error.code
            return self.conclude(**self.pending)
        return await self.test_draft()

    def conclude(self, summary, data):
        self.finished = {'summary': summary, 'data': data}
        self.tasks.append(asyncio.create_task(self.after_finish()))
        return {'text': 'ok, the run is finished'}

    async def test_draft(self):
        """Test the draft in this run's session. A pass publishes and finishes; a guard stop ends the run and never
        publishes; a failure goes back to the engine for at most MAX_REPAIRS repairs."""
        saver = self.saver
        outcome, detail = await settle(saver, await saver.test())
        if outcome is True:
            return self.conclude(**self.pending)
        if outcome is False:
            self.guard = {'rule': detail.get('rule'), 'matched': detail.get('matched'), 'when': detail.get('when'),
                          'package': None, 'sensitive': True, 'action': len(self.actions) + 1, 'tool': 'flow_test',
                          'step_path': detail.get('step_path')}
            self.tasks.append(asyncio.create_task(self.end('stopped_by_guard')))
            return {'text': 'STOPPED by the guard (' + str(detail.get('rule')) + '): the run ends here. Do nothing more.',
                    'is_error': True}
        if (saver.error != 'test_failed') or saver.repairs >= MAX_REPAIRS or saver.tests > MAX_REPAIRS:
            return self.conclude(**self.pending)
        return {'text': 'the draft test failed: ' + detail + f' Repair it with flow_edit (repairs left: '
                f'{MAX_REPAIRS - saver.repairs}), then flow_test.'}

    def repairing(self):
        if self.saver is None or self.pending is None or self.finished is not None:
            raise Refused('no_scenario_to_repair', 422)
        return self.saver

    async def after_finish(self):
        """The engine ends on its own after `finish`; one that lingers is stopped, then the run concludes."""
        try:
            await asyncio.wait_for(self.process.wait(), FINISH_GRACE_S)
        except (TimeoutError, AttributeError):
            pass
        await self.end('succeeded')

    def command(self, command):
        validate_commands([command])
        return command

    async def guarded(self, command):
        """One command through the guard and the secret resolution, then the guard's screen rule on its result."""
        result = await self.call(self.command(command))
        self.check_foreground(await self.call('__guard__'))
        return result

    def check_foreground(self, seen):
        """The app on screen after an action is one of the run's apps or a neutral system surface; any other ends the run."""
        package = (seen or {}).get('package')
        if package is None or package in self.apps or package in NEUTRAL_PACKAGES:
            return
        raise Stop('foreign_app', package, package, 'after')

    async def do_screen(self, args):
        await self.call('__guard__')
        parts = [compact((await self.call('__hierarchy__'))['xml'])]
        if args.ocr:
            parts.append('recognized text:\n' + render_lines(await self.call({'readScreenText': {'structure': True, 'limit': 100}})))
        image = None
        if args.image:
            if self.secrets.masker:
                parts.append('image withheld: a secret was typed in this run')
            elif self.images >= 3:
                parts.append('image budget used (3 per run)')
            else:
                shot = await self.agents.devices.capture_image(self.owner, self.device, 'both', None)
                if 'image' in shot:
                    self.images += 1
                    self.screenshots.append(shot['image_file']['file_id'])
                    image = shot['image']['data']
                else:
                    parts.append('image unavailable')
        return {'text': self.secrets.mask('\n'.join(parts)), **({'image': image} if image else {})}

    async def do_tap(self, args):
        selector = {'point': args.point} if args.point else args.selector.fields()
        if args.point and self.saver is not None:  # What the point hit, for a scenario saved from this run (ANYM-973).
            await self.call('__guard__')
            self.resolved = resolve_point((await self.call('__hierarchy__'))['xml'], args.point)
        await self.guarded({'tapOn': selector})
        return {'text': 'tapped'}

    async def do_flow_get(self, args):
        saver = self.repairing()
        return {'text': json.dumps(saver.library.read(self.owner, 'flow', saver.name, None, 'draft'))}

    async def do_flow_edit(self, args):
        saver = self.repairing()
        if saver.repairs >= MAX_REPAIRS:
            raise Refused('repair_limit', 422)
        saver.write_draft(args.yaml)
        saver.repairs += 1
        return {'text': f'draft saved; run flow_test (repairs used: {saver.repairs} of {MAX_REPAIRS})'}

    async def do_flow_test(self, args):
        self.repairing()
        return await self.test_draft()

    async def do_type(self, args):
        text = args.text
        found = REFERENCE.fullmatch(text)
        if found and found[1] not in self.secrets.bound:
            raise Refused('secret_not_declared', 422)
        if not found and '${secret:' in text:
            raise Refused('secret_reference_not_allowed', 422)
        text = text if found else VARIABLE.sub(lambda match: self.params.get(match[1], match[0]), text)
        await self.guarded({'inputText': text})
        return {'text': 'typed'}

    async def do_key(self, args):
        await self.guarded({'pressKey': args.key})
        return {'text': 'pressed ' + args.key}

    async def do_swipe(self, args):
        options = {'duration': args.duration_ms}
        options.update({'start': args.start, 'end': args.end} if args.start else {'direction': args.direction})
        await self.guarded({'swipe': options})
        return {'text': 'swiped'}

    async def do_wait(self, args):
        if args.settled:
            await self.call(self.command({'waitForAnimationToEnd': {'timeout': args.timeout_ms}}))
            return {'text': 'matched: true'}
        visible = args.visible is not None
        selector = (args.visible or args.gone).fields()
        result = await self.call(self.command({'extendedWaitUntil': {
            'visible' if visible else 'notVisible': selector, 'timeout': args.timeout_ms}}))
        return {'text': 'matched: ' + str(bool(result.get('matched'))).lower()}

    async def do_read_text(self, args):
        if args.selector is not None:
            result = await self.call(self.command({'copyTextFrom': args.selector.fields()}))
            self.read_value = self.secrets.mask(str(result.get('text', '')))[:500]
            return {'text': self.secrets.mask(str(result.get('text', '')))}
        options = {'limit': args.limit, **({'region': args.region.model_dump()} if args.region else {})}
        return {'text': render_lines(await self.call(self.command({'readScreenText': options})))}

    async def do_read_list(self, args):
        if args.map is None:
            result = await self.call(self.command({'readScreenText': {'structure': True, 'limit': 200}}))
            return {'text': render_lines({'structure': result.get('structure'), 'lines': []}) if result.get('structure')
                    else 'no lists recognized'}
        source = yaml.safe_dump({'map': args.map, 'steps': [{'readList': {'list': args.list, 'limit': args.limit}}]})
        return await self.flow(source, None, {})

    async def do_open_app(self, args):
        if args.package not in self.apps:
            raise Refused('app_not_allowed', 422)
        await self.guarded({'launchApp': {'appId': args.package, 'stopApp': False}})
        return {'text': 'opened ' + args.package}

    async def do_run_flow(self, args):
        if (args.yaml is None) == (args.name is None):
            raise Refused('provide_yaml_or_flow_name', 422)
        return await self.flow(args.yaml, {'name': args.name, 'version': args.version} if args.name else None, args.params)

    async def flow(self, source, root, params):
        merged = {**self.params, **{name: json.dumps(value) if isinstance(value, bool) else str(value)
                                    for name, value in params.items()}}
        commands, _ = compile_scenario(self.agents.store.library, self.owner, source, merged, stage='published', root=root,
                                       secrets=self.declared)
        self.check_commands(commands)
        runner = UiRunner(self.call, 5000, 1000)
        await runner.run(commands)
        self.check_foreground(await self.call('__guard__'))
        data = self.secrets.mask(collect_data(runner.steps))
        return {'text': cap_bytes(json.dumps({'steps': len(runner.steps), 'data': data}, ensure_ascii=False), 8192)}

    def check_commands(self, commands):
        """A flow acts in this run's apps only, and types only the secrets the run declares."""
        if references(commands) - self.secrets.bound.keys():
            raise Refused('secret_not_declared', 422)

        def walk(items):
            for command in items:
                if not isinstance(command, dict):
                    continue
                name, value = next(iter(command.items()))
                options = value if isinstance(value, dict) else {}
                packages = []
                if name in {'launchApp', 'stopApp', 'killApp', 'clearState'}:
                    packages = [options.get('appId', value)]
                elif name == 'openLink':
                    raise Refused('app_not_allowed', 422)
                elif name == 'checkpoint':
                    raise Refused('checkpoint_not_supported', 422)
                elif isinstance(options.get('app_map'), dict):
                    packages = [options['app_map'].get('package')]
                if any(package not in self.apps for package in packages):
                    raise Refused('app_not_allowed', 422)
                if name in {'runFlow', 'repeat', 'retry'}:
                    walk(options.get('commands', []))
        walk(commands)

    # the run's life
    async def supervise(self):
        heartbeat = asyncio.create_task(self.keep_lease())
        reader = None
        try:
            try:
                self.process = await engines.spawn(self.engine, self.config, self.dir, self.agents.url, self.bearer,
                                                   self.system_prompt, self.agents.task_text(self))
            except (OSError, ValueError):
                await self.end('failed', error='engine_start_failed')
                return
            self.engine_started = time.monotonic()
            reader = asyncio.create_task(self.read_stream())
            try:
                async with asyncio.timeout(self.request.max_minutes * MINUTE_S):
                    await self.process.wait()
            except TimeoutError:
                await self.end('timed_out')
            else:
                await self.end('succeeded' if self.finished is not None else 'failed', exit_code=self.process.returncode)
            await self.concluded.wait()
        finally:
            heartbeat.cancel()
            if reader is not None:
                try:
                    await asyncio.wait_for(reader, 5)
                except (TimeoutError, asyncio.CancelledError):
                    reader.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def keep_lease(self):
        while True:
            await asyncio.sleep(5)
            try:
                self.agents.store.touch(self.owner, self.device, self.request.session_id)
            except Refused as error:
                await self.end('failed', error=error.code)
                return

    async def read_stream(self):
        while True:
            try:
                line = await self.process.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                await self.process.stdout.read(65536)
                continue
            if not line:
                return
            known = self.stream.model
            note = self.stream.feed(line)
            if self.stream.model and self.stream.model != known:
                self.agents.db.set_model(self.owner, self.id, self.stream.model)
            if note and self.notes < MAX_NOTES:
                self.notes += 1
                await self.event('note', text=self.secrets.mask(note))

    async def end(self, state, **extra):
        """End the run in `state`. The first caller wins and kills the bearer at once; the others wait for the end."""
        if self.state != 'running':
            await self.concluded.wait()
            return
        self.state = state
        self.agents.by_bearer.pop(self.bearer, None)
        result = {}
        try:
            op = self.op
            if op is not None and not op.done() and op is not asyncio.current_task():
                op.cancel()
                await asyncio.gather(op, return_exceptions=True)
            if self.process is not None:
                await self.stop_engine()
                self.engine_ms = round((time.monotonic() - (self.engine_started or self.started)) * 1000, 2)
            result = await self.final(state, extra)
        except Exception:  # noqa: BLE001 - the row and the lock below must happen whatever the final read did
            result = result or {'error': 'final_observation_failed', 'finished': self.finished is not None}
        finally:
            try:
                self.agents.db.finish(self.owner, self.id, state, self.secrets.mask(result))
                await self.event('state', state=state)
            finally:
                self.agents.release(self)
                self.concluded.set()

    async def stop_engine(self):
        await engines.kill_group(self.process)

    async def final(self, state, extra):
        """The run's result: what it said and read, and a last look at the screen the phone stays on."""
        text, stop_screen, withheld = '', None, None
        devices = self.agents.devices
        try:
            async with asyncio.timeout(20):
                if state == 'stopped_by_guard' and self.guard:
                    stop_screen = await devices.stop_observation(self.owner, self.device, self.guard.get('package'),
                                                                 self.guard.get('sensitive', True), self.secrets)
                    withheld = stop_screen.get('withheld')
                else:
                    try:
                        await self.call('__guard__')
                        text = compact((await self.call('__hierarchy__'))['xml'], 8192)
                    except Refused as error:
                        if error.code != 'guard_stopped':
                            raise
                        withheld = 'protected_screen'
        except (Refused, TimeoutError, OSError, KeyError, ValueError):
            withheld = withheld or 'unavailable'
        finished = self.finished or {}
        data = finished.get('data', {})
        total = round((time.monotonic() - self.started) * 1000, 2)
        result = {'summary': finished.get('summary', ''), 'data': data,
                  'missing_outputs': [name for name in self.request.outputs if name not in data],
                  'final_screen_text': cap_bytes(text, 8192), 'screenshots': list(self.screenshots[:3]),
                  'action_log': self.actions, 'finished': self.finished is not None,
                  'timings': {'engine_ms': self.engine_ms, 'device_ms': round(self.device_ms, 2), 'total_ms': total},
                  'engine': self.engine, 'model': self.stream.model or self.config.model or engines.DEFAULT_MODEL[self.engine] or 'default',
                  'tokens': self.stream.tokens(), 'preamble_version': self.preamble_version, **extra}
        if self.saver is not None:
            result['scenario'] = self.saver.view()
        if withheld:
            result['final_screen_withheld'] = withheld
        if self.guard:
            result.update(guard={key: value for key, value in self.guard.items() if key != 'sensitive'}, stop_screen=stop_screen)
        return result


# --- the manager ------------------------------------------------------------------------------------------------------


class Agents:
    """Admission, lookup and shutdown of runs; owns the run table, the per-run bearers and the tool route's state."""

    def __init__(self, devices, store, configuration, url=None):
        self.devices, self.store, self.configuration, self.url = devices, store, configuration, url
        self.db, self.runs, self.by_bearer, self.tasks = AgentStore(store), {}, {}, set()
        self.root = Path(store.directory) / 'agent'
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.sessions = {}
        self.recover()

    def recover(self):
        """After a restart: kill the process group of each run directory left behind, by its private marker, mark the
        runs `lost`, and remove the directories."""
        for leftover in self.root.glob('run-*'):
            if leftover.is_dir():
                if not (leftover / 'marker.json').exists() or engines.kill_by_marker(leftover / 'marker.json'):
                    shutil.rmtree(leftover, ignore_errors=True)
        self.db.mark_lost()
        for marker in (self.root / 'sessions').glob('run-live-*/marker.json'):
            if engines.kill_by_marker(marker):
                marker.unlink(missing_ok=True)
        self.db.purge(time.time() - RETENTION_S)

    async def close(self):
        for run in list(self.runs.values()):
            await run.end('lost', error='service_stopped')
        for session in self.sessions.values():
            await session.close()

    def task_text(self, run):
        request = run.request
        lines = ['<task>', f'Goal: {request.goal}', f"The owner's words this run serves: {request.owner_instruction}",
                 'Apps you may act in: ' + ', '.join(request.apps)]
        if run.params:
            lines.append('Parameters (use ${NAME} in `type` text and pass them to run_flow): ' + ', '.join(
                f'{name}={json.dumps(value)}' for name, value in run.params.items()))
        if run.secrets.bound:
            lines.append('Secrets you may type with `type` (the whole text, as written): ' + ', '.join(
                '${secret:%s} (app %s)' % (name, package) for name, package in run.declared().items()))
        if request.outputs:
            lines.append('Outputs to fill in finish.data, by exact name: ' + ', '.join(request.outputs))
        if request.save_as:
            lines.append('This run is saved as the scenario ' + request.save_as + ': act in clean, repeatable steps and read each '
                         'declared output with read_text and a selector. After finish the service tests the saved draft; if '
                         'the test fails, repair it with flow_get, flow_edit and flow_test (at most 3 repairs), under the guard.')
        lines += [f'Limits: at most {request.max_actions} tool calls and {request.max_minutes} minutes.', '</task>']
        return '\n'.join(lines)

    def release(self, run):
        """The run is over: its directory goes, its lock is free, it is no longer live."""
        shutil.rmtree(run.dir, ignore_errors=True)
        self.runs.pop((run.owner, run.id), None)
        self.by_bearer.pop(run.bearer, None)
        lock = self.devices.locks[run.device]
        if lock.locked():
            lock.release()

    def read_config(self):
        try:
            return self.configuration.read()
        except (OSError, ValueError):
            raise Refused('configuration_unavailable', 503) from None

    async def describe(self):
        """The agent section of android_capabilities: the switch, each engine and why one is not offered."""
        config = self.read_config()
        if config.agent.enabled:
            found = {name: await asyncio.to_thread(engines.status, self.store.directory, name, config.engines.get(name), config.agent.guarded)
                     for name in ('claude', 'codex')}
        else:  # Off: no login file is read and no engine is run.
            found = {name: {'engine': name, 'provider': engines.PROVIDERS[name], 'available': False,
                            'reason': 'agent_disabled'} for name in ('claude', 'codex')}
        if not config.agent.guarded:
            found['claude'] = {**found['claude'], 'available': False, 'reason': 'bare_mode_codex_only'}
        default = (config.default_engine or 'claude') if config.agent.guarded else 'codex'
        try:
            version = preamble()[0] if config.agent.guarded else None
        except Refused:
            version = None
        return {'enabled': config.agent.enabled, **({} if config.agent.enabled else {'reason': 'agent_disabled'}),
                'mode': 'guarded' if config.agent.guarded else 'bare',
                'persistent': config.agent.persistent and not config.agent.guarded,
                'default_model': config.engines.get('codex').model or 'gpt-6.1-terra' if config.engines.get('codex') else 'gpt-6.1-terra',
                'default_effort': config.engines['codex'].reasoning_effort if 'codex' in config.engines else 'medium',
                'default_engine': default, 'engines': found, 'tools': list(MODELS) if config.agent.guarded else ['codex'], 'preamble_version': version, 'input_schema': AgentCall.model_json_schema(),
                'limits': {'max_minutes': 15, 'max_actions': 300, 'max_images': 3, 'wait_s': 25, 'apps': 5}}

    async def handle(self, owner, args):
        call = AgentCall.model_validate(args) if not isinstance(args, AgentCall) else args
        request_id = identifier(call.request_id)
        call = call.model_copy(update={'request_id': request_id})
        if call.action == 'start':
            return await self.start(owner, call)
        row = self.db.row(owner, request_id)
        if row is None:
            raise Refused('not_found', 404)
        if call.action == 'stop':
            run = self.runs.get((owner, request_id))
            if run is not None:
                await run.end('stopped')
            return self.view(owner, request_id, 0)
        return await self.poll(owner, request_id, call.after, call.wait_s)

    async def start(self, owner, call):
        self.db.purge(time.time() - RETENTION_S)
        identity = call.identity()
        existing = self.db.row(owner, call.request_id)
        if existing is not None:
            # Keep legacy identities usable while distinguishing exact typed model input.
            saved_params = json.loads(existing['request']).get('params', {})
            if existing['identity'] != identity or json.dumps(saved_params, sort_keys=True) != json.dumps(
                    call.params, sort_keys=True):
                raise Refused('request_id_conflict')
            return self.view(owner, call.request_id, 0)
        config = self.read_config()
        if not config.agent.enabled:
            raise Refused('agent_disabled')
        bare = not config.agent.guarded
        from .agent_bare import BareRun, admission
        selected = admission(call, config) if bare else None
        name = 'codex' if bare else call.engine or config.default_engine or 'claude'
        if not bare and not call.apps:
            raise Refused('apps_required', 422)
        offered = await asyncio.to_thread(engines.status, self.store.directory, name, config.engines.get(name), not bare)
        if not offered['available']:
            error = Refused('engine_unavailable')
            error.hint = offered['reason']
            raise error
        rules = Rules(config.guard.model_dump())
        if not bare and any(package in rules.write_denied or package in rules.protected for package in call.apps):
            raise Refused('app_not_allowed', 422)
        declared = {}
        if call.secrets:
            try:
                found = self.devices.secrets.read()
            except Refused:
                raise Refused('secrets_unavailable', 503) from None
            for secret in call.secrets:
                if secret not in found:
                    raise Refused('secret_unknown', 422)
                if found[secret]['package'] not in call.apps:
                    raise Refused('secret_not_in_apps', 422)
            declared = {secret: found[secret] for secret in call.secrets}
        if not bare and self.devices.ui is None:
            raise Refused('ui_engine_not_configured', 503)
        if not bare and self.url is None:
            raise Refused('agent_unavailable', 503)
        texts = (None, '') if bare else preamble()
        self.store.touch(owner, call.device_id, call.session_id)  # The caller's own, live lease.
        lock = self.devices.locks[call.device_id]
        if lock.locked():
            raise Refused('device_busy')
        await lock.acquire()
        run = None
        try:
            if self.devices.ui is not None:
                if bare:
                    await self.devices.ui.close(call.device_id)
                else:
                    await self.devices.ui.ensure_fenced_process_stopped(call.device_id)
            run_dir = Path(tempfile.mkdtemp(prefix='run-', dir=self.root))
            run_dir.chmod(0o700)
            secrets_run = SecretRun(self.devices.secrets)
            secrets_run.bound, secrets_run.packages = declared, sorted({entry['package'] for entry in declared.values()})
            selected = selected or config.engines[name].model_copy(update={
                'model': call.model or config.engines[name].model,
                'reasoning_effort': call.effort or config.engines[name].reasoning_effort})
            model = selected.model or engines.DEFAULT_MODEL[name] or 'default'
            run_type = BareRun if bare else AgentRun
            if bare and config.agent.persistent:
                from .codex_session import PersistentRun
                run_type = PersistentRun
            run = run_type(self, owner, call, name, selected, run_dir, secrets_run, texts)
            request = {**{key: value for key, value in call.model_dump(exclude={'session_id', 'after', 'wait_s'}).items()},
                       'engine': name}
            self.db.insert(owner, call.request_id, identity, call.device_id, name, model, request)
        except BaseException:
            if run is not None:
                shutil.rmtree(run.dir, ignore_errors=True)
            lock.release()
            raise
        self.runs[(owner, call.request_id)] = run
        if not bare:
            self.by_bearer[run.bearer] = run
        await run.event('state', state='running')
        task = asyncio.create_task(run.supervise())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return self.view(owner, call.request_id, 0)

    async def poll(self, owner, request_id, after, wait_s):
        run = self.runs.get((owner, request_id))
        if run is not None and wait_s and run.seq <= after:
            try:
                async with run.cond:
                    await asyncio.wait_for(run.cond.wait_for(lambda: run.seq > after or run.state != 'running'), wait_s)
            except TimeoutError:
                pass
        return self.view(owner, request_id, after)

    def view(self, owner, request_id, after):
        row = self.db.row(owner, request_id)
        events = self.db.events(owner, request_id, after)
        view = {'request_id': request_id, 'state': row['state'], 'engine': row['engine'], 'model': row['model'],
                'started_at': row['started'], 'events': events, 'next_cursor': events[-1]['seq'] if events else after}
        if row['state'] != 'running':
            view.update(json.loads(row['result'] or '{}'))
        return view

    def authenticate(self, authorization):
        """The live run whose per-run bearer this is; a run that ended has no bearer."""
        token = authorization[7:] if authorization.startswith('Bearer ') else ''
        for bearer, run in list(self.by_bearer.items()):
            if hmac.compare_digest(bearer, token) and run.state == 'running':
                return run
        raise Refused('unauthorized', 401)


def install_routes(app):
    """The tool route of the stdio server: POST /agent-tool/<tool> with the run's own bearer, not a principal's key."""

    @app.post('/agent-tool/{tool}')
    async def agent_tool(tool: str, request: Request):
        agents = request.app.state.agents
        run = agents.authenticate(request.headers.get('authorization', ''))
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise Refused('request_too_large', 413)
        try:
            arguments = json.loads(body or b'{}')
        except ValueError:
            raise Refused('invalid_arguments', 422) from None
        if tool not in MODELS:
            raise Refused('not_found', 404)
        if not isinstance(arguments, dict):
            raise Refused('invalid_arguments', 422)
        return JSONResponse(await run.tool(tool, arguments), headers={'Cache-Control': 'no-store'})
