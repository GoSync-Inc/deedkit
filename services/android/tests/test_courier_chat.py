"""The courier chat tool (ANYM-975): a synthetic Gojek order screen, no phone and no provider."""

import asyncio
import base64
import hashlib
import importlib
import json
import time
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient

from deedkit_android.app import create_app
from deedkit_android.config import Refused
from deedkit_android.guard import Stop
from deedkit_android.store import Store

PACKAGE = 'com.gojek.app'
ORDER, MESSAGE = 'Warung Sate', 'Saya di lobi, ya'
CONFIRMATION = f'Yes, send "{MESSAGE}" to the courier of {ORDER}'
CHAT_ENTRY, FIELD, SEND = (800, 1900, 1000, 2000), (60, 2100, 880, 2220), (900, 2100, 1040, 2220)
PNG = b'\x89PNG\r\n\x1a\n'


def cc():
    return importlib.import_module('deedkit_android.courier_chat')


def bounds(box):
    return '[%d,%d][%d,%d]' % box


def centre(box):
    return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2


def inside(box, x, y):
    return box[0] <= x < box[2] and box[1] <= y < box[3]


def element(text='', box=(0, 0, 10, 10), clickable=False, cls='android.widget.TextView', package=PACKAGE, **attrs):
    return {'text': text, 'content-desc': '', 'resource-id': '', 'class': cls, 'package': package,
            'bounds': bounds(box), 'clickable': str(clickable).lower(), 'enabled': 'true', 'focused': 'false',
            'visible-to-user': 'true', **attrs}


class Phone:
    """A scripted order screen: order -> chat -> typed -> sent, with switches for each failure."""

    def __init__(self, state='order', **switches):
        self.state, self.typed, self.focused, self.switches = state, switches.pop('draft', ''), False, switches
        self.ui_calls, self.adb_taps = [], []
        self.png = None
        self.incoming = []

    # What the hierarchy shows.
    def elements(self):
        extra = list(self.switches.get('extra', ()))
        if self.state == 'order':
            found = [element('Your order', (40, 100, 600, 180)), element('Payment: Cash', (40, 300, 600, 380)),
                     element('Chat', CHAT_ENTRY, True, 'android.widget.Button'),
                     element('Call', (600, 1900, 780, 2000), True, 'android.widget.Button')]
            if self.switches.get('no_chat_entry'):
                found = [item for item in found if item['text'] != 'Chat']
            return found + extra
        found = [element('Driver', (40, 100, 600, 180)), element('On my way', (40, 300, 500, 380))]
        found += [element(text, (40, 500 + 60 * index, 500, 540 + 60 * index)) for index, text in enumerate(self.incoming)]
        shown = self.typed or 'Type a message'
        if self.state == 'sent':
            found.append(element(MESSAGE, (560, 420, 1040, 500)))
        found.append(element(shown, FIELD, True, 'android.widget.EditText', hint='Type a message',
                             focused=str(self.focused).lower()))
        if not self.switches.get('no_send'):
            found.append(element('', SEND, True, 'android.widget.ImageButton', **{'content-desc': 'Send'}))
        if self.state == 'typed' and self.switches.get('pay_after_typed'):
            found.append(element('Bayar Rp45.000', (100, 1500, 400, 1600), True, 'android.widget.Button'))
        return found + extra

    # The UI engine: taps by point and text input.
    async def call(self, device, command, **options):
        self.ui_calls.append(command)
        if self.switches.get('engine_stop'):
            raise Stop('write_denied', 'com.example.chat', 'com.example.chat')
        name, value = next(iter(command.items()))
        if name == 'tapOn':
            x, y = (int(part) for part in value['point'].split(','))
            if self.state == 'order' and inside(CHAT_ENTRY, x, y):
                self.state = 'chat'
            elif self.state != 'order' and inside(FIELD, x, y):
                self.focused = True
        elif name == 'eraseText':
            self.typed = ''
        elif name == 'inputText':
            if self.switches.get('input_fails'):
                raise Refused('uiautomator_transport_error', 409)
            if not self.focused:
                raise Refused('focused_field_not_found', 409)
            self.typed += value + ('!' if self.switches.get('autocorrect') else '')
            self.state = 'typed'
        else:
            raise AssertionError(command)
        self.png = (self.state + str(self.typed)).encode()
        return {'success': True}

    async def ensure_fenced_process_stopped(self, device):
        return None

    async def close(self):
        return None

    # adb: only the send tap goes here.
    async def adb(self, argv, timeout, *args, **kwargs):
        assert argv[-5:-2] == ['shell', 'input', 'tap'], argv
        x, y = int(argv[-2]), int(argv[-1])
        self.adb_taps.append((x, y))
        if self.switches.get('tap_times_out'):
            raise TimeoutError
        if self.state == 'typed' and inside(SEND, x, y):
            if not self.switches.get('never_seen'):
                self.state = 'sent'
            self.typed = ''  # The app clears the field on send.
            self.png = (self.state + 'after').encode()
        return 0, b'', b''


