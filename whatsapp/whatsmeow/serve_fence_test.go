package main

// ANYM-937 Astra r1 regressions: each test fails on the r1 head (f74876eb) and passes after its fix.

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func rawPolicy(v map[string]any) json.RawMessage { b, _ := json.Marshal(v); return b }

func captureRows(t *testing.T, p *Provider) int {
	t.Helper()
	var n int
	if e := p.Capture.db.QueryRow("SELECT COUNT(*) FROM capture").Scan(&n); e != nil {
		t.Fatal(e)
	}
	return n
}

func holderSpoolFiles(t *testing.T, p *Provider) int {
	t.Helper()
	entries, e := os.ReadDir(p.Config.EventsDirectory)
	if e != nil {
		t.Fatal(e)
	}
	n := 0
	for _, entry := range entries {
		if entry.Type().IsRegular() && strings.HasSuffix(entry.Name(), ".json") && !strings.HasPrefix(entry.Name(), ".") {
			n++
		}
	}
	return n
}

func fencedProvider(t *testing.T, x *harness) *Provider {
	t.Helper()
	p := skipProvider(t)
	p.policySource = x.h.policyNow
	p.Capture.Fence = x.h.fence
	return p
}

// spec: ANYM-937
func TestACaptureWaitingForTheStoreWritesNothingAfterANewerPolicyExcludesItsChat(t *testing.T) {
	// Finding 1: the handler read version 1, then waited on the store while version 2 excluded the chat.
	x := newHarness(t)
	p := fencedProvider(t, x)
	_ = x.h.applyPolicy(rawPolicy(policyWith(1, x.clock.now().Add(45*time.Second))))
	p.Capture.mu.Lock()
	done := make(chan error, 1)
	go func() { done <- p.handleMessage(fixtureMessage("waiting", 1700000000)) }()
	time.Sleep(50 * time.Millisecond)
	if e := x.h.applyPolicy(rawPolicy(policyWith(2, x.clock.now().Add(45*time.Second), "111111@c.us"))); e != nil {
		t.Fatal(e)
	}
	p.Capture.mu.Unlock()
	if e := <-done; e != nil || captureRows(t, p) != 0 || holderSpoolFiles(t, p) != 0 {
		t.Fatal("written under version 1 after version 2 excluded the chat", e, captureRows(t, p))
	}
}

// spec: ANYM-937
func TestACaptureWaitingForTheStoreWritesNothingAfterThePolicyExpired(t *testing.T) {
	x := newHarness(t)
	p := fencedProvider(t, x)
	_ = x.h.applyPolicy(rawPolicy(policyWith(1, x.clock.now().Add(45*time.Second))))
	p.Capture.mu.Lock()
	done := make(chan error, 1)
	go func() { done <- p.handleMessage(fixtureMessage("late", 1700000000)) }()
	time.Sleep(50 * time.Millisecond)
	x.clock.advance(46 * time.Second)
	p.Capture.mu.Unlock()
	if e := <-done; e == nil || e.Error() != "policy_expired" || captureRows(t, p) != 0 {
		t.Fatal("a write after expires_at", e, captureRows(t, p))
	}
}

// spec: ANYM-937
func TestReplayNeverPublishesAChatTheCurrentPolicyExcludes(t *testing.T) {
	x := newHarness(t)
	p := fencedProvider(t, x)
	_ = x.h.applyPolicy(rawPolicy(policyWith(1, x.clock.now().Add(45*time.Second))))
	// A full spool keeps a history capture pending (journaled, not exported).
	p.Capture.MaxSpoolEntries = 0
	if e := p.Capture.Put(fixtureMessage("pending", 1700000000), "history"); e != nil {
		t.Fatal(e)
	}
	p.Capture.MaxSpoolEntries = 1000
	if captureRows(t, p) != 1 || holderSpoolFiles(t, p) != 0 {
		t.Fatal("fixture not pending")
	}
	_ = x.h.applyPolicy(rawPolicy(policyWith(2, x.clock.now().Add(45*time.Second), "111111@c.us")))
	if e := p.Capture.Replay(); e != nil || holderSpoolFiles(t, p) != 0 {
		t.Fatal("replay exported an excluded chat", e, holderSpoolFiles(t, p))
	}
	var published int
	_ = p.Capture.db.QueryRow("SELECT published FROM capture").Scan(&published)
	if published != 1 {
		t.Fatal("the excluded row stays pending forever")
	}
	// Under an expired policy replay writes nothing at all.
	p.Capture.MaxSpoolEntries = 0
	_ = x.h.applyPolicy(rawPolicy(policyWith(3, x.clock.now().Add(45*time.Second))))
	_ = p.Capture.Put(fixtureMessage("second", 1700000001), "history")
	p.Capture.MaxSpoolEntries = 1000
	x.clock.advance(46 * time.Second)
	if e := p.Capture.Replay(); e == nil || e.Error() != "policy_expired" || holderSpoolFiles(t, p) != 0 {
		t.Fatal("replay under an expired policy", e)
	}
}

