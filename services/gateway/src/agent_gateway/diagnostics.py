"""Bounded protocol diagnostics; backend source text is never error guidance."""
import hashlib
import json
import logging
import re
from uuid import uuid4

from platform_contracts.field_errors import MATCHING, validated

from .catalog import CATALOG, public


NEXT_DISCOVERY_ACTION = "refresh_connection_if_tools_list_differs"
BACKEND_ERROR_CODES = frozenset({
    "query_timeout", "service_busy", "service_unavailable", "temporarily_unavailable", "subscription_wait_timeout",
})
REQUEST_ID = re.compile(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z")


CATALOG_DIGITS = 12
STALE_OWNER_HINT = "Your DeedKit tool list may be out of date; reconnect the DeedKit MCP client."
MAX_FIELD_NAMES = 64
MAX_FIELD_VALUES = 8


def digest(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def discovery(visible, actions, revision):
    """Fingerprint the effective reviewed schemas, including narrowed action enums."""
    tools_sha256 = digest([public(CATALOG[name], actions) for name in sorted(visible)])
    return {
        "tool_count": len(visible),
        "tools_sha256": tools_sha256,
        # The digest is taken before the "Catalog:" sentence exists, so a host can compare it with its cached list.
        "catalog": tools_sha256[:CATALOG_DIGITS],
        "policy_revision": revision,
        "notifications_supported": False,
        "next_action": NEXT_DISCOVERY_ACTION,
    }


def manifest_summary(capabilities):
    """Keep catalog provenance without advertising unselected backend operations."""
    manifest = capabilities.get("tool_manifest")
    if not isinstance(manifest, dict):
        return None
    result = {}
    version = manifest.get("version")
    if isinstance(version, str) and re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", version):
        result["version"] = version
    names = manifest.get("names")
    if (isinstance(names, list) and len(names) <= 10000
            and all(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_]{1,128}", name) for name in names)):
        names = sorted(set(names))
        result.update(tool_count=len(names), names_sha256=digest(names))
    if manifest.get("discovery_action") == NEXT_DISCOVERY_ACTION:
        result["discovery_action"] = NEXT_DISCOVERY_ACTION
    return result or None


def failure_details(status, value, header_request_id=None):
    result = {"error": "backend_unavailable", "backend_status": status}
    value = value if isinstance(value, dict) else {}
    code = value.get("error")
    if isinstance(code, str) and code in BACKEND_ERROR_CODES:
        result["backend_error"] = code
    # Messaging issues a random hex UUID. Accept UUID encodings only, never an
    # arbitrary error body/header that happens to be labelled a request ID.
    for request_id in (value.get("request_id"), header_request_id):
        if isinstance(request_id, str) and REQUEST_ID.fullmatch(request_id):
            result["request_id"] = request_id
            break
    return result


CONSUMPTION_OPERATIONS = frozenset({
    "fetch_subscription_events", "ack_subscription_events", "wait_for_subscription_events",
    "configure_subscription_delivery", "test_subscription", "submit_feedback", "list_feedback",
})
CONSUMPTION_ERRORS = frozenset({
    "unauthorized", "forbidden", "not_found", "invalid_limit", "invalid_version", "invalid_receipt",
    "stale_ack", "idempotency_conflict", "invalid_parameters", "invalid_filter", "invalid_subscription",
    "subscription_access_changed", "subscription_closed", "subscription_conflict", "subscription_limit",
    "subscription_receipt_limit", "invalid_feedback", "feedback_limit", "invalid_receiver",
    "delivery_not_configured", "rate_limited", "invalid_request_id",
})


def _node(schema, node):
    """Follow a local $ref and, for a nullable union, its first non-null branch."""
    for _ in range(8):
        if not isinstance(node, dict):
            return {}
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            node = schema
            for part in ref[2:].split("/"):
                node = node.get(part, {}) if isinstance(node, dict) else {}
        elif isinstance(node.get("anyOf"), list):
            node = next((b for b in node["anyOf"] if isinstance(b, dict) and b.get("type") != "null"), {})
        else:
            return node
    return {}


def declared(schema, path):
    """The reviewed schema node of a field path, or None when the schema does not declare it."""
    node = _node(schema, schema)
    for segment in path.split("/"):
        if segment.isdigit() and "items" in node:
            node = _node(schema, node["items"])
        elif segment in node.get("properties", {}):
            node = _node(schema, node["properties"][segment])
        elif segment in node.get("propertyNames", {}).get("enum", ()):
            node = _node(schema, node.get("additionalProperties", {}))
        else:
            return None
    return node


def reviewed_field_errors(operation, value):
    """A backend `field_errors` list that is well formed and names only what the reviewed tool schema declares.

    Fields must be declared paths (or `arguments`, the whole input), and `allowed` values must come from that
    schema's own enum or its declared keys; one violation drops the whole list.
    """
    entries = validated(value)
    tool = CATALOG.get("messages_" + operation)
    if not entries or tool is None:
        return None
    schema = tool["inputSchema"]
    for entry in entries:
        if entry["field"] == "arguments" and "arguments" not in schema.get("properties", {}):
            node = _node(schema, schema)
        else:
            node = declared(schema, entry["field"])
        if node is None:
            return None
        expected = entry.get("expected", "")
        if expected.startswith(MATCHING) and expected[len(MATCHING):] != node.get("pattern"):
            return None
        if "allowed" in entry:
            permitted = [*node.get("enum", ()), *([node["const"]] if "const" in node else ()),
                         *node.get("propertyNames", {}).get("enum", ()),
                         *(node.get("properties", {}) if entry["reason"] == "unknown_field" else ())]
            if any(not any(v == p and type(v) is type(p) for p in permitted) for v in entry["allowed"]):
                return None
    return entries


def _field_type(schema, node):
    kind = _node(schema, node).get("type")
    return kind if isinstance(kind, str) and kind else "any"


def _field_values(schema, node):
    values = _node(schema, node).get("enum")
    if (isinstance(values, list) and 0 < len(values) <= MAX_FIELD_VALUES
            and all(isinstance(v, (str, int)) and not isinstance(v, bool) and len(str(v)) <= 64 for v in values)):
        return list(values)
    return None


def tool_fields(tool):
    """The top-level fields of a tool's packaged input schema: required with type (and short enums), then optional names."""
    schema = tool["inputSchema"]
    properties = schema.get("properties", {})
    required = [name for name in schema.get("required", []) if name in properties][:MAX_FIELD_NAMES]
    names = [name for name in properties if name not in required][:MAX_FIELD_NAMES - len(required)]
    fields = []
    for name in required:
        values = _field_values(schema, properties[name])
        fields.append({"name": name, "type": _field_type(schema, properties[name]),
                       **({"values": values} if values else {})})
    return {"required": fields, "optional": names}


def stale_arguments_hint(tool, detail):
    """An `invalid_arguments` body plus the tool's current fields when it names a missing or unknown top-level field.

    An unknown field also says the tool list is likely stale. Any failure while building the hint returns `detail`.
    """
    try:
        properties = tool["inputSchema"].get("properties", {})
        entries = detail["field_errors"]
        unknown = any(e["reason"] == "unknown_field"
                      and (e["field"] == "$" or "/" not in e["field"] and e["field"] not in properties)
                      for e in entries)
        missing = any(e["reason"] == "required" and "/" not in e["field"] for e in entries)
        if not (unknown or missing):
            return detail
        hinted = {**detail, "tool_fields": tool_fields(tool)}
        if unknown:
            hinted.update(tool_list_stale="likely", owner_hint=STALE_OWNER_HINT)
        return hinted
    except Exception:  # noqa: BLE001 - a hint must never replace the original error
        return detail


def unknown_tool_denial():
    """The admission denial of a tool this key cannot call, with the same once-only refresh hint for every cause."""
    return {"error": "not_found_or_forbidden", "next_action": "refresh_tool_list_once",
            "hint": "If the tool is still absent after a refresh, this key does not have it; do not ask again."}


def catalog_note(found):
    return " Catalog: " + found["catalog"]


def with_catalog(tool, found):
    """The `platform_capabilities` listing with the key's catalog value appended; any other tool, or a failure, is unchanged."""
    if tool["name"] != "platform_capabilities":
        return tool
    try:
        return {**tool, "description": tool["description"] + catalog_note(found)}
    except Exception:  # noqa: BLE001 - the listing must stay valid without the hint
        return tool


def consumption_failure(status, value, header_request_id=None, operation=None):
    result = failure_details(status, value, header_request_id)
    result.pop("backend_error", None)
    code = value.get("error")
    result["error"] = code if isinstance(code, str) and code in CONSUMPTION_ERRORS else "backend_denied"
    # The one backend detail relayed: a list that passes the shared shape, hint set and the tool's reviewed schema.
    if result["error"] != "backend_denied" and isinstance(operation, str):
        field_errors = reviewed_field_errors(operation, value.get("field_errors"))
        if field_errors:
            result["field_errors"] = field_errors
    return result


# Bounded guidance for read timeouts; codes are stable, hints are short prose.
QUERY_HINTS = {
    "list_chats": ("narrow_accounts_chat_types_or_awaiting_reply_days",
                   "Narrow with accounts, chat_types or a smaller awaiting_reply_days, then retry once."),
    "search": ("narrow_account_chat_or_date_filters",
               "Narrow with accounts, chats or from_time/to_time, then retry once."),
    "search_files": ("narrow_account_chat_or_date_filters",
                     "Narrow with accounts, chats or from_time/to_time, then retry once."),
    "search_people": ("narrow_query_or_accounts", "Use a longer query or select accounts, then retry once."),
    "get_messages": ("reduce_limit_or_add_date_range", "Use a smaller limit or a from_time/to_time range, then retry once."),
}
FAILURE_FIELDS = ("backend_error", "backend_status", "request_id", "reason")


def unavailable(operation, detail, *, phase, read_only):
    """Correlate an unavailable backend and say whether retrying is safe.

    A failed capability check happens before the operation is sent, so even a
    mutation was not attempted. A failed mutation keeps its unknown outcome.
    """
    detail = detail if isinstance(detail, dict) else {}
    result = {"error": "backend_unavailable", **{k: detail[k] for k in FAILURE_FIELDS if k in detail},
              "phase": phase}
    gateway_request_id = uuid4().hex
    if "request_id" not in result:
        # Nothing reached a backend log; this ID matches the gateway log line.
        result["gateway_request_id"] = gateway_request_id
    timeout = result.get("backend_error") == "query_timeout" or result.get("reason") == "gateway_timeout"
    action, hint = QUERY_HINTS.get(operation, ("retry_after_delay", None)) if timeout else ("retry_after_delay", None)
    if phase == "capabilities":
        result["operation_sent"] = False
    result.update(retryable=True, retry_after_seconds=2, next_action=action)
    if hint:
        result["hint"] = hint
    logging.getLogger("agent_gateway").warning(
        "backend_unavailable operation=%s phase=%s read_only=%s gateway_request_id=%s request_id=%s reason=%s",
        operation, phase, read_only, gateway_request_id, result.get("request_id"),
        result.get("backend_error") or result.get("reason"))
    return result
