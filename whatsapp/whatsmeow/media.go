package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types/events"
)

type nativeDownloadable interface {
	whatsmeow.DownloadableMessage
	GetFileLength() uint64
	GetFileSHA256() []byte
	GetMimetype() string
}

type nativeMediaInfo struct {
	Kind, Filename, Mime string
	Bytes                int64
	Payload              nativeDownloadable
}

func nativeMediaMetadata(m *events.Message) (nativeMediaInfo, error) {
	var out nativeMediaInfo
	if m == nil || m.Message == nil {
		return out, errors.New("media_unavailable")
	}
	// View-once retention must never be enabled accidentally by unwrapping it.
	if m.IsViewOnce || m.Message.GetViewOnceMessage() != nil || m.Message.GetViewOnceMessageV2() != nil || m.Message.GetViewOnceMessageV2Extension() != nil {
		return out, errors.New("unsupported_media_type")
	}
	wire := m.Message
	switch {
	case wire.GetImageMessage() != nil:
		out.Kind = "image"
		out.Payload = wire.GetImageMessage()
	case wire.GetAudioMessage() != nil:
		out.Kind = "audio"
		out.Payload = wire.GetAudioMessage()
		if wire.GetAudioMessage().GetPTT() {
			out.Kind = "ptt"
		}
	case wire.GetVideoMessage() != nil:
		out.Kind = "video"
		out.Payload = wire.GetVideoMessage()
	case wire.GetPtvMessage() != nil:
		// *VideoMessage downloads with the WhatsApp Video keys.
		out.Kind = "ptv"
		out.Payload = wire.GetPtvMessage()
	case wire.GetDocumentMessage() != nil:
		out.Kind = "document"
		out.Payload = wire.GetDocumentMessage()
		out.Filename = wire.GetDocumentMessage().GetFileName()
	case wire.GetStickerMessage() != nil:
		out.Kind = "sticker"
		out.Payload = wire.GetStickerMessage()
	default:
		return out, errors.New("unsupported_media_type")
	}
	// A declared size the archive cannot represent stays unknown (-1); it is
	// never clamped into a plausible value. Such an original is not fetched.
	out.Bytes = -1
	if out.Payload.GetFileLength() <= maxEnvelopeMediaBytes {
		out.Bytes = int64(out.Payload.GetFileLength())
	}
	out.Mime = envelopeMime(out.Payload.GetMimetype())
	if out.Filename == "" {
		out.Filename = envelopeText(LegacyMessageID(m), 120) + ".bin"
	}
	out.Filename = envelopeFilename(strings.Map(func(r rune) rune {
		if r < 32 || r == 127 || r == '/' || r == '\\' {
			return '_'
		}
		return r
	}, out.Filename))
	return out, nil
}

type mediaRequest struct {
	Chat       string `json:"chat"`
	Message    string `json:"message"`
	Descriptor struct {
		ProviderID string `json:"provider_id"`
		Bytes      int64  `json:"bytes"`
		Filename   string `json:"filename"`
		Mime       string `json:"mime"`
	} `json:"descriptor"`
}

type boundedMediaFile struct {
	File  *os.File
	Limit int64
	// Optional: last successful I/O (UnixNano) for the media idle watchdog.
	progress *atomic.Int64
	// The first failure of the local temporary file itself (ANYM-815): the
	// download then ends as media_storage_unavailable, not as a provider error.
	local error
}

func (f *boundedMediaFile) failed(err error) error {
	if err != nil && err != io.EOF && f.local == nil {
		f.local = err
	}
	return err
}

func (f *boundedMediaFile) touch(n int) {
	if f.progress != nil && n > 0 {
		f.progress.Store(time.Now().UnixNano())
	}
}
func (f *boundedMediaFile) idle() time.Duration { return time.Since(time.Unix(0, f.progress.Load())) }

func (f *boundedMediaFile) Read(b []byte) (int, error) {
	n, e := f.File.Read(b)
	f.touch(n)
	return n, f.failed(e)
}
func (f *boundedMediaFile) ReadAt(b []byte, o int64) (int, error) {
	n, e := f.File.ReadAt(b, o)
	f.touch(n)
	return n, f.failed(e)
}
func (f *boundedMediaFile) Seek(o int64, w int) (int64, error) {
	n, e := f.File.Seek(o, w)
	return n, f.failed(e)
}
func (f *boundedMediaFile) Stat() (os.FileInfo, error) {
	i, e := f.File.Stat()
	return i, f.failed(e)
}

