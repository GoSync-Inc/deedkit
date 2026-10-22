package main

import (
	"bytes"
	"compress/zlib"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	waHistorySync "go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
	"io"
	"os"
	"time"
)

const maxHistoryAttempts = 3

// Archive backpressure is retried after this delay without consuming one of
// the bounded download attempts; it resolves when the consumer drains.
const historyBackpressureDelay = 5 * time.Minute
const maxCompressedHistory = 32 << 20
const maxDecodedHistory = 64 << 20

type historyJob struct {
	Stanza  string
	Wire    []byte
	Attempt int
	Next    int64
}

// This only migrates the adapter's private cache, never the archive database.
func ensureHistoryRetryColumns(db *sql.DB) error {
	rows, err := db.Query("PRAGMA table_info(history_jobs)")
	if err != nil {
		return err
	}
	columns := map[string]bool{}
	for rows.Next() {
		var cid, notNull, pk int
		var name, typ string
		var defaultValue any
		if err = rows.Scan(&cid, &name, &typ, &notNull, &defaultValue, &pk); err != nil {
			rows.Close()
			return err
		}
		columns[name] = true
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	for _, column := range []struct{ name, definition string }{{"attempts", "INTEGER NOT NULL DEFAULT 0"}, {"next_attempt_ms", "INTEGER NOT NULL DEFAULT 0"}, {"error", "TEXT NOT NULL DEFAULT ''"}} {
		if !columns[column.name] {
			if _, err = db.Exec("ALTER TABLE history_jobs ADD COLUMN " + column.name + " " + column.definition); err != nil {
				return err
			}
		}
	}
	return nil
}

func historyRetryDelay(attempt int) time.Duration {
	if attempt < 2 {
		return time.Minute
	}
	return 5 * time.Minute
}

func (s *CaptureStore) historyStatus(state, reason string, attempt int, next int64) error {
	retry := "bounded_passive_download"
	if state == "failed" {
		retry = "explicit_only"
	} else if state == "captured" {
		retry = "none"
	}
	return atomicPrivate(s.Config.EventsDirectory, ".history-status", mustJSON(map[string]any{"state": state, "reason": reason, "attempt": attempt, "max_attempts": maxHistoryAttempts, "next_attempt_ms": next, "retry": retry, "coverage": "partial", "observed_at": time.Now().UTC().Format(time.RFC3339Nano)}))
}

// Reserve a passive download attempt before I/O. An interrupted attempt becomes
// eligible again after its persisted backoff; it never repeats a mutation.
func (s *CaptureStore) claimHistory(now time.Time) (*historyJob, error) {
	result, err := s.db.Exec("UPDATE history_jobs SET state='failed',error='history_attempts_exhausted' WHERE state='attempted' AND attempts>=? AND next_attempt_ms<=?", maxHistoryAttempts, now.UnixMilli())
	if err != nil {
		return nil, err
	}
	if n, err := result.RowsAffected(); err != nil {
		return nil, err
	} else if n > 0 {
		if err = s.historyStatus("failed", "history_attempts_exhausted", maxHistoryAttempts, 0); err != nil {
			return nil, err
		}
	}
	job := &historyJob{}
	err = s.db.QueryRow("SELECT stanza,wire,attempts FROM history_jobs WHERE state IN('pending','attempted')AND attempts<? AND next_attempt_ms<=? ORDER BY rowid LIMIT 1", maxHistoryAttempts, now.UnixMilli()).Scan(&job.Stanza, &job.Wire, &job.Attempt)
	if err != nil {
		return nil, err
	}
	job.Attempt++
	job.Next = now.Add(historyRetryDelay(job.Attempt)).UnixMilli()
	_, err = s.db.Exec("UPDATE history_jobs SET state='attempted',attempts=?,next_attempt_ms=? WHERE stanza=?", job.Attempt, job.Next, job.Stanza)
	if err == nil {
		err = s.historyStatus("downloading", "", job.Attempt, job.Next)
	}
	return job, err
}

func (s *CaptureStore) historyFailure(job *historyJob, reason string) error {
	state, next := "attempted", job.Next
	if job.Attempt >= maxHistoryAttempts {
		state, next = "failed", 0
	}
	if _, err := s.db.Exec("UPDATE history_jobs SET state=?,error=?,next_attempt_ms=? WHERE stanza=?", state, reason, next, job.Stanza); err != nil {
		return err
	}
	status := "retrying"
	if state == "failed" {
		status = "failed"
	}
	return s.historyStatus(status, reason, job.Attempt, next)
}

// historyBackpressure reports local capacity limits, not a failed download:
// a full export spool, the disk reserve or the bounded unconsumed journal.
func historyBackpressure(err error) bool {
	return err != nil && (err.Error() == "spool_full" || err.Error() == "disk_reserve" || err.Error() == "capture_overload")
}

// historyDeferred returns the attempt reserved by claimHistory and schedules
// the job after a backoff. The persisted status keeps the attempt number that
// ran, which is always within the reported 1..max_attempts range.
func (s *CaptureStore) historyDeferred(job *historyJob, reason string, now time.Time) error {
	next := now.Add(historyBackpressureDelay).UnixMilli()
	if _, err := s.db.Exec("UPDATE history_jobs SET state='attempted',attempts=?,error=?,next_attempt_ms=? WHERE stanza=?", job.Attempt-1, reason, next, job.Stanza); err != nil {
		return err
	}
	return s.historyStatus("retrying", reason, job.Attempt, next)
}

func (s *CaptureStore) EnqueueHistory(e *events.Message) error {
	e = unwrap(e)
	notif := e.Message.GetProtocolMessage().GetHistorySyncNotification()
	if notif == nil {
		return nil
	}
	// A notification outside the bounded history contract is permanently
	// invalid; only a full job queue below is (recoverable) overload.
	if (len(notif.GetInitialHistBootstrapInlinePayload()) == 0 && notif.GetFileLength() == 0) || notif.GetFileLength() > maxCompressedHistory || len(notif.GetInitialHistBootstrapInlinePayload()) > maxCompressedHistory {
		return errors.New("invalid_history_notification")
	}
	wire, err := proto.Marshal(e.RawMessage)
	if err != nil {
		return err
	}
	if len(wire) > 512<<10 {
		return errors.New("invalid_history_notification")
	}
	var n int
	if err = s.db.QueryRow("SELECT COUNT(*) FROM history_jobs").Scan(&n); err != nil {
		return err
	}
	if n >= 10000 {
		if _, err = s.db.Exec("DELETE FROM history_jobs WHERE rowid IN(SELECT rowid FROM history_jobs WHERE state='captured' ORDER BY rowid LIMIT 1000)"); err != nil {
			return err
		}
		if err = s.db.QueryRow("SELECT COUNT(*) FROM history_jobs").Scan(&n); err != nil {
			return err
		}
		if n >= 10000 {
			return errors.New("capture_overload")
		}
	}
	_, err = s.db.Exec("INSERT INTO history_jobs(stanza,wire)VALUES(?,?)ON CONFLICT DO NOTHING", e.Info.ID, wire)
	return err
}

func validateCompressedHistory(data []byte, maxDecoded int64) error {
	if len(data) == 0 || len(data) > maxCompressedHistory || maxDecoded < 1 {
		return errors.New("history_size_limit")
	}
	reader, err := zlib.NewReader(bytes.NewReader(data))
	if err != nil {
		return errors.New("invalid_history_compression")
	}
	defer reader.Close()
	count, err := io.Copy(io.Discard, io.LimitReader(reader, maxDecoded+1))
	if err != nil {
		return errors.New("invalid_history_compression")
	}
	if count > maxDecoded {
		return errors.New("history_size_limit")
	}
	return nil
}

func (p *Provider) downloadHistory(ctx context.Context, notif *waE2E.HistorySyncNotification) (*waHistorySync.HistorySync, error) {
	data := notif.GetInitialHistBootstrapInlinePayload()
	if len(data) == 0 {
		if notif.GetFileLength() == 0 || notif.GetFileLength() > maxCompressedHistory {
			return nil, errors.New("history_size_limit")
		}
		if err := p.Capture.checkDisk(int64(notif.GetFileLength()) + 32); err != nil {
			return nil, err
		}
		dir, err := nativeTempRoot(p.Config.SessionDirectory)
		if err != nil {
			return nil, err
		}
		file, err := os.CreateTemp(dir, ".history-download-*")
		if err != nil {
			return nil, err
		}
		defer os.Remove(file.Name())
		defer file.Close()
		if err = file.Chmod(0600); err != nil {
			return nil, err
		}
		// DownloadToFile authenticates/decrypts the native blob. Its encrypted
		// write has a small MAC/padding overhead; the resulting plaintext must
		// still match the advertised compressed length exactly.
		if err = p.Client.DownloadToFile(ctx, notif, &boundedMediaFile{File: file, Limit: maxCompressedHistory + 32}); err != nil {
			return nil, err
		}
		info, err := file.Stat()
		if err != nil || info.Size() != int64(notif.GetFileLength()) {
			return nil, errors.New("history_size_limit")
		}
		if _, err = file.Seek(0, io.SeekStart); err != nil {
			return nil, err
		}
		data, err = io.ReadAll(io.LimitReader(file, maxCompressedHistory+1))
		if err != nil {
			return nil, err
		}
	}
	if err := validateCompressedHistory(data, maxDecodedHistory); err != nil {
		return nil, err
	}
	// Reuse native storage of verified mappings, secrets and push names. Its
	// internal decompressor sees only these same immutable, pre-bounded bytes;
	// it cannot download or expand a different unbounded source.
	verified := proto.Clone(notif).(*waE2E.HistorySyncNotification)
	verified.InitialHistBootstrapInlinePayload = data
	return p.Client.DownloadHistorySync(ctx, verified, true)
}
func (p *Provider) captureHistory(data *waHistorySync.HistorySync) error {
	for _, conv := range data.GetConversations() {
		chat, err := types.ParseJID(conv.GetID())
		if err != nil {
			if err = p.Capture.skip(errors.New("invalid_history_chat")); err != nil {
				return err
			}
			continue
		}
		policy, err := p.policy()
		if err != nil {
			return err
		}
		if !policy.Allows(LegacyChatID(chat)) {
			continue
		}
		if conv.GetName() != "" {
			if _, err = p.Capture.db.Exec("INSERT INTO chats(chat,title,observed_at)VALUES(?,?,?)ON CONFLICT(chat)DO UPDATE SET title=excluded.title,observed_at=excluded.observed_at", LegacyChatID(chat), conv.GetName(), time.Now().UTC().Format(time.RFC3339Nano)); err != nil {
				return err
			}
		}
		for _, item := range conv.GetMessages() {
			policy, err = p.policy()
			if err != nil {
				return err
			}
			if !policy.Allows(LegacyChatID(chat)) {
				break
			}
			wm := item.GetMessage()
			if wm == nil || wm.GetKey().GetID() == "" || wm.GetMessageTimestamp() == 0 || wm.GetMessageTimestamp() > maxEnvelopeUnix {
				if err = p.Capture.skip(errors.New("invalid_history_message")); err != nil {
					return err
				}
				continue
			}
			var message *events.Message
			if wm.GetMessage() == nil && wm.GetMessageStubType() != 0 {
				message = &events.Message{SourceWebMsg: wm, Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: chat, IsGroup: chat.Server == types.GroupServer, IsFromMe: wm.GetKey().GetFromMe()}, ID: wm.GetKey().GetID(), Timestamp: time.Unix(int64(wm.GetMessageTimestamp()), 0)}}
			} else {
				message, err = p.Client.ParseWebMessage(chat, wm)
				if err != nil {
					if err = p.Capture.skip(errors.New("invalid_history_message")); err != nil {
						return err
					}
					continue
				}
			}
			if err = p.Capture.putOrSkip(message, "history"); err != nil {
				return err
			}
		}
		if err = p.captureHistoryReadState(chat, conv); err != nil {
			return err
		}
	}
	return nil
}

