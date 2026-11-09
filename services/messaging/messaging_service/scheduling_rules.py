"""ANYM-892: fixed ru/en rules that read a message's scheduling move, medium and times.

Pure: no database, network or clock. A phrase either matches one of these rules or yields nothing; every
assumption a rule makes is named in the result's `assumed` list (pm, next_week, default_duration, same_day).
Chat text is data: nothing in it is ever executed.
"""
import re
import unicodedata
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .time_references import EN_DAY, PAST_AFTER, PAST_BEFORE, RU_DAY, WEEKDAYS

MAX_CHARS = 4096
INTENTS = ('ask_availability', 'propose', 'accept', 'decline', 'cancel', 'reschedule', 'none')
MEDIA = ('call', 'video_call', 'meeting', 'unknown')
LINK_HOSTS = ('zoom.us', 'meet.google.com', 'teams.microsoft.com', 'telemost.yandex.ru')
LINK = re.compile(r'https?://(?:[\w-]+\.)*(' + '|'.join(re.escape(h) for h in LINK_HOSTS) + r')\S*', re.I)
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
# A clause opener: «лучше в среду» or "how about Wednesday" starts a new clause after a decline.
# A dot or comma between digits ("12.11", "15.00", "12,5") is part of a number, never a break.
SENTENCE_END = re.compile(r'[.!?](?!\d)|\n')
CLAUSE_BREAK = re.compile(r'[.;!?](?!\d)|\n|,(?!\d)|\b(?:лучше|давай\w*|но|зато|а\s+вот|but|how\s+about|what\s+about|instead|maybe)\b', re.I)
PAST_CLAUSE = _rx(r'\bвчера\b', r'\bпозавчера\b', r'\bпрошл\w*', r'\bназад\b', r'\byesterday\b', r'\blast\b', r'\bago\b',
    r'\bбыла\b', r'\bбыло\b', r'\bбыл\b', r'\bвстречались\b', r'\bсозванивались\b', r'\bwas\b', r'\bwere\b', r'\bmet\b',
    r'\bhad\b')
VERSION_BEFORE = re.compile(r'(?:верси\w*|version|ver\.?|v|релиз\w*|release|build|сборк\w*)\s*$', re.I)

# Explicit zones. Abbreviations that are also words ("wet", "et", "pt") count only in capitals.
# City words are a fixed table of at most 40 entries (ru dative "по <городу>", en "<City> time").
ZONE_WORDS = [(re.compile(r'\bмск\b|\bmsk\b|\bпо\s+москве\b|\bпо\s+московскому\b|\bmoscow\s+time\b', re.I), 'Europe/Moscow'),
    (re.compile(r'\bcest\b|\bcet\b', re.I), 'CET'), (re.compile(r'\beest\b|\beet\b', re.I), 'EET'),
    (re.compile(r'\bWES?T\b'), 'WET'), (re.compile(r'\bbst\b', re.I), 'Europe/London'),
    (re.compile(r'\b(?:ET|EST|EDT)\b'), 'America/New_York'), (re.compile(r'\b(?:PT|PST|PDT)\b'), 'America/Los_Angeles')]
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
RELATIVE = [(re.compile(r'\bпослезавтра\b|\bday\s+after\s+tomorrow\b', re.I), 2), (re.compile(r'\bзавтра\b|\btomorrow\b', re.I), 1),
    (re.compile(r'\bсегодня\b|\btoday\b', re.I), 0)]
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


def _zone_of(text):
    """(ZoneInfo, matched span) for an explicit zone marker, or None."""
    for m in UTC_OFFSET.finditer(text):
        if m.group(3):return ZoneInfo('UTC'), m.span()
        hours = int(m.group(2))
        east = m.group(1) == '+'
        if hours <= (14 if east else 12):  # real offsets run from UTC-12 to UTC+14
            sign = '-' if east else '+'  # Etc/GMT names invert the sign
            return ZoneInfo('UTC' if hours == 0 else f'Etc/GMT{sign}{hours}'), m.span()
    for rx, name in ZONE_WORDS:
        m = rx.search(text)
        if m:return ZoneInfo(name), m.span()
    for rx in (CITY_RU, CITY_EN):
        m = rx.search(text)
        if m:
            key = m.group(1).lower()
            return ZoneInfo(next(v for k, v in CITIES.items() if key.startswith(k) or k.startswith(key))), m.span()
    return None