@pytest.fixture
def env(tmp_path, monkeypatch):
    from deedkit_android.device import Devices

    holder = SimpleNamespace(monkeypatch=monkeypatch)
    monkeypatch.setattr('deedkit_android.courier_chat.POLL_S', 0, raising=False)
    monkeypatch.setattr('deedkit_android.courier_chat.SEND_WAIT_S', 0.05, raising=False)

    def build(state='order', **switches):
        phone = Phone(state, **switches)
        store = Store(tmp_path / ('state' + uuid.uuid4().hex))
        devices = Devices(SimpleNamespace(devices={'phone': 'SERIAL'}, adb='/synthetic', uiautomator2_jar='/synthetic',
                                          state_dir=str(tmp_path / 'state')), store)
        devices.ui = phone

        async def hierarchy(device):
            if switches.get('lose_hierarchy_after_tap') and phone.adb_taps:
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
        return SimpleNamespace(phone=phone, store=store, devices=devices, token=token, monkeypatch=monkeypatch)

    holder.build = build
    return holder


async def call(world, action, request_id=None, wait=5, **overrides):
    """What the route does: the pre-device checks, then the job under the device lock."""
    request_id = request_id or str(uuid.uuid4())
    args = {'action': action, 'device_id': 'phone', 'session_id': world.token, 'request_id': request_id,
            'app': 'gojek', 'order': ORDER, 'message': MESSAGE, 'timeout_s': 60, **overrides}
    if action == 'send':
        args.setdefault('owner_instruction', CONFIRMATION)
    cc().check_request('owner', args)
    job = cc().CourierChat(world.devices, 'owner', 'phone', args)
    return await world.devices.execute('owner', 'phone', world.token, request_id, None, 60, wait, job=job)


async def previewed_and_sent(world, **overrides):
    preview = await call(world, 'preview', **overrides)
    return preview, await call(world, 'send', preview_digest=preview['preview_digest'], **overrides)


def drive(env, scenario, state='order', **switches):
    world = env.build(state, **switches)

    async def main():
        try:
            return await scenario(world)
        finally:
            await world.devices.close()
            world.store.close()
    return world, asyncio.run(main())


def ui_points(phone):
    return [tuple(int(part) for part in command['tapOn']['point'].split(',')) for command in phone.ui_calls
            if isinstance(command, dict) and 'tapOn' in command]


def service_config(tmp_path, permissions=('android:control', 'android:read')):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'adb': '/synthetic', 'state_dir': str(tmp_path / 'state'), 'devices': {'home': 'SERIAL'},
                                  'principals': [{'id': 'one', 'key_sha256': hashlib.sha256(b'one').hexdigest(),
                                                  'devices': ['home'], 'permissions': list(permissions)},
                                                 {'id': 'reader', 'key_sha256': hashlib.sha256(b'reader').hexdigest(),
                                                  'devices': ['home'], 'permissions': ['android:read']}]}))
    config.chmod(0o600)
    return config


def digest_for(principal, args, chat='x', delta=600):
    expires, chat_sha = int(time.time()) + delta, hashlib.sha256(chat.encode()).hexdigest()
    return f'{expires}.{chat_sha}.{cc().sign(principal, args, chat_sha, expires)}'


