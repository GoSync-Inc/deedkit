package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"testing"
	"time"
	"unsafe"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	waBinary "go.mau.fi/whatsmeow/binary"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

// markerLog records, per dependency, whether each call's context had frame
// retries disabled.
type markerLog struct {
	mu    sync.Mutex
	calls map[string][]bool
}

func (l *markerLog) record(name string, ctx context.Context) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.calls[name] = append(l.calls[name], whatsmeow.FrameRetryDisabled(ctx))
}

// wrapContextDependencies replaces every dependency that takes a context with
// a wrapper that logs the marker, then calls the original.
func wrapContextDependencies(v reflect.Value, log *markerLog) {
	ctxType := reflect.TypeFor[context.Context]()
	for i := 0; i < v.NumField(); i++ {
		field := v.Type().Field(i)
		value := reflect.NewAt(field.Type, unsafe.Pointer(v.Field(i).UnsafeAddr())).Elem()
		if field.Anonymous && field.Type.Kind() == reflect.Struct {
			wrapContextDependencies(value, log)
			continue
		}
		if field.Type.Kind() != reflect.Func || value.IsNil() || field.Type.NumIn() == 0 || field.Type.In(0) != ctxType {
			continue
		}
		original, name := value.Interface(), field.Name
		value.Set(reflect.MakeFunc(field.Type, func(args []reflect.Value) []reflect.Value {
			if ctx, ok := args[0].Interface().(context.Context); ok {
				log.record(name, ctx)
			}
			return reflect.ValueOf(original).Call(args)
		}))
	}
}

// mutationDependencies are the boundaries where a frame leaves for WhatsApp.
var mutationDependencies = wordSet("send patch block status upload history markRead createGroup participants groupName groupDescription leave invite groupSetting joinRequestAction join")

// noReplayJob is a valid request for op that reaches its provider dependency.
func noReplayJob(t *testing.T, op string, d *operationDependencies) operationRequest {
	t.Helper()
	m := operationMessage(operationsChat, "TARGET", true, 1700000000)
	d.get = func(string, string) (*events.Message, error) { return m, nil }
	d.latest = func(string) (*events.Message, error) { return operationMessage(operationsChat, "LATEST", false, 1700000000), nil }
	if d.phone == nil {
		d.phone = func(_ context.Context, j types.JID) (types.JID, error) { return j, nil }
	}
	for _, name := range groupOperationNames {
		if name == op {
			return groupJob(op)
		}
	}
	params := map[string]map[string]any{
		"request_chat_history": {"count": 10}, "get_contact": {"user_id": operationsChat}, "send_poll": {"question": "Question", "options": []string{"a", "b"}, "multiple": false},
		"send_contact": {"contact_ids": []string{operationsChat}}, "send_location": {"latitude": 1, "longitude": 2}, "edit_message": {"text": "new"}, "delete_message": {"revoke": true},
		"send_reaction": {"emoji": "👍"}, "pin_message": {"seconds": 86400}, "mute_chat": {"seconds": 3600}, "block_contact": {"user_id": operationsChat},
		"unblock_contact": {"user_id": operationsChat}, "update_profile": {"status": "Status"}, "open_chat_by_phone": {"phone": phoneFixture},
		"forward_message": {"source_message_id": "archive_source", "source_revision": "revision"},
	}[op]
	job := operationJob(op, operationsChat, params)
	job.ProviderMessageID = LegacyMessageID(m)
	switch op {
	case "send_file":
		job, _ = registeredFileFixture(t)
		job.Operation, job.ProviderChat, job.ExpiresAt = op, operationsChat, float64(time.Now().Add(time.Hour).Unix())
		job.Parameters["mode"], job.Parameters["caption"] = json.RawMessage(`"document"`), json.RawMessage(`""`)
	case "forward_message":
		job.SourceProviderChat, job.SourceProviderMessage = operationsChat, LegacyMessageID(m)
	case "get_profile", "list_contacts", "list_blocked", "get_contact", "update_profile", "block_contact", "unblock_contact":
		job.ProviderChat = ""
	case "open_chat_by_phone":
		job = phoneJob(params)
	}
	return job
}

