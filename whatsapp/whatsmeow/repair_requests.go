package main

// ANYM-814: an opt-in repair asks the owner's own phone for the messages of an
// unverified span, one request at a time. Every request is reserved durably in
// the capture store before any network I/O and is never replayed: a row that
// may have reached the network ends `uncertain` and its anchor is never asked
// again (ANYM-441). A span closes only on an answer whose ON_DEMAND history
// sync carries the reserved request id as PeerDataRequestSessionID.

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"go.mau.fi/whatsmeow"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	waHistorySync "go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/types"
	"time"
)

const (
	repairCount            = 50
	repairSpanAnswers      = 10
	repairAnchorNoAnswers  = 2
	repairAnchorRetryAfter = 24 * time.Hour
	repairSpacing          = 5 * time.Minute
	repairDeadline         = 10 * time.Minute
	repairPause            = 6 * time.Hour
	repairPauseNoAnswers   = 2
)

// repairCorrelation is the build-time probe of criterion 3: the pinned
// whatsmeow proto must carry the field, or no answer is ever correlated.
var repairCorrelation = func(n *waE2E.HistorySyncNotification) string { return n.GetPeerDataRequestSessionID() }

// repairSend is the only network step; tests replace it with a fake.
var repairSend = func(ctx context.Context, c *whatsmeow.Client, id types.MessageID, anchor *types.MessageInfo, count int) error {
	own := c.Store.GetJID().ToNonAD()
	if own.IsEmpty() {
		return whatsmeow.ErrNotLoggedIn
	}
	_, err := c.SendMessage(ctx, own, c.BuildHistorySyncRequest(anchor, count), whatsmeow.SendRequestExtra{ID: id, Peer: true})
	return err
}

// repairID is replaced in tests; production uses the client's own generator.
var repairID = func(c *whatsmeow.Client) types.MessageID { return c.GenerateMessageID() }

type gapRequest struct {
	ID           string
	Chat         string
	Span         string
	AnchorID     string
	AnchorTS     int64
	AnchorFromMe bool
	Count        int
	State        string
	CreatedMS    int64
	DeadlineMS   int64
	Got          int
	OldestID     string
	OldestTS     int64
	OldestFromMe bool
}

func ensureRepairRequests(db *sql.DB) error {
	for _, q := range []string{
		`CREATE TABLE IF NOT EXISTS repair_requests(request_id TEXT PRIMARY KEY,account TEXT NOT NULL,chat TEXT NOT NULL,span_id TEXT NOT NULL,anchor_id TEXT NOT NULL,anchor_ts INTEGER NOT NULL,anchor_from_me INTEGER NOT NULL,count INTEGER NOT NULL,state TEXT NOT NULL,created_ms INTEGER NOT NULL,deadline_ms INTEGER NOT NULL DEFAULT 0,answered_ms INTEGER NOT NULL DEFAULT 0,got INTEGER NOT NULL DEFAULT 0,oldest_id TEXT NOT NULL DEFAULT '',oldest_ts INTEGER NOT NULL DEFAULT 0,oldest_from_me INTEGER NOT NULL DEFAULT 0)`,
		`CREATE INDEX IF NOT EXISTS repair_requests_span ON repair_requests(span_id,state)`,
		`CREATE TABLE IF NOT EXISTS repair_account(id INTEGER PRIMARY KEY CHECK(id=1),no_answer_streak INTEGER NOT NULL DEFAULT 0,paused_until_ms INTEGER NOT NULL DEFAULT 0,reason TEXT NOT NULL DEFAULT '')`,
		`INSERT INTO repair_account(id)VALUES(1)ON CONFLICT DO NOTHING`,
		// Restart: a reservation that never reached `sending` is released without
		// a send; one that may have started is uncertain, whatever the network did.
		`DELETE FROM repair_requests WHERE state='reserved'`,
		`UPDATE repair_requests SET state='uncertain' WHERE state='sending'`,
	} {
		if _, err := db.Exec(q); err != nil {
			return err
		}
	}
	return nil
}

