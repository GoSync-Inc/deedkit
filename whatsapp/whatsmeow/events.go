package main

import (
	"context"
	"errors"
	"math"
	"strings"
	"time"

	"go.mau.fi/whatsmeow"
	waCommon "go.mau.fi/whatsmeow/proto/waCommon"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	waWeb "go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"go.mau.fi/whatsmeow/util/gcmutil"
	"go.mau.fi/whatsmeow/util/hkdfutil"
	"google.golang.org/protobuf/proto"
)

// WhatsApp event invitations in the archive (ANYM-924). An event is a chat message of kind "event": its body is the
// name and its block keeps what the sender stated (name, description, times, place, join link, cancelled, call).
// A later edit, whatsmeow's MESSAGE_EDIT wrapper or the EVENT_EDIT secret message WhatsApp sends, is a new revision
// of the same message; a cancellation is such an edit. Responses (going, not going, maybe) are counted only: the
// newest response of each responder, decrypted with the event's stored secret, kept here to count, never exported.
// Texts are sender-written data: stored bounded, a link is never opened. The adapter never answers an event, and
// nothing here writes a calendar.
const (
	maxEventName        = 256
	maxEventDescription = 2048
	maxEventLink        = 2048
)

var (
	// A response whose event, secret or content cannot be read; acknowledged, counted and left out.
	errEventResponse = errors.New("event_response_undecryptable")
	// An EVENT_EDIT secret message that cannot be read; acknowledged and counted.
	errEventEdit = errors.New("event_edit_undecryptable")
)

// eventResponseTable keeps the newest response of each responder to a journaled event, under the event's journal
// chat and ID. Only the counts leave it.
const eventResponseTable = `CREATE TABLE IF NOT EXISTS event_responses(chat TEXT NOT NULL,event_id TEXT NOT NULL,responder TEXT NOT NULL,response INTEGER NOT NULL,at_ms INTEGER NOT NULL,PRIMARY KEY(chat,event_id,responder))`

// eventBlock is the archived event: {name, description, start, end, location: {title, address, latitude, longitude}
// | null, join_link, canceled, is_call}. Times are UTC RFC 3339; an absent or unrepresentable one is null.
func eventBlock(v *waE2E.EventMessage) map[string]any {
	var location any
	if l := v.GetLocation(); l != nil {
		text := func(s string) any {
			if s, _ = contactText(s); s != "" {
				return s
			}
			return nil
		}
		// JSON has no NaN or infinity; the archive drops a block whose point is out of range.
		coordinate := func(set bool, f float64) any {
			if !set || math.IsNaN(f) || math.IsInf(f, 0) {
				return nil
			}
			return f
		}
		place := map[string]any{"title": text(l.GetName()), "address": text(l.GetAddress()),
			"latitude": coordinate(l.DegreesLatitude != nil, l.GetDegreesLatitude()), "longitude": coordinate(l.DegreesLongitude != nil, l.GetDegreesLongitude())}
		for _, x := range place {
			if x != nil {
				location = place
				break
			}
		}
	}
	return map[string]any{"name": envelopeText(v.GetName(), maxEventName), "description": nilString(envelopeText(v.GetDescription(), maxEventDescription)),
		"start": eventTime(v.GetStartTime()), "end": eventTime(v.GetEndTime()), "location": location,
		"join_link": nilString(strings.TrimSpace(envelopeText(v.GetJoinLink(), maxEventLink))), "canceled": v.GetIsCanceled(), "is_call": v.GetIsScheduleCall()}
}

// eventTime is a stated Unix second as UTC RFC 3339, else nil.
func eventTime(s int64) any {
	if s <= 0 || s > maxEnvelopeUnix {
		return nil
	}
	return time.Unix(s, 0).UTC().Format(time.RFC3339)
}

// notAChatMessage refuses a modification that is never a chat message of its own: a poll vote (polls.go) or an
// event response. The live and history handlers route both before they reach the journal.
func notAChatMessage(m *waE2E.Message) error {
	switch {
	case m.GetPollUpdateMessage() != nil:
		return errPollVote
	case m.GetEncEventResponseMessage() != nil:
		return errEventResponse
	}
	return nil
}