// spec: ANYM-441
func TestNativeMutationsNeverReplay(t *testing.T) {
	unsupported := wordSet("add_contact delete_contact get_labels set_chat_labels")
	reached := map[string]bool{}
	for op := range operationFields {
		t.Run(op, func(t *testing.T) {
			d := groupOperationDeps(t, &recordingIQ{reply: syntheticReply})
			d.history = func(context.Context, string, int) (any, error) {
				return map[string]any{"requested": true, "count": 10, "pending": true, "coverage": "partial", "retry": "explicit_only"}, nil
			}
			d.block = func(context.Context, types.JID, bool) error { return nil }
			d.status = func(context.Context, string) error { return nil }
			d.blocklist = func(context.Context) (*types.Blocklist, error) { return &types.Blocklist{}, nil }
			d.markRead = func(context.Context, []types.MessageID, time.Time, types.JID, types.JID) error { return nil }
			d.contacts = func(context.Context) (map[types.JID]types.ContactInfo, error) { return nil, nil }
			d.upload = func(_ context.Context, b []byte, _ whatsmeow.MediaType) (whatsmeow.UploadResponse, error) {
				return whatsmeow.UploadResponse{FileLength: uint64(len(b)), FileSHA256: digestRaw(b)}, nil
			}
			if op == "open_chat_by_phone" {
				phone, _ := phoneLookupFake(t, nil, nil)
				d.isOnWhatsApp, d.phone, d.contact = phone.isOnWhatsApp, phone.phone, phone.contact
			}
			job := noReplayJob(t, op, &d)
			log := &markerLog{calls: map[string][]bool{}}
			wrapContextDependencies(reflect.ValueOf(&d).Elem(), log)
			_, err := runOperation(context.Background(), job, d)
			if unsupported[op] {
				requireError(t, err, "operation_unsupported")
				return
			}
			// A read served from local caches takes no context and sends nothing.
			if len(log.calls) == 0 && !(err == nil && readOperations[op]) {
				t.Fatalf("%s reached no dependency (err %v)", op, err)
			}
			for name, marks := range log.calls {
				for _, marked := range marks {
					if !marked {
						t.Fatalf("%s called %s without WithoutFrameRetry", op, name)
					}
				}
				if mutationDependencies[name] {
					reached[name] = true
				}
			}
		})
	}
	for _, name := range []string{"send", "patch", "block", "status", "upload", "history", "groupName", "participants"} {
		if !reached[name] {
			t.Fatalf("no operation reached %s", name)
		}
	}
	t.Run("send_text", func(t *testing.T) {
		d := sendFake(t)
		marked := false
		d.send = func(ctx context.Context, to types.JID, _ *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
			marked = whatsmeow.FrameRetryDisabled(ctx)
			return whatsmeow.SendResponse{ID: extra.ID, Timestamp: time.Unix(1700000000, 0), Chat: to, Sender: d.owner}, nil
		}
		if _, err := boundedSendText(context.Background(), textFixture(), d); err != nil || !marked {
			t.Fatalf("send_text marked=%v err=%v", marked, err)
		}
	})
	t.Run("requestHistory", func(t *testing.T) {
		p := historyProvider(t)
		var marks []bool
		old := sendHistoryRequest
		sendHistoryRequest = func(ctx context.Context, _ *whatsmeow.Client, _ *types.MessageInfo, _ int) error {
			marks = append(marks, whatsmeow.FrameRetryDisabled(ctx))
			return nil
		}
		t.Cleanup(func() { sendHistoryRequest = old })
		raw, _ := json.Marshal(map[string]any{"chat": historyChat, "count": 10})
		pol, _ := p.policy()
		// The direct request_history IPC method, outside runOperation.
		if _, err := p.requestHistory(context.Background(), raw, pol); err != nil {
			t.Fatal(err)
		}
		if _, err := p.Capture.db.Exec("DELETE FROM history_requests"); err != nil {
			t.Fatal(err)
		}
		// request_chat_history through the operation runner.
		d := operationsFake(t)
		d.policy = func() (Policy, error) { return pol, nil }
		d.history = func(ctx context.Context, chat string, count int) (any, error) {
			raw, _ := json.Marshal(map[string]any{"chat": chat, "count": count})
			return p.requestHistory(ctx, raw, pol)
		}
		result, err := runOperation(context.Background(), operationJob("request_chat_history", historyChat, map[string]any{"count": 10}), d)
		operationResult(t, result, err, "submitted")
		if len(marks) != 2 || !marks[0] || !marks[1] {
			t.Fatalf("history peer sends marked: %v", marks)
		}
	})
}

