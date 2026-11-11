package main

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"go.mau.fi/whatsmeow/types/events"
)

// Republish pass (ANYM-963). ANYM-809's count found journal rows of the
// pre-ANYM-471 "unknown" cohort whose retained raw now gives a body or a file.
// This pass rebuilds such a row's archive envelope from that raw with the
// current classifier and stores and exports it through the journal's own
// append: a new journal row under a new digest (the repair identity), the
// original row untouched. The archive takes the re-read at the retained
// provider time as a new revision through its completion rules (ANYM-560,
// ANYM-808) and answers a repeat `duplicate`. Only envelopes those rules accept
// are published; any other would_add_content row is counted unsupported.
//
// It shares the count's bounds (repair.go): at most one window of journal keys
// per tick, a time slice, bounded statements, capture priority between rows and
// statements, the capture file's page reserve before every write, and a stop on
// disconnect, lost lease or shutdown. It runs once the count is done.

// REPUBLISH_VERSION names the publish rules a pass ran with. A change of what
// is published bumps it, so the pass walks the journal again; the archive's
// receipt digest and append's digest skip keep a repeat harmless.
const REPUBLISH_VERSION = 1

// The fixed tallies and error codes; anything else is never stored or reported.
var republishStates = []string{"published", "unsupported", "parse_error"}
var republishErrors = []string{"republish_read", "republish_storage", "republish_spool"}

// The provider message types whose text completes an unknown revision
// (archive.py WHATSAPP_TEXT_TYPES) and the native kinds whose single file does
// (WHATSAPP_ATTACHMENT_KINDS photo, video, document; adapters.py kind map).
var republishTextTypes = map[string]bool{"template": true, "interactive": true, "buttons": true, "list": true, "product": true, "group_invite": true, "list_response": true, "buttons_response": true, "template_reply": true, "interactive_response": true}
var republishFileTypes = map[string]bool{"image": true, "video": true, "gif": true, "ptv": true, "document": true}

type republishItem struct {
	chat, id string
	stamp    int64
	raw      []byte
	envelope map[string]any
}

type republishPass struct {
	base     *repairPass // the count: bounds, yield, headroom, statement timeout
	version  int
	rebuild  func(repairRow) (string, republishItem) // "", "publish" or a tally
	publish  func(republishItem) error
	progress func(ctx context.Context, lastSeq int64, counts []byte) error // tests: a crash before it

	loaded    bool
	done      bool
	lastSeq   int64
	scanned   int64
	counts    map[string]int64
	lastError string
}

func newRepublishPass(base *repairPass) *republishPass {
	s := base.s
	p := &republishPass{base: base, version: REPUBLISH_VERSION}
	p.rebuild = func(r repairRow) (string, republishItem) { return s.republishItem(r) }
	p.publish = func(i republishItem) error { return s.append(i.chat, i.id, i.stamp, i.raw, i.envelope, false) }
	p.progress = p.writeProgress
	return p
}

// republishSupported is true for an envelope the archive's completion rules
// accept against an empty unknown revision: a new message (never an edit or a
// delete) with business text and no file, or exactly one photo, video or
// document. Location, contact, poll and the rest stay as they are.
func republishSupported(envelope map[string]any) bool {
	v, _ := envelope["event"].(map[string]any)
	if envelope["kind"] != "whatsapp_event" || envelope["change"] != "message" || v == nil {
		return false
	}
	kind, _ := v["messageType"].(string)
	body, _ := v["body"].(string)
	media, _ := v["hasMedia"].(bool)
	if service, _ := v["isService"].(bool); service {
		return false
	}
	return (republishTextTypes[kind] && body != "" && !media) || (republishFileTypes[kind] && media)
}