func (f *boundedMediaFile) Write(b []byte) (int, error) {
	o, err := f.File.Seek(0, io.SeekCurrent)
	if err != nil {
		return 0, f.failed(err)
	}
	if o < 0 || int64(len(b)) > f.Limit-o {
		return 0, errors.New("media_size_limit")
	}
	n, err := f.File.Write(b)
	f.touch(n)
	return n, f.failed(err)
}
func (f *boundedMediaFile) WriteAt(b []byte, o int64) (int, error) {
	if o < 0 || int64(len(b)) > f.Limit-o {
		return 0, errors.New("media_size_limit")
	}
	n, err := f.File.WriteAt(b, o)
	f.touch(n)
	return n, f.failed(err)
}
func (f *boundedMediaFile) Truncate(n int64) error {
	if n < 0 || n > f.Limit {
		return errors.New("media_size_limit")
	}
	return f.failed(f.File.Truncate(n))
}

func (p *Provider) Media(ctx context.Context, raw json.RawMessage, emit func([]byte) error) error {
	var req mediaRequest
	if decodeStrict(raw, &req) != nil {
		return errors.New("media_reference_changed")
	}
	return downloadMedia(ctx, req, p.Config, p.policy, p.Capture.Get,
		func(ctx context.Context, m whatsmeow.DownloadableMessage, f whatsmeow.File) error {
			return p.Client.DownloadToFile(ctx, m, f)
		}, emit)
}

func downloadMedia(ctx context.Context, req mediaRequest, cfg Config, policy func() (Policy, error), get func(string, string) (*events.Message, error), download func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error, emit func([]byte) error) error {
	check := func() error {
		p, err := policy()
		if err != nil || !p.Enabled || !p.Allows(req.Chat) {
			return errors.New("chat_excluded")
		}
		return nil
	}
	if err := check(); err != nil {
		return err
	}
	if !validMessageReference(req.Chat, req.Message) {
		return errors.New("media_reference_changed")
	}
	m, err := get(req.Chat, req.Message)
	if err != nil || m == nil {
		return errors.New("media_unavailable")
	}
	if LegacyChatID(m.Info.Chat) != req.Chat || LegacyMessageID(m) != req.Message {
		return errors.New("media_reference_changed")
	}
	meta, err := nativeMediaMetadata(m)
	if err != nil {
		return err
	}
	if meta.Bytes < 0 {
		return errors.New("media_size_limit")
	}
	if req.Descriptor.ProviderID != req.Message || req.Descriptor.Bytes != meta.Bytes || req.Descriptor.Filename != meta.Filename || req.Descriptor.Mime != meta.Mime {
		return errors.New("media_reference_changed")
	}
	limit := cfg.MaxMediaBytes
	if limit <= 0 {
		limit = 512 * 1024 * 1024
	}
	if meta.Bytes <= 0 || meta.Bytes > limit {
		return errors.New("media_size_limit")
	}
	if len(meta.Payload.GetFileSHA256()) != 32 {
		return errors.New("media_metadata_unavailable")
	}
	root := cfg.PagesDirectory
	if root == "" {
		root = cfg.EventsDirectory
	}
	// No arbitrary path comes from an agent. Only the configured private root is used.
	if err = ensureSafeMediaRoot(root); err != nil {
		return err
	}
	var disk syscall.Statfs_t
	if err = syscall.Statfs(root, &disk); err != nil {
		return errors.New("media_storage_unavailable")
	}
	reserve := cfg.DiskReserveBytes
	if reserve < 0 {
		return errors.New("media_size_limit")
	}
	available := uint64(disk.Bavail) * uint64(disk.Bsize)
	if uint64(meta.Bytes)+65536 > available || uint64(reserve) > available-uint64(meta.Bytes)-65536 {
		return errors.New("media_size_limit")
	}
	tmpRoot, err := nativeTempRoot(root)
	if err != nil {
		return err
	}
	tmp, err := createMediaTemp(tmpRoot, ".native-media-")
	if err != nil {
		return errors.New("media_storage_unavailable")
	}
	defer os.Remove(tmp.Name())
	defer tmp.Close()
	if err = tmp.Chmod(0600); err != nil {
		return errors.New("media_storage_unavailable")
	}
	if err = check(); err != nil {
		return err
	}
	// One deadline covers the provider download and the stream to Python. It
	// scales with the declared size; the idle watchdog still ends a stall early.
	ctx, cancel := context.WithTimeout(ctx, mediaDeadline(meta.Bytes))
	defer cancel()
	// Native decrypt writes ciphertext then plaintext in place, so admit a small
	// cryptographic overhead but never an unbounded provider-controlled length.
	file := &boundedMediaFile{File: tmp, Limit: meta.Bytes + 65536, progress: &atomic.Int64{}}
	stopWatch := watchMediaDownload(ctx, cancel, file, emit)
	err = func() error {
		if err := download(ctx, meta.Payload, file); err != nil {
			if file.local != nil {
				// A local write failure spends no provider attempt (archive.fail_media).
				return errors.New("media_storage_unavailable")
			}
			return safeMediaError(err)
		}
		info, err := tmp.Stat()
		if err != nil {
			return errors.New("media_storage_unavailable")
		}
		if info.Size() != meta.Bytes {
			return errors.New("media_size_mismatch")
		}
		if _, err = tmp.Seek(0, io.SeekStart); err != nil {
			return errors.New("media_storage_unavailable")
		}
		h := sha256.New()
		// Read through the wrapper: local hashing is progress for the watchdog.
		if _, err = io.Copy(h, file); err != nil {
			return errors.New("media_storage_unavailable")
		}
		if !bytes.Equal(h.Sum(nil), meta.Payload.GetFileSHA256()) {
			return errors.New("media_hash_mismatch")
		}
		return nil
	}()
	// The watchdog has stopped emitting before any verified chunk is sent.
	if keepaliveErr := stopWatch(); keepaliveErr != nil {
		return keepaliveErr
	}
	if err != nil {
		return err
	}
	if _, err = tmp.Seek(0, io.SeekStart); err != nil {
		return errors.New("media_storage_unavailable")
	}
	buffer := make([]byte, 64*1024)
	for {
		if err = check(); err != nil {
			return err
		}
		if err = ctx.Err(); err != nil {
			return safeMediaError(err)
		}
		n, readErr := tmp.Read(buffer)
		if n > 0 {
			if err = emit(buffer[:n]); err != nil {
				return err
			}
		}
		if readErr == io.EOF {
			return nil
		}
		if readErr != nil {
			return errors.New("media_storage_unavailable")
		}
	}
}

