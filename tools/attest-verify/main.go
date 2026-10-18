// attest-verify checks, without any GitHub credential, that a published DeedKit
// image carries a GitHub build-provenance attestation signed by the main-branch
// CI workflow for one exact commit. It prints the verified image reference only
// after every check passes; scripts/production.py pulls nothing before that.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"regexp"
	"strings"
	"time"
)

const (
	registryHost = "ghcr.io"
	imagePrefix  = registryHost + "/"
	runTimeout   = 3 * time.Minute
)

var (
	imagePattern  = regexp.MustCompile(`^ghcr\.io/gosync-inc/deedkit-[a-z][a-z-]*$`)
	shaPattern    = regexp.MustCompile(`^[0-9a-f]{40}$`)
	digestPattern = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
)

// refusal is a verification failure: exit 1, nothing on stdout.
type refusal struct{ reason string }

func (r *refusal) Error() string { return r.reason }

func refuse(format string, args ...any) error { return &refusal{fmt.Sprintf(format, args...)} }

// checkFunc verifies one serialized Sigstore bundle for the image digest and commit.
type checkFunc func(bundle []byte, digest, sha string) error

type result struct {
	Reference    string `json:"reference"`
	Digest       string `json:"digest"`
	ConfigDigest string `json:"config_digest"`
	SHA          string `json:"sha"`
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr, newRegistry("https://"+registryHost, newHTTPClient()), embeddedCheck))
}

func run(args []string, stdout, stderr io.Writer, reg *registry, check checkFunc) int {
	flags := flag.NewFlagSet("attest-verify", flag.ContinueOnError)
	flags.SetOutput(stderr)
	image := flags.String("image", "", "image name ghcr.io/gosync-inc/deedkit-<name>")
	sha := flags.String("sha", "", "40-character lowercase main commit")
	digest := flags.String("digest", "", "optional manifest digest sha256:<64 hex> to verify instead of the <sha> tag")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	usage := func(msg string) int { fmt.Fprintf(stderr, "attest-verify: usage: %s\n", msg); return 2 }
	switch {
	case flags.NArg() != 0:
		return usage("unexpected arguments")
	case !imagePattern.MatchString(*image):
		return usage("--image must be ghcr.io/gosync-inc/deedkit-<name>")
	case !shaPattern.MatchString(*sha):
		return usage("--sha must be 40 lowercase hex characters")
	case *digest != "" && !digestPattern.MatchString(*digest):
		return usage("--digest must be sha256:<64 lowercase hex>")
	}
	ctx, cancel := context.WithTimeout(context.Background(), runTimeout)
	defer cancel()
	out, err := attest(ctx, reg, check, *image, *sha, *digest)
	if err != nil {
		var r *refusal
		if !errors.As(err, &r) {
			err = refuse("%v", err)
		}
		fmt.Fprintf(stderr, "attest-verify: refused: %s\n", oneLine(err.Error()))
		return 1
	}
	line, _ := json.Marshal(out)
	fmt.Fprintf(stdout, "%s\n", line)
	return 0
}

func attest(ctx context.Context, reg *registry, check checkFunc, image, sha, digest string) (*result, error) {
	repo := strings.TrimPrefix(image, imagePrefix)
	if err := reg.authorize(ctx, repo); err != nil {
		return nil, err
	}
	ref := sha
	if digest != "" {
		ref = digest
	}
	m, err := reg.imageManifest(ctx, repo, ref, digest)
	if err != nil {
		return nil, err
	}
	bundles, err := reg.bundles(ctx, repo, m.digest)
	if err != nil {
		return nil, err
	}
	var reasons []string
	for _, b := range bundles {
		if b.err != nil {
			reasons = append(reasons, b.err.Error())
			continue
		}
		if err := check(b.body, m.digest, sha); err != nil {
			reasons = append(reasons, err.Error())
			continue
		}
		return &result{Reference: image + "@" + m.digest, Digest: m.digest, ConfigDigest: m.config, SHA: sha}, nil
	}
	if len(reasons) == 0 {
		return nil, refuse("no Sigstore bundle is attached to %s@%s", image, m.digest)
	}
	return nil, refuse("no attached bundle verified for %s@%s: %s", image, m.digest, strings.Join(reasons, "; "))
}

func oneLine(s string) string { return strings.Join(strings.Fields(s), " ") }
