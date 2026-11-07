package main

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

// journalUnknown stores a row the way the classifier before ANYM-471 did: the
// raw protobuf is kept, the envelope says kind unknown with no body or media.
func journalUnknown(t *testing.T, s *CaptureStore, e *events.Message, raw []byte) {
	t.Helper()
	envelope, err := captureEnvelope(s.Config.Account, e, s.Owner, "live", s.OwnerLID)
	if err != nil {
		t.Fatal(err)
	}
	event := envelope["event"].(map[string]any)
	event["messageType"], event["body"], event["hasMedia"], event["isService"] = "unknown", "", false, false
	event["mediaKind"], event["filename"], event["mimetype"], event["filesize"] = nil, nil, nil, nil
	if raw == nil {
		if raw, err = marshalMessage(e); err != nil {
			t.Fatal(err)
		}
	}
	if err = s.append(LegacyChatID(e.Info.Chat), LegacyMessageID(e), e.Info.Timestamp.Unix(), raw, envelope, false); err != nil {
		t.Fatal(err)
	}
}

type repairFixture struct {
	name string
	msg  *events.Message
	raw  []byte // nil: the marshaled message; empty: emptied raw
}

func unknownWireMessage() *waE2E.Message {
	m := &waE2E.Message{}
	m.ProtoReflect().SetUnknown(protowire.AppendBytes(protowire.AppendTag(nil, 9999, protowire.BytesType), []byte{1}))
	return m
}

func repairFixtures() []repairFixture {
	with := func(id string, m *waE2E.Message) *events.Message {
		e := fixtureMessage(id, 1700000000)
		e.Message, e.RawMessage = m, m
		return e
	}
	return []repairFixture{
		{"album_child", with("R1", &waE2E.Message{AssociatedChildMessage: &waE2E.FutureProofMessage{Message: &waE2E.Message{ImageMessage: &waE2E.ImageMessage{Mimetype: proto.String("image/jpeg"), FileLength: proto.Uint64(8)}}}}), nil},
		{"template_text", with("R2", &waE2E.Message{TemplateMessage: &waE2E.TemplateMessage{Format: &waE2E.TemplateMessage_HydratedFourRowTemplate_{HydratedFourRowTemplate: &waE2E.TemplateMessage_HydratedFourRowTemplate{HydratedContentText: proto.String("Your code is 0000")}}}}), nil},
		{"album_parent", with("R3", &waE2E.Message{AlbumMessage: &waE2E.AlbumMessage{ExpectedImageCount: proto.Uint32(2)}}), nil},
		{"history_notice", with("R4", &waE2E.Message{MessageHistoryNotice: &waE2E.MessageHistoryNotice{}}), nil},
		{"placeholder", with("R5", &waE2E.Message{PlaceholderMessage: &waE2E.PlaceholderMessage{Type: waE2E.PlaceholderMessage_MASK_LINKED_DEVICES.Enum()}}), nil},
		{"sender_key_only", groupFixture("R6", senderKeyPart()), nil},
		{"emptied_raw", with("R7", &waE2E.Message{AlbumMessage: &waE2E.AlbumMessage{ExpectedImageCount: proto.Uint32(3)}}), []byte{}},
		{"still_unknown", with("R8", unknownWireMessage()), nil},
		{"corrupt_raw", with("R9", unknownWireMessage()), []byte("{not a saved message")},
	}
}

// The states each fixture must reach under REPAIR_VERSION. Changing a fixture's
// result without bumping the version fails TestRepairGoldenStatesArePinnedToTheVersion.
const repairGoldenVersion = 1

var repairGolden = map[string]string{
	"album_child": "would_add_content", "template_text": "would_add_content",
	"album_parent": "label_only", "history_notice": "label_only",
	"placeholder": "placeholder", "sender_key_only": "sender_key_only", "emptied_raw": "no_raw",
	"still_unknown": "same", "corrupt_raw": "parse_error",
}

type journalSnapshot struct {
	rows  string
	spool []string
}

func snapshotJournal(t *testing.T, s *CaptureStore) journalSnapshot {
	t.Helper()
	rows, err := s.db.Query("SELECT seq,digest,raw,envelope,published FROM capture ORDER BY seq")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var b bytes.Buffer
	for rows.Next() {
		var seq int64
		var digest string
		var raw, envelope []byte
		var published int
		if err = rows.Scan(&seq, &digest, &raw, &envelope, &published); err != nil {
			t.Fatal(err)
		}
		b.WriteString(digest)
		b.Write(raw)
		b.Write(envelope)
		b.WriteByte(byte(published))
	}
	entries, err := os.ReadDir(s.Config.EventsDirectory)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, entry := range entries {
		if entry.Name() != ".repair-status" {
			names = append(names, entry.Name())
		}
	}
	sort.Strings(names)
	return journalSnapshot{b.String(), names}
}

