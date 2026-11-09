"""ANYM-892: fixed ru/en rules that read a message's scheduling move, medium and times.

Pure: no database, network or clock. A phrase either matches one of these rules or yields nothing; every
assumption a rule makes is named in the result's `assumed` list (pm, next_week, default_duration, same_day).
Chat text is data: nothing in it is ever executed.
"""
import re
import unicodedata
from bisect import bisect_right
from urllib.parse import urlsplit
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .time_references import EN_DAY, PAST_AFTER, PAST_BEFORE, RU_DAY, WEEKDAYS

MAX_CHARS = 4096
INTENTS = ('ask_availability', 'propose', 'accept', 'decline', 'cancel', 'reschedule', 'none')
MEDIA = ('call', 'video_call', 'meeting', 'unknown')
LINK_HOSTS = ('zoom.us', 'meet.google.com', 'teams.microsoft.com', 'telemost.yandex.ru')
ANY_LINK = re.compile(r'https?://\S+', re.I)


def _rx(*words):
    return re.compile('|'.join(words), re.I)


# Templates that state a meeting rather than arrange one: reminders, scheduled-meeting notices, call logs.
TEMPLATE = _rx(r'^\W*(?:напоминани[ея]|reminder)\b', r'\byour\s+(?:\w+\s+){0,2}meeting\s+(?:is|has been)\s+scheduled',
    r'\bis\s+scheduled\s+for\b', r'\bзапланирован[аоы]?\b', r'\bпропущенн\w*\s+(?:видео)?звон', r'\bmissed\s+(?:video\s+)?call\b',
    r'\b(?:входящ|исходящ)\w*\s+(?:видео)?звон', r'\b(?:incoming|outgoing)\s+(?:video\s+)?call\b',
    r'\b(?:звонок|вызов)\s+(?:заверш[её]н|окончен)', r'\bcall\s+ended\b')
VIDEO = _rx(r'\bzoom\b', r'\bзум\w*', r'\bgoogle\s+meet\b', r'\bteams\b', r'\bтелемост\w*', r'\bвидео\s?звон\w*',
    r'\bвидеосозвон\w*', r'\bvideo\s+call\b')
CALL = _rx(r'\bсозвон\w*', r'\bпозвон\w*', r'\bзвон\w*', r'\bнаберу\b', r'\bнабери\b', r'\bcall\b', r'\bphone\b', r'\bring\s+you\b')
MEETING = _rx(r'\bвстреч\w*', r'\bвстрет\w*', r'\bувидимся\b', r'\bувидеться\b', r'\bпересеч[её]мся\b', r'\bпересечься\b',
    r'\bкофе\b', r'\bпообеда\w*', r'\bпоужина\w*', r'\bприеду\b', r'\bзаеду\b', r'\bподъеду\b', r'\bзаехать\b',
    r'\bmeet\b', r'\bmeeting\b', r'\bcatch\s+up\b', r'\bcoffee\b', r'(?<!after\s)\blunch\b', r'\bdinner\b', r'\bsee\s+you\b',
    r'\bappointment\b', r'\bcome\s+(?:by|over)\b')
