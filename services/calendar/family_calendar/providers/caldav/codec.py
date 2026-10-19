"""Conservative iCalendar transformations preserving the full source object.

Future splitting is deliberately limited to a single simple weekly rule. Parsing
or expansion errors never become an empty calendar. Datetimes retain wall time.
"""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from itertools import islice

from dateutil.rrule import rruleset, rrulestr
from dateutil.tz import gettz
from icalendar import Calendar, Event, Timezone

from family_calendar.errors import CalendarError

MAX_ICS_BYTES = 1_048_576
MAX_OCCURRENCES = 10_000
VOLATILE = {"DTSTAMP", "LAST-MODIFIED", "CREATED", "SEQUENCE"}
TIMEZONE_LOWER = datetime(2020, 1, 1)
TIMEZONE_UPPER = datetime(2035, 1, 1)


def parse(data: bytes) -> Calendar:
    if not data or len(data) > MAX_ICS_BYTES:
        raise CalendarError("invalid_calendar_data")
    try:
        cal = Calendar.from_ical(data)
        events = cal.walk("VEVENT")
        if not events or any(c.errors for c in cal.walk()):
            raise ValueError
        if len({str(e["UID"]) for e in events}) != 1:
            raise ValueError
        if sum("RECURRENCE-ID" not in e for e in events) != 1:
            raise ValueError
        return cal
    except CalendarError:
        raise
    except Exception:
        raise CalendarError("invalid_calendar_data") from None


def master(cal):
    return next(e for e in cal.walk("VEVENT") if "RECURRENCE-ID" not in e)


def _set(component, name, value, *, params=None):
    if isinstance(value, datetime) and value.tzinfo is not None:
        back = value.astimezone(timezone.utc).astimezone(value.tzinfo)
        if back.replace(tzinfo=None) != value.replace(tzinfo=None):
            raise CalendarError("invalid_local_time")
        if value.replace(fold=0).utcoffset() != value.replace(fold=1).utcoffset():
            raise CalendarError("ambiguous_local_time")
    old_params = dict(component[name].params) if name in component else {}
    if isinstance(value, date):
        # These parameters describe the new value, unlike opaque extension parameters.
        old_params.pop("TZID", None)
        old_params.pop("VALUE", None)
    if name in component:
        del component[name]
    component.add(name, value)
    component[name].params.update(old_params if params is None else params)


def _touch(event):
    _set(event, "DTSTAMP", datetime.now(timezone.utc), params={})
    _set(event, "SEQUENCE", int(event.get("SEQUENCE", 0)) + 1, params={})


def create_event(uid, start, end, summary, *, rrule=None):
    if type(start) is not type(end) or end <= start:
        raise CalendarError("invalid_time_range")
    cal = Calendar()
    cal.add("PRODID", "-//Family Calendar Service//iCloud Pilot//EN")
    cal.add("VERSION", "2.0")
    event = Event()
    for key, value in (("UID", uid), ("DTSTART", start), ("DTEND", end), ("SUMMARY", summary)):
        _set(event, key, value, params={})
    event.add("DTSTAMP", datetime.now(timezone.utc))
    event.add("TRANSP", "OPAQUE")
    if rrule:
        event.add("RRULE", rrule)
    if isinstance(start, datetime) and getattr(start.tzinfo, "key", None):
        # The generator scans local wall time. ZoneInfo maps nonexistent spring
        # times to the pre-transition offset, making its onset one hour late.
        # dateutil resolves the gap to the new offset and emits RFC onsets.
        cal.add_component(
            Timezone.from_tzinfo(
                gettz(start.tzinfo.key),
                tzid=start.tzinfo.key,
                first_date=date(2020, 1, 1),
                last_date=date(2035, 1, 1),
            )
        )
    cal.add_component(event)
    return cal.to_ical()


