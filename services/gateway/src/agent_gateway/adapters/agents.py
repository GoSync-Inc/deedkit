"""The `agents` module (ANYM-820): six gateway tools in front of the agent runner's REST routes (ANYM-819).

HTTP only: no runner code is imported. Each principal reaches the runner with its own runner key, from which the
runner takes the principal, so a task is visible only to the principal that started it. Runner refusals become
fixed codes with a next_action; no runner text is relayed, and every result keeps only the fields named here.
"""

from datetime import datetime, timezone
import re
import uuid

from platform_contracts import safe_error

from ..policy import Denied

MODULE = "agents"
# Admission asks the runner nothing: the gateway's grants decide which tools a principal sees, and the runner checks
# its own key's read and run operations on every call. Asking it would cost a login check per engine (up to 30 s).
ADMISSION_CAPABILITIES = {"permissions": ["agents:read", "agents:run"]}
MUTATIONS = frozenset({"start_task", "stop_task"})
START_FIELDS = ("engine", "instruction", "owner_instruction", "request_id", "max_minutes", "continue_task_id")
LIST_NEXT = "agents_list_tasks with this request_id"
STATES = frozenset({"queued", "starting", "running", "succeeded", "failed", "stopped", "timed_out", "lost"})
OUTCOMES = frozenset({"done", "partial", "blocked", "unknown"})
PUBLISH_STATES = frozenset({"pending", "published", "no_changes", "refused", "failed"})
LOG_KINDS = frozenset({"assistant", "tool", "result"})
COMPARE_PREFIX = "https://github.com/"
# The runner's opaque log cursor (unpadded URL-safe base64), as the tool schema takes it back.
CURSOR = re.compile(r"[A-Za-z0-9_-]{1,64}")
# A refusal the runner names -> the next step it leaves the caller. Only these codes pass; anything else is
# backend_unavailable (a read) or an unknown outcome (a start or stop).
REFUSALS = {
    "runner_busy": "retry_agents_start_task_with_the_same_request_id_after_retry_after_s",
    "runner_paused": "tell_owner_runner_paused",
    "daily_cap_reached": "tell_owner_daily_cap_reached_owner_raises_it_by_an_owner_requested_setting",
    "request_id_conflict": "use_a_new_request_id_only_for_a_new_task_or_agents_list_tasks_for_this_one",
    "engine_not_configured": "tell_owner_engine_not_configured",
    "host_check_failed": "tell_owner_host_check_failed",
}
# A failed task's reason -> its next step, in place of the runner's tell_owner.
FAILED_REASONS = {
    "login_required": "ask_owner_to_log_in_engine",
    "host_busy": "tell_owner_host_busy_start_again_later",
}


def task_id(value):
    """The canonical form of a schema-checked UUID: the only caller text that reaches a runner path."""
    return str(uuid.UUID(value))


def request(operation, args):
    """(method, path, query, body) of one tool call on the runner."""
    if operation == "capabilities":
        return "GET", "/v1/capabilities", None, None
    if operation == "list_tasks":
        return "GET", "/v1/tasks", {"request_id": task_id(args["request_id"])}, None
    if operation == "get_task":
        return "GET", "/v1/tasks/" + task_id(args["task_id"]), None, None
    if operation == "read_task_log":
        query = {"raw": "true" if args.get("raw") else "false"}
        if args.get("cursor"):
            query = {"cursor": args["cursor"], **query}
        return "GET", "/v1/tasks/" + task_id(args["task_id"]) + "/log", query, None
    if operation == "start_task":
        return "POST", "/v1/tasks", None, {k: args[k] for k in START_FIELDS if args.get(k) is not None}
    if operation == "stop_task":
        return "POST", "/v1/tasks/" + task_id(args["task_id"]) + "/stop", None, None
    raise Denied("backend_unavailable", 503)


