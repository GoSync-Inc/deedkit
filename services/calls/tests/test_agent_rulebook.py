"""Every call status maps onto exactly one rulebook `state`; dial_state=unknown is `unknown` whatever the status (ANYM-544)."""
import ast
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
# The calls image ships without the shared package; it is standard library only, so tests read it from the repo.
sys.path.insert(0, str(ROOT / "packages/platform-contracts/src"))
from platform_contracts.agent import CALL_DIAL_STATES, STATES, STATUS_STATES, state_of  # noqa: E402

from core import ACTIVE  # noqa: E402

CALLS = ROOT / "services/calls"
RECORDS = frozenset(("v", "value"))  # the call record's names in core.py


def strings(node):
    """The values an expression can take: a string, or either branch of a conditional (never its test)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return strings(node.body) | strings(node.orelse)
    return set()


def call_statuses():
    """ACTIVE plus every status core.py writes to a call record: update(status=...), v["status"]=... and creation."""
    values = set(ACTIVE)
    for node in ast.walk(ast.parse((CALLS / "core.py").read_text())):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "update"
                and isinstance(node.func.value, ast.Name) and node.func.value.id in RECORDS):
            values |= {s for k in node.keywords if k.arg == "status" for s in strings(k.value)}
        elif (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Subscript)
              and isinstance(node.targets[0].value, ast.Name) and node.targets[0].value.id in RECORDS
              and isinstance(node.targets[0].slice, ast.Constant) and node.targets[0].slice.value == "status"):
            values |= strings(node.value)
        elif isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if {"status", "started_at", "outcome"} <= keys:
                values |= strings(node.values[[getattr(k, "value", None) for k in node.keys].index("status")])
    return values


def dial_states():
    found = set(re.findall(r"dial_state='([a-z_]+)'", (CALLS / "outbound.py").read_text()))
    return found | {None}  # browser-only and Telegram calls have no dial_state


# spec: ANYM-544
def test_every_call_status_maps_onto_exactly_one_state():
    statuses = call_statuses()
    assert {"waiting_for_browser", "dialing", "active", "finished", "failed", "interrupted"} <= statuses
    assert statuses == set(STATUS_STATES["calls.call"]), "a status without a mapping, or a removed one"
    assert dial_states() == CALL_DIAL_STATES, "a dial_state without a mapping"
    for status in statuses:
        for dial in dial_states() - {"unknown"}:
            assert state_of("calls.call", status, dial_state=dial) == STATUS_STATES["calls.call"][status] in STATES


# spec: ANYM-544
@pytest.mark.parametrize("status", sorted(STATUS_STATES["calls.call"]))
def test_an_unknown_dial_outcome_is_unknown_never_failed(status):
    assert state_of("calls.call", status, dial_state="unknown") == "unknown"
