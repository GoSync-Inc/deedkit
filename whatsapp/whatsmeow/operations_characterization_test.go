package main

// Characterization of executeOperation cases the other suites leave uncovered (refactor W0-4).
// Each test pins today's behaviour with fake dependencies; none touches a live provider. Where a
// branch looks odd the test records it as it is and does not judge it.

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

func characterizeJID(t *testing.T, id string) types.JID {
	t.Helper()
	j, err := ParseChatID(id)
	if err != nil {
		t.Fatal(err)
	}
	return j
}

func characterizeNoWrites(t *testing.T, d *operationDependencies) {
	t.Helper()
	d.send = func(context.Context, types.JID, *waE2E.Message, whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
		t.Fatal("a change was sent")
		return whatsmeow.SendResponse{}, nil
	}
	d.patch = func(context.Context, appstate.PatchInfo) error { t.Fatal("a patch was applied"); return nil }
	d.markRead = func(context.Context, []types.MessageID, time.Time, types.JID, types.JID) error {
		t.Fatal("a read receipt was sent")
		return nil
	}
	d.block = func(context.Context, types.JID, bool) error { t.Fatal("a block change was made"); return nil }
	d.status = func(context.Context, string) error { t.Fatal("the status was set"); return nil }
}

// spec: ANYM-549
func TestCharacterizeOperationGateRefusesBeforeAnyChange(t *testing.T) {
	disabled := allowedOperationPolicy()
	disabled.Enabled = false
	narrow := allowedOperationPolicy()
	narrow.OperationsEnabled = []string{"get_profile"}
	for name, tc := range map[string]struct {
		job    operationRequest
		policy func() (Policy, error)
		want   string
	}{
		"chat_operation_without_chat": {operationJob("send_location", "", map[string]any{"latitude": 1, "longitude": 2}), nil, "operation_target_invalid"},
		"unparsable_chat":             {operationJob("send_location", "not a chat", map[string]any{"latitude": 1, "longitude": 2}), nil, "operation_target_invalid"},
		"policy_unreadable": {operationJob("send_location", operationsChat, map[string]any{"latitude": 1, "longitude": 2}),
			func() (Policy, error) { return Policy{}, errors.New("unreadable") }, "operation_disabled"},
		"policy_disabled": {operationJob("send_location", operationsChat, map[string]any{"latitude": 1, "longitude": 2}),
			func() (Policy, error) { return disabled, nil }, "operation_disabled"},
		"operation_not_enabled": {operationJob("send_location", operationsChat, map[string]any{"latitude": 1, "longitude": 2}),
			func() (Policy, error) { return narrow, nil }, "operation_disabled"},
	} {
		t.Run(name, func(t *testing.T) {
			d := operationsFake(t)
			characterizeNoWrites(t, &d)
			if tc.policy != nil {
				d.policy = tc.policy
			}
			_, err := executeOperation(context.Background(), tc.job, d)
			requireError(t, err, tc.want)
		})
	}
}

