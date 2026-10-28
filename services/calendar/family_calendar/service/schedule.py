"""calendar_schedule (ANYM-552): create, move or cancel one event in one call, on top of plan and apply.

A preview is an ordinary immutable plan with a confirm token: the plan row keeps the token's sha256, the tool and
the canonical request it resolved. Applying the token consumes the plan once: the plan row is bound to the one
operation it started (`plans.operation_id`), and every later apply of that plan, with any request_id, returns that
operation. Agent rulebook v1 (AGENT_API.md, ANYM-544) is the result and error shape; the calendar image ships
without the shared package, so the few constants it needs are repeated here and its tests compare them.
"""
from datetime import date, datetime, timedelta, timezone
import hashlib
import re
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg.types.json import Jsonb
from family_calendar.errors import CalendarError
from .core import DATE_VALUE, wall_time
from .providers import created_event_ref
from .store import instant, ref

TOOL = "schedule"
TOOL_NAME = "calendar_schedule"
DEFAULT_MINUTES = 30
DEFAULT_WAIT_S = 10
MAX_WAIT_S = 25
MAX_CANDIDATES = 10
# A token-only apply keeps operations.request_id NOT NULL with a key no caller can know: the plan ID is random
# and a schedule preview never shows it.
TOKEN_KEY = "token~"
REQUEST_ID = re.compile(r"[a-zA-Z0-9_.:-]{8,128}")
PLACEHOLDER_REQUEST_ID = "REPLACE_WITH_YOUR_ID"
CONTROL = frozenset(("dry_run", "confirm_token", "request_id", "wait_s", "selection_token"))
SELECTION_TTL_S = 600
# Rulebook `state` of each operation state (platform_contracts.agent.STATUS_STATES["calendar.operation"]).
STATES = {
    "queued": "pending", "running": "running", "verified": "succeeded", "conflict": "failed",
    "failed": "failed", "unknown": "unknown", "needs_attention": "unknown", "partial": "unknown",
}
SETTLED = frozenset(("verified", "conflict", "failed", "unknown", "needs_attention", "partial"))
KINDS = {"create": "create", "move": "update", "cancel": "delete"}


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def request_of(arguments):
    """The canonical request a preview resolves: every argument except the control fields."""
    return {k: v for k, v in sorted(arguments.items()) if k not in CONTROL}


def selection(candidates, more_next=None):
    details = {"candidates": candidates[:MAX_CANDIDATES]}
    if len(candidates) > MAX_CANDIDATES and more_next:
        details.update(more=True, next=more_next)
    return CalendarError("selection_required", details=details)


def missing(field, reason="required"):
    return CalendarError("invalid_arguments", [{"field": field, "reason": reason}])


class Choice(CalendarError):
    """The calendars a request may go to. The tool turns it into `selection_required` outside every transaction,
    with a stored selection, so no candidate's `next` repeats the caller's title (the gateway drops payload)."""

    def __init__(self, calendars, request):
        super().__init__("selection_required")
        self.calendars, self.request = calendars, request


