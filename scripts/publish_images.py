#!/usr/bin/env python3
"""Publish the exact images CI built and smoke-tested to GHCR, write-once per source SHA.

A new image is pushed under a staging tag `<sha>-attempt-<run_id>-<run_attempt>` and gets its final tag `<sha>`
only after its attestation verifies, as the same manifest bytes (ANYM-602)."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import production  # noqa: E402

REGISTRY = "ghcr.io/gosync-inc"
# The images.yml matrix; tests/test_publish_images.py keeps both lists equal.
TARGETS = ("runtime", "indexer", "enricher-local", "whatsapp-native", "gateway", "calendar", "calls", "backup")
WORKFLOW = production.REPOSITORY + "/.github/workflows/ci.yml"
SOURCE = "https://github.com/" + production.REPOSITORY
ABSENT = ("manifest unknown", "name unknown")  # the registry's MANIFEST_UNKNOWN / NAME_UNKNOWN error codes
MANIFESTS = {"application/vnd.docker.distribution.manifest.v2+json", "application/vnd.oci.image.manifest.v1+json"}
# A network or API failure that says nothing about the image: a read retries it ATTEMPTS times, waiting BACKOFF,
# 2*BACKOFF, ... seconds in between. Anything else (404, "no attestations found", a policy mismatch) is final.
# Every gh call initializes its Sigstore verifier, which can fail transiently (likely the trusted-root fetch); that says nothing about the image.
TRANSIENT = re.compile(r"(?:HTTP(?: status:)?|status code|StatusCode:) (?:429|5\d\d)|rate limit|Internal Server Error"
                       r"|Bad Gateway|Service Unavailable|Gateway Time-?out|Too Many Requests|\bEOF\b|connection (?:reset|refused)|broken pipe|timeout|timed out"
                       r"|TLS handshake|no such host|temporary failure|Sigstore verifier"
                       r"|verifier is not available|initialization may have failed", re.IGNORECASE)
ATTEMPTS, BACKOFF = 4, 5


class Refused(RuntimeError):
    pass


def run(argv, *, check=True):
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=1800)
    except subprocess.TimeoutExpired as exc:
        raise Refused(f"{argv[0]} {argv[1]} did not finish within 30 minutes") from exc
    if check and result.returncode:
        raise Refused(f"{argv[0]} {argv[1]} failed (exit {result.returncode}): {result.stderr.strip()[-400:]}")
    return result


def transient(result):
    return bool(result.returncode and TRANSIENT.search(result.stderr or ""))


def read(argv):
    """A read-only command, retried with backoff while it fails transiently; returns the last result."""
    result = run(argv, check=False)
    for attempt in range(ATTEMPTS - 1):
        if not transient(result):
            break
        time.sleep(BACKOFF * 2 ** attempt)
        result = run(argv, check=False)
    return result


def expected_labels(target, sha):
    return {"org.opencontainers.image.revision": sha, "org.opencontainers.image.source": SOURCE,
            "io.deedkit.production.build": production.build_recipe(target, sha)[1]}


def check_labels(config, target, sha, what):
    labels = (config.get("config") or {}).get("Labels") or {}
    for key, value in expected_labels(target, sha).items():
        if labels.get(key) != value:
            raise Refused(f"{what} for {target} has the wrong {key} label")


def archive_config(path):
    """The config blob of a single-image `docker save` archive: its sha256 digest and parsed content."""
    try:
        with tarfile.open(path) as archive:
            def member(name):
                handle = archive.extractfile(name)
                if handle is None:
                    raise Refused(f"exported archive has no file {name}")
                return handle.read()
            manifest = json.loads(member("manifest.json"))
            if len(manifest) != 1:
                raise Refused("an exported archive must hold exactly one image")
            blob = member(manifest[0]["Config"])
        return "sha256:" + hashlib.sha256(blob).hexdigest(), json.loads(blob)
    except (OSError, tarfile.TarError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise Refused(f"exported archive {path.name} is unreadable ({type(exc).__name__})") from exc


def export(target, sha, directory):
    """Save the image check-image built and tested, with the identity the publish job must find again."""
    directory.mkdir(parents=True, exist_ok=False)
    run(["docker", "save", "--output", str(directory / "image.tar"), f"deedkit-check-{target}:{sha}"])
    config_digest, config = archive_config(directory / "image.tar")
    check_labels(config, target, sha, "tested image")
    record = {"target": target, "sha": sha, "config_digest": config_digest}
    (directory / "image.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


class Registry:
    """skopeo against GHCR, or a local plain-HTTP registry in the pull request dry run."""

    def __init__(self, prefix, *, insecure=False):
        self.prefix, self.tls = prefix, ["--tls-verify=false"] if insecure else []

    def name(self, target):
        return f"{self.prefix}/deedkit-{target}"

    def fetch(self, reference):
        """The raw manifest bytes of a tag or digest reference, or None when it does not exist."""
        return self.inspect(reference)

    def inspect(self, reference):
        result = read(["skopeo", "inspect", *self.tls, "--raw", "docker://" + reference])
        if result.returncode:
            # A failure that is still transient after the retries never proves absence, whatever its body says.
            if not transient(result) and any(marker in result.stderr.lower() for marker in ABSENT):
                return None
            raise Refused(f"cannot read {reference}: {result.stderr.strip()[-400:]}")
        return result.stdout.encode()

    def manifest(self, reference):
        """The raw manifest and its digest, or None when the tag does not exist."""
        raw = self.inspect(reference)
        if raw is None:
            return None
        manifest = json.loads(raw)
        if manifest.get("mediaType") not in MANIFESTS:
            raise Refused(f"{reference} is not a single-image manifest")
        return "sha256:" + hashlib.sha256(raw).hexdigest(), manifest

    def config(self, reference, manifest):
        result = read(["skopeo", "inspect", *self.tls, "--config", "--raw", "docker://" + reference])
        if result.returncode:
            raise Refused(f"cannot read the config of {reference}: {result.stderr.strip()[-400:]}")
        raw = result.stdout.encode()
        if "sha256:" + hashlib.sha256(raw).hexdigest() != manifest["config"]["digest"]:
            raise Refused(f"config of {reference} does not match its manifest")
        return json.loads(raw)

    def push(self, archive, reference):
        run(["skopeo", "copy", "--quiet", *[f"--dest-{flag[2:]}" for flag in self.tls],
             "docker-archive:" + str(archive), "docker://" + reference])

    def put(self, reference, raw, media_type):
        """PUT exactly these manifest bytes under a tag (OCI distribution spec, "Pushing manifests"). Never retried."""
        host, _, path = reference.partition("/")
        repository, _, tag = path.rpartition(":")
        request = urllib.request.Request(f"{'http' if self.tls else 'https'}://{host}/v2/{repository}/manifests/{tag}",
                                         data=raw, method="PUT", headers={"Content-Type": media_type})
        if not self.tls:  # the dry run's localhost registry has no authentication
            request.add_unredirected_header("Authorization", "Bearer " + self.token(host, repository))
        try:
            with urllib.request.urlopen(request, timeout=120):
                pass
        except (OSError, ValueError) as exc:  # urllib.error.URLError and HTTPError are OSErrors
            raise Refused(f"cannot write {reference}: {exc}") from exc

    def token(self, host, repository):
        """A push token for one repository from the registry's token endpoint, for the job's GITHUB_TOKEN."""
        user, secret = os.environ.get("ACTOR", ""), os.environ.get("GH_TOKEN", "")
        if not user or not secret:
            raise Refused("writing a tag needs ACTOR and GH_TOKEN")
        query = urllib.parse.urlencode({"scope": f"repository:{repository}:pull,push", "service": host})
        request = urllib.request.Request(f"https://{host}/token?{query}")
        request.add_unredirected_header("Authorization", "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode())
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                token = json.loads(response.read(65536)).get("token")
        except (OSError, ValueError) as exc:
            raise Refused(f"no push token for {repository}: {exc}") from exc
        if not isinstance(token, str) or not token:
            raise Refused(f"no push token for {repository}")
        return token


def attested(name, digest, sha):
    """Where a provenance attestation from main's ci.yml at this commit is stored for this digest.

    "complete" when GitHub's attestation API and the registry (the bundle `push-to-registry`
    attaches) both verify, "github" when only the API does (the attachment was interrupted),
    "none" otherwise. A network or API failure is retried and, if it persists, refuses: it never reads as "none".
    """
    flags = ["--repo", production.REPOSITORY, "--signer-workflow", WORKFLOW, "--source-ref", "refs/heads/main",
             "--source-digest", sha, "--deny-self-hosted-runners", "--format", "json"]
    subject = f"oci://{name}@{digest}"
    for where, extra, missing in (("GitHub", [], "none"), ("the registry", ["--bundle-from-oci"], "github")):
        result = read(["gh", "attestation", "verify", subject, *extra, *flags])
        stderr = (result.stderr or "").strip()[-400:]
        if transient(result):
            raise Refused(f"the attestation of {subject} in {where} could not be verified: {stderr}")
        if result.returncode:
            print(f"{subject}: no valid attestation in {where}: {stderr}", file=sys.stderr)
            return missing
    return "complete"


def decide(existing, existing_config, attestation, artifact):
    """Write-once decision for one target: push, reuse, attest or refuse; never moves an existing tag."""
    if existing is None:
        if artifact is None:
            raise Refused("the tag is absent and the tested image artifact is missing; rebuild in a new run")
        return "push"
    if attestation == "complete":
        return "reuse"
    if attestation == "github":
        return "attest"  # provenance exists; an interrupted attempt did not attach its bundle to the registry
    if artifact is not None and existing_config == artifact["config_digest"]:
        return "attest"  # an earlier attempt pushed these tested bytes but did not attest them
    raise Refused("the tag exists without a valid attestation and does not hold this run's tested image")


def tested_artifact(directory, target, sha):
    """This attempt's exported archive record, checked against the archive itself, or None when absent."""
    if not (directory / "image.json").exists():
        return None
    artifact = json.loads((directory / "image.json").read_text())
    actual, config = archive_config(directory / "image.tar")
    if (artifact.get("target"), artifact.get("sha"), artifact.get("config_digest")) != (target, sha, actual):
        raise Refused(f"exported image for {target} does not match its record")
    check_labels(config, target, sha, "exported image")
    return artifact


