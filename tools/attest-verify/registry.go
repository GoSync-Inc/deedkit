package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const (
	mediaOCIManifest    = "application/vnd.oci.image.manifest.v1+json"
	mediaOCIIndex       = "application/vnd.oci.image.index.v1+json"
	mediaDockerManifest = "application/vnd.docker.distribution.manifest.v2+json"
	mediaDockerList     = "application/vnd.docker.distribution.manifest.list.v2+json"
	mediaBundle         = "application/vnd.dev.sigstore.bundle.v0.3+json"

	maxManifestBytes = 4 << 20
	maxBundleBytes   = 16 << 20
	maxTokenBytes    = 64 << 10
	maxBundles       = 16
	maxRedirects     = 5
)

// newHTTPClient is plain net/http: no proxy, no cookie jar, no credential
// helper. Redirects stay on HTTPS and never carry the bearer token to another host.
func newHTTPClient() *http.Client {
	dialer := &net.Dialer{Timeout: 30 * time.Second}
	transport := &http.Transport{
		Proxy:                 nil,
		DialContext:           dialer.DialContext,
		TLSHandshakeTimeout:   30 * time.Second,
		ResponseHeaderTimeout: 60 * time.Second,
		ForceAttemptHTTP2:     true,
	}
	return &http.Client{Transport: transport, Timeout: 60 * time.Second, CheckRedirect: checkRedirect}
}

func checkRedirect(req *http.Request, via []*http.Request) error {
	if len(via) >= maxRedirects {
		return errors.New("too many redirects")
	}
	if req.URL.Scheme != "https" {
		return fmt.Errorf("redirect to non-HTTPS URL %s", req.URL.Redacted())
	}
	if req.URL.Host != via[0].URL.Host {
		req.Header.Del("Authorization")
	}
	return nil
}

type registry struct {
	base   string // https://ghcr.io, or a test server
	client *http.Client
	token  string
}

func newRegistry(base string, client *http.Client) *registry {
	u, err := url.Parse(base)
	if err != nil || u.Scheme != "https" || u.Host == "" {
		panic("registry base must be an https URL")
	}
	return &registry{base: strings.TrimRight(base, "/"), client: client}
}

// authorize obtains the anonymous pull token; no credential is ever sent.
func (r *registry) authorize(ctx context.Context, repo string) error {
	q := url.Values{"scope": {"repository:" + repo + ":pull"}, "service": {registryHost}}
	body, _, status, err := r.get(ctx, r.base+"/token?"+q.Encode(), "application/json", maxTokenBytes, false)
	if err != nil {
		return err
	}
	if status != http.StatusOK {
		return refuse("anonymous registry token for %s: HTTP %d", repo, status)
	}
	var t struct {
		Token       string `json:"token"`
		AccessToken string `json:"access_token"`
	}
	if err := json.Unmarshal(body, &t); err != nil {
		return refuse("anonymous registry token for %s: invalid JSON", repo)
	}
	if t.Token == "" {
		t.Token = t.AccessToken
	}
	if t.Token == "" || strings.ContainsAny(t.Token, " \r\n\t") {
		return refuse("anonymous registry token for %s: empty or malformed", repo)
	}
	r.token = t.Token
	return nil
}

// get performs one bounded GET. auth adds the anonymous bearer token, which the
// redirect policy strips when the request leaves the registry host.
func (r *registry) get(ctx context.Context, rawURL, accept string, limit int64, auth bool) ([]byte, http.Header, int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, nil, 0, err
	}
	req.Header.Set("Accept", accept)
	req.Header.Set("User-Agent", "deedkit-attest-verify")
	if auth {
		req.Header.Set("Authorization", "Bearer "+r.token)
	}
	resp, err := r.client.Do(req)
	if err != nil {
		return nil, nil, 0, refuse("registry request failed: %v", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return nil, nil, 0, refuse("registry read failed: %v", err)
	}
	if int64(len(body)) > limit {
		return nil, nil, 0, refuse("registry response for %s exceeds %d bytes", req.URL.Path, limit)
	}
	return body, resp.Header, resp.StatusCode, nil
}

