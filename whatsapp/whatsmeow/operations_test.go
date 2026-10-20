package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	waBinary "go.mau.fi/whatsmeow/binary"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

func operationJob(op, chat string, params map[string]any) operationRequest {
	out := operationRequest{Operation: op, ProviderChat: chat, ExpiresAt: float64(time.Now().Add(time.Hour).Unix()), Parameters: map[string]json.RawMessage{}}
	for k, v := range params {
		out.Parameters[k], _ = json.Marshal(v)
	}
	return out
}
func operationResult(t *testing.T, value any, err error, outcome string) map[string]any {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
	result, ok := value.(map[string]any)
	if !ok || result["outcome"] != outcome {
		t.Fatalf("want %s, got %#v", outcome, value)
	}
	return result["data"].(map[string]any)
}
func operationsFake(t *testing.T) operationDependencies {
	return operationDependencies{receipts: func(string, string) ([]map[string]any, error) { return nil, nil }, sendDependencies: sendFake(t), page: func(string, int, []any) ([]*events.Message, error) { return nil, nil }, patch: func(context.Context, appstate.PatchInfo) error { return nil }, contact: func(context.Context, types.JID) (types.ContactInfo, error) {
		return types.ContactInfo{Found: true, FullName: "Test Contact"}, nil
	}}
}

func TestNativeUnsupportedOperationsFailBeforeMutation(t *testing.T) {
	for _, op := range []string{"add_contact", "delete_contact", "get_labels", "set_chat_labels"} {
		t.Run(op, func(t *testing.T) {
			d := operationsFake(t)
			d.patch = func(context.Context, appstate.PatchInfo) error { t.Fatal("unsupported patch executed"); return nil }
			job := operationJob(op, operationsChat, nil)
			_, err := executeOperation(context.Background(), job, d)
			requireError(t, err, "operation_unsupported")
		})
	}
}

func TestNativeOperationRejectsUnknownNullAndOutOfBounds(t *testing.T) {
	for name, job := range map[string]operationRequest{
		"unknown_param":      operationJob("send_location", operationsChat, map[string]any{"latitude": 1, "longitude": 2, "raw_api": "danger"}),
		"null_latitude":      operationJob("send_location", operationsChat, map[string]any{"latitude": nil, "longitude": 2}),
		"invalid_latitude":   operationJob("send_location", operationsChat, map[string]any{"latitude": 91, "longitude": 2}),
		"wrong_group":        operationJob("rename_group", operationsChat, map[string]any{"title": "Title"}),
		"expired":            {Operation: "send_location", ProviderChat: operationsChat, ExpiresAt: 1},
		"null_poll_multiple": operationJob("send_poll", operationsChat, map[string]any{"question": "Question", "options": []string{"a", "b"}, "multiple": nil}),
		"duplicate_options":  operationJob("send_poll", operationsChat, map[string]any{"question": "Question", "options": []string{"a", "a"}, "multiple": false}),
	} {
		t.Run(name, func(t *testing.T) {
			d := operationsFake(t)
			d.send = func(context.Context, types.JID, *waE2E.Message, whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
				t.Fatal("invalid operation sent")
				return whatsmeow.SendResponse{}, nil
			}
			_, err := executeOperation(context.Background(), job, d)
			if err == nil {
				t.Fatal("invalid operation accepted")
			}
		})
	}
}

func TestNativeDeleteIsGlobalOwnMessageOnly(t *testing.T) {
	d := operationsFake(t)
	m := operationMessage(operationsChat, "ORIGINAL", true, 1700000000)
	job := operationJob("delete_message", operationsChat, map[string]any{"revoke": true})
	job.ProviderMessageID = LegacyMessageID(m)
	d.get = func(string, string) (*events.Message, error) { return m, nil }
	calls := 0
	d.send = func(_ context.Context, to types.JID, wire *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
		calls++
		pm := wire.GetProtocolMessage()
		if pm == nil || pm.GetType() != waE2E.ProtocolMessage_REVOKE || pm.GetKey().GetID() != m.Info.ID || !pm.GetKey().GetFromMe() || pm.GetKey().GetRemoteJID() != m.Info.Chat.String() {
			t.Fatal("wrong revoke target")
		}
		return whatsmeow.SendResponse{ID: extra.ID, Chat: to, Sender: d.owner, Timestamp: time.Unix(1700000001, 0)}, nil
	}
	r, e := executeOperation(context.Background(), job, d)
	operationResult(t, r, e, "submitted")
	if calls != 1 {
		t.Fatalf("sends=%d", calls)
	}
	job.Parameters["revoke"] = json.RawMessage("false")
	_, e = executeOperation(context.Background(), job, d)
	requireError(t, e, "operation_invalid_parameters")
	job.Parameters["revoke"] = json.RawMessage("true")
	m.Info.IsFromMe = false
	job.ProviderMessageID = LegacyMessageID(m)
	_, e = executeOperation(context.Background(), job, d)
	requireError(t, e, "operation_target_invalid")
	if calls != 1 {
		t.Fatal("unsafe delete fallback sent")
	}
}

func TestNativeOperationSendIncludesWireArchiveReference(t *testing.T) {
	d := operationsFake(t)
	requested, _ := ParseChatID(operationsChat)
	wireChat := types.NewJID("999900001111", types.HiddenUserServer)
	d.ownerLID = types.NewJID("999900002222", types.HiddenUserServer)
	d.lidForPN = func(context.Context, types.JID) (types.JID, error) { return wireChat, nil }
	d.pnForLID = func(context.Context, types.JID) (types.JID, error) { return requested, nil }
	d.send = func(_ context.Context, _ types.JID, _ *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
		return whatsmeow.SendResponse{ID: extra.ID, Chat: wireChat, Sender: d.ownerLID, Timestamp: time.Unix(1700000000, 0)}, nil
	}
	r, e := executeOperation(context.Background(), operationJob("send_location", operationsChat, map[string]any{"latitude": 1.0, "longitude": 2.0}), d)
	operationResult(t, r, e, "confirmed")
	value := r.(map[string]any)
	ref, ok := value["data"].(map[string]any)["archive_reference"].(map[string]any)
	if !ok || ref["provider_chat_id"] != LegacyChatID(wireChat) || ref["provider_message_id"] != "true_"+LegacyChatID(wireChat)+"_TESTSTANZA" ||
		value["provider_message_id"] != "true_"+operationsChat+"_TESTSTANZA" {
		t.Fatal("operation lost compatible public or exact wire reference", value)
	}
}

func TestNativeMutationErrorsAreUnknownWithoutRetry(t *testing.T) {
	d := operationsFake(t)
	calls := 0
	d.patch = func(context.Context, appstate.PatchInfo) error {
		calls++
		return errors.New("private transport credential failure")
	}
	job := operationJob("pin_chat", operationsChat, nil)
	r, e := executeOperation(context.Background(), job, d)
	data := operationResult(t, r, e, "unknown")
	if calls != 1 || data["code"] != "provider_operation_uncertain" {
		t.Fatalf("unsafe error/result %v calls=%d", data, calls)
	}
}

