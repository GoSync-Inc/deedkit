# Parallel development and releases

This document covers how several agent sessions work at once and how their work
reaches production. Tiden usage is defined in
[tiden-product-workflow.md](tiden-product-workflow.md), promotion in
[production.md](production.md), review levels, reviews and merge authority
under Review levels in [AGENTS.md](../../AGENTS.md), and the review procedure in
[review-protocol.md](review-protocol.md). Do not restate those rules here.

## Roles

- **Owner** sets priorities, answers the questions escalated to it, decides what
  it keeps under Review levels in `AGENTS.md` (among them production apply and
  every live provider action) and receives the summary of every owner-level pull
  request an agent merged. Nothing below delegates that authority implicitly.
- **Orchestrator** (one at a time) selects ready stories, checks lanes, dispatches
  worker sessions and audits their merge reports. It writes no product code.
- **Worker session** (Claude Code) implements exactly one story in its own
  worktree, branch, pull request and Tiden intent session or recorded skip.
- **Codex** reviews as Astra and gives a second opinion when planning;
  development goes to Claude Code workers.
- **Release owner** (one at a time) prepares and applies promotions of `main`.

## Unit of work

A story is **ready** when its Tiden card was accepted through the card intake in
the Tiden workflow and has the template's fields, including its Score, **lane**
and expected paths. A story that is not ready goes back to card intake (the owner for feature
and improvement). A
change that takes `tiden intent skip` without a story (agent rules, process
documents, wording) has a brief that names the goal, expected paths, acceptance
and checks instead.

One story maps to one Git branch, one pull request and one intent session, or a
recorded skip for a normal-level story that changes no acceptance criterion
(`AGENTS.md`). Split
larger work into stories that merge independently. A pull request should merge
within about a day; the older a branch, the more it conflicts.

**Claim.** A worker opens a draft pull request with its first push; a branch push
alone runs no CI, the pull request run verifies it. Its description names the
story (or the goal for a skip), the lane and the review level (`Lane: <lane>`,
`Review level: <level> — <reason>`) and carries the `Tiden intent branch:` line or
`Tiden: skip — <reason>`. The draft pull request is the claim. Before dispatching,
the orchestrator lists open pull requests and open intent branches; an item with a
claim is taken. A card named on the `Spec sync:` line of a pull request merged
since the last completed release spec sync is delivered and waits for that sync; it is not
dispatched again. When a claimed pull request has no activity for two days, the
orchestrator hands it to a new worker on the same branch and pull request and
tells the owner. Git allows one worktree per branch, so the orchestrator first
saves uncommitted work in the old worktree (a pushed work-in-progress commit or a
patch) and archives that workspace. The new worker continues a closed intent
session with `tiden intent continue --pr <n>`; a recorded skip stays a skip, and
anything else goes back to the orchestrator. A pull request is closed only when
its task is cancelled.

## Lanes

A lane never has more concurrent pull requests than its limit in the table; where
two are allowed, their files must not overlap. The lane is recorded in the story
and in the pull request description. Before dispatch the orchestrator compares the
planned paths with the expected paths in active workers' stories (for a skip,
its brief) and with the changed paths of open pull requests (`gh pr diff <n> --name-only`).

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

The first five lanes exist because single large functions or numbered files make
parallel edits collide: `create_app` holds every messaging MCP tool, provider
operations live in one dispatcher per provider, and migration numbers are
sequential with immutable checksums. Splitting those functions is itself a
`messaging-mcp` / provider-lane story and would raise the limits afterwards.
`ci` and `release-host` are separate because CI files affect every pull request,
while `scripts/production.py` is one large file.

A story that needs two lanes holds both, and several lanes make its review level
at least important (`AGENTS.md`). Prefer splitting it.

## Merging