def _hour(h, mark, *, bare):
    """Hour on a 24 h clock and the assumption it needed; None for an impossible hour."""
    mark = (mark or '').lower().replace('.', '')
    if h > 24:return None, []
    if mark in ('pm', 'дня', 'вечера'):return (h + 12 if h < 12 else h), []
    if mark in ('am', 'утра', 'ночи'):return (0 if h == 12 else h), []
    if bare and 1 <= h <= 6:return h + 12, ['pm']
    return (0 if h == 24 else h), []


def _days(text, base):
    """Day references: (start, end, date, granularity, rule, assumed)."""
    out = []
    for rx, days in RELATIVE:
        for m in rx.finditer(text):
            if not any(s <= m.start() < e for s, e, *_ in out):out.append((m.start(), m.end(), base + timedelta(days=days), 'day', 'day:relative', []))
    for m in WEEKDAY.finditer(text):
        weekday = WEEKDAYS.get(m.group(2)[:3].lower())
        before = unicodedata.normalize('NFKC', text[max(0, m.start() - 24):m.start()])
        after = unicodedata.normalize('NFKC', text[m.end():m.end() + 24])
        if weekday is None or PAST_BEFORE.search(before) or PAST_AFTER.search(after):continue
        ahead = (weekday - base.weekday()) % 7
        assumed, end = [], m.end()
        tail = NEXT_WEEK_AFTER.match(text[m.end():m.end() + 40])
        if tail:ahead, end = 7 - base.weekday() + weekday, m.end() + tail.end()
        elif (m.group(1) or '').lower().startswith(('следующ', 'next')):
            # "next Thursday" is that weekday of the next calendar week (weeks start on Monday).
            ahead = 7 - base.weekday() + weekday
            assumed = ['next_week']
        elif ahead == 0:
            ahead, assumed = 7, ['next_week']
        out.append((m.start(), end, base + timedelta(days=ahead), 'day', 'day:weekday', assumed))
    for m in WEEK.finditer(text):
        if any(s <= m.start() < e for s, e, *_ in out):continue
        nxt = (m.group(1) or m.group(2) or '').lower().startswith(('следующ', 'next'))
        monday = base - timedelta(days=base.weekday()) + timedelta(days=7)
        # "This week" runs from the send day to Sunday, never before the message.
        out.append((m.start(), m.end(), monday if nxt else base, 'week', 'day:week', [], monday + timedelta(days=7 if nxt else 0)))
    found = []
    for m in RU_DATE.finditer(text):
        month = next(v for k, v in RU_MONTHS.items() if m.group(2).lower().startswith(k))
        found.append((m, int(m.group(1)), month, None, 'day:date'))
    for m in EN_DATE.finditer(text):
        word, day = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        found.append((m, int(day), EN_MONTHS[word[:3].lower()], None, 'day:date'))
    for m in NUMERIC.finditer(text):
        if VERSION_BEFORE.search(text[max(0, m.start() - 12):m.start()]):continue
        year = m.group(3)
        found.append((m, int(m.group(1)), int(m.group(2)), (int(year) if len(year) == 4 else 2000 + int(year)) if year else None,
            'day:numeric'))
    for m, day, month, year, rule in found:
        if any(s <= m.start() < e for s, e, *_ in out):continue
        try:value = date(year or base.year, month, day)
        except ValueError:continue
        if year is None and value < base and base.month >= 11 and month <= 2:value = value.replace(year=base.year + 1)
        if value >= base:out.append((m.start(), m.end(), value, 'day', rule, []))
    for m in ORDINAL.finditer(text):
        if any(s <= m.start() < e for s, e, *_ in out):continue
        try:value = base.replace(day=int(m.group(1)))
        except ValueError:continue
        if value >= base:out.append((m.start(), m.end(), value, 'day', 'day:ordinal', []))
    return sorted(out)


