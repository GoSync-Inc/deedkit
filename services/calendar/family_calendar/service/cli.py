import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import sql
import secrets

from family_calendar.config import Registry
from family_calendar.errors import CalendarError
from family_calendar.private_files import read_private, write_private, private_dir
from family_calendar.providers.google.auth import connect
from .core import EVENTS, READ, WRITE
from .providers import Providers, source_ref
from .store import Store, caller_key, operation_name
from .vault import Vault


KEY_INFO_BYTES = 64 * 1024


def read_key(path):
    # ANYM-495: the caller's key from an owner-only regular file, or stdin; never printed. No symlink, no FIFO
    # wait (nonblocking open, then the type check) and a bounded read.
    if str(path) == "/dev/stdin":
        raw = sys.stdin.buffer.read(4097)
    else:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            raise CalendarError("unsafe_key_file") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise CalendarError("unsafe_key_file")
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
    lines = raw.decode(errors="replace").splitlines() if len(raw) <= 4096 else []
    if len(lines) != 1:
        raise CalendarError("invalid_key")
    return caller_key(lines[0].strip())


def parser():
    p = argparse.ArgumentParser(description="Family Calendar owner CLI / isolated gateway / worker")
    sub = p.add_subparsers(dest="action", required=True)
    for name in ("migrate", "schema-status", "serve", "worker"):
        sub.add_parser(name)
    roles = sub.add_parser("db-roles")
    roles.add_argument("--check", action="store_true", help="read-only: compare the grants with roles_expected.json")
    expand = sub.add_parser("migrate-expand", help="autodeploy's bounded additive migration (ANYM-586)")
    mode = expand.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", type=Path, help="the plan JSON migration_rollout.plan made")
    mode.add_argument("--status", action="store_true", help="read-only: the ledger's schema version")
    init = sub.add_parser("init-family")
    init.add_argument("--group", default="family")
    init.add_argument("--owner", default="egor")
    init.add_argument("--label", default="Егор")
    member = sub.add_parser("member")
    member.add_argument("--id", required=True)
    member.add_argument("--label", required=True)
    member.add_argument("--group", default="family")
    member.add_argument(
        "--role", required=True, choices=["group_admin", "coordinator", "adult", "dependent", "helper"]
    )
    # The owner's link of a calendar member to a messaging person (ANYM-554); --unlink is its undo.
    linked = sub.add_parser("member-person")
    linked.add_argument("--group", default="family")
    linked.add_argument("--member", required=True)
    target = linked.add_mutually_exclusive_group(required=True)
    target.add_argument("--person", help="the messaging person UUID")
    target.add_argument("--unlink", action="store_true")
    imported = sub.add_parser("import-icloud")
    imported.add_argument("--connection", required=True)
    imported.add_argument("--owner", default="egor")
    imported.add_argument(
        "--timezone",
        default="Asia/Makassar",
        help="IANA zone of the imported calendars until discover reads their own",
    )
    google = sub.add_parser("connect-google")
    google.add_argument("--id", required=True)
    google.add_argument("--email", required=True)
    google.add_argument("--client", type=Path, required=True)
    google.add_argument("--owner", default="egor")
    discover = sub.add_parser("discover")
    discover.add_argument("--connection", required=True)
    grant = sub.add_parser("grant")
    grant.add_argument("--group", default="family")
    grant.add_argument("--member", required=True)
    grant.add_argument("--calendar", required=True)
    grant.add_argument("--scopes", default=",".join(READ))
    grant.add_argument("--visibility", choices=["details", "busy_only"], default="details")
    bind = sub.add_parser("bind")
    bind.add_argument("--group", default="family")
    bind.add_argument("--member", required=True)
    bind.add_argument("--calendar", required=True)
    write = sub.add_parser("allow-write")
    write.add_argument("--calendar", required=True)
    reviewed = write.add_mutually_exclusive_group(required=True)
    reviewed.add_argument("--test-calendar", action="store_true")
    reviewed.add_argument(
        "--reviewed-calendar",
        action="store_true",
        help="Owner explicitly selected this calendar for agent writes",
    )
    disable = sub.add_parser("disable-write")
    disable.add_argument("--calendar", required=True)
    # Calendar invitations (ANYM-555): the owner's own opt-in, since Google may email the guests.
    invitations = sub.add_parser("allow-invitations")
    invitations.add_argument("--calendar", required=True)
    no_invitations = sub.add_parser("disable-invitations")
    no_invitations.add_argument("--calendar", required=True)
    # One-call scheduling (ANYM-552): the zone offset-free times are read in and the calendar used by default.
    settings = sub.add_parser("family-settings")
    settings.add_argument("--group", default="family")
    settings.add_argument("--timezone")
    settings.add_argument("--default-calendar")
    agent = sub.add_parser("agent-key")
    agent.add_argument("--id", required=True)
    agent.add_argument("--group", default="family")
    agent.add_argument("--member", required=True)
    agent.add_argument("--calendar", action="append", default=[])
    agent.add_argument("--scopes", default=",".join(READ))
    agent.add_argument("--visibility", choices=["details", "busy_only"], default="details")
    agent.add_argument("--output", type=Path)
    agent.add_argument("--token-file", type=Path, help="An existing owner-only file (or /dev/stdin) with the caller's key")
    agent.add_argument("--operation-id", help="1-128 of [A-Za-z0-9._/-]; the same request again replays")
    agent.add_argument("--days", default="90", help="Lifetime from now, 1-183 days (default 90)")
    revoke = sub.add_parser("revoke-agent")
    revoke.add_argument("--id", required=True)
    extend = sub.add_parser("extend-agent")
    extend.add_argument("--id", required=True)
    extend.add_argument("--days", required=True, help="New lifetime from now, 1-365 days")
    agents = sub.add_parser("list-agents")
    info = sub.add_parser("agent-info")
    info.add_argument("--key-file", type=Path, help="One key per line; /dev/stdin for a batch")
    info.add_argument("--operation-id")
    scope = sub.add_parser("agent-scope")
    scope.add_argument("--id", required=True)
    for command in (agent, revoke, extend, agents, info, scope):
        # ANYM-495: validate the arguments and report whether the schema is applied; writes nothing.
        command.add_argument("--check", action="store_true")
    disconnect = sub.add_parser("disconnect")
    disconnect.add_argument("--connection", required=True)
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--operation", required=True)
    rules = sub.add_parser("guardian-rule").add_subparsers(dest="rule_action", required=True)
    rule = rules.add_parser("set")
    rule.add_argument("--group", default="family")
    rule.add_argument("--dependent", required=True)
    rule.add_argument("--activity", required=True)
    rule.add_argument("--duty", required=True, choices=["dropoff", "pickup"])
    rule.add_argument("--guardian", required=True)
    rule.add_argument("--source-calendar", required=True)
    rule.add_argument("--target-calendar", required=True)
    rule.add_argument("--lead-minutes", required=True, help="Travel time before drop-off or after pick-up, 1-180")
    rule.add_argument("--title", required=True, help="Title of the projected drive; the lesson's own is never copied")
    # No default: the owner chooses who sees the drive (visibility_required prints both consequences).
    rule.add_argument("--visibility", choices=["private", "details"])
    rule.add_argument("--copy-location", action="store_true")
    rule.add_argument("--expected-version")
    disable_rule = rules.add_parser("disable")
    disable_rule.add_argument("--id", required=True)
    disable_rule.add_argument("--expected-version")
    rules.add_parser("list")
    return p


