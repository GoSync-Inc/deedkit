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
	repairBatchRows   = 50   // rows classified per tick at most
	repairWindowSeqs  = 1000 // journal sequence numbers one read may cover
	repairTimeSlice   = 200 * time.Millisecond
	repairStatementTO = 2 * time.Second // bound of every SQLite statement of the pass
)

// The fixed set of tallies; anything else is never stored or reported.
var repairStates = []string{"would_add_content", "label_only", "same", "no_raw", "placeholder", "sender_key_only", "parse_error"}

type repairRow struct {
	Seq      int64
	Envelope []byte
	Raw      []byte
}

type repairPass struct {
	s        *CaptureStore
	version  int
	slice    time.Duration
	stmt     time.Duration
	read     func(ctx context.Context, after, upto int64, limit int) ([]repairRow, error)
	classify func(repairRow) string
	now      func() time.Time

	loaded    bool
	done      bool
	lastSeq   int64
	scanned   int64
	counts    map[string]int64
	lastError string
	reported  string
}

func newRepairPass(s *CaptureStore) *repairPass {
	p := &repairPass{s: s, version: REPAIR_VERSION, slice: repairTimeSlice, stmt: repairStatementTO, now: time.Now}
	p.read = s.repairRows
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

// repairRows returns candidate rows in (after, upto], oldest first. The text
// prefilter only narrows the read: json.Marshal escapes quotes inside values,
// so the marker can only match the envelope's own field; repairCandidate decides.
func (s *CaptureStore) repairRows(ctx context.Context, after, upto int64, limit int) ([]repairRow, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT seq,envelope,raw FROM capture WHERE seq>? AND seq<=? AND instr(envelope,'"messageType":"unknown"')>0 ORDER BY seq LIMIT ?`, after, upto, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []repairRow
	for rows.Next() {
		var r repairRow
		if err = rows.Scan(&r.Seq, &r.Envelope, &r.Raw); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// captureQueued reports live capture work: a held capture lock, an unexported
// journal row or a due history download. The pass yields to all of them.
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

// repairCountsJSON is the stored counts column: the fixed integers only.
func repairCountsJSON(scanned int64, counts map[string]int64) []byte {
	out := map[string]int64{"scanned": scanned}
	for _, k := range repairStates {
		out[k] = counts[k]
	}
	return mustJSON(out)
}

// load reads this version's progress, or starts it: older versions' rows are
// deleted in the same transaction. The table is created here, not at open, so
// a full database can never stop capture from starting.
func (p *repairPass) load(ctx context.Context) (string, error) {
	c, cancel := context.WithTimeout(ctx, p.stmt)
	defer cancel()
	if _, err := p.s.db.ExecContext(c, `CREATE TABLE IF NOT EXISTS repair_progress(version INTEGER PRIMARY KEY,last_seq INTEGER NOT NULL,done INTEGER NOT NULL,counts TEXT NOT NULL)`); err != nil {
		return "repair_storage", err
	}
	p.counts = map[string]int64{}
	var done int
	var counts []byte
	err := p.s.db.QueryRowContext(c, "SELECT last_seq,done,counts FROM repair_progress WHERE version=?", p.version).Scan(&p.lastSeq, &done, &counts)
	if err == nil {
		var stored map[string]int64
		if err = json.Unmarshal(counts, &stored); err != nil {
			return "repair_read", err
		}
		p.scanned = stored["scanned"]
		for _, k := range repairStates {
			p.counts[k] = stored[k]
		}
		p.done = done == 1
		p.loaded = true
		return "", nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return "repair_read", err
	}
	tx, err := p.s.db.BeginTx(c, nil)
	if err != nil {
		return "repair_storage", err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(c, "DELETE FROM repair_progress WHERE version<>?", p.version); err != nil {
		return "repair_storage", err
	}
	p.lastSeq, p.scanned, p.done = 0, 0, false
	if _, err = tx.ExecContext(c, "INSERT INTO repair_progress(version,last_seq,done,counts)VALUES(?,0,0,?)", p.version, repairCountsJSON(0, nil)); err != nil {
		return "repair_storage", err
	}
	if err = tx.Commit(); err != nil {
		return "repair_storage", err
	}
	p.loaded = true
	return "", nil
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
	if p.done || ctx.Err() != nil {
		return ""
	}
	if busy, err := p.captureQueued(ctx); err != nil {
		return "repair_read"
	} else if busy {
		return ""
	}
	if !p.loaded {
		if code, _ := p.load(ctx); code != "" {
			return code
		}
		if p.done {
			return ""
		}
	}
	c, cancel := context.WithTimeout(ctx, p.stmt)
	var maxSeq int64
	err := p.s.db.QueryRowContext(c, "SELECT COALESCE(MAX(seq),0) FROM capture").Scan(&maxSeq)
	cancel()
	if err != nil {
		return "repair_read"
	}
	upto := min(p.lastSeq+repairWindowSeqs, maxSeq)
	c, cancel = context.WithTimeout(ctx, p.stmt)
	rows, err := p.read(c, p.lastSeq, upto, repairBatchRows)
	cancel()
	if err != nil {
		return "repair_read"
	}
	next, scanned := p.lastSeq, p.scanned
	counts := map[string]int64{}
	for k, v := range p.counts {
		counts[k] = v
	}
	deadline := p.now().Add(p.slice)
	complete := true
	for _, r := range rows {
		if ctx.Err() != nil || p.now().After(deadline) {
			complete = false
			break
		}
		if selected, _, _ := repairCandidate(r.Envelope); selected {
			scanned++
			state := p.safeClassify(r)
			if !validRepairState(state) {
				state = "parse_error"
			}
			counts[state]++
		}
		next = r.Seq
	}
	if complete && len(rows) < repairBatchRows {
		next = upto // the whole window was read
	}
	done := next >= maxSeq && complete
	c, cancel = context.WithTimeout(ctx, p.stmt)
	defer cancel()
	doneFlag := 0
	if done {
		doneFlag = 1
	}
	// Fail closed: the in-memory progress moves only after the row is durable.
	if _, err = p.s.db.ExecContext(c, "UPDATE repair_progress SET last_seq=?,done=?,counts=? WHERE version=?", next, doneFlag, repairCountsJSON(scanned, counts), p.version); err != nil {
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
