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
SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}|[0-9]{1,4}")
KEYS = frozenset(("field", "reason", "expected", "allowed"))
MAX_INTEGER = 2**53
# `expected` is a hint from this closed set, or `matching <pattern>` whose pattern the receiver compares with its
# reviewed schema; never free text, so a buggy backend cannot echo input through it.
MATCHING = "matching "
MAX_PATTERN = 80
HINT_TEXTS = frozenset((
    "string", "integer", "number", "boolean", "array", "object",
    "text with a non-space character", "at most 8 KiB", "only the listed reference types",
    "no control characters", "set when wake_rules has reply_to_confirmed_send",
))
HINT_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"[0-9]{1,6}–[0-9]{1,6} characters",
    r"at (?:least|most) [0-9]{1,6} (?:characters?|items)",
    r"(?:at least|more than|at most|less than) -?[0-9]{1,15}(?:\.[0-9]{1,6})?",
))


def _path(value):
    return (isinstance(value, str) and len(value) <= MAX_PATH
            and all(SEGMENT.fullmatch(part) for part in value.split("/")))


def _text(value, limit):
    """A string that is short and encodes to UTF-8 (an escaped lone surrogate does not)."""
    if not isinstance(value, str) or len(value) > limit:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def hint(value):
    if not isinstance(value, str):
        return False
    if value.startswith(MATCHING):
        return _text(value[len(MATCHING):], MAX_PATTERN) and len(value) > len(MATCHING)
    return value in HINT_TEXTS or any(p.fullmatch(value) for p in HINT_PATTERNS)


def _scalar(value):
    return (value is None or type(value) is bool or type(value) is int and abs(value) <= MAX_INTEGER
            or _text(value, MAX_ALLOWED_TEXT))


def _allowed(value):
    return isinstance(value, list) and 0 < len(value) <= MAX_ALLOWED and all(_scalar(v) for v in value)


def validated(value):
    """Return a copy of `value` when it is a well-formed `field_errors` list, else None (never a partial list)."""
    if not isinstance(value, list) or not 0 < len(value) <= MAX_ENTRIES:
        return None
    result, seen = [], set()
    for entry in value:
        if (not isinstance(entry, dict) or not KEYS.issuperset(entry) or not _path(entry.get("field"))
                or entry["field"] in seen or not isinstance(entry.get("reason"), str) or entry["reason"] not in REASONS):
            return None
        if "expected" in entry and not hint(entry["expected"]):
            return None
        if "allowed" in entry and not _allowed(entry["allowed"]):
            return None
        seen.add(entry["field"])
        result.append({k: entry[k] for k in ("field", "reason", "expected", "allowed") if k in entry})
    return result
