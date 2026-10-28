package main

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	waCommon "go.mau.fi/whatsmeow/proto/waCommon"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	waHistorySync "go.mau.fi/whatsmeow/proto/waHistorySync"
	waWeb "go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

func skipProvider(t *testing.T) *Provider {
	t.Helper()
	s := openTestStore(t)
	p := &Provider{Config: s.Config, Capture: s}
	savePolicy(t, p, time.Now().Add(time.Minute), Policy{Enabled: true, DiscoverChats: true, IncludeChats: []string{}, ExcludeChats: []string{}})
	p.ready.Store(true)
	return p
}

func skipCounts(t *testing.T, s *CaptureStore) map[string]int {
	t.Helper()
	rows, err := s.db.Query("SELECT code,count FROM capture_skips")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	out := map[string]int{}
	for rows.Next() {
		var code string
		var n int
		if err = rows.Scan(&code, &n); err != nil {
			t.Fatal(err)
		}
		out[code] = n
	}
	return out
}

func spoolFiles(t *testing.T, dir string) int {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	n := 0
	for _, entry := range entries {
		if strings.HasSuffix(entry.Name(), ".json") {
			n++
		}
	}
	return n
}

func TestInvalidLiveMessageIsAcknowledgedAndCountedWithoutFailingCapture(t *testing.T) {
	p := skipProvider(t)
	revoke := fixtureMessage("revoke", 1700000000)
	revoke.Message = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_REVOKE.Enum(), Key: &waCommon.MessageKey{}}}
	notification := fixtureMessage("history", 1700000000)
	notification.Message = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{HistorySyncNotification: &waE2E.HistorySyncNotification{FileLength: proto.Uint64(maxCompressedHistory + 1)}}}
	for _, m := range []*events.Message{fixtureMessage("nul\x00id", 1700000000), fixtureMessage("future", maxEnvelopeUnix+1), revoke, notification} {
		// nil means the event handler returns true: the message is ACKed once.
		if err := p.handleMessage(m); err != nil {
			t.Fatal("per-message validation stopped capture", err)
		}
	}
	if !p.ready.Load() {
		t.Fatal("provider marked unready")
	}
	if got := skipCounts(t, p.Capture); got["invalid_provider_message"] != 3 || got["invalid_history_notification"] != 1 || len(got) != 2 {
		t.Fatal(got)
	}
	if n := spoolFiles(t, p.Config.EventsDirectory); n != 0 {
		t.Fatal("invalid message exported", n)
	}
	var jobs int
	p.Capture.db.QueryRow("SELECT COUNT(*) FROM history_jobs").Scan(&jobs)
	if jobs != 0 {
		t.Fatal("invalid history notification queued", jobs)
	}
	// Store failures are not validation: they still surface to captureFailure.
	p.Capture.MaxEntries = 0
	if err := p.handleMessage(fixtureMessage("valid", 1700000000)); err == nil || err.Error() != "capture_overload" {
		t.Fatal("store failure was skipped", err)
	}
}

func TestLongNonLatinBodyIsMeasuredInCharacters(t *testing.T) {
	p := skipProvider(t)
	m := fixtureMessage("cyrillic", 1700000000)
	// 40000 characters but 80000 UTF-8 bytes: within the archive's 65536 limit.
	m.Message = &waE2E.Message{Conversation: proto.String(strings.Repeat("я", 40000))}
	if err := p.handleMessage(m); err != nil {
		t.Fatal(err)
	}
	if n := spoolFiles(t, p.Config.EventsDirectory); n != 1 || len(skipCounts(t, p.Capture)) != 0 {
		t.Fatal("long non-Latin body not captured", n)
	}
	long := fixtureMessage("long", 1700000001)
	long.Message = &waE2E.Message{Conversation: proto.String(strings.Repeat("я", 70000))}
	if err := p.handleMessage(long); err != nil {
		t.Fatal(err)
	}
	v, err := NormalizeMessage(long, p.Capture.Owner)
	if err != nil || len([]rune(v["body"].(string))) != maxEnvelopeBody {
		t.Fatal("over-long body not truncated", err)
	}
}

func TestOversizedNativeEvidenceStillExportsBoundedEnvelope(t *testing.T) {
	p := skipProvider(t)
	m := fixtureMessage("quoted", 1700000000)
	quoted := &waE2E.Message{Conversation: proto.String(strings.Repeat("文", 65536))}
	m.Message = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String(strings.Repeat("я", 60000)), ContextInfo: &waE2E.ContextInfo{StanzaID: proto.String("orig"), QuotedMessage: quoted}}}
	m.RawMessage = m.Message
	raw, _ := marshalMessage(m)
	envelope, _ := captureEnvelope("test", m, p.Capture.Owner, "live")
	if data, _ := json.Marshal(envelope); len(data)+len(raw) <= maxCaptureEnvelopeBytes || len(data) > maxCaptureEnvelopeBytes {
		t.Fatal("fixture does not exceed only the local evidence bound", len(data), len(raw))
	}
	if err := p.handleMessage(m); err != nil {
		t.Fatal("oversized native evidence stopped capture", err)
	}
	if n := spoolFiles(t, p.Config.EventsDirectory); n != 1 {
		t.Fatal("bounded envelope not exported", n)
	}
	var retained int
	p.Capture.db.QueryRow("SELECT length(raw) FROM capture").Scan(&retained)
	if retained != 0 {
		t.Fatal("oversized evidence retained", retained)
	}
}