func TestNativeReadDoesNotReleaseRevokedContact(t *testing.T) {
	d := operationsFake(t)
	calls := 0
	p := allowedOperationPolicy()
	d.policy = func() (Policy, error) {
		calls++
		if calls >= 4 {
			p.ExcludeChats = []string{operationsChat}
		}
		return p, nil
	}
	j, _ := ParseChatID(operationsChat)
	d.contacts = func(context.Context) (map[types.JID]types.ContactInfo, error) {
		return map[types.JID]types.ContactInfo{j: {Found: true, FullName: "Private"}}, nil
	}
	_, err := executeOperation(context.Background(), operationJob("list_contacts", "", nil), d)
	requireError(t, err, "operation_target_invalid")
}

func TestNativeMarkReadWholeChatAndBoundedReviewedReceipts(t *testing.T) {
	d := operationsFake(t)
	target := operationMessage(operationsGroup, "TARGET", false, 1700000010)
	older := operationMessage(operationsGroup, "OLDER", false, 1700000000)
	sameSecond := operationMessage(operationsGroup, "UNCERTAIN", false, 1700000010)
	newer := operationMessage(operationsGroup, "NEWER", false, 1700000020)
	outgoing := operationMessage(operationsGroup, "OWN", true, 1700000005)
	d.get = func(string, string) (*events.Message, error) { return target, nil }
	d.page = func(string, int, []any) ([]*events.Message, error) {
		return []*events.Message{older, outgoing, sameSecond, target, newer}, nil
	}
	readCount, patchCount := 0, 0
	d.markRead = func(_ context.Context, ids []types.MessageID, stamp time.Time, chat, sender types.JID) error {
		readCount++
		if len(ids) != 2 || ids[0] != "OLDER" || ids[1] != "TARGET" || chat != target.Info.Chat || sender != target.Info.Sender || !stamp.Equal(target.Info.Timestamp) {
			t.Fatalf("wrong receipts %#v %v %v", ids, chat, sender)
		}
		return nil
	}
	d.patch = func(_ context.Context, patch appstate.PatchInfo) error {
		patchCount++
		action := patch.Mutations[0].Value.GetMarkChatAsReadAction()
		if action == nil || !action.GetRead() {
			t.Fatal("whole chat read patch missing")
		}
		return nil
	}
	job := operationJob("mark_read", operationsGroup, nil)
	job.ProviderMessageID = LegacyMessageID(target)
	r, e := executeOperation(context.Background(), job, d)
	data := operationResult(t, r, e, "submitted")
	if readCount != 1 || patchCount != 1 || data["scope"] != "whole_chat" || data["receipt_count"] != 2 || data["receipt_coverage"] != "partial" {
		t.Fatalf("incorrect read state %#v", data)
	}
}

func TestNativeMarkReadRechecksPolicyBetweenReceiptAndPatch(t *testing.T) {
	d := operationsFake(t)
	p := allowedOperationPolicy()
	m := operationMessage(operationsChat, "TARGET", false, 1700000010)
	d.get = func(string, string) (*events.Message, error) { return m, nil }
	d.page = func(string, int, []any) ([]*events.Message, error) { return []*events.Message{m}, nil }
	d.policy = func() (Policy, error) { return p, nil }
	d.markRead = func(context.Context, []types.MessageID, time.Time, types.JID, types.JID) error {
		p.ExcludeChats = []string{operationsChat}
		return nil
	}
	d.patch = func(context.Context, appstate.PatchInfo) error { t.Fatal("revoked patch reached provider"); return nil }
	job := operationJob("mark_read", operationsChat, nil)
	job.ProviderMessageID = LegacyMessageID(m)
	r, e := executeOperation(context.Background(), job, d)
	operationResult(t, r, e, "unknown")
}

func TestNativeRequestHistoryBoundedAndExplicit(t *testing.T) {
	d := operationsFake(t)
	calls := 0
	d.history = func(_ context.Context, chat string, count int) (any, error) {
		calls++
		if chat != operationsChat || count != 50 {
			t.Fatalf("bad request %s %d", chat, count)
		}
		return map[string]any{"requested": true, "count": count, "retry": "explicit_only"}, nil
	}
	for _, count := range []any{0, 51, nil, 1.5} {
		_, err := executeOperation(context.Background(), operationJob("request_chat_history", operationsChat, map[string]any{"count": count}), d)
		requireError(t, err, "operation_invalid_parameters")
	}
	if calls != 0 {
		t.Fatal("invalid history called provider")
	}
	r, e := executeOperation(context.Background(), operationJob("request_chat_history", operationsChat, nil), d)
	operationResult(t, r, e, "submitted")
	if calls != 1 {
		t.Fatal("history not requested exactly once")
	}
	for _, code := range []string{"history_rate_limited", "history_anchor_unavailable", "history_request_unknown"} {
		d.history = func(context.Context, string, int) (any, error) { return nil, errors.New(code) }
		r, e = executeOperation(context.Background(), operationJob("request_chat_history", operationsChat, nil), d)
		if code == "history_request_unknown" {
			operationResult(t, r, e, "unknown")
		} else {
			requireError(t, e, code)
		}
	}
}

func registeredFileFixture(t *testing.T) (operationRequest, []byte) {
	t.Helper()
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	data := []byte("registered synthetic document")
	digest := digestBytes(data)
	path := filepath.Join(root, digest)
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	return operationRequest{TrustedBlobDirectory: root, RegisteredFilePath: path, FileDigest: digest, FileBytes: int64(len(data)), FileMime: "application/pdf", FileName: "test.pdf", Parameters: map[string]json.RawMessage{"sha256": json.RawMessage(`"` + digest + `"`)}}, data
}

func TestNativeRegisteredFilesRequireExactDigestRegularNoSymlinks(t *testing.T) {
	job, data := registeredFileFixture(t)
	actual, _, _, e := registeredFile(job, "document")
	if e != nil || string(actual) != string(data) {
		t.Fatalf("valid blob failed %v", e)
	}
	for _, mutation := range []func(*operationRequest){func(j *operationRequest) { j.FileBytes++ }, func(j *operationRequest) { j.RegisteredFilePath = filepath.Join(j.TrustedBlobDirectory, "different") }, func(j *operationRequest) { j.FileDigest = "../escape" }, func(j *operationRequest) { j.FileName = "../bad.pdf" }, func(j *operationRequest) { j.FileMime = "text/plain\r\nsecret" }} {
		copy := job
		mutation(&copy)
		_, _, _, e = registeredFile(copy, "document")
		requireError(t, e, "registered_file_invalid")
	}
	if e = os.WriteFile(job.RegisteredFilePath, []byte("wrong synthetic bytes"), 0600); e != nil {
		t.Fatal(e)
	}
	_, _, _, e = registeredFile(job, "document")
	requireError(t, e, "registered_file_invalid")
	if e = os.Remove(job.RegisteredFilePath); e != nil {
		t.Fatal(e)
	}
	other := filepath.Join(t.TempDir(), "source")
	if e = os.WriteFile(other, data, 0600); e != nil {
		t.Fatal(e)
	}
	if e = os.Symlink(other, job.RegisteredFilePath); e != nil {
		t.Fatal(e)
	}
	_, _, _, e = registeredFile(job, "document")
	requireError(t, e, "registered_file_invalid")
}