CUES = {
    'cancel': _rx(r'\bотмен\w*', r'\bне\s+состоится\b', r'\bcancel\w*', r'\bcall\s+(?:it\s+)?off\b'),
    'reschedule': _rx(r'\bперенес\w*', r'\bперенос\w*', r'\bперенест\w*', r'\breschedul\w*', r'\bpostpone\w*',
        r'\bmove\s+(?:it|the\s+\w+|our\s+\w+)\b', r'\bpush\s+(?:it|the\s+\w+)\b'),
    'decline': _rx(r'\bне\s+могу\b', r'\bне\s+смогу\b', r'\bне\s+получится\b', r'\bне\s+выйдет\b', r'\bне\s+успеваю\b',
        r'\bне\s+подходит\b', r'\bзанят[аы]?\b', r"\bcan'?t\b", r'\bcannot\b', r'\bcan\s+not\b', r"\bwon'?t\s+work\b",
        r"\bdoesn'?t\s+work\b", r'\bdoes\s+not\s+work\b', r'\bunable\b', r"\bi'?m\s+busy\b", r'\bnot\s+free\b'),
    'accept': _rx(r'\bдоговорились\b', r'\bподходит\b', r'\bгодится\b', r'\bотлично\b', r'\bок\b', r'\bокей\b', r'\bхорошо\b',
        r'\bдавай\s+так\b', r'\bсогласен\b', r'\bсогласна\b', r'\bдо\s+встречи\b', r'\bувидимся\s+тогда\b',
        r'\bworks\s+for\s+me\b', r'\bsounds\s+good\b', r'\bsee\s+you\s+then\b', r'\bdeal\b', r'\bconfirmed\b',
        r'\bperfect\b', r'\bok(?:ay)?\b', r'\bagreed\b'),
    'propose': _rx(r'\bдавай\w*', r'\bпредлагаю\b', r'\bможет\b', r'\bможем\b', r'\bможно\b', r'\bкак\s+насч[её]т\b',
        r'\bлучше\b', r'\bс?можешь\b', r'\bс?можете\b', r'\bhow\s+about\b', r"\blet'?s\b", r'\bshall\s+we\b', r'\bcan\s+we\b',
        r'\bcould\s+we\b', r'\bwhat\s+about\b', r'\bwould\b.{0,30}\bwork\b', r'\bworks\s+for\s+you\b', r'\binstead\b',
        r'\bmaybe\b'),
    'ask_availability': _rx(r'\bкогда\s+(?:тебе|вам)\s+удобно\b', r'\bкогда\s+удобно\b', r'\bкогда\s+сможешь\b',
        r'\bкогда\s+(?:ты|вы)\s+свобод\w*', r'\bты\s+свобод\w*', r'\bвы\s+свобод\w*', r'\bwhen\s+(?:are\s+you|is\s+good|works)\b',
        r'\bwhat\s+time\s+works\b', r'\bare\s+you\s+free\b', r'\bare\s+you\s+available\b'),
}
# A rescheduling request that stands alone is its whole clause: «перенесём?», "can we reschedule?"; «давай файлы перенесём» is not.
RESCHEDULE_ALONE = re.compile(r"(?:(?:может|давай|можем|можно|let'?s|can\s+we|could\s+we|shall\s+we|maybe|(?:i|we)?\s*need\s+to)\s+)?"
    r'(?:перенес\w*|перенест\w*|перенос\w*|reschedul\w*|postpone\w*)(?:\s+(?:it|this|that|это|е[её]|его))?', re.I)
# A clause opener: «лучше в среду» or "how about Wednesday" starts a new clause after a decline.
# A dot or comma between digits ("12.11", "15.00", "12,5") is part of a number, never a break.
OR_WORD = re.compile(r'\b(?:или|либо|or)\b', re.I)
SENTENCE_END = re.compile(r'[.!?](?!\d)|\n')
CLAUSE_BREAK = re.compile(r'[.;!?](?!\d)|\n|,(?!\d)|\b(?:лучше|давай\w*|но|зато|а\s+вот|but|how\s+about|what\s+about|instead|maybe|let\'?s)\b', re.I)
PAST_CLAUSE = _rx(r'\bвчера\b', r'\bпозавчера\b', r'\bпрошл\w*', r'\bназад\b', r'\byesterday\b', r'\blast\b', r'\bago\b',
    r'\bбыла\b', r'\bбыло\b', r'\bбыл\b', r'\bвстречались\b', r'\bсозванивались\b', r'\bwas\b', r'\bwere\b', r'\bmet\b',
    r'\bhad\b')
VERSION_BEFORE = re.compile(r'(?:верси\w*|version|ver\.?|v|релиз\w*|release|build|сборк\w*)\s*$', re.I)