type descriptor struct {
	MediaType    string `json:"mediaType"`
	ArtifactType string `json:"artifactType"`
	Digest       string `json:"digest"`
	Size         int64  `json:"size"`
}

type manifestDoc struct {
	MediaType    string          `json:"mediaType"`
	ArtifactType string          `json:"artifactType"`
	Config       *descriptor     `json:"config"`
	Layers       []descriptor    `json:"layers"`
	Manifests    json.RawMessage `json:"manifests"`
	Subject      *descriptor     `json:"subject"`
}

type imageManifest struct{ digest, config string }

func sha256Digest(b []byte) string {
	sum := sha256.Sum256(b)
	return "sha256:" + hex.EncodeToString(sum[:])
}

// fetchManifest fetches a manifest by tag or digest and returns its body and
// sha256 digest, refusing a Docker-Content-Digest that disagrees with the body.
func (r *registry) fetchManifest(ctx context.Context, repo, ref, accept string) ([]byte, http.Header, string, error) {
	body, header, status, err := r.get(ctx, r.base+"/v2/"+repo+"/manifests/"+ref, accept, maxManifestBytes, true)
	if err != nil {
		return nil, nil, "", err
	}
	if status == http.StatusNotFound {
		return nil, nil, "", refuse("manifest %s:%s not found", repo, ref)
	}
	if status != http.StatusOK {
		return nil, nil, "", refuse("manifest %s:%s: HTTP %d", repo, ref, status)
	}
	digest := sha256Digest(body)
	if h := header.Get("Docker-Content-Digest"); h != "" && h != digest {
		return nil, nil, "", refuse("manifest %s:%s: Docker-Content-Digest %s differs from body digest %s", repo, ref, h, digest)
	}
	return body, header, digest, nil
}

func contentType(h http.Header) string {
	mt, _, err := mime.ParseMediaType(h.Get("Content-Type"))
	if err != nil {
		return ""
	}
	return mt
}

// imageManifest resolves ref to exactly one single-platform image manifest.
func (r *registry) imageManifest(ctx context.Context, repo, ref, wantDigest string) (*imageManifest, error) {
	accept := strings.Join([]string{mediaOCIManifest, mediaDockerManifest, mediaOCIIndex, mediaDockerList}, ", ")
	body, header, digest, err := r.fetchManifest(ctx, repo, ref, accept)
	if err != nil {
		return nil, err
	}
	if wantDigest != "" && digest != wantDigest {
		return nil, refuse("manifest %s@%s: body digest is %s", repo, wantDigest, digest)
	}
	var m manifestDoc
	if err := json.Unmarshal(body, &m); err != nil {
		return nil, refuse("manifest %s:%s: invalid JSON", repo, ref)
	}
	mt := m.MediaType
	if mt == "" {
		mt = contentType(header)
	}
	if mt == mediaOCIIndex || mt == mediaDockerList || len(m.Manifests) > 0 {
		return nil, refuse("manifest %s:%s is an index or manifest list; one single-platform image manifest is required", repo, ref)
	}
	if mt != mediaOCIManifest && mt != mediaDockerManifest {
		return nil, refuse("manifest %s:%s has unsupported media type %q", repo, ref, mt)
	}
	if m.Config == nil || !digestPattern.MatchString(m.Config.Digest) {
		return nil, refuse("manifest %s:%s has no valid config digest", repo, ref)
	}
	return &imageManifest{digest: digest, config: m.Config.Digest}, nil
}

// bundleBlob is one attached bundle, or the reason it could not be retrieved intact.
type bundleBlob struct {
	body []byte
	err  error
}

// bundles discovers the Sigstore bundles attached to the image digest through the
// referrers API, falling back to the sha256-<hex> tag schema, and retrieves each
// one with every manifest and blob digest checked.
func (r *registry) bundles(ctx context.Context, repo, imageDigest string) ([]bundleBlob, error) {
	descs, err := r.referrers(ctx, repo, imageDigest)
	if err != nil {
		return nil, err
	}
	if len(descs) == 0 {
		if descs, err = r.fallbackReferrers(ctx, repo, imageDigest); err != nil {
			return nil, err
		}
	}
	if len(descs) > maxBundles {
		descs = descs[:maxBundles]
	}
	out := make([]bundleBlob, 0, len(descs))
	for _, d := range descs {
		body, err := r.bundle(ctx, repo, imageDigest, d)
		out = append(out, bundleBlob{body: body, err: err})
	}
	return out, nil
}

