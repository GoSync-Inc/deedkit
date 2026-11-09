package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"go.mau.fi/whatsmeow"
	waCommon "go.mau.fi/whatsmeow/proto/waCommon"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	waHistorySync "go.mau.fi/whatsmeow/proto/waHistorySync"
	waWeb "go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/encoding/protowire"
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
	if controlOnly(mixed) {
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

var groupJID = types.NewJID("120363000000000009", types.GroupServer)

func member(user string) types.JID { return types.NewJID(user, types.DefaultUserServer) }

// groupEntries returns the journaled group-change entries in journal order.
func groupEntries(t *testing.T, s *CaptureStore) []map[string]any {
	t.Helper()
	rows, err := s.db.Query("SELECT envelope FROM capture ORDER BY seq")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	out := []map[string]any{}
	for rows.Next() {
		var data []byte
		var v map[string]any
		if err = rows.Scan(&data); err != nil || json.Unmarshal(data, &v) != nil {
			t.Fatal("unreadable journal entry", err)
		}
		if event, _ := v["event"].(map[string]any); event["messageType"] == "group_change" {
			out = append(out, v)
		}
	}
	return out
}

func groupChangeOf(t *testing.T, entry map[string]any) map[string]any {
	t.Helper()
	event := entry["event"].(map[string]any)
	requireArchivable(t, event)
	if entry["origin"] == "live" && (event["isService"] != true || event["chatId"] != "120363000000000009@g.us" || event["body"] != "") {
		t.Fatalf("not a live service entry in the group: %#v", event)
	}
	change, ok := event["groupChange"].(map[string]any)
	if !ok {
		t.Fatalf("no groupChange: %#v", event)
	}
	return change
}

func participantsOf(change map[string]any) string {
	var ids []string
	for _, id := range change["participants"].([]any) {
		ids = append(ids, id.(string))
	}
	return strings.Join(ids, ",")
}

// spec: ANYM-869
func TestNativeGroupInfoChangesAreJournaledOnceEachWithActorAndParticipants(t *testing.T) {
	p := skipProvider(t)
	admin, alice, bob := member("333333"), member("444444"), member("555555")
	at := time.Unix(1700000100, 0)
	name, topic := "Родители 5Б", "Собрание в пятницу\nв 18:00"
	cases := []struct {
		change, actor, participants string
		value                       any
		event                       events.GroupInfo
	}{
		{"rename", "333333@c.us", "", name, events.GroupInfo{Sender: &admin, Name: &types.GroupName{Name: name}}},
		{"topic", "333333@c.us", "", topic, events.GroupInfo{Sender: &admin, Topic: &types.GroupTopic{Topic: topic}}},
		{"join", "444444@c.us", "444444@c.us", nil, events.GroupInfo{Sender: &alice, JoinReason: "invite", Join: []types.JID{alice}}},
		{"leave", "444444@c.us", "444444@c.us", nil, events.GroupInfo{Sender: &alice, Leave: []types.JID{alice}}},
		{"add", "333333@c.us", "444444@c.us,555555@c.us", nil, events.GroupInfo{Sender: &admin, Join: []types.JID{bob, alice}}},
		{"remove", "333333@c.us", "555555@c.us", nil, events.GroupInfo{Sender: &admin, Leave: []types.JID{bob}}},
		{"promote", "333333@c.us", "444444@c.us", nil, events.GroupInfo{Sender: &admin, Promote: []types.JID{alice}}},
		{"demote", "333333@c.us", "444444@c.us", nil, events.GroupInfo{Sender: &admin, Demote: []types.JID{alice}}},
	}
	for i, c := range cases {
		e := c.event
		e.JID, e.Timestamp = groupJID, at.Add(time.Duration(i)*time.Second)
		if !p.handleGroupEvent(&e) {
			t.Fatal("handler refused", c.change)
		}
		entries := groupEntries(t, p.Capture)
		if len(entries) != i+1 {
			t.Fatalf("%s: %d entries, want %d", c.change, len(entries), i+1)
		}
		got := groupChangeOf(t, entries[i])
		if entries[i]["origin"] != "live" || got["change"] != c.change || got["actor"] != c.actor || participantsOf(got) != c.participants || got["value"] != c.value {
			t.Fatalf("%s: %#v origin=%v", c.change, got, entries[i]["origin"])
		}
		if id := entries[i]["event"].(map[string]any)["messageId"]; id != fmt.Sprintf("group_120363000000000009@g.us_%d_%s", e.Timestamp.Unix(), c.change) {
			t.Fatalf("%s: provider ID %v is not derived from group, time and change", c.change, id)
		}
		// A redelivered notification is the same entry.
		if !p.handleGroupEvent(&e) || len(groupEntries(t, p.Capture)) != i+1 {
			t.Fatal("redelivery journaled a second entry", c.change)
		}
	}
	// One notification with several changes: one entry each, in a stable order.
	multi := events.GroupInfo{JID: groupJID, Timestamp: time.Unix(1700000200, 0), Sender: &admin, Name: &types.GroupName{Name: "Новое имя"}, Promote: []types.JID{bob}, Join: []types.JID{alice, admin}, Locked: &types.GroupLocked{IsLocked: true}}
	if !p.handleGroupEvent(&multi) {
		t.Fatal("handler refused")
	}
	entries := groupEntries(t, p.Capture)[len(cases):]
	var order []string
	for _, entry := range entries {
		order = append(order, groupChangeOf(t, entry)["change"].(string))
	}
	if strings.Join(order, ",") != "rename,join,add,promote" {
		t.Fatal("multi-change order", order)
	}
	if got := groupChangeOf(t, entries[1]); participantsOf(got) != "333333@c.us" {
		t.Fatal("the actor's own add is a join", got)
	}
	// A settings-only change (cut from the card) is not archived and not counted.
	settings := events.GroupInfo{JID: groupJID, Timestamp: time.Unix(1700000300, 0), Sender: &admin, Ephemeral: &types.GroupEphemeral{IsEphemeral: true, DisappearingTimer: 86400}}
	if !p.handleGroupEvent(&settings) || len(groupEntries(t, p.Capture)) != len(cases)+4 || len(skipCounts(t, p.Capture)) != 0 {
		t.Fatal("settings change archived or counted")
	}
	// Two separate adds in one second share the derived ID; the second is kept under its own suffix, once.
	first := events.GroupInfo{JID: groupJID, Timestamp: time.Unix(1700000400, 0), Sender: &admin, Join: []types.JID{alice}}
	second := first
	second.Join = []types.JID{bob}
	for _, e := range []*events.GroupInfo{&first, &second, &second, &first} {
		if !p.handleGroupEvent(e) {
			t.Fatal("handler refused")
		}
	}
	same := groupEntries(t, p.Capture)[len(cases)+4:]
	if len(same) != 2 || participantsOf(groupChangeOf(t, same[1])) != "555555@c.us" || !strings.HasPrefix(same[1]["event"].(map[string]any)["messageId"].(string), "group_120363000000000009@g.us_1700000400_add_") {
		t.Fatalf("same-second adds: %d entries", len(same))
	}
}

// spec: ANYM-869
func TestNativeJoinedGroupIsJournaledWithItsNameAndMembers(t *testing.T) {
	p := skipProvider(t)
	admin := member("333333")
	joined := &events.JoinedGroup{Sender: &admin, GroupInfo: types.GroupInfo{JID: groupJID, GroupName: types.GroupName{Name: "Кружок робототехники"}, ParticipantVersionID: "1700000400123", GroupCreated: time.Unix(1690000000, 0),
		Participants: []types.GroupParticipant{{JID: member("555555")}, {JID: admin, IsAdmin: true}, {JID: member("123456")}}}}
	if !p.handleGroupEvent(joined) || !p.handleGroupEvent(joined) {
		t.Fatal("handler refused")
	}
	entries := groupEntries(t, p.Capture)
	if len(entries) != 1 {
		t.Fatalf("%d entries for one join", len(entries))
	}
	got := groupChangeOf(t, entries[0])
	if got["change"] != "joined_group" || got["value"] != "Кружок робототехники" || got["actor"] != "333333@c.us" || participantsOf(got) != "123456@c.us,333333@c.us,555555@c.us" {
		t.Fatalf("%#v", got)
	}
	var title string
	p.Capture.db.QueryRow("SELECT title FROM chats WHERE chat=?", "120363000000000009@g.us").Scan(&title)
	if title != "Кружок робототехники" {
		t.Fatal("local dialog title not set from the join", title)
	}
}

// spec: ANYM-869
func TestNativeHistoryGroupStubMapsToTheLiveEntry(t *testing.T) {
	p := skipProvider(t)
	owner := p.Capture.Owner
	p.Client = nativeClient(&store.Device{ID: &owner})
	admin := member("333333")
	live := events.GroupInfo{JID: groupJID, Timestamp: time.Unix(1700000500, 0), Sender: &admin, Name: &types.GroupName{Name: "Дача"}}
	if !p.handleGroupEvent(&live) {
		t.Fatal("handler refused")
	}
	stub := func(id string, at uint64, kind waWeb.WebMessageInfo_StubType, params ...string) *waHistorySync.HistorySyncMsg {
		return &waHistorySync.HistorySyncMsg{Message: &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{RemoteJID: proto.String(groupJID.String()), ID: proto.String(id)}, Participant: proto.String("333333@s.whatsapp.net"), MessageTimestamp: proto.Uint64(at), MessageStubType: kind.Enum(), MessageStubParameters: params}}
	}
	data := &waHistorySync.HistorySync{Conversations: []*waHistorySync.Conversation{{ID: proto.String(groupJID.String()), Messages: []*waHistorySync.HistorySyncMsg{
		stub("STUB1", 1700000500, waWeb.WebMessageInfo_GROUP_CHANGE_SUBJECT, "Дача"),
		stub("STUB2", 1700000400, waWeb.WebMessageInfo_GROUP_PARTICIPANT_ADD, "444444@s.whatsapp.net"),
	}}}}
	for range 2 {
		if err := p.captureHistory(data); err != nil {
			t.Fatal(err)
		}
	}
	entries := groupEntries(t, p.Capture)
	if len(entries) != 2 {
		t.Fatalf("%d entries, want the live rename and one backfilled add", len(entries))
	}
	rename, add := entries[0]["event"].(map[string]any), entries[1]["event"].(map[string]any)
	if rename["messageId"] != "group_120363000000000009@g.us_1700000500_rename" || entries[0]["origin"] != "live" {
		t.Fatal("history stub did not map to the live entry", rename["messageId"], entries[0]["origin"])
	}
	got := groupChangeOf(t, entries[1])
	if add["messageId"] != "group_120363000000000009@g.us_1700000400_add" || entries[1]["origin"] != "backfill" || got["actor"] != "333333@c.us" || participantsOf(got) != "444444@c.us" {
		t.Fatalf("backfilled stub: %v %v %#v", add["messageId"], entries[1]["origin"], got)
	}
	var rows int
	p.Capture.db.QueryRow("SELECT COUNT(*) FROM capture").Scan(&rows)
	if rows != 2 {
		t.Fatal("history stub also journaled as a bare notification", rows)
	}
	// Two history-only adds of different members in one second keep both (Astra r1 B2); a synced
	// rename that arrives before its live notification is still one entry.
	more := &waHistorySync.HistorySync{Conversations: []*waHistorySync.Conversation{{ID: proto.String(groupJID.String()), Messages: []*waHistorySync.HistorySyncMsg{
		stub("STUB3", 1700000300, waWeb.WebMessageInfo_GROUP_PARTICIPANT_ADD, "666666@s.whatsapp.net"),
		stub("STUB4", 1700000300, waWeb.WebMessageInfo_GROUP_PARTICIPANT_ADD, "777777@s.whatsapp.net"),
		stub("STUB5", 1700000700, waWeb.WebMessageInfo_GROUP_CHANGE_SUBJECT, "Огород"),
	}}}}
	for range 2 {
		if err := p.captureHistory(more); err != nil {
			t.Fatal(err)
		}
	}
	later := events.GroupInfo{JID: groupJID, Timestamp: time.Unix(1700000700, 0), Sender: &admin, Name: &types.GroupName{Name: "Огород"}}
	if !p.handleGroupEvent(&later) {
		t.Fatal("handler refused")
	}
	entries = groupEntries(t, p.Capture)[2:]
	var kept []string
	for _, entry := range entries {
		kept = append(kept, participantsOf(groupChangeOf(t, entry))+"/"+entry["origin"].(string))
	}
	if strings.Join(kept, " ") != "666666@c.us/backfill 777777@c.us/backfill /backfill" {
		t.Fatal("synced same-second changes", kept)
	}
}

