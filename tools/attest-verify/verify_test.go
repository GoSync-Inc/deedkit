package main

import (
	"context"
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/asn1"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"math/big"
	"net/url"
	"os"
	"regexp"
	"strings"
	"testing"
	"time"

	ct "github.com/google/certificate-transparency-go"
	cttls "github.com/google/certificate-transparency-go/tls"
	ctx509 "github.com/google/certificate-transparency-go/x509"
	ctx509util "github.com/google/certificate-transparency-go/x509util"
	"github.com/secure-systems-lab/go-securesystemslib/dsse"
	"github.com/sigstore/sigstore-go/pkg/bundle"
	"github.com/sigstore/sigstore-go/pkg/fulcio/certificate"
	"github.com/sigstore/sigstore-go/pkg/root"
	"github.com/sigstore/sigstore-go/pkg/testing/ca"
	"github.com/sigstore/sigstore-go/pkg/tlog"
	"github.com/sigstore/sigstore-go/pkg/verify"
	"github.com/sigstore/sigstore/pkg/signature"
	sigdsse "github.com/sigstore/sigstore/pkg/signature/dsse"
)

// The real GitHub attestation of ghcr.io/gosync-inc/deedkit-runtime built by
// ci.yml on main at fixtureSHA; verified offline against the embedded root.
const (
	fixtureSHA    = "59d11b0224b3e9a346f5540716b71638f78b1853"
	fixtureDigest = "sha256:97723b9bd5860e2d6bf268a1dbbd7972b8d02c301b60f576500e073505ff5a51"
	otherSHA      = "0123456789abcdef0123456789abcdef01234567"
	otherDigest   = "sha256:0000000000000000000000000000000000000000000000000000000000000001"
)

func fixtureBundle(t *testing.T) []byte {
	t.Helper()
	b, err := os.ReadFile("testdata/runtime-59d11b0.sigstore.json")
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func mustRefuse(t *testing.T, err error, want string) {
	t.Helper()
	if err == nil {
		t.Fatalf("accepted; want refusal containing %q", want)
	}
	if _, ok := err.(*refusal); !ok {
		t.Fatalf("error %T %v is not a refusal", err, err)
	}
	if !strings.Contains(err.Error(), want) {
		t.Fatalf("refusal %q does not contain %q", err, want)
	}
}

func TestEmbeddedTrustedRootMatchesRecordedChecksumAndParses(t *testing.T) {
	readme, err := os.ReadFile("README.md")
	if err != nil {
		t.Fatal(err)
	}
	m := regexp.MustCompile("trusted_root\\.json SHA-256: `([0-9a-f]{64})`").FindSubmatch(readme)
	if m == nil {
		t.Fatal("README.md records no trusted_root.json SHA-256")
	}
	sum := sha256.Sum256(trustedRootJSON)
	if got := hex.EncodeToString(sum[:]); got != string(m[1]) {
		t.Fatalf("embedded trusted_root.json SHA-256 %s, README records %s", got, m[1])
	}
	tr, err := root.NewTrustedRootFromJSON(trustedRootJSON)
	if err != nil {
		t.Fatal(err)
	}
	if len(tr.FulcioCertificateAuthorities()) == 0 || len(tr.RekorLogs()) == 0 || len(tr.CTLogs()) == 0 {
		t.Fatal("embedded root lacks Fulcio, Rekor or CT material")
	}
}

func TestRealBundleIsAcceptedForItsCommitAndDigest(t *testing.T) {
	if err := embeddedCheck(fixtureBundle(t), fixtureDigest, fixtureSHA); err != nil {
		t.Fatal(err)
	}
}

func TestRealBundleCarriesTheExpectedBuildSignerURI(t *testing.T) {
	var b bundle.Bundle
	if err := b.UnmarshalJSON(fixtureBundle(t)); err != nil {
		t.Fatal(err)
	}
	vc, err := b.VerificationContent()
	if err != nil {
		t.Fatal(err)
	}
	s, err := certificate.SummarizeCertificate(vc.Certificate())
	if err != nil {
		t.Fatal(err)
	}
	if s.BuildSignerURI != workflowIdentity || s.SubjectAlternativeName != workflowIdentity || s.RunnerEnvironment != runnerEnvironment {
		t.Fatalf("unexpected certificate summary %+v", s)
	}
}

func TestRealBundleIsRefusedForAnotherCommit(t *testing.T) {
	mustRefuse(t, embeddedCheck(fixtureBundle(t), fixtureDigest, otherSHA), "SourceRepositoryDigest")
}

func TestRealBundleIsRefusedForAnotherDigest(t *testing.T) {
	mustRefuse(t, embeddedCheck(fixtureBundle(t), otherDigest, fixtureSHA), "artifact digest does not match")
}

func TestRealBundleIsRefusedWithAWrongIdentity(t *testing.T) {
	tr, err := embeddedRoot()
	if err != nil {
		t.Fatal(err)
	}
	cases := map[string]func(*identity){
		"other-workflow": func(id *identity) {
			id.san = sourceRepository + "/.github/workflows/release.yml@" + mainRef
			id.extensions.BuildSignerURI = id.san
		},
		"other-ref": func(id *identity) {
			id.san = sourceRepository + "/.github/workflows/ci.yml@refs/heads/dev"
			id.extensions.BuildSignerURI = id.san
			id.extensions.SourceRepositoryRef = "refs/heads/dev"
		},
		"other-repository": func(id *identity) { id.extensions.SourceRepositoryURI = "https://github.com/GoSync-Inc/other" },
		"other-issuer":     func(id *identity) { id.issuer = "https://accounts.google.com" },
		"self-hosted":      func(id *identity) { id.extensions.RunnerEnvironment = "self-hosted" },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			id := expectedIdentity(fixtureSHA)
			mutate(&id)
			mustRefuse(t, verifyBundle(tr, fixtureBundle(t), fixtureDigest, id), "failed to verify certificate identity")
		})
	}
}

