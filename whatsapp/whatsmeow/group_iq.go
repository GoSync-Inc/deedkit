// The group node shapes and the participant parser below are transcribed from
// go.mau.fi/whatsmeow group.go at f16f9ca30d6a, Copyright (c) 2021 Tulir Asokan.
//
// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at http://mozilla.org/MPL/2.0/.

package main

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	waBinary "go.mau.fi/whatsmeow/binary"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
)

// Group operations bypass whatsmeow's group helpers: those send through
// sendGroupIQ without NoRetry, so a socket drop makes the library resend an
// already applied change after reconnecting (request.go retryFrame). groupIQ
// builds the same nodes as the pinned helpers (group.go at f16f9ca30d6a) and
// sends them once; a drop is an uncertain outcome, never a replay (ANYM-431).
type groupIQ struct {
	send     func(context.Context, whatsmeow.DangerousInfoQuery) (*waBinary.Node, error)
	parse    func(*waBinary.Node) (*types.GroupInfo, error)
	pnForLID func(context.Context, types.JID) (types.JID, error)
	tcToken  func(context.Context, types.JID) ([]byte, error)
	newID    func() string
	// refresh applies the library's local side effects of a group read (group
	// cache, LID mappings, redacted phones). It runs off the reply path: the
	// group cache lock can be held across a network read by a concurrent send.
	refresh func(*types.GroupInfo)
}

// groupOperationDeadline bounds the IPC answer of every group operation, well
// inside the Python worker's 30 s operation timeout (commands.py).
var groupOperationDeadline = 20 * time.Second

// groupIQMaxTimeout bounds one group IQ when its context has no deadline.
const groupIQMaxTimeout = 20 * time.Second

const (
	groupIQGet whatsmeow.DangerousInfoQueryType = "get"
	groupIQSet whatsmeow.DangerousInfoQueryType = "set"
)

func newGroupIQ(c *whatsmeow.Client) groupIQ {
	in := c.DangerousInternals()
	return groupIQ{send: in.SendIQ, parse: in.ParseGroupNode, pnForLID: c.Store.LIDs.GetPNForLID, tcToken: in.EnsureTCToken,
		newID: func() string { return string(c.GenerateMessageID()) },
		refresh: func(info *types.GroupInfo) {
			go storeGroupInfo(info, in.CacheGroupInfo, c.Store.LIDs.PutManyLIDMappings, c.Store.Contacts.PutManyRedactedPhones,
				func(format string, args ...any) { c.Log.Warnf(format, args...) })
		}}
}

// storeGroupInfo mirrors the tail of the pinned getGroupInfo: cache first, then
// best-effort storage whose errors are only logged.
func storeGroupInfo(info *types.GroupInfo, cache func(*types.GroupInfo, bool) ([]store.LIDMapping, []store.RedactedPhoneEntry),
	putLIDs func(context.Context, []store.LIDMapping) error, putPhones func(context.Context, []store.RedactedPhoneEntry) error,
	warn func(string, ...any)) {
	ctx, done := context.WithTimeout(context.Background(), time.Minute)
	defer done()
	lids, phones := cache(info, true)
	if err := putLIDs(ctx, lids); err != nil {
		warn("Failed to store LID mappings for members of %s: %v", info.JID, err)
	}
	if err := putPhones(ctx, phones); err != nil {
		warn("Failed to store redacted phones for members of %s: %v", info.JID, err)
	}
}

func (g groupIQ) query(ctx context.Context, iqType whatsmeow.DangerousInfoQueryType, jid types.JID, content waBinary.Node) (*waBinary.Node, error) {
	timeout := groupIQMaxTimeout
	if deadline, ok := ctx.Deadline(); ok {
		timeout = min(timeout, time.Until(deadline))
	}
	// A zero Timeout would select the library's 75 s default.
	if timeout <= 0 || ctx.Err() != nil {
		return nil, cmp.Or(ctx.Err(), context.DeadlineExceeded)
	}
	return g.send(ctx, whatsmeow.DangerousInfoQuery{Namespace: "w:g2", Type: iqType, To: jid, Content: []waBinary.Node{content}, Timeout: timeout, NoRetry: true})
}