// expireRepairs turns sent requests past their deadline into no_answer; two in
// a row pause the account. A no_answer spends none of the span's budget.
func expireRepairs(tx *sql.Tx, now time.Time) error {
	rows, err := tx.Query("SELECT request_id FROM repair_requests WHERE state='sent' AND deadline_ms<=? ORDER BY created_ms", now.UnixMilli())
	if err != nil {
		return err
	}
	var ids []string
	for rows.Next() {
		var id string
		if err = rows.Scan(&id); err != nil {
			rows.Close()
			return err
		}
		ids = append(ids, id)
	}
	rows.Close()
	if err = rows.Err(); err != nil {
		return err
	}
	for _, id := range ids {
		if _, err = tx.Exec("UPDATE repair_requests SET state='no_answer' WHERE request_id=? AND state='sent'", id); err != nil {
			return err
		}
		if _, err = tx.Exec("UPDATE repair_account SET no_answer_streak=no_answer_streak+1 WHERE id=1"); err != nil {
			return err
		}
		if _, err = tx.Exec("UPDATE repair_account SET no_answer_streak=0,paused_until_ms=?,reason='phone_unreachable' WHERE id=1 AND no_answer_streak>=?", now.Add(repairPause).UnixMilli(), repairPauseNoAnswers); err != nil {
			return err
		}
	}
	return nil
}

func (s *CaptureStore) inRepairTx(fn func(*sql.Tx) error) error {
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	if err = fn(tx); err != nil {
		_ = tx.Rollback()
		return err
	}
	return tx.Commit()
}

// reserveRepair checks every cap and commits a `reserved` row in one
// transaction. It consumes the account's history request row (the one an
// agent's request_chat_history uses), so neither follows the other too closely.
func (s *CaptureStore) reserveRepair(row gapRequest, now time.Time) error {
	return s.inRepairTx(func(tx *sql.Tx) error {
		if err := expireRepairs(tx, now); err != nil {
			return err
		}
		var paused int64
		if err := tx.QueryRow("SELECT paused_until_ms FROM repair_account WHERE id=1").Scan(&paused); err != nil {
			return err
		}
		if paused > now.UnixMilli() {
			return errors.New("gap_repair_paused")
		}
		count := func(q string, args ...any) (n int, err error) {
			err = tx.QueryRow(q, args...).Scan(&n)
			return
		}
		checks := []struct {
			query string
			args  []any
			limit int
			code  string
		}{
			{"SELECT COUNT(*) FROM repair_requests WHERE state IN('reserved','sending','sent')", nil, 1, "gap_repair_outstanding"},
			{"SELECT COUNT(*) FROM repair_requests WHERE span_id=? AND state='uncertain'", []any{row.Span}, 1, "gap_repair_uncertain"},
			{"SELECT COUNT(*) FROM repair_requests WHERE chat=? AND anchor_id=? AND state='uncertain'", []any{row.Chat, row.AnchorID}, 1, "gap_repair_uncertain"},
			{"SELECT COUNT(*) FROM repair_requests WHERE span_id=? AND state='answered'", []any{row.Span}, repairSpanAnswers, "gap_repair_budget_exhausted"},
			{"SELECT COUNT(*) FROM repair_requests WHERE chat=? AND anchor_id=? AND state='no_answer'", []any{row.Chat, row.AnchorID}, repairAnchorNoAnswers, "gap_repair_no_answer_limit"},
			{"SELECT COUNT(*) FROM repair_requests WHERE chat=? AND anchor_id=? AND state='no_answer' AND created_ms>?", []any{row.Chat, row.AnchorID, now.Add(-repairAnchorRetryAfter).UnixMilli()}, 1, "gap_repair_retry_not_due"},
		}
		for _, c := range checks {
			n, err := count(c.query, c.args...)
			if err != nil {
				return err
			}
			if n >= c.limit {
				return errors.New(c.code)
			}
		}
		result, err := tx.Exec("INSERT INTO history_requests(id,last_ms)VALUES(1,?)ON CONFLICT(id)DO UPDATE SET last_ms=excluded.last_ms WHERE last_ms<=?", now.UnixMilli(), now.Add(-repairSpacing).UnixMilli())
		if err != nil {
			return err
		}
		if n, err := result.RowsAffected(); err != nil {
			return err
		} else if n != 1 {
			return errors.New("history_rate_limited")
		}
		_, err = tx.Exec("INSERT INTO repair_requests(request_id,account,chat,span_id,anchor_id,anchor_ts,anchor_from_me,count,state,created_ms)VALUES(?,?,?,?,?,?,?,?,'reserved',?)", row.ID, s.Config.Account, row.Chat, row.Span, row.AnchorID, row.AnchorTS, row.AnchorFromMe, row.Count, now.UnixMilli())
		return err
	})
}

