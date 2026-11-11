#!/usr/bin/env python3
"""Render the synthetic OCR quality corpus: phone-like screens with their ground truth.

Usage: (cd services/android && uv run python tools/make_screen_corpus.py tests/fixtures/screens [FONT_DIR])
Writes <name>.png and <name>.json per screen. All text is invented; no real screen is used. The PNGs are
committed, so CI never renders; rerun only to change the corpus (fonts: Arial, Verdana, Tahoma from macOS).
"""

import json
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont

WIDTH, MARGIN, PAD = 1080, 40, 32
THEMES = {
    'light': {'page': '#f2f2f2', 'card': '#ffffff', 'text': '#202124', 'muted': '#6b6b6b', 'accent': '#03ac0e',
              'on_accent': '#ffffff', 'line': '#dddddd'},
    'dark': {'page': '#121212', 'card': '#1e1e1e', 'text': '#e8e8e8', 'muted': '#a0a0a0', 'accent': '#8ab4f8',
             'on_accent': '#101010', 'line': '#333333'},
    'tinted': {'page': '#e3f2fd', 'card': '#ffffff', 'text': '#0d47a1', 'muted': '#5c6f82', 'accent': '#ff7043',
               'on_accent': '#ffffff', 'line': '#bbdefb'},
}


class Screen:
    def __init__(self, fonts, theme, face='arial', height=2400):
        self.fonts, self.colors, self.face = fonts, THEMES[theme], face
        self.image = Image.new('RGB', (WIDTH, height), self.colors['page'])
        self.draw = ImageDraw.Draw(self.image)
        self.y, self.lines, self.lists = MARGIN, [], []

    def font(self, size, bold=False, face=None):
        return ImageFont.truetype(str(self.fonts[(face or self.face) + ('_bold' if bold else '')]), size)

    def text(self, x, y, value, size, color='text', bold=False, face=None, anchor='la'):
        font = self.font(size, bold, face)
        self.draw.text((x, y), value, fill=self.colors.get(color, color), font=font, anchor=anchor)
        left, top, right, bottom = self.draw.textbbox((x, y), value, font=font, anchor=anchor)
        self.lines.append({'text': value, 'bounds': [left, top, right, bottom]})
        return size

    def row(self, left, right=None, size=32, gap=None, **style):
        """One visual row: a left text and an optional right-aligned text (str or {'text', 'color'})."""
        x, width = MARGIN + PAD, WIDTH - 2 * (MARGIN + PAD)
        for value, position, anchor in ((left, x, 'la'), (right, x + width, 'ra')):
            if value is not None:
                spec = value if isinstance(value, dict) else {'text': value}
                self.text(position, self.y, spec['text'], size, anchor=anchor,
                          **({'color': spec['color']} if 'color' in spec else style))
        self.y += size + (gap if gap is not None else round(size * 0.55))

    def header(self, title, size=44):
        self.draw.rectangle((0, 0, WIDTH, self.y + size + 48), fill=self.colors['card'])
        self.y += 20
        self.text(MARGIN, self.y, title, size, bold=True)
        self.y += size + 76

    def card(self, rows, size=32):
        top = self.y
        self.y += PAD
        start = len(self.lines)
        for row in rows:
            self.row(*row, size=size) if isinstance(row, tuple) else self.row(row, size=size)
        bottom = self.y - round(size * 0.55) + PAD
        self.draw.rounded_rectangle((MARGIN, top, WIDTH - MARGIN, bottom), radius=24, fill=self.colors['card'])
        # Redraw the text over the card background, keeping the recorded boxes.
        recorded, self.lines = self.lines[start:], self.lines[:start]
        self.y = top + PAD
        for row in rows:
            self.row(*row, size=size) if isinstance(row, tuple) else self.row(row, size=size)
        assert [line['text'] for line in self.lines[start:]] == [line['text'] for line in recorded]
        self.y = bottom + 40

    def item_list(self, cards, size=32):
        for rows in cards:
            self.card(rows, size)
        self.lists.append(len(cards))

    def settings(self, pairs, size=32):
        """Two-column rows separated by dividers: each row is one list item."""
        for index, (label, value) in enumerate(pairs):
            self.row(label, muted(value), size=size, gap=0)
            self.y += 44
            if index < len(pairs) - 1:
                self.draw.line((MARGIN, self.y - 22, WIDTH - MARGIN, self.y - 22), fill=self.colors['line'], width=2)
        self.lists.append(len(pairs))

    def paragraph(self, lines, size=30):
        for line in lines:
            self.row(line, size=size, gap=round(size * 0.45))
        self.y += 40

    def button(self, label, size=36):
        height = size + 2 * PAD
        self.draw.rounded_rectangle((MARGIN, self.y, WIDTH - MARGIN, self.y + height), radius=height // 2,
                                    fill=self.colors['accent'])
        self.text(WIDTH // 2, self.y + PAD, label, size, color='on_accent', bold=True, anchor='ma')
        self.y += height + 40

    def save(self, directory, name, languages):
        image = self.image.crop((0, 0, WIDTH, min(self.image.height, self.y + MARGIN)))
        image.save(directory / f'{name}.png', format='PNG', optimize=True)
        (directory / f'{name}.json').write_text(json.dumps(
            {'languages': languages, 'size': list(image.size), 'lines': self.lines, 'lists': self.lists},
            ensure_ascii=False, indent=1) + '\n')


def muted(value):
    return {'text': value, 'color': 'muted'}


def screens():
    """(name, theme, languages, builder[, font face]). Texts are invented examples of common phone screens."""
    def orders_id(screen, size=32):
        screen.header('Daftar Transaksi')
        screen.item_list([
            [('Pesanan Selesai', 'Rp 125.000'), muted('Rabu, 11 Nov 2026'), 'Kopi Susu Gula Aren 2x'],
            [('Dikirim', 'Rp 89.500'), muted('Senin, 9 Nov 2026'), 'Kabel Data USB-C 1 m'],
            [('Menunggu Pembayaran', 'Rp 1.250.000'), muted('Minggu, 8 Nov 2026'), 'Sepatu Lari Ukuran 42'],
        ], size)

    def orders_en(screen, size=32):
        screen.header('Your Orders')
        screen.item_list([
            [('Delivered', '$12.99'), muted('Nov 11, 2026'), 'Phone case, matte black'],
            [('Shipped', '$45.00'), muted('Nov 9, 2026'), 'Wireless charger 15W'],
            [('Cancelled', '$7.50'), muted('Nov 2, 2026'), 'Screen protector 2-pack'],
            [('Delivered', '$129.00'), muted('Oct 28, 2026'), 'Running shoes, size 10'],
        ], size)

    def orders_ru(screen, size=32):
        screen.header('Мои заказы')
        screen.item_list([
            [('Доставлен', '1 299 руб.'), muted('11 ноября 2026'), 'Чехол для телефона'],
            [('В пути', '459 руб.'), muted('9 ноября 2026'), 'Кабель USB-C, 1 метр'],
            [('Отменён', '2 990 руб.'), muted('3 ноября 2026'), 'Наушники беспроводные'],
        ], size)

    def chats_ru(screen, size=30):
        screen.header('Чаты')
        screen.item_list([
            [('Анна Петрова', muted('10:42')), muted('Встречаемся у входа в 19:00')],
            [('Рабочая группа', muted('09:15')), muted('Отчёт за октябрь готов')],
            [('Сергей', muted('вчера')), muted('Спасибо, получил документы')],
            [('Доставка', muted('8 ноя')), muted('Ваш заказ № 48213 передан курьеру')],
        ], size)

    def chats_id(screen, size=30):
        screen.header('Pesan')
        screen.item_list([
            [('Budi Santoso', muted('10.42')), muted('Sampai jumpa besok pagi')],
            [('Toko Elektronik', muted('09.15')), muted('Pesanan Anda sudah dikirim')],
            [('Ibu Sari', muted('Kemarin')), muted('Terima kasih banyak ya')],
        ], size)

    def settings_en(screen, size=32):
        screen.header('Settings')
        screen.settings([('Language', 'English'), ('Time zone', 'GMT+8'), ('Storage used', '41.2 GB'),
                         ('Battery', '87%'), ('Version', '16.0.2')], size)

    def settings_id(screen, size=32):
        screen.header('Pengaturan Akun')
        screen.settings([('Bahasa', 'Indonesia'), ('Nomor HP', '0812 3456 7890'), ('Saldo', 'Rp 250.000'),
                         ('Poin', '1.240'), ('Bergabung', '12 Jan 2024')], size)

    def settings_ru(screen, size=32):
        screen.header('Настройки')
        screen.settings([('Язык', 'Русский'), ('Часовой пояс', 'UTC+3'), ('Баланс', '1 500 руб.'),
                         ('Последний вход', '11.11.2026'), ('Версия', '4.12.0')], size)

    def receipt_en(screen, size=30):
        screen.header('Payment Details')
        screen.paragraph(['Paid on Wednesday, November 11, 2026', 'Order number 2026-1111-0042'], size)
        screen.settings([('Subtotal', '$58.40'), ('Shipping', '$4.99'), ('Tax', '$5.26'), ('Total', '$68.65')], size)
        screen.button('Download receipt')

    def receipt_id(screen, size=30):
        screen.header('Rincian Pembayaran')
        screen.paragraph(['Dibayar pada Rabu, 11 November 2026', 'Nomor invoice INV/20261111/0042'], size)
        screen.settings([('Total Harga', 'Rp 98.000'), ('Ongkos Kirim', 'Rp 12.000'), ('Diskon', '-Rp 10.000'),
                         ('Total Bayar', 'Rp 100.000')], size)
        screen.button('Lihat Invoice')

    def article_ru(screen, size=30):
        screen.header('Новости')
        screen.paragraph(['Сегодня в городе ожидается солнечная', 'погода, днём до плюс восемнадцати',
                          'градусов, ветер слабый.', 'Вечером возможен небольшой дождь.'], size)
        screen.button('Читать дальше')

    def article_en(screen, size=28):
        screen.header('Help Center')
        screen.paragraph(['Refunds are processed within 3 to 5', 'business days after we receive the',
                          'returned item. You will get an email', 'when the refund is complete.'], size)
        screen.button('Contact support')

    def big_text(screen, size=48):
        screen.header('Saldo GoPay', size=48)
        screen.paragraph(['Rp 1.234.567', 'Diperbarui 11 Nov 2026'], size)
        screen.button('Isi Saldo', size=44)

    def small_text_en(screen, size=24):
        screen.header('Notifications', size=36)
        screen.item_list([
            [('Package arrived', muted('2m')), muted('Your parcel is at the front desk')],
            [('Payment received', muted('1h')), muted('You received $25.00 from Alex')],
            [('Reminder', muted('3h')), muted('Dentist appointment on Nov 12 at 9:30')],
            [('Security alert', muted('1d')), muted('New sign-in from Chrome on Windows')],
            [('Weekly report', muted('2d')), muted('You walked 42,318 steps this week')],
        ], size)

    def small_text_ru(screen, size=24):
        screen.header('Уведомления', size=36)
        screen.item_list([
            [('Посылка прибыла', muted('2 мин')), muted('Заберите заказ в пункте выдачи')],
            [('Платёж получен', muted('1 ч')), muted('Перевод 2 500 руб. от Ивана')],
            [('Напоминание', muted('3 ч')), muted('Приём у врача 12 ноября в 9:30')],
        ], size)

    def products_id(screen, size=30):
        screen.header('Hasil Pencarian')
        screen.item_list([
            [('Kaos Polos Katun', 'Rp 49.900'), muted('Terjual 1rb+'), muted('Jakarta Barat')],
            [('Celana Jeans Slim', 'Rp 179.000'), muted('Terjual 250+'), muted('Bandung')],
            [('Topi Baseball', 'Rp 35.000'), muted('Terjual 3rb+'), muted('Surabaya')],
        ], size)

    return [
        ('orders_id_light', 'light', 'eng+ind', orders_id),
        ('orders_id_dark', 'dark', 'eng+ind', orders_id),
        ('orders_en_light', 'light', 'eng', orders_en),
        ('orders_en_tinted', 'tinted', 'eng', orders_en),
        ('orders_ru_light', 'light', 'eng+rus', orders_ru),
        ('orders_ru_dark', 'dark', 'eng+rus', orders_ru),
        ('chats_ru_light', 'light', 'eng+rus', chats_ru),
        ('chats_id_dark', 'dark', 'eng+ind', chats_id),
        ('settings_en_dark', 'dark', 'eng', settings_en),
        ('settings_id_light', 'light', 'eng+ind', settings_id),
        ('settings_ru_tinted', 'tinted', 'eng+rus', settings_ru),
        ('receipt_en_light', 'light', 'eng', receipt_en),
        ('receipt_id_dark', 'dark', 'eng+ind', receipt_id),
        ('article_ru_dark', 'dark', 'eng+rus', article_ru),
        ('article_en_tinted', 'tinted', 'eng', article_en),
        ('balance_id_light', 'light', 'eng+ind', big_text),
        ('balance_id_dark', 'dark', 'eng+ind', big_text),
        ('notifications_en_light', 'light', 'eng', small_text_en),
        ('notifications_en_dark', 'dark', 'eng', small_text_en),
        ('notifications_ru_dark', 'dark', 'eng+rus', small_text_ru),
        ('products_id_tinted', 'tinted', 'eng+ind', products_id),
        ('products_id_verdana', 'light', 'eng+ind', products_id, 'verdana'),
        ('chats_ru_tahoma', 'dark', 'eng+rus', chats_ru, 'tahoma'),
        ('settings_en_verdana', 'light', 'eng', settings_en, 'verdana'),
    ]


def main(output, font_dir='/System/Library/Fonts/Supplemental'):
    output, font_dir = Path(output), Path(font_dir)
    fonts = {'arial': font_dir / 'Arial.ttf', 'arial_bold': font_dir / 'Arial Bold.ttf',
             'verdana': font_dir / 'Verdana.ttf', 'verdana_bold': font_dir / 'Verdana Bold.ttf',
             'tahoma': font_dir / 'Tahoma.ttf', 'tahoma_bold': font_dir / 'Tahoma Bold.ttf'}
    output.mkdir(parents=True, exist_ok=True)
    for name, theme, languages, build, *face in screens():
        screen = Screen(fonts, theme, *face)
        build(screen)
        screen.save(output, name, languages)


if __name__ == '__main__':
    main(*sys.argv[1:])
