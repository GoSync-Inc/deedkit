"""Local text recognition with a synthetic screen and a fake Tesseract executable."""

import asyncio
import io
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import uuid

from PIL import Image
import pytest

from deedkit_android.config import Refused
from deedkit_android.flows import compile_flow as compile_base
from deedkit_android.uiautomator import UI_FIELDS

TSV = ('level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
       '5\t1\t1\t1\t1\t1\t4\t30\t20\t10\t91.5\tRp\n'
       '5\t1\t1\t1\t1\t2\t28\t30\t30\t10\t88.5\t125.000\n'
       '5\t1\t2\t1\t1\t1\t2\t4\t40\t12\t95\tSelesai\n'
       '5\t1\t3\t1\t1\t1\t2\t20\t10\t5\t20\tnoise\n'
       '5\t1\t4\t1\t1\t1\t0\t0\t0\t0\t-1\t \n')


def png(width=100, height=80):
    target = io.BytesIO()
    Image.new('RGB', (width, height), 'white').save(target, format='PNG')
    return target.getvalue()


def fake_tesseract(tmp_path, exit_code=0):
    record = tmp_path / 'tesseract-call.json'
    script = tmp_path / 'tesseract'
    script.write_text(
        f'#!{sys.executable}\nimport json, os, sys\n'
        'from PIL import Image\n'
        f'size = Image.open(sys.argv[1]).size\n'
        f'json.dump({{"argv": sys.argv[1:], "size": size, "existed": os.path.exists(sys.argv[1])}}, open({str(record)!r}, "w"))\n'
        f'sys.stdout.write({TSV!r})\nsys.exit({exit_code})\n')
    script.chmod(0o700)
    return {'command': str(script), 'languages': 'eng+ind'}, record


class Device:
    def __init__(self, data=None):
        self.data, self.shells, self.dumps = data or png(), [], []
        self._dev = self

    def shell2(self, args, timeout=None, encoding='utf-8'):
        self.shells.append((args, encoding))
        return SimpleNamespace(returncode=0, output=self.data)

    def jsonrpc_call(self, method, args, timeout=10):
        self.dumps.append((method, tuple(args)))
        return '<hierarchy><node text="Selesai" bounds="[0,0][10,10]" /></hierarchy>'

    def __call__(self, **selector):
        return SimpleNamespace(selector=selector, info={'bounds': {'left': 10, 'top': 20, 'right': 60, 'bottom': 50}})


# spec: ANYM-955
def test_screen_text_returns_ordered_lines_in_full_screen_coordinates(tmp_path):
    from deedkit_android.ui_worker import execute

    ocr, record = fake_tesseract(tmp_path)
    device = Device()
    result = execute(device, {'readScreenText': {'region': {'x': 10, 'y': 20, 'w': 50, 'h': 40}, 'key': 'orders'}}, ocr=ocr)
    assert device.shells == [(['screencap', '-p'], None)]
    call = json.loads(record.read_text())
    assert call['existed'] and call['size'] == [50, 40]
    assert call['argv'][1:] == ['stdout', '-l', 'eng+ind', '--psm', '11', 'tsv']
    assert not Path(call['argv'][0]).parent.exists()
    assert result['key'] == 'orders' and result['region'] == {'x': 10, 'y': 20, 'w': 50, 'h': 40}
    assert result['lines'] == [
        {'text': 'Selesai', 'bounds': '[12,24][52,36]', 'confidence': 95.0},
        {'text': 'Rp 125.000', 'bounds': '[14,50][68,60]', 'confidence': 90.0},
    ]
    assert result['truncated'] is False and result['ocr']['languages'] == 'eng+ind'


# spec: ANYM-955
def test_screen_text_limits_confidence_count_and_element_region(tmp_path):
    from deedkit_android.ui_worker import execute

    ocr, record = fake_tesseract(tmp_path)
    device = Device()
    result = execute(device, {'readScreenText': {'within': {'id': 'list'}, 'min_confidence': 0, 'limit': 2}}, ocr=ocr)
    assert json.loads(record.read_text())['size'] == [50, 30]
    assert result['region'] == {'x': 10, 'y': 20, 'w': 50, 'h': 30}
    assert [line['text'] for line in result['lines']] == ['Selesai', 'noise'] and result['truncated'] is True


