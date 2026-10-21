package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"go.mau.fi/whatsmeow"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

type Provider struct {
	Client      *whatsmeow.Client
	Config      Config
	Capture     *CaptureStore
	ready       atomic.Bool
	startMu     sync.Mutex
	db          *sqlstore.Container
	cancel      context.CancelFunc
	workers     sync.WaitGroup
	failureOnce sync.Once
	failure     atomic.Value // first failure code (string)
	// Orders group cache refreshes across operations (ANYM-431).
	groupRefresh groupRefresher
}

// Connection failures the Python collector retries or blocks on
// (operational.CONNECTION_ERRORS). Other codes stay behind a generic one.
var transportFailures = map[string]bool{"whatsapp_logged_out": true, "whatsapp_pairing_required": true, "provider_auth_failed": true, "whatsapp_identity_mismatch": true, "provider_disconnected": true, "provider_not_ready": true, "provider_initialization_failed": true, "durable_capture_failed": true, "capture_overload": true, "spool_full": true, "disk_reserve": true}

// whatsmeow never reconnects after these events, and reconnecting from Python
// would repeat them: a temporary ban or a session taken over by another client
// needs the owner (reconnecting during a ban risks extending it), while an
// outdated client needs an upgrade. The detail stays in .transport-failure.
func connectionFailure(value any) (code, detail string) {
	switch value.(type) {
	case *events.TemporaryBan:
		return "provider_auth_failed", "whatsapp_temporary_ban"
	case *events.StreamReplaced:
		return "provider_auth_failed", "whatsapp_session_replaced"
	case *events.ClientOutdated:
		return "provider_initialization_failed", "whatsapp_client_outdated"
	}
	return "", ""
}

// failureError reports the recorded failure when Python understands it, so a
// terminal code is not replaced by a retryable generic one.
func (p *Provider) failureError(fallback string) error {
	if code, _ := p.failure.Load().(string); transportFailures[code] {
		return errors.New(code)
	}
	return errors.New(fallback)
}

// All lifecycle clients use the same durability settings, including the two
// short-lived clients used to prove pairing restoration. Decrypted buffering
// closes the ratchet-advance/crash gap before Capture.Put can persist content.
func nativeClient(device *store.Device) *whatsmeow.Client {
	client := whatsmeow.NewClient(device, nil)
	client.SynchronousAck = true
	client.EnableDecryptedEventBuffer = true
	client.ManualHistorySyncDownload = true
	client.DisableManualHistorySyncReceipt = true
	return client
}

func (p *Provider) fail(code string) { p.failDetail(code, "") }

