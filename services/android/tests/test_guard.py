"""The run guard (ANYM-968): synthetic hierarchies, a fake uiautomator2 device, no phone."""

import asyncio
import json
from types import SimpleNamespace
import uuid
from xml.sax.saxutils import quoteattr

import pytest

from deedkit_android import guard as guard_module
from deedkit_android.config import Config, Refused
from deedkit_android.guard import PIN_PHRASES, TAP_PHRASES, Guard, Rules, Stop
from deedkit_android.ui_worker import execute

ALLOWED = ('Lihat keranjang', 'Checkout', 'Pesanan', 'Kirim ke', 'Send feedback', 'Pay later info')


def xml(*nodes, package='example.shop'):
    """A screen: a root frame and the given clickable buttons, one row each."""
    rows = ''.join(
        f'<node class="android.widget.Button" package="{package}" clickable="true" bounds="[0,{100 * i}][500,{100 * i + 80}]" '
        + ' '.join(f'{k}={quoteattr(v)}' for k, v in attrs.items()) + '>' + inner + '</node>'
        for i, (attrs, inner) in enumerate(nodes))
    return (f'<hierarchy><node class="android.widget.FrameLayout" package="{package}" clickable="false" '
            f'bounds="[0,0][1000,2000]">{rows}</node></hierarchy>')


def button(text='', inner='', **attrs):
    return ({'text': text, **attrs}, inner)


class Target:
    def __init__(self, device, selector):
        self.device, self.selector = device, selector

    @property
    def info(self):
        return self.device.matched

    def center(self):
        bounds = self.device.matched['visibleBounds']
        return (bounds['left'] + bounds['right']) // 2, (bounds['top'] + bounds['bottom']) // 2

    def set_text(self, value, timeout=0):
        self.device.inputs.append(value)

    def get_text(self, timeout=0):
        return 'text'


class Phone:
    """Records every input; a guard stop must leave these lists empty."""

    def __init__(self, hierarchy, package='example.shop', activity='.Main', matched=None, links=''):
        self.hierarchy, self.package, self.activity = hierarchy, package, activity
        self.matched = matched or {'visibleBounds': {'left': 0, 'top': 0, 'right': 500, 'bottom': 80}}
        self.clicks, self.inputs, self.swipes, self.keys, self.shells = [], [], [], [], []
        self._dev = SimpleNamespace(shell2=self.shell2)
        self.links = links

    def jsonrpc_call(self, method, params=None, timeout=10):
        if method == 'dumpWindowHierarchy':
            if isinstance(self.hierarchy, Exception):
                raise self.hierarchy
            return self.hierarchy
        return True  # waitForExists: the selector matched

    def app_current(self):
        if self.package is None:
            raise Refused('app_foreground_unavailable')
        return {'package': self.package, 'activity': self.activity}

    def window_size(self):
        return 1000, 2000

    def __call__(self, **selector):
        return Target(self, selector)

    def click(self, x, y):
        self.clicks.append((x, y))

    double_click = long_click = click

    def swipe(self, *args, **options):
        self.swipes.append(args)

    def press(self, key):
        self.keys.append(key)

    def shell2(self, args, timeout=None, encoding='utf-8'):
        self.shells.append(args)
        if args[:3] == ['cmd', 'package', 'query-activities']:
            return SimpleNamespace(returncode=0, output=self.links)
        if args[:3] == ['cmd', 'package', 'resolve-activity']:
            return SimpleNamespace(returncode=0, output='priority=0\nexample.shop/.Main\n')
        return SimpleNamespace(returncode=0, output='Starting: Intent\nStatus: ok\n')

    def acted(self):
        return self.clicks or self.inputs or self.swipes or self.keys


def stopped(phone, command, guard=None):
    with pytest.raises(Stop) as caught:
        execute(phone, command, guard=guard or Guard())
    assert not phone.acted()  # A stop sends no input to the device.
    return caught.value.guard