// The transfer deadline assumes a conservative minimum rate (about 2 Mbit/s):
// a 512 MiB original gets about 35 minutes. The cap stays below the Python
// collector's 3600 s transfer bound, so a request always ends with a
// synchronized error frame instead of a killed provider.
const (
	mediaDeadlineBase      = 60 * time.Second
	mediaMinBytesPerSecond = 256 << 10
	mediaDeadlineCap       = 50 * time.Minute
)

// Variables only so tests can shorten them. Python waits at most 90 s for each
// reply frame; keepalives are sent well inside that.
var (
	mediaKeepaliveInterval = 30 * time.Second
	mediaIdleTimeout       = 2 * time.Minute
)

func mediaDeadline(size int64) time.Duration {
	if size <= 0 {
		return mediaDeadlineBase
	}
	if size/mediaMinBytesPerSecond >= int64((mediaDeadlineCap-mediaDeadlineBase)/time.Second) {
		return mediaDeadlineCap
	}
	return mediaDeadlineBase + time.Duration(size/mediaMinBytesPerSecond)*time.Second
}

// A native original is emitted only after it has been downloaded and verified,
// which can take longer than Python's per-frame wait. Until then the watchdog
// sends empty chunks (an existing frame shape carrying no byte at all) and
// cancels the transfer when the temporary file shows no I/O for
// mediaIdleTimeout. The returned stop function waits for it to exit, so frames
// are never emitted concurrently.
func watchMediaDownload(ctx context.Context, cancel context.CancelFunc, file *boundedMediaFile, emit func([]byte) error) func() error {
	file.progress.Store(time.Now().UnixNano())
	stop, done := make(chan struct{}), make(chan struct{})
	var emitErr error
	go func() {
		defer close(done)
		ticker := time.NewTicker(mediaKeepaliveInterval)
		defer ticker.Stop()
		for {
			select {
			case <-stop:
				return
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
			if file.idle() > mediaIdleTimeout {
				cancel()
				return
			}
			if emitErr = emit([]byte{}); emitErr != nil {
				cancel()
				return
			}
		}
	}()
	return func() error {
		close(stop)
		<-done
		return emitErr
	}
}

// createMediaTemp is replaceable only so tests can simulate unwritable storage.
var createMediaTemp = os.CreateTemp

func ensureSafeMediaRoot(root string) error {
	if root == "" || !filepath.IsAbs(root) {
		return errors.New("invalid_media_path")
	}
	real, err := filepath.EvalSymlinks(root)
	if err != nil || filepath.Clean(root) != real {
		return errors.New("invalid_media_path")
	}
	info, err := os.Lstat(root)
	if err != nil || !info.IsDir() || info.Mode().Perm()&0077 != 0 {
		return errors.New("invalid_media_path")
	}
	return nil
}

// Native temporaries never sit beside spool queue files: the Python spool
// counts every regular file in its top directory against its byte quota, and a
// crash left them there. It only globs *.json and sums top-level regular files,
// so this private subdirectory is outside both its quota and its drain.
const nativeTempDirectory = ".native-tmp"

var nativeTempPrefixes = []string{".native-media-", ".history-download-"}

func nativeTempRoot(parent string) (string, error) {
	dir := filepath.Join(parent, nativeTempDirectory)
	if err := os.Mkdir(dir, 0700); err != nil && !errors.Is(err, os.ErrExist) {
		return "", errors.New("invalid_media_path")
	}
	info, err := os.Lstat(dir)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0077 != 0 {
		return "", errors.New("invalid_media_path")
	}
	return dir, nil
}

// sweepNativeTemp removes temporaries left by a crashed or killed provider,
// including ones from releases that wrote them into the parent directory. The
// caller holds the session lock, so no other provider of this account writes
// there. Only our own regular files are removed and no symlink is followed.
func sweepNativeTemp(cfg Config) {
	media := cfg.PagesDirectory
	if media == "" {
		media = cfg.EventsDirectory
	}
	for _, parent := range []string{media, cfg.SessionDirectory} {
		if parent == "" {
			continue
		}
		for _, dir := range []string{filepath.Join(parent, nativeTempDirectory), parent} {
			if info, err := os.Lstat(dir); err != nil || !info.IsDir() {
				continue
			}
			entries, err := os.ReadDir(dir)
			if err != nil {
				continue
			}
			for _, entry := range entries {
				for _, prefix := range nativeTempPrefixes {
					if entry.Type().IsRegular() && strings.HasPrefix(entry.Name(), prefix) {
						_ = os.Remove(filepath.Join(dir, entry.Name()))
					}
				}
			}
		}
	}
}

// whatsmeow wraps a CBC failure as "failed to decrypt file: %w", and the host
// loop may wrap that again ("failed to download media from last host: %w").
func decryptFailure(err error) bool {
	for ; err != nil; err = errors.Unwrap(err) {
		if strings.HasPrefix(err.Error(), "failed to decrypt file:") {
			return true
		}
	}
	return false
}

// HTTP answers of the media CDN that keep their own code (ANYM-815); any other
// status, and every error without one, stays media_network_error.
var mediaHTTPCodes = map[int]string{400: "media_http_400", 401: "media_http_401", 408: "media_http_408", 429: "media_http_429", 500: "media_http_500", 502: "media_http_502", 503: "media_http_503", 504: "media_http_504"}

func safeMediaError(err error) error {
	var status whatsmeow.DownloadHTTPError
	switch {
	case errors.Is(err, context.Canceled), errors.Is(err, context.DeadlineExceeded):
		return errors.New("media_timeout")
	case errors.Is(err, whatsmeow.ErrInvalidMediaSHA256), errors.Is(err, whatsmeow.ErrInvalidMediaEncSHA256), errors.Is(err, whatsmeow.ErrInvalidUnencryptedMediaSHA256):
		return errors.New("media_hash_mismatch")
	case errors.Is(err, whatsmeow.ErrInvalidMediaHMAC), decryptFailure(err):
		// The encrypted bytes matched their hash, so the media key does not open them.
		return errors.New("media_decrypt_failed")
	case errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith403), errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith404), errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith410):
		return errors.New("media_unavailable")
	case err.Error() == "media_size_limit":
		return errors.New("media_size_limit")
	case errors.As(err, &status) && status.Response != nil && mediaHTTPCodes[status.StatusCode] != "":
		return errors.New(mediaHTTPCodes[status.StatusCode])
	default:
		return errors.New("media_network_error")
	}
}

