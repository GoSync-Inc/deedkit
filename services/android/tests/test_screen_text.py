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


class TreeDevice(Device):
    """A device whose hierarchy dump is the given XML, or raises the given exception."""

    def __init__(self, hierarchy, data=None):
        super().__init__(data)
        self.hierarchy = hierarchy

    def jsonrpc_call(self, method, args, timeout=10):
        self.dumps.append((method, tuple(args)))
        if isinstance(self.hierarchy, Exception):
            raise self.hierarchy
        return self.hierarchy


def tree(*nodes):
    """Flat nodes (class, bounds, attributes) under one full-screen frame."""
    body = ''.join(f'<node class="{name}" bounds="{bounds}" ' + ' '.join(f'{key}="{value}"' for key, value in attributes.items()) + ' />'
                   for name, bounds, attributes in nodes)
    return f'<hierarchy><node class="android.widget.FrameLayout" bounds="[0,0][200,160]">{body}</node></hierarchy>'


def textured(width=200, height=160):
    """A gradient screen (every painted block differs from it) and its PNG bytes."""
    image = Image.linear_gradient('L').resize((width, height)).convert('RGB')
    target = io.BytesIO()
    image.save(target, format='PNG')
    return image, target.getvalue()


def recording_engine(monkeypatch):
    """Replace the engine: it returns no lines and keeps a copy of each image it was given, with its origin."""
    from deedkit_android import screen_text

    seen = []

    def recognize(image, ocr, origin=(0, 0), min_confidence=None):
        seen.append((image.copy(), origin))
        return [], {'engine': 'tesseract', 'preprocess': 'local_contrast'}
    monkeypatch.setattr(screen_text, 'recognize_with', recognize)
    return seen


OCR = {'command': '/synthetic/tesseract', 'languages': 'eng'}
IMAGE, TEXT = 'android.widget.ImageView', 'android.widget.TextView'


def changed_within(original, image, box):
    """The pixels that differ lie inside box (and some do)."""
    from PIL import ImageChops

    difference = ImageChops.difference(original, image).getbbox()
    return difference is not None and difference[0] >= box[0] and difference[1] >= box[1] and difference[2] <= box[2] and difference[3] <= box[3]


# spec: ANYM-980
def test_mask_images_paints_image_and_texture_bounds_in_the_ocr_copy_only(monkeypatch):
    from deedkit_android.ui_worker import execute

    seen = recording_engine(monkeypatch)
    original, data = textured()
    hierarchy = tree((IMAGE, '[20,20][120,100]', {}), ('androidx.appcompat.widget.AppCompatImageView', '[130,10][150,30]', {}),
                     ('android.view.TextureView', '[130,60][190,100]', {}), (TEXT, '[130,120][190,140]', {'text': 'Sepatu'}))
    device = TreeDevice(hierarchy, data)
    result = execute(device, {'readScreenText': {}}, ocr=OCR)
    image, origin = seen[0]
    assert origin == (0, 0) and image.size == original.size
    for box in ((20, 20, 120, 100), (130, 10, 150, 30), (130, 60, 190, 100)):
        assert image.crop(box).getcolors(1) is not None, box  # one solid colour
    assert changed_within(original, image, (20, 10, 190, 100))
    assert image.crop((130, 120, 190, 140)).tobytes() == original.crop((130, 120, 190, 140)).tobytes()  # text node untouched
    assert result['ocr']['masked'] == {'regions': 3, 'skipped_large': 0, 'tree': 'ok'}
    assert device.dumps == [('dumpWindowHierarchy', (True, 50))] and device.data == data


# spec: ANYM-980
def test_mask_images_paints_in_the_coordinates_of_a_region_read(monkeypatch):
    from deedkit_android.ui_worker import execute

    seen = recording_engine(monkeypatch)
    original, data = textured()
    hierarchy = tree((IMAGE, '[20,20][120,100]', {}), (IMAGE, '[160,120][190,150]', {}))
    result = execute(TreeDevice(hierarchy, data), {'readScreenText': {'region': {'x': 50, 'y': 10, 'w': 100, 'h': 100}}}, ocr=OCR)
    image, origin = seen[0]
    assert origin == (50, 10) and image.size == (100, 100)
    assert image.crop((0, 10, 70, 90)).getcolors(1) is not None  # the image's part inside the crop
    assert changed_within(original.crop((50, 10, 150, 110)), image, (0, 10, 70, 90))
    assert result['ocr']['masked'] == {'regions': 1, 'skipped_large': 0, 'tree': 'ok'}  # the second image is outside the crop


