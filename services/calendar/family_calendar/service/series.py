"""Recurring-series edits on Google (ANYM-556): all occurrences, or this and the following ones.

The provider's model is kept (Google Calendar API recurring events, RFC 5545 RRULE): a series is its master
event; an occurrence is named by its `originalStartTime`, never by where it starts now. `all` patches or deletes
the master. `following` ends the master just before the occurrence (UNTIL) and, for a move, first creates a
replacement series from that occurrence on. Times move by wall-clock time in the series' own timezone, so an
edit across a DST change keeps the local time. An exception (an occurrence changed or cancelled on its own)
whose fate the provider does not define for the edit refuses it before any write (`series_exceptions`);
exceptions outside the edited range are untouched. A time move previewed with a read of the series (ANYM-614)
carries its exceptions instead, each by one explicit write to its instance in the moved series (`carried`).
"""
from datetime import date, datetime, timedelta, timezone
from itertools import islice
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from family_calendar.errors import CalendarError
from .store import instant

SCOPES = ("this", "following", "all")
# Fields a replacement series copies from the original master; guests, conferences and attachments refuse.
# `status` too: a tentative series stays tentative (Google's default for a new event is confirmed).
COPIED = ("summary", "description", "location", "colorId", "transparency", "visibility", "reminders", "status")
UNCOPIABLE = ("attendees", "conferenceData", "attachments", "extendedProperties", "source")
# RRULE parts a date shift would contradict (the rule names days); time parts contradict any move. A monthly or
# yearly rule takes its day from DTSTART when it names none, and skips dates a month lacks: a date shift
# changes which dates exist (31 Jan monthly is not 1 Feb monthly), so it refuses too.
DATE_FREQS = ("MONTHLY", "YEARLY")
DAY_PARTS = ("BYDAY", "BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYMONTH", "BYSETPOS")
TIME_PARTS = ("BYHOUR", "BYMINUTE", "BYSECOND")
MAX_INSTANCES = 10_000
# What an exception keeps of its own through a time move (ANYM-614): the fields a replacement copies. Guests and the
# uncopiable fields refuse, as for the master; the times are its own only when it was moved on its own.
CARRIED = COPIED
MAX_CARRIED = 50


def rule_of(master):
    """The one RRULE of a master as ordered (name, value) parts, and the other recurrence lines (EXDATE only)."""
    lines = master.get("recurrence")
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        raise CalendarError("unsupported_recurrence")
    rules = [line for line in lines if line.upper().startswith("RRULE:")]
    others = [line for line in lines if not line.upper().startswith("RRULE:")]
    if len(rules) != 1 or any(not line.upper().startswith("EXDATE") for line in others):
        # RDATE and EXRULE are outside what an UNTIL can end; several rules are not one series shape.
        raise CalendarError("unsupported_recurrence")
    parts = []
    for part in rules[0][len("RRULE:"):].split(";"):
        name, sep, value = part.partition("=")
        if not sep or not name or not value:
            raise CalendarError("unsupported_recurrence")
        parts.append((name.upper(), value))
    names = [name for name, _ in parts]
    if len(set(names)) != len(names) or "FREQ" not in names or ("COUNT" in names and "UNTIL" in names):
        raise CalendarError("unsupported_recurrence")
    return parts, others


def rule_line(parts):
    return "RRULE:" + ";".join(f"{name}={value}" for name, value in parts)


def zone_of(master):
    """The zone the provider expands the series in; an all-day series has none (None)."""
    start = master.get("start") or {}
    if "date" in start and "dateTime" not in start:
        return None
    try:
        return ZoneInfo(start["timeZone"])
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        # Google requires timeZone on a recurring master; without it the expansion zone is a guess.
        raise CalendarError("unsupported_recurrence") from None


def wall(value, zone):
    """An instant as naive wall time in `zone`; a date stays a date."""
    if zone is None:
        return date.fromisoformat(value) if isinstance(value, str) else value
    return instant(value).astimezone(zone).replace(tzinfo=None)