// captureHistoryReadState keeps the provider's unread count or mark-unread flag
// of a synced conversation (ANYM-275). It is stamped with the conversation's
// newest message time, never the sync time, so it never looks fresher than it is.
func (p *Provider) captureHistoryReadState(chat types.JID, conv *waHistorySync.Conversation) error {
	if conv.UnreadCount == nil && conv.MarkedAsUnread == nil {
		return nil
	}
	var newest uint64
	for _, item := range conv.GetMessages() {
		if at := item.GetMessage().GetMessageTimestamp(); at > newest && at <= maxEnvelopeUnix {
			newest = at
		}
	}
	if newest == 0 {
		newest = conv.GetConversationTimestamp()
	}
	if newest == 0 || newest > maxEnvelopeUnix {
		return nil
	}
	policy, err := p.policy()
	if err != nil {
		return err
	}
	if !policy.Allows(LegacyChatID(chat)) {
		return nil
	}
	// An absent count stays unknown; a zero is never derived from partial history.
	observation := map[string]any{"unread_count": nil, "marked_unread": conv.GetMarkedAsUnread()}
	if conv.UnreadCount != nil {
		observation["unread_count"] = conv.GetUnreadCount()
	}
	return p.Capture.readState(LegacyChatID(chat), "whatsapp_history_sync", time.Unix(int64(newest), 0), observation, true)
}
func (p *Provider) historyWorker(ctx context.Context) {
	ticker := time.NewTicker(3 * time.Second)
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
		if _, err := p.policy(); err != nil {
			p.fail(safeError(err))
			return
		}
		job, err := p.Capture.claimHistory(time.Now())
		if errors.Is(err, sql.ErrNoRows) {
			continue
		}
		if err != nil {
			p.fail("durable_capture_failed")
			return
		}
		msg := &waE2E.Message{}
		if proto.Unmarshal(job.Wire, msg) != nil {
			_ = p.Capture.historyFailure(job, "invalid_history_notification")
			p.fail("durable_capture_failed")
			return
		}
		notif := unwrap(&events.Message{RawMessage: msg}).Message.GetProtocolMessage().GetHistorySyncNotification()
		if notif == nil || notif.GetFileLength() > maxCompressedHistory {
			p.fail("capture_overload")
			return
		}
		fetch, done := context.WithTimeout(ctx, 90*time.Second)
		data, err := p.downloadHistory(fetch, notif)
		done()
		if err != nil {
			if historyBackpressure(err) {
				err = p.Capture.historyDeferred(job, "history_download_failed", time.Now())
			} else {
				err = p.Capture.historyFailure(job, "history_download_failed")
			}
			if err != nil {
				p.fail("durable_capture_failed")
				return
			}
			continue
		}
		if err = p.captureHistory(data); err != nil {
			if !historyBackpressure(err) {
				_ = p.Capture.historyFailure(job, "history_capture_failed")
			} else if e := p.Capture.historyDeferred(job, "history_capture_failed", time.Now()); e != nil {
				p.fail("durable_capture_failed")
				return
			}
			// Rows already journaled stay pending for the replay worker and are
			// skipped on retry. A full spool keeps readiness; local disk or
			// journal limits still surface as a provider failure.
			p.captureFailure(err)
			if spoolBackpressure(err) {
				continue
			}
			return
		}
		if _, err = p.Capture.db.Exec("UPDATE history_jobs SET state='captured',error='',next_attempt_ms=0 WHERE stanza=?", job.Stanza); err != nil {
			p.fail("durable_capture_failed")
			return
		}
		if err = p.Capture.historyStatus("captured", "", job.Attempt, 0); err != nil {
			p.fail("durable_capture_failed")
			return
		}
		receipt, done := context.WithTimeout(ctx, 10*time.Second)
		_ = p.Client.SendProtocolMessageReceipt(receipt, job.Stanza, types.ReceiptTypeHistorySync)
		done()
	}
}
func (p *Provider) requestHistory(ctx context.Context, raw json.RawMessage, policy Policy) (any, error) {
	var v struct {
		Chat  string `json:"chat"`
		Count int    `json:"count"`
	}
	if json.Unmarshal(raw, &v) != nil || v.Count < 1 || v.Count > 50 {
		return nil, errors.New("invalid_request")
	}
	if !policy.Allows(v.Chat) {
		return nil, errors.New("chat_excluded")
	}
	// Select the oldest retained real message, not the oldest of the newest page.
	anchor, err := p.Capture.historyAnchor(v.Chat)
	if err != nil {
		return nil, errors.New("history_anchor_unavailable")
	}

	now := time.Now()
	result, err := p.Capture.db.Exec("INSERT INTO history_requests(id,last_ms)VALUES(1,?)ON CONFLICT(id)DO UPDATE SET last_ms=excluded.last_ms WHERE last_ms<=?", now.UnixMilli(), now.Add(-time.Minute).UnixMilli())
	if err != nil {
		return nil, err
	}
	n, err := result.RowsAffected()
	if err != nil {
		return nil, err
	}
	if n != 1 {
		return nil, errors.New("history_rate_limited")
	}
	fresh, err := p.policy()
	if err != nil {
		return nil, err
	}
	if !fresh.Allows(v.Chat) {
		return nil, errors.New("chat_excluded")
	}
	send, done := context.WithTimeout(ctx, 10*time.Second)
	defer done()
	_, err = p.Client.SendPeerMessage(send, p.Client.BuildHistorySyncRequest(anchor, v.Count))
	if err != nil {
		return nil, errors.New("history_request_unknown")
	}
	return map[string]any{"requested": true, "count": v.Count, "pending": true, "coverage": "partial", "retry": "explicit_only"}, nil
}

func (s *CaptureStore) historyAnchor(chat string) (*types.MessageInfo, error) {
	rows, err := s.db.Query("SELECT raw FROM capture WHERE seq IN(SELECT MAX(seq)FROM capture WHERE chat=? GROUP BY message_id)AND length(raw)>0 ORDER BY source_ts,message_id LIMIT 1000", chat)
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
			return nil, err
		}
		m := msg.Message
		if m == nil || m.GetProtocolMessage() != nil || m.GetReactionMessage() != nil || msg.Info.Sender.IsEmpty() {
			continue
		}
		return &msg.Info, nil
	}
	return nil, sql.ErrNoRows
}
