package main

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"go.mau.fi/whatsmeow"
	waCommon "go.mau.fi/whatsmeow/proto/waCommon"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

// requireArchivable mirrors the bounds of services/messaging envelope.validate
// and adapters.whatsapp_message for the fields a native capture supplies.
func requireArchivable(t *testing.T, v map[string]any) {
	t.Helper()
	// Python sees the JSON round trip, not Go's in-memory strings.
	data, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err = json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	for field, limit := range map[string]int{"chatId": 160, "messageId": 256, "senderId": 160, "body": 65536, "senderName": 512, "replyToId": 256, "filename": 255, "mimetype": 127} {
		value, ok := decoded[field].(string)
		if !ok {
			continue
		}
		if !utf8.ValidString(value) || strings.ContainsRune(value, 0) || utf8.RuneCountInString(value) > limit {
			t.Fatalf("%s violates archive bounds: %q", field, value)
		}
		if field == "replyToId" && value != v[field] {
			t.Fatalf("%s changed in JSON transit", field)
		}
	}
	if mime, ok := decoded["mimetype"].(string); ok && !envelopeMimePattern.MatchString(mime) {
		t.Fatalf("invalid MIME %q", mime)
	}
	if size, ok := decoded["filesize"].(float64); ok && (size < 0 || size > 8<<30) {
		t.Fatalf("invalid size %v", size)
	}
	if body, ok := decoded["body"].(string); ok && body != v["body"] {
		t.Fatal("body changed in JSON transit; wire digest would not match")
	}
}

func documentMessage(id string) *events.Message {
	m := fixtureMessage(id, 1700000000)
	m.Message = &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{FileName: proto.String("doc.pdf"), Mimetype: proto.String("application/pdf"), FileLength: proto.Uint64(8)}}
	return m
}

