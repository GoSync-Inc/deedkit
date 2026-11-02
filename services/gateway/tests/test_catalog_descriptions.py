"""Tool descriptions stay short, and the list_chats rules are each stated once (ANYM-674)."""
from agent_gateway.catalog import CATALOG

CAP = 3000
LIST_CHATS = CATALOG["messages_list_chats"]
# One marker phrase per rule of the pre-ANYM-674 description (ordering, paging, the awaiting_reply and bot rules,
# group attention, filters, counts, waiting, roles, the compact-row legend and the next_step table).
RULE_MARKERS = (
    "recent first", "stable catalogue traversal", "channel @username", "source status and last ingest",
    "No provider read changes", "excluding known bots", "explicit group owner mention/reply",
    "heuristic, not obligation", "0 = no age limit", "clears after owner writes",
    "priority requires awaiting_reply=true", "scores owner_request", "awaiting_reply_priority_components",
    "chat_types=['private']", "snoozed_until", "Archive chat IDs", "Account-scoped provider sender IDs",
    "read observations only", "question punctuation", "excluded_likely_broadcasts_total",
    "excluded_promotional_total", "muted_total", "longest wait first", "default 24, 0-720",
    "unknown_role_rows", "null = unknown", "omit an unknown my_role", "an absent field means the value below",
    "rows without type omit nothing", "channel_or_self_chat", "no_observed_owner_attention",
    "last_archived_message_incoming", "head_freshness: stale", "provider_head_stale", "message_version_conflict",
    "ambiguous_attention_reply_order", "head_candidates_truncated", "-> reread_now",
    "-> wait_for_provider_cooldown", "-> check_provider_access", "-> wait_for_history",
    "any other code -> get_context", "known_conflicts", "retry_after_s", "catchup (WhatsApp)",
    "(maximum 100); continue with next_cursor",
)


def list_chats_text():
    parts = [LIST_CHATS["description"]]
    parts += [p.get("description", "") for p in LIST_CHATS["inputSchema"]["properties"].values()]
    return "\n".join(parts)


# spec: ANYM-674
def test_list_chats_description_is_at_most_3000_characters():
    assert len(LIST_CHATS["description"]) <= CAP


# spec: ANYM-674
def test_list_chats_description_and_parameters_lost_at_least_2000_characters():
    # Measured at dea3f543: description 5475 plus the one parameter description (only_questions) 151 characters.
    now = len(LIST_CHATS["description"]) + sum(len(p.get("description", "")) for p in LIST_CHATS["inputSchema"]["properties"].values())
    assert 5475 + 151 - now >= 2000, now


# spec: ANYM-674
def test_list_chats_keeps_every_rule_once():
    text = list_chats_text()
    counts = {marker: text.count(marker) for marker in RULE_MARKERS}
    assert {m: n for m, n in counts.items() if n != 1} == {}


# spec: ANYM-674
def test_no_tool_description_exceeds_3000_characters():
    assert {name: len(tool["description"]) for name, tool in CATALOG.items() if len(tool["description"]) > CAP} == {}