// withEventResponses adds the counts of an event's journaled responses to its envelope; an event nobody answered
// has none.
func (s *CaptureStore) withEventResponses(envelope map[string]any) error {
	v, ok := envelope["event"].(map[string]any)
	if !ok || v["messageType"] != "event" {
		return nil
	}
	chat, _ := v["chatId"].(string)
	id, _ := v["messageId"].(string)
	rows, err := s.db.Query("SELECT response,COUNT(*) FROM event_responses WHERE chat=? AND event_id=? GROUP BY response", chat, id)
	if err != nil {
		return err
	}
	defer rows.Close()
	counts, found := map[string]any{"going": 0, "not_going": 0, "maybe": 0}, false
	for rows.Next() {
		var response, n int
		if err = rows.Scan(&response, &n); err != nil {
			return err
		}
		if name := eventResponseName(waE2E.EventResponseMessage_EventResponseType(response)); name != "" {
			counts[name], found = n, true
		}
	}
	if err = rows.Err(); err != nil {
		return err
	}
	if found {
		v["eventResponses"] = counts
	}
	return nil
}

func eventResponseName(r waE2E.EventResponseMessage_EventResponseType) string {
	switch r {
	case waE2E.EventResponseMessage_GOING:
		return "going"
	case waE2E.EventResponseMessage_NOT_GOING:
		return "not_going"
	case waE2E.EventResponseMessage_MAYBE:
		return "maybe"
	}
	return ""
}

// putEventResponse keeps a responder's newest response. One person under its phone and LID forms is one responder.
func (s *CaptureStore) putEventResponse(chat, eventID string, responder types.JID, fromMe bool, response *waE2E.EventResponseMessage, fallback time.Time) error {
	if fromMe && !s.Owner.IsEmpty() {
		responder = s.Owner
	}
	forms := s.pollIdentities(responder)
	if len(forms) == 0 || eventResponseName(response.GetResponse()) == "" {
		return errEventResponse
	}
	key := LegacyChatID(forms[0])
	for _, j := range forms[1:] {
		key = min(key, LegacyChatID(j))
	}
	at := pollVoteTime(response.GetTimestampMS(), fallback)
	_, err := s.db.Exec(`INSERT INTO event_responses(chat,event_id,responder,response,at_ms)VALUES(?,?,?,?,?)
		ON CONFLICT(chat,event_id,responder)DO UPDATE SET response=excluded.response,at_ms=excluded.at_ms WHERE excluded.at_ms>=event_responses.at_ms`,
		chat, eventID, key, int(response.GetResponse()), at.UnixMilli())
	return err
}

// captureEventSecret handles the modifications of an event that arrive as their own messages: a response, counted
// on its event, and an EVENT_EDIT, journaled as an edit of its event. Anything else is not handled (false). One
// that cannot be read is counted and acknowledged; only a store, disk or spool failure returns an error.
func (p *Provider) captureEventSecret(e *events.Message, origin string) (bool, error) {
	var err error
	switch m := e.Message; {
	case m.GetEncEventResponseMessage() != nil:
		err = p.eventResponse(e, origin)
	case m.GetSecretEncryptedMessage().GetSecretEncType() == waE2E.SecretEncryptedMessage_EVENT_EDIT:
		err = p.eventEdit(e, origin)
	default:
		return false, nil
	}
	if skippableCapture(err) {
		err = p.Capture.skip(err)
	}
	return true, err
}

func (p *Provider) eventResponse(e *events.Message, origin string) error {
	enc := e.Message.GetEncEventResponseMessage()
	chat, eventID, event, err := p.Capture.secretTarget(e, enc.GetEventCreationMessageKey(), errEventResponse)
	if err != nil {
		return err
	}
	if unwrap(event).Message.GetEventMessage() == nil || p.Client == nil {
		return errEventResponse
	}
	plain, err := openMsgSecret(p.Client, e, enc.GetEventCreationMessageKey(), "Event Response", enc.GetEncIV(), enc.GetEncPayload())
	var response waE2E.EventResponseMessage
	if err != nil || proto.Unmarshal(plain, &response) != nil {
		return errEventResponse
	}
	if err = p.Capture.putEventResponse(chat, eventID, e.Info.Sender, e.Info.IsFromMe, &response, e.Info.Timestamp); err != nil {
		return err
	}
	// The event is journaled again with its new counts; unchanged counts are the same journal entry.
	return p.Capture.Put(event, origin)
}