// spec: ANYM-549
func TestCharacterizeListBlockedKeepsActiveAllowedEntriesAndFailsClosed(t *testing.T) {
	allowed, hidden, inactive, lid := characterizeJID(t, operationsChat), characterizeJID(t, "15557654321@c.us"), characterizeJID(t, "15558887766@c.us"), characterizeJID(t, lidFixture)
	policy := allowedOperationPolicy()
	policy.ExcludeChats = []string{LegacyChatID(hidden)}
	list := &types.Blocklist{Items: []types.BlocklistItem{{PN: allowed, Active: true}, {PN: hidden, Active: true}, {PN: inactive}, {LID: lid, Active: true}}}
	d := operationsFake(t)
	d.policy = func() (Policy, error) { return policy, nil }
	d.blocklist = func(context.Context) (*types.Blocklist, error) { return list, nil }
	r, e := executeOperation(context.Background(), operationJob("list_blocked", "", nil), d)
	data := operationResult(t, r, e, "confirmed")
	items := data["items"].([]any)
	if len(items) != 2 || items[0].(map[string]any)["id"] != operationsChat || items[1].(map[string]any)["id"] != LegacyChatID(lid) {
		t.Fatalf("blocked items %#v", items)
	}
	for _, item := range items {
		if item.(map[string]any)["is_blocked"] != true {
			t.Fatalf("not marked blocked %#v", item)
		}
	}
	if data["coverage"] != "provider_observation" || data["state"] != "observed" {
		t.Fatalf("coverage %#v", data)
	}
	for name, change := range map[string]func(*operationDependencies){
		"nil_list": func(d *operationDependencies) {
			d.blocklist = func(context.Context) (*types.Blocklist, error) { return nil, nil }
		},
		"list_error": func(d *operationDependencies) {
			d.blocklist = func(context.Context) (*types.Blocklist, error) { return nil, errors.New("boom") }
		},
		"contact_error": func(d *operationDependencies) {
			d.contact = func(context.Context, types.JID) (types.ContactInfo, error) {
				return types.ContactInfo{}, errors.New("boom")
			}
		},
	} {
		t.Run(name, func(t *testing.T) {
			d := operationsFake(t)
			d.blocklist = func(context.Context) (*types.Blocklist, error) { return list, nil }
			change(&d)
			_, err := executeOperation(context.Background(), operationJob("list_blocked", "", nil), d)
			requireError(t, err, "operation_query_failed")
		})
	}
}

// spec: ANYM-549
func TestCharacterizeChatInfoOfAPrivateChatReportsLocalSettingsAndContactName(t *testing.T) {
	d := operationsFake(t)
	d.settings = func(context.Context, types.JID) (types.LocalChatSettings, error) {
		return types.LocalChatSettings{Found: true, MutedUntil: time.Now().Add(time.Hour), Archived: true, Pinned: true}, nil
	}
	d.contact = func(context.Context, types.JID) (types.ContactInfo, error) {
		return types.ContactInfo{Found: true, PushName: "Push Name"}, nil
	}
	r, e := executeOperation(context.Background(), operationJob("chat_info", operationsChat, nil), d)
	data := operationResult(t, r, e, "confirmed")
	if data["name"] != "Push Name" || data["is_group"] != false || data["is_muted"] != true || data["is_archived"] != true || data["is_pinned"] != true {
		t.Fatalf("chat info %#v", data)
	}
	d.settings = func(context.Context, types.JID) (types.LocalChatSettings, error) {
		return types.LocalChatSettings{}, errors.New("boom")
	}
	_, err := executeOperation(context.Background(), operationJob("chat_info", operationsChat, nil), d)
	requireError(t, err, "operation_query_failed")
}

// spec: ANYM-549
func TestCharacterizeGetContactOfAnUnknownContactIsTargetUnavailable(t *testing.T) {
	d := operationsFake(t)
	d.contact = func(context.Context, types.JID) (types.ContactInfo, error) { return types.ContactInfo{}, nil }
	_, err := executeOperation(context.Background(), operationJob("get_contact", "", map[string]any{"user_id": operationsChat}), d)
	requireError(t, err, "operation_target_unavailable")
}