// spec: ANYM-869
func TestNativeLateOlderRenameNeverRewindsTheDialogTitle(t *testing.T) {
	p := skipProvider(t)
	admin := member("333333")
	title := func() string {
		var v string
		p.Capture.db.QueryRow("SELECT title FROM chats WHERE chat=?", "120363000000000009@g.us").Scan(&v)
		return v
	}
	for _, r := range []struct {
		at   int64
		name string
		want string
	}{{1700000900, "Новое", "Новое"}, {1700000800, "Старое", "Новое"}, {1700001000, "Новейшее", "Новейшее"}} {
		e := events.GroupInfo{JID: groupJID, Timestamp: time.Unix(r.at, 0), Sender: &admin, Name: &types.GroupName{Name: r.name}}
		if !p.handleGroupEvent(&e) || title() != r.want {
			t.Fatalf("after %q the dialog title is %q, want %q", r.name, title(), r.want)
		}
	}
}

// spec: ANYM-869
func TestNativeMalformedGroupChangeIsCountedAndTheNextMessageIsCaptured(t *testing.T) {
	p := skipProvider(t)
	admin := member("333333")
	// 2048 IDs within every per-field bound still exceed the journal limit together (Astra r1 B1).
	var huge []types.JID
	for i := range maxGroupChangeParticipants {
		huge = append(huge, member(strings.Repeat("€", 140)+strconv.Itoa(i)))
	}
	for _, bad := range []any{
		(*events.GroupInfo)(nil), (*events.JoinedGroup)(nil), // typed nil (Astra r1 m1)
		&events.GroupInfo{JID: groupJID, Timestamp: time.Unix(1700000600, 0), Sender: &admin, Join: huge},
		&events.GroupInfo{JID: groupJID, Sender: &admin, Name: &types.GroupName{Name: "x"}},                                                    // no time
		&events.GroupInfo{JID: member("777777"), Timestamp: time.Unix(1700000600, 0), Sender: &admin, Name: &types.GroupName{Name: "x"}},       // not a group
		&events.GroupInfo{JID: groupJID, Timestamp: time.Unix(1700000600, 0), Sender: &admin, Join: []types.JID{types.NewJID("bad\x00", "x")}}, // unusable member ID
		&events.JoinedGroup{},
	} {
		// true: whatsmeow acknowledges it once and never redelivers it.
		if !p.handleGroupEvent(bad) {
			t.Fatalf("malformed group change refused: %#v", bad)
		}
	}
	if got := skipCounts(t, p.Capture); got["skipped_group_change"] != 7 || len(got) != 1 {
		t.Fatal(got)
	}
	if n := spoolFiles(t, p.Config.EventsDirectory); n != 0 || !p.ready.Load() {
		t.Fatal("malformed change exported or provider stopped", n)
	}
	if err := p.handleMessage(fixtureMessage("after", 1700000601)); err != nil {
		t.Fatal(err)
	}
	if n := spoolFiles(t, p.Config.EventsDirectory); n != 1 {
		t.Fatal("message after a malformed change not captured", n)
	}
}

