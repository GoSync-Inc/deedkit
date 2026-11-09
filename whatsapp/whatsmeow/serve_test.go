package main

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/types"
)

// A fake provider connection: it never reaches WhatsApp and counts every call.
type fakeProvider struct {
	world *fakeWorld
	ready atomic.Bool
	fail  atomic.Value
}

type fakeWorld struct {
	mu          sync.Mutex
	starts      int
	closes      int
	disconnects int
	operations  int
	startErr    error
	opErr       error
	opBlock     chan struct{}
	opEntered   chan struct{}
	onOperation func()
	last        *fakeProvider
	// startGate, when set, holds start until it is closed (or, with startOnCtx, until the context ends).
	startGate     chan struct{}
	startOnCtx    bool
	startReturned bool
	// readOnCtx: a control-lane media read runs until its context ends (a long download).
	readOnCtx     bool
	readsEntered  int
	readCancelled bool
}

func (f *fakeProvider) start(ctx context.Context) (any, error) {
	f.world.mu.Lock()
	f.world.starts++
	err, gate, onCtx := f.world.startErr, f.world.startGate, f.world.startOnCtx
	f.world.last = f
	f.world.mu.Unlock()
	defer f.world.set(func(w *fakeWorld) { w.startReturned = true })
	if gate != nil {
		if onCtx {
			select {
			case <-gate:
			case <-ctx.Done():
				return nil, errors.New("provider_initialization_failed")
			}
		} else {
			<-gate
		}
	}
	if err != nil {
		return nil, err
	}
	f.ready.Store(true)
	return map[string]any{"ready": true}, nil
}
func (f *fakeProvider) handle(ctx context.Context, r request, emit func(any) error) (any, error) {
	f.world.mu.Lock()
	onCtx := f.world.readOnCtx && r.Method == "media"
	f.world.mu.Unlock()
	if onCtx {
		f.world.set(func(w *fakeWorld) { w.readsEntered++ })
		<-ctx.Done()
		f.world.set(func(w *fakeWorld) { w.readCancelled = true })
		return nil, ctx.Err()
	}
	if r.Method != "messenger_operation" {
		return map[string]any{"method": r.Method}, nil
	}
	f.world.mu.Lock()
	f.world.operations++
	block, entered, hook, err := f.world.opBlock, f.world.opEntered, f.world.onOperation, f.world.opErr
	f.world.mu.Unlock()
	if hook != nil {
		hook()
	}
	if entered != nil {
		entered <- struct{}{}
	}
	if block != nil {
		<-block
	}
	if err != nil {
		return nil, err
	}
	var params map[string]any
	_ = json.Unmarshal(r.Params, &params)
	return map[string]any{"outcome": "succeeded", "operation": params["operation"]}, nil
}
func (f *fakeProvider) isReady() bool { return f.ready.Load() }
func (f *fakeProvider) failureCode() string {
	code, _ := f.fail.Load().(string)
	return code
}
func (f *fakeProvider) disconnectNow() {
	if !f.ready.Swap(false) {
		return // already down: no second disconnect from WhatsApp
	}
	f.world.mu.Lock()
	f.world.disconnects++
	f.world.mu.Unlock()
}
func (f *fakeProvider) Close() {
	f.world.mu.Lock()
	f.world.closes++
	f.world.mu.Unlock()
}

func (w *fakeWorld) count(field *int) int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return *field
}

type fakeClock struct {
	mu sync.Mutex
	at time.Time
}

func (c *fakeClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.at
}
func (c *fakeClock) advance(d time.Duration) {
	c.mu.Lock()
	c.at = c.at.Add(d)
	c.mu.Unlock()
}

// fakeLeases is collector_leases as the holder's login sees it: one live (owner, epoch) or none.
type fakeLeases struct {
	mu        sync.Mutex
	owner     string
	epoch     int64
	remaining time.Duration
	down      bool
	checks    int
	after     func(owner string)
}

func (l *fakeLeases) set(owner string, epoch int64) {
	l.mu.Lock()
	l.owner, l.epoch = owner, epoch
	l.mu.Unlock()
}
func (l *fakeLeases) setDown(down bool) {
	l.mu.Lock()
	l.down = down
	l.mu.Unlock()
}
func (l *fakeLeases) count() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.checks
}
func (w *fakeWorld) set(f func(w *fakeWorld)) {
	w.mu.Lock()
	f(w)
	w.mu.Unlock()
}
func (x *harness) state() (att *attachment, prov providerConn, terminal string) {
	x.h.mu.Lock()
	defer x.h.mu.Unlock()
	return x.h.att, x.h.prov, x.h.terminal
}
func sha256Sum(b []byte) [32]byte { return sha256.Sum256(b) }
func (l *fakeLeases) check(ctx context.Context, owner string, epoch int64) (leaseState, error) {
	state, err, hook := l.answer(owner, epoch)
	if hook != nil {
		hook(owner) // runs after the database answered: a slow or delayed reply
	}
	return state, err
}
func (l *fakeLeases) answer(owner string, epoch int64) (leaseState, error, func(string)) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.checks++
	if l.down {
		return leaseState{}, errors.New("database unreachable"), l.after
	}
	if owner != l.owner || epoch != l.epoch {
		return leaseState{}, nil, l.after
	}
	return leaseState{valid: true, remaining: l.remaining}, nil, l.after
}
func (l *fakeLeases) setAfter(hook func(string)) {
	l.mu.Lock()
	l.after = hook
	l.mu.Unlock()
}

