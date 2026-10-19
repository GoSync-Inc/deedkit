package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	waBinary "go.mau.fi/whatsmeow/binary"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
)

const testTopicID = "TOPIC1"
const testNewTopicID = "TOPIC2"

var testParser = whatsmeow.NewClient(&store.Device{}, nil).DangerousInternals()

// recordingIQ is a synthetic no-replay transport: it records every query and
// answers from reply; it never touches a provider.
type recordingIQ struct {
	mu      sync.Mutex
	calls   []whatsmeow.DangerousInfoQuery
	reply   func(int, whatsmeow.DangerousInfoQuery) (*waBinary.Node, error)
	refresh []*types.GroupInfo
}

func (r *recordingIQ) groupIQ() groupIQ {
	return groupIQ{
		send: func(_ context.Context, q whatsmeow.DangerousInfoQuery) (*waBinary.Node, error) {
			r.mu.Lock()
			r.calls = append(r.calls, q)
			n := len(r.calls) - 1
			r.mu.Unlock()
			return r.reply(n, q)
		},
		parse: testParser.ParseGroupNode,
		pnForLID: func(_ context.Context, j types.JID) (types.JID, error) {
			return types.NewJID("15557654321", types.DefaultUserServer), nil
		},
		tcToken: func(context.Context, types.JID) ([]byte, error) { return []byte("TCTOKEN"), nil },
		newID:   func() string { return testNewTopicID },
		refresh: func(info *types.GroupInfo) {
			r.mu.Lock()
			r.refresh = append(r.refresh, info)
			r.mu.Unlock()
		},
	}
}

func (r *recordingIQ) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.calls)
}

func iqResult(children ...waBinary.Node) *waBinary.Node {
	return &waBinary.Node{Tag: "iq", Attrs: waBinary.Attrs{"type": "result"}, Content: children}
}

func syntheticGroupNode() waBinary.Node {
	return waBinary.Node{Tag: "group", Attrs: waBinary.Attrs{"id": "15550000000-123456789", "subject": "Synthetic"}, Content: []waBinary.Node{
		{Tag: "participant", Attrs: waBinary.Attrs{"jid": types.NewJID("15551234567", types.DefaultUserServer), "lid": types.NewJID("111", types.HiddenUserServer)}},
		{Tag: "description", Attrs: waBinary.Attrs{"id": testTopicID, "t": "1700000000"}, Content: []waBinary.Node{{Tag: "body", Content: []byte("Old")}}},
	}}
}

// syntheticReply answers a group read with the synthetic group and a change
// with the reply shape of the pinned helper for that node.
func syntheticReply(_ int, q whatsmeow.DangerousInfoQuery) (*waBinary.Node, error) {
	content := q.Content.([]waBinary.Node)[0]
	switch content.Tag {
	case "query", "create":
		return iqResult(syntheticGroupNode()), nil
	case "add", "remove", "promote", "demote":
		return iqResult(waBinary.Node{Tag: content.Tag, Content: []waBinary.Node{{Tag: "participant", Attrs: waBinary.Attrs{"jid": types.NewJID("15551234567", types.DefaultUserServer)}}}}), nil
	case "invite":
		return iqResult(waBinary.Node{Tag: "invite", Attrs: waBinary.Attrs{"code": "SYNTHETIC"}}), nil
	}
	return iqResult(), nil
}

func groupOperationParams(op string) map[string]any {
	switch op {
	case "create_group":
		return map[string]any{"title": "New group", "user_ids": []string{operationsChat}}
	case "rename_group":
		return map[string]any{"title": "New group"}
	case "set_group_description":
		return map[string]any{"description": "Description"}
	case "add_participant", "remove_participant", "promote_participant", "demote_participant":
		return map[string]any{"user_id": operationsChat}
	}
	return map[string]any{}
}

