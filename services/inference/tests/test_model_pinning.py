import ast
import json
import os
import socket
import zipfile
from pathlib import Path

import pytest
from deedkit_inference import cli
from deedkit_inference.app import create_app
from deedkit_inference import models as pins
from deedkit_inference import pull as pulling
from deedkit_inference.errors import ConfigError
from deedkit_inference.models import ModelError
from engine_support import engines, flip_in_place, sha256, synthetic_store, vis
from support import fk

pytestmark = pytest.mark.anyio
SRC = Path(pins.__file__).parent


DATA = {"config.json": b"{}", "weights.safetensors": b"w"}


def hub(tamper=None, fail_after=None):
    """A fake Hub: serves the pinned bytes (or a same-size tampered weight), and may die midway."""
    served = []

    def fetch(repo, revision, name, destination, limit):
        if fail_after is not None and len(served) >= fail_after:
            raise RuntimeError("connection lost")
        served.append(name)
        destination.write_bytes(tamper if tamper is not None and name == "weights.safetensors" else DATA[name])

    return fetch


def uniform_models(size_of_weights=1):
    entry = lambda: {"model": {"repo": "x/y", "revision": "b" * 40, "files": {  # noqa: E731
        "config.json": {"size": 2, "sha256": sha256(b"{}")},
        "weights.safetensors": {"size": size_of_weights, "sha256": sha256(b"w")}}}}
    return {"version": 1, "packages": {}, "phases": {"audio-default/gigaam": entry(), "vision-default/vlm": entry()}}


# spec: ANYM-683
def test_unpinned_revision_refused(env, tmp_path):
    bad = uniform_models()
    for revision in ("main", "abc123", "A" * 40, "a" * 39, ""):
        bad["phases"]["vision-default/vlm"]["model"]["revision"] = revision
        with pytest.raises(ModelError) as caught:
            pins.parse_models(json.dumps(bad))
        assert caught.value.code == "revision_not_pinned"
    # The service refuses to start on such a file, and the packaged pins are all 40-hex.
    path = tmp_path / "m.json"
    path.write_text(json.dumps(bad))
    env.write_policy()
    with pytest.raises(ConfigError, match="revision_not_pinned"):
        create_app(env.config(**{**engines(env), "models_file": str(path)}))
    shipped = pins.load_models()
    assert all(pins.REVISION.fullmatch(e["revision"]) for a in shipped["phases"].values() for e in a.values())


# spec: ANYM-683
def test_pull_refuses_a_tampered_file_and_publishes_nothing(tmp_path):
    models, store = pins.parse_models(json.dumps(uniform_models())), tmp_path / "store"
    with pytest.raises(ModelError) as caught:
        pulling.pull(models, store, fetch=hub(tamper=b"x"), min_free_gb=0)  # same size, other bytes
    assert caught.value.code == "model_file_mismatch"
    assert [p.name for p in store.iterdir()] == []  # no alias directory, no temporary directory left
    assert all(problem for _, problem in pins.verify_all(models, store))


# spec: ANYM-683
def test_interrupted_pull_leaves_no_served_model(tmp_path):
    models, store = pins.parse_models(json.dumps(uniform_models())), tmp_path / "store"
    with pytest.raises(RuntimeError):
        pulling.pull(models, store, fetch=hub(fail_after=1), min_free_gb=0)
    assert [p.name for p in store.iterdir()] == []
    assert all(problem == "model_missing" for _, problem in pins.verify_all(models, store))
    # A good pull afterwards publishes whole directories, and a second pull has nothing left to fetch.
    done = pulling.pull(models, store, fetch=hub(), min_free_gb=0)
    assert done == ["audio-default/gigaam", "vision-default/vlm"]
    calls = []
    pulling.pull(models, store, fetch=lambda *a: calls.append(a), min_free_gb=0)
    assert calls == []


# spec: ANYM-683
def test_pull_redirects_stay_on_the_hubs_domains():
    import urllib.error
    import urllib.request

    for good in ("https://cdn-lfs.huggingface.co/x", "https://cas-bridge.xethub.hf.co/x", "https://hf.co/x"):
        assert pulling.allowed_host(good)
    for bad in ("http://huggingface.co/x", "https://evilhuggingface.co/x", "https://huggingface.co.evil.example/x",
                "https://huggingface.co@evil.example/x", "https://example.com/x", "ftp://hf.co/x"):
        assert not pulling.allowed_host(bad), bad
    request = urllib.request.Request("https://huggingface.co/a/resolve/b/c")
    handler = pulling._HttpsRedirects()
    with pytest.raises(urllib.error.URLError):
        handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/c")


