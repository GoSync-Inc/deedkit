# Product work through Tiden

Tiden is DeedKit's primary record of product requirements, future work, delivery
state and verification evidence. `BACKLOG.md` is a navigation entry point and
legacy-ID index. Do not maintain another editable queue in module documents,
plans, PR descriptions or chat summaries.

Use the existing [DeedKit product](https://app.tiden.ai/1dc18e09-4a09-4d74-8300-cd4302622357/products/75bd0580-2af4-4d5a-b236-c5a9d7ff7ce1).
Workspace: `1dc18e09-4a09-4d74-8300-cd4302622357`.
Product: `75bd0580-2af4-4d5a-b236-c5a9d7ff7ce1`.
Do not create a replacement product, reimport existing tests, or reset the drift
watermark to make a dashboard green.

## Standard skills first

Use the skills shipped with the installed Tiden CLI. A separate project-management
skill is unnecessary: this document supplies the project decisions those skills
cannot infer. Do not copy or fork the vendor skill bodies into another workflow.

| Skill | Use it for |
| --- | --- |
| `product-context` | Find existing specs and source anchors before planning or investigating; keep a mapping for each independent part. |
| `start-intent` | Begin or continue a scoped product change with observable promises; decide skip or the full loop by the Tiden rule under Review levels in `AGENTS.md`. |
| `capture-intent` | Reconcile the actual product change into existing specs or new narrowly scoped stories on the session branch, then close with the measured ledger verdict. |
| `link-coverage` | Connect already reported tests to requirements after reviewing the assertions; preserve existing links. |
| `generate-tests` | Reconcile genuinely missing test cases. For this existing product, review the import plan and identity/link preservation before any authoritative ingest. |
| `triage-issues` | Investigate reported runtime errors and prove a scoped repair; issue closure is separate from deployment acceptance. |

Inspect both scopes with `tiden skill list --agent codex --global` and `--local`.
A new worktree has no local copy, so install missing vendor skills with
`tiden skill install --agent codex --global`; use `--agent claude-code` for that
agent. Do not overwrite customized skills or change global hook trust,
credentials, sandbox permissions or other projects as part of routine setup.
`tiden doctor` can report global-scope warnings even when local skills and the
current session's network access work. Check each warning's actual scope instead
of rerunning setup blindly.

The local coding-agent intent loop uses the current agent and the Tiden CLI.
Tiden's hosted Agents page is a separate automation surface; configuring it or
adding an LLM credential is not a prerequisite for this workflow.

## Requirements, work and evidence

An Epic describes a product outcome; Features group coherent capabilities;
stories describe independently reviewable behavior with acceptance criteria.
Use existing parents/components and stable identities. Keep operational acceptance
separate when merged source cannot establish the property of a real installation.

Every queued item follows the card template under Card intake.

`High`, `Medium`, `Low` retain the old P1, P2, P3 ordering. Optional work stays
explicitly conditional; importing it does not authorize provider mutations,
publishing packages or production changes. `Critical` requires a concrete new
incident or owner priority decision.

Workflow status, merged source, test coverage and deployment are separate facts.
Use `Backlog` for future/conditional work, `Active` for work being implemented,
`Review` for a proposed implementation or acceptance awaiting review, and `Done`
only for the stated delivered scope. A broad maintained capability may remain
`Active` while its individual increments are delivered. A status update must
not weaken a contract, hide a failing test or remove it from verification scope.
Record the exact evidence and the unresolved boundary in the spec body.

The gate observed on 2026-10-16 includes queued `Backlog`/`Review` specs and the
General navigation index in its coverage calculation. These produce `no_test`
and a branch `NOT VERIFIED` verdict; main reports `BLOCKED` for the same coverage
gaps even when existing implementation evidence passes.
Workflow status does not automatically select release scope. Read the per-spec
reasons; do not create placeholder tests, link unrelated passing tests, or change
statuses to suppress this result. Release-readiness claims require a reviewed
release scope and actual evidence, separately from this whole-product plan.

An old `Active` baseline is not evidence of a currently running task. Do not
mass-convert legacy statuses without reviewing both implementation and how the
server includes that status in traceability/gates.

## Card intake

New work reaches Tiden from four sources: the owner; agent feedback recorded
through DeedKit `submit_feedback`; production issue triage (`triage-issues`); and
audits. `list_feedback` returns only the calling key's records (SUBSCRIPTIONS.md),
so the session holding that key relays its feedback to the orchestrator. A worker
that finds work outside its task reports it to the orchestrator instead of
starting it.

1. **Triage.** Search Tiden main and open branches (`product-context`,
   `tiden spec list`). A match, merged or not, is updated rather than duplicated.
   Decide whether the work is worth doing; a declined candidate gets its reason at
   its source and no card.
2. **Write.** Opus at high effort writes the card from the template
   below, never directly on main: on a proposal branch for a future-only
   requirement, which stays open until implemented, or on a review branch for a
   current defect, which merges to main after review and acceptance (Current
   truth and future proposals).
3. **Review.** The card's kind sets its reviewers (`AGENTS.md`: Fable, Astra,
   both, or the card checklist subagent of
   [review-protocol.md](review-protocol.md)); the card text goes to them as a
   file. They first judge whether the card should exist: a real need and value,
   no duplicate of an existing card or built-in capability, no over-engineering
   (a smaller or existing solution), and whether it must be built now. They may
   reject or shrink it; a rejected card gets its reason at its source.
