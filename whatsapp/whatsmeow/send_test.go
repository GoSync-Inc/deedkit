package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

const operationsChat = "15551234567@c.us"
const operationsGroup = "15550000000-123456789@g.us"

func operationMessage(chat, id string, fromMe bool, stamp int64) *events.Message {
	j, _ := ParseChatID(chat)
	sender, _ := ParseChatID("15559876543@c.us")
	if fromMe {
		sender, _ = ParseChatID("15550000000@c.us")
	}
	wire := &waE2E.Message{Conversation: proto.String("original text")}
	return &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: j, Sender: sender, IsFromMe: fromMe, IsGroup: j.Server == types.GroupServer}, ID: id, Timestamp: time.Unix(stamp, 0)}, Message: wire, RawMessage: wire}
}

func allowedOperationPolicy() Policy {
	ops := []string{}
	for op := range operationFields {
		ops = append(ops, op)
	}
	return Policy{Enabled: true, SendEnabled: true, DiscoverChats: true, OperationsEnabled: ops}
}

func sendFake(t *testing.T) sendDependencies {
	t.Helper()
	owner, _ := ParseChatID("15550000000@c.us")
	return sendDependencies{
		owner: owner, policy: func() (Policy, error) { return allowedOperationPolicy(), nil },
		get:   func(chat, id string) (*events.Message, error) { return nil, errors.New("missing") },
		put:   func(m *events.Message, origin string) error { return nil },
		newID: func() string { return "TESTSTANZA" },
		send: func(_ context.Context, to types.JID, _ *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
			return whatsmeow.SendResponse{ID: extra.ID, Timestamp: time.Unix(1700000000, 0), Chat: to, Sender: owner}, nil
		},
	}
}

func textFixture() textRequest {
	return textRequest{Chat: operationsChat, Text: "*bold* _italic_ https://example.org", ExpiresAt: float64(time.Now().Add(time.Hour).Unix())}
}
func requireError(t *testing.T, err error, code string) {
	t.Helper()
	if err == nil || err.Error() != code {
		t.Fatalf("want %s, got %v", code, err)
	}
}

func TestNativeTextValidatesBeforeSend(t *testing.T) {
	for name, modify := range map[string]func(*textRequest){
		"expired": func(r *textRequest) { r.ExpiresAt = 1 }, "nan": func(r *textRequest) { r.ExpiresAt = math.NaN() },
		"infinite": func(r *textRequest) { r.ExpiresAt = math.Inf(1) }, "empty": func(r *textRequest) { r.Text = " \n" },
		"control": func(r *textRequest) { r.Text = "unsafe\x00text" }, "utf16": func(r *textRequest) { r.Text = strings.Repeat("😀", 2001) },
		"target": func(r *textRequest) { r.Chat = "15551234567@evil" }, "rich_preview": func(r *textRequest) { v := true; r.LinkPreview = &v },
	} {
		t.Run(name, func(t *testing.T) {
			d := sendFake(t)
			called := false
			d.send = func(context.Context, types.JID, *waE2E.Message, whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
				called = true
				return whatsmeow.SendResponse{}, nil
			}
			r := textFixture()
			modify(&r)
			_, err := sendText(context.Background(), r, d)
			if err == nil || called {
				t.Fatalf("invalid request reached provider: %v, called=%v", err, called)
			}
		})
	}
}

func TestNativeTextPlainLinkAndQuoteAreExact(t *testing.T) {
	d := sendFake(t)
	r := textFixture()
	f := false
	r.LinkPreview = &f
	quoted := operationMessage(operationsChat, "QUOTED", false, 1600000000)
	r.ReplyProviderID = LegacyMessageID(quoted)
	d.get = func(chat, id string) (*events.Message, error) { return quoted, nil }
	d.send = func(_ context.Context, to types.JID, wire *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
		if wire.GetExtendedTextMessage().GetText() != r.Text || wire.GetExtendedTextMessage().GetContextInfo().GetStanzaID() != "QUOTED" || wire.GetExtendedTextMessage().GetMatchedText() != "" {
			t.Fatal("plain formatting/quote/preview changed")
		}
		return whatsmeow.SendResponse{ID: extra.ID, Timestamp: time.Unix(1700000000, 0), Chat: to, Sender: d.owner}, nil
	}
	d.put = func(m *events.Message, origin string) error {
		if m.Info.Timestamp.Unix() != 1700000000 || origin != "live" || !m.Info.IsFromMe {
			t.Fatal("capture invented receipt evidence")
		}
		return nil
	}
	result, err := sendText(context.Background(), r, d)
	if err != nil {
		t.Fatal(err)
	}
	if result.(map[string]any)["provider_message_id"] != "true_"+operationsChat+"_TESTSTANZA" {
		t.Fatalf("unexpected receipt %v", result)
	}
	quoted.Info.Chat, _ = ParseChatID("15559999999@c.us")
	_, err = sendText(context.Background(), r, d)
	requireError(t, err, "send_target_invalid")
}

