# attest-verify

Tokenless attestation verifier for DeedKit images (ANYM-301 B2). Before
`scripts/production.py` pulls an image, this helper checks the GitHub
build-provenance attestation of the exact image digest, anonymously and offline
against a pinned Sigstore trust root.

```sh
attest-verify --image ghcr.io/gosync-inc/deedkit-<name> --sha <40 lowercase hex> [--digest sha256:<64 hex>]
```

- Without `--digest` the tag `<sha>` is resolved to its manifest digest; with
  `--digest` that manifest is fetched by digest instead (proves that another
  commit's image is refused for this `<sha>`).
- Exit 0: one JSON line on stdout,
  `{"reference":"ghcr.io/gosync-inc/deedkit-<name>@sha256:…","digest":"sha256:…","config_digest":"sha256:…","sha":"<sha>"}`.
- Exit 1: nothing on stdout, `attest-verify: refused: <reason>` on stderr.
- Exit 2: usage error.

## What it checks

1. Registry (`registry.go`): the anonymous pull token from
   `https://ghcr.io/token?scope=repository:<repo>:pull&service=ghcr.io`; no
   `GH_TOKEN`, `GITHUB_TOKEN`, `DOCKER_CONFIG`, `~/.docker` or credential helper is
   read. The reference must resolve to exactly one single-platform image manifest
   (an index or manifest list is refused); its digest is sha256 of the body and
   must equal `Docker-Content-Digest` and `--digest` when given. Bundles
   (`application/vnd.dev.sigstore.bundle.v0.3+json`) are found through the
   referrers API, falling back to the `sha256-<hex>` tag; every bundle manifest and
   blob digest is checked, and a bundle manifest `subject` must name the image
   digest. HTTPS only, no proxy, 60 s timeouts, manifests ≤ 4 MiB, bundles ≤ 16 MiB,
   at most 16 bundles; a redirect to another host never carries the bearer token.
   Nothing is written to disk.
2. Verification (`verify.go`, sigstore-go v1.3.0): signature, Fulcio certificate
   chain, one embedded SCT, one Rekor transparency-log entry and signing-time
   validity at the log's observer timestamp, against the embedded public-good
   trust root; the in-toto subject must be the image digest. On that same
   verified result the certificate must carry exactly:
   - SAN `https://github.com/GoSync-Inc/deedkit/.github/workflows/ci.yml@refs/heads/main`
   - issuer `https://token.actions.githubusercontent.com`
   - `SourceRepositoryURI = https://github.com/GoSync-Inc/deedkit`,
     `SourceRepositoryRef = refs/heads/main`, `SourceRepositoryDigest = <sha>`,
     `RunnerEnvironment = github-hosted`, `BuildSignerURI` = the SAN (the real
     bundle of a ci.yml main build carries exactly this value). This is stricter
     than B2's list on purpose: if publication ever moves into a reusable workflow
     (`workflow_call`), the build signer changes and verification fails closed
     until `verify.go`'s expected identity is updated in a reviewed PR.

   and the statement predicate type must be `https://slsa.dev/provenance/v1`.
   Predicate claims are never used in place of certificate extensions. A missing
   or empty extension is refused: `certificate.CompareExtensions` skips only empty
   *expected* values, and the helper refuses to build an identity with one.

## Trusted root

`trusted_root.json` is embedded with `//go:embed`. It was fetched on 2026-10-18
with sigstore-go v1.3.0's TUF client (`tuf.New(tuf.DefaultOptions())` with a
throwaway cache path, then `GetTarget("trusted_root.json")`, which verifies it
against the TUF root embedded in sigstore-go) from a scratch program that is not
committed, and parsed with `root.NewTrustedRootFromJSON`.

trusted_root.json SHA-256: `6494e21ea73fa7ee769f85f57d5a3e6a08725eae1e38c755fc3517c9e6bc0b66`

`verify_test.go` asserts the embedded file matches this checksum. Refresh it the
same way when Sigstore rotates keys, update the checksum here and rebuild. The
fetch program, run from a scratch module that requires the same sigstore-go
version (`go run .`), is:

```go
package main

import (
	"os"

	"github.com/sigstore/sigstore-go/pkg/tuf"
)

func main() {
	cache, _ := os.MkdirTemp("", "tuf")
	client, err := tuf.New(tuf.DefaultOptions().WithCachePath(cache)) // verified against sigstore-go's embedded TUF root
	if err != nil {
		panic(err)
	}
	root, err := client.GetTarget("trusted_root.json")
	if err != nil {
		panic(err)
	}
	if err := os.WriteFile("trusted_root.json", root, 0o644); err != nil {
		panic(err)
	}
}
```

## Reproducible build

The host builds nothing; it runs the binary whose SHA-256 is recorded in
`attest-verify.linux-amd64.sha256`. Build with:

```sh
GOTOOLCHAIN=go1.26.9 CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -buildvcs=false -ldflags=-buildid= -o <out> .
```

`-buildvcs=false` keeps VCS stamping (which changes with every commit) out of the
binary. `tests/test_attest_verify.py` runs `go test ./...` and rebuilds with this
command to compare the SHA-256 with the recorded file.

## Tests

`go vet ./... && go test -race ./...`:

- `verify_test.go`: the real attestation of `deedkit-runtime` at
  `59d11b0224b3e9a346f5540716b71638f78b1853` (`testdata/`) verified offline:
  accepted for its commit and digest, refused for another commit, another digest
  and a wrong identity. Synthetic certificates with the Fulcio extension OIDs
  through `certificate.SummarizeCertificate` and the helper's identity, and an
  end-to-end test trust root (own Fulcio CA and CT log, VirtualSigstore Rekor):
  wrong, missing or empty source digest, source ref and runner environment, a
  self-hosted runner, wrong SAN, issuer, repository, build signer, predicate type,
  digest and an untrusted CA are refused.
- `registry_test.go`: an HTTPS test registry for tag resolution, index,
  digest-mismatch and missing-tag refusals, referrers and fallback-tag discovery,
  bundle subject and blob digest mismatches, and the absence of any credential
  other than the anonymous bearer token even with `GH_TOKEN`/`GITHUB_TOKEN` set.
- `main_test.go`: usage errors (exit 2) and the output and refusal shapes.
