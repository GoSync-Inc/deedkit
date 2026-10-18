import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile

import pytest
import yaml

from scripts import publish_images as pi
from scripts import production

ROOT = Path(__file__).resolve().parents[1]
SHA = "c" * 40
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

    def store(self, reference, config):
        config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
        raw = json.dumps({"mediaType": MANIFEST, "config": {"digest": config_digest}}).encode()
        self.tags[reference] = raw
        self.configs[config_digest] = config

    def manifest(self, reference):
        raw = self.tags.get(reference)
        return None if raw is None else ("sha256:" + hashlib.sha256(raw).hexdigest(), json.loads(raw))

    def config(self, reference, manifest):
        return json.loads(self.configs[manifest["config"]["digest"]])

    def push(self, archive, reference):
        self.pushes.append(reference)
        _, config = pi.archive_config(archive)
        blob = json.dumps({**config, "rewritten": True} if self.rewrite else config, sort_keys=True).encode()
        self.store(reference, blob)


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
    assert ci["jobs"]["release"]["needs"] == ["gate", "publish"]
    assert ci["jobs"]["gate"]["name"] == "CI gate" and set(ci["jobs"]["gate"]["needs"]) == {"reuse", "pr_reuse", "checks", "native", "images"}
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
    assert "--local-dry-run --registry localhost:5000/dry-run" in run and "for expected in push attest" in run
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
    monkeypatch.setattr(pi, "publish", lambda registry, sha, images, out, verify, targets: seen.append(out) or [])
    pi.main(["publish", "--local-dry-run", "--registry", "localhost:5000/dry-run", "--sha", SHA,
             "--directory", str(tmp_path), "--target", "gateway"])
    pi.main(["publish", "--sha", SHA, "--directory", str(tmp_path), "--target", "gateway"])
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
    result = pi.publish_one(registry, "runtime", SHA, tmp_path, never_attested)
    assert result["action"] == "push" and result["attest"] and registry.pushes == [f"ghcr.io/gosync-inc/deedkit-runtime:{SHA}"]
    assert registry.tagged("runtime")[1]["config"]["digest"] == tested and result["digest"] == registry.tagged("runtime")[0]


def test_a_pushed_manifest_without_the_tested_config_is_refused(tmp_path):
    exported(tmp_path, "gateway")
    with pytest.raises(pi.Refused, match="tested image config"):
        pi.publish_one(FakeRegistry(rewrite=True), "gateway", SHA, tmp_path, never_attested)


def test_rerun_after_an_interrupted_attestation_attests_without_pushing(tmp_path):
    registry = FakeRegistry()
    exported(tmp_path, "calls")
    first = pi.publish_one(registry, "calls", SHA, tmp_path, never_attested)
    # The attest step never ran (job cancelled after the push); the rerun keeps the same archive.
    second = pi.publish_one(registry, "calls", SHA, tmp_path, never_attested)
    assert second == {**first, "action": "attest"} and len(registry.pushes) == 1


def test_rerun_after_artifacts_expired_refuses_an_unattested_tag_without_moving_it(tmp_path):
    registry = FakeRegistry()
    exported(tmp_path / "first", "backup")
    pi.publish_one(registry, "backup", SHA, tmp_path / "first", never_attested)
    before = dict(registry.tags)
    with pytest.raises(pi.Refused, match="without a valid attestation"):
        pi.publish_one(registry, "backup", SHA, tmp_path / "expired", never_attested)
    exported(tmp_path / "rebuilt", "backup", salt="rebuilt")
    with pytest.raises(pi.Refused, match="without a valid attestation"):
        pi.publish_one(registry, "backup", SHA, tmp_path / "rebuilt", never_attested)
    assert registry.tags == before and len(registry.pushes) == 1


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
    results = pi.publish(registry, SHA, tmp_path, str(outputs), lambda name, *_: "complete" if name.endswith("deedkit-gateway") else "none")
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