def _canonical(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if hasattr(value, "dt"):
        return _canonical(value.dt)
    if hasattr(value, "dts"):
        return [_canonical(v) for v in value.dts]
    if hasattr(value, "to_ical"):
        return _canonical(value.to_ical())
    return str(value)


def _timezone_offsets(component):
    """Compare the pilot's timezone behavior, not provider history formatting.

    Expand every observance transition exactly in a declared finite interval.
    Custom zones, extensions, complex rules and excessive expansions retain
    their full structural comparison instead of receiving a weaker match.
    """
    if str(component.get("TZID")) not in {"Asia/Makassar", "Europe/Berlin"}:
        return None
    if set(component) - {"TZID", "COMMENT", "X-LIC-LOCATION", "LAST-MODIFIED"}:
        return None
    if not 0 < len(component.subcomponents) <= 64:
        return None
    for item in [component, *component.subcomponents]:
        if any(
            getattr(value, "params", {})
            for values in item.values()
            for value in (values if isinstance(values, list) else [values])
        ):
            return None
    transitions = {}
    try:
        for observance in component.subcomponents:
            if (
                observance.subcomponents
                or observance.name not in {"STANDARD", "DAYLIGHT"}
                or set(observance) - {"DTSTART", "TZOFFSETFROM", "TZOFFSETTO", "TZNAME", "RRULE", "RDATE"}
            ):
                return None
            start = observance.decoded("DTSTART")
            if not isinstance(start, datetime) or start.tzinfo is not None:
                return None
            offset_from = observance.decoded("TZOFFSETFROM")
            offset_to = observance.decoded("TZOFFSETTO")
            values = [start, *_dates(observance, "RDATE")]
            if "RRULE" in observance:
                rule = deepcopy(observance["RRULE"])
                if isinstance(rule, list) or rule.get("FREQ") != ["YEARLY"]:
                    return None
                if "UNTIL" in rule:
                    until = rule["UNTIL"][0]
                    if until.tzinfo is not None:
                        rule["UNTIL"] = [until.astimezone(timezone.utc).replace(tzinfo=None) + offset_from]
                for index, value in enumerate(rrulestr(rule.to_ical().decode(), dtstart=start)):
                    if index >= 1000:
                        return None
                    if value > TIMEZONE_UPPER + timedelta(days=1):
                        break
                    values.append(value)
            if len(values) > 10_000:
                return None
            for value in values:
                instant = value - offset_from
                if instant in transitions and transitions[instant] != offset_to:
                    return None
                transitions[instant] = offset_to
        prior = [instant for instant in transitions if instant <= TIMEZONE_LOWER]
        if not prior:
            return None
        offset = transitions[max(prior)]
        timeline = [[TIMEZONE_LOWER.isoformat(), int(offset.total_seconds())]]
        for instant in sorted(transitions):
            if TIMEZONE_LOWER < instant < TIMEZONE_UPPER and transitions[instant] != offset:
                offset = transitions[instant]
                timeline.append([instant.isoformat(), int(offset.total_seconds())])
        return {
            "type": "VTIMEZONE",
            "properties": {"TZID": str(component["TZID"])},
            "offset_scope": [TIMEZONE_LOWER.date().isoformat(), TIMEZONE_UPPER.date().isoformat()],
            "offset_timeline": timeline,
            "components": [],
        }
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None


def semantic_snapshot(data):
    def component(c):
        if c.name == "VTIMEZONE":
            offsets = _timezone_offsets(c)
            if offsets is not None:
                return offsets
        props = {}
        for key, value in sorted(c.items()):
            if key in VOLATILE:
                continue
            vals = value if isinstance(value, list) else [value]
            props[key] = [{"value": _canonical(v), "params": _canonical(dict(v.params))} for v in vals]
            if len(vals) == 1:
                props[key] = props[key][0]
        return {"type": c.name, "properties": props, "components": [component(x) for x in c.subcomponents]}

    cal = parse(data)
    result = component(cal)
    # Stable order, independent of server component/property ordering.
    result["components"] = sorted(result["components"], key=lambda c: json.dumps(c, sort_keys=True))
    return {"calendar": result, "events": [component(e)["properties"] for e in cal.walk("VEVENT")]}


def semantic_hash(data):
    snap = semantic_snapshot(data)["calendar"]
    return hashlib.sha256(json.dumps(snap, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _rule(event):
    value = event.get("RRULE")
    if isinstance(value, list):
        raise CalendarError("unsupported_recurrence")
    return value


def _dates(event, key):
    values = event.get(key, [])
    values = values if isinstance(values, list) else [values]
    return [x.dt for v in values for x in v.dts]


def _datetime(value, reference=None):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise CalendarError("floating_time_requires_timezone")
        return value
    zone = reference.tzinfo if isinstance(reference, datetime) else timezone.utc
    return datetime.combine(value, datetime.min.time(), tzinfo=zone)


def _recurrences(event):
    start = _datetime(event.decoded("DTSTART"))
    rules = rruleset()
    rule = _rule(event)
    if rule:
        rules.rrule(rrulestr(rule.to_ical().decode(), dtstart=start))
    else:
        rules.rdate(start)
    for value in _dates(event, "RDATE"):
        rules.rdate(_datetime(value, start))
    for value in _dates(event, "EXDATE"):
        rules.exdate(_datetime(value, start))
    return rules


def _convert(value, template):
    return value if isinstance(template, datetime) else value.date()


def occurrences(data, lower, upper):
    cal = parse(data)
    root = master(cal)
    template = root.decoded("DTSTART")
    start = _datetime(template)
    duration = _datetime(root.decoded("DTEND"), start) - start
    if lower >= upper:
        raise CalendarError("invalid_time_range")
    lower, upper = _datetime(lower, start), _datetime(upper, start)
    exceptions = {e.decoded("RECURRENCE-ID"): e for e in cal.walk("VEVENT") if "RECURRENCE-ID" in e}
    range_events = sorted(
        (e for e in exceptions.values() if e["RECURRENCE-ID"].params.get("RANGE") == "THISANDFUTURE"),
        key=lambda e: e.decoded("RECURRENCE-ID"),
    )
    # Include original identities for moved exceptions outside the normal window.
    candidates = list(islice(_recurrences(root).xafter(lower - duration, inc=True), MAX_OCCURRENCES + 1))
    candidates = [x for x in candidates if x < upper]
    if len(candidates) > MAX_OCCURRENCES:
        raise CalendarError("expansion_limit")
    ids = {_convert(x, template) for x in candidates}
    ids.update(exceptions)
    output = []
    for rid in sorted(ids):
        e = exceptions.get(rid)
        if e and str(e.get("STATUS", "")).upper() == "CANCELLED":
            continue
        actual = _datetime(rid, start)
        end = actual + duration
        if e:
            actual = _datetime(e.decoded("DTSTART"), start)
            end = _datetime(e.decoded("DTEND"), start)
        else:
            ranges = [r for r in range_events if r.decoded("RECURRENCE-ID") <= rid]
            if ranges:
                latest = ranges[-1]
                delta = _datetime(latest.decoded("DTSTART"), start) - _datetime(
                    latest.decoded("RECURRENCE-ID"), start
                )
                actual += delta
                end = actual + (
                    _datetime(latest.decoded("DTEND"), start) - _datetime(latest.decoded("DTSTART"), start)
                )
        if actual < upper and end > lower:
            output.append(
                {"identity": rid, "start": _convert(actual, template), "end": _convert(end, template)}
            )
    return sorted(output, key=lambda x: x["start"])


def _assert_identity(root, rid):
    expected = root.decoded("DTSTART")
    if type(rid) is not type(expected):
        raise CalendarError("invalid_occurrence_identity")
    value = _datetime(rid, _datetime(expected))
    if value not in _recurrences(root).between(value, value, inc=True):
        raise CalendarError("invalid_occurrence_identity")


def _identity(root, rid):
    """Write an identity in the zone of the series start, so a UTC value read back from storage
    still names the series' own wall time."""
    start = root.decoded("DTSTART")
    if isinstance(rid, datetime) and rid.tzinfo is not None and isinstance(start, datetime) and start.tzinfo:
        return rid.astimezone(start.tzinfo)
    return rid


def _override(cal, rid):
    return next(
        (e for e in cal.walk("VEVENT") if e.get("RECURRENCE-ID") and e.decoded("RECURRENCE-ID") == rid), None
    )


def _duration(event):
    if "DTEND" in event:
        return event.decoded("DTEND") - event.decoded("DTSTART")
    if "DURATION" in event:
        return event.decoded("DURATION")
    # RFC 5545 3.6.1: a DATE event lasts a day, a DATE-TIME event without an end is an instant.
    return timedelta(days=1) if not isinstance(event.decoded("DTSTART"), datetime) else timedelta(0)


def edit_single(data, rid, start, end, *, summary=None, location=None, description=None):
    """Create or update the override of exactly one occurrence, keyed by its original identity."""
    cal = parse(data)
    root = master(cal)
    rid = _identity(root, rid)
    _assert_identity(root, rid)
    if type(start) is not type(rid) or type(end) is not type(rid) or end <= start:
        raise CalendarError("invalid_time_range")
    event = _override(cal, rid)
    if event is None:
        event = deepcopy(root)
        for key in ("RRULE", "RDATE", "EXDATE"):
            event.pop(key, None)
        event.add("RECURRENCE-ID", rid)
        cal.add_component(event)
    _set(event, "DTSTART", start)
    _set(event, "DTEND", end)
    event.pop("DURATION", None)
    event.pop("STATUS", None)
    for name, value in (("SUMMARY", summary), ("LOCATION", location), ("DESCRIPTION", description)):
        if value is not None:
            _set(event, name, value)
    _touch(event)
    return cal.to_ical()


def move_single(data, rid, start, end):
    return edit_single(data, rid, start, end)


def cancel_single(data, rid):
    """A CANCELLED override with the original RECURRENCE-ID; the master and its EXDATE stay as they are."""
    cal = parse(data)
    root = master(cal)
    rid = _identity(root, rid)
    _assert_identity(root, rid)
    existing = _override(cal, rid)
    # An occurrence moved before keeps the time it was moved to.
    source, start = (existing, existing.decoded("DTSTART")) if existing is not None else (root, rid)
    changed = parse(edit_single(data, rid, start, start + _duration(source)))
    _set(_override(changed, rid), "STATUS", "CANCELLED", params={})
    return changed.to_ical()


def _shift_date_lists(event, delta):
    for key in ("RDATE", "EXDATE"):
        vals = event.get(key, [])
        vals = vals if isinstance(vals, list) else [vals]
        for value in vals:
            for d in value.dts:
                d.dt += delta


def _shift_root(root, delta):
    start = root.decoded("DTSTART")
    rule = _rule(root)
    if rule and "BYDAY" in rule and (start + delta).weekday() != start.weekday():
        raise CalendarError("unsupported_recurrence")
    if not isinstance(root.decoded("DTSTART"), datetime) and delta.seconds:
        raise CalendarError("unsupported_recurrence")
    for key in ("DTSTART", "DTEND"):
        _set(root, key, root.decoded(key) + delta)
    _shift_date_lists(root, delta)
    if rule and "UNTIL" in rule:
        rule["UNTIL"] = [rule["UNTIL"][0] + delta]
    _touch(root)


def shift_series(data, delta):
    cal = parse(data)
    _shift_root(master(cal), delta)
    for e in cal.walk("VEVENT"):
        if "RECURRENCE-ID" in e:
            if e["RECURRENCE-ID"].params.get("RANGE"):
                raise CalendarError("unsupported_recurrence")
            _set(e, "RECURRENCE-ID", e.decoded("RECURRENCE-ID") + delta)
            _touch(e)
    return cal.to_ical()


def native_future(data, rid, delta):
    root = master(parse(data))
    duration = root.decoded("DTEND") - root.decoded("DTSTART")
    cal = parse(move_single(data, rid, rid + delta, rid + delta + duration))
    event = next(
        e for e in cal.walk("VEVENT") if e.get("RECURRENCE-ID") and e.decoded("RECURRENCE-ID") == rid
    )
    event["RECURRENCE-ID"].params["RANGE"] = "THISANDFUTURE"
    return cal.to_ical()


def split_future(data, boundary, delta, new_uid):
    source = parse(data)
    root = master(source)
    rule = _rule(root)
    allowed = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST"}
    if not rule or set(rule) - allowed or rule.get("FREQ") != ["WEEKLY"]:
        raise CalendarError("unsupported_recurrence")
    weekday = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][root.decoded("DTSTART").weekday()]
    if "BYDAY" in rule and list(rule["BYDAY"]) != [weekday]:
        raise CalendarError("unsupported_recurrence")
    if any(e.get("RECURRENCE-ID") and e["RECURRENCE-ID"].params.get("RANGE") for e in source.walk("VEVENT")):
        raise CalendarError("unsupported_recurrence")
    start = root.decoded("DTSTART")
    if type(boundary) is not type(start) or new_uid == str(root["UID"]):
        raise CalendarError("invalid_occurrence_identity")
    raw_rule = rrulestr(rule.to_ical().decode(), dtstart=_datetime(start))
    bound = _datetime(boundary, _datetime(start))
    before = list(islice(raw_rule.xafter(_datetime(start), inc=True), MAX_OCCURRENCES + 1))
    before = [x for x in before if x <= bound]
    if len(before) > MAX_OCCURRENCES:
        raise CalendarError("expansion_limit")
    if not before or before[-1] != bound:
        raise CalendarError("invalid_occurrence_identity")
    n_before = len(before) - 1
    old, new = deepcopy(source), deepcopy(source)
    old_root, new_root = master(old), master(new)
    old.subcomponents = [
        c
        for c in old.subcomponents
        if c.name != "VEVENT" or c is old_root or c.decoded("RECURRENCE-ID") < boundary
    ]
    new.subcomponents = [
        c
        for c in new.subcomponents
        if c.name != "VEVENT" or c is new_root or c.decoded("RECURRENCE-ID") >= boundary
    ]
    mapping = {
        "old_uid": str(root["UID"]),
        "new_uid": new_uid,
        "boundary": boundary.isoformat(),
        "shift_seconds": delta.total_seconds(),
        "exceptions": {},
    }
    for e in new.walk("VEVENT"):
        _set(e, "UID", new_uid, params={})
        if "RECURRENCE-ID" in e:
            rid = e.decoded("RECURRENCE-ID")
            _set(e, "RECURRENCE-ID", rid + delta)
            mapping["exceptions"][rid.isoformat()] = (rid + delta).isoformat()
    duration = root.decoded("DTEND") - start
    _set(new_root, "DTSTART", boundary)
    _set(new_root, "DTEND", boundary + duration)
    for event, is_new in ((old_root, False), (new_root, True)):
        for key in ("RDATE", "EXDATE"):
            values = [v for v in _dates(root, key) if (v >= boundary) == is_new]
            event.pop(key, None)
            if values:
                event.add(key, values)
    old_rule, new_rule = _rule(old_root), _rule(new_root)
    old_rule.pop("UNTIL", None)
    old_rule["COUNT"] = [n_before] if n_before else [1]
    if "COUNT" in new_rule:
        new_rule["COUNT"] = [int(new_rule["COUNT"][0]) - n_before]
    _shift_root(new_root, delta)
    _touch(old_root)
    return (old.to_ical() if n_before else None), new.to_ical(), mapping