4. **Accept.** Cards in the categories feature and improvement (owner-visible
   product behavior) are accepted by the owner, presented in plain words, or by
   the orchestrator relaying the owner's quoted words. Cards in defect, infra,
   refactor and research are accepted by the orchestrator once their reviewers
   approve with no open findings, necessity check included. The orchestrator
   brings such a card to the owner when the reviewers disagree, when it changes
   product behavior beyond restoring the contract, or when an owner-level
   trigger in `AGENTS.md` applies. Record who accepted on the card.
5. **Queue.** Accepted cards with a lane and expected paths form the ready queue.
   A wave takes current breakages (defects, unresolved production incidents) and
   hard deadlines first. The rest of the wave is about 70% product (feature,
   improvement) and 30% engineering (infra, refactor), counted by Effort, and
   holds at least one refactor card when one is ready. Within each share, order
   by Score, highest first. When a feature needs a refactor or an enabler, create a separate
   refactor or infra card and link it as the feature's dependency. A research card
   counts toward the category of the work it unblocks, or engineering when that
   is unknown. Deferred work stays on main as planned `Backlog` work: it is not cancelled, moved or copied.
   An existing card becomes ready through a spec edit that adds the template
   fields, reviewed like any spec edit, and its acceptance; it is never recreated.

```text
Title: <plain words: what changes for the owner>
Problem: <what is wrong or missing today and the desired behavior; evidence, source>
Acceptance criteria:
- <observable, testable criterion>
Category: defect | feature | improvement | infra | refactor | research
Lane: <lane>   Expected paths: <paths>
Depends on: <ANYM-n, or none>
Priority: High | Medium | Low (Tiden's priority field)
Commitment: committed | conditional | needs a triage decision
Boundary: <what is already implemented; what remains>
Evidence to close: <tests and checks; any separately authorized live action>
Value: <1|2|3|5|8>   Effort: <1|2|3|5|8; 13 and 21 only before the split>   Score: <Value / Effort, two decimals>
History: <DK-* identity, related specs, source/PR links, as-of SHA>
How others solve it: <concrete open-source projects or standards; adopt | adapt | build, and why>
Research: <large items only, by the research rule in AGENTS.md>
Accepted: <date>, <owner | orchestrator | orchestrator relaying the owner>
Actual: <h> h (intent rounds <h> h; PR #<n> open→merge <h> h), added when the work merges
```

Category: defect = current behavior breaks a contract or spec; feature = a new
owner-visible capability; improvement = a better existing capability; infra = CI,
release, deploy, observability, operations, dependency updates; refactor = code structure without
behavior change, including enablers a feature needs; research = a time-boxed
investigation whose output is a decision or cards. An acceptance card takes the
category of the work it accepts.

Value is the benefit to the owner. Effort is in story points (Fibonacci 1, 2, 3,
5, 8, 13, 21): relative size from the amount of work, complexity and
uncertainty or risk, estimated by comparison with the reference stories below,
not in hours and not from human-team experience. Reference stories (merged PRs):
1 = #59 (one-line-scope release fix); 2 = #66 (small release fix with test);
3 = #52 (expire unprocessed originals by policy); 5 = #68 (exact source SHA in
images and release record); 8 = #40 (blue-green HTTP releases). Score stays
Value / Effort. Tiden's priority field keeps its meaning (High, Medium, Low) and
still raises review levels; the order within a share comes from Score.

13 and 21 are size signals only (21 is much larger): such a card becomes a parent
(Feature, or Epic for 21) that keeps the overall estimate, with child Stories of
at most 8, each an independent increment and one PR. Steps inside a story are the
worker's plan, not cards. Ready cards are at most 8; the card review checks every
8 for a split.

