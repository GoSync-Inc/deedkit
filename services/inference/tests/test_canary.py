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
def test_canary_prompt_is_the_enrichment_prompt_of_the_contract_fixtures():
    fixture = json.loads((CONTRACT / "chat_image.request.json").read_text())
    assert (CANARY / "prompt.txt").read_text() == fixture["json"]["messages"][0]["content"][0]["text"]
    assert canary.cer("абв", "абв") == 0 and canary.wer("a b c d", "a b x d") == 0.25
