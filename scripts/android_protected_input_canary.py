#!/usr/bin/env python3
"""Root-only synthetic canary for the existing protected password-EditText path (ANYM-998).

Default `plan` is offline: it prints the exact root invocation, device commands, undo and typed
prerequisites, and touches no process, socket, device, model or credential. `execute` is for root's
allocated run only, against the disposable fixture APK (app.deedkit.protectedfixture) with
runner-generated synthetic digits; it never accepts a value or reference from its caller.

It reuses the source path unchanged: OwnedToolInputBinding -> ProtectedInputRequest ->
ManualProtectedInput journal (durable one-shot, no replay) -> enter_password_once ->
ExistingInputChannel.set_password_once (one raw `setText` on the existing uiautomator2 server).
CanaryWindow is NOT a fence: it is fixture-only and exists to measure what the ordinary readers
(raw hierarchy, the agent's compact text, screenshot OCR, logcat) show after write, after the
positive receipt and during a lost-reply hold. A PASS does not unblock real automatic PIN entry:
the global native/OS read fence (protected_window for native Codex) remains unavailable.

Run from services/android: `uv run python ../../scripts/android_protected_input_canary.py [plan|execute ...]`.
"""

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from xml.etree import ElementTree

from deedkit_android.agent_screen import compact
from deedkit_android.protected_input import (
    InputPurpose, InputTransport, ManualProtectedInput, OwnedToolInputBinding, ProtectedInputError,
    ProtectedInputRequest, _identifier,
)
from deedkit_android.protected_input_transport import (
    ExistingInputChannel, FocusedPasswordTarget, LocalInputReference, enter_password_once,
)

PACKAGE = 'app.deedkit.protectedfixture'
ACTIVITY = PACKAGE + '/.FixtureActivity'
RESOURCE_ID = PACKAGE + ':id/protected_input'
TARGET_ID = 'fixture-protected-input'  # Host-minted opaque target identity.
APK_SHA256 = 'd2970eaa4475b6710bc30bd3fac8d8ab4014eb399ba69bd79790c95e178e97ab'  # thr_v7cjp524rq build-receipt
SERVER_DEVICE_PORT = 9008  # uiautomator2 3.x server inside the device
READERS = ('ui_xml', 'agent_compact', 'screenshot_ocr', 'logcat')
CHECKPOINTS = ('after_write', 'after_receipt', 'lost_reply_hold')
GET_STATE, PM_PATH, DATE = ('get-state',), ('shell', 'pm', 'path', PACKAGE), ('shell', 'date', '+%s')
INSTALL, START = ('install', '{apk}'), ('shell', 'am', 'start', '-W', '-n', ACTIVITY)
HOME, FORCE_STOP = ('shell', 'input', 'keyevent', 'KEYCODE_HOME'), ('shell', 'am', 'force-stop', PACKAGE)
UNINSTALL, SCREENCAP = ('uninstall', PACKAGE), ('exec-out', 'screencap', '-p')
LOGCAT = ('logcat', '-d', '-v', 'epoch', '-T', '{epoch}.000')
MAX_RESPONSE = 4 << 20


class CanaryError(Exception):
    """Fixed codes only; never a value, output or lower-layer message."""


@dataclass(frozen=True)
class Options:
    serial: str
    u2_port: int
    tesseract: str
    adb: str
    apk: str
    state_dir: str
    run_id: str
    report: str | None = None


def sha256_file(path):
    with open(path, 'rb') as stream:
        return hashlib.sha256(stream.read()).hexdigest()


def _run(argv, input=None, timeout=30):
    done = subprocess.run(argv, input=input, capture_output=True, timeout=timeout, check=False)
    return done.returncode, done.stdout  # stderr is never exported


