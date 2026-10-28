from datetime import date, datetime, time, timedelta, timezone
import hashlib
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg.types.json import Jsonb
from family_calendar.errors import CalendarError
from .store import digest, instant, person_uuid, ref

READ = ["agenda:read", "availability:read", "event:read", "assignment:read"]
WRITE = ["plan:create", "plan:execute", "event:create", "event:update", "event:delete", "assignment:write"]


def window(start, end):
    a, b = instant(start), instant(end)
    if not a < b or b - a > timedelta(days=31):
        raise CalendarError("invalid_window")
    return a, b


def clock():
    return datetime.now(timezone.utc)


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


TIME_FORMAT = (
    "ISO 8601 date-time with offset or Z, e.g. 2026-10-20T10:00:00+08:00; plan event "
    "start/end may omit the offset (YYYY-MM-DDTHH:MM[:SS]) and are then wall time in "
    "the event's IANA timezone"
)
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


# A live link that `project` leaves alone, by its drift status (ANYM-436); moved and missing drives are planned.
DRIFT_SKIP = {
    "in_sync": "already_projected",
    "unknown": "already_projected",
    "projection_edited": "projection_edited",
    "source_cancelled": "use_unproject",
}
EVENT_WRITE = (
    "guest-free single event, all-day event (date range, exclusive end), or one occurrence of a "
    "recurring series (update or cancel, chosen by its event_ref); series, this-and-following and "
    "invitations are not written by plan and apply; calendar_schedule move and cancel take series all or following "
    "on Google calendars (series_write); calendar_schedule create invites guests where a calendar's guests.supported "
    "is true"
)
# Series scopes of calendar_schedule move and cancel, per provider (ANYM-556); synthetic evidence until the owner's
# live run. iCloud refuses following and all with capability_unsupported and a next for this occurrence only.
SERIES_WRITE = {
    "google": {"scopes": ["this", "following", "all"], "tool": "calendar_schedule"},
    "icloud": {"scopes": ["this"], "error": "capability_unsupported"},
}
# Per provider, exactly what the write path implements; both rest on synthetic evidence until the owner's live run.
PROVIDER_WRITE = {
    provider: {"occurrence_write": True, "all_day_write": True} for provider in ("google", "icloud")
}
# Guests of a new event, per provider (ANYM-555): only calendar_schedule create sends them, and only Google invites;
# each calendar also needs the owner's invitation opt-in (schema 5, `service allow-invitations`).
GUEST_WRITE = {
    "google": {"supported": True, "tool": "calendar_schedule", "actions": ["create"], "notify": ["none", "all"]},
    "icloud": {"supported": False, "error": "capability_unsupported"},
}
# Mixed into the scope hash of a plan with guests: a worker that predates guests computes another hash and refuses
# the plan (scope_changed) instead of creating the event without them.
INVITE_SCOPE = "invitations-v1"
# Recurring-series scopes of calendar_schedule move and cancel (ANYM-556), Google only: `this` is the occurrence
# edit above; `all` writes the master; `following` ends the master before the occurrence and, for a move, first
# creates the replacement series. Mixed into the scope hash like INVITE_SCOPE, so a worker that predates series
# refuses the plan (scope_changed) instead of writing only the one occurrence.
SERIES_SCOPES = ("this", "following", "all")
SERIES_SCOPE = "series-v1"
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