func (g groupIQ) info(ctx context.Context, jid types.JID) (*types.GroupInfo, error) {
	res, err := g.query(ctx, groupIQGet, jid, waBinary.Node{Tag: "query", Attrs: waBinary.Attrs{"request": "interactive"}})
	if errors.Is(err, whatsmeow.ErrIQNotFound) {
		return nil, wrapGroupError(whatsmeow.ErrGroupNotFound, err)
	} else if errors.Is(err, whatsmeow.ErrIQForbidden) {
		return nil, wrapGroupError(whatsmeow.ErrNotInGroup, err)
	} else if err != nil {
		return nil, err
	}
	node, ok := res.GetOptionalChildByTag("group")
	if !ok {
		return nil, &whatsmeow.ElementMissingError{Tag: "groups", In: "response to group info query"}
	}
	info, err := g.parse(&node)
	if err != nil {
		return info, err
	}
	g.refresh(info)
	return info, nil
}

func (g groupIQ) create(ctx context.Context, req whatsmeow.ReqCreateGroup) (*types.GroupInfo, error) {
	participants := make([]waBinary.Node, len(req.Participants), len(req.Participants)+1)
	participants = append(participants, waBinary.Node{Tag: "member_add_mode", Content: string(cmp.Or(req.MemberAddMode, types.GroupMemberAddModeAllMember))})
	for i, participant := range req.Participants {
		node, err := g.participant(ctx, participant.ToNonAD(), true)
		if err != nil {
			return nil, err
		}
		participants[i] = node
	}
	// The adapter never creates communities, announcement-only, locked or
	// disappearing groups: these are the pinned helper's defaults.
	participants = append(participants, waBinary.Node{Tag: "ephemeral", Attrs: waBinary.Attrs{"expiration": 0}},
		waBinary.Node{Tag: "membership_approval_mode", Content: []waBinary.Node{{Tag: "group_join", Attrs: waBinary.Attrs{"state": "off"}}}})
	attrs := waBinary.Attrs{}
	if req.Name != "" {
		attrs["subject"] = req.Name
	}
	res, err := g.query(ctx, groupIQSet, types.GroupServerJID, waBinary.Node{Tag: "create", Attrs: attrs, Content: participants})
	if err != nil {
		return nil, err
	}
	node, ok := res.GetOptionalChildByTag("group")
	if !ok {
		return nil, &whatsmeow.ElementMissingError{Tag: "group", In: "response to create group query"}
	}
	return g.parse(&node)
}

// participant builds one participant node; add (and create) attach the stored
// phone number of a LID and the stored privacy token, as the pinned helpers do.
func (g groupIQ) participant(ctx context.Context, jid types.JID, add bool) (waBinary.Node, error) {
	node := waBinary.Node{Tag: "participant", Attrs: waBinary.Attrs{"jid": jid}}
	if !add {
		return node, nil
	}
	if jid.Server == types.HiddenUserServer {
		pn, err := g.pnForLID(ctx, jid)
		if err != nil {
			return node, fmt.Errorf("failed to get phone number for %s: %v", jid, err)
		} else if !pn.IsEmpty() {
			node.Attrs["phone_number"] = pn
		}
	}
	token, err := g.tcToken(ctx, jid)
	if err != nil {
		return node, fmt.Errorf("failed to get privacy token for participant %s: %v", jid, err)
	} else if len(token) > 0 {
		node.Content = []waBinary.Node{{Tag: "privacy", Content: token}}
	}
	return node, nil
}

func (g groupIQ) setName(ctx context.Context, jid types.JID, name string) error {
	_, err := g.query(ctx, groupIQSet, jid, waBinary.Node{Tag: "subject", Content: []byte(name)})
	return err
}

// setTopic takes the previous topic ID from the caller's target read instead of
// a second group read (ANYM-431).
func (g groupIQ) setTopic(ctx context.Context, jid types.JID, previousID, topic string) error {
	attrs := waBinary.Attrs{"id": g.newID()}
	if previousID != "" {
		attrs["prev"] = previousID
	}
	content := []waBinary.Node{{Tag: "body", Content: []byte(topic)}}
	if len(topic) == 0 {
		attrs["delete"] = "true"
		content = nil
	}
	_, err := g.query(ctx, groupIQSet, jid, waBinary.Node{Tag: "description", Attrs: attrs, Content: content})
	return err
}

