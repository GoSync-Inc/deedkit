import base64
import binascii
import hashlib
import re
from platform_contracts.agent import MAX_CANDIDATES, PLACEHOLDER_REQUEST_ID, validate_candidate
from .adapters.calls import receipt_reference

from .catalog import routed
from .diagnostics import manifest_summary
from .policy import Denied
from .sections import capabilities_view, subscription_blocks
from .catalog import CATALOG

CALENDAR_CONSTRAINTS = {
    "event_write",
    "time_format",
    "occurrence_write",
    "all_day_write",
    "provider_write",
    "recurrence_write",
    "future_split",
    "series_write",
    "invitations",
    "refresh",
    "window_limit_days",
    "calendar_limit",
    "event_limit",
    "cache_max_age_seconds",
    "refresh_wait_seconds",
    "plan_action_limit",
    "plan_step_limit",
    "plan_ttl_seconds",
    "atomic_cross_provider",
    "availability_sources",
}


# A request key that names something the caller already chose (an ID, an account, a mode); any other string the
# caller sent is payload: message text, file names, phone numbers, queries.
REFERENCE_KEY = re.compile(r"(?:^|_)(?:ids?|digest|ref|token)$|^(?:account|chat|person|sender|receipt|operation|"
                           r"consumer|reply_to|network|kind|view|detail|direction|order|format|status|live|modules|tool|"
                           r"purpose|calendar)$")
# platform_resolve_target's `to` (ANYM-545) is a reference only when it holds an exact archive ID or person UUID.
EXACT_TO = re.compile(r"(?:chat|msg)_[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# At least 7 digits, at most 3 separators between two digits, so "555 - 123 - 4567" and "(555)  123  4567" count.
PHONE = re.compile(r"\+?\d(?:[\s().-]{0,3}\d){6,}")
# A name, a dot and an extension of 1-10 letters or digits with at least one letter (.c, .7z, .sqlite3).
FILE_NAME = re.compile(r"[\w-]+\.(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{1,10}\b")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?")
# The fields a whole timestamp of the caller may stand in without counting as payload (caller_payload).
TIME_KEY = re.compile(r"start|end|date")
MIN_PAYLOAD, MIN_TOKEN = 4, 6


def payload_strings(value, key=None):
    """The non-empty strings of `value` that are not under a reference key, and their words of at least MIN_TOKEN
    characters, so an echoed fragment (a file name inside the text) counts too."""
    if isinstance(value, dict):
        return {s for k, v in value.items() for s in payload_strings(v, k)}
    if isinstance(value, list):
        return {s for v in value for s in payload_strings(v, key)}
    if isinstance(value, str) and value.strip() and not (key and REFERENCE_KEY.search(key)):
        return {value} | {word for word in value.split() if len(word) >= MIN_TOKEN}
    return set()


def branches(schema, defs):
    """The object schemas `schema` stands for: itself, its anyOf/oneOf/allOf members and local $defs references."""
    if not isinstance(schema, dict):
        return []
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        return branches(defs.get(ref.removeprefix("#/$defs/")), defs)
    nested = [b for k in ("anyOf", "oneOf", "allOf") if isinstance(schema.get(k), list) for s in schema[k]
              for b in branches(s, defs)]
    return [schema, *nested]


def fixed(value, schema, key, defs):
    """A caller string its own field fixes (ANYM-552): one of that field's enum or const words, or a whole
    timestamp in a declared time field (TIME_KEY). The same words in any other field stay payload."""
    if schema is None:
        return False
    for branch in branches(schema, defs):
        enum = branch.get("enum")
        if value == branch.get("const") or (isinstance(enum, list) and value in enum):
            return True
    return bool(key and TIME_KEY.fullmatch(key) and TIMESTAMP.fullmatch(value))


def caller_payload(value, schema, key=None, defs=None):
    """payload_strings, walking the tool's input schema beside the values: a continuation that echoes the
    caller's action ("cancel") or start time carries no payload; free text, titles and queries still do."""
    if defs is None:
        defs = schema.get("$defs", {}) if isinstance(schema, dict) else {}
    if isinstance(value, dict):
        fields = {}
        for branch in branches(schema, defs):
            fields.update(branch.get("properties") if isinstance(branch.get("properties"), dict) else {})
        return {s for k, v in value.items() for s in caller_payload(v, fields.get(k), k, defs)}
    if isinstance(value, list):
        items = [b["items"] for b in branches(schema, defs) if isinstance(b.get("items"), dict)]
        return {s for v in value for s in caller_payload(v, items[0] if items else None, key, defs)}
    if isinstance(value, str) and fixed(value, schema, key, defs):
        return set()
    return payload_strings(value, key)


def sensitive(text):
    """A phone-shaped number or a file name, whoever wrote it: never relayed in backend guidance."""
    return bool(PHONE.search(text) or FILE_NAME.search(text))


def exposed(value, key=None):
    """The strings of `value` outside a reference key; dict keys are field names, not values."""
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in exposed(v, k)]
    if isinstance(value, list):
        return [s for v in value for s in exposed(v, key)]
    if key == "to" and isinstance(value, str) and EXACT_TO.fullmatch(value):
        return []
    return [value] if isinstance(value, str) and not (key and REFERENCE_KEY.search(key)) else []