# Explicit zones. A standard or summer abbreviation names a fixed offset (CEST is UTC+2 all year); ET and PT name
# the region's clock, and MSK is Moscow's. Abbreviations that are also words ("wet", "et", "pt") count only in capitals.
# City words are a fixed table of at most 40 entries (ru dative "по <городу>", en "<City> time").
ZONE_WORDS = [(re.compile(r'\bмск\b|\bmsk\b|\bпо\s+москве\b|\bпо\s+московскому\b|\bmoscow\s+time\b', re.I), 'Europe/Moscow'),
    (re.compile(r'\bcest\b', re.I), 'Etc/GMT-2'), (re.compile(r'\bcet\b', re.I), 'Etc/GMT-1'),
    (re.compile(r'\beest\b', re.I), 'Etc/GMT-3'), (re.compile(r'\beet\b', re.I), 'Etc/GMT-2'),
    (re.compile(r'\bWEST\b'), 'Etc/GMT-1'), (re.compile(r'\bWET\b'), 'UTC'), (re.compile(r'\bbst\b', re.I), 'Etc/GMT-1'),
    (re.compile(r'\bET\b'), 'America/New_York'), (re.compile(r'\bEST\b'), 'Etc/GMT+5'), (re.compile(r'\bEDT\b'), 'Etc/GMT+4'),
    (re.compile(r'\bPT\b'), 'America/Los_Angeles'), (re.compile(r'\bPST\b'), 'Etc/GMT+8'), (re.compile(r'\bPDT\b'), 'Etc/GMT+7')]
CITIES = {'лиссабон': 'Europe/Lisbon', 'lisbon': 'Europe/Lisbon', 'лондон': 'Europe/London', 'london': 'Europe/London',
    'париж': 'Europe/Paris', 'paris': 'Europe/Paris', 'берлин': 'Europe/Berlin', 'berlin': 'Europe/Berlin',
    'мадрид': 'Europe/Madrid', 'madrid': 'Europe/Madrid', 'рим': 'Europe/Rome', 'rome': 'Europe/Rome',
    'амстердам': 'Europe/Amsterdam', 'amsterdam': 'Europe/Amsterdam', 'праг': 'Europe/Prague', 'prague': 'Europe/Prague',
    'варшав': 'Europe/Warsaw', 'warsaw': 'Europe/Warsaw', 'киев': 'Europe/Kyiv', 'kyiv': 'Europe/Kyiv',
    'минск': 'Europe/Minsk', 'minsk': 'Europe/Minsk', 'тбилиси': 'Asia/Tbilisi', 'tbilisi': 'Asia/Tbilisi',
    'ереван': 'Asia/Yerevan', 'yerevan': 'Asia/Yerevan', 'стамбул': 'Europe/Istanbul', 'istanbul': 'Europe/Istanbul',
    'дуба': 'Asia/Dubai', 'dubai': 'Asia/Dubai', 'алмат': 'Asia/Almaty', 'almaty': 'Asia/Almaty',
    'бали': 'Asia/Makassar', 'bali': 'Asia/Makassar', 'нью-йорк': 'America/New_York', 'new york': 'America/New_York',
    'екатеринбург': 'Asia/Yekaterinburg', 'новосибирск': 'Asia/Novosibirsk'}
CITY_RU = re.compile(r'\bпо\s+(' + '|'.join(re.escape(c) for c in CITIES if re.match('[а-я]', c)) + r')\w*', re.I)
CITY_EN = re.compile(r'\b(' + '|'.join(re.escape(c) for c in CITIES if re.match('[a-z]', c)) + r')\s+time\b', re.I)
UTC_OFFSET = re.compile(r'\b(?:utc|gmt)\s*([+\-−])\s*(\d{1,2})\b|\b(utc|gmt)\b', re.I)

RU_MONTHS = {'январ': 1, 'феврал': 2, 'март': 3, 'апрел': 4, 'ма': 5, 'июн': 6, 'июл': 7, 'август': 8, 'сентябр': 9,
    'октябр': 10, 'ноябр': 11, 'декабр': 12}
EN_MONTHS = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10,
    'nov': 11, 'dec': 12}
RU_MONTH = r'(январ[яь]|феврал[яь]|марта?|апрел[яь]|ма[яй]|июн[яь]|июл[яь]|августа?|сентябр[яь]|октябр[яь]|ноябр[яь]|декабр[яь])'
EN_MONTH = r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
RELATIVE = [(re.compile(r'\bпослезавтра(?:шн\w*)?\b|\bday\s+after\s+tomorrow\b', re.I), 2),
    (re.compile(r'\bзавтра(?:шн\w*)?\b|\btomorrow\b', re.I), 1), (re.compile(r'\bсегодня(?:шн\w*)?\b|\btoday\b', re.I), 0)]
