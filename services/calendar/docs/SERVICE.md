# Family service v0.2

## Account, person and calendar

`connection_id` is a provider account, `person_id` is a family member, `calendar_ref` is a calendar. A child's calendar may be stored in a parent's account: the binding counts it as a source of the child's busy time without changing the account owner.

Apple "Calendar" is an application. Google events in this application require Google OAuth; iCloud requires CalDAV. The service does not copy events between providers, does not fix sharing automatically and does not merge manually created duplicates into one resource. Slot search merges overlapping busy intervals.

## Connection by the owner

Connections, roles, grants, write opt-in and keys are available only through the local owner CLI. MCP/REST have no such methods.

Google: enable the Calendar API in your own Cloud project, create a desktop/installed OAuth client and save its JSON in a private file `0600`:

```sh
uv run family-calendar service connect-google --id google-parent --owner egor --email ACCOUNT_EMAIL --client .local/service/google-client.json
```

The new sign-in link is temporarily saved in `.local/service/google-authorization.url`. Open it in a browser and confirm access with the right account. The callback listens on loopback for up to an hour; success shows `Family Calendar Service connected`. If the callback has expired, start a new sign-in. Do not send passwords, codes, callback URLs or tokens in chat.

Scopes: OpenID/email, `calendar.calendarlist.readonly`, `calendar.events`, `calendar.app.created`; Gmail/Drive are not requested. A new OAuth flow with state/PKCE/nonce is used. The signed Google ID token, audience/issuer, verified email and the permanent `sub` are checked. An account cannot be swapped under the same connection ID. Other people's existing refresh tokens are not imported.

