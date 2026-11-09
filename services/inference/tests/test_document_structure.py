"""ANYM-850: `POST /v1/documents/convert` and the `document-structure` alias (Docling) on the fake runtime, the pins of
its own venv and models, and the runtime's pure parts (format sniffing, page spans, the cut)."""

import io
import json
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from deedkit_inference import models as pins
from deedkit_inference.config import AliasSpec
from deedkit_inference.engines import docling as docling_engine
from deedkit_inference.engines.runtimes import docling_rt
from deedkit_inference.errors import ServiceError
from deedkit_inference.pull import pull
from deedkit_inference.schemas import DOCUMENT_MAX_BYTES, DocumentResult
from engine_support import engines, flip_in_place, sha256, synthetic_store
from support import fk, multipart, principal

pytestmark = pytest.mark.anyio
ALIAS = "document-structure"


def pdf(**directive):
    """A file the service takes for a PDF (its magic bytes), steering the fake runtime."""
    return b"%PDF-1.7 FAKE " + json.dumps(directive).encode() + b"\n%%EOF\n"


async def convert(s, data, *, model=ALIAS, key=None, **kw):
    body, content_type = multipart({"model": model}, data, "document.pdf")
    extra = {"key": key} if key else {}
    return await s.request("POST", "/v1/documents/convert", body=body, headers={"content-type": content_type},
                           **extra, **kw)


def busy_docling(s):
    worker = s.service.engines[ALIAS].workers.get("docling")
    return worker.proc.pid if worker is not None and worker.alive() else None


# spec: ANYM-850
async def test_a_document_comes_back_as_markdown_with_page_spans_and_no_worker_stays(env):
    async with env.running(**engines(env)) as s:
        reply = await convert(s, pdf(pages=3))
        assert reply.status == 200, reply.json()
        body = DocumentResult.model_validate(reply.json())  # the typed contract ANYM-851 stores
        assert [span.page for span in body.page_spans] == [1, 2, 3]
        assert body.markdown[body.page_spans[1].start:body.page_spans[1].end].startswith("## Страница 2")
        assert body.coverage.pages == 3 and body.engine.name == "docling" and body.engine.network_attempts == 0
        # One child per job: nothing of it stays loaded or in the resident book once the job is over.
        await s.until(lambda: not s.service.engines[ALIAS].workers)
        assert (ALIAS, "docling") not in s.service.admission.loaded
        second = await convert(s, pdf(pages=1))
        assert second.status == 200 and "| 1 | 1 |" in second.json()["markdown"]  # a fresh child: its first job


# spec: ANYM-850
async def test_the_route_needs_the_document_scope(env):
    others = principal("other", keys=(("k-audio-only-0123456789abcdef0123456789", "2099-01-01T00:00:00Z"),),
                       scopes=("audio", "image"))
    async with env.running(principals=[principal(), others], **engines(env)) as s:
        reply = await convert(s, pdf(), key="k-audio-only-0123456789abcdef0123456789")
        assert reply.status == 403 and reply.code == "scope_denied"
        assert (await convert(s, pdf(), model="vision-default")).code == "unknown_model"


# spec: ANYM-850
async def test_a_file_that_is_neither_pdf_nor_zip_is_refused_before_any_child(env):
    async with env.running(**engines(env)) as s:
        for data in (b"hello, not a document", b"\xd0\xcf\x11\xe0 an encrypted office file is OLE, not a zip"):
            reply = await convert(s, data)
            assert reply.status == 422 and reply.code == "unsupported_format"
        assert not s.service.engines[ALIAS].workers


# spec: ANYM-850
async def test_child_refusals_are_final_422_and_an_unknown_one_is_an_engine_failure(env):
    async with env.running(**engines(env)) as s:
        for code in ("unsupported_format", "page_limit", "invalid_document"):
            reply = await convert(s, pdf(refused=code))
            assert reply.status == 422 and reply.code == code
        reply = await convert(s, pdf(refused="something_else"))
        assert reply.status == 502 and reply.code == "engine_failed"


