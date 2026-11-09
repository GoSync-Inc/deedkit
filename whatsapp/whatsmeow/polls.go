package main

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"slices"
	"time"

	"go.mau.fi/whatsmeow"
	waCommon "go.mau.fi/whatsmeow/proto/waCommon"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	waWeb "go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

// WhatsApp polls in the archive (ANYM-921). A poll creation keeps its options;
// a vote is one journal entry of kind poll_vote, never a chat message. A vote
// names its options by SHA-256 of their names (whatsmeow DecryptPollVote, or
// the already decrypted vote a history sync carries), matched here against the
// journaled poll. Option names are member-controlled text: stored bounded, as
// data. Logs and skip counts carry reason codes only.
const (
	maxPollOptions    = 12
	maxPollOptionText = 256
	maxPollQuestion   = 512
)

var (
	// A vote whose poll, secret or content cannot be read; acknowledged and counted.
	errPollVote = errors.New("poll_vote_undecryptable")
	// One selected option hash that names no option of the journaled poll.
	errPollVoteOption = errors.New("poll_vote_unknown_option")
)

// pollCreation returns the poll of a V1, V2 or V3 creation message, or nil.
func pollCreation(m *waE2E.Message) *waE2E.PollCreationMessage {
	for _, v := range []*waE2E.PollCreationMessage{m.GetPollCreationMessage(), m.GetPollCreationMessageV2(), m.GetPollCreationMessageV3()} {
		if v != nil {
			return v
		}
	}
	return nil
}

// pollBlock is the archived poll: {question, options:[{index, text}],
// selectable_count, truncated}; at most 12 options of at most 256 characters.
func pollBlock(v *waE2E.PollCreationMessage) map[string]any {
	question := envelopeText(v.GetName(), maxPollQuestion)
	truncated := question != v.GetName() || len(v.GetOptions()) > maxPollOptions
	options := []any{}
	for i, o := range v.GetOptions() {
		if i == maxPollOptions {
			break
		}
		text := envelopeText(o.GetOptionName(), maxPollOptionText)
		truncated = truncated || text != o.GetOptionName()
		options = append(options, map[string]any{"index": i, "text": text})
	}
	return map[string]any{"question": question, "options": options, "selectable_count": v.GetSelectableOptionsCount(), "truncated": truncated}
}

// pollOptionIndices maps selected option hashes to the indices of the poll's
// archived options. A hash that matches none of them is dropped and counted.
func pollOptionIndices(v *waE2E.PollCreationMessage, selected [][]byte) (indices []int, unknown int) {
	names := []string{}
	for _, o := range v.GetOptions() {
		names = append(names, o.GetOptionName())
	}
	hashes := whatsmeow.HashPollOptions(names)
	indices = []int{}
	for _, h := range selected {
		i := slices.IndexFunc(hashes, func(x []byte) bool { return bytes.Equal(x, h) })
		if i < 0 || i >= maxPollOptions {
			unknown++
		} else if !slices.Contains(indices, i) {
			indices = append(indices, i)
		}
	}
	slices.Sort(indices)
	return indices, unknown
}