An unverified application may show a warning. The publishing status of a specific project is checked separately. For External/Testing, a Calendar refresh token usually expires after seven days; the service returns `reauth_required`, not an empty calendar. [Google OAuth expiration](https://developers.google.com/identity/protocols/oauth2#expiration).

iCloud: first enter the app-specific password hidden into the legacy registry, then import and run a fresh discovery:

```sh
uv run family-calendar connection add-icloud --id apple-parent
uv run family-calendar pilot icloud discover --connection apple-parent
uv run family-calendar service import-icloud --connection apple-parent --owner egor --timezone Asia/Makassar
uv run family-calendar service discover --connection apple-parent
```

`--timezone` is the IANA zone of the calendars in which all-day events are read (default `Asia/Makassar`). Discover replaces it with the zone from the calendar's VTIMEZONE, and if there is none, keeps the one chosen at import. Import does not change the pilot's credentials, journals and allowlists. The service has its own Vault and a separate write opt-in; each account uses a separate transport.

## Family and permissions

```sh
uv run family-calendar service member --id partner --label Partner --role adult
uv run family-calendar service member --id child --label Child --role dependent
uv run family-calendar service bind --member child --calendar CALENDAR_REF
```

| Role | Actions given explicit scopes |
|---|---|
| group_admin / coordinator | Allowed events and the family's duties |
| adult | Allowed events and assigning duties to themselves |
| dependent / helper | Reading allowed sources; event writes are blocked |

An administrator does not get access to other people's calendars automatically. Access is the intersection of the key's scopes, the agent's calendar list, the member's grant and provider capabilities. `busy_only` hides titles, descriptions, places and event/source IDs, and blocks writes.

```sh
uv run family-calendar service grant --member egor --calendar CALENDAR_REF --scopes agenda:read,availability:read,event:read,plan:create,plan:execute,event:create,event:update,event:delete
uv run family-calendar service agent-key --id planner --member egor --calendar CALENDAR_REF --scopes agenda:read,availability:read,event:read,assignment:read,assignment:write,plan:create,plan:execute,event:create,event:update,event:delete --output .local/service/keys/planner.key
```

For several calendars, repeat `--calendar`. New calendars are not added automatically. Each agent gets a separate key, valid for 90 days; the DB stores only the SHA-256. `revoke-agent --id planner` revokes the key and unfinished plans. `extend-agent --id planner --days N` sets the expiry of an enabled, unexpired key to now + N days (database clock, N from 1 to 183) and prints the principal ID with the previous and new expiry; it refuses a shorter expiry (`agent_lifetime_would_shorten`), an expired key (`agent_expired`) and an unknown or revoked one (`not_found_or_forbidden`), and writes one `extend_agent` audit row. The key, scopes, calendars and principal version stay the same, so prepared plans still apply and the gateway policy needs no change. For a new key use a new principal ID. `disconnect --connection ID` disconnects the account and the write opt-in; connect it again with a new connection ID and explicit grants.

Writing is off by default. The owner's choice:

```sh
uv run family-calendar service allow-write --calendar TEST_REF --test-calendar
# For a calendar the owner explicitly chose for real operations:
uv run family-calendar service allow-write --calendar REVIEWED_REF --reviewed-calendar
uv run family-calendar service disable-write --calendar CALENDAR_REF
```

Write opt-in gives a technical capability. The agent must have a user instruction for the specific change. Test checks do not change ordinary events.

## Guardian rules

A guardian rule says which adult drops a child off at, or picks them up from, an activity by default, and in which of that adult's calendars the drive will appear. Only the owner writes rules, from this CLI; agents only read them. A rule is not a write authorization: projecting drives (ANYM-433) stays a plan the agent previews and applies under a user instruction, with the usual grant, agent calendar list and write opt-in.

```sh
uv run family-calendar service guardian-rule set --dependent child --activity Dance --duty dropoff \
  --guardian egor --source-calendar CHILD_REF --target-calendar EGOR_REF --lead-minutes 20 \
  --title "Drive to dance" --visibility private [--copy-location] [--expected-version V]
uv run family-calendar service guardian-rule disable --id RULE_ID [--expected-version V]
uv run family-calendar service guardian-rule list
```

`set` creates a rule (version 1) or replaces the one for the same child, activity and duty (version +1, re-enabling a disabled rule), and records the adult as the child's guardian. The guardian must be an enabled group_admin, coordinator or adult and the child an enabled dependent (`invalid_guardian`); the source calendar must be bound to the child and the target to the guardian (`binding_required`); the lead time is 1-180 minutes (`invalid_lead_time`), the activity 1-64 and the title 1-128 characters (`invalid_rule`); a stale `--expected-version` is `version_conflict`. The title is the owner's text: the lesson's own title, description, guests and IDs are never copied, the place only with `--copy-location`. `--visibility` has no default (`visibility_required` prints both options); the command prints who will see the drive:

| Target | `private` | `details` |
|---|---|---|
| Google | viewers of this calendar without edit rights see only busy | everyone who can see event details in this calendar sees the title and time (and place) |
| iCloud | iCloud does not enforce private events: everyone who can see this calendar sees the title and time (and place) | the same |

`disable` keeps the rule with `enabled=false` and version +1. `list` prints the rules without titles. Every `set` and `disable` writes one audit row (`guardian_rule_set`, `guardian_rule_disable`) with the rule ID only.

Readiness: rules live in migration 2. The feature is off until the owner runs `migrate` (schema version 2) and then `db-roles` again, so the runtime role can read the new tables; until then every existing tool works as before. `capabilities` reports top-level `guardian_projections` (kept by the common gateway) as `{"available": true}` or `{"available": false, "reason": "schema_not_ready" | "grants_not_ready"}`. The gateway role only reads both tables; the worker role reads rules and reads, inserts and updates projection links.

### Projecting drives (`project`)

`calendar_plan` takes `{"action": "project", "calendar_ref": SRC, "event_ref": OCC, "activity": LABEL}`: OCC is one lesson occurrence from the agenda, SRC its calendar, LABEL the rules' activity (1-64 characters; `activity` with any other action, or `project` without it, is `invalid_action`). For each enabled rule of a child bound to SRC with that activity and source, the plan adds one create step in the guardian's Google target calendar: drop-off `[start - lead, start]`, pick-up `[end - lead, end]`, both ending at the lesson boundary. The event has the rule's title, the target calendar's timezone, the lesson's place only with `--copy-location`, no description and no guests. The step shows `link` (`rule_id, rule_version, duty, guardian_id, source_event_ref`), `source` (`start, end, title`), the usual `diff` and `overlap_check`; an existing drive in the target calendar (two siblings in one class) is an overlap, the lesson the drive serves is not.

- A rule gets no step, and is listed in `projections_skipped` (`rule_id, duty, guardian_id, reason`), when it is `disabled`, its guardian's membership is off (`guardian_disabled`), the lesson already has a live (`already_projected`) or reserved (`projection_pending`) link for it, its target is not visible or writable to the key (`target_not_visible`, `target_not_writable`, `source_not_visible`), or the target is not Google (`provider_not_supported`, iCloud comes in ANYM-469). A plan with a `project` action and no step at all is `nothing_to_project` with `projections_skipped` and `activities` (the enabled activity labels of the children bound to SRC); no plan is stored. Two `project` actions for one occurrence are `duplicate_resource_action`.
- One `project` is one of the 8 actions; a plan holds at most 16 steps (`too_many_steps`, no plan stored). `capabilities.constraints` reports `plan_action_limit` and `plan_step_limit`.
- Source authority: the key must read SRC with details (`event:read` or `agenda:read`) and hold `assignment:read` (the rule-read policy; the gateway applies the same ceiling). A busy_only, unknown or other-family occurrence is `not_found_or_forbidden`, an all-day one `source_all_day`, a drive itself `projection_source_is_projection`; none of these name a rule. The plan stores SRC and its scope in the scope hash; apply and the worker recheck it before every fresh send, so a revoked grant, SRC removed from the key's list or turned busy_only gives `scope_changed` and nothing is written. Before guardian projections are available, `project` is `capability_unverified`.
- Privacy: the Google event carries only an opaque random link ID in `extendedProperties.private.deedkitLink`; nothing derived from the child, the lesson, its calendar or IDs. A `private` rule sets `visibility: private` (viewers without edit rights see busy); a `details` rule leaves the calendar default, so everyone who can see details sees title, time and, with `--copy-location`, the place. Sent with the client ID and `sendUpdates=none`.
- Reservation: when the worker admits a drive step to `sending`, the same transaction checks that the rule is still enabled at its planned version (`rule_changed`, nothing written) and inserts the `pending` link; a second pending or active link for the same lesson and rule is refused (`already_projected`) before any send. A step that fails definitely after admission (`version_conflict`, `write_rejected`) marks its pending link `removed` in the same transaction, so the lesson can be projected again; an ambiguous step keeps it. The verified transaction, also through `service reconcile`, makes the link `active` with the receipt ETag. A step admitted before a rule change is verified without resend and linked under its frozen rule version. A partial operation (drop-off verified, pick-up rejected) leaves one active link; projecting the lesson again plans only the missing duty.

### Taking drives back (`unproject`)

`calendar_plan` takes `{"action": "unproject", "calendar_ref": SRC, "event_ref": OCC}` (the drives of one lesson) or `{"action": "unproject", "operation_id": OP}` (the drives one of the key's own operations created). It plans one Google `delete` per active link, with `expected_etag` = the link's receipt ETag and the `link` and `source` blocks of `project`; the lesson, other lessons' or operations' drives and events without a link are never in the plan. Authority: `assignment:read` (the rule-read policy; the gateway applies the same ceiling) and a details read of the link's stored source calendar, not of the occurrence, so a moved, cancelled or vanished lesson is still unprojectable and `source` shows the stored span (`title` is null when the lesson is gone). A link `pending` or `repairing` (a write whose outcome is unknown) is listed in `projections_skipped` as `outcome_unknown` with nothing planned: run `service reconcile` first; a target the key can no longer delete in is `target_not_writable`. No active link: `nothing_to_unproject` with `projections_skipped`, no plan stored. An operation of another key is `not_found_or_forbidden`; `unproject` with both forms, `activity` or `event`, or `operation_id` on another action, is `invalid_action`; two actions naming the same drive are `duplicate_resource_action`.

- At send time the worker's fresh read decides: an ETag that differs from the link (someone edited the drive) is `version_conflict`, the drive stays and so does its link; a 404 or cancelled drive closes the link (`removed`) without a write. A verified delete sets the link `removed` in the step's verified transaction, so a second `unproject` plans nothing and the lesson can be projected again; of two concurrent undos one deletes and the other finds the drive gone. A crash after Google accepted the delete is verified by `service reconcile` by reading, without resending.
- In flight: the transaction that admits any DeedKit update or delete of a drive to `sending` sets its link `repairing`; while it is, another undo or write of that drive fails before any send (`outcome_unknown`) and a new `unproject` lists it as `outcome_unknown`. A definite failure after admission (`version_conflict`, `write_rejected`) sets it `active` again; an unknown outcome keeps it until `service reconcile` verifies the step (a reconcile that still sees the drive leaves the step `needs_attention`). The preview's `guardian_id` is the one the drive was made for, frozen in the projecting plan.
- Ordinary writes keep links true: a verified DeedKit `update` of a drive stores its new ETag in the link, a verified `delete` sets it `removed`, both in the step's verified transaction.

### Drives follow their lesson (ANYM-435)

Only for a `calendar_plan` call with `carry_projections: true` (capabilities `features.carry_projections`). The gateway sets it from its own `assignment:read` ceiling and drops any agent value; a gateway that predates it never sends it, so it gets the lesson step only and `activity` on `create` is `invalid_action`.

- `{"action": "create", "calendar_ref": SRC, "event": ..., "activity": LABEL}` plans the lesson create, then the linked creates of `project` for it; each linked step's `link` names the lesson by `source_step` (its plan ordinal) with `source_event_ref` null. The worker sends a linked step only after the lesson step verified (a failed or unknown lesson stops the operation; `service reconcile` verifies it first, then sends the drives) and takes `source_event_ref` from the lesson's verified receipt, the event_ref the next read publishes. Same authority, readiness gate, rules and skip reasons as `project`; no linked step at all is `nothing_to_project`, an all-day lesson `source_all_day`, `activity` on any other action `invalid_action`. One occurrence, never a series.
- `update` or `delete` of a lesson with links, by a key that reads rules and the lesson's details, adds after the lesson step one `update` (the new span by the rule's current lead and duty, the place when the rule copies it and the update sets one) or `delete` per active link, with `expected_etag` = the link ETag and `link.source_step`; the preview shows `link`, `source` (the new span) and `diff`. A `pending` link is listed in `projections_skipped` as `projection_pending`, a `repairing` one as `outcome_unknown`, a target the key cannot write as `target_not_writable`, a move to all-day as `source_all_day`. A key without rule read gets the lesson step only (the drives drift, ANYM-436).
- A drive edited since our receipt fails the worker's fresh read with `version_conflict` before any write: the lesson step stays verified, the operation is `partial`, link and drive are unchanged. A verified linked update stores the new ETag, the lesson's new span and the rule version in the link; a verified linked delete sets it `removed`.

### Drift (ANYM-436)

Changes made outside DeedKit (the school moves the lesson, a guardian edits or deletes a drive) are found from cached reads, never guessed.

- `calendar_agenda` with `projection_status: true` (capabilities `features.projection_status`; the gateway sets it under its `assignment:read` ceiling and drops any agent value) adds `projection: {link_id, duty, status, operation_id}` to each drive with a live link that a rule reader sees with details. `status`: `projection_edited` (the drive's cached ETag differs from the link's), `source_moved` (the lesson's occurrence differs from the link's stored span), `source_cancelled` (the occurrence is gone; a cancelled occurrence is never published), `in_sync`, or `unknown`. The lesson is authorized separately: without a details read of its calendar, or without complete coverage of the stored and the current span read in the last 5 minutes, the status is `unknown` and nothing about the lesson is read (the link lookup joins no occurrence). Before migration 2 and its grants an opted-in agenda reads no new table and answers as without the option. A `pending` or `repairing` link is `unknown`. A drive that is gone has no agenda row: `projection_missing` appears through `project`.
- `project` on a lesson with a live link plans the fix in one preview, each step with `reproject: true`: `source_moved` is an `update` of the drive to the new span by the rule's current lead (`expected_etag` = the link ETag, so a drive edited meanwhile is `version_conflict`); `projection_missing` (the drive gone in complete coverage of its calendar read after the link's last write) is a `create` with a new client ID on the same link. A live link's steps stay in the link's own target calendar even after the rule names another; authorization is checked on that calendar, never on the rule's new target. `projection_edited` is skipped as `projection_edited`, `source_cancelled` as `use_unproject` (also when the lesson itself is gone), `in_sync` or `unknown` as `already_projected`, a `repairing` link as `outcome_unknown`.
- Repair: the transaction that admits the re-create to `sending` moves the link `active` → `repairing` only if it still names the old target (else `projection_pending`, nothing sent), stores the new target and keeps the old one as `previous_target_id`. Before admission the worker reads the old drive by its ID: one that still exists (moved out of the read window or restored) fails the step `projection_edited` with nothing sent, and a failed read sends nothing either. The verified transaction, also through `service reconcile` by the new client ID, sets it `active` with the receipt ETag, the lesson's current span and the rule version (a link row no longer `repairing` on that target leaves the step `unknown`); a definite failure after admission (`sending`, `unknown` or `needs_attention`) gives it its old target back (a step that never was admitted, also when a reconcile re-enters it, never touches the link) (`active`, still missing on the next fresh read); an unknown result keeps `repairing`. Of two competing repairs one sends and the other fails `projection_pending`.

