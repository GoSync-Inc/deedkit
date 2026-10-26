package main

import (
	"encoding/json"
	"os"
	"regexp"
	"strings"
	"unicode/utf8"
)

// Shared contact card block (ANYM-529), checked again by services/messaging
// envelope.contact_block. Only FN, N and TEL (with TYPE and waid) are read; the
// raw vCard is never kept and no field reaches a log line.
const (
	maxContactCards      = 20
	maxContactPhones     = 10
	maxContactText       = 256
	maxContactBlockBytes = 16 * 1024
	maxVcardBytes        = 64 * 1024
)

var (
	e164      = regexp.MustCompile(`^\+[1-9][0-9]{7,14}$`)
	waidValue = regexp.MustCompile(`^[0-9]{1,32}$`)
)

// contactCardCapture is on unless ANYMSG_CAPTURE_CONTACT_CARDS=0, the kill switch (ANYM-529).
func contactCardCapture() bool {
	return os.Getenv("ANYMSG_CAPTURE_CONTACT_CARDS") != "0"
}

type sharedContact struct {
	displayName, vcard string
}

type vcardFields struct {
	name   string
	phones []map[string]any
	cut    bool
}

// contactText bounds one display field: valid UTF-8, no control characters,
// at most maxContactText code points. It reports whether the text was cut.
func contactText(s string) (string, bool) {
	s = strings.Map(func(r rune) rune {
		if r < 0x20 || r == 0x7f {
			return ' '
		}
		return r
	}, strings.ToValidUTF8(s, "�"))
	s = strings.TrimSpace(s)
	if utf8.RuneCountInString(s) <= maxContactText {
		return s, false
	}
	return string([]rune(s)[:maxContactText]), true
}

// normalizePhone returns E.164 for an international number written with
// visual separators, else the trimmed raw value and false. No country is guessed.
func normalizePhone(raw string) (string, bool) {
	value := strings.TrimSpace(raw)
	if len(value) >= 4 && strings.EqualFold(value[:4], "tel:") {
		value = strings.TrimSpace(value[4:])
	}
	compact := strings.Map(func(r rune) rune {
		if strings.ContainsRune(" \t-.()/ ", r) {
			return -1
		}
		return r
	}, value)
	if strings.HasPrefix(compact, "00") {
		compact = "+" + compact[2:]
	}
	if e164.MatchString(compact) {
		return compact, true
	}
	return strings.TrimSpace(raw), false
}

// splitUnescaped splits on sep characters that are not backslash-escaped.
func splitUnescaped(s string, sep byte) []string {
	var parts []string
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '\\' {
			i++
		} else if s[i] == sep {
			parts = append(parts, s[start:i])
			start = i + 1
		}
	}
	return append(parts, s[start:])
}