# spec: ANYM-850
async def test_a_child_that_tried_the_network_or_broke_the_contract_fails(env):
    span_past_text = {"markdown": "ab", "truncated": False, "page_spans": [{"page": 1, "start": 0, "end": 3}],
                      "coverage": {"pages": 1, "pages_with_text": 1, "tables": 0, "headings": 0},
                      "engine": {"name": "docling", "version": "2.149.0", "models": {}, "network_attempts": 0, "peak_rss_mb": 100}}
    overlapping = {**span_past_text, "markdown": "abcdef",
                   "page_spans": [{"page": 1, "start": 0, "end": 4}, {"page": 2, "start": 3, "end": 6}]}
    async with env.running(**engines(env)) as s:
        for directive in ({"network_attempts": 1}, {"raw": span_past_text}, {"raw": overlapping}):
            reply = await convert(s, pdf(**directive))
            assert reply.status == 502 and reply.code == "engine_failed", directive


# spec: ANYM-850
async def test_a_document_over_32_mib_is_413(env):
    async with env.running(**engines(env)) as s:
        reply = await convert(s, b"%PDF-" + b"0" * (DOCUMENT_MAX_BYTES - 4))
        assert reply.status == 413 and reply.code == "payload_too_large"
        assert not s.service.engines[ALIAS].workers


# spec: ANYM-850
async def test_a_hung_child_answers_504_and_is_gone(env):
    async with env.running(**engines(env, timeout=1)) as s:
        reply = await convert(s, pdf(mode="hang"))
        assert reply.status == 504 and reply.code == "engine_timeout"
        await s.until(lambda: not s.service.engines[ALIAS].workers)


# spec: ANYM-850
async def test_a_child_over_its_rss_limit_is_killed_alone_while_audio_finishes(env):
    async with env.running(**engines(env)) as s:
        s.probe.rss_fn = lambda pid: 3.0 if pid == busy_docling(s) else 0.5
        audio = s.spawn(s.transcribe(fk(language="ru", sleep=1.0), extra_fields={"language": "ru"}))
        reply = await convert(s, pdf(sleep=10))
        assert reply.status == 502 and reply.code == "engine_failed"
        assert reply.headers.get("x-engine-kill-reason") == "memory_limit"
        transcript = await audio
        assert transcript.status == 200 and transcript.json()["text"] == "gigaam transcript 0"
        s.probe.rss_fn = lambda pid: 0.5
        assert (await convert(s, pdf())).status == 200  # the next conversion starts a fresh child


# spec: ANYM-850
async def test_a_crashing_child_leaves_health_and_audio_answering(env):
    async with env.running(**engines(env)) as s:
        reply = await convert(s, pdf(mode="crash"))
        assert reply.status == 502 and reply.code == "engine_failed"
        assert (await s.get("/health")).status == 200
        assert (await s.transcribe(fk(), extra_fields={"language": "ru"})).status == 200


# spec: ANYM-850
async def test_models_lists_the_alias_with_its_availability(env):
    async with env.running(**engines(env)) as s:
        data = {item["id"]: item for item in (await s.get("/v1/models")).json()["data"]}
        assert data[ALIAS]["available"] is True and data[ALIAS]["scopes"] == ["document"]
        assert data["vision-default"]["available"] is True


# spec: ANYM-850
def test_a_document_alias_needs_the_document_scope():
    spec = {"kind": "document", "scope": "image", "engine": "docling", "timeout_seconds": 120,
            "phases": [{"name": "docling", "slot": "gpu", "resident_gb": 2.5, "rss_limit_gb": 2.5}]}
    with pytest.raises(ValueError, match="kind_scope_mismatch"):
        AliasSpec.model_validate(spec)
    assert AliasSpec.model_validate({**spec, "scope": "document"}).phases[0].rss_limit_gb == 2.5


# spec: ANYM-850
def test_the_shipped_pins_name_the_docling_models_and_environment():
    shipped = pins.load_models()
    phase = shipped["phases"]["document-structure/docling"]
    assert phase["layout"]["revision"] == "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8"
    assert phase["layout"]["files"]["model.safetensors"]["sha256"].startswith("00333a43")
    assert phase["tableformer"]["path"] == "model_artifacts/tableformer/accurate"
    assert phase["tableformer"]["files"]["tableformer_accurate.safetensors"]["size"] == 212_758_388
    assert shipped["environments"]["docling"]["docling"] == "2.149.0"
    assert set(docling_engine.PACKAGES) <= set(shipped["environments"]["docling"])


