import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile

import hypothesis
from hypothesis import strategies as st
import pytest
import yaml

from scripts import publish_images as pi
from scripts import production

ROOT = Path(__file__).resolve().parents[1]
SHA = "c" * 40
ATTEMPT = "40667989770-1"
STAGING = f"{SHA}-attempt-{ATTEMPT}"
MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"


def labels(target, sha=SHA, **change):
    return {**pi.expected_labels(target, sha), **change}


def archive(path, target, sha=SHA, salt="", **change):
    """A synthetic single-image `docker save` archive (Docker 25+ layout) with the given labels."""
    config = json.dumps({"architecture": "amd64", "config": {"Labels": labels(target, sha, **change)},
                         "rootfs": {"type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(salt.encode()).hexdigest()]}},
                        sort_keys=True).encode()
    digest = hashlib.sha256(config).hexdigest()
    manifest = json.dumps([{"Config": f"blobs/sha256/{digest}", "RepoTags": [f"deedkit-check-{target}:{sha}"],
                            "Layers": []}]).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as tar:
        for name, data in (("manifest.json", manifest), (f"blobs/sha256/{digest}", config)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return "sha256:" + digest, config


def exported(images, target, sha=SHA, salt="", **change):
    directory = images / f"image-{target}"
    config_digest, _ = archive(directory / "image.tar", target, sha, salt, **change)
    (directory / "image.json").write_text(json.dumps({"target": target, "sha": sha, "config_digest": config_digest}))
    return config_digest


class FakeRegistry(pi.Registry):
    """An in-memory registry: tags map to manifests, pushes copy the archive's config blob unchanged."""

    def __init__(self, rewrite=False):
        super().__init__("ghcr.io/gosync-inc")
        self.tags, self.configs, self.pushes, self.rewrite = {}, {}, [], rewrite
        self.reads, self.puts, self.reserialize, self.lose_put, self.lose_push = [], [], False, set(), False

    def store(self, reference, config):
        config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
        raw = json.dumps({"mediaType": MANIFEST, "config": {"digest": config_digest}}).encode()
        self.tags[reference] = raw
        self.configs[config_digest] = config

    def manifest(self, reference):
        self.reads.append(reference)
        raw = self.tags.get(reference)
        return None if raw is None else ("sha256:" + hashlib.sha256(raw).hexdigest(), json.loads(raw))

    def fetch(self, reference):
        """By digest: the stored bytes, or with `reserialize` a client's re-encoding of them."""
        self.reads.append(reference)
        name, _, digest = reference.partition("@")
        raw = next((raw for tag, raw in self.tags.items()
                    if tag.startswith(name + ":") and "sha256:" + hashlib.sha256(raw).hexdigest() == digest), None)
        return json.dumps(json.loads(raw), indent=1).encode() if raw is not None and self.reserialize else raw

    def put(self, reference, raw, media_type):
        assert media_type == json.loads(raw)["mediaType"]
        self.puts.append(reference)
        self.tags[reference] = raw
        if reference in self.lose_put:  # the registry stored it, the response never arrived
            raise pi.Refused(f"cannot write {reference}: connection reset by peer")

    def config(self, reference, manifest):
        return json.loads(self.configs[manifest["config"]["digest"]])

    def push(self, archive, reference):
        self.pushes.append(reference)
        _, config = pi.archive_config(archive)
        blob = json.dumps({**config, "rewritten": True} if self.rewrite else config, sort_keys=True).encode()
        self.store(reference, blob)
        if self.lose_push:  # the upload landed but skopeo reported a failure
            raise pi.Refused("skopeo copy failed (exit 1): connection reset by peer")


    def tagged(self, target, sha=SHA):
        found = self.manifest(f"{self.name(target)}:{sha}")
        assert found is not None, target
        return found


def published(registry, target, sha=SHA, salt="", **change):
    config = json.dumps({"architecture": "amd64", "config": {"Labels": labels(target, sha, **change)},
                         "rootfs": {"type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(salt.encode()).hexdigest()]}},
                        sort_keys=True).encode()
    registry.store(f"{registry.name(target)}:{sha}", config)
    return "sha256:" + hashlib.sha256(config).hexdigest()


def legacy(registry, target, images):
    """The final tag a run before ANYM-602 pushed straight from this archive (the same bytes push() stores)."""
    _, config = pi.archive_config(images / f"image-{target}" / "image.tar")
    registry.store(f"{registry.name(target)}:{SHA}", json.dumps(config, sort_keys=True).encode())


def never_attested(*_):
    return "none"


def always_attested(*_):
    return "complete"


def attested_only_in_github(*_):
    return "github"


def test_publication_targets_are_the_image_build_matrix():
    jobs = yaml.safe_load((ROOT / ".github/workflows/images.yml").read_text())["jobs"]
    assert list(pi.TARGETS) == jobs["build"]["strategy"]["matrix"]["target"]
    assert set(pi.TARGETS) == set(production.BUILDS) - {"http-router"}


def test_only_a_main_push_runs_the_publish_job_and_only_it_can_write():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    images = yaml.safe_load((ROOT / ".github/workflows/images.yml").read_text())
    assert ci["permissions"] == images["permissions"] == {"contents": "read"}
    job = ci["jobs"]["publish"]
    # A single job whose exact name production.py can require, after every verification passed.
    assert job["name"] == "publish" and "strategy" not in job and job["needs"] == ["gate"]
    assert job["if"] == ("${{ !cancelled() && needs.gate.result == 'success' "
                         "&& github.event_name == 'push' && github.ref == 'refs/heads/main' }}")
    assert job["permissions"] == {"contents": "read", "packages": "write", "id-token": "write", "attestations": "write"}
    writable = {"packages", "id-token", "attestations", "artifact-metadata", "contents", "actions"}
    for workflow in (ci, images):
        for name, other in workflow["jobs"].items():
            if other is not job:
                granted = {key for key, value in (other.get("permissions") or {}).items() if value == "write"}
                assert not granted & writable, name
    # The tested archive leaves the build job only on a main push run of the current commit.
    upload = next(step for step in images["jobs"]["build"]["steps"] if "upload-artifact" in step.get("uses", ""))
    assert upload["if"] == "${{ github.event_name == 'push' && github.ref == 'refs/heads/main' && inputs.source_ref == '' }}"
    assert upload["with"]["name"] == "image-${{ matrix.target }}" and upload["with"]["retention-days"] == 1
    # The Tiden release record waits for the publication; the required checks are untouched.
    assert ci["jobs"]["release"]["needs"] == ["gate", "publish", "checks"]
    assert ci["jobs"]["gate"]["name"] == "CI gate" and set(ci["jobs"]["gate"]["needs"]) == {"added_list", "reuse", "pr_reuse", "checks", "native", "images"}
    # Preparation pulls these images, so a promotable main run must show this exact job passed (ANYM-301 B3).
    assert "publish" in production.REQUIRED_JOBS


def test_build_jobs_export_the_tested_image_and_dry_run_its_publication():
    steps = yaml.safe_load((ROOT / ".github/workflows/images.yml").read_text())["jobs"]["build"]["steps"]
    names = [step.get("name") or step["uses"].split("@")[0] for step in steps]
    check = names.index("Build and smoke-test the production target without providers")
    export = names.index("Export the tested image and publish it to a local registry as a dry run")
    upload = names.index("Keep the tested image for the publish job")
    assert check < export < upload == len(steps) - 1
    run = steps[export]["run"]
    # The export names the same commit check-image built, and the dry run never leaves the runner.
    assert 'sha="$(git rev-parse HEAD)"' in run and "export --target \"$TARGET\" --sha \"$sha\"" in run
    assert "--local-dry-run --registry localhost:5000/dry-run" in run and "for expected in push reuse" in run
    assert '--attempt "$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"' in run
    assert '--directory "$RUNNER_TEMP/images/image-$TARGET"' in run
    assert steps[upload]["with"]["path"] == "${{ runner.temp }}/images/image-${{ matrix.target }}"
    # Pull requests and main pushes run it; merge queue runs and other-revision baselines do not.
    assert steps[export]["if"] == ("${{ github.event_name == 'pull_request' || (github.event_name == 'push' && "
                                   "github.ref == 'refs/heads/main' && inputs.source_ref == '') }}")
    service = yaml.safe_load((ROOT / ".github/workflows/images.yml").read_text())["jobs"]["build"]["services"]["registry"]
    assert re.fullmatch(r"registry:3@sha256:[0-9a-f]{64}", service["image"]) and service["ports"] == ["127.0.0.1:5000:5000"]


def test_publication_is_serialized_per_commit_and_never_cancelled():
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["publish"]
    assert job["concurrency"] == {"group": "publish-images-${{ github.sha }}", "cancel-in-progress": False}


def test_a_dry_run_writes_no_step_outputs(tmp_path, monkeypatch):
    outputs = tmp_path / "outputs"
    outputs.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    seen = []
    monkeypatch.setattr(pi, "publish", lambda registry, sha, images, out, verify, targets, attempt: seen.append(out) or [])
    monkeypatch.setattr(pi, "promote", lambda *_: {})
    pi.main(["publish", "--local-dry-run", "--registry", "localhost:5000/dry-run", "--sha", SHA, "--attempt", ATTEMPT,
             "--directory", str(tmp_path), "--target", "gateway"])
    pi.main(["publish", "--sha", SHA, "--attempt", ATTEMPT, "--directory", str(tmp_path), "--target", "gateway"])
    assert seen == [None, str(outputs)]


def test_a_damaged_archive_does_not_block_reuse_of_an_attested_tag(tmp_path):
    registry = FakeRegistry()
    published(registry, "calls")
    (tmp_path / "image-calls").mkdir()
    (tmp_path / "image-calls" / "image.json").write_text("{}")
    (tmp_path / "image-calls" / "image.tar").write_bytes(b"truncated")
    assert pi.publish_one(registry, "calls", SHA, tmp_path, always_attested)["action"] == "reuse"
    with pytest.raises(pi.Refused, match="unreadable"):  # an unattested tag still needs an intact archive
        pi.publish_one(registry, "calls", SHA, tmp_path, never_attested)


def test_a_timed_out_command_is_a_refusal(monkeypatch):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1800)
    monkeypatch.setattr(pi.subprocess, "run", slow)
    with pytest.raises(pi.Refused, match="did not finish"):
        pi.run(["skopeo", "copy"])


def test_publish_job_pushes_the_tested_archives_without_building():
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["publish"]
    text = json.dumps(job)
    assert "docker build" not in text and "check-image" not in text and "docker push" not in text
    steps = job["steps"]
    download = next(step for step in steps if "download-artifact" in step.get("uses", ""))
    assert download["with"] == {"pattern": "image-*", "path": "${{ runner.temp }}/images"}
    runs = [step.get("run", "") for step in steps]
    publish = next(i for i, run in enumerate(runs) if "publish_images.py publish" in run)
    confirm = next(i for i, run in enumerate(runs) if "publish_images.py confirm" in run)
    attests = [i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/attest@")]
    assert publish < min(attests) and max(attests) < confirm
    assert "--local-dry-run" not in runs[publish] and "--target" not in runs[publish]
    assert all("${{" not in run for run in runs)
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"].split()[0]) for step in steps if "uses" in step)


def test_every_target_is_attested_with_push_to_registry():
    steps = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["publish"]["steps"]
    attests = [step for step in steps if step.get("uses", "").startswith("actions/attest@")]
    assert len(attests) == len(pi.TARGETS)
    for target, step in zip(pi.TARGETS, attests, strict=True):
        key = pi.output_key(target)
        assert step["if"] == f"steps.publish.outputs.attest_{key} == 'true'"
        assert step["with"] == {"subject-name": f"ghcr.io/gosync-inc/deedkit-{target}",
                                "subject-digest": f"${{{{ steps.publish.outputs.digest_{key} }}}}",
                                "push-to-registry": True, "create-storage-record": False}


@pytest.mark.parametrize("existing, attested, artifact, action", [
    (None, None, {"config_digest": "sha256:a"}, "push"),
    (("sha256:m", {}), "complete", None, "reuse"),
    (("sha256:m", {}), "complete", {"config_digest": "sha256:other"}, "reuse"),
    (("sha256:m", {}), "none", {"config_digest": "sha256:a"}, "attest"),
    (("sha256:m", {}), "github", None, "attest"),
    (("sha256:m", {}), "github", {"config_digest": "sha256:other"}, "attest"),
], ids=["absent", "attested-without-artifact", "attested-after-full-rerun", "pushed-not-attested",
        "registry-bundle-missing", "registry-bundle-missing-after-full-rerun"])
def test_write_once_decision(existing, attested, artifact, action):
    assert pi.decide(existing, "sha256:a", attested, artifact) == action


@pytest.mark.parametrize("existing, artifact, message", [
    (None, None, "artifact is missing"),
    (("sha256:m", {}), None, "without a valid attestation"),
    (("sha256:m", {}), {"config_digest": "sha256:rebuilt"}, "without a valid attestation"),
], ids=["absent-and-expired", "unattested-and-expired", "unattested-and-rebuilt"])
def test_write_once_decision_never_moves_an_unverified_tag(existing, artifact, message):
    with pytest.raises(pi.Refused, match=message):
        pi.decide(existing, "sha256:a", "none", artifact)


def test_first_publication_pushes_the_exact_tested_config(tmp_path):
    registry = FakeRegistry()
    tested = exported(tmp_path, "runtime")
    result = pi.publish_one(registry, "runtime", SHA, tmp_path, never_attested, ATTEMPT)
    assert result["action"] == "push" and result["attest"] and registry.pushes == [f"ghcr.io/gosync-inc/deedkit-runtime:{STAGING}"]
    staged = registry.tagged("runtime", STAGING)
    assert staged[1]["config"]["digest"] == tested and result["digest"] == staged[0]
    assert registry.manifest(f"ghcr.io/gosync-inc/deedkit-runtime:{SHA}") is None  # final only after verification


def test_a_pushed_manifest_without_the_tested_config_is_refused(tmp_path):
    exported(tmp_path, "gateway")
    with pytest.raises(pi.Refused, match="tested image config"):
        pi.publish_one(FakeRegistry(rewrite=True), "gateway", SHA, tmp_path, never_attested, ATTEMPT)


def test_rerun_after_an_interrupted_attestation_attests_without_pushing(tmp_path):
    registry = FakeRegistry()
    exported(tmp_path, "calls")
    # A run before ANYM-602 pushed the final tag and its attest step never ran (job cancelled after the push).
    legacy(registry, "calls", tmp_path)
    # The rerun keeps the same archive: the legacy state is still repaired in place.
    second = pi.publish_one(registry, "calls", SHA, tmp_path, never_attested, ATTEMPT)
    assert second == {"target": "calls", "action": "attest", "digest": registry.tagged("calls")[0], "attest": True}
    assert registry.pushes == []


def test_rerun_after_artifacts_expired_refuses_an_unattested_tag_without_moving_it(tmp_path):
    registry = FakeRegistry()
    exported(tmp_path / "first", "backup")
    legacy(registry, "backup", tmp_path / "first")  # a final tag left unattested by a run before ANYM-602
    before = dict(registry.tags)
    with pytest.raises(pi.Refused, match="without a valid attestation"):
        pi.publish_one(registry, "backup", SHA, tmp_path / "expired", never_attested)
    exported(tmp_path / "rebuilt", "backup", salt="rebuilt")
    with pytest.raises(pi.Refused, match="without a valid attestation"):
        pi.publish_one(registry, "backup", SHA, tmp_path / "rebuilt", never_attested)
    assert registry.tags == before and registry.pushes == []


def test_full_rerun_reuses_the_attested_image_and_pushes_nothing(tmp_path):
    registry = FakeRegistry()
    digest = published(registry, "indexer")
    exported(tmp_path, "indexer", salt="rebuilt")  # builds are not reproducible
    result = pi.publish_one(registry, "indexer", SHA, tmp_path, always_attested)
    assert result["action"] == "reuse" and not result["attest"] and registry.pushes == []
    assert registry.tagged("indexer")[1]["config"]["digest"] == digest


@pytest.mark.parametrize("label, value", [
    ("org.opencontainers.image.revision", "d" * 40),
    ("org.opencontainers.image.source", "https://github.com/example/fork"),
    ("io.deedkit.production.build", "0" * 64),
])
def test_wrong_labels_are_refused_before_any_push(tmp_path, label, value):
    registry = FakeRegistry()
    exported(tmp_path, "calendar", **{label: value})
    with pytest.raises(pi.Refused, match="exported image .* wrong"):
        pi.publish_one(registry, "calendar", SHA, tmp_path, never_attested)
    published(registry, "whatsapp-native", **{label: value})
    with pytest.raises(pi.Refused, match="published image .* wrong"):
        pi.publish_one(registry, "whatsapp-native", SHA, tmp_path / "none", always_attested)
    assert registry.pushes == []


def test_an_archive_that_does_not_match_its_record_is_refused(tmp_path):
    exported(tmp_path, "enricher-local")
    record = tmp_path / "image-enricher-local" / "image.json"
    record.write_text(json.dumps({**json.loads(record.read_text()), "config_digest": "sha256:" + "0" * 64}))
    with pytest.raises(pi.Refused, match="does not match its record"):
        pi.publish_one(FakeRegistry(), "enricher-local", SHA, tmp_path, never_attested)


def test_publication_writes_digest_and_attest_outputs_for_every_target(tmp_path):
    registry = FakeRegistry()
    for target in pi.TARGETS:
        exported(tmp_path, target)
    published(registry, "gateway")
    outputs = tmp_path / "outputs"
    results = pi.publish(registry, SHA, tmp_path, str(outputs), lambda name, *_: "complete" if name.endswith("deedkit-gateway") else "none",
                         attempt=ATTEMPT)
    assert [result["action"] for result in results] == ["reuse" if t == "gateway" else "push" for t in pi.TARGETS]
    lines = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
    assert lines["attest_gateway"] == "false" and lines["attest_enricher_local"] == "true"
    assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", lines[f"digest_{pi.output_key(t)}"]) for t in pi.TARGETS)


def test_confirmation_requires_an_attested_image_for_every_target():
    registry = FakeRegistry()
    for target in pi.TARGETS:
        published(registry, target)
    result = pi.confirm(registry, SHA, always_attested)
    assert set(result) == set(pi.TARGETS) and all("@sha256:" in reference for reference in result.values())
    for state in ("none", "github"):  # a bundle missing from the registry is not a complete publication
        with pytest.raises(pi.Refused, match="calls is not published"):
            pi.confirm(registry, SHA, lambda name, *_, state=state: state if name.endswith("deedkit-calls") else "complete")
    del registry.tags[f"ghcr.io/gosync-inc/deedkit-backup:{SHA}"]
    with pytest.raises(pi.Refused, match="backup is not published"):
        pi.confirm(registry, SHA, always_attested)


def test_rerun_attaches_a_missing_registry_bundle_without_pushing(tmp_path):
    registry = FakeRegistry()
    published(registry, "runtime")
    result = pi.publish_one(registry, "runtime", SHA, tmp_path / "expired", attested_only_in_github)
    assert result["action"] == "attest" and result["attest"] and registry.pushes == []


def test_attestation_check_pins_workflow_ref_commit_and_hosted_runners(monkeypatch):
    # GitHub's API first, then the registry bundle: none, API only (attachment interrupted), both.
    for api, registry_bundle, state in [(1, 1, "none"), (0, 1, "github"), (0, 0, "complete")]:
        calls = []

        def run(argv, check=True, calls=calls, api=api, registry_bundle=registry_bundle):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, registry_bundle if "--bundle-from-oci" in argv else api)
        monkeypatch.setattr(pi, "run", run)
        assert pi.attested("ghcr.io/gosync-inc/deedkit-runtime", "sha256:" + "1" * 64, SHA) == state
        assert len(calls) == (1 if state == "none" else 2) and "--bundle-from-oci" not in calls[0]
        for argv in calls:
            assert argv[:4] == ["gh", "attestation", "verify", "oci://ghcr.io/gosync-inc/deedkit-runtime@sha256:" + "1" * 64]
            rest = [word for word in argv[4:] if word not in {"--bundle-from-oci", "--deny-self-hosted-runners"}]
            flags = dict(zip(rest[::2], rest[1::2]))
            assert flags["--repo"] == "GoSync-Inc/deedkit"
            assert flags["--signer-workflow"] == "GoSync-Inc/deedkit/.github/workflows/ci.yml"
            assert flags["--source-ref"] == "refs/heads/main" and flags["--source-digest"] == SHA
            assert "--deny-self-hosted-runners" in argv


def test_export_saves_the_tested_tag_and_records_its_config(tmp_path, monkeypatch):
    calls = []

    def run(argv, check=True):
        calls.append(argv)
        archive(Path(argv[argv.index("--output") + 1]), "gateway")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(pi, "run", run)
    record = pi.export("gateway", SHA, tmp_path / "image-gateway")
    assert calls == [["docker", "save", "--output", str(tmp_path / "image-gateway" / "image.tar"),
                      f"deedkit-check-gateway:{SHA}"]]
    assert record == json.loads((tmp_path / "image-gateway" / "image.json").read_text())
    assert record["config_digest"] == pi.archive_config(tmp_path / "image-gateway" / "image.tar")[0]
    monkeypatch.setattr(pi, "run", lambda argv, check=True: archive(Path(argv[3]), "gateway", sha="d" * 40) and None)
    with pytest.raises(pi.Refused, match="tested image for gateway has the wrong"):
        pi.export("gateway", SHA, tmp_path / "other")


@pytest.mark.parametrize("returncode, stderr, raw, outcome", [
    (0, "", json.dumps({"mediaType": MANIFEST, "config": {"digest": "sha256:x"}}), "manifest"),
    (1, "Error: manifest unknown", "", None),
    (1, "reading manifest: name unknown: repository name not known to registry", "", None),
    (1, "Error: unauthorized: authentication required", "", "refused"),
    (0, "", json.dumps({"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": []}), "refused"),
], ids=["present", "manifest-unknown", "name-unknown", "auth-failure", "index"])
def test_registry_treats_only_a_missing_tag_as_absent(monkeypatch, returncode, stderr, raw, outcome):
    calls = []
    monkeypatch.setattr(pi, "run", lambda argv, check=True: calls.append(argv) or subprocess.CompletedProcess(argv, returncode, raw, stderr))
    registry = pi.Registry("localhost:5000/dry-run", insecure=True)
    if outcome == "refused":
        with pytest.raises(pi.Refused):
            registry.manifest("localhost:5000/dry-run/deedkit-runtime:" + SHA)
    else:
        found = registry.manifest("localhost:5000/dry-run/deedkit-runtime:" + SHA)
        assert (found is None) == (outcome is None)
        if found:
            assert found[0] == "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    assert calls[0][:4] == ["skopeo", "inspect", "--tls-verify=false", "--raw"]


def test_dry_run_is_limited_to_a_localhost_registry_and_an_exact_sha(capsys):
    with pytest.raises(SystemExit):
        pi.main(["publish", "--local-dry-run", "--registry", "ghcr.io/gosync-inc", "--sha", SHA, "--directory", "x"])
    assert "only to a localhost registry" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        pi.main(["publish", "--sha", "main", "--directory", "x"])
    assert "40-character" in capsys.readouterr().err


# spec: ANYM-300
def test_publication_and_confirmation_check_every_target_in_parallel_and_finish_all_before_a_refusal(tmp_path):
    import threading
    registry = FakeRegistry()
    for target in pi.TARGETS:
        published(registry, target)
    # Every target's attestation check waits until all of them are running: serial checks would break the barrier.
    barrier = threading.Barrier(len(pi.TARGETS), timeout=10)

    def verify(name, *_):
        barrier.wait()
        return "none" if name.endswith("deedkit-calls") else "complete"
    for target in pi.TARGETS:
        if target != "calls":
            exported(tmp_path, target)
    # The calls tag exists unattested and no tested archive is here.
    with pytest.raises(pi.Refused, match="without a valid attestation"):
        pi.publish(registry, SHA, tmp_path, str(tmp_path / "outputs"), verify)
    assert not (tmp_path / "outputs").exists()  # no step outputs, so no attest step runs after a refusal
    barrier.reset()
    with pytest.raises(pi.Refused, match="calls is not published"):
        pi.confirm(registry, SHA, verify)
    barrier.reset()

    def complete(*_):
        barrier.wait()
        return "complete"
    assert list(pi.confirm(registry, SHA, complete)) == list(pi.TARGETS)


# Observed on main push runs 39591862192, 39592330444 and 39597355887 (gh 2.85 and skopeo wording).
TRANSIENT_FAILURES = [
    "Error: HTTP 502: Bad Gateway (https://api.github.com/repos/GoSync-Inc/deedkit/attestations/sha256:1)",
    "Error: HTTP 429: Too Many Requests",
    "Error: HTTP 403: API rate limit exceeded for installation ID 1.",
    "error creating Sigstore verifier: no valid Sigstore verifiers could be initialized",
    'pinging container registry ghcr.io: Get "https://ghcr.io/v2/": EOF',
    "read tcp 10.1.0.4:443: connection reset by peer",
    "net/http: TLS handshake timeout",
    # gh's OCI client (go-containerregistry) and skopeo (containers/image) word a registry 5xx this way.
    "Error: failed to fetch remote image: GET https://ghcr.io/v2/: unexpected status code 500 Internal Server Error",
    "reading manifest " + SHA + ": StatusCode: 503, Service Unavailable: backend not found",
]


def gh_attestation(monkeypatch, outcomes):
    """`gh attestation verify` answers from a list of (exit, stderr) per call; returns the calls and backoff delays."""
    calls, delays, outcomes = [], [], list(outcomes)

    def run(argv, check=True):
        calls.append(argv)
        code, stderr = outcomes.pop(0)
        return subprocess.CompletedProcess(argv, code, "", stderr)
    monkeypatch.setattr(pi, "run", run)
    monkeypatch.setattr(pi.time, "sleep", delays.append)
    return calls, delays


# spec: ANYM-300
@pytest.mark.parametrize("stderr", TRANSIENT_FAILURES, ids=["api-502", "api-429", "rate-limit", "sigstore", "eof",
                                                            "reset", "tls-timeout", "oci-500", "skopeo-503"])
def test_attestation_check_retries_a_transient_failure_with_backoff(monkeypatch, stderr):
    calls, delays = gh_attestation(monkeypatch, [(1, stderr), (1, stderr), (0, ""), (1, stderr), (0, "")])
    assert pi.attested("ghcr.io/gosync-inc/deedkit-backup", "sha256:" + "1" * 64, SHA) == "complete"
    assert len(calls) == 5 and ["--bundle-from-oci" in argv for argv in calls] == [False] * 3 + [True] * 2
    assert delays == [pi.BACKOFF, 2 * pi.BACKOFF, pi.BACKOFF]


# spec: ANYM-300
@pytest.mark.parametrize("stderr", [TRANSIENT_FAILURES[0], TRANSIENT_FAILURES[7]], ids=["api-502", "oci-500"])
@pytest.mark.parametrize("bundle", [False, True], ids=["api", "registry-bundle"])
def test_attestation_check_refuses_when_a_transient_failure_persists(monkeypatch, bundle, stderr):
    failures = [(1, stderr)] * pi.ATTEMPTS
    calls, delays = gh_attestation(monkeypatch, [(0, "")] * bundle + failures)
    with pytest.raises(pi.Refused, match="could not be verified.*50[02]"):
        pi.attested("ghcr.io/gosync-inc/deedkit-backup", "sha256:" + "1" * 64, SHA)
    assert len(calls) == bundle + pi.ATTEMPTS and delays == [pi.BACKOFF * 2 ** n for n in range(pi.ATTEMPTS - 1)]


# spec: ANYM-300
@pytest.mark.parametrize("api, bundle, state", [
    ((1, "Error: HTTP 404: Not Found (https://api.github.com/repos/GoSync-Inc/deedkit/attestations/sha256:1)"), None, "none"),
    ((1, "Error: no attestations found for subject"), None, "none"),
    ((1, "Error: expected SourceRepositoryDigest to be " + SHA + ", got " + "d" * 40), None, "none"),
    ((0, ""), (1, "Error: no attestations found with predicate type: https://slsa.dev/provenance/v1"), "github"),
], ids=["api-404", "api-none", "wrong-commit", "bundle-missing"])
def test_a_missing_or_mismatched_attestation_is_final_without_a_retry(monkeypatch, api, bundle, state):
    calls, delays = gh_attestation(monkeypatch, [api] + ([bundle] if bundle else []))
    assert pi.attested("ghcr.io/gosync-inc/deedkit-backup", "sha256:" + "1" * 64, SHA) == state
    assert len(calls) == (2 if bundle else 1) and delays == []


# spec: ANYM-300
def test_confirmation_survives_a_transient_attestation_api_failure(monkeypatch):
    registry = FakeRegistry()
    for target in pi.TARGETS:
        published(registry, target)
    failed = set()

    def run(argv, check=True):
        subject = argv[3]
        if subject not in failed and "--bundle-from-oci" in argv:  # each image's first registry lookup drops
            failed.add(subject)
            return subprocess.CompletedProcess(argv, 1, "", TRANSIENT_FAILURES[4])
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(pi, "run", run)
    monkeypatch.setattr(pi.time, "sleep", lambda _: None)
    assert list(pi.confirm(registry, SHA)) == list(pi.TARGETS) and len(failed) == len(pi.TARGETS)


# spec: ANYM-300
def test_registry_reads_retry_a_transient_network_failure_and_nothing_else(monkeypatch):
    config = b'{"config": {}}'
    raw = json.dumps({"mediaType": MANIFEST, "config": {"digest": "sha256:" + hashlib.sha256(config).hexdigest()}})
    registry, reference = pi.Registry("ghcr.io/gosync-inc"), "ghcr.io/gosync-inc/deedkit-runtime:" + SHA
    outcomes = [(1, "", TRANSIENT_FAILURES[4]), (0, raw, ""), (1, "", TRANSIENT_FAILURES[5]), (0, config.decode(), "")]
    calls, delays = [], []
    monkeypatch.setattr(pi, "run", lambda argv, check=True: calls.append(argv) or subprocess.CompletedProcess(
        argv, *outcomes.pop(0)))
    monkeypatch.setattr(pi.time, "sleep", delays.append)
    found = registry.manifest(reference)
    assert found is not None and found[1]["mediaType"] == MANIFEST and registry.config(reference, found[1]) == {"config": {}}
    assert len(calls) == 4 and delays == [pi.BACKOFF, pi.BACKOFF]
    outcomes[:] = [(1, "", "Error: unauthorized: authentication required")]
    with pytest.raises(pi.Refused, match="unauthorized"):  # not a network failure: refused at once
        registry.manifest(reference)
    outcomes[:] = [(1, "", TRANSIENT_FAILURES[4])] * pi.ATTEMPTS
    with pytest.raises(pi.Refused, match="EOF"):
        registry.manifest(reference)
    assert len(calls) == 5 + pi.ATTEMPTS


# spec: ANYM-300
@pytest.mark.parametrize("stderr", [
    TRANSIENT_FAILURES[8],
    'pinging container registry ghcr.io: Get "https://ghcr.io/v2/": EOF (manifest unknown)',
    "Error: 404 page not found",
], ids=["503-backend-not-found", "eof-manifest-unknown", "bare-not-found"])
def test_an_unreadable_registry_never_reads_an_existing_tag_as_absent(tmp_path, monkeypatch, stderr):
    class Unreadable(FakeRegistry):
        manifest = pi.Registry.manifest  # skopeo, whose every lookup fails with this stderr

    registry = Unreadable()
    published(registry, "runtime")
    exported(tmp_path, "runtime", salt="rebuilt")  # a rebuilt archive: pushing it would move the tag
    monkeypatch.setattr(pi, "run", lambda argv, check=True: subprocess.CompletedProcess(argv, 1, "", stderr))
    monkeypatch.setattr(pi.time, "sleep", lambda _: None)
    before = dict(registry.tags)
    with pytest.raises(pi.Refused, match="cannot read"):
        pi.publish_one(registry, "runtime", SHA, tmp_path, never_attested)
    assert registry.pushes == [] and registry.tags == before


# Verbatim from main push runs 39810919958 and 39821619870: gh could not build its public-good verifier.
VERIFIER_UNAVAILABLE = ("Error: failed to choose verifier based on provided bundle issuer: public good verifier is not "
                        "available (initialization may have failed)")


# spec: ANYM-300
@pytest.mark.parametrize("bundle", [False, True], ids=["api", "registry-bundle"])
def test_attestation_check_retries_when_gh_cannot_build_its_verifier(monkeypatch, bundle):
    calls, delays = gh_attestation(monkeypatch, [(0, "")] * bundle + [(1, VERIFIER_UNAVAILABLE)] + [(0, "")] * (2 - bundle))
    assert pi.attested("ghcr.io/gosync-inc/deedkit-backup", "sha256:" + "1" * 64, SHA) == "complete"
    assert len(calls) == 3 and delays == [pi.BACKOFF]


# spec: ANYM-300
@pytest.mark.parametrize("bundle", [False, True], ids=["api", "registry-bundle"])
def test_attestation_check_refuses_and_never_reads_none_when_the_verifier_stays_unavailable(monkeypatch, bundle):
    calls, delays = gh_attestation(monkeypatch, [(0, "")] * bundle + [(1, VERIFIER_UNAVAILABLE)] * pi.ATTEMPTS)
    with pytest.raises(pi.Refused, match="could not be verified.*verifier is not available"):
        pi.attested("ghcr.io/gosync-inc/deedkit-backup", "sha256:" + "1" * 64, SHA)
    assert len(calls) == bundle + pi.ATTEMPTS and delays == [pi.BACKOFF * 2 ** n for n in range(pi.ATTEMPTS - 1)]


def attested_set(digests):
    """A verifier that answers "complete" only for digests an attest step covered (GitHub and the registry bundle)."""
    def verify(name, digest, sha):
        return "complete" if digest in digests else "none"
    return verify


def publish_others(registry, digests, skip):
    for target in pi.TARGETS:
        if target not in skip:
            published(registry, target)
            digests.add(registry.tagged(target)[0])


# spec: ANYM-602
def test_a_new_target_gets_its_final_tag_on_the_attested_digest_only_after_verification(tmp_path):
    registry, digests, events = FakeRegistry(), set(), []
    exported(tmp_path, "runtime")
    result = pi.publish_one(registry, "runtime", SHA, tmp_path, never_attested, ATTEMPT)
    final = f"{registry.name('runtime')}:{SHA}"
    assert final not in registry.tags and registry.pushes == [f"{registry.name('runtime')}:{STAGING}"]
    digests.add(result["digest"])  # the attest step for the staged digest
    verify = attested_set(digests)
    assert pi.promote_one(registry, "runtime", SHA, ATTEMPT,
                          lambda *args: events.append(list(registry.puts)) or verify(*args)) == result["digest"]
    assert events == [[]] and registry.puts == [final]  # verified once, before any PUT
    assert registry.tagged("runtime")[0] == registry.tagged("runtime", STAGING)[0] == result["digest"]


# spec: ANYM-602
@pytest.mark.parametrize("state", ["none", "github"])
def test_a_failed_verification_leaves_only_the_staging_tag(tmp_path, state):
    registry = FakeRegistry()
    exported(tmp_path, "gateway")
    result = pi.publish_one(registry, "gateway", SHA, tmp_path, never_attested, ATTEMPT)
    with pytest.raises(pi.Refused, match="no complete attestation; its final tag is not written"):
        pi.promote_one(registry, "gateway", SHA, ATTEMPT, lambda *_: state)
    assert list(registry.tags) == [f"{registry.name('gateway')}:{STAGING}"] and registry.puts == []
    assert registry.tagged("gateway", STAGING)[0] == result["digest"]


# spec: ANYM-602
def test_a_full_rerun_after_a_failed_attestation_publishes_its_own_rebuilt_archive(tmp_path):
    registry, digests = FakeRegistry(), set()
    publish_others(registry, digests, {"calls"})
    exported(tmp_path / "1", "calls")
    first = pi.publish(registry, SHA, tmp_path / "1", None, attested_set(digests), ("calls",), "40667989770-1")
    # attempt 1's attest step failed (Rekor timeout): nothing attested its digest, so promote stops before the final tag
    with pytest.raises(pi.Refused, match="no complete attestation"):
        pi.promote(registry, SHA, "40667989770-1", attested_set(digests))
    stranded = f"{registry.name('calls')}:{SHA}-attempt-40667989770-1"
    assert registry.tags.get(stranded) and f"{registry.name('calls')}:{SHA}" not in registry.tags
    # "Re-run all jobs" rebuilds the image: another config digest, a new staging tag of attempt 2
    exported(tmp_path / "2", "calls", salt="rebuilt")
    registry.reads.clear()
    second = pi.publish(registry, SHA, tmp_path / "2", None, attested_set(digests), ("calls",), "40667989770-2")
    assert second[0]["action"] == "push" and second[0]["digest"] != first[0]["digest"]
    digests.add(second[0]["digest"])
    pi.promote(registry, SHA, "40667989770-2", attested_set(digests))
    confirmed = pi.confirm(registry, SHA, attested_set(digests), "40667989770-2")
    assert confirmed["calls"] == f"{registry.name('calls')}@{second[0]['digest']}"
    assert stranded in registry.tags and stranded not in registry.reads  # left, never read


# spec: ANYM-602
def test_a_rerun_reuses_a_final_tag_whose_put_response_was_lost_and_publishes_the_rest(tmp_path):
    registry, digests = FakeRegistry(), set()
    publish_others(registry, digests, {"gateway", "calls"})
    for target in ("gateway", "calls"):
        exported(tmp_path / "1", target)
    first = {r["target"]: r["digest"] for r in pi.publish(registry, SHA, tmp_path / "1", None, attested_set(digests),
                                                          ("gateway", "calls"), "41-1")}
    digests.add(first["gateway"])  # calls' attestation failed
    registry.lose_put.add(f"{registry.name('gateway')}:{SHA}")
    with pytest.raises(pi.Refused, match="gateway|calls"):
        pi.promote(registry, SHA, "41-1", attested_set(digests), ("gateway", "calls"))
    assert registry.tagged("gateway")[0] == first["gateway"] and f"{registry.name('calls')}:{SHA}" not in registry.tags
    # attempt 2: gateway's attested final tag is reused without an archive; calls publishes its own rebuild
    exported(tmp_path / "2", "calls", salt="rebuilt")
    results = {r["target"]: r for r in pi.publish(registry, SHA, tmp_path / "2", None, attested_set(digests),
                                                  ("gateway", "calls"), "41-2")}
    assert results["gateway"]["action"] == "reuse" and not results["gateway"]["attest"]
    assert results["calls"]["action"] == "push" and f"{registry.name('gateway')}:{SHA}-attempt-41-2" not in registry.tags
    digests.add(results["calls"]["digest"])
    assert pi.promote(registry, SHA, "41-2", attested_set(digests), ("gateway", "calls")) == {
        "gateway": None, "calls": results["calls"]["digest"]}
    confirmed = pi.confirm(registry, SHA, attested_set(digests), "41-2")
    assert confirmed["gateway"].endswith(first["gateway"]) and confirmed["calls"].endswith(results["calls"]["digest"])


# spec: ANYM-602
def test_promotion_refuses_a_client_side_conversion_before_the_final_put(tmp_path):
    registry = FakeRegistry()
    exported(tmp_path, "indexer")
    pi.publish_one(registry, "indexer", SHA, tmp_path, never_attested, ATTEMPT)
    registry.reserialize = True
    with pytest.raises(pi.Refused, match="not the verified bytes; its final tag is not written"):
        pi.promote_one(registry, "indexer", SHA, ATTEMPT, always_attested)
    assert registry.puts == [] and f"{registry.name('indexer')}:{SHA}" not in registry.tags


# spec: ANYM-602
def test_confirmation_requires_the_final_digest_to_be_the_verified_staging_digest(tmp_path):
    registry = FakeRegistry()
    publish_others(registry, set(), {"backup"})
    exported(tmp_path, "backup")
    pi.publish_one(registry, "backup", SHA, tmp_path, never_attested, ATTEMPT)
    # The registry answers the final tag with another digest B, which also carries a valid attestation.
    published(registry, "backup", salt="other")
    assert registry.tagged("backup")[0] != registry.tagged("backup", STAGING)[0]
    with pytest.raises(pi.Refused, match="backup final tag does not hold the image this attempt staged"):
        pi.confirm(registry, SHA, always_attested, ATTEMPT)
    with pytest.raises(pi.Refused, match="backup final tag holds another image"):
        pi.promote_one(registry, "backup", SHA, ATTEMPT, always_attested)
    # The backstop stays: an unattested final tag fails confirmation with or without a staging tag.
    registry.tags[f"{registry.name('backup')}:{SHA}"] = registry.tags[f"{registry.name('backup')}:{STAGING}"]
    with pytest.raises(pi.Refused, match="backup is not published"):
        pi.confirm(registry, SHA, lambda name, *_: "none" if name.endswith("backup") else "complete", ATTEMPT)
    assert set(pi.confirm(registry, SHA, always_attested, ATTEMPT)) == set(pi.TARGETS)


# spec: ANYM-602
def test_production_reads_a_staging_only_package_as_not_published(monkeypatch):
    registry = FakeRegistry()
    registry.store(f"{registry.name('runtime')}:{STAGING}", b'{"config": {}}')
    calls = []

    def verifier(argv, **kwargs):  # tools/attest-verify resolves exactly the tag --sha (main.go attest: ref := sha)
        calls.append(argv)
        image, sha = argv[argv.index("--image") + 1], argv[argv.index("--sha") + 1]
        found = registry.manifest(f"{image}:{sha}")
        if found is None:
            raise production.Refused(f"attest-verify failed (exit 1): attest-verify: refused: manifest {image}:{sha} not found")
        return json.dumps({"sha": sha, "digest": found[0], "config_digest": "sha256:" + "0" * 64,
                           "reference": f"{image}@{found[0]}"})
    monkeypatch.setattr(production, "check_verifier", lambda: None)
    monkeypatch.setattr(production, "run", verifier)
    with pytest.raises(production.Unattested):
        production.published("runtime", SHA)
    assert calls[0][1:] == ["--image", production.REGISTRY + "runtime", "--sha", SHA]
    registry.tags[f"{registry.name('runtime')}:{SHA}"] = registry.tags[f"{registry.name('runtime')}:{STAGING}"]
    assert production.published("runtime", SHA)["digest"] == registry.tagged("runtime")[0]


# spec: ANYM-602
def test_the_dry_run_verifier_answers_complete_only_for_its_own_staged_digest_and_only_under_local_dry_run(tmp_path, monkeypatch):
    registry = FakeRegistry()
    exported(tmp_path, "calendar")
    staged = pi.publish_one(registry, "calendar", SHA, tmp_path, never_attested, ATTEMPT)["digest"]
    verify = pi.dry_run_verifier(registry, ATTEMPT)
    name = registry.name("calendar")
    assert verify(name, staged, SHA) == "complete"
    assert verify(name, "sha256:" + "1" * 64, SHA) == "none" and pi.dry_run_verifier(registry, "1-9")(name, staged, SHA) == "none"
    seen = []
    for action in ("publish", "promote", "confirm"):
        monkeypatch.setattr(pi, action, lambda *args, **_: seen.append(args) or {})
    for action in ("publish", "promote", "confirm"):
        pi.main([action, "--sha", SHA, "--attempt", ATTEMPT, "--directory", str(tmp_path)])
    assert len(seen) == 3 and all(pi.attested in args for args in seen)
    seen.clear()
    pi.main(["publish", "--local-dry-run", "--registry", "localhost:5000/dry-run", "--sha", SHA, "--attempt", ATTEMPT,
             "--directory", str(tmp_path)])
    assert all(pi.attested not in args for args in seen) and len(seen) == 2  # publish, then promote
    with pytest.raises(SystemExit):  # never against a remote registry, and never without this attempt
        pi.main(["publish", "--local-dry-run", "--registry", "ghcr.io/gosync-inc", "--sha", SHA, "--attempt", ATTEMPT])
    with pytest.raises(SystemExit):
        pi.main(["promote", "--sha", SHA])


# spec: ANYM-602
def test_the_publish_job_promotes_between_the_attest_steps_and_confirm():
    steps = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["publish"]["steps"]
    runs = [step.get("run", "") for step in steps]
    index = {action: next(i for i, run in enumerate(runs) if f"publish_images.py {action}" in run)
             for action in ("publish", "promote", "confirm")}
    attests = [i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/attest@")]
    assert index["publish"] < min(attests) and max(attests) < index["promote"] == index["confirm"] - 1
    for action in index.values():
        assert '--attempt "$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"' in runs[action]
    assert steps[index["promote"]]["env"] == {"GH_TOKEN": "${{ github.token }}", "ACTOR": "${{ github.actor }}",
                                              "SHA": "${{ github.sha }}"}
    assert "if" not in steps[index["promote"]]


FAULTS = ("push", "attest", "verify", "put-lost", "none")


# spec: ANYM-602
@hypothesis.settings(max_examples=60, deadline=None)
@hypothesis.given(st.lists(st.sampled_from(FAULTS), min_size=1, max_size=5))
def test_any_sequence_of_failed_attempts_never_moves_or_strands_the_final_tag(faults):
    """Each full rerun rebuilds its archive. Whatever fails, the final tag only ever names an attested digest, is
    never moved once written, and the first attempt with no failure publishes and confirms."""
    import tempfile
    registry, digests, final = FakeRegistry(), set(), None
    publish_others(registry, digests, {"calls"})
    reference = f"{registry.name('calls')}:{SHA}"
    with tempfile.TemporaryDirectory() as directory:
        for number, fault in enumerate(faults + ["none"], start=1):
            attempt, images = f"7-{number}", Path(directory) / str(number)
            exported(images, "calls", salt=str(number))
            verify = attested_set(digests) if fault != "verify" else (lambda *_: "github")
            registry.lose_push = fault == "push"
            if fault == "put-lost":
                registry.lose_put.add(reference)
            try:
                result = pi.publish(registry, SHA, images, None, verify, ("calls",), attempt)[0]
                if result["attest"] and fault != "attest":
                    digests.add(result["digest"])
                pi.promote(registry, SHA, attempt, verify, ("calls",))
                confirmed = pi.confirm(registry, SHA, verify, attempt)
                # An injected fault may never fire: a rerun that reuses an attested final tag pushes nothing.
                assert confirmed["calls"] == f"{registry.name('calls')}@{registry.tagged('calls')[0]}"
                fault = "none"
            except pi.Refused:
                assert fault != "none"
            finally:
                registry.lose_push, registry.lose_put = False, set()
            if reference in registry.tags:
                digest = registry.tagged("calls")[0]
                assert digest in digests and final in (None, digest)
                final = digest
            if fault == "none":
                break
    assert final is not None