WEEKDAY = re.compile(rf'(?:\b(следующ\w*|next|this|этот|эту|это)\s+)?\b({RU_DAY}|{EN_DAY})\b', re.I)
WEEK = re.compile(r'\b(?:на\s+)?(эт(?:ой|у)|следующ(?:ей|ую))\s+недел[ею]\b|\b(this|next)\s+week\b', re.I)
# "в пятницу на следующей неделе", "Friday next week": that weekday of the next calendar week.
NEXT_WEEK_AFTER = re.compile(r'^\s*,?\s*(?:на\s+следующей\s+неделе|next\s+week)\b', re.I)
RU_DATE = re.compile(rf'\b(\d{{1,2}})\s+{RU_MONTH}\b', re.I)
EN_DATE = re.compile(rf'\b{EN_MONTH}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b|\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{EN_MONTH}\b', re.I)
ORDINAL = re.compile(r'\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b', re.I)
NUMERIC = re.compile(r'(?<![\d.,/:])(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?(?![\d.,/:]?\d)')
# Times: 15:00, «в 15.00», «в 3», «в 3 часа дня», "at 10:30", "3pm"; ranges «с 14 до 16», "2-4pm", "between 2 and 4".
MARK = r'(?:\s*(am|pm|a\.m\.|p\.m\.)|\s+(?:час(?:а|ов)?\s+)?(утра|дня|вечера|ночи))?'
RANGE = re.compile(r'\b(?:с|from|between)\s+(\d{1,2})(?:[:.](\d{2}))?\s*(?:до|по|to|and|-|–)\s*(\d{1,2})(?:[:.](\d{2}))?' + MARK
    + r'|\b(\d{1,2})(?::(\d{2}))?\s*[-–]\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b'
    + r'|(?<![\d.:])\b(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})(?![\d:])', re.I)
CLOCK = re.compile(r'(?<![\d.:])(\d{1,2}):(\d{2})(?![\d:])' + MARK
    + r'|\b(?:в|во|к|at|by)\s+(\d{1,2})(?:\.(\d{2}))?(?![\d.:,]\d|\d)' + MARK
    + r'|(?<![\d.:])\b(\d{1,2})\s*(am|pm)\b', re.I)
PARTS = [(re.compile(r'\bпосле\s+обеда\b|\bafter\s+lunch\b', re.I), (13, 17)), (re.compile(r'\bв\s+обед\b|\bat\s+lunch\b', re.I), (12, 14)),
    (re.compile(r'\bутром\b|\bс\s+утра\b|\bin\s+the\s+morning\b|\bmorning\b', re.I), (9, 12)),
    (re.compile(r'\bдн[её]м\b|\bin\s+the\s+afternoon\b|\bafternoon\b', re.I), (12, 17)),
    (re.compile(r'\bвечером\b|\bin\s+the\s+evening\b|\bevening\b', re.I), (18, 22)),
    (re.compile(r'\btonight\b', re.I), (18, 22))]


def _zones(text):
    """Explicit zone markers: sorted (start, end, ZoneInfo). An offset outside UTC-12..UTC+14 is no marker."""
    out = []
    for m in UTC_OFFSET.finditer(text):
        if m.group(3):
            out.append((m.start(), m.end(), ZoneInfo('UTC')))
            continue
        hours, east = int(m.group(2)), m.group(1) == '+'
        if hours <= (14 if east else 12):  # Etc/GMT names invert the sign
            out.append((m.start(), m.end(), ZoneInfo('UTC' if hours == 0 else f"Etc/GMT{'-' if east else '+'}{hours}")))
    for rx, name in ZONE_WORDS:
        out += [(m.start(), m.end(), ZoneInfo(name)) for m in rx.finditer(text)]
    for rx in (CITY_RU, CITY_EN):
        for m in rx.finditer(text):
            # Case-insensitive matching accepts letters that .lower() does not fold back ("İ", "ı"): no table entry, no zone.
            key = m.group(1).lower()
            name = next((v for k, v in CITIES.items() if key.startswith(k) or k.startswith(key)), None)
            if name:out.append((m.start(), m.end(), ZoneInfo(name)))
    return sorted(out, key=lambda z: (z[0], z[1]))


