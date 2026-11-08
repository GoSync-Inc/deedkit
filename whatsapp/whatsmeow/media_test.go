package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"go.mau.fi/whatsmeow/util/cbcutil"
	"go.mau.fi/whatsmeow/util/hkdfutil"
	"google.golang.org/protobuf/proto"
)

func mediaFixture(t *testing.T, data []byte) (mediaRequest, Config, *events.Message) {
	t.Helper()
	m := operationMessage(operationsChat, "MEDIA", false, 1700000000)
	hash := sha256.Sum256(data)
	m.Message = &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{FileLength: proto.Uint64(uint64(len(data))), FileSHA256: hash[:], FileName: proto.String("safe.pdf"), Mimetype: proto.String("Application/PDF; version=1")}}
	m.RawMessage = m.Message
	meta, e := nativeMediaMetadata(m)
	if e != nil {
		t.Fatal(e)
	}
	req := mediaRequest{Chat: operationsChat, Message: LegacyMessageID(m)}
	req.Descriptor.ProviderID = req.Message
	req.Descriptor.Bytes = meta.Bytes
	req.Descriptor.Filename = meta.Filename
	req.Descriptor.Mime = meta.Mime
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if e = os.Chmod(root, 0700); e != nil {
		t.Fatal(e)
	}
	return req, Config{PagesDirectory: root, MaxMediaBytes: 1024 * 1024}, m
}

func mediaPolicy() (Policy, error) { return allowedOperationPolicy(), nil }

// The spool parent holds no regular temp file and the private area is empty.
func requireNoNativeTemp(t *testing.T, root string) {
	t.Helper()
	for _, dir := range []string{root, filepath.Join(root, nativeTempDirectory)} {
		files, err := os.ReadDir(dir)
		if err != nil && !os.IsNotExist(err) {
			t.Fatal(err)
		}
		for _, f := range files {
			if f.Type().IsRegular() {
				t.Fatal("temporary media leaked", dir, f.Name())
			}
		}
	}
}
func mediaGet(m *events.Message) func(string, string) (*events.Message, error) {
	return func(string, string) (*events.Message, error) { return m, nil }
}

func TestNativeMediaVerifiedStreamingAndCleanup(t *testing.T) {
	data := bytes.Repeat([]byte("synthetic-only "), 10000)
	req, cfg, m := mediaFixture(t, data)
	var output bytes.Buffer
	largest := 0
	err := downloadMedia(context.Background(), req, cfg, mediaPolicy, mediaGet(m), func(_ context.Context, _ whatsmeow.DownloadableMessage, file whatsmeow.File) error {
		_, e := io.Copy(file, bytes.NewReader(data))
		return e
	}, func(chunk []byte) error {
		if len(chunk) > largest {
			largest = len(chunk)
		}
		_, e := output.Write(chunk)
		return e
	})
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(output.Bytes(), data) || largest > 65536 {
		t.Fatalf("wrong data/chunks %d", largest)
	}
	// Changed assertion: temporaries now live in the private .native-tmp area,
	// which persists empty; the spool directory itself must hold no temp file.
	requireNoNativeTemp(t, cfg.PagesDirectory)
	if req.Descriptor.Mime != "application/pdf" {
		t.Fatal("MIME was not canonicalized")
	}
}

func TestNativeMediaNeverEmitsBeforeHashAndSizeVerification(t *testing.T) {
	for _, mode := range []string{"hash", "size", "overflow"} {
		t.Run(mode, func(t *testing.T) {
			data := []byte("synthetic media bytes")
			req, cfg, m := mediaFixture(t, data)
			emitted := 0
			err := downloadMedia(context.Background(), req, cfg, mediaPolicy, mediaGet(m), func(_ context.Context, _ whatsmeow.DownloadableMessage, file whatsmeow.File) error {
				wrong := bytes.Repeat([]byte("x"), len(data))
				if mode == "size" {
					wrong = wrong[:len(wrong)-1]
				}
				if mode == "overflow" {
					wrong = bytes.Repeat([]byte("x"), int(req.Descriptor.Bytes)+65537)
				}
				_, e := io.Copy(file, bytes.NewReader(wrong))
				return e
			}, func([]byte) error { emitted++; return nil })
			want := map[string]string{"hash": "media_hash_mismatch", "size": "media_size_mismatch", "overflow": "media_size_limit"}[mode]
			requireError(t, err, want)
			if emitted != 0 {
				t.Fatal("unverified media emitted")
			}
			// Changed assertion: temporaries now live in the private .native-tmp area,
			// which persists empty; the spool directory itself must hold no temp file.
			requireNoNativeTemp(t, cfg.PagesDirectory)
		})
	}
}