# spec: ANYM-955
@pytest.mark.skipif(shutil.which('tesseract') is None, reason='Tesseract is not installed on this runner')
def test_screen_text_parses_real_tesseract_output():
    from PIL import ImageDraw, ImageFont
    from deedkit_android.ui_worker import execute

    image = Image.new('RGB', (1080, 400), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=40)
    draw.text((40, 60), 'Pesanan Selesai', fill='black', font=font)
    draw.text((700, 60), 'Rp 125.000', fill='black', font=font)
    target = io.BytesIO()
    image.save(target, format='PNG')
    result = execute(Device(target.getvalue()), {'readScreenText': {}},
                     ocr={'command': shutil.which('tesseract'), 'languages': 'eng'})
    texts = [line['text'] for line in result['lines']]
    assert texts == ['Pesanan Selesai', 'Rp 125.000']
    assert result['lines'][1]['bounds'].startswith('[70')


# spec: ANYM-955
def test_screen_text_without_configuration_fails_before_capture(tmp_path):
    from deedkit_android.ui_worker import execute

    device = Device()
    with pytest.raises(Refused) as failure:
        execute(device, {'readScreenText': {}}, ocr=None)
    assert failure.value.code == 'ocr_unavailable' and 'ocr_command' in failure.value.hint
    assert device.shells == []


# spec: ANYM-955
def test_screen_text_engine_failure_is_definite_and_removes_the_image(tmp_path):
    from deedkit_android.ui_worker import execute

    ocr, record = fake_tesseract(tmp_path, exit_code=1)
    with pytest.raises(Refused) as failure:
        execute(Device(), {'readScreenText': {}}, ocr=ocr)
    assert failure.value.code == 'ocr_failed'
    assert not Path(json.loads(record.read_text())['argv'][0]).parent.exists()
    with pytest.raises(Refused) as failure:
        execute(Device(b'not a png'), {'readScreenText': {}}, ocr=ocr)
    assert failure.value.code == 'screenshot_unavailable'
    with pytest.raises(Refused) as failure:
        execute(Device(), {'readScreenText': {'region': {'x': 90, 'y': 0, 'w': 50, 'h': 10}}}, ocr=ocr)
    assert failure.value.code == 'region_outside_screen'


# spec: ANYM-955
def test_screen_text_options_are_validated_before_admission():
    from deedkit_android.uiautomator import validate_commands

    for source in ('- readScreenText: {region: {x: 0, y: 0, w: 0, h: 5}}',
                   '- readScreenText: {region: {x: 0, y: 0, w: 5, h: 5}, within: {id: list}}',
                   '- readScreenText: {limit: 301}', '- readScreenText: {min_confidence: 101}',
                   '- readScreenText: {key: "bad key"}', '- readScreenText: {within: {image: a.png}}'):
        with pytest.raises(Refused):
            validate_commands(compile_base(source, {}, fields=UI_FIELDS))
    validate_commands(compile_base('- readScreenText: {}\n- readScreenText: {within: {id: list}, key: orders, limit: 5}', {}, fields=UI_FIELDS))


