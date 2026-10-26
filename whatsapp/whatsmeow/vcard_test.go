package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"

	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

const syntheticCard = "BEGIN:VCARD\r\nVERSION:3.0\r\nN:;Deskha;;;\r\nFN:Deskha\r\n" +
	"item1.TEL;type=CELL;type=VOICE;waid=6281200000001:+62 812-0000-0001\r\nitem1.X-ABLabel:Mobile\r\nEND:VCARD"

func privateContact(id string, m *waE2E.Message) *events.Message {
	chat := types.NewJID("111111", types.DefaultUserServer)
	return &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: chat, Sender: chat}, ID: id, Timestamp: time.Unix(1700000000, 0)}, Message: m, RawMessage: m}
}

func card(name, tel string) string {
	return "BEGIN:VCARD\nVERSION:3.0\nFN:" + name + "\n" + tel + "END:VCARD"
}

func contacts(t *testing.T, block map[string]any) []map[string]any {
	t.Helper()
	// The block reaches Python as JSON; check it in that form.
	data, err := json.Marshal(block)
	if err != nil {
		t.Fatal(err)
	}
	if len(data) > maxContactBlockBytes {
		t.Fatalf("block is %d bytes", len(data))
	}
	var decoded struct {
		Status   string           `json:"status"`
		Contacts []map[string]any `json:"contacts"`
	}
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	return decoded.Contacts
}

func phonesOf(t *testing.T, contact map[string]any) []any {
	t.Helper()
	phones, ok := contact["phones"].([]any)
	if !ok {
		t.Fatalf("phones missing: %#v", contact)
	}
	return phones
}

