"""calendar_schedule guests through the gateway: the schema takes them, the refusal keeps its next (ANYM-555)."""
import asyncio

import pytest

from agent_gateway.catalog import CATALOG
from agent_gateway.dispatch import caller_payload
from agent_gateway.policy import Denied
from test_calendar_schedule import gateway

CREATE = {"action": "create", "title": "Swimming", "start": "2026-11-02T10:00", "calendar": "Home iCloud",
          "guests": ["ana@example.invalid"], "notify": "all"}
CLOSEST = {"tool": "calendar_schedule",
           "arguments": {"selection_token": "s" * 32, "calendar": "cal-ia", "dry_run": True}}


# spec: ANYM-555
def test_guest_addresses_are_payload_and_notify_is_a_fixed_word():
    assert caller_payload(CREATE, CATALOG["calendar_schedule"]["inputSchema"]) == {
        "Swimming", "ana@example.invalid"}


# spec: ANYM-555
def test_capability_unsupported_reaches_the_agent_with_calendar_provider_and_closest_call(tmp_path):
    answer = {"error": "capability_unsupported", "capability": "guests", "provider": "icloud",
              "calendar": {"calendar_ref": "cal-ia", "label": "Home iCloud"}, "next": CLOSEST}
    with pytest.raises(Denied) as caught:
        asyncio.run(gateway(tmp_path, answer).call("gateway-a", "calendar_schedule", CREATE))
    assert caught.value.detail == answer


# spec: ANYM-555
def test_a_next_that_repeats_a_guest_address_is_dropped(tmp_path):
    leak = {"tool": "calendar_schedule", "arguments": {**CREATE, "guests": None, "notify": None,
                                                       "title": "ana@example.invalid", "dry_run": True}}
    answer = {"error": "capability_unsupported", "capability": "guests", "next": leak}
    with pytest.raises(Denied) as caught:
        asyncio.run(gateway(tmp_path, answer).call("gateway-a", "calendar_schedule", CREATE))
    assert "next" not in caught.value.detail