# spec: ANYM-975
def test_a_send_needs_a_valid_unexpired_digest_before_any_device_call(tmp_path, monkeypatch):
    with TestClient(create_app(service_config(tmp_path))) as client:
        calls = []

        async def execute(*args, **kwargs):
            calls.append(args)
            return {'request_id': args[3], 'state': 'succeeded'}
        monkeypatch.setattr(client.app.state.devices, 'execute', execute)
        base = {'device_id': 'home', 'session_id': str(uuid.uuid4()), 'app': 'gojek', 'order': ORDER, 'message': MESSAGE}

        def post(**body):
            return client.post('/v1/courier_chat', headers={'Authorization': 'Bearer one'},
                               json={'request_id': str(uuid.uuid4()), **base, **body})
        good = digest_for('one', base)
        refused = {
            'preview_required': {'action': 'send', 'owner_instruction': CONFIRMATION},
            'preview_mismatch': [
                {'action': 'send', 'owner_instruction': CONFIRMATION, 'preview_digest': 'garbage'},
                {'action': 'send', 'owner_instruction': CONFIRMATION, 'preview_digest': good[:-1] + ('0' if good[-1] != '0' else '1')},
                {'action': 'send', 'owner_instruction': CONFIRMATION, 'preview_digest': good, 'message': 'Another text'},
                {'action': 'send', 'owner_instruction': CONFIRMATION, 'preview_digest': good, 'order': 'Bakso Pak Eko'},
                {'action': 'send', 'owner_instruction': CONFIRMATION, 'preview_digest': digest_for('two', base)},
            ],
            'preview_expired': {'action': 'send', 'owner_instruction': CONFIRMATION,
                                'preview_digest': digest_for('one', base, delta=-5)},
        }
        for code, bodies in refused.items():
            for body in bodies if isinstance(bodies, list) else [bodies]:
                response = post(**body)
                answer = response.json()
                assert response.status_code == 409 and answer['error'] == code, (code, body, answer)
                assert answer['next_action'] == 'preview_then_send_with_digest' and answer['hint']
                assert answer['next_call'] == {'tool': 'android_courier_chat', 'caller_supplies': [
                    'session_id', 'request_id', 'message'], 'arguments': {
                    'action': 'preview', 'device_id': 'home', 'app': 'gojek', 'order': body.get('order', ORDER)}}
        assert calls == [] and 'preview_digest' not in answer
        assert post(action='send', preview_digest=good).status_code == 422  # the owner's reply is required
        assert post(action='send', preview_digest=good, owner_instruction=CONFIRMATION).status_code == 200
        assert post(action='preview').status_code == 200
        assert len(calls) == 2


# spec: ANYM-975
def test_a_preview_takes_no_digest_and_arguments_are_checked(tmp_path):
    with TestClient(create_app(service_config(tmp_path))) as client:
        base = {'device_id': 'home', 'session_id': str(uuid.uuid4()), 'request_id': str(uuid.uuid4()), 'action': 'preview',
                'app': 'gojek', 'order': ORDER, 'message': MESSAGE}
        for change in ({'message': 'line one\nline two'}, {'message': ''}, {'message': 'x' * 501}, {'order': 'ab'},
                       {'app': 'whatsapp'}, {'action': 'type'}, {'preview_digest': 'x'}, {'owner_instruction': 'Yes please'}):
            response = client.post('/v1/courier_chat', json={**base, **change}, headers={'Authorization': 'Bearer one'})
            assert response.status_code == 422 and response.json()['error'] == 'invalid_arguments', change
        # The operation is a control one and the capability list names it.
        assert client.post('/v1/courier_chat', json=base, headers={'Authorization': 'Bearer reader'}).status_code == 403
        capabilities = client.post('/v1/capabilities', json={}, headers={'Authorization': 'Bearer one'}).json()
        assert 'courier_chat' in capabilities['tool_manifest']['names']


# spec: ANYM-975
def test_a_preview_opens_the_chat_types_the_text_and_returns_a_digest_without_a_send_tap(env):
    world, receipt = drive(env, lambda world: call(world, 'preview'))
    phone = world.phone
    assert receipt['state'] == 'succeeded' and receipt['previewed'] is True and receipt['sent'] is False
    assert ui_points(phone) == [centre(CHAT_ENTRY), centre(FIELD)] and {'inputText': MESSAGE} in phone.ui_calls
    assert phone.adb_taps == [] and phone.typed == MESSAGE
    assert receipt['text'] == MESSAGE and receipt['target'] == {'app': 'gojek', 'package': PACKAGE, 'order': ORDER}
    assert receipt['chat'] == ['Driver', 'On my way'] and receipt['side_effects'] == 'none'
    assert receipt['screen']['image_file']['file_id'] and receipt['screen']['elements']
    expires, chat_sha, mac = receipt['preview_digest'].split('.')
    assert int(expires) == receipt['preview_digest_expires_at'] and abs(int(expires) - time.time() - 600) < 30
    assert chat_sha == hashlib.sha256('\n'.join(receipt['chat']).encode()).hexdigest() and len(mac) == 64
    assert receipt['untrusted'] and 'never' in receipt['untrusted']


