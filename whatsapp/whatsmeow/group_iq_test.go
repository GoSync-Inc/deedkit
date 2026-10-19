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
	waLog "go.mau.fi/whatsmeow/util/log"
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
		helpers := regexp.MustCompile(`\.(CreateGroup|SetGroupName|SetGroupDescription|SetGroupTopic|UpdateGroupParticipants|LeaveGroup|GetGroupInviteLink|GetGroupInfo|SendIQ)\b`)
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
		if !errors.Is(err, whatsmeow.ErrGroupNotFound) || !errors.Is(err, whatsmeow.ErrIQNotFound) || err.Error() != whatsmeow.ErrGroupNotFound.Error() {
			t.Fatalf("404: %v", err)
		}
		_, err = reply(nil, &whatsmeow.IQError{Code: 403, Text: "forbidden"}).groupIQ().info(ctx, group)
		if !errors.Is(err, whatsmeow.ErrNotInGroup) || !errors.Is(err, whatsmeow.ErrIQForbidden) || err.Error() != whatsmeow.ErrNotInGroup.Error() {
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
			if !errors.Is(err, want) || !errors.Is(err, iq) || err.Error() != want.Error() {
				t.Fatalf("%d: %v", code, err)
			}
		}
	})
	t.Run("production refresh: off the reply path, stores and warm cache", func(t *testing.T) {
		w := newWiredClient(t)
		w.client.DangerousInternals().CacheGroupInfo(&types.GroupInfo{JID: group, Participants: []types.GroupParticipant{{JID: types.NewJID("15550009999", types.DefaultUserServer)}}}, true)
		g := w.groupIQ(func(int, whatsmeow.DangerousInfoQuery) (*waBinary.Node, error) { return iqResult(lidGroupNode()), nil })
		w.block()
		type answer struct {
			info *types.GroupInfo
			err  error
		}
		answered := make(chan answer, 1)
		go func() {
			info, err := g.info(ctx, group)
			answered <- answer{info, err}
		}()
		var a answer
		select {
		case a = <-answered:
		case <-time.After(2 * time.Second):
			t.Fatal("group read waited for its blocked refresh")
		}
		info, err := a.info, a.err
		if err != nil || info.AddressingMode != types.AddressingModeLID {
			t.Fatalf("read answered %v %#v while its refresh is blocked", err, info)
		}
		w.release()
		if err := g.settle(ctx); err != nil {
			t.Fatal(err)
		}
		lid, pn := types.NewJID("111", types.HiddenUserServer), types.NewJID("15551234567", types.DefaultUserServer)
		if lids, phones := w.stored(); !reflect.DeepEqual(lids, []store.LIDMapping{{LID: lid, PN: pn}}) || !reflect.DeepEqual(phones, []store.RedactedPhoneEntry{{JID: lid, RedactedPhone: "+1•••4567"}}) {
			t.Fatalf("stores lids=%#v phones=%#v", lids, phones)
		}
		if warnings := w.warnings(); len(warnings) != 1 || !strings.Contains(warnings[0], "LID mappings") {
			t.Fatalf("storage error not logged: %v", warnings)
		}
		cached, err := w.client.DangerousInternals().GetCachedGroupData(ctx, group)
		if err != nil || cached.AddressingMode != types.AddressingModeLID || !reflect.DeepEqual(cached.Members, []types.JID{lid}) {
			t.Fatalf("warm cache not refreshed: %#v %v", cached, err)
		}
	})
}

// lidGroupNode is a LID-addressed group whose member carries a phone number
// and a redacted display name.
func lidGroupNode() waBinary.Node {
	return waBinary.Node{Tag: "group", Attrs: waBinary.Attrs{"id": "15550000000-123456789", "subject": "Synthetic", "addressing_mode": "lid"}, Content: []waBinary.Node{
		{Tag: "participant", Attrs: waBinary.Attrs{"jid": types.NewJID("111", types.HiddenUserServer), "phone_number": types.NewJID("15551234567", types.DefaultUserServer), "display_name": "+1•••4567"}},
	}}
}

// wiredClient is a real, never connected whatsmeow.Client with synthetic LID
// and contact stores, for the production groupIQ wiring. The LID store fails
// every write and can be blocked, standing for a slow store or cache lock.
type wiredClient struct {
	client *whatsmeow.Client
	lids   *syntheticLIDs
	phones *syntheticContacts
	log    *recordingLog
	iq     *groupRefresher
}

