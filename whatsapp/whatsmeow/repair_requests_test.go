package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"reflect"
	"runtime"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	waCommon "go.mau.fi/whatsmeow/proto/waCommon"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	waHistorySync "go.mau.fi/whatsmeow/proto/waHistorySync"
	waWeb "go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

// repairFake replaces the network step and the id generator; every send is counted.
type repairFake struct {
	sent    []string
	anchors []string
	fail    error
	after   func()
	next    int
}

func installRepairFake(t *testing.T) *repairFake {
	t.Helper()
	f := &repairFake{}
	oldSend, oldID := repairSend, repairID
	repairSend = func(_ context.Context, _ *whatsmeow.Client, id types.MessageID, anchor *types.MessageInfo, count int) error {
		if count != repairCount {
			t.Fatalf("count=%d", count)
		}
		f.sent = append(f.sent, string(id))
		f.anchors = append(f.anchors, anchor.ID)
		if f.after != nil {
			f.after()
		}
		return f.fail
	}
	repairID = func(*whatsmeow.Client) types.MessageID {
		f.next++
		return types.MessageID(fmt.Sprintf("REQ%d", f.next))
	}
	t.Cleanup(func() { repairSend, repairID = oldSend, oldID })
	return f
}

func repairProvider(t *testing.T) *Provider {
	t.Helper()
	p := historyProvider(t)
	p.Config.GapRepair = true
	p.Capture.Config.GapRepair = true
	return p
}

func askRepair(p *Provider, span, after string) (map[string]any, error) {
	pol, err := p.policy()
	if err != nil {
		return nil, err
	}
	raw, _ := json.Marshal(map[string]any{"chat": historyChat, "span_id": span, "before_ts": 1700000000, "after_request": after})
	result, err := p.gapRepairRequest(context.Background(), raw, pol)
	if err != nil {
		return nil, err
	}
	return result.(map[string]any), nil
}

func repairState(t *testing.T, p *Provider, id string) string {
	t.Helper()
	var state string
	if err := p.Capture.db.QueryRow("SELECT state FROM repair_requests WHERE request_id=?", id).Scan(&state); err != nil {
		return "absent"
	}
	return state
}

// allowNextRepair moves the shared history request row past the spacing.
func allowNextRepair(t *testing.T, p *Provider) {
	t.Helper()
	if _, err := p.Capture.db.Exec("UPDATE history_requests SET last_ms=last_ms-?", repairSpacing.Milliseconds()+1); err != nil {
		t.Fatal(err)
	}
}

func restartStore(t *testing.T, p *Provider) {
	t.Helper()
	if err := p.Capture.Close(); err != nil {
		t.Fatal(err)
	}
	s, err := OpenCapture(p.Config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close() })
	p.Capture = s
}

func onDemand(session string, chat string, ids []string, first int64) (*waE2E.HistorySyncNotification, *waHistorySync.HistorySync) {
	notif := &waE2E.HistorySyncNotification{SyncType: waE2E.HistorySyncType_ON_DEMAND.Enum()}
	if session != "" {
		notif.PeerDataRequestSessionID = proto.String(session)
	}
	conv := &waHistorySync.Conversation{ID: proto.String(chat)}
	for i, id := range ids {
		conv.Messages = append(conv.Messages, &waHistorySync.HistorySyncMsg{Message: &waWeb.WebMessageInfo{
			Key:              &waCommon.MessageKey{RemoteJID: proto.String(chat), FromMe: proto.Bool(false), ID: proto.String(id)},
			MessageTimestamp: proto.Uint64(uint64(first + int64(i))),
		}})
	}
	return notif, &waHistorySync.HistorySync{SyncType: waHistorySync.HistorySync_ON_DEMAND.Enum(), Conversations: []*waHistorySync.Conversation{conv}}
}

// keepAnswer captures the answer's messages as the history worker does before repairAnswer.
func keepAnswer(t *testing.T, p *Provider, ids []string, first int64) {
	t.Helper()
	for i, id := range ids {
		if err := p.Capture.Put(fixtureMessage(id, first+int64(i)), "history"); err != nil {
			t.Fatal(err)
		}
	}
}

func fiftyIDs() []string {
	ids := make([]string, 50)
	for i := range ids {
		ids[i] = fmt.Sprintf("OLD%02d", i)
	}
	return ids
}