// spec: ANYM-937
func TestADetachedPredecessorNeverReachesTheProvider(t *testing.T) {
	// Finding 2: the predecessor's lease check answered "valid", then a successor attached before it went on.
	x := newHarness(t)
	_, _ = readyCollector(t, x, c1, 1)
	send := x.attach(t, "send", c1)
	paused, resume := make(chan struct{}), make(chan struct{})
	once := false
	x.leases.setAfter(func(owner string) {
		if owner == "c1" && !once {
			once = true
			close(paused)
			<-resume
		}
	})
	done := make(chan struct{})
	go func() {
		defer close(done)
		b, _ := json.Marshal(map[string]any{"id": 1, "method": "messenger_operation", "params": operationParams(1, "block_contact")})
		_, _ = send.conn.Write(append(b, '\n'))
		_, _ = send.reader.ReadBytes('\n')
	}()
	<-paused
	x.leases.setAfter(nil)
	x.leases.set("c2", 2)
	x.attach(t, "control", leaseRef{"c2", 2})
	close(resume)
	<-done
	time.Sleep(50 * time.Millisecond)
	if x.world.count(&x.world.operations) != 0 {
		t.Fatal("a detached predecessor's mutation reached the provider")
	}
}

// spec: ANYM-937
func TestASuccessorAttachesOnlyAfterThePredecessorsCallsReturn(t *testing.T) {
	x := newHarness(t)
	_, _ = readyCollector(t, x, c1, 1)
	send := x.attach(t, "send", c1)
	block, entered := make(chan struct{}), make(chan struct{}, 1)
	x.world.set(func(w *fakeWorld) { w.opBlock, w.opEntered = block, entered })
	go func() {
		b, _ := json.Marshal(map[string]any{"id": 1, "method": "messenger_operation", "params": operationParams(1, "block_contact")})
		_, _ = send.conn.Write(append(b, '\n'))
		_, _ = send.reader.ReadBytes('\n')
	}()
	<-entered
	x.leases.set("c2", 2)
	attached := make(chan *lane, 1)
	go func() { attached <- x.dial(t, "control", leaseRef{"c2", 2}, [2]int{1, 1}, x.h.token) }()
	select {
	case <-attached:
		t.Fatal("a successor attached while the predecessor's mutation was in flight")
	case <-time.After(200 * time.Millisecond):
	}
	close(block)
	if l := <-attached; l.reply["attached"] == nil {
		t.Fatal(l.reply)
	}
}

// spec: ANYM-937
func TestASlowLeaseAnswerNeverExtendsTheLease(t *testing.T) {
	// Finding 3: the deadline counts from the question, and an answer after it is "not live".
	x := newHarness(t)
	x.leases.mu.Lock()
	x.leases.remaining = 3 * time.Second
	x.leases.mu.Unlock()
	x.leases.setAfter(func(string) { x.clock.advance(5 * time.Second) })
	if l := x.dial(t, "control", c1, [2]int{1, 1}, x.h.token); l.reply["error"] != "collector_lease_invalid" {
		t.Fatal("an answer that arrived after the lease ended attached", l.reply)
	}
	x.leases.mu.Lock()
	x.leases.remaining = 60 * time.Second
	x.leases.mu.Unlock()
	asked := x.clock.now()
	x.attach(t, "control", c1)
	att, _, _ := x.state()
	x.h.mu.Lock()
	until := att.verifiedUntil
	x.h.mu.Unlock()
	if !until.Equal(asked.Add(60 * time.Second)) {
		t.Fatal("the database's latency was added to the lease", until.Sub(asked))
	}
}

