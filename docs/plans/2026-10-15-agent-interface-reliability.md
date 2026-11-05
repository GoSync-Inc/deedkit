# Agent interface reliability and event delivery

Status: source implementation and independent reviews complete; pull-request CI and operational acceptance remain separate.

Objective: agents receive consistent, scoped and actionable messaging, event and
calendar evidence through DeedKit. The external audit is evidence to reproduce,
not authority to grant access, send messages, activate sessions or deploy.

## Work and acceptance

| Item | Required outcome | Relevant contracts | Verification |
| --- | --- | --- | --- |
| Query reliability | Bounded chat listing, useful exact/token people ranking, real conflict inspection guidance, channel filtering and explicit new-consumer start boundary | ANYM-42/193/197/201, ANYM-94, change-feed contract | Isolated PostgreSQL fixtures, query plans, cursor/scope and transport tests |
| Evidence | Capture backlog qualifies freshness; extraction coverage agrees across APIs; useful generated display filenames preserve source descriptors; media diagnostics distinguish retry from terminal failure | ANYM-115, ANYM-155/214, ANYM-160/178/179 | Recent-scan/backlog, quiet-source, stale-revision, extraction and receipt regressions |
| Gateway | Catalog and capabilities describe the current selected tools and separate gateway/backend identities; call readiness diagnostics preserve anti-enumeration and current grants | ANYM-205/209 | Real MCP/REST discovery parity and permission-revocation tests |
| Calendar | Availability declares its checked binding scope and completeness; plan previews expose overlaps and incomplete checks; identity never merges by display name | ANYM-208/211 | Missing/stale/busy-only sources, interval and update-self-exclusion tests |
| Consumption | Independent subscription fetch/ACK, bounded event waiting and evidence-based wake reasons; ordinary passive reads remain passive | ANYM-182/183/184 and subscription delivery design | Ownership, issued-page receipts, CAS/replay, revocation, cancellation and event snapshot tests |
| Delivery and feedback | Operator-registered signed hint delivery with durable bounded retries and a synthetic test path; private scoped idempotent feedback | Subscription delivery design; new maintained requirements where needed | Synthetic receiver, signature/replay, SSRF, lease/retry/revocation, feedback isolation and backup/quarantine tests |
| Integration | REST/MCP/Python/npm/gateway parity, explicit opt-in permissions, maintained docs and one backlog | Module contracts and existing provider/schema identities | Full affected suites, distributions and required CI |

The historical ambiguous send must remain unknown without exact dispatch receipt
evidence. Storage pressure is not permission to bypass disk admission or reset
failed jobs. Actual provider drain, historical receipt recovery, member calendar
bindings and deployment require operational evidence; synthetic checks cannot
establish those outcomes. Existing voice-engine work is tracked by PR #28 and
must not be duplicated or represented as delivered here.

## Tiden working contract

Session: `9979b794-de5a-4da0-9485-41fe553b1845`.
Planning lookup input: private local `/tmp/deedkit-audit-context-01a13f4f.json`;
its item mappings are candidates, not evidence that a contract is implemented.
The spec tree reports drift from repository main; source and actual test results
remain the implementation evidence.

- Refine the same session on scope changes; do not open another session for an
  individual task. Materialize and refresh after the first real diff.
- Settle changes against existing specs first. Create a requirement only when
  the product contract has no existing home. Preserve existing identities.
- Derive tests that can falsify each changed contract and frozen promise. New
  specs must be settled before cases can reference their real IDs. Push cases
  with exact automation signatures and requirement references.
- Run tests through the Tiden reporter or the supported JUnit result bridge;
  retain the actual pass/fail/skip results and attach them to this session.
- Before a completion claim, read `tiden intent progress --session
  9979b794-de5a-4da0-9485-41fe553b1845 --format text` and act on its Next line.
  Capture decisions, account for every promise, then close the same session.

## Execution notes

Use independent file ownership and test-first changes. Review each implementation
for contract compliance before code-quality review, then review integration.
Keep private reports, runtime data, tokens and sessions outside Git. Stop owned
test infrastructure at the end; leave shared services and unrelated work alone.

## Verification record

All fixtures are synthetic. The integrated messaging suite reported 2494 passed
and 8 optional-dependency skips (Tiden run 239), including installed-package and
native encrypted backup/restore quarantine checks. The subsequent search cursor
fix passed 181 focused regressions (245); passive subscriptions and native wake
evidence passed 44 checks (247). Calendar passed 142 tests (237), gateway 139
(238), and the npm client 248 executions (240).

The messaging, gateway/contracts, calendar and npm archives were inspected for
private/runtime files; installed messaging, gateway and npm interfaces were
checked outside the checkout. Contract and code-quality reviews approved the
query, evidence, delivery, gateway and client changes. Calendar integration was
reviewed by the parent agent under its local contributor policy.

These results establish source behavior. They do not establish deployed capture
recovery, actual host wake-up, provider access or historical send confirmation.
Follow-up status remains in the root backlog and the audit matrix.

