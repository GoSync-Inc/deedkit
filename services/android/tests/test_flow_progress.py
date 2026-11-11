"""A running flow shows its progress, copyTextFrom names its result, and a composite scenario returns what it read
in one receipt (ANYM-970). Synthetic fake devices and no phone."""

import asyncio
import hashlib
import json
import re
import time
from types import SimpleNamespace
import uuid
from xml.sax.saxutils import quoteattr

from fastapi.testclient import TestClient
import pytest
import yaml

from deedkit_android.app import create_app
from deedkit_android.app_maps import compile_scenario
from deedkit_android.config import Refused
from deedkit_android.device import Devices
from deedkit_android.guard import Guard
from deedkit_android.secrets import SecretStore
from deedkit_android.store import Store
from deedkit_android.ui_worker import execute

PACKAGE = 'example.food'
VALUE = 'canary-' + uuid.uuid4().hex
PROGRESS_KEYS = {'step_path', 'step_name', 'finished_steps', 'elapsed_ms'}
PLAIN = SimpleNamespace(read=lambda owner, kind, name, version, stage: {'version': 'v1', 'yaml': '- back'})


def host(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({
        'adb': '/synthetic/adb', 'state_dir': str(tmp_path / 'state'), 'devices': {'home': 'SERIAL'},
        'principals': [{'id': 'one', 'key_sha256': hashlib.sha256(b'one').hexdigest(), 'devices': ['home'],
                        'permissions': ['android:read', 'android:control']}]}))
    config.chmod(0o600)
    return config


def ui_devices(tmp_path, store):
    return Devices(SimpleNamespace(devices={'phone': 'SERIAL'}, adb='/synthetic', uiautomator2_jar='/synthetic',
                                   state_dir=str(tmp_path / 'state')), store)


def compiled(source, library=PLAIN, **options):
    return compile_scenario(library, 'owner', source, {}, **options)[0]


# --- criterion 2: named reads -----------------------------------------------------------------------------------


# spec: ANYM-970
def test_copy_text_from_names_its_result_and_keeps_its_old_shape_without_a_key():
    shop = FoodApp(screen='basket', ready_after=0)
    total = {'id': 'example.food:id/total'}
    assert execute(shop, {'copyTextFrom': total}) == {'text': '€12.50'}
    assert execute(shop, {'copyTextFrom': {**total, 'key': 'total'}}) == {'text': '€12.50', 'key': 'total'}


# spec: ANYM-970
@pytest.mark.parametrize('key', ['1bad', 'has space', 'a' * 65, '', 7, None, ['x']],
                         ids=['digit first', 'space', 'too long', 'empty', 'integer', 'null', 'list'])
def test_a_malformed_data_key_fails_validation(key):
    source = yaml.safe_dump([{'copyTextFrom': {'text': 'Total', 'key': key}}])
    with pytest.raises(Refused) as caught:
        compiled(source)
    assert caught.value.code == 'invalid_data_key' and caught.value.status == 422


LIST_MAP = ('package: example.shop\nselectors: {card: [{resourceId: "example.shop:id/card"}]}\n'
            'screens: {home: {require: [card]}}\nlists:\n  products:\n    item: [{resourceId: "example.shop:id/card"}]\n'
            '    fields: {title: [{resourceId: "example.shop:id/title"}]}\n')
CHILD = '- copyTextFrom: {text: Price, key: total}'
LIBRARY = SimpleNamespace(read=lambda owner, kind, name, version, stage: {
    'version': 'v1', 'yaml': LIST_MAP if kind == 'map' else CHILD})
