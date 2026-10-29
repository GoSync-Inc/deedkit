from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg import sql
from psycopg.errors import LockNotAvailable, QueryCanceled, TransactionTimeout
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout
from family_calendar.errors import CalendarError


HERE = Path(__file__).parent
MIGRATIONS = ((1, HERE / "schema.sql"), (2, HERE / "schema_v2.sql"), (3, HERE / "schema_v3.sql"),
              (4, HERE / "schema_v4.sql"), (5, HERE / "schema_v5.sql"), (6, HERE / "schema_v6.sql"),
              (7, HERE / "schema_v7.sql"))
ROLES = HERE / "roles.sql"
# migrate-expand's session budgets (ANYM-586 K2, as messaging's ANYM-596), read back after connecting.
EXPAND_SETTINGS = {"lock_timeout": "2s", "statement_timeout": "4s", "transaction_timeout": "4s"}
# The database a connection reached: cluster system identifier, database name and OID (ANYM-449).
EXPAND_IDENTITY = (
    "SELECT ARRAY[(SELECT system_identifier::text FROM pg_control_system()),current_database()::text,"
    "(SELECT oid::text FROM pg_database WHERE datname=current_database())] AS identity"
)


def ledger(conn, schema):
    """(max version, {version: checksum}) of the schema's migrations table; (0, {}) without it."""
    table = sql.Identifier(schema, "migrations")
    if conn.execute(sql.SQL("SELECT to_regclass({}) IS NULL AS missing").format(sql.Literal(schema + ".migrations"))
                    ).fetchone()["missing"]:
        return 0, {}
    applied = {r["version"]: r["checksum"] for r in conn.execute(sql.SQL("SELECT version, checksum FROM {}").format(table))}
    return max(applied, default=0), applied


def expand_code(run):
    """`run()` with every failure as a fixed code, never PostgreSQL's message. The connection is closed, never rolled
    back by psycopg's context manager; the server discards the open transaction when the session ends."""
    try:
        return run()
    except CalendarError:
        raise
    except LockNotAvailable:
        raise CalendarError("lock_timeout") from None
    except TransactionTimeout:
        raise CalendarError("transaction_timeout") from None
    except QueryCanceled:
        raise CalendarError("statement_timeout") from None
    except Exception:
        raise CalendarError("failed") from None


# A messaging person ID (ANYM-554): a UUID in its canonical 8-4-4-4-12 form, any case.
PERSON_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def person_uuid(value):
    """The lower-case person UUID of value, or None when value is not one."""
    return value.lower() if isinstance(value, str) and PERSON_ID.fullmatch(value) else None


def caller_key(value):
    # A caller-made key (ANYM-495): printable ASCII without spaces, as authentication reads it.
    if not isinstance(value, str) or not re.fullmatch(r"[!-~]{32,512}", value):
        raise CalendarError("invalid_key")
    return value


def operation_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", value):
        raise CalendarError("invalid_operation_id")
    return value


class OperationTaken(Exception):
    # ANYM-495: the operation ID, principal ID or key is held by a committed principal; the transaction rolls back.
    pass


def agent_operation(c, operation_id):
    # No foreign key (migration 3 takes no lock on principals): an operation without its principal still holds the ID.
    return c.execute(
        "SELECT p.id,p.expires_at,NOT p.enabled AS revoked,p.key_hash,o.operation_request FROM principal_operations o "
        "LEFT JOIN principals p ON p.id=o.principal_id WHERE o.operation_id=%s",
        (operation_id,),
    ).fetchone()


def replay(prior, key_hash, request, operation_id):
    if (prior["operation_request"] != request or prior["key_hash"] is None
            or not secrets.compare_digest(prior["key_hash"], key_hash)):
        raise CalendarError("operation_conflict")
    return {"principal_id": prior["id"], "expires_at": prior["expires_at"].isoformat(), "revoked": prior["revoked"],
            "operation_id": operation_id, "replayed": True}


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


