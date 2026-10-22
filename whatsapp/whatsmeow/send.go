package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
	"unicode/utf16"
	"unicode/utf8"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

// This small dependency boundary permits tests of the actual policy/receipt
// contract without creating a client, authorizing a device or sending anything.
type sendDependencies struct {
	policy   func() (Policy, error)
	get      func(string, string) (*events.Message, error)
	put      func(*events.Message, string) error
	send     func(context.Context, types.JID, *waE2E.Message, whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error)
	newID    func() string
	owner    types.JID
	ownerLID types.JID
	lidForPN func(context.Context, types.JID) (types.JID, error)
	pnForLID func(context.Context, types.JID) (types.JID, error)
	journal  func(string, string, map[string]any) error
	receipt  func(map[string]any) error
}

func (p *Provider) sendDependencies() sendDependencies {
	owner, _ := ParseChatID(p.Config.ExpectedWID)
	return sendDependencies{policy: p.policy, get: p.Capture.Get, put: p.Capture.Put, owner: owner,
		journal:  p.journalSendReceipt,
		ownerLID: p.Client.Store.GetLID(),
		lidForPN: p.Client.Store.LIDs.GetLIDForPN, pnForLID: p.Client.Store.LIDs.GetPNForLID,
		newID: func() string { return p.Client.GenerateMessageID() },
		send: func(ctx context.Context, to types.JID, message *waE2E.Message, extra whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
			return p.Client.SendMessage(ctx, to, message, extra)
		}}
}

type textRequest struct {
	SendID          string  `json:"send_id,omitempty"`
	Chat            string  `json:"chat"`
	Text            string  `json:"text"`
	ExpiresAt       float64 `json:"expires_at"`
	ReplyProviderID string  `json:"reply_provider_id,omitempty"`
	LinkPreview     *bool   `json:"link_preview,omitempty"`
}

func decodeStrict(raw json.RawMessage, value any) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(value); err != nil {
		return err
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return errors.New("invalid_json")
	}
	return nil
}

func safeText(value string, maximum int, empty bool) bool {
	if !utf8.ValidString(value) || len(utf16.Encode([]rune(value))) > maximum || (!empty && strings.TrimSpace(value) == "") {
		return false
	}
	for _, r := range value {
		if (r < 32 && r != '\n' && r != '\r' && r != '\t') || r == 127 {
			return false
		}
	}
	return true
}

var serializedMessagePattern = regexp.MustCompile(`^[a-zA-Z0-9_.:@-]{1,256}$`)

func validMessageReference(chat, id string) bool {
	return serializedMessagePattern.MatchString(id) &&
		((strings.HasPrefix(id, "true_"+chat+"_") && len(id) > len("true_"+chat+"_")) ||
			(strings.HasPrefix(id, "false_"+chat+"_") && len(id) > len("false_"+chat+"_")))
}

func exactCaptured(d sendDependencies, chat, id string) (*events.Message, error) {
	if !validMessageReference(chat, id) {
		return nil, errors.New("operation_target_invalid")
	}
	m, err := d.get(chat, id)
	if err != nil || m == nil {
		return nil, errors.New("operation_target_unavailable")
	}
	if LegacyChatID(m.Info.Chat) != chat || LegacyMessageID(m) != id || m.Message == nil ||
		(m.Message.GetProtocolMessage() != nil && m.Message.GetProtocolMessage().GetType() == waE2E.ProtocolMessage_REVOKE) {
		return nil, errors.New("operation_target_invalid")
	}
	return m, nil
}

func messageKey(m *events.Message) *waE2E.ContextInfo {
	return &waE2E.ContextInfo{StanzaID: proto.String(m.Info.ID),
		Participant:   proto.String(m.Info.Sender.ToNonAD().String()),
		QuotedMessage: proto.Clone(m.Message).(*waE2E.Message)}
}

func (p *Provider) SendText(ctx context.Context, raw json.RawMessage) (any, error) {
	var req textRequest
	if decodeStrict(raw, &req) != nil {
		return nil, errors.New("send_target_invalid")
	}
	return boundedSendText(ctx, req, p.sendDependencies())
}