// spec: ANYM-937
func TestShutdownWaitsForAProviderStartBeforeItReturns(t *testing.T) {
	// Finding 4: a start under way was invisible to shutdown, which then let serve release the claim.
	x := newHarness(t)
	gate := make(chan struct{})
	x.world.set(func(w *fakeWorld) { w.startGate = gate })
	control := x.attach(t, "control", c1)
	x.keepalive(t, control, c1, policyWith(1, x.clock.now().Add(45*time.Second)))
	go func() {
		b, _ := json.Marshal(map[string]any{"id": 1, "method": "start", "params": map[string]any{}})
		_, _ = control.conn.Write(append(b, '\n'))
	}()
	eventually(t, func() bool { return x.world.count(&x.world.starts) == 1 })
	go func() { time.Sleep(200 * time.Millisecond); close(gate) }()
	x.cancel()
	x.h.shutdown()
	x.world.mu.Lock()
	returned := x.world.startReturned
	x.world.mu.Unlock()
	if !returned {
		t.Fatal("shutdown returned (and the claim would be released) while the provider was still starting")
	}
	if _, prov, _ := x.state(); prov != nil {
		t.Fatal("a provider started during shutdown was published")
	}
}

// spec: ANYM-937
func TestPolicyExpiryCancelsAProviderStartUnderWay(t *testing.T) {
	x := newHarness(t)
	gate := make(chan struct{})
	defer close(gate)
	x.world.set(func(w *fakeWorld) { w.startGate, w.startOnCtx = gate, true })
	control := x.attach(t, "control", c1)
	x.keepalive(t, control, c1, policyWith(1, x.clock.now().Add(45*time.Second)))
	replies := make(chan map[string]any, 1)
	go func() {
		b, _ := json.Marshal(map[string]any{"id": 1, "method": "start", "params": map[string]any{}})
		_, _ = control.conn.Write(append(b, '\n'))
		line, _ := control.reader.ReadBytes('\n')
		var v map[string]any
		_ = json.Unmarshal(line, &v)
		replies <- v
	}()
	eventually(t, func() bool { return x.world.count(&x.world.starts) == 1 })
	x.clock.advance(46 * time.Second)
	x.h.watchOnce()
	r := <-replies
	if r["error"] == nil {
		t.Fatal("a connection completed under an expired policy", r)
	}
	if _, prov, _ := x.state(); prov != nil {
		t.Fatal("published after expiry")
	}
}

// spec: ANYM-937
func TestServeModeNeverReconnectsByItself(t *testing.T) {
	// Finding 5: whatsmeow's auto-reconnect is off in serve mode; a dropped connection ends, and only an attached
	// collector's `start` makes the next one, with the next generation.
	h := newHolder(t.Context(), testConfig(t), t.TempDir(), nil)
	p, ok := h.newProvider(h).(*Provider)
	if !ok || !p.serveMode || p.captureFence == nil || p.policySource == nil {
		t.Fatal("serve-mode provider without its fence or with auto-reconnect", p)
	}
	source, _ := os.ReadFile("runtime.go")
	if !strings.Contains(string(source), "p.Client.EnableAutoReconnect = !p.serveMode") {
		t.Fatal("the provider's auto-reconnect is not tied to serve mode")
	}
	x := newHarness(t)
	control, _ := readyCollector(t, x, c1, 1)
	x.world.mu.Lock()
	last := x.world.last
	x.world.mu.Unlock()
	last.ready.Store(false) // a plain disconnect: no failure code
	x.h.watchOnce()
	if _, prov, _ := x.state(); prov != nil || x.world.count(&x.world.closes) != 1 {
		t.Fatal("a dropped connection was kept for a silent reconnect")
	}
	if r := control.call(t, "page", map[string]any{}); r["error"] != "provider_disconnected" {
		t.Fatal(r)
	}
	if r := control.call(t, "start", map[string]any{}); r["result"].(map[string]any)["generation"] != float64(2) {
		t.Fatal("the next connection has no generation of its own", r)
	}
}