// spec: ANYM-529
func TestNativeContactCardCarriesNameE164AndWhatsAppID(t *testing.T) {
	t.Setenv("ANYMSG_CAPTURE_CONTACT_CARDS", "1")
	v, err := NormalizeMessage(privateContact("C1", &waE2E.Message{ContactMessage: &waE2E.ContactMessage{DisplayName: proto.String("Deskha"), Vcard: proto.String(syntheticCard)}}), types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	requireFields(t, v, map[string]any{"messageType": "vcard", "body": "Deskha"})
	block := v["contactCard"].(map[string]any)
	if block["status"] != "parsed" {
		t.Fatalf("status %v", block["status"])
	}
	got := contacts(t, block)
	if len(got) != 1 || got[0]["name"] != "Deskha" || got[0]["vcard_name"] != nil {
		t.Fatalf("contacts %#v", got)
	}
	phone := phonesOf(t, got[0])[0].(map[string]any)
	want := map[string]any{"number": "+6281200000001", "normalized": true, "type": "cell,voice", "whatsapp_id": "6281200000001"}
	for key, value := range want {
		if phone[key] != value {
			t.Fatalf("%s = %#v, want %#v", key, phone[key], value)
		}
	}
}

// spec: ANYM-529
func TestNativeContactsArrayFillsEveryCardAndKeepsADifferingVcardName(t *testing.T) {
	t.Setenv("ANYMSG_CAPTURE_CONTACT_CARDS", "1")
	m := &waE2E.Message{ContactsArrayMessage: &waE2E.ContactsArrayMessage{DisplayName: proto.String("2 contacts"), Contacts: []*waE2E.ContactMessage{
		{DisplayName: proto.String("Deskha"), Vcard: proto.String(card("Deskha Synthetic", "TEL;TYPE=cell:+62 812 0000 0001\n"))},
		{DisplayName: proto.String(""), Vcard: proto.String("BEGIN:VCARD\nVERSION:3.0\nN:Tester;Ann;;Dr.;\nTEL:+7 (900) 000-00-02\nEND:VCARD")},
	}}}
	v, err := NormalizeMessage(privateContact("C2", m), types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	requireFields(t, v, map[string]any{"messageType": "multi_vcard", "body": ""})
	got := contacts(t, v["contactCard"].(map[string]any))
	if len(got) != 2 || got[0]["name"] != "Deskha" || got[0]["vcard_name"] != "Deskha Synthetic" || got[1]["name"] != "Dr. Ann Tester" || got[1]["vcard_name"] != nil {
		t.Fatalf("contacts %#v", got)
	}
	if phone := phonesOf(t, got[1])[0].(map[string]any); phone["number"] != "+79000000002" || phone["whatsapp_id"] != nil || phone["type"] != nil {
		t.Fatalf("phone %#v", phone)
	}
}

// spec: ANYM-529
func TestNativeContactCardParserCases(t *testing.T) {
	cases := []struct {
		name, vcard, status string
		phones              []map[string]any
	}{
		{"folded lines", "BEGIN:VCARD\r\nVERSION:4.0\r\nFN:Desk\r\n ha\r\nTEL;TYPE=cell;waid=62812000\r\n 00001:+62812000\r\n\t00001\r\nEND:VCARD", "parsed",
			[]map[string]any{{"number": "+6281200000001", "normalized": true, "type": "cell", "whatsapp_id": "6281200000001"}}},
		{"missing waid", card("Deskha", "TEL;TYPE=CELL:+62 812-0000-0001\n"), "parsed",
			[]map[string]any{{"number": "+6281200000001", "normalized": true, "type": "cell"}}},
		{"no TEL", card("Deskha", ""), "parsed", []map[string]any{}},
		{"local format", card("Deskha", "TEL;CELL:0812-0000-0001\n"), "parsed",
			[]map[string]any{{"number": "0812-0000-0001", "normalized": false, "type": "cell"}}},
		{"international prefix and tel uri", card("Deskha", "TEL;VALUE=uri:tel:0062-812-0000-0001\n"), "parsed",
			[]map[string]any{{"number": "+6281200000001", "normalized": true}}},
		{"malformed: no BEGIN", "FN:Deskha\nTEL:+6281200000001\nEND:VCARD", "parse_failed", nil},
		{"malformed: line without value", "BEGIN:VCARD\nFN Deskha\nEND:VCARD", "parse_failed", nil},
		{"malformed: not UTF-8", "BEGIN:VCARD\nFN:\xff\nEND:VCARD", "parse_failed", nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			block := contactBlock([]sharedContact{{"Deskha", c.vcard}})
			if block["status"] != c.status {
				t.Fatalf("status %v, want %v", block["status"], c.status)
			}
			got := contacts(t, block)
			if len(got) != 1 || got[0]["name"] != "Deskha" {
				t.Fatalf("contacts %#v", got)
			}
			phones := phonesOf(t, got[0])
			if c.phones == nil {
				c.phones = []map[string]any{}
			}
			if len(phones) != len(c.phones) {
				t.Fatalf("phones %#v, want %#v", phones, c.phones)
			}
			for i, want := range c.phones {
				phone := phones[i].(map[string]any)
				if len(phone) != len(want) {
					t.Fatalf("phone %#v, want %#v", phone, want)
				}
				for key, value := range want {
					if phone[key] != value {
						t.Fatalf("%s = %#v, want %#v", key, phone[key], value)
					}
				}
			}
		})
	}
}

// spec: ANYM-529
func TestNativeMalformedContactCardKeepsTheMessage(t *testing.T) {
	t.Setenv("ANYMSG_CAPTURE_CONTACT_CARDS", "1")
	v, err := NormalizeMessage(privateContact("C3", &waE2E.Message{ContactMessage: &waE2E.ContactMessage{DisplayName: proto.String("Deskha"), Vcard: proto.String("garbage")}}), types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	requireFields(t, v, map[string]any{"messageType": "vcard", "body": "Deskha"})
	if block := v["contactCard"].(map[string]any); block["status"] != "parse_failed" {
		t.Fatalf("block %#v", block)
	}
}

// spec: ANYM-529
func TestNativeContactCardBoundsTruncate(t *testing.T) {
	long := strings.Repeat("Я", 300)
	var tels strings.Builder
	for i := 0; i < 12; i++ {
		fmt.Fprintf(&tels, "TEL;waid=62812%08d:+62812%08d\n", i, i)
	}
	many := make([]sharedContact, 25)
	for i := range many {
		many[i] = sharedContact{fmt.Sprintf("C%d", i), card("x", "TEL:+6281200000001\n")}
	}
	huge := make([]sharedContact, 20)
	for i := range huge {
		huge[i] = sharedContact{long, card(long, strings.Repeat("TEL;TYPE="+strings.Repeat("t", 300)+":"+strings.Repeat("9", 300)+"\n", 10))}
	}
	cases := map[string]struct {
		cards              []sharedContact
		contacts, phones   int
		nameRunes, maxSize int
	}{
		"contacts":   {many, 20, 1, 2, maxContactBlockBytes},
		"phones":     {[]sharedContact{{"Deskha", card("Deskha", tels.String())}}, 1, 10, 6, maxContactBlockBytes},
		"strings":    {[]sharedContact{{long, card(long, "TEL:+6281200000001\n")}}, 1, 1, 256, maxContactBlockBytes},
		"block size": {huge, -1, 10, 256, maxContactBlockBytes},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			block := contactBlock(c.cards)
			if block["status"] != "truncated" {
				t.Fatalf("status %v", block["status"])
			}
			got := contacts(t, block)
			if c.contacts >= 0 && len(got) != c.contacts || len(got) == 0 || len(got) > maxContactCards {
				t.Fatalf("%d contacts", len(got))
			}
			if n := len(phonesOf(t, got[0])); n != c.phones {
				t.Fatalf("%d phones", n)
			}
			for _, contact := range got {
				for key, value := range contact {
					if s, ok := value.(string); ok && len([]rune(s)) > maxContactText {
						t.Fatalf("%s has %d runes", key, len([]rune(s)))
					}
				}
			}
			if n := len([]rune(got[0]["name"].(string))); n != c.nameRunes {
				t.Fatalf("name has %d runes, want %d", n, c.nameRunes)
			}
		})
	}
	if len(contacts(t, contactBlock(huge))) == maxContactCards {
		t.Fatal("an oversized block kept every contact")
	}
}

