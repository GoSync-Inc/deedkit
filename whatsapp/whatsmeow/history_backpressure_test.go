package main

import (
	"bytes"
	"compress/zlib"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	waCommon "go.mau.fi/whatsmeow/proto/waCommon"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	waHistorySync "go.mau.fi/whatsmeow/proto/waHistorySync"
	waWeb "go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/store"
	"google.golang.org/protobuf/proto"
)

func enqueueHistoryFixture(t *testing.T, s *CaptureStore, stanza string) {
	t.Helper()
	n := fixtureMessage(stanza, 1700000000)
	n.Message = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{HistorySyncNotification: &waE2E.HistorySyncNotification{FileLength: proto.Uint64(100)}}}
	if err := s.EnqueueHistory(n); err != nil {
		t.Fatal(err)
	}
}

// The status file is read by the Python heartbeat; an invalid one stops it.
func requirePythonHistoryStatus(t *testing.T, s *CaptureStore, state, reason string, attempt int) map[string]any {
	t.Helper()
	raw, err := readPrivate(filepath.Join(s.Config.EventsDirectory, ".history-status"), 4096)
	if err != nil {
		t.Fatal(err)
	}
	var status map[string]any
	if err = json.Unmarshal(raw, &status); err != nil {
		t.Fatal(err)
	}
	if status["state"] != state || status["reason"] != reason || status["attempt"] != float64(attempt) || status["max_attempts"] != float64(maxHistoryAttempts) {
		t.Fatal("unexpected or invalid history status", string(raw))
	}
	return status
}

func TestHistoryBackpressureIsDeferredWithoutConsumingAttempts(t *testing.T) {
	s := openTestStore(t)
	enqueueHistoryFixture(t, s, "backpressure-control")
	now := time.Unix(1700000100, 0)
	// Far more deferrals than attempts: the old code failed the job after three.
	for i := 0; i < 3*maxHistoryAttempts; i++ {
		job, err := s.claimHistory(now)
		if err != nil || job.Attempt != 1 {
			t.Fatal("backpressure consumed a download attempt", i, job, err)
		}
		if err = s.historyDeferred(job, "history_capture_failed", now); err != nil {
			t.Fatal(err)
		}
		status := requirePythonHistoryStatus(t, s, "retrying", "history_capture_failed", 1)
		if status["next_attempt_ms"] != float64(now.Add(historyBackpressureDelay).UnixMilli()) {
			t.Fatal("deferral not visible as backoff", status)
		}
		if _, err = s.claimHistory(now.Add(historyBackpressureDelay - time.Second)); !errors.Is(err, sql.ErrNoRows) {
			t.Fatal("retried before backpressure backoff", err)
		}
		now = now.Add(historyBackpressureDelay)
	}
	// Real download failures stay bounded after any number of deferrals.
	for i := 1; i <= maxHistoryAttempts; i++ {
		job, err := s.claimHistory(now)
		if err != nil || job.Attempt != i {
			t.Fatal(job, err)
		}
		if err = s.historyFailure(job, "history_download_failed"); err != nil {
			t.Fatal(err)
		}
		now = time.UnixMilli(job.Next)
	}
	if _, err := s.claimHistory(now.Add(time.Hour)); !errors.Is(err, sql.ErrNoRows) {
		t.Fatal("unbounded history retry", err)
	}
	requirePythonHistoryStatus(t, s, "failed", "history_download_failed", maxHistoryAttempts)
}

func TestHistoryBackpressureClassification(t *testing.T) {
	for _, code := range []string{"spool_full", "disk_reserve", "capture_overload"} {
		if !historyBackpressure(errors.New(code)) {
			t.Fatal("not deferred", code)
		}
	}
	for _, code := range []string{"history_size_limit", "invalid_history_compression", "durable_capture_failed", "media_network_error"} {
		if historyBackpressure(errors.New(code)) {
			t.Fatal("real failure deferred", code)
		}
	}
	if historyBackpressure(nil) {
		t.Fatal("nil deferred")
	}
}

