"""Host-held app secrets (ANYM-969): `${secret:NAME}` resolves on the phone host only, into the app it is bound to,
and its value reaches no receipt, row, file, log or read. Synthetic values and a fake uiautomator2 device; no phone."""

import asyncio
import hashlib
import io
import json
import os
import sqlite3
import stat
import threading
from types import SimpleNamespace
import uuid
from xml.sax.saxutils import quoteattr

from fastapi.testclient import TestClient
import pytest

from deedkit_android import secrets as host_secrets
from deedkit_android.app import SCHEMAS, RunFlow, create_app
from deedkit_android.app_maps import compile_scenario, validate_map
from deedkit_android.config import Refused
from deedkit_android.flows import compile_flow
from deedkit_android.guard import Guard, Stop
from deedkit_android.secrets import Masker, Run, SecretStore
from deedkit_android.store import Store
from deedkit_android.ui_worker import execute, make_guard
from deedkit_android.uiautomator import DEFINITE, reference

VALUE = 'canary-' + uuid.uuid4().hex  # unique per run, synthetic
NAME = 'SHOP_PASSWORD'
PACKAGE = 'example.shop'
REF = '${secret:SHOP_PASSWORD}'
MAP = ('package: example.shop\nselectors:\n  password: [{resourceId: "example.shop:id/pw"}]\n'
       'screens:\n  login:\n    require: [password]\n')
LIBRARY = SimpleNamespace(read=lambda owner, kind, name, version, stage: {'version': 'v1', 'yaml': MAP})
BOUND = {NAME: {'package': PACKAGE, 'value': VALUE}}


def host(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({
        'adb': '/synthetic/adb', 'state_dir': str(tmp_path / 'state'), 'devices': {'home': 'SERIAL'},
        'principals': [{'id': 'one', 'key_sha256': hashlib.sha256(b'one').hexdigest(), 'devices': ['home'],
                        'permissions': ['android:read', 'android:control']}]}))
    config.chmod(0o600)
    return config


def cli(config, *argv, stdin=''):
    return host_secrets.main([*argv, '--config', str(config)], stdin=io.StringIO(stdin))


def leaks(root):
    """Files under root holding the canary, except the secret file itself."""
    return [str(path) for path in root.rglob('*')
            if path.is_file() and path.name != 'secrets.json' and VALUE.encode() in path.read_bytes()]


# --- criterion 1: the store -------------------------------------------------------------------------------------


# spec: ANYM-969
def test_secret_set_reads_standard_input_and_writes_a_private_file(tmp_path, capsys):
    config = host(tmp_path)
    assert cli(config, 'set', NAME, '--package', PACKAGE, stdin=VALUE + '\n') == 0
    path = tmp_path / 'state' / 'secrets.json'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text())['secrets'] == {NAME: {'package': PACKAGE, 'value': VALUE}}
    shown = capsys.readouterr()
    assert VALUE not in shown.out + shown.err


