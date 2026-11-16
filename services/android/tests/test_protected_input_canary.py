"""Root-only synthetic canary runner: fake device/server/adb only, never a phone, ADB, model or credential (ANYM-998)."""

import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
import threading
import time

import pytest

from deedkit_android.protected_input import InputPurpose, OwnedToolInputBinding

SCRIPT = Path(__file__).resolve().parents[3] / 'scripts' / 'android_protected_input_canary.py'
spec = importlib.util.spec_from_file_location('android_protected_input_canary', SCRIPT)
canary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(canary)

VALUES = ('111122223333', '444455556666')  # Synthetic canary digits, never a real PIN.
ADB, TESS, SERIAL = '/fake/adb', '/fake/tesseract', 'emulator-5554'


class FakeDevice:
    """Fake uiautomator2 JSON-RPC server plus fake adb/tesseract argv runner."""

    def __init__(self, *, reply='true', leak=None, change=None, preinstalled=False, fail=None):
        self.reply, self.leak, self.change, self.preinstalled, self.fail = reply, leak, change, preinstalled, fail
        self.foreground, self.value, self.installed = 'home', '', False
        self.writes, self.argv, self.fixture_dumps = [], [], 0
        self.server = socket.create_server(('127.0.0.1', 0))
        self.port = self.server.getsockname()[1]
        self.closed = False
        threading.Thread(target=self.serve, daemon=True).start()

    def close(self):
        self.closed = True
        self.server.close()

    def xml(self):
        if self.foreground != 'fixture':
            return ('<?xml version="1.0"?><hierarchy><node package="com.example.launcher" '
                    'class="android.widget.FrameLayout" text="" resource-id="" focused="false" password="false" '
                    'enabled="true" bounds="[0,0][100,100]"/></hierarchy>')
        self.fixture_dumps += 1
        focused = 'false' if self.change == 'focus' and self.fixture_dumps >= 2 else 'true'
        resource = canary.RESOURCE_ID if self.change != 'resource' else canary.PACKAGE + ':id/other'
        text = self.value if self.leak == 'xml' else '•' * len(self.value)
        return ('<?xml version="1.0"?><hierarchy><node package="%s" class="android.widget.FrameLayout" text="" '
                'resource-id="" focused="false" password="false" enabled="true" bounds="[0,0][100,100]">'
                '<node package="%s" class="android.widget.EditText" text="%s" resource-id="%s" focused="%s" '
                'password="true" enabled="true" bounds="[10,40][90,60]"/></node></hierarchy>'
                % (canary.PACKAGE, canary.PACKAGE, text, resource, focused))

    def serve(self):
        while not self.closed:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        with conn:
            conn.settimeout(2)
            data = bytearray()
            try:
                while b'\r\n\r\n' not in data:
                    data += conn.recv(4096)
                head, body = bytes(data).split(b'\r\n\r\n', 1)
                length = int([v for k, v in (line.split(b':', 1) for line in head.split(b'\r\n')[1:])
                              if k.lower() == b'content-length'][0])
                while len(body) < length:
                    body += conn.recv(4096)
            except (OSError, IndexError, ValueError):
                return
            message = json.loads(body)
            if message['method'] == 'dumpWindowHierarchy':
                response = dict(jsonrpc='2.0', id=message['id'], result=self.xml())
            elif message['method'] == 'setText':
                self.writes.append(message['params'][0])
                if self.reply in ('true', 'false', 'error'):
                    self.value = message['params'][1] if self.reply != 'false' else ''
                if self.reply == 'hang':
                    self.value = message['params'][1]
                    time.sleep(0.5)
                    return
                if self.reply == 'close':
                    return
                response = (dict(jsonrpc='2.0', id=message['id'], error=dict(code=-32001, message='x'))
                            if self.reply == 'error' else
                            dict(jsonrpc='2.0', id=message['id'], result=self.reply == 'true'))
            else:
                response = dict(jsonrpc='2.0', id=message['id'], error=dict(code=-32601, message='method'))
            payload = json.dumps(response).encode()
            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                         + str(len(payload)).encode() + b'\r\n\r\n' + payload)

    def run(self, argv, input=None, timeout=None):
        self.argv.append(list(argv))
        if argv[0] == TESS:
            assert input.startswith(b'\x89PNG')
            return 0, (self.value.encode() if self.leak == 'ocr' else b'Synthetic input only\n')
        assert argv[:3] == [ADB, '-s', SERIAL]
        rest = argv[3:]
        if self.fail and rest[:len(self.fail)] == self.fail:
            raise OSError('adb exploded ' + self.value)
        if rest == ['get-state']:
            return 0, b'device\n'
        if rest[:3] == ['shell', 'pm', 'path']:
            return (0, b'package:/data/app/base.apk\n') if self.preinstalled or self.installed else (1, b'')
        if rest[0] == 'install':
            self.installed = True
            return 0, b'Success\n'
        if rest == ['shell', 'date', '+%s']:
            return 0, b'1000\n'
        if rest[:3] == ['shell', 'am', 'start']:
            self.foreground, self.value = 'fixture', ''
            return 0, b'Status: ok\n'
        if rest == ['shell', 'input', 'keyevent', 'KEYCODE_HOME'] or rest[:3] == ['shell', 'am', 'force-stop']:
            self.foreground, self.value = 'home', ''
            return 0, b''
        if rest[0] == 'uninstall':
            self.installed = False
            return 0, b'Success\n'
        if rest == ['exec-out', 'screencap', '-p']:
            return 0, b'\x89PNG\r\n\x1a\nfake'
        if rest[0] == 'logcat':
            return 0, (b'I/Fixture: ' + self.value.encode() if self.leak == 'logcat' else b'I/ActivityManager: ok\n')
        raise AssertionError(rest)


