"""Shared synthetic pieces of the local-agent tests (ANYM-971): a fake engine binary, a fake phone and a live service on a
loopback port. No phone, no model and no network beyond loopback."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace
import uuid
from xml.sax.saxutils import quoteattr

import httpx
import uvicorn

from deedkit_android import engines
from deedkit_android.app import create_app
from deedkit_android.config import Refused
from deedkit_android.secrets import SecretStore

PACKAGE = 'example.food'
SECRET = 'SHOP_PASSWORD'
VERSION = '9.9.9'
PINNED = {'claude': '9.9.9', 'codex': '9.9.9'}

FAKE_ENGINE = r'''#!{python}
import json, os, re, signal, subprocess, sys, time, urllib.request
plan = json.load(open({plan!r}))
if sys.argv[1:] == ['--version']:
    print(plan.get('version', '9.9.9') + ' (fake)')
    raise SystemExit(0)
claude = plan['engine'] == 'claude'
out = {{'argv': sys.argv, 'env': dict(os.environ), 'cwd': os.getcwd(), 'pid': os.getpid(), 'pgid': os.getpgid(0),
       'stdin': sys.stdin.read(), 'replies': [], 'children': []}}
def save():
    json.dump(out, open(plan['record'], 'w'))
def emit(**line):
    print(json.dumps(line), flush=True)
save()
text = json.dumps(sys.argv)
for item in sys.argv:
    if item.endswith('mcp.json'):
        text += open(item).read()
run = json.load(open(re.search(r'[^\s"\',\]\\]+/run\.json', text)[0]))
if claude:
    emit(type='system', subtype='init', model='claude-sonnet-5-5', tools=['mcp__phone__' + name for name in plan['tools']] + plan.get('raw_tools', []))
else:
    emit(type='thread.started', thread_id='t')
def say(words):
    if claude:
        emit(type='assistant', message={{'id': words, 'content': [{{'type': 'text', 'text': words}}], 'usage': {{'input_tokens': 1, 'output_tokens': 1}}}})
    else:
        emit(type='item.completed', item={{'id': 'i', 'type': 'agent_message', 'text': words}})
def call(name, arguments):
    if claude:
        emit(type='assistant', message={{'id': name, 'content': [{{'type': 'tool_use', 'name': 'mcp__phone__' + name, 'input': arguments}}]}})
    else:
        emit(type='item.started', item={{'id': 'i', 'type': 'mcp_tool_call', 'server': 'phone', 'tool': name, 'arguments': arguments}})
    request = urllib.request.Request(run['url'] + '/agent-tool/' + name, data=json.dumps(arguments).encode(), method='POST',
                                     headers={{'Authorization': 'Bearer ' + run['bearer'], 'Content-Type': 'application/json'}})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            reply = [response.status, json.loads(response.read())]
    except urllib.error.HTTPError as error:
        reply = [error.code, json.loads(error.read() or b'{{}}')]
    except OSError as error:
        reply = ['oserror', str(error)]
    out['replies'].append([name, reply])
    save()
for step in plan['steps']:
    if 'say' in step:
        say(step['say'])
    elif 'call' in step:
        call(step['call'], step.get('args', {{}}))
    elif 'sleep' in step:
        time.sleep(step['sleep'])
    elif 'child' in step:
        script = 'trap "" TERM; sleep 120' if step['child'] == 'stubborn' else 'sleep 120'
        child = subprocess.Popen(['/bin/sh', '-c', script])
        out['children'].append(child.pid)
        save()
    elif 'foreign' in step:
        if claude:
            emit(type='assistant', message={{'id': 'f', 'content': [{{'type': 'tool_use', 'name': step['foreign'], 'input': {{}}}}]}})
        else:
            emit(type='item.started', item={{'id': 'f', 'type': 'mcp_tool_call', 'server': 'codex_apps', 'tool': step['foreign']}})
    elif 'ignore_term' in step:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    elif 'exit' in step:
        raise SystemExit(step['exit'])
    elif 'hang' in step:
        while True:
            time.sleep(1)
if claude:
    emit(type='result', subtype='success', is_error=False, usage={{'input_tokens': 6, 'output_tokens': 95,
         'cache_read_input_tokens': 7920, 'cache_creation_input_tokens': 4103}})
else:
    emit(type='turn.completed', usage={{'input_tokens': 1000, 'cached_input_tokens': 600, 'cache_write_input_tokens': 0,
         'output_tokens': 50}})
'''
TOOLS = ['screen', 'tap', 'type', 'key', 'swipe', 'wait', 'read_text', 'read_list', 'open_app', 'run_flow', 'flow_get', 'flow_edit',
         'flow_test', 'finish']


def fake_engine(tmp_path, name, steps, **plan):
    """An executable that behaves like `name`'s CLI: records its argv, environment and stdin, then plays the steps."""
    record = tmp_path / f'{name}-record-{uuid.uuid4().hex[:6]}.json'
    plan_file = tmp_path / f'{name}-plan-{uuid.uuid4().hex[:6]}.json'
    plan_file.write_text(json.dumps({'engine': name, 'steps': steps, 'record': str(record), 'tools': TOOLS, **plan}))
    binary = tmp_path / f'{name}-{uuid.uuid4().hex[:6]}'
    binary.write_text(FAKE_ENGINE.format(python=sys.executable, plan=str(plan_file)))
    binary.chmod(0o755)
    return binary, record