func TestNativeTextChecksRevocationAfterResolvingReply(t *testing.T) {
	d := sendFake(t)
	r := textFixture()
	quoted := operationMessage(operationsChat, "Q", false, 1)
	r.ReplyProviderID = LegacyMessageID(quoted)
	p := allowedOperationPolicy()
	d.policy = func() (Policy, error) { return p, nil }
	d.get = func(string, string) (*events.Message, error) { p.SendEnabled = false; return quoted, nil }
	d.send = func(context.Context, types.JID, *waE2E.Message, whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
		t.Fatal("revoked send reached provider")
		return whatsmeow.SendResponse{}, nil
	}
	_, err := sendText(context.Background(), r, d)
	requireError(t, err, "sending_disabled")
}

// spec: ANYM-928
func TestNativeTextMentionsSetMentionedJIDOnTheWireTextAndKeepTheQuote(t *testing.T) {
	d := sendFake(t)
	var wires []*waE2E.Message
	d.send = func(_ context.Context, to types.JID, wire *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
		wires = append(wires, wire)
		return whatsmeow.SendResponse{ID: extra.ID, Timestamp: time.Unix(1700000000, 0), Chat: to, Sender: d.owner}, nil
	}
	r := textFixture()
	r.Chat, r.Text = operationsGroup, "Привет, @15559876543 и @123456789012345!"
	r.Mentions = []string{"15559876543@c.us", "123456789012345@lid"}
	if _, err := sendText(context.Background(), r, d); err != nil {
		t.Fatal(err)
	}
	quoted := operationMessage(operationsGroup, "QUOTED", false, 1600000000)
	r.ReplyProviderID = LegacyMessageID(quoted)
	d.get = func(chat, id string) (*events.Message, error) { return quoted, nil }
	if _, err := sendText(context.Background(), r, d); err != nil {
		t.Fatal(err)
	}
	want := []string{"15559876543@s.whatsapp.net", "123456789012345@lid"}
	for i, wire := range wires {
		text := wire.GetExtendedTextMessage()
		if wire.GetConversation() != "" || text.GetText() != r.Text || strings.Join(text.GetContextInfo().GetMentionedJID(), ",") != strings.Join(want, ",") {
			t.Fatalf("send %d: wrong mention wire %v", i, wire)
		}
		if (i == 1) != (text.GetContextInfo().GetStanzaID() == "QUOTED") {
			t.Fatalf("send %d: quote changed", i)
		}
	}
	if len(wires) != 2 {
		t.Fatalf("want 2 sends, got %d", len(wires))
	}
}

// spec: ANYM-928
func TestNativeTextMentionsRefuseBeforeAnySend(t *testing.T) {
	many := []string{}
	for i := 0; i < 21; i++ {
		many = append(many, fmt.Sprintf("1555000%04d@c.us", i))
	}
	for name, modify := range map[string]func(*textRequest){
		"private_chat":  func(r *textRequest) { r.Chat = operationsChat },
		"missing_token": func(r *textRequest) { r.Text = "Привет, 15559876543" },
		"joined_token":  func(r *textRequest) { r.Text = "mail@15559876543" },
		"longer_number": func(r *textRequest) { r.Text = "Hi @155598765430" },
		"group_jid":     func(r *textRequest) { r.Mentions = []string{operationsGroup} },
		"device_jid":    func(r *textRequest) { r.Mentions = []string{"15559876543:2@c.us"} },
		"duplicate":     func(r *textRequest) { r.Mentions = []string{"15559876543@c.us", "15559876543@c.us"} },
		"empty":         func(r *textRequest) { r.Mentions = []string{} },
		"too_many":      func(r *textRequest) { r.Mentions = many },
		"menu_choice":   func(r *textRequest) { r.MenuChoice = &menuChoiceRequest{} },
	} {
		t.Run(name, func(t *testing.T) {
			d := sendFake(t)
			d.send = func(context.Context, types.JID, *waE2E.Message, whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
				t.Fatal("an invalid mention reached the provider")
				return whatsmeow.SendResponse{}, nil
			}
			r := textFixture()
			r.Chat, r.Text, r.Mentions = operationsGroup, "Hi @15559876543", []string{"15559876543@c.us"}
			modify(&r)
			_, err := sendText(context.Background(), r, d)
			requireError(t, err, "send_target_invalid")
		})
	}
	var raw json.RawMessage = []byte(`{"chat":"` + operationsGroup + `","text":"Hi @15559876543","expires_at":4102444800,"mentions":["15559876543@c.us"]}`)
	var req textRequest
	if decodeStrict(raw, &req) != nil || len(req.Mentions) != 1 {
		t.Fatal("the Python collector's mentions field does not decode")
	}
}