// spec: ANYM-549
func TestCharacterizeReactionsKeepTheLatestPerActorFromLocalCapture(t *testing.T) {
	original := operationMessage(operationsChat, "TARGET", true, 1700000000)
	reaction := func(id string, fromMe bool, target *events.Message, emoji string) *events.Message {
		m := operationMessage(operationsChat, id, fromMe, 1700000001)
		m.Message = &waE2E.Message{ReactionMessage: &waE2E.ReactionMessage{Key: capturedMessageKey(target), Text: &emoji, SenderTimestampMS: proto64(1700000001000)}}
		return m
	}
	elsewhere := operationMessage(operationsChat, "ELSEWHERE", false, 1700000000)
	d := operationsFake(t)
	d.get = func(string, string) (*events.Message, error) { return original, nil }
	d.page = func(string, int, []any) ([]*events.Message, error) {
		return []*events.Message{
			reaction("R1", false, original, "👍"), reaction("R2", false, original, ""),
			reaction("R3", true, original, "🎉"), reaction("R4", false, elsewhere, "🔥")}, nil
	}
	job := operationJob("get_reactions", operationsChat, nil)
	job.ProviderMessageID = LegacyMessageID(original)
	r, e := executeOperation(context.Background(), job, d)
	data := operationResult(t, r, e, "confirmed")
	items := data["items"].([]any)
	if len(items) != 2 {
		t.Fatalf("reactions %#v", items)
	}
	first, second := items[0].(map[string]any), items[1].(map[string]any)
	if first["emoji"] != "🎉" || first["is_by_self"] != true || first["removed"] != false ||
		second["emoji"] != "" || second["is_by_self"] != false || second["removed"] != true {
		t.Fatalf("reaction rows %#v", items)
	}
	if data["state"] != "partial" || data["coverage"] != "local_capture" || data["completeness"] != "unknown" {
		t.Fatalf("reaction coverage %#v", data)
	}
}

func proto64(v int64) *int64 { return &v }

// spec: ANYM-549
func TestCharacterizeSendContactBuildsEscapedVCardsAndNeedsAPhoneNumber(t *testing.T) {
	send := func(d *operationDependencies, wire **waE2E.Message) {
		d.send = func(_ context.Context, to types.JID, w *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
			*wire = w
			return whatsmeow.SendResponse{ID: extra.ID, Chat: to, Sender: d.owner, Timestamp: time.Unix(1700000001, 0)}, nil
		}
	}
	job := operationJob("send_contact", operationsChat, map[string]any{"contact_ids": []string{operationsChat}})
	for name, tc := range map[string]struct{ full, push, card string }{
		"escaped_full_name": {"Ann; Lee, Jr\nNote", "", `FN:Ann\; Lee\, Jr\nNote`},
		"push_name":         {"", "Push Name", "FN:Push Name"},
		"number_fallback":   {"", "", "FN:15551234567"},
	} {
		t.Run(name, func(t *testing.T) {
			d := operationsFake(t)
			var wire *waE2E.Message
			send(&d, &wire)
			d.contact = func(context.Context, types.JID) (types.ContactInfo, error) {
				return types.ContactInfo{Found: true, FullName: tc.full, PushName: tc.push}, nil
			}
			d.phone = func(_ context.Context, j types.JID) (types.JID, error) { return j, nil }
			r, e := executeOperation(context.Background(), job, d)
			operationResult(t, r, e, "confirmed")
			cards := wire.GetContactsArrayMessage().GetContacts()
			if len(cards) != 1 {
				t.Fatalf("cards %#v", cards)
			}
			if !strings.Contains(cards[0].GetVcard(), tc.card+"\r\n") || !strings.Contains(cards[0].GetVcard(), "TEL;type=CELL;waid=15551234567:+15551234567") {
				t.Fatalf("vcard %q", cards[0].GetVcard())
			}
		})
	}
	t.Run("hidden_address_without_a_phone_number", func(t *testing.T) {
		d := operationsFake(t)
		characterizeNoWrites(t, &d)
		d.phone = func(context.Context, types.JID) (types.JID, error) { return characterizeJID(t, lidFixture), nil }
		_, err := executeOperation(context.Background(), job, d)
		requireError(t, err, "operation_target_unavailable")
	})
}

