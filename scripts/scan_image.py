#!/usr/bin/env python3
"""Scan every layer and the config of an exported image for secrets before it can be published."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zlib

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".gitleaks-images.toml"
CANARY = ROOT / "tests/fixtures/gitleaks-canary/image.tar"
GZIP, ZSTD = b"\x1f\x8b", b"\x28\xb5\x2f\xfd"
FINDINGS = 2  # gitleaks --exit-code for findings; every other non-zero exit is a scanner error
CHUNK = 1024 * 1024
# The only gitleaks log lines a scan may print: the findings summary, and the documented skip of an
# archive nested inside a layer (.gz, .xz, .whl), which --max-archive-depth 1 does not open.
EXPECTED_LOG = re.compile(r"\S+ WRN (?:leaks found: \d+|skipping archive: exceeds max archive depth "
                          r"max_archive_depth=1 path=(?:[^\s\"]+|\"(?:[^\"\\]|\\.)*\"))")
MAX_ALIASES = 256
BLOCK, ZERO = 512, bytes(512)
PAYLOAD_FREE = frozenset(b"123456")  # link, device, directory and fifo headers carry no payload
# The two heuristic rules match ordinary code in hash-locked third-party packages (OAuth helpers,
# test vectors, API docs), so they do not apply inside installed third-party distributions and
# system files; every specific rule (private keys, provider tokens) still covers the whole image.
# This is code rather than a gitleaks allowlist because its RE2 paths cannot exclude our own
# packages, which uv installs into site-packages (agent_gateway, family_calendar). ANYM-303 C3.
HEURISTIC_RULES = frozenset({"generic-api-key", "jwt"})
# System files: only /usr/lib and /usr/include (and /lib*, their merged-usr aliases on Debian), never
# /usr/share or /usr/local, where our Dockerfiles COPY.
THIRD_PARTY = re.compile(r"(?:^|/)(?:site|dist)-packages/[^/]+/|^usr/include/|^(?:usr/)?(?:lib|lib32|lib64|libx32)/")
OWN_PACKAGES = frozenset({"agent_gateway", "anymsg", "deedkit_gateway", "deedkit_platform_contracts", "deedkit_recordings",
                          "family_calendar", "family_calendar_service", "messaging_service",
                          "personal_os_voice_service", "platform_contracts", "telegram_broker",
                          "telegram_session_owner"})


class Refused(RuntimeError):
    pass


def member(archive, name):
    handle = archive.extractfile(name)
    if handle is None:
        raise Refused(f"exported archive has no file {name}")
    return handle


def number(field):
    if field[0] & 0x80:  # GNU base-256
        return int.from_bytes(bytes([field[0] & 0x7F]) + field[1:], "big")
    text = field.rstrip(b"\0 ").lstrip(b" ")
    return int(text, 8) if text else 0


def pax_size(data):
    """The `size` record of a PAX extended header, if any; a malformed record refuses."""
    size, position = None, 0
    while position < len(data) and data[position:position + 1] != b"\0":
        space = data.find(b" ", position)
        digits = data[position:space] if space > position else b""
        length = int(digits) if digits.isdigit() else 0
        if length <= len(digits) + 1 or position + length > len(data) or data[position + length - 1] != ord("\n"):
            raise ValueError("malformed PAX record")
        key, _, value = data[space + 1:position + length - 1].partition(b"=")
        if key == b"size":
            size = int(value)
        position += length
    return size


def validate_tar(path):
    """Refuse a layer unless every header checksums, every payload is complete and only zeros follow the end.

    tarfile stops quietly at a malformed header, which would leave later members unscanned.
    """
    total = path.stat().st_size
    if total % BLOCK:
        raise Refused(f"{path.name} is not a whole number of tar blocks")
    override = None
    with path.open("rb") as layer:
        while True:
            header = layer.read(BLOCK)
            if len(header) < BLOCK:
                raise Refused(f"{path.name} ends without an end-of-archive marker")
            if header == ZERO:
                while chunk := layer.read(CHUNK):
                    if chunk.count(0) != len(chunk):
                        raise Refused(f"{path.name} has data after its end-of-archive marker")
                return
            try:
                checksum = number(header[148:156])
                size = number(header[124:136])
            except ValueError as exc:
                raise Refused(f"{path.name} has a malformed header") from exc
            if checksum != sum(header[:148]) + 8 * 32 + sum(header[156:]):
                raise Refused(f"{path.name} has a header with a bad checksum")
            kind = header[156]
            size = override if override is not None and kind not in b"xgLK" else size
            override = None
            if kind in PAYLOAD_FREE:
                continue
            blocks = -(-size // BLOCK) * BLOCK
            if layer.tell() + blocks > total:
                raise Refused(f"{path.name} has a truncated member")
            if kind == ord("x"):
                override = pax_size(layer.read(size))
                layer.seek(blocks - size, 1)
            else:
                layer.seek(blocks, 1)


def clean(path):
    """An image path without `./`, `..` or a leading slash."""
    return posixpath.normpath("/" + path).lstrip("/")


def aliases(path):
    """(kind, link path, raw link target) for every hard ("hard") or symbolic ("sym") link in a layer."""
    links = []
    with tarfile.open(path, "r:") as layer:
        for entry in layer:
            if entry.islnk():
                links.append(("hard", clean(entry.name), entry.linkname))
            elif entry.issym():
                links.append(("sym", clean(entry.name), entry.linkname))
    return links


def resolve(path, symlinks):
    """The physical path a name reaches, resolving symlinked components one by one as the kernel does.

    `..` applies to the resolved prefix, not to the spelling. None for a symlink loop.
    """
    pending = [part for part in path.split("/") if part not in ("", ".")]
    result, hops = [], 0
    while pending:
        part = pending.pop(0)
        if part == "..":
            if result:
                result.pop()
            continue
        target = symlinks.get("/".join(result + [part]))
        if target is None:
            result.append(part)
            continue
        hops += 1
        if hops > 40:
            return None
        if target.startswith("/"):
            result = []
        pending[:0] = [piece for piece in target.split("/") if piece not in ("", ".")]
    return "/".join(result)


def resolve_links(raw):
    """(link path, physical target) pairs; a target that cannot be resolved covers the whole image ("")."""
    symlinks = {name: target for kind, name, target in raw if kind == "sym"}
    resolved = []
    for kind, name, target in raw:
        if kind == "hard":
            resolved.append((name, clean(target)))
        else:
            physical = resolve(target if target.startswith("/") else posixpath.dirname(name) + "/" + target, symlinks)
            resolved.append((name, "" if physical is None else physical))
    return resolved


def names_of(path, links):
    """Every path a file is reachable by through hard links and file or directory symlinks, transitively.

    `links` holds resolved (link path, physical target) pairs; an empty target is the image root.
    None when the set grows past MAX_ALIASES, which callers treat as not narrowable.
    """
    names, pending = {path}, [path]
    while pending:
        current = pending.pop()
        for name, target in links:
            if target == "" or current == target or current.startswith(target + "/"):
                alias = name + ("/" + current if target == "" else current[len(target):])
                if alias not in names:
                    if len(names) >= MAX_ALIASES:
                        return None
                    names.add(alias)
                    pending.append(alias)
    return names


def prepare(archive_path, directory, links=None):
    """Write each layer as a validated uncompressed layer-<n>.tar and the config as config.json.

    Nothing inside a layer is extracted: the scan sees deleted, overwritten and hidden files of
    lower layers too, because a published image ships every layer blob.
    """
    directory.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(archive_path, "r:") as archive:
            manifest = json.load(member(archive, "manifest.json"))
            if len(manifest) != 1:
                raise Refused("an exported archive must hold exactly one image")
            config_bytes = member(archive, manifest[0]["Config"]).read()
            diff_ids = json.loads(config_bytes)["rootfs"]["diff_ids"]
            layers = manifest[0]["Layers"]
            if len(layers) != len(diff_ids):
                raise Refused(f"{len(layers)} layers but {len(diff_ids)} diff_ids in the image config")
            for number, (name, diff_id) in enumerate(zip(layers, diff_ids, strict=True)):
                blob = member(archive, name)
                magic = blob.read(4)
                blob.seek(0)
                if magic.startswith(ZSTD):
                    raise Refused(f"layer {number} is zstd-compressed, which the scan does not support")
                source = gzip.GzipFile(fileobj=blob) if magic.startswith(GZIP) else blob
                target = directory / f"layer-{number}.tar"
                digest = hashlib.sha256()
                with target.open("wb") as out:
                    while chunk := source.read(CHUNK):
                        digest.update(chunk)
                        out.write(chunk)
                if "sha256:" + digest.hexdigest() != diff_id:
                    raise Refused(f"layer {number} does not match its diff_id")
                validate_tar(target)
                if links is not None:
                    links.extend(aliases(target))
    except (OSError, EOFError, zlib.error, tarfile.TarError, KeyError, TypeError, ValueError) as exc:
        raise Refused(f"exported archive {Path(archive_path).name} is unreadable ({type(exc).__name__})") from exc
    (directory / "config.json").write_bytes(config_bytes)
    return len(diff_ids)


def own(path):
    # A distribution's metadata directory is named <distribution>-<version>.dist-info.
    return any(re.split(r"-\d", part)[0].replace("-", "_").replace(".", "_").lower() in OWN_PACKAGES
               for part in path.split("/"))


def third_party(rule, path, links=()):
    """Whether a heuristic finding lies only inside installed third-party distributions or system files.

    Every name the file is reachable by (hard links, file and directory symlinks, transitively) must
    be a third-party path outside our own packages; otherwise the heuristic finding stands.
    """
    if rule not in HEURISTIC_RULES:
        return False
    names = names_of(clean(path), links)
    return names is not None and all(not own(name) and THIRD_PARTY.search(name) for name in names)


def gitleaks():
    binary = os.environ.get("GITLEAKS") or shutil.which("gitleaks")
    if not binary:
        raise Refused("gitleaks is not installed")
    return binary


def scan(archive_path, *, config=CONFIG, binary=None):
    """Findings as (RuleID, layer, path inside the layer, StartLine); never the secret itself."""
    with tempfile.TemporaryDirectory(dir=os.environ.get("RUNNER_TEMP") or None) as work:
        work = Path(work)
        links = []
        layers = prepare(archive_path, work / "scan", links)
        (work / "ignore").mkdir()
        report = work / "report.json"
        # The image cannot steer the scan: the config comes from this repository, the ignore path is
        # empty and inline gitleaks:allow comments are ignored.
        result = subprocess.run(
            [binary or gitleaks(), "dir", "--config", str(config), "--gitleaks-ignore-path", str(work / "ignore"),
             "--ignore-gitleaks-allow", "--max-archive-depth", "1", "--redact", "--no-banner", "--no-color",
             "--log-level", "warn", "--exit-code", str(FINDINGS), "--report-format", "json",
             "--report-path", str(report), str(work / "scan")],
            capture_output=True, text=True, timeout=1800)
        if result.returncode not in (0, FINDINGS):
            raise Refused(f"gitleaks failed (exit {result.returncode})")
        # Skipped or unreadable files are only logged, so any other log line is a scanner problem.
        lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
        problems = [line for line in lines if not EXPECTED_LOG.fullmatch(line)]
        if problems or (result.returncode == 0 and any("leaks found" in line for line in lines)):
            raise Refused("gitleaks reported a problem: " + " | ".join(problems or lines)[-400:])
        findings = []
        for finding in json.loads(report.read_text() or "[]"):
            outer, _, inner = finding["File"].partition("!")
            findings.append((finding["RuleID"], Path(outer).name.removesuffix(".tar"), inner, finding["StartLine"]))
        if (result.returncode == FINDINGS) != bool(findings):
            raise Refused("gitleaks exit code and report disagree")
        resolved = resolve_links(links)
        return layers, sorted(finding for finding in findings if not third_party(finding[0], finding[2], resolved))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, nargs="?", default=None)
    parser.add_argument("--canary", action="store_true", help="scan the committed canary and require a finding")
    args = parser.parse_args(argv)
    try:
        archive = CANARY if args.canary else args.archive
        if archive is None:
            raise Refused("name an archive or pass --canary")
        started = time.monotonic()
        layers, findings = scan(archive)
        for rule, layer, path, line in findings:
            print(f"finding: {rule} in {layer} at {path}:{line}")
        print(f"scanned {layers} layers and the config of {archive.name} in {time.monotonic() - started:.1f}s: "
              f"{len(findings)} findings")
        if args.canary and not findings:
            raise Refused("the canary was not detected; the scanner, its config or the helper is broken")
        if findings and not args.canary:
            raise Refused("secrets found in the image; fix the image or add a reviewed allowlist entry")
    except Refused as exc:
        parser.exit(1, "image scan refused: " + str(exc) + "\n")


if __name__ == "__main__":
    sys.exit(main())
