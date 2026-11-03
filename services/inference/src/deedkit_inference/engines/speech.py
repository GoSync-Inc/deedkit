"""Speech to text: `audio-default`.

Phase 1 identifies the language on the first 30 s with a small multilingual Whisper (skipped when the request names
`language`, so a voice note never loads the large model just to decide). Phase 2 is one of two routes: Russian goes to
GigaAM v3 e2e-RNNT (onnx-asr, CPU slot, its own Silero VAD for audio over 60 s), every other language to Whisper
large-v3-turbo (mlx-whisper, GPU slot). Each phase is admitted on its own slot and memory (ANYM-682)."""

import json
from collections import OrderedDict

from pydantic import ValidationError

from .. import media
from ..errors import ServiceError
from ..schemas import AudioResult
from ..segments import TranscriptTooLarge, audio_result
from . import MAX_AUDIO_SECONDS, BadOutput, Rejected
from .pinned import PinnedEngine

RUNTIME_WHISPER = "deedkit_inference.engines.runtimes.mlx_whisper"
RUNTIME_ONNX = "deedkit_inference.engines.runtimes.onnx_asr"
DURATION_CACHE = 256


def primary_language(value):
    """`en-US` -> `en`; anything that is not a 2-3 letter language code -> None."""
    if not isinstance(value, str):
        return None
    head = value.split("-")[0].lower()
    return head if 2 <= len(head) <= 3 and head.isalpha() else None


class SpeechEngine(PinnedEngine):
    id = "speech"
    kind = "audio"
    RUNTIMES = {
        "langid": (RUNTIME_WHISPER, ["mlx-whisper"]),
        "whisper": (RUNTIME_WHISPER, ["mlx-whisper"]),
        "gigaam": (RUNTIME_ONNX, ["onnx-asr", "onnxruntime"]),
    }

    def __init__(self, kind, options, env):
        super().__init__(kind, options, env)
        self.durations = OrderedDict()

    def unavailable(self) -> str | None:
        return super().unavailable() or (None if self.tool("ffmpeg") and self.tool("ffprobe") else "tool_missing")

    # --- before any model ---------------------------------------------------------------------------------------
    def duration(self, path):
        key = str(path)
        if key not in self.durations:
            self.durations[key] = media.probe_duration(path, self.tool("ffprobe"))
            while len(self.durations) > DURATION_CACHE:
                self.durations.popitem(last=False)
        return self.durations[key]

    def precheck(self, path):
        try:
            seconds = self.duration(path)
        except (ValueError, OSError, TypeError):
            raise Rejected("audio_unreadable") from None
        if seconds > MAX_AUDIO_SECONDS:
            raise Rejected("audio_too_long")

    def estimate(self, path):
        return self.duration(path) * float(self.options.get("rtf", 0.04)) + float(self.options.get("load_seconds", 3.0))

    # --- phases -------------------------------------------------------------------------------------------------
    @staticmethod
    def language(params, state):
        return primary_language(params.get("language")) or state.get("language")

    def plan(self, spec, job_input, state):
        done = state.get("done", ())
        language = self.language(job_input.params, state)
        names = {p.name: p for p in spec.phases}
        sequence = []
        if language is None and "langid" in names:
            sequence.append(names["langid"])
        routed = [p for p in spec.phases if p.route]
        wanted = "ru" if language == "ru" else "other"
        chosen = next((p for p in routed if p.route == wanted), routed[-1] if routed else None)
        if chosen is not None:
            sequence.append(chosen)
        return [p for p in sequence if p.name not in done]

    def runtime_options(self, phase_name):
        options = super().runtime_options(phase_name)
        options.update(ffmpeg=self.tool("ffmpeg"), mode="langid" if phase_name == "langid" else "transcribe")
        return options

    def job_message(self, ctx):
        params = ctx.job.params
        return {"phase": ctx.name, "files": [str(ctx.job.primary)], "directory": str(ctx.job.directory),
                "duration": self.duration(ctx.job.primary), "language": self.language(params, ctx.state)}

    def parse(self, ctx, stdout):
        try:
            raw = json.loads(stdout)
            if ctx.name == "langid":
                ctx.state["language"] = primary_language(raw.get("language")) or ""
                return None
            if not ctx.last:
                return None
            duration = self.durations.pop(str(ctx.job.primary), None) or float(raw.get("duration") or 0.0)
            language = primary_language(raw.get("language")) or self.language(ctx.job.params, ctx.state) or None
            return AudioResult.model_validate(audio_result(raw.get("segments") or [], language, duration))
        except TranscriptTooLarge:
            raise ServiceError(422, "transcript_too_large", "the transcript is longer than the client accepts") from None
        except (ValueError, TypeError, AttributeError, ValidationError):
            raise BadOutput from None