// menuFixture is a bot's menu message in operationsChat with the given content.
func menuFixture(id string, m *waE2E.Message) *events.Message {
	e := operationMessage(operationsChat, id, false, 1600000000)
	e.Message, e.RawMessage = m, m
	return e
}

func listMenuMessage(rowID, title, description string) *waE2E.Message {
	return &waE2E.Message{ListMessage: &waE2E.ListMessage{Description: proto.String("Choose a department"), ButtonText: proto.String("Departments"),
		Sections: []*waE2E.ListMessage_Section{{Title: proto.String("Clinics"), Rows: []*waE2E.ListMessage_Row{
			{RowID: proto.String("dept_neuro"), Title: proto.String("Neurology")},
			{RowID: proto.String(rowID), Title: proto.String(title), Description: proto.String(description)}}}}}}
}

func menuChoiceFixture(menu *events.Message, optionID, label, menuType string) textRequest {
	r := textFixture()
	r.Text, r.ReplyProviderID = label, LegacyMessageID(menu)
	r.MenuChoice = &menuChoiceRequest{OptionID: optionID, Label: label, MenuType: menuType}
	return r
}

// spec: ANYM-837
func TestNativeMenuChoiceSendsTheStructuredReplyWithTheRawOptionOnce(t *testing.T) {
	list := menuFixture("MENU1", listMenuMessage("dept_cardio", "Cardiology", "Heart and vessels"))
	buttons := menuFixture("MENU2", &waE2E.Message{ButtonsMessage: &waE2E.ButtonsMessage{ContentText: proto.String("Confirm?"),
		Buttons: []*waE2E.ButtonsMessage_Button{{ButtonID: proto.String("yes_1"), ButtonText: &waE2E.ButtonsMessage_Button_ButtonText{DisplayText: proto.String("Yes")}, Type: waE2E.ButtonsMessage_Button_RESPONSE.Enum()}}}})
	for name, c := range map[string]struct {
		menu  *events.Message
		r     textRequest
		check func(*waE2E.Message) bool
	}{
		"list": {list, menuChoiceFixture(list, "dept_cardio", "Cardiology - Heart and vessels", "list"), func(m *waE2E.Message) bool {
			v := m.GetListResponseMessage()
			return v.GetSingleSelectReply().GetSelectedRowID() == "dept_cardio" && v.GetTitle() == "Cardiology" && v.GetDescription() == "Heart and vessels" &&
				v.GetListType() == waE2E.ListResponseMessage_SINGLE_SELECT && v.GetContextInfo().GetStanzaID() == "MENU1" && v.GetContextInfo().GetQuotedMessage() == nil
		}},
		"buttons": {buttons, menuChoiceFixture(buttons, "yes_1", "Yes", "buttons"), func(m *waE2E.Message) bool {
			v := m.GetButtonsResponseMessage()
			return v.GetSelectedButtonID() == "yes_1" && v.GetSelectedDisplayText() == "Yes" && v.GetType() == waE2E.ButtonsResponseMessage_DISPLAY_TEXT &&
				v.GetContextInfo().GetStanzaID() == "MENU2" && v.GetContextInfo().GetQuotedMessage() == nil
		}},
	} {
		t.Run(name, func(t *testing.T) {
			d := sendFake(t)
			d.get = func(chat, id string) (*events.Message, error) { return c.menu, nil }
			sends := 0
			d.send = func(_ context.Context, to types.JID, wire *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
				sends++
				if !c.check(wire) || wire.GetConversation() != "" || wire.GetExtendedTextMessage() != nil {
					t.Fatalf("wrong reply %v", wire)
				}
				return whatsmeow.SendResponse{ID: extra.ID, Timestamp: time.Unix(1700000000, 0), Chat: to, Sender: d.owner}, nil
			}
			var captured *events.Message
			d.put = func(m *events.Message, _ string) error { captured = m; return nil }
			if _, err := sendText(context.Background(), c.r, d); err != nil || sends != 1 {
				t.Fatalf("err=%v sends=%d", err, sends)
			}
			// The captured reply is archived as the chosen option, quoting the menu.
			if kind, body, ok := menuReply(captured.Message); !ok || !strings.HasPrefix(body, "Chose menu option: ") || kind == "" {
				t.Fatalf("capture %q %q", kind, body)
			}
		})
	}
}

