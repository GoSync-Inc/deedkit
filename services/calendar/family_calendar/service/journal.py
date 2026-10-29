"""The calendar change journal (ANYM-565): each complete window read is compared with the stored state of every
event_ref it holds, and the difference is journaled in the transaction that replaces the window. GET /v1/events
serves the journal in the ANYM-562 cursor contract (SUBSCRIPTIONS.md, Module events).

A key names what a provider marked cancelled: ("resource", resource_ref) a whole event or resource,
("series", series_ref) every occurrence of a series, ("instance", series_ref, original_start) one occurrence.
"""

from datetime import date, datetime, timedelta, timezone
import json

from psycopg.types.json import Jsonb

from family_calendar.errors import CalendarError
from .store import instant

# A cancelled event_ref stays readable from its last stored state this long (criterion 5), then the worker deletes it.
RETENTION = timedelta(days=30)
# The fields a diff names (criterion 2), in this order; the etag is never compared.
FIELDS = ("start", "end", "details")
# Steps that may have reached the provider: their principal is the origin of the change the next read sees.
SENT = ("sending", "verified", "unknown", "needs_attention")
SOURCE = "deedkit:calendar"
MAX_SEQ = 2**53 - 1
EARLIEST = datetime(1970, 1, 1, tzinfo=timezone.utc)
LATEST = datetime(9999, 12, 30, tzinfo=timezone.utc)
MAX_LIMIT = 200
SCAN_ROWS = 200
PAGE_BYTES = 240 * 1024


def original_key(value):
    """An occurrence's original start as one comparable text: a timestamp in UTC, a calendar date as it is."""
    if not isinstance(value, str):
        return None
    try:
        return instant(value).isoformat() if "T" in value else date.fromisoformat(value).isoformat()
    except (CalendarError, ValueError):
        return value


def ready(c):
    """The journal's tables exist and this role may write them; false before migration 8 and its db-roles grant."""
    return c.execute(
        "SELECT coalesce(has_table_privilege(to_regclass('change_journal'),'INSERT') "
        "AND has_table_privilege(to_regclass('event_states'),'UPDATE'),false) AS ok"
    ).fetchone()["ok"]


def observed(resources):
    """{event_ref: state} of the occurrences a read returned, stored as `occurrences` stores them."""
    rows = {}
    for resource in resources:
        for event in resource["occurrences"]:
            a = instant(event["start"])
            details = event["details"]
            rows[event["ref"]] = {
                "event_ref": event["ref"],
                "resource_ref": resource["resource_ref"],
                "master_ref": event.get("master_ref"),
                "series_ref": details.get("series_ref"),
                "original_start": original_key(details.get("original_start")),
                "start_at": a,
                "end_at": max(instant(event["end"]), a + timedelta(microseconds=1)),
                "busy": event["busy"],
                "details": details,
            }
    return rows


def differences(old, new):
    changed = []
    if old["start_at"] != new["start_at"]:
        changed.append("start")
    if old["end_at"] != new["end_at"]:
        changed.append("end")
    if old["details"] != new["details"] or old["busy"] != new["busy"]:
        changed.append("details")
    return changed


def key(k):
    return (k[0], k[1], original_key(k[2])) if k[0] == "instance" else tuple(k)


def deliverable(value):
    """A start_at the ANYM-562 contract accepts (1970-01-01 to 9999-12-30, module_events.py); a change of an event
    outside it is stored in the baseline but not journaled."""
    return EARLIEST <= value <= LATEST


def cancelled_by(state, keys):
    return (("resource", state["resource_ref"]) in keys or ("series", state["series_ref"]) in keys
            or ("instance", state["series_ref"], state["original_start"]) in keys)


def origins(c, refs):
    """{resource_ref: [(time, principal_id)]} of operations that sent a step to one of these resources. The time is
    when the worker last finished the operation (its audit row, after the provider write), so a write queued before
    a read but sent after it counts. An operation a restart interrupted before its audit row counts from its last
    step admission: the worker sets lease_until to now()+2 minutes when it admits a step to send (worker.py), and
    from its enqueue time without one."""
    found = {}
    if refs:
        for row in c.execute(
            "SELECT s.intent->>'resource_ref' AS ref,o.principal_id,coalesce(max(a.at),o.lease_until-interval '2 minutes',o.created_at) AS at "
            "FROM operation_steps s JOIN operations o ON o.id=s.operation_id "
            "LEFT JOIN audit a ON a.action='operation' AND a.ref=o.id "
            "WHERE s.intent->>'resource_ref'=ANY(%s) AND s.state=ANY(%s) GROUP BY 1,o.id",
            (sorted(refs), list(SENT)),
        ):
            found.setdefault(row["ref"], []).append((row["at"], row["principal_id"]))
    return found