# spec: ANYM-980
def test_mask_images_leaves_text_the_app_draws_over_an_image(monkeypatch):
    from deedkit_android.ui_worker import execute

    seen = recording_engine(monkeypatch)
    original, data = textured()
    hierarchy = tree((IMAGE, '[20,20][160,120]', {}), (TEXT, '[30,30][90,50]', {'text': 'Diskon 20%'}),
                     ('android.widget.FrameLayout', '[20,20][160,120]', {'content-desc': 'Sepatu', 'clickable': 'true'}),
                     (TEXT, '[100,60][150,110]', {'text': 'secret', 'password': 'true'}))
    result = execute(TreeDevice(hierarchy, data), {'readScreenText': {}}, ocr=OCR)
    image = seen[0][0]
    assert image.crop((30, 30, 90, 50)).tobytes() == original.crop((30, 30, 90, 50)).tobytes()  # the badge stays readable
    assert image.crop((100, 60, 150, 110)).getcolors(1) is not None  # a password node never unpaints, a container does not either
    assert image.crop((20, 60, 90, 120)).getcolors(1) is not None
    assert result['ocr']['masked']['regions'] == 1


# spec: ANYM-980
def test_mask_images_skips_an_image_that_covers_most_of_the_read_region(monkeypatch):
    from deedkit_android.ui_worker import execute

    seen = recording_engine(monkeypatch)
    original, data = textured()
    viewer = TreeDevice(tree((IMAGE, '[0,0][200,150]', {})), data)  # 93.75% of the screen: a full-screen image viewer
    result = execute(viewer, {'readScreenText': {}}, ocr=OCR)
    assert seen[0][0].tobytes() == original.tobytes()
    assert result['ocr']['masked'] == {'regions': 0, 'skipped_large': 1, 'tree': 'ok'}
    # `within` an image: the region is the image itself, so it is read, not painted.
    selected = TreeDevice(tree((IMAGE, '[10,20][60,50]', {})), data)
    result = execute(selected, {'readScreenText': {'within': {'id': 'photo'}}}, ocr=OCR)
    assert seen[1][0].tobytes() == original.crop((10, 20, 60, 50)).tobytes()
    assert result['ocr']['masked'] == {'regions': 0, 'skipped_large': 1, 'tree': 'ok'}


# spec: ANYM-980
def test_mask_images_false_reads_the_unpainted_capture_without_a_dump(monkeypatch):
    from deedkit_android.ui_worker import execute

    seen = recording_engine(monkeypatch)
    original, data = textured()
    device = TreeDevice(tree((IMAGE, '[20,20][120,100]', {})), data)
    result = execute(device, {'readScreenText': {'mask_images': False}}, ocr=OCR)
    assert seen[0][0].tobytes() == original.tobytes() and device.dumps == []
    assert result['ocr']['masked'] is None


# spec: ANYM-980
def test_mask_images_shares_one_dump_with_the_structure_option(monkeypatch):
    from deedkit_android.ui_worker import execute

    recording_engine(monkeypatch)
    _, data = textured()
    device = TreeDevice(tree((IMAGE, '[20,20][120,100]', {})), data)
    result = execute(device, {'readScreenText': {'structure': True}}, ocr=OCR)
    assert device.dumps == [('dumpWindowHierarchy', (True, 50))]
    assert result['structure']['tree'] == 'merged' and result['ocr']['masked']['regions'] == 1
    device = TreeDevice(tree((IMAGE, '[20,20][120,100]', {})), data)
    assert 'structure' not in execute(device, {'readScreenText': {'structure': False}}, ocr=OCR) and len(device.dumps) == 1


