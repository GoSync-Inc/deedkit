"""`messages_capabilities` views (ANYM-270): the gateway's own payload as a summary, named sections or in full."""

from .catalog import CATALOG
from .http_clients import WAIT_POOL
from .policy import Denied

# Same section names as the messaging backend; each holds the keys of the gateway payload
# that replace or keep the backend's block. A key in no section appears only in `full`.
SECTIONS = {
    "identity": ("service", "version", "build", "principal", "scope_version", "credential_id", "expires_at",
                 "key_permission_version", "chat_scope", "auth_mode", "gateway_principal",
                 "backend_principal", "gateway_policy_revision"),
    "accounts": ("accounts",),
    "coverage": ("synthetic", "sources", "coverage"),
    "tool_manifest": ("gateway_tools", "backend_tool_manifest"),
    "search_options": ("search_options",),
    "sending": ("sending",),
    "file_uploads": ("file_uploads",),
    "subscriptions": ("subscriptions", "events"),
    "feedback": ("feedback",),
    "messenger_operations": ("gateway_actions", "gateway_actions_scope", "gateway_actions_by_account",
                             "gateway_actions_excluded_by_account", "gateway_actions_basis"),
    "limits": ("limits", "unavailable"),
}

# ANYM-528: any spelling of a messaging call (backend operation or public name) -> its public gateway name.
MESSAGING_CALLS = {spelling: name for name, tool in CATALOG.items() if tool["module"] == "messaging"
                   for spelling in (tool["operation"], name)}
HIDDEN = object()
# Fields whose value is a call: kept only when it is one of the caller's listed tools.
EVENT_CALL_FIELDS = ("fetch", "ack", "wait", "test")


def named_calls(value, shown):
    """`value` with each string naming a messaging call as its public name; a call `shown` lacks is HIDDEN
    and dropped from the enclosing object or list."""
    if isinstance(value, str):
        name = MESSAGING_CALLS.get(value)
        return value if name is None else name if name in shown else HIDDEN
    if isinstance(value, dict):
        return {k: v for k, item in value.items() if (v := named_calls(item, shown)) is not HIDDEN}
    if isinstance(value, list):
        return [v for item in value if (v := named_calls(item, shown)) is not HIDDEN]
    return value


def transports(rows, shown):
    """ANYM-518: a delivery transport is listed only when the caller's tool list has every tool of it;
    long polling also shows the gateway's wait pool."""
    kept = []
    for row in rows if isinstance(rows, list) else ():
        tools = row.get("tools") if isinstance(row, dict) else None
        if (isinstance(tools, list) and tools
                and all(isinstance(t, str) and MESSAGING_CALLS.get(t) in shown for t in tools)):
            kept.append(row | {"wait_pool": WAIT_POOL} if row.get("name") == "long_poll" else row)
    return kept


def subscription_blocks(result, visible):
    """The `subscriptions` and `events` blocks name only calls in the caller's tool list (ANYM-528).

    A hidden call is omitted, `subscriptions.ack` becomes false, `wait_max_seconds` leaves with the wait
    call (or an omitted `events.wait`), `delivery_policy` with its tool and a transport with any of its
    tools. A call field naming no listed tool (an unknown operation or a value of another type) is omitted
    too. A block that is not an object is dropped, never passed on.
    """
    shown = set(visible)
    for key in ("subscriptions", "events"):
        if key not in result:
            continue
        if not isinstance(result[key], dict):
            del result[key]
            continue
        block = result[key]
        if key == "subscriptions" and "transports" in block:
            block = {**block, "transports": transports(block["transports"], shown)}
        block = named_calls(block, shown)
        if "messages_wait_for_subscription_events" not in shown:
            block.pop("wait_max_seconds", None)
        if key == "events":
            for field in EVENT_CALL_FIELDS:
                if field in block and not (isinstance(block[field], str) and block[field] in shown):
                    del block[field]
            if "wait" not in block:
                block.pop("wait_max_seconds", None)
        if key == "subscriptions":
            if "ack" in block:
                block["ack"] = block["ack"] is True and "messages_ack_subscription_events" in shown
            policy = block.get("delivery_policy")
            if not isinstance(policy, dict) or not isinstance(policy.get("tool"), str) or policy["tool"] not in shown:
                block.pop("delivery_policy", None)
        result[key] = block
    return result


def capabilities_view(result, args):
    """`detail=full` is the payload unchanged; `sections` only those blocks; otherwise the summary."""
    sections = args.get("sections")
    if sections is not None:
        if not sections or any(name not in SECTIONS for name in sections):
            raise Denied("invalid_filter", 400)
        return {key: result[key] for name in SECTIONS if name in sections
                for key in SECTIONS[name] if key in result}
    if args.get("detail", "summary") == "full":
        return result
    scope = result.get("chat_scope") if isinstance(result.get("chat_scope"), dict) else {}
    chats = scope.get("chat_ids")
    accounts = result.get("accounts") if isinstance(result.get("accounts"), list) else []
    # ANYM-592: the backend's `build` (source commit, start time) stays in the default view, unchanged.
    summary = {key: result[key] for key in ("service", "version", "build", "gateway_principal",
                                            "backend_principal", "credential_id", "expires_at") if key in result}
    # ANYM-470: the backend key's expiry and each owner's ID stay in the default view.
    summary.update(
        accounts=[{k: a.get(k) for k in ("id", "platform", "label")}
                  | ({"self_provider_id": a["self_provider_id"]} if "self_provider_id" in a else {})
                  for a in accounts if isinstance(a, dict)],
        chat_scope={"all_in_permitted_accounts": scope.get("all_in_permitted_accounts"),
                    "chat_count": len(chats) if isinstance(chats, list) else None},
        gateway_tool_count=len(result.get("gateway_tools") or []),
    )
    manifest = result.get("backend_tool_manifest")
    if isinstance(manifest, dict) and "version" in manifest:
        summary["tool_manifest"] = {"version": manifest["version"]}
    summary.update(detail="summary", sections=list(SECTIONS),
                   next_action="call messages_capabilities again with sections=[names] for those blocks "
                               "or detail=full for everything")
    return summary