# spec: ANYM-968
@pytest.mark.parametrize('phrase', TAP_PHRASES)
def test_every_built_in_phrase_stops_a_tap_with_and_without_an_amount(phrase):
    label = phrase.upper() if phrase.isascii() else phrase.capitalize()
    for text in (label, f'{label} Rp45.000', f'{label} • Rp 45.000', f'{label} $12.50', f'{label} 1 200 ₽'):
        phone = Phone(xml(button(text)))
        found = stopped(phone, {'tapOn': {'point': '250,40'}})
        assert found['rule'] == 'stop_phrase' and found['matched'] == phrase, text


# spec: ANYM-968
def test_indonesian_amount_glued_and_bulleted_labels_stop():
    for text in ('Bayar Rp45.000', 'Pesan & antar sekarang • Rp 45.000', 'Bayar Sekarang - IDR 120,000'):
        assert stopped(Phone(xml(button(text))), {'tapOn': {'point': '250,40'}})['rule'] == 'stop_phrase'


# spec: ANYM-968
@pytest.mark.parametrize('label', ALLOWED)
def test_allowed_labels_tap_normally(label):
    phone = Phone(xml(button(label)))
    assert execute(phone, {'tapOn': {'point': '250,40'}}, guard=Guard()) == {'success': True}
    assert phone.clicks == [(250, 40)]


# spec: ANYM-968
def test_selector_by_resource_id_and_by_child_text_check_the_node_not_the_selector():
    # A selector by id onto the pay button: its child text decides.
    phone = Phone(xml(button('', '<node class="android.widget.TextView" text="Bayar" bounds="[10,10][490,70]"/>',
                             **{'resource-id': 'example.shop:id/primary'})),
                  matched={'visibleBounds': {'left': 0, 'top': 0, 'right': 500, 'bottom': 80},
                           'className': 'android.widget.Button', 'resourceName': 'example.shop:id/primary'})
    assert stopped(phone, {'tapOn': {'id': 'example.shop:id/primary'}})['matched'] == 'bayar'
    # A selector by a harmless child text whose clickable ancestor reads Bayar.
    phone = Phone(xml(button('', '<node class="android.widget.TextView" text="Total" bounds="[10,10][200,70]"/>'
                                 '<node class="android.widget.TextView" text="Bayar" bounds="[300,10][490,70]"/>')),
                  matched={'visibleBounds': {'left': 10, 'top': 10, 'right': 200, 'bottom': 70},
                           'className': 'android.widget.TextView', 'text': 'Total'})
    assert stopped(phone, {'tapOn': 'Total'})['matched'] == 'bayar'
    # A resource-id entry name and a configured exact id.
    assert stopped(Phone(xml(button('', **{'resource-id': 'example.shop:id/place_order'}))),
                   {'tapOn': {'point': '250,40'}})['matched'] == 'place order'
    rules = Rules({'stop_ids': {'example.shop': ['example.shop:id/go']}})
    found = stopped(Phone(xml(button('Go', **{'resource-id': 'example.shop:id/go'}))),
                    {'tapOn': {'point': '250,40'}}, Guard(rules))
    assert found == {**found, 'rule': 'stop_id', 'matched': 'example.shop:id/go'}


# spec: ANYM-968
def test_a_nested_repeat_tap_stops_with_its_path_and_no_click():
    from deedkit_android.flows import compile_flow
    from deedkit_android.uiautomator import UiRunner

    phone = Phone(xml(button('Konfirmasi pesanan')))
    flow = compile_flow('- repeat:\n    times: 2\n    commands:\n'
                        '      - runFlow:\n          commands:\n            - tapOn: {point: "250,40"}\n', {})

    async def call(command, **options):
        if '__guard__' == command:
            return {'success': True}
        return execute(phone, command, guard=Guard())

    runner = UiRunner(call, 5000, 1000)
    with pytest.raises(Refused) as caught:
        asyncio.run(runner.run(flow))
    assert caught.value.code == 'guard_stopped' and caught.value.command_path == [0, 0, 0]
    assert runner.steps[-1]['error'] == 'guard_stopped' and phone.clicks == []


