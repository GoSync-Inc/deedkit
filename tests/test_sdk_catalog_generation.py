"""The generated SDK catalog remains a reproducible projection of the gateway."""
from scripts.generate_sdk_catalog import ROOT, generated


# spec: ANYM-952
def test_sdk_catalog_generation_matches_committed_files():
    first = generated(ROOT)
    assert first == generated(ROOT)
    assert all(path.read_text() == content for path, content in first.items())