async def discover(store, vault, connection_id):
    connection = store.one("SELECT * FROM connections WHERE id=%s AND enabled", (connection_id,))
    if not connection:
        raise CalendarError("connection_not_found")
    providers = Providers(store, vault)
    try:
        adapter = await providers.adapter(dict(connection, connection_id=connection_id))
        calendars = await adapter.discover()
        out = []
        for c in calendars:
            if connection["provider"] == "icloud":
                calendar_ref = c["ref"]
                target = c["href"]
                label = c["label"]
                # Without a calendar VTIMEZONE keep the zone chosen at import.
                old = store.one("SELECT timezone FROM calendars WHERE ref=%s", (calendar_ref,))
                zone = old["timezone"] if old else "Asia/Makassar"
                privilege = c["privilege"]
                if c.get("timezone_ical"):
                    from icalendar import Calendar

                    parsed = Calendar.from_ical(c["timezone_ical"])
                    zones = parsed.walk("VTIMEZONE")
                    if zones:
                        zone = str(zones[0].get("TZID", "Asia/Makassar"))
            else:
                target = c["id"]
                calendar_ref = source_ref("cal", connection_id, target)
                label = c.get("summary", "Calendar")
                zone = c.get("timeZone", "Asia/Makassar")
                privilege = "write" if c.get("accessRole") in ("writer", "owner") else "read_only"
            store.calendar(calendar_ref, connection_id, target, label, zone, privilege)
            out.append(
                {"calendar_ref": calendar_ref, "label": label, "privilege": privilege, "timezone": zone}
            )
        return {"connection_id": connection_id, "read_verified": True, "calendars": out}
    finally:
        await providers.close()


