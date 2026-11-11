"""Per-module recipes and the catalog changelog that platform_capabilities serves (ANYM-964).

Both files are reviewed repository content packaged beside catalog.json and checked at import like it, so a malformed
file fails the build and CI, never a running gateway's answer. What a key sees is cut to the tools its policy shows it:
a recipe with any step tool outside that set is left out whole, and a changelog entry lists only those tools.
"""
from copy import deepcopy
from datetime import date
from importlib.resources import files
import hashlib
import json
import re

from .catalog import CATALOG, InvalidArguments, arguments

MAX_RECIPES = 6
STEPS = (2, 8)
TITLE, WHEN, NOTE, CHANGE_NOTE = 80, 160, 160, 200
RECIPE_KEYS = {"id", "title", "when", "steps"}
STEP_KEYS = {"tool", "note", "arguments"}
ENTRY_KEYS = {"revision", "date", "module", "added", "changed", "removed", "note", "catalog_sha256"}
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
TOOLS = ("added", "changed", "removed")


def catalog_sha256(rows):
    """The digest of catalog.json's tool names and schemas; descriptions and guidance may change without an entry.

    scripts/catalog_changelog.py keeps a copy of this function; a test holds the two equal.
    """
    value = sorted(({"name": row["name"], "inputSchema": row["inputSchema"],
                     **({"outputSchema": row["outputSchema"]} if "outputSchema" in row else {})} for row in rows),
                   key=lambda row: row["name"])
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def text(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit


def load_recipes(value):
    """Check `{module: [recipe]}` against the bounds and the packaged schemas; raise ValueError on any violation."""
    if not isinstance(value, dict):
        raise ValueError("recipes: an object of modules")
    seen = set()
    for module, rows in value.items():
        if not (isinstance(rows, list) and len(rows) <= MAX_RECIPES and IDENTIFIER.match(module)):
            raise ValueError(f"recipes.{module}: at most {MAX_RECIPES} recipes")
        for recipe in rows:
            if not (isinstance(recipe, dict) and set(recipe) == RECIPE_KEYS and isinstance(recipe["id"], str)
                    and IDENTIFIER.match(recipe["id"]) and recipe["id"] not in seen):
                raise ValueError(f"recipes.{module}: a recipe needs a unique id and only {sorted(RECIPE_KEYS)}")
            seen.add(recipe["id"])
            if not (text(recipe["title"], TITLE) and text(recipe["when"], WHEN) and isinstance(recipe["steps"], list)
                    and STEPS[0] <= len(recipe["steps"]) <= STEPS[1]):
                raise ValueError(f"recipe {recipe['id']}: title, when or step count out of bounds")
            for step in recipe["steps"]:
                check_step(module, recipe["id"], step)
    return value


def check_step(module, recipe, step):
    if not (isinstance(step, dict) and {"tool", "note"} <= set(step) <= STEP_KEYS and text(step["note"], NOTE)):
        raise ValueError(f"recipe {recipe}: a step is {{tool, note, arguments?}} with a note of at most {NOTE}")
    tool = CATALOG.get(step["tool"]) if isinstance(step["tool"], str) else None
    if tool is None or tool["module"] not in {module, "gateway"}:
        raise ValueError(f"recipe {recipe}: {step['tool']!r} is not a {module} or gateway catalog tool")
    if "arguments" in step:
        try:
            arguments(tool, step["arguments"])
        except (InvalidArguments, ValueError, TypeError) as error:
            raise ValueError(f"recipe {recipe}: arguments fail the {step['tool']} schema") from error


def load_changelog(value):
    """Check the changelog: revisions 1, 2, ... with bounded fields; raise ValueError on any violation."""
    if not (isinstance(value, list) and value):
        raise ValueError("changelog: a non-empty list")
    for number, entry in enumerate(value, 1):
        if not (isinstance(entry, dict) and set(entry) == ENTRY_KEYS and type(entry["revision"]) is int
                and entry["revision"] == number):
            raise ValueError(f"changelog entry {number}: keys {sorted(ENTRY_KEYS)} and revision {number}")
        try:
            date.fromisoformat(entry["date"])
        except (TypeError, ValueError) as error:
            raise ValueError(f"changelog revision {number}: date YYYY-MM-DD") from error
        if not (isinstance(entry["date"], str) and len(entry["date"]) == 10 and isinstance(entry["module"], str)
                and IDENTIFIER.match(entry["module"]) and text(entry["note"], CHANGE_NOTE)
                and isinstance(entry["catalog_sha256"], str) and SHA256.match(entry["catalog_sha256"])
                and all(isinstance(entry[k], list) and all(isinstance(n, str) for n in entry[k]) for k in TOOLS)):
            raise ValueError(f"changelog revision {number}: module, note, catalog_sha256 or tool lists malformed")
    return value


RECIPES = load_recipes(json.loads(files(__package__).joinpath("recipes.json").read_text()))
CHANGELOG = load_changelog(json.loads(files(__package__).joinpath("changelog.json").read_text()))


def revision():
    return CHANGELOG[-1]["revision"]


def recipes_for(module, visible):
    """The module's recipes whose every step tool is in `visible`; each recipe whole or not at all."""
    seen = set(visible)
    return [deepcopy(r) for r in RECIPES.get(module, ()) if all(step["tool"] in seen for step in r["steps"])]


def whats_new(since, visible):
    """The changelog entries after `since`, each cut to the tools in `visible`.

    Removed tools have no schema left to check, so they are named only in entries of a module the key has tools in.
    An entry that named tools and has none left is dropped; a note-only entry (the baseline) stays.
    """
    seen = set(visible)
    modules = {CATALOG[name]["module"] for name in seen if name in CATALOG}
    result = []
    for entry in CHANGELOG:
        if entry["revision"] <= since:
            continue
        kept = {"added": [n for n in entry["added"] if n in seen], "changed": [n for n in entry["changed"] if n in seen],
                "removed": list(entry["removed"]) if entry["module"] in modules else []}
        if any(entry[k] for k in TOOLS) and not any(kept.values()):
            continue
        result.append({"revision": entry["revision"], "date": entry["date"], "module": entry["module"], **kept,
                       "note": entry["note"]})
    return result


def out_of_range(since):
    """The `invalid_arguments` body of a `since` outside 0..revision, or None when it is inside."""
    bound = "minimum" if since < 0 else "maximum" if since > revision() else None
    if bound is None:
        return None
    return {"error": "invalid_arguments", "field_errors": [{"field": "since", "reason": bound}],
            "changelog_revision": revision(), "next_action": "check_parameters"}


def extend(answer, visible, args):
    """Add recipe_ids, changelog_revision and, when asked, a module's recipes and whats_new to an answer."""
    for row in answer["modules_available"]:
        row["recipe_ids"] = [r["id"] for r in recipes_for(row["id"], visible)]
    answer["changelog_revision"] = revision()
    if args.get("module") is not None:
        answer["recipes"] = recipes_for(args["module"], visible)
    if args.get("since") is not None:
        answer["whats_new"] = whats_new(args["since"], visible)
    return answer