func digestRaw(b []byte) []byte {
	var out []byte
	fmt.Sscanf(digestBytes(b), "%x", &out)
	return out
}

const historyChat = "111111@c.us"

// historyProvider has a capture with one message in historyChat and a policy
// that allows it.
func historyProvider(t *testing.T) *Provider {
	t.Helper()
	s := openTestStore(t)
	if err := s.Put(fixtureMessage("ANCHOR", 1700000000), "live"); err != nil {
		t.Fatal(err)
	}
	saved, _ := json.Marshal(map[string]any{"expires_at": time.Now().Add(time.Hour).Unix(), "policy": Policy{Enabled: true, SendEnabled: true, DiscoverChats: true, IncludeChats: []string{}, ExcludeChats: []string{}, OperationsEnabled: []string{"request_chat_history"}}})
	if err := os.WriteFile(filepath.Join(s.Config.EventsDirectory, ".policy"), saved, 0600); err != nil {
		t.Fatal(err)
	}
	return &Provider{Config: s.Config, Capture: s}
}

func shortDeadlines(t *testing.T) {
	shortGroupDeadline(t)
	oldFile, oldText := fileOperationDeadline, sendTextDeadline
	fileOperationDeadline, sendTextDeadline = 50*time.Millisecond, 50*time.Millisecond
	t.Cleanup(func() { fileOperationDeadline, sendTextDeadline = oldFile, oldText })
}

// stallCase blocks one dependency of a valid request until release closes.
type stallCase struct {
	name  string
	op    string
	stall func(d *operationDependencies, release <-chan struct{})
	// want is the outcome of an operation result, or an error code.
	want string
}

func stallCases() []stallCase {
	block := func(release <-chan struct{}) { <-release }
	return []stallCase{
		{"send", "send_location", func(d *operationDependencies, r <-chan struct{}) {
			d.send = func(context.Context, types.JID, *waE2E.Message, whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
				block(r)
				return whatsmeow.SendResponse{}, errors.New("late")
			}
		}, "unknown"},
		{"patch", "archive_chat", func(d *operationDependencies, r <-chan struct{}) {
			d.patch = func(context.Context, appstate.PatchInfo) error { block(r); return nil }
		}, "unknown"},
		{"block", "block_contact", func(d *operationDependencies, r <-chan struct{}) {
			d.block = func(context.Context, types.JID, bool) error { block(r); return nil }
		}, "unknown"},
		{"status", "update_profile", func(d *operationDependencies, r <-chan struct{}) {
			d.status = func(context.Context, string) error { block(r); return nil }
		}, "unknown"},
		{"read", "get_contact", func(d *operationDependencies, r <-chan struct{}) {
			d.contact = func(context.Context, types.JID) (types.ContactInfo, error) { block(r); return types.ContactInfo{}, nil }
		}, "operation_query_failed"},
		{"upload", "send_file", func(d *operationDependencies, r <-chan struct{}) {
			d.upload = func(context.Context, []byte, whatsmeow.MediaType) (whatsmeow.UploadResponse, error) {
				block(r)
				return whatsmeow.UploadResponse{}, errors.New("late")
			}
		}, "operation_target_unavailable"},
		{"file send", "send_file", func(d *operationDependencies, r <-chan struct{}) {
			d.send = func(context.Context, types.JID, *waE2E.Message, whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
				block(r)
				return whatsmeow.SendResponse{}, errors.New("late")
			}
		}, "unknown"},
		{"history before gate", "request_chat_history", func(d *operationDependencies, r <-chan struct{}) {
			d.history = func(ctx context.Context, _ string, _ int) (any, error) {
				block(r)
				if !startChange(ctx) {
					return nil, errors.New("operation_target_unavailable")
				}
				return nil, errors.New("sent after the deadline answer")
			}
		}, "operation_target_unavailable"},
		{"history after send", "request_chat_history", func(d *operationDependencies, r <-chan struct{}) {
			d.history = func(ctx context.Context, _ string, _ int) (any, error) {
				startChange(ctx)
				block(r)
				return nil, errors.New("history_request_unknown")
			}
		}, "history_request_unknown"},
	}
}

