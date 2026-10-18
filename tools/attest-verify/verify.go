package main

import (
	_ "embed"
	"encoding/hex"
	"fmt"
	"strings"

	"github.com/sigstore/sigstore-go/pkg/bundle"
	"github.com/sigstore/sigstore-go/pkg/fulcio/certificate"
	"github.com/sigstore/sigstore-go/pkg/root"
	"github.com/sigstore/sigstore-go/pkg/verify"
)

const (
	sourceRepository  = "https://github.com/GoSync-Inc/deedkit"
	mainRef           = "refs/heads/main"
	workflowIdentity  = sourceRepository + "/.github/workflows/ci.yml@" + mainRef
	oidcIssuer        = "https://token.actions.githubusercontent.com"
	runnerEnvironment = "github-hosted"
	slsaProvenance    = "https://slsa.dev/provenance/v1"
)

// trustedRootJSON is the public-good Sigstore trusted root, fetched once through
// TUF and pinned by the SHA-256 in README.md; verification never goes online.
//
//go:embed trusted_root.json
var trustedRootJSON []byte

// identity is what a certificate must carry: an exact SAN and issuer (no regular
// expressions) and the Fulcio extensions compared by certificate.CompareExtensions.
type identity struct {
	san, issuer string
	extensions  certificate.Extensions
}

// expectedIdentity is the only identity the CLI accepts: the main-branch CI
// workflow of the DeedKit repository, built from commit sha on a GitHub-hosted runner.
// BuildSignerURI (OID 1.3.6.1.4.1.57264.1.9) is the workflow that signed, which
// for a ci.yml build on main is the SAN itself (checked against the real bundle).
func expectedIdentity(sha string) identity {
	return identity{san: workflowIdentity, issuer: oidcIssuer, extensions: certificate.Extensions{
		SourceRepositoryURI:    sourceRepository,
		SourceRepositoryRef:    mainRef,
		SourceRepositoryDigest: sha,
		RunnerEnvironment:      runnerEnvironment,
		BuildSignerURI:         workflowIdentity,
	}}
}

// certificateIdentity builds the sigstore-go matcher. An empty expected value
// would make CompareExtensions skip the field, so every field is required here;
// a present expected value refuses any different actual value, including an
// empty or missing one.
func (id identity) certificateIdentity() (verify.CertificateIdentity, error) {
	e := id.extensions
	for name, v := range map[string]string{"SAN": id.san, "issuer": id.issuer, "SourceRepositoryURI": e.SourceRepositoryURI,
		"SourceRepositoryRef": e.SourceRepositoryRef, "SourceRepositoryDigest": e.SourceRepositoryDigest,
		"RunnerEnvironment": e.RunnerEnvironment, "BuildSignerURI": e.BuildSignerURI} {
		if v == "" {
			return verify.CertificateIdentity{}, fmt.Errorf("expected %s is empty", name)
		}
	}
	san, err := verify.NewSANMatcher(id.san, "")
	if err != nil {
		return verify.CertificateIdentity{}, err
	}
	issuer, err := verify.NewIssuerMatcher(id.issuer, "")
	if err != nil {
		return verify.CertificateIdentity{}, err
	}
	return verify.NewCertificateIdentity(san, issuer, e)
}

func embeddedRoot() (*root.TrustedRoot, error) { return root.NewTrustedRootFromJSON(trustedRootJSON) }

// embeddedCheck is the CLI's checkFunc: the embedded root and expectedIdentity(sha).
func embeddedCheck(raw []byte, digest, sha string) error {
	tr, err := embeddedRoot()
	if err != nil {
		return refuse("embedded trusted root: %v", err)
	}
	return verifyBundle(tr, raw, digest, expectedIdentity(sha))
}

func verifyBundle(material root.TrustedMaterial, raw []byte, digest string, id identity) error {
	var b bundle.Bundle
	if err := b.UnmarshalJSON(raw); err != nil {
		return refuse("invalid Sigstore bundle: %v", err)
	}
	if b.GetMediaType() != mediaBundle {
		return refuse("bundle media type %q is not %s", b.GetMediaType(), mediaBundle)
	}
	return verifyEntity(material, &b, digest, id)
}

// verifyEntity checks signature, certificate chain, SCT, transparency-log
// inclusion and signing-time validity against material, binds the statement
// subject to digest, and then requires the identity and predicate type on that
// same verified result.
func verifyEntity(material root.TrustedMaterial, entity verify.SignedEntity, digest string, id identity) error {
	hexDigest, ok := strings.CutPrefix(digest, "sha256:")
	if !ok || !digestPattern.MatchString(digest) {
		return refuse("invalid image digest %q", digest)
	}
	want, _ := hex.DecodeString(hexDigest)
	certID, err := id.certificateIdentity()
	if err != nil {
		return refuse("certificate identity: %v", err)
	}
	v, err := verify.NewVerifier(material, verify.WithSignedCertificateTimestamps(1), verify.WithTransparencyLog(1), verify.WithObserverTimestamps(1))
	if err != nil {
		return refuse("verifier: %v", err)
	}
	res, err := v.Verify(entity, verify.NewPolicy(verify.WithArtifactDigest("sha256", want), verify.WithCertificateIdentity(certID)))
	if err != nil {
		return refuse("bundle verification failed: %v", err)
	}
	if res.VerifiedIdentity == nil || res.Signature == nil || res.Signature.Certificate == nil {
		return refuse("bundle verification returned no certificate identity")
	}
	if res.Statement == nil {
		return refuse("bundle carries no in-toto statement")
	}
	if pt := res.Statement.GetPredicateType(); pt != slsaProvenance {
		return refuse("statement predicate type %q is not %s", pt, slsaProvenance)
	}
	bound := false
	for _, s := range res.Statement.GetSubject() {
		if s.GetDigest()["sha256"] == hexDigest {
			bound = true
		}
	}
	if !bound {
		return refuse("statement subject does not name %s", digest)
	}
	return nil
}