def refusal(status, value):
    """The fixed error of a runner answer that refused the call, or None for an answer this module does not know."""
    code = value.get("error") if isinstance(value, dict) else None
    if not isinstance(code, str):
        # An object or list here is no refusal this module knows: the caller's fallback (sent, unknown) applies.
        return None
    if status == 404 and code == "not_found":
        return not_found()
    if status == 422 and code == "continue_task_invalid":
        # Another principal's task and an own task that cannot be continued look the same: no ownership oracle.
        denied = not_found()
        denied.detail = {**denied.detail, "field": "continue_task_id",
                         "next_action": "continue_task_id names your own finished task that has a branch"}
        return denied
    if status in (401, 403) and code in ("unauthorized", "forbidden", "peer_refused"):
        denied = Denied("backend_unavailable", 503)
        denied.detail = {**safe_error("backend_unavailable"), "reason": "runner_key_refused", "operation_sent": True,
                         "next_action": "tell_owner_runner_key_refused"}
        return denied
    if code not in REFUSALS:
        return None
    denied = Denied(code, status)
    denied.detail = {**safe_error(code), "next_action": REFUSALS[code]}
    if code == "runner_busy":
        denied.detail["retry_after_s"] = bounded(value.get("retry_after_s"), 1, 86400) or 60
    if code == "daily_cap_reached":
        denied.detail["next_slot_at"] = timestamp(value.get("next_slot_at"))
    return denied


def not_found():
    denied = Denied()
    denied.detail = {"error": "not_found_or_forbidden"}
    return denied


def unknown_outcome(operation, args):
    """A start or stop the runner may have received and acted on: read it back, never send it again."""
    denied = Denied("mutation_outcome_unknown", 503)
    denied.detail = {**safe_error("mutation_outcome_unknown", unknown_outcome=True)}
    if operation == "start_task":
        denied.detail.update(request_id=args["request_id"], next_action=LIST_NEXT,
                             next={"tool": "agents_list_tasks", "arguments": {"request_id": args["request_id"]}})
    else:
        denied.detail.update(task_id=args["task_id"], next_action="agents_get_task",
                             next={"tool": "agents_get_task", "arguments": {"task_id": args["task_id"]}})
    return denied


def bounded(value, low, high):
    return value if type(value) is int and low <= value <= high else None


def timestamp(value):
    if type(value) not in (int, float) or not 0 <= value < 253402300800:
        return None
    return datetime.fromtimestamp(int(value), timezone.utc).isoformat().replace("+00:00", "Z")


