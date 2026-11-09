package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
)

// ANYM-837: before a menu reply shape is built, the shape the owner's official
// phone sends is read from the capture journal. `--menu-reply-shapes` scans the
// journal read-only and query-only (as --media-keys: no provider client, device
// store or session owner lock), pairs each reply the owner sent to a bot menu
// (list, buttons, template or interactive response) with the menu it quotes, and
// prints, per unit (menu kind x reply kind), RELATIONS only: field names that are
// set, the protocol response name and version, which field carries the selected
// option's ID, how a template index relates to the menu, and whether texts or the
// quoted menu are copied. No ID, label, text, chat or message identifier is printed.

// One journal page per statement, each bounded like a media_keys lookup.
const menuShapePage = 200

var menuShapeTimeout = 200 * time.Millisecond

// A protocol name is printed only in this form; anything else is "other".
var protocolName = regexp.MustCompile(`^[a-z_]{1,32}$`)

type menuShapeRow struct {
	chat string
	raw  []byte
}

// menuShapePager returns journal rows with a raw message after seq, in order.
type menuShapePager func(ctx context.Context, after int64) (rows []menuShapeRow, last int64, err error)

type menuShapeUnit struct {
	Count     int            `json:"count"`
	Fields    []string       `json:"fields"`
	Relations map[string]int `json:"relations"`
	fields    map[string]bool
}

type menuShapeReport struct {
	RowsScanned     int                       `json:"rows_scanned"`
	RowsUnreadable  int                       `json:"rows_unreadable"`
	OwnerReplies    int                       `json:"owner_replies"`
	OtherReplies    int                       `json:"other_replies"`
	Units           map[string]*menuShapeUnit `json:"units"`
	Incomplete      bool                      `json:"incomplete"`
	IncompleteCause string                    `json:"incomplete_cause,omitempty"`
}