func TestHistoryBatchSkipsInvalidItemsAndCapturesTheRest(t *testing.T) {
	p := skipProvider(t)
	owner := p.Capture.Owner
	p.Client = nativeClient(&store.Device{ID: &owner})
	chat := "111111@s.whatsapp.net"
	valid := func(id string) *waHistorySync.HistorySyncMsg {
		return &waHistorySync.HistorySyncMsg{Message: &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{RemoteJID: proto.String(chat), ID: proto.String(id)}, MessageTimestamp: proto.Uint64(1700000000), Message: &waE2E.Message{Conversation: proto.String("synthetic")}}}
	}
	data := &waHistorySync.HistorySync{Conversations: []*waHistorySync.Conversation{
		{ID: proto.String("1.2.3@s.whatsapp.net")},
		{ID: proto.String(chat), Messages: []*waHistorySync.HistorySyncMsg{
			valid("before"),
			{Message: &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{RemoteJID: proto.String(chat)}, MessageTimestamp: proto.Uint64(1700000000)}},
			{Message: &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{RemoteJID: proto.String(chat), ID: proto.String("nul\x00")}, MessageTimestamp: proto.Uint64(1700000000), Message: &waE2E.Message{Conversation: proto.String("x")}}},
			valid("after"),
		}},
	}}
	if err := p.captureHistory(data); err != nil {
		t.Fatal("one invalid history item stopped the batch", err)
	}
	if got := skipCounts(t, p.Capture); got["invalid_history_chat"] != 1 || got["invalid_history_message"] != 1 || got["invalid_provider_message"] != 1 {
		t.Fatal(got)
	}
	var n int
	p.Capture.db.QueryRow("SELECT COUNT(*) FROM capture").Scan(&n)
	if n != 2 {
		t.Fatal("valid history around invalid items not captured", n)
	}
}

func TestPageOmitsRetainedRevisionTheArchiveCannotRepresent(t *testing.T) {
	p := skipProvider(t)
	if err := p.Capture.Put(fixtureMessage("good", 1700000000), "live"); err != nil {
		t.Fatal(err)
	}
	raw, err := marshalMessage(fixtureMessage("bad\x00", 1700000001))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = p.Capture.db.Exec("INSERT INTO capture(digest,chat,message_id,source_ts,raw,envelope,published,created_at)VALUES('legacy','111111@c.us','legacy',1700000001,?,'{}',1,0)", raw); err != nil {
		t.Fatal(err)
	}
	result, err := p.handle(context.Background(), request{ID: 1, Method: "page", Params: json.RawMessage(`{"chat":"111111@c.us","limit":10}`)}, func(any) error { return nil })
	if err != nil {
		t.Fatal("one unrepresentable revision made the chat unreadable", err)
	}
	messages := result.(map[string]any)["messages"].([]any)
	if len(messages) != 1 || messages[0].(map[string]any)["messageId"] != "false_111111@c.us_good" {
		t.Fatal(messages)
	}
}

func TestSkipCounterIsContentFree(t *testing.T) {
	s := openTestStore(t)
	if err := s.skip(errors.New("invalid_provider_message")); err != nil {
		t.Fatal(err)
	}
	if err := s.skip(errors.New("disk_reserve")); err == nil {
		t.Fatal("non-validation failure recorded as a skip")
	}
	var code string
	var count int
	if err := s.db.QueryRow("SELECT code,count FROM capture_skips").Scan(&code, &count); err != nil || code != "invalid_provider_message" || count != 1 {
		t.Fatal(code, count, err)
	}
	if _, err := os.Stat(filepath.Join(s.Config.EventsDirectory, ".transport-failure")); !os.IsNotExist(err) {
		t.Fatal("skip recorded a transport failure", err)
	}
}

// spec: ANYM-387
func TestHistoryBatchCountsMissingOrOutOfRangeTimestampAsInvalidMessage(t *testing.T) {
	p := skipProvider(t)
	owner := p.Capture.Owner
	p.Client = nativeClient(&store.Device{ID: &owner})
	chat := "111111@s.whatsapp.net"
	item := func(id string, stamp *uint64) *waHistorySync.HistorySyncMsg {
		return &waHistorySync.HistorySyncMsg{Message: &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{RemoteJID: proto.String(chat), ID: proto.String(id)}, MessageTimestamp: stamp, Message: &waE2E.Message{Conversation: proto.String("synthetic")}}}
	}
	data := &waHistorySync.HistorySync{Conversations: []*waHistorySync.Conversation{{ID: proto.String(chat), Messages: []*waHistorySync.HistorySyncMsg{
		item("before", proto.Uint64(1700000000)), item("missing", nil), item("future", proto.Uint64(maxEnvelopeUnix+1)), item("after", proto.Uint64(1700000001)),
	}}}}
	if err := p.captureHistory(data); err != nil {
		t.Fatal("an invalid timestamp stopped the batch", err)
	}
	if got := skipCounts(t, p.Capture); got["invalid_history_message"] != 2 || len(got) != 1 {
		t.Fatal(got)
	}
	var n int
	p.Capture.db.QueryRow("SELECT COUNT(*) FROM capture WHERE message_id IN('false_111111@c.us_before','false_111111@c.us_after')").Scan(&n)
	if n != 2 {
		t.Fatal("valid history around invalid timestamps not captured", n)
	}
}
