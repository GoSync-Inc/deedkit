"""Agent tool rulebook v1 (ANYM-544): one result shape, one error shape and the shared key rules.

AGENT_API.md is the prose; this module is what tools and their tests validate against. A validator returns the value
unchanged or raises ValueError naming the first broken rule. Standard library only.
"""
import hashlib
import json
import re

from . import field_errors

VERSION = 1
STATES = ("previewed", "pending", "running", "succeeded", "failed", "unknown", "cancelled", "expired")
# `previewed` is final: a dry run enqueued nothing. `unknown` is not final: its outcome still needs an operation.
FINAL_STATES = frozenset(("previewed", "succeeded", "failed", "cancelled", "expired"))
MODULES = frozenset(("gateway", "messaging", "calendar", "calls"))
REQUEST_ID = re.compile(r"[a-zA-Z0-9_.:-]{8,128}")
# The `next` of a missing request_id names this value. It matches REQUEST_ID, so every tool refuses it explicitly.
PLACEHOLDER_REQUEST_ID = "REPLACE_WITH_YOUR_ID"
MAX_WAIT_S = 25
PREVIEW_TTL_S = 600
REPLAY_RETENTION_S = 7 * 86400
MAX_CANDIDATES = 10
CHILD_KEY_LENGTH = 64
ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
MAX_ID = 256
MAX_LABEL = 200
MAX_HINT = 500
CANDIDATE_KEYS = frozenset(("id", "label", "kind", "network", "account", "last_activity", "next"))

# Every current module status, mapped onto exactly one `state` (the status itself stays as `detail_state`).
# Each module's tests read its own status source and fail on a value missing here.
STATUS_STATES = {
    "messaging.send": {
        "queued": "pending", "dispatching": "running", "sent": "succeeded", "failed": "failed",
        "expired": "expired", "cancelled": "cancelled", "unknown": "unknown",
    },
    "messaging.operation": {
        "queued": "pending", "dispatching": "running", "succeeded": "succeeded", "failed": "failed",
        "expired": "expired", "cancelled": "cancelled", "unknown": "unknown",
    },
    # partial and needs_attention ask for reconciliation ("do not create again"), so they are never `failed`.
    "calendar.operation": {
        "queued": "pending", "running": "running", "verified": "succeeded", "conflict": "failed",
        "failed": "failed", "unknown": "unknown", "needs_attention": "unknown", "partial": "unknown",
    },
    "calls.call": {
        "waiting_for_browser": "pending", "dialing": "running", "ringing": "running", "connecting": "running",
        "active": "running", "finished": "succeeded", "failed": "failed", "interrupted": "failed",
    },
}
# A call's dial outcome is independent of its status; `unknown` overrides any status.
CALL_DIAL_STATES = frozenset((None, "requesting", "accepted", "rejected", "unknown"))


def state_of(kind, status, *, dial_state=None):
    """The rulebook `state` of one module status; an unmapped value raises instead of guessing."""
    states = STATUS_STATES.get(kind)
    if states is None or status not in states:
        raise ValueError("unmapped_status")
    if kind == "calls.call":
        if dial_state not in CALL_DIAL_STATES:
            raise ValueError("unmapped_status")
        if dial_state == "unknown":
            return "unknown"
    return states[status]


def is_request_id(value):
    """A caller key a tool may act on: the pattern, and never the placeholder of a `next`."""
    return isinstance(value, str) and bool(REQUEST_ID.fullmatch(value)) and value != PLACEHOLDER_REQUEST_ID


