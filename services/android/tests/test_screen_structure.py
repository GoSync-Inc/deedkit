"""OCR layout structure and tree merge with a synthetic screen, fake Tesseract TSV and hierarchy."""

import io
import json
import shutil
import sys
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFont
import pytest

from deedkit_android.config import Refused

HEADER = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
CARDS = [('Pesanan Selesai', f'Rp 12{i}.000', f'Toko Contoh {i}', '11 Nov 2026') for i in range(3)]


def card_rows(search=True):
    rows, block = [], 0
    def line(words, left, top, conf=90):
        nonlocal block
        block += 1
        x = left
        for number, word in enumerate(words.split(), 1):
            width = 12 * len(word)
            rows.append(f'5\t1\t{block}\t1\t1\t{number}\t{x}\t{top}\t{width}\t40\t{conf}\t{word}')
            x += width + 12
    if search:
        line('Cari transaksi', 40, 0)
    for i, (label, price, store, date) in enumerate(CARDS):
        top = 200 + i * 300
        line(label, 40, top)
        line(price, 700, top + 2)
        line(store, 40, top + 80)
        line(date, 40, top + 150, 80)
    return HEADER + '\n'.join(rows) + '\n'


TREE = ('<hierarchy><node class="android.widget.FrameLayout" bounds="[0,0][1080,1200]">'
        '<node class="android.widget.EditText" resource-id="shop:id/search" text="Cari transaksi" bounds="[30,0][400,45]" />'
        + ''.join(f'<node class="android.view.ViewGroup" clickable="true" bounds="[20,{180 + i * 300}][1060,{400 + i * 300}]" />'
                  for i in range(3)) + '</node></hierarchy>')


def png(width=1080, height=1200):
    target = io.BytesIO()
    Image.new('RGB', (width, height), 'white').save(target, format='PNG')
    return target.getvalue()


def fake_tesseract(tmp_path, tsv):
    script = tmp_path / 'tesseract'
    script.write_text(f'#!{sys.executable}\nimport sys\nsys.stdout.write({tsv!r})\n')
    script.chmod(0o700)
    return {'command': str(script), 'languages': 'eng+ind'}


class Device:
    def __init__(self, tree=TREE, data=None):
        self.tree, self.data, self.dumps = tree, data or png(), []
        self._dev = self

    def shell2(self, args, timeout=None, encoding='utf-8'):
        return SimpleNamespace(returncode=0, output=self.data)

    def jsonrpc_call(self, method, params, timeout=10):
        self.dumps.append((method, tuple(params)))
        if isinstance(self.tree, Exception):
            raise self.tree
        return self.tree


def texts(block):
    return [line['text'] for line in block['lines']]


# spec: ANYM-960
def test_structure_groups_blocks_lines_and_words_as_laid_out(tmp_path):
    from deedkit_android.ui_worker import execute

    result = execute(Device(), {'readScreenText': {'structure': True}}, ocr=fake_tesseract(tmp_path, card_rows()))
    assert [line['text'] for line in result['lines']][:3] == ['Cari transaksi', 'Pesanan Selesai', 'Rp 120.000']
    blocks = result['structure']['blocks']
    assert len(blocks) == 4
    assert texts(blocks[1]) == ['Pesanan Selesai', 'Rp 120.000', 'Toko Contoh 0', '11 Nov 2026']
    assert [line['row'] for line in blocks[1]['lines']] == [0, 0, 1, 2]
    first = blocks[1]['lines'][0]
    assert first['words'] == [{'text': 'Pesanan', 'bounds': '[40,200][124,240]', 'confidence': 90.0},
                              {'text': 'Selesai', 'bounds': '[136,200][220,240]', 'confidence': 90.0}]
    assert first['bounds'] == '[40,200][220,240]' and blocks[1]['bounds'] == '[40,200][820,390]'
    assert blocks[1]['confidence'] == 87.5