def _read_http(sock, *, until_close):
    data = bytearray()
    while True:
        if not until_close and b'\r\n\r\n' in data:
            head, body = bytes(data).split(b'\r\n\r\n', 1)
            lengths = [int(v) for k, v in (line.split(b':', 1) for line in head.split(b'\r\n')[1:] if b':' in line)
                       if k.strip().lower() == b'content-length']
            if len(lengths) == 1 and len(body) >= lengths[0]:
                return bytes(data)
        chunk = sock.recv(65536)
        if not chunk:
            return bytes(data)
        data += chunk
        if len(data) > MAX_RESPONSE:
            raise ValueError('response_too_large')


def _decode(raw):
    head, _, body = raw.partition(b'\r\n\r\n')
    lines = head.split(b'\r\n')
    if not lines[0].startswith(b'HTTP/1.') or b' 200 ' not in lines[0] + b' ':
        raise ValueError('status')
    if any(b'transfer-encoding' in line.lower() and b'chunked' in line.lower() for line in lines[1:]):
        decoded, rest = bytearray(), body
        while True:
            size, _, rest = rest.partition(b'\r\n')
            size = int(size.split(b';')[0], 16)
            if size == 0:
                break
            decoded, rest = decoded + rest[:size], rest[size + 2:]
        body = bytes(decoded)
    return json.loads(body)


def rpc(port, method, params, timeout=8):
    """One value-free JSON-RPC read on the existing forwarded server (never setText)."""
    body = json.dumps(dict(jsonrpc='2.0', id=1, method=method, params=params)).encode()
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=timeout) as sock:
            sock.sendall(b'POST /jsonrpc/0 HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n'
                         b'Connection: close\r\nContent-Length: %d\r\n\r\n' % len(body) + body)
            response = _decode(_read_http(sock, until_close=True))
        if not isinstance(response, dict) or 'error' in response or 'result' not in response:
            raise ValueError('rpc')
        return response['result']
    except (OSError, ValueError, TypeError):
        raise CanaryError('u2_rpc_failed') from None


def leaked(value, text):
    """Exact or separator-split occurrence of the synthetic digits."""
    return value in text or value in re.sub(r'[\s.,:;_/|\\\-•·*]', '', text)


def _nodes(xml):
    try:
        return list(ElementTree.fromstring(xml).iter('node'))
    except ElementTree.ParseError:
        return []


def field_state(xml, value):
    node = next((n for n in _nodes(xml) if n.get('resource-id') == RESOURCE_ID), None)
    text = (node.get('text') or '') if node is not None else ''
    return {'present': node is not None, 'masked': bool(text) and not re.search(r'[0-9]', text),
            'length_matches': node is not None and len(text) == len(value)}


def target_from(xml, binding):
    node = next((n for n in _nodes(xml) if n.get('resource-id') == RESOURCE_ID), None)
    if node is None:
        return None
    return FocusedPasswordTarget(node.get('package') or '', RESOURCE_ID, binding.target_id, node.get('class') or '',
                                 node.get('focused') == 'true', node.get('password') == 'true',
                                 node.get('enabled') == 'true')