def _clocks(text, taken):
    """Times of day: (start, end, from_minutes, to_minutes or None, granularity, rule, assumed)."""
    out = []

    def free(m):return not any(s < m.end() and m.start() < e for s, e in taken + [(o[0], o[1]) for o in out])
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
        out.append((m.start(), m.end(), a * 60 + m1, b * 60 + m2, 'range', 'time:range', sorted(set(assumed_a + assumed_b))))
    for m in CLOCK.finditer(text):
        g = m.groups()
        if g[0] is not None:h, mins, mark, bare = int(g[0]), int(g[1]), g[2] or g[3], False
        elif g[4] is not None:h, mins, mark, bare = int(g[4]), int(g[5] or 0), g[6] or g[7], g[5] is None
        else:h, mins, mark, bare = int(g[8]), 0, g[9], False
        hour, assumed = _hour(h, mark, bare=bare and not mark)
        if hour is None or mins > 59 or not free(m):continue
        out.append((m.start(), m.end(), hour * 60 + mins, None, 'time', 'time:clock', assumed))
    for rx, (a, b) in PARTS:
        for m in rx.finditer(text):
            if free(m):out.append((m.start(), m.end(), a * 60, b * 60, 'part_of_day', 'time:part_of_day', []))
    return sorted(out)


def _clause_bounds(text, pos):
    start, end = 0, len(text)
    for m in CLAUSE_BREAK.finditer(text):
        if m.end() <= pos:start = m.start() if m.group(0)[0].isalpha() else m.end()
        elif m.start() > pos:
            end = m.start()
            break
    return start, end


def _sentence(text, pos):
    start, end = 0, len(text)
    for m in SENTENCE_END.finditer(text):
        if m.end() <= pos:start = m.end()
        else:
            end = m.start()
            break
    return start, end


def _first(rx, text):
    m = rx.search(text)
    return m.span() if m else None


