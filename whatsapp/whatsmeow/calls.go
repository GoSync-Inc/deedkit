package main

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"regexp"
	"sync"
	"time"

	waBinary "go.mau.fi/whatsmeow/binary"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	waSyncAction "go.mau.fi/whatsmeow/proto/waSyncAction"
	waWeb "go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

// Incoming WhatsApp calls (ANYM-923). A call is passive evidence: the adapter
// records it and never answers, rejects or acknowledges it toward the caller
// (this file holds no client). An offer is kept in the local calls table until
// its end is seen; then one service entry is journaled in the caller's chat (or
// the group) under a provider ID derived from the call ID, so a redelivered
// event, a synced call-log message or a call log record of the same call names
// the same entry. An offer with no end within callEndWindow is journaled with
// the outcome unknown. Outgoing calls are not recorded.

var errCallEvent = errors.New("skipped_call_event")

const (
	callEndWindow = 2 * time.Hour
	// A closed call's row only deduplicates late redeliveries and synced logs.
	callRetention = 30 * 24 * time.Hour
)

var callIDPattern = regexp.MustCompile(`^[A-Za-z0-9_.:-]{1,128}$`)

// callsMu orders the offer, end and expiry of calls across the event handler
// and the replay worker.
var callsMu sync.Mutex

func ensureCallTable(db *sql.DB) error {
	for _, q := range []string{`CREATE TABLE IF NOT EXISTS calls(call_id TEXT PRIMARY KEY,state TEXT NOT NULL,chat TEXT NOT NULL,sender TEXT NOT NULL,media TEXT NOT NULL,grp INTEGER NOT NULL,started_at INTEGER NOT NULL,accepted INTEGER NOT NULL DEFAULT 0,observations BLOB NOT NULL,updated_ms INTEGER NOT NULL)`,
		`CREATE INDEX IF NOT EXISTS calls_pending ON calls(started_at) WHERE state='pending'`} {
		if _, err := db.Exec(q); err != nil {
			return err
		}
	}
	return nil
}

type callRecord struct {
	id, chat, sender, media, outcome string
	group                            bool
	started, ended                   int64 // ended 0: no end known
	duration                         *int64
	observations                     []any
}

// handleCallEvent captures a call event. An event the archive cannot represent
// is counted under skipped_call_event and acknowledged (true), so whatsmeow
// never redelivers it and the handler goes on; only a store, disk or spool
// failure returns false, as for a message.
func (p *Provider) handleCallEvent(value any) bool {
	err := p.captureCallEvent(value)
	if skippableCapture(err) {
		err = p.Capture.skip(err)
	}
	if err != nil {
		p.captureFailure(err)
		return false
	}
	return true
}

func validCallMeta(m types.BasicCallMeta) bool {
	return callIDPattern.MatchString(m.CallID) && m.Timestamp.Unix() > 0 && m.Timestamp.Unix() <= maxEnvelopeUnix
}