// pollSecrets is an in-memory whatsmeow message-secret store (synthetic keys).
type pollSecrets map[string]struct {
	secret []byte
	sender types.JID
}

func pollSecretKey(chat, sender types.JID, id types.MessageID) string {
	return chat.ToNonAD().String() + "|" + sender.ToNonAD().String() + "|" + id
}
func (s pollSecrets) PutMessageSecrets(ctx context.Context, inserts []store.MessageSecretInsert) error {
	for _, i := range inserts {
		_ = s.PutMessageSecret(ctx, i.Chat, i.Sender, i.ID, i.Secret)
	}
	return nil
}
func (s pollSecrets) PutMessageSecret(_ context.Context, chat, sender types.JID, id types.MessageID, secret []byte) error {
	s[pollSecretKey(chat, sender, id)] = struct {
		secret []byte
		sender types.JID
	}{secret, sender}
	return nil
}
func (s pollSecrets) GetMessageSecret(_ context.Context, chat, sender types.JID, id types.MessageID) ([]byte, types.JID, error) {
	v, ok := s[pollSecretKey(chat, sender, id)]
	if !ok {
		return nil, types.EmptyJID, nil
	}
	return v.secret, v.sender, nil
}

func syntheticPoll(id string, question string, options ...string) *events.Message {
	poll := &waE2E.PollCreationMessage{Name: proto.String(question), SelectableOptionsCount: proto.Uint32(1)}
	for _, o := range options {
		poll.Options = append(poll.Options, &waE2E.PollCreationMessage_Option{OptionName: proto.String(o)})
	}
	m := &waE2E.Message{PollCreationMessageV3: poll, MessageContextInfo: &waE2E.MessageContextInfo{MessageSecret: bytes.Repeat([]byte{7}, 32)}}
	e := groupFixture(id, m)
	e.Info.Chat, e.Info.Sender = groupJID, member("444444")
	return e
}