DUPLICATES = {
    'two copies': '- copyTextFrom: {text: A, key: total}\n- copyTextFrom: {text: B, key: total}',
    'copy and screen text': '- copyTextFrom: {text: A, key: price}\n- readScreenText: {key: price}',
    'nested in runFlow and repeat': ('- runFlow: {commands: [{copyTextFrom: {text: A, key: total}}]}\n'
                                     '- repeat: {times: 2, commands: [{copyTextFrom: {text: B, key: total}}]}'),
    'against the default screen text key': '- readScreenText: {}\n- copyTextFrom: {text: A, key: screen_text}',
    'against a list name': ('map: shop\nsteps:\n  - readList: {list: products}\n'
                            '  - copyTextFrom: {text: A, key: products}'),
    'a saved child': '- copyTextFrom: {text: A, key: total}\n- runFlow: {name: child}',
}
UNIQUE = {
    'distinct keys': '- copyTextFrom: {text: A, key: one}\n- copyTextFrom: {text: B, key: two}\n- readScreenText: {key: three}',
    'copies without a key': '- copyTextFrom: A\n- copyTextFrom: B',
    # Reads that never named a key keep working as they did: the later one still replaces the earlier in data.
    'a keyless screen text twice': '- readScreenText: {}\n- readScreenText: {limit: 5}',
    'one list twice': 'map: shop\nsteps:\n  - readList: {list: products}\n  - readList: {list: products, limit: 5}',
}


# spec: ANYM-970
@pytest.mark.parametrize('source', DUPLICATES.values(), ids=DUPLICATES)
def test_two_reads_with_one_key_fail_validation_with_duplicate_data_key(source):
    with pytest.raises(Refused) as caught:
        compiled(source, LIBRARY)
    assert caught.value.code == 'duplicate_data_key' and caught.value.status == 422


# spec: ANYM-970
@pytest.mark.parametrize('source', UNIQUE.values(), ids=UNIQUE)
def test_reads_with_distinct_or_no_keys_still_validate(source):
    assert compiled(source, LIBRARY)


# spec: ANYM-970
def test_a_duplicate_key_is_refused_before_admission_through_http(tmp_path):
    with TestClient(create_app(host(tmp_path))) as client:
        request_id = str(uuid.uuid4())
        body = {'device_id': 'home', 'session_id': 'not-a-lease', 'request_id': request_id,
                'yaml': DUPLICATES['two copies']}
        refused = client.post('/v1/run_flow', json=body, headers={'Authorization': 'Bearer one'})
        assert refused.status_code == 422 and refused.json()['error'] == 'duplicate_data_key'
        missing = client.post('/v1/get_operation', json={'request_id': request_id},
                              headers={'Authorization': 'Bearer one'})
        assert missing.status_code == 404  # Nothing was admitted.


# --- criterion 1: progress --------------------------------------------------------------------------------------


# spec: ANYM-970
def test_a_poll_in_the_middle_shows_the_running_step_and_the_keys_read_so_far(tmp_path):
    app = create_app(host(tmp_path))

    async def call(device, command, **options):
        name = next(iter(command)) if isinstance(command, dict) else command
        if name == 'copyTextFrom':
            key = command['copyTextFrom']['key']
            return {'text': {'first': 'Tea House', 'second': '€12.50'}[key], 'key': key}
        if command == {'tapOn': 'Slow'}:
            await asyncio.sleep(1.6)
        return {'success': True}

    async def noop(*args):
        pass

    with TestClient(app) as client:
        app.state.devices.ui = SimpleNamespace(call=call, close=noop, ensure_fenced_process_stopped=noop)

        def post(name, body):
            response = client.post('/v1/' + name, json=body, headers={'Authorization': 'Bearer one'})
            assert response.status_code == 200, response.text
            return response.json()

        session = post('session', {'action': 'acquire', 'device_id': 'home'})['session_id']
        request_id = str(uuid.uuid4())
        source = ('- copyTextFrom: {text: Name, key: first}\n- tapOn: Slow\n'
                  '- copyTextFrom: {text: Total, key: second}\n')
        # The wait_s reply of android_run_flow comes back while the slow step runs.
        reply = post('run_flow', {'device_id': 'home', 'session_id': session, 'request_id': request_id,
                                  'yaml': source, 'wait_s': 1})
        assert reply['state'] == 'running'
        assert set(reply['progress']) == PROGRESS_KEYS
        assert reply['progress']['step_path'] == [1] and reply['progress']['step_name'] == 'tapOn'
        assert reply['progress']['finished_steps'] == 1 and reply['progress']['elapsed_ms'] >= 0
        assert reply['data'] == {'first': 'Tea House'}  # The keys finished so far, not the second.
        polled = post('get_operation', {'request_id': request_id, 'wait_s': 0})
        assert polled['state'] == 'running' and polled['progress'] == reply['progress'] and polled['data'] == reply['data']

        done = post('get_operation', {'request_id': request_id, 'wait_s': 5})
        assert done['state'] == 'succeeded' and done['data'] == {'first': 'Tea House', 'second': '€12.50'}
        assert 'progress' not in done  # Progress never changes the final receipt.
        assert [step['command'] for step in done['steps']] == ['copyTextFrom', 'tapOn', 'copyTextFrom']


