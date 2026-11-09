"""Documents: `document-structure` (ANYM-850), Docling 2.149.0 with the layout heron and TableFormer accurate models,
OCR off, in its own venv (`services/inference/docling/`: docling needs huggingface-hub<2, the service pins 2.4.0).

The service process only looks at the first bytes of the upload (a PDF or a zip); everything that parses the file
(the zip's members, encryption, the page count, the conversion) runs in the child, under the alias's timeout and the
phase's `rss_limit_gb`. The child is started for one job and stopped when the job ends (`stop_after_job`)."""

import json
import os
import sys
from importlib import metadata
from pathlib import Path

from pydantic import ValidationError

from ..errors import ServiceError
from ..models import ModelError, phase_dir, phase_key, verify_phase
from ..schemas import DOCUMENT_MAX_CHARS, DOCUMENT_MAX_PAGES, DOCUMENT_REFUSALS, DocumentResult
from . import BadOutput, Rejected
from .pinned import PinnedEngine

RUNTIME = "deedkit_inference.engines.runtimes.docling_rt"
ENVIRONMENT = "docling"
MAGIC = (b"%PDF-", b"PK\x03\x04")
#: The packages of the Docling venv that must equal `models.json` `environments.docling`.
PACKAGES = ["docling", "docling-slim", "docling-core", "docling-ibm-models", "docling-parse", "torch", "transformers"]


def default_interpreter():
    """releases/<sha>/services/inference/src/deedkit_inference/engines/docling.py -> services/inference/docling/.venv"""
    return Path(__file__).resolve().parents[3] / "docling" / ".venv" / "bin" / "python"


def canonical(name):
    return name.lower().replace("_", "-").replace(".", "-")


_VERSIONS = {}


def installed_versions(interpreter):
    """{package: version} of the venv `interpreter` belongs to, read from its metadata (never by importing it);
    cached per site-packages modification time, so a request costs one stat."""
    root = Path(interpreter).parent.parent / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    site = root / "site-packages"
    try:
        stamp = os.stat(site).st_mtime_ns
    except OSError:
        return None
    cached = _VERSIONS.get(str(site))
    if cached is None or cached[0] != stamp:
        versions = {}
        for dist in metadata.distributions(path=[str(site)]):
            name = dist.metadata["Name"]
            if name:
                versions[canonical(name)] = dist.version
        cached = _VERSIONS[str(site)] = (stamp, versions)
    return cached[1]