// moveRepair is one committed state step; it fails when the row is not in `from`.
func (s *CaptureStore) moveRepair(id, from, to string, deadline int64) error {
	result, err := s.db.Exec("UPDATE repair_requests SET state=?,deadline_ms=? WHERE request_id=? AND state=?", to, deadline, id, from)
	if err != nil {
		return err
	}
	if n, err := result.RowsAffected(); err != nil {
		return err
	} else if n != 1 {
		return errors.New("durable_capture_failed")
	}
	return nil
}

// repairAnchor is the message a request asks before: the oldest message of the
// previous correlated answer, else the oldest cached real message at or after
// the span's `to` time.
func (s *CaptureStore) repairAnchor(chat string, before int64, after string) (*types.MessageInfo, error) {
	jid, err := ParseChatID(chat)
	if err != nil {
		return nil, errors.New("invalid_request")
	}
	if after != "" {
		var id string
		var ts int64
		var fromMe bool
		err = s.db.QueryRow("SELECT oldest_id,oldest_ts,oldest_from_me FROM repair_requests WHERE request_id=? AND chat=? AND state='answered' AND oldest_id<>''", after, chat).Scan(&id, &ts, &fromMe)
		if err != nil {
			return nil, errors.New("history_anchor_unavailable")
		}
		return &types.MessageInfo{MessageSource: types.MessageSource{Chat: jid, IsFromMe: fromMe}, ID: id, Timestamp: time.Unix(ts, 0)}, nil
	}
	rows, err := s.db.Query("SELECT raw FROM capture WHERE seq IN(SELECT MAX(seq)FROM capture WHERE chat=? AND source_ts>=? GROUP BY message_id)AND length(raw)>0 ORDER BY source_ts,message_id LIMIT 200", chat, before)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var raw []byte
		if err = rows.Scan(&raw); err != nil {
			return nil, err
		}
		msg, err := unmarshalMessage(raw)
		if err != nil {
			continue
		}
		if m := msg.Message; m == nil || m.GetProtocolMessage() != nil || m.GetReactionMessage() != nil || msg.Info.Sender.IsEmpty() {
			continue
		}
		return &msg.Info, nil
	}
	return nil, errors.New("history_anchor_unavailable")
}

// gapRepairRequest reserves, sends and returns; the answer arrives later
// through the history worker, and the deadline is checked by repairStatus.
func (p *Provider) gapRepairRequest(ctx context.Context, raw json.RawMessage, policy Policy) (any, error) {
	if !p.Config.GapRepair {
		return nil, errors.New("gap_repair_disabled")
	}
	var v struct {
		Chat     string `json:"chat"`
		Span     string `json:"span_id"`
		BeforeTS int64  `json:"before_ts"`
		After    string `json:"after_request"`
	}
	if json.Unmarshal(raw, &v) != nil || v.Span == "" || len(v.Span) > 64 || len(v.After) > 128 || v.BeforeTS < 1 || v.BeforeTS > maxEnvelopeUnix {
		return nil, errors.New("invalid_request")
	}
	if !policy.Allows(v.Chat) {
		return nil, errors.New("chat_excluded")
	}
	anchor, err := p.Capture.repairAnchor(v.Chat, v.BeforeTS, v.After)
	if err != nil {
		return nil, err
	}
	id := string(repairID(p.Client))
	row := gapRequest{ID: id, Chat: v.Chat, Span: v.Span, AnchorID: anchor.ID, AnchorTS: anchor.Timestamp.Unix(), AnchorFromMe: anchor.IsFromMe, Count: repairCount}
	if err = p.Capture.reserveRepair(row, time.Now()); err != nil {
		return nil, err
	}
	release := func() { _, _ = p.Capture.db.Exec("DELETE FROM repair_requests WHERE request_id=? AND state='reserved'", id) }
	fresh, err := p.policy()
	if err != nil || !fresh.Allows(v.Chat) || !startChange(ctx) {
		release()
		if err != nil {
			return nil, err
		}
		if !fresh.Allows(v.Chat) {
			return nil, errors.New("chat_excluded")
		}
		return nil, errors.New("operation_target_unavailable")
	}
	// Committed before any network I/O: from here a restart never sends again.
	if err = p.Capture.moveRepair(id, "reserved", "sending", 0); err != nil {
		return nil, err
	}
	send, done := context.WithTimeout(whatsmeow.WithoutFrameRetry(ctx), 10*time.Second)
	err = repairSend(send, p.Client, types.MessageID(id), anchor, repairCount)
	done()
	if err != nil {
		_ = p.Capture.moveRepair(id, "sending", "uncertain", 0)
		return nil, errors.New("history_request_unknown")
	}
	deadline := time.Now().Add(repairDeadline).UnixMilli()
	if err = p.Capture.moveRepair(id, "sending", "sent", deadline); err != nil {
		return nil, err
	}
	return map[string]any{"request_id": id, "state": "sent", "deadline_ms": deadline, "count": repairCount}, nil
}