// spec: ANYM-529
// The name predates the default flip (a Tiden case ID); it now checks the 0 kill switch.
func TestNativeContactCardCaptureIsOffByDefaultAndLogsNothing(t *testing.T) {
	t.Setenv("ANYMSG_CAPTURE_CONTACT_CARDS", "0")
	var logged bytes.Buffer
	previous := log.Writer()
	log.SetOutput(&logged)
	t.Cleanup(func() { log.SetOutput(previous) })
	m := &waE2E.Message{ContactMessage: &waE2E.ContactMessage{DisplayName: proto.String("Deskha"), Vcard: proto.String(syntheticCard)}}
	v, err := NormalizeMessage(privateContact("C4", m), types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := v["contactCard"]; ok {
		t.Fatal("contactCard emitted while capture is off")
	}
	t.Setenv("ANYMSG_CAPTURE_CONTACT_CARDS", "1")
	for _, vcard := range []string{syntheticCard, "BEGIN:VCARD\nTEL 6281200000001\nEND:VCARD"} {
		m.ContactMessage.Vcard = proto.String(vcard)
		if _, err := NormalizeMessage(privateContact("C5", m), types.EmptyJID); err != nil {
			t.Fatal(err)
		}
	}
	if strings.Contains(logged.String(), "81200000001") {
		t.Fatal("a contact field reached the log")
	}
}

// spec: ANYM-529
func TestNativeContactCardCaptureIsOnWithoutTheVariable(t *testing.T) {
	t.Setenv("ANYMSG_CAPTURE_CONTACT_CARDS", "")
	m := &waE2E.Message{ContactMessage: &waE2E.ContactMessage{DisplayName: proto.String("Deskha"), Vcard: proto.String(syntheticCard)}}
	v, err := NormalizeMessage(privateContact("C6", m), types.EmptyJID)
	if err != nil {
		t.Fatal(err)
	}
	if card, ok := v["contactCard"].(map[string]any); !ok || card["status"] != "parsed" {
		t.Fatalf("contactCard missing by default: %#v", v["contactCard"])
	}
}

// spec: ANYM-529
func TestNativeContactCardMatchesTheSharedParserFixture(t *testing.T) {
	raw, err := os.ReadFile("../../tests/fixtures/contact-card-parser-outputs.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Cases []struct {
			Name  string `json:"name"`
			Cards []struct {
				DisplayName string `json:"displayName"`
				Vcard       string `json:"vcard"`
			} `json:"cards"`
			Block any `json:"block"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	for _, c := range fixture.Cases {
		cards := []sharedContact{}
		for _, card := range c.Cards {
			cards = append(cards, sharedContact{card.DisplayName, card.Vcard})
		}
		data, err := json.Marshal(contactBlock(cards))
		if err != nil {
			t.Fatal(err)
		}
		var got any
		if err := json.Unmarshal(data, &got); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(got, c.Block) {
			t.Fatalf("%s: native block %s differs from the fixture", c.Name, data)
		}
	}
}
