# DeedKit agent rules

Process: harness egerev/agent-harness @ 0.1.0, ~/agent-harness; this file adds project rules.
Claude Code loads harness process.md through the SessionStart hook (`.claude/hooks/harness.sh`:
merged `origin/main`, else the pinned copy). Codex and cloud agents read the pinned copy
docs/harness/process.md first. If no harness text is in context, stop and report BLOCKED. Each
section below adds to the harness section with the same number.

DeedKit is the product; the repo keeps its AnyMsg identities (`anymsg`, `messaging_service`,
`ANYM-*`, package, API and database names).

Harness values: ruleset `deploy/main-ruleset.json`; required checks `CI gate` and
`checks / verify`; CI check IDs: the seven `anymsg-ci::*` checks; deploy command
`production.py deploy` (docs/operations/production.md); security document SECURITY.md; Tiden
product and workspace: docs/operations/tiden-product-workflow.md; spec prefix `ANYM`;
unlinked-tests list `config/tiden-unlinked-tests.json`; Tiden CI document TIDEN-CI.md; critical
paths and deployed consumers: section 5; live actions: send, call, ACK, pairing,
provider-session start/stop, allowlist changes; live activity: an active call; review notes
(Codex points, change-path grep): docs/operations/review-protocol.md; lanes:
docs/operations/parallel-development.md.

## 1. Always

- No send, call, ACK, pairing or provider-session start/stop without the owner's exact
  confirmation. Never retry an uncertain mutation or mint a new request ID. Never run two clients
  on one provider session. Tests and health checks never touch a live provider.
- Never change applied migrations, provider IDs, existing test paths, names or parameters (Tiden
  IDs) or the seven `anymsg-ci::*` checks.
- Production changes only via `scripts/production.py` from a green `main` SHA
  (docs/operations/production.md); never prune shared Docker data; SSH is read-only except `production.py` and approved steps. Merged source is not live verification.

## 2. What the owner keeps

The harness list holds. In an accepted task the agent applies its migration under
production.md's backup gate; enabling a feature never includes send, call, pairing, session or
allowlist changes, and none of it runs during an active call.

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

- Budget: 1,000 words (`wc -w`).