// spec: ANYM-549
func TestCharacterizeSendFileModesPickTheMediaKindAndRefuseBadCombinations(t *testing.T) {
	for _, tc := range []struct {
		mode, mime string
		kind       whatsmeow.MediaType
		present    func(*waE2E.Message) bool
	}{
		{"document", "application/pdf", whatsmeow.MediaDocument, func(m *waE2E.Message) bool { return m.GetDocumentMessage() != nil }},
		{"photo", "image/png", whatsmeow.MediaImage, func(m *waE2E.Message) bool { return m.GetImageMessage() != nil }},
		{"sticker", "image/webp", whatsmeow.MediaImage, func(m *waE2E.Message) bool { return m.GetStickerMessage() != nil }},
		{"video", "video/mp4", whatsmeow.MediaVideo, func(m *waE2E.Message) bool { return m.GetVideoMessage() != nil }},
		{"audio", "audio/ogg", whatsmeow.MediaAudio, func(m *waE2E.Message) bool { return m.GetAudioMessage() != nil }},
		{"voice", "audio/ogg", whatsmeow.MediaAudio, func(m *waE2E.Message) bool { return m.GetAudioMessage() != nil }},
	} {
		t.Run(tc.mode, func(t *testing.T) {
			d := operationsFake(t)
			job, data := registeredFileFixture(t)
			job.Operation, job.ProviderChat, job.FileMime = "send_file", operationsChat, tc.mime
			job.ExpiresAt = float64(time.Now().Add(time.Hour).Unix())
			job.Parameters["mode"], _ = json.Marshal(tc.mode)
			var uploadKind whatsmeow.MediaType
			var sent *waE2E.Message
			d.upload = func(_ context.Context, _ []byte, kind whatsmeow.MediaType) (whatsmeow.UploadResponse, error) {
				uploadKind = kind
				hash := sha256.Sum256(data)
				return whatsmeow.UploadResponse{FileLength: uint64(len(data)), FileSHA256: hash[:]}, nil
			}
			d.send = func(_ context.Context, to types.JID, w *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
				sent = w
				return whatsmeow.SendResponse{ID: extra.ID, Chat: to, Sender: d.owner, Timestamp: time.Unix(1700000001, 0)}, nil
			}
			r, e := executeOperation(context.Background(), job, d)
			operationResult(t, r, e, "confirmed")
			if uploadKind != tc.kind || sent == nil || !tc.present(sent) {
				t.Fatalf("upload kind %v, message %#v", uploadKind, sent)
			}
		})
	}
	for name, tc := range map[string]struct {
		mode, mime, caption, want string
	}{
		"audio_with_caption":   {"audio", "audio/ogg", "caption", "operation_unsupported"},
		"voice_with_caption":   {"voice", "audio/ogg", "caption", "operation_unsupported"},
		"sticker_with_caption": {"sticker", "image/webp", "caption", "operation_unsupported"},
		"photo_with_pdf_mime":  {"photo", "application/pdf", "", "registered_file_invalid"},
		"sticker_not_webp":     {"sticker", "image/png", "", "registered_file_invalid"},
		"unknown_mode":         {"gif", "image/gif", "", "registered_file_invalid"},
	} {
		t.Run(name, func(t *testing.T) {
			d := operationsFake(t)
			characterizeNoWrites(t, &d)
			d.upload = func(context.Context, []byte, whatsmeow.MediaType) (whatsmeow.UploadResponse, error) {
				t.Fatal("uploaded a refused file")
				return whatsmeow.UploadResponse{}, nil
			}
			job, _ := registeredFileFixture(t)
			job.Operation, job.ProviderChat, job.FileMime = "send_file", operationsChat, tc.mime
			job.ExpiresAt = float64(time.Now().Add(time.Hour).Unix())
			job.Parameters["mode"], _ = json.Marshal(tc.mode)
			job.Parameters["caption"], _ = json.Marshal(tc.caption)
			_, err := executeOperation(context.Background(), job, d)
			requireError(t, err, tc.want)
		})
	}
}

