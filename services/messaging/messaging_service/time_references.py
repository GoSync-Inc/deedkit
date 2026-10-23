"""ANYM-282: an awaiting-reply message that names a date which has already passed.

A small allowlist (ru, en, id) instead of a date library: a phrase either matches one of these
patterns and resolves to one date, or it yields nothing; nothing is guessed.
"""
import re
import unicodedata
from datetime import date,timedelta,timezone
from zoneinfo import ZoneInfo

UTC_FALLBACK=(timezone.utc,'UTC_fallback')
WEEKDAYS={'пон':0,'вто':1,'сре':2,'чет':3,'пят':4,'суб':5,'вос':6,
    'mon':0,'tue':1,'wed':2,'thu':3,'fri':4,'sat':5,'sun':6,
    'sen':0,'sel':1,'rab':2,'kam':3,'jum':4,'sab':5,'min':6}
RU_DAY=r'понедельник[аиу]?|вторник[аиу]?|сред[уаые]|четверг[аиу]?|пятниц[уаые]|суббот[уаые]|воскресень[еяю]'
EN_DAY=r'monday|tuesday|wednesday|thursday|friday|saturday|sunday'
ID_DAY=r"senin|selasa|rabu|kamis|jum'?at|sabtu|minggu"
RELATIVE=[(r'послезавтра|day after tomorrow|lusa',2),(r'завтра|tomorrow|besok',1),(r'сегодня|today|hari ini',0)]
WEEKDAY=[rf'\b(?:во?|ко?|до|на)\s+({RU_DAY})\b',rf'\b(?:on|by|this|until|before)\s+({EN_DAY})\b',
    rf'\bhari\s+({ID_DAY})\b',rf'\b({ID_DAY})\s+ini\b']
# "в пятницу на прошлой неделе", "on Friday last week", "hari Jumat lalu" name a past day, not a promise.
# Only within the weekday's own sentence: any separator (spaces, line breaks, brackets, dashes), never a sentence end; NFKC first folds
# full-width and small forms (． ﹖ ！ ‼) into . ? ! so one short list covers them ("…on Friday? Last week was busy").
CLAUSE=r'[^\w.!?…‽。｡؟۔।॥።]*'
PAST_BEFORE=re.compile(r'(?:прошл\w*(?:\s+недел\w*)?|last(?:\s+week)?|lalu)'+CLAUSE+'$',re.I)
PAST_AFTER=re.compile('^'+CLAUSE+r'(?:(?:на\s+)?прошл\w*|(?:of\s+)?last\b|(?:minggu\s+)?lalu\b|kemarin\b)',re.I)
# A numeric date counts only with a year or a word that introduces a date ("версия 5.10" is not one).
NUMERIC=re.compile(r'(?<![\d.,/])(?:\b(до|к|на|by|on|until|pada|tanggal|tgl)\s+)?(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?(?![\d.,/]?\d)',re.I)


def owner_zone(environ):
    """The owner's IANA zone from ANYMSG_OWNER_TIMEZONE; unset or unknown means UTC, labelled as a fallback."""
    name=environ.get('ANYMSG_OWNER_TIMEZONE') or ''
    try:return (ZoneInfo(name),name) if name else UTC_FALLBACK
    except (ValueError,OSError,KeyError):return UTC_FALLBACK


def _references(text,base):
    for pattern,days in RELATIVE:
        for m in re.finditer(rf'\b(?:{pattern})\b',text,re.I):yield m.group(0),base+timedelta(days=days)
    for pattern in WEEKDAY:
        for m in re.finditer(pattern,text,re.I):
            weekday=WEEKDAYS.get(m.group(1)[:3].lower())
            # Case-insensitive matching also accepts look-alike letters ("Frİday"); those name no known day.
            before=unicodedata.normalize('NFKC',text[max(0,m.start()-24):m.start()])
            after=unicodedata.normalize('NFKC',text[m.end():m.end()+24])
            if weekday is None or PAST_BEFORE.search(before) or PAST_AFTER.search(after):continue
            ahead=(weekday-base.weekday())%7
            # The send day's own weekday is today or next week: a guess, so nothing.
            if ahead:yield m.group(0),base+timedelta(days=ahead)
    for m in NUMERIC.finditer(text):
        word,day,month,year=m.groups()
        if not word and not year:continue
        day,month=int(day),int(month)
        if year:year=int(year) if len(year)==4 else 2000+int(year)
        # A January or February date written in November or December is next year's (chosen before validating 29.02).
        else:year=base.year+1 if (month,day)<(base.month,base.day) and base.month>=11 and month<=2 else base.year
        try:found=date(year,month,day)
        except ValueError:continue
        if found>=base:yield m.group(0),found


def passed_reference(text,sent_at,now,zone):
    """The flag for a message whose latest named date is before today in the owner's zone, else None."""
    if not text:return None
    tz,label=zone
    # A heuristic hint: any surprise here drops the flag, never the queue read.
    try:found=max(_references(text,sent_at.astimezone(tz).date()),key=lambda ref:ref[1],default=None)
    except Exception:return None  # noqa: BLE001 - one odd message must never fail the queue page
    if found is None or found[1]>=now.astimezone(tz).date():return None
    return {'text':found[0][:40],'resolved_date':found[1].isoformat(),'status':'passed','time_zone':label,
        'suggest':'acknowledge_delay_offer_new_date'}
