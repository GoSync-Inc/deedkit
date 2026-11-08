package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"time"
)

// Repair count (ANYM-809). Rows the classifier before ANYM-471 stored as kind
// "unknown" keep their raw protobuf in the capture journal. This pass runs the
// current classifier on that raw and only tallies what a re-read would give.
// It never rewrites a journal row, publishes nothing, stores no candidate
// envelope and keeps no message content: its only write is one progress row of
// fixed integers per repair version, and its status file carries counts only.

// REPAIR_VERSION names the classifier a count was made with. A parser change
// that alters any fixture's state in repair_test.go bumps it, so the count
// starts again from the beginning of the journal.
const REPAIR_VERSION = 1

const (
	repairBatchRows = 50 // rows classified per tick at most
	// One tick covers at most this many journal keys (the replay batch) and
	// reads each row in a single-row key lookup of its own, so live capture
	// waits for at most one row read (append caps a row at 512 KiB).
	repairWindowSeqs  = replayBatchEntries
	repairTimeSlice   = 200 * time.Millisecond
	repairStatementTO = 2 * time.Second // bound of every SQLite statement of the pass
	// Free pages under the capture file's ceiling (max_page_count, store.go) the
	// pass leaves to live capture: 32 MiB of the 512 MiB file at 4 KiB pages.
	repairReservePages = 8192
)

// The fixed set of tallies; anything else is never stored or reported.
var repairStates = []string{"would_add_content", "label_only", "same", "no_raw", "placeholder", "sender_key_only", "parse_error"}

type repairRow struct {
	Seq      int64
	Envelope []byte
	Raw      []byte
}

type repairPass struct {
	s            *CaptureStore
	version      int
	slice        time.Duration
	stmt         time.Duration
	row          func(ctx context.Context, seq int64) (repairRow, bool, error)
	classify     func(repairRow) string
	now          func() time.Time
	active       func() bool // false on disconnect, lost lease or shutdown
	reservePages int64

	loaded    bool
	done      bool
	lastSeq   int64
	scanned   int64
	counts    map[string]int64
	lastError string
	reported  string
}

func newRepairPass(s *CaptureStore) *repairPass {
	p := &repairPass{s: s, version: REPAIR_VERSION, slice: repairTimeSlice, stmt: repairStatementTO, now: time.Now, reservePages: repairReservePages}
	p.active = func() bool { return true }
	p.row = s.repairRow
	p.classify = func(r repairRow) string { return s.repairState(r) }
	return p
}

// repairCandidate reads the stored envelope's own wire fields: an archived
// message event whose kind the old classifier left unknown.
func repairCandidate(envelope []byte) (selected bool, body string, hasMedia bool) {
	var v struct {
		Kind  string `json:"kind"`
		Event struct {
			MessageType string `json:"messageType"`
			Body        string `json:"body"`
			HasMedia    bool   `json:"hasMedia"`
		} `json:"event"`
	}
	if json.Unmarshal(envelope, &v) != nil || v.Kind != "whatsapp_event" || v.Event.MessageType != "unknown" {
		return false, "", false
	}
	return true, v.Event.Body, v.Event.HasMedia
}

// repairState runs the current classifier on a row's retained raw. A panic or
// an unreadable raw is parse_error; the pass goes on with the next row.
func (s *CaptureStore) repairState(r repairRow) (state string) {
	defer func() {
		if recover() != nil {
			state = "parse_error"
		}
	}()
	_, oldBody, oldMedia := repairCandidate(r.Envelope)
	if len(r.Raw) == 0 {
		return "no_raw"
	}
	msg, err := unmarshalMessage(r.Raw)
	if err != nil {
		return "parse_error"
	}
	if msg.Message != nil && controlOnly(msg.Message) {
		return "sender_key_only"
	}
	v, err := NormalizeMessage(msg, s.Owner, s.OwnerLID)
	if err != nil {
		return "parse_error"
	}
	kind, _ := v["messageType"].(string)
	body, _ := v["body"].(string)
	media, _ := v["hasMedia"].(bool)
	switch {
	case kind == "placeholder":
		return "placeholder"
	case (body != "" && oldBody == "") || (media && !oldMedia):
		return "would_add_content"
	case kind != "unknown":
		return "label_only"
	}
	return "same"
}

// repairRow reads one journal row by its key: one statement holds the shared
// connection for at most one row, which append caps at maxCaptureEnvelopeBytes.
// The pass has no multi-row iterator: the driver steps rows after QueryContext
// returns, outside its context interrupt, so the only uninterruptible work is
// this one key lookup. An evicted or never-used key is reported as absent.
func (s *CaptureStore) repairRow(ctx context.Context, seq int64) (repairRow, bool, error) {
	r := repairRow{Seq: seq}
	err := s.db.QueryRowContext(ctx, "SELECT envelope,raw FROM capture WHERE seq=?", seq).Scan(&r.Envelope, &r.Raw)
	if errors.Is(err, sql.ErrNoRows) {
		return r, false, nil
	}
	return r, err == nil, err
}