func runRepairToEnd(t *testing.T, p *repairPass) {
	t.Helper()
	for i := 0; i < 1000 && !p.done; i++ {
		p.tick(context.Background())
		if p.lastError != "" {
			t.Fatal("repair pass error", p.lastError)
		}
	}
	if !p.done {
		t.Fatal("repair pass did not finish")
	}
}

func readRepairStatus(t *testing.T, s *CaptureStore) (map[string]any, []byte) {
	t.Helper()
	raw, err := readPrivate(filepath.Join(s.Config.EventsDirectory, ".repair-status"), 4096)
	if err != nil {
		t.Fatal(err)
	}
	var status map[string]any
	if err = json.Unmarshal(raw, &status); err != nil {
		t.Fatal(err)
	}
	return status, raw
}

// spec: ANYM-809
func TestRepairCountTalliesJournalRowsAndRewritesNothing(t *testing.T) {
	s := openTestStore(t)
	for _, f := range repairFixtures() {
		journalUnknown(t, s, f.msg, f.raw)
	}
	// A row the current classifier already archived is not a candidate.
	if err := s.Put(fixtureMessage("TEXT", 1700000001), "live"); err != nil {
		t.Fatal(err)
	}
	before := snapshotJournal(t, s)
	p := newRepairPass(s)
	runRepairToEnd(t, p)
	want := map[string]int64{}
	for _, state := range repairGolden {
		want[state]++
	}
	for _, state := range repairStates {
		if p.counts[state] != want[state] {
			t.Fatalf("%s = %d, want %d (all %v)", state, p.counts[state], want[state], p.counts)
		}
	}
	if p.scanned != int64(len(repairGolden)) {
		t.Fatalf("scanned %d, want %d", p.scanned, len(repairGolden))
	}
	after := snapshotJournal(t, s)
	if after.rows != before.rows || strings.Join(after.spool, ",") != strings.Join(before.spool, ",") {
		t.Fatal("the count changed the journal or published something")
	}
	status, raw := readRepairStatus(t, s)
	keys := []string{}
	for k := range status {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	if strings.Join(keys, ",") != "counts,done,last_error,observed_at,rows_scanned,version" || status["done"] != true || status["version"] != float64(REPAIR_VERSION) {
		t.Fatal("unexpected repair status", string(raw))
	}
	if counts := status["counts"].(map[string]any); len(counts) != len(repairStates) || counts["would_add_content"] != float64(2) {
		t.Fatal("unexpected repair counts", string(raw))
	}
	if bytes.Contains(raw, []byte("Your code")) || bytes.Contains(raw, []byte("111111")) {
		t.Fatal("repair status carries message content or an ID")
	}
}

// spec: ANYM-809
func TestRepairParsePanicIsParseErrorAndThePassGoesOn(t *testing.T) {
	s := openTestStore(t)
	for _, f := range repairFixtures()[:3] {
		journalUnknown(t, s, f.msg, f.raw)
	}
	p := newRepairPass(s)
	calls := 0
	p.classify = func(r repairRow) string {
		if calls++; calls == 1 {
			panic("synthetic parser panic")
		}
		return s.repairState(r)
	}
	runRepairToEnd(t, p)
	if p.counts["parse_error"] != 1 || p.scanned != 3 || p.counts["would_add_content"] != 1 || p.counts["label_only"] != 1 {
		t.Fatalf("unexpected counts after a panicking row: %v scanned=%d", p.counts, p.scanned)
	}
	// The classifier itself also recovers: a nil store panics inside it.
	var r repairRow
	if err := s.db.QueryRow("SELECT seq,envelope,raw FROM capture ORDER BY seq LIMIT 1").Scan(&r.Seq, &r.Envelope, &r.Raw); err != nil {
		t.Fatal(err)
	}
	var broken *CaptureStore
	if got := broken.repairState(r); got != "parse_error" {
		t.Fatalf("a panic inside the classifier = %q, want parse_error", got)
	}
}

// spec: ANYM-809
func TestRepairGoldenStatesArePinnedToTheVersion(t *testing.T) {
	if REPAIR_VERSION != repairGoldenVersion {
		t.Fatalf("REPAIR_VERSION %d: update repairGolden and repairGoldenVersion together", REPAIR_VERSION)
	}
	s := openTestStore(t)
	for _, f := range repairFixtures() {
		journalUnknown(t, s, f.msg, f.raw)
	}
	rows, err := s.repairRows(context.Background(), 0, 1<<40, 100)
	if err != nil {
		t.Fatal(err)
	}
	fixtures := repairFixtures()
	if len(rows) != len(fixtures) {
		t.Fatalf("selected %d rows, want %d", len(rows), len(fixtures))
	}
	for i, r := range rows {
		if got := s.repairState(r); got != repairGolden[fixtures[i].name] {
			t.Fatalf("%s = %s, golden %s: a parser change needs a REPAIR_VERSION bump", fixtures[i].name, got, repairGolden[fixtures[i].name])
		}
	}
}

// spec: ANYM-809
func TestRepairResumesAfterLastSeqAndAFinishedVersionDoesNotRunAgain(t *testing.T) {
	s := openTestStore(t)
	f := repairFixtures()[2]
	for i := 0; i < repairBatchRows+10; i++ {
		e := fixtureMessage("P"+string(rune('a'+i%26))+string(rune('a'+i/26)), 1700000000+int64(i))
		e.Message, e.RawMessage = f.msg.Message, f.msg.Message
		journalUnknown(t, s, e, nil)
	}
	// A progress row of an older version is removed when this version starts.
	if _, err := s.db.Exec(`CREATE TABLE repair_progress(version INTEGER PRIMARY KEY,last_seq INTEGER NOT NULL,done INTEGER NOT NULL,counts TEXT NOT NULL)`); err != nil {
		t.Fatal(err)
	}
	if _, err := s.db.Exec("INSERT INTO repair_progress VALUES(0,99,1,'{}')"); err != nil {
		t.Fatal(err)
	}
	first := newRepairPass(s)
	first.tick(context.Background())
	if first.lastError != "" || first.scanned != repairBatchRows || first.done {
		t.Fatalf("first tick: scanned=%d done=%v err=%s", first.scanned, first.done, first.lastError)
	}
	var versions int
	s.db.QueryRow("SELECT COUNT(*) FROM repair_progress").Scan(&versions)
	if versions != 1 {
		t.Fatal("older version progress kept")
	}
	// Restart: a new pass resumes after last_seq and counts each row once.
	second := newRepairPass(s)
	runRepairToEnd(t, second)
	if second.scanned != repairBatchRows+10 || second.counts["label_only"] != repairBatchRows+10 {
		t.Fatalf("resumed pass: scanned=%d counts=%v", second.scanned, second.counts)
	}
	third := newRepairPass(s)
	third.classify = func(repairRow) string { t.Fatal("a finished version ran again"); return "" }
	journalUnknown(t, s, func() *events.Message {
		e := fixtureMessage("LATE", 1700001000)
		e.Message, e.RawMessage = f.msg.Message, f.msg.Message
		return e
	}(), nil)
	third.tick(context.Background())
	if !third.done || third.scanned != repairBatchRows+10 {
		t.Fatal("finished version not reloaded as done")
	}
}

// spec: ANYM-809
func TestRepairStalledJournalReadIsAStatusCodeAndCaptureContinues(t *testing.T) {
	s := openTestStore(t)
	journalUnknown(t, s, repairFixtures()[1].msg, nil)
	p := newRepairPass(s)
	p.stmt = 100 * time.Millisecond
	p.read = func(ctx context.Context, after, upto int64, limit int) ([]repairRow, error) {
		// A stalled read holds neither the capture lock nor the connection.
		done := make(chan error, 1)
		go func() { done <- s.Put(fixtureMessage("DURING", 1700000500), "live") }()
		select {
		case err := <-done:
			if err != nil {
				t.Error("capture failed during a stalled repair read:", err)
			}
		case <-time.After(5 * time.Second):
			t.Error("capture blocked by a stalled repair read")
		}
		<-ctx.Done()
		return nil, ctx.Err()
	}
	started := time.Now()
	p.tick(context.Background())
	if time.Since(started) > 3*time.Second {
		t.Fatal("stalled read not bounded")
	}
	if p.lastError != "repair_read" || p.lastSeq != 0 || p.scanned != 0 {
		t.Fatalf("stalled read: err=%q last_seq=%d", p.lastError, p.lastSeq)
	}
	status, raw := readRepairStatus(t, s)
	if status["last_error"] != "repair_read" || status["done"] != false {
		t.Fatal("unexpected status", string(raw))
	}
}

// spec: ANYM-809
func TestRepairYieldsToQueuedCaptureAndKeepsCaptureOrderAndLatency(t *testing.T) {
	s := openTestStore(t)
	album := repairFixtures()[2].msg.Message
	for i := 0; i < 400; i++ {
		e := fixtureMessage("Q"+strings.Repeat("x", i%7)+string(rune('A'+i%26))+string(rune('A'+i/26)), 1700000000+int64(i))
		e.Message, e.RawMessage = album, album
		journalUnknown(t, s, e, nil)
	}
	p := newRepairPass(s)
	// An unexported journal row is queued capture work: the pass waits.
	if _, err := s.db.Exec("UPDATE capture SET published=0 WHERE seq=(SELECT MAX(seq) FROM capture)"); err != nil {
		t.Fatal(err)
	}
	p.tick(context.Background())
	if p.scanned != 0 || p.lastError != "" {
		t.Fatal("pass ran while capture work was queued")
	}
	if err := s.Replay(); err != nil {
		t.Fatal(err)
	}
	// A due history download is queued capture work too.
	enqueueHistoryFixture(t, s, "H1")
	p.tick(context.Background())
	if p.scanned != 0 {
		t.Fatal("pass ran while a history download was due")
	}
	if _, err := s.db.Exec("DELETE FROM history_jobs"); err != nil {
		t.Fatal(err)
	}
	stop := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
				p.tick(context.Background())
			}
		}
	}()
	var worst time.Duration
	ids := []string{}
	for i := 0; i < 40; i++ {
		e := fixtureMessage("LIVE"+string(rune('A'+i%26))+string(rune('A'+i/26)), 1700100000+int64(i))
		started := time.Now()
		if err := s.Put(e, "live"); err != nil {
			t.Fatal(err)
		}
		worst = max(worst, time.Since(started))
		ids = append(ids, LegacyMessageID(e))
	}
	close(stop)
	wg.Wait()
	if worst > 2*time.Second {
		t.Fatal("capture latency under the repair pass", worst)
	}
	rows, err := s.db.Query("SELECT message_id FROM capture WHERE message_id LIKE '%LIVE%' ORDER BY seq")
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for rows.Next() {
		var id string
		rows.Scan(&id)
		got = append(got, id)
	}
	rows.Close()
	if strings.Join(got, ",") != strings.Join(ids, ",") {
		t.Fatal("capture order changed under the repair pass")
	}
	runRepairToEnd(t, p)
	if p.scanned != 400 || p.counts["label_only"] != 400 {
		t.Fatalf("count under contention: scanned=%d counts=%v", p.scanned, p.counts)
	}
}