# spec: ANYM-968
def test_a_swipe_within_ten_pixels_is_a_tap_and_a_long_swipe_is_not():
    phone = Phone(xml(button('Pay now')))
    assert stopped(phone, {'swipe': {'start': '250,40', 'end': '256,45'}})['rule'] == 'stop_phrase'
    assert execute(phone, {'swipe': {'start': '250,40', 'end': '250,900'}}, guard=Guard()) == {'success': True}
    assert len(phone.swipes) == 1


# spec: ANYM-968
def test_map_tap_element_and_point_candidates_are_checked():
    from deedkit_android.app_maps import validate_map
    app_map = validate_map('package: example.shop\nselectors:\n  pay: [{textExact: Bayar}]\n'
                           '  spot: [{point: "250,140"}]\nscreens:\n  home:\n    require: [{textExact: Bayar}]\n')
    phone = Phone(xml(button('Bayar'), button('Beli sekarang')))
    assert stopped(phone, {'tapElement': {'selector': 'pay', 'app_map': app_map}})['matched'] == 'bayar'
    assert stopped(phone, {'tapElement': {'selector': 'spot', 'app_map': app_map}})['matched'] == 'beli sekarang'


# spec: ANYM-968
def test_protected_package_stops_before_any_action_without_text():
    phone = Phone(xml(button('Saldo'), package='id.co.bri.brimo'), package='id.co.bri.brimo')
    found = stopped(phone, {'assertVisible': 'Saldo'})
    assert found['rule'] == 'protected_package' and found['matched'] == 'id.co.bri.brimo' and found['sensitive']
    extra = Guard(Rules({'protected_packages': ['example.wallet']}))
    assert stopped(Phone(xml(button('x')), package='example.wallet'), 'back', extra)['rule'] == 'protected_package'


# spec: ANYM-968
def test_configured_stop_activity_stops():
    rules = Rules({'stop_activities': ['example.shop/.CheckoutActivity']})
    phone = Phone(xml(button('Ringkasan')), activity='.CheckoutActivity')
    found = stopped(phone, {'tapOn': {'point': '250,40'}}, Guard(rules))
    assert found['rule'] == 'stop_activity' and found['matched'] == 'example.shop.CheckoutActivity'


# spec: ANYM-968
@pytest.mark.parametrize('phrase', PIN_PHRASES)
def test_pin_prompt_in_the_hierarchy_stops(phrase):
    phone = Phone(xml(button(phrase.title()), button('1')))
    found = stopped(phone, {'tapOn': {'point': '250,140'}})
    assert found['rule'] == 'pin_prompt' and found['sensitive']


# spec: ANYM-968
def test_pin_prompt_read_by_ocr_when_the_hierarchy_has_no_text(monkeypatch):
    from deedkit_android import screen_text
    monkeypatch.setattr(screen_text, 'read_screen_text',
                        lambda device, options, ocr, locate: {'lines': [{'text': 'Masukkan PIN'}]})
    phone = Phone(xml(button('')))
    assert stopped(phone, 'back', Guard(ocr={'command': '/ocr', 'languages': 'eng'}))['rule'] == 'pin_prompt'
    # Configured additions are matched the same way.
    phone = Phone(xml(button('Kode Rahasia')))
    assert stopped(phone, 'back', Guard(Rules({'pin_phrases': ['kode rahasia']})))['rule'] == 'pin_prompt'


# spec: ANYM-968
def test_password_field_stops_unless_the_app_has_a_bound_secret():
    field = button('', password='true', focused='true')
    assert stopped(Phone(xml(field)), {'inputText': 'x'})['rule'] == 'password_field'
    bound = Guard(secret_packages={'example.shop'})
    bound.before(Phone(xml(field)), {'inputText': 'x'})  # S2's binding: no stop


# spec: ANYM-968
@pytest.mark.parametrize('command', [
    {'tapOn': {'point': '250,40'}}, {'inputText': 'hi'}, {'pressKey': 'Enter'}, 'back',
    {'swipe': {'direction': 'UP'}}, {'setText': {'text': 'Chat', 'value': 'hi'}}])