class Device:
    def __init__(self, options, run, failures):
        self.options, self.run, self.failures = options, run, failures

    async def adb(self, *args, input=None, timeout=30):
        try:
            return await asyncio.to_thread(self.run, [self.options.adb, '-s', self.options.serial, *args], input, timeout)
        except (OSError, subprocess.SubprocessError, ValueError):
            self.failures.append('device_command_failed')
            raise CanaryError('device_command_failed') from None

    async def dump(self):
        return await asyncio.to_thread(rpc, self.options.u2_port, 'dumpWindowHierarchy', [True, 50])

    async def field_gone(self):
        try:
            return not any(n.get('package') == PACKAGE for n in _nodes(await self.dump()))
        except CanaryError:
            return False

    async def leave(self):
        """Pause the fixture (it clears its field on pause) and prove its window is gone."""
        if await self.field_gone():
            return True
        for step in (HOME, FORCE_STOP):
            try:
                await self.adb(*step)
            except CanaryError:
                continue
            if await self.field_gone():
                return True
        return False

    async def ocr(self, png):
        try:
            return await asyncio.to_thread(self.run, [self.options.tesseract, 'stdin', 'stdout', '--psm', '11'], png, 60)
        except (OSError, subprocess.SubprocessError, ValueError):
            self.failures.append('ocr_command_failed')
            return 1, b''

    async def measure(self, checkpoint, value, epoch, rows):
        outputs = dict.fromkeys(READERS)
        try:
            outputs['ui_xml'] = await self.dump()
            outputs['agent_compact'] = compact(outputs['ui_xml'])  # what the native agent reads of a screen
        except CanaryError:
            pass
        try:
            code, png = await self.adb(*SCREENCAP)
            if code == 0 and png.startswith(b'\x89PNG'):
                code, text = await self.ocr(png)
                outputs['screenshot_ocr'] = text.decode(errors='replace') if code == 0 else None
            code, text = await self.adb(*(part.format(epoch=epoch) for part in LOGCAT))
            outputs['logcat'] = text.decode(errors='replace') if code == 0 else None
        except CanaryError:
            pass
        for reader, text in outputs.items():
            row = {'checkpoint': checkpoint, 'reader': reader, 'state': 'unavailable'}
            if isinstance(text, str):
                row.update(state='present' if leaked(value, text) else 'absent', bytes=len(text.encode()),
                           sha256=hashlib.sha256(text.encode()).hexdigest())
                if reader == 'ui_xml':
                    row['field'] = field_state(text, value)
            rows.append(row)


class CanaryWindow:
    """Fixture-only ProtectedWindow for the synthetic canary. Not a native/OS read fence."""

    def __init__(self, binding, channel, after_write):
        self.binding, self.channel, self.basket_approved = binding, channel, False
        self.after_write, self.device = after_write, None
        self.held = self.resumed = False

    def hold(self):
        self.held = True

    def allow_resume(self):
        self.resumed = True  # Recorded only: the canary never resumes a native call.

    async def recheck(self):
        return target_from(await self.device.dump(), self.binding)

    async def sensitive_field_gone(self):
        await self.after_write()
        await self.device.adb(*HOME)
        return await self.device.field_gone()


def canary_window(device, channel, after_write, windows):
    @asynccontextmanager
    async def opened(binding):
        if (not isinstance(binding, OwnedToolInputBinding) or binding.package != PACKAGE
                or binding.target_id != TARGET_ID or binding.purpose != InputPurpose.LOGIN):
            raise CanaryError('fixture_only')
        window = CanaryWindow(binding, channel, after_write)
        window.device = device
        windows.append(window)
        yield window
    return opened


def lost_reply_socket(port, relay):
    """The channel's socket reaches the real server through a relay that drops the reply."""
    inner, outer = socket.socketpair()

    def forward():
        with outer:
            try:
                outer.settimeout(5)
                request = _read_http(outer, until_close=False)
                with socket.create_connection(('127.0.0.1', port), timeout=5) as upstream:
                    upstream.sendall(request)
                    relay['forwarded'] += 1
                    result = _decode(_read_http(upstream, until_close=True)).get('result')
                relay['device_result'] = {True: 'accepted', False: 'rejected'}.get(result, 'unknown')
            except (OSError, ValueError, TypeError, AttributeError):
                relay['device_result'] = 'unknown'
    thread = threading.Thread(target=forward, daemon=True)
    thread.start()
    return inner, thread