func TestNativeMessageOperationsTargetExactCapturedMessage(t *testing.T) {
	for _, op := range []string{"edit_message", "send_reaction", "pin_message", "unpin_message", "star_message", "unstar_message"} {
		t.Run(op, func(t *testing.T) {
			d := operationsFake(t)
			m := operationMessage(operationsGroup, "REVIEWED", true, 1700000000)
			job := operationJob(op, operationsGroup, nil)
			job.ProviderMessageID = LegacyMessageID(m)
			d.get = func(string, string) (*events.Message, error) { return m, nil }
			if op == "edit_message" {
				job.Parameters["text"] = json.RawMessage(`"*new text*"`)
			}
			if op == "send_reaction" {
				job.Parameters["emoji"] = json.RawMessage(`"👍"`)
			}
			calls := 0
			d.patch = func(_ context.Context, p appstate.PatchInfo) error {
				calls++
				star := p.Mutations[0].Value.GetStarAction()
				if star == nil || star.GetStarred() != (op == "star_message") {
					t.Fatal("wrong star mutation")
				}
				return nil
			}
			d.send = func(_ context.Context, to types.JID, wire *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
				calls++
				switch op {
				case "edit_message":
					p := wire.GetEditedMessage().GetMessage().GetProtocolMessage()
					if p.GetType() != waE2E.ProtocolMessage_MESSAGE_EDIT || p.GetKey().GetID() != m.Info.ID || p.GetEditedMessage().GetConversation() != "*new text*" {
						t.Fatal("wrong edit")
					}
				case "send_reaction":
					r := wire.GetReactionMessage()
					if r.GetKey().GetID() != m.Info.ID || r.GetText() != "👍" {
						t.Fatal("wrong reaction")
					}
				case "pin_message", "unpin_message":
					pin := wire.GetPinInChatMessage()
					want := waE2E.PinInChatMessage_PIN_FOR_ALL
					if op == "unpin_message" {
						want = waE2E.PinInChatMessage_UNPIN_FOR_ALL
					}
					if pin.GetKey().GetID() != m.Info.ID || pin.GetType() != want {
						t.Fatal("wrong pin")
					}
				}
				return whatsmeow.SendResponse{ID: extra.ID, Chat: to, Sender: d.owner, Timestamp: time.Unix(1700000001, 0)}, nil
			}
			r, e := executeOperation(context.Background(), job, d)
			operationResult(t, r, e, "submitted")
			if calls != 1 {
				t.Fatalf("operation calls=%d", calls)
			}
		})
	}
}

func TestNativeGroupAdministrationUsesTypedTargetAndNoPrivateInvites(t *testing.T) {
	for _, op := range []string{"rename_group", "set_group_description", "add_participant", "remove_participant", "promote_participant", "demote_participant", "leave_group", "get_invite_link", "revoke_invite_link"} {
		t.Run(op, func(t *testing.T) {
			d := operationsFake(t)
			group, _ := ParseChatID(operationsGroup)
			user, _ := ParseChatID(operationsChat)
			d.group = func(_ context.Context, j types.JID) (*types.GroupInfo, error) {
				if j != group {
					t.Fatal("wrong group read")
				}
				return &types.GroupInfo{JID: group}, nil
			}
			calls := 0
			d.groupName = func(_ context.Context, j types.JID, name string) error {
				calls++
				if j != group || name != "New group" {
					t.Fatal("wrong name")
				}
				return nil
			}
			d.groupDescription = func(_ context.Context, j types.JID, _ string, description string) error {
				calls++
				if j != group || description != "Description" {
					t.Fatal("wrong description")
				}
				return nil
			}
			d.participants = func(_ context.Context, j types.JID, users []types.JID, action whatsmeow.ParticipantChange) ([]types.GroupParticipant, error) {
				calls++
				expected := map[string]whatsmeow.ParticipantChange{"add_participant": whatsmeow.ParticipantChangeAdd, "remove_participant": whatsmeow.ParticipantChangeRemove, "promote_participant": whatsmeow.ParticipantChangePromote, "demote_participant": whatsmeow.ParticipantChangeDemote}[op]
				if j != group || len(users) != 1 || users[0] != user || action != expected {
					t.Fatal("wrong participant mutation")
				}
				return nil, nil
			}
			d.leave = func(_ context.Context, j types.JID) error {
				calls++
				if j != group {
					t.Fatal("wrong leave")
				}
				return nil
			}
			d.invite = func(_ context.Context, j types.JID, revoke bool) (string, error) {
				calls++
				if j != group || revoke != (op == "revoke_invite_link") {
					t.Fatal("wrong invite action")
				}
				return "https://chat.whatsapp.com/SYNTHETIC", nil
			}
			params := map[string]any{}
			switch op {
			case "rename_group":
				params["title"] = "New group"
			case "set_group_description":
				params["description"] = "Description"
			case "add_participant", "remove_participant", "promote_participant", "demote_participant":
				params["user_id"] = operationsChat
			}
			r, e := executeOperation(context.Background(), operationJob(op, operationsGroup, params), d)
			data := operationResult(t, r, e, "submitted")
			if calls != 1 {
				t.Fatalf("calls=%d", calls)
			}
			if op == "get_invite_link" && data["invite_link"] != "https://chat.whatsapp.com/SYNTHETIC" {
				t.Fatal("missing link")
			}
			d.send = func(context.Context, types.JID, *waE2E.Message, whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
				t.Fatal("private invitation sent")
				return whatsmeow.SendResponse{}, nil
			}
		})
	}
}

func TestNativeFileUploadVerifiedBeforeSendAndReauthorizedAfterUpload(t *testing.T) {
	for _, revoke := range []bool{false, true} {
		t.Run(map[bool]string{false: "valid", true: "revoked"}[revoke], func(t *testing.T) {
			d := operationsFake(t)
			job, data := registeredFileFixture(t)
			job.Operation = "send_file"
			job.ProviderChat = operationsChat
			job.ExpiresAt = float64(time.Now().Add(time.Hour).Unix())
			job.Parameters["mode"] = json.RawMessage(`"document"`)
			job.Parameters["caption"] = json.RawMessage(`"caption"`)
			p := allowedOperationPolicy()
			d.policy = func() (Policy, error) { return p, nil }
			uploads, sends := 0, 0
			d.upload = func(_ context.Context, b []byte, kind whatsmeow.MediaType) (whatsmeow.UploadResponse, error) {
				uploads++
				if string(b) != string(data) || kind != whatsmeow.MediaDocument {
					t.Fatal("wrong upload")
				}
				if revoke {
					p.ExcludeChats = []string{operationsChat}
				}
				hash := sha256.Sum256(data)
				return whatsmeow.UploadResponse{URL: "https://mmg.whatsapp.net/v/synthetic", DirectPath: "/v/synthetic", FileLength: uint64(len(data)), FileSHA256: hash[:]}, nil
			}
			d.send = func(_ context.Context, to types.JID, wire *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
				sends++
				doc := wire.GetDocumentMessage()
				if doc.GetCaption() != "caption" || doc.GetFileName() != "test.pdf" || doc.GetFileLength() != uint64(len(data)) {
					t.Fatal("wrong upload envelope")
				}
				return whatsmeow.SendResponse{ID: extra.ID, Chat: to, Sender: d.owner, Timestamp: time.Unix(1700000000, 0)}, nil
			}
			r, e := executeOperation(context.Background(), job, d)
			outcome := "confirmed"
			wantSends := 1
			if revoke {
				outcome = "unknown"
				wantSends = 0
			}
			operationResult(t, r, e, outcome)
			if uploads != 1 || sends != wantSends {
				t.Fatalf("uploads=%d sends=%d", uploads, sends)
			}
		})
	}
}

