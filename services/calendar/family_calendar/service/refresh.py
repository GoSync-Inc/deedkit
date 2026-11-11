"""Scheduled refresh of operator-configured calendars and `calendar.event.starting` reminders (ANYM-576).

The operator (owner CLI `service refresh-calendar`) names the calendars to refresh, the window (default now to +14
days), the cadence (default 5 minutes) and the lead times (default one, 15 minutes). Nothing else asks for a refresh:
no messaging or gateway path writes the configuration (roles.sql grants the gateway SELECT only).

Provider quota: a configured calendar is read at most once per its cadence (at least 1 minute), and the worker starts
at most one scheduled refresh per SPACING_S across all calendars, one calendar per tick. A refresh is the read an agent
would ask for: one window read (Google events.list pages, CalDAV one calendar-query REPORT, both bounded by the
service's event limit) plus the change journal's bounded incremental-sync and probe steps (worker.SUPPLEMENT_S each).
The whole refresh runs after the tick's operation and is cut at REFRESH_S, so a queued operation waits at most that
long behind it; a failure records the calendar's error and is never a journal event.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from family_calendar.errors import CalendarError
from . import journal

DEFAULT_WINDOW_DAYS = 14
DEFAULT_CADENCE_MINUTES = 5
DEFAULT_LEADS = (15,)
MAX_WINDOW_DAYS = 60
MAX_LEADS = 4
# At most one scheduled refresh starts per this many seconds across all calendars (the stated provider quota).
SPACING_S = 10
# A scheduled refresh (provider reads and the window's commit) ends within this bound or is cut as provider_timeout.
REFRESH_S = 45
# Provider authorization refused: after AUTH_STRIKES in a row the calendar's health is auth_failed.
AUTH_ERRORS = frozenset({"reauth_required", "oauth_identity_mismatch", "credentials_unreadable",
                         "unsafe_token_endpoint", "secret_key_missing"})
AUTH_STRIKES = 2
# Reminders come only from occurrences a successful refresh read within twice the cadence plus FRESH_SLACK.
FRESH_SLACK = timedelta(minutes=1)
REMINDERS_PER_PASS = 100


def ready(c):
    """Migration 9 and the worker's grants are in place; false before them (the worker then never refreshes)."""
    return c.execute(
        "SELECT coalesce(has_table_privilege(to_regclass('refresh_calendars'),'SELECT') "
        "AND has_column_privilege(to_regclass('refresh_calendars'),'health','UPDATE') "
        "AND has_table_privilege(to_regclass('starting_reminders'),'INSERT') "
        "AND has_table_privilege(to_regclass('change_journal'),'INSERT'),false) AS ok"
    ).fetchone()["ok"]


def configure(store, calendar_ref, window_days=None, cadence_minutes=None, leads=None):
    """The owner CLI's configuration of one calendar; omitted values take the defaults. Due at once."""
    window_days = DEFAULT_WINDOW_DAYS if window_days is None else window_days
    cadence_minutes = DEFAULT_CADENCE_MINUTES if cadence_minutes is None else cadence_minutes
    leads = sorted(leads or DEFAULT_LEADS)
    if (not 1 <= window_days <= MAX_WINDOW_DAYS or not 1 <= cadence_minutes <= 1440
            or not 1 <= len(leads) <= MAX_LEADS or len(set(leads)) != len(leads)
            or any(not 1 <= lead <= 1440 for lead in leads)):
        raise CalendarError("invalid_refresh_configuration")
    with store.transaction() as c:
        if not c.execute("SELECT 1 FROM calendars WHERE ref=%s", (calendar_ref,)).fetchone():
            raise CalendarError("calendar_not_found")
        c.execute(
            "INSERT INTO refresh_calendars(calendar_ref,window_days,cadence_minutes,leads) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT(calendar_ref) DO UPDATE SET window_days=excluded.window_days,"
            "cadence_minutes=excluded.cadence_minutes,leads=excluded.leads,next_at=NULL,configured_at=now()",
            (calendar_ref, window_days, cadence_minutes, leads),
        )
    return {"calendar_ref": calendar_ref, "window_days": window_days, "cadence_minutes": cadence_minutes,
            "leads": leads}


def unconfigure(store, calendar_ref):
    store.query("DELETE FROM refresh_calendars WHERE calendar_ref=%s", (calendar_ref,))
    return {"calendar_ref": calendar_ref, "refresh": "off"}


def listing(store):
    """`service refresh-list`: each configured calendar with its schedule and health, times as ISO 8601 text."""
    rows = store.query(
        "SELECT calendar_ref,window_days,cadence_minutes,leads,next_at,last_ok_at,failures,error,health,health_since "
        "FROM refresh_calendars ORDER BY calendar_ref")
    return {"calendars": [{k: v.isoformat() if isinstance(v, datetime) else v for k, v in row.items()}
                          for row in rows]}


def claim(store, now):
    """The calendar most overdue (a new one first), its next refresh set one cadence on, so neither a failure nor a
    crash reads it again sooner. None when nothing is due or before migration 9."""
    with store.transaction() as c:
        if not ready(c):
            return None
        cal = c.execute(
            "SELECT r.calendar_ref,r.window_days,r.cadence_minutes,c.*,cn.provider FROM refresh_calendars r "
            "JOIN calendars c ON c.ref=r.calendar_ref JOIN connections cn ON cn.id=c.connection_id "
            "WHERE cn.enabled AND (r.next_at IS NULL OR r.next_at<=%s) "
            "ORDER BY r.next_at NULLS FIRST,r.calendar_ref LIMIT 1 FOR UPDATE OF r SKIP LOCKED",
            (now,),
        ).fetchone()
        if cal:
            c.execute("UPDATE refresh_calendars SET next_at=%s WHERE calendar_ref=%s",
                      (now + timedelta(minutes=cal["cadence_minutes"]), cal["ref"]))
        return cal


