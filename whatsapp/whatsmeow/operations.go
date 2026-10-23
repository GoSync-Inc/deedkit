package main

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strings"
	"syscall"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

var operationFields = map[string]string{
	"request_chat_history": "count",
	"chat_info":            "", "list_participants": "offset limit", "get_reactions": "offset limit", "get_message_info": "",
	"get_profile": "", "list_contacts": "offset limit", "get_contact": "user_id", "list_blocked": "offset limit", "get_pinned_messages": "offset limit",
	"send_file": "attachment_id upload_id sha256 mode caption", "forward_message": "source_message_id source_revision", "send_poll": "question options multiple",
	"send_contact": "contact_ids", "send_location": "latitude longitude description", "edit_message": "text", "delete_message": "revoke",
	"send_reaction": "emoji", "pin_message": "seconds", "unpin_message": "", "mark_read": "", "mark_unread": "", "archive_chat": "", "unarchive_chat": "",
	"mute_chat": "seconds", "unmute_chat": "", "pin_chat": "", "unpin_chat": "", "star_message": "", "unstar_message": "", "create_group": "title user_ids",
	"rename_group": "title", "set_group_description": "description", "add_participant": "user_id", "remove_participant": "user_id", "promote_participant": "user_id",
	"demote_participant": "user_id", "leave_group": "", "get_invite_link": "", "revoke_invite_link": "", "add_contact": "phone first_name last_name",
	"delete_contact": "user_id", "block_contact": "user_id", "unblock_contact": "user_id", "update_profile": "name status", "get_labels": "offset limit", "set_chat_labels": "label_ids",
	"open_chat_by_phone": "phone create", "set_group_messages_admins_only": "enabled", "set_group_info_admins_only": "enabled", "set_join_approval": "enabled",
	"list_join_requests": "limit", "approve_join_requests": "user_ids", "reject_join_requests": "user_ids",
	"check_chat_link": "link", "join_chat": "link",
}
var globalOperations = wordSet("get_profile list_contacts list_blocked get_contact block_contact unblock_contact delete_contact create_group add_contact update_profile get_labels open_chat_by_phone check_chat_link join_chat")
var groupOperations = wordSet("list_participants rename_group set_group_description add_participant remove_participant promote_participant demote_participant leave_group get_invite_link revoke_invite_link set_group_messages_admins_only set_group_info_admins_only set_join_approval list_join_requests approve_join_requests reject_join_requests")
var readOperations = wordSet("check_chat_link list_join_requests chat_info list_participants get_reactions get_message_info get_profile list_contacts get_contact list_blocked get_pinned_messages get_labels")
var userPattern = regexp.MustCompile(`^[0-9]{1,30}@(c\.us|lid)$`)
var shaPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)
var mimePattern = regexp.MustCompile(`(?i)^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$`)
var phonePattern = regexp.MustCompile(`^[1-9][0-9]{6,14}$`)

// One usync lookup ends well before the Python worker's 30 s operation timeout,
// so a stalled query is a complete error frame, never a desynchronized pipe.
const phoneLookupTimeout = 10 * time.Second

// The collector sends at most the one archived chat it checked for the local LID.
const maxKnownChats = 20

func wordSet(value string) map[string]bool {
	out := map[string]bool{}
	for _, v := range strings.Fields(value) {
		out[v] = true
	}
	return out
}

type operationRequest struct {
	Operation             string                     `json:"operation"`
	Parameters            map[string]json.RawMessage `json:"parameters"`
	ProviderChat          string                     `json:"provider_chat"`
	ProviderMessageID     string                     `json:"provider_message_id"`
	ExpiresAt             float64                    `json:"expires_at"`
	SourceProviderChat    string                     `json:"source_provider_chat"`
	SourceProviderMessage string                     `json:"source_provider_message"`
	TrustedBlobDirectory  string                     `json:"trusted_blob_directory"`
	RegisteredFilePath    string                     `json:"registered_file_path"`
	FileDigest            string                     `json:"file_digest"`
	FileBytes             int64                      `json:"file_bytes"`
	FileMime              string                     `json:"file_mime"`
	FileName              string                     `json:"file_name"`
	KnownChats            []string                   `json:"known_chats"`
	LookupAllowed         bool                       `json:"lookup_allowed"`
	LocalOnly             bool                       `json:"local_only"`
	LookupMillis          int64                      `json:"lookup_ms"`
	// ExpectedChat is what the owner confirmed for join_chat (ANYM-243).
	ExpectedChat *expectedChat `json:"expected_chat"`
}

type expectedChat struct {
	Title string `json:"title"`
	Kind  string `json:"kind"`
	// MemberCount is shown in the owner's preview only; a count moves too
	// often to refuse a join over it.
	MemberCount *int `json:"member_count"`
}

type operationDependencies struct {
	sendDependencies
	group            func(context.Context, types.JID) (*types.GroupInfo, error)
	groupSettle      func(context.Context) error
	createGroup      func(context.Context, whatsmeow.ReqCreateGroup) (*types.GroupInfo, error)
	participants     func(context.Context, types.JID, []types.JID, whatsmeow.ParticipantChange) ([]types.GroupParticipant, error)
	groupName        func(context.Context, types.JID, string) error
	groupDescription func(context.Context, types.JID, string, string) error
	leave            func(context.Context, types.JID) error
	invite           func(context.Context, types.JID, bool) (string, error)
	groupSetting     func(context.Context, types.JID, string, bool) error
	joinRequests     func(context.Context, types.JID) ([]types.GroupParticipantRequest, error)
	joinAction       func(context.Context, types.JID, []types.JID, bool) ([]types.GroupParticipant, error)
	linkInfo         func(context.Context, string) (*types.GroupInfo, error)
	joinLink         func(context.Context, string) (types.JID, bool, error)
	contacts         func(context.Context) (map[types.JID]types.ContactInfo, error)
	contact          func(context.Context, types.JID) (types.ContactInfo, error)
	phone            func(context.Context, types.JID) (types.JID, error)
	settings         func(context.Context, types.JID) (types.LocalChatSettings, error)
	blocklist        func(context.Context) (*types.Blocklist, error)
	block            func(context.Context, types.JID, bool) error
	patch            func(context.Context, appstate.PatchInfo) error
	status           func(context.Context, string) error
	upload           func(context.Context, []byte, whatsmeow.MediaType) (whatsmeow.UploadResponse, error)
	receipts         func(string, string) ([]map[string]any, error)
	latest           func(string) (*events.Message, error)
	page             func(string, int, []any) ([]*events.Message, error)
	history          func(context.Context, string, int) (any, error)
	markRead         func(context.Context, []types.MessageID, time.Time, types.JID, types.JID) error
	isOnWhatsApp     func(context.Context, []string) ([]types.IsOnWhatsAppResponse, error)
	lookupTimeout    time.Duration
	ownerName        string
	platform         string
	// Set by runOperation (ANYM-431, ANYM-441).
	gate               *operationGate
	groupOperationDone func()
}

func (d *operationDependencies) useGroupIQ(iq groupIQ) {
	d.group, d.createGroup, d.participants, d.groupName, d.groupDescription, d.leave, d.invite = iq.info, iq.create, iq.participants, iq.setName, iq.setTopic, iq.leave, iq.invite
	d.groupSettle = iq.settle
	d.groupSetting, d.joinRequests, d.joinAction = iq.setSetting, iq.joinRequests, iq.joinRequestAction
	d.linkInfo, d.joinLink = iq.linkInfo, iq.joinLink
}

func (p *Provider) operationDependencies() operationDependencies {
	c := p.Client
	d := operationDependencies{sendDependencies: p.sendDependencies(), contacts: c.Store.Contacts.GetAllContacts, contact: c.Store.Contacts.GetContact,
		settings: c.Store.ChatSettings.GetChatSettings, patch: c.SendAppState, isOnWhatsApp: c.IsOnWhatsApp,
		markRead: func(ctx context.Context, ids []types.MessageID, stamp time.Time, chat, sender types.JID) error {
			return c.MarkRead(ctx, ids, stamp, chat, sender)
		}, upload: c.Upload, latest: p.Capture.Latest, page: p.Capture.Page, receipts: p.Capture.ReceiptObservations,
		ownerName: c.Store.PushName, platform: c.Store.Platform,
		history: func(ctx context.Context, chat string, count int) (any, error) {
			pol, err := p.policy()
			if err != nil {
				return nil, err
			}
			raw, _ := json.Marshal(map[string]any{"chat": chat, "count": count})
			return p.requestHistory(ctx, raw, pol)
		},
		blocklist: func(ctx context.Context) (*types.Blocklist, error) { return c.GetBlocklist(ctx, "") },
		block: func(ctx context.Context, j types.JID, block bool) error {
			action := events.BlocklistChangeActionUnblock
			if block {
				action = events.BlocklistChangeActionBlock
			}
			_, err := c.UpdateBlocklist(ctx, j, action, "")
			return err
		},
		status: func(ctx context.Context, text string) error {
			return c.SetStatusMessage(ctx, types.SetStatusInput{Text: &text})
		},
		phone: func(ctx context.Context, j types.JID) (types.JID, error) {
			if j.Server != types.HiddenUserServer {
				return j, nil
			}
			return c.Store.LIDs.GetPNForLID(ctx, j)
		},
	}
	d.useGroupIQ(newGroupIQ(c, &p.groupRefresh))
	return d
}