// spec: ANYM-837
func TestNativeMenuChoiceRefusesBeforeAnySend(t *testing.T) {
	interactive := &waE2E.Message{InteractiveMessage: &waE2E.InteractiveMessage{InteractiveMessage: &waE2E.InteractiveMessage_NativeFlowMessage_{
		NativeFlowMessage: &waE2E.InteractiveMessage_NativeFlowMessage{Buttons: []*waE2E.InteractiveMessage_NativeFlowMessage_NativeFlowButton{
			{Name: proto.String("quick_reply"), ButtonParamsJSON: proto.String(`{"display_text":"Info","id":"info_1"}`)},
			{Name: proto.String("cta_url"), ButtonParamsJSON: proto.String(`{"display_text":"Pay","url":"https://pay.example/x"}`)},
			{Name: proto.String("review_and_pay"), ButtonParamsJSON: proto.String(`{"display_text":"Pay now","id":"pay_1"}`)}}}}}}
	ownMenu := menuFixture("MENU9", listMenuMessage("dept_cardio", "Cardiology", ""))
	ownMenu.Info.IsFromMe = true
	cases := map[string]struct {
		menu *events.Message
		r    func(*events.Message) textRequest
		code string
	}{
		// The bot edited the row behind an unchanged shown ID: a truncated suffix or a tab for a space.
		"raw id suffix past the shown limit": {menuFixture("M", listMenuMessage(strings.Repeat("a", 256)+"PAY", "Cardiology", "")), func(m *events.Message) textRequest {
			return menuChoiceFixture(m, strings.Repeat("a", 256), "Cardiology", "list")
		}, "menu_option_id_unrepresentable"},
		"tab in raw id": {menuFixture("M", listMenuMessage("dept\tcardio", "Cardiology", "")), func(m *events.Message) textRequest {
			return menuChoiceFixture(m, "dept cardio", "Cardiology", "list")
		}, "menu_option_id_unrepresentable"},
		"option gone":   {menuFixture("M", listMenuMessage("dept_other", "Cardiology", "")), func(m *events.Message) textRequest { return menuChoiceFixture(m, "dept_cardio", "Cardiology", "list") }, "menu_changed"},
		"label changed": {menuFixture("M", listMenuMessage("dept_cardio", "Pay 500", "")), func(m *events.Message) textRequest { return menuChoiceFixture(m, "dept_cardio", "Cardiology", "list") }, "menu_changed"},
		"type changed": {menuFixture("M", listMenuMessage("dept_cardio", "Cardiology", "")), func(m *events.Message) textRequest {
			return menuChoiceFixture(m, "dept_cardio", "Cardiology", "buttons")
		}, "menu_changed"},
		"duplicate id": {menuFixture("M", listMenuMessage("dept_neuro", "Neurology", "")), func(m *events.Message) textRequest {
			return menuChoiceFixture(m, "dept_neuro", "Neurology", "list")
		}, "menu_option_ambiguous"},
		"owner menu": {ownMenu, func(m *events.Message) textRequest { return menuChoiceFixture(m, "dept_cardio", "Cardiology", "list") }, "menu_unavailable"},
		"no capture": {nil, func(*events.Message) textRequest {
			return menuChoiceFixture(menuFixture("GONE", listMenuMessage("x", "y", "")), "x", "y", "list")
		}, "menu_unavailable"},
		"payment flow": {menuFixture("M", interactive), func(m *events.Message) textRequest {
			return menuChoiceFixture(m, "pay_1", "Pay now (review_and_pay button)", "interactive")
		}, "menu_changed"},
		"unverified shape": {menuFixture("M", interactive), func(m *events.Message) textRequest {
			return menuChoiceFixture(m, "info_1", "Info", "interactive")
		}, "menu_reply_shape_unverified"},
		// A duplicate past the kept prefix, or a menu the scan budget cut, cannot prove the ID unique.
		"duplicate past the kept options": {menuFixture("M", manyRowsMenu(maxMenuOptions+1, "dup", "dup")), func(m *events.Message) textRequest {
			return menuChoiceFixture(m, "dup", "Row 1", "list")
		}, "menu_option_ambiguous"},
		"scan budget exhausted": {menuFixture("M", manyRowsMenu(maxMenuScan+5, "first", "")), func(m *events.Message) textRequest {
			return menuChoiceFixture(m, "first", "Row 1", "list")
		}, "menu_option_ambiguous"},
		// A native-flow button inside a buttons menu is never a plain reply button,
		// whatever display text and button ID it also carries.
		"payment button with display text": {menuFixture("M", &waE2E.Message{ButtonsMessage: &waE2E.ButtonsMessage{Buttons: []*waE2E.ButtonsMessage_Button{{
			ButtonID: proto.String("pay_1"), ButtonText: &waE2E.ButtonsMessage_Button_ButtonText{DisplayText: proto.String("Pay")},
			Type:           waE2E.ButtonsMessage_Button_RESPONSE.Enum(),
			NativeFlowInfo: &waE2E.ButtonsMessage_Button_NativeFlowInfo{Name: proto.String("review_and_pay"), ParamsJSON: proto.String(`{"display_text":"Pay","id":"pay_1"}`)}}}}}),
			func(m *events.Message) textRequest { return menuChoiceFixture(m, "pay_1", "Pay", "buttons") }, "menu_changed"},
		"quick reply inside buttons": {menuFixture("M", &waE2E.Message{ButtonsMessage: &waE2E.ButtonsMessage{Buttons: []*waE2E.ButtonsMessage_Button{{
			Type:           waE2E.ButtonsMessage_Button_NATIVE_FLOW.Enum(),
			NativeFlowInfo: &waE2E.ButtonsMessage_Button_NativeFlowInfo{Name: proto.String("quick_reply"), ParamsJSON: proto.String(`{"display_text":"Info","id":"info_1"}`)}}}}}),
			func(m *events.Message) textRequest { return menuChoiceFixture(m, "info_1", "Info", "buttons") }, "menu_option_unsupported"},
		"text is not the label": {menuFixture("M", listMenuMessage("dept_cardio", "Cardiology", "")), func(m *events.Message) textRequest {
			r := menuChoiceFixture(m, "dept_cardio", "Cardiology", "list")
			r.Text = "Pay"
			return r
		}, "send_target_invalid"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			d := sendFake(t)
			if c.menu != nil {
				d.get = func(string, string) (*events.Message, error) { return c.menu, nil }
			}
			d.send = func(context.Context, types.JID, *waE2E.Message, whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
				t.Fatal("a refused menu choice reached the provider")
				return whatsmeow.SendResponse{}, nil
			}
			_, err := sendText(context.Background(), c.r(c.menu), d)
			requireError(t, err, c.code)
		})
	}
}

