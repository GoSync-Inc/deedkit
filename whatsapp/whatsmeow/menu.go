package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strings"
	"unicode"
	"unicode/utf8"

	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"

	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
)

// A WhatsApp bot menu (list, buttons, native-flow buttons, template buttons)
// and the reply that chooses from it are archived as readable text: the menu's
// options with the IDs a choice answers with, and the chosen option with its
// ID. The text is sender-controlled data; control characters become spaces so
// an option cannot fake a line of its own. Link query strings and fragments are
// dropped and copy-button payloads are not read: they may carry tokens or
// codes. A WhatsApp Flow's form answers are never read. Nothing here sends,
// opens or fetches anything.

const (
	maxMenuOptions = 100
	maxMenuText    = 256
	maxMenuID      = 256
	maxMenuParams  = 16384
	// Native-flow parameter bytes decoded per message, all buttons together.
	maxMenuParamsTotal = 65536
	// One work budget per message: sections, buttons and rows examined, also
	// past the options kept, and native-flow parameter objects decoded.
	maxMenuScan = 1000
	// Room kept for the omission line at the end of a cut menu.
	menuNoticeReserve = 64
)

type menuOption struct {
	title, description, id, link, call string
	// reply names the structured reply that answers this option (ANYM-837);
	// empty for a link, call, copy, Flow, payment or unreadable button.
	reply string
	// idNote marks an ID the archive shows but a choice cannot name (ANYM-837):
	// "native flow" for a native-flow unit inside a buttons menu. optionLine adds
	// "inexact" itself for an ID the shown form does not keep losslessly.
	idNote string
	// A hydrated template quick reply's explicit Index and its position among
	// all buttons of the template.
	index    uint32
	position int
}

type menuSection struct {
	title   string
	options []menuOption
}

// menuCollector keeps the first maxMenuOptions options in source order and
// counts the rest within the scan budget.
type menuCollector struct {
	sections []menuSection
	kept     int
	options  int  // options examined, kept or not
	seen     int  // budget spent: sections and options
	params   int  // native-flow parameter bytes decoded
	cut      bool // the scan budget ended before the menu did
	// IDs of the options examined past maxMenuOptions (at most maxMenuScan), so a
	// choice can prove its ID is unique in the whole menu, not the kept prefix.
	overflowIDs []string
	// note, when set, makes every added option unanswerable with this idNote.
	note string
}

func (c *menuCollector) spend() bool {
	if c.seen >= maxMenuScan {
		c.cut = true
		return false
	}
	c.seen++
	return true
}

func (c *menuCollector) section(title string) bool {
	if !c.spend() {
		return false
	}
	c.sections = append(c.sections, menuSection{title: title})
	return true
}

// add appends to the current section; false once the scan budget is spent.
func (c *menuCollector) add(o menuOption) bool {
	if !c.spend() {
		return false
	}
	if len(c.sections) == 0 {
		c.sections = append(c.sections, menuSection{})
	}
	c.options++
	if c.note != "" {
		o.reply, o.idNote = "", c.note
	}
	if c.kept < maxMenuOptions {
		last := &c.sections[len(c.sections)-1]
		last.options = append(last.options, o)
		c.kept++
	} else if o.id != "" {
		c.overflowIDs = append(c.overflowIDs, o.id)
	}
	return true
}

func menuClean(s string, limit int) string {
	var b strings.Builder
	n := 0
	for _, r := range s {
		if n >= limit {
			break
		}
		if r == utf8.RuneError || unicode.IsControl(r) || unicode.Is(unicode.Cf, r) || unicode.Is(unicode.Zl, r) || unicode.Is(unicode.Zp, r) {
			r = ' '
		}
		b.WriteRune(r)
		n++
	}
	return strings.Join(strings.Fields(b.String()), " ")
}

// menuBrackets keeps the archive's markers unforgeable: no sender text shown in
// a menu block holds a square bracket, so " [id: " and its kin are ours alone.
var menuBrackets = strings.NewReplacer("[", "(", "]", ")")

// menuShow is the form a menu block shows of sender text (ANYM-837).
func menuShow(s string, limit int) string {
	return menuBrackets.Replace(menuClean(s, limit))
}

