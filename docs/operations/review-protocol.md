# Review protocol

This document is the procedure for the reviews that Review levels in
[AGENTS.md](../../AGENTS.md) require: what reviewers need not repeat, the
checklists, the reviewer packet, the round protocol and the release review. Who
reviews what, and who merges, stays in `AGENTS.md`. Model and effort for each
kind of work are in [parallel-development.md](parallel-development.md).

## Tool results and author records

Reviewers spend their time on what no tool proves: logic, failure modes,
security, concurrency, cross-file consistency and whether text matches code and
specs. They do not re-run what a tool already proved:

- the required checks, and the merge queue's full test run of every commit that
  reaches `main` (a docs-only PR run may skip tests; the queue never does);
- `ruff`, `actionlint`, `zizmor` and the other linters in CI;
- an intent session closed with PASS: the promised specs' linked tests ran and
  passed on the session's commits.

Author records are claims, not tool results, and reviewers audit them: the
`Spec impact:` list (the packet carries its inventory: changed paths, matched
anchors and the session delta) and the "fails without the fix" record. A
passing linked test proves only what its assertion checks.

**Fails without the fix.** A pull request that changes behavior names the tests
that prove the change. In a temporary worktree at the PR head, keep the tests
and their harness, undo only the behavioral change (restore the changed
non-test files to the merge base, or replace a new function's body with a stub
when its module is new), and run the named tests. They must fail on the
assertion of the changed behavior and pass at the head; an import, setup or
unrelated failure does not count. The PR records `Fails without the fix:
<tests> failed on <assertion> with <change> undone`. A refactor, a test-only
change or a dependency update records `Fails without the fix: n/a — <reason>`
and the tests that pass on both revisions; a documentation-only change records
`n/a — documentation only`.

## Checklists

**Author checklist.** The author goes through it before asking for review, and
for a normal-level pull request instead of a per-PR reviewer:

- The review level and lane follow the signals in `AGENTS.md`: a normal PR has
  no security, access (agent permission settings included), data model, CI,
  release, migration or High/Critical spec change.