def origin(writes, state, since):
    """`deedkit:<principal>` when a DeedKit operation wrote this event's resource (or its series' master) after the
    state the change is measured from, else `provider`."""
    latest = max((w for ref in (state["resource_ref"], state.get("master_ref")) for w in writes.get(ref, ())
                  if w[0] > since), default=None)
    return "deedkit:" + latest[1] if latest else "provider"


def save(c, state, now, version, cancelled=False):
    c.execute(
        "INSERT INTO event_states(event_ref,calendar_ref,resource_ref,master_ref,series_ref,original_start,start_at,"
        "end_at,busy,details,version,seen_at,cancelled_at) VALUES (%(event_ref)s,%(calendar_ref)s,%(resource_ref)s,"
        "%(master_ref)s,%(series_ref)s,%(original_start)s,%(start_at)s,%(end_at)s,%(busy)s,%(details)s,%(version)s,%(now)s,%(cancelled)s) "
        "ON CONFLICT(event_ref) DO UPDATE SET resource_ref=excluded.resource_ref,master_ref=excluded.master_ref,series_ref=excluded.series_ref,"
        "original_start=excluded.original_start,start_at=excluded.start_at,end_at=excluded.end_at,busy=excluded.busy,"
        "details=excluded.details,version=excluded.version,seen_at=excluded.seen_at,cancelled_at=excluded.cancelled_at",
        dict(state, details=Jsonb(state["details"]), version=version, now=now, cancelled=now if cancelled else None),
    )


def write(c, entries):
    """Appends entries under journal_head's row lock: seq stays gapless and commits in order."""
    if not entries:
        return
    head = c.execute(
        "UPDATE journal_head SET head=head+%s WHERE id=1 RETURNING head", (len(entries),)
    ).fetchone()["head"]
    for seq, entry in enumerate(entries, head - len(entries) + 1):
        c.execute(
            "INSERT INTO change_journal(seq,calendar_ref,type,event_ref,series_ref,start_at,version,changed,"
            "occurrences,origin) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (seq, entry["calendar_ref"], entry["type"], entry["event_ref"], entry["series_ref"], entry["start_at"],
             entry["version"], Jsonb(entry["changed"]) if entry["changed"] else None, entry["occurrences"],
             entry["origin"]),
        )