When a card's work merges, the worker records the actual time on the card in
hours, as one total from first start to merge (the parts overlap; never sum
them), with the parts: intent rounds from `tiden intent journal --format json`
(StartedAt to ClosedAt) and the PR's open-to-merge time; a recorded skip has the
PR times only. The orchestrator keeps the velocity ledger across cards; the weekly
review ([parallel-development.md](parallel-development.md)) reports points per
hour from these pairs, uses it for wave planning and revises the reference
stories.

## Working loop

### Current truth and future proposals

Put **new future-only** requirements on a separate proposal branch per independently
deliverable increment. Record priority, dependencies and historical IDs there.
Keep the proposal open until its behavior and evidence are reviewed. Do not merge
an entire planning collection into main merely to register its existence: a branch
merge admits all of its reviewed changes, not one selected task.

Main contains maintained behavior and its known deficiencies. Existing defects,
unverified operational acceptance and unresolved production incidents remain
readiness concerns. A mixed item needs an explicit current-defect/operational-risk
boundary and a separate future-increment boundary before any relocation. A new
filter, output field or contract change is not automatically a regression.

The initial backlog import already placed future requirements on main. In the
CLI 0.33.0 and UI inspected on 2026-10-16, no supported main-to-proposal relocation
preserving the same spec identity/history was found. `spec update --branch` makes
a copy-on-write proposal; it does not remove the original from main. Creating a
release does not provide a documented spec-status filter. Until an identity-safe
relocation is verified, keep those records and report the scope debt explicitly.
Do not substitute delete/recreate, copied cards, `Done`, fabricated links or risk
acceptance for a move. A private migration manifest is an audit artifact, not a
second editable backlog.

### Readiness decision

Record these independently, with timestamp, branch/release ID and exact Git SHA:

1. Native Tiden main/branch gate and its actual reasons. A feature subtree check
   is only a subtree result; a cached verdict keeps its original computed time.
2. A captured gate for the candidate release/environment and matching CI evidence.
   A missing release gate is unknown, not a pass. A package version shared by
   several commits cannot establish the deployed SHA.
3. Unresolved production issues and component attribution, plus the named live
   acceptance requirements. A main `prod_issues: PASS` does not supersede this
   review: the vendor documents production-issue gating for attributed release
   scopes. Unattributed errors and absent release snapshots require investigation.
4. The release review ([review-protocol.md](review-protocol.md)) or its
   recorded `none` reason: the reviewed range, each part's verdict line and
   report path, and its open blocking or major findings.
5. The release spec sync (below) or its recorded `none` reason: the review
   branch, the specs edited, the tests linked, the anchors added, the cards set
   to delivered, the audited range and its artifact, and the semantic baseline
   SHA it leaves (unchanged by a partial sync or `none`).

Readiness remains blocked while a relevant current defect, unresolved production
incident or required acceptance lacks evidence, or a release review finding
that holds the release ([review-protocol.md](review-protocol.md)) is open. This
is the project's release decision; do not present it as a newly computed native
Tiden verdict. Classifying future proposals alone
does not repair runtime incidents or establish readiness.

For issue triage, read `issue fix-context`, every suspect source path and the
relevant historical revision. `verified` on a suspect spec is not proof that its
tests cover the incident. Report truncation. Preserve redaction and issue history;
resolve only after a matching regression is demonstrated and the repair verified.
Deployment acceptance remains separate, and old `lastSeen` alone proves nothing.

### Release spec sync

A normal-level pull request that changes no acceptance criterion skips the
intent loop (`AGENTS.md`), so its spec writes wait for this release step: links
for its new tests, spec text outside the criteria that it made stale (Boundary,
evidence notes) and the delivered status of the card it names. Anchors for its
new code files cannot wait: it adds them before or in the same step as its merge,
or the drift workflow fails. The same step audits spec text against the code since the semantic
baseline: the candidate SHA of the last completed full sync, recorded in the
readiness record (item 5 above). The drift workflow advances the watermark after
every merge once changed code is anchored (Reconciliation and maintenance), so
the watermark never stands in for that baseline. The release owner runs the sync
before the release review whenever the delta from the semantic baseline to the
candidate changes a product file or a skipped pull request merged since the last
completed spec sync waits for its spec writes, and otherwise records
`Spec sync: none — <reason>`. The first semantic baseline is
69486f43b7140632c5eeed6a27690683e3a82c68, the target of the ANYM-314 catch-up
audit. Example: the last full sync was at A, the drift
workflow has moved the watermark to C, and the candidate is C; the sync audits
A..C, not the empty watermark range.

