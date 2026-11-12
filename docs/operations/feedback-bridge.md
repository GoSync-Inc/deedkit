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
`feedback-maintainer-write`) when that card ships. Until the tool exists or is granted the reverse write stays idle:
the bridge prints `reverse_unavailable_<code>` to stderr and exits 0.

## Mapping

| DeedKit status | Task status |
|---|---|
| received | backlog |
| accepted | todo (a human's in_progress or in_review stays) |
| fixed, verify_requested | in_review |
| verified | done |
| declined, duplicate | canceled |
| reopened | backlog |

Task side to DeedKit: todo, in_progress or in_review on a received or reopened item writes `accepted`; canceled on a
received, accepted or reopened item writes `declined`, or `duplicate` when the task carries the label `duplicate` and a
line `Duplicate-Of: <feedback id>`; backlog changes nothing; done is never written (a comment says that done in BB is not
fixed or deployed, which only release evidence sets).

A task's first description line is `Feedback-ID: <uuid>`; the bridge finds a task by that exact line. Do not edit it.
A human edit of the title or description is kept and a comment shows the new summary instead. An item older than
`DEEDKIT_BRIDGE_BACKFILL_DAYS` (default 14) and every item that is already closed gets no task.

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
truncated listing creates nothing). `forbidden`: the key lacks a tool or a permission. `item_malformed`: one metadata
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