func manyRowsMenu(n int, first, last string) *waE2E.Message {
	rows := make([]*waE2E.ListMessage_Row, n)
	for i := range rows {
		rows[i] = &waE2E.ListMessage_Row{RowID: proto.String(fmt.Sprintf("row_%d", i+1)), Title: proto.String(fmt.Sprintf("Row %d", i+1))}
	}
	rows[0].RowID = proto.String(first)
	if last != "" {
		rows[n-1].RowID = proto.String(last)
	}
	return &waE2E.Message{ListMessage: &waE2E.ListMessage{Sections: []*waE2E.ListMessage_Section{{Rows: rows}}}}
}

// The archive shows the plain [id: X] marker, the only one a choice can name,
// only for an ID kept losslessly and answerable in its menu: a raw ID with a tab
// shows as inexact, so a later edit to the clean form cannot pass behind it.
// spec: ANYM-837
func TestNativeMenuArchiveShowsAChoosableIDOnlyWhenExact(t *testing.T) {
	render := func(m *waE2E.Message) string { _, c := menuOptions(m); return withMenu("", "", c) }
	tabbed := render(listMenuMessage("dept\tcardio", "Cardiology", ""))
	clean := render(listMenuMessage("dept cardio", "Cardiology", ""))
	long := render(listMenuMessage(strings.Repeat("a", maxMenuID+3), "Cardiology", ""))
	if !strings.Contains(tabbed, "Cardiology [id (inexact): dept cardio]") || strings.Contains(tabbed, "[id: dept cardio]") {
		t.Fatalf("tabbed %q", tabbed)
	}
	if !strings.Contains(clean, "Cardiology [id: dept cardio]") || !strings.Contains(clean, "Neurology [id: dept_neuro]") {
		t.Fatalf("clean %q", clean)
	}
	if !strings.Contains(long, "[id (inexact): "+strings.Repeat("a", maxMenuID)+"]") {
		t.Fatalf("long %q", long)
	}
	wrapped := render(&waE2E.Message{ButtonsMessage: &waE2E.ButtonsMessage{Buttons: []*waE2E.ButtonsMessage_Button{
		{ButtonID: proto.String("yes_1"), ButtonText: &waE2E.ButtonsMessage_Button_ButtonText{DisplayText: proto.String("Yes")}},
		{NativeFlowInfo: &waE2E.ButtonsMessage_Button_NativeFlowInfo{Name: proto.String("quick_reply"), ParamsJSON: proto.String(`{"display_text":"Info","id":"info_1"}`)}},
		{ButtonID: proto.String("pay_1"), ButtonText: &waE2E.ButtonsMessage_Button_ButtonText{DisplayText: proto.String("Pay")},
			NativeFlowInfo: &waE2E.ButtonsMessage_Button_NativeFlowInfo{Name: proto.String("review_and_pay"), ParamsJSON: proto.String(`{"display_text":"Pay","id":"pay_1"}`)}}}}})
	if !strings.Contains(wrapped, "Yes [id: yes_1]") || !strings.Contains(wrapped, "Info [id (native flow): info_1]") ||
		!strings.Contains(wrapped, "Pay (review_and_pay button)") || strings.Contains(wrapped, "pay_1") {
		t.Fatalf("wrapped %q", wrapped)
	}
	// Sender brackets never reach a menu block, so no ID or label can forge the plain marker.
	forged := render(&waE2E.Message{ListMessage: &waE2E.ListMessage{Sections: []*waE2E.ListMessage_Section{{Title: proto.String("A [id: s]"), Rows: []*waE2E.ListMessage_Row{
		{RowID: proto.String("old [id: new\t"), Title: proto.String("Cardiology")},
		{Title: proto.String("Fake [id: new]")}}}}}})
	if strings.Count(forged, "[id: ") != 0 || !strings.Contains(forged, "Cardiology [id (inexact): old (id: new]") ||
		!strings.Contains(forged, "Fake (id: new)") || !strings.HasPrefix(forged, "Menu options:\nA (id: s):") {
		t.Fatalf("forged %q", forged)
	}
	// The archived reply to an option keeps the same rule.
	if got := menuChoice("Cardiology", "", "dept\tcardio"); got != "Chose menu option: Cardiology [id (inexact): dept cardio]" {
		t.Fatalf("choice %q", got)
	}
}