# spec: ANYM-980
def test_an_unreadable_hierarchy_never_fails_the_read(monkeypatch):
    from deedkit_android.ui_worker import execute

    seen = recording_engine(monkeypatch)
    original, data = textured()
    for broken in (RuntimeError('secure screen'), '<hierarchy><node'):
        result = execute(TreeDevice(broken, data), {'readScreenText': {}}, ocr=OCR)
        assert seen[-1][0].tobytes() == original.tobytes()
        assert result['lines'] == [] and result['ocr']['masked'] == {'regions': 0, 'skipped_large': 0, 'tree': 'unavailable'}
    result = execute(TreeDevice(RuntimeError('secure screen'), data), {'readScreenText': {'structure': True}}, ocr=OCR)
    assert result['structure']['tree'] == 'unavailable'


# spec: ANYM-980
def test_mask_images_option_is_validated_before_admission():
    from deedkit_android.uiautomator import validate_commands

    for value in ('"yes"', '1', '[true]', 'null'):
        with pytest.raises(Refused) as failure:
            validate_commands(compile_base('- readScreenText: {mask_images: %s}' % value, {}, fields=UI_FIELDS))
        assert failure.value.code == 'invalid_screen_text_options'
    validate_commands(compile_base('- readScreenText: {mask_images: false}\n- readScreenText: {mask_images: true, structure: true}',
                                   {}, fields=UI_FIELDS))


def crowded(extra):
    """A flat tree of exactly 5000 + extra nodes: an image first, then text nodes, an image last."""
    nodes = [(IMAGE, '[20,20][120,100]', {})] + [(TEXT, '[0,0][0,0]', {})] * (4997 + extra) + [(IMAGE, '[130,20][190,60]', {})]
    return tree(*nodes)


# spec: ANYM-981
def test_hierarchy_option_sets_the_dump_and_a_tree_past_5000_nodes_is_cut(monkeypatch):
    from deedkit_android.ui_worker import execute

    recording_engine(monkeypatch)
    _, data = textured()
    for options, dump in (({}, (True, 50)), ({'hierarchy': {'compressed': False, 'max_depth': 120}}, (False, 120)),
                          ({'hierarchy': {'max_depth': 80}}, (True, 80)), ({'hierarchy': {'compressed': False}}, (False, 50))):
        device = TreeDevice(tree((IMAGE, '[20,20][120,100]', {})), data)
        execute(device, {'readScreenText': {'structure': True, **options}}, ocr=OCR)
        assert device.dumps == [('dumpWindowHierarchy', dump)], options
    whole = execute(TreeDevice(crowded(0), data), {'readScreenText': {'structure': True}}, ocr=OCR)  # 5000 nodes: all read
    assert whole['ocr']['masked'] == {'regions': 2, 'skipped_large': 0, 'tree': 'ok'} and whole['structure']['tree'] == 'merged'
    cut = execute(TreeDevice(crowded(1), data), {'readScreenText': {'structure': True}}, ocr=OCR)  # the 5001st node is dropped
    assert cut['ocr']['masked'] == {'regions': 1, 'skipped_large': 0, 'tree': 'truncated'} and cut['structure']['tree'] == 'truncated'


# spec: ANYM-981
def test_hierarchy_and_card_options_are_validated_before_admission():
    from deedkit_android.uiautomator import validate_commands

    for value in ('{hierarchy: 5}', '{hierarchy: {depth: 3}}', '{hierarchy: {max_depth: 0}}', '{hierarchy: {max_depth: 201}}',
                  '{hierarchy: {max_depth: "5"}}', '{hierarchy: {compressed: "no"}}', '{per_card: 1}', '{per_card: "yes"}',
                  '{max_cards: 0}', '{max_cards: 51}', '{max_cards: "3"}', '{max_cards: true}'):
        with pytest.raises(Refused) as failure:
            validate_commands(compile_base('- readScreenText: %s' % value, {}, fields=UI_FIELDS))
        assert failure.value.code == 'invalid_screen_text_options', value
    validate_commands(compile_base('- readScreenText: {hierarchy: {compressed: false, max_depth: 200}, per_card: true, max_cards: 50}\n'
                                   '- readScreenText: {hierarchy: {}, per_card: false, max_cards: 1}', {}, fields=UI_FIELDS))


