import json
from pathlib import Path

from deedkit_inference import canary
from support import CONTRACT

CANARY = Path(__file__).resolve().parent / "canary"
EXPECTED = json.loads((CANARY / "expected.json").read_text())
TRUTH = {name: f"truth text of {name} " * 3 for name in EXPECTED["vision"]} | {
    "voice_ru.txt": "привет это голосовое сообщение про выходные " * 3, "voice_en.txt": "hi it is me the plumber moved our appointment " * 2}


def good_outputs():
    def answer(name):
        return {"content": json.dumps({"title": "t", "summary": "краткое описание", "document_type": "d", "text": TRUTH[name]}, ensure_ascii=False),
                "seconds": 4.0}

    plain = {"content": json.dumps({"title": "t", "summary": "описание на русском", "document_type": "d", "text": ""}, ensure_ascii=False), "seconds": 3.0}
    return {
        "vision": {name: answer(name) for name in EXPECTED["vision"]},
        "describe": plain, "text_only": plain,
        "speech": {name: {"text": TRUTH[bound["truth"]], "seconds": 2.0, "audio_seconds": 72.0} for name, bound in EXPECTED["speech"].items()},
        "long": {"duration": 1224.0, "seconds": 30.0, "segments": [{"start": i * 12.0, "end": i * 12.0 + 11.0, "text": f"фрагмент {i}"} for i in range(102)]},
    }


# spec: ANYM-683
def test_canary_scorer_passes_recorded_good_outputs_and_fails_a_regression():
    assert canary.score(good_outputs(), EXPECTED, TRUTH) == []
    # Receipt read badly, English note garbled, a slow job, no Cyrillic summary, a broken long recording.
    bad = good_outputs()
    bad["vision"]["receipt_ru.jpg"]["content"] = json.dumps({"title": "", "summary": "", "document_type": "x", "text": "totally wrong"})
    bad["vision"]["scan_ru.png"]["seconds"] = 40.0
    bad["speech"]["note_en"]["text"] = "garbled words"
    bad["speech"]["note_ru"]["seconds"] = 20.0  # real-time factor 0.28
    bad["describe"]["content"] = json.dumps({"title": "", "summary": "english only", "document_type": "x", "text": ""})
    bad["text_only"]["content"] = "not json"
    bad["long"]["segments"][3]["end"] = 90000.0
    failures = canary.score(bad, EXPECTED, TRUTH)
    joined = "\n".join(failures)
    for fragment in ("receipt_ru.jpg: character error", "scan_ru.png:", "note_en: word error", "note_ru: real-time factor",
                     "describe: no Cyrillic", "text_only:", "long: a segment is outside"):
        assert fragment in joined, fragment
    assert canary.score({}, EXPECTED, TRUTH)  # missing outputs are failures too


# spec: ANYM-683
def test_canary_loads_each_modality_once_before_the_timed_items_and_judges_the_cold_start_apart(tmp_path, monkeypatch):
    """Live run 11-02 on 9d2ccef1: the first request of each modality loads its models (vision 54 to 100 s, audio 67 s),
    and scoring it against the warm bounds failed receipt_ru and note_ru. The timed items now run warm."""
    for name in list(EXPECTED["vision"]) + ["note_ru.ogg", "note_ru_noisy.ogg", "note_en.ogg", "note_ru_long.ogg", "doc_text.txt"]:
        (tmp_path / name).write_bytes(b"x")
    calls, clock = [], {"cold": {"vision": True, "note_en": True, "note_ru": True}}

    def fake_post(url, key, body, content_type, timeout=3600):
        if url.endswith("/chat/completions"):
            label = "vision"
            seconds = 60.0 if clock["cold"].pop("vision", False) else 3.0
            reply = {"choices": [{"message": {"content": "{}"}}]}
        else:
            label = next(n for n in ("note_ru_noisy", "note_ru_long", "note_ru", "note_en") if n.encode() in body)
            seconds = 40.0 if clock["cold"].pop(label, False) else 2.0
            reply = {"text": "t", "duration": 72.0, "segments": [{"start": 0, "end": 1, "text": "t"}]}
        calls.append((label, seconds))
        return reply, seconds

    monkeypatch.setattr(canary, "post", fake_post)
    outputs = canary.run("http://x", "k", tmp_path, EXPECTED, "prompt")
    assert set(outputs["cold"]) == {"vision", "speech_en", "speech_ru"}
    assert outputs["cold"]["vision"]["seconds"] == 60.0 and outputs["cold"]["speech_en"]["seconds"] == 40.0
    # The scored items saw the warm path: every vision item and every speech item is the warm figure.
    assert {v["seconds"] for v in outputs["vision"].values()} == {3.0}
    assert {v["seconds"] for v in outputs["speech"].values()} == {2.0}
    # The warm-up of each route comes before the first timed note, English then Russian.
    labels = [label for label, _ in calls]
    assert labels[0] == "vision" and labels.index("note_en") < labels.index("note_ru")
    # The cold start has its own loose limit: a load that takes forever still fails the canary.
    slow = good_outputs() | {"cold": {"vision": {"seconds": EXPECTED["cold_max_seconds"] + 1}}}
    assert any(f.startswith("cold vision:") for f in canary.score(slow, EXPECTED, TRUTH))
    fine = good_outputs() | {"cold": {"vision": {"seconds": 99.0}, "speech_ru": {"seconds": 99.0}}}
    assert canary.score(fine, EXPECTED, TRUTH) == []


# spec: ANYM-683
def test_canary_voice_notes_are_ogg_opus_not_vorbis():
    """Live install 11-02: ffmpeg 8.1 on the mini has libopus and no libvorbis; Ogg/Opus is also what Telegram voice
    notes are, so the canary exercises the real container and codec."""
    source = (CANARY / "generate.py").read_text()
    assert "libvorbis" not in source and source.count("libopus") == 3  # the clean, the noisy and the long note


# spec: ANYM-683
def test_canary_prompt_is_the_enrichment_prompt_of_the_contract_fixtures():
    fixture = json.loads((CONTRACT / "chat_image.request.json").read_text())
    # The fixture is the shell's request; the mini provider appends the Russian sentence (ANYM-685, PROMPT_RU), and
    # the canary sends what the worker really sends, or its describe check measures a request nobody makes.
    sentence = " Write title and summary in Russian whatever the language of the file, and copy visible text in its own language."
    assert (CANARY / "prompt.txt").read_text() == fixture["json"]["messages"][0]["content"][0]["text"] + sentence
    assert canary.cer("абв", "абв") == 0 and canary.wer("a b c d", "a b x d") == 0.25