// repairAnswer records a correlated ON_DEMAND answer. Anything else (a
// pairing batch, an agent's request, an unknown id) closes nothing. Its error
// never stops history capture: the request then ends no_answer (fail closed).
func (s *CaptureStore) repairAnswer(notif *waE2E.HistorySyncNotification, data *waHistorySync.HistorySync, now time.Time) error {
	if notif.GetSyncType() != waE2E.HistorySyncType_ON_DEMAND || data.GetSyncType() != waHistorySync.HistorySync_ON_DEMAND {
		return nil
	}
	session := repairCorrelation(notif)
	if session == "" {
		return nil
	}
	return s.inRepairTx(func(tx *sql.Tx) error {
		var chat, state string
		var got int
		var oldestTS int64
		err := tx.QueryRow("SELECT chat,state,got,oldest_ts FROM repair_requests WHERE request_id=?", session).Scan(&chat, &state, &got, &oldestTS)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		// sending: the answer can be processed before `sent` is committed.
		if state != "sending" && state != "sent" && state != "answered" {
			return nil
		}
		oldestID, oldestFromMe := "", false
		if state == "answered" {
			if err = tx.QueryRow("SELECT oldest_id,oldest_from_me FROM repair_requests WHERE request_id=?", session).Scan(&oldestID, &oldestFromMe); err != nil {
				return err
			}
		} else {
			got, oldestTS = 0, 0
		}
		for _, conv := range data.GetConversations() {
			jid, err := types.ParseJID(conv.GetID())
			if err != nil || LegacyChatID(jid) != chat {
				continue
			}
			for _, item := range conv.GetMessages() {
				wm := item.GetMessage()
				ts := int64(wm.GetMessageTimestamp())
				if wm.GetKey().GetID() == "" || ts == 0 || ts > maxEnvelopeUnix {
					continue
				}
				got++
				if oldestTS == 0 || ts < oldestTS {
					oldestTS, oldestID, oldestFromMe = ts, wm.GetKey().GetID(), wm.GetKey().GetFromMe()
				}
			}
		}
		if _, err = tx.Exec("UPDATE repair_requests SET state='answered',answered_ms=?,got=?,oldest_id=?,oldest_ts=?,oldest_from_me=? WHERE request_id=?", now.UnixMilli(), got, oldestID, oldestTS, oldestFromMe, session); err != nil {
			return err
		}
		_, err = tx.Exec("UPDATE repair_account SET no_answer_streak=0 WHERE id=1")
		return err
	})
}

// repairStatus applies deadlines and returns the account summary with the
// latest requests: ids, states and counts, never message content.
func (s *CaptureStore) repairStatus(enabled bool, now time.Time) (map[string]any, error) {
	if err := s.inRepairTx(func(tx *sql.Tx) error { return expireRepairs(tx, now) }); err != nil {
		return nil, err
	}
	var paused int64
	var reason string
	if err := s.db.QueryRow("SELECT paused_until_ms,reason FROM repair_account WHERE id=1").Scan(&paused, &reason); err != nil {
		return nil, err
	}
	if paused <= now.UnixMilli() {
		paused, reason = 0, ""
	}
	rows, err := s.db.Query("SELECT request_id,chat,span_id,state,count,created_ms,got,oldest_ts FROM(SELECT * FROM repair_requests ORDER BY created_ms DESC LIMIT 500)ORDER BY created_ms")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	requests, outstanding := []map[string]any{}, 0
	for rows.Next() {
		var id, chat, span, state string
		var count, got int
		var created, oldest int64
		if err = rows.Scan(&id, &chat, &span, &state, &count, &created, &got, &oldest); err != nil {
			return nil, err
		}
		if state == "reserved" || state == "sending" || state == "sent" {
			outstanding++
		}
		requests = append(requests, map[string]any{"request_id": id, "chat": chat, "span_id": span, "state": state, "count": count, "created_ms": created, "got": got, "oldest_ts": oldest})
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}
	status := map[string]any{"enabled": enabled, "outstanding": outstanding, "paused_until_ms": paused, "requests": requests}
	if reason != "" {
		status["reason"] = reason
	}
	return status, nil
}