def _private_file(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(data)


async def scenario(name, number, value, ctx):
    options, device, journal, refs = ctx['options'], ctx['device'], ctx['journal'], ctx['refs']
    run_id, now = options.run_id, time.time()
    binding = OwnedToolInputBinding('canary-root', options.serial, f'canary-{run_id}', f'canary-{run_id}-thread',
                                    f'canary-{run_id}-turn', number, f'canary-{run_id}-call-{name}', PACKAGE,
                                    TARGET_ID, InputPurpose.LOGIN)
    operation = f'canary-{run_id}-{name}'
    item = ProtectedInputRequest(operation + '-request', operation, binding, now + 120, InputTransport.LOCAL_REFERENCE)
    reference = LocalInputReference('ref-' + name, operation, binding, now + 120)
    row = {'scenario': name, 'operation_id': operation, 'native_call_id': binding.native_call_id,
           'native_request_id': number}
    relay, thread = {'forwarded': 0, 'device_result': None}, None
    _private_file(refs / reference.reference_id, value)
    try:
        await device.adb(*START)
        if name == 'lost-reply':
            sock, thread = lost_reply_socket(options.u2_port, relay)
        else:
            sock = await asyncio.to_thread(socket.create_connection, ('127.0.0.1', options.u2_port), 3)
        channel = ExistingInputChannel(options.serial, sock, timeout=ctx['channel_timeout'])
        windows = []

        async def after_write():
            await device.measure('after_write', value, ctx['epoch'], ctx['rows'])
        result = await enter_password_once(journal, item, reference, private_directory=refs,
                                           protected_window=canary_window(device, channel, after_write, windows))
        if thread is not None:
            await asyncio.to_thread(thread.join, 6)
            row['relay'] = {**relay, 'reply_delivered': False}
        row.update(status=result.status, resume_once=result.resume_once,
                   resume_allowed=bool(windows and windows[0].resumed))
        if name == 'lost-reply':
            await device.measure('lost_reply_hold', value, ctx['epoch'], ctx['rows'])
    finally:
        (refs / reference.reference_id).unlink(missing_ok=True)
        row['field_gone'] = await device.leave()
        with sqlite3.connect(journal.path) as db:
            found = db.execute('SELECT status FROM inputs WHERE request_id=?', (item.request_id,)).fetchone()
        row['journal_status'] = found[0] if found else None
        ctx['report']['scenarios'].append(row)
    if name == 'positive' and row.get('status') == 'owner_done':
        await device.measure('after_receipt', value, ctx['epoch'], ctx['rows'])
    return row


def _offline_refusal(options, apk_sha256):
    try:
        _identifier(options.serial)
        _identifier(options.run_id)
        if len(options.run_id) > 48 or not 0 < int(options.u2_port) < 65536:
            raise ValueError
    except (ProtectedInputError, ValueError, TypeError):
        return 'invalid_options'
    state = Path(options.state_dir)
    if state.exists() and (not state.is_dir() or any(state.iterdir())):
        return 'state_dir_reused'
    try:
        if sha256_file(options.apk) != apk_sha256:
            return 'fixture_apk_mismatch'
    except OSError:
        return 'fixture_apk_mismatch'
    if not os.path.isabs(options.tesseract):
        return 'ocr_reader_unavailable'
    return None


async def execute(options, *, run=_run, values=None, apk_sha256=APK_SHA256, channel_timeout=3):
    report = {'mode': 'execute', 'verdict': 'BLOCKED', 'reasons': [], 'fixture': PACKAGE, 'scenarios': [],
              'measurements': [], 'undo': {}, 'unblocks_real_pin': False,
              'fence': 'canary_window_fixture_only_not_a_native_fence'}
    refusal = _offline_refusal(options, apk_sha256)
    if refusal:
        report['reasons'] = [refusal]
        return report
    values = values or (''.join(str(byte % 10) for byte in os.urandom(12)) for _ in range(2))  # synthetic digits
    failures, installed = [], False
    device = Device(options, run, failures)
    state = Path(options.state_dir)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    refs = state / 'refs'
    refs.mkdir(mode=0o700)
    ctx = {'options': options, 'device': device, 'refs': refs, 'rows': report['measurements'], 'report': report,
           'journal': ManualProtectedInput(state / 'journal'), 'channel_timeout': channel_timeout}
    try:
        code, out = await device.adb(*GET_STATE)
        if code != 0 or out.strip() != b'device':
            report['reasons'] = ['device_unavailable']
            return report
        code, out = await device.adb(*PM_PATH)
        if code == 0 and b'package:' in out:
            report['reasons'] = ['fixture_already_installed']  # Root inspects package/certificate; never replaced.
            return report
        code, out = await device.adb(*DATE)
        ctx['epoch'] = int(out.strip())
        await device.dump()
        installed = True
        code, _ = await device.adb(*(part.format(apk=options.apk) for part in INSTALL))
        if code != 0:
            report['reasons'] = ['install_failed']
            return report
        positive = await scenario('positive', 1, next(values), ctx)
        if positive.get('status') == 'owner_done':
            await scenario('lost-reply', 2, next(values), ctx)
    except CanaryError as error:
        if installed:
            failures.append(str(error))
        else:
            report['reasons'] = [str(error)]  # Preflight refusal before any device mutation.
    except (ProtectedInputError, OSError, ValueError, sqlite3.Error):
        failures.append('runner_error')
    finally:
        if installed:
            for key, step in (('force_stop', FORCE_STOP), ('uninstall', UNINSTALL)):
                try:
                    report['undo'][key] = (await device.adb(*step))[0]
                except CanaryError:
                    report['undo'][key] = None
    if report['reasons']:
        return report
    report['reasons'] = _reasons(report, failures)
    report['verdict'] = ('LEAK' if 'reader_leak' in report['reasons'] else
                         'INCONCLUSIVE' if report['reasons'] else 'PASS')
    return report


def _reasons(report, failures):
    reasons = list(dict.fromkeys(failures))
    rows, scenarios = report['measurements'], {s['scenario']: s for s in report['scenarios']}
    if any(row['state'] == 'present' for row in rows):
        reasons.insert(0, 'reader_leak')
    if any(row['state'] == 'unavailable' for row in rows):
        reasons.append('reader_unavailable')
    positive, lost = scenarios.get('positive', {}), scenarios.get('lost-reply')
    if positive.get('status') != 'owner_done' or not positive.get('resume_once'):
        reasons.append('positive_not_confirmed')
    if lost is None:
        reasons.append('lost_reply_not_run')
    elif (lost.get('status'), lost.get('journal_status'), lost['relay']['forwarded']) != ('unknown', 'unknown', 1):
        reasons.append('lost_reply_not_held')
    for checkpoint in ('after_write', 'lost_reply_hold'):
        for row in rows:
            if row['checkpoint'] == checkpoint and row['reader'] == 'ui_xml' and row['state'] != 'unavailable' and (
                    not row['field']['length_matches'] or not row['field']['masked']):
                reasons.append('write_not_observed_masked')
    if not all(s.get('field_gone') for s in report['scenarios']):
        reasons.append('fixture_not_left')
    if report['undo'].get('uninstall') != 0:
        reasons.append('undo_failed')
    return list(dict.fromkeys(reasons))


def plan(options):
    adb = ['adb', '-s', 'SERIAL']
    step = lambda parts: adb + [p.format(apk='APK', epoch='EPOCH') for p in parts]  # noqa: E731
    return {
        'mode': 'plan', 'status': 'PLAN_ONLY_NOT_EXECUTED', 'fixture': {
            'package': PACKAGE, 'activity': ACTIVITY, 'resource_id': RESOURCE_ID, 'apk_sha256': APK_SHA256,
            'apk': 'thread-storage thr_v7cjp524rq/protected-edittext-fixture/protected-edittext-canary.apk'},
        'interface': {
            'binding': 'OwnedToolInputBinding(android_order_control/request_protected_input)',
            'journal': 'ManualProtectedInput: durable unknown before reference read, one-shot, no replay',
            'entry': 'enter_password_once(journal, request, LocalInputReference, protected_window=CanaryWindow)',
            'write': 'ExistingInputChannel.set_password_once: POST /jsonrpc/0 setText',
            'native_route': 'codex_session.queue_control refuses request_protected_input (not exercised)'},
        'invocation': ['cd services/android',
                       'uv run python ../../scripts/android_protected_input_canary.py execute --serial SERIAL '
                       '--u2-port PORT --tesseract /ABSOLUTE/tesseract --apk APK --state-dir NEW_EMPTY_DIR '
                       '--run-id RUN_ID --report NEW_EMPTY_DIR.report.json'],
        'root_pre_steps': [adb + ['forward', 'tcp:PORT',f'tcp:{SERVER_DEVICE_PORT}']],
        'root_post_steps': [adb + ['forward', '--remove', 'tcp:PORT']],
        'device_commands': [step(s) for s in (GET_STATE, PM_PATH, DATE, INSTALL, START, SCREENCAP, LOGCAT, HOME,
                                               START, HOME, FORCE_STOP, UNINSTALL)],
        'undo': [step(FORCE_STOP), step(UNINSTALL)],
        'readers': list(READERS), 'checkpoints': list(CHECKPOINTS),
        'prerequisites': [
            {'id': 'global_native_fence', 'status': 'unavailable',
             'detail': 'no atomic native/descendant/device read barrier (protected-window-enforceability.md)'},
            {'id': 'native_protected_window', 'status': 'unavailable',
             'detail': 'no protected_window implementation for native Codex; canary uses fixture-only CanaryWindow'},
            {'id': 'device_allocation', 'status': 'root_supplied',
             'detail': 'exclusive device lease, no native/agent session or other UiAutomation client during the run'},
            {'id': 'u2_server_forward', 'status': 'root_supplied',
             'detail': 'existing uiautomator2 server running; host PORT forwarded; runner never starts/forwards it'},
            {'id': 'fixture_apk', 'status': 'root_supplied', 'detail': 'pinned sha256; package must be absent'},
            {'id': 'ocr_reader', 'status': 'root_supplied', 'detail': 'absolute Tesseract path on the USB host'}],
        'acceptance': {
            'PASS': ['positive owner_done with resume_once, exactly one setText',
                     'lost-reply: relay forwarded 1, reply dropped, status and journal unknown (held, no retry)',
                     'every reader absent at after_write, after_receipt, lost_reply_hold',
                     'ui_xml field masked with length equal to the synthetic value at write and hold',
                     'fixture window gone after each scenario; uninstall exit 0'],
            'LEAK': 'any reader shows the synthetic digits (report names reader/checkpoint only)',
            'INCONCLUSIVE': 'any other unmet item; BLOCKED: offline/preflight refusal before install'},
        'does_not_prove': ['global native/OS fence', 'Grab/custom keypad/unlock support', 'real PIN delivery',
                           'reference isolation from a same-UID native shell'],
        'unblocks_real_pin': False,
        'options': {k: v for k, v in vars(options).items() if k in ('serial', 'u2_port', 'run_id') and v},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('mode', nargs='?', choices=('plan', 'execute'), default='plan')
    for name in ('serial', 'tesseract', 'apk', 'state-dir', 'run-id', 'report'):
        parser.add_argument('--' + name)
    parser.add_argument('--u2-port', type=int)
    parser.add_argument('--adb', default='adb')
    ns = parser.parse_args(argv)
    if ns.mode == 'plan':
        print(json.dumps(plan(ns), indent=1))
        return 0
    if None in (ns.serial, ns.u2_port, ns.tesseract, ns.apk, ns.state_dir, ns.run_id):
        parser.error('execute needs --serial --u2-port --tesseract --apk --state-dir --run-id')
    options = Options(ns.serial, ns.u2_port, ns.tesseract, ns.adb, ns.apk, ns.state_dir, ns.run_id, ns.report)
    report = asyncio.run(execute(options))
    if options.report:
        fd = os.open(options.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, 'w') as stream:
            json.dump(report, stream, indent=1)
    print(json.dumps(report, indent=1))
    return 0 if report['verdict'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