// republishItem decides one cohort row: "" when the count's classifier finds
// nothing to add, "publish" with the rebuilt envelope, or a tally. A panic is
// parse_error; the pass goes on with the next row.
func (s *CaptureStore) republishItem(r repairRow) (state string, item republishItem) {
	defer func() {
		if recover() != nil {
			state, item = "parse_error", republishItem{}
		}
	}()
	if s.repairState(r) != "would_add_content" {
		return "", item
	}
	msg, err := unmarshalMessage(r.Raw)
	if err != nil {
		return "parse_error", item
	}
	msg = unwrap(msg)
	envelope, err := captureEnvelope(s.Config.Account, msg, s.Owner, "reconcile", s.OwnerLID)
	if err != nil {
		return "unsupported", item
	}
	if !republishSupported(envelope) {
		return "unsupported", item
	}
	return "publish", republishItem{chat: LegacyChatID(msg.Info.Chat), id: LegacyMessageID(msg), stamp: msg.Info.Timestamp.Unix(), raw: r.Raw, envelope: envelope}
}

// republishCountsJSON is the stored counts column: fixed integers only.
func republishCountsJSON(done bool, scanned int64, counts map[string]int64) []byte {
	out := map[string]int64{"scanned": scanned, "done": 0}
	if done {
		out["done"] = 1
	}
	for _, k := range republishStates {
		out[k] = counts[k]
	}
	return mustJSON(out)
}

// load reads this version's progress, or starts it, as the count's load does:
// the table is created only after the headroom check, older versions' rows are
// discarded, and every statement is idempotent and bounded.
func (p *republishPass) load(ctx context.Context) string {
	s, b := p.base.s, p.base
	c, cancel := context.WithTimeout(ctx, b.stmt)
	defer cancel()
	var exists int
	if err := s.db.QueryRowContext(c, "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='republish_progress'").Scan(&exists); err != nil {
		return "republish_read"
	}
	if exists == 1 {
		var last int64
		var counts []byte
		err := s.db.QueryRowContext(c, "SELECT last_seq,counts FROM republish_progress WHERE version=?", p.version).Scan(&last, &counts)
		if err == nil {
			var stored map[string]int64
			if json.Unmarshal(counts, &stored) != nil {
				return "republish_read"
			}
			p.counts = map[string]int64{}
			for _, k := range republishStates {
				p.counts[k] = stored[k]
			}
			p.lastSeq, p.scanned, p.done, p.loaded = last, stored["scanned"], stored["done"] == 1, true
			return ""
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return "republish_read"
		}
	}
	if b.headroom(ctx) != nil {
		return "republish_storage"
	}
	for _, q := range []struct {
		sql  string
		args []any
	}{
		{`CREATE TABLE IF NOT EXISTS republish_progress(version INTEGER PRIMARY KEY,last_seq INTEGER NOT NULL,counts TEXT NOT NULL)`, nil},
		{"DELETE FROM republish_progress WHERE version<>?", []any{p.version}},
		{"INSERT OR IGNORE INTO republish_progress(version,last_seq,counts)VALUES(?,0,?)", []any{p.version, republishCountsJSON(false, 0, nil)}},
	} {
		if stop, code := b.yield(ctx); stop {
			return republishCode(code)
		}
		c, cancel := context.WithTimeout(ctx, b.stmt)
		_, err := s.db.ExecContext(c, q.sql, q.args...)
		cancel()
		if err != nil {
			return "republish_storage"
		}
	}
	p.lastSeq, p.scanned, p.done, p.counts, p.loaded = 0, 0, false, map[string]int64{}, true
	return ""
}

// republishCode maps the count's read code onto this pass's own.
func republishCode(code string) string {
	if code == "repair_read" {
		return "republish_read"
	}
	return code
}

func (p *republishPass) writeProgress(ctx context.Context, lastSeq int64, counts []byte) error {
	c, cancel := context.WithTimeout(ctx, p.base.stmt)
	defer cancel()
	result, err := p.base.s.db.ExecContext(c, "UPDATE republish_progress SET last_seq=?,counts=? WHERE version=?", lastSeq, counts, p.version)
	if err != nil {
		return err
	}
	if n, err := result.RowsAffected(); err != nil || n != 1 {
		p.loaded = false // the row is gone: start the version again from its durable state
		return errors.New("republish_storage")
	}
	return nil
}

