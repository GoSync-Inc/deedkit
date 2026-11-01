"""The engine interface. A real engine (ANYM-683) runs in a child process the scheduler spawns and kills;
this shell ships only the `fake` engine, which tests use and a production config refuses."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from ..schemas import AudioResult, ChatResult

MAX_AUDIO_SECONDS = 14400  # enrichment_providers.py:93
MAX_PIXELS = 20_000_000  # 20 megapixels, enrichment_providers.py:92


class Rejected(Exception):
    """A precheck refusal: `code` is the machine code the client sees (422)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class BadOutput(Exception):
    """An engine child printed something that is not a valid result."""


@dataclass
class JobInput:
    kind: str  # "audio" | "chat"
    directory: Path
    paths: list  # the uploaded audio file, or the decoded images (may be empty for a text-only chat)
    primary: Path  # a file that always exists: the audio file, the first image, or prompt.txt
    params: dict = field(default_factory=dict)


@dataclass
class PhaseContext:
    index: int
    count: int
    name: str
    slot: str
    job: JobInput

    @property
    def last(self):
        return self.index == self.count - 1


class Engine:
    """Subclass per engine. All methods run in the service process, so none may load a model; `argv`
    only names the child that does."""

    id = "base"

    def __init__(self, kind, options):
        self.kind, self.options = kind, options

    def precheck(self, path):
        """Raise Rejected for an input that must not reach a model (audio over 4 h, an image over 20 MP)."""

    def estimate(self, path):
        """Seconds the whole job is expected to take (audio duration times a real-time factor; a fixed
        figure for an image). `path` is the job's primary file."""
        return 1.0

    def argv(self, ctx):
        raise NotImplementedError

    def env(self):
        return {"PATH": "/usr/bin:/bin"}

    def parse(self, ctx, stdout):
        """Validate what the child printed. Only the last phase returns a result."""
        if not ctx.last:
            return None
        model = AudioResult if self.kind == "audio" else ChatResult
        try:
            return model.model_validate(json.loads(stdout))
        except (ValueError, ValidationError):
            raise BadOutput from None

    def unload(self, phase_name):
        """The scheduler evicted this engine's phase from memory; free it."""


def build_engine(spec):
    from .fake import FakeEngine

    registry = {"fake": FakeEngine}
    cls = registry.get(spec.engine)
    return None if cls is None else cls(spec.kind, spec.engine_options)