# spec: ANYM-969
def test_secret_set_on_a_terminal_reads_without_echo(tmp_path, monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    asked = []
    monkeypatch.setattr(host_secrets.getpass, 'getpass', lambda prompt='': asked.append(prompt) or VALUE)
    config = host(tmp_path)
    assert host_secrets.main(['set', NAME, '--package', PACKAGE, '--config', str(config)],
                             stdin=Terminal('never read\n')) == 0
    assert len(asked) == 1 and VALUE not in asked[0]
    assert SecretStore(tmp_path / 'state').read() == BOUND


# spec: ANYM-969
@pytest.mark.parametrize('name', [
    'shop_password', '1SHOP', 'SHOP-PASSWORD', 'A' * 65, 'BANK_PIN', 'PINCODE', 'CVV', 'MY_CVC_VALUE',
    'CARD_NUMBER', 'LOGIN_OTP', 'PASSCODE_X'])
def test_secret_names_are_restricted_and_payment_secrets_are_refused(tmp_path, capsys, name):
    config = host(tmp_path)
    assert cli(config, 'set', name, '--package', PACKAGE, stdin=VALUE + '\n') != 0
    assert not (tmp_path / 'state' / 'secrets.json').exists()
    shown = capsys.readouterr()
    assert VALUE not in shown.out + shown.err and 'secret_name' in shown.err


# spec: ANYM-969
def test_secret_values_and_packages_are_bounded(tmp_path, capsys):
    config = host(tmp_path)
    assert cli(config, 'set', 'LONG_ENOUGH', '--package', PACKAGE, stdin='x' * 256 + '\r\n') == 0
    assert SecretStore(tmp_path / 'state').read()['LONG_ENOUGH']['value'] == 'x' * 256
    for value in ('x' * 257, '', '\n', 'two\nlines', 'nul\x00byte'):
        assert cli(config, 'set', 'REFUSED', '--package', PACKAGE, stdin=value) != 0
    assert cli(config, 'set', 'REFUSED', '--package', 'not a package', stdin=VALUE) != 0
    assert list(SecretStore(tmp_path / 'state').read()) == ['LONG_ENOUGH']
    assert VALUE not in capsys.readouterr().err


# spec: ANYM-969
def test_list_prints_names_and_packages_only_and_delete_removes_one(tmp_path, capsys):
    config = host(tmp_path)
    cli(config, 'set', NAME, '--package', PACKAGE, stdin=VALUE)
    cli(config, 'set', 'OTHER_LOGIN', '--package', 'example.other', stdin='another-' + VALUE)
    capsys.readouterr()
    assert cli(config, 'list') == 0
    listing = capsys.readouterr().out
    assert listing.splitlines() == ['OTHER_LOGIN\texample.other', f'{NAME}\t{PACKAGE}'] and VALUE not in listing
    assert cli(config, 'delete', NAME) == 0 and cli(config, 'delete', NAME) != 0
    assert list(SecretStore(tmp_path / 'state').read()) == ['OTHER_LOGIN']


# spec: ANYM-969
def test_secret_command_is_reached_from_the_installed_entry_point(tmp_path, monkeypatch, capsys):
    from deedkit_android import cli as entry
    config = host(tmp_path)
    monkeypatch.setattr('sys.stdin', io.StringIO(VALUE))
    with pytest.raises(SystemExit) as done:
        entry.main(['secret', 'set', NAME, '--package', PACKAGE, '--config', str(config)])
    assert done.value.code == 0 and SecretStore(tmp_path / 'state').read() == BOUND
    capsys.readouterr()
    with pytest.raises(SystemExit) as done:
        entry.main(['--help'])  # The server's own arguments are unchanged.
    assert done.value.code == 0 and '--config' in capsys.readouterr().out


# spec: ANYM-969
def test_concurrent_sets_keep_every_secret_and_a_failed_replace_keeps_the_old_file(tmp_path, monkeypatch):
    store = SecretStore(tmp_path / 'state')
    names = [f'SECRET_{index}' for index in range(12)]
    threads = [threading.Thread(target=store.set, args=(name, PACKAGE, VALUE + name)) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert set(store.read()) == set(names)
    path = tmp_path / 'state' / 'secrets.json'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not [entry for entry in path.parent.iterdir() if entry.name.endswith('.tmp')]
    before = path.read_bytes()

    def broken(*args):
        raise OSError('disk full')
    with monkeypatch.context() as patch:
        patch.setattr(os, 'replace', broken)
        with pytest.raises(OSError):
            store.set('NEW_ONE', PACKAGE, 'v')
    assert path.read_bytes() == before and not [e for e in path.parent.iterdir() if e.name.endswith('.tmp')]


# spec: ANYM-969
def test_a_secret_file_open_to_others_is_refused_and_never_read(tmp_path):
    store = SecretStore(tmp_path / 'state')
    store.set(NAME, PACKAGE, VALUE)
    (tmp_path / 'state' / 'secrets.json').chmod(0o644)
    with pytest.raises(Refused) as caught:
        store.read()
    assert caught.value.code == 'secrets_unavailable' and VALUE not in repr(vars(caught.value))


# spec: ANYM-969
def test_no_http_route_or_tool_sets_reads_or_lists_a_value(tmp_path):
    config = host(tmp_path)
    cli(config, 'set', NAME, '--package', PACKAGE, stdin=VALUE)
    with TestClient(create_app(config)) as client:
        for operation in ('secret', 'secrets', 'secret_set', 'secret_get', 'secret_list', 'secret_delete'):
            response = client.post('/v1/' + operation, json={'name': NAME}, headers={'Authorization': 'Bearer one'})
            assert response.status_code == 404 and VALUE not in response.text
    assert not [name for name in SCHEMAS if 'secret' in name]


# --- criterion 2: use -------------------------------------------------------------------------------------------


def compiled(source, known=None):
    known = {NAME: PACKAGE} if known is None else known
    return compile_scenario(LIBRARY, 'owner', source, {}, secrets=lambda: known)[0]


ALLOWED = {
    'inputText plain': '- inputText: ${secret:SHOP_PASSWORD}',
    'inputText quoted': '- inputText: "${secret:SHOP_PASSWORD}"',
    'inputText text': '- inputText: {text: "${secret:SHOP_PASSWORD}"}',
    'setText value': '- setText: {resourceId: "example.shop:id/pw", value: "${secret:SHOP_PASSWORD}"}',
    'typeInto text': 'map: shop\nsteps:\n  - typeInto: {selector: password, text: "${secret:SHOP_PASSWORD}"}',
    'nested in repeat': '- repeat: {times: 1, commands: [{inputText: "${secret:SHOP_PASSWORD}"}]}',
}
DENIED = {
    'tap text': '- tapOn: "${secret:SHOP_PASSWORD}"',
    'tap selector': '- tapOn: {text: "${secret:SHOP_PASSWORD}"}',
    'prefix': '- inputText: "user ${secret:SHOP_PASSWORD}"',
    'suffix': '- inputText: "${secret:SHOP_PASSWORD}!"',
    'twice': '- inputText: "${secret:SHOP_PASSWORD}${secret:SHOP_PASSWORD}"',
    'label': '- inputText: {text: hello, label: "${secret:SHOP_PASSWORD}"}',
    'setText selector': '- setText: {text: "${secret:SHOP_PASSWORD}", value: hello}',
    'setText both': '- setText: {resourceId: x, value: "${secret:SHOP_PASSWORD}", description: "${secret:SHOP_PASSWORD}"}',
    'launch': '- launchApp: "${secret:SHOP_PASSWORD}"',
    'link': '- openLink: "${secret:SHOP_PASSWORD}"',
    'key': '- pressKey: "${secret:SHOP_PASSWORD}"',
    'erase': '- eraseText: "${secret:SHOP_PASSWORD}"',
    'wait': '- extendedWaitUntil: {visible: "${secret:SHOP_PASSWORD}"}',
    'copy': '- copyTextFrom: "${secret:SHOP_PASSWORD}"',
    'read key': '- readScreenText: {key: "${secret:SHOP_PASSWORD}"}',
    'when': '- runFlow: {when: {visible: "${secret:SHOP_PASSWORD}"}, commands: [back]}',
    'while': '- repeat: {while: {visible: "${secret:SHOP_PASSWORD}"}, commands: [back]}',
    'nested tap': '- runFlow: {commands: [{tapOn: "${secret:SHOP_PASSWORD}"}]}',
    'key of a mapping': '- {"${secret:SHOP_PASSWORD}": x}',
    'typeInto selector': 'map: shop\nsteps:\n  - typeInto: {selector: "${secret:SHOP_PASSWORD}", text: hello}',
    'typeInto other field': ('map: shop\nsteps:\n  - typeInto: {selector: password, text: "${secret:SHOP_PASSWORD}", '
                             'expect_screen: "${secret:SHOP_PASSWORD}"}'),
    'tapElement': 'map: shop\nsteps:\n  - tapElement: {selector: "${secret:SHOP_PASSWORD}"}',
    'scenario parameter default': 'map: shop\nparams: {A: {default: "${secret:SHOP_PASSWORD}"}}\nsteps: [back]',
    'scenario description': 'map: shop\ndescription: "${secret:SHOP_PASSWORD}"\nsteps: [back]',
}


# spec: ANYM-969
@pytest.mark.parametrize('source', ALLOWED.values(), ids=ALLOWED)
def test_a_reference_is_accepted_as_the_whole_text_of_inputtext_settext_and_typeinto(source):
    commands = compiled(source)
    assert REF in json.dumps(commands) and VALUE not in json.dumps(commands)  # The reference text stays.


# spec: ANYM-969
@pytest.mark.parametrize('source', DENIED.values(), ids=DENIED)
def test_a_reference_anywhere_else_fails_validation_with_secret_reference_not_allowed(source):
    with pytest.raises(Refused) as caught:
        compiled(source)
    assert caught.value.code == 'secret_reference_not_allowed' and caught.value.status == 422


# spec: ANYM-969
def test_a_reference_in_a_map_or_a_parameter_value_is_refused():
    from deedkit_android.app_maps import validate_map as check
    with pytest.raises(Refused, match='secret_reference_not_allowed'):
        check(MAP.replace('{resourceId: "example.shop:id/pw"}', '{textExact: "${secret:SHOP_PASSWORD}"}'))
    with pytest.raises(Refused, match='invalid_flow_parameters'):
        compile_scenario(LIBRARY, 'owner', '- inputText: ${QUERY}', {'QUERY': REF}, secrets=lambda: {NAME: PACKAGE})
    with pytest.raises(Refused, match='secret_reference_not_allowed'):
        compile_flow('- inputText: "${secret:SHOP_PASSWORD}${secret:SHOP_PASSWORD}"', {})


# spec: ANYM-969
@pytest.mark.parametrize('source', ALLOWED.values(), ids=ALLOWED)
def test_an_unknown_secret_name_fails_validation_with_secret_unknown(source):
    with pytest.raises(Refused) as caught:
        compiled(source, known={})
    assert caught.value.code == 'secret_unknown' and caught.value.status == 422
    with pytest.raises(Refused, match='secret_unknown'):
        compiled(source, known={'OTHER_LOGIN': PACKAGE})


# spec: ANYM-969
def test_validation_runs_before_admission_through_every_authoring_and_run_entry_point(tmp_path):
    config = host(tmp_path)
    cli(config, 'set', NAME, '--package', PACKAGE, stdin=VALUE)
    with TestClient(create_app(config)) as client:
        def post(name, body, code):
            response = client.post('/v1/' + name, json=body, headers={'Authorization': 'Bearer one'})
            assert response.status_code == code, response.text
            return response.json()

        run = {'device_id': 'home', 'session_id': 'not-a-lease'}
        for source, error in (('- tapOn: "${secret:SHOP_PASSWORD}"', 'secret_reference_not_allowed'),
                              ('- inputText: "${secret:NOPE}"', 'secret_unknown')):
            request_id = str(uuid.uuid4())
            assert post('run_flow', {**run, 'request_id': request_id, 'yaml': source}, 422)['error'] == error
            post('get_operation', {'request_id': request_id}, 404)  # Nothing was admitted.
            assert post('maestro', {'action': 'validate', 'yaml': source}, 422)['error'] == error
            assert post('flows', {'action': 'draft', 'name': 'login', 'yaml': source}, 422)['error'] == error
            assert post('flows', {'action': 'put', 'name': 'login', 'yaml': source}, 422)['error'] == error
        good = '- inputText: "${secret:SHOP_PASSWORD}"'
        assert REF in json.dumps(post('maestro', {'action': 'validate', 'yaml': good}, 200)['commands'])
        post('flows', {'action': 'draft', 'name': 'login', 'yaml': good}, 200)


# spec: ANYM-969
def test_the_worker_types_a_secret_only_into_its_package_and_a_focused_editable_field():
    for form in ('inputText', 'setText', 'typeInto'):
        for situation, case in SITUATIONS.items():
            phone = Phone(**case['screen'])
            secrets = {NAME: {'package': case.get('bound', PACKAGE), 'value': VALUE}}
            command = secret_command(form)
            if case['types']:
                done = execute(phone, command, secrets=secrets, guard=Guard(secret_packages={PACKAGE}))
                assert done['success'] is True and phone.inputs == [VALUE], (form, situation)
                continue
            with pytest.raises(Refused) as caught:
                execute(phone, command, secrets=secrets, guard=Guard(secret_packages={PACKAGE}))
            assert caught.value.code == 'secret_package_mismatch', (form, situation)
            assert phone.inputs == [] and phone.acted() is False, (form, situation)  # Nothing was typed.
            assert VALUE not in repr(vars(caught.value)) and caught.value.details['secret'] == NAME


# spec: ANYM-969
def test_literal_text_and_a_missing_secret_at_the_worker():
    phone = Phone(**SITUATIONS['bound']['screen'])
    assert execute(phone, {'inputText': 'hello'}, guard=Guard()) == {'success': True} and phone.inputs == ['hello']
    with pytest.raises(Refused) as caught:
        execute(phone, {'inputText': REF}, secrets={}, guard=Guard(secret_packages={PACKAGE}))
    assert caught.value.code == 'secret_unknown' and phone.inputs == ['hello']
    unfocused = Phone(**SITUATIONS['not focused']['screen'])
    with pytest.raises(Refused, match='focused_field_not_found'):  # A literal keeps its own, older error.
        execute(unfocused, {'inputText': 'hello'}, guard=Guard())


# spec: ANYM-969
@pytest.mark.parametrize('stop', ['pin', 'protected', 'messenger'])
def test_a_secret_is_never_typed_on_a_pin_screen_a_protected_app_or_a_messenger(stop):
    screen = {'pin': {'extra': '<node class="android.widget.TextView" package="example.shop" text="Masukkan PIN" '
                               'bounds="[0,300][500,380]"/>'},
              'protected': {'package': 'id.dana'}, 'messenger': {'package': 'com.whatsapp'}}[stop]
    phone = Phone(**screen)
    package = screen.get('package', PACKAGE)
    with pytest.raises(Stop) as caught:
        execute(phone, {'inputText': REF}, secrets={NAME: {'package': package, 'value': VALUE}},
                guard=Guard(secret_packages={package}))
    assert caught.value.guard['rule'] == {'pin': 'pin_prompt', 'protected': 'protected_package',
                                          'messenger': 'write_denied'}[stop]
    assert phone.inputs == []


# spec: ANYM-969
def test_a_password_field_of_the_bound_app_passes_only_for_a_run_that_names_its_secret():
    password = {'password': 'true'}
    plain = Phone(**password)
    with pytest.raises(Stop) as caught:  # S1 criterion 3 stays: no bound secret in this run.
        execute(plain, {'inputText': REF}, secrets=BOUND, guard=make_guard({}))
    assert caught.value.guard['rule'] == 'password_field' and plain.inputs == []
    bound = Phone(**password)
    assert execute(bound, {'inputText': REF}, secrets=BOUND,
                   guard=make_guard({'secret_packages': [PACKAGE], 'guard': None})) == {'success': True}
    assert bound.inputs == [VALUE]
    other = Phone(**password)
    with pytest.raises(Stop):  # A secret bound to another app does not open this one.
        execute(other, {'inputText': 'hello'}, guard=make_guard({'secret_packages': ['example.other']}))


# spec: ANYM-969
def test_the_worker_error_codes_are_definite_so_the_persistent_worker_stays():
    assert {'secret_package_mismatch', 'secret_unknown', 'secrets_unavailable'} <= DEFINITE


# spec: ANYM-969
def test_the_reference_tells_agents_how_to_name_a_secret():
    assert '${secret:NAME}' in reference()['secrets'] and 'inputText' in reference()['secrets']


# --- criterion 3: never echoed ----------------------------------------------------------------------------------


# spec: ANYM-969
def test_masker_replaces_each_occurrence_in_every_read_and_leaves_the_input_alone():
    masker = Masker()
    assert masker.mask({'a': VALUE}) == {'a': VALUE}  # Nothing typed yet: nothing to mask.
    masker.add(NAME, VALUE)
    result = {'text': f'{VALUE} and {VALUE}', 'rows': [{'field': VALUE}, 3, None, True, 1.5],
              'lines': [{'text': f'x{VALUE}y', 'bounds': [1, 2]}], 'deep': {'tuple': (VALUE, 'plain')}}
    masked = masker.mask(result)
    assert masked['text'] == f'{REF} and {REF}' and masked['rows'] == [{'field': REF}, 3, None, True, 1.5]
    assert masked['lines'] == [{'text': f'x{REF}y', 'bounds': [1, 2]}] and masked['deep']['tuple'] == (REF, 'plain')
    assert VALUE not in json.dumps(masked) and result['text'].count(VALUE) == 2  # The input is not mutated.
    assert masker.mask(masked) == masked  # Masking twice changes nothing.


# spec: ANYM-969
@pytest.mark.parametrize('value', ['secret', 'SHOP', 'PASSWORD', 'SHOP_PASSWORD', ':', '${', '}'])
def test_masker_keeps_a_reference_whole_when_the_value_is_part_of_its_text(value):
    masker = Masker()
    masker.add(NAME, value)
    assert masker.mask(f'typed {REF} then {value}') == f'typed {REF} then {REF}'


# spec: ANYM-969
def test_masker_with_two_secrets_masks_the_longer_value_first():
    masker = Masker()
    masker.add('SHORT_ONE', 'abc')
    masker.add('LONGER_ONE', 'abcdef')
    assert masker.mask('abcdef abc') == '${secret:LONGER_ONE} ${secret:SHORT_ONE}'


# spec: ANYM-969
def test_masker_drops_ocr_words_of_a_masked_line_and_does_not_touch_keys():
    masker = Masker()
    masker.add(NAME, 'my pass phrase')
    line = {'text': 'my pass phrase', 'words': [{'text': 'my'}, {'text': 'pass'}, {'text': 'phrase'}]}
    other = {'text': 'Search', 'words': [{'text': 'Search'}]}
    assert masker.mask({'lines': [line, other]}) == {'lines': [{'text': REF, 'words': []}, other]}
    assert masker.mask({'my pass phrase': 1}) == {'my pass phrase': 1}


# spec: ANYM-969
def test_a_run_masks_every_result_and_error_from_the_typing_call_on(tmp_path):
    store = SecretStore(tmp_path / 'state')
    store.set(NAME, PACKAGE, VALUE)
    run = Run(store)
    run.bind([{'tapOn': 'Password'}, {'inputText': REF}, {'copyTextFrom': 'Password'}])
    sent = []

    async def send(command, **options):
        sent.append((command, options))
        if command == {'copyTextFrom': 'Password'}:
            return {'text': VALUE, 'rows': [{'field': f'<{VALUE}>'}]}
        if command == {'tapOn': 'Missing'}:
            error = Refused('ambiguous_selector')
            error.details = {'candidates': [VALUE]}
            error.hint = f'saw {VALUE}'
            raise error
        return {'success': True, 'echo': VALUE if command == 'before' else 'fine'}

    async def scenario():
        assert (await run.call(send, 'before'))['echo'] == VALUE  # Nothing typed yet: nothing to mask.
        await run.call(send, {'inputText': REF})
        assert (await run.call(send, {'copyTextFrom': 'Password'})) == {'text': REF, 'rows': [{'field': f'<{REF}>'}]}
        with pytest.raises(Refused) as caught:
            await run.call(send, {'tapOn': 'Missing'})
        assert caught.value.details == {'candidates': [REF]} and caught.value.hint == f'saw {REF}'
    asyncio.run(scenario())
    assert [options.get('secrets') for _, options in sent] == [None, BOUND, None, None]  # Only the typing call.
    assert all(options['secret_packages'] == [PACKAGE] for _, options in sent)


# spec: ANYM-969
def test_the_failure_observation_masks_before_it_cuts_long_text_and_takes_no_screenshot(tmp_path, monkeypatch):
    async def scenario():
        store = Store(tmp_path / 'state')
        devices = ui_devices(tmp_path, store)
        SecretStore(store.directory).set(NAME, PACKAGE, VALUE)
        taken = []

        async def hierarchy(device):
            # The value straddles the 256-character cut a stored observation makes.
            return {'elements': [{'text': 'x' * 250 + VALUE, 'class': 'android.widget.EditText'}]}

        async def capture(*args):
            taken.append(args)
            return {'image_file': {'file_id': 'unused'}}
        monkeypatch.setattr(devices, 'capture_hierarchy', hierarchy)
        monkeypatch.setattr(devices, 'capture_image', capture)
        try:
            run = Run(devices.secrets)
            run.bind([{'inputText': REF}])
            idle = await devices.failure_observation('owner', 'phone', run)  # Nothing typed: the usual observation.
            assert taken and 'image_file' in idle and idle['elements'][0]['text'] == ('x' * 250 + VALUE)[:256]
            taken.clear()
            run.attach({'inputText': REF})
            typed = await devices.failure_observation('owner', 'phone', run)
            assert taken == [] and typed['screenshot_withheld'] == 'secret_typed' and 'image_file' not in typed
            # Six characters of the value would survive a cut made before masking; five must not appear anywhere.
            assert typed['elements'][0]['text'].startswith('x' * 250) and VALUE[:5] not in json.dumps(typed)
            stop = await devices.stop_observation('owner', 'phone', PACKAGE, False, run)
            assert taken == [] and 'image_file' not in stop and VALUE[:5] not in json.dumps(stop)
        finally:
            await devices.close()
            store.close()
    asyncio.run(scenario())


def ui_devices(tmp_path, store):
    from deedkit_android.device import Devices
    return Devices(SimpleNamespace(devices={'phone': 'SERIAL'}, adb='/synthetic', uiautomator2_jar='/synthetic',
                                   state_dir=str(tmp_path / 'state')), store)


# spec: ANYM-969
def test_a_typed_value_reaches_no_receipt_row_file_log_or_read(tmp_path, capsys, caplog):
    config = host(tmp_path)
    assert cli(config, 'set', NAME, '--package', PACKAGE, stdin=VALUE + '\n') == 0
    app = create_app(config)
    field, seen, captures = {'text': ''}, [], []

    async def call(device, command, **options):
        seen.append((command, options))
        if command == '__guard__':
            return {'success': True}
        if command == '__hierarchy__':
            return {'xml': screen_xml(text=field['text'])}
        name = next(iter(command))
        if name == 'inputText':
            assert leaks(tmp_path) == []  # Nothing on disk holds the value while it is typed.
            typed = options['secrets'][NAME]['value'] if command['inputText'] == REF else command['inputText']
            field['text'] += typed
        elif name == 'copyTextFrom':
            return {'text': field['text']}
        elif name == 'readScreenText':
            return {'key': 'field', 'truncated': False,
                    'lines': [{'text': field['text'], 'bounds': {'left': 0, 'top': 0, 'right': 9, 'bottom': 9},
                               'confidence': 99}]}
        elif command == {'tapOn': 'Missing'}:
            error = Refused('element_not_found')
            error.details = {'screen_text': field['text']}  # A worker error that quotes the screen.
            raise error
        return {'success': True}

    async def capture(owner, device, mode, crop):
        captures.append(mode)
        return {'image_file': {**app.state.store.save(owner, b'synthetic PNG'), 'mime_type': 'image/png'}}

    async def noop(*args):
        pass

    with TestClient(app) as client:
        app.state.devices.ui = SimpleNamespace(call=call, close=noop, ensure_fenced_process_stopped=noop)
        app.state.devices.capture_image = capture

        def post(name, body, code=200):
            response = client.post('/v1/' + name, json=body, headers={'Authorization': 'Bearer one'})
            assert response.status_code == code, response.text
            return response.json()

        session = post('session', {'action': 'acquire', 'device_id': 'home'})['session_id']

        def run(source):
            body = {'device_id': 'home', 'session_id': session, 'request_id': str(uuid.uuid4()), 'yaml': source}
            return body, post('run_flow', body)

        failing = ('- tapOn: Password\n- inputText: ${secret:SHOP_PASSWORD}\n- copyTextFrom: Password\n'
                   '- readScreenText: {key: field}\n- tapOn: Missing\n')
        body, failed = run(failing)
        assert failed['state'] == 'failed' and failed['error'] == 'element_not_found'
        assert failed['failed_command'] == {'tapOn': 'Missing'} and failed['details'] == {'screen_text': REF}
        assert failed['steps'][2]['result'] == {'text': REF}  # copyTextFrom read the field back.
        assert failed['data']['field'][0]['text'] == REF and failed['steps'][-1]['details'] == {'screen_text': REF}
        observation = failed['observation']
        assert any(item.get('text') == REF for item in observation['elements'])
        assert 'image_file' not in observation and observation['screenshot_withheld'] == 'secret_typed'
        assert captures == []  # No screenshot is taken after a typed secret: it could show the value.

        after_failed = len(seen)
        field['text'] = ''
        paused_body, paused = run('- inputText: ${secret:SHOP_PASSWORD}\n- copyTextFrom: Password\n'
                                  '- checkpoint: verify\n- inputText: ${secret:SHOP_PASSWORD}\n- back\n')
        assert paused['state'] == 'paused' and REF in paused['remaining_yaml'] and VALUE not in paused['remaining_yaml']
        resumed = compile_scenario(LIBRARY, 'one', paused['remaining_yaml'], {}, secrets=lambda: {NAME: PACKAGE})[0]
        assert REF in json.dumps(resumed)  # The continuation is runnable and still holds only the reference.
        after_paused = len(seen)

        field['text'] = ''
        _, control = run('- tapOn: Missing\n')
        assert control['state'] == 'failed' and captures == ['file'] and 'image_file' in control['observation']

        # Only a typing command carries the value, and only a run that names a secret carries the guard's binding.
        def worker_calls(calls):
            return [(command, options) for command, options in calls if command != '__hierarchy__']
        named = worker_calls(seen[:after_paused])
        assert all(options.get('secret_packages') == [PACKAGE] for _, options in named)
        typing = [(command, options) for command, options in named if 'secrets' in options]
        assert [command for command, _ in typing] == [{'inputText': REF}] * 2
        assert all(options['secrets'] == BOUND for _, options in typing)
        assert all('secrets' not in options and 'secret_packages' not in options
                   for _, options in worker_calls(seen[after_paused:]))  # A run without a reference: unchanged.
        assert len(worker_calls(seen[:after_failed])) == 5 and len(named) == 5 + 3  # Run two: 2 steps and the guard.

        replayed = len(seen)
        assert post('run_flow', body) == failed and len(seen) == replayed  # A replay calls the device no more.

        capabilities = post('capabilities', {})
        assert capabilities['secrets'] == [{'name': NAME, 'package': PACKAGE}]

        # The request identity and the stored fingerprint hold the reference text, never the value.
        args = RunFlow.model_validate(body).model_dump()
        identity = {key: value for key, value in args.items() if key not in {'request_id', 'session_id', 'wait_s'}}
        flow = compile_scenario(LIBRARY, 'one', failing, {}, secrets=lambda: {NAME: PACKAGE})[0]
        payload = {'device_id': 'home', 'timeout_s': 120, 'flow': flow}
        assert VALUE not in json.dumps(identity) and VALUE not in json.dumps(payload) and REF in json.dumps(flow)
        every = [failed, paused, control, capabilities]
        assert VALUE not in json.dumps(every)
        rows = {'identity': hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
                'fingerprint': hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()}
    # The service's own SQLite connection belongs to its thread; read the file with another one.
    database = sqlite3.connect(tmp_path / 'state' / 'state.sqlite3')
    stored = database.execute('SELECT identity FROM operation_requests WHERE id=?', (body['request_id'],)).fetchone()
    assert stored == (rows['identity'],)
    stored = database.execute('SELECT fingerprint FROM operations WHERE id=?', (body['request_id'],)).fetchone()
    assert stored == (rows['fingerprint'],)
    database.close()
    assert leaks(tmp_path) == []  # The SQLite file, the stored files and the temporary directories.
    shown = capsys.readouterr()
    assert VALUE not in shown.out + shown.err + caplog.text


# spec: ANYM-969
def test_a_package_mismatch_fails_the_run_definitely_and_names_the_packages_not_the_value(tmp_path, monkeypatch):
    async def scenario():
        store = Store(tmp_path / 'state')
        devices = ui_devices(tmp_path, store)
        SecretStore(store.directory).set(NAME, PACKAGE, VALUE)

        async def call(device, command, **options):
            if command == '__hierarchy__':
                return {'xml': screen_xml(package='example.other')}
            error = Refused('secret_package_mismatch')
            error.details = {'secret': NAME, 'reason': 'foreground_package', 'bound_package': PACKAGE,
                             'foreground_package': 'example.other'}
            raise error

        async def capture(*args):
            return {'screenshot_error': 'unavailable'}
        monkeypatch.setattr(devices.ui, 'call', call)
        monkeypatch.setattr(devices, 'capture_image', capture)
        session = store.reserve('owner', 'phone', 'acquire')['session_id']
        flow = compile_flow('- inputText: "${secret:SHOP_PASSWORD}"', {})
        try:
            receipt = await devices.execute('owner', 'phone', session, str(uuid.uuid4()), None, 10, 10, flow)
            assert receipt['state'] == 'failed' and receipt['error'] == 'secret_package_mismatch'
            assert receipt['details']['bound_package'] == PACKAGE and receipt['failed_command'] == {'inputText': REF}
            assert VALUE not in json.dumps(receipt) and not devices.locks['phone'].locked()
        finally:
            await devices.close()
            store.close()
    asyncio.run(scenario())


# spec: ANYM-969
def test_a_secret_deleted_after_admission_fails_the_run_before_any_input(tmp_path, monkeypatch):
    async def scenario():
        store = Store(tmp_path / 'state')
        devices = ui_devices(tmp_path, store)
        secrets = SecretStore(store.directory)
        secrets.set(NAME, PACKAGE, VALUE)
        calls = []

        async def call(device, command, **options):
            calls.append(command)
            return {'success': True}

        async def capture(*args):
            return {'screenshot_error': 'unavailable'}
        monkeypatch.setattr(devices.ui, 'call', call)
        monkeypatch.setattr(devices, 'capture_image', capture)
        monkeypatch.setattr(devices, 'capture_hierarchy', lambda device: _hierarchy())
        session = store.reserve('owner', 'phone', 'acquire')['session_id']
        flow = compile_flow('- back\n- inputText: "${secret:SHOP_PASSWORD}"', {})
        secrets.delete(NAME)
        try:
            receipt = await devices.execute('owner', 'phone', session, str(uuid.uuid4()), None, 10, 10, flow)
            assert receipt['state'] == 'failed' and receipt['error'] == 'secret_unknown'
            assert calls == [] and receipt['commands_executed'] == 0 and not devices.locks['phone'].locked()
        finally:
            await devices.close()
            store.close()
    asyncio.run(scenario())


async def _hierarchy():
    return {'elements': []}


# --- criterion 4: capabilities ----------------------------------------------------------------------------------


# spec: ANYM-969
def test_capabilities_list_secret_names_and_packages_only(tmp_path):
    config = host(tmp_path)
    cli(config, 'set', NAME, '--package', PACKAGE, stdin=VALUE)
    cli(config, 'set', 'OTHER_LOGIN', '--package', 'example.other', stdin='x' + VALUE)
    with TestClient(create_app(config)) as client:
        response = client.post('/v1/capabilities', json={}, headers={'Authorization': 'Bearer one'})
        assert response.json()['secrets'] == [{'name': 'OTHER_LOGIN', 'package': 'example.other'},
                                              {'name': NAME, 'package': PACKAGE}]
        assert VALUE not in response.text
        (tmp_path / 'state' / 'secrets.json').chmod(0o644)  # An unreadable file never breaks the capability read.
        broken = client.post('/v1/capabilities', json={}, headers={'Authorization': 'Bearer one'})
        assert broken.status_code == 200 and broken.json()['secrets'] == [] and VALUE not in broken.text


# --- fakes ------------------------------------------------------------------------------------------------------


def screen_xml(*, package=PACKAGE, field_class='android.widget.EditText', focused='true', field_package=None,
               password='false', text='', extra=''):
    return ('<hierarchy>'
            f'<node class="android.widget.FrameLayout" package="{package}" clickable="false" bounds="[0,0][1000,2000]">'
            f'<node class="{field_class}" package="{field_package or package}" resource-id="example.shop:id/pw" '
            f'focused="{focused}" password="{password}" text={quoteattr(text)} clickable="true" '
            f'bounds="[0,100][1000,180]"/>{extra}</node></hierarchy>')


class Field:
    def __init__(self, phone, selector):
        self.phone, self.selector = phone, selector

    @property
    def info(self):
        return self.phone.field_info

    def set_text(self, value, timeout=0):
        self.phone.inputs.append(value)

    def get_text(self, timeout=0):
        return ''


class Phone:
    """A fake uiautomator2 device with one text field; records every input."""

    def __init__(self, package=PACKAGE, extra='', field_info=None, **screen):
        self.package, self.inputs = package, []
        self.xml = screen_xml(package=package, extra=extra, **screen)
        self.field_info = field_info or {'className': screen.get('field_class', 'android.widget.EditText'),
                                         'packageName': screen.get('field_package') or package,
                                         'focused': screen.get('focused', 'true') == 'true'}
        self.clicks, self.keys, self.swipes = [], [], []
        self._dev = SimpleNamespace(shell2=lambda *args, **options: SimpleNamespace(returncode=0, output=''))

    def jsonrpc_call(self, method, params=None, timeout=10):
        return self.xml if method == 'dumpWindowHierarchy' else True

    def app_current(self):
        return {'package': self.package, 'activity': '.Main'}

    def window_size(self):
        return 1000, 2000

    def __call__(self, **selector):
        return Field(self, selector)

    def acted(self):
        return bool(self.clicks or self.keys or self.swipes or self.inputs)


SITUATIONS = {
    'bound': {'types': True, 'screen': {}},
    'foreground elsewhere': {'types': False, 'screen': {'package': 'example.other'}},
    'foreground elsewhere, field of the bound app': {'types': False,
                                                     'screen': {'package': 'example.other', 'field_package': PACKAGE}},
    'bound elsewhere': {'types': False, 'bound': 'example.other', 'screen': {}},
    'field of another package': {'types': False, 'screen': {'field_package': 'example.other'}},
    'not focused': {'types': False, 'screen': {'focused': 'false'}},
    'not editable': {'types': False, 'screen': {'field_class': 'android.widget.Button'}},
}


def secret_command(form):
    if form == 'inputText':
        return {'inputText': REF}
    if form == 'setText':
        return {'setText': {'resourceId': 'example.shop:id/pw', 'value': REF}}
    return {'typeInto': {'selector': 'password', 'text': REF, 'app_map': validate_map(MAP),
                         'map_ref': {'kind': 'map', 'name': 'shop', 'version': 'v1'}}}

