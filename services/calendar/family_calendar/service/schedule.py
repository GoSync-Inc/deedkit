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
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg.types.json import Jsonb
from family_calendar.errors import CalendarError
from .core import DATE_VALUE, GUEST_WRITE, SERIES_SCOPES, SERIES_WRITE, wall_time
from .providers import created_event_ref, series_ref
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
# Guests of a create (ANYM-555): one plain address each, compared case-insensitively.
MAX_GUESTS = 20
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                   r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+")
INVITE_FIELDS = ("guests", "notify")
# The closest call after a series edit refused before any write: this occurrence alone, or (a following move from
# the series' first occurrence) the whole series.
CLOSEST_SCOPE = {"series_exceptions": "this", "series_starts_here": "all"}
# A preview of a series time move asks the worker for one read of the series (ANYM-614) and waits this long for it,
# only while a worker runs; a read not answered by then gives the plan without carried exceptions (the worker
# refuses one before any write, as before) and `exceptions_read: pending` with the same preview as `next`.
SERIES_READ_WAIT_S = 4
SERIES_READ_PENDING_S = 60
PENDING = object()
SERIES_READ_REUSE_S = 15
# What a series scope of a move or cancel does (ANYM-556), as the preview states it.
SERIES_EFFECTS = {
    ("all", "move"): "Every occurrence of the series moves by this occurrence's shift from its original start, in "
                     "the wall-clock time of the series' own timezone, so local times hold across a DST change. If "
                     "any occurrence was changed or cancelled on its own, nothing is written: series_exceptions.",
    ("all", "cancel"): "The whole series is deleted: every occurrence, also those changed on their own.",
    ("following", "move"): "First a new series from this occurrence on is created at the new time, with the same "
                           "rule and fields; then the original series ends just before this occurrence's original "
                           "start. Earlier occurrences, also those changed on their own, stay as they are. If an "
                           "occurrence from here on was changed or cancelled on its own, nothing is written: "
                           "series_exceptions.",
    ("following", "cancel"): "The series ends just before this occurrence's original start; earlier occurrences, "
                             "also those changed on their own, stay as they are. If an occurrence from here on was "
                             "changed on its own, nothing is written: series_exceptions.",
    # A time move previewed with its exceptions (ANYM-614): `exceptions` lists each one in range and its fate.
    ("all", "move", "exceptions"): "Every occurrence of the series moves by this occurrence's shift from its "
                                   "original start, in the wall-clock time of the series' own timezone. Each "
                                   "occurrence changed or cancelled on its own (listed in exceptions) is then "
                                   "written once more so it keeps its change; if the series or any of them changed "
                                   "since this preview, nothing is written: preview_stale.",
    ("following", "move", "exceptions"): "First a new series from this occurrence on is created at the new time, "
                                         "then the original series ends just before this occurrence's original "
                                         "start; earlier occurrences stay as they are. Each occurrence from here on "
                                         "changed or cancelled on its own (listed in exceptions) is then written in "
                                         "the new series so it keeps its change; if the series or any of them "
                                         "changed since this preview, nothing is written: preview_stale.",
}
# What happens to one exception of a time move (ANYM-614), as the preview's exceptions list states it.
EXCEPTION_EFFECTS = {
    "cancelled": "Stays cancelled: the occurrence at its original start plus the shift is cancelled again.",
    "moves": "Moves with the series and keeps its own title, place, description and other own fields.",
    "kept": "Keeps its own start and end (and its own fields); follow_up moves it back into the series.",
}


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def request_of(arguments):
    """The canonical request a preview resolves: every argument except the control fields."""
    return {k: v for k, v in sorted(arguments.items()) if k not in CONTROL}


def texts(value, key=None):
    """The caller's own words in a request: every string outside the fields the gateway reads as references or
    fixed values (action, calendar, start, date, IDs), with its words of 6 or more characters."""
    if isinstance(value, dict):
        return {t for k, v in value.items() for t in texts(v, k)}
    if isinstance(value, str) and value.strip() and key not in ("action", "calendar", "start", "date", "request_id"):
        return {value} | {word for word in value.split() if len(word) >= 6}
    return set()