def signals(text, sent_at, direction, zone):
    """One message's scheduling signal. `zone` is `time_references.owner_zone(...)`: (tzinfo, label)."""
    raw = (text or '')[:MAX_CHARS]
    # Quoted or forwarded lines are someone else's words: blank them, keeping offsets.
    work = re.sub(r'(?m)^[ \t]*(?:>|»).*$', lambda m: ' ' * len(m.group(0)), raw)
    owner_tz, label = zone
    explicit = _zone_of(work)
    tz, zone_source = (explicit[0], 'explicit') if explicit else (owner_tz, 'utc_fallback' if label == 'UTC_fallback' else 'owner')
    zone_name = getattr(tz, 'key', None) or 'UTC'
    hosts = sorted({m.group(1).lower() for m in LINK.finditer(work)})
    stripped = ANY_LINK.sub('', work).strip(' \t\n.,!?')
    medium = 'video_call' if hosts or VIDEO.search(work) else 'call' if CALL.search(work) else 'meeting' if MEETING.search(work) else 'unknown'
    rules = ['medium:' + medium] if medium != 'unknown' else []
    result = {'intent': 'none', 'medium': medium, 'times': [], 'cue_span': None, 'links': hosts, 'rules': rules,
        'confidence': 'low'}
    if not work.strip():return result
    if TEMPLATE.search(work):
        result['rules'] = rules + ['template']
        return result
    if hosts and not stripped:
        result['rules'] = rules + ['link_only']
        return result
    cues = {intent: _first(rx, work) for intent, rx in CUES.items()}
    base = sent_at.astimezone(tz).date()
    days = _days(work, base)
    clocks = _clocks(work, [(d[0], d[1]) for d in days])
    times = []
    used_days = set()
    for c in clocks:
        same = [d for d in days if _sentence(work, d[0]) == _sentence(work, c[0])]
        day = min(same, key=lambda d: min(abs(d[0] - c[1]), abs(c[0] - d[1])), default=None)
        assumed = list(c[6])
        # A part of day inside a whole week ("next week in the evening") names no single slot: the week stands alone.
        if day is not None and day[3] == 'week':continue
        if day is None:
            when, assumed = base, assumed + ['same_day']
            span = (c[0], c[1])
        else:
            used_days.add(day[0])
            when, assumed = day[2], assumed + day[5]
            span = (min(day[0], c[0]), max(day[1], c[1]))
        times.append((span, when, c, assumed, day[4] if day else None))
    # A clock inside a part of day ("tomorrow morning at 10") makes the part redundant.
    clocked = {t[1] for t in times if t[2][4] == 'time'}
    times = [t for t in times if not (t[2][4] == 'part_of_day' and t[1] in clocked and any(
        o[2][4] == 'time' and o[1] == t[1] and t[2][2] <= o[2][2] < t[2][3] for o in times))]
    out = []
    for span, when, c, assumed, day_rule in times:
        start = datetime.combine(when, time(), tz) + timedelta(minutes=c[2])
        if c[3] is not None:end = datetime.combine(when, time(), tz) + timedelta(minutes=c[3])
        else:
            end = start + timedelta(minutes=30 if medium in ('call', 'video_call') else 60)
            assumed = assumed + ['default_duration']
        out.append((span, start, end, c[4], [r for r in (day_rule, c[5]) if r], assumed))
    for d in days:
        if d[0] in used_days:continue
        start = datetime.combine(d[2], time(), tz)
        end = datetime.combine(d[6], time(), tz) if d[3] == 'week' else datetime.combine(d[2] + timedelta(days=1), time(), tz)
        out.append(((d[0], d[1]), start, end, d[3], [d[4]], list(d[5])))
    final = []
    for span, start, end, granularity, rule_ids, assumed in sorted(out, key=lambda o: o[0]):
        sentence = work[slice(*_sentence(work, span[0]))]
        if PAST_CLAUSE.search(sentence) and not cues['reschedule']:continue
        clause = work[slice(*_clause_bounds(work, span[0]))]
        polarity = 'rejected' if CUES['decline'].search(clause) else 'offered'
        final.append({'start': start.isoformat(), 'end': end.isoformat(), 'granularity': granularity, 'zone': zone_name,
            'zone_source': zone_source, 'assumed': sorted(set(assumed)), 'span': list(span), 'rule': '+'.join(rule_ids),
            'polarity': polarity})
    intent, cue = _intent(work, cues, final, medium)
    result.update({'intent': intent, 'times': final, 'cue_span': list(cue) if cue else None})
    result['rules'] = rules + sorted({t['rule'] for t in final}) + ([f'intent:{intent}'] if intent != 'none' else []) + (
        ['zone:explicit'] if explicit else [])
    has_cue = cue is not None
    if intent != 'none' and has_cue and final and medium != 'unknown':result['confidence'] = 'high'
    elif intent != 'none' and has_cue and (final or medium != 'unknown'):result['confidence'] = 'medium'
    return result


def _intent(text, cues, times, medium):
    """The winning move and its cue span. A scheduling move needs a time or a medium, except accept and decline
    replies, which stand alone ("ок", "не могу")."""
    found = {k: v for k, v in cues.items() if v}
    # An accept word that opens the message ("ок, среда 10:00") outranks a proposal word later in it.
    if 'accept' in found and found['accept'][0] <= 12 and not any(k in found for k in ('cancel', 'reschedule', 'decline')):
        return 'accept', found['accept']
    for intent in ('cancel', 'reschedule', 'decline'):
        if intent in found and (times or medium != 'unknown' or intent == 'decline'):return intent, found[intent]
    if 'ask_availability' in found and (times or medium != 'unknown'):return 'ask_availability', found['ask_availability']
    if 'propose' in found and (times or medium != 'unknown'):return 'propose', found['propose']
    if 'accept' in found:return 'accept', found['accept']
    if times and medium != 'unknown':return 'propose', None
    return 'none', None
