# Releasing accepted BB helper threads (DK-41, ANYM-991)

`scripts/bb_worker_cleanup.py` archives and then stops an orchestrator's own helper threads once the orchestrator
has accepted their work. One invocation is one bounded pass (at most 10 releases, `--max-actions`); it is not a
daemon and makes no model call. It uses only `bb thread list/show/archive/stop`, `bb thread queue list`,
`bb thread interactions list`, `bb terminal list`, `bb tasks show` and local `git status`/`git for-each-ref`.
It never deletes a thread, branch, worktree, artifact or task, and never changes a task's status.

## Authority

Only the owner's acceptance manifest selects a thread. Idle state, a published source, a child's own
`SOURCE_DONE` report or its task status alone never do. Root adds an `accepted` entry after it has consumed the
child's actual deliverable; the product obligation of that task stays with root.

Default path `~/.local/state/deedkit/bb-worker-cleanup/<owner>/acceptance.json` (`--manifest` overrides). The file
must be a regular file, not a symlink, owned by the running user and not group- or world-writable; the directory is
0700. Any other manifest, or one naming another owner or project, stops the whole pass with exit 1.

```json
{
  "schema": "deedkit.bb-worker-acceptance/v1",
  "owner": "thr_h748zsv2mq",
  "project": "proj_nfixav7q2f",
  "entries": [
    {"thread": "thr_xxxxxxxxxx", "authority": "accepted", "receipt": "path, PR or message that root consumed",
     "accepted_at": "2026-11-16T07:25:49Z", "resources_closed": true, "note": "optional"},
    {"thread": "thr_yyyyyyyyyy", "authority": "task_done", "task": "DK-12", "receipt": "root's arming note",
     "accepted_at": "2026-11-16T07:25:49Z", "resources_closed": true}
  ]
}
```

- `receipt` must be non-empty, `accepted_at` an ISO 8601 time with a zone, `resources_closed` the JSON value
  `true`; otherwise the entry is ignored. A thread listed twice, the owner itself and an unknown `authority` are
  ignored too.
- `resources_closed: true` is root's attestation that the child's own jobs ended (its provider shell jobs, test
  runs, watchers). BB's `idle`, queue, `activeBackgroundAgentCount` and terminal fields do not show a provider's
  native shell jobs: a worker reported "tests still running" while reading idle with all of them at 0. The script
  cannot prove those jobs closed, does not look for host processes, and relies on this attestation.
- Any thread activity after `accepted_at` (`updatedAt` or `latestAttentionAt` later than it) voids the entry:
  the thread is skipped as `activity_after_acceptance` until root accepts again with a newer time.
- `task_done` is a root trust decision for one named thread: it releases only when the pass runs with
  `--allow-task-done` (off by default and not in the installed command) and `bb tasks show <task>` reads `done`
  with the thread in `taskThreads`. Nothing is discovered from the tracker.

## What must hold, read fresh twice right before the archive

The thread is in `--project`, its `parentThreadId` and `lifecycleOwnerThreadId` are `--owner`, it is not the owner,
`originKind`/`originPluginId` are null, it is visible, not archived or deleted. `status` and `runtime.displayStatus`
are `idle`; `queuedMessageCount`, `activeBackgroundAgentCount` and every `activity` count are 0, `queuedWork` is
`none`, `hasPendingInteraction` is false; the queue and interaction lists are empty; `pendingTodos` is empty; every
terminal session has exited; it has no unarchived child. Its worktree exists, `git status --porcelain
--untracked-files=all` is empty and HEAD is on a `refs/remotes` ref. Anything unreadable, refused or changed between
the two reads skips the thread. Before the stop the archived thread is read again: still idle, empty queue, no child.

Threads in the manifest that are already archived (for example the seven root released by hand on 2026-11-16,
receipt `thread-storage/thr_h748zsv2mq/opus-wave-20261116/immediate-cleanup.json`) are reported
`already_archived`; their worktree is not opened and they are not stopped or restarted.

## Journal and unknown outcomes

`journal.jsonl` (0600) gets an fsynced `intent` line before each archive or stop, then `done`, `refused` or
`unknown` from a readback. `refused` (BB answered with an error) may be tried by a later pass. `intent` left by a
crash is settled by readback on the next pass (archived: `done`; otherwise `unknown`). An `unknown` thread is never
sent the call again; the pass prints `skip unknown_needs_root`. After checking the thread in BB, root records its
decision:

```sh
python3 scripts/bb_worker_cleanup.py --project proj_nfixav7q2f --owner thr_h748zsv2mq --clear-unknown thr_xxxxxxxxxx
```

`last-run.json` holds the latest pass's per-thread outcome. Exit codes: 0 done, 1 error, 64 usage, 75 another pass
holds the lock.

## Commands

```sh
python3 scripts/bb_worker_cleanup.py --project proj_nfixav7q2f --owner thr_h748zsv2mq           # dry run
python3 scripts/bb_worker_cleanup.py --project proj_nfixav7q2f --owner thr_h748zsv2mq --apply   # one pass
python3 scripts/bb_worker_cleanup.py --project proj_nfixav7q2f --owner thr_h748zsv2mq --print-install
```

`--print-install` prints the supported BB script automation, run from a checkout of green `main`:

```sh
bb automation create --project proj_nfixav7q2f --name 'DeedKit BB worker cleanup' --cron '*/10 * * * *' \
  --timezone UTC --script-file <absolute path>/scripts/bb_worker_cleanup.py --interpreter python3 \
  --timeout 300000 \
  --env-json '{"DEEDKIT_BB_CLEANUP_MODE": "apply", "DEEDKIT_BB_CLEANUP_OWNER": "thr_h748zsv2mq", "DEEDKIT_BB_CLEANUP_PROJECT": "proj_nfixav7q2f"}' \
  --working-directory automation-storage
```

The automation copies the script, so a later change ships by `bb automation update <id> --script-file ...`.
`DEEDKIT_BB_CLEANUP_STATE_DIR` overrides the state directory. Run the dry run once by hand before creating it.

## Undo

- One thread: unarchive it in BB; its history, branch and worktree are untouched, and a stopped runtime starts on
  the next message. Remove its manifest entry so no later pass selects it again (an unarchived thread whose archive
  is journaled `done` is skipped as `changed_after_archive` either way).
- The automation: `bb automation pause <id>` or `bb automation delete <id>`; the state directory can stay.