// spec: ANYM-937
func TestTheOperationJournalRetiresOnlyExpiredCommands(t *testing.T) {
	// Finding 6: pruning must never let an executed command run again.
	x := newHarness(t)
	_, _ = readyCollector(t, x, c1, 1)
	send := x.attach(t, "send", c1)
	directory, e := durableDirectory(x.h.cfg.EventsDirectory, operationJournalDirectory)
	if e != nil {
		t.Fatal(e)
	}
	write := func(i int, expires float64) {
		v := operationOutcome{Kind: "messenger_operation_outcome", CommandRowID: fmt.Sprintf("op_%032x", i), PayloadDigest: "d", ExpiresAt: expires, Result: json.RawMessage(`{}`)}
		if e := os.WriteFile(filepath.Join(directory, fmt.Sprintf("op_%032x.0.json", i)), mustJSON(v), 0600); e != nil {
			t.Fatal(e)
		}
	}
	// A full journal of commands that have not expired refuses a new one before any call.
	for i := 1000; i < 2000; i++ {
		write(i, operationExpiry)
	}
	if r := send.call(t, "messenger_operation", operationParams(1, "block_contact")); r["error"] != "spool_full" || x.world.count(&x.world.operations) != 0 {
		t.Fatal("unexpired evidence was dropped or the call ran", r)
	}
	entries, _ := os.ReadDir(directory)
	if len(entries) != 1000 {
		t.Fatal("unexpired records were removed", len(entries))
	}
	// Records of commands expired more than a day ago are retired; a request for such a key is refused as expired.
	old := float64(x.clock.now().Add(-25 * time.Hour).Unix())
	for i := 1000; i < 2000; i++ {
		write(i, old)
	}
	if r := send.call(t, "messenger_operation", operationParams(1, "block_contact")); r["result"] == nil || x.world.count(&x.world.operations) != 1 {
		t.Fatal(r)
	}
	retired := operationParams(3000, "block_contact") // its record is gone: retired
	retired["expires_at"] = old
	if r := send.call(t, "messenger_operation", retired); r["error"] != "operation_expired" || x.world.count(&x.world.operations) != 1 {
		t.Fatal("a retired command executed again", r)
	}
	far := operationParams(2, "block_contact")
	far["expires_at"] = float64(x.clock.now().Add(48 * time.Hour).Unix())
	if r := send.call(t, "messenger_operation", far); r["error"] != "operation_invalid_parameters" || x.world.count(&x.world.operations) != 1 {
		t.Fatal("a command beyond the longest lifetime", r)
	}
}

// spec: ANYM-937
func TestTheOperationJournalDirectoryIsDurableBeforeAnyCall(t *testing.T) {
	// Finding 7: the directory is created and its parent synced before a record is relied on; a failure stops
	// the operation before the provider call.
	x := newHarness(t)
	_, _ = readyCollector(t, x, c1, 1)
	send := x.attach(t, "send", c1)
	path := filepath.Join(x.h.cfg.EventsDirectory, operationJournalDirectory)
	if e := os.WriteFile(path, []byte("x"), 0600); e != nil {
		t.Fatal(e)
	}
	if r := send.call(t, "messenger_operation", operationParams(1, "block_contact")); r["error"] == nil || x.world.count(&x.world.operations) != 0 {
		t.Fatal("an operation ran without a durable journal", r)
	}
	_ = os.Remove(path)
	if _, e := durableDirectory(x.h.cfg.EventsDirectory, operationJournalDirectory); e != nil {
		t.Fatal(e)
	}
	if info, e := os.Lstat(path); e != nil || !info.IsDir() || info.Mode().Perm() != 0700 {
		t.Fatal(info, e)
	}
	if _, e := durableDirectory(filepath.Join(x.h.cfg.EventsDirectory, "missing"), "x"); e == nil || !errors.Is(e, os.ErrNotExist) {
		t.Fatal("a failed parent was not reported", e)
	}
}