def staging_tag(sha, attempt):
    return f"{sha}-attempt-{attempt}"


def dry_run_verifier(registry, attempt):
    """The pull request dry run has no Sigstore: only the image this dry run staged counts as attested."""
    def verify(name, digest, sha):
        staged = registry.manifest(f"{name}:{staging_tag(sha, attempt)}")
        return "complete" if staged and staged[0] == digest else "none"
    return verify


def publish_one(registry, target, sha, images, verify=attested, attempt=None):
    name = registry.name(target)
    reference = f"{name}:{sha}"
    directory = images / f"image-{target}"
    found = registry.manifest(reference)
    existing_config = attestation = None
    if found:
        digest, manifest = found
        check_labels(registry.config(f"{name}@{digest}", manifest), target, sha, "published image")
        existing_config, attestation = manifest["config"]["digest"], verify(name, digest, sha)
    # An attested tag needs no archive, so a damaged or expired download cannot fail its reuse.
    artifact = None if attestation in {"complete", "github"} else tested_artifact(directory, target, sha)
    action = decide(found, existing_config, attestation, artifact)
    if action == "push" and artifact is not None:  # decide() pushes only with an artifact
        if not attempt:
            raise Refused("a new image needs this run's attempt for its staging tag")
        # The final tag is written only by promote(), after the attestation of this digest verifies.
        staging = f"{name}:{staging_tag(sha, attempt)}"
        registry.push(directory / "image.tar", staging)
        found = registry.manifest(staging)
        if not found or found[1]["config"]["digest"] != artifact["config_digest"]:
            raise Refused(f"pushed {target} manifest does not carry the tested image config")
    if found is None:
        raise Refused(f"{target} has no manifest after publication")
    return {"target": target, "action": action, "digest": found[0], "attest": action in {"push", "attest"}}