func groupOperationDeps(t *testing.T, r *recordingIQ) operationDependencies {
	d := operationsFake(t)
	d.useGroupIQ(r.groupIQ())
	d.settings = func(context.Context, types.JID) (types.LocalChatSettings, error) {
		return types.LocalChatSettings{}, nil
	}
	return d
}

var groupOperationNames = []string{"list_participants", "chat_info", "create_group", "rename_group", "set_group_description", "add_participant", "remove_participant", "promote_participant", "demote_participant", "leave_group", "get_invite_link", "revoke_invite_link"}

func groupJob(op string) operationRequest {
	chat := operationsGroup
	if op == "create_group" {
		chat = ""
	}
	return operationJob(op, chat, groupOperationParams(op))
}

// Golden nodes transcribed from the pinned group.go (f16f9ca30d6a): getGroupInfo,
// CreateGroup with default ReqCreateGroup options, SetGroupName, SetGroupTopic,
// UpdateGroupParticipants, LeaveGroup and GetGroupInviteLink.
func goldenGroupQueries(op string) []whatsmeow.DangerousInfoQuery {
	group, _ := ParseChatID(operationsGroup)
	user, _ := ParseChatID(operationsChat)
	q := func(kind whatsmeow.DangerousInfoQueryType, to types.JID, node waBinary.Node) whatsmeow.DangerousInfoQuery {
		return whatsmeow.DangerousInfoQuery{Namespace: "w:g2", Type: kind, To: to, Content: []waBinary.Node{node}, NoRetry: true}
	}
	read := q("get", group, waBinary.Node{Tag: "query", Attrs: waBinary.Attrs{"request": "interactive"}})
	participant := waBinary.Node{Tag: "participant", Attrs: waBinary.Attrs{"jid": user}}
	added := waBinary.Node{Tag: "participant", Attrs: waBinary.Attrs{"jid": user}, Content: []waBinary.Node{{Tag: "privacy", Content: []byte("TCTOKEN")}}}
	switch op {
	case "list_participants", "chat_info":
		return []whatsmeow.DangerousInfoQuery{read}
	case "create_group":
		return []whatsmeow.DangerousInfoQuery{q("set", types.GroupServerJID, waBinary.Node{Tag: "create", Attrs: waBinary.Attrs{"subject": "New group"}, Content: []waBinary.Node{
			added,
			{Tag: "member_add_mode", Content: "all_member_add"},
			{Tag: "ephemeral", Attrs: waBinary.Attrs{"expiration": 0}},
			{Tag: "membership_approval_mode", Content: []waBinary.Node{{Tag: "group_join", Attrs: waBinary.Attrs{"state": "off"}}}},
		}})}
	case "rename_group":
		return []whatsmeow.DangerousInfoQuery{read, q("set", group, waBinary.Node{Tag: "subject", Content: []byte("New group")})}
	case "set_group_description":
		return []whatsmeow.DangerousInfoQuery{read, q("set", group, waBinary.Node{Tag: "description", Attrs: waBinary.Attrs{"id": testNewTopicID, "prev": testTopicID}, Content: []waBinary.Node{{Tag: "body", Content: []byte("Description")}}})}
	case "add_participant":
		return []whatsmeow.DangerousInfoQuery{read, q("set", group, waBinary.Node{Tag: "add", Content: []waBinary.Node{added}})}
	case "remove_participant", "promote_participant", "demote_participant":
		return []whatsmeow.DangerousInfoQuery{read, q("set", group, waBinary.Node{Tag: strings.TrimSuffix(op, "_participant"), Content: []waBinary.Node{participant}})}
	case "leave_group":
		return []whatsmeow.DangerousInfoQuery{read, q("set", types.GroupServerJID, waBinary.Node{Tag: "leave", Content: []waBinary.Node{{Tag: "group", Attrs: waBinary.Attrs{"id": group}}}})}
	case "get_invite_link":
		return []whatsmeow.DangerousInfoQuery{read, q("get", group, waBinary.Node{Tag: "invite"})}
	case "revoke_invite_link":
		return []whatsmeow.DangerousInfoQuery{read, q("set", group, waBinary.Node{Tag: "invite"})}
	}
	return nil
}

