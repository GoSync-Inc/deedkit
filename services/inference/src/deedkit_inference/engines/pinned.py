"""The base of the real engines: pinned models, pinned packages, one scratch directory per warm worker.

Only this layer decides what a worker may see: it passes the worker verified local paths (never a repository id),
an environment with the offline and telemetry switches and every cache directory inside the worker's scratch
directory, and nothing else of the service's environment."""

import json
import sys
import uuid
from importlib import metadata

from .. import media
from ..errors import ServiceError
from ..models import ModelError, verify_phase
from . import Engine, WorkerLaunch
from .offline import OFFLINE

RUNTIME_PREFIX = "deedkit_inference.engines.runtimes."
CACHE_DIRS = {
    "XDG_CACHE_HOME": "cache", "HF_HOME": "hf", "HF_HUB_CACHE": "hf/hub", "TRANSFORMERS_CACHE": "hf/transformers",
    "HF_DATASETS_CACHE": "hf/datasets", "TORCH_HOME": "torch", "MPLCONFIGDIR": "mpl", "NUMBA_CACHE_DIR": "numba",
    "TMPDIR": "tmp", "TEMP": "tmp", "TMP": "tmp",
}


def unavailable_service(code="engine_unavailable", message="the engine cannot serve this request now", retry_after=None):
    return ServiceError(503, code, message, retry_after=retry_after)


class PinnedEngine(Engine):
    worker = True
    #: phase name -> (runtime module, packages it needs)
    RUNTIMES = {}

    def __init__(self, kind, options, env):
        super().__init__(kind, options)
        self.context = env
        self.alias = env.alias
        self.tools = {}

    # --- what can run -------------------------------------------------------------------------------------------
    def runtime_module(self, phase_name):
        override = self.options.get("runtime")
        if override is not None:
            if not (self.context.testing and isinstance(override, str) and override.startswith(RUNTIME_PREFIX)):
                raise unavailable_service()
            return override
        return self.RUNTIMES[phase_name][0]

    def packages(self, phase_name):
        return [] if self.options.get("runtime") is not None else self.RUNTIMES[phase_name][1]

    def tool(self, name):
        """Absolute path of ffmpeg/ffprobe: an option, else the first of the usual places."""
        if name not in self.tools:
            self.tools[name] = media.find_tool(name, self.options.get(name))
        return self.tools[name]

    def unavailable(self) -> str | None:
        pins = (self.context.models or {}).get("packages", {})
        for phase in self.context.spec.phases:
            for package in self.packages(phase.name):
                try:
                    installed = metadata.version(package)
                except metadata.PackageNotFoundError:
                    return "package_missing"
                if pins.get(package) != installed:
                    return "package_not_pinned_version"
        return None

    # --- the worker ---------------------------------------------------------------------------------------------
    def prepare(self, phase_name, cancel=None):
        try:
            paths = verify_phase(self.context.models, self.context.model_dir, self.alias, phase_name, cancel)
        except ModelError as error:
            raise unavailable_service(message="the model files are missing or do not match their pins") from error
        scratch = self.context.work_root / f"w-{uuid.uuid4().hex}"
        scratch.mkdir(mode=0o700)
        env = {"PATH": media.TOOL_PATH, "HOME": str(scratch), "LANG": "C.UTF-8", **OFFLINE}
        for name, sub in CACHE_DIRS.items():
            env[name] = str(scratch / sub)
            (scratch / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
        spec = {"paths": {name: str(path) for name, path in paths.items()},
                "options": {**self.runtime_options(phase_name), "scratch": str(scratch)}}
        argv = [sys.executable, "-I", "-m", "deedkit_inference.engines.worker", self.runtime_module(phase_name),
                json.dumps(spec)]
        return WorkerLaunch(argv, env, scratch)

    def revision(self):
        phases = (self.context.models or {}).get("phases", {})
        parts = []
        for phase in self.context.spec.phases:
            for name, entry in phases.get(f"{self.alias}/{phase.name}", {}).items():
                if name == "model":
                    parts.append(f"{phase.name}:{entry['revision'][:12]}")
        return ",".join(parts) or None

    def runtime_options(self, phase_name):
        return {"phase": phase_name, **self.options.get("runtime_options", {})}