def output_key(target):
    return target.replace("-", "_")


def each(function, targets):
    """`function` of every target in parallel threads, results in target order. Every target finishes, as its own
    write-once decision, before the first refusal (in target order) is raised."""
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        futures = [pool.submit(function, target) for target in targets]
    return [future.result() for future in futures]


def publish(registry, sha, images, outputs, verify=attested, targets=TARGETS, attempt=None):
    results = each(lambda target: publish_one(registry, target, sha, images, verify, attempt), targets)
    lines = []
    for result in results:
        key = output_key(result["target"])
        lines += [f"digest_{key}={result['digest']}", f"attest_{key}={str(result['attest']).lower()}"]
    if outputs:
        with open(outputs, "a") as handle:
            handle.write("\n".join(lines) + "\n")
    return results


def promote_one(registry, target, sha, attempt, verify=attested):
    """The final tag for this attempt's staged image, written only after its attestation verifies, as the same bytes."""
    name = registry.name(target)
    staged = registry.manifest(f"{name}:{staging_tag(sha, attempt)}")
    if staged is None:
        return None  # not pushed by this attempt: its existing final tag was reused or attested in place
    digest, final = staged[0], registry.manifest(f"{name}:{sha}")
    if final is not None:
        if final[0] != digest:
            raise Refused(f"{target} final tag holds another image than this attempt staged")
        return digest
    if verify(name, digest, sha) != "complete":
        raise Refused(f"{target} staged image {digest} has no complete attestation; its final tag is not written")
    raw = registry.fetch(f"{name}@{digest}")
    # Any client-side conversion of the manifest would give the final tag another digest: refused before the PUT.
    if raw is None or "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
        raise Refused(f"{target} manifest read by digest is not the verified bytes; its final tag is not written")
    media_type = json.loads(raw).get("mediaType")
    if media_type not in MANIFESTS:
        raise Refused(f"{target} staged image is not a single-image manifest")
    registry.put(f"{name}:{sha}", raw, media_type)
    final = registry.manifest(f"{name}:{sha}")
    if final is None or final[0] != digest:
        raise Refused(f"{target} final tag does not hold the verified digest after promotion")
    return digest