// spec: ANYM-549
func TestCharacterizeForwardRefusesBadSourcesBeforeSending(t *testing.T) {
	viewOnce := operationMessage(operationsChat, "ONCE", false, 1700000000)
	viewOnce.IsViewOnce = true
	plain := operationMessage(operationsChat, "PLAIN", false, 1700000000)
	excluded := allowedOperationPolicy()
	excluded.ExcludeChats = []string{operationsChat}
	for name, tc := range map[string]struct {
		source  *events.Message
		chat    string
		policy  *Policy
		message string
		want    string
	}{
		"source_chat_unparsable":   {nil, "not a chat", nil, "X", "operation_invalid_parameters"},
		"source_not_captured":      {nil, operationsChat, nil, LegacyMessageID(plain), "operation_target_unavailable"},
		"source_view_once":         {viewOnce, operationsChat, nil, LegacyMessageID(viewOnce), "operation_target_invalid"},
		"source_chat_now_excluded": {plain, operationsChat, &excluded, LegacyMessageID(plain), "operation_target_invalid"},
	} {
		t.Run(name, func(t *testing.T) {
			d := operationsFake(t)
			characterizeNoWrites(t, &d)
			d.get = func(string, string) (*events.Message, error) {
				if tc.source == nil {
					return nil, errors.New("missing")
				}
				return tc.source, nil
			}
			if tc.policy != nil {
				d.policy = func() (Policy, error) { return *tc.policy, nil }
			}
			job := operationJob("forward_message", operationsGroup, nil)
			job.SourceProviderChat, job.SourceProviderMessage = tc.chat, tc.message
			_, err := executeOperation(context.Background(), job, d)
			requireError(t, err, tc.want)
		})
	}
}

// spec: ANYM-549
func TestCharacterizeMessageOperationsRefuseBadTargetsAndParametersBeforeAnyChange(t *testing.T) {
	foreign := operationMessage(operationsChat, "FOREIGN", false, 1700000000)
	own := operationMessage(operationsChat, "OWN", true, 1700000000)
	undated := operationMessage(operationsChat, "UNDATED", false, 1700000000)
	undated.Info.Timestamp = time.Time{}
	sender := characterizeJID(t, operationsGroup)
	strangerSender := operationMessage(operationsChat, "STRANGER", false, 1699999999)
	strangerSender.Info.Sender = sender
	for name, tc := range map[string]struct {
		op       string
		target   *events.Message
		params   map[string]any
		observed []*events.Message
		pageErr  error
		want     string
	}{
		"edit_without_text":         {"edit_message", own, nil, nil, nil, "operation_invalid_parameters"},
		"edit_of_a_foreign_message": {"edit_message", foreign, map[string]any{"text": "new"}, nil, nil, "operation_target_invalid"},
		"reaction_without_emoji":    {"send_reaction", own, nil, nil, nil, "operation_invalid_parameters"},
		"pin_with_zero_seconds":     {"pin_message", own, map[string]any{"seconds": 0}, nil, nil, "operation_invalid_parameters"},
		"star_of_an_uncaptured":     {"star_message", nil, nil, nil, nil, "operation_target_unavailable"},
		"mark_read_without_a_time":  {"mark_read", undated, nil, nil, nil, "operation_target_invalid"},
		"mark_read_page_failure":    {"mark_read", foreign, nil, nil, errors.New("boom"), "operation_target_unavailable"},
		"mark_read_foreign_sender":  {"mark_read", foreign, nil, []*events.Message{strangerSender}, nil, "operation_target_invalid"},
	} {
		t.Run(name, func(t *testing.T) {
			d := operationsFake(t)
			characterizeNoWrites(t, &d)
			d.get = func(string, string) (*events.Message, error) {
				if tc.target == nil {
					return nil, errors.New("missing")
				}
				return tc.target, nil
			}
			d.page = func(string, int, []any) ([]*events.Message, error) { return tc.observed, tc.pageErr }
			job := operationJob(tc.op, operationsChat, tc.params)
			if tc.target != nil {
				job.ProviderMessageID = LegacyMessageID(tc.target)
			} else {
				job.ProviderMessageID = LegacyMessageID(foreign)
			}
			_, err := executeOperation(context.Background(), job, d)
			requireError(t, err, tc.want)
		})
	}
}

