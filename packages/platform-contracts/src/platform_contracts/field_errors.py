"""The shared `field_errors` shape of a rejected request: `{field, reason, expected?, allowed?}` (ANYM-295).

Builders live with the service that owns the schema. This module holds the closed vocabulary and the
bounds, and `validated` rechecks an entry list that crossed a trust boundary. Standard library only.
"""
import re

REASONS = frozenset((
    "required", "unknown_field", "type", "enum", "min_length", "max_length", "pattern", "minimum",
    "maximum", "min_items", "max_items", "unique", "format", "blank", "size",
))
MAX_ENTRIES = 20
MAX_PATH = 128
MAX_ALLOWED = 50
MAX_ALLOWED_TEXT = 64
MAX_EXPECTED = 160
SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}|[0-9]{1,4}")
KEYS = frozenset(("field", "reason", "expected", "allowed"))


def _path(value):
    return (isinstance(value, str) and len(value) <= MAX_PATH
            and all(SEGMENT.fullmatch(part) for part in value.split("/")))


def _allowed(value):
    return (isinstance(value, list) and len(value) <= MAX_ALLOWED and all(
        v is None or isinstance(v, (bool, int)) or isinstance(v, str) and len(v) <= MAX_ALLOWED_TEXT for v in value))


def validated(value):
    """Return a copy of `value` when it is a well-formed `field_errors` list, else None (never a partial list)."""
    if not isinstance(value, list) or not 0 < len(value) <= MAX_ENTRIES:
        return None
    result, seen = [], set()
    for entry in value:
        if (not isinstance(entry, dict) or not KEYS.issuperset(entry) or not _path(entry.get("field"))
                or entry["field"] in seen or entry.get("reason") not in REASONS):
            return None
        expected = entry.get("expected")
        if "expected" in entry and not (isinstance(expected, str) and 0 < len(expected) <= MAX_EXPECTED):
            return None
        if "allowed" in entry and not (_allowed(entry["allowed"]) and entry["allowed"]):
            return None
        seen.add(entry["field"])
        result.append({k: entry[k] for k in ("field", "reason", "expected", "allowed") if k in entry})
    return result
