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