// menuLabel is an option's shown label, the one a choice confirms.
func menuLabel(o menuOption) string {
	return joinNonEmpty(" - ", menuShow(o.title, maxMenuText), menuShow(o.description, maxMenuText))
}

// menuLink keeps a web link's scheme, host and path only.
func menuLink(raw string) string {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" {
		return ""
	}
	return menuShow((&url.URL{Scheme: u.Scheme, Host: u.Host, Path: u.Path}).String(), maxMenuText)
}

func joinNonEmpty(sep string, parts ...string) string {
	kept := parts[:0:0]
	for _, p := range parts {
		if p != "" {
			kept = append(kept, p)
		}
	}
	return strings.Join(kept, sep)
}

func optionLine(i int, o menuOption) string {
	line := menuLabel(o)
	if line == "" {
		line = "(no label)"
	}
	if id := menuShow(o.id, maxMenuID); id != "" {
		// Only an ID shown exactly as the bot sent it gets the plain marker the
		// archive side accepts (menu_choice.WITH_ID); any other form is shown
		// for reading but cannot be chosen (ANYM-837).
		note := o.idNote
		if id != o.id {
			note = "inexact"
		}
		if note != "" {
			line += " [id (" + note + "): " + id + "]"
		} else {
			line += " [id: " + id + "]"
		}
	}
	if o.link != "" {
		line += " [link: " + o.link + "]"
	}
	if c := menuShow(o.call, maxMenuText); c != "" {
		line += " [call: " + c + "]"
	}
	return fmt.Sprintf("\n%d. %s", i, line)
}

// withMenu appends the rendered menu to a message's text so that the whole body
// stays within maxEnvelopeBody: only complete option lines are kept, and a cut
// menu ends with a line saying how many options it does not show.
func withMenu(text, label string, c *menuCollector) string {
	text = strings.TrimSpace(text)
	if c.kept == 0 {
		return text
	}
	budget := maxEnvelopeBody - menuNoticeReserve
	if text != "" {
		budget -= utf8.RuneCountInString(text) + 2 // joinText's separator
	}
	var b strings.Builder
	used := 0
	write := func(s string) bool {
		n := utf8.RuneCountInString(s)
		if used+n > budget {
			return false
		}
		b.WriteString(s)
		used += n
		return true
	}
	// "Menu options" marks this renderer: its [id: X] lines are exact and its
	// block holds no sender bracket. An older "Menu" block cannot be answered.
	header := "Menu options"
	if label = menuShow(label, maxMenuText); label != "" {
		header += " (" + label + ")"
	}
	shown, i := 0, 0
	full := write(header + ":")
	for _, s := range c.sections {
		if !full {
			break
		}
		if len(s.options) == 0 {
			continue
		}
		if t := menuShow(s.title, maxMenuText); t != "" && !write("\n"+t+":") {
			full = false
			break
		}
		for _, o := range s.options {
			i++
			if !write(optionLine(i, o)) {
				full = false
				break
			}
			shown++
		}
		if !full {
			break
		}
	}
	omitted := c.options - shown
	if shown == 0 {
		// Nothing fits after the text: the text gives up room for the notice.
		notice := fmt.Sprintf("(a menu of %d options is not shown: the message is too long)", c.options)
		if c.cut {
			notice = fmt.Sprintf("(a menu of at least %d options is not shown: the message is too long)", c.options)
		}
		room := maxEnvelopeBody - utf8.RuneCountInString(notice) - 2
		if r := []rune(text); len(r) > room {
			text = strings.TrimSpace(string(r[:room]))
		}
		return joinText(text, notice)
	}
	switch {
	case c.cut && omitted > 0:
		fmt.Fprintf(&b, "\n(at least %d more options not shown)", omitted)
	case c.cut:
		b.WriteString("\n(more options not shown)")
	case omitted > 0:
		fmt.Fprintf(&b, "\n(%d more options not shown)", omitted)
	}
	return joinText(text, b.String())
}

func listMenu(text string, v *waE2E.ListMessage) string {
	return withMenu(text, v.GetButtonText(), listOptions(v))
}