def record(c, calendar_ref, start, end, resources, changes=None):
    """Journals what a complete read of [start, end) changed and stores the new baseline; returns the entries.

    changes: {"cancelled": [key, ...]} the provider's explicit cancellations outside the read (Google sync, a CalDAV
    resource read back as gone), "sync": the calendar's next Google sync state {token, page, since}, and "probed":
    the resource_refs the CalDAV probes read.
    The first read of a calendar seeds the baseline and journals nothing: `created` means first seen after it.
    """
    changes = changes or {}
    a, b = instant(start), instant(end)
    new = observed(resources)
    keys = {key(k) for r in resources for k in r.get("cancelled", ())} | {key(k) for k in changes.get("cancelled", ())}
    # Locks in one order everywhere: the calendar's row, then the journal head (in write()).
    c.execute("INSERT INTO journal_head(id) VALUES (1) ON CONFLICT(id) DO NOTHING")
    seeding = c.execute(
        "INSERT INTO journal_calendars(calendar_ref) VALUES (%s) ON CONFLICT(calendar_ref) DO NOTHING "
        "RETURNING calendar_ref", (calendar_ref,),
    ).fetchone() is not None
    calendar = c.execute(
        "SELECT seeded_at,clock_timestamp() AS now FROM journal_calendars WHERE calendar_ref=%s FOR UPDATE",
        (calendar_ref,),
    ).fetchone()
    now = calendar["now"]
    if "sync" in changes:
        sync = changes["sync"]
        c.execute("UPDATE journal_calendars SET sync_token=%s,sync_page=%s,sync_since=%s WHERE calendar_ref=%s",
                  (sync.get("token"), sync.get("page"), sync.get("since"), calendar_ref))
    c.execute("UPDATE journal_calendars SET refreshed_at=%s WHERE calendar_ref=%s", (now, calendar_ref))
    if changes.get("probed"):
        # Probed resources go to the back of the next refresh's probe order (providers.GONE_PROBES).
        c.execute("UPDATE event_states SET probed_at=%s WHERE calendar_ref=%s AND resource_ref=ANY(%s)",
                  (now, calendar_ref, sorted(changes["probed"])))
    series = sorted({k[1] for k in keys if k[0] in ("series", "instance") and k[1]})
    gone = sorted({k[1] for k in keys if k[0] == "resource"})
    old = {
        s["event_ref"]: s
        for s in c.execute(
            "SELECT * FROM event_states WHERE calendar_ref=%s AND (event_ref=ANY(%s) OR cancelled_at IS NULL AND "
            "(start_at<%s AND end_at>%s OR series_ref=ANY(%s) OR resource_ref=ANY(%s)))",
            (calendar_ref, list(new), b, a, series, gone),
        )
    }
    found = []  # (type, state, version, changed, since)
    for ref, state in new.items():
        state = dict(state, calendar_ref=calendar_ref)
        before = old.get(ref)
        if before is None:
            version, kind, changed = 1, "created", []
        elif before["cancelled_at"] is not None:
            version, kind, changed = before["version"] + 1, "created", []
        else:
            changed = differences(before, state)
            version, kind = before["version"] + bool(changed), "changed" if changed else None
        save(c, state, now, version)
        if kind and not seeding and deliverable(state["start_at"]):
            found.append((kind, state, version, changed, before["seen_at"] if before else calendar["seeded_at"]))
    for ref, before in old.items():
        if ref in new or before["cancelled_at"] is not None or not cancelled_by(before, keys):
            continue
        save(c, dict(before), now, before["version"], cancelled=True)
        # A cancellation proven outside this window leaves no live occurrence anywhere (the snapshot stays).
        c.execute("DELETE FROM occurrences WHERE calendar_ref=%s AND ref=%s", (calendar_ref, ref))
        if not seeding and deliverable(before["start_at"]):
            found.append(("cancelled", before, before["version"], [], before["seen_at"]))
    c.execute("DELETE FROM event_states WHERE calendar_ref=%s AND cancelled_at<%s", (calendar_ref, now - RETENTION))
    writes = origins(c, {s.get(k) for _, s, *_ in found for k in ("resource_ref", "master_ref")} - {None})
    entries, series_entries = [], {}
    for kind, state, version, changed, since in sorted(found, key=lambda f: (f[1]["start_at"], f[1]["event_ref"])):
        entry = {"calendar_ref": calendar_ref, "type": kind, "event_ref": state["event_ref"],
                 "series_ref": state["series_ref"], "start_at": state["start_at"], "version": version,
                 "changed": changed, "occurrences": 1, "origin": origin(writes, state, since)}
        # One `changed` per series (criterion 2): its first changed occurrence, the union of fields, the count.
        group = series_entries.get(state["series_ref"]) if kind == "changed" and state["series_ref"] else None
        if group is None:
            entries.append(entry)
            if kind == "changed" and state["series_ref"]:
                series_entries[state["series_ref"]] = entry
            continue
        group["changed"] = [f for f in FIELDS if f in group["changed"] or f in changed]
        group["occurrences"] += 1
        if group["origin"] != entry["origin"]:
            group["origin"] = "provider"
    write(c, entries)
    return entries


def tracked_since(c, calendar_ref, start):
    """Where a full Google resync starts: the earliest live tracked event of the calendar, or `start`."""
    row = c.execute("SELECT min(start_at) AS t FROM event_states WHERE calendar_ref=%s AND cancelled_at IS NULL",
                    (calendar_ref,)).fetchone()
    return min(row["t"], instant(start)) if row["t"] else instant(start)


