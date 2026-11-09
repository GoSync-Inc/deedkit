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
    for name in list(EXPECTED["vision"]) + ["note_ru.ogg", "note_ru_noisy.ogg", "note_en.ogg", "note_ru_long.ogg", "doc_text.txt",
                                            "note_ru_opus.ogg", "note_en_opus.ogg"]:  # the last two: the worker's own Opus (ANYM-696)
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


DOCUMENTS = CANARY / "documents"
DOC_TRUTH = json.loads((DOCUMENTS / "truth.json").read_text())


def recorded():
    """What `canary.documents` recorded from the real Docling venv on 11-09 (laptop, the pinned models, network
    denied by the audit hook): every fixture a fresh child, 3.9 to 12.3 s, peak RSS 0.93 to 1.84 GB."""
    return json.loads((DOCUMENTS / "recorded.json").read_text())


# spec: ANYM-850
def test_document_canary_passes_the_recorded_docling_run_and_fails_each_regression():
    bound = EXPECTED["documents"]
    assert bound["max_seconds"] == 120 and bound["max_rss_mb"] == 2560  # the card's 120 s and 2.5 GB
    assert canary.score_documents(recorded(), DOC_TRUTH, bound) == []
    bad = recorded()
    table = bad["table.pdf"]
    table["markdown"] = "\n".join(line for line in table["markdown"].splitlines() if "D-4007" not in line)
    text = bad["text.pdf"]
    text["markdown"] = text["markdown"].replace("## Обязанности сторон", "Обязанности сторон")
    text["page_spans"][0], text["page_spans"][1] = text["page_spans"][1], text["page_spans"][0]
    deck = bad["deck.pptx"]
    deck["network_attempts"], deck["seconds"], deck["peak_rss_mb"] = 1, 121.0, 2561
    docx = bad["doc.docx"]
    docx["markdown"] = docx["markdown"].replace("маркер альфа", "marker").replace("маркер бета", "маркер альфа")
    failures = "\n".join(canary.score_documents(bad, DOC_TRUTH, bound))
    for fragment in ("table.pdf: table rows 7/8", "text.pdf: headings 2/3", "text.pdf: page spans [2, 1, 3], not pages 1..3",
                     "deck.pptx: 1 outbound attempts", "deck.pptx: 121.0 s > 120", "deck.pptx: peak RSS 2561 MB",
                     "doc.docx: markers out of order"):
        assert fragment in failures, fragment
    lost = recorded()
    lost["doc.docx"]["markdown"] = canary.CYRILLIC.sub("?", lost["doc.docx"]["markdown"])
    assert any(f.startswith("doc.docx: Russian words kept") for f in canary.score_documents(lost, DOC_TRUTH, bound))
    moved = recorded()  # a marker on the wrong slide although the spans are in order
    spans = moved["deck.pptx"]["page_spans"]
    spans[0]["end"], spans[1]["start"] = spans[0]["start"], spans[0]["start"]
    assert any(f.startswith("deck.pptx: markers not on their pages") for f in canary.score_documents(moved, DOC_TRUTH, bound))
    assert canary.score_documents({"text.pdf": {"error": "HTTP 503"}}, DOC_TRUTH, bound)[0] == "text.pdf: no answer: HTTP 503"


# spec: ANYM-850
def test_document_canary_judges_pages_by_their_numbers_and_each_fixture_by_its_own_page_count():
    """Astra r1: no spans used to pass as a DOCX, and pages shifted by ten passed as long as the markers matched by
    position. Now every fixture has its page count in expected.json and marker n must sit on page n."""
    bound = EXPECTED["documents"]
    assert set(bound["pages"]) == set(DOC_TRUTH) and bound["pages"]["doc.docx"] is None
    cases = {
        "no spans": lambda got: got.update(page_spans=[]),
        "shifted": lambda got: [span.update(page=span["page"] + 10) for span in got["page_spans"]],
        "last page left out": lambda got: got["page_spans"].pop(),
    }
    for label, change in cases.items():
        for name in ("text.pdf", "deck.pptx"):
            outputs = recorded()
            change(outputs[name])
            assert any(f.startswith(f"{name}: page spans") for f in canary.score_documents(outputs, DOC_TRUTH, bound)), (label, name)
    outputs = recorded()
    outputs["doc.docx"]["page_spans"] = [{"page": 1, "start": 0, "end": 10}]
    assert "doc.docx: page spans for a document without pages" in canary.score_documents(outputs, DOC_TRUTH, bound)
    outputs = recorded()  # the rouble sign is dropped for the PDF only: a DOCX cell without it is a lost row
    outputs["doc.docx"]["markdown"] = outputs["doc.docx"]["markdown"].replace("₽", "")
    assert "doc.docx: table rows 4/5" in canary.score_documents(outputs, DOC_TRUTH, bound)


