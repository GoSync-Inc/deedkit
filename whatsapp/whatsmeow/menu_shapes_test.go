package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"

	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
)

// Values that must never appear in the report: option IDs, labels, texts.
var menuShapeSecrets = []string{"SECRET-ROW", "Secret row", "SECRET-QUICK", "Secret quick", "SECRET-TPL", "Secret template",
	"Secret body", "MENU1", "MENU2", "MENU3", "111111"}

func menuShapeEvent(id string, stamp int64, fromMe bool, m *waE2E.Message) *events.Message {
	chat := types.NewJID("111111", types.DefaultUserServer)
	sender := chat
	if fromMe {
		sender = types.NewJID("123456", types.DefaultUserServer)
	}
	return &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: chat, Sender: sender, IsFromMe: fromMe},
		ID: id, Timestamp: time.Unix(stamp, 0)}, Message: m, RawMessage: m}
}

func quoting(id string) *waE2E.ContextInfo {
	return &waE2E.ContextInfo{StanzaID: proto.String(id), Participant: proto.String("111111@s.whatsapp.net")}
}

// spec: ANYM-837
func TestMenuReplyShapesPrintsRelationsOfTheOwnersRepliesAndNoValues(t *testing.T) {
	config := testConfig(t)
	s, err := OpenCapture(config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	s.Owner = types.NewJID("123456", types.DefaultUserServer)
	list := &waE2E.Message{ListMessage: &waE2E.ListMessage{Description: proto.String("Secret body"), ButtonText: proto.String("Choose"),
		Sections: []*waE2E.ListMessage_Section{{Rows: []*waE2E.ListMessage_Row{{RowID: proto.String("SECRET-ROW"), Title: proto.String("Secret row")}}}}}}
	interactive := &waE2E.Message{InteractiveMessage: &waE2E.InteractiveMessage{Body: &waE2E.InteractiveMessage_Body{Text: proto.String("Secret body")},
		InteractiveMessage: &waE2E.InteractiveMessage_NativeFlowMessage_{NativeFlowMessage: &waE2E.InteractiveMessage_NativeFlowMessage{Buttons: []*waE2E.InteractiveMessage_NativeFlowMessage_NativeFlowButton{
			{Name: proto.String("cta_url"), ButtonParamsJSON: proto.String(`{"display_text":"Site","url":"https://x.example/p"}`)},
			{Name: proto.String("quick_reply"), ButtonParamsJSON: proto.String(`{"display_text":"Secret quick","id":"SECRET-QUICK"}`)}}}}}}
	template := &waE2E.Message{TemplateMessage: &waE2E.TemplateMessage{HydratedTemplate: &waE2E.TemplateMessage_HydratedFourRowTemplate{
		HydratedContentText: proto.String("Secret body"),
		HydratedButtons: []*waE2E.HydratedTemplateButton{
			{Index: proto.Uint32(5), HydratedButton: &waE2E.HydratedTemplateButton_UrlButton{UrlButton: &waE2E.HydratedTemplateButton_HydratedURLButton{DisplayText: proto.String("Site"), URL: proto.String("https://x.example")}}},
			{Index: proto.Uint32(7), HydratedButton: &waE2E.HydratedTemplateButton_QuickReplyButton{QuickReplyButton: &waE2E.HydratedTemplateButton_HydratedQuickReplyButton{DisplayText: proto.String("Secret template"), ID: proto.String("SECRET-TPL")}}}}}}}
	msgs := []*events.Message{
		menuShapeEvent("MENU1", 1700000001, false, list),
		menuShapeEvent("MENU2", 1700000002, false, interactive),
		menuShapeEvent("MENU3", 1700000003, false, template),
		menuShapeEvent("REPLY1", 1700000011, true, &waE2E.Message{ListResponseMessage: &waE2E.ListResponseMessage{Title: proto.String("Secret row"),
			ListType: waE2E.ListResponseMessage_SINGLE_SELECT.Enum(), SingleSelectReply: &waE2E.ListResponseMessage_SingleSelectReply{SelectedRowID: proto.String("SECRET-ROW")}, ContextInfo: quoting("MENU1")}}),
		menuShapeEvent("REPLY2", 1700000012, true, &waE2E.Message{InteractiveResponseMessage: &waE2E.InteractiveResponseMessage{
			Body: &waE2E.InteractiveResponseMessage_Body{Text: proto.String("Secret quick")},
			InteractiveResponseMessage: &waE2E.InteractiveResponseMessage_NativeFlowResponseMessage_{NativeFlowResponseMessage: &waE2E.InteractiveResponseMessage_NativeFlowResponseMessage{
				Name: proto.String("quick_reply"), ParamsJSON: proto.String(`{"id":"SECRET-QUICK"}`), Version: proto.Int32(3)}},
			ContextInfo: quoting("MENU2")}}),
		menuShapeEvent("REPLY3", 1700000013, true, &waE2E.Message{TemplateButtonReplyMessage: &waE2E.TemplateButtonReplyMessage{SelectedID: proto.String("SECRET-TPL"),
			SelectedDisplayText: proto.String("Secret template"), SelectedIndex: proto.Uint32(7), ContextInfo: quoting("MENU3")}}),
		// A reply whose menu the journal does not hold, and a reply by the other side.
		menuShapeEvent("REPLY4", 1700000014, true, &waE2E.Message{ButtonsResponseMessage: &waE2E.ButtonsResponseMessage{SelectedButtonID: proto.String("SECRET-ROW"), ContextInfo: quoting("GONE")}}),
		menuShapeEvent("REPLY5", 1700000015, false, &waE2E.Message{ButtonsResponseMessage: &waE2E.ButtonsResponseMessage{SelectedButtonID: proto.String("SECRET-ROW")}}),
	}
	for _, m := range msgs {
		if err := s.Put(m, "live"); err != nil {
			t.Fatal(err)
		}
	}
	path := filepath.Join(filepath.Dir(config.SessionDirectory), "config.json")
	if err := os.WriteFile(path, mustJSON(config), 0600); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	if err := serveMenuReplyShapes(path, &out); err != nil {
		t.Fatal(err)
	}
	for _, secret := range menuShapeSecrets {
		if strings.Contains(out.String(), secret) {
			t.Fatalf("report holds the value %q: %s", secret, out.String())
		}
	}
	var report struct {
		OwnerReplies int `json:"owner_replies"`
		OtherReplies int `json:"other_replies"`
		Incomplete   bool
		Units        map[string]struct {
			Count     int
			Fields    []string
			Relations map[string]int
		}
	}
	if err := json.Unmarshal(out.Bytes(), &report); err != nil {
		t.Fatal(err)
	}
	if report.OwnerReplies != 4 || report.OtherReplies != 1 || report.Incomplete {
		t.Fatalf("counts: %s", out.String())
	}
	want := map[string][]string{
		"list/list_response":                           {"selected_id=source_raw_id", "text=source_title", "list_type=SINGLE_SELECT", "quoted_message=absent"},
		"interactive/interactive_response:quick_reply": {"selected_id=source_raw_id", "params_keys=id", "params_key_holding_source_id=id", "version=3", "text=source_title"},
		"template_hydrated/template_reply":             {"selected_id=source_raw_id", "selected_index=explicit_index", "text=source_title", "context_participant=set"},
		"no_source/buttons_response":                   {"source_menu=not_in_journal"},
	}
	for unit, relations := range want {
		u, ok := report.Units[unit]
		if !ok || u.Count != 1 {
			t.Fatalf("unit %s missing: %s", unit, out.String())
		}
		for _, r := range relations {
			if u.Relations[r] != 1 {
				t.Fatalf("unit %s lacks %s: %v", unit, r, u.Relations)
			}
		}
	}
	// Explicit index 7 is the second button: no positional hypothesis matches it.
	for r := range report.Units["template_hydrated/template_reply"].Relations {
		if strings.HasPrefix(r, "selected_index=position") {
			t.Fatalf("index relation %s claimed", r)
		}
	}
	if !strings.Contains(strings.Join(report.Units["interactive/interactive_response:quick_reply"].Fields, ","), "nativeFlowResponseMessage.paramsJSON") {
		t.Fatalf("fields: %v", report.Units["interactive/interactive_response:quick_reply"].Fields)
	}
	// Read-only: the scan's connection cannot write, and no journal is created where none exists.
	db, err := sql.Open("sqlite3", mediaKeysDSN(filepath.Join(config.SessionDirectory, "capture.sqlite")))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec("DELETE FROM capture"); err == nil {
		t.Fatal("menu shapes journal connection accepted a delete")
	}
	empty := testConfig(t)
	emptyPath := filepath.Join(filepath.Dir(empty.SessionDirectory), "config.json")
	if err := os.WriteFile(emptyPath, mustJSON(empty), 0600); err != nil {
		t.Fatal(err)
	}
	requireError(t, serveMenuReplyShapes(emptyPath, &out), "menu_shapes_unavailable")
	if _, err := os.Stat(filepath.Join(empty.SessionDirectory, "capture.sqlite")); !os.IsNotExist(err) {
		t.Fatal("menu shapes created a journal")
	}
}

// spec: ANYM-837
func TestMenuReplyShapesPrintsOnlyKnownProtocolWordsAndBoundsItsScan(t *testing.T) {
	config := testConfig(t)
	s, err := OpenCapture(config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	s.Owner = types.NewJID("123456", types.DefaultUserServer)
	interactive := &waE2E.Message{InteractiveMessage: &waE2E.InteractiveMessage{InteractiveMessage: &waE2E.InteractiveMessage_NativeFlowMessage_{NativeFlowMessage: &waE2E.InteractiveMessage_NativeFlowMessage{Buttons: []*waE2E.InteractiveMessage_NativeFlowMessage_NativeFlowButton{
		{Name: proto.String("quick_reply"), ButtonParamsJSON: proto.String(`{"display_text":"A","id":"opt_a"}`)},
		{Name: proto.String("quick_reply"), ButtonParamsJSON: proto.String(`{"display_text":"B","id":"opt_b"}`)}}}}}}
	template := &waE2E.Message{TemplateMessage: &waE2E.TemplateMessage{HydratedTemplate: &waE2E.TemplateMessage_HydratedFourRowTemplate{
		HydratedButtons: []*waE2E.HydratedTemplateButton{{HydratedButton: &waE2E.HydratedTemplateButton_QuickReplyButton{QuickReplyButton: &waE2E.HydratedTemplateButton_HydratedQuickReplyButton{DisplayText: proto.String("T"), ID: proto.String("tpl_a")}}}}}}}
	msgs := []*events.Message{
		menuShapeEvent("MENU1", 1700000001, false, interactive),
		menuShapeEvent("MENU2", 1700000002, false, template),
		// Message-controlled lowercase words and numbers in every printed position.
		menuShapeEvent("REPLY1", 1700000011, true, &waE2E.Message{InteractiveResponseMessage: &waE2E.InteractiveResponseMessage{
			InteractiveResponseMessage: &waE2E.InteractiveResponseMessage_NativeFlowResponseMessage_{NativeFlowResponseMessage: &waE2E.InteractiveResponseMessage_NativeFlowResponseMessage{
				Name: proto.String("private_word"), ParamsJSON: proto.String(`{"secret_key":"x","id":"opt_a","title":"opt_b"}`), Version: proto.Int32(987654)}},
			ContextInfo: quoting("MENU1")}}),
		menuShapeEvent("REPLY2", 1700000012, true, &waE2E.Message{ListResponseMessage: &waE2E.ListResponseMessage{ListType: waE2E.ListResponseMessage_ListType(424242).Enum(), ContextInfo: quoting("NONE")}}),
		// The template button has no explicit Index; the reply's index 0 is not evidence of one.
		menuShapeEvent("REPLY3", 1700000013, true, &waE2E.Message{TemplateButtonReplyMessage: &waE2E.TemplateButtonReplyMessage{SelectedID: proto.String("tpl_a"), SelectedIndex: proto.Uint32(0), ContextInfo: quoting("MENU2")}}),
	}
	for _, m := range msgs {
		if err := s.Put(m, "live"); err != nil {
			t.Fatal(err)
		}
	}
	path := filepath.Join(filepath.Dir(config.SessionDirectory), "config.json")
	if err := os.WriteFile(path, mustJSON(config), 0600); err != nil {
		t.Fatal(err)
	}
	// The scan runs while the owning adapter keeps writing; no capture write fails.
	done := make(chan error, 1)
	var out bytes.Buffer
	go func() { done <- serveMenuReplyShapes(path, &out) }()
	for i := 0; i < 50; i++ {
		if err := s.Put(fixtureMessage("W"+strings.Repeat("x", i%5)+string(rune('a'+i%26))+strconv.Itoa(i), 1700001000+int64(i)), "live"); err != nil {
			t.Fatal(err)
		}
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	for _, leak := range []string{"private_word", "secret_key", "987654", "424242", "opt_a", "opt_b", "tpl_a"} {
		if strings.Contains(out.String(), leak) {
			t.Fatalf("report holds %q: %s", leak, out.String())
		}
	}
	var report struct {
		Units map[string]struct{ Relations map[string]int }
	}
	if err := json.Unmarshal(out.Bytes(), &report); err != nil {
		t.Fatal(err)
	}
	flow := report.Units["interactive/interactive_response:other"].Relations
	if flow["params_selection=ambiguous"] != 1 || flow["version=other"] != 1 || flow["params_keys=id,title,other"] != 1 || flow["selected_id=source_raw_id"] != 0 {
		t.Fatalf("flow relations %v", flow)
	}
	if report.Units["no_source/list_response"].Relations["list_type=other"] != 1 {
		t.Fatalf("list relations %v", report.Units)
	}
	tpl := report.Units["template_hydrated/template_reply"].Relations
	if tpl["selected_index=explicit_index"] != 0 || tpl["source_index=absent"] != 1 || tpl["selected_index=position0_all"] != 1 {
		t.Fatalf("template relations %v", tpl)
	}
	// A journal that keeps growing ends the scan at its budget, reported as incomplete.
	defer func(budget int) { menuShapeRowBudget = budget }(menuShapeRowBudget)
	menuShapeRowBudget = 10
	endless := func(_ context.Context, after, _ int64) (menuShapeRow, int64, bool, error) {
		return menuShapeRow{chat: operationsChat, raw: []byte("{}")}, after + 1, true, nil
	}
	if r := menuReplyShapes(endless, nil, 1<<62); !r.Incomplete || r.IncompleteCause != "scan_budget" || r.RowsScanned != 10 {
		t.Fatalf("unbounded scan: %+v", r)
	}
	// The CLI's stderr names the missing journal.
	if safeError(errors.New("menu_shapes_unavailable")) != "menu_shapes_unavailable" {
		t.Fatal("menu_shapes_unavailable is not a safe error")
	}
}