func (p *Provider) failDetail(code, detail string) {
	p.ready.Store(false)
	p.failureOnce.Do(func() {
		p.failure.Store(code)
		value := map[string]any{"code": code, "observed_at": time.Now().UTC().Format(time.RFC3339Nano)}
		if detail != "" {
			value["detail"] = detail
		}
		_ = atomicPrivate(p.Config.EventsDirectory, ".transport-failure", mustJSON(value))
		if p.cancel != nil {
			p.cancel()
		}
	})
}
func mustJSON(v any) []byte { b, _ := json.Marshal(v); return b }
func (p *Provider) owner() types.JID {
	if p.Client != nil && p.Client.Store.ID != nil {
		return p.Client.Store.ID.ToNonAD()
	}
	return types.EmptyJID
}
func (p *Provider) identityOK() bool { return LegacyChatID(p.owner()) == p.Config.ExpectedWID }
func (p *Provider) start(ctx context.Context) (result any, err error) {
	defer func() {
		if err != nil {
			p.Close()
		}
	}()
	p.startMu.Lock()
	defer p.startMu.Unlock()
	if p.Client != nil {
		return nil, errors.New("provider_already_started")
	}
	if _, e := p.policy(); e != nil {
		return nil, e
	}
	var e error
	if e = p.prepareCapture(); e != nil {
		return nil, e
	}
	f, e := privateFile(p.Config.SessionDirectory, "auth.sqlite")
	if e != nil {
		return nil, e
	}
	f.Close()
	p.db, e = sqlstore.New(ctx, "sqlite3", sqliteDSN(filepath.Join(p.Config.SessionDirectory, "auth.sqlite")), nil)
	if e != nil {
		return nil, e
	}
	dev, e := p.db.GetFirstDevice(ctx)
	if e != nil {
		return nil, e
	}
	if dev.ID == nil {
		return nil, errors.New("whatsapp_pairing_required")
	}
	if LegacyChatID(dev.ID.ToNonAD()) != p.Config.ExpectedWID {
		return nil, errors.New("whatsapp_identity_mismatch")
	}
	store.SetOSInfo("AnyMsg", [3]uint32{0, 9, 18})
	store.DeviceProps.HistorySyncConfig.StorageQuotaMb = proto.Uint32(256)
	store.DeviceProps.HistorySyncConfig.FullSyncSizeMbLimit = proto.Uint32(32)
	store.DeviceProps.HistorySyncConfig.RecentSyncDaysLimit = proto.Uint32(7)
	store.DeviceProps.HistorySyncConfig.InitialSyncMaxMessagesPerChat = proto.Uint32(50)
	dev.EventBuffer = &nativeEventBuffer{EventBuffer: dev.EventBuffer, Capture: p.Capture}
	p.Client = nativeClient(dev)
	p.Client.UseRetryMessageStore = true
	p.Client.PreRetryCallback = func(receipt *events.Receipt, id types.MessageID, count int, msg *waE2E.Message) bool {
		return p.retryAllowed(receipt)
	}
	p.Capture.Owner = dev.ID.ToNonAD()
	p.Capture.OwnerLID = dev.GetLID()
	p.Client.EnableAutoReconnect = true
	child, cancel := context.WithCancel(ctx)
	p.cancel = cancel
	connected := make(chan struct{}, 1)
	p.Client.AddEventHandlerWithSuccessStatus(func(value any) bool {
		switch e := value.(type) {
		case *events.Connected:
			if !p.identityOK() {
				p.fail("whatsapp_identity_mismatch")
				return false
			}
			p.ready.Store(true)
			select {
			case connected <- struct{}{}:
			default:
			}
		case *events.LoggedOut:
			p.fail("whatsapp_logged_out")
			return false
		case *events.TemporaryBan, *events.StreamReplaced, *events.ClientOutdated:
			p.failDetail(connectionFailure(e))
			return false
		case *events.Disconnected:
			p.ready.Store(false)
		case *events.Message:
			if !p.identityOK() {
				p.fail("whatsapp_identity_mismatch")
				return false
			}
			if err := p.handleMessage(e); err != nil {
				p.captureFailure(err)
				return false
			}

		case *events.HistorySync:
			if !p.identityOK() {
				p.fail("whatsapp_identity_mismatch")
				return false
			}
			if err := p.captureHistory(e.Data); err != nil {
				p.captureFailure(err)
				return false
			}
		case *events.Receipt:
			if !p.identityOK() {
				p.fail("whatsapp_identity_mismatch")
				return false
			}
			policy, err := p.policy()
			if err != nil {
				p.fail(safeError(err))
				return false
			}
			if policy.Allows(LegacyChatID(e.Chat)) {
				if err = p.Capture.StoreReceipt(e); err != nil {
					p.captureFailure(err)
					return false
				}
			}
		}
		return true
	})
	p.workers.Add(2)
	go func() { defer p.workers.Done(); p.historyWorker(child) }()
	go func() { defer p.workers.Done(); p.replayWorker(child, time.Second) }()
	if e = p.Client.ConnectContext(child); e != nil {
		return nil, errors.New("provider_initialization_failed")
	}
	return p.awaitConnected(child, connected, 60*time.Second)
}

func (p *Provider) awaitConnected(ctx context.Context, connected <-chan struct{}, timeout time.Duration) (any, error) {
	select {
	case <-connected:
		return map[string]any{"ready": true}, nil
	case <-ctx.Done():
		// A failure during connect (e.g. logout or ban) cancels ctx; report it.
		return nil, p.failureError("provider_disconnected")
	case <-time.After(timeout):
		return nil, errors.New("provider_initialization_failed")
	}
}

func (p *Provider) prepareCapture() error {
	var err error
	p.Capture, err = OpenCapture(p.Config)
	if err != nil {
		return err
	}
	sweepNativeTemp(p.Config)
	return p.replayPending()
}

func spoolBackpressure(err error) bool { return err != nil && err.Error() == "spool_full" }

func (p *Provider) captureFailure(err error) {
	// A full export spool must not prevent readiness: the Python consumer only
	// starts draining after connect. Keep the session alive, return false to the
	// event handler (no ACK), and let replay publish the durable pending row.
	if !spoolBackpressure(err) {
		p.fail(safeError(err))
	}
}

