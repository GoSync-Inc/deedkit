"""Docling 2.149.0 (ANYM-850): a PDF, DOCX or PPTX to Markdown with page spans, offline, OCR off.

Runs only in the Docling venv on the Mac mini (CI uses the fake runtime). The models come as verified local
directories; Docling expects them under one `artifacts_path` in its Hub folder layout
(`docling-project--docling-layout-heron/`, `docling-project--docling-models/model_artifacts/tableformer/accurate/`,
docling/models/stages/table_structure/table_structure_model.py:79-118 at v2.149.0), so `load` builds that layout from
links to the verified files in the worker's scratch directory. `load` first installs an audit hook that counts and
refuses every outbound attempt (connect, getaddrinfo, sendto, urllib) from the Docling import on; the result reports
the count (and the child's peak RSS) and the service fails an answer whose count is not 0 (the sandbox denies the network anyway). This module
imports only the standard library at its top, so its pure functions are tested in CI without the hook.

Everything that parses the upload happens here: the zip's members, encryption, the page count (before any model
runs) and the conversion. A refusal is `{"refused": <code>}`: unsupported_format, page_limit, invalid_document."""

import gc
import io
import os
import re
import sys
import zipfile
from pathlib import Path

NETWORK_EVENTS = frozenset({"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto",
                            "socket.sendmsg", "urllib.Request"})
ATTEMPTS = [0]
MAX_ZIP_MEMBERS = 10_000
MAX_BYTES = 32 * 1024 * 1024
SLIDE = re.compile(r"^ppt/slides/slide[0-9]+\.xml$")
HEADING_LABELS = frozenset({"title", "section_header"})


def _audit(event, _args):
    if event in NETWORK_EVENTS:
        ATTEMPTS[0] += 1
        raise RuntimeError("network_denied")


def peak_rss_mb():
    """This process's peak resident memory in MiB; the child lives for one job, so it is the job's, load included."""
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak // (1024 * 1024) if sys.platform == "darwin" else peak // 1024)  # bytes on macOS, KiB on Linux


class Refused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def link_artifacts(paths, options):
    """artifacts/<repo with / as -->/<repo path>/<file> -> the verified file; returns the artifacts root."""
    root = Path(options["scratch"]) / "artifacts"
    for name, directory in paths.items():
        repo = options["repos"][name].replace("/", "--")
        target = root / repo / options["repo_paths"].get(name, "")
        target.mkdir(parents=True, exist_ok=True)
        for item in Path(directory).iterdir():
            link = target / item.name
            if not link.exists():
                link.symlink_to(item)
    return root


def sniff(path, max_pages):
    """(format, pages or None) from the bytes, before any model; Refused otherwise."""
    with open(path, "rb") as handle:
        head = handle.read(8)
    if head.startswith(b"%PDF-"):
        import pypdfium2

        try:
            document = pypdfium2.PdfDocument(path)
        except Exception:  # noqa: BLE001 - damaged or encrypted: pdfium says only that it cannot open it
            raise Refused("invalid_document") from None
        try:
            pages = len(document)
        finally:
            document.close()
        if pages > max_pages:
            raise Refused("page_limit")
        return "pdf", pages
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if len(members) > MAX_ZIP_MEMBERS:
                    raise Refused("invalid_document")
                names = {member.filename for member in members}
                if any(member.flag_bits & 0x1 for member in members):
                    raise Refused("invalid_document")  # an encrypted member
        except zipfile.BadZipFile:
            raise Refused("invalid_document") from None
        if "word/document.xml" in names:
            return "docx", None
        if "ppt/presentation.xml" in names:
            slides = sum(1 for name in names if SLIDE.fullmatch(name))
            if slides > max_pages:
                raise Refused("page_limit")
            return "pptx", slides
        raise Refused("unsupported_format")
    raise Refused("unsupported_format")


def assemble(document, max_chars):
    """Markdown of the whole document with document-relative code-point page spans, cut at `max_chars`."""
    pages = sorted(document.pages.keys()) if document.pages else []
    if pages:
        parts = [document.export_to_markdown(page_no=page) for page in pages]
        markdown, spans, offset = "", [], 0
        for index, (page, part) in enumerate(zip(pages, parts)):
            if index:
                markdown += "\n\n"
                offset += 2
            spans.append({"page": page, "start": offset, "end": offset + len(part)})
            markdown += part
            offset += len(part)
        with_text = sum(1 for part in parts if part.strip())
    else:
        markdown, spans, with_text = document.export_to_markdown(), [], 0
    truncated = len(markdown) > max_chars
    if truncated:
        markdown = markdown[:max_chars]
        spans = [{**span, "end": min(span["end"], max_chars)} for span in spans if span["start"] < max_chars]
    headings = sum(1 for item in document.texts if str(getattr(item.label, "value", item.label)) in HEADING_LABELS)
    coverage = {"pages": len(pages) if pages else None, "pages_with_text": with_text,
                "tables": len(document.tables), "headings": headings}
    return markdown, truncated, spans, coverage


class Runtime:
    def __init__(self, converter, revisions, version):
        self.converter, self.revisions, self.version = converter, revisions, version

    def run(self, job):
        from docling.datamodel.base_models import ConversionStatus, DocumentStream

        path, max_pages, max_chars = job["file"], int(job["max_pages"]), int(job["max_chars"])
        try:
            kind, _pages = sniff(path, max_pages)
        except Refused as refusal:
            return {"refused": refusal.code}
        with open(path, "rb") as handle:
            stream = DocumentStream(name=f"document.{kind}", stream=io.BytesIO(handle.read(MAX_BYTES + 1)))
        result = self.converter.convert(stream, raises_on_error=False, max_num_pages=max_pages, max_file_size=MAX_BYTES)
        if result.status not in (ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS):
            return {"refused": "invalid_document"}
        markdown, truncated, spans, coverage = assemble(result.document, max_chars)
        return {"markdown": markdown, "truncated": truncated, "page_spans": spans, "coverage": coverage,
                "engine": {"name": "docling", "version": self.version, "models": self.revisions,
                           "network_attempts": ATTEMPTS[0], "peak_rss_mb": peak_rss_mb()}}

    def unload(self):
        self.converter = None
        gc.collect()


def load(paths, options):
    sys.addaudithook(_audit)  # before the first Docling import; an audit hook cannot be removed
    from importlib import metadata

    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, PdfFormatOption

    artifacts = link_artifacts(paths, options)
    threads = int(os.environ.get("OMP_NUM_THREADS", "2"))
    pipeline = PdfPipelineOptions(
        artifacts_path=str(artifacts), enable_remote_services=False, do_ocr=False, do_table_structure=True,
        do_picture_classification=False, do_picture_description=False, do_code_enrichment=False,
        do_formula_enrichment=False, do_chart_extraction=False, generate_page_images=False,
        generate_picture_images=False,
        accelerator_options=AcceleratorOptions(num_threads=threads, device=AcceleratorDevice.CPU))
    pipeline.table_structure_options.mode = TableFormerMode.ACCURATE
    converter = DocumentConverter(allowed_formats=[InputFormat.PDF, InputFormat.DOCX, InputFormat.PPTX],
                                  format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline)})
    converter.initialize_pipeline(InputFormat.PDF)  # loads the layout and table models now, not in the first job
    return Runtime(converter, dict(options["revisions"]), metadata.version("docling"))
