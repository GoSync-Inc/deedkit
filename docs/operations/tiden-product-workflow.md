# Product work through Tiden

Tiden is DeedKit's record of product requirements, planned work, delivery state and test
evidence. `BACKLOG.md` is a navigation entry point and legacy-ID index; keep no other editable
queue in module documents, plans, PR descriptions or chat summaries. How a change records itself
(the first-step lookup, `spec:` markers, `Spec:`/`Delivers:` lines, check A, sync B) is in
[AGENTS.md](../../AGENTS.md) section 4; CI details in [TIDEN-CI.md](../../TIDEN-CI.md).

Use the existing [DeedKit product](https://app.tiden.ai/1dc18e09-4a09-4d74-8300-cd4302622357/products/75bd0580-2af4-4d5a-b236-c5a9d7ff7ce1)
(workspace `1dc18e09-4a09-4d74-8300-cd4302622357`, product
`75bd0580-2af4-4d5a-b236-c5a9d7ff7ce1`; pass them when a worktree is not bound). Never create a
replacement product, reimport existing tests or reset the drift watermark to make a dashboard
green. Skills come with the CLI (`tiden skill list --agent claude-code --global`); use
`product-context` for lookups, `link-coverage` and `triage-issues` as their names say, and do
not fork the vendor skill bodies.

## Specs and statuses

An Epic describes a product outcome, a Feature groups capabilities, a story is independently
reviewable behaviour with acceptance criteria. Markers and `Delivers:` lines name stories only.
Keep IDs, parents, components, sources and test links stable.

Status, merged source, coverage and deployment are separate facts. `Backlog` is future or
conditional work, `Active` work being implemented, `Review` an implementation or acceptance
awaiting evidence, `Done` only the stated delivered scope. A status update never weakens a
contract, hides a failing test or removes it from verification.

An open live or measured check is written in the spec as `[open: live]` or `[open: measured]` on
its criterion or evidence line. Whoever records its evidence (version, environment, run or
measurement, date) removes the marker in the same edit, and only then. A card with an open check
stays in `Review`; the `Tiden health` report fails on a `Done` spec that carries one (text inside
inline code or a fenced block is ignored, so a spec may document the marker; TIDEN-CI.md).

Main holds maintained behaviour and its known defects. New future-only requirements go on a
proposal branch per deliverable increment, which stays open until implemented; a current defect
goes on a review branch. Never delete and recreate a spec to simulate a move; no supported
relocation preserves identity (CLI 0.36.0).

## Card intake

New work comes from the owner, agent feedback (`submit_feedback`), production issue triage and
audits. A worker that finds work outside its task reports it to the orchestrator.

1. **Triage.** Search Tiden main and open branches. A match, merged or not, is updated, not
   duplicated. A declined candidate gets its reason at its source.
2. **Write** from the template, on a proposal or review branch, never on main: Opus for big
   cards, Sonnet for simple ones.
3. **Review** by kind (AGENTS.md section 5): product cards (feature, improvement, epic) one Fable
   round with the product-manager prompt; cards that set a contract in code, CI, data or security
   Astra r1 and at most one scoped recheck; small cards the author's self-check. Reviewers first
   judge whether the card should exist and may shrink or reject it.
4. **Accept.** The owner accepts feature and epic cards; the orchestrator the others
   (improvement, defect, infra, refactor, research). Record who accepted.
5. **Queue.** Current breakages and deadlines first; then about 70% product and 30% engineering
   by Effort, at least one refactor when one is ready, each share by Score. From 10-19 no new
   meta-research or process tooling is queued until ten product cards are delivered, unless the
   owner asks; incident fixes are allowed.

```text
Title: <plain words: what changes for the owner>
Problem: <what is wrong or missing and the desired behaviour; evidence, source>
Acceptance criteria:
- <observable, testable criterion>
Category: defect | feature | improvement | infra | refactor | research
Lane: <lane>   Expected paths: <paths>
Depends on: <ANYM-n, or none>
Priority: High | Medium | Low (Tiden's priority field)
Boundary: <what is already implemented; what remains>
Evidence to close: <tests and checks; any separately authorized live action>
Value: <1|2|3|5|8>   Effort: <1|2|3|5|8; 13 and 21 only before the split>   Score: <Value / Effort, two decimals>
History: <DK-* identity, related specs, source/PR links, as-of SHA>
How others solve it: <open-source projects or standards; adopt | adapt | build, and why>
Accepted: <date>, <owner | orchestrator | orchestrator relaying the owner>
```

Categories: defect = current behaviour breaks a contract or spec; feature = a new owner-visible
capability; improvement = a better existing one; infra = CI, release, deploy, observability,
operations, dependencies; refactor = structure without behaviour change, enablers included;
research = a time-boxed investigation that ends in a decision or cards. Defect, infra and
refactor work needs no new card when an existing spec covers it (AGENTS.md section 4).

Value is the benefit to the owner. Effort is story points (1, 2, 3, 5, 8, 13, 21) by comparison
with merged PRs: 1 = #59, 2 = #66, 3 = #52, 5 = #68, 8 = #40. A 13 or 21 becomes a parent with
child stories of at most 8, each one PR. For a new module, an infrastructure mechanism, an
external integration or more than ~300 new lines, `How others solve it` is a 30–60 minute study
(open source, standards, built-ins of Docker, Caddy, Postgres or GitHub), and the worker
re-checks it for 10–15 minutes before starting.

Issue triage (`triage-issues`, `issue fix-context`): read every suspect source path; `verified` on
a suspect spec is not proof that its tests cover the incident. Resolve an issue only after a
matching regression test fails before and passes after the fix, never on age or a nearby green
test, and never start a provider session or weaken redaction to diagnose one.

## One Tiden branch per pull request

A worker runs `tiden intent skip` and reads the card and `product-context` before code; it
opens no `intent/` branch. Tiden changes in step with each PR: the card's proposal or review
branch carries the card, every criterion edit or flag made while coding and the settle text; the
PR names it as `Tiden branch:`, the reviewers read it with the diff, check A reads its specs and compares
its merge preview with the `Spec:`/`Delivers:` lines, and sync B merges it right after the squash, so the
spec text is current at merge. This holds for every PR that changes behaviour; a PR that delivers no new card and changes no spec text names no branch.
Each deliverable has its own branch, because B merges only the specs a PR names.

Every PR description carries `Spec impact:` (the `Spec:` and `Delivers:` IDs, each with its
flags, such as a criterion deferred or not built, and the anchors it adds, or `none`) and
`Settle text:` (`none`, or a reviewer copy of the settle text on the PR's `Tiden branch:`).
Anything a PR missed is the backstop's: `Tiden drift` and `Tiden health` report it after the main
push, and a follow-up PR's branch fixes it. Maintenance windows (TIDEN-CI.md) remain only for hand
repairs that B cannot do, bulk restructuring and deletions.

The intent loop is paused (owner 10-19; the pilot ended in PR #162): no PR runs `tiden intent
start`, settle and close or uses the `start-intent` and `capture-intent` skills, and none needs a
`Tiden intent branch:` line (CI then records a skipped route and stays green). The loop returns
after a trial the orchestrator starts once Tiden fixes settle and close at DeedKit scale (settle
times out reading an intent branch; close counts specs a rebase pulled in, reads main's run
instead of the PR's and times out reading branch specs). Three feature PRs in a row that the
orchestrator picks run the full loop as the exception, and each settles and closes without a
timeout or an owner risk acceptance; a failure restarts the count. The orchestrator reports the
trial's start and result in the digest, and the loop returns with that report.

Criteria change only through a card's branch reviewed with its PR; no daily delta audit runs.

## Commands (CLI 0.36.0)

```sh
tiden intent skip --reason "<why>"                                   # first Tiden call
tiden spec get <id> --format json | jq '{title, status, content}'     # the card, once
tiden intent lookup "<task>" --path <repo path> --output <file>       # context, once, saved
tiden spec list --branch main --include-sources --format json         # anchors; save, then jq
tiden branch list --format json
tiden branch create --name proposal/<date>-<slug> --description "<card>"   # card writers
tiden spec create --branch <branch> --title "<t>" --type "User Story" --parent-id <id> --content-file <f>
tiden spec update --id <id> --branch <branch> --content-file <f>      # only on the PR's branch
tiden branch merge-preview --name <branch> --format json              # before naming it in a PR
tiden drift status --product-id <product-id> --format json
```

No `spec update` on main, `branch merge`, `test link-spec` or hand-written `--sources-file`:
sync B does those. A failed read-only listing is rerun once; a write is never retried. Honour
pagination (`nextPageToken` empty, `totalCount` equal to the rows) and save complete JSON
before summarizing it. A CLI failure is unknown state. Bulk maintenance (snapshots, provenance,
manual watermark advances) follows
[the reconciliation receipt](../quality/tiden-reconciliation-2026-10-16.md#maintenance-procedures).