func (p *Provider) Operation(ctx context.Context, raw json.RawMessage) (any, error) {
	var req operationRequest
	if err := decodeStrict(raw, &req); err != nil {
		return nil, errors.New("operation_invalid_parameters")
	}
	return runOperation(ctx, req, p.operationDependencies())
}

// fileOperationDeadline bounds send_file, upload plus send, 15 s inside the
// collector's 90 s IPC wait (whatsapp_collector.py) (ANYM-441).
var fileOperationDeadline = 75 * time.Second

func operationDeadline(job operationRequest) time.Duration {
	if job.Operation == "send_file" {
		return fileOperationDeadline
	}
	return groupOperationDeadline
}

// runOperation answers every operation within its deadline even when its
// goroutine waits on a library lock that ignores contexts, so the Python
// worker's timeout never tears down the shared native process. The gate keeps
// a late goroutine from starting a change after the answer; a frame it was
// already writing was answered unknown, and no frame is written twice after a
// reconnect (ANYM-431, ANYM-441).
func runOperation(ctx context.Context, job operationRequest, d operationDependencies) (any, error) {
	return runBounded(ctx, operationDeadline(job), func(ctx context.Context, gate *operationGate) (any, error) {
		d.gate = gate
		return executeOperation(ctx, job, d)
	}, func(started bool) (any, error) {
		if started && job.Operation == "request_chat_history" {
			return operationOutput("unknown", map[string]any{"code": "history_request_unknown", "retry": "explicit_only"}, "")
		}
		if started {
			return operationOutput("unknown", map[string]any{"code": "provider_operation_uncertain"}, "")
		}
		if readOperations[job.Operation] {
			return nil, errors.New("operation_query_failed")
		}
		return nil, errors.New("operation_target_unavailable")
	}, d.groupOperationDone)
}

type operationGateKey struct{}

// startChange opens the change phase of the bounded call that owns ctx; false
// once its deadline answer went out. Outside a bounded call it always allows.
func startChange(ctx context.Context) bool {
	gate, _ := ctx.Value(operationGateKey{}).(*operationGate)
	return gate == nil || gate.start()
}

// runBounded runs fn under deadline with frame retries disabled and answers
// with late(started) when fn is still running at the deadline. done, if set,
// runs after fn returns.
func runBounded(ctx context.Context, deadline time.Duration, fn func(context.Context, *operationGate) (any, error), late func(started bool) (any, error), done func()) (any, error) {
	gate := &operationGate{}
	ctx, cancel := context.WithTimeout(context.WithValue(whatsmeow.WithoutFrameRetry(ctx), operationGateKey{}, gate), deadline)
	type answer struct {
		result any
		err    error
	}
	finished := make(chan answer, 1)
	go func() {
		defer cancel()
		result, err := fn(ctx, gate)
		finished <- answer{result, err}
		if done != nil {
			done()
		}
	}()
	select {
	case a := <-finished:
		return a.result, a.err
	case <-ctx.Done():
	}
	select {
	case a := <-finished:
		return a.result, a.err
	default:
	}
	return late(gate.answer())
}

func operationOutput(outcome string, data map[string]any, id string) (any, error) {
	if data == nil {
		data = map[string]any{}
	}
	result := map[string]any{"outcome": outcome, "data": data}
	if id != "" {
		result["provider_message_id"] = id
	}
	encoded, err := json.Marshal(result)
	if err != nil || len(encoded) > 64*1024 {
		if outcome == "confirmed" {
			return nil, errors.New("operation_result_too_large")
		}
		return map[string]any{"outcome": "unknown", "data": map[string]any{"code": "operation_result_too_large"}}, nil
	}
	return result, nil
}