def promote(registry, sha, attempt, verify=attested, targets=TARGETS):
    return dict(zip(targets, each(lambda target: promote_one(registry, target, sha, attempt, verify), targets)))


def confirm(registry, sha, verify=attested, attempt=None):
    """After promotion: every final tag names an image of this commit attested in GitHub and in the registry, and a
    target this attempt staged holds exactly the staged, verified digest."""
    def confirm_one(target):
        found = registry.manifest(f"{registry.name(target)}:{sha}")
        staged = registry.manifest(f"{registry.name(target)}:{staging_tag(sha, attempt)}") if attempt else None
        if found and staged and found[0] != staged[0]:
            raise Refused(f"{target} final tag does not hold the image this attempt staged and verified")
        if not found or verify(registry.name(target), found[0], sha) != "complete":
            raise Refused(f"{target} is not published with a valid attestation")
        return f"{registry.name(target)}@{found[0]}"
    return dict(zip(TARGETS, each(confirm_one, TARGETS)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["export", "publish", "promote", "confirm"])
    parser.add_argument("--target", choices=TARGETS)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--attempt", help="<run_id>-<run_attempt>, naming this attempt's staging tags")
    parser.add_argument("--registry", default=REGISTRY)
    parser.add_argument("--local-dry-run", action="store_true",
                        help="plain-HTTP localhost registry, no Sigstore: publish also promotes (pull request dry run only)")
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
            raise Refused("an exact lowercase 40-character commit SHA is required")
        if args.local_dry_run and not args.registry.startswith("localhost:"):
            raise Refused("the dry run publishes only to a localhost registry")
        if args.action != "export" and not re.fullmatch(r"[0-9]+-[0-9]+", args.attempt or ""):
            raise Refused("--attempt <run_id>-<run_attempt> is required")
        registry = Registry(args.registry, insecure=args.local_dry_run)
        verify = dry_run_verifier(registry, args.attempt) if args.local_dry_run else attested
        if args.action == "export":
            result = export(args.target, args.sha, args.directory)
        elif args.action == "publish":
            targets = (args.target,) if args.target else TARGETS
            # A dry run leaves no step outputs that a later step could mistake for a publication.
            outputs = None if args.local_dry_run else os.environ.get("GITHUB_OUTPUT")
            result = publish(registry, args.sha, args.directory, outputs, verify, targets, args.attempt)
            if args.local_dry_run:  # no attest step runs between them in the dry run
                promote(registry, args.sha, args.attempt, verify, targets)
        elif args.action == "promote":
            result = promote(registry, args.sha, args.attempt, verify, (args.target,) if args.target else TARGETS)
        else:
            result = confirm(registry, args.sha, verify, args.attempt)
        print(json.dumps(result, indent=2))
    except Refused as exc:
        parser.exit(1, "publication refused: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
