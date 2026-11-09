import re
import tomllib
from pathlib import Path

from deedkit_inference import models as pins

ROOT = Path(__file__).resolve().parents[1]
MARKER = "sys_platform == 'darwin' and platform_machine == 'arm64'"
ENTRY = re.compile(r"^([a-z0-9-]+)==([0-9][0-9A-Za-z.]*); " + re.escape(MARKER) + "$")
RUNTIME_PACKAGES = {"mlx-vlm", "mlx-whisper", "onnx-asr", "onnxruntime", "huggingface-hub"}


def project():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


# spec: ANYM-683
def test_engines_group_is_darwin_only_and_pinned():
    config = project()
    group = config["dependency-groups"]["engines"]
    entries = [ENTRY.fullmatch(item) for item in group]
    assert all(entries), group  # exact version, Apple-silicon marker, nothing else
    assert {e.group(1) for e in entries} == RUNTIME_PACKAGES
    # The base dependencies and the dev group carry no engine package, so Linux CI installs none of them.
    names = {re.split(r"[=<>!~; \[]", item)[0].lower() for item in config["project"]["dependencies"]}
    assert not names & RUNTIME_PACKAGES and not any(
        re.split(r"[=<>!~; \[]", item)[0].lower() in RUNTIME_PACKAGES for item in config["dependency-groups"]["dev"])
    # Wheels only, never a source build of a third-party package on the mini: every locked package is named (the
    # project itself is built, so `no-build = true` cannot be used), and the list follows uv.lock.
    locked = tomllib.loads((ROOT / "uv.lock").read_text())["package"]
    assert sorted(config["tool"]["uv"]["no-build-package"]) == sorted(
        package["name"] for package in locked if package["name"] != "deedkit-inference")
    assert "no-build" not in config["tool"]["uv"]
    # models.json pins the same package versions the lock installs.
    assert pins.load_models()["packages"] == {e.group(1): e.group(2) for e in entries}


# spec: ANYM-850
def test_docling_environment_is_its_own_darwin_only_wheels_only_lock():
    docling = ROOT / "docling"
    config = tomllib.loads((docling / "pyproject.toml").read_text())
    lock = tomllib.loads((docling / "uv.lock").read_text())
    own = {"deedkit-inference", "deedkit-inference-docling"}
    assert config["project"]["dependencies"] == ["deedkit-inference", "docling==2.149.0"]
    assert config["tool"]["uv"]["environments"] == [MARKER] and config["tool"]["uv"]["package"] is False
    assert config["tool"]["uv"]["sources"]["deedkit-inference"] == {"path": "..", "editable": True}
    locked = [package for package in lock["package"] if package["name"] not in own]
    assert sorted(config["tool"]["uv"]["no-build-package"]) == sorted(package["name"] for package in locked)
    assert not [package["name"] for package in locked if package["name"].startswith("nvidia")]  # CPU torch only
    for package in locked:
        assert set(package["source"]) == {"registry"}, package["name"]
        assert package.get("wheels"), package["name"]
        assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", wheel["hash"]) for wheel in package["wheels"]), package["name"]
    versions = {package["name"]: package["version"] for package in locked}
    service = {package["name"]: package.get("version") for package in tomllib.loads((ROOT / "uv.lock").read_text())["package"]}
    assert versions["torch"] == service["torch"]  # the same torch as the service's own lock
    # models.json pins the versions this lock installs; the engine refuses a venv that differs (engines/docling.py).
    assert pins.load_models()["environments"]["docling"] == {
        name: versions[name] for name in pins.load_models()["environments"]["docling"]}


# spec: ANYM-683
def test_lock_for_engines_is_wheels_only():
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    versions = {package["name"]: package["version"] for package in lock["package"] if "version" in package}
    for name, version in pins.load_models()["packages"].items():
        assert versions.get(name) == version, name
    own = {"deedkit-inference"}
    for package in lock["package"]:
        if package["name"] in own:
            continue
        # Every locked package is an index package with hash-pinned wheels (a git or path source, or an sdist-only
        # package that would need a build, is not allowed).
        assert set(package["source"]) == {"registry"}, package["name"]
        assert package.get("wheels"), package["name"]
        assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", wheel["hash"]) for wheel in package["wheels"]), package["name"]
    assert lock["package"]
