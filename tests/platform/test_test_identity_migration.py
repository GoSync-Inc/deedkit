"""The source-layout migration may not silently relocate historical test cases."""

import json
from pathlib import Path
import xml.etree.ElementTree as ET

from scripts.tiden_ci import normalize_junit

ROOT = Path(__file__).resolve().parents[2]


def test_registered_case_ids_and_matcher_sources_are_unique_and_still_present():
    registry = json.loads((ROOT / "config/tiden-test-identities.json").read_text())
    assert registry["version"] == 1
    assert registry["physical_moves"] == {}  # No physical test moves accepted yet.
    cases = registry["cases"]
    assert len(cases) == 1278
    assert len({case["id"] for case in cases}) == len(cases)
    assert len({case["signature"] for case in cases}) == len(cases)
    for case in cases:
        if case.get("filePath") and not case["signature"].startswith("anymsg-ci::"):
            assert (ROOT / case["filePath"]).is_file(), case["filePath"]


def test_parameter_identity_changes_are_not_hidden_by_source_layout(tmp_path):
    path = tmp_path / "report.xml"
    path.write_text(
        '<testsuite><testcase name="test_acl[owner]" classname="tests.test_acl" file="tests/test_acl.py"/>'
        '<testcase name="test_acl[other]" classname="tests.test_acl" file="tests/test_acl.py"><skipped/></testcase></testsuite>'
    )
    xml, counts = normalize_junit(path, tmp_path, "python")
    rows = list(ET.fromstring(xml).iter("testcase"))
    assert [row.attrib["name"] for row in rows] == [
        "test_acl[owner]",
        "test_acl[other]",
    ]
    assert {row.attrib["classname"] for row in rows} == {"tests.test_acl"}
    assert counts == {"passed": 1, "skipped": 1}
