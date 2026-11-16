"""The courier call sheet probe (ANYM-976): a synthetic Gojek order and call sheet, no phone, no call, no provider."""

import asyncio
import base64
import hashlib
import importlib
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient

from deedkit_android.app import create_app
from deedkit_android.store import Store
from test_courier_chat import PNG, element, inside, service_config

ORDER = 'Warung Sate'
WORDS = f'Open the call sheet of the {ORDER} courier, I want to call him'
CALL, CHAT = (600, 1900, 780, 2000), (800, 1900, 1000, 2000)
REGULAR, INTERNET, CHANGE = (40, 1500, 1040, 1600), (40, 1620, 1040, 1720), (800, 1380, 1040, 1460)
NUMBER_TEXT = '+62 21-0000-0123'  # synthetic: a masked-number shape, no real line
DIALER = 'com.google.android.dialer'


def probe():
    return importlib.import_module('deedkit_android.courier_call')


def centre(box):
    return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2


class Phone:
    """A scripted order screen whose call entry opens a call sheet; every tap is recorded."""

    def __init__(self, state='order', **switches):
        self.state, self.switches, self.taps, self.ui_calls, self.adb_taps = state, switches, [], [], []
        self.png = None

    def elements(self):
        extra = list(self.switches.get('extra', ()))
        if self.state == 'dialer':
            return [element('Calling', (40, 100, 600, 180), package=DIALER)]
        found = [element(f'Your order {ORDER}', (40, 100, 600, 180)), element('Payment: Cash', (40, 300, 600, 380)),
                 element('Chat', CHAT, True, 'android.widget.Button')]
        if not self.switches.get('no_entry'):
            found.append(element('Call', CALL, True, 'android.widget.Button'))
        if self.state == 'sheet':
            found += [element('Call driver', (40, 1300, 600, 1360))]
            if not self.switches.get('no_number'):
                found.append(element(f'Masked number {NUMBER_TEXT}', (40, 1380, 760, 1460)))
            if self.switches.get('change'):
                found.append(element('Change', CHANGE, True, 'android.widget.Button'))
            found += [element('Regular call', REGULAR, True, 'android.widget.Button'),
                      element('Free internet call', INTERNET, True, 'android.widget.Button')]
        return found + extra

    async def call(self, device, command, **options):
        self.ui_calls.append(command)
        name, value = next(iter(command.items()))
        assert name == 'tapOn', command  # the probe types nothing and presses no key
        x, y = (int(part) for part in value['point'].split(','))
        self.taps.append((x, y))
        if self.state == 'order' and inside(CALL, x, y) and not self.switches.get('never_opens'):
            self.state = 'dialer' if self.switches.get('dials_at_once') else 'sheet'
        self.png = self.state.encode()
        return {'success': True}

    async def ensure_fenced_process_stopped(self, device):
        return None

    async def close(self):
        return None

    async def adb(self, argv, timeout, *args, **kwargs):
        self.adb_taps.append(argv)
        return 0, b'', b''


@pytest.fixture
def env(tmp_path, monkeypatch):
    from deedkit_android.device import Devices

    monkeypatch.setattr('deedkit_android.courier_chat.POLL_S', 0)
    monkeypatch.setattr('deedkit_android.courier_call.OPEN_WAIT_S', 0.05)

    def build(state='order', **switches):
        phone = Phone(state, **switches)
        store = Store(tmp_path / ('state' + uuid.uuid4().hex))
        devices = Devices(SimpleNamespace(devices={'phone': 'SERIAL'}, adb='/synthetic', uiautomator2_jar='/synthetic',
                                          state_dir=str(tmp_path / 'state')), store)
        devices.ui = phone

        async def hierarchy(device):
            if switches.get('lose_hierarchy_after_tap') and phone.taps:
                return {'hierarchy_error': 'unavailable'}
            return {'hierarchy_source': 'uiautomator2', 'elements': [dict(item) for item in phone.elements()]}

        async def image(owner, device, mode, crop):
            png = PNG + (phone.png or b'order-screen')
            if mode == 'file':
                return {'image_file': {**store.save(owner, png), 'mime_type': 'image/png'}}
            return {'image': {'mime_type': 'image/png', 'data': base64.b64encode(png).decode()}}

        monkeypatch.setattr(devices, 'capture_hierarchy', hierarchy)
        monkeypatch.setattr(devices, 'capture_image', image)
        monkeypatch.setattr('deedkit_android.courier_chat.run_process', phone.adb)
        token = store.reserve('owner', 'phone', 'acquire')['session_id']
        return SimpleNamespace(phone=phone, store=store, devices=devices, token=token)
    return SimpleNamespace(build=build)