class FamilyService:
    def __init__(self, store):
        self.store = store

    def principal(self, key=None, principal_id=None):
        field, value = (
            ("id", principal_id)
            if principal_id
            else ("key_hash", hashlib.sha256((key or "").encode()).hexdigest())
        )
        # Column selection is fixed, never user input.
        p = self.store.one(
            f"SELECT * FROM principals WHERE {field}=%s AND enabled AND expires_at>now()", (value,)
        )
        if not p:
            raise CalendarError("not_found_or_forbidden")
        m = self.store.one(
            "SELECT * FROM memberships WHERE group_id=%s AND person_id=%s AND enabled",
            (p["group_id"], p["person_id"]),
        )
        if not m:
            raise CalendarError("not_found_or_forbidden")
        return dict(p, role=m["role"], member_version=m["version"])

    def authorize(self, key, calendar_ref, scope, *, principal_id=None):
        p = self.principal(key, principal_id)
        c = self.store.one(
            "SELECT c.*,cn.provider,cn.enabled AS connection_enabled,cn.version AS connection_version, "
            "g.scopes AS grant_scopes,g.visibility AS grant_visibility,g.version AS grant_version, "
            "a.visibility AS agent_visibility FROM calendars c JOIN connections cn ON cn.id=c.connection_id "
            "JOIN calendar_grants g ON g.calendar_ref=c.ref JOIN agent_calendars a ON a.calendar_ref=c.ref "
            "WHERE c.ref=%s AND g.group_id=%s AND g.person_id=%s AND a.principal_id=%s",
            (calendar_ref, p["group_id"], p["person_id"], p["id"]),
        )
        if not c or scope not in p["scopes"] or scope not in c["grant_scopes"] or not c["connection_enabled"]:
            raise CalendarError("not_found_or_forbidden")
        c["visibility"] = (
            "busy_only" if "busy_only" in (c["agent_visibility"], c["grant_visibility"]) else "details"
        )
        if scope in WRITE:
            if c["visibility"] == "busy_only" or p["role"] in ("dependent", "helper"):
                raise CalendarError("not_found_or_forbidden")
            if c["privilege"] != "write":
                raise CalendarError("provider_readonly")
            if not c["write_enabled"]:
                raise CalendarError("write_not_enabled")
        return p, c

    def _scope_hash(self, p, calendars, invitations=False, series=False):
        return digest(
            [
                p["id"],
                p["version"],
                p["member_version"],
                p["role"],
                p["scopes"],
                [
                    (c["ref"], c["version"], c["connection_version"], c["grant_version"], c["visibility"])
                    for c in calendars
                ],
            ]
            + ([INVITE_SCOPE] if invitations else [])
            + ([SERIES_SCOPE] if series else [])
        )

    def invitations_enabled(self, calendar_ref):
        """The owner's invitation opt-in of a calendar; false before schema 5 and its db-roles grant."""
        if not self.store.one(
            "SELECT coalesce(has_table_privilege(to_regclass('invitation_opt_ins'),'SELECT'),false) AS ok"
        )["ok"]:
            return False
        row = self.store.one("SELECT enabled FROM invitation_opt_ins WHERE calendar_ref=%s", (calendar_ref,))
        return bool(row and row["enabled"])

    def guests(self, calendar):
        """What calendar_capabilities says about guests on this calendar."""
        support = GUEST_WRITE[calendar["provider"]]
        if not support["supported"]:
            return support
        if self.invitations_enabled(calendar["calendar_ref"]):
            return dict(support, enabled=True)
        return dict(support, enabled=False, error="invitations_not_enabled",
                    owner_step="service allow-invitations --calendar " + calendar["calendar_ref"])

    def calendars(self, key):
        p = self.principal(key)
        out = []
        for c in self.store.query(
            "SELECT c.*,cn.provider,cn.enabled AS connection_enabled,g.scopes AS grant_scopes,"
            "g.visibility AS grant_visibility,a.visibility AS agent_visibility FROM calendars c "
            "JOIN connections cn ON cn.id=c.connection_id JOIN calendar_grants g ON g.calendar_ref=c.ref "
            "JOIN agent_calendars a ON a.calendar_ref=c.ref "
            "WHERE g.group_id=%s AND g.person_id=%s AND a.principal_id=%s",
            (p["group_id"], p["person_id"], p["id"]),
        ):
            read_scopes = set(p["scopes"]) & set(c["grant_scopes"]) & {"agenda:read", "availability:read"}
            if not read_scopes or not c["connection_enabled"]:
                continue
            c["visibility"] = (
                "busy_only" if "busy_only" in (c["agent_visibility"], c["grant_visibility"]) else "details"
            )
            out.append(
                {
                    "calendar_ref": c["ref"],
                    "provider": c["provider"],
                    "label": c["label"] if c["visibility"] == "details" else "Calendar",
                    "timezone": c["timezone"],
                    "visibility": c["visibility"],
                    "write_enabled": c["write_enabled"],
                    "identity_basis": "provider_resource",
                    "primary": None,
                    "duplicate_of": None,
                    "allowed_event_actions": self._event_actions(p, c),
                }
            )
        return {"calendars": out, "group_id": p["group_id"]}

    def _event_actions(self, principal, calendar):
        if (
            principal["role"] in ("dependent", "helper")
            or calendar["visibility"] == "busy_only"
            or calendar["privilege"] != "write"
            or not calendar["write_enabled"]
        ):
            return []
        scopes = set(principal["scopes"]) & set(calendar["grant_scopes"])
        return [
            action
            for action in ("create", "update", "delete")
            if {"plan:create", "plan:execute", "event:" + action} <= scopes
        ]

    def family(self, key):
        p = self.principal(key)
        result = {
            "group_id": p["group_id"],
            "members": self.store.query(
                "SELECT m.person_id,m.role,p.label FROM memberships m JOIN people p ON p.id=m.person_id "
                "WHERE m.group_id=%s AND m.enabled ORDER BY m.person_id",
                (p["group_id"],),
            ),
        }
        for member in result["members"]:
            rows = self.store.query(
                "SELECT calendar_ref FROM bindings WHERE group_id=%s AND person_id=%s ORDER BY calendar_ref",
                (p["group_id"], member["person_id"]),
            )
            refs = []
            for row in rows:
                try:
                    self.authorize(key, row["calendar_ref"], "availability:read")
                except CalendarError:
                    continue
                refs.append(row["calendar_ref"])
            member["availability_calendar_refs"] = refs
            member["availability_sources_status"] = (
                "unbound" if not rows else "configured" if len(refs) == len(rows) else "inaccessible"
            )
        if self.rule_reader(p) and not self.store.guardian_readiness():
            rules = self.store.query(
                "SELECT * FROM guardian_rules WHERE group_id=%s ORDER BY activity,duty,id", (p["group_id"],)
            )
            for member in result["members"]:
                if member["role"] == "dependent":
                    member["guardian_rules"] = [
                        self.rule_view(p, r) for r in rules if r["dependent_id"] == member["person_id"]
                    ]
        return result

    @staticmethod
    def rule_reader(p):
        """The one rule-read policy: adult roles of the rule's group holding assignment:read."""
        return p["role"] in ("group_admin", "coordinator", "adult") and "assignment:read" in p["scopes"]

    def _can(self, p, calendar_ref, scope, details=False):
        try:
            _, c = self.authorize(None, calendar_ref, scope, principal_id=p["id"])
        except CalendarError:
            return False
        return not details or c["visibility"] == "details"

    def rule_view(self, p, rule):
        """A rule as `p` may see it: calendars it cannot read are null; not_ready_reason says why no drive is planned."""
        visible = {
            ref: self._can(p, ref, "agenda:read") or self._can(p, ref, "availability:read")
            for ref in (rule["source_calendar_ref"], rule["target_calendar_ref"])
        }
        reason = (
            "disabled"
            if not rule["enabled"]
            else "target_not_visible"
            if not visible[rule["target_calendar_ref"]]
            else "target_not_writable"
            if not self._can(p, rule["target_calendar_ref"], "event:create")
            else "source_not_visible"
            if not self._can(p, rule["source_calendar_ref"], "agenda:read", details=True)
            else None
        )
        view = {k: rule[k] for k in ("activity", "duty", "guardian_id", "lead_minutes", "title", "visibility")}
        return dict(
            view,
            rule_id=rule["id"],
            source_calendar_ref=rule["source_calendar_ref"] if visible[rule["source_calendar_ref"]] else None,
            target_calendar_ref=rule["target_calendar_ref"] if visible[rule["target_calendar_ref"]] else None,
            copy_location=rule["copy_location"],
            enabled=rule["enabled"],
            version=rule["version"],
            projection_ready=reason is None,
            not_ready_reason=reason,
        )

    def capabilities(self, key):
        calendars = self.calendars(key)
        calendars["calendars"] = [dict(c, guests=self.guests(c)) for c in calendars["calendars"]]
        return dict(
            calendars,
            permissions=sorted(self.principal(key)["scopes"]),
            constraints={
                "event_write": EVENT_WRITE,
                "time_format": TIME_FORMAT,
                "occurrence_write": True,
                "all_day_write": True,
                "provider_write": PROVIDER_WRITE,
                "series_write": SERIES_WRITE,
                "recurrence_write": False,
                "future_split": False,
                "invitations": False,
                "refresh": "bounded; pending jobs must be polled with refresh=false",
                "window_limit_days": 31,
                "calendar_limit": 32,
                "event_limit": 500,
                "cache_max_age_seconds": 300,
                "refresh_wait_seconds": 20,
                "plan_action_limit": 8,
                "plan_step_limit": 16,
                "plan_ttl_seconds": 600,
                "atomic_cross_provider": False,
                "availability_sources": "member bindings plus additional_calendar_refs; unbound members remain unknown",
            },
            features={
                "agenda": True,
                "slots": True,
                "assignments": True,
                "event_create": True,
                "event_update": EVENT_WRITE,
                "event_delete": EVENT_WRITE,
                "recurrence_read": True,
                "occurrence_write": True,
                "all_day_write": True,
                "recurrence_write": False,
                "future_split": False,
                "invitations": False,
                "refresh": "bounded on-demand polling",
                "window_limit_days": 31,
                "event_limit": 500,
                "atomic_cross_provider": False,
                # Plans carry drives with their lesson only for a caller that sends carry_projections (ANYM-435).
                "carry_projections": True,
                # Agenda marks linked drives with their drift only for a caller that sends projection_status (ANYM-436).
                "projection_status": True,
                # calendar_schedule: create, move or cancel one event in one call, with a dry-run token (ANYM-552).
                "schedule": True,
            },
            # Top level: the common gateway drops backend `features` but passes this through.
            guardian_projections=self.guardian_projections(),
        )

    def guardian_projections(self):
        reason = self.store.guardian_readiness()
        return {"available": True} if not reason else {"available": False, "reason": reason}

    def agenda(self, key, calendar_refs, start, end, scope="agenda:read", projection_status=False):
        a, b = window(start, end)
        if not calendar_refs or len(calendar_refs) > 32 or len(set(calendar_refs)) != len(calendar_refs):
            raise CalendarError("invalid_calendar_refs")
        coverage = []
        events = []
        # Before migration 2 and its grants no link exists: agenda never reads the new tables then.
        projection_status = projection_status and not self.store.guardian_readiness()
        for calendar_ref in calendar_refs:
            p, cal = self.authorize(key, calendar_ref, scope)
            cov = self.store.one("SELECT * FROM coverage WHERE calendar_ref=%s", (calendar_ref,))
            complete = bool(
                cov
                and cov["status"] == "complete"
                and cov["start_at"] <= a
                and cov["end_at"] >= b
                and cov["checked_at"] > datetime.now(timezone.utc) - timedelta(minutes=5)
            )
            coverage.append(
                {
                    "calendar_ref": calendar_ref,
                    "status": "complete" if complete else "unknown",
                    "checked_at": cov["checked_at"].isoformat() if cov else None,
                    "error": cov.get("error") if cov else None,
                }
            )
            try:
                zone = ZoneInfo(cal["timezone"])
            except (ZoneInfoNotFoundError, TypeError, ValueError):
                zone = None  # a bad stored zone must not fail the whole agenda; UTC fields remain
            # The label follows the calendars() visibility rule: busy_only callers never see the real one.
            label = cal["label"] if cal["visibility"] == "details" else "Calendar"
            for e in self.store.query(
                "SELECT * FROM occurrences WHERE calendar_ref=%s AND start_at<%s AND end_at>%s "
                "ORDER BY start_at LIMIT 501",
                (calendar_ref, b, a),
            ):
                event = {
                    "calendar_ref": calendar_ref,
                    "start": e["start_at"].isoformat(),
                    "end": (e["start_at"] if e["details"].get("instant") else e["end_at"]).isoformat(),
                    "busy": e["busy"],
                    "calendar_label": label,
                }
                if zone:
                    event.update(
                        local_start=e["start_at"].astimezone(zone).isoformat(),
                        local_end=(e["start_at"] if e["details"].get("instant") else e["end_at"])
                        .astimezone(zone)
                        .isoformat(),
                        timezone=cal["timezone"],
                    )
                if cal["visibility"] == "details":
                    event.update(event_ref=e["ref"], **e["details"])
                    # Links are rule data: only for a caller that opts in under its rule-read ceiling (ANYM-436).
                    link = projection_status and self.rule_reader(p) and self.store.one(
                        # Link only: the lesson is read in _drift, after its own authorization and freshness.
                        "SELECT l.* FROM projection_links l JOIN guardian_rules r ON r.id=l.rule_id "
                        "WHERE r.group_id=%s AND l.state<>'removed' AND l.target_calendar_ref=%s "
                        "AND l.target_resource_ref=%s",
                        (p["group_id"], calendar_ref, e["resource_ref"]),
                    )
                    if link:
                        event["projection"] = {
                            "link_id": link["id"],
                            "duty": link["duty"],
                            "status": self._drift(p, link),
                            "operation_id": link["operation_id"],
                        }
                events.append(event)
        if len(events) > 500:
            raise CalendarError("coverage_incomplete")
        return {
            "events": sorted(events, key=lambda x: x["start"]),
            "coverage": coverage,
            "required_sources_complete": all(c["status"] == "complete" for c in coverage),
        }

    def slots(self, key, member_ids, start, end, duration_minutes, additional_calendar_refs=None):
        p = self.principal(key)
        a, b = window(start, end)
        if "availability:read" not in p["scopes"] or not member_ids or len(member_ids) > 32:
            raise CalendarError("not_found_or_forbidden")
        if not isinstance(duration_minutes, int) or not 5 <= duration_minutes <= 1440:
            raise CalendarError("invalid_duration")
        additional_calendar_refs = additional_calendar_refs or []
        if len(additional_calendar_refs) > 32 or len(set(additional_calendar_refs)) != len(
            additional_calendar_refs
        ):
            raise CalendarError("invalid_calendar_refs")
        refs = []
        missing = []
        checked = []
        for member in member_ids:
            if not self.store.one(
                "SELECT 1 FROM memberships WHERE group_id=%s AND person_id=%s AND enabled",
                (p["group_id"], member),
            ):
                raise CalendarError("not_found_or_forbidden")
            rows = self.store.query(
                "SELECT calendar_ref FROM bindings WHERE group_id=%s AND person_id=%s ORDER BY calendar_ref",
                (p["group_id"], member),
            )
            if not rows:
                missing.append(member)
            checked.append({"member_id": member, "calendar_refs": sorted(r["calendar_ref"] for r in rows)})
            refs.extend(r["calendar_ref"] for r in rows)
        refs = list(dict.fromkeys(refs + additional_calendar_refs))
        agenda = (
            self.agenda(key, list(dict.fromkeys(refs)), start, end, "availability:read")
            if refs
            else {"events": [], "coverage": [], "required_sources_complete": False}
        )
        result = {
            "availability": "unknown",
            "verified_slots": [],
            "coverage": agenda["coverage"],
            "required_sources_complete": False,
            "configured_sources_complete": agenda["required_sources_complete"] and not missing,
            "unbound_members": missing,
            "availability_scope": "configured_and_explicit_sources"
            if additional_calendar_refs
            else "configured_bindings",
            "member_sources_complete": "unknown",
            "checked_member_sources": checked,
            "configured_bindings_availability": "unknown",
            "candidate_slots": [],
            "source_selection": {
                "scope": "configured_and_explicit_sources_only",
                "calendar_refs": refs,
                "member_sources": checked,
                "additional_calendar_refs": additional_calendar_refs,
            },
        }
        if not result["configured_sources_complete"]:
            return result
        busy = [
            (max(a, instant(e["start"])), min(b, instant(e["end"]))) for e in agenda["events"] if e["busy"]
        ]
        for row in self.store.query(
            "SELECT start_at,end_at FROM assignments WHERE group_id=%s AND person_id=ANY(%s) "
            "AND status='assigned' AND start_at<%s AND end_at>%s",
            (p["group_id"], member_ids, b, a),
        ):
            busy.append((max(a, row["start_at"]), min(b, row["end_at"])))
        cursor = a
        for left, right in sorted(busy) + [(b, b)]:
            if left - cursor >= timedelta(minutes=duration_minutes):
                result["candidate_slots"].append({"start": cursor.isoformat(), "end": left.isoformat()})
            cursor = max(cursor, right)
        # Bindings identify checked sources, not an owner-reviewed inventory of
        # every source of a person's availability. Never promote that subset to
        # a claim that the member is free in every calendar.
        result["configured_bindings_availability"] = "verified"
        return result

    def linked_member(self, group_id, person_id):
        """The member the owner linked to person_id in group_id (ANYM-554), or None. Before migration 6 and its grant
        no person is linked, so names and member IDs keep working."""
        try:
            row = self.store.one("SELECT member_id FROM member_people WHERE group_id=%s AND person_uuid=%s",
                                 (group_id, person_id))
        except (psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege):
            return None
        return row["member_id"] if row else None

    def find_members(self, p, request):
        """calendar_find_time's people (ANYM-553) as enabled members of principal p (availability:read checked by
        the caller): a member ID, or a member name the calendar knows (case and spacing ignored). One exact name
        resolves; several exact ones, or only names containing it, match several. Every name is looked up first: an
        unknown one is not_found, else the first that matches several is a selection whose candidates retry the
        request with that member and every resolved name by ID; `request["window"]` is already validated, so a
        retry repeats no caller text but names that matched member labels. The caller comes first unless
        exclude_me. Returns [{member_id, label, role}]."""
        members = self.store.query(
            "SELECT m.person_id AS member_id,p.label,m.role FROM memberships m JOIN people p ON p.id=m.person_id "
            "WHERE m.group_id=%s AND m.enabled ORDER BY p.label,m.person_id",
            (p["group_id"],),
        )

        def norm(text):
            return " ".join(text.split()).casefold()

        asked = []
        for name in request["people"]:
            wanted = norm(name)
            exact = [m for m in members if m["member_id"] == name]
            if not exact and person_uuid(name):
                # A messaging person (ANYM-554): only the owner's link to an enabled member of this calendar; no
                # name match and no messaging read, so an unlinked or hidden person is not_found.
                linked = self.linked_member(p["group_id"], person_uuid(name))
                exact = [m for m in members if m["member_id"] == linked]
                if not exact:
                    raise CalendarError("not_found", details={"next": {"tool": "calendar_members", "arguments": {}}})
            exact = exact or [m for m in members if norm(m["label"]) == wanted]
            matches = exact or [m for m in members if wanted and wanted in norm(m["label"])]
            if not matches:
                raise CalendarError("not_found", details={"next": {"tool": "calendar_members", "arguments": {}}})
            # Only one exact name or ID resolves; one partial match is still a selection.
            asked.append((matches, len(exact) == 1))
        people = [m[0]["member_id"] if one else name for (m, one), name in zip(asked, request["people"])]
        for i, (matches, one) in enumerate(asked):
            if one:
                continue
            # The title stays out, since a relayed next must not repeat it.
            retry = {k: v for k, v in request.items() if k not in ("title", "refresh")}
            raise CalendarError("selection_required", details={"candidates": [
                {"id": m["member_id"], "kind": "member", "label": m["label"][:200],
                 "next": {"tool": "calendar_find_time",
                          "arguments": {**retry, "people": [*people[:i], m["member_id"], *people[i + 1:]]}}}
                for m in matches[:10]
            ]})
        me = [] if request.get("exclude_me") else [m for m in members if m["member_id"] == p["person_id"]]
        found = list({m["member_id"]: m for m in me + [m[0] for m, _ in asked]}.values())
        if not found:
            raise CalendarError("invalid_arguments", [{"field": "people", "reason": "required"}])
        return found

    def plan(self, key, actions, carry_projections=False, invitations=False, series=False):
        p = self.principal(key)
        if not isinstance(actions, list) or not 1 <= len(actions) <= 8:
            raise CalendarError("invalid_actions")
        calendars = []
        prepared = []
        touched = set()
        selected = set()
        sources, source_calendars, skipped, activities, projected = [], [], [], set(), set()
        # Remote events this plan changes: one action per drive, whether by unproject or an ordinary write.
        targets, unprojected, carried = set(), False, False
        allowed = {"action", "calendar_ref", "event_ref", "event", "activity", "operation_id"}
        if invitations:
            # calendar_schedule only (ANYM-555): calendar_plan never takes guests.
            allowed.add("invite")
        if series:
            # calendar_schedule only (ANYM-556): a series scope of a move or cancel, and the undo of a partial
            # following move (`replacement`: the series_ref of the replacement series it created).
            allowed.update(("series", "replacement"))
        for action in actions:
            if not isinstance(action, dict) or set(action) - allowed:
                raise CalendarError("invalid_action")
            kind = action.get("action")
            if kind not in ("create", "update", "delete", "project", "unproject"):
                raise CalendarError("unsupported_action")
            if kind == "unproject":
                unprojected = True
                for step, cal, source, scope in self._unproject(key, p, action, skipped):
                    if (cal["ref"], step["target_id"]) in targets:
                        raise CalendarError("duplicate_resource_action")
                    targets.add((cal["ref"], step["target_id"]))
                    if source["ref"] not in [s["calendar_ref"] for s in sources]:
                        sources.append({"calendar_ref": source["ref"], "scope": scope})
                        source_calendars.append(source)
                    prepared.append(dict(step, ordinal=len(prepared)))
                    calendars.append(cal)
                continue
            if "operation_id" in action or not isinstance(action.get("calendar_ref"), str):
                raise CalendarError("invalid_action")
            if kind == "project":
                label = action.get("activity")
                if not isinstance(label, str) or not 1 <= len(label) <= 64 or "event" in action:
                    raise CalendarError("invalid_action")
                if action.get("event_ref") in projected:
                    raise CalendarError("duplicate_resource_action")
                projected.add(action.get("event_ref"))
                source, scope, steps, skips, labels = self._project(key, p, action)
                if source["ref"] not in [s["calendar_ref"] for s in sources]:
                    sources.append({"calendar_ref": source["ref"], "scope": scope})
                    source_calendars.append(source)
                for step, cal in steps:
                    if step["action"] == "update":
                        # A reprojected drive moves: its cached occurrence is no overlap of its new span.
                        if (cal["ref"], step["target_id"]) in targets:
                            raise CalendarError("duplicate_resource_action")
                        targets.add((cal["ref"], step["target_id"]))
                        selected.update(
                            o["ref"]
                            for o in self.store.query(
                                "SELECT ref FROM occurrences WHERE calendar_ref=%s AND resource_ref=%s",
                                (cal["ref"], step["resource_ref"]),
                            )
                        )
                    prepared.append(dict(step, ordinal=len(prepared)))
                    calendars.append(cal)
                skipped += skips
                activities |= labels
                continue
            label = action.get("activity")
            if "activity" in action and (
                kind != "create" or not carry_projections or not isinstance(label, str) or not 1 <= len(label) <= 64
            ):
                raise CalendarError("invalid_action")
            _, cal = self.authorize(key, action.get("calendar_ref"), "plan:create")
            self.authorize(key, cal["ref"], "event:" + kind)
            calendars.append(cal)
            step = {
                "action": kind,
                "calendar_ref": cal["ref"],
                "provider": cal["provider"],
                "ordinal": len(prepared),
            }
            if "invite" in action:
                guests = action["invite"]
                if (kind != "create" or not GUEST_WRITE[cal["provider"]]["supported"] or not isinstance(guests, dict)
                        or guests.get("notify") not in ("none", "all") or not isinstance(guests.get("guests"), list)
                        or not 1 <= len(guests["guests"]) <= 20):
                    raise CalendarError("capability_unsupported")
                if not self.invitations_enabled(cal["ref"]):
                    raise CalendarError("invitations_not_enabled")
                step["invite"] = {"guests": list(guests["guests"]), "notify": guests["notify"]}
            if kind != "delete":
                step["event"] = validate_event(action.get("event"), f"actions/{len(prepared)}/event")
                a, b = event_span(step["event"], cal["timezone"])
                step["span"] = {"start": a.isoformat(), "end": b.isoformat()}
            if kind == "create":
                if not step["event"].get("title"):
                    raise CalendarError("invalid_event")
                step["resource_ref"] = ref("res")
                step["target_id"] = uuid_token()
            elif "replacement" in action:
                if kind != "delete":
                    raise CalendarError("invalid_action")
                step = self._undo_step(p, cal, action["replacement"], step)
                if (cal["ref"], step["target_id"]) in targets:
                    raise CalendarError("duplicate_resource_action")
                targets.add((cal["ref"], step["target_id"]))
            else:
                row = self.store.one(
                    "SELECT o.*,r.etag,r.target FROM occurrences o JOIN resources r ON r.ref=o.resource_ref "
                    "WHERE o.ref=%s AND o.calendar_ref=%s",
                    (action.get("event_ref"), cal["ref"]),
                )
                if not row:
                    raise CalendarError("not_found_or_forbidden")
                if row["resource_ref"] in touched or (cal["ref"], row["target"]) in targets:
                    raise CalendarError("duplicate_resource_action")
                touched.add(row["resource_ref"])
                targets.add((cal["ref"], row["target"]))
                selected.add(action["event_ref"])
                details = row["details"]
                if details.get("guests") or details.get("special"):
                    raise CalendarError("capability_unverified")
                if details.get("recurring"):
                    # One occurrence only: a series-wide target (a master, or a row stored without
                    # an occurrence identity) has no original start to name.
                    if not (details.get("series_ref") and details.get("original_start")):
                        raise CalendarError("capability_unverified")
                    if kind == "update" and bool(step["event"].get("all_day")) != bool(details.get("all_day")):
                        raise CalendarError("capability_unverified")
                    step["occurrence"] = {
                        "series_ref": details["series_ref"],
                        "original_start": details["original_start"],
                    }
                if not row["etag"]:
                    raise CalendarError("conditional_write_unavailable")
                before = {
                    "start": row["start_at"].isoformat(),
                    # The stored instant span is internal; the source event ends at its start.
                    "end": (row["start_at"] if details.get("instant") else row["end_at"]).isoformat(),
                    **details,
                }
                if details.get("all_day"):
                    # Stored at local midnight; the event itself is a date range.
                    zone = ZoneInfo(cal["timezone"])
                    for field, stored in (("start", row["start_at"]), ("end", row["end_at"])):
                        before[field] = stored.astimezone(zone).date().isoformat()
                step.update(
                    resource_ref=row["resource_ref"],
                    target_id=row["target"],
                    expected_etag=row["etag"],
                    before=before,
                )
                scope = action.get("series", "this")
                if scope not in SERIES_SCOPES or (scope != "this" and "occurrence" not in step):
                    raise CalendarError("invalid_action")
                if scope != "this":
                    steps = self._series_steps(key, cal, step, scope, len(prepared))
                    # One calendar entry per step, as validate_plan recomputes the scope hash.
                    calendars.extend([cal] * (len(steps) - 1))
                    prepared.extend(steps)
                    continue
            prepared.append(step)
            if kind == "create" and label:
                # A new lesson and its drives in one plan; the drives wait for the lesson's verified create.
                if self.store.guardian_readiness():
                    raise CalendarError("capability_unverified")
                if not self.rule_reader(p):
                    raise CalendarError("not_found_or_forbidden")
                source, scope = self._source(key, cal["ref"])
                if step["event"].get("all_day"):
                    raise CalendarError("source_all_day")
                a, b = (instant(step["span"][k]) for k in ("start", "end"))
                drives, skips, labels = self._drives(
                    key, p, source, label, None, a, b, step["event"], source_step=step["ordinal"]
                )
                if not drives:
                    raise CalendarError(
                        "nothing_to_project", details={"projections_skipped": skips, "activities": sorted(labels)}
                    )
                skipped += skips
                carried = True
            elif kind != "create" and carry_projections and (found := self._carry(key, p, step, action["event_ref"], cal, skipped, selected)):
                source, scope, drives = found
                carried = True
            else:
                continue
            if source["ref"] not in [s["calendar_ref"] for s in sources]:
                sources.append({"calendar_ref": source["ref"], "scope": scope})
                source_calendars.append(source)
            for linked_step, target in drives:
                if (target["ref"], linked_step["target_id"]) in targets:
                    raise CalendarError("duplicate_resource_action")
                targets.add((target["ref"], linked_step["target_id"]))
                prepared.append(dict(linked_step, ordinal=len(prepared)))
                calendars.append(target)
        if projected and not prepared:
            raise CalendarError(
                "nothing_to_project",
                details={"projections_skipped": skipped, "activities": sorted(activities)},
            )
        if unprojected and not prepared:
            raise CalendarError("nothing_to_unproject", details={"projections_skipped": skipped})
        if len(prepared) > 16:
            raise CalendarError("too_many_steps")
        overlap_check = self._overlap_check(key, p, prepared, selected)
        plan_id = ref("plan")
        expires = datetime.now(timezone.utc) + timedelta(minutes=10)
        payload = {"steps": prepared, **({"sources": sources} if sources else {})}
        self.store.query(
            "INSERT INTO plans(id,principal_id,payload,scope_hash,expires_at) VALUES (%s,%s,%s,%s,%s)",
            (plan_id, p["id"], Jsonb(payload),
             self._scope_hash(p, calendars + source_calendars, any("invite" in s for s in prepared),
                              any("series" in s for s in prepared)), expires),
        )
        return {
            "plan_id": plan_id,
            "expires_at": expires.isoformat(),
            "steps": [self._preview(s) for s in prepared],
            "notifications": (
                "per step invite: guests of a create, emailed only with notify all; edits of events with guests are "
                "blocked" if any("invite" in s for s in prepared) else "none; events with guests are blocked"
            ),
            "atomic": False,
            "overlap_check": overlap_check,
            **({"projections_skipped": skipped} if projected or unprojected or carried else {}),
        }

    def _fresh(self, calendar_ref, start, end, after=None):
        """Complete coverage of [start, end] read in the last 5 minutes (SERVICE.md), and after `after`."""
        cov = self.store.one("SELECT * FROM coverage WHERE calendar_ref=%s", (calendar_ref,))
        since = datetime.now(timezone.utc) - timedelta(minutes=5)
        return bool(
            cov
            and cov["status"] == "complete"
            and cov["start_at"] <= start
            and cov["end_at"] >= end
            and cov["checked_at"] > (max(since, after) if after else since)
        )

    def _details(self, p, calendar_ref):
        return self._can(p, calendar_ref, "agenda:read", details=True) or self._can(
            p, calendar_ref, "event:read", details=True
        )

    def _drift(self, p, link):
        """A link's status as p may know it: from fresh coverage only, and about the lesson only through p's own
        details read of its calendar (authorized apart from the drive); anything else is `unknown`."""
        target, source = link["target_calendar_ref"], link["source_calendar_ref"]
        if link["state"] != "active" or not link["target_resource_ref"] or not self._details(p, target):
            return "unknown"
        drive = self.store.one(
            "SELECT r.etag FROM occurrences o JOIN resources r ON r.ref=o.resource_ref "
            "WHERE o.calendar_ref=%s AND o.resource_ref=%s LIMIT 1",
            (target, link["target_resource_ref"]),
        )
        if not drive:
            lead = self.store.one("SELECT lead_minutes FROM guardian_rules WHERE id=%s", (link["rule_id"],))
            # Gone only in a complete read, after the link's last write, of the span the drive can occupy.
            start = link["source_start"] - timedelta(minutes=lead["lead_minutes"])
            return "projection_missing" if self._fresh(target, start, link["source_end"], link["updated_at"]) else "unknown"
        if drive["etag"] != link["etag"]:
            return "projection_edited"
        if not self._details(p, source) or not self._fresh(source, link["source_start"], link["source_end"]):
            return "unknown"
        occ = self.store.one(
            "SELECT start_at,end_at FROM occurrences WHERE ref=%s AND calendar_ref=%s",
            (link["source_event_ref"], source),
        )
        if not occ:
            # A cancelled occurrence is never published, so cancelled and deleted read alike.
            return "source_cancelled"
        if (occ["start_at"], occ["end_at"]) == (link["source_start"], link["source_end"]):
            return "in_sync"
        return "source_moved" if self._fresh(source, occ["start_at"], occ["end_at"]) else "unknown"

    def _source(self, key, calendar_ref):
        """The lesson's calendar as the principal reads it with details, and the scope that grants it."""
        for scope in ("event:read", "agenda:read"):
            try:
                _, cal = self.authorize(key, calendar_ref, scope)
            except CalendarError:
                continue
            if cal["visibility"] == "details":
                return cal, scope
        raise CalendarError("not_found_or_forbidden")

    def _project(self, key, p, action):
        """One linked create per enabled rule of the lesson's children whose Google target the principal can write."""
        if self.store.guardian_readiness():
            raise CalendarError("capability_unverified")
        if not self.rule_reader(p):
            raise CalendarError("not_found_or_forbidden")
        source, scope = self._source(key, action.get("calendar_ref"))
        occ = self.store.one(
            "SELECT o.*,r.target FROM occurrences o JOIN resources r ON r.ref=o.resource_ref "
            "WHERE o.ref=%s AND o.calendar_ref=%s",
            (action.get("event_ref"), source["ref"]),
        )
        if not occ:
            # A lesson gone since it was projected still names its links: they point to unproject.
            gone = self.store.query(
                self.LINKS + "l.source_calendar_ref=%s AND l.source_event_ref=%s AND r.activity=%s "
                "ORDER BY l.duty,l.id",
                (p["group_id"], source["ref"], action.get("event_ref"), action["activity"]),
            ) if isinstance(action.get("event_ref"), str) else []
            if not gone:
                raise CalendarError("not_found_or_forbidden")
            skips = [
                {
                    "rule_id": r["rule_id"],
                    "duty": r["duty"],
                    "guardian_id": r["guardian_id"],
                    "reason": "projection_pending"
                    if r["state"] == "pending"
                    else "outcome_unknown"
                    if r["state"] != "active"
                    else {"projection_edited": "projection_edited", "unknown": "already_projected"}.get(
                        self._drift(p, r), "use_unproject"
                    ),
                }
                for r in gone
            ]
            labels = {
                r["activity"]
                for r in self.store.query(
                    "SELECT activity FROM guardian_rules WHERE group_id=%s AND source_calendar_ref=%s AND enabled",
                    (p["group_id"], source["ref"]),
                )
            }
            return source, scope, [], skips, labels
        if occ["details"].get("all_day"):
            raise CalendarError("source_all_day")
        # Projecting a drive would chain links; any link naming this event as its target refuses it.
        if self.store.one(
            "SELECT 1 FROM projection_links WHERE target_calendar_ref=%s AND target_id=%s",
            (source["ref"], occ["target"]),
        ):
            raise CalendarError("projection_source_is_projection")
        start = instant(occ["start_at"])
        end = start if occ["details"].get("instant") else instant(occ["end_at"])
        steps, skips, labels = self._drives(key, p, source, action["activity"], occ["ref"], start, end, occ["details"])
        return source, scope, steps, skips, labels

    def _drives(self, key, p, source, activity, event_ref, start, end, details, source_step=None):
        """The linked creates for one lesson; a lesson created in the same plan has no event_ref yet and names
        its create step instead, whose verified receipt supplies the event_ref."""
        rules = self.store.query(
            "SELECT r.*,m.enabled AS guardian_enabled,cn.provider AS target_provider,"
            "(SELECT l.state FROM projection_links l WHERE l.source_event_ref=%s AND l.rule_id=r.id "
            "AND l.state IN ('pending','active','repairing')) AS claim "
            "FROM guardian_rules r JOIN bindings b ON b.group_id=r.group_id AND b.person_id=r.dependent_id "
            "AND b.calendar_ref=r.source_calendar_ref "
            "JOIN memberships m ON m.group_id=r.group_id AND m.person_id=r.guardian_id "
            "JOIN calendars c ON c.ref=r.target_calendar_ref JOIN connections cn ON cn.id=c.connection_id "
            "WHERE r.group_id=%s AND r.source_calendar_ref=%s ORDER BY r.dependent_id,r.duty,r.id",
            (event_ref, p["group_id"], source["ref"]),
        )
        steps, skips = [], []
        for r in rules:
            if r["activity"] != activity:
                continue
            # A live link is planned again only to fix drift: a moved lesson or a drive gone (ANYM-436).
            link = r["claim"] == "active" and self.store.one(
                self.LINKS + "l.rule_id=%s AND l.source_event_ref=%s AND l.state='active'",
                (p["group_id"], r["id"], event_ref),
            )
            drift = self._drift(p, link) if link else None
            kind = "update" if drift == "source_moved" else "create"
            # A live link's drive stays in the calendar it was placed in, even after the rule names another.
            tcal = link["target_calendar_ref"] if link else r["target_calendar_ref"]
            tprovider = (
                self.store.one("SELECT provider FROM connections cn JOIN calendars c ON c.connection_id=cn.id "
                               "WHERE c.ref=%s", (tcal,)) or {}
            ).get("provider") if link else r["target_provider"]
            reason = (
                "disabled"
                if not r["enabled"]
                else "guardian_disabled"
                if not r["guardian_enabled"]
                else "projection_pending"
                if r["claim"] == "pending"
                else "outcome_unknown"
                if r["claim"] == "repairing"
                else DRIFT_SKIP.get(drift or "", "already_projected")
                if r["claim"] and drift not in ("source_moved", "projection_missing")
                # _source already proved source details through event:read or agenda:read. A live link is fixed in
                # its own calendar, so the rule's current target is not consulted for it.
                else (None if link else {"source_not_visible": None}.get(
                    ready := self.rule_view(p, r)["not_ready_reason"], ready))
                or (
                    None
                    if self._can(p, tcal, "plan:create") and self._can(p, tcal, "event:" + kind)
                    else "target_not_writable"
                )
                or (None if tprovider == "google" else "provider_not_supported")
            )
            if reason:
                skips.append({"rule_id": r["id"], "duty": r["duty"], "guardian_id": r["guardian_id"], "reason": reason})
                continue
            _, target = self.authorize(key, tcal, "event:" + kind)
            lead = timedelta(minutes=r["lead_minutes"])
            # Both drives end at the lesson boundary they serve.
            a, b = (start - lead, start) if r["duty"] == "dropoff" else (end - lead, end)
            event = {"title": r["title"], "start": a.isoformat(), "end": b.isoformat(), "timezone": target["timezone"]}
            if r["copy_location"] and details.get("location"):
                event["location"] = details["location"]
            if kind == "update":
                # Reproject: the drive moves to the lesson's new span, conditional on the link ETag.
                drive = self.store.one(
                    "SELECT start_at,end_at,details FROM occurrences WHERE calendar_ref=%s AND resource_ref=%s",
                    (target["ref"], link["target_resource_ref"]),
                )
                del event["title"]
                step = {
                    "action": "update",
                    "calendar_ref": target["ref"],
                    "provider": target["provider"],
                    "event": event,
                    "span": {"start": a.isoformat(), "end": b.isoformat()},
                    "resource_ref": link["target_resource_ref"],
                    "target_id": link["target_id"],
                    "expected_etag": link["etag"],
                    "reproject": True,
                    "link": {
                        "rule_id": r["id"],
                        "rule_version": r["version"],
                        "duty": r["duty"],
                        "guardian_id": link["guardian_id"],
                        "source_event_ref": event_ref,
                    },
                    "source": {"start": start.isoformat(), "end": end.isoformat(), "title": details.get("title")},
                    "before": {
                        "start": drive["start_at"].isoformat(),
                        "end": drive["end_at"].isoformat(),
                        **{k: v for k, v in drive["details"].items() if k == "location"},
                    },
                }
                steps.append((step, target))
                continue
            step = {
                "action": "create",
                "calendar_ref": target["ref"],
                "provider": target["provider"],
                "event": event,
                "span": {"start": a.isoformat(), "end": b.isoformat()},
                "resource_ref": ref("res"),
                "target_id": uuid_token(),
                "link": {
                    "rule_id": r["id"],
                    "rule_version": r["version"],
                    "duty": r["duty"],
                    "guardian_id": r["guardian_id"],
                    "source_event_ref": event_ref,
                    **({} if source_step is None else {"source_step": source_step}),
                },
                "source": {"start": start.isoformat(), "end": end.isoformat(), "title": details.get("title")},
                # Worker-only: the opaque link ID is the one marker the provider event carries.
                "projection": {"link_id": ref("link"), "source_calendar_ref": source["ref"], "visibility": r["visibility"]},
            }
            if link:
                # Re-create a drive gone outside DeedKit, with a new client ID on the same link row; the worker
                # takes the link over from its old target at admission, or fails projection_pending.
                step["link"]["guardian_id"] = link["guardian_id"]
                step["reproject"] = True
                step["projection"].update(
                    link_id=link["id"],
                    repair_of={"target_id": link["target_id"], "resource_ref": link["target_resource_ref"]},
                )
            steps.append((step, target))
        return steps, skips, {r["activity"] for r in rules if r["enabled"]}

    def _unproject(self, key, p, action, skips):
        """One delete per active link of a lesson occurrence or of one of p's operations; a link whose
        outcome is unknown is skipped. Source authority rests on the link's stored source calendar."""
        if self.store.guardian_readiness():
            raise CalendarError("capability_unverified")
        if not self.rule_reader(p):
            raise CalendarError("not_found_or_forbidden")
        if "operation_id" in action:
            if set(action) != {"action", "operation_id"}:
                raise CalendarError("invalid_action")
            if not self.store.one(
                "SELECT 1 FROM operations WHERE id=%s AND principal_id=%s", (action["operation_id"], p["id"])
            ):
                raise CalendarError("not_found_or_forbidden")
            rows = self.store.query(
                self.LINKS + "l.operation_id=%s ORDER BY l.ordinal,l.id", (p["group_id"], action["operation_id"])
            )
        else:
            if set(action) != {"action", "calendar_ref", "event_ref"} or not isinstance(action["event_ref"], str):
                raise CalendarError("invalid_action")
            # Checked before the lookup, so a calendar the principal cannot read reveals no link.
            self._source(key, action["calendar_ref"])
            rows = self.store.query(
                self.LINKS + "l.source_calendar_ref=%s AND l.source_event_ref=%s ORDER BY l.duty,l.id",
                (p["group_id"], action["calendar_ref"], action["event_ref"]),
            )
        sources = {ref: self._source(key, ref) for ref in {r["source_calendar_ref"] for r in rows}}
        result = []
        for r in rows:
            skip = {"rule_id": r["rule_id"], "duty": r["duty"], "guardian_id": r["guardian_id"]}
            if r["state"] != "active":
                # A pending or repairing link may name a write whose outcome is unknown: reconcile first.
                skips.append(dict(skip, reason="outcome_unknown"))
                continue
            if not (self._can(p, r["target_calendar_ref"], "plan:create") and self._can(
                p, r["target_calendar_ref"], "event:delete"
            )):
                skips.append(dict(skip, reason="target_not_writable"))
                continue
            _, target = self.authorize(key, r["target_calendar_ref"], "event:delete")
            start, end = r["source_start"], r["source_end"]
            # An instant lesson is stored with the smallest span the CHECK allows; it ends at its start.
            end = start if end - start <= timedelta(microseconds=1) else end
            step = {
                "action": "delete",
                "calendar_ref": target["ref"],
                "provider": target["provider"],
                "resource_ref": r["target_resource_ref"],
                "target_id": r["target_id"],
                "expected_etag": r["etag"],
                "link": {
                    "rule_id": r["rule_id"],
                    "rule_version": r["rule_version"],
                    "duty": r["duty"],
                    "guardian_id": r["guardian_id"],
                    "source_event_ref": r["source_event_ref"],
                },
                # The stored span: the lesson may since be moved, cancelled or gone.
                "source": {"start": start.isoformat(), "end": end.isoformat(), "title": r["title"]},
            }
            result.append((step, target, *sources[r["source_calendar_ref"]]))
        return result

    def _carry(self, key, p, step, event_ref, source, skips, selected):
        """The linked change of each active drive of a lesson that step moves or cancels: an update to the new
        span by the link's rule, or a delete; both conditional on the link ETag. None when the lesson has no
        link the principal may see."""
        if self.store.guardian_readiness() or not self.rule_reader(p):
            return None
        try:
            source, scope = self._source(key, source["ref"])
        except CalendarError:
            return None
        rows = self.store.query(
            self.LINKS + "l.source_calendar_ref=%s AND l.source_event_ref=%s ORDER BY l.duty,l.id",
            (p["group_id"], source["ref"], event_ref),
        )
        if not rows:
            return None
        kind, result = step["action"], []
        for r in rows:
            skip = {"rule_id": r["rule_id"], "duty": r["duty"], "guardian_id": r["guardian_id"]}
            reason = (
                "projection_pending"
                if r["state"] == "pending"
                # A repairing link names a write whose outcome is unknown: reconcile first.
                else "outcome_unknown"
                if r["state"] != "active"
                else None
                if self._can(p, r["target_calendar_ref"], "plan:create")
                and self._can(p, r["target_calendar_ref"], "event:" + kind)
                else "target_not_writable"
            )
            if not reason and kind == "update" and step["event"].get("all_day"):
                reason = "source_all_day"
            if reason:
                skips.append(dict(skip, reason=reason))
                continue
            _, target = self.authorize(key, r["target_calendar_ref"], "event:" + kind)
            rule = self.store.one(
                "SELECT version,lead_minutes,copy_location FROM guardian_rules WHERE id=%s", (r["rule_id"],)
            )
            drive = self.store.one(
                "SELECT ref,start_at,end_at,details FROM occurrences WHERE calendar_ref=%s AND resource_ref=%s",
                (target["ref"], r["target_resource_ref"]),
            )
            if drive:
                # The drive's cached occurrence moves with the plan: it is no overlap of its own new span.
                selected.add(drive["ref"])
            if kind == "update":
                start, end = (instant(step["span"][k]) for k in ("start", "end"))
                lead = timedelta(minutes=rule["lead_minutes"])
            else:
                start, end = r["source_start"], r["source_end"]
                end = start if end - start <= timedelta(microseconds=1) else end
            linked = {
                "action": kind,
                "calendar_ref": target["ref"],
                "provider": target["provider"],
                "resource_ref": r["target_resource_ref"],
                "target_id": r["target_id"],
                "expected_etag": r["etag"],
                "link": {
                    "rule_id": r["rule_id"],
                    "rule_version": rule["version"] if kind == "update" else r["rule_version"],
                    "duty": r["duty"],
                    "guardian_id": r["guardian_id"],
                    "source_event_ref": r["source_event_ref"],
                    "source_step": step["ordinal"],
                },
                "source": {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "title": step.get("event", {}).get("title") or r["title"],
                },
            }
            if kind == "update":
                a, b = (start - lead, start) if r["duty"] == "dropoff" else (end - lead, end)
                event = {"start": a.isoformat(), "end": b.isoformat(), "timezone": target["timezone"]}
                if rule["copy_location"] and "location" in step["event"]:
                    event["location"] = step["event"]["location"]
                linked.update(event=event, span={"start": a.isoformat(), "end": b.isoformat()})
                if drive:
                    linked["before"] = {
                        "start": drive["start_at"].isoformat(),
                        "end": drive["end_at"].isoformat(),
                        **{k: v for k, v in drive["details"].items() if k == "location"},
                    }
            result.append((linked, target))
        return source, scope, result

    LINKS = (
        # The guardian the drive was made for, frozen in the projecting plan; the rule may since name another.
        "SELECT l.*,coalesce((SELECT p.payload->'steps'->l.ordinal->'link'->>'guardian_id' FROM operations op "
        "JOIN plans p ON p.id=op.plan_id WHERE op.id=l.operation_id),r.guardian_id) AS guardian_id,"
        "o.details->>'title' AS title FROM projection_links l "
        "JOIN guardian_rules r ON r.id=l.rule_id "
        "LEFT JOIN occurrences o ON o.ref=l.source_event_ref AND o.calendar_ref=l.source_calendar_ref "
        "WHERE r.group_id=%s AND l.state<>'removed' AND "
    )

    def replacement_of(self, p, handle):
        """The verified replacement series one of p's own following moves created, by its series_ref: the plan step
        that created it and the create's receipt. Durable, so an undo never waits for a read to publish it."""
        from .providers import series_ref

        rows = self.store.query(
            "SELECT p.payload,s.receipt FROM operations o JOIN plans p ON p.id=o.plan_id "
            "JOIN operation_steps s ON s.operation_id=o.id AND s.ordinal=0 "
            "WHERE o.principal_id=%s AND s.state='verified' "
            "AND p.payload->'steps'->0->'series'->>'role'='replacement' ORDER BY o.created_at",
            (p["id"],),
        )
        for row in rows:
            made = row["payload"]["steps"][0]
            if series_ref(made["calendar_ref"], made["target_id"]) == handle and (row["receipt"] or {}).get("etag"):
                return dict(made, receipt=row["receipt"])
        raise CalendarError("not_found_or_forbidden")

    def _undo_step(self, p, cal, handle, step):
        """Delete the replacement series of a partial following move, only as its create left it (its etag)."""
        made = self.replacement_of(p, handle)
        if made["calendar_ref"] != cal["ref"] or cal["provider"] != "google":
            raise CalendarError("not_found_or_forbidden")
        return dict(step, target_id=made["target_id"], resource_ref=made["resource_ref"],
                    expected_etag=made["receipt"]["etag"], before=dict(made["span"]),
                    series={"scope": "all", "role": "undo", "series_ref": handle,
                            "planned_at": datetime.now(timezone.utc).isoformat()})

    def _series_steps(self, key, cal, step, scope, ordinal):
        """The steps of a series edit (ANYM-556): `all` is one write to the master; `following` ends the master just
        before the occurrence, and a following move first creates the replacement series under a stable,
        client-generated ID (step 0), then truncates (step 1), so a failed truncation never loses an occurrence."""
        if cal["provider"] != "google":
            raise CalendarError("capability_unsupported")
        planned = datetime.now(timezone.utc).isoformat()
        if scope == "all" or step["action"] == "delete":
            return [dict(step, ordinal=ordinal, series={"scope": scope, "planned_at": planned})]
        self.authorize(key, cal["ref"], "event:create")
        replacement = {
            "action": "create", "calendar_ref": step["calendar_ref"], "provider": step["provider"],
            "ordinal": ordinal, "event": step["event"], "span": step["span"], "resource_ref": ref("res"),
            "target_id": uuid_token(), "occurrence": step["occurrence"],
            "series": {"scope": "following", "role": "replacement", "instance_id": step["target_id"],
                       "instance_etag": step["expected_etag"], "planned_at": planned},
        }
        truncation = {k: v for k, v in step.items() if k not in ("event", "span")}
        truncation.update(ordinal=ordinal + 1, series={"scope": "following", "role": "truncation",
                                                       "replacement_step": ordinal, "planned_at": planned})
        return [replacement, truncation]

    def _overlap_check(self, key, principal, prepared, selected):
        """Advisory cached evidence within current read grants, never permission
        to read a hidden calendar or a guarantee against a concurrent edit."""
        # The truncation of a following move writes no time of its own: its replacement carries the span.
        proposed = [s for s in prepared if s["action"] != "delete" and "span" in s]
        result = {
            "status": "incomplete",
            "scope": "visible_calendars",
            "coverage": [],
            "overlaps": [],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "execution_recheck": False,
        }
        if not proposed:
            result["status"] = "not_applicable"
            return result
        a = min(instant(s["span"]["start"]) for s in proposed)
        b = max(instant(s["span"]["end"]) for s in proposed)
        # Independent valid actions may span more than the maximum read window.
        # Such a plan stays valid but its overlap check is explicitly incomplete.
        if b - a > timedelta(days=31):
            result["reason"] = "combined_window_exceeds_limit"
            return result
        rows = self.store.query(
            "SELECT c.ref,g.scopes FROM calendars c JOIN connections cn ON cn.id=c.connection_id "
            "JOIN calendar_grants g ON g.calendar_ref=c.ref JOIN agent_calendars ac ON ac.calendar_ref=c.ref "
            "WHERE g.group_id=%s AND g.person_id=%s AND ac.principal_id=%s AND cn.enabled ORDER BY c.ref",
            (principal["group_id"], principal["person_id"], principal["id"]),
        )
        readable = []
        for row in rows:
            scopes = set(row["scopes"]) & set(principal["scopes"])
            scope = next((s for s in ("availability:read", "agenda:read", "event:read") if s in scopes), None)
            if scope:
                readable.append((row["ref"], scope))
        if len(readable) > 32:
            result["reason"] = "calendar_limit"
            return result
        event_count = 0
        for calendar_ref, scope in readable:
            _, cal = self.authorize(key, calendar_ref, scope)
            try:
                agenda = self.agenda(key, [calendar_ref], a, b, scope)
            except CalendarError as error:
                if str(error) != "coverage_incomplete":
                    raise
                result["reason"] = "event_limit"
                result["coverage"].append(
                    {"calendar_ref": calendar_ref, "status": "unknown", "error": "event_limit"}
                )
                break
            result["coverage"].extend(agenda["coverage"])
            event_count += len(agenda["events"])
            if event_count > 500:
                result["reason"] = "event_limit"
                break
            for event in agenda["events"]:
                if not event["busy"]:
                    continue
                # Exclude only the occurrences this plan moves or deletes: the other occurrences of a
                # series stay where they are, even though they share its resource.
                if cal["visibility"] == "details" and event.get("event_ref") in selected:
                    continue
                for step in proposed:
                    # A drive never overlaps the lesson it serves.
                    if event.get("event_ref") and event["event_ref"] == step.get("link", {}).get("source_event_ref"):
                        continue
                    if instant(event["start"]) < instant(step["span"]["end"]) and instant(
                        event["end"]
                    ) > instant(step["span"]["start"]):
                        overlap = {
                            "step": step["ordinal"],
                            "calendar_ref": calendar_ref,
                            "start": event["start"],
                            "end": event["end"],
                            "visibility": cal["visibility"],
                        }
                        if cal["visibility"] == "details":
                            overlap["event_ref"] = event["event_ref"]
                        result["overlaps"].append(overlap)
                        if len(result["overlaps"]) >= 500:
                            result["reason"] = "overlap_limit"
                            return result
        for index, step in enumerate(proposed):
            for other in proposed[index + 1 :]:
                # A drive never overlaps the lesson it serves, in the plan as in the calendar.
                if step["ordinal"] == other.get("link", {}).get("source_step"):
                    continue
                if instant(step["span"]["start"]) < instant(other["span"]["end"]) and instant(
                    step["span"]["end"]
                ) > instant(other["span"]["start"]):
                    result["overlaps"].append(
                        {"step": step["ordinal"], "other_step": other["ordinal"], "kind": "proposed_overlap"}
                    )
        if (
            readable
            and not result.get("reason")
            and all(c["status"] == "complete" for c in result["coverage"])
        ):
            result["status"] = "complete_within_visible_calendars"
        return result

    @staticmethod
    def _preview(step):
        before = step.get("before", {})
        after = step.get("event", {})
        fields = ("title", "start", "end", "timezone", "location", "description")
        diff = {}
        update = step.get("action") == "update"
        was, will = bool(before.get("all_day")), bool(after.get("all_day"))
        for field in fields:
            # Providers keep fields an update omits, and stored details carry no original
            # timezone; neither may be shown as a change the apply would not make.
            if update and (field not in after or field not in before):
                continue
            old, new = before.get(field), after.get(field)
            equal = old == new
            # Dates compare as written; timed values by instant, whatever offset spells them.
            if field in ("start", "end") and old and new and not was and not will:
                equal = instant(old) == instant(new)
            if not equal:
                diff[field] = {"before": old, "after": new}
        if update and "event" in step and was != will:
            diff["all_day"] = {"before": was, "after": will}
        shown = {k: v for k, v in step.items()
                 if k not in ("target_id", "resource_ref", "expected_etag", "span", "projection", "series")}
        if "series" in step:
            # The scope and role only: the instance's provider ID and etag stay internal.
            shown["series"] = {k: step["series"][k] for k in ("scope", "role") if k in step["series"]}
        return {**shown, "diff": diff}

    def validate_plan(self, plan, principal_id):
        p = self.principal(principal_id=principal_id)
        calendars = []
        for step in plan["payload"]["steps"]:
            self.authorize(None, step["calendar_ref"], "plan:execute", principal_id=principal_id)
            _, cal = self.authorize(
                None, step["calendar_ref"], "event:" + step["action"], principal_id=principal_id
            )
            calendars.append(cal)
        sources = plan["payload"].get("sources", [])
        if sources and not self.rule_reader(p):
            raise CalendarError("scope_changed")
        for source in sources:
            # The lesson must stay readable with details, by the scope the preview used.
            try:
                _, cal = self.authorize(None, source["calendar_ref"], source["scope"], principal_id=principal_id)
            except CalendarError:
                raise CalendarError("scope_changed") from None
            if cal["visibility"] != "details":
                raise CalendarError("scope_changed")
            calendars.append(cal)
        invites = [s for s in plan["payload"]["steps"] if "invite" in s]
        if self._scope_hash(p, calendars, bool(invites),
                            any("series" in s for s in plan["payload"]["steps"])) != plan["scope_hash"]:
            raise CalendarError("scope_changed")
        if any(not self.invitations_enabled(s["calendar_ref"]) for s in invites):
            raise CalendarError("invitations_not_enabled")

    def apply(self, key, plan_id, request_id):
        p = self.principal(key)
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 128:
            raise CalendarError("invalid_request_id")
        plan = self.store.one("SELECT * FROM plans WHERE id=%s AND principal_id=%s", (plan_id, p["id"]))
        if not plan:
            raise CalendarError("not_found_or_forbidden")
        with self.store.transaction() as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (p["id"] + request_id,))
            old = c.execute(
                "SELECT * FROM operations WHERE principal_id=%s AND request_id=%s", (p["id"], request_id)
            ).fetchone()
            if old:
                if old["plan_id"] != plan_id:
                    raise CalendarError("idempotency_conflict")
                return {"operation_id": old["id"], "state": old["state"]}
            if plan["expires_at"] < datetime.now(timezone.utc):
                raise CalendarError("plan_expired")
            self.validate_plan(plan, p["id"])
            operation_id = ref("op")
            c.execute(
                "INSERT INTO operations(id,principal_id,plan_id,request_id) VALUES (%s,%s,%s,%s)",
                (operation_id, p["id"], plan_id, request_id),
            )
            for step in plan["payload"]["steps"]:
                c.execute(
                    "INSERT INTO operation_steps(operation_id,ordinal) VALUES (%s,%s)",
                    (operation_id, step["ordinal"]),
                )
        return {"operation_id": operation_id, "state": "queued"}

    def operation(self, key, operation_id):
        p = self.principal(key)
        op = self.store.one(
            "SELECT * FROM operations WHERE id=%s AND principal_id=%s", (operation_id, p["id"])
        )
        if not op:
            raise CalendarError("not_found_or_forbidden")
        plan = self.store.one("SELECT * FROM plans WHERE id=%s", (op["plan_id"],))
        # Historical read access does not require write opt-in to remain enabled.
        scope = "event:read" if "event:read" in p["scopes"] else "agenda:read"
        for step in plan["payload"]["steps"]:
            _, cal = self.authorize(key, step["calendar_ref"], scope)
            if cal["visibility"] != "details":
                raise CalendarError("not_found_or_forbidden")
        steps = self.store.query(
            "SELECT ordinal,state,receipt,error FROM operation_steps WHERE operation_id=%s ORDER BY ordinal",
            (operation_id,),
        )
        result = {
            "operation_id": operation_id,
            "state": op["state"],
            "steps": steps,
            "error": op["error"],
            "next_action": "reconcile; do not create again"
            if op["state"] in ("unknown", "needs_attention", "partial")
            else None,
        }
        first = plan["payload"]["steps"][0]
        if op["state"] == "partial" and (first.get("series") or {}).get("role") == "replacement":
            # A following move whose truncation was definitely refused (ANYM-556): the same definite failure, both
            # series and the undo preview calendar_schedule gives, never a reconcile.
            from .schedule import Scheduler

            scheduler = Scheduler(self)
            steps_ = plan["payload"]["steps"]
            result.update(scheduler.partial(scheduler.primary(steps_), steps_, scheduler.family(key, p)),
                          state="failed", detail_state="partial")
        return result

    def assignments(self, key, start, end):
        p = self.principal(key)
        a, b = window(start, end)
        if "assignment:read" not in p["scopes"]:
            raise CalendarError("not_found_or_forbidden")
        rows = self.store.query(
            "SELECT id AS assignment_id,person_id AS member_id,duty,start_at,end_at,status,version "
            "FROM assignments WHERE group_id=%s AND start_at<%s AND end_at>%s ORDER BY start_at LIMIT 501",
            (p["group_id"], b, a),
        )
        if len(rows) > 500:
            raise CalendarError("coverage_incomplete")
        return {
            "assignments": [
                dict((k, v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items())
                for row in rows
            ]
        }

    def assignment(
        self, key, member_id, duty, start, end, status="proposed", assignment_id=None, expected_version=None
    ):
        p = self.principal(key)
        a, b = window(start, end)
        if status == "accepted":
            raise CalendarError("confirmation_actor_required")
        if status not in ("proposed", "assigned", "declined", "cancelled") or duty not in (
            "pickup",
            "dropoff",
            "stay_with_child",
        ):
            raise CalendarError("invalid_assignment")
        if "assignment:write" not in p["scopes"] or p["role"] not in ("group_admin", "coordinator", "adult"):
            raise CalendarError("not_found_or_forbidden")
        if p["role"] == "adult" and member_id != p["person_id"]:
            raise CalendarError("not_found_or_forbidden")
        if not self.store.one(
            "SELECT 1 FROM memberships WHERE group_id=%s AND person_id=%s AND enabled",
            (p["group_id"], member_id),
        ):
            raise CalendarError("not_found_or_forbidden")
        if assignment_id:
            with self.store.transaction() as c:
                old = c.execute(
                    "SELECT * FROM assignments WHERE id=%s AND group_id=%s FOR UPDATE",
                    (assignment_id, p["group_id"]),
                ).fetchone()
                if not old or (p["role"] == "adult" and old["person_id"] != p["person_id"]):
                    raise CalendarError("not_found_or_forbidden")
                if expected_version != old["version"]:
                    raise CalendarError("version_conflict")
                c.execute(
                    "UPDATE assignments SET person_id=%s,duty=%s,start_at=%s,end_at=%s,status=%s,version=version+1 WHERE id=%s",
                    (member_id, duty, a, b, status, assignment_id),
                )
                version = old["version"] + 1
        else:
            if status in ("cancelled", "declined"):
                raise CalendarError("assignment_id_required")
            assignment_id = ref("assignment")
            self.store.query(
                "INSERT INTO assignments(id,group_id,person_id,principal_id,duty,start_at,end_at,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (assignment_id, p["group_id"], member_id, p["id"], duty, a, b, status),
            )
            version = 1
        return {
            "assignment_id": assignment_id,
            "version": version,
            "status": status,
            "member_id": member_id,
            "duty": duty,
            "start": a.isoformat(),
            "end": b.isoformat(),
        }


def uuid_token():
    # Google's client-generated IDs accept lowercase base32hex; hex is a valid subset.
    from uuid import uuid4

    return "fcs" + uuid4().hex