func boundedSendText(ctx context.Context, req textRequest, d sendDependencies) (any, error) {
	return runBounded(ctx, sendTextDeadline, func(ctx context.Context, _ *operationGate) (any, error) {
		return sendText(ctx, req, d)
	}, func(bool) (any, error) {
		// The steps before the send are local reads; the outbox records the
		// answer as unknown and never resends (ANYM-167).
		return nil, errors.New("provider_operation_uncertain")
	}, nil)
}

// sendTextDeadline answers send_text 10 s inside the outbox's 30 s dispatch
// bound (outgoing.py), so its expiry never tears down the native process
// (ANYM-441).
var sendTextDeadline = 20 * time.Second

func sendText(ctx context.Context, req textRequest, d sendDependencies) (any, error) {
	check := func() error {
		policy, err := d.policy()
		if err != nil || !policy.Enabled || !policy.SendEnabled {
			return errors.New("sending_disabled")
		}
		if _, err = ParseChatID(req.Chat); err != nil || !policy.Allows(req.Chat) || !safeText(req.Text, 4000, false) {
			return errors.New("send_target_invalid")
		}
		if math.IsNaN(req.ExpiresAt) || math.IsInf(req.ExpiresAt, 0) || req.ExpiresAt <= float64(time.Now().UnixNano())/1e9 {
			return errors.New("send_expired")
		}
		return nil
	}
	if err := check(); err != nil {
		return nil, err
	}
	if req.SendID != "" {
		if !regexp.MustCompile(`^send_[a-f0-9]{32}$`).MatchString(req.SendID) || d.journal == nil {
			return nil, errors.New("send_target_invalid")
		}
		d.receipt = func(receipt map[string]any) error { return d.journal(req.SendID, req.Chat, receipt) }
	}
	// Native WhatsApp does not fetch arbitrary links. Enabling rich previews needs
	// a separate bounded URL-fetch contract; do not silently claim it happened.
	if req.LinkPreview != nil && *req.LinkPreview {
		return nil, errors.New("link_preview_unsupported")
	}
	to, _ := ParseChatID(req.Chat)
	message := &waE2E.Message{Conversation: proto.String(req.Text)}
	if req.ReplyProviderID != "" {
		quoted, err := exactCaptured(d, req.Chat, req.ReplyProviderID)
		if err != nil {
			return nil, errors.New("send_target_invalid")
		}
		message = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String(req.Text), ContextInfo: messageKey(quoted)}}
	}
	if err := check(); err != nil {
		return nil, err
	}
	return sendAndCaptureReceipt(ctx, d, to, message)
}

// A server acknowledgement is evidence of acceptance, not delivery or reading.
// Every error after invoking send is ambiguous and must never trigger a retry.
func sendAndCapture(ctx context.Context, d sendDependencies, to types.JID, message *waE2E.Message) (string, error) {
	receipt, err := sendAndCaptureReceipt(ctx, d, to, message)
	if err != nil {
		return "", err
	}
	return receipt["provider_message_id"].(string), nil
}