def screen_xml(package=PACKAGE, text='', nodes=()):
    body = ''.join(f'<node text={quoteattr(label)} resource-id={quoteattr(rid)} class="android.widget.Button" '
                   f'package="{package}" clickable="true" bounds="[{x},{y}][{x + 200},{y + 80}]" />'
                   for label, rid, x, y in nodes)
    field = (f'<node text={quoteattr(text)} resource-id="{package}:id/password" class="android.widget.EditText" '
             f'package="{package}" focused="true" password="true" bounds="[0,900][600,980]" />')
    return f'<hierarchy rotation="0">{body}{field}</hierarchy>'


class FakePhone:
    """What the host's UIA2 worker would answer. A screen is a hierarchy; `stop` makes the guard stop an action."""

    def __init__(self):
        self.commands, self.options, self.field, self.stop, self.slow = [], [], '', None, {}
        self.nodes = [('Add to cart', f'{PACKAGE}:id/add', 100, 300), ('Total €12.50', f'{PACKAGE}:id/total', 100, 600)]
        self.package = PACKAGE
        self.counts, self.fail_from, self.fail_calls, self.stop_nth = {}, {}, {}, {}  # per command: fail from the nth call on, fail
        # the listed calls, guard-stop the nth

    async def call(self, device, command, **options):
        self.commands.append(command)
        self.options.append(options)
        if command == '__guard__':
            return {'success': True, 'package': self.package, 'packages': [self.package]}
        if command == '__hierarchy__':
            return {'xml': screen_xml(self.package, self.field, self.nodes)}
        name = next(iter(command))
        self.counts[name] = self.counts.get(name, 0) + 1
        if self.counts[name] in self.fail_calls.get(name, ()) or (name in self.fail_from and self.counts[name] >= self.fail_from[name]):
            raise Refused('element_not_found')
        if self.stop_nth.get(name) == self.counts[name]:
            error = Refused('guard_stopped')
            error.guard = {'rule': 'tap_phrase', 'matched': 'pay now', 'when': 'before', 'package': self.package,
                           'sensitive': False}
            raise error
        if self.stop and name in self.stop:
            error = Refused('guard_stopped')
            error.guard = {'rule': 'tap_phrase', 'matched': self.stop[name], 'when': 'before', 'package': self.package,
                           'sensitive': False}
            raise error
        if name in self.slow:
            await asyncio.sleep(self.slow[name])
        if name == 'inputText':
            typed = command['inputText']
            if typed.startswith('${secret:'):
                typed = options['secrets'][typed[9:-1]]['value']
            self.field += typed
        if name == 'copyTextFrom':
            key = (command['copyTextFrom'] or {}).get('key') if isinstance(command['copyTextFrom'], dict) else None
            return {'text': self.field, **({'key': key} if key else {})}
        if name == 'extendedWaitUntil':
            return {'matched': True}
        return {'success': True}

    async def idle(self, *args):
        return None


def init_tools(extra=()):
    return TOOLS + list(extra)


def login_files(tmp_path):
    token = tmp_path / 'claude.token'
    token.write_text('claude-login-' + uuid.uuid4().hex)
    token.chmod(0o600)
    home = tmp_path / 'codex-home'
    home.mkdir(mode=0o700)
    (home / 'auth.json').write_text('{"tokens": "codex-login-' + uuid.uuid4().hex + '"}')
    (home / 'auth.json').chmod(0o600)
    return token, home


