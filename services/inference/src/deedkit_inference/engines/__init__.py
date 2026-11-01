"""The engine interface. The `fake` engine (tests only; a production config refuses it) runs one child per phase.
The real engines (`speech`, `vision`, ANYM-683) keep a warm worker per phase, a child process the scheduler
spawns once, talks to by lines and kills on a timeout, a memory overrun, unload or shutdown."""

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
class EngineEnv:
    """What an engine needs from the service: its alias and spec, the model store and pins, the work directory."""

    alias: str
    spec: object
    model_dir: Path
    models: dict | None
    work_root: Path
    testing: bool = False


@dataclass
class PhaseContext:
    index: int
    count: int
    name: str
    slot: str
    job: JobInput
    state: dict = field(default_factory=dict)  # per job, shared by the phases of one job

    @property
    def last(self):
        return self.index == self.count - 1


class Engine:
    """Subclass per engine. All methods run in the service process, so none may load a model; `argv`
    only names the child that does."""

    id = "base"
    worker = False  # True: one warm worker child per phase (see workers.py)

    def __init__(self, kind, options):
        self.kind, self.options = kind, options
        # True for an engine that keeps a warm worker between jobs: it stays in the resident book while idle.
        self.persistent = bool(options.get("persistent", False)) or self.worker
        self.workers = {}  # phase name -> Worker (worker engines)

    def precheck(self, path):
        """Raise Rejected for an input that must not reach a model (audio over 4 h, an image over 20 MP)."""

    def estimate(self, path):
        """Seconds the whole job is expected to take (audio duration times a real-time factor; a fixed
        figure for an image). `path` is the job's primary file."""
        return 1.0

    def revision(self):
        """What /v1/models shows as the alias's revision: None for an engine without pinned models."""
        return None

    def plan(self, spec, job_input, state):
        """The phases still to run, given what the finished ones left in `state` (`state["done"]` lists them).
        An engine with routes (the language picks the phase) answers differently as `state` grows."""
        return list(spec.phases[len(state.get("done", ())):])

    def unavailable(self) -> str | None:
        """A reason (machine code) when this engine cannot serve now: packages missing, tools missing."""
        return None

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
        worker = self.workers.pop(phase_name, None)
        if worker is not None:
            worker.stop()

    def stop_workers(self):
        for worker in list(self.workers.values()):
            worker.kill()
        self.workers.clear()

    # --- worker engines -------------------------------------------------------------------------------------
    def prepare(self, phase_name):
        """Blocking, off the loop: verify the phase's model files in full and return a WorkerLaunch."""
        raise NotImplementedError

    def job_message(self, ctx):
        raise NotImplementedError


@dataclass
class WorkerLaunch:
    argv: list
    env: dict
    scratch: Path


def build_engine(spec, env=None):
    from .fake import FakeEngine
    from .speech import SpeechEngine
    from .vision import VisionEngine

    if spec.engine == "fake":
        return FakeEngine(spec.kind, spec.engine_options)
    cls = {"speech": SpeechEngine, "vision": VisionEngine}.get(spec.engine)
    if cls is None or env is None or spec.kind != cls.kind:
        return None
    return cls(spec.kind, spec.engine_options, env)
