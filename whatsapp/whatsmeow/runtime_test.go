package main

import (
	"bytes"
	"compress/zlib"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	waCommon "go.mau.fi/whatsmeow/proto/waCommon"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	waHistorySync "go.mau.fi/whatsmeow/proto/waHistorySync"
	waSyncAction "go.mau.fi/whatsmeow/proto/waSyncAction"
	waWeb "go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"go/ast"
	"go/parser"
	"go/token"
	"google.golang.org/protobuf/proto"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

func testConfig(t *testing.T) Config {
	t.Helper()
	root := t.TempDir()
	_ = os.Chmod(root, 0700)
	c := Config{Account: "test", ExpectedWID: "123456@c.us", SessionDirectory: filepath.Join(root, "session"), EventsDirectory: filepath.Join(root, "events"), PagesDirectory: filepath.Join(root, "pages"), MaxMediaBytes: 8 << 20}
	for _, d := range []string{c.SessionDirectory, c.EventsDirectory, c.PagesDirectory} {
		if e := os.Mkdir(d, 0700); e != nil {
			t.Fatal(e)
		}
	}
	return c
}
func fixtureMessage(id string, stamp int64) *events.Message {
	chat := types.NewJID("111111", types.DefaultUserServer)
	return &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: chat, Sender: chat}, ID: id, Timestamp: time.Unix(stamp, 0)}, Message: &waE2E.Message{Conversation: proto.String("hello")}}
}
func openTestStore(t *testing.T) *CaptureStore {
	t.Helper()
	s, e := OpenCapture(testConfig(t))
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { s.Close() })
	s.Owner = types.NewJID("123456", types.DefaultUserServer)
	return s
}
func savePolicy(t *testing.T, p *Provider, expires time.Time, policy Policy) {
	t.Helper()
	b, _ := json.Marshal(map[string]any{"expires_at": float64(expires.UnixNano()) / 1e9, "policy": policy})
	if e := os.WriteFile(filepath.Join(p.Config.EventsDirectory, ".policy"), b, 0600); e != nil {
		t.Fatal(e)
	}
}
func TestPolicyFreshFileAndExclusions(t *testing.T) {
	p := &Provider{Config: testConfig(t)}
	allow := Policy{Enabled: true, DiscoverChats: true, IncludeChats: []string{}, ExcludeChats: []string{"111111@c.us"}}
	savePolicy(t, p, time.Now().Add(time.Minute), allow)
	policy, e := p.policy()
	if e != nil || policy.Allows("111111@c.us") || !policy.Allows("222222@c.us") {
		t.Fatal(policy, e)
	}
	savePolicy(t, p, time.Now().Add(-time.Second), allow)
	if _, e = p.policy(); e == nil || e.Error() != "policy_expired" {
		t.Fatal(e)
	}
	savePolicy(t, p, time.Now().Add(time.Minute), allow)
	os.Chmod(filepath.Join(p.Config.EventsDirectory, ".policy"), 0644)
	if _, e = p.policy(); e == nil {
		t.Fatal("public policy accepted")
	}
}

func TestEveryLifecycleClientDefersACKUntilDurableHandling(t *testing.T) {
	// Exercise the actual constructor used by initial pairing, restored pairing,
	// and the normal collector, without connecting or creating a session.
	client := nativeClient(&store.Device{})
	if !client.SynchronousAck || !client.EnableDecryptedEventBuffer {
		t.Fatal("decrypt/ACK can consume a message before durable capture")
	}
	if !client.ManualHistorySyncDownload || !client.DisableManualHistorySyncReceipt {
		t.Fatal("history notification can escape the durable bounded queue")
	}
}
func TestPolicySymlinkRejected(t *testing.T) {
	p := &Provider{Config: testConfig(t)}
	target := filepath.Join(p.Config.SessionDirectory, "target")
	os.WriteFile(target, []byte(`{}`), 0600)
	os.Symlink(target, filepath.Join(p.Config.EventsDirectory, ".policy"))
	if _, e := p.policy(); e == nil {
		t.Fatal("symlink accepted")
	}
}
func TestIDsRetainObservedNamespaces(t *testing.T) {
	for _, id := range []string{"111111@c.us", "111111@lid", "111111-222@g.us"} {
		j, e := ParseChatID(id)
		if e != nil || LegacyChatID(j) != id {
			t.Fatal(id, j, e)
		}
	}
	if _, e := ParseChatID("111111:4@c.us"); e == nil {
		t.Fatal("AD accepted")
	}
	m := fixtureMessage("abc", 1700000000)
	if LegacyMessageID(m) != "false_111111@c.us_abc" {
		t.Fatal(LegacyMessageID(m))
	}
	m.Info.Chat = types.NewJID("123-456", types.GroupServer)
	m.Info.IsGroup = true
	if LegacyMessageID(m) != "false_123-456@g.us_abc_111111@c.us" {
		t.Fatal(LegacyMessageID(m))
	}
}
func TestNativeMessageMetadataOwnerLIDAndNoMillisecondInvention(t *testing.T) {
	m := fixtureMessage("a", 1700000000)
	owner := types.NewJID("123456", types.DefaultUserServer)
	lid := types.NewJID("999", types.HiddenUserServer)
	m.Message = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String("hello"), ContextInfo: &waE2E.ContextInfo{MentionedJID: []string{lid.String()}, StanzaID: proto.String("original"), Participant: proto.String(lid.String())}}}
	v, e := NormalizeMessage(m, owner, lid)
	if e != nil || v["mentionsOwner"] != true || v["replyToOwner"] != true || v["timestampResolution"] != "second" {
		t.Fatal(v, e)
	}
	v, e = NormalizeMessage(m, owner)
	if e != nil || v["mentionsOwner"] != nil || v["replyToOwner"] != nil {
		t.Fatal("unknown namespace fabricated", v, e)
	}
}
func TestMediaDescriptorExactFallbackAndFilename(t *testing.T) {
	m := fixtureMessage("media", 1700000000)
	m.Message = &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{FileName: proto.String("../../doc\n.pdf"), Mimetype: proto.String("application/pdf"), FileLength: proto.Uint64(8)}}
	v, e := NormalizeMessage(m, types.EmptyJID)
	if e != nil || v["filename"] != ".._.._doc_.pdf" || v["filesize"] != int64(8) || v["messageType"] != "document" {
		t.Fatal(v, e)
	}
}
func TestHistoryOriginAcceptedByArchiveContract(t *testing.T) {
	v, e := captureEnvelope("test", fixtureMessage("a", 1700000000), types.EmptyJID, "history")
	if e != nil || v["origin"] != "backfill" {
		t.Fatal(v, e)
	}
}