def _hour(h, mark, *, bare):
    """Hour on a 24 h clock and the assumption it needed; None for an impossible hour ("24:30", "00pm", "13pm")."""
    mark = (mark or '').lower().replace('.', '')
    if mark:
        if not 1 <= h <= 12:return None, []
        if mark in ('pm', 'дня', 'вечера'):return (h if h == 12 else h + 12), []
        return (0 if h == 12 else h), []  # am, утра, ночи
    if h > 23:return None, []
    if bare and 1 <= h <= 6:return h + 12, ['pm']
    return h, []


def _shift(day, days):
    try:return day + timedelta(days=days)
    except OverflowError:return None


def _days(text):
    """Day references: (start, end, granularity, rule, resolve). `resolve(base)` gives (first day, end day or None,
    assumed) against the send day in the time's own zone, or None when the phrase names no future day there."""
    out = []

    def free(m):return not any(s <= m.start() < e for s, e, *_ in out)

    for rx, n in RELATIVE:
        for m in rx.finditer(text):
            if free(m):out.append((m.start(), m.end(), 'day', 'day:relative', lambda b, n=n: (_shift(b, n), None, [])))
    for m in WEEKDAY.finditer(text):
        weekday = WEEKDAYS.get(m.group(2)[:3].lower())
        before = unicodedata.normalize('NFKC', text[max(0, m.start() - 24):m.start()])
        after = unicodedata.normalize('NFKC', text[m.end():m.end() + 24])
        if weekday is None or PAST_BEFORE.search(before) or PAST_AFTER.search(after):continue
        tail = NEXT_WEEK_AFTER.match(text[m.end():m.end() + 40])
        mode = 'tail' if tail else 'next' if (m.group(1) or '').lower().startswith(('следующ', 'next')) else 'plain'

        def resolve(b, wd=weekday, mode=mode):
            ahead, assumed = (wd - b.weekday()) % 7, []
            if mode != 'plain':
                # "next Thursday" and "Thursday next week" are that weekday of the next calendar week (weeks start on Monday).
                ahead, assumed = 7 - b.weekday() + wd, (['next_week'] if mode == 'next' else [])
            elif ahead == 0:ahead, assumed = 7, ['next_week']
            return _shift(b, ahead), None, assumed
        out.append((m.start(), m.end() + (tail.end() if tail else 0), 'day', 'day:weekday', resolve))
    for m in WEEK.finditer(text):
        if not free(m):continue
        nxt = (m.group(1) or m.group(2) or '').lower().startswith(('следующ', 'next'))

        def resolve(b, nxt=nxt):
            monday = _shift(b, 7 - b.weekday())
            # "This week" runs from the send day to Sunday, never before the message.
            return (monday, monday and _shift(monday, 7), []) if nxt else (b, monday, [])
        out.append((m.start(), m.end(), 'week', 'day:week', resolve))
    found = []
    for m in RU_DATE.finditer(text):
        month = next((v for k, v in RU_MONTHS.items() if m.group(2).lower().startswith(k)), None)
        found.append((m, int(m.group(1)), month, None, 'day:date'))
    for m in EN_DATE.finditer(text):
        word, day = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        found.append((m, int(day), EN_MONTHS.get(word[:3].lower()), None, 'day:date'))
    for m in NUMERIC.finditer(text):
        if VERSION_BEFORE.search(text[max(0, m.start() - 12):m.start()]):continue
        year = m.group(3)
        found.append((m, int(m.group(1)), int(m.group(2)), (int(year) if len(year) == 4 else 2000 + int(year)) if year else None,
            'day:numeric'))
    for m, day, month, year, rule in found:
        if month is None or not free(m):continue
        try:date(2000, month, day)  # a leap year: the day exists in some year
        except ValueError:continue

        def resolve(b, day=day, month=month, year=year):
            if year is None:
                # A November or December message that names January or February means next year.
                year = b.year + 1 if (month, day) < (b.month, b.day) and b.month >= 11 and month <= 2 else b.year
            elif not b.year <= year <= b.year + 1:return None
            try:value = date(year, month, day)
            except ValueError:return None
            return (value, None, []) if value >= b else None
        out.append((m.start(), m.end(), 'day', rule, resolve))
    for m in ORDINAL.finditer(text):
        if not free(m) or not 1 <= int(m.group(1)) <= 31:continue

        def resolve(b, n=int(m.group(1))):
            try:value = b.replace(day=n)
            except ValueError:return None
            return (value, None, []) if value >= b else None
        out.append((m.start(), m.end(), 'day', 'day:ordinal', resolve))
    return sorted(out, key=lambda d: (d[0], d[1]))


