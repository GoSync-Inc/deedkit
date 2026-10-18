package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

const (
	testRepo  = "gosync-inc/deedkit-runtime"
	testImage = "ghcr.io/" + testRepo
	anonToken = "anonymous-pull-token"
	configDig = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
)

type served struct {
	body        []byte
	contentType string
	digest      string // Docker-Content-Digest override; empty: sha256 of body
}

// fakeGHCR is an HTTPS registry speaking the subset of the distribution API the
// helper uses. It records the Authorization header of every request.
type fakeGHCR struct {
	t          *testing.T
	mu         sync.Mutex
	manifests  map[string]served // tag or digest
	referrers  map[string][]byte // image digest -> index; absent: 404
	blobs      map[string][]byte
	blobRedir  string // base URL blobs redirect to
	auth       map[string][]string
	tokenScope string
}

func newFakeGHCR(t *testing.T) *fakeGHCR {
	return &fakeGHCR{t: t, manifests: map[string]served{}, referrers: map[string][]byte{}, blobs: map[string][]byte{}, auth: map[string][]string{}}
}

func (f *fakeGHCR) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	f.auth[r.URL.Path] = append(f.auth[r.URL.Path], r.Header.Get("Authorization"))
	f.mu.Unlock()
	if r.URL.Path == "/token" {
		f.tokenScope = r.URL.Query().Get("scope") + " " + r.URL.Query().Get("service")
		_ = json.NewEncoder(w).Encode(map[string]string{"token": anonToken})
		return
	}
	prefix := "/v2/" + testRepo + "/"
	if !strings.HasPrefix(r.URL.Path, prefix) {
		http.NotFound(w, r)
		return
	}
	kind, ref, _ := strings.Cut(strings.TrimPrefix(r.URL.Path, prefix), "/")
	switch kind {
	case "manifests":
		m, ok := f.manifests[ref]
		if !ok {
			http.NotFound(w, r)
			return
		}
		d := m.digest
		if d == "" {
			d = sha256Digest(m.body)
		}
		w.Header().Set("Content-Type", m.contentType)
		w.Header().Set("Docker-Content-Digest", d)
		_, _ = w.Write(m.body)
	case "referrers":
		body, ok := f.referrers[ref]
		if !ok {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", mediaOCIIndex)
		_, _ = w.Write(body)
	case "blobs":
		if f.blobRedir != "" {
			http.Redirect(w, r, f.blobRedir+"/blob/"+ref, http.StatusTemporaryRedirect)
			return
		}
		f.serveBlob(w, r, ref)
	default:
		http.NotFound(w, r)
	}
}