func moduleDir(t *testing.T) string {
	t.Helper()
	out, err := exec.Command("go", "list", "-m", "-f", "{{.Dir}}", "go.mau.fi/whatsmeow").Output()
	if err != nil || len(bytes.TrimSpace(out)) == 0 {
		t.Fatalf("module dir: %v", err)
	}
	return string(bytes.TrimSpace(out))
}

func functionBody(t *testing.T, path, signature string) string {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	source := string(raw)
	start := strings.Index(source, signature)
	if start < 0 {
		t.Fatalf("%s missing from %s", signature, path)
	}
	body := source[start:]
	if end := strings.Index(body[1:], "\nfunc "); end >= 0 {
		body = body[:end+1]
	}
	return body
}

// spec: ANYM-431
func TestGroupOperationsUseNoReplayIQ(t *testing.T) {
	for _, op := range groupOperationNames {
		t.Run(op, func(t *testing.T) {
			r := &recordingIQ{reply: syntheticReply}
			if _, err := runOperation(context.Background(), groupJob(op), groupOperationDeps(t, r)); err != nil {
				t.Fatal(err)
			}
			want := goldenGroupQueries(op)
			if len(r.calls) != len(want) {
				t.Fatalf("calls=%d want %d", len(r.calls), len(want))
			}
			for i, got := range r.calls {
				if !got.NoRetry || got.Timeout <= 0 || got.Timeout > 20*time.Second {
					t.Fatalf("query %d may replay or wait too long: NoRetry=%v Timeout=%s", i, got.NoRetry, got.Timeout)
				}
				got.Timeout = 0
				if !reflect.DeepEqual(got, want[i]) {
					t.Fatalf("query %d differs from the pinned helper:\n got %#v\nwant %#v", i, got, want[i])
				}
			}
		})
	}
	t.Run("empty description deletes", func(t *testing.T) {
		r := &recordingIQ{reply: syntheticReply}
		group, _ := ParseChatID(operationsGroup)
		if err := r.groupIQ().setTopic(context.Background(), group, testTopicID, ""); err != nil {
			t.Fatal(err)
		}
		want := waBinary.Node{Tag: "description", Attrs: waBinary.Attrs{"id": testNewTopicID, "prev": testTopicID, "delete": "true"}, Content: []waBinary.Node(nil)}
		if got := r.calls[0].Content.([]waBinary.Node)[0]; !reflect.DeepEqual(got, want) {
			t.Fatalf("delete node %#v", got)
		}
	})
	t.Run("LID participant carries phone number", func(t *testing.T) {
		r := &recordingIQ{reply: syntheticReply}
		group, _ := ParseChatID(operationsGroup)
		lid := types.NewJID("111", types.HiddenUserServer)
		if _, err := r.groupIQ().participants(context.Background(), group, []types.JID{lid}, whatsmeow.ParticipantChangeAdd); err != nil {
			t.Fatal(err)
		}
		want := waBinary.Node{Tag: "add", Content: []waBinary.Node{{Tag: "participant", Attrs: waBinary.Attrs{"jid": lid, "phone_number": types.NewJID("15557654321", types.DefaultUserServer)}, Content: []waBinary.Node{{Tag: "privacy", Content: []byte("TCTOKEN")}}}}}
		if got := r.calls[0].Content.([]waBinary.Node)[0]; !reflect.DeepEqual(got, want) {
			t.Fatalf("add node %#v", got)
		}
	})
	t.Run("pinned whatsmeow version", func(t *testing.T) {
		raw, err := os.ReadFile("go.mod")
		if err != nil || !bytes.Contains(raw, []byte("go.mau.fi/whatsmeow v0.0.0-20261014094007-f16f9ca30d6a\n")) {
			t.Fatal("whatsmeow changed: re-derive the golden group nodes from the new group.go, then update this pin")
		}
	})
	t.Run("no library group helper", func(t *testing.T) {
		helpers := regexp.MustCompile(`\.(CreateGroup|SetGroupName|SetGroupDescription|SetGroupTopic|UpdateGroupParticipants|LeaveGroup|GetGroupInviteLink|GetGroupInfo)\b`)
		files, _ := filepath.Glob("*.go")
		for _, file := range files {
			if strings.HasSuffix(file, "_test.go") {
				continue
			}
			raw, _ := os.ReadFile(file)
			if match := helpers.Find(raw); match != nil {
				t.Fatalf("%s uses the replaying library helper %s", file, match)
			}
		}
	})
}

