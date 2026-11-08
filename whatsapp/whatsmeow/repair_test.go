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
	var rows []repairRow
	for seq := int64(1); seq <= int64(len(repairFixtures())); seq++ {
		r, ok, err := s.repairRow(context.Background(), seq)
		if err != nil || !ok {
			t.Fatal(err, ok)
		}
		if selected, _, _ := repairCandidate(r.Envelope); selected {
			rows = append(rows, r)
		}
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
	for i := 0; i < repairWindowSeqs+10; i++ {
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
	if first.lastError != "" || first.scanned != repairWindowSeqs || first.done {
		t.Fatalf("first tick: scanned=%d done=%v err=%s", first.scanned, first.done, first.lastError)
	}
	var versions int
	s.db.QueryRow("SELECT COUNT(*) FROM repair_progress").Scan(&versions)
	if versions != 1 {
		t.Fatal("older version progress kept")
	}
	// Restart: a new pass resumes after last_seq and counts each row once.
	second := newRepairPass(s)
	row := second.row
	reads := 0
	second.row = func(ctx context.Context, seq int64) (repairRow, bool, error) {
		reads++
		return row(ctx, seq)
	}
	for i := 0; i < 1000 && !second.done; i++ {
		reads = 0
		second.tick(context.Background())
		if second.lastError != "" || reads > repairWindowSeqs || repairWindowSeqs > repairBatchRows {
			t.Fatalf("one tick read %d journal rows (err %q)", reads, second.lastError)
		}
	}
	if second.scanned != repairWindowSeqs+10 || second.counts["label_only"] != repairWindowSeqs+10 {
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
	if !third.done || third.scanned != repairWindowSeqs+10 {
		t.Fatal("finished version not reloaded as done")
	}
}

// spec: ANYM-809
func TestRepairStalledJournalReadIsAStatusCodeAndCaptureContinues(t *testing.T) {
	s := openTestStore(t)
	journalUnknown(t, s, repairFixtures()[1].msg, nil)
	p := newRepairPass(s)
	p.stmt = 100 * time.Millisecond
	p.row = func(ctx context.Context, seq int64) (repairRow, bool, error) {
		// A stalled row read past its statement bound; capture is not held behind it.
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
		return repairRow{}, false, ctx.Err()
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

func pageCount(t *testing.T, s *CaptureStore) int64 {
	t.Helper()
	var pages int64
	if err := s.db.QueryRow("PRAGMA page_count").Scan(&pages); err != nil {
		t.Fatal(err)
	}
	return pages
}

// setCeiling leaves only extra free pages under the capture file's ceiling:
// less than the pass's reserve, enough for live capture's next insert.
func setCeiling(t *testing.T, s *CaptureStore, extra int64) {
	t.Helper()
	if _, err := s.db.Exec("PRAGMA max_page_count=" + strconv.FormatInt(pageCount(t, s)+extra, 10)); err != nil {
		t.Fatal(err)
	}
}

func durableLastSeq(t *testing.T, s *CaptureStore) int64 {
	t.Helper()
	var last int64
	if err := s.db.QueryRow("SELECT last_seq FROM repair_progress WHERE version=?", REPAIR_VERSION).Scan(&last); err != nil {
		t.Fatal(err)
	}
	return last
}

// spec: ANYM-809
func TestRepairProgressAtThePageCeilingFailsClosedAndCaptureStillInserts(t *testing.T) {
	s := openTestStore(t)
	for i := 0; i < 20; i++ {
		journalUnknown(t, s, fixtureMessage("FULL"+string(rune('A'+i)), 1700000000+int64(i)), nil)
	}
	// Near the ceiling, below the eviction threshold (production MaxEntries):
	// the pass writes nothing, so the insert capture can make still succeeds.
	setCeiling(t, s, 16)
	before := pageCount(t, s)
	p := newRepairPass(s)
	p.tick(context.Background())
	if p.lastError != "repair_storage" || p.loaded || p.scanned != 0 {
		t.Fatalf("admission near the ceiling: err=%q loaded=%v", p.lastError, p.loaded)
	}
	var tables int
	s.db.QueryRow("SELECT COUNT(*) FROM sqlite_master WHERE name='repair_progress'").Scan(&tables)
	if tables != 0 || pageCount(t, s) != before {
		t.Fatal("the refused pass allocated pages")
	}
	status, raw := readRepairStatus(t, s)
	if status["last_error"] != "repair_storage" {
		t.Fatal("unexpected status", string(raw))
	}
	if s.MaxEntries != 200000 {
		t.Fatal("test must keep production retention")
	}
	if err := s.Put(fixtureMessage("FULLZ", 1700000100), "live"); err != nil {
		t.Fatal("capture insert failed near the ceiling after the repair pass:", err)
	}
}

// spec: ANYM-809
func TestRepairFailedProgressUpdateKeepsTheDurableAndInMemoryPosition(t *testing.T) {
	s := openTestStore(t)
	album := repairFixtures()[2].msg.Message
	journal := func(prefix string, n int) {
		for i := 0; i < n; i++ {
			e := fixtureMessage(prefix+string(rune('A'+i%26))+string(rune('A'+i/26)), 1700000000+int64(i))
			e.Message, e.RawMessage = album, album
			journalUnknown(t, s, e, nil)
		}
	}
	journal("U", repairWindowSeqs+5)
	p := newRepairPass(s)
	p.tick(context.Background()) // loads and records the first window
	if p.lastError != "" || p.lastSeq == 0 || p.done {
		t.Fatalf("first tick: err=%q last_seq=%d", p.lastError, p.lastSeq)
	}
	last, scanned := p.lastSeq, p.scanned
	// An established progress row near the ceiling: the update is refused.
	setCeiling(t, s, 16)
	p.tick(context.Background())
	if p.lastError != "repair_storage" || p.lastSeq != last || p.scanned != scanned || durableLastSeq(t, s) != last {
		t.Fatalf("refused update moved progress: err=%q last=%d durable=%d", p.lastError, p.lastSeq, durableLastSeq(t, s))
	}
	if err := s.Put(fixtureMessage("AFTER", 1700000200), "live"); err != nil {
		t.Fatal("capture insert failed after a refused progress update:", err)
	}
	if _, err := s.db.Exec("PRAGMA max_page_count=131072"); err != nil {
		t.Fatal(err)
	}
	// An UPDATE that SQLite refuses keeps the durable and in-memory position.
	if _, err := s.db.Exec("CREATE TRIGGER refuse_repair BEFORE UPDATE ON repair_progress BEGIN SELECT RAISE(ABORT,'synthetic storage failure'); END"); err != nil {
		t.Fatal(err)
	}
	p.lastError = ""
	p.tick(context.Background())
	if p.lastError != "repair_storage" || p.lastSeq != last || p.scanned != scanned || !p.loaded || durableLastSeq(t, s) != last {
		t.Fatalf("refused UPDATE moved progress: err=%q last=%d durable=%d", p.lastError, p.lastSeq, durableLastSeq(t, s))
	}
	if _, err := s.db.Exec("DROP TRIGGER refuse_repair"); err != nil {
		t.Fatal(err)
	}
	// An UPDATE that finds no row (the row is gone) keeps the position too.
	if _, err := s.db.Exec("DELETE FROM repair_progress"); err != nil {
		t.Fatal(err)
	}
	p.lastError = ""
	p.tick(context.Background())
	if p.lastError != "repair_storage" || p.lastSeq != last || p.scanned != scanned || p.loaded {
		t.Fatalf("failed update moved progress: err=%q last=%d loaded=%v", p.lastError, p.lastSeq, p.loaded)
	}
}

// spec: ANYM-809
func TestRepairTimeSliceEndsTheTickAndTheNextTickContinues(t *testing.T) {
	s := openTestStore(t)
	album := repairFixtures()[2].msg.Message
	for i := 0; i < 10; i++ {
		e := fixtureMessage("S"+string(rune('A'+i)), 1700000000+int64(i))
		e.Message, e.RawMessage = album, album
		journalUnknown(t, s, e, nil)
	}
	p := newRepairPass(s)
	clock := time.Unix(1700000000, 0)
	p.now = func() time.Time { return clock }
	p.slice = 250 * time.Millisecond
	p.classify = func(r repairRow) string { clock = clock.Add(100 * time.Millisecond); return s.repairState(r) }
	p.tick(context.Background())
	if p.lastError != "" || p.scanned != 3 || p.done {
		t.Fatalf("one slice: scanned=%d done=%v err=%q", p.scanned, p.done, p.lastError)
	}
	var third int64
	s.db.QueryRow("SELECT seq FROM capture ORDER BY seq LIMIT 1 OFFSET 2").Scan(&third)
	if p.lastSeq != third || durableLastSeq(t, s) != third {
		t.Fatalf("slice position %d, want %d", p.lastSeq, third)
	}
	runRepairToEnd(t, p)
	if p.scanned != 10 || p.counts["label_only"] != 10 {
		t.Fatalf("after slices: scanned=%d counts=%v", p.scanned, p.counts)
	}
}

// spec: ANYM-809
func TestRepairStopsDuringAnActiveTickOnShutdownOrDisconnectAndKeepsNothing(t *testing.T) {
	for _, stop := range []string{"shutdown", "disconnect"} {
		t.Run(stop, func(t *testing.T) {
			s := openTestStore(t)
			for _, f := range repairFixtures()[:3] {
				journalUnknown(t, s, f.msg, f.raw)
			}
			p := newRepairPass(s)
			p.tick(context.Background()) // loads, counts all three, done
			if !p.done {
				t.Fatal("setup pass did not finish")
			}
			if _, err := s.db.Exec("DELETE FROM repair_progress"); err != nil {
				t.Fatal(err)
			}
			p = newRepairPass(s)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			connected := true
			p.active = func() bool { return connected }
			calls := 0
			p.classify = func(r repairRow) string {
				if calls++; calls == 2 {
					if stop == "shutdown" {
						cancel()
					} else {
						connected = false
					}
				}
				return s.repairState(r)
			}
			p.tick(ctx)
			if p.lastError != "" || p.lastSeq != 0 || p.scanned != 0 || p.done || durableLastSeq(t, s) != 0 {
				t.Fatalf("%s mid-tick kept progress: last=%d scanned=%d err=%q", stop, p.lastSeq, p.scanned, p.lastError)
			}
			if calls != 2 {
				t.Fatalf("%s mid-tick classified %d rows", stop, calls)
			}
		})
	}
}

// spec: ANYM-809
func TestRepairWorkerRunsOnlyWhileReadyAndEndsWithTheProviderContext(t *testing.T) {
	s := openTestStore(t)
	journalUnknown(t, s, repairFixtures()[1].msg, nil)
	p := &Provider{Capture: s, Config: s.Config}
	ctx, cancel := context.WithCancel(context.Background())
	ended := make(chan struct{})
	go func() { p.repairWorker(ctx, 5*time.Millisecond); close(ended) }()
	status := filepath.Join(s.Config.EventsDirectory, ".repair-status")
	time.Sleep(100 * time.Millisecond)
	if _, err := os.Lstat(status); !os.IsNotExist(err) {
		t.Fatal("the pass ran while the provider was not ready")
	}
	p.ready.Store(true)
	deadline := time.Now().Add(5 * time.Second)
	for {
		if _, err := os.Lstat(status); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the pass did not run once ready")
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	select {
	case <-ended:
	case <-time.After(2 * time.Second):
		t.Fatal("the worker outlived the provider context")
	}
	if code, _ := p.failure.Load().(string); code != "" {
		t.Fatal("the repair worker failed the provider:", code)
	}
}

// spec: ANYM-809
func TestRepairSlowReadsStillAdvanceTheCountEveryTick(t *testing.T) {
	s := openTestStore(t)
	album := repairFixtures()[2].msg.Message
	for i := 0; i < 5; i++ {
		e := fixtureMessage("W"+string(rune('A'+i)), 1700000000+int64(i))
		e.Message, e.RawMessage = album, album
		journalUnknown(t, s, e, nil)
	}
	p := newRepairPass(s)
	clock := time.Unix(1700000000, 0)
	p.now = func() time.Time { return clock }
	row := p.row
	// Every row read costs more than the whole time slice.
	p.row = func(ctx context.Context, seq int64) (repairRow, bool, error) {
		clock = clock.Add(2 * p.slice)
		return row(ctx, seq)
	}
	for tick := 1; tick <= 5; tick++ {
		before := p.lastSeq
		p.tick(context.Background())
		if p.lastError != "" || p.lastSeq <= before {
			t.Fatalf("tick %d made no progress: last=%d err=%q", tick, p.lastSeq, p.lastError)
		}
	}
	runRepairToEnd(t, p)
	if p.scanned != 5 {
		t.Fatalf("scanned %d, want 5", p.scanned)
	}
}

// spec: ANYM-809
func TestRepairCaptureArrivingMidTickWaitsForAtMostOneRowRead(t *testing.T) {
	s := openTestStore(t)
	// Rows near the journal's size bound: a large raw behind a small envelope.
	big := &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String(strings.Repeat("x", 300<<10))}}
	for i := 0; i < repairWindowSeqs; i++ {
		e := fixtureMessage("B"+string(rune('A'+i%26))+string(rune('A'+i/26)), 1700000000+int64(i))
		e.Message, e.RawMessage = big, big
		envelope, err := captureEnvelope(s.Config.Account, e, s.Owner, "live", s.OwnerLID)
		if err != nil {
			t.Fatal(err)
		}
		event := envelope["event"].(map[string]any)
		event["messageType"], event["body"] = "unknown", ""
		raw, _ := marshalMessage(e)
		if err = s.append(LegacyChatID(e.Info.Chat), LegacyMessageID(e), e.Info.Timestamp.Unix(), raw, envelope, false); err != nil {
			t.Fatal(err)
		}
	}
	var withRaw int
	s.db.QueryRow("SELECT COUNT(*) FROM capture WHERE length(raw)>300000").Scan(&withRaw)
	if withRaw != repairWindowSeqs {
		t.Fatal("fixture rows lost their raw", withRaw)
	}
	p := newRepairPass(s)
	type result struct {
		err     error
		latency time.Duration
	}
	arrived := make(chan result, 1)
	calls := 0
	p.classify = func(r repairRow) string {
		if calls++; calls == 2 {
			// Capture arrives after the pass was admitted, while it keeps reading rows.
			go func() {
				started := time.Now()
				err := s.Put(fixtureMessage("MIDTICK", 1700100000), "live")
				arrived <- result{err, time.Since(started)}
			}()
		}
		return s.repairState(r)
	}
	p.tick(context.Background())
	var got result
	select {
	case got = <-arrived:
	case <-time.After(10 * time.Second):
		t.Fatal("capture did not finish") // also when the tick ended before the second row
	}
	if got.err != nil {
		t.Fatal(got.err)
	}
	if got.latency > time.Second {
		t.Fatal("capture waited behind the repair pass", got.latency)
	}
	var last string
	s.db.QueryRow("SELECT message_id FROM capture ORDER BY seq DESC LIMIT 1").Scan(&last)
	if !strings.Contains(last, "MIDTICK") {
		t.Fatal("capture order changed under the repair pass")
	}
	runRepairToEnd(t, p)
	if p.scanned != repairWindowSeqs || p.counts["would_add_content"] != repairWindowSeqs {
		t.Fatalf("count with capture mid-tick: scanned=%d counts=%v", p.scanned, p.counts)
	}
}

// spec: ANYM-809
func TestRepairCaptureQueuedBehindARepairStatementIsReleasedOnCancel(t *testing.T) {
	s := openTestStore(t)
	album := repairFixtures()[2].msg.Message
	for i := 0; i < 4; i++ {
		e := fixtureMessage("O"+string(rune('A'+i)), 1700000000+int64(i))
		e.Message, e.RawMessage = album, album
		journalUnknown(t, s, e, nil)
	}
	p := newRepairPass(s)
	p.tick(context.Background()) // loads and counts the four rows
	if !p.done {
		t.Fatal("setup pass did not finish")
	}
	if _, err := s.db.Exec("DELETE FROM repair_progress"); err != nil {
		t.Fatal(err)
	}
	p = newRepairPass(s)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	type result struct {
		err     error
		latency time.Duration
	}
	arrived := make(chan result, 1)
	var released time.Time
	row := p.row
	p.row = func(c context.Context, seq int64) (repairRow, bool, error) {
		if seq != 2 {
			return row(c, seq)
		}
		// The repair statement owns the only connection and stalls in it.
		conn, err := s.db.Conn(c)
		if err != nil {
			return repairRow{}, false, err
		}
		waits := s.db.Stats().WaitCount
		go func() {
			started := time.Now()
			err := s.Put(fixtureMessage("QUEUED", 1700100000), "live")
			arrived <- result{err, time.Since(started)}
		}()
		for s.db.Stats().WaitCount == waits {
			time.Sleep(time.Millisecond) // until capture is queued on the connection
		}
		cancel() // shutdown during ownership
		<-c.Done()
		released = time.Now()
		conn.Close()
		return repairRow{}, false, c.Err()
	}
	started := time.Now()
	p.tick(ctx)
	if time.Since(started) > 3*time.Second {
		t.Fatal("the stalled statement was not released")
	}
	var got result
	select {
	case got = <-arrived:
	case <-time.After(10 * time.Second):
		t.Fatal("queued capture never ran")
	}
	if got.err != nil {
		t.Fatal(got.err)
	}
	if got.latency > 3*time.Second || released.IsZero() {
		t.Fatal("capture was not released with the repair statement", got.latency)
	}
	if p.lastError != "" || p.lastSeq != 0 || p.scanned != 0 || durableLastSeq(t, s) != 0 {
		t.Fatalf("progress kept after a cancelled tick: last=%d scanned=%d", p.lastSeq, p.scanned)
	}
	var last string
	s.db.QueryRow("SELECT message_id FROM capture ORDER BY seq DESC LIMIT 1").Scan(&last)
	if !strings.Contains(last, "QUEUED") {
		t.Fatal("capture order changed under the repair pass")
	}
}