func TestReceiptsRetainOnlyObservedOutgoingEvidence(t *testing.T) {
	s := openTestStore(t)
	m := fixtureMessage("out", 1700000000)
	m.Info.IsFromMe = true
	m.Info.Sender = s.Owner
	if err := s.Put(m, "live"); err != nil {
		t.Fatal(err)
	}
	if err := s.Put(fixtureMessage("in", 1700000001), "live"); err != nil {
		t.Fatal(err)
	}
	receipt := &events.Receipt{MessageSource: types.MessageSource{Chat: m.Info.Chat}, MessageIDs: []types.MessageID{"out", "in", "absent"}, Timestamp: time.Unix(1700000010, 0), Type: types.ReceiptTypeRead}
	if err := s.StoreReceipt(receipt); err != nil {
		t.Fatal(err)
	}
	got, err := s.ReceiptObservations(LegacyChatID(m.Info.Chat), LegacyMessageID(m))
	if err != nil || len(got) != 1 || got[0]["state"] != "read" || got[0]["provider_at"] != nil || got[0]["audience"] != "private_peer" {
		t.Fatal(got, err)
	}
	got, err = s.ReceiptObservations(LegacyChatID(m.Info.Chat), "false_111111@c.us_in")
	if err != nil || len(got) != 0 {
		t.Fatal("incoming receipt fabricated", got, err)
	}
}
func TestDurableDedupeAndLocalPage(t *testing.T) {
	s := openTestStore(t)
	for _, id := range []string{"b", "a"} {
		if e := s.Put(fixtureMessage(id, 1700000000), "live"); e != nil {
			t.Fatal(e)
		}
	}
	if e := s.Put(fixtureMessage("a", 1700000000), "history"); e != nil {
		t.Fatal(e)
	}
	var n int
	s.db.QueryRow("SELECT COUNT(*) FROM capture").Scan(&n)
	if n != 2 {
		t.Fatal("replay duplicate", n)
	}
	page, e := s.Page("111111@c.us", 1, nil)
	if e != nil || len(page) != 1 || page[0].Info.ID != "b" {
		t.Fatal(page, e)
	}
	page, e = s.Page("111111@c.us", 1, []any{float64(1700000000), LegacyMessageID(page[0])})
	if e != nil || len(page) != 1 || page[0].Info.ID != "a" {
		t.Fatal(page, e)
	}
}
func TestSpoolFailureSurvivesRestartReplay(t *testing.T) {
	c := testConfig(t)
	s, e := OpenCapture(c)
	if e != nil {
		t.Fatal(e)
	}
	s.MaxSpoolEntries = 0
	if e = s.Put(fixtureMessage("a", 1700000000), "live"); e == nil || e.Error() != "spool_full" {
		t.Fatal(e)
	}
	s.Close()
	s, e = OpenCapture(c)
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	if e = s.Replay(); e != nil {
		t.Fatal(e)
	}
	entries, _ := os.ReadDir(c.EventsDirectory)
	if len(entries) != 1 || !strings.HasSuffix(entries[0].Name(), ".json") {
		t.Fatal(entries)
	}
	var published int
	s.db.QueryRow("SELECT published FROM capture").Scan(&published)
	if published != 1 {
		t.Fatal(published)
	}
}

