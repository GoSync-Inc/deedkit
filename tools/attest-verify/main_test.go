package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func runCLI(t *testing.T, reg *registry, check checkFunc, args ...string) (int, string, string) {
	t.Helper()
	var out, errOut bytes.Buffer
	code := run(args, &out, &errOut, reg, check)
	return code, out.String(), errOut.String()
}

func TestUsageErrorsExitTwoWithoutOutput(t *testing.T) {
	never := func([]byte, string, string) error { t.Fatal("verification ran on a usage error"); return nil }
	reg := newRegistry("https://ghcr.invalid", nil)
	good := []string{"--image", testImage, "--sha", fixtureSHA}
	cases := map[string][]string{
		"no-arguments":     {},
		"missing-sha":      {"--image", testImage},
		"missing-image":    {"--sha", fixtureSHA},
		"uppercase-image":  {"--image", "ghcr.io/GoSync-Inc/deedkit-runtime", "--sha", fixtureSHA},
		"other-registry":   {"--image", "docker.io/gosync-inc/deedkit-runtime", "--sha", fixtureSHA},
		"other-repository": {"--image", "ghcr.io/gosync-inc/other-runtime", "--sha", fixtureSHA},
		"empty-name":       {"--image", "ghcr.io/gosync-inc/deedkit-", "--sha", fixtureSHA},
		"tagged-image":     {"--image", testImage + ":latest", "--sha", fixtureSHA},
		"short-sha":        {"--image", testImage, "--sha", fixtureSHA[:7]},
		"uppercase-sha":    {"--image", testImage, "--sha", strings.ToUpper(fixtureSHA)},
		"bad-digest":       append(append([]string{}, good...), "--digest", "sha256:abc"),
		"sha512-digest":    append(append([]string{}, good...), "--digest", "sha512:"+strings.Repeat("a", 64)),
		"unknown-flag":     append(append([]string{}, good...), "--token", "x"),
		"extra-argument":   append(append([]string{}, good...), "extra"),
	}
	for name, args := range cases {
		t.Run(name, func(t *testing.T) {
			code, out, _ := runCLI(t, reg, never, args...)
			if code != 2 || out != "" {
				t.Fatalf("exit %d stdout %q, want 2 and nothing", code, out)
			}
		})
	}
}

func TestSuccessPrintsExactlyOneJSONLine(t *testing.T) {
	f := newFakeGHCR(t)
	d := f.image(fixtureSHA)
	blob := []byte(`{"bundle":"good"}`)
	f.fallback(d, f.bundle(d, blob))
	code, out, errOut := runCLI(t, startRegistry(t, f), (&recordingCheck{accept: [][]byte{blob}}).check, "--image", testImage, "--sha", fixtureSHA)
	if code != 0 || errOut != "" {
		t.Fatalf("exit %d stderr %q", code, errOut)
	}
	want := `{"reference":"` + testImage + "@" + d + `","digest":"` + d + `","config_digest":"` + configDig + `","sha":"` + fixtureSHA + `"}` + "\n"
	if out != want {
		t.Fatalf("stdout %q, want %q", out, want)
	}
	dec := json.NewDecoder(strings.NewReader(out))
	dec.DisallowUnknownFields()
	var r result
	if err := dec.Decode(&r); err != nil || r.SHA != fixtureSHA {
		t.Fatalf("decode %v %+v", err, r)
	}
}

func TestRefusalExitsOneWithOneStderrLine(t *testing.T) {
	f := newFakeGHCR(t)
	d := f.image(fixtureSHA)
	f.fallback(d, f.bundle(d, []byte(`{"bundle":"bad"}`)))
	code, out, errOut := runCLI(t, startRegistry(t, f), (&recordingCheck{}).check, "--image", testImage, "--sha", fixtureSHA, "--digest", d)
	if code != 1 || out != "" {
		t.Fatalf("exit %d stdout %q", code, out)
	}
	if !strings.HasPrefix(errOut, "attest-verify: refused: ") || strings.Count(errOut, "\n") != 1 || !strings.HasSuffix(errOut, "\n") {
		t.Fatalf("stderr %q", errOut)
	}
}

func TestRealCheckRefusesASyntheticBundleThroughTheCLI(t *testing.T) {
	f := newFakeGHCR(t)
	d := f.image(fixtureSHA)
	f.fallback(d, f.bundle(d, fixtureBundle(t)))
	code, out, errOut := runCLI(t, startRegistry(t, f), embeddedCheck, "--image", testImage, "--sha", fixtureSHA)
	if code != 1 || out != "" || !strings.Contains(errOut, "artifact digest does not match") {
		t.Fatalf("exit %d stdout %q stderr %q", code, out, errOut)
	}
}