def label_for(label, request, n):
    """A candidate label the gateway relays: one that repeats the caller's title or query would drop the candidate
    (ANYM-544 payload filter), so that label falls back to the first of `#n`, `#na` … `#nz` that repeats none."""
    def found(piece, text):
        return piece in text if len(piece) >= 4 else re.search(rf"(?<!\w){re.escape(piece)}(?!\w)", text) is not None
    pieces = texts(request)
    for shown in (label[:200], f"#{n}", *(f"#{n}{c}" for c in "abcdefghijklmnopqrstuvwxyz")):
        if not any(found(t, shown) for t in pieces):
            return shown
    return label[:200]


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


class Unsupported(CalendarError):
    """Guests asked of a calendar whose provider cannot invite (`capability_unsupported`) or whose owner has not
    opted in to invitations (`invitations_not_enabled`). The tool turns it into that error outside every
    transaction, with a stored selection for the same event without guests."""

    def __init__(self, calendar, request, code="capability_unsupported"):
        super().__init__(code)
        self.calendar, self.request = calendar, request


class SeriesUnsupported(CalendarError):
    """A series scope asked of a calendar whose provider does not write it (ANYM-556). The tool turns it into
    `capability_unsupported` outside every transaction, with a stored selection for this occurrence only."""

    def __init__(self, calendar, request):
        super().__init__("capability_unsupported")
        self.calendar, self.request = calendar, request


def invite(request):
    """The create's guests and notify, checked before anything resolves; None without guests."""
    guests, notify = request.get("guests"), request.get("notify")
    if guests is None:
        if notify is not None:
            raise missing("guests")
        return None
    if notify not in ("none", "all"):
        raise missing("notify", "required" if notify is None else "enum")
    if not isinstance(guests, list) or not 1 <= len(guests) <= MAX_GUESTS:
        raise missing("guests", "size")
    seen = set()
    for n, guest in enumerate(guests):
        if not isinstance(guest, str) or len(guest) > 254 or not EMAIL.fullmatch(guest):
            raise missing(f"guests/{n}", "format")
        if guest.casefold() in seen:
            raise missing(f"guests/{n}", "unique")
        seen.add(guest.casefold())
    return {"guests": list(guests), "notify": notify}