func TestNativeEnvelopeSanitizesSenderControlledDisplayFields(t *testing.T) {
	m := documentMessage("display")
	m.Info.PushName = strings.Repeat("\x00名", 600)
	doc := m.Message.DocumentMessage
	doc.FileName = proto.String(strings.Repeat("я", 300) + "\n\x00/" + ".pdf")
	doc.Caption = proto.String("caption\x00 with NUL")
	v, err := NormalizeMessage(m, types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	requireArchivable(t, v)
	if name := v["senderName"].(string); utf8.RuneCountInString(name) != 512 || name != strings.Repeat("名", 512) {
		t.Fatal("sender name not truncated by code point", utf8.RuneCountInString(name))
	}
	if name := v["filename"].(string); utf8.RuneCountInString(name) != 255 || !strings.HasSuffix(name, ".pdf") || strings.ContainsAny(name, "\n\x00/") {
		t.Fatal("filename not bounded with its extension", name)
	}
	if v["body"] != "caption with NUL" {
		t.Fatal("NUL not removed", v["body"])
	}
	if f := v["formatting"].(map[string]any); f["state"] != "unknown" || f["wire_sha256"] != nil {
		t.Fatal("changed body still claims exact native wire", f)
	}
}

func TestNativeEnvelopeNormalizesMimeAndOutOfRangeSize(t *testing.T) {
	for raw, want := range map[string]string{
		"Image/JPEG; q=1":                "image/jpeg",
		"audio/ogg; codecs=opus\r\nX: y": "audio/ogg",
		"text/plain\x00":                 "application/octet-stream",
		"text/plain\r\nInjected: header": "application/octet-stream",
		"":                               "application/octet-stream",
		"not a mime":                     "application/octet-stream",
		"изображение/jpeg":               "application/octet-stream",
		"a/" + strings.Repeat("b", 126):  "application/octet-stream",
		"vnd.a+b/x-c.d_e-f":              "vnd.a+b/x-c.d_e-f",
	} {
		m := documentMessage("mime")
		m.Message.DocumentMessage.Mimetype = proto.String(raw)
		v, err := NormalizeMessage(m, types.EmptyJID)
		if err != nil || v["mimetype"] != want {
			t.Fatalf("%q -> %v %v", raw, v["mimetype"], err)
		}
		requireArchivable(t, v)
	}
	for size, want := range map[uint64]any{8 << 30: int64(8 << 30), 8<<30 + 1: nil, ^uint64(0): nil, 0: int64(0)} {
		m := documentMessage("size")
		m.Message.DocumentMessage.FileLength = proto.Uint64(size)
		v, err := NormalizeMessage(m, types.EmptyJID)
		if err != nil || v["filesize"] != want || v["hasMedia"] != true {
			t.Fatalf("%d -> %v %v", size, v["filesize"], err)
		}
		requireArchivable(t, v)
	}
}

func TestNativeMediaWithUnknownArchiveSizeIsNeverFetched(t *testing.T) {
	req, cfg, m := mediaFixture(t, []byte("synthetic"))
	m.Message.DocumentMessage.FileLength = proto.Uint64(9 << 30)
	called := false
	err := downloadMedia(context.Background(), req, cfg, mediaPolicy, mediaGet(m), func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error { called = true; return nil }, func([]byte) error { return nil })
	requireError(t, err, "media_size_limit")
	if called {
		t.Fatal("unrepresentable original downloaded")
	}
}

func TestNativeEnvelopeOmitsUnarchivableReferencesAndEditTimes(t *testing.T) {
	owner := types.NewJID("123456", types.DefaultUserServer)
	for _, stanza := range []string{strings.Repeat("A", 257), "id\x00", "bad\xff"} {
		m := fixtureMessage("reply", 1700000000)
		m.Message = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String("hello"), ContextInfo: &waE2E.ContextInfo{StanzaID: proto.String(stanza), Participant: proto.String(owner.String())}}}
		v, err := NormalizeMessage(m, owner)
		if err != nil || v["replyToId"] != nil || v["replyToOwner"] != nil {
			t.Fatalf("%q -> %v %v %v", stanza, v["replyToId"], v["replyToOwner"], err)
		}
		requireArchivable(t, v)
	}
	m := fixtureMessage("reply", 1700000000)
	m.Message = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String("hello"), ContextInfo: &waE2E.ContextInfo{StanzaID: proto.String(strings.Repeat("A", 256))}}}
	if v, err := NormalizeMessage(m, owner); err != nil || v["replyToId"] != strings.Repeat("A", 256) {
		t.Fatal("valid reference dropped", err)
	}
	for stamp, want := range map[int64]any{1700000000000: int64(1700000000000), 1699999999999: nil, maxEnvelopeUnix*1000 + 1: nil} {
		edit := fixtureMessage("editid", 1700000000)
		edit.Message = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(), Key: &waCommon.MessageKey{ID: proto.String("original")}, EditedMessage: &waE2E.Message{Conversation: proto.String("edited")}, TimestampMS: proto.Int64(stamp)}}
		v, err := captureEnvelope("test", edit, owner, "live")
		if err != nil || v["event"].(map[string]any)["editedTimestampMs"] != want {
			t.Fatalf("%d -> %v %v", stamp, v, err)
		}
		edit.IsEdit = true
		edit.Message = &waE2E.Message{Conversation: proto.String("edited")}
		edit.Info.MsgBotInfo.EditSenderTimestampMS = time.UnixMilli(stamp)
		if v, err := NormalizeMessage(edit, owner); err != nil || v["editedTimestampMs"] != want {
			t.Fatalf("stored edit %d -> %v %v", stamp, v["editedTimestampMs"], err)
		}
	}
}