type harness struct {
	h      *holder
	cancel context.CancelFunc
	world  *fakeWorld
	clock  *fakeClock
	leases *fakeLeases
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	c := testConfig(t)
	// Short: a unix socket path has a 104-byte limit on macOS.
	sockets, e := os.MkdirTemp("", "hs")
	if e != nil {
		t.Fatal(e)
	}
	_ = os.Chmod(sockets, 0700)
	ctx, cancel := context.WithCancel(context.Background())
	x := &harness{world: &fakeWorld{}, clock: &fakeClock{at: time.Unix(1_800_000_000, 0)},
		leases: &fakeLeases{owner: "c1", epoch: 1, remaining: 60 * time.Second}}
	x.cancel = cancel
	x.h = newHolder(ctx, c, sockets, nil)
	x.h.now = x.clock.now
	x.h.checkLease = x.leases.check
	x.h.newProvider = func(*holder) providerConn { return &fakeProvider{world: x.world} }
	if e = x.h.listen(); e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() {
		x.h.shutdown()
		cancel()
		os.RemoveAll(sockets)
	})
	return x
}

type lane struct {
	conn   *net.UnixConn
	reader *bufio.Reader
	hello  map[string]any
	reply  map[string]any
	next   int64
}

func (x *harness) dial(t *testing.T, name string, lease leaseRef, supported [2]int, token string) *lane {
	t.Helper()
	file := controlSocket
	if name == "send" {
		file = sendSocket
	}
	conn, e := net.DialUnix("unix", nil, &net.UnixAddr{Name: filepath.Join(x.h.socketDir, file), Net: "unix"})
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { conn.Close() })
	_ = conn.SetDeadline(time.Now().Add(10 * time.Second))
	l := &lane{conn: conn, reader: bufio.NewReader(conn)}
	l.hello = l.read(t)
	frame := map[string]any{"hello": map[string]any{"protocol": supported[1], "supported": supported}, "token": token, "lane": name, "lease": lease}
	l.write(t, frame)
	l.reply = l.read(t)
	return l
}

func (x *harness) attach(t *testing.T, name string, lease leaseRef) *lane {
	t.Helper()
	l := x.dial(t, name, lease, [2]int{1, 1}, x.h.token)
	if l.reply["attached"] == nil {
		t.Fatalf("%s lane not attached: %v", name, l.reply)
	}
	return l
}

func (l *lane) write(t *testing.T, v any) {
	t.Helper()
	b, _ := json.Marshal(v)
	if _, e := l.conn.Write(append(b, '\n')); e != nil {
		t.Fatal(e)
	}
}
func (l *lane) read(t *testing.T) map[string]any {
	t.Helper()
	line, e := l.reader.ReadBytes('\n')
	if e != nil {
		t.Fatalf("read: %v", e)
	}
	var v map[string]any
	if json.Unmarshal(line, &v) != nil {
		t.Fatalf("frame %q", line)
	}
	return v
}
func (l *lane) call(t *testing.T, method string, params any) map[string]any {
	t.Helper()
	l.next++
	l.write(t, map[string]any{"id": l.next, "method": method, "params": params})
	return l.read(t)
}
func (l *lane) closed(t *testing.T) bool {
	t.Helper()
	_ = l.conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	_, e := l.reader.ReadBytes('\n')
	return e != nil
}

func policyWith(version int64, expires time.Time, exclude ...string) map[string]any {
	if exclude == nil {
		exclude = []string{}
	}
	return map[string]any{"version": version, "expires_at": float64(expires.UnixNano()) / 1e9, "enabled": true,
		"send_enabled": true, "operations_enabled": []string{}, "discover_chats": true, "include_chats": []string{}, "exclude_chats": exclude}
}