func (p *Provider) captureCallEvent(value any) error {
	var meta types.BasicCallMeta
	media, group := "", false
	switch e := value.(type) {
	case *events.CallOffer:
		if e == nil {
			return errCallEvent
		}
		meta, media = e.BasicCallMeta, offerMedia(e.Data)
		group = !e.GroupJID.IsEmpty()
	case *events.CallOfferNotice:
		if e == nil {
			return errCallEvent
		}
		meta, group = e.BasicCallMeta, true
		if e.Media == "audio" || e.Media == "video" {
			media = e.Media
		}
	case *events.CallAccept:
		if e == nil || !validCallMeta(e.BasicCallMeta) {
			return errCallEvent
		}
		return p.Capture.acceptCall(e.CallID)
	case *events.CallReject:
		if e == nil || !validCallMeta(e.BasicCallMeta) {
			return errCallEvent
		}
		return p.Capture.endCall(e.CallID, "rejected", e.Timestamp.Unix())
	case *events.CallTerminate:
		if e == nil || !validCallMeta(e.BasicCallMeta) {
			return errCallEvent
		}
		return p.Capture.endCall(e.CallID, "", e.Timestamp.Unix())
	default:
		return nil
	}
	if !validCallMeta(meta) || meta.CallCreator.IsEmpty() {
		return errCallEvent
	}
	if ownedIdentity(meta.CallCreator, p.Capture.Owner, p.Capture.OwnerLID) {
		return nil // an outgoing call (out of scope)
	}
	r := callRecord{id: meta.CallID, media: media, group: group, started: meta.Timestamp.Unix(), sender: LegacyChatID(meta.CallCreator), observations: []any{}}
	if pair := observedPair(meta.CallCreator, meta.CallCreatorAlt, time.Now().UTC().Format(time.RFC3339Nano)); pair != nil {
		r.observations = append(r.observations, pair)
	}
	if group {
		if meta.GroupJID.Server != types.GroupServer {
			return errCallEvent
		}
		r.chat = LegacyChatID(meta.GroupJID)
	} else {
		chat, err := p.Capture.callChat(meta.CallCreator, meta.CallCreatorAlt)
		if err != nil {
			return err
		}
		r.chat, r.sender = chat, chat
	}
	if !chatPattern.MatchString(r.chat) || !envelopeIdentifier(r.sender, maxEnvelopeSender) {
		return errCallEvent
	}
	policy, err := p.policy()
	if err != nil {
		return err
	}
	if !policy.Allows(r.chat) {
		return nil
	}
	return p.Capture.offerCall(r)
}

// offerMedia reads a 1:1 offer's media: a video call offers a video stream.
func offerMedia(n *waBinary.Node) string {
	if n == nil {
		return ""
	}
	media := ""
	for _, child := range n.GetChildren() {
		if child.Tag == "video" {
			return "video"
		}
		if child.Tag == "audio" {
			media = "audio"
		}
	}
	return media
}

// callChat names the caller's private chat in the form (phone number or LID)
// the journal already has messages in, else the caller's own form.
func (s *CaptureStore) callChat(creator, alt types.JID) (string, error) {
	forms := []types.JID{creator.ToNonAD()}
	if !alt.IsEmpty() {
		forms = append(forms, alt.ToNonAD())
	}
	if s.Alias != nil {
		if other := s.Alias(creator.ToNonAD()); !other.IsEmpty() {
			forms = append(forms, other.ToNonAD())
		}
	}
	for _, form := range forms {
		var one int
		err := s.db.QueryRow("SELECT 1 FROM capture WHERE chat=? LIMIT 1", LegacyChatID(form)).Scan(&one)
		if err == nil {
			return LegacyChatID(form), nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return "", err
		}
	}
	return LegacyChatID(forms[0]), nil
}

func (s *CaptureStore) offerCall(r callRecord) error {
	callsMu.Lock()
	defer callsMu.Unlock()
	obs, _ := json.Marshal(r.observations)
	_, err := s.db.Exec("INSERT INTO calls(call_id,state,chat,sender,media,grp,started_at,observations,updated_ms)VALUES(?,'pending',?,?,?,?,?,?,?)ON CONFLICT(call_id)DO NOTHING", r.id, r.chat, r.sender, r.media, r.group, r.started, obs, time.Now().UnixMilli())
	return err
}

// acceptCall notes an accept seen for a pending call: it ends as answered.
func (s *CaptureStore) acceptCall(id string) error {
	callsMu.Lock()
	defer callsMu.Unlock()
	_, err := s.db.Exec("UPDATE calls SET accepted=1,updated_ms=? WHERE call_id=? AND state='pending'", time.Now().UnixMilli(), id)
	return err
}