// captureQueued reports live capture work: a held capture lock, an unexported
// journal row or a due history download. The pass yields to all of them, and
// asks again before each of its statements, so capture waits at most for one
// bounded repair statement on the shared connection.
func (p *repairPass) captureQueued(ctx context.Context) (bool, error) {
	if !p.s.mu.TryLock() {
		return true, nil
	}
	p.s.mu.Unlock()
	c, cancel := context.WithTimeout(ctx, p.stmt)
	defer cancel()
	var n int
	err := p.s.db.QueryRowContext(c, "SELECT (SELECT COUNT(*) FROM (SELECT 1 FROM capture WHERE published=0 LIMIT 1))+(SELECT COUNT(*) FROM (SELECT 1 FROM history_jobs WHERE state IN('pending','attempted') AND next_attempt_ms<=? LIMIT 1))", p.now().UnixMilli()).Scan(&n)
	return n > 0, err
}

// yield is true when the pass must stop this tick without writing: shutdown,
// a lost lease or disconnect (inactive), or capture work that arrived.
func (p *repairPass) yield(ctx context.Context) (bool, string) {
	if ctx.Err() != nil || !p.active() {
		return true, ""
	}
	busy, err := p.captureQueued(ctx)
	if err != nil {
		return true, "repair_read"
	}
	return busy, ""
}

// headroom refuses a repair write unless the capture file keeps at least
// reservePages free pages under its page ceiling and the disk keeps its
// reserve. The count never spends the room live capture inserts need.
func (p *repairPass) headroom(ctx context.Context) error {
	c, cancel := context.WithTimeout(ctx, p.stmt)
	defer cancel()
	var ceiling, pages, free int64
	for _, q := range []struct {
		sql string
		v   *int64
	}{{"PRAGMA max_page_count", &ceiling}, {"PRAGMA page_count", &pages}, {"PRAGMA freelist_count", &free}} {
		if err := p.s.db.QueryRowContext(c, q.sql).Scan(q.v); err != nil {
			return err
		}
	}
	if ceiling-pages+free < p.reservePages {
		return errors.New("repair_storage")
	}
	return p.s.checkDisk(64 << 10)
}

// repairCountsJSON is the stored counts column: the fixed integers only.
func repairCountsJSON(scanned int64, counts map[string]int64) []byte {
	out := map[string]int64{"scanned": scanned}
	for _, k := range repairStates {
		out[k] = counts[k]
	}
	return mustJSON(out)
}

// load reads this version's progress, or starts it: it creates the table,
// deletes older versions' rows and inserts this one. The table is created
// here, not at open, so a full database never stops capture from starting,
// and only after the headroom check.
func (p *repairPass) load(ctx context.Context) string {
	c, cancel := context.WithTimeout(ctx, p.stmt)
	defer cancel()
	var exists int
	if err := p.s.db.QueryRowContext(c, "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='repair_progress'").Scan(&exists); err != nil {
		return "repair_read"
	}
	if exists == 1 {
		var done int
		var counts []byte
		var last int64
		err := p.s.db.QueryRowContext(c, "SELECT last_seq,done,counts FROM repair_progress WHERE version=?", p.version).Scan(&last, &done, &counts)
		if err == nil {
			var stored map[string]int64
			if json.Unmarshal(counts, &stored) != nil {
				return "repair_read"
			}
			p.counts = map[string]int64{}
			for _, k := range repairStates {
				p.counts[k] = stored[k]
			}
			p.lastSeq, p.scanned, p.done, p.loaded = last, stored["scanned"], done == 1, true
			return ""
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return "repair_read"
		}
	}
	if p.headroom(ctx) != nil {
		return "repair_storage"
	}
	// Three autocommit statements, each under the driver's exec interrupt, with
	// capture priority between them; no explicit transaction, whose commit the
	// driver runs without the context. Each one is idempotent, so a stop after any
	// of them resumes on the next tick: a table without this version's row is
	// started again, and older versions' rows are only ever discarded.
	for _, q := range []struct {
		sql  string
		args []any
	}{
		{`CREATE TABLE IF NOT EXISTS repair_progress(version INTEGER PRIMARY KEY,last_seq INTEGER NOT NULL,done INTEGER NOT NULL,counts TEXT NOT NULL)`, nil},
		{"DELETE FROM repair_progress WHERE version<>?", []any{p.version}},
		{"INSERT OR IGNORE INTO repair_progress(version,last_seq,done,counts)VALUES(?,0,0,?)", []any{p.version, repairCountsJSON(0, nil)}},
	} {
		if stop, code := p.yield(ctx); stop {
			return code
		}
		c, cancel := context.WithTimeout(ctx, p.stmt)
		_, err := p.s.db.ExecContext(c, q.sql, q.args...)
		cancel()
		if err != nil {
			return "repair_storage"
		}
	}
	p.lastSeq, p.scanned, p.done, p.counts, p.loaded = 0, 0, false, map[string]int64{}, true
	return ""
}

// tick classifies at most one bounded batch. Every error is a fixed status
// code; it never fails the provider and never carries row content.
func (p *repairPass) tick(ctx context.Context) {
	code := p.step(ctx)
	if code != "" {
		p.lastError = code
	}
	p.writeStatus()
}