// pollTarget finds the journaled poll a vote names: the chat it is kept under,
// its capture ID and its creation message. The key's fromMe is the voter's
// view (whatsmeow getOrigSenderFromKey): true means the voter created the poll.
// The capture ID is matched exactly, creator included (a group ID carries the
// sender), in the observed chat and creator form or their verified PN/LID alias.
func (s *CaptureStore) pollTarget(e *events.Message, key *waCommon.MessageKey) (string, string, *waE2E.PollCreationMessage, error) {
	if key.GetID() == "" {
		return "", "", nil, errPollVote
	}
	creator := e.Info.Sender
	if !key.GetFromMe() {
		raw := key.GetParticipant()
		if !e.Info.IsGroup {
			raw = key.GetRemoteJID()
		}
		j, err := types.ParseJID(raw)
		if err != nil || j.IsEmpty() {
			return "", "", nil, errPollVote
		}
		creator = j
	}
	fromMe := ownedIdentity(creator, s.Owner, s.OwnerLID)
	creators := s.pollIdentities(creator)
	if fromMe {
		creators = s.pollIdentities(append(creators, s.Owner, s.OwnerLID)...)
	}
	for _, chat := range s.pollIdentities(e.Info.Chat) {
		candidates := []string{fmt.Sprintf("%t_%s_%s", fromMe, LegacyChatID(chat), key.GetID())}
		if e.Info.IsGroup {
			candidates = candidates[:0]
			for _, c := range creators {
				candidates = append(candidates, fmt.Sprintf("%t_%s_%s_%s", fromMe, LegacyChatID(chat), key.GetID(), LegacyChatID(c)))
			}
		}
		for _, id := range candidates {
			// A journal read failure stays a failure; only a missing poll is skipped.
			var one int
			err := s.db.QueryRow("SELECT 1 FROM capture WHERE chat=? AND message_id=? AND length(raw)>0 LIMIT 1", LegacyChatID(chat), id).Scan(&one)
			if errors.Is(err, sql.ErrNoRows) {
				continue
			}
			if err != nil {
				return "", "", nil, err
			}
			m, err := s.Get(LegacyChatID(chat), id)
			if err != nil {
				return "", "", nil, errPollVote
			}
			if poll := pollCreation(unwrap(m).Message); poll != nil {
				return LegacyChatID(chat), id, poll, nil
			}
			return "", "", nil, errPollVote
		}
	}
	return "", "", nil, errPollVote
}

// pollIdentities is each JID and its verified PN/LID alias, without devices,
// empty entries or repeats, in the given order.
func (s *CaptureStore) pollIdentities(ids ...types.JID) []types.JID {
	out := []types.JID{}
	add := func(j types.JID) {
		if j = j.ToNonAD(); !j.IsEmpty() && !slices.Contains(out, j) {
			out = append(out, j)
		}
	}
	for _, j := range ids {
		add(j)
		if s.Alias != nil && !j.IsEmpty() {
			add(s.Alias(j.ToNonAD()))
		}
	}
	return out
}

// pollVoteEnvelope is the journal entry of one vote. The newest vote of a voter
// replaces the older one in the archive; an empty selection is a retracted vote.
func pollVoteEnvelope(account, chat, pollID string, voter types.JID, fromMe bool, indices []int, at time.Time, origin string) (map[string]any, error) {
	id := LegacyChatID(voter)
	if voter.IsEmpty() || !envelopeIdentifier(id, maxEnvelopeSender) || !envelopeIdentifier(pollID, maxEnvelopeProviderID) || at.Unix() <= 0 || at.Unix() > maxEnvelopeUnix {
		return nil, errPollVote
	}
	if origin == "history" {
		origin = "backfill"
	}
	return map[string]any{"kind": "poll_vote", "account": account, "chat": chat, "poll_provider_id": pollID, "voter": id, "from_me": fromMe, "option_indices": indices,
		"voted_at": at.UTC().Format(time.RFC3339Nano), "observed_at": time.Now().UTC().Format(time.RFC3339Nano), "origin": origin}, nil
}

// putPollVote journals one vote under the vote message's own ID. The journal
// digest ignores observation time and origin, so the same vote delivered live
// and again by a history sync is one entry.
func (s *CaptureStore) putPollVote(chat, pollID, voteID string, poll *waE2E.PollCreationMessage, voter types.JID, fromMe bool, selected [][]byte, at time.Time, origin string) error {
	indices, unknown := pollOptionIndices(poll, selected)
	for range unknown {
		if err := s.skip(errPollVoteOption); err != nil {
			return err
		}
	}
	if len(indices) == 0 && len(selected) > 0 {
		// Only unknown options: never read as a retracted vote.
		return nil
	}
	if fromMe && !s.Owner.IsEmpty() {
		// The owner votes from any device under one identity.
		voter = s.Owner
	}
	envelope, err := pollVoteEnvelope(s.Config.Account, chat, pollID, voter, fromMe, indices, at, origin)
	if err != nil {
		return err
	}
	return s.append(chat, voteID, at.Unix(), nil, envelope, origin == "history")
}