## Unified MCP

The official Python MCP SDK; Streamable HTTP at `http://127.0.0.1:13232/mcp` with `Authorization: Bearer KEY`. The stdio proxy reads the key from a private file; the configuration holds only its path. For Codex use absolute paths:

```sh
codex mcp add family_calendar -- /ABS/REPO/.venv/bin/python -m family_calendar.service.proxy --url http://127.0.0.1:13232/mcp --key-file /ABS/REPO/.local/service/keys/planner.key
```

The proxy allows only a loopback URL. New chats load MCP from the configuration; an already open chat may need its connection refreshed. Checking the SDK transport does not prove hot reload of the current chat.

| Tool | Purpose / REST method |
|---|---|
| family_capabilities | Capabilities and limits / capabilities |
| family_calendars | Allowed calendars / calendars |
| family_members | Family members and roles / members |
| family_agenda | Shared agenda with coverage / agenda |
| family_slots | Free intervals of members / slots |
| family_plan | Saved preview of changes / plan |
| family_apply | Admission with a persistent request_id / apply |
| family_operation | Status and receipts / operation |
| family_assignments | View duties in a window / assignments |
| family_assignment | Create/change/cancel a duty / assignment |

REST: `POST /v1/{method}`, the same JSON arguments, key and policy layer. Unknown fields are rejected; Host/Origin are limited to localhost, the body to 64 KiB, the notification stream is off. The gateway does not get the Vault and cannot change DB memberships/grants/keys. Both processes run under one OS user: this is **not isolation from a compromised process of the same user**. A deployment requires separate OS identities and a secret store.