# spec: ANYM-970
def test_progress_is_written_at_most_every_250_ms_and_the_final_receipt_has_none(tmp_path, monkeypatch):
    async def scenario():
        store = Store(tmp_path / 'state')
        devices = ui_devices(tmp_path, store)
        writes = []
        original = store.progress
        monkeypatch.setattr(store, 'progress', lambda *args: writes.append(time.monotonic()) or original(*args))

        async def call(device, command, **options):
            await asyncio.sleep(0.02)
            return {'success': True}
        monkeypatch.setattr(devices.ui, 'call', call)
        session = store.reserve('owner', 'phone', 'acquire')['session_id']
        flow = compiled('- back\n' * 40)
        try:
            started = time.monotonic()
            receipt = await devices.execute('owner', 'phone', session, str(uuid.uuid4()), None, 30, 30, flow)
            elapsed = time.monotonic() - started
            assert receipt['state'] == 'succeeded' and receipt['commands_executed'] == 40
            assert 2 <= len(writes) <= 2 + elapsed / 0.25  # Forty steps, about one write per quarter second.
            assert all(later - earlier >= 0.2 for earlier, later in zip(writes, writes[1:]))
            assert 'progress' not in receipt and set(receipt) >= {'steps', 'data', 'elapsed_ms', 'engine'}
        finally:
            await devices.close()
            store.close()
    asyncio.run(scenario())


# spec: ANYM-970
def test_progress_values_pass_the_secret_masking(tmp_path, monkeypatch):
    async def scenario():
        store = Store(tmp_path / 'state')
        devices = ui_devices(tmp_path, store)
        SecretStore(store.directory).set('SHOP_PASSWORD', PACKAGE, VALUE)
        gate = asyncio.Event()

        async def call(device, command, **options):
            if command == {'copyTextFrom': {'id': 'example.food:id/field', 'key': 'field'}}:
                return {'text': VALUE, 'key': 'field'}
            if command == {'tapOn': 'Slow'}:
                await gate.wait()
            return {'success': True}
        monkeypatch.setattr(devices.ui, 'call', call)
        session = store.reserve('owner', 'phone', 'acquire')['session_id']
        flow = compiled('- inputText: ${secret:SHOP_PASSWORD}\n- copyTextFrom: {id: "example.food:id/field", key: field}\n'
                        '- tapOn: Slow\n', secrets=lambda: {'SHOP_PASSWORD': PACKAGE})
        request_id = str(uuid.uuid4())
        try:
            first = await devices.execute('owner', 'phone', session, request_id, None, 30, 0, flow)
            assert first['state'] == 'running'
            receipt = first
            for _ in range(100):
                receipt = await devices.receipt('owner', request_id, 0)
                if receipt.get('progress', {}).get('step_name') == 'tapOn':
                    break
                await asyncio.sleep(0.05)
            assert receipt['progress']['step_name'] == 'tapOn' and receipt['progress']['finished_steps'] == 2
            assert receipt['data'] == {'field': '${secret:SHOP_PASSWORD}'} and VALUE not in json.dumps(receipt)
            stored = [path for path in tmp_path.rglob('*') if path.is_file() and path.name != 'secrets.json'
                      and VALUE.encode() in path.read_bytes()]
            assert stored == []  # Not in the SQLite file that holds the progress row either.
            gate.set()
            done = await devices.receipt('owner', request_id, 5)
            assert done['state'] == 'succeeded' and VALUE not in json.dumps(done)
        finally:
            gate.set()
            await devices.close()
            store.close()
    asyncio.run(scenario())


