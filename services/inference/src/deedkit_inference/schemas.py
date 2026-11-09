"""Strict request and engine-result schemas (the contract fixtures validate against these)."""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

MAX_TOKENS_LIMIT = 16000
MAX_MESSAGES = 32
MAX_IMAGES = 5
DATA_URL = re.compile(r"^data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/]*={0,2})$")
LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")
ALIAS = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class TextPart(Strict):
    type: Literal["text"]
    text: Annotated[str, StringConstraints(max_length=400_000)]


class ImageUrl(Strict):
    url: str

    @field_validator("url")
    @classmethod
    def data_url_only(cls, value):
        if not DATA_URL.fullmatch(value):
            raise ValueError("image_url must be a base64 data URL of png, jpeg, webp or gif")
        return value


class ImagePart(Strict):
    type: Literal["image_url"]
    image_url: ImageUrl


class Message(Strict):
    role: Literal["system", "user", "assistant"]
    content: str | Annotated[list[Annotated[TextPart | ImagePart, Field(discriminator="type")]], Field(min_length=1)]


class ChatRequest(Strict):
    model: str
    messages: Annotated[list[Message], Field(min_length=1, max_length=MAX_MESSAGES)]
    max_tokens: Annotated[int, Field(ge=1, le=MAX_TOKENS_LIMIT, strict=True)] | None = None

    def image_count(self):
        return sum(
            1 for m in self.messages if isinstance(m.content, list) for p in m.content if p.type == "image_url"
        )


class TranscriptionForm(Strict):
    model: str
    language: str | None = None
    response_format: Literal["json", "verbose_json"] = "json"

    @field_validator("language")
    @classmethod
    def language_shape(cls, value):
        if value is not None and not LANGUAGE.fullmatch(value):
            raise ValueError("language")
        return value


class Segment(Strict):
    start: float
    end: float
    text: str


class AudioResult(Strict):
    """What an audio engine child prints on stdout."""

    text: str
    language: str | None = None
    duration: float = Field(ge=0)
    segments: list[Segment] = Field(default_factory=list, max_length=20000)


class Usage(Strict):
    prompt_tokens: int = Field(ge=0, default=0)
    completion_tokens: int = Field(ge=0, default=0)


class ChatResult(Strict):
    """What a chat engine child prints on stdout."""

    content: str
    finish_reason: Literal["stop", "length"] = "stop"
    usage: Usage = Field(default_factory=Usage)


class DocumentForm(Strict):
    model: str


# --- documents (ANYM-850): the typed result ANYM-851 stores; tests/test_inference_contract.py keeps the messaging copy
# of these limits equal. Offsets are Unicode code points into `markdown`, `start` inclusive, `end` exclusive.
DOCUMENT_MAX_BYTES = 32 * 1024 * 1024
DOCUMENT_MAX_PAGES = 100
DOCUMENT_MAX_CHARS = 1_000_000
DOCUMENT_REFUSALS = ("unsupported_format", "page_limit", "invalid_document")


class PageSpan(Strict):
    page: int = Field(ge=1, le=DOCUMENT_MAX_PAGES, strict=True)  # 1-based: a PDF page or a PPTX slide
    start: int = Field(ge=0, le=DOCUMENT_MAX_CHARS, strict=True)
    end: int = Field(ge=0, le=DOCUMENT_MAX_CHARS, strict=True)


class Coverage(Strict):
    pages: int | None = Field(default=None, ge=0, le=DOCUMENT_MAX_PAGES)  # None: a DOCX has no pages
    pages_with_text: int = Field(ge=0, le=DOCUMENT_MAX_PAGES)
    tables: int = Field(ge=0)
    headings: int = Field(ge=0)


class EngineInfo(Strict):
    name: Literal["docling"]
    version: Annotated[str, StringConstraints(pattern=r"^[0-9][0-9A-Za-z.]{0,31}$")]
    models: dict[Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,16}$")],
                 Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]] = Field(max_length=8)
    # Outbound attempts the child's audit hook saw during this job (connect, getaddrinfo, sendto, urllib); the
    # canary requires 0, and the service refuses any other figure as a failed engine.
    network_attempts: int = Field(ge=0)
    # The child's peak resident memory for this job, model load included (one child per job); the canary bounds it.
    peak_rss_mb: int = Field(ge=0, le=1_048_576)


class DocumentResult(Strict):
    """What the document engine child prints on stdout, and the body of POST /v1/documents/convert."""

    markdown: Annotated[str, StringConstraints(max_length=DOCUMENT_MAX_CHARS)]
    truncated: bool
    page_spans: list[PageSpan] = Field(max_length=DOCUMENT_MAX_PAGES)
    coverage: Coverage
    engine: EngineInfo

    @field_validator("page_spans")
    @classmethod
    def ordered(cls, spans, info):
        markdown = info.data.get("markdown")
        previous_end, previous_page = 0, 0
        for span in spans:
            if span.start < previous_end or span.end < span.start or span.page <= previous_page:
                raise ValueError("page_spans_not_ordered")
            if markdown is not None and span.end > len(markdown):
                raise ValueError("page_span_past_markdown")
            previous_end, previous_page = span.end, span.page
        return spans


def document_response(result):
    return result.model_dump()


def chat_response(job_id, created, alias, result):
    usage = result.usage
    return {
        "id": f"chatcmpl-{job_id}",
        "object": "chat.completion",
        "created": created,
        "model": alias,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.content},
                "finish_reason": result.finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.prompt_tokens + usage.completion_tokens,
        },
    }


def audio_response(result, verbose):
    if not verbose:
        return {"text": result.text}
    return {
        "task": "transcribe",
        "language": result.language,
        "duration": result.duration,
        "text": result.text,
        "segments": [
            {"id": i, "start": s.start, "end": s.end, "text": s.text} for i, s in enumerate(result.segments)
        ],
    }
