package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	waCommon "go.mau.fi/whatsmeow/proto/waCommon"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

func groupFixture(id string, m *waE2E.Message) *events.Message {
	chat := types.NewJID("120363000000000001", types.GroupServer)
	sender := types.NewJID("222222", types.DefaultUserServer)
	return &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: chat, Sender: sender, IsGroup: true}, ID: id, Timestamp: time.Unix(1700000000, 0)}, Message: m, RawMessage: m}
}

func senderKeyPart() *waE2E.Message {
	return &waE2E.Message{SenderKeyDistributionMessage: &waE2E.SenderKeyDistributionMessage{GroupID: proto.String("120363000000000001@g.us"), AxolotlSenderKeyDistributionMessage: []byte{1, 2, 3}}, MessageContextInfo: &waE2E.MessageContextInfo{MessageSecret: []byte{4, 5, 6}}}
}

func requireFields(t *testing.T, v map[string]any, want map[string]any) {
	t.Helper()
	requireArchivable(t, v)
	for field, value := range want {
		if v[field] != value {
			t.Fatalf("%s = %#v, want %#v", field, v[field], value)
		}
	}
}

// spec: ANYM-471
func TestNativeSenderKeyOnlyEventIsNotCapturedAndTheDocumentIs(t *testing.T) {
	s := openTestStore(t)
	if err := s.Put(groupFixture("DOC1", senderKeyPart()), "live"); err != nil {
		t.Fatal(err)
	}
	var rows int
	s.db.QueryRow("SELECT COUNT(*) FROM capture").Scan(&rows)
	if rows != 0 || spoolFiles(t, s.Config.EventsDirectory) != 0 {
		t.Fatalf("sender-key-only event captured: rows=%d spool=%d", rows, spoolFiles(t, s.Config.EventsDirectory))
	}
	doc := &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{FileName: proto.String("report.pdf"), Mimetype: proto.String("application/pdf"), FileLength: proto.Uint64(4096)}, MessageContextInfo: &waE2E.MessageContextInfo{MessageSecret: []byte{4, 5, 6}}}
	if err := s.Put(groupFixture("DOC1", doc), "live"); err != nil {
		t.Fatal(err)
	}
	s.db.QueryRow("SELECT COUNT(*) FROM capture").Scan(&rows)
	if rows != 1 || spoolFiles(t, s.Config.EventsDirectory) != 1 {
		t.Fatalf("document not captured once: rows=%d", rows)
	}
	loaded, err := s.Get("120363000000000001@g.us", "false_120363000000000001@g.us_DOC1_222222@c.us")
	if err != nil {
		t.Fatal(err)
	}
	v, err := NormalizeMessage(loaded, types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	requireFields(t, v, map[string]any{"messageType": "document", "mediaKind": "document", "hasMedia": true, "filename": "report.pdf", "body": ""})
	// A sender key travelling with real content is an ordinary message.
	mixed := senderKeyPart()
	mixed.Conversation = proto.String("hello group")
	if senderKeyOnly(mixed) {
		t.Fatal("content with a sender key was treated as sender-key-only")
	}
}

func albumChild(id string, inner *waE2E.Message) *events.Message {
	return groupFixture(id, &waE2E.Message{AssociatedChildMessage: &waE2E.FutureProofMessage{Message: inner}, MessageContextInfo: &waE2E.MessageContextInfo{MessageSecret: []byte{7}}})
}