func pollVoteTime(senderMS int64, fallback time.Time) time.Time {
	if senderMS > 0 {
		return time.UnixMilli(senderMS)
	}
	return fallback
}

// capturePollVote journals a pushed (or synced, still encrypted) vote. A vote
// whose poll or secret is missing is counted under poll_vote_undecryptable and
// acknowledged, so the handler goes on with the next message. Only a store,
// disk or spool failure returns an error, as for a message.
func (p *Provider) capturePollVote(e *events.Message, origin string) error {
	err := p.pollVote(e, origin)
	if skippableCapture(err) {
		return p.Capture.skip(err)
	}
	return err
}

func (p *Provider) pollVote(e *events.Message, origin string) error {
	update := e.Message.GetPollUpdateMessage()
	chat, pollID, poll, err := p.Capture.pollTarget(e, update.GetPollCreationMessageKey())
	if err != nil {
		return err
	}
	// whatsmeow's GCM open panics on a nonce that is not 12 bytes; such a vote is
	// refused before decryption, and any other panic there is a skipped vote, so
	// the history worker and the handler never stop on one (Astra r1 B1).
	if enc := update.GetVote(); p.Client == nil || enc == nil || len(enc.GetEncIV()) != 12 || len(enc.GetEncPayload()) == 0 {
		return errPollVote
	}
	vote, err := decryptPollVote(p.Client, e)
	if err != nil {
		return errPollVote
	}
	return p.Capture.putPollVote(chat, pollID, LegacyMessageID(e), poll, e.Info.Sender, e.Info.IsFromMe, vote.GetSelectedOptions(), pollVoteTime(update.GetSenderTimestampMS(), e.Info.Timestamp), origin)
}

func decryptPollVote(client *whatsmeow.Client, e *events.Message) (vote *waE2E.PollVoteMessage, err error) {
	defer func() {
		if recover() != nil {
			vote, err = nil, errPollVote
		}
	}()
	return client.DecryptPollVote(context.Background(), e)
}

// captureHistoryPollVotes journals the votes a history sync carries on a poll
// (WebMessageInfo.pollUpdates, already decrypted). The voter follows
// whatsmeow ParseWebMessage's sender rule for the vote message's key.
func (p *Provider) captureHistoryPollVotes(poll *events.Message, updates []*waWeb.PollUpdate) error {
	if len(updates) == 0 {
		return nil
	}
	m := unwrap(poll)
	creation := pollCreation(m.Message)
	chat := LegacyChatID(m.Info.Chat)
	for _, u := range updates {
		err := p.historyPollVote(m, creation, chat, u)
		if skippableCapture(err) {
			err = p.Capture.skip(err)
		}
		if err != nil {
			return err
		}
	}
	return nil
}

func (p *Provider) historyPollVote(poll *events.Message, creation *waE2E.PollCreationMessage, chat string, u *waWeb.PollUpdate) error {
	key := u.GetPollUpdateMessageKey()
	if creation == nil || u.GetVote() == nil || key.GetID() == "" {
		return errPollVote
	}
	voter := p.Capture.Owner
	if !key.GetFromMe() && poll.Info.IsGroup {
		j, err := types.ParseJID(key.GetParticipant())
		if err != nil || j.IsEmpty() {
			return errPollVote
		}
		voter = j
	} else if !key.GetFromMe() {
		voter = poll.Info.Chat
	}
	vote := &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: poll.Info.Chat, Sender: voter, IsFromMe: key.GetFromMe(), IsGroup: poll.Info.IsGroup}, ID: key.GetID()}}
	at := pollVoteTime(u.GetSenderTimestampMS(), time.UnixMilli(u.GetServerTimestampMS()))
	return p.Capture.putPollVote(chat, LegacyMessageID(poll), LegacyMessageID(vote), creation, voter, key.GetFromMe(), u.GetVote().GetSelectedOptions(), at, "history")
}