# spec: ANYM-970
def test_a_restart_leaves_the_last_progress_on_the_unknown_run_and_a_finished_run_ignores_progress(tmp_path):
    store = Store(tmp_path / 'state')
    request_id = str(uuid.uuid4())
    store.admit('owner', request_id, {'device_id': 'phone'}, metadata={'library': {'pins': []}})
    last = {'step_path': [3], 'step_name': 'tapOn', 'finished_steps': 3, 'elapsed_ms': 812.5}
    store.progress('owner', request_id, last, {'first': 'Tea House'})
    running = store.operation('owner', request_id)
    assert running['state'] == 'running' and running['progress'] == last and running['library'] == {'pins': []}
    store.close()
    store = Store(tmp_path / 'state')  # A crash: the existing restart rule marks the run unknown.
    crashed = store.operation('owner', request_id)
    assert crashed['state'] == 'unknown' and crashed['progress'] == last and crashed['data'] == {'first': 'Tea House'}
    finished = str(uuid.uuid4())
    store.admit('owner', finished, {'device_id': 'phone'})
    store.finish('owner', finished, 'succeeded', {'data': {}})
    store.progress('owner', finished, last, {'late': 'write'})
    assert store.operation('owner', finished) == {'request_id': finished, 'state': 'succeeded', 'data': {}}
    store.progress('owner', str(uuid.uuid4()), last, {})  # An unknown request id is ignored.
    store.close()


# spec: ANYM-970
def test_a_failing_progress_write_never_breaks_the_run(tmp_path, monkeypatch):
    async def scenario():
        store = Store(tmp_path / 'state')
        devices = ui_devices(tmp_path, store)

        def broken(*args):
            raise RuntimeError('database is locked')
        monkeypatch.setattr(store, 'progress', broken)

        async def call(device, command, **options):
            return {'success': True}
        monkeypatch.setattr(devices.ui, 'call', call)
        session = store.reserve('owner', 'phone', 'acquire')['session_id']
        try:
            receipt = await devices.execute('owner', 'phone', session, str(uuid.uuid4()), None, 10, 10, compiled('- back\n- back'))
            assert receipt['state'] == 'succeeded' and receipt['commands_executed'] == 2
        finally:
            await devices.close()
            store.close()
    asyncio.run(scenario())


# --- criterion 4: composite proof -------------------------------------------------------------------------------

FOOD_MAP = ('package: example.food\nselectors:\n  note: [{resourceId: "example.food:id/note"}]\n'
            '  basket: [{resourceId: "example.food:id/basket"}]\n'
            'screens:\n  home: {require: [{resourceId: "example.food:id/search"}]}\n')
FOOD_ORDER = '''map: food
side_effects: none
params:
  SHOP: {type: string}
  ITEM: {type: string}
  NOTE: {type: string}
steps:
  - openApp: {expect_screen: home}
  - tapOn: {id: "example.food:id/search"}
  - inputText: ${SHOP}
  - pressKey: Enter
  - tapOn: ${SHOP}
  - tapOn: ${ITEM}
  - tapOn: Large
  - tapOn: Add to basket
  - tapOn: {id: "example.food:id/note"}
  - typeInto: {selector: note, text: "${NOTE}"}
  - tapElement: {selector: basket}
  - extendedWaitUntil: {visible: {id: "example.food:id/total"}, timeout: 5000}
  - copyTextFrom: {id: "example.food:id/total", key: total}
'''