// spec: ANYM-471
func TestNativeAlbumChildIsCapturedAsPhotoAndDownloadable(t *testing.T) {
	photo := &waE2E.Message{ImageMessage: &waE2E.ImageMessage{Caption: proto.String("site visit"), Mimetype: proto.String("image/jpeg"), FileLength: proto.Uint64(2048)}}
	m := albumChild("ALB1", photo)
	v, err := NormalizeMessage(m, types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	requireFields(t, v, map[string]any{"messageType": "image", "mediaKind": "photo", "hasMedia": true, "mimetype": "image/jpeg", "filesize": int64(2048), "body": "site visit"})
	s := openTestStore(t)
	if err = s.Put(m, "live"); err != nil {
		t.Fatal(err)
	}
	loaded, err := s.Get("120363000000000001@g.us", LegacyMessageID(m))
	if err != nil {
		t.Fatal(err)
	}
	meta, err := nativeMediaMetadata(loaded)
	if err != nil || meta.Kind != "image" || meta.Bytes != 2048 || meta.Mime != "image/jpeg" {
		t.Fatalf("album photo has no download descriptor after reload: %+v %v", meta, err)
	}
	// A view-once wrapper inside the child keeps its restriction.
	once := albumChild("ALB2", &waE2E.Message{ViewOnceMessageV2: &waE2E.FutureProofMessage{Message: photo}})
	v, err = NormalizeMessage(once, types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	if v["hasMedia"] != false || v["mediaKind"] != nil {
		t.Fatalf("view-once album child retained: %v", v)
	}
	if _, err = nativeMediaMetadata(unwrap(once)); err == nil || err.Error() != "unsupported_media_type" {
		t.Fatalf("view-once album child has a descriptor: %v", err)
	}
}

// spec: ANYM-471
func TestNativeAlbumParentAndHistoryNoticeAreServiceEvents(t *testing.T) {
	for kind, m := range map[string]*waE2E.Message{
		"album":          {AlbumMessage: &waE2E.AlbumMessage{ExpectedImageCount: proto.Uint32(3)}},
		"history_notice": {MessageHistoryNotice: &waE2E.MessageHistoryNotice{}},
	} {
		v, err := NormalizeMessage(groupFixture("SVC", m), types.EmptyJID)
		if err != nil {
			t.Fatal(err)
		}
		requireFields(t, v, map[string]any{"messageType": kind, "isService": true, "hasMedia": false, "body": ""})
	}
}

// spec: ANYM-471
func TestNativeBusinessMessagesKeepTheirVisibleText(t *testing.T) {
	interactive := &waE2E.InteractiveMessage{Header: &waE2E.InteractiveMessage_Header{Title: proto.String("Invoice 42")}, Body: &waE2E.InteractiveMessage_Body{Text: proto.String("Your invoice is ready.")}, Footer: &waE2E.InteractiveMessage_Footer{Text: proto.String("Reply STOP")}}
	hydrated := &waE2E.TemplateMessage_HydratedFourRowTemplate{Title: &waE2E.TemplateMessage_HydratedFourRowTemplate_HydratedTitleText{HydratedTitleText: "Booking"}, HydratedContentText: proto.String("Check-in at 14:00"), HydratedFooterText: proto.String("Hotel")}
	for name, c := range map[string]struct {
		m    *waE2E.Message
		body string
	}{
		"template":     {&waE2E.Message{TemplateMessage: &waE2E.TemplateMessage{Format: &waE2E.TemplateMessage_HydratedFourRowTemplate_{HydratedFourRowTemplate: hydrated}}}, "Booking\n\nCheck-in at 14:00\n\nHotel"},
		"interactive":  {&waE2E.Message{InteractiveMessage: interactive}, "Invoice 42\n\nYour invoice is ready.\n\nReply STOP"},
		"buttons":      {&waE2E.Message{ButtonsMessage: &waE2E.ButtonsMessage{ContentText: proto.String("Confirm the visit?"), FooterText: proto.String("Clinic")}}, "Confirm the visit?\n\nClinic"},
		"list":         {&waE2E.Message{ListMessage: &waE2E.ListMessage{Title: proto.String("Menu"), Description: proto.String("Pick a dish")}}, "Menu\n\nPick a dish"},
		"product":      {&waE2E.Message{ProductMessage: &waE2E.ProductMessage{Product: &waE2E.ProductMessage_ProductSnapshot{Title: proto.String("Chair"), Description: proto.String("Oak")}}}, "Chair\n\nOak"},
		"group_invite": {&waE2E.Message{GroupInviteMessage: &waE2E.GroupInviteMessage{GroupName: proto.String("Villa team"), Caption: proto.String("Join us")}}, "Villa team\n\nJoin us"},
	} {
		t.Run(name, func(t *testing.T) {
			v, err := NormalizeMessage(groupFixture("BIZ", c.m), types.EmptyJID)
			if err != nil {
				t.Fatal(err)
			}
			requireFields(t, v, map[string]any{"messageType": name, "body": c.body, "isService": false, "hasMedia": false})
		})
	}
	// An interactive template is read like an interactive message.
	v, err := NormalizeMessage(groupFixture("BIZ2", &waE2E.Message{TemplateMessage: &waE2E.TemplateMessage{Format: &waE2E.TemplateMessage_InteractiveMessageTemplate{InteractiveMessageTemplate: interactive}}}), types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	requireFields(t, v, map[string]any{"messageType": "template", "body": "Invoice 42\n\nYour invoice is ready.\n\nReply STOP"})
}

// spec: ANYM-471
func TestNativeBusinessPlaceholderStaysEmpty(t *testing.T) {
	v, err := NormalizeMessage(groupFixture("PH", &waE2E.Message{PlaceholderMessage: &waE2E.PlaceholderMessage{Type: waE2E.PlaceholderMessage_MASK_LINKED_DEVICES.Enum()}}), types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	requireFields(t, v, map[string]any{"messageType": "placeholder", "body": "", "hasMedia": false, "isService": false})
}

// spec: ANYM-471
func TestNativeAlbumPhotoDownloadsAfterStoreReload(t *testing.T) {
	data := bytes.Repeat([]byte("synthetic album photo "), 500)
	hash := sha256.Sum256(data)
	m := albumChild("ALB3", &waE2E.Message{ImageMessage: &waE2E.ImageMessage{Mimetype: proto.String("image/jpeg"), FileLength: proto.Uint64(uint64(len(data))), FileSHA256: hash[:]}})
	s := openTestStore(t)
	if err := s.Put(m, "live"); err != nil {
		t.Fatal(err)
	}
	chat := "120363000000000001@g.us"
	loaded, err := s.Get(chat, LegacyMessageID(m))
	if err != nil {
		t.Fatal(err)
	}
	meta, err := nativeMediaMetadata(loaded)
	if err != nil {
		t.Fatal(err)
	}
	req := mediaRequest{Chat: chat, Message: LegacyMessageID(m)}
	req.Descriptor.ProviderID, req.Descriptor.Bytes, req.Descriptor.Filename, req.Descriptor.Mime = req.Message, meta.Bytes, meta.Filename, meta.Mime
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if err = os.Chmod(root, 0700); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	err = downloadMedia(context.Background(), req, Config{PagesDirectory: root, MaxMediaBytes: 1024 * 1024}, mediaPolicy, s.Get, func(_ context.Context, d whatsmeow.DownloadableMessage, f whatsmeow.File) error {
		if _, ok := d.(*waE2E.ImageMessage); !ok {
			t.Fatalf("downloader got %T, want the album image", d)
		}
		_, e := io.Copy(f, bytes.NewReader(data))
		return e
	}, func(chunk []byte) error { _, e := out.Write(chunk); return e })
	if err != nil {
		t.Fatal(err)
	}
	if got := sha256.Sum256(out.Bytes()); !bytes.Equal(out.Bytes(), data) || got != hash {
		t.Fatal("album photo bytes or hash differ")
	}
}

// spec: ANYM-471
func TestNativeProtocolEventsWithSenderKeyKeepTheirHandling(t *testing.T) {
	s := openTestStore(t)
	if err := s.Put(groupFixture("ORIG", &waE2E.Message{Conversation: proto.String("first")}), "live"); err != nil {
		t.Fatal(err)
	}
	key := &waCommon.MessageKey{ID: proto.String("ORIG"), Participant: proto.String("222222@s.whatsapp.net")}
	withKey := func(pm *waE2E.ProtocolMessage) *waE2E.Message {
		m := senderKeyPart()
		m.ProtocolMessage = pm
		return m
	}
	edit := withKey(&waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(), Key: key, EditedMessage: &waE2E.Message{Conversation: proto.String("second")}, TimestampMS: proto.Int64(1700000100000)})
	revoke := withKey(&waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_REVOKE.Enum(), Key: key})
	history := withKey(&waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_HISTORY_SYNC_NOTIFICATION.Enum(), HistorySyncNotification: &waE2E.HistorySyncNotification{FileLength: proto.Uint64(100)}})
	if senderKeyOnly(history) {
		t.Fatal("history-sync notification with a sender key was treated as sender-key-only")
	}
	for i, m := range []*waE2E.Message{edit, revoke} {
		if err := s.Put(groupFixture([]string{"EDIT", "REVOKE"}[i], m), "live"); err != nil {
			t.Fatal(i, err)
		}
	}
	rows, err := s.db.Query("SELECT envelope FROM capture ORDER BY seq")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var got []string
	for rows.Next() {
		var raw []byte
		var v map[string]any
		if err = rows.Scan(&raw); err != nil || json.Unmarshal(raw, &v) != nil {
			t.Fatal(err)
		}
		label, _ := v["kind"].(string)
		if c, ok := v["change"].(string); ok {
			label += "/" + c
		}
		got = append(got, label)
	}
	want := []string{"whatsapp_event/message", "whatsapp_event/edit", "whatsapp_delete"}
	if len(got) != len(want) || got[0] != want[0] || got[1] != want[1] || got[2] != want[2] {
		t.Fatalf("journal = %v, want %v", got, want)
	}
}
