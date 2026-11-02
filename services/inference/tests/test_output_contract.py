import json

import pytest
from deedkit_inference.segments import MAX_SEGMENTS, TranscriptTooLarge, audio_result, chat_content
from engine_support import engines, vis
from support import fk

pytestmark = pytest.mark.anyio


def inside_the_client_bounds(segments, text):
    """The rules of `normalize_result(..., audio=True)` (enrichment_providers.py:128-141), restated."""
    assert len(segments) <= 20000
    total = 0
    for segment in segments:
        start, end, words = segment["start"], segment["end"], segment["text"]
        assert type(start) in (int, float) and type(end) in (int, float) and 0 <= start <= end <= 86400
        assert isinstance(words, str) and len(words) <= 10000 and "\x00" not in words
        total += len(words)
    assert total <= 1_000_000 and len(text) <= 1_000_000


# spec: ANYM-683
def test_four_hour_segments_stay_inside_normalize_result_bounds():
    # 4 h of speech cut into 30,000 short pieces, one with a NUL and one over the per-segment limit.
    raw = [{"start": i * 0.48, "end": i * 0.48 + 0.4, "text": f"слово {i}"} for i in range(30000)]
    raw[5]["text"] = "bad\x00text"
    raw[6] = {"start": 3.0, "end": 9.0, "text": "w " * 7000}
    result = audio_result(raw, "ru", 14400.0)
    assert len(result["segments"]) <= MAX_SEGMENTS < 30000
    inside_the_client_bounds(result["segments"], result["text"])
    assert result["segments"][0]["start"] == 0.0 and result["segments"][-1]["end"] <= 14400.0
    # Non-finite and out-of-range times are brought inside, not passed on.
    odd = audio_result([{"start": -5, "end": float("inf"), "text": "a"}, {"start": 1e9, "end": 5, "text": "b"}], None, 1.0)
    inside_the_client_bounds(odd["segments"], odd["text"])


# spec: ANYM-683
async def test_four_hour_recording_through_the_service(env):
    async with env.running(**engines(env)) as s:
        reply = await s.transcribe(fk(language="ru", segments=25000, duration=14400),
                                   extra_fields={"language": "ru", "response_format": "verbose_json"})
        body = reply.json()
        assert reply.status == 200 and body["duration"] == 14400.0
        inside_the_client_bounds(body["segments"], body["text"])
        assert 0 < len(body["segments"]) <= MAX_SEGMENTS


# spec: ANYM-683
def test_transcript_over_the_total_limit_is_422_not_truncated_in_the_segmenter():
    with pytest.raises(TranscriptTooLarge):
        audio_result([{"start": i, "end": i + 1, "text": "x" * 9000} for i in range(120)], "en", 200.0)


# spec: ANYM-683
async def test_transcript_over_the_total_limit_is_422_not_truncated(env):
    async with env.running(**engines(env)) as s:
        reply = await s.transcribe(fk(language="en", segments=150, chars=9000), extra_fields={"language": "en"})
        assert reply.status == 422 and reply.code == "transcript_too_large"
        # Just inside the limit: served whole.
        reply = await s.transcribe(fk(language="en", segments=100, chars=9000), extra_fields={"language": "en"})
        assert reply.status == 200 and len(reply.json()["text"]) >= 900000


# spec: ANYM-683
async def test_chat_content_for_the_enrichment_prompt_is_json_with_required_keys(env):
    fenced = "```json\n" + json.dumps({"title": "Receipt", "summary": "A shop receipt", "document_type": "r\x07" + "t" * 200,
                                       "text": "Total 12.50", "extra": 1}) + "\n```"
    async with env.running(**engines(env)) as s:
        for answer in (fenced, "just words, no JSON at all", '{"title": 5, "summary": null}', ""):
            reply = await s.chat(vis(answer=answer))
            assert reply.status == 200, answer
            content = json.loads(reply.json()["choices"][0]["message"]["content"])
            assert set(content) == {"title", "summary", "document_type", "text"}
            assert all(isinstance(v, str) for v in content.values())
            assert len(content["document_type"]) <= 80 and not any(ord(c) < 32 for c in content["document_type"])
        shaped = json.loads((await s.chat(vis(answer=fenced))).json()["choices"][0]["message"]["content"])
        assert shaped["title"] == "Receipt" and shaped["text"] == "Total 12.50"
        prose = json.loads((await s.chat(vis(answer="just words"))).json()["choices"][0]["message"]["content"])
        assert prose["text"] == "just words" and prose["document_type"] == "unknown"
        # A text-only chat (the document kind sends text and no image) is served by the same alias.
        from support import chat_body

        reply = await s.chat(chat_body(text='FAKE {"answer": "{\\"title\\": \\"Doc\\"}"}'))
        assert reply.status == 200
        assert json.loads(reply.json()["choices"][0]["message"]["content"])["title"] == "Doc"


# spec: ANYM-683
def test_chat_content_unit():
    assert json.loads(chat_content('noise {"title": "A", "text": "B"} trailing'))["title"] == "A"
    value = json.loads(chat_content("[1, 2]"))
    assert value["text"] == "[1, 2]" and value["document_type"] == "unknown"


# spec: ANYM-683
def test_chat_content_unescapes_a_double_escaped_text_field():
    # The model sometimes double-escapes in JSON mode: the field holds a literal backslash-n, not a newline.
    double = json.dumps({"title": "R", "text": "Shop\\nTotal 12.50\\nThanks"})
    assert json.loads(chat_content(double))["text"] == "Shop\nTotal 12.50\nThanks"
    # Only when the field has no real newline and at least two literal ones: real text is left alone.
    mixed = json.dumps({"text": "Shop\nTotal\\nThanks"})
    assert json.loads(chat_content(mixed))["text"] == "Shop\nTotal\\nThanks"
    single = json.dumps({"text": "path C:\\new"})
    assert json.loads(chat_content(single))["text"] == "path C:\\new"
    # Other fields are never touched.
    assert json.loads(chat_content(json.dumps({"title": "a\\nb\\nc"})))["title"] == "a\\nb\\nc"


# spec: ANYM-683
async def test_engine_answers_match_the_contract_goldens(env):
    from support import CONTRACT

    chat = json.loads((CONTRACT / "chat_image_engine.response.json").read_text())
    audio = json.loads((CONTRACT / "audio_transcription_verbose_engine.response.json").read_text())
    answer = "```json\n" + json.dumps({"title": "Receipt", "summary": "A shop receipt", "document_type": "receipt",
                                      "text": "Total 12.50"}) + "\n```"
    async with env.running(**engines(env)) as s:
        reply = await s.chat(vis(answer=answer))
        body = reply.json()
        body["id"], body["created"] = "chatcmpl-<request id>", 0
        assert reply.status == chat["status"] and body == chat["body"]
        reply = await s.transcribe(fk(language="ru", segments=2, duration=4.0),
                                   extra_fields={"language": "ru", "response_format": "verbose_json"})
        assert reply.status == audio["status"] and reply.json() == audio["body"]
