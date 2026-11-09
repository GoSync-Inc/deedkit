package main

// ANYM-937: serve mode. `anymsg-whatsmeow --serve <socket-dir> --config <path>` is the account's session holder: it
// owns the session directory, the ANYM-914 claim and the provider connection, and the Python collector attaches over
// two private unix sockets (the control and send lanes of child mode) instead of being the adapter's parent. Child
// mode (main.go execute) is unchanged.

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

// The holder's protocol range. Rule (COLLECTORS.md): a holder supports at least the previous collector protocol.
const holderProtocol = 1

var holderSupported = [2]int{1, 1}

// sourceSHA is set at build time (-ldflags "-X main.sourceSHA=<sha>"); the hello and --version report it.
var sourceSHA = "unknown"

const (
	controlSocket        = "control.sock"
	sendSocket           = "send.sock"
	holderTokenFile      = "token"
	holderGenerationFile = ".holder-generation"
	holderTerminalFile   = ".holder-terminal"
	handshakeTimeout     = 10 * time.Second
	frameWriteTimeout    = 30 * time.Second
	policyHorizon        = 45 * time.Second
	holderTick           = 250 * time.Millisecond
	maxFrame             = 1 << 20
)

var controlMethods = []string{"start", "policy", "dialogs", "page", "media", "request_history", "gap_repair", "gap_repair_status"}
var sendMethods = []string{"send_text", "messenger_operation"}

// Session-terminal errors (runtime.go transportFailures): the holder stays up and idle with no further connect until a
// pairing or a hand `deedkit admin` action replaces the session (criterion 6).
var sessionTerminal = map[string]bool{"whatsapp_logged_out": true, "whatsapp_pairing_required": true, "provider_auth_failed": true, "whatsapp_identity_mismatch": true}

func init() {
	for _, code := range []string{"collector_lease_invalid", "collector_lease_unverified", "holder_busy", "holder_not_attached",
		"holder_protocol_unsupported", "collector_protocol_unsupported", "holder_token_invalid", "holder_peer_refused",
		"session_claim_lost", "session_claimed_elsewhere", "session_claim_restore_hold", "holder_dsn_required"} {
		safeErrors[code] = true
	}
}

func holderLog(event string, fields map[string]any) {
	value := map[string]any{"event": event, "observed_at": time.Now().UTC().Format(time.RFC3339Nano)}
	for k, v := range fields {
		value[k] = v
	}
	_ = json.NewEncoder(os.Stderr).Encode(value)
}

type holderPolicy struct {
	policy  Policy
	version int64
	expires time.Time
}

type leaseRef struct {
	Owner string `json:"owner"`
	Epoch int64  `json:"epoch"`
}

type attachment struct {
	lease         leaseRef
	verifiedUntil time.Time
	control       *frameConn
	send          *frameConn
	attachedAt    time.Time
	closed        chan struct{}
	once          sync.Once
	// dead is set (under holder.mu) the moment the attachment ends; inflight counts the calls admitted before that.
	// A successor attaches only after every admitted call of its predecessor returned (finding 2).
	dead     bool
	inflight sync.WaitGroup
}

type frameConn struct {
	conn *net.UnixConn
	mu   sync.Mutex
}