func TestBoundedMediaFileCannotBypassLimitThroughCopyOrWriteAt(t *testing.T) {
	f, e := os.CreateTemp(t.TempDir(), "bounded-")
	if e != nil {
		t.Fatal(e)
	}
	defer f.Close()
	bounded := &boundedMediaFile{File: f, Limit: 100}
	// io.Copy normally chooses os.File.ReadFrom. A promoted method used to bypass
	// the Write cap, so exercise that interface selection as well as direct I/O.
	_, e = io.Copy(bounded, io.LimitReader(bytes.NewReader(bytes.Repeat([]byte("x"), 101)), 101))
	requireError(t, e, "media_size_limit")
	if _, e = bounded.WriteAt([]byte("x"), 101); e == nil {
		t.Fatal("WriteAt exceeded bound")
	}
	if e = bounded.Truncate(101); e == nil {
		t.Fatal("Truncate exceeded bound")
	}
	info, _ := f.Stat()
	if info.Size() > 100 {
		t.Fatal("size cap bypassed")
	}
}

func TestNativeMediaRejectsChangedDescriptorsAndViewOnceBeforeDownload(t *testing.T) {
	for _, mode := range []string{"filename", "mime", "bytes", "provider_id", "wrong_chat", "viewonce", "scope", "size_limit"} {
		t.Run(mode, func(t *testing.T) {
			req, cfg, m := mediaFixture(t, []byte("safe"))
			policy := allowedOperationPolicy()
			switch mode {
			case "filename":
				req.Descriptor.Filename = "different.pdf"
			case "mime":
				req.Descriptor.Mime = "image/png"
			case "bytes":
				req.Descriptor.Bytes++
			case "provider_id":
				req.Descriptor.ProviderID = "different"
			case "wrong_chat":
				m.Info.Chat, _ = ParseChatID("15559999999@c.us")
			case "viewonce":
				m.IsViewOnce = true
			case "scope":
				policy.ExcludeChats = []string{operationsChat}
			case "size_limit":
				cfg.MaxMediaBytes = 1
			}
			called := false
			err := downloadMedia(context.Background(), req, cfg, func() (Policy, error) { return policy, nil }, mediaGet(m), func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error { called = true; return nil }, func([]byte) error { t.Fatal("invalid media emitted"); return nil })
			if err == nil || called {
				t.Fatalf("invalid media fetched %v", err)
			}
		})
	}
}

func TestNativeMediaRechecksScopeAfterDownloadAndEachChunk(t *testing.T) {
	for _, revokeAt := range []string{"after_download", "after_first_chunk"} {
		t.Run(revokeAt, func(t *testing.T) {
			data := bytes.Repeat([]byte("x"), 140000)
			req, cfg, m := mediaFixture(t, data)
			policy := allowedOperationPolicy()
			emitted := 0
			err := downloadMedia(context.Background(), req, cfg, func() (Policy, error) { return policy, nil }, mediaGet(m), func(_ context.Context, _ whatsmeow.DownloadableMessage, file whatsmeow.File) error {
				_, e := file.Write(data)
				if revokeAt == "after_download" {
					policy.ExcludeChats = []string{operationsChat}
				}
				return e
			}, func([]byte) error { emitted++; policy.ExcludeChats = []string{operationsChat}; return nil })
			requireError(t, err, "chat_excluded")
			want := 0
			if revokeAt == "after_first_chunk" {
				want = 1
			}
			if emitted != want {
				t.Fatalf("revocation emitted %d chunks want %d", emitted, want)
			}
		})
	}
}

func TestNativeMediaRootSymlinkAndPublicDirectoryRejected(t *testing.T) {
	root := t.TempDir()
	if e := os.Chmod(root, 0755); e != nil {
		t.Fatal(e)
	}
	requireError(t, ensureSafeMediaRoot(root), "invalid_media_path")
	if e := os.Chmod(root, 0700); e != nil {
		t.Fatal(e)
	}
	link := filepath.Join(t.TempDir(), "link")
	if e := os.Symlink(root, link); e != nil {
		t.Fatal(e)
	}
	requireError(t, ensureSafeMediaRoot(link), "invalid_media_path")
}

func TestNativeMediaTemporaryStaysOutsideSpoolQuota(t *testing.T) {
	data := []byte("synthetic media bytes")
	req, cfg, m := mediaFixture(t, data)
	err := downloadMedia(context.Background(), req, cfg, mediaPolicy, mediaGet(m), func(_ context.Context, _ whatsmeow.DownloadableMessage, file whatsmeow.File) error {
		// While the original is in flight the spool directory itself holds no
		// regular file, so the Python spool's byte quota is unaffected.
		entries, _ := os.ReadDir(cfg.PagesDirectory)
		for _, entry := range entries {
			if entry.Type().IsRegular() {
				t.Fatal("temporary original counted by the spool quota", entry.Name())
			}
		}
		inside, _ := os.ReadDir(filepath.Join(cfg.PagesDirectory, nativeTempDirectory))
		if len(inside) != 1 || !strings.HasPrefix(inside[0].Name(), ".native-media-") {
			t.Fatal("temporary original not in the private area", inside)
		}
		_, e := file.Write(data)
		return e
	}, func([]byte) error { return nil })
	if err != nil {
		t.Fatal(err)
	}
	info, err := os.Lstat(filepath.Join(cfg.PagesDirectory, nativeTempDirectory))
	if err != nil || info.Mode().Perm() != 0700 {
		t.Fatal("private temporary area not 0700", err)
	}
	requireNoNativeTemp(t, cfg.PagesDirectory)
}

