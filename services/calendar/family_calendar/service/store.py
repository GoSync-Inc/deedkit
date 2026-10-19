from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout
from family_calendar.errors import CalendarError


def ref(prefix):
    return prefix + "-" + uuid4().hex


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def instant(value):
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if t.tzinfo is None:
            raise ValueError
        return t.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise CalendarError("invalid_time") from None


class Store:
    def __init__(self, dsn, schema="fcs"):
        if not re.fullmatch("[a-z][a-z0-9_]{0,60}", schema):
            raise CalendarError("invalid_schema")
        self.dsn, self.schema = dsn, schema
        self.pool = ConnectionPool(
            dsn,
            min_size=0,
            max_size=8,
            timeout=15,
            open=True,
            kwargs={"row_factory": dict_row, "connect_timeout": 10},
        )

    def close(self):
        self.pool.close()

    def lock_key(self, kind, identity):
        return self.schema + ":" + kind + ":" + identity

    def _connect(self):
        try:
            return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=10)
        except psycopg.Error:
            raise CalendarError("database_unavailable") from None

    @contextmanager
    def transaction(self):
        try:
            with self.pool.connection() as conn:
                conn.execute(
                    sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.schema))
                )
                yield conn
        except (PoolTimeout, psycopg.OperationalError):
            raise CalendarError("database_unavailable") from None

    def migrate(self):
        body = Path(__file__).with_name("schema.sql").read_text()
        checksum = hashlib.sha256(body.encode()).hexdigest()
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(61729128)")
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.schema)))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS migrations(version int PRIMARY KEY, checksum text NOT NULL)"
            )
            row = conn.execute("SELECT checksum FROM migrations WHERE version=1").fetchone()
            if row and row["checksum"] != checksum:
                raise CalendarError("migration_checksum_mismatch")
            if not row:
                conn.execute(body)
                conn.execute("INSERT INTO migrations VALUES (1,%s)", (checksum,))

    def drop_test_schema(self):
        if not self.schema.startswith("test_"):
            raise CalendarError("test_schema_required")
        with self._connect() as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def query(self, statement, args=()):
        with self.transaction() as conn:
            cur = conn.execute(statement, args)
            return cur.fetchall() if cur.description else []

    def one(self, statement, args=()):
        rows = self.query(statement, args)
        return rows[0] if rows else None

    def person(self, person_id, label):
        self.query(
            "INSERT INTO people VALUES (%s,%s) ON CONFLICT(id) DO UPDATE SET label=excluded.label",
            (person_id, label),
        )

    def group(self, group_id, label):
        self.query(
            "INSERT INTO groups(id,label) VALUES (%s,%s) ON CONFLICT(id) DO NOTHING", (group_id, label)
        )

    def member(self, group_id, person_id, role):
        self.query(
            "INSERT INTO memberships(group_id,person_id,role) VALUES (%s,%s,%s) "
            "ON CONFLICT(group_id,person_id) DO UPDATE SET role=excluded.role,version=memberships.version+1",
            (group_id, person_id, role),
        )

    def connection(self, connection_id, owner_id, provider):
        with self.transaction() as c:
            old = c.execute("SELECT * FROM connections WHERE id=%s FOR UPDATE", (connection_id,)).fetchone()
            if old and (old["owner_id"] != owner_id or old["provider"] != provider):
                raise CalendarError("connection_identity_changed")
            c.execute(
                "INSERT INTO connections(id,owner_id,provider) VALUES (%s,%s,%s) ON CONFLICT(id) DO NOTHING",
                (connection_id, owner_id, provider),
            )

    def calendar(self, calendar_ref, connection_id, target, label, zone, privilege):
        # Re-discovery cannot silently transfer an existing ref or revive write permission.
        with self.transaction() as c:
            old = c.execute("SELECT * FROM calendars WHERE ref=%s FOR UPDATE", (calendar_ref,)).fetchone()
            if old and (old["connection_id"] != connection_id or old["target"] != target):
                raise CalendarError("calendar_identity_changed")
            c.execute(
                "INSERT INTO calendars(ref,connection_id,target,label,timezone,privilege) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(ref) DO UPDATE SET label=excluded.label, "
                "timezone=excluded.timezone,privilege=excluded.privilege",
                (calendar_ref, connection_id, target, label, zone, privilege),
            )

    def write_opt_in(self, calendar_ref, enabled):
        self.query(
            "UPDATE calendars SET write_enabled=%s,version=version+1 WHERE ref=%s", (enabled, calendar_ref)
        )

    def grant(self, group_id, person_id, calendar_ref, scopes, visibility="details"):
        self.query(
            "INSERT INTO calendar_grants(group_id,person_id,calendar_ref,scopes,visibility) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT(group_id,person_id,calendar_ref) DO UPDATE SET scopes=excluded.scopes, "
            "visibility=excluded.visibility,version=calendar_grants.version+1",
            (group_id, person_id, calendar_ref, Jsonb(scopes), visibility),
        )

    def binding(self, group_id, person_id, calendar_ref):
        self.query(
            "INSERT INTO bindings VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (group_id, person_id, calendar_ref),
        )

    def agent(self, principal_id, group_id, person_id, scopes, calendars, visibility="details"):
        token = secrets.token_urlsafe(48)
        with self.transaction() as c:
            c.execute(
                "INSERT INTO principals(id,group_id,person_id,scopes,key_hash) VALUES (%s,%s,%s,%s,%s)",
                (
                    principal_id,
                    group_id,
                    person_id,
                    Jsonb(scopes),
                    hashlib.sha256(token.encode()).hexdigest(),
                ),
            )
            for calendar in calendars:
                c.execute(
                    "INSERT INTO agent_calendars VALUES (%s,%s,%s)", (principal_id, calendar, visibility)
                )
        return token

    def revoke(self, principal_id):
        self.query("UPDATE principals SET enabled=false,version=version+1 WHERE id=%s", (principal_id,))

    def extend_agent(self, principal_id, days):
        # Renew in place within the issued 90-day lifetime: same key, scopes, calendars and version.
        if not re.fullmatch("[0-9]{1,2}", days) or not 1 <= int(days) <= 90:
            raise CalendarError("invalid_agent_lifetime")
        with self.transaction() as c:
            # now() is the transaction's instant, the same clock authentication compares against.
            p = c.execute(
                "SELECT enabled,expires_at,expires_at>now() AS live,now()+make_interval(days=>%s) AS new "
                "FROM principals WHERE id=%s FOR UPDATE",
                (int(days), principal_id),
            ).fetchone()
            if not p or not p["enabled"]:
                raise CalendarError("not_found_or_forbidden")
            if not p["live"]:
                raise CalendarError("agent_expired")
            if p["new"] < p["expires_at"]:
                raise CalendarError("agent_lifetime_would_shorten")
            c.execute("UPDATE principals SET expires_at=%s WHERE id=%s", (p["new"], principal_id))
            c.execute(
                "INSERT INTO audit(principal_id,action,outcome) VALUES (%s,'extend_agent','extended')",
                (principal_id,),
            )
        return p["expires_at"], p["new"]

    def publish_window(self, calendar_ref, start, end, resources, status, error=None):
        try:
            self._publish_window(calendar_ref, start, end, resources, status, error)
        except (psycopg.DataError, psycopg.IntegrityError):
            # Provider data the projection cannot hold is not an unavailable provider.
            raise CalendarError("invalid_provider_data") from None

    def _publish_window(self, calendar_ref, start, end, resources, status, error):
        # Replace only this bounded projection, not durable assignments, plans or operation history.
        with self.transaction() as c:
            c.execute(
                "DELETE FROM occurrences WHERE calendar_ref=%s AND start_at<%s AND end_at>%s",
                (calendar_ref, instant(end), instant(start)),
            )
            for resource in resources:
                c.execute(
                    "INSERT INTO resources VALUES (%s,%s,%s,%s,%s) ON CONFLICT(ref) DO UPDATE SET "
                    "etag=excluded.etag,payload=excluded.payload",
                    (
                        resource["resource_ref"],
                        calendar_ref,
                        resource["target"],
                        resource.get("etag"),
                        Jsonb(resource["payload"]),
                    ),
                )
                for event in resource["occurrences"]:
                    a, b = instant(event["start"]), instant(event["end"])
                    if b < a:
                        raise CalendarError("invalid_provider_event_time")
                    c.execute(
                        "INSERT INTO occurrences VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(ref) DO UPDATE SET "
                        "start_at=excluded.start_at,end_at=excluded.end_at,details=excluded.details,busy=excluded.busy",
                        (
                            event["ref"],
                            resource["resource_ref"],
                            calendar_ref,
                            a,
                            # A zero-length instant keeps the smallest span CHECK(end_at>start_at) allows.
                            max(b, a + timedelta(microseconds=1)),
                            Jsonb(event["details"]),
                            event["busy"],
                        ),
                    )
            c.execute(
                "INSERT INTO coverage(calendar_ref,start_at,end_at,status,error) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT(calendar_ref) DO UPDATE SET start_at=excluded.start_at,end_at=excluded.end_at, "
                "status=excluded.status,checked_at=now(),error=excluded.error",
                (calendar_ref, instant(start), instant(end), status, error),
            )