# spec: ANYM-975
def test_a_preview_from_an_open_chat_keeps_or_replaces_a_draft(env):
    world, receipt = drive(env, lambda world: call(world, 'preview'), state='chat', draft='old draft')
    assert receipt['state'] == 'succeeded' and world.phone.typed == MESSAGE
    assert ui_points(world.phone) == [centre(FIELD)]
    assert world.phone.ui_calls[-2:] == [{'eraseText': {}}, {'inputText': MESSAGE}]
    world, receipt = drive(env, lambda world: call(world, 'preview'), state='chat', draft=MESSAGE)
    assert receipt['state'] == 'succeeded' and world.phone.typed == MESSAGE
    assert not [command for command in world.phone.ui_calls if 'inputText' in command or 'eraseText' in command]


# spec: ANYM-975
def test_text_that_does_not_read_back_exactly_gives_no_preview(env):
    world, receipt = drive(env, lambda world: call(world, 'preview'), autocorrect=True)
    assert receipt['state'] == 'failed' and receipt['error'] == 'typed_text_mismatch'
    assert receipt['side_effects'] == 'none' and 'preview_digest' not in receipt and world.phone.adb_taps == []


# spec: ANYM-975
def test_a_send_after_its_preview_taps_send_once_and_returns_the_chat(env):
    world, (preview, sent) = drive(env, previewed_and_sent)
    assert sent['state'] == 'succeeded' and sent['sent'] is True and sent['side_effects'] == 'sent'
    assert world.phone.adb_taps == [centre(SEND)]
    assert sent['chat'][-1] == MESSAGE and 'On my way' in sent['chat'] and len(sent['chat']) <= 10
    assert 'preview_digest' not in sent and sent['owner_instruction'] == CONFIRMATION
    assert sent['next_action'] == 'read_replies_with_android_observe' and sent['automatic_retry'] is False


# spec: ANYM-975
@pytest.mark.parametrize('change', ['draft', 'courier-message', 'app'])
def test_a_changed_field_chat_or_app_between_preview_and_send_is_a_preview_mismatch(env, change):
    async def scenario(world):
        preview = await call(world, 'preview')
        if change == 'draft':
            world.phone.typed = 'something else'
        elif change == 'courier-message':
            world.phone.incoming.append('Sudah sampai')
        else:
            world.phone.switches['extra'] = [element('x', (0, 0, 5, 5), package='com.whatsapp')]
        return await call(world, 'send', preview_digest=preview['preview_digest'])
    world, sent = drive(env, scenario)
    assert sent['state'] == 'failed' and sent['error'] == 'preview_mismatch' and sent['side_effects'] == 'none'
    assert sent['next_call']['arguments']['action'] == 'preview' and world.phone.adb_taps == []
    if change == 'app':
        assert sent['observation'] == {'withheld': 'not_courier_app', 'expected_package': PACKAGE}


# spec: ANYM-975
def test_a_digest_is_single_use_even_with_a_new_request_id(env):
    async def scenario(world):
        preview, first = await previewed_and_sent(world)
        again = await call(world, 'send', preview_digest=preview['preview_digest'])
        return first, again
    world, (first, again) = drive(env, scenario)
    assert first['state'] == 'succeeded'
    assert again['state'] == 'failed' and again['error'] == 'preview_already_used'
    assert again['earlier_request_id'] and again['next_call']['arguments']['action'] == 'preview'
    assert world.phone.adb_taps == [centre(SEND)]


# spec: ANYM-975
def test_an_expired_digest_is_refused_on_the_send(env):
    async def scenario(world):
        preview = await call(world, 'preview')
        world.monkeypatch.setattr('deedkit_android.courier_chat.clock', lambda: time.time() + 700)
        with pytest.raises(Refused) as caught:
            await call(world, 'send', preview_digest=preview['preview_digest'])
        return caught.value
    world, error = drive(env, scenario)
    assert error.code == 'preview_expired' and error.extra['next_call']['tool'] == 'android_courier_chat'
    assert world.phone.adb_taps == []