func TestNativeEnvelopeRejectsUnrepresentableIdentityAndTime(t *testing.T) {
	m := fixtureMessage("future", maxEnvelopeUnix+1)
	if _, err := NormalizeMessage(m, types.EmptyJID); err == nil || err.Error() != "invalid_provider_message" {
		t.Fatal("timestamp beyond archive range accepted", err)
	}
	m = fixtureMessage(strings.Repeat("A", 120), 1700000000)
	m.Info.Chat = types.NewJID(strings.Repeat("1", 60)+"-1", types.GroupServer)
	m.Info.IsGroup = true
	m.Info.Sender = types.NewJID(strings.Repeat("2", 80), types.DefaultUserServer)
	if _, err := NormalizeMessage(m, types.EmptyJID); err == nil || err.Error() != "invalid_provider_message" {
		t.Fatal("provider ID beyond archive bound accepted", len(LegacyMessageID(m)), err)
	}
	m = fixtureMessage("nul\x00id", 1700000000)
	if _, err := NormalizeMessage(m, types.EmptyJID); err == nil || err.Error() != "invalid_provider_message" {
		t.Fatal("NUL provider ID accepted", err)
	}
}

func TestNativeIdentityPairsOnlyForArchivablePersonalIDs(t *testing.T) {
	at := time.Now().UTC().Format(time.RFC3339Nano)
	pn, lid := types.NewJID("123456789", types.DefaultUserServer), types.NewJID("700", types.HiddenUserServer)
	if observedPair(lid, pn, at) == nil {
		t.Fatal("valid PN/LID evidence dropped")
	}
	for _, bad := range []types.JID{types.NewJID("abc", types.DefaultUserServer), types.NewJID(strings.Repeat("1", 21), types.DefaultUserServer)} {
		if observedPair(lid, bad, at) != nil {
			t.Fatal("unarchivable PN evidence emitted", bad)
		}
	}
}

func ptvMessage(id string) *events.Message {
	m := fixtureMessage(id, 1700000000)
	m.Message = &waE2E.Message{PtvMessage: &waE2E.VideoMessage{Caption: proto.String("round"), Mimetype: proto.String("video/mp4"), FileLength: proto.Uint64(4096), Seconds: proto.Uint32(17), ContextInfo: &waE2E.ContextInfo{StanzaID: proto.String("quoted1"), IsForwarded: proto.Bool(true)}}}
	m.RawMessage = m.Message
	return m
}