def _clocks(text, taken):
    """Times of day: (start, end, from_minutes, to_minutes or None, granularity, rule, assumed)."""
    out, spans = [], list(taken)

    def free(m):return not any(s < m.end() and m.start() < e for s, e in spans)

    def add(item):
        out.append(item)
        spans.append(item[:2])
    for m in RANGE.finditer(text):
        g = m.groups()
        if g[0] is not None:h1, m1, h2, m2, mark = int(g[0]), int(g[1] or 0), int(g[2]), int(g[3] or 0), g[4] or g[5]
        elif g[6] is not None:h1, m1, h2, m2, mark = int(g[6]), int(g[7] or 0), int(g[8]), int(g[9] or 0), g[10]
        else:h1, m1, h2, m2, mark = int(g[11]), int(g[12]), int(g[13]), int(g[14]), None
        bare = not mark and g[11] is None
        a, assumed_a = _hour(h1, mark if h1 <= h2 or not mark else None, bare=bare)
        b, assumed_b = _hour(h2, mark, bare=bare)
        if a is None or b is None or m1 > 59 or m2 > 59 or not free(m):continue
        if b * 60 + m2 <= a * 60 + m1:continue
        add((m.start(), m.end(), a * 60 + m1, b * 60 + m2, 'range', 'time:range', sorted(set(assumed_a + assumed_b))))
    for m in CLOCK.finditer(text):
        g = m.groups()
        if g[0] is not None:h, mins, mark, bare = int(g[0]), int(g[1]), g[2] or g[3], False
        elif g[4] is not None:h, mins, mark, bare = int(g[4]), int(g[5] or 0), g[6] or g[7], g[5] is None
        else:h, mins, mark, bare = int(g[8]), 0, g[9], False
        hour, assumed = _hour(h, mark, bare=bare and not mark)
        if hour is None or mins > 59 or not free(m):continue
        add((m.start(), m.end(), hour * 60 + mins, None, 'time', 'time:clock', assumed))
    for rx, (a, b) in PARTS:
        for m in rx.finditer(text):
            if free(m):add((m.start(), m.end(), a * 60, b * 60, 'part_of_day', 'time:part_of_day', []))
    return sorted(out)


class _Bounds:
    """Sentence or clause bounds from break spans, found once per message (bisect, so many times stay linear)."""

    def __init__(self, text, breaks, word_opens=False):
        self.text, self.breaks = text, sorted(breaks)
        self.ends = [e for _, e, _ in self.breaks]
        self.starts = [s for s, _, _ in self.breaks]
        self.word_opens = word_opens

    def index(self, pos):return bisect_right(self.ends, pos)

    def bounds(self, pos):
        i = self.index(pos)
        start = 0
        if i:
            s, e, word = self.breaks[i - 1]
            start = s if self.word_opens and word else e
        j = bisect_right(self.starts, pos)
        return start, (self.starts[j] if j < len(self.starts) else len(self.text))


def _breaks(rx, text):
    return [(m.start(), m.end(), m.group(0)[:1].isalpha()) for m in rx.finditer(text)]


def _meeting_host(url):
    """The allowed meeting host of one whole link, from its parsed hostname (userinfo, port and query never count)."""
    try:host = (urlsplit(url).hostname or '').rstrip('.')
    except ValueError:return None
    return next((h for h in LINK_HOSTS if host == h or host.endswith('.' + h)), None)


