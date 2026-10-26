"""Rulebook v1 shapes, bounds and the conformance matrix on the in-memory reference (ANYM-544)."""
import pytest

from platform_contracts import agent
from platform_contracts.agent import agent_error, agent_result, validate_error, validate_result
from platform_contracts.agent_conformance import CASES, MemoryIntents, run_matrix

OPERATION = {"module": "messaging", "id": "operation-1"}
SEND = {"tool": "messages_send_message", "arguments": {"chat_id": "chat-1", "request_id": "req-candidate-1"}}
SEARCH = {"tool": "messages_search_people", "arguments": {"query": "synthetic"}}
READ_ONLY = frozenset({"messages_search_people"})


def candidate(n, next=None):
    return {"id": f"chat-{n}", "label": f"Synthetic {n}", "kind": "chat", "network": "telegram", "account": "synthetic",
            "last_activity": "2026-10-26T00:00:00Z", "next": next}


# spec: ANYM-544
@pytest.mark.parametrize("value", [
    {"state": "succeeded", "detail_state": "sent", "request_id": "req-synthetic-1", "next": None},
    {"state": "previewed", "preview": {"text": "x"}, "confirm_token": "tok", "next": None},
    {"state": "pending", "detail_state": "queued", "operation": OPERATION,
     "next": {"tool": "messages_get_send_status", "arguments": {"send_id": "operation-1"}}},
    {"state": "unknown", "detail_state": "unknown", "operation": OPERATION},
    {"state": "failed", "resumable": True},
])
def test_valid_results_pass(value):
    assert validate_result(value) is value


# spec: ANYM-544
@pytest.mark.parametrize("value, reason", [
    ({"state": "done"}, "invalid_state"),
    ({"state": "running"}, "operation_required"),
    ({"state": "unknown"}, "operation_required"),
    ({"state": "previewed", "preview": {}, "confirm_token": "tok", "operation": OPERATION}, "invalid_preview"),
    ({"state": "previewed", "preview": {}}, "invalid_preview"),
    ({"state": "pending", "operation": {"module": "mail", "id": "x"}}, "invalid_operation"),
    ({"state": "succeeded", "request_id": "short"}, "invalid_request_id"),
    ({"state": "succeeded", "request_id": agent.PLACEHOLDER_REQUEST_ID}, "invalid_request_id"),
    ({"state": "succeeded", "next": {"tool": "x"}}, "invalid_next"),
    ({"state": "succeeded", "resumable": True}, "invalid_resumable"),
])
def test_invalid_results_name_the_rule(value, reason):
    with pytest.raises(ValueError, match=reason):
        validate_result(value)


# spec: ANYM-544
def test_the_messaging_envelope_keys_and_the_new_members_validate():
    value = agent_error("invalid_arguments", request_id="0123456789abcdef0123456789abcdef", retryable=False,
                        field_errors=[{"field": "chat_id", "reason": "required"}], hint="Name the chat.",
                        next=SEARCH, candidates=[candidate(1, SEND)])
    assert set(value) == {"error", "request_id", "retryable", "field_errors", "hint", "next", "candidates"}


# spec: ANYM-544
def test_candidates_are_bounded_at_ten_and_more_needs_a_read_only_next():
    ten = [candidate(n) for n in range(10)]
    assert validate_error({"error": "selection_required", "candidates": ten, "more": True, "next": SEARCH},
                          read_only=READ_ONLY)
    with pytest.raises(ValueError, match="too_many_candidates"):
        validate_error({"error": "selection_required", "candidates": [*ten, candidate(10)]}, read_only=READ_ONLY)
    with pytest.raises(ValueError, match="more_needs_read_only_next"):
        validate_error({"error": "selection_required", "candidates": ten, "more": True}, read_only=READ_ONLY)
    with pytest.raises(ValueError, match="more_needs_read_only_next"):
        validate_error({"error": "not_found", "candidates": ten, "more": True, "next": SEND}, read_only=READ_ONLY)
    with pytest.raises(ValueError, match="invalid_candidate"):
        validate_error({"error": "not_found", "candidates": [{**candidate(1), "phone": "+10000000000"}]})


# spec: ANYM-544
@pytest.mark.parametrize("count", [1, 3])
def test_selection_required_keeps_a_mutating_continuation_on_candidates_only(count):
    """One fuzzy candidate is still a selection: the send lives on the candidate, never at the top."""
    candidates = [candidate(n, SEND) for n in range(count)]
    assert validate_error({"error": "selection_required", "candidates": candidates, "next": SEARCH}, read_only=READ_ONLY)
    assert validate_error({"error": "selection_required", "candidates": candidates})
    with pytest.raises(ValueError, match="mutating_next_on_selection"):
        validate_error({"error": "selection_required", "candidates": candidates, "next": SEND}, read_only=READ_ONLY)
    with pytest.raises(ValueError, match="selection_needs_candidates"):
        validate_error({"error": "selection_required", "candidates": []})