// spec: ANYM-814
func TestGapRepairFlagOffMakesNoRequestAndNoReservation(t *testing.T) {
	p := historyProvider(t)
	f := installRepairFake(t)
	repairSend = func(context.Context, *whatsmeow.Client, types.MessageID, *types.MessageInfo, int) error {
		t.Fatal("a request left with the flag off")
		return nil
	}
	if _, err := askRepair(p, "span-a", ""); err == nil || err.Error() != "gap_repair_disabled" {
		t.Fatalf("err=%v", err)
	}
	restartStore(t, p)
	if _, err := askRepair(p, "span-a", ""); err == nil || err.Error() != "gap_repair_disabled" {
		t.Fatalf("after restart err=%v", err)
	}
	var n int
	if err := p.Capture.db.QueryRow("SELECT COUNT(*) FROM repair_requests").Scan(&n); err != nil || n != 0 || f.next != 0 {
		t.Fatalf("reservations=%d ids=%d err=%v", n, f.next, err)
	}
}

// spec: ANYM-814
func TestGapRepairCrashBeforeSendingReleasesWithoutSend(t *testing.T) {
	p := repairProvider(t)
	f := installRepairFake(t)
	row := gapRequest{ID: "CRASH1", Chat: historyChat, Span: "span-a", AnchorID: "ANCHOR", AnchorTS: 1700000000, Count: repairCount}
	if err := p.Capture.reserveRepair(row, time.Now()); err != nil {
		t.Fatal(err)
	}
	restartStore(t, p)
	if got := repairState(t, p, "CRASH1"); got != "absent" || len(f.sent) != 0 {
		t.Fatalf("state=%s sent=%v", got, f.sent)
	}
	// The released anchor may be asked again: nothing reached the network.
	allowNextRepair(t, p)
	if _, err := askRepair(p, "span-a", ""); err != nil || len(f.sent) != 1 {
		t.Fatalf("err=%v sent=%v", err, f.sent)
	}
}

// spec: ANYM-814
func TestGapRepairTerminationAfterNetworkAcceptanceIsUncertainAndNeverResent(t *testing.T) {
	p := repairProvider(t)
	f := installRepairFake(t)
	// The network accepts the request, then the process ends before `sent` is committed.
	f.after = runtime.Goexit
	done := make(chan struct{})
	go func() {
		defer close(done)
		_, _ = askRepair(p, "span-a", "")
	}()
	<-done
	if got := repairState(t, p, "REQ1"); got != "sending" {
		t.Fatalf("before restart state=%s", got)
	}
	restartStore(t, p)
	f.after = nil
	if got := repairState(t, p, "REQ1"); got != "uncertain" {
		t.Fatalf("after restart state=%s", got)
	}
	allowNextRepair(t, p)
	if _, err := askRepair(p, "span-a", ""); err == nil || err.Error() != "gap_repair_uncertain" || len(f.sent) != 1 {
		t.Fatalf("second ask err=%v sent=%v", err, f.sent)
	}
	// The same anchor through another span is not asked either.
	if _, err := askRepair(p, "span-b", ""); err == nil || err.Error() != "gap_repair_uncertain" || len(f.sent) != 1 {
		t.Fatalf("other span err=%v sent=%v", err, f.sent)
	}
}

// spec: ANYM-814
func TestGapRepairUncertainSendIsNeverRetried(t *testing.T) {
	p := repairProvider(t)
	f := installRepairFake(t)
	f.fail = context.DeadlineExceeded
	if _, err := askRepair(p, "span-a", ""); err == nil || err.Error() != "history_request_unknown" {
		t.Fatalf("err=%v", err)
	}
	if got := repairState(t, p, "REQ1"); got != "uncertain" {
		t.Fatalf("state=%s", got)
	}
	f.fail = nil
	allowNextRepair(t, p)
	if _, err := askRepair(p, "span-a", ""); err == nil || err.Error() != "gap_repair_uncertain" || len(f.sent) != 1 {
		t.Fatalf("retry err=%v sent=%v", err, f.sent)
	}
	status, err := p.Capture.repairStatus(true, time.Now())
	if err != nil || status["outstanding"] != 0 {
		t.Fatalf("status=%v err=%v", status, err)
	}
}