// spec: ANYM-549
func TestCharacterizeAFailedChangeAfterItStartedIsUnknownNotAnError(t *testing.T) {
	target := operationMessage(operationsChat, "TARGET", false, 1700000000)
	earlier := operationMessage(operationsChat, "EARLIER", false, 1699999990)
	unknown := func(t *testing.T, d operationDependencies, job operationRequest) {
		t.Helper()
		r, e := executeOperation(context.Background(), job, d)
		if data := operationResult(t, r, e, "unknown"); data["code"] != "provider_operation_uncertain" {
			t.Fatalf("unknown result %#v", data)
		}
	}
	t.Run("mark_read_receipt", func(t *testing.T) {
		d := operationsFake(t)
		d.get = func(string, string) (*events.Message, error) { return target, nil }
		d.page = func(string, int, []any) ([]*events.Message, error) { return []*events.Message{earlier}, nil }
		d.markRead = func(context.Context, []types.MessageID, time.Time, types.JID, types.JID) error {
			return errors.New("boom")
		}
		job := operationJob("mark_read", operationsChat, nil)
		job.ProviderMessageID = LegacyMessageID(target)
		unknown(t, d, job)
	})
	for _, op := range []string{"mute_chat", "block_contact", "update_profile"} {
		t.Run(op, func(t *testing.T) {
			d := operationsFake(t)
			d.patch = func(context.Context, appstate.PatchInfo) error { return errors.New("boom") }
			d.block = func(context.Context, types.JID, bool) error { return errors.New("boom") }
			job := map[string]operationRequest{
				"mute_chat":      operationJob("mute_chat", operationsChat, map[string]any{"seconds": 3600}),
				"block_contact":  operationJob("block_contact", "", map[string]any{"user_id": operationsChat}),
				"update_profile": operationJob("update_profile", "", map[string]any{"name": "Synthetic"}),
			}[op]
			unknown(t, d, job)
		})
	}
	t.Run("mute_with_zero_seconds_is_refused_before_any_patch", func(t *testing.T) {
		d := operationsFake(t)
		characterizeNoWrites(t, &d)
		_, err := executeOperation(context.Background(), operationJob("mute_chat", operationsChat, map[string]any{"seconds": 0}), d)
		requireError(t, err, "operation_invalid_parameters")
	})
}

// spec: ANYM-242
func TestCharacterizeGroupOperationsRefuseBadParametersAndFailedSettlesBeforeAnyChange(t *testing.T) {
	group := &types.GroupInfo{JID: characterizeJID(t, operationsGroup)}
	for name, tc := range map[string]struct {
		op     string
		params map[string]any
		settle error
		want   string
	}{
		"rename_without_title":          {"rename_group", nil, nil, "operation_invalid_parameters"},
		"description_missing":           {"set_group_description", nil, nil, "operation_invalid_parameters"},
		"participant_id_malformed":      {"add_participant", map[string]any{"user_id": "not a user"}, nil, "operation_invalid_parameters"},
		"rename_after_failed_settle":    {"rename_group", map[string]any{"title": "Title"}, errors.New("stale"), "operation_target_unavailable"},
		"setting_after_failed_settle":   {"set_group_messages_admins_only", map[string]any{"enabled": true}, errors.New("stale"), "operation_target_unavailable"},
		"approve_after_failed_settle":   {"approve_join_requests", map[string]any{"user_ids": []string{operationsChat}}, errors.New("stale"), "operation_target_unavailable"},
		"approve_with_a_repeated_id":    {"approve_join_requests", map[string]any{"user_ids": []string{operationsChat, operationsChat}}, nil, "operation_invalid_parameters"},
		"create_group_repeated_invitee": {"create_group", map[string]any{"title": "Title", "user_ids": []string{operationsChat, operationsChat}}, nil, "operation_invalid_parameters"},
	} {
		t.Run(name, func(t *testing.T) {
			d := operationsFake(t)
			characterizeNoWrites(t, &d)
			d.group = func(context.Context, types.JID) (*types.GroupInfo, error) { return group, nil }
			d.groupSettle = func(context.Context) error { return tc.settle }
			d.groupName = func(context.Context, types.JID, string) error { t.Fatal("renamed"); return nil }
			d.groupSetting = func(context.Context, types.JID, string, bool) error { t.Fatal("setting changed"); return nil }
			d.joinAction = func(context.Context, types.JID, []types.JID, bool) ([]types.GroupParticipant, error) {
				t.Fatal("join request answered")
				return nil, nil
			}
			d.participants = func(context.Context, types.JID, []types.JID, whatsmeow.ParticipantChange) ([]types.GroupParticipant, error) {
				t.Fatal("participants changed")
				return nil, nil
			}
			d.createGroup = func(context.Context, whatsmeow.ReqCreateGroup) (*types.GroupInfo, error) {
				t.Fatal("group created")
				return nil, nil
			}
			chat := operationsGroup
			if tc.op == "create_group" {
				chat = ""
			}
			_, err := executeOperation(context.Background(), operationJob(tc.op, chat, tc.params), d)
			requireError(t, err, tc.want)
		})
	}
}