def models_doc(path):
    entry = {"repo": "synthetic/model", "revision": "b" * 40, "files": {"tm_config.json": {"size": 2, "sha256": "c" * 64}}}
    if path is not None:
        entry["path"] = path
    return json.dumps({"version": 1, "packages": {}, "environments": {"docling": {"docling": "2.149.0"}},
                       "phases": {"document-structure/docling": {"tableformer": entry}}})


# spec: ANYM-850
@pytest.mark.parametrize("path", ["../escape", "/abs/olute", "a/../b", "a/b/c/d/e", "", "a//b"])
def test_an_artifact_folder_outside_its_repository_is_refused(path):
    with pytest.raises(pins.ModelError):
        pins.parse_models(models_doc(path))


# spec: ANYM-850
def test_pull_fetches_an_artifact_from_its_repository_folder(tmp_path):
    data = b"{}"
    doc = json.loads(models_doc("model_artifacts/tableformer/accurate"))
    doc["phases"]["document-structure/docling"]["tableformer"]["files"]["tm_config.json"] = {
        "size": len(data), "sha256": sha256(data)}
    models = pins.parse_models(json.dumps(doc))
    asked = []

    def fetch(repo, revision, name, destination, limit):
        asked.append((repo, revision, name))
        destination.write_bytes(data)

    assert pull(models, tmp_path / "store", fetch=fetch, min_free_gb=0) == ["document-structure/docling"]
    assert asked == [("synthetic/model", "b" * 40, "model_artifacts/tableformer/accurate/tm_config.json")]
    assert (tmp_path / "store" / "document-structure" / "docling" / "tableformer" / "tm_config.json").read_bytes() == data


def fake_venv(tmp_path, versions):
    site = tmp_path / "venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    site.mkdir(parents=True)
    for name, version in versions.items():
        info = site / f"{name.replace('-', '_')}-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    return str(python)


def engine_with(python, models, model_dir=None):
    spec = AliasSpec.model_validate({"kind": "document", "scope": "document", "engine": "docling",
                                     "timeout_seconds": 120, "engine_options": {"python": python},
                                     "phases": [{"name": "docling", "slot": "gpu", "resident_gb": 2.5}]})
    env = SimpleNamespace(alias=ALIAS, spec=spec, model_dir=model_dir, models=models,
                          work_root=Path(model_dir) if model_dir else None, testing=False)
    return docling_engine.DoclingEngine("document", spec.engine_options, env)


# spec: ANYM-850
def test_the_engine_is_unavailable_until_its_own_venv_matches_the_pins(tmp_path, monkeypatch):
    monkeypatch.setattr(docling_engine.sys, "version_info", SimpleNamespace(major=3, minor=13))
    shipped = pins.load_models()
    wanted = shipped["environments"]["docling"]
    assert engine_with(str(tmp_path / "missing" / "python"), shipped).unavailable() == "environment_missing"
    assert engine_with(fake_venv(tmp_path / "a", {**wanted, "torch": "2.14.0"}), shipped).unavailable() == \
        "package_not_pinned_version"
    partial = {k: v for k, v in wanted.items() if k != "docling-parse"}
    assert engine_with(fake_venv(tmp_path / "b", partial), shipped).unavailable() == "package_missing"
    good = engine_with(fake_venv(tmp_path / "c", wanted), shipped)
    assert good.unavailable() == "model_missing"  # the venv matches, the models are not there (Astra r1)
    assert good.interpreter().endswith("/venv/bin/python")
    assert good.revision() == "docling:2.149.0,layout:8f39ad3c0b4c,tableformer:2199320848bb"