func TestNativeForwardStripsSourceContextAndRechecksSourceScope(t *testing.T) {
	d := operationsFake(t)
	source := operationMessage(operationsChat, "SOURCE", false, 1700000000)
	source.Message = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String("Text"), ContextInfo: &waE2E.ContextInfo{StanzaID: proto.String("PRIVATE_QUOTE"), MentionedJID: []string{"PRIVATE_PERSON"}}}}
	d.get = func(string, string) (*events.Message, error) { return source, nil }
	job := operationJob("forward_message", operationsGroup, map[string]any{"source_message_id": "archive_source", "source_revision": "revision"})
	job.SourceProviderChat = operationsChat
	job.SourceProviderMessage = LegacyMessageID(source)
	d.send = func(_ context.Context, to types.JID, wire *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
		context := wire.GetExtendedTextMessage().GetContextInfo()
		if !context.GetIsForwarded() || context.GetStanzaID() != "" || len(context.GetMentionedJID()) != 0 {
			t.Fatal("private source context leaked")
		}
		return whatsmeow.SendResponse{ID: extra.ID, Chat: to, Sender: d.owner, Timestamp: time.Unix(1700000001, 0)}, nil
	}
	r, e := executeOperation(context.Background(), job, d)
	operationResult(t, r, e, "confirmed")
	p := allowedOperationPolicy()
	d.policy = func() (Policy, error) { return p, nil }
	d.get = func(string, string) (*events.Message, error) {
		p.ExcludeChats = []string{operationsChat}
		return source, nil
	}
	_, e = executeOperation(context.Background(), job, d)
	requireError(t, e, "operation_target_invalid")
}

func TestNativeReceiptReadsReportObservedPartialEvidence(t *testing.T) {
	d := operationsFake(t)
	m := operationMessage(operationsChat, "MESSAGE", true, 1700000000)
	d.get = func(string, string) (*events.Message, error) { return m, nil }
	job := operationJob("get_message_info", operationsChat, nil)
	job.ProviderMessageID = LegacyMessageID(m)
	r, e := executeOperation(context.Background(), job, d)
	data := operationResult(t, r, e, "confirmed")
	if data["state"] != "unknown" || data["delivery_remaining"] != nil {
		t.Fatal("invented receipt completeness")
	}
	d.receipts = func(chat, id string) ([]map[string]any, error) {
		if chat != operationsChat || id != LegacyMessageID(m) {
			t.Fatal("wrong receipt target")
		}
		return []map[string]any{{"state": "read", "provider_id": id, "provider_at": nil, "audience": "unknown"}}, nil
	}
	r, e = executeOperation(context.Background(), job, d)
	data = operationResult(t, r, e, "confirmed")
	if data["state"] != "partial" || data["completeness"] != "unknown" || len(data["observations"].([]map[string]any)) != 1 {
		t.Fatalf("wrong evidence %v", data)
	}
}

func TestNativeParticipantRefusalAndInvitationRemainVisible(t *testing.T) {
	d := operationsFake(t)
	group, _ := ParseChatID(operationsGroup)
	member, _ := ParseChatID(operationsChat)
	d.group = func(context.Context, types.JID) (*types.GroupInfo, error) { return &types.GroupInfo{JID: group}, nil }
	d.participants = func(context.Context, types.JID, []types.JID, whatsmeow.ParticipantChange) ([]types.GroupParticipant, error) {
		return []types.GroupParticipant{{JID: member, Error: 403, AddRequest: &types.GroupParticipantAddRequest{Code: "SECRET_INVITE_CREDENTIAL", Expiration: time.Unix(1700000000, 0)}}}, nil
	}
	d.send = func(context.Context, types.JID, *waE2E.Message, whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
		t.Fatal("unapproved automatic invitation")
		return whatsmeow.SendResponse{}, nil
	}
	r, e := executeOperation(context.Background(), operationJob("add_participant", operationsGroup, map[string]any{"user_id": operationsChat}), d)
	data := operationResult(t, r, e, "submitted")
	rows := data["participants"].([]any)
	row := rows[0].(map[string]any)
	if row["state"] != "invitation_required" || row["error_code"] != 403 || row["requires_invitation"] != true || data["auto_invite_sent"] != false {
		t.Fatalf("refusal hidden %#v", data)
	}
	encoded, _ := json.Marshal(data)
	if string(encoded) == "" || bytes.Contains(encoded, []byte("SECRET_INVITE_CREDENTIAL")) {
		t.Fatal("invitation credential disclosed")
	}
	d.participants = func(context.Context, types.JID, []types.JID, whatsmeow.ParticipantChange) ([]types.GroupParticipant, error) {
		return []types.GroupParticipant{{JID: member, Error: 409}}, nil
	}
	r, e = executeOperation(context.Background(), operationJob("remove_participant", operationsGroup, map[string]any{"user_id": operationsChat}), d)
	data = operationResult(t, r, e, "submitted")
	row = data["participants"].([]any)[0].(map[string]any)
	if row["state"] != "refused" || row["error_code"] != 409 {
		t.Fatalf("known refusal hidden %#v", row)
	}
}

func TestNativePinsExcludeKnownExpiryAndUnpin(t *testing.T) {
	d := operationsFake(t)
	expired := operationMessage(operationsChat, "EXPIRED", false, 1700000000)
	active := operationMessage(operationsChat, "ACTIVE", false, 1700000001)
	removed := operationMessage(operationsChat, "REMOVED", false, 1700000002)
	pinEvent := func(id string, kind waE2E.PinInChatMessage_Type, stamp int64, duration uint32) *events.Message {
		event := operationMessage(operationsChat, "PIN_"+id, false, stamp/1000)
		event.Message = &waE2E.Message{PinInChatMessage: &waE2E.PinInChatMessage{Key: capturedMessageKey(operationMessage(operationsChat, id, false, 1)), Type: &kind, SenderTimestampMS: &stamp}, MessageContextInfo: &waE2E.MessageContextInfo{MessageAddOnDurationInSecs: &duration}}
		return event
	}
	now := time.Now().UnixMilli()
	d.page = func(string, int, []any) ([]*events.Message, error) {
		return []*events.Message{expired, active, removed, pinEvent("EXPIRED", waE2E.PinInChatMessage_PIN_FOR_ALL, now-120000, 60), pinEvent("ACTIVE", waE2E.PinInChatMessage_PIN_FOR_ALL, now, 86400), pinEvent("REMOVED", waE2E.PinInChatMessage_PIN_FOR_ALL, now, 86400), pinEvent("REMOVED", waE2E.PinInChatMessage_UNPIN_FOR_ALL, now+1, 86400)}, nil
	}
	r, e := executeOperation(context.Background(), operationJob("get_pinned_messages", operationsChat, nil), d)
	data := operationResult(t, r, e, "confirmed")
	items := data["items"].([]any)
	if data["state"] != "partial" || len(items) != 1 || items[0].(map[string]any)["provider_message_id"] != LegacyMessageID(active) {
		t.Fatalf("expired or removed pin included %#v", data)
	}
}