// tick runs at most one bounded batch; every error is a fixed status code.
func (p *republishPass) tick(ctx context.Context) {
	if code := p.step(ctx); code != "" {
		p.lastError = code
	}
}

func (p *republishPass) step(ctx context.Context) string {
	if p.done {
		return ""
	}
	b, s := p.base, p.base.s
	deadline := b.now().Add(b.slice)
	if stop, code := b.yield(ctx); stop {
		return republishCode(code)
	}
	if !p.loaded {
		if code := p.load(ctx); code != "" || p.done {
			return code
		}
		if stop, code := b.yield(ctx); stop {
			return republishCode(code)
		}
	}
	c, cancel := context.WithTimeout(ctx, b.stmt)
	var maxSeq int64
	err := s.db.QueryRowContext(c, "SELECT COALESCE(MAX(seq),0) FROM capture").Scan(&maxSeq)
	cancel()
	if err != nil {
		return "republish_read"
	}
	upto := min(p.lastSeq+repairWindowSeqs, maxSeq)
	next, scanned := p.lastSeq, p.scanned
	counts := map[string]int64{}
	for k, v := range p.counts {
		counts[k] = v
	}
	complete, handled := true, 0
	for seq := p.lastSeq + 1; seq <= upto; seq++ {
		if seq > p.lastSeq+1 && (b.now().After(deadline) || handled >= repairBatchRows) {
			complete = false
			break
		}
		if stop, code := b.yield(ctx); stop {
			if code != "" || ctx.Err() != nil || !b.active() {
				return republishCode(code) // stopped: the tick's progress is not kept
			}
			complete = false
			break
		}
		c, cancel = context.WithTimeout(ctx, b.stmt)
		r, ok, err := b.row(c, seq)
		cancel()
		if err != nil {
			if ctx.Err() != nil || !b.active() {
				return ""
			}
			return "republish_read"
		}
		if ok {
			if selected, _, _ := repairCandidate(r.Envelope); selected {
				scanned++
				handled++
				state, item := p.safeRebuild(r)
				if state == "publish" {
					// The write never spends the room live capture needs. A failed
					// append keeps nothing of this tick; its durable row (if any) is
					// exported by replay and skipped by digest on the next attempt.
					if b.headroom(ctx) != nil {
						return "republish_storage"
					}
					if err := p.publish(item); err != nil {
						if spoolBackpressure(err) {
							return "republish_spool"
						}
						return "republish_storage"
					}
					state = "published"
				}
				if state != "" {
					counts[state]++
				}
			}
		}
		next = seq
	}
	if complete {
		next = upto
	}
	done := next >= maxSeq && complete
	if stop, code := b.yield(ctx); stop {
		return republishCode(code)
	}
	if next == p.lastSeq && !done {
		return ""
	}
	if b.headroom(ctx) != nil {
		return "republish_storage"
	}
	// Fail closed: the in-memory progress moves only after the row is durable.
	if p.progress(ctx, next, republishCountsJSON(done, scanned, counts)) != nil {
		return "republish_storage"
	}
	p.lastSeq, p.scanned, p.counts, p.done = next, scanned, counts, done
	return ""
}

func (p *republishPass) safeRebuild(r repairRow) (state string, item republishItem) {
	defer func() {
		if recover() != nil {
			state, item = "parse_error", republishItem{}
		}
	}()
	return p.rebuild(r)
}

// status: the version, whether it finished, fixed integer counts and one fixed
// error code. Never an ID, chat or content. Nil until the pass has started.
func (p *republishPass) status() map[string]any {
	if p == nil || (!p.loaded && p.lastError == "") {
		return nil
	}
	out := map[string]any{"version": p.version, "done": p.done, "rows_scanned": p.scanned, "last_error": p.lastError}
	for _, k := range republishStates {
		out[k] = p.counts[k]
	}
	return out
}

