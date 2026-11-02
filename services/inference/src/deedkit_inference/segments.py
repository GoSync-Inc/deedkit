"""Engine output made to fit the enrichment client's `normalize_result` (enrichment_providers.py:108-157).

Transcript: at most 20,000 segments, each text at most 10,000 characters, `start`/`end` real numbers with
0 <= start <= end <= 86400, no NUL anywhere, and at most 1,000,000 characters of segment text in total. A
transcript over the total is refused (`TranscriptTooLarge`), never cut. Chat: the content is a JSON object with
string `title`, `summary`, `text` and a `document_type` of at most 80 characters without control characters."""

import json
import math
import re

MAX_SEGMENTS = 20000
MAX_SEGMENT_CHARS = 10000
MAX_TOTAL_CHARS = 1_000_000
MAX_SECONDS = 86400.0
MAX_TITLE, MAX_SUMMARY, MAX_DOC_TYPE = 300, 4000, 80
FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n(.*?)\n?```\s*$", re.S)
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class TranscriptTooLarge(Exception):
    """The transcript is longer than the client accepts."""


def clean_text(value):
    return "" if value is None else str(value).replace("\x00", "")


def seconds(value, fallback=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return min(max(number, 0.0), MAX_SECONDS) if math.isfinite(number) else fallback


def split_text(text, limit):
    """Pieces of at most `limit` characters, cut at whitespace when there is any in the second half."""
    pieces = []
    while len(text) > limit:
        cut = text.rfind(" ", limit // 2, limit)
        cut = limit if cut <= 0 else cut + 1
        pieces.append(text[:cut])
        text = text[cut:]
    if text:
        pieces.append(text)
    return pieces


def normalize_segments(raw):
    """Clean and split segments so every one is inside the bounds; empty ones are dropped."""
    cleaned = []
    for item in raw:
        text = clean_text(item.get("text")).strip()
        if not text:
            continue
        start = seconds(item.get("start"))
        end = max(seconds(item.get("end"), start), start)
        pieces = split_text(text, MAX_SEGMENT_CHARS)
        for index, piece in enumerate(pieces):  # equal shares of the time for the pieces of a long segment
            low = start + (end - start) * index / len(pieces)
            high = start + (end - start) * (index + 1) / len(pieces)
            cleaned.append({"start": low, "end": high, "text": piece})
    return cleaned


def merge_to_count(segments):
    """Merge neighbours (smallest text first would be cleverer; equal groups are enough) down to MAX_SEGMENTS,
    never past MAX_SEGMENT_CHARS per segment."""
    while len(segments) > MAX_SEGMENTS:
        merged, index = [], 0
        while index < len(segments):
            current = dict(segments[index])
            nxt = segments[index + 1] if index + 1 < len(segments) else None
            if nxt is not None and len(current["text"]) + 1 + len(nxt["text"]) <= MAX_SEGMENT_CHARS:
                current = {"start": min(current["start"], nxt["start"]), "end": max(current["end"], nxt["end"]),
                           "text": current["text"] + " " + nxt["text"]}
                index += 1
            merged.append(current)
            index += 1
        if len(merged) == len(segments):
            raise TranscriptTooLarge  # nothing left to merge inside the per-segment bound
        segments = merged
    return segments


def audio_result(raw_segments, language, duration):
    """The dict an AudioResult is built from. `raw_segments` is a list of {start, end, text}."""
    duration = seconds(duration)
    segments = merge_to_count(normalize_segments(raw_segments))
    total = sum(len(s["text"]) for s in segments)
    text = " ".join(s["text"] for s in segments)
    if total > MAX_TOTAL_CHARS or len(text) > MAX_TOTAL_CHARS:
        raise TranscriptTooLarge
    return {"text": text, "language": language, "duration": duration, "segments": segments}


def _field(value, limit):
    return clean_text(value if isinstance(value, str) else ("" if value is None else json.dumps(value, ensure_ascii=False)))[:limit]


def chat_content(model_text):
    """The service's answer content for a chat: a JSON object with the four string fields. A model answer that
    is a JSON object (bare or in a code fence) keeps its fields; any other answer becomes `text`."""
    text = clean_text(model_text).strip()
    fenced = FENCE.match(text)
    candidate = fenced.group(1) if fenced else text
    parsed = None
    try:
        value = json.loads(candidate)
        parsed = value if isinstance(value, dict) else None
    except (ValueError, RecursionError):
        start, end = candidate.find("{"), candidate.rfind("}")
        if 0 <= start < end:
            try:
                value = json.loads(candidate[start:end + 1])
                parsed = value if isinstance(value, dict) else None
            except (ValueError, RecursionError):
                parsed = None
    if parsed is None:
        parsed = {"title": "", "summary": "", "document_type": "unknown", "text": text}
    document_type = CONTROL.sub(" ", _field(parsed.get("document_type", "unknown"), 1000)).strip()[:MAX_DOC_TYPE]
    body = _field(parsed.get("text"), MAX_TOTAL_CHARS)
    if "\n" not in body and body.count("\\n") >= 2:  # the model double-escaped the line breaks in JSON mode
        body = body.replace("\\n", "\n")
    return json.dumps({
        "title": _field(parsed.get("title"), MAX_TITLE),
        "summary": _field(parsed.get("summary"), MAX_SUMMARY),
        "document_type": document_type or "unknown",
        "text": body,
    }, ensure_ascii=False)