NAMED_WINDOWS = frozenset({"today", "tomorrow", "this_week", "next_week"})


def resolver_query(name, values):
    """platform_resolve_target's `to` (ANYM-545) is the name to look up: its candidates' labels and their read-only
    listing repeat it by design. Only that request's `to` stops being payload; phones and file names in the relayed
    error are still dropped (`sensitive`), and a `to` in any relayed `next` is still checked."""
    if name == "platform_resolve_target" and isinstance(values, dict):
        return {key: value for key, value in values.items() if key != "to"}
    return values


def candidate_query(name, values, candidate):
    """The payload one relayed candidate is checked against. calendar_find_time's `people` (ANYM-553) are names too,
    and a candidate's label and read-only retry repeat them and the `window`: for that candidate an entry stops being
    payload only as a member ID or as part of its own label, and the `window` only as a named window or two
    timestamps. Any other entry (an unknown trailing name, free text in `from`) and the `title` stay payload; another
    candidate's label exempts nothing here, and the hint and top-level next keep the whole payload."""
    if name == "calendar_find_time" and isinstance(values, dict):
        label = candidate.get("label") if isinstance(candidate, dict) else None
        label = " ".join(label.split()).casefold() if isinstance(label, str) else ""

        def member(entry):
            if not isinstance(entry, str):
                return False
            wanted = " ".join(entry.split()).casefold()
            return bool(EXACT_TO.fullmatch(entry)) or bool(wanted and label) and wanted in label

        window = values.get("window")
        checked = (isinstance(window, str) and window in NAMED_WINDOWS) or (
            isinstance(window, dict) and set(window) == {"from", "to"}
            and all(isinstance(v, str) and TIMESTAMP.fullmatch(v) for v in window.values()))
        rest = {key: value for key, value in values.items() if key not in ("people", "window")}
        people = values.get("people")
        kept = [entry for entry in people if not member(entry)] if isinstance(people, list) else people
        if kept:
            rest["people"] = kept
        if not checked and "window" in values:
            rest["window"] = window
        return rest
    return values


INLINE_UPLOAD_FIELDS = frozenset({"account", "chat_id", "filename", "mime", "bytes", "sha256", "request_id"})


def inline_upload_next(name, values, detail):
    """ANYM-547: messages_send over its inline cap names the upload call that takes that file instead. Its file
    fields are the caller's payload, so the relay keeps that `next` only when they are one inline file of this very
    call, recomputed here from its bytes; account and chat_id are references and request_id is the placeholder."""
    step = detail.get("next") if isinstance(detail, dict) else None
    args = step.get("arguments") if isinstance(step, dict) else None
    if (name != "messages_send" or not isinstance(args, dict) or step.get("tool") != "messages_prepare_file_upload"
            or set(args) != INLINE_UPLOAD_FIELDS or args["request_id"] != PLACEHOLDER_REQUEST_ID):
        return None
    files = values.get("files") if isinstance(values, dict) else None
    for item in files if isinstance(files, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("base64"), str):
            continue
        try:
            raw = base64.b64decode(item["base64"], validate=True)
        except (ValueError, binascii.Error):
            continue
        if (args["filename"], args["mime"], args["bytes"], args["sha256"]) == (
                item.get("name"), item.get("mime"), len(raw), hashlib.sha256(raw).hexdigest()):
            return step
    return None