func (p *Provider) replayPending() error {
	err := p.Capture.Replay()
	if spoolBackpressure(err) {
		return nil
	}
	return err
}

func (p *Provider) replayWorker(ctx context.Context, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		if _, err := p.policy(); err != nil {
			p.fail(safeError(err))
			return
		}
		if err := p.replayPending(); err != nil {
			p.fail(safeError(err))
			return
		}
	}
}
func (p *Provider) Close() {
	if p.cancel != nil {
		p.cancel()
	}
	if p.Client != nil {
		p.Client.Disconnect()
		p.Client.RemoveEventHandlers()
	}
	p.workers.Wait()
	if p.Capture != nil {
		_ = p.Capture.Close()
	}
	if p.db != nil {
		_ = p.db.Close()
	}
}

type request struct {
	ID     int64           `json:"id"`
	Method string          `json:"method"`
	Params json.RawMessage `json:"params"`
}

func (p *Provider) handle(ctx context.Context, r request, emit func(any) error) (any, error) {
	if r.Method == "start" {
		return p.start(ctx)
	}
	if !p.ready.Load() {
		// After a failure the process stays up; the next request tells Python
		// why (e.g. a replaced session must block, not reconnect).
		return nil, p.failureError("provider_not_ready")
	}
	policy, e := p.policy()
	if e != nil {
		return nil, e
	}
	switch r.Method {
	case "policy":
		return map[string]any{"updated": true}, nil
	case "dialogs":
		var v struct {
			Limit int `json:"limit"`
		}
		if json.Unmarshal(r.Params, &v) != nil {
			return nil, errors.New("invalid_request")
		}
		return p.Capture.Dialogs(v.Limit, policy)
	case "page":
		var v struct {
			Chat   string `json:"chat"`
			Limit  int    `json:"limit"`
			Before []any  `json:"before"`
		}
		if json.Unmarshal(r.Params, &v) != nil {
			return nil, errors.New("invalid_request")
		}
		if !policy.Allows(v.Chat) {
			return nil, errors.New("chat_excluded")
		}
		messages, e := p.Capture.Page(v.Chat, v.Limit, v.Before)
		if e != nil {
			return nil, e
		}
		normalized := []any{}
		for _, m := range messages {
			value, e := NormalizeMessage(m, p.owner(), p.Capture.OwnerLID)
			if e != nil {
				// A retained revision the archive cannot represent must not make
				// the whole chat unreadable; it is omitted from this page.
				continue
			}
			normalized = append(normalized, value)
		}
		for len(mustJSON(normalized)) > 900<<10 && len(normalized) > 1 {
			normalized = normalized[1:]
		}
		return map[string]any{"messages": normalized, "exhausted": false, "pending": len(messages) < v.Limit}, nil
	case "request_history":
		return p.requestHistory(ctx, r.Params, policy)
	case "send_text":
		return p.SendText(ctx, r.Params)
	case "messenger_operation":
		return p.Operation(ctx, r.Params)
	case "media":
		e := p.Media(ctx, r.Params, func(chunk []byte) error { return emit(map[string]any{"id": r.ID, "chunk": chunk}) })
		return map[string]any{"verified": true}, e
	}
	return nil, errors.New("unsupported_method")
}

var safeErrors = map[string]bool{}

func init() {
	for _, code := range []string{"provider_operation_uncertain", "operation_invalid_parameters", "operation_target_unavailable", "operation_result_too_large", "operation_query_failed", "registered_file_invalid", "link_preview_unsupported", "owner_terminal_required", "session_already_owned",
		"invite_invalid", "invite_expired", "chat_full", "join_not_allowed", "already_member", "operation_target_changed"} {
		safeErrors[code] = true
	}
}