def iso(value):
    """An ISO-8601 time string from the runner, or None."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def text(value, limit):
    return value[:limit] if isinstance(value, str) else None


def word(value, allowed):
    return value if isinstance(value, str) and value in allowed else None


def name(value, limit=128):
    """A runner code or branch name: short, printable, no spaces."""
    return value if isinstance(value, str) and 0 < len(value) <= limit and value.isprintable() and " " not in value \
        else None


def task(view):
    """One task as S1 criterion 8 names it, plus its place in the queue."""
    view = view if isinstance(view, dict) else {}
    progress = given if isinstance(given := view.get("progress"), dict) else {}
    state = word(view.get("state"), STATES)
    compare = view.get("compare_url")
    reason = name(view.get("reason"), 64)
    next_action = word(view.get("next_action"), {"wait", "tell_owner"})
    if state == "failed" and reason in FAILED_REASONS:
        next_action = FAILED_REASONS[reason]
    return {
        "task_id": task_id(view["id"]) if isinstance(view.get("id"), str) and valid_uuid(view["id"]) else None,
        "state": state, "reason": reason, "outcome": word(view.get("outcome"), OUTCOMES),
        "engine": word(view.get("engine"), {"claude", "codex"}),
        "request_id": view.get("request_id") if valid_uuid(view.get("request_id")) else None,
        "continue_task_id": view.get("continue_task_id") if valid_uuid(view.get("continue_task_id")) else None,
        "created_at": iso(view.get("created_at")), "started_at": iso(view.get("started_at")),
        "finished_at": iso(view.get("finished_at")),
        # A queued task has no expected_finish_by yet; the runner's estimate from the tasks ahead stands in.
        "expected_finish_by": iso(view.get("expected_finish_by")) or iso(view.get("estimated_finish_by")),
        "ahead_of_you": bounded(view.get("ahead_of_you"), 0, 1000),
        "exit_code": bounded(view.get("exit_code"), -1024, 1024),
        "summary": text(view.get("summary"), 4000),
        "progress": {"last_activity_at": iso(progress.get("last_activity_at")),
                     "last_text": text(progress.get("last_text"), 300),
                     "commits": bounded(progress.get("commits"), 0, 100000) or 0,
                     "files_changed": bounded(progress.get("files_changed"), 0, 100000) or 0},
        "publish_state": word(view.get("publish_state"), PUBLISH_STATES),
        "publish_reason": name(view.get("publish_reason"), 64), "branch": name(view.get("branch"), 200),
        "compare_url": compare if isinstance(compare, str) and compare.startswith(COMPARE_PREFIX)
        and len(compare) <= 400 and compare.isprintable() and " " not in compare else None,
        "next_action": next_action,
    }


def valid_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def answers(operation, value, args):
    """Whether a 2xx body is this call's answer: a task names a valid id and state, and the start's request_id and
    engine, or the stopped or read task's id; a log page has entries; capabilities have engines. Anything else is an
    answer of unknown shape (for a start or stop: an unknown outcome, never a success without a task)."""
    if operation == "read_task_log":
        return isinstance(value.get("entries"), list)
    if operation == "capabilities":
        return isinstance(value.get("engines"), dict)
    if not valid_uuid(value.get("id")) or value.get("state") not in STATES:
        return False
    if operation in ("start_task", "list_tasks"):
        if value.get("request_id") != task_id(args["request_id"]):
            return False
        return operation != "start_task" or value.get("engine") == args["engine"]
    return value["id"] == task_id(args["task_id"])


def started(view):
    """What a start returns: the task, its place in the queue and the call that follows it."""
    value = task(view)
    return {key: value[key] for key in ("task_id", "state", "engine", "request_id", "continue_task_id",
                                        "ahead_of_you", "expected_finish_by")} | {"next_action": "agents_get_task"}


def log_page(page):
    page = page if isinstance(page, dict) else {}
    entries = []
    for row in given if isinstance(given := page.get("entries"), list) else ():
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            continue
        entry = {"time": iso(row.get("time"))}
        if "kind" in row:
            entry["kind"] = word(row.get("kind"), LOG_KINDS)
        entries.append(entry | {"text": row["text"]})
    cursor = page.get("next_cursor")
    return {"entries": entries,
            "next_cursor": cursor if isinstance(cursor, str) and CURSOR.fullmatch(cursor) else None,
            "end": page.get("end") is True}


def capabilities(value):
    value = value if isinstance(value, dict) else {}
    engines = given if isinstance(given := value.get("engines"), dict) else {}
    gate = given if isinstance(given := value.get("host_gate"), dict) else {}
    return {
        "engines": {
            engine: {"configured": row.get("configured") is True,
                     "login_state": word(row.get("login_state"), {"ok", "login_required", "unknown"}) or "unknown",
                     **{k: bounded(row.get(k), 0, 1000) for k in ("max_running", "max_queued", "running", "queued")}}
            for engine in ("claude", "codex") if isinstance(row := engines.get(engine), dict)
        },
        "daily_starts": bounded(value.get("daily_starts"), 0, 1000),
        "starts_left": bounded(value.get("starts_left"), 0, 1000),
        "paused": value.get("paused") is True,
        "host_gate": {"state": word(gate.get("state"), {"ok", "busy"}) or "busy",
                      **({"reason": word(gate.get("reason"), {"memory", "disk", "inference", "tests", "host_check"})}
                         if gate.get("reason") is not None else {})},
        "host_check": word(value.get("host_check"), {"ok", "failed"}) or "failed",
    }


def shape(operation, result):
    """The result a caller sees for one runner answer."""
    if operation == "start_task":
        return started(result)
    if operation == "list_tasks":
        return {"tasks": [task(result)]}
    if operation == "read_task_log":
        return log_page(result)
    if operation == "capabilities":
        return capabilities(result)
    return task(result)