# spec: ANYM-850
def test_the_alias_is_unavailable_while_its_models_are_missing_or_failed_their_full_check(tmp_path):
    """Astra r1: /v1/models said available with a matching venv and no model store. A stat (no hash on the event loop)
    finds missing, extra or wrongly sized files; a same-size corruption is found by the full check before the load, and
    the alias stays unavailable until the files change (a new pull)."""
    models_file, store = synthetic_store(tmp_path)
    models = json.loads(models_file.read_text())
    models["environments"] = pins.load_models()["environments"]
    python = fake_venv(tmp_path / "v", models["environments"]["docling"])
    engine = engine_with(python, models, str(store))
    assert engine.unavailable() is None
    artifact = store / ALIAS / "docling" / "model"
    first = sorted(artifact.iterdir())[0]
    (artifact / "extra.json").write_text("{}")
    assert engine.unavailable() == "model_files_differ"
    (artifact / "extra.json").unlink()
    data = first.read_bytes()
    first.write_bytes(data + b"x")
    assert engine.unavailable() == "model_file_mismatch"
    first.write_bytes(data)
    flip_in_place(first)
    assert engine.unavailable() is None  # same size: a stat cannot see it, the request's own load check would
    assert engine.available() is False  # /v1/models runs the full check first (Sol r2): never advertised
    assert engine.unavailable() == "model_file_mismatch"
    first.unlink()
    first.write_bytes(data)  # a new pull: new file, new stamp
    assert engine.unavailable() is None and engine.available() is True
    flip_in_place(first)
    with pytest.raises(ServiceError):  # the load's full check finds it too
        engine.prepare("docling")
    assert engine.unavailable() == "model_file_mismatch" and engine.available() is False
    first.unlink()
    first.write_bytes(data)
    launch = engine.prepare("docling")
    assert engine.unavailable() is None and launch.argv[0] == python
    # A check cancelled while it hashed (the client left) says nothing about the files: the alias stays available.
    gone = threading.Event()
    gone.set()
    with pytest.raises(ServiceError):
        engine.prepare("docling", cancel=gone)
    assert engine.unavailable() is None and engine.available() is True
    fresh = engine_with(python, models, str(store))
    with pytest.raises(ServiceError):
        fresh.prepare("docling", cancel=gone)
    assert fresh.unavailable() is None and fresh.available() is True
    assert engine_with(python, models, str(tmp_path / "nowhere")).unavailable() == "model_missing"


def zip_bytes(names, encrypted=False):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, "<x/>")
    data = bytearray(buffer.getvalue())
    if encrypted:  # set the "encrypted" flag bit of every central directory entry
        index = data.find(b"PK\x01\x02")
        while index >= 0:
            data[index + 8] |= 0x1
            index = data.find(b"PK\x01\x02", index + 4)
    return bytes(data)


# spec: ANYM-850
def test_the_child_tells_docx_pptx_and_refusals_apart_from_the_bytes(tmp_path):
    cases = {
        "docx": (zip_bytes(["word/document.xml", "[Content_Types].xml"]), ("docx", None)),
        "pptx": (zip_bytes(["ppt/presentation.xml"] + [f"ppt/slides/slide{i}.xml" for i in range(1, 4)]), ("pptx", 3)),
        "xlsx": (zip_bytes(["xl/workbook.xml"]), "unsupported_format"),
        "deck": (zip_bytes(["ppt/presentation.xml"] + [f"ppt/slides/slide{i}.xml" for i in range(1, 102)]), "page_limit"),
        "locked": (zip_bytes(["word/document.xml"], encrypted=True), "invalid_document"),
        "bomb": (zip_bytes([f"m/{i}.xml" for i in range(docling_rt.MAX_ZIP_MEMBERS + 1)]), "invalid_document"),
        "broken": (b"PK\x03\x04 not really a zip", "invalid_document"),
        "text": (b"plain text", "unsupported_format"),
    }
    for name, (data, expected) in cases.items():
        path = tmp_path / name
        path.write_bytes(data)
        if isinstance(expected, tuple):
            assert docling_rt.sniff(str(path), 100) == expected, name
        else:
            with pytest.raises(docling_rt.Refused) as refusal:
                docling_rt.sniff(str(path), 100)
            assert refusal.value.code == expected, name