func parseIndex(body []byte) ([]descriptor, error) {
	var idx struct {
		Manifests []descriptor `json:"manifests"`
	}
	if err := json.Unmarshal(body, &idx); err != nil {
		return nil, err
	}
	var out []descriptor
	for _, d := range idx.Manifests {
		if d.ArtifactType == mediaBundle || d.ArtifactType == "" {
			out = append(out, d)
		}
	}
	return out, nil
}

// referrers returns nil when the registry has no referrers API or no entries,
// so the caller tries the fallback tag.
func (r *registry) referrers(ctx context.Context, repo, imageDigest string) ([]descriptor, error) {
	u := r.base + "/v2/" + repo + "/referrers/" + imageDigest + "?" + url.Values{"artifactType": {mediaBundle}}.Encode()
	body, header, status, err := r.get(ctx, u, mediaOCIIndex, maxManifestBytes, true)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK || (contentType(header) != "" && contentType(header) != mediaOCIIndex) {
		return nil, nil
	}
	descs, err := parseIndex(body)
	if err != nil {
		return nil, nil
	}
	return descs, nil
}

func (r *registry) fallbackReferrers(ctx context.Context, repo, imageDigest string) ([]descriptor, error) {
	tag := strings.Replace(imageDigest, ":", "-", 1)
	body, _, status, err := r.get(ctx, r.base+"/v2/"+repo+"/manifests/"+tag, mediaOCIIndex, maxManifestBytes, true)
	if err != nil {
		return nil, err
	}
	if status == http.StatusNotFound {
		return nil, nil
	}
	if status != http.StatusOK {
		return nil, refuse("attestation tag %s:%s: HTTP %d", repo, tag, status)
	}
	descs, err := parseIndex(body)
	if err != nil {
		return nil, refuse("attestation tag %s:%s: invalid index", repo, tag)
	}
	return descs, nil
}

func (r *registry) bundle(ctx context.Context, repo, imageDigest string, d descriptor) ([]byte, error) {
	if !digestPattern.MatchString(d.Digest) {
		return nil, refuse("bundle manifest has invalid digest %q", d.Digest)
	}
	body, _, digest, err := r.fetchManifest(ctx, repo, d.Digest, mediaOCIManifest)
	if err != nil {
		return nil, err
	}
	if digest != d.Digest {
		return nil, refuse("bundle manifest %s: body digest is %s", d.Digest, digest)
	}
	var m manifestDoc
	if err := json.Unmarshal(body, &m); err != nil {
		return nil, refuse("bundle manifest %s: invalid JSON", d.Digest)
	}
	if m.Subject != nil && m.Subject.Digest != imageDigest {
		return nil, refuse("bundle manifest %s: subject %s is not %s", d.Digest, m.Subject.Digest, imageDigest)
	}
	var layer *descriptor
	for i := range m.Layers {
		if m.Layers[i].MediaType == mediaBundle {
			layer = &m.Layers[i]
			break
		}
	}
	if layer == nil {
		return nil, refuse("bundle manifest %s: no %s layer", d.Digest, mediaBundle)
	}
	if !digestPattern.MatchString(layer.Digest) {
		return nil, refuse("bundle manifest %s: invalid layer digest %q", d.Digest, layer.Digest)
	}
	if layer.Size > maxBundleBytes {
		return nil, refuse("bundle %s: declared size %d exceeds %d bytes", layer.Digest, layer.Size, maxBundleBytes)
	}
	blob, _, status, err := r.get(ctx, r.base+"/v2/"+repo+"/blobs/"+layer.Digest, mediaBundle+", */*", maxBundleBytes, true)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, refuse("bundle blob %s: HTTP %d", layer.Digest, status)
	}
	if got := sha256Digest(blob); got != layer.Digest {
		return nil, refuse("bundle blob %s: body digest is %s", layer.Digest, got)
	}
	return blob, nil
}