# spec: ANYM-955
def test_screen_text_failure_keeps_the_worker_and_fails_the_receipt(tmp_path, monkeypatch):
    from deedkit_android.device import Devices
    from deedkit_android.store import Store

    async def scenario():
        store = Store(tmp_path / 'state')
        config = SimpleNamespace(devices={'phone': 'SERIAL'}, maestro=None, adb='/synthetic', uiautomator2_jar='/synthetic',
                                 state_dir=str(tmp_path / 'state'), ocr_command=None, ocr_languages='eng')
        devices = Devices(config, store)
        closed = []
        async def close(device=None):
            closed.append(device)
        async def capture(*args):
            return {}
        monkeypatch.setattr(devices.ui, 'close', close)
        monkeypatch.setattr(devices, 'capture_image', capture)
        monkeypatch.setattr(devices, 'capture_hierarchy', capture)
        lines = [{'text': 'Selesai', 'bounds': '[0,0][1,1]', 'confidence': 90.0}]
        async def answer(command, **options):
            if command in ('back', '__guard__'):
                return {'success': True}
            if command == {'readScreenText': {'key': 'orders'}}:
                return {'key': 'orders', 'lines': lines}
            error = Refused('ocr_unavailable')
            error.hint = 'configure ocr_command'
            raise error
        monkeypatch.setattr(devices.ui, 'call', lambda device, command, **options: answer(command, **options))
        session = store.reserve('owner', 'phone', 'acquire')['session_id']
        flow = compile_base('- back\n- readScreenText: {key: orders}', {}, fields=UI_FIELDS)
        try:
            done = await devices.execute('owner', 'phone', session, str(uuid.uuid4()), None, 10, 10, flow)
            assert done['state'] == 'succeeded' and done['data'] == {'orders': lines}
            failed = await devices.execute('owner', 'phone', session, str(uuid.uuid4()), None, 10, 10,
                                           compile_base('- readScreenText: {}', {}, fields=UI_FIELDS))
            assert failed['state'] == 'failed' and failed['error'] == 'ocr_unavailable'
            assert failed['next_action'] == 'inspect_failure_observation_and_correct_flow'
            assert failed['hint'] == 'configure ocr_command' and closed == []
        finally:
            await devices.close()
            store.close()
    asyncio.run(scenario())


# spec: ANYM-955
def test_uiautomator_keeps_its_worker_after_a_definite_ocr_failure(tmp_path):
    from deedkit_android.uiautomator import Uiautomator

    config = SimpleNamespace(devices={'phone': 'SERIAL'}, uiautomator2_jar='/synthetic', adb='/synthetic/adb',
                             state_dir=str(tmp_path), ocr_command='/opt/ocr/tesseract', ocr_languages='eng+ind')
    ui = Uiautomator(config, 0)
    written = []

    class Process:
        returncode = None
        stdin = SimpleNamespace(write=written.append, drain=lambda: asyncio.sleep(0))
        stdout = SimpleNamespace(readline=lambda: asyncio.sleep(0, b'{"error": "ocr_failed", "hint": "check the host"}\n'))
    ui.processes['phone'] = Process()

    async def scenario():
        with pytest.raises(Refused, match='ocr_failed') as failure:
            await ui.call('phone', {'readScreenText': {}}, element_timeout_ms=5000)
        assert failure.value.hint == 'check the host'
    asyncio.run(scenario())
    assert 'phone' in ui.processes
    assert json.loads(written[0])['ocr'] == {'command': '/opt/ocr/tesseract', 'languages': 'eng+ind'}


# spec: ANYM-955
def test_map_hierarchy_option_controls_every_map_snapshot():
    from deedkit_android.app_maps import validate_map
    from deedkit_android.map_runtime import execute_map

    source = 'package: example.shop\nselectors:\n  done: [{textExact: Selesai}]\nscreens:\n  orders:\n    require: [done]\n'
    full = validate_map(source + 'hierarchy: {compressed: false, max_depth: 120}\n')
    device = Device()
    execute_map(device, 'waitScreen', {'app_map': full, 'screen': 'orders'}, None)
    execute_map(device, 'inspectMap', {'app_map': full}, None)
    assert device.dumps == [('dumpWindowHierarchy', (False, 120))] * 2
    device = Device()
    execute_map(device, 'waitScreen', {'app_map': validate_map(source), 'screen': 'orders'}, None)
    assert device.dumps == [('dumpWindowHierarchy', (True, 50))]
    for bad in ('hierarchy: {compressed: no_value}\n', 'hierarchy: {max_depth: 0}\n', 'hierarchy: {depth: 3}\n'):
        with pytest.raises(Refused):
            validate_map(source + bad)
