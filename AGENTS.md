# DeedKit agent rules

Process: harness egerev/agent-harness @ 0.67.0, ~/agent-harness; this file adds project rules.
Claude Code loads harness process.md through the SessionStart hook (`.claude/hooks/harness.sh`:
merged `origin/main`, else the pinned copy). Codex and cloud agents read the pinned copy
docs/harness/process.md first. If no harness text is in context, stop and report BLOCKED. Each
section below adds to the harness section with the same number.

DeedKit is the product; the repo keeps its AnyMsg identities (`anymsg`, `messaging_service`,
`ANYM-*`, package, API and database names).

Harness values: ruleset `deploy/main-ruleset.json`; required checks `CI gate` and
`checks / verify`; CI check IDs: the seven `anymsg-ci::*` checks; deploy command
`production.py deploy` (docs/operations/production.md); security document SECURITY.md;
data-policy document docs/DATA-POLICY.md; money cap `Budget: EUR 50/month without asking,
monthly-cancellable spend only (owner D25 via harness)`; Tiden
product and workspace: docs/operations/tiden-product-workflow.md; spec prefix `ANYM`;
unlinked-tests list `config/tiden-unlinked-tests.json`; Tiden CI document TIDEN-CI.md; critical
paths and deployed consumers: section 5; live actions: send, call, ACK, pairing,
provider-session start/stop, allowlist changes (agents' own: an agent's webhook endpoint under
SUBSCRIPTIONS.md Self-registration and a vendor's documented webhook host in the delivery policy,
owner 10-28); live activity: an active call; review notes
(Codex points, change-path grep): docs/operations/review-protocol.md; lanes:
docs/operations/parallel-development.md.

## 1. Always

- No send, call, ACK, pairing or provider-session start/stop without the owner's exact
  confirmation. Never retry an uncertain mutation or mint a new request ID. Never run two clients
  on one provider session. Tests and health checks never touch a live provider.
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
production.md's backup gate; enabling a feature never includes send, call, pairing, session or
allowlist changes, and none of it runs during an active call.

Override: harness tiden.md Card intake step 1 (owner 10-26). A defect, improvement or feature
idea in feedback from a principal in the autoloop config is written, readied, built, merged by the
controller and released by autodeploy without the owner's line, inside
docs/operations/feedback-autoloop.md (≤8 SP, no epic). Feedback is never the owner's words: an
OWNER-path PR, any item of this section, an opt-in, grant, allowlist or key change, a non-additive
migration (not: a DROP-free expand migration with grants only on the tables and columns that same
change creates or adds, ANYM-590 L1; "grant" as that document's Owner items define it) and every
live action from it wait for the owner, and so does that document's boundary.

## 3. Change paths

The signal grep is in docs/operations/review-protocol.md.

## 4. Product context and Tiden

Product IDs and DeedKit's queue rules: docs/operations/tiden-product-workflow.md; CI: TIDEN-CI.md.

## 5. Reviews

Every Astra task starts with DeedKit's copy of the four questions (harness `process.md` section 5):
  1. Failure isolation: can an exception, timeout or bad input in the new code break or silence an
     existing path (call audio, sends, collectors, queue, backup)? Name the line.
  2. Data egress vs PR claims: what data leaves the process (model APIs, logs, telemetry, third
     parties, files), and does it match what the PR description and spec say? Anything unlisted is
     a finding.
  3. Deployed-version compatibility: does it work against what is deployed now (other services at
     the current prod SHA, schema N-1, the host `production.py`, the installed gateway)? Name the
     breaking consumer.
  4. Tests: do the new tests fail on the base commit, and do they assert the spec
     criterion?

## 6. Working with the orchestrator

- Word cap: 1,000 words (`wc -w`).