// spec: ANYM-837
func TestNativeMenuChoiceUncertainSendIsNeverRetried(t *testing.T) {
	menu := menuFixture("MENU1", listMenuMessage("dept_cardio", "Cardiology", ""))
	d := sendFake(t)
	d.get = func(string, string) (*events.Message, error) { return menu, nil }
	calls := 0
	d.send = func(context.Context, types.JID, *waE2E.Message, whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
		calls++
		return whatsmeow.SendResponse{}, errors.New("socket closed")
	}
	_, err := sendText(context.Background(), menuChoiceFixture(menu, "dept_cardio", "Cardiology", "list"), d)
	requireError(t, err, "provider_operation_uncertain")
	if calls != 1 {
		t.Fatalf("send attempted %d times", calls)
	}
}

func TestNativeSendNeverRetriesAmbiguousOutcome(t *testing.T) {
	for _, mode := range []string{"timeout", "wrong_id", "wrong_chat", "missing_timestamp", "capture_failure"} {
		t.Run(mode, func(t *testing.T) {
			d := sendFake(t)
			calls, puts := 0, 0
			d.send = func(_ context.Context, to types.JID, _ *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
				calls++
				response := whatsmeow.SendResponse{ID: extra.ID, Chat: to, Sender: d.owner, Timestamp: time.Unix(1700000000, 0)}
				switch mode {
				case "timeout":
					return response, context.DeadlineExceeded
				case "wrong_id":
					response.ID = "OTHER"
				case "wrong_chat":
					response.Chat, _ = ParseChatID("15559999999@c.us")
				case "missing_timestamp":
					response.Timestamp = time.Time{}
				}
				return response, nil
			}
			d.put = func(*events.Message, string) error { puts++; return errors.New("disk failure") }
			_, err := sendText(context.Background(), textFixture(), d)
			want := "send_receipt_mismatch"
			if mode == "timeout" || mode == "capture_failure" {
				want = "provider_operation_uncertain"
			}
			requireError(t, err, want)
			if calls != 1 || (mode != "capture_failure" && puts != 0) {
				t.Fatalf("calls=%d puts=%d", calls, puts)
			}
		})
	}
}

func TestNativeTextProtocolRejectsUnknownFieldsAndTrailingJSON(t *testing.T) {
	for _, raw := range []string{`{"chat":"x","text":"y","expires_at":1,"raw_api":"danger"}`, `{} {}`, `{"text":42}`} {
		var r textRequest
		if decodeStrict(json.RawMessage(raw), &r) == nil {
			t.Fatalf("accepted %s", raw)
		}
	}
}