// spec: ANYM-431
func TestGroupIQResponseParity(t *testing.T) {
	group, _ := ParseChatID(operationsGroup)
	ctx := context.Background()
	reply := func(node *waBinary.Node, err error) *recordingIQ {
		return &recordingIQ{reply: func(int, whatsmeow.DangerousInfoQuery) (*waBinary.Node, error) { return node, err }}
	}
	t.Run("info", func(t *testing.T) {
		r := reply(iqResult(syntheticGroupNode()), nil)
		info, err := r.groupIQ().info(ctx, group)
		if err != nil || info.JID != group || info.TopicID != testTopicID || len(info.Participants) != 1 || len(r.refresh) != 1 || r.refresh[0] != info {
			t.Fatalf("info %#v %v refresh=%d", info, err, len(r.refresh))
		}
		if _, err = reply(iqResult(), nil).groupIQ().info(ctx, group); !errors.As(err, new(*whatsmeow.ElementMissingError)) {
			t.Fatalf("missing group node: %v", err)
		}
		_, err = reply(nil, &whatsmeow.IQError{Code: 404, Text: "item-not-found"}).groupIQ().info(ctx, group)
		if !errors.Is(err, whatsmeow.ErrGroupNotFound) || !errors.Is(err, whatsmeow.ErrIQNotFound) {
			t.Fatalf("404: %v", err)
		}
		_, err = reply(nil, &whatsmeow.IQError{Code: 403, Text: "forbidden"}).groupIQ().info(ctx, group)
		if !errors.Is(err, whatsmeow.ErrNotInGroup) || !errors.Is(err, whatsmeow.ErrIQForbidden) {
			t.Fatalf("403: %v", err)
		}
	})
	t.Run("create", func(t *testing.T) {
		r := reply(iqResult(syntheticGroupNode()), nil)
		info, err := r.groupIQ().create(ctx, whatsmeow.ReqCreateGroup{Name: "New group"})
		if err != nil || info.JID != group || len(r.refresh) != 0 {
			t.Fatalf("create %#v %v", info, err)
		}
		if _, err = reply(iqResult(), nil).groupIQ().create(ctx, whatsmeow.ReqCreateGroup{Name: "x"}); !errors.As(err, new(*whatsmeow.ElementMissingError)) {
			t.Fatalf("missing group node: %v", err)
		}
	})
	t.Run("participants", func(t *testing.T) {
		pn := types.NewJID("15551234567", types.DefaultUserServer)
		lid := types.NewJID("222", types.HiddenUserServer)
		lidPN := types.NewJID("15559876543", types.DefaultUserServer)
		node := iqResult(waBinary.Node{Tag: "add", Content: []waBinary.Node{
			{Tag: "participant", Attrs: waBinary.Attrs{"jid": pn, "lid": types.NewJID("111", types.HiddenUserServer), "type": "admin"}},
			{Tag: "participant", Attrs: waBinary.Attrs{"jid": lid, "phone_number": lidPN, "error": "403"}, Content: []waBinary.Node{{Tag: "add_request", Attrs: waBinary.Attrs{"code": "SECRET_INVITE_CREDENTIAL", "expiration": "1700000000"}}}},
			{Tag: "participant", Attrs: waBinary.Attrs{"jid": types.NewJID("15550001111", types.DefaultUserServer), "error": "409"}},
		}})
		got, err := reply(node, nil).groupIQ().participants(ctx, group, []types.JID{pn}, whatsmeow.ParticipantChangeAdd)
		want := []types.GroupParticipant{
			{JID: pn, PhoneNumber: pn, LID: types.NewJID("111", types.HiddenUserServer), IsAdmin: true},
			{JID: lid, LID: lid, PhoneNumber: lidPN, Error: 403, AddRequest: &types.GroupParticipantAddRequest{Code: "SECRET_INVITE_CREDENTIAL", Expiration: time.Unix(1700000000, 0)}},
			{JID: types.NewJID("15550001111", types.DefaultUserServer), PhoneNumber: types.NewJID("15550001111", types.DefaultUserServer), Error: 409},
		}
		if err != nil || !reflect.DeepEqual(got, want) {
			t.Fatalf("participants %#v %v", got, err)
		}
		if _, err = reply(iqResult(), nil).groupIQ().participants(ctx, group, []types.JID{pn}, whatsmeow.ParticipantChangeAdd); !errors.As(err, new(*whatsmeow.ElementMissingError)) {
			t.Fatalf("missing action node: %v", err)
		}
		// Projection: the LID member matches by phone number, the credential stays private,
		// and a requested member missing from the reply stays unknown.
		for user, state := range map[string]string{"15559876543@c.us": "invitation_required", "15550002222@c.us": "unknown"} {
			r := &recordingIQ{reply: func(n int, q whatsmeow.DangerousInfoQuery) (*waBinary.Node, error) {
				if n == 0 {
					return syntheticReply(n, q)
				}
				return node, nil
			}}
			result, err := runOperation(ctx, operationJob("add_participant", operationsGroup, map[string]any{"user_id": user}), groupOperationDeps(t, r))
			data := operationResult(t, result, err, "submitted")
			row := data["participants"].([]any)[0].(map[string]any)
			encoded, _ := json.Marshal(data)
			if row["state"] != state || bytes.Contains(encoded, []byte("SECRET_INVITE_CREDENTIAL")) {
				t.Fatalf("%s: %s", user, encoded)
			}
		}
	})
	t.Run("invite", func(t *testing.T) {
		link, err := reply(iqResult(waBinary.Node{Tag: "invite", Attrs: waBinary.Attrs{"code": "ABC"}}), nil).groupIQ().invite(ctx, group, false)
		if err != nil || link != "https://chat.whatsapp.com/ABC" {
			t.Fatalf("invite %q %v", link, err)
		}
		if _, err = reply(iqResult(), nil).groupIQ().invite(ctx, group, false); err == nil {
			t.Fatal("missing code accepted")
		}
		for code, want := range map[int]error{401: whatsmeow.ErrGroupInviteLinkUnauthorized, 403: whatsmeow.ErrNotInGroup, 404: whatsmeow.ErrGroupNotFound} {
			iq := map[int]*whatsmeow.IQError{401: {Code: 401, Text: "not-authorized"}, 403: {Code: 403, Text: "forbidden"}, 404: {Code: 404, Text: "item-not-found"}}[code]
			_, err = reply(nil, iq).groupIQ().invite(ctx, group, true)
			if !errors.Is(err, want) || !errors.Is(err, iq) {
				t.Fatalf("%d: %v", code, err)
			}
		}
	})
	t.Run("stores and warm cache", func(t *testing.T) {
		client := whatsmeow.NewClient(&store.Device{}, nil).DangerousInternals()
		lid := types.NewJID("111", types.HiddenUserServer)
		client.CacheGroupInfo(&types.GroupInfo{JID: group, Participants: []types.GroupParticipant{{JID: types.NewJID("15550009999", types.DefaultUserServer)}}}, true)
		r := reply(iqResult(syntheticGroupNode()), nil)
		info, err := r.groupIQ().info(ctx, group)
		if err != nil {
			t.Fatal(err)
		}
		info.AddressingMode = types.AddressingModeLID
		var lids []store.LIDMapping
		var warnings []string
		storeGroupInfo(info, client.CacheGroupInfo,
			func(_ context.Context, m []store.LIDMapping) error {
				lids = m
				return errors.New("synthetic storage failure")
			},
			func(context.Context, []store.RedactedPhoneEntry) error {
				warnings = append(warnings, "phones stored")
				return nil
			},
			func(format string, _ ...any) { warnings = append(warnings, format) })
		if len(lids) != 1 || lids[0].LID != lid || len(warnings) != 2 || !strings.Contains(warnings[0], "LID mappings") {
			t.Fatalf("stores lids=%#v warnings=%v", lids, warnings)
		}
		cached, err := client.GetCachedGroupData(ctx, group)
		if err != nil || cached.AddressingMode != types.AddressingModeLID || len(cached.Members) != 1 || cached.Members[0] != types.NewJID("15551234567", types.DefaultUserServer) {
			t.Fatalf("warm cache not refreshed: %#v %v", cached, err)
		}
	})
}

