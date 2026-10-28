import asyncio
import base64
from datetime import date, datetime, time, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar
import recurring_ical_events
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.auth.exceptions import RefreshError
from google.oauth2 import id_token

from family_calendar.errors import CalendarError
from family_calendar.providers.caldav.adapter import CalDAVAdapter
from family_calendar.providers.caldav.transport import DAVTransport, within_collection
from family_calendar.providers.caldav.codec import (
    cancel_single,
    create_event,
    edit_single,
    parse,
    master,
    _set,
    _touch,
    semantic_hash,
)
from family_calendar.providers.google.adapter import GoogleAdapter
from . import series as recurrence
from .store import digest, instant

# Occurrences published for one calendar window; the same limit as agenda/slots responses.
EVENT_LIMIT = 500
# Private extended property of a projected Google event; its value is the random link ID only.
LINK_PROPERTY = "deedkitLink"
# dateutil (behind recurring_ical_events) walks and caches every rule instance from
# DTSTART up to the window end, even for a narrow window. Bound that work before
# expansion: per resource for memory, per calendar window for CPU.
RULE_INSTANCE_LIMIT = 100_000
CALENDAR_RULE_INSTANCE_LIMIT = 1_000_000
_FREQ_SECONDS = {
    "SECONDLY": 1,
    "MINUTELY": 60,
    "HOURLY": 3600,
    "DAILY": 86400,
    "WEEKLY": 7 * 86400,
    "MONTHLY": 28 * 86400,
    "YEARLY": 365 * 86400,
}
_DAYS_PER_PERIOD = {"WEEKLY": 7, "MONTHLY": 31, "YEARLY": 366}


def source_ref(prefix, *parts):
    return prefix + "-" + digest(parts)[:32]


def created_event_ref(provider, resource_ref, uid):
    """The event_ref the next read publishes for a single event DeedKit created (normalize_google/_icloud)."""
    if provider == "google":
        return source_ref("event", resource_ref, "master")
    return source_ref("event", resource_ref, uid, "master")


def series_ref(calendar_ref, series):
    """Public name of a recurring series: the provider's own ID or href is never shown."""
    return source_ref("series", calendar_ref, series)


def ical_time(value, zone):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise CalendarError("coverage_incomplete")
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, ZoneInfo(zone)).astimezone(timezone.utc)
    raise CalendarError("coverage_incomplete")


def provider_text(value, limit):
    # PostgreSQL text/jsonb rejects NUL and unpaired surrogates.
    return str(value).encode("utf-8", "replace").decode().replace("\x00", "")[:limit]


def ends_before_start(event, start, zone):
    # recurring_ical_events silently swaps a reversed DTEND or negative DURATION.
    if "DTEND" not in event:
        return "DURATION" in event and event.decoded("DURATION") < timedelta(0)
    finish = event.decoded("DTEND")
    floating = isinstance(finish, datetime) and finish.tzinfo is None
    return not floating and ical_time(finish, zone) < start


def expansion_budget():
    """Shared by all resources of one calendar window."""
    return {"occurrences": EVENT_LIMIT, "rule_instances": CALENDAR_RULE_INSTANCE_LIMIT}