def consequence(provider, visibility, copy_location):
    """Who sees a projected drive in the guardian's target calendar, by provider and visibility."""
    seen = "the title, time and place" if copy_location else "the title and time"
    if provider == "icloud":
        return "iCloud does not enforce private events: everyone who can see this calendar sees " + seen
    if visibility == "private":
        return "viewers of this calendar without edit rights see only busy"
    return "everyone who can see event details in this calendar sees " + seen


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
            # No server-side prepared statements: `service migrate` runs beside live services, and a cached
            # SELECT * plan fails with "cached plan must not change result type" once a migration adds a column.
            kwargs={"row_factory": dict_row, "prepare_threshold": None, "connect_timeout": 10},
        )
        self._local = threading.local()

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
        bound = getattr(self._local, "conn", None)
        if bound is not None:
            yield bound
            return
        try:
            with self.pool.connection() as conn:
                conn.execute(
                    sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.schema))
                )
                yield conn
        except (PoolTimeout, psycopg.OperationalError):
            raise CalendarError("database_unavailable") from None

    @contextmanager
    def bound(self):
        """One transaction that every store call of this thread joins until it ends (ANYM-552): a caller that holds
        a pooled connection never waits for a second one, so concurrent callers cannot drain the pool of 8."""
        outer = getattr(self._local, "conn", None)
        with self.transaction() as conn:
            self._local.conn = conn
            try:
                yield conn
            finally:
                self._local.conn = outer

    def migrate(self, through=None):
        # One transaction for all pending versions; an applied file is never edited, only a new one added.
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(61729128)")
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.schema)))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS migrations(version int PRIMARY KEY, checksum text NOT NULL)"
            )
            for version, path in MIGRATIONS:
                if through is not None and version > through:
                    break
                body = path.read_text()
                checksum = hashlib.sha256(body.encode()).hexdigest()
                row = conn.execute("SELECT checksum FROM migrations WHERE version=%s", (version,)).fetchone()
                if row and row["checksum"] != checksum:
                    raise CalendarError("migration_checksum_mismatch")
                if not row:
                    conn.execute(body)
                    conn.execute("INSERT INTO migrations VALUES (%s,%s)", (version, checksum))

    def expand_connection(self):
        """The only connection of migrate-expand (ANYM-586 K2): its budgets and search_path set at connect and read
        back before anything runs. PGAPPNAME (deploy's container name) overrides an application name the DSN names,
        so deploy finds this session by it (ANYM-586 K3)."""
        name = os.environ.get("PGAPPNAME") or None
        expected = dict(EXPAND_SETTINGS, search_path=self.schema + ",public", **({"application_name": name} if name else {}))
        try:
            conn = psycopg.connect(
                self.dsn, row_factory=dict_row, prepare_threshold=None, connect_timeout=5,
                options="-c lock_timeout=2000 -c statement_timeout=4000 -c transaction_timeout=4000 "
                        "-c search_path=" + self.schema + ",public -c client_min_messages=error -c timezone=UTC",
                **({"application_name": name} if name else {}),
            )
        except psycopg.Error:
            raise CalendarError("database_unavailable") from None
        try:
            if any(conn.execute("SHOW " + name).fetchone()[name] != value for name, value in expected.items()):
                raise CalendarError("settings_refused")
        except BaseException:
            conn.close()
            raise
        return conn

    def expand_status(self):
        def run():
            conn = self.expand_connection()
            try:
                return {"schema_version": ledger(conn, self.schema)[0]}
            finally:
                conn.close()
        return expand_code(run)

    def migrate_expand(self, plan, migrations=MIGRATIONS, roles=ROLES, before_commit=None):
        """Applies only DROP-free expand migrations and appended roles.sql grants in one bounded transaction on one
        connection, or nothing (ANYM-586 K2, the calendar copy of messaging's ANYM-596 entry point). The plan is
        `migration_rollout.plan`'s: {base, target, roles: {bytes, sha256}} and an optional `identity` (EXPAND_IDENTITY
        of the database deploy copied). Raises CalendarError with a fixed code; `before_commit(conn)` is a test hook."""
        from . import migration_rollout as rollout

        files = {version: Path(path).read_bytes() for version, path in migrations}
        data = Path(roles).read_bytes()
        try:
            base, target, prefix, identity = plan["base"], plan["target"], plan["roles"], plan.get("identity")
            valid = (
                (identity is None or type(identity) is list and len(identity) == 3
                 and all(type(i) is str for i in identity))
                and all(type(v) is int for v in (base, target)) and 1 <= base <= target == max(files)
                and sorted(files) == list(range(1, len(files) + 1)) and type(prefix) is dict
                and type(prefix.get("bytes")) is int and 0 <= prefix["bytes"] <= len(data)
                and type(prefix.get("sha256")) is str
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise CalendarError("plan_invalid") from None
        if not valid:
            raise CalendarError("plan_invalid")
        if hashlib.sha256(data[:prefix["bytes"]]).hexdigest() != prefix["sha256"]:
            raise CalendarError("prefix_mismatch")
        try:
            old, new = data[:prefix["bytes"]].decode(), data.decode()
            pending = [(version, files[version].decode()) for version in range(base + 1, target + 1)]
        except UnicodeDecodeError:
            raise CalendarError("prefix_mismatch") from None

        def run():
            conn = self.expand_connection()
            try:
                if identity is not None and conn.execute(EXPAND_IDENTITY).fetchone()["identity"] != identity:
                    raise CalendarError("identity_mismatch")
                if not conn.execute("SELECT pg_try_advisory_xact_lock(61729128) AS locked").fetchone()["locked"]:
                    raise CalendarError("busy")
                version, applied = ledger(conn, self.schema)
                if version != base or applied != {
                        v: hashlib.sha256(body).hexdigest() for v, body in files.items() if v <= base}:
                    raise CalendarError("ledger_mismatch")
                # A GRANT target is an ordinary or partitioned table of the schema; a new table may not reuse the
                # name of any relation of the schema or pg_catalog, or IF NOT EXISTS would skip it.
                relations = conn.execute(
                    "SELECT n.nspname, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname IN (%s, 'pg_catalog')", (self.schema,)
                ).fetchall()
                taken = {r["relname"] for r in relations}
                inventory = {r["relname"] for r in relations if r["nspname"] == self.schema
                             and r["relkind"] in ("r", "p")} - {
                    r["relname"] for r in relations if r["nspname"] == "pg_catalog"}
                created = {}
                for _, text in pending:
                    if rollout.unattended_class(text, inventory | set(created), self.schema) != "expand":
                        raise CalendarError("not_unattended")
                    following = rollout.new_tables(text, created, self.schema)
                    if (set(following) - set(created)) & taken:
                        raise CalendarError("not_unattended")
                    created = following
                if rollout.role_class(old, new, created, tables=inventory, schema=self.schema) != "additive":
                    raise CalendarError("role_not_additive")
                for version, text in pending:
                    conn.execute(text)
                    conn.execute("INSERT INTO migrations VALUES (%s,%s)", (version, hashlib.sha256(text.encode()).hexdigest()))
                # Each claimed new table must now be an ordinary or partitioned table of the schema.
                if created and conn.execute(
                    "SELECT count(*) AS n FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=%s AND c.relkind IN ('r','p') AND c.relname=ANY(%s)", (self.schema, sorted(created))
                ).fetchone()["n"] != len(created):
                    raise CalendarError("not_unattended")
                if rollout.statements(new[len(old):], self.schema):
                    conn.execute(new[len(old):])
                if before_commit:
                    before_commit(conn)
                conn.commit()
            finally:
                conn.close()
            return {"schema_version": target}
        return expand_code(run)

    def guardian_readiness(self):
        """Why guardian projections are off, or None; reads only the catalog and migrations, never the new tables."""
        row = self.one(
            "SELECT EXISTS(SELECT 1 FROM migrations WHERE version=2) AS schema_ready, "
            "coalesce(has_table_privilege(to_regclass('guardian_rules'),'SELECT') "
            "AND has_table_privilege(to_regclass('projection_links'),'SELECT'),false) AS grants_ready"
        )
        return None if row["schema_ready"] and row["grants_ready"] else (
            "schema_not_ready" if not row["schema_ready"] else "grants_not_ready"
        )

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

    def family_settings(self, group_id, zone=None, default_calendar_ref=None):
        """A family's timezone and default calendar (ANYM-552): an omitted value stays, "" clears it."""
        if zone:
            try:
                ZoneInfo(zone)
            except (ZoneInfoNotFoundError, ValueError):
                raise CalendarError("invalid_timezone") from None
        if default_calendar_ref and not self.one("SELECT 1 FROM calendars WHERE ref=%s", (default_calendar_ref,)):
            raise CalendarError("not_found_or_forbidden")
        row = self.one(
            "UPDATE groups SET timezone=CASE WHEN %(z)s::text IS NULL THEN timezone ELSE nullif(%(z)s,'') END,"
            "default_calendar_ref=CASE WHEN %(d)s::text IS NULL THEN default_calendar_ref "
            "ELSE nullif(%(d)s,'') END,version=version+1 WHERE id=%(g)s RETURNING timezone,default_calendar_ref",
            {"z": zone, "d": default_calendar_ref, "g": group_id},
        )
        if not row:
            raise CalendarError("not_found_or_forbidden")
        return {"group_id": group_id, **row}

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

    def invitations_opt_in(self, calendar_ref, enabled):
        """The owner's invitation opt-in (ANYM-555); the version bump makes every open preview of it stale."""
        with self.transaction() as c:
            c.execute(
                "INSERT INTO invitation_opt_ins(calendar_ref,enabled) VALUES (%s,%s) ON CONFLICT(calendar_ref) "
                "DO UPDATE SET enabled=excluded.enabled,changed_at=now()", (calendar_ref, enabled),
            )
            c.execute("UPDATE calendars SET version=version+1 WHERE ref=%s", (calendar_ref,))

    def member_person(self, group_id, member_id, person_id):
        """The owner's link of a calendar member to a messaging person UUID (ANYM-554); person_id None unlinks. Only
        the UUID is kept. Returns the link with the person it replaced, so a relink or unlink can be undone."""
        if person_id is not None:
            person_id = person_uuid(person_id)
            if person_id is None:
                raise CalendarError("invalid_person_id")
        with self.transaction() as c:
            if not c.execute("SELECT 1 FROM memberships WHERE group_id=%s AND person_id=%s FOR UPDATE",
                             (group_id, member_id)).fetchone():
                raise CalendarError("not_found")
            old = c.execute("SELECT person_uuid::text AS p FROM member_people WHERE group_id=%s AND member_id=%s",
                            (group_id, member_id)).fetchone()
            if person_id is None:
                c.execute("DELETE FROM member_people WHERE group_id=%s AND member_id=%s", (group_id, member_id))
            else:
                if c.execute("SELECT 1 FROM member_people WHERE group_id=%s AND person_uuid=%s AND member_id<>%s",
                             (group_id, person_id, member_id)).fetchone():
                    raise CalendarError("person_already_linked")
                try:
                    c.execute("INSERT INTO member_people(group_id,member_id,person_uuid) VALUES (%s,%s,%s) "
                              "ON CONFLICT(group_id,member_id) DO UPDATE SET person_uuid=excluded.person_uuid,"
                              "linked_at=now()", (group_id, member_id, person_id))
                except psycopg.errors.UniqueViolation as e:
                    # A concurrent link of the person to another member committed after the check above.
                    if e.diag.constraint_name == "member_people_group_id_person_uuid_key":
                        raise CalendarError("person_already_linked") from None
                    raise
        return {"group_id": group_id, "member_id": member_id, "person_id": person_id,
                "linked": person_id is not None, "previous": old["p"] if old else None}

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

    def agent(self, principal_id, group_id, person_id, scopes, calendars, visibility="details", *, days=90,
              key=None, operation_id=None, check=False):
        """A new agent key; returns the key. With the caller's key and an operation ID (ANYM-495) it stores
        only the hash and returns public fields, and the same request again returns the same record."""
        if type(days) is not int or not 1 <= days <= 183:
            raise CalendarError("invalid_agent_lifetime")
        if len(set(calendars)) != len(calendars):
            raise CalendarError("duplicate_calendar")
        once = operation_id is not None
        if once != (key is not None):
            raise CalendarError("key_and_operation_id_required")
        if once:
            key, operation_id = caller_key(key), operation_name(operation_id)
            request = hashlib.sha256(
                json.dumps([principal_id, group_id, person_id, sorted(scopes), sorted(calendars), visibility, days])
                .encode()
            ).hexdigest()
        else:
            key, request = secrets.token_urlsafe(48), None
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        try:
            with self.transaction() as c:
                if once:
                    if not c.execute("SELECT 1 FROM migrations WHERE version=3").fetchone():
                        raise CalendarError("schema_not_ready")
                    prior = agent_operation(c, operation_id)
                    if prior:
                        return replay(prior, key_hash, request, operation_id)
                if check:
                    # ANYM-495 check mode: what the inserts below would refuse, read only.
                    if not c.execute(
                        "SELECT 1 FROM memberships WHERE group_id=%s AND person_id=%s", (group_id, person_id)
                    ).fetchone():
                        raise CalendarError("unknown_member")
                    known = c.execute("SELECT ref FROM calendars WHERE ref=ANY(%s)", (list(calendars),)).fetchall()
                    if {r["ref"] for r in known} != set(calendars):
                        raise CalendarError("unknown_calendar")
                    if c.execute(
                        "SELECT 1 FROM principals WHERE id=%s OR key_hash=%s", (principal_id, key_hash)
                    ).fetchone():
                        raise CalendarError("principal_or_key_in_use")
                    return {"check": "ok"}
                # A taken principal ID, key or operation ID is no error statement, so no key hash reaches the
                # database log; the commit that holds it is read back below.
                row = c.execute(
                    "INSERT INTO principals(id,group_id,person_id,scopes,key_hash,expires_at) "
                    "VALUES (%s,%s,%s,%s,%s,now()+make_interval(days=>%s))"
                    + (" ON CONFLICT DO NOTHING" if once else "") + " RETURNING id,expires_at",
                    (principal_id, group_id, person_id, Jsonb(scopes), key_hash, days),
                ).fetchone()
                if row is None or once and not c.execute(
                    "INSERT INTO principal_operations(operation_id,operation_request,principal_id) "
                    "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING RETURNING operation_id",
                    (operation_id, request, principal_id),
                ).fetchone():
                    raise OperationTaken
                for calendar in calendars:
                    c.execute(
                        "INSERT INTO agent_calendars VALUES (%s,%s,%s)", (principal_id, calendar, visibility)
                    )
        except OperationTaken:
            # A concurrent call won the operation ID, the principal ID or the key; its commit is visible now.
            with self.transaction() as c:
                prior = agent_operation(c, operation_id)
            if prior is None:
                raise CalendarError("principal_or_key_in_use") from None
            return replay(prior, key_hash, request, operation_id)
        if not once:
            return key
        return {"principal_id": row["id"], "expires_at": row["expires_at"].isoformat(), "revoked": False,
                "operation_id": operation_id, "replayed": False}

    def agent_operation_info(self, operation_id):
        # ANYM-495: the agent an operation issued, public fields only.
        operation_id = operation_name(operation_id)
        if self.check_schema()["check"] != "ok":
            raise CalendarError("schema_not_ready")
        row = self.one(
            "SELECT p.id AS principal_id,p.expires_at,NOT p.enabled AS revoked,o.operation_id "
            "FROM principal_operations o JOIN principals p ON p.id=o.principal_id WHERE o.operation_id=%s",
            (operation_id,),
        )
        if row is None:
            return [{"found": False, "operation_id": operation_id}]
        return [dict(row, found=True, expires_at=row["expires_at"].isoformat())]

    def check_schema(self, version=3):
        # ANYM-495 check mode: reads only the migrations table, writes nothing.
        current = self.one("SELECT coalesce(max(version),0) AS v FROM migrations")["v"]
        return {"check": "ok" if current >= version else "schema_not_ready", "schema_version": current,
                "required": version}

    def revoke(self, principal_id):
        self.query("UPDATE principals SET enabled=false,version=version+1 WHERE id=%s", (principal_id,))

    def extend_agent(self, principal_id, days):
        # Renew in place for up to 365 days from now (owner 2026-10-24, ANYM-499): same key, scopes, calendars and version.
        if not re.fullmatch("[0-9]{1,3}", days) or not 1 <= int(days) <= 365:
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

    def list_agents(self):
        # ANYM-494: every agent principal with public fields only; revoked means disabled.
        with self.transaction() as conn:
            conn.execute("SET LOCAL statement_timeout = '5s'")  # an admin read never outlives its CLI
            rows = conn.execute(
                "SELECT p.id AS principal_id,p.group_id,p.person_id,p.expires_at,NOT p.enabled AS revoked,"
                "(SELECT count(*) FROM agent_calendars a WHERE a.principal_id=p.id) AS calendars "
                "FROM principals p ORDER BY p.id"
            ).fetchall()
        return [dict(row, expires_at=row["expires_at"].isoformat()) for row in rows]

    def agent_scope(self, principal_id):
        # ANYM-496: an agent's scope, so the admin CLI can clone it; public fields only, never the key hash.
        with self.transaction() as conn:
            conn.execute("SET LOCAL statement_timeout = '5s'")
            row = conn.execute(
                "SELECT id AS principal_id,group_id,person_id,scopes,NOT enabled AS revoked,expires_at "
                "FROM principals WHERE id=%s",
                (principal_id,),
            ).fetchone()
            if row is None:
                return {"found": False, "principal_id": principal_id}
            calendars = conn.execute(
                "SELECT calendar_ref AS ref,visibility FROM agent_calendars WHERE principal_id=%s ORDER BY calendar_ref",
                (principal_id,),
            ).fetchall()
        return dict(row, found=True, expires_at=row["expires_at"].isoformat(), calendars=[dict(c) for c in calendars])

    def agent_info(self, keys):
        # ANYM-494: an agent's public record by its key, in input order; the hash never leaves this method.
        if not isinstance(keys, list) or not 1 <= len(keys) <= 100:
            raise CalendarError("invalid_key_count")
        result = []
        with self.transaction() as conn:
            conn.execute("SET LOCAL statement_timeout = '5s'")
            for key in keys:
                key = key.strip()
                if not 1 <= len(key) <= 512 or any(c.isspace() for c in key):
                    result.append({"found": False, "error": "invalid_key"})
                    continue
                row = conn.execute(
                    "SELECT id AS principal_id,expires_at,NOT enabled AS revoked FROM principals WHERE key_hash=%s",
                    (hashlib.sha256(key.encode()).hexdigest(),),
                ).fetchone()
                result.append(dict(row, found=True, expires_at=row["expires_at"].isoformat()) if row else {"found": False})
        return result

    def guardian_rule_set(self, group_id, dependent, activity, duty, guardian, source, target, lead, title,
                          visibility, copy_location=False, expected_version=None):
        # Owner CLI only (SERVICE.md): a rule is the default drive duty, never a write authorization.
        if not re.fullmatch("[0-9]{1,3}", str(lead)) or not 1 <= int(lead) <= 180:
            raise CalendarError("invalid_lead_time")
        if not 1 <= len(activity) <= 64 or not 1 <= len(title) <= 128 or duty not in ("dropoff", "pickup"):
            raise CalendarError("invalid_rule")
        if expected_version is not None and not re.fullmatch("[0-9]{1,18}", str(expected_version)):
            raise CalendarError("version_conflict")
        if self.guardian_readiness() == "schema_not_ready":
            raise CalendarError("schema_not_ready")
        with self.transaction() as c:
            roles = {
                r["person_id"]: r["role"]
                for r in c.execute(
                    "SELECT person_id,role FROM memberships WHERE group_id=%s AND person_id IN (%s,%s) AND enabled",
                    (group_id, guardian, dependent),
                )
            }
            if roles.get(guardian) not in ("group_admin", "coordinator", "adult") or roles.get(dependent) != "dependent":
                raise CalendarError("invalid_guardian")
            bound = {
                (r["person_id"], r["calendar_ref"])
                for r in c.execute(
                    "SELECT person_id,calendar_ref FROM bindings WHERE group_id=%s", (group_id,)
                )
            }
            if (dependent, source) not in bound or (guardian, target) not in bound:
                raise CalendarError("binding_required")
            provider = c.execute(
                "SELECT cn.provider FROM calendars c JOIN connections cn ON cn.id=c.connection_id WHERE c.ref=%s",
                (target,),
            ).fetchone()["provider"]
            if visibility not in ("private", "details"):
                raise CalendarError(
                    "visibility_required",
                    details={"options": {v: consequence(provider, v, copy_location) for v in ("private", "details")}},
                )
            old = c.execute(
                "SELECT id,version FROM guardian_rules WHERE group_id=%s AND dependent_id=%s AND activity=%s "
                "AND duty=%s FOR UPDATE",
                (group_id, dependent, activity, duty),
            ).fetchone()
            if expected_version is not None and (not old or old["version"] != int(expected_version)):
                raise CalendarError("version_conflict")
            c.execute(
                "INSERT INTO guardians VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (group_id, guardian, dependent)
            )
            values = (guardian, source, target, int(lead), title, visibility, copy_location)
            if old:
                rule = c.execute(
                    "UPDATE guardian_rules SET guardian_id=%s,source_calendar_ref=%s,target_calendar_ref=%s,"
                    "lead_minutes=%s,title=%s,visibility=%s,copy_location=%s,enabled=true,version=version+1,"
                    "updated_at=now() WHERE id=%s RETURNING id,version",
                    (*values, old["id"]),
                ).fetchone()
            else:
                try:
                    rule = c.execute(
                        "INSERT INTO guardian_rules(id,group_id,dependent_id,activity,duty,guardian_id,"
                        "source_calendar_ref,target_calendar_ref,lead_minutes,title,visibility,copy_location) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,version",
                        (ref("rule"), group_id, dependent, activity, duty, *values),
                    ).fetchone()
                except psycopg.errors.UniqueViolation:
                    # A concurrent owner command created the same key first.
                    raise CalendarError("version_conflict") from None
            c.execute(
                "INSERT INTO audit(action,ref,outcome) VALUES ('guardian_rule_set',%s,'saved')", (rule["id"],)
            )
        return {
            "rule_id": rule["id"],
            "version": rule["version"],
            "consequence": consequence(provider, visibility, copy_location),
        }

    def guardian_rule_disable(self, rule_id, expected_version=None):
        if expected_version is not None and not re.fullmatch("[0-9]{1,18}", str(expected_version)):
            raise CalendarError("version_conflict")
        if self.guardian_readiness() == "schema_not_ready":
            raise CalendarError("schema_not_ready")
        with self.transaction() as c:
            old = c.execute("SELECT version FROM guardian_rules WHERE id=%s FOR UPDATE", (rule_id,)).fetchone()
            if not old:
                raise CalendarError("rule_not_found")
            if expected_version is not None and old["version"] != int(expected_version):
                raise CalendarError("version_conflict")
            rule = c.execute(
                "UPDATE guardian_rules SET enabled=false,version=version+1,updated_at=now() WHERE id=%s "
                "RETURNING id,version",
                (rule_id,),
            ).fetchone()
            c.execute(
                "INSERT INTO audit(action,ref,outcome) VALUES ('guardian_rule_disable',%s,'disabled')", (rule_id,)
            )
        return {"rule_id": rule["id"], "version": rule["version"], "enabled": False}

    def guardian_rule_list(self):
        if self.guardian_readiness() == "schema_not_ready":
            raise CalendarError("schema_not_ready")
        # No titles: the list is for checking wiring, not for reading what guardians will see.
        return self.query(
            "SELECT id AS rule_id,group_id,dependent_id,activity,duty,guardian_id,source_calendar_ref,"
            "target_calendar_ref,lead_minutes,visibility,copy_location,enabled,version FROM guardian_rules "
            "ORDER BY group_id,dependent_id,activity,duty"
        )

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
