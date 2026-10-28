"""calendar_schedule series scopes through the gateway: a fixed word, and the iCloud refusal keeps its next (ANYM-556)."""
import asyncio

import pytest

from agent_gateway.catalog import CATALOG
from agent_gateway.dispatch import caller_payload
from agent_gateway.policy import Denied
from test_calendar_schedule import gateway

MOVE = {"action": "move", "event": "evt-1f3a", "start": "2026-11-02T10:30", "series": "following"}
CLOSEST = {"tool": "calendar_schedule", "arguments": {"selection_token": "s" * 32, "dry_run": True}}


# spec: ANYM-556
def test_the_schema_takes_a_series_scope_and_it_is_no_payload():
    schema = CATALOG["calendar_schedule"]["inputSchema"]
    assert schema["properties"]["series"]["anyOf"][0]["enum"] == ["this", "following", "all"]
    assert "following" not in caller_payload(MOVE, schema)


# spec: ANYM-556
def test_capability_unsupported_for_a_series_reaches_the_agent_with_its_closest_call(tmp_path):
    answer = {"error": "capability_unsupported", "capability": "series", "provider": "icloud", "scopes": ["this"],
              "calendar": {"calendar_ref": "cal-ia", "label": "Home iCloud"}, "next": CLOSEST}
    with pytest.raises(Denied) as caught:
        asyncio.run(gateway(tmp_path, answer).call("gateway-a", "calendar_schedule", MOVE))
    assert caught.value.detail == answer