// At most this many messages per media_keys request.
const mediaKeysBatch = 200

// One journal lookup may hold SQLite's shared lock this long. The owning
// adapter's writer waits up to busy_timeout(5000) for it, so capture never
// fails behind the inventory; a slower lookup answers unknown instead.
var mediaKeyLookupTimeout = 200 * time.Millisecond

// serveMediaKeys answers, for an operator inventory, whether the capture journal
// holds each message's media key (ANYM-815). It reads the config without
// creating any directory, opens the journal read-only and query-only, and never
// opens the provider client, the device store or the session owner lock, so it
// can run beside the collector's adapter. One JSON request per line:
// {"id":1,"method":"media_keys","params":{"messages":[{"chat":..,"message":..}]}}
// answered {"id":1,"result":{"keys":["held"|"none"|"unknown",..]}}.
func serveMediaKeys(path string, in io.Reader, out io.Writer) error {
	journal, e := mediaKeysJournal(path)
	if e != nil {
		return e
	}
	db, e := sql.Open("sqlite3", mediaKeysDSN(journal))
	if e != nil {
		return errors.New("media_keys_unavailable")
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	return answerMediaKeys(journalKeyLookup(db), in, out)
}

// mediaKeysJournal validates the private config like loadConfig but performs no
// directory creation or permission change: a missing path is refused.
func mediaKeysJournal(path string) (string, error) {
	b, e := readPrivate(path, 1<<20)
	if e != nil {
		return "", errors.New("invalid_config")
	}
	var c Config
	if json.Unmarshal(b, &c) != nil || !regexp.MustCompile(`^[a-zA-Z0-9_.-]{1,64}$`).MatchString(c.Account) {
		return "", errors.New("invalid_config")
	}
	if !filepath.IsAbs(c.SessionDirectory) || filepath.Clean(c.SessionDirectory) != c.SessionDirectory {
		return "", errors.New("absolute_state_path_required")
	}
	journal := filepath.Join(c.SessionDirectory, "capture.sqlite")
	if info, e := os.Lstat(journal); e != nil || !info.Mode().IsRegular() {
		return "", errors.New("media_keys_unavailable")
	}
	return journal, nil
}

// Read-only and query-only: the lookup can neither create nor change the journal.
func mediaKeysDSN(journal string) string {
	u := url.URL{Scheme: "file", Path: journal}
	return u.String() + "?mode=ro&_pragma=query_only(1)&_pragma=busy_timeout(1000)"
}

// A lookup answers the newest raw row, (nil, nil) when the journal has none,
// or an error when the journal could not be read in time.
type mediaKeyLookup func(ctx context.Context, chat, id string) ([]byte, error)

func journalKeyLookup(db *sql.DB) mediaKeyLookup {
	return func(ctx context.Context, chat, id string) ([]byte, error) {
		var raw []byte
		e := db.QueryRowContext(ctx, "SELECT raw FROM capture WHERE chat=? AND message_id=? ORDER BY seq DESC LIMIT 1", chat, id).Scan(&raw)
		if errors.Is(e, sql.ErrNoRows) {
			return nil, nil
		}
		return raw, e
	}
}

func mediaKeyState(lookup mediaKeyLookup, chat, id string) string {
	if !validMessageReference(chat, id) {
		return "none"
	}
	ctx, cancel := context.WithTimeout(context.Background(), mediaKeyLookupTimeout)
	defer cancel()
	raw, e := lookup(ctx, chat, id)
	if e != nil {
		return "unknown"
	}
	if len(raw) == 0 {
		return "none"
	}
	msg, e := unmarshalMessage(raw)
	if e != nil {
		return "unknown"
	}
	meta, e := nativeMediaMetadata(msg)
	if e == nil && len(meta.Payload.GetMediaKey()) > 0 {
		return "held"
	}
	return "none"
}

func answerMediaKeys(lookup mediaKeyLookup, in io.Reader, out io.Writer) error {
	scanner := bufio.NewScanner(in)
	scanner.Buffer(make([]byte, 4096), 1<<20)
	enc := json.NewEncoder(out)
	for scanner.Scan() {
		var r struct {
			ID     int64  `json:"id"`
			Method string `json:"method"`
			Params struct {
				Messages []struct {
					Chat    string `json:"chat"`
					Message string `json:"message"`
				} `json:"messages"`
			} `json:"params"`
		}
		if json.Unmarshal(scanner.Bytes(), &r) != nil || r.ID < 1 || r.Method != "media_keys" || len(r.Params.Messages) > mediaKeysBatch {
			if e := enc.Encode(map[string]any{"id": r.ID, "error": "invalid_request"}); e != nil {
				return e
			}
			continue
		}
		keys := make([]string, len(r.Params.Messages))
		for i, m := range r.Params.Messages {
			// One journal row per statement, each bounded by mediaKeyLookupTimeout.
			keys[i] = mediaKeyState(lookup, m.Chat, m.Message)
		}
		if e := enc.Encode(map[string]any{"id": r.ID, "result": map[string]any{"keys": keys}}); e != nil {
			return e
		}
	}
	return scanner.Err()
}

func digestBytes(value []byte) string { h := sha256.Sum256(value); return hex.EncodeToString(h[:]) }