# ANYM-519: write-only arguments of the endpoint tools; no gateway response repeats them.
WRITE_ONLY_ARGUMENTS = frozenset({"signing_secret", "new_secret", "headers"})


def found(piece, text):
    """A piece of payload in `text`: a substring, or a whole word when shorter than MIN_PAYLOAD."""
    if len(piece) >= MIN_PAYLOAD:
        return piece in text
    return re.search(rf"(?<!\w){re.escape(piece)}(?!\w)", text) is not None


def carries(value, payload):
    """The caller's payload, a phone number or a file name in any string of `value` outside a reference key."""
    # A whole timestamp (a schedule's start) is no phone number, though its digits match PHONE (ANYM-552).
    return any((sensitive(s) and not TIMESTAMP.fullmatch(s)) or any(found(p, s) for p in payload)
               for s in exposed(value))


# ANYM-660: constants of the messaging backend's read refusals. A hint that equals one of these exactly cannot carry the
# caller's payload, so it survives even when a query word happens to occur in it; any other hint is still checked.
REVIEWED_HINTS = frozenset({
    "omit sections to get all sections",
    "omit my_roles for all roles",
    "swap the bounds or widen the range; the range is half-open and must not be empty",
})


def relayed_error(detail, runnable, read_only, payload, candidate_payload=None):
    # candidate_payload(candidate): the payload that candidate is checked against (default: the whole payload).
    """A backend error as the gateway relays it (ANYM-544). Every `next` (top level and per candidate) must run as
    given for this caller and route; a `hint`, `next` or candidate with the caller's payload, a phone number or a
    file name outside an ID or reference field is dropped. `selection_required` keeps a mutating continuation on
    candidates only."""
    detail = dict(detail)
    if "hint" in detail and (not isinstance(detail["hint"], str)
                             or detail["hint"] not in REVIEWED_HINTS and carries(detail["hint"], payload)):
        detail.pop("hint")

    def safe(value, payload=payload):
        return runnable(value) and not carries(value, payload)

    candidate_payload = candidate_payload or (lambda candidate: payload)

    if "next" in detail and not (safe(detail["next"]) and (detail.get("error") != "selection_required"
                                                           or detail["next"]["tool"] in read_only)):
        detail.pop("next")
    if "candidates" in detail:
        kept = []
        for candidate in detail["candidates"] if isinstance(detail["candidates"], list) else []:
            own = candidate_payload(candidate)
            if isinstance(candidate, dict) and "next" in candidate and not safe(candidate["next"], own):
                candidate = {k: v for k, v in candidate.items() if k != "next"}
            if (isinstance(candidate, dict) and "last_activity" in candidate
                    and not (isinstance(candidate["last_activity"], str)
                             and TIMESTAMP.fullmatch(candidate["last_activity"]))):
                candidate = {k: v for k, v in candidate.items() if k != "last_activity"}
            try:
                validate_candidate(candidate)
            except ValueError:
                continue
            if not carries({k: v for k, v in candidate.items() if k not in ("next", "last_activity")}, own):
                kept.append(candidate)
        more = len(kept) > MAX_CANDIDATES or detail.get("more") is True
        detail["candidates"] = kept[:MAX_CANDIDATES]
        detail.pop("more", None)
        if more and "next" in detail and detail["next"]["tool"] in read_only:
            detail["more"] = True
    return detail


