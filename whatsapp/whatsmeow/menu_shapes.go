package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
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

// One journal row per statement, each bounded like a media_keys lookup: the
// SQLite interrupt of modernc's driver covers a statement only until its first
// row, so a statement never returns more than one.
var menuShapeTimeout = 200 * time.Millisecond

// The whole scan stops at the journal's newest row when it started, after this
// many rows or after this long, and then says it is incomplete.
var (
	menuShapeRowBudget = 500000
	menuShapeDeadline  = 2 * time.Minute
)

// menuShapeBeforeRow, when set (tests only), runs before each journal statement
// with the number of rows scanned so far.
var menuShapeBeforeRow func(scanned int)

// Only these protocol words are printed; anything else is "other", so no
// message-controlled spelling or number reaches the report.
var (
	knownFlowNames  = map[string]bool{"quick_reply": true, "single_select": true}
	knownParamKeys  = map[string]bool{"id": true, "title": true, "description": true, "display_text": true, "header": true}
	knownVersions   = map[int32]bool{1: true, 2: true, 3: true}
	knownListTypes  = map[waE2E.ListResponseMessage_ListType]bool{waE2E.ListResponseMessage_UNKNOWN: true, waE2E.ListResponseMessage_SINGLE_SELECT: true}
	knownButtonType = map[waE2E.ButtonsResponseMessage_Type]bool{waE2E.ButtonsResponseMessage_UNKNOWN: true, waE2E.ButtonsResponseMessage_DISPLAY_TEXT: true}
)

type menuShapeRow struct {
	chat string
	raw  []byte
}

// menuShapePager returns the next journal row with a raw message after seq and
// at most upto (ok false when there is none).
type menuShapePager func(ctx context.Context, after, upto int64) (row menuShapeRow, seq int64, ok bool, err error)

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
	ctx, cancel := context.WithTimeout(context.Background(), menuShapeTimeout)
	var upto sql.NullInt64
	e = db.QueryRowContext(ctx, "SELECT max(seq) FROM capture").Scan(&upto)
	cancel()
	if e != nil {
		return errors.New("menu_shapes_unavailable")
	}
	report := menuReplyShapes(journalShapePager(db), journalKeyLookup(db), upto.Int64)
	return json.NewEncoder(out).Encode(report)
}

func journalShapePager(db *sql.DB) menuShapePager {
	return func(ctx context.Context, after, upto int64) (menuShapeRow, int64, bool, error) {
		var r menuShapeRow
		var seq int64
		e := db.QueryRowContext(ctx, "SELECT seq,chat,raw FROM capture WHERE seq>? AND seq<=? AND length(raw)>0 ORDER BY seq LIMIT 1", after, upto).Scan(&seq, &r.chat, &r.raw)
		if errors.Is(e, sql.ErrNoRows) {
			return r, after, false, nil
		}
		return r, seq, e == nil, e
	}
}

func menuReplyShapes(page menuShapePager, lookup mediaKeyLookup, upto int64) *menuShapeReport {
	report := &menuShapeReport{Units: map[string]*menuShapeUnit{}}
	after := int64(0)
	stop := time.Now().Add(menuShapeDeadline)
	for {
		if report.RowsScanned >= menuShapeRowBudget || time.Now().After(stop) {
			report.Incomplete, report.IncompleteCause = true, "scan_budget"
			break
		}
		if menuShapeBeforeRow != nil {
			menuShapeBeforeRow(report.RowsScanned)
		}
		ctx, cancel := context.WithTimeout(context.Background(), menuShapeTimeout)
		r, seq, ok, e := page(ctx, after, upto)
		cancel()
		if e != nil {
			report.Incomplete, report.IncompleteCause = true, "journal_read_timeout"
			break
		}
		if !ok {
			break
		}
		after = seq
		{
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
		if !knownFlowNames[name] {
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
	hasIndex               bool   // whether the button sets Index at all
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
		return "interactive", rawInteractiveOptions(m.GetInteractiveMessage())
	case m.GetTemplateMessage() != nil:
		t := m.GetTemplateMessage()
		if i := t.GetInteractiveMessageTemplate(); i != nil {
			return "template_interactive", rawInteractiveOptions(i)
		}
		h := t.GetHydratedFourRowTemplate()
		if h == nil {
			h = t.GetHydratedTemplate()
		}
		var out []rawMenuOption
		quick := 0
		for i, b := range h.GetHydratedButtons() {
			if q := b.GetQuickReplyButton(); q != nil {
				out = append(out, rawMenuOption{id: q.GetID(), title: q.GetDisplayText(), index: b.GetIndex(), hasIndex: b.Index != nil, positionAll: i, positionQuick: quick})
				quick++
			}
		}
		return "template_hydrated", out
	}
	return "not_a_menu", nil
}

func rawInteractiveOptions(v *waE2E.InteractiveMessage) []rawMenuOption {
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
		if t := v.GetListType(); knownListTypes[t] {
			rel("list_type=" + t.String())
		} else {
			rel("list_type=other")
		}
	case m.GetButtonsResponseMessage() != nil:
		v := m.GetButtonsResponseMessage()
		selected, text = v.GetSelectedButtonID(), v.GetSelectedDisplayText()
		if t := v.GetType(); knownButtonType[t] {
			rel("buttons_type=" + t.String())
		} else {
			rel("buttons_type=other")
		}
	case m.GetTemplateButtonReplyMessage() != nil:
		v := m.GetTemplateButtonReplyMessage()
		selected, text, index, hasIndex = v.GetSelectedID(), v.GetSelectedDisplayText(), v.GetSelectedIndex(), v.SelectedIndex != nil
	case m.GetInteractiveResponseMessage() != nil:
		v := m.GetInteractiveResponseMessage()
		nf := v.GetNativeFlowResponseMessage()
		text = v.GetBody().GetText()
		if v := nf.GetVersion(); knownVersions[v] {
			rel("version=" + strconv.Itoa(int(v)))
		} else {
			rel("version=other")
		}
		var params map[string]any
		if len(nf.GetParamsJSON()) <= maxMenuParams && json.Unmarshal([]byte(nf.GetParamsJSON()), &params) == nil {
			keys := []string{}
			others := false
			var holders []string
			// Every string value is compared with the source options, under any key;
			// an unknown key is printed only as "other".
			for k, v := range params {
				name := k
				if knownParamKeys[k] {
					keys = append(keys, k)
				} else {
					others, name = true, "other"
				}
				if s, ok := v.(string); ok {
					if o := optionWithID(options, s); o != nil {
						holders = append(holders, name)
						selected = s
					}
				}
			}
			sort.Strings(keys)
			if others {
				keys = append(keys, "other")
			}
			rel("params_keys=" + strings.Join(keys, ","))
			switch {
			case len(holders) == 1:
				rel("params_key_holding_source_id=" + holders[0])
			case len(holders) > 1:
				// Several keys name source options: no derivation is claimed.
				rel("params_selection=ambiguous")
				selected = ""
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
			if !o.hasIndex {
				rel("source_index=absent")
			}
			for _, h := range []struct {
				name  string
				value int
				set   bool
			}{{"explicit_index", int(o.index), o.hasIndex}, {"position0_all", o.positionAll, true}, {"position1_all", o.positionAll + 1, true}, {"position0_quick", o.positionQuick, true}, {"position1_quick", o.positionQuick + 1, true}} {
				if h.set && int(index) == h.value {
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