const phoneFixture = "12025550123" // NANP fictional 555-01xx range
const lidFixture = "987654321012345@lid"

// phoneLookupFake resolves like the pinned IsOnWhatsApp: one usync row whose JID is
// the LID, with the PN mapping stored as a side effect. It counts provider queries.
func phoneLookupFake(t *testing.T, row *types.IsOnWhatsAppResponse, stored *types.JID) (operationDependencies, *int) {
	t.Helper()
	d := operationsFake(t)
	queries := 0
	lid, _ := ParseChatID(lidFixture)
	d.lidForPN = func(context.Context, types.JID) (types.JID, error) {
		if stored == nil {
			return types.EmptyJID, nil
		}
		return *stored, nil
	}
	d.isOnWhatsApp = func(_ context.Context, phones []string) ([]types.IsOnWhatsAppResponse, error) {
		queries++
		if len(phones) != 1 || phones[0] != "+"+phoneFixture {
			t.Fatalf("unexpected query %v", phones)
		}
		if row != nil {
			return []types.IsOnWhatsAppResponse{*row}, nil
		}
		pn := types.NewJID(phoneFixture, types.DefaultUserServer)
		return []types.IsOnWhatsAppResponse{{Query: "+" + phoneFixture, JID: lid, IsIn: true, PhoneNumber: pn}}, nil
	}
	return d, &queries
}

// phoneJob is an open_chat_by_phone request with the collector's lookup grant.
func phoneJob(params map[string]any) operationRequest {
	job := operationJob("open_chat_by_phone", "", params)
	job.LookupAllowed = true
	return job
}

// waitPhoneSlot waits until an abandoned resolution has returned its slot.
func waitPhoneSlot(t *testing.T) {
	t.Helper()
	select {
	case phoneLookupSlot <- struct{}{}:
		<-phoneLookupSlot
	case <-time.After(2 * time.Second):
		t.Fatal("an abandoned phone resolution never returned")
	}
}

// spec: ANYM-430
func TestNativeOpenChatByPhoneResolvesTheCanonicalLID(t *testing.T) {
	d, queries := phoneLookupFake(t, nil, nil)
	value, err := executeOperation(context.Background(), phoneJob(map[string]any{"phone": phoneFixture}), d)
	data := operationResult(t, value, err, "confirmed")
	if data["provider_chat"] != lidFixture || data["on_whatsapp"] != true || data["provider_lookup"] != true || *queries != 1 {
		t.Fatalf("unexpected answer %#v after %d queries", data, *queries)
	}
	// A send to that chat archives its echo in the same chat, as the pinned sender routes.
	lid, _ := ParseChatID(lidFixture)
	receipt, err := sendAndCaptureReceipt(context.Background(), d.sendDependencies, lid, &waE2E.Message{Conversation: proto.String("synthetic")})
	if err != nil || receipt["archive_reference"].(map[string]any)["provider_chat_id"] != lidFixture {
		t.Fatalf("echo left the canonical chat: %#v %v", receipt, err)
	}
}

// spec: ANYM-430
func TestNativeOpenChatByPhoneAnswersAKnownMappingLocally(t *testing.T) {
	lid, _ := ParseChatID(lidFixture)
	d, queries := phoneLookupFake(t, nil, &lid)
	job := phoneJob(map[string]any{"phone": phoneFixture, "create": true})
	job.KnownChats = []string{lidFixture}
	value, err := executeOperation(context.Background(), job, d)
	data := operationResult(t, value, err, "confirmed")
	if data["provider_chat"] != lidFixture || data["provider_lookup"] != false || *queries != 0 {
		t.Fatalf("local answer queried the provider: %#v %d", data, *queries)
	}
	// The same mapping without an archived chat still asks WhatsApp once.
	job.KnownChats = nil
	value, err = executeOperation(context.Background(), job, d)
	if data = operationResult(t, value, err, "confirmed"); data["provider_lookup"] != true || *queries != 1 {
		t.Fatalf("unarchived mapping answered locally: %#v %d", data, *queries)
	}
}

// spec: ANYM-430
func TestNativeOpenChatByPhoneNegativeUnresolvedAndPolicy(t *testing.T) {
	pn := types.NewJID(phoneFixture, types.DefaultUserServer)
	d, _ := phoneLookupFake(t, &types.IsOnWhatsAppResponse{Query: "+" + phoneFixture, JID: pn, IsIn: false, PhoneNumber: pn}, nil)
	value, err := executeOperation(context.Background(), phoneJob(map[string]any{"phone": phoneFixture}), d)
	if data := operationResult(t, value, err, "confirmed"); data["on_whatsapp"] != false || data["provider_chat"] != nil {
		t.Fatalf("unregistered number got a chat: %#v", data)
	}
	d, _ = phoneLookupFake(t, &types.IsOnWhatsAppResponse{Query: "+" + phoneFixture, JID: pn, IsIn: true, PhoneNumber: pn}, nil)
	_, err = executeOperation(context.Background(), phoneJob(map[string]any{"phone": phoneFixture}), d)
	requireError(t, err, "identity_unresolved")
	// The LID learned only from the query is excluded: refused, never returned.
	d, queries := phoneLookupFake(t, nil, nil)
	d.policy = func() (Policy, error) {
		p := allowedOperationPolicy()
		p.ExcludeChats = []string{lidFixture}
		return p, nil
	}
	_, err = executeOperation(context.Background(), phoneJob(map[string]any{"phone": phoneFixture}), d)
	requireError(t, err, "chat_not_collected")
	if *queries != 1 {
		t.Fatalf("queries %d", *queries)
	}
	// Discovery off or an excluded PN refuses before any query.
	for name, change := range map[string]func(*Policy){
		"discovery_off": func(p *Policy) { p.DiscoverChats = false; p.IncludeChats = []string{phoneFixture + "@c.us"} },
		"excluded_pn":   func(p *Policy) { p.ExcludeChats = []string{phoneFixture + "@c.us"} },
	} {
		d, queries = phoneLookupFake(t, nil, nil)
		d.policy = func() (Policy, error) { p := allowedOperationPolicy(); change(&p); return p, nil }
		_, err = executeOperation(context.Background(), phoneJob(map[string]any{"phone": phoneFixture}), d)
		requireError(t, err, "chat_not_collected")
		if *queries != 0 {
			t.Fatalf("%s queried the provider", name)
		}
	}
}

// spec: ANYM-430
func TestNativeOpenChatByPhoneStalledLookupIsABoundedTimeout(t *testing.T) {
	d, _ := phoneLookupFake(t, nil, nil)
	d.lookupTimeout = 50 * time.Millisecond
	release := make(chan struct{})
	d.isOnWhatsApp = func(context.Context, []string) ([]types.IsOnWhatsAppResponse, error) {
		<-release // ignores its context, like a wedged socket
		return nil, nil
	}
	started := time.Now()
	_, err := executeOperation(context.Background(), phoneJob(map[string]any{"phone": phoneFixture}), d)
	requireError(t, err, "provider_timeout")
	if time.Since(started) > 2*time.Second {
		t.Fatal("lookup deadline not enforced")
	}
	close(release)
	waitPhoneSlot(t)
	if phoneLookupTimeout != 10*time.Second {
		t.Fatal("the production deadline must stay under the 30 s worker timeout")
	}
}