PHOTO_CARD = '<node class="android.view.ViewGroup" clickable="true" bounds="[0,0][400,140]"><node class="android.widget.ImageView" bounds="[0,0][400,100]"/></node>'
TEXT_CARD = ('<node class="android.view.ViewGroup" clickable="true" bounds="[0,150][400,290]">'
             '<node class="android.widget.TextView" text="Sepatu Lari" bounds="[10,160][200,190]"/>'
             '<node class="android.widget.TextView" text="Rp 125.000" bounds="[10,200][200,230]"/></node>')


def cards_tree(*cards):
    return ('<hierarchy><node class="android.widget.FrameLayout" bounds="[0,0][400,300]">'
            '<node class="androidx.recyclerview.widget.RecyclerView" scrollable="true" bounds="[0,0][400,300]">' + ''.join(cards)
            + '</node></node></hierarchy>')


def card_engine(monkeypatch, fail=()):
    """An engine that answers every call with one line named after the call's origin; records (size, origin, min_confidence, pixels)."""
    from deedkit_android import screen_text

    calls = []

    def recognize(image, ocr, origin=(0, 0), min_confidence=None):
        calls.append((image.size, origin, min_confidence, image.copy()))
        if origin in fail:
            raise Refused('ocr_failed')
        left, top = origin
        return ([{'text': f'read@{left},{top}', 'bounds': f'[{left},{top}][{left + 50},{top + 10}]', 'confidence': 91.0,
                  'box': (left, top, left + 50, top + 10), 'words': []}],
                {'engine': 'tesseract', 'preprocess': 'local_contrast'})
    monkeypatch.setattr(screen_text, 'recognize_with', recognize)
    return calls


# spec: ANYM-981
def test_per_card_reads_a_card_without_tree_text_from_its_own_crop_and_takes_the_rest_from_the_tree(monkeypatch):
    from deedkit_android.ui_worker import execute

    calls = card_engine(monkeypatch)
    _, data = textured(400, 300)
    result = execute(TreeDevice(cards_tree(PHOTO_CARD, TEXT_CARD), data), {'readScreenText': {'per_card': True, 'min_confidence': 70}}, ocr=OCR)
    assert [(size, origin, minimum) for size, origin, minimum, _ in calls] == [((400, 300), (0, 0), 70), ((400, 140), (0, 0), 70)]
    assert [line['text'] for line in result['lines']] == ['read@0,0']  # the whole-screen read is what it was without per_card
    first, second = result['cards']
    assert (first['list'], first['bounds'], first['source']) == (0, '[0,0][400,140]', 'ocr') and first['node']['class'] == 'android.view.ViewGroup'
    assert first['lines'] == [{'text': 'read@0,0', 'bounds': '[0,0][50,10]', 'confidence': 91.0, 'source': 'ocr'}]
    assert (second['bounds'], second['source']) == ('[0,150][400,290]', 'tree') and 'node' in second
    assert second['lines'] == [{'text': 'Sepatu Lari', 'bounds': '[10,160][200,190]', 'confidence': 100.0, 'source': 'tree'},
                               {'text': 'Rp 125.000', 'bounds': '[10,200][200,230]', 'confidence': 100.0, 'source': 'tree'}]
    assert result['cards_truncated'] is False and result['ocr']['cards'] == {'tree': 'ok', 'from_tree': 1, 'from_ocr': 1}
    assert calls[1][3].crop((0, 0, 400, 100)).getcolors(1) is not None  # the crop is cut from the copy with the photo painted out


# spec: ANYM-981
def test_per_card_bounds_the_cards_read_and_keeps_the_result_shape_without_it(monkeypatch):
    from deedkit_android.ui_worker import execute

    calls = card_engine(monkeypatch)
    _, data = textured(400, 300)
    three = cards_tree(*(f'<node class="android.view.ViewGroup" bounds="[0,{100 * i}][400,{100 * i + 90}]"/>' for i in range(3)))
    result = execute(TreeDevice(three, data), {'readScreenText': {'per_card': True, 'max_cards': 2}}, ocr=OCR)
    assert [card['bounds'] for card in result['cards']] == ['[0,0][400,90]', '[0,100][400,190]'] and result['cards_truncated'] is True
    assert result['ocr']['cards'] == {'tree': 'ok', 'from_tree': 0, 'from_ocr': 2} and len(calls) == 3
    plain = execute(TreeDevice(three, data), {'readScreenText': {}}, ocr=OCR)
    assert 'cards' not in plain and 'cards_truncated' not in plain and 'cards' not in plain['ocr']
    assert len(execute(TreeDevice(three, data), {'readScreenText': {'per_card': True}}, ocr=OCR)['cards']) == 3  # default 12


