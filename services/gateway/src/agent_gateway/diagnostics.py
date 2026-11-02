"""Bounded protocol diagnostics; backend source text is never error guidance."""
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
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


SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z")
STARTED_AT = datetime.now(timezone.utc)
OPERATION_MISSING_HINT = ("A changed module build or manifest version is a signal to read tools/list and "
                          "platform_capabilities again, never proof that the operation exists.")
# The same hint for a key whose policy does not allow platform_capabilities: it never names a tool outside its list.
OPERATION_MISSING_HINT_WITHOUT_PLATFORM = ("A changed module build or manifest version is a signal to read tools/list "
                                           "again, never proof that the operation exists.")


def valid_timestamp(value):
    """A bounded ISO-8601 timestamp with an offset whose date, time and offset are real."""
    if not (isinstance(value, str) and len(value) <= 40 and TIMESTAMP.match(value)):
        return False
    offset = re.search(r"[+-]([0-9]{2}):([0-9]{2})\Z", value)
    if offset and (int(offset[1]) > 23 or int(offset[2]) > 59):  # fromisoformat accepts +00:99 on some Pythons
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return False
    return True


def gateway_build():
    """The gateway's own build by messaging's rule: the 40-hex DEEDKIT_SOURCE_SHA of the image, else null (ANYM-662)."""
    sha = os.environ.get("DEEDKIT_SOURCE_SHA", "")
    return {"source_sha": sha if SOURCE_SHA.match(sha) else None,
            "started_at": STARTED_AT.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}


def build_summary(capabilities):
    """A module's reported build, validated and bounded: `{source_sha, started_at}` with null for what is malformed,
    or None when the module reports none (an older build)."""
    build = capabilities.get("build") if isinstance(capabilities, dict) else None
    if not isinstance(build, dict):
        return None
    sha, started = build.get("source_sha"), build.get("started_at")
    return {"source_sha": sha if isinstance(sha, str) and SOURCE_SHA.match(sha) else None,
            "started_at": started if valid_timestamp(started) else None}


def manifest_lacks(tool, capabilities):
    """True when the tool needs its operation in the module's manifest and the manifest does not list it.

    Membership of the operation, not a version; a module with no (or a malformed) manifest lacks every operation.
    """
    if not tool.get("required_manifest"):
        return False
    manifest = capabilities.get("tool_manifest") if isinstance(capabilities, dict) else None
    names = manifest.get("names") if isinstance(manifest, dict) else None
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return True
    return tool["operation"] not in names


def module_identity(capabilities):
    """`(module_manifest, module_build)`: the manifest version and the build commit, each null when not reported."""
    summary = manifest_summary(capabilities) or {}
    build = build_summary(capabilities) or {}
    return summary.get("version"), build.get("source_sha")


def operation_missing_detail(tool, capabilities, platform_allowed):
    """The body of a call to a policy-allowed tool whose operation the module's build does not have (ANYM-662).

    The hint names platform_capabilities only when the re-authorized key's policy allows that tool.
    """
    manifest, build = module_identity(capabilities)
    return {"error": "operation_not_in_module_build", "module": tool["module"], "operation": tool["operation"],
            "module_manifest": manifest, "module_build": build, "next_action": "refresh_tools_after_release",
            "hint": OPERATION_MISSING_HINT if platform_allowed else OPERATION_MISSING_HINT_WITHOUT_PLATFORM}