async def call(world, request_id=None, **overrides):
    args = {'device_id': 'phone', 'session_id': world.token, 'request_id': request_id or str(uuid.uuid4()),
            'app': 'gojek', 'order': ORDER, 'owner_instruction': WORDS, 'wait_s': 5, **overrides}
    return await probe().handle(world.devices, 'owner', args)


def drive(env, scenario, state='order', **switches):
    world = env.build(state, **switches)

    async def main():
        try:
            return await scenario(world)
        finally:
            await world.devices.close()
            world.store.close()
    return world, asyncio.run(main())


def no_forbidden_tap(phone, forbidden):
    return not [point for point in phone.taps for box in forbidden if inside(box, *point)]


# spec: ANYM-976
@pytest.mark.parametrize('words,field', [
    (None, 'owner_instruction'),
    ('ok', 'owner_instruction'),
    ('Call the courier please', 'owner_instruction'),
    (f'Open the {ORDER} chat and say hi', 'owner_instruction'),
    (f'Recall the {ORDER} courier details', 'owner_instruction'),
], ids=['missing', 'too-short', 'no-order', 'no-call-word', 'call-inside-a-word'])
def test_the_probe_needs_the_owners_words_naming_the_order_and_a_call_before_any_device_call(tmp_path, monkeypatch,
                                                                                             words, field):
    with TestClient(create_app(service_config(tmp_path))) as client:
        calls = []

        async def execute(*args, **kwargs):
            calls.append(args)
            return {'request_id': args[3], 'state': 'succeeded'}
        monkeypatch.setattr(client.app.state.devices, 'execute', execute)
        body = {'device_id': 'home', 'session_id': str(uuid.uuid4()), 'request_id': str(uuid.uuid4()), 'app': 'gojek',
                'order': ORDER, **({'owner_instruction': words} if words is not None else {})}
        response = client.post('/v1/courier_call_sheet', json=body, headers={'Authorization': 'Bearer one'})
        answer = response.json()
        assert response.status_code == 422 and answer['error'] == 'invalid_arguments', answer
        assert [entry['field'] for entry in answer['field_errors']] == [field] and calls == []


# spec: ANYM-976
@pytest.mark.parametrize('words', [WORDS, f'TELEPON   kurir {ORDER.upper()} ya', f'tolong panggil driver {ORDER.lower()}'],
                         ids=['call', 'telepon-case-and-spaces', 'panggil'])
def test_the_owners_call_words_reach_the_device_once_and_the_operation_is_a_control_one(tmp_path, monkeypatch, words):
    with TestClient(create_app(service_config(tmp_path))) as client:
        calls = []

        async def execute(*args, **kwargs):
            calls.append((args, kwargs))
            return {'request_id': args[3], 'state': 'succeeded'}
        monkeypatch.setattr(client.app.state.devices, 'execute', execute)
        body = {'device_id': 'home', 'session_id': str(uuid.uuid4()), 'request_id': str(uuid.uuid4()), 'app': 'gojek',
                'order': ORDER, 'owner_instruction': words}
        assert client.post('/v1/courier_call_sheet', json=body, headers={'Authorization': 'Bearer reader'}).status_code == 403
        assert client.post('/v1/courier_call_sheet', json=body, headers={'Authorization': 'Bearer one'}).status_code == 200
        assert len(calls) == 1 and calls[0][1]['job'].identity['owner_instruction'] == words
        capabilities = client.post('/v1/capabilities', json={}, headers={'Authorization': 'Bearer one'}).json()
        assert 'courier_call_sheet' in capabilities['tool_manifest']['names']
        assert set(capabilities['input_schemas']['courier_call_sheet']['properties']) == {
            'device_id', 'session_id', 'request_id', 'app', 'order', 'owner_instruction', 'wait_s'}


# spec: ANYM-976
def test_the_probe_taps_the_call_entry_once_and_reads_the_masked_number(env):
    world, receipt = drive(env, lambda world: call(world))
    phone = world.phone
    assert receipt['state'] == 'succeeded' and receipt['tapped_call_entry'] is True
    assert phone.taps == [centre(CALL)] and phone.adb_taps == [] and phone.state == 'sheet'
    assert receipt['number'] == '+622100000123' and receipt['change_visible'] is False and receipt['sheet_visible'] is True
    assert f'Masked number {NUMBER_TEXT}' in receipt['sheet'] and 'Regular call' in receipt['sheet']
    assert receipt['screenshot_before_sha256'] == hashlib.sha256(PNG + b'order-screen').hexdigest()
    assert receipt['screenshot_after_sha256'] == hashlib.sha256(PNG + b'sheet').hexdigest()
    assert receipt['owner_instruction'] == WORDS and receipt['order'] == ORDER and receipt['automatic_retry'] is False
    assert receipt['request_id'] in receipt['dial_evidence'] and ORDER in receipt['dial_evidence']
    assert len(receipt['dial_evidence']) <= 300 and 'sent' not in receipt and 'image' not in receipt
    assert 'dial_only_on_the_owners_yes' in receipt['next_action']


