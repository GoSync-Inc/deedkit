"""calls_call and calls_call_result (ANYM-551): one-call intents on the calls storage, never a second dial."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import threading

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
# The calls image ships without the shared package; it is standard library only, so tests read it from the repo.
sys.path.insert(0, str(ROOT / "packages/platform-contracts/src"))
from platform_contracts import agent  # noqa: E402
from platform_contracts.agent_conformance import CASES, Crash, run_matrix  # noqa: E402

from agent_api import SCOPES, AgentAPI, AgentRegistry  # noqa: E402
import call_intent  # noqa: E402
from core import ServiceError, VoiceService  # noqa: E402
from outbound import CallingSettings, Outbound  # noqa: E402

CHAT = "chat_" + "a" * 32
PERSON = "0b9d6c1e-3f4a-4c55-9a7e-1d2c3b4a5f60"
PHONE, OTHER_PHONE, TYPED = "+12025550123", "+12025550125", "+12025550199"
ARCHIVE_WORDS = "Call the supplier and ask about the delivery"
TYPED_WORDS = "Call +1 202 555 0199 and ask about the delivery"


class Died(BaseException):
    """The process dies inside the provider request: nothing below it catches this."""


class Provider:
    def __init__(self, world):
        self.world = world

    async def preflight(self, brief):
        return {"configured": True}

    async def dial(self, call):
        self.world.effects += 1
        await asyncio.sleep(0.01)
        if self.world.crash:
            self.world.crash = False
            raise Died()
        if self.world.uncertain:
            raise httpx.ReadTimeout("synthetic timeout")
        if self.world.finish:
            self.world.service.stop(call["id"], "provider_completed")
        return {"provider_call_id": "CA" + "1" * 32, "provider_status": "queued"}

    async def hangup(self, sid):
        pass


class Archive:
    """The scoped messaging archive: ANYM-545's resolver and the provider ID of a chat."""

    def __init__(self, world):
        self.world = world

    async def resolve_target(self, to, purpose, account=""):
        assert purpose == "call"
        self.world.resolved.append((to, account))
        if to in (CHAT, PERSON):
            return 200, {"state": "succeeded", "target": dict(self.world.target), "next": None}
        if to == "Ivan":
            return 409, {"error": "selection_required", "candidates": [
                {"id": CHAT, "label": "Ivan Petrov", "kind": "chat", "network": "whatsapp", "account": "wa1",
                 "next": {"tool": "platform_resolve_target", "arguments": {"to": CHAT, "purpose": "call"}}}]}
        if to == "8 (202) 555-0177":
            return 200, {"target": {"account": "wa1", "chat_id": None, "phone": "+12025550177", "network": "whatsapp",
                                    "label": "WhatsApp +12025550177", "how_resolved": "phone_opens_chat",
                                    "opens_chat": True}}
        return 404, {"error": "not_found"}

    async def provider_chat(self, chat_id):
        return self.world.telegram_user

    async def close(self):
        pass


