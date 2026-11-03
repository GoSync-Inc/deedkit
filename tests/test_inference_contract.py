import json
from pathlib import Path

import pytest

from messaging_service.enrichment_providers import ModelError, normalize_result

CONTRACT = Path(__file__).resolve().parent / "fixtures" / "inference_contract"


def goldens():
    return sorted(CONTRACT.glob("*.response.json"))


# spec: ANYM-683
def test_golden_responses_pass_the_real_normalize_result():
    chats = [p for p in goldens() if p.name.startswith("chat")]
    audios = [p for p in goldens() if p.name.startswith("audio")]
    assert chats and audios
    for path in chats:
        content = json.loads(path.read_text())["body"]["choices"][0]["message"]["content"]
        value = json.loads(content)
        assert set(value) == {"title", "summary", "document_type", "text"} and all(isinstance(v, str) for v in value.values())
        result = normalize_result(value)
        assert result["truncated"] is False and result["metadata"]["document_type"] == (value["document_type"].strip() or "unknown")
    for path in audios:
        body = json.loads(path.read_text())["body"]
        result = normalize_result(body, audio=True)
        assert result["truncated"] is False
        if "segments" in body:
            assert [s["text"] for s in result["metadata"]["segments"]] == [s["text"] for s in body["segments"]]


# spec: ANYM-683
def test_the_bounds_the_service_keeps_are_the_ones_normalize_result_enforces():
    """The limits services/inference/segments.py restates are the client's: over any of them the client refuses."""
    ok = {"text": "x", "segments": [{"start": 0, "end": 86400, "text": "x" * 10000}]}
    normalize_result(ok, audio=True)
    for bad in ({"start": 0, "end": 86400.5, "text": "x"}, {"start": 2, "end": 1, "text": "x"},
                {"start": 0, "end": 1, "text": "x" * 10001}, {"start": 0, "end": 1, "text": "a\x00b"},
                {"start": True, "end": 1, "text": "x"}):
        with pytest.raises(ModelError):
            normalize_result({"text": "x", "segments": [bad]}, audio=True)
    with pytest.raises(ModelError):
        normalize_result({"text": "x", "segments": [{"start": 0, "end": 1, "text": "x"}] * 20001}, audio=True)
    with pytest.raises(ModelError):  # the total over all segments: 101 segments of 10,000 characters
        normalize_result({"text": "x", "segments": [{"start": 0, "end": 1, "text": "x" * 10000}] * 101}, audio=True)
    with pytest.raises(ModelError):
        normalize_result({"title": "t", "summary": "s", "text": "t", "document_type": "d" * 81})
    with pytest.raises(ModelError):
        normalize_result({"title": "t", "summary": "s", "text": "t", "document_type": "a\x01b"})