type syntheticLIDs struct {
	store.LIDStore
	mu    sync.Mutex
	gate  chan struct{}
	saved []store.LIDMapping
	calls int
}

func (s *syntheticLIDs) PutManyLIDMappings(_ context.Context, m []store.LIDMapping) error {
	s.mu.Lock()
	gate := s.gate
	s.mu.Unlock()
	if gate != nil {
		<-gate
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.saved, s.calls = append(s.saved, m...), s.calls+1
	return errors.New("synthetic storage failure")
}

type syntheticContacts struct {
	store.ContactStore
	mu    sync.Mutex
	saved []store.RedactedPhoneEntry
}

func (s *syntheticContacts) PutManyRedactedPhones(_ context.Context, e []store.RedactedPhoneEntry) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.saved = append(s.saved, e...)
	return nil
}

type recordingLog struct {
	waLog.Logger
	mu   sync.Mutex
	warn []string
}

func (l *recordingLog) Warnf(format string, _ ...any) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.warn = append(l.warn, format)
}

func newWiredClient(t *testing.T) *wiredClient {
	w := &wiredClient{lids: &syntheticLIDs{}, phones: &syntheticContacts{}, log: &recordingLog{Logger: waLog.Noop}, iq: &groupRefresher{}}
	w.client = whatsmeow.NewClient(&store.Device{LIDs: w.lids, Contacts: w.phones}, w.log)
	t.Cleanup(w.release)
	return w
}

// groupIQ is the production wiring with only the network replaced.
func (w *wiredClient) groupIQ(reply func(int, whatsmeow.DangerousInfoQuery) (*waBinary.Node, error)) groupIQ {
	g := newGroupIQ(w.client, w.iq)
	var mu sync.Mutex
	n := 0
	g.send = func(_ context.Context, q whatsmeow.DangerousInfoQuery) (*waBinary.Node, error) {
		mu.Lock()
		i := n
		n++
		mu.Unlock()
		return reply(i, q)
	}
	return g
}

func (w *wiredClient) block() {
	w.lids.mu.Lock()
	defer w.lids.mu.Unlock()
	w.lids.gate = make(chan struct{})
}

func (w *wiredClient) release() {
	w.lids.mu.Lock()
	defer w.lids.mu.Unlock()
	if w.lids.gate != nil {
		close(w.lids.gate)
		w.lids.gate = nil
	}
}

func (w *wiredClient) stored() ([]store.LIDMapping, []store.RedactedPhoneEntry) {
	w.lids.mu.Lock()
	defer w.lids.mu.Unlock()
	w.phones.mu.Lock()
	defer w.phones.mu.Unlock()
	return w.lids.saved, w.phones.saved
}

func (w *wiredClient) warnings() []string {
	w.log.mu.Lock()
	defer w.log.mu.Unlock()
	return w.log.warn
}