def db_roles(store, root, config):
    # A private owner-only DB container. No change to shared infrastructure.
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(config["dsn"])
    with store.transaction() as c:
        for role, profile in [("fcs_gateway", "gateway"), ("fcs_worker", "worker")]:
            row = c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
            if not row:
                password = secrets.token_urlsafe(40)
                c.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
                    ).format(sql.Identifier(role), sql.Literal(password))
                )
                dsn = urlunsplit(
                    (
                        parsed.scheme,
                        role + ":" + password + "@" + parsed.netloc.split("@", 1)[1],
                        parsed.path,
                        "",
                        "",
                    )
                )
                write_private(
                    root / (profile + ".json"), json.dumps({"dsn": dsn, "port": config["port"]}).encode()
                )
        c.execute(Path(__file__).with_name("roles.sql").read_text())
    return {"db_roles": ["gateway", "worker"], "provider_secrets_in_gateway": False}


ROLES = ("fcs_gateway", "fcs_worker")
# ANYM-601: one statement, so one snapshot: the schema version, every ACL entry in the schema held by a service role
# or PUBLIC (schema, tables and sequences, columns), every membership of a service role, and which roles exist.
GRANTS = """
WITH n AS (SELECT oid, nspname, nspacl FROM pg_namespace WHERE nspname=%(schema)s),
r AS (SELECT oid, rolname FROM pg_roles WHERE rolname=ANY(%(roles)s)),
acl AS (
 SELECT n.nspname AS object, NULL::text AS col, a.grantee, a.privilege_type, a.is_grantable
  FROM n, aclexplode(n.nspacl) a
 UNION ALL
 SELECT n.nspname || '.' || c.relname, NULL, a.grantee, a.privilege_type, a.is_grantable
  FROM n JOIN pg_class c ON c.relnamespace=n.oid, aclexplode(c.relacl) a
 UNION ALL
 SELECT n.nspname || '.' || c.relname, t.attname::text, a.grantee, a.privilege_type, a.is_grantable
  FROM n JOIN pg_class c ON c.relnamespace=n.oid JOIN pg_attribute t ON t.attrelid=c.oid AND NOT t.attisdropped,
  aclexplode(t.attacl) a)
SELECT 'acl' AS kind, CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE r.rolname END AS role, acl.object, acl.col,
 acl.privilege_type AS privilege, acl.is_grantable AS option
 FROM acl LEFT JOIN r ON r.oid=acl.grantee WHERE acl.grantee=0 OR r.oid IS NOT NULL
UNION ALL
SELECT 'member', r.rolname, g.rolname, NULL,
 concat_ws(', ', CASE WHEN m.admin_option THEN 'admin' END, CASE WHEN m.inherit_option THEN 'inherit' END,
  CASE WHEN m.set_option THEN 'set' END), false
 FROM pg_auth_members m JOIN r ON r.oid=m.member JOIN pg_roles g ON g.oid=m.roleid
UNION ALL
SELECT 'role', rolname, NULL, NULL, NULL, false FROM r
UNION ALL
SELECT 'version', NULL, max(version)::text, NULL, NULL, false FROM migrations
"""