def placed(local, zone):
    """Naive wall time in `zone` as an aware time; a DST gap or fold fails explicitly, before any write."""
    t = local.replace(tzinfo=zone)
    if t.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local:
        raise CalendarError("invalid_local_time")
    if t.replace(fold=0).utcoffset() != t.replace(fold=1).utcoffset():
        raise CalendarError("ambiguous_local_time")
    return t


def master_start(master, zone):
    start = master["start"]
    return wall(start["dateTime"], zone) if zone else date.fromisoformat(start["date"])


def boundary_of(occurrence, zone):
    """The occurrence's original start, as the series' wall time."""
    return wall(occurrence["original_start"], zone)


def original_of(event, zone):
    """An instance's originalStartTime as wall time in the series zone; None when it cannot be read."""
    value = event.get("originalStartTime") if isinstance(event, dict) else None
    if not isinstance(value, dict):
        return None
    try:
        if zone is None:
            return date.fromisoformat(value["date"]) if "date" in value else None
        return wall(value["dateTime"], zone) if "dateTime" in value else None
    except (CalendarError, ValueError, TypeError):
        return None


def check_exceptions(exceptions, zone, boundary, scope, cancel):
    """Refuse an edit that would leave an exception's fate to the provider: for a move every exception in the
    range (all: the whole series), for a following cancel every live exception from the boundary on. An exception
    with guests of its own in the range is an edit of an event with guests (capability_unverified)."""
    affected = []
    for e in exceptions:
        original = original_of(e, zone)
        if original is None:
            raise CalendarError("series_exceptions")
        if scope == "following" and original < boundary:
            continue
        affected.append(e)
    # Google lets an exception carry attendees the master lacks; cancelling it is a change of an event with guests.
    if any(e.get("attendees") for e in affected):
        raise CalendarError("capability_unverified")
    if scope == "all" and cancel:
        # Deleting the master deletes the whole series, its exceptions included.
        return
    if any(not (cancel and e.get("status") == "cancelled") for e in affected):
        raise CalendarError("series_exceptions")


def shift(occurrence, event, zone):
    """The wall-clock shift from the occurrence's original start to its requested start, and the new length."""
    if zone is None:
        if not event.get("all_day"):
            raise CalendarError("capability_unverified")
        start, end = date.fromisoformat(event["start"]), date.fromisoformat(event["end"])
        return start - boundary_of(occurrence, zone), end - start
    if event.get("all_day"):
        raise CalendarError("capability_unverified")
    a, b = instant(event["start"]), instant(event["end"])
    return wall(event["start"], zone) - boundary_of(occurrence, zone), b - a


def check_shift(parts, delta, first):
    """A rule that names hours or minutes cannot follow a moved start, one that names days only a move within
    the same date: every occurrence keeps the master's time of day, so its date moves exactly when the first's
    does."""
    names = {name for name, _ in parts}
    if names & set(TIME_PARTS):
        raise CalendarError("unsupported_recurrence")
    moved = first + delta
    freq = dict(parts).get("FREQ", "").upper()
    if (names & set(DAY_PARTS) or freq in DATE_FREQS) and (moved.date() if isinstance(moved, datetime) else moved) != (
            first.date() if isinstance(first, datetime) else first):
        raise CalendarError("unsupported_recurrence")


def times(start, length, zone):
    """Google start and end of a (possibly new) series start, in the series zone."""
    if zone is None:
        return {"date": start.isoformat()}, {"date": (start + length).isoformat()}
    a = placed(start, zone)
    b = (a.astimezone(timezone.utc) + length).astimezone(zone)
    return ({"dateTime": a.isoformat(), "timeZone": zone.key},
            {"dateTime": b.isoformat(), "timeZone": zone.key})