def write_config(tmp_path, claude=None, codex=None, enabled=True, default=None, guard=None, devices=None):
    token, home = login_files(tmp_path)
    engine_config = {}
    if claude:
        engine_config['claude'] = {'binary': str(claude), 'version': VERSION, 'token_file': str(token)}
    if codex:
        engine_config['codex'] = {'binary': str(codex), 'version': VERSION, 'codex_home': str(home)}
    config = tmp_path / 'config.json'
    body = {'adb': '/synthetic/adb', 'state_dir': str(tmp_path / 'state'), 'devices': devices or {'home': 'SERIAL'},
            'principals': [
                {'id': 'one', 'key_sha256': hashlib.sha256(b'one').hexdigest(), 'devices': list(devices or ['home']),
                 'permissions': ['android:read', 'android:control']},
                {'id': 'reader', 'key_sha256': hashlib.sha256(b'reader').hexdigest(), 'devices': ['home'],
                 'permissions': ['android:read']},
                {'id': 'two', 'key_sha256': hashlib.sha256(b'two').hexdigest(), 'devices': ['home'],
                 'permissions': ['android:read', 'android:control']}],
            'engines': engine_config, 'agent': {'enabled': enabled}}
    if default:
        body['default_engine'] = default
    if guard:
        body['guard'] = guard
    config.write_text(json.dumps(body))
    config.chmod(0o600)
    return config


def pass_probe(tmp_path, *names):
    (tmp_path / 'state').mkdir(mode=0o700, exist_ok=True)
    for name in names:
        engines.write_probe(tmp_path / 'state', name, {'engine': name, 'version': VERSION, 'passed': True, 'failed': []})


def set_secret(tmp_path, value):
    (tmp_path / 'state').mkdir(mode=0o700, exist_ok=True)
    SecretStore(tmp_path / 'state').set(SECRET, PACKAGE, value)


class Service:
    """The app on a real loopback port (the engine's stdio server calls it over HTTP), with a fake phone behind it."""

    def __init__(self, config, phone=None):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        self.url = 'http://127.0.0.1:%d' % sock.getsockname()[1]
        self.app = create_app(config, agent_url=self.url)
        self.phone = phone or FakePhone()
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level='critical', access_log=False))
        self.thread = threading.Thread(target=lambda: asyncio.run(self.server.serve(sockets=[sock])), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            assert time.monotonic() < deadline and self.thread.is_alive(), 'service did not start'
            time.sleep(0.02)

        async def noop(*args):
            pass
        self.app.state.devices.ui = SimpleNamespace(call=self.phone.call, close=noop, ensure_fenced_process_stopped=noop)

        async def capture(owner, device, mode, crop):
            return {'image': {'mime_type': 'image/png', 'data': 'UE5H', 'width': 1, 'height': 1},
                    'image_file': {**self.app.state.store.save(owner, b'synthetic PNG'), 'mime_type': 'image/png'}}
        self.app.state.devices.capture_image = capture
        self.client = httpx.Client(base_url=self.url, timeout=30)

    def post(self, operation, body, key='one', code=200):
        response = self.client.post('/v1/' + operation, json=body, headers={'Authorization': 'Bearer ' + key})
        assert response.status_code == code, response.text
        return response.json()

    def lease(self, device='home', key='one'):
        return self.post('session', {'action': 'acquire', 'device_id': device}, key)['session_id']

    def start(self, session, request_id=None, key='one', code=200, **fields):
        body = {'action': 'start', 'device_id': 'home', 'session_id': session, 'request_id': request_id or str(uuid.uuid4()),
                'goal': 'Find the shop, add the item and read the basket total.', 'owner_instruction': 'Order check for me',
                'apps': [PACKAGE], **fields}
        return self.post('agent', body, key, code)

    def get(self, request_id, after=0, wait_s=0, key='one', code=200):
        return self.post('agent', {'action': 'get', 'request_id': request_id, 'after': after, 'wait_s': wait_s}, key, code)

    def finished(self, request_id, timeout=30, key='one'):
        deadline = time.monotonic() + timeout
        while True:
            view = self.get(request_id, wait_s=1, key=key)
            if view['state'] != 'running':
                return view
            assert time.monotonic() < deadline, 'the run did not end: ' + json.dumps(view)[:500]

    def close(self):
        self.client.close()
        self.server.should_exit = True
        self.thread.join(30)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def recorded(record, timeout=10):
    deadline = time.monotonic() + timeout
    while not Path(record).exists() or not Path(record).read_text():
        assert time.monotonic() < deadline, 'the engine left no record'
        time.sleep(0.02)
    return json.loads(Path(record).read_text())


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True

