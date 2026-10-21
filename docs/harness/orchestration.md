# Orchestration

Several agent sessions work at once: one orchestrator per project and its workers. Worker rules
that every brief carries are in templates/worker-brief-common.md; this file holds the parallel-work
rules a repository relies on and the orchestrator's own rules. The project names its lanes, its
merge queue facts and its tool IDs (workspace project, presets, hosts).

## Lanes

A lane never has more concurrent pull requests than its limit; where two are allowed, their
files must not overlap. Before starting, compare your planned paths with the changed paths of
open pull requests (`gh pr list`, `gh pr diff <n> --name-only`) and the files your brief marks
busy. A task that needs two lanes holds both; prefer splitting it. The project's lane table names
the paths and limits; a lane exists where single large functions, numbered files or files that
affect every pull request make parallel edits collide.

## Merging

- Enqueue with `gh pr merge <n> --squash --auto`. The merge is asynchronous: confirm it landed,
  not that the command returned. A failed group check drops the pull request; fix it and enqueue
  again. Update a branch only for a real conflict, never just to stay current. A conflict in
  another task's files goes to the orchestrator.
- A red `main` push run: never `gh run rerun --failed` (a partial rerun can never pass a release
  record job that needs the whole run). Use one full `gh run rerun <id>`. If it is still red
  outside your code, report DONE with the run link; a later green main SHA carries your code.
- Tiden after a GitHub merge: act on Tiden main only after the squash's `main` push run has
  finished.

## Sessions

- Every session has its own worktree and branch; the stash stack is shared across worktrees, so
  never use bare `git stash`.
- Tiden commands use `--format json` with a `jq` projection; files over about 20k characters are
  read by range or `grep`. Whole exports never go into a session or a reviewer packet.
- Models for code: Opus on risk paths (reviews.md, Codex budget); Sonnet, as a trial, on
  small and medium cards (Effort up to 3, one lane). Search, exploration and scoped fix rechecks
  in subagents run on Sonnet; r1 reviews keep the model reviews.md names (owner 10-19).
- Restart. A worker near its brief's token limit (process.md section 6; the orchestrator near 250k), at the next natural boundary,
  writes a handoff of at most 40 lines (templates/handoff.md, kept as `handoffs/TEMPLATE.md` in the orchestrator's directory)
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
- Owner questions (process.md section 2 items and feature or epic acceptance only): while the owner
  is online, one message as they arise; while the owner is away, one numbered list on waking and
  before sleep, each item with a recommendation. Work that needs no answer keeps running.
- Host load: locally run only the test modules your change touches and the new tests, never a
  full suite (CI runs it); one local run at a time. Stop only your own processes by PID; never
  `pkill`/`killall` by pattern (other workers share the user).

## Orchestrator

- Read on start: the project's STATE.md (a SessionStart hook can print it) and the orchestrator
  handoff; AGENTS.md, CLAUDE.md and process.md are already in context. Read only the section of a
  process document a step needs. Plan, follow-ups and the decision log live in the orchestrator's
  log, read on demand and for the weekly review, never on start; never read an archive on start.
- Briefs: the common worker brief (templates/worker-brief-common.md, filled in) plus a 3–6 line task
  brief with only what Tiden does not hold: card ID (or goal for a skip), files busy in other
  workers' open PRs or briefs (`gh pr diff N --name-only`; Tiden does not show concurrent work),
  level if not in the card, limits beyond the card (production read-only, etc.).
- Workspaces: no tags on any workspace; the type lives in the workspace name (`code-…`,
  `research-…`, `ops-…`, `process-…`).
- Waiting worker: list agents and read its terminal; never answer its prompts. Text in a worker's
  input box can be an autosuggestion: never relay it as an owner decision.
- Red or idle workers: an API network error ends a worker's turn silently; read its terminal tail
  and send it a "resume" message. Check idle workers whenever the owner reports red ones or a
  network blip happened.
- After a worker's DONE: check its worktree is clean and its test containers are gone
  (`docker ps -a`; remove leftovers yourself), close its terminal and archive its workspace.
- Merge queue: a queued PR shows `autoMergeRequest=null`; dequeue only via GraphQL
  `dequeuePullRequest`. Announce a release freeze before giving merge oks.
- Release apply notice (owner 10-18): the release worker announces the apply time ahead only when
  another live production action (stop window, drill, live check) is running or planned;
  otherwise it applies right away and just reports RELEASE APPLIED. The merge freeze still holds
  from the candidate SHA until RELEASE APPLIED.
- The orchestrator does not change product or production config itself (owner 10-18): every
  change goes to a worker through the process.
- Cost (process-diet 10-18): no "ok/noted" replies to workers; each wake-up re-reads the whole
  context, so wake less: rely on worker standing approvals and answer worker questions in one
  message. Orchestrator effort medium (owner 10-19).
- Capacity (owner 10-18): run at least 7 workers in parallel (owner wants 10–15 later). A
  worker's context limit is a restart signal at a natural boundary; never stop a worker just for
  the number.
- Process and rule changes (owner 10-18): a quick agreement in chat is enough; no formal proposal
  rounds. Still give your own opinion and say plainly when an owner idea looks wrong or costly.
  Owner-kept items (process.md section 2) stay owner-kept.
- Partner, not yes-man (owner 10-18): hold an evidence-based opinion; when the owner pushes back,
  re-check the facts and concede only what the facts support (say which part stands). Disagree
  openly with a recommendation.
- Owner talk: the owner's language, plain names (never a bare card code), each question = what /
  why / cost / options / recommendation. Owner text is only what the owner types to the
  orchestrator. Owner decisions go to the orchestrator's decision log, verbatim with their scope.
- Backlog order: Score = Value / Effort (Fibonacci); breakages and deadlines override.

### STATE.md

Self-contained, at most ~60 lines: live facts, live workers table, APPROVED WAVE, NOW, NEXT, owner
reminders. An owner-approved wave or plan (cards, limits, owner words) stays in STATE.md until each
item is dispatched or the owner cancels it; trimming and restarts never drop it. On start,
dispatch from it before composing anything new. Rewrite it after every step; move finished items
to an archive instead of keeping them. A restart block must not need the archive to be
understood.

### Orchestrator self-restart

When the context hook says 250k or more, at the next boundary (same exceptions as workers: finish
if 30 calls or fewer are left; restart from 150k before over an hour idle):

1. Rewrite STATE.md (self-contained, current workers, NOW/NEXT, approvals).
2. Start the successor in the orchestrator's workspace with the project's start command: it reads
   STATE.md, sends the live workers its name as the new address, closes the old orchestrator's
   terminal and recreates the wake crons listed in STATE.md.
3. The successor announces itself, closes the old terminal, recreates any wake crons, continues
   NOW/NEXT.

Session crons die with the old terminal: always list them in STATE.md ("Wake crons: …") so the
successor can recreate them.