// spec: ANYM-431
func TestGroupIQPhaseOutcomes(t *testing.T) {
	for _, op := range groupOperationNames {
		for step := range goldenGroupQueries(op) {
			r := &recordingIQ{reply: func(n int, q whatsmeow.DangerousInfoQuery) (*waBinary.Node, error) {
				if n == step {
					return nil, &whatsmeow.DisconnectedError{Action: "info query"}
				}
				return syntheticReply(n, q)
			}}
			result, err := runOperation(context.Background(), groupJob(op), groupOperationDeps(t, r))
			if r.count() != step+1 {
				t.Fatalf("%s step %d: %d sends, want exactly one per query", op, step, r.count())
			}
			change := op == "create_group" || step == 1
			switch {
			case change:
				data := operationResult(t, result, err, "unknown")
				if data["code"] != "provider_operation_uncertain" {
					t.Fatalf("%s change drop: %#v", op, data)
				}
			case readOperations[op]:
				if err == nil || err.Error() != "operation_query_failed" {
					t.Fatalf("%s read drop: %v %#v", op, err, result)
				}
			default:
				if err == nil || err.Error() != "operation_target_unavailable" {
					t.Fatalf("%s target read drop: %v %#v", op, err, result)
				}
			}
		}
	}
}

// spec: ANYM-431
func TestPinnedNoRetryContract(t *testing.T) {
	body := functionBody(t, filepath.Join(moduleDir(t), "request.go"), "func (cli *Client) sendIQ(")
	noRetry := strings.Index(body, "if query.NoRetry {")
	retry := strings.Index(body, "cli.retryFrame(")
	if noRetry < 0 || retry < 0 || noRetry > retry || !strings.HasPrefix(strings.TrimSpace(body[noRetry+len("if query.NoRetry {"):]), "return nil, &DisconnectedError{") {
		t.Fatal("pinned sendIQ no longer returns before retryFrame for NoRetry queries")
	}
}

