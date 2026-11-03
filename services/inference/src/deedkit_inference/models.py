"""The pinned models (ANYM-683): `models.json`, the only download path (`pull`), and the integrity checks.

`models.json` lists per `<alias>/<phase>` every artifact the loaders consume: repository, a 40-hex revision and the
size and sha256 of every file. The server never downloads and never gives a loader anything but a verified local
path. Before every child load the files are re-hashed in full (no cache keyed on size or mtime: bytes changed in
place with a restored mtime must still be caught). Hashes establish identity, not safety, so model material that
runs code is refused as well: only safetensors, ONNX, NPZ (read by MLX, which has no pickle path, and scanned
for object arrays here) and plain text/JSON files are allowed, and a configuration naming custom code is refused."""

import ast
import hashlib
import json
import os
import re
import stat
import zipfile
from importlib import resources
from pathlib import Path

REVISION = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}/[a-z0-9_]{1,32}$")
ARTIFACT = re.compile(r"^[a-z0-9_]{1,16}$")
ALLOWED_SUFFIXES = (".safetensors", ".onnx", ".npz", ".json", ".txt", ".jinja")
#: Configuration keys that make a loader execute code from the model directory or the network.
CODE_KEYS = frozenset({"auto_map", "model_file", "custom_code", "trust_remote_code", "custom_pipelines", "code_revision"})
CONFIG_NAMES = ("config.json", "tokenizer_config.json", "processor_config.json", "preprocessor_config.json",
                "video_preprocessor_config.json", "generation_config.json")
MAX_FILE_BYTES = 16 * 1024**3
MAX_JSON_BYTES = 8 * 1024**2
CHUNK = 4 * 1024 * 1024


class ModelError(Exception):
    """A machine code, never a path or a value from a file."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code, self.detail = code, detail


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def check_file_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name) or not name.endswith(ALLOWED_SUFFIXES):
        raise ModelError("artifact_not_allowed", str(name)[:60])


def parse_models(raw):
    try:
        doc = json.loads(raw, object_pairs_hook=_unique)
        packages, phases = doc["packages"], doc["phases"]
        assert doc["version"] == 1 and set(doc) == {"version", "packages", "phases"}
        assert isinstance(packages, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in packages.items())
        assert isinstance(phases, dict) and phases
    except (ValueError, KeyError, TypeError, AssertionError):
        raise ModelError("models_invalid") from None
    for key, artifacts in phases.items():
        if not KEY.fullmatch(key) or not isinstance(artifacts, dict) or not artifacts:
            raise ModelError("models_invalid", key)
        for name, entry in artifacts.items():
            if not ARTIFACT.fullmatch(name) or not isinstance(entry, dict) or set(entry) != {"repo", "revision", "files"}:
                raise ModelError("models_invalid", key)
            if not isinstance(entry["revision"], str) or not REVISION.fullmatch(entry["revision"]):
                raise ModelError("revision_not_pinned", f"{key}/{name}")
            if not isinstance(entry["repo"], str) or not REPO.fullmatch(entry["repo"]):
                raise ModelError("models_invalid", key)
            files = entry["files"]
            if not isinstance(files, dict) or not files:
                raise ModelError("models_invalid", key)
            for file_name, info in files.items():
                check_file_name(file_name)
                ok = (isinstance(info, dict) and set(info) == {"size", "sha256"} and type(info["size"]) is int
                      and 0 <= info["size"] <= MAX_FILE_BYTES and isinstance(info["sha256"], str)
                      and SHA256.fullmatch(info["sha256"]))
                if not ok:
                    raise ModelError("models_invalid", f"{key}/{name}/{file_name}")
    return doc


def load_models(path=None):
    """The packaged pins, or (test mode) the file `path`."""
    if path is None:
        raw = resources.files("deedkit_inference").joinpath("models.json").read_bytes()
    else:
        raw = Path(path).read_bytes()
    return parse_models(raw)


def phase_key(alias, phase):
    return f"{alias}/{phase}"


def phase_dir(store, alias, phase):
    return Path(store) / alias / phase


def hash_open(fd, size_limit, cancel=None):
    digest, total = hashlib.sha256(), 0
    while True:
        if cancel is not None and cancel.is_set():
            raise ModelError("cancelled")
        chunk = os.read(fd, CHUNK)
        if not chunk:
            return digest.hexdigest(), total
        total += len(chunk)
        if total > size_limit:
            return digest.hexdigest(), total
        digest.update(chunk)


def hash_file(path, size_limit=MAX_FILE_BYTES, cancel=None):
    """Full sha256 and size of a regular file, never through a symlink. No cache of any kind."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise ModelError("model_file_missing") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ModelError("model_file_not_regular")
        return hash_open(fd, size_limit, cancel)
    finally:
        os.close(fd)