// spec: ANYM-549
func TestCharacterizeProfileUpdateWritesOnlyTheFieldsGiven(t *testing.T) {
	for name, tc := range map[string]struct {
		params            map[string]any
		patches, statuses int
		want              string
	}{
		"nothing_given":   {nil, 0, 0, "operation_invalid_parameters"},
		"empty_name":      {map[string]any{"name": ""}, 0, 0, "operation_invalid_parameters"},
		"overlong_name":   {map[string]any{"name": strings.Repeat("n", 65)}, 0, 0, "operation_invalid_parameters"},
		"overlong_status": {map[string]any{"status": strings.Repeat("s", 140)}, 0, 0, "operation_invalid_parameters"},
		"name_only":       {map[string]any{"name": "Synthetic"}, 1, 0, ""},
		"empty_status":    {map[string]any{"status": ""}, 0, 1, ""},
		"name_and_status": {map[string]any{"name": "Synthetic", "status": "Away"}, 1, 1, ""},
	} {
		t.Run(name, func(t *testing.T) {
			d := operationsFake(t)
			patches, statuses := 0, 0
			d.patch = func(context.Context, appstate.PatchInfo) error { patches++; return nil }
			d.status = func(context.Context, string) error { statuses++; return nil }
			r, e := executeOperation(context.Background(), operationJob("update_profile", "", tc.params), d)
			if tc.want != "" {
				requireError(t, e, tc.want)
			} else {
				operationResult(t, r, e, "submitted")
			}
			if patches != tc.patches || statuses != tc.statuses {
				t.Fatalf("patches=%d statuses=%d", patches, statuses)
			}
		})
	}
}

// spec: ANYM-549
func TestCharacterizeBlockChecksThePolicyForTheBlockedUserBeforeChanging(t *testing.T) {
	excluded := allowedOperationPolicy()
	excluded.ExcludeChats = []string{operationsChat}
	for _, op := range []string{"block_contact", "unblock_contact"} {
		t.Run(op, func(t *testing.T) {
			d := operationsFake(t)
			characterizeNoWrites(t, &d)
			d.policy = func() (Policy, error) { return excluded, nil }
			_, err := executeOperation(context.Background(), operationJob(op, "", map[string]any{"user_id": operationsChat}), d)
			requireError(t, err, "operation_target_invalid")
		})
		t.Run(op+"_allowed", func(t *testing.T) {
			d := operationsFake(t)
			var changed []bool
			d.block = func(_ context.Context, j types.JID, block bool) error {
				if LegacyChatID(j) != operationsChat {
					t.Fatalf("blocked %s", j)
				}
				changed = append(changed, block)
				return nil
			}
			r, e := executeOperation(context.Background(), operationJob(op, "", map[string]any{"user_id": operationsChat}), d)
			operationResult(t, r, e, "submitted")
			if len(changed) != 1 || changed[0] != (op == "block_contact") {
				t.Fatalf("block calls %v", changed)
			}
		})
	}
}
