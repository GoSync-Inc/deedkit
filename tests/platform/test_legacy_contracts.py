"""Compare discovered backend contracts, with separately installed environments."""

import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("module", ["messaging", "calendar"])
def test_locked_backend_contract_matches_snapshot(module, tmp_path):
    source = Path(
        os.environ.get(
            "DEEDKIT_" + module.upper() + "_SOURCE",
            ROOT if module == "messaging" else ROOT / "services/calendar",
        )
    )
    python = os.environ.get(
        "DEEDKIT_" + module.upper() + "_PYTHON", str(source / ".venv/bin/python")
    )
    output = tmp_path / "captured.json"
    subprocess.run(
        [
            python,
            str(ROOT / "scripts/capture_legacy_contract.py"),
            "--module",
            module,
            "--source",
            str(source),
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
    )
    before = json.loads(
        (ROOT / "tests/platform/fixtures" / (module + "-contracts.json")).read_text()
    )
    actual = json.loads(output.read_text())
    assert actual == before
    assert actual["transport"] == {"unknown_rest_status": 404, "get_mcp_status": 405}


def test_snapshot_has_no_provider_credentials_or_runtime_data():
    for path in (ROOT / "tests/platform/fixtures").glob("*-contracts.json"):
        value = json.loads(path.read_text())
        assert value["synthetic"] is True
        assert len({t["name"] for t in value["tools"]}) == len(value["tools"])
        assert "synthetic-contract-key" not in path.read_text()


# spec: ANYM-399
def test_canonical_capture_reproduces_the_committed_messaging_snapshot(tmp_path):
    source = Path(os.environ.get("DEEDKIT_MESSAGING_SOURCE", ROOT))
    python = os.environ.get("DEEDKIT_MESSAGING_PYTHON", str(source / ".venv/bin/python"))
    output = tmp_path / "captured.json"
    subprocess.run(
        [
            python,
            str(ROOT / "scripts/capture_legacy_contract.py"),
            "--module",
            "messaging",
            "--source",
            str(source),
            "--output",
            str(output),
            "--canonical",
        ],
        check=True,
        capture_output=True,
    )
    committed = ROOT / "tests/platform/fixtures/messaging-contracts.json"
    # Regenerating the snapshot rewrites no line: refresh it with --canonical.
    assert output.read_bytes() == committed.read_bytes()