// spec: ANYM-431
func TestGroupRefreshOrdering(t *testing.T) {
	ctx := context.Background()
	group := types.NewJID("15550000000-123456789", types.GroupServer)
	t.Run("refreshes apply one at a time in reply order", func(t *testing.T) {
		var r groupRefresher
		var mu sync.Mutex
		var order []int
		record := func(i int) {
			mu.Lock()
			defer mu.Unlock()
			order = append(order, i)
		}
		gate := make(chan struct{})
		r.enqueue(func() { <-gate; record(1) })
		r.enqueue(func() { record(2) })
		short, cancel := context.WithTimeout(ctx, 50*time.Millisecond)
		defer cancel()
		if err := r.wait(short); !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("wait returned %v while a refresh is blocked", err)
		}
		mu.Lock()
		early := len(order)
		mu.Unlock()
		close(gate)
		if err := r.wait(ctx); err != nil {
			t.Fatal(err)
		}
		if early != 0 || !reflect.DeepEqual(order, []int{1, 2}) {
			t.Fatalf("early=%d order=%v", early, order)
		}
	})
	t.Run("an older read never replaces a newer one", func(t *testing.T) {
		w := newWiredClient(t)
		older := waBinary.Node{Tag: "group", Attrs: waBinary.Attrs{"id": "15550000000-123456789", "subject": "Synthetic"}, Content: []waBinary.Node{
			{Tag: "participant", Attrs: waBinary.Attrs{"jid": types.NewJID("15550009999", types.DefaultUserServer)}}}}
		g := w.groupIQ(func(n int, _ whatsmeow.DangerousInfoQuery) (*waBinary.Node, error) {
			if n == 0 {
				return iqResult(older), nil
			}
			return iqResult(lidGroupNode()), nil
		})
		w.block()
		if _, err := g.info(ctx, group); err != nil {
			t.Fatal(err)
		}
		if _, err := g.info(ctx, group); err != nil {
			t.Fatal(err)
		}
		w.release()
		if err := g.settle(ctx); err != nil {
			t.Fatal(err)
		}
		cached, err := w.client.DangerousInternals().GetCachedGroupData(ctx, group)
		if err != nil || !reflect.DeepEqual(cached.Members, []types.JID{types.NewJID("111", types.HiddenUserServer)}) {
			t.Fatalf("older snapshot won: %#v %v", cached, err)
		}
	})
	t.Run("a change starts after its target read is cached", func(t *testing.T) {
		w := newWiredClient(t)
		changed := make(chan []types.JID, 1)
		g := w.groupIQ(func(n int, q whatsmeow.DangerousInfoQuery) (*waBinary.Node, error) {
			if n == 1 {
				cached, _ := w.client.DangerousInternals().GetCachedGroupData(ctx, group)
				changed <- cached.Members
			}
			return syntheticReply(n, q)
		})
		w.block()
		d := groupOperationDeps(t, &recordingIQ{reply: syntheticReply})
		d.useGroupIQ(g)
		answered := make(chan error, 1)
		go func() {
			_, err := runOperation(ctx, groupJob("rename_group"), d)
			answered <- err
		}()
		select {
		case <-changed:
			t.Fatal("change sent before its target read was cached")
		case <-time.After(100 * time.Millisecond):
		}
		w.release()
		select {
		case members := <-changed:
			if !reflect.DeepEqual(members, []types.JID{types.NewJID("15551234567", types.DefaultUserServer)}) {
				t.Fatalf("change saw cache %v", members)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("change not sent after the refresh landed")
		}
		if err := <-answered; err != nil {
			t.Fatal(err)
		}
	})
	t.Run("a refresh blocked past the deadline stops the change", func(t *testing.T) {
		shortGroupDeadline(t)
		w := newWiredClient(t)
		calls := 0
		var mu sync.Mutex
		g := w.groupIQ(func(n int, q whatsmeow.DangerousInfoQuery) (*waBinary.Node, error) {
			mu.Lock()
			calls++
			mu.Unlock()
			return syntheticReply(n, q)
		})
		w.block()
		d := groupOperationDeps(t, &recordingIQ{reply: syntheticReply})
		d.useGroupIQ(g)
		result, err := runOperation(ctx, groupJob("rename_group"), d)
		if err == nil || err.Error() != "operation_target_unavailable" {
			t.Fatalf("deadline during refresh: %v %#v", err, result)
		}
		w.release()
		if err := g.settle(ctx); err != nil {
			t.Fatal(err)
		}
		time.Sleep(50 * time.Millisecond)
		mu.Lock()
		defer mu.Unlock()
		if calls != 1 {
			t.Fatalf("%d IQs after a deadline answer during the refresh, want only the target read", calls)
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

// fakeWire is a real, never connected client's response waiters with a
// synthetic socket write.
type fakeWire struct {
	*whatsmeow.DangerousInternalClient
	write func(waBinary.Node) error
	sent  []waBinary.Node
}

func (f *fakeWire) SendNode(_ context.Context, node waBinary.Node) error {
	f.sent = append(f.sent, node)
	return f.write(node)
}

// spec: ANYM-431
func TestGroupQueryTransport(t *testing.T) {
	ctx := context.Background()
	group := types.NewJID("15550000000-123456789", types.GroupServer)
	query := whatsmeow.DangerousInfoQuery{Namespace: "w:g2", Type: groupIQGet, To: group, Content: []waBinary.Node{{Tag: "query"}}, Timeout: time.Second, NoRetry: true}
	wire := func(write func(*whatsmeow.DangerousInternalClient, waBinary.Node) error) *fakeWire {
		in := whatsmeow.NewClient(&store.Device{}, nil).DangerousInternals()
		return &fakeWire{DangerousInternalClient: in, write: func(n waBinary.Node) error { return write(in, n) }}
	}
	reply := func(in *whatsmeow.DangerousInternalClient, n waBinary.Node, attrs waBinary.Attrs, content ...waBinary.Node) bool {
		attrs["id"] = n.Attrs["id"]
		return in.ReceiveResponse(ctx, &waBinary.Node{Tag: "iq", Attrs: attrs, Content: content})
	}
	t.Run("node and result", func(t *testing.T) {
		w := wire(func(in *whatsmeow.DangerousInternalClient, n waBinary.Node) error {
			reply(in, n, waBinary.Attrs{"type": "result"})
			return nil
		})
		res, err := sendGroupQuery(ctx, w, query)
		if err != nil || res.Attrs["type"] != "result" || len(w.sent) != 1 {
			t.Fatalf("%v %v", res, err)
		}
		got := w.sent[0]
		if got.Tag != "iq" || got.Attrs["xmlns"] != "w:g2" || got.Attrs["type"] != "get" || got.Attrs["to"] != group || got.Attrs["id"] == "" || !reflect.DeepEqual(got.Content, query.Content) {
			t.Fatalf("node %#v", got)
		}
	})
	t.Run("error reply", func(t *testing.T) {
		w := wire(func(in *whatsmeow.DangerousInternalClient, n waBinary.Node) error {
			reply(in, n, waBinary.Attrs{"type": "error"}, waBinary.Node{Tag: "error", Attrs: waBinary.Attrs{"code": "404", "text": "item-not-found"}})
			return nil
		})
		_, err := sendGroupQuery(ctx, w, query)
		var iq *whatsmeow.IQError
		if !errors.As(err, &iq) || iq.Code != 404 || iq.Text != "item-not-found" || iq.ErrorNode == nil || !errors.Is(err, whatsmeow.ErrIQNotFound) {
			t.Fatalf("%v", err)
		}
	})
	t.Run("disconnect is never resent", func(t *testing.T) {
		w := wire(func(in *whatsmeow.DangerousInternalClient, n waBinary.Node) error {
			in.ClearResponseWaiters(&waBinary.Node{Tag: "stream:error"})
			return nil
		})
		if _, err := sendGroupQuery(ctx, w, query); !errors.As(err, new(*whatsmeow.DisconnectedError)) || len(w.sent) != 1 {
			t.Fatalf("%v, %d sends", err, len(w.sent))
		}
	})
	t.Run("timeout", func(t *testing.T) {
		w := wire(func(*whatsmeow.DangerousInternalClient, waBinary.Node) error { return nil })
		q := query
		q.Timeout = 10 * time.Millisecond
		if _, err := sendGroupQuery(ctx, w, q); !errors.Is(err, whatsmeow.ErrIQTimedOut) {
			t.Fatal(err)
		}
	})
	// Regression: whatsmeow's sendIQ closes the waiter after any send error,
	// which panics the receive loop when the frame left after the context
	// expired and its reply is being delivered.
	t.Run("a failed send whose frame may have left keeps its waiter open", func(t *testing.T) {
		var sent waBinary.Node
		w := wire(func(_ *whatsmeow.DangerousInternalClient, n waBinary.Node) error {
			sent = n
			return context.DeadlineExceeded
		})
		if _, err := sendGroupQuery(ctx, w, query); !errors.Is(err, context.DeadlineExceeded) {
			t.Fatal(err)
		}
		if !reply(w.DangerousInternalClient, sent, waBinary.Attrs{"type": "result"}) {
			t.Fatal("waiter closed after a send whose frame may have left")
		}
		w.ClearResponseWaiters(&waBinary.Node{Tag: "xmlstreamend"})
	})
	t.Run("a send that never left closes its waiter", func(t *testing.T) {
		var sent waBinary.Node
		w := wire(func(_ *whatsmeow.DangerousInternalClient, n waBinary.Node) error {
			sent = n
			return whatsmeow.ErrNotConnected
		})
		if _, err := sendGroupQuery(ctx, w, query); !errors.Is(err, whatsmeow.ErrNotConnected) {
			t.Fatal(err)
		}
		if reply(w.DangerousInternalClient, sent, waBinary.Attrs{"type": "result"}) {
			t.Fatal("waiter left registered after ErrNotConnected")
		}
	})
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