class Scheduler:
    def __init__(self, service):
        self.service, self.store = service, service.store

    # --- context: schema, family timezone, calendars -------------------------------------------------------

    def ready(self):
        if not self.store.one("SELECT 1 AS ok FROM migrations WHERE version=4"):
            raise CalendarError("schema_not_ready")

    @staticmethod
    def writable(family, kind):
        """Visible calendars this key may plan and apply `kind` (create, update, delete) in."""
        return [c for c in family["visible"] if kind in c["allowed_event_actions"]]

    def family(self, key, p):
        group = self.store.one("SELECT timezone,default_calendar_ref FROM groups WHERE id=%s", (p["group_id"],))
        group = group or {"timezone": None, "default_calendar_ref": None}
        visible = self.service.calendars(key)["calendars"]
        creatable = [c for c in visible if "create" in c["allowed_event_actions"]]
        default = next((c for c in creatable if c["calendar_ref"] == group["default_calendar_ref"]), None)
        if default is None and len(creatable) == 1:
            default = creatable[0]
        name = group["timezone"] or (default or {}).get("timezone") or "UTC"
        try:
            zone = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            name, zone = "UTC", ZoneInfo("UTC")
        return {"timezone": name, "zone": zone, "default": default, "creatable": creatable, "visible": visible}

    @staticmethod
    def context(family):
        """What every calendar_schedule answer states: the family timezone and the current time in it."""
        return {"timezone": family["timezone"],
                "now": datetime.now(timezone.utc).astimezone(family["zone"]).isoformat(timespec="seconds")}

    def calendar(self, name, family, kind, request):
        """The one visible calendar `name` names in full (label, case-insensitive, or calendar_ref), when this key
        may `kind` in it. A name several visible calendars share, or no full name, is the choice among the writable
        ones; ambiguity is judged before write access, so a read-only twin never makes a name look unique."""
        writable = self.writable(family, kind)
        exact = [c for c in family["visible"]
                 if name == c["calendar_ref"] or c["label"].casefold() == name.casefold()]
        if len(exact) == 1:
            if exact[0] in writable:
                return exact[0]
            raise CalendarError("not_found_or_forbidden")
        raise self.choice([c for c in writable if c in exact]
                          or [c for c in writable if name.casefold() in c["label"].casefold()] or writable, request)

    @staticmethod
    def choice(calendars, request):
        """The calendars to choose from; none writable: not_found_or_forbidden."""
        if not calendars:
            return CalendarError("not_found_or_forbidden")
        return Choice(calendars, request)

    def chosen(self, p, choice):
        """`selection_required` for a Choice: each candidate previews the stored request in its calendar."""
        token = secrets.token_urlsafe(24)
        self.store.query(
            "INSERT INTO schedule_selections(token_hash,principal_id,request,expires_at) "
            "VALUES (%s,%s,%s,now()+make_interval(secs=>%s))",
            (token_hash(token), p["id"], Jsonb({k: v for k, v in choice.request.items() if k != "calendar"}),
             SELECTION_TTL_S),
        )
        return selection([
            {"id": c["calendar_ref"], "label": c["label"][:200], "kind": "calendar",
             "next": {"tool": TOOL_NAME, "arguments": {"selection_token": token, "calendar": c["calendar_ref"],
                                                       "dry_run": True}}}
            for c in choice.calendars
        ])

    def selected(self, p, token):
        """The request a selection stored; the caller's own arguments then add the chosen calendar."""
        if not isinstance(token, str) or not 8 <= len(token) <= 256:
            raise CalendarError("invalid_selection_token")
        row = self.store.one(
            "SELECT request FROM schedule_selections WHERE token_hash=%s AND principal_id=%s AND expires_at>now()",
            (token_hash(token), p["id"]),
        )
        if not row:
            raise CalendarError("invalid_selection_token")
        return row["request"]

    # --- resolution: one request to one plan action --------------------------------------------------------

    def when(self, value, zone):
        """An ISO 8601 start: with an offset an instant, without one wall time in the family timezone."""
        if not isinstance(value, str):
            raise missing("start")
        local = wall_time(value, zone)
        return local if isinstance(local, datetime) else instant(value)

    def action(self, key, p, request, family):
        kind = request.get("action")
        if kind not in KINDS:
            raise missing("action")
        zone = family["zone"]
        if kind == "create":
            if not request.get("title"):
                raise missing("title")
            if "start" not in request:
                raise missing("start")
            if "calendar" in request:
                cal = self.calendar(request["calendar"], family, "create", request)
            elif family["default"]:
                cal = family["default"]
            else:
                raise self.choice(family["creatable"], request)
            start = request["start"]
            if isinstance(start, str) and DATE_VALUE.fullmatch(start):
                if "duration" in request:
                    raise missing("duration", "format")
                event = {"title": request["title"], "all_day": True, "start": start}
            else:
                # Elapsed time: a duration across a DST change keeps its minutes.
                a = self.when(start, zone).astimezone(timezone.utc)
                b = a + timedelta(minutes=request.get("duration", DEFAULT_MINUTES))
                event = {"title": request["title"], "start": a.astimezone(zone).isoformat(),
                         "end": b.astimezone(zone).isoformat(), "timezone": family["timezone"]}
            return {"action": "create", "calendar_ref": cal["calendar_ref"], "event": event}, cal
        if "event" not in request:
            raise missing("event")
        if kind == "move" and "start" not in request:
            raise missing("start")
        calendars = self.writable(family, KINDS[kind])
        if "calendar" in request:
            calendars = [self.calendar(request["calendar"], family, KINDS[kind], request)]
        target = request["event"]
        if isinstance(target, dict):
            self.choose(request, target, calendars, zone)
        occurrence = self.store.one(
            "SELECT ref,calendar_ref,start_at,end_at,details FROM occurrences WHERE ref=%s AND calendar_ref=ANY(%s)",
            (target, [c["calendar_ref"] for c in calendars]),
        )
        if not occurrence:
            raise CalendarError("not_found_or_forbidden")
        cal = next(c for c in calendars if c["calendar_ref"] == occurrence["calendar_ref"])
        action = {"action": KINDS[kind], "calendar_ref": cal["calendar_ref"], "event_ref": target}
        if kind == "move":
            if occurrence["details"].get("all_day"):
                if not (isinstance(request["start"], str) and DATE_VALUE.fullmatch(request["start"])):
                    raise missing("start", "format")
                # All-day bounds are midnights in the calendar's zone: count dates there, not elapsed days.
                try:
                    own = ZoneInfo(cal["timezone"])
                except (ZoneInfoNotFoundError, TypeError, ValueError):
                    own = zone
                days = max(1, (occurrence["end_at"].astimezone(own).date()
                               - occurrence["start_at"].astimezone(own).date()).days)
                first = date.fromisoformat(request["start"])
                action["event"] = {"all_day": True, "start": first.isoformat(),
                                   "end": (first + timedelta(days=days)).isoformat()}
            else:
                a = self.when(request["start"], zone).astimezone(timezone.utc)
                length = (timedelta(minutes=request["duration"]) if "duration" in request
                          else occurrence["end_at"] - occurrence["start_at"])
                action["event"] = {"start": a.astimezone(zone).isoformat(),
                                   "end": (a + length).astimezone(zone).isoformat(), "timezone": family["timezone"]}
        return action, cal

    def choose(self, request, target, calendars, zone):
        """A title and date never act: they list the matching events, each previewed by its event_ref."""
        title, day = target.get("title"), target.get("date")
        if not isinstance(day, str) or not DATE_VALUE.fullmatch(day):
            raise missing("event/date", "format")
        start = datetime.combine(date.fromisoformat(day), datetime.min.time(), zone)
        end = datetime.combine(date.fromisoformat(day) + timedelta(days=1), datetime.min.time(), zone)
        refs = [c["calendar_ref"] for c in calendars]
        pattern = "%" + re.sub(r"([%_\\])", r"\\\1", title or "") + "%"
        rows = self.store.query(
            "SELECT ref,calendar_ref,start_at,end_at FROM occurrences WHERE calendar_ref=ANY(%s) "
            "AND start_at<%s AND end_at>%s AND coalesce(details->>'title','') ILIKE %s "
            "ORDER BY start_at,ref LIMIT %s",
            (refs, end, start, pattern, MAX_CANDIDATES + 1),
        )
        agenda = {"tool": "calendar_agenda",
                  "arguments": {"calendar_refs": refs[:32], "start": start.isoformat(), "end": end.isoformat()}}
        if not rows:
            raise CalendarError("event_not_found", details={"next": agenda})
        moved = {k: request[k] for k in ("start", "duration") if k in request}
        raise selection([
            {"id": row["ref"], "kind": "event", "account": row["calendar_ref"],
             # The label names the time only: the title is the caller's own words.
             "label": row["start_at"].astimezone(zone).strftime("%a %d %b %H:%M") + "–"
             + row["end_at"].astimezone(zone).strftime("%H:%M"),
             "next": {"tool": TOOL_NAME, "arguments": {"action": request["action"], "event": row["ref"], **moved,
                                                       "dry_run": True}}}
            for row in rows
        ], agenda)

    # --- previews ----------------------------------------------------------------------------------------

    def preview(self, key, p, request, family):
        """Resolve `request` into a fresh plan with a token. Returns the plan row and the token."""
        action, cal = self.action(key, p, request, family)
        plan = self.service.plan(key, [action])
        token = secrets.token_urlsafe(24)
        row = self.store.one(
            "UPDATE plans SET token_hash=%s,tool=%s,request=%s WHERE id=%s AND principal_id=%s RETURNING *",
            (token_hash(token), TOOL, Jsonb(request), plan["plan_id"], p["id"]),
        )
        return row, token, dict(plan, calendar=cal)

    @staticmethod
    def view(plan, family):
        """The exact plan, times in the family timezone."""
        zone = family["zone"]
        step = plan["steps"][0]
        event = step.get("event", {})
        shown = {"action": {"create": "create", "update": "move", "delete": "cancel"}[step["action"]],
                 "calendar": {"calendar_ref": plan["calendar"]["calendar_ref"], "label": plan["calendar"]["label"]}}
        if "title" in event:
            shown["title"] = event["title"]
        if event.get("all_day"):
            shown.update(all_day=True, start=event["start"], end=event.get("end"))
        elif event:
            shown.update(start=instant(event["start"]).astimezone(zone).isoformat(),
                         end=instant(event["end"]).astimezone(zone).isoformat())
        before = step.get("before")
        if before:
            shown["event_ref"] = plan["event_ref"]
            shown["before"] = {k: before[k] if before.get("all_day") else instant(before[k]).astimezone(zone).isoformat()
                               for k in ("start", "end")}
        shown["overlap_check"] = plan["overlap_check"]
        shown["expires_at"] = plan["expires_at"]
        return shown

    def previewed(self, key, p, request, family):
        row, token, plan = self.preview(key, p, request, family)
        plan["event_ref"] = request.get("event")
        return row, token, {
            "state": "previewed", "preview": self.view(plan, family), "confirm_token": token,
            "next": {"tool": TOOL_NAME, "arguments": {"confirm_token": token}},
        }

    @staticmethod
    def fingerprint(row):
        """What a preview resolved, without the random IDs a create mints anew on every plan."""
        step = row["payload"]["steps"][0]
        return {
            "calendar": step["calendar_ref"],
            "scope": row["scope_hash"],
            "event": [step.get(k) for k in ("target_id", "expected_etag", "before")] if step["action"] != "create" else None,
            "times": step.get("span") or step.get("event"),
        }

    # --- operations ----------------------------------------------------------------------------------------

    def outcome(self, op, plan, family):
        state = STATES[op["state"]]
        result = {"state": state, "detail_state": op["state"], "operation": {"module": "calendar", "id": op["id"]}}
        if not op["request_id"].startswith(TOKEN_KEY):
            result["request_id"] = op["request_id"]
        step = plan["payload"]["steps"][0]
        if state == "succeeded" and step["action"] == "create":
            receipt = self.store.one(
                "SELECT receipt FROM operation_steps WHERE operation_id=%s AND ordinal=0", (op["id"],)
            )["receipt"] or {}
            if receipt.get("resource_ref"):
                result["event_ref"] = created_event_ref(step["provider"], receipt["resource_ref"], step["target_id"])
        elif state == "succeeded" and step["action"] == "update":
            result["event_ref"] = (plan["request"] or {}).get("event")
        if state == "failed":
            result["error"] = op["error"]
        result["next"] = (
            {"tool": "calendar_operation", "arguments": {"operation_id": op["id"]}}
            if state in ("pending", "running", "unknown") else None
        )
        if state == "unknown":
            result["next_action"] = "reconcile; do not create again"
        return result

    def replay(self, operation_id, family):
        op = self.store.one("SELECT * FROM operations WHERE id=%s", (operation_id,))
        plan = self.store.one("SELECT * FROM plans WHERE id=%s", (op["plan_id"],))
        return self.outcome(op, plan, family)

    def start(self, c, p, plan, request_id):
        """Bind `plan` to a new operation inside the caller's transaction; the plan row is then consumed."""
        self.service.validate_plan(plan, p["id"])
        operation_id = ref("op")
        c.execute(
            "INSERT INTO operations(id,principal_id,plan_id,request_id) VALUES (%s,%s,%s,%s)",
            (operation_id, p["id"], plan["id"], request_id),
        )
        for step in plan["payload"]["steps"]:
            c.execute("INSERT INTO operation_steps(operation_id,ordinal) VALUES (%s,%s)", (operation_id, step["ordinal"]))
        c.execute("UPDATE plans SET operation_id=%s WHERE id=%s AND operation_id IS NULL", (operation_id, plan["id"]))
        return operation_id

    def lock(self, c, *parts):
        """Never waits: a waiting caller would hold a pooled connection the holder may need (pool of 8)."""
        if not c.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS ok", ("".join(parts),)
                         ).fetchone()["ok"]:
            raise CalendarError("in_progress")

    # --- the tool ----------------------------------------------------------------------------------------

    def schedule(self, key, arguments, tool=TOOL):
        """One call: a preview, a token apply or a keyed apply. Returns (result, operation_id or None, family);
        an error states the family timezone and now too."""
        self.ready()
        p = self.service.principal(key)
        family = self.family(key, p)
        try:
            try:
                result, operation_id = self.run(key, p, arguments, family, tool)
            except Choice as choice:
                raise self.chosen(p, choice) from None
        except CalendarError as e:
            e.details = {**e.details, **self.context(family)}
            raise
        return result, operation_id, family

    def run(self, key, p, arguments, family, tool):
        request_id = arguments.get("request_id")
        if request_id is not None and (not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id)
                                       or request_id == PLACEHOLDER_REQUEST_ID):
            raise missing("request_id", "pattern")
        request = request_of(arguments)
        if "selection_token" in arguments:
            if "confirm_token" in arguments:
                raise missing("selection_token", "unknown_field")
            request = {**self.selected(p, arguments["selection_token"]), **request}
        if arguments.get("dry_run"):
            if "confirm_token" in arguments:
                raise missing("confirm_token", "unknown_field")
            return self.previewed(key, p, request, family)[2], None
        token = arguments.get("confirm_token")
        if token is not None:
            return self.consume(key, p, token, request, request_id, family, tool)
        if request_id is None:
            raise CalendarError("invalid_arguments", [{"field": "request_id", "reason": "required"}], details={
                "hint": "request_id is required for a live effect: choose one per intent and keep it on retries.",
                "next": {"tool": TOOL_NAME, "arguments": {**arguments, "request_id": PLACEHOLDER_REQUEST_ID}},
            })
        # One connection for the whole call: the preview and validation inside join this transaction.
        with self.store.bound() as c:
            # The same lock key as calendar_apply: one request_id, one operation, whichever tool used it.
            self.lock(c, p["id"], request_id)
            old = c.execute(
                "SELECT o.id,p.tool,p.request FROM operations o JOIN plans p ON p.id=o.plan_id "
                "WHERE o.principal_id=%s AND o.request_id=%s", (p["id"], request_id),
            ).fetchone()
            if old:
                # The stored request is compared before anything is resolved again, so drift never turns a replay
                # into a conflict.
                if old["tool"] != tool or old["request"] != request:
                    raise CalendarError("idempotency_conflict")
                operation_id = old["id"]
            else:
                row = self.preview(key, p, request, family)[0]
                operation_id = self.start(c, p, row, request_id)
        return self.replay(operation_id, family), operation_id

    def consume(self, key, p, token, request, request_id, family, tool):
        if not isinstance(token, str) or not 8 <= len(token) <= 256:
            raise CalendarError("invalid_confirm_token")
        found = self.store.one(
            "SELECT id FROM plans WHERE token_hash=%s AND principal_id=%s AND tool=%s", (token_hash(token), p["id"], tool)
        )
        if not found:
            raise CalendarError("invalid_confirm_token")
        stale = None
        with self.store.bound() as c:
            self.lock(c, "plan:", found["id"])
            if request_id is not None:
                self.lock(c, p["id"], request_id)
            plan = c.execute("SELECT * FROM plans WHERE id=%s FOR UPDATE", (found["id"],)).fetchone()
            if request and request != plan["request"]:
                raise missing("confirm_token", "unique")
            bound = c.execute(
                "SELECT id FROM operations WHERE principal_id=%s AND request_id=%s", (p["id"], request_id)
            ).fetchone() if request_id is not None else None
            if plan["operation_id"]:
                # A consumed plan returns its operation, after expiry or drift, whatever request_id comes with it.
                if bound and bound["id"] != plan["operation_id"]:
                    raise CalendarError("idempotency_conflict")
                operation_id = plan["operation_id"]
            else:
                step = plan["payload"]["steps"][0]
                # The calendar the preview resolved its name to, by calendar_ref: a rename since then is a changed
                # calendar with a fresh plan, never a new choice.
                again = dict(plan["request"], calendar=step["calendar_ref"]) if "calendar" in plan["request"] else (
                    plan["request"])
                try:
                    fresh, _, view = self.previewed(key, p, again, family)
                    changed = {k for k, v in self.fingerprint(fresh).items() if v != self.fingerprint(plan)[k]}
                    # A calendar named by its label that now has another label is a changed calendar too.
                    named = plan["request"].get("calendar")
                    if named and named != step["calendar_ref"] and (
                            view["preview"]["calendar"]["label"].casefold() != named.casefold()):
                        changed.add("calendar")
                    changed = sorted(changed)
                except CalendarError as e:
                    # The request no longer resolves as previewed: its calendar went away or became a choice, or
                    # its event did. The plan is stale; there is no fresh plan to offer.
                    # A calendar this key may no longer write to is a revocation (ANYM-544 matrix), not staleness.
                    kept = step["calendar_ref"] in {c["calendar_ref"] for c in self.writable(family, step["action"])}
                    if not kept or e.code not in ("selection_required", "not_found_or_forbidden", "event_not_found"):
                        raise
                    changed, view = ["calendar"] if isinstance(e, Choice) else ["event"], None
                if plan["expires_at"] <= datetime.now(timezone.utc):
                    changed = ["expires_at", *changed]
                if changed:
                    stale = (changed, view)
                    operation_id = None
                elif bound:
                    raise CalendarError("idempotency_conflict")
                else:
                    operation_id = self.start(c, p, plan, request_id or TOKEN_KEY + plan["id"])
        if stale:
            changed, view = stale
            raise CalendarError("preview_stale", details={"changed": changed, **(
                {"preview": view["preview"], "confirm_token": view["confirm_token"]} if view else {})})
        return self.replay(operation_id, family), operation_id

    def settled(self, operation_id):
        return self.store.one("SELECT state FROM operations WHERE id=%s", (operation_id,))["state"] in SETTLED
