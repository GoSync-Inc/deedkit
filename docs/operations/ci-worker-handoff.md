# CI delivery handoff to BB

The repository-owned `scripts/ci_watch.py` (ANYM-996) lets an existing worker end its
model turn while the existing BB **script** scheduler observes delivery. Keeper
continues to own merge decisions. The observer reads GitHub and sends actionable
BB messages; it never merges, enqueues, reruns, cancels, disarms or touches a provider.
The existing required checks, reviews policy and infrastructure-only retry boundary
remain in force. A notification explicitly authorizes no retry or bypass.

## Worker handoff

Use the ordinary draft, local checks, ready/conflict guard and `ship`/Keeper process
first. Register the actual pushed head before ending the turn. Confirm that the common
runtime is enabled, has a future tick, and the exact record below is read back. A saved
mapping without a running schedule is not a handoff. Stop the PR's previous observer
only at an explicit owner handoff after its accepted run has settled; never dual-watch.

Keep the registry in root-owned private thread storage, outside disposable worktrees.
Use the runtime's **actual** registry path from `bb automation show`, not a new per-worker
file that no scheduler reads. A registration file has exactly these fields:

```json
{
  "repository": "GoSync-Inc/deedkit",
  "pr": 123,
  "head": "0000000000000000000000000000000000000000",
  "thread": "thr_worker",
  "project": "proj_project",
  "phase": "source",
  "remaining_action": "Read back imported tests and install the host observer",
  "action_owner": "thr_worker",
  "deadline": "2026-11-14T12:00:00Z"
}
```

Replace all example identities and use a future UTC deadline. `source` observes merge,
covering main CI and the **exact squash's executed sync B**. `merge` stops at merge and
is appropriate only when a separately named automation owns all later delivery. Neither
phase proves deployment. An installation, serving observation, spec/readback or other
real worker obligation must be an explicit `remaining_action`, owned by the registered
thread. If Keeper/CI already completes every registered obligation, declare
`remaining_action: "none"`, `action_owner: "keeper-and-ci"`: green then writes a final
receipt without waking a model. This declaration cannot erase other delivery duties.

```sh
python3 scripts/ci_watch.py register --state /absolute/private/registry.json --mapping /absolute/private/handoff.json
python3 scripts/ci_watch.py status --state /absolute/private/registry.json
```

The exact mapping is idempotent, including while notification delivery is unresolved.
A changed head creates another retained record and supersedes a still-active old head
only for the same owner. A different owner is refused. An explicit coordinated transfer
uses the existing record key, expected old thread, and a mapping changing only the owner
thread/project and its action owner:

```sh
python3 scripts/ci_watch.py transfer --state /absolute/private/registry.json --record RECORD_KEY --from-thread thr_old --mapping /absolute/private/new-owner.json
```

Transfer preserves the old mapping and notification receipt. It refuses unresolved
transport and does not transfer CI, retry or merge authority. Do not edit the registry
by hand to clear an uncertain send or silently follow a new head.

## What a tick proves

A tick holds a nonblocking file lock and visits at most four records, rotating fairly.
Its shared command budget is 90 seconds; individual CLI calls have at most ten seconds.
The scheduler's timeout should exceed that budget (110 seconds). No sleep or polling
loop runs inside the observer. File replacement fsyncs the new file and its directory;
the lock inode is never replaced. Terminal records remain as receipts and stop GitHub
observation. An unavailable/archived owner produces a durable handoff problem, never a
replacement child. `status` exposes records, proofs, notices and owner history.

Required checks come from the current main rules, by name **and app**. The newest check
id must match the exact SHA, CI workflow/check suite, and job in the workflow's latest
attempt. Pending/missing/stale checks remain pending; skipped `CI gate`, ALLSKIPPED or
unreadable provenance never becomes green. The ordinary verify skip counts only with
a successful gate, matching Keeper. Queue membership is read directly; null
`autoMergeRequest` while queued never means dequeue. A failed check, conflict, actual
queue removal, changed head, closed unmerged PR or green PR with no merge owner wakes
once. Pending, queued and green covered by Keeper stay quiet.

After merge, GitHub compare must prove the squash is an ancestor of the selected main
push. A cancelled coalesced push can be covered by the next descendant push; an unrelated
latest push cannot substitute, and a real failed main is actionable. Success needs the
main run's current required checks and actual latest attempt. Sync B must name the exact
squash, use the main sync workflow, and have a successful actual `Tiden sync` job in its
latest attempt with successful main/exact-commit/ancestry/sync steps. A green workflow
whose queue-triggered job was skipped does not count. Guarded B success is bookkeeping
proof; it does not claim that product-wide quality, production or serving is healthy.

A deadline or unreadable evidence yields a bounded action event. Stable event identity
includes PR/head, phase, owner, reason and concrete run/attempt proof. The observer stops
GitHub work for that event and reconciles delivery, then retains `action_required` for
the worker. It does not keep waking on the same failed job. A worker handles the result
under normal source/delivery rules; it must not interpret the callback as retry authority.

## Notification uncertainty

The outbox persists `prepared` body/identity before transport and fsyncs `dispatching`
before invoking native `bb thread tell`. The native command has no idempotency key.
A restart from `prepared` may send once. After `dispatching` or an accepted-but-lost
reply, subsequent ticks read **both** the actual BB queued messages and timeline.
Search is only an index hint: the actual user conversation row must match the complete
body and destination. Tool output or another thread's match cannot acknowledge delivery.

A queued message stays `queued` until actual timeline delivery is found. If neither
source proves the outcome, `needs-reconciliation` remains durable and the runtime
reports `handoff-unresolved`; it never blindly resends or invents another event id.
An unavailable owner and unresolved delivery remain visible in the registry/runtime
error. Resolve using actual timeline/queue evidence or explicit root coordination;
absence from a possibly delayed search index does not prove non-delivery. Never mark
such a record successful, delete the outbox or manually resend the same event.

## Runtime installation and undo

Install only after the source PR's green main and exact B, using the existing BB script
runtime and an explicit handoff from any private observer. Root coordinates activation.
The runtime stores its own script copy, so source edits do not update an installed copy.
Create one paused script automation, read back its id/path/project/registry, then resume
only the agreed registry. Do not change other automations or registrations.

```sh
bb automation create --project PROJECT --name deedkit-ci-worker-handoff --disabled --cron '*/2 * * * *' --timezone UTC --script-file /absolute/green-checkout/scripts/ci_watch.py --host HOST --interpreter python3 --timeout 110s --working-directory /absolute/private --env-json '{"CI_WATCH_STATE":"/absolute/private/registry.json"}'
bb automation show AUTOMATION --project PROJECT --json
bb automation resume AUTOMATION --project PROJECT
```

With no CLI arguments the copied script runs one tick using `CI_WATCH_STATE`. Successful
quiet ticks end with `{"wakeAgent":false}`, recorded by BB as silent skipped ticks.
Read back schedule and record after registration. A bounded labelled fixture using fake
GH/BB processes can test the stored-runtime path without manufacturing CI failures or
waking a provider. The implementing PR records fixture ids/results and native BB
queue/timeline reconciliation proof separately from live delivery evidence.

Undo: pause **this** automation, retain registry/outbox/history, and read back disabled
and no active run. Coordinate a single replacement observer for any outstanding record;
do not cancel accepted workflows or duplicate an uncertain notification. Refresh a green
installed copy with `bb automation update --script-file ... --host ...`, preserving its
project, registry, schedule, interpreter, timeout and working directory.