func TestEmptyExpectedValuesAreRejectedBeforeVerification(t *testing.T) {
	id := expectedIdentity(fixtureSHA)
	id.extensions.SourceRepositoryDigest = ""
	if _, err := id.certificateIdentity(); err == nil {
		t.Fatal("an empty expected SourceRepositoryDigest would make CompareExtensions skip the check")
	}
}

func TestMalformedBundleIsRefused(t *testing.T) {
	mustRefuse(t, embeddedCheck([]byte(`{"mediaType":"x"}`), fixtureDigest, fixtureSHA), "invalid Sigstore bundle")
}

// Fulcio extension OIDs 1.3.6.1.4.1.57264.1.<n>; n >= 8 are DER UTF8String (v2).
const (
	oidIssuer              = 8
	oidBuildSignerURI      = 9
	oidRunnerEnvironment   = 11
	oidSourceRepositoryURI = 12
	oidSourceRepositoryDig = 13
	oidSourceRepositoryRef = 14
)

func goodFields(sha string) map[int]string {
	return map[int]string{oidIssuer: oidcIssuer, oidBuildSignerURI: workflowIdentity, oidRunnerEnvironment: runnerEnvironment,
		oidSourceRepositoryURI: sourceRepository, oidSourceRepositoryDig: sha, oidSourceRepositoryRef: mainRef}
}

func fulcioExtensions(t *testing.T, fields map[int]string) []pkix.Extension {
	t.Helper()
	var out []pkix.Extension
	for n := 1; n <= 24; n++ {
		v, ok := fields[n]
		if !ok {
			continue
		}
		der, err := asn1.MarshalWithParams(v, "utf8")
		if err != nil {
			t.Fatal(err)
		}
		out = append(out, pkix.Extension{Id: asn1.ObjectIdentifier{1, 3, 6, 1, 4, 1, 57264, 1, n}, Value: der})
	}
	return out
}

type certCase struct {
	name, san string
	mutate    func(map[int]string)
	want      string // empty: accepted
}