// pollProvider journals a synthetic group poll by 444444 whose secret the store holds,
// and returns a client of voter 555555 that encrypts votes with the same secret.
func pollProvider(t *testing.T) (*Provider, *whatsmeow.Client, *events.Message, pollSecrets) {
	t.Helper()
	p := skipProvider(t)
	owner := p.Capture.Owner
	secrets := pollSecrets{}
	p.Client = nativeClient(&store.Device{ID: &owner, MsgSecrets: secrets})
	poll := syntheticPoll("POLL1", "Кто в субботу?", "Да", "Нет", "Может быть")
	if err := p.handleMessage(poll); err != nil {
		t.Fatal(err)
	}
	_ = secrets.PutMessageSecret(context.Background(), poll.Info.Chat, poll.Info.Sender, poll.Info.ID, poll.Message.GetMessageContextInfo().GetMessageSecret())
	voter := member("555555")
	return p, nativeClient(&store.Device{ID: &voter, MsgSecrets: secrets}), poll, secrets
}

func pollVote(t *testing.T, voter *whatsmeow.Client, poll *events.Message, id string, at int64, selected ...[]byte) *events.Message {
	t.Helper()
	update, err := voter.EncryptPollVote(context.Background(), &poll.Info, &waE2E.PollVoteMessage{SelectedOptions: selected})
	if err != nil {
		t.Fatal(err)
	}
	update.SenderTimestampMS = proto.Int64(at)
	e := groupFixture(id, &waE2E.Message{PollUpdateMessage: update})
	e.Info.Chat, e.Info.Sender, e.Info.Timestamp = groupJID, member("555555"), time.UnixMilli(at)
	return e
}

func journalKinds(t *testing.T, s *CaptureStore) []map[string]any {
	t.Helper()
	rows, err := s.db.Query("SELECT envelope FROM capture ORDER BY seq")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	out := []map[string]any{}
	for rows.Next() {
		var data []byte
		var v map[string]any
		if err = rows.Scan(&data); err != nil || json.Unmarshal(data, &v) != nil {
			t.Fatal("unreadable journal entry", err)
		}
		out = append(out, v)
	}
	return out
}

func pollOptionHash(name string) []byte { return whatsmeow.HashPollOptions([]string{name})[0] }

// spec: ANYM-921
func TestNativePollCreationKeepsItsOptionsAndSelectableCount(t *testing.T) {
	owner := types.NewJID("123456", types.DefaultUserServer)
	poll := func(name string, count uint32, options ...string) *waE2E.PollCreationMessage {
		v := &waE2E.PollCreationMessage{Name: proto.String(name), SelectableOptionsCount: proto.Uint32(count)}
		for _, o := range options {
			v.Options = append(v.Options, &waE2E.PollCreationMessage_Option{OptionName: proto.String(o)})
		}
		return v
	}
	for name, m := range map[string]*waE2E.Message{
		"v1": {PollCreationMessage: poll("Кто в субботу?", 0, "Да", "Нет")},
		"v2": {PollCreationMessageV2: poll("Кто в субботу?", 0, "Да", "Нет")},
		"v3": {PollCreationMessageV3: poll("Кто в субботу?", 0, "Да", "Нет")},
	} {
		v, err := NormalizeMessage(groupFixture("P"+name, m), owner)
		if err != nil {
			t.Fatal(name, err)
		}
		requireFields(t, v, map[string]any{"messageType": "poll_creation", "body": "Кто в субботу?"})
		data, _ := json.Marshal(v["poll"])
		if string(data) != `{"options":[{"index":0,"text":"Да"},{"index":1,"text":"Нет"}],"question":"Кто в субботу?","selectable_count":0,"truncated":false}` {
			t.Fatal(name, string(data))
		}
	}
	var many []string
	for i := range 13 {
		many = append(many, strconv.Itoa(i)+strings.Repeat("я", 300))
	}
	v, err := NormalizeMessage(groupFixture("PBIG", &waE2E.Message{PollCreationMessageV3: poll(strings.Repeat("в", 600), 3, many...)}), owner)
	if err != nil {
		t.Fatal(err)
	}
	block := v["poll"].(map[string]any)
	options := block["options"].([]any)
	if len(options) != 12 || block["truncated"] != true || block["selectable_count"] != uint32(3) || utf8.RuneCountInString(block["question"].(string)) != 512 {
		t.Fatalf("bounds: %d options, %#v", len(options), block["truncated"])
	}
	for i, o := range options {
		if o.(map[string]any)["index"] != i || utf8.RuneCountInString(o.(map[string]any)["text"].(string)) != 256 {
			t.Fatal("option bound", i)
		}
	}
	if v, _ := NormalizeMessage(fixtureMessage("TEXT", 1700000000), owner); v["poll"] != nil {
		t.Fatal("poll block on a text message")
	}
}

