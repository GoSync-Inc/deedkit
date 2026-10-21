# Product work through Tiden

Tiden is the project's record of product requirements, planned work, delivery state and test
evidence. Keep no other editable queue in module documents, plans, PR descriptions or chat
summaries (a project may keep a navigation index). How a change records itself (the first-step
lookup, `spec:` markers, `Spec:`/`Delivers:` lines, check A, sync B) is in process.md section 4;
CI details in the project's Tiden CI document. The project names its Tiden product and workspace
IDs; pass them when a worktree is not bound.

Never create a replacement product, reimport existing tests or reset the drift watermark to make
a dashboard green. Skills come with the CLI (skills.md); use `product-context` for lookups,
`link-coverage` and `triage-issues` as their names say, and do not fork the vendor skill bodies.

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
stays in `Review`; where the project runs a `Tiden health` report, it fails on a `Done` spec that
carries one (text inside inline code or a fenced block is ignored, so a spec may document the
marker).

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
3. **Review** by kind (process.md section 5): product cards (feature, improvement, epic) one Fable
   round with the product-manager prompt, which also checks `How others solve it` (adopt | adapt |
   build, sized to the task) and whether the card should be split; cards that set a contract in
   code, CI, data or security Astra r1 and at most one scoped recheck; small cards the author's
   self-check. Reviewers first
   judge whether the card should exist and may shrink or reject it.
4. **Accept.** The owner accepts feature and epic cards; the orchestrator the others
   (improvement, defect, infra, refactor, research). Record who accepted.
5. **Queue.** Current breakages and deadlines first; then by Score, with the project's share
   rules (its AGENTS.md or workflow document).

Writers and reviewers by kind: Opus high writes new capability, infrastructure and main-research ones; Opus medium defects and small improvements with clear evidence; Sonnet simple ones. Product cards: one Fable round (research note, split), contract cards (code, CI, data, security) and operation plans Astra, small cards self-check.

```text
Title: <plain words: what changes for the owner>
Problem: <what is wrong or missing and the desired behaviour; evidence, source>
Acceptance criteria:
- <observable, testable criterion>
Category: defect | feature | improvement | infra | refactor | research
Lane: <lane>   Expected paths: <paths>
Depends on: <PREFIX-n, or none>
Priority: High | Medium | Low (Tiden's priority field)
Boundary: <what is already implemented; what remains>
Evidence to close: <tests and checks; any separately authorized live action>
Value: <1|2|3|5|8>   Effort: <1|2|3|5|8; 13 and 21 only before the split>   Score: <Value / Effort, two decimals>
History: <legacy identity, related specs, source/PR links, as-of SHA>
How others solve it: <open-source projects or standards; adopt | adapt | build, and why>
Accepted: <date>, <owner | orchestrator | orchestrator relaying the owner>
```

Categories: defect = current behaviour breaks a contract or spec; feature = a new owner-visible
capability; improvement = a better existing one; infra = CI, release, deploy, observability,
operations, dependencies; refactor = structure without behaviour change, enablers included;
research = a time-boxed investigation that ends in a decision or cards. Defect, infra and
refactor work needs no new card when an existing spec covers it (process.md section 4).

Value is the benefit to the owner. Effort is story points (1, 2, 3, 5, 8, 13, 21) by comparison
with merged PRs (the project names its reference PRs). A 13 or 21 becomes a parent with
child stories of at most 8, each one PR. For a new module, an infrastructure mechanism, an
external integration or more than ~300 new lines, `How others solve it` is a 30–60 minute study
(open source, standards, built-ins of the platforms the project uses), and the worker
re-checks it for 10–15 minutes before starting.

### Writing a card or spec text

Self-check before every submission and after every fix; record it in the card's research note
(process-diet 2026-10-18: these rules prevent most review findings):

1. Code facts: every claim about existing code, schema, limits or tests cites `file:line` or the
   command run; trace each new job/artifact through every existing consumer.
2. Failure scenarios for anything that mutates, releases or ingests: rerun, retry, partial
   failure, crash between two writes, concurrent actor, rollback, uncertain result.
3. External tools (GitHub Actions, Docker, Postgres, OpenSSH ...): back each claim with a minimal
   executed probe, quoted.
4. Adversary: for each security/integrity control, name the bypass and the test that shows it
   blocked.
5. Consistency sweep: grep each changed term; reconcile criteria, Boundary, Evidence, Depends on
   and sibling cards.
6. Testable criteria: each names its test, where it runs (is the binary in CI?) and a
   deterministic pass/fail.
7. Authority: name the acting principal; quote the owner decision and what it authorizes.
8. Data egress: list every datum crossing a trust boundary and who may see it.

### Issue triage

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
Each deliverable has its own branch, because B merges only the specs a PR names. A project without
check A and sync B in CI does those steps by hand (differences.md).

Every PR description carries `Spec impact:` (the `Spec:` and `Delivers:` IDs, each with its
flags, such as a criterion deferred or not built, and the anchors it adds, or `none`) and
`Settle text:` (`none`, or a reviewer copy of the settle text on the PR's `Tiden branch:`).
A project helper may build the `Spec impact:` list (DeedKit: `scripts/spec_impact.py`): the specs
anchored to the changed files and those the body names, each marked `named (Delivers)`,
`named (Spec)` or `anchor only`. It suggests flags; the author still checks every listed spec and
flags it by hand when needed. Anything a PR missed is the backstop's: `Tiden drift` and
`Tiden health` report it after the main push, and a follow-up PR's branch fixes it. Maintenance
windows (the project's Tiden CI document) remain only for hand repairs that B cannot do, bulk
restructuring and deletions.

The intent loop is paused (owner 10-19; the pilot ended in DeedKit PR #162): no PR runs `tiden intent
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
sync B does those (a project without sync B: differences.md). A failed read-only listing is
rerun once; a write is never retried. Honour pagination (`nextPageToken` empty, `totalCount`
equal to the rows) and save complete JSON before summarizing it. A CLI failure is unknown state.

Tool notes: `tiden spec get` needs the UUID (find it with `tiden spec list [--branch …]
--format json` and jq on `seqNum`); Tiden output is not piped to head, tail or grep (a hook
blocks it), use `--format json` and jq; a Tiden read costs about $0.21, so read saved JSON
instead of live when possible.
