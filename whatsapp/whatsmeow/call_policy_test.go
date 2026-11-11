package main

import (
	"testing"
	"time"

	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	waHistorySync "go.mau.fi/whatsmeow/proto/waHistorySync"
	waSyncAction "go.mau.fi/whatsmeow/proto/waSyncAction"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

// spec: ANYM-923
func TestIncomingCallEndHonorsCurrentChatExclusion(t *testing.T) {
	for _, event := range []any{
		&events.CallAccept{BasicCallMeta: callMeta("EXCLUDED", 1700001030)},
		&events.CallReject{BasicCallMeta: callMeta("EXCLUDED", 1700001030)},
		&events.CallTerminate{BasicCallMeta: callMeta("EXCLUDED", 1700001030)},
	} {
		p := skipProvider(t)
		if !p.handleCallEvent(callOffer("EXCLUDED", 1700001000, "audio")) {
			t.Fatal("initial offer refused")
		}
		savePolicy(t, p, time.Now().Add(time.Minute), Policy{
			Enabled: true, DiscoverChats: true,
			IncludeChats: []string{}, ExcludeChats: []string{LegacyChatID(caller)},
		})
		if !p.handleCallEvent(event) {
			t.Fatalf("excluded event %T should be skipped", event)
		}
		// Re-enabling the chat must not resurrect the excluded call.
		savePolicy(t, p, time.Now().Add(time.Minute), Policy{
			Enabled: true, DiscoverChats: true,
			IncludeChats: []string{}, ExcludeChats: []string{},
		})
		if !p.handleCallEvent(callOffer("EXCLUDED", 1700001000, "audio")) ||
			!p.handleCallEvent(&events.CallTerminate{BasicCallMeta: callMeta("EXCLUDED", 1700001060)}) {
			t.Fatal("redelivery refused")
		}
		if got := len(callEntries(t, p.Capture)); got != 0 {
			t.Fatalf("%T exported a call after its chat was excluded: %d", event, got)
		}
		if got := spoolFiles(t, p.Config.EventsDirectory); got != 0 {
			t.Fatalf("%T exported excluded call evidence: %d", event, got)
		}
	}
}

// spec: ANYM-923
func TestIncomingCallEndHonorsExpiredPolicy(t *testing.T) {
	for _, reason := range []string{"policy_expired", "collector_disabled"} {
		for _, event := range []any{
			&events.CallAccept{BasicCallMeta: callMeta("EXPIRED", 1700002030)},
			&events.CallReject{BasicCallMeta: callMeta("EXPIRED", 1700002030)},
			&events.CallTerminate{BasicCallMeta: callMeta("EXPIRED", 1700002030)},
		} {
			p := skipProvider(t)
			if !p.handleCallEvent(callOffer("EXPIRED", 1700002000, "audio")) {
				t.Fatal("initial offer refused")
			}
			expires := time.Now().Add(time.Minute)
			if reason == "policy_expired" {
				expires = time.Now().Add(-time.Second)
			}
			savePolicy(t, p, expires, Policy{
				Enabled: reason != "collector_disabled", DiscoverChats: true,
				IncludeChats: []string{}, ExcludeChats: []string{},
			})
			err := p.captureCallEvent(event)
			if err == nil || err.Error() != reason {
				t.Fatalf("%T must observe %s, got %v", event, reason, err)
			}
			pending, accepted, err := p.Capture.pendingCall("EXPIRED")
			if err != nil || pending == nil || accepted {
				t.Fatalf("%T mutated pending call under %s: %v %v %v", event, reason, pending, accepted, err)
			}
			if len(callEntries(t, p.Capture)) != 0 || spoolFiles(t, p.Config.EventsDirectory) != 0 {
				t.Fatalf("%T exported a call under %s", event, reason)
			}
		}
	}
}

// spec: ANYM-923
func TestSyncedIncomingCallUsesExistingLIDChat(t *testing.T) {
	p := skipProvider(t)
	lid := types.NewJID("777777", types.HiddenUserServer)
	p.Capture.Alias = func(j types.JID) types.JID {
		if j == caller {
			return lid
		}
		if j == lid {
			return caller
		}
		return types.EmptyJID
	}
	message := fixtureMessage("EXISTING_LID", 1700003000)
	message.Info.Chat, message.Info.Sender = lid, lid
	if err := p.Capture.putOrSkip(message, "live"); err != nil {
		t.Fatal(err)
	}
	records := &waHistorySync.HistorySync{CallLogRecords: []*waSyncAction.CallLogRecord{{
		CallID: proto.String("HISTORY_ALIAS"), IsIncoming: proto.Bool(true),
		StartTime: proto.Int64(1700003100), CallCreatorJID: proto.String(caller.String()),
		CallResult: waSyncAction.CallLogRecord_MISSED.Enum(),
	}}}
	if err := p.captureHistory(records); err != nil {
		t.Fatal(err)
	}
	entries := callEntries(t, p.Capture)
	if len(entries) != 1 {
		t.Fatalf("expected one call entry, got %d", len(entries))
	}
	event, _ := callOf(t, entries[0])
	if event["chatId"] != LegacyChatID(lid) {
		t.Fatalf("synced call split from existing LID conversation: got %v, want %s", event["chatId"], LegacyChatID(lid))
	}
}

// spec: ANYM-923
func TestSyncedIncomingCallUsesExistingPhoneChat(t *testing.T) {
	p := skipProvider(t)
	lid := types.NewJID("777777", types.HiddenUserServer)
	p.Capture.Alias = func(j types.JID) types.JID {
		if j == lid {
			return caller
		}
		return types.EmptyJID
	}
	message := fixtureMessage("EXISTING_PHONE", 1700003000)
	message.Info.Chat, message.Info.Sender = caller, caller
	if err := p.Capture.putOrSkip(message, "live"); err != nil {
		t.Fatal(err)
	}
	if err := p.captureHistory(callLogHistory(lid, "HISTORY_PHONE_ALIAS", 1700003100, waE2E.CallLogMessage_MISSED, false, 0, false)); err != nil {
		t.Fatal(err)
	}
	entries := callEntries(t, p.Capture)
	if len(entries) != 1 {
		t.Fatalf("expected one call entry, got %d", len(entries))
	}
	event, _ := callOf(t, entries[0])
	requireFields(t, event, map[string]any{"chatId": LegacyChatID(caller), "senderId": LegacyChatID(caller)})
}

// spec: ANYM-923
func TestSyncedIncomingCallChecksCanonicalChatPolicy(t *testing.T) {
	for _, allowed := range []bool{true, false} {
		p := skipProvider(t)
		lid := types.NewJID("777777", types.HiddenUserServer)
		p.Capture.Alias = func(j types.JID) types.JID {
			if j == caller {
				return lid
			}
			return types.EmptyJID
		}
		message := fixtureMessage("CANONICAL_POLICY", 1700003000)
		message.Info.Chat, message.Info.Sender = lid, lid
		if err := p.Capture.putOrSkip(message, "live"); err != nil {
			t.Fatal(err)
		}
		policy := Policy{Enabled: true, IncludeChats: []string{LegacyChatID(lid)}, ExcludeChats: []string{}}
		if !allowed {
			policy.DiscoverChats = true
			policy.ExcludeChats = []string{LegacyChatID(lid)}
		}
		savePolicy(t, p, time.Now().Add(time.Minute), policy)
		if err := p.captureHistory(&waHistorySync.HistorySync{CallLogRecords: []*waSyncAction.CallLogRecord{{
			CallID: proto.String("CANONICAL_POLICY"), IsIncoming: proto.Bool(true),
			StartTime: proto.Int64(1700003100), CallCreatorJID: proto.String(caller.String()),
			CallResult: waSyncAction.CallLogRecord_MISSED.Enum(),
		}}}); err != nil {
			t.Fatal(err)
		}
		entries := callEntries(t, p.Capture)
		if allowed {
			if len(entries) != 1 {
				t.Fatalf("canonical allowlist lost call: %d entries", len(entries))
			}
			event, _ := callOf(t, entries[0])
			requireFields(t, event, map[string]any{"chatId": LegacyChatID(lid), "senderId": LegacyChatID(lid)})
		} else if len(entries) != 0 {
			t.Fatalf("canonical exclusion leaked call: %d entries", len(entries))
		}
	}
}

// spec: ANYM-923
func TestIncomingCallAliasRemainsOneEntryAcrossLiveAndHistory(t *testing.T) {
	p := skipProvider(t)
	lid := types.NewJID("777777", types.HiddenUserServer)
	p.Capture.Alias = func(j types.JID) types.JID {
		if j == caller {
			return lid
		}
		return types.EmptyJID
	}
	message := fixtureMessage("EXISTING_ALIAS", 1700003000)
	message.Info.Chat, message.Info.Sender = lid, lid
	if err := p.Capture.putOrSkip(message, "live"); err != nil {
		t.Fatal(err)
	}
	if !p.handleCallEvent(callOffer("ALIAS_DEDUP", 1700003100, "audio")) ||
		!p.handleCallEvent(&events.CallTerminate{BasicCallMeta: callMeta("ALIAS_DEDUP", 1700003130)}) {
		t.Fatal("live call refused")
	}
	if err := p.captureHistory(callLogHistory(caller, "ALIAS_DEDUP", 1700003100, waE2E.CallLogMessage_MISSED, false, 0, false)); err != nil {
		t.Fatal(err)
	}
	if err := p.captureHistory(&waHistorySync.HistorySync{CallLogRecords: []*waSyncAction.CallLogRecord{{
		CallID: proto.String("ALIAS_DEDUP"), IsIncoming: proto.Bool(true),
		StartTime: proto.Int64(1700003100), CallCreatorJID: proto.String(caller.String()),
		CallResult: waSyncAction.CallLogRecord_MISSED.Enum(),
	}}}); err != nil {
		t.Fatal(err)
	}
	entries := callEntries(t, p.Capture)
	if len(entries) != 1 || entries[0]["origin"] != "live" {
		t.Fatalf("expected one live call entry, got %#v", entries)
	}
	event, _ := callOf(t, entries[0])
	requireFields(t, event, map[string]any{"chatId": LegacyChatID(lid), "senderId": LegacyChatID(lid)})
}