func TestHistoryBatchUnderFullSpoolIsJournaledAndRetriedWithoutDuplicates(t *testing.T) {
	p := skipProvider(t)
	owner := p.Capture.Owner
	p.Client = nativeClient(&store.Device{ID: &owner})
	chat := "111111@s.whatsapp.net"
	var messages []*waHistorySync.HistorySyncMsg
	for _, id := range []string{"h1", "h2", "h3", "h4"} {
		messages = append(messages, &waHistorySync.HistorySyncMsg{Message: &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{RemoteJID: proto.String(chat), ID: proto.String(id)}, MessageTimestamp: proto.Uint64(1700000000), Message: &waE2E.Message{Conversation: proto.String("synthetic " + id)}}})
	}
	data := &waHistorySync.HistorySync{Conversations: []*waHistorySync.Conversation{{ID: proto.String(chat), Messages: messages}}}
	counts := func() (rows, published, files int) {
		p.Capture.db.QueryRow("SELECT COUNT(*),COALESCE(SUM(published),0) FROM capture").Scan(&rows, &published)
		return rows, published, spoolFiles(t, p.Config.EventsDirectory)
	}
	// One export slot: the batch continues past the full spool and journals
	// the rest instead of stopping at the first spool_full.
	p.Capture.MaxSpoolEntries = 1
	if err := p.captureHistory(data); err != nil {
		t.Fatal("full spool stopped the history batch", err)
	}
	if rows, published, files := counts(); rows != 4 || published != 1 || files != 1 {
		t.Fatal("batch not journaled behind full spool", rows, published, files)
	}
	// A retry while still full, and one after space frees, skips pending rows:
	// they are exported by replay only, never duplicated by the batch.
	for _, quota := range []int{1, 10000} {
		p.Capture.MaxSpoolEntries = quota
		if err := p.captureHistory(data); err != nil {
			t.Fatal(err)
		}
		if rows, published, files := counts(); rows != 4 || published != 1 || files != 1 {
			t.Fatal("retry duplicated or exported pending rows", quota, rows, published, files)
		}
	}
	if err := p.replayPending(); err != nil {
		t.Fatal(err)
	}
	if err := p.captureHistory(data); err != nil {
		t.Fatal(err)
	}
	if rows, published, files := counts(); rows != 4 || published != 4 || files != 4 {
		t.Fatal("replay did not export the journaled history exactly once", rows, published, files)
	}
	if !p.ready.Load() {
		t.Fatal("history backpressure cleared readiness")
	}
	// Live capture keeps the existing unacknowledged spool_full contract.
	p.Capture.MaxSpoolEntries = 4
	if err := p.Capture.Put(fixtureMessage("live", 1700000001), "live"); !spoolBackpressure(err) {
		t.Fatal("live capture changed under full spool", err)
	}
}

func TestHistoryWorkerDefersLocalCapacityWithoutFailingProvider(t *testing.T) {
	p := skipProvider(t)
	enqueueHistoryFixture(t, p.Capture, "disk-control")
	// The download needs local temporary space it does not have.
	p.Capture.Config.DiskReserveBytes = 1 << 62
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	started := time.Now()
	go func() {
		defer close(done)
		p.historyWorker(ctx)
	}()
	defer func() {
		cancel()
		<-done
	}()
	// The status file is written after the job row, so wait for it.
	for deadline := time.Now().Add(15 * time.Second); ; {
		raw, _ := readPrivate(filepath.Join(p.Config.EventsDirectory, ".history-status"), 4096)
		var status map[string]any
		if json.Unmarshal(raw, &status) == nil && status["state"] != "downloading" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("history worker did not run")
		}
		time.Sleep(50 * time.Millisecond)
	}
	var attempts int
	var state, reason string
	var next int64
	p.Capture.db.QueryRow("SELECT state,attempts,error,next_attempt_ms FROM history_jobs WHERE stanza='disk-control'").Scan(&state, &attempts, &reason, &next)
	if state != "attempted" || attempts != 0 || reason != "history_download_failed" || next < started.Add(historyBackpressureDelay).UnixMilli() {
		t.Fatal("local capacity consumed an attempt or skipped backoff", state, attempts, reason, next)
	}
	requirePythonHistoryStatus(t, p.Capture, "retrying", "history_download_failed", 1)
	if !p.ready.Load() {
		t.Fatal("deferred download failed the provider")
	}
	if _, err := os.Stat(filepath.Join(p.Config.EventsDirectory, ".transport-failure")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("deferred download reported a transport failure", err)
	}
}

