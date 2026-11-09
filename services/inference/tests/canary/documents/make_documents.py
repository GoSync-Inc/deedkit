"""The document canary set of ANYM-850: text.pdf, table.pdf, doc.docx, deck.pptx and truth.json. All content is invented.

Taken from the research run doc-extract-11-09 (its make_fixtures.py, the four documents only). The committed files are
its output; this script regenerates them on a Mac with reportlab, python-docx and python-pptx installed (none of them
is a dependency of the service): `python make_documents.py <out dir>`. The PDFs embed the macOS font below because
the text is Russian and English; the rouble sign does not survive PDF text extraction (the research saw that with
every engine), so the scorer drops it."""

import json
import sys
from pathlib import Path

OUT = Path(sys.argv[1])
FONT = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"

PAGES = [
    ("Договор аренды склада", [
        "Страница один маркер альфа. Арендатор обязуется вносить плату до пятого числа каждого месяца.",
        "The tenant shall pay the rent before the fifth day of each month without any deduction.",
        "Сумма ежемесячного платежа составляет 145 000 рублей, включая коммунальные услуги и охрану."]),
    ("Обязанности сторон", [
        "Страница два маркер бета. Арендодатель предоставляет доступ к помещению круглосуточно.",
        "The landlord keeps the loading dock, the fire alarm and the heating system in working order.",
        "Срок действия договора: с 1 марта 2026 года по 28 февраля 2027 года включительно."]),
    ("Final provisions", [
        "Страница три маркер гамма. Споры разрешаются в арбитражном суде по месту нахождения склада.",
        "This agreement is signed in two copies of equal legal force, one for each party.",
        "Подписи сторон: Ковалёва Е. Н., Смирнов П. А.; дата подписания 14 февраля 2026 года."]),
]
TABLE = [["Артикул", "Наименование", "Кол-во", "Цена, ₽", "Warehouse"],
         ["A-1021", "Кабель медный", "120", "84,50", "North"],
         ["A-1022", "Розетка двойная", "45", "312,00", "South"],
         ["B-2040", "Щит распределительный", "3", "18 900,00", "North"],
         ["B-2041", "Автомат 16А", "60", "540,00", "East"],
         ["C-3100", "Lamp LED 12W", "200", "129,90", "West"],
         ["C-3101", "Светильник уличный", "12", "4 250,00", "South"],
         ["D-4007", "Датчик движения", "25", "890,00", "East"]]
QUARTERS = [["Квартал", "Выручка", "Расходы"], ["Q1 2026", "1 200 000", "950 000"], ["Q2 2026", "1 450 000", "1 010 000"]]
SLIDES = [("Квартальный обзор склада", ["Слайд один маркер альфа", "Выручка выросла на 21 процент",
                                         "New warehouse opened in Kazan"]),
          ("План на второй квартал", ["Слайд два маркер бета", "Автоматизация приёмки товара",
                                      "Hire two forklift operators"])]


def pdfs(truth):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(TTFont("AU", FONT))
    heading = ParagraphStyle("h", fontName="AU", fontSize=18, leading=24, spaceAfter=10)
    body = ParagraphStyle("b", fontName="AU", fontSize=11, leading=15, spaceAfter=6)
    story = []
    for index, (title, paragraphs) in enumerate(PAGES):
        story += [Paragraph(title, heading)] + [Paragraph(text, body) for text in paragraphs]
        if index < len(PAGES) - 1:
            story.append(PageBreak())
    SimpleDocTemplate(str(OUT / "text.pdf"), pagesize=A4).build(story)
    truth["text.pdf"] = {"text": "\n".join(h + "\n" + "\n".join(ps) for h, ps in PAGES), "headings": [h for h, _ in PAGES],
                         "markers": ["маркер альфа", "маркер бета", "маркер гамма"], "table": None}
    grid = [("FONTNAME", (0, 0), (-1, -1), "AU"), ("GRID", (0, 0), (-1, -1), 0.5, colors.black)]
    stock = Table(TABLE, repeatRows=1)
    stock.setStyle(TableStyle(grid + [("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
    quarters = Table(QUARTERS)
    quarters.setStyle(TableStyle(grid))
    SimpleDocTemplate(str(OUT / "table.pdf"), pagesize=A4).build(
        [Paragraph("Остатки на складе", heading), stock, Spacer(1, 20), Paragraph("Квартальный отчёт", heading), quarters])
    text = ("Остатки на складе\n" + "\n".join(" ".join(row) for row in TABLE) + "\nКвартальный отчёт\n"
            + "\n".join(" ".join(row) for row in QUARTERS))
    truth["table.pdf"] = {"text": text, "headings": ["Остатки на складе", "Квартальный отчёт"], "markers": None,
                          "table": TABLE}


def docx_file(truth):
    import docx

    document = docx.Document()
    for title, paragraphs in PAGES[:2]:
        document.add_heading(title, level=1)
        for text in paragraphs:
            document.add_paragraph(text)
    table = document.add_table(rows=5, cols=5)
    for r, row in enumerate(TABLE[:5]):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    document.save(OUT / "doc.docx")
    truth["doc.docx"] = {"text": "\n".join(h + "\n" + "\n".join(ps) for h, ps in PAGES[:2]) + "\n"
                         + "\n".join(" ".join(row) for row in TABLE[:5]),
                         "headings": [h for h, _ in PAGES[:2]], "markers": ["маркер альфа", "маркер бета"],
                         "table": TABLE[:5]}


def pptx_file(truth):
    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    for title, bullets in SLIDES:
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = title
        frame = slide.placeholders[1].text_frame
        frame.text = bullets[0]
        for bullet in bullets[1:]:
            frame.add_paragraph().text = bullet
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "Остатки по позициям"
    shape = slide.shapes.add_table(5, 5, Inches(0.5), Inches(1.5), Inches(9), Inches(3))
    for r, row in enumerate(TABLE[:5]):
        for c, value in enumerate(row):
            shape.table.cell(r, c).text = value
    deck.save(OUT / "deck.pptx")
    truth["deck.pptx"] = {"text": "\n".join(t + "\n" + "\n".join(b) for t, b in SLIDES) + "\nОстатки по позициям\n"
                          + "\n".join(" ".join(row) for row in TABLE[:5]),
                          "headings": [t for t, _ in SLIDES] + ["Остатки по позициям"],
                          "markers": ["маркер альфа", "маркер бета"], "table": TABLE[:5]}


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    truth = {}
    pdfs(truth)
    docx_file(truth)
    pptx_file(truth)
    (OUT / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1) + "\n")
