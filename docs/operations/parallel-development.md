# Parallel development

Several agent sessions work at once. The rules (lanes, merging, sessions: restart, background
jobs, owner questions) are harness `orchestration.md`; this document holds DeedKit's two facts
they need: which files concurrent pull requests must not share, and how the merge queue behaves.

## Lanes

| Lane | Paths | Concurrent PRs |
| --- | --- | --- |
| `schema` | `services/messaging/messaging_service/migrations/`, any `schema.sql` or `*_role.sql` | 1 |
| `messaging-mcp` | `services/messaging/messaging_service/app.py` | 1 |
| `messaging-store` | `services/messaging/messaging_service/store.py` | 1 |
| `telegram-ops` | `services/messaging/messaging_service/telegram_operations.py`, `telegram_collector.py` | 1 |
| `whatsapp-native` | `whatsapp/whatsmeow/` | 1 |
| `ci` | `.github/workflows/`, Dockerfiles, lock files, scripts used only by CI | 1 |
| `release-host` | `deploy/` (including systemd units and timers), `scripts/production.py`, `scripts/seamless_release.py`, `services/release-router/` | 1 |
| `process-docs` | `AGENTS.md`, `CLAUDE.md`, `.claude/`, `TIDEN-CI.md`, `BACKLOG.md`, `docs/operations/` | 2, disjoint files |
| module lanes | `services/calendar/`, `services/calls/`, `services/gateway/`, `packages/`, `whatsapp/` browser collector | 2 each, disjoint files |

The first five lanes exist because single large functions or numbered files make parallel edits
collide: `create_app` holds every messaging MCP tool, provider operations live in one dispatcher
per provider, and migration numbers are sequential with immutable checksums. `ci` and
`release-host` are separate because CI files affect every pull request, while
`scripts/production.py` is one large file.

## Merging

- `main` is protected by the ruleset `deploy/main-ruleset.json` (live as ruleset 25274451):
  squash only, `ALLGREEN`, build concurrency 6, one merge at a time, `CI gate` and
  `checks / verify` required, no bypass. A pull request need not be up to date with `main`: the
  queue tests it against the latest `main` plus the pull requests ahead of it, on `merge_group`.
- When the group commit has exactly the tree a passing full pull request run tested, the queue
  run reuses that verification and takes a minute or two (ANYM-358). A queue run publishes
  nothing to Tiden; the `main` push run publishes (TIDEN-CI.md).
- `main` push runs are never cancelled (`ci.yml` concurrency), so every merged SHA keeps a
  complete result; production promotes only a SHA whose own push run succeeded
  ([production.md](production.md)).

## Merge keeper

The merge keeper (`scripts/merge_keeper.py`) enqueues reviewed, green pull requests, so nothing green waits for a
person or the morning (lead-time study 11-09: 11 green, reviewed pull requests waited 8.6-32 h to be enqueued).
It acts only on an open, non-draft, same-repository pull request into `main` that carries the label `ship`:

1. **Approval on the head.** An APPROVE review by an owner, member or collaborator on the head SHA, or a
   description line `Review: orchestrator diff read <sha>` naming the head. When the approval names an older SHA,
   the keeper does not enqueue and comments once.
2. **Green with no required job skipped.** Each required check of the live `main` rules has its latest run on the
   head completed after the last ready-for-review event. `CI gate` must be `success`. The other checks may be
   `skipped` only because `CI gate` validates that skip (docs-only, reuse). All-skipped never counts.
3. **Migration numbers.** New files in `services/messaging/messaging_service/migrations/` must take the next
   free numbers on `main`. Otherwise the keeper comments the exact `git mv` lines and the schema pins to change,
   and does not enqueue. It never renumbers: the pins include comments, table-set names and test expectations
   (ANYM-848's renumber changed 15 files).
4. **Dequeues.** A merge-queue removal for failed checks on the current head is retried once: an enqueue again
   reruns the whole group, which a rerun of a dropped group's jobs cannot. A second failure gets one comment
   with the failing jobs, and the keeper stops for that head. A removal by hand holds the pull request until a
   new head arrives or the `ship` label is added again. A conflict gets one comment.

A pull request that is queued or has auto-merge armed is left to GitHub. Each comment carries an HTML marker
`<!-- merge-keeper:<kind>:<head> -->` and is posted once per head. The enqueue passes `expectedHeadOid`, so a
push racing the keeper is never enqueued.

**Where it runs.** The keeper runs every 300 s on the Mac mini as the LaunchDaemon
`com.egoregerev.deedkit-merge-keeper` (`deploy/merge-keeper/`). It runs as the admin account with that account's
existing `gh` login and from its own clone, which it moves to `origin/main` before each tick
(`scripts/merge_keeper_mini.sh`). The keeper does not use GitHub Actions to act: an enqueue with the workflow
`GITHUB_TOKEN` starts no `merge_group` run, because GitHub starts no workflow from that token's events. The
entry would then hold the queue head until the 60-min check timeout. `.github/workflows/merge-keeper.yml` runs
the same script in report mode (`--dry-run`) with read-only permissions every 5 minutes and on review
submissions, and writes the decisions to the job summary. Moving the keeper to Actions later needs only a token
whose events start workflows (a GitHub App token as `GH_TOKEN`) and dropping `--dry-run`. The script does not
change.

**Heartbeat.** Each mini tick writes `~/.local/state/deedkit-merge-keeper/heartbeat.json` on the mini and the
repository Actions variable `MERGE_KEEPER_HEARTBEAT` (`{"at","ok_at","host","prs","errors","enqueued"}`). The
hourly workflow run fails when `ok_at` is older than 20 minutes. Run `python3 scripts/merge_keeper.py
heartbeat-check` from any checkout to get the same answer (exit 1 when stale). The tick log is
`~/Library/Logs/deedkit-merge-keeper.log` on the mini.

**Workers.** Add the label (`gh pr edit <n> --add-label ship`) once the review passed on the head. Either way,
arming `gh pr merge <n> --squash --auto` yourself still works, and the keeper then leaves the pull request
alone. A push after approval needs a new approval line or review.

**Install on the mini** (once, as the admin account):

```sh
git clone -q https://github.com/GoSync-Inc/deedkit.git ~/.local/share/deedkit-merge-keeper/repo
sudo -n cp ~/.local/share/deedkit-merge-keeper/repo/deploy/merge-keeper/com.egoregerev.deedkit-merge-keeper.plist /Library/LaunchDaemons/
sudo -n launchctl bootstrap system /Library/LaunchDaemons/com.egoregerev.deedkit-merge-keeper.plist
```

**Undo.** Stop the actor with `sudo -n launchctl bootout system/com.egoregerev.deedkit-merge-keeper`, then
`sudo -n rm /Library/LaunchDaemons/com.egoregerev.deedkit-merge-keeper.plist`. Stop the report and the
watchdog with `gh workflow disable merge-keeper.yml`. Delete the heartbeat with `gh variable delete
MERGE_KEEPER_HEARTBEAT`. Removing the `ship` label from a pull request takes it out of the keeper's hands.