func (f *frameConn) write(v any) error {
	b, e := json.Marshal(v)
	if e != nil {
		return e
	}
	if len(b)+1 > maxFrame {
		return errors.New("provider_response_too_large")
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	_ = f.conn.SetWriteDeadline(time.Now().Add(frameWriteTimeout))
	_, e = f.conn.Write(append(b, '\n'))
	return e
}

type holder struct {
	cfg       Config
	socketDir string
	token     string
	db        *sql.DB
	ctx       context.Context
	now       func() time.Time
	// leaseCheck is leaseCheck against db; tests replace it.
	checkLease func(ctx context.Context, owner string, epoch int64) (leaseState, error)
	// newProvider builds a provider connection; tests replace it with a fake whose start never reaches WhatsApp.
	newProvider func(h *holder) providerConn
	supported   [2]int

	mu        sync.Mutex
	att       *attachment
	pol       *holderPolicy
	terminal  string
	prov      providerConn
	provGen   int64
	provSince time.Time

	// Handlers hold provUse for reading while they use the provider; start and close hold it for writing, so a store
	// is never closed beneath a live capture, send or operation.
	provUse sync.RWMutex
	// gate serializes policy updates with capture writes (CaptureStore.Fence): a write holds it for reading
	// across its policy check and the write itself, an update holds it for writing.
	gate sync.RWMutex
	// starting cancels a provider start under way; provCancel ends the published provider's context.
	starting   context.CancelFunc
	provCancel context.CancelFunc

	jmu      sync.Mutex
	inflight map[string]chan struct{}

	listeners []*net.UnixListener
	connected atomic.Bool
}

// providerConn is the part of *Provider the holder drives.
type providerConn interface {
	start(ctx context.Context) (any, error)
	handle(ctx context.Context, r request, emit func(any) error) (any, error)
	isReady() bool
	failureCode() string
	disconnectNow()
	Close()
}

func (p *Provider) isReady() bool { return p.ready.Load() }
func (p *Provider) failureCode() string {
	code, _ := p.failure.Load().(string)
	return code
}
func (p *Provider) disconnectNow() {
	p.ready.Store(false)
	if p.cancel != nil {
		p.cancel()
	}
	if p.Client != nil {
		p.Client.Disconnect()
	}
}

func newHolder(ctx context.Context, cfg Config, socketDir string, db *sql.DB) *holder {
	h := &holder{cfg: cfg, socketDir: socketDir, db: db, ctx: ctx, now: time.Now, supported: holderSupported,
		inflight: map[string]chan struct{}{}}
	h.checkLease = func(ctx context.Context, owner string, epoch int64) (leaseState, error) {
		return leaseCheck(ctx, db, cfg.Account, owner, epoch)
	}
	h.newProvider = func(h *holder) providerConn {
		return &Provider{Config: h.cfg, policySource: h.policyNow, captureFence: h.fence, serveMode: true}
	}
	return h
}

// policyNow is the provider's policy in serve mode: only what the attached collector sent, only until it expires.
func (h *holder) policyNow() (Policy, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.pol == nil {
		return Policy{}, errors.New("invalid_policy")
	}
	if !h.now().Before(h.pol.expires) {
		return Policy{}, errors.New("policy_expired")
	}
	if !h.pol.policy.Enabled {
		return Policy{}, errors.New("collector_disabled")
	}
	p := h.pol.policy
	p.Version = h.pol.version
	return p, nil
}

type policyFrame struct {
	Policy
	Version   int64   `json:"version"`
	ExpiresAt float64 `json:"expires_at"`
}

// fence is CaptureStore.Fence in serve mode: a write proceeds only under a fresh policy that allows its chat, and a
// policy update waits for it (finding 1). An excluded chat's event is discarded before any write.
func (h *holder) fence(chat string) (func(), error) {
	h.gate.RLock()
	p, e := h.policyNow()
	if e == nil && chat != "" && !p.Allows(chat) {
		e = errFenceExcluded
	}
	if e != nil {
		h.gate.RUnlock()
		return nil, e
	}
	return h.gate.RUnlock, nil
}

func (h *holder) applyPolicy(raw json.RawMessage) error { return h.applyPolicyFor(nil, raw) }

// applyPolicyFor keeps the newest version; `expires_at` is bounded to 45 s ahead whatever the collector sent. With
// an attachment, the policy is applied only while that attachment is the current one, in the same critical section.
func (h *holder) applyPolicyFor(att *attachment, raw json.RawMessage) error {
	var f policyFrame
	if decodeStrict(raw, &f) != nil || f.Version < 1 || f.ExpiresAt <= 0 || f.IncludeChats == nil || f.ExcludeChats == nil || f.OperationsEnabled == nil {
		return errors.New("invalid_policy")
	}
	now := h.now()
	expires := time.Unix(0, int64(f.ExpiresAt*1e9))
	if limit := now.Add(policyHorizon); expires.After(limit) {
		expires = limit
	}
	h.gate.Lock()
	defer h.gate.Unlock()
	h.mu.Lock()
	defer h.mu.Unlock()
	if att != nil && (h.att != att || att.dead) {
		return errors.New("collector_lease_invalid")
	}
	if h.pol != nil && f.Version < h.pol.version {
		return nil // never back to an older version after a newer one arrived
	}
	h.pol = &holderPolicy{policy: f.Policy, version: f.Version, expires: expires}
	return nil
}

func (h *holder) methods() []string {
	return append(append([]string{}, controlMethods...), sendMethods...)
}

// verifyLease asks the database and turns its answer into a deadline counted from when the question was asked, so
// a slow answer never extends a lease, and an answer that arrives after that deadline is "not live" (finding 3).
// It updates the attachment's last verified deadline; an error is "unknown".
func (h *holder) verifyLease(att *attachment, lease leaseRef) (bool, time.Time, error) {
	asked := h.now()
	state, e := h.checkLease(h.ctx, lease.Owner, lease.Epoch)
	if e != nil {
		return false, time.Time{}, e
	}
	deadline := asked.Add(state.remaining)
	valid := state.valid && h.now().Before(deadline)
	if valid && att != nil {
		h.mu.Lock()
		att.verifiedUntil = deadline
		h.mu.Unlock()
	}
	return valid, deadline, nil
}

// admit registers a call on the attachment while it is the current one; a call that waited (for the database or a
// lock) is admitted only after that wait, so a detached predecessor never reaches the provider (finding 2).
func (h *holder) admit(att *attachment) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.att != att || att.dead || !h.now().Before(att.verifiedUntil) {
		return false
	}
	att.inflight.Add(1)
	return true
}