func TestNativeRoundVideoIsCapturedAsVideoAttachment(t *testing.T) {
	v, err := NormalizeMessage(ptvMessage("round"), types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	requireArchivable(t, v)
	for field, want := range map[string]any{"messageType": "ptv", "mediaKind": "video", "hasMedia": true, "mimetype": "video/mp4", "filesize": int64(4096), "durationSeconds": uint32(17), "body": "round", "replyToId": "quoted1", "forwarded": true, "filename": "false_111111@c.us_round.bin"} {
		if v[field] != want {
			t.Fatalf("%s = %#v, want %#v", field, v[field], want)
		}
	}
}

func TestNativeViewOnceRoundVideoIsNotRetained(t *testing.T) {
	wrap := map[string]func(*waE2E.Message) *waE2E.Message{
		"v1": func(m *waE2E.Message) *waE2E.Message {
			return &waE2E.Message{ViewOnceMessage: &waE2E.FutureProofMessage{Message: m}}
		},
		"v2": func(m *waE2E.Message) *waE2E.Message {
			return &waE2E.Message{ViewOnceMessageV2: &waE2E.FutureProofMessage{Message: m}}
		},
		"extension": func(m *waE2E.Message) *waE2E.Message {
			return &waE2E.Message{ViewOnceMessageV2Extension: &waE2E.FutureProofMessage{Message: m}}
		},
		"flag": func(m *waE2E.Message) *waE2E.Message { return m },
	}
	for name, w := range wrap {
		t.Run(name, func(t *testing.T) {
			m := ptvMessage("once")
			m.RawMessage = w(m.Message)
			m.Message = m.RawMessage
			m.IsViewOnce = name == "flag"
			v, err := NormalizeMessage(m, types.EmptyJID)
			if err != nil {
				t.Fatal(err)
			}
			if v["hasMedia"] != false || v["mediaKind"] != nil || v["filename"] != nil || v["mimetype"] != nil || v["filesize"] != nil {
				t.Fatalf("view-once round video retained: %v", v)
			}
			if _, err = nativeMediaMetadata(unwrap(m)); err == nil || err.Error() != "unsupported_media_type" {
				t.Fatalf("view-once round video has a descriptor: %v", err)
			}
		})
	}
}

// The native cache keeps only the wire message, so the view-once restriction of
// a flag-only event or a wrapped edit must be persisted on its own.
func TestNativeViewOnceRoundVideoStaysUnretainedAfterCacheReload(t *testing.T) {
	for _, name := range []string{"flag", "wrapped_edit", "wrapper"} {
		t.Run(name, func(t *testing.T) {
			s := openTestStore(t)
			m := ptvMessage("stored-once")
			switch name {
			case "flag":
				m.IsViewOnce = true
			case "wrapped_edit":
				edit := &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(), Key: &waCommon.MessageKey{ID: proto.String("stored-once"), FromMe: proto.Bool(false)}, EditedMessage: m.Message, TimestampMS: proto.Int64(1700000100123)}}
				m.RawMessage = &waE2E.Message{ViewOnceMessageV2: &waE2E.FutureProofMessage{Message: edit}}
				m.Message = m.RawMessage
			case "wrapper":
				m.RawMessage = &waE2E.Message{ViewOnceMessageV2: &waE2E.FutureProofMessage{Message: m.Message}}
				m.Message = m.RawMessage
			}
			if err := s.Put(m, "live"); err != nil {
				t.Fatal(err)
			}
			loaded, err := s.Get(LegacyChatID(m.Info.Chat), "false_111111@c.us_stored-once")
			if err != nil {
				t.Fatal(err)
			}
			v, err := NormalizeMessage(loaded, types.EmptyJID)
			if err != nil {
				t.Fatal(err)
			}
			if _, err = nativeMediaMetadata(loaded); v["hasMedia"] != false || err == nil || err.Error() != "unsupported_media_type" {
				t.Fatalf("view-once round video retained after reload: hasMedia=%v descriptor=%v", v["hasMedia"], err)
			}
		})
	}
}

// Records written before the view-once flag was persisted lost the restriction
// of flag-only and wrapped-edit events, so a cached round video in that format
// keeps the attachment-free projection it was originally captured with.
func TestNativeRoundVideoInPreUpgradeCacheRecordStaysAttachmentFree(t *testing.T) {
	image := fixtureMessage("legacy-image", 1700000000)
	image.Message = &waE2E.Message{ImageMessage: &waE2E.ImageMessage{Mimetype: proto.String("image/jpeg"), FileLength: proto.Uint64(64)}}
	for name, c := range map[string]struct {
		m      *events.Message
		legacy bool
		media  bool
	}{
		"legacy_round_video":  {ptvMessage("legacy-round"), true, false},
		"legacy_image":        {image, true, true},
		"current_round_video": {ptvMessage("current-round"), false, true},
	} {
		t.Run(name, func(t *testing.T) {
			s := openTestStore(t)
			c.m.RawMessage = c.m.Message
			raw, err := marshalMessage(c.m)
			if err != nil {
				t.Fatal(err)
			}
			if c.legacy {
				wire, _ := proto.Marshal(c.m.RawMessage)
				raw, _ = json.Marshal(struct {
					Info   types.MessageInfo
					Wire   []byte
					Web    []byte
					IsEdit bool
				}{Info: c.m.Info, Wire: wire})
			}
			envelope, err := captureEnvelope("test", c.m, types.EmptyJID, "live")
			if err != nil {
				t.Fatal(err)
			}
			if err = s.append(LegacyChatID(c.m.Info.Chat), LegacyMessageID(c.m), c.m.Info.Timestamp.Unix(), raw, envelope, false); err != nil {
				t.Fatal(err)
			}
			loaded, err := s.Get(LegacyChatID(c.m.Info.Chat), LegacyMessageID(c.m))
			if err != nil {
				t.Fatal(err)
			}
			v, err := NormalizeMessage(loaded, types.EmptyJID)
			if err != nil {
				t.Fatal(err)
			}
			_, descriptorErr := nativeMediaMetadata(loaded)
			if v["hasMedia"] != c.media || (descriptorErr == nil) != c.media {
				t.Fatalf("hasMedia=%v descriptor=%v, want media %v", v["hasMedia"], descriptorErr, c.media)
			}
			if c.legacy && !c.media && (v["messageType"] != "unknown" || v["durationSeconds"] != nil) {
				t.Fatalf("legacy round video projection changed: %v", v)
			}
		})
	}
}