def sync_state(c, calendar_ref):
    """The calendar's stored Google sync state: {"token", "page", "since"} (all None before the first sync)."""
    row = c.execute("SELECT sync_token AS token,sync_page AS page,sync_since AS since FROM journal_calendars "
                    "WHERE calendar_ref=%s", (calendar_ref,)).fetchone()
    return dict(row) if row else {"token": None, "page": None, "since": None}


def missing(c, calendar_ref, start, end, read):
    """[(resource_ref, target)] of live stored events overlapping [start, end) whose resource a complete read of
    that window did not return (deleted, or moved out of the window), least recently probed first."""
    return [(r["resource_ref"], r["target"]) for r in c.execute(
        "SELECT s.resource_ref,r.target FROM event_states s JOIN resources r ON r.ref=s.resource_ref "
        "WHERE s.calendar_ref=%s AND s.cancelled_at IS NULL AND s.start_at<%s AND s.end_at>%s "
        "AND NOT s.resource_ref=ANY(%s) GROUP BY s.resource_ref,r.target "
        "ORDER BY max(s.probed_at) NULLS FIRST,s.resource_ref",
        (calendar_ref, instant(end), instant(start), sorted(read)),
    )]


def rfc3339(value):
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S") + (".%06d" % value.microsecond if value.microsecond else "") + "Z"


def envelope(row):
    """One event of the cursor contract: ANYM-562's envelope, its calendar data keys and a top-level scope_ref.
    series_ref, occurrences and origin stay in the journal until the contract accepts them."""
    data = {"calendar_ref": row["calendar_ref"], "event_ref": row["event_ref"], "start_at": rfc3339(row["start_at"])}
    if row["type"] != "cancelled":
        data["version"] = row["version"]
    if row["type"] == "changed":
        data["changed"] = row["changed"]
    return {"specversion": "1.0", "id": "calendar:%d" % row["seq"], "source": SOURCE,
            "type": "calendar.event." + row["type"], "subject": row["event_ref"], "time": rfc3339(row["at"]),
            "data": data, "urgency": "batch", "expires_at": None, "scope_ref": "calendar:" + row["calendar_ref"]}


def head(c):
    row = c.execute("SELECT journal_id::text AS journal_id,head,retention_floor FROM journal_head WHERE id=1").fetchone()
    if row is None:
        raise CalendarError("journal_not_ready")
    return row


def page(c, after, limit, calendars):
    """{events, next_after, has_more, retention_floor, journal_id} after the exclusive cursor `after`, for the
    calendars the reader is bound to; next_after moves past rows of other calendars, a cursor past the head reads
    the head (the router's source reset)."""
    top = head(c)
    last, floor, journal_id = top["head"], top["retention_floor"], top["journal_id"]
    shell = {"next_after": last, "has_more": False, "retention_floor": floor, "journal_id": journal_id}
    if after >= last:
        return dict(shell, events=[])
    rows = c.execute(
        "SELECT * FROM change_journal WHERE seq>%s AND seq<=%s ORDER BY seq LIMIT %s", (after, last, SCAN_ROWS)
    ).fetchall()
    events, position = [], after
    size = len(json.dumps(dict(shell, events=[], next_after=MAX_SEQ), separators=(",", ":")))
    for row in rows:
        if row["calendar_ref"] in calendars:
            if len(events) == limit:
                break
            event = envelope(row)
            grown = size + len(json.dumps(event, separators=(",", ":")).encode()) + 1
            if grown > PAGE_BYTES:
                break
            events.append(event)
            size = grown
        position = row["seq"]
    if len(rows) < SCAN_ROWS and (not rows or position == rows[-1]["seq"]):
        position = last
    return dict(shell, events=events, next_after=position, has_more=position < last)


def cancelled(c, calendar_ref, a, b, event_refs=None):
    """Cancelled events of a calendar whose last stored state overlaps [a, b), cancelled under RETENTION ago; with
    event_refs, only those."""
    return c.execute(
        "SELECT * FROM event_states WHERE calendar_ref=%s AND cancelled_at>now()-%s AND start_at<%s AND end_at>%s "
        "AND (%s::text[] IS NULL OR event_ref=ANY(%s)) ORDER BY start_at,event_ref LIMIT 501",
        (calendar_ref, RETENTION, b, a, event_refs, event_refs),
    ).fetchall()

