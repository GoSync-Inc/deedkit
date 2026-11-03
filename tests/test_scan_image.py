import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import tarfile
import tomllib

import pytest
import yaml

from scripts import scan_image as si

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "config/gitleaks/gitleaks-8.30.1-default.toml"
UPSTREAM_SHA256 = "e163e53b9e7e8a8511e77271e2b323ed057759542a6d988258afe3a1fa329caf"
BINARY = os.environ.get("GITLEAKS") or shutil.which("gitleaks")
needs_gitleaks = pytest.mark.skipif(not BINARY, reason="the pinned gitleaks binary is not installed; images.yml always runs it")


def made_up_aws_pair(seed):
    """A made-up AWS key pair in the format gitleaks' aws-access-token rule matches; never a real key."""
    rng = random.Random(seed)
    key_id = "AKIA" + "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for _ in range(16))
    secret = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789") for _ in range(40))
    return key_id, secret


def made_up_private_key(seed):
    """A made-up PEM block of random bytes: matches the private-key rule, is no usable key."""
    body = base64.encodebytes(random.Random(seed).randbytes(600)).decode()
    return "-----BEGIN " + "PRIVATE KEY-----\n" + body + "-----END " + "PRIVATE KEY-----\n"


def layer_tar(files):
    """An uncompressed layer tar: {path: text or bytes}; a None value is a whiteout or opaque marker."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in files.items():
            data = b"" if data is None else data.encode() if isinstance(data, str) else data
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def image_archive(path, layers, *, env=(), history=(), gzip_layers=(), diff_ids=None, raw_layers=None):
    """A hand-made single-image `docker save` archive (Docker 25+ layout); no Docker needed."""
    raw = [layer_tar(files) for files in layers] if raw_layers is None else raw_layers
    config = {"architecture": "amd64", "os": "linux", "config": {"Env": list(env), "Labels": {}},
              "history": [{"created_by": entry} for entry in history],
              "rootfs": {"type": "layers", "diff_ids": diff_ids if diff_ids is not None
                         else ["sha256:" + hashlib.sha256(data).hexdigest() for data in raw]}}
    config_bytes = json.dumps(config).encode()
    blobs = {"sha256:" + hashlib.sha256(config_bytes).hexdigest(): config_bytes}
    names = []
    for number, data in enumerate(raw):
        stored = gzip.compress(data, mtime=0) if number in gzip_layers else data
        digest = "sha256:" + hashlib.sha256(stored).hexdigest()
        blobs[digest] = stored
        names.append("blobs/sha256/" + digest.removeprefix("sha256:"))
    config_name = "blobs/sha256/" + hashlib.sha256(config_bytes).hexdigest()
    manifest = json.dumps([{"Config": config_name, "RepoTags": ["deedkit-check-test:latest"], "Layers": names}]).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as tar:
        for name, data in [("manifest.json", manifest)] + [("blobs/sha256/" + d.removeprefix("sha256:"), b) for d, b in blobs.items()]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def settings(seed=1):
    key_id, secret = made_up_aws_pair(seed)
    return f'aws_access_key_id = "{key_id}"\naws_secret_access_key = "{secret}"\n'


def findings(tmp_path, **kwargs):
    archive = image_archive(tmp_path / "image.tar", **kwargs)
    return si.scan(archive, binary=BINARY)[1]


# Helper: layers are validated and copied, never extracted.

def test_each_layer_becomes_a_tar_matching_its_diff_id_and_nothing_is_extracted(tmp_path):
    layers = [{"app/one.txt": "one"}, {"app/two.txt": "two", ".wh.app/one.txt": None}]
    archive = image_archive(tmp_path / "image.tar", layers=layers, gzip_layers={1})
    assert si.prepare(archive, tmp_path / "scan") == 2
    assert sorted(p.name for p in (tmp_path / "scan").iterdir()) == ["config.json", "layer-0.tar", "layer-1.tar"]
    for number, files in enumerate(layers):
        assert (tmp_path / "scan" / f"layer-{number}.tar").read_bytes() == layer_tar(files)  # gzip layer decompressed
    config = json.loads((tmp_path / "scan/config.json").read_text())
    assert config["rootfs"]["diff_ids"] == ["sha256:" + hashlib.sha256(layer_tar(f)).hexdigest() for f in layers]


@pytest.mark.parametrize("change, message", [
    (dict(diff_ids=["sha256:" + "0" * 64]), "2 layers but 1 diff_ids"),
    (dict(diff_ids=["sha256:" + "0" * 64, "sha256:" + "1" * 64]), "does not match its diff_id"),
    (dict(raw_layers=[layer_tar({"a": "a"}), layer_tar({"b": "b" * 4096})[:700]]), "unreadable|diff_id|whole number|truncated"),
    (dict(raw_layers=[layer_tar({"a": "a"}), si.ZSTD + b"\x00" * 64]), "zstd"),
], ids=["layer-count", "diff-id", "truncated", "zstd"])
def test_a_layer_that_cannot_be_validated_is_refused_before_any_scan(tmp_path, change, message):
    layers = [{"a": "a"}, {"b": "b"}]
    archive = image_archive(tmp_path / "image.tar", layers=layers, **change)
    with pytest.raises(si.Refused, match=message):
        si.scan(archive, binary="/nonexistent/gitleaks-must-not-run")


def test_a_truncated_layer_with_a_matching_digest_is_refused_by_the_tar_walk(tmp_path):
    truncated = layer_tar({"b": "b" * 4096})[:700]  # its diff_id is computed over the truncated bytes
    archive = image_archive(tmp_path / "image.tar", layers=None, raw_layers=[truncated])
    with pytest.raises(si.Refused, match="whole number of tar blocks|truncated"):
        si.prepare(archive, tmp_path / "scan")


# Configuration: default rules without the default global path exemptions.

def test_image_config_keeps_the_pinned_default_rules_without_global_path_exemptions():
    assert hashlib.sha256(UPSTREAM.read_bytes()).hexdigest() == UPSTREAM_SHA256
    upstream = tomllib.loads(UPSTREAM.read_text())
    ours = tomllib.loads(si.CONFIG.read_text())
    assert ours["rules"] == upstream["rules"]
    # The upstream global allowlist becomes the first [[allowlists]] entry, without its paths.
    assert "allowlist" not in ours and upstream["allowlist"]["paths"]
    assert ours["allowlists"][0] == {k: v for k, v in upstream["allowlist"].items() if k != "paths"}
    assert "extend" not in ours and "disabledRules" not in json.dumps(ours)
    for entry in ours["allowlists"][1:]:
        assert entry.get("targetRules"), entry  # a project entry never silences every rule
        assert entry.get("description", "").strip(), entry
        assert entry.get("paths") or entry.get("regexes") or entry.get("commits"), entry
        assert not any(re.fullmatch(r"\(\?:\^\|/\)[^()]*\(\?:/\.\*\)\?\$", p) for p in entry.get("paths", [])), entry


# Real pinned gitleaks: what a public image would expose is found.

@needs_gitleaks
@pytest.mark.parametrize("path", ["app/settings.py", "usr/local/lib/python3.13/site-packages/pkg/settings.py",
                                  "app/node_modules/pkg/settings.js", "usr/lib/python3/dist-packages/x-1.0.dist-info/RECORD"])
def test_a_credential_in_any_layer_path_is_found(tmp_path, path):
    found = findings(tmp_path, layers=[{"etc/clean.txt": "nothing"}, {path: settings()}])
    assert ("aws-access-token", "layer-1", path, 1) in found


@needs_gitleaks
@pytest.mark.parametrize("hide", [{".wh.settings.py": None}, {"app/settings.py": "replaced\n"}, {"app/.wh..wh..opq": None}],
                         ids=["whiteout", "overwritten", "opaque"])
def test_a_credential_hidden_by_a_later_layer_is_still_found(tmp_path, hide):
    hide = {f"app/{k}" if not k.startswith("app/") else k: v for k, v in hide.items()}
    found = findings(tmp_path, layers=[{"app/settings.py": settings()}, hide])
    assert ("aws-access-token", "layer-0", "app/settings.py", 1) in found


@needs_gitleaks
def test_a_credential_in_a_gzip_layer_is_found(tmp_path):
    assert ("aws-access-token", "layer-0", "app/settings.py", 1) in \
        findings(tmp_path, layers=[{"app/settings.py": settings()}], gzip_layers={0})


@needs_gitleaks
@pytest.mark.parametrize("where", ["env", "history"])
def test_a_credential_only_in_the_image_config_is_found(tmp_path, where):
    key_id, _ = made_up_aws_pair(3)
    value = {"env": dict(env=[f"AWS_ACCESS_KEY_ID={key_id}"]), "history": dict(history=[f"RUN export AWS_KEY={key_id}"])}
    found = findings(tmp_path, layers=[{"app/clean.txt": "clean"}], **value[where])
    assert any(rule == "aws-access-token" and layer == "config.json" for rule, layer, _, _ in found)


@needs_gitleaks
@pytest.mark.parametrize("files", [
    {".gitleaks.toml": '[allowlist]\npaths = [".*"]\n'},
    {".gitleaksignore": "*\n"},
    {"app/allowed.py": None},
], ids=["config-in-image", "ignore-file-in-image", "inline-allow"])
def test_an_image_cannot_suppress_its_own_findings(tmp_path, files):
    key_id, secret = made_up_aws_pair(4)
    files = {name: data if data is not None else f'aws_access_key_id = "{key_id}"  # gitleaks:allow\n'
             for name, data in files.items()}
    found = findings(tmp_path, layers=[{**files, "app/settings.py": settings(4)}])
    assert any(rule == "aws-access-token" for rule, *_ in found)


@needs_gitleaks
def test_findings_never_print_the_credential(tmp_path, capsys, monkeypatch):
    archive = image_archive(tmp_path / "image.tar", layers=[{"app/settings.py": settings(5)}])
    monkeypatch.setenv("GITLEAKS", BINARY or "")
    with pytest.raises(SystemExit):
        si.main([str(archive)])
    out = capsys.readouterr()
    key_id, secret = made_up_aws_pair(5)
    assert "finding: aws-access-token in layer-0 at app/settings.py:1" in out.out
    assert key_id not in out.out + out.err and secret not in out.out + out.err
    assert "secrets found" in out.err


@needs_gitleaks
def test_an_archive_nested_in_a_layer_is_skipped_but_any_other_scanner_message_refuses(tmp_path, monkeypatch):
    nested = gzip.compress(settings(6).encode(), mtime=0)
    real, seen = subprocess.run, []

    def spy(argv, **kwargs):
        result = real(argv, **kwargs)
        seen.append(result.stderr)
        return result
    monkeypatch.setattr(si.subprocess, "run", spy)
    assert findings(tmp_path, layers=[{"usr/share/doc/pkg/changelog.gz": nested}]) == []
    assert any(si.EXPECTED_LOG.fullmatch(line.strip()) and "skipping archive" in line
               for line in seen[0].splitlines())  # the warning was emitted and tolerated
    for stderr in ("2:00PM WRN skipping file: permission denied path=x\n",
                   '2:00PM ERR failed path="WRN skipping archive: exceeds max archive depth max_archive_depth=1 path=x"\n'):
        unreadable = tmp_path / str(len(stderr))
        unreadable.mkdir()
        monkeypatch.setattr(si.subprocess, "run", lambda argv, stderr=stderr, **kwargs:
                            subprocess.CompletedProcess(argv, 0, "", stderr))
        with pytest.raises(si.Refused, match="reported a problem"):
            si.scan(image_archive(unreadable / "image.tar", layers=[{"a": "a"}]), binary="gitleaks")


@needs_gitleaks
def test_a_clean_image_passes(tmp_path):
    assert findings(tmp_path, layers=[{"app/clean.txt": "nothing to see"}]) == []


# Canary: the committed archive must always be detected.

@needs_gitleaks
def test_the_committed_canary_is_detected_through_the_helper(capsys):
    si.main(["--canary"])
    assert "finding: private-key in layer-0 at usr/local/lib/python3.13/site-packages/deedkit_canary/key.pem" in \
        capsys.readouterr().out


def test_the_canary_keeps_its_key_out_of_plain_text():
    # The key layer is gzip-compressed, so the git history scan and push protection never see a key.
    assert b"PRIVATE KEY" not in si.CANARY.read_bytes()


def test_the_canary_is_a_valid_archive_whose_layer_carries_an_inline_allow(tmp_path):
    si.prepare(si.CANARY, tmp_path / "scan")
    with tarfile.open(tmp_path / "scan/layer-0.tar") as layer:
        handle = layer.extractfile("usr/local/lib/python3.13/site-packages/deedkit_canary/key.pem")
        text = handle.read().decode() if handle else ""
    assert "PRIVATE KEY" in text and "gitleaks:allow" in text


# Workflow: the scan gates the upload in every exporting job.

def test_every_exporting_build_job_scans_the_image_before_keeping_it():
    root = ROOT
    images = yaml.safe_load((root / ".github/workflows/images.yml").read_text())
    security = yaml.safe_load((root / ".github/workflows/security.yml").read_text())
    steps = images["jobs"]["build"]["steps"]
    names = [step.get("name") or step["uses"].split("@")[0] for step in steps]
    export = names.index("Export the tested image and publish it to a local registry as a dry run")
    install = names.index("Install the pinned gitleaks")
    scan = names.index("Scan every layer and the config of the tested image for secrets")
    upload = names.index("Keep the tested image for the publish job")
    assert export < install < scan < upload
    assert steps[install]["if"] == steps[scan]["if"] == steps[export]["if"]
    assert "continue-on-error" not in steps[install] and "continue-on-error" not in steps[scan]
    # A pipe must not hide the scan's exit code (GitHub's default shell has no pipefail).
    assert steps[scan]["run"].splitlines()[0] == "set -o pipefail" and "shell" not in steps[scan]
    assert "python3 scripts/scan_image.py --canary" in steps[scan]["run"]
    assert 'python3 scripts/scan_image.py "$RUNNER_TEMP/images/image-$TARGET/image.tar"' in steps[scan]["run"]
    # The same release URL and SHA-256 as security.yml's pinned download.
    url = re.compile(r"https://github\.com/gitleaks/gitleaks/releases/download/\S+\.tar\.gz")
    ours_url = url.search(steps[install]["run"]).group(0)
    ours_sha = re.search(r'echo "([0-9a-f]{64})  \$RUNNER_TEMP/gitleaks\.tar\.gz" \| sha256sum --check --strict',
                         steps[install]["run"]).group(1)
    theirs = re.search(re.escape(ours_url) + r"\s*\\\s*\n\s*([0-9a-f]{64})",
                       (root / ".github/workflows/security.yml").read_text())
    assert theirs and theirs.group(1) == ours_sha
    # checks / verify runs the real-scanner tests with the same pinned binary.
    verify = yaml.safe_load((root / ".github/workflows/checks.yml").read_text())["jobs"]["verify"]["steps"]
    install_tests = next(i for i, s in enumerate(verify) if s.get("name") == "Install the pinned gitleaks for the image-scan tests")
    python = next(i for i, s in enumerate(verify) if s.get("id") == "python")
    assert install_tests < python and ours_url in verify[install_tests]["run"] and ours_sha in verify[install_tests]["run"]
    assert "GITLEAKS=" in verify[install_tests]["run"]
    assert security  # parsed, so the security workflow stays valid YAML


@pytest.mark.parametrize("path", ["scripts/scan_image.py", ".gitleaks-images.toml", "tests/fixtures/gitleaks-canary/image.tar"])
def test_a_scan_only_change_still_builds_and_scans_every_image(tmp_path, path):
    from tests.test_tiden_ci import image_decision, pull_request_checkout

    def change(root, _git):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("changed\n")
    checkout, head = pull_request_checkout(tmp_path, change)
    assert image_decision(checkout, tmp_path, "pull_request", head)[0] == ["build=true"]


# Narrowing (ANYM-303 C3): heuristic rules skip third-party distributions, never our own code.

@pytest.mark.parametrize("rule, path, dropped", [
    ("generic-api-key", "usr/local/lib/python3.13/site-packages/oauthlib/oauth1/rfc5849/parameters.py", True),
    ("generic-api-key", "app/.venv/lib/python3.13/site-packages/google/api_core/page_iterator.py", True),
    ("jwt", "usr/local/lib/python3.13/site-packages/pyjwt-2.15.1.dist-info/METADATA", True),
    ("generic-api-key", "usr/lib/x86_64-linux-gnu/perl/5.36.0/CORE/cop.h", True),
    ("generic-api-key", "usr/lib/python3/dist-packages/apt/package.py", True),
    ("generic-api-key", "usr/lib/python3/dist-packages/agent_gateway/settings.py", False),
    ("generic-api-key", "usr/share/doc/age/settings.env", False),
    ("generic-api-key", "usr/local/bin/tool.sh", False),
    ("generic-api-key", "include/settings.py", False),
    ("generic-api-key", "usr/include/openssl/x509.h", True),
    ("generic-api-key", "lib/x86_64-linux-gnu/perl/5.36.0/CORE/cop.h", True),
    ("generic-api-key", "app/services/gateway/.venv/lib/python3.13/site-packages/agent_gateway/app.py", False),
    ("generic-api-key", "app/.venv/lib/python3.13/site-packages/family_calendar/google_api.py", False),
    ("jwt", "app/.venv/lib/python3.13/site-packages/family_calendar_service-0.1.0.dist-info/METADATA", False),
    ("generic-api-key", "app/messaging_service/app.py", False),
    ("generic-api-key", "app/settings.py", False),
    ("private-key", "usr/local/lib/python3.13/site-packages/somepkg/key.pem", False),
    ("aws-access-token", "usr/local/lib/python3.13/site-packages/somepkg/config.py", False),
])
def test_heuristic_rules_skip_only_third_party_distributions(rule, path, dropped):
    assert si.third_party(rule, path) is dropped


def test_a_third_party_file_linked_from_our_code_is_not_narrowed(tmp_path):
    third = "usr/local/lib/python3.13/site-packages/somelib/settings.py"
    assert si.third_party("generic-api-key", third, [("usr/local/lib/python3.13/site-packages/other/x.py", third)])
    assert not si.third_party("generic-api-key", third, [("app/settings.py", third)])
    assert not si.third_party("generic-api-key", third, [("app/agent_gateway/settings.py", third)])
    layer = tmp_path / "layer.tar"
    with tarfile.open(layer, "w") as tar:
        data = b"x = 1\n"
        info = tarfile.TarInfo(third)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        hard = tarfile.TarInfo("app/settings.py")
        hard.type, hard.linkname = tarfile.LNKTYPE, third
        tar.addfile(hard)
        soft = tarfile.TarInfo("app/conf/link.py")
        soft.type, soft.linkname = tarfile.SYMTYPE, "../../" + third
        tar.addfile(soft)
    assert si.resolve_links(si.aliases(layer)) == [("app/settings.py", third), ("app/conf/link.py", third)]


@pytest.mark.parametrize("raw, path", [
    # `..` after a directory symlink applies to the resolved directory, not to the spelling.
    ([("sym", "usr/lib/current", "vendor/subdir"),
      ("sym", "app/agent_gateway/settings.py", "../../usr/lib/current/../settings.py")], "usr/lib/vendor/settings.py"),
    # A link to the image root reaches every file.
    ([("sym", "app/agent_gateway/root", "../..")], "usr/lib/vendor/settings.py"),
    ([("sym", "app/root", "/")], "usr/local/lib/python3.13/site-packages/x/s.py"),
    # A symlink loop is uncertain, so it is treated as reaching everything.
    ([("sym", "app/a", "b"), ("sym", "app/b", "a")], "usr/lib/vendor/settings.py"),
], ids=["dotdot-after-directory-symlink", "relative-root", "absolute-root", "loop"])
def test_symlinks_resolve_like_the_kernel_so_our_code_cannot_reach_a_narrowed_file(raw, path):
    assert not si.third_party("generic-api-key", path, si.resolve_links(raw))


def test_venv_lib64_and_merged_usr_aliases_keep_third_party_files_narrowed():
    raw = [("sym", "app/.venv/lib64", "lib"), ("sym", "lib", "usr/lib"), ("sym", "usr/lib/current", "vendor")]
    links = si.resolve_links(raw)
    assert si.third_party("generic-api-key", "app/.venv/lib/python3.13/site-packages/x/s.py", links)
    assert si.third_party("generic-api-key", "usr/lib/vendor/settings.py", links)


def test_links_are_resolved_transitively_and_through_directories():
    site = "app/.venv/lib/python3.13/site-packages"
    links = [("app/.venv/lib64", "app/.venv/lib"),  # venv plumbing: both names stay third-party
             ("app/conf", f"{site}/vendored"),  # a directory symlink from our code
             ("app/a.py", "app/b.py"), ("app/b.py", f"{site}/chained/s.py")]  # a chain ending in third-party code
    assert si.third_party("generic-api-key", f"{site}/plain/s.py", links)
    perl = "usr/lib/x86_64-linux-gnu/perl/5.36.0/CORE/cop.h"  # Debian's merged-usr /lib -> usr/lib alias
    assert si.third_party("generic-api-key", perl, [("lib", "usr/lib"), ("lib64", "usr/lib64")])
    assert not si.third_party("generic-api-key", f"{site}/vendored/s.py", links)
    assert not si.third_party("generic-api-key", f"./{site}/chained/s.py", links)
    cycle = [("a", "b"), ("b", "a")]
    assert si.third_party("generic-api-key", f"{site}/x/s.py", cycle)
    fan = [(f"app/copy{n}", site) for n in range(si.MAX_ALIASES + 1)]
    assert not si.third_party("generic-api-key", f"{site}/x/s.py", fan)  # too many names: not narrowed


def test_malformed_pax_records_refuse_instead_of_looping():
    assert si.pax_size(b"11 size=12\n") == 12
    for record in (b"0 size=1\n", b"2 x\n", b"99 size=1\n", b"11 size=12X"):
        with pytest.raises(ValueError):
            si.pax_size(record)


def test_a_quoted_nested_archive_path_is_an_expected_log_line():
    line = '7:06AM WRN skipping archive: exceeds max archive depth max_archive_depth=1 path="/tmp/s/layer-0.tar!app/a b/c.gz"'
    assert si.EXPECTED_LOG.fullmatch(line)
    assert not si.EXPECTED_LOG.fullmatch(line + ' ERR="x"')


@needs_gitleaks
def test_a_hardlink_from_our_code_keeps_a_third_party_finding(tmp_path):
    third = "app/.venv/lib/python3.13/site-packages/somelib/settings.py"
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    rng = random.Random(9)
    secret = ('service_api_key = "' + "".join(rng.choice(alphabet) for _ in range(40)) + '"\n').encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(third)
        info.size = len(secret)
        tar.addfile(info, io.BytesIO(secret))
        hard = tarfile.TarInfo("app/agent_gateway/settings.py")
        hard.type, hard.linkname = tarfile.LNKTYPE, third
        tar.addfile(hard)
    archive = image_archive(tmp_path / "image.tar", layers=None, raw_layers=[buffer.getvalue()])
    assert [f[2] for f in si.scan(archive, binary=BINARY)[1]] == [third]


def tar_with(*members):
    """Concatenate raw 512-byte tar blocks."""
    return b"".join(members)


def header_block(name, payload=b"", corrupt_checksum=False):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    data = buffer.getvalue()
    member = data[:512 + -(-len(payload) // 512) * 512]
    if corrupt_checksum:
        member = member[:148] + b"0000000\0" + member[156:]
    return member


@pytest.mark.parametrize("layer, message", [
    (tar_with(header_block("a", b"one"), header_block("b", b"two", corrupt_checksum=True), header_block("c", b"x"), bytes(1024)),
     "bad checksum"),
    (tar_with(header_block("a", b"one"), header_block("b", b"t" * 2000)[:1024]), "truncated|end-of-archive"),
    (tar_with(header_block("a", b"one")), "end-of-archive"),
    (tar_with(header_block("a", b"one"), bytes(1024), header_block("c", b"hidden")), "data after"),
    (tar_with(header_block("a", b"one"))[:700], "whole number"),
], ids=["malformed-middle-header", "truncated-member", "no-end-marker", "data-after-end", "partial-block"])
def test_a_malformed_layer_is_refused_even_when_its_digest_matches(tmp_path, layer, message):
    archive = image_archive(tmp_path / "image.tar", layers=None, raw_layers=[layer])
    with pytest.raises(si.Refused, match=message):
        si.prepare(archive, tmp_path / "scan")


def test_own_package_list_covers_every_project_installed_into_an_image():
    names = set()
    for pyproject in ("services/gateway", "services/calendar", "services/calls", "packages/platform-contracts",
                      "services/messaging", "services/recordings"):
        project = tomllib.loads((ROOT / pyproject / "pyproject.toml").read_text())
        names.add(project["project"]["name"].replace("-", "_").replace(".", "_").lower())
        for wheel in project.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {}).get("packages", []):
            names.add(Path(wheel).name)
    # Only these are installed into site-packages; other app code lives under /app and is never narrowed.
    names |= {path.name for path in (ROOT / "services/gateway/src").iterdir() if path.is_dir() and not path.name.startswith(".")}
    assert names <= si.OWN_PACKAGES, names - si.OWN_PACKAGES


@needs_gitleaks
def test_a_heuristic_finding_in_our_own_installed_package_still_fails(tmp_path):
    own = "app/services/gateway/.venv/lib/python3.13/site-packages/agent_gateway/settings.py"
    third = "app/services/gateway/.venv/lib/python3.13/site-packages/somelib/settings.py"
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    rng = random.Random(7)
    secret = 'service_api_key = "' + "".join(rng.choice(alphabet) for _ in range(40)) + '"\n'
    found = findings(tmp_path, layers=[{own: secret, third: secret}])
    assert [f[2] for f in found] == [own]


PYTHON_GPG = "GPG_KEY=" + "7169605F62C75135" + "6D054A26A821E680E5FA6305"  # public CPython signing-key fingerprint


@needs_gitleaks
@pytest.mark.parametrize("env, allowed", [(PYTHON_GPG, True),
                                         ("GPG_KEY=" + "0123456789ABCDEF" + "0123456789ABCDEF01234567", False)])
def test_only_the_base_image_signing_key_fingerprint_is_allowed_in_the_config(tmp_path, env, allowed):
    found = findings(tmp_path, layers=[{"app/clean.txt": "clean"}], env=[env], history=["ENV " + env])
    assert (found == []) is allowed


@needs_gitleaks
@pytest.mark.parametrize("env", [PYTHON_GPG + "ABCDEF0123456789", "X" + PYTHON_GPG], ids=["suffixed", "prefixed"])
def test_the_signing_key_allowlist_matches_only_the_exact_value(tmp_path, env):
    assert findings(tmp_path, layers=[{"app/clean.txt": "clean"}], env=[env])


@needs_gitleaks
@pytest.mark.parametrize("path, allowed", [("etc/ssl/private/ssl-cert-snakeoil.key", True),
                                          ("etc/ssl/private/server.key", False),
                                          ("usr/local/lib/python3.13/site-packages/rsa/pem.py", True),
                                          ("usr/local/lib/python3.13/site-packages/rsa/key.py", False),
                                          ("app/.venv/lib/python3.13/site-packages/google/auth/crypt/_python_rsa.py", True),
                                          ("app/.venv/lib/python3.13/site-packages/google/auth/transport/_mtls_helper.py", True),
                                          ("app/.venv/lib/python3.13/site-packages/google/auth/crypt/rsa.py", False)])
def test_private_key_allowlists_are_exact_files(tmp_path, path, allowed):
    found = findings(tmp_path, layers=[{path: made_up_private_key(path)}])
    assert (found == []) is allowed
    # targetRules scope each entry to private-key: another rule in the same file is still found.
    other = findings(tmp_path / "other", layers=[{path: settings(8)}])
    assert ("aws-access-token", "layer-0", path, 1) in other
    if not allowed:
        assert [(f[0], f[2]) for f in found] == [("private-key", path)]


def test_history_scan_policy_excludes_only_scan_policy_files_and_old_synthetic_values():
    policy = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    assert policy["extend"] == {"useDefault": True} and "rules" not in policy
    files, synthetic = policy["allowlists"]
    assert files["paths"] == [r"^config/gitleaks/gitleaks-8\.30\.1-default\.toml$", r"^\.gitleaks-images\.toml$"]
    assert set(files) == {"description", "paths"}
    assert synthetic["condition"] == "AND" and synthetic["targetRules"] == ["generic-api-key"]
    assert synthetic["paths"] == [r"^tests/test_scan_image\.py$"] and len(synthetic["regexes"]) == 2