# spec: ANYM-683
async def test_same_size_tampered_weight_makes_the_alias_unavailable(env):
    kwargs = engines(env)
    async with env.running(**kwargs) as s:
        assert (await s.transcribe(fk(), extra_fields={"language": "en"})).status == 200
    target = Path(kwargs["model_dir"]) / "audio-default" / "whisper" / "model" / "weights.safetensors"
    original = target.read_bytes()
    target.write_bytes(b"X" * len(original))  # the same size, other bytes
    async with env.running(**kwargs) as s:
        reply = await s.transcribe(fk(), extra_fields={"language": "en"})
        assert reply.status == 503 and reply.code == "engine_unavailable"
        assert s.service.engines["audio-default"].workers == {}  # nothing was started on the bad file
        assert (await s.transcribe(fk(), extra_fields={"language": "ru"})).status == 200  # the other phases still serve


# spec: ANYM-683
async def test_in_place_change_with_restored_mtime_is_caught_on_the_next_load(env):
    kwargs = engines(env)
    target = Path(kwargs["model_dir"]) / "audio-default" / "gigaam" / "model" / "weights.safetensors"
    async with env.running(**kwargs) as s:
        assert (await s.transcribe(fk(), extra_fields={"language": "ru"})).status == 200  # warm
        stamp = os.stat(target).st_mtime_ns
        flip_in_place(target)
        assert os.stat(target).st_mtime_ns == stamp and os.stat(target).st_size == len(target.read_bytes())
        # Warm: the loaded worker keeps answering (the check happens at load), but the next load refuses.
        s.service.admission.evict([("audio-default", "gigaam")])
        await s.until(lambda: "gigaam" not in s.service.engines["audio-default"].workers)
        reply = await s.transcribe(fk(), extra_fields={"language": "ru"})
        assert reply.status == 503 and reply.code == "engine_unavailable"


# spec: ANYM-683
async def test_missing_auxiliary_file_makes_the_alias_unavailable(env):
    kwargs = engines(env)
    (Path(kwargs["model_dir"]) / "audio-default" / "gigaam" / "vad" / "config.json").unlink()
    async with env.running(**kwargs) as s:
        reply = await s.transcribe(fk(), extra_fields={"language": "ru"})
        assert reply.status == 503 and reply.code == "engine_unavailable"
        extra = Path(kwargs["model_dir"]) / "audio-default" / "whisper" / "model" / "extra.safetensors"
        extra.write_bytes(b"not pinned")  # a file the pins do not list is refused too
        assert (await s.transcribe(fk(), extra_fields={"language": "en"})).code == "engine_unavailable"


# spec: ANYM-683
def test_verify_detects_a_changed_file(tmp_path, capsys):
    models, store = synthetic_store(tmp_path)
    parsed = pins.load_models(models)
    assert all(problem is None for _, problem in pins.verify_all(parsed, store))
    flip_in_place(store / "vision-default" / "vlm" / "model" / "weights.safetensors")
    problems = dict(pins.verify_all(parsed, store))
    assert problems["vision-default/vlm"] == "model_file_mismatch:weights.safetensors"
    assert problems["audio-default/whisper"] is None
    # The command line: the packaged pins against an empty store fail, with no network.
    with pytest.raises(SystemExit) as caught:
        cli.main(["verify", "--model-dir", str(tmp_path / "empty")])
    assert caught.value.code == 1 and "FAIL audio-default/gigaam" in capsys.readouterr().out


# spec: ANYM-683
async def test_server_never_opens_a_network_connection_for_a_missing_model(env, monkeypatch):
    kwargs = engines(env)
    for path in Path(kwargs["model_dir"]).rglob("*.safetensors"):
        path.unlink()
    attempts = []
    monkeypatch.setattr(socket.socket, "connect", lambda self, address: attempts.append(address))
    async with env.running(**kwargs) as s:
        reply = await s.transcribe(fk(), extra_fields={"language": "ru"})
        assert reply.status == 503 and reply.code == "engine_unavailable"
    assert attempts == []
    # Only the modules that exist to download or to talk to the service may import a network client.
    clients = {"urllib.request", "http.client", "socket", "requests", "httpx", "huggingface_hub", "urllib3", "aiohttp"}
    allowed = {"pull.py", "canary.py", "egress_probe.py"}  # none of them is imported by the serving path
    for source in SRC.rglob("*.py"):
        if source.name in allowed:
            continue
        for node in ast.walk(ast.parse(source.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module] if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 else [])
            assert not clients & set(names), (source.name, names)


