package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// Archive envelope bounds enforced by services/messaging envelope.validate and
// adapters.whatsapp_message. Python counts code points; for valid UTF-8 that is
// the Go rune count. Provider content is end-to-end encrypted and sender
// controlled, so display fields are sanitized here instead of being rejected.
const (
	maxEnvelopeBody       = 65536
	maxEnvelopeSenderName = 512
	maxEnvelopeFilename   = 255
	maxEnvelopeMime       = 127
	maxEnvelopeProviderID = 256
	maxEnvelopeSender     = 160
	maxForwardName        = 256
	maxEnvelopeMediaBytes = 8 << 30
	maxEnvelopeUnix       = 253402300799 // 9999-12-31T23:59:59Z, Python datetime bound
)

var envelopeMimePattern = regexp.MustCompile(`^[a-z0-9_.+-]+/[a-z0-9_.+-]+$`)

// envelopeText returns valid UTF-8 without NUL and at most limit code points.
// Each invalid byte becomes U+FFFD exactly as encoding/json would emit it, so
// a value echoed back by Python (e.g. a media descriptor) compares equal.
func envelopeText(s string, limit int) string {
	if utf8.ValidString(s) && !strings.ContainsRune(s, 0) && utf8.RuneCountInString(s) <= limit {
		return s
	}
	var b strings.Builder
	for i, n := 0, 0; i < len(s) && n < limit; {
		r, size := utf8.DecodeRuneInString(s[i:])
		i += size
		if r == 0 {
			continue
		}
		b.WriteRune(r)
		n++
	}
	return b.String()
}

// envelopeIdentifier reports whether a provider reference can be archived
// unchanged. References are never truncated: a shortened ID names another message.
func envelopeIdentifier(s string, limit int) bool {
	return s != "" && utf8.ValidString(s) && !strings.ContainsRune(s, 0) && utf8.RuneCountInString(s) <= limit
}

// forwardOriginCapture stays off until the release a rollback would return to
// accepts forwardOrigin (ANYM-274, ANYMSG_CAPTURE_FORWARD_ORIGIN).
func forwardOriginCapture() bool {
	return os.Getenv("ANYMSG_CAPTURE_FORWARD_ORIGIN") == "1"
}

// providerFlagsCapture stays off until the release a rollback would return to
// accepts senderIsBusiness (ANYM-276, ANYMSG_CAPTURE_PROVIDER_FLAGS).
func providerFlagsCapture() bool {
	return os.Getenv("ANYMSG_CAPTURE_PROVIDER_FLAGS") == "1"
}

// nativeForwardOrigin reports only what ContextInfo states. WhatsApp does not
// carry the author of an ordinary chat forward, so that origin stays unknown.
func nativeForwardOrigin(ctx *waE2E.ContextInfo) map[string]any {
	origin := map[string]any{"kind": "unknown", "basis": "whatsmeow_context_info"}
	if ctx.ForwardingScore != nil {
		origin["forwarding_score"] = ctx.GetForwardingScore()
	}
	if ctx.ForwardOrigin != nil {
		// A value outside the archive's closed enum is omitted, never renamed.
		switch v := ctx.GetForwardOrigin(); v {
		case waE2E.ContextInfo_UNKNOWN, waE2E.ContextInfo_CHAT, waE2E.ContextInfo_STATUS, waE2E.ContextInfo_CHANNELS, waE2E.ContextInfo_META_AI, waE2E.ContextInfo_UGC:
			origin["provider_origin_type"] = v.String()
		}
	}
	if n := ctx.GetForwardedNewsletterMessageInfo(); n != nil && envelopeIdentifier(n.GetNewsletterJID(), maxEnvelopeSender) {
		origin["kind"] = "newsletter"
		origin["chat_id"] = n.GetNewsletterJID()
		if name := envelopeText(n.GetNewsletterName(), maxForwardName); name != "" {
			origin["sender_name"] = name
		}
		if n.ServerMessageID != nil {
			origin["provider_message_id"] = strconv.Itoa(int(n.GetServerMessageID()))
		}
	}
	return origin
}