func forwardedText(id string, ctx *waE2E.ContextInfo) *events.Message {
	m := fixtureMessage(id, 1700000000)
	ctx.IsForwarded = proto.Bool(true)
	m.Message = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String("forwarded words"), ContextInfo: ctx}}
	m.RawMessage = m.Message
	return m
}

func TestNativeForwardOriginStatesNewsletterOrUnknown(t *testing.T) {
	t.Setenv("ANYMSG_CAPTURE_FORWARD_ORIGIN", "1")
	newsletter := forwardedText("news", &waE2E.ContextInfo{ForwardingScore: proto.Uint32(3), ForwardOrigin: waE2E.ContextInfo_CHANNELS.Enum(),
		ForwardedNewsletterMessageInfo: &waE2E.ContextInfo_ForwardedNewsletterMessageInfo{NewsletterJID: proto.String("120363000000000001@newsletter"),
			NewsletterName: proto.String("Synthetic Channel"), ServerMessageID: proto.Int32(77)}})
	v, err := NormalizeMessage(newsletter, types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	requireArchivable(t, v)
	want := map[string]any{"kind": "newsletter", "basis": "whatsmeow_context_info", "chat_id": "120363000000000001@newsletter",
		"sender_name": "Synthetic Channel", "provider_message_id": "77", "forwarding_score": uint32(3), "provider_origin_type": "CHANNELS"}
	origin, _ := v["forwardOrigin"].(map[string]any)
	if v["forwarded"] != true || len(origin) != len(want) {
		t.Fatalf("forwardOrigin = %#v", v["forwardOrigin"])
	}
	for field, value := range want {
		if origin[field] != value {
			t.Fatalf("%s = %#v, want %#v", field, origin[field], value)
		}
	}
	// An ordinary chat forward does not carry its author: kind unknown, with the score.
	plain, err := NormalizeMessage(forwardedText("plain", &waE2E.ContextInfo{ForwardingScore: proto.Uint32(5)}), types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	origin, _ = plain["forwardOrigin"].(map[string]any)
	if len(origin) != 3 || origin["kind"] != "unknown" || origin["forwarding_score"] != uint32(5) || origin["basis"] != "whatsmeow_context_info" {
		t.Fatalf("plain forwardOrigin = %#v", plain["forwardOrigin"])
	}
}

func TestNativeForwardOriginIsOmittedUnlessEnabledAndForwarded(t *testing.T) {
	t.Setenv("ANYMSG_CAPTURE_FORWARD_ORIGIN", "")
	v, err := NormalizeMessage(forwardedText("off", &waE2E.ContextInfo{ForwardingScore: proto.Uint32(5)}), types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := v["forwardOrigin"]; ok || v["forwarded"] != true {
		t.Fatalf("disabled capture emitted forwardOrigin: %#v", v)
	}
	t.Setenv("ANYMSG_CAPTURE_FORWARD_ORIGIN", "1")
	m := forwardedText("not-forwarded", &waE2E.ContextInfo{ForwardingScore: proto.Uint32(5)})
	m.Message.ExtendedTextMessage.ContextInfo.IsForwarded = proto.Bool(false)
	if v, err = NormalizeMessage(m, types.EmptyJID); err != nil {
		t.Fatal(err)
	}
	if _, ok := v["forwardOrigin"]; ok {
		t.Fatalf("non-forwarded message has forwardOrigin: %#v", v)
	}
}

func TestNativeBusinessSenderFlagIsGatedAndStatedOnly(t *testing.T) {
	business := func(id string, fromMe bool, name *types.VerifiedName) *events.Message {
		m := forwardedText(id, &waE2E.ContextInfo{})
		m.Info.IsFromMe = fromMe
		m.Info.VerifiedName = name
		return m
	}
	// Off by default: a rollback server that rejects the field never meets it.
	t.Setenv("ANYMSG_CAPTURE_PROVIDER_FLAGS", "")
	v, err := NormalizeMessage(business("off", false, &types.VerifiedName{VerifiedLevel: "high"}), types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := v["senderIsBusiness"]; ok {
		t.Fatalf("disabled capture emitted senderIsBusiness: %#v", v)
	}
	t.Setenv("ANYMSG_CAPTURE_PROVIDER_FLAGS", "1")
	if v, err = NormalizeMessage(business("on", false, &types.VerifiedName{VerifiedLevel: "high"}), types.EmptyJID); err != nil {
		t.Fatal(err)
	}
	requireArchivable(t, v)
	if v["senderIsBusiness"] != true {
		t.Fatalf("VerifiedName not captured: %#v", v)
	}
	// No VerifiedName is unknown, not "not a business"; the owner's own message states nothing.
	for name, m := range map[string]*events.Message{"plain": business("plain", false, nil), "mine": business("mine", true, &types.VerifiedName{})} {
		if v, err = NormalizeMessage(m, types.EmptyJID); err != nil {
			t.Fatal(err)
		}
		if _, ok := v["senderIsBusiness"]; ok {
			t.Fatalf("%s: unexpected senderIsBusiness: %#v", name, v)
		}
	}
}

// spec: ANYM-389
func TestNativePNLIDObservationOnlyForOnePersonalPhoneAndOneLID(t *testing.T) {
	pn20, lid := types.NewJID(strings.Repeat("9", 20), types.DefaultUserServer), types.NewJID("700", types.HiddenUserServer)
	observations := func(sender, alt types.JID) []any {
		t.Helper()
		m := fixtureMessage("pair", 1700000000)
		m.Info.Sender, m.Info.SenderAlt = sender, alt
		v, err := NormalizeMessage(m, types.EmptyJID)
		if err != nil {
			t.Fatal(err)
		}
		return v["identityObservations"].([]any)
	}
	for _, tc := range []struct{ sender, alt types.JID }{{lid, pn20}, {pn20, lid}} {
		got := observations(tc.sender, tc.alt)
		if len(got) != 1 {
			t.Fatal("valid PN/LID pair not observed", tc.sender, tc.alt, got)
		}
		pair := got[0].(map[string]any)
		if pair["pn"] != pn20.User+"@c.us" || pair["lid"] != "700@lid" || pair["identity_id"] != LegacyChatID(tc.sender) || pair["provenance"] != "whatsmeow_message_alt_v1" {
			t.Fatal("wrong PN/LID observation", pair)
		}
	}
	for name, tc := range map[string]struct{ sender, alt types.JID }{
		"non_numeric_phone": {lid, types.NewJID("abc", types.DefaultUserServer)},
		"21_digit_phone":    {lid, types.NewJID(strings.Repeat("1", 21), types.DefaultUserServer)},
		"21_digit_lid":      {types.NewJID(strings.Repeat("7", 21), types.HiddenUserServer), pn20},
		"two_phones":        {types.NewJID("111111", types.DefaultUserServer), pn20},
		"two_lids":          {lid, types.NewJID("800", types.HiddenUserServer)},
	} {
		if got := observations(tc.sender, tc.alt); len(got) != 0 {
			t.Fatal("unarchivable PN/LID pair observed", name, got)
		}
	}
}
