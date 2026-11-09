package main

import (
	"encoding/json"
	"fmt"
	"net/url"
	"strings"
	"unicode"
	"unicode/utf8"

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
	if c.kept < maxMenuOptions {
		last := &c.sections[len(c.sections)-1]
		last.options = append(last.options, o)
		c.kept++
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

// menuLink keeps a web link's scheme, host and path only.
func menuLink(raw string) string {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" {
		return ""
	}
	return menuClean((&url.URL{Scheme: u.Scheme, Host: u.Host, Path: u.Path}).String(), maxMenuText)
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
	line := joinNonEmpty(" - ", menuClean(o.title, maxMenuText), menuClean(o.description, maxMenuText))
	if line == "" {
		line = "(no label)"
	}
	if id := menuClean(o.id, maxMenuID); id != "" {
		line += " [id: " + id + "]"
	}
	if o.link != "" {
		line += " [link: " + o.link + "]"
	}
	if c := menuClean(o.call, maxMenuText); c != "" {
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
	header := "Menu"
	if label = menuClean(label, maxMenuText); label != "" {
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
		if t := menuClean(s.title, maxMenuText); t != "" && !write("\n"+t+":") {
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
	c := &menuCollector{}
	for _, s := range v.GetSections() {
		if !c.section(s.GetTitle()) {
			break
		}
		for _, r := range s.GetRows() {
			if !c.add(menuOption{title: r.GetTitle(), description: r.GetDescription(), id: r.GetRowID()}) {
				break
			}
		}
		if c.cut {
			break
		}
	}
	return withMenu(text, v.GetButtonText(), c)
}

func buttonsMenu(text string, v *waE2E.ButtonsMessage) string {
	c := &menuCollector{}
	for _, btn := range v.GetButtons() {
		if c.cut {
			break
		}
		if info := btn.GetNativeFlowInfo(); info != nil && btn.GetButtonText().GetDisplayText() == "" {
			nativeFlowButton(c, info.GetName(), info.GetParamsJSON())
			continue
		}
		c.add(menuOption{title: btn.GetButtonText().GetDisplayText(), id: btn.GetButtonID()})
	}
	return withMenu(text, "", c)
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
				if !c.add(menuOption{title: joinNonEmpty(": ", st, title), description: r.Description, id: r.ID}) {
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
		c.add(menuOption{title: p.DisplayText, id: p.ID})
	default:
		c.add(menuOption{title: joinNonEmpty(" ", menuClean(p.DisplayText, maxMenuText), "("+menuClean(name, 64)+" button)")})
	}
}

func interactiveMenu(text string, v *waE2E.InteractiveMessage) string {
	c := &menuCollector{}
	for _, btn := range v.GetNativeFlowMessage().GetButtons() {
		if c.cut {
			break
		}
		nativeFlowButton(c, btn.GetName(), btn.GetButtonParamsJSON())
	}
	return withMenu(text, "", c)
}

func templateMenu(text string, v *waE2E.TemplateMessage) string {
	if i := v.GetInteractiveMessageTemplate(); i != nil {
		return interactiveMenu(text, i)
	}
	h := v.GetHydratedFourRowTemplate()
	if h == nil {
		h = v.GetHydratedTemplate()
	}
	c := &menuCollector{}
	for _, btn := range h.GetHydratedButtons() {
		var ok bool
		switch {
		case btn.GetQuickReplyButton() != nil:
			ok = c.add(menuOption{title: btn.GetQuickReplyButton().GetDisplayText(), id: btn.GetQuickReplyButton().GetID()})
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
	return withMenu(text, "", c)
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