// spec: ANYM-814
func TestGapRepairSharesTheAgentHistoryRequestRow(t *testing.T) {
	p := repairProvider(t)
	f := installRepairFake(t)
	agent := 0
	old := sendHistoryRequest
	sendHistoryRequest = func(context.Context, *whatsmeow.Client, *types.MessageInfo, int) error {
		agent++
		return nil
	}
	t.Cleanup(func() { sendHistoryRequest = old })
	pol, _ := p.policy()
	askAgent := func() error {
		raw, _ := json.Marshal(map[string]any{"chat": historyChat, "count": 10})
		_, err := p.requestHistory(context.Background(), raw, pol)
		return err
	}
	if err := askAgent(); err != nil || agent != 1 {
		t.Fatalf("agent err=%v", err)
	}
	// Within 5 minutes after the agent's request: no repair request.
	if _, err := p.Capture.db.Exec("UPDATE history_requests SET last_ms=last_ms-?", (4 * time.Minute).Milliseconds()); err != nil {
		t.Fatal(err)
	}
	if _, err := askRepair(p, "span-a", ""); err == nil || err.Error() != "history_rate_limited" || len(f.sent) != 0 {
		t.Fatalf("repair after agent err=%v sent=%v", err, f.sent)
	}
	allowNextRepair(t, p)
	if _, err := askRepair(p, "span-a", ""); err != nil || len(f.sent) != 1 {
		t.Fatalf("repair err=%v", err)
	}
	// And the agent's per-minute limit counts the repair request.
	if err := askAgent(); err == nil || err.Error() != "history_rate_limited" || agent != 1 {
		t.Fatalf("agent after repair err=%v", err)
	}
}