# spec: ANYM-981
def test_one_failed_card_crop_does_not_fail_the_others_but_all_failing_does(monkeypatch):
    from deedkit_android.ui_worker import execute

    _, data = textured(400, 300)
    three = cards_tree(*(f'<node class="android.view.ViewGroup" bounds="[0,{10 + 100 * i}][400,{100 + 100 * i}]"/>' for i in range(3)))
    card_engine(monkeypatch, fail={(0, 110)})
    result = execute(TreeDevice(three, data), {'readScreenText': {'per_card': True}}, ocr=OCR)
    assert [(card['source'], card.get('error'), len(card['lines'])) for card in result['cards']] == [
        ('ocr', None, 1), ('ocr', 'ocr_failed', 0), ('ocr', None, 1)] and len(result['lines']) == 1
    card_engine(monkeypatch, fail={(0, 10), (0, 110), (0, 210)})
    with pytest.raises(Refused) as failure:  # every card read failed: a definite failure, as a failed whole-screen read is
        execute(TreeDevice(three, data), {'readScreenText': {'per_card': True}}, ocr=OCR)
    assert failure.value.code == 'ocr_failed'


# spec: ANYM-981
def test_per_card_without_a_hierarchy_or_a_list_reads_the_whole_screen_with_no_cards(monkeypatch):
    from deedkit_android.ui_worker import execute

    calls = card_engine(monkeypatch)
    _, data = textured(400, 300)
    result = execute(TreeDevice(RuntimeError('secure screen'), data), {'readScreenText': {'per_card': True}}, ocr=OCR)
    assert result['cards'] == [] and result['cards_truncated'] is False and len(result['lines']) == 1 and len(calls) == 1
    assert result['ocr']['cards'] == {'tree': 'unavailable', 'from_tree': 0, 'from_ocr': 0}
    no_list = tree((TEXT, '[10,10][100,40]', {'text': 'Sepatu Lari'}))
    result = execute(TreeDevice(no_list, data), {'readScreenText': {'per_card': True}}, ocr=OCR)
    assert result['cards'] == [] and result['ocr']['cards'] == {'tree': 'ok', 'from_tree': 0, 'from_ocr': 0} and len(calls) == 2


# spec: ANYM-981
def test_per_card_reads_cards_clipped_to_a_region_in_full_screen_coordinates(monkeypatch):
    from deedkit_android.ui_worker import execute

    calls = card_engine(monkeypatch)
    _, data = textured(400, 300)
    three = cards_tree(*(f'<node class="android.view.ViewGroup" bounds="[0,{100 * i}][400,{100 * i + 90}]"/>' for i in range(3)))
    result = execute(TreeDevice(three, data), {'readScreenText': {'per_card': True, 'region': {'x': 100, 'y': 50, 'w': 200, 'h': 150}}}, ocr=OCR)
    assert [card['bounds'] for card in result['cards']] == ['[100,50][300,90]', '[100,100][300,190]']  # the third card starts at 200: outside
    assert [(size, origin) for size, origin, _, _ in calls] == [((200, 150), (100, 50)), ((200, 40), (100, 50)), ((200, 90), (100, 100))]


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


def inked_png():
    """Two black word shapes and a faint third line on white, for a fake Vision that pads its boxes."""
    from PIL import ImageDraw

    image = Image.new('RGB', (300, 160), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 40, 79, 59), fill='black')
    draw.rectangle((100, 40, 159, 59), fill='black')
    draw.rectangle((20, 110, 79, 129), fill='black')
    target = io.BytesIO()
    image.save(target, format='PNG')
    return target.getvalue()


def fake_vision(monkeypatch, error=None, delay=0):
    from deedkit_android import screen_vision

    calls = []

    def observe(image, codes):
        calls.append((image.size, codes))
        if delay:
            import time
            time.sleep(delay)
        if error:
            raise error
        return [('Lacak Rp68.310', 50.0, [('Lacak', (12, 30, 88, 70)), ('Rp68.310', (92, 30, 168, 70))]),
                ('PLUS', 30.0, [('PLUS', (12, 100, 88, 140))])]
    monkeypatch.setattr(screen_vision, 'observations', observe)
    return calls


