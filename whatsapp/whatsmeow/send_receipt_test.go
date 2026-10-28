package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

const receiptSendID = "send_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

func TestExactSendJournalPrecedesCaptureAndLostIPCDoesNotResend(t *testing.T) {
	for _, captureFails := range []bool{false, true} {
		t.Run(map[bool]string{false: "success", true: "capture_failure"}[captureFails], func(t *testing.T) {
			d := sendFake(t)
			steps := []string{}
			original := d.send
			d.send = func(ctx context.Context, to types.JID, m *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
				steps = append(steps, "provider_ack")
				return original(ctx, to, m, extra)
			}
			d.journal = func(sendID, chat string, receipt map[string]any) error {
				steps = append(steps, "durable_receipt")
				if sendID != receiptSendID || chat != operationsChat || receipt["provider_message_id"] != "true_"+operationsChat+"_TESTSTANZA" {
					t.Fatal("journal lost exact dispatch or provider correlation", sendID, chat, receipt)
				}
				return nil
			}
			d.put = func(*events.Message, string) error {
				steps = append(steps, "capture")
				if captureFails {
					return errors.New("disk failure")
				}
				return nil
			}
			req := textFixture()
			req.SendID = receiptSendID
			_, err := sendText(context.Background(), req, d)
			if captureFails {
				requireError(t, err, "provider_operation_uncertain")
			} else if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(steps, []string{"provider_ack", "durable_receipt", "capture"}) {
				t.Fatal("receipt not durable before capture/IPC", steps)
			}
		})
	}
}

func TestOnlyVerifiedProviderAcknowledgementIsJournaled(t *testing.T) {
	for _, mode := range []string{"timeout", "wrong_id", "wrong_sender", "journal_failure", "bad_correlation"} {
		t.Run(mode, func(t *testing.T) {
			d := sendFake(t)
			sends, journals, captures := 0, 0, 0
			original := d.send
			d.send = func(ctx context.Context, to types.JID, m *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
				sends++
				reply, err := original(ctx, to, m, extra)
				if mode == "timeout" {
					return reply, context.DeadlineExceeded
				}
				if mode == "wrong_id" {
					reply.ID = "OTHER"
				}
				if mode == "wrong_sender" {
					reply.Sender = types.NewJID("999999", types.DefaultUserServer)
				}
				return reply, err
			}
			d.journal = func(string, string, map[string]any) error {
				journals++
				if mode == "journal_failure" {
					return errors.New("disk failure")
				}
				return nil
			}
			d.put = func(*events.Message, string) error { captures++; return nil }
			req := textFixture()
			req.SendID = receiptSendID
			if mode == "bad_correlation" {
				req.SendID = "user-request-id"
			}
			_, err := sendText(context.Background(), req, d)
			if err == nil || captures != 0 || sends > 1 {
				t.Fatal("invalid/ambiguous receipt captured or retried", sends, journals, captures, err)
			}
			if mode == "bad_correlation" && sends != 0 {
				t.Fatal("invalid correlation reached provider")
			}
			if mode == "journal_failure" && journals != 1 {
				t.Fatal("exact receipt was not journaled")
			}
			if mode != "journal_failure" && journals != 0 {
				t.Fatal("unverified receipt was journaled")
			}
		})
	}
}

func TestNativeSendReceiptJournalIsPrivateExactBoundedAndContentFree(t *testing.T) {
	capture := openTestStore(t)
	p := &Provider{Config: capture.Config, Capture: capture}
	receipt := map[string]any{"provider_message_id": "true_" + operationsChat + "_TESTSTANZA",
		"archive_reference": map[string]any{"provider_chat_id": operationsChat, "provider_message_id": "true_" + operationsChat + "_TESTSTANZA"}}
	if err := p.journalSendReceipt(receiptSendID, operationsChat, receipt); err != nil {
		t.Fatal(err)
	}
	dir := filepath.Join(p.Config.EventsDirectory, "send-receipts")
	entries, err := os.ReadDir(dir)
	if err != nil || len(entries) != 1 {
		t.Fatal(entries, err)
	}
	file := filepath.Join(dir, entries[0].Name())
	raw, err := os.ReadFile(file)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(file, "-"+digestBytes(raw)+".json") {
		t.Fatal("consumer cannot verify journal digest", file)
	}
	var value map[string]any
	if err = json.Unmarshal(raw, &value); err != nil {
		t.Fatal(err)
	}
	if value["kind"] != "outgoing_send_receipt" || value["send_id"] != receiptSendID ||
		value["account"] != p.Config.Account || value["requested_chat"] != operationsChat || len(value) != 7 {
		t.Fatal("incorrect exact receipt", value)
	}
	info, _ := os.Stat(file)
	if info.Mode().Perm()&0077 != 0 {
		t.Fatal("journal is public")
	}
	if err = os.WriteFile(filepath.Join(dir, "occupied"), make([]byte, 4<<20), 0600); err != nil {
		t.Fatal(err)
	}
	requireError(t, p.journalSendReceipt(receiptSendID, operationsChat, receipt), "spool_full")
}

// spec: ANYM-391
func TestNativeSendReceiptJournalIsBoundedTo1000Entries(t *testing.T) {
	capture := openTestStore(t)
	p := &Provider{Config: capture.Config, Capture: capture}
	receipt := map[string]any{"provider_message_id": "true_" + operationsChat + "_TESTSTANZA",
		"archive_reference": map[string]any{"provider_chat_id": operationsChat, "provider_message_id": "true_" + operationsChat + "_TESTSTANZA"}}
	if err := p.journalSendReceipt(receiptSendID, operationsChat, receipt); err != nil {
		t.Fatal(err)
	}
	// 998 tiny pending entries: far below the 4 MiB byte bound.
	dir := filepath.Join(p.Config.EventsDirectory, "send-receipts")
	for i := 0; i < 998; i++ {
		if err := os.WriteFile(filepath.Join(dir, fmt.Sprintf("%020d-pending.json", i)), []byte("{}"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	if err := p.journalSendReceipt(receiptSendID, operationsChat, receipt); err != nil {
		t.Fatal("entry 1000 refused", err)
	}
	requireError(t, p.journalSendReceipt(receiptSendID, operationsChat, receipt), "spool_full")
	if entries, _ := os.ReadDir(dir); len(entries) != 1000 {
		t.Fatal("journal not bounded at 1000 entries", len(entries))
	}
}