# spec: ANYM-975
@pytest.mark.parametrize('overrides,switches', [
    ({'app': 'grab'}, {}),
    ({}, {'extra': [element('x', (0, 0, 5, 5), package='com.whatsapp')]}),
    ({}, {'extra': [element('x', (0, 0, 5, 5), package='com.google.android.dialer')]}),
], ids=['grab-asked-gojek-shown', 'messenger-window', 'dialer-window'])
def test_only_the_courier_app_may_receive_input(env, overrides, switches):
    world, receipt = drive(env, lambda world: call(world, 'preview', **overrides), **switches)
    assert receipt['state'] == 'failed' and receipt['error'] == 'not_courier_app' and receipt['side_effects'] == 'none'
    assert world.phone.ui_calls == [] and world.phone.adb_taps == []


# spec: ANYM-975
def test_input_lands_on_three_labelled_nodes_never_on_call_change_or_pay_nodes(env):
    near = [element('Telepon', (100, 1800, 300, 1880), True), element('Panggil', (320, 1800, 500, 1880), True),
            element('Regular call', (100, 1700, 300, 1780), True), element('Free internet call', (320, 1700, 500, 1780), True),
            element('Change', (520, 1700, 700, 1780), True), element('Bayar', (100, 1600, 300, 1680), False)]
    world, (preview, sent) = drive(env, previewed_and_sent, extra=near)
    assert sent['state'] == 'succeeded'
    assert set(ui_points(world.phone)) | set(world.phone.adb_taps) <= {centre(CHAT_ENTRY), centre(FIELD), centre(SEND)}


# spec: ANYM-975
def test_a_node_that_also_carries_a_call_or_pay_label_is_never_a_target(env):
    both = element('Chat', (800, 1700, 1000, 1800), True, 'android.widget.Button', **{'content-desc': 'Panggil'})
    world, receipt = drive(env, lambda world: call(world, 'preview'), no_chat_entry=True, extra=[both])
    assert receipt['state'] == 'failed' and receipt['error'] == 'chat_entry_not_found'
    assert world.phone.ui_calls == [] and world.phone.adb_taps == []


# spec: ANYM-975
def test_a_missing_chat_entry_fails_with_no_side_effect_and_the_screen(env):
    world, receipt = drive(env, lambda world: call(world, 'preview'), no_chat_entry=True)
    assert receipt['state'] == 'failed' and receipt['error'] == 'chat_entry_not_found'
    assert receipt['side_effects'] == 'none' and receipt['automatic_retry'] is False
    assert receipt['observation']['image_file']['file_id'] and world.phone.ui_calls == []


# spec: ANYM-975
def test_a_missing_send_node_fails_the_send_with_no_side_effect_and_the_screen(env):
    world, (preview, sent) = drive(env, previewed_and_sent, state='chat', no_send=True)
    assert preview['state'] == 'succeeded'
    assert sent['state'] == 'failed' and sent['error'] == 'send_button_not_found'
    assert sent['side_effects'] == 'none' and sent['observation']['image_file']['file_id'] and world.phone.adb_taps == []


# spec: ANYM-975
@pytest.mark.parametrize('extra,rule,sensitive', [
    ([element('Masukkan PIN', (10, 10, 200, 60))], 'pin_prompt', True),
    ([element('Enter your PIN to pay', (10, 10, 400, 60))], 'pin_prompt', True),
    ([element('x', (0, 0, 5, 5), package='com.gojek.gopay')], 'protected_package', True),
    ([element('secret', (10, 10, 200, 60), password='true', cls='android.widget.EditText')], 'password_field', True),
    ([element('Bayar Rp45.000', (100, 1500, 400, 1600), True, 'android.widget.Button')], 'payment_screen', False),
    ([element('Nomor kartu', (100, 1500, 400, 1600))], 'payment_screen', False),
    ([element('CVV', (100, 1500, 400, 1600))], 'payment_screen', False),
], ids=['pin', 'pin-sentence', 'protected', 'password', 'pay-button', 'card-number', 'cvv'])
def test_a_payment_pin_or_card_screen_stops_before_any_input(env, extra, rule, sensitive):
    world, receipt = drive(env, lambda world: call(world, 'preview'), extra=extra)
    assert receipt['state'] == 'stopped' and receipt['stop_reason'] == 'payment_or_pin_screen'
    assert receipt['guard']['rule'] == rule and receipt['guard']['matched']
    assert receipt['side_effects'] == 'none' and receipt['automatic_retry'] is False
    assert world.phone.ui_calls == [] and world.phone.adb_taps == []
    observation = receipt['observation']
    if sensitive:
        assert observation['withheld'] == 'protected_screen' and 'elements' not in observation and 'image_file' not in observation
        assert 'screenshot_after_sha256' not in receipt and 'screenshot_before_sha256' not in receipt
    else:
        assert observation['elements'] and observation['image_file']['file_id']
        assert receipt['screenshot_after_sha256']


