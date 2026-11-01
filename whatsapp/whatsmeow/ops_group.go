package main

import (
	"cmp"
	"errors"
	"regexp"
	"slices"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
)

func (e *operationEnv) groupOperation(op string) (result any, err error) {
	ctx, job, d, target, check, mutate, read, submitted, getGroup, text, integer, user, users := e.ctx, e.job, e.d, e.target, e.check, e.mutate, e.read, e.submitted, e.getGroup, e.text, e.integer, e.user, e.users
	switch op {
	case "create_group":
		title, e := text("title", 128, false, nil)
		if e != nil {
			return nil, e
		}
		ids, e := users("user_ids", 20)
		if e != nil {
			return nil, e
		}
		var g *types.GroupInfo
		if err = mutate("", func() error {
			var e error
			g, e = d.createGroup(ctx, whatsmeow.ReqCreateGroup{Name: title, Participants: ids})
			return e
		}); err != nil {
			return nil, err
		}
		data := map[string]any{}
		if g != nil {
			p, e := d.policy()
			if e == nil && p.Enabled && p.Allows(LegacyChatID(g.JID)) {
				data["provider_chat"] = LegacyChatID(g.JID)
				for key, value := range participantResults(g.Participants, ids) {
					data[key] = value
				}
			}
		}
		return submitted(data)
	case "rename_group", "set_group_description", "add_participant", "remove_participant", "promote_participant", "demote_participant", "leave_group", "get_invite_link", "revoke_invite_link":
		g, e := getGroup()
		if e != nil {
			return nil, e
		}
		// The target read's cache refresh lands before the change (ANYM-431).
		if d.groupSettle != nil {
			if e := d.groupSettle(ctx); e != nil {
				return nil, e
			}
		}
		if op == "rename_group" {
			value, e := text("title", 128, false, nil)
			if e != nil {
				return nil, e
			}
			err = mutate("", func() error { return d.groupName(ctx, target, value) })
		} else if op == "set_group_description" {
			value, e := text("description", 255, true, nil)
			if e != nil {
				return nil, e
			}
			err = mutate("", func() error { return d.groupDescription(ctx, target, g.TopicID, value) })
		} else if op == "leave_group" {
			err = mutate("", func() error { return d.leave(ctx, target) })
		} else if op == "get_invite_link" || op == "revoke_invite_link" {
			var link string
			err = mutate("", func() error { var e error; link, e = d.invite(ctx, target, op == "revoke_invite_link"); return e })
			if err != nil {
				return nil, err
			}
			if e := check(""); e != nil {
				return nil, e
			}
			if op == "get_invite_link" && regexp.MustCompile(`^https://chat\.whatsapp\.com/[a-zA-Z0-9_-]{1,128}$`).MatchString(link) {
				return submitted(map[string]any{"invite_link": link})
			}
		} else {
			j, e := user("user_id")
			if e != nil {
				return nil, e
			}
			action := map[string]whatsmeow.ParticipantChange{"add_participant": whatsmeow.ParticipantChangeAdd, "remove_participant": whatsmeow.ParticipantChangeRemove, "promote_participant": whatsmeow.ParticipantChangePromote, "demote_participant": whatsmeow.ParticipantChangeDemote}[op]
			var participants []types.GroupParticipant
			err = mutate("", func() error {
				var e error
				participants, e = d.participants(ctx, target, []types.JID{j}, action)
				return e
			})
			if err != nil {
				return nil, err
			}
			if e := check(""); e != nil {
				return nil, e
			}
			return submitted(participantResults(participants, []types.JID{j}))
		}
		if err != nil {
			return nil, err
		}
		return submitted(nil)
	case "set_group_messages_admins_only", "set_group_info_admins_only", "set_join_approval":
		var enabled bool
		if decodeValue(job.Parameters["enabled"], &enabled) != nil {
			return nil, errors.New("operation_invalid_parameters")
		}
		setting := groupSettingOf[op]
		g, e := getGroup()
		if e != nil {
			return nil, e
		}
		if d.groupSettle != nil {
			if e := d.groupSettle(ctx); e != nil {
				return nil, e
			}
		}
		if !ownGroupAdmin(g, d.owner, d.ownerLID) {
			return nil, errors.New("forbidden")
		}
		previous := groupSettingValue(g, setting)
		if previous == enabled {
			return read(map[string]any{"enabled": enabled, "previous": previous, "changed": false}, "")
		}
		if err = mutate("", func() error { return d.groupSetting(ctx, target, setting, enabled) }); err != nil {
			return nil, err
		}
		// Only a read-back showing the new value confirms the change.
		after, e := d.group(ctx, target)
		if e != nil {
			return nil, e
		}
		if e := check(""); e != nil {
			return nil, e
		}
		data := map[string]any{"enabled": enabled, "previous": previous, "changed": true}
		if after != nil && LegacyChatID(after.JID) == job.ProviderChat && groupSettingValue(after, setting) == enabled {
			return operationOutput("confirmed", data, "")
		}
		return submitted(data)
	case "list_join_requests":
		limit, e := integer("limit", 1, 100, 50)
		if e != nil {
			return nil, e
		}
		requests, e := d.joinRequests(ctx, target)
		if e != nil {
			return nil, e
		}
		slices.SortStableFunc(requests, func(a, b types.GroupParticipantRequest) int { return a.RequestedAt.Compare(b.RequestedAt) })
		more := len(requests) > limit
		if more {
			requests = requests[:limit]
		}
		rows := []any{}
		for _, request := range requests {
			j := request.JID.ToNonAD()
			var name, at any
			if c, e := d.contact(ctx, j); e == nil && c.Found && cmp.Or(c.FullName, c.PushName) != "" {
				name = truncate(cmp.Or(c.FullName, c.PushName), 512)
			}
			if !request.RequestedAt.IsZero() {
				at = request.RequestedAt.Unix()
			}
			row := map[string]any{"id": LegacyChatID(j), "requested_at": at, "name": name}
			// The whole answer stays within the receipt budget; rows that do
			// not fit are reported as more, never as a failed read.
			if joinRequestsSize(append(rows, row)) > joinRequestsBytes {
				more = true
				break
			}
			rows = append(rows, row)
		}
		// Names are the requesters' own text: data, never instructions.
		return read(joinRequestsPage(rows, more), "")
	case "approve_join_requests", "reject_join_requests":
		ids, e := users("user_ids", 20)
		if e != nil {
			return nil, e
		}
		g, e := getGroup()
		if e != nil {
			return nil, e
		}
		if d.groupSettle != nil {
			if e := d.groupSettle(ctx); e != nil {
				return nil, e
			}
		}
		if !ownGroupAdmin(g, d.owner, d.ownerLID) {
			return nil, errors.New("forbidden")
		}
		var answered []types.GroupParticipant
		err = mutate("", func() error {
			var e error
			answered, e = d.joinAction(ctx, target, ids, op == "approve_join_requests")
			return e
		})
		if err != nil {
			return nil, err
		}
		if e := check(""); e != nil {
			return nil, e
		}
		data, proven := joinRequestResults(answered, ids)
		if proven {
			return operationOutput("confirmed", data, "")
		}
		return operationOutput("unknown", data, "")
	}
	return nil, errors.New("operation_unsupported")
}