# spec: ANYM-544
@pytest.mark.parametrize("value, valid", [
    ("a" * 7, False), ("a" * 8, True), ("a" * 128, True), ("a" * 129, False), ("req id 1", False),
    ("req/slash1", False), ("Req_1.2:3-4", True), (agent.PLACEHOLDER_REQUEST_ID, False), (12345678, False),
])
def test_request_id_bounds(value, valid):
    assert agent.is_request_id(value) is valid


# spec: ANYM-544
def test_missing_request_id_names_the_field_and_a_next_that_cannot_run_verbatim():
    error = agent.missing_request_id("messages_send_message", {"chat_id": "chat-1", "text": "synthetic"})
    assert error["field_errors"] == [{"field": "request_id", "reason": "required"}]
    assert error["next"]["tool"] == "messages_send_message"
    assert error["next"]["arguments"] == {"chat_id": "chat-1", "text": "synthetic", "request_id": "REPLACE_WITH_YOUR_ID"}
    assert not agent.is_request_id(error["next"]["arguments"]["request_id"])


# spec: ANYM-544
def test_child_keys_fit_from_a_128_character_parent_and_differ_per_step_and_attempt():
    parent = "p" * 128
    key = agent.child_key("principal-1", "messages_send_intent", parent, "send", 1)
    assert len(key) == agent.CHILD_KEY_LENGTH and agent.is_request_id(key)
    assert key == agent.child_key("principal-1", "messages_send_intent", parent, "send", 1)
    assert len({key, agent.child_key("principal-1", "messages_send_intent", parent, "send", 2),
                agent.child_key("principal-1", "messages_send_intent", parent, "upload", 1),
                agent.child_key("principal-2", "messages_send_intent", parent, "send", 1)}) == 4
    with pytest.raises(ValueError):
        agent.child_key("principal-1", "tool", parent, "send", 0)


# spec: ANYM-544
def test_a_caller_cannot_pre_occupy_a_child_key_through_the_old_api():
    store = MemoryIntents()
    key = agent.child_key("p1", "t_send", "parent-request-1", "send", 1)
    first = store.call("p1", "t_send", {"target": "alpha", "text": "synthetic", "request_id": key})
    step = store.step("p1", "t_send", key, {"target": "alpha", "text": "synthetic"})
    assert first["state"] == step["state"] == "succeeded"
    assert first["operation"] != step["operation"] and store.effects == 2
    assert store.step("p1", "t_send", key, {"target": "alpha", "text": "synthetic"}) == step and store.effects == 2


# spec: ANYM-544
def test_every_status_maps_to_one_state_and_an_unknown_value_raises():
    for states in agent.STATUS_STATES.values():
        assert set(states.values()) <= set(agent.STATES)
    assert agent.state_of("calls.call", "finished", dial_state="unknown") == "unknown"
    assert agent.state_of("calls.call", "active", dial_state="accepted") == "running"
    for kind, status, dial in (("messaging.send", "delivered", None), ("calls.call", "finished", "lost"),
                               ("mail.send", "sent", None)):
        with pytest.raises(ValueError, match="unmapped_status"):
            agent.state_of(kind, status, dial_state=dial)


# spec: ANYM-544
def test_agent_result_builder_keeps_detail_state_and_validates():
    value = agent_result("pending", detail_state="queued", request_id="req-synthetic-1", operation=OPERATION)
    assert value == {"state": "pending", "detail_state": "queued", "request_id": "req-synthetic-1",
                     "operation": OPERATION, "next": None}
    with pytest.raises(ValueError):
        agent_result("running")


# spec: ANYM-544
@pytest.mark.parametrize("case", sorted(CASES))
def test_conformance_matrix_on_the_in_memory_reference(case):
    assert run_matrix(MemoryIntents, [case]) == [case]


# spec: ANYM-544
def test_the_matrix_covers_the_card_cases():
    assert {"tampered_token", "other_principal", "other_tool", "expiry", "drift", "revocation",
            "concurrent_consumption", "crash_after_binding", "replay_with_other_request_id",
            "lost_response_replay"} <= set(CASES)


class ResolvesBeforeReplay(MemoryIntents):
    """A broken store: a same-key replay is compared with a fresh resolution, so drift turns it into a conflict."""

    def _admit(self, principal, tool, arguments):
        bound = self.keys.get(("caller", principal, arguments.get("request_id")))
        if bound is not None and "confirm_token" not in arguments and self._resolve(arguments) != self.intents[bound]["resolution"]:
            return agent.agent_error("idempotency_conflict")
        return super()._admit(principal, tool, arguments)


class StaleConsumedToken(MemoryIntents):
    """A broken store: a consumed token is checked for freshness again instead of returning its intent."""

    def _admit(self, principal, tool, arguments):
        record = self.tokens.get(arguments.get("confirm_token"))
        if record is not None and record["intent"] is not None and self._resolve(record["arguments"]) != record["resolution"]:
            return agent.agent_error("preview_stale", changed=["target_id"])
        return super()._admit(principal, tool, arguments)


# spec: ANYM-544
@pytest.mark.parametrize("broken", [ResolvesBeforeReplay, StaleConsumedToken])
def test_the_matrix_fails_a_store_whose_replay_depends_on_drift(broken):
    with pytest.raises(AssertionError):
        run_matrix(broken, ["replay_after_drift"])