func executeOperation(ctx context.Context, job operationRequest, d operationDependencies) (result any, err error) {
	allowed, ok := operationFields[job.Operation]
	if !ok {
		return nil, errors.New("operation_unsupported")
	}
	fields := wordSet(allowed)
	for key := range job.Parameters {
		if !fields[key] {
			return nil, errors.New("operation_invalid_parameters")
		}
	}
	if !globalOperations[job.Operation] && job.ProviderChat == "" {
		return nil, errors.New("operation_target_invalid")
	}
	var target types.JID
	if job.ProviderChat != "" {
		target, err = ParseChatID(job.ProviderChat)
		if err != nil {
			return nil, errors.New("operation_target_invalid")
		}
	}
	if groupOperations[job.Operation] && target.Server != types.GroupServer {
		return nil, errors.New("operation_target_invalid")
	}
	check := func(extra string) error {
		p, e := d.policy()
		if e != nil || !p.Enabled {
			return errors.New("operation_disabled")
		}
		enabled := false
		for _, op := range p.OperationsEnabled {
			if op == job.Operation {
				enabled = true
				break
			}
		}
		if !enabled {
			return errors.New("operation_disabled")
		}
		if (job.ProviderChat != "" && !p.Allows(job.ProviderChat)) || (extra != "" && !p.Allows(extra)) {
			return errors.New("operation_target_invalid")
		}
		if math.IsNaN(job.ExpiresAt) || math.IsInf(job.ExpiresAt, 0) || job.ExpiresAt <= float64(time.Now().UnixNano())/1e9 {
			return errors.New("operation_expired")
		}
		return nil
	}
	if err = check(""); err != nil {
		return nil, err
	}
	mutating := false
	defer func() {
		if err == nil {
			return
		}
		if mutating {
			result, err = operationOutput("unknown", map[string]any{"code": "provider_operation_uncertain"}, "")
			return
		}
		switch err.Error() {
		case "history_rate_limited", "history_anchor_unavailable", "operation_unsupported", "operation_invalid_parameters", "operation_disabled", "operation_expired", "operation_target_invalid", "operation_target_unavailable", "operation_result_too_large", "registered_file_invalid",
			"invalid_phone", "identity_unresolved", "chat_not_collected", "provider_timeout", "rate_limited", "operation_query_failed", "forbidden",
			"invite_invalid", "invite_expired", "chat_full", "join_not_allowed", "already_member", "operation_target_changed":
		default:
			if readOperations[job.Operation] {
				err = errors.New("operation_query_failed")
			} else {
				err = errors.New("operation_target_unavailable")
			}
		}
	}()
	mutate := func(extra string, fn func() error) error {
		if e := check(extra); e != nil {
			return e
		}
		// After a deadline answer, no change may start (ANYM-431).
		if d.gate != nil && !d.gate.start() {
			return errors.New("operation_target_unavailable")
		}
		mutating = true
		return fn()
	}
	read := func(data map[string]any, extra string) (any, error) {
		if e := check(extra); e != nil {
			return nil, e
		}
		if job.Operation == "list_contacts" || job.Operation == "list_blocked" {
			if rows, ok := data["items"].([]any); ok {
				for _, item := range rows {
					row, _ := item.(map[string]any)
					id, _ := row["id"].(string)
					if id == "" {
						return nil, errors.New("operation_query_failed")
					}
					if e := check(id); e != nil {
						return nil, e
					}
				}
			}
		}
		return operationOutput("confirmed", data, "")
	}
	submitted := func(data map[string]any) (any, error) { return operationOutput("submitted", data, "") }
	getMessage := func() (*events.Message, error) {
		return exactCaptured(d.sendDependencies, job.ProviderChat, job.ProviderMessageID)
	}
	getGroup := func() (*types.GroupInfo, error) {
		g, e := d.group(ctx, target)
		if e != nil {
			return nil, e
		}
		if g == nil || LegacyChatID(g.JID) != job.ProviderChat {
			return nil, errors.New("operation_target_unavailable")
		}
		return g, nil
	}
	text := func(key string, max int, empty bool, defaultValue *string) (string, error) {
		raw, found := job.Parameters[key]
		if !found && defaultValue != nil {
			return *defaultValue, nil
		}
		var value string
		if !found || decodeValue(raw, &value) != nil || !safeText(value, max, empty) {
			return "", errors.New("operation_invalid_parameters")
		}
		return value, nil
	}
	integer := func(key string, min, max, def int) (int, error) {
		raw, found := job.Parameters[key]
		if !found {
			return def, nil
		}
		var n int
		if decodeValue(raw, &n) != nil || n < min || n > max {
			return 0, errors.New("operation_invalid_parameters")
		}
		return n, nil
	}
	user := func(key string) (types.JID, error) {
		s, e := text(key, 140, false, nil)
		if e != nil || !userPattern.MatchString(s) {
			return types.EmptyJID, errors.New("operation_invalid_parameters")
		}
		return ParseChatID(s)
	}
	users := func(key string, max int) ([]types.JID, error) {
		var values []string
		if decodeValue(job.Parameters[key], &values) != nil || len(values) == 0 || len(values) > max {
			return nil, errors.New("operation_invalid_parameters")
		}
		seen := map[string]bool{}
		out := []types.JID{}
		for _, s := range values {
			if !userPattern.MatchString(s) || seen[s] {
				return nil, errors.New("operation_invalid_parameters")
			}
			seen[s] = true
			j, e := ParseChatID(s)
			if e != nil {
				return nil, errors.New("operation_invalid_parameters")
			}
			out = append(out, j)
		}
		return out, nil
	}
	page := func(items []any, coverage, state string) (map[string]any, error) {
		offset, e := integer("offset", 0, 100000, 0)
		if e != nil {
			return nil, e
		}
		limit, e := integer("limit", 1, 100, 50)
		if e != nil {
			return nil, e
		}
		end := offset + limit
		more := end < len(items)
		if end > len(items) {
			end = len(items)
		}
		start := offset
		if start > len(items) {
			start = len(items)
		}
		var next any
		if more {
			next = offset + limit
		}
		return map[string]any{"items": items[start:end], "offset": offset, "limit": limit, "has_more": more, "next_offset": next, "coverage": coverage, "state": state}, nil
	}
	// Validate pagination even when a cache is unknown or empty.
	if fields["offset"] || fields["limit"] {
		if _, err = page([]any{}, "provider_observation", "unknown"); err != nil {
			return nil, err
		}
	}
	contactRow := func(j types.JID, c types.ContactInfo) map[string]any {
		return map[string]any{"id": LegacyChatID(j), "name": truncate(c.FullName, 512), "push_name": truncate(c.PushName, 512), "short_name": truncate(c.FirstName, 512), "is_business": nil, "is_my_contact": c.FullName != "", "is_blocked": nil, "is_self": j.ToNonAD() == d.owner.ToNonAD()}
	}
	send := func(message *waE2E.Message, extra string) (any, error) {
		var receipt map[string]any
		e := mutate(extra, func() error {
			var e error
			receipt, e = sendAndCaptureReceipt(ctx, d.sendDependencies, target, message)
			return e
		})
		if e != nil {
			return nil, e
		}
		return operationOutput("confirmed", map[string]any{"archive_reference": receipt["archive_reference"]}, receipt["provider_message_id"].(string))
	}
	op := job.Operation
	switch op {
	case "open_chat_by_phone":
		return openChatByPhone(ctx, job, d, check)
	case "request_chat_history":
		count, e := integer("count", 1, 50, 50)
		if e != nil {
			return nil, e
		}
		if e = check(""); e != nil {
			return nil, e
		}
		response, e := d.history(ctx, job.ProviderChat, count)
		if e != nil {
			if e.Error() == "history_request_unknown" {
				return operationOutput("unknown", map[string]any{"code": "history_request_unknown", "retry": "explicit_only"}, "")
			}
			return nil, e
		}
		data, ok := response.(map[string]any)
		if !ok {
			return operationOutput("unknown", map[string]any{"code": "provider_operation_uncertain"}, "")
		}
		return submitted(data)
	case "get_profile":
		return read(map[string]any{"id": LegacyChatID(d.owner), "name": truncate(d.ownerName, 512), "platform": truncate(d.platform, 64), "coverage": "cached_self"}, "")
	case "chat_info":
		settings, e := d.settings(ctx, target)
		if e != nil {
			return nil, e
		}
		data := map[string]any{"id": job.ProviderChat, "name": nil, "is_group": target.Server == types.GroupServer, "is_read_only": nil, "is_locked": nil, "is_muted": nil, "mute_expiration": nil, "is_archived": nil, "is_pinned": nil, "unread_count": nil, "timestamp": nil, "description": nil, "owner_id": nil, "group_settings": nil, "coverage": "provider_observation"}
		if settings.Found {
			data["is_muted"] = settings.MutedUntil.After(time.Now())
			data["mute_expiration"] = settings.MutedUntil.Unix()
			data["is_archived"] = settings.Archived
			data["is_pinned"] = settings.Pinned
		}
		if target.Server == types.GroupServer {
			g, e := getGroup()
			if e != nil {
				return nil, e
			}
			data["name"] = truncate(g.Name, 512)
			data["description"] = truncate(g.Topic, 2048)
			data["owner_id"] = LegacyChatID(g.OwnerJID)
			data["is_locked"] = g.IsLocked
			data["is_read_only"] = g.IsAnnounce
			data["group_settings"] = map[string]any{"messages_admins_only": g.IsAnnounce, "info_admins_only": g.IsLocked, "slow_mode_seconds": nil, "join_approval": g.IsJoinApprovalRequired}
		} else {
			c, e := d.contact(ctx, target)
			if e != nil {
				return nil, e
			}
			data["name"] = truncate(c.FullName, 512)
			if c.FullName == "" {
				data["name"] = truncate(c.PushName, 512)
			}
		}
		return read(data, "")
	case "list_participants":
		g, e := getGroup()
		if e != nil {
			return nil, e
		}
		items := []any{}
		for _, v := range g.Participants {
			items = append(items, map[string]any{"id": LegacyChatID(v.JID), "is_admin": v.IsAdmin, "is_owner": v.IsSuperAdmin})
		}
		data, e := page(items, "provider_observation", "observed")
		if e != nil {
			return nil, e
		}
		return read(data, "")
	case "list_contacts":
		values, e := d.contacts(ctx)
		if e != nil {
			return nil, e
		}
		p, e := d.policy()
		if e != nil {
			return nil, e
		}
		keys := []string{}
		byID := map[string]types.JID{}
		for j := range values {
			key := LegacyChatID(j)
			if key != "" && p.Allows(key) {
				keys = append(keys, key)
				byID[key] = j
			}
		}
		sort.Strings(keys)
		items := []any{}
		for _, key := range keys {
			j := byID[key]
			items = append(items, contactRow(j, values[j]))
		}
		data, e := page(items, "cached_contacts", "observed")
		if e != nil {
			return nil, e
		}
		return read(data, "")
	case "get_contact":
		j, e := user("user_id")
		if e != nil {
			return nil, e
		}
		extra := LegacyChatID(j)
		if e = check(extra); e != nil {
			return nil, e
		}
		c, e := d.contact(ctx, j)
		if e != nil {
			return nil, e
		}
		if !c.Found {
			return nil, errors.New("operation_target_unavailable")
		}
		return read(contactRow(j, c), extra)
	case "list_blocked":
		values, e := d.blocklist(ctx)
		if e != nil {
			return nil, e
		}
		if values == nil {
			return nil, errors.New("operation_query_failed")
		}
		p, e := d.policy()
		if e != nil {
			return nil, e
		}
		items := []any{}
		for _, v := range values.Items {
			j := v.PN
			if j.IsEmpty() {
				j = v.LID
			}
			if v.Active && p.Allows(LegacyChatID(j)) {
				c, e := d.contact(ctx, j)
				if e != nil {
					return nil, e
				}
				row := contactRow(j, c)
				row["is_blocked"] = true
				items = append(items, row)
			}
		}
		data, e := page(items, "provider_observation", "observed")
		if e != nil {
			return nil, e
		}
		return read(data, "")
	case "get_reactions", "get_pinned_messages":
		var original *events.Message
		if op == "get_reactions" {
			original, err = getMessage()
			if err != nil {
				return nil, err
			}
		}
		messages, e := d.page(job.ProviderChat, 100, nil)
		if e != nil {
			return nil, e
		}
		items := []any{}
		// This is intentionally a partial local observation, never a claim that
		// unobserved reactions/pins do not exist on other devices.
		if op == "get_reactions" {
			seen := map[string]bool{}
			for i := len(messages) - 1; i >= 0; i-- {
				m := messages[i]
				r := m.Message.GetReactionMessage()
				if r == nil || r.GetKey().GetID() != original.Info.ID {
					continue
				}
				actor := LegacyChatID(m.Info.Sender)
				if seen[actor] {
					continue
				}
				seen[actor] = true
				items = append(items, map[string]any{"emoji": truncate(r.GetText(), 64), "actor_id": actor, "timestamp": r.GetSenderTimestampMS() / 1000, "removed": r.GetText() == "", "is_by_self": m.Info.IsFromMe, "group_has_reaction_by_self": nil})
			}
		}
		if op == "get_pinned_messages" {
			seen := map[string]bool{}
			for i := len(messages) - 1; i >= 0; i-- {
				pin := messages[i].Message.GetPinInChatMessage()
				if pin == nil || seen[pin.GetKey().GetID()] {
					continue
				}
				seen[pin.GetKey().GetID()] = true
				if pin.GetType() != waE2E.PinInChatMessage_PIN_FOR_ALL {
					continue
				}
				duration := messages[i].Message.GetMessageContextInfo().GetMessageAddOnDurationInSecs()
				if pin.GetSenderTimestampMS() > 0 && duration > 0 && !time.UnixMilli(pin.GetSenderTimestampMS()).Add(time.Duration(duration)*time.Second).After(time.Now()) {
					continue
				}
				for _, m := range messages {
					if m.Info.ID == pin.GetKey().GetID() {
						items = append(items, observedMessageRow(m))
						break
					}
				}
			}
		}
		data, e := page(items, "local_capture", "partial")
		if e != nil {
			return nil, e
		}
		data["completeness"] = "unknown"
		return read(data, "")
	case "get_message_info":
		if _, err = getMessage(); err != nil {
			return nil, err
		}
		observations, e := d.receipts(job.ProviderChat, job.ProviderMessageID)
		if e != nil {
			return nil, e
		}
		state, reason := "unknown", "no_observed_receipts"
		if len(observations) > 0 {
			state, reason = "partial", "observed_receipts_only"
		}
		return read(map[string]any{"state": state, "coverage": "local_capture", "completeness": "unknown", "reason": reason, "observations": observations, "delivery": []any{}, "read": []any{}, "played": []any{}, "delivery_truncated": false, "read_truncated": false, "played_truncated": false, "delivery_remaining": nil, "read_remaining": nil, "played_remaining": nil}, "")
	case "send_location":
		var latitude, longitude float64
		if decodeValue(job.Parameters["latitude"], &latitude) != nil || decodeValue(job.Parameters["longitude"], &longitude) != nil || math.IsNaN(latitude) || math.IsNaN(longitude) || math.Abs(latitude) > 90 || math.Abs(longitude) > 180 {
			return nil, errors.New("operation_invalid_parameters")
		}
		empty := ""
		description, e := text("description", 255, true, &empty)
		if e != nil {
			return nil, e
		}
		return send(&waE2E.Message{LocationMessage: &waE2E.LocationMessage{DegreesLatitude: &latitude, DegreesLongitude: &longitude, Name: &description}}, "")
	case "send_poll":
		question, e := text("question", 255, false, nil)
		if e != nil {
			return nil, e
		}
		var options []string
		var multiple bool
		if decodeValue(job.Parameters["options"], &options) != nil || len(options) < 2 || len(options) > 10 || decodeValue(job.Parameters["multiple"], &multiple) != nil {
			return nil, errors.New("operation_invalid_parameters")
		}
		seen := map[string]bool{}
		for _, s := range options {
			if !safeText(s, 100, false) || seen[s] {
				return nil, errors.New("operation_invalid_parameters")
			}
			seen[s] = true
		}
		count := 1
		if multiple {
			count = len(options)
		}
		return send((&whatsmeow.Client{}).BuildPollCreation(question, options, count), "")
	case "send_contact":
		ids, e := users("contact_ids", 20)
		if e != nil {
			return nil, e
		}
		contacts := []*waE2E.ContactMessage{}
		for _, j := range ids {
			c, e := d.contact(ctx, j)
			if e != nil {
				return nil, e
			}
			pn, e := d.phone(ctx, j)
			if e != nil || pn.IsEmpty() || pn.Server != types.DefaultUserServer {
				return nil, errors.New("operation_target_unavailable")
			}
			name := c.FullName
			if name == "" {
				name = c.PushName
			}
			if name == "" {
				name = pn.User
			}
			name = truncate(name, 512)
			name = strings.NewReplacer("\\", "\\\\", ";", "\\;", ",", "\\,", "\r", "", "\n", "\\n").Replace(name)
			card := fmt.Sprintf("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:%s\r\nTEL;type=CELL;waid=%s:+%s\r\nEND:VCARD", name, pn.User, pn.User)
			contacts = append(contacts, &waE2E.ContactMessage{DisplayName: proto.String(truncate(name, 512)), Vcard: &card})
		}
		return send(&waE2E.Message{ContactsArrayMessage: &waE2E.ContactsArrayMessage{Contacts: contacts}}, "")
	case "send_file":
		mode, e := text("mode", 20, false, nil)
		if e != nil {
			return nil, e
		}
		empty := ""
		caption, e := text("caption", 1024, true, &empty)
		if e != nil {
			return nil, e
		}
		if (mode == "audio" || mode == "voice" || mode == "sticker") && caption != "" {
			return nil, errors.New("operation_unsupported")
		}
		bytes, mime, name, e := registeredFile(job, mode)
		if e != nil {
			return nil, e
		}
		var uploaded whatsmeow.UploadResponse
		kind := whatsmeow.MediaDocument
		switch mode {
		case "photo", "sticker":
			kind = whatsmeow.MediaImage
		case "audio", "voice":
			kind = whatsmeow.MediaAudio
		case "video":
			kind = whatsmeow.MediaVideo
		}
		// The upload changes nothing a chat can see, so it is not a change
		// phase: a deadline during it answers operation_target_unavailable.
		if err = check(""); err != nil {
			return nil, err
		}
		if uploaded, err = d.upload(ctx, bytes, kind); err != nil {
			return nil, err
		}
		if uploaded.FileLength != uint64(len(bytes)) || digestBytes(bytes) != fmt.Sprintf("%x", uploaded.FileSHA256) {
			return nil, errors.New("registered_file_invalid")
		}
		return send(uploadMessage(mode, caption, mime, name, uploaded), "")
	case "forward_message":
		if _, e := ParseChatID(job.SourceProviderChat); e != nil {
			return nil, errors.New("operation_invalid_parameters")
		}
		if err = check(job.SourceProviderChat); err != nil {
			return nil, err
		}
		m, e := exactCaptured(d.sendDependencies, job.SourceProviderChat, job.SourceProviderMessage)
		if e != nil {
			return nil, e
		}
		forward, e := forwardPayload(m)
		if e != nil {
			return nil, e
		}
		return send(forward, job.SourceProviderChat)
	case "edit_message", "delete_message", "send_reaction", "pin_message", "unpin_message", "star_message", "unstar_message", "mark_read":
		m, e := getMessage()
		if e != nil {
			return nil, e
		}
		key := capturedMessageKey(m)
		var wire *waE2E.Message
		switch op {
		case "edit_message":
			value, e := text("text", 4000, false, nil)
			if e != nil {
				return nil, e
			}
			if !m.Info.IsFromMe {
				return nil, errors.New("operation_target_invalid")
			}
			wire = (&whatsmeow.Client{}).BuildEdit(target, m.Info.ID, &waE2E.Message{Conversation: &value})
		case "delete_message":
			if !m.Info.IsFromMe {
				return nil, errors.New("operation_target_invalid")
			}
			if raw, exists := job.Parameters["revoke"]; exists {
				var revoke bool
				if decodeValue(raw, &revoke) != nil || !revoke {
					return nil, errors.New("operation_invalid_parameters")
				}
			}
			wire = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_REVOKE.Enum(), Key: key}}
		case "send_reaction":
			emoji, e := text("emoji", 64, true, nil)
			if e != nil {
				return nil, e
			}
			wire = &waE2E.Message{ReactionMessage: &waE2E.ReactionMessage{Key: key, Text: &emoji, SenderTimestampMS: proto.Int64(time.Now().UnixMilli())}}
		case "pin_message", "unpin_message":
			duration, e := integer("seconds", 1, 2147483647, 86400)
			if e != nil {
				return nil, e
			}
			kind := waE2E.PinInChatMessage_PIN_FOR_ALL
			if op == "unpin_message" {
				kind = waE2E.PinInChatMessage_UNPIN_FOR_ALL
			}
			wire = &waE2E.Message{PinInChatMessage: &waE2E.PinInChatMessage{Key: key, Type: &kind, SenderTimestampMS: proto.Int64(time.Now().UnixMilli())}, MessageContextInfo: &waE2E.MessageContextInfo{MessageAddOnDurationInSecs: proto.Uint32(uint32(duration))}}
		case "star_message", "unstar_message":
			err = mutate("", func() error {
				return d.patch(ctx, appstate.BuildStar(target, m.Info.Sender, m.Info.ID, m.Info.IsFromMe, op == "star_message"))
			})
			if err != nil {
				return nil, err
			}
			return submitted(nil)
		case "mark_read":
			if m.Info.Timestamp.IsZero() {
				return nil, errors.New("operation_target_invalid")
			}
			observed, e := d.page(job.ProviderChat, 100, nil)
			if e != nil {
				return nil, e
			}
			// The request explicitly authorizes whole-chat read state. Receipts are
			// restricted to observed incoming content at/before the reviewed target;
			// equal-second messages with unknown relative order are not inferred read.
			type receiptBatch struct {
				sender types.JID
				ids    []types.MessageID
				stamp  time.Time
			}
			batches := map[string]*receiptBatch{}
			for _, incoming := range observed {
				if incoming == nil || incoming.Message == nil || incoming.Info.IsFromMe || incoming.Info.Chat.ToNonAD() != target.ToNonAD() ||
					incoming.Info.Timestamp.After(m.Info.Timestamp) ||
					(incoming.Info.Timestamp.Equal(m.Info.Timestamp) && incoming.Info.ID != m.Info.ID) || !ordinaryContent(incoming.Message) {
					continue
				}
				sender := incoming.Info.Sender.ToNonAD()
				if sender.IsEmpty() || (sender.Server != types.DefaultUserServer && sender.Server != types.HiddenUserServer) {
					return nil, errors.New("operation_target_invalid")
				}
				key := sender.String()
				batch := batches[key]
				if batch == nil {
					batch = &receiptBatch{sender: sender}
					batches[key] = batch
				}
				batch.ids = append(batch.ids, incoming.Info.ID)
				if incoming.Info.Timestamp.After(batch.stamp) {
					batch.stamp = incoming.Info.Timestamp
				}
			}
			keys := make([]string, 0, len(batches))
			for key := range batches {
				keys = append(keys, key)
			}
			sort.Strings(keys)
			count := 0
			for _, key := range keys {
				batch := batches[key]
				if err = mutate("", func() error { return d.markRead(ctx, batch.ids, batch.stamp, target, batch.sender) }); err != nil {
					return nil, err
				}
				count += len(batch.ids)
			}
			err = mutate("", func() error { return d.patch(ctx, appstate.BuildMarkChatAsRead(target, true, m.Info.Timestamp, key)) })
			if err != nil {
				return nil, err
			}
			return submitted(map[string]any{"scope": "whole_chat", "receipt_scope": "observed_incoming_through_target", "receipt_count": count, "receipt_coverage": "partial"})
		}
		if err = mutate("", func() error { _, e := sendAndCapture(ctx, d.sendDependencies, target, wire); return e }); err != nil {
			return nil, err
		}
		return submitted(nil)
	case "mark_unread", "archive_chat", "unarchive_chat", "mute_chat", "unmute_chat", "pin_chat", "unpin_chat":
		var patch appstate.PatchInfo
		switch op {
		case "mark_unread":
			patch = appstate.BuildMarkChatAsRead(target, false, time.Time{}, nil)
		case "archive_chat", "unarchive_chat":
			patch = appstate.BuildArchive(target, op == "archive_chat", time.Time{}, nil)
		case "mute_chat", "unmute_chat":
			seconds, e := integer("seconds", 1, 2147483647, 86400)
			if e != nil {
				return nil, e
			}
			patch = appstate.BuildMute(target, op == "mute_chat", time.Duration(seconds)*time.Second)
		case "pin_chat", "unpin_chat":
			patch = appstate.BuildPin(target, op == "pin_chat")
		}
		if err = mutate("", func() error { return d.patch(ctx, patch) }); err != nil {
			return nil, err
		}
		return submitted(nil)
	case "create_group":
		title, e := text("title", 128, false, nil)
		if e != nil {
			return nil, e
		}
		ids, e := users("user_ids", 20)
		if e != nil {
			return nil, e
		}
		var g *types.GroupInfo
		if err = mutate("", func() error {
			var e error
			g, e = d.createGroup(ctx, whatsmeow.ReqCreateGroup{Name: title, Participants: ids})
			return e
		}); err != nil {
			return nil, err
		}
		data := map[string]any{}
		if g != nil {
			p, e := d.policy()
			if e == nil && p.Enabled && p.Allows(LegacyChatID(g.JID)) {
				data["provider_chat"] = LegacyChatID(g.JID)
				for key, value := range participantResults(g.Participants, ids) {
					data[key] = value
				}
			}
		}
		return submitted(data)
	case "check_chat_link", "join_chat":
		return chatLink(ctx, job, d, mutate, read, &mutating)
	case "rename_group", "set_group_description", "add_participant", "remove_participant", "promote_participant", "demote_participant", "leave_group", "get_invite_link", "revoke_invite_link":
		g, e := getGroup()
		if e != nil {
			return nil, e
		}
		// The target read's cache refresh lands before the change (ANYM-431).
		if d.groupSettle != nil {
			if e := d.groupSettle(ctx); e != nil {
				return nil, e
			}
		}
		if op == "rename_group" {
			value, e := text("title", 128, false, nil)
			if e != nil {
				return nil, e
			}
			err = mutate("", func() error { return d.groupName(ctx, target, value) })
		} else if op == "set_group_description" {
			value, e := text("description", 255, true, nil)
			if e != nil {
				return nil, e
			}
			err = mutate("", func() error { return d.groupDescription(ctx, target, g.TopicID, value) })
		} else if op == "leave_group" {
			err = mutate("", func() error { return d.leave(ctx, target) })
		} else if op == "get_invite_link" || op == "revoke_invite_link" {
			var link string
			err = mutate("", func() error { var e error; link, e = d.invite(ctx, target, op == "revoke_invite_link"); return e })
			if err != nil {
				return nil, err
			}
			if e := check(""); e != nil {
				return nil, e
			}
			if op == "get_invite_link" && regexp.MustCompile(`^https://chat\.whatsapp\.com/[a-zA-Z0-9_-]{1,128}$`).MatchString(link) {
				return submitted(map[string]any{"invite_link": link})
			}
		} else {
			j, e := user("user_id")
			if e != nil {
				return nil, e
			}
			action := map[string]whatsmeow.ParticipantChange{"add_participant": whatsmeow.ParticipantChangeAdd, "remove_participant": whatsmeow.ParticipantChangeRemove, "promote_participant": whatsmeow.ParticipantChangePromote, "demote_participant": whatsmeow.ParticipantChangeDemote}[op]
			var participants []types.GroupParticipant
			err = mutate("", func() error {
				var e error
				participants, e = d.participants(ctx, target, []types.JID{j}, action)
				return e
			})
			if err != nil {
				return nil, err
			}
			if e := check(""); e != nil {
				return nil, e
			}
			return submitted(participantResults(participants, []types.JID{j}))
		}
		if err != nil {
			return nil, err
		}
		return submitted(nil)
	case "set_group_messages_admins_only", "set_group_info_admins_only", "set_join_approval":
		var enabled bool
		if decodeValue(job.Parameters["enabled"], &enabled) != nil {
			return nil, errors.New("operation_invalid_parameters")
		}
		setting := groupSettingOf[op]
		g, e := getGroup()
		if e != nil {
			return nil, e
		}
		if d.groupSettle != nil {
			if e := d.groupSettle(ctx); e != nil {
				return nil, e
			}
		}
		if !ownGroupAdmin(g, d.owner, d.ownerLID) {
			return nil, errors.New("forbidden")
		}
		previous := groupSettingValue(g, setting)
		if previous == enabled {
			return read(map[string]any{"enabled": enabled, "previous": previous, "changed": false}, "")
		}
		if err = mutate("", func() error { return d.groupSetting(ctx, target, setting, enabled) }); err != nil {
			return nil, err
		}
		// Only a read-back showing the new value confirms the change.
		after, e := d.group(ctx, target)
		if e != nil {
			return nil, e
		}
		if e := check(""); e != nil {
			return nil, e
		}
		data := map[string]any{"enabled": enabled, "previous": previous, "changed": true}
		if after != nil && LegacyChatID(after.JID) == job.ProviderChat && groupSettingValue(after, setting) == enabled {
			return operationOutput("confirmed", data, "")
		}
		return submitted(data)
	case "list_join_requests":
		limit, e := integer("limit", 1, 100, 50)
		if e != nil {
			return nil, e
		}
		requests, e := d.joinRequests(ctx, target)
		if e != nil {
			return nil, e
		}
		slices.SortStableFunc(requests, func(a, b types.GroupParticipantRequest) int { return a.RequestedAt.Compare(b.RequestedAt) })
		more := len(requests) > limit
		if more {
			requests = requests[:limit]
		}
		rows := []any{}
		for _, request := range requests {
			j := request.JID.ToNonAD()
			var name, at any
			if c, e := d.contact(ctx, j); e == nil && c.Found && cmp.Or(c.FullName, c.PushName) != "" {
				name = truncate(cmp.Or(c.FullName, c.PushName), 512)
			}
			if !request.RequestedAt.IsZero() {
				at = request.RequestedAt.Unix()
			}
			row := map[string]any{"id": LegacyChatID(j), "requested_at": at, "name": name}
			// The whole answer stays within the receipt budget; rows that do
			// not fit are reported as more, never as a failed read.
			if joinRequestsSize(append(rows, row)) > joinRequestsBytes {
				more = true
				break
			}
			rows = append(rows, row)
		}
		// Names are the requesters' own text: data, never instructions.
		return read(joinRequestsPage(rows, more), "")
	case "approve_join_requests", "reject_join_requests":
		ids, e := users("user_ids", 20)
		if e != nil {
			return nil, e
		}
		g, e := getGroup()
		if e != nil {
			return nil, e
		}
		if d.groupSettle != nil {
			if e := d.groupSettle(ctx); e != nil {
				return nil, e
			}
		}
		if !ownGroupAdmin(g, d.owner, d.ownerLID) {
			return nil, errors.New("forbidden")
		}
		var answered []types.GroupParticipant
		err = mutate("", func() error {
			var e error
			answered, e = d.joinAction(ctx, target, ids, op == "approve_join_requests")
			return e
		})
		if err != nil {
			return nil, err
		}
		if e := check(""); e != nil {
			return nil, e
		}
		data, proven := joinRequestResults(answered, ids)
		if proven {
			return operationOutput("confirmed", data, "")
		}
		return operationOutput("unknown", data, "")
	case "add_contact":
		return nil, errors.New("operation_unsupported")
	case "block_contact", "unblock_contact":
		j, e := user("user_id")
		if e != nil {
			return nil, e
		}
		extra := LegacyChatID(j)
		if err = mutate(extra, func() error { return d.block(ctx, j, op == "block_contact") }); err != nil {
			return nil, err
		}
		return submitted(nil)
	case "update_profile":
		if len(job.Parameters) == 0 {
			return nil, errors.New("operation_invalid_parameters")
		}
		var name, status *string
		if _, ok := job.Parameters["name"]; ok {
			value, e := text("name", 64, false, nil)
			if e != nil {
				return nil, e
			}
			name = &value
		}
		if _, ok := job.Parameters["status"]; ok {
			value, e := text("status", 139, true, nil)
			if e != nil {
				return nil, e
			}
			status = &value
		}
		if name != nil {
			if err = mutate("", func() error { return d.patch(ctx, appstate.BuildSettingPushName(*name)) }); err != nil {
				return nil, err
			}
		}
		if status != nil {
			if err = mutate("", func() error { return d.status(ctx, *status) }); err != nil {
				return nil, err
			}
		}
		return submitted(nil)
	case "delete_contact", "get_labels", "set_chat_labels":
		return nil, errors.New("operation_unsupported")
	default:
		return nil, errors.New("operation_unsupported")
	}
}

