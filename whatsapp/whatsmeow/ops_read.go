package main

import (
	"errors"
	"sort"
	"time"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

func (e *operationEnv) readOperation(op string) (result any, err error) {
	ctx, job, d, target, check, read, getMessage, getGroup, user, page, contactRow := e.ctx, e.job, e.d, e.target, e.check, e.read, e.getMessage, e.getGroup, e.user, e.page, e.contactRow
	switch op {
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
	}
	return nil, errors.New("operation_unsupported")
}