func (x *harness) keepalive(t *testing.T, l *lane, lease leaseRef, policy map[string]any) {
	t.Helper()
	before := x.leases.count()
	l.write(t, map[string]any{"keepalive": map[string]any{"lease": lease, "policy": policy}})
	eventually(t, func() bool { return x.leases.count() > before })
	if policy != nil {
		eventually(t, func() bool {
			p, e := x.h.policyNow()
			return e == nil && p.Version >= policy["version"].(int64)
		})
	}
}

func eventually(t *testing.T, ok func() bool) {
	t.Helper()
	for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline); time.Sleep(5 * time.Millisecond) {
		if ok() {
			return
		}
	}
	t.Fatal("condition not reached")
}

var c1 = leaseRef{Owner: "c1", Epoch: 1}

// spec: ANYM-937
func TestHolderNegotiatesTheProtocolRange(t *testing.T) {
	for _, tc := range []struct {
		holder, collector [2]int
		protocol          int
		code              string
	}{
		{[2]int{1, 2}, [2]int{1, 2}, 2, ""},                               // N/N
		{[2]int{1, 2}, [2]int{1, 1}, 1, ""},                               // holder N, collector N-1
		{[2]int{1, 1}, [2]int{1, 2}, 1, ""},                               // holder N-1, collector N
		{[2]int{2, 3}, [2]int{1, 1}, 0, "collector_protocol_unsupported"}, // collector N-2
		{[2]int{1, 1}, [2]int{3, 3}, 0, "holder_protocol_unsupported"},    // holder N-2
		{[2]int{1, 1}, [2]int{0, 0}, 0, "collector_protocol_unsupported"},
	} {
		protocol, code := negotiate(tc.holder, tc.collector)
		if protocol != tc.protocol || code != tc.code {
			t.Fatalf("%v/%v: %d %q", tc.holder, tc.collector, protocol, code)
		}
	}
	// Over the socket: the holder's hello lists its range and methods, and an N-2 collector is refused before any
	// method runs.
	x := newHarness(t)
	x.h.mu.Lock()
	x.h.supported = [2]int{2, 3}
	x.h.mu.Unlock()
	l := x.dial(t, "control", c1, [2]int{1, 1}, x.h.token)
	hello, _ := l.hello["hello"].(map[string]any)
	if hello == nil || hello["account"] != "test" || hello["holder_source_sha"] == nil || len(hello["methods"].([]any)) != 10 {
		t.Fatal(l.hello)
	}
	if l.reply["error"] != "collector_protocol_unsupported" || !l.closed(t) {
		t.Fatal(l.reply)
	}
	if x.world.count(&x.world.starts) != 0 || x.leases.count() != 0 {
		t.Fatal("refused collector reached the lease check or the provider")
	}
}

// spec: ANYM-937
func TestHolderRefusesAWrongTokenBeforeAnyMethod(t *testing.T) {
	x := newHarness(t)
	last := "0"
	if x.h.token[63] == '0' {
		last = "1"
	}
	for _, token := range []string{"", "00", x.h.token[:63] + last} {
		l := x.dial(t, "control", c1, [2]int{1, 1}, token)
		if l.reply["error"] != "holder_token_invalid" || !l.closed(t) {
			t.Fatal(token, l.reply)
		}
	}
	if x.leases.count() != 0 || x.world.count(&x.world.starts) != 0 {
		t.Fatal("unauthenticated connection reached the lease check")
	}
}

// spec: ANYM-937
func TestHolderSocketsAreOwnerOnlyAndOnlyAStaleSocketIsReplaced(t *testing.T) {
	x := newHarness(t)
	for _, name := range []string{controlSocket, sendSocket, holderTokenFile} {
		info, e := os.Lstat(filepath.Join(x.h.socketDir, name))
		if e != nil || info.Mode().Perm() != 0600 {
			t.Fatal(name, info.Mode(), e)
		}
	}
	// A previous holder's socket file is replaced; a regular file in its place is never removed.
	x.h.closeListeners()
	stale, e := net.ListenUnix("unix", &net.UnixAddr{Name: filepath.Join(x.h.socketDir, controlSocket), Net: "unix"})
	if e != nil {
		t.Fatal(e)
	}
	stale.SetUnlinkOnClose(false)
	stale.Close()
	if e = x.h.listen(); e != nil {
		t.Fatal("stale socket not replaced", e)
	}
	x.h.closeListeners()
	_ = os.WriteFile(filepath.Join(x.h.socketDir, controlSocket), []byte("x"), 0600)
	if e = x.h.listen(); e == nil || e.Error() != "holder_socket_path_invalid" {
		t.Fatal(e)
	}
	if _, e = os.Lstat(filepath.Join(x.h.socketDir, controlSocket)); e != nil {
		t.Fatal("a regular file was removed")
	}
	c := x.h.cfg
	if validSocketDirectory(c, c.SessionDirectory) == nil || validSocketDirectory(c, filepath.Join(c.EventsDirectory, "s")) == nil || validSocketDirectory(c, "relative") == nil {
		t.Fatal("a socket directory may overlap the session or events directory")
	}
}