def npy(descr):
    header = f"{{'descr': '{descr}', 'fortran_order': False, 'shape': (1,), }}".encode()
    header += b" " * (-(len(header) + 11) % 64) + b"\n"
    return b"\x93NUMPY\x01\x00" + len(header).to_bytes(2, "little") + header + b"\x00" * 8


# spec: ANYM-683
async def test_config_naming_a_custom_model_file_is_refused_before_loading(env):
    for key in ("model_file", "auto_map", "trust_remote_code", "custom_code"):
        config = json.dumps({"architectures": ["X"], "nested": [{key: "evil.py"}]}).encode()
        kwargs = engines(env, store_extra={"vision-default/vlm/model/config.json": config})
        async with env.running(**kwargs) as s:
            reply = await s.chat(vis())
            assert reply.status == 503 and reply.code == "engine_unavailable", key
            assert s.service.engines["vision-default"].workers == {}  # the worker never started
    with pytest.raises(ModelError) as caught:
        pins.scan_config(env.tmp / "nonexistent.json")
    assert caught.value.code == "config_unreadable"


# spec: ANYM-683
def test_pickle_or_torch_checkpoint_artifacts_are_refused(tmp_path):
    for name in ("weights.bin", "model.pt", "model.pth", "state.pkl", "model.ckpt", "custom.py", "weights.so", "../x.json",
                 "a/b.json", ".hidden.json"):
        doc = uniform_models()
        doc["phases"]["vision-default/vlm"]["model"]["files"][name] = {"size": 1, "sha256": sha256(b"w")}
        with pytest.raises(ModelError) as caught:
            pins.parse_models(json.dumps(doc))
        assert caught.value.code == "artifact_not_allowed", name
    # An NPZ holding an object array (pickle) is refused, one of plain numbers is accepted.
    bad, good = tmp_path / "bad.npz", tmp_path / "good.npz"
    for path, descr in ((bad, "|O"), (good, "<f4")):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("weights.npy", npy(descr))
    with pytest.raises(ModelError) as caught:
        pins.scan_npz(bad)
    assert caught.value.code == "npz_object_array"
    pins.scan_npz(good)
    with zipfile.ZipFile(tmp_path / "other.npz", "w") as archive:
        archive.writestr("payload.pkl", b"\x80\x04N.")
    with pytest.raises(ModelError):
        pins.scan_npz(tmp_path / "other.npz")


# spec: ANYM-683
def test_no_engine_call_enables_trust_remote_code():
    for source in SRC.rglob("*.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert "trust_remote_code" not in {k.arg for k in node.keywords}, source.name
    # The name appears only in the list of refused configuration keys.
    mentions = [s.name for s in SRC.rglob("*.py") if "trust_remote_code" in s.read_text()]
    assert mentions == ["models.py"]


# spec: ANYM-683
def test_no_runtime_download_fallback_is_reachable():
    banned = {"snapshot_download", "hf_hub_download", "from_pretrained", "urlopen", "requests", "huggingface_hub",
              "get_model_path", "load_dataset", "from_hub"}
    for source in (SRC / "engines" / "runtimes").glob("*.py"):
        text = source.read_text()
        for name in banned:
            assert name not in text, (source.name, name)
    # Every loader call receives a directory checked by require_dir (a missing path would be taken for a Hub id).
    expected = {"mlx_vlm.py": ["load_model(require_dir("], "onnx_asr.py": ["load_model(\"gigaam-v3-e2e-rnnt\", path=require_dir(",
                                                                         "load_vad(\"silero\", path=require_dir("],
                "mlx_whisper.py": ["directory = require_dir("]}
    for name, fragments in expected.items():
        text = (SRC / "engines" / "runtimes" / name).read_text()
        for fragment in fragments:
            assert fragment in text, (name, fragment)
    # The pull is the one place that names the Hub.
    assert [s.name for s in SRC.rglob("*.py") if "huggingface.co" in s.read_text()] == ["pull.py"]