# spec: ANYM-960
def test_repeated_cards_become_list_items_with_their_own_rows(tmp_path):
    from deedkit_android.ui_worker import execute

    for tree in (TREE, '<hierarchy />', OSError('dump failed')):
        result = execute(Device(tree), {'readScreenText': {'structure': True}},
                         ocr=fake_tesseract(tmp_path, card_rows(search=tree == TREE)))
        structure = result['structure']
        assert len(structure['lists']) == 1
        items = [structure['blocks'][index] for index in structure['lists'][0]['items']]
        assert [texts(item)[:2] for item in items] == [['Pesanan Selesai', f'Rp 12{i}.000'] for i in range(3)]
        assert all(len(item['lines']) == 4 for item in items)
    assert structure['tree'] == 'unavailable'


# spec: ANYM-960
def test_blocks_merge_into_the_smallest_tree_node_and_tree_text_is_marked(tmp_path):
    from deedkit_android.ui_worker import execute

    result = execute(Device(), {'readScreenText': {'structure': True}}, ocr=fake_tesseract(tmp_path, card_rows()))
    blocks = result['structure']['blocks']
    assert blocks[0]['node'] == {'index': 1, 'class': 'android.widget.EditText', 'resource-id': 'shop:id/search',
                                 'bounds': '[30,0][400,45]'}
    assert blocks[0]['lines'][0]['source'] == 'tree' and blocks[0]['lines'][0]['node'] == 1
    for i, block in enumerate(blocks[1:]):
        assert block['node'] == {'index': 2 + i, 'class': 'android.view.ViewGroup',
                                 'bounds': f'[20,{180 + i * 300}][1060,{400 + i * 300}]'}
        assert {line['source'] for line in block['lines']} == {'ocr'}
    assert result['structure']['tree'] == 'merged'


# spec: ANYM-960
def test_structure_is_bounded_and_flat_lines_stay_unchanged(tmp_path):
    from deedkit_android.ui_worker import execute
    from deedkit_android.uiautomator import validate_commands
    from deedkit_android.flows import compile_flow
    from deedkit_android.uiautomator import UI_FIELDS

    ocr = fake_tesseract(tmp_path, card_rows())
    plain = execute(Device(), {'readScreenText': {}}, ocr=ocr)
    assert 'structure' not in plain and len(plain['lines']) == 13
    small = execute(Device(), {'readScreenText': {'structure': True, 'limit': 5}}, ocr=ocr)
    assert sum(len(block['lines']) for block in small['structure']['blocks']) == 5
    assert small['structure']['truncated'] is True
    with pytest.raises(Refused):
        validate_commands(compile_flow('- readScreenText: {structure: yes_please}', {}, fields=UI_FIELDS))
    validate_commands(compile_flow('- readScreenText: {structure: true, key: orders}', {}, fields=UI_FIELDS))


# spec: ANYM-960
def test_structure_lands_in_receipt_data(tmp_path, monkeypatch):
    import asyncio
    import uuid
    from deedkit_android.device import Devices
    from deedkit_android.flows import compile_flow
    from deedkit_android.store import Store
    from deedkit_android.uiautomator import UI_FIELDS

    async def scenario():
        store = Store(tmp_path / 'state')
        devices = Devices(SimpleNamespace(devices={'phone': 'SERIAL'}, adb='/synthetic', uiautomator2_jar='/synthetic', maestro=None,
                                          state_dir=str(tmp_path / 'state')), store)
        structure = {'blocks': [{'lines': []}], 'lists': [], 'tree': 'merged', 'truncated': False}
        async def call(device, command, **options):
            return {'key': 'orders', 'lines': [], 'structure': structure}
        monkeypatch.setattr(devices.ui, 'call', call)
        session = store.reserve('owner', 'phone', 'acquire')['session_id']
        try:
            done = await devices.execute('owner', 'phone', session, str(uuid.uuid4()), None, 10, 10,
                                         compile_flow('- readScreenText: {structure: true, key: orders}', {}, fields=UI_FIELDS))
            assert done['data'] == {'orders': {'lines': [], **structure}}
        finally:
            await devices.close()
            store.close()
    asyncio.run(scenario())