def notifications(event, guests, notify):
    """Who is told what (ANYM-555): with notify all, each guest's invitation email and its fields."""
    if notify == "none":
        # Google's sendUpdates=none asks for no email but does not promise it (events.insert reference).
        return {"notify": "none", "recipients": [], "emails_requested": False,
                "summary": "Nobody is notified by DeedKit: Google is asked to send no invitation email "
                           "(sendUpdates=none). Google does not guarantee that for every guest, and it can still "
                           "show the event, with these fields, in a guest's own calendar."}
    # Google's default guest list visibility: each guest sees the others.
    receives = {**{k: event.get(k) for k in ("title", "start", "end", "timezone", "description", "location")},
                "guests": guests}
    return {"notify": "all", "recipients": [{"email": g, "receives": receives} for g in guests],
            "summary": f"Google emails an invitation from this calendar's account to each of {len(guests)} "
                       "guest(s), with the fields below and the guest list."}


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
            {"id": c["calendar_ref"], "label": label_for(c["label"], choice.request, n), "kind": "calendar",
             "next": {"tool": TOOL_NAME, "arguments": {"selection_token": token, "calendar": c["calendar_ref"],
                                                       "dry_run": True}}}
            for n, c in enumerate(choice.calendars, 1)
        ])

    def unsupported(self, p, refusal):
        """`capability_unsupported` for guests: `next` previews the same event without guests in that calendar."""
        cal, token = refusal.calendar, secrets.token_urlsafe(24)
        self.store.query(
            "INSERT INTO schedule_selections(token_hash,principal_id,request,expires_at) "
            "VALUES (%s,%s,%s,now()+make_interval(secs=>%s))",
            (token_hash(token), p["id"], Jsonb({k: v for k, v in refusal.request.items()
                                                 if k not in ("calendar", *INVITE_FIELDS)}), SELECTION_TTL_S),
        )
        details = {
            "capability": "guests", "provider": cal["provider"],
            "calendar": {"calendar_ref": cal["calendar_ref"], "label": cal["label"]},
            "next": {"tool": TOOL_NAME, "arguments": {"selection_token": token, "calendar": cal["calendar_ref"],
                                                      "dry_run": True}},
        }
        if refusal.code == "invitations_not_enabled":
            details["owner_step"] = "service allow-invitations --calendar " + cal["calendar_ref"]
        return CalendarError(refusal.code, details=details)

    def series_unsupported(self, p, refusal):
        """`capability_unsupported` for a series scope: `next` previews the same change for this occurrence only."""
        cal, token = refusal.calendar, secrets.token_urlsafe(24)
        self.store.query(
            "INSERT INTO schedule_selections(token_hash,principal_id,request,expires_at) "
            "VALUES (%s,%s,%s,now()+make_interval(secs=>%s))",
            (token_hash(token), p["id"], Jsonb(dict(refusal.request, series="this")), SELECTION_TTL_S),
        )
        return CalendarError("capability_unsupported", details={
            "capability": "series", "provider": cal["provider"],
            "calendar": {"calendar_ref": cal["calendar_ref"], "label": cal["label"]},
            "scopes": SERIES_WRITE[cal["provider"]]["scopes"],
            "next": {"tool": TOOL_NAME, "arguments": {"selection_token": token, "dry_run": True}},
        })

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
        if kind != "create":
            for field in INVITE_FIELDS:
                if field in request:
                    raise missing(field, "unknown_field")
        elif "series" in request:
            raise missing("series", "unknown_field")
        scope = request.get("series", "this")
        if scope not in SERIES_SCOPES:
            raise missing("series", "enum")
        if kind == "create":
            guests = invite(request)
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
            if guests and not GUEST_WRITE[cal["provider"]]["supported"]:
                raise Unsupported(cal, request)
            if guests and not self.service.invitations_enabled(cal["calendar_ref"]):
                raise Unsupported(cal, request, "invitations_not_enabled")
            return {"action": "create", "calendar_ref": cal["calendar_ref"], "event": event,
                    **({"invite": guests} if guests else {})}, cal
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
        if isinstance(target, str) and target.startswith("series-"):
            # A series handle names the replacement series of one of this principal's partial following moves:
            # its undo is a cancel of that whole series, resolved from the durable record, never the cache.
            if kind != "cancel" or scope != "all":
                raise missing("series", "enum")
            made = self.service.replacement_of(p, target)
            cal = next((c for c in calendars if c["calendar_ref"] == made["calendar_ref"]), None)
            if cal is None:
                raise CalendarError("not_found_or_forbidden")
            return {"action": "delete", "calendar_ref": cal["calendar_ref"], "replacement": target}, cal
        occurrence = self.store.one(
            "SELECT ref,calendar_ref,start_at,end_at,details FROM occurrences WHERE ref=%s AND calendar_ref=ANY(%s)",
            (target, [c["calendar_ref"] for c in calendars]),
        )
        if not occurrence:
            raise CalendarError("not_found_or_forbidden")
        cal = next(c for c in calendars if c["calendar_ref"] == occurrence["calendar_ref"])
        action = {"action": KINDS[kind], "calendar_ref": cal["calendar_ref"], "event_ref": target}
        if scope != "this":
            if not occurrence["details"].get("recurring"):
                raise missing("series", "not_recurring")
            if scope not in SERIES_WRITE.get(cal["provider"], {}).get("scopes", ()):
                raise SeriesUnsupported(cal, request)
            action["series"] = scope
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
        moved = {k: request[k] for k in ("start", "duration", "series") if k in request}
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

    def series_read(self, p, action, cal, request):
        """The worker's read of the series a time move of `all` or `following` edits on Google (ANYM-614), asked for
        and waited on here, outside every transaction: the read, PENDING when no worker answered it in time, or None
        for any other request and before schema 7 and its grants (the plan then carries no exceptions, and the
        worker refuses one before any write)."""
        if action.get("series") not in ("all", "following") or action["action"] != "update" or (
                cal["provider"] != "google"):
            return None
        if not self.store.one(
            "SELECT coalesce(has_table_privilege(to_regclass('series_reads'),'SELECT') "
            "AND has_table_privilege(to_regclass('series_reads'),'INSERT'),false) AS ok"
        )["ok"]:
            return None
        found = self.store.one(
            "SELECT r.target FROM occurrences o JOIN resources r ON r.ref=o.resource_ref "
            "WHERE o.ref=%s AND o.calendar_ref=%s", (action["event_ref"], cal["calendar_ref"]))
        if not found:
            return None
        row = self.store.one(
            "SELECT id FROM series_reads WHERE principal_id=%s AND calendar_ref=%s AND target=%s AND ("
            "(state IN ('prepared','sending') AND created_at>now()-make_interval(secs=>%s)) OR "
            "(state IN ('verified','failed') AND created_at>now()-make_interval(secs=>%s))) "
            "ORDER BY created_at DESC LIMIT 1",
            (p["id"], cal["calendar_ref"], found["target"], SERIES_READ_PENDING_S, SERIES_READ_REUSE_S))
        read_id = row["id"] if row else ref("read")
        if not row:
            self.store.query("INSERT INTO series_reads(id,principal_id,calendar_ref,target) VALUES (%s,%s,%s,%s)",
                             (read_id, p["id"], cal["calendar_ref"], found["target"]))
        # The worker's session lock (worker.py run): without a running worker nobody answers, so nothing waits.
        running = self.store.one(
            "SELECT EXISTS(SELECT 1 FROM pg_locks l, (SELECT hashtextextended(%s,0) AS k) h "
            "WHERE l.locktype='advisory' AND l.granted AND l.objsubid=1 "
            "AND l.classid=((h.k>>32)&4294967295)::oid AND l.objid=(h.k&4294967295)::oid) AS ok",
            (self.store.lock_key("worker", "singleton"),))["ok"]
        deadline = time.monotonic() + (SERIES_READ_WAIT_S if running else 0)
        while True:
            row = self.store.one("SELECT state,result,error FROM series_reads WHERE id=%s", (read_id,))
            if row["state"] == "verified":
                return row["result"]
            if row["state"] == "failed":
                raise CalendarError(row["error"] or "provider_unavailable")
            if time.monotonic() >= deadline:
                return PENDING
            time.sleep(0.25)

    def preview(self, key, p, request, family, read=None, ask=False):
        """Resolve `request` into a fresh plan with a token. Returns the plan row and the token. A dry run (`ask`)
        of a series time move reads the series first; a confirm re-previews with the plan's own read."""
        action, cal = self.action(key, p, request, family)
        pending = False
        if ask and read is None:
            read = self.series_read(p, action, cal, request)
            pending, read = read is PENDING, (None if read is PENDING else read)
        try:
            plan = self.service.plan(key, [action], invitations=True, series=True, read=read)
        except CalendarError as e:
            if e.code != "series_exceptions_too_many":
                raise
            # A following move from a later occurrence carries no more than the limit.
            later = e.details.get("later")
            e.details = {k: v for k, v in e.details.items() if k in ("count", "limit")}
            if later:
                zone, at = family["zone"], later["original_start"]
                day = at if "T" not in at else instant(at).astimezone(zone).date().isoformat()
                e.details["next"] = {"tool": TOOL_NAME, "arguments": {
                    "action": "move", "event": {"date": day},
                    "start": later["start"] if "T" not in later["start"] else (
                        instant(later["start"]).astimezone(zone).isoformat()),
                    **{k: request[k] for k in ("duration",) if k in request}, "series": "following",
                    "dry_run": True}}
            raise
        token = secrets.token_urlsafe(24)
        row = self.store.one(
            "UPDATE plans SET token_hash=%s,tool=%s,request=%s WHERE id=%s AND principal_id=%s RETURNING *",
            (token_hash(token), TOOL, Jsonb(request), plan["plan_id"], p["id"]),
        )
        if pending:
            plan["exceptions_read"] = {"state": "pending", "next": {
                "tool": TOOL_NAME, "arguments": {**request, "dry_run": True}}}
        return row, token, dict(plan, calendar=cal)

    @staticmethod
    def primary(steps):
        """The step a preview and its result describe: a following move's truncation, not its replacement."""
        return next((s for s in steps if s["action"] != "create"), steps[0])

    @staticmethod
    def view(plan, family):
        """The exact plan, times in the family timezone."""
        zone = family["zone"]
        steps = plan["steps"]
        step = Scheduler.primary(steps)
        # A following move's new times are its replacement's.
        event = next((s["event"] for s in steps if s.get("event")), {})
        shown = {"action": {"create": "create", "update": "move", "delete": "cancel"}[step["action"]],
                 "calendar": {"calendar_ref": plan["calendar"]["calendar_ref"], "label": plan["calendar"]["label"]}}
        if "title" in event:
            shown["title"] = event["title"]
        if event.get("all_day"):
            shown.update(all_day=True, start=event["start"], end=event.get("end"))
        elif event:
            shown.update(start=instant(event["start"]).astimezone(zone).isoformat(),
                         end=instant(event["end"]).astimezone(zone).isoformat())
        if step.get("invite"):
            guests = step["invite"]["guests"]
            shown["guests"] = guests
            shown["notifications"] = notifications(
                {**event, **{k: shown.get(k) for k in ("start", "end")}}, guests, step["invite"]["notify"])
        before = step.get("before")
        if before:
            shown["event_ref"] = plan["event_ref"]
            shown["before"] = {k: before[k] if before.get("all_day") else instant(before[k]).astimezone(zone).isoformat()
                               for k in ("start", "end")}
        scope = (step.get("series") or {}).get("scope")
        if scope and "occurrence" not in step:
            # The undo of a partial following move: its replacement series, as created.
            shown.update(series=scope, effect="The replacement series this principal created is deleted, only if "
                                              "nobody changed it since.")
        elif scope:
            original = step["occurrence"]["original_start"]
            carried = step["series"].get("carried")
            shown.update(series=scope, effect=SERIES_EFFECTS[(scope, shown["action"])],
                         original_start=original if "T" not in original else (
                             instant(original).astimezone(zone).isoformat()),
                         overlap_scope="this occurrence only")
            names = ["create_replacement", "end_original"] if steps[0]["action"] == "create" else []
            if carried is not None:
                shown.update(effect=SERIES_EFFECTS[(scope, "move", "exceptions")], writes=len(steps),
                             exceptions=[Scheduler.exception(c, event, zone) for c in carried])
                names = (names or ["move_series"]) + ["carry_exception"] * (len(steps) - max(1, len(names)))
            if names:
                shown["steps"] = names
        if plan.get("exceptions_read"):
            # The read of the series was not answered in time: this plan carries no exception (an exception in
            # range refuses it before any write); the repeat previews with the read.
            shown["exceptions_read"] = plan["exceptions_read"]
        shown["overlap_check"] = plan["overlap_check"]
        shown["expires_at"] = plan["expires_at"]
        return shown

    @staticmethod
    def exception(carried, event, zone):
        """One exception of a time move as its preview lists it (ANYM-614): where the series had it, what happens
        to it, and for one that keeps its own time the follow-up that would move it back into the series."""
        def local(value):
            return value if "T" not in value else instant(value).astimezone(zone).isoformat()

        fate = carried["fate"]
        shown = {"original_start": local(carried["original_start"]), "fate": fate, "effect": EXCEPTION_EFFECTS[fate],
                 "series_start": local(carried["start"]), "write": carried["write"]}
        if fate == "kept":
            own = {k: local(v) for k, v in carried["own"].items()}
            arguments = {"action": "move", "event": {"date": own["start"][:10]}, "start": shown["series_start"]}
            if not event.get("all_day"):
                arguments["duration"] = int((instant(event["end"]) - instant(event["start"])).total_seconds() // 60)
            shown.update(own=own, follow_up={"tool": TOOL_NAME, "arguments": {
                **arguments, "series": "this", "dry_run": True}})
        return shown

    def previewed(self, key, p, request, family, read=None, ask=False):
        row, token, plan = self.preview(key, p, request, family, read, ask)
        plan["event_ref"] = request.get("event")
        return row, token, {
            "state": "previewed", "preview": self.view(plan, family), "confirm_token": token,
            "next": {"tool": TOOL_NAME, "arguments": {"confirm_token": token}},
        }

    @staticmethod
    def fingerprint(row):
        """What a preview resolved, without the random IDs a create mints anew on every plan."""
        steps = row["payload"]["steps"]
        step = Scheduler.primary(steps)
        timed = next((s for s in steps if s.get("span") or s.get("event")), step)
        return {
            "calendar": step["calendar_ref"],
            "scope": row["scope_hash"],
            "event": [step.get(k) for k in ("target_id", "expected_etag", "before")] if step["action"] != "create" else None,
            "times": timed.get("span") or timed.get("event"),
        }

    # --- operations ----------------------------------------------------------------------------------------

    def outcome(self, op, plan, family):
        steps = plan["payload"]["steps"]
        step = self.primary(steps)
        series = step.get("series")
        replaced = bool(series) and (steps[0].get("series") or {}).get("role") == "replacement"
        # Carried exceptions that did not end as previewed (ANYM-614): the series and the other exceptions were
        # written; a definite failure naming each such occurrence.
        mismatch = op["state"] == "partial" and op["error"] == "series_exception_mismatch"
        # A following move whose replacement exists and whose truncation was definitely refused (ANYM-556): a
        # definite failure with both series named, never an operation to reconcile.
        partial = replaced and op["state"] == "partial" and not mismatch
        state = "failed" if partial or mismatch else STATES[op["state"]]
        result = {"state": state, "detail_state": op["state"], "operation": {"module": "calendar", "id": op["id"]}}
        if not op["request_id"].startswith(TOKEN_KEY):
            result["request_id"] = op["request_id"]
        if (state == "succeeded" or mismatch) and series:
            # An occurrence's event_ref names its original start, which an all move changes: the series is the handle.
            result["series_ref"] = (step.get("occurrence") or series)["series_ref"]
            if replaced:
                result["replacement"] = {"series_ref": series_ref(step["calendar_ref"], steps[0]["target_id"])}
        elif state == "succeeded" and step["action"] == "create":
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
        if partial:
            result.update(self.partial(step, steps, family))
        elif mismatch:
            result.update(self.mismatches(op["id"], steps, family))
        elif state == "failed" and op["error"] == "preview_stale":
            request = plan["request"] or {}
            # Nothing was written: the series or one of its exceptions changed since the preview.
            result["next"] = {"tool": TOOL_NAME, "arguments": {
                **{k: request[k] for k in ("action", "event", "start", "duration", "series") if k in request},
                "dry_run": True}}
        elif state == "failed" and op["error"] in CLOSEST_SCOPE:
            request = plan["request"] or {}
            # Nothing was written; the same change for this occurrence alone (or the whole series) is the closest.
            result["next"] = {"tool": TOOL_NAME, "arguments": {
                **{k: request[k] for k in ("action", "event", "start", "duration") if k in request},
                "series": CLOSEST_SCOPE[op["error"]], "dry_run": True}}
        return result

    @staticmethod
    def partial(step, steps, family):
        """Both series of a following move whose truncation failed: a read-only look at them, and the preview that
        removes the replacement. Nothing is retried, deleted or overwritten on its own."""
        calendar = step["calendar_ref"]
        original = step["occurrence"]["series_ref"]
        replacement = series_ref(calendar, steps[0]["target_id"])
        at = step["occurrence"]["original_start"]
        start = (datetime.combine(date.fromisoformat(at), datetime.min.time(), family["zone"]) if "T" not in at
                 else instant(at).astimezone(family["zone"]))

        def inspect(first):
            # Each series from where it overlaps on: the replacement can start far from the original boundary.
            return {"tool": "calendar_agenda", "arguments": {
                "calendar_refs": [calendar], "start": (first - timedelta(days=7)).isoformat(),
                "end": (first + timedelta(days=21)).isoformat()}}

        moved = instant(steps[0]["span"]["start"]).astimezone(family["zone"])
        return {
            "partial": True,
            "handles": {"original": {"calendar_ref": calendar, "series_ref": original, "inspect": inspect(start)},
                        "replacement": {"calendar_ref": calendar, "series_ref": replacement,
                                        "inspect": inspect(moved)}},
            "next": inspect(start),
            "undo": {"tool": TOOL_NAME, "arguments": {"action": "cancel", "event": replacement, "series": "all",
                                                      "dry_run": True}},
            "next_action": "inspect both series; the original was not ended, so occurrences from here on show twice; "
                           "undo previews removing the replacement",
        }

    def mismatches(self, operation_id, steps, family):
        """The carried exceptions whose write did not end as previewed (ANYM-614), each with a read-only look at its
        day; nothing is retried or undone on its own."""
        rows = {r["ordinal"]: r for r in self.store.query(
            "SELECT ordinal,state,error FROM operation_steps WHERE operation_id=%s", (operation_id,))}
        zone, calendar, found = family["zone"], steps[0]["calendar_ref"], []
        for s in steps:
            series = s.get("series") or {}
            if series.get("role") != "exception" or rows[s["ordinal"]]["state"] not in ("conflict", "failed"):
                continue
            at = series["exception"]["original_start"]
            start = (datetime.combine(date.fromisoformat(at), datetime.min.time(), zone) if "T" not in at
                     else instant(at).astimezone(zone))
            found.append({"original_start": start.isoformat() if "T" in at else at,
                          "fate": series["exception"]["fate"], "error": rows[s["ordinal"]]["error"],
                          "inspect": {"tool": "calendar_agenda", "arguments": {
                              "calendar_refs": [calendar], "start": (start - timedelta(days=1)).isoformat(),
                              "end": (start + timedelta(days=2)).isoformat()}}})
        return {"partial": True, "mismatches": found, "next": found[0]["inspect"] if found else None,
                "next_action": "inspect each listed occurrence; the series and the other exceptions were written "
                               "as previewed"}

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

    def stated(self, key):
        """The family timezone and now for an answer that fails before scheduling (bad arguments); empty when the
        key or the schema cannot give them, so that answer keeps its own error."""
        try:
            self.ready()
            return self.context(self.family(key, self.service.principal(key)))
        except CalendarError:
            return {}

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
            except Unsupported as refusal:
                raise self.unsupported(p, refusal) from None
            except SeriesUnsupported as refusal:
                raise self.series_unsupported(p, refusal) from None
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
            return self.previewed(key, p, request, family, ask=True)[2], None
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
                step = self.primary(plan["payload"]["steps"])
                # The calendar the preview resolved its name to, by calendar_ref: a rename since then is a changed
                # calendar with a fresh plan, never a new choice.
                again = dict(plan["request"], calendar=step["calendar_ref"]) if "calendar" in plan["request"] else (
                    plan["request"])
                try:
                    fresh, _, view = self.previewed(key, p, again, family, read=plan["payload"].get("series_read"))
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
