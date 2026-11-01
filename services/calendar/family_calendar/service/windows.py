from datetime import datetime, timedelta, timezone
import re

from family_calendar.errors import CalendarError
from .store import instant


def window(start, end):
    a, b = instant(start), instant(end)
    if not a < b or b - a > timedelta(days=31):
        raise CalendarError("invalid_window")
    return a, b


NAMED_WINDOWS = ("today", "tomorrow", "this_week", "next_week")
QUARTER = 900


def find_window(value, zone, now):
    """calendar_find_time's window (ANYM-553) as (from, to, step): a named window in the family timezone (weeks run
    Monday to Monday; today and this_week start at the next quarter hour), or ISO {from, to} whose offset-free times
    are wall time in that zone. step is the window's length in whole days: the widening of an empty answer."""
    if isinstance(value, str):
        local = now.astimezone(zone)
        day = datetime(local.year, local.month, local.day, tzinfo=zone)
        monday = day - timedelta(days=local.weekday())
        a, b, step = {
            "today": (day, day + timedelta(days=1), 1),
            "tomorrow": (day + timedelta(days=1), day + timedelta(days=2), 1),
            "this_week": (monday, monday + timedelta(days=7), 7),
            "next_week": (monday + timedelta(days=7), monday + timedelta(days=14), 7),
        }[value]
        soon = datetime.fromtimestamp(-(-now.timestamp() // QUARTER) * QUARTER, timezone.utc)
        return max(a, soon.astimezone(zone)), b, step
    a, b = (local if isinstance(local := wall_time(value[k], zone), datetime) else instant(local)
            for k in ("from", "to"))
    window(a, b)
    return a.astimezone(zone), b.astimezone(zone), max(1, -(-(b - a) // timedelta(days=1)))


def widened(a, b, step):
    """One step wider: `to` later by step days, inside the 31-day limit; a window at the limit moves on instead.
    The limit counts elapsed time, as window() does: a DST change makes a wall-clock day 23 or 25 hours."""
    def limit(start):
        return (start.astimezone(timezone.utc) + timedelta(days=31)).astimezone(start.tzinfo)

    end = min(b + timedelta(days=step), limit(a))
    c, d = (a, end) if end > b else (b, min(b + timedelta(days=step), limit(b)))
    window(c, d)
    return c, d


LOCAL_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?")


def wall_time(value, zone):
    """Resolve an offset-free event time in its own zone; gaps and folds fail explicitly."""
    if not isinstance(value, str) or not LOCAL_TIME.fullmatch(value):
        return value
    try:
        t = datetime.fromisoformat(value).replace(tzinfo=zone)
    except ValueError:
        raise CalendarError("invalid_time") from None
    if t.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != t.replace(tzinfo=None):
        raise CalendarError("invalid_local_time")
    if t.replace(fold=0).utcoffset() != t.replace(fold=1).utcoffset():
        raise CalendarError("ambiguous_local_time")
    return t
