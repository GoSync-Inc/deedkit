# Parallel development

Several agent sessions work at once. The rules are in [AGENTS.md](../../AGENTS.md); this
document holds the two facts they need about each other: which files concurrent pull requests
must not share, and how the merge queue behaves.

## Lanes

A lane never has more concurrent pull requests than its limit; where two are allowed, their
files must not overlap. Before starting, compare your planned paths with the changed paths of
open pull requests (`gh pr list`, `gh pr diff <n> --name-only`) and the files your brief marks
busy. A task that needs two lanes holds both; prefer splitting it.

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
  squash only, `ALLGREEN`, build concurrency 3, one merge at a time, `CI gate` and
  `checks / verify` required, no bypass. A pull request need not be up to date with `main`: the
  queue tests it against the latest `main` plus the pull requests ahead of it, on `merge_group`.
- When the group commit has exactly the tree a passing full pull request run tested, the queue
  run reuses that verification and takes a minute or two (ANYM-358). A queue run publishes
  nothing to Tiden; the `main` push run publishes (TIDEN-CI.md).
- Enqueue with `gh pr merge <n> --squash --auto`. The merge is asynchronous: confirm it landed,
  not that the command returned. A failed group check drops the pull request; fix it and enqueue
  again. Update a branch only for a real conflict, never just to stay current. A conflict in
  another task's files goes to the orchestrator.
- `main` push runs are never cancelled (`ci.yml` concurrency), so every merged SHA keeps a
  complete result; production promotes only a SHA whose own push run succeeded
  ([production.md](production.md)).

## Sessions

- Every session has its own worktree and branch; the stash stack is shared across worktrees, so
  never use bare `git stash`.
- Tiden commands use `--format json` with a `jq` projection; files over about 20k characters are
  read by range or `grep`. Whole exports never go into a session or a reviewer packet.
- Models for code: Opus on risk paths (review-protocol.md, Codex budget); Sonnet, as a trial, on
  small and medium cards (Effort up to 3, one lane).
- Restart. A worker near 200k tokens (the orchestrator near 250k), at the next natural boundary,
  writes a handoff of at most 40 lines (`handoffs/TEMPLATE.md` in the orchestrator's directory)
  that lists the files and ranges already read and the decisions taken, waits for its own
  background jobs (closing the terminal kills them), then restarts itself with the orchestrator's
  `worker-restart.sh` (handoff, your current model and effort, brief), sends the orchestrator `RESTARTED <path>` and
  stops. Not with about 30 calls left (only merge, `main` push run, read-back, DONE): finish.
  Restart early, from 150k, before an expected idle of over an hour (owner gate, usage limit,
  overnight).
- Background jobs (a Codex review, a backup, a CI or merge wait): one check about 30 s after the
  start that the job runs (the log shows progress, no usage or trust error); a `timeout` on every
  job (a merge wait: 60 minutes; on its timeout run `gh pr merge --disable-auto` or read the state
  once with `gh pr view` before reporting); a wait that ends on failure too (a failed required check, a
  dequeue, a closed pull request), because `gh pr merge --auto` stays armed on a failed one; then
  read the exit status and the report. A missing report is a failure, not "still running". That
  is one check, never polling.
- Owner questions (AGENTS.md section 2 items and feature or epic acceptance only): while the owner
  is online, one message as they arise; while the owner is away, one numbered list on waking and
  before sleep, each item with a recommendation. Work that needs no answer keeps running.