# spec: ANYM-850
def test_a_pipeline_failure_or_an_outbound_attempt_fails_the_engine_and_only_bad_input_is_refused(tmp_path, monkeypatch):
    """Astra r1: Docling with `raises_on_error=False` answers FAILURE also when its pipeline raised on a valid file;
    that is a 502, not a final 422. And a refusal must not hide an outbound attempt the audit hook recorded."""
    path = tmp_path / "doc.docx"
    path.write_bytes(zip_bytes(["word/document.xml"]))
    job = {"file": str(path), "max_pages": 100, "max_chars": 1000}

    class Converter:
        def __init__(self, status, valid=True):
            self.result = SimpleNamespace(status=SimpleNamespace(value=status), input=SimpleNamespace(valid=valid),
                                          document=FakeDocument(None, labels=("title",)))

        def convert(self, stream, **_):
            return self.result

    def runtime(status, valid=True):
        return docling_rt.Runtime(Converter(status, valid), {"layout": "a" * 40}, "2.149.0", lambda **kw: kw)

    monkeypatch.setattr(docling_rt, "peak_rss_mb", lambda: 900)
    monkeypatch.setattr(docling_rt, "ATTEMPTS", [0])
    answer = runtime("success").run(job)
    assert answer["markdown"] == "# Whole document" and answer["engine"]["network_attempts"] == 0
    DocumentResult.model_validate(answer)
    assert runtime("failure", valid=False).run(job) == {"refused": "invalid_document"}
    assert runtime("skipped").run(job) == {"refused": "unsupported_format"}
    for status in ("failure", "pending", "started"):  # a valid input whose pipeline failed
        with pytest.raises(docling_rt.EngineFailure):
            runtime(status).run(job)
    monkeypatch.setattr(docling_rt, "ATTEMPTS", [1])
    for status, valid in (("success", True), ("failure", False)):
        with pytest.raises(docling_rt.EngineFailure):
            runtime(status, valid).run(job)
    (tmp_path / "x.xlsx").write_bytes(zip_bytes(["xl/workbook.xml"]))  # a sniff refusal is checked as well
    with pytest.raises(docling_rt.EngineFailure):
        runtime("success").run({**job, "file": str(tmp_path / "x.xlsx")})


class FakeDocument:
    def __init__(self, pages, tables=0, labels=()):
        self.parts = pages
        self.pages = {n: object() for n in range(1, len(pages) + 1)} if pages is not None else {}
        self.tables = [object()] * tables
        self.texts = [SimpleNamespace(label=SimpleNamespace(value=label)) for label in labels]

    def export_to_markdown(self, page_no=None):
        return self.parts[page_no - 1] if page_no else "# Whole document"


# spec: ANYM-850
def test_page_spans_are_code_point_offsets_and_the_cut_clips_them():
    document = FakeDocument(["## Ёлка — страница", "", "третья"], tables=1, labels=("section_header", "text", "title"))
    markdown, truncated, spans, coverage = docling_rt.assemble(document, 1000)
    assert markdown == "## Ёлка — страница\n\n\n\nтретья" and not truncated
    assert [markdown[s["start"]:s["end"]] for s in spans] == ["## Ёлка — страница", "", "третья"]
    assert len("## Ёлка — страница".encode()) != spans[0]["end"]  # code points, not bytes
    assert coverage == {"pages": 3, "pages_with_text": 2, "tables": 1, "headings": 2}
    cut, truncated, spans, _ = docling_rt.assemble(document, 10)
    assert truncated and len(cut) == 10 and spans == [{"page": 1, "start": 0, "end": 10}]
    DocumentResult.model_validate({"markdown": cut, "truncated": True, "page_spans": spans, "coverage": coverage,
                                   "engine": {"name": "docling", "version": "2.149.0", "models": {},
                                              "network_attempts": 0, "peak_rss_mb": 1900}})
    whole, _, spans, coverage = docling_rt.assemble(FakeDocument(None), 1000)  # a DOCX has no pages
    assert whole == "# Whole document" and spans == [] and coverage["pages"] is None


# spec: ANYM-850
def test_the_runtime_imports_no_engine_library_and_no_network_client_at_its_top():
    import ast
    from pathlib import Path

    source = Path(docling_rt.__file__).read_text()
    top = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            top |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            top.add(node.module.split(".")[0])
    assert not top & {"docling", "docling_core", "docling_ibm_models", "pypdfium2", "torch", "transformers",
                      "huggingface_hub", "urllib", "http", "requests", "socket"}
    assert "sys.addaudithook(_audit)" in source.split("def load(")[1].split("from importlib")[0]