// envelopeMime keeps only the media type and falls back to the generic type when
// the archive pattern would reject it.
func envelopeMime(raw string) string {
	mime := strings.ToLower(strings.TrimSpace(strings.SplitN(raw, ";", 2)[0]))
	if len(mime) > maxEnvelopeMime || !envelopeMimePattern.MatchString(mime) {
		return "application/octet-stream"
	}
	return mime
}

// envelopeFilename shortens a display-only name, keeping a short extension.
func envelopeFilename(name string) string {
	name = envelopeText(name, len(name))
	if utf8.RuneCountInString(name) <= maxEnvelopeFilename {
		return name
	}
	ext := ""
	if i := strings.LastIndexByte(name, '.'); i > 0 && utf8.RuneCountInString(name[i:]) <= 16 {
		ext = name[i:]
	}
	return envelopeText(strings.TrimSuffix(name, ext), maxEnvelopeFilename-utf8.RuneCountInString(ext)) + ext
}

// envelopeEditStamp drops sender-controlled edit times the archive cannot
// represent: before the original send or past Python's datetime range.
func envelopeEditStamp(ms int64, sent int64) any {
	if ms <= 0 || ms < sent*1000 || ms > maxEnvelopeUnix*1000 {
		return nil
	}
	return ms
}

// Small deterministic offline bridge lets the existing Python validator check
// real native envelopes. It cannot initialize or touch a WhatsApp session.
func normalizeFixture(path string) error {
	var v struct {
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
	}
	data, err := readPrivate(path, 1<<20)
	if err != nil {
		return err
	}
	if json.Unmarshal(data, &v) != nil || !v.Synthetic {
		return errors.New("invalid_fixture")
	}
	chat, err := ParseChatID(v.Chat)
	if err != nil {
		return err
	}
	sender, err := ParseChatID(v.Sender)
	if err != nil && v.Sender != "" {
		return err
	}
	owner, err := ParseChatID(v.Owner)
	if err != nil {
		return err
	}
	lid := types.EmptyJID
	if v.OwnerLID != "" {
		lid, err = ParseChatID(v.OwnerLID)
		if err != nil {
			return err
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

// IDs deliberately retain the observed namespace. PN/LID mappings are separate
// evidence and never silently rewrite existing archive references.
func LegacyMessageID(e *events.Message) string {
	id := fmt.Sprintf("%t_%s_%s", e.Info.IsFromMe, LegacyChatID(e.Info.Chat), e.Info.ID)
	if e.Info.IsGroup && !e.Info.Sender.IsEmpty() {
		id += "_" + LegacyChatID(e.Info.Sender)
	}
	return id
}
func unwrap(e *events.Message) *events.Message {
	copy := *e
	m := e.RawMessage
	if m == nil {
		m = e.Message
	}
	if m != nil {
		copy.RawMessage = proto.Clone(m).(*waE2E.Message)
		copy.UnwrapRaw()
		unwrapAlbumChild(&copy)
	}
	return &copy
}

// An album photo or video arrives inside associatedChildMessage, which
// UnwrapRaw leaves wrapped (Baileys normalizeMessageContent unwraps it like the
// view-once wrappers). Wrappers inside the child keep their restrictions.
func unwrapAlbumChild(e *events.Message) {
	child := e.Message.GetAssociatedChildMessage().GetMessage()
	if child == nil {
		return
	}
	inner := (&events.Message{RawMessage: child}).UnwrapRaw()
	e.Message = inner.Message
	e.IsViewOnce = e.IsViewOnce || inner.IsViewOnce
	e.IsEphemeral = e.IsEphemeral || inner.IsEphemeral
	e.IsDocumentWithCaption = e.IsDocumentWithCaption || inner.IsDocumentWithCaption
}

func joinText(parts ...string) string {
	kept := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			kept = append(kept, p)
		}
	}
	return strings.Join(kept, "\n\n")
}
func templateText(v *waE2E.TemplateMessage) string {
	if i := v.GetInteractiveMessageTemplate(); i != nil {
		return interactiveText(i)
	}
	h := v.GetHydratedFourRowTemplate()
	if h == nil {
		h = v.GetHydratedTemplate()
	}
	return joinText(h.GetHydratedTitleText(), h.GetHydratedContentText(), h.GetHydratedFooterText())
}
func interactiveText(v *waE2E.InteractiveMessage) string {
	return joinText(v.GetHeader().GetTitle(), v.GetHeader().GetSubtitle(), v.GetBody().GetText(), v.GetFooter().GetText())
}

// whatsmeow dispatches the sender-key distribution part of a group message as
// its own event with the message's ID and time; the content follows as another
// event. A message holding only that and/or message context info carries
// nothing to archive (mautrix-whatsapp getMessageType ignores it). Unknown wire
// fields may be content this build cannot read, so they keep the message.
func controlOnly(m *waE2E.Message) bool {
	if m.GetSenderKeyDistributionMessage() == nil && m.GetMessageContextInfo() == nil || len(m.ProtoReflect().GetUnknown()) > 0 {
		return false
	}
	only := true
	m.ProtoReflect().Range(func(f protoreflect.FieldDescriptor, _ protoreflect.Value) bool {
		switch f.Name() {
		case "senderKeyDistributionMessage", "messageContextInfo":
			return true
		}
		only = false
		return false
	})
	return only
}
func messageContext(m *waE2E.Message) *waE2E.ContextInfo {
	switch {
	case m.GetExtendedTextMessage() != nil:
		return m.GetExtendedTextMessage().GetContextInfo()
	case m.GetImageMessage() != nil:
		return m.GetImageMessage().GetContextInfo()
	case m.GetVideoMessage() != nil:
		return m.GetVideoMessage().GetContextInfo()
	case m.GetPtvMessage() != nil:
		return m.GetPtvMessage().GetContextInfo()
	case m.GetAudioMessage() != nil:
		return m.GetAudioMessage().GetContextInfo()
	case m.GetDocumentMessage() != nil:
		return m.GetDocumentMessage().GetContextInfo()
	case m.GetStickerMessage() != nil:
		return m.GetStickerMessage().GetContextInfo()
	}
	return nil
}
func observedPair(id, other types.JID, at string) any {
	id = id.ToNonAD()
	other = other.ToNonAD()
	pn, lid := id, other
	if pn.Server == types.HiddenUserServer {
		pn, lid = other, id
	}
	if pn.Server != types.DefaultUserServer || lid.Server != types.HiddenUserServer || !personalUser(pn.User) || !personalUser(lid.User) {
		return nil
	}
	return map[string]any{"identity_id": LegacyChatID(id), "pn": LegacyChatID(pn), "lid": LegacyChatID(lid), "provenance": "whatsmeow_message_alt_v1", "observed_at": at}
}

// The archive accepts PN/LID evidence only for 1-20 digit personal IDs and
// rejects the whole capture otherwise; unverifiable pairs are simply omitted.
func personalUser(user string) bool {
	if user == "" || len(user) > 20 {
		return false
	}
	for _, c := range []byte(user) {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}
func NormalizeMessage(e *events.Message, owner types.JID, ownerAliases ...types.JID) (map[string]any, error) {
	if e == nil || e.Info.ID == "" || len(e.Info.ID) > 120 || e.Info.Timestamp.Unix() <= 0 || e.Info.Timestamp.Unix() > maxEnvelopeUnix {
		return nil, errors.New("invalid_provider_message")
	}
	chat := LegacyChatID(e.Info.Chat)
	if !chatPattern.MatchString(chat) {
		return nil, errors.New("unsupported_chat")
	}
	e = unwrap(e)
	messageID, sender := LegacyMessageID(e), LegacyChatID(e.Info.Sender)
	if !envelopeIdentifier(messageID, maxEnvelopeProviderID) || (sender != "" && !envelopeIdentifier(sender, maxEnvelopeSender)) {
		return nil, errors.New("invalid_provider_message")
	}
	m := e.Message
	body, kind, media := "", "unknown", ""
	var filename, mime, size, duration, reply, mentions, replyOwner, forwarded, edited any
	var forwardOrigin, contactCard map[string]any
	if e.IsEdit && e.Info.MsgBotInfo.EditSenderTimestampMS.UnixMilli() > 0 {
		edited = envelopeEditStamp(e.Info.MsgBotInfo.EditSenderTimestampMS.UnixMilli(), e.Info.Timestamp.Unix())
	}
	service := false
	if m == nil {
		service = true
		kind = "notification"
	} else {
		body = m.GetConversation()
		if m.GetExtendedTextMessage() != nil {
			body = m.GetExtendedTextMessage().GetText()
		}
		if body != "" {
			kind = "chat"
		}
		switch {
		case m.GetImageMessage() != nil:
			v := m.GetImageMessage()
			kind = "image"
			media = "photo"
			body = v.GetCaption()
			mime = v.GetMimetype()
			size = v.GetFileLength()
		case m.GetVideoMessage() != nil:
			v := m.GetVideoMessage()
			kind = "video"
			media = "video"
			body = v.GetCaption()
			mime = v.GetMimetype()
			size = v.GetFileLength()
			duration = v.GetSeconds()
			if v.GetGifPlayback() {
				kind = "gif"
				media = "animation"
			}
		case m.GetPtvMessage() != nil:
			// A round video (PTV) is an ordinary VideoMessage in its own field.
			v := m.GetPtvMessage()
			kind = "ptv"
			media = "video"
			body = v.GetCaption()
			mime = v.GetMimetype()
			size = v.GetFileLength()
			duration = v.GetSeconds()
		case m.GetAudioMessage() != nil:
			v := m.GetAudioMessage()
			kind = "audio"
			media = "audio"
			mime = v.GetMimetype()
			size = v.GetFileLength()
			duration = v.GetSeconds()
			if v.GetPTT() {
				kind = "ptt"
				media = "voice"
			}
		case m.GetDocumentMessage() != nil:
			v := m.GetDocumentMessage()
			kind = "document"
			media = "document"
			body = v.GetCaption()
			mime = v.GetMimetype()
			size = v.GetFileLength()
			if v.GetFileName() != "" {
				filename = v.GetFileName()
			}
		case m.GetStickerMessage() != nil:
			v := m.GetStickerMessage()
			kind = "sticker"
			media = "sticker"
			mime = v.GetMimetype()
			size = v.GetFileLength()
		case m.GetContactMessage() != nil:
			v := m.GetContactMessage()
			kind = "vcard"
			body = v.GetDisplayName()
			if contactCardCapture() {
				contactCard = contactBlock([]sharedContact{{v.GetDisplayName(), v.GetVcard()}})
			}
		case m.GetContactsArrayMessage() != nil:
			kind = "multi_vcard"
			if contactCardCapture() {
				var cards []sharedContact
				for _, v := range m.GetContactsArrayMessage().GetContacts() {
					cards = append(cards, sharedContact{v.GetDisplayName(), v.GetVcard()})
				}
				contactCard = contactBlock(cards)
			}
		case m.GetLocationMessage() != nil:
			kind = "location"
			body = m.GetLocationMessage().GetName()
		case m.GetPollCreationMessage() != nil:
			kind = "poll_creation"
			body = m.GetPollCreationMessage().GetName()
		case m.GetPollCreationMessageV2() != nil:
			kind = "poll_creation"
			body = m.GetPollCreationMessageV2().GetName()
		case m.GetPollCreationMessageV3() != nil:
			kind = "poll_creation"
			body = m.GetPollCreationMessageV3().GetName()
		// Business messages keep their visible text; header media is not fetched.
		case m.GetTemplateMessage() != nil:
			kind = "template"
			body = templateText(m.GetTemplateMessage())
		case m.GetInteractiveMessage() != nil:
			kind = "interactive"
			body = interactiveText(m.GetInteractiveMessage())
		case m.GetButtonsMessage() != nil:
			v := m.GetButtonsMessage()
			kind = "buttons"
			body = joinText(v.GetText(), v.GetContentText(), v.GetFooterText())
		case m.GetListMessage() != nil:
			v := m.GetListMessage()
			kind = "list"
			body = joinText(v.GetTitle(), v.GetDescription(), v.GetFooterText())
		case m.GetProductMessage() != nil:
			v := m.GetProductMessage()
			kind = "product"
			body = joinText(v.GetProduct().GetTitle(), v.GetProduct().GetDescription(), v.GetBody(), v.GetFooter())
		case m.GetGroupInviteMessage() != nil:
			v := m.GetGroupInviteMessage()
			kind = "group_invite"
			body = joinText(v.GetGroupName(), v.GetCaption())
		case m.GetAlbumMessage() != nil:
			// The parent only announces a count; each photo or video is its own message.
			kind = "album"
			service = true
		case m.GetMessageHistoryNotice() != nil:
			kind = "history_notice"
			service = true
		case m.GetPlaceholderMessage() != nil:
			// Business content masked from linked devices; only the phone shows it.
			kind = "placeholder"
		case m.GetReactionMessage() != nil:
			kind = "reaction"
			service = true
			body = m.GetReactionMessage().GetText()
		case m.GetProtocolMessage() != nil:
			kind = "protocol"
			service = true
		}
		if ctx := messageContext(m); ctx != nil {
			forwarded = ctx.GetIsForwarded()
			if ctx.GetIsForwarded() && forwardOriginCapture() {
				forwardOrigin = nativeForwardOrigin(ctx)
			}
			// An unarchivable reference is omitted, never shortened into another ID.
			if envelopeIdentifier(ctx.GetStanzaID(), maxEnvelopeProviderID) {
				reply = ctx.GetStanzaID()
				replyOwner = nil
				if j, err := types.ParseJID(ctx.GetParticipant()); err == nil && !owner.IsEmpty() {
					replyOwner = identityObservation(j, append(ownerAliases, owner)...)
				}
			}
			if !owner.IsEmpty() {
				mentions = false
				for _, mention := range ctx.GetMentionedJID() {
					if j, err := types.ParseJID(mention); err == nil {
						hit := identityObservation(j, append(ownerAliases, owner)...)
						if hit == true {
							mentions = true
							break
						}
						if hit == nil {
							mentions = nil
						}
					} else {
						mentions = nil
					}
				}
			}
		}
	}
	if media != "" {
		// Metadata is already normalized to the archive descriptor contract.
		if meta, err := nativeMediaMetadata(e); err == nil {
			filename = meta.Filename
			mime = meta.Mime
			size = nil
			if meta.Bytes >= 0 {
				size = meta.Bytes
			}
		} else if e.IsViewOnce || err.Error() == "unsupported_media_type" {
			// View-once content (also a nested wrapper) is never retained.
			media = ""
			filename = nil
			mime = nil
			size = nil
		} else {
			return nil, errors.New("invalid_provider_media")
		}
	}
	// Python counts the body in code points and rejects NUL. Keep the archived
	// text instead of failing capture; a changed body is no longer the exact
	// native wire, so it carries no wire digest claim.
	wire := body
	body = envelopeText(body, maxEnvelopeBody)
	formatting := map[string]any{"version": 1, "state": "unknown", "basis": nil, "wire_sha256": nil, "entities": nil, "unsupported_entities": []any{}, "warnings": []string{"native_entities_not_captured"}}
	if body == wire {
		h := sha256.Sum256([]byte(body))
		formatting = map[string]any{"version": 1, "state": "wire_observed", "basis": "whatsapp_native_wire", "wire_sha256": hex.EncodeToString(h[:]), "entities": nil, "unsupported_entities": []any{}, "warnings": []string{"syntax_interpretation_unknown"}}
	}
	var self any
	if e.Info.IsGroup {
		self = false
	} else if !owner.IsEmpty() && (e.Info.Chat.Server == owner.Server || (!lidEmpty(ownerAliases) && e.Info.Chat.Server == types.HiddenUserServer)) {
		self = identityObservation(e.Info.Chat, append(ownerAliases, owner)...)
	}
	observations := []any{}
	at := time.Now().UTC().Format(time.RFC3339Nano)
	if pair := observedPair(e.Info.Sender, e.Info.SenderAlt, at); pair != nil {
		observations = append(observations, pair)
	}
	if pair := observedPair(e.Info.Chat, e.Info.RecipientAlt, at); pair != nil && LegacyChatID(e.Info.Chat) != LegacyChatID(e.Info.Sender) {
		observations = append(observations, pair)
	}
	value := map[string]any{"chatId": chat, "chatName": chat, "messageId": messageID, "messageType": kind, "timestampResolution": "second", "durationSeconds": duration, "isService": service, "isSelfChat": self, "senderId": sender, "senderName": envelopeText(e.Info.PushName, maxEnvelopeSenderName), "body": body, "timestamp": e.Info.Timestamp.Unix(), "fromMe": e.Info.IsFromMe, "hasMedia": media != "", "filename": filename, "mimetype": mime, "filesize": size, "replyToId": reply, "mentionsOwner": mentions, "replyToOwner": replyOwner, "forwarded": forwarded, "mediaKind": nilString(media), "editedTimestampMs": edited, "identityObservations": observations, "formatting": formatting}
	if forwardOrigin != nil {
		// Present only when captured, so an older reader never meets the field (rollback).
		value["forwardOrigin"] = forwardOrigin
	}
	// Present only when captured, so an older reader never meets the field (rollback).
	if contactCard != nil {
		value["contactCard"] = contactCard
	}
	// Only a stated certificate is captured; no VerifiedName is unknown, never "not a business".
	if providerFlagsCapture() && !e.Info.IsFromMe && e.Info.VerifiedName != nil {
		value["senderIsBusiness"] = true
	}
	return value, nil
}
func lidEmpty(ids []types.JID) bool {
	for _, id := range ids {
		if !id.IsEmpty() {
			return false
		}
	}
	return true
}
func identityObservation(id types.JID, owners ...types.JID) any {
	if ownedIdentity(id, owners...) {
		return true
	}
	for _, owner := range owners {
		if !owner.IsEmpty() && id.Server == owner.Server {
			return false
		}
	}
	return nil
}
func ownedIdentity(id types.JID, owners ...types.JID) bool {
	for _, owner := range owners {
		if !owner.IsEmpty() && id.ToNonAD() == owner.ToNonAD() {
			return true
		}
	}
	return false
}
func fixtureOrigin(v string) string {
	if v == "" {
		return "live"
	}
	return v
}
func nilString(v string) any {
	if v == "" {
		return nil
	}
	return v
}

func captureEnvelope(account string, e *events.Message, owner types.JID, origin string, ownerAliases ...types.JID) (map[string]any, error) {
	e = unwrap(e)
	if origin == "history" {
		origin = "backfill"
	}
	change := "message"
	var editStamp int64
	at := time.Now().UTC().Format(time.RFC3339Nano)
	if pm := e.Message.GetProtocolMessage(); pm != nil {
		if pm.GetType() == waE2E.ProtocolMessage_REVOKE {
			target := *e
			target.Info.ID = pm.GetKey().GetID()
			target.Info.IsFromMe = pm.GetKey().GetFromMe()
			if part, err := types.ParseJID(pm.GetKey().GetParticipant()); err == nil {
				target.Info.Sender = part
			}
			if target.Info.ID == "" {
				return nil, errors.New("invalid_provider_message")
			}
			return map[string]any{"kind": "whatsapp_delete", "account": account, "chat": LegacyChatID(target.Info.Chat), "ids": []string{LegacyMessageID(&target)}, "observed_at": at}, nil
		}
		if pm.GetType() == waE2E.ProtocolMessage_MESSAGE_EDIT && pm.GetEditedMessage() != nil {
			change = "edit"
			editStamp = pm.GetTimestampMS()
			copy := *e
			copy.Info.ID = pm.GetKey().GetID()
			copy.Message = pm.GetEditedMessage()
			copy.RawMessage = copy.Message
			e = &copy
		}
	}
	if e.IsEdit {
		change = "edit"
	}
	v, err := NormalizeMessage(e, owner, ownerAliases...)
	if err != nil {
		return nil, err
	}
	if change == "edit" && editStamp > 0 {
		v["editedTimestampMs"] = envelopeEditStamp(editStamp, e.Info.Timestamp.Unix())
	}
	return map[string]any{"kind": "whatsapp_event", "account": account, "event": v, "change": change, "observed_at": at, "origin": origin}, nil
}
