"""The synthetic canary set (ANYM-683): images with known text and Russian/English voice notes, made with fixed seeds
from text in this file. No real person's data. Needs Pillow and, for the voice notes, macOS `say` and ffmpeg (the mini).
Usage: python generate.py <output directory>"""
import json, random, subprocess, sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
SANS = '/System/Library/Fonts/Supplemental/Arial.ttf'
SERIF = '/System/Library/Fonts/Supplemental/Times New Roman.ttf'
MONO = '/System/Library/Fonts/Supplemental/Courier New.ttf'
random.seed(7)
truth = {}

def photoize(img, angle, name):
    img = img.rotate(angle, expand=True, fillcolor=(90, 80, 70)).filter(ImageFilter.GaussianBlur(0.8))
    px = img.load(); w, h = img.size
    for _ in range(w * h // 40):
        x, y = random.randrange(w), random.randrange(h); v = random.randint(-40, 40)
        r, g, b = px[x, y][:3]; px[x, y] = (max(0, min(255, r + v)), max(0, min(255, g + v)), max(0, min(255, b + v)))
    img.save(out / name, quality=70)

# 1. Receipt (Russian), photographed look
lines = ['ООО «Северный Ветер»', 'Магазин «Продукты у дома»', 'г. Калуга, ул. Садовая, д. 14', 'КАССОВЫЙ ЧЕК № 0457', '01.11.2026 18:42',
         'Молоко 3,2% 1 л       2 x 89,90 = 179,80', 'Хлеб Бородинский            64,50', 'Сыр Российский 0,412 кг    321,36',
         'Яблоки Гренни 1,150 кг     201,25', 'Кофе молотый 250 г         459,00', 'ИТОГО:                   1225,91',
         'Оплата картой             1225,91', 'ИНН 4027123456  ФН 9960440300117744', 'Спасибо за покупку!']
img = Image.new('RGB', (760, 60 + 46 * len(lines)), (246, 243, 236)); d = ImageDraw.Draw(img); f = ImageFont.truetype(MONO, 26)
for i, t in enumerate(lines): d.text((30, 30 + 46 * i), t, font=f, fill=(30, 30, 30))
photoize(img, 2.5, 'receipt_ru.jpg'); truth['receipt_ru.jpg'] = '\n'.join(lines)

# 2. Event poster (mixed ru/en)
img = Image.new('RGB', (900, 1200), (24, 40, 90)); d = ImageDraw.Draw(img)
d.rectangle((0, 820, 900, 1200), fill=(240, 180, 40))
poster = [('ДЖАЗ НА КРЫШЕ', 84, (255, 255, 255), 120), ('Jazz on the Roof', 54, (250, 210, 90), 250), ('Квартет Анны Морозовой', 44, (255, 255, 255), 380),
          ('15 ноября 2026, 19:30', 48, (255, 255, 255), 480), ('Москва, Покровка, 27, стр. 3', 40, (220, 230, 255), 580),
          ('Вход свободный по регистрации', 36, (220, 230, 255), 680), ('Tickets: jazzroof.example/ru', 40, (24, 40, 90), 900), ('Дети до 12 лет — бесплатно', 40, (24, 40, 90), 1000)]
for t, size, col, y in poster: d.text((60, y), t, font=ImageFont.truetype(SANS, size), fill=col)
photoize(img, -1.5, 'poster_mixed.jpg'); truth['poster_mixed.jpg'] = '\n'.join(p[0] for p in poster)

# 3. Scanned document page (Russian, with table)
doc = ['ДОГОВОР АРЕНДЫ ПАРКОВОЧНОГО МЕСТА № 17/26', 'г. Тверь                                              3 октября 2026 г.',
       'Арендодатель Петров Иван Сергеевич и Арендатор Смирнова Ольга Викторовна заключили настоящий договор о',
       'нижеследующем. Арендодатель передаёт во временное пользование парковочное место № 42 на подземном',
       'уровне жилого комплекса «Речной», а Арендатор обязуется своевременно вносить арендную плату.',
       '1. Срок аренды составляет одиннадцать месяцев с даты подписания акта приёма-передачи.',
       '2. Арендная плата вносится не позднее пятого числа каждого месяца на счёт Арендодателя.',
       '3. Стороны вправе расторгнуть договор, письменно уведомив другую сторону за тридцать дней.',
       'Месяц         Сумма, руб.     Срок оплаты', 'Ноябрь        6 500           05.11.2026', 'Декабрь       6 500           05.12.2026',
       'Январь        7 000           05.01.2027', 'Подписи сторон: ____________ Петров И. С.     ____________ Смирнова О. В.']
img = Image.new('RGB', (1700, 120 + 70 * len(doc)), (255, 255, 255)); d = ImageDraw.Draw(img); f = ImageFont.truetype(SERIF, 34)
for i, t in enumerate(doc): d.text((80, 60 + 70 * i), t, font=ImageFont.truetype(SERIF, 44) if i == 0 else f, fill=(10, 10, 10))
img = img.convert('L').rotate(0.6, expand=True, fillcolor=255).filter(ImageFilter.GaussianBlur(0.6)).convert('RGB')
img.save(out / 'scan_ru.png'); img.save(out / 'scan_ru.pdf'); truth['scan_ru.png'] = '\n'.join(doc)


random.seed(11)
hard = {}
r = Image.open(out / 'receipt_ru.jpg'); w, h = r.size
grad = Image.linear_gradient('L').resize((w, h)).point(lambda v: 60 + v // 2)
r = Image.composite(r, Image.new('RGB', (w, h), (20, 18, 15)), grad)
r = r.resize((int(w * 0.5), int(h * 0.5))).filter(ImageFilter.GaussianBlur(0.9))
r.save(out / 'receipt_hard.jpg', quality=35); hard['receipt_hard.jpg'] = truth['receipt_ru.jpg']
note = ['Список на субботу:', 'купить батарейки для пульта', 'забрать посылку на почте до 15:00', 'позвонить бабушке Вере',
        'оплатить кружок — 3 200 р.', 'взять Тимура на тренировку в 17:30', 'не забыть: ключи от гаража у соседа']
img = Image.new('RGB', (1000, 120 + 90 * len(note)), (250, 248, 235)); d = ImageDraw.Draw(img)
for y in range(100, img.size[1], 90): d.line((0, y + 62, 1000, y + 62), fill=(170, 190, 225), width=2)
for i, t in enumerate(note):
    f = ImageFont.truetype('/System/Library/Fonts/Supplemental/Comic Sans MS.ttf', random.randint(38, 44))
    d.text((60 + random.randint(-10, 10), 100 + 90 * i + random.randint(-6, 6)), t, font=f, fill=(25, 40, 120))
img = img.rotate(-4, expand=True, fillcolor=(60, 60, 60)).filter(ImageFilter.GaussianBlur(1.1)).resize((700, int(700 * img.size[1] / img.size[0])))
img.save(out / 'note_hand.jpg', quality=50); hard['note_hand.jpg'] = '\n'.join(note)
s = Image.open(out / 'scan_ru.png'); s = s.resize((int(s.size[0] * 0.42), int(s.size[1] * 0.42)))
s.save(out / 'scan_lowdpi.png'); hard['scan_lowdpi.png'] = truth['scan_ru.png']

truth.update(hard)
(out / 'truth.json').write_text(json.dumps(truth, ensure_ascii=False, indent=1))
# The text-only document: the extracted text of the scan (the document kind sends text and no image).
(out / 'doc_text.txt').write_text(truth['scan_ru.png'])

# Voice notes: a 72 s Russian note, a noisy copy, a 15 s English note, a 20-minute recording made by repeating the note.
EN = ("Hi, it's me. The plumber moved our appointment to Thursday afternoon, somewhere between two and five. Could you leave the "
      "spare key with the neighbour if you are out? Also, remind me to renew the car insurance before the end of the month. Thanks, talk later.")
(out / 'voice_en.txt').write_text(EN)
(out / 'voice_ru.txt').write_text((HERE / 'voice_ru.txt').read_text())

def run(*argv):
    subprocess.run(argv, check=True, stdin=subprocess.DEVNULL)

run('say', '-v', 'Milena', '-f', str(HERE / 'voice_ru.txt'), '-o', str(out / 'note_ru.aiff'))
run('say', '-v', 'Samantha', '-f', str(out / 'voice_en.txt'), '-o', str(out / 'note_en.aiff'))
for name in ('note_ru', 'note_en'):
    run('ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(out / f'{name}.aiff'), '-ar', '16000', '-ac', '1', '-c:a', 'libopus', str(out / f'{name}.ogg'))
run('ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(out / 'note_ru.ogg'), '-filter_complex',
    '[0:a]aformat=sample_rates=16000[a];anoisesrc=color=pink:amplitude=0.02:seed=11:sample_rate=16000[n];[a][n]amix=inputs=2:duration=first:normalize=0',
    '-c:a', 'libopus', str(out / 'note_ru_noisy.ogg'))
run('ffmpeg', '-nostdin', '-v', 'error', '-y', '-stream_loop', '16', '-i', str(out / 'note_ru.ogg'), '-c:a', 'libopus', str(out / 'note_ru_long.ogg'))
print('ok', sorted(p.name for p in out.iterdir()))