func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}
func capturedMessageKey(m *events.Message) *waCommon.MessageKey {
	key := &waCommon.MessageKey{FromMe: proto.Bool(m.Info.IsFromMe), ID: proto.String(m.Info.ID), RemoteJID: proto.String(m.Info.Chat.String())}
	if m.Info.IsGroup && !m.Info.IsFromMe {
		key.Participant = proto.String(m.Info.Sender.ToNonAD().String())
	}
	return key
}
func observedMessageRow(m *events.Message) map[string]any {
	text := m.Message.GetConversation()
	if text == "" {
		text = m.Message.GetExtendedTextMessage().GetText()
	}
	return map[string]any{"provider_message_id": LegacyMessageID(m), "provider_chat": LegacyChatID(m.Info.Chat), "body": truncate(text, 256), "body_truncated": len([]rune(text)) > 256, "from_me": m.Info.IsFromMe, "timestamp": m.Info.Timestamp.Unix(), "type": "observed", "has_media": mediaPayloadPresent(m.Message), "is_starred": nil}
}
func mediaPayloadPresent(m *waE2E.Message) bool {
	return m.GetImageMessage() != nil || m.GetAudioMessage() != nil || m.GetVideoMessage() != nil || m.GetDocumentMessage() != nil || m.GetStickerMessage() != nil
}

