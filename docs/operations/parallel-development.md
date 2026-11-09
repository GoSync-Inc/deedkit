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

1. **Approval on the full head SHA, by an account with write access**: its effective repository permission
   (`collaborators/<login>/permission`) is write, maintain or admin. An organization member with read or triage
   access does not count. Either an APPROVE review on the head commit, or a comment
   `Diff read: <orchestrator session> <40-hex head sha>` (harness 0.80.0; the older
   `Review: orchestrator diff read <40-hex head sha>` also counts) that was posted after the head arrived and was
   never edited. An approval line in the description does not count: its author can edit it after a push.
   A no-review pull request (owner review cut 11-09, or a small fix under AGENTS.md section 3) instead carries
   the description line `Review record: none (...)`. It needs no approval when the head's diff is clean under the
   change-path signal grep (review-protocol.md: both lines, over the files API patches; a file without a
   readable patch counts as a hit). A hit holds the pull request with one comment: a risk path needs its review
   and an approval on the head. The grep is the guard here, because the line is author-written. When the approval names an older
   SHA, the keeper does not enqueue and comments once. The trust boundary is write access, which can already arm
   `gh pr merge --auto` directly, so the keeper adds no authority.
2. **Green with no required job skipped.** Each required check of the live `main` rules, matched by name and app,
   has its newest run on the head completed. `CI gate` must be `success`. The other checks may be `skipped` only
   because `CI gate` validates that skip (docs-only, reuse). All-skipped never counts. A draft's run counts:
   `ci.yml` runs full CI on drafts, and `ready_for_review` starts no new run.
3. **Migration numbers.** New files in `services/messaging/messaging_service/migrations/` must take the next
   free numbers on `main`. Otherwise the keeper comments the exact `git mv` lines and the schema pins to change,
   and does not enqueue. It never renumbers: the pins include comments, table-set names and test expectations
   (ANYM-848's renumber changed 15 files).
4. **Dequeues.** A merge-queue removal for failed checks on the current head gets one comment with the
   failing jobs, and the keeper does not re-enqueue it. The one flake retry stays with the worker's
   `wait-merge.sh`, so the queue has a single retry actor. The keeper can retry once (`RETRY_FAILED_CHECKS` in
   the script, 3 minutes after the removal). It stays off until `wait-merge.sh` skips its re-enqueue for `ship`
   pull requests, because two actors with separate retry counts could start a third attempt (Sol r3 on #790).
   A second failure on a head gets one comment, and the keeper stops. A queue `merge_conflict` waits until main
   moves past the entry ahead and is retried once; `wait-merge.sh` never retries conflicts. A second conflict
   gets a comment, and the keeper stops. A removal by hand holds the pull request until a new head arrives or
   the `ship` label is added again. A conflict with main gets one comment. The counts come from the pull
   request's timeline.

A pull request that is queued or has auto-merge armed is left to GitHub. Each comment carries an HTML marker
`<!-- merge-keeper:<kind>:<head> -->` and is posted once per head; markers count only from a write-access account
(effective permission) or `github-actions[bot]`. The enqueue passes `expectedHeadOid`, so a
push racing the keeper is never enqueued.

**Where it runs.** The keeper runs every 300 s on the Mac mini as the LaunchDaemon
`com.egoregerev.deedkit-merge-keeper` (`deploy/merge-keeper/`). It runs as the admin account with that account's
existing `gh` login and from its own clone, which it moves to `origin/main` before each tick
(`scripts/merge_keeper_mini.sh`). The keeper does not use GitHub Actions to act: an enqueue with the workflow
`GITHUB_TOKEN` starts no `merge_group` run, because GitHub starts no workflow from that token's events. The
entry would then hold the queue head until the 60-min check timeout. `.github/workflows/merge-keeper.yml` runs
the same script in report mode (`--dry-run`) with read-only permissions every 5 minutes and on review
submissions, and writes the decisions to the job summary. `pull_request_review` runs the workflow file of the
pull request's merge ref. That is safe only while the job has read permissions and no credential. Moving the
keeper to Actions later needs three changes: a token whose events start workflows (a GitHub App token as
`GH_TOKEN`), dropping `--dry-run`, and dropping the `pull_request_review` trigger. The script does not change.
The hourly watchdog has its own concurrency group, so a pending report never replaces it.

**Heartbeat.** Each mini tick writes `~/.local/state/deedkit-merge-keeper/heartbeat.json` on the mini and the
repository Actions variable `MERGE_KEEPER_HEARTBEAT` (`{"at","ok_at","host","prs","errors","enqueued"}`). The
hourly workflow run fails when `ok_at` is older than 20 minutes. Run `python3 scripts/merge_keeper.py
heartbeat-check` from any checkout to get the same answer (exit 1 when stale). The tick log is
`~/Library/Logs/deedkit-merge-keeper.log` on the mini.

**Workers.** Add the label (`gh pr edit <n> --add-label ship`) once the review passed on the head. The
orchestrator records its diff read as a comment `Diff read: <session> <full head SHA>`. Arming `gh pr merge <n> --squash --auto`
yourself still works, and the keeper then leaves the pull request alone. A push after approval needs a new
approval comment or review.

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