// spec: ANYM-837
func TestNativeMenuChoiceDecodesThePythonCollectorsSendText(t *testing.T) {
	// tests/test_menu_choice.py writes the params the Python collector sends and
	// asserts they equal this file; the native adapter must accept them strictly.
	raw, err := os.ReadFile(filepath.Join("..", "..", "tests", "fixtures", "menu-send-text.json"))
	if err != nil {
		t.Fatal(err)
	}
	var r textRequest
	if err := decodeStrict(json.RawMessage(raw), &r); err != nil {
		t.Fatal(err)
	}
	if r.MenuChoice == nil || r.MenuChoice.OptionID != "card_1" || r.MenuChoice.Label != r.Text || r.MenuChoice.MenuType != "list" || r.ReplyProviderID == "" {
		t.Fatalf("%+v %+v", r, r.MenuChoice)
	}
}

func TestNativeSendPNReceiptRetainsVerifiedWireIdentity(t *testing.T) {
	d := sendFake(t)
	requested, _ := ParseChatID(operationsChat)
	wireChat := types.NewJID("999900001111", types.HiddenUserServer)
	d.ownerLID = types.NewJID("999900002222", types.HiddenUserServer)
	d.lidForPN = func(_ context.Context, pn types.JID) (types.JID, error) {
		if pn != requested {
			t.Fatal("unexpected identity lookup", pn)
		}
		return wireChat, nil
	}
	d.pnForLID = func(_ context.Context, lid types.JID) (types.JID, error) {
		if lid != wireChat {
			t.Fatal("unexpected identity lookup", lid)
		}
		return requested, nil
	}
	calls := 0
	d.send = func(_ context.Context, to types.JID, wire *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
		calls++
		if to != requested {
			t.Fatal("requested destination changed")
		}
		return whatsmeow.SendResponse{ID: extra.ID, Chat: wireChat, Sender: d.ownerLID, Timestamp: time.Unix(1700000000, 0)}, nil
	}
	var captured *events.Message
	d.put = func(m *events.Message, _ string) error { captured = m; return nil }
	result, err := sendText(context.Background(), textFixture(), d)
	if err != nil {
		t.Fatal("verified PN to LID receipt rejected:", err)
	}
	if calls != 1 || captured == nil {
		t.Fatal("send/capture count", calls, captured)
	}
	if captured.Info.Chat != wireChat || captured.Info.Sender != d.ownerLID || captured.Info.RecipientAlt != requested || captured.Info.SenderAlt != d.owner {
		t.Fatal("capture lost actual wire/alternate identities", captured.Info.MessageSource)
	}
	if got := result.(map[string]any)["provider_message_id"]; got != "true_"+operationsChat+"_TESTSTANZA" {
		t.Fatal("public requested target ID changed", got)
	}
	ref, ok := result.(map[string]any)["archive_reference"].(map[string]any)
	if !ok || ref["provider_chat_id"] != "999900001111@lid" || ref["provider_message_id"] != LegacyMessageID(captured) {
		t.Fatal("missing exact wire archive reference", result)
	}
	if LegacyMessageID(captured) != "true_999900001111@lid_TESTSTANZA" {
		t.Fatal("wire archive namespace rewritten")
	}
}

func TestNativeSendGroupReceiptPreservesPublicIDAndWireSender(t *testing.T) {
	d := sendFake(t)
	d.ownerLID = types.NewJID("999900002222", types.HiddenUserServer)
	r := textFixture()
	r.Chat = operationsGroup
	d.lidForPN = func(context.Context, types.JID) (types.JID, error) {
		t.Fatal("group alias lookup")
		return types.EmptyJID, nil
	}
	d.send = func(_ context.Context, to types.JID, _ *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
		return whatsmeow.SendResponse{ID: extra.ID, Chat: to, Sender: d.ownerLID, Timestamp: time.Unix(1700000000, 0)}, nil
	}
	var captured *events.Message
	d.put = func(m *events.Message, _ string) error { captured = m; return nil }
	result, err := sendText(context.Background(), r, d)
	if err != nil {
		t.Fatal(err)
	}
	got := result.(map[string]any)
	if got["provider_message_id"] != "true_"+operationsGroup+"_TESTSTANZA_"+LegacyChatID(d.owner) {
		t.Fatal("public group receipt changed", got)
	}
	ref, ok := got["archive_reference"].(map[string]any)
	if !ok || ref["provider_chat_id"] != operationsGroup || ref["provider_message_id"] != "true_"+operationsGroup+"_TESTSTANZA_"+LegacyChatID(d.ownerLID) ||
		captured.Info.Sender != d.ownerLID || captured.Info.SenderAlt != d.owner {
		t.Fatal("wire group sender evidence lost", got, captured)
	}
}