def child_key(principal, tool, parent_key, step, attempt):
    """The key of one step of a composite intent, stored under the internal key origin, never the caller's."""
    if type(attempt) is not int or attempt < 1 or not all(isinstance(v, str) and v for v in (principal, tool, parent_key, step)):
        raise ValueError("invalid_child_key")
    raw = json.dumps(["agent-child-v1", principal, tool, parent_key, step, attempt], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _text(value, limit, *, empty=False):
    return isinstance(value, str) and (empty or bool(value)) and len(value) <= limit


def next_call(tool, arguments):
    return validate_next({"tool": tool, "arguments": arguments})


def validate_next(value):
    if (not isinstance(value, dict) or set(value) != {"tool", "arguments"} or not isinstance(value["tool"], str)
            or not TOOL_NAME.fullmatch(value["tool"]) or not isinstance(value["arguments"], dict)):
        raise ValueError("invalid_next")
    return value


def _operation(value):
    if (not isinstance(value, dict) or set(value) != {"module", "id"} or value["module"] not in MODULES
            or not _text(value["id"], 128)):
        raise ValueError("invalid_operation")


def validate_result(value):
    """An `AgentResult`: top-level `state`, module status in `detail_state`, an operation while not final."""
    if not isinstance(value, dict) or value.get("state") not in STATES:
        raise ValueError("invalid_state")
    state = value["state"]
    if "detail_state" in value and not _text(value["detail_state"], 64):
        raise ValueError("invalid_detail_state")
    if "request_id" in value and value["request_id"] is not None and not is_request_id(value["request_id"]):
        raise ValueError("invalid_request_id")
    if state == "previewed":
        if "operation" in value or not _text(value.get("confirm_token"), 256) or "preview" not in value:
            raise ValueError("invalid_preview")
    elif state not in FINAL_STATES and "operation" not in value:
        raise ValueError("operation_required")
    if "operation" in value:
        _operation(value["operation"])
    if value.get("next") is not None:
        validate_next(value["next"])
    if "resumable" in value and (type(value["resumable"]) is not bool or state != "failed"):
        raise ValueError("invalid_resumable")
    return value


def validate_candidate(value):
    if not isinstance(value, dict) or not CANDIDATE_KEYS.issuperset(value):
        raise ValueError("invalid_candidate")
    if not (_text(value.get("id"), MAX_ID) and _text(value.get("label"), MAX_LABEL) and _text(value.get("kind"), 64)):
        raise ValueError("invalid_candidate")
    for key in ("network", "account", "last_activity"):
        if value.get(key) is not None and not _text(value[key], MAX_ID):
            raise ValueError("invalid_candidate")
    if value.get("next") is not None:
        validate_next(value["next"])
    return value


def validate_error(value, *, read_only=frozenset()):
    """An `AgentError`. `read_only` names the caller's read-only tools: only those may be a top-level `next`
    of an ambiguity (`selection_required`) or of a cut candidate list (`more`)."""
    if not isinstance(value, dict) or not isinstance(value.get("error"), str) or not ERROR_CODE.fullmatch(value["error"]):
        raise ValueError("invalid_error")
    if value.get("request_id") is not None and not (isinstance(value["request_id"], str)
                                                     and REQUEST_ID.fullmatch(value["request_id"])):
        raise ValueError("invalid_request_id")
    if "retryable" in value and type(value["retryable"]) is not bool:
        raise ValueError("invalid_retryable")
    if "field_errors" in value and field_errors.validated(value["field_errors"]) != value["field_errors"]:
        raise ValueError("invalid_field_errors")
    if "hint" in value and not _text(value["hint"], MAX_HINT):
        raise ValueError("invalid_hint")
    top = value.get("next")
    if top is not None:
        validate_next(top)
    candidates = value.get("candidates")
    if candidates is not None:
        if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
            raise ValueError("too_many_candidates")
        for candidate in candidates:
            validate_candidate(candidate)
    if "more" in value:
        if type(value["more"]) is not bool or candidates is None:
            raise ValueError("invalid_more")
        if value["more"] and (top is None or top["tool"] not in read_only):
            raise ValueError("more_needs_read_only_next")
    if value["error"] == "selection_required":
        if not candidates:
            raise ValueError("selection_needs_candidates")
        if top is not None and top["tool"] not in read_only:
            raise ValueError("mutating_next_on_selection")
    return value


def agent_result(state, *, detail_state=None, request_id=None, operation=None, next=None, **payload):
    value = {"state": state, **payload}
    for key, item in (("detail_state", detail_state), ("request_id", request_id), ("operation", operation)):
        if item is not None:
            value[key] = item
    value["next"] = next
    return validate_result(value)


def agent_error(code, *, read_only=frozenset(), **fields):
    return validate_error({"error": code, **fields}, read_only=read_only)


def missing_request_id(tool, arguments):
    """`invalid_arguments` for a live effect without a request_id; its `next` can never run verbatim."""
    return agent_error("invalid_arguments", retryable=False,
                       field_errors=[{"field": "request_id", "reason": "required"}],
                       hint="request_id is required for a live effect: choose one per intent and keep it on retries.",
                       next=next_call(tool, {**arguments, "request_id": PLACEHOLDER_REQUEST_ID}))