func registeredFile(job operationRequest, mode string) ([]byte, string, string, error) {
	fail := func() ([]byte, string, string, error) { return nil, "", "", errors.New("registered_file_invalid") }
	family, known := map[string]string{"document": "", "photo": "image/", "video": "video/", "audio": "audio/", "voice": "audio/", "sticker": "image/"}[mode]
	if !known {
		return fail()
	}
	if !filepath.IsAbs(job.TrustedBlobDirectory) || !filepath.IsAbs(job.RegisteredFilePath) || !shaPattern.MatchString(job.FileDigest) || job.FileBytes < 1 || job.FileBytes > 32*1024*1024 || filepath.Clean(job.RegisteredFilePath) != filepath.Join(filepath.Clean(job.TrustedBlobDirectory), job.FileDigest) {
		return fail()
	}
	if raw, exists := job.Parameters["sha256"]; exists {
		var value string
		if decodeValue(raw, &value) != nil || value != job.FileDigest {
			return fail()
		}
	}
	mime := job.FileMime
	if mime == "" {
		mime = "application/octet-stream"
	}
	name := job.FileName
	if name == "" {
		name = "attachment-" + job.FileDigest[:12]
	}
	if !safeText(mime, 128, false) || !mimePattern.MatchString(mime) || !safeText(name, 255, false) || strings.ContainsAny(name, "/\\") || (family != "" && !strings.HasPrefix(mime, family)) {
		return fail()
	}
	if mode == "sticker" && mime != "image/webp" {
		return fail()
	}
	real, err := filepath.EvalSymlinks(job.TrustedBlobDirectory)
	if err != nil || real != filepath.Clean(job.TrustedBlobDirectory) {
		return fail()
	}
	info, err := os.Lstat(real)
	if err != nil || !info.IsDir() {
		return fail()
	}
	fd, err := syscall.Open(job.RegisteredFilePath, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return fail()
	}
	file := os.NewFile(uintptr(fd), "registered-blob")
	defer file.Close()
	info, err = file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() != job.FileBytes {
		return fail()
	}
	data, err := io.ReadAll(io.LimitReader(file, job.FileBytes+1))
	if err != nil || int64(len(data)) != job.FileBytes || digestBytes(data) != job.FileDigest {
		return fail()
	}
	return data, mime, name, nil
}