func sendAndCaptureReceipt(ctx context.Context, d sendDependencies, to types.JID, message *waE2E.Message) (map[string]any, error) {
	id := d.newID()
	if id == "" || !serializedMessagePattern.MatchString(id) {
		return nil, errors.New("send_target_invalid")
	}
	// After a deadline answer, no send may start (ANYM-441).
	if !startChange(ctx) {
		return nil, errors.New("operation_target_unavailable")
	}
	resp, err := d.send(ctx, to, message, whatsmeow.SendRequestExtra{ID: id})
	if err != nil {
		return nil, errors.New("provider_operation_uncertain")
	}
	if resp.ID != id || resp.Timestamp.IsZero() || resp.Chat.IsEmpty() ||
		(resp.Chat.ToNonAD() != to.ToNonAD() && !verifiedSendLIDPair(ctx, d, to, resp.Chat)) ||
		resp.Sender.IsEmpty() || !ownedIdentity(resp.Sender, d.owner, d.ownerLID) {
		return nil, errors.New("send_receipt_mismatch")
	}
	// The provider may route a PN request through its verified LID. Retain that
	// actual namespace and sender so a later own-device echo has the same archive
	// identity. A second synthetic PN archive copy would duplicate the message.
	source := types.MessageSource{Chat: resp.Chat, Sender: resp.Sender, IsFromMe: true, IsGroup: resp.Chat.Server == types.GroupServer}
	if resp.Chat.ToNonAD() != to.ToNonAD() {
		source.RecipientAlt = to.ToNonAD()
	}
	if resp.Sender.Server == types.HiddenUserServer {
		source.SenderAlt = d.owner.ToNonAD()
		source.AddressingMode = types.AddressingModeLID
	} else if d.ownerLID.Server == types.HiddenUserServer && !d.ownerLID.IsEmpty() {
		source.SenderAlt = d.ownerLID.ToNonAD()
		source.AddressingMode = types.AddressingModePN
	}
	event := &events.Message{Info: types.MessageInfo{MessageSource: source, ID: id, Timestamp: resp.Timestamp}, Message: message, RawMessage: message}
	// The existing outbox contract references the requested public chat ID. This
	// receipt projection is not written back to capture and never rewrites archive
	// IDs or broadens policy to another namespace.
	public := *event
	public.Info.Chat, public.Info.Sender = to, d.owner
	receipt := map[string]any{"provider_message_id": LegacyMessageID(&public), "archive_reference": map[string]any{
		"provider_chat_id": LegacyChatID(event.Info.Chat), "provider_message_id": LegacyMessageID(event),
	}}
	// Only a verified provider acknowledgement reaches this journal. It is durable
	// before capture/IPC can fail, and carries the exact internal dispatch ID.
	if d.receipt != nil {
		if err = d.receipt(receipt); err != nil {
			return nil, errors.New("provider_operation_uncertain")
		}
	}
	if err = d.put(event, "live"); err != nil {
		return nil, errors.New("provider_operation_uncertain")
	}
	return receipt, nil
}

func (p *Provider) journalSendReceipt(sendID, requested string, receipt map[string]any) error {
	dir := filepath.Join(p.Config.EventsDirectory, "send-receipts")
	if err := privateRoot(dir); err != nil {
		return err
	}
	data := mustJSON(map[string]any{"kind": "outgoing_send_receipt", "account": p.Config.Account,
		"send_id": sendID, "requested_chat": requested, "provider_message_id": receipt["provider_message_id"],
		"archive_reference": receipt["archive_reference"], "observed_at": time.Now().UTC().Format(time.RFC3339Nano)})
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	usage, err := measureSpool(entries)
	if err != nil {
		return err
	}
	if usage.count >= 1000 || usage.bytes+int64(len(data)) > 4<<20 {
		return errors.New("spool_full")
	}
	if err = p.Capture.checkDisk(int64(len(data))); err != nil {
		return err
	}
	return atomicPrivate(dir, fmt.Sprintf("%020d-%s.json", time.Now().UnixNano(), digestBytes(data)), data)
}

// Pinned SendMessage only rewrites PN destinations to LID, never groups or the
// reverse direction. Both session-store lookups must agree; equal user strings,
// an arbitrary receipt or a one-way/conflicting mapping are not alias evidence.
// These are local store reads, not account discovery or a network retry.
func verifiedSendLIDPair(ctx context.Context, d sendDependencies, pn, lid types.JID) bool {
	pn, lid = pn.ToNonAD(), lid.ToNonAD()
	if pn.Server != types.DefaultUserServer || lid.Server != types.HiddenUserServer ||
		pn.IsEmpty() || lid.IsEmpty() || d.lidForPN == nil || d.pnForLID == nil {
		return false
	}
	storedLID, err := d.lidForPN(ctx, pn)
	if err != nil || storedLID.ToNonAD() != lid {
		return false
	}
	storedPN, err := d.pnForLID(ctx, lid)
	return err == nil && storedPN.ToNonAD() == pn
}