func serveMenuReplyShapes(path string, out io.Writer) error {
	journal, e := mediaKeysJournal(path)
	if e != nil {
		if e.Error() == "media_keys_unavailable" {
			return errors.New("menu_shapes_unavailable")
		}
		return e
	}
	db, e := sql.Open("sqlite3", mediaKeysDSN(journal))
	if e != nil {
		return errors.New("menu_shapes_unavailable")
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	report := menuReplyShapes(journalShapePager(db), journalKeyLookup(db))
	return json.NewEncoder(out).Encode(report)
}

func journalShapePager(db *sql.DB) menuShapePager {
	return func(ctx context.Context, after int64) ([]menuShapeRow, int64, error) {
		rows, e := db.QueryContext(ctx, "SELECT seq,chat,raw FROM capture WHERE seq>? AND length(raw)>0 ORDER BY seq LIMIT ?", after, menuShapePage)
		if e != nil {
			return nil, after, e
		}
		defer rows.Close()
		var page []menuShapeRow
		last := after
		for rows.Next() {
			var r menuShapeRow
			if e = rows.Scan(&last, &r.chat, &r.raw); e != nil {
				return nil, after, e
			}
			page = append(page, r)
		}
		return page, last, rows.Err()
	}
}

func menuReplyShapes(page menuShapePager, lookup mediaKeyLookup) *menuShapeReport {
	report := &menuShapeReport{Units: map[string]*menuShapeUnit{}}
	after := int64(0)
	for {
		ctx, cancel := context.WithTimeout(context.Background(), menuShapeTimeout)
		rows, last, e := page(ctx, after)
		cancel()
		if e != nil {
			report.Incomplete, report.IncompleteCause = true, "journal_read_timeout"
			break
		}
		if len(rows) == 0 {
			break
		}
		after = last
		for _, r := range rows {
			report.RowsScanned++
			m, e := unmarshalMessage(r.raw)
			if e != nil || m.Message == nil {
				report.RowsUnreadable++
				continue
			}
			reply, ctxInfo := menuReplyOf(m.Message)
			if reply == "" {
				continue
			}
			if !m.Info.IsFromMe {
				report.OtherReplies++
				continue
			}
			report.OwnerReplies++
			source := menuSource(lookup, r.chat, m.Info.IsGroup, ctxInfo)
			menuShapeAdd(report, m.Message, reply, ctxInfo, source)
		}
	}
	for _, u := range report.Units {
		u.Fields = make([]string, 0, len(u.fields))
		for f := range u.fields {
			u.Fields = append(u.Fields, f)
		}
		sort.Strings(u.Fields)
	}
	return report
}

// menuReplyOf names the reply kind and returns its context info.
func menuReplyOf(m *waE2E.Message) (string, *waE2E.ContextInfo) {
	switch {
	case m.GetListResponseMessage() != nil:
		return "list_response", m.GetListResponseMessage().GetContextInfo()
	case m.GetButtonsResponseMessage() != nil:
		return "buttons_response", m.GetButtonsResponseMessage().GetContextInfo()
	case m.GetTemplateButtonReplyMessage() != nil:
		return "template_reply", m.GetTemplateButtonReplyMessage().GetContextInfo()
	case m.GetInteractiveResponseMessage() != nil:
		name := m.GetInteractiveResponseMessage().GetNativeFlowResponseMessage().GetName()
		if !protocolName.MatchString(name) {
			name = "other"
		}
		return "interactive_response:" + name, m.GetInteractiveResponseMessage().GetContextInfo()
	}
	return "", nil
}

// menuSource returns the quoted menu's newest raw capture, or nil.
func menuSource(lookup mediaKeyLookup, chat string, group bool, c *waE2E.ContextInfo) *waE2E.Message {
	stanza := c.GetStanzaID()
	if stanza == "" {
		return nil
	}
	id := "false_" + chat + "_" + stanza
	if group {
		p, e := types.ParseJID(c.GetParticipant())
		if e != nil || p.IsEmpty() {
			return nil
		}
		id += "_" + LegacyChatID(p)
	}
	ctx, cancel := context.WithTimeout(context.Background(), menuShapeTimeout)
	defer cancel()
	raw, e := lookup(ctx, chat, id)
	if e != nil || len(raw) == 0 {
		return nil
	}
	m, e := unmarshalMessage(raw)
	if e != nil {
		return nil
	}
	return m.Message
}

// One option of a raw menu, with what a reply may refer to.
type rawMenuOption struct {
	id, title, description string
	index                  uint32 // a hydrated template button's explicit Index
	positionAll            int    // position among all buttons of its menu (template)
	positionQuick          int    // position among quick replies only (template)
	name                   string // native-flow button name
}

// rawMenu returns the menu's kind and its options with raw values.
func rawMenu(m *waE2E.Message) (string, []rawMenuOption) {
	if m == nil {
		return "no_source", nil
	}
	switch {
	case m.GetListMessage() != nil:
		var out []rawMenuOption
		for _, s := range m.GetListMessage().GetSections() {
			for _, r := range s.GetRows() {
				out = append(out, rawMenuOption{id: r.GetRowID(), title: r.GetTitle(), description: r.GetDescription()})
			}
		}
		return "list", out
	case m.GetButtonsMessage() != nil:
		var out []rawMenuOption
		kind := "buttons"
		for _, b := range m.GetButtonsMessage().GetButtons() {
			if info := b.GetNativeFlowInfo(); info != nil {
				kind = "buttons_native_flow"
				out = append(out, nativeFlowOptions(info.GetName(), info.GetParamsJSON())...)
				continue
			}
			out = append(out, rawMenuOption{id: b.GetButtonID(), title: b.GetButtonText().GetDisplayText()})
		}
		return kind, out
	case m.GetInteractiveMessage() != nil:
		return "interactive", interactiveOptions(m.GetInteractiveMessage())
	case m.GetTemplateMessage() != nil:
		t := m.GetTemplateMessage()
		if i := t.GetInteractiveMessageTemplate(); i != nil {
			return "template_interactive", interactiveOptions(i)
		}
		h := t.GetHydratedFourRowTemplate()
		if h == nil {
			h = t.GetHydratedTemplate()
		}
		var out []rawMenuOption
		quick := 0
		for i, b := range h.GetHydratedButtons() {
			if q := b.GetQuickReplyButton(); q != nil {
				out = append(out, rawMenuOption{id: q.GetID(), title: q.GetDisplayText(), index: b.GetIndex(), positionAll: i, positionQuick: quick})
				quick++
			}
		}
		return "template_hydrated", out
	}
	return "not_a_menu", nil
}

func interactiveOptions(v *waE2E.InteractiveMessage) []rawMenuOption {
	var out []rawMenuOption
	for _, b := range v.GetNativeFlowMessage().GetButtons() {
		out = append(out, nativeFlowOptions(b.GetName(), b.GetButtonParamsJSON())...)
	}
	return out
}

func nativeFlowOptions(name, raw string) []rawMenuOption {
	var p nativeFlowParams
	if raw == "" || len(raw) > maxMenuParams || json.Unmarshal([]byte(raw), &p) != nil {
		return nil
	}
	switch name {
	case "quick_reply":
		return []rawMenuOption{{id: p.ID, title: p.DisplayText, name: name}}
	case "single_select":
		var out []rawMenuOption
		for _, s := range p.Sections {
			for _, r := range s.Rows {
				out = append(out, rawMenuOption{id: r.ID, title: r.Title, description: r.Description, name: name})
			}
		}
		return out
	}
	return nil
}

func menuShapeAdd(report *menuShapeReport, m *waE2E.Message, reply string, c *waE2E.ContextInfo, source *waE2E.Message) {
	kind, options := rawMenu(source)
	key := kind + "/" + reply
	u := report.Units[key]
	if u == nil {
		u = &menuShapeUnit{Relations: map[string]int{}, fields: map[string]bool{}}
		report.Units[key] = u
	}
	u.Count++
	fieldPaths(m.ProtoReflect(), "", 0, u.fields)
	rel := func(name string) { u.Relations[name]++ }
	selected, text, description := "", "", ""
	var index uint32
	hasIndex := false
	switch {
	case m.GetListResponseMessage() != nil:
		v := m.GetListResponseMessage()
		selected, text, description = v.GetSingleSelectReply().GetSelectedRowID(), v.GetTitle(), v.GetDescription()
		rel("list_type=" + strconv.Itoa(int(v.GetListType())))
	case m.GetButtonsResponseMessage() != nil:
		v := m.GetButtonsResponseMessage()
		selected, text = v.GetSelectedButtonID(), v.GetSelectedDisplayText()
		rel("buttons_type=" + strconv.Itoa(int(v.GetType())))
	case m.GetTemplateButtonReplyMessage() != nil:
		v := m.GetTemplateButtonReplyMessage()
		selected, text, index, hasIndex = v.GetSelectedID(), v.GetSelectedDisplayText(), v.GetSelectedIndex(), v.SelectedIndex != nil
	case m.GetInteractiveResponseMessage() != nil:
		v := m.GetInteractiveResponseMessage()
		nf := v.GetNativeFlowResponseMessage()
		text = v.GetBody().GetText()
		rel("version=" + strconv.Itoa(int(nf.GetVersion())))
		var params map[string]any
		if len(nf.GetParamsJSON()) <= maxMenuParams && json.Unmarshal([]byte(nf.GetParamsJSON()), &params) == nil {
			keys := make([]string, 0, len(params))
			for k := range params {
				if protocolName.MatchString(k) {
					keys = append(keys, k)
				} else {
					keys = append(keys, "other")
				}
			}
			sort.Strings(keys)
			rel("params_keys=" + strings.Join(keys, ","))
			for k, v := range params {
				if s, ok := v.(string); ok && protocolName.MatchString(k) && optionWithID(options, s) != nil {
					rel("params_key_holding_source_id=" + k)
					selected = s
				}
			}
		} else {
			rel("params_unreadable")
		}
	}
	if source == nil {
		rel("source_menu=not_in_journal")
		return
	}
	o := optionWithID(options, selected)
	if o == nil {
		rel("selected_id=no_matching_source_option")
	} else {
		rel("selected_id=source_raw_id")
		rel("text=" + relation(text, o.title, joinNonEmpty(" - ", o.title, o.description)))
		if description != "" || o.description != "" {
			rel("description=" + relation(description, o.description, ""))
		}
		if hasIndex {
			for _, h := range []struct {
				name  string
				value int
			}{{"explicit_index", int(o.index)}, {"position0_all", o.positionAll}, {"position1_all", o.positionAll + 1}, {"position0_quick", o.positionQuick}, {"position1_quick", o.positionQuick + 1}} {
				if int(index) == h.value {
					rel("selected_index=" + h.name)
				}
			}
		}
	}
	switch q := c.GetQuotedMessage(); {
	case q == nil:
		rel("quoted_message=absent")
	case proto.Equal(q, source):
		rel("quoted_message=equals_source")
	default:
		rel("quoted_message=differs_from_source")
	}
	if c.GetParticipant() != "" {
		rel("context_participant=set")
	}
}

func optionWithID(options []rawMenuOption, id string) *rawMenuOption {
	if id == "" {
		return nil
	}
	var found *rawMenuOption
	for i := range options {
		if options[i].id == id {
			if found != nil {
				return nil // ambiguous: no relation is claimed
			}
			found = &options[i]
		}
	}
	return found
}

// relation compares a reply text with the source option's title and label.
func relation(text, title, label string) string {
	switch {
	case text == "":
		return "empty"
	case text == title:
		return "source_title"
	case label != "" && text == label:
		return "source_title_dash_description"
	}
	return "other"
}

// fieldPaths records the names of set fields; a quoted message is recorded but
// not entered (it is the source menu).
func fieldPaths(m protoreflect.Message, prefix string, depth int, into map[string]bool) {
	if depth > 6 || len(into) > 200 {
		return
	}
	m.Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		path := prefix + string(fd.Name())
		into[path] = true
		if fd.Kind() == protoreflect.MessageKind && !fd.IsList() && !fd.IsMap() && fd.Name() != "quotedMessage" {
			fieldPaths(v.Message(), path+".", depth+1, into)
		}
		return true
	})
}
