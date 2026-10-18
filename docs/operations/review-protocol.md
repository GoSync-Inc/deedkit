# Review protocol

This document is the procedure for the reviews that Review levels in
[AGENTS.md](../../AGENTS.md) require: what CI and Tiden already prove, the
author checklist, the reviewer packet, the round protocol and the release
review. Who reviews what, and who merges, stays in `AGENTS.md`; do not restate it
here. Model and effort for each kind of work are in
[parallel-development.md](parallel-development.md).

## What reviewers do not re-check

Reviewers spend their time on what no tool proves: logic, failure modes,
security, concurrency, cross-file consistency and whether text matches code and
specs. They do not re-check what these already prove:

- the required checks, and the merge queue's full test run of every commit that
  reaches `main` (a docs-only PR run may skip tests; the queue never does);
- `ruff`, `actionlint`, `zizmor` and the other linters in CI;
- an intent session closed with PASS: the promised specs' linked tests ran and
  passed on the session's commits;
- the `Spec impact:` list built as `AGENTS.md` describes (reviewers check each
  disposition, not the list's construction);
- the "fails without the fix" record below.

**Fails without the fix.** A pull request that changes code names the tests
that prove the change and shows that they fail without it. In a temporary
worktree at the PR head, restore the non-test files to the merge base
(`git checkout <merge-base> -- <non-test paths>`), run the named tests and see
them fail. The PR records `Fails without the fix: <tests> failed with <paths>
restored to <merge-base>`. A test that passes there proves nothing and is a
finding.

## Author checklist

The author goes through this list before asking for review, and for a
normal-level pull request instead of a per-PR reviewer. Each item names the
kind of defect it has caught.

- The review level and lane follow the signals in `AGENTS.md`: a normal PR has
  no security, access, data model, CI, release, migration or High/Critical spec
  change.
- Every changed behavior has a test, and the "fails without the fix" record
  exists (a vacuous test once hid a recursion, PR #64).
- Every new or changed acceptance criterion has a covered-by test or is marked
  pending (a claim without a test, PR #88).
- Each `no behavior change` disposition was checked against the criteria the
  diff touches.
- Every changed term, ID, path and test name was searched for (`git grep`) and
  each reference to it re-read; no stale cross-reference is left.
- A rerunnable job that uploads an artifact sets `overwrite: true`; a suite that
  needs a binary fails in CI instead of skipping (PRs #83, #86).

**Card checklist.** A checklist subagent (Sonnet, medium) reads a card or spec
edit against this list when `AGENTS.md` gives it the checklist review, and
returns findings like any reviewer:

- every template field from the card intake is present;
- the problem has evidence, and no card in the packet's title list of Tiden
  specs covers the same need;
- the ANYM IDs, test names and paths it names exist (the packet carries a
  Tiden export of the referenced specs and the repository file list);
- Score is Value / Effort with two decimals, Effort is a Fibonacci number and a
  ready card is at most 8;
- the component matches the expected paths;
- against the previous text, no acceptance criterion is dropped without saying
  so, and Boundary, criteria and tables do not contradict each other;
- a claimed approval is the owner's quoted words or a standing approval with
  conditions and an expiry;
- the review level it claims follows `AGENTS.md`; the research note exists and
  fits the card's size.

## Reviewer packet

A review is one packet of files and one report file. The author writes the
packet into its scratchpad; reviewers have no Tiden access.

- `task.md`: unit, review level, round, the intent goal and promises (the task
  goal for a skip), what this reviewer checks (`AGENTS.md`), and for a recheck
  its own previous findings list;
- `diff.patch`: `git diff <base>..<head>` of the reviewed range; for a recheck,
  only the fix range;
- `specs.md`: the current text with acceptance criteria of the promised specs
  and every spec under `Spec impact:`, or the card text;
- `pr.md`: the pull request description.

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

A round is one review by one reviewer. Each reviewer has at most two: the
review and one recheck.

1. **Scope freezes** when the first review starts. A new decision, the owner's
   included, goes to a follow-up card or the next package; the orchestrator holds
   such relays until the round ends. A stop order is never held.
2. **One fix pass.** The author fixes every reviewer's findings together, then
   makes a consistency pass: search for every term the fixes changed and re-read
   the clauses that refer to it. The PR records `Consistency pass: <terms>`.
3. **Recheck** only by a reviewer that raised blocking or major findings, on the
   fix range only, with its own findings list: it verifies each item and scans
   the fix diff. It is never a fresh full review.
4. **Minors and nits** are fixed without review or listed as a follow-up. A
   recheck's minor never opens another round.
5. **After the recheck**, a remaining blocking or major finding is fixed with a
   regression test and listed in the PR. The orchestrator asks for a third,
   fix-range round only when that fix changes a security, data, migration or
   release path. Merging with such a finding unfixed accepts risk and is the
   owner's (`AGENTS.md`).
6. A reviewer is never re-run for wording-only edits or for a relayed owner
   decision.

## Release review

Normal-level pull requests have no per-PR reviewer (`AGENTS.md`); their review
happens once, on the release that would ship them.

- **Trigger.** Before the release owner prepares a release whose delta, from
  the last released SHA to the candidate, contains a normal-level pull request
  that no release review has covered yet. A release without one needs none.
- **Scope.** The squash commits of those pull requests, their descriptions and
  `Spec impact:` lists and the current text of their listed specs; plus the
  titles, levels and changed file list of every pull request in the delta, to
  look for interactions between them. Such an interaction once surfaced only
  in production: a CI change to how `main` push runs verify, which the host's
  older `production.py` could not read (cold-archive rollout, 2026-10-17).
- **Reviewers.** Astra on the code and the interactions; Fable on the spec,
  contract-document and process text in the delta. Run only the part that is not
  empty. For code Codex wrote, Fable reviews the code instead of Astra. The
  packet and rounds follow the sections above, with the delta as the reviewed
  range.
- **What blocks.** An open blocking or major finding holds the release
  (Readiness decision in [tiden-product-workflow.md](tiden-product-workflow.md))
  until its fix merges, reviewed at the fix's own level and rechecked by the
  reviewer that raised it, or the pull request is reverted. A finding on text
  that ships nothing to production does not hold the release; its fix is a pull
  request in the current wave. Minors become cards.
- **Record.** The release receipt names the reviewed range, the verdict lines
  and the report paths.
- **Never batched.** Important and owner-level pull requests, which include
  security and access, migrations and the data model, CI, release and deploy
  code and High or Critical specs, are reviewed before they merge. So is a
  production operation plan, before the owner authorizes it.