func uploadMessage(mode, caption, mime, name string, u whatsmeow.UploadResponse) *waE2E.Message {
	switch mode {
	case "photo":
		return &waE2E.Message{ImageMessage: &waE2E.ImageMessage{URL: &u.URL, DirectPath: &u.DirectPath, MediaKey: u.MediaKey, FileSHA256: u.FileSHA256, FileEncSHA256: u.FileEncSHA256, FileLength: &u.FileLength, Mimetype: &mime, Caption: &caption}}
	case "video":
		return &waE2E.Message{VideoMessage: &waE2E.VideoMessage{URL: &u.URL, DirectPath: &u.DirectPath, MediaKey: u.MediaKey, FileSHA256: u.FileSHA256, FileEncSHA256: u.FileEncSHA256, FileLength: &u.FileLength, Mimetype: &mime, Caption: &caption}}
	case "audio", "voice":
		return &waE2E.Message{AudioMessage: &waE2E.AudioMessage{URL: &u.URL, DirectPath: &u.DirectPath, MediaKey: u.MediaKey, FileSHA256: u.FileSHA256, FileEncSHA256: u.FileEncSHA256, FileLength: &u.FileLength, Mimetype: &mime, PTT: proto.Bool(mode == "voice")}}
	case "sticker":
		return &waE2E.Message{StickerMessage: &waE2E.StickerMessage{URL: &u.URL, DirectPath: &u.DirectPath, MediaKey: u.MediaKey, FileSHA256: u.FileSHA256, FileEncSHA256: u.FileEncSHA256, FileLength: &u.FileLength, Mimetype: &mime}}
	default:
		return &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{URL: &u.URL, DirectPath: &u.DirectPath, MediaKey: u.MediaKey, FileSHA256: u.FileSHA256, FileEncSHA256: u.FileEncSHA256, FileLength: &u.FileLength, Mimetype: &mime, FileName: &name, Caption: &caption}}
	}
}