func listOptions(v *waE2E.ListMessage) *menuCollector {
	c := &menuCollector{}
	for _, s := range v.GetSections() {
		if !c.section(s.GetTitle()) {
			break
		}
		for _, r := range s.GetRows() {
			if !c.add(menuOption{title: r.GetTitle(), description: r.GetDescription(), id: r.GetRowID(), reply: "list_row"}) {
				break
			}
		}
		if c.cut {
			break
		}
	}
	return c
}

func buttonsMenu(text string, v *waE2E.ButtonsMessage) string {
	return withMenu(text, "", buttonsOptions(v))
}

func buttonsOptions(v *waE2E.ButtonsMessage) *menuCollector {
	c := &menuCollector{}
	for _, btn := range v.GetButtons() {
		if c.cut {
			break
		}
		// The button's own kind decides, before any display text: a native-flow
		// button (payment, Flow, quick reply, ...) is never a plain reply button,
		// and its units are not answerable inside a buttons menu (ANYM-837).
		if info := btn.GetNativeFlowInfo(); info != nil || btn.GetType() == waE2E.ButtonsMessage_Button_NATIVE_FLOW {
			c.note = "native flow"
			if info == nil {
				c.add(menuOption{title: joinNonEmpty(" ", menuClean(btn.GetButtonText().GetDisplayText(), maxMenuText), "(native flow button)")})
			} else {
				nativeFlowButton(c, info.GetName(), info.GetParamsJSON())
			}
			c.note = ""
			continue
		}
		c.add(menuOption{title: btn.GetButtonText().GetDisplayText(), id: btn.GetButtonID(), reply: "button"})
	}
	return c
}

type nativeFlowParams struct {
	DisplayText string `json:"display_text"`
	ID          string `json:"id"`
	URL         string `json:"url"`
	Phone       string `json:"phone_number"`
	Title       string `json:"title"`
	Sections    []struct {
		Title string `json:"title"`
		Rows  []struct {
			Header      string `json:"header"`
			Title       string `json:"title"`
			Description string `json:"description"`
			ID          string `json:"id"`
		} `json:"rows"`
	} `json:"sections"`
}

// nativeFlowButton decodes one native-flow button's parameters once and adds
// its options in source order. single_select carries a whole list.
func nativeFlowButton(c *menuCollector, name, raw string) {
	var p nativeFlowParams
	if c.params += len(raw); c.params > maxMenuParamsTotal {
		c.cut = true
		return
	}
	if raw == "" || len(raw) > maxMenuParams || json.Unmarshal([]byte(raw), &p) != nil {
		c.add(menuOption{title: "(" + menuClean(name, 64) + " button)"})
		return
	}
	switch name {
	case "single_select":
		if !c.section(p.Title) {
			return
		}
		for _, s := range p.Sections {
			// Inner sections spend the budget like list sections.
			if !c.spend() {
				return
			}
			st := menuClean(s.Title, maxMenuText)
			for _, r := range s.Rows {
				title := joinNonEmpty(" ", menuClean(r.Header, maxMenuText), menuClean(r.Title, maxMenuText))
				if !c.add(menuOption{title: joinNonEmpty(": ", st, title), description: r.Description, id: r.ID, reply: "native_flow_single_select"}) {
					return
				}
			}
		}
		// Buttons after a selection list start an untitled section of their
		// own; bounded, since each selection list spent a unit for its section.
		c.sections = append(c.sections, menuSection{})
	case "cta_url":
		c.add(menuOption{title: p.DisplayText, link: menuLink(p.URL)})
	case "cta_call":
		c.add(menuOption{title: p.DisplayText, call: p.Phone})
	case "cta_copy":
		// The copied code is not read: it may be a credential.
		c.add(menuOption{title: joinNonEmpty(" ", menuClean(p.DisplayText, maxMenuText), "(copy button)")})
	case "quick_reply":
		c.add(menuOption{title: p.DisplayText, id: p.ID, reply: "native_flow_quick_reply"})
	default:
		c.add(menuOption{title: joinNonEmpty(" ", menuClean(p.DisplayText, maxMenuText), "("+menuClean(name, 64)+" button)")})
	}
}

func interactiveMenu(text string, v *waE2E.InteractiveMessage) string {
	return withMenu(text, "", interactiveOptions(v))
}