def test_write_denied_app_takes_no_input(command):
    phone = Phone(xml(button('Chat'), package='com.whatsapp'), package='com.whatsapp')
    found = stopped(phone, command)
    assert found['rule'] == 'write_denied' and found['matched'] == 'com.whatsapp'


# spec: ANYM-968
def test_write_denied_app_reads_and_launch_or_link_into_it_stops():
    phone = Phone(xml(button('Chat'), package='org.telegram.messenger'), package='org.telegram.messenger')
    assert execute(phone, {'copyTextFrom': 'Chat'}, guard=Guard()) == {'text': 'text'}
    assert execute(phone, {'assertVisible': 'Chat'}, guard=Guard()) == {'matched': True}
    shop = Phone(xml(button('Home')))
    assert stopped(shop, {'launchApp': {'appId': 'com.whatsapp'}})['matched'] == 'com.whatsapp'
    assert stopped(shop, {'clearState': 'org.telegram.messenger'})['rule'] == 'write_denied'
    linked = Phone(xml(button('Home')), links='com.android.chrome/.Main\norg.telegram.messenger/.DefaultIcon\n')
    assert stopped(linked, {'openLink': 'https://t.me/example'})['matched'] == 'org.telegram.messenger'
    from deedkit_android.app_maps import validate_map
    app_map = validate_map('package: example.shop\nselectors:\n  home: [{textExact: Home}]\nscreens:\n'
                           '  home:\n    require: [home]\ndeep_links:\n  chat: {url: "https://wa.me/1", screen: home}\n')
    linked.links = 'com.whatsapp/.Main\n'
    assert stopped(linked, {'openDeepLink': {'name': 'chat', 'app_map': app_map}})['matched'] == 'com.whatsapp'
    plain = Phone(xml(button('Home')), links='com.android.chrome/.Main\n')
    assert execute(plain, {'openLink': 'https://example.com'}, guard=Guard()) == {'success': True}


# spec: ANYM-968
def test_unresolved_targets_fail_closed():
    assert stopped(Phone(TimeoutError('hierarchy')), {'tapOn': {'point': '1,1'}}) == {
        'rule': 'unresolved_target', 'matched': 'hierarchy_unavailable', 'when': 'before', 'package': None,
        'sensitive': False}
    # A point with no node (outside every bound) and no OCR configured.
    empty = '<hierarchy><node package="example.shop" bounds="[0,0][10,10]"/></hierarchy>'
    assert stopped(Phone(empty), {'tapOn': {'point': '500,900'}})['matched'] == 'no_readable_text'


# spec: ANYM-968
def test_point_without_text_reads_ocr_in_a_box_around_it(monkeypatch):
    from deedkit_android import screen_text
    regions = []

    def read(device, options, ocr, locate):
        regions.append(options.get('region'))
        return {'lines': [{'text': 'Bayar'}] if len(regions) == 1 else []}
    monkeypatch.setattr(screen_text, 'read_screen_text', read)
    ocr = Guard(ocr={'command': '/ocr', 'languages': 'eng'})
    canvas = '<hierarchy><node package="example.shop" text="Toko" bounds="[0,0][1000,100]"/>' \
             '<node package="example.shop" bounds="[0,100][1000,2000]"/></hierarchy>'
    assert stopped(Phone(canvas), {'tapOn': {'point': '500,900'}}, ocr)['matched'] == 'bayar'
    assert regions == [{'x': 300, 'y': 780, 'w': 400, 'h': 240}]
    assert stopped(Phone(canvas), {'tapOn': {'point': '500,900'}}, ocr)['matched'] == 'no_readable_text'

    def broken(*args):
        raise Refused('ocr_failed')
    monkeypatch.setattr(screen_text, 'read_screen_text', broken)
    assert stopped(Phone(canvas), {'tapOn': {'point': '500,900'}}, ocr)['matched'] == 'ocr_failed'