def until_text(value, zone):
    if zone is None:
        return value.strftime("%Y%m%d")
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def until_value(text, zone):
    """An RRULE UNTIL as wall time in the series zone (UTC form) or a date (all-day series)."""
    try:
        if zone is None:
            return datetime.strptime(text[:8], "%Y%m%d").date()
        if text.endswith("Z"):
            return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(zone).replace(
                tzinfo=None)
        if "T" in text:
            return datetime.strptime(text, "%Y%m%dT%H%M%S")
        return datetime.combine(datetime.strptime(text, "%Y%m%d").date(), datetime.max.time().replace(microsecond=0))
    except ValueError:
        raise CalendarError("unsupported_recurrence") from None


def before(parts, first, boundary):
    """How many rule instances start before `boundary` (the RRULE expanded in wall time from `first`)."""
    as_time = first if isinstance(first, datetime) else datetime.combine(first, datetime.min.time())
    stop = boundary if isinstance(boundary, datetime) else datetime.combine(boundary, datetime.min.time())
    plain = [(name, value) for name, value in parts if name != "UNTIL"]
    try:
        rule = rrulestr(rule_line(plain)[len("RRULE:"):], dtstart=as_time)
    except (ValueError, TypeError):
        raise CalendarError("unsupported_recurrence") from None
    count = 0
    for value in islice(rule, MAX_INSTANCES + 1):
        if value >= stop:
            return count
        count += 1
    raise CalendarError("expansion_limit")


def truncated(master, zone, boundary):
    """The master's recurrence ending just before `boundary`; None when the boundary is its first occurrence
    (nothing would remain: the series itself goes)."""
    parts, others = rule_of(master)
    if boundary <= master_start(master, zone):
        return None
    last = boundary - timedelta(days=1) if zone is None else placed(boundary, zone) - timedelta(seconds=1)
    kept = [(name, value) for name, value in parts if name not in ("COUNT", "UNTIL")]
    return [rule_line([*kept, ("UNTIL", until_text(last, zone))]), *others]


def moved_rule(parts, zone, delta, first, boundary=None):
    """The rule of a series whose start moves by `delta` (wall time): UNTIL moves with it; a replacement that
    starts at `boundary` counts only the instances it takes over."""
    out = []
    for name, value in parts:
        if name == "UNTIL":
            moved = until_value(value, zone) + delta
            value = until_text(moved if zone is None else placed(moved, zone), zone)
        elif name == "COUNT" and boundary is not None:
            try:
                total = int(value)
            except ValueError:
                raise CalendarError("unsupported_recurrence") from None
            left = total - before(parts, first, boundary)
            if left < 1:
                raise CalendarError("version_conflict")
            value = str(left)
        out.append((name, value))
    return rule_line(out)


def edit_all(master, occurrence, event, zone):
    """The patch that moves every occurrence of the series by the occurrence's wall-clock shift."""
    parts, others = rule_of(master)
    if others:
        # An EXDATE names original starts; moving the series would make it name nothing.
        raise CalendarError("series_exceptions")
    delta, length = shift(occurrence, event, zone)
    first = master_start(master, zone)
    check_shift(parts, delta, first)
    start, end = times(first + delta, length, zone)
    desired: dict = {"start": start, "end": end}
    if any(name == "UNTIL" for name, _ in parts):
        desired["recurrence"] = [moved_rule(parts, zone, delta, first)]
    return desired


def replacement(master, occurrence, event, zone, new_id):
    """A new series from the occurrence on, at its requested time: the master's own fields and rule."""
    if any(master.get(k) for k in UNCOPIABLE) or master.get("eventType", "default") != "default":
        raise CalendarError("capability_unverified")
    parts, others = rule_of(master)
    if others:
        raise CalendarError("series_exceptions")
    delta, length = shift(occurrence, event, zone)
    check_shift(parts, delta, master_start(master, zone))
    boundary = boundary_of(occurrence, zone)
    start, end = times(boundary + delta, length, zone)
    body = {k: master[k] for k in COPIED if k in master}
    body.update(id=new_id, start=start, end=end,
                recurrence=[moved_rule(parts, zone, delta, master_start(master, zone), boundary)])
    return body