func TestActuallyFullSpoolAllowsReadyAndRecoversAfterConsumerDrain(t *testing.T) {
	c := testConfig(t)
	s, err := OpenCapture(c)
	if err != nil {
		t.Fatal(err)
	}
	// A real existing spool entry consumes the quota; a second message is
	// durably journalled but cannot be exported, as during an archive DB outage.
	s.MaxSpoolEntries = 1
	if err = s.Put(fixtureMessage("already-exported", 1700000000), "live"); err != nil {
		t.Fatal(err)
	}
	if err = s.Put(fixtureMessage("pending", 1700000001), "live"); !spoolBackpressure(err) {
		t.Fatal("expected actually full existing spool", err)
	}
	files, err := os.ReadDir(c.EventsDirectory)
	if err != nil || len(files) != 1 {
		t.Fatal(files, err)
	}
	fullName := files[0].Name()
	s.Close()

	// Fill the actual production count boundary before reopening. Copies of an
	// exported capture stand in for the remaining unconsumed files, so startup
	// uses its real 10000-entry quota rather than a raised test-only capacity.
	exported, err := os.ReadFile(filepath.Join(c.EventsDirectory, fullName))
	if err != nil {
		t.Fatal(err)
	}
	for i := 1; i < 10000; i++ {
		if err = os.WriteFile(filepath.Join(c.EventsDirectory, fmt.Sprintf("filler-%05d.json", i)), exported, 0600); err != nil {
			t.Fatal(err)
		}
	}
	p := &Provider{Config: c}
	if err = p.prepareCapture(); err != nil {
		t.Fatal("full spool prevents provider startup/readiness", err)
	}
	defer p.Capture.Close()
	p.ready.Store(true) // the real Connected handler sets this after identity verification
	savePolicy(t, p, time.Now().Add(time.Minute), Policy{Enabled: true, DiscoverChats: true, IncludeChats: []string{}, ExcludeChats: []string{}})
	if _, err = p.handle(context.Background(), request{ID: 1, Method: "policy", Params: json.RawMessage(`{}`)}, func(any) error { return nil }); err != nil {
		t.Fatal("consumer cannot start after connected readiness", err)
	}
	p.captureFailure(errors.New("spool_full"))
	if !p.ready.Load() {
		t.Fatal("backpressure cancelled ready provider before consumer drain")
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { p.replayWorker(ctx, 10*time.Millisecond); close(done) }()
	defer func() { cancel(); <-done }()
	// The archive consumer commits the old event, then removes its spool file.
	if err = os.Remove(filepath.Join(c.EventsDirectory, fullName)); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	var pending int
	for time.Now().Before(deadline) {
		if err = p.Capture.db.QueryRow("SELECT COUNT(*) FROM capture WHERE published=0").Scan(&pending); err != nil {
			t.Fatal(err)
		}
		if pending == 0 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if pending != 0 || !p.ready.Load() {
		t.Fatal("pending capture did not recover after consumer freed capacity", pending)
	}
	var count int
	p.Capture.db.QueryRow("SELECT COUNT(*) FROM capture").Scan(&count)
	if count != 2 {
		t.Fatal("lost or duplicated journal captures", count)
	}
	files, _ = os.ReadDir(c.EventsDirectory)
	var recovered int
	for _, f := range files {
		if strings.HasPrefix(f.Name(), "00000000000000000002-") {
			recovered++
		}
	}
	if recovered != 1 {
		t.Fatal("pending capture exported more/less than once", recovered)
	}
}

func TestReplayPublishesBoundedOldestBatches(t *testing.T) {
	s := openTestStore(t)
	s.MaxSpoolEntries = 0
	const total = 2*replayBatchEntries + 1
	for i := 0; i < total; i++ {
		if err := s.Put(fixtureMessage(fmt.Sprintf("pending-%03d", i), 1700000000+int64(i)), "live"); !spoolBackpressure(err) {
			t.Fatal("capture was not durably queued", err)
		}
	}
	s.MaxSpoolEntries = 10000
	for _, want := range []int{replayBatchEntries, 2 * replayBatchEntries, total} {
		if err := s.Replay(); err != nil {
			t.Fatal(err)
		}
		var count, maxSeq int
		var bytes int64
		if err := s.db.QueryRow("SELECT COUNT(*),MAX(seq),SUM(length(envelope)) FROM capture WHERE published=1").Scan(&count, &maxSeq, &bytes); err != nil {
			t.Fatal(err)
		}
		if count != want || maxSeq != want {
			t.Fatal("replay skipped or exceeded oldest-first batch", count, maxSeq, want)
		}
		if want == replayBatchEntries && bytes > replayBatchBytes {
			t.Fatal("first batch exceeded byte budget", bytes)
		}
	}
}

func TestReplayBatchSharesConservativeSpoolQuota(t *testing.T) {
	s := openTestStore(t)
	s.MaxSpoolEntries = 0
	for i := 0; i < 5; i++ {
		if err := s.Put(fixtureMessage(fmt.Sprintf("quota-%d", i), 1700000000+int64(i)), "live"); !spoolBackpressure(err) {
			t.Fatal(err)
		}
	}
	s.MaxSpoolEntries = 2
	if err := s.Replay(); !spoolBackpressure(err) {
		t.Fatal("batch exceeded count quota", err)
	}
	files, err := os.ReadDir(s.Config.EventsDirectory)
	if err != nil || len(files) != 2 {
		t.Fatal("shared budget did not count newly exported files", len(files), err)
	}
	var count, maxSeq int
	if err = s.db.QueryRow("SELECT COUNT(*),MAX(seq) FROM capture WHERE published=1").Scan(&count, &maxSeq); err != nil || count != 2 || maxSeq != 2 {
		t.Fatal("quota interrupted batch lost oldest-first progress", count, maxSeq, err)
	}
}

func TestSpoolMeasurementToleratesConcurrentConsumerUnlink(t *testing.T) {
	dir := t.TempDir()
	for _, name := range []string{"committed.json", "pending.json"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(`{"event":"fixture"}`), 0600); err != nil {
			t.Fatal(err)
		}
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	// Deterministically exercise the real race window: directory snapshot first,
	// consumer commit/unlink second, per-entry stat last.
	if err = os.Remove(filepath.Join(dir, "committed.json")); err != nil {
		t.Fatal(err)
	}
	usage, err := measureSpool(entries)
	if err != nil || usage.count != 1 || usage.bytes != int64(len(`{"event":"fixture"}`)) {
		t.Fatal("normal consumer drain became a fatal capture error", usage, err)
	}
	if err = os.Symlink("pending.json", filepath.Join(dir, "unexpected-link")); err != nil {
		t.Fatal(err)
	}
	entries, err = os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = measureSpool(entries); err == nil || err.Error() != "spool_symlink" {
		t.Fatal("unlink tolerance also accepted an unsafe directory entry", err)
	}
}

func TestJournalQuotaNeverEvictsUnconsumedSpool(t *testing.T) {
	s := openTestStore(t)
	s.MaxEntries = 1
	if e := s.Put(fixtureMessage("a", 1700000000), "live"); e != nil {
		t.Fatal(e)
	}
	if e := s.Put(fixtureMessage("b", 1700000001), "live"); e == nil || e.Error() != "capture_overload" {
		t.Fatal(e)
	}
	files, _ := os.ReadDir(s.Config.EventsDirectory)
	for _, f := range files {
		os.Remove(filepath.Join(s.Config.EventsDirectory, f.Name()))
	}
	if e := s.Put(fixtureMessage("b", 1700000001), "live"); e != nil {
		t.Fatal("consumed bounded cache not reclaimed", e)
	}
}
func TestEditTargetAndWireTimeStableAcrossReplay(t *testing.T) {
	for _, zone := range []*time.Location{time.UTC, time.FixedZone("source-offset", 8*60*60)} {
		t.Run(zone.String(), func(t *testing.T) {
			s := openTestStore(t)
			original := fixtureMessage("original", 1700000000)
			original.Info.Timestamp = original.Info.Timestamp.In(zone)
			if e := s.Put(original, "live"); e != nil {
				t.Fatal(e)
			}
			edit := fixtureMessage("editid", 1700000100)
			edit.Message = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(), Key: &waCommon.MessageKey{ID: proto.String("original"), FromMe: proto.Bool(false)}, EditedMessage: &waE2E.Message{Conversation: proto.String("edited")}, TimestampMS: proto.Int64(1700000100123)}}
			if e := s.Put(edit, "live"); e != nil {
				t.Fatal(e)
			}
			if e := s.Put(edit, "history"); e != nil {
				t.Fatal(e)
			}
			m, e := s.Get("111111@c.us", LegacyMessageID(original))
			// JSON preserves the instant and offset, not a Location's identity.
			// In UTC, time.Unix's Local and decoded Z's UTC can differ structurally.
			if e != nil || m.Message.GetConversation() != "edited" || !m.Info.Timestamp.Equal(original.Info.Timestamp) {
				t.Fatal(m, e)
			}
			v, e := NormalizeMessage(m, s.Owner)
			if e != nil || v["editedTimestampMs"] != int64(1700000100123) {
				t.Fatal(v, e)
			}
			var n int
			if e = s.db.QueryRow("SELECT COUNT(*) FROM capture").Scan(&n); e != nil || n != 2 {
				t.Fatal("edit replay changed", n, e)
			}
		})
	}
}
func TestGlobalRevokeNoBlankHumanMessage(t *testing.T) {
	s := openTestStore(t)
	m := fixtureMessage("original", 1700000000)
	s.Put(m, "live")
	revoke := fixtureMessage("revokeid", 1700000001)
	revoke.Message = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_REVOKE.Enum(), Key: &waCommon.MessageKey{ID: proto.String("original"), FromMe: proto.Bool(false)}}}
	if e := s.Put(revoke, "live"); e != nil {
		t.Fatal(e)
	}
	if _, e := s.Get("111111@c.us", LegacyMessageID(m)); e == nil {
		t.Fatal("revoked target readable")
	}
	page, e := s.Page("111111@c.us", 10, nil)
	if e != nil || len(page) != 0 {
		t.Fatal(page, e)
	}
}
func TestPairRequiresTerminalBeforeOpeningAuth(t *testing.T) {
	c := testConfig(t)
	if info, _ := os.Stderr.Stat(); info.Mode()&os.ModeCharDevice != 0 {
		t.Skip("running on TTY")
	}
	if e := pairSession(context.Background(), c); e == nil || e.Error() != "owner_terminal_required" {
		t.Fatal(e)
	}
	if _, e := os.Stat(filepath.Join(c.SessionDirectory, "auth.sqlite")); !errors.Is(e, os.ErrNotExist) {
		t.Fatal("opened auth before terminal gate", e)
	}
}
func TestExclusiveNativeSessionLock(t *testing.T) {
	c := testConfig(t)
	one, e := acquire(c.SessionDirectory)
	if e != nil {
		t.Fatal(e)
	}
	defer one.Close()
	if two, e := acquire(c.SessionDirectory); e == nil {
		two.Close()
		t.Fatal("duplicate owner")
	}
}

func TestNativeIPCProcessHelper(t *testing.T) {
	if os.Getenv("ANYMSG_TEST_IPC_PROCESS") != "1" {
		return
	}
	if err := execute(os.Getenv("ANYMSG_TEST_IPC_CONFIG"), false); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	os.Exit(0)
}