def module_conditions(allowed, states, capabilities):
    """What the key's own policy-allowed tools can learn about their modules: `(unavailable, builds)`.

    `allowed` holds only tools the key's policy allows, so a module in which it has none never appears. Each module is
    handled alone and built from data `discover` already loaded; an exception there omits that module's entry and
    never fails a listing for healthy modules.
    """
    unavailable, builds = [], {}
    by_module = {}
    for name in allowed:
        if CATALOG[name]["module"] != "gateway":
            by_module.setdefault(CATALOG[name]["module"], []).append(name)
    for module in sorted(by_module):
        try:
            caps = capabilities.get(module) or {}
            if states.get(module) != "ready":
                unavailable.append({"module": module, "state": "unavailable", "reason": "backend_unavailable",
                                    "module_manifest": None, "module_build": None, "required_by": sorted(by_module[module])})
                continue
            builds[module] = build_summary(caps)
            lacking = sorted(n for n in by_module[module] if manifest_lacks(CATALOG[n], caps))
            if lacking:
                manifest, build = module_identity(caps)
                unavailable.append({"module": module, "state": "outdated", "reason": "operation_not_in_module_build",
                                    "module_manifest": manifest, "module_build": build, "required_by": lacking})
        except Exception:  # noqa: BLE001 - a diagnostic must never fail discovery for healthy modules
            logging.getLogger(__name__).warning("module_condition_failed module=%s", module)
    return unavailable, builds


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
    "register_delivery_endpoint", "list_delivery_endpoints", "update_delivery_endpoint", "delete_delivery_endpoint",
    "test_delivery_endpoint", "list_delivery_attempts",
})
CONSUMPTION_ERRORS = frozenset({
    "unauthorized", "forbidden", "not_found", "invalid_limit", "invalid_version", "invalid_receipt",
    "stale_ack", "idempotency_conflict", "invalid_parameters", "invalid_filter", "invalid_subscription",
    "subscription_access_changed", "subscription_closed", "subscription_conflict", "subscription_limit",
    "subscription_receipt_limit", "invalid_feedback", "feedback_limit", "invalid_receiver",
    "delivery_not_configured", "rate_limited", "invalid_request_id",
    "delivery_disabled", "delivery_policy_unset", "unsafe_endpoint", "invalid_signing_secret", "invalid_header",
    "reserved_header", "rotation_in_progress", "endpoint_limit", "endpoint_conflict", "operator_disabled",
    "invalid_endpoint_state", "endpoint_paused", "endpoint_auto_paused", "endpoint_test_failed", "invalid_limit",
})
# ANYM-519: the endpoint errors' bounded extras. Values are reviewed constants, counters or a header name
# (never a header value or secret); anything else is dropped.
ENDPOINT_STATES = frozenset({"enabled", "agent_paused", "auto_paused", "operator_disabled", "deleted"})
ENDPOINT_NEXT_STEPS = frozenset({
    "for a tailnet or private host ask the operator for the wake relay (ANYM-228)",
    "fix the receiver, then call test_delivery_endpoint with a new request_id",
    "ask the operator to enable the endpoint", "call update_delivery_endpoint with action resume",
    "fix the receiver, then call update_delivery_endpoint with action resume again",
})
# ANYM-520: a refused resume's test error, one of the backend's bounded codes (never response text).
TEST_ERROR = re.compile(r"[a-z_]{1,64}\Z")
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}\Z")


def endpoint_extras(value):
    result = {}
    retry = value.get("retry_after_s")
    if type(retry) is int and 0 < retry <= 86400:
        result["retry_after_s"] = retry
    if isinstance(value.get("state"), str) and value["state"] in ENDPOINT_STATES:
        result["state"] = value["state"]
    if isinstance(value.get("header"), str) and HEADER_NAME.match(value["header"]):
        result["header"] = value["header"]
    if isinstance(value.get("next_step"), str) and value["next_step"] in ENDPOINT_NEXT_STEPS:
        result["next_step"] = value["next_step"]
    if isinstance(value.get("test_error"), str) and TEST_ERROR.match(value["test_error"]):
        result["test_error"] = value["test_error"]
    if type(value.get("http_status")) is int and 100 <= value["http_status"] <= 599:
        result["http_status"] = value["http_status"]
    if type(value.get("version")) is int and 1 <= value["version"] <= 2**53 - 1:
        result["version"] = value["version"]
    return result


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

    An unknown field also says the tool list is likely stale, unless every unknown field is the singular or plural
    of a field the tool has (`account` for `accounts`): then `use_instead` names that field and the list is current.
    Any failure while building the hint returns `detail`.
    """
    try:
        properties = tool["inputSchema"].get("properties", {})
        entries = detail["field_errors"]
        unknown = [e["field"] for e in entries if e["reason"] == "unknown_field"
                   and (e["field"] == "$" or "/" not in e["field"] and e["field"] not in properties)]
        missing = any(e["reason"] == "required" and "/" not in e["field"] for e in entries)
        if not (unknown or missing):
            return detail
        hinted = {**detail, "tool_fields": tool_fields(tool)}
        renamed = {name: other for name in unknown for other in (name + "s", name[:-1] if name.endswith("s") else "")
                   if other and other in properties}
        if renamed and len(renamed) == len(unknown):
            hinted["use_instead"] = renamed
        elif unknown:
            hinted.update(tool_list_stale="likely", owner_hint=STALE_OWNER_HINT)
        return hinted
    except Exception:  # noqa: BLE001 - a hint must never replace the original error
        return detail


def unknown_tool_denial(capabilities=False):
    """The admission denial of a tool this key cannot call, with the same once-only refresh hint for every cause.

    `next` (ANYM-544) is platform_capabilities when the key may call it; it never depends on the name asked for.
    """
    result = {"error": "not_found_or_forbidden", "next_action": "refresh_tool_list_once",
              "hint": "If the tool is still absent after a refresh, this key does not have it; do not ask again."}
    if capabilities:
        result["next"] = {"tool": "platform_capabilities", "arguments": {}}
    return result


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
        if operation.endswith("_delivery_endpoint") or operation in ("list_delivery_endpoints", "list_delivery_attempts",
                                                                     "configure_subscription_delivery"):
            result.update(endpoint_extras(value))
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