// eventEdit journals a decrypted EVENT_EDIT as whatsmeow's MESSAGE_EDIT of its event. Only the creator edits an
// event, so the event is the editor's own message exactly when the edit is.
func (p *Provider) eventEdit(e *events.Message, origin string) error {
	target := e.Message.GetSecretEncryptedMessage().GetTargetMessageKey()
	if p.Client == nil || target.GetID() == "" {
		return errEventEdit
	}
	edited, err := decryptEventEdit(p.Client, e)
	if err != nil || edited.GetEventMessage() == nil {
		return errEventEdit
	}
	edit := *e
	key := &waCommon.MessageKey{RemoteJID: proto.String(e.Info.Chat.String()), FromMe: proto.Bool(e.Info.IsFromMe), ID: proto.String(target.GetID())}
	edit.Message = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(), Key: key, EditedMessage: edited, TimestampMS: proto.Int64(e.Info.Timestamp.UnixMilli())}}
	edit.RawMessage = edit.Message
	return p.Capture.Put(&edit, origin)
}

func decryptEventEdit(client *whatsmeow.Client, e *events.Message) (m *waE2E.Message, err error) {
	// whatsmeow's GCM open panics on a nonce that is not 12 bytes (polls.go); any panic is an unreadable edit.
	defer func() {
		if recover() != nil {
			m, err = nil, errEventEdit
		}
	}()
	if sem := e.Message.GetSecretEncryptedMessage(); len(sem.GetEncIV()) != 12 || len(sem.GetEncPayload()) == 0 {
		return nil, errEventEdit
	}
	return client.DecryptSecretEncryptedMessage(context.Background(), e)
}

// openMsgSecret opens a modification sealed with the original message's secret, as whatsmeow's decryptMsgSecret does
// for its use cases (whatsmeow has no event response helper): the original sender named by the key (or the one the
// secret was stored under), HKDF-SHA256 over ID, original sender, modifying sender and use case, AES-GCM with the ID
// and modifying sender as additional data.
func openMsgSecret(client *whatsmeow.Client, e *events.Message, key *waCommon.MessageKey, useCase string, iv, payload []byte) (plain []byte, err error) {
	defer func() {
		if recover() != nil {
			plain, err = nil, errEventResponse
		}
	}()
	if client == nil || client.Store == nil || client.Store.MsgSecrets == nil || key.GetID() == "" || len(iv) != 12 || len(payload) == 0 {
		return nil, errEventResponse
	}
	creator := e.Info.Sender
	if !key.GetFromMe() {
		raw := key.GetParticipant()
		if e.Info.Chat.Server == types.DefaultUserServer || e.Info.Chat.Server == types.HiddenUserServer {
			raw = key.GetRemoteJID()
		}
		if creator, err = types.ParseJID(raw); err != nil || creator.IsEmpty() {
			return nil, errEventResponse
		}
	}
	secret, stored, err := client.Store.MsgSecrets.GetMessageSecret(context.Background(), e.Info.Chat, creator, key.GetID())
	if err != nil || secret == nil {
		return nil, errEventResponse
	}
	modifier := e.Info.Sender.ToNonAD().String()
	for _, orig := range []types.JID{creator, stored} {
		if orig.IsEmpty() {
			continue
		}
		k := hkdfutil.SHA256(secret, nil, []byte(key.GetID()+orig.ToNonAD().String()+modifier+useCase), 32)
		if plain, err = gcmutil.Decrypt(k, iv, payload, []byte(key.GetID()+"\x00"+modifier)); err == nil {
			return plain, nil
		}
	}
	return nil, errEventResponse
}

// recordHistoryEventResponses keeps the responses a history sync carries on an event (WebMessageInfo.eventResponses,
// already decrypted) before the event itself is journaled, so it is journaled once, with its counts. The responder
// follows the rule of a synced poll vote (polls.go historyActor).
func (p *Provider) recordHistoryEventResponses(event *events.Message, responses []*waWeb.EventResponse) error {
	m := unwrap(event)
	if len(responses) == 0 || m.Message.GetEventMessage() == nil {
		return nil
	}
	for _, r := range responses {
		err := errEventResponse
		if key := r.GetEventResponseMessageKey(); r.GetEventResponseMessage() != nil && key.GetID() != "" {
			if responder, ok := p.Capture.historyActor(m, key); ok {
				err = p.Capture.putEventResponse(LegacyChatID(m.Info.Chat), LegacyMessageID(m), responder, key.GetFromMe(), r.GetEventResponseMessage(), time.UnixMilli(r.GetTimestampMS()))
			}
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
