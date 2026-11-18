"""The merge-preview schema metadata of the Tiden CLI 0.54 contract (`schemaVersion` 3 with a `previewDigest`).

The shared preview parser (scripts/tiden_pr_check.py, also used by sync B) accepts exactly that supported pair and
nothing looser; every other preview rule is unchanged. The digest is only checked for its form, never verified.
Synthetic previews and the fake Tiden CLI only; nothing calls Tiden.
"""
import pytest

from scripts.tiden_pr_check import Refused, preview_numbers
from tests.test_tiden_sync import ready_run, ready_server, server  # noqa: F401 (the fake-CLI fixture)

DIGEST = "0123456789abcdef" * 4  # 64 lowercase hex characters, synthetic


def legacy():
    """A spec-only preview of one addition and one modification, in the shape older CLIs return."""
    return {"additions": [{"seqNum": 70, "parentId": None}],
            "modifications": [{"mainVersion": {"seqNum": 60, "parentId": None},
                               "branchVersion": {"seqNum": 60, "parentId": None}}],
            "deletions": [], "stats": {"additions": 1, "modifications": 1, "conflicts": 0},
            "intentSession": None, "blockingSessions": 0, "hasRequirementChanges": True}


def schema3(**change):
    return {"schemaVersion": 3, "previewDigest": DIGEST, **legacy(), **change}


# spec: ANYM-445
def test_a_schema_3_preview_with_its_digest_reads_as_its_spec_changes():
    assert preview_numbers(schema3(), "proposal/ready") == ({60, 70}, [])


# spec: ANYM-445
def test_a_legacy_preview_without_schema_metadata_still_reads_as_before():
    assert preview_numbers(legacy(), "proposal/ready") == ({60, 70}, [])


# spec: ANYM-445
@pytest.mark.parametrize("metadata", [
    {"schemaVersion": True}, {"schemaVersion": 3.0}, {"schemaVersion": "3"}, {"schemaVersion": 2}, {"schemaVersion": 4},
    {"schemaVersion": None}, {"previewDigest": DIGEST.upper()}, {"previewDigest": DIGEST[:63]},
    {"previewDigest": DIGEST + "0"}, {"previewDigest": "g" + DIGEST[1:]}, {"previewDigest": 7}, {"previewDigest": None},
    {"previewDigest": " " + DIGEST[1:]}, {"previewDigest": DIGEST[:63] + "\n"},
], ids=lambda value: repr(value)[:40])
def test_unsupported_or_malformed_schema_metadata_refuses(metadata):
    with pytest.raises(Refused, match="schema metadata"):
        preview_numbers(schema3(**metadata), "proposal/ready")


# spec: ANYM-445
@pytest.mark.parametrize("key", ["schemaVersion", "previewDigest"])
def test_one_half_of_the_metadata_pair_alone_refuses(key):
    preview = schema3()
    del preview[key]
    with pytest.raises(Refused, match="schema metadata"):
        preview_numbers(preview, "proposal/ready")


# spec: ANYM-445
def test_supported_metadata_never_admits_an_unknown_field():
    with pytest.raises(Refused, match="unknown fields"):
        preview_numbers(schema3(previewSignature=DIGEST), "proposal/ready")


# spec: ANYM-445
@pytest.mark.parametrize("change, forbidden", [
    ({"testDuplicates": [{"match": "suite_path"}], "stats": {"additions": 1, "modifications": 1, "testDuplicates": 1}},
     ["testDuplicates", "stats.testDuplicates"]),
    ({"stats": {"additions": 1, "modifications": 1, "testAdditions": 1}}, ["stats.testAdditions"]),
    ({"autoMerge": {"state": "in_progress"}}, ["autoMerge"]),
    ({"stats": {"additions": 2, "modifications": 1}}, ["stats.additions"]),
])
def test_supported_metadata_never_admits_a_forbidden_change_class(change, forbidden):
    assert preview_numbers(schema3(**change), "proposal/ready") == (set(), forbidden)


# spec: ANYM-445
def test_a_ready_merge_of_a_schema_3_preview_merges_through_the_sync(server):  # noqa: F811
    ready_server(server, preview=schema3())
    code, out = ready_run(server)
    assert code == 0, out
    assert [entry[:2] for entry in server.writes()] == [["branch", "merge"]]
    assert out.splitlines()[-1] == "Ready merge of proposal/ready done (ANYM-60, ANYM-70)."


# spec: ANYM-445
@pytest.mark.parametrize("change, reason", [({"schemaVersion": 4}, "schema metadata"),
                                            ({"previewDigest": DIGEST.upper()}, "schema metadata"),
                                            ({"previewSignature": DIGEST}, "unknown fields (previewSignature)")],
                         ids=["version", "digest", "unknown"])
def test_a_ready_merge_of_unsupported_preview_metadata_writes_nothing(server, change, reason):  # noqa: F811
    ready_server(server, preview=schema3(**change))
    code, out = ready_run(server)
    assert code == 1 and "Failed at step preview" in out and reason in out, out
    assert server.writes() == [] and "Ready merge of" not in out