// spec: ANYM-921
func TestNativePollVoteIsOneJournalEntryWithTheChosenOptions(t *testing.T) {
	p, voter, poll, _ := pollProvider(t)
	vote := pollVote(t, voter, poll, "VOTE1", 1700000100250, pollOptionHash("Нет"), pollOptionHash("Да"), pollOptionHash("forged"))
	if err := p.handleMessage(vote); err != nil {
		t.Fatal(err)
	}
	entries := journalKinds(t, p.Capture)
	if len(entries) != 2 || entries[0]["event"].(map[string]any)["poll"] == nil {
		t.Fatalf("want the poll and one vote entry, got %d", len(entries))
	}
	got := entries[1]
	data, _ := json.Marshal(got["option_indices"])
	if got["kind"] != "poll_vote" || got["poll_provider_id"] != "false_120363000000000009@g.us_POLL1_444444@c.us" || got["voter"] != "555555@c.us" ||
		got["from_me"] != false || string(data) != "[0,1]" || got["voted_at"] != "2023-11-14T22:15:00.25Z" || got["chat"] != "120363000000000009@g.us" || got["origin"] != "live" {
		t.Fatalf("vote entry: %#v", got)
	}
	if _, ok := got["event"]; ok {
		t.Fatal("a vote is not a chat message")
	}
	if counts := skipCounts(t, p.Capture); counts["poll_vote_unknown_option"] != 1 || len(counts) != 1 {
		t.Fatal(counts)
	}
	// A retracted vote is an empty selection; a vote of only unknown options is no retraction.
	if err := p.handleMessage(pollVote(t, voter, poll, "VOTE2", 1700000200000)); err != nil {
		t.Fatal(err)
	}
	if err := p.handleMessage(pollVote(t, voter, poll, "VOTE3", 1700000300000, pollOptionHash("forged"))); err != nil {
		t.Fatal(err)
	}
	entries = journalKinds(t, p.Capture)
	if len(entries) != 3 || fmt.Sprint(entries[2]["option_indices"]) != "[]" {
		t.Fatalf("retraction: %d entries", len(entries))
	}
}

// spec: ANYM-921
func TestNativeUndecryptablePollVoteIsCountedWithoutContentAndTheNextMessageIsCaptured(t *testing.T) {
	p, voter, poll, secrets := pollProvider(t)
	other := syntheticPoll("POLL2", "Второй?", "А", "Б")
	_ = secrets.PutMessageSecret(context.Background(), other.Info.Chat, other.Info.Sender, other.Info.ID, other.Message.GetMessageContextInfo().GetMessageSecret())
	unknown := pollVote(t, voter, other, "VOTE9", 1700000100000, pollOptionHash("А")) // its poll was never journaled
	lost := pollVote(t, voter, poll, "VOTE7", 1700000100000)                          // its secret is gone below
	clear(secrets)
	noSecret := groupFixture("VOTE8", &waE2E.Message{PollUpdateMessage: &waE2E.PollUpdateMessage{
		PollCreationMessageKey: &waCommon.MessageKey{RemoteJID: proto.String(groupJID.String()), ID: proto.String("POLL1"), Participant: proto.String("444444@s.whatsapp.net")},
		Vote:                   &waE2E.PollEncValue{EncPayload: []byte("synthetic"), EncIV: bytes.Repeat([]byte{1}, 12)}}})
	noSecret.Info.Chat, noSecret.Info.Sender = groupJID, member("555555")
	stderr := os.Stderr
	r, w, _ := os.Pipe()
	os.Stderr = w
	for _, vote := range []*events.Message{unknown, noSecret, lost} {
		if err := p.handleMessage(vote); err != nil {
			t.Fatal(err)
		}
	}
	w.Close()
	os.Stderr = stderr
	logged, _ := io.ReadAll(r)
	if counts := skipCounts(t, p.Capture); counts["poll_vote_undecryptable"] != 3 || len(counts) != 1 {
		t.Fatal(counts)
	}
	if bytes.Contains(logged, []byte("555555")) || bytes.Contains(logged, []byte("POLL")) || !bytes.Contains(logged, []byte("poll_vote_undecryptable")) {
		t.Fatalf("log: %s", logged)
	}
	if n := spoolFiles(t, p.Config.EventsDirectory); n != 1 || !p.ready.Load() {
		t.Fatal("undecryptable vote exported or provider stopped", n)
	}
	if err := p.handleMessage(fixtureMessage("after", 1700000601)); err != nil {
		t.Fatal(err)
	}
	if n := spoolFiles(t, p.Config.EventsDirectory); n != 2 {
		t.Fatal("message after an undecryptable vote not captured", n)
	}
}