func TestNativeProcessExitsWithInheritedBlockingIPC(t *testing.T) {
	for _, stop := range []string{"sigterm", "background-eof", "action-eof", "sigterm-output-backpressure"} {
		t.Run(stop, func(t *testing.T) {
			config := testConfig(t)
			configPath := filepath.Join(filepath.Dir(config.SessionDirectory), "config.json")
			if err := os.WriteFile(configPath, mustJSON(config), 0600); err != nil {
				t.Fatal(err)
			}
			pair, err := syscall.Socketpair(syscall.AF_UNIX, syscall.SOCK_STREAM, 0)
			if err != nil {
				t.Fatal(err)
			}
			syscall.CloseOnExec(pair[0])
			syscall.CloseOnExec(pair[1])
			peerFile := os.NewFile(uintptr(pair[0]), "action-peer")
			childFile := os.NewFile(uintptr(pair[1]), "inherited-action")
			defer childFile.Close()
			// A retained duplicate reproduces Python socket.makefile's extra
			// reference: closing its socket alone does not send the child EOF.
			duplicate, err := syscall.Dup(pair[0])
			if err != nil {
				peerFile.Close()
				t.Fatal(err)
			}
			syscall.CloseOnExec(duplicate)
			retainedPeer := os.NewFile(uintptr(duplicate), "retained-peer-stream")
			defer retainedPeer.Close()
			peer, err := net.FileConn(peerFile)
			peerFile.Close()
			if err != nil {
				t.Fatal(err)
			}
			defer peer.Close()
			cmd := exec.Command(os.Args[0], "-test.run=^TestNativeIPCProcessHelper$")
			cmd.Env = append(os.Environ(), "ANYMSG_TEST_IPC_PROCESS=1", "ANYMSG_TEST_IPC_CONFIG="+configPath, "ANYMSG_WHATSAPP_SEND_FD=3")
			cmd.ExtraFiles = []*os.File{childFile}
			stdin, err := cmd.StdinPipe()
			if err != nil {
				t.Fatal(err)
			}
			defer stdin.Close()
			stdout, err := cmd.StdoutPipe()
			if err != nil {
				t.Fatal(err)
			}
			var stderr bytes.Buffer
			cmd.Stderr = &stderr
			if err = cmd.Start(); err != nil {
				t.Fatal(err)
			}
			childFile.Close()
			done := make(chan error, 1)
			go func() { done <- cmd.Wait() }()
			waited := false
			defer func() {
				if !waited {
					_ = cmd.Process.Kill()
					<-done
				}
			}()
			// Neither request starts a provider or opens auth. Their replies
			// prove both real execute lanes are alive before becoming idle.
			probe := []byte("{\"id\":1,\"method\":\"policy\",\"params\":{}}\n")
			if _, err = stdin.Write(probe); err != nil {
				t.Fatal(err)
			}
			background := make(chan map[string]any, 1)
			go func() {
				var response map[string]any
				_ = json.NewDecoder(stdout).Decode(&response)
				background <- response
			}()
			select {
			case response := <-background:
				if response["error"] != "provider_not_ready" {
					t.Fatal("background probe", response)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("background lane did not answer")
			}
			if err = peer.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
				t.Fatal(err)
			}
			if _, err = peer.Write([]byte("{\"id\":1,\"method\":\"send_text\",\"params\":{}}\n")); err != nil {
				t.Fatal(err)
			}
			var response map[string]any
			if err = json.NewDecoder(peer).Decode(&response); err != nil || response["error"] != "provider_not_ready" {
				t.Fatal("action probe", response, err)
			}
			// Let both idle readers block in the inherited descriptors. They
			// must be cancellable even if their peer remains open indefinitely.
			time.Sleep(50 * time.Millisecond)
			switch stop {
			case "sigterm":
				if err = cmd.Process.Signal(syscall.SIGTERM); err != nil {
					t.Fatal(err)
				}
				peer.Close() // retainedPeer still holds the action endpoint open
			case "background-eof":
				stdin.Close() // action endpoint and retained stream both stay open
			case "action-eof":
				peer.Close()
				retainedPeer.Close() // background stdin writer stays open
			case "sigterm-output-backpressure":
				// Stop consuming stdout and fill both pipes. Shutdown must also
				// interrupt an emitter, not only a reader waiting for more input.
				floodDone := make(chan struct{})
				go func() {
					_, _ = stdin.Write(bytes.Repeat(probe, 10000))
					close(floodDone)
				}()
				defer func() { stdin.Close(); <-floodDone }()
				select {
				case <-floodDone:
					t.Fatal("output backpressure fixture unexpectedly fit in IPC buffers")
				case <-time.After(100 * time.Millisecond):
				}
				if err = cmd.Process.Signal(syscall.SIGTERM); err != nil {
					t.Fatal(err)
				}
			}
			select {
			case err = <-done:
				waited = true
				if err != nil {
					t.Fatalf("process did not exit cleanly: %v; stderr: %s", err, stderr.String())
				}
			case <-time.After(5 * time.Second):
				t.Fatal("process required a kill while an IPC peer remained open")
			}
			if _, err = os.Stat(filepath.Join(config.SessionDirectory, "auth.sqlite")); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("offline lifecycle regression opened auth", err)
			}
		})
	}
}

func TestPageUsesExactCollectorContractAndNeverClaimsComplete(t *testing.T) {
	s := openTestStore(t)
	s.Put(fixtureMessage("a", 1700000000), "live")
	p := &Provider{Config: s.Config, Capture: s}
	p.ready.Store(true)
	savePolicy(t, p, time.Now().Add(time.Minute), Policy{Enabled: true, DiscoverChats: true, IncludeChats: []string{}, ExcludeChats: []string{}})
	// A ready synthetic provider may omit its client. Exercise pure page shaping.
	result, e := p.handle(context.Background(), request{ID: 1, Method: "page", Params: json.RawMessage(`{"chat":"111111@c.us","limit":10}`)}, func(any) error { return nil })
	if e != nil {
		t.Fatal(e)
	}
	v := result.(map[string]any)
	if len(v) != 3 || v["messages"] == nil || v["exhausted"] != false || v["pending"] != true {
		t.Fatal(v)
	}
}
func TestScopedPolicyStillDurablyQueuesHistoryControl(t *testing.T) {
	s := openTestStore(t)
	p := &Provider{Config: s.Config, Capture: s}
	savePolicy(t, p, time.Now().Add(time.Minute), Policy{Enabled: true, IncludeChats: []string{"222222@c.us"}, ExcludeChats: []string{}})
	notification := fixtureMessage("control", 1700000000)
	notification.Message = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{HistorySyncNotification: &waE2E.HistorySyncNotification{FileLength: proto.Uint64(100)}}}
	if e := p.handleMessage(notification); e != nil {
		t.Fatal(e)
	}
	var n int
	s.db.QueryRow("SELECT COUNT(*) FROM history_jobs").Scan(&n)
	if n != 1 {
		t.Fatal("history control discarded by chat scope", n)
	}
	s.db.QueryRow("SELECT COUNT(*) FROM capture").Scan(&n)
	if n != 0 {
		t.Fatal("excluded control message in archive", n)
	}
}

func TestHistoryDownloadRetriesResumeAfterCrashWithPersistedBackoff(t *testing.T) {
	c := testConfig(t)
	s, err := OpenCapture(c)
	if err != nil {
		t.Fatal(err)
	}
	notification := fixtureMessage("retry-control", 1700000000)
	notification.Message = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{HistorySyncNotification: &waE2E.HistorySyncNotification{FileLength: proto.Uint64(100)}}}
	if err = s.EnqueueHistory(notification); err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1700000100, 0)
	first, err := s.claimHistory(now)
	if err != nil || first.Attempt != 1 || first.Next != now.Add(time.Minute).UnixMilli() {
		t.Fatal(first, err)
	}
	// Simulate a crash after only a prefix was durably captured. No failure
	// callback runs. The same notification and its attempt survive reopening.
	if err = s.Put(fixtureMessage("prefix", 1700000000), "history"); err != nil {
		t.Fatal(err)
	}
	s.Close()
	s, err = OpenCapture(c)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if _, err = s.claimHistory(now.Add(30 * time.Second)); !errors.Is(err, sql.ErrNoRows) {
		t.Fatal("retried before persisted backoff", err)
	}
	second, err := s.claimHistory(now.Add(time.Minute))
	if err != nil || second.Attempt != 2 {
		t.Fatal(second, err)
	}
	if err = s.Put(fixtureMessage("prefix", 1700000000), "history"); err != nil {
		t.Fatal(err)
	}
	var count int
	s.db.QueryRow("SELECT COUNT(*) FROM capture").Scan(&count)
	if count != 1 {
		t.Fatal("partial history replay created duplicate", count)
	}
	if err = s.historyFailure(second, "history_download_failed"); err != nil {
		t.Fatal(err)
	}
	third, err := s.claimHistory(time.UnixMilli(second.Next))
	if err != nil || third.Attempt != maxHistoryAttempts {
		t.Fatal(third, err)
	}
	// A crash during the last attempt also reaches a terminal, visible state.
	if _, err = s.claimHistory(time.UnixMilli(third.Next)); !errors.Is(err, sql.ErrNoRows) {
		t.Fatal("unbounded history retry", err)
	}
	var state, reason string
	s.db.QueryRow("SELECT state,error FROM history_jobs WHERE stanza='retry-control'").Scan(&state, &reason)
	if state != "failed" || reason != "history_attempts_exhausted" {
		t.Fatal(state, reason)
	}
	status, err := readPrivate(filepath.Join(c.EventsDirectory, ".history-status"), 4096)
	if err != nil || !strings.Contains(string(status), `"state":"failed"`) {
		t.Fatal(string(status), err)
	}
}