// spec: ANYM-431
func TestPinnedNoiseSocketContextCheck(t *testing.T) {
	body := functionBody(t, filepath.Join(moduleDir(t), "socket", "noisesocket.go"), "func (ns *NoiseSocket) SendFrame(")
	lock := strings.Index(body, "ns.writeLock.Lock()")
	check := strings.Index(body, "if ctx.Err() != nil {")
	write := strings.Index(body, "ns.fs.SendFrame(")
	if lock < 0 || check < lock || write < check {
		t.Fatal("pinned NoiseSocket.SendFrame no longer checks the context after taking writeLock and before writing")
	}
}

func shortGroupDeadline(t *testing.T) {
	old := groupOperationDeadline
	groupOperationDeadline = 50 * time.Millisecond
	t.Cleanup(func() { groupOperationDeadline = old })
}

// spec: ANYM-431
func TestGroupOperationDeadline(t *testing.T) {
	shortGroupDeadline(t)
	for _, op := range groupOperationNames {
		for step := range goldenGroupQueries(op) {
			release := make(chan struct{})
			// The blocked call ignores its context, like a lock that does.
			r := &recordingIQ{reply: func(n int, q whatsmeow.DangerousInfoQuery) (*waBinary.Node, error) {
				if n == step {
					<-release
				}
				return syntheticReply(n, q)
			}}
			d := groupOperationDeps(t, r)
			started := time.Now()
			result, err := runOperation(context.Background(), groupJob(op), d)
			if elapsed := time.Since(started); elapsed > 2*time.Second {
				t.Fatalf("%s step %d answered after %s", op, step, elapsed)
			}
			change := op == "create_group" || step == 1
			if change {
				if data := operationResult(t, result, err, "unknown"); data["code"] != "provider_operation_uncertain" {
					t.Fatalf("%s: %#v", op, data)
				}
			} else if err == nil {
				t.Fatalf("%s step %d: %#v", op, step, result)
			}
			// The same provider keeps serving while the first goroutine is blocked.
			next := &recordingIQ{reply: syntheticReply}
			result, err = runOperation(context.Background(), groupJob("list_participants"), groupOperationDeps(t, next))
			operationResult(t, result, err, "confirmed")
			close(release)
		}
	}
}