def expected_grants(version, schema="fcs", names=None):
    """The (role, object, column, privilege) set roles.sql grants at a schema version, or None for an unknown one."""
    data = json.loads(Path(__file__).with_name("roles_expected.json").read_text())
    if str(version) not in data:
        return None
    names = names or {}
    out = set()
    for v in range(1, version + 1):
        for role, entries in data.get(str(v), {}).items():
            for entry in entries:
                privilege, obj, col = re.fullmatch(r"([A-Z]+) fcs((?:\.[a-z0-9_]+)?)(?:\(([a-z0-9_]+)\))?", entry).groups()
                out.add((names.get(role, role), schema + obj, col, privilege))
    return out


def check_roles(conn, schema="fcs", names=None):
    """`db-roles --check`: one line per missing or extra privilege, membership or role; empty when they match."""
    names = names or {}
    roles = [names.get(r, r) for r in ROLES]
    rows = conn.execute(GRANTS, {"schema": schema, "roles": roles}).fetchall()
    version = int(next(r["object"] for r in rows if r["kind"] == "version") or 0)
    expected = expected_grants(version, schema, names)
    if expected is None:
        return version, ["unknown schema version %d" % version]
    present = {r["role"] for r in rows if r["kind"] == "role"}
    missing_roles = [r for r in roles if r not in present]
    lines = ["missing role " + r for r in missing_roles]

    def name(role, obj, col, privilege):
        return " ".join((role, privilege, obj + ("(%s)" % col if col else "")))

    acl = [r for r in rows if r["kind"] == "acl"]
    actual = {(r["role"], r["object"], r["col"], r["privilege"]) for r in acl}
    granting = {(r["role"], r["object"], r["col"], r["privilege"]) for r in acl if r["option"]}

    def order(entry):
        return tuple(x or "" for x in entry)

    for entry in sorted(expected - actual, key=order):
        if entry[0] not in missing_roles:
            lines.append("missing " + name(*entry))
    for entry in sorted(actual - expected | granting, key=order):
        lines.append("extra " + name(*entry) + (" WITH GRANT OPTION" if entry in granting else ""))
    for r in sorted((r for r in rows if r["kind"] == "member"), key=lambda r: (r["role"], r["object"])):
        lines.append("extra %s MEMBER OF %s (%s)" % (r["role"], r["object"], r["privilege"] or "no options"))
    return version, lines


def db_roles_check(store):
    with store._connect() as conn:
        conn.read_only = True
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(store.schema)))
        version, lines = check_roles(conn, store.schema)
    if lines:
        raise CalendarError("db_roles_drift", details={"schema_version": version, "drift": lines})
    return {"db_roles_check": "ok", "schema_version": version}


def run(argv, home):
    for name in ("httpx", "httpcore", "google.auth", "google_auth_oauthlib", "urllib3", "oauthlib", "mcp"):
        logging.getLogger(name).disabled = True
    args = parser().parse_args(argv)
    if args.action in ("serve", "worker"):
        profile_name = "gateway" if args.action == "serve" else "worker"
        if not home.is_absolute() or not (home / "service" / (profile_name + ".json")).is_file():
            raise CalendarError("existing_runtime_configuration_required")
        if args.action == "worker" and not (home / "service/vault/key").is_file():
            raise CalendarError("existing_vault_required")
    if getattr(args, "check", False):
        # ANYM-601: the read-only check creates nothing, not even the service directory.
        if (home / "service").is_symlink() or not (home / "service" / "runtime.json").is_file():
            raise CalendarError("existing_runtime_configuration_required")
    root = private_dir(home / "service")
    profile = args.action if args.action in ("serve", "worker") else "runtime"
    if profile == "serve":
        profile = "gateway"
    config = json.loads(read_private(root / (profile + ".json")))
    store = Store(config["dsn"])
    try:
        return dispatch(args, home, root, config, store)
    finally:
        store.close()


