# DeedKit agent rules

Process: harness egerev/agent-harness @ 0.83.0, ~/agent-harness; this file adds project rules.
Claude Code loads harness process.md through the SessionStart hook (`.claude/hooks/harness.sh`:
merged `origin/main`, else the pinned copy). Codex and cloud agents read the pinned copy
docs/harness/process.md first. If no harness text is in context, stop and report BLOCKED. Each
section below adds to the harness section with the same number.

DeedKit is the product; the repo keeps its AnyMsg identities (`anymsg`, `messaging_service`,
`ANYM-*`, package, API and database names).

Installing for yourself on your own server? Read docs/personal-install.md.

Harness values: ruleset `deploy/main-ruleset.json`; required checks `CI gate` and
`checks / verify`; CI check IDs: the seven `anymsg-ci::*` checks; deploy command
`production.py deploy` (docs/operations/production.md); security document SECURITY.md;
data-policy document docs/DATA-POLICY.md; money cap `Budget: EUR 50/month without asking,
monthly-cancellable spend only (owner D25 via harness)`; Tiden
product and workspace: docs/operations/tiden-product-workflow.md; spec prefix `ANYM`;
unlinked-tests list `config/tiden-unlinked-tests.json`; Tiden CI document TIDEN-CI.md; live actions: send, call, ACK, pairing,
provider-session start/stop, allowlist changes (agents' own: an agent's webhook endpoint under
SUBSCRIPTIONS.md Self-registration and a vendor's documented webhook host in the delivery policy,
owner 10-28); review paths: none (section 5); live activity: an active call; pull request lines
(change-path grep, autoloop protected paths): docs/operations/review-protocol.md; lanes:
docs/operations/parallel-development.md.

## 1. Always

- No send, call, ACK, pairing or provider-session start/stop without the owner's exact
  confirmation, except the incident recovery authority in section 2. Never retry an uncertain mutation or mint a new request ID. Never run two clients
  on one provider session. Tests and health checks never touch a live provider.
- Override: the exact confirmation per message in one courier chat (owner 11-11: «Если ему говорю
  какую-то цель, он там может серию сообщений делать сам. Я вообще с водителем не хочу общаться ... он может
  полностью их проводить, пока они не доходят до меня, если только он не справляется»). Under the owner's goal for
  an order, recorded by `android_courier_chat action=goal`, the agent messages that order's courier only, inside
  the code's bounds, and hands the chat back when it cannot cope. Calls, `android_execute` and every other
  send keep this section's confirmation. Enforcer: services/android/tests/test_courier_goal.py.
- Never change applied migrations, provider IDs, existing test paths, names or parameters (Tiden
  IDs) or the seven `anymsg-ci::*` checks.
- Deploys only via `scripts/production.py` from a green `main` SHA (docs/operations/production.md);
  other production changes: section 2 gates. Never prune shared Docker data; SSH is read-only
  except `production.py`, `deedkit admin` and section 2-gated steps. Merged source is not live
  verification.
- Backups: over the tailnet to the shared backup server only; no public backup port.

## 2. What the owner keeps

Authority: owner decision D25 (harness, 2026-10-27, «1) да 2)да 3)да»): the owner keeps only the
five harness items; production changes, rollbacks, restores, destructive SQL, keys and access are
agents' under the harness section 2 gates. Item 2 asks only what docs/DATA-POLICY.md does not
cover; item 3 is the Budget line above. The live actions act on the owner's messenger accounts
under the owner's name (item 4) and the owner's devices and provider auth (item 5): they keep
section 1's exact confirmation. In a task (a ready card) the agent applies its migration under
production.md "Migration releases"; enabling a feature never includes send, call, pairing,
session or allowlist changes, and none of it runs during an active call.

Releases and migrations follow harness process.md section 2 (owner 11-04: «давай все делать не ждать ничего»).
production.md "Migration risk tiers" lists what deletes or rewrites user data (critical);
"Migration releases" item 4 is the hand route.

Override: harness process.md sections 1–5, incident recovery only
(owner 10-30, Telegram sends: «это быстрый hotfix», «И только после этого уже продолжаем работу
над задачами»). A confirmed production outage or broken critical user path takes priority over
all other work. The agent immediately performs the smallest reversible recovery: restart the
existing service/session, compatible rollback, or a service-only hotfix from verified green main
through the promotion tool. This is standing authority; do not ask again for those actions.
Defer Tiden/card intake until recovery. Preserve CI, source/image checks, active-call checks,
state and session identity. No sends/replays, new auth, access changes or destructive actions are
included. Enforcer: follow [incident recovery](docs/operations/incident-recovery.md), including
before/after evidence and undo; complete deferred checks and process corrections before resuming
other tasks. This local override takes precedence over the pinned harness; its replaced gates
and owner words are recorded in that runbook.

Override: harness tiden.md Card intake step 1 (owner 10-26). A defect, improvement or feature
idea in feedback from a principal in the autoloop config is written, readied, built, merged by the
controller and released by autodeploy without the owner's line, inside
docs/operations/feedback-autoloop.md (≤8 SP, no epic). Feedback is never the owner's words: an
OWNER-path PR, any item of this section, an opt-in, grant, allowlist or key change, a non-additive
migration (not: a DROP-free expand migration with grants only on the tables and columns that same
change creates or adds, ANYM-590 L1; "grant" as that document's Owner items define it) and every
live action from it wait for the owner, and so does that document's boundary.

## 3. Change paths

The signal grep is in docs/operations/review-protocol.md. AUTO+review is gone (section 5): a path is
AUTO or OWNER.

## 4. Product context and Tiden

Product IDs and DeedKit's queue rules: docs/operations/tiden-product-workflow.md; CI: TIDEN-CI.md.

## 5. Reviews

Override: harness process.md sections 2, 3 and 5, reviews.md and tiden.md review steps (owner
11-11: «отмени все ревью в целом», «мы их и так отменяли», «не только текущих у всех в будущем»).
No review on any path: no Astra, Fable, Codex, plan or card review, no Diff read. A pull request
merges on green required checks with a failing-first test; PR line `Review record: none (owner
11-11: no reviews)`. Kept: section 1's exact owner confirmation of live actions, and a fresh verified
backup and undo before deleting or rewriting user data. Enforcer: the merge keeper and the autoloop
controller enqueue a green pull request with no approval (tests/test_merge_keeper.py,
tests/platform/test_autoloop.py).

## 6. Working with the orchestrator

- Word cap: 1,000 words (`wc -w`).