// spec: ANYM-814
func TestGapRepairCapsAndPhoneUnreachablePause(t *testing.T) {
	p := repairProvider(t)
	f := installRepairFake(t)
	if _, err := askRepair(p, "span-a", ""); err != nil {
		t.Fatal(err)
	}
	// One outstanding request per account.
	allowNextRepair(t, p)
	if _, err := askRepair(p, "span-b", ""); err == nil || err.Error() != "gap_repair_outstanding" {
		t.Fatalf("outstanding err=%v", err)
	}
	// Past the deadline: no_answer. The same anchor waits 24 hours.
	later := time.Now().Add(repairDeadline + time.Second)
	if _, err := p.Capture.repairStatus(true, later); err != nil || repairState(t, p, "REQ1") != "no_answer" {
		t.Fatalf("expiry err=%v state=%s", err, repairState(t, p, "REQ1"))
	}
	if _, err := askRepair(p, "span-a", ""); err == nil || err.Error() != "gap_repair_retry_not_due" {
		t.Fatalf("retry err=%v", err)
	}
	if _, err := p.Capture.db.Exec("UPDATE repair_requests SET created_ms=created_ms-? WHERE request_id='REQ1'", repairAnchorRetryAfter.Milliseconds()+1); err != nil {
		t.Fatal(err)
	}
	if _, err := askRepair(p, "span-a", ""); err != nil {
		t.Fatalf("second ask after 24h err=%v", err)
	}
	// The second consecutive no_answer pauses the account for 6 hours.
	status, err := p.Capture.repairStatus(true, time.Now().Add(repairDeadline+time.Second))
	if err != nil || status["reason"] != "phone_unreachable" || status["paused_until_ms"].(int64) <= time.Now().Add(5*time.Hour).UnixMilli() {
		t.Fatalf("pause status=%v err=%v", status, err)
	}
	allowNextRepair(t, p)
	if _, err := askRepair(p, "span-b", ""); err == nil || err.Error() != "gap_repair_paused" {
		t.Fatalf("paused err=%v", err)
	}
	// After the pause the anchor's two no_answers are its limit; no budget was spent.
	if _, err := p.Capture.db.Exec("UPDATE repair_account SET paused_until_ms=0"); err != nil {
		t.Fatal(err)
	}
	if _, err := p.Capture.db.Exec("UPDATE repair_requests SET created_ms=created_ms-? WHERE request_id='REQ2'", repairAnchorRetryAfter.Milliseconds()+1); err != nil {
		t.Fatal(err)
	}
	if _, err := askRepair(p, "span-a", ""); err == nil || err.Error() != "gap_repair_no_answer_limit" {
		t.Fatalf("anchor limit err=%v", err)
	}
	var answered int
	_ = p.Capture.db.QueryRow("SELECT COUNT(*) FROM repair_requests WHERE state='answered'").Scan(&answered)
	if answered != 0 || len(f.sent) != 2 {
		t.Fatalf("answered=%d sent=%v", answered, f.sent)
	}
	// Ten answered requests are the span's budget.
	for i := 0; i < repairSpanAnswers; i++ {
		if _, err := p.Capture.db.Exec("INSERT INTO repair_requests(request_id,account,chat,span_id,anchor_id,anchor_ts,anchor_from_me,count,state,created_ms)VALUES(?,?,?,?,?,0,0,50,'answered',0)", fmt.Sprintf("OLD%d", i), "test", historyChat, "span-c", fmt.Sprintf("A%d", i)); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := askRepair(p, "span-c", ""); err == nil || err.Error() != "gap_repair_budget_exhausted" {
		t.Fatalf("budget err=%v", err)
	}
}

// spec: ANYM-814
func TestGapRepairCountsOnlyTheCorrelatedOnDemandAnswer(t *testing.T) {
	// Build-time probe: the pinned proto carries the correlation field.
	if _, ok := reflect.TypeOf(waE2E.HistorySyncNotification{}).FieldByName("PeerDataRequestSessionID"); !ok {
		t.Fatal("PeerDataRequestSessionID missing: no answer can be correlated")
	}
	p := repairProvider(t)
	installRepairFake(t)
	if _, err := askRepair(p, "span-a", ""); err != nil {
		t.Fatal(err)
	}
	ids := make([]string, 50)
	for i := range ids {
		ids[i] = fmt.Sprintf("OLD%02d", i)
	}
	// An uncorrelated history sync, a pairing batch and another chat close nothing.
	for _, session := range []string{"", "AGENT-REQUEST"} {
		notif, data := onDemand(session, "111111@s.whatsapp.net", ids, 1600000000)
		if err := p.Capture.repairAnswer(notif, data, time.Now()); err != nil {
			t.Fatal(err)
		}
	}
	notif, data := onDemand("REQ1", "111111@s.whatsapp.net", ids, 1600000000)
	notif.SyncType = waE2E.HistorySyncType_RECENT.Enum()
	if err := p.Capture.repairAnswer(notif, data, time.Now()); err != nil || repairState(t, p, "REQ1") != "sent" {
		t.Fatalf("uncorrelated closed state=%s err=%v", repairState(t, p, "REQ1"), err)
	}
	keepAnswer(t, p, ids, 1600000000)
	notif, data = onDemand("REQ1", "111111@s.whatsapp.net", ids, 1600000000)
	if err := p.Capture.repairAnswer(notif, data, time.Now()); err != nil {
		t.Fatal(err)
	}
	status, err := p.Capture.repairStatus(true, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	row := status["requests"].([]map[string]any)[0]
	if row["state"] != "answered" || row["got"] != 50 || row["skipped"] != 0 || row["oldest_ts"] != int64(1600000000) || status["outstanding"] != 0 {
		t.Fatalf("row=%v status=%v", row, status)
	}
	// The cursor moves to the answer's oldest message; the span stays.
	allowNextRepair(t, p)
	f := installRepairFake(t)
	f.next = 1
	if _, err := askRepair(p, "span-a", "REQ1"); err != nil || len(f.anchors) != 1 || f.anchors[0] != "OLD00" {
		t.Fatalf("next anchor err=%v anchors=%v", err, f.anchors)
	}
}

// spec: ANYM-814
func TestGapRepairWaitingHoldsNoLock(t *testing.T) {
	p := repairProvider(t)
	installRepairFake(t)
	if _, err := askRepair(p, "span-a", ""); err != nil {
		t.Fatal(err)
	}
	// With the request outstanding: live capture, other history and page reads continue.
	if err := p.Capture.Put(fixtureMessage("LIVE1", 1700000100), "live"); err != nil {
		t.Fatal(err)
	}
	_, data := onDemand("", "111111@s.whatsapp.net", []string{"H1"}, 1500000000)
	// A stub message is captured without the client's parser.
	data.Conversations[0].Messages[0].Message.MessageStubType = waWeb.WebMessageInfo_GROUP_CREATE.Enum()
	data.SyncType = waHistorySync.HistorySync_RECENT.Enum()
	if err := p.captureHistory(data); err != nil {
		t.Fatal(err)
	}
	page, err := p.Capture.Page(historyChat, 10, nil)
	if err != nil || len(page) < 2 {
		t.Fatalf("page=%d err=%v", len(page), err)
	}
	if got := repairState(t, p, "REQ1"); got != "sent" {
		t.Fatalf("state=%s", got)
	}
}

// spec: ANYM-814
func TestGapRepairAnswerCountsOnlyMessagesCaptureKept(t *testing.T) {
	p := repairProvider(t)
	installRepairFake(t)
	if _, err := askRepair(p, "span-a", ""); err != nil {
		t.Fatal(err)
	}
	ids := fiftyIDs()
	// The oldest message was not kept (skipped or excluded): it is no evidence.
	keepAnswer(t, p, ids[1:], 1600000001)
	notif, data := onDemand("REQ1", "111111@s.whatsapp.net", ids, 1600000000)
	if err := p.Capture.repairAnswer(notif, data, time.Now()); err != nil {
		t.Fatal(err)
	}
	status, err := p.Capture.repairStatus(true, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	row := status["requests"].([]map[string]any)[0]
	if row["got"] != 49 || row["skipped"] != 1 || row["oldest_ts"] != int64(1600000001) {
		t.Fatalf("row=%v", row)
	}
}

// spec: ANYM-814
func TestGapRepairAnswerBeforeTheSendReturnsCompletesTheRequest(t *testing.T) {
	p := repairProvider(t)
	f := installRepairFake(t)
	ids := fiftyIDs()
	// The phone answers while the send is still returning (Astra r1 B2).
	f.after = func() {
		keepAnswer(t, p, ids, 1600000000)
		notif, data := onDemand("REQ1", "111111@s.whatsapp.net", ids, 1600000000)
		if err := p.Capture.repairAnswer(notif, data, time.Now()); err != nil {
			t.Error(err)
		}
	}
	result, err := askRepair(p, "span-a", "")
	if err != nil || result["state"] != "answered" || repairState(t, p, "REQ1") != "answered" {
		t.Fatalf("result=%v err=%v state=%s", result, err, repairState(t, p, "REQ1"))
	}
}

// spec: ANYM-814
func TestGapRepairAndAgentRequestRaceSendOnlyOne(t *testing.T) {
	for i := 0; i < 20; i++ {
		p := repairProvider(t)
		f := installRepairFake(t)
		var agent atomic.Int32
		old := sendHistoryRequest
		sendHistoryRequest = func(context.Context, *whatsmeow.Client, *types.MessageInfo, int) error {
			agent.Add(1)
			return nil
		}
		pol, _ := p.policy()
		start := make(chan struct{})
		var wg sync.WaitGroup
		wg.Go(func() {
			<-start
			raw, _ := json.Marshal(map[string]any{"chat": historyChat, "count": 10})
			_, _ = p.requestHistory(context.Background(), raw, pol)
		})
		wg.Go(func() {
			<-start
			_, _ = askRepair(p, "span-a", "")
		})
		close(start)
		wg.Wait()
		sendHistoryRequest = old
		if total := int(agent.Load()) + len(f.sent); total != 1 {
			t.Fatalf("round %d: agent=%d repair=%d", i, agent.Load(), len(f.sent))
		}
	}
}

// spec: ANYM-814
func TestGapRepairOutstandingLeavesSendsAndMediaRunning(t *testing.T) {
	p := repairProvider(t)
	installRepairFake(t)
	if _, err := askRepair(p, "span-a", ""); err != nil {
		t.Fatal(err)
	}
	// A send completes and its capture write reaches the same store while the repair waits.
	d := sendFake(t)
	stored := false
	d.put = func(*events.Message, string) error {
		stored = true
		return p.Capture.Put(fixtureMessage("SENT1", 1700000200), "live")
	}
	if _, err := sendText(context.Background(), textFixture(), d); err != nil || !stored {
		t.Fatalf("send err=%v stored=%v", err, stored)
	}
	// A media download streams to the end.
	data := bytes.Repeat([]byte("synthetic-only "), 100)
	req, cfg, m := mediaFixture(t, data)
	var output bytes.Buffer
	err := downloadMedia(context.Background(), req, cfg, mediaPolicy, mediaGet(m), func(_ context.Context, _ whatsmeow.DownloadableMessage, file whatsmeow.File) error {
		_, e := io.Copy(file, bytes.NewReader(data))
		return e
	}, func(chunk []byte) error {
		_, e := output.Write(chunk)
		return e
	})
	if err != nil || !bytes.Equal(output.Bytes(), data) {
		t.Fatalf("media err=%v", err)
	}
	if got := repairState(t, p, "REQ1"); got != "sent" {
		t.Fatalf("state=%s", got)
	}
}
