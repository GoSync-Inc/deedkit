"""The shared confirm_token and request_id conformance matrix (ANYM-544), and its in-memory reference implementation.

An intent story runs `run_matrix(make)` against its own storage. `make()` returns a fresh harness with:
  call(principal, tool, arguments) -> dict   the tool's entry point: `dry_run`, `confirm_token`, `request_id`
  advance(seconds)                           move the storage clock
  drift() -> list[str]                       change the world so a previewed target resolves differently;
                                             returns the field names `preview_stale` must name in `changed`
  revoke(principal, tool)                    withdraw the principal's right to the tool
  crash_after_binding()                      the next execution binds its intent, then dies before a result
  effects -> int                             live effects done so far
Arguments of the matrix are `{"target": "alpha", "text": ...}`; "alpha" resolves in a fresh world.
"""
import secrets
import threading

from .agent import (PLACEHOLDER_REQUEST_ID, PREVIEW_TTL_S, agent_error, agent_result, is_request_id,
                    missing_request_id, validate_error, validate_result)

CONTROL = frozenset(("dry_run", "confirm_token", "request_id"))


class Crash(Exception):
    """The simulated process death of `crash_after_binding`."""


class MemoryIntents:
    """Reference storage: durable preview records, one intent per key and per token, a key origin per domain."""

    def __init__(self, module="messaging"):
        self.module, self.now, self.effects = module, 0.0, 0
        self.world, self.revoked = {"alpha": "target-1"}, set()
        self.tokens, self.keys, self.intents = {}, {}, {}
        self.crash, self.lock = False, threading.Lock()

    def advance(self, seconds):
        self.now += seconds

    def drift(self):
        self.world["alpha"] = "target-2"
        return ["target_id"]

    def revoke(self, principal, tool):
        self.revoked.add((principal, tool))

    def crash_after_binding(self):
        self.crash = True

    def _resolve(self, arguments):
        payload = {k: v for k, v in sorted(arguments.items()) if k not in CONTROL}
        return {"target_id": self.world.get(str(payload.get("target"))), "payload": payload}

    def _preview(self, principal, tool, arguments, resolution):
        token = secrets.token_urlsafe(24)
        self.tokens[token] = {"principal": principal, "tool": tool, "arguments": arguments, "resolution": resolution,
                              "expires": self.now + PREVIEW_TTL_S, "intent": None}
        return agent_result("previewed", preview=resolution, confirm_token=token)

    def _saved(self, intent_id):
        intent = self.intents[intent_id]
        if intent["result"] is not None:
            return intent["result"]
        if intent["crashed"]:
            # The effect may have happened: report it as unknown and never run it again.
            return agent_result("unknown", detail_state="interrupted", request_id=intent["request_id"],
                                operation={"module": self.module, "id": intent_id})
        return agent_error("in_progress", retryable=True)

    def _bind(self, origin, principal, tool, key, request_id, resolution):
        intent_id = "intent-" + secrets.token_hex(8)
        # The binding keeps the canonical payload: a replay is compared with it, never with a fresh resolution.
        self.intents[intent_id] = {"principal": principal, "tool": tool, "resolution": resolution,
                                   "payload": resolution["payload"], "request_id": request_id, "result": None,
                                   "crashed": False}
        if key is not None:
            self.keys[(origin, principal, key)] = intent_id
        return intent_id

    def _run(self, intent_id):
        crash, self.crash = self.crash, False
        intent = self.intents[intent_id]
        if crash:
            with self.lock:
                intent["crashed"] = True
            raise Crash()
        result = agent_result("succeeded", request_id=intent["request_id"],
                              operation={"module": self.module, "id": intent_id},
                              receipt={"target_id": intent["resolution"]["target_id"]})
        with self.lock:
            self.effects += 1
            intent["result"] = result
        return result

    def call(self, principal, tool, arguments):
        with self.lock:
            intent_id = self._admit(principal, tool, arguments)
            if isinstance(intent_id, dict):
                return intent_id
        return self._run(intent_id)

    def _admit(self, principal, tool, arguments):
        """A dict is the answer; a string is a newly bound intent to run outside the lock."""
        if (principal, tool) in self.revoked:
            return agent_error("not_found_or_forbidden")
        request_id = arguments.get("request_id")
        if request_id is not None and not is_request_id(request_id):
            return agent_error("invalid_arguments", field_errors=[{"field": "request_id", "reason": "pattern"}])
        if arguments.get("dry_run"):
            resolution = self._resolve(arguments)
            if resolution["target_id"] is None:
                return agent_error("not_found_or_forbidden")
            return self._preview(principal, tool, {k: v for k, v in arguments.items() if k not in CONTROL}, resolution)
        bound = self.keys.get(("caller", principal, request_id)) if request_id is not None else None
        token = arguments.get("confirm_token")
        if token is not None:
            record = self.tokens.get(token) if isinstance(token, str) else None
            if record is None or record["principal"] != principal or record["tool"] != tool:
                return agent_error("invalid_confirm_token")
            if record["intent"] is not None:
                if bound is not None and bound != record["intent"]:
                    return agent_error("idempotency_conflict")
                return self._saved(record["intent"])
            if record["expires"] <= self.now:
                return agent_error("preview_expired", next={"tool": tool, "arguments": {**record["arguments"], "dry_run": True}})
            current = self._resolve(record["arguments"])
            changed = sorted(k for k in current if current[k] != record["resolution"][k])
            if changed:
                fresh = self._preview(principal, tool, record["arguments"], current)
                return agent_error("preview_stale", changed=changed, preview=fresh["preview"],
                                   confirm_token=fresh["confirm_token"])
            if bound is not None:
                return agent_error("idempotency_conflict")
            # Consume and bind in one step: the token is the key, the request_id an alias of the same intent.
            record["intent"] = self._bind("caller", principal, tool, request_id, request_id, current)
            return record["intent"]
        if request_id is None:
            return missing_request_id(tool, arguments)
        if bound is not None:
            # Same key: compare the submitted tool and payload with the binding first, so drift never turns a
            # replay into a conflict.
            payload = {k: v for k, v in sorted(arguments.items()) if k not in CONTROL}
            if self.intents[bound]["payload"] != payload or self.intents[bound]["tool"] != tool:
                return agent_error("idempotency_conflict")
            return self._saved(bound)
        resolution = self._resolve(arguments)
        if resolution["target_id"] is None:
            return agent_error("not_found_or_forbidden")
        return self._bind("caller", principal, tool, request_id, request_id, resolution)

    def step(self, principal, tool, key, arguments):
        """A composite step under the internal key origin: `key` comes from `child_key`, never from a caller."""
        with self.lock:
            existing = self.keys.get(("child", principal, key))
            if existing is not None:
                return self._saved(existing)
            intent_id = self._bind("child", principal, tool, key, None, self._resolve(arguments))
        return self._run(intent_id)


