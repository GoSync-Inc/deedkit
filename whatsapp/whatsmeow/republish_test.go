package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"google.golang.org/protobuf/proto"
)

// republishFixtures: the ANYM-809 cohort plus the would_add_content kinds the
// archive's completion rules do not take (location, contact).
func republishFixtures() []repairFixture {
	with := func(id string, m *waE2E.Message) repairFixture {
		e := fixtureMessage(id, 1700000000)
		e.Message, e.RawMessage = m, m
		return repairFixture{id, e, nil}
	}
	out := repairFixtures()
	return append(out,
		with("LOC1", &waE2E.Message{LocationMessage: &waE2E.LocationMessage{DegreesLatitude: proto.Float64(1.5), DegreesLongitude: proto.Float64(2.5), Name: proto.String("Synthetic place")}}),
		with("VCF1", &waE2E.Message{ContactMessage: &waE2E.ContactMessage{DisplayName: proto.String("Synthetic Person"), Vcard: proto.String("BEGIN:VCARD\nVERSION:3.0\nFN:Synthetic Person\nTEL:+10000000000\nEND:VCARD")}}),
	)
}

// newLinkedPasses is the worker's pair: the count, then the republish pass.
func newLinkedPasses(s *CaptureStore) (*repairPass, *republishPass) {
	count := newRepairPass(s)
	again := newRepublishPass(count)
	count.next = again
	return count, again
}

func runRepublishToEnd(t *testing.T, count *repairPass, again *republishPass) {
	t.Helper()
	for i := 0; i < 1000 && !(count.done && again.done); i++ {
		count.tick(context.Background())
		if count.lastError != "" || again.lastError != "" {
			t.Fatal("pass error", count.lastError, again.lastError)
		}
	}
	if !again.done {
		t.Fatal("republish pass did not finish")
	}
}

// spoolEnvelopes reads the exported envelopes, in spool order.
func spoolEnvelopes(t *testing.T, s *CaptureStore) []map[string]any {
	t.Helper()
	entries, err := os.ReadDir(s.Config.EventsDirectory)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".json") && !strings.HasPrefix(e.Name(), ".") {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	var out []map[string]any
	for _, name := range names {
		raw, err := readPrivate(filepath.Join(s.Config.EventsDirectory, name), 512<<10)
		if err != nil {
			t.Fatal(err)
		}
		var v map[string]any
		if err = json.Unmarshal(raw, &v); err != nil {
			t.Fatal(err)
		}
		out = append(out, v)
	}
	return out
}

// consumeSpool removes the exports, as the archive consumer does after commit.
func consumeSpool(t *testing.T, s *CaptureStore) {
	t.Helper()
	entries, _ := os.ReadDir(s.Config.EventsDirectory)
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".json") && !strings.HasPrefix(e.Name(), ".") {
			if err := os.Remove(filepath.Join(s.Config.EventsDirectory, e.Name())); err != nil {
				t.Fatal(err)
			}
		}
	}
}

