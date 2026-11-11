"""`field_errors` for a request body the service's models reject (ANYM-984).

The entries are the gateway's shape and vocabulary, `{field, reason, expected?, allowed?}`: the field path and every
hint come from the reviewed model, never from the submitted value. An unknown key the caller sent is named only when
it is a short identifier. This service keeps its own lockfile, so the vocabulary is repeated here, not imported from
the gateway's `platform_contracts`.
"""
import re

MAX_ENTRIES, MAX_PATH, MAX_ALLOWED, MAX_PATTERN = 20, 128, 50, 80
ROOT = "$"  # the request object itself
SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}|[0-9]{1,4}")
TYPES = {"string": "string", "int": "integer", "bool": "boolean", "float": "number", "dict": "object",
         "list": "array", "model": "object"}
UNION_TAGS = frozenset(("str", "int", "bool", "float", "dict", "list"))
LIMITS = {  # pydantic error type -> (reason, ctx key, hint)
    "greater_than_equal": ("minimum", "ge", "at least {}"), "less_than_equal": ("maximum", "le", "at most {}"),
    "string_too_short": ("min_length", "min_length", "at least {} characters"),
    "string_too_long": ("max_length", "max_length", "at most {} characters"),
}


def field_path(loc):
    """The `/`-joined path of the model location, cut at the first part a field name cannot carry (`$` for none)."""
    field = ROOT
    for index, part in enumerate(loc):
        text = str(part)
        parts = [str(p) for p in loc[:index + 1]]
        if not SEGMENT.fullmatch(text) or len("/".join(parts)) > MAX_PATH:
            break
        field = "/".join(parts)
    return field


def entry(kind, context, properties, field):
    """`(reason, extras)` for one pydantic error type; `extras` holds `expected` or `allowed` from the model only."""
    if kind == "missing":
        return "required", {}
    if kind == "extra_forbidden":
        return "unknown_field", {}
    prefix = kind.split("_")[0]
    if kind.endswith(("_type", "_parsing")) and prefix in TYPES:
        return "type", {"expected": TYPES[prefix]}
    if kind == "literal_error":
        allowed = properties.get(field, {}).get("enum")
        scalar = isinstance(allowed, list) and 0 < len(allowed) <= MAX_ALLOWED and all(
            isinstance(v, (str, int, bool)) and not (isinstance(v, str) and len(v) > 64) for v in allowed)
        return "enum", {"allowed": list(allowed)} if scalar else {}
    if kind in LIMITS:
        reason, key, template = LIMITS[kind]
        value = (context or {}).get(key)
        return reason, {"expected": template.format(value)} if type(value) is int else {}
    if kind == "string_pattern_mismatch":
        pattern = (context or {}).get("pattern")
        if isinstance(pattern, str) and 0 < len(pattern) <= MAX_PATTERN:
            return "pattern", {"expected": "matching " + pattern}
        return "pattern", {}
    return "format", {}


def entries(error, model):
    """At most MAX_ENTRIES entries, one per field, from a pydantic `ValidationError` of `model`; never an input value."""
    properties = model.model_json_schema().get("properties", {})
    found, kinds = {}, {}
    for item in error.errors(include_url=False, include_context=True, include_input=False):
        loc = item["loc"]
        if item["type"].endswith(("_type", "_parsing")) and len(loc) > 1 and str(loc[-1]) in UNION_TAGS:
            loc = loc[:-1]  # the member a union (`str | int`) tried, not a field
        field = field_path(loc)
        kinds.setdefault(field, set()).add(item["type"])
        if field in found:
            continue
        reason, extras = entry(item["type"], item.get("ctx"), properties, field)
        found[field] = {"field": field, "reason": reason, **extras}
    for field, seen in kinds.items():
        if len(seen) > 1:  # a union (`str | int`) fails once per member: no single type to expect
            found[field].pop("expected", None)
    return list(found.values())[:MAX_ENTRIES]