# spec: ANYM-970
def test_a_food_order_scenario_runs_in_one_call_waits_by_selector_and_returns_the_total(tmp_path):
    app = create_app(host(tmp_path))
    shop = FoodApp()
    seen = []

    async def call(device, command, **options):
        seen.append(command)
        if command == '__hierarchy__':
            return {'xml': shop.xml()}
        if command == '__guard__':
            Guard().screen(shop, when='after')
            return {'success': True}
        timeouts = {key: value for key, value in options.items() if key in {'element_timeout_ms', 'optional_timeout_ms'}}
        return execute(shop, command, guard=Guard(), **timeouts)

    async def noop(*args):
        pass

    with TestClient(app) as client:
        app.state.devices.ui = SimpleNamespace(call=call, close=noop, ensure_fenced_process_stopped=noop)

        def post(name, body):
            response = client.post('/v1/' + name, json=body, headers={'Authorization': 'Bearer one'})
            assert response.status_code == 200, response.text
            return response.json()

        post('flows', {'action': 'draft', 'kind': 'map', 'name': 'food', 'yaml': FOOD_MAP})
        session = post('session', {'action': 'acquire', 'device_id': 'home'})['session_id']
        receipt = post('run_flow', {
            'device_id': 'home', 'session_id': session, 'request_id': str(uuid.uuid4()), 'yaml': FOOD_ORDER,
            'stage': 'draft', 'parameters': {'SHOP': 'Tea House', 'ITEM': 'Jasmine Tea', 'NOTE': 'no sugar'},
            'wait_s': 20})
    assert receipt['state'] == 'succeeded', receipt
    assert receipt['data'] == {'total': '€12.50'} and 'progress' not in receipt
    commands = [step['command'] for step in receipt['steps']]
    assert commands == ['openApp', 'tapOn', 'inputText', 'pressKey', 'tapOn', 'tapOn', 'tapOn', 'tapOn', 'tapOn',
                        'typeInto', 'tapElement', 'extendedWaitUntil', 'copyTextFrom']
    assert 'sleep' not in commands  # The wait is a selector, not a pause.
    assert all(isinstance(step['elapsed_ms'], float) for step in receipt['steps'])
    waited = receipt['steps'][commands.index('extendedWaitUntil')]
    assert waited['result'] == {'matched': True} and waited['elapsed_ms'] > 100  # It waited for the total to load.
    assert receipt['steps'][-1]['result'] == {'text': '€12.50', 'key': 'total'}
    assert shop.texts['note'] == 'no sugar' and shop.screen == 'basket' and shop.placed == []  # Nothing was ordered.


# --- the fake device --------------------------------------------------------------------------------------------


class Field:
    """One matched node of the fake shop, as the worker reaches it."""

    def __init__(self, shop, selector):
        self.shop, self.selector = shop, selector

    @property
    def node(self):
        found = self.shop.find(self.selector)
        if not found:
            raise Refused('element_not_found')
        return found[0]

    @property
    def info(self):
        left, top, right, bottom = self.node['bounds']
        box = {'left': left, 'top': top, 'right': right, 'bottom': bottom}
        return {'visibleBounds': box, 'bounds': box, 'className': self.node['class'], 'text': self.node['text'],
                'resourceName': self.node['resource-id'], 'packageName': PACKAGE, 'focused': self.node['focused']}

    def center(self):
        left, top, right, bottom = self.node['bounds']
        return (left + right) // 2, (top + bottom) // 2

    def get_text(self, timeout=0):
        return self.node['text']

    def set_text(self, value, timeout=0):
        self.shop.texts[self.node['id']] = value


