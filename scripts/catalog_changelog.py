"""Write the next services/gateway changelog entry for a catalog.json change, or check that none is missing (ANYM-964).

Every change of a tool name or schema in catalog.json carries its changelog.json entry in the same pull request;
the gateway test test_the_newest_changelog_entry_pins_the_packaged_catalog fails without it.

    python3 scripts/catalog_changelog.py --check
    python3 scripts/catalog_changelog.py --add --note "Chats gain a waiting filter." [--module messaging] [--base origin/main]

--add diffs the base's catalog.json (default origin/main) against the working tree and appends revision N+1. After a
rebase that brought another entry, take main's changelog.json and run --add again. The note names no tool: whats_new
filters the tool lists by key, never the note.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "services/gateway/src/agent_gateway"
CATALOG = ROOT / PACKAGE / "catalog.json"
CHANGELOG = ROOT / PACKAGE / "changelog.json"


def catalog_sha256(rows):
    """A copy of agent_gateway.recipes.catalog_sha256: tool names and schemas only."""
    value = sorted(({"name": row["name"], "inputSchema": row["inputSchema"],
                     **({"outputSchema": row["outputSchema"]} if "outputSchema" in row else {})} for row in rows),
                   key=lambda row: row["name"])
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def next_entry(old, new, changelog, *, note, today, module=None):
    """Revision N+1 for the change from `old` (the catalog the newest entry pins) to `new`."""
    if catalog_sha256(old) != changelog[-1]["catalog_sha256"]:
        raise ValueError("the base catalog is not the one the newest changelog entry pins; "
                         "take the base branch's changelog.json or pass --base")
    if catalog_sha256(new) == changelog[-1]["catalog_sha256"]:
        raise ValueError("no tool name or schema changed; no entry is needed")
    before = {row["name"]: row for row in old}
    after = {row["name"]: row for row in new}
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    changed = sorted(n for n in after.keys() & before.keys() if catalog_sha256([after[n]]) != catalog_sha256([before[n]]))
    modules = {after[n]["module"] for n in added + changed} | {before[n]["module"] for n in removed}
    if module is None:
        if len(modules) != 1:
            raise ValueError(f"the change spans modules {sorted(modules)}; pass --module")
        module = modules.pop()
    return {"revision": changelog[-1]["revision"] + 1, "date": today, "module": module, "added": added,
            "changed": changed, "removed": removed, "note": note, "catalog_sha256": catalog_sha256(new)}


def dump(changelog):
    return "[\n" + ",\n".join("  " + json.dumps(entry, ensure_ascii=False) for entry in changelog) + "\n]\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 when catalog.json has no changelog entry")
    parser.add_argument("--add", action="store_true", help="append the next entry")
    parser.add_argument("--note", help="what changed, at most 200 characters, naming no tool")
    parser.add_argument("--module", help="the entry's module when the change spans several")
    parser.add_argument("--base", default="origin/main", help="the git revision whose catalog.json the change starts from")
    args = parser.parse_args()
    changelog = json.loads(CHANGELOG.read_text())
    current = json.loads(CATALOG.read_text())
    if args.add:
        if not args.note or len(args.note) > 200:
            parser.error("--add needs --note of 1-200 characters")
        base = json.loads(subprocess.run(["git", "show", f"{args.base}:{PACKAGE}/catalog.json"], cwd=ROOT,
                                         check=True, capture_output=True, text=True).stdout)
        try:
            entry = next_entry(base, current, changelog, note=args.note, module=args.module,
                               today=datetime.now(timezone.utc).date().isoformat())
        except ValueError as error:
            parser.exit(1, f"{error}\n")
        CHANGELOG.write_text(dump([*changelog, entry]))
        print(f"changelog revision {entry['revision']}: added {entry['added']} changed {entry['changed']} "
              f"removed {entry['removed']}")
    elif catalog_sha256(current) != changelog[-1]["catalog_sha256"]:
        parser.exit(1, "catalog.json changed without a changelog entry: run "
                       "python3 scripts/catalog_changelog.py --add --note '...'\n")
    else:
        print(f"changelog revision {changelog[-1]['revision']} pins catalog.json")


if __name__ == "__main__":
    main()