def scan_config(path):
    """Refuse a JSON configuration (at any depth) that names code to run."""
    try:
        if os.stat(path).st_size > MAX_JSON_BYTES:
            raise ModelError("config_too_large")
        value = json.loads(Path(path).read_bytes(), object_pairs_hook=_unique)
    except (OSError, ValueError, RecursionError):
        raise ModelError("config_unreadable") from None
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if CODE_KEYS & set(item):
                raise ModelError("config_names_model_code", sorted(CODE_KEYS & set(item))[0])
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def scan_npz(path):
    """An NPZ is a zip of .npy arrays. MLX reads them without pickle; this refuses object arrays and any other
    member anyway, so the file is plain numbers."""
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.filename.endswith(".npy") or "/" in info.filename or info.file_size > MAX_FILE_BYTES:
                    raise ModelError("npz_member_not_allowed")
                with archive.open(info) as member:
                    head = member.read(8)
                    if head[:6] != b"\x93NUMPY" or head[6:7] not in (b"\x01", b"\x02", b"\x03"):
                        raise ModelError("npz_member_not_allowed")
                    length = int.from_bytes(member.read(2 if head[6] == 1 else 4), "little")
                    if length > 65536:
                        raise ModelError("npz_member_not_allowed")
                    header = ast.literal_eval(member.read(length).decode("latin1").strip())
                    descr = header["descr"]
                    if not isinstance(descr, str) or "O" in descr or header.get("fortran_order") not in (True, False):
                        raise ModelError("npz_object_array")
    except ModelError:
        raise
    except (OSError, zipfile.BadZipFile, ValueError, KeyError, SyntaxError, TypeError, IndexError):
        raise ModelError("npz_unreadable") from None


def scan_artifact(directory, files):
    for name in files:
        path = Path(directory) / name
        if name.endswith(".json") and name in CONFIG_NAMES:
            scan_config(path)
        elif name.endswith(".npz"):
            scan_npz(path)


def verify_artifact(directory, entry, cancel=None):
    """Every pinned file present with the pinned size and sha256, nothing else in the directory."""
    directory = Path(directory)
    try:
        info = os.lstat(directory)
        present = set(os.listdir(directory))
    except OSError:
        raise ModelError("model_missing") from None
    if not stat.S_ISDIR(info.st_mode):
        raise ModelError("model_missing")
    if present != set(entry["files"]):
        raise ModelError("model_files_differ")
    for name, pinned in entry["files"].items():
        digest, size = hash_file(directory / name, pinned["size"], cancel)
        if size != pinned["size"] or digest != pinned["sha256"]:
            raise ModelError("model_file_mismatch", name)
    scan_artifact(directory, entry["files"])


def verify_phase(models, store, alias, phase, cancel=None):
    """The paths of a phase's artifacts, after the full check; ModelError otherwise."""
    artifacts = models["phases"].get(phase_key(alias, phase))
    if artifacts is None:
        raise ModelError("model_not_pinned")
    root = phase_dir(store, alias, phase)
    if not root.is_dir() or root.is_symlink():
        raise ModelError("model_missing")
    if set(os.listdir(root)) != set(artifacts):
        raise ModelError("model_files_differ")
    paths = {}
    for name, entry in artifacts.items():
        verify_artifact(root / name, entry, cancel)
        paths[name] = root / name
    return paths


def verify_all(models, store):
    """[(alias/phase, None | code)] for every pinned phase; the CLI `verify` prints it."""
    result = []
    for key in sorted(models["phases"]):
        alias, phase = key.split("/")
        try:
            verify_phase(models, store, alias, phase)
            result.append((key, None))
        except ModelError as error:
            result.append((key, error.code + (f":{error.detail}" if error.detail else "")))
    return result