# spec: ANYM-976
def test_a_sheet_without_a_number_returns_null_after_the_one_tap(env):
    world, receipt = drive(env, lambda world: call(world), no_number=True)
    assert receipt['state'] == 'succeeded' and receipt['number'] is None and receipt['dial_evidence'] is None
    assert world.phone.taps == [centre(CALL)] and receipt['next_action'] == 'tell_the_owner_no_number_was_shown_and_stop'


# spec: ANYM-976
def test_a_change_option_is_reported_and_never_tapped(env):
    world, receipt = drive(env, lambda world: call(world), change=True)
    assert receipt['state'] == 'succeeded' and receipt['change_visible'] is True and receipt['number'] == '+622100000123'
    assert world.phone.taps == [centre(CALL)] and no_forbidden_tap(world.phone, (CHANGE, REGULAR, INTERNET))


# spec: ANYM-976
def test_option_and_money_nodes_beside_the_entry_are_never_targets(env):
    beside = [element('Panggilan biasa', (100, 1800, 300, 1880), True), element('Free internet call', (320, 1800, 500, 1880), True),
              element('Ubah', (520, 1800, 580, 1880), True), element('Bayar', (100, 1700, 300, 1780), False),
              element('Telepon', (320, 1700, 500, 1780), True, **{'content-desc': 'Regular call'}),
              element('Panggilan internet gratis', (520, 1700, 700, 1780), True)]
    world, receipt = drive(env, lambda world: call(world), extra=beside)
    assert receipt['state'] == 'succeeded'
    assert world.phone.taps == [centre(CALL)]
    assert no_forbidden_tap(world.phone, [tuple(map(int, node['bounds'].replace('][', ',').strip('[]').split(',')))
                                          for node in beside])


# spec: ANYM-976
@pytest.mark.parametrize('switches,code', [
    ({'no_entry': True}, 'call_entry_not_found'),
    ({'extra': [element('Telepon', (100, 1900, 300, 2000), True)]}, 'call_entry_ambiguous'),
    ({'extra': [element('Regular call', (580, 1880, 800, 2020), True)]}, 'call_entry_covered'),
], ids=['absent', 'ambiguous', 'covered-by-an-option'])
def test_an_absent_ambiguous_or_covered_entry_fails_with_no_tap(env, switches, code):
    world, receipt = drive(env, lambda world: call(world), **switches)
    assert receipt['state'] == 'failed' and receipt['error'] == code and receipt['side_effects'] == 'none'
    assert receipt['tapped_call_entry'] is False and world.phone.taps == [] and world.phone.adb_taps == []


# spec: ANYM-976
def test_an_entry_that_opens_nothing_is_one_tap_and_no_number(env):
    world, receipt = drive(env, lambda world: call(world), never_opens=True)
    assert receipt['state'] == 'succeeded' and receipt['sheet_visible'] is False and receipt['number'] is None
    assert world.phone.taps == [centre(CALL)]


# spec: ANYM-976
@pytest.mark.parametrize('switches', [{'dials_at_once': True}, {'lose_hierarchy_after_tap': True}],
                         ids=['dialer-opens', 'hierarchy-lost'])
def test_an_uncertain_tap_is_unknown_and_its_request_id_replays_without_a_second_tap(env, switches):
    request = str(uuid.uuid4())

    async def scenario(world):
        first = await call(world, request_id=request)
        taps = list(world.phone.taps)
        again = await call(world, request_id=request)
        return first, again, taps
    world, (first, again, taps) = drive(env, scenario, **switches)
    assert first['state'] == 'unknown' and first['side_effects'] == 'may_have_occurred'
    assert first['tapped_call_entry'] is True and first['automatic_retry'] is False
    assert first['next_action'] == probe().NEXT_UNKNOWN and 'number' not in first
    assert again == first and taps == world.phone.taps == [centre(CALL)]


# spec: ANYM-976
def test_a_payment_screen_stops_the_probe_before_the_tap(env):
    world, receipt = drive(env, lambda world: call(world), extra=[element('Bayar Rp45.000', (100, 1500, 400, 1600), True)])
    assert receipt['state'] == 'stopped' and receipt['guard']['rule'] == 'payment_screen'
    assert receipt['owner_instruction'] == WORDS and world.phone.taps == []


# spec: ANYM-976
def test_another_app_on_screen_gets_no_tap(env):
    world, receipt = drive(env, lambda world: call(world, app='grab'))
    assert receipt['state'] == 'failed' and receipt['error'] == 'not_courier_app' and world.phone.taps == []