func journalRows(t *testing.T, s *CaptureStore) int {
	t.Helper()
	var n int
	if err := s.db.QueryRow("SELECT COUNT(*) FROM capture").Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

// spec: ANYM-963
func TestRepublishPublishesTemplateTextAndAlbumPhotoOnceAndLeavesTheRestAlone(t *testing.T) {
	s := openTestStore(t)
	fixtures := republishFixtures()
	for _, f := range fixtures {
		journalUnknown(t, s, f.msg, f.raw)
	}
	consumeSpool(t, s)
	before := snapshotJournal(t, s)
	count, again := newLinkedPasses(s)
	runRepublishToEnd(t, count, again)
	if count.counts["would_add_content"] != 4 {
		t.Fatalf("fixture cohort changed: %v", count.counts)
	}
	if again.counts["published"] != 2 || again.counts["unsupported"] != 2 || again.counts["parse_error"] != 0 || again.scanned != int64(len(fixtures)) {
		t.Fatalf("published=%d unsupported=%d parse_error=%d scanned=%d", again.counts["published"], again.counts["unsupported"], again.counts["parse_error"], again.scanned)
	}
	// published plus unsupported is the count's would_add_content (criterion 5's equation).
	if again.counts["published"]+again.counts["unsupported"] != count.counts["would_add_content"] {
		t.Fatal("published + unsupported != would_add_content")
	}
	// The original rows are untouched: the journal only grew by the new rows.
	after := snapshotJournal(t, s)
	if !strings.HasPrefix(after.rows, before.rows) || journalRows(t, s) != len(fixtures)+2 {
		t.Fatal("an original journal row was rewritten, or the wrong number of rows was added")
	}
	got := spoolEnvelopes(t, s)
	if len(got) != 2 {
		t.Fatalf("spool holds %d envelopes, want 2", len(got))
	}
	want := map[string]string{"true_111111@c.us_R1": "image", "false_111111@c.us_R1": "image", "false_111111@c.us_R2": "template", "true_111111@c.us_R2": "template"}
	for _, envelope := range got {
		event := envelope["event"].(map[string]any)
		if envelope["origin"] != "reconcile" || envelope["change"] != "message" || envelope["kind"] != "whatsapp_event" {
			t.Fatal("republished envelope is not a reconcile message event", envelope)
		}
		if want[event["messageId"].(string)] != event["messageType"] {
			t.Fatal("unexpected republished message", event["messageId"], event["messageType"])
		}
		if event["messageType"] == "template" && event["body"] != "Your code is 0000" {
			t.Fatal("template text not restored")
		}
		if event["messageType"] == "image" && event["hasMedia"] != true {
			t.Fatal("album child photo not restored")
		}
	}
	// The new row keeps the raw, so the media download can use it.
	var raws int
	if err := s.db.QueryRow("SELECT COUNT(*) FROM capture WHERE seq>? AND length(raw)>0", len(fixtures)).Scan(&raws); err != nil || raws != 2 {
		t.Fatal("republished rows lost their raw", raws, err)
	}
	// A finished version does not run again.
	count2, again2 := newLinkedPasses(s)
	runRepublishToEnd(t, count2, again2)
	if journalRows(t, s) != len(fixtures)+2 || len(spoolEnvelopes(t, s)) != 2 {
		t.Fatal("a finished version ran again")
	}
}

// spec: ANYM-963
func TestRepublishPanicOnOneRowIsParseErrorAndThePassGoesOn(t *testing.T) {
	s := openTestStore(t)
	for _, f := range repairFixtures()[:2] { // album child, template: both publishable
		journalUnknown(t, s, f.msg, f.raw)
	}
	count, again := newLinkedPasses(s)
	calls := 0
	again.rebuild = func(r repairRow) (string, republishItem) {
		if calls++; calls == 1 {
			panic("synthetic rebuild panic")
		}
		return s.republishItem(r)
	}
	runRepublishToEnd(t, count, again)
	if again.counts["parse_error"] != 1 || again.counts["published"] != 1 || again.scanned != 2 {
		t.Fatalf("after a panicking row: %v scanned=%d", again.counts, again.scanned)
	}
}

// spec: ANYM-963
func TestRepublishCrashBetweenAppendAndProgressResumesAndTheSpoolHoldsTheEnvelopeOnce(t *testing.T) {
	s := openTestStore(t)
	for _, f := range repairFixtures()[:2] {
		journalUnknown(t, s, f.msg, f.raw)
	}
	consumeSpool(t, s)
	count, again := newLinkedPasses(s)
	// A loaded runner: every clock read below is a second later. Under -race the first append alone outlasted the
	// shared 200 ms slice and the setup tick ended with one envelope of two (#921's PR run).
	clock := time.Unix(1700000000, 0)
	count.now = func() time.Time { clock = clock.Add(time.Second); return clock }
	// The setup's one tick covers both rows; the slice that bounds a tick is not what this test checks.
	count.slice = time.Hour
	// The process dies after append, before the progress write.
	again.progress = func(context.Context, int64, []byte) error { return errors.New("crash") }
	count.tick(context.Background())
	if !count.done || again.lastSeq != 0 || len(spoolEnvelopes(t, s)) != 2 {
		t.Fatalf("setup: count done=%v republish last=%d", count.done, again.lastSeq)
	}
	var durable int64
	if err := s.db.QueryRow("SELECT last_seq FROM republish_progress WHERE version=?", REPUBLISH_VERSION).Scan(&durable); err != nil || durable != 0 {
		t.Fatal("progress moved without its write", durable, err)
	}
	// Restart: a new process resumes from the durable progress.
	count, again = newLinkedPasses(s)
	runRepublishToEnd(t, count, again)
	if n := len(spoolEnvelopes(t, s)); n != 2 || journalRows(t, s) != 4 {
		t.Fatalf("after resume the spool holds %d envelopes and the journal %d rows", n, journalRows(t, s))
	}
	if again.counts["published"] != 2 {
		t.Fatal("the resumed pass counted", again.counts)
	}
	// The archive consumed them; a second full run of the same rules publishes nothing.
	consumeSpool(t, s)
	if _, err := s.db.Exec("DELETE FROM republish_progress"); err != nil {
		t.Fatal(err)
	}
	count, again = newLinkedPasses(s)
	runRepublishToEnd(t, count, again)
	if n := len(spoolEnvelopes(t, s)); n != 0 || journalRows(t, s) != 4 {
		t.Fatalf("a second full run exported %d envelopes, journal %d rows", n, journalRows(t, s))
	}
}

// spec: ANYM-963
func TestRepublishAtThePageCeilingStopsWithStorageBeforeWriting(t *testing.T) {
	s := openTestStore(t)
	for _, f := range repairFixtures()[:2] {
		journalUnknown(t, s, f.msg, f.raw)
	}
	count, again := newLinkedPasses(s)
	count.tick(context.Background()) // the count loads and finishes; republish loads and publishes
	if _, err := s.db.Exec("DELETE FROM republish_progress"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.db.Exec("DELETE FROM capture WHERE seq>2"); err != nil {
		t.Fatal(err)
	}
	consumeSpool(t, s)
	setCeiling(t, s, 16)
	rows := journalRows(t, s)
	count, again = newLinkedPasses(s)
	count.tick(context.Background())
	if again.lastError != "republish_storage" || journalRows(t, s) != rows || len(spoolEnvelopes(t, s)) != 0 {
		t.Fatalf("near the ceiling: err=%q rows=%d", again.lastError, journalRows(t, s))
	}
	status, raw := readRepairStatus(t, s)
	if status["republish"].(map[string]any)["last_error"] != "republish_storage" {
		t.Fatal("unexpected status", string(raw))
	}
}

// spec: ANYM-963
func TestRepublishStopsOnLostLeaseOrDisconnectAndPublishesNothing(t *testing.T) {
	s := openTestStore(t)
	for _, f := range repairFixtures()[:2] {
		journalUnknown(t, s, f.msg, f.raw)
	}
	consumeSpool(t, s)
	count, again := newLinkedPasses(s)
	// A loaded runner: every clock read below is a second later. Under -race the first row alone could outlast the
	// 200 ms slice, and the setup tick ended before the count finished.
	clock := time.Unix(1700000000, 0)
	count.now = func() time.Time { clock = clock.Add(time.Second); return clock }
	// The setup's one tick covers the count; the slice that bounds a tick is not what this test checks.
	count.slice = time.Hour
	connected := true
	count.active = func() bool { return connected }
	again.rebuild = func(r repairRow) (string, republishItem) {
		connected = false // lost lease while the row is rebuilt
		return s.republishItem(r)
	}
	count.tick(context.Background())
	if !count.done {
		t.Fatal("setup: count did not finish")
	}
	if len(spoolEnvelopes(t, s)) > 1 || again.lastSeq != 0 || again.counts["published"] != 0 {
		t.Fatalf("the pass went on after the stop: spool=%d last=%d", len(spoolEnvelopes(t, s)), again.lastSeq)
	}
}

// spec: ANYM-963
func TestRepublishStatusIsCountsAndFixedCodesOnly(t *testing.T) {
	s := openTestStore(t)
	for _, f := range republishFixtures() {
		journalUnknown(t, s, f.msg, f.raw)
	}
	count, again := newLinkedPasses(s)
	runRepublishToEnd(t, count, again)
	status, raw := readRepairStatus(t, s)
	republish, ok := status["republish"].(map[string]any)
	if !ok {
		t.Fatal("no republish status", string(raw))
	}
	keys := []string{}
	for k := range republish {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	if strings.Join(keys, ",") != "done,last_error,parse_error,published,rows_scanned,unsupported,version" {
		t.Fatal("unexpected republish status keys", keys)
	}
	if republish["done"] != true || republish["published"] != float64(2) || republish["unsupported"] != float64(2) || republish["version"] != float64(REPUBLISH_VERSION) {
		t.Fatal("unexpected republish status", string(raw))
	}
	for _, secret := range []string{"Your code", "111111", "Synthetic", "R2"} {
		if bytes.Contains(raw, []byte(secret)) {
			t.Fatal("status carries content or an ID:", secret)
		}
	}
	// The count alone (no republish pass linked) keeps its ANYM-809 status shape.
	alone := newRepairPass(s)
	if _, ok := alone.status()["republish"]; ok {
		t.Fatal("a count without the republish pass reports one")
	}
}

// spec: ANYM-963
func TestRepublishSupportedTakesOnlyWhatTheArchiveCompletes(t *testing.T) {
	event := func(kind, body string, media, service bool) map[string]any {
		return map[string]any{"kind": "whatsapp_event", "change": "message", "event": map[string]any{"messageType": kind, "body": body, "hasMedia": media, "isService": service}}
	}
	for _, c := range []struct {
		envelope map[string]any
		want     bool
	}{
		{event("template", "text", false, false), true},
		{event("interactive_response", "choice", false, false), true},
		{event("template", "", false, false), false},
		{event("template", "text", true, false), false}, // header media is not fetched
		{event("image", "", true, false), true},
		{event("ptv", "", true, false), true},
		{event("document", "caption", true, false), true},
		{event("image", "", false, false), false}, // view-once: no file
		{event("location", "place", false, false), false},
		{event("vcard", "", false, false), false},
		{event("chat", "text", false, false), false},
		{event("audio", "", true, false), false},
		{event("template", "text", false, true), false},
		{map[string]any{"kind": "whatsapp_event", "change": "edit", "event": event("template", "x", false, false)["event"]}, false},
		{map[string]any{"kind": "whatsapp_delete"}, false},
	} {
		if got := republishSupported(c.envelope); got != c.want {
			t.Fatalf("%v: got %v want %v", c.envelope, got, c.want)
		}
	}
}
