import ast
import os
import subprocess
import sys
from pathlib import Path

from deedkit_inference.engines import offline
from deedkit_inference.engines.pinned import CACHE_DIRS
from engine_support import engines
from support import fk

import pytest

pytestmark = pytest.mark.anyio
WORKER = Path(offline.__file__).with_name("worker.py")


# spec: ANYM-683
def test_offline_and_telemetry_switches_are_set_before_engine_imports():
    tree = ast.parse(WORKER.read_text())
    first_update = next(n for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                        and ast.unparse(n.value.func) == "os.environ.update")
    # Nothing but the standard library (and the dependency-free switch list) is imported before it.
    for node in tree.body:
        if node.lineno >= first_update.lineno:
            break
        if isinstance(node, ast.Import):
            assert {a.name for a in node.names} <= {"os", "sys"}
        elif isinstance(node, ast.ImportFrom):
            assert node.module == "offline" and node.level == 1
    # The engine library is only ever imported by name, after the switches.
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func) == "importlib.import_module"]
    assert imports and all(call.lineno > first_update.lineno for call in imports)
    assert ast.unparse(first_update.value.args[0]) == "OFFLINE"
    assert offline.OFFLINE["HF_HUB_OFFLINE"] == "1" and offline.OFFLINE["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert offline.OFFLINE["TRANSFORMERS_OFFLINE"] == "1" and offline.OFFLINE["DO_NOT_TRACK"] == "1"
    # No module of an engine library is imported at the top level of a runtime (only inside load/run).
    libraries = {"mlx", "mlx_vlm", "mlx_whisper", "onnx_asr", "onnxruntime", "transformers", "torch"}
    for source in (WORKER.parent / "runtimes").glob("*.py"):
        for node in ast.parse(source.read_text()).body:
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module] if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 else [])
            assert not {n.split(".")[0] for n in names} & libraries, (source.name, names)


# spec: ANYM-683
async def test_worker_launch_carries_the_offline_switches_and_private_caches(env):
    async with env.running(**engines(env)) as s:
        engine = s.service.engines["vision-default"]
        launch = engine.prepare("vlm")
        try:
            assert all(launch.env[key] == value for key, value in offline.OFFLINE.items())
            assert launch.env["HOME"] == str(launch.scratch)
            for name in CACHE_DIRS:
                assert launch.env[name].startswith(str(launch.scratch) + os.sep) and os.path.isdir(launch.env[name])
            assert set(launch.env) == {"PATH", "HOME", "LANG", *offline.OFFLINE, *CACHE_DIRS}  # no inherited variable
            assert launch.argv[1:4] == ["-I", "-m", "deedkit_inference.engines.worker"]
        finally:
            import shutil

            shutil.rmtree(launch.scratch)


# spec: ANYM-683
async def test_a_runtime_module_outside_the_package_is_refused(env):
    kwargs = engines(env)
    kwargs["aliases"]["audio-default"]["engine_options"]["runtime"] = "os"
    async with env.running(**kwargs) as s:
        reply = await s.transcribe(fk(), extra_fields={"language": "ru"})
        assert reply.status == 503 and reply.code == "engine_unavailable"
    # The worker itself refuses it too (in a process of its own: it sets the offline switches at import).
    done = subprocess.run([sys.executable, "-I", "-m", "deedkit_inference.engines.worker", "os", "{}"],
                          capture_output=True, timeout=60, check=False)
    assert done.returncode == 2