func certCases() []certCase {
	return []certCase{
		{name: "correct"},
		{name: "wrong-source-digest", mutate: func(f map[int]string) { f[oidSourceRepositoryDig] = otherSHA }, want: "SourceRepositoryDigest"},
		{name: "missing-source-digest", mutate: func(f map[int]string) { delete(f, oidSourceRepositoryDig) }, want: "SourceRepositoryDigest"},
		{name: "empty-source-digest", mutate: func(f map[int]string) { f[oidSourceRepositoryDig] = "" }, want: "SourceRepositoryDigest"},
		{name: "wrong-source-ref", mutate: func(f map[int]string) { f[oidSourceRepositoryRef] = "refs/heads/dev" }, want: "SourceRepositoryRef"},
		{name: "missing-source-ref", mutate: func(f map[int]string) { delete(f, oidSourceRepositoryRef) }, want: "SourceRepositoryRef"},
		{name: "empty-source-ref", mutate: func(f map[int]string) { f[oidSourceRepositoryRef] = "" }, want: "SourceRepositoryRef"},
		{name: "self-hosted-runner", mutate: func(f map[int]string) { f[oidRunnerEnvironment] = "self-hosted" }, want: "RunnerEnvironment"},
		{name: "missing-runner", mutate: func(f map[int]string) { delete(f, oidRunnerEnvironment) }, want: "RunnerEnvironment"},
		{name: "empty-runner", mutate: func(f map[int]string) { f[oidRunnerEnvironment] = "" }, want: "RunnerEnvironment"},
		{name: "wrong-repository", mutate: func(f map[int]string) { f[oidSourceRepositoryURI] = "https://github.com/GoSync-Inc/other" }, want: "SourceRepositoryURI"},
		{name: "wrong-build-signer", mutate: func(f map[int]string) {
			f[oidBuildSignerURI] = sourceRepository + "/.github/workflows/release.yml@" + mainRef
		}, want: "BuildSignerURI"},
		{name: "wrong-san", san: sourceRepository + "/.github/workflows/release.yml@" + mainRef, want: "SAN"},
		{name: "wrong-issuer", mutate: func(f map[int]string) { f[oidIssuer] = "https://accounts.google.com" }, want: "issuer"},
	}
}

func selfSignedLeaf(t *testing.T, san string, ext []pkix.Extension) *x509.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	u, err := url.Parse(san)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{SerialNumber: big.NewInt(1), URIs: []*url.URL{u}, NotBefore: time.Now().Add(-time.Minute),
		NotAfter: time.Now().Add(10 * time.Minute), ExtraExtensions: ext}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return cert
}