def _check(condition, message):
    if not condition:
        raise AssertionError(message)


ARGS = {"target": "alpha", "text": "synthetic text"}


def _preview(h, principal="p1", tool="t_send"):
    result = validate_result(h.call(principal, tool, {**ARGS, "dry_run": True}))
    _check(result["state"] == "previewed" and result["confirm_token"], "dry_run returns previewed and a token")
    return result["confirm_token"]


def _error(result, code):
    validate_error(result)
    _check(result.get("error") == code, f"expected {code}, got {result}")
    return result


def case_tampered_token(h):
    token = _preview(h)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    _error(h.call("p1", "t_send", {"confirm_token": tampered, "request_id": "req-tamper-1"}), "invalid_confirm_token")
    _check(h.effects == 0, "a tampered token has no effect")


def case_other_principal(h):
    token = _preview(h)
    _error(h.call("p2", "t_send", {"confirm_token": token, "request_id": "req-other-1"}), "invalid_confirm_token")
    _check(h.effects == 0, "another principal's attempt has no effect")
    _check(validate_result(h.call("p1", "t_send", {"confirm_token": token}))["state"] == "succeeded",
           "the failed foreign attempt does not consume the token")


def case_other_tool(h):
    token = _preview(h)
    _error(h.call("p1", "t_other", {"confirm_token": token}), "invalid_confirm_token")
    _check(h.effects == 0, "a token of another tool has no effect")


def case_expiry(h):
    token = _preview(h)
    h.advance(PREVIEW_TTL_S + 1)
    _error(h.call("p1", "t_send", {"confirm_token": token}), "preview_expired")
    _check(h.effects == 0, "an expired token has no effect")
    bound = _preview(h)
    first = validate_result(h.call("p1", "t_send", {"confirm_token": bound}))
    h.advance(PREVIEW_TTL_S + 1)
    _check(h.call("p1", "t_send", {"confirm_token": bound}) == first, "a bound token replays after expiry")
    _check(h.effects == 1, "the replay after expiry has no effect")


def case_drift(h):
    token = _preview(h)
    changed = h.drift()
    stale = _error(h.call("p1", "t_send", {"confirm_token": token}), "preview_stale")
    _check(stale["changed"] == changed and stale["confirm_token"] != token and "preview" in stale,
           "preview_stale names the changed fields and carries a fresh preview")
    _check(h.effects == 0, "a stale token has no effect")
    _check(validate_result(h.call("p1", "t_send", {"confirm_token": stale["confirm_token"]}))["state"] == "succeeded",
           "the fresh token executes")