async def read(worker, cal, start, end):
    """The same read as an agent's refresh (Worker.sync_one), under the calendar's lock."""
    store = worker.store
    with store.transaction() as lock:
        if not lock.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS ok", (store.lock_key("calendar", cal["ref"]),)
        ).fetchone()["ok"]:
            raise CalendarError("resource_busy")
        changes = await worker._journal_changes(cal, start)
        resources = await worker.providers.read_window(cal, start, end)
        changes = await worker._gone(cal, start, end, resources, changes)
        store.publish_window(cal["ref"], start, end, resources, "complete", changes=changes)


def outcome(store, cal, now, start, end, error):
    """Record the refresh's result on the calendar; a failure also marks its coverage blocked, as an agent's failed
    refresh does. Never a journal event."""
    if error is None:
        store.query(
            "UPDATE refresh_calendars SET last_ok_at=%s,failures=0,auth_failures=0,error=NULL,health='ok',"
            "health_since=NULL WHERE calendar_ref=%s", (now, cal["ref"]))
        return
    auth = error in AUTH_ERRORS
    store.query(
        "UPDATE refresh_calendars SET failures=failures+1,error=%(error)s,"
        "auth_failures=CASE WHEN %(auth)s THEN auth_failures+1 ELSE auth_failures END,"
        "health=CASE WHEN %(auth)s AND auth_failures+1>=%(strikes)s THEN 'auth_failed' ELSE health END,"
        "health_since=CASE WHEN %(auth)s AND auth_failures+1>=%(strikes)s THEN coalesce(health_since,%(now)s) "
        "ELSE health_since END WHERE calendar_ref=%(ref)s",
        {"error": error, "auth": auth, "strikes": AUTH_STRIKES, "now": now, "ref": cal["ref"]},
    )
    store.query(
        "INSERT INTO coverage(calendar_ref,start_at,end_at,status,error) VALUES (%s,%s,%s,'blocked',%s) "
        "ON CONFLICT(calendar_ref) DO UPDATE SET status='blocked',error=excluded.error",
        (cal["ref"], start, end, error),
    )


async def refresh_one(worker, now=None):
    """One due calendar's scheduled refresh, bounded by REFRESH_S. True when one was attempted."""
    now = now or datetime.now(timezone.utc)
    moment = worker.clock()
    if worker.refreshed_at is not None and moment - worker.refreshed_at < worker.refresh_spacing:
        return False
    cal = claim(worker.store, now)
    if cal is None:
        return False
    worker.refreshed_at = moment
    start, end = now, now + timedelta(days=cal["window_days"])
    try:
        await asyncio.wait_for(read(worker, cal, start, end), REFRESH_S)
        error = None
    except CalendarError as e:
        error = e.code
    except asyncio.TimeoutError:
        error = "provider_timeout"
    except Exception:
        error = "provider_unavailable"
    outcome(worker.store, cal, now, start, end, error)
    return True


def remind(store, now=None):
    """Journal `calendar.event.starting` for each due (occurrence, lead); returns how many. Once per (event_ref,
    start_at, lead) for a future start only, from the occurrences a fresh successful refresh read: a moved occurrence
    has a new start (its old reminder is never due), a cancelled one is gone, an all-day one is skipped."""
    now = now or datetime.now(timezone.utc)
    with store.transaction() as c:
        if not ready(c):
            return 0
        # The worker is the only writer of reminders, so the head's row is taken only when one is due.
        due = c.execute(
            "SELECT o.calendar_ref,o.ref AS event_ref,o.start_at,l.lead FROM refresh_calendars r "
            "JOIN occurrences o ON o.calendar_ref=r.calendar_ref CROSS JOIN LATERAL unnest(r.leads) AS l(lead) "
            "WHERE r.last_ok_at>%(now)s-make_interval(mins=>2*r.cadence_minutes)-%(slack)s "
            "AND o.start_at>%(now)s AND o.start_at<=%(now)s+make_interval(mins=>l.lead) "
            "AND o.start_at<=%(latest)s AND NOT coalesce((o.details->>'all_day')::boolean,false) "
            "AND NOT EXISTS (SELECT 1 FROM starting_reminders s WHERE s.event_ref=o.ref AND s.start_at=o.start_at "
            "AND s.lead_minutes=l.lead) ORDER BY o.start_at,o.ref,l.lead DESC LIMIT %(limit)s",
            {"now": now, "slack": FRESH_SLACK, "latest": journal.LATEST, "limit": REMINDERS_PER_PASS},
        ).fetchall()
        if not due:
            return 0
        # As journal.write: the head's row lock keeps seq gapless and in commit order with change_journal's writers.
        c.execute("INSERT INTO journal_head(id) VALUES (1) ON CONFLICT(id) DO NOTHING")
        head = c.execute(
            "UPDATE journal_head SET head=head+%s WHERE id=1 RETURNING head", (len(due),)
        ).fetchone()["head"]
        for seq, row in enumerate(due, head - len(due) + 1):
            c.execute(
                "INSERT INTO starting_reminders(seq,calendar_ref,event_ref,start_at,lead_minutes,at) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (seq, row["calendar_ref"], row["event_ref"], row["start_at"], row["lead"], now),
            )
        return len(due)