def _first(rx, text):
    m = rx.search(text)
    return m.span() if m else None


def _local(dt, tz):
    try:return dt.astimezone(tz).date()
    except (OverflowError, ValueError):return None


def signals(text, sent_at, direction, zone):
    """One message's scheduling signal. `zone` is `time_references.owner_zone(...)`: (tzinfo, label).

    `direction` ('in' or 'out') is part of the contract for ANYM-893 and does not change a single message's reading.
    """
    raw = (text or '')[:MAX_CHARS]
    # Quoted or forwarded lines are someone else's words: blank them, keeping offsets.
    work = re.sub(r'(?m)^[ \t]*(?:>|»).*$', lambda m: ' ' * len(m.group(0)), raw)
    # A link's path and query are never read: blanked, keeping offsets, and each link ends a sentence.
    links = [m.span() for m in ANY_LINK.finditer(work)]
    hosts = sorted({h for h in (_meeting_host(work[s:e]) for s, e in links) if h})
    for s, e in links:work = work[:s] + ' ' * (e - s) + work[e:]
    owner_tz, label = zone
    owner_source = 'utc_fallback' if label == 'UTC_fallback' else 'owner'
    medium = 'video_call' if hosts or VIDEO.search(work) else 'call' if CALL.search(work) else 'meeting' if MEETING.search(work) else 'unknown'
    rules = ['medium:' + medium] if medium != 'unknown' else []
    result = {'intent': 'none', 'medium': medium, 'times': [], 'cue_span': None, 'links': hosts, 'rules': rules,
        'confidence': 'low'}
    if TEMPLATE.search(work):
        result['rules'] = rules + ['template']
        return result
    if not work.strip(' \t\n.,!?'):
        if hosts:result['rules'] = rules + ['link_only']
        return result
    cues = {intent: _first(rx, work) for intent, rx in CUES.items()}
    sentences = _Bounds(work, _breaks(SENTENCE_END, work) + [(s, e, False) for s, e in links])
    clauses = _Bounds(work, _breaks(CLAUSE_BREAK, work) + [(s, e, False) for s, e in links], word_opens=True)
    zones = _zones(work)

    def zone_for(span):
        """The explicit marker nearest the time, in its sentence first; else the owner zone."""
        if not zones:return owner_tz, owner_source
        here = sentences.index(span[0])

        def gap(z):return sentences.index(z[0]) != here, max(0, z[0] - span[1], span[0] - z[1]), z[0]
        return min(zones, key=gap)[2], 'explicit'
    days = _days(work)
    clocks = _clocks(work, [(d[0], d[1]) for d in days])
    by_sentence = {}
    for d in days:by_sentence.setdefault(sentences.index(d[0]), []).append(d)
    items, used = [], set()
    for c in clocks:
        same = by_sentence.get(sentences.index(c[0]), [])
        day = min(same, key=lambda d: min(abs(d[0] - c[1]), abs(c[0] - d[1])), default=None)
        # A part of day inside a whole week ("next week in the evening") names no single slot: the week stands alone.
        if day is not None and day[2] == 'week':continue
        if day is not None:used.add(day[0])
        span = (c[0], c[1]) if day is None else (min(day[0], c[0]), max(day[1], c[1]))
        items.append((span, c, day))
    items += [((d[0], d[1]), None, d) for d in days if d[0] not in used]
    resolved = []
    for span, c, day in items:
        # A zone marker binds to the clock it follows or precedes, not to the day the clock was paired with.
        tz, zone_source = zone_for(span if c is None else c[:2])
        base = _local(sent_at, tz)
        got = (base, None, ['same_day']) if day is None else (day[4](base) if base else None)
        if base is None or got is None or got[0] is None:continue
        first, last, assumed = got[0], got[1], list(got[2])
        try:
            if c is None:
                start = datetime.combine(first, time(), tz)
                end = datetime.combine(last if day[2] == 'week' else first + timedelta(days=1), time(), tz)
                granularity, rule_ids = day[2], [day[3]]
            else:
                start = datetime.combine(first, time(), tz) + timedelta(minutes=c[2])
                assumed += c[6]
                if c[3] is not None:end = datetime.combine(first, time(), tz) + timedelta(minutes=c[3])
                else:
                    end = start + timedelta(minutes=30 if medium in ('call', 'video_call') else 60)
                    assumed.append('default_duration')
                granularity, rule_ids = c[4], [r for r in (day and day[3], c[5]) if r]
        except (OverflowError, ValueError, TypeError):continue
        resolved.append((span, c, start, end, granularity, rule_ids, assumed, tz, zone_source, day))
    # A clock inside a part of day on the same day reference ("tomorrow morning at 10") makes the part redundant;
    # alternatives ("tomorrow morning or tomorrow at 12", «утром или в 10») stay.
    def redundant(r):
        return r[4] == 'part_of_day' and any(o[4] == 'time' and o[9] is r[9] and r[2] <= o[2] < r[3]
            and (r[9] is not None or sentences.index(o[1][0]) == sentences.index(r[1][0]))
            and not OR_WORD.search(work[min(o[1][1], r[1][1]):max(o[1][0], r[1][0])]) for o in resolved)
    resolved = [r for r in resolved if not redundant(r)]
    past = {}
    final = []
    for span, c, start, end, granularity, rule_ids, assumed, tz, zone_source, _ in sorted(resolved, key=lambda r: r[0]):
        here = sentences.index(span[0])
        if here not in past:past[here] = bool(PAST_CLAUSE.search(work[slice(*sentences.bounds(span[0]))]))
        if past[here] and not cues['reschedule']:continue
        # The clause of the clock itself, not of the day it borrowed ("cancel tomorrow at 10, let's do at 11").
        clause = work[slice(*clauses.bounds(c[0] if c else span[0]))]
        polarity = 'rejected' if CUES['decline'].search(clause) or CUES['cancel'].search(clause) else 'offered'
        final.append({'start': start.isoformat(), 'end': end.isoformat(), 'granularity': granularity,
            'zone': getattr(tz, 'key', None) or 'UTC', 'zone_source': zone_source, 'assumed': sorted(set(assumed)),
            'span': list(span), 'rule': '+'.join(rule_ids), 'polarity': polarity})
    intent, cue = _intent(work, cues, final, medium, clauses)
    result.update({'intent': intent, 'times': final, 'cue_span': list(cue) if cue else None})
    result['rules'] = rules + sorted({t['rule'] for t in final}) + ([f'intent:{intent}'] if intent != 'none' else []) + (
        ['zone:explicit'] if any(t['zone_source'] == 'explicit' for t in final) else [])
    has_cue = cue is not None
    if intent != 'none' and has_cue and final and medium != 'unknown':result['confidence'] = 'high'
    elif intent != 'none' and has_cue and (final or medium != 'unknown'):result['confidence'] = 'medium'
    return result


