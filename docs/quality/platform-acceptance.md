# DeedKit specifications and verification in Tiden

This document defines migration acceptance and evidence boundaries. Gateway,
calendar and calls are integrated in this same repository alongside messaging.
The repository is now DeedKit; its GitHub identity and Tiden product are retained. A Git worktree is another checkout of that same
repository; it does not establish a second product or repository connection.

## How changes reach Tiden

While the intent loop is paused
([product workflow](../operations/tiden-product-workflow.md#one-tiden-branch-per-pull-request)),
steps 1 and 3 use the card's `Tiden branch:` instead of an intent session.

1. Start a scoped intent in the existing product and record its branch/session.
   Use the current Git branch and explicit product/workspace. Do not run setup
   to create or replace the product. Link the code PR with `tiden intent link-pr`.
2. Commit and push each verified increment to the existing repository. Keep the
   current repository name until the separately accepted rename step. Local
   uncommitted files and code in other repositories are not a GitHub sync input.
3. Review behavioral specs on the intent branch. Specs describe observable
   contracts; source anchors identify the implementation at a recorded revision.
   A plan or source anchor alone is not implementation or test evidence.
4. Execute tests in each module's locked environment. Report real JUnit results
   and required check outcomes, including failed, skipped and unrun checks. Use
   the exact tested checkout SHA and explicit Tiden branch, never a convenient
   successful main SHA. Link only tests whose assertions prove the spec.
5. Verify the branch preview, proposed links, run association and coverage. Merge
   code and reviewed specification changes as separate operations. After both,
   read back the actual main gate, IDs, links and source anchors. A branch gate
   is not main's gate. GitHub App processing status is a separate acceptance item.

The GitHub App checks merges into the connected repository's primary branch.
Pushing a feature branch or attaching a PR does not prove that main has been
indexed. Keep in sync, Processing history and drift status must be checked; do
not advance a drift watermark just to dismiss a real unprocessed change.

## Product areas

Preserve the existing eleven messaging components, specification IDs, test IDs,
suite identities and historical source anchors. The integration adds the following logical areas: **Platform Gateway**, **Family Calendar**, **Voice
Calls**, and **Platform Integration & Operations**. These four components are registered in the existing product.
Component metadata is global in the installed CLI; do not assume an intent branch isolates component edits.

Existing messaging gateway specs still describe the messaging backend. They do
not automatically cover the new platform gateway. Calendar/calls tests are imported with their source and run in this repository's CI. Review the assertions before linking any additional behavior; a file
anchor alone is not coverage.

## Behavior and proof matrix

The labels below are local review labels, not remote Tiden requirement IDs.
All rows require real executions after integration; none are marked verified by
this document. Test anchors are candidates until their bodies and exact versions
have been reviewed. Add a test when the existing assertions do not cover the
whole stated contract.

| Label / area | Required behavior | Concrete proof and boundary |
|---|---|---|
| G1 Gateway catalog | Only configured modules, profile tools and permitted principal scopes are exposed; manually calling a hidden method also fails. | Exercise MCP discovery, MCP call and REST, with two principals and spoofed headers. `services/gateway/tests/test_policy.py` and `test_transport.py` are the initial candidates. Also prove backend ACL enforcement with real synthetic backends. |
| G2 Gateway revocation | An atomic policy update revokes access without reconnecting; malformed or rolled-back policy fails closed. | Keep an MCP client open across the update, then call through both transports. Do not count a new process loading a new file as hot reload. |
| G3 Files | The original bytes remain available through a bounded streaming ticket, subject to TTL and revocation. | Compare byte count and SHA-256, deny expired/revoked/cross-principal tickets, reject arbitrary URLs and redirects, measure memory/backpressure and midstream cancellation. A URL in JSON alone is insufficient. |
| G4 Events | Backend subscription owner, cursor and ACK identity survive routing and gateway-key rotation. Passive reads do not acknowledge events. | Seed events and two backend identities, rotate only the gateway key, verify pending events and exact cursor/ACK state. Prove no loss or replay caused by the migration. Do not claim new delivery guarantees absent from the backend. |
| G5 Lifecycle | The real SDK transport starts and stops cleanly; unsupported SSE is explicit. | Run initialize/list/call with the actual SDK, test GET `/mcp` rejection, bounded request bodies, shutdown and closed connection pools. Mocks of domain responses are acceptable; replacing the MCP transport is not. |
| G6 Isolation | Slow calls and bulk files cannot monopolize message/calendar capacity. | Same-data direct API, gateway REST and MCP measurements: p50/p95, explicit budgets, at least 1,000 requests in three runs, including contention. Keep measured results, not inferred PASS. |
| G7 Actions | Permission for one action never enables a neighboring action in a generic operation tool. | Permit rename-group and reject leave-group within the same category, including aliases, preview/execute revocation and digest mismatch. Keep generic mutation tools unexposed until this is proved. |
| C1 Calendar identity | Connections, members, family roles and agent grants are distinct; group administration does not imply access to a private calendar. | Existing source tests `test_group_admin_cannot_see_ungranted_private_calendar`, `test_calendar_list_intersects_connection_member_and_agent_scopes` and auth account-binding tests. Extend with cross-family/principal transport cases. |
| C2 Calendar privacy | Busy-only access hides event details and opaque provider identifiers consistently. | Existing `test_busy_only_hides_titles_in_all_agenda_rows` and REST/MCP parity assertions. Check slots, errors, caches and receipts as applicable, not just one agenda row. |
| C3 Availability | Missing, stale, partial or unbound required sources never become verified free time. | Existing `test_unknown_required_source_never_yields_verified_free_slot`; add all coverage states and mixed-provider windows. Synthetic tests do not prove complete real family coverage. |
| C4 Calendar writes | Write opt-in, current grants, plan binding, concurrency checks and operation identity are enforced at execution. | Existing grant-revocation, idempotency, worker/provider contract tests; add ETag conflict, timeout/unknown outcome and no blind repeat. No live invitations or event edits in CI. |
| C5 Family assignments | Assignment status/version is durable; an agent cannot invent another member's acceptance. | Existing `test_assignment_cannot_claim_someone_elses_acceptance` and update/cancel/version-conflict assertions. Linking a calendar as a child's schedule does not establish their own provider-account identity. |
| C6 Server calendar | Moving off the Mac preserves IDs, roles, grants, encrypted credentials and operation history with exactly one active worker. | Synthetic DB+vault backup/restore drill, source-path independence, restart and migration tests. Separately record actual server cutover receipts and Mac-independence verification. Unit tests alone cannot prove production migration. |
| V1 Calls ownership | A principal owns the brief and all derived tasks, followups, artifacts, media and decisions. | Add a two-principal denial matrix at every endpoint. The current shared control token and a task-only filter do not prove this requirement. This is a prerequisite for gateway exposure. |
| V2 Owner decisions | Counterparty audio/tool text cannot approve an owner decision; changed terms invalidate an old answer. | Existing source `test_counterparty_cannot_approve_owner_request_by_audio_or_tool_argument`, decision-workflow tests and owner-browser boundary checks. Extend through the platform gateway. |
| V3 Call execution | Preparation does not dial, one explicit attempt is bound to its recipient/channel, and unknown outcome is not retried automatically. | Existing outbound preparation, concurrent-repeat/unknown and restart-reconciliation tests with provider doubles. No real calls, microphones or fallback channels in CI. |
| V4 Calls lifecycle/privacy | Stop/restart release owned media resources and preserve durable status; credentials and private learning are scoped. | Existing task restart, WebRTC cleanup, secret-redaction and learning API tests; extend with per-principal ACL, artifacts and isolated state paths. Passing audio pipeline tests is not a claim about transcription quality. |
| I1 Independent modules | Selected modules start with independent environments, processes and data; absent modules add no tools or heavyweight dependencies. | Profile resolution, image/wheel install and Compose validation, installed outside checkout. Calendar-only must not import Pipecat, Chromium or messaging sessions. |
| I2 Evidence identity | Old IDs/links survive and every new result has the correct module, source revision, test identity and branch. | Stable full exports before/after plus exact comparison; authentic CI readback. Never use equal counts as preservation proof. Never ingest/recreate old tests to repair a path move. |

## CI and specification maintenance

- Keep the seven existing `anymsg-ci::*` check identities and original messaging
  test logical paths. Add uniquely named module reports/checks; do not rename
  old cases to a new platform prefix. Physical test moves wait for demonstrated
  ID preservation through a real report/readback.
- Keep module jobs in their own environments and separate PostgreSQL schemas or
  databases. New reports must record module, actual source SHA, workflow URL,
  lock/tool versions and expected report set. Never merge different revisions
  into one green result. Missing or all-skipped required suites remain blocked.
- The trusted reporting job receives Tiden credentials but no provider sessions
  or personal data. Test jobs receive synthetic fixtures only. A failed job must
  still upload its real report. Fork PRs never receive reporting credentials.
- Ordinary CI publishes executions; it does not regenerate specs or replace
  test links. Agents review and update behavior changes on the corresponding
  intent branch (while the loop is paused, the card's `Tiden branch:`), then
  reconcile source anchors and assertion-level links.
- R1 acceptance adds G1–G6 and the calendar read contracts it actually exercises.
  Later increments add G7, calendar mutation/server and calls requirements when
  implemented. No full-platform green verdict while required areas are unrun.

## Official references

- [GitHub App and primary-branch processing](https://docs.tiden.ai/docs/how-to/connect-github-repository)
- [Reviewed spec branches and merges](https://docs.tiden.ai/docs/how-to/manage-branches)
- [Spec source provenance and imports](https://docs.tiden.ai/docs/how-to/import-requirements)
- [Component, test-link and gate workflow](https://docs.tiden.ai/docs/how-to/configure-quality-gate)

Use installed CLI help when a command differs from the documentation. API JSON
still calls specs `requirements`. Preserve the raw private receipts; public
documentation must not contain credentials or personal source data.