def rule_instances(event, end, zone):
    """Upper bound of RRULE instances from DTSTART until end, computed without generating them."""
    start = ical_time(event.decoded("DTSTART"), zone)
    rules = event.get("RRULE", [])
    total = 0
    for rule in rules if isinstance(rules, list) else [rules]:
        freq = rule.get("FREQ", [None])[0]
        if freq not in _FREQ_SECONDS:
            raise CalendarError("coverage_incomplete")
        stop = end
        if rule.get("UNTIL"):
            until = rule["UNTIL"][0]
            if not isinstance(until, datetime):
                until += timedelta(days=1)  # a DATE includes its whole day
            elif until.tzinfo is None:
                until = until.replace(tzinfo=ZoneInfo(zone))
            stop = min(stop, ical_time(until, zone))
        step = _FREQ_SECONDS[freq] * max(int(rule.get("INTERVAL", [1])[0]), 1)
        count = max(int((stop - start).total_seconds() // step), 0) + 1
        # BYxxx parts finer than FREQ expand every period; coarser parts only filter.
        for part, seconds in (("BYSECOND", 1), ("BYMINUTE", 60), ("BYHOUR", 3600)):
            if rule.get(part) and _FREQ_SECONDS[freq] > seconds:
                count *= len(rule[part])
        if freq == "WEEKLY" and rule.get("BYDAY"):
            count *= min(len(rule["BYDAY"]), 7)
        elif freq in _DAYS_PER_PERIOD and any(
            rule.get(part) for part in ("BYDAY", "BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYMONTH")
        ):
            count *= _DAYS_PER_PERIOD[freq]
        if rule.get("COUNT"):
            count = min(count, int(rule["COUNT"][0]))
        total += count
    return total


def expand(cal, start, end):
    # CalendarQuery.between() copies a component for every occurrence before the caller
    # can count them; Series.between() is a generator, so the caller's limit stops it.
    for series in recurring_ical_events.of(cal).series:
        for occurrence in series.between(start, end):
            yield occurrence.as_component(False)


def normalize_icloud(calendar_ref, resource, start, end, zone, budget=None):
    budget = expansion_budget() if budget is None else budget
    try:
        cal = Calendar.from_ical(resource["data"])
        original = cal.walk("VEVENT")
        recurring = any("RRULE" in e or "RECURRENCE-ID" in e or "RDATE" in e for e in original)
        occurrences = []
        resource_ref = source_ref("res", calendar_ref, resource["href"])
        # Refuse unspecified floating time rather than assume server/user timezone.
        for e in original:
            if "DTSTART" in e and ends_before_start(e, ical_time(e.decoded("DTSTART"), zone), zone):
                raise CalendarError("invalid_provider_event_time")
        # Dense (MINUTELY/SECONDLY) or long-running rules are incomplete, never expanded.
        instances = sum(rule_instances(e, end, zone) for e in original if "RRULE" in e)
        budget["rule_instances"] -= instances
        if instances > RULE_INSTANCE_LIMIT or budget["rule_instances"] < 0:
            raise CalendarError("coverage_incomplete")
        for e in expand(cal, start, end):
            if str(e.get("STATUS", "")).upper() == "CANCELLED":
                continue
            a = ical_time(e.decoded("DTSTART"), zone)
            finish = (
                e.decoded("DTEND")
                if "DTEND" in e
                else e.decoded("DTSTART") + e.decoded("DURATION", timedelta(days=1))
            )
            b = ical_time(finish, zone)
            if b < a:
                raise CalendarError("invalid_provider_event_time")
            # A zero-length event (e.g. DTSTART without DTEND) is a non-blocking instant.
            if a >= end or (b <= start if b > a else a < start):
                continue
            original = e.get("RECURRENCE-ID") or e["DTSTART"]
            identity = original.to_ical().decode() if recurring else "master"
            details = {
                "title": provider_text(e.get("SUMMARY", ""), 256),
                "location": provider_text(e.get("LOCATION", ""), 512),
                "description": provider_text(e.get("DESCRIPTION", ""), 4000),
                "recurring": recurring,
                "guests": "ATTENDEE" in e or "ORGANIZER" in e,
                "all_day": not isinstance(e.decoded("DTSTART"), datetime),
                "instant": a == b,
            }
            if recurring:
                # The series and the occurrence's original start name exactly one occurrence.
                details.update(
                    series_ref=series_ref(calendar_ref, resource["href"]), original_start=original.dt.isoformat()
                )
            occurrences.append(
                {
                    "ref": source_ref("event", resource_ref, str(e["UID"]), identity),
                    "start": a,
                    "end": b,
                    "busy": b > a and str(e.get("TRANSP", "OPAQUE")).upper() != "TRANSPARENT",
                    "details": details,
                }
            )
            if len(occurrences) > budget["occurrences"]:
                raise CalendarError("coverage_incomplete")
        budget["occurrences"] -= len(occurrences)
        return {
            "resource_ref": resource_ref,
            "target": resource["href"],
            "etag": resource["etag"],
            "payload": {"ical": base64.b64encode(resource["data"]).decode()},
            "occurrences": occurrences,
        }
    except CalendarError:
        raise
    except Exception:
        raise CalendarError("coverage_incomplete") from None


def valid_original(value):
    """An instance identity the guard can compare: a timestamp with an offset or a calendar date."""
    if not isinstance(value, str):
        return False
    try:
        instant(value) if "T" in value else date.fromisoformat(value)
    except (CalendarError, ValueError):
        return False
    return True


def normalize_google(calendar_ref, e, zone):
    resource_ref = source_ref("res", calendar_ref, e["id"])
    rows = []
    if e.get("status") != "cancelled" and "start" in e and "end" in e:

        def point(value):
            return (
                instant(value["dateTime"])
                if "dateTime" in value
                else ical_time(date.fromisoformat(value["date"]), zone)
            )

        a, b = point(e["start"]), point(e["end"])
        if b < a:
            raise CalendarError("invalid_provider_event_time")
        identity = e.get("originalStartTime", "master")
        details = {
            "title": provider_text(e.get("summary", ""), 256),
            "location": provider_text(e.get("location", ""), 512),
            "description": provider_text(e.get("description", ""), 4000),
            "recurring": bool(e.get("recurringEventId") or e.get("recurrence")),
            "guests": bool(e.get("attendees")),
            "special": e.get("eventType", "default") != "default",
            "all_day": "date" in e["start"],
            "instant": a == b,
        }
        original = identity.get("dateTime") or identity.get("date") if isinstance(identity, dict) else None
        if not valid_original(original):
            original = None
        if e.get("recurringEventId") and original and not e.get("recurrence"):
            # An instance (singleEvents=true) names its series and its original start; a master has neither.
            details.update(series_ref=series_ref(calendar_ref, e["recurringEventId"]), original_start=original)
        rows.append(
            {
                "ref": source_ref("event", resource_ref, identity),
                "start": a,
                "end": b,
                # A zero-length event is a non-blocking instant.
                "busy": b > a and e.get("transparency") != "transparent",
                "details": details,
            }
        )
    return {
        "resource_ref": resource_ref,
        "target": e["id"],
        "etag": e.get("etag"),
        "payload": e,
        "occurrences": rows,
    }


def same_time(actual, desired):
    """A Google start/end read back as written; a null part of `desired` must be absent."""
    for part, wanted in desired.items():
        if wanted is None:
            if actual.get(part) is not None:
                return False
        elif part == "dateTime":
            if "dateTime" not in actual or instant(actual["dateTime"]) != instant(wanted):
                return False
        elif actual.get(part) != wanted:
            return False
    return True


def same_guests(actual, desired):
    """Google's attendees are the guests sent: response fields added, addresses in any case, and only the
    calendar's own account (organizer or self) besides them."""
    wanted = {g["email"].casefold() for g in desired}
    found = {a.get("email", "").casefold() for a in actual if isinstance(a, dict)}
    extra = [a for a in actual if isinstance(a, dict) and a.get("email", "").casefold() not in wanted]
    return wanted <= found and all(a.get("organizer") or a.get("self") for a in extra)


class Providers:
    """Only the worker has this object and access to provider secrets."""

    def __init__(self, store, vault):
        self.store, self.vault = store, vault
        self.adapters = {}
        self.transports = []

    def _token(self, connection_id):
        # A DB advisory lock serializes refresh across processes. Read the latest encrypted credentials under it.
        with self.store.transaction() as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("oauth:" + connection_id,))
            saved = self.vault.load(connection_id)
            credentials = Credentials.from_authorized_user_info(saved["credentials"])
            if credentials.token_uri not in (
                "https://oauth2.googleapis.com/token",
                "https://accounts.google.com/o/oauth2/token",
            ):
                raise CalendarError("unsafe_token_endpoint")
            if not credentials.valid:
                try:
                    credentials.refresh(Request())
                    if credentials.id_token:
                        claims = id_token.verify_oauth2_token(
                            credentials.id_token, Request(), credentials.client_id
                        )
                        if claims["sub"] != saved["sub"]:
                            raise CalendarError("oauth_identity_mismatch")
                    saved["credentials"] = json.loads(credentials.to_json())
                    self.vault.save(connection_id, saved)
                except RefreshError:
                    raise CalendarError("reauth_required") from None
            return credentials.token

    async def adapter(self, cal):
        connection_id = cal["connection_id"]
        if connection_id not in self.adapters:
            if cal["provider"] == "icloud":
                secret = self.vault.load(connection_id)
                transport = DAVTransport(secret["username"], secret["password"])
                self.transports.append(transport)
                adapter = CalDAVAdapter(connection_id, transport)
            else:
                client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
                self.transports.append(client)
                adapter = GoogleAdapter(
                    connection_id, client, lambda: asyncio.to_thread(self._token, connection_id)
                )
            self.adapters[connection_id] = adapter
        return self.adapters[connection_id]

    async def close(self):
        for transport in self.transports:
            await (transport.close() if isinstance(transport, DAVTransport) else transport.aclose())

    async def read_window(self, cal, start, end):
        adapter = await self.adapter(cal)
        if cal["provider"] == "icloud":
            # timestamptz values arrive in the DB session zone; the CalDAV time-range is UTC.
            result = await adapter.read_window(
                cal["target"], *(t.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") for t in (start, end))
            )
            budget = expansion_budget()
            rows = [
                normalize_icloud(cal["ref"], r, start, end, cal["timezone"], budget)
                for r in result["resources"]
            ]
        else:
            rows = [
                normalize_google(cal["ref"], e, cal["timezone"])
                for e in await adapter.window(cal["target"], start.isoformat(), end.isoformat())
            ]
        if sum(len(r["occurrences"]) for r in rows) > EVENT_LIMIT:
            raise CalendarError("coverage_incomplete")
        for r in rows:
            r["payload"] = {
                "encrypted_v1": self.vault._cipher().encrypt(json.dumps(r["payload"]).encode()).decode()
            }
        return rows

    async def read(self, cal, target):
        adapter = await self.adapter(cal)
        if cal["provider"] == "icloud":
            if not within_collection(target, cal["target"]):
                raise CalendarError("provider_resource_outside_calendar")
            return await adapter.read(target)
        return await adapter.read(cal["target"], target)

    @staticmethod
    def _guard_google(cal, old, occurrence):
        if old.get("status") == "cancelled":
            raise CalendarError("version_conflict")
        if old.get("attendees") or old.get("eventType", "default") != "default":
            raise CalendarError("capability_unverified")
        if occurrence is None:
            if old.get("recurrence") or old.get("recurringEventId"):
                raise CalendarError("capability_unverified")
            return
        # Only an instance of a series, never the master.
        if old.get("recurrence") or not old.get("recurringEventId"):
            raise CalendarError("capability_unverified")
        started, wanted = old.get("originalStartTime") or {}, occurrence["original_start"]
        if not isinstance(started, dict):
            raise CalendarError("version_conflict")
        same = (
            "T" in wanted and instant(started["dateTime"]) == instant(wanted)
            if "dateTime" in started
            else started.get("date") == wanted
        )
        if not same or series_ref(cal["ref"], old["recurringEventId"]) != occurrence["series_ref"]:
            raise CalendarError("version_conflict")

    @staticmethod
    def _guard_caldav(cal, target, original, occurrence):
        events = original.walk("VEVENT")
        e = master(original)
        if any(k in ev for ev in events for k in ("ATTENDEE", "ORGANIZER")):
            raise CalendarError("capability_unverified")
        if occurrence is None:
            if len(events) != 1 or any(k in e for k in ("RRULE", "RDATE", "RECURRENCE-ID")):
                raise CalendarError("capability_unverified")
            return
        # A series of this module's kind: one master, exceptions that name a single occurrence each.
        if not ("RRULE" in e or "RDATE" in e) or any(
            "RANGE" in ev["RECURRENCE-ID"].params for ev in events if "RECURRENCE-ID" in ev
        ):
            raise CalendarError("capability_unverified")
        if series_ref(cal["ref"], target) != occurrence["series_ref"]:
            raise CalendarError("version_conflict")

    @staticmethod
    def _google_times(event, old):
        """Date or dateTime for start and end. A conversion removes the other form: a patch only merges."""
        times = {}
        for name in ("start", "end"):
            previous = (old or {}).get(name) or {}
            if event.get("all_day"):
                value = {"date": event[name]}
                value.update({k: None for k in ("dateTime", "timeZone") if k in previous})
            else:
                value = {"dateTime": event[name], "timeZone": event["timezone"]}
                value.update({"date": None} if "date" in previous else {})
            times[name] = value
        return times

    async def prepare(self, cal, step):
        if step.get("projection") and cal["provider"] != "google":
            raise CalendarError("provider_not_supported")
        invite = step.get("invite")
        if invite and (step["action"] != "create" or cal["provider"] != "google"):
            # Guests only on a Google create (ANYM-555); nothing was read or sent yet.
            raise CalendarError("capability_unsupported")
        if step.get("series"):
            return await self._prepare_series(cal, step)
        target = step["target_id"]
        event = step.get("event", {})
        occurrence = step.get("occurrence")
        if step["action"] == "create" and cal["provider"] == "icloud":
            target = cal["target"].rstrip("/") + "/" + target + ".ics"
        old = await self.read(cal, target)
        if step["action"] == "delete" and step.get("link") and (not old or old.get("status") == "cancelled"):
            # Unproject of a drive already gone (404 or cancelled): nothing to send.
            return {"action": "delete", "target": target, "absent": True, "resource_ref": source_ref("res", cal["ref"], target)}
        if step["action"] == "create" and old:
            raise CalendarError("version_conflict")
        repair_of = (step.get("projection") or {}).get("repair_of")
        if repair_of:
            # Absent from a read window is not deleted: the old drive is read by its ID, and one that still
            # exists (moved or restored) is never replaced; a failed read raises before any write.
            gone = await self.read(cal, repair_of["target_id"])
            if gone and gone.get("status") != "cancelled":
                raise CalendarError("projection_edited")
        if step["action"] != "create":
            if not old or old.get("etag") != step["expected_etag"]:
                raise CalendarError("version_conflict")
            if cal["provider"] == "google":
                self._guard_google(cal, old, occurrence)
            else:
                self._guard_caldav(cal, target, parse(old["data"]), occurrence)
        intent = {
            "action": step["action"],
            "target": target,
            "expected_etag": step.get("expected_etag"),
            "resource_ref": source_ref("res", cal["ref"], target),
        }
        if occurrence:
            intent["scope"] = "occurrence"
        if step["action"] == "delete" and cal["provider"] == "google":
            # A deleted instance becomes a cancelled instance; the series is untouched.
            intent["desired"] = None
        elif cal["provider"] == "google":
            desired = self._google_times(event, old)
            desired.update(
                {
                    {"title": "summary", "location": "location", "description": "description"}[k]: event[k]
                    for k in ("title", "location", "description")
                    if k in event
                }
            )
            if step["action"] == "create":
                desired["id"] = target
            if invite:
                desired["attendees"] = [{"email": guest} for guest in invite["guests"]]
                intent["send_updates"] = invite["notify"]
            projection = step.get("projection")
            if projection:
                # The one marker: an opaque random link ID, nothing derived from the child or the lesson.
                desired["extendedProperties"] = {"private": {LINK_PROPERTY: projection["link_id"]}}
                if projection["visibility"] == "private":
                    desired["visibility"] = "private"
            intent["desired"] = desired
        elif occurrence:
            if step["action"] == "delete":
                # Cancelling one occurrence rewrites the resource; the resource itself is not deleted.
                intent["action"] = "update"
            try:
                wanted = occurrence["original_start"]
                rid = datetime.fromisoformat(wanted) if "T" in wanted else date.fromisoformat(wanted)
                data = (
                    cancel_single(old["data"], rid)
                    if step["action"] == "delete"
                    else edit_single(old["data"], rid, *self._caldav_times(event), **self._caldav_text(event))
                )
            except CalendarError:
                raise
            except Exception:
                # Nothing was sent yet: an unexpected shape is a definite refusal, never an unknown write.
                raise CalendarError("unsupported_recurrence") from None
            intent["desired"] = base64.b64encode(data).decode()
            intent["semantic_hash"] = semantic_hash(data)
        elif step["action"] == "delete":
            intent["desired"] = None
        else:
            original = (
                parse(old["data"])
                if old
                else Calendar.from_ical(create_event(step["target_id"], *self._caldav_times(event), event["title"]))
            )
            e = master(original)
            start, end = self._caldav_times(event)
            _set(e, "DTSTART", start)
            _set(e, "DTEND", end)
            e.pop("DURATION", None)
            for name, value in self._caldav_text(event).items():
                _set(e, name.upper(), value)
            _touch(e)
            intent["desired"] = base64.b64encode(original.to_ical()).decode()
            intent["semantic_hash"] = semantic_hash(original.to_ical())
        return intent

    async def _prepare_series(self, cal, step):
        """A series edit (ANYM-556): the instance the plan named is read and guarded as an occurrence edit, then its
        master by the instance's own recurringEventId; the write goes to the master, or creates the replacement."""
        series, occurrence = step["series"], step.get("occurrence")
        if cal["provider"] != "google":
            raise CalendarError("capability_unsupported")
        if not occurrence or series.get("scope") not in ("all", "following"):
            raise CalendarError("capability_unverified")
        replacing = step["action"] == "create"
        if replacing and await self.read(cal, step["target_id"]):
            raise CalendarError("version_conflict")
        instance = await self.read(cal, series["instance_id"] if replacing else step["target_id"])
        if not instance or instance.get("etag") != (series["instance_etag"] if replacing else step["expected_etag"]):
            raise CalendarError("version_conflict")
        self._guard_google(cal, instance, occurrence)
        master = await self.read(cal, instance["recurringEventId"])
        if not master or master.get("status") == "cancelled" or not master.get("recurrence"):
            raise CalendarError("version_conflict")
        if master.get("attendees") or master.get("eventType", "default") != "default":
            raise CalendarError("capability_unverified")
        if not master.get("etag") or not master.get("iCalUID"):
            raise CalendarError("conditional_write_unavailable")
        # The master as the plan saw it: changed since the preview, or since the replacement was made from it.
        if series.get("master_etag") and master["etag"] != series["master_etag"]:
            raise CalendarError("version_conflict")
        if master.get("updated") and instant(master["updated"]) > instant(series["planned_at"]):
            raise CalendarError("version_conflict")
        zone = recurrence.zone_of(master)
        boundary = recurrence.boundary_of(occurrence, zone)
        adapter = await self.adapter(cal)
        exceptions = [e for e in await adapter.exceptions(cal["target"], master["iCalUID"])
                      if isinstance(e, dict) and e.get("recurringEventId") == master["id"]]
        cancel = step["action"] == "delete"
        recurrence.check_exceptions(exceptions, zone, boundary, series["scope"], cancel)
        intent = {"action": "update", "target": master["id"], "expected_etag": master["etag"],
                  "resource_ref": source_ref("res", cal["ref"], master["id"]), "scope": "series",
                  "series_etag": master["etag"]}
        if replacing:
            intent.update(action="create", target=step["target_id"], expected_etag=None,
                          resource_ref=source_ref("res", cal["ref"], step["target_id"]),
                          desired=recurrence.replacement(master, occurrence, step["event"], zone, step["target_id"]))
        elif series["scope"] == "all":
            if cancel:
                intent.update(action="delete", desired=None)
            else:
                intent["desired"] = recurrence.edit_all(master, occurrence, step["event"], zone)
        else:
            lines = recurrence.truncated(master, zone, boundary)
            if lines is None:
                # From its first occurrence on, nothing of the series is left: the master itself goes.
                intent.update(action="delete", desired=None)
            else:
                intent["desired"] = {"recurrence": lines}
        return intent

    @staticmethod
    def _caldav_times(event):
        if event.get("all_day"):
            return date.fromisoformat(event["start"]), date.fromisoformat(event["end"])
        return instant(event["start"]), instant(event["end"])

    @staticmethod
    def _caldav_text(event):
        fields = (("summary", "title"), ("location", "location"), ("description", "description"))
        return {name: event[key] for name, key in fields if key in event}

    async def mutate(self, cal, intent):
        adapter = await self.adapter(cal)
        if cal["provider"] == "icloud":
            if intent["action"] == "delete":
                await adapter.delete(intent["target"], intent["expected_etag"])
            else:
                await adapter.put(
                    intent["target"],
                    base64.b64decode(intent["desired"]),
                    etag=intent["expected_etag"],
                    create=intent["action"] == "create",
                )
        elif intent["action"] == "create":
            await adapter.create(cal["target"], intent["desired"], send_updates=intent.get("send_updates", "none"))
        elif intent["action"] == "update":
            await adapter.patch(cal["target"], intent["target"], intent["desired"], intent["expected_etag"])
        else:
            await adapter.delete(cal["target"], intent["target"], intent["expected_etag"])

    async def verify(self, cal, intent):
        value = await self.read(cal, intent["target"])
        if intent["action"] == "delete":
            return (
                {"deleted": True, "resource_ref": intent["resource_ref"]}
                if value is None or value.get("status") == "cancelled"
                else None
            )
        if not value:
            return None
        if cal["provider"] == "icloud":
            match = semantic_hash(value["data"]) == intent["semantic_hash"]
        else:

            def equal(k, v):
                if k in ("start", "end"):
                    return same_time(value.get(k) or {}, v)
                if k == "attendees":
                    return same_guests(value.get(k) or [], v)
                return value.get(k) == v

            match = all(equal(k, v) for k, v in intent["desired"].items())
        return (
            {"resource_ref": intent["resource_ref"], "verified": True, "etag": value.get("etag")}
            if match
            else None
        )