// spec: ANYM-430
func TestNativeOpenChatByPhoneLookupKeepsToTheCollectorsRemainingBudget(t *testing.T) {
	d, _ := phoneLookupFake(t, nil, nil)
	release := make(chan struct{})
	d.isOnWhatsApp = func(context.Context, []string) ([]types.IsOnWhatsAppResponse, error) {
		<-release
		return nil, nil
	}
	job := phoneJob(map[string]any{"phone": phoneFixture})
	// What the local-only call and archive check left of the shared deadline, as the collector sends it.
	if err := json.Unmarshal([]byte(`{"lookup_ms":50}`), &job); err != nil {
		t.Fatal(err)
	}
	started := time.Now()
	_, err := executeOperation(context.Background(), job, d)
	requireError(t, err, "provider_timeout")
	if time.Since(started) > 2*time.Second {
		t.Fatal("lookup outlived the collector's remaining budget")
	}
	close(release)
	waitPhoneSlot(t)
	if err := json.Unmarshal([]byte(`{"lookup_ms":-1}`), &job); err != nil {
		t.Fatal(err)
	}
	_, err = executeOperation(context.Background(), job, d)
	requireError(t, err, "operation_invalid_parameters")
}

// spec: ANYM-430
func TestNativeOpenChatByPhoneRejectsUnnormalizedNumbers(t *testing.T) {
	for _, phone := range []any{"+12025550123", "07700 900123", "07700900123", "1 202", "", 12025550123, "12025550123@c.us"} {
		d, queries := phoneLookupFake(t, nil, nil)
		_, err := executeOperation(context.Background(), phoneJob(map[string]any{"phone": phone}), d)
		requireError(t, err, "invalid_phone")
		if *queries != 0 {
			t.Fatalf("%v queried the provider", phone)
		}
	}
}

// spec: ANYM-430
func TestNativeOpenChatByPhoneDeadlineCoversTheSessionStore(t *testing.T) {
	lid, _ := ParseChatID(lidFixture)
	d, queries := phoneLookupFake(t, nil, &lid)
	d.lookupTimeout = 50 * time.Millisecond
	release := make(chan struct{})
	d.lidForPN = func(context.Context, types.JID) (types.JID, error) {
		<-release // a store lock held across SQL ignores the context
		return lid, nil
	}
	job := phoneJob(map[string]any{"phone": phoneFixture})
	job.KnownChats = []string{lidFixture}
	started := time.Now()
	_, err := executeOperation(context.Background(), job, d)
	requireError(t, err, "provider_timeout")
	// While the wedged read still runs, the next request does not pile up behind it.
	_, err = executeOperation(context.Background(), job, d)
	requireError(t, err, "provider_timeout")
	if time.Since(started) > 2*time.Second || *queries != 0 {
		t.Fatalf("store read escaped the deadline (%d queries)", *queries)
	}
	close(release)
	waitPhoneSlot(t)
	value, err := executeOperation(context.Background(), job, d)
	if data := operationResult(t, value, err, "confirmed"); data["provider_lookup"] != false {
		t.Fatalf("recovered lane did not answer locally: %#v", data)
	}
}

// spec: ANYM-430
func TestNativeOpenChatByPhoneSpentBudgetStillAnswersLocally(t *testing.T) {
	lid, _ := ParseChatID(lidFixture)
	d, queries := phoneLookupFake(t, nil, &lid)
	job := phoneJob(map[string]any{"phone": phoneFixture, "create": true})
	job.LookupAllowed = false
	job.KnownChats = []string{"111222333444555@lid", lidFixture}
	value, err := executeOperation(context.Background(), job, d)
	if data := operationResult(t, value, err, "confirmed"); data["provider_chat"] != lidFixture || data["provider_lookup"] != false {
		t.Fatalf("local answer refused with a spent budget: %#v", data)
	}
	job.KnownChats = nil
	_, err = executeOperation(context.Background(), job, d)
	requireError(t, err, "rate_limited")
	if *queries != 0 {
		t.Fatalf("spent budget still queried WhatsApp %d times", *queries)
	}
}

// spec: ANYM-430
func TestNativeOpenChatByPhoneErrorsSurviveTheWireSanitizer(t *testing.T) {
	for _, code := range []string{"invalid_phone", "identity_unresolved", "chat_not_collected", "provider_timeout", "rate_limited"} {
		if got := safeError(errors.New(code)); got != code {
			t.Fatalf("%s crosses the lane as %s", code, got)
		}
	}
}

// spec: ANYM-430
func TestNativeOpenChatByPhoneRevokedOptInBeforeTheQuery(t *testing.T) {
	for name, revoke := range map[string]func(*Policy){
		"operation": func(p *Policy) { p.OperationsEnabled = nil },
		"exclusion": func(p *Policy) { p.ExcludeChats = []string{phoneFixture + "@c.us"} },
	} {
		d, queries := phoneLookupFake(t, nil, nil)
		revoked := false
		d.policy = func() (Policy, error) {
			p := allowedOperationPolicy()
			if revoked {
				revoke(&p)
			}
			return p, nil
		}
		d.lidForPN = func(context.Context, types.JID) (types.JID, error) {
			revoked = true // the owner turns it off while the store is read
			return types.EmptyJID, nil
		}
		_, err := executeOperation(context.Background(), phoneJob(map[string]any{"phone": phoneFixture}), d)
		if err == nil || *queries != 0 {
			t.Fatalf("%s: the number went to WhatsApp after the revocation (%v, %d queries)", name, err, *queries)
		}
	}
}

// spec: ANYM-430
func TestNativeOpenChatByPhoneLocalOnlyStepNeverQueries(t *testing.T) {
	lid, _ := ParseChatID(lidFixture)
	for _, stored := range []*types.JID{&lid, nil} {
		d, queries := phoneLookupFake(t, nil, stored)
		job := phoneJob(map[string]any{"phone": phoneFixture})
		job.LocalOnly = true
		value, err := executeOperation(context.Background(), job, d)
		data := operationResult(t, value, err, "confirmed")
		want := any(nil)
		if stored != nil {
			want = lidFixture
		}
		if data["local_lid"] != want || *queries != 0 || data["provider_chat"] != nil {
			t.Fatalf("local step answered %#v after %d queries", data, *queries)
		}
	}
}

// groupWith is the synthetic group with extra setting children (ANYM-242).
func groupWith(children ...waBinary.Node) *waBinary.Node {
	node := syntheticGroupNode()
	node.Content = append(node.Content.([]waBinary.Node), children...)
	return iqResult(node)
}

// settingIQ answers group reads with the setting on or off, flipping it when a
// change arrives unless frozen (a read-back that does not show the change).
func settingIQ(child waBinary.Node, on, frozen bool) *recordingIQ {
	return &recordingIQ{reply: func(_ int, q whatsmeow.DangerousInfoQuery) (*waBinary.Node, error) {
		content := q.Content.([]waBinary.Node)[0]
		if content.Tag == "query" {
			if on {
				return groupWith(child), nil
			}
			return groupWith(), nil
		}
		if !frozen {
			on = !on
		}
		return iqResult(), nil
	}}
}