def dispatch(args, home, root, config, store):
    if args.action == "serve":
        import uvicorn
        from .api import create_app

        uvicorn.run(
            create_app(store, config["port"], config.get("allowed_hosts"), config.get("allowed_origins")),
            host=config.get("bind", "127.0.0.1"),
            port=config["port"],
            access_log=False,
            log_level="warning",
            proxy_headers=False,
            timeout_graceful_shutdown=15,
        )
        return {"stopped": True}
    if args.action == "migrate":
        store.migrate()
        return {"migrated": True}
    if args.action == "schema-status":
        return {"migrations": store.query("SELECT version,checksum FROM migrations ORDER BY version")}
    if args.action == "db-roles":
        return db_roles_check(store) if args.check else db_roles(store, root, config)
    if args.action == "migrate-expand":
        if args.status:
            return store.expand_status()
        try:
            plan = json.loads(args.plan.read_bytes())
        except (OSError, ValueError):
            raise CalendarError("plan_invalid") from None
        return store.migrate_expand(plan)
    if args.action == "init-family":
        store.person(args.owner, args.label)
        store.group(args.group, "Семья")
        store.member(args.group, args.owner, "group_admin")
        return {"group_id": args.group, "owner_id": args.owner}
    if args.action == "member":
        store.person(args.id, args.label)
        store.member(args.group, args.id, args.role)
        return {"member_id": args.id, "role": args.role, "provider_connected": False}
    if args.action == "grant":
        scopes = args.scopes.split(",")
        if set(scopes) - set(READ + WRITE):
            raise CalendarError("unsupported_scope")
        store.grant(args.group, args.member, args.calendar, scopes, args.visibility)
        return {"grant_saved": True}
    if args.action == "bind":
        store.binding(args.group, args.member, args.calendar)
        return {"binding_saved": True}
    if args.action == "member-person":
        return store.member_person(args.group, args.member, None if args.unlink else args.person)
    if args.action == "family-settings":
        return store.family_settings(args.group, args.timezone, args.default_calendar)
    if args.action in ("allow-write", "disable-write"):
        store.write_opt_in(args.calendar, args.action == "allow-write")
        return {"write_enabled": args.action == "allow-write"}
    if args.action in ("allow-invitations", "disable-invitations"):
        store.invitations_opt_in(args.calendar, args.action == "allow-invitations")
        return {"invitations_enabled": args.action == "allow-invitations"}
    if args.action == "agent-key":
        scopes = args.scopes.split(",")
        # The event router's key holds calendar:events alone (ANYM-565).
        if set(scopes) - set(READ + WRITE) and scopes != [EVENTS]:
            raise CalendarError("unsupported_scope")
        if (args.output is None) == (args.token_file is None):
            raise CalendarError("output_or_token_file_required")
        if (args.token_file is None) != (args.operation_id is None):
            raise CalendarError("key_and_operation_id_required")
        if not re.fullmatch("[0-9]{1,3}", args.days) or not 1 <= int(args.days) <= 183:
            raise CalendarError("invalid_agent_lifetime")
        options = {"days": int(args.days)}
        if args.token_file is not None:
            # ANYM-495: the caller wrote the key durably first; only its hash is stored, nothing is written.
            options.update(key=read_key(args.token_file), operation_id=operation_name(args.operation_id))
        if args.check:
            # The issue's own validation (member, calendars, principal and key free, operation replay), no write.
            result = store.check_schema()
            if result["check"] == "ok" or args.token_file is None:
                store.agent(args.id, args.group, args.member, scopes, args.calendar, args.visibility, **options,
                            check=True)
            return result
        if args.token_file is not None:
            return store.agent(args.id, args.group, args.member, scopes, args.calendar, args.visibility, **options)
        key = store.agent(args.id, args.group, args.member, scopes, args.calendar, args.visibility, **options)
        write_private(args.output, key.encode())
        return {"principal_id": args.id, "key_saved": True, "expires_days": options["days"]}
    if args.action in ("revoke-agent", "extend-agent", "list-agents") and args.check:
        if args.action == "extend-agent" and (not re.fullmatch("[0-9]{1,3}", args.days) or not 1 <= int(args.days) <= 365):
            raise CalendarError("invalid_agent_lifetime")
        return store.check_schema()
    if args.action == "revoke-agent":
        store.revoke(args.id)
        return {"revoked": True}
    if args.action == "extend-agent":
        previous, new = store.extend_agent(args.id, args.days)
        return {"principal_id": args.id, "previous_expires_at": previous.isoformat(), "expires_at": new.isoformat()}
    if args.action == "list-agents":
        return {"agents": store.list_agents()}
    if args.action == "agent-scope":
        # ANYM-496: read only; the scope a clone copies (member, scopes, calendars and their visibility).
        return store.check_schema() if args.check else store.agent_scope(args.id)
    if args.action == "agent-info" and (args.key_file is None) == (args.operation_id is None):
        raise CalendarError("key_file_or_operation_id_required")
    if args.action == "agent-info":
        if args.operation_id is not None:
            operation_name(args.operation_id)
        else:
            # ANYM-494: public fields by key file, in input order; the key and its hash are never printed.
            with args.key_file.open("rb") as source:
                raw = source.read(KEY_INFO_BYTES + 1)
            if len(raw) > KEY_INFO_BYTES:
                raise CalendarError("key_file_too_large")
            keys = raw.decode().splitlines()
            if not 1 <= len(keys) <= 100:
                raise CalendarError("invalid_key_count")
        if args.check:
            return store.check_schema()
        if args.operation_id is not None:
            return {"agents": store.agent_operation_info(args.operation_id)}
        return {"agents": store.agent_info(keys)}
    if args.action == "guardian-rule":
        if args.rule_action == "set":
            return store.guardian_rule_set(
                args.group,
                args.dependent,
                args.activity,
                args.duty,
                args.guardian,
                args.source_calendar,
                args.target_calendar,
                args.lead_minutes,
                args.title,
                args.visibility,
                args.copy_location,
                args.expected_version,
            )
        if args.rule_action == "disable":
            return store.guardian_rule_disable(args.id, args.expected_version)
        return {"guardian_rules": store.guardian_rule_list()}
    if args.action == "disconnect":
        with store.transaction() as c:
            c.execute(
                "UPDATE connections SET enabled=false,version=version+1 WHERE id=%s", (args.connection,)
            )
            c.execute(
                "UPDATE calendars SET write_enabled=false,version=version+1 WHERE connection_id=%s",
                (args.connection,),
            )
        return {"disconnected": True}
    vault = Vault(root / "vault")
    if args.action == "import-icloud":
        try:
            ZoneInfo(args.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise CalendarError("invalid_timezone") from None
        legacy = Registry(home)
        connection = legacy.load(args.connection)
        # Preserve the pilot's credentials/discovery/allowlists/journals unchanged.
        vault.save(
            args.connection,
            {"provider": "icloud", "username": connection.username, "password": connection.password},
        )
        store.connection(args.connection, args.owner, "icloud")
        for c in legacy._entry(args.connection)["calendars"]:
            store.calendar(c["ref"], args.connection, c["href"], c["label"], args.timezone, c["privilege"])
        return {"connection_id": args.connection, "imported": True, "write_enabled": False}
    if args.action == "connect-google":
        result = connect(args.client, vault, args.id, args.email, root / "google-authorization.url")
        store.connection(args.id, args.owner, "google")
        result.update(asyncio.run(discover(store, vault, args.id)))
        return result
    if args.action == "discover":
        return asyncio.run(discover(store, vault, args.connection))
    from .worker import Worker

    providers = Providers(store, vault)
    worker = Worker(store, providers)
    if args.action == "worker":
        asyncio.run(worker.run())
        return {"stopped": True}
    if args.action == "reconcile":

        async def reconcile():
            try:
                await worker.run_operation(args.operation, reconcile=True)
            finally:
                await providers.close()

        asyncio.run(reconcile())
        return {"operation_id": args.operation, "reconciled": True}