Example of `family_slots`:

```json
{"member_ids":["egor","child"],"start":"2026-10-19T08:00:00+08:00","end":"2026-10-19T20:00:00+08:00","duration_minutes":60,"refresh":true}
```

Up to 31 days, 32 calendars, 500 events. Google pagination and the CalDAV REPORT must complete in full. Coverage older than five minutes, errors and unbound members give `unknown`. `candidate_slots` relate only to **configured and explicitly chosen sources**; a person's unknown calendars are not counted as read. A partner who is not connected does not allow claiming the availability of the whole family. Polling happens on request; this is not a full background/incremental sync.

`family_members` returns `availability_calendar_refs` and `availability_sources_status`
for each member: `configured`, `unbound` or `inaccessible`. References to inaccessible
calendars are hidden. When guardian rules are ready, each dependent also gets `guardian_rules`
(`rule_id, activity, duty, guardian_id, source_calendar_ref, target_calendar_ref, lead_minutes, title,
visibility, copy_location, enabled, version, projection_ready, not_ready_reason`), but only for a key
with `assignment:read` held by a group_admin, coordinator or adult of the family; dependents and helpers
never get the key. A calendar the key cannot read is `null`. `not_ready_reason` is `disabled`,
`target_not_visible`, `target_not_writable` (no `event:create`, provider read-only or write not enabled)
or `source_not_visible` (the lesson calendar is not readable with details). Before searching for slots, check the sources against the owner's instruction.
Do not include all visible calendars automatically: visibility does not mean participation in busy time.