func (p *repairPass) step(ctx context.Context) string {
	if p.done {
		return ""
	}
	// The time slice covers the pass's reads as well as classification.
	deadline := p.now().Add(p.slice)
	if stop, code := p.yield(ctx); stop {
		return code
	}
	if !p.loaded {
		if code := p.load(ctx); code != "" || p.done {
			return code
		}
		if stop, code := p.yield(ctx); stop {
			return code
		}
	}
	c, cancel := context.WithTimeout(ctx, p.stmt)
	var maxSeq int64
	err := p.s.db.QueryRowContext(c, "SELECT COALESCE(MAX(seq),0) FROM capture").Scan(&maxSeq)
	cancel()
	if err != nil {
		return "repair_read"
	}
	if stop, code := p.yield(ctx); stop {
		return code
	}
	upto := min(p.lastSeq+repairWindowSeqs, maxSeq)
	next, scanned := p.lastSeq, p.scanned
	counts := map[string]int64{}
	for k, v := range p.counts {
		counts[k] = v
	}
	complete := true
	for seq := p.lastSeq + 1; seq <= upto; seq++ {
		// At least one row per tick, so a slow read can never stall the count.
		if seq > p.lastSeq+1 && p.now().After(deadline) {
			complete = false
			break
		}
		// Capture priority between rows: capture waits for at most one row read.
		if stop, code := p.yield(ctx); stop {
			if code != "" || ctx.Err() != nil || !p.active() {
				return code // stopped: nothing of this tick is kept
			}
			complete = false
			break
		}
		c, cancel = context.WithTimeout(ctx, p.stmt)
		r, ok, err := p.row(c, seq)
		cancel()
		if err != nil {
			if ctx.Err() != nil || !p.active() {
				return "" // stopped, not a read failure
			}
			return "repair_read"
		}
		if ok {
			if selected, _, _ := repairCandidate(r.Envelope); selected {
				scanned++
				state := p.safeClassify(r)
				if !validRepairState(state) {
					state = "parse_error"
				}
				counts[state]++
			}
		}
		next = seq
	}
	if complete {
		next = upto // the whole window was read
	}
	done := next >= maxSeq && complete
	if stop, code := p.yield(ctx); stop {
		return code
	}
	if next == p.lastSeq && !done {
		return "" // nothing to record
	}
	if p.headroom(ctx) != nil {
		return "repair_storage"
	}
	doneFlag := 0
	if done {
		doneFlag = 1
	}
	c, cancel = context.WithTimeout(ctx, p.stmt)
	defer cancel()
	// Fail closed: the in-memory progress moves only after the row is durable.
	result, err := p.s.db.ExecContext(c, "UPDATE repair_progress SET last_seq=?,done=?,counts=? WHERE version=?", next, doneFlag, repairCountsJSON(scanned, counts), p.version)
	if err != nil {
		return "repair_storage"
	}
	if n, err := result.RowsAffected(); err != nil || n != 1 {
		p.loaded = false // the row is gone: start the version again from its durable state
		return "repair_storage"
	}
	p.lastSeq, p.scanned, p.counts, p.done = next, scanned, counts, done
	return ""
}

// safeClassify keeps one bad row from stopping the pass.
func (p *repairPass) safeClassify(r repairRow) (state string) {
	defer func() {
		if recover() != nil {
			state = "parse_error"
		}
	}()
	return p.classify(r)
}

func validRepairState(state string) bool {
	for _, k := range repairStates {
		if k == state {
			return true
		}
	}
	return false
}

// status is what leaves the pass: the version, whether it finished, and fixed
// integer counts plus one fixed error code. Never an ID, chat or content.
func (p *repairPass) status() map[string]any {
	counts := map[string]int64{}
	for _, k := range repairStates {
		counts[k] = p.counts[k]
	}
	return map[string]any{"version": p.version, "done": p.done, "rows_scanned": p.scanned, "counts": counts, "last_error": p.lastError}
}

// writeStatus updates .repair-status only when it changed; a failed write is
// retried on the next tick and never stops capture.
func (p *repairPass) writeStatus() {
	if !p.loaded && p.lastError == "" {
		return
	}
	key := string(mustJSON(p.status()))
	if key == p.reported {
		return
	}
	value := p.status()
	value["observed_at"] = p.now().UTC().Format(time.RFC3339Nano)
	if atomicPrivate(p.s.Config.EventsDirectory, ".repair-status", mustJSON(value)) == nil {
		p.reported = key
	}
}

// repairWorker runs the count while the provider is connected. It holds no
// lifecycle lock and stops with the provider's context (disconnect, lost
// lease or shutdown all end that context or clear readiness).
func (p *Provider) repairWorker(ctx context.Context, interval time.Duration) {
	pass := newRepairPass(p.Capture)
	pass.active = func() bool { return p.ready.Load() && ctx.Err() == nil }
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		if !p.ready.Load() {
			continue
		}
		pass.tick(ctx)
	}
}
