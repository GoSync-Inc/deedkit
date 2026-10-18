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
    "minimum": "at least {}",
    "exclusiveMinimum": "more than {}",
    "maximum": "at most {}",
    "exclusiveMaximum": "less than {}",
    "minItems": "at least {} items",
    "maxItems": "at most {} items",
}


def _types(value):
    names = [value] if isinstance(value, str) else [v for v in value if isinstance(v, str)]
    return " or ".join(n for n in names if n != "null") or "null"


def _length(schema, keyword):
    low, high = schema.get("minLength"), schema.get("maxLength")
    if low is not None and high is not None:
        return f"{low}–{high} characters"
    return f"at least {low} character{'' if low == 1 else 's'}" if keyword == "minLength" else f"at most {high} characters"


def _union(error):
    """The failures of the one anyOf/oneOf branch that fits the value's type, else None."""
    branches = {}
    for sub in error.context:
        branches.setdefault(sub.relative_schema_path[0], []).append(sub)
    fitting = [subs for subs in branches.values()
               if not any(s.validator == "type" and not s.relative_path for s in subs)]
    return fitting[0] if len(fitting) == 1 else None


def _entries(error):
    """Yield (path, reason, expected, allowed) for one jsonschema error."""
    path = [str(p) for p in error.absolute_path]
    keyword = error.validator
    if keyword in ("anyOf", "oneOf"):
        subs = _union(error)
        if subs is not None:
            for sub in subs:
                yield from _entries(sub)
            return
        kinds = [s.validator_value for s in error.context if s.validator == "type" and not s.relative_path]
        flat = [k for kind in kinds for k in ([kind] if isinstance(kind, str) else kind)]
        yield path, ("type" if flat else "format"), _types(list(dict.fromkeys(flat))) if flat else None, None
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
    elif keyword in ("minLength", "maxLength"):
        expected = _length(error.schema, keyword)
    elif keyword in EXPECTED:
        expected = EXPECTED[keyword].format(error.validator_value)
    yield path, reason, expected, allowed


def _resolve(root, node):
    """Follow a local $ref, and for a union the first branch that has properties, items or a $ref."""
    for _ in range(8):
        if not isinstance(node, dict):
            return {}
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            target = root
            for part in ref[2:].split("/"):
                target = target.get(part, {}) if isinstance(target, dict) else {}
            node = target
            continue
        union = node.get("anyOf") or node.get("oneOf")
        if union and "properties" not in node and "items" not in node:
            node = next((b for b in union if isinstance(b, dict) and ("properties" in b or "items" in b or "$ref" in b)), {})
            continue
        return node
    return {}


def _rank(schema, field):
    """Sort key of a field path in the reviewed schema's own order (array indexes numerically, keys of a map by name)."""
    node, key = schema, []
    for segment in ([] if field == ROOT else field.split("/")):
        node = _resolve(schema, node)
        if segment.isdigit():
            key.append((int(segment), ""))
            node = node.get("items", {})
        else:
            props = list(node.get("properties", {}))
            key.append((props.index(segment), "") if segment in props else (len(props), segment))
            node = node.get("properties", {}).get(segment, node.get("additionalProperties", {}))
    return key


def field_errors(schema, errors):
    """Collapse jsonschema errors into at most MAX_ENTRIES entries, one per field, in schema order."""
    found = {}
    for error in errors:
        for path, reason, expected, allowed in _entries(error):
            field = ROOT
            for index, name in enumerate(path):
                if not SEGMENT.fullmatch(name) or len("/".join(path[:index + 1])) > MAX_PATH:
                    break
                field = "/".join(path[:index + 1])
            if field in found:
                continue
            found[field] = {
                "field": field,
                "reason": reason,
                **({"expected": expected} if expected else {}),
                **({"allowed": allowed} if allowed else {}),
            }
    return sorted(found.values(), key=lambda e: _rank(schema, e["field"]))[:MAX_ENTRIES]