// spec: ANYM-387
func TestHistoryLaterAttemptsWaitFiveMinutes(t *testing.T) {
	s := openTestStore(t)
	enqueueHistoryFixture(t, s, "later-attempts")
	now := time.Unix(1700000100, 0)
	first, err := s.claimHistory(now)
	if err != nil || first.Attempt != 1 || first.Next != now.Add(time.Minute).UnixMilli() {
		t.Fatal("first retry is not one minute later", first, err)
	}
	if err = s.historyFailure(first, "history_download_failed"); err != nil {
		t.Fatal(err)
	}
	now = time.UnixMilli(first.Next)
	for attempt := 2; attempt <= maxHistoryAttempts; attempt++ {
		job, err := s.claimHistory(now)
		if err != nil || job.Attempt != attempt || job.Next != now.Add(5*time.Minute).UnixMilli() {
			t.Fatal("later attempt does not wait 5 minutes", attempt, job, err)
		}
		if err = s.historyFailure(job, "history_download_failed"); err != nil {
			t.Fatal(err)
		}
		if attempt < maxHistoryAttempts {
			requirePythonHistoryStatus(t, s, "retrying", "history_download_failed", attempt)
			if _, err = s.claimHistory(now.Add(5*time.Minute - time.Second)); !errors.Is(err, sql.ErrNoRows) {
				t.Fatal("claimed before the 5 minute backoff", attempt, err)
			}
		}
		now = time.UnixMilli(job.Next)
	}
}

// spec: ANYM-387
func TestHistoryWorkerKeepsProviderReadyUnderFullSpool(t *testing.T) {
	p := skipProvider(t)
	owner := p.Capture.Owner
	p.Client = nativeClient(&store.Device{ID: &owner})
	// A @lid conversation needs no session-store lookup in the native history
	// storage step, so the real worker path runs offline.
	chat := "111111@lid"
	var messages []*waHistorySync.HistorySyncMsg
	for _, id := range []string{"w1", "w2", "w3"} {
		messages = append(messages, &waHistorySync.HistorySyncMsg{Message: &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{RemoteJID: proto.String(chat), ID: proto.String(id)}, MessageTimestamp: proto.Uint64(1700000000), Message: &waE2E.Message{Conversation: proto.String("synthetic " + id)}}})
	}
	wire, err := proto.Marshal(&waHistorySync.HistorySync{SyncType: waHistorySync.HistorySync_RECENT.Enum(), Conversations: []*waHistorySync.Conversation{{ID: proto.String(chat), Messages: messages}}})
	if err != nil {
		t.Fatal(err)
	}
	var compressed bytes.Buffer
	writer := zlib.NewWriter(&compressed)
	writer.Write(wire)
	writer.Close()
	n := fixtureMessage("full-spool-control", 1700000000)
	n.Message = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{HistorySyncNotification: &waE2E.HistorySyncNotification{InitialHistBootstrapInlinePayload: compressed.Bytes()}}}
	if err = p.Capture.EnqueueHistory(n); err != nil {
		t.Fatal(err)
	}
	// One export slot for three history messages.
	p.Capture.MaxSpoolEntries = 1
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		p.historyWorker(ctx)
	}()
	defer func() {
		cancel()
		<-done
	}()
	for deadline := time.Now().Add(15 * time.Second); ; {
		raw, _ := readPrivate(filepath.Join(p.Config.EventsDirectory, ".history-status"), 4096)
		var status map[string]any
		if json.Unmarshal(raw, &status) == nil && status["state"] != "downloading" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("history worker did not run")
		}
		time.Sleep(50 * time.Millisecond)
	}
	requirePythonHistoryStatus(t, p.Capture, "captured", "", 1)
	var rows, published int
	p.Capture.db.QueryRow("SELECT COUNT(*),COALESCE(SUM(published),0) FROM capture").Scan(&rows, &published)
	if rows != 3 || published != 1 || spoolFiles(t, p.Config.EventsDirectory) != 1 {
		t.Fatal("history batch not journaled behind the full spool", rows, published)
	}
	if !p.ready.Load() {
		t.Fatal("full spool in the history worker cleared readiness")
	}
	if _, err = os.Stat(filepath.Join(p.Config.EventsDirectory, ".transport-failure")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("full spool in the history worker recorded a transport failure", err)
	}
}