var groupSettingCases = []struct {
	op, onTag, offTag string
	child             waBinary.Node
	node              func(bool) waBinary.Node
}{
	{"set_group_messages_admins_only", "announcement", "not_announcement", waBinary.Node{Tag: "announcement"}, nil},
	{"set_group_info_admins_only", "locked", "unlocked", waBinary.Node{Tag: "locked"}, nil},
	{"set_join_approval", "membership_approval_mode", "membership_approval_mode", waBinary.Node{Tag: "membership_approval_mode", Content: []waBinary.Node{{Tag: "group_join", Attrs: waBinary.Attrs{"state": "on"}}}},
		func(on bool) waBinary.Node {
			state := map[bool]string{true: "on", false: "off"}[on]
			return waBinary.Node{Tag: "membership_approval_mode", Content: []waBinary.Node{{Tag: "group_join", Attrs: waBinary.Attrs{"state": state}}}}
		}},
}

// spec: ANYM-242
func TestGroupSettingsChangeConfirmsFromReadBack(t *testing.T) {
	for _, c := range groupSettingCases {
		for _, enabled := range []bool{true, false} {
			r := settingIQ(c.child, !enabled, false)
			result, err := runOperation(context.Background(), operationJob(c.op, operationsGroup, map[string]any{"enabled": enabled}), groupOperationDeps(t, r))
			data := operationResult(t, result, err, "confirmed")
			if !reflect.DeepEqual(data, map[string]any{"enabled": enabled, "previous": !enabled, "changed": true}) {
				t.Fatalf("%s %v: %#v", c.op, enabled, data)
			}
			if len(r.calls) != 3 {
				t.Fatalf("%s: %d calls", c.op, len(r.calls))
			}
			got := r.calls[1]
			want := waBinary.Node{Tag: map[bool]string{true: c.onTag, false: c.offTag}[enabled]}
			if c.node != nil {
				want = c.node(enabled)
			}
			if got.Type != groupIQSet || !reflect.DeepEqual(got.Content, []waBinary.Node{want}) || LegacyChatID(got.To) != operationsGroup {
				t.Fatalf("%s %v sent %#v", c.op, enabled, got)
			}
		}
	}
}

// spec: ANYM-242
func TestGroupSettingsAlreadyInStateDoesNotMutate(t *testing.T) {
	for _, c := range groupSettingCases {
		for _, enabled := range []bool{true, false} {
			r := settingIQ(c.child, enabled, false)
			result, err := runOperation(context.Background(), operationJob(c.op, operationsGroup, map[string]any{"enabled": enabled}), groupOperationDeps(t, r))
			data := operationResult(t, result, err, "confirmed")
			if !reflect.DeepEqual(data, map[string]any{"enabled": enabled, "previous": enabled, "changed": false}) || len(r.calls) != 1 {
				t.Fatalf("%s %v: %#v, %d calls", c.op, enabled, data, len(r.calls))
			}
		}
	}
}

// spec: ANYM-242
func TestGroupSettingsUnconfirmedReadBackIsSubmitted(t *testing.T) {
	for _, c := range groupSettingCases {
		r := settingIQ(c.child, false, true)
		result, err := runOperation(context.Background(), operationJob(c.op, operationsGroup, map[string]any{"enabled": true}), groupOperationDeps(t, r))
		data := operationResult(t, result, err, "submitted")
		if !reflect.DeepEqual(data, map[string]any{"enabled": true, "previous": false, "changed": true}) {
			t.Fatalf("%s: %#v", c.op, data)
		}
	}
}

// spec: ANYM-242
func TestGroupSettingsRefuseNonAdminBeforeMutation(t *testing.T) {
	for _, op := range []string{"set_group_messages_admins_only", "set_group_info_admins_only", "set_join_approval", "approve_join_requests", "reject_join_requests"} {
		r := &recordingIQ{reply: syntheticReply}
		d := groupOperationDeps(t, r)
		d.ownerLID = types.NewJID("999", types.HiddenUserServer)
		_, err := runOperation(context.Background(), groupJob(op), d)
		requireError(t, err, "forbidden")
		if len(r.calls) != 1 {
			t.Fatalf("%s mutated a group it does not administer: %d calls", op, len(r.calls))
		}
	}
}

// spec: ANYM-242
func TestGroupSettingsParameters(t *testing.T) {
	for name, job := range map[string]operationRequest{
		"slow mode is Telegram only": operationJob("set_slow_mode", operationsGroup, map[string]any{"seconds": 10}),
		"missing enabled":            operationJob("set_join_approval", operationsGroup, nil),
		"null enabled":               operationJob("set_join_approval", operationsGroup, map[string]any{"enabled": nil}),
		"string enabled":             operationJob("set_join_approval", operationsGroup, map[string]any{"enabled": "true"}),
		"rights payload":             operationJob("set_group_messages_admins_only", operationsGroup, map[string]any{"enabled": true, "rights": map[string]any{}}),
		"not a group":                operationJob("set_group_info_admins_only", operationsChat, map[string]any{"enabled": true}),
		"21 ids":                     operationJob("approve_join_requests", operationsGroup, map[string]any{"user_ids": manyUsers(21)}),
		"duplicate ids":              operationJob("reject_join_requests", operationsGroup, map[string]any{"user_ids": []string{operationsChat, operationsChat}}),
		"limit 0":                    operationJob("list_join_requests", operationsGroup, map[string]any{"limit": 0}),
		"limit 101":                  operationJob("list_join_requests", operationsGroup, map[string]any{"limit": 101}),
	} {
		r := &recordingIQ{reply: syntheticReply}
		if _, err := runOperation(context.Background(), job, groupOperationDeps(t, r)); err == nil {
			t.Fatalf("%s accepted", name)
		}
		if len(r.calls) > 1 || len(r.calls) == 1 && r.calls[0].Type != groupIQGet {
			t.Fatalf("%s reached a change: %#v", name, r.calls)
		}
	}
}

func manyUsers(n int) []string {
	out := make([]string, n)
	for i := range out {
		out[i] = fmt.Sprintf("1555000%04d@c.us", i)
	}
	return out
}

func batchIQ(answer func([]waBinary.Node) []waBinary.Node) *recordingIQ {
	return &recordingIQ{reply: func(n int, q whatsmeow.DangerousInfoQuery) (*waBinary.Node, error) {
		content := q.Content.([]waBinary.Node)[0]
		if content.Tag != "membership_requests_action" {
			return syntheticReply(n, q)
		}
		action := content.Content.([]waBinary.Node)[0]
		return iqResult(waBinary.Node{Tag: "membership_requests_action", Content: []waBinary.Node{{Tag: action.Tag, Content: answer(action.Content.([]waBinary.Node))}}}), nil
	}}
}