func interactiveOptions(v *waE2E.InteractiveMessage) *menuCollector {
	c := &menuCollector{}
	for _, btn := range v.GetNativeFlowMessage().GetButtons() {
		if c.cut {
			break
		}
		nativeFlowButton(c, btn.GetName(), btn.GetButtonParamsJSON())
	}
	return c
}

func templateMenu(text string, v *waE2E.TemplateMessage) string {
	return withMenu(text, "", templateOptions(v))
}

func templateOptions(v *waE2E.TemplateMessage) *menuCollector {
	if i := v.GetInteractiveMessageTemplate(); i != nil {
		return interactiveOptions(i)
	}
	h := v.GetHydratedFourRowTemplate()
	if h == nil {
		h = v.GetHydratedTemplate()
	}
	c := &menuCollector{}
	for i, btn := range h.GetHydratedButtons() {
		var ok bool
		switch {
		case btn.GetQuickReplyButton() != nil:
			ok = c.add(menuOption{title: btn.GetQuickReplyButton().GetDisplayText(), id: btn.GetQuickReplyButton().GetID(),
				reply: "template_quick_reply", index: btn.GetIndex(), position: i})
		case btn.GetUrlButton() != nil:
			ok = c.add(menuOption{title: btn.GetUrlButton().GetDisplayText(), link: menuLink(btn.GetUrlButton().GetURL())})
		case btn.GetCallButton() != nil:
			ok = c.add(menuOption{title: btn.GetCallButton().GetDisplayText(), call: btn.GetCallButton().GetPhoneNumber()})
		default:
			ok = c.spend()
		}
		if !ok {
			break
		}
	}
	return c
}

// menuOptions returns the archived menu type (provider_message_type) and the
// options of a menu message, enumerated by the same code that renders them, so
// the options an agent reads and the ones it can answer are the same.
func menuOptions(m *waE2E.Message) (string, *menuCollector) {
	// The order of NormalizeMessage's cases (capture.go).
	switch {
	case m.GetTemplateMessage() != nil:
		return "template", templateOptions(m.GetTemplateMessage())
	case m.GetInteractiveMessage() != nil:
		return "interactive", interactiveOptions(m.GetInteractiveMessage())
	case m.GetButtonsMessage() != nil:
		return "buttons", buttonsOptions(m.GetButtonsMessage())
	case m.GetListMessage() != nil:
		return "list", listOptions(m.GetListMessage())
	}
	return "", nil
}

// menuChoice is the archived text of a reply that chose a menu option.
func menuChoice(title, description, id string) string {
	return "Chose menu option: " + strings.TrimPrefix(optionLine(0, menuOption{title: title, description: description, id: id}), "\n0. ")
}

// Native-flow responses whose parameters name the chosen option. A Flow's
// response (another name) carries the user's form answers: never read.
var optionResponseNames = map[string]bool{"quick_reply": true, "single_select": true}

// menuReply reads the four reply kinds; ok is false for any other message.
func menuReply(m *waE2E.Message) (kind, body string, ok bool) {
	switch {
	case m.GetListResponseMessage() != nil:
		v := m.GetListResponseMessage()
		return "list_response", menuChoice(v.GetTitle(), v.GetDescription(), v.GetSingleSelectReply().GetSelectedRowID()), true
	case m.GetButtonsResponseMessage() != nil:
		v := m.GetButtonsResponseMessage()
		return "buttons_response", menuChoice(v.GetSelectedDisplayText(), "", v.GetSelectedButtonID()), true
	case m.GetTemplateButtonReplyMessage() != nil:
		v := m.GetTemplateButtonReplyMessage()
		return "template_reply", menuChoice(v.GetSelectedDisplayText(), "", v.GetSelectedID()), true
	case m.GetInteractiveResponseMessage() != nil:
		v := m.GetInteractiveResponseMessage()
		id := ""
		if nf := v.GetNativeFlowResponseMessage(); nf != nil && optionResponseNames[nf.GetName()] && len(nf.GetParamsJSON()) <= maxMenuParams {
			var p struct {
				ID string `json:"id"`
			}
			if json.Unmarshal([]byte(nf.GetParamsJSON()), &p) == nil {
				id = p.ID
			}
		}
		return "interactive_response", menuChoice(v.GetBody().GetText(), "", id), true
	}
	return "", "", false
}