`family_slots` accepts an optional `additional_calendar_refs` — additional explicitly
chosen sources. They are combined with the bindings, not replacing them; the total limit is 32.
The `source_selection` response lists the resulting sources and each member's bindings.
An additional calendar does not remove another person's `unbound`. `verified` applies
only to the specified set of sources, not to unknown calendars or the whole family.

`refresh=true` first validates the whole request, then queues the refresh.
Concurrent requests of one agent for the same window use the existing refresh.
The wait is limited to 20 seconds; the response contains `refresh.status` (`pending`, `complete`,
`failed`), safe job IDs and `retry_after_seconds`. For `pending`, repeat the same
request with `refresh=false`: this reads the state without new jobs. While the refresh
is running, the old cache does not become confirmation of free time. A provider failure
is reflected in coverage even if there was no previous cache.

`family_capabilities.constraints` describes the limits on writes, freshness, window sizes
and plans. `calendars[].allowed_event_actions` takes into account current scopes, role, visibility,
provider rights and write opt-in. The separate `write_enabled` is only a calendar setting;
on its own it does not allow the agent to write. The shared MCP uses `calendar_*` names;
the gateway additionally limits actions to the chosen tools and grants.

## Changes and recovery

Read capabilities/agenda, create a `family_plan` of 1–8 actions, check the preview within the user's instruction, call apply with a saved `request_id`, read the operation. A plan lives ten minutes. Permissions are re-checked before admission and sending. Update/delete use the `event_ref` from the agenda and the saved ETag; the worker checks the version with a fresh GET.