func init() {
	for _, v := range []string{"invalid_request", "unsupported_method", "provider_not_ready", "provider_already_started", "whatsapp_logged_out", "whatsapp_pairing_required", "provider_auth_failed", "whatsapp_identity_mismatch", "provider_disconnected", "provider_initialization_failed", "durable_capture_failed", "spool_full", "disk_reserve", "capture_overload", "collector_disabled", "invalid_policy", "policy_expired", "chat_excluded", "send_expired", "sending_disabled", "send_target_invalid", "send_receipt_mismatch", "message_target_unavailable", "media_unavailable", "media_timeout", "media_network_error", "media_storage_unavailable", "media_metadata_unavailable", "media_reference_changed", "unsupported_media_type", "media_hash_mismatch", "media_size_mismatch", "media_size_limit", "invalid_media_path", "provider_response_too_large", "invalid_limit", "invalid_cursor", "history_rate_limited", "history_anchor_unavailable", "history_request_unknown", "operation_disabled", "operation_unsupported", "operation_expired", "operation_invalid", "operation_outcome_unknown", "operation_target_invalid", "invalid_phone", "identity_unresolved", "chat_not_collected", "provider_timeout", "rate_limited"} {
		safeErrors[v] = true
	}
}
func safeError(e error) string {
	if e == nil {
		return ""
	}
	if safeErrors[e.Error()] {
		return e.Error()
	}
	return "provider_operation_failed"
}
func runLane(ctx context.Context, p *Provider, in io.Reader, out io.Writer, allowed map[string]bool) error {
	scanner := bufio.NewScanner(in)
	scanner.Buffer(make([]byte, 4096), 1<<20)
	enc := json.NewEncoder(out)
	emit := func(v any) error {
		b, e := json.Marshal(v)
		if e != nil {
			return e
		}
		if len(b)+1 > 1<<20 {
			return errors.New("provider_response_too_large")
		}
		return enc.Encode(v)
	}
	for scanner.Scan() {
		if err := ctx.Err(); err != nil {
			return err
		}
		var r request
		if json.Unmarshal(scanner.Bytes(), &r) != nil || r.ID < 1 || r.ID > 9007199254740991 || !allowed[r.Method] {
			if e := emit(map[string]any{"id": r.ID, "error": "provider_operation_failed"}); e != nil {
				return e
			}
			continue
		}
		result, e := p.handle(ctx, r, emit)
		if e != nil {
			if e = emit(map[string]any{"id": r.ID, "error": safeError(e)}); e != nil {
				return e
			}
		} else if e = emit(map[string]any{"id": r.ID, "result": result}); e != nil {
			// An oversized result was never written, so the pipe is still
			// synchronized. Answer a read (e.g. a large dialogs catalog) with an
			// error frame, as the Node lane does, instead of ending the native
			// process. A mutation keeps the lane-exit unknown outcome.
			if e.Error() != "provider_response_too_large" || r.Method == "send_text" || r.Method == "messenger_operation" {
				return e
			}
			if e = emit(map[string]any{"id": r.ID, "error": "provider_response_too_large"}); e != nil {
				return e
			}
		}
	}
	return scanner.Err()
}
func sendingFile() (*os.File, error) {
	n, e := strconv.Atoi(os.Getenv("ANYMSG_WHATSAPP_SEND_FD"))
	if e != nil || n < 3 {
		return nil, errors.New("private_sending_channel_required")
	}
	var info syscall.Stat_t
	if e = syscall.Fstat(n, &info); e != nil || info.Mode&syscall.S_IFMT != syscall.S_IFSOCK {
		return nil, errors.New("private_sending_channel_required")
	}
	return inheritedIPCFile(n, "sending")
}

func inheritedIPCFile(fd int, name string) (*os.File, error) {
	// Python hands off blocking pipes/sockets. NewFile only registers inherited
	// descriptors with Go's runtime poller if O_NONBLOCK is already set. Without
	// it Close waits for an idle Read/Write forever while the peer stays open,
	// so SIGTERM cannot drain the lanes. Set the flag before creating the File.
	if err := syscall.SetNonblock(fd, true); err != nil {
		return nil, err
	}
	return os.NewFile(uintptr(fd), name), nil
}

func (p *Provider) handleMessage(e *events.Message) error {
	policy, err := p.policy()
	if err != nil {
		return err
	}
	unwrapped := unwrap(e)
	if unwrapped.Message.GetProtocolMessage().GetHistorySyncNotification() != nil {
		if err = p.Capture.EnqueueHistory(unwrapped); skippableCapture(err) {
			return p.Capture.skip(err)
		}
		return err
	}
	if !policy.Allows(LegacyChatID(e.Info.Chat)) {
		return nil
	}
	return p.Capture.putOrSkip(e, "live")
}

func (p *Provider) retryAllowed(receipt *events.Receipt) bool {
	if receipt == nil || !p.ready.Load() || !p.identityOK() {
		return false
	}
	policy, err := p.policy()
	return err == nil && policy.SendEnabled && policy.Allows(LegacyChatID(receipt.Chat))
}