func baseStallDeps(t *testing.T) operationDependencies {
	d := operationsFake(t)
	d.block = func(context.Context, types.JID, bool) error { return nil }
	d.status = func(context.Context, string) error { return nil }
	d.upload = func(_ context.Context, b []byte, _ whatsmeow.MediaType) (whatsmeow.UploadResponse, error) {
		return whatsmeow.UploadResponse{FileLength: uint64(len(b)), FileSHA256: digestRaw(b)}, nil
	}
	return d
}

// spec: ANYM-441
func TestNativeOperationDeadline(t *testing.T) {
	shortDeadlines(t)
	for _, tc := range stallCases() {
		t.Run(tc.name, func(t *testing.T) {
			release := make(chan struct{})
			defer close(release)
			d := baseStallDeps(t)
			job := noReplayJob(t, tc.op, &d)
			tc.stall(&d, release)
			started := time.Now()
			result, err := runOperation(context.Background(), job, d)
			if elapsed := time.Since(started); elapsed > time.Second+50*time.Millisecond {
				t.Fatalf("answered after %s", elapsed)
			}
			switch tc.want {
			case "unknown":
				if data := operationResult(t, result, err, "unknown"); data["code"] != "provider_operation_uncertain" {
					t.Fatalf("%#v", data)
				}
			case "history_request_unknown":
				if data := operationResult(t, result, err, "unknown"); data["code"] != "history_request_unknown" || data["retry"] != "explicit_only" {
					t.Fatalf("%#v", data)
				}
			default:
				requireError(t, err, tc.want)
			}
			// The same provider keeps serving while the first goroutine is blocked.
			next := baseStallDeps(t)
			result, err = runOperation(context.Background(), operationJob("get_profile", "", nil), next)
			operationResult(t, result, err, "confirmed")
		})
	}
	t.Run("send_text", func(t *testing.T) {
		release := make(chan struct{})
		defer close(release)
		d := sendFake(t)
		d.send = func(context.Context, types.JID, *waE2E.Message, whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
			<-release
			return whatsmeow.SendResponse{}, errors.New("late")
		}
		started := time.Now()
		_, err := boundedSendText(context.Background(), textFixture(), d)
		if time.Since(started) > time.Second+50*time.Millisecond {
			t.Fatal("send_text answered late")
		}
		requireError(t, err, "provider_operation_uncertain")
	})
}