def ui_devices(tmp_path, store):
    from deedkit_android.device import Devices
    return Devices(SimpleNamespace(devices={'phone': 'SERIAL'}, adb='/synthetic', uiautomator2_jar='/synthetic',
                                   state_dir=str(tmp_path / 'state')), store)


def run_stop(tmp_path, monkeypatch, stop, image=None, after=False):
    from deedkit_android.flows import compile_flow
    from deedkit_android.store import Store

    async def scenario():
        store = Store(tmp_path / 'state')
        devices = ui_devices(tmp_path, store)
        seen = []

        async def call(device, command, **options):
            seen.append(command)
            if (command == '__guard__') == after and (after or len(seen) == 2):
                error = Refused('guard_stopped')
                error.guard = stop
                raise error
            return {'success': True}

        async def capture(*args):
            return image or {'image_file': store.save('owner', b'PNG')}

        async def hierarchy(*args):
            return {'elements': [{'text': 'Bayar Rp45.000'}]}
        monkeypatch.setattr(devices.ui, 'call', call)
        monkeypatch.setattr(devices, 'capture_image', capture)
        monkeypatch.setattr(devices, 'capture_hierarchy', hierarchy)
        session = store.reserve('owner', 'phone', 'acquire')['session_id']
        flow = compile_flow('- back\n- tapOn: Bayar\n- back', {})
        try:
            receipt = await devices.execute('owner', 'phone', session, str(uuid.uuid4()), None, 10, 10, flow)
            assert not devices.locks['phone'].locked()  # The device lock is released.
            return receipt, seen
        finally:
            await devices.close()
            store.close()
    return asyncio.run(scenario())


# spec: ANYM-968
def test_stop_receipt_is_definite_with_the_stop_screen(tmp_path, monkeypatch):
    stop = {'rule': 'stop_phrase', 'matched': 'bayar', 'when': 'before', 'package': 'example.shop',
            'sensitive': False}
    receipt, seen = run_stop(tmp_path, monkeypatch, stop)
    assert receipt['state'] == 'stopped' and receipt['stop_reason'] == 'guard'
    assert receipt['guard'] == {'rule': 'stop_phrase', 'matched': 'bayar', 'when': 'before', 'step_path': [1]}
    assert [step['command'] for step in receipt['steps']] == ['back', 'tapOn']
    assert receipt['steps'][-1]['error'] == 'guard_stopped' and receipt['commands_executed'] == 1
    assert receipt['observation']['image_file']['file_id'] and receipt['observation']['elements']
    assert 'side_effects' not in receipt and len(seen) == 2  # Nothing after the stop; no back press.


# spec: ANYM-968
def test_stop_on_a_protected_or_secure_screen_keeps_only_the_package(tmp_path, monkeypatch):
    stop = {'rule': 'protected_package', 'matched': 'id.dana', 'when': 'before', 'package': 'id.dana',
            'sensitive': True}
    receipt, _ = run_stop(tmp_path, monkeypatch, stop)
    assert receipt['observation'] == {**receipt['observation'], 'package': 'id.dana', 'withheld': 'protected_screen'}
    assert 'elements' not in receipt['observation'] and 'image_file' not in receipt['observation']
    secure = {'rule': 'stop_phrase', 'matched': 'pay', 'when': 'after', 'package': 'example.shop',
              'sensitive': False}
    receipt, seen = run_stop(tmp_path / 'secure', monkeypatch, secure, image={'screenshot_error': 'unavailable'},
                             after=True)
    assert receipt['state'] == 'stopped' and receipt['guard']['when'] == 'after' and seen[-1] == '__guard__'
    assert receipt['observation']['withheld'] == 'secure_screen' and 'elements' not in receipt['observation']


# spec: ANYM-968
def test_after_the_last_step_the_screen_rule_runs_in_the_worker():
    phone = Phone(xml(button('Masukkan PIN')))
    with pytest.raises(Stop) as caught:
        Guard().screen(phone, when='after')
    assert caught.value.guard['when'] == 'after' and caught.value.guard['rule'] == 'pin_prompt'


