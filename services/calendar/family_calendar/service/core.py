from datetime import date, datetime, time, timedelta, timezone  # noqa: F401  (date, time: kept importable)
import hashlib
import re  # noqa: F401
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg.types.json import Jsonb
from family_calendar.errors import CalendarError
from . import journal
from .build import build
from .store import digest, instant, person_uuid, ref
from .projection import (  # noqa: F401  (the moved names stay importable from here)
    DRIFT_SKIP, ProjectionMixin, carried_step, carries, shown_time, uuid_token)
from .validation import (  # noqa: F401  (the moved names stay importable from here)
    DATE_VALUE, LIMITS, day, event_span, invalid, validate_all_day, validate_event, validate_timed)
from .windows import (  # noqa: F401
    LOCAL_TIME, NAMED_WINDOWS, QUARTER, find_window, wall_time, widened, window)

READ = ["agenda:read", "availability:read", "event:read", "assignment:read"]
WRITE = ["plan:create", "plan:execute", "event:create", "event:update", "event:delete", "assignment:write"]
# The event router's read of the change journal (ANYM-565): a key with this scope alone, nothing else.
EVENTS = "calendar:events"


def clock():
    return datetime.now(timezone.utc)


TIME_FORMAT = (
    "ISO 8601 date-time with offset or Z, e.g. 2026-10-20T10:00:00+08:00; plan event "
    "start/end may omit the offset (YYYY-MM-DDTHH:MM[:SS]) and are then wall time in "
    "the event's IANA timezone"
)


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
# A series time move that carries its exceptions (ANYM-614): a worker that predates the per-exception steps refuses
# the plan (scope_changed) instead of moving the series without them.
CARRIED_SCOPE = "series-exceptions-v1"


class PlanDraft:
    """What one `plan` call collects while its phases run: _plan_unproject, _plan_project, _plan_write and
    _plan_linked add to it, _plan_finish stores it."""

    def __init__(self):
        self.calendars = []
        self.prepared = []
        self.touched = set()
        self.selected = set()
        self.sources, self.source_calendars, self.skipped, self.activities, self.projected = [], [], [], set(), set()
        # Remote events this plan changes: one action per drive, whether by unproject or an ordinary write.
        self.targets, self.unprojected, self.carried = set(), False, False


