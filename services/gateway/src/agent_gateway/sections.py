"""`messages_capabilities` views (ANYM-270): the gateway's own payload as a summary, named sections or in full."""

from .policy import Denied

# Same section names as the messaging backend; each holds the keys of the gateway payload
# that replace or keep the backend's block. A key in no section appears only in `full`.
SECTIONS = {
    "identity": ("service", "version", "principal", "scope_version", "credential_id", "expires_at",
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
    summary = {key: result[key] for key in ("service", "version", "gateway_principal", "backend_principal",
                                            "credential_id", "expires_at") if key in result}
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
