import argparse
import asyncio
import json
import logging
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import sql
import secrets

from family_calendar.config import Registry
from family_calendar.errors import CalendarError
from family_calendar.private_files import read_private, write_private, private_dir
from family_calendar.providers.google.auth import connect
from .core import READ, WRITE
from .providers import Providers, source_ref
from .store import Store
from .vault import Vault


def parser():
    p = argparse.ArgumentParser(description="Family Calendar owner CLI / isolated gateway / worker")
    sub = p.add_subparsers(dest="action", required=True)
    for name in ("migrate", "schema-status", "db-roles", "serve", "worker"):
        sub.add_parser(name)
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
    agent = sub.add_parser("agent-key")
    agent.add_argument("--id", required=True)
    agent.add_argument("--group", default="family")
    agent.add_argument("--member", required=True)
    agent.add_argument("--calendar", action="append", default=[])
    agent.add_argument("--scopes", default=",".join(READ))
    agent.add_argument("--visibility", choices=["details", "busy_only"], default="details")
    agent.add_argument("--output", type=Path, required=True)
    revoke = sub.add_parser("revoke-agent")
    revoke.add_argument("--id", required=True)
    extend = sub.add_parser("extend-agent")
    extend.add_argument("--id", required=True)
    extend.add_argument("--days", required=True, help="New lifetime from now, 1-365 days")
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
        return db_roles(store, root, config)
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
    if args.action in ("allow-write", "disable-write"):
        store.write_opt_in(args.calendar, args.action == "allow-write")
        return {"write_enabled": args.action == "allow-write"}
    if args.action == "agent-key":
        scopes = args.scopes.split(",")
        if set(scopes) - set(READ + WRITE):
            raise CalendarError("unsupported_scope")
        key = store.agent(args.id, args.group, args.member, scopes, args.calendar, args.visibility)
        write_private(args.output, key.encode())
        return {"principal_id": args.id, "key_saved": True, "expires_days": 90}
    if args.action == "revoke-agent":
        store.revoke(args.id)
        return {"revoked": True}
    if args.action == "extend-agent":
        previous, new = store.extend_agent(args.id, args.days)
        return {"principal_id": args.id, "previous_expires_at": previous.isoformat(), "expires_at": new.isoformat()}
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
