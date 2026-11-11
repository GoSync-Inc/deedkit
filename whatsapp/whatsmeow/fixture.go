package main

// Small deterministic offline bridge lets the existing Python validator check
// real native envelopes. It cannot initialize or touch a WhatsApp session.
// (Moved from capture.go unchanged in behaviour; ANYM-963's republish fixture
// builds its synthetic messages with the same spec.)

import (
	"encoding/json"
	"errors"
	"os"
	"time"

	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

type fixtureSpec struct {
	Synthetic                                                            bool `json:"synthetic"`
	Account, Chat, Sender, Owner, ID, Text, Kind, Filename, Mime, Origin string
	OwnerLID                                                             string `json:"owner_lid"`
	Timestamp                                                            int64
	FromMe                                                               bool `json:"from_me"`
	Bytes                                                                uint64
	Duration                                                             uint32 `json:"duration_seconds"`
	ReplyID                                                              string `json:"reply_to_id"`
	ReplyParticipant                                                     string `json:"reply_participant"`
	Mentions                                                             []string
	SenderAlt                                                            string `json:"sender_alt"`
	RecipientAlt                                                         string `json:"recipient_alt"`
	SenderName                                                           string `json:"sender_name"`
	// kind "menu_list" (ANYM-837): a list menu's rows, the contract of the archive render and the Python parser.
	Rows []struct{ ID, Title string }
}

// event builds the synthetic provider message and the owner's identities.
func (v fixtureSpec) event() (*events.Message, types.JID, types.JID, error) {
	chat, err := ParseChatID(v.Chat)
	if err != nil {
		return nil, types.EmptyJID, types.EmptyJID, err
	}
	sender, err := ParseChatID(v.Sender)
	if err != nil && v.Sender != "" {
		return nil, types.EmptyJID, types.EmptyJID, err
	}
	owner, err := ParseChatID(v.Owner)
	if err != nil {
		return nil, types.EmptyJID, types.EmptyJID, err
	}
	lid := types.EmptyJID
	if v.OwnerLID != "" {
		lid, err = ParseChatID(v.OwnerLID)
		if err != nil {
			return nil, types.EmptyJID, types.EmptyJID, err
		}
	}
	msg := &waE2E.Message{Conversation: proto.String(v.Text)}
	ctx := &waE2E.ContextInfo{StanzaID: proto.String(v.ReplyID), Participant: proto.String(v.ReplyParticipant), MentionedJID: v.Mentions}
	switch v.Kind {
	case "image":
		msg = &waE2E.Message{ImageMessage: &waE2E.ImageMessage{Caption: proto.String(v.Text), Mimetype: proto.String(v.Mime), FileLength: proto.Uint64(v.Bytes), ContextInfo: ctx}}
	case "ptt":
		msg = &waE2E.Message{AudioMessage: &waE2E.AudioMessage{PTT: proto.Bool(true), Seconds: proto.Uint32(v.Duration), Mimetype: proto.String(v.Mime), FileLength: proto.Uint64(v.Bytes), ContextInfo: ctx}}
	case "ptv":
		msg = &waE2E.Message{PtvMessage: &waE2E.VideoMessage{Caption: proto.String(v.Text), Seconds: proto.Uint32(v.Duration), Mimetype: proto.String(v.Mime), FileLength: proto.Uint64(v.Bytes), ContextInfo: ctx}}
	case "document":
		msg = &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{FileName: proto.String(v.Filename), Caption: proto.String(v.Text), Mimetype: proto.String(v.Mime), FileLength: proto.Uint64(v.Bytes), ContextInfo: ctx}}
	case "album_photo":
		msg = &waE2E.Message{AssociatedChildMessage: &waE2E.FutureProofMessage{Message: &waE2E.Message{ImageMessage: &waE2E.ImageMessage{Caption: proto.String(v.Text), Mimetype: proto.String(v.Mime), FileLength: proto.Uint64(v.Bytes), ContextInfo: ctx}}}}
	case "sender_key":
		msg = &waE2E.Message{SenderKeyDistributionMessage: &waE2E.SenderKeyDistributionMessage{GroupID: proto.String(v.Chat), AxolotlSenderKeyDistributionMessage: []byte{1}}}
	case "template":
		msg = &waE2E.Message{TemplateMessage: &waE2E.TemplateMessage{Format: &waE2E.TemplateMessage_HydratedFourRowTemplate_{HydratedFourRowTemplate: &waE2E.TemplateMessage_HydratedFourRowTemplate{HydratedContentText: proto.String(v.Text)}}}}
	case "location":
		msg = &waE2E.Message{LocationMessage: &waE2E.LocationMessage{DegreesLatitude: proto.Float64(1.5), DegreesLongitude: proto.Float64(2.5), Name: proto.String(v.Text)}}
	case "menu_list":
		list := &waE2E.ListMessage{Description: proto.String(v.Text), Sections: []*waE2E.ListMessage_Section{{}}}
		for _, r := range v.Rows {
			list.Sections[0].Rows = append(list.Sections[0].Rows, &waE2E.ListMessage_Row{RowID: proto.String(r.ID), Title: proto.String(r.Title)})
		}
		msg = &waE2E.Message{ListMessage: list}
	case "notification":
		msg = nil
	default:
		msg = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String(v.Text), ContextInfo: ctx}}
	}
	event := &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: chat, Sender: sender, IsGroup: chat.Server == types.GroupServer, IsFromMe: v.FromMe}, ID: v.ID, Timestamp: time.Unix(v.Timestamp, 0), PushName: v.SenderName}, Message: msg, RawMessage: msg}
	if v.SenderAlt != "" {
		event.Info.SenderAlt, _ = ParseChatID(v.SenderAlt)
	}
	if v.RecipientAlt != "" {
		event.Info.RecipientAlt, _ = ParseChatID(v.RecipientAlt)
	}
	return event, owner, lid, nil
}

func normalizeFixture(path string) error {
	var v fixtureSpec
	data, err := readPrivate(path, 1<<20)
	if err != nil {
		return err
	}
	if json.Unmarshal(data, &v) != nil || !v.Synthetic {
		return errors.New("invalid_fixture")
	}
	event, owner, lid, err := v.event()
	if err != nil {
		return err
	}
	if controlOnly(unwrap(event).Message) {
		return json.NewEncoder(os.Stdout).Encode(nil)
	} // as CaptureStore.Put: nothing is journaled
	if v.Account == "" {
		v.Account = "test"
	}
	envelope, err := captureEnvelope(v.Account, event, owner, fixtureOrigin(v.Origin), lid)
	if err != nil {
		return err
	}
	return json.NewEncoder(os.Stdout).Encode(envelope)
}