# spec: ANYM-968
def test_capabilities_list_built_in_and_configured_rules(tmp_path):
    from fastapi.testclient import TestClient
    from deedkit_android.app import create_app
    import hashlib
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({
        'adb': '/synthetic/adb', 'state_dir': str(tmp_path / 'state'), 'devices': {'home': 'SERIAL'},
        'principals': [{'id': 'p', 'key_sha256': hashlib.sha256(b'one').hexdigest(), 'devices': ['home'],
                        'permissions': ['android:read']}],
        'guard': {'stop_phrases': ['Selesaikan pembayaran'], 'protected_packages': ['example.wallet'],
                  'stop_ids': {'example.shop': ['example.shop:id/go']}, 'stop_activities': ['example.shop/.Pay']}}))
    config.chmod(0o600)
    with TestClient(create_app(config)) as client:
        rules = client.post('/v1/capabilities', json={}, headers={'Authorization': 'Bearer one'}).json()['guard']
    assert rules['enforced'] is True and rules['can_disable'] is False
    assert {'bayar', 'оплатить', 'pay now', 'selesaikan pembayaran'} <= set(rules['tap_phrases'])
    assert {'com.whatsapp', 'com.google.android.dialer'} <= set(rules['write_denied_packages'])
    assert {'example.wallet', 'ru.sberbankmobile'} <= set(rules['protected_packages'])
    assert rules['stop_ids'] == {'example.shop': ['example.shop:id/go']}
    assert rules['stop_activities'] == ['example.shop.Pay'] and 'masukkan pin' in rules['pin_phrases']


# spec: ANYM-968
def test_no_request_scenario_map_or_host_field_disables_the_guard():
    from deedkit_android.app import RunFlow
    from deedkit_android.app_maps import compile_scenario, validate_map
    from pydantic import ValidationError

    library = SimpleNamespace(read=lambda owner, kind, name, version, stage: {
        'version': 'v1', 'yaml': 'package: example.shop\nselectors:\n  pay: [{textExact: Bayar}]\n'
                                 'screens:\n  home:\n    require: [pay]\n'})
    for source in ('guard: false\nsteps:\n  - back\n',
                   'map: shop\nguard: {enabled: false}\nsteps:\n  - back\n',
                   'map: shop\nsteps:\n  - tapElement: {selector: pay, guard: false}\n',
                   'map: shop\nsteps:\n  - tapElement: {selector: pay, unsafe: true}\n',
                   '- tapOn: {text: Bayar, guard: false}\n',
                   '- tapOn: {text: Bayar, allow_payment: true}\n',
                   '- runFlow: {guard: false, commands: [back]}\n'):
        with pytest.raises(Refused) as caught:
            compile_scenario(library, 'owner', source, {})
        assert caught.value.status == 422, source
    for source in ('package: example.shop\nguard: false\n', 'package: example.shop\nmeta: {}\nunsafe: true\n'):
        with pytest.raises(Refused):
            validate_map(source)
    base = {'device_id': 'home', 'session_id': 's', 'request_id': 'r', 'yaml': '- back'}
    for field in ({'guard': False}, {'unsafe': True}, {'side_effects': 'none'}):
        with pytest.raises(ValidationError):
            RunFlow.model_validate({**base, **field})
    host = {'adb': '/a', 'state_dir': '/s', 'devices': {}, 'principals': []}
    for shape in ({'enabled': False}, {'disable': ['stop_phrase']}, {'remove_phrases': ['pay']},
                  {'protected_packages': None}):
        with pytest.raises(ValidationError):
            Config.model_validate({**host, 'guard': shape})
    with pytest.raises(ValidationError):
        Config.model_validate({**host, 'guard': False})
    # An empty addition list leaves every built-in rule in place.
    rules = Rules(Config.model_validate({**host, 'guard': {'stop_phrases': []}}).guard.model_dump())
    assert set(map(guard_module.normalize, TAP_PHRASES)) <= rules.tap_phrases
