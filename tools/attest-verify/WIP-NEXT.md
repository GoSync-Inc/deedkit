# Story B (ANYM-301): paused work in progress. Delete this file before the PR.

Paused 2026-10-17 by owner decision (usage limit); resume only when the orchestrator says so.
Tiden session 5f2c9b94-6ded-468f-9d5d-c6ef3082c7f7 (intent/2026-10-17-production-preparation-pulls),
promises B1–B9 as approved. Branch from main 6d375b0.

## Done
- Verifier freshness check: cosign v3.1.3 has no RunnerEnvironment, SourceRepositoryRef or
  SourceRepositoryDigest flags (only the legacy github-workflow-* OIDs), so B2 needs the sigstore-go helper.
- go.mod with sigstore-go v1.3.0 (go 1.26.9, the pin of checks.yml/whatsmeow).
- Facts: the repository is public, so the bundles use public-good Sigstore (Rekor v1 dsse 0.0.1 entry, logId
  wNI9atQG…, SCT in the cert, no RFC 3161 timestamp). The predicate is https://slsa.dev/provenance/v1. GHCR
  stores the bundles under the `sha256-<hex>` fallback tag index (artifactType
  application/vnd.dev.sigstore.bundle.v0.3+json).

## Next
1. Helper (`main.go`, `registry.go`, `verify.go`):
   - Registry: anonymous GHCR token; resolve `<sha>` to exactly one single-image manifest (refuse an index,
     refuse when Docker-Content-Digest differs from sha256(body)); return the config digest; find bundles
     through the referrers API, falling back to the `sha256-<hex>` tag; check the digests of every manifest
     and blob.
   - Verification: `verify.NewVerifier(root, WithSignedCertificateTimestamps(1), WithTransparencyLog(1),
     WithObserverTimestamps(1))`. The policy is `WithArtifactDigest(sha256, image)` +
     `NewCertificateIdentity(SAN ci.yml@refs/heads/main, issuer token.actions…, Extensions{SourceRepositoryURI,
     SourceRepositoryRef refs/heads/main, SourceRepositoryDigest sha, RunnerEnvironment github-hosted,
     BuildSignerURI})`. Then require predicateType slsa v1. CompareExtensions refuses empty actual values.
   - Embedded `trusted_root.json`, fetched once with sigstore-go `tuf.New(DefaultOptions).GetTarget`.
   - Output JSON: reference name@digest, digest, config_digest.
2. Tests:
   - A real bundle fixture (repos/GoSync-Inc/deedkit/attestations/sha256:97723b9b…, runtime @ 59d11b0)
     verified offline: accept; another sha and another digest refused.
   - Synthetic certificates with extensions → certificate.SummarizeCertificate → identity refusals (digest,
     ref, runner, missing).
   - httptest GHCR for resolution and discovery.
3. Reproducible build: `CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags=-buildid=`. Record
   the SHA-256 in the repository. A pytest test runs `go test` and rebuilds to compare the hash (Go is set up
   before Python tests in checks.yml).
4. production.py:
   - PUBLISHED targets; verifier path and SHA-256 check; B1 pull + tag; reuse when the image Id equals the
     config digest (classic store) or the manifest digest (containerd store).
   - B3 `publish` in REQUIRED_JOBS (keep #87's skipped-verify exception); B5 receipts; B6 rollback; B7
     `images`.
5. Then: docs (B8), tests, the PR (owner level, Astra + Fable). B9 needs the packages public (owner cutover,
   ~/deedkit-orchestrator/research/ghcr-cutover-commands.md).
