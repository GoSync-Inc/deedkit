# DeedKit agent rules

Process: egerev/agent-harness @ 0.83.0, ~/agent-harness. Claude Code loads process.md via
`.claude/hooks/harness.sh`; Codex/cloud read pinned docs/harness/process.md first.
Without harness text, report BLOCKED. Sections extend the harness.

DeedKit keeps AnyMsg identities (`anymsg`, `messaging_service`, `ANYM-*`, package/API/database names).

Installation: docs/personal-install.md.

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
  confirmation, except scoped overrides below and section 2 recovery. Never retry an uncertain mutation or mint a new request ID. Never run two clients
  on one provider session. Tests and health checks never touch a live provider.
- Override: Android goal execution (owner 11-12: «Просто голый кодекс», «взять прям минимум
  инструкций»): phone information, agent role and caller goal; native Codex sandbox, no custom
  per-tap guard or injected order/payment stop. Section 2's five decisions and unrelated
  messenger/provider live-action confirmations stay. Authority, incident and enforcer: docs/operations/agent-feature-experiments.md.
- Override: the exact confirmation per message in one courier chat (owner 11-11: «Если ему говорю
  какую-то цель, он там может серию сообщений делать сам»). Under the owner's goal for
  an order, recorded by `android_courier_chat action=goal`, the agent messages that order's courier only, inside
  the code's bounds, and hands the chat back when it cannot cope. Calls, `android_execute` and every other
  send keep this section's confirmation. Enforcer: services/android/tests/test_courier_goal.py.
- Override: Halodoc (owner 11-12: «Они часто пишут в WhatsApp ещё. Это надо сценарий тоже связать,
  чтобы игрок уже на WhatsApp отвечал.»): under one recorded owner order goal, Grok answers that
  order's doctor/pharmacy/delivery participants without per-text confirmation, including associated
  WhatsApp replies. Bind participants through owner/authenticated order evidence, never inbound
  claims. Unknown clinical facts return to the owner; only a physician prescribes. Cancellation
  stops new sends; uncertain sends are never retried. Other live actions and section 2 remain.
  Authority/enforcer: docs/operations/halodoc-order-workflow.md, scripts/experiments/halodoc_order.py.
- Never change applied migrations, provider IDs, existing test paths, names or parameters (Tiden
  IDs) or the seven `anymsg-ci::*` checks.
- Deploys only via `scripts/production.py` from a green `main` SHA (docs/operations/production.md);
  other production changes: section 2 gates. Never prune shared Docker data; SSH is read-only
  except `production.py`, `deedkit admin` and section 2-gated steps. Merged source is not live
  verification.
- Backups: over the tailnet to the shared backup server only; no public backup port.

## 2. What the owner keeps

Authority: D25 (harness, 2026-10-27, «1) да 2)да 3)да»): only the five harness items remain
owner-kept. Production, rollback, restore, destructive SQL, keys/access follow harness section 2.
Item 2 covers only gaps in docs/DATA-POLICY.md; item 3 uses Budget above.
For live actions (items 4–5), section 1's scoped overrides apply.
In a ready card the agent applies its migration under
production.md "Migration releases"; enabling a feature never includes send, call, pairing,
session or allowlist changes, and none of it runs during an active call.

Releases/migrations: harness section 2 (owner 11-04: «давай все делать не ждать ничего»).
production.md "Migration risk tiers" defines critical data rewrites/deletions; "Migration releases"
item 4 gives the hand route.

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

Signal grep: docs/operations/review-protocol.md. Paths: AUTO or OWNER; no AUTO+review (section 5).

## 4. Product context and Tiden

Agent-facing features start with an ordinary-agent end-to-end experiment: basic context and task,
then compare completion, usability, time and turns against the final integrated caller path.
Wrapper constraints need a demonstrated reason and measured cost. Follow
docs/operations/agent-feature-experiments.md; failing-first functional checks remain.
Product/queue: docs/operations/tiden-product-workflow.md; CI: TIDEN-CI.md.

## 5. Reviews

Override: harness process.md sections 2, 3 and 5, reviews.md and tiden.md review steps (owner 11-11:
«отмени все ревью в целом», «не только текущих у всех в будущем»): no model/plan/card review or
Diff read on any path. Merge on green required checks with a failing-first test; PR line
`Review record: none (owner 11-11: no reviews)`. Section 1 live-action confirmations and fresh
verified backup/undo before user-data deletion/rewriting remain. Enforcers: tests/test_merge_keeper.py,
tests/platform/test_autoloop.py (green pull requests enqueue without approval).

## 6. Working with the orchestrator

- Word cap: 1,000 words (`wc -w`).