// spec: ANYM-242
func TestJoinRequestBatchPerIDRows(t *testing.T) {
	ids := manyUsers(3)
	r := batchIQ(func(requested []waBinary.Node) []waBinary.Node {
		requested[2].Attrs = waBinary.Attrs{"jid": requested[2].Attrs["jid"], "error": "404"}
		// The provider answers out of order; rows follow the request.
		return []waBinary.Node{requested[2], requested[0], requested[1]}
	})
	result, err := runOperation(context.Background(), operationJob("approve_join_requests", operationsGroup, map[string]any{"user_ids": ids}), groupOperationDeps(t, r))
	data := operationResult(t, result, err, "confirmed")
	want := map[string]any{"all_applied": false, "results": []any{
		map[string]any{"id": ids[0], "state": "applied"},
		map[string]any{"id": ids[1], "state": "applied"},
		map[string]any{"id": ids[2], "state": "refused", "reason": "provider_refused", "provider_code": 404},
	}}
	if !reflect.DeepEqual(data, want) {
		t.Fatalf("%#v", data)
	}
	if len(r.calls) != 2 {
		t.Fatalf("%d calls, want one read and one batch request", len(r.calls))
	}
	sent := r.calls[1].Content.([]waBinary.Node)[0].Content.([]waBinary.Node)[0]
	if sent.Tag != "approve" || len(sent.Content.([]waBinary.Node)) != 3 {
		t.Fatalf("batch node %#v", sent)
	}
}

// spec: ANYM-242
func TestJoinRequestBatchMissingIDIsUnknown(t *testing.T) {
	ids := manyUsers(2)
	r := batchIQ(func(requested []waBinary.Node) []waBinary.Node { return requested[:1] })
	result, err := runOperation(context.Background(), operationJob("approve_join_requests", operationsGroup, map[string]any{"user_ids": ids}), groupOperationDeps(t, r))
	data := operationResult(t, result, err, "unknown")
	want := map[string]any{"all_applied": false, "results": []any{
		map[string]any{"id": ids[0], "state": "applied"},
		map[string]any{"id": ids[1], "state": "unknown"},
	}}
	if !reflect.DeepEqual(data, want) {
		t.Fatalf("%#v", data)
	}
}

// spec: ANYM-242
func TestJoinRequestBatchRejectSendsRejectAction(t *testing.T) {
	ids := manyUsers(2)
	r := batchIQ(func(requested []waBinary.Node) []waBinary.Node { return requested })
	result, err := runOperation(context.Background(), operationJob("reject_join_requests", operationsGroup, map[string]any{"user_ids": ids}), groupOperationDeps(t, r))
	data := operationResult(t, result, err, "confirmed")
	if data["all_applied"] != true {
		t.Fatalf("%#v", data)
	}
	sent := r.calls[1].Content.([]waBinary.Node)[0].Content.([]waBinary.Node)[0]
	if sent.Tag != "reject" {
		t.Fatalf("reject sent %q", sent.Tag)
	}
}

// spec: ANYM-242
func TestJoinRequestsProjection(t *testing.T) {
	request := func(user string, at string) waBinary.Node {
		j, _ := ParseChatID(user)
		return waBinary.Node{Tag: "membership_approval_request", Attrs: waBinary.Attrs{"jid": j, "request_time": at}}
	}
	r := &recordingIQ{reply: func(int, whatsmeow.DangerousInfoQuery) (*waBinary.Node, error) {
		return iqResult(waBinary.Node{Tag: "membership_approval_requests", Content: []waBinary.Node{
			request("15550001111@c.us", "1700000300"), request("222@lid", "1700000100"), request("15550003333@c.us", "1700000200"),
		}}), nil
	}}
	d := groupOperationDeps(t, r)
	d.contact = func(_ context.Context, j types.JID) (types.ContactInfo, error) {
		if j.User == "222" {
			return types.ContactInfo{Found: true, PushName: "Pushed"}, nil
		}
		return types.ContactInfo{}, nil
	}
	result, err := runOperation(context.Background(), operationJob("list_join_requests", operationsGroup, map[string]any{"limit": 2}), d)
	data := operationResult(t, result, err, "confirmed")
	want := map[string]any{"has_more": true, "untrusted": []any{"name"}, "requests": []any{
		map[string]any{"id": "222@lid", "requested_at": int64(1700000100), "name": "Pushed"},
		map[string]any{"id": "15550003333@c.us", "requested_at": int64(1700000200), "name": nil},
	}}
	if !reflect.DeepEqual(data, want) {
		t.Fatalf("%#v", data)
	}
}

// spec: ANYM-242
func TestChatInfoGroupSettings(t *testing.T) {
	r := &recordingIQ{reply: func(int, whatsmeow.DangerousInfoQuery) (*waBinary.Node, error) {
		return groupWith(waBinary.Node{Tag: "announcement"}, waBinary.Node{Tag: "membership_approval_mode", Content: []waBinary.Node{{Tag: "group_join", Attrs: waBinary.Attrs{"state": "on"}}}}), nil
	}}
	result, err := runOperation(context.Background(), operationJob("chat_info", operationsGroup, nil), groupOperationDeps(t, r))
	data := operationResult(t, result, err, "confirmed")
	want := map[string]any{"messages_admins_only": true, "info_admins_only": false, "slow_mode_seconds": nil, "join_approval": true}
	if !reflect.DeepEqual(data["group_settings"], want) {
		t.Fatalf("%#v", data["group_settings"])
	}
	result, err = runOperation(context.Background(), operationJob("chat_info", operationsChat, nil), groupOperationDeps(t, r))
	if data := operationResult(t, result, err, "confirmed"); data["group_settings"] != nil {
		t.Fatalf("private chat settings %#v", data["group_settings"])
	}
}

// spec: ANYM-242
func TestJoinRequestsProjectionStaysWithinReceiptBudget(t *testing.T) {
	long := strings.Repeat("名", 300) + strings.Repeat("<\"& ", 53)
	for _, limit := range []int{50, 100} {
		r := &recordingIQ{reply: func(int, whatsmeow.DangerousInfoQuery) (*waBinary.Node, error) {
			requests := make([]waBinary.Node, 100)
			for i := range requests {
				j, _ := ParseChatID(fmt.Sprintf("1555000%04d@c.us", i))
				requests[i] = waBinary.Node{Tag: "membership_approval_request", Attrs: waBinary.Attrs{"jid": j, "request_time": fmt.Sprint(1700000000 + i)}}
			}
			return iqResult(waBinary.Node{Tag: "membership_approval_requests", Content: requests}), nil
		}}
		d := groupOperationDeps(t, r)
		d.contact = func(context.Context, types.JID) (types.ContactInfo, error) {
			return types.ContactInfo{Found: true, FullName: long}, nil
		}
		result, err := runOperation(context.Background(), operationJob("list_join_requests", operationsGroup, map[string]any{"limit": limit}), d)
		data := operationResult(t, result, err, "confirmed")
		rows := data["requests"].([]any)
		encoded, _ := json.Marshal(result)
		if len(rows) == 0 || len(rows) >= limit || data["has_more"] != true || len(encoded) > joinRequestsBytes {
			t.Fatalf("limit %d: %d rows, has_more=%v, %d bytes", limit, len(rows), data["has_more"], len(encoded))
		}
		if rows[0].(map[string]any)["name"] != truncate(long, 512) {
			t.Fatal("a fitting row lost its name")
		}
	}
}