func forwardPayload(m *events.Message) (*waE2E.Message, error) {
	if m.IsViewOnce {
		return nil, errors.New("operation_target_invalid")
	}
	// Only ordinary content can be forwarded. Never replay protocol/admin/control
	// messages or destination-specific context, quote metadata, mentions or ads.
	orig := m.Message
	ctx := &waE2E.ContextInfo{IsForwarded: proto.Bool(true), ForwardingScore: proto.Uint32(1)}
	out := &waE2E.Message{}
	switch {
	case orig.GetConversation() != "":
		out.ExtendedTextMessage = &waE2E.ExtendedTextMessage{Text: proto.String(orig.GetConversation()), ContextInfo: ctx}
	case orig.GetExtendedTextMessage() != nil:
		v := proto.Clone(orig.GetExtendedTextMessage()).(*waE2E.ExtendedTextMessage)
		v.ContextInfo = ctx
		out.ExtendedTextMessage = v
	case orig.GetImageMessage() != nil:
		v := proto.Clone(orig.GetImageMessage()).(*waE2E.ImageMessage)
		v.ContextInfo = ctx
		out.ImageMessage = v
	case orig.GetVideoMessage() != nil:
		v := proto.Clone(orig.GetVideoMessage()).(*waE2E.VideoMessage)
		v.ContextInfo = ctx
		out.VideoMessage = v
	case orig.GetAudioMessage() != nil:
		v := proto.Clone(orig.GetAudioMessage()).(*waE2E.AudioMessage)
		v.ContextInfo = ctx
		out.AudioMessage = v
	case orig.GetDocumentMessage() != nil:
		v := proto.Clone(orig.GetDocumentMessage()).(*waE2E.DocumentMessage)
		v.ContextInfo = ctx
		out.DocumentMessage = v
	case orig.GetStickerMessage() != nil:
		v := proto.Clone(orig.GetStickerMessage()).(*waE2E.StickerMessage)
		v.ContextInfo = ctx
		out.StickerMessage = v
	case orig.GetLocationMessage() != nil:
		v := proto.Clone(orig.GetLocationMessage()).(*waE2E.LocationMessage)
		v.ContextInfo = ctx
		out.LocationMessage = v
	case orig.GetContactMessage() != nil:
		v := proto.Clone(orig.GetContactMessage()).(*waE2E.ContactMessage)
		v.ContextInfo = ctx
		out.ContactMessage = v
	case orig.GetContactsArrayMessage() != nil:
		v := proto.Clone(orig.GetContactsArrayMessage()).(*waE2E.ContactsArrayMessage)
		v.ContextInfo = ctx
		out.ContactsArrayMessage = v
	default:
		return nil, errors.New("operation_unsupported")
	}
	return out, nil
}

func ordinaryContent(m *waE2E.Message) bool {
	return m != nil && (m.GetConversation() != "" || m.GetExtendedTextMessage() != nil || mediaPayloadPresent(m) || m.GetLocationMessage() != nil || m.GetContactMessage() != nil || m.GetContactsArrayMessage() != nil || m.GetPollCreationMessage() != nil || m.GetPollCreationMessageV3() != nil)
}

func decodeValue(raw json.RawMessage, out any) error {
	if len(raw) == 0 || strings.TrimSpace(string(raw)) == "null" {
		return errors.New("operation_invalid_parameters")
	}
	return json.Unmarshal(raw, out)
}

// The provider can refuse one member while acknowledging the request itself.
// Keep those observations distinct and never turn an AddRequest into a private
// invitation or disclose its invitation credential.
func participantResults(values []types.GroupParticipant, expected []types.JID) map[string]any {
	rows := []any{}
	truncated := len(values) > 100
	if truncated {
		values = values[:100]
	}
	known := 0
	for _, requested := range expected {
		row := map[string]any{"id": LegacyChatID(requested), "state": "unknown", "requires_invitation": false}
		for _, v := range values {
			if v.JID.ToNonAD() != requested.ToNonAD() && v.PhoneNumber.ToNonAD() != requested.ToNonAD() && v.LID.ToNonAD() != requested.ToNonAD() {
				continue
			}
			known++
			row["state"] = "provider_accepted"
			if v.Error != 0 {
				row["state"] = "refused"
				row["error_code"] = v.Error
			}
			if v.AddRequest != nil {
				row["state"] = "invitation_required"
				row["requires_invitation"] = true
				if !v.AddRequest.Expiration.IsZero() {
					row["invitation_expires_at"] = v.AddRequest.Expiration.Unix()
				}
			}
			break
		}
		rows = append(rows, row)
	}
	state := "observed"
	if truncated || known < len(expected) {
		state = "partial"
	}
	return map[string]any{"participants": rows, "participant_state": state, "participant_coverage": "provider_response", "participants_truncated": truncated, "auto_invite_sent": false}
}

// joinRequestsBytes bounds the encoded list_join_requests answer like the
// Telegram adapter (JOIN_REQUESTS_BYTES), below the worker's 60,000-byte
// receipt limit (commands.py safe_result) and the 64 KiB output limit.
const joinRequestsBytes = 48000

func joinRequestsPage(rows []any, more bool) map[string]any {
	return map[string]any{"requests": rows, "has_more": more, "untrusted": []any{"name"}}
}

// joinRequestsSize is the encoded size of the full answer with these rows. Go
// escapes <, >, & and U+2028/2029 where Python's UTF-8 canonical form does not,
// so it never undercounts the receipt.
func joinRequestsSize(rows []any) int {
	encoded, err := json.Marshal(map[string]any{"outcome": "confirmed", "data": joinRequestsPage(rows, true)})
	if err != nil {
		return math.MaxInt
	}
	return len(encoded)
}

var groupSettingOf = map[string]string{"set_group_messages_admins_only": groupSettingAnnounce, "set_group_info_admins_only": groupSettingLocked, "set_join_approval": groupSettingJoinApproval}

func groupSettingValue(g *types.GroupInfo, setting string) bool {
	switch setting {
	case groupSettingAnnounce:
		return g.IsAnnounce
	case groupSettingLocked:
		return g.IsLocked
	}
	return g.IsJoinApprovalRequired
}

// inviteCode is a WhatsApp group invite link or its bare code; no URL is
// fetched, and channel links, query strings and fragments are refused.
var inviteCode = regexp.MustCompile(`^(?:https://chat\.whatsapp\.com/)?([A-Za-z0-9]{8,64})$`)

// chatLink checks or joins the group behind an invite link (ANYM-243). The
// check reads the link and then the group itself for membership; it returns no
// member list, inviter, description or chat ID. A join repeats the check, then
// sends the join once: a definite IQ refusal is failed, anything else after the
// send is unknown.
func chatLink(ctx context.Context, job operationRequest, d operationDependencies, mutate func(string, func() error) error, read func(map[string]any, string) (any, error), mutating *bool) (any, error) {
	var link string
	raw, found := job.Parameters["link"]
	if !found || decodeValue(raw, &link) != nil {
		return nil, errors.New("operation_invalid_parameters")
	}
	match := inviteCode.FindStringSubmatch(link)
	if match == nil || (job.Operation == "join_chat" && job.ExpectedChat == nil) {
		return nil, errors.New("operation_invalid_parameters")
	}
	code := match[1]
	refused := func(err error) error {
		switch {
		case errors.Is(err, whatsmeow.ErrInviteLinkRevoked):
			return errors.New("invite_expired")
		case errors.Is(err, whatsmeow.ErrInviteLinkInvalid):
			return errors.New("invite_invalid")
		}
		return err
	}
	info, err := d.linkInfo(ctx, code)
	if err != nil {
		return nil, refused(err)
	}
	membership, basis := "unknown", "unverified"
	if g, e := d.group(ctx, info.JID); errors.Is(e, whatsmeow.ErrNotInGroup) {
		membership, basis = "not_member", "not_in_group"
	} else if e == nil && g != nil && ownGroupParticipant(g, d.owner, d.ownerLID, false) {
		membership, basis = "member", "participant_list"
	}
	if job.Operation == "check_chat_link" {
		var count any
		if info.ParticipantCount > 0 {
			count = info.ParticipantCount
		} else if len(info.Participants) > 0 {
			count = len(info.Participants)
		}
		return read(map[string]any{"title": truncate(info.Name, 512), "kind": "group", "member_count": count, "approval_required": info.IsJoinApprovalRequired,
			"paid": false, "membership": membership, "membership_basis": basis}, "")
	}
	switch {
	case membership == "member":
		return nil, errors.New("already_member")
	case membership != "not_member":
		return nil, errors.New("operation_target_unavailable")
	case job.ExpectedChat.Kind != "group" || job.ExpectedChat.Title != truncate(info.Name, 512):
		return nil, errors.New("operation_target_changed")
	}
	var joined types.JID
	var pending bool
	if err := mutate("", func() error { var e error; joined, pending, e = d.joinLink(ctx, code); return e }); err != nil {
		// A definite refusal answered the join itself: nothing was joined.
		if code := refused(err); code != err {
			*mutating = false
			return nil, code
		}
		return nil, err
	}
	if joined != info.JID {
		// The receipt names another group than the one checked: unknown, never confirmed.
		return nil, errors.New("join_receipt_mismatch")
	}
	if pending {
		return operationOutput("confirmed", map[string]any{"state": "pending_approval"}, "")
	}
	data := map[string]any{"state": "joined", "archive": "not_collected", "next_step": "the operator adds this chat to the account's collection policy"}
	if p, e := d.policy(); e == nil && p.Enabled && p.Allows(LegacyChatID(joined)) {
		data = map[string]any{"state": "joined", "archive": "collected", "provider_chat": LegacyChatID(joined)}
	}
	return operationOutput("confirmed", data, "")
}

// ownGroupAdmin reports whether the account itself, by phone JID or LID, is an
// administrator of the group read.
func ownGroupAdmin(g *types.GroupInfo, owner, ownerLID types.JID) bool {
	return ownGroupParticipant(g, owner, ownerLID, true)
}