func (h *holder) dispatch(att *attachment, lane string, r request, emit func(any) error) (any, error) {
	if lane == "send" {
		// Before every send-lane method: a positive "not this lease" refuses and detaches; an unanswered check
		// keeps the last verified lease until it ends.
		if valid, _, e := h.verifyLease(att, att.lease); e == nil && !valid {
			return nil, errLeaseRefused // answer detaches after the reply
		}
	}
	if !h.admit(att) {
		return nil, errLeaseRefused
	}
	defer att.inflight.Done()
	if r.Method == "start" {
		return h.start()
	}
	h.provUse.RLock()
	defer h.provUse.RUnlock()
	h.mu.Lock()
	p, terminal, dead := h.prov, h.terminal, att.dead
	h.mu.Unlock()
	if dead {
		return nil, errLeaseRefused
	}
	if p == nil {
		if terminal != "" {
			return nil, errors.New(terminal)
		}
		// No connection: the collector's reconnect path calls `start` (serve mode never reconnects by itself).
		return nil, errors.New("provider_disconnected")
	}
	if r.Method == "messenger_operation" {
		return h.journaledOperation(p, r)
	}
	return p.handle(h.ctx, r, emit)
}

// start connects once: an attached collector's `start` returns the running connection when there is one (a collector
// restart within the policy's lifetime keeps it), else makes one connection under a fresh policy.
func (h *holder) start() (any, error) {
	h.provUse.Lock()
	defer h.provUse.Unlock()
	h.mu.Lock()
	p, terminal := h.prov, h.terminal
	h.mu.Unlock()
	if p != nil {
		if p.isReady() && p.failureCode() == "" {
			return map[string]any{"ready": true, "generation": h.generation()}, nil
		}
		// Serve mode has no automatic reconnect: a connection that is not ready has ended.
		h.closeLocked(p, "provider")
		h.mu.Lock()
		terminal = h.terminal
		h.mu.Unlock()
	}
	if terminal != "" {
		return nil, errors.New(terminal)
	}
	if _, e := h.policyNow(); e != nil {
		return nil, e
	}
	_ = os.Remove(filepath.Join(h.cfg.EventsDirectory, ".transport-failure"))
	p = h.newProvider(h)
	// The start is tracked (finding 4): a policy expiry or a shutdown cancels it, and shutdown waits for it on provUse.
	ctx, cancel := context.WithCancel(h.ctx)
	h.mu.Lock()
	h.starting = cancel
	h.mu.Unlock()
	_, e := p.start(ctx)
	h.mu.Lock()
	h.starting = nil
	h.mu.Unlock()
	if e == nil {
		if _, pe := h.policyNow(); pe != nil || ctx.Err() != nil {
			// The policy expired or the holder is stopping while it connected: disconnect, publish nothing.
			p.disconnectNow()
			p.Close()
			e = pe
			if e == nil {
				e = errors.New("provider_disconnected")
			}
		}
	} else if code := safeError(e); sessionTerminal[code] {
		h.setTerminal(code)
	}
	if e != nil {
		cancel()
		return nil, e
	}
	generation, e := h.nextGeneration()
	if e != nil {
		p.disconnectNow()
		p.Close()
		cancel()
		return nil, e
	}
	h.mu.Lock()
	h.prov, h.provGen, h.provSince, h.provCancel = p, generation, h.now(), cancel
	h.mu.Unlock()
	holderLog("holder.provider_connected", map[string]any{"account": h.cfg.Account, "generation": generation})
	return map[string]any{"ready": true, "generation": generation}, nil
}