func TestNativeSendRejectsUnprovenReceiptIdentitiesOnce(t *testing.T) {
	for _, mode := range []string{"no_mapping", "different_lid", "reverse_mismatch", "mapping_error", "same_digits", "group_alias", "lid_to_pn", "unknown_sender", "missing_sender", "missing_chat"} {
		t.Run(mode, func(t *testing.T) {
			d := sendFake(t)
			requested, _ := ParseChatID(operationsChat)
			wireChat := types.NewJID("999900001111", types.HiddenUserServer)
			d.ownerLID = types.NewJID("999900002222", types.HiddenUserServer)
			d.lidForPN = func(context.Context, types.JID) (types.JID, error) { return wireChat, nil }
			d.pnForLID = func(context.Context, types.JID) (types.JID, error) { return requested, nil }
			r := textFixture()
			sender := d.ownerLID
			switch mode {
			case "no_mapping":
				d.lidForPN = nil
			case "different_lid":
				d.lidForPN = func(context.Context, types.JID) (types.JID, error) {
					return types.NewJID("999900003333", types.HiddenUserServer), nil
				}
			case "reverse_mismatch":
				d.pnForLID = func(context.Context, types.JID) (types.JID, error) { return d.owner, nil }
			case "mapping_error":
				d.pnForLID = func(context.Context, types.JID) (types.JID, error) { return types.EmptyJID, errors.New("read failure") }
			case "same_digits":
				wireChat = types.NewJID(requested.User, types.HiddenUserServer)
				d.lidForPN = nil
			case "group_alias":
				r.Chat = operationsGroup
			case "lid_to_pn":
				r.Chat = LegacyChatID(wireChat)
				wireChat = requested
			case "unknown_sender":
				sender = types.NewJID("999900009999", types.HiddenUserServer)
				wireChat = requested
			case "missing_sender":
				sender = types.EmptyJID
				wireChat = requested
			case "missing_chat":
				wireChat = types.EmptyJID
			}
			calls, puts := 0, 0
			d.send = func(_ context.Context, _ types.JID, _ *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
				calls++
				return whatsmeow.SendResponse{ID: extra.ID, Chat: wireChat, Sender: sender, Timestamp: time.Unix(1700000000, 0)}, nil
			}
			d.put = func(*events.Message, string) error { puts++; return nil }
			_, err := sendText(context.Background(), r, d)
			requireError(t, err, "send_receipt_mismatch")
			if calls != 1 || puts != 0 {
				t.Fatalf("ambiguous send retried/captured: calls=%d puts=%d", calls, puts)
			}
		})
	}
}

func TestNativeSendWireEchoKeepsOneArchiveIdentity(t *testing.T) {
	d := sendFake(t)
	s := openTestStore(t)
	s.Owner = d.owner
	s.OwnerLID = types.NewJID("999900002222", types.HiddenUserServer)
	d.ownerLID = s.OwnerLID
	requested, _ := ParseChatID(operationsChat)
	wireChat := types.NewJID("999900001111", types.HiddenUserServer)
	d.lidForPN = func(context.Context, types.JID) (types.JID, error) { return wireChat, nil }
	d.pnForLID = func(context.Context, types.JID) (types.JID, error) { return requested, nil }
	d.put = s.Put
	d.send = func(_ context.Context, _ types.JID, _ *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
		return whatsmeow.SendResponse{ID: extra.ID, Chat: wireChat, Sender: d.ownerLID, Timestamp: time.Unix(1700000000, 0)}, nil
	}
	if _, err := sendText(context.Background(), textFixture(), d); err != nil {
		t.Fatal(err)
	}
	// Another own device can enrich the raw observation; its namespace and stanza
	// still identify the same archive message rather than a second PN copy.
	echo := operationMessage(LegacyChatID(wireChat), "TESTSTANZA", true, 1700000000)
	echo.Info.Sender = d.ownerLID
	echo.Info.Sender.Device = 2
	echo.Info.SenderAlt = d.owner
	echo.Info.RecipientAlt = requested
	echo.Message = &waE2E.Message{Conversation: proto.String(textFixture().Text)}
	echo.RawMessage = echo.Message
	if err := s.Put(echo, "live"); err != nil {
		t.Fatal(err)
	}
	rows, err := s.Page(LegacyChatID(wireChat), 100, nil)
	if err != nil || len(rows) != 1 {
		t.Fatalf("wire echo duplicated: %v rows=%d", err, len(rows))
	}
	rows, err = s.Page(operationsChat, 100, nil)
	if err != nil || len(rows) != 0 {
		t.Fatalf("invented PN archive copy: %v rows=%d", err, len(rows))
	}
}
