"""Images and documents: `vision-default` (Qwen3.5-4B, MLX 4-bit, through mlx-vlm).

The answer's content is always the JSON object the enrichment prompt asks for (title, summary, document_type, text);
a text-only chat (the document kind sends the extracted text and no image) is served by the same model."""

import json

from pydantic import ValidationError

from .. import media
from ..schemas import ChatResult, Usage
from ..segments import chat_content
from . import MAX_PIXELS, BadOutput, Rejected
from .pinned import PinnedEngine

RUNTIME = "deedkit_inference.engines.runtimes.mlx_vlm"


class VisionEngine(PinnedEngine):
    id = "vision"
    kind = "chat"
    RUNTIMES = {"vlm": (RUNTIME, ["mlx-vlm"])}

    def precheck(self, path):
        try:
            size = media.image_size(path)
        except OSError:
            size = None
        if size is None or min(size) < 1:
            raise Rejected("image_unreadable")
        if size[0] * size[1] > MAX_PIXELS:
            raise Rejected("image_too_large")

    def estimate(self, path):
        return float(self.options.get("fixed_seconds", 8.0))

    def job_message(self, ctx):
        return {"phase": ctx.name, "files": [str(p) for p in ctx.job.paths], "directory": str(ctx.job.directory),
                "max_tokens": int(ctx.job.params.get("max_tokens", 2000))}

    def parse(self, ctx, stdout):
        if not ctx.last:
            return None
        try:
            raw = json.loads(stdout)
            return ChatResult(
                content=chat_content(raw.get("text", "")),
                finish_reason="length" if raw.get("finish_reason") == "length" else "stop",
                usage=Usage(prompt_tokens=max(0, int(raw.get("prompt_tokens") or 0)),
                            completion_tokens=max(0, int(raw.get("completion_tokens") or 0))))
        except (ValueError, TypeError, AttributeError, ValidationError):
            raise BadOutput from None