func (h *holder) generation() int64 {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.provGen
}

// closeProvider ends the provider connection: the WhatsApp disconnect happens at once, the stores close after the
// in-flight handlers return.
func (h *holder) closeProvider(reason string) {
	h.mu.Lock()
	p, starting := h.prov, h.starting
	h.mu.Unlock()
	if starting != nil {
		starting()
	}
	if p != nil {
		p.disconnectNow()
	}
	// Always taken, also with no published provider: a start under way holds it until its own teardown ended.
	h.provUse.Lock()
	defer h.provUse.Unlock()
	h.mu.Lock()
	p = h.prov
	h.mu.Unlock()
	if p != nil {
		h.closeLocked(p, reason)
	}
}

func (h *holder) closeLocked(p providerConn, reason string) {
	h.mu.Lock()
	if h.prov != p {
		h.mu.Unlock()
		return
	}
	h.prov = nil
	generation, since, cancel := h.provGen, h.provSince, h.provCancel
	h.provCancel = nil
	h.mu.Unlock()
	if cancel != nil {
		defer cancel()
	}
	code := p.failureCode()
	p.disconnectNow()
	p.Close()
	if sessionTerminal[code] {
		h.setTerminal(code)
	}
	fields := map[string]any{"account": h.cfg.Account, "generation": generation, "reason": reason,
		"uptime_s": int64(h.now().Sub(since).Seconds())}
	if code != "" {
		fields["code"] = safeError(errors.New(code))
	}
	holderLog("holder.provider_disconnected", fields)
}

// watch disconnects at once when the policy expires (no capture under an expired policy), closes a failed provider
// and detaches an attachment whose last verified lease ended.
func (h *holder) watch(ctx context.Context) {
	t := time.NewTicker(holderTick)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
		h.watchOnce()
	}
}

func (h *holder) watchOnce() {
	h.mu.Lock()
	p, att, starting := h.prov, h.att, h.starting
	expired := h.pol == nil || !h.now().Before(h.pol.expires)
	leaseOver := att != nil && !h.now().Before(att.verifiedUntil)
	h.mu.Unlock()
	if leaseOver {
		h.detach(att, "collector_lease_invalid")
	}
	if expired && starting != nil {
		starting() // no connection may complete under an expired policy
	}
	if p == nil {
		return
	}
	if expired {
		h.closeProvider("policy_expired")
	} else if p.failureCode() != "" || !p.isReady() {
		// A failure or a plain disconnect ends the connection; only an attached `start` makes the next one.
		h.closeProvider("provider")
	}
}

func (h *holder) nextGeneration() (int64, error) {
	var current int64
	if raw, e := readPrivate(filepath.Join(h.cfg.SessionDirectory, holderGenerationFile), 64); e == nil {
		current, _ = strconv.ParseInt(strings.TrimSpace(string(raw)), 10, 64)
	} else if !os.IsNotExist(e) {
		return 0, errors.New("private_regular_file_required")
	}
	current++
	return current, atomicPrivate(h.cfg.SessionDirectory, holderGenerationFile, []byte(strconv.FormatInt(current, 10)+"\n"))
}

// sessionFingerprint names the session store's current content; a pairing or an admin replacement changes it.
func sessionFingerprint(session string) string {
	info, e := os.Lstat(filepath.Join(session, "auth.sqlite"))
	if e != nil {
		return "absent"
	}
	st, _ := info.Sys().(*syscall.Stat_t)
	if st == nil {
		return "unknown"
	}
	return strconv.FormatUint(uint64(st.Ino), 10) + ":" + strconv.FormatInt(info.Size(), 10) + ":" + strconv.FormatInt(info.ModTime().UnixNano(), 10)
}

func (h *holder) setTerminal(code string) {
	h.mu.Lock()
	first := h.terminal == ""
	h.terminal = code
	h.mu.Unlock()
	_ = atomicPrivate(h.cfg.SessionDirectory, holderTerminalFile, mustJSON(map[string]any{"code": code,
		"session": sessionFingerprint(h.cfg.SessionDirectory), "observed_at": time.Now().UTC().Format(time.RFC3339Nano)}))
	if first {
		holderLog("holder.session_terminal", map[string]any{"account": h.cfg.Account, "code": code})
	}
}