// ownGroupParticipant reports whether the group read lists the account itself,
// as an administrator when admin is set.
func ownGroupParticipant(g *types.GroupInfo, owner, ownerLID types.JID, admin bool) bool {
	for _, p := range g.Participants {
		if admin && !p.IsAdmin {
			continue
		}
		for _, own := range []types.JID{owner, ownerLID} {
			if own.IsEmpty() {
				continue
			}
			own = own.ToNonAD()
			if p.JID.ToNonAD() == own || (!p.PhoneNumber.IsEmpty() && p.PhoneNumber.ToNonAD() == own) || (!p.LID.IsEmpty() && p.LID.ToNonAD() == own) {
				return true
			}
		}
	}
	return false
}

// joinRequestResults gives one row per requested ID, in request order: applied,
// refused with the provider's error code, or unknown when the answer omits it.
// proven is false when any row is unknown.
func joinRequestResults(values []types.GroupParticipant, expected []types.JID) (map[string]any, bool) {
	if len(values) > 100 {
		values = values[:100]
	}
	rows := make([]any, 0, len(expected))
	applied, proven := 0, true
	for _, requested := range expected {
		want := requested.ToNonAD()
		row := map[string]any{"id": LegacyChatID(requested), "state": "unknown"}
		for _, v := range values {
			if v.JID.ToNonAD() != want && v.PhoneNumber.ToNonAD() != want && v.LID.ToNonAD() != want {
				continue
			}
			if v.Error != 0 {
				row["state"], row["reason"], row["provider_code"] = "refused", "provider_refused", v.Error
			} else {
				row["state"] = "applied"
				applied++
			}
			break
		}
		if row["state"] == "unknown" {
			proven = false
		}
		rows = append(rows, row)
	}
	return map[string]any{"results": rows, "all_applied": applied == len(expected)}, proven
}

// phoneLookupSlot admits one resolution at a time per adapter. A timed-out
// resolution keeps its slot until its store or provider call returns, so wedged
// calls cannot accumulate; the next request gets provider_timeout at once.
var phoneLookupSlot = make(chan struct{}, 1)

// openChatByPhone resolves one number to its canonical LID chat; it never sends
// and never writes the archive (the Python collector materializes the chat).
// The collector first asks with local_only for the LID the session store maps the
// number to, checks that exact chat in the archive, then asks again: a mapping
// whose chat is archived (known_chats) answers locally, even with the lookup
// budget spent; otherwise one usync query runs if the collector granted one
// (lookup_allowed). The session-store reads and the query share one deadline, so
// nothing here can hold the lane until the worker's 30 s timeout disconnects
// the adapter.
func openChatByPhone(ctx context.Context, job operationRequest, d operationDependencies, check func(string) error) (any, error) {
	var phone string
	if raw, found := job.Parameters["phone"]; !found || decodeValue(raw, &phone) != nil || !phonePattern.MatchString(phone) {
		return nil, errors.New("invalid_phone")
	}
	if raw, found := job.Parameters["create"]; found {
		var create bool
		if decodeValue(raw, &create) != nil {
			return nil, errors.New("operation_invalid_parameters")
		}
	}
	if len(job.KnownChats) > maxKnownChats {
		return nil, errors.New("operation_invalid_parameters")
	}
	known := make(map[string]bool, len(job.KnownChats))
	for _, chat := range job.KnownChats {
		known[chat] = true
	}
	// Discovery must be on: a LID unknown before the query cannot be matched
	// against include_chats, and exclusions apply to both namespaces.
	collected := func(chat string) error {
		p, e := d.policy()
		if e != nil || !p.Enabled {
			return errors.New("operation_disabled")
		}
		if !p.DiscoverChats || !p.Allows(chat) {
			return errors.New("chat_not_collected")
		}
		return nil
	}
	pn := types.NewJID(phone, types.DefaultUserServer)
	if e := collected(LegacyChatID(pn)); e != nil {
		return nil, e
	}
	timeout := d.lookupTimeout
	if timeout <= 0 {
		timeout = phoneLookupTimeout
	}
	// The collector passes what is left of the resolution's shared deadline
	// after the local-only call and its archive check.
	if job.LookupMillis < 0 {
		return nil, errors.New("operation_invalid_parameters")
	}
	if job.LookupMillis > 0 && job.LookupMillis < timeout.Milliseconds() {
		timeout = time.Duration(job.LookupMillis) * time.Millisecond
	}
	deadline, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	select {
	case phoneLookupSlot <- struct{}{}:
	default:
		return nil, errors.New("provider_timeout")
	}
	done := make(chan phoneResolution, 1)
	go func() {
		defer func() { <-phoneLookupSlot }()
		done <- resolvePhone(deadline, phone, pn, job, known, d, collected, check)
	}()
	var got phoneResolution
	select {
	case got = <-done:
	case <-deadline.Done():
		return nil, errors.New("provider_timeout")
	}
	if got.err != nil {
		if errors.Is(got.err, context.DeadlineExceeded) || deadline.Err() != nil {
			return nil, errors.New("provider_timeout")
		}
		return nil, got.err
	}
	if job.LocalOnly {
		var local any
		if !got.lid.IsEmpty() {
			local = LegacyChatID(got.lid)
		}
		if e := check(""); e != nil {
			return nil, e
		}
		return operationOutput("confirmed", map[string]any{"local_lid": local}, "")
	}
	if !got.on {
		if e := check(""); e != nil {
			return nil, e
		}
		return operationOutput("confirmed", map[string]any{"on_whatsapp": false, "verified_name": nil, "provider_lookup": true}, "")
	}
	chat := LegacyChatID(got.lid)
	if e := collected(chat); e != nil {
		return nil, e
	}
	if e := check(""); e != nil {
		return nil, e
	}
	return operationOutput("confirmed", map[string]any{"on_whatsapp": true, "verified_name": got.name,
		"provider_chat": chat, "provider_lookup": got.lookup}, "")
}

type phoneResolution struct {
	lid    types.JID
	name   any
	lookup bool
	on     bool
	err    error
}

// resolvePhone does every session-store and provider call of openChatByPhone
// under the caller's deadline. Opt-in, expiry and collection policy are checked
// again right before the number goes to WhatsApp.
func resolvePhone(ctx context.Context, phone string, pn types.JID, job operationRequest, known map[string]bool, d operationDependencies, collected, check func(string) error) phoneResolution {
	var local types.JID
	if d.lidForPN != nil {
		if lid, e := d.lidForPN(ctx, pn); e == nil && lid.Server == types.HiddenUserServer && lid.User != "" {
			local = lid.ToNonAD()
			if e = collected(LegacyChatID(local)); e != nil {
				return phoneResolution{err: e}
			}
			if known[LegacyChatID(local)] {
				return phoneResolution{lid: local, on: true}
			}
		}
	}
	if job.LocalOnly {
		return phoneResolution{lid: local}
	}
	if ctx.Err() != nil {
		return phoneResolution{err: ctx.Err()}
	}
	if !job.LookupAllowed {
		return phoneResolution{err: errors.New("rate_limited")}
	}
	if d.isOnWhatsApp == nil {
		return phoneResolution{err: errors.New("operation_unsupported")}
	}
	if e := check(""); e != nil {
		return phoneResolution{err: e}
	}
	if e := collected(LegacyChatID(pn)); e != nil {
		return phoneResolution{err: e}
	}
	if !local.IsEmpty() {
		if e := collected(LegacyChatID(local)); e != nil {
			return phoneResolution{err: e}
		}
	}
	rows, e := d.isOnWhatsApp(ctx, []string{"+" + phone})
	if e != nil {
		if errors.Is(e, context.DeadlineExceeded) || ctx.Err() != nil {
			return phoneResolution{err: context.DeadlineExceeded}
		}
		return phoneResolution{err: errors.New("operation_query_failed")}
	}
	if len(rows) != 1 {
		return phoneResolution{err: errors.New("operation_query_failed")}
	}
	row := rows[0]
	if !row.PhoneNumber.IsEmpty() && row.PhoneNumber.User != phone {
		return phoneResolution{err: errors.New("operation_query_failed")}
	}
	if !row.IsIn {
		return phoneResolution{lookup: true}
	}
	lid := row.JID.ToNonAD()
	if lid.Server != types.HiddenUserServer && d.lidForPN != nil {
		// IsOnWhatsApp stores the mappings it receives; read the store once more.
		if stored, e := d.lidForPN(ctx, pn); e == nil {
			lid = stored.ToNonAD()
		}
	}
	if lid.Server != types.HiddenUserServer || lid.User == "" {
		return phoneResolution{err: errors.New("identity_unresolved")}
	}
	var name any
	if row.VerifiedName != nil && row.VerifiedName.Details != nil {
		if v := truncate(row.VerifiedName.Details.GetVerifiedName(), 256); safeText(v, 512, false) {
			name = v
		}
	}
	return phoneResolution{lid: lid, name: name, lookup: true, on: true}
}