```json
{"actions":[{"action":"create","calendar_ref":"CAL_REF","event":{"title":"Lesson","start":"2026-10-19T16:00:00+08:00","end":"2026-10-19T17:00:00+08:00","timezone":"Asia/Makassar"}}]}
```

```json
{"plan_id":"PLAN_REF","request_id":"stable-user-request-001"}
```

An event time in `plan` is ISO 8601 with an offset or `Z`; without an offset (`YYYY-MM-DDTHH:MM[:SS]`) it is read as local time in `event.timezone` (IANA). A nonexistent or doubled local time of a DST transition is rejected as `invalid_local_time` and `ambiguous_local_time`. Agenda/slots/assignments windows require an offset. Time errors contain a fixed `hint`, and `capabilities.constraints.time_format` describes the format. `plan` saves only a local plan and does not change calendars, so it is marked non-destructive; reads are marked read-only.

The intent and `sending` are saved before the network; operation/resource advisory locks protect against two workers. A read-back is mandatory after writing. Repeating apply with the same request ID returns the same operation; a different plan under that ID is rejected. A crash/unknown is reconciled by the same Google ID or CalDAV href and **does not resend the create**. `service reconcile --operation OP_REF` checks sent unknown steps; if the result is not proven, it stays `needs_attention`. After an ambiguous step the following ones are not sent. There is no atomicity between providers; partial does not mean rollback.