// loadTerminal keeps a recorded session-terminal state across holder restarts while the session store is the same.
func (h *holder) loadTerminal() {
	raw, e := readPrivate(filepath.Join(h.cfg.SessionDirectory, holderTerminalFile), 4096)
	if e != nil {
		return
	}
	var v struct {
		Code    string `json:"code"`
		Session string `json:"session"`
	}
	if json.Unmarshal(raw, &v) != nil || !sessionTerminal[v.Code] || v.Session != sessionFingerprint(h.cfg.SessionDirectory) {
		_ = os.Remove(filepath.Join(h.cfg.SessionDirectory, holderTerminalFile))
		return
	}
	h.terminal = v.Code
	holderLog("holder.session_terminal", map[string]any{"account": h.cfg.Account, "code": v.Code})
}

// shutdown stops accepting, detaches and disconnects the provider; the caller releases the claim after it.
func (h *holder) shutdown() {
	h.closeListeners()
	h.mu.Lock()
	att := h.att
	h.mu.Unlock()
	if att != nil {
		h.detach(att, "holder_stop")
	}
	h.closeProvider("holder_restart")
}

func validSocketDirectory(c Config, socketDir string) error {
	if !filepath.IsAbs(socketDir) || filepath.Clean(socketDir) != socketDir {
		return errors.New("absolute_state_path_required")
	}
	for _, d := range []string{c.SessionDirectory, c.EventsDirectory, c.PagesDirectory} {
		if d == socketDir || strings.HasPrefix(d, socketDir+string(os.PathSeparator)) || strings.HasPrefix(socketDir, d+string(os.PathSeparator)) {
			return errors.New("collector_directories_overlap")
		}
	}
	return privateRoot(socketDir)
}

// waitOwned retries a lock another process holds with the claim's backoff; any other error ends the holder.
func waitOwned[T any](ctx context.Context, b *backoff, account, what string, open func() (T, error)) (T, error) {
	for {
		v, e := open()
		if e == nil || e.Error() != "session_already_owned" {
			return v, e
		}
		if e = b.wait(ctx, "holder.lock_waiting", map[string]any{"account": account, "lock": what}); e != nil {
			var zero T
			return zero, e
		}
	}
}

func serve(path, socketDir string) error {
	c, e := loadConfig(path)
	if e != nil {
		return e
	}
	if e = validSocketDirectory(c, socketDir); e != nil {
		return e
	}
	db, e := openClaimDatabase(c.HolderDSNFile)
	if e != nil {
		return e
	}
	defer db.Close()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	b := newBackoff()
	lock, e := waitOwned(ctx, b, c.Account, "host", func() (*hostLock, error) {
		return openHostLock(c.Account, c.SessionLockDirectory, c.BootIDPath)
	})
	if e != nil {
		return quiet(ctx, e)
	}
	defer lock.Close()
	session, e := waitOwned(ctx, b, c.Account, "session", func() (*os.File, error) { return sessionDirectoryLock(c.SessionDirectory) })
	if e != nil {
		return quiet(ctx, e)
	}
	defer session.Close()
	owner, e := waitOwned(ctx, b, c.Account, "native-owner", func() (*os.File, error) { return acquire(c.SessionDirectory) })
	if e != nil {
		return quiet(ctx, e)
	}
	defer owner.Close()
	claim, e := claimAcquireWaiting(ctx, db, lock, b)
	if e != nil {
		return quiet(ctx, e)
	}
	holderLog("holder.claimed", map[string]any{"account": c.Account, "claim_generation": claim.generation})
	run, cancel := context.WithCancel(ctx)
	defer cancel()
	h := newHolder(run, c, socketDir, db)
	h.loadTerminal()
	if e = h.listen(); e != nil {
		claimRelease(db, claim)
		return e
	}
	lost := make(chan error, 1)
	go func() { lost <- claimKeep(run, db, claim, claimRefreshEvery) }()
	go h.watch(run)
	select {
	case <-ctx.Done():
		e = nil
	case e = <-lost:
		if ctx.Err() != nil {
			e = nil
		}
	}
	cancel()
	h.shutdown()
	if e == nil || e.Error() != "session_claim_lost" {
		claimRelease(db, claim)
	}
	return e
}

// quiet turns a stop signal while waiting into a clean exit.
func quiet(ctx context.Context, e error) error {
	if ctx.Err() != nil {
		return nil
	}
	return e
}