- **Merge queue.** `main` is protected by the ruleset `deploy/main-ruleset.json`
  (live as ruleset 25274451): squash, `ALLGREEN`, build concurrency 3, one merge at
  a time, and `CI gate` plus `checks / verify` required. A pull request does not
  need to be up to date with `main`. The queue tests it against the latest `main`
  plus the pull requests ahead of it, and CI runs the required checks on
  `merge_group`; a queue run publishes nothing to Tiden
  ([TIDEN-CI.md](../../TIDEN-CI.md)).
- Enqueue with `gh pr merge <n> --squash --auto` (repository auto-merge is on).
  The merge is asynchronous: confirm it landed, not that the command returned. A
  failed group check drops the pull request from the queue; fix it and enqueue
  again. Update a branch only for a real conflict or when a review asks for it,
  never just to stay current. A worker closes the intent session on its own commit
  range before it merges ([Claude Code guide](claude-code-tiden.md)); a conflict
  in another task's files goes back to the orchestrator.
- Merge method: **squash** only; merge commits and rebase merges are disabled in
  the repository settings (2026-10-16). One story becomes one commit on `main`.
- Who merges follows the review level in `AGENTS.md`. A worker merges without an
  "ok" from the orchestrator, except during a merge freeze (Releases).
  Enqueueing a pull request is merging it.
  Never bypass required checks or use administrator merges.
- Merging the Git pull request never merges its Tiden branch. Merge both in the
  same step, under the same review level and conditions: check the Tiden merge
  preview before merging and read back Tiden main after it, so nothing of the
  change is left unmerged.

## Schema compatibility

- **Expand, then contract.** A migration must work with both the currently deployed
  code and the new code. Dropping, renaming or narrowing happens in a later release,
  after no deployed code uses the old shape. Blue-green handoff and image rollback
  depend on this.
- Never edit a merged migration. Reserve the next migration number in the pull
  request title while the `schema` lane is held.
- A schema migration is owner-level (`AGENTS.md`). Its pull request states its
  backup prerequisite, and its release waits for the backup gate in
  [production.md](production.md).

## Releases

- Merging and deploying are independent: without automatic deployment a merge
  never implies a deploy. A release in flight holds merges from prepare to apply,
  because apply checks again that its SHA is the current `main`
  ([production.md](production.md)). The release owner tells the orchestrator
  before prepare and after apply, and the orchestrator announces a merge freeze
  to the workers in between, during which no worker enqueues; it lets already
  enqueued pull requests merge or dequeues them before the release SHA is chosen.
- The release owner promotes the exact current `main` SHA by
  [production.md](production.md), after that SHA's own push run succeeds; never
  an earlier SHA. The readiness decision in the Tiden workflow is recorded for
  that SHA first, and live-session authority exists where the changed service
  requires it.