1. One worker session (Tiden card and spec text,
   [parallel-development.md](parallel-development.md)) with `tiden intent skip`
   works on one review branch, `review/<date>-spec-sync`.
2. Its inputs are every file changed from the semantic baseline to the
   candidate (`git diff --name-only <baseline> <candidate>`), matched against the
   anchors as in Implementation sequence step 4, and the `Spec impact:` and
   `Spec sync:` lines of every skipped pull request merged since the last
   completed spec sync, whether or not the watermark has since moved past it (an
   automated or other advance does not complete these writes).
3. On the review branch it checks each matched spec's text against the code and
   corrects stale text; it anchors each changed file that no anchor matches to
   the spec it implements or records why none applies; it links each named new
   test with `tiden test link-spec` after reviewing its assertion; and it sets
   each named card to its delivered status. It changes no acceptance criterion:
   a skipped pull request that needed one should have run the full intent loop,
   and that is a release review finding ([review-protocol.md](review-protocol.md)).
4. Its edits are spec edits and take the reviewers `AGENTS.md` assigns them by
   kind; a High or Critical spec or a weakened criterion is never
   checklist-only. When the release review's Part A runs, they enter its packet
   as previous and proposed text instead of a separate review. After that review
   the branch merges under the review levels in `AGENTS.md`, and Tiden main is
   read back.
5. A sync that covered the whole range is the delta audit under Reconciliation
   and maintenance: its candidate SHA becomes the new semantic baseline, and when
   the watermark is still behind it, it advances by a drift-sync branch merge
   under the rules there. Until the drift audit card (ANYM-314) has completed once, the sync is
   partial: it covers only the skipped pull requests since the last completed
   spec sync, completes no drift audit and moves neither the semantic baseline
   nor the watermark.

### Names and release identity

DeedKit is the platform; AnyMsg is the compatibility identity of its messaging
package. Messaging telemetry names its release `deedkit@` plus the full source
commit from `DEEDKIT_SOURCE_SHA`; without a valid commit it keeps `anymsg@` plus
`messaging_service.__version__` (0.9.19 at the reviewed revision), a module version
that is not a platform deployment SHA (TELEMETRY.md). Stack frames are
repository-root paths, so Tiden can attribute them to components. The Tiden
product's historical `ANYM` prefix and existing component/test identities likewise
survive the rename.

Keep historical release records and links. The release record for a revision must
carry the same `deedkit@<sha>` version, its environment and its build URL; the
runtime string alone does not create that association. Do not relabel old events
or manufacture a production release to obtain a gate. Contributor headings and
display text can use DeedKit immediately.

### Implementation sequence

1. Read this guide and `TIDEN-CI.md`. Fetch the relevant current Tiden main specs
   and inspect open intent branches/PRs before creating anything. A match in an
   unmerged branch is still an existing identity, not a reason to duplicate it.
2. For product changes, use `product-context`, read the code its results point
   to, and only then run `start-intent`. Promises are frozen when the session
   starts, so phrase them from what the code actually does. Keep promises about
   observable behavior; never invent a product requirement merely to track a
   documentation edit or bookkeeping session. Use an explicit review branch for
   a bulk spec/backlog maintenance import.
3. Implement in the appropriate Git branch. Update the existing requirement when
   its behavior changes; create a child only for an independent increment.
   Preserve preimages, historical source anchors and accepted test links.
4. Run the local tests `AGENTS.md` names (the touched code's tests and the new
   tests); the full synthetic suites run in CI, which publishes authentic results
   through the existing reporter with exact checkout SHA and branch. Open the pull request
   with the `Tiden intent branch:` line the contract prints; CI routing uses it
   (TIDEN-CI.md). Never rename old
   tests or the seven `anymsg-ci::*` checks. Review assertions before proposing
   links: even an exact source-file match does not prove a whole specification.
   Build the `Spec impact:` list (`AGENTS.md`): match
   `git diff --no-renames --name-only <merge-base>..HEAD` (both paths of a
   rename) against the `repo_file` anchors in
   `tiden spec list --branch main --include-sources --format json`, let an anchor
   from before the move into `services/messaging/` match without that prefix,
   leave out the `BACKLOG.md` hub, and add the session delta from
   `tiden intent progress`. A delta spec cannot be dropped from the session;
   `tiden intent radius drop` with a reason is for radius specs outside the delta.
   A recorded skip has no session delta; its list ends with the `Spec sync:`
   line for the release spec sync.
5. Review the Tiden merge preview, conflicts and link proposals. Record a Git PR
   and Tiden branch together. A closed intent session or merged Git PR does not
   merge its Tiden branch: merge both in the same step. Merge reviewed spec
   changes under the review levels in `AGENTS.md`; never blanket-merge unrelated
   open branches.