- The "fails without the fix" record exists (a vacuous test once hid a
  recursion, PR #64).
- Every new or changed acceptance criterion has a test that exercises it or is
  marked pending (a claim without a test, PR #88).
- Each `no behavior change` disposition was checked against the criteria the
  diff touches, and each spec whose criteria the diff changes, may change,
  restores or newly tests is `(flagged)` (`AGENTS.md`).
- A skipped code pull request is normal level, changes no acceptance criterion
  and has its `Spec sync:` line (`AGENTS.md`).
- Every changed term, ID, path and test name was searched for (`git grep`) and
  each reference to it re-read.
- A rerunnable job that uploads an artifact sets `overwrite: true`; a suite that
  needs a binary fails in CI instead of skipping (PRs #83, #86).

**Card checklist.** A checklist subagent (Sonnet, medium) reads a card or spec
edit against it when `AGENTS.md` gives it the checklist review:

- every template field from the card intake is present;
- the problem has evidence; no spec in the packet's search results covers the
  same need, and no smaller, existing or built-in solution does; the card says
  why it is needed now;
- the ANYM IDs, test names and paths it names exist;
- Score is Value / Effort with two decimals, Effort is a Fibonacci number and a
  ready card is at most 8;
- the component matches the expected paths;
- against the previous text, no acceptance criterion is dropped without saying
  so, and Boundary, criteria and tables do not contradict each other;
- a claimed approval is the owner's quoted words or a standing approval with
  conditions and an expiry;
- the review level it claims follows `AGENTS.md`; the research note exists and
  fits the card's size.

**Process-text checklist.** The same subagent reads a normal-level change to
agent rules or process documents before it merges, because such text is in
force from the merge:

- it changes no review level, merge authority, owner-level trigger or what
  the owner keeps (those are owner-level) and no permission or access rule (at
  least important);
- it does not contradict the other process documents it names or that name it;
- every changed term was searched for and its references re-read;
- each rule says who does what and when, without a term left undefined.

## Reviewer packet

A review is one packet of files and one report file, written into the author's
scratchpad; reviewers have no Tiden access. Packets carry projections, never
whole exports.

- `task.md`: unit, review level, round, the intent goal and promises (the task
  goal for a skip), what this reviewer checks (`AGENTS.md`), and for a recheck
  its own open findings;
- `diff.patch`: `git diff <base>..<head>` of the reviewed range; for a recheck,
  only the fix range;
- `specs.md`: the current text with acceptance criteria of the promised and the
  flagged specs, and the `Spec impact:` list with its inventory, which gives the
  rest by ID and disposition, and the spec text the author will write at
  settle, as previous and proposed text. A reviewer that needs the text of an
  unflagged spec names it as a missing input;
- `pr.md`: the pull request description.

A card or spec edit has no diff; its packet holds instead the previous and the
proposed text, the search results for similar specs (ID, title, status), the
referenced specs' text, `git ls-files -- <expected paths>`, the research note
and the quoted approval it relies on. A missing input is a finding, not a
reason to guess.

Not whole files, other reviewers' reports or the review history. The reviewer
writes `review-<reviewer>-<unit>-r<n>.md` next to the packet. Each finding has a
severity, `file:line`, the failure scenario and a suggested fix. Severities:
**blocking**, a contract, security or data defect would ship; **major**, a real
defect or an untested criterion; **minor**; **nit**. The reviewer returns only
one line:

```text
VERDICT: APPROVE | CHANGES REQUIRED — <b> blocking, <m> major, <n> minor — <report path>
```

## Rounds

A round is one review by one reviewer of the whole packet: the diff and the
proposed settle text together, never a separate round for either. The budget is
two per reviewer: the review and one recheck, which only a blocking or major
finding opens.

1. **Scope freezes** when the first review starts: an optional addition, the
   owner's included, goes to a follow-up card or the next package, and the
   orchestrator holds it until the round ends. A cancellation, a stop order, or
   a change to authority, approval conditions, required acceptance or safety
   constraints is relayed at once; the affected work pauses until the packet
   reflects it.
2. **One fix pass.** The author fixes every reviewer's findings together, then
   makes a consistency pass: search for every term the fixes changed and re-read
   the clauses that refer to it. The PR records `Consistency pass: <terms>`.
3. **Recheck** by each reviewer that raised blocking or major findings, on the
   fix range only, with its own open findings: it verifies each item and scans
   the fix diff. It is never a fresh full review. For a card or spec edit the
   fix range is the diff between the reviewed and the fixed text. A fix that
   materially changes what another reviewer approved goes to that reviewer too,
   on that part only. On an owner-level PR, a fix range that touches a
   migration, data or security path goes to both reviewers.
4. **Minors and nits** are fixed without review or recorded in the PR as a
   follow-up with its card. A recheck's minor never opens another round.
5. **Closure.** A blocking or major finding stays open until the reviewer that
   raised it confirms the fix. When the budget is spent with one still open,
   the orchestrator either asks that reviewer for one more fix-range recheck of
   the open items only, or the scope is reduced or the change reverted;
   merging with it open accepts risk and is the owner's (`AGENTS.md`).
6. A reviewer is never re-run for wording-only edits, for settle writing the
   text the round reviewed, or for a relayed owner decision that changes no
   rule the review covered.

## Release review

A normal-level pull request has no per-PR code reviewer (`AGENTS.md`); its
code and contract text are reviewed once, before the release that would ship
them. The release review also checks what no per-PR review can see: how the
candidate fits what is deployed. Such an interaction once surfaced only in
production: a CI change to how `main` push runs verify, which the host's older
`production.py` could not read (cold-archive rollout, 2026-10-17).

- **Trigger.** Before the release owner prepares a release or publishes a
  package ([RELEASING.md](../../RELEASING.md)). Part A runs when the delta, from
  the last released SHA to the candidate, holds a normal-level pull request no
  release review has covered. Part B runs when the delta changes CI, release or
  deploy code, the schema, or an interface that a deployed service, host tool
  or verifier consumes. Otherwise the release owner records `Release review:
  none — <reason>`.
- **Part A, normal PRs.** Astra reviews their code, Fable their spec,
  contract-document and process text and the release spec sync's spec edits
  ([tiden-product-workflow.md](tiden-product-workflow.md)), each only when that
  part is not empty.
  `diff.patch` holds those PRs' squash commits (`git show <sha>...`); `specs.md`
  and `pr.md` hold their specs and descriptions.
- **Part B, compatibility.** Astra reviews `delta.md` (title, level and changed
  files of every PR in the delta), `deploy.md` (the deployed SHAs of each
  service, host tool and verifier the release touches, the schema transition and
  the apply order) and the diffs of the interface files only, not the PRs'
  whole code again. An unknown deployed SHA prevents a pass. A new commit in the
  candidate re-opens Part B; Part A runs again only for a normal-level pull
  request it has not covered.
- **What blocks.** An open blocking or major finding holds the release
  (Readiness decision in [tiden-product-workflow.md](tiden-product-workflow.md))
  until its fix merges, reviewed at the fix's own level and rechecked by the
  reviewer that raised it, or the pull request is reverted. A finding on agent
  rules or process documents holds the release only when it affects the
  candidate's operation, authority, evidence or acceptance, and the reviewer
  says which; otherwise its fix is a pull request in the current wave. Either
  way, a blocking or major finding on a procedure stops that procedure's use
  until the fix merges or the owner decides. Minors become cards.
- **Record.** The readiness decision for the candidate SHA records the reviewed
  range, each part's verdict line and report path, or the `none` reason.
- Pull requests that `AGENTS.md` reviews before merge are not reviewed again
  here except for Part B.