CalDAV create: `If-None-Match`, update/delete: `If-Match`. Google update/delete: `If-Match`; insert: the saved client-generated ID, since conditional insert is not supported. [Google resource versions](https://developers.google.com/workspace/calendar/api/guides/version-resources).

Writing means guest-free events without special Google types, in three kinds. Fields that were not requested and ICS properties/alarms are preserved.

- **A single event**, timed (`start`, `end`, `timezone`; a missing `end` or `timezone` is `invalid_event` with `field_errors` naming it; a missing `start` or an unknown key is rejected by the request schema as `invalid_request`).
- **An all-day event**: `all_day: true`, `start`/`end` as `YYYY-MM-DD` dates, `end` exclusive and optional (default `start` + 1 day), no `timezone` (`invalid_event` with `field_errors`; `end` not after `start`, longer than 31 days, or at the edge of the supported dates (`0001-01-01`, `9999-12-31`), is `invalid_window`). It is written as Google `start.date`/`end.date` and CalDAV `DTSTART;VALUE=DATE`/`DTEND;VALUE=DATE`, and read back as one `all_day` occurrence. Updating an all-day event with `all_day: true` keeps it all-day; a switch between timed and all-day is shown as `diff.all_day`.
- **One occurrence of a recurring series**: `update` (title, time, location, description) or `delete` (= cancel) with the `event_ref` of an occurrence from the agenda. The preview shows `occurrence` (`series_ref` and the occurrence's `original_start`) and the field diff. Google: the instance is patched or deleted by its own ID with the instance ETag and `sendUpdates=none`; the master and the other instances are not written. CalDAV: one conditional `If-Match` PUT of the resource in which the override with the original `RECURRENCE-ID` is added or updated (a second edit updates the same override), or, for a cancel, a `STATUS:CANCELLED` override with the original `RECURRENCE-ID`. `EXDATE`, the master, other overrides, `VALARM`, `VTIMEZONE` and unknown properties are not changed. An occurrence cannot switch between timed and all-day. An occurrence (or series) that is `CANCELLED` in the provider is not brought back by an edit or cancelled twice: the step is a `version_conflict` and nothing is written.

Refused with `capability_unverified`: a series-wide target (a master, or an occurrence row without `series_ref`/`original_start`, for example stored before this scope existed; a refresh re-publishes it), this-and-following (`RANGE=THISANDFUTURE`), occurrences or series with guests (`attendees`, `ATTENDEE`/`ORGANIZER`), special Google types. `capabilities` reports `occurrence_write`, `all_day_write` and `provider_write` (per provider) as true; `recurrence_write`, `future_split` and `invitations` stay false. A cancelled CALDAV override is the standard model (RFC 5545 §3.8.4.4); how iCloud and the Apple Calendar client show it is **not verified live** (owner check, ANYM-234 criterion 11). `shift_series` and `split_future` of the harness are not included in MCP. Duties `dropoff/pickup/stay_with_child` are stored separately: assigned takes a slot, proposed does not. Change/cancel by `assignment_id` and `expected_version`. The consent of another person is not asserted. Guardian rules (section "Guardian rules") say who drives by default; `project` (section "Projecting drives") plans the drives into the guardians' Google calendars.

## Coverage and read errors

iCloud recurrences are expanded lazily: at most 500 instances in one calendar window, the same as in agenda/slots responses (`event_limit`). Before expansion each RRULE is estimated from above without generating: dateutil walks all instances from DTSTART to the end of the window. More than 100 000 per resource or 1 000 000 per calendar give `coverage_incomplete`, for example `FREQ=MINUTELY`/`SECONDLY` or an hourly event running for more than 11 years. An hourly reminder gives 744 events in 31 days, so such a window stays `unknown`, while a window of up to 20 days is read in full. The limit of 500 is not raised: agenda and slots apply the same one, so a larger limit of one worker would not give coverage. On `coverage_incomplete` caused by a dense calendar, request a shorter window.

Each agenda event also carries `local_start` and `local_end` (ISO 8601 with offset) in the calendar's configured IANA zone, that zone as `timezone`, and `calendar_label`. The label follows the `calendars` rule: a `busy_only` caller sees `"Calendar"`, never the real label. The UTC `start` and `end` are unchanged; a calendar whose stored zone is invalid keeps the UTC fields and omits the local ones.

A zero-length event (for example, DTSTART without DTEND) is published as a moment: `instant=true`, `busy=false`, `end` equals `start`; it does not occupy slots. In the plan preview the `before.end` of such an event also equals `start`. A zero-length write is rejected with `invalid_window`, so an update always sets a duration on the event. An event that ends before it starts gives coverage `invalid_provider_event_time`, and data that cannot be stored gives `invalid_provider_data`. These are calendar data errors, not `provider_unavailable`.

If a worker restarted during a read, that refresh becomes `failed`, and coverage gets `sync_interrupted`. It is not run again: a read that crashed the process would otherwise block all refreshes and operations. For a new read the agent calls agenda/slots with `refresh=true` again.

## Operations

`family-calendar-local status/start/stop` manages only its own macOS launch agents; PostgreSQL remains a separate persistent container. The Docker daemon must be running. Keys/passwords are not passed in argv/env; logs are private. On other OSes, run it manually or under your own supervisor.

Credentials and the raw source cache are encrypted, but event details, plans and the journal contain private data in the DB. This service does not encrypt the volume. A backup must include PostgreSQL, the Vault/key, the private DB profiles and the agents' keys. Automatic backup and restore are not yet installed/verified. This is a local MVP, not a public multi-tenant deployment.

## Availability and preview boundaries

`slots` lists `checked_member_sources` and the resulting `source_selection`.
`availability_scope=configured_bindings` with bindings alone; additional explicitly
chosen sources give `configured_and_explicit_sources`.
A binding defines a verified source but does not confirm that all of a person's
calendars are listed. Therefore the overall `availability` stays `unknown`,
`member_sources_complete=unknown`, and `required_sources_complete=false`. With fresh
coverage of all configured sources, `configured_bindings_availability=verified`,
`configured_sources_complete=true`, and `candidate_slots` contains free intervals
only inside the resulting `source_selection` (bindings plus additional sources).
On a pending/failed refresh these fields are reset to unknown/false/empty list.
`verified_slots` stays empty. Connecting an account
does not by itself create a binding and does not widen grants.

The `plan` preview returns field changes in `diff` and an `overlap_check`: half-open
busy intervals in the calendars available for reading, including busy-only without titles
and event identifiers. Resources being moved/deleted are excluded from the old
schedule; overlaps between the proposed steps are shown separately. Missing
or stale coverage marks the check `incomplete`. This is a snapshot of the cache, not a lock on
time and not a guarantee that there are no parallel edits at execution.

Calendars keep a separate provider resource identity (`identity_basis`).
`primary` and `duplicate_of` are null until the owner defines them explicitly: the same
name or a different time zone does not prove that these are duplicates.
