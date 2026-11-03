package main

import (
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
)

func (e *operationEnv) messageOperation(op string) (result any, err error) {
	ctx, job, d, target, check, mutate, submitted, getMessage, text, integer, users, send := e.ctx, e.job, e.d, e.target, e.check, e.mutate, e.submitted, e.getMessage, e.text, e.integer, e.users, e.send
	switch op {
	case "send_location":
		var latitude, longitude float64
		if decodeValue(job.Parameters["latitude"], &latitude) != nil || decodeValue(job.Parameters["longitude"], &longitude) != nil || math.IsNaN(latitude) || math.IsNaN(longitude) || math.Abs(latitude) > 90 || math.Abs(longitude) > 180 {
			return nil, errors.New("operation_invalid_parameters")
		}
		empty := ""
		description, e := text("description", 255, true, &empty)
		if e != nil {
			return nil, e
		}
		return send(&waE2E.Message{LocationMessage: &waE2E.LocationMessage{DegreesLatitude: &latitude, DegreesLongitude: &longitude, Name: &description}}, "")
	case "send_poll":
		question, e := text("question", 255, false, nil)
		if e != nil {
			return nil, e
		}
		var options []string
		var multiple bool
		if decodeValue(job.Parameters["options"], &options) != nil || len(options) < 2 || len(options) > 10 || decodeValue(job.Parameters["multiple"], &multiple) != nil {
			return nil, errors.New("operation_invalid_parameters")
		}
		seen := map[string]bool{}
		for _, s := range options {
			if !safeText(s, 100, false) || seen[s] {
				return nil, errors.New("operation_invalid_parameters")
			}
			seen[s] = true
		}
		count := 1
		if multiple {
			count = len(options)
		}
		return send((&whatsmeow.Client{}).BuildPollCreation(question, options, count), "")
	case "send_contact":
		ids, e := users("contact_ids", 20)
		if e != nil {
			return nil, e
		}
		contacts := []*waE2E.ContactMessage{}
		for _, j := range ids {
			c, e := d.contact(ctx, j)
			if e != nil {
				return nil, e
			}
			pn, e := d.phone(ctx, j)
			if e != nil || pn.IsEmpty() || pn.Server != types.DefaultUserServer {
				return nil, errors.New("operation_target_unavailable")
			}
			name := c.FullName
			if name == "" {
				name = c.PushName
			}
			if name == "" {
				name = pn.User
			}
			name = truncate(name, 512)
			name = strings.NewReplacer("\\", "\\\\", ";", "\\;", ",", "\\,", "\r", "", "\n", "\\n").Replace(name)
			card := fmt.Sprintf("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:%s\r\nTEL;type=CELL;waid=%s:+%s\r\nEND:VCARD", name, pn.User, pn.User)
			contacts = append(contacts, &waE2E.ContactMessage{DisplayName: proto.String(truncate(name, 512)), Vcard: &card})
		}
		return send(&waE2E.Message{ContactsArrayMessage: &waE2E.ContactsArrayMessage{Contacts: contacts}}, "")
	case "send_file":
		mode, e := text("mode", 20, false, nil)
		if e != nil {
			return nil, e
		}
		empty := ""
		caption, e := text("caption", 1024, true, &empty)
		if e != nil {
			return nil, e
		}
		if (mode == "audio" || mode == "voice" || mode == "sticker") && caption != "" {
			return nil, errors.New("operation_unsupported")
		}
		bytes, mime, name, e := registeredFile(job, mode)
		if e != nil {
			return nil, e
		}
		var uploaded whatsmeow.UploadResponse
		kind := whatsmeow.MediaDocument
		switch mode {
		case "photo", "sticker":
			kind = whatsmeow.MediaImage
		case "audio", "voice":
			kind = whatsmeow.MediaAudio
		case "video":
			kind = whatsmeow.MediaVideo
		}
		// The upload changes nothing a chat can see, so it is not a change
		// phase: a deadline during it answers operation_target_unavailable.
		if err = check(""); err != nil {
			return nil, err
		}
		if uploaded, err = d.upload(ctx, bytes, kind); err != nil {
			return nil, err
		}
		if uploaded.FileLength != uint64(len(bytes)) || digestBytes(bytes) != fmt.Sprintf("%x", uploaded.FileSHA256) {
			return nil, errors.New("registered_file_invalid")
		}
		return send(uploadMessage(mode, caption, mime, name, uploaded), "")
	case "forward_message":
		if _, e := ParseChatID(job.SourceProviderChat); e != nil {
			return nil, errors.New("operation_invalid_parameters")
		}
		if err = check(job.SourceProviderChat); err != nil {
			return nil, err
		}
		m, e := exactCaptured(d.sendDependencies, job.SourceProviderChat, job.SourceProviderMessage)
		if e != nil {
			return nil, e
		}
		forward, e := forwardPayload(m)
		if e != nil {
			return nil, e
		}
		return send(forward, job.SourceProviderChat)
	case "edit_message", "delete_message", "send_reaction", "pin_message", "unpin_message", "star_message", "unstar_message", "mark_read":
		m, e := getMessage()
		if e != nil {
			return nil, e
		}
		key := capturedMessageKey(m)
		var wire *waE2E.Message
		switch op {
		case "edit_message":
			value, e := text("text", 4000, false, nil)
			if e != nil {
				return nil, e
			}
			if !m.Info.IsFromMe {
				return nil, errors.New("operation_target_invalid")
			}
			wire = (&whatsmeow.Client{}).BuildEdit(target, m.Info.ID, &waE2E.Message{Conversation: &value})
		case "delete_message":
			if !m.Info.IsFromMe {
				return nil, errors.New("operation_target_invalid")
			}
			if raw, exists := job.Parameters["revoke"]; exists {
				var revoke bool
				if decodeValue(raw, &revoke) != nil || !revoke {
					return nil, errors.New("operation_invalid_parameters")
				}
			}
			wire = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_REVOKE.Enum(), Key: key}}
		case "send_reaction":
			emoji, e := text("emoji", 64, true, nil)
			if e != nil {
				return nil, e
			}
			wire = &waE2E.Message{ReactionMessage: &waE2E.ReactionMessage{Key: key, Text: &emoji, SenderTimestampMS: proto.Int64(time.Now().UnixMilli())}}
		case "pin_message", "unpin_message":
			duration, e := integer("seconds", 1, 2147483647, 86400)
			if e != nil {
				return nil, e
			}
			kind := waE2E.PinInChatMessage_PIN_FOR_ALL
			if op == "unpin_message" {
				kind = waE2E.PinInChatMessage_UNPIN_FOR_ALL
			}
			wire = &waE2E.Message{PinInChatMessage: &waE2E.PinInChatMessage{Key: key, Type: &kind, SenderTimestampMS: proto.Int64(time.Now().UnixMilli())}, MessageContextInfo: &waE2E.MessageContextInfo{MessageAddOnDurationInSecs: proto.Uint32(uint32(duration))}}
		case "star_message", "unstar_message":
			err = mutate("", func() error {
				return d.patch(ctx, appstate.BuildStar(target, m.Info.Sender, m.Info.ID, m.Info.IsFromMe, op == "star_message"))
			})
			if err != nil {
				return nil, err
			}
			return submitted(nil)
		case "mark_read":
			if m.Info.Timestamp.IsZero() {
				return nil, errors.New("operation_target_invalid")
			}
			observed, e := d.page(job.ProviderChat, 100, nil)
			if e != nil {
				return nil, e
			}
			// The request explicitly authorizes whole-chat read state. Receipts are
			// restricted to observed incoming content at/before the reviewed target;
			// equal-second messages with unknown relative order are not inferred read.
			type receiptBatch struct {
				sender types.JID
				ids    []types.MessageID
				stamp  time.Time
			}
			batches := map[string]*receiptBatch{}
			for _, incoming := range observed {
				if incoming == nil || incoming.Message == nil || incoming.Info.IsFromMe || incoming.Info.Chat.ToNonAD() != target.ToNonAD() ||
					incoming.Info.Timestamp.After(m.Info.Timestamp) ||
					(incoming.Info.Timestamp.Equal(m.Info.Timestamp) && incoming.Info.ID != m.Info.ID) || !ordinaryContent(incoming.Message) {
					continue
				}
				sender := incoming.Info.Sender.ToNonAD()
				if sender.IsEmpty() || (sender.Server != types.DefaultUserServer && sender.Server != types.HiddenUserServer) {
					return nil, errors.New("operation_target_invalid")
				}
				key := sender.String()
				batch := batches[key]
				if batch == nil {
					batch = &receiptBatch{sender: sender}
					batches[key] = batch
				}
				batch.ids = append(batch.ids, incoming.Info.ID)
				if incoming.Info.Timestamp.After(batch.stamp) {
					batch.stamp = incoming.Info.Timestamp
				}
			}
			keys := make([]string, 0, len(batches))
			for key := range batches {
				keys = append(keys, key)
			}
			sort.Strings(keys)
			count := 0
			for _, key := range keys {
				batch := batches[key]
				if err = mutate("", func() error { return d.markRead(ctx, batch.ids, batch.stamp, target, batch.sender) }); err != nil {
					return nil, err
				}
				count += len(batch.ids)
			}
			err = mutate("", func() error { return d.patch(ctx, appstate.BuildMarkChatAsRead(target, true, m.Info.Timestamp, key)) })
			if err != nil {
				return nil, err
			}
			return submitted(map[string]any{"scope": "whole_chat", "receipt_scope": "observed_incoming_through_target", "receipt_count": count, "receipt_coverage": "partial"})
		}
		if err = mutate("", func() error { _, e := sendAndCapture(ctx, d.sendDependencies, target, wire); return e }); err != nil {
			return nil, err
		}
		return submitted(nil)
	case "mark_unread", "archive_chat", "unarchive_chat", "mute_chat", "unmute_chat", "pin_chat", "unpin_chat":
		var patch appstate.PatchInfo
		switch op {
		case "mark_unread":
			patch = appstate.BuildMarkChatAsRead(target, false, time.Time{}, nil)
		case "archive_chat", "unarchive_chat":
			patch = appstate.BuildArchive(target, op == "archive_chat", time.Time{}, nil)
		case "mute_chat", "unmute_chat":
			seconds, e := integer("seconds", 1, 2147483647, 86400)
			if e != nil {
				return nil, e
			}
			patch = appstate.BuildMute(target, op == "mute_chat", time.Duration(seconds)*time.Second)
		case "pin_chat", "unpin_chat":
			patch = appstate.BuildPin(target, op == "pin_chat")
		}
		if err = mutate("", func() error { return d.patch(ctx, patch) }); err != nil {
			return nil, err
		}
		return submitted(nil)
	}
	return nil, errors.New("operation_unsupported")
}