// endCall journals a pending call with its outcome; "" derives it from an
// accept seen. A call never offered here, or already closed, adds nothing.
func (s *CaptureStore) endCall(id, outcome string, at int64) error {
	callsMu.Lock()
	defer callsMu.Unlock()
	r, accepted, err := s.pendingCall(id)
	if err != nil || r == nil {
		return err
	}
	if outcome == "" {
		outcome = "missed"
		if accepted {
			outcome = "answered"
		}
	}
	r.outcome, r.ended = outcome, at
	if at < r.started {
		r.ended = r.started
	}
	return s.closeCall(*r, "live")
}

func (s *CaptureStore) pendingCall(id string) (*callRecord, bool, error) {
	r := callRecord{id: id}
	var accepted bool
	var obs []byte
	err := s.db.QueryRow("SELECT chat,sender,media,grp,started_at,accepted,observations FROM calls WHERE call_id=? AND state='pending'", id).Scan(&r.chat, &r.sender, &r.media, &r.group, &r.started, &accepted, &obs)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	if json.Unmarshal(obs, &r.observations) != nil || r.observations == nil {
		r.observations = []any{}
	}
	return &r, accepted, nil
}

// closeCall journals the entry once and marks the call closed. A journaled row
// whose export waits for spool space still closes the call: replay publishes it.
func (s *CaptureStore) closeCall(r callRecord, origin string) error {
	envelope, err := callEnvelope(s.Config.Account, r, origin)
	if err != nil {
		return err
	}
	id := envelope["event"].(map[string]any)["messageId"].(string)
	var one int
	err = s.db.QueryRow("SELECT 1 FROM capture WHERE chat=? AND message_id=? LIMIT 1", r.chat, id).Scan(&one)
	if errors.Is(err, sql.ErrNoRows) {
		err = s.append(r.chat, id, r.started, nil, envelope, origin == "history")
	}
	if err != nil && !spoolBackpressure(err) {
		return err
	}
	obs, _ := json.Marshal(r.observations)
	if _, e := s.db.Exec("INSERT INTO calls(call_id,state,chat,sender,media,grp,started_at,observations,updated_ms)VALUES(?,'done',?,?,?,?,?,?,?)ON CONFLICT(call_id)DO UPDATE SET state='done',updated_ms=excluded.updated_ms", r.id, r.chat, r.sender, r.media, r.group, r.started, obs, time.Now().UnixMilli()); e != nil {
		return e
	}
	return err
}

func callEnvelope(account string, r callRecord, origin string) (map[string]any, error) {
	if !callIDPattern.MatchString(r.id) || !chatPattern.MatchString(r.chat) || !envelopeIdentifier(r.sender, maxEnvelopeSender) || r.started <= 0 || r.started > maxEnvelopeUnix || r.ended < 0 || r.ended > maxEnvelopeUnix {
		return nil, errCallEvent
	}
	var media, ended, duration any
	if r.media != "" {
		media = r.media
	}
	if r.ended > 0 {
		ended = r.ended
	}
	if r.duration != nil {
		duration = *r.duration
	}
	h := sha256.Sum256(nil)
	formatting := map[string]any{"version": 1, "state": "wire_observed", "basis": "whatsapp_native_wire", "wire_sha256": hex.EncodeToString(h[:]), "entities": nil, "unsupported_entities": []any{}, "warnings": []string{"syntax_interpretation_unknown"}}
	event := map[string]any{"chatId": r.chat, "chatName": r.chat, "messageId": "call_" + r.id, "messageType": "call", "timestampResolution": "second", "durationSeconds": duration, "isService": true, "isSelfChat": false, "senderId": r.sender, "senderName": "", "body": "", "timestamp": r.started, "fromMe": false, "hasMedia": false, "filename": nil, "mimetype": nil, "filesize": nil, "replyToId": nil, "mentionsOwner": nil, "replyToOwner": nil, "forwarded": nil, "mediaKind": nil, "editedTimestampMs": nil, "identityObservations": r.observations, "formatting": formatting,
		"call": map[string]any{"call_id": r.id, "direction": "incoming", "media": media, "group": r.group, "started_at": r.started, "ended_at": ended, "outcome": r.outcome}}
	if origin == "history" {
		origin = "backfill"
	}
	return map[string]any{"kind": "whatsapp_event", "account": account, "event": event, "change": "message", "observed_at": time.Now().UTC().Format(time.RFC3339Nano), "origin": origin}, nil
}