- `main` push runs are never cancelled (`ci.yml` concurrency, since PR #60), so
  every merged SHA keeps a complete result. Promote only a SHA whose own push run
  succeeded.
- The merge queue run is the authoritative test run of a merged SHA. Its `main`
  push run validates that run's evidence and publishes it to Tiden instead of
  running the test suite again, and still builds and publishes the images and
  records the release; without a valid queue run it verifies in full
  ([TIDEN-CI.md](../../TIDEN-CI.md), ANYM-322). `production.py verify` accepts the
  queue's verification only in place of `checks / verify` (ANYM-321).
- Before prepare or a package publication, the release owner runs the release
  review ([review-protocol.md](review-protocol.md)) or records why none is due;
  its open blocking or major findings hold the release as that document
  describes.
- The release owner keeps one release in flight, from prepare to acceptance. The
  host deployment lock only stops two deployment commands from running at once.
- Release often; the target is several times a day. Once automatic deployment
  exists, release after every merge whose checks and production.md gates pass;
  until then, as often as the rollout allows. Each release keeps production.md's
  receipts and the readiness decision for its SHA.

## Orchestrator loop

1. Read Tiden main and open proposal branches; list ready stories.
2. List open pull requests and intent branches; compute busy lanes.
3. Select the wave by the Queue rule in the card intake
   ([tiden-product-workflow.md](tiden-product-workflow.md)) and dispatch every
   selected story whose paths fit a free lane; there is no fixed cap on
   workers, and the owner's target is at least seven at once (10–15 later). The
   real limits are file overlap (lanes), the GitHub CI queue and the
   owner's attention for what it keeps and for questions; the orchestrator
   watches them.
4. Each brief names the story and adds only what the story and the Tiden
   working contract do not hold: the files that other active workers' stories
   and open pull requests are changing (Tiden does not show concurrent work),
   and limits beyond the story, such as read-only production. Acceptance
   criteria, lane, expected paths and checks come from the story and the working
   contract; the evidence and final result follow the level (Worker protocol);
   the worker sets the level from the working contract (`AGENTS.md`). The brief
   does not restate them. A skip without a story has a brief that carries the
   goal, expected paths, acceptance and checks.
5. Answer workers' questions and audit each merge report (Worker protocol). Read
   a waiting worker's terminal with `superset terminals read` and give the owner
   exactly what it asks, so the owner need not open worker terminals.
6. After merge: read the worker's post-merge check in its final result, update
   the story (for a skip, the release spec sync updates it), free the lane.
7. Escalate to the owner: priority conflicts, the pull requests and actions the
   owner keeps under Review levels in `AGENTS.md`, disagreeing reviewers and
   unclear product direction as questions, any feature or improvement story that
   is not ready. Relay to the owner the summary of every owner-level pull request
   an agent merged.

The orchestrator starts no new worker when its context is above about 60%, at the
end of the working day or when the current wave of tasks is done. Running workers
still reach their final result: merge, post-merge check, archive. The orchestrator
keeps its rules and its current state in a handoff file and updates the state
after every decision, so a compaction or a new session loses nothing; the file
holds no finished history. Finished history, the decision log and the session
notes live in the orchestrator log, a separate file next to the handoff file
that a new session does not read on start. A new orchestrator session first
reads the handoff file and the list of live sessions, and does not read
documents already loaded into its context (`AGENTS.md`, `CLAUDE.md`) a second
time.

Questions to the owner use plain names, never internal codes alone. Each states
what it is, why it matters to the owner, what it costs, the options with their
consequences and a recommendation. There is no limit on how many are asked;
"да на всё" ("yes to all") accepts every recommendation except a merge the owner
keeps, which still needs an explicit "merge <n>".

The orchestrator log holds the owner's decisions: its recommendation, the
owner's choice and whether they matched, so autonomy grows on facts. At the end
of each session it writes two or three lines to the orchestrator log: what got
in the way and what to change. Once a week it reviews the decision log, these notes and
the measurements under Worker protocol, and proposes rule changes as separate
pull requests.

## Worker protocol

The orchestrator starts each worker in its own Superset workspace, with its own
worktree and branch, and keeps a table: task, dependencies, workspace, branch,
status, result. For Claude Code workers, model and effort follow the kind of
work; the orchestrator picks them for each worker:

| Work | Model and effort |
| --- | --- |
| Orchestrator | Opus, high; medium for routine sweeps |
| Product code, CI and release code, permission settings | Opus, high; xhigh only for an autonomous task longer than about 30 minutes |
| A normal-level code fix of Effort 1 or 2 in one lane with tests | Sonnet, high, as a trial (below) |
| Security, unclear root cause | Opus, xhigh |
| Tiden card and spec text, production operation plans | Opus, high |
| Research reports | Opus, medium |
| Process and wording documents, translations, receipts, gathering data from logs and transcripts | Sonnet, medium |
| Checklist subagent: card and process-text checklists ([review-protocol.md](review-protocol.md)) | Sonnet, medium |
| Code exploration | Haiku or Sonnet, low |

Fable is a reviewer only, under Review levels in `AGENTS.md`; reviewers follow
those levels whatever the worker's model. The Sonnet code trial records, for each
such unit, green CI from the first push and its release review findings, next to
comparable Opus units; the weekly review keeps or ends it after eight units. The
orchestrator measures usage-limit spend by role, review findings and rounds per
pull request and the share of pull requests with green CI from the first push;
the weekly review reads these measurements.

The card is the plan: there is no plan approval round. The worker writes and
waits for the answer only before going out of scope: another task's file, an
action the owner keeps, a change to the card's criteria, or a fork without an
obvious answer.

The worker writes without waiting:

- at the start: its objective and promises, for information; it starts the
  intent at once, and the promises cannot be edited after the start;
- when it opens the pull request;
- when it merges: the pull request, CI, review results, the Tiden verdict or
  recorded skip and the squash SHA. It merges itself when the conditions under
  Review levels in `AGENTS.md` hold and no merge freeze is announced, and hands
  a pull request the owner keeps to the owner. The orchestrator audits the
  report after the merge: that the level follows the signals in `AGENTS.md`
  and, for a normal-level pull request, that the author checklist, the "fails
  without the fix" record and any process-text checklist review are in the
  description. A missing record is added to the pull request description; a
  level the signals raise gets that level's missing reviews on the squash
  commit, and their blocking or major findings hold the release as in the
  release review ([review-protocol.md](review-protocol.md)); a merged pull
  request the owner keeps goes to the owner at once, with a revert pull request
  ready;
- its final result. It ends with a `SUPERSET_WORKER_DONE` or
  `SUPERSET_WORKER_BLOCKED` block. Both list `task`, `summary`, `files` and
  `checks`; DONE adds `handoff`, BLOCKED adds `reason` and `needs`.

The final result depends on the level:

- micro: merged after green CI;
- normal, important and owner: the pull request and its Tiden branch, if any,
  merged and checked after the merge, the intent session closed with PASS or a
  recorded skip; at owner level, also the plain-language summary for the owner
  (`AGENTS.md`);
- a pull request the owner keeps: the pull request ready (not draft, green CI,
  reviews with no open findings, Tiden closed or skipped) and a summary for the
  owner in its description.

The owner decides a pull request it keeps and its Tiden branch together: an
explicit "merge <n>" covers both, and the orchestrator may carry it out. Without
that explicit word neither the worker nor the orchestrator merges them. After the
merge the worker checks it and reports.

Checked after the merge means a successful `main` push run on the squash commit
or a later one, a green `Tiden drift` run on it or a later one (a red run names
the files to anchor, TIDEN-CI.md) and, for a change with a Tiden branch, Tiden
main and drift read back as in [tiden-product-workflow.md](tiden-product-workflow.md).

After its final result the worker stays open until its pull request is merged and
checked after the merge. The orchestrator then closes its terminal and archives
the workspace without asking again, after checking that the worktree has no
uncommitted changes: archiving deletes the folder, the branch stays.

Claude Code workers and the orchestrator talk through messages between sessions.
A Codex session asks in its terminal, which the orchestrator reads and answers in.
The orchestrator checks a silent worker when it gets the idle notice; there are no
timed reports.

## Sessions

- Every session uses its own worktree and branch. Never share a worktree and never
  use bare `git stash`; the stash stack is shared across worktrees.
- Codex and Claude Code follow the same rules. Tool-specific setup lives in
  `CLAUDE.md` and the [Claude Code Tiden guide](claude-code-tiden.md).
- **Context budget.** Every call re-reads the whole context, so cost grows with
  session length. One story is one worker session. About 250k tokens of context
  is a soft signal, not a stop: the worker finishes its current step and hands
  off only at the next natural boundary (between cards, or before a long new
  phase). It then writes a handoff file (task, state, open items, next step) into
  its scratchpad and tells the orchestrator, which starts a fresh session from
  that file with the context it needs. The orchestrator restarts from its own
  handoff file at about 300k.
- **Large output.** Tiden commands use `--format json` with a `jq` field
  projection; files over about 20k characters are read by range or `grep`.
  Whole exports never go into a session or a reviewer packet.
- **Messages.** A worker writes once per state change (the start, a question,
  the pull request opened, merged, blocked, the final result), not
  progress reports. The orchestrator asks for an idle notice only when it
  expects an answer.