6. Read back actual Tiden main IDs, sources, links, gate and GitHub drift after
   merging. A green branch projection is not a main result. `no_test`, `not_run`
   and failed executions remain visible. Do not create passing evidence or
   accept risk to compensate for a bookkeeping gap.
7. Close only the implemented increment. Keep rollout, capacity, actual host
   wake-up, provider completeness and empirical quality acceptance open until
   their own evidence exists. Update the Tiden item at the same time as the code
   review; do not create a replacement checklist in Markdown.

## Reconciliation and maintenance

Before a bulk change, export complete specs/sources and test identities/links
with `scripts/tiden_snapshot.py`; retain private snapshots outside Git. Its two
complete reads must agree. Network or pagination failure is an incomplete export,
not permission to claim preservation from equal counts.

Reconcile already merged code's spec branches before importing new work. Review
each proposed link and scope boundary. Leave experiments with open code PRs in
their own branches. An old recovery branch can contain duplicates of the present
baseline; preserve its history and record the disposition instead of merging
duplicates or deleting requirements to clean a dashboard.

After the update, compare exact object/link identities and historical anchors,
with a reviewed allowlist of intended additions and changes. Use
`scripts/verify_tiden_preservation.py` for complete snapshots. Report source
coverage, assertion review and live acceptance as distinct audit depths.

The observed CLI 0.33.0 source update/merge path regenerates source-record IDs
and does not round-trip nested `reverse_pr` metadata. Preserve each affected
original ID in `metadata.historical_source_id` and its complete original metadata
as serialized JSON in `metadata.historical_metadata_json`, alongside the original
source fields. Read these values back before a bulk update, and verify them after
merge. This is explicit provenance preservation, not permission to drop metadata
or to change specification, test or accepted-link identities. Do not overwrite
an existing provenance receipt during later updates.

Drift compares Git main with the spec-sync watermark. Re-anchoring, PR
links and component changes are bookkeeping, not verification: they do not make
spec text current. The drift workflow (`.github/workflows/tiden-drift.yml`,
[TIDEN-CI.md](../../TIDEN-CI.md)) advances the watermark by itself after every
merge, with `--reason empty_sync`, and only when every code file changed since
the watermark has an anchor on some spec; otherwise it fails, names the files and
leaves the watermark. Every pull request therefore anchors its new code files on
the specs they implement before or in the same step as its merge. The watermark means "changed code is
anchored", not "spec text audited": the audit of spec text is the release spec
sync, tracked by its own semantic baseline. `empty_sync` is a compare-and-set on
`--from`: on 2026-10-18 a stale `--from` returned `"advanced": false` and the
current one advanced (receipts on ANYM-355); only the drift workflow uses it.
A manual advance follows a delta audit that has checked every spec anchored to
files changed since the previous audit against the code, anchored every changed
file without a matching anchor or recorded why none applies, and merged its edits
on Tiden main. It merges an empty drift-sync branch
(`tiden branch create --sync-base <W> --sync-target <SHA> --sync-repo
github.com/GoSync-Inc/deedkit`, recorded as `sync_merge`); `baseline` only sets
a first watermark (it returned `"advanced": false` on an existing one) and
`bootstrap` is never used. Record the audited SHA, the spec count and the
findings in the audit artifact, outside Git.
A new commit after the audited target is fresh drift, not a reason to claim the
old review covered it.

Useful read-only commands (pass the existing product/workspace explicitly when
the worktree is not bound):

```sh
tiden spec list --branch main --include-sources --format json
tiden branch list --format json
tiden branch merge-preview --name <branch> --format json
tiden coverage-gaps --product-id <product-id> --branch main --format json
tiden gate check --product-id <product-id> --format json
tiden drift status --product-id <product-id> --format json
```

Honor pagination and save complete JSON before summarizing it. A CLI failure is
unknown state. Do not infer completion from a truncated display or a past chat.

## Vendor references

- [Specs and structured provenance](https://docs.tiden.ai/docs/product/specs)
- [Recommended operating model](https://docs.tiden.ai/docs/product/overview)
- [Requirement import](https://docs.tiden.ai/docs/how-to/import-requirements)
- [Branch review and merge](https://docs.tiden.ai/docs/how-to/manage-branches)
- [Quality gates and traceability](https://docs.tiden.ai/docs/product/quality-gates)
- [Agent configurations and intent capture](https://docs.tiden.ai/docs/how-to/configure-agents)

The installed CLI help and shipped skills resolve version-specific commands;
the project rules above define how DeedKit uses them.