class DoclingEngine(PinnedEngine):
    id = "docling"
    kind = "document"
    stop_after_job = True
    RUNTIMES = {"docling": (RUNTIME, PACKAGES)}

    def interpreter(self) -> str:
        override = self.options.get("python")
        if override is not None:
            if not (isinstance(override, str) and os.path.isabs(override)):
                raise ServiceError(503, "engine_unavailable", "the engine cannot serve this request now")
            return override
        if self.options.get("runtime") is not None:  # CI: the fake runtime runs on the service's own Python
            return sys.executable
        return str(default_interpreter())

    def extra_env(self):
        # The research run (doc-extract-11-09) saw 1.1-1.4 cores at 4 threads and the same times at 2.
        threads = str(int(self.options.get("threads", 2)))
        return {"OMP_NUM_THREADS": threads, "MKL_NUM_THREADS": threads}

    _failed_stamp = None  # the files' stat when the last full check found them wrong (Astra r1, ANYM-850)
    _verified_stamp = None  # the files' stat when the last full check passed (Sol r2)

    def unavailable(self) -> str | None:
        packages = self.packages("docling")
        problem = self.venv_problem(packages) if packages else None
        return problem or self.model_problem()

    def model_stamp(self):
        """(problem, stamp) from a stat of the phase's pinned files: present, nothing extra, the pinned sizes. No hash on
        the event loop; the full sha256 check runs in `prepare` (a thread) before every load."""
        artifacts = (self.context.models or {}).get("phases", {}).get(phase_key(self.alias, "docling"))
        if not artifacts:
            return "model_not_pinned", None
        stamp = []
        try:
            root = phase_dir(self.context.model_dir, self.alias, "docling")
            for name, entry in artifacts.items():
                if set(os.listdir(root / name)) != set(entry["files"]):
                    return "model_files_differ", None
                for file_name, pinned in entry["files"].items():
                    info = os.stat(root / name / file_name)
                    if info.st_size != pinned["size"]:
                        return "model_file_mismatch", None
                    stamp.append((name, file_name, info.st_ino, info.st_size, info.st_mtime_ns))
        except (OSError, TypeError):
            return "model_missing", None
        return None, tuple(stamp)

    def model_problem(self) -> str | None:
        problem, stamp = self.model_stamp()
        if problem is None and stamp == self._failed_stamp:
            return "model_file_mismatch"  # the full check failed on exactly these files; a new pull changes the stamp
        return problem

    def note_check(self, stamp, error):
        """Remember the outcome of a full check of `stamp`. Only a finding about the files counts: a cancelled check
        (the client left while it hashed, Sol r2) says nothing about them and changes nothing."""
        if error is None:
            self._verified_stamp, self._failed_stamp = stamp, None
        elif error.code != "cancelled":
            self._failed_stamp, self._verified_stamp = stamp, None

    def available(self) -> bool:
        """/v1/models (in a thread): advertised only after a full sha256 check of the files as they are now passed."""
        if self.unavailable() is not None:
            return False
        _, stamp = self.model_stamp()
        if stamp != self._verified_stamp:
            try:
                verify_phase(self.context.models, self.context.model_dir, self.alias, "docling")
            except ModelError as error:
                self.note_check(stamp, error)
                return False
            self.note_check(stamp, None)
        return True

    def prepare(self, phase_name, cancel=None):
        _, stamp = self.model_stamp()
        try:
            launch = super().prepare(phase_name, cancel)
        except ServiceError as error:
            if isinstance(error.__cause__, ModelError):
                self.note_check(stamp, error.__cause__)
            raise
        self.note_check(stamp, None)
        return launch

    def venv_problem(self, packages) -> str | None:
        interpreter = self.interpreter()
        if not os.path.isfile(interpreter):
            return "environment_missing"
        pins = (self.context.models or {}).get("environments", {}).get(ENVIRONMENT, {})
        installed = installed_versions(interpreter)
        if installed is None:
            return "environment_missing"
        for package in packages:
            if package not in installed:
                return "package_missing"
            if pins.get(package) != installed[package]:
                return "package_not_pinned_version"
        return None

    def revision(self) -> str | None:
        phases = (self.context.models or {}).get("phases", {})
        pins = (self.context.models or {}).get("environments", {}).get(ENVIRONMENT, {})
        parts = [f"docling:{pins['docling']}"] if "docling" in pins else []
        for name, entry in phases.get(f"{self.alias}/docling", {}).items():
            parts.append(f"{name}:{entry['revision'][:12]}")
        return ",".join(parts) or None

    def precheck(self, path):
        """Constant cost, in the service: the first bytes only. Everything else is the child's."""
        try:
            with open(path, "rb") as handle:
                head = handle.read(8)
        except OSError:
            head = b""
        if not head.startswith(MAGIC):
            raise Rejected("unsupported_format")

    def estimate(self, path):
        return float(self.options.get("fixed_seconds", 8.0))

    def job_message(self, ctx):
        return {"phase": ctx.name, "file": str(ctx.job.primary), "directory": str(ctx.job.directory),
                "max_pages": DOCUMENT_MAX_PAGES, "max_chars": DOCUMENT_MAX_CHARS}

    def runtime_options(self, phase_name):
        models = (self.context.models or {}).get("phases", {}).get(f"{self.alias}/{phase_name}", {})
        return {**super().runtime_options(phase_name),
                "repos": {name: entry["repo"] for name, entry in models.items()},
                "repo_paths": {name: entry.get("path", "") for name, entry in models.items()},
                "revisions": {name: entry["revision"] for name, entry in models.items()}}

    def parse(self, ctx, stdout):
        if not ctx.last:
            return None
        try:
            raw = json.loads(stdout)
        except ValueError:
            raise BadOutput from None
        if isinstance(raw, dict) and set(raw) == {"refused"}:
            if raw["refused"] not in DOCUMENT_REFUSALS:
                raise BadOutput
            raise ServiceError(422, raw["refused"], "the document is refused before any model ran on it")
        try:
            result = DocumentResult.model_validate(raw)
        except (ValidationError, TypeError):
            raise BadOutput from None
        if result.engine.network_attempts:
            raise BadOutput  # an engine that tried the network is a failed engine, whatever it answered
        return result