func (g groupIQ) leave(ctx context.Context, jid types.JID) error {
	_, err := g.query(ctx, groupIQSet, types.GroupServerJID, waBinary.Node{Tag: "leave", Content: []waBinary.Node{{Tag: "group", Attrs: waBinary.Attrs{"id": jid}}}})
	return err
}

func (g groupIQ) participants(ctx context.Context, jid types.JID, changes []types.JID, action whatsmeow.ParticipantChange) ([]types.GroupParticipant, error) {
	content := make([]waBinary.Node, len(changes))
	for i, participant := range changes {
		node, err := g.participant(ctx, participant, action == whatsmeow.ParticipantChangeAdd)
		if err != nil {
			return nil, err
		}
		content[i] = node
	}
	res, err := g.query(ctx, groupIQSet, jid, waBinary.Node{Tag: string(action), Content: content})
	if err != nil {
		return nil, err
	}
	node, ok := res.GetOptionalChildByTag(string(action))
	if !ok {
		return nil, &whatsmeow.ElementMissingError{Tag: string(action), In: "response to group participants update"}
	}
	children := node.GetChildrenByTag("participant")
	out := make([]types.GroupParticipant, len(children))
	for i, child := range children {
		out[i] = parseGroupParticipant(&child)
	}
	return out, nil
}

func (g groupIQ) invite(ctx context.Context, jid types.JID, reset bool) (string, error) {
	iqType := groupIQGet
	if reset {
		iqType = groupIQSet
	}
	res, err := g.query(ctx, iqType, jid, waBinary.Node{Tag: "invite"})
	if errors.Is(err, whatsmeow.ErrIQNotAuthorized) {
		return "", wrapGroupError(whatsmeow.ErrGroupInviteLinkUnauthorized, err)
	} else if errors.Is(err, whatsmeow.ErrIQNotFound) {
		return "", wrapGroupError(whatsmeow.ErrGroupNotFound, err)
	} else if errors.Is(err, whatsmeow.ErrIQForbidden) {
		return "", wrapGroupError(whatsmeow.ErrNotInGroup, err)
	} else if err != nil {
		return "", err
	}
	code, ok := res.GetChildByTag("invite").Attrs["code"].(string)
	if !ok {
		return "", fmt.Errorf("didn't find invite code in response")
	}
	return whatsmeow.InviteLinkPrefix + code, nil
}

// parseGroupParticipant transcribes the pinned, unexported parseParticipant.
func parseGroupParticipant(child *waBinary.Node) types.GroupParticipant {
	ag := child.AttrGetter()
	kind := ag.OptionalString("type")
	participant := types.GroupParticipant{IsAdmin: kind == "admin" || kind == "superadmin", IsSuperAdmin: kind == "superadmin",
		JID: ag.JID("jid"), DisplayName: ag.OptionalString("display_name")}
	if participant.JID.Server == types.HiddenUserServer {
		participant.LID = participant.JID
		participant.PhoneNumber = ag.OptionalJIDOrEmpty("phone_number")
	} else if participant.JID.Server == types.DefaultUserServer {
		participant.PhoneNumber = participant.JID
		participant.LID = ag.OptionalJIDOrEmpty("lid")
	}
	if code := ag.OptionalInt("error"); code != 0 {
		participant.Error = code
		if request, ok := child.GetOptionalChildByTag("add_request"); ok {
			rag := request.AttrGetter()
			participant.AddRequest = &types.GroupParticipantAddRequest{Code: rag.String("code"), Expiration: rag.UnixTime("expiration")}
		}
	}
	return participant
}

// wrapGroupError keeps errors.Is for both the library's group error and the IQ
// error, like the pinned wrapIQError.
func wrapGroupError(human, iq error) error { return fmt.Errorf("%w: %w", human, iq) }

// operationGate serializes the deadline answer with the start of a change: once
// the answer is chosen no change may start, and a started change is answered
// as uncertain.
type operationGate struct {
	mu       sync.Mutex
	answered bool
	started  bool
}

func (g *operationGate) start() bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.answered {
		return false
	}
	g.started = true
	return true
}

func (g *operationGate) answer() (started bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.answered = true
	return g.started
}