class FoodApp:
    """A small stateful shop: search, results, a menu, an option sheet and a basket whose total loads late."""

    def __init__(self, screen='home', ready_after=0.35):
        self.screen, self.focus, self.texts, self.size, self.added = screen, None, {}, None, False
        self.ready_after, self.ready_at, self.placed = ready_after, 0, []
        if screen == 'basket':
            self.ready_at = time.monotonic() + ready_after
        self._dev = SimpleNamespace(shell2=self.shell2)

    def nodes(self):
        views = {
            'home': [('EditText', '', 'search'), ('TextView', 'Popular near you', None)],
            'results': [('TextView', 'Tea House', 'shop_card'), ('TextView', 'Burger Barn', 'shop_card')],
            'menu': [('TextView', 'Jasmine Tea', 'item'), ('TextView', 'Mint Tea', 'item'),
                     *([('EditText', '', 'note'), ('TextView', 'View basket', 'basket')] if self.added else [])],
            'options': [('TextView', 'Choose a size', None), ('RadioButton', 'Large', 'size_large'),
                        ('Button', 'Add to basket', 'add')],
            'basket': ([('TextView', '€12.50', 'total')] if time.monotonic() >= self.ready_at
                       else [('TextView', 'Loading the total', None)]) + [('Button', 'Place order', 'place')],
        }[self.screen]
        nodes = []
        for index, (kind, text, ident) in enumerate(views):
            nodes.append({'class': 'android.widget.' + kind, 'id': ident, 'resource-id': f'{PACKAGE}:id/{ident}' if ident else '',
                          'text': self.texts.get(ident, text) if kind == 'EditText' else text, 'content-desc': '',
                          'bounds': (0, 100 * index, 1000, 100 * index + 80), 'focused': self.focus == ident and ident is not None,
                          'clickable': ident is not None or kind in {'Button', 'RadioButton'}})
        return nodes

    def xml(self):
        rows = ''.join(
            f'<node class="{node["class"]}" package="{PACKAGE}" resource-id="{node["resource-id"]}" text={quoteattr(node["text"])} '
            f'content-desc="" focused="{str(node["focused"]).lower()}" clickable="{str(node["clickable"]).lower()}" '
            f'password="false" bounds="[{node["bounds"][0]},{node["bounds"][1]}][{node["bounds"][2]},{node["bounds"][3]}]"/>'
            for node in self.nodes())
        return (f'<hierarchy><node class="android.widget.FrameLayout" package="{PACKAGE}" clickable="false" '
                f'bounds="[0,0][1000,2000]">{rows}</node></hierarchy>')

    def find(self, selector):
        def fits(node):
            for key, wanted in selector.items():
                if key == 'instance':
                    continue
                actual = {'textMatches': node['text'], 'text': node['text'], 'resourceIdMatches': node['resource-id'],
                          'resourceId': node['resource-id'], 'description': node['content-desc'],
                          'className': node['class'], 'focused': node['focused'], 'clickable': node['clickable'],
                          'packageName': PACKAGE}.get(key)
                if key.endswith('Matches'):
                    if re.fullmatch(wanted, actual or '') is None:
                        return False
                elif actual != wanted:
                    return False
            return True
        found = [node for node in self.nodes() if fits(node)]
        return found[selector['instance']:selector['instance'] + 1] if 'instance' in selector else found

    def jsonrpc_call(self, method, params=None, timeout=10):
        if method == 'dumpWindowHierarchy':
            return self.xml()
        if method in {'waitForExists', 'waitUntilGone'}:
            selector, wait_ms = params
            deadline = time.monotonic() + wait_ms / 1000
            while True:
                present = bool(self.find(selector))
                if present == (method == 'waitForExists') or time.monotonic() >= deadline:
                    return present == (method == 'waitForExists')
                time.sleep(0.02)
        return True

    def __call__(self, **selector):
        return Field(self, selector)

    def app_current(self):
        return {'package': PACKAGE, 'activity': '.Main'}

    def window_size(self):
        return 1000, 2000

    def shell2(self, args, timeout=None, encoding='utf-8'):
        output = 'priority=0\nexample.food/.Main\n' if args[:3] == ['cmd', 'package', 'resolve-activity'] else 'Status: ok\n'
        return SimpleNamespace(returncode=0, output=output)

    def click(self, x, y):
        hit = next((node for node in reversed(self.nodes()) if node['clickable'] and
                    node['bounds'][0] <= x < node['bounds'][2] and node['bounds'][1] <= y < node['bounds'][3]), None)
        if hit is None:
            return
        ident, text = hit['id'], hit['text']
        if self.screen == 'home' and ident == 'search':
            self.focus = 'search'
        elif self.screen == 'results' and ident == 'shop_card' and text == 'Tea House':
            self.screen = 'menu'
        elif self.screen == 'menu' and ident == 'item' and text == 'Jasmine Tea':
            self.screen = 'options'
        elif self.screen == 'menu' and ident == 'note':
            self.focus = 'note'
        elif self.screen == 'menu' and ident == 'basket':
            self.screen, self.ready_at = 'basket', time.monotonic() + self.ready_after
        elif self.screen == 'options' and ident == 'size_large':
            self.size = 'Large'
        elif self.screen == 'options' and ident == 'add' and self.size:
            self.screen, self.added = 'menu', True
        elif self.screen == 'basket' and ident == 'place':
            self.placed.append('order')  # The guard must never let a run reach this.

    def press(self, key):
        if key == 'enter' and self.screen == 'home' and self.texts.get('search'):
            self.screen = 'results'
