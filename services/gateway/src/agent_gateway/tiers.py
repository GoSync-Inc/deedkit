"""The everyday tool layer and the `advanced` tier (ANYM-550).

The tier filters only an MCP route's tools/list over the caller's existing tool and action entitlements: it never
admits a tool the profile, scopes or actions exclude, and every entitled tool stays callable by name on any route.
"""

ADVANCED = "advanced"
CAPABILITIES = "platform_capabilities"

# The everyday layer: find, read, send, the risk-class messenger tools, calls and platform help; calendar joins with
# ANYM-553.
EVERYDAY = (
    CAPABILITIES,
    "messages_search",
    "messages_search_people",
    "platform_resolve_target",
    "messages_get_messages",
    "messages_get_context",
    "messages_get_file_text",
    "messages_send",
    "messages_change_message",
    "messages_manage_chat",
    "messages_change_contacts",
    "messages_read_contacts",
    "messages_get_messenger_operation",
    "calls_call",
    "calls_call_result",
)

# A low-level tool and its everyday twin, which does all of its job with the same or a narrower entitlement: the
# low-level tool leaves a route's default list only while its twin is on it. Every other tool stays until its twin
# lands. Not twins yet: messages_send lacks link_preview, allow_lossy and send_id receipts (send_message,
# preview_message, get_send_status); calls_call takes no existing brief_id (preflight_call, dial_call,
# prepare_external_call); calls_call_result omits status and record diagnostics (get_call_status, get_call_result).
REPLACED_BY = {
    # calls_call_result carries learning.report(call_id), the same object get_call_analysis returns.
    "calls_get_call_analysis": "calls_call_result",
}


def default_list(names):
    """The names an MCP route lists without `advanced`: all but the low-level tools whose twin is among them."""
    present = set(names)
    return [n for n in names if REPLACED_BY.get(n) not in present]