# spec: ANYM-966
def test_vision_engine_reads_lines_with_ink_tight_boxes_and_its_own_confidence_default(tmp_path, monkeypatch):
    from deedkit_android.ui_worker import execute

    calls = fake_vision(monkeypatch)
    ocr, record = fake_tesseract(tmp_path)
    result = execute(Device(inked_png()), {'readScreenText': {'region': {'x': 0, 'y': 0, 'w': 300, 'h': 160}, 'structure': True}},
                     ocr={**ocr, 'engine': 'vision'})
    assert calls == [((300, 160), 'eng+ind')] and not record.exists()
    assert result['ocr']['engine'] == 'apple-vision' and result['ocr']['bounds'] == 'ink'
    assert [line['text'] for line in result['lines']] == ['Lacak Rp68.310']  # 30 is under Vision's default of 50
    left, top, right, bottom = (int(v) for v in result['lines'][0]['bounds'].replace('][', ',').strip('[]').split(','))
    assert abs(left - 20) <= 2 and abs(top - 40) <= 2 and abs(right - 160) <= 2 and abs(bottom - 60) <= 2
    words = result['structure']['blocks'][0]['lines'][0]['words']
    assert [word['text'] for word in words] == ['Lacak', 'Rp68.310'] and words[1]['bounds'].startswith('[10')
    every = execute(Device(inked_png()), {'readScreenText': {'min_confidence': 0}}, ocr={**ocr, 'engine': 'vision'})
    assert [line['text'] for line in every['lines']] == ['Lacak Rp68.310', 'PLUS']


# spec: ANYM-966
def test_vision_failure_or_timeout_falls_back_to_tesseract(tmp_path, monkeypatch):
    from deedkit_android import screen_vision
    from deedkit_android.ui_worker import execute

    ocr, record = fake_tesseract(tmp_path)
    fake_vision(monkeypatch, error=RuntimeError('framework'))
    result = execute(Device(), {'readScreenText': {}}, ocr={**ocr, 'engine': 'vision'})
    assert result['ocr']['engine'] == 'tesseract' and result['ocr']['preprocess'] == 'local_contrast'
    assert [line['text'] for line in result['lines']] == ['Selesai', 'Rp 125.000'] and record.exists()
    monkeypatch.setattr(screen_vision, 'TIMEOUT_SECONDS', 0.05)
    fake_vision(monkeypatch, delay=1)
    assert execute(Device(), {'readScreenText': {}}, ocr={**ocr, 'engine': 'vision'})['ocr']['engine'] == 'tesseract'
    fake_vision(monkeypatch, error=RuntimeError('framework'))
    with pytest.raises(Refused) as failure:
        execute(Device(), {'readScreenText': {}}, ocr={'command': None, 'languages': 'eng', 'engine': 'vision'})
    assert failure.value.code == 'ocr_failed'


# spec: ANYM-966
def test_host_engine_is_vision_only_where_the_extra_is_installed_and_languages_map(monkeypatch):
    from deedkit_android import screen_vision, uiautomator

    for installed, engine in ((True, 'vision'), (False, 'tesseract')):
        monkeypatch.setattr(screen_vision, 'available', lambda: installed)
        uiautomator.ocr_engine.cache_clear()
        assert uiautomator.ocr_engine() == engine
    uiautomator.ocr_engine.cache_clear()
    monkeypatch.setattr(screen_vision.sys, 'platform', 'linux')
    assert screen_vision.available() is False
    assert screen_vision.languages('eng+ind+xyz') == ['en-US', 'id-ID'] and screen_vision.languages('xyz') == ['en-US']


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
    from deedkit_android.uiautomator import Uiautomator, ocr_engine

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
    ocr = json.loads(written[0])['ocr']
    assert {key: ocr[key] for key in ('command', 'languages')} == {'command': '/opt/ocr/tesseract', 'languages': 'eng+ind'}
    assert ocr.get('engine', 'tesseract') == ocr_engine()  # vision on a macOS host with the vision extra


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