# spec: ANYM-850
def test_document_canary_posts_each_committed_fixture_to_the_document_route(monkeypatch):
    calls = []

    def fake_post(url, key, body, content_type, timeout=3600):
        calls.append((url, body, timeout))
        if b"deck.pptx" in body:
            raise RuntimeError("HTTP 502 engine_failed")
        got = recorded()[next(n for n in DOC_TRUTH if f'filename="{n}"'.encode() in body)]
        return {"markdown": got["markdown"], "truncated": False, "page_spans": got["page_spans"], "coverage": got["coverage"],
                "engine": {"name": "docling", "network_attempts": 0, "peak_rss_mb": 900}}, 5.0

    monkeypatch.setattr(canary, "post", fake_post)
    outputs = canary.documents("http://x", "k", DOCUMENTS, DOC_TRUTH, EXPECTED["documents"])
    assert [url for url, _, _ in calls] == ["http://x/v1/documents/convert"] * 4
    assert all(b'name="model"\r\n\r\ndocument-structure\r\n' in body and timeout == 180 for _, body, timeout in calls)
    assert outputs["deck.pptx"] == {"error": "HTTP 502 engine_failed"}
    assert outputs["table.pdf"]["peak_rss_mb"] == 900 and outputs["table.pdf"]["seconds"] == 5.0
    failures = canary.score_documents(outputs, DOC_TRUTH, EXPECTED["documents"])
    assert failures == ["deck.pptx: no answer: HTTP 502 engine_failed"]


# spec: ANYM-683
def test_canary_voice_notes_are_ogg_opus_not_vorbis():
    """Live install 11-02: ffmpeg 8.1 on the mini has libopus and no libvorbis; Ogg/Opus is also what Telegram voice
    notes are, so the canary exercises the real container and codec."""
    source = (CANARY / "generate.py").read_text()
    # the clean, the noisy and the long note, and the worker's own Opus transcode argument list (ANYM-696)
    assert "libvorbis" not in source and source.count("libopus") == 4


# spec: ANYM-683
def test_canary_prompt_is_the_enrichment_prompt_of_the_contract_fixtures():
    fixture = json.loads((CONTRACT / "chat_image.request.json").read_text())
    # The fixture is the shell's request; the mini provider appends the Russian sentence (ANYM-685, PROMPT_RU), and
    # the canary sends what the worker really sends, or its describe check measures a request nobody makes.
    sentence = " Write title and summary in Russian whatever the language of the file, and copy visible text in its own language."
    assert (CANARY / "prompt.txt").read_text() == fixture["json"]["messages"][0]["content"][0]["text"] + sentence
    assert canary.cer("абв", "абв") == 0 and canary.wer("a b c d", "a b x d") == 0.25


# spec: ANYM-683
def test_canary_scores_a_double_escaped_line_break_as_a_break():
    # The model sometimes answers a JSON text field with literal backslash-n; the service passes it on untouched
    # and the canary must not count each one as a stray "n" letter.
    truth = "ООО Ветер\nИТОГО 12,50\nСпасибо"
    assert canary.cer(truth, "ООО Ветер\\nИТОГО 12,50\\nСпасибо") == 0
    assert canary.cer(truth, truth) == 0
    assert canary.cer(truth, "ООО Ветер ИТОГО 99,99 Спасибо") > 0
