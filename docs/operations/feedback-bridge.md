# Feedback bridge: DeedKit feedback and BB Tasks, both ways (ANYM-990)

`scripts/feedback_bridge.py` runs on the owner's Ryzen host. It turns every agent feedback item into a task in the
BB tracker `DK` and follows a status change on either side. It reads only metadata (the gateway tool
`messages_export_feedback_metadata`, ANYM-988) and the loop's released brief; the description, references, status note
and key ID of an item are never read. The host listens on nothing: a systemd `--user` unit long-polls the maintainer
subscription (`wait_for_subscription_events`, 25 seconds), and a bb script automation runs the same applier every
10 minutes as the fallback. There is no webhook receiver and no Funnel (DeedKit's delivery egress is 443 only).

| Piece | Where |
|---|---|
| Program | `~/.local/lib/deedkit-feedback-bridge/feedback_bridge.py` (one file, standard library only) |
| Credentials | `~/.config/deedkit/feedback-bridge.env` and the key file it names, both 0600 (a looser file is refused) |
| State | `~/.local/state/deedkit-feedback-bridge/` (0700): `ledger.sqlite3` (links, create journal, cursor, subscription ID), `bridge.lock` |
| Unit | `deedkit-feedback-bridge` (`watch`) |
| Automation | `deedkit-feedback-bridge-run` (`run`, every 10 minutes) |

The key holds the gateway tools `messages_export_feedback_metadata`, `messages_create_subscription`,
`messages_list_subscriptions`, `messages_wait_for_subscription_events`, `messages_fetch_subscription_events` and
`messages_ack_subscription_events`; the reverse path adds `messages_set_feedback_triage` (permission
`feedback-maintainer-write`, ANYM-992). The key holds `feedback-maintainer-read`, `feedback-maintainer-write`,
`subscription-write` and `subscription-ack`, and no `read`. Until the tool is granted the reverse write stays idle:
the bridge prints `reverse_unavailable_<code>` to stderr and exits 0 (`not_found_or_forbidden` is the gateway's one answer for a
tool the key cannot call).

## Mapping

| DeedKit status | Task status |
|---|---|
| received | backlog |
| accepted | todo (a human's in_progress or in_review stays) |
| fixed, verify_requested | in_review |
| verified | done |
| declined, duplicate | canceled |
| reopened | backlog |

Task side to DeedKit: todo, in_progress or in_review on a received or reopened item writes `accepted` with the card that
plans it, `spec_ref`: the task's label `spec:ANYM-<n>` (exactly one), else the primary card of the item's projection
(`primary_spec`); with neither, nothing is written and the bridge leaves one task comment that a card is needed (add the
label and move the task again); canceled on a
received, accepted or reopened item writes `declined`, or `duplicate` when the task carries the label `duplicate` and a
line `Duplicate-Of: <feedback id>`; backlog changes nothing; done is never written (a comment says that done in BB is not
fixed or deployed, which only release evidence sets).

A task's first description line is `Feedback-ID: <uuid>`; the bridge finds a task by that exact line. Do not edit it.
A human edit of the title or description is kept and a comment shows the new summary instead. An item older than
`DEEDKIT_BRIDGE_BACKFILL_DAYS` (default 14) and every item that is already closed gets no task.

## ACK after durable BB save

Owner authority, 2026-11-13: «Да, разрешаю, если так надо, давай делать. Я разрешаю, конечно.» The quoted question's
scope is only the dedicated feedback-bridge subscription, after its events are durably saved in BB, for the bounded
verification and normal operation; an unknown result must not be retried. No other subscription or provider action is
included. Enforcer: `feedback_bridge.py` receipt validation, actual BB task readback and pending-ACK reconciliation.

Every event covered by an ACK must be a valid mapped feedback event on that subscription. There must be no message
items, scope mask, gap or hidden through-position. Each feedback ID needs a non-skipped ledger link AND a successful
current `bb tasks show` matching the linked task ID/key and exact first description line. A skipped/history/closed
ledger row, exhausted unresolved attempts, a malformed event or an empty filtered ID list never authorizes ACK.
Valid items may still be applied while the receipt stays unacknowledged. Backfill filtering itself is unchanged.

Before one ACK attempt, the existing SQLite `meta.pending_ack` records subscription identity/version and before/through
positions for both streams, not the transport receipt. A successful matching response clears it. An unknown response
or crash leaves it across restart. Only authoritative readback of the same subscription/definition with a newer ACK
version and ACK positions covering both saved through-positions clears it without resending. Unchanged, incomplete,
masked, changed-definition or different-subscription readback remains `ack_outcome_unresolved`; no new receipt is ACKed.
A definite API refusal clears that attempt's intent because it proves non-application. Never delete the intent merely
to retry: if positive readback cannot reconcile it, stop for an operator's definitive receipt/transaction investigation.

The historical test `test_an_event_for_an_item_nobody_returns_is_acked_after_three_cycles` retains its Tiden identity;
its assertion now requires no ACK after three cycles under this owner authority. Existing crash/retry test identities
also remain; an unknown transport-entry crash holds, while a committed ACK is reconciled from positions without retry.

### Initial scope before normal activation

Keep the unit, fallback automation and dispatcher OFF until the root's fresh activation decision. This correction does
not authorize the default 14-day metadata export/backfill and does not move its cursor. The completed bounded live
fixture must not be submitted, created or ACKed again.

The smallest initial pass uses only the stored existing subscription and its intended event IDs, targeted durable BB
task reconciliation, and a metadata cursor bounded immediately before the intended submission/change. Require every
returned metadata row and event to belong to that intended set; stop on another row, incomplete page or ambiguous
range. Apply only those intended rows and ACK only after their actual BB task evidence passes the normal ACK code.
An old/closed/orphaned event is held, never silently dropped or ACKed to advance the page.

The current metadata tool has cursor/limit, no ID filter. If an existing receipt cannot be isolated this way, leave the
boundary stopped: either DevOps supplies an exact whitelisted metadata packet for the explicitly intended IDs, or a
separate scoped change adds ID filtering to that existing metadata read under its existing permission. Neither option
needs a new service, credential or broader subscription. The normal broad forward/backfill path is not an initial-scope
substitute; any event-scoped follow-up needs its own explicit root scope before activation.

## Install (nothing in the merge does this)

Prerequisites from DevOps: the maintainer key file (0600) and the gateway URL. Run from a clean checkout of `main`.

```sh
# 1. the tracker and its labels (one time)
bb tasks project create --name DeedKit --prefix DK --link-bb-project proj_nfixav7q2f
bb tasks label create --project DK --name feedback
bb tasks label create --project DK --name duplicate

# 2. the program and the credentials
install -d -m 0700 ~/.local/lib/deedkit-feedback-bridge ~/.config/deedkit
install -m 0755 scripts/feedback_bridge.py ~/.local/lib/deedkit-feedback-bridge/feedback_bridge.py
install -m 0600 deploy/feedback-bridge/feedback-bridge.env.example ~/.config/deedkit/feedback-bridge.env
# edit that file (gateway URL, key file path); DevOps writes the key file, 0600
python3 ~/.local/lib/deedkit-feedback-bridge/feedback_bridge.py check

# 3. the unit (it creates the maintainer subscription on its first start)
install -d ~/.config/systemd/user
install -m 0644 deploy/feedback-bridge/deedkit-feedback-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now deedkit-feedback-bridge
journalctl --user -u deedkit-feedback-bridge -n 50 --no-pager

# 4. the 10-minute fallback
bb automation create --project proj_nfixav7q2f --name deedkit-feedback-bridge-run --cron '*/10 * * * *' --timezone UTC \
  --script-file deploy/feedback-bridge/automation-run.sh --interpreter bash --timeout 5m \
  --env-json '{"PATH":"<HOME>/.local/bin:<HOME>/.local/opt/node-v26.13.1-linux-x64/bin:/usr/local/bin:/usr/bin:/bin"}'
bb automation list --project proj_nfixav7q2f
```

`bb` is a node script: the unit's `PATH` line and the automation's `PATH` must reach `bb` and its node directory (adjust
the node directory in both when it differs). `loginctl show-user $USER -p Linger` should read `Linger=yes` so the unit
survives a logout.

Verify with a synthetic item from a test key (never a live send): within a minute `bb tasks list --project DK --label
feedback` shows one task whose first line is `Feedback-ID: <that item's id>`; moving the task to `todo` makes the feedback
status `accepted` (`list_feedback` of the reporting key).

## Reading a failure

Every error is one line `feedback-bridge: <code>` on stderr (`journalctl --user -u deedkit-feedback-bridge`, or the
automation's run output); no key, path or response body is printed. `gateway_unreachable`, `gateway_http_5xx`,
`tasks_timeout`, `tasks_failed`: transient, the loop backs off from 1 s to 60 s and the next cycle repeats the work (no
create is ever repeated blind: a pending create is reconciled by a complete tracker listing first, and a failed or
truncated listing, one whose `has_more` is not false, creates nothing). A `gateway_http_5xx` line adds the gateway's own
fixed diagnosis in parentheses, for example `gateway_http_503 (backend_status=500)`: the backend's status, its fixed error code
or the transport reason, never a body. `forbidden`: the key lacks a tool or a permission. `item_malformed`: one metadata
row was skipped. `lock_busy`: another cycle held the lock for 5 minutes.

## Undo

```sh
systemctl --user disable --now deedkit-feedback-bridge
rm ~/.config/systemd/user/deedkit-feedback-bridge.service
systemctl --user daemon-reload
bb automation list --project proj_nfixav7q2f
bb automation delete <id> --project proj_nfixav7q2f --yes
```

The tasks stay. Removing `~/.local/state/deedkit-feedback-bridge` forgets the links and the subscription ID: keep the
directory unless the tracker is also reset. The maintainer subscription stays on the server until DevOps closes it; a
start with no stored ID would create a second one.

## Dispatcher (ANYM-991)

`scripts/feedback_dispatch.py` starts a process worker for a feedback task. It reads only the tracker `DK` through
`bb tasks` and starts workers through `bb thread spawn`; it makes no model call and no gateway call, and it is off until
`"dispatch": true` is set. A bb script automation runs one tick every 10 minutes and prints nothing when nothing is due.
The boundary is docs/operations/feedback-autoloop.md, unchanged: its Envelope and Owner items are what the worker brief
enforces, and a principal outside the list is report-only.

A task gets a worker only when all hold: label `feedback` and status backlog or todo; the bridge's header (first line
`Feedback-ID:`, a `Source:` line whose principal is in the config's `principals`, `DeedKit status:` received, accepted or
reopened, a `Triage:` classification `reported-bug` or `feature`); a released summary (not `Metadata only`); no label
`duplicate`, `hold` or `epic`; no key in `hold`; no thread attached to it. Caps: at most 2 live workers, one start per
task, feedback item and cluster (new-card slug, else primary card) ever plus one relaunch of a lost worker, and
`daily_starts` (default 6) per UTC day. A report-only task gets one comment and never a worker.

| Piece | Where |
|---|---|
| Program | `~/.local/lib/deedkit-feedback-bridge/feedback_dispatch.py` (beside `feedback_bridge.py`, which it imports) |
| Config | `~/.config/deedkit/feedback-dispatch.json` (0600; `dispatch` false by default) |
| State | `~/.local/state/deedkit-feedback-dispatch/` (0700): `dispatch.sqlite3` (the start journal), `dispatch.lock`, `PAUSE` |
| Worker brief | `~/deedkit-orchestrator/briefs/feedback-task.md` (a copy of `deploy/feedback-bridge/feedback-task.md`) |
| Automation | `deedkit-feedback-dispatch-run` (`run`, every 10 minutes) |

Every start is journaled before `bb thread spawn`; the thread is titled `feedback-<task key>`, a child of
`orchestrator_thread`, and `bb tasks attach` links it to the task. A spawn of unknown outcome is found again by that title
in the thread list and never repeated; a worker whose thread is archived while its task is not in review gets one relaunch.

### Install and enable (nothing in the merge does this)

Run from a clean checkout of `main`, after the bridge works and the server side is deployed.

```sh
install -m 0755 scripts/feedback_dispatch.py ~/.local/lib/deedkit-feedback-bridge/feedback_dispatch.py
install -m 0644 deploy/feedback-bridge/feedback-task.md ~/deedkit-orchestrator/briefs/feedback-task.md
install -m 0600 deploy/feedback-bridge/feedback-dispatch.json.example ~/.config/deedkit/feedback-dispatch.json
# edit that file: principals (the names the autoloop config lists) and orchestrator_thread; dispatch stays false
python3 ~/.local/lib/deedkit-feedback-bridge/feedback_dispatch.py check
bb automation create --project proj_nfixav7q2f --name deedkit-feedback-dispatch-run --cron '*/10 * * * *' --timezone UTC \
  --script-file deploy/feedback-bridge/dispatch-run.sh --interpreter bash --timeout 5m \
  --env-json '{"PATH":"<HOME>/.local/bin:<HOME>/.local/opt/node-v26.13.1-linux-x64/bin:/usr/local/bin:/usr/bin:/bin"}'
bb automation list --project proj_nfixav7q2f
```

Enable by setting `"dispatch": true` in `feedback-dispatch.json`; the next tick starts workers within the caps. A first
look: `python3 ~/.local/lib/deedkit-feedback-bridge/feedback_dispatch.py status` lists each start (key, attempt, state,
thread).

### Pause, hold and gate

```sh
touch ~/.local/state/deedkit-feedback-dispatch/PAUSE
rm ~/.local/state/deedkit-feedback-dispatch/PAUSE
python3 ~/.local/lib/deedkit-feedback-bridge/feedback_dispatch.py gate DK-12
```

`PAUSE` stops every action of the tick (no start, reconcile or attach) and makes a worker's `gate` exit 1, so a running
worker stops at its next Tiden write, push or merge arm. A key in the config's `hold` list does the same for that task;
the label `hold` on a task only stops a start. Workers run `gate <key>` themselves (the brief says so); it makes no bb
call and prints `go` or `stop <reason>`.

### Reading a failure

One line `feedback-dispatch: <code>` on stderr. `tasks_*` and `threads_*` are bb failures (a listing that cannot finish
starts nothing); `config_invalid`, `file_mode`, `file_unreadable`: the config; `brief_missing`: one of the three brief files
is absent from the briefs directory while a start was due; any other code is the bb CLI's own refusal of a spawn, which
marks that start lost. `status` shows the journal; the exit code 75 means another tick holds the lock.

### Undo

```sh
python3 ~/.local/lib/deedkit-feedback-bridge/feedback_dispatch.py status
bb automation list --project proj_nfixav7q2f
bb automation delete <id> --project proj_nfixav7q2f --yes
```

Set `"dispatch": false` (or `PAUSE`) first. Threads already started stay; the orchestrator archives them. Keep the state
directory: it holds the "one start ever" journal, and removing it lets the same tasks start again.