def shape_result(gateway, result, *, tool, module, snapshot, agent, caps, current, principal, allowed_actions,
                 rules_ceiling, live_denied, live_requested, key, credential, args, started):
    """The per-module tail of `Dispatch.call`: what a backend result becomes before the caller sees it. A pure
    move of the code that ran inline; `gateway` is the Dispatch that called it."""
    if module == "messaging":
        result = gateway.next_call(result, snapshot, agent, caps)
    if live_denied and isinstance(result, dict):
        # detail=transcript moves live and suggested_call from result_evidence to the top level.
        evidence = result["result_evidence"] if isinstance(result.get("result_evidence"), dict) else result
        if (evidence.get("suggested_call") or {}).get("tool") == "messages_search":
            evidence.pop("suggested_call")
        if live_requested:
            evidence["live"] = [{"state": "not_permitted", "reason": "gateway_action"}]
    if module == "calls":
        receipt_reference(tool["operation"], result)
    if (module == "calendar" and tool["operation"] == "plan" and not rules_ceiling and isinstance(result, dict)
            and ("projections_skipped" in result
                 or any(isinstance(st, dict) and "link" in st for st in result.get("steps") or []))):
        # A lesson change the backend carried to its drives shows rule IDs past this gateway's ceiling;
        # the stored plan is never applied through it and expires.
        raise Denied()
    if module == "calendar" and tool["operation"] in {"calendars", "capabilities"}:
        result = dict(result)
        if "constraints" in result:
            constraints = result["constraints"]
            result["constraints"] = (
                {field: value for field, value in constraints.items() if field in CALENDAR_CONSTRAINTS}
                if isinstance(constraints, dict)
                else {}
            )
        selected = set(gateway.policy.visible(current, principal))
        scopes = set(principal["grants"].get("calendar", [])) & set(caps["permissions"])
        result["calendars"] = [
            dict(
                row,
                allowed_event_actions=[
                    action
                    for action in row.get("allowed_event_actions", [])
                    if action in {"create", "update", "delete"}
                    and {"calendar_plan", "calendar_apply"} <= selected
                    and {"plan:create", "plan:execute", "event:" + action} <= scopes
                ],
            )
            for row in result.get("calendars", [])
        ]
    if module == "calendar" and tool["operation"] == "members":
        # Guardian rules follow the gateway's assignment ceiling, not only the backend key's.
        if "assignment:read" not in set(principal["grants"].get("calendar", [])) & set(caps["permissions"]):
            result = dict(result)
            result["members"] = [
                {k: v for k, v in row.items() if k != "guardian_rules"} if isinstance(row, dict) else row
                for row in result.get("members", [])
            ]
    if tool.get("action_group"):
        if tool["operation"] == "list_messenger_operations":
            result = {
                **result,
                "items": [
                    row
                    for row in result.get("items", [])
                    if row.get("operation") in allowed_actions and row.get("supported") is True
                ],
            }
        elif result.get("operation") not in allowed_actions:
            raise Denied()
    if tool["operation"] == "get_attachment" and result.get("download_url"):
        result = gateway.tickets.issue(
            result, key, agent, snapshot, credential, args["attachment_id"], started
        )
    if tool["operation"] == "capabilities":
        # Preserve scoped coverage/account data, while avoiding the upstream
        # catalog advertising methods unavailable through this connection.
        visible = [
            n
            for n in gateway.policy.visible(snapshot, agent)
            if CATALOG[n]["module"] == module
            and set(CATALOG[n]["scopes"]) <= set(caps["permissions"])
            and gateway.implemented(CATALOG[n], snapshot, agent, caps)
            and (
                not CATALOG[n].get("action_group")
                or bool(routed(CATALOG[n], allowed_actions))
                or (
                    CATALOG[n]["operation"] == "get_messenger_operation"
                    and bool(gateway.policy.actions(snapshot, agent, caps["permissions"]))
                )
            )
        ]
        result = dict(result)
        for field in (
            "tool_manifest",
            "messenger_operations",
            "available",
            "features",
            "key_permission_ceiling",
            "permissions",
        ):
            result.pop(field, None)
        result["gateway_tools"] = visible
        result["gateway_principal"] = agent["id"]
        result["backend_principal"] = result.get("principal")
        result["gateway_actions"] = allowed_actions
        result["gateway_actions_scope"] = "messenger_operations"
        manifest = manifest_summary(caps)
        if manifest is not None:
            result["backend_tool_manifest"] = manifest
        if module == "messaging":
            result.update(gateway.usable_actions(snapshot, agent, caps))
        result["gateway_policy_revision"] = snapshot["revision"]
        if module == "messaging":
            result = capabilities_view(subscription_blocks(result, visible), args)
    return result