# spec: ANYM-960
@pytest.mark.skipif(shutil.which('tesseract') is None, reason='Tesseract is not installed on this runner')
def test_real_tesseract_cards_become_three_items():
    from deedkit_android.ui_worker import execute

    image = Image.new('RGB', (1080, 1100), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=40)
    for i, (label, price, store, date) in enumerate(CARDS):
        top = 100 + i * 330
        draw.rectangle((20, top - 30, 1060, top + 230), outline=(200, 200, 200))
        draw.text((40, top), label, fill='black', font=font)
        draw.text((700, top), price, fill='black', font=font)
        draw.text((40, top + 80), store, fill=(90, 90, 90), font=font)
        draw.text((40, top + 150), date, fill=(90, 90, 90), font=font)
    target = io.BytesIO()
    image.save(target, format='PNG')
    result = execute(Device('<hierarchy />', target.getvalue()), {'readScreenText': {'structure': True}},
                     ocr={'command': shutil.which('tesseract'), 'languages': 'eng'})
    structure = result['structure']
    assert len(structure['lists']) == 1 and len(structure['lists'][0]['items']) == 3
    for i, index in enumerate(structure['lists'][0]['items']):
        assert f'Rp 12{i}.000' in texts(structure['blocks'][index])
    json.dumps(result)


GRID = ('<hierarchy><node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">'
        '<node class="android.widget.EditText" text="Search" bounds="[40,40][1040,140]"/>'
        '<node class="androidx.recyclerview.widget.RecyclerView" scrollable="true" bounds="[0,200][1080,2400]">'
        '<node class="android.view.ViewGroup" clickable="true" bounds="[20,220][530,1000]"/>'
        '<node class="android.view.ViewGroup" clickable="true" bounds="[550,220][1060,900]"/>'
        '<node class="android.view.ViewGroup" clickable="true" bounds="[20,1020][530,1700]"/>'
        '<node class="android.view.ViewGroup" clickable="true" bounds="[550,1020][1060,1800]"/>'
        '</node></node></hierarchy>')


def grid_lines():
    def line(text, left, top):
        return {'text': text, 'box': (left, top, left + 400, top + 40), 'bounds': f'[{left},{top}][{left + 400},{top + 40}]',
                'confidence': 90.0, 'words': [], 'source': 'ocr'}
    # Product photos (no text) fill each card's top; titles wrap differently, so rows differ.
    cards = [(40, 760, ['Clay Tools 36pcs', 'Rp68.310', 'Power Shop']),
             (570, 700, ['Body Wash 200g', 'Rp46.718']),
             (40, 1480, ['Facial Wash', 'and Toner Set', 'Rp213.596', 'Bisa COD']),
             (570, 1580, ['Sabun Mandi', 'Rp243.804', 'Belanja'])]
    lines = [line('Search', 60, 70)]
    for left, top, texts in cards:
        lines += [line(text, left, top + 50 * number) for number, text in enumerate(texts)]
    return sorted(lines, key=lambda item: (item['box'][1], item['box'][0]))


# spec: ANYM-960
def test_cards_under_a_scrolling_list_form_one_list_in_reading_order():
    from xml.etree import ElementTree

    from deedkit_android.screen_structure import build

    blocks, lists = build(grid_lines(), ElementTree.fromstring(GRID), (0, 0, 1080, 2400))
    firsts = [blocks[index]['lines'][0]['text'] for index in lists[0]['items']]
    assert len(lists) == 1 and firsts == ['Clay Tools 36pcs', 'Body Wash 200g', 'Facial Wash', 'Sabun Mandi']
    assert all(len(card) == 1 for card in lists[0]['cards'])
    assert 'Search' not in [blocks[index]['lines'][0]['text'] for index in lists[0]['items']]