// spec: ANYM-937
func TestHolderAttachVerifiesTheLeaseAgainstTheDatabase(t *testing.T) {
	x := newHarness(t)
	for _, lease := range []leaseRef{{"forged", 1}, {"c1", 7}, {"c1", 0}} {
		if l := x.dial(t, "control", lease, [2]int{1, 1}, x.h.token); l.reply["error"] != "collector_lease_invalid" {
			t.Fatal(lease, l.reply)
		}
	}
	// A released lease (expires_at moved to now) is no live row for that owner and epoch.
	x.leases.set("", 0)
	if l := x.dial(t, "control", c1, [2]int{1, 1}, x.h.token); l.reply["error"] != "collector_lease_invalid" {
		t.Fatal(l.reply)
	}
	// Another account's lease: the holder's login sees only its own account's row (row security), so it is no row.
	x.leases.set("c1", 1)
	x.leases.setDown(true)
	if l := x.dial(t, "control", c1, [2]int{1, 1}, x.h.token); l.reply["error"] != "collector_lease_unverified" {
		t.Fatal("a lease the database did not confirm attached", l.reply)
	}
	x.leases.setDown(false)
	if l := x.dial(t, "send", c1, [2]int{1, 1}, x.h.token); l.reply["error"] != "holder_not_attached" {
		t.Fatal("send lane without an attachment", l.reply)
	}
	control := x.attach(t, "control", c1)
	if l := x.dial(t, "control", c1, [2]int{1, 1}, x.h.token); l.reply["error"] != "holder_busy" {
		t.Fatal("second live attachment", l.reply)
	}
	if l := x.dial(t, "send", leaseRef{"c1", 2}, [2]int{1, 1}, x.h.token); l.reply["error"] != "holder_not_attached" {
		t.Fatal("send lane of another lease", l.reply)
	}
	send := x.attach(t, "send", c1)
	if l := x.dial(t, "send", c1, [2]int{1, 1}, x.h.token); l.reply["error"] != "holder_busy" {
		t.Fatal("second send lane", l.reply)
	}
	// A successor's lease: the database's positive answer for it detaches the stale attachment, both lanes.
	x.leases.set("c2", 2)
	x.attach(t, "control", leaseRef{"c2", 2})
	if !control.closed(t) || !send.closed(t) {
		t.Fatal("stale connection kept after a new lease")
	}
}