// expireCalls closes every call offered before now-callEndWindow with no end
// seen as unknown, and forgets closed calls past their retention. It never
// stops capture: a failure is logged by its code only and retried next sweep.
func (p *Provider) expireCalls(now time.Time) error {
	policy, err := p.policy()
	if err != nil {
		return err
	}
	return p.Capture.expireCalls(now, policy)
}

func (s *CaptureStore) expireCalls(now time.Time, policy Policy) error {
	callsMu.Lock()
	defer callsMu.Unlock()
	rows, err := s.db.Query("SELECT call_id FROM calls WHERE state='pending' AND started_at<=? ORDER BY started_at LIMIT 100", now.Add(-callEndWindow).Unix())
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
		r, _, err := s.pendingCall(id)
		if err != nil || r == nil {
			return err
		}
		r.outcome = "unknown"
		if !policy.Allows(r.chat) {
			_, err = s.db.Exec("UPDATE calls SET state='done',updated_ms=? WHERE call_id=?", now.UnixMilli(), id)
		} else if err = s.closeCall(*r, "live"); skippableCapture(err) {
			if err = s.skip(err); err == nil {
				_, err = s.db.Exec("UPDATE calls SET state='done',updated_ms=? WHERE call_id=?", now.UnixMilli(), id)
			}
		}
		if err != nil && !spoolBackpressure(err) {
			return err
		}
	}
	_, err = s.db.Exec("DELETE FROM calls WHERE state='done' AND updated_ms<?", now.Add(-callRetention).UnixMilli())
	return err
}

func (p *Provider) sweepCalls(now time.Time) {
	if err := p.expireCalls(now); err != nil && !spoolBackpressure(err) {
		_ = json.NewEncoder(os.Stderr).Encode(map[string]any{"state": "call_sweep_failed", "reason": safeError(err)})
	}
}

// putCallLog journals a call-log message or a synced call record, unless the
// same call ID already has its entry (a live call, or an earlier sync).
func (s *CaptureStore) putCallLog(r callRecord, origin string) error {
	callsMu.Lock()
	defer callsMu.Unlock()
	var state string
	err := s.db.QueryRow("SELECT state FROM calls WHERE call_id=?", r.id).Scan(&state)
	if err == nil && state == "done" {
		return nil
	}
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	return s.closeCall(r, origin)
}

// callLogMessage maps a call-log message (live or synced). Its message ID is
// taken as the call ID. ok is false for a message that is no incoming ended
// call: it keeps the message path it had before ANYM-923 (outgoing calls are
// out of scope, an ongoing one has no outcome yet).
func callLogMessage(chat, sender types.JID, id string, fromMe bool, at int64, m *waE2E.CallLogMessage) (r *callRecord, ok bool, err error) {
	if m == nil || fromMe {
		return nil, false, nil
	}
	outcome := map[waE2E.CallLogMessage_CallOutcome]string{waE2E.CallLogMessage_CONNECTED: "answered", waE2E.CallLogMessage_ACCEPTED_ELSEWHERE: "answered", waE2E.CallLogMessage_MISSED: "missed", waE2E.CallLogMessage_SILENCED_BY_DND: "missed", waE2E.CallLogMessage_SILENCED_UNKNOWN_CALLER: "missed", waE2E.CallLogMessage_REJECTED: "rejected", waE2E.CallLogMessage_FAILED: "unknown"}[m.GetCallOutcome()]
	if outcome == "" {
		return nil, false, nil
	}
	if m.DurationSecs != nil && (m.GetDurationSecs() < 0 || m.GetDurationSecs() > 7*24*3600) {
		return nil, true, errCallEvent
	}
	return historyCallRecord(chat, sender, id, outcome, m.GetIsVideo(), at, m.DurationSecs)
}