// spec: ANYM-441
func TestNativeDeadlineStartGate(t *testing.T) {
	shortDeadlines(t)
	// Each case stalls a local read before its change; after the deadline
	// answer the released goroutine must not reach the provider.
	for _, op := range []string{"send_reaction", "edit_message", "star_message", "send_location", "request_chat_history"} {
		t.Run(op, func(t *testing.T) {
			release, finished := make(chan struct{}), make(chan struct{})
			d := baseStallDeps(t)
			job := noReplayJob(t, op, &d)
			get := d.get
			d.get = func(chat, id string) (*events.Message, error) { <-release; return get(chat, id) }
			d.settings = func(context.Context, types.JID) (types.LocalChatSettings, error) { <-release; return types.LocalChatSettings{}, nil }
			sent := 0
			d.send = func(context.Context, types.JID, *waE2E.Message, whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
				sent++
				return whatsmeow.SendResponse{}, errors.New("late")
			}
			d.patch = func(context.Context, appstate.PatchInfo) error { sent++; return nil }
			d.history = func(ctx context.Context, _ string, _ int) (any, error) {
				<-release
				if startChange(ctx) {
					sent++
				}
				return nil, errors.New("operation_target_unavailable")
			}
			d.groupOperationDone = func() { close(finished) }
			if op == "send_location" {
				// No read precedes this send: stall the policy check instead.
				pol := d.policy
				d.policy = func() (Policy, error) { <-release; return pol() }
			}
			_, err := runOperation(context.Background(), job, d)
			requireError(t, err, "operation_target_unavailable")
			close(release)
			select {
			case <-finished:
			case <-time.After(5 * time.Second):
				t.Fatal("goroutine never finished")
			}
			if sent != 0 {
				t.Fatalf("%s reached the provider after the deadline answer", op)
			}
		})
	}
	t.Run("send_text", func(t *testing.T) {
		release := make(chan struct{})
		d := sendFake(t)
		sent := make(chan struct{}, 1)
		d.send = func(context.Context, types.JID, *waE2E.Message, whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
			sent <- struct{}{}
			return whatsmeow.SendResponse{}, errors.New("late")
		}
		m := operationMessage(operationsChat, "QUOTED", false, 1700000000)
		d.get = func(string, string) (*events.Message, error) { <-release; return m, nil }
		req := textFixture()
		req.ReplyProviderID = LegacyMessageID(m)
		_, err := boundedSendText(context.Background(), req, d)
		requireError(t, err, "provider_operation_uncertain")
		close(release)
		select {
		case <-sent:
			t.Fatal("send_text sent after the deadline answer")
		case <-time.After(300 * time.Millisecond):
		}
	})
	t.Run("requestHistory refuses after the answer", func(t *testing.T) {
		p := historyProvider(t)
		old := sendHistoryRequest
		sendHistoryRequest = func(context.Context, *whatsmeow.Client, *types.MessageInfo, int) error {
			t.Fatal("history request sent after the deadline answer")
			return nil
		}
		t.Cleanup(func() { sendHistoryRequest = old })
		gate := &operationGate{}
		gate.answer()
		raw, _ := json.Marshal(map[string]any{"chat": historyChat, "count": 10})
		pol, _ := p.policy()
		_, err := p.requestHistory(context.WithValue(context.Background(), operationGateKey{}, gate), raw, pol)
		requireError(t, err, "operation_target_unavailable")
	})
}

func forkTestClient() *whatsmeow.Client {
	device := *store.NoopDevice
	return whatsmeow.NewClient(&device, nil)
}

// spec: ANYM-441
func TestForkRetryFrameRefusesMarkedContext(t *testing.T) {
	c := forkTestClient()
	marked := whatsmeow.WithoutFrameRetry(context.Background())
	derived, cancel := context.WithTimeout(marked, time.Minute)
	defer cancel()
	for name, ctx := range map[string]context.Context{"marked": marked, "derived": derived} {
		started := time.Now()
		_, err := c.DangerousInternals().RetryFrame(ctx, "message", "ID1", []byte("frame"), &waBinary.Node{Tag: "xmlstreamend"}, time.Second)
		var disconnected *whatsmeow.DisconnectedError
		if !errors.As(err, &disconnected) {
			t.Fatalf("%s: %v", name, err)
		}
		if elapsed := time.Since(started); elapsed > 100*time.Millisecond {
			t.Fatalf("%s waited %s for a reconnect", name, elapsed)
		}
	}
	if whatsmeow.FrameRetryDisabled(context.Background()) {
		t.Fatal("unmarked context reports disabled retries")
	}
}

