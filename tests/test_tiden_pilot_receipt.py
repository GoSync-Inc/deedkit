"""Preservation of the 2026-10-26 Archive & Search pilot's receipt."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess

import pytest

RECEIPT = Path(__file__).with_name("fixtures") / "tiden-anym-508-links.json"


def main_links(case: dict) -> set[str]:
    env = os.environ
    command = ["tiden", "test", "links", case["id"], "--branch", "main", "--format", "json", "--timeout", "10s"]
    if env.get("TIDEN_PRODUCT_ID"):
        command.extend(["--product-id", env["TIDEN_PRODUCT_ID"]])
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ValueError(f"Tiden CLI exited {result.returncode}")
        response = json.loads(result.stdout)
        if response["readOnly"] is not False or not isinstance(response["links"], list):
            raise ValueError("expected main links")
        return {link["requirementId"] for link in response["links"]}
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        raise AssertionError(f"Could not read Tiden main links for {case['signature']} ({case['id']})") from None


# spec: ANYM-508
@pytest.mark.skipif(
    os.environ.get("DEEDKIT_TIDEN_PILOT") != "1",
    reason="[tiden:host-required] Run only with authenticated Tiden CLI (DEEDKIT_TIDEN_PILOT=1)",
)
def test_tiden_main_retains_the_archive_search_pilot_links():
    receipt = json.loads(RECEIPT.read_text())
    cases, specs = receipt["cases"], receipt["specs"]
    pairs = {(case["id"], spec) for case in cases for spec in case["specs"]}
    assert len(cases) == len({case["id"] for case in cases}) == 62
    assert len(pairs) == sum(len(case["specs"]) for case in cases) == 86
    assert {spec for _, spec in pairs} == set(specs) and len(set(specs.values())) == 20

    with ThreadPoolExecutor(max_workers=6) as readers:
        missing = [
            f"{spec} <- {case['signature']} ({case['id']})"
            for case, actual in zip(cases, readers.map(main_links, cases), strict=True)
            for spec in case["specs"]
            if specs[spec] not in actual
        ]
    assert not missing, "Pilot receipt links absent from Tiden main:\n" + "\n".join(missing)