// spec: ANYM-937
func TestASuccessorAttachesWhileThePredecessorsLongReadIsCancelled(t *testing.T) {
	x := newHarness(t)
	control, r := readyCollector(t, x, c1, 1)
	if r["result"] == nil {
		t.Fatal(r)
	}
	x.world.set(func(w *fakeWorld) { w.readOnCtx = true })
	// A media download of the predecessor is in flight (its deadline can be 50 min) when the successor attaches.
	control.write(t, map[string]any{"id": 99, "method": "media", "params": map[string]any{}})
	eventually(t, func() bool { return x.world.count(&x.world.readsEntered) == 1 })
	x.leases.set("c2", 2)
	done := make(chan *lane, 1)
	go func() { done <- x.dial(t, "control", leaseRef{"c2", 2}, [2]int{1, 1}, x.h.token) }()
	select {
	case l := <-done:
		if l.reply["attached"] == nil {
			t.Fatal(l.reply)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the successor waited for the predecessor's read")
	}
	x.world.mu.Lock()
	cancelled := x.world.readCancelled
	x.world.mu.Unlock()
	if !cancelled {
		t.Fatal("the predecessor's read kept running after its attachment ended")
	}
}

// spec: ANYM-937
func TestHolderKeepsTheLastVerifiedLeaseOnlyUntilItEnds(t *testing.T) {
	x := newHarness(t)
	control := x.attach(t, "control", c1)
	x.leases.setDown(true)
	x.keepalive(t, control, c1, nil)
	x.clock.advance(59 * time.Second)
	x.h.watchOnce()
	if att, _, _ := x.state(); att == nil {
		t.Fatal("database outage detached a verified lease before its end")
	}
	x.clock.advance(2 * time.Second)
	x.h.watchOnce()
	if !control.closed(t) {
		t.Fatal("attachment outlived its last verified lease")
	}
	// A keepalive naming another lease, and a positive "not this lease", both detach.
	x.leases.setDown(false)
	control = x.attach(t, "control", c1)
	control.write(t, map[string]any{"keepalive": map[string]any{"lease": leaseRef{"c1", 9}}})
	if !control.closed(t) {
		t.Fatal("keepalive with another lease kept")
	}
	control = x.attach(t, "control", c1)
	x.leases.set("c1", 2)
	control.write(t, map[string]any{"keepalive": map[string]any{"lease": c1}})
	if !control.closed(t) {
		t.Fatal("released lease kept")
	}
}

// spec: ANYM-937
func TestHolderChecksTheLeaseBeforeEverySendLaneMethod(t *testing.T) {
	x := newHarness(t)
	control := x.attach(t, "control", c1)
	send := x.attach(t, "send", c1)
	x.keepalive(t, control, c1, policyWith(1, x.clock.now().Add(45*time.Second)))
	if r := control.call(t, "start", map[string]any{}); r["result"] == nil {
		t.Fatal(r)
	}
	if r := control.call(t, "send_text", map[string]any{}); r["error"] != "operation_unsupported" {
		t.Fatal("send method on the control lane", r)
	}
	if r := send.call(t, "page", map[string]any{}); r["error"] != "operation_unsupported" {
		t.Fatal("control method on the send lane", r)
	}
	before := x.leases.count()
	x.leases.set("c2", 2) // the collector lost its lease
	r := send.call(t, "messenger_operation", map[string]any{"command_row_id": "op_" + fmt.Sprintf("%032x", 1), "operation": "block_contact"})
	if r["error"] != "collector_lease_invalid" || x.leases.count() != before+1 || x.world.count(&x.world.operations) != 0 {
		t.Fatal("send-lane method ran on a lease the database refused", r)
	}
	if !control.closed(t) {
		t.Fatal("refused lease kept the attachment")
	}
}

func readyCollector(t *testing.T, x *harness, lease leaseRef, version int64) (*lane, map[string]any) {
	t.Helper()
	x.leases.set(lease.Owner, lease.Epoch)
	control := x.attach(t, "control", lease)
	x.keepalive(t, control, lease, policyWith(version, x.clock.now().Add(45*time.Second)))
	return control, control.call(t, "start", map[string]any{})
}

// spec: ANYM-937
func TestACollectorRestartWithinThePolicyKeepsTheConnection(t *testing.T) {
	x := newHarness(t)
	control, r := readyCollector(t, x, c1, 1)
	result, _ := r["result"].(map[string]any)
	if result == nil || result["generation"] != float64(1) {
		t.Fatal(r)
	}
	control.conn.Close() // the collector stops
	eventually(t, func() bool { att, _, _ := x.state(); return att == nil })
	for i := 0; i < 30; i++ { // a 30 s restart, watched every second
		x.clock.advance(time.Second)
		x.h.watchOnce()
	}
	_, r = readyCollector(t, x, leaseRef{"c1", 2}, 2)
	result, _ = r["result"].(map[string]any)
	if result == nil || result["generation"] != float64(1) || x.world.count(&x.world.starts) != 1 || x.world.count(&x.world.disconnects) != 0 {
		t.Fatal("a restart inside the policy's lifetime reconnected WhatsApp", r, x.world.starts, x.world.disconnects)
	}
}

// spec: ANYM-937
func TestPolicyExpiryDisconnectsOnceAndNeverReconnectsWithoutACollector(t *testing.T) {
	x := newHarness(t)
	control, _ := readyCollector(t, x, c1, 1)
	control.conn.Close()
	eventually(t, func() bool { att, _, _ := x.state(); return att == nil })
	x.clock.advance(44 * time.Second)
	x.h.watchOnce()
	if x.world.count(&x.world.closes) != 0 {
		t.Fatal("disconnected before expires_at")
	}
	x.clock.advance(2 * time.Second)
	for i := 0; i < 600; i++ { // ten minutes without a collector
		x.h.watchOnce()
		x.clock.advance(time.Second)
	}
	if x.world.count(&x.world.closes) != 1 || x.world.count(&x.world.disconnects) != 1 || x.world.count(&x.world.starts) != 1 {
		t.Fatal("expiry must cost exactly one disconnect and no reconnect", x.world.closes, x.world.disconnects, x.world.starts)
	}
	if _, e := x.h.policyNow(); e == nil || e.Error() != "policy_expired" {
		t.Fatal("capture allowed under an expired policy", e)
	}
	// A collector back with a fresh policy: one new connection, the next generation.
	_, r := readyCollector(t, x, leaseRef{"c1", 2}, 2)
	result, _ := r["result"].(map[string]any)
	if result == nil || result["generation"] != float64(2) || x.world.count(&x.world.starts) != 2 {
		t.Fatal(r)
	}
	// An attached collector without a fresh policy cannot start a connection.
	x.clock.advance(46 * time.Second)
	x.h.watchOnce()
	x.leases.set("c1", 3)
	control = x.attach(t, "control", leaseRef{"c1", 3})
	if r = control.call(t, "start", map[string]any{}); r["error"] != "policy_expired" || x.world.count(&x.world.starts) != 2 {
		t.Fatal(r)
	}
}

// spec: ANYM-937
func TestPolicyVersionsOnlyMoveForwardAndExpiryIsBounded(t *testing.T) {
	x := newHarness(t)
	raw := func(v map[string]any) json.RawMessage { b, _ := json.Marshal(v); return b }
	far := x.clock.now().Add(time.Hour)
	if e := x.h.applyPolicy(raw(policyWith(5, far, "111111@c.us"))); e != nil {
		t.Fatal(e)
	}
	x.h.mu.Lock()
	expires := x.h.pol.expires
	x.h.mu.Unlock()
	if expires != x.clock.now().Add(45*time.Second) {
		t.Fatal("expires_at beyond 45 s accepted", expires)
	}
	if e := x.h.applyPolicy(raw(policyWith(4, far))); e != nil {
		t.Fatal(e)
	}
	p, e := x.h.policyNow()
	if e != nil || p.Version != 5 || p.Allows("111111@c.us") {
		t.Fatal("an older version replaced a newer one", p, e)
	}
	bad := policyWith(6, far)
	delete(bad, "exclude_chats")
	if x.h.applyPolicy(raw(bad)) == nil || x.h.applyPolicy(raw(map[string]any{"version": 7, "unknown": 1})) == nil {
		t.Fatal("malformed policy accepted")
	}
}

// spec: ANYM-937
func TestExpiredPolicyCapturesNothingOnTheEventPath(t *testing.T) {
	x := newHarness(t)
	p := skipProvider(t)
	p.policySource = x.h.policyNow
	count := func() int {
		var n int
		p.Capture.db.QueryRow("SELECT COUNT(*) FROM capture").Scan(&n)
		return n
	}
	raw := func(v map[string]any) json.RawMessage { b, _ := json.Marshal(v); return b }
	// No collector has sent a policy yet: the event is refused (not ACKed, it stays in whatsmeow's buffer).
	if e := p.handleMessage(fixtureMessage("before", 1700000000)); e == nil || count() != 0 {
		t.Fatal("captured without a policy", e)
	}
	_ = x.h.applyPolicy(raw(policyWith(1, x.clock.now().Add(45*time.Second))))
	if e := p.handleMessage(fixtureMessage("fresh", 1700000001)); e != nil || count() != 1 {
		t.Fatal(e)
	}
	x.clock.advance(46 * time.Second)
	if e := p.handleMessage(fixtureMessage("late", 1700000002)); e == nil || e.Error() != "policy_expired" || count() != 1 {
		t.Fatal("a write after expires_at", e)
	}
	// The re-attach policy excludes the chat: its buffered event is discarded before any write.
	_ = x.h.applyPolicy(raw(policyWith(2, x.clock.now().Add(45*time.Second), "111111@c.us")))
	if e := p.handleMessage(fixtureMessage("late", 1700000002)); e != nil || count() != 1 {
		t.Fatal("an excluded chat's buffered event was written", e)
	}
	if policy, _ := p.policy(); policy.Version != 2 {
		t.Fatal("the capture does not see the current version", policy.Version)
	}
	_ = types.EmptyJID
}

// operationExpiry is ten minutes after the harness clock's start (a messenger command's default lifetime).
const operationExpiry = 1_800_000_600

func operationParams(row int, operation string) map[string]any {
	return map[string]any{"command_row_id": fmt.Sprintf("op_%032x", row), "operation": operation, "parameters": map[string]any{}, "expires_at": operationExpiry}
}

// spec: ANYM-937
func TestOperationJournalSendsEachCommandAtMostOnce(t *testing.T) {
	x := newHarness(t)
	_, _ = readyCollector(t, x, c1, 1)
	send := x.attach(t, "send", c1)
	journal := filepath.Join(x.h.cfg.EventsDirectory, operationJournalDirectory)
	// The pending record is durable before the provider call.
	hook := func() {
		raw, e := os.ReadFile(filepath.Join(journal, fmt.Sprintf("op_%032x.0.json", 1)))
		var v operationOutcome
		if e != nil || json.Unmarshal(raw, &v) != nil || !v.Pending {
			t.Error("provider call before the pending record", e)
		}
	}
	x.world.set(func(w *fakeWorld) { w.onOperation = hook })
	// Collector stopped before dispatch: nothing journaled, so the request runs once.
	first := send.call(t, "messenger_operation", operationParams(1, "block_contact"))
	x.world.set(func(w *fakeWorld) { w.onOperation = nil })
	if first["result"] == nil || x.world.count(&x.world.operations) != 1 {
		t.Fatal(first)
	}
	// Collector stopped after dispatch and asks again: the journaled outcome, no second call.
	again := send.call(t, "messenger_operation", operationParams(1, "block_contact"))
	if fmt.Sprint(again["result"]) != fmt.Sprint(first["result"]) || x.world.count(&x.world.operations) != 1 {
		t.Fatal("a journaled operation was sent again", again)
	}
	changed := operationParams(1, "unblock_contact")
	if r := send.call(t, "messenger_operation", changed); r["error"] != "operation_invalid_parameters" || x.world.count(&x.world.operations) != 1 {
		t.Fatal("one key with another payload", r)
	}
	// Two commands with the same request ID from different principals are two command rows: each runs once.
	send.call(t, "messenger_operation", operationParams(2, "block_contact"))
	send.call(t, "messenger_operation", operationParams(3, "block_contact"))
	if x.world.count(&x.world.operations) != 3 {
		t.Fatal(x.world.operations)
	}
	// The holder ended mid-call: a pending record with no call in flight never sends again.
	_ = x.h.writeOutcome(operationOutcome{CommandRowID: fmt.Sprintf("op_%032x", 4), PayloadDigest: "x", Pending: true})
	if r := send.call(t, "messenger_operation", operationParams(4, "block_contact")); r["error"] != "operation_invalid_parameters" && r["error"] != "operation_outcome_unknown" {
		t.Fatal(r)
	}
	b, _ := json.Marshal(map[string]any{"expires_at": operationExpiry, "operation": "block_contact", "parameters": map[string]any{}})
	digest := fmt.Sprintf("%x", sha256Sum(b))
	_ = x.h.writeOutcome(operationOutcome{CommandRowID: fmt.Sprintf("op_%032x", 5), PayloadDigest: digest, Pending: true})
	if r := send.call(t, "messenger_operation", operationParams(5, "block_contact")); r["error"] != "operation_outcome_unknown" || x.world.count(&x.world.operations) != 3 {
		t.Fatal("holder crash mid-call: retried", r)
	}
	if r := send.call(t, "messenger_operation", map[string]any{"operation": "block_contact", "parameters": map[string]any{}, "expires_at": operationExpiry}); r["error"] != "operation_invalid_parameters" || x.world.count(&x.world.operations) != 3 {
		t.Fatal("an operation without its command row", r)
	}
}

// spec: ANYM-937
func TestOperationJournalASecondRequestWaitsForTheFirstOutcome(t *testing.T) {
	x := newHarness(t)
	_, _ = readyCollector(t, x, c1, 1)
	block, entered := make(chan struct{}), make(chan struct{}, 1)
	x.world.set(func(w *fakeWorld) { w.opBlock, w.opEntered = block, entered })
	p := &fakeProvider{world: x.world}
	params, _ := json.Marshal(operationParams(1, "block_contact"))
	results := make(chan any, 2)
	go func() {
		r, e := x.h.journaledOperation(p, request{ID: 1, Method: "messenger_operation", Params: params})
		results <- fmt.Sprint(r, e)
	}()
	<-entered
	go func() {
		r, e := x.h.journaledOperation(p, request{ID: 2, Method: "messenger_operation", Params: params})
		results <- fmt.Sprint(r, e)
	}()
	time.Sleep(50 * time.Millisecond)
	close(block)
	a, b := <-results, <-results
	if a != b || x.world.count(&x.world.operations) != 1 {
		t.Fatal("concurrent duplicate sent twice or answered differently", a, b)
	}
	// A failed provider call is journaled as its error and returned again, never retried.
	x.world.set(func(w *fakeWorld) {
		w.opBlock, w.opEntered, w.opErr = nil, nil, errors.New("provider_operation_uncertain")
	})
	params, _ = json.Marshal(operationParams(2, "block_contact"))
	for i := 0; i < 2; i++ {
		if _, e := x.h.journaledOperation(p, request{ID: 3, Method: "messenger_operation", Params: params}); e == nil || e.Error() != "provider_operation_uncertain" {
			t.Fatal(e)
		}
	}
	if x.world.count(&x.world.operations) != 2 {
		t.Fatal("an uncertain operation was retried", x.world.operations)
	}
	// Reads change nothing and are not journaled.
	params, _ = json.Marshal(operationParams(3, "list_contacts"))
	x.world.set(func(w *fakeWorld) { w.opErr = nil })
	x.h.journaledOperation(p, request{ID: 4, Method: "messenger_operation", Params: params})
	if _, e := os.Lstat(x.h.journalPath(fmt.Sprintf("op_%032x", 3), 0)); !os.IsNotExist(e) {
		t.Fatal("a read was journaled")
	}
}

// spec: ANYM-937
func TestSessionTerminalErrorKeepsTheHolderIdle(t *testing.T) {
	x := newHarness(t)
	x.world.set(func(w *fakeWorld) { w.startErr = errors.New("whatsapp_logged_out") })
	control, r := readyCollector(t, x, c1, 1)
	if r["error"] != "whatsapp_logged_out" {
		t.Fatal(r)
	}
	for i := 0; i < 3; i++ {
		if r = control.call(t, "start", map[string]any{}); r["error"] != "whatsapp_logged_out" {
			t.Fatal(r)
		}
	}
	if x.world.count(&x.world.starts) != 1 {
		t.Fatal("a session-terminal error was followed by another connect", x.world.starts)
	}
	// A restarted holder over the same session store stays idle; a replaced store (a pairing) clears it.
	h2 := newHolder(context.Background(), x.h.cfg, x.h.socketDir, nil)
	h2.loadTerminal()
	if h2.terminal != "whatsapp_logged_out" {
		t.Fatal("terminal state lost across a holder restart")
	}
	if e := os.WriteFile(filepath.Join(x.h.cfg.SessionDirectory, "auth.sqlite"), []byte("paired"), 0600); e != nil {
		t.Fatal(e)
	}
	h3 := newHolder(context.Background(), x.h.cfg, x.h.socketDir, nil)
	h3.loadTerminal()
	if h3.terminal != "" {
		t.Fatal("a replaced session store stayed terminal")
	}
}

// spec: ANYM-937
func TestAProviderFailureEndsTheConnectionAndTheGenerationSurvivesRestarts(t *testing.T) {
	x := newHarness(t)
	_, r := readyCollector(t, x, c1, 1)
	if r["result"].(map[string]any)["generation"] != float64(1) {
		t.Fatal(r)
	}
	x.world.mu.Lock()
	last := x.world.last
	x.world.mu.Unlock()
	last.fail.Store("durable_capture_failed")
	x.h.watchOnce()
	if _, prov, terminal := x.state(); x.world.count(&x.world.closes) != 1 || prov != nil || terminal != "" {
		t.Fatal("a failed connection was kept or made terminal")
	}
	h2 := newHolder(context.Background(), x.h.cfg, x.h.socketDir, nil)
	if g, e := h2.nextGeneration(); e != nil || g != 2 {
		t.Fatal("the connection generation restarted", g, e)
	}
}

// spec: ANYM-937
func TestHolderLocksRefuseAChildModeCollectorAndAPairRun(t *testing.T) {
	c := testConfig(t)
	boot := filepath.Join(t.TempDir(), "boot_id")
	_ = os.WriteFile(boot, []byte("boot-1\n"), 0600)
	held, e := openHostLock(c.Account, c.SessionLockDirectory, boot)
	if e != nil {
		t.Fatal(e)
	}
	defer held.Close()
	if !hex32.MatchString(held.hostID) || held.bootID != "boot-1" || held.lockRef == "" {
		t.Fatal(held)
	}
	// A collector in child mode (and whatsapp_pair) takes the same canonical lock first: refused.
	if _, e = openHostLock(c.Account, c.SessionLockDirectory, boot); e == nil || e.Error() != "session_already_owned" {
		t.Fatal(e)
	}
	session, e := sessionDirectoryLock(c.SessionDirectory)
	if e != nil {
		t.Fatal(e)
	}
	defer session.Close()
	if _, e = sessionDirectoryLock(c.SessionDirectory); e == nil || e.Error() != "session_already_owned" {
		t.Fatal(e)
	}
	// The host ID is created once and never overwritten.
	again, _ := readHostID(c.SessionLockDirectory)
	if again != held.hostID {
		t.Fatal("host ID changed")
	}
	if _, e = openHostLock(".hidden", c.SessionLockDirectory, boot); e == nil {
		t.Fatal("a dot account names a hidden lock file")
	}
}

// spec: ANYM-937
func TestClaimWaitBacksOffToFiveMinutesAndLogsHourly(t *testing.T) {
	b := newBackoff()
	clock := &fakeClock{at: time.Unix(1_800_000_000, 0)}
	var slept []time.Duration
	b.now = clock.now
	b.sleep = func(ctx context.Context, d time.Duration) error {
		slept = append(slept, d)
		clock.advance(d)
		return nil
	}
	logged := 0
	for i := 0; i < 20; i++ {
		before := b.logged
		_ = b.wait(context.Background(), "session_claim.waiting", nil)
		if b.logged != before {
			logged++
		}
	}
	if slept[0] != 5*time.Second || slept[1] != 10*time.Second || slept[len(slept)-1] != 5*time.Minute {
		t.Fatal(slept)
	}
	if logged < 2 || logged > 2+int((time.Duration(len(slept))*5*time.Minute)/time.Hour) {
		t.Fatal("logged", logged)
	}
}
