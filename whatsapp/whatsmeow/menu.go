package main

import (
	"encoding/json"
	"fmt"
	"net/url"
	"strings"
	"unicode"

	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
)

// A WhatsApp bot menu (list, buttons, native-flow buttons, template buttons)
// and the reply that chooses from it are archived as readable text: the menu's
// options with the IDs a choice answers with, and the chosen option with its
// ID. The text is sender-controlled data; control characters become spaces so
// an option cannot fake a line of its own. Link query strings and fragments are
// dropped: they may carry tokens. Nothing here sends, opens or fetches anything.

const (
	maxMenuOptions   = 100
	maxMenuText      = 256
	maxMenuID        = 256
	maxMenuParams    = 16384
	maxMenuScanItems = 1000
)

type menuOption struct {
	title, description, id, link, call string
}

type menuSection struct {
	title   string
	options []menuOption
}

func menuClean(s string, limit int) string {
	var b strings.Builder
	n := 0
	for _, r := range s {
		if n >= limit {
			break
		}
		if r == 0xFFFD || unicode.IsControl(r) || unicode.Is(unicode.Cf, r) || unicode.Is(unicode.Zl, r) || unicode.Is(unicode.Zp, r) {
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

func renderMenu(label string, sections []menuSection) string {
	var b strings.Builder
	n, more := 0, 0
	for _, s := range sections {
		for range s.options {
			if n+more >= maxMenuOptions {
				more++
			} else {
				n++
			}
		}
	}
	if n == 0 {
		return ""
	}
	b.WriteString("Menu")
	if label = menuClean(label, maxMenuText); label != "" {
		b.WriteString(" (" + label + ")")
	}
	b.WriteString(":")
	i := 0
	for _, s := range sections {
		if i >= maxMenuOptions {
			break
		}
		if t := menuClean(s.title, maxMenuText); t != "" && len(s.options) > 0 {
			b.WriteString("\n" + t + ":")
		}
		for _, o := range s.options {
			if i >= maxMenuOptions {
				break
			}
			i++
			line := menuClean(o.title, maxMenuText)
			if d := menuClean(o.description, maxMenuText); d != "" {
				line = joinNonEmpty(" - ", line, d)
			}
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
			fmt.Fprintf(&b, "\n%d. %s", i, line)
		}
	}
	if more > 0 {
		fmt.Fprintf(&b, "\n(%d more options not shown)", more)
	}
	return b.String()
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

func listMenu(v *waE2E.ListMessage) string {
	var sections []menuSection
	scanned := 0
	for _, s := range v.GetSections() {
		sec := menuSection{title: s.GetTitle()}
		for _, r := range s.GetRows() {
			if scanned++; scanned > maxMenuScanItems {
				break
			}
			sec.options = append(sec.options, menuOption{title: r.GetTitle(), description: r.GetDescription(), id: r.GetRowID()})
		}
		sections = append(sections, sec)
		if scanned > maxMenuScanItems {
			break
		}
	}
	return renderMenu(v.GetButtonText(), sections)
}

func buttonsMenu(v *waE2E.ButtonsMessage) string {
	var options []menuOption
	for i, btn := range v.GetButtons() {
		if i >= maxMenuScanItems {
			break
		}
		if info := btn.GetNativeFlowInfo(); info != nil && btn.GetButtonText().GetDisplayText() == "" {
			options = append(options, nativeFlowOptions(info.GetName(), info.GetParamsJSON())...)
			continue
		}
		options = append(options, menuOption{title: btn.GetButtonText().GetDisplayText(), id: btn.GetButtonID()})
	}
	return renderMenu("", []menuSection{{options: options}})
}

type nativeFlowRow struct {
	Header      string `json:"header"`
	Title       string `json:"title"`
	Description string `json:"description"`
	ID          string `json:"id"`
}

type nativeFlowParams struct {
	DisplayText string `json:"display_text"`
	ID          string `json:"id"`
	URL         string `json:"url"`
	Phone       string `json:"phone_number"`
	CopyCode    string `json:"copy_code"`
	Title       string `json:"title"`
	Sections    []struct {
		Title string          `json:"title"`
		Rows  []nativeFlowRow `json:"rows"`
	} `json:"sections"`
}

func parseNativeFlow(raw string) (nativeFlowParams, bool) {
	var p nativeFlowParams
	if raw == "" || len(raw) > maxMenuParams || json.Unmarshal([]byte(raw), &p) != nil {
		return p, false
	}
	return p, true
}

// nativeFlowOptions reads one native-flow button. single_select carries a whole
// list; other names are one option each.
func nativeFlowOptions(name, raw string) []menuOption {
	p, ok := parseNativeFlow(raw)
	if !ok {
		return []menuOption{{title: "(" + menuClean(name, 64) + " button)"}}
	}
	switch name {
	case "single_select":
		var out []menuOption
		for _, s := range p.Sections {
			for _, r := range s.Rows {
				if len(out) >= maxMenuScanItems {
					return out
				}
				title := joinNonEmpty(" ", menuClean(r.Header, maxMenuText), menuClean(r.Title, maxMenuText))
				if st := menuClean(s.Title, maxMenuText); st != "" {
					title = joinNonEmpty(": ", st, title)
				}
				out = append(out, menuOption{title: title, description: r.Description, id: r.ID})
			}
		}
		return out
	case "cta_url":
		return []menuOption{{title: p.DisplayText, link: menuLink(p.URL)}}
	case "cta_call":
		return []menuOption{{title: p.DisplayText, call: p.Phone}}
	case "cta_copy":
		return []menuOption{{title: p.DisplayText, description: "copies " + menuClean(p.CopyCode, maxMenuText)}}
	}
	return []menuOption{{title: p.DisplayText, id: p.ID}}
}

func interactiveMenu(v *waE2E.InteractiveMessage) string {
	nf := v.GetNativeFlowMessage()
	if nf == nil {
		return ""
	}
	var sections []menuSection
	var plain []menuOption
	for i, btn := range nf.GetButtons() {
		if i >= maxMenuScanItems {
			break
		}
		if btn.GetName() == "single_select" {
			p, _ := parseNativeFlow(btn.GetButtonParamsJSON())
			sections = append(sections, menuSection{title: p.Title, options: nativeFlowOptions("single_select", btn.GetButtonParamsJSON())})
			continue
		}
		plain = append(plain, nativeFlowOptions(btn.GetName(), btn.GetButtonParamsJSON())...)
	}
	return renderMenu("", append([]menuSection{{options: plain}}, sections...))
}

func templateMenu(v *waE2E.TemplateMessage) string {
	if i := v.GetInteractiveMessageTemplate(); i != nil {
		return interactiveMenu(i)
	}
	h := v.GetHydratedFourRowTemplate()
	if h == nil {
		h = v.GetHydratedTemplate()
	}
	var options []menuOption
	for i, btn := range h.GetHydratedButtons() {
		if i >= maxMenuScanItems {
			break
		}
		switch {
		case btn.GetQuickReplyButton() != nil:
			options = append(options, menuOption{title: btn.GetQuickReplyButton().GetDisplayText(), id: btn.GetQuickReplyButton().GetID()})
		case btn.GetUrlButton() != nil:
			options = append(options, menuOption{title: btn.GetUrlButton().GetDisplayText(), link: menuLink(btn.GetUrlButton().GetURL())})
		case btn.GetCallButton() != nil:
			options = append(options, menuOption{title: btn.GetCallButton().GetDisplayText(), call: btn.GetCallButton().GetPhoneNumber()})
		}
	}
	return renderMenu("", []menuSection{{options: options}})
}

// menuChoice is the archived text of a reply that chose a menu option.
func menuChoice(title, description, id string) string {
	line := joinNonEmpty(" - ", menuClean(title, maxMenuText), menuClean(description, maxMenuText))
	if line == "" {
		line = "(no label)"
	}
	if id = menuClean(id, maxMenuID); id != "" {
		line += " [id: " + id + "]"
	}
	return "Chose menu option: " + line
}

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
		// Only a quick reply's ID is read; a flow's form answers are not archived here.
		if nf := v.GetNativeFlowResponseMessage(); nf != nil && len(nf.GetParamsJSON()) <= maxMenuParams {
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