// spec: ANYM-921
func TestNativeHistoryPollAndItsVotesMatchTheLiveEntriesWithoutDuplicates(t *testing.T) {
	p, voter, poll, _ := pollProvider(t)
	if err := p.handleMessage(pollVote(t, voter, poll, "VOTE1", 1700000100000, pollOptionHash("Да"))); err != nil {
		t.Fatal(err)
	}
	creation := proto.Clone(poll.Message).(*waE2E.Message)
	update := func(id, participant string, at int64, options ...string) *waWeb.PollUpdate {
		return &waWeb.PollUpdate{PollUpdateMessageKey: &waCommon.MessageKey{RemoteJID: proto.String(groupJID.String()), ID: proto.String(id), Participant: proto.String(participant)},
			Vote: &waE2E.PollVoteMessage{SelectedOptions: whatsmeow.HashPollOptions(options)}, SenderTimestampMS: proto.Int64(at)}
	}
	data := &waHistorySync.HistorySync{Conversations: []*waHistorySync.Conversation{{ID: proto.String(groupJID.String()), Messages: []*waHistorySync.HistorySyncMsg{
		{Message: &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{RemoteJID: proto.String(groupJID.String()), ID: proto.String("POLL1")}, Participant: proto.String("444444@s.whatsapp.net"),
			MessageTimestamp: proto.Uint64(uint64(poll.Info.Timestamp.Unix())), Message: creation,
			PollUpdates: []*waWeb.PollUpdate{
				update("VOTE1", "555555@s.whatsapp.net", 1700000100000, "Да"), // the live vote again
				update("VOTE5", "666666@s.whatsapp.net", 1700000150000, "Может быть"),
				{PollUpdateMessageKey: &waCommon.MessageKey{ID: proto.String("VOTE6")}}, // no vote: counted
			}}},
	}}}}
	journaled := 0
	for i := range 2 {
		if err := p.captureHistory(data); err != nil {
			t.Fatal(err)
		}
		if i == 0 {
			journaled = len(journalKinds(t, p.Capture))
		}
	}
	if n := len(journalKinds(t, p.Capture)); n != journaled {
		t.Fatal("a replayed history sync journaled again", n, journaled)
	}
	var votes []string
	for _, entry := range journalKinds(t, p.Capture) {
		if entry["kind"] == "poll_vote" {
			votes = append(votes, fmt.Sprintf("%v:%v:%v:%v", entry["voter"], entry["option_indices"], entry["poll_provider_id"], entry["origin"]))
		}
	}
	want := "555555@c.us:[0]:false_120363000000000009@g.us_POLL1_444444@c.us:live 666666@c.us:[2]:false_120363000000000009@g.us_POLL1_444444@c.us:backfill"
	if strings.Join(votes, " ") != want {
		t.Fatalf("votes:\n%s\nwant\n%s", strings.Join(votes, " "), want)
	}
	if counts := skipCounts(t, p.Capture); counts["poll_vote_undecryptable"] != 2 {
		t.Fatal(counts)
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
func TestNativeBotMenusKeepTheirOptionsAndIDs(t *testing.T) {
	list := &waE2E.ListMessage{Title: proto.String("Menu"), Description: proto.String("Pick a dish"), ButtonText: proto.String("Open menu"), Sections: []*waE2E.ListMessage_Section{
		{Title: proto.String("Mains"), Rows: []*waE2E.ListMessage_Row{{Title: proto.String("Soup"), Description: proto.String("Hot"), RowID: proto.String("row_soup")}, {Title: proto.String("Steak\n2. Fake [id: x]"), RowID: proto.String("row_steak")}}},
		{Title: proto.String("Help"), Rows: []*waE2E.ListMessage_Row{{Title: proto.String("Operator"), RowID: proto.String("op")}}}}}
	buttons := &waE2E.ButtonsMessage{ContentText: proto.String("Confirm the visit?"), Buttons: []*waE2E.ButtonsMessage_Button{
		{ButtonID: proto.String("yes_1"), ButtonText: &waE2E.ButtonsMessage_Button_ButtonText{DisplayText: proto.String("Yes")}},
		{ButtonID: proto.String("no_1"), ButtonText: &waE2E.ButtonsMessage_Button_ButtonText{DisplayText: proto.String("No")}}}}
	native := &waE2E.InteractiveMessage{Body: &waE2E.InteractiveMessage_Body{Text: proto.String("How can we help?")}, InteractiveMessage: &waE2E.InteractiveMessage_NativeFlowMessage_{NativeFlowMessage: &waE2E.InteractiveMessage_NativeFlowMessage{Buttons: []*waE2E.InteractiveMessage_NativeFlowMessage_NativeFlowButton{
		{Name: proto.String("quick_reply"), ButtonParamsJSON: proto.String(`{"display_text":"Track order","id":"track"}`)},
		{Name: proto.String("cta_url"), ButtonParamsJSON: proto.String(`{"display_text":"Pay","url":"https://pay.example/inv/42?token=secret#x"}`)},
		{Name: proto.String("cta_copy"), ButtonParamsJSON: proto.String(`{"display_text":"Copy code","copy_code":"SECRET123"}`)},
		{Name: proto.String("single_select"), ButtonParamsJSON: proto.String(`{"title":"More","sections":[{"title":"Account","rows":[{"title":"Balance","description":"Current","id":"bal"}]}]}`)},
		{Name: proto.String("broken"), ButtonParamsJSON: proto.String(`{`)}}}}}
	template := &waE2E.TemplateMessage_HydratedFourRowTemplate{HydratedContentText: proto.String("Check-in at 14:00"), HydratedButtons: []*waE2E.HydratedTemplateButton{
		{HydratedButton: &waE2E.HydratedTemplateButton_QuickReplyButton{QuickReplyButton: &waE2E.HydratedTemplateButton_HydratedQuickReplyButton{DisplayText: proto.String("Late check-in"), ID: proto.String("late")}}},
		{HydratedButton: &waE2E.HydratedTemplateButton_CallButton{CallButton: &waE2E.HydratedTemplateButton_HydratedCallButton{DisplayText: proto.String("Call hotel"), PhoneNumber: proto.String("+10000000000")}}}}}
	for name, c := range map[string]struct {
		m    *waE2E.Message
		body string
	}{
		"list":        {&waE2E.Message{ListMessage: list}, "Menu\n\nPick a dish\n\nMenu options (Open menu):\nMains:\n1. Soup - Hot [id: row_soup]\n2. Steak 2. Fake (id: x) [id: row_steak]\nHelp:\n3. Operator [id: op]"},
		"buttons":     {&waE2E.Message{ButtonsMessage: buttons}, "Confirm the visit?\n\nMenu options:\n1. Yes [id: yes_1]\n2. No [id: no_1]"},
		"interactive": {&waE2E.Message{InteractiveMessage: native}, "How can we help?\n\nMenu options:\n1. Track order [id: track]\n2. Pay [link: https://pay.example/inv/42]\n3. Copy code (copy button)\nMore:\n4. Account: Balance - Current [id: bal]\n5. (broken button)"},
		"template":    {&waE2E.Message{TemplateMessage: &waE2E.TemplateMessage{Format: &waE2E.TemplateMessage_HydratedFourRowTemplate_{HydratedFourRowTemplate: template}}}, "Check-in at 14:00\n\nMenu options:\n1. Late check-in [id: late]\n2. Call hotel [call: +10000000000]"},
	} {
		t.Run(name, func(t *testing.T) {
			v, err := NormalizeMessage(groupFixture("MENU", c.m), types.EmptyJID)
			if err != nil {
				t.Fatal(err)
			}
			requireFields(t, v, map[string]any{"messageType": name, "body": c.body, "isService": false, "hasMedia": false})
		})
	}
	// A huge list is bounded, and says how much it left out.
	var rows []*waE2E.ListMessage_Row
	for i := 0; i < maxMenuOptions+5; i++ {
		rows = append(rows, &waE2E.ListMessage_Row{Title: proto.String("Item"), RowID: proto.String("r")})
	}
	v, err := NormalizeMessage(groupFixture("BIG", &waE2E.Message{ListMessage: &waE2E.ListMessage{Sections: []*waE2E.ListMessage_Section{{Rows: rows}}}}), types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	if body := v["body"].(string); !strings.HasSuffix(body, "\n100. Item [id: r]\n(5 more options not shown)") {
		t.Fatalf("body tail = %q", body[len(body)-80:])
	}
	// Past the scan budget the count is a lower bound; empty sections spend the budget too.
	var sections []*waE2E.ListMessage_Section
	for i := 0; i < 2*maxMenuScan; i++ {
		sections = append(sections, &waE2E.ListMessage_Section{})
	}
	sections = append(sections, &waE2E.ListMessage_Section{Rows: rows})
	v, err = NormalizeMessage(groupFixture("EMPTY", &waE2E.Message{ListMessage: &waE2E.ListMessage{Title: proto.String("Menu"), Sections: sections}}), types.EmptyJID)
	if err != nil || v["body"] != "Menu" {
		t.Fatalf("empty sections: %v %q", err, v["body"])
	}
	rows = nil
	for i := 0; i < 2*maxMenuScan; i++ {
		rows = append(rows, &waE2E.ListMessage_Row{Title: proto.String("Item"), RowID: proto.String("r")})
	}
	v, err = NormalizeMessage(groupFixture("SCAN", &waE2E.Message{ListMessage: &waE2E.ListMessage{Sections: []*waE2E.ListMessage_Section{{Rows: rows}}}}), types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	if body := v["body"].(string); !strings.HasSuffix(body, "\n100. Item [id: r]\n(at least 899 more options not shown)") {
		t.Fatalf("scan tail = %q", body[len(body)-80:])
	}
	// The whole body stays within the envelope limit: complete option lines only, then the omission line.
	long := strings.Repeat("x", maxMenuText)
	rows = nil
	for i := 0; i < maxMenuOptions; i++ {
		rows = append(rows, &waE2E.ListMessage_Row{Title: proto.String(long), Description: proto.String(long), RowID: proto.String(long)})
	}
	text := strings.Repeat("y", maxEnvelopeBody-2000)
	v, err = NormalizeMessage(groupFixture("FIT", &waE2E.Message{ListMessage: &waE2E.ListMessage{Title: proto.String(text), Sections: []*waE2E.ListMessage_Section{{Rows: rows}}}}), types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	body := v["body"].(string)
	if utf8.RuneCountInString(body) > maxEnvelopeBody || !strings.HasPrefix(body, text+"\n\nMenu options:\n1. ") || !strings.HasSuffix(body, " [id: "+long+"]\n(98 more options not shown)") {
		t.Fatalf("fit: %d runes, tail %q", utf8.RuneCountInString(body), body[len(body)-120:])
	}
	// A text that fills the envelope gives up room for a notice; no partial option line.
	notice := "\n\n(a menu of 100 options is not shown: the message is too long)"
	v, _ = NormalizeMessage(groupFixture("FULL", &waE2E.Message{ListMessage: &waE2E.ListMessage{Title: proto.String(strings.Repeat("y", maxEnvelopeBody)), Sections: []*waE2E.ListMessage_Section{{Rows: rows}}}}), types.EmptyJID)
	if body := v["body"].(string); body != strings.Repeat("y", maxEnvelopeBody-utf8.RuneCountInString(notice))+notice {
		t.Fatalf("full text: %d runes, tail %q", utf8.RuneCountInString(body), body[len(body)-90:])
	}
	// Native-flow selection lists share the budget: inner sections spend it, and decoded parameter bytes are bounded.
	empty := strings.Repeat(`{"rows":[]},`, 1200)
	selectButton := &waE2E.InteractiveMessage_NativeFlowMessage_NativeFlowButton{Name: proto.String("single_select"), ButtonParamsJSON: proto.String(`{"sections":[` + empty + `{"rows":[{"title":"Late","id":"late"}]}]}`)}
	v, _ = NormalizeMessage(groupFixture("NFSEC", &waE2E.Message{InteractiveMessage: &waE2E.InteractiveMessage{Body: &waE2E.InteractiveMessage_Body{Text: proto.String("Pick")}, InteractiveMessage: &waE2E.InteractiveMessage_NativeFlowMessage_{NativeFlowMessage: &waE2E.InteractiveMessage_NativeFlowMessage{Buttons: []*waE2E.InteractiveMessage_NativeFlowMessage_NativeFlowButton{selectButton}}}}}), types.EmptyJID)
	if v["body"] != "Pick" {
		t.Fatalf("nested sections: %q", v["body"])
	}
	var many []*waE2E.InteractiveMessage_NativeFlowMessage_NativeFlowButton
	params := `{"display_text":"Go","id":"` + strings.Repeat("g", maxMenuParams-40) + `"}`
	for i := 0; i < 10; i++ {
		many = append(many, &waE2E.InteractiveMessage_NativeFlowMessage_NativeFlowButton{Name: proto.String("quick_reply"), ButtonParamsJSON: proto.String(params)})
	}
	v, _ = NormalizeMessage(groupFixture("NFBYTES", &waE2E.Message{InteractiveMessage: &waE2E.InteractiveMessage{InteractiveMessage: &waE2E.InteractiveMessage_NativeFlowMessage_{NativeFlowMessage: &waE2E.InteractiveMessage_NativeFlowMessage{Buttons: many}}}}), types.EmptyJID)
	if body := v["body"].(string); strings.Count(body, "\n") != 5 || !strings.HasSuffix(body, "]\n(more options not shown)") {
		t.Fatalf("param bytes: %q", body[len(body)-60:])
	}
}

// spec: ANYM-471
func TestNativeMenuRepliesKeepTheChosenOptionAndItsMenu(t *testing.T) {
	ctx := &waE2E.ContextInfo{StanzaID: proto.String("MENU1"), Participant: proto.String("333333@s.whatsapp.net")}
	for name, c := range map[string]struct {
		m    *waE2E.Message
		body string
	}{
		"list_response":    {&waE2E.Message{ListResponseMessage: &waE2E.ListResponseMessage{Title: proto.String("Soup"), Description: proto.String("Hot"), SingleSelectReply: &waE2E.ListResponseMessage_SingleSelectReply{SelectedRowID: proto.String("row_soup")}, ContextInfo: ctx}}, "Chose menu option: Soup - Hot [id: row_soup]"},
		"buttons_response": {&waE2E.Message{ButtonsResponseMessage: &waE2E.ButtonsResponseMessage{SelectedButtonID: proto.String("yes_1"), Response: &waE2E.ButtonsResponseMessage_SelectedDisplayText{SelectedDisplayText: "Yes"}, ContextInfo: ctx}}, "Chose menu option: Yes [id: yes_1]"},
		"template_reply":   {&waE2E.Message{TemplateButtonReplyMessage: &waE2E.TemplateButtonReplyMessage{SelectedID: proto.String("late"), SelectedDisplayText: proto.String("Late check-in"), ContextInfo: ctx}}, "Chose menu option: Late check-in [id: late]"},
		// A Flow's response holds form answers: only its visible text is kept.
		"interactive_response_flow": {&waE2E.Message{InteractiveResponseMessage: &waE2E.InteractiveResponseMessage{Body: &waE2E.InteractiveResponseMessage_Body{Text: proto.String("Sent")}, InteractiveResponseMessage: &waE2E.InteractiveResponseMessage_NativeFlowResponseMessage_{NativeFlowResponseMessage: &waE2E.InteractiveResponseMessage_NativeFlowResponseMessage{Name: proto.String("flow"), ParamsJSON: proto.String(`{"id":"my passport 1234","flow_token":"t"}`)}}, ContextInfo: ctx}}, "Chose menu option: Sent"},
		"interactive_response":      {&waE2E.Message{InteractiveResponseMessage: &waE2E.InteractiveResponseMessage{Body: &waE2E.InteractiveResponseMessage_Body{Text: proto.String("Track order")}, InteractiveResponseMessage: &waE2E.InteractiveResponseMessage_NativeFlowResponseMessage_{NativeFlowResponseMessage: &waE2E.InteractiveResponseMessage_NativeFlowResponseMessage{Name: proto.String("quick_reply"), ParamsJSON: proto.String(`{"id":"track"}`)}}, ContextInfo: ctx}}, "Chose menu option: Track order [id: track]"},
	} {
		t.Run(name, func(t *testing.T) {
			v, err := NormalizeMessage(groupFixture("CHOICE", c.m), types.EmptyJID)
			if err != nil {
				t.Fatal(err)
			}
			requireFields(t, v, map[string]any{"messageType": strings.TrimSuffix(name, "_flow"), "body": c.body, "isService": false, "hasMedia": false, "replyToId": "MENU1"})
		})
	}
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
	if controlOnly(history) {
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

// spec: ANYM-471
func TestNativeContextOnlyEventIsSkippedButUnknownFieldsAreKept(t *testing.T) {
	s := openTestStore(t)
	contextOnly := &waE2E.Message{MessageContextInfo: &waE2E.MessageContextInfo{MessageSecret: []byte{4, 5, 6}}}
	if err := s.Put(groupFixture("CTX1", contextOnly), "live"); err != nil {
		t.Fatal(err)
	}
	var rows int
	s.db.QueryRow("SELECT COUNT(*) FROM capture").Scan(&rows)
	if rows != 0 || spoolFiles(t, s.Config.EventsDirectory) != 0 {
		t.Fatalf("context-only event captured: rows=%d", rows)
	}
	doc := &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{FileName: proto.String("plan.pdf"), Mimetype: proto.String("application/pdf"), FileLength: proto.Uint64(1024)}, MessageContextInfo: contextOnly.MessageContextInfo}
	if err := s.Put(groupFixture("CTX1", doc), "live"); err != nil {
		t.Fatal(err)
	}
	s.db.QueryRow("SELECT COUNT(*) FROM capture").Scan(&rows)
	if rows != 1 {
		t.Fatalf("document after context-only event not captured once: rows=%d", rows)
	}
	// A field this build cannot read may be content: the event is journaled.
	future := senderKeyPart()
	wire := protowire.AppendTag(nil, 9999, protowire.BytesType)
	future.ProtoReflect().SetUnknown(protowire.AppendBytes(wire, []byte("future content")))
	if controlOnly(future) {
		t.Fatal("sender key with an unknown wire field was treated as control-only")
	}
	if err := s.Put(groupFixture("FUT1", future), "live"); err != nil {
		t.Fatal(err)
	}
	s.db.QueryRow("SELECT COUNT(*) FROM capture").Scan(&rows)
	if rows != 2 {
		t.Fatalf("event with unknown wire field dropped: rows=%d", rows)
	}
	loaded, err := s.Get("120363000000000001@g.us", "false_120363000000000001@g.us_FUT1_222222@c.us")
	if err != nil || len(loaded.Message.ProtoReflect().GetUnknown()) == 0 {
		t.Fatalf("unknown wire field not retained in the journal: %v", err)
	}
}