// republishFixture (--republish-fixture) runs the count and this pass on a
// throwaway journal of synthetic messages stored the way the classifier before
// ANYM-471 did, and prints the journaled and the republished envelopes. It
// opens no provider or session; the Python contract test feeds both to the
// archive (tests/test_whatsapp_republish.py).
func republishFixture(path string) error {
	var v struct {
		Synthetic bool          `json:"synthetic"`
		Messages  []fixtureSpec `json:"messages"`
	}
	data, err := readPrivate(path, 1<<20)
	if err != nil {
		return err
	}
	if json.Unmarshal(data, &v) != nil || !v.Synthetic || len(v.Messages) == 0 || len(v.Messages) > 100 {
		return errors.New("invalid_fixture")
	}
	root, err := os.MkdirTemp(filepath.Dir(path), "republish-fixture-") // beside the fixture, never the process temp directory
	if err != nil {
		return err
	}
	defer os.RemoveAll(root)
	config := Config{Account: v.Messages[0].Account, SessionDirectory: filepath.Join(root, "session"), EventsDirectory: filepath.Join(root, "events")}
	if config.Account == "" {
		config.Account = "test"
	}
	s, err := OpenCapture(config)
	if err != nil {
		return err
	}
	defer s.Close()
	for _, m := range v.Messages {
		if !m.Synthetic {
			return errors.New("invalid_fixture")
		}
		event, owner, lid, err := m.event()
		if err != nil {
			return err
		}
		s.Owner, s.OwnerLID = owner, lid
		if err = s.journalAsUnknown(unwrap(event)); err != nil {
			return err
		}
	}
	// The archive consumed the old exports.
	journaled, err := drainFixtureSpool(config.EventsDirectory)
	if err != nil {
		return err
	}
	count := newRepairPass(s)
	again := newRepublishPass(count)
	count.next = again
	for i := 0; i < 1000 && !(count.done && again.done); i++ {
		count.tick(context.Background())
		if count.lastError != "" || again.lastError != "" {
			return errors.New("republish_fixture_failed")
		}
	}
	if !again.done {
		return errors.New("republish_fixture_failed")
	}
	published, err := drainFixtureSpool(config.EventsDirectory)
	if err != nil {
		return err
	}
	return json.NewEncoder(os.Stdout).Encode(map[string]any{"journaled": journaled, "published": published, "status": count.status()})
}

// journalAsUnknown stores a message as the classifier before ANYM-471 did: the
// raw protobuf is kept, the envelope says kind unknown with no text or file.
func (s *CaptureStore) journalAsUnknown(e *events.Message) error {
	envelope, err := captureEnvelope(s.Config.Account, e, s.Owner, "live", s.OwnerLID)
	if err != nil {
		return err
	}
	event := envelope["event"].(map[string]any)
	event["messageType"], event["body"], event["hasMedia"], event["isService"] = "unknown", "", false, false
	event["mediaKind"], event["filename"], event["mimetype"], event["filesize"], event["durationSeconds"] = nil, nil, nil, nil, nil
	empty := sha256.Sum256(nil)
	event["formatting"] = map[string]any{"version": 1, "state": "wire_observed", "basis": "whatsapp_native_wire", "wire_sha256": hex.EncodeToString(empty[:]), "entities": nil, "unsupported_entities": []any{}, "warnings": []string{"syntax_interpretation_unknown"}}
	raw, err := marshalMessage(e)
	if err != nil {
		return err
	}
	return s.append(LegacyChatID(e.Info.Chat), LegacyMessageID(e), e.Info.Timestamp.Unix(), raw, envelope, false)
}

// drainFixtureSpool reads and removes the exported envelopes in spool order.
func drainFixtureSpool(dir string) ([]json.RawMessage, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var names []string
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".json") && !strings.HasPrefix(e.Name(), ".") {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	out := []json.RawMessage{}
	for _, name := range names {
		data, err := readPrivate(filepath.Join(dir, name), 512<<10)
		if err != nil {
			return nil, err
		}
		if err = os.Remove(filepath.Join(dir, name)); err != nil {
			return nil, err
		}
		out = append(out, data)
	}
	return out, nil
}