# spec: ANYM-975
def test_a_payment_sheet_that_appears_after_the_preview_stops_before_the_send_tap(env):
    async def scenario(world):
        preview = await call(world, 'preview')
        world.phone.switches['pay_after_typed'] = True
        return await call(world, 'send', preview_digest=preview['preview_digest'])
    world, sent = drive(env, scenario, state='chat')
    assert sent['state'] == 'stopped' and sent['guard']['rule'] == 'payment_screen' and sent['sent'] is False
    assert world.phone.adb_taps == [] and world.phone.typed == MESSAGE


# spec: ANYM-975
def test_a_payment_sheet_that_appears_while_typing_stops_the_preview(env):
    world, receipt = drive(env, lambda world: call(world, 'preview'), state='chat', pay_after_typed=True)
    assert receipt['state'] == 'stopped' and receipt['guard']['rule'] == 'payment_screen'
    assert {'inputText': MESSAGE} in world.phone.ui_calls and world.phone.adb_taps == []
    assert 'preview_digest' not in receipt


# spec: ANYM-975
def test_a_stop_of_the_ui_engines_guard_is_a_definite_stop_too(env):
    world, receipt = drive(env, lambda world: call(world, 'preview'), engine_stop=True)
    assert receipt['state'] == 'stopped' and receipt['stop_reason'] == 'guard'
    assert receipt['guard']['rule'] == 'write_denied' and world.phone.adb_taps == []


# spec: ANYM-975
@pytest.mark.parametrize('switches', [{'tap_times_out': True}, {'lose_hierarchy_after_tap': True}, {'never_seen': True}],
                         ids=['tap-times-out', 'hierarchy-lost', 'message-never-seen'])
def test_an_uncertain_send_is_unknown_never_retried_and_replays_without_a_second_tap(env, switches):
    request = str(uuid.uuid4())

    async def scenario(world):
        preview = await call(world, 'preview')
        digest = preview['preview_digest']
        first = await call(world, 'send', request_id=request, preview_digest=digest)
        taps = list(world.phone.adb_taps)
        again = await call(world, 'send', request_id=request, preview_digest=digest)
        return first, again, taps
    world, (first, again, taps) = drive(env, scenario, state='chat', **switches)
    assert first['state'] == 'unknown' and first['side_effects'] == 'may_have_occurred'
    assert first['automatic_retry'] is False
    assert first['next_action'] == 'read_chat_with_android_observe_before_any_new_request'
    assert again == first and taps == world.phone.adb_taps == [centre(SEND)]


# spec: ANYM-975
def test_a_failure_before_the_tap_is_failed_with_no_side_effect(env):
    world, receipt = drive(env, lambda world: call(world, 'preview'), input_fails=True)
    assert receipt['state'] == 'failed' and receipt['side_effects'] == 'none'
    assert receipt['error'] == 'uiautomator_transport_error' and world.phone.adb_taps == []


# spec: ANYM-975
def test_the_receipt_keeps_the_confirming_words_the_message_and_screenshot_hashes_only(env):
    world, (preview, sent) = drive(env, previewed_and_sent)
    assert (sent['action'], sent['app'], sent['order'], sent['message'], sent['owner_instruction']) == (
        'send', 'gojek', ORDER, MESSAGE, CONFIRMATION)
    before, after = PNG + b'typed' + MESSAGE.encode(), PNG + b'sentafter'
    assert sent['screenshot_before_sha256'] == hashlib.sha256(before).hexdigest()
    assert sent['screenshot_after_sha256'] == hashlib.sha256(after).hexdigest()
    assert 'image' not in sent and 'image_file' not in sent and 'preview_digest' not in json.dumps(sent)
    assert preview['screenshot_before_sha256'] == hashlib.sha256(PNG + b'order-screen').hexdigest()


# spec: ANYM-975
def test_the_same_request_id_with_another_message_is_a_conflict(env):
    request = str(uuid.uuid4())

    async def scenario(world):
        await call(world, 'preview', request_id=request)
        with pytest.raises(Refused, match='request_id_conflict'):
            await call(world, 'preview', request_id=request, message='Other text')
    drive(env, scenario, state='chat')