func TestHistoryKnownDownloadFailuresStopAfterThreeAttempts(t *testing.T) {
	s := openTestStore(t)
	n := fixtureMessage("retry-control", 1700000000)
	n.Message = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{HistorySyncNotification: &waE2E.HistorySyncNotification{FileLength: proto.Uint64(100)}}}
	if err := s.EnqueueHistory(n); err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1700000100, 0)
	for i := 1; i <= maxHistoryAttempts; i++ {
		job, err := s.claimHistory(now)
		if err != nil || job.Attempt != i {
			t.Fatal(job, err)
		}
		if err = s.historyFailure(job, "history_download_failed"); err != nil {
			t.Fatal(err)
		}
		now = time.UnixMilli(job.Next)
	}
	if _, err := s.claimHistory(now.Add(time.Hour)); !errors.Is(err, sql.ErrNoRows) {
		t.Fatal("retried a terminal download failure", err)
	}
}

func TestExactOutgoingRetryPayloadSurvivesRestartAndExpires(t *testing.T) {
	c := testConfig(t)
	s, err := OpenCapture(c)
	if err != nil {
		t.Fatal(err)
	}
	chat := types.NewJID("111111", types.DefaultUserServer)
	wire, _ := proto.Marshal(&waE2E.Message{Conversation: proto.String("exact original")})
	b := &nativeEventBuffer{Capture: s}
	if err = b.AddOutgoingEvent(context.Background(), chat, "same-stanza", "wa", wire); err != nil {
		t.Fatal(err)
	}
	s.Close()
	s, err = OpenCapture(c)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	b = &nativeEventBuffer{Capture: s}
	format, got, err := b.GetOutgoingEvent(context.Background(), chat, types.EmptyJID, "same-stanza")
	if err != nil || format != "wa" || !bytes.Equal(got, wire) {
		t.Fatal(format, got, err)
	}
	if _, _, err = b.GetOutgoingEvent(context.Background(), types.NewJID("222222", types.DefaultUserServer), types.EmptyJID, "same-stanza"); !errors.Is(err, sql.ErrNoRows) {
		t.Fatal("cross-chat payload lookup", err)
	}
	s.db.Exec("UPDATE outgoing_retry SET created_at=?", time.Now().Add(-8*24*time.Hour).UnixMilli())
	if _, _, err = b.GetOutgoingEvent(context.Background(), chat, types.EmptyJID, "same-stanza"); !errors.Is(err, sql.ErrNoRows) {
		t.Fatal("expired payload returned", err)
	}
	if err = b.DeleteOldOutgoingEvents(context.Background()); err != nil {
		t.Fatal(err)
	}
	var count int
	s.db.QueryRow("SELECT COUNT(*) FROM outgoing_retry").Scan(&count)
	if count != 0 {
		t.Fatal("expired payload retained", count)
	}
}