class FamilyService(ProjectionMixin):
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

    def _scope_hash(self, p, calendars, invitations=False, series=False, carried=False):
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
            + ([CARRIED_SCOPE] if carried else [])
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
            build=build(),
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

    def agenda(self, key, calendar_refs, start, end, scope="agenda:read", projection_status=False, event_refs=None):
        a, b = window(start, end)
        if not calendar_refs or len(calendar_refs) > 32 or len(set(calendar_refs)) != len(calendar_refs):
            raise CalendarError("invalid_calendar_refs")
        coverage = []
        events = []
        cancelled = []
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
            if cal["visibility"] == "details":
                cancelled.extend(self._cancelled(calendar_ref, a, b, event_refs or None))
        if len(events) > 500:
            raise CalendarError("coverage_incomplete")
        result = {
            "events": sorted(events, key=lambda x: x["start"]),
            "coverage": coverage,
            "required_sources_complete": all(c["status"] == "complete" for c in coverage),
        }
        if cancelled:
            result["cancelled"] = sorted(cancelled, key=lambda x: (x["start"], x["event_ref"]))[:500]
            if len(cancelled) > 500:
                # More than the response holds: a narrower window lists the rest.
                result["cancelled_incomplete"] = True
        return result

    def _cancelled(self, calendar_ref, a, b, event_refs=None):
        """Events of the window cancelled in the last 30 days, from their last stored state (ANYM-565 criterion 5):
        the event_ref a calendar.event.cancelled names stays readable, by its window or by event_refs. Nothing before
        migration 8 and its grants."""
        with self.store.transaction() as c:
            if not c.execute(
                "SELECT coalesce(has_table_privilege(to_regclass('event_states'),'SELECT'),false) AS ok"
            ).fetchone()["ok"]:
                return []
            rows = journal.cancelled(c, calendar_ref, a, b, event_refs)
        return [
            {"calendar_ref": calendar_ref, "event_ref": r["event_ref"], "status": "cancelled",
             "start": r["start_at"].isoformat(),
             "end": (r["start_at"] if r["details"].get("instant") else r["end_at"]).isoformat(),
             "busy": r["busy"], "cancelled_at": r["cancelled_at"].isoformat(), **r["details"]}
            for r in rows
        ]

    def events_page(self, key, after, limit):
        """The change journal for the event router (GET /v1/events, ANYM-562 cursor contract): a key whose only scope
        is calendar:events reads the events of the calendars it is bound to."""
        p = self.principal(key)
        if p["scopes"] != [EVENTS]:
            raise CalendarError("not_found_or_forbidden")
        with self.store.transaction() as c:
            calendars = {r["calendar_ref"] for r in c.execute(
                "SELECT calendar_ref FROM agent_calendars WHERE principal_id=%s", (p["id"],))}
            return journal.page(c, after, limit, calendars)

    def events_head(self, key):
        p = self.principal(key)
        if p["scopes"] != [EVENTS]:
            raise CalendarError("not_found_or_forbidden")
        with self.store.transaction() as c:
            row = journal.head(c)
        return {"journal_id": row["journal_id"], "head": row["head"]}

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

    def linked_member(self, p, person_id):
        """The member the owner linked to person_id in principal p's family (ANYM-554), or None. A member with a
        bound calendar p cannot read for availability is hidden: None, the same answer as an unlinked person, so
        a UUID never tells which member it names. Before migration 6 and its grant no person is linked, so names
        and member IDs keep working."""
        try:
            row = self.store.one("SELECT member_id FROM member_people WHERE group_id=%s AND person_uuid=%s",
                                 (p["group_id"], person_id))
        except (psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege):
            return None
        if not row:
            return None
        for b in self.store.query("SELECT calendar_ref FROM bindings WHERE group_id=%s AND person_id=%s",
                                  (p["group_id"], row["member_id"])):
            try:
                self.authorize(None, b["calendar_ref"], "availability:read", principal_id=p["id"])
            except CalendarError:
                return None
        return row["member_id"]

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
                # name match and no messaging read, so an unlinked or hidden person is not_found, before any
                # selection can name a member.
                linked = self.linked_member(p, person_uuid(name))
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

    def plan(self, key, actions, carry_projections=False, invitations=False, series=False, read=None):
        p = self.principal(key)
        if not isinstance(actions, list) or not 1 <= len(actions) <= 8:
            raise CalendarError("invalid_actions")
        draft = PlanDraft()
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
                self._plan_unproject(key, p, action, draft)
                continue
            if "operation_id" in action or not isinstance(action.get("calendar_ref"), str):
                raise CalendarError("invalid_action")
            if kind == "project":
                self._plan_project(key, p, action, draft)
                continue
            written = self._plan_write(key, p, action, kind, draft, carry_projections, read)
            if written is None:
                continue
            step, cal, label = written
            draft.prepared.append(step)
            self._plan_linked(key, p, action, kind, step, cal, label, carry_projections, draft)
        return self._plan_finish(key, p, draft, read)

    def _plan_unproject(self, key, p, action, draft):
        """The `unproject` action of a plan."""
        draft.unprojected = True
        for step, cal, source, scope in self._unproject(key, p, action, draft.skipped):
            if (cal["ref"], step["target_id"]) in draft.targets:
                raise CalendarError("duplicate_resource_action")
            draft.targets.add((cal["ref"], step["target_id"]))
            if source["ref"] not in [s["calendar_ref"] for s in draft.sources]:
                draft.sources.append({"calendar_ref": source["ref"], "scope": scope})
                draft.source_calendars.append(source)
            draft.prepared.append(dict(step, ordinal=len(draft.prepared)))
            draft.calendars.append(cal)

    def _plan_project(self, key, p, action, draft):
        """The `project` action of a plan."""
        label = action.get("activity")
        if not isinstance(label, str) or not 1 <= len(label) <= 64 or "event" in action:
            raise CalendarError("invalid_action")
        if action.get("event_ref") in draft.projected:
            raise CalendarError("duplicate_resource_action")
        draft.projected.add(action.get("event_ref"))
        source, scope, steps, skips, labels = self._project(key, p, action)
        if source["ref"] not in [s["calendar_ref"] for s in draft.sources]:
            draft.sources.append({"calendar_ref": source["ref"], "scope": scope})
            draft.source_calendars.append(source)
        for step, cal in steps:
            if step["action"] == "update":
                # A reprojected drive moves: its cached occurrence is no overlap of its new span.
                if (cal["ref"], step["target_id"]) in draft.targets:
                    raise CalendarError("duplicate_resource_action")
                draft.targets.add((cal["ref"], step["target_id"]))
                draft.selected.update(
                    o["ref"]
                    for o in self.store.query(
                        "SELECT ref FROM occurrences WHERE calendar_ref=%s AND resource_ref=%s",
                        (cal["ref"], step["resource_ref"]),
                    )
                )
            draft.prepared.append(dict(step, ordinal=len(draft.prepared)))
            draft.calendars.append(cal)
        draft.skipped += skips
        draft.activities |= labels

    def _plan_write(self, key, p, action, kind, draft, carry_projections, read):
        """A create, update or delete action: the checked step and its calendar, or None when it already added its own steps (a series scope)."""
        label = action.get("activity")
        if "activity" in action and (
            kind != "create" or not carry_projections or not isinstance(label, str) or not 1 <= len(label) <= 64
        ):
            raise CalendarError("invalid_action")
        _, cal = self.authorize(key, action.get("calendar_ref"), "plan:create")
        self.authorize(key, cal["ref"], "event:" + kind)
        draft.calendars.append(cal)
        step = {
            "action": kind,
            "calendar_ref": cal["ref"],
            "provider": cal["provider"],
            "ordinal": len(draft.prepared),
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
            step["event"] = validate_event(action.get("event"), f"actions/{len(draft.prepared)}/event")
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
            if (cal["ref"], step["target_id"]) in draft.targets:
                raise CalendarError("duplicate_resource_action")
            draft.targets.add((cal["ref"], step["target_id"]))
        else:
            row = self.store.one(
                "SELECT o.*,r.etag,r.target FROM occurrences o JOIN resources r ON r.ref=o.resource_ref "
                "WHERE o.ref=%s AND o.calendar_ref=%s",
                (action.get("event_ref"), cal["ref"]),
            )
            if not row:
                raise CalendarError("not_found_or_forbidden")
            if row["resource_ref"] in draft.touched or (cal["ref"], row["target"]) in draft.targets:
                raise CalendarError("duplicate_resource_action")
            draft.touched.add(row["resource_ref"])
            draft.targets.add((cal["ref"], row["target"]))
            draft.selected.add(action["event_ref"])
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
                steps = self._series_steps(key, cal, step, scope, len(draft.prepared), read)
                # One calendar entry per step, as validate_plan recomputes the scope hash.
                draft.calendars.extend([cal] * (len(steps) - 1))
                draft.prepared.extend(steps)
                return None
        return step, cal, label

    def _plan_linked(self, key, p, action, kind, step, cal, label, carry_projections, draft):
        """The drives a create with an activity, or a carried update or delete, adds to the plan."""
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
            draft.skipped += skips
            draft.carried = True
        elif kind != "create" and carry_projections and (found := self._carry(key, p, step, action["event_ref"], cal, draft.skipped, draft.selected)):
            source, scope, drives = found
            draft.carried = True
        else:
            return
        if source["ref"] not in [s["calendar_ref"] for s in draft.sources]:
            draft.sources.append({"calendar_ref": source["ref"], "scope": scope})
            draft.source_calendars.append(source)
        for linked_step, target in drives:
            if (target["ref"], linked_step["target_id"]) in draft.targets:
                raise CalendarError("duplicate_resource_action")
            draft.targets.add((target["ref"], linked_step["target_id"]))
            draft.prepared.append(dict(linked_step, ordinal=len(draft.prepared)))
            draft.calendars.append(target)

    def _plan_finish(self, key, p, draft, read):
        """Limits, overlap check, the stored plan and its preview."""
        if draft.projected and not draft.prepared:
            raise CalendarError(
                "nothing_to_project",
                details={"projections_skipped": draft.skipped, "activities": sorted(draft.activities)},
            )
        if draft.unprojected and not draft.prepared:
            raise CalendarError("nothing_to_unproject", details={"projections_skipped": draft.skipped})
        # The writes of exceptions a series move carries (at most 50, ANYM-614) come on top of the plan's 16 steps.
        if len(draft.prepared) - sum(carried_step(s) for s in draft.prepared) > 16:
            raise CalendarError("too_many_steps")
        overlap_check = self._overlap_check(key, p, draft.prepared, draft.selected)
        plan_id = ref("plan")
        expires = datetime.now(timezone.utc) + timedelta(minutes=10)
        payload = {"steps": draft.prepared, **({"sources": draft.sources} if draft.sources else {})}
        if read is not None and any(carries(s) for s in draft.prepared):
            # The series read the preview listed the exceptions from: a re-preview at confirm reuses it.
            payload["series_read"] = read
        self.store.query(
            "INSERT INTO plans(id,principal_id,payload,scope_hash,expires_at) VALUES (%s,%s,%s,%s,%s)",
            (plan_id, p["id"], Jsonb(payload),
             self._scope_hash(p, draft.calendars + draft.source_calendars, any("invite" in s for s in draft.prepared),
                              any("series" in s for s in draft.prepared), any(carries(s) for s in draft.prepared)), expires),
        )
        return {
            "plan_id": plan_id,
            "expires_at": expires.isoformat(),
            "steps": [self._preview(s) for s in draft.prepared],
            "notifications": (
                "per step invite: guests of a create, emailed only with notify all; edits of events with guests are "
                "blocked" if any("invite" in s for s in draft.prepared) else "none; events with guests are blocked"
            ),
            "atomic": False,
            "overlap_check": overlap_check,
            **({"projections_skipped": draft.skipped} if draft.projected or draft.unprojected or draft.carried else {}),
        }

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
            # The scope and role only, and the exceptions a time move carries as its preview lists them: the
            # instance's provider ID and etag stay internal.
            shown["series"] = {k: step["series"][k] for k in ("scope", "role", "carried") if k in step["series"]}
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
        steps = plan["payload"]["steps"]
        if self._scope_hash(p, calendars, bool(invites), any("series" in s for s in steps),
                            any(carries(s) for s in steps)) != plan["scope_hash"]:
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
        if op["state"] == "partial" and op["error"] == "series_exception_mismatch":
            # Carried exceptions that did not end as previewed (ANYM-614): a definite failure naming each one.
            from .schedule import Scheduler

            scheduler = Scheduler(self)
            result.update(scheduler.mismatches(operation_id, plan["payload"]["steps"], scheduler.family(key, p)),
                          state="failed", detail_state="partial")
        elif op["state"] == "partial" and (first.get("series") or {}).get("role") == "replacement":
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