def instance_id(base, start, zone):
    """Google's ID of an instance of series `base` whose original start is `start` (series wall time or a date)."""
    if zone is None:
        return f"{base}_{start.strftime('%Y%m%d')}"
    return f"{base}_{placed(start, zone).astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def span_of(value, zone):
    """Wall start and length of a Google start/end pair in the series' form; None when the form differs."""
    start, end = value.get("start") or {}, value.get("end") or {}
    try:
        if zone is None:
            if "date" not in start or "dateTime" in start:
                return None
            a = date.fromisoformat(start["date"])
            return a, date.fromisoformat(end["date"]) - a
        if "dateTime" not in start or "dateTime" not in end:
            return None
        return wall(start["dateTime"], zone), instant(end["dateTime"]) - instant(start["dateTime"])
    except (CalendarError, KeyError, TypeError, ValueError):
        return None


def in_range(exceptions, zone, boundary, scope):
    """The exceptions a move of `scope` from `boundary` touches, with their original starts, in original order; an
    exception whose original start cannot be read refuses (`series_exceptions`)."""
    found = []
    for e in exceptions:
        original = original_of(e, zone)
        if original is None:
            raise CalendarError("series_exceptions")
        if scope == "following" and original < boundary:
            continue
        found.append((original, e))
    return sorted(found, key=lambda pair: pair[0])


def carried(master, exceptions, occurrence, event, scope, zone):
    """The exceptions a time move of `scope` carries (ANYM-614), in original-start order, each with its fate and the
    one write that keeps it in the moved series (its instance there is named by its original start plus the shift):
    a cancelled one stays cancelled (a delete); one edited without a time change moves with the series and keeps its
    own fields (a patch of the fields that differ from the master, or no write); one whose start or length was
    changed keeps its own start and end too. The occurrence the move names follows the requested time. Refuses
    what check_exceptions refuses: guests of its own (or a special type, or an uncopiable field) and an unreadable
    original start."""
    delta, length = shift(occurrence, event, zone)
    boundary = boundary_of(occurrence, zone)
    series = span_of(master, zone)
    if series is None:
        raise CalendarError("unsupported_recurrence")
    out = []
    for original, e in in_range(exceptions, zone, boundary, scope):
        if any(e.get(k) for k in UNCOPIABLE) or e.get("eventType", "default") != "default":
            raise CalendarError("capability_unverified")
        item = {"id": e.get("id"), "etag": e.get("etag"), "original": original, "start": original + delta}
        if e.get("status") == "cancelled":
            out.append(dict(item, fate="cancelled", desired=None))
            continue
        own = span_of(e, zone)
        if own is None:
            # An exception that switched between timed and all-day has no form this write keeps.
            raise CalendarError("capability_unverified")
        desired = {k: e.get(k) for k in CARRIED if e.get(k) != master.get(k)}
        if original == boundary or own == (original, series[1]):
            out.append(dict(item, fate="moves", desired=desired))
        else:
            form = "date" if zone is None else "dateTime"
            out.append(dict(item, fate="kept", desired={**desired, "start": e["start"], "end": e["end"]},
                            own={"start": e["start"][form], "end": e["end"][form]}))
    return out


def later_start(parts, first, exceptions, keep):
    """The original start of the first rule instance, not an exception, after which at most `keep` of the
    (original-ordered) `exceptions` follow: where a following move carries no more than it may. None past the
    rule's end."""
    if len(exceptions) <= keep:
        return None
    after = exceptions[len(exceptions) - keep - 1]
    taken = set(exceptions)
    as_time = first if isinstance(first, datetime) else datetime.combine(first, datetime.min.time())
    try:
        rule = rrulestr(rule_line(parts)[len("RRULE:"):], dtstart=as_time, ignoretz=True)
    except (ValueError, TypeError):
        raise CalendarError("unsupported_recurrence") from None
    for value in islice(rule, MAX_INSTANCES):
        found = value if isinstance(first, datetime) else value.date()
        if found > after and found not in taken:
            return found
    return None