// spec: ANYM-441
func TestForkCancelResponseRace(t *testing.T) {
	in := forkTestClient().DangerousInternals()
	reply := func(id string) *waBinary.Node { return &waBinary.Node{Tag: "iq", Attrs: waBinary.Attrs{"id": id, "type": "result"}} }
	open := func(ch chan *waBinary.Node) bool {
		select {
		case _, ok := <-ch:
			return ok
		default:
			return true
		}
	}
	ch := in.WaitResponse("own")
	in.CancelResponse("own", ch)
	if !open(ch) {
		t.Fatal("CancelResponse closed the waiter")
	}
	if in.ReceiveResponse(context.Background(), reply("own")) {
		t.Fatal("CancelResponse kept its registration")
	}
	older := in.WaitResponse("shared")
	newer := in.WaitResponse("shared")
	in.CancelResponse("shared", older)
	if !in.ReceiveResponse(context.Background(), reply("shared")) {
		t.Fatal("CancelResponse removed a newer waiter")
	}
	if got := <-newer; got.Attrs["id"] != "shared" {
		t.Fatalf("newer waiter got %v", got)
	}
	cleared := in.WaitResponse("cleared")
	in.CancelResponse("cleared", cleared)
	in.ClearResponseWaiters(&waBinary.Node{Tag: "xmlstreamend"})
	if !open(cleared) {
		t.Fatal("ClearResponseWaiters touched a cancelled waiter")
	}
	for i := range 10000 {
		id := fmt.Sprintf("race-%d", i)
		ch := in.WaitResponse(id)
		var wg sync.WaitGroup
		wg.Go(func() { in.ReceiveResponse(context.Background(), reply(id)) })
		in.CancelResponse(id, ch)
		wg.Wait()
	}
}

// spec: ANYM-477
func TestRequestHistoryIsOncePerMinutePerAccountOnTheRealStore(t *testing.T) {
	p := historyProvider(t)
	other := fixtureMessage("OTHER", 1700000001)
	other.Info.Chat = types.NewJID("222222", types.DefaultUserServer)
	other.Info.Sender = other.Info.Chat
	if err := p.Capture.Put(other, "live"); err != nil {
		t.Fatal(err)
	}
	sent := 0
	old := sendHistoryRequest
	sendHistoryRequest = func(context.Context, *whatsmeow.Client, *types.MessageInfo, int) error {
		sent++
		return nil
	}
	t.Cleanup(func() { sendHistoryRequest = old })
	pol, _ := p.policy()
	ask := func(chat string) error {
		raw, _ := json.Marshal(map[string]any{"chat": chat, "count": 10})
		_, err := p.requestHistory(context.Background(), raw, pol)
		return err
	}
	if err := ask(historyChat); err != nil || sent != 1 {
		t.Fatalf("first request err=%v sent=%d", err, sent)
	}
	// Within the minute: refused before any peer send, for this chat and for another chat of the account.
	for _, chat := range []string{historyChat, "222222@c.us"} {
		if err := ask(chat); err == nil || err.Error() != "history_rate_limited" || sent != 1 {
			t.Fatalf("second request in the minute chat=%s err=%v sent=%d", chat, err, sent)
		}
	}
	// Past the minute boundary the account may ask again.
	if _, err := p.Capture.db.Exec("UPDATE history_requests SET last_ms=last_ms-60001 WHERE id=1"); err != nil {
		t.Fatal(err)
	}
	if err := ask("222222@c.us"); err != nil || sent != 2 {
		t.Fatalf("request after the minute err=%v sent=%d", err, sent)
	}
}