func vcardUnescape(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '\\' && i+1 < len(s) {
			i++
			if s[i] == 'n' || s[i] == 'N' {
				b.WriteByte(' ')
			} else {
				b.WriteByte(s[i])
			}
			continue
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

// unfoldVcard joins continuation lines (RFC 6350 section 3.2): a line break
// followed by one space or tab is removed together with that character.
func unfoldVcard(text string) []string {
	text = strings.ReplaceAll(strings.ReplaceAll(text, "\r\n", "\n"), "\r", "\n")
	var lines []string
	for _, line := range strings.Split(text, "\n") {
		if (strings.HasPrefix(line, " ") || strings.HasPrefix(line, "\t")) && len(lines) > 0 {
			lines[len(lines)-1] += line[1:]
			continue
		}
		lines = append(lines, line)
	}
	return lines
}

// parseVcard reads FN, N and TEL. A vCard without BEGIN/END or with a content
// line that has no value separator is malformed (false).
func parseVcard(text string) (vcardFields, bool) {
	var fields vcardFields
	if len(text) > maxVcardBytes || !utf8.ValidString(text) {
		return fields, false
	}
	begun, ended := false, false
	fn, n := "", ""
	for _, line := range unfoldVcard(text) {
		if strings.TrimSpace(line) == "" {
			continue
		}
		colon, quoted := -1, false
		for i := 0; i < len(line); i++ {
			if line[i] == '"' {
				quoted = !quoted
			} else if line[i] == ':' && !quoted {
				colon = i
				break
			}
		}
		if colon <= 0 {
			return fields, false
		}
		params := strings.Split(line[:colon], ";")
		name := strings.ToUpper(params[0])
		if dot := strings.LastIndexByte(name, '.'); dot >= 0 {
			name = name[dot+1:]
		}
		value := line[colon+1:]
		switch {
		case name == "BEGIN" && strings.EqualFold(strings.TrimSpace(value), "VCARD"):
			begun = true
		case name == "END" && strings.EqualFold(strings.TrimSpace(value), "VCARD"):
			ended = begun
		case !begun || ended:
		case name == "FN" && fn == "":
			fn = vcardUnescape(value)
		case name == "N" && n == "":
			// family;given;additional;prefix;suffix, shown as prefix given additional family suffix.
			parts := splitUnescaped(value, ';')
			for len(parts) < 5 {
				parts = append(parts, "")
			}
			var words []string
			for _, i := range []int{3, 1, 2, 0, 4} {
				if part := strings.TrimSpace(strings.ReplaceAll(vcardUnescape(strings.ReplaceAll(parts[i], `\,`, "\x00")), ",", " ")); part != "" {
					words = append(words, strings.ReplaceAll(part, "\x00", ","))
				}
			}
			n = strings.Join(words, " ")
		case name == "TEL":
			raw := strings.TrimSpace(vcardUnescape(value))
			if raw == "" {
				continue
			}
			if len(fields.phones) == maxContactPhones {
				fields.cut = true
				continue
			}
			var types []string
			waid := ""
			for _, param := range params[1:] {
				key, val, named := strings.Cut(param, "=")
				switch {
				case named && strings.EqualFold(key, "waid"):
					waid = strings.Trim(val, `"`)
				case named && strings.EqualFold(key, "type"):
					for _, t := range strings.Split(strings.Trim(val, `"`), ",") {
						types = append(types, strings.ToLower(strings.TrimSpace(t)))
					}
				case !named && param != "":
					types = append(types, strings.ToLower(strings.TrimSpace(param))) // vCard 2.1 bare type
				}
			}
			number, normalized := normalizePhone(raw)
			number, cut := contactText(number)
			fields.cut = fields.cut || cut
			phone := map[string]any{"number": number, "normalized": normalized}
			var seen []string
			for _, t := range types {
				if t != "" && !containsString(seen, t) {
					seen = append(seen, t)
				}
			}
			if len(seen) > 0 {
				kind, cut := contactText(strings.Join(seen, ","))
				fields.cut = fields.cut || cut
				phone["type"] = kind
			}
			if waidValue.MatchString(waid) {
				phone["whatsapp_id"] = waid
			}
			if number != "" {
				fields.phones = append(fields.phones, phone)
			}
		}
	}
	if !begun || !ended {
		return vcardFields{}, false
	}
	fields.name = fn
	if strings.TrimSpace(fields.name) == "" {
		fields.name = n
	}
	return fields, true
}

func containsString(values []string, value string) bool {
	for _, v := range values {
		if v == value {
			return true
		}
	}
	return false
}

// contactBlock builds the bounded block for the shared cards. A malformed card
// keeps its display name and makes the status parse_failed; the message itself
// is always captured.
func contactBlock(cards []sharedContact) map[string]any {
	contacts := []any{}
	failed, cut := false, len(cards) > maxContactCards
	if cut {
		cards = cards[:maxContactCards]
	}
	for _, card := range cards {
		name, nameCut := contactText(card.displayName)
		cut = cut || nameCut
		fields, ok := parseVcard(card.vcard)
		if !ok {
			failed = true
			if name != "" {
				contacts = append(contacts, map[string]any{"name": name, "phones": []any{}})
			}
			continue
		}
		cut = cut || fields.cut
		vcardName, vcardCut := contactText(fields.name)
		cut = cut || vcardCut
		if name == "" {
			name = vcardName
		}
		phones := make([]any, 0, len(fields.phones))
		for _, phone := range fields.phones {
			phones = append(phones, phone)
		}
		if name == "" && len(phones) == 0 {
			continue
		}
		contact := map[string]any{"name": name, "phones": phones}
		if vcardName != "" && vcardName != name {
			contact["vcard_name"] = vcardName
		}
		contacts = append(contacts, contact)
	}
	block := map[string]any{"contacts": contacts}
	// Go escapes more than Python's canonical JSON, so this size never undercounts.
	for len(contacts) > 0 {
		block["contacts"] = contacts
		encoded, _ := json.Marshal(block)
		if len(encoded)+len(`,"status":"parse_failed"`) <= maxContactBlockBytes {
			break
		}
		contacts, cut = contacts[:len(contacts)-1], true
	}
	block["contacts"] = contacts
	switch {
	case failed:
		block["status"] = "parse_failed"
	case cut:
		block["status"] = "truncated"
	default:
		block["status"] = "parsed"
	}
	return block
}
