"""Every calendar operation state maps onto exactly one rulebook `state` (ANYM-544)."""
import ast
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[4]
# The calendar image ships without the shared package; it is standard library only, so tests read it from the repo.
sys.path.insert(0, str(ROOT / "packages/platform-contracts/src"))
from platform_contracts.agent import STATES, STATUS_STATES, state_of  # noqa: E402

SOURCE = ROOT / "services/calendar/family_calendar"


def outcomes(node, assigned):
    """The values an expression can take: a string, either branch of a conditional, or a local it was assigned."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return outcomes(node.body, assigned) | outcomes(node.orelse, assigned)
    if isinstance(node, ast.Name):
        return set().union(*(outcomes(v, {}) for v in assigned.get(node.id, [])))
    return set()


def operation_states():
    """Every value the code writes to operations.state: _finish arguments, SQL literals and the queued default."""
    values = set()
    for path in SOURCE.rglob("*.py"):
        text = path.read_text()
        values |= set(re.findall(r"UPDATE operations SET state='([a-z_]+)'", text))
        tree = ast.parse(text)
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            assigned = {}
            for node in ast.walk(function):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    assigned.setdefault(node.targets[0].id, []).append(node.value)
            for node in ast.walk(function):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_finish"
                        and len(node.args) >= 2):
                    values |= outcomes(node.args[1], assigned)
    schema = (SOURCE / "service/schema.sql").read_text()
    values |= set(re.findall(r"CREATE TABLE operations\(.*?state text NOT NULL DEFAULT '([a-z_]+)'", schema, re.S))
    return values


# spec: ANYM-544
def test_every_calendar_operation_state_maps_onto_exactly_one_state():
    states = operation_states()
    assert {"queued", "running", "verified", "failed", "unknown"} <= states
    assert states == set(STATUS_STATES["calendar.operation"]), "a state without a mapping, or a removed one"
    for value in states:
        assert state_of("calendar.operation", value) in STATES


# spec: ANYM-544
@pytest.mark.parametrize("value", ["unknown", "needs_attention", "partial"])
def test_a_calendar_outcome_to_reconcile_is_never_failed(value):
    assert state_of("calendar.operation", value) == "unknown"