def _intent(text, cues, times, medium, clauses):
    """The winning move and its cue span. A scheduling move needs a time or a medium, except accept and decline
    replies, which stand alone ("ок", "не могу"), and a rescheduling request that is its whole clause ("перенесём?")."""
    found = {k: v for k, v in cues.items() if v}
    # An accept word that opens the message ("ок, среда 10:00") outranks a proposal word later in it.
    if 'accept' in found and found['accept'][0] <= 12 and not any(k in found for k in ('cancel', 'reschedule', 'decline')):
        return 'accept', found['accept']
    resched = found.get('reschedule')
    alone = bool(resched) and RESCHEDULE_ALONE.fullmatch(text[slice(*clauses.bounds(resched[0]))].strip(' \t?!.…,')) is not None
    for intent in ('cancel', 'reschedule', 'decline'):
        if intent in found and (times or medium != 'unknown' or intent == 'decline' or (intent == 'reschedule' and alone)):
            return intent, found[intent]
    if 'ask_availability' in found and (times or medium != 'unknown'):return 'ask_availability', found['ask_availability']
    if 'propose' in found and (times or medium != 'unknown'):return 'propose', found['propose']
    if 'accept' in found:return 'accept', found['accept']
    if times and medium != 'unknown':return 'propose', None
    return 'none', None