// spec: ANYM-809
func TestRepairProgressAtThePageCeilingFailsClosedAndCaptureStillInserts(t *testing.T) {
	s := openTestStore(t)
	for i := 0; i < 20; i++ {
		journalUnknown(t, s, fixtureMessage("FULL"+string(rune('A'+i)), 1700000000+int64(i)), nil)
	}
	// The archive consumer drained the spool, so capture may evict published rows.
	entries, _ := os.ReadDir(s.Config.EventsDirectory)
	for _, entry := range entries {
		if strings.HasSuffix(entry.Name(), ".json") {
			os.Remove(filepath.Join(s.Config.EventsDirectory, entry.Name()))
		}
	}
	var pages int64
	if err := s.db.QueryRow("PRAGMA page_count").Scan(&pages); err != nil {
		t.Fatal(err)
	}
	if _, err := s.db.Exec("PRAGMA max_page_count=" + strconv.FormatInt(pages, 10)); err != nil {
		t.Fatal(err)
	}
	p := newRepairPass(s)
	p.tick(context.Background())
	if p.lastError != "repair_storage" || p.loaded || p.scanned != 0 {
		t.Fatalf("progress write at the ceiling: err=%q loaded=%v", p.lastError, p.loaded)
	}
	status, raw := readRepairStatus(t, s)
	if status["last_error"] != "repair_storage" {
		t.Fatal("unexpected status", string(raw))
	}
	var count int
	s.db.QueryRow("SELECT COUNT(*) FROM capture").Scan(&count)
	s.MaxEntries = count
	if err := s.Put(fixtureMessage("FULLZ", 1700000100), "live"); err != nil {
		t.Fatal("capture insert failed at the ceiling after the repair pass:", err)
	}
}
