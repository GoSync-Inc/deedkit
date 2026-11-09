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


RECORDED = Path(__file__).resolve().parents[1] / "services" / "inference" / "tests" / "canary" / "documents" / "recorded.json"
SCHEMAS = Path(__file__).resolve().parents[1] / "services" / "inference" / "src" / "deedkit_inference" / "schemas.py"


def recorded_results():
    """ANYM-850's recorded real Docling answers for the synthetic canary set, shaped as the route's DocumentResult."""
    engine = {"name": "docling", "version": "2.55.1", "models": {"layout": "a" * 40, "tableformer": "b" * 40}}
    for name, value in json.loads(RECORDED.read_text()).items():
        yield name, {"markdown": value["markdown"], "truncated": False, "page_spans": value["page_spans"],
                     "coverage": value["coverage"],
                     "engine": {**engine, "network_attempts": value["network_attempts"], "peak_rss_mb": value["peak_rss_mb"]}}


# spec: ANYM-851
def test_the_messaging_copy_of_the_document_contract_keeps_the_service_limits():
    import re
    from messaging_service import enrichment_providers as providers
    source = SCHEMAS.read_text()
    for name in ("DOCUMENT_MAX_BYTES", "DOCUMENT_MAX_PAGES", "DOCUMENT_MAX_CHARS"):
        expression = re.search(rf"^{name} = (.+)$", source, re.M)[1]
        assert eval(expression, {}) == getattr(providers, name), name  # the service's literal, e.g. 32 * 1024 * 1024
    assert eval(re.search(r"^DOCUMENT_REFUSALS = (\(.+\))$", source, re.M)[1], {}) == providers.DOCUMENT_REFUSALS


# spec: ANYM-851
def test_recorded_docling_answers_pass_the_real_normalize_document():
    from messaging_service.enrichment_providers import normalize_document
    results = dict(recorded_results())
    assert set(results) == {"text.pdf", "table.pdf", "doc.docx", "deck.pptx"}
    for name, value in results.items():
        clean = normalize_document(value)
        assert clean["markdown"] == value["markdown"] and clean["metadata"]["page_spans"] == value["page_spans"], name
        assert clean["metadata"]["coverage"] == {"pages": value["coverage"]["pages"], **value["coverage"]}
        assert clean["engine"]["version"] == "2.55.1" and "network_attempts" not in clean["engine"]
    value = results["table.pdf"]
    for bad in ({**value, "extra": 1}, {**value, "truncated": 0}, {**value, "markdown": "a\x00b"},
                {**value, "page_spans": [{"page": 1, "start": 0, "end": len(value["markdown"]) + 1}]},
                {**value, "page_spans": [{"page": 2, "start": 0, "end": 1}, {"page": 1, "start": 2, "end": 3}]},
                {**value, "coverage": {**value["coverage"], "tables": True}},
                {**value, "engine": {**value["engine"], "network_attempts": 1}},
                {**value, "engine": {**value["engine"], "name": "markitdown"}},
                {**value, "engine": {**value["engine"], "models": {"layout": "short"}}}):
        with pytest.raises(ModelError, match="invalid_model_response"):
            normalize_document(bad)
