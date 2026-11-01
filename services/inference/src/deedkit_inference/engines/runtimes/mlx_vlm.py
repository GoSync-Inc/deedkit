"""Qwen3.5-4B (MLX 4-bit) through mlx-vlm 0.7.11, loaded from a verified local directory.

Calls (mlx_vlm/utils.py:823, prompt_utils.py:985, generate/dispatch.py:1198 at v0.7.11): `load(dir)`,
`apply_chat_template(processor, model.config, prompt, num_images=n, enable_thinking=False)`, `generate(...)` with local
image paths only. no remote-code switch is ever set; the configuration was scanned for custom code before this
process started (models.py). Only runs on the Mac mini; CI uses the fake runtime."""

import gc
import json
import os

from .common import require_dir


def prompt_text(directory):
    """The system and user texts of the request, in order (the assistant turns of a transcript are not asked about)."""
    try:
        with open(os.path.join(directory, "messages.json"), encoding="utf-8") as handle:
            messages = json.load(handle)
        return "\n\n".join(m["text"] for m in messages if m["role"] in ("system", "user") and m["text"])
    except (OSError, ValueError, KeyError, TypeError):
        with open(os.path.join(directory, "prompt.txt"), encoding="utf-8") as handle:
            return handle.read()


class Runtime:
    def __init__(self, model, processor):
        self.model, self.processor = model, processor

    def run(self, job):
        from mlx_vlm import apply_chat_template, generate

        files = job["files"]
        prompt = apply_chat_template(self.processor, self.model.config, prompt_text(job["directory"]),
                                     num_images=len(files), enable_thinking=False)
        result = generate(self.model, self.processor, prompt, image=files or None, max_tokens=int(job["max_tokens"]),
                          temperature=0.0, verbose=False)
        return {"text": result.text, "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.generation_tokens,
                "finish_reason": "length" if getattr(result, "finish_reason", None) == "length" else "stop"}

    def unload(self):
        self.model = self.processor = None
        gc.collect()
        import mlx.core as mx

        mx.clear_cache()


def load(paths, options):
    from mlx_vlm import load as load_model

    model, processor = load_model(require_dir(paths["model"]))
    return Runtime(model, processor)
