# Product work in Tiden

The [DeedKit Tiden product](https://app.tiden.ai/1dc18e09-4a09-4d74-8300-cd4302622357/products/75bd0580-2af4-4d5a-b236-c5a9d7ff7ce1) is the primary record of
requirements, queued work, priorities, delivery status and verification evidence.
Open **spec 260 — DeedKit product work — legacy DK index and delivery history**
for the complete navigation and delivered-source history.

This file is a stable entry point and legacy-ID crosswalk. Update the linked
Tiden requirement when work changes; do not add statuses, priorities or acceptance
checklists here. Module documents describe contracts and acceptance procedures,
not another work queue.

- [Agent workflow and standard Tiden skills](docs/operations/tiden-product-workflow.md)
- [CI and evidence rules](TIDEN-CI.md)
- [Dated reconciliation audit](docs/quality/tiden-reconciliation-2026-10-16.md)
- [Pre-migration backlog](https://github.com/GoSync-Inc/deedkit/blob/e513e3890dacadd5386d7c6924aa6d2b18b20069/BACKLOG.md)

The crosswalk preserves all 57 historical identifiers. Specification numbers are
unique in this product. DK-40 and DK-41 retain their existing identities in the
open implementation branches below; their presence is not a merged-code claim.
Cards marked "was N" were unbuilt Backlog cards; on 2026-10-21 the owner had them recreated
as new specs on `proposal/2026-10-21-*` branches, so they are off Tiden main until work starts.

| Historical ID | Tiden specification(s) | Topic |
| --- | --- | --- |
| DK-02 | 222 | Operational restore, originals integrity and receiver capacity |
| DK-03 | 223 | Retain file evidence and expire eligible originals after 30 idle days |
| DK-04 | 456 (was 224) | Execute durable fair history waves with restart recovery |
| DK-05 | 225 | Recover historical exhausted media jobs within bounded retry policy |
| DK-06 | 457 (was 226) | Classify and audit contradictory source observations |
| DK-07 | 227 | Accept native WhatsApp rollout and rollback on the intended installation |
| DK-10 | 228 | Integrate a host receiver and prove an idle-agent turn |
| DK-11 | 458 (was 229) | Provide a compact incoming overview across selected chats |
| DK-12 | 230 | Version independent file titles, summaries and AI artifacts |
| DK-13 | 459 (was 231) | Measure local and API file understanding quality and cost |
| DK-14 | 232 | Evaluate hybrid retrieval against a measured baseline |
| DK-15 | 460 (was 233) | Persist calendar incremental sync cursors |
| DK-16 | 234 | Establish Google and service recurrence parity |
| DK-17 | 235 | Project guardian assignments and family activity safely |
| DK-18 | 236 | Accept multiple Google and mixed Google/iCloud grants and privacy |
| DK-19 | 237 | Accept calendar isolation, vault/database restore and independent operation |
| DK-20 | 461 (was 238) | Persist calls control, owner notifications and follow-up scheduling |
| DK-21 | 239 | Accept authorized live voice behavior and acoustic quality |
| DK-22 | 462 (was 240) | Expand bounded passive rich message observations |
| DK-23 | 463 (was 241) | Add scoped profile pictures and permitted profile updates |
| DK-24 | 242 | Add typed group restrictions and admission operations |
| DK-25 | 243 | Add scoped join, invitation, channel and forum operations |
| DK-26 | 464 (was 244) | Add bounded scheduling, albums and richer media actions |
| DK-27 | 465 (was 245) | Evaluate expiring server-managed message approvals |
| DK-28 | 246 | Extend native WhatsApp capabilities one reviewed operation at a time |
| DK-29 | 247 | Publish a verified package release with registry receipts |
| DK-30 | 466 (was 248) | Add authenticated bounded MCP discovery notifications |
| DK-31 | 467 (was 249) | Record disposition of historical telemetry and subscription reviews |
| DK-33 | 250 | Accept deployed agent-audit behavior on the intended installation |
| DK-35 | 251 | Establish owner-selected calendar sources and primary/duplicate mapping |
| DK-36 | 252 | Define silence timers, quiet hours, debounce and delivery batching |
| DK-39 | 253 | Complete residual agent query and presentation feedback |
| DK-40 | 220 | Compare opt-in minimal Grok browser prompts |
| DK-41 | 221 | Review and accept verified blue-green HTTP release handoff |
| DK-42 | 254 | Narrow collector, indexer and enrichment database roles |
| DK-43 | 255 | Resolve bounded collector status, locking and page edge cases |
| DK-44 | 256 | Preserve instant event end in calendar mutation previews |
| DK-45 | 257 | Resolve subscription receipt annotations, hint latency and retention |
| DONE-01 | 199, 200, 201 | Conversation views and archived ordering |
| DONE-02 | 204, 207, 210 | Independent gateway, messaging, calendar and calls modules |
| DONE-03 | 258 | Native WhatsApp provider and explicit browser fallback |
| DONE-04 | 258, 178 | Native send receipts, shutdown and capability discovery |
| DONE-05 | 259 | Scoped file uploads and send reconciliation |
| DONE-06 | 258 | Native capture integrity and media locking |
| DONE-07 | 214 | Bounded original prefetch and download demand |
| DONE-08 | 182, 183, 184, 185, 186 | Durable subscriptions and file-readiness events |
| DONE-09 | 158, 162, 165, 178 | Opt-in sends and provider operation catalog |
| DK-01 | 209 | Calls owner authentication and service health |
| DK-08 | 217 | Issued subscription receipts and explicit ACK |
| DK-09 | 218, 182 | Durable signed hints and bounded waiting |
| DK-32 | 31, 42, 44, 48, 94, 115, 155, 160, 205, 208, 211, 219 | First agent-audit corrections |
| DK-34 | 27, 42, 198, 199, 200, 205, 209 | Compact/transcript projections and interface corrections |
| DK-37 | 216, 209 | Experimental Grok voice transport and readiness |
| DK-38 | 195, 199, 200 | Qualified service and unknown-message presentation |
| DONE-10 | 215, 154, 137 | Verified promotion and feature-branch CI routing |
| DONE-11 | 113, 121, 206, 211, 212 | Service recovery and operational corrections |
| DONE-12 | 51, 52, 94, 115, 178 | Quiet-source, conflict and receipt corrections |

Existing implementation review branches:

- DK-40 / spec 220: [minimal-prompt experiment](https://app.tiden.ai/1dc18e09-4a09-4d74-8300-cd4302622357/products/75bd0580-2af4-4d5a-b236-c5a9d7ff7ce1/branches/f2a387cf-ccd3-4dd2-bc12-1a25ffee26f6), [PR #34](https://github.com/GoSync-Inc/deedkit/pull/34).
- DK-41 / spec 221: [release admission](https://app.tiden.ai/1dc18e09-4a09-4d74-8300-cd4302622357/products/75bd0580-2af4-4d5a-b236-c5a9d7ff7ce1/branches/93b6fc08-78fd-499e-94b3-cd0c59cbd45a), [PR #40](https://github.com/GoSync-Inc/deedkit/pull/40).
