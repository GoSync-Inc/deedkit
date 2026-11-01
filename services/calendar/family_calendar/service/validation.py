from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from family_calendar.errors import CalendarError
from .store import instant
from .windows import wall_time, window


LIMITS = [("title", 256), ("location", 512), ("description", 4000)]
DATE_VALUE = re.compile(r"\d{4}-\d{2}-\d{2}")


def invalid(path, *problems):
    """invalid_event naming each field at fault: (field, reason) pairs relative to `path`."""
    return CalendarError("invalid_event", [{"field": f"{path}/{f}", "reason": r} for f, r in problems])


def day(value, path, field):
    if not isinstance(value, str) or not DATE_VALUE.fullmatch(value):
        raise invalid(path, (field, "date"))
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise invalid(path, (field, "date")) from None


def validate_all_day(event, path):
    """Date range with an exclusive end; a missing end is one day. No time or timezone."""
    if "timezone" in event:
        raise invalid(path, ("timezone", "not_allowed_for_all_day"))
    if "start" not in event:
        raise invalid(path, ("start", "required"))
    a = day(event["start"], path, "start")
    try:
        b = day(event["end"], path, "end") if "end" in event else a + timedelta(days=1)
    except OverflowError:
        # The last supported date has no following day.
        raise CalendarError("invalid_window") from None
    if not a < b or b - a > timedelta(days=31):
        raise CalendarError("invalid_window")
    return dict(event, all_day=True, start=a.isoformat(), end=b.isoformat())


def validate_timed(event, path):
    missing = [(f, "required") for f in ("start", "end", "timezone") if f not in event]
    if missing:
        raise invalid(path, *missing)
    try:
        zone = ZoneInfo(event["timezone"])
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise CalendarError("invalid_timezone") from None
    a, b = window(wall_time(event["start"], zone), wall_time(event["end"], zone))
    return {k: v for k, v in event.items() if k != "all_day"} | {"start": a.isoformat(), "end": b.isoformat()}


def validate_event(event, path="event"):
    allowed = {"title", "start", "end", "timezone", "location", "description", "all_day"}
    if not isinstance(event, dict):
        raise CalendarError("invalid_event")
    if set(event) - allowed:
        raise invalid(path, *((f, "unknown") for f in sorted(set(event) - allowed)))
    if not isinstance(event.get("all_day", False), bool):
        raise invalid(path, ("all_day", "type"))
    result = validate_all_day(event, path) if event.get("all_day") else validate_timed(event, path)
    for k, limit in LIMITS:
        if k in event and (not isinstance(event[k], str) or len(event[k]) > limit):
            raise invalid(path, (k, "type" if not isinstance(event[k], str) else "max_length"))
    return result


def event_span(event, zone_name):
    """Instants an event occupies; an all-day event spans whole days of its calendar's own timezone."""
    if not event.get("all_day"):
        return instant(event["start"]), instant(event["end"])
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise CalendarError("invalid_timezone") from None
    try:
        return tuple(
            datetime.combine(date.fromisoformat(event[k]), time.min, zone).astimezone(timezone.utc)
            for k in ("start", "end")
        )
    except OverflowError:
        # A boundary date whose midnight does not exist in UTC.
        raise CalendarError("invalid_window") from None
