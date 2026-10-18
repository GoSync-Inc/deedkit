"""Bounded `field_errors` for arguments that fail a reviewed tool schema.

Entries are `{field, reason, expected?, allowed?}`. Field paths come from the reviewed
schema, `reason` is a closed vocabulary, and `expected`/`allowed` are copied from the
schema, never from the submitted value. An unknown key the agent sent is named only
when it is a short identifier.
"""

import re

REASONS = frozenset((
    "required", "unknown_field", "type", "enum", "min_length", "max_length", "pattern", "minimum",
    "maximum", "min_items", "max_items", "unique", "format", "blank", "size",
))
MAX_ENTRIES = 20
MAX_PATH = 128
MAX_ALLOWED = 50
SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}|[0-9]{1,4}")
# `$` stands for the arguments object itself.
ROOT = "$"

KEYWORDS = {
    "type": "type",
    "enum": "enum",
    "const": "enum",
    "minLength": "min_length",
    "maxLength": "max_length",
    "pattern": "pattern",
    "minimum": "minimum",
    "exclusiveMinimum": "minimum",
    "maximum": "maximum",
    "exclusiveMaximum": "maximum",
    "minItems": "min_items",
    "maxItems": "max_items",
    "contains": "min_items",
    "minContains": "min_items",
    "maxContains": "max_items",
    "uniqueItems": "unique",
    "required": "required",
    "dependentRequired": "required",
    "additionalProperties": "unknown_field",
    "propertyNames": "unknown_field",
    "minProperties": "size",
    "maxProperties": "size",
}
EXPECTED = {
    "minLength": "length at least {}",
    "maxLength": "length at most {}",
    "minimum": "at least {}",
    "exclusiveMinimum": "more than {}",
    "maximum": "at most {}",
    "exclusiveMaximum": "less than {}",
    "minItems": "at least {} items",
    "maxItems": "at most {} items",
}


def _join(path, name):
    """Extend a field path, or return None when the segment is not a short identifier or index."""
    segment = str(name)
    if not SEGMENT.fullmatch(segment):
        return None
    joined = "/".join((*path, segment))
    return joined if len(joined) <= MAX_PATH else None


def _types(value):
    names = [value] if isinstance(value, str) else [v for v in value if isinstance(v, str)]
    return " or ".join(n for n in names if n != "null") or "null"


def _entries(error, path):
    """Yield (path, reason, expected, allowed) for one jsonschema error at `path`."""
    keyword = error.validator
    if keyword in ("anyOf", "oneOf"):
        # A branch whose type matched but whose constraint failed explains the failure best.
        shallow = [e for e in error.context if not e.relative_path and e.validator != "type"]
        if shallow:
            yield from _entries(shallow[0], path)
            return
        kinds = [v for e in error.context if e.validator == "type" for v in ([e.validator_value]
                 if isinstance(e.validator_value, str) else e.validator_value)]
        yield path, "type", _types(list(dict.fromkeys(kinds))) if kinds else None, None
        return
    if keyword == "required":
        for name in error.validator_value:
            if isinstance(error.instance, dict) and name not in error.instance:
                yield [*path, name], "required", None, None
        return
    if keyword == "additionalProperties":
        known = error.schema.get("properties", {})
        for name in error.instance:
            if name not in known:
                # An unsafe key is reported at the object that holds it.
                yield ([*path, name] if SEGMENT.fullmatch(str(name)) else path), "unknown_field", None, None
        return
    reason = KEYWORDS.get(keyword, "format")
    expected = allowed = None
    if keyword == "type":
        expected = _types(error.validator_value)
    elif keyword in ("enum", "const"):
        values = error.validator_value if keyword == "enum" else [error.validator_value]
        allowed = [v for v in values if v is None or isinstance(v, (bool, int)) or isinstance(v, str) and len(v) <= 64]
        allowed = allowed[:MAX_ALLOWED]
    elif keyword in EXPECTED:
        expected = EXPECTED[keyword].format(error.validator_value)
    if keyword == "minLength" and isinstance(error.instance, str) and not error.instance.strip():
        reason = "blank"
    yield path, reason, expected, allowed


def field_errors(schema, errors):
    """Collapse jsonschema errors into at most MAX_ENTRIES entries, one per field, in schema order."""
    found = {}
    for error in errors:
        for path, reason, expected, allowed in _entries(error, [str(p) for p in error.absolute_path]):
            field = ROOT
            for index, name in enumerate(path):
                joined = _join(path[:index], name)
                if joined is None:
                    break
                field = joined
            if field in found:
                continue
            found[field] = {
                "field": field,
                "reason": reason,
                **({"expected": expected} if expected else {}),
                **({"allowed": allowed} if allowed else {}),
            }
    order = {name: i for i, name in enumerate(schema.get("properties", {}))}
    ranked = sorted(found.values(), key=lambda e: order.get(e["field"].split("/")[0], len(order)))
    return ranked[:MAX_ENTRIES]