@pytest.fixture
def apk(tmp_path):
    path = tmp_path / 'fixture.apk'
    path.write_bytes(b'synthetic apk bytes')
    return path


def args(tmp_path, apk, device, **changes):
    values = dict(serial=SERIAL, u2_port=device.port, tesseract=TESS, adb=ADB, apk=str(apk),
                  state_dir=str(tmp_path / 'state'), run_id='run-1', report=None)
    values.update(changes)
    return canary.Options(**values)


def execute(tmp_path, apk, device, **changes):
    try:
        return asyncio.run(canary.execute(args(tmp_path, apk, device, **changes), run=device.run, values=iter(VALUES),
                                          apk_sha256=canary.sha256_file(apk), channel_timeout=0.3))
    finally:
        device.close()


def assert_value_free(report, device, *outputs):
    text = json.dumps(report) + json.dumps(device.argv) + ''.join(outputs)
    for value in VALUES:
        assert value not in text


# spec: ANYM-998
def test_plan_is_default_offline_and_names_invocation_undo_and_typed_prerequisites(capsys, monkeypatch):
    monkeypatch.setattr(canary.subprocess, 'run', lambda *a, **k: pytest.fail('plan ran a process'))
    monkeypatch.setattr(canary.socket, 'create_connection', lambda *a, **k: pytest.fail('plan opened a socket'))
    assert canary.main([]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['mode'] == 'plan' and plan['status'] == 'PLAN_ONLY_NOT_EXECUTED'
    assert plan['interface']['binding'] == 'OwnedToolInputBinding(android_order_control/request_protected_input)'
    assert plan['interface']['write'] == 'ExistingInputChannel.set_password_once: POST /jsonrpc/0 setText'
    prerequisites = {item['id']: item['status'] for item in plan['prerequisites']}
    assert prerequisites['global_native_fence'] == 'unavailable'
    assert prerequisites['native_protected_window'] == 'unavailable'
    assert prerequisites['device_allocation'] == prerequisites['u2_server_forward'] == 'root_supplied'
    assert plan['invocation'][-1].startswith('uv run python ../../scripts/android_protected_input_canary.py execute')
    commands = [' '.join(step) for step in plan['device_commands']]
    assert 'adb -s SERIAL install APK' in commands and 'adb -s SERIAL uninstall app.deedkit.protectedfixture' in commands
    assert [' '.join(step) for step in plan['undo']][-1] == 'adb -s SERIAL uninstall app.deedkit.protectedfixture'
    assert plan['unblocks_real_pin'] is False


# spec: ANYM-998
def test_positive_and_lost_reply_measure_every_reader_without_echoing_the_value(tmp_path, apk, capsys):
    device = FakeDevice()
    report = execute(tmp_path, apk, device)
    assert report['verdict'] == 'PASS', report['reasons']
    positive, lost = report['scenarios']
    assert positive['status'] == 'owner_done' and positive['resume_once'] is True and positive['resume_allowed']
    assert positive['native_call_id'] == 'canary-run-1-call-positive' and positive['native_request_id'] == 1
    assert lost['status'] == 'unknown' and lost['resume_once'] is False and not lost['resume_allowed']
    assert lost['relay'] == {'forwarded': 1, 'device_result': 'accepted', 'reply_delivered': False}
    assert lost['journal_status'] == 'unknown'  # Hold/tombstone retained; never retried.
    assert len(device.writes) == 2  # Exactly one setText per scenario.
    checkpoints = {(row['checkpoint'], row['reader']): row for row in report['measurements']}
    assert set(checkpoints) == {(c, r) for c in ('after_write', 'after_receipt', 'lost_reply_hold')
                                for r in ('ui_xml', 'agent_compact', 'screenshot_ocr', 'logcat')}
    assert all(row['state'] == 'absent' for row in checkpoints.values())
    assert checkpoints['after_write', 'ui_xml']['field'] == {'present': True, 'masked': True, 'length_matches': True}
    assert checkpoints['lost_reply_hold', 'ui_xml']['field']['length_matches'] is True
    assert all(s['field_gone'] for s in report['scenarios']) and report['undo']['uninstall'] == 0
    assert not device.installed and report['unblocks_real_pin'] is False
    assert_value_free(report, device, capsys.readouterr().out)
    with sqlite3.connect(tmp_path / 'state' / 'journal' / 'protected-input.sqlite3') as db:
        rows = dict(db.execute('SELECT operation_id, status FROM inputs'))
    assert rows == {'canary-run-1-positive': 'owner_done', 'canary-run-1-lost-reply': 'unknown'}
    assert not list((tmp_path / 'state' / 'refs').iterdir())


# spec: ANYM-998
def test_reused_state_never_writes_again(tmp_path, apk):
    assert execute(tmp_path, apk, FakeDevice())['verdict'] == 'PASS'
    device = FakeDevice()
    report = execute(tmp_path, apk, device)
    assert report['verdict'] == 'BLOCKED' and report['reasons'] == ['state_dir_reused']
    assert device.writes == [] and device.argv == []


# spec: ANYM-998
@pytest.mark.parametrize('leak', ['xml', 'ocr', 'logcat'])
def test_a_reader_leak_fails_the_canary_without_echoing_the_value(tmp_path, apk, leak):
    device = FakeDevice(leak=leak)
    report = execute(tmp_path, apk, device)
    assert report['verdict'] == 'LEAK'
    leaked = {(row['checkpoint'], row['reader']) for row in report['measurements'] if row['state'] == 'present'}
    reader = {'xml': 'ui_xml', 'ocr': 'screenshot_ocr', 'logcat': 'logcat'}[leak]
    assert ('after_write', reader) in leaked
    assert_value_free(report, device)


# spec: ANYM-998
@pytest.mark.parametrize('reply,status', [('false', 'rejected'), ('error', 'unknown'), ('hang', 'unknown'),
                                          ('close', 'unknown')])
def test_rejected_error_timeout_and_lost_reply_hold_without_retry_or_second_scenario(tmp_path, apk, reply, status):
    device = FakeDevice(reply=reply)
    report = execute(tmp_path, apk, device)
    assert report['verdict'] == 'INCONCLUSIVE'
    assert [s['status'] for s in report['scenarios']] == [status]
    assert report['scenarios'][0]['journal_status'] == status and not report['scenarios'][0]['resume_allowed']
    assert len(device.writes) == 1 and report['scenarios'][0]['field_gone'] is True
    assert report['undo']['uninstall'] == 0 and not device.installed
    assert_value_free(report, device)


# spec: ANYM-998
@pytest.mark.parametrize('change,status', [('focus', 'target_changed'), ('resource', 'input_target_unsupported')])
def test_target_changes_never_write(tmp_path, apk, change, status):
    device = FakeDevice(change=change)
    report = execute(tmp_path, apk, device)
    assert [s['status'] for s in report['scenarios']] == [status]
    assert device.writes == [] and report['verdict'] == 'INCONCLUSIVE' and not device.installed


# spec: ANYM-998
def test_source_changes_refuse_before_any_device_mutation(tmp_path, apk):
    device = FakeDevice()
    report = asyncio.run(canary.execute(args(tmp_path, apk, device), run=device.run, values=iter(VALUES),
                                        apk_sha256='0' * 64))
    assert (report['verdict'], report['reasons'], device.argv) == ('BLOCKED', ['fixture_apk_mismatch'], [])
    device.close()
    device = FakeDevice(preinstalled=True)
    report = execute(tmp_path, apk, device, state_dir=str(tmp_path / 'other'))
    assert (report['verdict'], report['reasons']) == ('BLOCKED', ['fixture_already_installed'])
    assert not [argv for argv in device.argv if argv[3] in ('install', 'uninstall')] and device.writes == []
    device = FakeDevice()
    report = execute(tmp_path, apk, device, state_dir=str(tmp_path / 'third'), tesseract='relative/tesseract')
    assert (report['verdict'], report['reasons'], device.argv) == ('BLOCKED', ['ocr_reader_unavailable'], [])


# spec: ANYM-998
def test_device_failure_mid_run_reports_a_fixed_code_and_still_undoes(tmp_path, apk):
    device = FakeDevice(fail=['shell', 'input', 'keyevent'])
    report = execute(tmp_path, apk, device)
    assert report['verdict'] == 'INCONCLUSIVE' and 'device_command_failed' in report['reasons']
    assert report['scenarios'][0]['status'] == 'unknown' and len(device.writes) == 1
    assert report['undo']['uninstall'] == 0 and not device.installed
    assert_value_free(report, device)


# spec: ANYM-998
def test_canary_window_is_fixture_only(tmp_path):
    binding = OwnedToolInputBinding('canary-root', SERIAL, 'canary-task', 'thread', 'turn', 1, 'call',
                                    'com.example.shop', canary.TARGET_ID, InputPurpose.LOGIN)
    window = canary.canary_window(None, 1, None, None)

    async def enter():
        async with window(binding):
            pass
    with pytest.raises(canary.CanaryError, match='fixture_only'):
        asyncio.run(enter())