func TestOutgoingRetryCacheEnforcesCountAndByteBounds(t *testing.T) {
	s := openTestStore(t)
	b := &nativeEventBuffer{Capture: s}
	chat := types.NewJID("111111", types.DefaultUserServer)
	ctx := context.Background()
	// Populate the count boundary in one transaction, then exercise the actual
	// insertion/eviction path. Oldest content may leave this retry cache only;
	// archive captures and unconsumed spool records use their separate journal.
	tx, _ := s.db.Begin()
	for i := 0; i < maxOutgoingRetryMessages; i++ {
		if _, err := tx.Exec("INSERT INTO outgoing_retry VALUES(?,?,?,?,?)", chat.String(), strconv.Itoa(i), "wa", []byte("old"), time.Now().Add(-time.Minute).UnixMilli()); err != nil {
			t.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	if err := b.AddOutgoingEvent(ctx, chat, "new", "wa", []byte("retained")); err != nil {
		t.Fatal(err)
	}
	var count, size int64
	s.db.QueryRow("SELECT COUNT(*),SUM(length(payload))FROM outgoing_retry").Scan(&count, &size)
	if count != maxOutgoingRetryMessages {
		t.Fatal(count)
	}
	if _, got, err := b.GetOutgoingEvent(ctx, chat, types.EmptyJID, "new"); err != nil || string(got) != "retained" {
		t.Fatal(got, err)
	}
	s.db.Exec("DELETE FROM outgoing_retry")
	tx, _ = s.db.Begin()
	for i := 0; i < maxOutgoingRetryBytes/(512<<10); i++ {
		if _, err := tx.Exec("INSERT INTO outgoing_retry VALUES(?,?,?,?,?)", chat.String(), strconv.Itoa(i), "wa", bytes.Repeat([]byte{1}, 512<<10), time.Now().Add(-time.Minute).UnixMilli()); err != nil {
			t.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	if err := b.AddOutgoingEvent(ctx, chat, "new", "wa", []byte("retained")); err != nil {
		t.Fatal(err)
	}
	s.db.QueryRow("SELECT COUNT(*),SUM(length(payload))FROM outgoing_retry").Scan(&count, &size)
	if size > maxOutgoingRetryBytes || count > maxOutgoingRetryMessages {
		t.Fatal(count, size)
	}
}

func TestProtocolRetryIsFencedByFreshIdentityReadinessAndPolicy(t *testing.T) {
	s := openTestStore(t)
	owner := s.Owner
	p := &Provider{Config: s.Config, Capture: s, Client: nativeClient(&store.Device{ID: &owner})}
	receipt := &events.Receipt{MessageSource: types.MessageSource{Chat: types.NewJID("111111", types.DefaultUserServer)}}
	allow := Policy{Enabled: true, SendEnabled: true, DiscoverChats: true, IncludeChats: []string{}, ExcludeChats: []string{}}
	savePolicy(t, p, time.Now().Add(time.Minute), allow)
	if p.retryAllowed(receipt) {
		t.Fatal("retry before connected")
	}
	p.ready.Store(true)
	if !p.retryAllowed(receipt) {
		t.Fatal("scoped original-stanza retry denied")
	}
	allow.ExcludeChats = []string{"111111@c.us"}
	savePolicy(t, p, time.Now().Add(time.Minute), allow)
	if p.retryAllowed(receipt) {
		t.Fatal("retry after scope revocation")
	}
	allow.ExcludeChats = []string{}
	savePolicy(t, p, time.Now().Add(-time.Second), allow)
	if p.retryAllowed(receipt) {
		t.Fatal("retry with expired policy")
	}
	savePolicy(t, p, time.Now().Add(time.Minute), allow)
	p.Config.ExpectedWID = "222222@c.us"
	if p.retryAllowed(receipt) {
		t.Fatal("retry from wrong account")
	}
}

func TestHistoryDecompressionRejectsBombAndCorruptPayload(t *testing.T) {
	var compressed bytes.Buffer
	writer := zlib.NewWriter(&compressed)
	if _, err := writer.Write(bytes.Repeat([]byte("A"), 1<<20)); err != nil {
		t.Fatal(err)
	}
	writer.Close()
	if err := validateCompressedHistory(compressed.Bytes(), 1024); err == nil || err.Error() != "history_size_limit" {
		t.Fatal("decompression bomb accepted", err)
	}
	if err := validateCompressedHistory(compressed.Bytes(), 1<<20); err != nil {
		t.Fatal("bounded payload rejected", err)
	}
	if err := validateCompressedHistory(compressed.Bytes()[:compressed.Len()-2], 1<<20); err == nil {
		t.Fatal("truncated zlib accepted")
	}
}

func TestDialogsMarkOwnerLIDChatAsSelfChat(t *testing.T) {
	s := openTestStore(t)
	s.OwnerLID = types.NewJID("999900002222", types.HiddenUserServer)
	at := time.Now().UTC().Format(time.RFC3339Nano)
	for _, chat := range []string{"999900002222@lid", "999900003333@lid", "123456@c.us", "777777@c.us"} {
		if _, e := s.db.Exec("INSERT INTO chats(chat,title,observed_at)VALUES(?,?,?)", chat, chat, at); e != nil {
			t.Fatal(e)
		}
	}
	rows, e := s.Dialogs(10, Policy{Enabled: true, DiscoverChats: true, IncludeChats: []string{}, ExcludeChats: []string{}})
	if e != nil {
		t.Fatal(e)
	}
	got := map[string]any{}
	for _, row := range rows {
		v := row.([]any)
		got[v[0].(string)] = v[4].(map[string]any)["is_self_chat"]
	}
	want := map[string]any{"999900002222@lid": true, "999900003333@lid": false, "123456@c.us": true, "777777@c.us": false}
	for chat, self := range want {
		if got[chat] != self {
			t.Fatalf("%s is_self_chat=%v, want %v", chat, got[chat], self)
		}
	}
	// Without a known owner LID, a LID chat stays unknown rather than guessed.
	s.OwnerLID = types.EmptyJID
	rows, _ = s.Dialogs(10, Policy{Enabled: true, DiscoverChats: true, IncludeChats: []string{}, ExcludeChats: []string{}})
	for _, row := range rows {
		v := row.([]any)
		if v[0] == "999900002222@lid" && v[4].(map[string]any)["is_self_chat"] != nil {
			t.Fatal("owner LID guessed without identity evidence")
		}
	}
}

func readStateProvider(t *testing.T) *Provider {
	t.Helper()
	p := skipProvider(t)
	owner := p.Capture.Owner
	p.Client = nativeClient(&store.Device{ID: &owner})
	return p
}

// readCaptures returns the owner read-state and gap envelopes in capture order.
func readCaptures(t *testing.T, s *CaptureStore) []map[string]any {
	t.Helper()
	rows, err := s.db.Query("SELECT envelope FROM capture WHERE message_id LIKE 'read:%' OR message_id LIKE 'gap:%' ORDER BY seq")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	out := []map[string]any{}
	for rows.Next() {
		var data []byte
		var v map[string]any
		if err = rows.Scan(&data); err != nil || json.Unmarshal(data, &v) != nil {
			t.Fatal(err)
		}
		out = append(out, v)
	}
	return out
}

func ownerReceipt(owner types.JID, chat types.JID, typ types.ReceiptType, at int64, ids ...types.MessageID) *events.Receipt {
	return &events.Receipt{MessageSource: types.MessageSource{Chat: chat, Sender: owner, IsFromMe: true}, MessageIDs: ids, Timestamp: time.Unix(at, 0), Type: typ}
}

// spec: ANYM-275
func TestMarkChatAsReadGivesZeroAtActionTimeAndUnreadOnlyAFlag(t *testing.T) {
	p := readStateProvider(t)
	chat := types.NewJID("111111", types.DefaultUserServer)
	at := time.Unix(1700000100, 500)
	if !p.handleReadEvent(&events.MarkChatAsRead{JID: chat, Timestamp: at, Action: &waSyncAction.MarkChatAsReadAction{Read: proto.Bool(true)}}) {
		t.Fatal("mark-read not stored")
	}
	if !p.handleReadEvent(&events.MarkChatAsRead{JID: chat, Timestamp: at.Add(time.Minute), Action: &waSyncAction.MarkChatAsReadAction{Read: proto.Bool(false)}}) {
		t.Fatal("mark-unread not stored")
	}
	got := readCaptures(t, p.Capture)
	if len(got) != 2 {
		t.Fatal(got)
	}
	stamp := at.UTC().Format(time.RFC3339Nano)
	read := got[0]["observation"].(map[string]any)
	if got[0]["kind"] != "owner_read_state" || got[0]["chat"] != "111111@c.us" || got[0]["observed_at"] != stamp || read["observed_at"] != stamp ||
		read["basis"] != "whatsapp_app_state_read" || read["unread_count"] != float64(0) || read["marked_unread"] != false {
		t.Fatal("mark-read", got[0])
	}
	unread := got[1]["observation"].(map[string]any)
	if count, present := unread["unread_count"]; !present || count != nil || unread["marked_unread"] != true || unread["basis"] != "whatsapp_app_state_read" {
		t.Fatal("mark-unread must be a flag without a count", got[1])
	}
	// A replayed app-state action is the same capture.
	p.handleReadEvent(&events.MarkChatAsRead{JID: chat, Timestamp: at, Action: &waSyncAction.MarkChatAsReadAction{Read: proto.Bool(true)}})
	if len(readCaptures(t, p.Capture)) != 2 {
		t.Fatal("replay duplicated the observation")
	}
}

// spec: ANYM-275
func TestOwnDeviceReceiptListsExactlyCapturedIncomingIDs(t *testing.T) {
	p := readStateProvider(t)
	s := p.Capture
	chat := types.NewJID("111111", types.DefaultUserServer)
	for i, id := range []string{"m1", "m2", "m3", "m4"} {
		if err := s.Put(fixtureMessage(id, 1700000000+int64(i)), "live"); err != nil {
			t.Fatal(err)
		}
	}
	out := fixtureMessage("out", 1700000005)
	out.Info.IsFromMe = true
	out.Info.Sender = s.Owner
	if err := s.Put(out, "live"); err != nil {
		t.Fatal(err)
	}
	// Selective receipt: messages 2 and 4 (one listed twice), plus an outgoing and an unknown ID.
	if !p.handleReadEvent(ownerReceipt(s.Owner, chat, types.ReceiptTypeRead, 1700000020, "m4", "m2", "m4", "out", "absent")) {
		t.Fatal("receipt not stored")
	}
	if !p.handleReadEvent(ownerReceipt(s.Owner, chat, types.ReceiptTypeReadSelf, 1700000030, "m1")) {
		t.Fatal("read-self not stored")
	}
	got := readCaptures(t, s)
	if len(got) != 2 {
		t.Fatal(got)
	}
	first := got[0]["observation"].(map[string]any)
	ids := fmt.Sprint(first["read_message_ids"])
	if first["basis"] != "whatsapp_owner_receipt" || ids != "[false_111111@c.us_m2 false_111111@c.us_m4]" || first["observed_at"] != time.Unix(1700000020, 0).UTC().Format(time.RFC3339Nano) {
		t.Fatal("receipt evidence must list exactly 2 and 4", first)
	}
	if count, present := first["unread_count"]; !present || count != nil || first["marked_unread"] != nil {
		t.Fatal("a receipt never carries a count or flag", first)
	}
	if fmt.Sprint(got[1]["observation"].(map[string]any)["read_message_ids"]) != "[false_111111@c.us_m1]" {
		t.Fatal("read-self", got[1])
	}
	// Owner reads never become delivery evidence for the outgoing message.
	if obs, err := s.ReceiptObservations("111111@c.us", LegacyMessageID(out)); err != nil || len(obs) != 0 {
		t.Fatal("own-device receipt became delivery evidence", obs, err)
	}
	// A receipt that lists no retained incoming message still invalidates an
	// older count: it records an empty ID list. A chat with no capture records nothing.
	p.handleReadEvent(ownerReceipt(s.Owner, chat, types.ReceiptTypeRead, 1700000040, "out", "absent"))
	p.handleReadEvent(ownerReceipt(s.Owner, types.NewJID("999999", types.DefaultUserServer), types.ReceiptTypeRead, 1700000041, "x"))
	if got = readCaptures(t, s); len(got) != 3 || fmt.Sprint(got[2]["observation"].(map[string]any)["read_message_ids"]) != "[]" {
		t.Fatal("receipt without retained messages", got)
	}
	// A peer's read receipt on the outgoing message stays delivery evidence only.
	peer := &events.Receipt{MessageSource: types.MessageSource{Chat: chat, Sender: chat}, MessageIDs: []types.MessageID{"out", "m3"}, Timestamp: time.Unix(1700000050, 0), Type: types.ReceiptTypeRead}
	if !p.handleReadEvent(peer) {
		t.Fatal("peer receipt")
	}
	if obs, err := s.ReceiptObservations("111111@c.us", LegacyMessageID(out)); err != nil || len(obs) != 1 || obs[0]["state"] != "read" {
		t.Fatal("peer delivery evidence", obs, err)
	}
	if len(readCaptures(t, s)) != 3 {
		t.Fatal("peer receipt became owner read evidence")
	}
}

// spec: ANYM-275
func TestHistoryUnreadCountIsStampedNoLaterThanNewestMessage(t *testing.T) {
	p := readStateProvider(t)
	chat := "111111@s.whatsapp.net"
	var messages []*waHistorySync.HistorySyncMsg
	for i, id := range []string{"h1", "h2"} {
		messages = append(messages, &waHistorySync.HistorySyncMsg{Message: &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{RemoteJID: proto.String(chat), ID: proto.String(id)}, MessageTimestamp: proto.Uint64(1700000000 + uint64(i)*60), Message: &waE2E.Message{Conversation: proto.String("synthetic " + id)}}})
	}
	data := &waHistorySync.HistorySync{Conversations: []*waHistorySync.Conversation{
		{ID: proto.String(chat), UnreadCount: proto.Uint32(3), ConversationTimestamp: proto.Uint64(1700009999), Messages: messages},
		// Only the flag: the count stays unknown, never a derived zero.
		{ID: proto.String("222222@s.whatsapp.net"), MarkedAsUnread: proto.Bool(true), ConversationTimestamp: proto.Uint64(1700000500)},
		// Neither field: no observation.
		{ID: proto.String("333333@s.whatsapp.net"), ConversationTimestamp: proto.Uint64(1700000500)},
	}}
	before := time.Now()
	if err := p.captureHistory(data); err != nil {
		t.Fatal(err)
	}
	got := readCaptures(t, p.Capture)
	if len(got) != 2 {
		t.Fatal(got)
	}
	count := got[0]["observation"].(map[string]any)
	at, _ := time.Parse(time.RFC3339Nano, count["observed_at"].(string))
	if count["basis"] != "whatsapp_history_sync" || count["unread_count"] != float64(3) || count["marked_unread"] != false ||
		at.After(time.Unix(1700000060, 0)) || !at.Before(before) || got[0]["chat"] != "111111@c.us" {
		t.Fatal("history count must be stamped at the newest message time", got[0])
	}
	flag := got[1]["observation"].(map[string]any)
	if c, present := flag["unread_count"]; !present || c != nil || flag["marked_unread"] != true || flag["observed_at"] != time.Unix(1700000500, 0).UTC().Format(time.RFC3339Nano) {
		t.Fatal("flag-only history", got[1])
	}
}

// spec: ANYM-275
func TestAppStateSyncErrorRecordsAReadStateGap(t *testing.T) {
	p := readStateProvider(t)
	for range 2 {
		if !p.handleReadEvent(&events.AppStateSyncError{Name: "regular_high", Error: errors.New("synthetic")}) {
			t.Fatal("gap not stored")
		}
		time.Sleep(time.Millisecond)
	}
	got := readCaptures(t, p.Capture)
	if len(got) != 2 || got[0]["kind"] != "read_state_gap" || got[0]["reason"] != "app_state_sync_error" || got[0]["account"] != "test" || len(got[0]) != 4 {
		t.Fatal("each gap is its own capture", got)
	}
	if _, err := time.Parse(time.RFC3339Nano, got[0]["observed_at"].(string)); err != nil {
		t.Fatal(err)
	}
}

// spec: ANYM-275
func TestReadStatePathsSendNothingToTheProvider(t *testing.T) {
	// Every read-state path only records; none may call the client (no send,
	// no receipt, no mark-read, no presence).
	files := token.NewFileSet()
	paths := map[string]bool{"handleReadEvent": true, "StoreReceipt": true, "capturedMessage": true, "readState": true,
		"storeOwnerRead": true, "StoreMarkChatAsRead": true, "StoreReadStateGap": true, "captureHistoryReadState": true}
	found := 0
	for _, name := range []string{"runtime.go", "store.go", "history.go"} {
		f, err := parser.ParseFile(files, name, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		for _, decl := range f.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || !paths[fn.Name.Name] {
				continue
			}
			found++
			ast.Inspect(fn.Body, func(n ast.Node) bool {
				if sel, ok := n.(*ast.SelectorExpr); ok && sel.Sel.Name == "Client" {
					t.Errorf("%s reaches the provider client", fn.Name.Name)
				}
				return true
			})
		}
	}
	if found != len(paths) {
		t.Fatal("read-state functions moved", found)
	}
	// The handler runs on a client that never connected: any provider request would fail it.
	p := readStateProvider(t)
	chat := types.NewJID("111111", types.DefaultUserServer)
	if err := p.Capture.Put(fixtureMessage("m1", 1700000000), "live"); err != nil {
		t.Fatal(err)
	}
	for _, event := range []any{ownerReceipt(p.Capture.Owner, chat, types.ReceiptTypeRead, 1700000010, "m1"),
		&events.MarkChatAsRead{JID: chat, Timestamp: time.Unix(1700000020, 0), Action: &waSyncAction.MarkChatAsReadAction{Read: proto.Bool(true)}},
		&events.AppStateSyncError{Name: "regular", Error: errors.New("synthetic")}} {
		if !p.handleReadEvent(event) {
			t.Fatal("read event needed the provider", event)
		}
	}
	if p.Client.IsConnected() || len(readCaptures(t, p.Capture)) != 3 {
		t.Fatal("unexpected provider use or missing evidence")
	}
}

// spec: ANYM-275
func TestReadStateWriteFailureEndsContinuityWithoutStoppingCapture(t *testing.T) {
	p := readStateProvider(t)
	s := p.Capture
	chat := types.NewJID("111111", types.DefaultUserServer)
	if err := s.Put(fixtureMessage("m1", 1700000000), "live"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.db.Exec(`CREATE TRIGGER fail_read BEFORE INSERT ON capture WHEN NEW.message_id LIKE 'read:%' OR NEW.message_id LIKE 'gap:%' BEGIN SELECT RAISE(ABORT,'synthetic'); END`); err != nil {
		t.Fatal(err)
	}
	for _, event := range []any{
		&events.MarkChatAsRead{JID: chat, Timestamp: time.Unix(1700000100, 0), Action: &waSyncAction.MarkChatAsReadAction{Read: proto.Bool(true)}},
		ownerReceipt(s.Owner, chat, types.ReceiptTypeRead, 1700000110, "m1"),
		&events.AppStateSyncError{Name: "regular_high", Error: errors.New("synthetic")},
	} {
		if !p.handleReadEvent(event) {
			t.Fatalf("%T: a read-state write failure must not refuse the event", event)
		}
	}
	if p.failure.Load() != nil || !p.ready.Load() || !p.readGapPending.Load() {
		t.Fatal("read-state failure stopped the provider or lost the gap", p.failure.Load())
	}
	// Message capture keeps working, and the pending gap is written before the next read event.
	if err := s.Put(fixtureMessage("m2", 1700000200), "live"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.db.Exec("DROP TRIGGER fail_read"); err != nil {
		t.Fatal(err)
	}
	p.handleReadEvent(ownerReceipt(s.Owner, chat, types.ReceiptTypeRead, 1700000300, "m2"))
	got := readCaptures(t, s)
	if p.readGapPending.Load() || len(got) != 2 || got[0]["kind"] != "read_state_gap" || got[0]["reason"] != "read_state_write_failed" || got[1]["kind"] != "owner_read_state" {
		t.Fatal("pending gap", got)
	}
	// Disconnect and reconnect gaps use the same capture.
	p.readGap("provider_disconnected")
	p.readGap("provider_reconnected")
	if got = readCaptures(t, s); len(got) != 4 || got[2]["reason"] != "provider_disconnected" || got[3]["reason"] != "provider_reconnected" {
		t.Fatal("connection gaps", got)
	}
}

// spec: ANYM-275
func TestOwnerReadResolvesLocalAliasAndOldRetainedMessages(t *testing.T) {
	p := readStateProvider(t)
	s := p.Capture
	lid := types.NewJID("777777", types.HiddenUserServer)
	pn := types.NewJID("111111", types.DefaultUserServer)
	s.Alias = func(j types.JID) types.JID {
		if j == lid {
			return pn
		}
		return types.EmptyJID
	}
	// The receipt names the oldest of more than 1,000 retained messages.
	for i := range 1005 {
		if err := s.Put(fixtureMessage(fmt.Sprintf("m%d", i), 1700000000+int64(i)), "live"); err != nil {
			t.Fatal(err)
		}
	}
	p.handleReadEvent(ownerReceipt(s.Owner, lid, types.ReceiptTypeRead, 1700002000, "m0"))
	p.handleReadEvent(&events.MarkChatAsRead{JID: lid, Timestamp: time.Unix(1700002100, 0), Action: &waSyncAction.MarkChatAsReadAction{Read: proto.Bool(true)}})
	got := readCaptures(t, s)
	if len(got) != 2 || got[0]["chat"] != "111111@c.us" || fmt.Sprint(got[0]["observation"].(map[string]any)["read_message_ids"]) != "[false_111111@c.us_m0]" || got[1]["chat"] != "111111@c.us" {
		t.Fatal("LID receipt for a PN-captured chat", got)
	}
}

// spec: ANYM-275
func TestOwnerReadSequencesGiveTheSameCapturesInAnyOrder(t *testing.T) {
	// Baseline mark-read at T, incoming A and B, receipts for A (twice) and B, a
	// newer mark-read: the captured evidence is the same set in any order.
	chat := types.NewJID("111111", types.DefaultUserServer)
	run := func(order []int) []string {
		p := readStateProvider(t)
		s := p.Capture
		for i, id := range []string{"a", "b"} {
			if err := s.Put(fixtureMessage(id, 1700000100+int64(i)), "live"); err != nil {
				t.Fatal(err)
			}
		}
		read := func(at int64) any {
			return &events.MarkChatAsRead{JID: chat, Timestamp: time.Unix(at, 0), Action: &waSyncAction.MarkChatAsReadAction{Read: proto.Bool(true)}}
		}
		evs := []any{read(1700000000), ownerReceipt(s.Owner, chat, types.ReceiptTypeRead, 1700000200, "a"),
			ownerReceipt(s.Owner, chat, types.ReceiptTypeRead, 1700000200, "a"), ownerReceipt(s.Owner, chat, types.ReceiptTypeRead, 1700000300, "b"), read(1700000400)}
		for _, i := range order {
			if !p.handleReadEvent(evs[i]) {
				t.Fatal("event", i)
			}
		}
		out := []string{}
		for _, v := range readCaptures(t, s) {
			out = append(out, string(mustJSON(v["observation"])))
		}
		sort.Strings(out)
		return out
	}
	want := run([]int{0, 1, 2, 3, 4})
	if len(want) != 4 || !strings.Contains(strings.Join(want, "\n"), `"read_message_ids":["false_111111@c.us_a"]`) || strings.Contains(strings.Join(want, "\n"), `"unread_count":2`) {
		t.Fatal("in-order evidence", want)
	}
	for _, order := range [][]int{{3, 1, 0, 2, 4}, {4, 2, 3, 1, 0, 1}} {
		if got := run(order); fmt.Sprint(got) != fmt.Sprint(want) {
			t.Fatal("order changed the evidence", order, got, want)
		}
	}
}

// spec: ANYM-275
func TestGroupReceiptsMatchCapturesWithTheSenderSuffix(t *testing.T) {
	p := readStateProvider(t)
	s := p.Capture
	group := types.NewJID("123-456", types.GroupServer)
	member := types.NewJID("222222", types.DefaultUserServer)
	in := fixtureMessage("g1", 1700000000)
	in.Info.Chat, in.Info.Sender, in.Info.IsGroup = group, member, true
	out := fixtureMessage("g2", 1700000001)
	out.Info.Chat, out.Info.Sender, out.Info.IsGroup, out.Info.IsFromMe = group, s.Owner, true, true
	for _, m := range []*events.Message{in, out} {
		if err := s.Put(m, "live"); err != nil {
			t.Fatal(err)
		}
	}
	p.handleReadEvent(ownerReceipt(s.Owner, group, types.ReceiptTypeRead, 1700000100, "g1"))
	got := readCaptures(t, s)
	if len(got) != 1 || fmt.Sprint(got[0]["observation"].(map[string]any)["read_message_ids"]) != "["+LegacyMessageID(in)+"]" {
		t.Fatal("group owner read", got)
	}
	peer := &events.Receipt{MessageSource: types.MessageSource{Chat: group, Sender: member, IsGroup: true}, MessageIDs: []types.MessageID{"g2"}, Timestamp: time.Unix(1700000200, 0), Type: types.ReceiptTypeRead}
	if !p.handleReadEvent(peer) {
		t.Fatal("peer receipt")
	}
	if obs, err := s.ReceiptObservations(LegacyChatID(group), LegacyMessageID(out)); err != nil || len(obs) != 1 {
		t.Fatal("group delivery evidence", obs, err)
	}
}