// callLogRecord maps a synced call log record; it carries the call ID itself.
func callLogRecord(c *waSyncAction.CallLogRecord) (*callRecord, error) {
	if !c.GetIsIncoming() {
		return nil, nil
	}
	outcome := map[waSyncAction.CallLogRecord_CallResult]string{waSyncAction.CallLogRecord_CONNECTED: "answered", waSyncAction.CallLogRecord_ACCEPTEDELSEWHERE: "answered", waSyncAction.CallLogRecord_MISSED: "missed", waSyncAction.CallLogRecord_CANCELLED: "missed", waSyncAction.CallLogRecord_ABANDONED: "missed", waSyncAction.CallLogRecord_REJECTED: "rejected", waSyncAction.CallLogRecord_FAILED: "unknown", waSyncAction.CallLogRecord_UNAVAILABLE: "unknown", waSyncAction.CallLogRecord_INVALID: "unknown"}[c.GetCallResult()]
	if outcome == "" {
		return nil, nil // ongoing or upcoming: the live events or a later sync close it
	}
	creator, err := types.ParseJID(c.GetCallCreatorJID())
	if err != nil || creator.IsEmpty() {
		return nil, errCallEvent
	}
	chat := creator
	if c.GetGroupJID() != "" {
		if chat, err = types.ParseJID(c.GetGroupJID()); err != nil {
			return nil, errCallEvent
		}
	}
	at := c.GetStartTime()
	if at > 100000000000 { // milliseconds
		at /= 1000
	}
	if c.Duration != nil && (c.GetDuration() < 0 || c.GetDuration() > 7*24*3600) {
		return nil, errCallEvent
	}
	r, _, err := historyCallRecord(chat, creator, c.GetCallID(), outcome, c.GetIsVideo(), at, c.Duration)
	return r, err
}

func historyCallRecord(chat, sender types.JID, id, outcome string, video bool, at int64, duration *int64) (*callRecord, bool, error) {
	if !callIDPattern.MatchString(id) || at <= 0 || at > maxEnvelopeUnix || sender.IsEmpty() {
		return nil, true, errCallEvent
	}
	r := &callRecord{id: id, chat: LegacyChatID(chat), sender: LegacyChatID(sender), media: "audio", outcome: outcome, group: chat.Server == types.GroupServer, started: at, observations: []any{}}
	if video {
		r.media = "video"
	}
	if duration != nil {
		d := *duration
		r.duration, r.ended = &d, at+d
	}
	return r, true, nil
}

// captureCallLog journals a call-log message; handled is false for any other message.
func (p *Provider) captureCallLog(chat, sender types.JID, id string, fromMe bool, at int64, m *waE2E.CallLogMessage, origin string) (handled bool, err error) {
	if chat.Server != types.GroupServer {
		sender = chat
	}
	r, ok, err := callLogMessage(chat, sender, id, fromMe, at, m)
	if !ok {
		return false, nil
	}
	if err == nil && r != nil {
		err = p.Capture.putCallLog(*r, origin)
	}
	if skippableCapture(err) {
		err = p.Capture.skip(err)
	}
	return true, err
}

// captureCallLogRecords journals a history sync's incoming call log records.
func (p *Provider) captureCallLogRecords(records []*waSyncAction.CallLogRecord) error {
	for _, c := range records {
		r, err := callLogRecord(c)
		if err == nil && r != nil {
			var policy Policy
			if policy, err = p.policy(); err != nil {
				return err
			}
			if !policy.Allows(r.chat) {
				continue
			}
			err = p.Capture.putCallLog(*r, "history")
		}
		if skippableCapture(err) {
			err = p.Capture.skip(err)
		}
		if err != nil {
			return err
		}
	}
	return nil
}

// historySender is a synced group message's participant, else empty (the chat).
func historySender(wm *waWeb.WebMessageInfo) types.JID {
	j, err := types.ParseJID(wm.GetParticipant())
	if err != nil {
		return types.EmptyJID
	}
	return j
}