func TestNativeMediaRejectsUnsafeTemporaryArea(t *testing.T) {
	for _, mode := range []string{"symlink", "public"} {
		t.Run(mode, func(t *testing.T) {
			req, cfg, m := mediaFixture(t, []byte("safe"))
			area := filepath.Join(cfg.PagesDirectory, nativeTempDirectory)
			target := t.TempDir()
			if mode == "symlink" {
				if err := os.Symlink(target, area); err != nil {
					t.Fatal(err)
				}
			} else if err := os.Mkdir(area, 0700); err != nil || os.Chmod(area, 0755) != nil {
				t.Fatal(err)
			}
			called := false
			err := downloadMedia(context.Background(), req, cfg, mediaPolicy, mediaGet(m), func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error { called = true; return nil }, func([]byte) error { return nil })
			requireError(t, err, "invalid_media_path")
			if entries, _ := os.ReadDir(target); called || len(entries) != 0 {
				t.Fatal("download used an unsafe temporary area")
			}
		})
	}
}

func TestStartupSweepsOnlyStaleNativeTemporaries(t *testing.T) {
	c := testConfig(t)
	write := func(path string) {
		t.Helper()
		if err := os.WriteFile(path, []byte("stale"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	pagesArea, err := nativeTempRoot(c.PagesDirectory)
	if err != nil {
		t.Fatal(err)
	}
	stale := []string{filepath.Join(pagesArea, ".native-media-123"),
		// Left in the parent directories by releases before the private area.
		filepath.Join(c.PagesDirectory, ".native-media-legacy"), filepath.Join(c.SessionDirectory, ".history-download-legacy")}
	kept := []string{filepath.Join(c.PagesDirectory, "00000000000000000001-abc.json"), filepath.Join(c.PagesDirectory, ".pending-python"), filepath.Join(c.SessionDirectory, "auth.sqlite"), filepath.Join(pagesArea, "unrelated")}
	// A symlinked area is never followed, even with a matching name inside.
	outside := t.TempDir()
	foreign := filepath.Join(outside, ".history-download-outside")
	if err = os.Symlink(outside, filepath.Join(c.SessionDirectory, nativeTempDirectory)); err != nil {
		t.Fatal(err)
	}
	for _, path := range append(append(append([]string{}, stale...), kept...), foreign) {
		write(path)
	}
	p := &Provider{Config: c}
	if err = p.prepareCapture(); err != nil {
		t.Fatal(err)
	}
	defer p.Capture.Close()
	for _, path := range stale {
		if _, err = os.Lstat(path); !os.IsNotExist(err) {
			t.Fatal("stale temporary survived startup", path, err)
		}
	}
	for _, path := range append(kept, foreign) {
		if _, err = os.Lstat(path); err != nil {
			t.Fatal("startup sweep removed a file it does not own", path, err)
		}
	}
}

func TestNativeMediaDeadlineScalesWithDeclaredSize(t *testing.T) {
	for size, want := range map[int64]time.Duration{0: time.Minute, 1 << 20: 64 * time.Second, 512 << 20: time.Minute + 2048*time.Second, 4 << 30: mediaDeadlineCap} {
		if got := mediaDeadline(size); got != want {
			t.Fatalf("%d -> %s want %s", size, got, want)
		}
	}
	// A 512 MiB original over 20 Mbit/s needs about 215 s; Python's own
	// transfer bound is 3600 s and must never be reached first.
	if mediaDeadline(512<<20) < 215*time.Second || mediaDeadlineCap >= time.Hour {
		t.Fatal("deadline does not fit a large original on a slow link")
	}
}

func shortMediaWatch(t *testing.T, keepalive, idle time.Duration) {
	t.Helper()
	oldKeepalive, oldIdle := mediaKeepaliveInterval, mediaIdleTimeout
	mediaKeepaliveInterval, mediaIdleTimeout = keepalive, idle
	t.Cleanup(func() { mediaKeepaliveInterval, mediaIdleTimeout = oldKeepalive, oldIdle })
}

func TestNativeMediaSlowDownloadIsKeptAliveWithoutUnverifiedBytes(t *testing.T) {
	// Total transfer time exceeds the idle timeout; each gap does not.
	shortMediaWatch(t, 10*time.Millisecond, 300*time.Millisecond)
	data := bytes.Repeat([]byte("synthetic-only "), 2000)
	req, cfg, m := mediaFixture(t, data)
	var output bytes.Buffer
	keepalives, dataStarted := 0, false
	err := downloadMedia(context.Background(), req, cfg, mediaPolicy, mediaGet(m), func(_ context.Context, _ whatsmeow.DownloadableMessage, file whatsmeow.File) error {
		for offset := 0; offset < len(data); offset += len(data) / 8 {
			time.Sleep(60 * time.Millisecond)
			if _, e := file.Write(data[offset:min(offset+len(data)/8, len(data))]); e != nil {
				return e
			}
		}
		return nil
	}, func(chunk []byte) error {
		if len(chunk) == 0 {
			// The frame must encode as an empty base64 string, never null.
			if frame, _ := json.Marshal(map[string]any{"chunk": chunk}); chunk == nil || string(frame) != `{"chunk":""}` || dataStarted {
				t.Error("invalid or late keepalive frame", string(frame))
			}
			keepalives++
			return nil
		}
		dataStarted = true
		_, e := output.Write(chunk)
		return e
	})
	if err != nil {
		t.Fatal("progressing download was cancelled", err)
	}
	if keepalives == 0 || !bytes.Equal(output.Bytes(), data) {
		t.Fatal("no keepalive before verified data, or data changed", keepalives)
	}
}

func TestNativeMediaStalledDownloadEndsWithTimeoutFrame(t *testing.T) {
	shortMediaWatch(t, 10*time.Millisecond, 100*time.Millisecond)
	req, cfg, m := mediaFixture(t, []byte("synthetic"))
	started := time.Now()
	nonEmpty := 0
	err := downloadMedia(context.Background(), req, cfg, mediaPolicy, mediaGet(m), func(ctx context.Context, _ whatsmeow.DownloadableMessage, _ whatsmeow.File) error {
		<-ctx.Done() // a stalled CDN connection that honours cancellation
		return ctx.Err()
	}, func(chunk []byte) error {
		if len(chunk) > 0 {
			nonEmpty++
		}
		return nil
	})
	requireError(t, err, "media_timeout")
	if nonEmpty != 0 || time.Since(started) > 5*time.Second {
		t.Fatal("stall emitted data or was not ended by the idle watchdog", nonEmpty)
	}
	requireNoNativeTemp(t, cfg.PagesDirectory)
}

func TestNativeMediaKeepaliveWriteFailureStopsTransfer(t *testing.T) {
	shortMediaWatch(t, 10*time.Millisecond, time.Minute)
	req, cfg, m := mediaFixture(t, []byte("synthetic"))
	err := downloadMedia(context.Background(), req, cfg, mediaPolicy, mediaGet(m), func(ctx context.Context, _ whatsmeow.DownloadableMessage, _ whatsmeow.File) error {
		<-ctx.Done()
		return ctx.Err()
	}, func([]byte) error { return errors.New("provider_response_too_large") })
	requireError(t, err, "provider_response_too_large")
}
func TestNativeMediaUnwritableStorageIsNotANetworkError(t *testing.T) {
	data := []byte("synthetic-only")
	req, cfg, m := mediaFixture(t, data)
	createMediaTemp = func(string, string) (*os.File, error) { return nil, os.ErrPermission }
	t.Cleanup(func() { createMediaTemp = os.CreateTemp })
	downloads := 0
	err := downloadMedia(context.Background(), req, cfg, mediaPolicy, mediaGet(m), func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error {
		downloads++
		return nil
	}, func([]byte) error { return nil })
	requireError(t, err, "media_storage_unavailable")
	if downloads != 0 {
		t.Fatal("provider download started without local storage")
	}
	if safeError(err) != "media_storage_unavailable" {
		t.Fatal("storage error not in the safe vocabulary")
	}
}

// spec: ANYM-815
func TestNativeMediaProviderErrorsKeepFixedCodesThroughTheSafeVocabulary(t *testing.T) {
	status := func(code int) error { return whatsmeow.DownloadHTTPError{Response: &http.Response{StatusCode: code}} }
	lastHost := func(err error) error { return fmt.Errorf("failed to download media from last host: %w", err) }
	for _, c := range []struct {
		err  error
		code string
	}{
		{whatsmeow.ErrMediaDownloadFailedWith403, "media_unavailable"},
		{whatsmeow.ErrMediaDownloadFailedWith404, "media_unavailable"},
		{whatsmeow.ErrMediaDownloadFailedWith410, "media_unavailable"},
		{lastHost(status(500)), "media_http_500"},
		{lastHost(status(503)), "media_http_503"},
		{status(429), "media_http_429"},
		{status(401), "media_http_401"},
		{lastHost(status(418)), "media_network_error"},
		{whatsmeow.ErrInvalidMediaHMAC, "media_decrypt_failed"},
		{fmt.Errorf("failed to decrypt file: %w", errors.New("synthetic padding")), "media_decrypt_failed"},
		{lastHost(fmt.Errorf("failed to decrypt file: %w", errors.New("synthetic padding"))), "media_decrypt_failed"},
		{lastHost(whatsmeow.ErrInvalidMediaHMAC), "media_decrypt_failed"},
		{whatsmeow.ErrInvalidMediaEncSHA256, "media_hash_mismatch"},
		{errors.New("synthetic unknown failure"), "media_network_error"},
	} {
		data := []byte("synthetic-only")
		req, cfg, m := mediaFixture(t, data)
		err := downloadMedia(context.Background(), req, cfg, mediaPolicy, mediaGet(m), func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error {
			return c.err
		}, func([]byte) error { return nil })
		requireError(t, err, c.code)
		// The IPC error frame carries the code itself, never provider_operation_failed.
		if safeError(err) != c.code {
			t.Fatalf("%v: %s is not in the safe vocabulary", c.err, c.code)
		}
		requireNoNativeTemp(t, cfg.PagesDirectory)
	}
}

// spec: ANYM-815
func TestNativeMediaLocalWriteFailureDuringDownloadIsStorageUnavailable(t *testing.T) {
	data := []byte("synthetic-only")
	req, cfg, m := mediaFixture(t, data)
	err := downloadMedia(context.Background(), req, cfg, mediaPolicy, mediaGet(m), func(_ context.Context, _ whatsmeow.DownloadableMessage, f whatsmeow.File) error {
		_ = f.(*boundedMediaFile).File.Close()
		if _, e := f.Write([]byte("x")); e != nil {
			return fmt.Errorf("failed to write synthetic media: %w", e)
		}
		return nil
	}, func([]byte) error { return nil })
	requireError(t, err, "media_storage_unavailable")
}

func mediaKeyMessage(t *testing.T, id string, key []byte) *events.Message {
	t.Helper()
	m := operationMessage(operationsChat, id, false, 1700000000)
	hash := sha256.Sum256([]byte(id))
	m.Message = &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{FileLength: proto.Uint64(4), FileSHA256: hash[:], FileEncSHA256: hash[:], MediaKey: key, DirectPath: proto.String("/v/t62.7119-24/synthetic"), FileName: proto.String("synthetic.pdf"), Mimetype: proto.String("application/pdf")}}
	m.RawMessage = m.Message
	return m
}

// spec: ANYM-815
func TestMediaKeysAnswersFromTheJournalReadOnlyBesideTheOwningAdapter(t *testing.T) {
	config := testConfig(t)
	s, err := OpenCapture(config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	s.Owner = types.NewJID("123456", types.DefaultUserServer)
	held, none := mediaKeyMessage(t, "KEYHELD", bytes.Repeat([]byte{7}, 32)), mediaKeyMessage(t, "KEYNONE", nil)
	for _, m := range []*events.Message{held, none} {
		if err := s.Put(m, "live"); err != nil {
			t.Fatal(err)
		}
	}
	path := filepath.Join(filepath.Dir(config.SessionDirectory), "config.json")
	if err := os.WriteFile(path, mustJSON(config), 0600); err != nil {
		t.Fatal(err)
	}
	ref := func(m *events.Message) map[string]string {
		return map[string]string{"chat": LegacyChatID(m.Info.Chat), "message": LegacyMessageID(m)}
	}
	missing := map[string]string{"chat": operationsChat, "message": "false_" + operationsChat + "_ABSENT"}
	invalid := map[string]string{"chat": operationsChat, "message": "not-a-reference"}
	over := make([]map[string]string, mediaKeysBatch+1)
	for i := range over {
		over[i] = missing
	}
	in := strings.NewReader(string(mustJSON(map[string]any{"id": 1, "method": "media_keys", "params": map[string]any{"messages": []any{ref(held), ref(none), missing, invalid}}})) + "\n" +
		string(mustJSON(map[string]any{"id": 2, "method": "media", "params": map[string]any{}})) + "\n" +
		string(mustJSON(map[string]any{"id": 3, "method": "media_keys", "params": map[string]any{"messages": over}})) + "\n")
	var out bytes.Buffer
	if err := serveMediaKeys(path, in, &out); err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	want := []string{`{"id":1,"result":{"keys":["held","none","none","none"]}}`, `{"error":"invalid_request","id":2}`, `{"error":"invalid_request","id":3}`}
	if strings.Join(lines, "|") != strings.Join(want, "|") {
		t.Fatalf("got %q", lines)
	}
	// The owning adapter keeps writing while the lookup runs, and the lookup's connection cannot write.
	if err := s.Put(fixtureMessage("AFTER", 1700000002), "live"); err != nil {
		t.Fatal(err)
	}
	db, err := sql.Open("sqlite3", mediaKeysDSN(filepath.Join(config.SessionDirectory, "capture.sqlite")))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec("CREATE TABLE inventory_probe(x)"); err == nil {
		t.Fatal("media_keys journal connection accepted a write")
	}
	if _, err := db.Exec("DELETE FROM capture"); err == nil {
		t.Fatal("media_keys journal connection accepted a delete")
	}
	// No session directory journal: the lookup refuses instead of creating one.
	empty := testConfig(t)
	emptyPath := filepath.Join(filepath.Dir(empty.SessionDirectory), "config.json")
	if err := os.WriteFile(emptyPath, mustJSON(empty), 0600); err != nil {
		t.Fatal(err)
	}
	requireError(t, serveMediaKeys(emptyPath, strings.NewReader(""), &out), "media_keys_unavailable")
	if _, err := os.Stat(filepath.Join(empty.SessionDirectory, "capture.sqlite")); !os.IsNotExist(err) {
		t.Fatal("media_keys created a journal")
	}
}

// spec: ANYM-815
func TestMediaKeysAnswersUnknownWhenTheJournalCannotBeReadInTime(t *testing.T) {
	m := mediaKeyMessage(t, "KEYHELD", bytes.Repeat([]byte{7}, 32))
	chat, id := LegacyChatID(m.Info.Chat), LegacyMessageID(m)
	var deadline time.Duration
	slow := func(ctx context.Context, _, _ string) ([]byte, error) {
		d, ok := ctx.Deadline()
		if !ok {
			t.Fatal("journal lookup without a deadline")
		}
		deadline = time.Until(d)
		<-ctx.Done()
		return nil, ctx.Err()
	}
	if got := mediaKeyState(slow, chat, id); got != "unknown" {
		t.Fatalf("slow lookup answered %s", got)
	}
	if deadline <= 0 || deadline > mediaKeyLookupTimeout || mediaKeyLookupTimeout >= time.Second {
		t.Fatalf("lookup deadline %s, bound %s: must stay far below the writer's 5 s busy timeout", deadline, mediaKeyLookupTimeout)
	}
	broken := func(context.Context, string, string) ([]byte, error) {
		return nil, errors.New("database disk image is malformed")
	}
	garbled := func(context.Context, string, string) ([]byte, error) { return []byte("not a message"), nil }
	missing := func(context.Context, string, string) ([]byte, error) { return nil, nil }
	for name, c := range map[string]struct {
		lookup mediaKeyLookup
		want   string
	}{"error": {broken, "unknown"}, "undecodable": {garbled, "unknown"}, "missing": {missing, "none"}} {
		if got := mediaKeyState(c.lookup, chat, id); got != c.want {
			t.Fatalf("%s: got %s, want %s", name, got, c.want)
		}
	}
}

// spec: ANYM-815
func TestMediaKeysLookupsNeverMakeCaptureWritesFail(t *testing.T) {
	config := testConfig(t)
	s, err := OpenCapture(config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	s.Owner = types.NewJID("123456", types.DefaultUserServer)
	for i := 0; i < 200; i++ {
		if err := s.Put(mediaKeyMessage(t, fmt.Sprintf("SEED%03d", i), bytes.Repeat([]byte{7}, 32)), "live"); err != nil {
			t.Fatal(err)
		}
	}
	db, err := sql.Open("sqlite3", mediaKeysDSN(filepath.Join(config.SessionDirectory, "capture.sqlite")))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	lookup := journalKeyLookup(db)
	stop := make(chan struct{})
	done := make(chan int)
	go func() {
		n := 0
		for {
			select {
			case <-stop:
				done <- n
				return
			default:
			}
			mediaKeyState(lookup, operationsChat, fmt.Sprintf("false_%s_SEED%03d", operationsChat, n%200))
			n++
		}
	}()
	// The owning adapter keeps capturing while the inventory reads in a tight loop.
	for i := 0; i < 100; i++ {
		if err := s.Put(fixtureMessage(fmt.Sprintf("LIVE%03d", i), 1700000100+int64(i)), "live"); err != nil {
			close(stop)
			t.Fatalf("capture write %d failed beside the lookup: %v", i, err)
		}
	}
	close(stop)
	if n := <-done; n == 0 {
		t.Fatal("no lookup ran during capture")
	}
}

// spec: ANYM-815
func TestMediaKeysRealJournalLookupEndsAtItsDeadlineAndCaptureContinues(t *testing.T) {
	config := testConfig(t)
	s, err := OpenCapture(config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	s.Owner = types.NewJID("123456", types.DefaultUserServer)
	m := mediaKeyMessage(t, "KEYHELD", bytes.Repeat([]byte{7}, 32))
	if err := s.Put(m, "live"); err != nil {
		t.Fatal(err)
	}
	journal := filepath.Join(config.SessionDirectory, "capture.sqlite")
	ro, err := sql.Open("sqlite3", mediaKeysDSN(journal))
	if err != nil {
		t.Fatal(err)
	}
	defer ro.Close()
	ro.SetMaxOpenConns(1)
	lookup := journalKeyLookup(ro)
	chat, id := LegacyChatID(m.Info.Chat), LegacyMessageID(m)
	// A second writer connection holds the journal's exclusive lock longer than the
	// lookup's deadline but shorter than its busy timeout: only the deadline can end it.
	w, err := sql.Open("sqlite3", sqliteDSN(journal))
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	conn, err := w.Conn(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if _, err := conn.ExecContext(context.Background(), "BEGIN EXCLUSIVE"); err != nil {
		t.Fatal(err)
	}
	released := make(chan struct{})
	go func() {
		time.Sleep(900 * time.Millisecond)
		_, _ = conn.ExecContext(context.Background(), "ROLLBACK")
		close(released)
	}()
	started := time.Now()
	state := mediaKeyState(lookup, chat, id)
	elapsed := time.Since(started)
	if state != "unknown" || elapsed > 700*time.Millisecond {
		t.Fatalf("locked journal: got %s after %s; the lookup must end at its %s deadline", state, elapsed, mediaKeyLookupTimeout)
	}
	<-released
	if got := mediaKeyState(lookup, chat, id); got != "held" {
		t.Fatalf("after the lock: got %s", got)
	}
	if err := s.Put(fixtureMessage("AFTERLOCK", 1700000300), "live"); err != nil {
		t.Fatal("capture write after the bounded lookup:", err)
	}
}

// spec: ANYM-815
func TestMediaKeysCreatesNoDirectoryForAMissingSession(t *testing.T) {
	root := t.TempDir()
	_ = os.Chmod(root, 0700)
	c := Config{Account: "test", ExpectedWID: "123456@c.us", SessionDirectory: filepath.Join(root, "session"), EventsDirectory: filepath.Join(root, "events"), PagesDirectory: filepath.Join(root, "pages")}
	path := filepath.Join(root, "config.json")
	if err := os.WriteFile(path, mustJSON(c), 0600); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	requireError(t, serveMediaKeys(path, strings.NewReader(""), &out), "media_keys_unavailable")
	for _, d := range []string{c.SessionDirectory, c.EventsDirectory, c.PagesDirectory} {
		if _, err := os.Stat(d); !os.IsNotExist(err) {
			t.Fatal("media_keys created", d)
		}
	}
}

// whatsmeowDecrypt mirrors whatsmeow's downloadAndDecrypt offline: the HKDF
// info comes from the payload's own media type, as in Client.DownloadToFile.
func whatsmeowDecrypt(msg whatsmeow.DownloadableMessage, mediaType whatsmeow.MediaType, blob []byte) ([]byte, error) {
	expanded := hkdfutil.SHA256(msg.GetMediaKey(), nil, []byte(mediaType), 112)
	iv, cipherKey, macKey := expanded[:16], expanded[16:48], expanded[48:80]
	ciphertext, mac := blob[:len(blob)-10], blob[len(blob)-10:]
	h := hmac.New(sha256.New, macKey)
	h.Write(iv)
	h.Write(ciphertext)
	if !hmac.Equal(h.Sum(nil)[:10], mac) {
		return nil, whatsmeow.ErrInvalidMediaHMAC
	}
	return cbcutil.Decrypt(cipherKey, iv, ciphertext)
}

func TestNativeRoundVideoDownloadsAndVerifiesWithVideoKeys(t *testing.T) {
	plain := bytes.Repeat([]byte("synthetic round video "), 500)
	mediaKey := bytes.Repeat([]byte{7}, 32)
	expanded := hkdfutil.SHA256(mediaKey, nil, []byte("WhatsApp Video Keys"), 112)
	iv, cipherKey, macKey := expanded[:16], expanded[16:48], expanded[48:80]
	ciphertext, err := cbcutil.Encrypt(cipherKey, iv, plain[:len(plain):len(plain)])
	if err != nil {
		t.Fatal(err)
	}
	h := hmac.New(sha256.New, macKey)
	h.Write(iv)
	h.Write(ciphertext)
	blob := append(ciphertext, h.Sum(nil)[:10]...)
	plainHash, encHash := sha256.Sum256(plain), sha256.Sum256(blob)
	_, cfg, m := mediaFixture(t, plain)
	m.Message = &waE2E.Message{PtvMessage: &waE2E.VideoMessage{Mimetype: proto.String("video/mp4"), FileLength: proto.Uint64(uint64(len(plain))), Seconds: proto.Uint32(9), MediaKey: mediaKey, FileSHA256: plainHash[:], FileEncSHA256: encHash[:], DirectPath: proto.String("/synthetic")}}
	m.RawMessage = m.Message
	meta, err := nativeMediaMetadata(m)
	if err != nil || meta.Payload != m.Message.GetPtvMessage() {
		t.Fatal("round video has no media descriptor", err)
	}
	req := mediaRequest{Chat: operationsChat, Message: LegacyMessageID(m)}
	req.Descriptor.ProviderID, req.Descriptor.Bytes, req.Descriptor.Filename, req.Descriptor.Mime = req.Message, meta.Bytes, meta.Filename, meta.Mime
	var output bytes.Buffer
	err = downloadMedia(context.Background(), req, cfg, mediaPolicy, mediaGet(m), func(_ context.Context, msg whatsmeow.DownloadableMessage, file whatsmeow.File) error {
		if whatsmeow.GetMediaType(msg) != whatsmeow.MediaVideo {
			t.Fatalf("round video downloads as %q", whatsmeow.GetMediaType(msg))
		}
		if _, e := whatsmeowDecrypt(msg, whatsmeow.MediaImage, blob); !errors.Is(e, whatsmeow.ErrInvalidMediaHMAC) {
			t.Fatal("payload verified with keys other than Video", e)
		}
		data, e := whatsmeowDecrypt(msg, whatsmeow.GetMediaType(msg), blob)
		if e != nil {
			return e
		}
		_, e = file.Write(data)
		return e
	}, func(chunk []byte) error {
		_, e := output.Write(chunk)
		return e
	})
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(output.Bytes(), plain) || req.Descriptor.Mime != "video/mp4" {
		t.Fatal("round video original not verified and emitted")
	}
	requireNoNativeTemp(t, cfg.PagesDirectory)
}

// spec: ANYM-386
func TestNativeMediaChangedReferenceIsRefusedWithReferenceChangedCode(t *testing.T) {
	for _, mode := range []string{"filename", "mime", "bytes", "provider_id", "wrong_chat"} {
		t.Run(mode, func(t *testing.T) {
			req, cfg, m := mediaFixture(t, []byte("safe"))
			switch mode {
			case "filename":
				req.Descriptor.Filename = "different.pdf"
			case "mime":
				req.Descriptor.Mime = "image/png"
			case "bytes":
				req.Descriptor.Bytes++
			case "provider_id":
				req.Descriptor.ProviderID = "different"
			case "wrong_chat":
				m.Info.Chat, _ = ParseChatID("15559999999@c.us")
			}
			downloads, emitted := 0, 0
			err := downloadMedia(context.Background(), req, cfg, mediaPolicy, mediaGet(m), func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error { downloads++; return nil }, func([]byte) error { emitted++; return nil })
			requireError(t, err, "media_reference_changed")
			if downloads != 0 || emitted != 0 {
				t.Fatal("changed reference fetched or emitted", downloads, emitted)
			}
		})
	}
}

// spec: ANYM-386
func TestNativeMediaViewOnceAndOversizeAreRefusedWithTheirCodes(t *testing.T) {
	for mode, want := range map[string]string{"viewonce_flag": "unsupported_media_type", "viewonce_wrapper": "unsupported_media_type", "over_max_media_bytes": "media_size_limit"} {
		t.Run(mode, func(t *testing.T) {
			data := []byte("synthetic media bytes")
			req, cfg, m := mediaFixture(t, data)
			switch mode {
			case "viewonce_flag":
				m.IsViewOnce = true
			case "viewonce_wrapper":
				m.Message = &waE2E.Message{ViewOnceMessageV2: &waE2E.FutureProofMessage{Message: m.Message}}
				m.RawMessage = m.Message
			case "over_max_media_bytes":
				cfg.MaxMediaBytes = int64(len(data)) - 1
			}
			downloads, emitted := 0, 0
			err := downloadMedia(context.Background(), req, cfg, mediaPolicy, mediaGet(m), func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error { downloads++; return nil }, func([]byte) error { emitted++; return nil })
			requireError(t, err, want)
			if downloads != 0 || emitted != 0 {
				t.Fatal("refused media fetched or emitted", downloads, emitted)
			}
		})
	}
}

// spec: ANYM-386
func TestNativeMediaChecksPolicyBeforeCacheReadAndAfterTemporaryCreation(t *testing.T) {
	for _, revokeAt := range []string{"before_cache_read", "after_temporary_created"} {
		t.Run(revokeAt, func(t *testing.T) {
			req, cfg, m := mediaFixture(t, []byte("safe"))
			policyCalls, gets, temps, downloads := 0, 0, 0, 0
			// The first policy read allows the chat only in the second mode; every
			// later read excludes it.
			policy := func() (Policy, error) {
				policyCalls++
				p := allowedOperationPolicy()
				if revokeAt == "before_cache_read" || policyCalls > 1 {
					p.ExcludeChats = []string{operationsChat}
				}
				return p, nil
			}
			get := func(string, string) (*events.Message, error) { gets++; return m, nil }
			createMediaTemp = func(dir, pattern string) (*os.File, error) { temps++; return os.CreateTemp(dir, pattern) }
			t.Cleanup(func() { createMediaTemp = os.CreateTemp })
			err := downloadMedia(context.Background(), req, cfg, policy, get, func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error { downloads++; return nil }, func([]byte) error { t.Fatal("excluded media emitted"); return nil })
			requireError(t, err, "chat_excluded")
			want := map[string][3]int{"before_cache_read": {1, 0, 0}, "after_temporary_created": {2, 1, 1}}[revokeAt]
			if got := [3]int{policyCalls, gets, temps}; got != want || downloads != 0 {
				t.Fatalf("policy/cache/temp calls %v want %v, downloads %d", got, want, downloads)
			}
			requireNoNativeTemp(t, cfg.PagesDirectory)
		})
	}
}

// spec: ANYM-386
func TestNativeMediaProductionKeepaliveIntervalAndIdleTimeout(t *testing.T) {
	// The watchdog itself is exercised with shortened values above; this pins the
	// production defaults those tests replace.
	if mediaKeepaliveInterval != 30*time.Second || mediaIdleTimeout != 2*time.Minute {
		t.Fatal("production media watchdog values changed", mediaKeepaliveInterval, mediaIdleTimeout)
	}
}