// ANYM-837: the reply units (archived menu type / reply kind) this adapter
// answers, each built from a wire reference (docs/whatsapp-native.md, Menu
// choices). Python mirrors the menu types in outgoing.MENU_CHOICE_TYPES.
var shippedMenuReplies = map[string]bool{"list/list_row": true, "buttons/button": true}

// menuChoiceRequest is send_text's menu_choice: the option the owner confirmed,
// as archived (option ID and label as shown, the menu's provider_message_type).
type menuChoiceRequest struct {
	OptionID string `json:"option_id"`
	Label    string `json:"label"`
	MenuType string `json:"menu_type"`
}

// menuAnswer builds the structured reply to one option of the newest captured
// menu, or refuses with a code; nothing is sent here. The option is bound by its
// RAW ID, which must already be in the form the archive shows (menuShow would
// not change it), so a bot cannot swap the ID behind an unchanged shown one; the
// label is compared by its shown form and the reply carries the raw text.
func menuAnswer(menu *events.Message, req menuChoiceRequest) (*waE2E.Message, error) {
	if menu.Info.IsFromMe || menu.Message == nil {
		return nil, errors.New("menu_unavailable")
	}
	kind, c := menuOptions(menu.Message)
	if c == nil || kind != req.MenuType {
		return nil, errors.New("menu_changed")
	}
	var match []menuOption
	shownOnly := false
	for _, s := range c.sections {
		for _, o := range s.options {
			if o.id == req.OptionID {
				match = append(match, o)
			} else if o.id != "" && menuShow(o.id, maxMenuID) == req.OptionID {
				shownOnly = true
			}
		}
	}
	// Uniqueness covers the whole menu: options past the kept prefix count, and a
	// menu the scan budget cut cannot prove it (ANYM-837).
	extra := false
	for _, id := range c.overflowIDs {
		if id == req.OptionID || menuShow(id, maxMenuID) == req.OptionID {
			extra = true
		}
	}
	switch {
	case len(match) > 0 && (extra || c.cut), len(match) > 1, len(match) == 1 && shownOnly:
		return nil, errors.New("menu_option_ambiguous")
	case len(match) == 0 && shownOnly:
		return nil, errors.New("menu_option_id_unrepresentable")
	case len(match) == 0:
		return nil, errors.New("menu_changed")
	}
	o := match[0]
	if o.id == "" || menuShow(o.id, maxMenuID) != o.id {
		return nil, errors.New("menu_option_id_unrepresentable")
	}
	if label := menuLabel(o); label == "" || label != req.Label {
		return nil, errors.New("menu_changed")
	}
	if o.reply == "" || o.idNote != "" {
		return nil, errors.New("menu_option_unsupported")
	}
	if !shippedMenuReplies[kind+"/"+o.reply] {
		return nil, errors.New("menu_reply_shape_unverified")
	}
	// The menu is referenced by its ID; its content is not quoted back.
	quote := &waE2E.ContextInfo{StanzaID: proto.String(menu.Info.ID), Participant: proto.String(menu.Info.Sender.ToNonAD().String())}
	switch o.reply {
	case "list_row":
		reply := &waE2E.ListResponseMessage{Title: proto.String(o.title), ListType: waE2E.ListResponseMessage_SINGLE_SELECT.Enum(),
			SingleSelectReply: &waE2E.ListResponseMessage_SingleSelectReply{SelectedRowID: proto.String(o.id)}, ContextInfo: quote}
		if o.description != "" {
			reply.Description = proto.String(o.description)
		}
		return &waE2E.Message{ListResponseMessage: reply}, nil
	case "button":
		return &waE2E.Message{ButtonsResponseMessage: &waE2E.ButtonsResponseMessage{SelectedButtonID: proto.String(o.id),
			Response: &waE2E.ButtonsResponseMessage_SelectedDisplayText{SelectedDisplayText: o.title},
			Type:     waE2E.ButtonsResponseMessage_DISPLAY_TEXT.Enum(), ContextInfo: quote}}, nil
	}
	return nil, errors.New("menu_reply_shape_unverified")
}
