"""The regression canary (ANYM-683): the synthetic set of `tests/canary/` through the live aliases, scored against
`expected.json`. `score` is pure and runs in CI on recorded outputs; `run` talks to a running service (the mini).
Only metadata and synthetic text pass through here."""

import base64
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .segments import MAX_SECONDS, MAX_TOTAL_CHARS

CYRILLIC = re.compile("[А-Яа-яЁё]")
JSON_KEYS = ("title", "summary", "document_type", "text")


def norm(text):
    text = text.lower().replace("ё", "е")
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.S)
    return " ".join(re.sub(r"[*#|`_\-–—«»\"“”„()\[\]{}:;!?.,/\\]", " ", text).split())


def edit_distance(a, b):
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def cer(reference, hypothesis):
    ref, hyp = norm(reference), norm(hypothesis)
    return edit_distance(ref, hyp) / max(1, len(ref))


def wer(reference, hypothesis):
    ref, hyp = norm(reference).split(), norm(hypothesis).split()
    return edit_distance(ref, hyp) / max(1, len(ref))


def chat_object(content):
    """The criterion-2 JSON of a chat answer, or None."""
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return None
    ok = isinstance(value, dict) and all(isinstance(value.get(k), str) for k in JSON_KEYS)
    return value if ok and len(value["document_type"]) <= 80 else None


def score(outputs, expected, truth):
    """The list of failures (empty when the canary passes). `outputs` is what `run` records."""
    failures = []
    for name, bound in expected["vision"].items():
        got = outputs.get("vision", {}).get(name)
        value = chat_object(got["content"]) if got else None
        if value is None:
            failures.append(f"{name}: answer is not the enrichment JSON")
            continue
        error = cer(truth[name], value["text"])
        if error > bound["max_cer"]:
            failures.append(f"{name}: character error {error:.3f} > {bound['max_cer']}")
        if got["seconds"] > expected["vision_max_seconds"]:
            failures.append(f"{name}: {got['seconds']:.1f} s > {expected['vision_max_seconds']} s")
    described = chat_object(outputs.get("describe", {}).get("content"))
    if described is None or (expected["describe"]["cyrillic"] and not CYRILLIC.search(described["summary"])):
        failures.append("describe: no Cyrillic summary")
    if chat_object(outputs.get("text_only", {}).get("content")) is None:
        failures.append("text_only: answer is not the enrichment JSON")
    for name, bound in expected["speech"].items():
        got = outputs.get("speech", {}).get(name)
        if not got:
            failures.append(f"{name}: no answer")
            continue
        error = wer(truth[bound["truth"]], got["text"])
        if error > bound["max_wer"]:
            failures.append(f"{name}: word error {error:.3f} > {bound['max_wer']}")
        if got["seconds"] / max(got["audio_seconds"], 1e-9) > expected["max_rtf"]:
            failures.append(f"{name}: real-time factor {got['seconds'] / got['audio_seconds']:.3f} > {expected['max_rtf']}")
    failures += score_long(outputs.get("long"), expected["long"])
    return failures


def score_long(got, bound):
    if not got or not got.get("segments"):
        return ["long: no segments"]
    segments, duration = got["segments"], float(got["duration"])
    problems, previous = [], 0.0
    if len(segments) < bound["min_segments"] or len(segments) > 20000:
        problems.append(f"long: {len(segments)} segments")
    total = 0
    for segment in segments:
        start, end, text = segment["start"], segment["end"], segment["text"]
        total += len(text)
        if not (0 <= start <= end <= MAX_SECONDS) or start < previous - 1.0 or len(text) > 10000 or "\x00" in text:
            problems.append("long: a segment is outside the bounds")
            break
        previous = start
    if total > MAX_TOTAL_CHARS:
        problems.append("long: transcript over the total limit")
    if segments[-1]["end"] > duration + 2 or segments[-1]["end"] < duration * 0.9:
        problems.append("long: timestamps do not cover the recording")
    return problems


# --- talking to the live service ---------------------------------------------------------------------------------
def post(url, key, body, content_type, timeout=3600):
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": content_type})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return json.loads(reply.read()), time.monotonic() - started
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"HTTP {error.code} {error.read()[:200]!r}") from None


def chat(base, key, prompt, text, image=None):
    content = [{"type": "text", "text": prompt + text}]
    if image is not None:
        mime = "image/png" if image.suffix == ".png" else "image/jpeg"
        data = base64.b64encode(image.read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
    body = {"model": "vision-default", "messages": [{"role": "user", "content": content}], "max_tokens": 2000}
    answer, seconds = post(base + "/v1/chat/completions", key, json.dumps(body).encode(), "application/json")
    return {"content": answer["choices"][0]["message"]["content"], "seconds": seconds}


def transcribe(base, key, path, verbose=False):
    boundary = uuid.uuid4().hex
    fields = {"model": "audio-default", **({"response_format": "verbose_json"} if verbose else {})}
    body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items())
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
             "Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return post(base + "/v1/audio/transcriptions", key, body, f"multipart/form-data; boundary={boundary}")


def run(base, key, directory, expected, prompt):
    """Record the canary's outputs from a running service. `directory` holds the generated set."""
    directory = Path(directory)
    outputs = {"vision": {}, "speech": {}}
    for name in expected["vision"]:
        outputs["vision"][name] = chat(base, key, prompt, "", directory / name)
    outputs["describe"] = chat(base, key, prompt, "", directory / expected["describe"]["image"])
    outputs["text_only"] = chat(base, key, prompt, "\nUNTRUSTED SOURCE TEXT:\n" + (directory / expected["text_only"]["file"]).read_text())
    for name in expected["speech"]:
        answer, seconds = transcribe(base, key, directory / f"{name}.ogg", verbose=True)
        outputs["speech"][name] = {"text": answer["text"], "seconds": seconds, "audio_seconds": answer["duration"]}
    answer, seconds = transcribe(base, key, directory / expected["long"]["file"], verbose=True)
    outputs["long"] = {"segments": [{k: s[k] for k in ("start", "end", "text")} for s in answer["segments"]],
                       "duration": answer["duration"], "seconds": seconds}
    return outputs