func (f *fakeGHCR) serveBlob(w http.ResponseWriter, r *http.Request, ref string) {
	b, ok := f.blobs[ref]
	if !ok {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	_, _ = w.Write(b)
}

func mustJSON(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// image publishes a single-platform image manifest under tag and returns its digest.
func (f *fakeGHCR) image(tag string) string {
	body := mustJSON(f.t, map[string]any{"schemaVersion": 2, "mediaType": mediaOCIManifest,
		"config": map[string]any{"mediaType": "application/vnd.oci.image.config.v1+json", "digest": configDig, "size": 10},
		"layers": []any{}})
	d := sha256Digest(body)
	f.manifests[tag] = served{body: body, contentType: mediaOCIManifest}
	f.manifests[d] = served{body: body, contentType: mediaOCIManifest}
	return d
}

// bundle attaches a bundle blob with a manifest whose subject is subject and
// returns the bundle manifest descriptor.
func (f *fakeGHCR) bundle(subject string, blob []byte) map[string]any {
	blobDigest := sha256Digest(blob)
	f.blobs[blobDigest] = blob
	body := mustJSON(f.t, map[string]any{"schemaVersion": 2, "mediaType": mediaOCIManifest, "artifactType": mediaBundle,
		"config":  map[string]any{"mediaType": "application/vnd.oci.empty.v1+json", "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a", "size": 2},
		"layers":  []any{map[string]any{"mediaType": mediaBundle, "digest": blobDigest, "size": len(blob)}},
		"subject": map[string]any{"mediaType": mediaOCIManifest, "digest": subject, "size": 100}})
	d := sha256Digest(body)
	f.manifests[d] = served{body: body, contentType: mediaOCIManifest}
	return map[string]any{"mediaType": mediaOCIManifest, "artifactType": mediaBundle, "digest": d, "size": len(body)}
}

func index(t *testing.T, descs ...map[string]any) []byte {
	return mustJSON(t, map[string]any{"schemaVersion": 2, "mediaType": mediaOCIIndex, "manifests": descs})
}

func (f *fakeGHCR) fallback(imageDigest string, descs ...map[string]any) {
	f.manifests[strings.Replace(imageDigest, ":", "-", 1)] = served{body: index(f.t, descs...), contentType: mediaOCIIndex}
}

func startRegistry(t *testing.T, f *fakeGHCR) *registry {
	t.Helper()
	srv := httptest.NewTLSServer(f)
	t.Cleanup(srv.Close)
	client := srv.Client()
	client.CheckRedirect = checkRedirect
	return newRegistry(srv.URL, client)
}

// recordingCheck accepts exactly the bundle bodies in accept and records its calls.
type recordingCheck struct {
	accept [][]byte
	calls  []string
}

func (c *recordingCheck) check(b []byte, digest, sha string) error {
	c.calls = append(c.calls, digest+" "+sha)
	for _, a := range c.accept {
		if bytes.Equal(a, b) {
			return nil
		}
	}
	return refuse("synthetic check refused %d bytes", len(b))
}

func attestRefused(t *testing.T, reg *registry, check checkFunc, digest, want string) {
	t.Helper()
	_, err := attest(context.Background(), reg, check, testImage, fixtureSHA, digest)
	var r *refusal
	if !errors.As(err, &r) {
		t.Fatalf("got %v, want refusal containing %q", err, want)
	}
	if !strings.Contains(err.Error(), want) {
		t.Fatalf("refusal %q does not contain %q", err, want)
	}
}

func TestTagResolvesToImageDigestAndFallbackBundle(t *testing.T) {
	f := newFakeGHCR(t)
	d := f.image(fixtureSHA)
	blob := []byte(`{"bundle":"good"}`)
	f.fallback(d, f.bundle(d, blob))
	c := &recordingCheck{accept: [][]byte{blob}}
	res, err := attest(context.Background(), startRegistry(t, f), c.check, testImage, fixtureSHA, "")
	if err != nil {
		t.Fatal(err)
	}
	want := result{Reference: testImage + "@" + d, Digest: d, ConfigDigest: configDig, SHA: fixtureSHA}
	if *res != want {
		t.Fatalf("got %+v, want %+v", *res, want)
	}
	if len(c.calls) != 1 || c.calls[0] != d+" "+fixtureSHA {
		t.Fatalf("check calls %v", c.calls)
	}
	if f.tokenScope != "repository:"+testRepo+":pull ghcr.io" {
		t.Fatalf("token scope %q", f.tokenScope)
	}
}

func TestReferrersApiIsUsedBeforeTheFallbackTag(t *testing.T) {
	f := newFakeGHCR(t)
	d := f.image(fixtureSHA)
	good, other := []byte(`{"bundle":"referrers"}`), []byte(`{"bundle":"fallback"}`)
	f.referrers[d] = index(t, f.bundle(d, good))
	f.fallback(d, f.bundle(d, other))
	c := &recordingCheck{accept: [][]byte{good}}
	if _, err := attest(context.Background(), startRegistry(t, f), c.check, testImage, fixtureSHA, ""); err != nil {
		t.Fatal(err)
	}
	if n := len(f.auth["/v2/"+testRepo+"/manifests/"+strings.Replace(d, ":", "-", 1)]); n != 0 {
		t.Fatalf("fallback tag fetched %d times despite referrers", n)
	}
}

func TestEmptyReferrersFallBackToTheTag(t *testing.T) {
	f := newFakeGHCR(t)
	d := f.image(fixtureSHA)
	blob := []byte(`{"bundle":"fallback"}`)
	f.referrers[d] = index(t)
	f.fallback(d, f.bundle(d, blob))
	c := &recordingCheck{accept: [][]byte{blob}}
	if _, err := attest(context.Background(), startRegistry(t, f), c.check, testImage, fixtureSHA, ""); err != nil {
		t.Fatal(err)
	}
}

func TestAnyVerifiedBundleIsEnoughAndFailuresAreReported(t *testing.T) {
	f := newFakeGHCR(t)
	d := f.image(fixtureSHA)
	bad, good := []byte(`{"bundle":"bad"}`), []byte(`{"bundle":"good"}`)
	f.fallback(d, f.bundle(d, bad), f.bundle(d, good))
	reg := startRegistry(t, f)
	if _, err := attest(context.Background(), reg, (&recordingCheck{accept: [][]byte{good}}).check, testImage, fixtureSHA, ""); err != nil {
		t.Fatal(err)
	}
	attestRefused(t, reg, (&recordingCheck{}).check, "", "synthetic check refused")
}

func TestExplicitDigestIsFetchedByDigest(t *testing.T) {
	f := newFakeGHCR(t)
	d := f.image("unrelated-tag")
	blob := []byte(`{"bundle":"x"}`)
	f.fallback(d, f.bundle(d, blob))
	res, err := attest(context.Background(), startRegistry(t, f), (&recordingCheck{accept: [][]byte{blob}}).check, testImage, fixtureSHA, d)
	if err != nil {
		t.Fatal(err)
	}
	if res.Digest != d || len(f.auth["/v2/"+testRepo+"/manifests/"+fixtureSHA]) != 0 {
		t.Fatalf("digest %s, tag requests %v", res.Digest, f.auth)
	}
}

func TestIndexIsRefused(t *testing.T) {
	for _, mt := range []string{mediaOCIIndex, mediaDockerList} {
		f := newFakeGHCR(t)
		f.manifests[fixtureSHA] = served{body: mustJSON(t, map[string]any{"schemaVersion": 2, "mediaType": mt,
			"manifests": []any{map[string]any{"mediaType": mediaOCIManifest, "digest": configDig, "size": 1}}}), contentType: mt}
		attestRefused(t, startRegistry(t, f), (&recordingCheck{}).check, "", "index or manifest list")
	}
}

func TestIndexWithoutMediaTypeIsRefused(t *testing.T) {
	f := newFakeGHCR(t)
	f.manifests[fixtureSHA] = served{body: mustJSON(t, map[string]any{"schemaVersion": 2,
		"manifests": []any{map[string]any{"mediaType": mediaOCIManifest, "digest": configDig, "size": 1}}}), contentType: mediaOCIManifest}
	attestRefused(t, startRegistry(t, f), (&recordingCheck{}).check, "", "index or manifest list")
}

func TestDockerContentDigestMismatchIsRefused(t *testing.T) {
	f := newFakeGHCR(t)
	f.image(fixtureSHA)
	m := f.manifests[fixtureSHA]
	m.digest = otherDigest
	f.manifests[fixtureSHA] = m
	attestRefused(t, startRegistry(t, f), (&recordingCheck{}).check, "", "Docker-Content-Digest")
}

func TestDigestFlagMismatchIsRefused(t *testing.T) {
	f := newFakeGHCR(t)
	d := f.image(fixtureSHA)
	f.manifests[otherDigest] = f.manifests[d] // the registry answers otherDigest with another image's body
	attestRefused(t, startRegistry(t, f), (&recordingCheck{}).check, otherDigest, "body digest is "+d)
}

func TestMissingTagIsRefused(t *testing.T) {
	attestRefused(t, startRegistry(t, newFakeGHCR(t)), (&recordingCheck{}).check, "", "not found")
}

func TestNoAttachedBundleIsRefused(t *testing.T) {
	f := newFakeGHCR(t)
	f.image(fixtureSHA)
	attestRefused(t, startRegistry(t, f), (&recordingCheck{}).check, "", "no Sigstore bundle is attached")
}

func TestBundleManifestSubjectMismatchIsRefused(t *testing.T) {
	f := newFakeGHCR(t)
	d := f.image(fixtureSHA)
	blob := []byte(`{"bundle":"for another image"}`)
	f.fallback(d, f.bundle(otherDigest, blob))
	attestRefused(t, startRegistry(t, f), (&recordingCheck{accept: [][]byte{blob}}).check, "", "subject "+otherDigest+" is not "+d)
}

func TestBundleBlobDigestMismatchIsRefused(t *testing.T) {
	f := newFakeGHCR(t)
	d := f.image(fixtureSHA)
	blob := []byte(`{"bundle":"original"}`)
	desc := f.bundle(d, blob)
	f.blobs[sha256Digest(blob)] = []byte(`{"bundle":"tampered"}`)
	f.fallback(d, desc)
	attestRefused(t, startRegistry(t, f), (&recordingCheck{accept: [][]byte{blob, []byte(`{"bundle":"tampered"}`)}}).check, "", "bundle blob")
}

func TestBundleManifestDigestMismatchIsRefused(t *testing.T) {
	f := newFakeGHCR(t)
	d := f.image(fixtureSHA)
	blob := []byte(`{"bundle":"x"}`)
	desc := f.bundle(d, blob)
	m := f.manifests[desc["digest"].(string)]
	f.manifests[desc["digest"].(string)] = served{body: append(m.body, ' '), contentType: m.contentType}
	f.fallback(d, desc)
	attestRefused(t, startRegistry(t, f), (&recordingCheck{accept: [][]byte{blob}}).check, "", "bundle manifest "+desc["digest"].(string)+": body digest is")
}

func TestOversizedManifestIsRefused(t *testing.T) {
	f := newFakeGHCR(t)
	f.manifests[fixtureSHA] = served{body: bytes.Repeat([]byte(" "), maxManifestBytes+1), contentType: mediaOCIManifest}
	attestRefused(t, startRegistry(t, f), (&recordingCheck{}).check, "", "exceeds")
}

func TestOnlyTheAnonymousBearerIsSentEvenWithGitHubTokensSet(t *testing.T) {
	t.Setenv("GH_TOKEN", "gh-secret")
	t.Setenv("GITHUB_TOKEN", "github-secret")
	t.Setenv("DOCKER_CONFIG", t.TempDir())
	f := newFakeGHCR(t)
	blobHost := newFakeGHCR(t)
	blobSrv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		blobHost.mu.Lock()
		blobHost.auth[r.URL.Path] = append(blobHost.auth[r.URL.Path], r.Header.Get("Authorization"))
		blobHost.mu.Unlock()
		f.serveBlob(w, r, strings.TrimPrefix(r.URL.Path, "/blob/"))
	}))
	t.Cleanup(blobSrv.Close)
	f.blobRedir = blobSrv.URL
	d := f.image(fixtureSHA)
	blob := []byte(`{"bundle":"redirected"}`)
	f.fallback(d, f.bundle(d, blob))
	if _, err := attest(context.Background(), startRegistry(t, f), (&recordingCheck{accept: [][]byte{blob}}).check, testImage, fixtureSHA, ""); err != nil {
		t.Fatal(err)
	}
	for path, headers := range f.auth {
		for _, h := range headers {
			want := "Bearer " + anonToken
			if path == "/token" {
				want = ""
			}
			if h != want {
				t.Fatalf("%s sent Authorization %q, want %q", path, h, want)
			}
		}
	}
	if len(blobHost.auth) == 0 {
		t.Fatal("blob redirect was not followed")
	}
	for path, headers := range blobHost.auth {
		for _, h := range headers {
			if h != "" {
				t.Fatalf("redirected blob %s received Authorization %q", path, h)
			}
		}
	}
}

func TestRedirectToPlainHTTPIsRefused(t *testing.T) {
	plain := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) }))
	t.Cleanup(plain.Close)
	f := newFakeGHCR(t)
	f.blobRedir = plain.URL
	d := f.image(fixtureSHA)
	f.fallback(d, f.bundle(d, []byte(`{"bundle":"x"}`)))
	attestRefused(t, startRegistry(t, f), (&recordingCheck{}).check, "", "non-HTTPS")
}

func TestRegistryRequiresHTTPS(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("plain-HTTP registry base accepted")
		}
	}()
	newRegistry("http://ghcr.io", http.DefaultClient)
}
