import argparse
import asyncio
from datetime import datetime, timezone
import getpass
import json
import logging
import os
from pathlib import Path
import sys

from .config import Registry
from .errors import CalendarError
from .private_files import read_private, write_private
from .providers.caldav.adapter import CalDAVAdapter
from .providers.caldav.transport import DAVTransport
from .pilot.icloud import Pilot
from .pilot.journal import Journal
from .pilot.visual import agent_visual_evidence, visual_verified


def parser():
    p = argparse.ArgumentParser(description="Family Calendar Service — isolated iCloud pilot")
    p.add_argument(
        "--home",
        type=Path,
        default=Path(os.environ.get("FCS_DATA_DIR", Path.cwd() / ".local")),
        help="Owner-only state directory; FCS_DATA_DIR is independent of checkout",
    )
    sub = p.add_subparsers(dest="command", required=True)
    connection = sub.add_parser("connection").add_subparsers(dest="action", required=True)
    add = connection.add_parser("add-icloud", help="Enter Apple Account and app-specific password locally")
    add.add_argument("--id", required=True)
    connection.add_parser("list")
    service = sub.add_parser("service", help="Multi-account owner CLI, REST/MCP gateway and worker")
    service.add_argument("arguments", nargs=argparse.REMAINDER)
    pilot = sub.add_parser("pilot").add_subparsers(dest="provider", required=True)
    icloud = pilot.add_parser("icloud").add_subparsers(dest="action", required=True)
    for action in ("discover", "allow-write", "disable-write", "run"):
        cmd = icloud.add_parser(action)
        cmd.add_argument("--connection", required=True)
        if action != "discover":
            cmd.add_argument("--calendar-ref", required=True)
        if action == "allow-write":
            cmd.add_argument(
                "--test-calendar",
                action="store_true",
                required=True,
                help="I selected a dedicated synthetic test calendar",
            )
        if action == "run":
            cmd.add_argument("--allow-write", action="store_true", required=True)
            cmd.add_argument("--run-dir", type=Path, required=True)
    for action in ("status", "reconcile", "cleanup", "record-visual-check"):
        cmd = icloud.add_parser(action)
        cmd.add_argument("--run-dir", type=Path, required=True)
        if action == "record-visual-check":
            cmd.add_argument("--result", choices=("pass", "fail"), required=True)
            cmd.add_argument(
                "--future-result", choices=("pass", "fail", "not-checked"), default="not-checked"
            )
            cmd.add_argument(
                "--native-range-result", choices=("pass", "fail", "not-checked"), default="not-checked"
            )
            cmd.add_argument("--checked-all-cases", action="store_true", required=True)
            cmd.add_argument("--client", required=True, help="Apple Calendar/iCloud UI and version")
            cmd.add_argument(
                "--source", choices=("owner_reported", "agent_observed"), default="owner_reported"
            )
            cmd.add_argument(
                "--evidence", type=Path, help="Agent observation manifest with image/TXT artifacts in run-dir"
            )
    return p


def output(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


async def _live(args, registry):
    if args.action in {"status", "record-visual-check"}:
        report_path = args.run_dir / "report.json"
        report = json.loads(read_private(report_path))
        if args.action == "record-visual-check":
            if report["environment"] != "icloud_live":
                raise CalendarError("mock_evidence_not_live")
            visual = {
                "result": args.result,
                "future_result": args.future_result,
                "native_range_result": args.native_range_result,
                "client": args.client[:200],
                "source": args.source,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            if args.source == "agent_observed":
                if args.evidence is None:
                    raise CalendarError("visual_evidence_required")
                visual.update(agent_visual_evidence(args.run_dir, args.evidence))
                if not visual_verified(args.run_dir, visual):
                    raise CalendarError("invalid_visual_evidence")
            write_private(args.run_dir / "visual-check.json", json.dumps(visual).encode())
            # Gate is recomputed by run/reconcile; recording a declaration does not invent server evidence.
            output({"result": args.result, "next_action": "reconcile_to_refresh_report"})
        else:
            output(report)
        return
    if args.action in {"reconcile", "cleanup"}:
        scope = json.loads(read_private(args.run_dir / "scope.json"))
        args.connection, args.calendar_ref = scope["connection_id"], scope["calendar_ref"]
    connection = registry.load(args.connection)
    transport = DAVTransport(connection.username, connection.password)
    try:
        adapter = CalDAVAdapter(connection.id, transport)
        if args.action == "discover":
            calendars = await adapter.discover()
            registry.save_discovery(connection.id, calendars)
            output(
                {
                    "connection_id": connection.id,
                    "read_verified": True,
                    "calendars": [{k: c[k] for k in ("ref", "label", "privilege")} for c in calendars],
                }
            )
            return
        if args.action in {"allow-write", "disable-write"}:
            if args.action == "allow-write":
                registry.allow_calendar(connection.id, args.calendar_ref)
            else:
                registry.disable_write(connection.id, args.calendar_ref)
            output(
                {
                    "connection_id": connection.id,
                    "calendar_ref": args.calendar_ref,
                    "write_opt_in": args.action == "allow-write",
                    "write_verified": False,
                }
            )
            return
        calendar = registry.require_write(connection.id, args.calendar_ref)
        journal = Journal(args.run_dir, connection.id, args.calendar_ref)
        try:
            pilot = Pilot(
                adapter,
                journal,
                calendar["href"],
                check_write=lambda: registry.require_write(connection.id, args.calendar_ref),
            )
            if args.action == "cleanup":
                output(await pilot.cleanup())
            else:
                if args.action == "reconcile":
                    for step in journal.steps():
                        if step["state"] in {"sending", "unknown", "needs_attention"}:
                            await pilot.executor.reconcile(step["id"])
                output(await pilot.run())
        finally:
            journal.close()
    finally:
        await transport.close()


def main(argv=None):
    # No library access logs, request bodies or exception reprs for real accounts.
    for name in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(name).disabled = True
    try:
        args = parser().parse_args(argv)
        registry = Registry(args.home) if args.command != "service" else None
        if args.command == "service":
            from .service.cli import run

            output(run(args.arguments, args.home))
        elif args.command == "connection":
            if args.action == "list":
                output({"connections": registry.list_connections()})
            else:
                if not sys.stdin.isatty():
                    raise CalendarError("interactive_terminal_required")
                username = input("Apple Account email: ").strip()
                password = getpass.getpass("App-specific password (hidden): ")
                registry.add(args.id, username, password)
                output(
                    {
                        "connection_id": args.id,
                        "saved": True,
                        "read_verified": False,
                        "next_action": "pilot icloud discover",
                    }
                )
        else:
            asyncio.run(_live(args, registry))
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    except CalendarError as exc:
        output({"error": exc.code, **exc.details})
        return 1
    except KeyboardInterrupt:
        output({"error": "interrupted", "next_action": "inspect_run_status_before_retry"})
        return 130
    except (OSError, ValueError, KeyError, TypeError):
        output({"error": "local_configuration_error"})
        return 1
    except Exception:
        # An unexpected error can happen after a request was sent. Never print its sensitive repr.
        output({"error": "unexpected_error", "next_action": "inspect_run_status_before_retry"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
