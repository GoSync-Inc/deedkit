"""Synthetic delivery guards for the separately isolated restore image."""
import json
from pathlib import Path

import pytest

from scripts import production as promotion
from scripts import tiden_ci


def test_backup_image_verification_is_required_even_with_a_green_summary():
    sha = "a" * 40
    jobs = [{"name": name, "status": "completed", "conclusion": "success"}
            for name in promotion.REQUIRED_JOBS if name != "images / build (backup)"]

    def get(path):
        if path == "git/ref/heads/main":
            return {"object": {"sha": sha}}
        if "/runs?" in path:
            return {"workflow_runs": [{"id": 12, "run_attempt": 1, "head_sha": sha,
                    "head_branch": "main", "event": "push", "status": "completed",
                    "conclusion": "success", "html_url": "https://example.invalid/run/12",
                    "head_repository": {"full_name": promotion.REPOSITORY}}]}
        return {"jobs": jobs}

    with pytest.raises(promotion.Refused, match="required job"):
        promotion.verify(sha, get)


def test_backup_image_smoke_uses_native_tools_without_secrets_or_network(monkeypatch):
    calls, tagged = [], []
    image = "sha256:" + "b" * 64
    manifest = "sha256:" + "9" * 64

    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "tag"]:
            tagged.append(args[-1])
        if args[:3] == ["docker", "image", "ls"]:
            return image if tagged else ""
        if args[:3] != ["docker", "image", "inspect"]:
            return ""
        if "--format" in args:
            return image
        # The CI-published backup image names its source revision and build (scripts/production.py).
        return json.dumps([{"Id": image, "Config": {"Labels": {
            "org.opencontainers.image.revision": "a" * 40, "org.opencontainers.image.source": "https://github.com/" + promotion.REPOSITORY,
            "io.deedkit.production.build": promotion.build_recipe("backup", "a" * 40)[1]},
            "Env": ["DEEDKIT_SOURCE_SHA=" + "a" * 40]}}])

    monkeypatch.setattr(promotion, "run", run)
    # A fake attestation verifier: preparation pulls the image CI published for this revision (ANYM-301).
    monkeypatch.setattr(promotion, "published", lambda target, sha: {
        "reference": promotion.REGISTRY + target + "@" + manifest, "digest": manifest, "config_digest": image})
    assert promotion.build_image(Path("/synthetic-source"), "a" * 40, "backup") == image
    smoke = next(args for args in calls if args[:2] == ["docker", "run"])
    assert smoke[smoke.index("--network") + 1] == "none"
    assert "--read-only" in smoke and "--cap-drop" in smoke
    assert not any(arg in smoke for arg in ("--volume", "-v", "--env-file", "--privileged"))
    assert image in smoke
    for required in ("backup_restore_worker", "age", "pg_dump", "pg_restore"):
        assert required in smoke[-1]


def test_native_restore_failure_survives_reporting_with_original_check_identities(tmp_path):
    modules = {**tiden_ci.MODULE_REPORTS, "backup_restore": ("backup-restore.xml", "python", "")}
    for name, (filename, _, _) in modules.items():
        failure = '<failure message="synthetic restore failure"/>' if name == "backup_restore" else ""
        (tmp_path / filename).write_text(
            f'<testsuite><testcase name="{name}" file="tests/synthetic.py">{failure}</testcase></testsuite>')
    for filename in tiden_ci.REPORTS.values():
        (tmp_path / filename).write_text(
            '<testsuite><testcase name="synthetic" file="tests/synthetic.py"/></testsuite>')
    metadata = {"sha": "a" * 40, "git_branch": "codex/synthetic",
                "ci_url": "https://example.invalid/synthetic-run",
                "checks": {name: "success" for name in tiden_ci.CHECKS},
                "module_checks": {name: "success" for name in modules}}
    result = tiden_ci.prepare(tmp_path, tmp_path, metadata)
    checks = json.loads((tmp_path / "ci-checks.json").read_text())
    assert result["test_counts"]["failed"] == 1
    assert next(row for row in checks if row["signature"] == "deedkit-ci::backup_restore")["execution"]["status"] == "failed"
    assert {row["signature"] for row in checks if row["signature"].startswith("anymsg-ci::")} == {
        "anymsg-ci::" + name for name in ("build", "release", "python", "collector", "npm", "smoke", "distributions")}