def case_replay_after_drift(h):
    token = _preview(h)
    by_token = validate_result(h.call("p1", "t_send", {"confirm_token": token}))
    by_key = validate_result(h.call("p1", "t_send", {**ARGS, "request_id": "req-drift-1"}))
    h.drift()
    _check(h.call("p1", "t_send", {"confirm_token": token}) == by_token, "a consumed token replays after drift")
    _check(h.call("p1", "t_send", {**ARGS, "request_id": "req-drift-1"}) == by_key,
           "the same request_id and payload replay after drift")
    _check(h.effects == 2, "neither replay after drift has an effect")


def case_revocation(h):
    token = _preview(h)
    h.revoke("p1", "t_send")
    _error(h.call("p1", "t_send", {"confirm_token": token}), "not_found_or_forbidden")
    _check(h.effects == 0, "a revoked right has no effect")


def case_concurrent_consumption(h):
    token = _preview(h)
    barrier, results = threading.Barrier(8), []

    def consume():
        barrier.wait()
        results.append(h.call("p1", "t_send", {"confirm_token": token}))

    threads = [threading.Thread(target=consume) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    done = [r for r in results if r.get("state") == "succeeded"]
    _check(len(results) == 8 and done and all(r == done[0] for r in done), "every winner sees one intent")
    _check(all(r in done or r.get("error") == "in_progress" for r in results), "a loser is in_progress")
    _check(h.effects == 1, "one token, one effect")


def case_crash_after_binding(h):
    token = _preview(h)
    h.crash_after_binding()
    try:
        h.call("p1", "t_send", {"confirm_token": token, "request_id": "req-crash-1"})
    except Exception:  # noqa: BLE001 - the simulated death of the executing process
        pass
    before = h.effects
    replay = validate_result(h.call("p1", "t_send", {"confirm_token": token}))
    _check(replay["state"] == "unknown" and "operation" in replay, "an interrupted bound intent is unknown")
    again = validate_result(h.call("p1", "t_send", {**ARGS, "request_id": "req-crash-1"}))
    _check(again == replay and h.effects == before, "neither the token nor its request_id runs the effect again")


def case_replay_with_other_request_id(h):
    token = _preview(h)
    first = validate_result(h.call("p1", "t_send", {"confirm_token": token, "request_id": "req-first-1"}))
    _check(h.call("p1", "t_send", {"confirm_token": token, "request_id": "req-second-1"}) == first,
           "a bound token returns its intent whatever new request_id comes with it")
    other = _preview(h)
    _error(h.call("p1", "t_send", {"confirm_token": other, "request_id": "req-first-1"}), "idempotency_conflict")
    _check(h.effects == 1, "neither replay has an effect")


def case_lost_response_replay(h):
    token = _preview(h)
    first = validate_result(h.call("p1", "t_send", {"confirm_token": token}))  # the caller never sees it
    _check(h.call("p1", "t_send", {"confirm_token": token}) == first and h.effects == 1, "the replay is the first result")


def case_request_id_rules(h):
    _error(missing := h.call("p1", "t_send", dict(ARGS)), "invalid_arguments")
    _check(missing["field_errors"][0]["field"] == "request_id"
           and missing["next"]["arguments"]["request_id"] == PLACEHOLDER_REQUEST_ID, "missing request_id names it")
    _error(h.call("p1", "t_send", missing["next"]["arguments"]), "invalid_arguments")
    _check(h.effects == 0, "the placeholder never runs")
    _check(validate_result(h.call("p1", "t_send", {**ARGS, "request_id": "req-dry-run-1", "dry_run": True}))["state"]
           == "previewed", "dry_run takes a request_id")
    first = validate_result(h.call("p1", "t_send", {**ARGS, "request_id": "req-dry-run-1"}))
    _check(first["state"] == "succeeded", "dry_run does not consume the request_id")
    _check(h.call("p1", "t_send", {**ARGS, "request_id": "req-dry-run-1"}) == first, "same ID, same payload: first result")
    _error(h.call("p1", "t_send", {**ARGS, "text": "other", "request_id": "req-dry-run-1"}), "idempotency_conflict")
    _check(h.effects == 1, "one request_id, one effect")


CASES = {name[len("case_"):]: fn for name, fn in sorted(globals().items()) if name.startswith("case_")}


def run_matrix(make, cases=None):
    """Run every case (or the named ones) on a fresh harness each; return the names that ran."""
    names = sorted(CASES) if cases is None else list(cases)
    for name in names:
        CASES[name](make())
    return names