// spec: ANYM-431
func TestGroupDeadlineStartGate(t *testing.T) {
	shortGroupDeadline(t)
	for _, op := range []string{"rename_group", "set_group_description", "add_participant", "leave_group", "revoke_invite_link"} {
		release := make(chan struct{})
		finished := make(chan struct{})
		r := &recordingIQ{reply: func(n int, q whatsmeow.DangerousInfoQuery) (*waBinary.Node, error) {
			if n == 0 {
				<-release
			}
			return syntheticReply(n, q)
		}}
		d := groupOperationDeps(t, r)
		d.groupOperationDone = func() { close(finished) }
		_, err := runOperation(context.Background(), groupJob(op), d)
		if err == nil || err.Error() != "operation_target_unavailable" {
			t.Fatalf("%s: %v", op, err)
		}
		close(release)
		select {
		case <-finished:
		case <-time.After(5 * time.Second):
			t.Fatalf("%s goroutine never finished", op)
		}
		if r.count() != 1 {
			t.Fatalf("%s sent a change after the deadline answer: %d sends", op, r.count())
		}
	}
}

// spec: ANYM-431
func TestGroupDeadlineScope(t *testing.T) {
	deadline := func(t *testing.T, job operationRequest, d operationDependencies, hasDeadline bool) {
		t.Helper()
		seen := false
		d.settings = func(ctx context.Context, _ types.JID) (types.LocalChatSettings, error) {
			_, seen = ctx.Deadline()
			return types.LocalChatSettings{}, nil
		}
		d.contact = func(ctx context.Context, _ types.JID) (types.ContactInfo, error) {
			_, seen = ctx.Deadline()
			return types.ContactInfo{Found: true}, nil
		}
		if _, err := runOperation(context.Background(), job, d); err != nil {
			t.Fatal(err)
		}
		if seen != hasDeadline {
			t.Fatalf("%s deadline=%v want %v", job.Operation, seen, hasDeadline)
		}
	}
	r := &recordingIQ{reply: syntheticReply}
	deadline(t, operationJob("chat_info", operationsGroup, nil), groupOperationDeps(t, r), true)
	deadline(t, operationJob("chat_info", operationsChat, nil), groupOperationDeps(t, r), false)
	deadline(t, operationJob("get_contact", "", map[string]any{"user_id": operationsChat}), groupOperationDeps(t, r), false)
	for _, op := range []string{"send_file", "request_chat_history", "mark_read", "archive_chat", "block_contact"} {
		if groupBounded(operationJob(op, operationsChat, nil)) {
			t.Fatalf("%s must keep its own bounds", op)
		}
	}
}
