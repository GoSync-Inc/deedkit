"""Recognition quality on the committed synthetic screen corpus (tests/fixtures/screens)."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFont
import pytest

CORPUS = Path(__file__).parent / 'fixtures' / 'screens'
TOOLS = Path(__file__).parents[1] / 'tools'
LANGUAGES = {'eng', 'ind', 'rus'}


def tesseract():
    """The Tesseract path with eng, ind and rus. The checks.yml android step sets DEEDKIT_OCR_REQUIRED and
    installs it, so a missing engine fails there; elsewhere the test skips."""
    command = shutil.which('tesseract')
    installed = set()
    if command:
        done = subprocess.run([command, '--list-langs'], capture_output=True, text=True, check=False)
        installed = set(done.stdout.split()[1:]) if done.returncode == 0 else set()
    if LANGUAGES <= installed:
        return command
    if os.environ.get('DEEDKIT_OCR_REQUIRED'):
        pytest.fail(f'The android CI step must install Tesseract with {sorted(LANGUAGES)}; found {sorted(installed)}')
    pytest.skip('Tesseract with eng, ind and rus is not installed here')


def truths():
    return {path.stem: json.loads(path.read_text()) for path in sorted(CORPUS.glob('*.json'))}


# spec: ANYM-961
def test_corpus_covers_themes_languages_sizes_and_layouts():
    screens = truths()
    assert len(screens) >= 20 and all((CORPUS / f'{name}.png').is_file() for name in screens)
    names = ' '.join(screens)
    assert all(theme in names for theme in ('_light', '_dark', '_tinted'))
    assert {language for truth in screens.values() for language in truth['languages'].split('+')} == LANGUAGES
    heights = [line['bounds'][3] - line['bounds'][1] for truth in screens.values() for line in truth['lines']]
    assert min(heights) < 24 and max(heights) > 40
    texts = [line['text'] for truth in screens.values() for line in truth['lines']]
    assert any(text.startswith('Rp ') for text in texts) and any(text.startswith('$') for text in texts)
    assert any('руб.' in text for text in texts) and any('2026' in text for text in texts)
    assert sum(1 for truth in screens.values() if truth['lists']) >= 15
    assert any(name.startswith(('settings', 'receipt')) for name in screens)  # two-column rows


# spec: ANYM-961
def test_local_contrast_makes_light_text_on_a_coloured_button_dark_on_white():
    from deedkit_android.screen_text import normalize

    image = Image.new('RGB', (600, 300), '#f2f2f2')
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((20, 100, 580, 200), radius=50, fill='#03ac0e')
    draw.text((300, 150), 'Bayar', fill='white', font=ImageFont.load_default(size=40), anchor='mm')
    result = normalize(image)
    assert result.size == image.size and result.mode == 'L'
    assert result.getpixel((5, 5)) > 200 and result.getpixel((40, 150)) > 200  # page and button background
    stroke = min(result.getpixel((x, y)) for x in range(240, 360) for y in range(130, 170))
    assert stroke < 80  # the white letters are now dark
    edge = min(result.getpixel((x, 100)) for x in range(100, 500))
    assert edge > 150  # the button's straight edge cancels


# spec: ANYM-961
def test_corpus_character_error_rate_and_list_items_meet_the_bar():
    from deedkit_android.ocr_quality import evaluate

    summary = evaluate(CORPUS, tesseract())
    assert summary['screens'] >= 20
    assert summary['cer_percent'] <= 2.0, summary
    assert summary['list_items_exact'] == summary['list_screens'], summary


# spec: ANYM-966
def test_vision_corpus_character_error_rate_and_list_items_meet_the_bar():
    """Apple Vision with ink-tight boxes holds the same bar; it runs only on a macOS host with the vision extra."""
    from deedkit_android.ocr_quality import evaluate
    from deedkit_android.screen_text import recognize
    from deedkit_android.screen_vision import available

    if not available():
        pytest.skip('Apple Vision (macOS with the vision extra) is not available here')
    summary = evaluate(CORPUS, None, recognizer=lambda image, ocr: recognize(image, {**ocr, 'engine': 'vision'}))
    assert summary['screens'] >= 20
    assert summary['cer_percent'] <= 2.0, summary
    assert summary['list_items_exact'] == summary['list_screens'], summary


# spec: ANYM-961
def test_read_screen_text_reports_preprocessing_and_reads_a_corpus_list(tmp_path):
    from deedkit_android.ui_worker import execute

    class Device:
        def __init__(self, data):
            self._dev, self.data = self, data

        def shell2(self, args, timeout=None, encoding='utf-8'):
            return SimpleNamespace(returncode=0, output=self.data)

        def jsonrpc_call(self, method, params, timeout=10):
            raise RuntimeError('no tree')

    target = io.BytesIO()
    Image.open(CORPUS / 'orders_id_dark.png').save(target, format='PNG')
    result = execute(Device(target.getvalue()), {'readScreenText': {'structure': True}},
                     ocr={'command': tesseract(), 'languages': 'eng+ind'})
    assert result['ocr']['preprocess'] == 'local_contrast'
    assert [len(item['items']) for item in result['structure']['lists']] == [3]
    assert 'Rp 1.250.000' in [line['text'] for line in result['lines']]


# spec: ANYM-980
def test_mask_images_keeps_text_printed_inside_a_photo_out_of_the_lines():
    from deedkit_android.ui_worker import execute

    image = Image.new('RGB', (1080, 700), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 60, 520, 520), fill='#7aa6c2')  # a product photo with text printed in it
    draw.text((60, 260), 'FLASH SALE 50', fill='#10222e', font=ImageFont.load_default(size=56))
    draw.text((580, 100), 'Sepatu Lari Pria', fill='black', font=ImageFont.load_default(size=44))
    draw.text((580, 200), 'Rp 125.000', fill='black', font=ImageFont.load_default(size=44))
    target = io.BytesIO()
    image.save(target, format='PNG')
    hierarchy = ('<hierarchy><node class="android.widget.FrameLayout" bounds="[0,0][1080,700]">'
                 '<node class="android.widget.ImageView" bounds="[40,60][520,520]" />'
                 '<node class="android.widget.TextView" text="Sepatu Lari Pria" bounds="[580,100][1000,160]" />'
                 '<node class="android.widget.TextView" text="Rp 125.000" bounds="[580,200][900,260]" />'
                 '</node></hierarchy>')

    class Device:
        def __init__(self):
            self._dev = self

        def shell2(self, args, timeout=None, encoding='utf-8'):
            return SimpleNamespace(returncode=0, output=target.getvalue())

        def jsonrpc_call(self, method, params, timeout=10):
            return hierarchy

    ocr = {'command': tesseract(), 'languages': 'eng'}
    plain = execute(Device(), {'readScreenText': {'mask_images': False}}, ocr=ocr)
    assert any('FLASH' in line['text'] or 'SALE' in line['text'] for line in plain['lines'])  # the photo's text is readable
    masked = execute(Device(), {'readScreenText': {}}, ocr=ocr)
    inside = [line['text'] for line in masked['lines']
              if all(40 <= int(value) <= 520 for value in line['bounds'].replace('][', ',').strip('[]').split(',')[::2])]
    assert inside == [] and 'FLASH' not in ' '.join(line['text'] for line in masked['lines'])
    texts = [line['text'] for line in masked['lines']]
    assert 'Sepatu Lari Pria' in texts and 'Rp 125.000' in texts
    assert masked['ocr']['masked'] == {'regions': 1, 'skipped_large': 0, 'tree': 'ok'}


# spec: ANYM-981
def test_per_card_returns_one_record_per_card_with_no_line_from_a_neighbour():
    from deedkit_android.ui_worker import execute

    cards = [('Sepatu Lari Pria', 'Rp 125.000'), ('Tas Kulit Wanita', 'Rp 340.000'), ('Topi Baseball Biru', 'Rp 59.000')]
    image = Image.new('RGB', (1080, 900), 'white')
    draw = ImageDraw.Draw(image)
    nodes = ''
    for number, (title, price) in enumerate(cards):
        top = 20 + number * 290
        draw.rectangle((20, top, 1060, top + 270), outline='#9aa0a6', width=3)
        draw.text((60, top + 40), title, fill='black', font=ImageFont.load_default(size=44))
        draw.text((60, top + 150), price, fill='black', font=ImageFont.load_default(size=44))
        nodes += f'<node class="android.view.ViewGroup" clickable="true" bounds="[20,{top}][1060,{top + 270}]" />'  # no text in the tree
    hierarchy = ('<hierarchy><node class="android.widget.FrameLayout" bounds="[0,0][1080,900]">'
                 '<node class="androidx.recyclerview.widget.RecyclerView" scrollable="true" bounds="[0,0][1080,900]">'
                 + nodes + '</node></node></hierarchy>')
    target = io.BytesIO()
    image.save(target, format='PNG')

    class Device:
        def __init__(self):
            self._dev = self

        def shell2(self, args, timeout=None, encoding='utf-8'):
            return SimpleNamespace(returncode=0, output=target.getvalue())

        def jsonrpc_call(self, method, params, timeout=10):
            return hierarchy

    result = execute(Device(), {'readScreenText': {'per_card': True}}, ocr={'command': tesseract(), 'languages': 'eng'})
    assert [card['source'] for card in result['cards']] == ['ocr'] * 3
    for card, own in zip(result['cards'], cards):
        assert [line['text'] for line in card['lines']] == list(own), card
    assert result['ocr']['cards'] == {'tree': 'ok', 'from_tree': 0, 'from_ocr': 3} and result['cards_truncated'] is False


# spec: ANYM-961
def test_evaluation_prints_numbers_and_names_only(tmp_path):
    shutil.copy(CORPUS / 'settings_en_dark.png', tmp_path / 'private-1.png')
    shutil.copy(CORPUS / 'settings_en_dark.json', tmp_path / 'private-1.json')
    engine = tmp_path / 'engine'
    engine.write_text(f'#!{sys.executable}\nprint("level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num'
                      '\\tleft\\ttop\\twidth\\theight\\tconf\\ttext")\n'
                      'print("5\\t1\\t1\\t1\\t1\\t1\\t72\\t180\\t150\\t30\\t95\\tLanguage")\n')
    engine.chmod(0o700)
    done = subprocess.run([sys.executable, str(TOOLS / 'evaluate_ocr.py'), str(tmp_path), '--tsv-command',
                           f'{engine} {{image}} {{languages}}'], capture_output=True, text=True, check=True)
    summary = json.loads(done.stdout)
    assert summary['screens'] == 1 and summary['list_screens'] == 1 and summary['misses'] == ['private-1']
    assert 0 < summary['cer_percent'] <= 100
    truth = json.loads((tmp_path / 'private-1.json').read_text())
    assert not any(line['text'] in done.stdout + done.stderr for line in truth['lines'])