// Synthetic certificates go through the same SummarizeCertificate and
// CertificateIdentity that the verifier applies to a verified leaf.
func TestSyntheticCertificatesAgainstTheHelperIdentity(t *testing.T) {
	certID, err := expectedIdentity(fixtureSHA).certificateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range certCases() {
		t.Run(c.name, func(t *testing.T) {
			fields, san := goodFields(fixtureSHA), workflowIdentity
			if c.mutate != nil {
				c.mutate(fields)
			}
			if c.san != "" {
				san = c.san
			}
			summary, err := certificate.SummarizeCertificate(selfSignedLeaf(t, san, fulcioExtensions(t, fields)))
			if err != nil {
				t.Fatal(err)
			}
			err = certID.Verify(summary)
			if c.want == "" {
				if err != nil {
					t.Fatal(err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("got %v, want refusal mentioning %s", err, c.want)
			}
		})
	}
}

// testSigstore is an end-to-end test trust root. VirtualSigstore issues leaf
// certificates only with an e-mail SAN and the issuer extension, and keeps its
// Fulcio and CT keys private, so the test supplies its own Fulcio CA (built with
// ca.GenerateRootCa / GenerateFulcioIntermediate) and CT log, embeds a real SCT,
// and reuses VirtualSigstore for the Rekor entry, inclusion proof and SET.
type testSigstore struct {
	*ca.VirtualSigstore
	fulcio    []root.CertificateAuthority
	ctlogs    map[string]*root.TransparencyLog
	interCert *x509.Certificate
	interKey  *ecdsa.PrivateKey
	ctKey     *ecdsa.PrivateKey
	ctLogID   [32]byte
}

func (s *testSigstore) FulcioCertificateAuthorities() []root.CertificateAuthority { return s.fulcio }
func (s *testSigstore) CTLogs() map[string]*root.TransparencyLog                  { return s.ctlogs }

func newTestSigstore(t *testing.T) *testSigstore {
	t.Helper()
	vs, err := ca.NewVirtualSigstore()
	if err != nil {
		t.Fatal(err)
	}
	rootCert, rootKey, err := ca.GenerateRootCa()
	if err != nil {
		t.Fatal(err)
	}
	interCert, interKey, err := ca.GenerateFulcioIntermediate(rootCert, rootKey)
	if err != nil {
		t.Fatal(err)
	}
	ctKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	pub, err := x509.MarshalPKIXPublicKey(&ctKey.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	logID := sha256.Sum256(pub)
	now := time.Now()
	return &testSigstore{VirtualSigstore: vs, interCert: interCert, interKey: interKey, ctKey: ctKey, ctLogID: logID,
		fulcio: []root.CertificateAuthority{&root.FulcioCertificateAuthority{Root: rootCert, Intermediates: []*x509.Certificate{interCert},
			ValidityPeriodStart: now.Add(-time.Hour), ValidityPeriodEnd: now.Add(time.Hour)}},
		ctlogs: map[string]*root.TransparencyLog{hex.EncodeToString(logID[:]): {BaseURL: "https://ctfe.localhost", ID: logID[:],
			HashFunc: crypto.SHA256, PublicKey: &ctKey.PublicKey, ValidityPeriodStart: now.Add(-time.Hour), ValidityPeriodEnd: now.Add(time.Hour)}}}
}

// issue creates a leaf under the test intermediate with an embedded SCT over the
// precertificate (the same TBS without the SCT list).
func (s *testSigstore) issue(t *testing.T, san string, ext []pkix.Extension) (*x509.Certificate, *ecdsa.PrivateKey) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	u, err := url.Parse(san)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{SerialNumber: big.NewInt(time.Now().UnixNano()), URIs: []*url.URL{u},
		NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(10 * time.Minute),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageCodeSigning}, ExtraExtensions: ext}
	create := func() *x509.Certificate {
		der, err := x509.CreateCertificate(rand.Reader, tmpl, s.interCert, &key.PublicKey, s.interKey)
		if err != nil {
			t.Fatal(err)
		}
		c, err := x509.ParseCertificate(der)
		if err != nil {
			t.Fatal(err)
		}
		return c
	}
	pre := create()
	sct := ct.SignedCertificateTimestamp{SCTVersion: ct.V1, LogID: ct.LogID{KeyID: s.ctLogID}, Timestamp: uint64(time.Now().UnixMilli())}
	entry := ct.LogEntry{Leaf: ct.MerkleTreeLeaf{Version: ct.V1, LeafType: ct.TimestampedEntryLeafType, TimestampedEntry: &ct.TimestampedEntry{
		Timestamp: sct.Timestamp, EntryType: ct.PrecertLogEntryType,
		PrecertEntry: &ct.PreCert{IssuerKeyHash: sha256.Sum256(s.interCert.RawSubjectPublicKeyInfo), TBSCertificate: pre.RawTBSCertificate}}}}
	input, err := ct.SerializeSCTSignatureInput(sct, entry)
	if err != nil {
		t.Fatal(err)
	}
	h := sha256.Sum256(input)
	sig, err := ecdsa.SignASN1(rand.Reader, s.ctKey, h[:])
	if err != nil {
		t.Fatal(err)
	}
	sct.Signature = ct.DigitallySigned{Algorithm: cttls.SignatureAndHashAlgorithm{Hash: cttls.SHA256, Signature: cttls.ECDSA}, Signature: sig}
	list, err := ctx509util.MarshalSCTsIntoSCTList([]*ct.SignedCertificateTimestamp{&sct})
	if err != nil {
		t.Fatal(err)
	}
	raw, err := cttls.Marshal(*list)
	if err != nil {
		t.Fatal(err)
	}
	value, err := asn1.Marshal(raw)
	if err != nil {
		t.Fatal(err)
	}
	tmpl.ExtraExtensions = append(append([]pkix.Extension{}, ext...), pkix.Extension{Id: asn1.ObjectIdentifier(ctx509.OIDExtensionCTSCT), Value: value})
	return create(), key
}

type testEntity struct {
	verify.BaseSignedEntity
	cert    *x509.Certificate
	env     *dsse.Envelope
	entries []*tlog.Entry
}

func (e *testEntity) HasInclusionPromise() bool { return true }
func (e *testEntity) HasInclusionProof() bool   { return true }
func (e *testEntity) VerificationContent() (verify.VerificationContent, error) {
	return bundle.NewCertificate(e.cert), nil
}
func (e *testEntity) SignatureContent() (verify.SignatureContent, error) {
	return &bundle.Envelope{Envelope: e.env}, nil
}
func (e *testEntity) Timestamps() ([][]byte, error)       { return nil, nil }
func (e *testEntity) TlogEntries() ([]*tlog.Entry, error) { return e.entries, nil }
func (e *testEntity) Version() (string, error)            { return "v0.3", nil }

func statement(t *testing.T, digest, predicateType string) []byte {
	t.Helper()
	b, err := json.Marshal(map[string]any{"_type": "https://in-toto.io/Statement/v1", "predicateType": predicateType,
		"subject":   []map[string]any{{"name": "ghcr.io/gosync-inc/deedkit-runtime", "digest": map[string]string{"sha256": strings.TrimPrefix(digest, "sha256:")}}},
		"predicate": map[string]any{"buildDefinition": map[string]any{"buildType": "https://actions.github.io/buildtypes/workflow/v1"}}})
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// attest signs payload as a DSSE envelope with a leaf from issuer and logs it in s's Rekor.
func (s *testSigstore) attest(t *testing.T, issuer *testSigstore, san string, ext []pkix.Extension, payload []byte) *testEntity {
	t.Helper()
	leaf, key := issuer.issue(t, san, ext)
	signer, err := signature.LoadECDSASignerVerifier(key, crypto.SHA256)
	if err != nil {
		t.Fatal(err)
	}
	es, err := dsse.NewEnvelopeSigner(&sigdsse.SignerAdapter{SignatureSigner: signer, Pub: &key.PublicKey})
	if err != nil {
		t.Fatal(err)
	}
	env, err := es.SignPayload(context.Background(), "application/vnd.in-toto+json", payload)
	if err != nil {
		t.Fatal(err)
	}
	sig, err := base64.StdEncoding.DecodeString(env.Signatures[0].Sig)
	if err != nil {
		t.Fatal(err)
	}
	entry, err := s.GenerateTlogEntry(leaf, env, sig, time.Now().Unix(), true)
	if err != nil {
		t.Fatal(err)
	}
	return &testEntity{cert: leaf, env: env, entries: []*tlog.Entry{entry}}
}

const e2eDigest = "sha256:1111111111111111111111111111111111111111111111111111111111111111"

func TestEndToEndVerifiedBundleWithTestTrustRoot(t *testing.T) {
	s := newTestSigstore(t)
	for _, c := range certCases() {
		t.Run(c.name, func(t *testing.T) {
			fields, san := goodFields(fixtureSHA), workflowIdentity
			if c.mutate != nil {
				c.mutate(fields)
			}
			if c.san != "" {
				san = c.san
			}
			entity := s.attest(t, s, san, fulcioExtensions(t, fields), statement(t, e2eDigest, slsaProvenance))
			err := verifyEntity(s, entity, e2eDigest, expectedIdentity(fixtureSHA))
			if c.want == "" {
				if err != nil {
					t.Fatal(err)
				}
				return
			}
			mustRefuse(t, err, c.want)
		})
	}
}

func TestEndToEndRefusesOtherDigestPredicateAndUntrustedCA(t *testing.T) {
	s, stranger := newTestSigstore(t), newTestSigstore(t)
	ext := fulcioExtensions(t, goodFields(fixtureSHA))
	good := s.attest(t, s, workflowIdentity, ext, statement(t, e2eDigest, slsaProvenance))
	mustRefuse(t, verifyEntity(s, good, otherDigest, expectedIdentity(fixtureSHA)), "artifact digest does not match")
	mustRefuse(t, verifyEntity(s, good, e2eDigest, expectedIdentity(otherSHA)), "SourceRepositoryDigest")
	v02 := s.attest(t, s, workflowIdentity, ext, statement(t, e2eDigest, "https://slsa.dev/provenance/v0.2"))
	mustRefuse(t, verifyEntity(s, v02, e2eDigest, expectedIdentity(fixtureSHA)), "predicate type")
	foreign := s.attest(t, stranger, workflowIdentity, ext, statement(t, e2eDigest, slsaProvenance))
	mustRefuse(t, verifyEntity(s, foreign, e2eDigest, expectedIdentity(fixtureSHA)), "leaf certificate verification failed")
}