class World:
    """One calls service with the real AgentAPI, outbound and storage on its own event loop thread."""

    def __init__(self, tmp_path, mode="archive", channels=("phone", "whatsapp", "telegram", "sip")):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.tmp, self.mode, self.offset = tmp_path, mode, 0.0
        self.effects, self.crash, self.uncertain, self.finish = 0, False, False, True
        self.resolved, self.telegram_user = [], "123456789"
        self.target = {"account": "wa1", "chat_id": CHAT, "phone": PHONE, "network": "whatsapp",
                       "label": "Alpha Supplier", "how_resolved": "chat_id", "person": PERSON}
        self.registry = tmp_path / "agents.json"
        self.doc = {"version": 1, "revision": 1, "principals": [
            {"id": name, "key_sha256": hashlib.sha256(name.encode()).hexdigest(), "enabled": True,
             "expires_at": "2099-01-01T00:00:00Z", "scopes": sorted(SCOPES), "channels": list(channels),
             "destinations": {"sip": ["sip:desk@pbx.example.test"]} if "sip" in channels else {}, "archive_config": "/synthetic/archive.json"}
            for name in ("p1", "p2")]}
        self.save()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.boot()

    def save(self):
        self.registry.write_text(json.dumps(self.doc))
        self.registry.chmod(0o600)

    def boot(self):
        self.service = VoiceService(self.tmp / "data")
        self.outbound = Outbound(self.service, lambda: "fake", CallingSettings(enabled=True), {"twilio": Provider(self)})

        async def stop(identity):
            return await self.outbound.stop(identity, "agent_stop")
        self.api = AgentAPI(self.service, AgentRegistry(str(self.registry)), None, stop, self.outbound,
                            lambda principal: Archive(self))
        self.api.intents.clock = lambda: 1_000_000.0 + self.offset

    def run(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(30)

    def tool(self, principal, name, arguments):
        try:
            return self.run(self.api.call(principal, name, arguments))
        except ServiceError as error:
            return {**error.detail, "error": error.code}
        except Died:
            # The process died: a new one starts on the same storage and the caller sees a dropped request.
            self.service.close()
            self.boot()
            raise Crash() from None

    def other_tool(self, principal, arguments):
        """The same token store under another tool name (ANYM-544 case other_tool)."""
        who = self.api.registry.authenticate(principal)
        try:
            return self.run(self.api.intents.call(who, lambda: None, self.api.route, "call_other", arguments))
        except ServiceError as error:
            return {**error.detail, "error": error.code}

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


class Matrix:
    """ANYM-544's harness over World: target "alpha" is the archive chat, or the owner-typed number."""

    def __init__(self, world):
        self.world = world

    @property
    def effects(self):
        return self.world.effects

    def call(self, principal, tool, arguments):
        args = dict(arguments)
        if "target" in args:
            args["to"] = {"archive": CHAT, "typed": TYPED}[self.world.mode] if args.pop("target") == "alpha" else "x"
        if "text" in args:
            args["goal"] = args.pop("text")
        # The owner's words come with every call, also with a token: they are never read from the preview.
        args["owner_instruction"] = ARCHIVE_WORDS if self.world.mode == "archive" else TYPED_WORDS
        if tool == "t_other":
            return self.world.other_tool(principal, args)
        return self.world.tool(principal, "call", args)

    def advance(self, seconds):
        self.world.offset += seconds

    def drift(self):
        self.world.target["phone"] = OTHER_PHONE
        return ["destination"]

    def revoke(self, principal, tool):
        for p in self.world.doc["principals"]:
            if p["id"] == principal:
                p["scopes"] = [s for s in p["scopes"] if s != "calls:dial"]
        self.world.doc["revision"] += 1
        self.world.save()

    def crash_after_binding(self):
        self.world.crash = True


@pytest.fixture
def world(tmp_path):
    made = []

    def make(**options):
        made.append(World(tmp_path / str(len(made)), **options))
        return made[-1]
    yield make
    for w in made:
        w.close()


def harness(world, mode):
    return lambda: Matrix(world(mode=mode))


def preview(w, **extra):
    result = w.tool("p1", "call", {"to": CHAT, "goal": "Ask when the order arrives", "dry_run": True,
                                   "owner_instruction": ARCHIVE_WORDS, **extra})
    assert agent.validate_result(result)["state"] == "previewed", result
    return result


def error(result, code):
    agent.validate_error(result)
    assert result["error"] == code, result
    return result


def execute(w, token, **extra):
    return w.tool("p1", "call", {"confirm_token": token, "owner_instruction": ARCHIVE_WORDS, **extra})


# The matrix's case_expiry wants preview_expired; this card (criterion 2), like calendar_schedule, answers an expired
# preview with preview_stale and changed: test_an_expired_preview_is_stale_with_changed_and_a_fresh_token.
# spec: ANYM-551
def test_archive_destination_meets_the_shared_token_and_key_matrix(world):
    cases = sorted(set(CASES) - {"request_id_rules", "replay_after_drift", "expiry"})
    assert run_matrix(harness(world, "archive"), cases) == cases


# spec: ANYM-551
def test_owner_typed_number_meets_the_shared_token_and_key_matrix(world):
    cases = sorted(set(CASES) - {"drift", "expiry"})
    assert run_matrix(harness(world, "typed"), cases) == cases


# spec: ANYM-551
def test_status_map_copies_the_rulebook():
    assert call_intent.STATUS_STATES == agent.STATUS_STATES["calls.call"]
    assert call_intent.FINAL_STATES == agent.FINAL_STATES
    assert (call_intent.REQUEST_ID.pattern, call_intent.PLACEHOLDER_REQUEST_ID, call_intent.PREVIEW_TTL_S,
            call_intent.MAX_WAIT_S) == (agent.REQUEST_ID.pattern, agent.PLACEHOLDER_REQUEST_ID, agent.PREVIEW_TTL_S,
                                        agent.MAX_WAIT_S)


# spec: ANYM-551
def test_dry_run_shows_brief_destination_and_preflight_and_execution_dials_that_brief(world):
    w = world()
    shown = preview(w, questions=["When does it arrive?", "Who signs for it?"])
    assert w.effects == 0 and w.service.list_calls() == []
    destination, brief = shown["preview"]["destination"], shown["preview"]["brief"]
    assert (destination["label"], destination["destination"], destination["channel"]) == ("Alpha Supplier", PHONE,
                                                                                           "whatsapp")
    assert destination["destination_source"] == "agent_found"
    assert brief["success_fields"] == ["answer_1", "answer_2"] and "Who signs for it?" in brief["goal"]
    assert shown["preview"]["preflight"]["ok"] is True and shown["preview"]["preflight"]["dial_units"] == 1
    done = agent.validate_result(execute(w, shown["confirm_token"]))
    call = w.service.get_call(done["call_id"])
    assert done["state"] == "succeeded" and w.effects == 1 and call["brief_id"] == brief["id"]
    # The destination's origin is derived from the archive source and is never owner_named.
    assert call["destination_source"] == "agent_found"
    assert CHAT in call["destination_evidence"] and PERSON in call["destination_evidence"]
    assert call["owner_instruction"] == ARCHIVE_WORDS


# spec: ANYM-551
def test_changed_goal_questions_or_instruction_is_stale_and_never_dials(world):
    w = world()
    token = preview(w)["confirm_token"]
    stale = error(execute(w, token, goal="Ask something else", questions=["Is it paid?"]), "preview_stale")
    assert stale["changed"] == ["goal", "questions"] and stale["confirm_token"] != token
    stale = error(execute(w, token, owner_instruction="Call someone else instead"), "preview_stale")
    assert stale["changed"] == ["owner_instruction"]
    assert w.effects == 0 and w.service.list_calls() == []


# spec: ANYM-551
def test_source_drift_between_preview_and_dial_is_stale(world):
    w = world()
    token = preview(w)["confirm_token"]
    w.target["phone"] = OTHER_PHONE
    stale = error(execute(w, token), "preview_stale")
    assert stale["changed"] == ["destination"] and stale["preview"]["destination"]["destination"] == OTHER_PHONE
    assert w.effects == 0


# spec: ANYM-551
def test_instruction_is_required_and_an_archive_destination_needs_a_token(world):
    w = world()
    missing = error(w.tool("p1", "call", {"to": CHAT, "goal": "Ask when it arrives", "request_id": "req-missing-1"}),
                    "invalid_arguments")
    assert {"field": "owner_instruction", "reason": "required"} in missing["field_errors"]
    both = error(w.tool("p1", "call", {"to": TYPED, "goal": "Ask when it arrives", "request_id": "req-both-1",
                                       "owner_instruction": TYPED_WORDS, "authorization_reference": TYPED_WORDS}),
                 "invalid_arguments")
    assert both["field_errors"][0]["field"] == "owner_instruction"
    error(w.tool("p1", "call", {"to": CHAT, "goal": "Ask when it arrives", "request_id": "req-archive-1",
                                "owner_instruction": ARCHIVE_WORDS}), "preview_required")
    # A typed number the owner did not write is not owner_named, and a bare number no source holds is refused.
    error(w.tool("p1", "call", {"to": "+12025550188", "goal": "Ask when it arrives", "request_id": "req-typed-1",
                                "owner_instruction": TYPED_WORDS}), "destination_not_in_owner_instruction")
    error(w.tool("p1", "call", {"to": "8 (202) 555-0177", "goal": "Ask when it arrives", "dry_run": True,
                                "owner_instruction": "Call 8 (202) 555-0177"}), "destination_not_in_owner_instruction")
    typed = agent.validate_result(w.tool("p1", "call", {"to": TYPED, "goal": "Ask when it arrives",
                                                        "request_id": "req-typed-2", "owner_instruction": TYPED_WORDS}))
    call = w.service.get_call(typed["call_id"])
    assert (call["destination_source"], call["brief"]["destination"]) == ("owner_named", TYPED) and w.effects == 1


# spec: ANYM-551
def test_a_fuzzy_name_is_refused_with_candidates_and_no_continuation(world):
    w = world()
    refused = error(w.tool("p1", "call", {"to": "Ivan", "goal": "Ask when it arrives", "dry_run": True,
                                          "owner_instruction": "Call Ivan about the delivery"}), "selection_required")
    assert [c["id"] for c in refused["candidates"]] == [CHAT] and "next" not in refused["candidates"][0]
    assert "next" not in refused and w.effects == 0 and w.service.list_calls() == []


# spec: ANYM-551
@pytest.mark.parametrize(("to", "words", "code"), [
    ("911", "Call 911 now", "emergency_number_unsupported"),
    ("+7 900", "Call +7 900", "short_code_unsupported"),
    ("sip:other@pbx.example.test", "Call sip:other@pbx.example.test", "not_found_or_forbidden"),
])
def test_destination_policy_refusals_stay(world, to, words, code):
    w = world()
    error(w.tool("p1", "call", {"to": to, "goal": "Ask when it arrives", "request_id": "req-policy-1",
                                "owner_instruction": words}), code)
    assert w.effects == 0 and w.service.list_calls() == []


# spec: ANYM-551
def test_premium_units_channel_grants_and_the_daily_cap_stay(world):
    w = world(channels=("phone",))
    error(w.tool("p1", "call", {"to": CHAT, "goal": "Ask when it arrives", "dry_run": True,
                                "owner_instruction": ARCHIVE_WORDS}), "not_found_or_forbidden")
    premium = w.tool("p1", "call", {"to": "+1 900 555 0123", "goal": "Ask when it arrives", "dry_run": True,
                                    "owner_instruction": "Call +1 900 555 0123"})
    assert premium["preview"]["preflight"]["dial_units"] == policy_units("+19005550123")
    w.outbound.daily_calls_remaining = lambda raises=None: 0
    args = {"to": TYPED, "goal": "Ask when it arrives", "request_id": "req-cap-1", "owner_instruction": TYPED_WORDS}
    error(w.tool("p1", "call", args), "daily_call_limit")
    w.outbound.daily_calls_remaining = lambda raises=None: 10
    error(w.tool("p1", "call", args), "daily_call_limit")  # the refusal is the intent's first result
    assert w.effects == 0 and w.service.list_calls() == []


def policy_units(number):
    import destination_policy
    return destination_policy.dial_units("phone", number)


# spec: ANYM-551
def test_concurrent_repeats_of_one_request_id_dial_once(world):
    w = world()
    args = {"to": TYPED, "goal": "Ask when it arrives", "request_id": "req-concurrent-1",
            "owner_instruction": TYPED_WORDS}

    async def both():
        return await asyncio.gather(*(w.api.call("p1", "call", dict(args)) for _ in range(4)),
                                    return_exceptions=True)
    results = w.run(both())
    done = [r for r in results if isinstance(r, dict)]
    assert w.effects == 1 and len(w.service.list_calls()) == 1 and done
    assert all(r == done[0] for r in done)
    assert all(isinstance(r, dict) or r.code == "in_progress" for r in results)
    assert w.tool("p1", "call", args) == done[0]


# spec: ANYM-551
def test_an_uncertain_dial_is_unknown_with_the_call_handle_and_never_retried(world):
    w = world()
    w.uncertain = True
    args = {"to": TYPED, "goal": "Ask when it arrives", "request_id": "req-unknown-1", "owner_instruction": TYPED_WORDS}
    first = agent.validate_result(w.tool("p1", "call", args))
    assert first["state"] == "unknown" and first["operation"] == {"module": "calls", "id": first["call_id"]}
    assert first["next"]["tool"] == "calls_call_result"
    assert w.tool("p1", "call", args) == first and w.effects == 1


# spec: ANYM-551
def test_call_result_waits_then_returns_state_expected_wait_and_next(world):
    w = world()
    w.finish = False
    args = {"to": TYPED, "goal": "Ask when it arrives", "questions": ["When does it arrive?"],
            "request_id": "req-result-1", "owner_instruction": TYPED_WORDS}
    call_id = w.tool("p1", "call", args)["call_id"]
    running = agent.validate_result(w.tool("p1", "call_result", {"call_id": call_id, "wait_s": 1}))
    assert running["state"] == "running" and 0 < running["expected_wait_s"] <= 640
    assert running["next"] == {"tool": "calls_call_result", "arguments": {"call_id": call_id, "wait_s": 25}}
    w.service.transcript(call_id, "user", "Tomorrow at ten")

    async def later():
        await asyncio.sleep(0.3)
        w.service.stop(call_id, "provider_completed")
    asyncio.run_coroutine_threadsafe(later(), w.loop)
    final = agent.validate_result(w.tool("p1", "call_result", {"call_id": call_id, "wait_s": 5}))
    assert final["state"] == "succeeded" and final["next"] is None and "expected_wait_s" not in final
    assert final["questions"] == {"answer_1": "When does it arrive?"} and final["request_id"] == "req-result-1"
    assert {"role": "user", "text": "Tomorrow at ten"} in final["transcript"] and "analysis" in final
    error(w.tool("p2", "call_result", {"call_id": call_id}), "not_found_or_forbidden")


# spec: ANYM-551
def test_channels_and_messaging_account_names_map_or_name_the_allowed_values(world):
    w = world()
    shown = preview(w, channel="whatsapp-owner-1")
    assert w.resolved[-1] == (CHAT, "whatsapp-owner-1") and shown["preview"]["destination"]["channel"] == "whatsapp"
    assert preview(w, channel="phone")["preview"]["destination"]["channel"] == "phone"
    refused = error(w.tool("p1", "call", {"to": CHAT, "goal": "Ask when it arrives", "dry_run": True,
                                          "channel": "viber", "owner_instruction": ARCHIVE_WORDS}), "invalid_arguments")
    assert refused["field_errors"] == [{"field": "channel", "reason": "enum",
                                        "allowed": ["phone", "whatsapp", "telegram", "sip"]}]
    mismatch = error(w.tool("p1", "call", {"to": CHAT, "goal": "Ask when it arrives", "dry_run": True,
                                           "channel": "telegram", "owner_instruction": ARCHIVE_WORDS}),
                     "invalid_arguments")
    assert mismatch["field_errors"][0]["allowed"] == ["whatsapp", "phone"]
    w.target = {"account": "tg1", "chat_id": CHAT, "network": "telegram", "label": "Alpha", "how_resolved": "chat_id"}
    telegram = preview(w)["preview"]["destination"]
    assert (telegram["channel"], telegram["destination"]) == ("telegram", "123456789")


# spec: ANYM-551
def test_a_token_without_the_owners_words_never_dials(world):
    w = world()
    token = preview(w)["confirm_token"]
    missing = error(w.tool("p1", "call", {"confirm_token": token}), "invalid_arguments")
    assert {"field": "owner_instruction", "reason": "required"} in missing["field_errors"]
    assert w.effects == 0 and w.service.list_calls() == []
    assert agent.validate_result(execute(w, token))["state"] == "succeeded" and w.effects == 1


# spec: ANYM-551
def test_a_channel_revoked_between_preview_and_token_never_dials(world):
    w = world()
    token = preview(w)["confirm_token"]
    w.doc["principals"][0]["channels"] = ["phone", "telegram", "sip"]
    w.doc["revision"] += 1
    w.save()
    error(execute(w, token), "not_found_or_forbidden")
    error(execute(w, token), "not_found_or_forbidden")  # the refusal is the intent's result, never in_progress
    assert w.effects == 0 and w.service.list_calls() == []


# spec: ANYM-551
@pytest.mark.parametrize("drifted", [False, True])
def test_a_crash_before_the_call_row_resumes_only_on_an_unchanged_destination(world, drifted):
    w = world()
    token = preview(w)["confirm_token"]

    async def die(*args, **kwargs):
        raise Died()
    w.outbound.dial = die  # the process dies after binding, before any call row or provider contact
    with pytest.raises(Crash):
        execute(w, token)
    if drifted:
        w.target["phone"] = OTHER_PHONE
        assert error(execute(w, token), "preview_stale")["changed"] == ["destination"]
        assert error(execute(w, token), "preview_stale") and w.effects == 0 and w.service.list_calls() == []
    else:
        done = agent.validate_result(execute(w, token))
        assert done["state"] == "succeeded" and w.effects == 1
        assert w.service.get_call(done["call_id"])["brief"]["destination"] == PHONE


# spec: ANYM-551
def test_two_previews_racing_for_one_request_id_dial_once(world):
    w = world()
    first, second = preview(w)["confirm_token"], preview(w, goal="Ask who signs for the order")["confirm_token"]
    plan = w.api.intents.plan

    async def slow_plan(*args):
        await asyncio.sleep(0.05)  # both executions pass the key check before either binds
        return await plan(*args)
    w.api.intents.plan = slow_plan

    async def both():
        return await asyncio.gather(*(w.api.call("p1", "call", {"confirm_token": t, "request_id": "req-race-1",
                                                                 "owner_instruction": ARCHIVE_WORDS})
                                      for t in (first, second)), return_exceptions=True)
    results = w.run(both())
    assert [r.code for r in results if isinstance(r, ServiceError)] == ["idempotency_conflict"]
    assert sum(isinstance(r, dict) for r in results) == 1 and w.effects == 1
    loser = (first, second)[[isinstance(r, ServiceError) for r in results].index(True)]
    error(execute(w, loser, request_id="req-race-1"), "idempotency_conflict")
    assert w.effects == 1 and len(w.service.list_calls()) == 1


# spec: ANYM-551
def test_an_expired_preview_is_stale_with_changed_and_a_fresh_token(world):
    w = world()
    token = preview(w)["confirm_token"]
    w.offset += call_intent.PREVIEW_TTL_S + 1
    stale = error(execute(w, token), "preview_stale")
    assert stale["changed"] == ["expires_at"] and stale["confirm_token"] != token and w.effects == 0
    assert agent.validate_result(execute(w, stale["confirm_token"]))["state"] == "succeeded" and w.effects == 1


# spec: ANYM-551
def test_records_past_seven_days_keep_only_what_stops_a_second_dial(world):
    w = world()
    used = preview(w)["confirm_token"]
    by_token = agent.validate_result(execute(w, used))
    args = {"to": TYPED, "goal": "Ask when it arrives", "questions": ["When does it arrive?"], "request_id": "req-keep-1",
            "owner_instruction": TYPED_WORDS}
    by_key = agent.validate_result(w.tool("p1", "call", args))
    unused = preview(w)["confirm_token"]
    w.offset += call_intent.KEEP_S + 1
    preview(w)  # any new record expires the old ones
    db = w.service.db
    assert db.execute("SELECT 1 FROM call_previews WHERE token_sha256=?", (call_intent.sha(unused),)).fetchone() is None
    assert db.execute("SELECT payload,destination FROM call_previews WHERE token_sha256=?",
                      (call_intent.sha(used),)).fetchone() == ("{}", "{}")
    kept = db.execute("SELECT payload,origin FROM call_intents").fetchall()
    assert len(kept) == 2 and all(set(json.loads(p)) <= {"sha256", "questions"} and o == "{}" for p, o in kept)
    # Replays still return the first call and never dial again; another payload under the key is still a conflict.
    assert w.tool("p1", "call", args) == by_key and execute(w, used) == by_token and w.effects == 2
    error(w.tool("p1", "call", {**args, "goal": "Ask something else"}), "idempotency_conflict")
    result = w.tool("p1", "call_result", {"call_id": by_key["call_id"]})
    assert result["questions"] == {"answer_1": "When does it arrive?"}


# spec: ANYM-551
def test_concurrent_recoveries_never_hide_the_placed_call(world):
    w = world()
    token = preview(w)["confirm_token"]

    async def die(*args, **kwargs):
        raise Died()
    w.outbound.dial = die
    with pytest.raises(Crash):
        execute(w, token)
    plan, plans = w.api.intents.plan, []

    async def drifting_plan(*args):
        plans.append(1)
        if len(plans) == 2:
            w.target["phone"] = OTHER_PHONE  # the second recovery would see a drifted source
        await asyncio.sleep(0.05)
        return await plan(*args)
    w.api.intents.plan = drifting_plan

    async def both():
        return await asyncio.gather(*(w.api.call("p1", "call", {"confirm_token": token,
                                                                 "owner_instruction": ARCHIVE_WORDS})
                                      for _ in range(2)), return_exceptions=True)
    results = w.run(both())
    assert [r.code for r in results if isinstance(r, ServiceError)] == ["in_progress"] and len(plans) == 1
    done = next(r for r in results if isinstance(r, dict))
    assert done["state"] == "succeeded" and w.effects == 1
    assert execute(w, token) == done
